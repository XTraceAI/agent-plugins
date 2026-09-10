#!/usr/bin/env python3
"""Native reader stream: real CLI, existing reader parity and read-only state."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import readers_test as fixtures
from readers import codex, cursor
from readers.discovery import paths
import cursor_flush
import readers_cli

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "plugins/memhub/scripts/readers_cli.py"
SID = "11111111-2222-3333-4444-555555555555"
STAMP = "2026-01-01T00:00:00.123456789012Z"
MTIME = 1788825600


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    os.utime(path, (MTIME, MTIME))
    return path


def rollout(home, surface="Future Desktop"):
    rows = copy.deepcopy(fixtures.CODEX_SYNTH)
    rows[0]["payload"].update(timestamp=STAMP, originator=surface,
                               git={"branch": "synthetic-branch"})
    return write_jsonl(home / ".codex/sessions/2026/01/01/rollout-synthetic.jsonl", rows)


def transcript(home):
    return write_jsonl(home / f".cursor/projects/synthetic/agent-transcripts/{SID}/{SID}.jsonl",
                       fixtures.CURSOR_TRANSCRIPT)


def run(home, host, *arguments):
    guard = home / "guard"
    guard.mkdir(exist_ok=True)
    (guard / "sitecustomize.py").write_text(
        "import builtins, socket\n"
        "original = builtins.__import__\n"
        "def checked(name, *args, **kwargs):\n"
        "    if name == 'mcp' or name.startswith('mcp.'):\n"
        "        raise RuntimeError('reader stream must not import MCP')\n"
        "    return original(name, *args, **kwargs)\n"
        "builtins.__import__ = checked\n"
        "def denied(*args, **kwargs):\n"
        "    raise RuntimeError('reader stream must not connect to a network')\n"
        "socket.socket.connect = denied\n")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
           "XDG_CONFIG_HOME": str(home / ".config"), "CODEX_HOME": str(home / ".codex"),
           "PYTHONPATH": str(guard), "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, str(CLI), "--host", host, *arguments],
                            env=env, text=True, capture_output=True, timeout=20)
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    return result, rows


def normalized(rows):
    result = copy.deepcopy(rows)
    for row in result:
        if row.get("type") == "session":
            row["path"] = "synthetic/" + Path(row["path"]).name
    return result


def test_cli_goldens_and_existing_record_normalization():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        for host, path, reader in [("codex", rollout(home), codex),
                                   ("cursor", transcript(home), cursor)]:
            result, rows = run(home, host)
            assert result.returncode == 0, result.stderr
            assert rows[0]["conversation_id"] == host + "-" + rows[0]["native_session_id"]
            expected, _ = reader.to_canonical(path)
            assert rows[1:] == expected, "CLI must preserve the existing canonical record bytes"
            golden = ROOT / "tests/goldens" / f"readers-cli-{host}.jsonl"
            assert normalized(rows) == [json.loads(line) for line in golden.read_text().splitlines()]
        assert rows[0]["source_surface"] == "cursor-ide"
        assert rows[0]["started_at"] is None, "first message and mtime are not native starts"


def test_metadata_only_does_not_export_prompt_derived_content():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path = rollout(home)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[1]["payload"] = {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "PROMPT_SENTINEL_NOT_METADATA"}]}
        write_jsonl(path, rows)
        transcript(home)
        for host in ("codex", "cursor"):
            result, headers = run(home, host, "--metadata-only")
            assert result.returncode == 0, result.stderr
            assert len(headers) == 1 and headers[0]["type"] == "session"
            assert headers[0]["title"] is None
            assert "PROMPT_SENTINEL_NOT_METADATA" not in result.stdout + result.stderr
            assert "message" not in headers[0]
        result, headers = run(home, "codex", "--metadata-only")
        assert headers[0]["started_at"] == STAMP
        assert headers[0]["git_branch"] == "synthetic-branch"


def test_native_surface_and_start_are_lossless_or_unknown():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        for surface in ["codex_cli", "Future Desktop", None]:
            path = rollout(home, surface)
            result, headers = run(home, "codex", "--metadata-only")
            assert result.returncode == 0, result.stderr
            assert headers[0]["source_surface"] == surface
            assert headers[0]["started_at"] == STAMP
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0].pop("timestamp", None)
        rows[0]["payload"].pop("timestamp", None)
        write_jsonl(path, rows)
        result, headers = run(home, "codex", "--metadata-only")
        assert result.returncode == 0 and headers[0]["started_at"] is None
        outside = write_jsonl(home / f"{SID}.jsonl", fixtures.CURSOR_TRANSCRIPT)
        result, headers = run(home, "cursor", "--session", str(outside), "--metadata-only")
        assert result.returncode == 0 and headers[0]["source_surface"] is None


def test_cursor_restores_saved_pins_without_writing_capture_state():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path = transcript(home)
        before_result, before = run(home, "cursor")
        assert before_result.returncode == 0
        target = next(row for row in reversed(before[1:]) if row["type"] == "assistant")
        assert "usage" not in target["message"]
        state = {"offset": 73, "record_ts": {target["uuid"]: STAMP},
                 "usage_events": cursor_flush._usage_events_with({}, "synthetic-generation", target["uuid"],
                     {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 0})}
        state_path = home / f".config/memhub-plugin/cursorflush/{SID}.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps(state))
        state_bytes, source_bytes = state_path.read_bytes(), path.read_bytes()
        result, after = run(home, "cursor")
        assert result.returncode == 0, result.stderr
        again, replay = run(home, "cursor")
        assert again.returncode == 0 and replay == after
        assert [r["uuid"] for r in before[1:]] == [r["uuid"] for r in after[1:]]
        saved = next(r for r in after[1:] if r["uuid"] == target["uuid"])
        assert saved["timestamp"] == STAMP and saved["message"]["usage"]["output_tokens"] == 3
        assert state_path.read_bytes() == state_bytes and path.read_bytes() == source_bytes
        assert list(state_path.parent.iterdir()) == [state_path], "reader cannot create capture locks/cursors"
        os.utime(state_path, (MTIME + 100, MTIME + 100))
        result, recent = run(home, "cursor", "--since", "2026-09-08T00:01:00Z")
        assert result.returncode == 0 and len(recent) == len(after), result.stderr
        assert recent[0]["mtime"] == MTIME + 100
        assert recent[1:] == after[1:], "pin-only changes must survive the mtime prefilter"
        os.utime(state_path, (MTIME, MTIME))
        result, older = run(home, "cursor", "--since", "2026-09-08T00:01:00Z")
        assert result.returncode == 0 and older == []


def test_store_header_and_since_include_wal_observations():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path = fixtures._make_cursor_store(home / ".cursor/chats")
        meta_path = path.parent / "meta.json"
        meta = json.loads(meta_path.read_text());meta["source_surface"] = "Novel Cursor Surface"
        meta_path.write_text(json.dumps(meta))
        os.utime(path, (MTIME - 100, MTIME - 100));os.utime(meta_path, (MTIME - 100, MTIME - 100))
        wal = path.with_name("store.db-wal");wal.write_bytes(b"synthetic mtime observation")
        os.utime(wal, (MTIME + 100, MTIME + 100))
        result, headers = run(home, "cursor", "--metadata-only", "--since", "2026-09-08T00:00:00Z")
        assert result.returncode == 0 and len(headers) == 1, result.stderr
        assert headers[0]["mtime"] == MTIME + 100
        assert headers[0]["source_surface"] == "Novel Cursor Surface"
        assert headers[0]["started_at"] == cursor._iso_ms(meta["createdAtMs"])
        wal.unlink()
        result, headers = run(home, "cursor", "--metadata-only", "--since", "2026-09-08T00:00:00Z")
        assert result.returncode == 0 and headers == []
        result, rows = run(home, "cursor")
        assert result.returncode == 0, result.stderr
        expected, _ = cursor.to_canonical(path)
        assert rows[1:] == expected


def test_missing_unreadable_and_incomplete_discovery_are_not_empty_success():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        for host in ("codex", "cursor"):
            result, rows = run(home, host, "--metadata-only")
            assert result.returncode == 2 and rows == []
            assert "discovery_incomplete" in result.stderr
        path = rollout(home)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]["payload"].pop("id")
        write_jsonl(path, rows)
        result, rows = run(home, "codex", "--metadata-only")
        assert result.returncode == 2 and rows == [] and "session_unreadable" in result.stderr
        errors = []
        with patch("os.scandir", side_effect=PermissionError("synthetic access failure")):
            assert paths(home, ("**", "*.jsonl"), errors.append) == []
        assert errors and isinstance(errors[0], PermissionError)
        link = home / ".codex/sessions/linked"
        link.symlink_to(home / ".codex/sessions", target_is_directory=True)
        result, _ = run(home, "codex", "--metadata-only")
        assert result.returncode == 2 and "discovery_incomplete" in result.stderr


def test_bad_store_metadata_keeps_healthy_peer_sessions():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path = fixtures._make_cursor_store(home / ".cursor/chats", uuid="bad-store")
        (path.parent / "meta.json").write_text('{"updatedAtMs":"bad value"}')
        transcript(home)
        result, rows = run(home, "cursor", "--metadata-only")
        assert result.returncode == 2 and "session_unreadable" in result.stderr
        assert [row["native_session_id"] for row in rows] == [SID]


def test_reader_errors_never_emit_partial_session_records():
    with tempfile.TemporaryDirectory() as td:
        path = rollout(Path(td))
        original = codex.to_canonical

        def malformed(source):
            records, native = original(source)
            records[-1]["synthetic_invalid_number"] = float("nan")
            return records, native

        def changing(source):
            result = original(source)
            with source.open("a") as handle:
                handle.write('{}\n')
            return result

        for read, code in [(malformed, "session_unreadable"), (changing, "source_changed")]:
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(codex, "to_canonical", side_effect=read), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = readers_cli.main(["--host", "codex", "--session", str(path)])
            assert status == 2 and output.getvalue() == "" and code in errors.getvalue()


def test_store_paths_with_uri_characters_remain_read_only():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "native stores #1?"
        path = fixtures._make_cursor_store(home / ".cursor/chats")
        before = path.read_bytes()
        result, rows = run(home, "cursor")
        assert result.returncode == 0 and len(rows) > 1, result.stderr
        assert path.read_bytes() == before


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASS")
