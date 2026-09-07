"""Tests for persistent harness sessions and user-link extraction."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import researchkit.harness_session as hs
import researchkit.user_links as ul


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(["x"], returncode, stdout=stdout, stderr=stderr)


class TestSessionRouting:
    def test_only_cli_harnesses_support_sessions(self) -> None:
        for spec in (
            "claude:claude-opus-5@xhigh",
            "codex:gpt-5.6-sol",
            "grokcli:grok-4.6",
            "kimicli:kimi-code/k3",
            "agy:gemini-3.8-flash-high",
        ):
            assert hs.supports_sessions(spec), spec
        for spec in ("gpt-5.5", "gemini-3.8-flash", "sonar", ""):
            assert not hs.supports_sessions(spec), spec

    def test_non_harness_member_fails_without_running_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hs, "run_subprocess", lambda *a, **k: pytest.fail("must not spawn")
        )
        turn = hs.run_session_turn("gpt-5.5", "sys", "hi", session_id=None, cwd="/tmp")
        assert not turn.ok and "not a CLI harness" in (turn.error or "")

    def test_member_failure_is_isolated_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            hs, "run_subprocess", lambda *a, **k: _proc(1, stderr="boom")
        )
        turn = hs.run_session_turn(
            "codex:gpt-5.6-sol", "sys", "hi", session_id=None, cwd="/tmp"
        )
        assert not turn.ok and "boom" in (turn.error or "")


class TestSessionCommands:
    """The resume commands are the part that silently rots — pin their shape."""

    def test_codex_resume_drops_sandbox_and_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression: `codex exec resume` rejects -s/-m ("unexpected argument"),
        # which killed every codex follow-up until the flags were dropped.
        seen: list[list[str]] = []

        def fake(cmd: list[str], **kwargs: Any):
            seen.append(cmd)
            return _proc(
                0,
                stdout=json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "ok"},
                    }
                ),
            )

        monkeypatch.setattr(hs, "run_subprocess", fake)
        hs.run_session_turn(
            "codex:gpt-5.6-sol@xhigh", "sys", "hi", session_id="th_1", cwd="/tmp"
        )
        cmd = seen[0]
        assert cmd[:4] == ["codex", "exec", "resume", "th_1"]
        assert "-s" not in cmd and "-m" not in cmd

    def test_codex_start_keeps_read_only_sandbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []
        monkeypatch.setattr(
            hs,
            "run_subprocess",
            lambda cmd, **k: (seen.append(cmd), _proc(0, stdout="{}"))[1],
        )
        hs.run_session_turn(
            "codex:gpt-5.6-sol", "sys", "hi", session_id=None, cwd="/tmp"
        )
        assert seen[0][seen[0].index("-s") + 1] == "read-only"
        assert "--ephemeral" not in seen[0]  # would stop the thread persisting

    def test_claude_session_denies_write_tools(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[list[str]] = []
        monkeypatch.setattr(
            hs,
            "run_subprocess",
            lambda cmd, **k: (
                seen.append(cmd),
                _proc(0, stdout=json.dumps({"result": "hi", "session_id": "s1"})),
            )[1],
        )
        turn = hs.run_session_turn(
            "claude:claude-opus-5", "sys", "hi", session_id=None, cwd="/tmp"
        )
        assert turn.session_id == "s1"
        denied = seen[0][seen[0].index("--disallowed-tools") + 1]
        for tool in ("Write", "Edit", "Bash"):
            assert tool in denied

    def test_resuming_claude_does_not_resend_system_prompt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Re-sending --system-prompt on resume starts a NEW conversation.
        seen: list[list[str]] = []
        monkeypatch.setattr(
            hs,
            "run_subprocess",
            lambda cmd, **k: (
                seen.append(cmd),
                _proc(0, stdout=json.dumps({"result": "hi", "session_id": "s1"})),
            )[1],
        )
        hs.run_session_turn(
            "claude:claude-opus-5", "sys", "hi", session_id="s1", cwd="/tmp"
        )
        assert "--system-prompt" not in seen[0]
        assert seen[0][seen[0].index("-r") + 1] == "s1"


class TestSessionStore:
    def test_roundtrip_and_listing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RESEARCHKIT_SESSIONS_DIR", str(tmp_path))
        session = hs.AdviseSession.create("/repo", "harness", ["codex:gpt-5.6-sol"])
        session.record(
            "q1",
            [hs.HarnessTurn(member="codex:gpt-5.6-sol", answer="a", session_id="th_9")],
        )
        path = session.save()
        assert path.stat().st_mode & 0o077 == 0  # owner-only: holds cwd + questions

        loaded = hs.load_session(session.id)
        assert loaded.members["codex:gpt-5.6-sol"] == "th_9"
        assert loaded.cwd == "/repo" and len(loaded.turns) == 1
        assert [s.id for s in hs.list_sessions()] == [session.id]

    def test_unknown_session_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RESEARCHKIT_SESSIONS_DIR", str(tmp_path))
        with pytest.raises(FileNotFoundError):
            hs.load_session("rks_nope")

    def test_corrupt_record_is_skipped_not_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RESEARCHKIT_SESSIONS_DIR", str(tmp_path))
        (tmp_path / "rks_bad.json").write_text("{not json", encoding="utf-8")
        assert hs.list_sessions() == []


class TestYouTubeDetection:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&t=30s", "dQw4w9WgXcQ"),
            ("https://example.com/watch?v=dQw4w9WgXcQ", None),
            ("https://www.youtube.com/", None),
        ],
    )
    def test_video_id(self, url: str, expected: str | None) -> None:
        assert ul.youtube_video_id(url) == expected

    def test_host_matching_is_not_substring_based(self) -> None:
        # notyoutube.com must not pass as YouTube.
        assert not ul.is_youtube_url("https://notyoutube.com/watch?v=x")
        assert ul.is_youtube_url("https://youtu.be/abc")


class TestLinkFetching:
    def test_youtube_without_extractor_reports_why(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ul, "_summarize_command", lambda: None)
        monkeypatch.setattr(
            ul, "safe_fetch_text", lambda *a, **k: pytest.fail("must not fall back")
        )
        content = ul.fetch_link("https://youtu.be/dQw4w9WgXcQ")
        assert not content.ok and "summarize" in (content.error or "")

    def test_youtube_uses_summarize_and_is_marked_transcript(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ul, "_summarize_command", lambda: ["summarize"])
        monkeypatch.setattr(
            ul, "run_subprocess", lambda *a, **k: _proc(0, stdout="spoken words")
        )
        content = ul.fetch_link("https://youtu.be/dQw4w9WgXcQ")
        assert (
            content.ok and content.kind == "transcript" and content.via == "summarize"
        )

    def test_article_falls_back_to_guarded_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ul, "_summarize_command", lambda: None)
        monkeypatch.setattr(
            ul, "safe_fetch_text", lambda *a, **k: ("page text", "https://final/")
        )
        content = ul.fetch_link("https://example.com")
        assert content.ok and content.via == "fetch" and content.kind == "article"

    def test_fetch_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ul, "_summarize_command", lambda: None)

        def boom(*a: Any, **k: Any) -> None:
            raise RuntimeError("network on fire")

        monkeypatch.setattr(ul, "safe_fetch_text", boom)
        content = ul.fetch_link("https://example.com")
        assert not content.ok and "network on fire" in (content.error or "")
