# The repo brain — naming, resolution, and creation rules

The canonical rules for turning a git repository into its MemHub agent brain
("the repo room"). Every skill that touches a repo brain follows this file.

Each skill states the common-path rule inline, so the normal case costs no
read. Come here for the edge cases and for the rules that apply whenever a
skill is about to CREATE a brain.

---

## 1. The name

```
Repo: <org>/<name>
```

Derived from `git remote get-url origin`. This name is a **lookup key** — a
brain is found by matching it EXACTLY, so every skill must derive it
identically. A one-character difference silently creates a second brain for
the same repo, and the two rooms never see each other's memory.

Normalization, in order:

1. Read `git remote get-url origin`.
2. Strip the transport and host. Both remote forms must produce the same
   result:
   - HTTPS — `https://github.com/XTraceAI/xmem.git` → `XTraceAI/xmem`
   - SSH — `git@github.com:XTraceAI/xmem.git` → `XTraceAI/xmem`
   - SSH URL — `ssh://git@github.com/XTraceAI/xmem.git` → `XTraceAI/xmem`
3. Strip a trailing `.git` and any trailing `/`.
4. Keep only the last two path segments (`<org>/<name>`). Self-hosted hosts
   can nest deeper (e.g. `gitlab.example.com/group/subgroup/repo`) — take
   `subgroup/repo`.
5. **Preserve case exactly as the remote gives it.** Do not lowercase.
   `XTraceAI/xmem` and `xtraceai/xmem` are different brains; the remote is
   the tiebreaker.
6. Prefix with `Repo: ` (one space).

Result: `Repo: XTraceAI/xmem`.

**A remote with no org** (`git@host:name.git`, common on self-hosted servers)
yields a one-segment `Repo: name`. Keep it — the remote is still stable across
clones and worktrees, which is the property the key needs. Do NOT substitute
the no-remote fallback below: that keys on a local directory basename, which
differs between clones and would split the room. Note the resulting name is
shaped like the no-remote form, so a no-remote repo whose directory happens to
share that basename lands in the same room; prefer `room_map.py name` over
deriving by hand so every caller at least agrees.

## 2. Edge cases

**Worktrees and subdirectories.** All worktrees of a repo, and any
subdirectory within it, resolve to the SAME brain — because `origin` is the
same. Never derive the name from the current directory when a remote exists.
This is why the remote, not the path, is the source of truth. The no-remote
fallback below preserves this guarantee by keying on the main worktree rather
than the current one.

**Monorepos.** One repo is one brain. Do not invent per-package brains; the
package is a detail inside the room, not a room of its own.

**Multiple remotes.** Use `origin`. If `origin` is missing but other remotes
exist, do NOT guess which is canonical — ask the user which remote to use,
or apply the no-remote rule below if they don't care.

**No remote, but inside a git repo.** Derive from the MAIN worktree, never
from the current directory — in a linked worktree `git rev-parse
--show-toplevel` returns *that worktree's* path, so every worktree of one
repo would get a different name:

```sh
git rev-parse --path-format=absolute --git-common-dir   # → /path/to/repo/.git
```

Do NOT blindly take the parent directory — that only works for the standard
layout. Normalize: if the last path component is exactly `.git`, drop it;
then strip a trailing `.git` extension from what remains; then take the
basename.

```
/path/to/repo/.git   → /path/to/repo   → repo    (standard worktree)
/path/to/repo.git    → /path/to/repo   → repo    (bare repo)
/path/to/repo        → /path/to/repo   → repo    (custom GIT_DIR)
```

Taking the parent unconditionally would name the brain after the CONTAINING
directory in the latter two cases — a wrong lookup key, which mints an
unfindable room.

```
Repo: <basename>
```

**This name can never match a remote-derived `Repo: <org>/<name>`** — it has
one segment where that has two, and there is no way to recover the org
without a remote. So it is a genuinely DISTINCT brain, not the same room
under a shorter name. Say so out loud and confirm before creating one: if the
repo has a remote anywhere else (a teammate's clone, CI), their room is the
two-segment one, and creating this would fork the repo's memory in exactly
the way §1 warns about.

**Not a git repository at all.** Do NOT invent a repo name. Fall back to
plain workspace memory (omit `agent_brain_id` entirely), and **tell the user
that's what happened** — e.g. "not in a git repo, so this went to your
workspace memory rather than a repo brain." Silently inventing a brain name
here is how unfindable one-off brains get created. MemHub is used outside
code repos (meetings, documents, research); that path is legitimate and must
not be forced into a repo shape.

## 3. Resolve before you create — ALWAYS

Creating a brain is the last resort, never the first move.

1. Derive the name (§1).
2. `list_agent_brains` → look for an **exact-name match**. Reuse that
   `agent_brain_id` if found — a teammate may have created the room and
   shared it with you, and theirs is the right one.
3. No exact match → before creating, run `search_brains` with the repo or
   topic in natural language. An existing brain may hold this subject under
   a different name; prefer it over minting a near-duplicate.
4. Only when both come back empty: `create_agent_brain` (omit `workspace_id`
   — §3.1), and then **share it** (§3.2), or you have just created the
   duplicate the next step warns about.

Duplicate brains are the main way a MemHub org degrades: cross-brain routing
ranks brains by their overview, so several near-identical rooms on one
subject make the right one harder to find for every future search.

**Two lookups coming back empty does NOT prove the room doesn't exist.** Both
of them see only brains you can already reach — ones you created, and ones
explicitly shared with you. Being in the workspace a brain lives in reaches
nothing. So a teammate's repo room that was never shared is invisible to step
2 AND step 3, and every teammate in turn falls through to step 4 and mints
their own copy of the same room, each capturing into a room the others cannot
read. That is why §3.2 is not optional politeness — it is the half of creation
that makes step 2 work for the next person.

### 3.1 Where a new brain lives

Omit `workspace_id`, so the brain lands in your own personal workspace.

The reason is *not* that this preserves your access — it is that a repo room
is yours until you decide who else gets it, and the personal workspace is the
neutral default. **Where a brain lives has nothing to do with who can read
it.** You are its creator, which makes you admin on it wherever it is homed,
so nothing about sharing depends on this choice either way.

Homing it personally costs nothing at share time: only the *target* of a share
has to be a team workspace. A brain in your personal workspace can be shared
with the org's shared workspace exactly like any other.

### 3.2 A brain you just created is readable by you and NOBODY else

Not by the workspace it lives in. Not by your org. Access is the creator plus
explicit shares, and nothing else. If you report a new repo room as "set up
for the team" without sharing it, that statement is false in the direction
nobody notices — everything looks fine and exactly one person can read it.

A repo room is meant to be the team's, so share it with the org's **default
workspace** — every member of the org is in it, and it is the one
`list_workspaces` returns with `is_default: true` and `is_personal: false`
(there is exactly one per org, fixed when the org was created).

**Who actually performs the share.** Granting a whole org access needs a user
present to approve it, so it belongs to `/memhub:onboard`, which is a
deliberate setup flow with someone reading the answer. A skill that creates a
room in the MIDDLE of another task — `/memhub:spec`, `/memhub:pr-babysit`, and
any automatic capture path — must NOT share on its own: nobody is there to
consent, and a grant is not a side effect to slip into a babysit pass. Those
callers do the other half instead: say in one line that the room is private
and point at `/memhub:onboard`. Everything below is for the flow that shares.

**Confirm before granting.** Name the workspace and ask for the access level
in the same breath — this hands a whole org access, which is the user's call,
not yours:

> Created `Repo: XTraceAI/xmem` — right now only you can read it. Share it
> with **Shared Workspace** (7 members) so your teammates' rooms resolve to
> this one? `contributor` lets their sessions capture into it; `viewer` is
> read-only.

- **`contributor`** — teammates' capture hooks can WRITE into the room. This
  is what makes it shared team memory rather than a room they can only watch.
- **`viewer`** — read-only. Their own sessions keep landing in personal
  memory, so the duplicate-room problem is solved but the shared-capture one
  is not. Say that when they pick it.

On yes:

```
share_agent_brain_with_workspace(
    agent_brain_id="<ROOM>",
    workspace_id="<the default workspace_id>",
    permission="contributor",   # or "viewer", as answered
)
```

Two properties worth repeating to the user, because both surprise people:

- **It is a snapshot, not a live link.** Whoever joins the org afterwards gets
  nothing; the tool has to be run again to include them.
- **It never changes an existing grant.** Someone already at `admin` stays at
  `admin`; the response reports them under `skipped` with a reason rather than
  failing.

**When it can't be done, degrade — never fail the caller's task over it:**

- No workspace with `is_default: true` comes back (a personal org has none) →
  say the room is private and name the per-person path: `list_teammates`, then
  `share_agent_brain` for each teammate.
- `share_agent_brain_with_workspace` is not available at all (a backend older
  than the release that added it) → same fallback, same one line. Do not retry
  it, and do not stop what you were doing.

In both cases say the room is private in plain words. Silence here is what
produces a "shared" room that isn't.

## 4. Cache the resolution — resolve once, route from then on

Once §3 gives you an id, **persist it** so later writers don't redo the lookup:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" set --brain-id "<ROOM>" --org-id "<ORG_ID>"
```

`--org-id` is the org that owns the brain — the `org_id` you passed to
`list_agent_brains` / `create_agent_brain`, or the default org's `org_id` from
`list_orgs` when you passed none (the response's `scope` carries only
`org_name`). Single-org accounts can omit it; multi-org accounts must not,
because a brain lives in exactly one org and the caller's default org follows
whatever was last selected in the MemHub app. An entry cached without it is
re-probed (rate-limited) until the org is known.

That writes `~/.config/memhub-plugin/rooms.json` — the plugin's per-user state
dir, alongside the OAuth token cache. **Never inside the repo.** A brain id is
account state, not project state: writing it into the working tree would push a
private id into whatever repo the user happens to be in, including public ones,
and force every user to decide whether to commit it.

Entries are keyed by the repo's room name (§1), then by backend (`production` /
`staging` hold different ids for the same repo, so a single flat id would write
to the wrong brain on whichever install didn't match).

Read it back — bare id on stdout, exit 1 and silence when nothing is cached:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" show
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/room_map.py" name   # the §1 name
```

Why this exists: the AUTOMATIC capture paths — the per-turn Stop flush, the
SessionEnd hook and the commit/PR flush — run with no model in the loop. Before
the cache they passed only `namespace` and their memories landed in **personal
memory, never in the room**. Today the hooks resolve the room themselves on a
cache miss (`brain_resolve.resolve_repo_brain` does the exact-name lookup and
caches the answer), so capture only falls back to personal memory when no brain
of the repo's exact name exists on this backend. The cache is what makes that
resolution a once-per-repo cost, records the org that owns the room
(`set --org-id`, needed for writes outside the caller's default org), and
collapses five skills' worth of independent re-derivation into one answer,
which is the drift §1 warns about.

`import_session.py` and `save_artifact.py` read it automatically when
`--agent-brain-id` is not passed (`--no-room` opts out), so a plain invocation
lands in the room.

Because the key is the room NAME (derived from the remote), every worktree and
subdirectory of a repo shares one entry automatically, with no dependence on
which branch is checked out. Each teammate resolves once on their own machine —
one `list_agent_brains` call — which is the price of keeping the id out of the
working tree.

## 5. Every brain you create needs a real description

`create_agent_brain` accepts a `description`. It is not decoration — it is
the text an agent reads when choosing between brains, and a brain with no
description is effectively invisible when picking from a list.

Write one line answering **what questions this brain can answer**. Name the
subject and the kind of content.

- Good — "Shared room for the xmem repo: specs, PR babysit sessions,
  reviews, and imported implementation sessions."
- Useless — "xmem stuff", "notes", or an empty description.

## 6. Say where things landed

After any write, tell the user which brain received it, by name:

> Saved to `Repo: XTraceAI/xmem`.

Routing that happens silently reads as losing things. One line keeps
automatic placement trustworthy, and lets the user correct a wrong
destination immediately rather than discovering it weeks later.
