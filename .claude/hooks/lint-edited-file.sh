#!/usr/bin/env bash
# PostToolUse hook (matcher: Edit|Write) — lint/format ONLY the file just
# edited. Exit 2 cannot undo the edit (it already happened); it feeds the
# failure back into the agent's context so it self-corrects immediately.
# Keep this sub-second: it runs synchronously on every matching tool call.
set -u

command -v jq >/dev/null 2>&1 || exit 0
FILE_PATH=$(jq -r '.tool_input.file_path // empty' 2>/dev/null) || exit 0
[ -n "$FILE_PATH" ] && [ -f "$FILE_PATH" ] || exit 0   # tool may have errored

# Venv binary directly: no uv launch overhead per edit, and a missing venv
# (fresh clone before `uv sync`) exits 0 instead of misreporting a lint error.
RUFF="${CLAUDE_PROJECT_DIR:-.}/.venv/bin/ruff"
[ -x "$RUFF" ] || exit 0

case "$FILE_PATH" in
  *.py)
    # --fix + format: auto-repair what is mechanical, report what is not.
    OUT=$("$RUFF" check --fix "$FILE_PATH" 2>&1) || {
      echo "ruff found problems in $FILE_PATH it could not auto-fix:" >&2
      echo "$OUT" >&2
      exit 2
    }
    OUT=$("$RUFF" format "$FILE_PATH" 2>&1) || {
      echo "ruff format failed on $FILE_PATH:" >&2
      echo "$OUT" >&2
      exit 2
    }
    ;;
  *)
    # Route other file types to their formatter here if wanted (taplo for
    # .toml, prettier/mdformat for .md/.yaml) — dispatch by extension, never
    # one formatter for everything.
    ;;
esac

exit 0
