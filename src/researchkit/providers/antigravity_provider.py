"""Gemini-via-Antigravity-CLI provider (``agy``) with web search.

Antigravity (``agy``) is Google's agentic CLI; under the hood it is Gemini with
Google Search grounding. Like ``CodexProvider`` shells out to ``codex exec``,
this provider shells out to ``agy --print`` (``-p``) for a single non-interactive
query. The draw is billing: ``agy`` runs on the Google/Antigravity sign-in
session rather than a ``GEMINI_API_KEY``.

Selecting it: set the ``gemini`` model in ``models.yaml`` to ``agy`` /
``antigravity`` (agy default model) or ``agy:<model>`` (e.g.
``agy:gemini-3.5-flash``). The aggregator routes the Gemini step to this provider
whenever :func:`is_antigravity_model` matches. It still reports
``provider_name = "gemini"`` so it slots into the existing "gemini" provider
slot; the run is differentiated by its ``model`` field, reported as
``agy:<model>`` / ``agy:default``.

Citations. ``agy`` has no ``--json`` mode, so citations are recovered from the
response *text*. When asked for sources, Gemini emits them as markdown links
whose URLs are opaque grounding redirects
(``https://vertexaisearch.cloud.google.com/grounding-api-redirect/...``). This
provider resolves each redirect to the real publisher URL (and scrapes the page
``<title>``), mirroring ``GeminiProvider._resolve_redirect_url`` — so the emitted
sources are real, clickable links, comparable to the API provider.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Any

import requests

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
from researchkit.safe_io import run_subprocess, scrubbed_env

logger = logging.getLogger(__name__)

# Markdown link: [title](url) — agy formats grounded sources this way.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
# Bare URL fallback (trailing punctuation trimmed by _clean_url).
_URL_RE = re.compile(r"https?://[^\s)\]<>\"']+")

_GROUNDING_MARKER = "grounding-api-redirect"
_UA = "Mozilla/5.0 (compatible; SocialResearch/1.0)"

# agy web search + reasoning can run a while; floor the subprocess timeout.
_AGY_MIN_TIMEOUT = 600.0
# Duration string passed to `agy --print-timeout`.
_AGY_PRINT_TIMEOUT = "9m"

# Model prefixes (before the first ':') that select this CLI path.
_AGY_PREFIXES = frozenset({"agy", "antigravity"})

# Appended to research prompts so Gemini emits a parseable citation list (agy
# only surfaces grounding URLs in text when explicitly asked for sources).
_SOURCES_INSTRUCTION = (
    "\n\nAt the very end, add a '## Sources' section listing EVERY web source "
    "you used, one per line as a markdown link: - [title](url)."
)


def is_antigravity_model(model: str | None) -> bool:
    """Return ``True`` when ``model`` selects the Antigravity-CLI path.

    Matches ``agy``/``antigravity`` and ``agy:<model>`` (case-insensitive).
    """
    if not model:
        return False
    return model.lower().split(":", 1)[0].strip() in _AGY_PREFIXES


def antigravity_underlying_model(model: str | None) -> str | None:
    """Extract the underlying model from an ``agy`` spec.

    ``"agy:gemini-3.5-flash"`` -> ``"gemini-3.5-flash"``; ``"agy"`` -> ``None``
    (agy default model).
    """
    if not model:
        return None
    parts = model.split(":", 1)
    if len(parts) == 2 and parts[1].strip():
        return parts[1].strip()
    return None


def _clean_url(url: str) -> str:
    """Strip trailing punctuation regex/markdown commonly leaves attached."""
    return url.rstrip(".,;:)]}>\"'")


def _resolve_grounding_url(
    url: str, provider_name: str, timeout: float = 5.0
) -> tuple[str, str | None]:
    """Resolve a Gemini grounding redirect to (real_url, page_title).

    Non-grounding URLs pass through unchanged. On any failure the original URL
    is returned with no title.
    """
    if _GROUNDING_MARKER not in url:
        return url, None
    try:
        head = with_network_retry(
            requests.head,
            url,
            label="antigravity.resolve.head",
            provider=provider_name,
            allow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": _UA},
        )
        final_url = head.url
        title: str | None = None
        try:
            got = with_network_retry(
                requests.get,
                final_url,
                label="antigravity.resolve.get",
                provider=provider_name,
                timeout=timeout,
                headers={"User-Agent": _UA},
            )
            if got.status_code == 200:
                content = got.text[:10000]
                low = content.lower()
                if "<title>" in low:
                    start = low.find("<title>") + 7
                    end = low.find("</title>")
                    if end > start:
                        title = " ".join(content[start:end].split())[:200]
        except Exception:
            pass  # title is best-effort
        return final_url, title
    except Exception as e:
        logger.debug(f"Failed to resolve grounding redirect: {e}")
        return url, None


class AntigravityProvider(BaseProvider):
    """Gemini-via-``agy`` provider with web search and resolved citations.

    Mirrors :class:`GeminiProvider`'s social/web dual-query shape; sources are
    parsed from the printed response and grounding redirects are resolved to
    real publisher URLs (with titles).
    """

    def __init__(
        self,
        api_key: str | None = None,  # parity only; agy uses its own sign-in
        sources: set[str] | None = None,
        model: str | None = None,
        agy_bin: str = "agy",
        provider_name: str = "gemini",
    ) -> None:
        """Initialize the Antigravity provider.

        Args:
            api_key: Ignored — ``agy`` authenticates via Google sign-in.
            sources: Set of sources to query ("social", "web", or both).
            model: Underlying model passed to ``agy --model`` (None = agy default).
            agy_bin: Path/name of the Antigravity CLI binary.
            provider_name: Slot this provider reports as. Defaults to "gemini"
                so it drops into the aggregator's Gemini slot; the agy marker
                lives in the reported ``model`` field instead.
        """
        self.provider_name = provider_name
        self.sources = sources or {"social", "web"}
        self.model_name = model or ""
        self.agy_bin = agy_bin

    @property
    def _model_label(self) -> str:
        """Differentiated model string reported on results."""
        return f"agy:{self.model_name}" if self.model_name else "agy:default"

    def _build_cmd(
        self, prompt: str, *, print_timeout: str = _AGY_PRINT_TIMEOUT
    ) -> list[str]:
        """Assemble an ``agy --print`` command for a single prompt."""
        cmd = [self.agy_bin, "--dangerously-skip-permissions"]
        if self.model_name:
            cmd += ["--model", self.model_name]
        cmd += ["--print-timeout", print_timeout, "-p", prompt]
        return cmd

    def _run_cli(self, prompt: str, label: str) -> str:
        """Run ``agy`` non-interactively and return its printed response text."""
        cmd = self._build_cmd(prompt)
        timeout = max(provider_http_timeout(), _AGY_MIN_TIMEOUT)

        def _call() -> subprocess.CompletedProcess[str]:
            # agy runs --dangerously-skip-permissions with no read-only flag
            # available, so scrub the env to strip other providers' API keys —
            # agy auths via Google/Antigravity sign-in and keeps only the Google
            # keys. Own process group + kill-on-timeout (C2), UTF-8 (L26). (M7.)
            proc = run_subprocess(
                cmd,
                timeout=timeout,
                env=scrubbed_env(keep=frozenset({"GEMINI_API_KEY", "GOOGLE_API_KEY"})),
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"agy failed (exit {proc.returncode}): {proc.stderr[-500:]}"
                )
            return proc

        proc = with_network_retry(_call, label=label, provider=self.provider_name)
        return proc.stdout.strip()

    def _extract_sources(self, text: str, source_type: SourceType) -> list[Source]:
        """Parse markdown/bare URLs from the response and resolve redirects."""
        pairs: list[tuple[str | None, str]] = []
        captured: set[str] = set()

        for m in _MD_LINK_RE.finditer(text):
            url = _clean_url(m.group(2))
            pairs.append((m.group(1).strip(), url))
            captured.add(url)

        for raw in _URL_RE.findall(text):
            url = _clean_url(raw)
            if url not in captured:
                pairs.append((None, url))
                captured.add(url)

        sources: list[Source] = []
        seen: set[str] = set()
        for link_title, url in pairs:
            real_url, page_title = _resolve_grounding_url(url, self.provider_name)
            if not real_url or real_url in seen:
                continue
            seen.add(real_url)
            sources.append(
                Source(
                    url=real_url,
                    title=page_title or link_title,
                    source_type=source_type,
                )
            )
        return sources

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch insights via ``agy`` based on configured sources."""
        self._log_start()

        if not shutil.which(self.agy_bin):
            return self._create_error_result(
                f"agy CLI not found on PATH ({self.agy_bin!r})"
            )

        try:
            sources: list[Source] = []
            meta: dict[str, Any] = {}
            sections: list[str] = []

            if "social" in self.sources:
                self._log_query("social")
                prompt = (
                    f"{get_base_system_prompt(days)}\n\n"
                    f"{get_user_prompt(topic, days)}{_SOURCES_INSTRUCTION}"
                )
                social_text = self._run_cli(prompt, "agy.print:social")
                social_sources = self._extract_sources(social_text, SourceType.SOCIAL)
                sources.extend(social_sources)
                meta["social_source_count"] = len(social_sources)
                sections.append(f"# Social Media Analysis\n\n{social_text}")

            if "web" in self.sources:
                self._log_query("web")
                prompt = (
                    f"{get_web_system_prompt(days)}\n\n"
                    f"{get_web_user_prompt(topic, days)}{_SOURCES_INSTRUCTION}"
                )
                web_text = self._run_cli(prompt, "agy.print:web")
                web_sources = self._extract_sources(web_text, SourceType.WEB)
                sources.extend(web_sources)
                meta["web_source_count"] = len(web_sources)
                sections.append(f"# Web Research Analysis\n\n{web_text}")

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
            return self._create_error_result(f"Antigravity (agy) error: {e}")

    def generate_keywords(self, topic: str, days: int, context: str = "") -> list[str]:
        """Generate keywords via ``agy`` (no web search)."""
        if not shutil.which(self.agy_bin):
            return []
        try:
            from researchkit.keyword_synthesizer import parse_keyword_json
            from researchkit.prompts import (
                get_keyword_generation_system_prompt,
                get_keyword_generation_user_prompt,
            )

            prompt = (
                f"{get_keyword_generation_system_prompt()}\n\n"
                f"{get_keyword_generation_user_prompt(topic, days, context)}\n\n"
                "Do not search the web. Respond ONLY with the JSON."
            )
            return parse_keyword_json(self._run_cli(prompt, "agy.print:keywords"))
        except Exception as e:
            logger.warning(f"Antigravity keyword generation failed: {e}")
            return []

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """Summarize this provider's result via ``agy`` (no web search)."""
        if not shutil.which(self.agy_bin):
            return raw_text[:500] + "..." if len(raw_text) > 500 else raw_text
        prompt = (
            "You are a precise summarizer. Distill the social insight report below "
            "into 5-8 markdown bullet points, preserving specific examples, quotes, "
            "data points and source attributions. Start bullets with a bold label "
            "when appropriate (e.g. **Trend:**). Do not search the web.\n\n"
            f"**Topic:** {topic}\n\n---\n{raw_text}\n---"
        )
        try:
            return self._run_cli(prompt, "agy.print:summarize") or (
                raw_text[:500] + "..." if len(raw_text) > 500 else raw_text
            )
        except Exception as e:
            return f"*Summarization failed: {e}*\n\n{raw_text[:500]}..."
