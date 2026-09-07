---
name: advise-max
description: Asks the cutting-edge flagship of every logged-in coding-agent CLI at maximum reasoning effort (Claude Fable 5 @max, Codex GPT-6 Astra @ultra, Gemini 3.7 Flash High, Grok 4.6 @xhigh, Kimi K3) the same question, side by side — subscription auth, zero API keys. Use when the user wants the strongest possible second opinions, "ask the best models", "max-effort advice", "advise-max", or a hard question worth top-tier reasoning. Not for quick/cheap opinions (use advise), a synthesized answer (use council), or web research (use explore).
argument-hint: "<question>"
---

# advise-max — flagship models, maximum effort, side by side

## Purpose

`advise`, but every member is the strongest model its harness offers, at the
highest reasoning tier the CLI accepts. Slower and much heavier on
subscription quota than plain `/advise` — reserve it for questions that
deserve it.

## Workflow

1. Run from the researchkit repo root (or `--directory` a checkout):

   ```bash
   uv run researchkit advise "QUESTION" --harnesses \
     claude:claude-fable-5@max \
     codex:gpt-6-astra@ultra \
     agy:gemini-3.7-flash-high \
     grokcli:grok-4.6@xhigh \
     kimicli:kimi-code/k3
   ```

   `--context-file notes.md` appends context, same as `/advise`.

2. Relay each `## <harness-spec>` answer side by side, attributed — no
   merging (run `/council` with these same `--harnesses` for synthesis).

Model notes (Codex re-verified live 2026-09-07; the rest 2026-08-21):
`gpt-6-astra` is the current Codex flagship, and it supersedes
`gpt-5.6-sol`. Effort is a REASONING TIER, never part of the model id —
pass `-m gpt-6-astra` with `model_reasoning_effort`, since an id like
`gpt-6-astra-ultra` does not exist. Astra accepts both `ultra` and `xhigh`
(both answered a probe cleanly); `ultra` is what this skill uses and
`xhigh` is what `codex`'s own config defaults to. Antigravity now lists real ids (`agy models` prints
`<id>\t<display name>`) and bakes the thinking level into the id, so pass
`agy:gemini-3.7-flash-high` — no quoting, and a `@high` suffix does
nothing. The Grok CLI's effort ceiling rose to `xhigh` (`max` is still
rejected: "use one of: xhigh, high, medium, low"); `grok models` offers
`grok-4.6` (default) and `grok-4.5`. Kimi Code has no effort flag —
`kimicli:kimi-code/k3` runs at the CLI config's `[thinking]` effort. Effort
tiers and model ids drift — `uv run researchkit doctor --preset harness`
catches dead pins in seconds, and when a member fails with an "unknown
effort/model" error, trust the CLI's error text over these notes.

## Failure modes

- Same isolation as `/advise`: a member out of quota ("usage limit",
  "Individual quota reached") or not logged in fails alone, reason inline.
- Ultra/max tiers multiply token burn per answer — expect ~2–15 min and
  meaningful subscription usage per run. A long, multi-question prompt can
  push a single member past 10 minutes; harness calls get a 30-minute
  budget for exactly that reason (`RESEARCHKIT_CLI_TIMEOUT=<seconds>`
  raises or lowers it). Do not shorten it to "speed the run up" — that is
  what used to turn codex into a `*failed: timed out*` line.
