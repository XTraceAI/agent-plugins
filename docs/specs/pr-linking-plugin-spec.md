# Plugin Spec: session ↔ PR linking, confirmed by a human (KISS)

**Repo:** `XTraceAI/agent-plugins`. **Companion:** `MemHub-Backend/docs/specs/session-pr-linking-spec.md`
— the backend half. The two were written together; §2 below restates the wire contract, but the
backend spec is authoritative if they ever disagree.

**Status:** not implemented. This document is the sole source of truth for the plugin half: an
implementer should need nothing else.

---

## 0. Overview

**Problem.** Nothing in the plugin ever asks a human whether a session belongs to a pull request.
Attribution has been left to the server's branch-name inference, which is defeated by worktrees,
reused refs, and inconsistent `gitBranch` resolution, and which is being deleted. The plugin does
already notice `gh pr create` twice — `pr_provenance.py` extracts the URL for effort telemetry,
and `pr_babysit_trigger.py` arms the babysit loop — but neither creates a link.

**Solution.** One new hook and two new skills.

- **`pr_link_trigger.py`** — a PostToolUse hook on shell commands. When a `gh pr …` command's
  output contains exactly one PR URL, it asks the backend one question
  (`GET /v1/team/pr-links/check`) and injects one of three instructions:
  1. *not connected* → tell the user, once, that connecting GitHub on MemHub is what links this
     work to the code;
  2. *connected, and this session wrote the code* → call `link_pr(pr_url, [session_id],
     link_source="session_self")`;
  3. *connected, and it did not* → offer to run `/memhub:find-contributing-sessions`.

  **Who decides which of 2 or 3 applies is the model, not the hook.** The hook has no authorship
  detection; the agent knows whether it edited the code in this PR this session, and that
  judgment is the whole design.
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
| `plugins/memhub/scripts/pr_link.py` | **New.** Shared, importable, side-effect-free: command detection, URL extraction, `conversation_id_for`, the `check` call, and the three context texts. |
| `plugins/memhub/scripts/pr_link_trigger.py` | **New.** The hook entry point: stdin → `pr_link` → `additionalContext` on stdout. |
| `plugins/memhub/scripts/capture.py` | **Changed.** New `current` subcommand (§7). |
| `plugins/memhub/scripts/readers/{claude,codex,cursor}.py` | **Changed.** Each gains `session_cwd(path) -> str | None` (§7). |
| `plugins/memhub/scripts/codex_hook_bridge.py` | **Changed.** `_dispatch_post` also runs `pr_link_trigger.py` for shell tools (§5.2). |
| `plugins/memhub/scripts/cursor_capture.py` | **Changed, conditionally.** §5.3. |
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

### 4.1 `is_gh_pr_command(command: str) -> bool`

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

### 4.2 `pr_url_from_response(tool_response) -> str | None`

1. Take the response's text: `tool_response["stdout"]` when it is a dict with a string `stdout`;
   the whole string when the response is a string; otherwise `""` **plus** `tool_response.get(
   "stderr")` if it is a string — `gh pr create` on an existing branch prints the existing PR's
   URL to stderr, and that is still the PR the user is working on.
2. `urls = pr_provenance.urls_from_output_text(text)` — bounded, canonical, deduped, lowercased.
3. Return `urls[0]` **only when `len(urls) == 1`**. Zero (a `gh pr list` with no matches, a failed
   command, `gh pr checkout` printing only a branch) or two-or-more (`gh pr list`, `gh pr status`)
   → `None` → the hook is silent.

This single rule is what keeps the hook quiet on the listing commands while still firing on
`create`, `view`, `checkout`, `comment`, `merge`, `ready`, and `edit`.

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

### 4.5 The three contexts

Exact strings live in `pr_link.py` as module constants so the tests can assert on them and the two
skills can quote the same vocabulary. Each is prefixed with the PR URL and, when
`linked_sessions` is non-empty, a one-line summary of what is already linked.

**A — not connected** (`github_connected:false`):

> MemHub: this org has no GitHub integration connected, so the sessions behind this pull request
> can't be linked to it. Mention this to the user ONCE per session, briefly, and only if it is
> not intrusive right now: connecting GitHub on MemHub (`<connect_url>`) is what links their
> agent sessions to the code that shipped. Do not repeat it, do not offer to do it for them, and
> do not let it interrupt what they asked for.

**B — connected, decide authorship** (`github_connected:true`):

> MemHub: a pull request is in play — `<pr_url>` (`<repo>#<n>`, `<state>`).
> `<Already linked: 2 sessions (1 of them this user's).>`
> A pull request has many sessions and a session has many pull requests, so linking again from a
> different session is expected, not a duplicate.
> **If the code in this pull request was written in THIS session** — you edited those files, or
> you opened the PR for work you did here — call `link_pr` with `pr_url="<pr_url>"`,
> `session_ids=["<session_id>"]`, `link_source="session_self"`. Do it without asking; it is a
> record of work you did, and it is idempotent.
> **If it was not** — you are reviewing, checking out, or commenting on someone else's work, or
> work from an earlier session — do not link. Instead offer, in one sentence, to run
> `/memhub:find-contributing-sessions <pr_url>` to find the sessions that did write it. Do not
> run it without the user saying yes.
> If this session already linked itself to this pull request, say nothing at all.

`repo_in_install:false` uses variant **A** with a different first sentence naming the repo:
"…GitHub is connected, but `<repo>` isn't part of the install, so this PR can't be linked."

The final line of B is what keeps a stateless hook quiet inside a `/memhub:pr-babysit` loop, which
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
  "matcher": "Bash",
  "hooks": [{
    "type": "command",
    "timeout": 15,
    "statusMessage": "MemHub: checking PR link",
    "command": "IN=$(cat); case \"$IN\" in *gh*pr*) if [ -n \"${CLAUDE_PLUGIN_ROOT:-}\" ] && printf %s \"$IN\" | python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/claude_hook_guard.py\" ignore PostToolUse; then printf %s \"$IN\" | python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/pr_link_trigger.py\"; fi ;; esac"
  }]
}
```

The `case` pre-filter is a cheap byte test that keeps `python3` from starting on ordinary shell
traffic — the same trick the flush and babysit entries use. Synchronous (not `async`), because
`additionalContext` from an async hook is not delivered; 15s covers a 4s HTTP call with room to
spare, and the script self-limits regardless.

Hook input fields consumed: `session_id`, `tool_input.command`, `tool_response`.

### 5.2 Codex — `codex_hook_bridge.py`

In `_dispatch_post`, add a job when `tool in _SHELL_TOOLS`:

```python
if tool in _SHELL_TOOLS:
    jobs.append(lambda: _run(root, "pr_link_trigger.py", payload, timeout=_PR_LINK_TIMEOUT_S))
```

`_dispatch_post` already runs its jobs concurrently and folds each result's
`hookSpecificOutput.additionalContext` into one document, so nothing else changes. Add
`_PR_LINK_TIMEOUT_S = 15`. `references/codex-hooks-bridge.json` already dispatches `PostToolUse`
for `shell`/`local_shell`/`Bash`; **no manifest change is needed**, which matters because that
file is what a user has already trusted in `~/.codex/hooks.json`.

Codex's hook payload names the session as `session_id` (`codex_flush.py:266` reads
`payload.get("session_id") or payload.get("conversation_id")`); `pr_link_trigger` uses the same
fallback and then applies `conversation_id_for("codex", sid)`.

### 5.3 Cursor — verify before implementing

`cursor_capture.py` is a launcher: it detaches `cursor_flush.py` and answers
`{"permission": "allow"}`. There is no context-injection path in this repo today, and
`afterShellExecution`'s output contract is not documented anywhere in it.

**Implementation step, in this order:**

1. Check Cursor's current hooks reference for whether `afterShellExecution` accepts an
   agent-visible field (`agentMessage`, `userMessage`, or equivalent) in its JSON reply.
2. **If it does:** in `cursor_capture.py`, when `event == "afterShellExecution"`, run
   `pr_link_trigger.py` synchronously with a 5s cap *before* printing, and merge its context into
   the reply: `{"permission": "allow", "<field>": context}`. Detaching the flusher must still
   happen first, and a failure or timeout in the trigger must leave the reply exactly as it is
   today. `tests/cursor_capture_test.py` asserts the current reply byte-for-byte — extend it, do
   not weaken it.
3. **If it does not:** ship Cursor with skills only. Note it in the README's Cursor section:
   Cursor users link with `/memhub:link-pr`. Do **not** approximate it by injecting text into the
   transcript some other way.

Cursor's payload gives the session id via `payload.get("session_id") or
payload.get("conversation_id")` (`cursor_flush.py:336`), then `conversation_id_for("cursor", …)`.

---

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
  stdout parses as `hookSpecificOutput.additionalContext` containing the URL, the session id, and
  the "if the code in this pull request was written in THIS session" sentence.
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
- **`README.md`**, the PR section — a short paragraph: after any `gh pr …` command, MemHub checks
  whether the org has GitHub connected and either links this session (when it wrote the code),
  offers the finder, or mentions connecting GitHub. Say plainly that **linking is never automatic
  for work this session did not do**.
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

From `0.47.1` → **`0.48.0`** (new user-visible surfaces, not a fix).

**Order of operations.** The backend ships first and the flag is enabled for the test org; the
plugin can ship at any time after, because every path degrades to silence when `check` reports
`enabled:false` or is unreachable. Do not ship the plugin before the endpoint exists — the hook
would be silent, which is safe, but there is nothing to gain and the version is then burned.

Verify on staging first: install `memhub-staging@memhub-internal` from the branch
(`CONTRIBUTING.md` § "Installing the staging build"), and exercise:

1. `gh pr create` in a repo whose org has the flag on → the agent self-links; check the row.
2. `gh pr view` on a teammate's PR → the agent offers the finder and does **not** link.
3. `gh pr list` → nothing at all.
4. A repo whose org has no GitHub integration → the advisory, once, and a cache file written.
5. `/memhub:link-pr --unlink` → the link disappears.
6. `/memhub:pr-babysit` for a few passes → confirm the injected context does not derail the loop
   (this is the one behaviour a stateless hook puts at risk, and it is why B's last line exists).
