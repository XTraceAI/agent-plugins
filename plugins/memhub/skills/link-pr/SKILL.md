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
- `--session <id>` (repeatable) = the session(s) to link. Omit and step 3
  resolves the running one.
- `--unlink` = remove the link instead of creating it.

## 1. Resolve the pull request

- A full URL in `$ARGUMENTS` is used as-is.
- A bare number resolves against the current repo: `gh pr view <n> --json url -q .url`.
- No argument → the current branch's PR:
  `gh pr view --json url,number,state,headRefName -q .url`. If that fails
  (no PR for this branch, not a repo, `gh` unauthenticated), **ask** which PR
  they mean rather than guessing.

Normalise to `https://github.com/<owner>/<repo>/pull/<n>` — no trailing
slash, no query, no fragment; `www.github.com` is `github.com`. **Only
github.com pull requests can be linked**: the server refuses any other host
("Expected an exact pull request URL…"), so GitHub Enterprise is not
supported yet. For an enterprise PR (`https://ghe.corp/o/r/pull/7`) do not call
`link_pr`, and do not rewrite the host to `github.com` — that names a different
pull request. Tell the user enterprise GitHub cannot be linked in MemHub yet
and stop.

## 2. Resolve the sessions

`--session <id>` wins, and is used verbatim. Otherwise ask the plugin which
session is running:

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

**Classifying the pull request is a separate, narrower claim.** `link_pr` also
takes `pr_type` (`feat`, `fix`, `chore`, `docs`, `perf`, `refactor`, `other`)
with `classification_session_id`, and the server accepts it only alongside
`link_source="session_self"` — the classification has to come from a session
that did the work, not from a person pointing at one. So:

- **This session opened or wrote the PR** (you are linking it to itself):
  send `link_source="session_self"`, `classification_session_id=<that same
  session id>` and the `pr_type` you judge from the actual change.
- **Anything else** — a session the user named, a `--session` id, an unlink:
  send no `pr_type`. Do not guess a type for work you did not see; the first
  classification wins and an identical retry preserves it, while a different
  one conflicts.

## 4. Report the reply honestly

Relay what the server actually said; do not re-word a partial result into a
success it does not claim.

- `linked[]` entry with `created: true` → "linked".
- `upgraded: true` → "upgraded an old inferred link to a confirmed one".
- `skipped: already_linked` → "was already linked" — not a failure, and not a
  new link either.
- `skipped: session_not_found` → the session is not in MemHub yet or belongs to
  someone else. If it may simply not have been captured, say so and offer
  `/memhub:import-session <id>`.
- An error reaches you as the server's message only — match on its wording:
  - "…has no active GitHub connection…" or "That repository is not part of
    this organization's GitHub installation." → relay the message verbatim
    (the first names where to connect GitHub when the server has that URL
    configured). **Do not retry** — nothing here can fix it; an admin connects
    GitHub or adds the repo in MemHub.
  - "GitHub did not answer…" or "GitHub could not be reached…" → nothing was
    written; retrying is safe. Retry once, then relay it.
  - "GitHub returned no pull request…" immediately after `gh pr create` →
    GitHub may not have published the PR yet. Suggest re-running in a moment;
    do not loop on it. Otherwise relay it: the PR may not exist, or the GitHub
    App may no longer be allowed to read it.
  - "This PR already has a different classification…" → the first
    classification stands. Re-send the link without `pr_type`; do not argue
    the type.
  - "The classification session was not found among your sessions." → that
    session is not in MemHub yet (or not the user's), so nothing was linked.
    Offer `/memhub:import-session <id>`, or re-send without `pr_type`.
  - "Expected an exact pull request URL…" → the URL is malformed or not on
    github.com; fix the spelling (step 1) or, for an enterprise host, stop.
- A successful link (created or already linked) also clears the user's own
  dismissal of that PR in MemHub, so it is no longer hidden from them.

**Mention the payoff once, and only if it happened**: when a link was actually
created and the org has PR session insights on, the PR's MemHub comment
refreshes on its own within a minute. Do not promise that if the reply created
nothing.

Never call the REST endpoint directly. The MCP tools are the model-facing
surface and they carry the org resolution; a hand-rolled request would skip it.
