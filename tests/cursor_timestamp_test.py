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
GEN_3 = "2fea5ff0-44df-4223-b409-0c92ef551c42"
GEN_4 = "109a78fc-9b1f-41bc-95ac-5e0628efb894"

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
    trigger = cursor_flush._RECORD_TS_PRUNE_TRIGGER
    # Over the trigger with a mix: pins whose records are still in the batch
    # MUST survive — evicting one would re-date a live record at the next
    # flush's wall clock (the review finding on the original FIFO cap).
    # Only pins orphaned by the artifact (uuids no longer present, e.g. a
    # checkpoint restore shifting the index-derived ids) are pruned.
    prior = {f"orphan{i}": NOW_1 for i in range(trigger)}
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

    # Under the trigger, orphaned pins are kept (continuity across a
    # transient shrunk read costs nothing until the map is actually
    # oversized).
    small = cursor_flush._stamp_records(
        [_rec("live")], {"gone": NOW_1, "live": NOW_1}, NOW_2,
        first_observation=False)
    assert small == {"gone": NOW_1, "live": NOW_1}

    # All-present pins survive even past the trigger — the trigger is a
    # prune opportunity, deliberately NOT a hard cap: the map scales with
    # the live artifact, which every flush already re-reads and re-uploads
    # whole, so the transcript itself is the binding cost at that size.
    oversized_live = {f"u{i}": NOW_1 for i in range(trigger + 10)}
    batch = [_rec(f"u{i}") for i in range(trigger + 10)]
    kept = cursor_flush._stamp_records(
        batch, oversized_live, NOW_2, first_observation=False)
    assert len(kept) == trigger + 10
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
        # IDE input is gross of its 5 cache reads, so 95 is stored.
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

        revisions: list[str] = []

        async def fake_flush(uuid, source_path, blob_ids, flush_mode="now",
                             **kwargs):
            calls.append(json.loads(json.dumps(kwargs["records"])))
            revisions.append(kwargs["source_revision"])
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
            assert calls[0][3]["message"]["usage"]["input_tokens"] == 95
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
            assert calls[1][6]["message"]["usage"]["input_tokens"] == 95
            assert _stamps_of(calls[1])[:4] == _stamps_of(calls[0])

            # A third boundary with NO new content but a fresh usage sample:
            # the send is forced by usage_pending, and the content revision
            # is IDENTICAL to the previous send's — the gate hashes pre-stamp
            # content only, so pin minting/upgrading can never re-fire it.
            clock["now"] = "2026-08-23T18:20:00.000Z"
            assert _run_main("stop", _payload(
                path, "stop", GEN_3, "SECOND")) == 0
            assert len(calls) == 3
            assert revisions[2] == revisions[1]
            assert _stamps_of(calls[2]) == _stamps_of(calls[1])

            # A guaranteed non-sender (non-milestone shell) exits before
            # even reading the transcript — no pins minted or persisted,
            # no observation recorded (a quiet READER before the first
            # flush would skew first_observation — review finding). The
            # next flushing hook dates the new record at ITS clock, so the
            # shell event's clock appears in no pin.
            _write_rows(path, TURN_1 + TURN_2 + [
                {"role": "assistant", "message": {"content": [
                    {"type": "text", "text": "THIRD"}]}},
                {"type": "turn_ended", "status": "success"},
            ])
            quiet_clock = "2026-08-23T18:20:30.000Z"
            clock["now"] = quiet_clock
            assert _run_main("beforeShellExecution", {
                "hook_event_name": "beforeShellExecution",
                "session_id": SESSION, "conversation_id": SESSION,
                "transcript_path": str(path),
                "workspace_roots": [str(path.parent)],
                "command": "ls",
            }) == 0
            assert len(calls) == 3                      # no flush happened
            state = cursor_flush._read_state(SESSION)
            assert len(state["record_ts"]) == 7         # THIRD not pinned
            clock["now"] = "2026-08-23T18:21:00.000Z"
            assert _run_main("stop", _payload(
                path, "stop", GEN_4, "THIRD")) == 0
            assert len(calls) == 4
            assert _stamps_of(calls[3]) == _stamps_of(calls[1]) + [
                "2026-08-23T18:21:00.000Z"]
            state = cursor_flush._read_state(SESSION)
            assert quiet_clock not in state["record_ts"].values()

            # Current Cursor JSONL omits tool results, so the exact PR URL
            # arrives only on afterShellExecution. A new URL is durable before
            # the send and forces a resend even though content is unchanged;
            # without an acknowledgement, duplicate delivery retries it.
            pr_url = "https://github.com/xtraceai/agent-plugins/pull/456"
            shell_payload = {
                "hook_event_name": "afterShellExecution",
                "session_id": SESSION,
                "conversation_id": SESSION,
                "transcript_path": str(path),
                "workspace_roots": [str(path.parent)],
                "command": "gh pr create --fill",
                "output": pr_url,
            }
            assert _run_main("afterShellExecution", shell_payload) == 0
            assert len(calls) == 5
            assert revisions[4] == revisions[3]
            state = cursor_flush._read_state(SESSION)
            assert state["pending_pr_urls"] == [pr_url]
            assert _run_main("afterShellExecution", shell_payload) == 0
            assert len(calls) == 6

            # apply_session_state restores the same fidelity onto a fresh
            # out-of-band re-read (the capture.py / sweep backstop).
            records, _meta = cursor_reader.to_canonical(
                path, session_id=SESSION)
            assert _stamps_of(records) == [
                ISO_1, ISO_1, None, None, ISO_2, None, None, None]
            cursor_flush.apply_session_state(records, SESSION)
            assert _stamps_of(records) == _stamps_of(calls[3])
            assert records[3]["message"]["usage"]["input_tokens"] == 95
            assert records[7]["message"]["usage"]["input_tokens"] == 95
        finally:
            cursor_flush.STATE_DIR = originals["state_dir"]
            cursor_flush._CURSOR_PROJECTS = originals["projects"]
            cursor_flush.cursor_reader._CHATS = originals["reader_chats"]
            cursor_flush.cursor_reader._PROJECTS = originals["reader_projects"]
            cursor_flush._flush = originals["flush"]
            cursor_flush._log = originals["log"]
            cursor_flush._now_iso = originals["now_iso"]
    print("PASS test_main_flow_pins_persist_and_ship")


def test_capture_session_sid_falls_back_to_real_uuid():
    """The capture.py fallback must key flush state by the SESSION uuid: a
    store.db's uuid is its directory name — the file stem is just 'store',
    which would silently no-op the state restore (review finding)."""
    import capture
    store = Path("/home/u/.cursor/chats/hash") / SESSION / "store.db"
    assert capture._session_sid({}, store) == SESSION
    transcript = Path("/x/agent-transcripts") / SESSION / f"{SESSION}.jsonl"
    assert capture._session_sid({}, transcript) == SESSION
    assert capture._session_sid({"session_id": "meta-wins"}, store) == "meta-wins"
    assert capture._session_sid({"session_id": ""}, store) == SESSION
    print("PASS test_capture_session_sid_falls_back_to_real_uuid")


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


def test_event_can_flush_seals_quiet_observers():
    """Every event that READS the transcript must be a potential sender —
    that is what lets ``record_ts`` absence mean "never observed" for
    first_observation. Guaranteed non-senders are refused up front."""
    can = cursor_flush._event_can_flush
    assert not can("beforeShellExecution", {"command": "ls -la"})
    assert not can("beforeShellExecution", {"command": ["git", "commit"]})
    assert not can("beforeShellExecution", {})
    assert can("beforeShellExecution", {"command": "git commit -m x"})
    assert can("beforeShellExecution", {"command": "gh pr create -f"})
    assert can("afterShellExecution", {
        "command": "gh pr create -f",
        "output": "https://github.com/x/r/pull/1",
    })
    assert not can("afterShellExecution", {
        "command": "gh pr create -f", "output": "failed",
    })
    for event in ("afterFileEdit", "afterAgentResponse", "stop",
                  "beforeSubmitPrompt", "sessionEnd"):
        assert can(event, {}), event
    assert not can("someFutureHook", {})
    assert not can("unknown", {})
    # sessionStart saves only its clock (see the sessionStart tests below);
    # it never reads the transcript, so it stays outside the observers.
    assert not can("sessionStart", {})
    print("PASS test_event_can_flush_seals_quiet_observers")


def test_capture_restore_is_best_effort_never_fatal():
    """A broken cursor_flush (or a failing state read) must degrade the
    import to artifact-carried clocks, not abort it (review finding)."""
    import types as _types
    import capture
    poisoned = _types.ModuleType("cursor_flush")

    def _boom(*_args):
        raise RuntimeError("boom")

    poisoned.apply_session_state = _boom
    real = sys.modules.get("cursor_flush")
    old_stderr = sys.stderr
    try:
        sys.modules["cursor_flush"] = poisoned
        sys.stderr = io.StringIO()
        records = [_rec("tagged", ts=ISO_1)]
        capture._restore_cursor_state(records, SESSION)   # must not raise
        assert records[0]["timestamp"] == ISO_1
        assert "artifact-carried clocks" in sys.stderr.getvalue()
    finally:
        sys.stderr = old_stderr
        if real is not None:
            sys.modules["cursor_flush"] = real
        else:
            sys.modules.pop("cursor_flush", None)
    print("PASS test_capture_restore_is_best_effort_never_fatal")


def test_apply_session_state_refuses_non_uuid_ids():
    """Same gate as the live path (review finding): a caller-chosen id that
    is not a real session uuid must not select ANY state file — even one
    that exists under the sanitized name."""
    with tempfile.TemporaryDirectory() as td:
        old_state = cursor_flush.STATE_DIR
        cursor_flush.STATE_DIR = Path(td) / "state"
        try:
            cursor_flush.STATE_DIR.mkdir(parents=True)
            planted = {"record_ts": {"bare": NOW_1}}
            for name in ("store", "..", "a/b"):
                (cursor_flush.STATE_DIR /
                 f"{cursor_flush._safe_uuid(name)}.json").write_text(
                    json.dumps(planted), encoding="utf-8")
                records = [_rec("bare")]
                cursor_flush.apply_session_state(records, name)
                assert "timestamp" not in records[0], name
            # A real uuid still applies its own state.
            (cursor_flush.STATE_DIR / f"{SESSION}.json").write_text(
                json.dumps(planted), encoding="utf-8")
            records = [_rec("bare")]
            cursor_flush.apply_session_state(records, SESSION)
            assert records[0]["timestamp"] == NOW_1
        finally:
            cursor_flush.STATE_DIR = old_state
    print("PASS test_apply_session_state_refuses_non_uuid_ids")


# ---- hook wall clocks: sessionStart, beforeSubmitPrompt, sessionEnd ------


class _Flow:
    """main() against a fixture transcript, with the clock and send faked."""

    def __init__(self, root: Path):
        self.path = (root / "projects" / "fixture" / "agent-transcripts" /
                     SESSION / f"{SESSION}.jsonl")
        self.root = root
        self.calls: list[list[dict]] = []
        self.now = NOW_1

    def __enter__(self):
        self.originals = (cursor_flush.STATE_DIR, cursor_flush._CURSOR_PROJECTS,
                          cursor_flush.cursor_reader._CHATS,
                          cursor_flush.cursor_reader._PROJECTS,
                          cursor_flush._flush, cursor_flush._log,
                          cursor_flush._now_iso)

        async def fake_flush(uuid, source_path, blob_ids, flush_mode="now",
                             **kwargs):
            self.calls.append(json.loads(json.dumps(kwargs["records"])))
            cursor_flush._save_state(
                uuid, transcript_revision=kwargs["source_revision"],
                sent_usage_generations=sorted(kwargs["applied_usage"]),
                pending_pr_urls=[], last_flush_at=1_787_000_000.0)

        cursor_flush.STATE_DIR = self.root / "state"
        cursor_flush._CURSOR_PROJECTS = self.root / "projects"
        cursor_flush.cursor_reader._CHATS = self.root / "chats"
        cursor_flush.cursor_reader._PROJECTS = self.root / "projects"
        cursor_flush._flush = fake_flush
        cursor_flush._log = lambda _message: None
        cursor_flush._now_iso = lambda: self.now
        return self

    def __exit__(self, *_exc):
        (cursor_flush.STATE_DIR, cursor_flush._CURSOR_PROJECTS,
         cursor_flush.cursor_reader._CHATS, cursor_flush.cursor_reader._PROJECTS,
         cursor_flush._flush, cursor_flush._log,
         cursor_flush._now_iso) = self.originals

    def hook(self, event: str, now: str, **extra) -> None:
        self.now = now
        payload = {"hook_event_name": event, "session_id": SESSION,
                   "conversation_id": SESSION, "generation_id": "",
                   "workspace_roots": [str(self.path.parent)],
                   "model": "cursor-test-model",
                   # sessionStart's transcript_path is null on IDE and CLI.
                   "transcript_path": (None if event == "sessionStart"
                                       else str(self.path))}
        payload.update(extra)
        assert _run_main(event, payload) == 0

    def state(self) -> dict:
        return cursor_flush._read_state(SESSION)


START = "2026-08-23T18:16:12.481Z"     # inside TAG_1's minute (18:16)
END_1 = "2026-08-23T18:18:20.904Z"     # after TAG_2's minute starts (18:18)
END_2 = "2026-08-23T18:18:47.115Z"


def test_session_start_saves_its_clock_without_observing():
    with tempfile.TemporaryDirectory() as td, _Flow(Path(td)) as flow:
        _write_rows(flow.path, TURN_1)
        flow.hook("sessionStart", START)
        state = flow.state()
        assert state["session_start_at"] == START
        # No pin map, no source, no send: the first REAL observation is still
        # a first observation, so its backlog semantics are unchanged.
        assert "record_ts" not in state and "source_kind" not in state
        assert flow.calls == []
        # A repeated sessionStart never moves the session's start later.
        flow.hook("sessionStart", "2026-08-23T18:40:00.000Z")
        assert flow.state()["session_start_at"] == START
    print("PASS test_session_start_saves_its_clock_without_observing")


def test_cli_session_end_dates_each_invocation_and_resends_are_stable():
    """cursor-agent: sessionStart once, then one sessionEnd per invocation
    and no usage hooks. Before, invocation 1 shipped only minute tags and
    undated records — the session spanned 0 s of agent time."""
    with tempfile.TemporaryDirectory() as td, _Flow(Path(td)) as flow:
        flow.hook("sessionStart", START)
        _write_rows(flow.path, TURN_1)
        flow.hook("sessionEnd", END_1, reason="completed")
        assert len(flow.calls) == 1
        # Banner and prompt: the session's exact start beats the 18:16 tag.
        # Final assistant: the sessionEnd clock, though this is the first
        # observation. The mid-turn record has no real clock and stays NULL.
        assert _stamps_of(flow.calls[0]) == [START, START, None, END_1]

        # --resume: no sessionStart. Invocation 2's prompt carries only an
        # 18:18 tag, which reads EARLIER than invocation 1's end (18:18:20):
        # it is raised to that bound rather than reordered before it.
        _write_rows(flow.path, TURN_1 + TURN_2)
        flow.hook("sessionEnd", END_2, reason="completed")
        assert len(flow.calls) == 2
        stamps = _stamps_of(flow.calls[1])
        assert stamps == [START, START, None, END_1, END_1, END_2, END_2]
        assert stamps[:4] == _stamps_of(flow.calls[0])
        dated = [cursor_flush._parse_iso(s) for s in stamps if s]
        assert dated == sorted(dated)
        assert (dated[-1] - dated[0]).total_seconds() > 0

        # A re-send at a much later wall clock (forced here by queued PR
        # evidence) ships the ORIGINAL dates, never its own clock.
        flow.hook("afterShellExecution", "2026-08-23T23:00:00.000Z",
                  command="gh pr create --fill",
                  output="https://github.com/xtraceai/agent-plugins/pull/9")
        assert len(flow.calls) == 3
        assert _stamps_of(flow.calls[2]) == stamps
        records, _meta = cursor_reader.to_canonical(flow.path, session_id=SESSION)
        cursor_flush.apply_session_state(records, SESSION)
        assert _stamps_of(records) == stamps
    print("PASS test_cli_session_end_dates_each_invocation_and_resends_are_stable")


def test_session_end_dates_its_records_at_the_hook_clock():
    """The sessionEnd boundary may wait on a busy upload before it reads;
    the invocation's new records must not then be dated after the final
    record its clock dates (seen live: 4 ms out of order)."""
    with tempfile.TemporaryDirectory() as td, _Flow(Path(td)) as flow:
        flow.hook("sessionStart", START)
        _write_rows(flow.path, TURN_1)
        flow.hook("sessionEnd", END_1)
        _write_rows(flow.path, TURN_1 + TURN_2)
        later = iter([END_2, "2026-08-23T18:18:49.000Z"])
        cursor_flush._now_iso = lambda: next(later)
        assert _run_main("sessionEnd", {
            "hook_event_name": "sessionEnd", "session_id": SESSION,
            "conversation_id": SESSION, "transcript_path": str(flow.path),
            "workspace_roots": [str(flow.path.parent)]}) == 0
        assert _stamps_of(flow.calls[-1])[-2:] == [END_2, END_2]
    print("PASS test_session_end_dates_its_records_at_the_hook_clock")


def test_session_end_never_dates_records_written_while_it_waits():
    """An in-flight send can hold the upload lock while the next --resume
    invocation writes its turn. sessionEnd observes before that wait, so its
    clock dates only its own invocation; the next sessionEnd keeps its end."""
    with tempfile.TemporaryDirectory() as td, _Flow(Path(td)) as flow:
        flow.hook("sessionStart", START)
        _write_rows(flow.path, TURN_1)
        real_acquire = cursor_flush._acquire

        def busy_upload(uuid, blocking=False, *, observations=False):
            if not observations:
                # Invocation 2 runs while this hook waits on the send.
                _write_rows(flow.path, TURN_1 + TURN_2)
                flow.now = "2026-08-23T18:18:40.000Z"
            return real_acquire(uuid, blocking, observations=observations)

        cursor_flush._acquire = busy_upload
        try:
            flow.hook("sessionEnd", END_1)
        finally:
            cursor_flush._acquire = real_acquire
        assert _stamps_of(flow.calls[-1]) == [START, START, None, END_1]
        flow.hook("sessionEnd", END_2)
        stamps = _stamps_of(flow.calls[-1])
        assert stamps == [START, START, None, END_1, END_1, END_2, END_2]
    print("PASS test_session_end_never_dates_records_written_while_it_waits")


def test_ide_before_submit_prompt_clock_beats_the_minute_tag():
    prompt_1 = "2026-08-23T18:16:31.579Z"
    stop_1 = "2026-08-23T18:16:40.250Z"
    prompt_2 = "2026-08-23T18:18:02.991Z"
    stop_2 = "2026-08-23T18:18:33.425Z"
    with tempfile.TemporaryDirectory() as td, _Flow(Path(td)) as flow:
        # The IDE fires sessionStart when the chat opens, which can be well
        # before the first prompt: 18:15:40 is outside the 18:16 tag's
        # minute, so it dates nothing and the banner keeps its tag.
        flow.hook("sessionStart", "2026-08-23T18:15:40.000Z")
        # beforeSubmitPrompt fires before Cursor writes the prompt (the
        # transcript does not exist yet): its clock is queued, not lost.
        flow.hook("beforeSubmitPrompt", prompt_1, prompt="Reply  once")
        assert len(flow.state()["prompt_clocks"]) == 1
        _write_rows(flow.path, TURN_1)
        flow.hook("stop", stop_1, generation_id=GEN_1, input_tokens=100,
                  output_tokens=10, status="completed")
        assert _stamps_of(flow.calls[-1]) == [ISO_1, prompt_1, None, stop_1]
        assert flow.state()["prompt_clocks"] == []

        # Turn 2's clock waits through a boundary that sees no new prompt,
        # then lands on the prompt whose text it announced.
        flow.hook("beforeSubmitPrompt", prompt_2, prompt="Edit it")
        _write_rows(flow.path, TURN_1 + TURN_2)
        flow.hook("stop", stop_2, generation_id=GEN_2, input_tokens=100,
                  output_tokens=10, status="completed")
        assert _stamps_of(flow.calls[-1]) == [
            ISO_1, prompt_1, None, stop_1, prompt_2, stop_2, stop_2]
        assert flow.state()["prompt_clocks"] == []
    print("PASS test_ide_before_submit_prompt_clock_beats_the_minute_tag")


def test_prompt_clock_only_dates_its_own_prompt():
    with tempfile.TemporaryDirectory() as td, _Flow(Path(td)) as flow:
        # A submit Cursor never wrote (different text) stays queued and
        # dates nothing; the prompt keeps its tag.
        flow.hook("beforeSubmitPrompt", "2026-08-23T18:16:20.000Z",
                  prompt="something else")
        _write_rows(flow.path, TURN_1)
        flow.hook("stop", NOW_1, generation_id=GEN_1, input_tokens=100,
                  output_tokens=10, status="completed")
        assert _stamps_of(flow.calls[-1]) == [ISO_1, ISO_1, None, NOW_1]
        assert len(flow.state()["prompt_clocks"]) == 1
    # Unit: a clock that reads earlier than the prompt's own tag minute is
    # not its clock; an already-pinned prompt is never re-dated.
    records = [_rec("banner", ts=ISO_1, role="user"), {
        "type": "user", "uuid": "ask", "timestamp": ISO_2,
        "message": {"role": "user", "content": "Edit it"}}]
    state = {"prompt_clocks": [{"at": "2026-08-23T18:17:59.000Z",
                                "digest": cursor_flush._prompt_digest("Edit it")}]}
    assert cursor_flush._exact_clocks(records, state, "stop", NOW_2) == ({}, [])
    state["record_ts"] = {"ask": ISO_2}
    state["prompt_clocks"][0]["at"] = NOW_2
    assert cursor_flush._exact_clocks(records, state, "stop", NOW_2) == ({}, [])
    print("PASS test_prompt_clock_only_dates_its_own_prompt")


def test_new_prompt_is_never_dated_before_the_previous_turn():
    def ask(uuid, ts):
        return {"type": "user", "uuid": uuid, "timestamp": ts,
                "message": {"role": "user", "content": uuid}}
    late = "2026-08-23T18:18:40.000Z"
    records = [ask("p1", ISO_1), _rec("a1"), ask("p2", ISO_2), _rec("a2")]
    stamps = cursor_flush._stamp_records(
        records, {"p1": ISO_1, "a1": late}, NOW_2, first_observation=False)
    # p2's tag (18:18:00) predates a1's pin: raised to it, not reordered.
    assert _stamps_of(records) == [ISO_1, late, late, NOW_2]
    assert stamps["p2"] == late
    # Same for a hook clock; and a later-than-floor clock is left alone.
    records = [ask("p1", ISO_1), _rec("a1"), ask("p2", ISO_2)]
    cursor_flush._stamp_records(
        records, {"p1": ISO_1, "a1": late}, NOW_2, first_observation=False,
        clocks={"p2": "2026-08-23T18:18:10.000Z"})
    assert records[2]["timestamp"] == late
    records = [ask("p1", ISO_1), _rec("a1"), ask("p2", ISO_2)]
    cursor_flush._stamp_records(
        records, {"p1": ISO_1, "a1": NOW_1}, NOW_2, first_observation=False,
        clocks={"p2": "2026-08-23T18:18:10.000Z"})
    assert records[2]["timestamp"] == "2026-08-23T18:18:10.000Z"
    # Read-only replay never re-dates: pins and tags ride as they are.
    records = [ask("p1", ISO_1), _rec("a1"), ask("p2", ISO_2)]
    cursor_flush._stamp_records(records, {"a1": late}, None, first_observation=True)
    assert _stamps_of(records) == [ISO_1, late, ISO_2]
    print("PASS test_new_prompt_is_never_dated_before_the_previous_turn")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
