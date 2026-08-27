#!/usr/bin/env python3
"""Per-record timestamp pinning for Cursor capture.

The server persists a record's ``timestamp`` as the row's ``event_date``,
whose contract is "the turn's clock at its source; NULL means unmeasured"
(MemHub claude_parts, ENG-675b). Cursor's artifacts carry real clocks for
only SOME records, and the old fallbacks (meta.json ``updatedAtMs``, file
ctime, tag carry-forward) dated everything else at flush-adjacent time —
collapsing whole production sessions onto one or two event_date values.

This suite pins the replacement: artifact clocks ride verbatim, undated
records are dated by the hook that FIRST OBSERVES them (a real turn-boundary
clock), every stamp is pinned in session state so re-sends never re-date, and
what has no real clock stays honestly unmeasured.

Run: python3 cursor_timestamp_test.py
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import cursor_flush  # noqa: E402
from readers import cursor as cursor_reader  # noqa: E402

SESSION = "8af6c93d-7e11-4a47-a6a4-2084d62ed1c9"
GEN_1 = "324ce2e2-d685-4fec-9978-c05b9fb907b3"
GEN_2 = "ca6fc036-69a7-4e46-bef4-8e773133cabb"

TAG_1 = ("<timestamp>Sunday, Aug 23, 2026, 11:16 AM (UTC-7)</timestamp>\n"
         "<user_query>\nReply once\n</user_query>")
TAG_2 = ("<timestamp>Sunday, Aug 23, 2026, 11:18 AM (UTC-7)</timestamp>\n"
         "<user_query>\nEdit it\n</user_query>")
ISO_1 = "2026-08-23T18:16:00.000Z"
ISO_2 = "2026-08-23T18:18:00.000Z"
NOW_1 = "2026-08-23T18:17:05.000Z"
NOW_2 = "2026-08-23T18:19:30.000Z"

TURN_1 = [
    {"role": "user", "message": {"content": [{"type": "text", "text": TAG_1}]}},
    {"role": "assistant", "message": {"content": [
        {"type": "text", "text": "Thinking it through."}]}},
    {"role": "assistant", "message": {"content": [
        {"type": "text", "text": "USAGE_OK"}]}},
    {"type": "turn_ended", "status": "success"},
]
TURN_2 = [
    {"role": "user", "message": {"content": [{"type": "text", "text": TAG_2}]}},
    {"role": "assistant", "message": {"content": [
        {"type": "text", "text": "Working."}]}},
    {"role": "assistant", "message": {"content": [
        {"type": "text", "text": "SECOND"}]}},
    {"type": "turn_ended", "status": "success"},
]


def _rec(uuid: str, ts: str | None = None, role: str = "assistant") -> dict:
    record = {"type": role, "uuid": uuid,
              "message": {"role": role, "content": [
                  {"type": "text", "text": uuid}]}}
    if ts:
        record["timestamp"] = ts
    return record


# ---- _stamp_records unit behavior --------------------------------------


def test_first_observation_backlog_stays_unmeasured():
    records = [_rec("banner", ts=ISO_1, role="user"),
               _rec("thinking"), _rec("final")]
    stamps = cursor_flush._stamp_records(
        records, None, NOW_1, first_observation=True,
        boundary_uuids={"final"})
    # Artifact clocks ride; the boundary record gets the hook's clock; the
    # rest of a first-observation backlog predates any hook by an unknowable
    # margin and stays unmeasured — NEVER "now".
    assert records[0]["timestamp"] == ISO_1
    assert "timestamp" not in records[1]
    assert records[2]["timestamp"] == NOW_1
    assert stamps == {"banner": ISO_1, "thinking": None, "final": NOW_1}
    print("PASS test_first_observation_backlog_stays_unmeasured")


def test_new_records_are_dated_and_pins_never_drift():
    prior = {"old": NOW_1}
    records = [_rec("old"), _rec("fresh")]
    stamps = cursor_flush._stamp_records(
        records, prior, NOW_2, first_observation=False)
    assert records[0]["timestamp"] == NOW_1   # pinned, not re-dated
    assert records[1]["timestamp"] == NOW_2   # first seen by THIS hook
    # A later re-send re-applies identical stamps whatever the clock says,
    # and a pin outranks reader drift for the same record.
    later = [_rec("old", ts="2026-08-23T23:59:59.000Z"), _rec("fresh")]
    again = cursor_flush._stamp_records(
        later, stamps, "2026-08-24T09:00:00.000Z", first_observation=False)
    assert later[0]["timestamp"] == NOW_1
    assert later[1]["timestamp"] == NOW_2
    assert again == {"old": NOW_1, "fresh": NOW_2}
    print("PASS test_new_records_are_dated_and_pins_never_drift")


def test_unmeasured_pin_upgrades_when_artifact_gains_clock():
    stamps = {"u1": None}
    records = [_rec("u1", ts=ISO_2)]
    out = cursor_flush._stamp_records(
        records, stamps, NOW_2, first_observation=False)
    assert records[0]["timestamp"] == ISO_2
    assert out == {"u1": ISO_2}
    print("PASS test_unmeasured_pin_upgrades_when_artifact_gains_clock")


def test_apply_only_mode_mints_nothing():
    records = [_rec("pinned"), _rec("unknown"), _rec("tagged", ts=ISO_1)]
    cursor_flush._stamp_records(
        records, {"pinned": NOW_1}, None, first_observation=True)
    assert records[0]["timestamp"] == NOW_1     # pin replayed
    assert "timestamp" not in records[1]        # nothing minted
    assert records[2]["timestamp"] == ISO_1     # artifact clock kept
    print("PASS test_apply_only_mode_mints_nothing")


def test_pin_map_prunes_only_orphaned_uuids_never_present_records():
    cap = cursor_flush._RECORD_TS_CAP
    # Over the cap with a mix: pins whose records are still in the batch
    # MUST survive — evicting one would re-date a live record at the next
    # flush's wall clock (the review finding on the original FIFO cap).
    # Only pins orphaned by the artifact (uuids no longer present, e.g. a
    # checkpoint restore shifting the index-derived ids) are pruned.
    prior = {f"orphan{i}": NOW_1 for i in range(cap)}
    prior["live"] = NOW_1
    records = [_rec("live"), _rec("newest")]
    out = cursor_flush._stamp_records(
        records, prior, NOW_2, first_observation=False)
    assert out == {"live": NOW_1, "newest": NOW_2}, len(out)
    assert records[0]["timestamp"] == NOW_1
    # Re-running with the pruned map re-mints nothing: the live pins were
    # kept, so no record ever falls into the "new uuid" branch again.
    rerun = [_rec("live"), _rec("newest")]
    cursor_flush._stamp_records(
        rerun, out, "2026-08-24T09:00:00.000Z", first_observation=False)
    assert rerun[0]["timestamp"] == NOW_1
    assert rerun[1]["timestamp"] == NOW_2

    # Under the cap, orphaned pins are kept (continuity across a transient
    # shrunk read costs nothing until the map is actually oversized).
    small = cursor_flush._stamp_records(
        [_rec("live")], {"gone": NOW_1, "live": NOW_1}, NOW_2,
        first_observation=False)
    assert small == {"gone": NOW_1, "live": NOW_1}

    # All-present pins are never evicted even when over the cap: the map's
    # true bound is the artifact itself, which every flush re-reads whole.
    oversized_live = {f"u{i}": NOW_1 for i in range(cap + 10)}
    batch = [_rec(f"u{i}") for i in range(cap + 10)]
    kept = cursor_flush._stamp_records(
        batch, oversized_live, NOW_2, first_observation=False)
    assert len(kept) == cap + 10
    print("PASS test_pin_map_prunes_only_orphaned_uuids_never_present_records")


# ---- main() flow: persisted before send, shipped on every send ---------


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8")


def _payload(path: Path, event: str, generation: str, text: str) -> dict:
    payload = {
        "hook_event_name": event, "session_id": SESSION,
        "conversation_id": SESSION, "generation_id": generation,
        "transcript_path": str(path),
        "workspace_roots": [str(path.parent)],
        "model": "cursor-test-model",
        "input_tokens": 100, "output_tokens": 10,
        "cache_read_tokens": 5, "cache_write_tokens": 1,
        "status": "completed",
    }
    if event == "afterAgentResponse":
        payload["text"] = text
    return payload


def _run_main(event: str, payload: dict) -> int:
    old_stdin, old_argv = sys.stdin, sys.argv
    try:
        sys.stdin = io.StringIO(json.dumps(payload))
        sys.argv = ["cursor_flush.py", event]
        return cursor_flush.main()
    finally:
        sys.stdin, sys.argv = old_stdin, old_argv


def _stamps_of(records: list[dict]) -> list[str | None]:
    return [record.get("timestamp") for record in records]


def test_main_flow_pins_persist_and_ship():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        projects = root / "projects"
        path = (projects / "fixture" / "agent-transcripts" / SESSION /
                f"{SESSION}.jsonl")
        calls: list[list[dict]] = []
        clock = {"now": NOW_1}
        originals = {
            "state_dir": cursor_flush.STATE_DIR,
            "projects": cursor_flush._CURSOR_PROJECTS,
            "reader_chats": cursor_flush.cursor_reader._CHATS,
            "reader_projects": cursor_flush.cursor_reader._PROJECTS,
            "flush": cursor_flush._flush,
            "log": cursor_flush._log,
            "now_iso": cursor_flush._now_iso,
        }

        async def fake_flush(uuid, source_path, blob_ids, flush_mode="now",
                             **kwargs):
            calls.append(json.loads(json.dumps(kwargs["records"])))
            cursor_flush._save_state(
                uuid, transcript_revision=kwargs["source_revision"],
                sent_usage_generations=sorted(kwargs["applied_usage"]),
                last_flush_at=1_787_000_000.0)

        try:
            cursor_flush.STATE_DIR = root / "state"
            cursor_flush._CURSOR_PROJECTS = projects
            cursor_flush.cursor_reader._CHATS = root / "chats"
            cursor_flush.cursor_reader._PROJECTS = projects
            cursor_flush._flush = fake_flush
            cursor_flush._log = lambda _message: None
            cursor_flush._now_iso = lambda: clock["now"]

            _write_rows(path, TURN_1)
            assert _run_main("afterAgentResponse", _payload(
                path, "afterAgentResponse", GEN_1, "USAGE_OK")) == 0
            assert len(calls) == 1
            # banner + user ride their artifact tags; the usage target is
            # dated by THIS boundary hook even in a first-observation
            # backlog; the mid-turn record has no real clock and stays
            # unmeasured.
            assert _stamps_of(calls[0]) == [ISO_1, ISO_1, None, NOW_1]
            assert calls[0][3]["message"]["usage"]["input_tokens"] == 100
            state = cursor_flush._read_state(SESSION)
            assert sorted(state["record_ts"].values(),
                          key=lambda v: (v is None, v)) == [
                ISO_1, ISO_1, NOW_1, None]

            # Turn 2 lands; the stop hook dates ONLY the records it first
            # observes — turn 1's pins replay verbatim (including the
            # unmeasured one), whatever the wall clock now says.
            _write_rows(path, TURN_1 + TURN_2)
            clock["now"] = NOW_2
            assert _run_main("stop", _payload(
                path, "stop", GEN_2, "SECOND")) == 0
            assert len(calls) == 2
            assert _stamps_of(calls[1]) == [
                ISO_1, ISO_1, None, NOW_1, ISO_2, NOW_2, NOW_2]
            assert calls[1][6]["message"]["usage"]["input_tokens"] == 100
            assert _stamps_of(calls[1])[:4] == _stamps_of(calls[0])

            # apply_session_state restores the same fidelity onto a fresh
            # out-of-band re-read (the capture.py / sweep backstop).
            records, _meta = cursor_reader.to_canonical(
                path, session_id=SESSION)
            assert _stamps_of(records) == [
                ISO_1, ISO_1, None, None, ISO_2, None, None]
            cursor_flush.apply_session_state(records, SESSION)
            assert _stamps_of(records) == _stamps_of(calls[1])
            assert records[3]["message"]["usage"]["input_tokens"] == 100
            assert records[6]["message"]["usage"]["input_tokens"] == 100
        finally:
            cursor_flush.STATE_DIR = originals["state_dir"]
            cursor_flush._CURSOR_PROJECTS = originals["projects"]
            cursor_flush.cursor_reader._CHATS = originals["reader_chats"]
            cursor_flush.cursor_reader._PROJECTS = originals["reader_projects"]
            cursor_flush._flush = originals["flush"]
            cursor_flush._log = originals["log"]
            cursor_flush._now_iso = originals["now_iso"]
    print("PASS test_main_flow_pins_persist_and_ship")


def test_apply_session_state_without_state_is_inert():
    with tempfile.TemporaryDirectory() as td:
        old_state = cursor_flush.STATE_DIR
        cursor_flush.STATE_DIR = Path(td) / "state"
        try:
            records = [_rec("tagged", ts=ISO_1), _rec("bare")]
            cursor_flush.apply_session_state(records, SESSION)
            assert records[0]["timestamp"] == ISO_1
            assert "timestamp" not in records[1]
            assert "usage" not in records[1]["message"]
        finally:
            cursor_flush.STATE_DIR = old_state
    print("PASS test_apply_session_state_without_state_is_inert")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
