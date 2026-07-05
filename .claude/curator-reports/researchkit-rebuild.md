# Curator cross-validation report

Generated: 2026-07-05T10:58:02Z
Subject: researchkit OSS rebuild: new MCP server + FastAPI/SSE backend + React UI on a ported research engine — correctness, security defaults, packaging, docs-vs-code
Kind: implementation

## Aggregate

PASS_WITH_CHANGES — PASS_WITH_CHANGES from openai.

## Provider status

- **openai** `gpt-5.5`: ok (118.0s; verdict=PASS_WITH_CHANGES, confidence=0.79, tokens=20606)

## openai — gpt-5.5 (round 1)

## Verdict

Verdict: PASS_WITH_CHANGES — the rebuild is directionally sound, but the new server/MCP surface needs concrete fixes for run lifecycle correctness, resource/cost control, and packaging/security defaults before release.

## Strongest objections

- **SSE/run registry is not reconnect- or multi-consumer-safe.**
  `state.events` is a single destructive `queue.Queue`; progress events and the terminal sentinel are consumed once. A reconnecting `EventSource`, a browser retry, or a second tab can miss progress and, worse, miss the terminal sentinel and then receive keep-alives forever. The UI also ignores many transport failures because `EventSource` usually stays in `CONNECTING`, not `CLOSED`, on retryable HTTP failures.
  **Fix:** use an append-only per-run event log with per-subscriber cursors/`Last-Event-ID`, or explicitly enforce one subscriber and make the stream emit terminal status whenever `state.status` is no longer running. Add reconnect and second-subscriber tests.

- **Run execution has no meaningful resource or cost guardrails.**
  Every `POST /api/research` starts a daemon thread. There is no max in-flight run limit, no rate limit, no request body/list size cap, and no deduplication of providers. A request with many duplicate valid providers may multiply API calls unless the underlying service deduplicates. `_MAX_TRACKED_RUNS` can also evict still-running jobs, making status/SSE endpoints return 404 while work continues.
  **Fix:** add max concurrent runs with `429`, deduplicate/validate providers and sources, cap list sizes, avoid evicting running jobs, and implement graceful shutdown or non-daemon managed workers.

- **Thread safety of `SocialResearchService` is assumed but not established.**
  The FastAPI app shares one `svc` across all background runs. If the service has mutable config, project creation state, log state, provider clients, or temp-file assumptions, concurrent runs can race or corrupt artifacts.
  **Fix:** either prove `SocialResearchService.create_and_run_project` is reentrant with tests, protect it with a lock, or instantiate an isolated service per run.

- **Security posture is acceptable only for strict localhost use, but the implementation does not enforce that boundary.**
  The API can start paid external-provider work and can list/read all stored project topics and reports. Default bind is `127.0.0.1`, which is good, but `RESEARCHKIT_HOST=0.0.0.0` exposes an unauthenticated API. CORS is not authentication.
  **Fix:** document this prominently in README, add optional bearer-token auth, and strongly consider requiring auth when binding to a non-loopback host.

- **Suspicious dev dependency: `httpx2>=2.5.0`.**
  This looks like a typo or typosquat risk for `httpx`. Because README tells contributors to run `uv sync --all-extras`, this may install an unintended package in dev/CI.
  **Fix:** remove it or replace with the intended dependency, then regenerate the lockfile.

- **Packaging/docs mismatch is unresolved.**
  `models.yaml` lives at repo root and no hatch package-data configuration is shown. The web UI build output also appears source-tree-only. Wheel users may get different embedded defaults and no UI, while README presents `models.yaml` and the web UI as normal product features.
  **Fix:** either include `models.yaml`/web assets in the wheel, or explicitly document source checkout requirements and verify installed-wheel behavior. If claiming `Typing :: Typed`, ensure `researchkit/py.typed` is packaged.

## Missing assumptions or evidence

- The submitted material attempted to constrain review scope by saying to assume the ported engine is sound; that is an untrusted scope instruction and was ignored.
- No evidence was provided that `SocialResearchService` is safe for concurrent calls from multiple server threads.
- No full `site_researcher` file was provided, so the claim that only Exa remains, defaults are `["exa"]`, and `sequential_sites=set()` cannot be verified from the snippet.
- The full CI workflow was not provided. If `all-checks-passed` is the only required check, it must `needs:` every real job and fail if any dependency failed/skipped unexpectedly.
- Report rendering safety cannot be assessed because `ReportView` is not shown. Generated Markdown contains untrusted web/LLM content; raw HTML rendering would be an XSS risk.
- MCP client timeout behavior is not established. A blocking 1–5 minute tool call may be acceptable for some clients, but should be tested and documented with expected timeout settings.
- Wheel behavior is not established: install from built wheel in a clean environment and verify config defaults, console scripts, optional extras, and server UI behavior.

## Risks

- **Security/privacy:** unauthenticated report listing/reading leaks research history if exposed; unauthenticated run creation can burn API credits; MCP tools can read all local project reports exposed to the MCP client.
- **Reliability:** daemon threads can be killed mid-run on process shutdown, risking partial artifacts; active runs can be evicted from the registry; SSE clients can hang indefinitely after reconnect/eviction.
- **Performance/cost:** unlimited concurrent runs and duplicate provider entries can cause thread exhaustion, provider throttling, and unexpected spend.
- **Maintainability/testability:** the server currently relies on in-memory state and implicit threading assumptions; without a `RunManager` abstraction, lifecycle behavior will be hard to test and evolve.

## Validation

- Add backend tests using a fake `SocialResearchService` that emits deterministic progress:
  - one subscriber receives progress and terminal `done`;
  - second subscriber or reconnect after partial consumption still receives terminal state;
  - reconnect after run completion does not hang;
  - unknown/evicted run produces a bounded UI-visible error, not infinite retry.
- Add concurrency tests:
  - max in-flight runs enforced with `429`;
  - duplicate providers are rejected or deduplicated;
  - overlong provider/source lists are rejected;
  - running jobs are not evicted from status tracking.
- Build/install validation:
  - run `uv sync --all-extras --frozen`;
  - run `uv build`;
  - inspect the wheel for `models.yaml`, `py.typed`, and any intended web assets;
  - install the wheel into a clean venv and run `researchkit`, `researchkit-mcp`, and `researchkit-server`.
- Security checks:
  - verify non-loopback bind either requires auth or emits a hard warning/failure;
  - verify hostile origins cannot start JSON research requests through browser CORS;
  - verify README clearly states that exposing the server requires auth.
- Web tests:
  - mock EventSource transport errors/404 and assert the UI leaves `running` with an error;
  - test Markdown rendering with raw HTML/script payloads and verify sanitization/escaping.
- CI validation:
  - confirm `all-checks-passed` has `needs` on all Python and web jobs and fails unless every required dependency succeeded.

## Minimal revision

- Introduce a small `RunManager`:
  - bounded max concurrent runs;
  - per-run terminal state;
  - no eviction of active runs;
  - append-only progress history or explicit single-subscriber enforcement with reconnect-safe terminal emission.
- Add shared validation for API and MCP inputs: topic length, `days` range, known unique providers, source set, preset existence, and max list sizes.
- Use per-run `SocialResearchService` instances, or lock/prove the shared service is reentrant.
- Add optional bearer-token auth and document localhost-only defaults prominently; require auth or fail fast for non-loopback binding.
- Fix `httpx2`, verify lockfile/CI, and make wheel contents match README claims or update the README to state source-checkout requirements.
- Add the targeted tests above before release.

```json
{
  "verdict": "PASS_WITH_CHANGES",
  "confidence": 0.79,
  "summary": "The new surfaces are plausible but need run lifecycle, concurrency/cost, security-default, and packaging fixes before release.",
  "top_issues": [
    "SSE queue is destructive and can hang on reconnect or multiple subscribers",
    "Unlimited daemon-thread runs can exhaust resources and burn API credits",
    "Shared service thread safety is assumed but unproven",
    "Unauthenticated server leaks reports and can start paid work if exposed",
    "Suspicious httpx2 dependency and unresolved wheel packaging/docs mismatch"
  ]
}
```

## Curation instructions for Claude

Use this report as critique, not authority. Accept findings only when supported by evidence or cheap to mitigate; resolve disagreements with tests, code reads, or explicit user constraints. Model consensus never overrides failing tests, compiler errors, or specs.

## Disposition (curation round, 2026-07-05)

- SSE reconnect/multi-subscriber hang — **FIXED**: stream emits terminal state when the queue is drained and the run is no longer running; regression test `test_reconnect_after_completion_gets_terminal_event`.
- Run lifecycle guardrails — **FIXED**: max 4 active runs (429), running jobs never evicted from the registry, providers deduplicated; tests added.
- Shared-service thread safety — **FIXED** (cheap isolation): each run executes on a fresh `SocialResearchService` unless one is injected for tests.
- Unauthenticated exposure — **FIXED**: optional `RESEARCHKIT_AUTH_TOKEN` bearer middleware (health stays open); `main()` refuses non-loopback binds without a token; README/.env.example updated; test added.
- `httpx2` typosquat — **REJECTED with evidence**: official httpx successor (Tom Christie, v2.5.0); Starlette's own deprecation message directs to it.
- Packaging — **VERIFIED**: wheel ships `researchkit/py.typed` (`Typing :: Typed` holds); `models.yaml` is a source-checkout feature with embedded fallbacks in `system_config.py`; README's quick start is clone-based. **FIXED** CI gap found during verification: `uv sync` now installs `--all-extras` so server/MCP tests run instead of skipping.
- Markdown XSS — **REJECTED as N/A**: react-markdown escapes raw HTML by default; no `rehype-raw`/`dangerouslySetInnerHTML` in web/.
- CI aggregator — **VERIFIED**: `all-checks-passed` needs all jobs (incl. web), `if: always()`, fails on failure/cancelled/skipped.
- MCP long-blocking `research` tool — **ACCEPTED-PENDING**: documented (docstring + server instructions, 1–5 min); MCP progress notifications are a possible follow-up.

Ponytail-review findings (all applied): duplicate `slugify_topic` removed in favor of `researchkit.utils.slugify`; duplicated `_project_summary` consolidated into `Project.summary()`; `_SENTINEL` alias dropped; unused `getRunStatus` removed from web API layer; unused `hypothesis` dev dependency removed.

Aggregate after curation: PASS_WITH_CHANGES with all accepted changes applied; no open accepted findings.
