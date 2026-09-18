"""Self-test for the per-turn flush's AGGREGATE size bound (ENG-1085).

The defect these lock down was silent and permanent. ``flush_turn`` capped
individual records but never the batch, so once a session's pending delta
crossed the server's request limit every flush returned 413, the cursor could
not advance past bytes that were never sent, and the same ever-growing delta
was re-sent on every later turn — forever. Six sessions on one machine were in
that state at once, three of which had never captured a single turn, and the
banner that reported it blamed the credential.

Nothing here reaches a network: the MCP session is a stub and every path runs
against a tmpdir. Run: python3 turn_flush_bound_test.py   (stdlib only)
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path

# The tests live outside the plugin so they are not shipped to users;
# the code under test is still in the plugin's scripts dir.
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import flush_turn as ft  # noqa: E402
import mcp_http  # noqa: E402
import transcript_chunks  # noqa: E402

_failures = []


def check(label, got, want):
    if got != want:
        _failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


def _rec(uid, text="hi"):
    return {"type": "user", "uuid": uid, "cwd": "/repo",
            "message": {"role": "user", "content": text}}


def _fat(uid, nbytes):
    """A record whose serialized size is about ``nbytes`` and that the elider
    leaves alone — plain user prose under HARD_MAX_RECORD_BYTES."""
    return _rec(uid, "x" * nbytes)


def _write(path, records):
    with open(path, "wb") as fh:
        for r in records:
            fh.write((json.dumps(r) + "\n").encode())
    return os.path.getsize(path)


# ── the bound itself ──────────────────────────────────────────────────

def test_a_delta_that_fits_is_sent_whole():
    """The common case must be untouched: one slice, and the cursor lands on
    the FULL consumed span — not the last record's end — so records the filters
    dropped are consumed too instead of being re-read every turn."""
    print("_bounded — a delta that fits")
    sendable = [_rec("a"), _rec("b")]
    ends = [100, 200]
    batch, consumed = _bounded_at(sendable, ends, 350, 3_500_000)
    check("everything goes", [r["uuid"] for r in batch], ["a", "b"])
    check("cursor takes the whole read span, not ends[-1]", consumed, 350)


def _bounded_at(sendable, ends, consumed, chunk_bytes):
    return ft._bounded(sendable, ends, consumed, chunk_bytes)


def test_an_oversized_delta_is_cut_to_one_slice():
    """The fix. Three records that cannot share a payload go one per turn, and
    the cursor stops on the one that was actually sent."""
    print("_bounded — an oversized delta")
    sendable = [_fat("a", 400), _fat("b", 400), _fat("c", 400)]
    ends = [1_000, 2_000, 3_000]
    batch, consumed = _bounded_at(sendable, ends, 3_000, 500)
    check("only the first slice goes", [r["uuid"] for r in batch], ["a"])
    check("cursor stops at that record's end", consumed, 1_000)

    # The next turn resumes from there and takes the next one.
    batch, consumed = _bounded_at(sendable[1:], ends[1:], 3_000, 500)
    check("the next turn carries the next record",
          [r["uuid"] for r in batch], ["b"])
    check("and advances to its end", consumed, 2_000)


def test_a_bounded_batch_always_carries_a_message_bearing_record():
    """REGRESSION LOCK. The server reads a batch with no `message` among its
    records as plain chat and fails role validation — the contract
    `_INERT_RECORD_TYPES` is written around, and the reason `attachment` sits
    OUTSIDE that set ("the next turn re-sends it alongside the message records
    that make the batch valid").

    Bounding broke that reasoning: a first slice of nothing but attachments is
    refused as `server_rejected` — NOT a 413 — so it neither advances the
    cursor nor reaches the shrink ladder, and the next turn slices the
    identical delta identically. A permanent stall, reintroduced through a
    different door than the one this module closed."""
    print("_bounded — every batch carries a message")
    att = lambda i: {"type": "attachment", "uuid": f"att{i}",
                     "attachment": {"pasted": "x" * 200_000}}
    usr = _rec("u1", "here is the file")
    sendable = [att(0), att(1), usr]
    ends = [100, 200, 300]

    batch, consumed = ft._bounded(sendable, ends, 300, ft._MIN_SLICE_BYTES)
    check("the batch is widened to reach the message record",
          [r["uuid"] for r in batch], ["att0", "att1", "u1"])
    check("any batch sent carries a message",
          any(ft._carries_message(r) for r in batch), True)
    check("and the cursor covers exactly what was sent", consumed, 300)

    # The same rule must hold on the one-record rung, or that rung walks
    # straight back into the stall it exists to escape.
    batch, _ = ft._bounded(sendable, ends, ft._MIN_SLICE_BYTES, True)
    check("one_record still cannot emit a message-less batch",
          any(ft._carries_message(r) for r in batch), True)

    # A slice that already has one is left exactly as it was — the common case
    # must not be widened.
    plain = [_rec("a"), _rec("b"), _rec("c")]
    batch, consumed = ft._bounded(plain, [10, 20, 30], 30, 10_000_000)
    check("a healthy batch is untouched", [r["uuid"] for r in batch],
          ["a", "b", "c"])

    # Nothing to widen to: return as-is rather than inventing a record. The
    # pre-existing answer (be refused, keep the cursor, carry it next turn)
    # still applies.
    only_atts = [att(0), att(1)]
    batch, _ = ft._bounded(only_atts, [100, 200], 200, ft._MIN_SLICE_BYTES)
    check("an all-attachment delta is returned unchanged",
          [r["uuid"] for r in batch], ["att0"])


def test_an_inert_prefix_is_consumed_not_widened_into_the_batch():
    """REGRESSION LOCK. `_INERT_RECORD_TYPES` records are UI bookkeeping the
    server has no use for — the all-inert branch already consumes them without
    sending. But a leading run of them used to be WIDENED INTO the batch by
    `_with_a_message` reaching for the message behind them, and a
    `file-history-snapshot` runs to megabytes: measured, a 4 MB snapshot ahead
    of a 68-byte message produced a 4 MB payload where dropping the snapshot
    leaves a legal 68-byte one. A 413 on that sent the session dormant with a
    trivially sendable batch sitting right there — and unlike the attachment
    case this needs no unusual server, since one snapshot can clear the real
    4 MiB limit by itself."""
    print("_bounded — an inert prefix is consumed, not carried")
    snap = {"type": "file-history-snapshot", "uuid": "snap",
            "snapshot": {"files": {f"f{i}": "x" * 1000 for i in range(4000)}}}
    usr = _rec("u1", "hi")
    check("the snapshot really is inert", ft._is_inert(snap), True)
    check("and really is huge", ft._record_bytes(snap) > 4_000_000, True)

    batch, consumed = ft._bounded([snap, usr], [100, 200], 200,
                                  ft._MIN_SLICE_BYTES)
    check("the inert prefix is dropped", [r["uuid"] for r in batch], ["u1"])
    check("leaving a payload the server can actually take",
          ft._record_bytes(batch[0]) < 1_000, True)
    check("and the cursor still covers the dropped prefix", consumed, 200)

    # An attachment is NOT inert — it is real user content and must survive.
    att = {"type": "attachment", "uuid": "att", "attachment": {"pasted": "x" * 100}}
    check("an attachment is not inert", ft._is_inert(att), False)
    batch, _ = ft._bounded([att, usr], [100, 200], 200, ft._MIN_SLICE_BYTES)
    check("so an attachment prefix is still carried",
          [r["uuid"] for r in batch], ["att", "u1"])

    # Interleaved: only the LEADING run goes; nothing after the first real
    # record is touched, so this cannot quietly change the common case.
    mid = {"type": "ai-title", "uuid": "t", "aiTitle": "x"}
    batch, _ = ft._bounded([snap, usr, mid, _rec("u2")], [10, 20, 30, 40], 40,
                           10_000_000)
    check("only the leading run is dropped",
          [r["uuid"] for r in batch], ["u1", "t", "u2"])


def test_a_single_record_still_rides_alone():
    """``slices`` never splits inside a record, so a lone oversized record is
    still handed over as its own payload. That is what makes the 413 below a
    statement about the RECORD rather than about the batch."""
    print("_bounded — one record over the cap")
    sendable = [_fat("big", 4_000)]
    batch, consumed = _bounded_at(sendable, [9_000], 9_000, 500)
    check("it is not dropped", [r["uuid"] for r in batch], ["big"])
    check("and the span is the full read span", consumed, 9_000)


def test_the_cap_is_the_shared_constant():
    """One definition of 'a payload one call can carry', shared with
    flush_session and import_session — not a second per-turn number that has to
    be kept under the server's limit independently."""
    print("_slice_bytes — default and clamping")
    check("default is the shared chunk size", ft._slice_bytes({}),
          transcript_chunks.DEFAULT_CHUNK_BYTES)
    check("a remembered smaller cap is honoured",
          ft._slice_bytes({"slice_bytes": 400_000}), 400_000)
    check("it can never exceed the default",
          ft._slice_bytes({"slice_bytes": 99_000_000}),
          transcript_chunks.DEFAULT_CHUNK_BYTES)
    check("nor fall below the floor",
          ft._slice_bytes({"slice_bytes": 1}), ft._MIN_SLICE_BYTES)
    check("garbage falls back to the default",
          ft._slice_bytes({"slice_bytes": "wat"}),
          transcript_chunks.DEFAULT_CHUNK_BYTES)
    check("so does a missing value",
          ft._slice_bytes({"slice_bytes": None}),
          transcript_chunks.DEFAULT_CHUNK_BYTES)


# ── reacting to a 413 ─────────────────────────────────────────────────

def test_a_413_halves_the_cap_and_remembers_it():
    """Without this the bug simply moves to a server whose limit is lower than
    our cap: the next turn would re-slice the same delta identically, get the
    same 413, and stall exactly as before."""
    print("_shrink_slice — halving")
    original = ft.STATE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        ft.STATE_DIR = Path(tmp)
        try:
            sid = "shrink"
            ft._shrink_slice(sid, {}, [_rec("a"), _rec("b")], 500)
            state = ft._read_state(sid)
            check("the cap halves",
                  state.get("slice_bytes"),
                  transcript_chunks.DEFAULT_CHUNK_BYTES // 2)
            check("and the cursor does NOT move", state.get("offset"), None)

            # Sticky: the next turn reads the smaller cap back and halves again.
            ft._shrink_slice(sid, ft._read_state(sid),
                             [_rec("a"), _rec("b")], 500)
            check("it halves again from the remembered value",
                  ft._read_state(sid).get("slice_bytes"),
                  transcript_chunks.DEFAULT_CHUNK_BYTES // 4)
        finally:
            ft.STATE_DIR = original


def test_the_floor_falls_through_to_one_record_per_turn():
    """REGRESSION LOCK. The first cut of this fix stopped at the floor: with
    more than one record in the batch, `max(floor, current // 2) == current`,
    so `_shrink_slice` wrote nothing at all — no cap change, no cursor move —
    and `slices` regrouped the identical delta identically on the next turn.
    That is the original bug verbatim, relocated from the server's 4 MiB limit
    down to our own 256 KB floor. There has to be a rung below the floor, and
    it is one record per turn."""
    print("_shrink_slice — the floor falls through")
    original = ft.STATE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        ft.STATE_DIR = Path(tmp)
        try:
            sid = "floor"
            ft._shrink_slice(sid, {"slice_bytes": ft._MIN_SLICE_BYTES + 10},
                             [_rec("a"), _rec("b")], 500)
            check("halving stops at the floor",
                  ft._read_state(sid).get("slice_bytes"), ft._MIN_SLICE_BYTES)
            check("and that alone is not a cursor move",
                  ft._read_state(sid).get("offset"), None)

            ft._shrink_slice(sid, ft._read_state(sid),
                             [_rec("a"), _rec("b")], 500)
            state = ft._read_state(sid)
            check("at the floor it drops to one record per turn",
                  state.get("one_record"), True)
            check("still without moving the cursor", state.get("offset"), None)

            # And that flag actually changes what the next turn sends — the
            # floor cannot, because `slices` never splits inside a record.
            sendable = [_rec("a"), _rec("b"), _rec("c")]
            check("the floor alone still groups them",
                  len(transcript_chunks.slices(sendable, ft._MIN_SLICE_BYTES)[0]),
                  3)
            batch, consumed = ft._bounded(sendable, [10, 20, 30], 30,
                                          ft._MIN_SLICE_BYTES, True)
            check("one_record sends exactly one",
                  [r["uuid"] for r in batch], ["a"])
            check("and stops the cursor on it", consumed, 10)
        finally:
            ft.STATE_DIR = original


def test_the_ladder_ends_in_dormancy_not_an_endless_retry():
    """REGRESSION LOCK. `one_record` shrinks the payload; `_with_a_message`
    widens it to stay legal. When an attachment prefix plus the first message
    exceeds the limit those two pull against each other, and the ladder ran out
    of state to change — cursor pinned, identical payload re-sent every turn
    forever, which is the stall this module exists to remove.

    They are not actually in conflict: a batch must carry a message to be
    parsed AND be under the limit to be accepted, and since the cursor is a
    single byte offset only a PREFIX can be sent. If the shortest
    message-bearing prefix is over the limit, no legal batch exists. So stop
    trying — dormancy costs nothing, because SessionEnd sends the whole
    transcript independently of this cursor."""
    print("_shrink_slice — the ladder terminates")
    original = ft.STATE_DIR
    att = lambda i: {"type": "attachment", "uuid": f"att{i}",
                     "attachment": {"pasted": "x" * 200_000}}
    sendable = [att(0), att(1), att(2), _rec("u1")]
    batch, _ = ft._bounded(sendable, [100, 200, 300, 400], 400,
                           ft._MIN_SLICE_BYTES, True)
    check("staying legal does widen past one record",
          len(batch) > 1, True)
    with tempfile.TemporaryDirectory() as tmp:
        ft.STATE_DIR = Path(tmp)
        try:
            sid = "terminal"
            ft._save_state(sid, slice_bytes=ft._MIN_SLICE_BYTES,
                           one_record=True, last_error="payload_too_large")
            ft._shrink_slice(sid, ft._read_state(sid), batch, 400)
            state = ft._read_state(sid)
            check("the ladder ends in dormancy", state.get("unsupported"), True)
            check("the cursor is still not moved", state.get("offset"), None)
            check("and the breadcrumb survives, so the banner still explains it",
                  state.get("last_error"), "payload_too_large")
            check("no false success is recorded", state.get("last_ok_at"), None)
        finally:
            ft.STATE_DIR = original


def test_one_unsendable_record_is_stepped_over():
    """A record above the floor cannot ride in any payload we can build, so
    pinning the cursor on it means the session never captures another turn. It
    is skipped and the loss is written down — one record against every turn
    that follows it."""
    print("_shrink_slice — stepping over a record")
    original = ft.STATE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        ft.STATE_DIR = Path(tmp)
        try:
            sid = "skip"
            big = _fat("big", ft._MIN_SLICE_BYTES + 5_000)
            check("the record really is over the floor",
                  ft._record_bytes(big) > ft._MIN_SLICE_BYTES, True)
            ft._shrink_slice(sid, {"slice_bytes": ft._MIN_SLICE_BYTES},
                             [big], 7_777)
            state = ft._read_state(sid)
            check("the cursor steps past it", state.get("offset"), 7_777)
            check("under its own reason",
                  state.get("last_error"), "payload_too_large")
            check("and the detail says what was lost",
                  "skipped one record" in (state.get("last_error_detail") or ""),
                  True)
            check("no false success is recorded",
                  state.get("last_ok_at"), None)
        finally:
            ft.STATE_DIR = original


def test_a_small_record_is_never_deleted_by_a_spurious_413():
    """REGRESSION LOCK, and the nastiest bug this change nearly shipped.

    The skip used to key on the CAP being at the floor rather than on the
    record's own size — so a 67-byte record was permanently deleted on a single
    413. And because the cap is sticky and never restored on success, one
    earlier pathological record put the session in delete-on-413 mode for the
    rest of its life, silently discarding ordinary turns. That is exactly the
    permanent invisible loss the cursor rule exists to prevent, so the size of
    the record — not the state of the cap — is what decides."""
    print("_shrink_slice — a small record survives")
    original = ft.STATE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        ft.STATE_DIR = Path(tmp)
        try:
            sid = "keep"
            tiny = _rec("tiny", "hi")
            check("the record is far below the floor",
                  ft._record_bytes(tiny) < ft._MIN_SLICE_BYTES, True)
            ft._shrink_slice(sid, {"slice_bytes": ft._MIN_SLICE_BYTES},
                             [tiny], 123)
            state = ft._read_state(sid)
            check("the cursor does NOT move past it", state.get("offset"), None)
            check("nothing pretends it succeeded", state.get("last_ok_at"), None)
        finally:
            ft.STATE_DIR = original


def test_a_size_refusal_without_a_413_status_is_still_recognized():
    """A proxy, or a server answering inside a 200 envelope or as a tool error,
    delivers the same refusal with no status to branch on. Read as a generic
    fault it would never bring the cap down, and the session would stall
    exactly as it did before the batch was bounded."""
    print("_is_size_refusal")
    check("the server's own 413 wording",
          ft._is_size_refusal("tools/call failed: Request body too large"), True)
    check("a proxy's wording",
          ft._is_size_refusal("413 Request Entity Too Large"), True)
    check("a slug echoed back",
          ft._is_size_refusal("error: payload_too_large"), True)
    check("an ordinary failure is not one",
          ft._is_size_refusal("connection reset by peer"), False)
    # A bare status code must NOT match: it is a plausible substring of an
    # unrelated message, and a false positive puts the session on the shrink
    # ladder — and eventually the step-over — for no reason.
    check("a bare number is not a size refusal",
          ft._is_size_refusal("upstream returned 413xyz unknown"), False)
    check("empty is not one", ft._is_size_refusal(""), False)
    check("None is not one", ft._is_size_refusal(None), False)


# ── end to end through _flush ─────────────────────────────────────────

class _Recorder:
    """A stub MCP session that accepts anything and records what it was sent."""

    def __init__(self, *_a, **_k):
        pass

    sent = []

    async def call_tool(self, _name, arguments):
        _Recorder.sent.append(arguments)
        return types.SimpleNamespace(
            structuredContent={"conversation_id": arguments["conversation_id"],
                               "ack_through": arguments["messages"][-1]["uuid"]},
            content=[], isError=False)


class _Refuser:
    """A stub MCP session that refuses every payload with a 413, whatever its
    size — standing in for a server whose limit is below anything we send."""

    seen = []

    def __init__(self, *_a, **_k):
        pass

    async def call_tool(self, _name, arguments):
        _Refuser.seen.append(len(arguments["messages"]))
        raise mcp_http.McpError(
            "tools/call failed (413): Request body too large", 413)


def _patched(tmp):
    """Swap the network and the state dir out; returns the originals."""
    originals = {
        "state_dir": ft.STATE_DIR, "namespace": ft._namespace,
        "resolve_bearer": ft.resolve_bearer,
        "resolve_repo_brain": ft.resolve_repo_brain,
        "env_for_url": ft.env_for_url, "session": ft.mcp_http.Session,
        "log": ft._log,
    }

    async def no_room(_session, _cwd, _env):
        return None

    ft.STATE_DIR = Path(tmp) / "state"
    ft._namespace = lambda _records: ("/repo", "agent-plugins")
    ft.resolve_bearer = lambda: ("https://example.test/mcp", "token")
    ft.resolve_repo_brain = no_room
    ft.env_for_url = lambda _url: "staging"
    ft._log = lambda _message: None
    return originals


def _restore(originals):
    ft.STATE_DIR = originals["state_dir"]
    ft._namespace = originals["namespace"]
    ft.resolve_bearer = originals["resolve_bearer"]
    ft.resolve_repo_brain = originals["resolve_repo_brain"]
    ft.env_for_url = originals["env_for_url"]
    ft.mcp_http.Session = originals["session"]
    ft._log = originals["log"]


def test_a_backlogged_session_drains_over_turns_instead_of_stalling():
    """THE regression. A delta far past one payload used to 413 forever; now
    each turn carries what it can and the session catches up."""
    print("_flush — a backlogged session drains")
    _Recorder.sent = []
    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "session.jsonl"
        # Records sized so the FLOOR cap genuinely splits them — no monkeypatch
        # over `_slice_bytes`. Patching it out is what let an earlier version of
        # this suite pass with the state→cap wiring severed: every assertion
        # here held even when `_flush` ignored the remembered cap entirely.
        records = [_fat("a", 200_000), _fat("b", 200_000), _fat("c", 200_000)]
        size = _write(transcript, records)
        ends = []
        running = 0
        for r in records:
            running += len(json.dumps(r).encode()) + 1  # + the newline
            ends.append(running)
        originals = _patched(tmp)
        ft.mcp_http.Session = _Recorder
        try:
            sid = "drain"
            # A server limit lower than the delta, remembered as if an earlier
            # 413 had taught us — the same state the real path reaches.
            ft._save_state(sid, slice_bytes=ft._MIN_SLICE_BYTES)

            asyncio.run(ft._flush(sid, str(transcript)))
            check("turn 1 sends one record",
                  [r["uuid"] for r in _Recorder.sent[0]["messages"]], ["a"])
            check("and advances to exactly that record's end",
                  ft._read_state(sid).get("offset"), ends[0])
            check("the remembered cap is the reason it split",
                  ft._read_state(sid).get("slice_bytes"), ft._MIN_SLICE_BYTES)

            asyncio.run(ft._flush(sid, str(transcript)))
            check("turn 2 carries the next one",
                  [r["uuid"] for r in _Recorder.sent[1]["messages"]], ["b"])
            check("and advances to exactly its end",
                  ft._read_state(sid).get("offset"), ends[1])

            asyncio.run(ft._flush(sid, str(transcript)))
            check("turn 3 finishes the backlog",
                  [r["uuid"] for r in _Recorder.sent[2]["messages"]], ["c"])
            check("and the session is fully caught up",
                  ft._read_state(sid).get("offset"), size)
            check("no failure was ever recorded",
                  ft._read_state(sid).get("last_error"), None)
        finally:
            _restore(originals)


def test_a_413_is_reported_under_its_own_reason_not_as_error():
    """The banner used to say 'the capture hook hit an unexpected error … run
    /memhub:login --status' — pointing at a credential that was fine, above a
    session whose capture had been dead for hours."""
    print("_flush — a 413 is classified")
    _Refuser.seen = []
    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "session.jsonl"
        _write(transcript, [_rec("a"), _rec("b")])
        originals = _patched(tmp)
        ft.mcp_http.Session = _Refuser
        try:
            sid = "refused"
            asyncio.run(ft._flush(sid, str(transcript)))
            state = ft._read_state(sid)
            check("the reason is payload_too_large",
                  state.get("last_error"), "payload_too_large")
            check("the detail keeps the server's own words",
                  "413" in (state.get("last_error_detail") or ""), True)
            check("the cursor is unmoved", state.get("offset"), None)
            check("and the cap came down for next turn",
                  state.get("slice_bytes"),
                  transcript_chunks.DEFAULT_CHUNK_BYTES // 2)
        finally:
            _restore(originals)


def test_a_413_never_advances_past_records_that_could_still_be_sent():
    """The invariant the whole module is built on. Stepping over a record is
    permitted ONLY for a record bigger than the floor; anything else must keep
    retrying, because it is the server refusing, not the record being
    unsendable."""
    print("_flush — a refused batch keeps its records")
    _Refuser.seen = []
    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "session.jsonl"
        _write(transcript, [_rec("a"), _rec("b")])
        originals = _patched(tmp)
        ft.mcp_http.Session = _Refuser
        try:
            sid = "pinned"
            ft._save_state(sid, slice_bytes=ft._MIN_SLICE_BYTES)
            asyncio.run(ft._flush(sid, str(transcript)))
            state = ft._read_state(sid)
            check("both records were offered together", _Refuser.seen[-1], 2)
            check("the cursor stays pinned", state.get("offset"), None)
            check("and the floor falls through to one record per turn",
                  state.get("one_record"), True)

            # Next turn: one record, still refused, and it is SMALL — so it
            # must be kept, not deleted. This is the loop the original code
            # would have turned into silent deletion of ordinary turns.
            asyncio.run(ft._flush(sid, str(transcript)))
            state = ft._read_state(sid)
            check("the retry offers exactly one record", _Refuser.seen[-1], 1)
            check("a small record is kept, not stepped over",
                  state.get("offset"), None)
            check("and it is still reported as a size refusal",
                  state.get("last_error"), "payload_too_large")
        finally:
            _restore(originals)


def test_the_cursor_tracks_the_record_sent_not_the_bytes_read():
    """``_read_tail`` reports a byte end offset per record, and they stay in
    step with the records. An ``ends`` list that drifted would move the cursor
    past a record that was never sent — the one failure this module exists to
    prevent."""
    print("_read_tail — per-record offsets")
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "t.jsonl"
        lines = [_rec("a", "🎉 café 日本語"), _rec("b"), _rec("c")]
        size = _write(p, lines)
        records, ends, consumed = ft._read_tail(str(p), 0)
        check("one end per record", len(ends), len(records))
        check("they ascend", ends == sorted(ends), True)
        check("the last one is the consumed span", ends[-1], consumed)
        check("and the consumed span is the file", consumed, size)
        # Each end must be a real resume point: reading from it yields exactly
        # the records after it.
        for i, end in enumerate(ends):
            rest, _, _ = ft._read_tail(str(p), end)
            check(f"resuming at ends[{i}] yields the rest",
                  [r["uuid"] for r in rest],
                  [r["uuid"] for r in records[i + 1:]])


def test_a_dropped_wrapper_does_not_shift_the_offsets():
    """The filters run over an index-preserving copy, so a slash-command
    wrapper dropped from the middle of a delta cannot make a later record's
    cursor offset point at a different record."""
    print("_flush — offsets survive filtering")
    _Recorder.sent = []
    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "session.jsonl"
        wrapper = {"type": "user", "uuid": "w", "cwd": "/repo",
                   "message": {"role": "user",
                               "content": "<command-name>/model</command-name>"}}
        first_rec = _fat("a", 200_000)
        size = _write(transcript, [first_rec, wrapper, _fat("b", 200_000)])
        first_end = len(json.dumps(first_rec).encode()) + 1
        originals = _patched(tmp)
        ft.mcp_http.Session = _Recorder
        try:
            sid = "filtered"
            ft._save_state(sid, slice_bytes=ft._MIN_SLICE_BYTES)
            asyncio.run(ft._flush(sid, str(transcript)))
            check("the wrapper is not sent",
                  [r["uuid"] for r in _Recorder.sent[0]["messages"]], ["a"])
            check("the cursor lands on the sent record, not the wrapper",
                  ft._read_state(sid).get("offset"), first_end)

            asyncio.run(ft._flush(sid, str(transcript)))
            check("the next turn picks up the record after the wrapper",
                  [r["uuid"] for r in _Recorder.sent[1]["messages"]], ["b"])
            check("the session ends caught up",
                  ft._read_state(sid).get("offset"), size)
        finally:
            _restore(originals)


# ── the health message ────────────────────────────────────────────────

def test_the_health_banner_names_the_real_cause():
    """A 413 used to render as the generic 'unexpected error' whose advice is
    to go inspect the credential. It has to name the size and say the flush
    fixes itself, or it sends the reader to the one thing that was fine."""
    print("capture_health — payload_too_large")
    sys.path.insert(0, str(SCRIPTS))
    import capture_health as ch  # noqa: E402

    check("the reason exists", "payload_too_large" in ch._REASONS, True)
    detail = ch._REASONS["payload_too_large"]
    check("it names the size, not the credential", "too large" in detail, True)
    check("it does not say 'unexpected error'",
          "unexpected error" in detail, False)

    # REGRESSION LOCK. The recoverable case and the terminal one must not read
    # the same. Once the flush has gone dormant, telling the user that capture
    # "splits it over the next few turns on its own, and the session-end
    # backstop covers the rest" is a promise nothing will keep — per-turn
    # capture has stopped trying, and the session-end path slices at a fixed
    # size with no adaptive handling, so it can refuse the same content. This
    # module's own `no_refresh` comment states the principle: advice that
    # cannot work is worse than no advice, because it spends the user's trust
    # proving it. And a reassuring banner over silently-absent capture is the
    # exact failure ENG-1085 began with.
    now = time.time()
    recovering = ch._message(None, None, ("payload_too_large", now, False), None)
    terminal = ch._message(None, None, ("payload_too_large", now, True), None)
    check("the recoverable case still reassures",
          "splits it over the next few turns" in recovering, True)
    check("the terminal case does NOT promise the backstop covers it",
          "backstop covers the rest" in terminal, False)
    check("the terminal case says capture has stopped",
          "dormant" in terminal, True)
    check("and gives the one lever that is actually the user's",
          "/memhub:import-session" in terminal, True)
    check("both still name the size rather than the credential",
          "too large" in recovering and "too large" in terminal, True)
    check("neither sends them to check the credential",
          "--status" in recovering or "--status" in terminal, False)

    # A 2-tuple from an older caller must not raise on SessionStart.
    legacy = ch._message(None, None, ("payload_too_large", now), None)
    check("a legacy 2-tuple still renders", "too large" in legacy, True)


if __name__ == "__main__":
    # Discovered from globals(), NOT a hand-maintained tuple — the same reason
    # flush_turn_test.py does it: a registration list in a second place
    # eventually disagrees with the file, and the suite passes over less.
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        raise SystemExit(1)
    print("all passed")
