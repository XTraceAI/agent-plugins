---
title: "Spec: Codex session titles align with Codex's own UI"
type: spec
---

# Plugin Spec: a captured Codex session is called what Codex calls it

**Repo:** `XTraceAI/agent-plugins`. **Client-only** — no MemHub server change is required, and
none is assumed. **Target version:** `v0.52.1`.

**Goal, in the user's words:** *"all I care about is that the titles displayed on Codex's own UI
align with the ones on MemHub."* Everything below follows from that one sentence. This is a
fidelity requirement, not a title-generation requirement: where Codex has named a thread, MemHub
must show that name, byte for byte. Where Codex has not, MemHub falls back to a derived name —
but the fallback is a last resort, not a competing title generator.

**Status:** implemented in v0.52.1. This document is the sole source of truth for the change;
an implementer should need nothing else.

---

## 0. Overview

**Problem.** Codex names every substantive thread itself and displays that name in its UI, but
the MemHub reader never reads it. `readers/codex.py::_title()` instead takes the first user
message, keeps only its first line, and hard-cuts at 150 characters — no whitespace collapse, no
word boundary, no ellipsis. The result is that MemHub shows a different, worse name than Codex
does for the same session.

**Solution.** Read Codex's own name. Add two sources above the existing fallback (the rollout's
`thread_name_updated` record, then the `~/.codex/session_index.jsonl` sidecar), and clean up the
fallback so that when it *is* used it produces a readable title instead of a ragged fragment.
The same normalization is applied to Cursor, which carries a byte-identical defect.

**Also fixed, because it is in the code being touched:** the Codex and Cursor titles are derived
from RAW records and never redacted, so a first prompt containing a secret ships as the
conversation's name (§5); and `load_rollout` could hand a non-dict to the transform and crash the
`Stop` hook (§3.4).

---

## 1. Measured evidence (the reason this spec exists)

Run of the current `origin/main` reader over all seven Codex rollouts on the author's machine,
beside the name Codex itself displays:

| rollout | Codex's own `thread_name` | `meta["title"]` today |
|---|---|---|
| `019daccf` (2026-04-20) | `Review org switcher refresh bug` | `can you take a look at my memory-hub project at /Users/xtrace/repos/xtrace/memory_hub_frontend/memory-hub and specifically the changes that have been ` |
| `019df592` (2026-05-04) | `Fix doc ingest 500` | `❯ I am getting these errors from doc ingest:     2026-05-05T00:16:28.620Z    INFO: 10.0.1.121:44566 - "POST                    ` |
| `019ed7af-2116` (2026-06-17) | `Review context-sessions endpoints` | `can you do a reading of the context-sessions endpoints for team orgs? I want to do the following: right now, the user is able to query the policy and ` |
| `019ed7af-c7bf` (2026-06-17) | `Inspect context-sessions endpoints` | *(byte-identical to the row above)* |
| `019ed7b0` (2026-06-17) | `Read context-sessions endpoints` | *(byte-identical to the row above)* |
| `019f8238` (2026-07-20) | `Add memhub claude plugin` *(sidecar only)* | `/plugin marketplace add XTraceAI/memhub-claude-plugin` |
| `019fb0b4` (2026-07-29) | *(none — the session errored on its first request)* | `can you set up the plugin at https://github.com/XTraceAI/memhub-claude-plugin` |

Four distinct failure modes, all visible above:

1. **Mid-sentence truncation at exactly 150 chars**, with a trailing space and no ellipsis.
2. **Pasted terminal output becomes the name** — a shell prompt glyph, a log timestamp, an
   internal IP and an HTTP verb, for a session Codex called *Fix doc ingest 500*.
3. **Three distinct sessions collapse to one byte-identical title.** Codex distinguished them as
   *Read* / *Inspect* / *Review*; in a MemHub sessions list they are three identical rows.
4. **A slash command becomes the name.**

---

## 2. Where Codex keeps the name

### 2.1 In the rollout: `event_msg` / `thread_name_updated`

Verbatim from a real rollout (`~/.codex/sessions/2026/04/20/rollout-…-019daccf….jsonl`, line 9 of
680):

```json
{"timestamp":"2026-04-20T21:32:52.024Z","type":"event_msg",
 "payload":{"type":"thread_name_updated",
            "thread_id":"019daccf-641d-7243-be7b-7ce39be99773",
            "thread_name":"Review org switcher refresh bug"}}
```

Properties an implementer must rely on:

- It lands at **line 8-10**, right after the first turn's `task_complete` — so it is present on
  the **first `Stop` flush**, not only at session end.
- The type is `thread_name_updated`, so it **can repeat**. Only one occurrence per file was
  observed locally, but a rename must be assumed to emit another. **The last one wins** — exactly
  the rule `session_title.generated_title` already applies to Claude's `ai-title`.
- It is absent from short or errored sessions (2 of 7 locally), and absent from at least one
  Codex Desktop session that *is* named (see §2.2).

Confirmed absent as title sources, so do not look for them: `session_meta` carries no name field
of any kind; `turn_context.summary` is the reasoning-summary mode enum (`"auto"`), not a summary;
the only `"title"` string anywhere in any rollout is a JSON-Schema property inside
`session_meta.payload.dynamic_tools[].inputSchema`.

### 2.2 The sidecar: `~/.codex/session_index.jsonl`

One JSON object per session, all six lines on the author's machine:

```json
{"id":"019daccf-641d-7243-be7b-7ce39be99773","thread_name":"Review org switcher refresh bug","updated_at":"2026-04-20T21:32:52.02504Z"}
{"id":"019df592-4d8f-7e41-80b6-830ce49d4d29","thread_name":"Fix doc ingest 500","updated_at":"2026-05-05T00:38:21.531635Z"}
{"id":"019ed7af-2116-7cb3-8722-740e2c0636c9","thread_name":"Review context-sessions endpoints","updated_at":"2026-06-17T22:24:03.577986Z"}
{"id":"019ed7af-c7bf-7db2-9443-85cf3d2063c7","thread_name":"Inspect context-sessions endpoints","updated_at":"2026-06-17T22:24:45.878173Z"}
{"id":"019ed7b0-274b-7ee1-8abb-2da13b82b178","thread_name":"Read context-sessions endpoints","updated_at":"2026-06-17T22:25:10.049896Z"}
{"id":"019f8238-6bf2-75d0-bc2d-405bd69ee556","thread_name":"Add memhub claude plugin","updated_at":"2026-07-21T01:09:41.034278Z"}
```

**It is strictly more complete than the rollouts.** Session `019f8238` (Codex Desktop 0.142.5) is
named here but has **no `thread_name_updated` record in its rollout at all** — so for Desktop
sessions the sidecar may be the only place the name exists. That single row is the entire
justification for reading it; without it, Desktop sessions fall through to the prompt fallback.

It is a **global** file, so it is a fallback and never the primary: the rollout is per-session and
is the file the reader was actually handed.

---

## 3. The change: `readers/codex.py::_title`

### 3.1 The ladder

```
_title(rollout, session_id) ->
  1. last event_msg/thread_name_updated  -> payload.thread_name     VERBATIM
  2. session_index.jsonl[session_id]     -> thread_name             VERBATIM
  3. clean_user_text(first user message)                            NORMALIZED
  4. last event_msg/task_complete        -> last_agent_message      NORMALIZED
  5. None
```

Steps 1 and 2 are new. Steps 3 and 4 are today's behavior, in today's order (3 above 4 — the
existing `src = first_user or last_complete` preference is deliberate and is preserved), with
normalization added.

### 3.2 Verbatim vs normalized — the load-bearing distinction

**A `thread_name` is not reshaped**: not whitespace-collapsed, not truncated to 80, never given
an ellipsis. That is the direct consequence of the goal — trimming Codex's own display name would
*reintroduce* the misalignment this change exists to remove.

"Not reshaped" is not the same as "unbounded", and two invariants the old `splitlines()[0][:150]`
supplied for free still have to hold, in `_one_line()`:

- **One line.** A `\r` in a title would let a rollout overwrite the line `import_session` prints to
  the terminal (`:407-409`).
- **Bounded at `_MAX_NAME` (200), matching the send sites.** Without a cap in the reader,
  `meta["title"]` comes out longer on the manual-import path than on the flush path — and at ~1 MB
  the `--title` argv hand-off in `capture.py:290` fails outright with `E2BIG`. The pre-change
  reader bounded *every* title unconditionally; this keeps that promise.

Observed names run 18-31 characters, so neither limit is reached in practice.

**A derived title (steps 3 and 4) is normalized** — it is our text, not Codex's, and it is the
thing that produced the ragged fragments in §1.

### 3.3 Reading the sidecar safely

The reader is called from a `Stop` hook (`codex-hooks.json`, `timeout: 30`) and must not become a
failure source. Requirements:

- Path: `Path.home() / ".codex" / "session_index.jsonl"`. Honour a `CODEX_HOME` override if the
  reader already has that notion; otherwise `$HOME` is correct.
- Wrap the whole read in `try/except Exception` and return `None` on **any** failure. A missing
  file is the common case (a fresh machine, a Codex build that does not write one) and must be
  silent.
- Parse line by line, skipping malformed lines, exactly as `load_rollout` already does. A
  truncated final line from an interrupted write is normal.
- `encoding="utf-8"`, explicitly — the same reason `load_rollout` pins it (a bare read decodes
  with the OS locale codec and one em-dash kills the import on a cp1252 box).
- **Bound the read — from the TAIL.** This file grows with every session the user has ever run,
  and it is append-ordered (oldest first), so the row for the session being flushed is always among
  the newest. Keep the last `_INDEX_MAX_LINES` (10,000) lines with
  `collections.deque(fh, maxlen=…)`: one pass, bounded memory, and "last matching row wins"
  unchanged. Bounding from the FRONT instead would silently kill this lane for any long-time Codex
  user — every Desktop session would revert to the prompt fallback, which is the very bug the lane
  exists to fix. Do not stop at the first match; the scan must see the whole window.
- Match on the `id` field against the session id the reader already resolved from `session_meta`
  (`sm.get("id")`). If `session_id` is `None`, skip step 2 entirely.
- Last matching row wins — the index records an update by appending.

Known limit, accepted: the bound counts LINES, so a single unterminated line (an index symlinked
to `/dev/zero`) is still unbounded, and a FIFO in its place blocks until the hook's timeout. Both
require a hostile file inside the user's own `~/.codex`, which is not a boundary this reader
defends.

### 3.4 A non-dict record must not crash the hook

`load_rollout` is annotated `list[dict]` but appends whatever `json.loads` returns, so a rollout
line holding a bare JSON scalar (`null`, a number, a quoted string) arrives in the list as a
non-dict. Every consumer walks these with `r.get(...)`, so one such line raises `AttributeError`
— inside a `Stop` hook, where it surfaces as a traceback in the user's session.

Found by the new suite's junk-record case. Guarding the two title loops was the first attempt and
was **wrong**: `_session_meta` runs before either of them and crashed anyway, and so did the model
loop in `rollout_to_claude_records` and the timestamp scan at `:498`. Chasing call sites is
whack-a-mole on a function whose declared type is already `list[dict]`, so the fix is at the
source — `load_rollout` drops non-dict records. The defensive `isinstance` guards in the title
loops stay, because `_title` is also called directly with hand-built lists (the tests do this).

This is pre-existing and strictly wider than the titling change, but it is reachable through the
new sidecar lane's own entry point and cheap to close correctly, so it is closed here.

### 3.5 Signature

`_title` currently takes `(rollout)`. It needs the session id for step 2. Take it as a second
parameter rather than re-deriving it: `rollout_to_claude_records` has already computed
`sm = _session_meta(rollout)` before it calls `_title`, so pass `sm.get("id")`.

Keep the parameter optional (`session_id: str | None = None`) so any existing direct caller and
the current tests keep working.

---

## 4. The shared normalizer

### 4.1 Why it is shared

`session_title.prompt_title` already implements exactly the wanted behavior for Claude — collapse
whitespace, cap at 80, break on a word boundary past the halfway mark, strip trailing
`" ,.;:—-"`, append `"…"` — but it is welded to Claude's record walk. Codex and Cursor each
reimplemented a cruder version (`splitlines()[0][:150]`).

Factor the *text* step out of `session_title.py` as a public helper and call it from all three.
One rule, three hosts, and the Claude behavior is preserved by construction.

### 4.2 Contract

```python
def normalize_title(text: str | None, limit: int = _MAX_LEN) -> str | None:
    """A single line of prose, fit to read as a title in a sessions list."""
```

- `None`, empty, or whitespace-only in → `None` out.
- Collapse **all** whitespace runs (including newlines) to single spaces, then strip. This is
  what kills the `❯ I am getting these errors from doc ingest:     2026-05-05T…` title's internal
  gaps; note it also means the first-line-only rule is no longer needed — collapsing subsumes it,
  and keeping more than the first line is strictly better for a prompt whose first line is short.
- `len <= limit` → return unchanged.
- Otherwise cut at `limit - 1`, back up to the last space if that space is at or past `limit // 2`,
  strip trailing `" ,.;:—-"`, and append `"…"`. Result is always `<= limit`.
- `_MAX_LEN` stays **80**.

### 4.3 Refactor discipline

`session_title.prompt_title` must be rewritten to *call* `normalize_title` for its tail, and must
come out behaviorally identical. `tests/session_title_test.py` already pins the exact cases —
`"a long prompt is truncated"`, `"truncation marks itself"`, `"truncation breaks on a word"`,
`"an exactly-80-char prompt is untouched"`, `"whitespace is collapsed"` — and must pass unchanged.
Do not edit those assertions.

One behavioral subtlety to preserve: `prompt_title` collapses whitespace *before* its length test.
`normalize_title` must do the same, in that order.

### 4.4 Import direction

`readers/codex.py` and `readers/cursor.py` will import from `session_title`. Check for a cycle
before writing: `session_title` imports `transcript_filter` only, and the readers are not imported
by either, so `readers → session_title → transcript_filter` is acyclic. Keep it stdlib-only and
free of import-time side effects — `session_title`'s module docstring states this constraint
because `flush_turn` imports it inside a `Stop` hook.

---

## 5. Redaction at the send sites

**The defect.** `codex_flush.py:444` calls `to_canonical(rollout)` on RAW records; `redact_records`
at `:458` covers only `sendable`. `meta["title"]` bypasses redaction entirely and is sent at
`:539-544`. A Codex session whose first prompt is `export MEMHUB_TOKEN=mhk_…` ships that key as the
conversation's **name** — the most visible field there is. Cursor has the identical gap at
`cursor_flush.py:1028-1033`.

Claude's path does not have this bug, and says so in two load-bearing comments
(`flush_turn.py:297-303`, `flush_session.py:238-241`) that describe this exact scenario.

**The fix, as decided: redact at the send sites**, not in the readers. There are three:

| file:line | call |
|---|---|
| `codex_flush.py:539-544` | `arguments["title"] = redact_text(title.strip())[:200]` |
| `cursor_flush.py:1028-1033` | same |
| `import_session.py:378` | `explicit = redact_text(args.title) if args.title else None` |

`import_session.py` is included because it *is* a send site, and it is the one that receives the
unredacted reader title by way of `capture.py:333-334` (`--title` from `meta["title"]`) for Codex
and Cursor imports. Leaving it out would keep the leak open on the manual-import path.

There it is applied at **derivation** (`:378`) rather than at the `call_args` assignment, and only
to `args.title` — the other three sources already read from the redacted `records`. Derivation is
the better site on this path because the resolved title is *echoed to the terminal* at `:407-409`;
redacting only at the send would print the secret while declining to transmit it.

Note the ordering: redact **then** truncate to 200. Capping first can chop a straddling key below
`_SECRET`'s `{16,}` floor (`redact.py:36`), after which the fragment matches nothing and ships in
the clear — verified both ways. The new suite pins the expression at each of the three sites, because this leak is invisible
in behaviour right up until someone's key is the name of a conversation.

`redact_text` lives at `redact.py:41` and is stdlib-only. `codex_flush.py:46` already imports
`redact_records` from that module; extend the import.

**Out of scope, deliberately:** the Claude path's redaction is already correct and is not touched.

---

## 6. Cursor

`readers/cursor.py:447` is `title = ask.strip().splitlines()[0][:150]`. Replace with
`normalize_title(ask)`. Cursor has no `thread_name` equivalent — nothing in its artifacts carries
a host-generated name — so it gets the normalization half only, and its title remains a derived
one. This keeps the three readers on one titling rule.

---

## 7. What does NOT change

Named explicitly, because each was considered and rejected:

- **No slash-command or pasted-log filtering** on the Codex fallback. Claude's `prompt_title`
  skips command wrappers via `is_command_wrapper`, but Codex rollouts carry no
  `isMeta`/sidechain/command marking to key off, and the goal is alignment with Codex's UI rather
  than a better title than Codex's. Both real cases that would have motivated it
  (`/plugin marketplace add …`, the pasted log) are already solved by reading `thread_name`.
- **No backfill.** `codex_flush` re-parses the whole rollout and re-sends `title` on every flush
  under a constant `conversation_id`, so an in-flight session self-corrects on its next `Stop`
  once the plugin upgrades, and a mid-session rename propagates the same way. A session that has
  already finished never flushes again and keeps its old title unless the user re-imports it with
  `/memhub:import-session`. Re-titling finished sessions would need a MemHub **server** change;
  this repo is client-only and that is out of scope.
- **No state migration.** Unlike Claude's delta path, Codex and Cursor re-derive the title from
  the full file every flush, so nothing is persisted and nothing needs migrating.
- **`event_msg`/`user_message`** stays ignored for content (`readers/codex.py:21-25` documents
  why); only `response_item` role=user feeds the fallback, as today.
- **Claude's title path** is unchanged in behavior. Only the shared-helper extraction touches
  `session_title.py`.

---

## 8. Compatibility with already-installed cached plugin versions

The plugin cache is keyed by version on both Claude
(`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`) and Codex, so an install on an older
version keeps its old code until the version number changes — it does not half-adopt this change.

Nothing here is a wire-format change: `title` is the same optional string argument to the same
`import_conversation` tool. An old client sending an old title and a new client sending a new one
are both valid, and a session's title simply improves on the first flush after upgrade. No
server coordination is required.

---

## 9. Tests

New file `tests/codex_session_title_test.py`, in the house style: no pytest, no new dependencies,
a module-level `check(name, condition)` accumulating into `FAILURES`, `sys.exit(1 if FAILURES)`.
Isolated via `tmpdir` and env overrides; it must never reach a live backend.

Cases to pin:

**The ladder**
- a rollout with `thread_name_updated` titles the session with `thread_name`, not the first prompt
- the **last** `thread_name_updated` wins when there are several (the rename case)
- a blank / non-string `thread_name` is ignored and the ladder falls through
- with no `thread_name_updated`, the sidecar supplies the name — this is the `019f8238` /
  Desktop case and is the whole reason step 2 exists
- the sidecar is consulted **only** when the rollout has no record (rollout wins on conflict)
- no `thread_name` anywhere → today's first-user-prompt fallback, now normalized
- no user turn at all → `task_complete.last_agent_message`, normalized
- an empty rollout → `None`

**Verbatim vs normalized**
- a `thread_name` longer than 80 chars is **not** truncated and gets no ellipsis
- a `thread_name` with odd internal whitespace is **not** collapsed (only stripped)
- a 200-char first prompt **is** cut to <= 80, ends in `…`, and does not break mid-word

**Sidecar robustness** — each must yield `None` from step 2 and never raise
- missing file · unreadable file · malformed JSON lines mixed with good ones · a truncated final
  line · no row matching this session id · `session_id=None`

**Regression cases straight from §1** — assert the exact good title for a fixture built from each
real shape: the pasted-log session titles as `Fix doc ingest 500`, and the three
`context-sessions` sessions produce three *different* titles.

**Redaction** — a first prompt containing `export MEMHUB_TOKEN=mhk_…` does not reach
`arguments["title"]` in cleartext, asserted at the send site.

**Cursor** — `normalize_title` is applied: a long Cursor ask is cut to <= 80 with an ellipsis, the
reader uses the *shared* normalizer (identity check), and a whitespace-only ask returns `None`
rather than raising `IndexError` as `splitlines()[0]` did.

Also update, do not delete: `tests/readers_test.py` `test_codex_transform()` asserts
`meta["title"] == "Fix the bug"` from a fixture with no `thread_name_updated`. That fixture still
exercises the fallback and its assertion stays valid; add a sibling case that adds a
`thread_name_updated` to the same fixture and asserts the name wins.

**Both runs must be green:**

```
python3 tests/run_all.py
uv run --with 'mcp<2' python tests/run_all.py
```

The second is the only one that covers the mcp-importing suites, which includes `codex_flush`.

---

## 10. Verification beyond the suites

- **Real-data check.** Run the new reader over every rollout under `~/.codex/sessions` and confirm
  each title equals the `thread_name` in `~/.codex/session_index.jsonl` for the six sessions that
  have one. This is the acceptance test for the stated goal and should be a printed summary in
  the test file, gated on the directory existing (a CI box has none), mirroring how
  `session_title_test.py` scans real `~/.claude/projects` transcripts.
- **Hook smoke.** Pipe a realistic Codex `Stop` event JSON into `codex_hook_bridge.py dispatch
  Stop`: exit 0, nothing printed on the unhappy path, well inside the 30s budget.
- **Never against production.** Any manual run sets
  `MEMHUB_MCP_BASE_URL=https://api.staging.memhub.xtrace.ai` explicitly — the staging plugin's
  symlinked `scripts/` otherwise auto-resolves to prod. Clean up with `scripts/purge_today.py`.

---

## 11. Version and manifests

`0.52.0` → **`0.52.1`** (a fix; matches the `0.49.1` / `0.46.5` precedent).

`tests/version_parity_test.py` enforces **five** version-bearing manifests, all of which must be
bumped in the same commit:

| | |
|---|---|
| `plugins/memhub/plugin.json` | Agent Plugins 1.0 root |
| `plugins/memhub/.claude-plugin/plugin.json` | Claude |
| `plugins/memhub/.codex-plugin/plugin.json` | Codex |
| `plugins/memhub/.cursor-plugin/plugin.json` | Cursor |
| `plugins/memhub-staging/.claude-plugin/plugin.json` | staging (Claude) |

On the unpinned channels (Codex, Cursor) the bump reaching `main` **is** the release, so a
straggler manifest is a straggler channel. Manifests must also parse with unique keys — the test
checks this, because a duplicate `description` key silently ships the stale text.

Nothing dev-only may live under `plugins/` — the directory is copied verbatim into every install.
The new test goes in `tests/`.

---

## 12. Files touched

| File | Change |
|---|---|
| `plugins/memhub/scripts/session_title.py` | extract `normalize_title`; `prompt_title` calls it (behavior identical) |
| `plugins/memhub/scripts/readers/codex.py` | `_title(rollout, session_id=None)`: the four-step ladder + sidecar reader |
| `plugins/memhub/scripts/readers/cursor.py` | `:447` uses `normalize_title` |
| `plugins/memhub/scripts/codex_flush.py` | import + apply `redact_text` at `:539` |
| `plugins/memhub/scripts/cursor_flush.py` | import + apply `redact_text` at `:1028` |
| `plugins/memhub/scripts/import_session.py` | apply `redact_text` to `args.title` at `:378` |
| `plugins/memhub/scripts/capture.py` | redact the title before it becomes child argv |
| `tests/codex_session_title_test.py` | new |
| `tests/readers_test.py` | add the `thread_name` case |
| five `plugin.json` manifests | `0.52.1` |
| `README.md`, `plugin.json` / `marketplace.json` descriptions | user-facing behavior changed |

Descriptions are documentation, not metadata: the manifest text claims sessions are "named with
the title Claude Code generates" and says nothing about Codex. That sentence needs to cover the
Codex host now.
