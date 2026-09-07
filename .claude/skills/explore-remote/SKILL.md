---
name: explore-remote
description: Runs an explore (boosted, subscription-only research) on a REMOTE researchkit machine over MCP, so the 15-40 minute run and its CLI subscriptions live on that box, not this one. Use when the user says "explore on the remote", "run research on my server/other machine", "explore-remote", or wants a cited report without tying up (or logging into) the local machine. Not for local research (use explore) or quick questions (use advise/council).
argument-hint: "<topic>"
---

# explore-remote — explore on someone else's machine

## Purpose

Identical output to [explore](../explore/SKILL.md) — council-refined topic,
parallel sub-investigations, cited super-summary — but the work runs on a
remote researchkit host reached over MCP. The subscriptions, the CLI logins,
the projects directory and the materials archive all live **there**.

Use it when the local machine is not where the research should happen: no
harness logins here, a laptop that sleeps, or a long run you do not want to
babysit.

## Preconditions

The remote must be serving the researchkit MCP endpoint:

```bash
# on the remote box
uv run researchkit-mcp --http --host 0.0.0.0 --port 8765
```

and this machine must have it configured as an MCP server named `researchkit`:

```bash
claude mcp add --transport http researchkit http://<remote-host>:8765/mcp
```

Check with `claude mcp list`. If the tools below are missing, say so and point
at the README's "Remote access over MCP" section rather than silently falling
back to a local run — a local explore spends _this_ machine's quota, which is
the opposite of what was asked.

## Procedure

1. **Start it.** Call `explore_start` with the topic. It returns a `run_id`
   immediately; nothing is researched yet.

   ```
   explore_start(topic="<the user's topic>", days=7, materials=true)
   ```

   Widen `days` when the topic is not about the last week.

2. **Poll, do not block.** Call `explore_status(run_id)` every few minutes. A
   run is 15–40 minutes; `stage` moves through council → boost_collecting →
   super_summary. Report progress to the user rather than sitting silent, and
   never start a second explore because the first "seems stuck".

3. **Read the write-up.** Once `status` is `done`, call
   `explore_report(run_id)` — the super-summary if the run decomposed, the
   report otherwise.

4. **Relay it.** Give the user the findings with their citations. The full
   pack (sub-reports, `materials/`, `result.json`) stays on the remote under
   its project folder; `list_research_projects` and `get_research_report` reach
   the archive there.

## Failure modes, and what they mean

- `status: error` with quota text — the remote's subscriptions are exhausted,
  not a bug. Report which harness and stop; retrying spends nothing but time.
- `Unknown run_id` — the remote restarted (ids are in memory). Finished work
  survives on disk: find it with `list_research_projects`.
- Connection refused / tool missing — the remote is not serving, or this
  machine has no MCP entry. Fix the setup; do not substitute a local run.
- A run that fails immediately with `RESEARCHKIT_NESTED` means you are already
  inside a researchkit-spawned agent. That block is deliberate; stop.

## Cost

The remote bills its own logged-in CLI subscriptions — no API keys, on either
machine. It is still a real explore: one topic fans out into several
sub-investigations. One run, then stop.
