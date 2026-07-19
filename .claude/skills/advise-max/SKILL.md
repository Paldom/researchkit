---
name: advise-max
description: Asks the cutting-edge flagship of every logged-in coding-agent CLI at maximum reasoning effort (Claude Fable 5 @max, Codex GPT-5.6 Sol @ultra, Gemini 3.5 Flash (High), Grok 4.5 @high, Kimi K3) the same question, side by side — subscription auth, zero API keys. Use when the user wants the strongest possible second opinions, "ask the best models", "max-effort advice", "advise-max", or a hard question worth top-tier reasoning. Not for quick/cheap opinions (use advise), a synthesized answer (use council), or web research (use explore).
argument-hint: "<question>"
---

# advise-max — flagship models, maximum effort, side by side

## Purpose

`advise`, but every member is the strongest model its harness offers, at the
highest reasoning tier the CLI accepts. Slower and much heavier on
subscription quota than plain `/advise` — reserve it for questions that
deserve it.

## Workflow

1. Run from the researchkit repo root (or `--directory` a checkout). The agy
   spec uses Antigravity's display-name format (the parenthesized thinking
   level is part of the model selection), so keep it quoted:

   ```bash
   uv run researchkit advise "QUESTION" --harnesses \
     claude:claude-fable-5@max \
     codex:gpt-5.6-sol@ultra \
     "agy:Gemini 3.5 Flash (High)" \
     grokcli:grok-4.5@high \
     kimicli:kimi-code/k3
   ```

   `--context-file notes.md` appends context, same as `/advise`.

2. Relay each `## <harness-spec>` answer side by side, attributed — no
   merging (run `/council` with these same `--harnesses` for synthesis).

Model notes (checked 2026-07-11): Codex "Sol Ultra" is the `ultra`
REASONING tier on `gpt-5.6-sol` (`model_reasoning_effort=ultra`, a
subagent-orchestration mode in closed preview — enabled on this account,
verified live), NOT a model id; `-m gpt-5.6-sol-ultra` is rejected.
Antigravity encodes the thinking level in the display name — bare
`gemini-3.5-flash` ignores a `@high` suffix. `@max` is verified live for
Fable 5; the Grok CLI's effort ceiling is now `high` (as of 2026-07-18 it
rejects `max`: "use one of: high, medium, low"; `grok models` currently
offers `grok-4.5`, `grok-build` is gone). Kimi Code has no effort flag —
`kimicli:kimi-code/k3` runs at the CLI config's `[thinking]` effort, which
defaults to `max` for K3. Effort tiers and model ids drift — when a member
fails with an "unknown effort/model" error, trust the CLI's error text over
these notes. Swap in newer flagships as they land; the specs are just
`--harnesses` arguments.

## Failure modes

- Same isolation as `/advise`: a member out of quota ("usage limit",
  "Individual quota reached") or not logged in fails alone, reason inline.
- Ultra/max tiers multiply token burn per answer — expect ~1–5 min and
  meaningful subscription usage per run.
