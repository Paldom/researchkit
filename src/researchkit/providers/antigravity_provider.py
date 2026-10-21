"""Gemini-via-Antigravity-CLI provider (``agy``) with web search.

Antigravity (``agy``) is Google's agentic CLI; under the hood it is Gemini with
Google Search grounding. Like ``CodexProvider`` shells out to ``codex exec``,
this provider shells out to ``agy --print`` (``-p``) for a single non-interactive
query. The draw is billing: ``agy`` runs on the Google/Antigravity sign-in
session rather than a ``GEMINI_API_KEY``.

Selecting it: set the ``gemini`` model in ``models.yaml`` to ``agy`` /
``antigravity`` (agy default model) or ``agy:<model>``. ``agy models`` lists
ids alongside their display names (``<id>\t<display name>``) — pass the ID,
e.g. ``agy:gemini-3.8-flash-high``; the thinking level is baked into it, so a
researchkit ``@<effort>`` suffix does nothing here. (Older agy builds listed
only display names; those pins no longer resolve and ``researchkit doctor``
fails them.) The aggregator routes the Gemini step to this provider
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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import (
    CLI_QUICK_TIMEOUT,
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
    cli_harness_timeout,
    get_base_system_prompt,
    get_user_prompt,
    get_web_system_prompt,
    get_web_user_prompt,
    run_branches,
)
from researchkit.safe_io import run_subprocess, scrubbed_env

logger = logging.getLogger(__name__)

# Markdown link: [title](url) — agy formats grounded sources this way.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
# Bare URL fallback (trailing punctuation trimmed by _clean_url).
_URL_RE = re.compile(r"https?://[^\s)\]<>\"']+")

_GROUNDING_MARKER = "grounding-api-redirect"
_UA = "Mozilla/5.0 (compatible; SocialResearch/1.0)"

# Margin (seconds) between agy's own `--print-timeout` and our subprocess kill,
# so agy reports its own timeout instead of dying to SIGKILL with no message.
# 60s, not 30: our clock starts at spawn while agy's starts after binary
# startup and sign-in, and it still needs to flush and exit inside the gap.
_AGY_PRINT_MARGIN = 60.0

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

    ``"agy:gemini-3.8-flash-high"`` -> ``"gemini-3.8-flash-high"``;
    ``"agy"`` -> ``None`` (agy default model).
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


def _resolve_grounding_url(url: str, provider_name: str, timeout: float = 5.0) -> str:
    """Resolve a Gemini grounding redirect to the real URL (one HEAD).

    Non-grounding URLs pass through unchanged; on any failure the original
    URL is returned. Titles come from the archived page, not a second GET
    here (PLAN 3.3).
    """
    if _GROUNDING_MARKER not in url:
        return url
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
        return str(head.url)
    except Exception as e:
        logger.debug(f"Failed to resolve grounding redirect: {e}")
        return url


class AntigravityProvider(BaseProvider):
    """Gemini-via-``agy`` provider with web search and resolved citations.

    Mirrors :class:`GeminiProvider`'s social/web dual-query shape; sources are
    parsed from the printed response and grounding redirects are resolved to
    real publisher URLs (with titles).
    """

    @property
    def quota_pool(self) -> str:
        """Subscription allowance this harness bills."""
        return "agy"

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

    def _build_cmd(self, prompt: str, *, timeout: float) -> list[str]:
        """Assemble an ``agy --print`` command for a single prompt.

        ``--print-timeout`` is derived from the subprocess budget (Go duration
        string) and always lands strictly *below* it, so agy times out on its
        own clock and prints a real error instead of dying to SIGKILL with an
        empty stdout. No floor: a floor above the budget would invert exactly
        the invariant this exists to hold.
        """
        # Headless print mode needs auto-approval; --sandbox ("terminal
        # restrictions") bounds it so a planted instruction in a fetched page
        # can't drive arbitrary shell commands. Web tools are unaffected.
        cmd = [self.agy_bin, "--sandbox", "--dangerously-skip-permissions"]
        if self.model_name:
            cmd += ["--model", self.model_name]
        print_timeout = f"{max(int(timeout - _AGY_PRINT_MARGIN), 1)}s"
        cmd += ["--print-timeout", print_timeout, "-p", prompt]
        return cmd

    def _run_cli(self, prompt: str, label: str, *, quick: bool = False) -> str:
        """Run ``agy`` non-interactively and return its printed response text."""
        # Side calls (keywords, summaries) get the short budget; research keeps
        # the full one.
        timeout = (
            cli_harness_timeout(CLI_QUICK_TIMEOUT) if quick else cli_harness_timeout()
        )
        cmd = self._build_cmd(prompt, timeout=timeout)

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
        with ThreadPoolExecutor(max_workers=8) as pool:
            resolved = list(
                pool.map(
                    lambda u: _resolve_grounding_url(u, self.provider_name),
                    [url for _, url in pairs],
                )
            )
        for (link_title, _), real_url in zip(pairs, resolved, strict=True):
            if not real_url or real_url in seen:
                continue
            seen.add(real_url)
            sources.append(
                Source(url=real_url, title=link_title, source_type=source_type)
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

            # Social and web run concurrently, each kept on its own.
            prompts: dict[str, str] = {}
            if "social" in self.sources:
                self._log_query("social")
                prompts["social"] = (
                    f"{get_base_system_prompt(days)}\n\n"
                    f"{get_user_prompt(topic, days)}{_SOURCES_INSTRUCTION}"
                )
            if "web" in self.sources:
                self._log_query("web")
                prompts["web"] = (
                    f"{get_web_system_prompt(days)}\n\n"
                    f"{get_web_user_prompt(topic, days)}{_SOURCES_INSTRUCTION}"
                )

            def _branch(prompt: str, name: str) -> Callable[[], str]:
                return lambda: self._run_cli(prompt, f"agy.print:{name}")

            outcomes = run_branches({n: _branch(p, n) for n, p in prompts.items()})
            errors = {
                name: str(out)
                for name, out in outcomes.items()
                if isinstance(out, BaseException)
            }
            if errors and len(errors) == len(outcomes):
                raise RuntimeError("; ".join(f"{n}: {e}" for n, e in errors.items()))
            for name, heading, kind in (
                ("social", "Social Media Analysis", SourceType.SOCIAL),
                ("web", "Web Research Analysis", SourceType.WEB),
            ):
                out = outcomes.get(name)
                if out is None or isinstance(out, BaseException):
                    continue
                branch_sources = self._extract_sources(out, kind)
                sources.extend(branch_sources)
                meta[f"{name}_source_count"] = len(branch_sources)
                sections.append(f"# {heading}\n\n{out}")
            if errors:
                meta["branch_errors"] = errors

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
            return parse_keyword_json(
                self._run_cli(prompt, "agy.print:keywords", quick=True)
            )
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
            return self._run_cli(prompt, "agy.print:summarize", quick=True) or (
                raw_text[:500] + "..." if len(raw_text) > 500 else raw_text
            )
        except Exception as e:
            return f"*Summarization failed: {e}*\n\n{raw_text[:500]}..."
