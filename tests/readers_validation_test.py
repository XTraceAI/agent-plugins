"""Strict native reads expose damaged sources without changing capture defaults."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

import readers_test as fixtures
from readers import codex, cursor, discovery
import cursor_flush

SID="11111111-2222-3333-4444-555555555555"


def rejected(call):
    try:call()
    except (ValueError, UnicodeError):return
    raise AssertionError("strict native read accepted malformed data")


def sources(home):
    rollout=fixtures._write_jsonl(home/"rollout.jsonl",copy.deepcopy(fixtures.CODEX_SYNTH))
    transcript=fixtures._write_jsonl(home/f"{SID}.jsonl",copy.deepcopy(fixtures.CURSOR_TRANSCRIPT))
    return [(codex,rollout),(cursor,transcript)]


def test_strict_reads_preserve_existing_canonical_records_and_source_bytes():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td)
        for reader,path in sources(home)+[(cursor,fixtures._make_cursor_store(home/"native stores #1?"))]:
            before=path.read_bytes();expected=reader.to_canonical(path)
            assert reader.to_canonical(path,strict_utf8=True,strict_json=True)==expected
            assert path.read_bytes()==before


def test_complete_non_object_rows_fail_but_legacy_capture_stays_tolerant():
    with tempfile.TemporaryDirectory() as td:
        for reader,path in sources(Path(td)):
            original=path.read_bytes();expected=reader.to_canonical(path)
            for row in [b'null\n',b'17\n',b'[]\n',b'"text"\n',b'null']:
                path.write_bytes(original+row)
                assert reader.to_canonical(path)==expected
                rejected(lambda:reader.to_canonical(path,strict_json=True))
            path.write_bytes(original)


def test_cursor_recognized_roles_require_object_messages_in_strict_reads():
    with tempfile.TemporaryDirectory() as td:
        reader,path=sources(Path(td))[1];original=path.read_bytes();expected=reader.to_canonical(path)
        for role in ['user','assistant','system','tool']:
            for body in [None,[],17]:
                path.write_bytes(original+json.dumps({'role':role,'message':body}).encode()+b'\n')
                assert reader.to_canonical(path)==expected
                rejected(lambda:reader.to_canonical(path,strict_json=True))


def test_strict_reads_defer_only_unfinished_json_and_reject_invalid_utf8():
    with tempfile.TemporaryDirectory() as td:
        for reader,path in sources(Path(td)):
            original=path.read_bytes();expected=reader.to_canonical(path)
            path.write_bytes(original+b'{"unfinished":')
            assert reader.to_canonical(path,strict_utf8=True,strict_json=True)==expected
            path.write_bytes(original+b'{bad}\n')
            rejected(lambda:reader.to_canonical(path,strict_utf8=True,strict_json=True))
            for ending in [b'\n',b'']:
                path.write_bytes(original+b'{"bad":"\xff"}'+ending)
                rejected(lambda:reader.to_canonical(path,strict_utf8=True,strict_json=True))


def test_strict_cursor_store_decodes_message_leaves_without_replacement():
    with tempfile.TemporaryDirectory() as td:
        store=fixtures._make_cursor_store(Path(td)/"chats")
        with sqlite3.connect(store) as connection:
            identity,data=next((key,value) for key,value in connection.execute("SELECT id,data FROM blobs")
                               if isinstance(value,bytes) and b'"role": "assistant"' in value)
            message=json.loads(data);message['content']=[{'type':'text','text':'invalidXtext'}]
            data=json.dumps(message).encode().replace(b'invalidXtext',b'invalid\xfftext')
            connection.execute('UPDATE blobs SET data=? WHERE id=?',(data,identity))
        assert '\ufffd' in json.dumps(cursor.to_canonical(store),ensure_ascii=False)
        rejected(lambda:cursor.to_canonical(store,strict_utf8=True,strict_json=True))
        with sqlite3.connect(store) as connection:
            connection.execute('UPDATE blobs SET data=? WHERE id=?',(b'{"role":',identity))
        rejected(lambda:cursor.to_canonical(store,strict_utf8=True,strict_json=True))


def test_title_index_strictness_is_opt_in_and_preserves_incomplete_tail():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=sources(home)[0][1]
        sid=codex.session_metadata(path)['session_id'];index=home/'session_index.jsonl'
        with patch.object(codex,'_SESSION_INDEX',index):
            for content in [b'null\n',b'{bad}\n',b'{"bad":"\xff"}']:
                index.write_bytes(content);codex.to_canonical(path)
                rejected(lambda:codex.to_canonical(path,strict_utf8=True,strict_json=True))
            index.write_text(json.dumps({'id':sid,'thread_name':'native title'})+'\n{"unfinished":')
            assert codex.to_canonical(path,strict_utf8=True,strict_json=True)[1]['title']=='native title'


def test_title_index_utf8_and_json_flags_are_independent():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=sources(home)[0][1];index=home/'session_index.jsonl'
        with patch.object(codex,'_SESSION_INDEX',index):
            for body in [b'{bad}\n',b'null\n']:
                index.write_bytes(body)
                codex.to_canonical(path,strict_utf8=True)
                rejected(lambda:codex.to_canonical(path,strict_json=True))
            index.write_bytes(b'{"thread_name":"invalid\xfftext"}\n')
            codex.to_canonical(path,strict_json=True)
            rejected(lambda:codex.to_canonical(path,strict_utf8=True))


def test_saved_observations_are_strict_only_when_requested_and_never_written():
    with tempfile.TemporaryDirectory() as td,patch.object(cursor_flush,'STATE_DIR',Path(td)):
        path=Path(td)/f'{SID}.json'
        assert cursor_flush._read_state(SID,strict=True)=={}
        for body in ['{bad}','[]','null','{"record_ts":[]}','{"usage_events":17}',
                     '{"record_ts":{"record":"not a date"}}','{"usage_events":{"generation":{}}}']:
            path.write_text(body);before=path.read_bytes()
            cursor_flush._read_state(SID)
            rejected(lambda:cursor_flush._read_state(SID,strict=True))
            assert path.read_bytes()==before
        path.write_text('{"record_ts":{"record":null},"usage_events":{}}')
        assert cursor_flush._read_state(SID,strict=True)['record_ts']['record'] is None


def test_discovery_reports_missing_and_symlinked_sources_and_keeps_healthy_paths():
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);errors=[]
        assert discovery.paths(root/'missing',('**','rollout-*.jsonl'),errors.append)==[] and errors
        good=root/'2026';good.mkdir();path=good/'rollout-synthetic.jsonl';path.write_text('{}\n')
        (root/'cycle').symlink_to(root,target_is_directory=True);errors=[]
        assert discovery.paths(root,('**','rollout-*.jsonl'),errors.append)==[path]
        assert len(errors)==1


def test_metadata_preserves_native_surface_start_and_unknowns_without_titles():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);rows=copy.deepcopy(fixtures.CODEX_SYNTH)
        stamp='2026-01-01T00:00:00.123456789012Z'
        rows[0]['payload'].update(originator='Future Desktop',timestamp=stamp,git={'branch':'synthetic'})
        path=fixtures._write_jsonl(home/'rollout.jsonl',rows)
        result=codex.session_metadata(path)
        assert result['source_surface']=='Future Desktop' and result['started_at']==stamp
        assert result['git_branch']=='synthetic' and 'title' not in result
        path=fixtures._write_jsonl(home/f'{SID}.jsonl',fixtures.CURSOR_TRANSCRIPT)
        assert cursor.session_metadata(path)['source_surface'] is None
        assert cursor.session_metadata(path)['started_at'] is None
        projects=home/'.cursor/projects';native=fixtures._make_cursor_transcript(projects,uuid=SID)
        with patch.object(cursor,'_PROJECTS',projects):
            assert cursor.session_metadata(native)['source_surface']=='cursor-ide'


def test_strict_cursor_tree_rejects_missing_references_and_cycles_but_allows_shared_nodes():
    for damage in ("missing_root", "missing_leaf", "missing_root_pointer", "cycle", "shared"):
        with tempfile.TemporaryDirectory() as td:
            store=fixtures._make_cursor_store(Path(td)/"chats")
            with sqlite3.connect(store) as connection:
                meta=json.loads(connection.execute("SELECT value FROM meta").fetchone()[0])
                root=meta["latestRootBlobId"]
                data=connection.execute("SELECT data FROM blobs WHERE id=?",(root,)).fetchone()[0]
                children,_=cursor._parse_node(data)
                if damage=="missing_root":
                    connection.execute("DELETE FROM blobs WHERE id=?",(root,))
                elif damage=="missing_leaf":
                    connection.execute("DELETE FROM blobs WHERE id=?",(children[-1],))
                elif damage=="missing_root_pointer":
                    meta.pop("latestRootBlobId")
                    connection.execute("UPDATE meta SET value=?",(json.dumps(meta),))
                else:
                    reference=root if damage=="cycle" else children[0]
                    connection.execute("UPDATE blobs SET data=? WHERE id=?",(data+b"\x0a\x20"+bytes.fromhex(reference),root))
            before=store.read_bytes()
            expected=cursor.to_canonical(store)
            if damage=="shared":
                assert cursor.to_canonical(store,strict_json=True)==expected
            else:
                rejected(lambda:cursor.to_canonical(store,strict_json=True))
            assert store.read_bytes()==before


def test_legacy_cwd_probe_tolerates_later_decode_damage():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[0][1]
        expected=codex.session_cwd(path)
        assert expected
        path.write_bytes(path.read_bytes()+b'{"bad":"\xff"}\n')
        assert codex.session_cwd(path)==expected
        rejected(lambda:codex.session_metadata(path))
        rejected(lambda:codex.to_canonical(path,strict_utf8=True))


def test_strict_cursor_tree_validates_complete_protobuf_nodes():
    tails=[b"\x0a\x20short", b"\x80", b"\x0a\x80", b"\x15x", b"\x19x",
           b"\x00", b"\x0e", b"\x0a\x01x", b"\x10"+b"\x80"*10+b"\x00"]
    for tail in tails:
        with tempfile.TemporaryDirectory() as td:
            store=fixtures._make_cursor_store(Path(td)/"chats")
            with sqlite3.connect(store) as connection:
                root=json.loads(connection.execute("SELECT value FROM meta").fetchone()[0])["latestRootBlobId"]
                data=connection.execute("SELECT data FROM blobs WHERE id=?",(root,)).fetchone()[0]
                connection.execute("UPDATE blobs SET data=? WHERE id=?",(data+tail,root))
            before=store.read_bytes()
            cursor.to_canonical(store,strict_utf8=True)
            rejected(lambda:cursor.to_canonical(store,strict_json=True))
            assert store.read_bytes()==before


def test_cursor_json_only_mode_does_not_enable_utf8_strictness():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[1][1];original=path.read_bytes();expected=cursor.to_canonical(path)
        row=b'{"role":"assistant","message":{"content":"invalid\xfftext"}}'
        for ending in (b"\n",b""):
            path.write_bytes(original+row+ending)
            result=cursor.to_canonical(path,strict_json=True,strict_utf8=False)
            assert "invalid\ufffdtext" in json.dumps(result,ensure_ascii=False)
            rejected(lambda:cursor.to_canonical(path,strict_json=True,strict_utf8=True))
        # Original capture still defers an unterminated undecodable byte tail.
        assert cursor.to_canonical(path)==expected


def test_strict_cursor_store_rejects_non_message_leaves_and_invalid_content():
    invalid=[{"not_role":1}, {"role":None}, {"role":[]}, {"role":"future"},
             {"role":"assistant"}, {"role":"user","content":17},
             {"role":"assistant","content":[17]}]
    for message in invalid:
        with tempfile.TemporaryDirectory() as td:
            store=fixtures._make_cursor_store(Path(td)/"chats")
            with sqlite3.connect(store) as connection:
                identity=next(key for key,value in connection.execute("SELECT id,data FROM blobs")
                              if isinstance(value,bytes) and b'"role": "assistant"' in value)
                connection.execute("UPDATE blobs SET data=? WHERE id=?",(json.dumps(message).encode(),identity))
            before=store.read_bytes()
            cursor.to_canonical(store)
            rejected(lambda:cursor.to_canonical(store,strict_json=True))
            assert store.read_bytes()==before


if __name__=='__main__':
    for name,fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn();print('PASS',name)
    print('ALL PASS')
