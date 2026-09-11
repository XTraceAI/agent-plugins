---
title: "Spec: Codex / Cursor reliability matrix"
type: spec
---

# Codex / Cursor reliability matrix

**Repo:** `XTraceAI/agent-plugins`. **Tracking:** ENG-1034. Family E and the test sandbox are 1034a; families A–D, F, G and the manual checklist are 1034b. **Grounded at:** `origin/main` @ `2c50f8c`.

**Why this exists.** Capture bugs in this plugin are invisible from the inside: they live in harness ordering, host payload shapes and server replies that nobody is watching. So this document is not a list of fixes. It is a list of **things a user can do, or a server can answer**, each paired with **something you can look at** that says whether capture did the right thing.

The owner's framing: *"come up with as many variabilities in how people may use the plugin (actions they could take, ordering of events, etc) and attempt to tie them to verifiable outcomes."*

## 1. Observation points

Every row's *Where to look* names one of these and nothing vaguer. All paths are as of `main`.

| Id | Observation point | Written by |
|---|---|---|
| **OP-CX-LOG** | `~/.config/memhub-plugin/codexflush/log`: one timestamped `[codex-flush]` line per decision | `codex_flush._log` |
| **OP-CX-STATE** | `~/.config/memhub-plugin/codexflush/<sid>.json` | `codex_flush._save_state` |
| **OP-CU-LOG** | `~/.config/memhub-plugin/cursorflush/log` (`[cursor-flush]`) | `cursor_flush._log`, the Cursor launcher |
| **OP-CU-STATE** | `~/.config/memhub-plugin/cursorflush/<uuid>.json` | `cursor_flush._save_state` |
| **OP-CL-STATE** | `~/.config/memhub-plugin/turnflush/<sid>.json` and `<sid>.sessionflush.json`. These are **the only files `capture_health` reads**. | `flush_turn`, `flush_session._breadcrumb` |
| **OP-ROW** | The conversation row in MemHub: count, name, room, message count | server |
| **OP-CX-HOOKS** | `~/.codex/hooks.json`, `~/.codex/memhub_hook_bridge.py` | `setup_codex_hooks.py` |
| **OP-EXIT** | A non-zero exit code with a documented meaning (`link-pr`: `4` ambiguous, `3` not found) | the script |

> **On the capture stack (#208 → #214)** state and logs move under `capture_context.state_directory(<base>)`, one per destination. Once it lands, every row must name the **destination** as well as the file.

> **These files are trustworthy only because the test suites no longer write into them.** Until ENG-1034a, `codex_capture_test.py` and `cursor_capture_test.py` appended synthetic lines to the real logs on every local run. See §5.

## 2. Conventions

- **A row names an ordering, not just an action.** "Five failed imports, then the window elapses, then a confirmed re-probe" is a row. "Server fails" is not.
- **"Nothing happens" is not an allowed observable.** A silent exit cannot be told apart from a successful capture, so every row ends in a line, a field, or a row.
- **State-field glossary:**

| Field / constant | Meaning |
|---|---|
| `rollout_size` (Codex) | Byte watermark: the rollout size last **confirmed** stored |
| `transcript_revision` (Cursor, transcript source) / `blob_ids` (Cursor, store source) | Cursor watermark |
| `fail_streak` | Consecutive **server-contacted** failures since the last success |
| `last_error`, `last_error_at` | Why the last attempt failed, and when (`last_error_at` is Codex only) |
| `last_ok_at` | Last confirmed import |
| `last_flush_at` (Cursor) | Last attempt; also the edit debounce clock |
| `unsupported`, `unsupported_at` | Dormant, and since when |
| `MAX_UNCONFIRMED = 5` | Streak length that makes a session dormant |
| `DORMANT_RETRY_S = 1800` | A dormant session is re-probed once per 30 min |
| `ERROR_COOLDOWN_S = 60` (Codex) | Non-Stop events skip for 60 s after a failure; Stop is exempt, because Codex has no SessionEnd |
| `DEBOUNCE_S = 120` (Cursor) | `afterFileEdit` flushes at most once per 2 min |

## 3. Family E — server interaction

**Question per row:** does the watermark **advance**, **hold**, or go **dormant**, and what says so?

*Held* means the watermark stays at the last confirmed value while newer content waits for the next attempt.

Rows that list "Codex, Cursor" were checked on both. Tests live in `tests/capture_server_replies_test.py`, drive the real state file and log, and stub only the credential, the MCP session and the rollout parse. Pure reply classification (`_verdict`) is also pinned in `codex_capture_test.py` / `cursor_capture_test.py::test_import_verdicts_and_dormancy`.

| # | Variation (ordering) | Host(s) | Expected observable | Where to look | Automated? |
|---|---|---|---|---|---|
| E1 | New content, then `{"conversation_id": <ours>, "ack_through": "u1"}` | Codex, Cursor | **Advance.** `fail_streak: 0`, `last_error: null`, `last_ok_at` set, `unsupported: false`; log `flushed N records → codex-<sid> (personal)` / `cursor-<uuid>` | OP-CX-STATE/LOG, OP-CU-STATE/LOG | `test_e01_confirmed_ack_advances_the_watermark` |
| E2 | Confirmed flush, new content, then `ack_through: null` | Codex, Cursor | **Hold.** `fail_streak: 1`, `last_error: unconfirmed_import`; log `import NOT confirmed (ack_through null) — holding the watermark…`. Cursor also stamps `last_flush_at`. | same | `test_e02_null_ack_holds_the_watermark` |
| E3 | …then an ack **with** `records_dropped: 6` | Codex, Cursor | **Hold** (a drop outranks the ack); streak 1; log `server dropped 6 record(s) — holding the watermark` | same | `test_e03_dropped_records_hold_even_beside_an_ack` |
| E4 | …then `isError` carrying text | Codex, Cursor | **Hold**; streak 1; log `server rejected the import: ['<the text>']` | same | `test_e04_server_error_holds_and_names_its_text` |
| E4b | …then `isError` with **no** content blocks | Codex, Cursor | **Hold**; streak 1; log `server rejected the import: []`. An empty list means the reply carried no text. It was the signature of the test pollution (§5), not of a real server. | same | `test_e04b_textless_server_error_logs_an_empty_list` |
| E5 | …then a text block that is not JSON | Codex, Cursor | **Hold**; streak 1; log `import response unrecognized — holding the watermark` | same | `test_e05_unparseable_reply_holds` |
| E6 | …then a confirming ack for **another** conversation id | Codex, Cursor | **Hold**; streak 1; log `import response unrecognized` (the expected-id filter drops it) | same | `test_e06_ack_for_another_conversation_holds` |
| E7 | …then an ack that **omits** the `ack_through` key | Codex, Cursor | **Dormant at once.** `unsupported: true`, `unsupported_at` = now, streak 0, watermark held; log `server does not report ack_through — per-event flush is dormant…`; the next boundary event (Codex `Stop`, Cursor `stop`) is refused inside the window | same | `test_e07_missing_ack_field_goes_dormant_at_once` |
| E8 | …then HTTP 429 (`McpRateLimited`) | Codex, Cursor | **Hold**; streak 1; `last_error: rate_limited`; log `rate limited: …` | same | `test_e08_rate_limit_counts_toward_dormancy` |
| E9 | …then a transport/HTTP error, e.g. 401 (`McpError`) | Codex, Cursor | **Hold**; streak 1; `last_error: mcp_error: <message>`; log `import failed: <message>`. 401, 403 and 5xx are indistinguishable (FE-3). | same | `test_e09_transport_error_counts_toward_dormancy` |
| E10 | Streak at 3, then no usable credential | Codex, Cursor | **No server call**; watermark held; `last_error: no_credential`; **streak reset to 0** (a local gap never tips a session into dormancy); log `no usable credential — skipping (run /memhub:login)` | same | `test_e10_missing_credential_is_neutral` |
| E11 | Five consecutive `ack_through: null` replies | Codex, Cursor | After #4: streak 4, not dormant. After #5: `unsupported: true`, streak 0; log `5 consecutive failed imports (unconfirmed_import) — per-event flush is dormant for this session; run /memhub:import-session… Re-probes in 30 min.`; boundary events refused | same | `test_e11_five_failures_go_dormant` |
| E12 | Dormant, window elapses, re-probe answers `null` | Codex, Cursor | One server call; **stays dormant**; `unsupported_at` reset to now; streak 0 (no fresh 5-attempt budget); refused again | same | `test_e12_failed_reprobe_stays_dormant_without_a_fresh_budget` |
| E13 | Dormant, window elapses, re-probe confirmed | Codex, Cursor | **Advance and re-arm.** `unsupported: false`, `unsupported_at: 0`, streak 0; gate open | same | `test_e13_confirmed_reprobe_rearms_the_session` |
| E14 | Codex: a failed flush, then within 60 s a `git commit` PostToolUse, then a `Stop` | Codex | PostToolUse makes **no server call**; log `PostToolUse: unconfirmed_import 0s ago — cooling down (60s)`. The Stop **does** call the server. | OP-CX-LOG | `test_e14_codex_cooldown_skips_milestones_but_not_stop`, `test_e14b_cooldown_is_logged_with_its_window` |
| E15 | Server call outlives the flush deadline (`FLUSH_TIMEOUT_S`, 240 s) | Codex | **Hold**; streak 1; `last_error: flush_error: TimeoutError`; log `Stop: flush error: …` | OP-CX-STATE/LOG | `test_e15_codex_timeout_counts_toward_dormancy` |
| E15 | same | Cursor | Expected: identical, via `cursor_flush.main`'s own `wait_for` and broad handler | OP-CU-STATE/LOG | **No.** `main()` owns the timeout (FE-5) |
| E16 | Server reachable, then the connection drops mid-call | Codex | **Hold**; streak 1; `last_error: flush_error: ConnectionResetError`; log `Stop: flush error: <message>` | OP-CX-STATE/LOG | `test_e16_connection_drop_mid_call` |
| E16 | same | Cursor | `_flush` lets the error out with the watermark held (automated); `main()` converts it to `flush_error: <Type>` plus a streak step (not automated, FE-5) | OP-CU-STATE/LOG | Partial: `test_e16_connection_drop_mid_call` |

**Answer to ENG-1034's question** *"does the watermark advance, hold, or reset — today several hold forever"*: **none holds forever.**
- Every unconfirmed shape holds and counts toward a 5-attempt dormancy.
- A missing `ack_through` key goes dormant at once.
- Dormancy re-probes every 30 minutes, and a confirmed re-probe re-arms the session.
- The retry is bounded; `/memhub:import-session` is the documented recovery for content that fell inside a dormant window.

### Family E findings for triage

Recorded here, not fixed: ENG-1034 is an observer of capture code.

- **FE-1 — The "live" `ack_through: null` failure was test pollution.** The `codexflush/log` lines quoted in ENG-1034 came from test runs, and so did `server dropped 6 record(s)`, `server rejected the import: []` and `import response unrecognized`. On the machine they were collected from, **0 of 756** such lines fell outside a test burst. There is no production evidence of `ack_through: null` here, so no backend investigation is scheduled for it.
- **FE-2 — Codex/Cursor failures never reach SessionStart.** `capture_health.py` scans only `turnflush/`. A Codex or Cursor session that is failing, or dormant, produces no health warning; `codexflush/log` / `cursorflush/log` are the only trace.
- **FE-3 — Auth failures are not named on Codex/Cursor.** Both flushers fold 401, 403, 5xx and transport errors into `mcp_error: <message>`. `flush_session` distinguishes `auth` and `forbidden`, which is what lets a health message name the fix.
- **FE-4 — A real Claude SessionEnd rejection, cause unknown.** Session `33f509ce-…` (2026-09-09 14:59) breadcrumbed `server_rejected` with *"messages failed validation: Input should be 'user', 'ai' or 'assistant'"*. That is the source of the owner's *"capture last failed 23h ago"*. The transcript now holds only `custom-title` and `agent-name` records. Claude path; triage separately from this matrix.
- **FE-5 — Cursor `main()`'s timeout and transport-raise handling is not automated.** E15/E16 on Cursor need a driver that goes through `main()`: a transcript on disk, the session lock and the event gate.

## 4. Families A–D, F, G

*1034b — not yet written.* The variation lists are in ENG-1034 §Deliverable 2:
- **A** — install / upgrade / trust
- **B** — auth
- **C** — session identity and lifecycle, which overlaps ENG-1039
- **D** — transcript / rollout content
- **F** — Cursor specifically
- **G** — PR linking from the plugin

The manual checklist also belongs to 1034b.

## 5. Test sandbox: Deliverable 3, as built

**Found.** Every suite on `2c50f8c` was run under a throwaway `$HOME` and checked for files left behind. Exactly **2 of 50** wrote into it:
- `codex_capture_test.py` → `.config/memhub-plugin/codexflush/log`
- `cursor_capture_test.py` → `.config/memhub-plugin/cursorflush/log`

Both flushers fix `STATE_DIR` from `Path.home()` at import, and neither suite redirected `HOME`. CI was already isolated by `scripts/check-plugin.sh`; a local `python3 tests/run_all.py`, or a suite run directly, was not.

**Fixed, in two layers:**
1. **Each suite that imports capture code redirects `HOME` and `USERPROFILE` before the import** (the `flush_session_test.py` pattern). The two leaking suites now do, as does `capture_server_replies_test.py`. This makes a direct `python3 tests/<suite>.py` safe.
2. **`tests/run_all.py` runs every suite under its own throwaway `HOME`** (plus `USERPROFILE`, `XDG_CONFIG_HOME`), and **fails a suite that leaves any file in it**, naming the files. This keeps the runner hermetic and turns the next leaking suite into a red build instead of a polluted log. The check is pinned by `tests/run_all_sandbox_test.py`.

**The rule for new suites.** If the code under test resolves anything from `Path.home()`, redirect `HOME`/`USERPROFILE` to a temp dir before importing it. The runner will fail the suite otherwise.
