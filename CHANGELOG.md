# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Canonical `claude:<model>` spec: the Claude slot and council members now
  follow the same `<harness>:<model>[@<effort>]` grammar as
  `codex:`/`agy:`/`grokcli:` (e.g. `claude:claude-opus-4-8@xhigh`,
  `claude:opus`); the part after the prefix goes to `claude --model`
  verbatim. Bare `claude-*` ids keep working as the legacy spelling. All
  shipped presets migrated, and a new [models.yaml guide](docs/models-guide.md)
  documents presets, slots, the spec grammar, and the `@<effort>` suffix.
- Agent Skills for the harness mode: `advise`, `council`, and `explore`
  ship as skills under `.claude/skills/` (installable into 70+ agents via
  `npx skills add Paldom/researchkit`), wrapping the CLI commands with the
  same no-API-keys guarantee. Authored to the skill-authoring standard:
  one-line trigger-rich descriptions with anti-triggers, when-NOT-to-use
  sections, and concrete command workflows.
- Subscription-only harness mode (no API keys): new `advise` (every
  logged-in CLI harness answers the same question), `council` (lensed
  advisory deliberation with boss synthesis, convergence, and dissent — per
  the llm-council-harness pattern), and `explore` (boost-mode research on
  CLI providers only) commands, plus a "Harness only" toggle in the web UI.
  A new `harness` preset in models.yaml carries the defaults (Claude Opus
  4.8 @xhigh, codex:gpt-5.6-sol @xhigh, agy:gemini-3.5-flash @high,
  grokcli:grok-build); council member specs accept a `@<effort>` suffix,
  and the summarizer slot accepts CLI-backed specs so even the meta-summary
  runs keyless. The council's backend router is now module-level
  (`complete_via_spec`), shared by council, advise, consult, and the
  CLI-routed summarizer.

- Grok CLI backend: `grok: grokcli` (or `grokcli:<model>`) in `models.yaml`
  routes the Grok slot through xAI's Grok CLI headless mode (`grok -p`) —
  grok.com-subscription billing via `grok login` as an alternative to
  XAI_API_KEY, mirroring `codex:`/`agy:`. Web search stays on; runs are
  hardened with the CLI's kernel-enforced read-only sandbox, denied
  side-effect tools (shell, edits, subagents), a neutral cwd, and a
  scrubbed environment. Council members accept the same spec.

- Research→brain seam (field feedback, 2026-07-08): `--boost --materials`
  now archives cited sources per sub-project (it previously archived
  nothing), `--materials-limit` controls the fetch cap on run/instant (the
  summary line says when the cap truncated and how to lift it), and
  `--ingest <brain-dir>` hands the finished project to brainkit in one shot
  (soft dependency — prints the manual command when brainkit isn't
  installed; ingest errors never mask the report and propagate to the exit
  code; materials/ingest are skipped when every provider failed).
- Boost liveness: compact `[boost]` heartbeat lines on stderr (council
  stages, per-sub-project provider completions) even without `--verbose`,
  so long fan-out runs are distinguishable from hangs.
- `RESEARCHKIT_PROJECTS_DIR` env var for the projects directory, and an
  absolute `wrote: <path>` line at the end of every run — output is
  findable when invoked via `uv run --directory`.

- Exa registered as a first-class research provider (Tavily-symmetric
  neural search with social/web dual queries) alongside its site-research
  connector; site research now defaults to every active connector, and the
  web UI gained a config-driven site picker.
- **Plugin system**: research providers and site-research connectors are
  now plugins — the built-ins register through the same registry external
  plugins use. Plugins are normal packages exposing a
  `researchkit.plugins` entry point; activation is key-based (install +
  set the declared API key), with per-plugin quarantine, API-version
  handshake, collision rejection, provenance in `result.json`, and
  `RESEARCHKIT_NO_PLUGINS` / `RESEARCHKIT_PLUGINS` rails. New
  `researchkit plugins` command and a README plugin overview +
  development guide. Materials archives connector-provided content
  (`SiteItem.content`) directly; connectors gained generic
  `search_batch` / `summarize_batch` / `popularity_label` hooks (exa's
  special-casing removed); CLI/API/UI provider and site lists are
  registry-driven; presets accept plugin model keys and a `plugins:`
  options block; CI guards core manifests against plugin deps.

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

### Fixed

- `models.yaml` is now found from any working directory (the src-layout
  fallback looked one level too shallow, so runs launched outside the repo
  cwd silently fell back to built-in presets).
- Council/keyword JSON parsing: one shared tolerant extractor (fence
  stripping, prose preamble scan, truncated-output repair) replaces the
  greedy-regex keyword parser and recovers the mid-array truncations
  observed in live boosted runs instead of dropping the work.

### Changed

- mypy runs strict globally with a shrink-only exemption list for ported
  modules; the coverage gate is set at the measured 35% baseline with a
  ratchet-up policy (see `pyproject.toml` comments).
