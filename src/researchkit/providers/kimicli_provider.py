"""Kimi provider variant that runs via the Kimi Code CLI in non-interactive mode.

Instead of calling the Moonshot API directly (see ``KimiProvider``), this
provider shells out to the Kimi Code CLI's headless single-turn mode
(``kimi -p … --output-format stream-json``). The main draw is auth/billing:
after ``kimi login`` (OAuth device flow), queries run against the Kimi Code
subscription rather than MOONSHOT_API_KEY per-token billing — the same story
as ``codex:`` (ChatGPT subscription), ``agy:`` (Google account) and
``grokcli:`` (grok.com subscription).

Selecting it: set the ``kimi`` model in ``models.yaml`` to ``kimicli`` (CLI
default model) or ``kimicli:<alias>`` (e.g. ``kimicli:kimi-code/k3``; the
CLI's config.toml lists the aliases). The aggregator routes the kimi slot to
this provider whenever :func:`is_kimicli_model` matches; it still reports
``provider_name = "kimi"`` and is differentiated by its ``model`` field.
Council members accept the same spec.

This targets Moonshot's **Kimi Code CLI** (``MoonshotAI/kimi-code``, the
TypeScript rewrite; ``brew install kimi-code``), not the wound-down Python
``kimi-cli``. Headless notes:

- ``-p`` implies auto-permission and the CLI has no sandbox/tool-deny flags,
  so runs get the agy treatment: scrubbed env (no other providers' keys to
  exfiltrate) and a neutral temp cwd (host project instructions stay out of
  prompts; file edits land in temp). ``run_subprocess`` kills the process
  group on timeout, which also bounds the CLI's unbounded print-mode
  background tasks.
- Web search/fetch are built-in tools injected by the subscription auth;
  there is no CLI flag to disable them, so ``web_search=False`` calls rely on
  the prompt simply not needing a search.
- Reasoning effort has no CLI flag (config.toml ``[thinking]`` governs); the
  constructor accepts ``reasoning_effort`` for parity and ignores it.

Citations: the headless stream carries only message text (no structured
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


def is_kimicli_model(model: str | None) -> bool:
    """Return ``True`` when ``model`` selects the Kimi-Code-CLI path.

    Matches ``kimicli`` and ``kimicli:<alias>`` (case-insensitive).
    """
    if not model:
        return False
    return model.lower().split(":", 1)[0].strip() == "kimicli"


def kimicli_underlying_model(model: str | None) -> str | None:
    """Extract the underlying model alias from a ``kimicli`` spec.

    ``"kimicli:kimi-code/k3"`` -> ``"kimi-code/k3"``; ``"kimicli"`` ->
    ``None`` (CLI default model from config.toml).
    """
    if not model:
        return None
    parts = model.split(":", 1)
    if len(parts) == 2 and parts[1].strip():
        return parts[1].strip()
    return None


class KimiCliProvider(CodexProvider):
    """Kimi-via-``kimi -p`` provider with web search.

    Subclasses :class:`CodexProvider` for its whole research shape (social/web
    dual query, inline-URL source extraction, title backfill, CLI-backed
    keywords and summaries); only the command line and output parsing differ.
    """

    _label_prefix = "kimicli"
    _error_prefix = "Kimi Code CLI"

    def __init__(
        self,
        sources: set[str] | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        kimi_bin: str = "kimi",
        provider_name: str = "kimi",
    ) -> None:
        """Initialize the Kimi Code CLI provider.

        The CLI resolves its own auth (``kimi login`` OAuth or a config.toml
        provider key), so there is no api_key parameter.

        Args:
            sources: Set of sources to query ("social", "web", or both).
            model: Model alias passed as ``-m`` (None = CLI default model).
            reasoning_effort: Accepted for parity; the CLI has no effort flag
                (config.toml ``[thinking]`` governs) so it is ignored.
            kimi_bin: Path/name of the Kimi Code CLI binary.
            provider_name: Slot this provider reports as (default "kimi").
        """
        super().__init__(
            sources=sources,
            model=model,
            reasoning_effort=reasoning_effort,
            codex_bin=kimi_bin,  # inherited which()-availability checks use this
            provider_name=provider_name,
        )

    @property
    def _model_label(self) -> str:
        """Differentiated model string reported on results."""
        return f"kimicli:{self.model_name}" if self.model_name else "kimicli:default"

    def _build_cmd(self, web_search: bool) -> list[str]:
        """Assemble the headless ``kimi`` command (prompt appended by _exec).

        ``web_search`` is unused: the CLI's search tools can't be toggled per
        run (see module docstring).
        """
        cmd = [
            self.codex_bin,
            "--output-format",
            "stream-json",
        ]
        if self.model_name:
            cmd += ["-m", self.model_name]
        return cmd

    @staticmethod
    def _parse_output(stdout: str) -> tuple[str, list[str]]:
        """Parse the stream-json JSONL into (final_text, []).

        The stream emits ``{"role": "assistant", "content": …}`` messages
        (plus tool/meta events); the final answer is the LAST assistant
        message with non-empty string content. Error meta events raise so
        transient messages reach the retry policy's phrase matching.
        """
        text = ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue
            if "error" in str(evt.get("type", "")):
                raise RuntimeError(
                    f"kimi CLI error: {evt.get('content') or evt.get('message') or evt}"
                )
            if evt.get("role") == "assistant":
                content = evt.get("content")
                if isinstance(content, str) and content.strip():
                    text = content
        return text, []

    def _exec(
        self, prompt: str, *, web_search: bool, label: str
    ) -> tuple[str, list[str]]:
        """Run one headless ``kimi`` invocation; return (final_text, [])."""
        timeout = max(provider_http_timeout(), 300.0)

        def _call() -> tuple[str, list[str]]:
            # Own process group + kill-on-timeout, UTF-8 decode, and a
            # scrubbed env so untrusted web content can't exfiltrate other
            # providers' API keys — the CLI auths via ``kimi login`` and
            # reads no key env vars at all.
            proc = run_subprocess(
                [*self._build_cmd(web_search), "-p", prompt],
                timeout=timeout,
                env=scrubbed_env(),
                # Neutral cwd: keep the host project's AGENTS.md/instructions
                # out of research prompts, and point the CLI's workspace (it
                # has no read-only mode) at a throwaway directory.
                cwd=tempfile.gettempdir(),
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(
                    f"kimi CLI failed (exit {proc.returncode}): {detail[-500:]}"
                )
            # Parse INSIDE the retried callable: error meta events on exit 0
            # ("rate limit", "overloaded") must reach the retry policy.
            return self._parse_output(proc.stdout)

        return with_network_retry(_call, label=label, provider=self.provider_name)
