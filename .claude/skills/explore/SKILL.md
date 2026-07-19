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

1. Run from the researchkit repo root (or `--directory` a checkout). Quote
   the topic; add flags as needed:

   ```bash
   uv run researchkit explore "TOPIC" --days 7
   uv run researchkit explore "TOPIC" --materials --ingest ../brainkit/brain
   ```

2. It is alive, not hung: `[boost]` heartbeat lines on stderr track the
   council, each sub-project's providers, and the super-summary.

3. Outputs land under `projects/<timestamp>_<topic>/` (the final absolute
   path is printed as `wrote: …`): sub-reports in `subprojects/*/report.md`,
   the super-summary as the parent `report.md`. `--materials` archives cited
   pages per sub-project; `--ingest <brain>` hands the run to brainkit.

4. Relay the super-summary; point at the sub-reports for depth. If stderr
   says "super-summary unavailable", the sub-reports still stand.

Defaults come from the `harness` preset in `models.yaml` (providers
codex:gpt-5.6-sol / "agy:Gemini 3.5 Flash (High)" / grokcli:grok-4.5 /
kimicli:kimi-code/k3 / claude:claude-opus-4-8; site research off — its
connectors are API-key paths). `--preset <name>` swaps the whole model set.

## Failure modes

- Providers degrade independently: a harness that fails or returns nothing
  (observed live: codex refusing heavy research prompts, agy timing out on
  long ones) costs its sources, not the run — Claude Code and the Grok CLI
  carry the depth today.
- All five CLIs must be signed in; there is no API fallback by design.
- Budget note: a boosted run is many harness invocations against your
  subscriptions — don't loop it casually.
