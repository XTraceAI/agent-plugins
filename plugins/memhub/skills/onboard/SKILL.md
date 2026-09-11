---
description: Use when a new user wants to set up MemHub / an agent brain for their repo, or asks to "onboard", "get started", "set up my brain", or "seed a brain from my work". Sets up the repo's agent brain (where team artifacts and specs land), seeds the user's own memory from a real Claude Code session, and proves proactive recall on the repo's own symbols — then reports an activation funnel.
argument-hint: [session-id-or-path]
allowed-tools: Bash, mcp__plugin_memhub_memhub__list_agent_brains, mcp__plugin_memhub_memhub__create_agent_brain, mcp__plugin_memhub_memhub__search_brains, mcp__plugin_memhub_memhub__recall_directives, mcp__plugin_memhub_memhub__ingest_document_from_url, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_agent_brains, mcp__plugin_memhub-staging_memhub__create_agent_brain, mcp__plugin_memhub-staging_memhub__search_brains, mcp__plugin_memhub-staging_memhub__recall_directives, mcp__plugin_memhub-staging_memhub__ingest_document_from_url, mcp__plugin_memhub-staging_memhub__list_orgs
---

**Plugin root:** commands below use `${CLAUDE_PLUGIN_ROOT}`. Claude Code and
Codex export it automatically; if it is unset (e.g. on Cursor), set it first to
this plugin's root — the ancestor directory of this skill file that contains
`.claude-plugin/` — with `export CLAUDE_PLUGIN_ROOT="<plugin-root>"`.

Onboard a new user onto MemHub for the repo they're in. This skill's one job is
to reach the **first useful recall** fast: set up the repo's room (where team
artifacts and specs land), seed the user's own memory from one piece of real
work, then prove recall fires on the repo's own code. Optimize for
**time-to-first-useful-recall**, not steps completed. Report an activation
funnel at the end.

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
and the capture hooks cannot run without it.

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
  teammate may have created it). **Only** `create_agent_brain` when there is no
  exact match — do NOT mint a second room for a repo that already has one, and
  give it a real one-line description.
- Edge cases (SSH remotes, no remote, worktrees, **not a git repo at all**) and
  the full create-time rules are in
  `${CLAUDE_PLUGIN_ROOT}/references/repo-brain.md` — read it if the common path
  above doesn't apply cleanly.
- Record the `agent_brain_id`; call it `ROOM`. Also note the org the brain
  lives in: the `org_id` you passed to `list_agent_brains` / `create_agent_brain`,
  or — if you passed none — the default org's `org_id` from `list_orgs` (the
  response's `scope` only carries `org_name`, not the id). Accounts in a single
  org can skip this.
- **Cache it — so artifacts and the session-start brief find this room:**

  ```bash
  python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" set --brain-id "<ROOM>" --org-id "<ORG_ID>"
  ```

  `/memhub:save-artifact`, `/memhub:spec` and the automatic `.md` spec capture
  write into this room, and every session in this repo opens with a line naming
  it. `--org-id` is what lets those writes reach a room outside the caller's
  default org (a brain lives in exactly one org; without it the write fails with
  "Agent brain not found" in multi-org accounts, and the entry gets re-probed).
  It writes to `~/.config/memhub-plugin/rooms.json` — the user's own config,
  never the repo — and covers every worktree of this repo. Teammates run
  `/memhub:onboard` once themselves.

  Session capture does NOT route here: every session, and the memory extracted
  from it, lands in the user's **personal memory**. What automatic capture needs
  is the key from §0.

## 2. Seed your memory — ONE substantive session
Seed from **exactly one** session, not many. One is enough to fire recall, and
it's the fastest path to the first aha — importing several only multiplies the
async extraction latency and *delays* it. **Quality over quantity:** pick the
**most recent session that did real code work** (touched actual files/symbols).
A trivial chat yields a gist but no directives — if the newest session is
trivial, say so and pick an earlier substantive one rather than seed noise. This
one session is the **first deposit**, not a finished memory (§4).

Import it via the helper script (never call `import_conversation` yourself; it
handles any size):

```bash
uv run --with 'mcp<2' python "${CLAUDE_PLUGIN_ROOT}/scripts/import_session.py" \
  --session "<session-id-or-path>" \
  --title "Onboarding seed — <org>/<repo>"
```
Do NOT pass `--conversation-id`. Omitted, it defaults to the session's own id —
the one per-turn capture uses — so the seed lands as the SAME conversation
rather than a second copy of a session capture may have already sent. It lands
in the user's personal memory, exactly where capture puts it. A re-run is safe
either way: the server's watermark makes re-imports incremental.

The script targets the plugin's default endpoint — **production**
(`api.memhub.xtrace.ai`) when installed as `memhub`, staging when installed as
`memhub-staging`. Do NOT pass `--url` to cross between them: `--url` overrides
only the endpoint, while the OAuth client id and Auth0 tenant still come from
the *installed* plugin's `.mcp.json`, so a prod install pointed at staging
authenticates with prod credentials against the staging tenant and fails.

Seeding on **staging** means running from the staging install, which is
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
Extraction (episodes, directives and the gist) then runs **in the background** —
minutes for a large session.

**Optional — breadth from the repo's specs (don't block the aha on it).** The one
session gives *depth* on recent work, but only covers the files it touched. For
*breadth* — the codebase's durable design intent — ingest a few key docs
(`README`, top `docs/specs/*.md`) into `ROOM` if they're reachable as URLs
(`ingest_document_from_url`). Offer this, but keep it optional and after the
session: it adds ingest latency, and `.md` specs are the highest-signal breadth
source (grep-hostile, hierarchical) when the user wants the room to help beyond
the one session's slice.

## 3. Prove proactive recall — the aha
Pick 2–3 concrete symbols the seeded work actually touched (from
`git ls-files | head` / recently-edited files / symbols named in the session).
For each, `recall_directives(entities=["<file-or-symbol>"], repo="<repo>")` and
show what fires. A returned lesson/procedure = the differentiated value: a rule
the agent will get **proactively when it touches that code**, without asking.

Recall reads the user's personal memory as well as the room, so directives from
the seed fire whether or not the room resolved. An empty result means nothing has
been extracted for those symbols yet — extraction may still be running (§2
latency), or the seed did not touch them. Say so; it is not a failure.

## 4. Report the activation funnel + set the compounding habit
Print a compact funnel with real values:
- **Room** — resolved or created, and cached?
- **Seeded** — records imported, `path`.
- **Directives fired** — count + one example (the aha); else "extraction still
  running".
- **Time-to-first-recall** — wall-clock from the seed import → first directive
  fired (or "pending").

Set expectations honestly: recall now helps **on the files this one session
touched** — coverage grows with every session, which capture ships on its own.

**Tell them to restart Claude Code**, and what they will see when they do: every
session in this repo now opens with a line naming the room, and — once the room
holds specs or artifacts — the agent receives its compiled overview as context
before the first prompt. That is the
fastest way for the user to confirm §1 actually took, since the brief only
appears once a room resolves. From then on `/memhub:search-memory` searches this
room alongside personal memory rather than personal memory alone.

Then the one CTA: **keep working — MemHub learns as you go.** Save specs and
design docs as artifacts (`/memhub:save-artifact`, `/memhub:spec`) so the room
holds what the whole team should see.

Plain-English output throughout. If a step fails on authentication, send the
user to `/memhub:login`, not to `/mcp` — the hooks and the scripts here use the
plugin's own credential, and a connected `/mcp` says nothing about whether they
have one.
