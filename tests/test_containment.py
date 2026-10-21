"""Containment of harness CLIs that read untrusted web pages (PLAN 1.10).

Spotlighting/delimiting falls to >95% attack success under adaptive attacks;
what holds is capability containment outside the model (digest 5). These
tests pin the argv every research harness runs with, so a change that widens
a tool list or drops a sandbox fails CI. Live checks (2026-10-21): headless
Kimi without a profile ran a planted `touch`; with the profile it refused,
and a resumed session kept the profile.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from researchkit.harness_profiles import profile_path

_SHELL_OR_WRITE = {"Bash", "Shell", "Write", "Edit", "WriteFile", "NotebookEdit"}


def _profile_tools(name: str) -> list[str]:
    text = Path(profile_path(name)).read_text(encoding="utf-8")
    front = text.split("---")[1]
    if re.search(r"^tools:\s*\[\]\s*$", front, re.M):
        return []
    return re.findall(r"^\s+-\s+(\S+)\s*$", front.split("tools:", 1)[1], re.M)


class TestKimiProfiles:
    def test_researcher_is_web_only(self) -> None:
        assert set(_profile_tools("kimi-researcher.md")) == {"WebSearch", "FetchURL"}

    def test_no_tools_profile_is_empty(self) -> None:
        assert _profile_tools("kimi-no-tools.md") == []

    def test_advisor_reads_but_never_writes_or_shells(self) -> None:
        tools = set(_profile_tools("kimi-advisor.md"))
        assert {"Read", "Grep", "Glob"} <= tools
        assert not tools & _SHELL_OR_WRITE

    def test_provider_passes_profile_per_call_kind(self) -> None:
        from researchkit.providers.kimicli_provider import KimiCliProvider

        p = KimiCliProvider(model="kimi-code/k3")
        research = p._build_cmd(web_search=True)
        side = p._build_cmd(web_search=False)
        assert research[research.index("--agent-file") + 1].endswith(
            "kimi-researcher.md"
        )
        assert side[side.index("--agent-file") + 1].endswith("kimi-no-tools.md")

    def test_session_new_uses_advisor_resume_inherits(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import researchkit.harness_session as hs

        seen: list[list[str]] = []

        def fake(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(hs, "_spawn", fake)
        hs._run_kimicli("kimicli:kimi-code/k3", "sys", "q", None, "/tmp")
        hs._run_kimicli("kimicli:kimi-code/k3", "sys", "q", "session_x", "/tmp")
        assert seen[0][seen[0].index("--agent-file") + 1].endswith("kimi-advisor.md")
        assert "--agent-file" not in seen[1] and "-r" in seen[1]


class TestHarnessArgv:
    def test_codex_runs_read_only(self) -> None:
        from researchkit.providers.codex_provider import CodexProvider

        cmd = CodexProvider(model="gpt-6.1-sol")._build_cmd(web_search=True)
        assert cmd[cmd.index("-s") + 1] == "read-only"

    def test_grok_sandboxed_and_shell_denied(self) -> None:
        from researchkit.providers.grokcli_provider import GrokCliProvider

        cmd = GrokCliProvider(model="grok-4.7")._build_cmd(web_search=True)
        assert cmd[cmd.index("--sandbox") + 1] in ("read-only", "workspace")
        denied = cmd[cmd.index("--disallowed-tools") + 1]
        assert "run_terminal_cmd" in denied.split(",")  # grok's shell tool

    def test_agy_sandboxed(self) -> None:
        from researchkit.providers.antigravity_provider import AntigravityProvider

        cmd = AntigravityProvider(model="gemini-3.8-flash-high")._build_cmd(
            "q", timeout=600
        )
        assert "--sandbox" in cmd

    def test_claude_research_is_web_only(self) -> None:
        import json

        from researchkit.providers.claude_provider import ClaudeProvider

        done = subprocess.CompletedProcess(
            ["claude"], 0, stdout=json.dumps({"result": "x https://a.b/c"}), stderr=""
        )
        with patch(
            "researchkit.providers.claude_provider._run_claude_cli_with_retry",
            return_value=done,
        ) as run:
            ClaudeProvider(model="claude-opus-5-5", sources={"web"}).fetch_insights(
                "t", 7
            )
        cmd = run.call_args.args[0]
        assert cmd[cmd.index("--allowed-tools") + 1] == "WebSearch,WebFetch"
        assert "Bash" in cmd[cmd.index("--disallowed-tools") + 1]
        assert "--strict-mcp-config" in cmd

    def test_writers_have_no_tools_that_act(self) -> None:
        from researchkit.final_summary import ArticleGenerator

        gen = ArticleGenerator()
        with patch(
            "researchkit.final_summary.run_subprocess",
            return_value=subprocess.CompletedProcess(
                ["claude"], 0, stdout="{}", stderr=""
            ),
        ) as run:
            gen._run_claude("prompt")
        cmd = run.call_args.args[0]
        denied = set(cmd[cmd.index("--disallowed-tools") + 1].split(","))
        assert {"Bash", "Write", "Edit", "WebFetch", "WebSearch"} <= denied
        assert "--strict-mcp-config" in cmd

    def test_summarize_npx_is_pinned(self) -> None:
        from researchkit.user_links import _SUMMARIZE_PACKAGE

        assert not _SUMMARIZE_PACKAGE.endswith("@latest")
        assert re.search(r"@\d+\.\d+\.\d+$", _SUMMARIZE_PACKAGE)
