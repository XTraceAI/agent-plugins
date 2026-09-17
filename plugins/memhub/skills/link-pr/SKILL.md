---
description: Use when the user wants to link a coding session to a GitHub pull request in MemHub, or to undo such a link (e.g. "link this session to PR 42", "/memhub:link-pr", "attach my work to this PR", "unlink that session from the PR"). Records the link as confirmed, so the PR's session context is published from facts rather than a branch-name guess.
argument-hint: [pr-number-or-url] [--session <id>...] [--unlink]
allowed-tools: Bash, mcp__plugin_memhub_memhub__link_pr, mcp__plugin_memhub-staging_memhub__link_pr, mcp__plugin_memhub_memhub__unlink_pr, mcp__plugin_memhub-staging_memhub__unlink_pr, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_orgs
---

**Plugin root:** Resolve this skill's plugin root once: it is the ancestor of
this file containing `plugin.json` and the `scripts/` directory. A trusted host
variable such as `CLAUDE_PLUGIN_ROOT` or `CURSOR_PLUGIN_ROOT` may already point
there; use it only when it resolves to that same ancestor. Substitute the
resulting absolute path as `<plugin-root>` below; do not infer it from the
workspace cwd. Commands show `python3`; on native Windows use `py -3`.

Link a coding session to a pull request, so the PR's session context is
published from a **confirmed fact** instead of a branch-name inference. A pull
request has many sessions and a session has many pull requests — linking is
additive, and linking one session never displaces another.

This is also the answer whenever the automatic path could not see what
happened: a PR opened by a script, a Makefile target, a CI helper, `hub
pull-request`, or a GitHub MCP tool on a host where MCP calls are not
dispatched to hooks. None of those are detected, deliberately; this skill is
one command away from the link they would have made.

Arguments: `$ARGUMENTS`
- First token = a PR number or full URL (optional).
- `--session <id>` (repeatable) = the session(s) to link. Omit and step 2
  resolves the running one.
- `--unlink` = remove the link instead of creating it.

## 1. Resolve the pull request

- A full URL in `$ARGUMENTS` is used as-is.
- A bare number resolves against the current repo: `gh pr view <n> --json url -q .url`.
- No argument → the current branch's PR:
  `gh pr view --json url,number,state,headRefName -q .url`. If that fails
  (no PR for this branch, not a repo, `gh` unauthenticated), **ask** which PR
  they mean rather than guessing.

Normalise to `https://<host>/<owner>/<repo>/pull/<n>` — no trailing slash, no
query, no fragment. **Keep the host the user gave you.** Most PRs are on
`github.com`, but an enterprise PR (`https://ghe.corp/o/r/pull/7`) is equally
valid and the hook already passes those to the same backend; rewriting the host
to `github.com` would name a different pull request, and rejecting it would
leave enterprise users — including every Cursor user, for whom this skill is
the only path — with no way to link at all.

## 2. Resolve the sessions

`--session <id>` wins. Strip leading and trailing whitespace off each one —
the server strips them inside `session_ids` but not in the classification
argument step 4 sends, so a padded id links fine and then fails to classify.
Otherwise ask the plugin which session is running:

```bash
python3 "<plugin-root>/scripts/capture.py" current --json
```

Read the **exit code**, not just the output:

- **0** → use the reply's `conversation_id`.
- **4** (ambiguous — several live sessions in this directory, or sessions from
  more than one host) → show the candidates it listed and **ask** which. Two
  agents in one worktree is real, and picking the newest would link the wrong
  one.
- **3** (none found) → run `python3 "<plugin-root>/scripts/capture.py" list
  --limit 20` and ask which session they mean.

**Never invent a session id, and never pass a raw Codex or Cursor UUID.** The
server matches on the conversation id capture already sent — bare for Claude
Code, `codex-<uuid>` for Codex, `cursor-<uuid>` for Cursor — and `capture.py`
returns exactly that namespaced form. A bare UUID from Codex matches nothing
and fails silently as "session not found".

## 3. Link

```
link_pr(pr_url="…", session_ids=["…"], link_source="manual")
```

`--unlink` calls `unlink_pr` with the same `pr_url` and `session_ids` instead.
Use `link_source="manual"` here — this skill is a person saying so, which is
what that value means. (`session_self` is the hook's, `session_found` is
`/memhub:find-contributing-sessions`'s.)

## 4. Record the work type

Skip this entire step when `--unlink` was used: unlinking does not clear a
type, and implying otherwise would be a lie.

Read `pr.pr_type` on the reply you just got from step 3. **If it is not null,
this pull request already has a type** — say which one, and go straight to
step 5. The first decision is permanent (MemHub has no edit path), so a second
one is refused and there is nothing to add.

If it is null, ask the user which of these the pull request primarily is:

```
feat — a new capability or behavior
fix — corrects existing faulty behavior
chore — maintenance, tooling, dependencies, tests, build/CI, style
docs — documentation changes
perf — performance improvements
refactor — structural change that preserves intended behavior
other — a deliberate choice when none of the above fit
```

This skill is a **human** saying so, so the type is theirs — do not pick one on
their behalf. If they have no strong view, `other` is the honest answer rather
than a guess; it cannot be corrected later. A PR that does several things gets
its primary purpose; never more than one.

Then send a SECOND call naming **one** session. The id you name is recorded
permanently as the classification's author, so choose it deliberately: use the
session step 2 resolved as the running one, and when several `--session` ids
were linked and the running one is not among them, **ask** which session the
type belongs to rather than picking the first.

```
link_pr(pr_url="…", session_ids=["<that one id>"], link_source="session_self",
        pr_type="<the chosen type>", classification_session_id="<that same id>")
```

Three things about this call that will otherwise look like bugs:

- **`link_source="session_self"`, not `"manual"`.** The server refuses
  classification from any other value. This does not relabel anything: the row
  written in step 3 keeps its `manual` source (a confirmed link is never
  rewritten), and the classification table has no `link_source` column at all.
- **`classification_session_id` must equal its `session_ids` entry and carry
  no leading or trailing whitespace.** The server strips each `session_ids`
  entry but compares `classification_session_id` raw, so sending `"  abc  "` in
  both places still fails with `classification_session_id must name one of
  session_ids`. Strip any id a user passed via `--session` before using it
  here.
- **`skipped: already_linked` with `linked: []` is SUCCESS here**, not a
  failure. The session was linked in step 3; this call exists only to carry the
  type. Confirm it by reading `pr.pr_type` on the reply.

If it fails saying the session **"was not found among your sessions"**, capture
has not caught up: wait 10s and retry the identical call once. If that fails
too, say the link from step 3 stands but the type was not recorded. Never drop
the type to force it through — an unarrived session links nothing either way,
so that retry buys nothing.

If it fails saying the PR **"already has a different classification"**, someone
classified it between step 3 and now. The server refuses the **whole write** on
that error, so nothing was lost here only because step 3 already made the link —
report the type that is there and do not try another one.

## 5. Report the reply honestly

Relay what the server actually said; do not re-word a partial result into a
success it does not claim.

- `linked[]` entry with `created: true` → "linked".
- `upgraded: true` → "upgraded an old inferred link to a confirmed one".
- `skipped: already_linked` → "was already linked" — not a failure, and not a
  new link either.
- `skipped: session_not_found` → the session is not in MemHub yet or belongs to
  someone else. If it may simply not have been captured, say so and offer
  `/memhub:import-session <id>`.
- error `github_not_connected` / `repo_not_in_install` → relay the message and
  the `connect_url` verbatim. **Do not retry** — nothing here can fix it; an
  admin connects GitHub or adds the repo in MemHub.
- error `feature_disabled` → PR linking is not enabled for this org yet. Stop.
- `pr_not_found` immediately after `gh pr create` → GitHub may not have
  published the PR yet. Suggest re-running in a moment; do not loop on it.

**Mention the payoff once, and only if it happened**: when a link was actually
created and the org has PR session insights on, the PR's MemHub comment
refreshes on its own within a minute. Do not promise that if the reply created
nothing.

Never call the REST endpoint directly. The MCP tools are the model-facing
surface and they carry the org resolution; a hand-rolled request would skip it.
