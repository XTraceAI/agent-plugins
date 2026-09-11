"""Local Cursor observations remain recoverable without a successful upload."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import cursor_usage_test as fixtures
import cursor_flush
import portable_lock


def test_busy_upload_skips_ordinary_edits_and_shell_before_observation():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        projects = root / "projects"
        path = fixtures._write_transcript(projects)
        state_dir = root / "state"
        state_dir.mkdir()
        fd = os.open(state_dir / f"{fixtures.SESSION}.flush.lock", os.O_CREAT | os.O_RDWR, 0o600)
        portable_lock.lock_exclusive(fd, blocking=False)
        sent = []

        async def fake_flush(*args, **kwargs):
            sent.append(kwargs["records"])

        with patch.multiple(cursor_flush, STATE_DIR=state_dir, _CURSOR_PROJECTS=projects,
                            LOCK_WAIT_S=0.01, _flush=fake_flush, _log=lambda _: None), \
             patch.multiple(cursor_flush.cursor_reader, _PROJECTS=projects, _CHATS=root / "chats"), \
             patch.object(cursor_flush.cursor_reader, "to_canonical",
                          wraps=cursor_flush.cursor_reader.to_canonical) as reader:
            try:
                for event in ("afterFileEdit", "beforeShellExecution"):
                    payload = dict(fixtures._payload(path, event), command="git commit -m fixture")
                    assert cursor_flush._event_can_flush(event, payload)
                    for _ in range(3):
                        assert fixtures._run_main(event, payload) == 0
                assert reader.call_count == 0, "busy ordinary events parsed the source"
                assert not (state_dir / f"{fixtures.SESSION}.observations.lock").exists()
                assert not cursor_flush._state_path(fixtures.SESSION).exists()
                assert not sent
            finally:
                os.close(fd)
            assert fixtures._run_main("afterFileEdit", fixtures._payload(path, "afterFileEdit")) == 0
            assert reader.call_count == 1 and len(sent) == 1


def test_busy_upload_keeps_usage_and_restores_it_after_restart():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        projects = root / "projects"
        path = fixtures._write_transcript(projects)
        original = path.read_bytes()
        state_dir = root / "state"
        state_dir.mkdir()
        fd = os.open(state_dir / f"{fixtures.SESSION}.flush.lock", os.O_CREAT | os.O_RDWR, 0o600)
        portable_lock.lock_exclusive(fd, blocking=False)
        sent = []

        async def fake_flush(uuid, _path, _blobs, _mode="now", **kwargs):
            sent.append(kwargs["records"])
            cursor_flush._save_state(uuid, transcript_revision=kwargs["source_revision"],
                                     sent_usage_generations=sorted(kwargs["applied_usage"]))

        with patch.multiple(cursor_flush, STATE_DIR=state_dir, _CURSOR_PROJECTS=projects,
                            LOCK_WAIT_S=0.01, _flush=fake_flush, _log=lambda _: None), \
             patch.multiple(cursor_flush.cursor_reader, _PROJECTS=projects, _CHATS=root / "chats"):
            try:
                assert fixtures._run_main("afterAgentResponse", fixtures._payload(path, "afterAgentResponse")) == 0
                saved = cursor_flush._read_state(fixtures.SESSION)
                assert fixtures.GENERATION in saved.get("usage_events", {}), "busy upload lost exact usage"
                assert not sent
            finally:
                os.close(fd)
            records, _ = cursor_flush.cursor_reader.to_canonical(path, session_id=fixtures.SESSION)
            before = cursor_flush._state_path(fixtures.SESSION).read_bytes()
            cursor_flush.apply_session_state(records, fixtures.SESSION)
            assert records[-1]["message"]["usage"]["output_tokens"] == 48
            assert cursor_flush._state_path(fixtures.SESSION).read_bytes() == before
            # A later hook need not repeat usage for the existing uploader to recover.
            assert fixtures._run_main("stop", {"session_id": fixtures.SESSION}) == 0
            assert len(sent) == 1 and sent[0][-1]["message"]["usage"]["output_tokens"] == 48
            assert fixtures._run_main("stop", {"session_id": fixtures.SESSION}) == 0
            assert len(sent) == 1
        assert path.read_bytes() == original


def test_historical_usage_survives_513_records_and_duplicate_generation():
    with tempfile.TemporaryDirectory() as td, patch.object(cursor_flush, "STATE_DIR", Path(td)):
        usage = {"input_tokens": 10, "output_tokens": 2,
                 "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        state = {}
        for i in range(513):
            state["usage_events"] = cursor_flush._usage_events_with(
                state, f"generation-{i}", f"record-{i}", usage)
        state["usage_events"] = cursor_flush._usage_events_with(
            state, "generation-0", "wrong-delayed-target", usage)
        cursor_flush._save_state(fixtures.SESSION, **state)
        before = cursor_flush._state_path(fixtures.SESSION).read_bytes()
        records = [{"type": "assistant", "uuid": f"record-{i}",
                    "message": {"role": "assistant", "content": []}} for i in range(513)]
        cursor_flush.apply_session_state(records, fixtures.SESSION)
        assert sum("usage" in row["message"] for row in records) == 513
        assert len(cursor_flush._read_state(fixtures.SESSION)["usage_events"]) == 513
        assert cursor_flush._state_path(fixtures.SESSION).read_bytes() == before


_CHILD = r'''
import asyncio, json, os, socket, sys, time
from pathlib import Path
import cursor_flush
role = sys.argv[1]
sys.argv = ["cursor_flush.py", "stop"]
cursor_flush.LOCK_WAIT_S = 0.05
control = Path.home() / "control"
acquire = cursor_flush._acquire
def acquire_after_pause(uuid, blocking=False, **kwargs):
    if role == "lagging" and not kwargs.get("observations"):
        (control / "waiting").touch()
        deadline = time.monotonic() + 8
        while not (control / "resume").exists():
            assert time.monotonic() < deadline, "test did not resume upload"
            time.sleep(0.01)
    return acquire(uuid, blocking=blocking, **kwargs)
cursor_flush._acquire = acquire_after_pause
read_source = cursor_flush.cursor_reader.to_canonical
reads = 0
def counted_read(*args, **kwargs):
    global reads
    reads += 1
    (control / (role + ".reads")).write_text(str(reads))
    return read_source(*args, **kwargs)
cursor_flush.cursor_reader.to_canonical = counted_read
def forbidden(*args, **kwargs):
    raise AssertionError("network forbidden")
socket.socket.connect = forbidden
async def upload(uuid, source_path, blob_ids, mode="now", **kwargs):
    (control / (role + ".json")).write_text(json.dumps(kwargs["records"]))
    if role == "first":
        deadline = time.monotonic() + 8
        while not (control / "release").exists():
            assert time.monotonic() < deadline, "test did not release upload"
            await asyncio.sleep(0.01)
    cursor_flush._save_state(uuid, transcript_revision=kwargs["source_revision"],
        sent_usage_generations=sorted(kwargs["applied_usage"]), last_flush_at=time.time())
cursor_flush._flush = upload
raise SystemExit(cursor_flush.main())
'''


def test_slow_upload_preserves_a_concurrent_generation_until_later_delivery():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        control = home / "control"
        control.mkdir()
        path = fixtures._write_transcript(home / ".cursor/projects")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
               "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
               "PYTHONPATH": str(fixtures.ROOT / "plugins/memhub/scripts"),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}

        def command(role):
            return [sys.executable, "-c", _CHILD, role]

        def run(role, payload):
            result = subprocess.run(command(role), input=json.dumps(payload), env=env,
                                    capture_output=True, text=True, timeout=8)
            assert result.returncode == 0, result.stdout + result.stderr

        first = subprocess.Popen(command("first"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=env)
        try:
            first.stdin.write(json.dumps(fixtures._payload(path, "stop")))
            first.stdin.close()
            first.stdin = None
            deadline = time.monotonic() + 5
            while not (control / "first.json").exists():
                assert first.poll() is None, first.stderr.read()
                assert time.monotonic() < deadline, "first upload never started"
                time.sleep(0.01)
            with path.open("a") as output:
                for role, text in [("user", "second request"), ("assistant", "second reply")]:
                    output.write(json.dumps({"role": role, "message": {"content": [
                        {"type": "text", "text": text}]}}) + "\n")
            generation = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
            payload = dict(fixtures._payload(path, "stop"), generation_id=generation, output_tokens=17)
            original = path.read_bytes()
            run("second", payload)
            state_path = home / f".config/memhub-plugin/cursorflush/{fixtures.SESSION}.json"
            state = json.loads(state_path.read_text())
            assert set(state["usage_events"]) == {fixtures.GENERATION, generation}
            assert state["usage_events"][generation]["usage"]["output_tokens"] == 17
            assert not (control / "second.json").exists(), "upload lock was bypassed"
            pins = state["record_ts"]
            (control / "release").touch()
            stdout, stderr = first.communicate(timeout=5)
            assert first.returncode == 0, stdout + stderr
            assert json.loads(state_path.read_text())["record_ts"] == pins
            run("recovery", {"session_id": fixtures.SESSION})
            restored = json.loads((control / "recovery.json").read_text())
            assert [row["message"]["usage"]["output_tokens"] for row in restored
                    if row.get("message", {}).get("usage")] == [48, 17]
            run("duplicate", payload)
            assert not (control / "duplicate.json").exists()
            assert json.loads(state_path.read_text())["record_ts"] == pins
            assert (control / "first.reads").read_text() == "1"
            assert (control / "second.reads").read_text() == "1"
            assert path.read_bytes() == original
        finally:
            (control / "release").touch()
            if first.poll() is None:
                first.communicate(timeout=10)


def test_waiting_older_hook_cannot_upload_a_stale_snapshot_after_newer_hook():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        control = home / "control"
        control.mkdir()
        path = fixtures._write_transcript(home / ".cursor/projects")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
               "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
               "PYTHONPATH": str(fixtures.ROOT / "plugins/memhub/scripts"),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
        lagging = subprocess.Popen([sys.executable, "-c", _CHILD, "lagging"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env=env)
        try:
            lagging.stdin.write(json.dumps(fixtures._payload(path, "stop")))
            lagging.stdin.close()
            lagging.stdin = None
            deadline = time.monotonic() + 5
            while not (control / "waiting").exists():
                assert lagging.poll() is None, lagging.stderr.read()
                assert time.monotonic() < deadline
                time.sleep(0.01)
            with path.open("a") as output:
                for role in ["user", "assistant"]:
                    output.write(json.dumps({"role": role, "message": {"content": [
                        {"type": "text", "text": "new " + role}]}}) + "\n")
            newer_payload = dict(fixtures._payload(path, "stop"),
                                 model="newer-model",
                                 generation_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
            newer = subprocess.run([sys.executable, "-c", _CHILD, "newer"],
                                   input=json.dumps(newer_payload), capture_output=True,
                                   text=True, env=env, timeout=8)
            assert newer.returncode == 0, newer.stdout + newer.stderr
            state_path = home / f".config/memhub-plugin/cursorflush/{fixtures.SESSION}.json"
            before = state_path.read_bytes()
            (control / "resume").touch()
            stdout, stderr = lagging.communicate(timeout=5)
            assert lagging.returncode == 0, stdout + stderr
            assert (control / "newer.json").exists()
            assert not (control / "lagging.json").exists()
            assert state_path.read_bytes() == before
        finally:
            (control / "resume").touch()
            if lagging.poll() is None:
                lagging.communicate(timeout=10)


if __name__ == "__main__":
    for name, fn in sorted(globals().copy().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASS")
