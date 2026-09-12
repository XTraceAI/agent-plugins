#!/usr/bin/env python3
"""Native reader stream: real CLI, existing reader parity and read-only state."""
from __future__ import annotations

import pathlib
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
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
GENERATION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
STAMP = "2026-01-01T00:00:00.123456789012Z"
MTIME = 1788825600
SYSTEM_ENV = {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}


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


def run(home, host, *arguments, cwd=None):
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
    env = {**SYSTEM_ENV, "PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home),
           "XDG_CONFIG_HOME": str(home / ".config"), "CODEX_HOME": str(home / ".codex"),
           "PYTHONPATH": str(guard), "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, str(CLI), "--host", host, *arguments],
                            cwd=cwd, env=env, text=True, capture_output=True, timeout=20)
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
        state = {"source_kind": "transcript", "transcript_path": str(path),
                 "offset": 73, "record_ts": {target["uuid"]: STAMP},
                 "usage_events": cursor_flush._usage_events_with({}, GENERATION_ID, target["uuid"],
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

        def malformed(source, **kwargs):
            records, native = original(source, **kwargs)
            records[-1]["synthetic_invalid_number"] = float("nan")
            return records, native

        def changing(source, **kwargs):
            # The reader consumes a private snapshot; a write to the native
            # rollout during the read must still surface as source_changed.
            assert source != path
            result = original(source, **kwargs)
            with path.open("a") as handle:
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
        home = Path(td) / "native stores #1%2"
        path = fixtures._make_cursor_store(home / ".cursor/chats")
        before = path.read_bytes()
        result, rows = run(home, "cursor")
        assert result.returncode == 0 and len(rows) > 1, result.stderr
        assert path.read_bytes() == before


def test_invalid_utf8_is_incomplete_without_changing_legacy_reader_tolerance():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path = rollout(home)
        lines = path.read_bytes().splitlines(keepends=True)
        padding = json.dumps({"type": "synthetic-padding", "value": "x" * 9000}).encode() + b"\n"
        path.write_bytes(lines[0] + padding + b"".join(lines[1:]).replace(b"On it.", b"bad\xfftext"))
        legacy, _ = codex.to_canonical(path)
        assert "\ufffd" in json.dumps(legacy, ensure_ascii=False)
        result, rows = run(home, "codex")
        assert result.returncode == 2 and rows == [] and "session_unreadable" in result.stderr
        assert "bad" not in result.stderr and "Traceback" not in result.stderr
        store = fixtures._make_cursor_store(home / ".cursor/chats")
        with contextlib.closing(sqlite3.connect(store)) as connection, connection:
            identity, data = next((key, value) for key, value in connection.execute("SELECT id,data FROM blobs")
                                  if isinstance(value, bytes) and b'"role": "assistant"' in value)
            message = json.loads(data)
            message["content"] = [{"type": "text", "text": "badXtext"}]
            invalid = json.dumps(message).encode().replace(b"badXtext", b"bad\xfftext")
            connection.execute("UPDATE blobs SET data=? WHERE id=?", (invalid, identity))
        legacy, _ = cursor.to_canonical(store)
        assert "\ufffd" in json.dumps(legacy, ensure_ascii=False)
        result, rows = run(home, "cursor")
        assert result.returncode == 2 and rows == [] and "session_unreadable" in result.stderr


def test_oversized_native_start_keeps_healthy_peer_sessions():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path = fixtures._make_cursor_store(home / ".cursor/chats", uuid="bad-start")
        meta_path = path.parent / "meta.json"
        meta = json.loads(meta_path.read_text());meta["createdAtMs"] = 10 ** 400
        meta_path.write_text(json.dumps(meta))
        transcript(home)
        result, rows = run(home, "cursor", "--metadata-only")
        assert result.returncode == 2 and "session_unreadable" in result.stderr
        assert [row["native_session_id"] for row in rows] == [SID]
        assert "Traceback" not in result.stderr


def test_wal_snapshot_reads_latest_records_without_native_sidecar_writes():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path = fixtures._make_cursor_store(home / ".cursor/chats")
        script = '''import hashlib,json,os,sqlite3,sys
con=sqlite3.connect(sys.argv[1])
con.execute("PRAGMA journal_mode=WAL")
con.execute("PRAGMA wal_autocheckpoint=0")
body=json.dumps({"role":"assistant","content":[{"type":"text","text":"WAL_ONLY_REPLY"}]}).encode()
identity=hashlib.sha256(body).hexdigest()
con.execute("INSERT INTO blobs(id,data) VALUES (?,?)",(identity,body))
con.execute("UPDATE meta SET value=?",(json.dumps({"latestRootBlobId":identity}),))
con.commit()
os._exit(0)
'''
        result = subprocess.run([sys.executable, "-c", script, str(path)],
                                env={**SYSTEM_ENV, "HOME": td, "USERPROFILE": td}, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
        wal = path.with_name("store.db-wal")
        assert wal.exists()
        path.with_name("store.db-shm").unlink(missing_ok=True)
        files = list(path.parent.iterdir())
        before = {file.name: file.read_bytes() for file in files}
        for file in files:
            file.chmod(0o400)
        path.parent.chmod(0o500)
        try:
            result, rows = run(home, "cursor")
            assert result.returncode == 0 and "WAL_ONLY_REPLY" in result.stdout, result.stderr
            again, replay = run(home, "cursor")
            assert again.returncode == 0 and replay == rows
            assert {file.name: file.read_bytes() for file in path.parent.iterdir()} == before
        finally:
            path.parent.chmod(0o700)
            for file in path.parent.iterdir():
                file.chmod(0o600)


def test_hot_rollback_journal_recovers_only_the_private_snapshot():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path = fixtures._make_cursor_store(home / ".cursor/chats")
        expected, _ = cursor.to_canonical(path)
        script = '''import os,sqlite3,sys
con=sqlite3.connect(sys.argv[1])
con.execute("PRAGMA journal_mode=DELETE")
con.execute("PRAGMA cache_size=2")
con.execute("BEGIN IMMEDIATE")
con.execute("DELETE FROM blobs")
for number in range(200):
    con.execute("INSERT INTO blobs(id,data) VALUES (?,?)",(str(number),b"uncommitted"*1000))
os._exit(0)
'''
        result = subprocess.run([sys.executable, "-c", script, str(path)],
                                env={**SYSTEM_ENV, "HOME": td, "USERPROFILE": td}, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
        journal = path.with_name("store.db-journal")
        assert journal.exists() and journal.read_bytes()[:8] != b"\0" * 8
        before = {file.name: file.read_bytes() for file in path.parent.iterdir()}
        result, rows = run(home, "cursor")
        assert result.returncode == 0 and rows[1:] == expected, result.stderr
        again, replay = run(home, "cursor")
        assert again.returncode == 0 and replay == rows
        assert {file.name: file.read_bytes() for file in path.parent.iterdir()} == before


def test_cursor_unterminated_tail_rejects_decoding_errors_only_in_strict_mode():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=transcript(home)
        expected,_=cursor.to_canonical(path)
        original=path.read_bytes()
        path.write_bytes(original + b'{"role":"assistant","message":{"content":"bad\xfftext"}}')
        legacy,_=cursor.to_canonical(path)
        assert legacy==expected
        result,rows=run(home,"cursor")
        assert result.returncode==2 and rows==[] and "session_unreadable" in result.stderr
        assert "bad" not in result.stderr and "Traceback" not in result.stderr
        path.write_bytes(original + b'{"role":"assistant","message":')
        result,rows=run(home,"cursor")
        assert result.returncode==0 and rows[1:]==expected, result.stderr


def test_duplicate_native_identities_are_excluded_before_any_session_is_emitted():
    import shutil
    for host in ("codex","cursor"):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            if host == "cursor":
                original=fixtures._make_cursor_store(home/".cursor/chats")
                duplicate=home/".cursor/chats/another-workspace"/original.parent.name
                shutil.copytree(original.parent,duplicate)
                healthy=fixtures._make_cursor_store(home/".cursor/chats",uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
            else:
                original=rollout(home)
                duplicate=original.with_name("rollout-another.jsonl")
                shutil.copyfile(original,duplicate)
                rows=[json.loads(line) for line in original.read_text().splitlines()]
                rows[0]["payload"].update(id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",timestamp=STAMP)
                healthy=write_jsonl(original.with_name("rollout-healthy.jsonl"),rows)
            for mode in ([],["--metadata-only"]):
                result,rows=run(home,host,*mode)
                assert result.returncode==2 and "discovery_incomplete" in result.stderr
                headers=[row for row in rows if row.get("type")=="session"]
                assert len(headers)==1 and headers[0]["path"]==str(healthy.resolve())
            native_id=(codex.session_metadata(original)["session_id"] if host=="codex" else original.parent.name)
            result,rows=run(home,host,"--session",native_id,"--metadata-only")
            assert result.returncode==2 and rows==[] and "discovery_incomplete" in result.stderr,result.stderr
            # Latest retains native ordering, but cannot hide another copy of
            # its actual identity. Both metadata and full reads fail closed.
            os.utime(original,(MTIME+100,MTIME+100))
            if host=="cursor":
                meta_path=original.parent/"meta.json";meta=json.loads(meta_path.read_text())
                meta["updatedAtMs"]=int((MTIME+100)*1000);meta_path.write_text(json.dumps(meta))
                healthy_meta=healthy.parent/"meta.json";meta=json.loads(healthy_meta.read_text())
                meta["updatedAtMs"]=int(MTIME*1000);healthy_meta.write_text(json.dumps(meta))
                duplicate_meta=duplicate/"meta.json";meta=json.loads(duplicate_meta.read_text())
                meta["updatedAtMs"]=int(MTIME*1000);duplicate_meta.write_text(json.dumps(meta))
            for mode in ([],["--metadata-only"]):
                result,rows=run(home,host,"--session","latest",*mode)
                assert result.returncode==2 and rows==[] and "discovery_incomplete" in result.stderr,result.stderr
            if host=="cursor":
                # A malformed copy still owns the same path-derived identity.
                (duplicate/"meta.json").write_text("{broken}")
                for selection in ([],["--session",native_id],["--session","latest"]):
                    for mode in ([],["--metadata-only"]):
                        result,rows=run(home,host,*selection,*mode)
                        assert result.returncode==2,result.stderr
                        assert all(row.get("native_session_id")!=native_id for row in rows if row.get("type")=="session")
                result,rows=run(home,host,"--session",str(original))
                assert result.returncode==0 and rows[0]["native_session_id"]==native_id,result.stderr
                shutil.rmtree(duplicate)
            else:duplicate.unlink()
            result,rows=run(home,host,"--session","latest")
            assert result.returncode==0 and rows[0]["path"]==str(original.resolve()),result.stderr
            result,rows=run(home,host,"--session",native_id,"--metadata-only")
            assert result.returncode==0 and rows[0]["native_session_id"]==native_id,result.stderr
            # An explicit native path remains an unambiguous request.
            result,rows=run(home,host,"--session",str(original),"--metadata-only")
            assert result.returncode==0 and len(rows)==1,result.stderr


def test_codex_malformed_header_still_counts_toward_duplicate_identity():
    for field,value in [('timestamp','not-a-date'),('cwd',17),('originator',False),('git',{'branch':17})]:
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);original=rollout(home)
            records=[json.loads(line) for line in original.read_text().splitlines()]
            native_id=records[0]['payload']['id']
            records[0]['payload'][field]=value
            duplicate=write_jsonl(original.with_name('rollout-malformed-copy.jsonl'),records)
            healthy_records=[json.loads(line) for line in original.read_text().splitlines()]
            healthy_records[0]['payload']['id']='independent-native-id'
            healthy=write_jsonl(original.with_name('rollout-healthy-peer.jsonl'),healthy_records)
            os.utime(original,(MTIME+100,MTIME+100))
            before={p:p.read_bytes() for p in (original,duplicate,healthy)}
            for selection in ([],['--session',native_id],['--session','latest']):
                for mode in ([],['--metadata-only']):
                    result,rows=run(home,'codex',*selection,*mode)
                    headers=[row for row in rows if row.get('type')=='session']
                    assert result.returncode==2 and 'discovery_incomplete' in result.stderr,result.stderr
                    assert all(row['native_session_id']!=native_id for row in headers),headers
                    assert len(headers)==(0 if selection else 1)
            result,rows=run(home,'codex','--session',str(original),'--metadata-only')
            assert result.returncode==0 and rows[0]['native_session_id']==native_id,result.stderr
            assert all(p.read_bytes()==raw for p,raw in before.items())


def test_complete_malformed_native_json_fails_while_unfinished_codex_tail_waits():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=rollout(home);original=path.read_bytes()
        expected,_=codex.to_canonical(path)
        for malformed in (b'{"type":broken}\n',b'null\n',b'17\n'):
            path.write_bytes(original+malformed)
            legacy,_=codex.to_canonical(path)
            assert legacy==expected
            result,rows=run(home,"codex")
            assert result.returncode==2 and rows==[] and "session_unreadable" in result.stderr
            assert "broken" not in result.stderr
        path.write_bytes(original+b'{"type":"response_item","payload":')
        result,rows=run(home,"codex")
        assert result.returncode==0 and rows[1:]==expected,result.stderr
        path.write_bytes(original+b'{"type":"future_extension"}')
        result,rows=run(home,"codex")
        assert result.returncode==0 and rows[1:]==expected,result.stderr
        store=fixtures._make_cursor_store(home/".cursor/chats")
        with contextlib.closing(sqlite3.connect(store)) as db, db:
            leaf=db.execute("SELECT id FROM blobs WHERE substr(data,1,1)=? LIMIT 1",(b'{',)).fetchone()[0]
            db.execute("UPDATE blobs SET data=? WHERE id=?",(b'{"role":broken}',leaf))
        cursor.to_canonical(store)  # Legacy capture remains tolerant.
        result,rows=run(home,"cursor")
        assert result.returncode==2 and rows==[] and "session_unreadable" in result.stderr


def test_codex_title_sidecar_changes_participate_in_since_and_revision_checks():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=rollout(home)
        original=[json.loads(line) for line in path.read_text().splitlines()]
        sid=original[0]["payload"]["id"]
        index=write_jsonl(home/".codex/session_index.jsonl",[{"id":sid,"thread_name":"Native title"}])
        os.utime(index,(MTIME+100,MTIME+100))
        result,rows=run(home,"codex","--since","2026-09-08T00:01:00Z")
        assert result.returncode==0 and rows[0]["mtime"]==MTIME+100,result.stderr
        assert rows[0]["title"]=="Native title"
        write_jsonl(index,[{"id":sid,"thread_name":"Renamed title"}]);os.utime(index,(MTIME+200,MTIME+200))
        result,rows=run(home,"codex","--since","2026-09-08T00:02:00Z")
        assert result.returncode==0 and rows[0]["title"]=="Renamed title"
        real=codex.to_canonical
        def changing(*args,**kwargs):
            value=real(*args,**kwargs)
            index.write_text(index.read_text()+json.dumps({"id":sid,"thread_name":"Changed during read"})+"\n")
            return value
        stdout,stderr=io.StringIO(),io.StringIO()
        with patch.object(codex,"_SESSION_INDEX",index), patch.object(codex,"to_canonical",side_effect=changing), \
                contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            code=readers_cli.main(["--host","codex","--session",str(path)])
        assert code==2 and stdout.getvalue()=="" and "source_changed" in stderr.getvalue()


def test_existing_malformed_cursor_pins_are_incomplete_but_missing_is_optional():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=transcript(home)
        state=home/f".config/memhub-plugin/cursorflush/{SID}.json";state.parent.mkdir(parents=True)
        for invalid in ['{truncated','[]','null','{"record_ts":[]}','{"usage_events":17}',
                        '{"record_ts":{"record":"not a date"}}',
                        '{"usage_events":{"generation":{}}}']:
            state.write_text(invalid);before=state.read_bytes()
            result,rows=run(home,"cursor")
            assert result.returncode==2 and rows==[] and "session_unreadable" in result.stderr
            assert "truncated" not in result.stderr and state.read_bytes()==before
        target=next(row['uuid'] for row in reversed(cursor.to_canonical(path)[0])
                    if row.get('type')=='assistant')
        state.write_text(json.dumps({'record_ts':{target:None},'usage_events':{}}))
        result,rows=run(home,"cursor")
        assert result.returncode==0 and rows,result.stderr
        state.unlink()
        result,rows=run(home,"cursor")
        assert result.returncode==0 and rows[1:]==cursor.to_canonical(path)[0],result.stderr


def test_codex_title_sidecar_is_strict_only_for_full_discovery_reads():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=rollout(home)
        sid=json.loads(path.read_text().splitlines()[0])["payload"]["id"]
        index=home/".codex/session_index.jsonl"
        for content in [json.dumps({"id":sid,"thread_name":"bad"}).encode().replace(b'bad',b'bad\xff'),b'{bad}\n']:
            index.write_bytes(content)
            with patch.object(codex,"_SESSION_INDEX",index):
                codex.to_canonical(path)  # Ordinary capture remains tolerant.
            result,rows=run(home,"codex")
            assert result.returncode==2 and rows==[] and "session_unreadable" in result.stderr
            assert "bad" not in result.stderr
        index.write_text(json.dumps({"id":sid,"thread_name":"complete title"})+'\n{"id":')
        result,rows=run(home,"codex")
        assert result.returncode==0 and rows[0]["title"]=="complete title",result.stderr


def test_complete_cursor_non_object_rows_are_incomplete_not_silently_skipped():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=transcript(home);original=path.read_bytes()
        for row in [b'null\n',b'17\n',b'[]\n',b'null',b'{"role":"user","message":null}\n']:
            path.write_bytes(original+row);before=path.read_bytes()
            result,rows=run(home,"cursor")
            assert result.returncode==2 and rows==[] and "session_unreadable" in result.stderr
            assert path.read_bytes()==before and "Traceback" not in result.stderr
        path.write_bytes(original+b'{"unfinished":')
        result,rows=run(home,"cursor")
        assert result.returncode==0 and rows[1:]==cursor.to_canonical(path)[0]


def test_cursor_pins_follow_the_saved_representation_and_reject_mismatched_paths():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=transcript(home)
        _,before=run(home,"cursor")
        target=next(row for row in reversed(before[1:]) if row["type"]=="assistant")
        state_path=home/f".config/memhub-plugin/cursorflush/{SID}.json";state_path.parent.mkdir(parents=True)
        state={"source_kind":"transcript","transcript_path":str(path),
               "record_ts":{target["uuid"]:STAMP},"usage_events":cursor_flush._usage_events_with(
                   {},GENERATION_ID,target["uuid"],{"input_tokens":7,"output_tokens":3})}
        state_path.write_text(json.dumps(state));saved=state_path.read_bytes()
        store=fixtures._make_cursor_store(home/".cursor/chats",uuid=SID)
        original={p:p.read_bytes() for p in [path,store,store.parent/"meta.json"]}
        result,rows=run(home,"cursor")
        assert result.returncode==0 and rows[0]["path"]==str(path.resolve()),result.stderr
        assistant=next(row for row in rows[1:] if row["uuid"]==target["uuid"])
        assert assistant["timestamp"]==STAMP and assistant["message"]["usage"]["output_tokens"]==3
        by_id,selected=run(home,"cursor","--session",SID)
        assert by_id.returncode==0 and selected[1:]==rows[1:],by_id.stderr
        result,explicit=run(home,"cursor","--session",str(path))
        assert result.returncode==0 and explicit[1:]==rows[1:],result.stderr
        result,wrong=run(home,"cursor","--session",str(store))
        assert result.returncode==2 and wrong==[] and "session_unreadable" in result.stderr
        assert state_path.read_bytes()==saved and all(p.read_bytes()==content for p,content in original.items())
        for source_kind,recorded_path in [("transcript",str(home/"missing.jsonl")),("unknown",str(path)),(None,None)]:
            state.update(source_kind=source_kind,transcript_path=recorded_path);state_path.write_text(json.dumps(state))
            result,rows=run(home,"cursor")
            assert result.returncode==2 and rows==[],(source_kind,result.stderr)
        state.update(source_kind="store");state_path.write_text(json.dumps(state))
        result,rows=run(home,"cursor","--session",str(path))
        assert result.returncode==2 and rows==[]
        result,rows=run(home,"cursor","--session",str(store))
        assert result.returncode==0 and rows,result.stderr
        duplicate=fixtures._make_cursor_store(home/"duplicate",uuid=SID)
        alternate=home/f".cursor/chats/other-workspace/{SID}";alternate.parent.mkdir()
        duplicate.parent.rename(alternate)
        result,rows=run(home,"cursor","--session",str(store))
        assert result.returncode==2 and rows==[],result.stderr


def test_native_id_selection_cannot_substitute_a_misleading_rollout_filename():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=rollout(home);actual=codex.session_metadata(path)["session_id"]
        misleading="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert misleading!=actual
        path.rename(path.with_name(f"rollout-2026-01-01T00-00-00-{misleading}.jsonl"))
        result,rows=run(home,"codex","--session",misleading)
        assert result.returncode==2 and rows==[] and "session_unavailable" in result.stderr
        result,rows=run(home,"codex","--session",actual)
        assert result.returncode==0 and rows[0]["native_session_id"]==actual,result.stderr
        result,rows=run(home,"codex","--session","x"*5000)
        assert result.returncode==2 and rows==[] and "Traceback" not in result.stderr


def test_native_ids_win_over_coincidental_relative_files():
    for host in ('codex','cursor'):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);source=rollout(home) if host=='codex' else transcript(home)
            native_id=(codex.session_metadata(source)['session_id'] if host=='codex' else SID)
            working=home/'working';working.mkdir();collision=working/native_id
            collision.write_text('not a native session')
            result,rows=run(home,host,'--session',native_id,cwd=working)
            assert result.returncode==0 and rows[0]['native_session_id']==native_id,result.stderr
            assert rows[0]['path']==str(source.resolve())
            result,rows=run(home,host,'--session',f'./{native_id}',cwd=working)
            if host=='codex':
                assert result.returncode==2 and rows==[] and 'session_unreadable' in result.stderr
            else:
                assert result.returncode==0 and rows[0]['path']==str(collision.resolve()),result.stderr


def test_latest_cursor_uses_newer_transcript_without_losing_saved_source():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);store=fixtures._make_cursor_store(home/".cursor/chats",uuid=SID)
        path=transcript(home)
        meta_path=store.parent/"meta.json";meta=json.loads(meta_path.read_text())
        meta["updatedAtMs"]=int((MTIME-100)*1000);meta_path.write_text(json.dumps(meta))
        result,rows=run(home,"cursor","--session","latest")
        assert result.returncode==0 and rows[0]["path"]==str(path.resolve()),result.stderr
        state_dir=home/".config/memhub-plugin/cursorflush";state_dir.mkdir(parents=True)
        (state_dir/f"{SID}.json").write_text(json.dumps({"source_kind":"store"}))
        result,rows=run(home,"cursor","--session","latest")
        assert result.returncode==0 and rows[0]["path"]==str(store.resolve()),result.stderr


def test_since_filter_checks_the_final_source_revision():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=rollout(home);original=readers_cli.header_for
        def changing(*args,**kwargs):
            header=original(*args,**kwargs)
            os.utime(path,(MTIME+200,MTIME+200))
            return header
        stdout,stderr=io.StringIO(),io.StringIO()
        with patch.object(codex,"_SESSION_INDEX",home/"index.jsonl"), \
             patch.object(readers_cli,"header_for",changing), \
             contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            code=readers_cli.main(["--host","codex","--session",str(path),"--since","2026-09-08T00:01:40Z"])
        assert code==2 and not stdout.getvalue() and "source_changed" in stderr.getvalue(),stderr.getvalue()
        os.utime(path,(MTIME,MTIME))
        result,rows=run(home,"codex","--session",str(path),"--since","2026-09-08T00:01:40Z")
        assert result.returncode==0 and not rows and not result.stderr,result.stderr


def test_historical_export_keeps_titles_outside_the_capture_tail_window():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=rollout(home);sid=codex.session_metadata(path)["session_id"]
        index=home/".codex/session_index.jsonl"
        with index.open("w") as handle:
            handle.write(json.dumps({"id":sid,"thread_name":"Original native title"})+"\n")
            for n in range(10001):
                handle.write(json.dumps({"id":f"other-{n}","thread_name":"x"*500})+"\n")
        assert index.stat().st_size>codex._INDEX_TAIL_BYTES
        with patch.object(codex,"_SESSION_INDEX",index):
            assert codex.to_canonical(path)[1]["title"]!="Original native title"
        result,rows=run(home,"codex")
        assert result.returncode==0 and rows[0]["title"]=="Original native title",result.stderr
        with index.open("a") as handle:
            handle.write(json.dumps({"id":sid,"thread_name":"Latest native title"})+"\n")
        result,rows=run(home,"codex","--session",sid)
        assert result.returncode==0 and rows[0]["title"]=="Latest native title",result.stderr


def test_rollout_titles_do_not_consult_or_inherit_unrelated_index_changes():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=rollout(home)
        with path.open("a") as handle:
            handle.write(json.dumps({"type":"event_msg","payload":{
                "type":"thread_name_updated","thread_name":"Native rollout title"}})+"\n")
        os.utime(path,(MTIME,MTIME));index=home/".codex/session_index.jsonl"
        for data in [b"{bad}\n",b"invalid\xff\n"]:
            index.write_bytes(data);os.utime(index,(MTIME+200,MTIME+200))
            result,rows=run(home,"codex")
            assert result.returncode==0 and rows[0]["title"]=="Native rollout title",result.stderr
            assert rows[0]["mtime"]==MTIME
            result,rows=run(home,"codex","--since","2026-09-08T00:01:00Z")
            assert result.returncode==0 and not rows,result.stderr


def test_fallback_revision_and_timestamp_are_scoped_to_the_matching_title():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=rollout(home);sid=codex.session_metadata(path)["session_id"]
        index=write_jsonl(home/".codex/session_index.jsonl",[{
            "id":sid,"thread_name":"Selected title","updated_at":"2026-09-08T00:00:20Z"},
            {"id":"unrelated","thread_name":"Newer title","updated_at":"2026-09-08T00:02:00Z"}])
        os.utime(index,(MTIME+200,MTIME+200))
        result,rows=run(home,"codex","--since","2026-09-08T00:01:00Z")
        assert result.returncode==0 and not rows,result.stderr
        real=codex.to_canonical
        def changing(*args,**kwargs):
            value=real(*args,**kwargs)
            with index.open("a") as handle:
                handle.write(json.dumps({"id":"unrelated","thread_name":"Changed elsewhere"})+"\n")
            return value
        stdout,stderr=io.StringIO(),io.StringIO()
        with patch.object(codex,"_SESSION_INDEX",index),patch.object(codex,"to_canonical",side_effect=changing), \
                contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            code=readers_cli.main(["--host","codex","--session",str(path)])
        assert code==0 and not stderr.getvalue(),stderr.getvalue()
        header=json.loads(stdout.getvalue().splitlines()[0])
        assert header["title"]=="Selected title" and header["mtime"]==MTIME+20


def test_cursor_uuid_selection_does_not_parse_unrelated_saved_state():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=transcript(home);other="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        write_jsonl(home/f".cursor/projects/synthetic/agent-transcripts/{other}/{other}.jsonl",[None])
        state=home/f".config/memhub-plugin/cursorflush/{other}.json"
        state.parent.mkdir(parents=True);state.write_text("{broken")
        result,rows=run(home,"cursor","--session",SID)
        assert result.returncode==0 and rows[0]["native_session_id"]==SID,result.stderr
        assert path.read_bytes() and state.read_text()=="{broken"
        result,rows=run(home,"cursor")
        assert result.returncode==2 and "session_unreadable" in result.stderr



def test_hidden_cursor_transcript_duplicates_are_ambiguous_even_with_one_store():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);store=fixtures._make_cursor_store(home/".cursor/chats",uuid=SID)
        first=transcript(home)
        second=write_jsonl(home/f".cursor/projects/other/agent-transcripts/{SID}/{SID}.jsonl",fixtures.CURSOR_TRANSCRIPT)
        os.utime(first,(MTIME+200,MTIME+200));os.utime(second,(MTIME+100,MTIME+100))
        for selection in [[],["--session",SID],["--session","latest"]]:
            for mode in [[],["--metadata-only"]]:
                result,rows=run(home,"cursor",*selection,*mode)
                assert result.returncode==2 and rows==[] and "discovery_incomplete" in result.stderr,(rows,result.stderr)
        for path in [first,second,store]:
            result,rows=run(home,"cursor","--session",str(path),"--metadata-only")
            assert result.returncode==0 and len(rows)==1,result.stderr
        second.unlink()
        for selection in [[],["--session",SID],["--session","latest"]]:
            result,rows=run(home,"cursor",*selection,"--metadata-only")
            assert result.returncode==0 and len(rows)==1,result.stderr


def test_cursor_file_aliases_never_emit_duplicate_resolved_identities():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);original=transcript(home);other="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        alias=home/f".cursor/projects/alias/agent-transcripts/{other}/{other}.jsonl"
        alias.parent.mkdir(parents=True);alias.symlink_to(original)
        for mode in [[],["--metadata-only"]]:
            result,rows=run(home,"cursor",*mode)
            headers=[row for row in rows if row.get("type")=="session"]
            assert result.returncode==2 and len(headers)==1,(rows,result.stderr)
            assert headers[0]["native_session_id"]==SID
            assert "discovery_incomplete" in result.stderr
        result,rows=run(home,"cursor","--session",str(alias),"--metadata-only")
        assert result.returncode==0 and len(rows)==1 and rows[0]["native_session_id"]==SID,result.stderr


def test_latest_cursor_does_not_prepare_unrelated_saved_state_or_metadata():
    for damaged in ["state", "metadata"]:
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);path=transcript(home);other="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
            if damaged=="state":
                older=write_jsonl(home/f".cursor/projects/older/agent-transcripts/{other}/{other}.jsonl",[None])
                os.utime(older,(MTIME-100,MTIME-100))
                bad=home/f".config/memhub-plugin/cursorflush/{other}.json"
                bad.parent.mkdir(parents=True);bad.write_text("{broken")
            else:
                older=fixtures._make_cursor_store(home/".cursor/chats",uuid=other)
                bad=older.parent/"meta.json";bad.write_text('{"updatedAtMs":0,"cwd":17}')
            before=bad.read_bytes()
            for mode in [[],["--metadata-only"]]:
                result,rows=run(home,"cursor","--session","latest",*mode)
                assert result.returncode==0 and rows[0]["native_session_id"]==SID,(damaged,result.stderr)
                assert {row.get("native_session_id") for row in rows if row.get("type")=="session"}=={SID}
            result,_=run(home,"cursor","--metadata-only")
            assert result.returncode==2 and "session_unreadable" in result.stderr
            assert path.read_bytes() and bad.read_bytes()==before


def test_latest_never_reintroduces_symlinked_discovery_directories():
    for host in ["codex","cursor"]:
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);safe=rollout(home) if host=="codex" else transcript(home)
            external=home/"outside"; external.mkdir()
            if host=="codex":
                rows=copy.deepcopy(fixtures.CODEX_SYNTH)
                rows[0]["payload"].update(id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",timestamp=STAMP)
                bad=write_jsonl(external/"rollout-outside.jsonl",rows)
                link=home/".codex/sessions/2027";link.symlink_to(external,target_is_directory=True)
            else:
                sid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                bad=write_jsonl(external/f"agent-transcripts/{sid}/{sid}.jsonl",fixtures.CURSOR_TRANSCRIPT)
                link=home/".cursor/projects/outside";link.symlink_to(external,target_is_directory=True)
            os.utime(bad,(MTIME+100,MTIME+100))
            for mode in [[],["--metadata-only"]]:
                result,rows=run(home,host,"--session","latest",*mode)
                assert result.returncode==2 and "discovery_incomplete" in result.stderr,result.stderr
                headers=[row for row in rows if row.get("type")=="session"]
                assert len(headers)==1 and headers[0]["path"]==str(safe.resolve()),headers


def test_metadata_only_rejects_nonfinite_cursor_metadata():
    for value in ("NaN", "Infinity", "-Infinity", "1e999"):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            store = fixtures._make_cursor_store(home / ".cursor/chats", uuid=SID)
            metadata = store.parent / "meta.json"
            original = json.loads(metadata.read_text())
            invalid = json.dumps(original).replace(
                str(original["createdAtMs"]), value, 1).encode()
            metadata.write_bytes(invalid)
            before = {p.name: p.read_bytes() for p in store.parent.iterdir() if p.is_file()}
            for mode in ([], ["--metadata-only"]):
                result, rows = run(home, "cursor", "--session", str(store), *mode)
                assert result.returncode == 2 and rows == [], (value, result.stdout, result.stderr)
                assert "session_unreadable" in result.stderr and "Traceback" not in result.stderr
            assert before == {p.name: p.read_bytes() for p in store.parent.iterdir() if p.is_file()}
            metadata.write_text(json.dumps(original))
            result, rows = run(home, "cursor", "--session", str(store), "--metadata-only")
            assert result.returncode == 0 and len(rows) == 1 and rows[0]["started_at"]


def test_cursor_creation_timestamp_requires_a_number_or_unknown():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        store = fixtures._make_cursor_store(home / ".cursor/chats", uuid=SID)
        metadata = store.parent / "meta.json"
        original = json.loads(metadata.read_text())
        for value in (True, False, "123", [], {}):
            metadata.write_text(json.dumps({**original, "createdAtMs": value}))
            before = {p.name: p.read_bytes() for p in store.parent.iterdir() if p.is_file()}
            for mode in ([], ["--metadata-only"]):
                result, rows = run(home, "cursor", "--session", str(store), *mode)
                assert result.returncode == 2 and rows == [], (value, result.stdout, result.stderr)
                assert "session_unreadable" in result.stderr and "Traceback" not in result.stderr
            assert before == {p.name: p.read_bytes() for p in store.parent.iterdir() if p.is_file()}
        for value in (None, 0, original["createdAtMs"], float(original["createdAtMs"])):
            metadata.write_text(json.dumps({**original, "createdAtMs": value}))
            for mode in ([], ["--metadata-only"]):
                result, rows = run(home, "cursor", "--session", str(store), *mode)
                assert result.returncode == 0, result.stderr
                header = next(row for row in rows if row.get("type") == "session")
                assert (header["started_at"] is None) == (value is None), header
        del original["createdAtMs"]
        metadata.write_text(json.dumps(original))
        for mode in ([], ["--metadata-only"]):
            result, rows = run(home, "cursor", "--session", str(store), *mode)
            assert result.returncode == 0, result.stderr
            assert next(row for row in rows if row.get("type") == "session")["started_at"] is None


def test_latest_cursor_reports_unreadable_ranking_metadata():
    for data in [b"{broken", b"null", b"[]", b'{"updatedAtMs":"later"}', b"{}",
                 b'{"updatedAtMs":NaN}', b'{"updatedAtMs":1e999}',
                 b'{"updatedAtMs":0,"extra":"bad\xff"}',
                 ('{"updatedAtMs":'+'9'*310+'}').encode()]:
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);transcript(home)
            store=fixtures._make_cursor_store(home/".cursor/chats",uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
            metadata=store.parent/"meta.json";metadata.write_bytes(data)
            for mode in [[],["--metadata-only"]]:
                result,rows=run(home,"cursor","--session","latest",*mode)
                assert result.returncode==2 and rows==[] and "discovery_incomplete" in result.stderr,(data,result.stdout,result.stderr)
                assert "Traceback" not in result.stderr and metadata.read_bytes()==data


def test_latest_cursor_rejects_finite_out_of_range_update_clocks():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);transcript(home)
        store=fixtures._make_cursor_store(home/".cursor/chats",uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        metadata=store.parent/"meta.json";original=json.loads(metadata.read_text())
        for value in (10**18,-10**18,1e300,-1e300):
            metadata.write_text(json.dumps({**original,'updatedAtMs':value}));before=metadata.read_bytes()
            for mode in ([],['--metadata-only']):
                result,rows=run(home,'cursor','--session','latest',*mode)
                assert result.returncode==2 and rows==[] and 'discovery_incomplete' in result.stderr,(value,result.stdout,result.stderr)
                assert metadata.read_bytes()==before
        for value in (0,original['updatedAtMs']):
            metadata.write_text(json.dumps({**original,'updatedAtMs':value}))
            for mode in ([],['--metadata-only']):
                result,rows=run(home,'cursor','--session','latest',*mode)
                assert result.returncode==0 and rows,result.stderr


def test_saved_cursor_store_cannot_bypass_safe_discovery():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as outside:
        home=Path(td);safe=transcript(home)
        store=fixtures._make_cursor_store(Path(outside)/"chats",uuid=SID)
        link=home/".cursor/chats/outside";link.parent.mkdir(parents=True)
        link.symlink_to(store.parent.parent,target_is_directory=True)
        saved=home/f".config/memhub-plugin/cursorflush/{SID}.json"
        saved.parent.mkdir(parents=True);saved.write_text(json.dumps({"source_kind":"store"}))
        before={path:path.read_bytes() for path in [safe,saved,store,store.parent/"meta.json"]}
        for selection in [[],["--session",SID],["--session","latest"]]:
            for mode in [[],["--metadata-only"]]:
                result,rows=run(home,"cursor",*selection,*mode)
                assert result.returncode==2 and rows==[],(selection,mode,result.stderr,rows)
                assert "discovery_incomplete" in result.stderr and "Traceback" not in result.stderr
        # Explicit paths remain an intentional selection, including an alias.
        result,rows=run(home,"cursor","--session",str(store),"--metadata-only")
        assert result.returncode==0 and rows[0]["path"]==str(store.resolve()),result.stderr
        assert all(path.read_bytes()==data for path,data in before.items())


def test_selected_codex_skips_distinct_malformed_headers_after_counting_ids():
    for field,value in [('timestamp','bad-clock'),('cwd',17),('originator',[]),('git',{'branch':17})]:
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);selected=rollout(home)
            sid=codex.session_metadata(selected)['session_id']
            bad_rows=copy.deepcopy(fixtures.CODEX_SYNTH)
            bad_rows[0]['payload'].update(id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',timestamp=STAMP)
            bad_rows[0]['payload'][field]=value
            bad=write_jsonl(selected.with_name('rollout-unrelated.jsonl'),bad_rows)
            os.utime(bad,(MTIME-100,MTIME-100))
            originals={path:path.read_bytes() for path in (selected,bad)}
            for selection in [sid,sid+'.jsonl','latest',str(selected)]:
                for mode in [[],['--metadata-only']]:
                    result,rows=run(home,'codex','--session',selection,*mode)
                    assert result.returncode==0,(field,selection,result.stderr)
                    headers=[row for row in rows if row.get('type')=='session']
                    assert len(headers)==1 and headers[0]['native_session_id']==sid
            result,_=run(home,'codex')
            assert result.returncode==2 and 'session_unreadable' in result.stderr
            assert all(path.read_bytes()==data for path,data in originals.items())


def test_title_index_cr_records_preserve_titles_and_per_record_byte_bound():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=rollout(home);sid=codex.session_metadata(path)['session_id']
        index=home/'.codex/session_index.jsonl'
        entries=[{'id':'other','thread_name':'Other title'},
                 {'id':sid,'thread_name':'标题\u2028native title'}]
        rows=[json.dumps(row,ensure_ascii=False).encode() for row in entries]
        for ending in (b'\n',b'\r',b'\r\n'):
            for trailing in (True,False):
                raw=ending.join(rows)+(ending if trailing else b'')
                index.write_bytes(raw)
                result,output=run(home,'codex')
                assert result.returncode==0,result.stderr
                assert output[0]['title']==codex._one_line(entries[1]['thread_name'])
                assert index.read_bytes()==raw
                with patch.object(codex,'_SESSION_INDEX',index),patch.object(codex,'_INDEX_TAIL_BYTES',max(map(len,rows))+len(ending)):
                    assert readers_cli.historical_titles(codex,{sid})[sid][0]==output[0]['title']
        index.write_bytes(rows[1]+b'\r')
        with patch.object(codex,'_SESSION_INDEX',index),patch.object(codex,'_INDEX_TAIL_BYTES',len(rows[1])):
            try:readers_cli.historical_titles(codex,{sid})
            except ValueError:pass
            else:raise AssertionError('title index accepted an oversized UTF-8 record')


def test_discovered_posix_fifo_does_not_block_healthy_session_output():
    if not hasattr(os,'mkfifo'):
        return
    for host in ('codex','cursor'):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);good=rollout(home) if host=='codex' else transcript(home)
            fifo=good.with_name('rollout-pipe.jsonl') if host=='codex' else home/'.cursor/projects/other/agent-transcripts/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl'
            fifo.parent.mkdir(parents=True,exist_ok=True);os.mkfifo(fifo)
            result,rows=run(home,host,'--metadata-only')
            assert result.returncode==2 and 'discovery_incomplete' in result.stderr
            assert len(rows)==1 and rows[0]['path']==str(good.resolve())


def test_cursor_special_file_sidecars_are_rejected_without_blocking():
    if not hasattr(os,'mkfifo'):
        return  # Native Windows does not expose POSIX FIFO creation.
    for sidecar in ('meta.json','store.db-wal','store.db-journal','saved-state'):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);store=fixtures._make_cursor_store(home/'.cursor/chats',uuid=SID)
            fifo=(home/f'.config/memhub-plugin/cursorflush/{SID}.json'
                  if sidecar=='saved-state' else store.parent/sidecar)
            fifo.parent.mkdir(parents=True,exist_ok=True)
            if fifo.exists():fifo.unlink()
            os.mkfifo(fifo);before=fifo.lstat()
            for mode in ([],['--metadata-only']):
                result,rows=run(home,'cursor',*mode)
                assert result.returncode==2 and rows==[],(sidecar,result.stdout,result.stderr)
                assert 'session_unreadable' in result.stderr and 'Traceback' not in result.stderr
            after=fifo.lstat();assert stat.S_ISFIFO(after.st_mode)
            assert (before.st_dev,before.st_ino)==(after.st_dev,after.st_ino)


def test_codex_special_title_index_is_rejected_only_when_consumed():
    if not hasattr(os,'mkfifo'):
        return  # Native Windows does not expose POSIX FIFO creation.
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);rollout(home)
        index=home/'.codex/session_index.jsonl';os.mkfifo(index)
        result,rows=run(home,'codex','--metadata-only')
        assert result.returncode==0 and len(rows)==1,result.stderr
        result,rows=run(home,'codex')
        assert result.returncode==2 and rows==[] and 'session_unreadable' in result.stderr
        assert stat.S_ISFIFO(index.lstat().st_mode)


def test_codex_bad_git_container_is_rejected_without_losing_identity_checks():
    for git in (17,[],False,'branch'):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);good=rollout(home);rows=[json.loads(x) for x in good.read_text().splitlines()]
            sid=rows[0]['payload']['id'];bad_rows=copy.deepcopy(rows);bad_rows[0]['payload']['git']=git
            bad=write_jsonl(good.with_name('rollout-bad-git.jsonl'),bad_rows)
            os.utime(bad,(MTIME-100,MTIME-100))
            for selection in (sid,'latest'):
                result,output=run(home,'codex','--session',selection)
                assert result.returncode==2 and output==[],result.stderr
            result,output=run(home,'codex','--session',str(bad),'--metadata-only')
            assert result.returncode==2 and output==[],result.stderr
            bad_rows[0]['payload']['id']='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
            write_jsonl(bad,bad_rows);os.utime(bad,(MTIME-100,MTIME-100))
            for selection in (sid,'latest'):
                result,output=run(home,'codex','--session',selection,'--metadata-only')
                assert result.returncode==0 and len(output)==1,result.stderr
            result,output=run(home,'codex','--metadata-only')
            assert result.returncode==2 and len(output)==1,result.stderr


def test_cursor_out_of_range_start_is_incomplete_in_both_export_modes():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);store=fixtures._make_cursor_store(home/'.cursor/chats',uuid=SID)
        path=store.parent/'meta.json';original=json.loads(path.read_text())
        for value in (10**18,-10**18,1e300,10**400):
            path.write_text(json.dumps({**original,'createdAtMs':value}))
            for mode in ([],['--metadata-only']):
                result,rows=run(home,'cursor',*mode)
                assert result.returncode==2 and rows==[] and 'session_unreadable' in result.stderr
            try:cursor._created_at({'createdAtMs':value},strict=True)
            except ValueError:pass
            else:raise AssertionError('checked creation conversion accepted out-of-range time')


def test_saved_state_is_applied_from_the_validated_descriptor():
    """Proof the check/use race is closed. The state path is swapped for a
    directory after its bytes are read; applying those bytes still succeeds,
    while the old reopen-by-path behaviour refuses. STATE_DIR is patched
    directly because cursor_flush resolves it at import time."""
    import tempfile, json as _json
    import cursor_flush
    sid = "11111111-2222-4333-8444-555555555555"
    target_uuid = "66666666-7777-4888-8999-aaaaaaaaaaaa"
    original = cursor_flush.STATE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        cursor_flush.STATE_DIR = pathlib.Path(tmp)
        try:
            path = cursor_flush._state_path(sid)
            path.write_text(_json.dumps({"usage_events": {sid: {
                "target_uuid": target_uuid, "usage": {"inputTokens": 5}}}}), encoding="utf-8")
            with open(path, "rb") as handle:          # the validated descriptor
                validated = handle.read().decode("utf-8")
            path.unlink()
            path.mkdir()                              # any reopen-by-path now fails
            records = [{"uuid": target_uuid, "type": "assistant",
                        "message": {"role": "assistant", "content": [], "model": "m"}}]
            cursor_flush.apply_session_state(records, sid, strict=True,
                                             state_text=validated)
            assert records[0]["message"]["usage"]["input_tokens"] == 5
            reopened = False
            try:
                cursor_flush.apply_session_state(records, sid, strict=True)
            except (OSError, ValueError):
                reopened = True
            assert reopened, "apply_session_state still reopened the swapped path"
        finally:
            cursor_flush.STATE_DIR = original

def test_absent_saved_state_is_never_reopened_by_path():
    """Absent at validation must stay absent through apply: cursor_source
    returns an explicit empty state and apply_session_state consults no path.
    A trap on _read_state proves neither step reopens it."""
    import tempfile
    import readers_cli, cursor_flush
    sid = "11111111-2222-4333-8444-555555555555"
    original_dir, original_read = cursor_flush.STATE_DIR, cursor_flush._read_state
    def trap(uuid, *, strict=False, text=None):
        if text is None:
            raise AssertionError("a state path was reopened")
        return original_read(uuid, strict=strict, text=text)
    with tempfile.TemporaryDirectory() as tmp:
        cursor_flush.STATE_DIR = pathlib.Path(tmp)
        cursor_flush._read_state = trap
        try:
            src = pathlib.Path(tmp) / (sid + ".jsonl")
            src.write_text("", encoding="utf-8")
            resolved, state = readers_cli.cursor_source(src, want_state=True)
            assert resolved == src and state == {}
            records = [{"uuid": sid, "type": "assistant",
                        "message": {"role": "assistant", "content": []}}]
            cursor_flush.apply_session_state(records, sid, strict=True, state=state)
        finally:
            cursor_flush.STATE_DIR = original_dir
            cursor_flush._read_state = original_read


def test_header_probes_never_reopen_the_native_path():
    """Metadata probes and full reads must consume the validated descriptor or
    the private snapshot: a trap on every by-name open under the native home
    proves the rollout, store and meta.json are never reopened by path."""
    import builtins, tempfile
    import readers_cli, cursor_flush
    with tempfile.TemporaryDirectory() as td:
        home = Path(td).resolve()
        sources = {"codex": [rollout(home)],
                   "cursor": [fixtures._make_cursor_store(home / ".cursor/chats", uuid=SID),
                              transcript(home)]}
        real_open, real_path_open = builtins.open, pathlib.Path.open
        violations = []

        def native(target):
            try:
                return pathlib.Path(os.fsdecode(target)).resolve().is_relative_to(home)
            except (TypeError, ValueError, OSError):
                return False

        def trap_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)) and native(file):
                violations.append(str(file))
            return real_open(file, *args, **kwargs)

        def trap_path_open(self, *args, **kwargs):
            if native(self):
                violations.append(str(self))
            return real_path_open(self, *args, **kwargs)

        original_dir = cursor_flush.STATE_DIR
        cursor_flush.STATE_DIR = home / ".config/memhub-plugin/cursorflush"
        try:
            for host, paths_ in sources.items():
                for source in paths_:
                    for mode in ([], ["--metadata-only"]):
                        output = io.StringIO()
                        with patch("builtins.open", trap_open), \
                                patch.object(pathlib.Path, "open", trap_path_open), \
                                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                            status = readers_cli.main(["--host", host, "--session", str(source), *mode])
                        rows = [json.loads(line) for line in output.getvalue().splitlines()]
                        assert status == 0 and [row["type"] for row in rows].count("session") == 1, (host, source, mode)
                        assert not violations, (host, source, mode, violations)
        finally:
            cursor_flush.STATE_DIR = original_dir


def test_transcript_snapshots_ignore_sibling_metadata():
    """Only stores carry meta.json. An unrelated sibling of that name beside a
    Cursor transcript is never read, so it must not decide whether the healthy
    transcript exports in full mode when metadata-only mode emits it."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        path = transcript(home)
        (path.parent / "meta.json").mkdir()               # not a regular file
        for mode in ([], ["--metadata-only"]):
            result, rows = run(home, "cursor", "--session", str(path), *mode)
            assert result.returncode == 0, (mode, result.stderr)
            assert [row["type"] for row in rows].count("session") == 1 and rows[0]["native_session_id"] == SID


def test_discovered_aliases_are_rechecked_before_resolving():
    """A discovered rollout or transcript swapped for a symlink after discovery's
    lstat() must not be followed: resolving it would validate and emit the
    external target under the discovered name, in listing and latest mode."""
    import tempfile
    import readers_cli, cursor_flush
    other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    for host in ("codex", "cursor"):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td).resolve()
            reader = codex if host == "codex" else cursor
            if host == "codex":
                rows = copy.deepcopy(fixtures.CODEX_SYNTH)
                rows[0]["payload"].update(id=other, timestamp=STAMP)
                external = write_jsonl(home / "outside/rollout-outside.jsonl", rows)
                roots = {"_SESSIONS": home / ".codex/sessions"}
                restore = lambda: rollout(home)
            else:
                external = write_jsonl(home / f"outside/{other}.jsonl", fixtures.CURSOR_TRANSCRIPT)
                roots = {"_CHATS": home / ".cursor/chats", "_PROJECTS": home / ".cursor/projects"}
                restore = lambda: transcript(home)
            real_list = reader.list_sessions

            def swapped(*args, **kwargs):
                discovered = real_list(*args, **kwargs)
                assert [Path(row["path"]) for row in discovered] == [path], discovered
                path.unlink()
                path.symlink_to(external)             # alias arrives after discovery
                return discovered

            original_dir = cursor_flush.STATE_DIR
            cursor_flush.STATE_DIR = home / ".config/memhub-plugin/cursorflush"
            try:
                with patch.multiple(reader, list_sessions=swapped, **roots):
                    for selection in ([], ["--session", "latest"]):
                        for mode in ([], ["--metadata-only"]):
                            path = restore()
                            output, errors = io.StringIO(), io.StringIO()
                            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                                status = readers_cli.main(["--host", host, *selection, *mode])
                            emitted = [json.loads(line) for line in output.getvalue().splitlines()]
                            assert status == 2 and not [row for row in emitted if row.get("type") == "session"], (host, selection, mode, emitted)
                            assert "session_unreadable" in errors.getvalue() or "discovery_incomplete" in errors.getvalue(), errors.getvalue()
                            path.unlink()                 # remove the alias before the next discovery
            finally:
                cursor_flush.STATE_DIR = original_dir
            assert external.read_bytes()                  # the target was never touched


def test_saved_source_selection_is_bound_to_the_state_revision():
    """The saved pin that selected a representation must be the state the
    revision baseline records. A pin deleted between the selection and the
    baseline reports source_changed instead of emitting the stale choice, in
    listing, native-ID and latest selection, full and metadata-only."""
    import tempfile
    import readers_cli, cursor_flush
    with tempfile.TemporaryDirectory() as td:
        home = Path(td).resolve()
        path = transcript(home)
        fixtures._make_cursor_store(home / ".cursor/chats", uuid=SID)   # discovery prefers the store
        state_dir = home / ".config/memhub-plugin/cursorflush"
        state_dir.mkdir(parents=True)
        state_path = state_dir / f"{SID}.json"
        pin = json.dumps({"source_kind": "transcript", "transcript_path": str(path)})
        real_revision = readers_cli.source_revision

        def vanishing(source, host, **options):
            if state_path.exists():
                state_path.unlink()              # the pin vanishes after it chose the transcript
            return real_revision(source, host, **options)

        def export(*arguments):
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = readers_cli.main(["--host", "cursor", *arguments])
            rows = [json.loads(line) for line in output.getvalue().splitlines()]
            return status, [row for row in rows if row.get("type") == "session"], errors.getvalue()

        original_dir = cursor_flush.STATE_DIR
        cursor_flush.STATE_DIR = state_dir
        try:
            with patch.multiple(cursor, _CHATS=home / ".cursor/chats", _PROJECTS=home / ".cursor/projects"), \
                    patch.object(cursor_flush, "_CURSOR_PROJECTS", home / ".cursor/projects"):
                selections = ([], ["--session", SID], ["--session", "latest"])
                for selection in selections:
                    # Control: an intact pin selects exactly the pinned transcript.
                    state_path.write_text(pin)
                    status, headers, diagnostics = export(*selection, "--metadata-only")
                    assert status == 0 and [row["path"] for row in headers] == [str(path)], (selection, diagnostics)
                with patch.object(readers_cli, "source_revision", vanishing):
                    for selection in selections:
                        for mode in ([], ["--metadata-only"]):
                            state_path.write_text(pin)
                            status, headers, diagnostics = export(*selection, *mode)
                            assert status == 2 and "source_changed" in diagnostics, (selection, mode, diagnostics)
                            assert not headers, (selection, mode, headers)
        finally:
            cursor_flush.STATE_DIR = original_dir


def test_resolution_is_anchored_to_the_rechecked_identity():
    """The alias recheck and resolve() are separate operations. A rollout, or
    one of its parent directories, swapped for a symlink in between must not
    hand back the alias target: the resolved file has to be the very inode the
    recheck observed."""
    import tempfile
    import readers_cli
    other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    for swap in ("leaf", "parent"):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td).resolve()
            path = rollout(home)
            rows = copy.deepcopy(fixtures.CODEX_SYNTH)
            rows[0]["payload"].update(id=other, timestamp=STAMP)
            external = write_jsonl(home / "outside/01/rollout-synthetic.jsonl", rows)
            real_resolve = pathlib.Path.resolve
            swapped = []

            def resolve(self, strict=False):
                if self == path and not swapped:
                    swapped.append(self)
                    if swap == "leaf":
                        path.unlink()
                        path.symlink_to(external)
                    else:
                        import shutil
                        shutil.rmtree(path.parent)
                        path.parent.symlink_to(external.parent, target_is_directory=True)
                return real_resolve(self, strict=strict)

            with patch.object(codex, "_SESSIONS", home / ".codex/sessions"), \
                    patch.object(pathlib.Path, "resolve", resolve):
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    status = readers_cli.main(["--host", "codex", "--metadata-only"])
            emitted = [json.loads(line) for line in output.getvalue().splitlines()]
            assert swapped and status == 2, (swap, errors.getvalue())
            assert not [row for row in emitted if row.get("type") == "session"], (swap, emitted)
            assert "session_unreadable" in errors.getvalue() or "discovery_incomplete" in errors.getvalue()


def test_reads_after_the_baseline_demand_the_baseline_identity():
    """A source or saved state swapped after the revision baseline and swapped
    back before the final comparison must not feed the read: the snapshot copy
    and the applied state must be the very files the baseline observed."""
    import tempfile, shutil
    import readers_cli, cursor_flush
    other_rows = [dict(row, uuid=row["uuid"].replace("a", "b")) if isinstance(row, dict) and "uuid" in row else row
                  for row in fixtures.CURSOR_TRANSCRIPT]
    for swap in ("source", "state"):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td).resolve()
            path = transcript(home)
            state_dir = home / ".config/memhub-plugin/cursorflush"
            state_dir.mkdir(parents=True)
            state_path = state_dir / f"{SID}.json"
            state_path.write_text(json.dumps({"source_kind": "transcript", "transcript_path": str(path)}))
            keep = home / "keep"
            real_snapshot, real_selection = readers_cli.source_snapshot, readers_cli.cursor_selection

            def exchange():
                # Replace with a distinct inode carrying different bytes.
                target = path if swap == "source" else state_path
                shutil.move(target, keep)
                if swap == "source":
                    write_jsonl(target, other_rows)
                else:
                    # Same pin, different bytes and inode: identity, not content, is at stake.
                    target.write_text(json.dumps({"source_kind": "transcript", "transcript_path": str(path)}, indent=2))

            def restore():
                target = path if swap == "source" else state_path
                target.unlink()
                shutil.move(keep, target)

            @contextlib.contextmanager
            def swapped_snapshot(source, host, *rest):
                exchange() if swap == "source" else None
                try:
                    with real_snapshot(source, host, *rest) as snapshot:
                        yield snapshot
                finally:
                    if swap == "source":
                        restore()

            calls = []

            def swapped_selection(source, **options):
                calls.append(options)
                if swap == "state" and len(calls) == 2:    # the apply-time read
                    exchange()
                    try:
                        return real_selection(source, **options)
                    finally:
                        restore()
                return real_selection(source, **options)

            original_dir = cursor_flush.STATE_DIR
            cursor_flush.STATE_DIR = state_dir
            try:
                with patch.multiple(cursor, _CHATS=home / ".cursor/chats", _PROJECTS=home / ".cursor/projects"), \
                        patch.object(cursor_flush, "_CURSOR_PROJECTS", home / ".cursor/projects"), \
                        patch.object(readers_cli, "source_snapshot", swapped_snapshot), \
                        patch.object(readers_cli, "cursor_selection", swapped_selection):
                    output, errors = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                        status = readers_cli.main(["--host", "cursor", "--session", SID])
                emitted = [json.loads(line) for line in output.getvalue().splitlines()]
                assert status == 2 and "source_changed" in errors.getvalue(), (swap, errors.getvalue())
                assert not [row for row in emitted if row.get("type") == "session"], (swap, emitted)
                assert path.read_text() == "".join(json.dumps(row) + "\n" for row in fixtures.CURSOR_TRANSCRIPT)
            finally:
                cursor_flush.STATE_DIR = original_dir


def test_saved_store_pins_ignore_the_working_directory():
    """A pinned store is resolved from the native chats root. A working-directory
    entry named after the UUID must not be selected by the legacy locator and
    make the healthy discovered store unreadable."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        store = fixtures._make_cursor_store(home / ".cursor/chats", uuid=SID)
        state_path = home / f".config/memhub-plugin/cursorflush/{SID}.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(json.dumps({"source_kind": "store"}))
        work = home / "work"
        work.mkdir()
        (work / SID).write_text("not a session\n")
        for selection in ([], ["--session", SID], ["--session", "latest"]):
            result, rows = run(home, "cursor", *selection, "--metadata-only", cwd=work)
            assert result.returncode == 0, (selection, result.stderr)
            assert [row["path"] for row in rows if row["type"] == "session"] == [str(store.resolve())], (selection, rows)


def test_empty_session_selectors_are_rejected():
    """``--session ""`` (an unset shell variable) must be a usage error, never a
    silent fall-through into enumerating every discovered session."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        rollout(home)
        transcript(home)
        for host in ("codex", "cursor"):
            for value in ("", "  "):
                result, rows = run(home, host, "--session", value)
                assert result.returncode == 2 and rows == [], (host, value, result.stdout)
                assert "--session" in result.stderr, (host, value, result.stderr)


def test_root_level_sources_get_their_own_snapshot_directory():
    """A source directly under a filesystem root (``/rollout.jsonl``) has an empty
    parent name. The snapshot must still land in a fresh subdirectory instead of
    colliding with the temporary directory itself. A bare relative name has the
    same empty parent and stands in for the root here."""
    import readers_cli
    with tempfile.TemporaryDirectory() as td:
        source = Path(td) / "rollout-root.jsonl"
        source.write_text('{"marker": true}\n', encoding="utf-8")
        previous = os.getcwd()
        os.chdir(td)
        try:
            bare = Path("rollout-root.jsonl")
            assert bare.parent.name == ""
            for host in ("codex", "cursor"):
                with readers_cli.source_snapshot(bare, host) as snapshot:
                    assert snapshot.name == bare.name and snapshot.parent.name
                    assert snapshot.read_text(encoding="utf-8") == '{"marker": true}\n'
        finally:
            os.chdir(previous)


def test_configured_root_symlinks_are_anchored_across_discovery():
    """A configured root may be a stable symlink. Retargeting it after
    list_sessions() returns must not let a same-named file under the new
    target pass as the discovered one: the resolved file has to sit exactly
    where the root led before discovery."""
    import tempfile
    import readers_cli, cursor_flush
    other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    for host in ("codex", "cursor"):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td).resolve()
            reader = codex if host == "codex" else cursor
            before, after = home / "before", home / "after"
            if host == "codex":
                rollout(before)
                rows = copy.deepcopy(fixtures.CODEX_SYNTH)
                rows[0]["payload"].update(id=other, timestamp=STAMP)
                write_jsonl(after / ".codex/sessions/2026/01/01/rollout-synthetic.jsonl", rows)
                link, targets = home / "sessions", (before / ".codex/sessions", after / ".codex/sessions")
                roots = {"_SESSIONS": link}
            else:
                transcript(before)
                write_jsonl(after / f".cursor/projects/synthetic/agent-transcripts/{SID}/{SID}.jsonl",
                            [dict(row, uuid=row["uuid"].replace("a", "b")) if isinstance(row, dict) and "uuid" in row else row
                             for row in fixtures.CURSOR_TRANSCRIPT])
                link, targets = home / "projects", (before / ".cursor/projects", after / ".cursor/projects")
                roots = {"_CHATS": home / ".cursor/chats", "_PROJECTS": link}
            link.symlink_to(targets[0], target_is_directory=True)
            real_list = reader.list_sessions

            def retargeted(*args, **kwargs):
                discovered = real_list(*args, **kwargs)
                assert discovered, "discovery through the stable root symlink must find the session"
                link.unlink()
                link.symlink_to(targets[1], target_is_directory=True)   # retarget after discovery
                return discovered

            original_dir = cursor_flush.STATE_DIR
            cursor_flush.STATE_DIR = home / ".config/memhub-plugin/cursorflush"
            try:
                with patch.multiple(reader, list_sessions=retargeted, **roots):
                    for selection in ([], ["--session", "latest"]):
                        for mode in ([], ["--metadata-only"]):
                            link.unlink()
                            link.symlink_to(targets[0], target_is_directory=True)
                            output, errors = io.StringIO(), io.StringIO()
                            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                                status = readers_cli.main(["--host", host, *selection, *mode])
                            emitted = [json.loads(line) for line in output.getvalue().splitlines()]
                            headers = [row for row in emitted if row.get("type") == "session"]
                            assert status == 2 and not headers, (host, selection, mode, headers, errors.getvalue())
                            assert "session_unreadable" in errors.getvalue() or "discovery_incomplete" in errors.getvalue()
                # A stable root symlink keeps working.
                link.unlink()
                link.symlink_to(targets[0], target_is_directory=True)
                with patch.multiple(reader, **roots):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                        status = readers_cli.main(["--host", host, "--metadata-only"])
                headers = [json.loads(line) for line in output.getvalue().splitlines()]
                assert status == 0 and len(headers) == 1 and headers[0]["path"].startswith(str(targets[0].resolve())), (host, headers)
            finally:
                cursor_flush.STATE_DIR = original_dir


def test_undated_fallback_title_is_bound_to_the_index_stamp():
    """An undated matching row keeps an identical tuple across an unrelated
    index append, but its effective mtime came from the index stamp, so the
    changed stamp must count as a change."""
    import tempfile, json as _json
    import readers_cli, readers.codex as codex_reader
    sid = "01a0" + "0" * 28
    with tempfile.TemporaryDirectory() as tmp:
        idx = pathlib.Path(tmp) / "session_index.jsonl"
        idx.write_text(_json.dumps({"id": sid, "thread_name": "Old"}) + "\n", encoding="utf-8")
        original = codex_reader._SESSION_INDEX
        codex_reader._SESSION_INDEX = idx
        try:
            titles = readers_cli.TitleIndex(codex_reader, {sid})
            observation = titles.get(sid)
            assert observation[0] == "Old" and titles.stamp_bound(observation)
            stamp_used = titles.stamp
            with open(idx, "a", encoding="utf-8") as fh:   # unrelated append
                fh.write(_json.dumps({"id": "other", "thread_name": "x"}) + "\n")
            assert titles.get(sid) == observation          # same tuple ...
            assert titles.stamp != stamp_used              # ... different stamp
            dated = ("T", "2026-09-12T00:00:00Z")
            assert not titles.stamp_bound(dated)
        finally:
            codex_reader._SESSION_INDEX = original

def test_checked_title_index_rejects_malformed_matching_row():
    """A matching historical title row with a non-string thread_name must fail
    the checked read instead of silently yielding a derived title."""
    import tempfile, json as _json
    import readers_cli, readers.codex as codex_reader
    sid = "01a0" + "0" * 28
    with tempfile.TemporaryDirectory() as tmp:
        idx = pathlib.Path(tmp) / "session_index.jsonl"
        idx.write_text(_json.dumps({"id": sid, "thread_name": 17}) + "\n", encoding="utf-8")
        original = codex_reader._SESSION_INDEX
        codex_reader._SESSION_INDEX = idx
        try:
            ok = True
            try:
                readers_cli.TitleIndex(codex_reader, {sid}).get(sid)
                ok = False
            except ValueError:
                pass
            assert ok, "checked title index accepted a malformed matching row"
        finally:
            codex_reader._SESSION_INDEX = original


def test_non_sqlite_sources_are_read_from_a_private_snapshot():
    """A Codex rollout or Cursor transcript must be parsed from a snapshot taken
    through the validated descriptor: once the snapshot exists, the native
    path is swapped for a directory and the read still succeeds untouched."""
    import tempfile, os
    import readers_cli
    with tempfile.TemporaryDirectory() as tmp:
        for host, rel in (("codex", "2026/09/12/rollout-2026-09-12T00-00-00-01a0" + "0" * 28 + ".jsonl"),
                          ("cursor", "11111111-2222-4333-8444-555555555555/11111111-2222-4333-8444-555555555555.jsonl")):
            src = pathlib.Path(tmp) / host / rel
            src.parent.mkdir(parents=True)
            src.write_text('{"marker": true}\n', encoding="utf-8")
            with readers_cli.source_snapshot(src, host) as snap:
                assert snap != src and snap.name == src.name and snap.parent.name == src.parent.name
                src.unlink(); src.mkdir()                 # swap after the snapshot
                assert snap.read_text(encoding="utf-8") == '{"marker": true}\n'
            assert not snap.exists()                      # private copy is cleaned up
            # And a source that is already a special file is refused, never opened.
            fifo = pathlib.Path(tmp) / host / "fifo.jsonl"
            os.mkfifo(fifo)
            refused = False
            try:
                with readers_cli.source_snapshot(fifo, host):
                    pass
            except ValueError:
                refused = True
            assert refused


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASS")
