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
      gemini: agy:gemini-3.5-flash
      grok: grokcli:grok-build
      claude: claude:claude-sonnet-4-6
      summarizer: gemini-3.5-flash # cross-provider meta-summary
      site_summarizer: gemini-3-flash-preview
      improver: gpt-5.4-mini # topic improvement + keyword generation
    reasoning_effort: medium # providers that support it
    perplexity_search_type: fast # fast | auto | pro
    tavily_search_depth: fast # basic | advanced | fast | ultra-fast
    claude_max_budget: 5.0 # USD cap per Claude CLI call
    council: # boost/advise/council members
      members: [claude:claude-opus-4-8@xhigh, codex:gpt-5.5]
      boss: claude:claude-opus-4-8
    boost:
      enabled: false # `--boost`/`explore` force it per run
      max_subprojects: 5
```

## Model spec grammar

A slot or council member is either a **plain API model id** or a **CLI-backed
spec** that routes the step through a logged-in coding-agent CLI
(subscription billing, no API key):

| Spec                | Backend                      | Auth                | Example                                                |
| ------------------- | ---------------------------- | ------------------- | ------------------------------------------------------ |
| plain id            | provider API                 | API key from `.env` | `gpt-5.4-mini`, `grok-4.3`                             |
| `codex[:<model>]`   | Codex CLI (`codex exec`)     | ChatGPT login       | `codex:gpt-5.6-sol`                                    |
| `agy[:<model>]`     | Antigravity CLI              | Google account      | `agy:gemini-3.5-flash`                                 |
| `grokcli[:<model>]` | Grok CLI (`grok -p`)         | `grok login`        | `grokcli:grok-build`                                   |
| `claude[:<model>]`  | Claude Code CLI              | Claude subscription | `claude:opus`, `claude:claude-sonnet-4-6`              |
| `deep[:<model>]`    | Claude Code `/deep-research` | Claude subscription | `deep:claude-sonnet-5` (opt-in only — slow, expensive) |

Rules:

- The part after the colon is the **underlying model**, passed to the CLI
  verbatim (`claude:opus` uses the CLI's alias resolution); a bare prefix
  (`codex`, `claude`) uses that CLI's default model. Bare `claude-*` ids
  remain accepted as the legacy spelling of `claude:<id>`.
- **`@<effort>` suffix** (council members and the boss): per-member
  reasoning effort where the CLI supports it — `codex:gpt-5.6-sol@xhigh`,
  `claude:claude-opus-4-8@xhigh`. Antigravity has no effort control.
- CLI-backed specs work in the **provider slots** (openai/gemini/grok/claude),
  **council members/boss**, and the **summarizer** slot (prefer
  codex/agy/grokcli there; `claude:` runs under the default $3 CLI budget).
  `site_summarizer`/`improver` take plain API ids.
- Keys `models:` doesn't recognize as built-in slots become **plugin model
  slots** (e.g. `youtube: gemini-3.5-flash`); a sibling `plugins:` block
  passes per-plugin option dicts.

## The presets that ship

- `default` — latest models, best quality (API keys).
- `optimal` / `optimal_quality` / `budget_friendly` — benchmarked cheap/fast
  tradeoffs (API keys).
- `harness` — subscription-only: every step on logged-in CLIs, no API keys.
  Used by `advise`, `council`, `explore`, and the UI's "Harness only" mode.

Precedence: per-run `--preset` > the `active_preset` in the sidecar state
(set via the UI or `models.yaml`). A `models.yaml` in your current working
directory overrides the repo one entirely.
