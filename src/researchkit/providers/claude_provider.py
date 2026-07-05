"""Claude Code provider using CLI subprocess with WebSearch and WebFetch tools."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any
from urllib.parse import urlparse

from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from researchkit.providers.base import (
    SOCIAL_DOMAINS,
    BaseProvider,
    ProviderResult,
    Source,
    SourceType,
    get_base_system_prompt,
    get_user_prompt,
    get_web_system_prompt,
    get_web_user_prompt,
)
from researchkit.safe_io import run_subprocess

logger = logging.getLogger(__name__)


def is_deep_research_spec(model: str | None) -> bool:
    """Return ``True`` when ``model`` selects the /deep-research skill path.

    Matches ``deep`` and ``deep:<model>`` (also ``deep-research[:<model>]``),
    case-insensitive — same convention as ``codex:<m>`` / ``agy:<m>``.
    """
    if not model:
        return False
    return model.lower().split(":", 1)[0].strip() in ("deep", "deep-research")


def deep_research_underlying_model(model: str | None) -> str | None:
    """Extract the underlying model from a ``deep`` spec.

    ``"deep:claude-opus-4-8"`` -> ``"claude-opus-4-8"``;
    ``"deep"`` -> ``None`` (Claude Code CLI default model).
    """
    if not model:
        return None
    parts = model.split(":", 1)
    if len(parts) == 2 and parts[1].strip():
        return parts[1].strip()
    return None


# The /deep-research workflow fans out many searches and verification agents,
# so it needs far more headroom than a plain two-query run.
DEEP_RESEARCH_TIMEOUT_S = 2400

SUMMARIZER_SYSTEM_PROMPT = """You are a precise summarizer. Your task is to distill social insight reports into their essential points.

Rules:
- Extract 5-8 key bullet points
- Preserve specific examples, quotes, or data points
- Keep platform/source attributions and any source URLs
- Be concise but preserve critical details
- Summarize only what is in the source — never add claims, figures, or sources
  that do not appear in the report"""


# Conservative retry policy for the Claude Code CLI subprocess.
# Subprocess calls are expensive (each can take minutes) so we retry only
# on transient network signatures and give it just one retry.
_CLAUDE_CLI_NETWORK_SIGNATURES = (
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


def _is_claude_cli_transient(exc: BaseException) -> bool:
    """True if the Claude CLI subprocess error looks like a transient network issue."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return True
    if isinstance(exc, RuntimeError):
        msg = str(exc).lower()
        return any(sig in msg for sig in _CLAUDE_CLI_NETWORK_SIGNATURES)
    return False


def _claude_cli_retry_log(state: Any) -> None:
    exc = state.outcome.exception() if state.outcome else None
    if exc is None:
        return
    next_sleep = getattr(state.next_action, "sleep", None)
    wait_s = float(next_sleep) if next_sleep is not None else 0.0
    logger.warning(
        "Retrying claude CLI (attempt %d/2, waiting %.1fs) — %s: %s",
        state.attempt_number,
        wait_s,
        type(exc).__name__,
        str(exc)[:200],
        extra={
            "stage": "network_retry",
            "label": "claude.cli.subprocess",
            "attempt": state.attempt_number,
            "max_attempts": 2,
            "wait_s": round(wait_s, 2),
            "error_type": type(exc).__name__,
            "provider": "claude",
        },
    )


def _run_claude_cli_with_retry(
    cmd: list[str],
    env: dict[str, str],
    timeout: int,
    retry_on_timeout: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the Claude Code CLI with a single transient-network retry.

    ``retry_on_timeout=False`` treats a wall-clock timeout as terminal —
    appropriate for long deep-research runs where hitting the limit means
    the workload is too big, not that the network hiccuped.
    """

    def _should_retry(exc: BaseException) -> bool:
        if isinstance(exc, subprocess.TimeoutExpired) and not retry_on_timeout:
            return False
        return _is_claude_cli_transient(exc)

    last_exc: BaseException | None = None
    for attempt in Retrying(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=2, max=10) + wait_random(0, 2),
        retry=retry_if_exception(_should_retry),
        before_sleep=_claude_cli_retry_log,
        reraise=True,
    ):
        with attempt:
            # run_subprocess: own process group + kill-the-group on timeout so a
            # claude-spawned node/MCP grandchild can't be orphaned (review C2);
            # UTF-8 decode with errors="replace" for non-Latin output (review L26).
            result = run_subprocess(
                cmd,
                timeout=timeout,
                env=env,
            )
            # Surface non-zero exits as RuntimeError so the retry can inspect
            # the stderr for transient network signatures.
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                raise RuntimeError(
                    f"Claude Code CLI exited with code {result.returncode}: {detail}"
                )
            # An exit-0 result can still report {"is_error": true} for a transient
            # failure (overloaded/network); raise inside the retry scope so a
            # transient one is retried via the message signature check. A terminal
            # is_error reraises and callers handle it as before. (Review L24.)
            _raise_if_claude_is_error(result.stdout)
            return result
    raise RuntimeError("Claude Code CLI retry exhausted") from last_exc  # unreachable


def _raise_if_claude_is_error(stdout: str) -> None:
    """Raise RuntimeError if the CLI JSON reports ``is_error`` on an exit-0 run."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return
    if isinstance(payload, dict) and payload.get("is_error"):
        raise RuntimeError(f"Claude Code CLI error: {payload.get('result', 'unknown')}")


class ClaudeProvider(BaseProvider):
    """
    Claude Code provider using non-interactive CLI subprocess.

    Two modes, selected by the model spec:
    - Plain model id (e.g. ``claude-opus-4-8``): spawns `claude -p "..."` with
      WebSearch + WebFetch tools for two agentic queries (social + web).
    - ``deep`` / ``deep:<model>``: runs one combined query through Claude
      Code's built-in /deep-research skill — a dynamic workflow that fans out
      web searches, fetches sources, adversarially verifies claims and
      synthesizes a cited report. Much deeper, but slower and costlier.

    Requires:
    - Claude Code CLI installed: npm install -g @anthropic-ai/claude-code
    - Active Claude Code subscription (uses subscription auth, not API key)
    """

    provider_name = "claude"
    model_name = "claude-opus-4-7"

    def __init__(
        self,
        sources: set[str] | None = None,
        model: str | None = None,
        max_budget: float | None = None,
        reasoning_effort: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.sources = sources or {"social", "web"}
        # Deep-research effort follows the preset's reasoning_effort: `max`
        # fans out enormously (a live run made 174 web searches and spent
        # ~$19 before the budget cap killed it), so it must stay a
        # deliberate choice, never the default.
        self.reasoning_effort = reasoning_effort or "medium"
        self.deep_research = is_deep_research_spec(model)
        self._cli_model: str | None
        if self.deep_research:
            # Bare `deep` defers to the CLI's default model.
            underlying = deep_research_underlying_model(model)
            self._cli_model = underlying
            self.model_name = (
                f"deep-research:{underlying}" if underlying else "deep-research"
            )
        else:
            if model:
                self.model_name = model
            self._cli_model = self.model_name
        self.max_budget = max_budget or 5.0

    def _model_args(self) -> list[str]:
        """CLI ``--model`` args; empty when deferring to the CLI default."""
        return ["--model", self._cli_model] if self._cli_model else []

    def _build_env(self) -> dict[str, str]:
        """Build environment for subprocess with privacy settings."""
        env = {**os.environ}
        # Remove ANTHROPIC_API_KEY so the CLI uses subscription auth
        env.pop("ANTHROPIC_API_KEY", None)
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        return env

    def _extract_urls(self, text: str) -> list[str]:
        """Extract URLs from text, deduplicated and order-preserved.

        Keeps parens that are balanced within the URL (Wikipedia-style
        ``.../GPT-5_(model)``) while trimming trailing markdown emphasis,
        punctuation, and closing parens that belong to the surrounding text.
        """
        urls = []
        for raw in re.findall(r'https?://[^\s<>\[\]{}"\'`]+', text):
            url = raw
            while True:
                stripped = url.rstrip(".,;:!?\"'`*_")
                if stripped.endswith(")") and stripped.count("(") < stripped.count(")"):
                    stripped = stripped[:-1]
                if stripped == url:
                    break
                url = stripped
            if url:
                urls.append(url)
        return list(dict.fromkeys(urls))

    def _build_sources(self, text: str, source_type: SourceType) -> list[Source]:
        """Build Source objects from URLs extracted from text."""
        urls = self._extract_urls(text)
        return [Source(url=url, source_type=source_type) for url in urls]

    def _classify_url(self, url: str) -> SourceType:
        """SOCIAL if the URL host is (a subdomain of) a known social domain."""
        host = urlparse(url).netloc.lower().split(":", 1)[0]
        for domain in SOCIAL_DOMAINS:
            if host == domain or host.endswith("." + domain):
                return SourceType.SOCIAL
        return SourceType.WEB

    def _parse_cli_json(self, stdout: str) -> tuple[str, dict[str, Any]]:
        """Parse `claude -p --output-format json` stdout into (text, meta)."""
        try:
            output = json.loads(stdout)
        except json.JSONDecodeError:
            # Fallback: treat stdout as plain text
            return stdout.strip(), {}

        # Check for JSON-level errors (CLI may return exit code 0 with is_error=true)
        if output.get("is_error"):
            raise RuntimeError(
                f"Claude Code CLI error: {output.get('result', 'unknown error')}"
            )

        text = output.get("result", stdout.strip())

        meta: dict[str, Any] = {}
        for key in (
            "cost_usd",
            "total_cost_usd",
            "num_turns",
            "duration_ms",
            "duration_api_ms",
            "is_error",
            "session_id",
            "total_cost",
        ):
            if key in output:
                meta[key] = output[key]

        return text, meta

    def _run_claude_subprocess(
        self,
        system_prompt: str,
        user_prompt: str,
        allowed_domains: list[str] | None = None,
        source_type: SourceType = SourceType.WEB,
    ) -> tuple[str, list[Source], dict[str, Any]]:
        """
        Run a Claude Code CLI query with web search tools.

        Args:
            system_prompt: System prompt for the query
            user_prompt: User prompt (the research question)
            allowed_domains: Optional domain list to guide social queries
            source_type: Source type for extracted URLs

        Returns:
            Tuple of (response_text, sources, metadata)
        """
        if allowed_domains:
            domain_instruction = (
                "\n\nIMPORTANT: Focus your web searches on these social/discussion "
                "domains: "
                + ", ".join(allowed_domains[:15])
                + ". Prioritize content from Reddit, X/Twitter, Hacker News, "
                "and other community platforms."
            )
            system_prompt = system_prompt + domain_instruction

        cmd = [
            "claude",
            "-p",
            user_prompt,
            *self._model_args(),
            "--effort",
            "max",
            "--allowed-tools",
            "WebSearch,WebFetch",
            "--disallowed-tools",
            "Write,Edit,Bash,Read,Glob,Grep,NotebookEdit,Agent",
            "--permission-mode",
            "bypassPermissions",
            # Only load built-in tools — don't inherit the operator's MCP servers,
            # which bypassPermissions would auto-approve for untrusted web content
            # (review M8).
            "--strict-mcp-config",
            "--output-format",
            "json",
            "--system-prompt",
            system_prompt,
            "--no-session-persistence",
            "--max-budget-usd",
            str(self.max_budget),
        ]

        logger.debug(
            f"Running Claude Code CLI: model={self.model_name}, "
            f"max_budget=${self.max_budget}",
            extra={"stage": "claude_subprocess", "provider": self.provider_name},
        )

        result = _run_claude_cli_with_retry(cmd, env=self._build_env(), timeout=600)

        text, meta = self._parse_cli_json(result.stdout)
        sources = self._build_sources(text, source_type)

        return text, sources, meta

    def _deep_research_prompt(self, topic: str, days: int) -> str:
        """Build the /deep-research skill invocation for a topic."""
        scopes = []
        if "social" in self.sources:
            scopes.append(
                "social platforms (X/Twitter, Reddit, Hacker News, YouTube, "
                "TikTok, LinkedIn and similar community spaces)"
            )
        if "web" in self.sources:
            scopes.append(
                "the broader web (news sites, blogs, forums, expert commentary)"
            )
        scope = " and ".join(scopes) if scopes else "the web"
        return (
            f'/deep-research What are people saying about "{topic}" '
            f"in the last {days} days across {scope}? "
            "Surface the dominant narratives and discussions, overall sentiment "
            "and how it is shifting, notable posts/threads/videos, emerging "
            "trends or controversies, and specific real examples. "
            "This question is fully specified — do not ask clarifying "
            "questions; proceed straight to research. Prioritize content from "
            "the requested time window and attach a direct source URL to every "
            "claim. Scale note: this runs as one provider stream inside a "
            "larger aggregation pipeline, so keep the workflow lean — roughly "
            "10-20 targeted searches total and a small verification pass on "
            "load-bearing claims, not an exhaustive audit. Deliver the "
            "complete cited report in markdown as your final message; do not "
            "write it to a file."
        )

    def _fetch_deep_research(self, topic: str, days: int) -> ProviderResult:
        """Run one combined /deep-research workflow over the requested sources."""
        self._log_start()

        try:
            self._log_query("deep-research")
            if self.max_budget < 15.0:
                logger.warning(
                    "Deep-research budget $%.2f is likely too low — the CLI "
                    "enforces it lazily mid-workflow and an over-budget run "
                    "is killed WITHOUT returning a report. Recommend >= $15.",
                    self.max_budget,
                    extra={
                        "stage": "claude_subprocess",
                        "provider": self.provider_name,
                    },
                )
            # No --system-prompt override and no --allowed-tools restriction:
            # the skill needs Claude Code's default harness (Skill/Workflow/
            # subagents + web tools). Mutating tools are disabled so the run
            # is read-only and the report comes back inline on stdout.
            cmd = [
                "claude",
                "-p",
                self._deep_research_prompt(topic, days),
                *self._model_args(),
                "--effort",
                self.reasoning_effort,
                "--disallowed-tools",
                "Write,Edit,NotebookEdit,Bash",
                "--permission-mode",
                "bypassPermissions",
                "--output-format",
                "json",
                "--no-session-persistence",
                "--max-budget-usd",
                str(self.max_budget),
            ]

            logger.debug(
                f"Running Claude Code /deep-research: model={self.model_name}, "
                f"max_budget=${self.max_budget}",
                extra={"stage": "claude_subprocess", "provider": self.provider_name},
            )

            result = _run_claude_cli_with_retry(
                cmd,
                env=self._build_env(),
                timeout=DEEP_RESEARCH_TIMEOUT_S,
                # A deep run that hits the 40-min wall is workload-bound;
                # rerunning it would stall the pipeline and double the spend.
                retry_on_timeout=False,
            )

            text, cli_meta = self._parse_cli_json(result.stdout)
            sources = [
                Source(url=url, source_type=self._classify_url(url))
                for url in self._extract_urls(text)
            ]
            self._log_done(len(sources), len(text))

            return ProviderResult(
                provider=self.provider_name,
                model=self.model_name,
                raw_text=text,
                sources=sources,
                meta={"mode": "deep-research", **cli_meta},
            )

        except FileNotFoundError:
            return self._create_error_result(
                "Claude Code CLI not found "
                "(install: npm install -g @anthropic-ai/claude-code)"
            )
        except subprocess.TimeoutExpired:
            return self._create_error_result(
                f"Claude Code deep-research timed out "
                f"({DEEP_RESEARCH_TIMEOUT_S}s limit)"
            )
        except Exception as e:
            return self._create_error_result(f"Claude Code deep-research error: {e}")

    def fetch_insights(self, topic: str, days: int) -> ProviderResult:
        """Fetch insights using Claude Code with web search."""
        if self.deep_research:
            return self._fetch_deep_research(topic, days)

        self._log_start()

        try:
            sources: list[Source] = []
            meta: dict[str, Any] = {}
            sections: list[str] = []

            if "social" in self.sources:
                self._log_query("social")
                text, social_sources, social_meta = self._run_claude_subprocess(
                    get_base_system_prompt(days),
                    get_user_prompt(topic, days),
                    allowed_domains=SOCIAL_DOMAINS,
                    source_type=SourceType.SOCIAL,
                )
                sources.extend(social_sources)
                meta["social"] = social_meta
                sections.append(f"# Social Media Analysis\n\n{text}")

            if "web" in self.sources:
                self._log_query("web")
                text, web_sources, web_meta = self._run_claude_subprocess(
                    get_web_system_prompt(days),
                    get_web_user_prompt(topic, days),
                    source_type=SourceType.WEB,
                )
                sources.extend(web_sources)
                meta["web"] = web_meta
                sections.append(f"# Web Research Analysis\n\n{text}")

            combined = "\n\n---\n\n".join(sections)
            self._log_done(len(sources), len(combined))

            return ProviderResult(
                provider=self.provider_name,
                model=self.model_name,
                raw_text=combined,
                sources=sources,
                meta=meta,
            )

        except FileNotFoundError:
            return self._create_error_result(
                "Claude Code CLI not found "
                "(install: npm install -g @anthropic-ai/claude-code)"
            )
        except subprocess.TimeoutExpired:
            return self._create_error_result("Claude Code query timed out (600s limit)")
        except Exception as e:
            return self._create_error_result(f"Claude Code error: {e}")

    def generate_keywords(self, topic: str, days: int, context: str = "") -> list[str]:
        """Generate keywords using Claude Code CLI (no web tools)."""
        try:
            from researchkit.keyword_synthesizer import parse_keyword_json
            from researchkit.prompts import (
                get_keyword_generation_system_prompt,
                get_keyword_generation_user_prompt,
            )

            system_prompt = get_keyword_generation_system_prompt()
            user_prompt = get_keyword_generation_user_prompt(topic, days, context)

            cmd = [
                "claude",
                "-p",
                user_prompt,
                *self._model_args(),
                "--system-prompt",
                system_prompt,
                "--no-session-persistence",
                "--strict-mcp-config",  # built-in tools only (review M8)
                "--output-format",
                "json",
                "--disallowed-tools",
                "WebSearch,WebFetch,Write,Edit,Bash,Read,Glob,Grep,NotebookEdit,Agent",
                "--permission-mode",
                "bypassPermissions",
            ]

            try:
                result = _run_claude_cli_with_retry(
                    cmd, env=self._build_env(), timeout=120
                )
            except (RuntimeError, subprocess.TimeoutExpired):
                return []

            try:
                output = json.loads(result.stdout)
                if output.get("is_error"):
                    return []
                return parse_keyword_json(output.get("result", ""))
            except json.JSONDecodeError:
                return parse_keyword_json(result.stdout.strip())
        except Exception as e:
            logger.warning(f"Claude keyword generation failed: {e}")
            return []

    def summarize_result(self, raw_text: str, topic: str) -> str:
        """Summarize result using Claude Code CLI (no web tools)."""
        try:
            user_prompt = (
                f"Summarize this social insight report into 5-8 key bullet "
                f"points:\n\n**Topic:** {topic}\n\n---\n{raw_text}\n---\n\n"
                f"Format as a markdown bullet list. Start each bullet with a "
                f"bold label when appropriate (e.g., **Trend:**, **Sentiment:**, "
                f"**Notable:**)."
            )

            cmd = [
                "claude",
                "-p",
                user_prompt,
                *self._model_args(),
                "--system-prompt",
                SUMMARIZER_SYSTEM_PROMPT,
                "--no-session-persistence",
                "--strict-mcp-config",  # built-in tools only (review M8)
                "--output-format",
                "json",
                "--disallowed-tools",
                "WebSearch,WebFetch,Write,Edit,Bash,Read,Glob,Grep,NotebookEdit,Agent",
                "--permission-mode",
                "bypassPermissions",
            ]

            try:
                result = _run_claude_cli_with_retry(
                    cmd, env=self._build_env(), timeout=120
                )
            except (RuntimeError, subprocess.TimeoutExpired):
                return f"*Summarization failed*\n\n{raw_text[:500]}..."

            if result.stdout.strip():
                try:
                    output = json.loads(result.stdout)
                    if output.get("is_error"):
                        return f"*Summarization failed*\n\n{raw_text[:500]}..."
                    return output.get("result", result.stdout.strip())
                except json.JSONDecodeError:
                    return result.stdout.strip()
            return f"*Summarization failed*\n\n{raw_text[:500]}..."

        except Exception as e:
            return f"*Summarization failed: {e}*\n\n{raw_text[:500]}..."
