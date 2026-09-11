"""Strict native reads expose damaged sources without changing capture defaults."""
from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
from unittest.mock import patch

import readers_test as fixtures
from readers import claude, codex, cursor, discovery
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
        for reader,path in sources(home)+[(cursor,fixtures._make_cursor_store(home/"native stores #1%2"))]:
            before=path.read_bytes();expected=reader.to_canonical(path)
            assert reader.to_canonical(path,strict=True)==expected
            assert path.read_bytes()==before


def test_complete_non_object_rows_fail_but_legacy_capture_stays_tolerant():
    with tempfile.TemporaryDirectory() as td:
        for reader,path in sources(Path(td)):
            original=path.read_bytes();expected=reader.to_canonical(path)
            for row in [b'null\n',b'17\n',b'[]\n',b'"text"\n',b'null']:
                path.write_bytes(original+row)
                assert reader.to_canonical(path)==expected
                rejected(lambda:reader.to_canonical(path,strict=True))
            path.write_bytes(original)


def test_cursor_recognized_roles_require_object_messages_in_strict_reads():
    with tempfile.TemporaryDirectory() as td:
        reader,path=sources(Path(td))[1];original=path.read_bytes();expected=reader.to_canonical(path)
        for role in ['user','assistant','system','tool']:
            for body in [None,[],17]:
                path.write_bytes(original+json.dumps({'role':role,'message':body}).encode()+b'\n')
                assert reader.to_canonical(path)==expected
                rejected(lambda:reader.to_canonical(path,strict=True))


def test_strict_reads_defer_only_unfinished_json_and_reject_invalid_utf8():
    with tempfile.TemporaryDirectory() as td:
        for reader,path in sources(Path(td)):
            original=path.read_bytes();expected=reader.to_canonical(path)
            path.write_bytes(original+b'{"unfinished":')
            assert reader.to_canonical(path,strict=True)==expected
            path.write_bytes(original+b'{bad}\n')
            rejected(lambda:reader.to_canonical(path,strict=True))
            for ending in [b'\n',b'']:
                path.write_bytes(original+b'{"bad":"\xff"}'+ending)
                rejected(lambda:reader.to_canonical(path,strict=True))


def test_strict_cursor_store_decodes_message_leaves_without_replacement():
    for raw in [b'{"role":"assistant","content":"invalid\xfftext"}', b'{"role":']:
        with tempfile.TemporaryDirectory() as td:
            store=fixtures._make_cursor_store(Path(td)/"chats")
            identity=hashlib.sha256(raw).hexdigest()
            with closing(sqlite3.connect(store)) as connection, connection:
                connection.execute('DELETE FROM blobs')
                connection.execute('INSERT INTO blobs VALUES (?,?)',(identity,raw))
                connection.execute('UPDATE meta SET value=?',(json.dumps({'latestRootBlobId':identity}),))
            before=store.read_bytes()
            legacy=cursor.to_canonical(store)
            if b'\xff' in raw: assert '\ufffd' in json.dumps(legacy,ensure_ascii=False)
            rejected(lambda:cursor.to_canonical(store,strict=True))
            assert store.read_bytes()==before


def test_title_index_strictness_is_opt_in_and_preserves_incomplete_tail():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);path=sources(home)[0][1]
        sid=codex.session_metadata(path)['session_id'];index=home/'session_index.jsonl'
        with patch.object(codex,'_SESSION_INDEX',index):
            for content in [b'null\n',b'{bad}\n',b'{"bad":"\xff"}']:
                index.write_bytes(content);codex.to_canonical(path)
                rejected(lambda:codex.to_canonical(path,strict=True))
            index.write_text(json.dumps({'id':sid,'thread_name':'native title'})+'\n{"unfinished":')
            assert codex.to_canonical(path,strict=True)[1]['title']=='native title'



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
            with closing(sqlite3.connect(store)) as connection, connection:
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
                    updated=data+b"\x0a\x20"+bytes.fromhex(reference)
                    identity=hashlib.sha256(updated).hexdigest() if damage=="shared" else root
                    connection.execute("UPDATE blobs SET id=?,data=? WHERE id=?",(identity,updated,root))
                    meta["latestRootBlobId"]=identity
                    connection.execute("UPDATE meta SET value=?",(json.dumps(meta),))
            before=store.read_bytes()
            expected=cursor.to_canonical(store)
            if damage=="shared":
                assert cursor.to_canonical(store,strict=True)==expected
            else:
                rejected(lambda:cursor.to_canonical(store,strict=True))
            assert store.read_bytes()==before


def test_legacy_cwd_probe_tolerates_later_decode_damage():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[0][1]
        expected=codex.session_cwd(path)
        assert expected
        path.write_bytes(path.read_bytes()+b'{"bad":"\xff"}\n')
        assert codex.session_cwd(path)==expected
        # A bounded metadata probe must not depend on decoder read-ahead into
        # an unread body. The complete checked read still reports that damage.
        assert codex.session_metadata(path)['cwd']==expected
        rejected(lambda:codex.to_canonical(path,strict=True))


def test_strict_cursor_tree_validates_complete_protobuf_nodes():
    tails=[b"\x0a\x20short", b"\x80", b"\x0a\x80", b"\x15x", b"\x19x",
           b"\x00", b"\x0e", b"\x0a\x01x", b"\x10"+b"\x80"*10+b"\x00"]
    for tail in tails:
        with tempfile.TemporaryDirectory() as td:
            store=fixtures._make_cursor_store(Path(td)/"chats")
            with closing(sqlite3.connect(store)) as connection, connection:
                root=json.loads(connection.execute("SELECT value FROM meta").fetchone()[0])["latestRootBlobId"]
                data=connection.execute("SELECT data FROM blobs WHERE id=?",(root,)).fetchone()[0]
                updated=data+tail;identity=hashlib.sha256(updated).hexdigest()
                connection.execute("UPDATE blobs SET id=?,data=? WHERE id=?",(identity,updated,root))
                connection.execute("UPDATE meta SET value=?",(json.dumps({"latestRootBlobId":identity}),))
            before=store.read_bytes()
            cursor.to_canonical(store)
            rejected(lambda:cursor.to_canonical(store,strict=True))
            assert store.read_bytes()==before



def test_strict_cursor_store_rejects_non_message_leaves_and_invalid_content():
    invalid=[{"not_role":1}, {"role":None}, {"role":[]}, {"role":"future"},
             {"role":"assistant"}, {"role":"user","content":17},
             {"role":"assistant","content":[17]},
             {"role":"tool","content":"lost output"},
             {"role":"tool","content":[{"type":"text","text":"lost output"}]}]
    for message in invalid:
        with tempfile.TemporaryDirectory() as td:
            store=fixtures._make_cursor_store(Path(td)/"chats")
            with closing(sqlite3.connect(store)) as connection, connection:
                raw=json.dumps(message).encode();identity=hashlib.sha256(raw).hexdigest()
                connection.execute("DELETE FROM blobs")
                connection.execute("INSERT INTO blobs VALUES (?,?)",(identity,raw))
                connection.execute("UPDATE meta SET value=?",(json.dumps({"latestRootBlobId":identity}),))
            before=store.read_bytes()
            cursor.to_canonical(store)
            rejected(lambda:cursor.to_canonical(store,strict=True))
            assert store.read_bytes()==before


def test_strict_cursor_hashes_reject_structurally_valid_modified_blobs():
    for leaf in (True,False):
        with tempfile.TemporaryDirectory() as td:
            store=fixtures._make_cursor_store(Path(td)/"chats")
            with closing(sqlite3.connect(store)) as connection, connection:
                identity,data=next((key,value) for key,value in connection.execute("SELECT id,data FROM blobs")
                                   if value.startswith(b"{")==leaf)
                changed=(json.dumps({"role":"assistant","content":"changed message"}).encode()
                         if leaf else data+b"\x10\x01")
                connection.execute("UPDATE blobs SET data=? WHERE id=?",(changed,identity))
            before=store.read_bytes()
            cursor.to_canonical(store)
            rejected(lambda:cursor.to_canonical(store,strict=True))
            assert store.read_bytes()==before



def test_strict_json_rejects_nonstandard_numbers_on_all_opted_in_loaders():
    for constant in ["NaN", "Infinity", "-Infinity", "1e999"]:
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            for reader,path in sources(home):
                original=path.read_bytes()
                row=({"type":"event_msg","payload":{"type":"unknown"}} if reader is codex else
                     {"role":"assistant","message":{"content":"synthetic output"}})
                raw=json.dumps(row)[:-1]+',"extra":'+constant+'}'
                for ending in ["\n", ""]:
                    path.write_bytes(original+raw.encode()+ending.encode())
                    reader.to_canonical(path)
                    rejected(lambda:reader.to_canonical(path,strict=True))
            index=home/"session_index.jsonl"
            rollout=fixtures._write_jsonl(home/"rollout-index.jsonl",copy.deepcopy(fixtures.CODEX_SYNTH))
            for ending in ["\n", ""]:
                index.write_text('{"id":"synthetic","extra":'+constant+'}'+ending)
                with patch.object(codex,"_SESSION_INDEX",index):
                    rejected(lambda:codex.to_canonical(rollout,strict=True))
            with patch.object(cursor_flush,"STATE_DIR",home/"state"):
                cursor_flush.STATE_DIR.mkdir(exist_ok=True)
                saved=cursor_flush._state_path(SID)
                saved.write_text('{"record_ts":{},"extra":'+constant+'}')
                cursor_flush._read_state(SID)
                rejected(lambda:cursor_flush._read_state(SID,strict=True))
            store=fixtures._make_cursor_store(home/"chats")
            raw=('{"role":"assistant","content":"synthetic output","extra":'+constant+'}').encode()
            key=hashlib.sha256(raw).hexdigest()
            with closing(sqlite3.connect(store)) as sql, sql:
                meta=json.loads(sql.execute("SELECT value FROM meta").fetchone()[0]);meta["latestRootBlobId"]=key
                sql.execute("DELETE FROM blobs");sql.execute("INSERT INTO blobs VALUES (?,?)",(key,raw))
                sql.execute("UPDATE meta SET value=?",(json.dumps(meta),))
            cursor.to_canonical(store)
            rejected(lambda:cursor.to_canonical(store,strict=True))
            # Validate both native metadata locations, independent of leaf content.
            clean=b'{"role":"assistant","content":"synthetic output"}'
            key=hashlib.sha256(clean).hexdigest()
            with closing(sqlite3.connect(store)) as sql, sql:
                sql.execute("DELETE FROM blobs");sql.execute("INSERT INTO blobs VALUES (?,?)",(key,clean))
                sql.execute("UPDATE meta SET value=?",('{"latestRootBlobId":"'+key+'","extra":'+constant+'}',))
            rejected(lambda:cursor.to_canonical(store,strict=True))
            with closing(sqlite3.connect(store)) as sql, sql:
                sql.execute("UPDATE meta SET value=?",(json.dumps({"latestRootBlobId":key}),))
            meta=store.parent/"meta.json";original=meta.read_text()
            meta.write_text(original.rstrip()[:-1]+',"extra":'+constant+'}')
            rejected(lambda:cursor.to_canonical(store,strict=True))


def test_strict_cursor_content_blocks_match_the_canonicalizer():
    invalid=[{"type":"future","text":"lost output"},{"type":"text","text":17},
             {"type":"reasoning","text":[]},{"type":"tool-call","toolCallId":17,"toolName":"Read","args":{}},
             {"type":"tool_use","id":"call","name":[],"input":{}},
             {"type":"tool-call","toolName":"Read","args":{}},
             {"type":"tool-call","toolCallId":"","toolName":"Read","args":{}},
             {"type":"tool_use","id":"","name":"Read","input":{}}]
    supported=[{"type":"text","text":"synthetic output"},{"type":"reasoning","text":"synthetic reasoning"},
               {"type":"tool-call","toolCallId":"one","toolName":"Read","args":{}},
               {"type":"tool_use","id":"two","name":"Read","input":{}},
               {"type":"tool-call","id":"three","toolName":"Read","args":{}},
               {"type":"tool_use","toolCallId":"four","name":"Read","input":{}}]
    for kind in ("tool-call", "tool_use"):
        for names in ({}, {"toolName": ""}, {"name": ""}, {"toolName": None, "name": None}):
            invalid.append({"type": kind, "toolCallId": "call", "args": {}, **names})
        for names in ({"toolName": "Read"}, {"name": "Read"},
                      {"toolName": "", "name": "Read"}, {"toolName": "Read", "name": ""}):
            supported.append({"type": kind, "toolCallId": "call", "args": {}, **names})
    for block in invalid+supported:
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);store=fixtures._make_cursor_store(home/"chats")
            message={"role":"assistant","content":[block],"usage":{"input_tokens":3}}
            raw=json.dumps(message).encode();key=hashlib.sha256(raw).hexdigest()
            with closing(sqlite3.connect(store)) as sql, sql:
                meta=json.loads(sql.execute("SELECT value FROM meta").fetchone()[0]);meta["latestRootBlobId"]=key
                sql.execute("DELETE FROM blobs");sql.execute("INSERT INTO blobs VALUES (?,?)",(key,raw))
                sql.execute("UPDATE meta SET value=?",(json.dumps(meta),))
            transcript=fixtures._write_jsonl(home/f"{SID}.jsonl",[{"role":"assistant","message":{k:v for k,v in message.items() if k!="role"}}])
            for source in [store,transcript]:
                before=source.read_bytes()
                if block in invalid: rejected(lambda:cursor.to_canonical(source,strict=True))
                else:
                    actual=cursor.to_canonical(source,strict=True)
                    assert actual==cursor.to_canonical(source)
                    assert any(row.get("message",{}).get("usage",{}).get("input_tokens")==3 for row in actual[0])
                assert source.read_bytes()==before



def test_strict_cursor_user_blocks_preserve_text_or_reject_unsupported_content():
    invalid=[{"type":"text","text":17},{"type":"image","data":"synthetic"},
             {"type":"text","text":None},{"type":"text"}]
    supported=[{"type":"text","text":"synthetic prompt"},{"type":"text","text":""}]
    for block in invalid+supported:
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);store=fixtures._make_cursor_store(home/"chats")
            message={"role":"user","content":[block]};raw=json.dumps(message).encode();key=hashlib.sha256(raw).hexdigest()
            with closing(sqlite3.connect(store)) as sql, sql:
                meta=json.loads(sql.execute("SELECT value FROM meta").fetchone()[0]);meta["latestRootBlobId"]=key
                sql.execute("DELETE FROM blobs");sql.execute("INSERT INTO blobs VALUES (?,?)",(key,raw))
                sql.execute("UPDATE meta SET value=?",(json.dumps(meta),))
            transcript=fixtures._write_jsonl(home/f"{SID}.jsonl",[{"role":"user","message":{"content":[block]}}])
            for source in [store,transcript]:
                before=source.read_bytes();legacy=cursor.to_canonical(source)
                if block in invalid:rejected(lambda:cursor.to_canonical(source,strict=True))
                else:assert cursor.to_canonical(source,strict=True)==legacy
                assert source.read_bytes()==before


def test_jsonl_unicode_separators_remain_inside_codex_and_claude_records():
    for separator in ["\u0085", "\u2028", "\u2029"]:
        for ending in ["\n", "\r\n", "\r", ""]:
            with tempfile.TemporaryDirectory() as td:
                home=Path(td);text="before"+separator+"after"
                record={"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":text}]}}
                path=sources(home)[0][1];original=path.read_bytes();path.write_bytes(original+json.dumps(record,ensure_ascii=False).encode()+ending.encode())
                for strict in [False,True]:
                    assert codex.load_rollout(path,strict=strict)[-1]==record
                    assert text in json.dumps(codex.to_canonical(path,strict=strict),ensure_ascii=False)
                index=home/'session_index.jsonl';index.write_bytes(json.dumps({'id':SID,'thread_name':text},ensure_ascii=False).encode()+ending.encode())
                with patch.object(codex,'_SESSION_INDEX',index):
                    for strict in [False,True]:assert codex._sidecar_thread_name(SID,strict=strict)=='before'
                native={'type':'user','cwd':'/synthetic/'+text,'message':{'role':'user','content':text}}
                path=home/'claude.jsonl';path.write_bytes(json.dumps(native,ensure_ascii=False).encode()+ending.encode())
                assert claude.load(path)==[native]
                assert claude.session_cwd(path)==native['cwd']
                # Unicode/control separators outside strings are not JSON whitespace.
                malformed=home/'invalid.jsonl'
                for suffix in ['\v','\f',separator]:
                    malformed.write_bytes(json.dumps(record).encode()+suffix.encode()+b'\n')
                    rejected(lambda:codex.load_rollout(malformed,strict=True))


def test_strict_cursor_assistant_text_and_reasoning_require_string_values():
    for kind in ['text','reasoning']:
        for value in [None,17,'MISSING']:
            block={'type':kind}
            if value!='MISSING':block['text']=value
            with tempfile.TemporaryDirectory() as td:
                home=Path(td);store=fixtures._make_cursor_store(home/'chats');message={'role':'assistant','content':[block]}
                raw=json.dumps(message).encode();key=hashlib.sha256(raw).hexdigest()
                with closing(sqlite3.connect(store)) as sql, sql:
                    meta=json.loads(sql.execute('SELECT value FROM meta').fetchone()[0]);meta['latestRootBlobId']=key
                    sql.execute('DELETE FROM blobs');sql.execute('INSERT INTO blobs VALUES (?,?)',(key,raw));sql.execute('UPDATE meta SET value=?',(json.dumps(meta),))
                transcript=fixtures._write_jsonl(home/f'{SID}.jsonl',[{'role':'assistant','message':{'content':[block]}}])
                for source in [store,transcript]:rejected(lambda:cursor.to_canonical(source,strict=True))


def test_bounded_codex_index_keeps_cr_and_crlf_records_after_partial_prefix():
    for newline in (b"\r", b"\r\n", b"\n"):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/"index.jsonl"
            wanted=json.dumps({"id":SID,"thread_name":"synthetic title"}).encode()+newline
            path.write_bytes(b"x"*200+newline+wanted)
            with patch.object(codex,"_SESSION_INDEX",path),patch.object(codex,"_INDEX_TAIL_BYTES",len(wanted)+20):
                for strict in (False,True):
                    assert codex._sidecar_thread_name(SID,strict=strict)=="synthetic title"


def test_strict_cursor_tool_result_fallback_preserves_supported_payloads():
    invalid=[[{"type":"image","data":"synthetic"}],[{"type":"text","text":17}],
             [{"type":"text"}],{},17]
    invalid.append(None)
    supported=["synthetic",[{"type":"text","text":"synthetic"}],[]]
    for fallback in invalid+supported:
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);store=fixtures._make_cursor_store(home/"chats")
            block={"type":"tool-result","toolCallId":"call","experimental_content":fallback}
            message={"role":"tool","content":[block]};raw=json.dumps(message).encode();key=hashlib.sha256(raw).hexdigest()
            with closing(sqlite3.connect(store)) as sql, sql:
                sql.execute("DELETE FROM blobs");sql.execute("INSERT INTO blobs VALUES (?,?)",(key,raw))
                sql.execute("UPDATE meta SET value=?",(json.dumps({"latestRootBlobId":key}),))
            transcript=fixtures._write_jsonl(home/f"{SID}.jsonl",[{"role":"tool","message":{"content":[block]}}])
            for source in [store,transcript]:
                before=source.read_bytes()
                if fallback in invalid:rejected(lambda:cursor.to_canonical(source,strict=True))
                else:assert cursor.to_canonical(source,strict=True)==cursor.to_canonical(source)
                assert source.read_bytes()==before


def test_strict_cursor_tool_results_require_the_consumed_call_identifier():
    for identity in [{}, {"id":"call"}, {"toolCallId":""}, {"toolCallId":None}, {"toolCallId":"call"}]:
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);store=fixtures._make_cursor_store(home/"chats")
            block={"type":"tool-result","result":"synthetic output",**identity}
            raw=json.dumps({"role":"tool","content":[block]}).encode();key=hashlib.sha256(raw).hexdigest()
            with closing(sqlite3.connect(store)) as sql, sql:
                sql.execute("DELETE FROM blobs");sql.execute("INSERT INTO blobs VALUES (?,?)",(key,raw))
                sql.execute("UPDATE meta SET value=?",(json.dumps({"latestRootBlobId":key}),))
            transcript=fixtures._write_jsonl(home/f"{SID}.jsonl",[{"role":"tool","message":{"content":[block]}}])
            for source in [store,transcript]:
                before=source.read_bytes();legacy=cursor.to_canonical(source)
                if identity.get("toolCallId")!="call":rejected(lambda:cursor.to_canonical(source,strict=True))
                else:
                    records,metadata=cursor.to_canonical(source,strict=True)
                    assert (records,metadata)==legacy
                    results=[part for row in records for part in row.get("message",{}).get("content",[])
                             if isinstance(part,dict) and part.get("type")=="tool_result"]
                    assert len(results)==1 and results[0]["tool_use_id"]=="call"
                assert source.read_bytes()==before


def test_checked_cursor_tool_results_require_a_present_payload():
    for block in ({"type":"tool-result","toolCallId":"call"},
                  {"type":"tool-result","toolCallId":"call","result":None},
                  {"type":"tool-result","toolCallId":"call","result":None,
                   "experimental_content":None}):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td);store=fixtures._make_cursor_store(home/"chats")
            raw=json.dumps({"role":"tool","content":[block]}).encode()
            key=hashlib.sha256(raw).hexdigest()
            with closing(sqlite3.connect(store)) as sql,sql:
                sql.execute("DELETE FROM blobs");sql.execute("INSERT INTO blobs VALUES (?,?)",(key,raw))
                sql.execute("UPDATE meta SET value=?",(json.dumps({"latestRootBlobId":key}),))
            transcript=fixtures._write_jsonl(home/f"{SID}.jsonl",[
                {"role":"tool","message":{"content":[block]}}])
            for source in (store,transcript):
                before=source.read_bytes();cursor.to_canonical(source)
                rejected(lambda:cursor.to_canonical(source,strict=True))
                assert source.read_bytes()==before



def test_optional_bad_usage_remains_unknown_without_rejecting_the_session():
    for key in ("usage", "tokenCount"):
        for usage in (None, {}, {"inputTokens": "not-a-number"}, {"inputTokens": True},
                      {"inputTokens": -1}, {"inputTokens": 3}):
            with tempfile.TemporaryDirectory() as td:
                home=Path(td);store=fixtures._make_cursor_store(home/"chats")
                message={"role":"assistant","content":"readable response",key:usage}
                raw=json.dumps(message).encode();identity=hashlib.sha256(raw).hexdigest()
                with closing(sqlite3.connect(store)) as sql, sql:
                    sql.execute("DELETE FROM blobs");sql.execute("INSERT INTO blobs VALUES (?,?)",(identity,raw))
                    sql.execute("UPDATE meta SET value=?",(json.dumps({"latestRootBlobId":identity}),))
                transcript=fixtures._write_jsonl(home/f"{SID}.jsonl",[
                    {"role":"assistant","message":{k:v for k,v in message.items() if k!="role"}}])
                for source in (store,transcript):
                    before=source.read_bytes();records,_=cursor.to_canonical(source,strict=True)
                    assistant=next(row["message"] for row in records if row["type"]=="assistant")
                    assert assistant["content"][0]["text"]=="readable response"
                    if usage=={"inputTokens":3}:assert assistant["usage"]["input_tokens"]==3
                    else:assert "usage" not in assistant, "unusable usage must not become measured zero"
                    assert source.read_bytes()==before


def test_checked_store_metadata_rejects_values_that_would_fabricate_path_or_time():
    for field,value in [("cwd",17),("cwd",False),("createdAtMs",True),("createdAtMs","123")]:
        with tempfile.TemporaryDirectory() as td:
            store=fixtures._make_cursor_store(Path(td)/"chats")
            path=store.parent/"meta.json";meta=json.loads(path.read_text());meta[field]=value
            path.write_text(json.dumps(meta));before=path.read_bytes()
            rejected(lambda:cursor.to_canonical(store,strict=True))
            rejected(lambda:cursor.session_metadata(store))
            assert path.read_bytes()==before
            # Missing optional facts remain valid unknowns, not manufactured values.
            meta[field]=None;path.write_text(json.dumps(meta))
            cursor.to_canonical(store,strict=True)
            cursor.session_metadata(store)


def test_checked_metadata_decoding_rejects_invalid_utf8_at_both_store_locations():
    for location in ("meta.json", "sqlite"):
        with tempfile.TemporaryDirectory() as td:
            store=fixtures._make_cursor_store(Path(td)/"chats")
            if location=="meta.json":
                path=store.parent/"meta.json";raw=path.read_bytes()
                path.write_bytes(raw.rstrip()[:-1]+b',"extra":"invalid\xfftext"}')
            else:
                with closing(sqlite3.connect(store)) as sql, sql:
                    raw=sql.execute("SELECT value FROM meta").fetchone()[0].encode()
                    sql.execute("UPDATE meta SET value=?",(raw.rstrip()[:-1]+b',"extra":"invalid\xfftext"}',))
            originals={path:path.read_bytes() for path in (store,store.parent/"meta.json")}
            rejected(lambda:cursor.to_canonical(store,strict=True))
            assert all(path.read_bytes()==raw for path,raw in originals.items())


def test_checked_codex_tools_require_consumed_native_identity():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[0][1];original=path.read_bytes()
        for kind in ('function_call','custom_tool_call','function_call_output','custom_tool_call_output'):
            valid={'type':kind,'call_id':'native-call','name':'native-tool','output':'done',
                   'arguments':'{}' if kind=='function_call' else None,'input':'native command'}
            fields=['call_id','name'] if kind.endswith('_call') else ['call_id']
            for field in fields:
                for value in (None,'',' ',17,False):
                    payload=dict(valid);payload[field]=value
                    path.write_bytes(original+json.dumps({'type':'response_item','payload':payload}).encode()+b'\n')
                    before=path.read_bytes();codex.to_canonical(path)
                    rejected(lambda:codex.to_canonical(path,strict=True))
                    assert path.read_bytes()==before
            # Both native ID spellings used by normalization remain supported.
            for key in ('call_id','id'):
                payload=dict(valid);payload.pop('call_id');payload[key]='native-call'
                path.write_bytes(original+json.dumps({'type':'response_item','payload':payload}).encode()+b'\n')
                assert codex.to_canonical(path,strict=True)==codex.to_canonical(path)


def test_checked_cursor_transcripts_reject_unknown_roles():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[1][1];original=path.read_bytes();expected=cursor.to_canonical(path)
        for role in ('future','',None,17,[]):
            path.write_bytes(json.dumps({'role':role,'message':{'content':'unsupported'}}).encode()+b'\n'+original)
            before=path.read_bytes()
            assert cursor.to_canonical(path)==expected
            rejected(lambda:cursor.to_canonical(path,strict=True))
            assert path.read_bytes()==before


def test_checked_cursor_empty_assistants_preserve_usage_and_legacy_identities():
    for content in ('',' \t',[],[{'type':'text','text':''}],[{'type':'reasoning','text':' '} ]):
        with tempfile.TemporaryDirectory() as td:
            home=Path(td)
            message={'role':'assistant','content':content,'usage':{'inputTokens':3},
                     'providerOptions':{'cursor':{'modelName':'native-model'}}}
            transcript=fixtures._write_jsonl(home/f'{SID}.jsonl',[
                {'role':'assistant','message':{k:v for k,v in message.items() if k!='role'}},
                {'role':'assistant','message':{'content':[{'type':'tool_use','name':'native-tool'}]}}])
            store=fixtures._make_cursor_store(home/'chats')
            raw=json.dumps(message).encode();identity=hashlib.sha256(raw).hexdigest()
            with closing(sqlite3.connect(store)) as sql, sql:
                sql.execute('DELETE FROM blobs');sql.execute('INSERT INTO blobs VALUES (?,?)',(identity,raw))
                sql.execute('UPDATE meta SET value=?',(json.dumps({'latestRootBlobId':identity}),))
            for source in (transcript,store):
                before=source.read_bytes();legacy,_=cursor.to_canonical(source)
                checked,_=cursor.to_canonical(source,strict=True)
                measured=[r for r in checked if r.get('message',{}).get('usage')]
                assert len(measured)==1 and measured[0]['message']['usage']['input_tokens']==3
                assert measured[0]['message']['content']==[{'type':'text','text':''}]
                assert measured[0]['message']['model']=='native-model'
                assert [r for r in checked if r not in measured]==legacy
                assert len({r['uuid'] for r in checked})==len(checked)
                assert cursor.to_canonical(source,strict=True)[0]==checked
                assert source.read_bytes()==before
            first=cursor.to_canonical(transcript,strict=True)[0]
            with transcript.open('a') as handle:
                handle.write(json.dumps({'role':'assistant','message':{'content':'later'}})+'\n')
            assert cursor.to_canonical(transcript,strict=True)[0][:-1]==first


def test_cursor_store_hex_metadata_validates_tree_without_changing_legacy_ids():
    with tempfile.TemporaryDirectory() as td:
        store=fixtures._make_cursor_store(Path(td)/'chats')
        with closing(sqlite3.connect(store)) as sql, sql:
            # Deliberately insert leaves in a different order than the tree.
            rows=sql.execute('SELECT id,data FROM blobs').fetchall()
            sql.execute('DELETE FROM blobs')
            sql.executemany('INSERT INTO blobs VALUES (?,?)',reversed(rows))
            value=sql.execute('SELECT value FROM meta').fetchone()[0]
            sql.execute('UPDATE meta SET value=?',(value.encode('utf-8').hex(),))
        before=store.read_bytes()
        expected=cursor.to_canonical(store)
        assert cursor.to_canonical(store,strict=True)==expected
        assert store.read_bytes()==before
        # Decoding hex must still expose corrupt/missing references.
        with closing(sqlite3.connect(store)) as sql, sql:
            sql.execute('UPDATE meta SET value=?',(json.dumps({'latestRootBlobId':'0'*64}).encode().hex(),))
        assert cursor.to_canonical(store)==expected
        rejected(lambda:cursor.to_canonical(store,strict=True))


def test_checked_codex_text_fields_cannot_silently_drop_supported_content():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[0][1];original=path.read_bytes()
        for shape in ('message','reasoning'):
            for text in (None,17,{},[]):
                block={'type':'output_text' if shape=='message' else 'summary_text','text':text}
                payload=({'type':'message','role':'assistant','content':[block]} if shape=='message'
                         else {'type':'reasoning','summary':[block]})
                path.write_bytes(original+json.dumps({'type':'response_item','payload':payload}).encode()+b'\n')
                codex.to_canonical(path)
                rejected(lambda:codex.to_canonical(path,strict=True))
        # String blocks and non-text input retain the established projection.
        for role in ('future',None,17):
            payload={'type':'message','role':role,'content':'unsupported role'}
            path.write_bytes(original+json.dumps({'type':'response_item','payload':payload}).encode()+b'\n')
            codex.to_canonical(path)
            rejected(lambda:codex.to_canonical(path,strict=True))
        for content in (['native text'],[{'type':'input_image','image_url':'synthetic'}],[]):
            payload={'type':'message','role':'user','content':content}
            path.write_bytes(original+json.dumps({'type':'response_item','payload':payload}).encode()+b'\n')
            assert codex.to_canonical(path,strict=True)==codex.to_canonical(path)


def test_checked_codex_keeps_text_projection_when_native_images_are_present():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[0][1];original=path.read_bytes()
        text={'type':'input_text','text':'readable synthetic prompt'}
        for content in [[text],[{'type':'input_image','image_url':'synthetic'},text]]:
            payload={'type':'message','role':'user','content':content}
            path.write_bytes(original+json.dumps({'type':'response_item','payload':payload}).encode()+b'\n')
            actual=codex.to_canonical(path,strict=True)
            assert actual==codex.to_canonical(path)
            if len(content)==1:
                expected=actual
            else:
                assert actual==expected


def test_checked_jsonl_defers_unparseable_unterminated_fragments():
    with tempfile.TemporaryDirectory() as td:
        for reader,path in sources(Path(td)):
            original=path.read_bytes();expected=reader.to_canonical(path,strict=True)
            for tail in [b'{"unfinished":',b'{bad}',b'{"bad":truX}',b'[1,]']:
                path.write_bytes(original+tail);before=path.read_bytes()
                assert reader.to_canonical(path,strict=True)==expected
                assert path.read_bytes()==before
                path.write_bytes(before+b'\n')
                rejected(lambda:reader.to_canonical(path,strict=True))


def test_checked_codex_tool_arguments_preserve_supported_values_or_fail():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[0][1];original=path.read_bytes()
        for kind,field in [('function_call','arguments'),('custom_tool_call','input')]:
            for raw in [[],17,True,None]:
                payload={'type':kind,'call_id':'native-call','name':'native-tool',field:raw}
                path.write_bytes(original+json.dumps({'type':'response_item','payload':payload}).encode()+b'\n')
                before=path.read_bytes()
                codex.to_canonical(path)
                rejected(lambda:codex.to_canonical(path,strict=True))
                assert path.read_bytes()==before
            for raw in [{}, {'path':'synthetic'}, '{"path":"synthetic"}', '[1,true,null]', 'native raw command']:
                payload={'type':kind,'call_id':'native-call','name':'native-tool',field:raw}
                path.write_bytes(original+json.dumps({'type':'response_item','payload':payload}).encode()+b'\n')
                assert codex.to_canonical(path,strict=True)==codex.to_canonical(path)
            for raw in ['{"value":NaN}','[Infinity]','1e999']:
                payload={'type':kind,'call_id':'native-call','name':'native-tool',field:raw}
                path.write_bytes(original+json.dumps({'type':'response_item','payload':payload}).encode()+b'\n')
                codex.to_canonical(path)
                rejected(lambda:codex.to_canonical(path,strict=True))


def test_idless_tool_use_exception_is_limited_to_transcripts():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);store=fixtures._make_cursor_store(home/'chats')
        message={'role':'assistant','content':[{'type':'tool_use','name':'Read','input':{}}]}
        raw=json.dumps(message).encode();identity=hashlib.sha256(raw).hexdigest()
        with closing(sqlite3.connect(store)) as sql, sql:
            sql.execute('DELETE FROM blobs');sql.execute('INSERT INTO blobs VALUES (?,?)',(identity,raw))
            sql.execute('UPDATE meta SET value=?',(json.dumps({'latestRootBlobId':identity}),))
        transcript=fixtures._write_jsonl(home/f'{SID}.jsonl',[{'role':'assistant','message':{'content':message['content']}}])
        original=store.read_bytes()
        rejected(lambda:cursor.to_canonical(store,strict=True))
        assert cursor.to_canonical(transcript,strict=True)==cursor.to_canonical(transcript)
        assert store.read_bytes()==original


def test_cursor_transcript_line_endings_preserve_records_and_byte_limits():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[1][1]
        rows=path.read_bytes().splitlines();expected=cursor.to_canonical(path,strict=True)
        for ending in (b'\n',b'\r',b'\r\n'):
            for trailing in (True,False):
                raw=ending.join(rows)+(ending if trailing else b'')
                path.write_bytes(raw)
                assert cursor.to_canonical(path,strict=True)==expected
                assert cursor.to_canonical(path)==expected
                assert path.read_bytes()==raw
        # UTF-8 byte limits still apply even when a character spans bytes.
        row=json.dumps({'role':'user','message':{'content':'界'*16}},ensure_ascii=False).encode()
        path.write_bytes(row+b'\r')
        with patch.object(cursor,'_MAX_TRANSCRIPT_LINE_BYTES',len(row)):
            rejected(lambda:cursor.to_canonical(path,strict=True))
        with patch.object(cursor,'_MAX_TRANSCRIPT_LINE_BYTES',len(row)+1):
            assert cursor.to_canonical(path,strict=True)


def test_codex_metadata_checks_committed_prefix_and_finite_json():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[0][1];original=path.read_bytes()
        expected=codex.session_metadata(path)
        for ending in (b'\n',b'\r',b'\r\n'):
            for prefix in (b'{bad}',b'null',b'[]',b'{"ignored":NaN}',b'{"ignored":1e999}',
                           b'{"type":"session_meta","payload":null}',
                           b'{"type":"session_meta","payload":17}'):
                path.write_bytes(prefix+ending+original)
                before=path.read_bytes()
                rejected(lambda:codex.session_metadata(path))
                assert codex.session_metadata(path,strict=False)==expected
                assert path.read_bytes()==before
            # Metadata probes stop at the header, before damaged body bytes.
            first=original.splitlines()[0]
            path.write_bytes(first+ending+b'{"body":"bad\xff"}'+ending)
            assert codex.session_metadata(path)==expected
            path.write_bytes(ending+first+ending)
            assert codex.session_metadata(path)==expected
        for tail in (b'{unfinished',b'{bad}'):
            path.write_bytes(tail)
            assert codex.session_metadata(path)=={}
        path.write_bytes(b'{"type":"session_meta","payload":{}}\n'+original)
        assert codex.session_metadata(path)['session_id'] is None
        assert codex.session_metadata(path,strict=False)==expected


def test_cursor_schema_version_requires_an_integer():
    with tempfile.TemporaryDirectory() as td:
        store=fixtures._make_cursor_store(Path(td)/'chats');path=store.parent/'meta.json'
        original=json.loads(path.read_text());expected=cursor.to_canonical(store,strict=True)
        for value in (True,False,1.0,'1',None,[],{},2):
            path.write_text(json.dumps({**original,'schemaVersion':value}))
            before=path.read_bytes()
            rejected(lambda:cursor.to_canonical(store,strict=True))
            rejected(lambda:cursor.to_canonical(store))
            rejected(lambda:cursor.session_metadata(store))
            assert path.read_bytes()==before
        path.write_text(json.dumps(original))
        assert cursor.to_canonical(store,strict=True)==expected
        assert cursor.session_metadata(store)['session_id']==store.parent.name


def test_discovery_skips_posix_special_files_and_keeps_regular_peers():
    if not hasattr(os,'mkfifo'):
        return  # Native Windows does not expose POSIX FIFO creation.
    with tempfile.TemporaryDirectory(prefix='nr-') as td:
        root=Path(td);good=root/'good.jsonl';good.write_text('{}\n')
        fifo=root/'p.jsonl';os.mkfifo(fifo)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
            listener.bind(str(root/'s.jsonl'))
            errors=[]
            assert discovery.paths(root,('**','*.jsonl'),errors.append)==[good]
            assert len(errors)==2


def test_checked_codex_rejects_malformed_response_envelopes():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[0][1];original=path.read_bytes()
        expected=codex.to_canonical(path)
        for payload in (None,17,False,'text',[],{}, {'type':None},{'type':17},{'type':''}):
            path.write_bytes(original+json.dumps({'type':'response_item','payload':payload}).encode()+b'\n')
            before=path.read_bytes()
            assert codex.to_canonical(path)==expected
            rejected(lambda:codex.to_canonical(path,strict=True))
            assert path.read_bytes()==before


def test_checked_codex_metadata_validates_returned_types_and_git_container():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[0][1];rows=copy.deepcopy(fixtures.CODEX_SYNTH)
        for field in ('id','cwd','originator','timestamp'):
            for value in ([],{},17,False):
                changed=copy.deepcopy(rows);changed[0]['payload'][field]=value
                fixtures._write_jsonl(path,changed);before=path.read_bytes()
                rejected(lambda:codex.session_metadata(path))
                codex.session_metadata(path,strict=False)
                rejected(lambda:codex.to_canonical(path,strict=True))
                assert path.read_bytes()==before
        for git in (17,[],False,'branch',{'branch':[]},{'branch':17}):
            changed=copy.deepcopy(rows);changed[0]['payload']['git']=git
            fixtures._write_jsonl(path,changed)
            rejected(lambda:codex.session_metadata(path))
            codex.session_metadata(path,strict=False)
            rejected(lambda:codex.to_canonical(path,strict=True))
        for stamp in ('not-a-clock','2026-01-01T00:00:00',True):
            changed=copy.deepcopy(rows);changed[0]['payload']['timestamp']=stamp
            fixtures._write_jsonl(path,changed)
            rejected(lambda:codex.session_metadata(path))
        for git in (None,{}, {'branch':None},{'branch':'native-branch'}):
            changed=copy.deepcopy(rows);changed[0]['payload'].update(git=git,timestamp=None)
            fixtures._write_jsonl(path,changed)
            meta=codex.session_metadata(path)
            assert meta['session_id']==rows[0]['payload']['id']
            assert meta['started_at']==rows[0]['timestamp']


def test_cursor_optional_metadata_text_fields_have_valid_types():
    with tempfile.TemporaryDirectory() as td:
        store=fixtures._make_cursor_store(Path(td)/'chats');path=store.parent/'meta.json'
        original=json.loads(path.read_text())
        for field in ('gitBranch','source_surface'):
            for value in ([],{},17,False):
                path.write_text(json.dumps({**original,field:value}))
                rejected(lambda:cursor.session_metadata(store))
            for value in (None,'future-value'):
                path.write_text(json.dumps({**original,field:value}))
                assert cursor.session_metadata(store)


def test_checked_codex_consumed_timestamps_are_parseable():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[0][1]
        for source in ('response','usage-only','initial-fallback'):
            for value in ('not-a-time','',17,False,[],{}):
                rows=copy.deepcopy(fixtures.CODEX_SYNTH)
                if source=='response':
                    item=next(row for row in rows if row.get('type')=='response_item' and row['payload'].get('role')=='user')
                    item['timestamp']=value
                elif source=='usage-only':
                    rows=[rows[0],{'type':'event_msg','timestamp':value,'payload':{'type':'token_count','info':{'total_token_usage':{'input_tokens':10,'cached_input_tokens':2,'output_tokens':3}}}}]
                else:
                    if not isinstance(value,str):
                        continue  # The existing initial fallback selects a text clock.
                    rows[0]['payload']['timestamp']='2026-01-01T00:00:00Z'
                    rows[0]['timestamp']=value
                    rows=[rows[0],{'type':'response_item','payload':{'type':'message','role':'user','content':'synthetic'}}]
                fixtures._write_jsonl(path,rows);before=path.read_bytes()
                codex.to_canonical(path)
                rejected(lambda:codex.to_canonical(path,strict=True))
                assert path.read_bytes()==before
        for value in (None,'2026-01-01T00:00:00.123456789012Z','2026-01-01T02:00:00+02:00'):
            rows=copy.deepcopy(fixtures.CODEX_SYNTH)
            item=next(row for row in rows if row.get('type')=='response_item' and row['payload'].get('role')=='user')
            item['timestamp']=value;fixtures._write_jsonl(path,rows)
            assert codex.to_canonical(path,strict=True)==codex.to_canonical(path)


def test_checked_cursor_rejects_finite_out_of_range_creation_times():
    with tempfile.TemporaryDirectory() as td:
        store=fixtures._make_cursor_store(Path(td)/'chats');path=store.parent/'meta.json'
        original=json.loads(path.read_text())
        for value in (10**18,-10**18,1e300,-1e300,10**400):
            path.write_text(json.dumps({**original,'createdAtMs':value}));before=path.read_bytes()
            rejected(lambda:cursor.to_canonical(store,strict=True))
            rejected(lambda:cursor.session_metadata(store))
            assert path.read_bytes()==before
        for value in (None,0,original['createdAtMs']):
            path.write_text(json.dumps({**original,'createdAtMs':value}))
            assert cursor.to_canonical(store,strict=True)==cursor.to_canonical(store)
            assert cursor.session_metadata(store)


def test_checked_codex_requires_native_session_identity_before_record_ids():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td)
        for shape in ('no-header','missing-id',None,'',' \t',False,17,[],{}):
            rows=copy.deepcopy(fixtures.CODEX_SYNTH)
            if shape=='no-header':
                rows=[row for row in rows if row.get('type')!='session_meta']
            elif shape=='missing-id':
                rows[0]['payload'].pop('id')
            else:
                rows[0]['payload']['id']=shape
            path=fixtures._write_jsonl(home/'rollout.jsonl',rows);before=path.read_bytes()
            codex.to_canonical(path)  # Existing capture remains tolerant.
            rejected(lambda:codex.to_canonical(path,strict=True))
            assert path.read_bytes()==before
        identities=[]
        for sid in ('native-session-a','native-session-b'):
            rows=copy.deepcopy(fixtures.CODEX_SYNTH);rows[0]['payload']['id']=sid
            path=fixtures._write_jsonl(home/(sid+'.jsonl'),rows)
            expected=codex.to_canonical(path)
            assert codex.to_canonical(path,strict=True)==expected
            identities.append({record['uuid'] for record in expected[0]})
        assert identities[0] and identities[1] and identities[0].isdisjoint(identities[1])


def test_checked_codex_tool_results_require_present_output():
    with tempfile.TemporaryDirectory() as td:
        path=sources(Path(td))[0][1]
        for missing in (True,False):
            rows=copy.deepcopy(fixtures.CODEX_SYNTH)
            result=next(row for row in rows if row.get('type')=='response_item'
                        and row.get('payload',{}).get('type') in
                        ('function_call_output','custom_tool_call_output'))
            if missing:
                result['payload'].pop('output',None)
            else:
                result['payload']['output']=None
            fixtures._write_jsonl(path,rows);before=path.read_bytes()
            codex.to_canonical(path)  # Existing capture remains tolerant.
            rejected(lambda:codex.to_canonical(path,strict=True))
            assert path.read_bytes()==before
        for value in ('',{},[],False,0):
            rows=copy.deepcopy(fixtures.CODEX_SYNTH)
            result=next(row for row in rows if row.get('type')=='response_item'
                        and row.get('payload',{}).get('type') in
                        ('function_call_output','custom_tool_call_output'))
            result['payload']['output']=value;fixtures._write_jsonl(path,rows)
            checked=codex.to_canonical(path,strict=True)
            assert checked==codex.to_canonical(path)


if __name__=='__main__':
    for name,fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn();print('PASS',name)
    print('ALL PASS')
