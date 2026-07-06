# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Web UI feature parity with the legacy Gradio app: boost (LLM council)
  runs, all providers pre-selected, keywords with a generate helper,
  improve-topic helper, include-raw and site-research toggles, custom URL
  sources, preset switching, and per-project outputs — article prompt,
  raw result JSON, link analytics (strict/loose), and the run log — via new
  `/api` endpoints. UI restyled orange with a collapsible project sidebar
  and an A4-width reading frame.

- Materials archive hardening (integration review + live brain QA):
  canonical citation URLs in frontmatter (tracking params/fragments dropped,
  host lowercased — the cross-run identity brainkit dedupes on), `--limit`
  bounds fetch _attempts_ (dead links can't cause unbounded fetching),
  idempotent reuse verifies the existing file's URL, junk interstitial
  titles ("Please wait…") replaced with URL-derived ones, `published:`
  passthrough when citations carry dates.
- Meta-summary token budget raised 2500→8000: thinking models spend
  reasoning tokens from the same cap, which truncated summaries mid-sentence.
- Materials archive: `--materials` flag and `researchkit materials` command
  download every cited source into `projects/<run>/materials/` as
  frontmattered markdown with a fetch manifest (SSRF-guarded, deduplicated
  incl. tracking params, old.reddit rewrite for extractable threads, binary/
  JS-shell detection, idempotent re-runs).
- Claude Code skill (`.claude/skills/researchkit`) describing the research →
  materials → brainkit pipeline.

- Core research engine ported from the pre-OSS codebase: 8 concurrent
  AI web-search providers (OpenAI, Gemini, Grok, Perplexity, Tavily, Claude,
  GitHub, GLM) with graceful degradation, cross-provider synthesis,
  LLM council + boost orchestration, Exa site research, project persistence,
  and per-run observability.
- `researchkit` CLI (instant mode, create/run/list, user sources, link
  analytics, keyword tooling).
- `researchkit-mcp`: stdio MCP server exposing `research`,
  `list_research_projects`, and `get_research_report` tools.
- `researchkit-server`: FastAPI backend with SSE progress streaming, project
  and report endpoints, and static serving of the built web UI.
- React 19 + Vite + Tailwind 4 web dashboard (`web/`): research form, live
  provider progress, rendered cited reports, past-project browser.
- `models.yaml` presets (safe defaults; deep-research/CLI-backed modes
  opt-in), `.env.example`, and a frontend CI job.
- Full project toolchain: uv packaging, ruff, mypy (strict), pytest coverage
  gate, pre-commit, CI, and tag-triggered PyPI publishing.

### Changed

- mypy runs strict globally with a shrink-only exemption list for ported
  modules; the coverage gate is set at the measured 35% baseline with a
  ratchet-up policy (see `pyproject.toml` comments).
