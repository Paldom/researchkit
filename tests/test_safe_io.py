"""Tests for the shared safe-IO primitives (review C2, S1, S2, L18, S3, M7)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from researchkit.safe_io import (
    atomic_write_text,
    extract_urls_balanced,
    run_subprocess,
    safe_fetch_text,
    safe_join_within,
    safe_unlink_within,
    scrubbed_env,
)


class TestExtractUrlsBalanced:
    def test_keeps_balanced_parens_trims_trailing(self) -> None:
        text = (
            "See https://en.wikipedia.org/wiki/GPT-5_(model)) and "
            "(https://example.com/a). Trailing dot https://example.com/b."
        )
        assert extract_urls_balanced(text) == [
            "https://en.wikipedia.org/wiki/GPT-5_(model)",
            "https://example.com/a",
            "https://example.com/b",
        ]

    def test_dedup_order_preserved(self) -> None:
        assert extract_urls_balanced("https://a/x b https://a/x c https://b/y") == [
            "https://a/x",
            "https://b/y",
        ]

    def test_empty(self) -> None:
        assert extract_urls_balanced("") == []


class TestSafeJoinWithin:
    def test_accepts_basename(self, tmp_path: Path) -> None:
        assert safe_join_within(tmp_path, "ok.txt") == (tmp_path.resolve() / "ok.txt")

    @pytest.mark.parametrize(
        "bad", ["../x", "/etc/passwd", "a/b", "", "..", "sub/../x"]
    )
    def test_rejects_traversal(self, tmp_path: Path, bad: str) -> None:
        with pytest.raises(ValueError):
            safe_join_within(tmp_path, bad)

    def test_unlink_within(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("hi")
        assert safe_unlink_within(tmp_path, "f.txt") is True
        assert safe_unlink_within(tmp_path, "missing.txt") is False
        with pytest.raises(ValueError):
            safe_unlink_within(tmp_path, "../evil")


class TestAtomicWrite:
    def test_writes_and_leaves_no_temp(self, tmp_path: Path) -> None:
        p = tmp_path / "sub" / "cfg.json"
        atomic_write_text(p, '{"a": 1}')
        assert p.read_text() == '{"a": 1}'
        leftovers = [x for x in (tmp_path / "sub").iterdir() if x.name != "cfg.json"]
        assert leftovers == []

    def test_overwrite_is_atomic(self, tmp_path: Path) -> None:
        p = tmp_path / "x"
        atomic_write_text(p, "first")
        atomic_write_text(p, "second")
        assert p.read_text() == "second"


class TestScrubbedEnv:
    def test_removes_secrets_keeps_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-1")
        monkeypatch.setenv("XAI_API_KEY", "xai-1")
        monkeypatch.setenv("PATH", "/usr/bin")
        env = scrubbed_env(keep=frozenset({"OPENAI_API_KEY"}))
        assert "XAI_API_KEY" not in env
        assert env.get("OPENAI_API_KEY") == "sk-1"
        assert env.get("PATH") == "/usr/bin"


class TestSafeFetchTextSSRF:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/x",
            "http://localhost/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/x",
            "http://[::1]/x",
            "ftp://example.com/x",
            "file:///etc/passwd",
        ],
    )
    def test_refuses_non_public_or_bad_scheme(self, url: str) -> None:
        text, _final = safe_fetch_text(url, timeout=2.0)
        assert text is None

    def test_refuses_hostname_resolving_to_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import researchkit.safe_io as safe_io

        # Public-looking host that resolves to loopback must still be refused.
        monkeypatch.setattr(
            safe_io.socket,
            "getaddrinfo",
            lambda host, *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))],
        )
        text, _ = safe_fetch_text("http://evil.example.com/x", timeout=2.0)
        assert text is None


class TestRunSubprocess:
    def test_captures_utf8(self) -> None:
        cp = run_subprocess(["python3", "-c", "print('héllo—world 🌍')"], timeout=10)
        assert cp.returncode == 0
        assert "héllo" in cp.stdout

    def test_input_is_passed(self) -> None:
        cp = run_subprocess(
            ["python3", "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"],
            input="hello",
            timeout=10,
        )
        assert cp.stdout == "HELLO"

    def test_timeout_kills_grandchild(self) -> None:
        # Child forks a grandchild that sleeps; a timeout must kill the whole
        # process group so nothing is orphaned. (Review C2.)
        marker = f"orphan_marker_{os.getpid()}_{int(time.monotonic() * 1000)}"
        script = (
            "import subprocess, sys, time;"
            f"subprocess.Popen(['sleep', '30'], "
            f"env={{**__import__('os').environ, 'MARK': {marker!r}}});"
            "time.sleep(30)"
        )
        with pytest.raises(subprocess.TimeoutExpired):
            run_subprocess(["python3", "-c", script], timeout=1)
        time.sleep(1.0)
        # No surviving process should carry our unique marker.
        out = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
        assert out.stdout.strip() == "", f"orphaned grandchild survived: {out.stdout!r}"
