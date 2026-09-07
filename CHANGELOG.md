# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `advise-session` — `advise` with a memory. Each harness answers inside a
  persistent conversation pinned to a working directory, with read-only
  access to it, and the command returns one researchkit session id the
  caller holds instead of five per-vendor ids. Follow up with
  `--session <id>`; `--list` shows what is open. Every CLI's resume path was
  verified live (`claude -r`, `codex exec resume`, `grok -r`, `kimi -r`,
  `agy --conversation`). Ships as an Agent Skill alongside the others.
- Two more keyless providers: **arXiv** (`--providers arxiv`) for preprints,
  which land months before peer review indexes them, and **Stack Exchange**
  (`--providers stackexchange`) for vote-scored practitioner Q&A. Reddit and
  Polymarket were evaluated and deliberately left out: Reddit's JSON API now
  returns 403 without OAuth (it belongs in the browser-session plugin, not a
  keyless provider), and Polymarket was unreachable from here, so shipping it
  would have meant shipping something unverified.
- Engagement-ranked providers now say so in their own output: points and
  install counts measure attention and adoption, never correctness. Taken
  from an `explore` run's own finding that link validity (>94%) and relevance
  (>80%) diverge sharply from claim-level factual support (39–77%).
- Two keyless providers, both scored by engagement rather than relevance —
  the idea worth taking from `mvanhorn/last30days-skill`. **Hacker News**
  (`--providers hackernews`) ranks windowed stories by points, citing the
  thread and the submitted article separately. **skills.sh**
  (`--providers skillsh`) ranks the Agent Skills directory by real install
  counts, self-gating to "not relevant" the way OpenAlex does.
- `run --since-last`: an update run that narrows the window to the time
  elapsed since the previous run and archives the old report and result
  under `history/`, so successive runs are diffable. The narrowed window is
  in-memory only — a project's declared window is not a property of one
  update.
- `--clone-repos`: shallow-clones the GitHub/skills.sh repositories a run
  cited into `repos/` and writes `repos/SUMMARY.md`. A cited repo archived
  as an HTML page loses the one thing worth having; deep links collapse to
  their repo, non-repo namespaces are rejected, and a repo that will not
  clone is recorded with its reason.
- `explore` now runs every keyless provider (Hacker News, skills.sh, arXiv,
  Stack Exchange, OpenAlex) alongside the five CLI harnesses — they need no
  API key, so the zero-key guarantee holds — and accepts `--providers` /
  `--clone-repos`. The web UI's "Harness only" toggle was widened to the
  same set, so the toggle and the CLI now produce the same research.
- `advise --max` / `advise-session --max` select each harness's flagship at
  its top reasoning tier (the `advise-max` set), defined once in
  `MAX_EFFORT_HARNESSES` so the two commands and the skill cannot drift.
  Not the default for sessions: at `@max`/`@ultra` one turn can run past ten
  minutes, and a session exists to be asked several questions.
- `repos/SUMMARY.md` now opens with a model-written section on what the
  cloned repositories contribute _to this topic_; the structural facts say
  what a repo is, not why it mattered. Falls back to the structural summary
  alone when the model is unavailable.

### Changed

- Gemini Flash pins moved from 3.7 to 3.8 everywhere: `gemini-3.8-flash` on the
  API slots (provider, meta-summarizer, council member) and
  `agy:gemini-3.8-flash-high` on the Antigravity harness. Both ids verified
  against the live API model list and `agy models` on 2026-09-07. The
  high-volume `gemini-3.5-flash-lite` slots stay: no 3.8 Flash-Lite exists.
- **Materials are archived by default** (`--no-materials` opts out). The
  research pack is what brainkit ingests and skillskit skillifies, and the
  archive is the part that survives link rot; making the artifact the
  default costs one polite fetch pass over at most 25 cited sources.
- User-supplied links are read, not just listed. A URL added with
  `add-source` used to be metadata — the article was told the link existed
  and nothing ever fetched it, so a YouTube link was worth its title.
  Links are now extracted into `user_sources/` and flow into the final
  article keyed by URL (staying citable, unlike background documents).
  YouTube goes through `@steipete/summarize`, falling back to `npx -y` so
  transcripts work with no install; other pages use the SSRF-guarded
  fetcher.
- Prompts cut against a four-harness audit: persona openers, ceremonial
  footers, restated task lists and empty quality talk deleted; the
  professional-overview pair rewritten (it asked for "concise but
  comprehensive" and then "do not sacrifice completeness for brevity" —
  completeness won, and every report inherited it); lower-bounded ranges
  turned into caps, since a model reads "3-7 bullets" as a quota of 7. Two
  real bugs went with them: "extend to 30 days" shrank the window whenever
  `days > 30`, and the digest named Exa as a required input even in runs
  without it. Every citation, injection-fence, JSON-schema and
  Weakest-Evidence contract the audit called load-bearing was kept.

- Model defaults tracked forward to current models across every preset,
  in-code fallback, skill and doc (each id verified live against the
  provider's model list or a smoke call on 2026-08-21): Gemini 3.7 Flash,
  Claude Opus 5 / Sonnet 5, Grok 4.6, GLM 5.3, Kimi K3, and
  `gemini-3.5-flash-lite` for the high-volume site-summarizer slot. Two
  pins were not merely stale but dead: `glm-4.7-flashx` is gone from z.ai
  (now `glm-5-turbo`), and Antigravity switched `agy models` from bare
  display names to `<id>\t<display name>`, so the harness preset's
  `"agy:Gemini 3.5 Flash (High)"` no longer resolved — it is now
  `agy:gemini-3.7-flash-high`. The Grok CLI's effort ceiling also rose from
  `high` to `xhigh`, which `/advise-max` now uses.

  In-code provider class defaults (the fallback when nothing configures a
  slot) now follow one rule — mirror the shipped `default` preset — instead
  of each drifting on its own. That deliberately re-tiers three of them:
  Gemini `gemini-3.1-pro-preview` -> `gemini-3.7-flash`, Claude
  `claude-opus-4-7` -> `claude-sonnet-5`, Grok `grok-4-1-fast` ->
  `grok-4.6`. Anyone constructing a provider directly without a preset gets
  the documented default rather than a stale surprise.

### Fixed

- Harness CLI calls no longer die to the stopwatch mid-answer. Every
  coding-agent CLI carried its own floor (codex 300s, grokcli/kimicli 300s,
  agy 600s, claude 300s in the council path and 120s for keyword/summary
  calls), each derived from `provider_http_timeout()` — an HTTP notion
  applied to a subprocess that reasons for minutes. A dense five-question
  prompt at the top effort tier was measured past 10 minutes on the Codex
  CLI, so 300s killed that member three retries deep while the other four
  answered. All five paths now take one budget from
  `cli_harness_timeout()`, and agy's own `--print-timeout` is derived from
  that budget (always strictly below it) instead of a hardcoded `9m`, so it
  can no longer self-kill first. Two budgets, not one: 1800s for a full
  answer, 300s (`CLI_QUICK_TIMEOUT`) for short pipeline steps like keyword
  generation and per-provider summaries, which already degrade gracefully
  and shouldn't stall a run for half an hour. `RESEARCHKIT_CLI_TIMEOUT`
  overrides both and wins over `PROVIDER_HTTP_TIMEOUT` (which still raises,
  never lowers, the default when the new knob is unset); non-positive or
  non-finite values fall back. A wall-clock timeout is no longer retried on
  the Claude paths — at a 30-minute budget, retrying only multiplies the
  stall. The Kimi research floor rose 420s -> 900s, and the final-summary
  CLI call joined the shared budget (it had its own fixed 300s).

### Added

- Codex support at Claude-Code parity: the repo's skills (advise,
  advise-max, council, explore, researchkit) are discoverable by Codex and
  every Agent-Skills-standard agent via `.agents/skills/` symlinks (one
  source of truth in `.claude/skills/`); AGENTS.md was already the shared
  instruction file. Verified live with `codex exec` in-checkout.

- Trio unification seams (flagship cross-review, 2026-07-21): the research
  pack is now a documented interchange contract
  ([docs/research-pack.md](docs/research-pack.md) v1 — layout, frontmatter
  keys, run-complete gate, chunking rules, additive evolution), and every
  finished run prints the fork as runnable next steps (brainkit ingest /
  /skill-from-research / the contract doc) instead of leaving the journey's
  next leg as tribal knowledge.

- Brave Search provider (`--providers brave`, `BRAVE_API_KEY`): the only
  large independent English index plus its discussions vertical — result
  dates flow to materials `published`; queries are sequentially paced for
  starter-tier rate limits (cheap tier requires public attribution).
- OpenAlex provider (`--providers openalex`, keyless; `OPENALEX_MAILTO`
  optional): peer-reviewed works with DOIs, venues, citation counts, and a
  deterministic relevance self-gate — topics with no matching scholarly
  literature report "not relevant" with zero sources (which the uncited
  downgrade keeps out of the consensus) instead of padding the report.
  Lookback widens to >=1 year (scholarly publishing outpaces no news cycle).

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
