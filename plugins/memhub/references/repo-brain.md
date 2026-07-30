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
   `agent_brain_id` if found — a teammate may have created the room, and
   theirs is the right one.
3. No exact match → before creating, run `search_brains` with the repo or
   topic in natural language. An existing brain may hold this subject under
   a different name; prefer it over minting a near-duplicate.
4. Only when both come back empty: `create_agent_brain` (omit
   `workspace_id` so it lands in your own workspace and you keep the
   contributor access that sharing requires).

Duplicate brains are the main way a MemHub org degrades: cross-brain routing
ranks brains by their overview, so several near-identical rooms on one
subject make the right one harder to find for every future search.

## 4. Every brain you create needs a real description

`create_agent_brain` accepts a `description`. It is not decoration — it is
the text an agent reads when choosing between brains, and a brain with no
description is effectively invisible when picking from a list.

Write one line answering **what questions this brain can answer**. Name the
subject and the kind of content.

- Good — "Shared room for the xmem repo: specs, PR babysit sessions,
  reviews, and imported implementation sessions."
- Useless — "xmem stuff", "notes", or an empty description.

## 5. Say where things landed

After any write, tell the user which brain received it, by name:

> Saved to `Repo: XTraceAI/xmem`.

Routing that happens silently reads as losing things. One line keeps
automatic placement trustworthy, and lets the user correct a wrong
destination immediately rather than discovering it weeks later.
