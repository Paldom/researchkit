"""Moonshot AI Kimi provider with built-in web search.

Kimi (Moonshot AI) exposes an OpenAI-compatible chat-completions endpoint, so
this provider reuses the OpenAI SDK pointed at the international
``api.moonshot.ai`` base URL.

Web search uses Moonshot's **official Formula tools** (the current
mechanism; the legacy ``$web_search`` builtin echo is deprecated and,
verified live, feeds the model no search content at all): declare the
``web_search`` function from ``GET /formulas/moonshot%2Fweb-search/tools``,
execute each tool call via ``POST /formulas/moonshot%2Fweb-search/fibers``,
and pass the returned ``encrypted_output`` back as the tool message — the
server decrypts it into model context. Kimi returns NO structured citation
array — sources are recovered from markdown links / bare URLs cited inline
in the answer, the same path the CLI-backed providers use.

Sampling caveat: current Kimi models (kimi-k3, kimi-k2.7*, kimi-k2.6) run
with FIXED sampling parameters — passing ``temperature``/``top_p`` etc. is
rejected — so this provider never sends them.

The module-level helpers (``make_kimi_client``, ``is_kimi_model``) let other
parts of the app (improver, summarizer) use Kimi as a generic
OpenAI-compatible model, mirroring the GLM helpers.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from openai import OpenAI

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
    get_base_system_prompt,
    get_user_prompt,
    get_web_system_prompt,
    get_web_user_prompt,
    provider_http_timeout,
)
from researchkit.safe_io import extract_urls_balanced

logger = logging.getLogger(__name__)

# OpenAI-compatible base URL for the international Moonshot Open Platform.
# Override with KIMI_BASE_URL / MOONSHOT_BASE_URL for the China platform
# (https://api.moonshot.cn/v1) or the Kimi Code subscription endpoint
# (https://api.kimi.com/coding/v1 — Console key, no per-token billing). Keys
# are system-locked: each endpoint 401s on the others' keys.
KIMI_BASE_URL = "https://api.moonshot.ai/v1"

# The Kimi Code coding endpoint serves the same models under different ids
# (verified live: its /models are k3 / kimi-for-coding[-highspeed]). Translate
# Open-Platform-style ids so one models.yaml works against either endpoint.
_CODING_ENDPOINT_MARKER = "kimi.com/coding"
_CODING_MODEL_MAP = {
    "kimi-k3": "k3",
    "kimi-k2.7-code": "kimi-for-coding",
    "kimi-k2.7-code-highspeed": "kimi-for-coding-highspeed",
    "kimi-k2.6": "kimi-for-coding",  # closest available; K2.6 isn't served there
}


def kimi_base_url() -> str:
    """The effective Moonshot base URL (env override or the international default)."""
    return os.getenv("KIMI_BASE_URL") or os.getenv("MOONSHOT_BASE_URL") or KIMI_BASE_URL


def resolve_kimi_model(model: str) -> str:
    """Map an Open-Platform model id to the coding endpoint's dialect if needed."""
    if _CODING_ENDPOINT_MARKER in kimi_base_url():
        return _CODING_MODEL_MAP.get(model, model)
    return model


# Formula URI of the official web-search tool (URL-encoded path segment).
_WEB_SEARCH_FORMULA = "moonshot%2Fweb-search"

# The web_search function as served by GET /formulas/moonshot%2Fweb-search/
# tools (mirrored statically to save a request per run; refetch if drifted).
_WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for information",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for"},
                "classes": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "all",
                            "academic",
                            "social",
                            "library",
                            "finance",
                            "code",
                            "ecommerce",
                            "medical",
                        ],
                    },
                    "description": (
                        "Search domains to focus on. Defaults to 'all' if not "
                        "specified."
                    ),
                },
            },
            "required": ["query"],
        },
    },
}

# A search answer usually needs 1-2 tool rounds; the cap keeps a wedged model
# from looping on the search tool.
_MAX_TOOL_ROUNDS = 6

# kimi-k3 runs always-on thinking at effort max: research completions
# routinely exceed the 180s default HTTP timeout, so research calls get a
# higher floor (mirrors codex/agy). Improver/summarizer calls keep the
# default via make_kimi_client.
_KIMI_RESEARCH_MIN_TIMEOUT = 420.0

# Markdown [title](url) links in the answer carry titles for free.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")

# Appended to research prompts so the answer carries a parseable citation
# list — Kimi returns no citation array, and without the nudge it synthesizes
# prose with zero URLs (verified live). Mirrors the agy provider's fix.
_SOURCES_INSTRUCTION = (
    "\n\nAt the very end, add a '## Sources' section listing EVERY web source "
    "you used, one per line as a markdown link: - [title](url)."
)


def get_kimi_api_key(explicit: str | None = None) -> str | None:
    """Resolve the Kimi API key from an explicit value or the environment.

    ``MOONSHOT_API_KEY`` is the official convention; ``KIMI_API_KEY`` is
    accepted as the researchkit spelling (see ``.env.example``).
    """
    return explicit or os.getenv("MOONSHOT_API_KEY") or os.getenv("KIMI_API_KEY")


def is_kimi_model(model: str | None) -> bool:
    """Return ``True`` when ``model`` is a Kimi / Moonshot API model id.

    Matches ``kimi-*`` and legacy ``moonshot-*`` ids; a ``kimicli`` CLI spec
    is NOT an API model (that routes to the Kimi Code CLI provider).
    """
    if not model:
        return False
    m = model.lower()
    from researchkit.providers.kimicli_provider import is_kimicli_model

    if is_kimicli_model(m):
        return False
    return m.startswith(("kimi", "moonshot"))


def make_kimi_client(api_key: str | None = None) -> OpenAI:
    """Create an OpenAI client configured for the Moonshot endpoint.

    ``max_retries=0`` defers retries to the unified ``network_retry`` policy
    and an explicit ``timeout`` avoids hanging on a stalled socket — matching
    the other OpenAI-based providers.
    """
    key = get_kimi_api_key(api_key)
    if not key:
        raise RuntimeError("MOONSHOT_API_KEY / KIMI_API_KEY not set")
    return OpenAI(
        api_key=key,
        base_url=kimi_base_url(),
        max_retries=0,
        timeout=provider_http_timeout(),
    )


class KimiProvider(BaseProvider):
    """Kimi provider using chat completions with the ``$web_search`` tool.

    Runs queries for social media and/or web research based on ``sources``,
    mirroring the other search providers (analysis text + extracted citations).
    """

    provider_name = "kimi"
    model_name = "kimi-k2.6"

    def __init__(
        self,
        api_key: str | None = None,
        sources: set[str] | None = None,
        model: str | None = None,
    ) -> None:
        """
        Initialize the Kimi provider.

        Args:
            api_key: Moonshot API key (defaults to MOONSHOT_API_KEY /
                KIMI_API_KEY env vars)
            sources: Set of sources to query ("social", "web", or both)
            model: Model to use (overrides default kimi-k2.6)
        """
        self.api_key = get_kimi_api_key(api_key)
        self.sources = sources or {"social", "web"}
        if model:
            self.model_name = model
        self.model_name = resolve_kimi_model(self.model_name)
        self._client: Any = None

    def _get_client(self) -> OpenAI:
        """Lazy-load the Moonshot (OpenAI-compatible) client."""
        if self._client is None:
            self._client = make_kimi_client(self.api_key)
        return self._client

    def _chat(self, label: str, **kwargs: Any) -> Any:
        """One retried chat-completions call (no sampling params — see module doc)."""
        client = self._get_client()
        return with_network_retry(
            client.chat.completions.create,
            label=label,
            provider=self.provider_name,
            model=self.model_name,
            timeout=max(provider_http_timeout(), _KIMI_RESEARCH_MIN_TIMEOUT),
            **kwargs,
        )

    def _run_fiber(self, name: str, arguments: str, label: str) -> str:
        """Execute one Formula tool call; return the (encrypted) output.

        ``POST /formulas/moonshot%2Fweb-search/fibers`` runs the search
        server-side; the returned ``encrypted_output`` goes back to the model
        verbatim as the tool message (the server decrypts it into context).
        """
        import httpx

        def _call() -> str:
            resp = httpx.post(
                f"{kimi_base_url()}/formulas/{_WEB_SEARCH_FORMULA}/fibers",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"name": name, "arguments": arguments},
                timeout=provider_http_timeout(),
            )
            resp.raise_for_status()
            data = resp.json()
            context = data.get("context") or {}
            output = context.get("encrypted_output") or context.get("output") or ""
            return str(output)

        return with_network_retry(
            _call, label=f"{label}:fiber", provider=self.provider_name
        )

    def _run_query(
        self, system_prompt: str, user_prompt: str, label: str
    ) -> tuple[str, int]:
        """Run one web-search query via the official Formula tool loop.

        Returns the final answer text plus the number of searches performed.
        The assistant tool-call message is rebuilt as plain dicts (the SDK's
        model_dump warns on server extras) with ``reasoning_content`` riding
        along, as the docs require for multi-turn tool use.
        """
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"{user_prompt}{_SOURCES_INSTRUCTION}"},
        ]
        searches = 0
        text = ""
        for _ in range(_MAX_TOOL_ROUNDS):
            response = self._chat(label, messages=messages, tools=[_WEB_SEARCH_TOOL])
            choice = response.choices[0]
            message = choice.message
            if choice.finish_reason == "tool_calls" and message.tool_calls:
                assistant: dict[str, Any] = {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
                reasoning = getattr(message, "reasoning_content", None)
                if reasoning:
                    assistant["reasoning_content"] = reasoning
                messages.append(assistant)
                for call in message.tool_calls:
                    searches += 1
                    try:
                        output = self._run_fiber(
                            call.function.name, call.function.arguments, label
                        )
                    except Exception as e:
                        # Degrade per call (e.g. no Formula API on the coding
                        # endpoint): the model is told and answers without it.
                        logger.warning("kimi web_search fiber failed: %s", e)
                        output = f"web search unavailable: {e}"
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": output,
                        }
                    )
                continue
            text = message.content or ""
            break
        if not text.strip() and searches:
            # Recovers two observed cases: k3 stopping reasoning-only (empty
            # content) after a tool round, and the round cap expiring while
            # the model still wants to search. No tools on this call — the
            # model must answer from the results it already has. (An empty
            # assistant message must NOT be appended first: the API 400s on
            # "role 'assistant' must not be empty".)
            messages.append({"role": "user", "content": "Write the full answer now."})
            response = self._chat(label, messages=messages)
            text = response.choices[0].message.content or ""
        return text, searches

    def _extract_sources(
        self,
        text: str,
        source_type: SourceType,
        seen: set[str] | None = None,
    ) -> list[Source]:
        """Recover sources from inline citations (Kimi has no citation array).

        Markdown ``[title](url)`` links keep their titles; bare URLs get none.
        Pass a shared ``seen`` set across the social + web queries to dedup.
        """
        if seen is None:
            seen = set()
        pairs: list[tuple[str | None, str]] = []
        captured: set[str] = set()
        for m in _MD_LINK_RE.finditer(text):
            url = m.group(2).rstrip(".,;:!?\"'`*_")
            pairs.append((m.group(1).strip(), url))
            captured.add(url)
        for url in extract_urls_balanced(text):
            if url not in captured:
                pairs.append((None, url))
                captured.add(url)

        sources: list[Source] = []
        for title, url in pairs:
            if url in seen:
                continue
            seen.add(url)
            sources.append(Source(url=url, title=title, source_type=source_type))
        return sources

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch insights based on configured sources."""
        self._log_start()

        if not self.api_key:
            return self._create_error_result("MOONSHOT_API_KEY / KIMI_API_KEY not set")

        try:
            sources: list[Source] = []
            seen_urls: set[str] = set()
            meta: dict[str, Any] = {}
            sections: list[str] = []

            if "social" in self.sources:
                self._log_query("social")
                social_text, social_searches = self._run_query(
                    system_prompt=get_base_system_prompt(days),
                    user_prompt=get_user_prompt(topic, days),
                    label="kimi.chat.completions:social",
                )
                sources.extend(
                    self._extract_sources(social_text, SourceType.SOCIAL, seen_urls)
                )
                meta["social_searches"] = social_searches
                sections.append(f"# Social Media Analysis\n\n{social_text}")

            if "web" in self.sources:
                self._log_query("web")
                web_text, web_searches = self._run_query(
                    system_prompt=get_web_system_prompt(days),
                    user_prompt=get_web_user_prompt(topic, days),
                    label="kimi.chat.completions:web",
                )
                sources.extend(
                    self._extract_sources(web_text, SourceType.WEB, seen_urls)
                )
                meta["web_searches"] = web_searches
                sections.append(f"# Web Research Analysis\n\n{web_text}")

            combined_text = "\n\n---\n\n".join(sections)
            self._log_done(len(sources), len(combined_text))

            return ProviderResult(
                provider=self.provider_name,
                model=self.model_name,
                raw_text=combined_text,
                sources=sources,
                meta=meta,
            )

        except Exception as e:
            return self._create_error_result(f"Kimi API error: {e}")

    def generate_keywords(self, topic: str, days: int, context: str = "") -> list[str]:
        """Generate keywords using Kimi chat completions (no web search)."""
        if not self.api_key:
            return []
        try:
            from researchkit.keyword_synthesizer import parse_keyword_json
            from researchkit.prompts import (
                get_keyword_generation_system_prompt,
                get_keyword_generation_user_prompt,
            )

            response = self._chat(
                "kimi.chat.completions:keywords",
                messages=[
                    {
                        "role": "system",
                        "content": get_keyword_generation_system_prompt(),
                    },
                    {
                        "role": "user",
                        "content": get_keyword_generation_user_prompt(
                            topic, days, context
                        ),
                    },
                ],
                response_format={"type": "json_object"},
            )
            return parse_keyword_json(response.choices[0].message.content or "")
        except Exception as e:
            logger.warning(f"Kimi keyword generation failed: {e}")
            return []

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """Summarize this provider's result using Kimi (no web search)."""
        if not self.api_key:
            return raw_text[:500] + "..." if len(raw_text) > 500 else raw_text

        system_prompt = """You are a precise summarizer. Your task is to distill social insight reports into their essential points.

Rules:
- Extract 5-8 key bullet points
- Preserve specific examples, quotes, or data points
- Keep platform/source attributions
- Be concise but preserve critical details"""

        user_prompt = f"""Summarize this social insight report into 5-8 key bullet points:

**Topic:** {topic}

---
{raw_text}
---

Format as a markdown bullet list. Start each bullet with a bold label when appropriate (e.g., **Trend:**, **Sentiment:**, **Notable:**)."""

        try:
            # No max_tokens: Kimi's thinking models spend reasoning from the
            # same budget and a small cap truncates the summary mid-sentence.
            response = self._chat(
                "kimi.chat.completions:summarize",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            return f"*Summarization failed: {e}*\n\n{raw_text[:500]}..."
