---
title: "Spec: Codex capture correctness"
type: spec
---

# Spec: Codex capture correctness

Three independent correctness defects in the Codex lane, from ENG-1070 (two)
and ENG-1075 (one). Each was reproduced against the MemHub **staging**
database or proven from code before this spec was written; the evidence is
recorded inline so an implementer can re-run it rather than trust it.

Base: `origin/main` @ `2538729`. Branch: `fix/codex-capture-correctness`.

---

## Goal

1. **A Codex install that cannot capture must say so.** Today the hook bridge
   exits 0 in silence when it cannot find the plugin, and the installer's own
   `status` reports OK while capture is dead.
2. **Stop importing Codex's internal guardian/subagent review sessions.** They
   are 35% of all captured Codex sessions in staging.
3. **Make Codex rule fires linkable to the session that produced them.** The
   fire's `session_id` and the captured conversation's `source_id` are in
   different namespaces, so they can never match.

## Non-goals

- **Backfilling already-orphaned fires.** Existing rows keep their un-namespaced
  `session_id`. Linking them is a backend concern (see *Handoff to backend*).
- **Deleting the 103 guardian sessions already in staging.** This spec stops
  new ones; cleanup is a separate data task.
- **Building a Cursor rule-evaluation lane.** Cursor has none today
  (`cursor-hooks.json` routes only to `cursor_capture.cmd`). We add a guard so
  a future lane is correct on day one, and nothing more.
- **Changing how the staging plugin is packaged.** The dangling-symlink root
  cause of defect ①  is a release/packaging matter that
  `RELEASING.md` already documents. We make the failure *loud*, not impossible.
- **Bumping `memhub-staging`'s manifest.** Deliberately decoupled — see
  *Decisions*.

---

## Evidence

All queries were run **read-only against MemHub staging**
(`db.uyctquxfccwjdbobjnoz.supabase.co`, the commented-out `DATABASE_URL` in
`MemHub-Backend/.env.local`). Production was never touched.

### ① Silent capture death

`plugins/memhub/scripts/codex_hook_bridge.py:57-74` gates on one file:

```python
versions = [path for path in (cache / marketplace / plugin).glob("*")
            if (path / "scripts" / "codex_flush.py").is_file()]
```

and `main()` (`:379-381`) does:

```python
root = resolve_plugin_root()
if root is None:
    return 0          # no output, no breadcrumb, no trace
```

Root cause of the missing file: `plugins/memhub-staging/{scripts,hooks,skills,
references}` are **relative symlinks escaping the plugin root** into
`../memhub/`. Any installer that copies only the plugin subdirectory leaves
them dangling. `RELEASING.md:113-121` documents this ("observed live on PR #56:
the install came up with no `scripts/` directory at all and still reported
success") and notes it makes staging "nonconforming under Agent Plugins 1.0 …
one more reason it never enters the Codex/Cursor catalogs".

Aggravator: `setup_codex_hooks.status()` (`:233-240`) checks only `hooks.json`
handler equality and the runner's bytes. It never checks that a plugin root
resolves, so it prints `OK` while capture is dead.

### ② Guardian sessions imported as real sessions

```sql
select conversation_source_platform, count(*),
       count(*) filter (where name ilike 'The following is the Codex agent history%')
from team_conversations group by 1;
```

| platform | total | guardian-titled |
|---|---|---|
| claude | 1751 | **0** |
| memhub | 889 | **0** |
| **codex** | **298** | **103** |
| cursor | 10 | **0** |

**103 of 298 (35%)**, of which **94 arrived in the last 3 days** — active, not
historical. Contamination is exclusive to the codex platform. `source_config`
is `{}` for every Codex row, so nothing server-side distinguishes them: the
filter must be client-side.

No guardian/subagent filter exists in the Codex reader. The only `subagent`
handling is `subagent_history_start_ordinal` (`readers/codex_history.py:78`,
added by #238), which concerns inherited context *within* one rollout — a
different problem. By contrast Claude's path *does* exclude subagent
transcripts (`readers/claude.py:11`, `import_session.py:116`). This is a host
asymmetry, not a missing feature.

**The discriminator is in the rollout**, so no sqlite dependency is needed.
`session_meta.payload` of a real session (cli 0.146.0) carries:

```
source           'cli'
thread_source    'user'        ← discriminator
```

Codex's own `~/.codex/state_5.sqlite` `threads` table corroborates the shape
with `thread_source`, `agent_role`, `has_user_event` and a
`thread_spawn_edges(parent_thread_id, child_thread_id, status)` lineage table —
matching ENG-1070's "marked `subagent → guardian`". We do **not** read that DB.

### ③ Fires can never link

| host | capture writes `conversation_id` | fire posts `session_id` | match |
|---|---|---|---|
| Claude — `flush_turn.py:456` | `session_id` (**bare**) | bare | ✅ |
| Codex — `codex_flush.py:526` | `f"codex-{sid}"` | bare | ❌ |
| Cursor — `cursor_flush.py:1045` | `f"cursor-{uuid}"` | bare | ❌ (latent) |

`rulebook_hook.log_fires` (`:3690`) writes `"session_id": ctx["session"]`, and
`ctx["session"]` is `data.get("session_id", "")` (`:4134`) — the raw hook
payload value, never namespaced, on every host. Claude matches only because it
happens to have no prefix.

Staging corroboration:

```
platform | conversations | source_id prefixed
claude   |          1751 | 0      (bare uuid)
codex    |           298 | 275    (codex-…)
cursor   |            10 | 5      (cursor-…)
```

Fires linked to a Codex conversation: **0**. Fires whose `session_id` matches a
Codex `source_id` in either form: **0**.

**Caveat, stated plainly.** Staging contains *zero* Codex fires at all (every
one of its 5,668 fires carries a UUIDv4 / Claude-shaped id; Codex ids are
UUIDv7). So staging cannot demonstrate the orphaning directly — it has no rows
to orphan. ENG-1075 was observed in **production**
(repo `memhub-production-release-e2e`, absent from staging), which this work
deliberately did not query. The production screenshot shows `Not linked yet`,
i.e. the row **exists** with `conv_id` NULL — consistent with the namespace
mismatch above. The mechanism is therefore proven from code plus the staging
schema, **not** from the production rows.

---

## Design

### Fix ① — the bridge speaks for itself

`plugins/memhub/scripts/codex_hook_bridge.py`

When `resolve_plugin_root()` returns `None`:

1. **Write a breadcrumb** to
   `~/.config/memhub-plugin/codexflush/_bridge.json` in the shape
   `capture_health` already reads (`capture_health.py:46,300`):
   `{"last_error": "plugin_root_unresolved", "last_error_at": <epoch float>}`.
   Stdlib-only, no import from the (missing) plugin root.
2. **On `SessionStart` only**, print one `systemMessage` telling the user
   capture is off and how to repair it. This is essential: with no plugin root,
   `capture_health.py` *cannot run* (it lives in that same missing root), so a
   breadcrumb alone would never surface. SessionStart is naturally once-per-
   session, so no extra rate limiting is required.
3. **Still `return 0`.** A memory hook must never block the host.

When the root *does* resolve, **clear** `_bridge.json` — a stale breadcrumb
must not outlive the failure it recorded (this repo has been bitten by exactly
that on the recall lane).

`plugins/memhub/scripts/setup_codex_hooks.py`

`status()` returns `(hooks_ok, actual, expected, root)` and additionally reports
whether a plugin root resolves, reusing the bridge's own `resolve_plugin_root`
rather than reimplementing the search. Two rules, both learned the hard way in
review:

- **It must ask with the HOOK's environment, not the caller's.**
  `resolve_plugin_root` honours `PLUGIN_ROOT` / `CLAUDE_PLUGIN_ROOT` first, and
  the setup skill always runs with one set (`skills/setup/SKILL.md`: "Codex sets
  `PLUGIN_ROOT`; Claude Code sets `CLAUDE_PLUGIN_ROOT`") while the user-level
  bridge is invoked with neither. Resolving under the caller's environment
  reported a healthy plugin for an install where every real hook event finds
  nothing — the check defeated the very failure it was written for. So
  `_plugin_root(home)` clears those three variables and pins `CODEX_HOME` to
  the `home` under test, restoring the caller's environment afterwards. Pinning
  `home` also makes `status --codex-home X` judge X rather than `~/.codex`.
- **The headline still describes the HOOKS.** `hooks_ok` drives the
  `OK / NOT INSTALLED` line and a missing plugin gets its own line; only the
  exit code folds the two. Printing `NOT INSTALLED (4/4 handlers)` invited the
  setup skill to reinstall and re-trust handlers that were already correct.

### Fix ② — allowlist the thread source

New helper in `plugins/memhub/scripts/readers/codex.py`:

**The gate is a denylist, and the reason is upstream's own type.** From
`enum ThreadSource` in `codex-rs/protocol/src/protocol.rs`:

```rust
pub enum ThreadSource {
    User,                 // "user"
    Subagent,             // "subagent"
    GuardianReview,       // "guardian_review"
    Feature(String),      // ← OPEN: any unnamed string parses here
    MemoryConsolidation,  // "memory_consolidation"
}
```

`Feature(String)` is why the generated TypeScript is
`export type ThreadSource = string;` rather than a union. A product surface
that ships tomorrow arrives as a `Feature`, and **a Feature thread is the
person's** — that is what the variant means.

```python
BOT_THREAD_SOURCES = ("subagent", "guardian_review", "memory_consolidation")
KNOWN_OWN_THREAD_SOURCES = (None, "", "user")

def thread_source_of(rows) -> str | None: ...
def is_own_thread(rows) -> bool: ...            # thread_source NOT in BOT_…
def thread_source_of_path(path) -> str | None:  # bounded header read
def is_own_thread_path(path) -> bool: ...       # same predicate, from disk
```

- **Deny by name; capture everything else.** An allowlist against an open type
  drops real work the first time Codex names a surface — and because the skip
  advances the watermark, a session that has since stopped growing never
  re-flushes. That loss is unrecoverable; importing one stray bot thread is
  not. Asymmetric risk, asymmetric default.
- **Absent ⇒ own.** Rollouts predating the field (cli < ~0.142) have no
  `thread_source`; they must keep being captured.
- **An unfamiliar value is captured and noted** (log line only — *not* a
  `last_error`, which would print "capture failed" over a session that
  captured fine), so a new Feature surface gets classified deliberately.
- **An unrecognised value is skipped *and recorded*.** A kind in
  `KNOWN_BOT_THREAD_SOURCES` is routine: skipped quietly, and it clears any
  earlier `last_error`/`fail_streak` the way the other never-contacted-the-
  server no-ops do, so a stale failure cannot outlive a session nothing will
  retry for. Any **other** non-user value writes
  `last_error="unknown_thread_source"` with a timestamp. This matters: if Codex
  renames the marker for ordinary sessions, that path is *every* session, and a
  silent skip would rebuild the silent-capture-death bug on a new axis. The
  watermark advance makes it unrecoverable after the fact — a session that has
  stopped growing never re-flushes — so the alarm is the containment.

`capture_health.py` gains `_REASONS` entries and remedies for both
`plugin_root_unresolved` and `unknown_thread_source`; without them the generic
"run `/memhub:login --status`" advice points at a credential that is fine.

Call sites, all through the one predicate:

| path | gated | why |
|---|---|---|
| `codex_flush._flush` | yes, before `to_canonical` | never capture a bot thread |
| `capture.py` candidate scan | yes | do not offer candidates capture will refuse |
| `find_sessions.py` | yes | a guardian review *contains* the reviewed conversation, so it scores like a strong authorship match on the very evidence the ranking uses |
| `readers_cli.py` / `capture.py list` | **no** | a documented read-only stream with a wire contract and golden files; filtering inside `list_sessions` would break it, and an explicit enumeration should show what is on disk |

### Fix ③ — namespace at the wire boundary only

The naive fix — prefixing `ctx["session"]` — would change the key used for
local ordering state, obligations, dedup and `state_path()`, breaking
in-flight sessions. Instead:

1. `log_fires` records a **local-only** `host` field on each ledger row
   (precedent: `rulebook_id` is local-only on purpose, `rulebook_hook.py:2470`).
   `host` is **not** in `WIRE_KEYS`.
2. At the wire projection (`wire_row`), `session_id` is rendered through
   **`pr_link.conversation_id_for`** — the helper that already owns this
   projection for the PR-link lane. It is not reimplemented: a second copy is
   how the two lanes come to disagree about what a Codex session is called, and
   the existing one already handles what a fresh one forgets (surrounding
   whitespace, a host spelled `Codex`, an id that already carries its prefix).
   Imported lazily inside the function — only the flush lane projects rows, so
   the per-call pre/post lanes never pay the ~17 ms import — and on a fail-open
   path, so a fire that cannot be namespaced still ships rather than taking the
   hook down.
3. The host reaches the hook via a `--host <claude|codex|cursor>` flag,
   matching the existing convention (`capture_health.py --host`,
   `pr_link_trigger.py --host codex`). Default `claude` preserves today's
   behaviour exactly.
4. `codex_hook_bridge._rulebook_result` passes `--host codex`.
5. `cursor_capture.upgrade_context` passes `--host cursor`. This is **inert
   today and deliberately so**: the `upgrade` lane returns before
   `rulebook_hook` builds its ctx, so the flag is not read, and Cursor has no
   rule-evaluation lane to log a fire from at all
   (`cursor-hooks.json` routes every event to `cursor_capture.cmd`). It is
   passed because this is the one place Cursor names itself to the hook; the
   guard that actually matters is `wire_session_id` already handling `cursor`,
   so a future Cursor fire lane is correct on its first day.

Local state, ordering, dedup and obligations continue to key off the **raw**
session id. Only the bytes that cross the wire change.

---

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Client-side namespacing here; backend backfill handled separately | Keeps this PR inside `plugins/memhub/`; the backend fix lives in another repo |
| D2 | ~~Allowlist `{absent, "user"}`~~ → **denylist of the named bot kinds** | **Reversed in review.** Codex's review of this PR reported a user-started web thread with `thread_source="codex_web_code_review"`, and upstream's `enum ThreadSource` confirms why: `Feature(String)` is an open variant, so the value space is unbounded and the *non-bot* side is the open one. An allowlist silently dropped real sessions, unrecoverably (the skip advances the watermark). See D11. |
| D3 | Namespace at the wire, not in `ctx` | Preserves local state keys; no in-flight session breakage |
| D4 | Add the Cursor guard, don't build the lane | One-line correctness for free; the lane is a separate ticket |
| D5 | Bump **only** the four production manifests `0.58.3 → 0.58.4`; leave `memhub-staging` at `0.57.0` | The decoupling is deliberate: `16798a9` ("Keep staging version independent of the production release", PR #229) removed staging from `version_parity_test.py` and set `RELEASING.md:72` to "Leave its manifest unchanged during a production-only release". `CONTRIBUTING.md:104-106` still says lockstep and is **stale**. |
| D6 | Fail open everywhere | A memory hook must never block the host agent |
| D7 | `wire_session_id` delegates to `pr_link.conversation_id_for` rather than owning a namespace map | Found in review: the helper already existed. A parallel mechanism is the default wrong answer, and the local copy was already wrong on surrounding whitespace and on a host spelled `Codex` — both of which silently ship an *unnamespaced* id, i.e. the very bug this fixes |
| D8 | ~~Report an unrecognised kind as a capture failure~~ → **capture it and log it** | Superseded by D11: an unfamiliar kind is now captured, so it is not a failure. A `last_error` there would print "capture failed" over a session that captured fine |
| D9 | `_plugin_root` resolves under the HOOK's environment, not the caller's | Found in review: the setup skill always runs with `PLUGIN_ROOT` set, so the check reported a healthy plugin for a dead install — it could not detect the failure it was written for |
| D10 | Discovery gates candidate *selection*, not read-only *enumeration* | `readers_cli` has a published wire contract and golden files; `capture.py list` is an explicit "show me what is on disk" |
| D11 | The bot literals are taken from upstream source, not inferred | `guardian_review`, not `guardian` — the invented literal matched nothing, so every real guardian skip would have raised a false "unknown kind" alarm. `memory_consolidation` was missing entirely. Read from `codex-rs/protocol/src/protocol.rs`, not guessed |

---

## Milestones

1. **M1 — bridge + installer honesty** (`codex_hook_bridge.py`,
   `setup_codex_hooks.py`, `capture_health.py`)
2. **M2 — guardian allowlist** (`readers/codex.py`, `codex_flush.py`,
   `capture.py`, `find-contributing-sessions/scripts/find_sessions.py`,
   `capture_health.py`)
3. **M3 — wire namespacing** (`rulebook_hook.py`, `codex_hook_bridge.py`,
   `cursor_capture.py`)
4. **M4 — tests** — new `tests/codex_correctness_test.py`, house style: no
   pytest, no new dependencies, isolated via tmpdir + `MEMHUB_*` env
   overrides, never reaching a live backend. `run_all.py` auto-discovers
   `*_test.py`, so it needs no edit — which also keeps this work clear of the
   "Codex / Cursor reliability matrix" spec's globs.
5. **M5 — version bump** per D5, plus README / `plugin.json` /
   `marketplace.json` description updates where user-facing behaviour changed.

### Verification

- `python3 tests/run_all.py` green **and**
  `uv run --with 'mcp<2' python tests/run_all.py` green (the second covers the
  mcp-importing suites).
- Every changed hook script smoke-tested by piping realistic event JSON in:
  exits 0, prints nothing on the unhappy path, finishes inside its
  `hooks.json` timeout.
- Manual runs go to staging with `MEMHUB_MCP_BASE_URL` set **explicitly** —
  the staging plugin's symlinked `scripts/` otherwise auto-resolves to prod.
  Clean up with `scripts/purge_today.py`.
- **Never** exercise `api.memhub.xtrace.ai`.

---

## Open questions

1. ~~The exact non-user `thread_source` literal is unconfirmed.~~
   **Resolved.** Read from upstream: `enum ThreadSource` in
   `codex-rs/protocol/src/protocol.rs` gives `user`, `subagent`,
   `guardian_review`, `memory_consolidation`, and the open `Feature(String)`.
   What remains open is which *Feature* surfaces exist; the denylist makes that
   safe — a new one is captured, not dropped.
2. **Backend backfill** — the 103 guardian conversations and the orphaned
   production fires both need a data decision. Out of scope here.
3. **`CONTRIBUTING.md:104-106` is stale** (contradicts `RELEASING.md:72` and
   the parity test). Left untouched deliberately; worth its own PR.
4. **`version_parity_test.py` cannot catch staging drift by design** (D5). If
   the team ever wants staging drift detected without re-coupling the bump, it
   needs a different check.

## Handoff to backend

For the MemHub backend, separately from this PR:

- Resolve a fire to a conversation by matching `session_id` against
  `source_id` **and** `'<platform>-' || session_id`, so historical orphaned
  Codex fires link retroactively.
- Decide the fate of the 103 guardian conversations already in staging (and
  the production equivalent).
