#!/usr/bin/env bash
# Stop hook — refuse to end the turn while the repo's real quality gate fails.
# Exit 2 on Stop means "keep working"; the harness force-overrides after 8
# consecutive blocks. The stop_hook_active guard below is MANDATORY — without
# it this hook loops the first time the agent cannot immediately fix a failure.
set -u

command -v jq >/dev/null 2>&1 || exit 0
INPUT=$(cat)
ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null) || exit 0
[ "$ACTIVE" = "true" ] && exit 0   # already re-running because of us — let go

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

# The project's real quality gate — same commands as CI. Fail fast, report last.
# --no-sync: use the existing venv; hooks must never resolve/install.
# Coverage threshold comes from pyproject [tool.coverage.report] fail_under —
# no CLI override, so there is exactly one place the number lives.
run_gate() {
  uv run --no-sync ruff check . \
    && uv run --no-sync ruff format --check . \
    && uv run --no-sync mypy src \
    && uv run --no-sync pytest --cov -q
}

OUT=$(run_gate 2>&1) || {
  echo "Verification failed (ruff check / ruff format --check / mypy src / pytest --cov). Fix before finishing:" >&2
  echo "$OUT" | tail -30 >&2
  exit 2
}

exit 0
