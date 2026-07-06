#!/usr/bin/env bash
# PreToolUse hook (matcher: Bash) — deny a short list of never-do commands.
# Exit 2 blocks the call; the reason on stderr is fed back to the agent.
# Scope honesty: this is an agent convenience guard, not a security boundary
# (regex guards are bypassable via aliases/functions); server-side rules and
# CI remain the real gate. Whitelist-style hooks are stronger where it matters.
set -u

command -v jq >/dev/null 2>&1 || exit 0   # never block on our own missing dep
CMD=$(jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
[ -n "$CMD" ] || exit 0

deny() { echo "$1" >&2; exit 2; }

# 1. Bare pip / python outside uv (command position only — 'uv run python',
#    'grep python' and 'which pip' stay legal)
if echo "$CMD" | grep -qE '(^|[;&|]\s*)pip3?\s+install\b'; then
  deny "Blocked: bare 'pip install' breaks the uv-managed environment. Use 'uv add <pkg>' (or 'uv add --dev <pkg>')."
fi
if echo "$CMD" | grep -qE '(^|[;&|]\s*)python3?\s+-m\s+pip\b'; then
  deny "Blocked: 'python -m pip' bypasses uv. Use 'uv add' / 'uv pip' inside the project environment."
fi

# 2. Recursive rm on repo root or home (also the tab-completed ./ ~/ forms)
if echo "$CMD" | grep -qE '(^|[;&|]\s*)rm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(/|~|\.|\*)/?(\s|$)'; then
  deny "Blocked: recursive force-delete of a broad path. Delete specific paths explicitly."
fi

exit 0
