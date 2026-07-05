# AGENTS.md

researchkit: define a topic, research everywhere with an LLM council —
parallel AI web search synthesized into one cited report. Greenfield typed
Python library, src layout (`src/researchkit`), tests in `tests/`,
Python >=3.11, managed entirely by uv.

## Commands

- Install/sync: `uv sync`
- Lint: `uv run ruff check .` (auto-fix: `uv run ruff check --fix .`)
- Format: `uv run ruff format .` (verify: `uv run ruff format --check .`)
- Type check: `uv run mypy src`
- Test: `uv run pytest --cov -q` (the 90% branch threshold comes from
  `[tool.coverage.report] fail_under` in pyproject.toml — the one place it lives)
- Full gate (run before calling any task done):
  `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest --cov -q`

## Environment

- Use `uv` for everything — never `pip install`, `poetry`, or bare `python`.
  Add dependencies with `uv add <pkg>` (dev: `uv add --dev <pkg>`).
- Never edit `uv.lock` by hand; it changes only via uv commands.
- Tool versions are pinned (ruff==0.15.20, mypy==2.1.0); don't work around a
  pin, update it deliberately.

## Strictness policy — exemptions only shrink

This repo is strict from day one. Never loosen a gate to get green:

- mypy runs `strict = true` on `src`. No new `# type: ignore` without a
  specific error code and a reason; never disable strict flags.
- ruff uses an explicit `select` list in `pyproject.toml`. Add rules if you
  like; never remove rules or add ignores to silence a finding you can fix.
- Coverage `fail_under = 90` with branch coverage. It may go up, never down.
- No skipping/deleting failing tests, no `|| true`, no `--no-verify`, no
  `pragma: no cover` on reachable code.

## Definition of done

- The full gate above passes locally before you present work as complete.
  Run it; do not assume.
- Public API changes update docstrings/README in the same change.
- The package ships `py.typed`; every public function stays fully annotated.

## Review boundaries

- Flag — do not silently change — anything touching dependency declarations,
  `uv.lock`, `pyproject.toml` gate config, `.github/workflows/`, or `.claude/`.
- New dependencies require explicit human sign-off; verify the package exists
  and is established before adding it.

## Conventions

- src layout: importable code only under `src/researchkit`; tests never live
  in `src`.
- `known-first-party = ["researchkit"]` — import the package absolutely, no
  relative imports across modules.
- Tests use plain pytest + hypothesis where it pays; `filterwarnings = error`
  means fix warnings, don't suppress them.
