# Plugin Spec: session ↔ PR linking, confirmed by a human (KISS)

**Repo:** `XTraceAI/agent-plugins`. **Companion:** `MemHub-Backend/docs/specs/session-pr-linking-spec.md`
— the backend half. The two were written together; §2 below restates the wire contract, but the
backend spec is authoritative if they ever disagree.

**Status:** implemented in v0.50.0, and inert until the backend half ships (§12). This document
is the sole source of truth for the plugin half: an implementer should need nothing else.

---

## 0. Overview

**Problem.** Nothing in the plugin ever asks a human whether a session belongs to a pull request.
Attribution has been left to the server's branch-name inference, which is defeated by worktrees,
reused refs, and inconsistent `gitBranch` resolution, and which is being deleted. The plugin does
already notice `gh pr create` twice — `pr_provenance.py` extracts the URL for effort telemetry,
and `pr_babysit_trigger.py` arms the babysit loop — but neither creates a link.

**Solution.** One new hook and two new skills.

- **`pr_link_trigger.py`** — a PostToolUse hook on shell commands **and GitHub MCP tool calls**.
  When a call that addressed GitHub — `gh pr …`, a `curl`/`gh api` request to the REST API, or a
  GitHub MCP tool — returns output containing exactly one PR URL, it asks the backend one question
  (`GET /v1/team/pr-links/check`) and injects one instruction:
  1. *not connected* → tell the user, once, that connecting GitHub on MemHub is what links this
     work to the code;
  2. *connected, and this call OPENED the pull request* (`gh pr create`, a POST to
     `…/repos/<o>/<r>/pulls`, or a create-shaped GitHub MCP tool) → link this session,
     unconditionally, `link_source="session_self"` — and, only if the code came mostly from
     elsewhere, also offer `/memhub:find-contributing-sessions`;
  3. *connected, any other GitHub call that names one PR* → the model decides: link if it wrote
     the code here, else offer `/memhub:find-contributing-sessions`.

  **Opening a pull request is itself work the session did**, so case 2 asks no authorship question
  — the creating session is always part of that PR's history, and a PR having many sessions means
  linking it displaces nobody. Case 3 is where judgment lives, and it is the model's, not the
  hook's: the agent knows whether it edited these files this session, and no heuristic has to.
- **`/memhub:link-pr`** — the manual path. Link this session (or named ones) to a PR.
- **`/memhub:find-contributing-sessions`** — scans local session history for sessions that
  plausibly wrote the code in a PR, ranks them, has the user approve, and links the approved ones.

**Unchanged, deliberately:** `pr_provenance.py` and the `provenance.github_pr_urls` capture path
stay exactly as they are. They feed a captured-PR count, not a link, and this feature does not
touch them. `pr_babysit_trigger.py` also stays; the two hooks fire on overlapping commands and
are independent.

---

## 1. Prior art in this repo (read these before writing code)

| File | Why it matters here |
|---|---|
| `plugins/memhub/scripts/pr_babysit_trigger.py` | The exact precedent: a PostToolUse(Bash) hook that recognises `gh pr create`, pulls a URL out of `tool_response`, and prints `hookSpecificOutput.additionalContext`. Its `QUOTED`-stripping and command-position regex are the pattern to widen, not to reinvent. |
| `plugins/memhub/scripts/pr_provenance.py` | Owns `_PR_URL_RE`, `urls_from_output_text()` (bounded, canonical, lowercased) and a much stricter `is_pr_creation_command()`. **Reuse `urls_from_output_text` for extraction.** Do not modify this module. |
| `plugins/memhub/scripts/rulebook_hook.py` (~L1340-1380) | How a hook speaks plain REST: `resolve_bearer()` → `pak.api_base(url)` → `mcp_http.rest(...)`, with failures recorded as a breadcrumb and the hook degrading to silence. Copy this shape. |
| `plugins/memhub/scripts/mcp_http.py` | `rest(url, bearer, method, body=…, headers=…, timeout=…) -> RestReply(status, data, etag)`. Stdlib only; refuses cleartext via `require_secure`. |
| `plugins/memhub/scripts/_memhub_auth.py` | `resolve_bearer(refresh=False) -> (url, bearer)`. `pak.api_base(url)` turns the MCP URL into the REST base. |
| `plugins/memhub/scripts/claude_hook_guard.py` | Every hook command is fronted by `claude_hook_guard.py ignore <Event>`; the new one is no exception. |
| `plugins/memhub/scripts/codex_hook_bridge.py` `_dispatch_post` | How Codex fans several hook scripts out and folds their `additionalContext` into one document. |
| `plugins/memhub/scripts/cursor_capture.py` | Cursor's launcher. Today it only ever answers `{"permission": "allow"}` — see §5.3. |
| `plugins/memhub/scripts/capture.py` + `scripts/readers/` | `list`/`import` over all three hosts. `current` is added here (§7). |
| `plugins/memhub/skills/pr-babysit/SKILL.md` | House style for a skill that takes a PR argument, resolves the repo's room, and is auto-armed by a hook. |
| `plugins/memhub/skills/rules-from-sessions/` | The precedent for a skill with its own `scripts/` that scans local transcripts and hands back JSON instead of transcript text. |

---

## 2. Wire contract

Base URL: `pak.api_base(url)` where `url` comes from `resolve_bearer()`. Auth: `Authorization:
Bearer <mhk_…>` — the capture credential from `/memhub:login`, the same one the rulebook hook
uses. Envelope: `{"code": 0, "msg": "ok", "data": {…}}`.

### 2.1 `GET /v1/team/pr-links/check?pr_url=<url>`

```json
{"enabled": true, "github_connected": true, "repo_in_install": true,
 "connect_url": "https://app.xtrace.ai/settings/integrations",
 "pr": {"known": true, "repo_full_name": "XTraceAI/MemHub-Backend", "pr_number": 1211,
        "state": "open", "merged": false, "title": "…"},
 "linked_sessions": [{"session_id": "…", "conv_id": "…", "name": "…", "author": "…",
                      "is_mine": true, "link_source": "session_self",
                      "linked_at": "2026-09-07T18:04:11Z"}]}
```

`enabled:false` ⇒ the org has the feature off; the plugin must be completely silent.

### 2.2 MCP tools

- `link_pr(pr_url, session_ids=[…], link_source="session_self"|"session_found"|"manual", org_id=…)`
- `unlink_pr(pr_url, session_ids=[…], org_id=…)`

`link_pr` with `session_ids` omitted is a **probe**: it writes nothing and returns the `check`
payload.

### 2.3 What a `session_id` is, per host

The value is the conversation id the capture client already sends, so the server can find the row:

| Host | Value |
|---|---|
| Claude Code | the bare session UUID (`flush_session.py` sends `conversation_id = session_id`) |
| Codex | `codex-<session-uuid>` (`codex_flush.py:526`) |
| Cursor | `cursor-<uuid>` (`cursor_flush.py:1011`) |

Put this mapping in ONE helper — `conversation_id_for(host, session_id)` in
`plugins/memhub/scripts/pr_link.py` (§4) — and have the hook, both skills and `capture.py current`
all call it. Three copies of a string prefix is how a host silently stops linking.

---

## 3. Files

| File | Change |
|---|---|
| `plugins/memhub/scripts/pr_link.py` | **New.** Shared, importable, side-effect-free: the `touches_github` / `creates_pr` detectors (shell, REST API, MCP), URL extraction, `conversation_id_for`, the `check` call, and the context texts. |
| `plugins/memhub/scripts/pr_link_trigger.py` | **New.** The hook entry point: stdin → `pr_link` → `additionalContext` on stdout. |
| `plugins/memhub/scripts/capture.py` | **Changed.** New `current` subcommand (§7). |
| `plugins/memhub/scripts/readers/{claude,codex,cursor}.py` | **Changed.** Each gains `session_cwd(path) -> str | None` (§7). |
| `plugins/memhub/scripts/codex_hook_bridge.py` | **Changed.** `_dispatch_post` also runs `pr_link_trigger.py` for shell tools (§5.2). |
| `plugins/memhub/scripts/cursor_capture.py` | **Unchanged** — §5.3 resolved to skills-only for Cursor. |
| `plugins/memhub/hooks/codex-hooks.json` | **Changed.** PostToolUse matcher widened for GitHub MCP tools (§5.2). |
| `plugins/memhub/hooks/claude-hooks.json` | **Changed.** One new PostToolUse(Bash) entry (§5.1). |
| `plugins/memhub/skills/link-pr/SKILL.md` | **New** (§8). |
| `plugins/memhub/skills/find-contributing-sessions/SKILL.md` | **New** (§9). |
| `plugins/memhub/skills/find-contributing-sessions/scripts/find_sessions.py` | **New** (§9.2). |
| `tests/pr_link_test.py`, `tests/pr_link_trigger_test.py`, `tests/find_sessions_test.py`, `tests/capture_current_test.py` | **New** (§10). |
| `README.md` | **Changed.** Two new entries in the skills list; a paragraph in the PR section (§11). |
| the five version manifests | **Changed.** Bump (§12). |

---

## 4. `pr_link.py` — the shared module

Stdlib only. No network at import. Every public function is pure except `check()`.

### 4.1 Two questions, not one

Every detector below answers one of exactly two questions, and the answers are independent:

- **`touches_github(tool_name, tool_input) -> bool`** — did this tool call address GitHub at all?
  This is the gate: if it is false the hook stops, whatever the output contains.
- **`creates_pr(tool_name, tool_input) -> bool`** — is this call *opening* a pull request? True
  selects context **B1** (unconditional self-link); false selects **B2** (the model judges).

`gh pr …` is one implementation of each (§4.1a / §4.1b); the other PR-creation paths are §4.1c.
`creates_pr` implies `touches_github` for every input, and a test asserts it (§10).

**Why a "touches GitHub" gate rather than "any output containing a PR URL".** The URL rule in
§4.2 is what actually recognises a pull request, and it is tempting to run it over every tool
result. Don't: a `cat CHANGELOG.md`, a `git log` whose commit message cites a PR, a WebFetch of a
PR page, or the agent re-reading its own earlier output would all inject linking context about a
pull request nobody is working on. The gate keeps the feature attached to *acting on GitHub*,
which is the thing that correlates with the session having a stake in the PR.

### 4.1a `is_gh_pr_command(command: str) -> bool`

True when the command invokes `gh` **at command position** with `pr` as its subcommand — *any*
`gh pr …`, not just `create`. Build it from `pr_babysit_trigger.GH_PR_CREATE` by replacing the
trailing `\bpr\s+create\b` with `\bpr\b`, keeping everything else:

- quoted segments stripped first (`QUOTED`), so `grep "gh pr view"` never matches;
- `gh` only after a start, `;`, `&`, `|`, backtick, newline, `(`, or `$(`;
- leading `VAR=…`, `env`, `sudo`, `nohup`, `command`, `exec`, `timeout` and their flags tolerated;
- flags allowed between `gh` and `pr`, but never across a separator.

Copy the regex into `pr_link.py` with its comment rather than importing from
`pr_babysit_trigger` — that module is a hook entry point, and a shared import between two hooks
is a coupling neither wants. The duplication is eight lines and is covered by a test that asserts
both modules agree on a shared corpus of commands (§10).

Deliberately **no subcommand allowlist.** `gh pr list` and `gh pr status` are excluded by §4.2's
"exactly one URL" rule, not by naming subcommands — a rule that keeps working when `gh` adds one.

### 4.1b `is_gh_pr_create(command: str) -> bool`

The same regex with `\bpr\s+create\b` instead of `\bpr\b` — i.e. exactly
`pr_babysit_trigger.is_pr_create`. This is a **separate, narrower** predicate, not a refinement of
§4.1, and it decides which of the two connected contexts the hook emits (§4.5): a session that
ran `gh pr create` links itself unconditionally, while every other `gh pr …` subcommand leaves the
decision to the model.

Both predicates run on every qualifying command. `is_gh_pr_create` implies `is_gh_pr_command`,
and a test asserts that (§10) — a corpus where the narrow one accepts something the wide one
rejects would mean a `gh pr create` that never reaches the hook at all.

### 4.1c The other ways an agent opens a pull request

`gh pr create` is the common path, not the only one. Each of these must reach the same B1
context, or a session that opened a PR silently fails to link itself — and the user has no way to
tell that happened.

**1. The REST API from a shell command** (`curl`, `http`, `wget`, `xh`, or `gh api`):

```bash
curl -L -X POST -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer $TOKEN" \
  https://api.github.com/repos/OWNER/REPO/pulls \
  -d '{"title":"…","head":"octocat:new-feature","base":"master"}'

gh api --method POST repos/OWNER/REPO/pulls -f title=… -f head=… -f base=main
```

`github_api_call(command)` → `("pulls_collection" | "pull_item" | None, is_write)`:

- Strip quoted segments first (as §4.1a does), then look for a GitHub API target:
  `https://api.github.com/repos/<owner>/<repo>/pulls` or, for `gh api`, a bare
  `repos/<owner>/<repo>/pulls` path argument. A trailing `/<number>` (or `/<number>/…`) makes it
  `pull_item`; the bare collection is `pulls_collection`. Enterprise hosts count too: accept any
  `https://<host>/api/v3/repos/…` and `gh api --hostname <host>`.
- `is_write` when the command carries `-X POST` / `--request POST` / `--method POST` / `-XPOST`,
  or (for `gh api`) any of `-f`/`--field`/`--raw-field`/`--input`, or (for `curl`) `-d`/`--data*`
  without an explicit non-POST method. `curl` with `-d` and no `-X` **is** a POST — that is the
  example in the wild, and reading it as a GET would miss every hand-rolled PR creation.
- **`creates_pr` = `pulls_collection` AND `is_write`.** A POST to the *collection* is what opens a
  PR; a POST to `pulls/<n>` is an edit, and a GET of either is B2 territory.
- `touches_github` = any GitHub API target at all, read or write.

The response body is the PR object, whose `html_url` is the one `github.com/<o>/<r>/pull/<n>`
string in it — `issue_url`, `comments_url`, `review_comments_url` and `_links` are all
`api.github.com` URLs, which `_PR_URL_RE` does not match, and `head.repo.html_url` has no `/pull/`
segment. So §4.2's "exactly one URL" rule resolves a create response cleanly with no special
casing.

**2. A GitHub MCP server tool call.** The hook payload names the tool rather than a command
(`mcp__github__create_pull_request`, `mcp__github-mcp__create_pr`, vendor-specific spellings
vary):

- `touches_github` when the name splits as `mcp__<server>__<tool>` and the **server** segment
  contains "github". The server segment runs to the FIRST `__`, and is not `[^_]*`: server names
  routinely contain underscores (`github_enterprise`, and every plugin-provided server — this
  repo's own tools arrive as `mcp__plugin_memhub-staging_memhub__add_memory`), and a
  no-underscore pattern rejected all of them. Matching `github` anywhere in the WHOLE name would
  instead catch `mcp__notes__github_summary`, which is a note-taking tool — the server segment is
  what separates the two.
- `creates_pr` when the tool segment, read as WORDS, opens the pull request itself. Split the
  segment on camelCase and `_`/`-`; require a creation verb (`create`, `open`, `submit`, `new`)
  anywhere; require the tail to be the pull request (`…pull request`, `…pr`, `…prs`); and reject
  the name outright if it contains an object that can only be attached TO a pull request —
  `review`, `comment`, `thread`, `reply`, `annotation`, `suggestion`, `label`, `assignee`,
  `reviewer`, `milestone`.

  **Do not do this with one regex.** `(?i)(create|open|submit).*(pull.?request|\bpr\b)` was the
  first attempt and it accepted `create_pull_request_review`; anchoring the object at the end
  fixed that and still accepted `create_review_for_pull_request` and
  `create_comment_on_pull_request`. Both spellings make a session that REVIEWED a teammate's pull
  request record itself as its author, which is the one mistake this feature cannot take back.
  The same regex also silently rejected `create_pr`, because `_` is a word character so `\bpr\b`
  has no boundary after `create_`.

  The asymmetry is the rule to keep: a missed create falls through to B2, where the model judges
  and links only if it wrote the code, so an unusual spelling losing the tail costs little; a
  false create writes a confirmed authorship claim about work the session did not do.
- The result is a JSON object; §4.2 already walks dicts/lists for text, so the PR's `html_url`
  surfaces the same way.

**3. Everything else** — a Python script using PyGithub, a Makefile target, a CI helper, `hub
pull-request`. These are **not** detected, deliberately: recognising arbitrary programs that
happen to open a PR is the automatic-attribution problem this whole design walked away from. The
answer is `/memhub:link-pr`, and §8's skill exists precisely so an undetected path is one command
away from a link rather than a silent gap. Say so in the README (§11) rather than leaving the
user to discover it.

**Ordering.** A create-shaped MCP tool name answers `creates_pr` on its own — an MCP call is one
call. For a SHELL command the question is asked of the whole call, not of the first match in it:
count the pull-request-producing segments (`gh pr <sub>` invocations plus writing
`pulls_collection` REST calls), and answer true only when there is **exactly one** and it is a
create. There is no scoring and no first-match-wins.

**Why the whole call.** One Bash command routinely chains several, and the tool result is their
COMBINED output with nothing in it to say which segment produced the single URL §4.2 extracted.
`gh pr create >/dev/null && gh pr view 99 --json url` opens one pull request and prints another's;
`gh api --method POST repos/o/a/pulls >/dev/null && gh api --method GET repos/o/b/pulls/7` does
the same through the REST lane. Taking the create verdict and applying it to the URL that came
back links the session as the author of a pull request it did not open. An ambiguous call still
reaches B2 — it did address GitHub — and the model judges there.

Flags are read the same way: from the shell TOKENS of the segment that carries the target, never
with a regex over the whole command string. A regex has to be told where quoting starts and
stops, and got it wrong three separate ways (a quoted URL, a quoted `--method`, and a method
belonging to a different invocation), each time turning a listing into a claimed creation.

### 4.2 `pr_url_from_response(tool_response) -> str | None`

1. Take the response's text. A shell result is a dict with `stdout` / `stderr` strings, or a bare
   string; an MCP result is a nested object. Handle both by walking the response for strings the
   way `pr_provenance._result_strings` already does (`content` / `text` / `output` keys, bounded
   by node count and byte budget) and concatenating what comes back, with `stdout` and `stderr`
   both included — `gh pr create` on an existing branch prints the existing PR's URL to stderr,
   and that is still the PR the user is working on. Reuse that helper rather than writing a
   second walker; it already carries the depth, node and byte caps.
2. `urls = pr_provenance.urls_from_output_text(text)` — bounded, canonical, deduped, lowercased.
3. Return `urls[0]` **only when `len(urls) == 1`**. Zero (a `gh pr list` with no matches, a failed
   command, `gh pr checkout` printing only a branch) or two-or-more (`gh pr list`, `gh pr status`)
   → `None` → the hook is silent.

This single rule is what keeps the hook quiet on the listing commands while still firing on
`create`, `view`, `checkout`, `comment`, `merge`, `ready`, and `edit` — and, unchanged, on a
`curl` POST response or a GitHub MCP result, both of which carry exactly one `html_url`.

`urls_from_output_text` already tolerates a `#issuecomment-…` suffix (its regex stops at the PR
number and rejects only a following alphanumeric or `/`), so `gh pr comment`'s reply URL resolves
to the PR.

### 4.3 `check(pr_url, *, timeout=4.0) -> dict | None`

```python
url, bearer = resolve_bearer(refresh=False)      # from _memhub_auth
if not bearer: return None
reply = mcp_http.rest(f"{pak.api_base(url)}/v1/team/pr-links/check?pr_url={quote(pr_url)}",
                      bearer, "GET", timeout=timeout)
return reply.data if reply.status == 200 and isinstance(reply.data, dict) else None
```

- **Timeout 4s, and every exception is swallowed** into `None`. This runs after a shell command in
  a live session; it is never allowed to slow one down or fail one.
- `None` (no credential, transport error, non-200, unexpected shape) ⇒ the hook emits nothing.
  Silence is always the safe answer: the user can still run `/memhub:link-pr`.
- Record failures as a breadcrumb under `~/.config/memhub-plugin/` the way `rulebook_hook`
  does, so an operator can see why a machine went quiet.

### 4.4 Caching

Cache **only the negative org-level answer**: when a reply has `enabled:false` or
`github_connected:false`, write `{"answer": …, "at": <epoch>}` to
`~/.config/memhub-plugin/prlink/<sha256(api_base)[:16]>.json` (via `atomic_write.py`) and skip the
network for 24h. Everything else — `linked_sessions`, `pr.known` — changes constantly and is
never cached.

Rationale: an org with GitHub disconnected would otherwise pay an HTTP round trip on every `gh pr`
command forever, for an answer that cannot change without an admin action. A freshly connected
integration is picked up within a day, or immediately if the user runs `/memhub:link-pr` (which
always calls live).

### 4.5 The contexts

Four of them — one advisory, one repo-specific variant of it, and the two connected cases.
Exact strings live in `pr_link.py` as module constants so the tests can assert on them and the two
skills can quote the same vocabulary. Each is prefixed with the PR URL and, when
`linked_sessions` is non-empty, a one-line summary of what is already linked.

**A — not connected** (`github_connected:false`):

> MemHub: this org has no GitHub integration connected, so the sessions behind this pull request
> can't be linked to it. Mention this to the user ONCE per session, briefly, and only if it is
> not intrusive right now: connecting GitHub on MemHub (`<connect_url>`) is what links their
> agent sessions to the code that shipped. Do not repeat it, do not offer to do it for them, and
> do not let it interrupt what they asked for.

**B1 — connected, and this call opened the pull request** (`github_connected:true` and
`creates_pr` true — §4.1b or §4.1c). **No authorship judgment happens here.** The session that opened the pull request is
part of that pull request's history whatever else contributed to it — it is where the PR was
written up, where the branch was pushed from, and usually where the review will be handled — so
it links itself unconditionally:

> MemHub: you just opened `<pr_url>` (`<repo>#<n>`).
> `<Already linked: 2 sessions (1 of them this user's).>`
> Link this session to it now: `link_pr` with `pr_url="<pr_url>"`,
> `session_ids=["<session_id>"]`, `link_source="session_self"`. Do this without asking — opening
> the pull request is itself work this session did, and the call is idempotent.
> Then, **only if most of the code in this pull request was written in OTHER sessions** (you
> opened it for work done earlier, or you are shipping someone else's branch), add one sentence
> offering `/memhub:find-contributing-sessions <pr_url>` so those sessions get linked too. A pull
> request has many sessions; yours does not displace them. Do not run it without a yes.

**B2 — connected, any other GitHub call naming one PR** (`github_connected:true`,
`creates_pr` false). Here the model does judge, because viewing, checking out, commenting on or
fetching a pull request says nothing about who wrote it:

> MemHub: a pull request is in play — `<pr_url>` (`<repo>#<n>`, `<state>`).
> `<Already linked: 2 sessions (1 of them this user's).>`
> A pull request has many sessions and a session has many pull requests, so linking again from a
> different session is expected, not a duplicate.
> **If the code in this pull request was written in THIS session** — you edited those files here
> — call `link_pr` with `pr_url="<pr_url>"`, `session_ids=["<session_id>"]`,
> `link_source="session_self"`. Do it without asking; it is a record of work you did, and it is
> idempotent.
> **If it was not** — you are reviewing, checking out, or commenting on someone else's work, or
> work from an earlier session — do not link. Instead offer, in one sentence, to run
> `/memhub:find-contributing-sessions <pr_url>` to find the sessions that did write it. Do not
> run it without the user saying yes.
> If this session already linked itself to this pull request, say nothing at all.

`repo_in_install:false` uses variant **A** with a different first sentence naming the repo:
"…GitHub is connected, but `<repo>` isn't part of the install, so this PR can't be linked."

The final line of B2 is what keeps a stateless hook quiet inside a `/memhub:pr-babysit` loop, which
runs `gh pr view` on every pass: the agent has its own conversation context and can see it already
handled this PR. There is deliberately **no state file** — a PR↔session relationship is
many-to-many, and a dedup file keyed on the PR is exactly what would stop a genuinely new session
from linking itself later.

---

## 5. Host wiring

### 5.1 Claude Code — `plugins/memhub/hooks/claude-hooks.json`

A new entry in `PostToolUse`, alongside (not merged into) the babysit one, so a change to either
cannot silence the other:

```json
{
  "matcher": "^(Bash|mcp__.*[Gg]it[Hh]ub.*__.*)$",
  "hooks": [{
    "type": "command",
    "timeout": 15,
    "statusMessage": "MemHub: checking PR link",
    "command": "IN=$(cat); case \"$IN\" in *gh*pr*|*[Gg]it[Hh]ub*|*api/v3*|*repos/*pulls*) if [ -n \"${CLAUDE_PLUGIN_ROOT:-}\" ] && printf %s \"$IN\" | python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/claude_hook_guard.py\" ignore PostToolUse; then printf %s \"$IN\" | python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/pr_link_trigger.py\"; fi ;; esac"
  }]
}
```

**The matcher is a regex over the tool name**, so it must name the GitHub MCP servers as well as
`Bash` — `"Bash"` alone silently misses every §4.1c case 2. It stays narrower than `mcp__.*`,
because matching every MCP tool would start a Python process on every MCP call in every session
for a check that is almost always going to decline — but it must NOT use `[^_]*` around the
server segment, which rejects `github_enterprise` and every plugin-provided server. This matcher
and the `case` guard below are coarse pre-filters that only decide whether a process starts;
`pr_link.is_github_mcp_tool` is the precise gate, and it is the one that has to be exactly right.

The `case` pre-filter is a cheap byte test that keeps `python3` from starting on ordinary shell
traffic — the same trick the flush and babysit entries use. All four alternatives are needed:
`*gh*pr*` catches `gh pr create` (which contains no "github"); `*[Gg]it[Hh]ub*` catches the
`api.github.com` URL in a `curl`/`gh api` command or response and the server name in an MCP tool
call; and `*api/v3*` plus `*repos/*pulls*` catch the ENTERPRISE forms, which contain neither —
`curl -X POST https://ghe.corp/api/v3/repos/o/r/pulls` has no "github" anywhere in it, and
`gh api --hostname ghe.corp … repos/o/r/pulls` has no URL at all. Without those two the whole
enterprise path is detected by the Python and then never reached, which is a silence no log
explains. A `gh api repos/o/r/pulls` command matches through the `api.github.com` URL in its own
*response*, which is in the same payload.

Synchronous (not `async`), because `additionalContext` from an async hook is not delivered; 15s
covers a 4s HTTP call with room to spare, and the script self-limits regardless.

Hook input fields consumed: `session_id`, `tool_name`, `tool_input` (`.command` for shell, the
whole object for MCP), `tool_response`.

### 5.2 Codex — `codex_hook_bridge.py`

In `_dispatch_post`, add a job for shell tools **and** GitHub MCP tools:

```python
_GITHUB_MCP_RX = re.compile(r"(?i)^mcp__.*github.*__")

if tool in _SHELL_TOOLS or (isinstance(tool, str) and _GITHUB_MCP_RX.match(tool)):
    jobs.append(lambda: _run(root, "pr_link_trigger.py", payload, timeout=_PR_LINK_TIMEOUT_S))
```

Codex has **two** PostToolUse manifests, and they are widened differently because they cost
different things:

- **`plugins/memhub/hooks/codex-hooks.json`** — the plugin-bundled manifest, read by Codex
  releases that load hooks from the plugin itself. It is re-fetched with every plugin upgrade and
  costs the user nothing, so its `PostToolUse` matcher IS widened here, to
  `^(Edit|MultiEdit|Write|NotebookEdit|apply_patch|Bash|shell|local_shell|mcp__.*[Gg]it[Hh]ub.*__.*)$`.
  On that path Codex gets GitHub-MCP detection.
- **`references/codex-hooks-bridge.json`** — the compatibility bridge `setup_codex_hooks.py`
  installs into `~/.codex/hooks.json`, a file the user has already trusted. Widening it would
  require a re-trust, so it is **left exactly as it is**. On the bridge path, shell-based PR
  creation (`gh`, `curl`) is covered and MCP-based creation is not.

Say that split in the README (§11) alongside the Cursor note: on Codex, MCP-based PR creation is
detected on the native plugin-hooks path and not on the compatibility bridge, where the answer is
`/memhub:link-pr`.

`_dispatch_post` already runs its jobs concurrently and folds each result's
`hookSpecificOutput.additionalContext` into one document, so nothing else changes. Add
`_PR_LINK_TIMEOUT_S = 15`. `references/codex-hooks-bridge.json` already dispatches `PostToolUse`
for `shell`/`local_shell`/`Bash`; **no manifest change is needed**, which matters because that
file is what a user has already trusted in `~/.codex/hooks.json`.

Codex's hook payload names the session as `session_id` (`codex_flush.py:266` reads
`payload.get("session_id") or payload.get("conversation_id")`); `pr_link_trigger` uses the same
fallback and then applies `conversation_id_for("codex", sid)`.

### 5.3 Cursor — resolved: skills only

`cursor_capture.py` is a launcher: it detaches `cursor_flush.py` and answers
`{"permission": "allow"}`. There is no context-injection path in this repo today, and step 1
below settled why there cannot be one.

1. **Checked (2026-09-07, Cursor's hooks reference).** `afterShellExecution` has **no output
   schema at all** — the reference describes it as observational ("useful for auditing or
   collecting metrics from command output") and defines no reply fields. Only
   `beforeShellExecution` returns anything the agent can see (`permission`, plus `agent_message`,
   which is the message shown *when the call is denied*, and `user_message`). There is no field on
   the post-shell event that puts text in front of the model.
2. **Therefore: Cursor ships with skills only.** `cursor_capture.py` is **unchanged** — it still
   detaches the flusher and prints exactly `{"permission": "allow"}`, and
   `tests/cursor_capture_test.py`'s byte-for-byte assertion on that reply stands untouched. Do
   **not** approximate the injection by borrowing `beforeShellExecution` (it fires before the
   command runs, so there is no PR URL yet, and its `agent_message` only surfaces on a denial) and
   do **not** write into the transcript some other way.
3. Note it in the README's Cursor section: Cursor users link with `/memhub:link-pr`.

Cursor's hook set is shell- and edit-shaped (`beforeShellExecution`, `afterShellExecution`,
`afterFileEdit`, `afterAgentResponse`, `stop`, `beforeSubmitPrompt`, `sessionEnd`) with no
MCP-execution event either, so §4.1c case 2 could not be detected there in any case. On Cursor,
every path to a link is `/memhub:link-pr`.

Cursor's payload gives the session id via `payload.get("session_id") or
payload.get("conversation_id")` (`cursor_flush.py:336`), then `conversation_id_for("cursor", …)` —
which the skill path still needs, through `capture.py current`.

### 5.4 Which host is the hook running under

`pr_link_trigger.py` must namespace the session id per §2.3, so it must know its host, and the
payload does not reliably say. The host is passed **on argv**, not sniffed:

```
python3 pr_link_trigger.py [--host claude|codex|cursor]     # default: claude
```

Claude Code's hook entry (§5.1) passes nothing and gets the default; the Codex bridge passes
`--host codex`. Sniffing (Codex's `shell`/`local_shell` tool names, Claude's `transcript_path`)
would be a heuristic that silently mis-namespaces a session the day a host renames a field —
and a mis-namespaced id links nothing, with no error. An unknown `--host` value is treated as
`claude`, because a wrong prefix is worse than no prefix.

## 6. Exposing the tools to the skills

Both new skills and any existing skill that should be able to link must list the tools in their
frontmatter `allowed-tools`, in **both** the prod and staging spellings, exactly as every other
skill does:

```
mcp__plugin_memhub_memhub__link_pr, mcp__plugin_memhub-staging_memhub__link_pr,
mcp__plugin_memhub_memhub__unlink_pr, mcp__plugin_memhub-staging_memhub__unlink_pr
```

Nothing in `.mcp.json` / `mcp.json` changes — they name the server, not its tools.

---

## 7. `capture.py current`

```
python3 capture.py current [--host auto|claude|codex|cursor] [--cwd PATH]
                           [--max-age-s 1800] [--json]
```

Prints the current session's **conversation id** (host-namespaced per §2.3) and its host, or
exits non-zero with a diagnostic. Stdlib only, like `list`.

**Why it exists:** the hook path already knows the session id from its payload; the manual skill
does not, and no host exports one to a skill. Guessing wrong links the wrong session, so this
command refuses rather than guesses.

**Per-reader addition — `session_cwd(path) -> str | None`:**

| Reader | Source |
|---|---|
| `claude.py` | the top-level `cwd` key on transcript records — read the LAST record that has one, scanning backwards over at most 200 records (a session can `cd`; the current directory is what matters). Do not use `f.parent.name`: it is the encoded project dir, which collapses `/` to `-` and cannot be compared to a real path. |
| `codex.py` | the rollout's `session_meta` record's `cwd` — `_session_meta()` already exists and `to_canonical` already reads `cwd` off it (`readers/codex.py:285`); `session_cwd` is that read, without parsing the whole rollout. |
| `cursor.py` | `meta.json`'s `cwd`, already surfaced by `list_sessions`. |

**Algorithm:**

1. `target = realpath(--cwd or os.getcwd())`.
2. Candidates = `list_sessions(limit=40)` for each host in scope, keeping rows whose
   `realpath(session_cwd)` equals `target`.
3. Drop candidates whose `mtime` is older than `--max-age-s` (default 1800). The current session's
   transcript was written seconds ago; anything stale is a different session.
4. Zero candidates → exit 3 with `no live session found for <cwd>` (the skill then asks the user).
5. Two or more candidates whose mtimes are within 120s of each other → **exit 4**, printing all of
   them (id, host, age, cwd) so the skill can ask the user which. Two agents in one worktree is
   real, and picking the newest would silently link the wrong one.
6. Otherwise print the newest.

`--host auto` scans every installed host; when candidates come from more than one host, that is
also the ambiguous case (exit 4).

`--json` prints `{"conversation_id": …, "session_id": …, "host": …, "cwd": …, "mtime": …}` for
programmatic use; the default prints two lines for a human.

**Precedent to honour:** a team lesson from an earlier session-eval mishap says not to trust
"newest `.jsonl` by mtime" alone, because several transcripts get touched in the same window.
Steps 3 and 5 are that lesson encoded: the cwd match is the content signature, and ambiguity is
an error rather than a guess.

---

## 8. `/memhub:link-pr`

`plugins/memhub/skills/link-pr/SKILL.md`.

**Frontmatter**

```yaml
description: Use when the user wants to link a coding session to a GitHub pull request in MemHub, or to undo such a link (e.g. "link this session to PR 42", "/memhub:link-pr", "attach my work to this PR", "unlink that session from the PR"). Records the link as confirmed, so the PR's session context is published from facts rather than a branch-name guess.
argument-hint: [pr-number-or-url] [--session <id>...] [--unlink]
allowed-tools: Bash, mcp__plugin_memhub_memhub__link_pr, mcp__plugin_memhub-staging_memhub__link_pr, mcp__plugin_memhub_memhub__unlink_pr, mcp__plugin_memhub-staging_memhub__unlink_pr, mcp__plugin_memhub_memhub__list_orgs, mcp__plugin_memhub-staging_memhub__list_orgs
```

**Body — the steps, in order:**

1. **Plugin root** paragraph, copied verbatim from `import-session/SKILL.md` (hosts differ on
   `CLAUDE_PLUGIN_ROOT`).
2. **Resolve the PR.** From `$ARGUMENTS` — a full URL is used as-is; a bare number resolves
   against the current repo via `gh pr view <n> --json url -q .url`. No argument → `gh pr view
   --json url,number,state,headRefName -q .url` for the current branch; if that fails, ask.
   Normalise to `https://github.com/<owner>/<repo>/pull/<n>` with no trailing slash, query or
   fragment — the server rejects anything else.
3. **Resolve the sessions.** `--session <id>` (repeatable) wins. Otherwise
   `python3 "<plugin-root>/scripts/capture.py" current --json`:
   - exit 0 → use its `conversation_id`;
   - exit 4 (ambiguous) → show the listed candidates and ask which;
   - exit 3 (none) → run `capture.py list --limit 20` and ask.
   Never invent a session id and never pass a raw Codex/Cursor UUID — `capture.py` returns the
   namespaced form the server expects.
4. **Link.** `link_pr(pr_url=…, session_ids=[…], link_source="manual")`. `--unlink` calls
   `unlink_pr` with the same arguments instead.
5. **Report the reply honestly**, without re-wording it into a success it does not claim:
   - `linked[]` entries with `created:true` → "linked"; `upgraded:true` → "upgraded an old
     inferred link to a confirmed one"; `skipped: already_linked` → "was already linked".
   - `skipped: session_not_found` → say the session is not in MemHub yet (it may not have been
     captured — `/memhub:import-session <id>` fixes that) or belongs to someone else.
   - error `github_not_connected` / `repo_not_in_install` → relay the message and the
     `connect_url`; do not retry.
   - error `feature_disabled` → say linking is not enabled for this org yet. Stop.
   - `pr_not_found` right after `gh pr create` → GitHub may not have the PR yet; suggest
     re-running the command in a moment.
6. **Mention the payoff once**: when a link is created and the org has PR session insights on, the
   PR's MemHub comment refreshes on its own within a minute. Do not promise it if the reply did
   not create anything.

The skill never calls the REST endpoint directly — the MCP tools are the model-facing surface, and
they carry the org resolution.

---

## 9. `/memhub:find-contributing-sessions`

`plugins/memhub/skills/find-contributing-sessions/SKILL.md` plus its own
`scripts/find_sessions.py`.

**Frontmatter description** must name the trigger phrases the hook uses ("find the sessions
behind this PR", "which sessions wrote this PR", "/memhub:find-contributing-sessions") and state
that it searches **local** session history on this machine.

### 9.1 The flow

1. Resolve the PR (same rules as §8 step 2).
2. Collect the PR's facts with `gh`, in the terminal, never by pasting a diff into context:
   - `gh pr view <n> --json headRefName,baseRefName,createdAt,mergedAt,url,title`
   - `gh pr view <n> --json files -q '.files[].path'` (or `gh api …/pulls/<n>/files --paginate
     -q '.[].filename'` when the PR is large)
   - `gh pr view <n> --json commits -q '.commits[].oid'`
3. Run the scanner (§9.2) with those facts. It prints ranked candidates as JSON — never transcript
   text.
4. **Present the candidates and ask.** One line each: session id (short), host, when, its cwd, the
   evidence that matched (how many of the PR's files it edited, whether it ran on the head
   branch, whether one of the PR's commit shas appears in it). Say plainly that these are
   *candidates*, then ask which to link. Default to the ones scoring on more than one signal, but
   **link nothing without an explicit yes.**
5. `link_pr(pr_url=…, session_ids=[approved…], link_source="session_found")` — one call for the
   whole approved set.
6. Report as in §8 step 5. Sessions already in the `check` reply's `linked_sessions` are excluded
   in step 4 and mentioned as "already linked".

### 9.2 `scripts/find_sessions.py`

```
python3 find_sessions.py --files-from <path> --branch <head-ref> [--base <base-ref>]
                         [--sha <oid>]... [--created-at <iso>] [--host all|claude|codex|cursor]
                         [--limit 200] [--max-candidates 10] [--json]
```

Stdlib only. Reads local sessions through `readers` — the same machinery
`skills/rules-from-sessions/scripts/mine_sessions.py` uses; read that file first and follow its
shape (bounded reads, no transcript echo, JSON out).

**Scanning.** For each of the most recent `--limit` sessions across the requested hosts, walk the
canonical records and collect, with hard caps (stop after 20k records or 64 MiB per session):

- **Edited paths** — from tool-call inputs: `file_path` on `Edit`/`Write`/`MultiEdit`/
  `NotebookEdit`, the path argument of `apply_patch`, and paths appearing in a `git add` /
  `git commit` command string. Normalise to repo-relative by suffix-matching against the PR's
  file list rather than trying to resolve absolute paths (a worktree's absolute prefix differs
  from the PR's, and that mismatch is the whole reason this feature exists).
- **Branches** — the top-level `gitBranch` on Claude records; for other hosts, `git checkout` /
  `git switch` / `git branch` command strings.
- **Commit shas** — any 7-to-40 hex token in tool output that matches a `--sha` prefix.
- **Session cwd** and mtime (§7).

**Scoring** (report the components, do not hide them behind one number):

| Signal | Points |
|---|---|
| a PR commit sha appears in the session | 5 (this is proof, not inference) |
| each distinct PR file the session edited | 2, capped at 8 |
| session ran on the PR's head branch | 3 |
| session active within 30 days before `--created-at` | 1 |
| session ran on the base branch only, never the head, and edited none of the files | −5 |

Emit the top `--max-candidates` with score > 0, newest first among ties:

```json
[{"conversation_id": "codex-01J…", "session_id": "01J…", "host": "codex",
  "cwd": "/Users/x/repos/foo", "mtime": 1757260000.0, "score": 11,
  "evidence": {"shas": ["a1b2c3d"], "files": ["app/x.py", "app/y.py"],
               "branch_match": true, "in_window": true}}]
```

**Never print transcript content** — only paths, branches, shas and counts. A session transcript
can exceed a million tokens, and this output goes straight into model context.

---

## 10. Tests

`tests/` is auto-discovered by `run_all.py` (glob `*_test.py`) and `registration_test.py` requires
every `def test_*` to actually be invoked — use `globals()` discovery or list them all.

**`tests/pr_link_test.py`**
- `is_gh_pr_command`: true for `gh pr create`, `gh pr view 12`, `gh pr checkout 12`,
  `gh --repo o/r pr merge`, `env FOO=1 gh pr ready`, `sudo gh pr edit`, `x && gh pr view`;
  false for `grep "gh pr view" f`, `echo gh pr create`, `ghost pr view`, `gh repo view && foo pr
  create`, `gh pr` inside single quotes.
- `is_gh_pr_create`: true for `gh pr create --fill`, **`cd .. && gh pr create`**,
  `cd /repo; gh pr create`, `(cd sub && gh pr create)`, `env X=1 gh pr create`; false for
  `gh pr view`, `gh pr merge`, `grep "gh pr create" f`. The chained-command cases are not
  decoration — a `^gh pr create` anchor would miss them, and missing them is what silently turns
  an unconditional self-link into a coin flip.
- **implication test**: for every command in the corpus, `is_gh_pr_create(c)` implies
  `is_gh_pr_command(c)`, and `creates_pr(t, i)` implies `touches_github(t, i)`.
- `github_api_call`: the user's exact `curl -L -X POST … https://api.github.com/repos/O/R/pulls
  -d '{…}'` → `("pulls_collection", True)`; the same without `-X POST` but with `-d` → still a
  write (this is the shape people actually paste); with `-X GET` → not a write;
  `…/pulls/12` → `("pull_item", …)`; `gh api --method POST repos/o/r/pulls -f title=x` → write on
  the collection; `gh api repos/o/r/pulls` → read; an enterprise `https://gh.corp/api/v3/repos/o/r/pulls`
  POST → write; `curl https://api.github.com/repos/o/r/issues` → `(None, …)`;
  `echo "https://api.github.com/repos/o/r/pulls"` and a quoted URL inside `grep` → `(None, …)`.
- MCP detection: `mcp__github__create_pull_request` → both true;
  `mcp__github-mcp__open_pull_request` → both true; `mcp__github__get_pull_request` →
  `touches_github` only; `mcp__notes__github_summary` → **neither** (the server segment is not
  GitHub); `mcp__GitHub__createPullRequest` → both true (case-insensitive).
- a real `create_pull_request` MCP result fixture (with `html_url`, `issue_url`, `comments_url`,
  `_links` and `head.repo.html_url`) → `pr_url_from_response` returns exactly the `html_url`; a
  real `curl` POST response body fixture → the same.
- **agreement test**: every command in the shared corpus that `pr_babysit_trigger.is_pr_create`
  accepts must also satisfy `pr_link.is_gh_pr_command` — the widened regex may never be narrower.
- `pr_url_from_response`: exactly one URL → that URL; `gh pr list` output with three → `None`;
  empty → `None`; URL only on stderr → found; `…/pull/12#issuecomment-99` → `…/pull/12`;
  a dict response with a non-string `stdout` → `None`.
- `conversation_id_for`: claude → bare id; codex → `codex-…`; cursor → `cursor-…`; an already
  namespaced id is not double-prefixed.
- `check()` with a stubbed `mcp_http.rest`: 200 → dict; 500, timeout, exception, missing bearer →
  `None` and no raise.
- cache: a `github_connected:false` reply writes the file and a second call inside 24h makes no
  HTTP call; a `github_connected:true` reply writes nothing; a corrupt cache file is ignored, not
  fatal.

**`tests/pr_link_trigger_test.py`** (subprocess, like the other hook tests)
- a Claude PostToolUse payload for `gh pr create` with one URL and a stubbed connected `check` →
  stdout parses as `hookSpecificOutput.additionalContext` carrying the **B1** text: the URL, the
  session id, `link_source="session_self"`, and "without asking". It must NOT contain B2's
  "if the code in this pull request was written in THIS session" conditional.
- the same payload with `cd .. && gh pr create` as the command → still B1.
- a `curl -X POST …/pulls` payload whose response body is a PR object → **B1**.
- an `mcp__github__create_pull_request` payload (tool_name set, no `tool_input.command`) → **B1**.
- an `mcp__github__get_pull_request` payload → **B2**.
- a `cat CHANGELOG.md` payload whose output contains one PR URL → **empty stdout**: the gate is
  "did this call address GitHub", not "does this text mention a PR".
- a `gh pr view` payload with one URL and the same stubbed `check` → the **B2** text, with the
  conditional and no unconditional link instruction.
- `github_connected:false` → variant A text, with the `connect_url`.
- `repo_in_install:false` → the repo-specific variant of A.
- `enabled:false` → **empty stdout, exit 0**.
- `check` unreachable → empty stdout, exit 0, and a breadcrumb written.
- a non-`gh pr` command, a `gh pr list` with several URLs, and a failed `gh pr create` → empty
  stdout.
- malformed JSON on stdin → exit 0, no output, no traceback.
- the same payload run twice produces the same output (statelessness is a property, not an
  accident).

**`tests/capture_current_test.py`** — a fake `$HOME` with synthesized Claude/Codex/Cursor session
stores: single match → the namespaced conversation id; two matches 10s apart in one cwd → exit 4
listing both; only a stale match → exit 3; a session in a sibling worktree does not match.

**`tests/find_sessions_test.py`** — a fixture transcript that edits two of the PR's files on the
head branch scores above one that only ran on `main`; a sha match outscores everything; output is
JSON with no transcript text (assert no sentence from the fixture's prose appears in stdout);
caps are honoured on a synthetic 50k-record session.

**Existing suites that must be extended, not weakened:** `cursor_capture_test.py` (§5.3),
`codex_hooks_parity_test.py` (the bridge's new job), `documentation_test.py` (§11),
`version_parity_test.py` (§12).

---

## 11. Documentation

- **`README.md`**, the `/memhub:*` list — two entries, in the house voice, next to `pr-babysit`:
  - `/memhub:link-pr [pr] [--session <id>] [--unlink]` — links a coding session to a pull request
    in MemHub, so the PR's session context is published from a confirmed fact.
  - `/memhub:find-contributing-sessions [pr]` — scans this machine's session history for the
    sessions that wrote a PR's code, ranks the candidates, and links the ones you approve.
- **`README.md`**, the PR section — a short paragraph: after a call that addresses GitHub (`gh pr
  …`, a `curl` / `gh api` request to the REST API, or a GitHub MCP tool), MemHub checks whether
  the org has GitHub connected and either links this session, offers the finder, or mentions
  connecting GitHub. Say plainly that **a session that opens a PR always links itself**, that
  **linking is otherwise never automatic for work this session did not do**, and that **a PR
  opened by some other means — a script, a CI helper — is linked with `/memhub:link-pr`**, with
  the per-host gaps named (no MCP detection on Codex or Cursor; no hook at all on Cursor if §5.3
  lands skills-only).
- If §5.3 lands skills-only for Cursor, say so in the Cursor install section.
- `documentation_test.py` asserts on README strings; add assertions for the two new skill names so
  a future rename cannot silently drop them from the docs.

---

## 12. Release

`RELEASING.md` and `.github/workflows/bump-guard.yml`: **any PR touching `plugins/memhub/**` must
bump the version**, and on Codex and Cursor the bump *is* the release. All five version-bearing
manifests move together (`version_parity_test.py` enforces it):

```
plugins/memhub/plugin.json
plugins/memhub/.claude-plugin/plugin.json
plugins/memhub/.codex-plugin/plugin.json
plugins/memhub/.cursor-plugin/plugin.json
plugins/memhub-staging/.claude-plugin/plugin.json
```

From `0.49.1` → **`0.50.0`** (new user-visible surfaces, not a fix). The base moved
under this work — `0.48.0` and `0.49.x` shipped while it was being written — so the
number here is whatever the next minor is at merge time, not a fixed one.

**Order of operations.** The backend ships first and the flag is enabled for the test org; the
plugin can ship at any time after, because every path degrades to silence when `check` reports
`enabled:false` or is unreachable. Do not ship the plugin before the endpoint exists — the hook
would be silent, which is safe, but there is nothing to gain and the version is then burned.

**Status as implemented (2026-09-07).** The backend half is **not deployed**:
`GET https://api.staging.memhub.xtrace.ai/v1/team/pr-links/check` answers `404`, and neither
`link_pr` nor `unlink_pr` appears in the staging MCP tool list. The plugin half is nonetheless
implemented in full, hook entry included, because every path is silent without the endpoint —
`check()` returns `None` on a 404 and the hook prints nothing. **The pull request carrying it must
not be merged until the endpoint exists**, and the manual verification below cannot be performed
before then; steps 1–6 are unrun, and saying otherwise would be a claim about behaviour nobody has
observed.

Verify on staging first: install `memhub-staging@memhub-internal` from the branch
(`CONTRIBUTING.md` § "Installing the staging build"), and exercise:

1. `gh pr create` in a repo whose org has the flag on → the agent self-links; check the row.
2. `gh pr view` on a teammate's PR → the agent offers the finder and does **not** link.
3. `gh pr list` → nothing at all.
4. A repo whose org has no GitHub integration → the advisory, once, and a cache file written.
5. `/memhub:link-pr --unlink` → the link disappears.
6. `/memhub:pr-babysit` for a few passes → confirm the injected context does not derail the loop
   (this is the one behaviour a stateless hook puts at risk, and it is why B's last line exists).
