# The research pack — interchange contract (v2)

A finished researchkit run directory IS the interchange format the kit trio
shares: brainkit ingests it, skillskit's `/skill-from-research` skillifies
it. This page is the contract both consumers pin; changes bump the version.

```
projects/<timestamp>_<slug>/
├── result.json     # REQUIRED — its existence means "run complete"
├── report.md       # the cited synthesis; `## ` sections are chunkable
├── config.json     # run parameters
├── run.log         # full log
├── article.md      # publication-ready article over the same evidence
├── claims.jsonl    # one row per cited sentence × source (see below)
├── evidence.md     # the report's numbered sources with archive status
├── providers.md    # per-slot appendix (state, summary or excerpt)
├── claims.labels.jsonl  # optional: `researchkit verify` labels
├── rubric.json     # optional: `researchkit score` grade
├── brain/          # the pack's own brainkit brain over materials/
├── materials/      # archived cited sources (default on)
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
   **Manifest v2** adds `schema_version: 2`, a full `sha256` on every
   fetched entry, `claims_sha256` (the digest of `claims.jsonl`, or null),
   and `complete`. The manifest is written before the writers run
   (archive-first) with `complete: false`, then sealed with `complete: true`
   once the claims exist, before `result.json`. Consumers must refuse an
   unknown `schema_version`, an incomplete pack, or a file whose hash does
   not match, and must refuse it before writing anything. A manifest
   without `schema_version` is v1.
4. **`report.md` `## ` headings are chunk boundaries** (fence-aware).
   researchkit demotes provider-emitted `## ` headings inside embedded raw
   text so only real sections chunk.
5. **`claims.jsonl`** is the handoff unit: one JSON row per cited sentence
   and source, `schema_version: 1`. Keys: `id` (stable across
   renumbering), `run_id`, `document`, `section`, `sentence` (verbatim,
   hedges and numbers kept), `canonical_url`, `content_hash`,
   `archive_status`, `material_file`, `hedges`, `numbers`, `co_cited`, and
   `native_spans` (the sentences an API provider natively cited this URL
   for: OpenAI annotations, Gemini grounding supports, Perplexity markers;
   a locator, not the source's text). `claims.labels.jsonl` rows join on
   `id`: `label` is `supported` / `unsupported` / `contradicted` /
   `unavailable`, and `unavailable` never counts as support.
6. **`result.json` run facts**: `run_status` (`ok`/`degraded`/`failed`
   with reasons and per-slot states, `partial` for a slot cut off at its
   deadline), `timings`, `materials`, `publication`, `citations`, `depth`
   (per slot and branch; `unknown` where the provider exposes none), and
   `brain` when a shared brain was fed.
7. **Additive evolution.** New frontmatter keys and result.json keys may
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
