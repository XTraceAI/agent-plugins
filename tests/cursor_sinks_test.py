"""Cursor destinations share native observations but never delivery progress."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import multi_sink_test as cases
import cursor_flush
import portable_lock
from readers import cursor

SID=cases.SID
GEN="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
GEN2="aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"


def source(home):
    _,_,payload,path,*_=cases.routing.sources(home)[3]
    return payload,path


def measured(payload,generation=GEN):
    return {**payload,"generation_id":generation,"status":"completed","input_tokens":20120,
            "output_tokens":48,"cache_read_tokens":1024,"cache_write_tokens":9}


def append(path,index):
    with path.open("a") as output:
        for role,text in [("user",f"synthetic follow-up {index}"),("assistant",f"synthetic reply {index}")]:
            output.write(json.dumps({"role":role,"message":{"content":[{"type":"text","text":text}]}})+"\n")


def shared_path(home):
    return home/f".config/memhub-plugin/cursorflush/{SID}.json"


def state_path(home,name,local):
    return Path(str(cases.directory(home,name,local)).replace("/turnflush/","/cursorflush/"))/f"{SID}.json"


def state(home,name,local):
    path=state_path(home,name,local)
    return json.loads(path.read_text()) if path.exists() else {}


def command(budget=None):
    if budget is None:return [sys.executable,str(cases.routing.SCRIPTS/"cursor_flush.py"),"stop"]
    return [sys.executable,"-c",f"import cursor_flush,sys;cursor_flush.FLUSH_TIMEOUT_S={budget!r};sys.argv=['cursor_flush.py','stop'];raise SystemExit(cursor_flush.main())"]


def invoke(home,cloud,payload,*,budget=None,extra=None):
    env=cases.environment(home,cloud);env.update(extra or {})
    started=time.monotonic()
    result=subprocess.run(command(budget),env=env,input=json.dumps(payload),text=True,capture_output=True,timeout=8)
    assert result.returncode==0 and "Traceback" not in result.stderr,(result.stdout,result.stderr)
    return time.monotonic()-started


def restored(home,path):
    saved=json.loads(shared_path(home).read_text());meta=saved['cursor_meta']
    records,_=cursor.to_canonical(path,session_id=SID,cwd=meta.get('cwd'),model=meta.get('model'))
    with patch.object(cursor_flush,'STATE_DIR',shared_path(home).parent):
        cursor_flush.apply_session_state(records,SID,strict=True)
    return records


def test_cursor_failure_recovery_keeps_identical_usage_and_timestamp_pins():
    with tempfile.TemporaryDirectory() as td,cases.receiver('local',[]) as local,cases.receiver('cloud',[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0]);cloud[2]['status']=500
        invoke(home,cloud[0],measured(payload));first=json.loads(shared_path(home).read_text())
        assert first['usage_events'][GEN] and first['record_ts']
        assert GEN in state(home,'local',local[0])['sent_usage_generations']
        assert not state(home,'cloud',local[0]).get('sent_usage_generations')
        append(path,1);invoke(home,cloud[0],measured(payload,GEN2))
        second=json.loads(shared_path(home).read_text())
        assert all(second['record_ts'][key]==value for key,value in first['record_ts'].items())
        assert second['usage_events'][GEN]==first['usage_events'][GEN]
        # A stale destination file cannot override authoritative shared pins.
        stale=state(home,'cloud',local[0]);stale.update(usage_events={},record_ts={},source_kind='store')
        state_path(home,'cloud',local[0]).write_text(json.dumps(stale))
        cloud[2]['status']=200;invoke(home,cloud[0],payload)
        a=cases.routing.imports(local[1])[-1];b=cases.routing.imports(cloud[1])[-1]
        assert a['messages']==b['messages']==restored(home,path)
        assert state(home,'local',local[0])['sent_usage_generations']==state(home,'cloud',local[0])['sent_usage_generations']
        for family in (local,cloud):
            count=len(family[1]);invoke(home,cloud[0],payload);assert len(family[1])==count


def test_late_cursor_usage_enriches_the_same_records_in_both_arrival_orders():
    outcomes=[]
    for raw_first in [True,False]:
        with tempfile.TemporaryDirectory() as td,cases.receiver('local',[]) as local,cases.receiver('cloud',[]) as cloud:
            home=Path(td);payload,path=source(home);cases.configure(home,local[0])
            raw,_=cursor.to_canonical(path)
            if raw_first:
                invoke(home,cloud[0],payload)
                assert not any(row.get('message',{}).get('usage') for row in cases.routing.imports(local[1])[-1]['messages'])
            invoke(home,cloud[0],measured(payload))
            a=cases.routing.imports(local[1])[-1]['messages'];b=cases.routing.imports(cloud[1])[-1]['messages']
            assert a==b==restored(home,path)
            assert [row['uuid'] for row in a]==[row['uuid'] for row in raw]
            usages=[(row['uuid'],row['message']['usage']) for row in a if row.get('message',{}).get('usage')]
            assert len(usages)==1 and usages[0][1]['output_tokens']==48
            outcomes.append(usages)
    assert outcomes[0]==outcomes[1]


def test_cursor_surface_identity_and_old_cloud_compatibility_preserve_records():
    for surface in ['cursor-ide','Future Cursor',None]:
        with tempfile.TemporaryDirectory() as td,cases.receiver('local',[]) as local,cases.receiver('cloud',[]) as cloud:
            home=Path(td);payload,path=source(home);cases.configure(home,local[0]);cloud[2]['reject_extensions']=True
            if surface is None:
                path=cases.routing.fixtures._make_cursor_store(home/'.cursor/chats',uuid=SID)
                payload={'session_id':SID}
            elif surface!='cursor-ide':payload['source_surface']=surface
            invoke(home,cloud[0],payload)
            a=cases.routing.imports(local[1]);b=cases.routing.imports(cloud[1])
            assert len(a)==1 and len(b)==2
            assert a[0]['conversation_id']=='cursor-'+SID and a[0]['native_session_id']==SID
            assert a[0].get('source_surface')==surface
            assert 'native_session_id' not in b[-1] and 'source_surface' not in b[-1]
            assert a[0]['messages']==b[-1]['messages']
            if surface is None:
                native,_=cursor.to_canonical(path)
                assert [row.get('message',{}).get('usage') for row in a[0]['messages']]==[row.get('message',{}).get('usage') for row in native]
            else:
                assert not any(row.get('message',{}).get('usage') for row in a[0]['messages'])


def test_cursor_legacy_cloud_dormancy_cannot_disable_local_delivery():
    with tempfile.TemporaryDirectory() as td,cases.receiver('local',[]) as local,cases.receiver('cloud',[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0])
        installed=home/'installed';installed.mkdir();(installed/'scripts').symlink_to(cases.routing.SCRIPTS,target_is_directory=True)
        (installed/'.mcp.json').write_text(json.dumps({'mcpServers':{'memhub':{'url':cases.CLOUD}}}))
        old=shared_path(home);old.parent.mkdir(parents=True);held={'unsupported':True,'unsupported_at':time.time(),'transcript_revision':'legacy-cloud'};old.write_text(json.dumps(held))
        invoke(home,cloud[0],measured(payload),extra={'CLAUDE_PLUGIN_ROOT':str(installed)})
        assert len(local[1])==1 and not cloud[1]
        saved=json.loads(old.read_text());assert all(saved[key]==value for key,value in held.items())
        assert saved['usage_events'][GEN] and saved['record_ts']


def test_cursor_partial_batch_retry_holds_destination_progress_and_shared_pins():
    with tempfile.TemporaryDirectory() as td,cases.receiver('local',[]) as local,cases.receiver('cloud',[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0],active=['local'])
        for index in range(1000):append(path,index)
        local[2]['wrong_ack_at']=2;invoke(home,cloud[0],payload)
        assert not state(home,'local',local[0]).get('transcript_revision')
        before=json.loads(shared_path(home).read_text())['record_ts']
        local[2].pop('wrong_ack_at');local[1].clear()
        local[2].update(all_dropped_at=1,nested_ack=True,text_ack=True)
        invoke(home,cloud[0],payload)
        lengths=[len(batch['messages']) for batch in cases.routing.imports(local[1])]
        assert lengths==[2000,7],lengths
        assert state(home,'local',local[0])['transcript_revision']
        assert json.loads(shared_path(home).read_text())['record_ts']==before


def test_cursor_slow_cloud_uses_its_budget_after_local_capture():
    with tempfile.TemporaryDirectory() as td,cases.receiver('local',[]) as local,cases.receiver('cloud',[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0]);cloud[2]['drip']=True
        elapsed=invoke(home,cloud[0],measured(payload),budget=0.5)
        assert elapsed<1.5 and state(home,'local',local[0])['transcript_revision'],elapsed
        assert not state(home,'cloud',local[0]).get('transcript_revision')
        assert json.loads(shared_path(home).read_text())['usage_events'][GEN]


def test_cursor_observations_survive_when_every_delivery_lock_is_busy():
    with tempfile.TemporaryDirectory() as td,cases.receiver('local',[]) as local,cases.receiver('cloud',[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0]);fds=[]
        try:
            for name in ['local','cloud']:
                lock=state_path(home,name,local[0]).with_suffix('.flush.lock');lock.parent.mkdir(parents=True)
                fd=os.open(lock,os.O_RDWR|os.O_CREAT,0o600);portable_lock.lock_exclusive(fd,blocking=False);fds.append(fd)
            elapsed=invoke(home,cloud[0],measured(payload),budget=0.5)
            assert elapsed<1.5 and not local[1] and not cloud[1]
            assert json.loads(shared_path(home).read_text())['usage_events'][GEN]
        finally:
            for fd in fds:os.close(fd)
        invoke(home,cloud[0],payload)
        assert GEN in state(home,'local',local[0])['sent_usage_generations']
        assert GEN in state(home,'cloud',local[0])['sent_usage_generations']


def test_cursor_network_wait_does_not_hold_shared_observations_or_another_destination():
    with tempfile.TemporaryDirectory() as td,cases.receiver('local',[]) as local,cases.receiver('cloud',[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0]);release=threading.Event();cloud[2]['release']=release
        env=cases.environment(home,cloud[0]);first=subprocess.Popen(command(),env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            first.stdin.write(json.dumps(measured(payload)));first.stdin.close();first.stdin=None
            deadline=time.monotonic()+3
            while not cloud[1] and time.monotonic()<deadline:time.sleep(0.01)
            assert cloud[1]
            append(path,1);invoke(home,cloud[0],measured(payload,GEN2),budget=0.5)
            assert len(local[1])==2 and len(cloud[1])==1
            saved=json.loads(shared_path(home).read_text());assert set(saved['usage_events'])=={GEN,GEN2}
        finally:release.set();stdout,stderr=first.communicate(timeout=5)
        assert first.returncode==0 and 'Traceback' not in stderr
        invoke(home,cloud[0],payload)
        assert cases.routing.imports(local[1])[-1]['messages']==cases.routing.imports(cloud[1])[-1]['messages']
        assert state(home,'cloud',local[0])['sent_usage_generations']==[GEN,GEN2]


def test_delayed_destination_recovers_usage_older_than_five_hundred_twelve_generations():
    import uuid
    with tempfile.TemporaryDirectory() as td,cases.receiver('local',[]) as local,cases.receiver('cloud',[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0]);cloud[2]['status']=500
        invoke(home,cloud[0],measured(payload));saved=json.loads(shared_path(home).read_text())
        original=saved['usage_events'][GEN];usage=original['usage']
        for index in range(512):append(path,index)
        records,_=cursor.to_canonical(path,session_id=SID)
        targets=[row['uuid'] for row in records if row['type']=='assistant'][-512:]
        # Persist the same observation transitions without starting 512
        # identical child processes; both real delivery paths read this state.
        for index,target in enumerate(targets):
            generation=str(uuid.uuid5(uuid.NAMESPACE_URL,f'synthetic-generation-{index}'))
            saved['usage_events']=cursor_flush._usage_events_with(saved,generation,target,usage)
        assert len(saved['usage_events'])==513 and saved['usage_events'][GEN]==original
        shared_path(home).write_text(json.dumps(saved));invoke(home,cloud[0],payload)
        assert len(state(home,'local',local[0])['sent_usage_generations'])==513
        assert not state(home,'cloud',local[0]).get('sent_usage_generations')
        cloud[2]['status']=200;invoke(home,cloud[0],payload)
        received=cases.routing.imports(cloud[1])[-1]['messages']
        measured_rows=[row for row in received if row.get('message',{}).get('usage')]
        assert len(measured_rows)==513 and sum(row['message']['usage']['output_tokens'] for row in measured_rows)==513*48
        assert next(row for row in received if row['uuid']==original['target_uuid'])['message']['usage']==usage
        assert state(home,'local',local[0])['sent_usage_generations']==state(home,'cloud',local[0])['sent_usage_generations']
        assert json.loads(shared_path(home).read_text())['usage_events'][GEN]==original


def test_cursor_slow_preparation_preserves_deadline_and_has_no_late_state_writes():
    for operation in ["observation", "canonicalize", "redact", "store", "metadata"]:
        order=[]
        with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
            home=Path(td);payload,path=source(home);cases.configure(home,local[0])
            if operation=="store":
                path=cases.routing.fixtures._make_cursor_store(home/".cursor/chats",uuid=SID)
                payload={"session_id":SID}
            env=cases.environment(home,cloud[0]);guard=home/"guard/sitecustomize.py"
            target={"redact":"cursor_flush.redact_once", "store":"cursor_flush.current_blob_ids", "metadata":"cursor_flush.cursor_reader.session_metadata"}.get(operation,"cursor_flush.cursor_reader.to_canonical")
            with guard.open("a") as output:
                output.write("\nimport time,cursor_flush,capture_context\n"
                    f"original_prepare={target}\n"
                    "def slow_prepare(*args,**kwargs):\n"
                    "    selected=capture_context._current.get()\n"
                    f"    if (selected is None if {operation!r}=='observation' else selected is not None and selected.is_local): time.sleep(1.2)\n"
                    "    return original_prepare(*args,**kwargs)\n"
                    f"{target}=slow_prepare\n")
            result=subprocess.run([sys.executable,"-c",
                "import cursor_flush,time,sys;cursor_flush.FLUSH_TIMEOUT_S=0.8;sys.argv=['cursor_flush.py','stop'];"
                "started=time.monotonic();cursor_flush.main();assert time.monotonic()-started<1.1;"
                "saved={p:p.read_bytes() for p in cursor_flush.STATE_DIR.rglob('*.json')};time.sleep(1.3);"
                "assert saved=={p:p.read_bytes() for p in cursor_flush.STATE_DIR.rglob('*.json')},'late state write'"],
                env=env,input=json.dumps(measured(payload)),text=True,capture_output=True,timeout=5)
            assert result.returncode==0 and "Traceback" not in result.stderr,(operation,result.stderr)
            assert order==(["local","cloud"] if operation=="observation" else ["cloud"]),(operation,order)
            assert state(home,"cloud",local[0]).get("transcript_revision") or state(home,"cloud",local[0]).get("blob_ids")
            if operation!="observation":
                assert not state(home,"local",local[0]).get("transcript_revision")
                assert not state(home,"local",local[0]).get("blob_ids")


if __name__=='__main__':
    for name,fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn();print('PASS',name)
    print('ALL PASS')
