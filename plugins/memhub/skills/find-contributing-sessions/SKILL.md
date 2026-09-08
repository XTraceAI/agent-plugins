---
description: Use when the user wants to know which coding sessions actually wrote a pull request's code, and to link them in MemHub (e.g. "/memhub:find-contributing-sessions", "find the sessions behind this PR", "which sessions wrote this PR", "link the sessions that built PR 42"). Scans THIS MACHINE's local session history (Claude Code, Codex, Cursor), ranks the candidates by the evidence that matched, and links only the ones you approve.
argument-hint: [pr-number-or-url]
allowed-tools: Bash, mcp__plugin_memhub_memhub__link_pr, mcp__plugin_memhub-staging_memhub__link_pr, mcp__plugin_memhub_memhub__unlink_pr, mcp__plugin_memhub-staging_memhub__unlink_pr, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_orgs
---

**Plugin root:** Resolve this skill's plugin root once: it is the ancestor of
this file containing `plugin.json` and the `scripts/` directory. A trusted host
variable such as `CLAUDE_PLUGIN_ROOT` or `CURSOR_PLUGIN_ROOT` may already point
there; use it only when it resolves to that same ancestor. Substitute the
resulting absolute path as `<plugin-root>` below; do not infer it from the
workspace cwd. Commands show `python3`; on native Windows use `py -3`.

A pull request is usually written across several sessions and then opened from
one of them. That last session links itself; the others are what this skill
finds. It searches **local session history on this machine only** — Claude
Code, Codex and Cursor transcripts under this user's home — so it finds the
user's own work, never a teammate's.

Everything it produces is a **candidate**. Nothing is linked without an
explicit yes.

## 1. Resolve the pull request

Same rules as `/memhub:link-pr`: a full URL is used as-is; a bare number
resolves with `gh pr view <n> --json url -q .url`; no argument uses the current
branch's PR. Normalise to `https://github.com/<owner>/<repo>/pull/<n>` with no
trailing slash, query or fragment.

## 2. Collect the PR's facts — in the terminal, never in context

Never paste a diff into context. Write the file list to a file and pass the
rest as flags:

```bash
gh pr view <n> --json headRefName,baseRefName,createdAt,url,title
gh pr view <n> --json files -q '.files[].path' > /tmp/pr-files.txt
gh pr view <n> --json commits -q '.commits[].oid'
```

For a large PR, `gh pr view --json files` truncates; use
`gh api repos/<owner>/<repo>/pulls/<n>/files --paginate -q '.[].filename' > /tmp/pr-files.txt`
instead.

## 3. Run the scanner

```bash
python3 "<plugin-root>/skills/find-contributing-sessions/scripts/find_sessions.py" \
  --files-from /tmp/pr-files.txt \
  --branch <headRefName> --base <baseRefName> \
  --sha <oid> --sha <oid> \
  --created-at <createdAt>
```

It prints ranked candidates as JSON — conversation id, host, cwd, mtime, score
and the evidence components. It never prints transcript text.

**Read stderr too.** A transcript too large to parse into memory is not
scanned, and the scanner says so there (`note: N session(s) larger than … were
not scanned`). If that line appears, tell the user which sessions were skipped —
a long session is exactly the kind that did the work, and an unexamined one must
not look like an examined one that scored zero.

What the score is made of, so you can explain a row rather than quote a number:

| Signal | Points |
|---|---|
| a PR commit sha appears in the session | 5 — this is proof, not inference |
| each distinct PR file the session edited | 2, capped at 8 |
| the session ran on the PR's head branch | 3 |
| the session was active within 30 days before the PR was opened | 1 |
| ran on the base branch only, never the head, and edited none of the files | −5 |

## 4. Present the candidates and ask

One line each: short session id, host, when it ran, its cwd, and **the evidence
that matched** — how many of the PR's files it edited, whether it ran on the
head branch, whether one of the PR's commit shas appears in it. Say plainly
that these are candidates.

Exclude sessions already linked (the `check` reply's `linked_sessions`, or what
a previous run of this skill linked) from the list to approve, and mention them
separately as "already linked".

Then ask which to link. Default your recommendation to the rows scoring on
**more than one signal** — one weak signal is a coincidence — but **link
nothing without an explicit yes**, and never expand the set beyond what the
user named.

## 5. Link the approved set

One call for the whole approved set:

```
link_pr(pr_url="…", session_ids=[approved…], link_source="session_found")
```

`session_found` is this skill's value: it records that the link came from a
scan a human confirmed, which is a different claim from `session_self` (the
session was there) and `manual` (a person named it outright).

## 6. Report

Exactly as `/memhub:link-pr` step 4: relay `created` / `upgraded` /
`skipped: already_linked` / `skipped: session_not_found` honestly, relay
`github_not_connected`, `repo_not_in_install` and `feature_disabled` with their
messages and do not retry them, and mention the PR-comment refresh only if
something was actually created.

If the scan found nothing, say so plainly — an empty result means the code was
written somewhere this machine cannot see (a teammate's laptop, a session that
predates capture, or a host with no local transcript). `/memhub:link-pr
--session <id>` is the way to link one by hand.
