---
name: council
description: Convenes an LLM council of logged-in CLI harnesses (Claude Code, Codex, Antigravity, Grok CLI, Kimi Code CLI) on a question - members answer through distinct lenses, a boss model synthesizes one decisive answer with explicit convergence and dissent; subscription auth, zero API keys. Use when the user wants a deliberated or consulted answer, "convene a council", "have the models debate this", "what's the consensus", or a decision with disagreement surfaced. Not for raw side-by-side answers (use advise) or web research (use explore).
argument-hint: "<question>"
---

# council — lensed deliberation, one synthesized answer

## Purpose

An advisory council over subscription CLI harnesses: each member answers the
same question independently through a forced lens (Direct & Practical /
Skeptic & Risks / Context & Tradeoffs), then a boss model synthesizes the
anonymized answers into one decisive verdict that names its confidence, how
aligned the members were, and the strongest unresolved dissent. No API keys.

## When to use / when NOT to use

Use for decisions worth a second opinion: architecture calls, adopt-or-not
questions, risk assessments, "is this a good idea". NOT for: seeing each
model's raw answer (that is `/advise`), sourced/cited research (that is
`/explore`), or trivial factual lookups (a council is 6 harness calls —
5 members + the boss).

## Workflow

1. Run from the researchkit repo root (or `--directory` a checkout):

   ```bash
   uv run researchkit council "QUESTION"
   uv run researchkit council "QUESTION" --context-file notes.md
   uv run researchkit -v council "QUESTION"        # -v appends full member answers
   ```

2. Read the output top-down:
   - `# Council answer (confidence: …, convergence: …)` — the synthesized
     verdict. Low convergence means the members genuinely split; weigh it
     accordingly.
   - `## Dissent` — the strongest unresolved disagreement. Treat it as
     signal, not noise: relay it to the user, never smooth it over.
   - `## Members` — one line per member with lens, confidence, rationale
     (and `failed: …` for members that errored).

3. If stderr warns "boss synthesis unavailable", the shown answer is the
   first valid member's (deterministic fallback) — say so when relaying.

Members/boss default to the `harness` preset in `models.yaml`
(claude:claude-opus-5@xhigh, codex:gpt-6-astra@xhigh,
agy:gemini-3.7-flash-high, grokcli:grok-4.6, kimicli:kimi-code/k3;
boss claude:claude-opus-5@xhigh); override with `--harnesses <specs…>`
and `--boss <spec>` (specs accept `@<effort>`). `uv run researchkit doctor
--preset harness` verifies every pin before spending anything.

## Failure modes

- Individual member failures are isolated; the run errors only when every
  member fails (the message lists each member's error — usually a CLI not
  installed, not logged in, or out of subscription quota).
- Expect ~2–10 min (members in parallel, then one boss call). Each harness
  call gets a 30-minute budget: a dense question at `@xhigh` can take a
  member past 10 minutes, and a tighter clock only costs you that member's
  answer. `RESEARCHKIT_CLI_TIMEOUT=<seconds>` overrides it.
- Members answer from model knowledge — no web tools in deliberation.
