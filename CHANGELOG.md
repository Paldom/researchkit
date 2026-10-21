# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- A bold lead-in (`**Codex drops results.** Issue…`) no longer glues itself
  and the following sentences into one `claims.jsonl` row: a sentence now
  also ends after closing markup (`**`, `*`, `_`, a quote, a bracket or a
  backtick).

- **trafilatura could abort a run.** Concurrent calls from the archive's
  fetch pool crashed lxml ("Fatal Python error: Aborted", 1 in 3
  reproductions), killing a live A/B run mid-archive with no traceback.
  Extraction is now serialized behind a lock (8/8 clean reproductions);
  the network fetches stay parallel.

- **Claims were attached to the wrong source** whenever the writer cited
  after the full stop ("…20 October 2026. [14] Next sentence…", 51 of 52
  anchors in a live run). The sentence splitter gave each anchor group to the
  _next_ sentence and left bare `[n]` as empty claims, which skewed
  `claims.jsonl`, the integrity gate and `researchkit verify` alike
  (122/152 "unsupported" on the validation run). Anchors that open a
  sentence now belong to the one before, and anchor-only fragments make no
  claim. The gate also flags sentences about the research rather than the
  topic ("Providers found…") as a `meta_talk` advisory.

- Keyword synthesis for site research always used the OpenAI client, so a
  GLM or Kimi improver model failed. A summarizer set to a non-Gemini API id
  failed in the Gemini SDK. The topic improver had no retry policy. All
  three now go through the shared dispatcher.

- The HTTP API ignored `materials: false` and `materials_limit` for the
  in-run archive, and a boosted `--no-materials` / `--materials-limit` run
  never passed either flag to its sub-projects. Both now reach every run.

- `--providers` defaulted to `openai gemini grok perplexity` on the command
  line, so `--preset harness-fast` silently ran a Codex research slot and the
  Perplexity API instead of its declared set (found by a live validation
  run). The flag now defaults to the preset's set.

- The web server substituted _every_ known provider when a request named
  none, bypassing the preset's declared set (a harness-preset request called
  Perplexity, Tavily and the rest). It now defers to the preset.

- Two runs whose long topics truncate to the same slug in the same second
  (batch runs sharing a preamble) got the same project name. A shared brain
  keys runs by name, so the second ingest pruned the first run's sources.
  Truncated slugs now carry a short hash of the full topic; short topics
  keep their exact names.

- `run_project` dropped the run status when it rebuilt the artifacts, so
  the CLI exit code on the project path always read "ok".

- `result.json`, the research pack's "run complete" marker, is now written
  _last_, after the report, article and skill. A rerun that produces no
  article or skill removes the stale one instead of leaving it next to newer
  research.
- Resume reuses only _successful_ provider partials whose model still
  matches the preset. A partial left by a quota failure or a timeout is
  re-run instead of replayed after the outage is over.
- A connector summary archived as a fallback keeps origin `summary` when it's
  reused on a later run, instead of becoming `cached` and passing as source
  text.

- **Security: the Kimi Code CLI could run shell commands from web content.**
  Headless `kimi -p` auto-approves every tool: a planted instruction ran
  `touch` on the host (verified live). Research calls now pass an
  `--agent-file` allowlist (`WebSearch`, `FetchURL`), keyword/summary calls
  get no tools at all, and `advise-session` starts Kimi as a read-only advisor
  (`Read`, `Grep`, `Glob` and web; no shell, no writes). Resumed sessions keep
  their profile (verified). Profiles ship in `researchkit/harness_profiles/`.
- Antigravity research and sessions run with `--sandbox` (terminal
  restrictions) under their auto-approve mode. Claude Code's opt-in `deep:`
  path adds `--strict-mcp-config`, so the operator's MCP servers stay out of
  a run that reads untrusted pages under `bypassPermissions`.
- The `npx` fallback for user links is pinned (`@steipete/summarize@0.25.1`)
  instead of `@latest`, which ran whatever the registry served that day.
- `tests/test_containment.py` pins every research harness's argv (tool lists,
  sandboxes, strict MCP), so loosening containment fails CI.

- Harness-mode Grok CLI calls no longer die on Docker Desktop. `grok
--sandbox read-only` refuses to start when a runtime socket such as
  `/var/run/docker.sock` is a symlink (`socket deny resolution failed`).
  The provider retries that one refusal with the `workspace` profile and
  keeps it for the rest of the run. The temp directory is already the
  working directory, so that profile's extra write root is the temp dir.
  Any other sandbox failure still aborts.

### Added

- **Experiment arms for the PLAN 3.8 and 5.4 A/Bs.** `RESEARCHKIT_DEPTH=deep`
  runs the Codex research slot with live search, `xhigh` effort and a
  rounds contract (sub-questions, search, primary pages, gaps, one
  follow-up round). Other CLIs ignore it. It becomes a preset option only
  if it wins. Measured 2026-10-21: rubric tie, claim support down, so it
  is not promoted (the council pre-pass stays: it broadens sources at a support cost). `create_and_run_boosted(decompose=False)` keeps the council
  pre-pass (improved topic and keywords) but never fans out.

- `researchkit.verify.calibration_report`: the verifier's agreement with
  human labels (overall agreement, Cohen's kappa, confusion counts). This is
  the check the verifier must pass before it may ever block a run (PLAN 4.2).

- **Research-pack manifest v2** (PLAN 2.12, brainkit contract change
  approved 2026-10-21). `materials/index.json` carries `schema_version: 2`,
  a full `sha256` per fetched file, `claims_sha256` and `complete`. The
  archive-first manifest says `complete: false` until
  `Project.save_publication` seals it after the claims, before
  `result.json`. `docs/research-pack.md` is now contract v2. brainkit
  checks the version, completeness and hashes before it writes anything.

- **Main-content extraction and PDF text in the archive** (PLAN 4.7, 2.13;
  new dependencies `trafilatura` and `pypdf`, approved 2026-10-21).
  Archived pages are now trafilatura's main content. A bake-off on 36
  recorded pages cut boilerplate from 1.37 to 0 markers per 1k words and
  nav-like lines from 56% to 16%. The stdlib extractor stays as the
  fallback when trafilatura returns under 50 words. PDFs are archived from
  their text layer (first 60 pages, re-fetched once with a 20 MB cap when
  the page cap truncated them). Scanned, encrypted or cut-off PDFs stay
  `pdf_unparsed`, cited but link-only.

- **MCP: `cancel_run` and the pack's claims and materials** (PLAN 5.2).
  `cancel_run(run_id)` stops a running explore. Every harness CLI the run
  started is registered under its run id (cancel scopes in `safe_io`, and
  pool workers now carry the run's context). Its process groups are
  killed, new CLI calls refuse to start, and what already landed stays on
  disk. `get_research_artifact` also serves `claims` (with the verifier's
  labels when present) and `materials` (the manifest, or one archived file
  by name, with path traversal refused). The connector-boundary prototype
  is deferred: no second consumer yet, so no new plugin type.

- **Provider-native citations reach the claims table** (PLAN A.2, A.9). The
  API providers now keep their structured claim→source locators when they
  read the response: OpenAI `url_citation` offsets (Responses or Chat
  shape), Gemini grounding supports (the chunk list stays index-aligned,
  and each chunk records the resolved URL), and Perplexity `[n]` markers
  into its result list. Adapters in `researchkit.citations` normalize them
  to `meta.native_citations`, and every `claims.jsonl` row carries
  `native_spans`, the provider sentences that cited its source. Offline
  parity tests replay SDK-shaped responses through each adapter and a
  frozen API-style run through publication. `docs/research-pack.md` now
  documents `claims.jsonl`, the labels file, and the newer `result.json`
  run facts.

- **Claim verifier v1: `researchkit verify <project>`** (PLAN 4.3). A
  Claude Code judge labels each `claims.jsonl` row from its archived source
  text alone: `supported`, `unsupported`, `contradicted`, or `unavailable`
  (no archive, or no verdict; never counted as support). Sources are packed
  into a few bounded calls, sentences are judged whole, and there is no
  search step because the pages are already on disk. The labels go to
  `claims.labels.jsonl` (verifier version and judge model on every row).
  Advisory only until the labels are calibrated against human judgments.

- **Rubric contract and `researchkit score`** (PLAN 4.4). `researchkit.rubric`
  holds one versioned list of binary checks (v1, nine items: facts cited,
  numbers dated, both readings on conflict, qualified single-source claims,
  primary sources, open questions, on-topic, specifics first, no filler).
  The same list goes into the writer prompts as a pre-answer self-check and
  to a Claude Code judge as its score sheet. `researchkit score <project>`
  grades a frozen `report.md` and writes `rubric.json` (version, judge
  model, verdict and reason per item). A missing verdict counts as a fail.

- **Origin folding** (PLAN 4.6). Archived sources whose text is the same
  (5-word shingle Jaccard of 0.6 or more: syndication, mirrors, scraped
  copies) share one origin. The writer's source list marks the copy
  "(same text as [n])", the prompt asks for "independently originating"
  sources, and the integrity gate fails a "corroborated" claim whose
  citations are copies of one text.

- **Opt-in connectors** (PLAN 5.1): `ConnectorSpec.default_enabled`
  (additive; `PLUGIN_API_VERSION` stays 1). A connector with
  `default_enabled=False` is installed and selectable by name but never part
  of a plain run's site research, and the web UI offers it unticked. The
  youtube, medium, substack and stealth plugins (webui, linkedin,
  medium_stealth) now ship opt-in: they log in, scrape walled sites, or
  earned under 5% of citations.

- **Depth telemetry** (PLAN 3.7). `result.json` gains a `depth` block with
  one entry per slot and branch, read from what each CLI already reports:
  Codex (searches, pages opened, cited-but-never-opened), Kimi (search and
  fetch tool calls), Grok (stop reason) and Claude (turns, server-side
  searches, stop reason). Slots whose output exposes none (API providers,
  agy) read `unknown`. A slot cut off at its deadline reports
  `stop_reason: deadline`.

- **Partial capture at the deadline** (PLAN 3.1). A Codex or Kimi research
  call killed at its time limit now keeps what it had already streamed: the
  last answer text, or failing that the pages it opened, which the archive
  fetches and the writer reads. The slot reports `partial`
  (`meta.partial_branches` names the branch), still counts toward cited
  sources, and the run is `degraded` with the reason "cut off at the
  deadline". Before this, a 30-minute timeout threw away everything the
  run had paid for. The research budget itself is unchanged.

- **Shared brains** (`researchkit.brains`): one brainkit brain fed by many
  runs.
  - `researchkit brain create NAME` makes an empty brain in advance.
  - `researchkit "topic" --brain NAME` feeds a run into it (a name from
    `brain list` or a path; `--ingest PATH` keeps its old meaning).
  - `researchkit brain add NAME <projects…>` attaches earlier runs
    (re-adding is a no-op), and `researchkit brain list` shows run and note
    counts.
  - With `--brain`, a run starts from what the brain already knows: matching
    source notes join the evidence as numbered, citable "(earlier research)"
    sources, and the writer says when a finding was already known. Only raw
    source notes are used, never summaries, and the web search still runs.
  - Ingest happens in the service right after `result.json` (so every
    entrypoint gets it). It is serialized per brain, retried while another
    process holds brainkit's writer lock, and recorded in `result.json`
    `brain`.
  - Web UI: a **Shared brain** field on the research form (existing names
    suggested, a new name creates the brain), a sidebar list of shared
    brains with create and a browser reusing the Brain tab, and an
    "add to brain…" box on each project.
  - HTTP API: `GET/POST /api/brains`, `GET /api/brains/{name}` (+`/note`),
    `POST /api/brains/{name}/ingest`, and `brain` on `/api/research`. MCP:
    `research(brain=…)`, `list_brains`, `brain_context`. Over HTTP and MCP a
    brain is addressed by name only, never a path.

- `result.json` carries `timings`: every progress event (provider start/done,
  summaries, writers, formatting) with seconds since run start on a monotonic
  clock, so the critical path of a run can be read off its own record.

- `researchkit doctor --probe` sends one tiny prompt to each CLI harness the
  preset uses (in parallel, 120 s cap) and reports exhausted quota, revoked
  auth or an unusable model id. A login check can't see those: Antigravity
  showed ✓ while out of quota for a week. `--probe-api` also probes LLM API
  slots (opt-in, since each probe is a small billed request).
- Honest run status. `result.json` carries `run_status` (`ok` / `degraded` /
  `failed`, reasons, per-slot state). The done line reads "Run degraded: 2
  with sources, 5 empty, 2 failed — failed slots: kimi (quota)…", and a run
  where no slot returned a source exits non-zero even if every CLI answered.
- Failure classes (`researchkit.failures`): quota, rate limit, auth, config,
  timeout, transient, error. The patterns come from 248 past runs' real error
  strings. A quota failure marks that allowance (one per CLI harness or API
  vendor) exhausted process-wide until its stated reset or 1 h. Later
  providers, council members and writers on the same allowance are skipped
  with the original reason instead of failing again. Retries no longer fire
  on quota, auth or config errors.

- `harness_only: true` preset flag (set on `harness` and `harness-fast`):
  every LLM step a run uses — the research slots it selects, summarizers,
  improver, council and boss — must be a CLI harness spec. A run that would
  bill an LLM API is refused before it spends, naming each `slot=spec`;
  `researchkit doctor` reports the same up front. Search APIs (Perplexity,
  Tavily, Brave, Exa) stay allowed. `glm` has no harness route and is
  refused when selected on these presets.
- The GitHub provider runs subscription-only when its slot holds a CLI spec:
  GitHub REST search plus analysis on that harness, without the OpenAI-API
  github.com web search. `harness`/`harness-fast` now set
  `github: codex:gpt-6.1-sol`.

### Changed

- The youtube, medium, substack and stealth plugins (webui, linkedin,
  medium_stealth) run by default again, so every site is selected in a plain
  run as before. `default_enabled` stays available to plugin authors.

- **One dispatcher for plain-API completions** (PLAN A.8):
  `researchkit.api_llm.complete`. The improver (six client branches), the
  summarizer (Gemini, GLM, Kimi), the keyword synthesizer and
  `complete_via_spec` with a plain model id all route through it, so client
  setup, vendor routing and the retry policy live in one place. Each vendor
  keeps its existing knobs, and the commands behave as before. Deleted as
  dead: `Summarizer.summarize_single` and its two single-summary prompts
  (no caller in core or the plugins).

- `default` preset: Grok slot `grok-4.3` → `grok-4.7`, the current flagship
  (PLAN A.5). The OpenAI API pins are unchanged: the gpt-6.x ids are only
  confirmed for Codex, and the API side is unverified without a paid probe
  (`researchkit doctor --probe-api`).

- **One archive and one brain per run, built by the service** (PLAN 3.5).
  The CLI, the HTTP server and the MCP server each re-ran the materials
  download and the brain build after a run that had already archived its
  sources in-run, and a boosted run did that once per sub-project. Now
  `run_project` builds the pack's `brain/` right after `result.json`, and a
  boosted run builds only the parent's, which covers every sub-project.
  `researchkit materials <run>` still re-downloads on request.

- **Repo harvest runs beside the archive download** (PLAN 3.4), so the
  writers wait for the slower of the two rather than both in turn. Every
  clone is capped by what remains of the 90 s deadline and the worker pool
  is joined: a slow clone is killed instead of abandoned, and nothing keeps
  writing into `repos/` after the run moved on. The serial
  `clone_cited_repos` (the `repos` command) and the pipeline now share one
  clone, harvest and delete-after-extract routine.

- **No provider-side page fetches** (PLAN 3.3). Codex, Grok CLI and Kimi no
  longer scrape every cited page for its `<title>`, and agy no longer GETs
  each page after resolving its grounding redirect (the HEAD that resolves
  the redirect stays, and now runs concurrently). The materials stage
  already fetches every cited page and takes the title from it, so each page
  was fetched twice.

- **Per-provider summaries run only when site research needs them** (PLAN
  3.2). Their one real consumer is keyword synthesis for site research; the
  report writers read the archive. Harness presets turn site research off,
  so a harness run no longer makes one extra CLI call per slot. `providers.md`
  shows an excerpt of each slot's own answer instead. API presets with site
  research on are unchanged.

- **One breadth-first report, written from the archive.** `report.md` used
  to stack three independent writers that restated the same answer under the
  same headings (14–21k words). The new publication stage
  (`researchkit.publish`) works in four steps:
  1. **Evidence:** every cited source is numbered, and archived pages are
     split into passages (`researchkit.evidence`).
  2. **Body:** one writer turns the analysis's questions into 3–5 finding
     sections, each written from its own selected passages and ending with
     "What the sources did not answer".
  3. **Lead and article:** the TL;DR and "In brief" are written _last_, from
     the finished body. The article is written in parallel as a second
     render of the same cited report, and may link only its sources.
  4. **Citations in code:** `[n]` anchors are mapped and renumbered by first
     appearance, unused sources dropped, and one Sources list kept. Inline
     URLs a writer used become anchors (`researchkit.citations`).

  `report.md` is TL;DR → In brief → sections → Sources, with
  `evidence.md` (cross-source analysis) and `providers.md` (per-provider
  summaries) as appendices. Raw output stays in `result.json`.

- `claims.jsonl`: one row per cited sentence and source (verbatim sentence,
  hedge words, numbers, canonical URL, content hash, archive status), the
  handoff unit for brainkit and skillskit. `result.json` gains `publication`
  (sources cited, claims, hard issues, advisories, writer errors) and
  `citations`.
- Integrity gate:
  - **Hard:** unknown citation numbers, off-list URLs (report and article),
    chatbot artifacts, "corroborated" backed by one origin.
  - **Advisory:** cited sources without archived text, and numbers that
    appear in none of a sentence's archived sources.
- A run with no citable research gets an honest stdlib "what failed"
  article instead of an LLM writing on an empty trace. If the body writer
  fails, the report falls back to the cross-source analysis and says so.
- `researchkit republish <project>`: re-run only the writers on a finished
  project. It uses the same research and makes no provider calls.
- The skill is no longer written on every run (skillskit owns the
  pack → skill rewrite). `researchkit skill <project>` distills one on request
  from the analysis plus the run's verified claims.
- Removed the digest and professional-overview writers, `digest.py`, and the
  report stacking. A blind A/B on frozen evidence (Fable 5.1 and GPT-6 Astra
  as judges, `.local/research-202610/ab/AB-RESULTS.md`) preferred the new
  report on every topic, with completeness at parity after one fix round.

- **Archive first.** A project run downloads its cited sources into
  `materials/` _before_ the writers start, from the in-memory result
  (`download_materials(project, result=…)`; `result.json` is no longer a
  prerequisite). This covers the CLI, the web server, MCP and every boosted
  sub-run, which all go through `run_project`. `result.json` records
  `materials: {fetched, total_cited}`, and a run whose citations could not be
  archived at all is `failed`. `--no-materials` / `--materials-limit` apply
  to the in-run archive. The post-run materials step is now an idempotent
  cache hit that still builds the brain.

- Materials follow a fetch policy by host class:
  - X, LinkedIn, Medium, YouTube, Instagram, Facebook and TikTok links stay
    cited but link-only (`tos-blocked`). Their terms forbid scraping, and a
    plain fetch only burned its timeout to archive a logged-out husk.
    Connector-supplied transcripts and articles are still archived first.
  - archive.today mirrors are `rejected`.
  - A bare `github.com/<owner>/<repo>` link archives the raw README instead
    of the HTML page.
  - PDFs are recorded as `pdf_unparsed` (they used to be dropped as
    `binary`).
  - A page carrying schema.org `isAccessibleForFree: false` with a
    teaser-length body is `paywall` instead of being archived as the source.
  - Entries record `http_status`.

  brainkit ingests only `fetched` rows, so the new statuses are additive.

- One source identity: `researchkit.urls.canonical_url` replaces the two
  identical copies in `materials.py`. It keeps the identity earlier runs
  stored (and brainkit keys on) and only adds default-port stripping.
  `origin_key` is a looser key for counting independent origins, and
  `safe_fetch` returns a `FetchResult` (status, MIME type, final URL, raw
  bytes, truncation, error) instead of text-or-None. `safe_fetch_text`
  wraps it unchanged.

- Presets can declare run defaults in `models.yaml`: `providers:` and
  `site_research:`. They apply only when the run names none. `harness` and
  `harness-fast` declare their zero-key sets and turn site research off, so
  `researchkit "topic" --preset harness-fast` alone is the subscription-only,
  time-boxed run: no Perplexity or Exa calls, no ChatGPT web browser
  connector, no keyword fan-out. `explore`, the MCP explore tool and the web
  UI's "Harness only" mode read the set from the preset instead of three
  hard-coded copies. API presets are unchanged (`openai gemini grok
perplexity`, site research on). `--no-site-research` and the API's
  `site_research` are tri-state: unset means the preset decides.
- `mcp` is pinned `>=1.2,<2`: `mcp_server.py` uses `FastMCP`, which SDK 2.x
  replaced with `MCPServer`.

- skills.sh hits are marked as procedures (`meta.evidence = "procedures"`).
  The meta-summary is told, through the pipeline-verified channel, never to
  cite them as support or count them toward corroboration, and `result.json`
  lists them as `skill_candidates` for skillskit.

- The keyless providers query with a few key terms instead of the raw topic
  sentence (`researchkit.queries`: deterministic, no model call). Names and
  identifiers rank first; dates, numbers and research filler are dropped;
  `?`/`*` are stripped (OpenAlex returned HTTP 400 on them); arXiv gets an
  AND of terms instead of one exact phrase. A live backtest on 30 past topics
  moved runs-with-sources from 24% → 80% (OpenAlex), 1% → 20% (Hacker News),
  3% → 13% (arXiv) and 0% → 3% (Stack Exchange).

- Each CLI research slot runs its social and web queries **concurrently**
  (Claude, Codex, Antigravity, Grok, Kimi). They're the same two queries on
  the same model and effort, so only the wait changes: Claude's two serial
  ~19–21 min queries were 38 of a 44.5 min `harness` run. Each branch is kept
  on its own: a failed web query no longer discards a paid social answer, and
  `meta.branch_errors` records what failed. The slot fails only when every
  branch fails.

- `advise` and `council` give Claude the preset's full `claude_max_budget`
  ($15 on harness) instead of the in-run $3 cap, which stopped Fable 5.1
  mid-answer on long `--context-file` questions (`error_max_budget_usd`).

- Startup is ~9x faster: `import researchkit.cli` takes 0.10 s instead of
  0.88 s. The package and `researchkit.providers` resolve their exports
  lazily, so the API SDKs (google-genai, openai) load only when an API slot
  actually runs. Every `--help`, `doctor`, `advise`, MCP boot and web request
  used to pay for them. API mode works exactly as before.
- Keyword generation and per-provider summaries on the Codex, Antigravity,
  Grok and Kimi CLIs get the 300 s side-call budget Claude already used,
  instead of the 1800 s research budget. Research and writer calls keep their
  full budget.

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

- Claude runs only as a harness, never on an API: every `claude` subprocess
  (research slot, writers, council, boss, `advise-session`, the doctor and
  run-start auth checks) now strips `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL` and the Bedrock / Vertex /
  Foundry / Mantle switches through one shared env builder. Before, only
  `ANTHROPIC_API_KEY` was removed, and doctor checked auth with the
  unscrubbed env. `doctor` now fails the Claude checks when Claude Code would
  still bill an API (an `apiKeyHelper` in its settings, a cloud provider).
- `provider_models` in `result.json` records the Claude slot as
  `claude:<model>`, not a plain id that read as API billing.
- Stale Claude ids: `claude-sonnet-5` / `claude-opus-5` fallbacks and pins
  (including the `harness-fast` council member) move to `claude-sonnet-5-5`
  / `claude-opus-5-5`.

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
