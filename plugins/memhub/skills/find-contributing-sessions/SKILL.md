---
description: Use when the user wants to know which coding sessions actually wrote their pull requests, and to link them in MemHub (e.g. "/memhub:find-contributing-sessions", "map my unlinked PRs to sessions", "find the sessions behind my PRs", "which sessions wrote this PR", "link the sessions that built PR 42"). With no argument it lists the user's own PRs that have no confirmed session link; `--pr <url>` (repeatable) names specific ones. Scans THIS MACHINE's local session history (Claude Code, Codex, Cursor) once for all of them, ranks the candidates per PR by the evidence that matched, and links only the ones you approve.
argument-hint: [pr-number-or-url] [--pr <url-or-number>]...
allowed-tools: Bash, AskUserQuestion, mcp__plugin_memhub_memhub__list_my_unlinked_prs, mcp__plugin_memhub-staging_memhub__list_my_unlinked_prs, mcp__plugin_memhub_memhub__link_pr, mcp__plugin_memhub-staging_memhub__link_pr, mcp__plugin_memhub_memhub__unlink_pr, mcp__plugin_memhub-staging_memhub__unlink_pr, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_orgs
---

**Plugin root:** Resolve this skill's plugin root once: it is the ancestor of
this file containing `plugin.json` and the `scripts/` directory. A trusted host
variable such as `CLAUDE_PLUGIN_ROOT` or `CURSOR_PLUGIN_ROOT` may already point
there; use it only when it resolves to that same ancestor. Substitute the
resulting absolute path as `<plugin-root>` below; do not infer it from the
workspace cwd. Commands show `python3`; on native Windows use `py -3`.

A pull request is usually written across several sessions and opened from one
of them. That last session links itself; the others are what this skill finds.
It searches **local session history on this machine only** — Claude Code,
Codex and Cursor transcripts under this user's home — so it finds the user's
own work, never a teammate's.

Everything it produces is a **candidate**. Nothing is linked without an
explicit yes.

Arguments: `$ARGUMENTS`
- `--pr <url-or-number>` (repeatable) = map exactly these pull requests.
- A bare URL or number with no flag = the same as one `--pr` (so
  `/memhub:find-contributing-sessions <pr_url>`, which the PR hook suggests,
  still works).
- Nothing = map **my own unlinked PRs** (step 1b).

**Asking the user.** Where this says "ask", use AskUserQuestion on Claude Code;
Codex and Cursor have no such UI, so ask the same question as a short numbered
list in plain text and wait for the reply.

## 1. Decide which pull requests

### 1a. PRs were named

Resolve every `--pr` value and the bare one. A full URL is used as-is. A bare
number resolves against the current checkout with
`gh pr view <n> --json url -q .url`; if that fails, **ask** which PR they mean
rather than guessing. Normalise each to `https://github.com/<owner>/<repo>/pull/<n>`
— no trailing slash, query, fragment or tab suffix (`/files`, `/commits/…`).
**Only github.com pull requests can be linked**: MemHub refuses any other
host, so GitHub Enterprise is not supported yet. Do not rewrite an enterprise
PR (`https://ghe.corp/o/r/pull/7`) to `github.com` — that names a different
pull request — and do not send it to `link_pr`; the scanner skips it as
`unsupported_host`, and you report it. Then go to step 2 — the list tool is
not called.

### 1b. Nothing was named — ask which, then list my unlinked PRs

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
3. "Just this branch's PR" → step 2 with that one URL.
4. Otherwise call
   `list_my_unlinked_prs(limit=25)` — adding `repo="<owner/name>"` for "This
   repo". It returns the caller's own PRs that have **no confirmed session
   link**, newest first, with PRs the user dismissed in MemHub already left
   out. Do not pass `state`, `since` or `include_dismissed`.
5. Read the reply, **in this order**:
   - **The tool is not available** (not in your tool list, or an unknown-tool
     error) **or it failed** → say in one sentence that this MemHub backend
     cannot list unlinked PRs yet (or the error, briefly), and go to 1c.
   - **`github_connected: false`** → the org has no usable GitHub connection,
     and `link_pr` would refuse every PR — whatever else the reply says. Relay
     that with the `connect_url` and **stop**.
   - **`github_identity_linked: false`** → MemHub does not know which GitHub
     account is theirs, so it cannot tell which PRs they authored. Say so
     once, give the `connect_url` verbatim as where to link it, and go to 1c.
   - **`prs` is empty** → "no unlinked PRs of yours in <scope>". Stop.
   - Otherwise take every `prs[].html_url` into step 2. Keep each PR's
     `legacy_branch_sessions` for step 3, and the reply's `offset`, `count`,
     `total` and `has_more` for step 5.

### 1c. Fallback: the user's own GitHub CLI

`gh` cannot know MemHub's links or dismissals, so the user picks.

```bash
gh search prs --author=@me --limit 30 --sort created --order desc \
  --json url,title,repository,state,createdAt
```

Add `--repo <owner/name>` when they chose "This repo". If `gh` is missing,
unauthenticated or fails, **ask** them to paste the PR URLs or numbers instead
and continue from 1a.

Show a numbered list — `n. owner/repo#num — title (state, created date)` — and
ask which to map: numbers, ranges, `all` or `none`. This is always a text
reply, on every host: there can be thirty rows, and AskUserQuestion holds four.
Take at most 25 of the chosen URLs into step 2.

## 2. Collect the facts and scan — in the terminal, never in context

Hand the URLs to the scanner, one `--pr-url` each. It reads each PR's head
branch, base branch, creation date, files and commits with `gh` **by URL** —
never by bare number, because `gh pr view <n>` resolves the number against the
CURRENT checkout and would rank sessions against a different pull request than
the one being linked. It checks that the `url` `gh` answers with is the one it
asked about, pages the file list through `gh api … --paginate` when a PR has
100 files or more, and then parses every local transcript **once**, scoring it
against every PR.

```bash
python3 "<plugin-root>/skills/find-contributing-sessions/scripts/find_sessions.py" \
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
and that mode is unchanged. This skill uses the batch mode above.)

## 3. One consolidated review

1. **Already linked.** PRs that came from `list_my_unlinked_prs` have no
   confirmed link by definition. **Every other PR** — named with `--pr`, this
   branch's PR, or picked in 1c — gets `link_pr(pr_url="…")` with **no**
   `session_ids` first: the probe. Its `linked_sessions` mixes confirmed
   links with MemHub's old branch guesses: drop from that PR's candidates only
   the rows whose `link_source` is **not** `branch`, mentioning them as
   "already linked". A `branch` row is a guess that still needs a real link —
   treat it exactly like a `legacy_branch_sessions` entry in the next step. If
   the probe says `github_connected: false` or `repo_in_install: false`, drop
   that PR and relay the reason (and `connect_url`) once.
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

## 4. Link the approved set

One call per PR that has approved sessions:

```
link_pr(pr_url="…", session_ids=[approved…], link_source="session_found")
```

`session_found` is this skill's value: it records that the link came from a
scan a human confirmed, which is a different claim from `session_self` (the
session was there) and `manual` (a person named it outright). An unverified
legacy session the user named goes in a separate call for that PR with its
`session_id` and `link_source="manual"` — a person named it, and no scan found
it.

**Never send `pr_type` from here.** The server takes a classification only with
`link_source="session_self"`, and for good reason: this skill links sessions it
found by scanning, so it is in no position to say what the pull request's work
was. The session that opened the PR classifies it (the plugin's own hook asks
it to); a scan does not.

## 5. Report

Per PR, exactly as `/memhub:link-pr` step 4: relay `created` / `upgraded` /
`skipped: already_linked` / `skipped: session_not_found` honestly, and handle
an error by its message wording exactly as that step does — relay the GitHub
connection and installation messages and do not retry them. Then:

- the PRs that had no candidates — the code was written somewhere this machine
  cannot see (a teammate's laptop, a session that predates capture, a host with
  no local transcript); `/memhub:link-pr --session <id>` links one by hand;
- the skipped PRs, by reason;
- when the list had more (`has_more`): say how many remain (`total − offset −
  count`) and offer the **next 25**. The list is newest first and live: a PR
  leaves it once it is linked or dismissed, so re-running from the start would
  show the ones this machine had nothing for again, and a plain `offset + 25`
  would skip as many unseen PRs as this page linked. On a yes, call
  `list_my_unlinked_prs` again with the same `repo` and `offset=<offset +
  count − PRs linked from this page>`, and continue from step 2. Stop when
  there is no `has_more` or the user says so;
- the PR-comment refresh, only if something was actually created.

This skill never dismisses a PR. Dismissing one hides it from the list; that
happens in MemHub, not here.
