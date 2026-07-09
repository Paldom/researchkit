"""Grok provider variant that runs via the Grok CLI in non-interactive mode.

Instead of calling the xAI API directly (see ``GrokProvider``), this provider
shells out to the Grok CLI's headless single-turn mode (``grok -p …
--output-format json``) with its built-in web search. The main draw is
auth/billing: after ``grok login`` (OAuth), queries run against the grok.com
subscription rather than XAI_API_KEY per-token billing — the same story as
``codex:`` (ChatGPT subscription) and ``agy:`` (Google account).

Selecting it: set the ``grok`` model in ``models.yaml`` to ``grokcli`` (CLI
default model) or ``grokcli:<model>`` (``grok models`` lists what your login
offers, e.g. ``grokcli:grok-build``). The aggregator routes the grok slot to
this provider whenever :func:`is_grokcli_model` matches; it still reports
``provider_name = "grok"`` and is differentiated by its ``model`` field
(``grokcli:<model>`` / ``grokcli:default``). Council members accept the same
spec.

This targets xAI's official Grok CLI (the ``grok`` binary with ``-p``,
``--output-format json``, ``--disallowed-tools``). Other tools that ship a
``grok`` binary (e.g. superagent-ai's grok-cli) use different flags and
GROK_API_KEY billing — point ``grok_bin`` at the official CLI.

Citations: the headless JSON carries only the final text (no structured
citations), so sources are the URLs cited inline in the answer, with titles
backfilled — the same recovery path CodexProvider uses.
"""

from __future__ import annotations

import json
import tempfile

from researchkit.network_retry import with_network_retry
from researchkit.providers.base import provider_http_timeout
from researchkit.providers.codex_provider import CodexProvider
from researchkit.safe_io import run_subprocess, scrubbed_env


def is_grokcli_model(model: str | None) -> bool:
    """Return ``True`` when ``model`` selects the Grok-CLI path.

    Matches ``grokcli`` and ``grokcli:<model>`` (case-insensitive).
    """
    if not model:
        return False
    return model.lower().split(":", 1)[0].strip() == "grokcli"


def grokcli_underlying_model(model: str | None) -> str | None:
    """Extract the underlying model from a ``grokcli`` spec.

    ``"grokcli:grok-build"`` -> ``"grok-build"``; ``"grokcli"`` -> ``None``
    (CLI default model).
    """
    if not model:
        return None
    parts = model.split(":", 1)
    if len(parts) == 2 and parts[1].strip():
        return parts[1].strip()
    return None


# Web research needs several search/fetch rounds; a cap keeps a wedged agent
# from looping on the subscription.
_MAX_TURNS = "15"

# The Grok CLI is a full coding agent; research runs deny the side-effect
# tools (shell, edits, subagents) AND the read-class tools — the kernel
# sandbox already blocks writes, and denying reads narrows injection-steered
# read-secrets-then-exfiltrate. The web search/fetch channel itself remains:
# the same accepted exposure as the codex path. (A --tools allow-list would
# be tighter but trips CLI-internal tool validation as of 0.2.93.)
_DENY_TOOLS = "run_terminal_cmd,search_replace,read_file,grep,list_dir,Agent"


class GrokCliProvider(CodexProvider):
    """Grok-via-``grok -p`` provider with web search.

    Subclasses :class:`CodexProvider` for its whole research shape (social/web
    dual query, inline-URL source extraction, title backfill, CLI-backed
    keywords and summaries); only the command line and output parsing differ.
    """

    _label_prefix = "grokcli"
    _error_prefix = "grok CLI"

    def __init__(
        self,
        sources: set[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        grok_bin: str = "grok",
        provider_name: str = "grok",
    ) -> None:
        """Initialize the Grok CLI provider.

        The CLI resolves its own auth (``grok login`` or its XAI_API_KEY
        fallback), so there is no api_key parameter.

        Args:
            sources: Set of sources to query ("social", "web", or both).
            model: Underlying model passed as ``-m`` (None = CLI default).
            reasoning_effort: Passed to ``--reasoning-effort`` (the CLI
                accepts none/minimal/low/medium/high/xhigh/max).
            grok_bin: Path/name of the Grok CLI binary.
            provider_name: Slot this provider reports as (default "grok").
        """
        super().__init__(
            sources=sources,
            model=model,
            reasoning_effort=reasoning_effort,
            codex_bin=grok_bin,  # inherited which()-availability checks use this
            provider_name=provider_name,
        )

    @property
    def _model_label(self) -> str:
        """Differentiated model string reported on results."""
        return f"grokcli:{self.model_name}" if self.model_name else "grokcli:default"

    def _build_cmd(self, web_search: bool) -> list[str]:
        """Assemble the headless ``grok`` command (prompt appended by _exec)."""
        cmd = [
            self.codex_bin,
            "--output-format",
            "json",
            "--verbatim",
            "--permission-mode",
            "bypassPermissions",
            # bypassPermissions is bounded by the layers below: kernel-enforced
            # read-only sandbox (writes only to ~/.grok session state + temp,
            # mirroring codex's `-s read-only`) + side-effect tools denied.
            "--sandbox",
            "read-only",
            "--max-turns",
            _MAX_TURNS,
            "--disallowed-tools",
            _DENY_TOOLS,
            # Keep any user-configured MCP servers out of research runs
            # (deny rules take precedence over the permission mode).
            "--deny",
            "MCPTool",
            # Neutral cwd: keep the host project's AGENTS.md/instructions out
            # of research prompts (context pollution + injection surface).
            "--cwd",
            tempfile.gettempdir(),
        ]
        if not web_search:
            cmd.append("--disable-web-search")
        if self.model_name:
            cmd += ["-m", self.model_name]
        if self.reasoning_effort:
            cmd += ["--reasoning-effort", self.reasoning_effort]
        return cmd

    @staticmethod
    def _parse_output(stdout: str) -> tuple[str, list[str]]:
        """Parse the single headless JSON object into (final_text, []).

        The CLI emits ``{"text": …, "stopReason": …}`` on success and
        ``{"type": "error", "message": …}`` on failure. Unlike codex there is
        no opened-URL stream — sources come from inline citations only.
        """
        try:
            data = json.loads(stdout.strip() or "{}")
        except json.JSONDecodeError:
            return stdout.strip(), []  # best effort: treat as plain text
        if isinstance(data, dict):
            if data.get("type") == "error":
                raise RuntimeError(f"grok CLI error: {data.get('message', 'unknown')}")
            return str(data.get("text") or ""), []
        return stdout.strip(), []

    def _exec(
        self, prompt: str, *, web_search: bool, label: str
    ) -> tuple[str, list[str]]:
        """Run one headless ``grok`` invocation; return (final_text, [])."""
        timeout = max(provider_http_timeout(), 300.0)

        def _call() -> tuple[str, list[str]]:
            # Prompt via file, not argv: research prompts (and the full report
            # text summarize_result embeds) stay out of `ps`, and Linux's
            # per-argument size cap can't truncate the exec.
            with tempfile.NamedTemporaryFile(
                "w", suffix=".md", encoding="utf-8"
            ) as prompt_file:
                prompt_file.write(prompt)
                prompt_file.flush()
                # Own process group + kill-on-timeout, UTF-8 decode, and a
                # scrubbed env so untrusted web content can't exfiltrate other
                # providers' API keys — the CLI auths via `grok login`
                # (XAI_API_KEY kept as its documented fallback).
                proc = run_subprocess(
                    [*self._build_cmd(web_search), "--prompt-file", prompt_file.name],
                    timeout=timeout,
                    env=scrubbed_env(keep=frozenset({"XAI_API_KEY"})),
                )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"grok CLI failed (exit {proc.returncode}): {proc.stderr[-500:]}"
                )
            # Parse INSIDE the retried callable: the CLI reports some failures
            # as exit-0 {"type":"error"} objects, and transient ones ("rate
            # limit", "unavailable") must reach the retry policy's phrase
            # matching like their nonzero-exit siblings.
            return self._parse_output(proc.stdout)

        return with_network_retry(_call, label=label, provider=self.provider_name)
