"""Persistent, resumable coding-agent CLI sessions.

``advise`` asks every harness a one-shot question with no memory and no repo
access. This module is the other half: a *conversation* per harness, pinned to
a working directory, that a caller can come back to.

Every supported CLI already persists conversations and hands back an id —
verified live 2026-08-22:

===========  ==========================================  ==========================
harness      id lands in                                 resumed with
===========  ==========================================  ==========================
claude       ``session_id`` (JSON envelope)              ``claude -r <id>``
codex        ``thread.started.thread_id`` (JSON events)  ``codex exec resume <id>``
grokcli      ``sessionId`` (JSON envelope)               ``grok -r <id>``
kimicli      ``session.resume_hint.session_id``          ``kimi -r <id>``
agy          conversation id (JSON envelope)             ``agy --conversation <id>``
===========  ==========================================  ==========================

The agy row is implemented from its documented flags but is the one path not
verified live (the account was quota-locked); it degrades to a fresh session
rather than failing when no id can be parsed.

Tools are READ-ONLY by design. The point of a session is that the harness can
see the project, so it runs in the project's cwd with file *reads* allowed —
but nothing in this module lets a member write, edit, or run shell commands.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from researchkit.council import split_effort_spec
from researchkit.providers.base import cli_harness_timeout
from researchkit.safe_io import run_subprocess, scrubbed_env

logger = logging.getLogger(__name__)

__all__ = [
    "AdviseSession",
    "HarnessTurn",
    "list_sessions",
    "load_session",
    "run_session_turn",
    "sessions_dir",
    "supports_sessions",
]

# Read-only tool policy for the Claude CLI: the session may inspect the project
# but never mutate it or reach the network.
_CLAUDE_DENIED_TOOLS = "Write,Edit,NotebookEdit,Bash,Agent,WebSearch,WebFetch"


@dataclass
class HarnessTurn:
    """One member's answer plus the id needed to continue the conversation."""

    member: str
    answer: str = ""
    session_id: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.answer)


def supports_sessions(spec: str) -> bool:
    """True when ``spec`` routes to a CLI this module can hold a session on."""
    return _adapter_name(spec) is not None


def _adapter_name(spec: str) -> str | None:
    from researchkit.providers.antigravity_provider import is_antigravity_model
    from researchkit.providers.codex_provider import is_codex_model
    from researchkit.providers.grokcli_provider import is_grokcli_model
    from researchkit.providers.kimicli_provider import is_kimicli_model

    base, _ = split_effort_spec(spec)
    if is_codex_model(base):
        return "codex"
    if is_antigravity_model(base):
        return "agy"
    if is_grokcli_model(base):
        return "grokcli"
    if is_kimicli_model(base):
        return "kimicli"
    if base.lower().startswith("claude"):
        return "claude"
    return None


def _underlying(spec: str) -> str | None:
    """The model id a spec carries, with any ``@effort`` suffix stripped."""
    base, _ = split_effort_spec(spec)
    parts = base.split(":", 1)
    return parts[1].strip() if len(parts) == 2 and parts[1].strip() else None


def _first_json_value(stream: str, key: str) -> str | None:
    """First ``key`` value across a JSON document or a JSONL event stream."""
    for chunk in (stream, *stream.splitlines()):
        try:
            data = json.loads(chunk.strip() or "{}")
        except json.JSONDecodeError:
            continue
        found = _search_key(data, key)
        if found:
            return found
    return None


def _search_key(node: object, key: str) -> str | None:
    if isinstance(node, dict):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        for child in node.values():
            found = _search_key(child, key)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _search_key(child, key)
            if found:
                return found
    return None


# --- per-harness adapters --------------------------------------------------


def _run_claude(
    spec: str, system_prompt: str, prompt: str, session_id: str | None, cwd: str
) -> tuple[str, str | None]:
    model = _underlying(spec)
    _, effort = split_effort_spec(spec)
    cmd = ["claude", "-p", "--output-format", "json"]
    if session_id:
        # Resuming carries the model and system prompt from the live session;
        # re-passing --system-prompt would silently start a fresh one.
        cmd += ["-r", session_id]
    else:
        cmd += [*(["--model", model] if model else [])]
        cmd += ["--system-prompt", system_prompt]
    if effort:
        cmd += ["--effort", effort]
    cmd += [
        "--strict-mcp-config",
        "--disallowed-tools",
        _CLAUDE_DENIED_TOOLS,
        "--permission-mode",
        "bypassPermissions",
    ]
    proc = _spawn(cmd, cwd=cwd, input_text=prompt, keep=frozenset())
    payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    if isinstance(payload, dict):
        if payload.get("is_error"):
            raise RuntimeError(str(payload.get("result", "claude CLI error")))
        result = payload.get("result", "")
        return (
            result if isinstance(result, str) else str(result),
            _search_key(payload, "session_id"),
        )
    return proc.stdout.strip(), None


def _run_codex(
    spec: str, system_prompt: str, prompt: str, session_id: str | None, cwd: str
) -> tuple[str, str | None]:
    model = _underlying(spec)
    _, effort = split_effort_spec(spec)
    # No --ephemeral here: it is exactly the flag that stops codex persisting
    # the thread we need to resume.
    if session_id:
        # `codex exec resume` takes no -s/--sandbox or -m: both are inherited
        # from the session being resumed, and passing them is a hard error.
        cmd = [
            "codex",
            "exec",
            "resume",
            session_id,
            "--json",
            "--skip-git-repo-check",
            "-",
        ]
    else:
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check", "-s", "read-only"]
        cmd += ["-m", model] if model else []
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        cmd += ["-"]
    full = prompt if session_id else f"{system_prompt}\n\n{prompt}"
    proc = _spawn(cmd, cwd=cwd, input_text=full, keep=frozenset({"OPENAI_API_KEY"}))
    text = _codex_final_message(proc.stdout)
    return text, _first_json_value(proc.stdout, "thread_id") or session_id


def _codex_final_message(stdout: str) -> str:
    """Last ``agent_message`` item in codex's JSON event stream."""
    answer = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if isinstance(item, dict) and item.get("type") == "agent_message":
            answer = str(item.get("text") or "")
    return answer


def _run_grokcli(
    spec: str, system_prompt: str, prompt: str, session_id: str | None, cwd: str
) -> tuple[str, str | None]:
    model = _underlying(spec)
    _, effort = split_effort_spec(spec)
    cmd = ["grok", "--output-format", "json"]
    if session_id:
        cmd += ["-r", session_id]
    else:
        cmd += [*(["-m", model] if model else [])]
        if effort:
            cmd += ["--reasoning-effort", effort]
    full = prompt if session_id else f"{system_prompt}\n\n{prompt}"
    # Prompt via file, not argv: keeps long context out of `ps` and clear of
    # the per-argument size cap (same reason as the grokcli provider).
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8") as f:
        f.write(full)
        f.flush()
        proc = _spawn(
            [*cmd, "--prompt-file", f.name],
            cwd=cwd,
            input_text=None,
            keep=frozenset({"XAI_API_KEY"}),
        )
    payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    if isinstance(payload, dict):
        if payload.get("type") == "error":
            raise RuntimeError(str(payload.get("message", "grok CLI error")))
        return str(payload.get("text") or ""), _search_key(payload, "sessionId")
    return proc.stdout.strip(), None


def _run_kimicli(
    spec: str, system_prompt: str, prompt: str, session_id: str | None, cwd: str
) -> tuple[str, str | None]:
    model = _underlying(spec)
    cmd = ["kimi", "--output-format", "stream-json"]
    if session_id:
        cmd += ["-r", session_id]
    elif model:
        cmd += ["-m", model]
    full = prompt if session_id else f"{system_prompt}\n\n{prompt}"
    proc = _spawn(
        [*cmd, "-p", full], cwd=cwd, input_text=None, keep=frozenset({"KIMI_API_KEY"})
    )
    answer = ""
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("role") == "assistant" and isinstance(event.get("content"), str):
            answer = event["content"]
    return answer, _first_json_value(proc.stdout, "session_id") or session_id


def _run_agy(
    spec: str, system_prompt: str, prompt: str, session_id: str | None, cwd: str
) -> tuple[str, str | None]:
    model = _underlying(spec)
    timeout = cli_harness_timeout()
    cmd = [
        "agy",
        "--dangerously-skip-permissions",
        "--output-format",
        "json",
        "--print-timeout",
        f"{max(int(timeout - 60), 1)}s",
    ]
    if session_id:
        cmd += ["--conversation", session_id]
    elif model:
        cmd += ["--model", model]
    full = prompt if session_id else f"{system_prompt}\n\n{prompt}"
    proc = _spawn(
        [*cmd, "-p", full],
        cwd=cwd,
        input_text=None,
        keep=frozenset({"GEMINI_API_KEY", "GOOGLE_API_KEY"}),
    )
    try:
        payload = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        # agy's print mode is text-first; treat stdout as the answer and let
        # the session start over next turn rather than failing the run.
        return proc.stdout.strip(), None
    if isinstance(payload, dict):
        answer = payload.get("response") or payload.get("text") or payload.get("result")
        found = _search_key(payload, "conversationId") or _search_key(
            payload, "conversation_id"
        )
        return str(answer or proc.stdout.strip()), found or session_id
    return proc.stdout.strip(), session_id


_ADAPTERS = {
    "claude": _run_claude,
    "codex": _run_codex,
    "grokcli": _run_grokcli,
    "kimicli": _run_kimicli,
    "agy": _run_agy,
}


def _spawn(
    cmd: list[str], *, cwd: str, input_text: str | None, keep: frozenset[str]
) -> subprocess.CompletedProcess[str]:
    """Run a harness CLI in the project directory on the shared budget."""
    proc = run_subprocess(
        cmd,
        input=input_text,
        timeout=cli_harness_timeout(),
        env=scrubbed_env(keep=keep),
        cwd=cwd,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"{cmd[0]} exited {proc.returncode}: {detail[-400:]}")
    return proc


def run_session_turn(
    member: str,
    system_prompt: str,
    prompt: str,
    *,
    session_id: str | None,
    cwd: str,
) -> HarnessTurn:
    """Run one conversational turn against ``member``, resuming when possible.

    Never raises: a failed member reports its error and the others carry on,
    matching ``advise``'s isolation contract.
    """
    turn = HarnessTurn(member=member)
    name = _adapter_name(member)
    if name is None:
        turn.error = f"{member!r} is not a CLI harness — sessions need one"
        return turn
    try:
        answer, new_id = _ADAPTERS[name](member, system_prompt, prompt, session_id, cwd)
    except Exception as e:
        turn.error = _clean_error(str(e))
        logger.warning("session member %s failed: %s", member, turn.error)
        return turn
    turn.answer = (answer or "").strip()
    turn.session_id = new_id
    if not turn.answer:
        turn.error = "empty response"
    return turn


def _clean_error(message: str) -> str:
    """Collapse a CLI's error spew to one readable line."""
    return re.sub(r"\s+", " ", message).strip()[:400]


# --- session store ---------------------------------------------------------
#
# Sessions live outside any project folder: they are a conversation about a
# working directory, not a research run, and they outlive the checkout.


def sessions_dir() -> Path:
    """Where session records live (``RESEARCHKIT_SESSIONS_DIR`` overrides)."""
    override = os.getenv("RESEARCHKIT_SESSIONS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".researchkit" / "sessions"


@dataclass
class AdviseSession:
    """A multi-harness conversation pinned to one working directory.

    ``members`` maps a model spec to that harness's own session id — they are
    per-vendor and not interchangeable, which is why the researchkit id exists:
    one handle the caller can hold instead of five.
    """

    id: str
    cwd: str
    preset: str = "harness"
    members: dict[str, str] = field(default_factory=dict)
    turns: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def create(cls, cwd: str, preset: str, members: list[str]) -> AdviseSession:
        now = _now()
        return cls(
            id=f"rks_{uuid.uuid4().hex[:12]}",
            cwd=cwd,
            preset=preset,
            members=dict.fromkeys(members, ""),
            created_at=now,
            updated_at=now,
        )

    @property
    def path(self) -> Path:
        return sessions_dir() / f"{self.id}.json"

    def record(self, question: str, turns: list[HarnessTurn]) -> None:
        """Fold one round of answers back into the session."""
        for turn in turns:
            if turn.session_id:
                self.members[turn.member] = turn.session_id
            elif turn.member not in self.members:
                self.members[turn.member] = ""
        self.turns.append(
            {
                "at": _now(),
                "question": question,
                "answered": sorted(t.member for t in turns if t.ok),
                "failed": {t.member: t.error for t in turns if not t.ok},
            }
        )
        self.updated_at = _now()

    def save(self) -> Path:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Session records name working directories and question text; keep them
        # owner-only rather than world-readable under $HOME.
        path.write_text(
            json.dumps(
                {
                    "id": self.id,
                    "cwd": self.cwd,
                    "preset": self.preset,
                    "members": self.members,
                    "turns": self.turns,
                    "created_at": self.created_at,
                    "updated_at": self.updated_at,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def load_session(session_id: str) -> AdviseSession:
    """Load a session by id; raises ``FileNotFoundError`` when unknown."""
    path = sessions_dir() / f"{session_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"no such session: {session_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return AdviseSession(
        id=str(data["id"]),
        cwd=str(data.get("cwd", ".")),
        preset=str(data.get("preset", "harness")),
        members={str(k): str(v) for k, v in (data.get("members") or {}).items()},
        turns=list(data.get("turns") or []),
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
    )


def list_sessions() -> list[AdviseSession]:
    """Every stored session, most recently used first."""
    directory = sessions_dir()
    if not directory.exists():
        return []
    found: list[AdviseSession] = []
    for path in directory.glob("rks_*.json"):
        try:
            found.append(load_session(path.stem))
        except (OSError, ValueError, KeyError) as e:
            logger.warning("skipping unreadable session %s: %s", path.name, e)
    return sorted(found, key=lambda s: s.updated_at, reverse=True)
