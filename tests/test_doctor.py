"""Tests for the `researchkit doctor` preflight and its CLI routing."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

import researchkit.doctor as doctor_mod
from researchkit.doctor import (
    CheckResult,
    _check_api_key,
    _check_spec,
    _CliProbe,
    format_report,
    run_doctor,
)


def _proc(rc: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(["x"], rc, stdout=stdout, stderr=stderr)


class TestCliProbe:
    def test_listable_model_present_and_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(doctor_mod.shutil, "which", lambda _: "/bin/grok")
        calls: list[list[str]] = []

        def run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
            calls.append(cmd)
            return _proc(0, stdout="Available models:\n  * grok-4.5 (default)\n")

        monkeypatch.setattr(doctor_mod, "run_subprocess", run)
        probe = _CliProbe()
        assert probe.check_listable("grok", ["models"], "grok-4.5")[0] == "ok"
        status, detail = probe.check_listable("grok", ["models"], "grok-build")
        assert status == "fail" and "grok-build" in detail
        assert len(calls) == 1  # the list command runs once per binary (cached)

    def test_model_match_is_whole_token_not_substring(self) -> None:
        from researchkit.doctor import _model_listed

        # A dead `grok-4` pin must NOT pass because `grok-4.5`/-fast is listed.
        assert not _model_listed("grok-4", "Available:\n  grok-4.5\n  grok-4-fast\n")
        assert _model_listed("grok-4.5", "Available:\n  * grok-4.5 (default)\n")
        # Display names with spaces/parens match exactly, not partially.
        listing = "Gemini 3.5 Flash (Medium)\nGemini 3.5 Flash (High)\n"
        assert _model_listed("Gemini 3.5 Flash (High)", listing)
        assert not _model_listed("Gemini 3.5 Flash", listing)

    def test_listable_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor_mod.shutil, "which", lambda _: None)
        probe = _CliProbe()
        assert probe.check_listable("grok", ["models"], None)[0] == "fail"

        monkeypatch.setattr(doctor_mod.shutil, "which", lambda _: "/bin/agy")
        monkeypatch.setattr(
            doctor_mod,
            "run_subprocess",
            lambda *a, **k: _proc(1, stderr="not logged in"),
        )
        status, detail = _CliProbe().check_listable("agy", ["models"], "X")
        assert status == "fail" and "not logged in" in detail

    def test_listable_spawn_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor_mod.shutil, "which", lambda _: "/bin/grok")

        def boom(*a: Any, **k: Any) -> subprocess.CompletedProcess:
            raise TimeoutError("timed out")

        monkeypatch.setattr(doctor_mod, "run_subprocess", boom)
        status, detail = _CliProbe().check_listable("grok", ["models"], None)
        assert status == "fail" and "timed out" in detail

    def test_binary_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor_mod.shutil, "which", lambda _: "/bin/kimi")
        assert _CliProbe().check_binary_only("kimi", "installed") == (
            "ok",
            "installed",
        )
        monkeypatch.setattr(doctor_mod.shutil, "which", lambda _: None)
        assert _CliProbe().check_binary_only("kimi", "installed")[0] == "fail"


class TestApiKeyChecks:
    def test_key_present_warn_and_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
        ok = _check_api_key(
            "perplexity", "sonar", ("PERPLEXITY_API_KEY",), skippable=True
        )
        assert ok.status == "ok" and "PERPLEXITY_API_KEY" in ok.detail

        monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
        warn = _check_api_key(
            "perplexity", "sonar", ("PERPLEXITY_API_KEY",), skippable=True
        )
        assert warn.status == "warn" and "skipped" in warn.detail
        fail = _check_api_key(
            "summarizer", "sonar", ("PERPLEXITY_API_KEY",), skippable=False
        )
        assert fail.status == "fail"


class TestSpecRouting:
    def test_cli_specs_route_to_their_binaries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(doctor_mod.shutil, "which", lambda b: f"/bin/{b}")
        monkeypatch.setattr(
            doctor_mod,
            "run_subprocess",
            lambda cmd, **k: _proc(0, stdout="grok-4.5\nGemini 3.5 Flash (High)\n"),
        )
        probe = _CliProbe()
        assert _check_spec("grok", "grokcli:grok-4.5@high", probe).status == "ok"
        assert (
            _check_spec("gemini", "agy:Gemini 3.5 Flash (High)", probe).status == "ok"
        )
        assert _check_spec("openai", "codex:gpt-5.6-sol@xhigh", probe).status == "ok"
        assert _check_spec("kimi", "kimicli:kimi-code/k3", probe).status == "ok"
        assert _check_spec("claude", "claude:claude-opus-4-8", probe).status == "ok"
        assert _check_spec("claude", "deep:claude-sonnet-5", probe).status == "ok"

    def test_plain_id_routes_to_key_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
        monkeypatch.delenv("KIMI_API_KEY", raising=False)
        result = _check_spec("summarizer", "kimi-k2.6", _CliProbe())
        assert result.status == "fail" and "MOONSHOT_API_KEY" in result.detail
        monkeypatch.setenv("GEMINI_API_KEY", "k")
        assert _check_spec("summarizer", "gemini-3.5-flash", _CliProbe()).status == "ok"


class TestRunDoctorAndReport:
    def test_harness_preset_end_to_end(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor_mod.shutil, "which", lambda b: f"/bin/{b}")
        monkeypatch.setattr(
            doctor_mod,
            "run_subprocess",
            lambda cmd, **k: _proc(0, stdout="grok-4.5\nGemini 3.5 Flash (High)\n"),
        )
        results = run_doctor("harness")
        slots = [r.slot for r in results]
        assert "kimi" in slots and "boss" in slots and "council[5]" in slots
        assert all(r.status != "fail" for r in results if r.slot.startswith("council"))

        report = format_report(results, "harness")
        assert "preset 'harness'" in report and "checks:" in report

    def test_dead_model_id_fails_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor_mod.shutil, "which", lambda b: f"/bin/{b}")
        monkeypatch.setattr(
            doctor_mod,
            "run_subprocess",
            lambda cmd, **k: _proc(0, stdout="grok-4.5\n"),  # agy list lacks the model
        )
        results = run_doctor("harness")
        gemini = next(r for r in results if r.slot == "gemini")
        assert gemini.status == "fail" and "not in" in gemini.detail

    def test_missing_provider_binary_warns_but_dead_model_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # kimi CLI absent -> the kimi PROVIDER slot warns (provider skipped);
        # a council member on the same broken spec still hard-fails.
        monkeypatch.setattr(
            doctor_mod.shutil, "which", lambda b: None if b == "kimi" else f"/bin/{b}"
        )
        monkeypatch.setattr(
            doctor_mod,
            "run_subprocess",
            lambda cmd, **k: _proc(0, stdout="grok-4.5\nGemini 3.5 Flash (High)\n"),
        )
        results = run_doctor("harness")
        kimi_slot = next(r for r in results if r.slot == "kimi")
        assert kimi_slot.status == "warn" and "not found on PATH" in kimi_slot.detail
        kimi_member = next(r for r in results if r.slot == "council[5]")
        assert kimi_member.status == "fail"


class TestCliRouting:
    def test_every_subparser_is_in_the_instant_mode_guard(self) -> None:
        # A subcommand missing from SUBCOMMANDS silently becomes a PAID
        # instant research run on its own name (this bit `doctor` live).
        import argparse

        import researchkit.cli as cli

        parser = cli.create_parser()
        sub_actions = [
            a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
        ]
        registered = set(sub_actions[0].choices)
        assert registered == set(cli.SUBCOMMANDS)

    def test_single_token_typo_refused_not_researched(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The incident: `researchkit docter` must NOT become a paid run.
        import researchkit.cli as cli

        monkeypatch.setattr(cli.sys, "argv", ["researchkit", "docter"])
        assert cli.main() == 2
        err = capsys.readouterr().err
        assert "did you mean 'doctor'" in err
        # A multi-word topic is never mistaken for a typo'd subcommand.
        monkeypatch.setattr(cli.sys, "argv", ["researchkit", "docter visits trend"])
        monkeypatch.setattr(
            cli, "cmd_instant", lambda args, service, topic: 0, raising=True
        )
        assert cli.main() == 0

    def test_cmd_doctor_exit_codes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import argparse

        import researchkit.cli as cli

        results_ok = [CheckResult("openai", "x", "ok", "fine")]
        results_bad = [CheckResult("grok", "x", "fail", "dead id")]
        monkeypatch.setattr(doctor_mod, "run_doctor", lambda p: results_ok)
        args = argparse.Namespace(preset="harness")
        assert cli.cmd_doctor(args) == 0
        monkeypatch.setattr(doctor_mod, "run_doctor", lambda p: results_bad)
        assert cli.cmd_doctor(args) == 1
