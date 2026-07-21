# The research pack — interchange contract (v1)

A finished researchkit run directory IS the interchange format the kit trio
shares: brainkit ingests it, skillskit's `/skill-from-research` skillifies
it. This page is the contract both consumers pin; changes bump the version.

```
projects/<timestamp>_<slug>/
├── result.json     # REQUIRED — its existence means "run complete"
├── report.md       # the cited synthesis; `## ` sections are chunkable
├── config.json     # run parameters
├── run.log         # full log
├── materials/      # archived cited sources (with --materials)
│   ├── index.json  # manifest; consumers ingest only status:"fetched" files
│   └── NNN-*.md    # one frontmattered markdown file per source
└── subprojects/    # boosted runs: each child is itself a valid pack
```

## Contract rules

1. **`result.json` existence = run complete.** Consumers must refuse a
   directory without it (a partial run must never be ingested). Keys:
   `topic` (or boosted `overarching_topic`), `meta_summary` (or
   `super_summary`), `provider_results[]` with per-source `url`/`title`/
   `date`, `provider_models`, `system_config_used`.
2. **Materials frontmatter** (flat `key: value`, plain strings): `url`
   (REQUIRED — the cross-run identity/merge key), `title`, `final_url`,
   `source_type` (`social`|`web`), `providers`, `topic`, `fetched_at`,
   `published` (ISO date when known), `content_kind`
   (`article`|`transcript`|`summary`), `content_digest` (sha256-16 of the
   body — content lineage across re-downloads).
3. **`materials/index.json`** is authoritative: consumers ingest only files
   it lists as `status: "fetched"`; a corrupt manifest fails closed.
4. **`report.md` `## ` headings are chunk boundaries** (fence-aware).
   researchkit demotes provider-emitted `## ` headings inside embedded raw
   text so only real sections chunk.
5. **Additive evolution.** New frontmatter keys and result.json keys may
   appear at any time; consumers ignore unknown keys. Renames/removals bump
   this spec's version.

## Consumers

- **brainkit** (compounding memory): `brainkit --brain <dir> ingest <pack>`
  — URL-deduped source notes, topic note from `meta_summary`,
  `--include-reports` chunks report sections, `subprojects/` recurse.
- **skillskit** (one-shot skill): in an agent session,
  `/skill-from-research <pack>` — inventories the pack, verifies
  load-bearing claims, authors eval-first skills. `report.md` is the
  synthesis to mine; `materials/*.md` are the primary sources to cite
  (their `url` frontmatter is the citable link, `published` the recency
  signal); `run.log`/`config.json` are context, not content.
