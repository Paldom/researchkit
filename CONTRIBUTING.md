# Contributing to researchkit

Thanks for considering a contribution. researchkit is maintained by one person
([@Paldom](https://github.com/Paldom)) on a best-effort basis — small, focused
pull requests get reviewed fastest.

## Before you start

- **Bug fixes and small improvements:** open a PR directly.
- **New features, new providers/connectors, dependency changes:** open an issue
  first and outline the idea — it saves both of us a rewritten PR. New runtime
  dependencies need explicit maintainer sign-off (see AGENTS.md).

## Development setup

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required; Node 20+ only
if you touch the web UI.

```bash
git clone https://github.com/Paldom/researchkit && cd researchkit
uv sync --all-extras
uv run pre-commit install        # hooks: ruff, mypy, prettier, hygiene
cp .env.example .env             # only needed to run live research
```

## Quality gate

Run the full gate before opening a PR — CI runs exactly this:

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest --cov -q
```

For web UI changes, also (from `web/`):

```bash
npm install && npm run lint && npm run typecheck && npm test && npm run build
```

This repo has a strictness policy ([AGENTS.md](AGENTS.md)): gates never get
loosened to make a change pass. The mypy exemption list for ported modules and
the coverage threshold in `pyproject.toml` may shrink/rise, never the reverse.
Tests use plain pytest (plus hypothesis where it pays); pre-push runs the test
suite automatically.

## Pull requests

- Keep PRs single-purpose; note behavior changes in `CHANGELOG.md`.
- Public API changes update docstrings/README in the same PR.
- No live API calls in tests — mock HTTP at the provider boundary.
- CI must be green (`all-checks-passed` is the required check).

## Security issues

Please don't open public issues for vulnerabilities — see
[SECURITY.md](SECURITY.md).
