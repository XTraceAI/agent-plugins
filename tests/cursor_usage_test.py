#!/usr/bin/env python3
"""Current Cursor transcript + exact hook-usage capture regressions."""
from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import cursor_flush  # noqa: E402

SESSION = "8af6c93d-7e11-4a47-a6a4-2084d62ed1c9"
GENERATION = "324ce2e2-d685-4fec-9978-c05b9fb907b3"


def _write_transcript(projects: Path) -> Path:
    path = (projects / "fixture" / "agent-transcripts" / SESSION /
            f"{SESSION}.jsonl")
    path.parent.mkdir(parents=True)
    rows = [
        {"role": "user", "message": {"content": [{"type": "text", "text":
            "<timestamp>Sunday, Aug 23, 2026, 11:16 AM (UTC-7)</timestamp>\n"
            "<user_query>\nReply once\n</user_query>"}]}},
        {"role": "assistant", "message": {"content": [
            {"type": "text", "text": "USAGE_OK"}]}},
        {"type": "turn_ended", "status": "success"},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows),
                    encoding="utf-8")
    return path


def _payload(path: Path, event: str) -> dict:
    payload = {
        "hook_event_name": event,
        "session_id": SESSION,
        "conversation_id": SESSION,
        "generation_id": GENERATION,
        "transcript_path": str(path),
        "workspace_roots": [str(path.parent)],
        "model": "cursor-test-model",
    }
    if event in ("afterAgentResponse", "stop"):
        payload.update({
            "input_tokens": 20120,
            "output_tokens": 48,
            "cache_read_tokens": 1024,
            "cache_write_tokens": 9,
            "status": "completed",
        })
    if event == "afterAgentResponse":
        payload["text"] = "USAGE_OK"
    return payload


def _run_main(event: str, payload: dict) -> int:
    old_stdin, old_argv = sys.stdin, sys.argv
    try:
        sys.stdin = io.StringIO(json.dumps(payload))
        sys.argv = ["cursor_flush.py", event]
        return cursor_flush.main()
    finally:
        sys.stdin, sys.argv = old_stdin, old_argv


def test_transcript_path_is_uuid_bound_and_contained():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        projects = root / "projects"
        path = _write_transcript(projects)
        old = cursor_flush._CURSOR_PROJECTS
        cursor_flush._CURSOR_PROJECTS = projects
        try:
            hit, err = cursor_flush._valid_transcript_path(str(path), SESSION)
            assert hit == path.resolve() and not err
            hit, err = cursor_flush._valid_transcript_path(
                str(path.with_name("other.jsonl")), SESSION)
            assert hit is None and "session UUID" in err

            outside = root / "outside" / SESSION / f"{SESSION}.jsonl"
            outside.parent.mkdir(parents=True)
            outside.write_text("{}\n", encoding="utf-8")
            hit, err = cursor_flush._valid_transcript_path(str(outside), SESSION)
            assert hit is None and "under ~/.cursor/projects" in err

            link = (projects / "linked" / "agent-transcripts" / SESSION /
                    f"{SESSION}.jsonl")
            link.parent.mkdir(parents=True)
            try:
                link.symlink_to(outside)
            except OSError:
                pass  # native Windows without symlink privilege
            else:
                hit, err = cursor_flush._valid_transcript_path(str(link), SESSION)
                assert hit is None and "under ~/.cursor/projects" in err
        finally:
            cursor_flush._CURSOR_PROJECTS = old
    print("PASS test_transcript_path_is_uuid_bound_and_contained")


def test_hook_usage_is_exact_and_aborts_remain_unmeasured():
    payload = _payload(Path("/unused"), "afterAgentResponse")
    generation, usage = cursor_flush._hook_usage(
        "afterAgentResponse", payload)
    assert generation == GENERATION
    assert usage == {
        "input_tokens": 20120,
        "output_tokens": 48,
        "cache_read_input_tokens": 1024,
        "cache_creation_input_tokens": 9,
    }
    assert cursor_flush._hook_usage(
        "stop", {"generation_id": GENERATION, "status": "aborted"}) is None
    malformed = dict(payload, input_tokens=True)
    assert cursor_flush._hook_usage("afterAgentResponse", malformed) is None

    records = [{
        "type": "assistant", "uuid": "final-record",
        "message": {"role": "assistant", "content": [
            {"type": "text", "text": "USAGE_OK"}]},
    }]
    events = cursor_flush._usage_events_with(
        {}, generation, "final-record", usage)
    delayed_duplicate = cursor_flush._usage_events_with(
        {"usage_events": events}, generation, "next-turn-record", usage)
    assert delayed_duplicate[generation]["target_uuid"] == "final-record"
    assert cursor_flush._apply_usage(records, events) == {GENERATION}
    assert records[0]["message"]["usage"] == usage
    assert cursor_flush._last_assistant_uuid(records, "USAGE_OK") == "final-record"
    assert cursor_flush._last_assistant_uuid(records, "other") is None
    print("PASS test_hook_usage_is_exact_and_aborts_remain_unmeasured")


def test_transcript_gate_deduplicates_revision_and_usage_generation():
    now = 1_787_000_000.0
    state = {"transcript_revision": "same",
             "sent_usage_generations": [GENERATION],
             "last_flush_at": now - 300}
    assert not cursor_flush.should_flush(
        "stop", {}, state, set(), now,
        source_kind="transcript", source_revision="same")
    assert cursor_flush.should_flush(
        "afterAgentResponse", {}, state, set(), now,
        source_kind="transcript", source_revision="same", usage_pending=True)
    assert cursor_flush.should_flush(
        "sessionEnd", {}, state, set(), now,
        source_kind="transcript", source_revision="new")
    assert not cursor_flush.should_flush(
        "unknown", {}, state, set(), now,
        source_kind="transcript", source_revision="new")
    print("PASS test_transcript_gate_deduplicates_revision_and_usage_generation")


def _main_delivery_case(*, first_send_succeeds: bool) -> tuple[list[dict], dict]:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        projects = root / "projects"
        chats = root / "chats"
        path = _write_transcript(projects)
        calls: list[dict] = []
        originals = {
            "state_dir": cursor_flush.STATE_DIR,
            "projects": cursor_flush._CURSOR_PROJECTS,
            "reader_chats": cursor_flush.cursor_reader._CHATS,
            "reader_projects": cursor_flush.cursor_reader._PROJECTS,
            "flush": cursor_flush._flush,
            "log": cursor_flush._log,
        }

        async def fake_flush(uuid, source_path, blob_ids, flush_mode="now",
                             **kwargs):
            calls.append({
                "uuid": uuid, "source_path": source_path,
                "records": json.loads(json.dumps(kwargs["records"])),
                "revision": kwargs["source_revision"],
                "applied_usage": set(kwargs["applied_usage"]),
            })
            if first_send_succeeds or len(calls) > 1:
                cursor_flush._save_state(
                    uuid, transcript_revision=kwargs["source_revision"],
                    sent_usage_generations=sorted(kwargs["applied_usage"]),
                    last_flush_at=1_787_000_000.0)

        try:
            cursor_flush.STATE_DIR = root / "state"
            cursor_flush._CURSOR_PROJECTS = projects
            cursor_flush.cursor_reader._CHATS = chats
            cursor_flush.cursor_reader._PROJECTS = projects
            cursor_flush._flush = fake_flush
            cursor_flush._log = lambda _message: None

            assert _run_main(
                "afterAgentResponse", _payload(path, "afterAgentResponse")) == 0
            assert len(calls) == 1
            assert _run_main("stop", _payload(path, "stop")) == 0
            assert len(calls) == (1 if first_send_succeeds else 2)
            state = cursor_flush._read_state(SESSION)
            return calls, state
        finally:
            cursor_flush.STATE_DIR = originals["state_dir"]
            cursor_flush._CURSOR_PROJECTS = originals["projects"]
            cursor_flush.cursor_reader._CHATS = originals["reader_chats"]
            cursor_flush.cursor_reader._PROJECTS = originals["reader_projects"]
            cursor_flush._flush = originals["flush"]
            cursor_flush._log = originals["log"]


def test_main_persists_usage_before_send_and_deduplicates_stop():
    calls, state = _main_delivery_case(first_send_succeeds=True)
    final = calls[0]["records"][-1]
    assert final["message"]["content"][0]["text"] == "USAGE_OK"
    assert final["message"]["usage"] == {
        "input_tokens": 20120, "output_tokens": 48,
        "cache_read_input_tokens": 1024,
        "cache_creation_input_tokens": 9,
    }
    assert calls[0]["applied_usage"] == {GENERATION}
    assert list(state["usage_events"]) == [GENERATION]
    assert state["source_kind"] == "transcript"
    assert state["sent_usage_generations"] == [GENERATION]
    print("PASS test_main_persists_usage_before_send_and_deduplicates_stop")


def test_main_retries_same_exact_usage_after_failed_send():
    calls, state = _main_delivery_case(first_send_succeeds=False)
    assert len(calls) == 2
    assert calls[0]["records"][-1]["message"]["usage"] == (
        calls[1]["records"][-1]["message"]["usage"])
    assert state["sent_usage_generations"] == [GENERATION]
    print("PASS test_main_retries_same_exact_usage_after_failed_send")


def test_sticky_transcript_source_never_switches_to_late_store():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        projects = root / "projects"
        transcript = _write_transcript(projects)
        old_projects = cursor_flush._CURSOR_PROJECTS
        old_locate = cursor_flush.cursor_reader.locate
        cursor_flush._CURSOR_PROJECTS = projects
        cursor_flush.cursor_reader.locate = lambda _uuid: (
            root / "late" / "store.db", "")
        try:
            kind, path, err = cursor_flush._source_for(
                SESSION, {}, {"source_kind": "transcript",
                              "transcript_path": str(transcript)})
            assert (kind, path, err) == ("transcript", transcript.resolve(), "")
        finally:
            cursor_flush._CURSOR_PROJECTS = old_projects
            cursor_flush.cursor_reader.locate = old_locate
    print("PASS test_sticky_transcript_source_never_switches_to_late_store")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
