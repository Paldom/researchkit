---
name: advise
description: Asks every logged-in coding-agent CLI (Claude Code, Codex, Antigravity, Grok CLI, Kimi Code CLI) the same question and returns each harness's answer side by side — subscription auth, zero API keys. Use when the user wants several models' independent takes, "ask all the models", "what would the other AIs say", "get second opinions on this", or to compare vendors' answers verbatim. Not for one synthesized answer (use council) or web research (use explore).
argument-hint: "<question>"
---

# advise — every harness answers, side by side

## Purpose

One question goes to each subscription CLI harness in parallel; every answer
comes back verbatim and labeled. The value is the un-synthesized comparison —
you see where four different vendors' flagship models agree and diverge.
Runs entirely on logged-in CLI subscriptions; no API keys are read.

## When to use / when NOT to use

Use for opinion questions, design calls, tradeoff checks, "sanity-check this
idea against several models". NOT for: a single decisive answer (that is
`/council`), live web research or citations (that is `/explore`), or anything
requiring repo file access — members answer from model knowledge only.

## Workflow

1. Run from the researchkit repo root (or point `--directory` at a checkout):

   ```bash
   uv run researchkit advise "QUESTION"
   uv run researchkit advise "QUESTION" --context-file notes.md   # append context
   uv run --directory /path/to/researchkit researchkit advise "QUESTION"
   ```

2. Read the output: one `## <harness-spec>` section per member. A failed
   member prints `*failed: <reason>*` and never blocks the others; the stderr
   tail says `N/5 harnesses answered`.

3. Relay the answers side by side, attributed. Do not merge them into one
   answer — if the user wants synthesis, run `/council` instead.

Default members come from the `harness` preset in `models.yaml`
(claude:claude-opus-4-8@xhigh, codex:gpt-5.6-sol@xhigh,
"agy:Gemini 3.5 Flash (High)", grokcli:grok-4.5, kimicli:kimi-code/k3).
Override per run: `--harnesses codex:gpt-5.6-sol grokcli:grok-4.5` (a
`@<effort>` suffix sets reasoning effort where the CLI supports it).

## Failure modes

- A harness that is not installed, not logged in, or **out of subscription
  quota** ("usage limit", "Individual quota reached") fails that member
  only — the reason prints inline; exit code is 1 only when ALL members fail.
- Requires the CLIs to be signed in (`claude`, `codex`, `agy`, `grok login`,
  `kimi login`) — there is no API-key fallback by design.
- Expect ~20–60 s wall clock (parallel; slowest member dominates).
