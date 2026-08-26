---
name: explore
description: Runs boosted multi-provider web research entirely on logged-in CLI subscriptions (Claude Code, Codex, Antigravity, Grok CLI, Kimi Code CLI) - a council refines and decomposes the topic, parallel sub-investigations research it with web search, and a super-summary synthesizes; zero API keys. Use when the user wants deep research without API costs, "research this on my subscriptions", "harness-only research", "explore this topic", or a cited report while API keys are unavailable. Not for quick lookups (takes 15-30 minutes) or single-model questions (use advise/council).
argument-hint: "<topic>"
---

# explore — boosted research on subscriptions only

## Purpose

The full researchkit boost pipeline with every step on subscription CLI
harnesses: the council (5 harnesses) refines the topic and decomposes it into
sub-investigations; each sub-project researches with the five CLI providers
(web search included); summaries and the cross-cutting super-summary run
through CLIs too. No API key is read at any step.

## When to use / when NOT to use

Use for deep, citation-backed research when API spend is unwanted or keys are
unavailable. NOT for: quick questions (a run is 15–30 minutes of real
subscription usage), opinion-only questions (`/advise`, `/council`), or
maximum source breadth — API providers harvest more citations faster; the
harness path trades breadth for depth (agents that actually read pages).

## Workflow

1. **Pick the output directory first.** If the project you are working in has
   a gitignored `.local/` directory, put the run there — research output is
   bulky and must never reach a commit:

   ```bash
   git check-ignore -q .local && export RESEARCHKIT_PROJECTS_DIR="$PWD/.local/research"
   ```

   Check it is actually ignored (`git check-ignore -q .local`), not merely
   present: an un-ignored `.local/` would stage the whole pack. With no
   `.local/`, leave the variable unset and output goes to `projects/`.

2. Run from the researchkit repo root (or `--directory` a checkout). Quote
   the topic; add flags as needed:

   ```bash
   uv run researchkit explore "TOPIC" --days 7
   uv run researchkit explore "TOPIC" --materials --ingest ../brainkit/brain
   ```

3. It is alive, not hung: `[boost]` heartbeat lines on stderr track the
   council, each sub-project's providers, and the super-summary.

4. Outputs land under `<projects-dir>/<timestamp>_<topic>/` — `.local/research`
   when step 1 set it, else `projects/` (the absolute path is printed as
   `wrote: …`): sub-reports in `subprojects/*/report.md`,
   the super-summary as the parent `report.md`. `--materials` archives cited
   pages per sub-project; `--ingest <brain>` hands the run to brainkit.

5. Relay the super-summary; point at the sub-reports for depth. If stderr
   says "super-summary unavailable", the sub-reports still stand.

Defaults come from the `harness` preset in `models.yaml` (providers
codex:gpt-5.6-sol / agy:gemini-3.7-flash-high / grokcli:grok-4.6 /
kimicli:kimi-code/k3 / claude:claude-opus-5; site research off — its
connectors are API-key paths). `--preset <name>` swaps the whole model set.
`uv run researchkit doctor --preset harness` verifies every pin first — a
20-minute run is a bad place to discover a dead model id.

## Failure modes

- Providers degrade independently: a harness that fails or returns nothing
  (observed live: codex refusing heavy research prompts) costs its sources,
  not the run — Claude Code and the Grok CLI carry the depth today. Each
  harness call gets a 30-minute budget (`RESEARCHKIT_CLI_TIMEOUT=<seconds>`
  overrides), so slow agents finish rather than time out.
- All five CLIs must be signed in; there is no API fallback by design.
- Budget note: a boosted run is many harness invocations against your
  subscriptions — don't loop it casually.
