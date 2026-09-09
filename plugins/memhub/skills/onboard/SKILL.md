---
description: Use when a new user wants to set up MemHub / an agent brain for their repo, or asks to "onboard", "get started", "set up my brain", or "seed a brain from my work". Crosses the empty-brain cold start — creates the repo's agent brain, seeds it from a real Claude Code session, shows the compiled overview, and proves proactive recall on the repo's own symbols — then reports an activation funnel.
argument-hint: [session-id-or-path]
allowed-tools: Bash, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub_memhub__create_agent_brain, mcp__plugin_memhub_memhub__get_brain_overview, mcp__plugin_memhub_memhub__refresh_brain_overview, mcp__plugin_memhub_memhub__recall_directives, mcp__plugin_memhub_memhub__search_brains, mcp__plugin_memhub_memhub__ingest_document_from_url, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub_memhub__list_workspaces, mcp__plugin_memhub_memhub__share_agent_brain_with_workspace, mcp__plugin_memhub_memhub__list_teammates, mcp__plugin_memhub_memhub__share_agent_brain, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__create_agent_brain, mcp__plugin_memhub-staging_memhub__get_brain_overview, mcp__plugin_memhub-staging_memhub__refresh_brain_overview, mcp__plugin_memhub-staging_memhub__recall_directives, mcp__plugin_memhub-staging_memhub__search_brains, mcp__plugin_memhub-staging_memhub__ingest_document_from_url, mcp__plugin_memhub-staging_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_workspaces, mcp__plugin_memhub-staging_memhub__share_agent_brain_with_workspace, mcp__plugin_memhub-staging_memhub__list_teammates, mcp__plugin_memhub-staging_memhub__share_agent_brain
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

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

## 0. Authenticate the plugin itself (before anything else)

```bash
uv run --with 'mcp<2' python "${CLAUDE_PLUGIN_ROOT}/scripts/login.py" --status
```

Not logged in → run `/memhub:login` (no `--status`) and let it finish before
continuing. Everything below needs it: the seed import in §2 sends as this user,
and the capture hooks in §1 cannot run without it.

This is **not** the `/mcp` connector's login. They share an Auth0 client but
store tokens in different places, so "connected in `/mcp`" and "my sessions are
being captured" are independent facts — never treat the first as evidence of the
second. What the hooks actually use is a **personal access key** (`mhk_…`) that
`/memhub:login` mints and stores at `~/.config/memhub-plugin/pak-<host>.json`: a
static bearer, because a hook is a cold background process that can never open a
browser to refresh an expiring token. See `/memhub:login` for the full story.

## 1. Resolve the repo room (the durable boundary — never a blank brain)
- Derive the room name from the repo: `Repo: <org>/<name>` from
  `git remote get-url origin` (host + `.git` stripped).
- `list_agent_brains` → **exact-name match**. Reuse the existing id if found (a
  teammate may have created it and shared it with you). **Only**
  `create_agent_brain` when there is no exact match — do NOT mint a second room
  for a repo that already has one, and give it a real one-line description.
- **A miss is not proof the room doesn't exist.** That listing shows only
  brains you created or were explicitly shared on — workspace membership
  reaches nothing — so a teammate's unshared room is invisible here. That cuts
  both ways, which is why §1.5 exists: sharing what you create is what makes
  the NEXT teammate's lookup find it instead of minting a third room.
- Edge cases (SSH remotes, no remote, worktrees, **not a git repo at all**) and
  the full create-time rules are in
  `${CLAUDE_PLUGIN_ROOT}/references/repo-brain.md` — read it if the common path
  above doesn't apply cleanly.
- Record the `agent_brain_id`; call it `ROOM`. Also note the org the brain
  lives in: the `org_id` you passed to `list_agent_brains` / `create_agent_brain`,
  or — if you passed none — the default org's `org_id` from `list_orgs` (the
  response's `scope` only carries `org_name`, not the id). Accounts in a single
  org can skip this.
- **Cache it — so capture routes to this room from the very next turn:**

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" set --brain-id "<ROOM>" --org-id "<ORG_ID>"
  ```

  The capture paths — the per-turn `Stop` flush, the commit/PR flush, and the
  `SessionEnd` backstop — also resolve the room themselves on a cache miss
  (exact-name lookup, then cached), so they only fall back to personal memory
  when no brain of this exact name is REACHABLE by that user — a teammate's
  unshared room is invisible to the hooks just as it is to the lookup above. Caching here is still what
  makes the FIRST capture after onboarding route without a lookup, and
  `--org-id` is what lets captures reach a room outside the caller's default
  org (a brain lives in exactly one org; without it the write fails with
  "Agent brain not found" in multi-org accounts, and the entry gets re-probed).
  It writes to `~/.config/memhub-plugin/rooms.json` — the user's own config,
  never the repo — and covers every worktree of this repo. Teammates run
  `/memhub:onboard` once themselves — which lands them in THIS room only if
  §1.5 shared it; otherwise their exact-name lookup misses and each of them
  creates a private room of the same name.

  Automatic capture needs **both** halves: the room (cached here, or resolved
  by the hooks) says *where* to write, and the key from §0 is *what*
  authenticates the write. Neither alone is enough, and each fails differently
  — a missing room writes to the wrong place, a missing key writes nowhere.

## 1.5 Share the room, or every teammate creates their own

**Only do this when you CREATED the room in §1.** Reused an existing one →
skip; it is already shared with you, and re-sharing it is not yours to decide.

A brain you just created is readable by you and nobody else. Not by the
workspace it lives in, not by your org — access is the creator plus explicit
shares. So an unshared repo room is invisible to every teammate's §1 lookup,
and the "teammates run `/memhub:onboard` themselves" flow above quietly
produces N private rooms named `Repo: <org>/<name>` instead of one shared one.

1. `list_workspaces(org_id=<ORG_ID>)` — the `<ORG_ID>` you recorded in §1,
   the org that owns the brain. Take the workspace with `is_default: true` and
   `is_personal: false`. Every member of the org is in that one, and there is
   exactly one per org. (A personal org has none — jump to the fallback.)

   **Pass the org.** The caller's default org follows whatever was last
   selected in the MemHub app, so on a multi-org account a bare
   `list_workspaces` returns a DIFFERENT org's workspaces — and the user would
   approve a workspace name without realising it belongs to another org.
   Single-org accounts can omit it.
2. **Ask before granting.** This hands the whole org access; it is the user's
   call. Name the workspace and get the access level in the same question:

   > `Repo: XTraceAI/xmem` is created, and right now only you can read it.
   > Share it with **Shared Workspace** so your teammates' onboarding finds
   > this room instead of making their own? `contributor` lets their sessions
   > capture into it — `viewer` is read-only.

   - **`contributor`** — their capture hooks can write into the room. This is
     what makes it shared team memory, and it is the answer that matches what
     onboarding is for.
   - **`viewer`** — read-only, and **it breaks their capture**. Say this
     BEFORE they choose. The room becomes reachable, so their hooks resolve it
     and cache it as the write target; every flush is then rejected, and
     because the client only retries unrouted when the server says the brain
     doesn't exist, the turn's memory is dropped rather than saved to personal
     memory. Offer it only for someone who should read the room, never for a
     teammate whose sessions should land in it.

   Declined → say the room is private and move on to §2. Never grant anyway.
3. On yes:

   ```
   share_agent_brain_with_workspace(
       agent_brain_id="<ROOM>",
       workspace_id="<default workspace_id>",
       permission="contributor",   # or "viewer", as answered
       org_id="<ORG_ID>",          # same org as the list_workspaces call
   )
   ```

   Report `granted_count` by name. It is a **snapshot** — whoever joins the org
   later needs a re-run — and it never changes an existing grant, so anyone
   already on the room comes back under `skipped`, which is not a failure.

**Fallback — never fail onboarding over this.** No default workspace, the tool
isn't available (a backend older than the release that added it), **or the call
returns any error at all**: say the room is private in plain words, name the
per-person path (`list_teammates`, then `share_agent_brain` per teammate), and
continue to §2. Never retry the share. A wrong or missing
share costs a duplicate room; a failed onboarding costs the user everything.

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
uv run --with 'mcp<2' python "${CLAUDE_PLUGIN_ROOT}/scripts/import_session.py" \
  --session "<session-id-or-path>" \
  --title "Onboarding seed — <org>/<repo>" \
  --agent-brain-id "<ROOM>"
```
Do NOT pass `--conversation-id`. Omitted, it defaults to the session's own id —
the one per-turn capture uses — so the seed lands as ONE conversation in this
room rather than a second copy of a session capture may have already sent. A
re-run is safe either way: the server's watermark makes re-imports incremental.

The script targets the plugin's default endpoint — **production**
(`api.memhub.xtrace.ai`) when installed as `memhub`, staging when installed as
`memhub-staging`. Do NOT pass `--url` to cross between them: `--url` overrides
only the endpoint, while the OAuth client id and Auth0 tenant still come from
the *installed* plugin's `.mcp.json`, so a prod install pointed at staging
authenticates with prod credentials against the staging tenant and fails.

Seeding a **staging** brain means running from the staging install, which is
XTrace-internal and not in the public marketplace — it lives in a second
marketplace inside the repo and installs from a local clone:

```text
/plugin marketplace add <path-to-repo>/plugins
/plugin install memhub-staging@memhub-internal
```

The two register the same MCP server name, so enable one or the other, never
both. See CONTRIBUTING.md for why the staging entry cannot be a `git-subdir`
source.

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

The response carries `scope.brains` — the brains recall actually read. Check it
before explaining an empty result, because the two causes need opposite answers:

- **`scope.brains` lists the room** → nothing has been extracted for those
  symbols yet. Capture may still be running (§3 latency), or the seed did not
  touch them. Say so; it is not a failure.
- **`scope.brains` is empty** → the room was never resolved, so recall never
  looked in it. Nothing will ever fire, however long you wait. Re-check §1.

## 5. Route — confirm discoverability
`search_brains("<a topic from the seed>")` → confirm `ROOM` appears, so the agent
can find this brain from any task.

## 6. Report the activation funnel + set the compounding habit
Print a compact funnel with real values:
- **Seeded** — records imported, `path`.
- **Digest** — rendered? version.
- **Directives fired** — count + one example (the aha); else "capture still
  running" or "room not resolved", whichever `scope.brains` says it is (§4).
- **Routed** — did `search_brains` surface the room?
- **Time-to-first-recall** — wall-clock from create → first directive fired (or
  "pending").

Set expectations honestly: the brain now helps **on the files this one session
touched** — coverage grows with every session.

**Tell them to restart Claude Code**, and what they will see when they do: every
session in this repo now opens with a line naming the brain, and the agent
receives its compiled overview as context before the first prompt. That is the
onboarding becoming permanent — it is also the fastest way for the user to
confirm §1 actually took, since the brief only appears once a room resolves.
From then on `/memhub:search-memory` searches this brain alongside personal
memory rather than personal memory alone.

Then the one CTA: **keep working — MemHub learns as you go.** Import sessions
after substantive work (`/memhub:import-session`), and (when available) enable
PR-merge memory so the brain compounds automatically.

Plain-English output throughout. If a step fails on authentication, send the
user to `/memhub:login`, not to `/mcp` — the hooks and the scripts here use the
plugin's own credential, and a connected `/mcp` says nothing about whether they
have one.

**On overview latency:** the digest normally lands via the async event trigger
fired by the import. `refresh_brain_overview` (step 3) runs it on demand when
that is slow, so a long wait is not a dead end — but the digest itself still
takes time on a large seed. Say so plainly rather than implying it hung.
