"""Claude-based final report summaries."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import TYPE_CHECKING, Any

from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from researchkit.prompts import (
    get_digest_system_prompt,
    get_digest_user_prompt_footer,
    get_digest_user_prompt_header,
    get_professional_overview_system_prompt,
    get_professional_overview_user_prompt_footer,
    get_professional_overview_user_prompt_header,
    get_super_summary_system_prompt,
    get_super_summary_user_prompt_footer,
    get_super_summary_user_prompt_header,
)
from researchkit.providers.claude_provider import (
    deep_research_underlying_model,
    is_deep_research_spec,
)
from researchkit.safe_io import run_subprocess

if TYPE_CHECKING:
    from collections.abc import Sequence

    from researchkit.project import UserFileSource, UserUrlSource
    from researchkit.providers.base import ProviderResult
    from researchkit.site_research import SiteResearchBundle
    from researchkit.system_config import EffectiveModels

logger = logging.getLogger(__name__)

FINAL_SUMMARY_MAX_BUDGET = 1.0
FINAL_SUMMARY_TIMEOUT_SECONDS = 300

_CLI_NETWORK_SIGNATURES = (
    "connection reset",
    "connection refused",
    "connection error",
    "timed out",
    "timeout",
    "network is unreachable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "rate limit",
    "too many requests",
    "remote disconnected",
    "name or service not known",
    "temporary failure",
)


def _is_cli_transient(exc: BaseException) -> bool:
    """True if a Claude CLI subprocess error looks like a transient network issue."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        return any(sig in msg for sig in _CLI_NETWORK_SIGNATURES)
    return False


def _retry_log(state: Any) -> None:
    exc = state.outcome.exception() if state.outcome else None
    if exc is None:
        return
    next_sleep = getattr(state.next_action, "sleep", None)
    wait_s = float(next_sleep) if next_sleep is not None else 0.0
    logger.warning(
        "Retrying final summary claude CLI (attempt %d/2, waiting %.1fs) — %s: %s",
        state.attempt_number,
        wait_s,
        type(exc).__name__,
        str(exc)[:200],
        extra={
            "stage": "network_retry",
            "label": "final_summary.claude.cli",
            "attempt": state.attempt_number,
            "max_attempts": 2,
            "wait_s": round(wait_s, 2),
            "error_type": type(exc).__name__,
            "provider": "claude",
        },
    )


class ClaudeFinalSummaryGenerator:
    """Shared Claude Code runner for final report summaries."""

    stage_name = "final_summary"
    display_name = "final summary"

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        max_budget: float = FINAL_SUMMARY_MAX_BUDGET,
    ) -> None:
        self.model = model
        self.max_budget = max_budget

    @classmethod
    def from_effective_models(
        cls,
        effective_models: EffectiveModels | None,
    ) -> ClaudeFinalSummaryGenerator:
        """Create a generator using config from EffectiveModels."""
        if effective_models is None:
            return cls()
        model: str | None = effective_models.claude
        # The claude slot may carry the provider-routing `deep[:<model>]`
        # spec; summaries run a plain CLI call, so unwrap to the underlying
        # model (bare `deep` falls back to the class default).
        if is_deep_research_spec(model):
            model = deep_research_underlying_model(model)
        if model:
            return cls(model=model, max_budget=effective_models.claude_max_budget)
        return cls(max_budget=effective_models.claude_max_budget)

    def get_system_prompt(self) -> str:
        """Return the system prompt for this summary type."""
        raise NotImplementedError

    def get_user_prompt_header(self, topic: str, days: int) -> str:
        """Return the leading user prompt for this summary type."""
        raise NotImplementedError

    def get_user_prompt_footer(self) -> str:
        """Return the closing instruction for this summary type."""
        raise NotImplementedError

    def _build_env(self) -> dict[str, str]:
        """Build environment for subprocess with privacy settings."""
        env = {**os.environ}
        env.pop("ANTHROPIC_API_KEY", None)
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env

    def _build_sources_section(self, provider_results: list[ProviderResult]) -> str:
        """Build a markdown section listing all referenced sources/links."""
        lines: list[str] = []
        for result in provider_results:
            if not result.is_success or not result.sources:
                continue
            lines.append(f"\n### {result.provider.title()} Sources")
            for src in result.sources:
                title = src.title or src.url
                lines.append(f"- [{title}]({src.url})")
        return "\n".join(lines) if lines else ""

    def _build_site_research_section(
        self,
        site_research: SiteResearchBundle | None,
    ) -> str:
        """Build a markdown section from site research results."""
        if not site_research or not site_research.items_by_site:
            return ""

        lines: list[str] = []
        for site, items in site_research.items_by_site.items():
            if not items:
                continue
            lines.append(f"\n### {site.title()}")
            for item in items:
                title = item.title or item.url
                line = f"- [{title}]({item.url})"
                if item.author_or_channel:
                    line += f" - {item.author_or_channel}"
                lines.append(line)
                if item.summary and item.summary.tldr:
                    for bullet in item.summary.tldr:
                        lines.append(f"  - {bullet}")
        return "\n".join(lines) if lines else ""

    def _build_user_url_sources_section(
        self,
        url_sources: Sequence[UserUrlSource],
    ) -> str:
        """Build a markdown section for user-curated URLs (citable)."""
        if not url_sources:
            return ""
        lines: list[str] = []
        for s in url_sources:
            title = s.title or s.url
            line = f"- [{title}]({s.url})"
            if s.note:
                line += f" — {s.note}"
            lines.append(line)
        return "\n".join(lines)

    def _build_user_file_context_section(
        self,
        file_sources: Sequence[UserFileSource],
        file_contents: dict[str, str],
    ) -> str:
        """Build a markdown section with user-supplied document content (context only)."""
        if not file_sources:
            return ""
        lines: list[str] = []
        for f in file_sources:
            text = file_contents.get(f.filename)
            if not text:
                continue
            header = f.title or f.filename
            lines.append(f"\n#### {header}")
            if f.note:
                lines.append(f"_Note: {f.note}_")
            lines.append("")
            lines.append(text.strip())
            lines.append("")
        return "\n".join(lines).strip()

    def _build_user_prompt(
        self,
        meta_summary: str,
        individual_summaries: dict[str, str],
        topic: str,
        days: int,
        provider_results: list[ProviderResult] | None = None,
        site_research: SiteResearchBundle | None = None,
        user_url_sources: Sequence[UserUrlSource] | None = None,
        user_file_sources: Sequence[UserFileSource] | None = None,
        user_file_contents: dict[str, str] | None = None,
    ) -> str:
        """Build the full user prompt with shared research context."""
        summaries_text = ""
        for provider, summary in individual_summaries.items():
            summaries_text += f"\n### {provider.title()}\n{summary}\n"

        raw_text = ""
        if provider_results:
            for result in provider_results:
                if result.is_success and result.raw_text:
                    raw_text += f"\n### {result.provider.title()} ({result.model})\n"
                    raw_text += f"{result.raw_text}\n"

        sources_text = self._build_sources_section(provider_results or [])
        site_research_text = self._build_site_research_section(site_research)
        user_url_text = self._build_user_url_sources_section(user_url_sources or [])
        user_file_text = self._build_user_file_context_section(
            user_file_sources or [], user_file_contents or {}
        )

        user_prompt = (
            f"{self.get_user_prompt_header(topic, days)}\n\n"
            f"---\n\n"
            f"## Consolidated Analysis\n\n{meta_summary}\n\n"
            f"## Individual Provider Summaries\n{summaries_text}\n"
        )

        if raw_text:
            user_prompt += f"## Full Provider Reports\n{raw_text}\n"

        if sources_text:
            user_prompt += f"## Referenced Sources & Links\n{sources_text}\n"

        if site_research_text:
            user_prompt += (
                f"## Site Research (Medium, YouTube, Exa)\n{site_research_text}\n"
            )

        if user_url_text:
            user_prompt += (
                "## User-Curated Sources (cite in the article)\n"
                "These URLs were provided by the user. Treat them as authoritative "
                "additional sources alongside the research findings, weave them "
                "into the article where relevant, and cite them as proper links.\n\n"
                f"{user_url_text}\n\n"
            )

        if user_file_text:
            user_prompt += (
                "## User-Provided Document Context\n"
                "The user supplied the following documents as background context. "
                "**Do not cite the documents themselves** (do not reference the "
                "filenames, local paths, 'the user's notes', or anything that "
                "would not make sense in a published article). However, **if these "
                "documents reference URLs, books, papers, authors, or other named "
                "works that would be appropriate citations in a published article, "
                "you may and should cite those items** when their content informs "
                "the article.\n\n"
                f"{user_file_text}\n\n"
            )

        user_prompt += f"---\n\n{self.get_user_prompt_footer()}"
        return user_prompt

    def _run_claude(self, user_prompt: str) -> str | None:
        """Run Claude Code CLI and return the summary markdown."""
        cmd = [
            "claude",
            "-p",
            "--model",
            self.model,
            "--system-prompt",
            self.get_system_prompt(),
            "--no-session-persistence",
            "--strict-mcp-config",  # built-in tools only (review M8)
            "--disallowed-tools",
            "WebSearch,WebFetch,Write,Edit,Bash,Read,Glob,Grep,NotebookEdit,Agent",
            "--permission-mode",
            "bypassPermissions",
            "--output-format",
            "json",
            "--max-budget-usd",
            str(self.max_budget),
        ]

        logger.info(
            f"Generating {self.display_name}: model={self.model}, "
            f"input_size={len(user_prompt)} chars",
            extra={"stage": f"{self.stage_name}_start"},
        )

        try:
            for attempt in Retrying(
                stop=stop_after_attempt(2),
                wait=wait_exponential(multiplier=1, min=2, max=10) + wait_random(0, 2),
                retry=retry_if_exception(_is_cli_transient),
                before_sleep=_retry_log,
                reraise=True,
            ):
                with attempt:
                    # Own process group + kill-on-timeout (C2), UTF-8 decode (L26).
                    result = run_subprocess(
                        cmd,
                        input=user_prompt,
                        timeout=FINAL_SUMMARY_TIMEOUT_SECONDS,
                        env=self._build_env(),
                    )
                    if result.returncode != 0:
                        detail = (result.stderr or result.stdout or "").strip()
                        raise RuntimeError(
                            f"Claude CLI exited with code {result.returncode}: {detail}"
                        )
        except FileNotFoundError:
            logger.warning(
                f"{self.__class__.__name__}: Claude Code CLI not found",
                extra={"stage": f"{self.stage_name}_error"},
            )
            return None
        except subprocess.TimeoutExpired:
            logger.warning(
                f"{self.__class__.__name__}: timed out after {FINAL_SUMMARY_TIMEOUT_SECONDS}s",
                extra={"stage": f"{self.stage_name}_error"},
            )
            return None
        except RuntimeError as e:
            logger.warning(
                f"{self.__class__.__name__}: {e}",
                extra={"stage": f"{self.stage_name}_error"},
            )
            return None

        try:
            output: dict[str, Any] = json.loads(result.stdout)
            if output.get("is_error"):
                logger.warning(
                    f"{self.__class__.__name__}: CLI error: {output.get('result', 'unknown')}",
                    extra={"stage": f"{self.stage_name}_error"},
                )
                return None
            result_text = output.get("result", result.stdout.strip())
            text = result_text if isinstance(result_text, str) else str(result_text)
        except json.JSONDecodeError:
            text = result.stdout.strip()

        if not text:
            logger.warning(
                f"{self.__class__.__name__}: empty response",
                extra={"stage": f"{self.stage_name}_error"},
            )
            return None

        logger.info(
            f"{self.display_name.title()} generated: {len(text)} chars",
            extra={"stage": f"{self.stage_name}_done"},
        )
        return text

    def generate(
        self,
        meta_summary: str,
        individual_summaries: dict[str, str],
        topic: str,
        days: int,
        provider_results: list[ProviderResult] | None = None,
        site_research: SiteResearchBundle | None = None,
        user_url_sources: Sequence[UserUrlSource] | None = None,
        user_file_sources: Sequence[UserFileSource] | None = None,
        user_file_contents: dict[str, str] | None = None,
    ) -> str | None:
        """Generate the final summary markdown."""
        user_prompt = self._build_user_prompt(
            meta_summary=meta_summary,
            individual_summaries=individual_summaries,
            topic=topic,
            days=days,
            provider_results=provider_results,
            site_research=site_research,
            user_url_sources=user_url_sources,
            user_file_sources=user_file_sources,
            user_file_contents=user_file_contents,
        )
        return self._run_claude(user_prompt)


class DigestGenerator(ClaudeFinalSummaryGenerator):
    """Generate the concise digest summary."""

    stage_name = "digest"
    display_name = "digest"

    def get_system_prompt(self) -> str:
        """Return the digest system prompt."""
        return get_digest_system_prompt()

    def get_user_prompt_header(self, topic: str, days: int) -> str:
        """Return the digest task header."""
        return get_digest_user_prompt_header(topic, days)

    def get_user_prompt_footer(self) -> str:
        """Return the digest task footer."""
        return get_digest_user_prompt_footer()


class ProfessionalOverviewGenerator(ClaudeFinalSummaryGenerator):
    """Generate the article-style professional overview."""

    stage_name = "professional_overview"
    display_name = "professional overview"

    def get_system_prompt(self) -> str:
        """Return the professional overview system prompt."""
        return get_professional_overview_system_prompt()

    def get_user_prompt_header(self, topic: str, days: int) -> str:
        """Return the professional overview task header."""
        return get_professional_overview_user_prompt_header(topic, days)

    def get_user_prompt_footer(self) -> str:
        """Return the professional overview task footer."""
        return get_professional_overview_user_prompt_footer()


# Super-summary across boosted sub-projects gets more headroom than a single report.
SUPER_SUMMARY_MAX_BUDGET = 8.0


class SuperSummaryGenerator(ClaudeFinalSummaryGenerator):
    """Synthesize several parallel sub-project reports into one blog-style article.

    Authored by the council *boss* (e.g. Opus): integrates across sub-investigations,
    references each by sub-topic, and preserves the most important source links.
    """

    stage_name = "super_summary"
    display_name = "super-summary"

    @classmethod
    def from_effective_models(
        cls,
        effective_models: EffectiveModels | None,
    ) -> SuperSummaryGenerator:
        """Create the generator pinned to the council boss model."""
        if effective_models is None:
            return cls(model="claude-opus-4-8", max_budget=SUPER_SUMMARY_MAX_BUDGET)
        # The super-summary runs via the Claude Code CLI, so it needs a Claude
        # boss; fall back to Opus if the configured boss is a non-Claude model.
        boss = effective_models.council_boss
        if boss.lower().startswith("claude"):
            model = boss
        else:
            logger.warning(
                "Super-summary runs via the Claude CLI but council boss is %r; "
                "falling back to claude-opus-4-8 for the super-summary.",
                boss,
            )
            model = "claude-opus-4-8"
        return cls(
            model=model,
            max_budget=max(
                effective_models.claude_max_budget, SUPER_SUMMARY_MAX_BUDGET
            ),
        )

    def get_system_prompt(self) -> str:
        """Return the super-summary system prompt."""
        return get_super_summary_system_prompt()

    def generate_super_summary(
        self,
        topic: str,
        days: int,
        sub_reports: list[dict[str, str]],
    ) -> str | None:
        """Generate the integrated super-summary markdown.

        Args:
            topic: The overarching (parent) topic.
            days: Lookback window.
            sub_reports: One dict per sub-project with keys ``subtopic``,
                ``summary_md`` (the sub-report's overview/digest markdown), and
                ``sources_md`` (a markdown bullet list of its key links).
        """
        if not sub_reports:
            return None

        sections: list[str] = [
            get_super_summary_user_prompt_header(topic, days, len(sub_reports))
        ]
        for i, sr in enumerate(sub_reports, start=1):
            subtopic = sr.get("subtopic", f"Sub-investigation {i}")
            summary_md = sr.get("summary_md", "").strip()
            sources_md = sr.get("sources_md", "").strip()
            block = [f"\n\n---\n\n## Sub-investigation {i}: {subtopic}\n"]
            if summary_md:
                block.append(f"### Findings\n{summary_md}\n")
            if sources_md:
                block.append(f"### Sources\n{sources_md}\n")
            sections.append("\n".join(block))
        sections.append(f"\n\n---\n\n{get_super_summary_user_prompt_footer()}")

        user_prompt = "\n".join(sections)
        return self._run_claude(user_prompt)
