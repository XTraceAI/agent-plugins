"""Rule-event delivery through real hooks and disposable REST contract receivers.

The receiver models row accounting and retention; it is not the desktop intake.
All network traffic is restricted to loopback by the shared socket guard.
"""
from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
import capture_context
import portable_lock
import rulebook_hook as hook
import sinks


@contextlib.contextmanager
def receiver():
    requests=[];stored={};controls={"status":202,"reply":None,"metadata_only":False,"slow":False,"receipts":[]}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path,self.headers.get("Authorization"),body))
            assert self.path=="/v1/team/rulebook/fires"
            rows=body["fires"];accepted=0;rejected=[]
            if controls["status"]==202:
                for row in rows:
                    if not all(isinstance(row.get(key),str) and row[key] for key in ("fire_id","rule_id","session_id")):
                        rejected.append({"reason":"invalid identity"});continue
                    if row.get("rule_id")==controls.get("reject_rule"):
                        rejected.append({"reason":"unknown rule"});continue
                    accepted+=1 # unchanged duplicates still count as accepted INPUT rows
                    previous=stored.setdefault(row["fire_id"],{})
                    previous.update({key:value for key,value in row.items()
                                     if key!="excerpt" or not controls["metadata_only"]})
            result=controls["reply"]
            if result is None:result={"accepted":accepted,"rejected":rejected}
            controls["receipts"].append(result)
            raw=json.dumps(result).encode()
            self.send_response(controls["status"]);self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(raw)));self.end_headers()
            try:
                if controls["slow"]:
                    for byte in raw:self.wfile.write(bytes([byte]));self.wfile.flush();time.sleep(.03)
                else:self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f"http://127.0.0.1:{server.server_port}",requests,controls,stored
    finally:server.shutdown();server.server_close();thread.join()


def ledger(home):return home/".config/memhub-plugin/rulebook/ledger"


def event(identity="fire-1",**extra):
    return {"fire_id":identity,"rule_id":"rule-1","session_id":cases.SID,
            "fired_at":"2026-01-01T00:00:00Z","origin_sink":"cloud",
            "rule_source_id":"book-1","source_platform":"codex","source_surface":"Future Desktop",
            "excerpt":"synthetic excerpt",**extra}


def append(home,rows):
    directory=ledger(home);directory.mkdir(parents=True,exist_ok=True)
    path=directory/"fires.jsonl"
    with path.open("a") as stream:
        for row in rows:stream.write(json.dumps(row)+"\n")
    return path


def directory(home,name,local):
    sink=sinks.Sink(name,cases.CLOUD if name=="cloud" else local+"/mcp-server/mcp")
    return capture_context.state_directory(ledger(home),sink)


def state(home,name,local):
    path=directory(home,name,local)/".sent"
    return json.loads(path.read_text()) if path.exists() else {}


def environment(home,cloud,*,budget=None):
    env=cases.environment(home,cloud)
    guard=home/"guard/sitecustomize.py"
    with guard.open("a") as file:
        file.write("\noriginal_rest=mcp_http.rest\ndef rest(url,*args,**kwargs):\n"
                   "    if url.startswith('https://cloud.example.test/'):\n"
                   "        url="+repr(cloud)+"+url[len('https://cloud.example.test'):]\n"
                   "    return original_rest(url,*args,**kwargs)\nmcp_http.rest=rest\n")
        if budget is not None:
            file.write("import rulebook_hook\nrulebook_hook.FIRE_FLUSH_BUDGET_S="+repr(budget)+"\n")
    return env


def invoke(home,cloud,*,final=True,extra=None,budget=None):
    env=environment(home,cloud,budget=budget);env.update(extra or {})
    code="import rulebook_hook;rulebook_hook.flush_fires(final="+repr(final)+")"
    started=time.monotonic()
    command=([sys.executable,str(cases.routing.SCRIPTS/"rulebook_hook.py"),"flush",*(["final"] if final else [])]
             if budget is None else [sys.executable,"-c",code])
    result=subprocess.run(command,env=env,capture_output=True,text=True,timeout=8)
    assert result.returncode==0 and "Traceback" not in result.stderr,(result.stdout,result.stderr)
    return time.monotonic()-started


def health(home,cloud):
    result=subprocess.run([sys.executable,str(cases.routing.SCRIPTS/"capture_health.py")],
                          env=environment(home,cloud),input="{}",capture_output=True,text=True,timeout=5)
    assert result.returncode==0 and "Traceback" not in result.stderr,result.stderr
    return result.stdout


def test_duplicate_rows_are_accounted_and_each_destination_recovers_independently():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0]);path=append(home,[event(),event(),event("fire-2")]);original=path.read_bytes()
        cloud[2]["status"]=500;invoke(home,cloud[0])
        assert len(local[1][0][2]["fires"])==3 and len(local[3])==2
        assert state(home,"local",local[0])["fires_offset"]==len(original)
        assert state(home,"cloud",local[0]).get("fires_offset",0)==0
        message=health(home,cloud[0]);assert "Rule-fire capture destination 'cloud'" in message and "Rule-fire capture destination 'local'" not in message
        cloud[2]["status"]=202;invoke(home,cloud[0])
        assert len(local[1])==1 and len(cloud[1])==2 and len(cloud[3])==2
        assert state(home,"cloud",local[0])["fires_offset"]==len(original)
        assert "Rule-fire capture destination" not in health(home,cloud[0])
        assert path.read_bytes()==original


def test_local_projection_redacts_caps_and_preserves_evaluation_source_and_cloud_shape():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0],active=["local","cloud"])
        secret="mhk_"+"A"*40
        path=append(home,[event(excerpt=secret+"λ"*3000),event("unknown",origin_sink=None,rule_source_id=None,source_platform=None,source_surface=None)])
        original=path.read_bytes();invoke(home,cloud[0])
        a,b=local[1][0][2]["fires"],cloud[1][0][2]["fires"]
        assert len(a[0]["excerpt"])==2048 and secret not in a[0]["excerpt"]
        assert a[0]["origin_sink"]=="cloud" and a[0]["rule_source_id"]=="book-1"
        assert a[0]["source_surface"]=="Future Desktop"
        assert not {"origin_sink","rule_source_id","source_platform","source_surface"}.intersection(a[1])
        assert all(set(row)==set(hook.WIRE_KEYS) for row in b)
        # Projection itself is safe in either order even though live delivery
        # intentionally reserves the first share of the budget for loopback.
        row=json.loads(path.read_text().splitlines()[0]);before=json.loads(json.dumps(row))
        for order in [(False,True),(True,False)]:
            projected={local:hook.wire_row(row,local=local) for local in order}
            assert projected[True]["origin_sink"]=="cloud" and projected[True]["rule_source_id"]=="book-1"
            assert set(projected[False])==set(hook.WIRE_KEYS) and secret not in projected[True]["excerpt"]
            assert row==before
        assert path.read_bytes()==original


def test_valid_duplicate_replay_is_acknowledged_as_three_inputs_and_two_stored_fires():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0]);path=append(home,[event(),event(),event("fire-2")])
        invoke(home,cloud[0])
        for name in ("local","cloud"):(directory(home,name,local[0])/".sent").unlink()
        invoke(home,cloud[0]) # server committed but client lost the receipt
        for destination in (local,cloud):
            assert destination[2]["receipts"]==[{"accepted":3,"rejected":[]}]*2
            assert len(destination[3])==2
        assert state(home,"local",local[0])["fires_offset"]==state(home,"cloud",local[0])["fires_offset"]==path.stat().st_size


def test_unknown_cloud_rule_is_accounted_without_rejecting_the_local_fire():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0]);path=append(home,[event()]);cloud[2]["reject_rule"]="rule-1"
        invoke(home,cloud[0])
        assert len(local[3])==1 and cloud[3]=={}
        assert cloud[2]["receipts"][0]=={"accepted":0,"rejected":[{"reason":"unknown rule"}]}
        assert state(home,"local",local[0])["fires_offset"]==state(home,"cloud",local[0])["fires_offset"]==path.stat().st_size
        assert not (directory(home,"local",local[0])/"rejected.jsonl").exists()
        assert (directory(home,"cloud",local[0])/"rejected.jsonl").is_file()


def test_invalid_rows_are_rejected_individually_without_collapsing_valid_duplicates():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0],active=["local"])
        path=append(home,[event(),event(),event("bad",session_id=None),{"fire_id":[]},None])
        invoke(home,cloud[0]);rows=local[1][0][2]["fires"]
        assert len(rows)==5 and len(local[3])==1
        assert state(home,"local",local[0])["fires_offset"]==path.stat().st_size
        rejected=directory(home,"local",local[0])/"rejected.jsonl"
        assert len(rejected.read_text().splitlines())==3
        invoke(home,cloud[0]);assert len(local[1])==1


def test_receipt_counts_must_be_integer_nonnegative_and_no_more_than_input_rows():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0],active=["local"]);append(home,[event()])
        for reply in [{"accepted":True,"rejected":0},{"accepted":-1,"rejected":0},
                      {"accepted":2,"rejected":0},{"accepted":1,"rejected":True},
                      {"accepted":1,"rejected":-1},{"accepted":1,"rejected":1}]:
            local[2]["reply"]=reply;invoke(home,cloud[0])
            assert state(home,"local",local[0]).get("fires_offset",0)==0
        local[2]["reply"]=None;invoke(home,cloud[0])
        assert state(home,"local",local[0])["fires_offset"]>0


def test_short_count_quarantine_and_throttle_are_destination_local():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0]);path=append(home,[event()])
        cloud[2]["reply"]={"accepted":0,"rejected":0}
        for _ in range(3):invoke(home,cloud[0])
        assert len(local[1])==1 and len(cloud[1])==3
        assert state(home,"cloud",local[0])["fires_offset"]==path.stat().st_size
        assert (directory(home,"cloud",local[0])/"rejected.jsonl").is_file()
        assert not (directory(home,"local",local[0])/"rejected.jsonl").exists()
        append(home,[event("fire-2")]);invoke(home,cloud[0],final=False)
        assert len(local[1])==1 and len(cloud[1])==3
        cloud[2]["reply"]=None;invoke(home,cloud[0])
        assert len(local[1])==2 and len(cloud[1])==4


def test_installed_cloud_alone_adopts_legacy_progress_and_cloud_lock_never_blocks_local():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0]);path=append(home,[event()])
        installed=home/"installed";installed.mkdir();(installed/"scripts").symlink_to(cases.routing.SCRIPTS,target_is_directory=True)
        (installed/".mcp.json").write_text(json.dumps({"mcpServers":{"memhub":{"url":cases.CLOUD}}}))
        legacy=ledger(home)/".sent";legacy.write_text(json.dumps({"fires_offset":path.stat().st_size}))
        invoke(home,cloud[0],extra={"CLAUDE_PLUGIN_ROOT":str(installed)})
        assert len(local[1])==1 and cloud[1]==[]
        append(home,[event("fire-2")]);lock=ledger(home)/".flush.lock"
        with lock.open("a+") as handle:
            portable_lock.lock_exclusive(handle.fileno(),blocking=False)
            try:invoke(home,cloud[0],extra={"CLAUDE_PLUGIN_ROOT":str(installed)})
            finally:portable_lock.unlock(handle.fileno())
        assert len(local[1])==2 and cloud[1]==[]
        assert json.loads(legacy.read_text())["fires_offset"]<path.stat().st_size
        invoke(home,cloud[0],extra={"CLAUDE_PLUGIN_ROOT":str(installed)})
        assert len(cloud[1])==1 and cloud[1][0][2]["fires"][0]["fire_id"]=="fire-2"


def test_slow_cloud_body_cannot_delay_local_or_hold_the_hook_process():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0]);append(home,[event()]);cloud[2]["slow"]=True
        elapsed=invoke(home,cloud[0],budget=.5)
        assert elapsed<1.5,elapsed
        assert state(home,"local",local[0])["fires_offset"]>0
        assert state(home,"cloud",local[0]).get("fires_offset",0)==0


def test_metadata_only_receiver_discards_new_content_without_erasing_existing_excerpt():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0],active=["local"]);append(home,[event()]);invoke(home,cloud[0])
        local[2]["metadata_only"]=True
        append(home,[event(excerpt="enrichment",converted=True),event("fire-2",excerpt="new content",ask_result="unrequested content")]);invoke(home,cloud[0])
        assert local[3]["fire-1"]["excerpt"]=="synthetic excerpt" and local[3]["fire-1"]["converted"] is True
        assert "excerpt" not in local[3]["fire-2"] and "ask_result" not in local[3]["fire-2"]


def test_new_ledger_rows_record_source_once_before_any_destination_is_bound():
    with tempfile.TemporaryDirectory() as td,patch.object(hook,"BASE",td):
        ctx={"rule_version":"v1","session":cases.SID,"agent_id":None,"repo":"synthetic",
             "branch":"main","tool":"Bash","source_platform":"codex","source_surface":"Future Desktop"}
        rules=[{"id":"rule-1","_origin_sink":"cloud","_rule_source_id":"book-1"}]
        ids=hook.log_fires(ctx,rules,hook_phase="pre",mode="advise",excerpt="mhk_"+"B"*40)
        row=json.loads((Path(td)/"ledger/fires.jsonl").read_text())
        assert row["fire_id"]==ids["rule-1"] and row["origin_sink"]=="cloud" and row["rule_source_id"]=="book-1"
        assert "mhk_"+"B"*40 not in row["excerpt"]
        with capture_context.bind(sinks.Sink("local","http://127.0.0.1:47421/mcp","local")):
            assert hook.wire_row(row)["origin_sink"]=="cloud"


def test_recovered_batch_does_not_restore_stale_quarantine_state_on_later_batches():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0]);path=append(home,[event(f"fire-{i}") for i in range(201)])
        cloud[2]["reply"]={"accepted":0,"rejected":0}
        invoke(home,cloud[0]);invoke(home,cloud[0])
        assert state(home,"cloud",local[0])["stall"]["n"]==2
        cloud[2]["reply"]=None;invoke(home,cloud[0])
        assert state(home,"cloud",local[0])["fires_offset"]==path.stat().st_size
        assert "stall" not in state(home,"cloud",local[0])
        assert [len(request[2]["fires"]) for request in cloud[1]]==[200,200,200,1]
        assert len(local[1])==2


def test_duplicate_rows_keep_their_own_fields_until_a_conversion_is_observed():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0],active=["local"])
        append(home,[event(converted=True,converted_at="2026-01-01T00:00:00Z"),event(converted=None)])
        invoke(home,cloud[0]);rows=local[1][0][2]["fires"]
        assert rows[0]["converted"] is True and rows[1]["converted"] is None
        with (ledger(home)/"conversions.jsonl").open("w") as file:
            file.write(json.dumps({"fire_id":"fire-1","converted":True,"converted_at":"2026-01-02T00:00:00Z"})+"\n")
        invoke(home,cloud[0]);row=local[1][1][2]["fires"][0]
        assert row["converted"] is True and row["converted_at"]=="2026-01-02T00:00:00Z"


def test_executable_claude_hook_records_known_platform_and_observed_entrypoint():
    with tempfile.TemporaryDirectory() as td,receiver() as local,receiver() as cloud:
        home=Path(td);cases.configure(home,local[0]);repo=home/"synthetic"
        (repo/".git").mkdir(parents=True);(repo/".git/HEAD").write_text("ref: refs/heads/main\n")
        base=home/".config/memhub-plugin/rulebook"
        with patch.object(hook,"BOOK_DIR",str(base/"book")):cache=Path(hook.book_path(repo.name))
        cache.parent.mkdir(parents=True,exist_ok=True)
        cache.write_text(json.dumps({"fetched_at":"2026-01-01T00:00:00Z","rules":[{
            "rule_id":"rule-provenance","statement":"Synthetic advice","mode":"advise","version":1,
            "status":"active","scope_repos":[],"matcher":{"event":"bash","command_rx":"synthetic-command"}}]}))
        observations=[({},None),({"entrypoint":"Future Claude Desktop"},"Future Claude Desktop"),
                      ({"source_surface":"Explicit surface","entrypoint":"fallback"},"Explicit surface"),
                      ({"source_surface":[],"entrypoint":"cli"},"cli")]
        for index,(extra,expected) in enumerate(observations):
            payload={"session_id":f"synthetic-{index}","cwd":str(repo),"tool_name":"Bash",
                     "tool_input":{"command":"synthetic-command"},**extra}
            cases.routing.invoke(home,"rulebook_hook.py",payload,"pre",override={
                "MEMHUB_RULEBOOK_BASE":str(base),"MEMHUB_RULEBOOK_FETCH":"0"})
            row=json.loads((ledger(home)/"fires.jsonl").read_text().splitlines()[-1])
            assert row["session_id"]==payload["session_id"] and row["source_platform"]=="claude"
            assert row["source_surface"]==expected and row["origin_sink"]=="cloud"
        invoke(home,cloud[0])
        rows=local[1][0][2]["fires"]
        assert len(rows)==4 and all(row["source_platform"]=="claude" for row in rows)
        assert [row.get("source_surface") for row in rows]==[value for _,value in observations]
        assert all(set(row)==set(hook.WIRE_KEYS) for row in cloud[1][0][2]["fires"])


if __name__=="__main__":
    for name,fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn();print("PASS",name)
    print("ALL PASS")
