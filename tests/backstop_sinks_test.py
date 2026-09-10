"""Claude backstop fan-out, provenance and failure isolation through real hooks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import multi_sink_test as cases
import capture_context

SID=cases.SID


def state(home,name,local):
    path=cases.directory(home,name,local)/f"{SID}.sessionflush.json"
    return json.loads(path.read_text()) if path.exists() else {}


def backstop(home,cloud,data,**kwargs):
    return cases.invoke(home,cloud,data,script="flush_session.py",**kwargs)


def test_backstop_reaches_both_after_turn_capture_and_replays_logically_once():
    order=[]
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
        home=Path(td);data=cases.payload(home);cases.configure(home,local[0])
        cases.invoke(home,cloud[0],data)
        order.clear();backstop(home,cloud[0],data);backstop(home,cloud[0],data)
        assert order==["local","cloud","local","cloud"]
        for destination in (local,cloud):
            batches=cases.routing.imports(destination[1])
            assert len(batches)==3 and len({row["uuid"] for batch in batches for row in batch["messages"]})==1
        previous=state(home,"cloud",local[0]);assert previous["last_ok_at"]
        cloud[2]["status"]=500;cases.append(Path(data["transcript_path"]),1)
        backstop(home,cloud[0],data)
        assert state(home,"local",local[0])["last_ok_at"] > 0
        assert state(home,"cloud",local[0])["last_error"]=="error"
        cloud[2]["status"]=200;backstop(home,cloud[0],data)
        assert state(home,"cloud",local[0])["last_ok_at"] > previous["last_ok_at"]
        assert cases.routing.imports(local[1])[-1]["messages"]==cases.routing.imports(cloud[1])[-1]["messages"]


def test_old_cloud_fallback_preserves_provenance_and_local_native_surface():
    order=[]
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
        home=Path(td);data=cases.payload(home);cases.configure(home,local[0]);cloud[2]["reject_extensions"]=True
        url="https://github.com/example/project/pull/42"
        rows=[{"uuid":"call","type":"assistant","message":{"role":"assistant","content":[
            {"type":"tool_use","id":"call-1","name":"Bash","input":{"command":"gh pr create --fill"}}]}},
            {"uuid":"result","type":"user","message":{"role":"user","content":[
            {"type":"tool_result","tool_use_id":"call-1","content":url}]}}]
        with Path(data["transcript_path"]).open("a") as output:
            for row in rows:output.write(json.dumps(row)+"\n")
        data["tool_input"]={"command":"gh pr create --fill"}
        backstop(home,cloud[0],data)
        a=cases.routing.imports(local[1]);b=cases.routing.imports(cloud[1])
        assert len(a)==1 and len(b)==2
        assert a[0]["native_session_id"]==SID and a[0]["source_surface"]=="Future Claude"
        assert "native_session_id" not in b[1] and "source_surface" not in b[1]
        assert a[0]["messages"]==b[0]["messages"]==b[1]["messages"]
        assert all(item["provenance"]=={"github_pr_urls":[url]} for item in a+b)
        assert state(home,"local",local[0])["last_ok_at"] and state(home,"cloud",local[0])["last_ok_at"]


def test_missing_surface_and_legacy_non_durable_cloud_are_explicit():
    order=[]
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
        home=Path(td);data=cases.payload(home);data.pop("entrypoint");cases.configure(home,local[0]);cloud[2]["ack"]=False
        backstop(home,cloud[0],data)
        assert all("source_surface" not in batch for batch in cases.routing.imports(local[1])+cases.routing.imports(cloud[1]))
        assert state(home,"cloud",local[0])["last_error"]=="unrecognized_response"
        assert not state(home,"cloud",local[0]).get("last_ok_at")
        installed=home/"installed";installed.mkdir()
        (installed/"scripts").symlink_to(cases.routing.SCRIPTS,target_is_directory=True)
        (installed/".mcp.json").write_text(json.dumps({"mcpServers":{"memhub":{"url":cases.CLOUD}}}))
        extra={"CLAUDE_PLUGIN_ROOT":str(installed)}
        backstop(home,cloud[0],data,extra=extra)
        legacy=home/f".config/memhub-plugin/turnflush/{SID}.sessionflush.json"
        assert json.loads(legacy.read_text())["last_ok_at"]
        local[2]["ack"]=False;backstop(home,cloud[0],data,extra=extra)
        assert state(home,"local",local[0])["last_error"]=="unrecognized_response"


def test_cloud_slow_drip_cannot_delay_local_or_hold_the_backstop_process():
    order=[]
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
        home=Path(td);data=cases.payload(home);cases.configure(home,local[0]);cloud[2]["drip"]=True
        elapsed=backstop(home,cloud[0],data,extra={"MEMHUB_FLUSH_DEADLINE_S":"0.5"})
        assert elapsed < 1.5,elapsed
        assert state(home,"local",local[0])["last_ok_at"]
        assert state(home,"cloud",local[0])["last_error"]=="timeout"


def test_backstop_batches_at_most_two_thousand_records_and_reports_bad_ack():
    order=[]
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
        home=Path(td);data=cases.payload(home,2001);cases.configure(home,local[0],active=["local"])
        backstop(home,cloud[0],data)
        assert [len(batch["messages"]) for batch in cases.routing.imports(local[1])]==[2000,1]
        local[2]["wrong_ack"]=True;backstop(home,cloud[0],data)
        assert state(home,"local",local[0])["last_error"]=="unrecognized_response"


def test_concurrent_backstops_own_separate_destination_locks():
    order=[]
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
        home=Path(td);data=cases.payload(home);cases.configure(home,local[0]);release=threading.Event();cloud[2]["release"]=release
        env=cases.environment(home,cloud[0])
        first=subprocess.Popen([sys.executable,str(cases.routing.SCRIPTS/"flush_session.py")],env=env,
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            first.stdin.write(json.dumps(data));first.stdin.close();first.stdin=None
            deadline=time.monotonic()+3
            while not cloud[1] and time.monotonic()<deadline:time.sleep(0.01)
            assert cloud[1]
            backstop(home,cloud[0],data)
            assert len(cloud[1])==1 and len(local[1])==2
        finally:
            release.set();stdout,stderr=first.communicate(timeout=5)
        assert first.returncode==0 and "Traceback" not in stderr
        assert state(home,"local",local[0])["last_ok_at"] and state(home,"cloud",local[0])["last_ok_at"]


def test_nested_and_text_acknowledgements_confirm_the_backstop_slice():
    order=[]
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
        home=Path(td);data=cases.payload(home);cases.configure(home,local[0],active=["local"])
        for text in (False,True):
            local[2].update(nested_ack=True,text_ack=text,all_dropped=True)
            backstop(home,cloud[0],data)
            assert state(home,"local",local[0])["last_ok_at"] and not state(home,"local",local[0]).get("last_error")
        local[2]["wrong_ack"]=True;backstop(home,cloud[0],data)
        assert state(home,"local",local[0])["last_error"]=="unrecognized_response"


def test_slow_local_preparation_preserves_cloud_budget_and_cannot_commit_late():
    order=[]
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
        home=Path(td);data=cases.payload(home);cases.configure(home,local[0])
        env=cases.environment(home,cloud[0]);env["MEMHUB_FLUSH_DEADLINE_S"]="0.8"
        guard=home/"guard/sitecustomize.py"
        with guard.open("a") as output:
            output.write("\nimport time,flush_session,capture_context\n"
                         "original_prepare=flush_session._prepare_transcript\n"
                         "def slow_prepare(path):\n"
                         "    if capture_context._current.get().is_local: time.sleep(1.2)\n"
                         "    return original_prepare(path)\n"
                         "flush_session._prepare_transcript=slow_prepare\n")
        started=time.monotonic()
        result=subprocess.run([sys.executable,"-c",
            "import flush_session,time; flush_session.main(); time.sleep(1.3)"],
            env=env,input=json.dumps(data),text=True,capture_output=True,timeout=5)
        assert result.returncode==0 and "Traceback" not in result.stderr,result.stderr
        assert time.monotonic()-started<2.5
        assert order==["cloud"],order
        assert state(home,"cloud",local[0])["last_ok_at"]
        assert state(home,"local",local[0])["last_error"]=="timeout"
        assert not state(home,"local",local[0]).get("last_ok_at")


def test_long_native_ids_capture_through_both_claude_hooks_with_bounded_state_names():
    for length in (200,201,237,238,256):
        with tempfile.TemporaryDirectory() as td,cases.receiver("local",[]) as local,cases.receiver("cloud",[]) as cloud:
            home=Path(td);data=cases.payload(home);cases.configure(home,local[0])
            sid="s"*length;data["session_id"]=sid
            cases.invoke(home,cloud[0],data);backstop(home,cloud[0],data)
            for receiver in (local,cloud):
                batches=cases.routing.imports(receiver[1])
                assert len(batches)==2,(length,len(batches))
                assert all(batch["conversation_id"]==sid for batch in batches)
            for name in ("local","cloud"):
                folder=cases.directory(home,name,local[0]);files=list(folder.iterdir())
                assert all(len(path.name.encode())<=255 for path in files)
                turn=next(path for path in files if path.suffix==".json" and ".sessionflush." not in path.name)
                stop=next(path for path in files if path.name.endswith(".sessionflush.json"))
                assert json.loads(turn.read_text())["offset"]>0
                assert json.loads(stop.read_text())["last_ok_at"]>0


if __name__=="__main__":
    for name,fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn();print("PASS",name)
    print("ALL PASS")
