"""Tests for the shared safe-IO primitives (review C2, S1, S2, L18, S3, M7)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from researchkit.safe_io import (
    CLAUDE_API_ROUTE_ENV_KEYS,
    NESTED_ENV_VAR,
    atomic_write_text,
    claude_subscription_env,
    extract_urls_balanced,
    guard_no_nested_run,
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


class TestClaudeSubscriptionEnv:
    def test_strips_every_api_route_keeps_oauth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in CLAUDE_API_ROUTE_ENV_KEYS:
            monkeypatch.setenv(key, "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sub-token")
        env = claude_subscription_env()
        assert not CLAUDE_API_ROUTE_ENV_KEYS & env.keys()
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sub-token"
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"

    def test_filters_a_given_base(self) -> None:
        env = claude_subscription_env({"ANTHROPIC_AUTH_TOKEN": "t", "PATH": "/b"})
        assert env == {"PATH": "/b", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


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


class TestSafeFetchResult:
    """PLAN 2.2: the fetch reports what happened, not just text-or-None."""

    def _serve(self, monkeypatch: pytest.MonkeyPatch, responses: list[object]) -> None:
        import researchkit.safe_io as safe_io

        monkeypatch.setattr(safe_io, "_host_is_public", lambda host: True)
        queue = list(responses)

        class _Resp:
            def __init__(
                self, status: int, headers: dict[str, str], body: bytes
            ) -> None:
                self.status_code, self.headers, self._body = status, headers, body
                self.encoding = "utf-8"

            def iter_content(self, n: int) -> object:
                for i in range(0, len(self._body), n):
                    yield self._body[i : i + n]

            def close(self) -> None:
                pass

        class _Session:
            trust_env = True

            def get(self, url: str, **kw: object) -> _Resp:
                return _Resp(*queue.pop(0))  # type: ignore[arg-type]

            def close(self) -> None:
                pass

        monkeypatch.setattr(safe_io.requests, "Session", _Session)

    def test_reports_status_type_and_truncation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from researchkit.safe_io import safe_fetch

        self._serve(
            monkeypatch,
            [(200, {"Content-Type": "application/pdf; x=1"}, b"%PDF" + b"x" * 50)],
        )
        r = safe_fetch("https://example.com/a.pdf", max_bytes=20)
        assert r.status == 200 and r.content_type == "application/pdf"
        assert r.truncated and r.body.startswith(b"%PDF") and len(r.body) == 20

    def test_reports_http_errors_and_redirect_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from researchkit.safe_io import safe_fetch

        self._serve(
            monkeypatch,
            [
                (301, {"Location": "/moved"}, b""),
                (404, {"Content-Type": "text/html"}, b""),
            ],
        )
        r = safe_fetch("https://example.com/a")
        assert r.text is None and r.status == 404 and r.error == "http 404"
        assert r.final_url == "https://example.com/moved"


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


class TestGuardNoNestedRun:
    """The recursion guard: researchkit must refuse to re-enter itself.

    A coding-agent CLI that researchkit spawned once shelled out to
    `researchkit explore` on its own, and each resulting run spawned more
    agents that did the same — ~200 runs from one topic in 45 minutes.
    """

    @staticmethod
    def _isolated_environ(monkeypatch: pytest.MonkeyPatch) -> None:
        """Swap in a throwaway environ — the guard writes to the real one."""
        monkeypatch.setattr(
            os, "environ", {k: v for k, v in os.environ.items() if k != NESTED_ENV_VAR}
        )

    def test_allows_a_top_level_run_and_marks_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._isolated_environ(monkeypatch)
        assert guard_no_nested_run() is None
        # The marker must land in os.environ so EVERY child env built from it
        # (scrubbed_env or a {**os.environ} copy) carries it down.
        assert os.environ[NESTED_ENV_VAR] == str(os.getpid())
        assert scrubbed_env()[NESTED_ENV_VAR] == str(os.getpid())

    def test_same_process_reentry_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Calling an entrypoint twice in one process (tests, library use) is not
        # the recursion we are blocking — only a child crossing a process is.
        self._isolated_environ(monkeypatch)
        assert guard_no_nested_run() is None
        assert guard_no_nested_run() is None

    def test_refuses_when_marker_names_another_process(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(NESTED_ENV_VAR, str(os.getpid() + 1))
        message = guard_no_nested_run()
        assert message is not None
        assert NESTED_ENV_VAR in message

    @pytest.mark.parametrize("explicit_env", [False, True])
    def test_run_subprocess_stamps_every_child(self, explicit_env: bool) -> None:
        # The stamp must happen at the spawn site, not at an entrypoint: it has
        # to hold when researchkit runs under `uvicorn --factory`, MCP stdio, or
        # as an imported library, where no main() ever claimed the marker.
        cp = run_subprocess(
            [sys.executable, "-c", f"import os; print(os.environ['{NESTED_ENV_VAR}'])"],
            timeout=60,
            env={"PATH": os.environ["PATH"]} if explicit_env else None,
        )
        assert cp.stdout.strip() == str(os.getpid()), cp.stderr

    def test_a_real_child_inheriting_the_marker_refuses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The mechanism end to end: claim the marker, then hand a spawned agent
        # CLI exactly the env it would get — that child must refuse to re-enter.
        self._isolated_environ(monkeypatch)
        assert guard_no_nested_run() is None
        cp = run_subprocess(
            [
                sys.executable,
                "-c",
                "from researchkit.safe_io import guard_no_nested_run;"
                "print(guard_no_nested_run() is not None)",
            ],
            timeout=60,
            env=scrubbed_env(),
        )
        assert cp.stdout.strip() == "True", cp.stderr

    def test_cli_main_exits_nonzero_when_nested(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from researchkit.cli import main

        monkeypatch.setenv(NESTED_ENV_VAR, str(os.getpid() + 1))
        monkeypatch.setattr("sys.argv", ["researchkit", "explore", "some topic"])
        assert main() == 2
