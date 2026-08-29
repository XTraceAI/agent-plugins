# Rulebook hook — what fires, what it marks, and what it reports

**Domain** `HOOK` (Rulebook Reconciliation, wave 2) ·
**Repo** `XTraceAI/agent-plugins` · **Paired with** `FIRE-LEDGER`
(MemHub-Backend) on contract **C3**.

**Files owned by this spec**

| Path | Why |
|---|---|
| `plugins/memhub/scripts/rulebook_hook.py` | the whole delivery path: fetch, match, inject, log, flush |
| `tests/rulebook_hook_test.py` | the drift gate for the three lanes |
| `tests/rulebook_client_test.py` | the drift gate for the client contract (fetch / recall / flush) |

Not owned here: the two authoring skills and `rulebook_conflicts.py`
(`AUTHORING-SKILLS`, spec `rulebook-authoring-skills`), anything in
MemHub-Backend (`FIRE-LEDGER`, `RULES-QUERY`, `AUTHORING-API`), the Rulebook
screen (`RULEBOOK-UI`, `FIRE-UI`).

---

## 1. Why this exists

Three defects, in descending order of blast radius. All three were verified
against `api.staging.memhub.xtrace.ai` and `agent-plugins@origin/rulebook`
(`25eeb28` / v0.29.5) on 2026-08-28.

| # | What the hook does | What is true | Effect |
|---|---|---|---|
| 1 | `fetch_book()` requests `?status=active&view=hook&repo=…` | `RULES-QUERY` (C5) made list filters PostgREST-style; the server answers **400 `Filter 'status' must use the 'op.value' form`** | **The book is never refreshed.** `fetch_book` keeps the cache on any non-200/304, so a fresh install has no rules at all and an existing one is frozen at whatever it last fetched — silently, with no error anywhere |
| 2 | Fire rows carry no pointer at the moment they fired | C3 requires `message_ref`, the transcript entry uuid | The ledger can say a rule fired in a session but never *where*; `FIRE-UI`'s "what fired here" surface has nothing to anchor to |
| 3 | `to_hook_rule` copies only `scope_repos`; `scope_ok` has no path notion | The hook view serializes `scope_paths` and `scope_exclude_paths`, and `create-rule` SKILL.md advertises them | A rule scoped to `src/**` fires on **every** edit in the repo — the skills promise a narrowing the hook does not perform |

Defect 1 is the one that matters most and was not in the wave plan: it makes
defects 2 and 3 moot, because a hook with no book fires nothing. It was found
by probing the live endpoint rather than by reading code, which is the whole
point of the plan's §4 exit test.

Defect 3 was raised — not absorbed — by `AUTHORING-SKILLS` (its spec §6,
"Known gap, not this domain's to fix") and is accepted here.

**What is NOT on this list.** The wave plan's `HOOK` scope also named the
dangling `_(why: )_` clause and removing `MAX_POSTURE`. The empty-`why` clause
was already fixed on `main` by the `_why()` helper. `MAX_POSTURE` is decision 4
below.

---

## 2. Decisions of record

Settled with the owner before implementation. Do not relitigate.

1. **Base is the `rulebook` integration branch**, not `main` and not `staging`.
   `origin/staging` was deleted; merging to `main` *is* the release on the Codex
   and Cursor channels. Branch `feat/rulebook-hook-message-ref`, PR base
   `rulebook`.
2. **The version pin does not move.** `AUTHORING-SKILLS` bumped all five
   manifests to **0.29.5** on this branch; the `HOOK` change rides the same pin,
   because `rulebook → main` releases the wave as one version.
3. **The marker ships unconditionally** — no env flag, no staged rollout.
   Verified on staging: `POST /v1/team/rulebook/fires` answers
   `202 {"accepted", "rejected"}` identically with and without the key, so an
   unknown field cannot 4xx a batch and strand the ledger behind the watermark.
4. **`MAX_POSTURE` stays.** Waves decision 17 ("the session-rule cap goes
   entirely") is **superseded**: it was written against v0.28.0, and `main` has
   since replaced the cap-of-3 with 15 rules / 8000 chars, deterministically
   ordered, logging every cut rule to the ledger as `suppressed`. Nothing about
   the cap changes here. Known consequence, accepted: an author filing a 16th
   session rule is not told it will not be delivered — the ledger's `suppressed`
   rows are the only signal, and `AUTHORING-SKILLS` has already removed the
   number from both skills so nothing states a figure that could drift.
5. **`excerpt` stays stripped** from the wire, unconditionally (C3). The
   transcript marker is the path to "what fired here"; the excerpt is not.
6. **`LEDGER_SCHEMA` stays at 2.** `message_ref` is additive and nullable; old
   rows read back as `None` through `wire_row`'s `row.get()`, so there is no
   v2→v3 migration to write and nothing to migrate.
7. **The status filter is dropped entirely, not translated to `eq.active`.**
   A bare `?view=hook&repo=…` is the one form both the old (prod) and new
   (staging) servers accept, so this needs no version sniffing and no retry.

   **What actually keeps a retired rule out of the book is the server, not the
   client.** An earlier draft of this decision claimed the client-side
   `status` re-checks made the filter redundant; that argument is circular. A
   hook-view row carries **no `status` field at all**, and `to_hook_rule`
   defaults a missing status to `active`, so those re-checks evaluate
   `"active" == "active"` for exactly the rows this change relies on. They
   still catch a row that names a non-active status explicitly — they simply
   are not the thing doing the work here.

   The direct evidence is that the hook view filters server-side: with the
   staging probe rule `active`, `?view=hook&repo=agent-plugins` returned it;
   the moment it was PATCHed to `deprecated`, the same request returned **zero
   rules**. **Residual risk, stated rather than hidden:** that was measured
   against staging. If an older production hook view were to serve a
   non-active row *and* omit `status`, the hook would deliver it. Verifying
   that needs a request to production, which this work does not make. Note the
   pre-change behaviour is not a safer baseline — it fetched nothing at all.
8. **Path scope filters only calls that carry a path.** A path-scoped rule on a
   Bash command, an `output` result, a `session_context` digest or an
   `anchor_recall` handle stays in scope. Adding a scope the hook cannot
   evaluate must never silently kill a rule.
9. **gitignore-style globs**, matched against the **repo-relative** path.
10. **The marker travels under BOTH names — C3 amended, not absorbed.** C3
   froze the key as `message_ref`. The deployed `FIRE-LEDGER` reads
   **`source_message_id`**. Proved on staging: a fire sent with `message_ref`
   comes back `202 {accepted: 1}` and is stored with `source_message_id: null`
   — the marker is dropped with no error anywhere, which is the exact failure
   mode C3 exists to prevent. Raised with the owner rather than absorbed; the
   call is to send one value under both keys, so the marker lands whichever
   name the server honours and neither half has to ship first. Revisit once
   the two agree on one name.

---

## 3. `message_ref` — the transcript marker (C3)

### 3.1 What goes on the wire

The hook adds the bare transcript entry uuid to the fire row, under **both**
contract names (decision 10) — one value, two keys:

```
fire_id · rule_id · rule_version · session_id · agent_id · repo · branch ·
tool · hook_phase · mode · dedup_key · raw_matches_before_fire · fired_at ·
converted · converted_at ·
message_ref · source_message_id                ← new, same value
```

The local ledger stores the marker once, as `message_ref`; `wire_row` is what
fills `source_message_id` from it on the way out. A pre-existing ledger row
that predates this change sends null under both names, not a `KeyError`.

The hook does **not** send `conv_id` (resolved server-side from
`session_id → team_conversations.source_id`) and does **not** send
`message_logical_id` (the server resolves it by prefix-matching
`source_message_id LIKE '<message_ref>_%'` and taking the lowest ordinal).
`message_ref` is stored raw and permanently, so a fire can be re-resolved if
capture's id scheme changes.

A fire naming a conversation or message the server has not ingested yet is
accepted with nulls and linked by the backfill. That is the normal case — fires
flush at ≥10 rows or ≥5 minutes, capture flushes per turn — not an edge case.

### 3.2 How the uuid is found

Claude Code's hook payload carries `session_id`, `transcript_path`, `cwd`,
`tool_name`, `tool_input` and `tool_response` — **no message id**. So read the
tail of `transcript_path`.

In the current transcript format each assistant *block* is its own record with
its own `uuid`, and the record carrying the tool call is already written by the
time either lane runs:

```
assistant  cec44043  [thinking]
assistant  4cc352fa  [tool_use name=Bash]   ← this uuid
user       ac8de49e  [tool_result]
```

**Algorithm.** Read the last 64 KiB of `transcript_path`; discard the first
(partial) line when the file is larger than the window; walk the complete lines
**backwards**, JSON-decoding each:

1. the record whose `tool_use` block matches this call by **`name` AND
   `input`** → its `uuid`. **Exact**, including when one turn fans out several
   calls to the same tool — matching on `name` alone would hand every one of
   them the newest sibling's uuid.
2. else the newest record with a `tool_use` for this tool; else the newest
   `type == "assistant"` record's `uuid`. **Turn-level fallback**, which is the
   granularity the server resolves at anyway.
3. else, if nothing parsed at all and the file is larger than the window, retry
   **once** with a 1 MiB window and repeat 1–2. A single `Write`/`MultiEdit`
   `tool_use` record can exceed 64 KiB, and those are exactly the calls most
   likely to fire an edit rule — without the escalation they would be the ones
   that lose their marker.
4. else `None`.

The escalation is deliberately one step and deliberately capped: it fires only
when the cheap pass found *nothing*, so the common case never pays for it.

**Malformed input is not an error.** A missing, unreadable, empty, truncated or
non-JSON transcript degrades to `None` — never an exception, never a stderr
byte, never a non-zero exit. Every field access inside the scan is defensive
for the same reason: one bad record (a `message` that is a list, seen in the
wild) must cost that record only, not the whole scan and the fallback it had
already found.

### 3.3 Where it is attached

| Lane | `message_ref` |
|---|---|
| `pre` (PreToolUse) | resolved |
| `post` (PostToolUse) | resolved — the same uuid as `pre` for the same call |
| `session` (SessionStart) | `None` — there is no tool call, and often no transcript yet |

Both lanes resolve it **once per invocation**, before `log_fires`, and pass it
down through `ctx`. It is written to every row of that call — `advise` rows and
`suppressed` rows alike, since a suppressed fire happened at the same instant.

Retries reuse the row from the ledger, so a retried fire keeps the `fire_id`
**and** the `message_ref` it was written with. The hook never mints a new one.

---

## 4. `fetch_book` — the query that stopped working

```diff
- q = "status=active&view=hook&repo=" + urllib.parse.quote(repo, safe="")
+ q = "view=hook&repo=" + urllib.parse.quote(repo, safe="")
```

Everything else about `fetch_book` is unchanged: `If-None-Match`, 304 → touch
`fetched_at`, 200 → rewrite the cache, anything else → leave the cache exactly
as it was.

This is safe on both server generations because the hook view is active-only on
the server and `status` is re-checked client-side at every delivery site
(`session_digest`'s `in_scope`, and the main loop's per-rule guard). A row that
arrives without a `status` field — which is what the hook view actually sends —
defaults to `active` in `to_hook_rule`, unchanged.

---

## 5. Path scope

### 5.1 The wire

`?view=hook` rows carry `scope_paths` and `scope_exclude_paths` as lists of
glob strings (verified live):

```json
{"rule_id": "…", "scope_repos": ["agent-plugins"],
 "scope_paths": ["plugins/memhub/scripts/**", "tests/*.py"],
 "scope_exclude_paths": ["**/vendor/**"]}
```

`to_hook_rule` copies them to `_scope_paths` / `_scope_exclude_paths`
(underscore-prefixed, like `_scope_repos`, so a matcher key can never collide
with them), keeping only non-empty strings and dropping the key entirely when
nothing survives. Both are copied on **every** delivery shape — session, anchor,
ordering and matcher rules alike — because the copy happens before those early
returns.

That is the *server* row shape. `to_hook_rule`'s other branch, the
pass-through for a row that already carries an `on` key, does no renaming at
all, so such a row must name `_scope_paths` itself. `_scope_repos` has always
behaved this way; the pass-through exists for rows the hook has already
normalised (and for the tests), not for anything the server sends.

### 5.2 The dialect

gitignore-style, matched against the path **relative to the worktree root**:

| Pattern | Matches | Does not match |
|---|---|---|
| `src/**` | `src/a.py`, `src/deep/b.py` | `docs/a.py` |
| `tests/*.py` | `tests/a.py` | `tests/sub/b.py` |
| `*.py` | `a.py`, `src/deep/b.py` | `a.md` |
| `**/vendor/**` | `a/vendor/x.js` | `a/vendors/x.js` |

- `**` crosses directory separators; `*` and `?` do not.
- A pattern containing **no** `/` matches its last segment at any depth.
- A trailing `/` or a bare directory name (`src`) also matches everything
  beneath it, so `src` behaves like `src/**`.
- Matching is on the POSIX-separator form of the path; a Windows `\` is
  normalised to `/` first.
**The matcher is deliberately not a regex.** Every other pattern that comes off
the wire in this file is linted by `rx_ok` (length cap plus the
nested-quantifier denylist) before it reaches `re`. A glob has no such lint,
and the obvious translation — `*` → `[^/]*`, `**` → `.*` — is the textbook
catastrophic-backtracking shape: measured on the first implementation,
`*a*a*a*a*a*a*a*a*a*a*b` against a 120-character path took **over five
seconds**, the entire `pre` budget, and `re` has no timeout. A scope glob is
data any teammate can put in a rule through `create_rule`, so that is a rule
author stalling every teammate's session on every tool call.

So matching is a two-pointer wildcard match instead — one backtrack point,
advanced monotonically — at both levels: `_seg_match` for one segment, and
`_glob_match` for `**` across segments. Worst case is O(len(pattern) ×
len(path)) with no exponential term; the same pathological pattern now costs
**0.024 ms**. `tests/rulebook_hook_test.py` keeps a timing gate on it so the
regex form cannot come back by accident.

- Patterns are split into segments once per pattern and cached in a
  module-global dict. The hook is a **fresh process per tool call**, so that
  cache is per invocation, not per session — it saves the repeated matches
  within one call, not across calls.
- A pattern that is not a string, is longer than 256 characters, or that
  splits to nothing is skipped rather than fatal — and if that leaves **no**
  usable include glob, the rule stays in scope. Silencing a rule because its
  scope was unreadable would invert §5.3 clause 2.

### 5.3 The rule

`scope_ok(rule, repo, gitdir, path=None)` — the repo test is unchanged; the
path test is appended:

1. No `_scope_paths` and no `_scope_exclude_paths` → in scope (today's
   behaviour, for every rule that carries no path scope).
2. `path` is `None` or empty → **in scope** (decision 8). A Bash call, an
   `output` result, the session digest and an anchor handle have no path.
3. `path` matches any `_scope_exclude_paths` glob → out of scope. Exclusion
   wins over inclusion.
4. `_scope_paths` present → in scope only if `path` matches one of them.
5. Otherwise in scope.

A path outside the worktree root (an absolute path elsewhere on disk) cannot be
made relative, and is treated as **not matching** any include glob — it is not
part of this repo, so a repo-relative scope should not claim it.

`scope_ok` runs the **repo** test first and the path test second, so a rule
scoped to a different repo never pays for path matching at all.

Call sites: `session_digest` passes no path; the main loop passes the current
call's `file_path` (empty for Bash), resolved against the worktree root once
per call. A `file_path` that is present but null — or any non-string — is
treated as no path, not as the literal string `"None"`, which would look like
an in-worktree relative path and wrongly exclude the rule.

`rel_path` compares normalised paths, then falls back to comparing
`os.path.realpath` of both. Without that fallback a symlinked worktree root
(macOS `/tmp` → `/private/tmp` is the everyday case) makes every include-scoped
rule silent for the whole session.

---

## 6. Non-negotiable constraints on this file

These predate the spec and outrank anything in it.

- Every failure path stays **silent and exits 0**. A broken hook must never
  touch a tool call or a session.
- **Stdlib only**, and the tool-call lanes must not start importing auth/HTTP
  modules (`_api` and everything under it stays lazily imported by `fetch` and
  `flush`).
- The transcript read is **bounded** — 64 KiB, escalating once to 1 MiB. The
  whole file is never parsed.
- Retries reuse the same `fire_id`; a new one is never minted.
- The `pre` lane's latency budget is 5 s in `hooks.json` and is the tightest in
  the plugin. Measure any addition to it.

---

## 7. Tests

`tests/rulebook_hook_test.py`, house style: stdlib only, no pytest, driven by
`tests/run_all.py`, isolated with a tmpdir plus `MEMHUB_*` env overrides so it
never reaches a live backend.

| Test | Asserts |
|---|---|
| `message_ref` exact | a `tool_use` record whose `name` matches `tool_name` wins over a newer assistant record |
| `message_ref` fallback | no name match → the newest assistant record's uuid |
| `message_ref` post lane | the `pre` and `post` lanes resolve the **same** uuid for one call |
| `message_ref` oversize | a `tool_use` record larger than the 64 KiB window is still found via the 1 MiB escalation |
| `message_ref` degrades | missing / unreadable / empty / truncated / non-JSON transcript → row present, `message_ref` null, exit 0, no output |
| `message_ref` on the wire | `message_ref` ∈ `WIRE_KEYS`; `wire_row` emits the same value under `message_ref` AND `source_message_id`; a pre-existing row without the key yields `None` under both, not a `KeyError` |
| session lane | posture fires carry `message_ref: None` |
| `fetch_book` query | the request path contains `view=hook` and **no** `status=` — asserted in **both** suites, since `rulebook_client_test.py` pinned the old string |
| path scope | the dialect table in §5.2, both include and exclude, exclusion winning |
| path scope, no path | a path-scoped rule still fires on Bash and still shows at session start |
| path scope absent | a rule with no path scope is unaffected |
| path scope, malformed | an unhashable or untranslatable glob is ignored rather than raising or silencing; one usable glob among junk still narrows |
| path scope, pathological | a 20-wildcard glob against a 255-character path stays under 5 ms — the timing gate that keeps the regex form from coming back |
| path scope, symlinked root | a worktree reached through a symlink still resolves to a repo-relative path |
| path scope, null `file_path` | a present-but-null path is no path, and does not silence a path-scoped rule |
| `message_ref`, malformed record | a `message` that is a list costs that record only; the fallback survives |
| `message_ref`, parallel calls | two `Bash` calls in one turn each resolve to their OWN record, by input |
| `message_ref` is lazy | a call that fires nothing never reads the transcript — the whole latency argument, asserted rather than assumed |

---

## 8. Verification

1. `python3 tests/run_all.py` and
   `uv run --with 'mcp<2' python tests/run_all.py` — both green.
2. **`pre`-lane latency, measured, as the acceptance test requires.**

   | Measurement | Result |
   |---|---|
   | End-to-end `pre` lane, quiet call (nothing fires), 9.4 MB transcript, 60 interleaved A/B runs | base 49.7 ms → **50.0 ms** (Δ +0.27 ms) |
   | End-to-end `pre` lane, a rule fires (marker is read), same conditions | base 49.1 ms → **50.9 ms** (Δ +1.76 ms) |
   | `message_ref()` alone, five real 5.6–13.8 MB transcripts, 50 calls each | median **0.27–0.37 ms**, p90 0.44 ms |
   | `path_in_scope()`, first call in a fresh process (what the hook actually pays) | **0.015 ms** |
   | `path_in_scope()`, warm | 0.0018 ms |

   Worst case is **+1.8 ms on a 5 000 ms budget — 0.035% of it**, and only on
   a call that fires. Two corrections to earlier drafts of this table, both
   worth keeping visible: a first naive benchmark (non-interleaved, 40 runs)
   reported +25 ms on the quiet path, which is impossible — that path reads no
   transcript at all — and was machine drift that interleaving removed. And an
   earlier `path_in_scope` figure (0.00084 ms) was a warm-cache in-process
   measurement; the hook is a fresh process per tool call, so the cold number
   above is the one that is actually paid.

   The post-only fallback the wave plan offered is therefore not needed, and
   both lanes carry the marker.
3. Every changed hook script smoked by piping realistic event JSON in: exit 0,
   no stdout on the unhappy path, well inside its `hooks.json` timeout.
4. **C3 round trip on staging, run 2026-08-28** (never production). A scratch
   rule was filed `active` with `scope_paths`, and:
   - `rulebook_hook.py fetch` pulled it into the local book — which is the
     fetch fix proved end to end, since the old query returns a 400 and would
     have cached nothing;
   - a `pre` call fired it in a real session and resolved the marker to
     `bb3bf7e2…`, confirmed by reading the transcript back to be exactly the
     assistant record carrying that Bash `tool_use`;
   - `flush final` posted the batch; the row arrived with
     `source_message_id` equal to the local `message_ref`.

   `message_logical_id` and the row's `session` come back null: both are
   resolved server-side by `FIRE-LEDGER`'s backfill, per C3, and are not this
   domain's to produce.

   Cleaned up by PATCHing the rule to `deprecated` — `scripts/purge_today.py`
   does not cover rules, and there is no `delete_rule`. The probe fires
   themselves cannot be deleted and were left in place on the scratch rule.
5. **`/bug-hunt` on the diff, run 2026-08-28.** An adversarial pass found one
   critical and four lesser bugs, all reproduced before being fixed and all
   now covered by a test: the glob ReDoS (§5.2), a malformed transcript record
   discarding the whole scan, parallel same-tool calls resolving to a
   sibling's uuid, a null `file_path` becoming the literal path `"None"`, and
   a symlinked worktree root silencing every path-scoped rule. It also caught
   three claims in this document that were wrong — the circular status-filter
   argument (decision 7), the warm-cache latency figure (above), and a
   reference to a script that does not exist in this repo (decision 6) — which
   are corrected in place rather than quietly dropped.

## 9. Out of scope

`MAX_POSTURE` (decision 4) · the two authoring skills and `rulebook_conflicts.py`
(`AUTHORING-SKILLS`) · every backend change, including whether `message_ref`
resolves to a `message_logical_id` (`FIRE-LEDGER`) · the Rulebook and fire
screens (`RULEBOOK-UI`, `FIRE-UI`) · the `gate` tier (waves decision 1) ·
`machine_check` delivery (Phase 2) · excerpts (waves §6 gate: leave as-is).
