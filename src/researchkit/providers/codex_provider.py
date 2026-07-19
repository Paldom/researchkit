"""OpenAI provider variant that runs via the Codex CLI in non-interactive mode.

Instead of calling the OpenAI Responses API directly (see ``OpenAIProvider``),
this provider shells out to ``codex exec`` with web search enabled. The main
draw is auth/billing: when Codex is "Logged in using ChatGPT", queries run
against the ChatGPT subscription rather than per-token API billing.

Selecting it: set the ``openai`` model in ``models.yaml`` to ``codex`` (Codex
default model) or ``codex:<model>`` (e.g. ``codex:gpt-5.5``). The aggregator
routes the OpenAI step to this provider whenever :func:`is_codex_model` matches.
It still reports ``provider_name = "openai"`` so it slots into the existing
"openai" provider slot; the run is differentiated by its ``model`` field, which
is reported as ``codex:<model>`` / ``codex:default``.

Citations. The ``codex exec --json`` JSONL stream emits ``web_search`` items
containing only the *search queries* plus a handful of explicitly-opened page
URLs (``action.type == "other"``); it does NOT return the structured
``url_citation`` annotations (url + title + snippet) the Responses API exposes.
We recover source URLs from those opened pages plus URLs cited inline in the
final message, then backfill page titles with a best-effort ``<title>`` scrape
so sources carry titles like the Responses API path.
"""

from __future__ import annotations

import html
import json
import logging
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any

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
from researchkit.safe_io import (
    extract_urls_balanced,
    run_subprocess,
    safe_fetch_text,
    scrubbed_env,
)

logger = logging.getLogger(__name__)

# First <title>...</title> of an HTML page, for source title backfill.
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# Codex web search can run many tool calls + reasoning; give it ample headroom
# over the default provider timeout.
_CODEX_MIN_TIMEOUT = 300.0

_TITLE_FETCH_TIMEOUT = 8.0
_TITLE_FETCH_WORKERS = 8

# Appended to research prompts so the answer carries a parseable citation
# list — the headless CLIs return no citation array, and without the nudge
# they often cite few or zero URLs (the agy/kimi providers ship the same
# instruction; citation yield is the product).
_SOURCES_INSTRUCTION = (
    "\n\nAt the very end, add a '## Sources' section listing EVERY web source "
    "you used, one per line as a markdown link: - [title](url)."
)
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def is_codex_model(model: str | None) -> bool:
    """Return ``True`` when ``model`` selects the Codex-CLI path.

    Matches ``codex`` and ``codex:<model>`` (case-insensitive).
    """
    if not model:
        return False
    return model.lower().split(":", 1)[0].strip() == "codex"


def codex_underlying_model(model: str | None) -> str | None:
    """Extract the underlying model from a ``codex`` spec.

    ``"codex:gpt-5.5"`` -> ``"gpt-5.5"``; ``"codex"`` -> ``None`` (Codex default).
    """
    if not model:
        return None
    parts = model.split(":", 1)
    if len(parts) == 2 and parts[1].strip():
        return parts[1].strip()
    return None


def _clean_url(url: str) -> str:
    """Strip trailing punctuation that regex/markdown commonly leaves attached."""
    return url.rstrip(".,;:)]}>\"'")


class CodexProvider(BaseProvider):
    """OpenAI-via-``codex exec`` provider with web search.

    Mirrors :class:`OpenAIProvider`'s social/web dual-query shape so results are
    directly comparable, but sources come from the Codex JSONL stream + inline
    citations (titles backfilled) rather than structured annotations.
    """

    # Subclass knobs: GrokCliProvider reuses this provider's whole research
    # shape and overrides only command construction + output parsing.
    _label_prefix = "codex.exec"
    _error_prefix = "Codex exec"

    def __init__(
        self,
        api_key: str | None = None,  # accepted for parity; unused (Codex handles auth)
        sources: set[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        codex_bin: str = "codex",
        provider_name: str = "openai",
    ) -> None:
        """Initialize the Codex provider.

        Args:
            api_key: Ignored — Codex resolves its own auth (ChatGPT login or
                ``CODEX_API_KEY``). Present only for constructor parity.
            sources: Set of sources to query ("social", "web", or both).
            model: Underlying model passed to ``codex exec -m`` (None = default).
            reasoning_effort: "low" | "medium" | "high" -> ``model_reasoning_effort``.
            codex_bin: Path/name of the Codex CLI binary.
            provider_name: Slot this provider reports as. Defaults to "openai"
                so it drops into the aggregator's OpenAI slot; the codex marker
                lives in the reported ``model`` field instead.
        """
        self.provider_name = provider_name
        self.sources = sources or {"social", "web"}
        self.model_name = model or ""
        self.reasoning_effort = reasoning_effort
        self.codex_bin = codex_bin

    @property
    def _model_label(self) -> str:
        """Differentiated model string reported on results."""
        return f"codex:{self.model_name}" if self.model_name else "codex:default"

    def _build_cmd(self, web_search: bool) -> list[str]:
        """Assemble the ``codex exec`` command (prompt is fed via stdin)."""
        cmd = [
            self.codex_bin,
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "-s",
            "read-only",
        ]
        if web_search:
            cmd += ["-c", "tools.web_search=true"]
        if self.model_name:
            cmd += ["-m", self.model_name]
        if self.reasoning_effort:
            cmd += ["-c", f"model_reasoning_effort={self.reasoning_effort}"]
        cmd += ["-"]  # read prompt from stdin
        return cmd

    @staticmethod
    def _parse_jsonl(stdout: str) -> tuple[str, list[str]]:
        """Parse the Codex JSONL stream into (final_text, opened_urls).

        ``opened_urls`` are URLs the model explicitly fetched (web_search items
        with ``action.type == "other"``). The final text is the last
        ``agent_message`` item.
        """
        text = ""
        opened: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") != "item.completed":
                continue
            item = evt.get("item") or {}
            itype = item.get("item_type") or item.get("type")
            if itype == "agent_message":
                text = item.get("text") or text
            elif itype == "web_search":
                action = item.get("action") or {}
                if action.get("type") == "other":
                    q = item.get("query") or ""
                    if q.startswith("http"):
                        opened.append(_clean_url(q))
        return text, opened

    @staticmethod
    def _error_detail(proc: subprocess.CompletedProcess[str]) -> str:
        """Best human-readable failure reason for a failed ``codex exec``.

        Codex reports errors (usage limits, auth) as ``{"type": "error"}`` /
        ``turn.failed`` events in the stdout JSONL stream with an EMPTY
        stderr — surfacing only stderr produced blank error messages.
        """
        if proc.stderr and proc.stderr.strip():
            return proc.stderr.strip()[-500:]
        for line in reversed(proc.stdout.splitlines()):
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue
            if isinstance(evt.get("error"), dict) and evt["error"].get("message"):
                return str(evt["error"]["message"])[:500]
            if evt.get("type") == "error" and evt.get("message"):
                return str(evt["message"])[:500]
        return proc.stdout.strip()[-300:] or "(no output)"

    def _exec(
        self, prompt: str, *, web_search: bool, label: str
    ) -> tuple[str, list[str]]:
        """Run one ``codex exec`` invocation; return (final_text, opened_urls)."""
        cmd = self._build_cmd(web_search)
        timeout = max(provider_http_timeout(), _CODEX_MIN_TIMEOUT)

        def _call() -> subprocess.CompletedProcess[str]:
            # Own process group + kill-on-timeout (C2), UTF-8 decode (L26), and a
            # scrubbed env so untrusted web content can't exfiltrate other
            # providers' API keys — Codex auths via ChatGPT login / its own
            # OPENAI_API_KEY, so only that is kept. (Review M7.)
            proc = run_subprocess(
                cmd,
                input=prompt,
                timeout=timeout,
                env=scrubbed_env(keep=frozenset({"OPENAI_API_KEY"})),
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"codex exec failed (exit {proc.returncode}): "
                    f"{self._error_detail(proc)}"
                )
            return proc

        proc = with_network_retry(_call, label=label, provider=self.provider_name)
        return self._parse_jsonl(proc.stdout)

    def _extract_sources(
        self, text: str, opened_urls: list[str], source_type: SourceType
    ) -> list[Source]:
        """Build Source objects from opened-page URLs + inline-cited URLs."""
        urls: list[str] = list(opened_urls)
        # Balanced-paren extraction keeps Wikipedia-style ..._(genus) URLs intact
        # (the old _URL_RE excluded ')' and truncated them). (Review S3.)
        urls += extract_urls_balanced(text)
        seen: set[str] = set()
        sources: list[Source] = []
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(Source(url=url, title=None, source_type=source_type))
        return sources

    @staticmethod
    def _fetch_title(url: str) -> str | None:
        """Best-effort fetch of a page's <title> (None on any failure).

        Goes through the SSRF-guarded fetch helper: the URLs come from untrusted
        web/model output, so a poisoned ``http://169.254.169.254/…`` must not turn
        into a server-side request to an internal endpoint. (Review S2.)
        """
        page, _ = safe_fetch_text(
            url,
            timeout=_TITLE_FETCH_TIMEOUT,
            max_bytes=15_000,
            headers={"User-Agent": _BROWSER_UA},
        )
        if not page:
            return None
        match = _TITLE_RE.search(page)
        if match:
            return html.unescape(match.group(1).strip())[:300] or None
        return None

    def _backfill_titles(self, sources: list[Source]) -> None:
        """Populate missing titles concurrently via a lightweight HTML scrape."""
        targets = [s for s in sources if not s.title]
        if not targets:
            return
        with ThreadPoolExecutor(max_workers=_TITLE_FETCH_WORKERS) as pool:
            titles = list(pool.map(self._fetch_title, [s.url for s in targets]))
        for source, title in zip(targets, titles, strict=True):
            if title:
                source.title = title

    def _run_query(
        self, system_prompt: str, user_prompt: str, label: str
    ) -> tuple[str, list[str]]:
        """Run one web-search query (Codex has no system role, so prompts join)."""
        return self._exec(
            f"{system_prompt}\n\n{user_prompt}{_SOURCES_INSTRUCTION}",
            web_search=True,
            label=label,
        )

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch insights via ``codex exec`` based on configured sources."""
        self._log_start()

        if not shutil.which(self.codex_bin):
            return self._create_error_result(f"{self.codex_bin} CLI not found on PATH")

        try:
            sources: list[Source] = []
            meta: dict[str, Any] = {}
            sections: list[str] = []

            if "social" in self.sources:
                self._log_query("social")
                social_text, social_opened = self._run_query(
                    system_prompt=get_base_system_prompt(days),
                    user_prompt=get_user_prompt(topic, days),
                    label=f"{self._label_prefix}:social",
                )
                sources.extend(
                    self._extract_sources(social_text, social_opened, SourceType.SOCIAL)
                )
                meta["social_opened_urls"] = social_opened
                sections.append(f"# Social Media Analysis\n\n{social_text}")

            if "web" in self.sources:
                self._log_query("web")
                web_text, web_opened = self._run_query(
                    system_prompt=get_web_system_prompt(days),
                    user_prompt=get_web_user_prompt(topic, days),
                    label=f"{self._label_prefix}:web",
                )
                sources.extend(
                    self._extract_sources(web_text, web_opened, SourceType.WEB)
                )
                meta["web_opened_urls"] = web_opened
                sections.append(f"# Web Research Analysis\n\n{web_text}")

            # Codex gives URLs but no titles — backfill them so sources match
            # the Responses API path (url + title).
            self._backfill_titles(sources)

            combined_text = "\n\n---\n\n".join(sections)
            self._log_done(len(sources), len(combined_text))

            return ProviderResult(
                provider=self.provider_name,
                model=self._model_label,
                raw_text=combined_text,
                sources=sources,
                meta=meta,
            )

        except Exception as e:
            return self._create_error_result(f"{self._error_prefix} error: {e}")

    def generate_keywords(self, topic: str, days: int, context: str = "") -> list[str]:
        """Generate keywords via ``codex exec`` (no web search)."""
        if not shutil.which(self.codex_bin):
            return []
        try:
            from researchkit.keyword_synthesizer import parse_keyword_json
            from researchkit.prompts import (
                get_keyword_generation_system_prompt,
                get_keyword_generation_user_prompt,
            )

            system_prompt = get_keyword_generation_system_prompt()
            user_prompt = get_keyword_generation_user_prompt(topic, days, context)
            text, _ = self._exec(
                f"{system_prompt}\n\n{user_prompt}\n\nRespond ONLY with the JSON.",
                web_search=False,
                label=f"{self._label_prefix}:keywords",
            )
            return parse_keyword_json(text)
        except Exception as e:
            logger.warning(f"{self._error_prefix} keyword generation failed: {e}")
            return []

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """Summarize this provider's result via ``codex exec`` (no web search)."""
        if not shutil.which(self.codex_bin):
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
            text, _ = self._exec(
                f"{system_prompt}\n\n{user_prompt}",
                web_search=False,
                label=f"{self._label_prefix}:summarize",
            )
            return text or (raw_text[:500] + "..." if len(raw_text) > 500 else raw_text)
        except Exception as e:
            return f"*Summarization failed: {e}*\n\n{raw_text[:500]}..."
