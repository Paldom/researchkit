"""Tests for the Grok-CLI-backed provider (grokcli:<model> spec)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import researchkit.providers.grokcli_provider as grokcli_mod
from researchkit.providers import GrokProvider
from researchkit.providers.grokcli_provider import (
    GrokCliProvider,
    grokcli_underlying_model,
    is_grokcli_model,
)


class TestSpecHelpers:
    def test_is_grokcli_model(self) -> None:
        assert is_grokcli_model("grokcli")
        assert is_grokcli_model("grokcli:grok-build")
        assert is_grokcli_model("GROKCLI:grok-build")
        assert not is_grokcli_model("grok-4.3")  # plain id -> API path
        assert not is_grokcli_model("codex:gpt-5.5")
        assert not is_grokcli_model(None)
        assert not is_grokcli_model("")

    def test_underlying_model(self) -> None:
        assert grokcli_underlying_model("grokcli:grok-build") == "grok-build"
        assert grokcli_underlying_model("grokcli") is None
        assert grokcli_underlying_model("grokcli:") is None
        assert grokcli_underlying_model(None) is None


class TestBuildCmd:
    def test_web_search_command_shape(self) -> None:
        provider = GrokCliProvider(model="grok-build", reasoning_effort="low")
        cmd = provider._build_cmd(web_search=True)
        assert cmd[0] == "grok"
        assert "--output-format" in cmd and "json" in cmd
        assert "--verbatim" in cmd
        assert "--disallowed-tools" in cmd
        deny = cmd[cmd.index("--disallowed-tools") + 1]
        assert "run_terminal_cmd" in deny and "Agent" in deny
        assert "read_file" in deny  # read-class tools denied too
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"
        assert cmd[cmd.index("--deny") + 1] == "MCPTool"
        assert "--disable-web-search" not in cmd
        assert cmd[cmd.index("-m") + 1] == "grok-build"
        assert cmd[cmd.index("--reasoning-effort") + 1] == "low"

    def test_no_web_search_disables_it(self) -> None:
        cmd = GrokCliProvider()._build_cmd(web_search=False)
        assert "--disable-web-search" in cmd
        assert "-m" not in cmd  # CLI default model

    def test_model_label(self) -> None:
        assert GrokCliProvider(model="grok-build")._model_label == "grokcli:grok-build"
        assert GrokCliProvider()._model_label == "grokcli:default"
        assert GrokCliProvider().provider_name == "grok"


class TestParseOutput:
    def test_success_object(self) -> None:
        out = json.dumps({"text": "answer", "stopReason": "EndTurn", "thought": "…"})
        assert GrokCliProvider._parse_output(out) == ("answer", [])

    def test_error_object_raises(self) -> None:
        out = json.dumps({"type": "error", "message": "Couldn't start session"})
        with pytest.raises(RuntimeError, match="Couldn't start session"):
            GrokCliProvider._parse_output(out)

    def test_non_json_falls_back_to_plain_text(self) -> None:
        assert GrokCliProvider._parse_output("plain answer\n") == ("plain answer", [])

    def test_empty_output(self) -> None:
        assert GrokCliProvider._parse_output("") == ("", [])


def _fake_run(text: str):
    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # prompt travels via file, not argv (ps visibility / arg-size cap)
        prompt_path = cmd[cmd.index("--prompt-file") + 1]
        assert Path(prompt_path).read_text(encoding="utf-8")
        assert "-p" not in cmd
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"text": text, "stopReason": "EndTurn"}),
            stderr="",
        )

    return run


class TestFetchInsights:
    def test_dual_query_extracts_inline_sources(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        provider = GrokCliProvider(model="grok-build")
        monkeypatch.setattr(
            "researchkit.providers.codex_provider.shutil.which", lambda _: "/bin/grok"
        )
        monkeypatch.setattr(
            grokcli_mod,
            "run_subprocess",
            _fake_run("Found [thing](https://example.test/story). More prose."),
        )
        monkeypatch.setattr(GrokCliProvider, "_backfill_titles", lambda self, s: None)

        result = provider.fetch_insights("ai agents", days=7)
        assert result.is_success
        assert result.provider == "grok"
        assert result.model == "grokcli:grok-build"
        urls = {s.url for s in result.sources}
        assert urls == {"https://example.test/story"}
        # social + web sections both present
        assert "Social Media Analysis" in result.raw_text
        assert "Web Research Analysis" in result.raw_text

    def test_missing_binary_is_graceful(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "researchkit.providers.codex_provider.shutil.which", lambda _: None
        )
        result = GrokCliProvider().fetch_insights("topic", days=7)
        assert not result.is_success
        assert "not found on PATH" in (result.error or "")

    def test_cli_error_object_becomes_error_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "researchkit.providers.codex_provider.shutil.which", lambda _: "/bin/grok"
        )

        def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"type":"error","message":"boom"}', stderr=""
            )

        monkeypatch.setattr(grokcli_mod, "run_subprocess", run)
        result = GrokCliProvider().fetch_insights("topic", days=7)
        assert not result.is_success
        assert "boom" in (result.error or "")

    def test_error_object_raises_inside_retried_callable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exit-0 {"type":"error"} objects must surface INSIDE the retried
        # callable so transient messages hit the retry policy (red-team M1).
        raised_inside: list[RuntimeError] = []

        def fake_retry(fn: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return fn()
            except RuntimeError as e:
                raised_inside.append(e)
                raise

        monkeypatch.setattr(grokcli_mod, "with_network_retry", fake_retry)

        def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout='{"type":"error","message":"rate limit"}', stderr=""
            )

        monkeypatch.setattr(grokcli_mod, "run_subprocess", run)
        with pytest.raises(RuntimeError, match="rate limit"):
            GrokCliProvider()._exec("prompt", web_search=False, label="t")
        assert len(raised_inside) == 1


class TestCodexErrorDetail:
    def test_stdout_jsonl_error_surfaced_when_stderr_empty(self) -> None:
        # Codex reports usage-limit/auth errors as JSONL events on STDOUT
        # with empty stderr; the failure message must carry them.
        import subprocess

        from researchkit.providers.codex_provider import CodexProvider

        proc = subprocess.CompletedProcess(
            ["codex"],
            1,
            stdout=(
                '{"type":"turn.started"}\n'
                '{"type":"error","message":"You have hit your usage limit."}\n'
                '{"type":"turn.failed","error":{"message":"You have hit your usage limit."}}'
            ),
            stderr="",
        )
        assert "usage limit" in CodexProvider._error_detail(proc)
        # stderr wins when present
        proc2 = subprocess.CompletedProcess(["codex"], 1, stdout="x", stderr="boom")
        assert CodexProvider._error_detail(proc2) == "boom"
        # no output at all
        proc3 = subprocess.CompletedProcess(["codex"], 1, stdout="", stderr="")
        assert CodexProvider._error_detail(proc3) == "(no output)"


class TestRouting:
    def test_factory_routes_grokcli_spec(self) -> None:
        from researchkit.plugin_api import ProviderContext
        from researchkit.plugins_builtin import _make_grok

        ctx = ProviderContext(
            model="grokcli:grok-build",
            sources=frozenset({"web"}),
            keywords=(),
            options={"reasoning_effort": "low"},
        )
        provider = _make_grok(ctx)
        assert isinstance(provider, GrokCliProvider)
        assert provider.model_name == "grok-build"
        assert provider.reasoning_effort == "low"
        assert provider.provider_name == "grok"

    def test_factory_routes_plain_id_to_api(self) -> None:
        from researchkit.plugin_api import ProviderContext
        from researchkit.plugins_builtin import _make_grok

        ctx = ProviderContext(
            model="grok-4.3", sources=frozenset({"web"}), keywords=(), options={}
        )
        provider = _make_grok(ctx)
        assert isinstance(provider, GrokProvider)
        assert not isinstance(provider, GrokCliProvider)

    def test_council_routes_grokcli_member(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from researchkit.council import LLMCouncil

        calls: list[str] = []

        def fake_exec(self, prompt: str, *, web_search: bool, label: str):
            calls.append(self.model_name)
            assert web_search is False
            return json.dumps({"improved_topic": "better topic"}), []

        monkeypatch.setattr(GrokCliProvider, "_exec", fake_exec)
        council = LLMCouncil(members=["grokcli:grok-build"], boss="grokcli")
        text = council._complete("grokcli:grok-build", "sys", "user", label="t")
        assert "better topic" in text
        assert calls == ["grok-build"]
