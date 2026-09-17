# Plugin Spec: the per-turn flush bounds its payload (ENG-1085)

**Repo:** `XTraceAI/agent-plugins`. Client-only — **no MemHub server change is required**.
**Companion:** the investigation artifact `Urgent: Claude Code per-turn capture dies at 413`
in the *urgent plugin fix (claude code)* brain, which holds the live evidence this was
diagnosed from. This file is the standing description of the mechanism.

**Status:** implemented in v0.59.2. Covered by `tests/turn_flush_bound_test.py`.

---

## 0. The defect

`flush_turn.py` capped individual records and never the **aggregate delta**.

- `transcript_filter.elide_oversized_tool_results` bounds one record —
  `MAX_TOOL_RESULT_BYTES = 200_000`, and `HARD_MAX_RECORD_BYTES = 3_400_000` for a record
  that cannot otherwise ship. That per-record ceiling works and is unchanged here.
- `flush_session.py` bounds the batch, with `transcript_chunks.slices(records)` —
  `DEFAULT_CHUNK_BYTES = 3_500_000`, under the server's 4 MiB request limit.
- `flush_turn.py` had no equivalent call. It sent the whole delta.

So once a session's pending bytes crossed the request limit, every per-turn flush returned
**413**; the cursor rightly never advances past what was not sent; and the same
ever-growing delta was re-sent on every later turn. Per-turn capture was dead for the rest
of that session's life — and it failed on precisely the long, tool-heavy sessions worth
keeping, because a short session never reaches the limit.

Measured on one machine, 2026-09-16: six of seven failing sessions were this same 413, and
three of them had **never captured a single turn**. One had 7.58 MB pending behind a cursor
frozen at 435172, re-sent every turn for hours. `flush_session.py` ingested the *same* 8 MB
transcript without complaint — `slice 1/2` + `slice 2/2`, 654 messages stored. Same bytes.
The slicing path succeeded; the per-turn path could not.

## 1. The mechanism

`_read_tail` reports a byte end offset **per record** alongside the records and the total
consumed span. `_bounded` runs the delta through `transcript_chunks.slices` — the same
splitter the whole-transcript paths use, so there is exactly one definition of "a payload
one call can carry" — and returns the **first** slice with the byte offset of its last
record. The cursor lands there.

When the delta fits one payload, the offset is the **full consumed span**, not the last
record's end, so records the filters dropped are consumed too rather than re-read on every
later turn. That keeps the common case byte-identical to the old behaviour.

**Every batch must carry a message-bearing record.** The server reads a batch with no
`message` among its records as plain chat and fails role validation — the contract
`_INERT_RECORD_TYPES` is written around, and the reason `attachment` sits *outside* that
set ("the next turn re-sends it alongside the message records that make the batch valid").
That reasoning assumed the whole delta always ships. Bounding broke it: a first slice of
nothing but attachments or sidecars is refused as `server_rejected` — **not** a 413 — so it
neither advances the cursor nor reaches the shrink ladder, and the next turn slices the
identical delta identically. A permanent stall through a different door than the one this
change closes. `_with_a_message` widens the batch until it reaches the first message-bearing
record, overshooting the byte cap if it must: a payload the server *might* refuse beats one
it *must* refuse, and it is no larger than the single request the unbounded code sent every
turn anyway. A delta with no message-bearing record at all is left alone — there is nothing
to widen to, and being refused while keeping the cursor is already the documented answer.

**One slice per turn, not all of them.** This hook is one call per turn, on a 60 s budget,
holding the session's flock while `turn_flush_prefilter.py` skips behind it. A backlog
still drains, because steady-state per-turn deltas are kilobytes: 3.5 MB a turn catches up
far faster than a session produces. `flush_session.py` remains the backstop for whatever a
session ends before reaching.

**The filters run index-preserving.** `flush_turn` drops slash-command wrappers through
`is_command_wrapper` — the predicate `drop_command_wrappers` is built from, and the one
`session_title.py` already uses — rather than calling the dropper and losing which record
came from where. `elide_oversized_tool_results` and `redact_records` are both 1:1,
including on their never-raise fallbacks, so the record ↔ offset correspondence survives
the whole pipeline. If it did not, the cursor could advance past a record that was never
sent, which is the one failure the module exists to prevent.

## 2. When a bounded payload is still refused

A 413 arrives as `mcp_http.McpError` with `status == 413`, and is classified under its own
`payload_too_large` reason instead of falling into the generic `error` arm. The same
refusal delivered **without** a status — by a proxy, inside a 200 JSON-RPC envelope, or as
an `isError` tool result — is recognised by phrase (`_is_size_refusal`) and treated
identically; read as a generic fault it would never bring the cap down, and the session
would stall exactly as it did before. Deliberately phrases only, never a bare `413`: a
status code is a plausible substring of an unrelated message, and a false positive puts the
session on the shrink ladder for no reason.

`_shrink_slice` decides what to do, because the call site is the only place that knows how
many records went. The ladder, in order:

| state | action |
|---|---|
| >1 record, cap above the floor | halve the cap, **remember it**, cursor unmoved |
| >1 record, cap at the floor (`_MIN_SLICE_BYTES = 256_000`) | **one record per turn** from here on, cursor unmoved |
| exactly 1 record, **larger** than the floor | **step over it**, breadcrumb the loss |
| exactly 1 record, **at or below** the floor | keep it, cursor unmoved — the server is refusing, not the record |
| already one record per turn, still refused | **dormant** — no legal batch exists; stop burning a round trip per turn |

Each rung exists because the one above it has run out, and the last two are what keep this
from being the original bug at a lower threshold:

- **Halving is sticky**, never restored on success, because a request limit is a property
  of the server: re-probing the full cap after every success buys one guaranteed-wasted
  round trip per turn and learns nothing.
- **The floor is not the end of the ladder.** `max(floor, current // 2) == current` once
  the cap is at the floor, so stopping there wrote *nothing* — no cap change, no cursor
  move — and `slices` regrouped the identical delta identically next turn. That is the
  original defect verbatim, relocated from the server's 4 MiB limit down to our own
  256 KB floor. One record per turn is the payload below a floor-sized slice.
- **A single-record batch skips the ladder entirely.** `transcript_chunks.slices` never
  splits inside a record, so `slices([R], n) == [[R]]` for every `n` — lowering the cap
  cannot change what the next turn sends. Walking the ladder anyway cost four full-size
  uploads, each refused, with the whole delta frozen behind them.
- **Stepping over a record is gated on the RECORD's own size**, not on the cap having
  bottomed out. Inferring "unsendable" from the cap alone deleted a **67-byte** record on
  one spurious 413 — and because the cap is sticky, a session that had ever been driven to
  the floor stayed in delete-on-413 mode for the rest of its life, silently discarding
  ordinary turns. That is precisely the permanent invisible loss the cursor rule exists to
  prevent. A record above the floor is one no payload we can build will carry (in
  practice: a record `_elide_record` leaves untouched because it carries no `message`
  dict); below it, the server is refusing something minimal, which says nothing about the
  record. So drop the first, keep the second, and let the SessionEnd backstop have it.

- **The ladder terminates in dormancy, not an endless retry.** `one_record` shrinks the
  payload and `_with_a_message` widens it to stay legal, so an attachment prefix whose first
  message pushes it over the limit pulls both ways. They are not really in conflict: a batch
  must carry a message to be *parsed* and be under the limit to be *accepted*, and because
  the cursor is a single byte offset only a **prefix** can be sent — so if the shortest
  message-bearing prefix is over the limit, **no legal batch exists**. Re-sending it every
  turn is the stall this change removes. Setting `unsupported` stops the prefilter spawning
  doomed flushes, and the `payload_too_large` breadcrumb stands so the banner still explains
  it.

  Dormancy is the **least-bad** answer here, not a rescue. `flush_session` usually does
  recover the session — it re-sends the whole transcript against its own cursor — but it is
  **not a guarantee in this state**: it slices at a fixed `DEFAULT_CHUNK_BYTES`, has no 413
  handling of its own, and stops on the first rejected slice, so a prefix unsendable here
  can be unsendable there too. Nothing this hook can do changes that. Stepping over the
  prefix *would* rescue the rest of the session, at the cost of deleting an `attachment` —
  real user content this module deliberately keeps outside `_INERT_RECORD_TYPES` so it is
  never dropped. Widening the single-record step-over that far is a policy call and is
  deliberately not made here; `flush_session`'s missing adaptive handling is tracked
  separately.

When a record is stepped over, the cursor advance and the breadcrumb go out in **one**
atomic publish, so a crash between them cannot leave a cursor that skipped a record nobody
knows about. One record lost against every turn that follows it — the same asymmetry
`HARD_MAX_RECORD_BYTES` already resolves this way.

`slice_bytes` (an integer) and `one_record` (a flag) are the only state this adds, both
optional. Older builds ignore them; a state file written by an older build is read by the
new one with the defaults. `slice_bytes` is clamped on read, so a hand-edited or corrupt
value can neither widen the cap past the default nor narrow it below the floor.

## 3. The health message

The failure was always breadcrumbed — under the generic `error` slug, which
`capture_health.py` renders as *"the capture hook hit an unexpected error … run
`/memhub:login --status` to check."* That points at the credential, which was fine. That
exact banner sat at the top of the session that found this bug and was read past for hours.

`payload_too_large` now has its own reason and its own advice, on the same reasoning as
`budget_exhausted`: not a credential question, and self-correcting, so it must not read as
an action item.

> MemHub capture last failed 12m ago — one turn was too large for the server to accept in
> one piece. Capture splits it over the next few turns on its own, and the session-end
> backstop covers the rest.

## 4. Recovery of already-stalled sessions

None needed, and none is shipped. A stalled session self-heals: the first turn it takes on
a fixed plugin slices and drains. A session that never takes another turn was already
captured by the `SessionEnd` backstop, which slices.

## 5. Out of scope

### The Codex and Cursor per-turn lanes still have the unbounded aggregate

`codex_flush.py` and `cursor_flush.py` build `"messages": sendable` with no
`transcript_chunks.slices` call — `transcript_chunks` is imported only by `flush_session`,
`import_session` and now `flush_turn` — and neither classifies a 413. They are less
exposed than the Claude lane was, because both carry a consecutive-failure dormancy
counter, so they degrade rather than re-send an oversized delta forever. But the defect
itself is only fixed for Claude. Deliberate: this change is scoped to the lane the
investigation reproduced. **Worth its own ticket.**

### Two Claude-lane gaps found alongside this one

Neither affects long-session capture, and neither is addressed here.

- **`claude_hook_guard` suppresses capture on an env var alone.** Any non-empty `CURSOR_*`
  marker makes `is_cursor()` return True regardless of the payload — and the SessionStart
  health hook sits behind the same guard, so the one channel that would report it is
  suppressed too.
- **`flush="auto"` can leave a named, empty session.** Auto-buffered records get
  dedup-registered without persisting, so a session that never reaches the drain threshold
  can end with a title and `msgs=0` while health reports "all fine". This bites **short**
  sessions only; a long session always drains. The real fix is backend-side — the server
  folding dedup registration into the drain — which is why `cursor_flush._FLUSH_MODE`'s
  forced `"now"` is labelled TEMPORARY in that file, and why forcing `"now"` here would be
  the wrong trade: it pays per-turn LLM extraction and shreds the episode boundaries the
  batching exists to protect.
