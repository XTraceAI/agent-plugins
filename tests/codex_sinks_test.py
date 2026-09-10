"""Codex fan-out uses real hooks, canonical fixtures and isolated receivers."""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import multi_sink_test as cases
import portable_lock
from readers import codex

SID=cases.SID


def source(home):
    _,_,payload,path,*_=cases.routing.sources(home)[2]
    return payload,path


def append(path,text="synthetic follow-up"):
    row={"timestamp":"2026-01-01T00:02:00.000Z","type":"response_item",
         "payload":{"type":"message","role":"user","content":[{"type":"input_text","text":text}]}}
    with path.open("a") as output:output.write(json.dumps(row)+"\n")


def state_path(home,name,local):
    directory=cases.directory(home,name,local)
    return Path(str(directory).replace("/turnflush/","/codexflush/"))/f"{SID}.json"


def state(home,name,local):
    path=state_path(home,name,local)
    return json.loads(path.read_text()) if path.exists() else {}


def invoke(home,cloud,payload,*,budget=None):
    env=cases.environment(home,cloud)
    command=[sys.executable,str(cases.routing.SCRIPTS/"codex_flush.py"),"Stop"]
    if budget is not None:
        command=[sys.executable,"-c",f"import codex_flush,sys;codex_flush.FLUSH_TIMEOUT_S={budget!r};sys.argv=['codex_flush.py','Stop'];raise SystemExit(codex_flush.main())"]
    started=time.monotonic()
    result=subprocess.run(command,env=env,input=json.dumps(payload),text=True,capture_output=True,timeout=8)
    assert result.returncode==0 and "Traceback" not in result.stderr,(result.stdout,result.stderr)
    return time.monotonic()-started


def test_codex_cloud_failure_and_recovery_keep_local_progress_independent():
    order=[]
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0]);cloud[2]["status"]=500
        for turn in range(3):
            if turn:append(path,str(turn))
            invoke(home,cloud[0],payload)
            assert state(home,"local",local[0])["rollout_size"]==path.stat().st_size
            assert not state(home,"cloud",local[0]).get("rollout_size")
        assert order==["local","cloud"]*3
        cloud[2]["status"]=200;invoke(home,cloud[0],payload)
        assert state(home,"cloud",local[0])["rollout_size"]==path.stat().st_size
        a=cases.routing.imports(local[1])[-1];b=cases.routing.imports(cloud[1])[-1]
        assert a["messages"]==b["messages"]
        count=len(order);invoke(home,cloud[0],payload);assert len(order)==count
        append(path);local[2]["status"]=500;invoke(home,cloud[0],payload)
        assert state(home,"cloud",local[0])["rollout_size"]==path.stat().st_size
        assert state(home,"local",local[0])["rollout_size"]<path.stat().st_size


def test_codex_unsupported_state_is_per_destination_and_reprobe_preserves_ids():
    order=[]
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0]);cloud[2]["ack"]=False
        invoke(home,cloud[0],payload);assert state(home,"cloud",local[0])["unsupported"]
        append(path);invoke(home,cloud[0],payload)
        assert len(local[1])==2 and len(cloud[1])==1
        p=state_path(home,"cloud",local[0]);saved=json.loads(p.read_text());saved["unsupported_at"]=0;p.write_text(json.dumps(saved))
        cloud[2]["ack"]=True;invoke(home,cloud[0],payload)
        assert not state(home,"cloud",local[0])["unsupported"]
        assert cases.routing.imports(local[1])[-1]["messages"]==cases.routing.imports(cloud[1])[-1]["messages"]


def test_codex_raw_originator_native_identity_and_old_cloud_match_reader_goldens():
    for originator in ["codex_cli_rs","Codex Desktop","Future Surface",None]:
        with tempfile.TemporaryDirectory() as td,cases.receiver("local",[]) as local,cases.receiver("cloud",[]) as cloud:
            home=Path(td);payload,path=source(home);cases.configure(home,local[0]);cloud[2]["reject_extensions"]=True
            rows=[json.loads(line) for line in path.read_text().splitlines()]
            if originator is None:rows[0]["payload"].pop("originator",None)
            else:rows[0]["payload"]["originator"]=originator
            path.write_text("".join(json.dumps(row)+"\n" for row in rows));before=path.read_bytes()
            expected,_=codex.to_canonical(path)
            invoke(home,cloud[0],payload)
            a=cases.routing.imports(local[1]);b=cases.routing.imports(cloud[1])
            assert len(a)==1 and len(b)==2
            assert a[0]["conversation_id"]=="codex-"+SID and a[0]["native_session_id"]==SID
            assert a[0].get("source_surface")==originator
            assert "native_session_id" not in b[-1] and "source_surface" not in b[-1]
            assert a[0]["messages"]==b[-1]["messages"]==expected
            assert path.read_bytes()==before


def test_codex_cloud_legacy_watermark_cannot_skip_local_capture():
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",[]) as local,cases.receiver("cloud",[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0])
        installed=home/"installed";installed.mkdir();(installed/"scripts").symlink_to(cases.routing.SCRIPTS,target_is_directory=True);(installed/".mcp.json").write_text(json.dumps({"mcpServers":{"memhub":{"url":cases.CLOUD}}}))
        old=home/f".config/memhub-plugin/codexflush/{SID}.json";old.parent.mkdir(parents=True);old.write_text(json.dumps({"rollout_size":path.stat().st_size}))
        env=cases.environment(home,cloud[0]);env["CLAUDE_PLUGIN_ROOT"]=str(installed)
        result=subprocess.run([sys.executable,str(cases.routing.SCRIPTS/"codex_flush.py"),"Stop"],env=env,input=json.dumps(payload),text=True,capture_output=True,timeout=8)
        assert result.returncode==0 and len(local[1])==1 and not cloud[1], (result.stdout,result.stderr,len(local[1]),len(cloud[1]))
        assert json.loads(old.read_text())["rollout_size"]==path.stat().st_size


def test_codex_batching_and_wrong_ack_hold_the_whole_rollout_watermark():
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",[]) as local,cases.receiver("cloud",[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0],active=["local"])
        for index in range(2000):append(path,str(index))
        local[2]["wrong_ack_at"]=2;invoke(home,cloud[0],payload)
        assert not state(home,"local",local[0]).get("rollout_size")
        local[2].pop("wrong_ack_at");local[1].clear()
        local[2].update(all_dropped_at=1,nested_ack=True)
        invoke(home,cloud[0],payload)
        lengths=[len(batch["messages"]) for batch in cases.routing.imports(local[1])]
        assert lengths==[2000,5],lengths
        assert state(home,"local",local[0])["rollout_size"]==path.stat().st_size


def test_codex_slow_cloud_exits_within_shared_budget_after_local_success():
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",[]) as local,cases.receiver("cloud",[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0]);cloud[2]["drip"]=True
        elapsed=invoke(home,cloud[0],payload,budget=0.5)
        assert elapsed<1.5,elapsed
        assert state(home,"local",local[0])["rollout_size"]==path.stat().st_size
        assert not state(home,"cloud",local[0]).get("rollout_size")


def test_codex_cloud_lock_wait_is_bounded_without_borrowing_local_progress():
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",[]) as local,cases.receiver("cloud",[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0])
        lock=state_path(home,"cloud",local[0]).with_suffix(".flush.lock");lock.parent.mkdir(parents=True)
        fd=os.open(lock,os.O_RDWR|os.O_CREAT,0o600)
        try:
            portable_lock.lock_exclusive(fd,blocking=False)
            elapsed=invoke(home,cloud[0],payload,budget=0.5)
            assert elapsed<1.5 and len(local[1])==1 and not cloud[1]
        finally:os.close(fd)
        invoke(home,cloud[0],payload)
        assert len(local[1])==1 and len(cloud[1])==1
        assert state(home,"cloud",local[0])["rollout_size"]==path.stat().st_size


def test_codex_one_unwritable_destination_does_not_abort_the_other():
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",[]) as local,cases.receiver("cloud",[]) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0])
        directory=state_path(home,"local",local[0]).parent;directory.parent.mkdir(parents=True);directory.write_text("not a directory")
        invoke(home,cloud[0],payload)
        assert not local[1] and len(cloud[1])==1
        assert state(home,"cloud",local[0])["rollout_size"]==path.stat().st_size


def test_codex_slow_local_preparation_preserves_cloud_budget_without_late_progress():
    for operation in ["canonicalize","redact"]:
        order=[]
        with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
            home=Path(td);payload,path=source(home);cases.configure(home,local[0])
            env=cases.environment(home,cloud[0]);guard=home/"guard/sitecustomize.py"
            target="codex_flush.codex_reader.to_canonical" if operation=="canonicalize" else "codex_flush.redact_once"
            with guard.open("a") as output:
                output.write("\nimport time,codex_flush,capture_context\n"
                    f"original_prepare={target}\n"
                    "def slow_prepare(*args,**kwargs):\n"
                    "    if capture_context._current.get().is_local: time.sleep(1.2)\n"
                    "    return original_prepare(*args,**kwargs)\n"
                    f"{target}=slow_prepare\n")
            started=time.monotonic()
            result=subprocess.run([sys.executable,"-c",
                "import codex_flush,time,sys;codex_flush.FLUSH_TIMEOUT_S=0.8;sys.argv=['codex_flush.py','Stop'];codex_flush.main();time.sleep(1.3)"],
                env=env,input=json.dumps(payload),text=True,capture_output=True,timeout=5)
            assert result.returncode==0 and "Traceback" not in result.stderr,result.stderr
            assert time.monotonic()-started<2.5
            assert order==["cloud"],(operation,order)
            assert state(home,"cloud",local[0])["rollout_size"]==path.stat().st_size
            assert not state(home,"local",local[0]).get("rollout_size")
            assert "TimeoutError" in state(home,"local",local[0])["last_error"]


def test_codex_reuses_prepared_metadata_without_a_second_filesystem_probe():
    order=[]
    with tempfile.TemporaryDirectory() as td,cases.receiver("local",order) as local,cases.receiver("cloud",order) as cloud:
        home=Path(td);payload,path=source(home);cases.configure(home,local[0])
        env=cases.environment(home,cloud[0]);guard=home/"guard/sitecustomize.py"
        with guard.open("a") as output:
            output.write("\nimport codex_flush\n"
                         "def forbidden_probe(*args,**kwargs): raise AssertionError('metadata was already prepared')\n"
                         "codex_flush.codex_reader.session_metadata=forbidden_probe\n")
        result=subprocess.run([sys.executable,str(cases.routing.SCRIPTS/"codex_flush.py"),"Stop"],
            env=env,input=json.dumps(payload),text=True,capture_output=True,timeout=8)
        assert result.returncode==0 and order==["local","cloud"],(order,result.stderr)
        for receiver in [local,cloud]:
            assert cases.routing.imports(receiver[1])[0].get("source_surface")==json.loads(path.read_text().splitlines()[0])["payload"].get("originator")


if __name__=="__main__":
    for name,fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn();print("PASS",name)
    print("ALL PASS")
