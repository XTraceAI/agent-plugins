#!/usr/bin/env python3
"""Native reader stream: real CLI, existing reader parity and read-only state."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
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
        state = {"source_kind": "transcript", "transcript_path": str(path),
                 "offset": 73, "record_ts": {target["uuid"]: STAMP},
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

        def malformed(source, **kwargs):
            records, native = original(source, **kwargs)
            records[-1]["synthetic_invalid_number"] = float("nan")
            return records, native

        def changing(source, **kwargs):
            result = original(source, **kwargs)
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
        with sqlite3.connect(store) as connection:
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
                                env={"HOME": td, "USERPROFILE": td}, capture_output=True, timeout=20)
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
                                env={"HOME": td, "USERPROFILE": td}, capture_output=True, timeout=20)
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
        with sqlite3.connect(store) as db:
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
        state.write_text('{"record_ts":{"record":null},"usage_events":{}}')
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
                   {},"synthetic-generation",target["uuid"],{"input_tokens":7,"output_tokens":3})}
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASS")
