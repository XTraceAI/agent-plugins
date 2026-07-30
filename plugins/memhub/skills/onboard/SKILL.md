---
description: Use when a new user wants to set up MemHub / an agent brain for their repo, or asks to "onboard", "get started", "set up my brain", or "seed a brain from my work". Crosses the empty-brain cold start — creates the repo's agent brain, seeds it from a real Claude Code session, shows the compiled overview, and proves proactive recall on the repo's own symbols — then reports an activation funnel.
argument-hint: [session-id-or-path]
allowed-tools: Bash, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub_memhub__create_agent_brain, mcp__plugin_memhub_memhub__get_brain_overview, mcp__plugin_memhub_memhub__refresh_brain_overview, mcp__plugin_memhub_memhub__recall_directives, mcp__plugin_memhub_memhub__search_brains, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__create_agent_brain, mcp__plugin_memhub-staging_memhub__get_brain_overview, mcp__plugin_memhub-staging_memhub__refresh_brain_overview, mcp__plugin_memhub-staging_memhub__recall_directives, mcp__plugin_memhub-staging_memhub__search_brains
---

Onboard a new user onto MemHub for the repo they're in. The value of an agent
brain is a **compiled layer over content** (a self-describing overview + proactive
code-anchored directives) — so a brand-new empty brain shows nothing. This skill's
one job is to **cross the empty-brain cold start**: seed the brain from real work,
then prove it's immediately useful. Optimize for **time-to-first-useful-recall**,
not steps completed. Report an activation funnel at the end.

Arguments: `$ARGUMENTS` — an optional session id / `.jsonl` path to seed from.
Omit → use the most recently modified `.jsonl` DIRECTLY inside the
`~/.claude/projects/` directory matching the current working directory (top level
only; subdirectory `.jsonl` are subagent/workflow transcripts, not sessions).

Do exactly this:

## 1. Resolve the repo room (the durable boundary — never a blank brain)
- Derive the room name from the repo: `Repo: <org>/<name>` from
  `git remote get-url origin` (host + `.git` stripped).
- `list_agent_brains` → **exact-name match**. Reuse the existing id if found (a
  teammate may have created it). **Only** `create_agent_brain` when there is no
  exact match — do NOT mint a second room for a repo that already has one, and
  give it a real one-line description.
- Edge cases (SSH remotes, no remote, worktrees, **not a git repo at all**) and
  the full create-time rules are in
  `${CLAUDE_PLUGIN_ROOT}/references/repo-brain.md` — read it if the common path
  above doesn't apply cleanly.
- Record the `agent_brain_id`; call it `ROOM`.

## 2. Seed it — ONE substantive session (cross the cold start)
Seed from **exactly one** session, not many. One is enough to fire recall + get a
digest, and it's the fastest path to the first aha — importing several only
multiplies the async extraction latency (§3) and *delays* it. **Quality over
quantity:** pick the **most recent session that did real code work** (touched
actual files/symbols). A trivial chat yields a digest but no directives — if the
newest session is trivial, say so and pick an earlier substantive one rather than
seed noise. This one session is the **first deposit**, not a finished brain (§6).

Import it via the helper script (never call `import_conversation` yourself; it
handles any size):

```bash
uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/import_session.py" \
  --session "<session-id-or-path>" \
  --conversation-id "onboard-seed-<org>-<repo>" \
  --title "Onboarding seed — <org>/<repo>" \
  --agent-brain-id "<ROOM>"
```
The script targets the plugin's default endpoint — **production**
(`api.memhub.xtrace.ai`) when installed as `memhub`, staging when installed as
`memhub-staging`. Do NOT pass `--url` to cross between them: `--url` overrides
only the endpoint, while the OAuth client id and Auth0 tenant still come from
the *installed* plugin's `.mcp.json`, so a prod install pointed at staging
authenticates with prod credentials against the staging tenant and fails. To
seed a staging brain, install the `memhub-staging` plugin and run it from there.

Verify the output reports `path: "agentic"` (the agentic path composes the gist
**and** runs directive capture — the plain path does not). Note the record count.
Extraction (facts/episodes/directives + the digest) then runs **in the
background** — minutes for a large session.

**Optional — breadth from the repo's specs (don't block the aha on it).** The one
session gives *depth* on recent work, but only covers the files it touched. For
*breadth* — the codebase's durable design intent — ingest a few key docs
(`README`, top `docs/specs/*.md`) if they're reachable as URLs
(`ingest_document_from_url`). Offer this, but keep it optional and after the
session: it adds ingest latency, and `.md` specs are the highest-signal breadth
source (grep-hostile, hierarchical) when the user wants the brain to help beyond
the one session's slice.

## 3. Orient — the guaranteed aha (the brain describing the user's own repo)
Poll `get_brain_overview(ROOM)` until it returns a non-null `overview`
(the event-triggered digest refresh fires off the import). If it is still null
after a couple of polls, call `refresh_brain_overview(ROOM)` to run the digest on
demand rather than waiting on the async trigger, then poll again. Poll a few
times over ~2–5 min; if still null, tell the user the overview is still compiling
and to re-run `get_brain_overview` shortly — do NOT block indefinitely. When it renders,
show it: *"Here's what MemHub already learned about your repo."* This is the
reliable payoff and it's on the user's OWN content, not a demo.

## 4. Prove proactive recall — the delight aha
Pick 2–3 concrete symbols the seeded work actually touched (from
`git ls-files | head` / recently-edited files / symbols named in the session).
For each, `recall_directives(entities=["<file-or-symbol>"], repo="<repo>")` and
show what fires. A returned lesson/procedure = the differentiated value: a rule
the agent will get **proactively when it touches that code**, without asking.
(If nothing fires yet, capture may still be running — say so; this is the
capture→recall latency, not a failure.)

## 5. Route — confirm discoverability
`search_brains("<a topic from the seed>")` → confirm `ROOM` appears, so the agent
can find this brain from any task.

## 6. Report the activation funnel + set the compounding habit
Print a compact funnel with real values:
- **Seeded** — records imported, `path`.
- **Digest** — rendered? version.
- **Directives fired** — count + one example (the aha), or "capture still running".
- **Routed** — did `search_brains` surface the room?
- **Time-to-first-recall** — wall-clock from create → first directive fired (or
  "pending").

Set expectations honestly: the brain now helps **on the files this one session
touched** — coverage grows with every session. Then the one CTA: **keep working —
MemHub learns as you go.** Import sessions after substantive work
(`/memhub:import-session`), and (when available) enable PR-merge memory so the
brain compounds automatically.

Plain-English output throughout. If the memhub MCP is not connected, do
the seeding anyway and tell the user the reads need `/mcp` authentication.

**On overview latency:** the digest normally lands via the async event trigger
fired by the import. `refresh_brain_overview` (step 3) runs it on demand when
that is slow, so a long wait is not a dead end — but the digest itself still
takes time on a large seed. Say so plainly rather than implying it hung.
