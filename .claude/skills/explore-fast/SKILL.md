---
name: explore-fast
description: Runs a time-boxed explore (boosted, subscription-only, cited research) on the providers with the best time-value ratio - Antigravity, Grok CLI, Kimi Code CLI and the keyless sources, capped at 15 minutes per call - so it finishes in 15-25 minutes instead of waiting on a 30-55 minute Codex outlier. Use for "quick explore", "explore-fast", "research this but don't take an hour", or a cited report under a deadline. Not for maximum depth (use explore) or opinions (use advise/council).
argument-hint: "<topic>"
---

# explore-fast — explore, minus the outliers

## Purpose

Same pipeline and same output as [explore](../explore/SKILL.md): a council
refines and decomposes the topic, parallel sub-investigations research it with
web search, a super-summary synthesizes, zero API keys. The difference is that
every step is picked for time-value. Providers run concurrently, so a run is
exactly as slow as its slowest member — and two members are outliers. Measured
on this machine's harness runs (about 120 samples per slot):

| Slot                               | Median  | p90     | Here?                                 |
| ---------------------------------- | ------- | ------- | ------------------------------------- |
| openai (codex research)            | 30 min  | 40 min  | no — the outlier                      |
| claude (opus-5 at max effort)      | 11 min  | 33 min  | not as a researcher; Sonnet writes    |
| kimi (kimicli:kimi-code/k3)        | 4 min   | 10 min  | yes                                   |
| gemini (agy:gemini-3.7-flash-high) | 1 min   | 18 min  | yes                                   |
| grok (grokcli:grok-4.6)            | 1 min   | 32 min  | yes — the tail is what the cap is for |
| keyless (hackernews, skillsh, …)   | seconds | seconds | yes                                   |

Two levers do the work: the `harness-fast` preset in `models.yaml` (no Codex
research, `claude-sonnet-5` for the four writers, a council without Codex,
effort `high`) and `RESEARCHKIT_CLI_TIMEOUT=900`, which turns a straggler into
a dropped provider instead of an hour-long wait.

## When to use / when NOT to use

Use when a cited, multi-source report is wanted on subscriptions and the clock
matters: a deadline, a first pass before a deeper run, or a topic that does
not need Codex's 20-30k-character deep read. NOT for: maximum depth (`explore`
keeps Codex and Opus and takes 30-90 minutes), quick opinions (`/advise`,
`/council`), or maximum breadth (API providers harvest more citations faster).

## Workflow

1. **Pick the output directory first**, exactly as `explore` does: with a
   gitignored `.local/` in the project you are in,
   `git check-ignore -q .local && export RESEARCHKIT_PROJECTS_DIR="$PWD/.local/research"`;
   otherwise output goes to `projects/`.

2. Run from the researchkit repo root (or `--directory` a checkout):

   ```bash
   RESEARCHKIT_CLI_TIMEOUT=900 uv run researchkit explore "TOPIC" --days 7 \
     --preset harness-fast \
     --providers gemini grok kimi hackernews skillsh arxiv stackexchange openalex
   ```

   `--materials` / `--ingest <brain>` / `--no-raw` work as in `explore`. The
   provider list is the point: it omits the `openai` and `claude` research
   slots on purpose — do not add them back to "get more"; that is `explore`.

3. It is alive, not hung: `[boost]` heartbeat lines on stderr track the
   council, each sub-project's providers, and the super-summary. A provider
   that hits the 15-minute cap is logged and dropped; the run continues.

4. Outputs land under `<projects-dir>/<timestamp>_<topic>/` (the absolute
   path is printed as `wrote: …`): sub-reports in `subprojects/*/report.md`,
   the super-summary as the parent `report.md`, plus each sub-project's
   `article.md`, `skill/SKILL.md` (when the research supports one),
   `materials/` and `brain/`.

5. Relay the super-summary; point at the sub-reports for depth. The
   `**Coverage:**` line at the end of each report names any provider the cap
   cut, so the reader knows what is missing.

## What you give up

- Codex's research report — the deepest single read, and the slowest.
- Opus as a researcher and as the writer of the summaries (the boss that
  writes the super-summary stays on `claude-opus-5@high`).
- Nothing else: site research is off in both skills (its connectors are
  API-key paths, and a web UI connector was the 86-minute outlier in the
  last full explore).

## Failure modes

- All CLIs in the preset must be signed in; there is no API fallback.
  `uv run researchkit doctor --preset harness-fast` checks every pin in
  seconds — run it before a run, not after 20 minutes.
- The cap applies to every CLI call, including the council, the boss and the
  writers. If stderr shows the super-summary timing out, raise it for that
  run (`RESEARCHKIT_CLI_TIMEOUT=1500`) rather than removing the provider list.
- Budget: still many subscription calls per run (council + 3 sub-projects ×
  3 CLI providers + writers). Don't loop it casually.
