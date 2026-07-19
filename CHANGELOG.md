# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Materials frontmatter carries a `content_digest` (sha256-16 of the body,
  written by the single shared material writer — the fetched-page path was
  routed through it too): downstream knowledge tools get content lineage
  and a second dedup key beyond the URL. Raw provider `## ` headings are
  demoted to `#### ` in report.md so instructed `## Sources` sections can't
  become junk chunks for `##`-splitting ingesters (brainkit).

- `researchkit doctor` — preflight the active (or `--preset`) configuration
  before any spend: CLI harnesses installed and logged in, pinned model ids
  still listed by `grok models` / `agy models` (whole-token/whole-line
  matching — a dead `grok-4` pin can't false-pass against `grok-4.5`), API
  keys present per slot (missing keys and missing provider CLIs warn —
  providers skip gracefully; dead model ids and pipeline/council slots hard
  fail), binary-only CLIs labeled "auth not verifiable pre-spend". No
  tokens are spent; exits 1 on hard failures. Motivated by three silent
  model-id drift breakages in one week (review + backlog P1).
- Typo guard on instant mode: a single bare token near a subcommand name
  (`researchkit docter`) now refuses with "did you mean" instead of
  silently launching a PAID research run on the typo as the topic (this
  bit `doctor` live before it was registered); multi-word topics are
  unaffected, and a regression test pins the subcommand registry to the
  registered subparsers.
- Citation-yield hardening: the codex/grokcli harness research prompts now
  request an explicit `## Sources` markdown-link list (the agy/kimi
  providers already did), and a provider that succeeds with ZERO extracted
  sources is loudly logged and flagged to the meta-summarizer via the
  TRUSTED prompt channel, outside the untrusted data block — an in-band
  note would be both ignorable under the data-not-instructions rule and
  forgeable by injected web content (advise-max validation finding).

- Kimi (Moonshot AI) provider, in both flavors. **API**: new `kimi` slot
  backed by the OpenAI-compatible Moonshot endpoint with Moonshot's
  official web-search tool — the Formula API (`web_search` tool calls
  executed via `POST /formulas/moonshot%2Fweb-search/fibers`, the
  `encrypted_output` passed back as the tool message; the deprecated
  `$web_search` builtin echo feeds the model no content and is not used);
  sources recovered from inline markdown links since Kimi returns no
  citation array, `KIMI_API_KEY`/`MOONSHOT_API_KEY` auth, `KIMI_BASE_URL` override
  for the China platform or the Kimi Code subscription endpoint
  (`api.kimi.com/coding/v1`, with automatic model-id dialect translation
  like `kimi-k3` → `k3`), and no sampling params (current Kimi models
  reject overrides). **Kimi Code CLI**: `kimicli[:<alias>]` specs route the
  kimi slot, council members, advise/council/explore, and any model slot
  through `kimi -p --output-format stream-json` on kimi.com-subscription
  billing — the fifth harness alongside claude/codex/agy/grokcli. Kimi
  usable as improver and summarizer; keys scrubbed from sibling CLI
  subprocess envs.

### Fixed

- Harness preset model ids that had gone stale: the Grok CLI no longer
  serves `grok-build` (now `grokcli:grok-4.5`) and Antigravity now names
  models by display name (`agy:Gemini 3.5 Flash (High)` — effort is part of
  the id, `@high` suffixes on bare ids are ignored). Presets, council
  defaults, docs, and skills updated; both failures previously killed those
  members in every advise/council/explore run.
- `GLM_API_KEY`/`ZHIPUAI_API_KEY` (and the new Kimi keys) added to the
  subprocess env scrub list — they were exfiltratable into research CLI
  child processes.

- Hybrid harness/API runs: every model slot now accepts CLI-backed specs —
  `site_summarizer` (exa/medium/youtube connectors summarize on the
  logged-in CLI; youtube summarizes fetched transcripts, video
  understanding stays on the Gemini API), `improver`/keyword synthesis,
  and the GitHub provider's improver, joining the summarizer and provider
  slots. New `hybrid` preset (CLI subscriptions for reasoning, API
  endpoints for breadth) and a `--preset` flag on classic create/instant
  runs. The harness preset is now fully CLI on every LLM slot, and
  `plugin_api` exposes `complete_via_spec`/`is_cli_backed_spec` so
  connector plugins can route the same way.

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
