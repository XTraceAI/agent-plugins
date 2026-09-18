---
title: "Spec: Codex capture correctness"
type: spec
---

# Spec: Codex capture correctness

Defects in the Codex lane from ENG-1070 and ENG-1075. Every fix here is backed
by something **observed** — in the staging database, in a live Codex session, or
in upstream source. A third defect was investigated at length and deliberately
**not** fixed; *Investigated and not fixed* is the most useful section of this
document, because it records what the evidence would not support.

Base: `origin/main` @ `04bb90c` (v0.59.0). Branch: `fix/codex-capture-correctness`.

---

## Goal

1. **Stop capturing Codex's own threads as the person's sessions.**
2. **Make a Codex rule fire joinable to the session that produced it** — on both
   fire lanes.
3. **Stop telling users the Codex hooks bridge is optional.** It is the only
   path by which MemHub hooks run on Codex.

## Non-goals

- **Backfilling.** Orphaned fires keep their ids; captured bot threads are not
  deleted. Backend data decisions.
- **Repairing a broken install.** See *Investigated and not fixed*.
- **A Cursor rule lane.** Cursor has no rule evaluation today.

---

## Evidence

Staging reads were read-only via the commented-out `DATABASE_URL` in
`MemHub-Backend/.env.local`. Live Codex work ran against the real `~/.codex`
with `MEMHUB_MCP_BASE_URL` pinned to staging.

### ① Codex's own threads captured as sessions — REPRODUCED LIVE

Staging, `team_conversations`:

| platform | total | titled `'The following is the Codex agent history%'` |
|---|---|---|
| claude | 1751 | **0** |
| memhub | 889 | **0** |
| **codex** | **298** | **103** |
| cursor | 10 | **0** |

**Attribution matters and was initially got wrong:** all 103 belong to one
teammate's machine, not to the machine this work was done on. The defect is
real; it is not "your session list".

**Then reproduced live.** `codex review --uncommitted` on real Codex produced
two threads:

```
id                                    thread_source  source                  title
01a0ad2c-9184-7e50-b6cf-e573ee393214  subagent       {"subagent":"review"}   Review the current code changes…
01a0ad2c-90e8-7fe1-ba82-c1f74c76d905  user           exec                    Review the current code changes…
```

Fed to the **unfixed** bridge, the bot thread landed in staging:

```
flushed 16 records → codex-01a0ad2c-9184-7e50-b6cf-e573ee393214
row: ('codex-01a0ad2c-9184-…', 5be4db26-…, 'Review the current code changes…')
```

**16 records for the bot thread against 2 for the real one** — the pollutant is
the larger of the two. With the branch installed, the same payload:

```
not this person's thread (thread_source='subagent') — not captured
```

`subagent` is the literal observed live. `guardian_review` and
`memory_consolidation` come from upstream's enum and the 0.154 binary — **read,
not observed**. The distinction is why the gate denies by name rather than
allowing by name.

### ② Fires cannot be joined to their session — two lanes

| host | capture writes `conversation_id` | fire posted `session_id` | match |
|---|---|---|---|
| Claude — `flush_turn.py` | `session_id` (**bare**) | bare | ✅ |
| Codex — `codex_flush.py` | `f"codex-{sid}"` | bare | ❌ |
| Cursor — `cursor_flush.py` | `f"cursor-{uuid}"` | bare | ❌ (latent) |

`codex-X` ≠ `X`. This is arithmetic, not inference. Claude matches only because
it happens to carry no prefix.

**And main's #240 duplicated it.** That PR added a second path — `events.jsonl`
→ `/fire-events`, which "the server folds into each fire's outcome" — whose row
builder also wrote `"session_id": ctx["session"]` bare. An event naming the
session differently from the fire it answers folds into nothing. Both lanes are
fixed here.

**Not reproduced:** ENG-1075's `Not linked yet` was seen in **production**,
which this work did not query. The live test could not reach it either — firing
a rule needs a trusted hook dispatch, and trust could not be granted
non-interactively. The mismatch is certain; that it causes the production
symptom is strongly supported, not demonstrated.

### ③ The Codex hooks bridge is mandatory, and the docs said otherwise

```
$ codex features list
plugin_hooks    removed    false
```

Verified on **0.146 and 0.154**. Enabling it is a no-op — a `removed` stage
stays `false`. Proven end-to-end rather than inferred: a throwaway plugin whose
only hook appended to a file never created it; moving the same handlers into
`~/.codex/hooks.json` put them in the trust queue immediately.

So the plugin's own `.codex-plugin/plugin.json` `"hooks"` key is **inert**, and
`setup_codex_hooks.py` is not a "0.148–0.149 compatibility shim" — it is the
only path by which MemHub hooks run on Codex. The docs named a version window
that does not match reality, and a user who installs the plugin and never runs
`memhub:setup` gets **zero capture, silently**.

Trust compounds it: every handler arrives `"trustStatus": "untrusted"` and is
never dispatched until approved in `/hooks`.

### A note on log evidence — do not repeat this mistake

`~/.config/memhub-plugin/codexflush/log` shows hundreds of `no session identity
in payload` lines. **They are test output.** `codex_flush` fixes `STATE_DIR`
from `Path.home()` at import and the suites did not redirect `$HOME`. Measured:
237 such lines over **33 distinct seconds**, 209 of them inside one 13-second
window at 16–29 per second, and the rotated backup holds the identical burst so
a naive cross-file count double-counts. `~/.codex/hooks.json` did not even exist
during that period, so none of them can be a hook fire.

PR #217 (ENG-1034a) established this first and builds the fix. **Until it lands,
this log is not admissible evidence.**

---

## Design

### Fix ① — deny Codex's own thread kinds by name

From `enum ThreadSource` in `codex-rs/protocol/src/protocol.rs`:

```rust
pub enum ThreadSource {
    User, Subagent, GuardianReview, Feature(String), MemoryConsolidation,
}
```

`Feature(String)` is an open variant — which is why the generated TypeScript is
`export type ThreadSource = string;`. A product surface shipped tomorrow arrives
as a `Feature`, and **a Feature thread is the person's**.

```python
BOT_THREAD_SOURCES = ("subagent", "guardian_review", "memory_consolidation")
KNOWN_OWN_THREAD_SOURCES = (None, "", "user")
```

- **Deny by name; capture everything else.** An allowlist against an open type
  drops real work the first time Codex names a surface, and because the skip
  advances the capture watermark a session that has stopped growing never
  re-flushes. Unrecoverable on one side, one stray bot thread on the other.
- **Absent ⇒ own**, so rollouts predating the field keep being captured.
- **An unfamiliar value is captured and noted** — a log line, deliberately not a
  `last_error`, since a health banner reading "capture failed" over a session
  that captured fine is its own bug.

Gated: `codex_flush._flush` (before `to_canonical`), `capture.py`'s candidate
scan, and `find_sessions.py`. **Not** gated: `readers_cli.py` and `capture.py
list` — a documented read-only stream with a wire contract and golden files.

### Fix ② — namespace the session id at the wire boundary, on both lanes

1. `log_fires` and the event-row builder each record a **local-only** `host`
   field (precedent: `rulebook_id`). Neither `WIRE_KEYS` nor `EVENT_WIRE_KEYS`
   carries it.
2. `wire_row` **and** `event_wire_row` render `session_id` through
   `pr_link.conversation_id_for` — the helper that already owns this projection
   for the PR-link lane. Not reimplemented: a second copy is how two lanes come
   to disagree, and the existing one already handles whitespace, a host spelled
   `Codex`, and an already-prefixed id.
3. The host arrives via `--host claude|codex|cursor`, matching the existing
   convention. Default `claude`, so existing behaviour is byte-identical.

Local state, ordering, dedup and obligations keep the **raw** id. Only the bytes
crossing the wire change.

### Fix ③ — say that the bridge is required

`.codex-plugin/plugin.json`, `skills/setup/SKILL.md`, `codex/README.md` and
`README.md` now state that `plugin_hooks` is `removed`, that the manifest's
`hooks` key is never dispatched, and that skipping `memhub:setup` or the
`/hooks` trust step leaves capture silently off. Descriptions are documentation,
not metadata.

Two smaller defects observed during the same live run:

- **`remove` was not the inverse of `install`.** On a machine with no
  `hooks.json` it left a `{"hooks": {}}` stub. It now removes the file when
  nothing but our own entries were ever in it — and keeps it when it holds
  someone else's hooks.
- **`status` said `NOT INSTALLED (4/4 handlers)`** after a plugin upgrade, while
  capture was demonstrably working and only the copied runner was stale. It now
  distinguishes `STALE BRIDGE — re-run setup` from a missing install.

---

## Investigated and not fixed

**ENG-1070 #1 — "capture is dead and nothing says so." NOW FIXED — the
precondition was observed.**

An earlier fix was written and reverted on the grounds that its precondition was
never demonstrated. **That reasoning was wrong, and an observational study
settled it.** A blind Codex session — an ordinary "fix the failing test" task,
with no mention of MemHub — was run against a real install:

```
$ codex plugin add memhub-staging@memhub-internal     # reports success, prints 0.59.1
$ ls -a <cache>/memhub-internal/memhub-staging/0.59.1/
.claude-plugin   .mcp.json   mcp.json
```

**Three files.** All six symlinked entries (`scripts`, `hooks`, `skills`,
`references`, `LICENSE`, `NOTICE`) were not copied — not dangling, absent.
**Codex's installer copies regular files and skips symlinks.** A control
install of the prod `memhub` plugin (real files, no symlinks) from the same
marketplace landed complete, 49 entries under `scripts/`.

The A/B, same blind prompt, only the install differing:

| | broken install | repaired install |
|---|---|---|
| rulebook ledger rows | **0** | 6 |
| `codexflush/<sid>` state | **none** | written |
| anything naming MemHub | **nothing** | health sidecar |

The hooks were firing the whole time; they were landing in silence.

**Scope, honestly:** the install that breaks is `memhub-staging`, the internal
build, which is not in the Codex public catalog — so the people at risk today
are XTrace developers running the internal build on Codex. That is exactly who
filed ENG-1070, against staging 0.54.0 and 0.55.1.

This also falsifies `RELEASING.md`, which said a path source "dereferences the
symlinks". True on Claude Code; false on Codex. Both that file and
`CONTRIBUTING.md` are corrected here.

**What was restored, and what was not.** The breadcrumb, the SessionStart
notice and the `capture_health` reason are back — they address the observed
defect, which is *silence*. Still **not** restored, because nothing observed
justifies them:

- The failure mode was reproduced: a version directory holding only
  `codex_flush.py` is selected over a complete one, and the flush child dies on
  `ModuleNotFoundError` with its output discarded.
- **The precondition was never demonstrated.** Nothing shows Codex produces a
  half-populated version directory; a live install found `codex plugin add`
  lands a full copy. The broken folder was built by hand.
- **The live upgrade test contradicted the premise.** Capture *survived*
  0.58.3 → 0.58.4 with a deliberately stale bridge, and the ENG-1070 #2 fix took
  effect through it, because the bridge resolves the plugin root per event.
- **The fix had introduced a worse bug than it cured**, caught in review:
  ranking pooled the production and staging marketplaces into one tier, so a
  production user with a newer staging install would have resolved to the
  staging root — and `_memhub_auth` derives the backend and token cache from
  that root, sending captures to the wrong environment.

Also investigated, also not fixed:

- **Hook trust** stops capture on 100% of fresh installs and is **already known
  to the team** — PR #233 flags the re-trust cost explicitly; PR #80 separated
  installation reporting from trust reporting. Documented here, not re-solved.
- **Editing a hook manifest silently un-trusts every user.** `hook_hash`
  (`discovery.rs:775-791`) covers `command`, `matcher`, `timeout` and
  `statusMessage`, but *not* the referenced script's bytes. `fe5e745` rewrote
  every command. This is why **no manifest is touched here**, and it deserves a
  `RELEASING.md` warning of its own.
- **`readers/codex.py` ignores `$CODEX_HOME`.** The code states this is a
  containment boundary for payload-supplied paths and that widening it needs its
  own review.
- **`mcp.json` vs `.mcp.json`.** Codex reads the undotted file, Claude Code the
  dotted one. Two files, two endpoints — a live test pointed only `.mcp.json` at
  staging and Codex's MCP client then handshook against production (401, no data
  written). Worth a guard; not a product defect.
- **A Claude-lane 413 stall.** `flush_turn` caps individual records but not the
  aggregate delta, so a long session 413s forever and its cursor never advances.
  Six sessions on one machine, three of which never captured a turn. Written up
  in the `urgent plugin fix (claude code)` brain.

**Refuted, recorded so nobody re-derives them:**

- *"Codex does not export `PLUGIN_ROOT`."* **False** —
  `discovery.rs:262-270`, confirmed with a probe plugin.
- *"Changing `codex_hook_bridge.py` un-trusts the hooks."* **False** — the hash
  covers the config, never the script's bytes.
- *"A subagent rollout's parent-uuid `payload.session_id` mis-identifies it."*
  **False** — `_sid_of()` derives identity from the resolved rollout filename,
  "never from the payload field that happened to locate it."

---

## Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Client-side namespacing here; backend backfill separately | Keeps this inside `plugins/memhub/` |
| D2 | Deny the named bot kinds; capture everything else | `Feature(String)` is open, so the non-bot side is the open one |
| D3 | Namespace at the wire, not in `ctx` | Preserves local state keys; no in-flight session breakage |
| D4 | Delegate to `pr_link.conversation_id_for` | The helper already existed; a parallel mechanism is the default wrong answer |
| D5 | Bump **all five** manifests to 0.59.1 | #229 decoupled staging, but #240 — the most recent release — bumped it in lockstep again. Follow what main does, not what an older PR decided |
| D6 | Fail open everywhere | A memory hook must never block the host |
| D7 | Bot literals read from upstream source, not inferred | `guardian_review`, not `guardian` — an invented literal matched nothing |
| D8 | Discovery gates candidate *selection*, not read-only *enumeration* | `readers_cli` has a published wire contract and golden files |
| D9 | **Ship nothing whose precondition is unproven** | The reverted resolver fix: reproduced failure mode, unproven cause, and it introduced a wrong-environment routing bug |
| D11 | Keep the ENG-1070 #1 fix even though the staging migration removes its trigger | All six symlinks live in `memhub-staging` and go with it when staging moves to its own repo; `memhub` has none. But the fix addresses the SILENCE, not the symlink — `resolve_plugin_root() -> None -> exit 0` swallows any cause, and removing today's cause does not make the next one visible. ~110 self-contained lines that fire on any unresolvable root. **Condition on the migration:** it only holds if the new repo ships staging with real files; a repo that vendors shared code by symlink reproduces this exactly. |
| D10 | Touch no hook manifest | Editing one un-trusts every existing Codex user |

---

## Verification

- `python3 tests/run_all.py` and `uv run --with 'mcp<2' python tests/run_all.py`
  both green, exit 0.
- `tests/codex_correctness_test.py` — stdlib-only, tmpdir-isolated, never
  reaches a backend.
- Every changed hook smoked with realistic event JSON: exits 0, silent on the
  unhappy path, inside its `hooks.json` timeout.
- Manual runs go to staging with `MEMHUB_MCP_BASE_URL` set **explicitly** — and
  note that Codex reads `mcp.json`, not `.mcp.json`.
- **Never** exercise `api.memhub.xtrace.ai`.

## Open questions

1. **What breaks Codex capture on upgrade?** The field report is credible and
   ENG-1070 saw it recur at 0.55.1, but the live upgrade test did **not**
   reproduce it. Hook trust remains the strongest untested candidate.
2. **ENG-1075 end to end.** Needs a trusted hook dispatch on a real Codex
   install — one `/hooks` approval away, and untested until then.
3. **Backend backfill** for orphaned fires and stored bot-thread conversations.
4. **PR #217** should land first; until it does the Codex capture log cannot be
   used as evidence.
