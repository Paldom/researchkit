# models.yaml — configuration guide

`models.yaml` at the repo root maps every pipeline step to a model and sets
the run knobs, grouped into named **presets**. Pick one with `active_preset:`
(or per run: `explore --preset harness`, API `{"preset": "..."}`; the UI's
preset picker and "Harness only" toggle set the same thing).

## Preset anatomy

```yaml
presets:
  my-preset:
    description: One line shown in the CLI/UI
    models:
      openai: gpt-5.4-mini # provider slots — one per provider
      gemini: agy:gemini-3.7-flash-high
      grok: grokcli:grok-4.6
      claude: claude:claude-sonnet-5
      kimi: kimicli:kimi-code/k3
      summarizer: gemini-3.7-flash # cross-provider meta-summary
      site_summarizer: gemini-3.5-flash-lite
      improver: gpt-5.4-mini # topic improvement + keyword generation
    reasoning_effort: medium # providers that support it
    perplexity_search_type: fast # fast | auto | pro
    tavily_search_depth: fast # basic | advanced | fast | ultra-fast
    claude_max_budget: 5.0 # USD cap per Claude CLI call
    council: # boost/advise/council members
      members: [claude:claude-opus-5@xhigh, codex:gpt-6-astra]
      boss: claude:claude-opus-5
    boost:
      enabled: false # `--boost`/`explore` force it per run
      max_subprojects: 5
```

## Model spec grammar

A slot or council member is either a **plain API model id** or a **CLI-backed
spec** that routes the step through a logged-in coding-agent CLI
(subscription billing, no API key):

| Spec                | Backend                      | Auth                | Example                                                     |
| ------------------- | ---------------------------- | ------------------- | ----------------------------------------------------------- |
| plain id            | provider API                 | API key from `.env` | `gpt-5.4-mini`, `grok-4.6`                                  |
| `codex[:<model>]`   | Codex CLI (`codex exec`)     | ChatGPT login       | `codex:gpt-6-astra`                                         |
| `agy[:<model>]`     | Antigravity CLI              | Google account      | `agy:gemini-3.7-flash-high` (`agy models` lists ids)        |
| `grokcli[:<model>]` | Grok CLI (`grok -p`)         | `grok login`        | `grokcli:grok-4.6` (`grok models` lists ids)                |
| `kimicli[:<alias>]` | Kimi Code CLI (`kimi -p`)    | `kimi login`        | `kimicli:kimi-code/k3`, `kimicli:kimi-code/kimi-for-coding` |
| `claude[:<model>]`  | Claude Code CLI              | Claude subscription | `claude:opus`, `claude:claude-sonnet-5`                     |
| `deep[:<model>]`    | Claude Code `/deep-research` | Claude subscription | `deep:claude-sonnet-5` (opt-in only — slow, expensive)      |

Rules:

- The part after the colon is the **underlying model**, passed to the CLI
  verbatim (`claude:opus` uses the CLI's alias resolution); a bare prefix
  (`codex`, `claude`) uses that CLI's default model. Bare `claude-*` ids
  remain accepted as the legacy spelling of `claude:<id>`.
- **`@<effort>` suffix** (council members and the boss): per-member
  reasoning effort where the CLI supports it — `codex:gpt-6-astra@xhigh`,
  `claude:claude-opus-5@xhigh`, `grokcli:grok-4.6@xhigh`. Antigravity bakes
  effort into the model id (`-high`/`-medium`/`-low`); Kimi Code takes it
  from its own config.toml `[thinking]`.
- CLI-backed specs work in **every slot**: provider slots, council
  members/boss, `summarizer`, `site_summarizer`, and `improver` (prefer
  codex/agy/grokcli in summary slots; `claude:` runs under the default $3
  CLI budget). Caveats: `site_summarizer` is high-volume — a CLI spec is
  slower than the Gemini API — and a CLI `site_summarizer` for the youtube
  connector only summarizes fetched transcripts (native video understanding
  needs the Gemini API).
- Keys `models:` doesn't recognize as built-in slots become **plugin model
  slots** (e.g. `youtube: gemini-3.7-flash`); a sibling `plugins:` block
  passes per-plugin option dicts.
- **Model ids drift.** `researchkit doctor [--preset <name>]` checks every
  pin in a preset against the live CLI model lists and your `.env` keys
  before a run spends anything.
- **CLI-backed slots get a 30-minute wall clock** for a full answer, and 5
  minutes for short pipeline steps (keyword generation, per-provider
  summaries) that already degrade gracefully. Harnesses reason for minutes:
  a dense prompt at a top effort tier can run past 10, and a tighter clock
  loses that slot's answer entirely. `RESEARCHKIT_CLI_TIMEOUT=<seconds>`
  overrides both, and takes precedence over `PROVIDER_HTTP_TIMEOUT`. A
  wall-clock timeout is never retried — at these budgets it means the
  workload is too big, not that the network hiccuped.

## The presets that ship

- `default` — latest models, best quality (API keys).
- `optimal` / `optimal_quality` / `budget_friendly` — benchmarked cheap/fast
  tradeoffs (API keys).
- `harness` — subscription-only: every step on logged-in CLIs, no API keys.
  Used by `advise`, `council`, `explore`, and the UI's "Harness only" mode.
- `harness-fast` — `harness` minus the time outliers, for the `explore-fast`
  skill. Providers run concurrently, so a run is as slow as its slowest
  member: measured here, the Codex research slot takes 30–55 min per call and
  Opus at max effort 11–17, while `agy` / `grokcli` / `kimicli` finish in 1–4.
  The preset keeps the same CLIs but writes the summaries on
  `claude-sonnet-5`, seats a council without Codex, and runs at effort `high`;
  the skill adds `--providers gemini grok kimi <keyless…>` (no `openai` /
  `claude` research slots) and `RESEARCHKIT_CLI_TIMEOUT=900` so a straggler
  is dropped instead of waited for.
- `hybrid` — CLI subscriptions for reasoning (codex/grokcli/claude slots,
  summarizer, improver) + API endpoints for breadth (gemini, perplexity,
  tavily, exa, site research). Select per run: `--preset hybrid` (works on
  classic runs too, not just `explore`).

Precedence: per-run `--preset` > the `active_preset` in the sidecar state
(set via the UI or `models.yaml`). A `models.yaml` in your current working
directory overrides the repo one entirely.
