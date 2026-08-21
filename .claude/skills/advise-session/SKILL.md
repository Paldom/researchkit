---
name: advise-session
description: Opens a persistent, project-aware session with every logged-in coding-agent CLI (Claude Code, Codex, Grok CLI, Kimi Code, Antigravity), returns one session id, and lets you follow up with full memory of the conversation and read-only access to the repo — subscription auth, zero API keys. Use for a multi-turn second opinion, "keep asking the other models", "follow up with them", "ask them about this codebase", or when one question will lead to another. Not for a single throwaway question (use advise) or a synthesized verdict (use council).
argument-hint: "<question> [--session <id>]"
---

# advise-session — a conversation with every harness, not a one-shot

## Purpose

`/advise` asks each harness one question from a cold start: no memory, no repo.
This opens a real session per harness instead. Each one can **read the project**
and **remember the last turn**, so you can drill in — "why?", "what about the
error path?", "you said X, show me where" — instead of re-pasting context.

You get back one researchkit session id. Hold that; it maps to the five
per-vendor session ids so you never touch them.

## When to use / when NOT to use

Use when the question has follow-ups, or when the answer depends on this
codebase. NOT for: a single self-contained question (`/advise` is cheaper), one
decisive verdict (`/council`), or web research (`/explore`).

## Workflow

1. Open a session. Run from the repo you want the harnesses to see:

   ```bash
   uv run researchkit advise-session "Is the retry policy in network_retry.py sound?"
   ```

   The last stdout line is `session_id: rks_…`. **Keep it** — it is the handle
   for every follow-up.

2. Follow up as many times as you like:

   ```bash
   uv run researchkit advise-session "What breaks if I drop the delay cap?" --session rks_…
   ```

3. List what you have open:

   ```bash
   uv run researchkit advise-session --list
   ```

Useful flags: `--cwd <dir>` points the session at another project, `--harnesses`
overrides the members, `--context-file notes.md` appends context to a turn.

## What the harnesses can and cannot do

Read-only, deliberately. They may read, glob and grep the project; they cannot
write, edit, or run shell commands. Say "read X before answering" when you want
grounding — they will, and a good answer usually says which files it looked at.

## Failure modes

- A member that is not installed, not logged in, or out of quota fails alone and
  prints its reason; the others continue. Exit code is 1 only when all fail.
- A member that fails mid-session keeps its old session id and rejoins on the
  next turn — one bad turn does not end the conversation.
- Only CLI harnesses can hold a session. Plain API model ids in a preset are
  skipped with a notice.
- Sessions are stored under `~/.researchkit/sessions/` (override with
  `RESEARCHKIT_SESSIONS_DIR`), owner-readable only, and outlive the checkout.
- Expect the same per-turn cost as `/advise` — 1–5 minutes, slowest member
  dominating. Each harness call gets a 30-minute budget.
