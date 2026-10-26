"""Finish every article with the owner's skills: /deslop, then /illustrate.

The owner's standing rule (2026-10-26): every article goes through
``/deslop`` (github.com/Paldom/noslop) and ``/illustrate``
(github.com/Paldom/diagram-skills), with the skills checked for updates
first. Both are Claude Code skills that run scripts (the noslop lint and
verifier; the diagram brief, draw and review engines), so they need a Claude
Code session with tools, unlike the tool-less writers.

Containment, because the article is built from untrusted web content:

- The session runs in an empty temporary directory holding only
  ``article.md``; nothing else of the project or the machine is in reach.
- Tools are an explicit allowlist (read, write and edit files, skills,
  subagents, and Bash limited to the interpreters the skills' scripts use).
  No web tools, no MCP servers, no hooks, no permission bypass.
- Billing is the logged-in Claude subscription (API routes stripped).

``/deslop`` may only change wording: if any citation anchor, link URL or
number differs afterwards, the original article is kept. The illustration
is copied to ``article-assets/`` and linked under the article's title. A
missing skill, a failed update, or a failed session never costs the
article; it is recorded and the run continues.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from researchkit.providers.base import CLAUDE_HEADLESS_ARGS, cli_harness_timeout
from researchkit.safe_io import claude_subscription_env, run_subprocess

logger = logging.getLogger(__name__)

# The two skill families and every sibling their entry points call.
SKILLS = (
    "deslop",
    "deslop-verify",
    "slop-lint",
    "illustrate",
    "diagram-brief",
    "illustration-draw",
    "mermaid-draw",
    "diagram-review",
    "diagram-design-system",
    "diagram-animate",
)
ASSETS_DIRNAME = "article-assets"
_ALLOWED_TOOLS = (
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Skill",
    "Task",
    "Agent",
    "Bash(python3:*)",
    "Bash(node:*)",
    "Bash(npx:*)",
)
_DENIED_TOOLS = "WebSearch,WebFetch"
_UPDATE_TIMEOUT_S = 180.0
_SKILL_DIRS = (Path.home() / ".agents" / "skills", Path.home() / ".claude" / "skills")
_ANCHOR = re.compile(r"\[\d{1,3}(?:\s*[,;]\s*\d{1,3})*\]")
_URL = re.compile(r"https?://[^\s)\]>\"']+")
_NUMBER = re.compile(r"\d[\d,.]*%?")

_update_lock = threading.Lock()
_update_result: list[str | None] = []  # set once per process


@dataclass
class Finished:
    """The article after the finishing pass, plus what happened."""

    markdown: str
    deslopped: bool = False
    illustrations: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # slop-lint before/after ({"score", "band"}): a clean article is left
    # untouched by design, and this says so.
    lint: dict[str, dict[str, Any]] = field(default_factory=dict)


def enabled() -> bool:
    """On unless ``RESEARCHKIT_ARTICLE_FINISH=0`` (tests; offline runs)."""
    import os

    return os.getenv("RESEARCHKIT_ARTICLE_FINISH", "1").strip() not in (
        "0",
        "false",
        "no",
    )


def missing_skills() -> list[str]:
    """Skills of the two families that are not installed for Claude Code."""
    return [
        name
        for name in SKILLS
        if not any((d / name / "SKILL.md").is_file() for d in _SKILL_DIRS)
    ]


def update_skills(
    run: Callable[..., Any] = run_subprocess,
) -> str | None:
    """Update both skill families to their latest versions, once per process.

    Returns a warning when the update could not run (offline, no npx); the
    installed versions are used then. Never raises.
    """
    with _update_lock:
        if _update_result:
            return _update_result[0]
        warning: str | None = None
        try:
            proc = run(
                ["npx", "--yes", "skills", "update", "-g", "-y", *SKILLS],
                timeout=_UPDATE_TIMEOUT_S,
                cwd=tempfile.gettempdir(),
            )
            if proc.returncode != 0:
                warning = f"skills update failed (exit {proc.returncode})"
        except Exception as e:  # offline, npx missing: use what is installed
            warning = f"skills update failed: {e}"
        _update_result.append(warning)
        return warning


def _fingerprint(text: str) -> tuple[Counter[str], Counter[str], Counter[str]]:
    """What deslop must not change: anchors, link URLs, numbers."""
    return (
        Counter(_ANCHOR.findall(text)),
        Counter(u.rstrip(".,;:") for u in _URL.findall(text)),
        Counter(_NUMBER.findall(_URL.sub("", _ANCHOR.sub("", text)))),
    )


def _claude(
    prompt: str,
    workdir: Path,
    *,
    model: str,
    max_budget: float,
    run: Callable[..., Any],
) -> str:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        model,
        *CLAUDE_HEADLESS_ARGS,
        "--strict-mcp-config",
        "--no-session-persistence",
        "--allowed-tools",
        ",".join(_ALLOWED_TOOLS),
        "--disallowed-tools",
        _DENIED_TOOLS,
        "--permission-mode",
        "acceptEdits",
        "--output-format",
        "json",
        "--max-budget-usd",
        str(max_budget),
    ]
    proc = run(
        cmd,
        timeout=cli_harness_timeout(),
        env=claude_subscription_env(),
        cwd=str(workdir),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "")[-300:].strip())
    try:
        return str(json.loads(proc.stdout).get("result") or "")
    except ValueError:
        return str(proc.stdout)


def _lint(path: Path, run: Callable[..., Any]) -> dict[str, Any] | None:
    """slop-lint's score and band for ``path`` (deterministic, no model)."""
    for root in _SKILL_DIRS:
        script = root / "slop-lint" / "scripts" / "slop_lint.py"
        if script.is_file():
            break
    else:
        return None
    try:
        proc = run(
            ["python3", str(script), str(path), "--genre", "technical", "--json"],
            timeout=120.0,
        )
        data = json.loads(proc.stdout)
    except Exception:
        return None
    row = data[0] if isinstance(data, list) and data else data
    if not isinstance(row, dict):
        return None
    return {"score": row.get("score"), "band": row.get("band")}


def _insert_figures(markdown: str, figures: list[tuple[str, str]]) -> str:
    """Link each figure right under the article's H1 (or at the top)."""
    block = "\n".join(f"![{alt}]({path})" for alt, path in figures)
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return "\n".join([*lines[: i + 1], "", block, *lines[i + 1 :]])
    return f"{block}\n\n{markdown}"


def finish_article(
    article: str,
    *,
    title: str,
    project_dir: Path,
    model: str,
    max_budget: float,
    emit: Callable[[dict[str, Any]], None] = lambda evt: None,
    run: Callable[..., Any] = run_subprocess,
) -> Finished:
    """Run /deslop and /illustrate on ``article``; never raises."""
    out = Finished(markdown=article)
    if warning := update_skills(run):
        out.notes.append(warning)
    if missing := missing_skills():
        out.notes.append(
            "skills not installed: "
            + ", ".join(missing)
            + " (npx skills add Paldom/noslop Paldom/diagram-skills -g)"
        )
        return out

    with tempfile.TemporaryDirectory(prefix="rk-article-") as tmp:
        work = Path(tmp)
        (work / "article.md").write_text(article, encoding="utf-8")

        if before := _lint(work / "article.md", run):
            out.lint["before"] = before
        emit({"stage": "article_deslop", "message": "Deslopping the article"})
        try:
            _claude(
                "/deslop article.md — genre: technical. Edit article.md in place. "
                "Keep every [n] citation anchor, link URL, number and quote "
                "exactly as written.",
                work,
                model=model,
                max_budget=max_budget,
                run=run,
            )
            edited = (work / "article.md").read_text(encoding="utf-8")
            if _fingerprint(edited) != _fingerprint(article):
                out.notes.append(
                    "deslop changed a citation, link or number; original kept"
                )
            elif edited.strip() and edited != article:
                out.markdown = edited
                out.deslopped = True
            if after := _lint(work / "article.md", run):
                out.lint["after"] = after
        except Exception as e:
            out.notes.append(f"deslop failed: {e}")

        emit({"stage": "article_illustrate", "message": "Illustrating the article"})
        try:
            _claude(
                f"/illustrate {title}. Destination: the illustration of a "
                "Medium-style article (article.md in this directory is the "
                "text; read it for the takeaway). Write every output under "
                "this directory.",
                work,
                model=model,
                max_budget=max_budget,
                run=run,
            )
            # The newest SVG is the reviewed final; earlier ones are drafts.
            svgs = sorted(
                (p for p in work.rglob("*.svg") if "node_modules" not in p.parts),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:1]
        except Exception as e:
            out.notes.append(f"illustrate failed: {e}")
            svgs = []
        if svgs:
            assets = project_dir / ASSETS_DIRNAME
            assets.mkdir(parents=True, exist_ok=True)
            figures: list[tuple[str, str]] = []
            for svg in svgs:
                shutil.copyfile(svg, assets / svg.name)
                out.illustrations.append(f"{ASSETS_DIRNAME}/{svg.name}")
                figures.append((title, f"{ASSETS_DIRNAME}/{svg.name}"))
            out.markdown = _insert_figures(out.markdown, figures)
        elif not any(n.startswith("illustrate failed") for n in out.notes):
            out.notes.append("illustrate produced no SVG")

    for note in out.notes:
        logger.warning("article finishing: %s", note)
    return out
