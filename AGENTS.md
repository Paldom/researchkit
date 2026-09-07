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

## Running the tool costs real money

Every live run spends. Test with the smallest thing that proves the change:

- **Prefer the harness path.** `--preset harness` / `advise` / `council` bill
  logged-in CLI subscriptions, not API keys. For pipeline smoke tests the
  keyless providers cost nothing at all — use them first:
  `--providers hackernews skillsh arxiv stackexchange openalex`
- **Minimum iterations.** One small run, then stop. Do not re-run to admire
  the output. `--days 7`, a narrow `--providers`, `--no-site-research`.
- **Every run ends in four Claude Code writers** (overview, digest, article,
  skill), concurrent, billed to the subscription. Even a keyless smoke run
  pays for those four; the brain that follows is free (stdlib brainkit).
- **Never boost to smoke-test.** `--boost` and `explore` fan out into many
  sub-runs and multiply cost by the fan-out. They are for real research, not
  verification.
- **Ten runs at once, machine-wide.** Every run holds one of
  `RESEARCHKIT_MAX_CONCURRENT_RUNS` (default 10) cross-process slots. Over the
  cap a run waits, then fails — it does not queue forever silently. Check what
  is spending with `researchkit runs`; a boosted run takes one slot per
  sub-query.
- **researchkit never invokes researchkit.** If you are a coding-agent CLI that
  researchkit spawned (`RESEARCHKIT_NESTED` is in your env), do not shell out to
  `researchkit`/`uv run researchkit` — answer from your own web search. Every
  entrypoint refuses that call anyway (exit 2): each nested run starts more
  agents that do the same, which is exponential, not a fan-out.
- **Verify what actually ran, not what you typed.** `provider_models` in
  `result.json` names the resolved model per slot. A plain id (`grok-4.6`)
  means the API billed you; `grokcli:grok-4.6` means the subscription did.
  Check it before claiming a run was free.
- Some providers bill **per source**, not per token, and a boosted run can
  fan out into many sub-runs. Cost scales with the fan-out, not with how
  long you waited.

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
