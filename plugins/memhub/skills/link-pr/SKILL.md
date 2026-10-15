---
description: Link your coding sessions to a GitHub pull request in MemHub so the team sees the real work behind it — or find which of your past sessions on this machine wrote a pull request and link the ones you approve. Use when the user says e.g. "link this session to PR 42", "/memhub:link-pr", "attach my work to this PR", "unlink that session from the PR", "which sessions wrote this PR", "find the sessions behind my PRs", "map my unlinked PRs to sessions", "link the sessions that built PR 42". Every link is recorded as confirmed, so the PR's session context comes from facts rather than a branch-name guess; found sessions are only candidates until you say yes.
argument-hint: "[pr] | --session <id>... [pr] | --unlink [--session <id>...] [pr] | --find [pr-or-url ...]"
allowed-tools: 'Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capture.py" current *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/capture.py" list *), Bash(python3 "${CLAUDE_PLUGIN_ROOT}/skills/link-pr/scripts/find_sessions.py" *), Bash(gh pr view *), Bash(gh repo view *), Bash(gh search prs *), AskUserQuestion, mcp__plugin_memhub_memhub__link_pr, mcp__plugin_memhub-staging_memhub__link_pr, mcp__plugin_memhub_memhub__unlink_pr, mcp__plugin_memhub-staging_memhub__unlink_pr, mcp__plugin_memhub_memhub__list_my_unlinked_prs, mcp__plugin_memhub-staging_memhub__list_my_unlinked_prs, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_orgs'
---

**Plugin root:** Resolve this skill's plugin root once: it is the ancestor of
this file containing the `scripts/` directory and a plugin manifest
(`.claude-plugin/plugin.json` or `plugin.json`). A trusted host
variable such as `CLAUDE_PLUGIN_ROOT` or `CURSOR_PLUGIN_ROOT` may already point
there; use it only when it resolves to that same ancestor. Substitute the
resulting absolute path as `<plugin-root>` below; do not infer it from the
workspace cwd. Commands show `python3`; on native Windows use `py -3`.

Link coding sessions to a pull request, so the PR's session context is
published from a **confirmed fact** instead of a branch-name inference. A pull
request has many sessions and a session has many pull requests — linking is
additive, and linking one session never displaces another.

This is also the answer whenever the automatic path could not see what
happened: a PR opened by a script, a Makefile target, a CI helper, `hub
pull-request`, or a GitHub MCP tool on a host where MCP calls are not
dispatched to hooks. None of those are detected, deliberately; this skill is
one command away from the link they would have made.

Arguments: `$ARGUMENTS` — one of four modes. Every mode ends in the same
`link_pr` (or `unlink_pr`) call; only `link_source` differs.

| Mode | What it does | `link_source` |
|---|---|---|
| `/memhub:link-pr [pr]` | link the session running now | `manual`, or `session_self` when this session wrote the PR (step 2b) |
| `/memhub:link-pr --session <id>... [pr]` | link the session(s) you name (repeatable) | `manual` |
| `/memhub:link-pr --unlink [--session <id>...] [pr]` | remove a link instead of creating it | — |
| `/memhub:link-pr --find [pr-or-url ...]` | find which of your past sessions on this machine wrote the PR(s), and link the ones you approve | `session_found` |

`[pr]` is a PR number or full URL; with none, the current branch's PR. After
`--find`, any number of PRs may follow (`--pr <url-or-number>` is accepted as
the same thing); with none, `--find` maps **your own unlinked PRs** (step 3.1).

**Asking the user.** Where this says "ask", use AskUserQuestion on Claude Code;
Codex and Cursor have no such UI, so ask the same question as a short numbered
list in plain text and wait for the reply.

## 1. Resolve the pull request

- A full URL is used as-is.
- A bare number resolves against the current repo:
  `gh pr view <n> --json url -q .url`. If that fails, **ask** which PR they
  mean rather than guessing.
- No PR, outside `--find` → the current branch's PR:
  `gh pr view --json url,number,state,headRefName -q .url`. If that fails
  (no PR for this branch, not a repo, `gh` unauthenticated), **ask** which PR
  they mean rather than guessing. (No PR with `--find` → step 3.1.)

Normalise every URL to `https://github.com/<owner>/<repo>/pull/<n>` — no
trailing slash, query, fragment or tab suffix (`/files`, `/commits/…`);
`www.github.com` is `github.com`. **Only github.com pull requests can be
linked**: the server refuses any other host ("Expected an exact pull request
URL…"), so GitHub Enterprise is not supported yet. For an enterprise PR
(`https://ghe.corp/o/r/pull/7`) do not call `link_pr`, and **do not rewrite the
host to `github.com`** — that names a different pull request. Tell the user
enterprise GitHub cannot be linked in MemHub yet: outside `--find`, stop; with
`--find`, the scanner skips it as `unsupported_host` and you report it with
the other skipped PRs.

Then: `--find` → step 3. Every other mode → step 2.

## 2. Link the running session, or the ones named

### 2a. Resolve the sessions

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

### 2b. Link

```
link_pr(pr_url="…", session_ids=["…"], link_source="manual")
```

`--unlink` calls `unlink_pr` with the same `pr_url` and `session_ids` instead.
Use `link_source="manual"` here — a person saying so is what that value means.

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

Then report (step 4).

## 3. `--find`: which of my sessions wrote this PR?

A pull request is usually written across several sessions and opened from one
of them. That last session links itself; the others are what `--find` finds.
It searches **local session history on this machine only** — Claude Code,
Codex and Cursor transcripts under this user's home — so it finds the user's
own work, never a teammate's.

Everything it produces is a **candidate**. Nothing is linked without an
explicit yes.

### 3.1 Which pull requests

**PRs were named** → resolve each as in step 1 and go to 3.2 — the list tool
is not called.

**Nothing was named — ask which, then list my unlinked PRs.**

1. Work out what can be offered:
   - **This repo** — when `gh repo view --json nameWithOwner -q .nameWithOwner`
     succeeds here; remember that `<owner>/<name>`.
   - **Just this branch's PR** — when
     `gh pr view --json url,number,state -q '[.url,.number,.state]|@tsv'`
     returns a PR whose state is `OPEN`.
   - **All my repos** — always.
2. **Ask once** which one: "This repo (<owner/name>)", "All my repos", and
   "Just this branch's PR (#n)" when there is one. Ask even when only "All my
   repos" is on offer — it says what is about to be scanned.
3. "Just this branch's PR" → 3.2 with that one URL.
4. Otherwise call
   `list_my_unlinked_prs(limit=25)` — adding `repo="<owner/name>"` for "This
   repo". It returns the caller's own PRs that have **no confirmed session
   link**, newest first, with PRs the user dismissed in MemHub already left
   out. Do not pass `state`, `since` or `include_dismissed`.
5. Read the reply, **in this order**:
   - **The tool is not available** (not in your tool list, or an unknown-tool
     error) **or it failed** → say in one sentence that this MemHub backend
     cannot list unlinked PRs yet (or the error, briefly), and use the
     fallback below.
   - **`github_connected: false`** → the org has no usable GitHub connection,
     and `link_pr` would refuse every PR — whatever else the reply says. Relay
     that with the `connect_url` and **stop**.
   - **`github_identity_linked: false`** → MemHub does not know which GitHub
     account is theirs, so it cannot tell which PRs they authored. Say so
     once, give the `connect_url` verbatim as where to link it, and use the
     fallback below.
   - **`prs` is empty** → "no unlinked PRs of yours in <scope>". Stop.
   - Otherwise take every `prs[].html_url` into 3.2. Keep each PR's
     `legacy_branch_sessions` for 3.3, and the reply's `offset`, `count`,
     `total` and `has_more` for 3.5.

**Fallback: the user's own GitHub CLI.** `gh` cannot know MemHub's links or
dismissals, so the user picks.

```bash
gh search prs --author=@me --limit 30 --sort created --order desc \
  --json url,title,repository,state,createdAt
```

Add `--repo <owner/name>` when they chose "This repo". If `gh` is missing,
unauthenticated or fails, **ask** them to paste the PR URLs or numbers instead
and resolve those as in step 1.

Show a numbered list — `n. owner/repo#num — title (state, created date)` — and
ask which to map: numbers, ranges, `all` or `none`. This is always a text
reply, on every host: there can be thirty rows, and AskUserQuestion holds four.
Take at most 25 of the chosen URLs into 3.2.

### 3.2 Collect the facts and scan — in the terminal, never in context

Hand the URLs to the scanner, one `--pr-url` each. It reads each PR's head
branch, base branch, creation date, files and commits with `gh` **by URL** —
never by bare number, because `gh pr view <n>` resolves the number against the
CURRENT checkout and would rank sessions against a different pull request than
the one being linked. It checks that the `url` `gh` answers with is the one it
asked about, pages the file list through `gh api … --paginate` when a PR has
100 files or more, and then parses every local transcript **once**, scoring it
against every PR.

```bash
python3 "<plugin-root>/skills/link-pr/scripts/find_sessions.py" \
  --pr-url "<url 1>" --pr-url "<url 2>"
```

A full batch reads 25 PRs from GitHub and a few hundred transcripts, which
takes tens of seconds to a few minutes. Give the command a **long timeout**
(ten minutes on Claude Code's Bash tool; the longest your host allows
elsewhere), and do not start it in the background and then guess its result.
(`--prs-from <file>`, one URL per line, takes the same list from a file.)

It prints one JSON object: `prs[]` in the order given — each with `pr_url`,
`repo`, `number`, `title`, `head_ref`, `base_ref` and ranked `candidates` —
plus `skipped_prs[]` (`{pr_url, reason}`) and `sessions_scanned`. It never
prints transcript text. Candidates are already limited to sessions in that
PR's repository, because file paths match by suffix and an unrelated project's
`README.md` must not take a slot from someone who wrote the code.

**Read stderr and `skipped_prs` too.** A transcript too large to parse is not
scanned (`note: N session(s) larger than … were not scanned`) — tell the user
which, since a long session is exactly the kind that did the work. A
`files_truncated` note means that PR was scored on part of its file list.
Report skipped PRs by reason: `gh_failed` (gh could not read it), `url_mismatch`
(gh answered for a different PR), `url_redirected` (the repo was renamed or
transferred — the entry carries `canonical_url`; say so and offer to map that
URL instead), `no_files`, `invalid_url`, `over_cap`, `unsupported_host` (not
on github.com — MemHub cannot link enterprise GitHub PRs yet). Exit
code 2 with `gh not found` means the GitHub CLI is required to read PR facts —
say so and stop.

What a candidate's score is made of, so you can explain a row rather than
quote a number:

| Signal | Points |
|---|---|
| a PR commit sha appears in the session | 5 — this is proof, not inference |
| each distinct PR file the session edited | 2, capped at 8 |
| the session ran on the PR's head branch | 3 |
| the session was last active between 30 days before the PR was opened and a day after | 1 |
| ran on the base branch only, never the head, and edited none of the files | −5 |

(A single known PR can still be scanned the old way —
`--files-from <file> --branch … --base … --sha … --created-at … --repo …` —
and that mode is unchanged. `--find` uses the batch mode above.)

### 3.3 One consolidated review

1. **Already linked.** PRs that came from `list_my_unlinked_prs` have no
   confirmed link by definition. **Every other PR** — named after `--find`,
   this branch's PR, or picked from the `gh` list — gets
   `link_pr(pr_url="…")` with **no** `session_ids` first: the probe. Its
   `linked_sessions` mixes confirmed links with MemHub's old branch guesses:
   drop from that PR's candidates only the rows whose `link_source` is **not**
   `branch`, mentioning them as "already linked". A `branch` row is a guess
   that still needs a real link — treat it exactly like a
   `legacy_branch_sessions` entry in the next step. If the probe says
   `github_connected: false` or `repo_in_install: false`, drop that PR and
   relay the reason (and `connect_url`) once.
2. **MemHub's branch guesses.** Compare each legacy entry's (and each probe
   `branch` row's) **`session_id`**
   with the candidates' `conversation_id` — both are the capture client's
   session id. (`conv_id` is MemHub's own row id: it never equals a
   candidate, and `link_pr` does not accept it.) A matching candidate gets the
   note "MemHub guessed this from the branch". Legacy sessions the scan did
   not find are listed under the PR as "unverified — branch guess, not found
   on this machine". Neither changes a score or a recommendation.
3. **Show everything grouped by PR.** A header line per PR
   (`owner/repo#n — title`), then one line per candidate: short session id,
   host, when it ran, its cwd, and **the evidence that matched** — how many of
   the PR's files it edited, whether it ran on the head branch, whether one of
   the PR's commit shas appears in it. Mark as **recommended** the rows scoring
   on **more than one signal** — one weak signal is a coincidence. List the
   PRs with no candidates together at the end, one line each. Say plainly that
   these are candidates.
4. **Ask once**: "Link the recommended (N sessions across M PRs)", "Let me
   choose" (then take their reply naming session ids), or "Link none". **Link
   nothing without an explicit yes**, and never expand the set beyond what the
   user named. An unverified legacy session is linked only if the user names
   it.

### 3.4 Link the approved set

One call per PR that has approved sessions:

```
link_pr(pr_url="…", session_ids=[approved…], link_source="session_found")
```

`session_found` records that the link came from a scan a human confirmed,
which is a different claim from `session_self` (the session was there) and
`manual` (a person named it outright). An unverified legacy session the user
named goes in a separate call for that PR with its `session_id` and
`link_source="manual"` — a person named it, and no scan found it.

**Never send `pr_type` from `--find`.** The server takes a classification only
with `link_source="session_self"`, and for good reason: these sessions were
found by scanning, so this mode is in no position to say what the pull
request's work was. The session that opened the PR classifies it (the
plugin's own hook asks it to); a scan does not.

Report each PR's reply as step 4 says, then:

- the PRs that had no candidates — the code was written somewhere this machine
  cannot see (a teammate's laptop, a session that predates capture, a host with
  no local transcript); `/memhub:link-pr --session <id> <pr>` links one by
  hand;
- the skipped PRs, by reason.

### 3.5 The next page

When the list had more (`has_more`): say how many remain (`total − offset −
count`) and offer the **next 25**. The list is newest first and live: a PR
leaves it once it is linked or dismissed, so re-running from the start would
show the ones this machine had nothing for again, and a plain `offset + 25`
would skip as many unseen PRs as this page linked. On a yes, call
`list_my_unlinked_prs` again with the same `repo` and `offset=<offset +
count − PRs linked from this page>`, and continue from 3.2. Stop when there is
no `has_more` or the user says so.

`--find` never dismisses a PR. Dismissing one hides it from the list; that
happens in MemHub, not here.

## 4. Report the reply honestly

Every mode, per PR. Relay what the server actually said; do not re-word a
partial result into a success it does not claim.

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
