"""Per-turn fan-out through real hooks and two disposable HTTP receivers.

Only the fake cloud's logical HTTPS address is mapped to loopback, at the
existing transport boundary. Selection, credentials, MCP framing and hooks
remain real; a socket guard forbids external requests.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import capture_routing_test as routing
import capture_async
import capture_context
import capture_redaction
import capture_health
import flush_turn as ft
import mcp_http
import portable_lock
import sinks

CLOUD = "https://cloud.example.test/mcp-server/mcp"
SID = routing.SID


@contextlib.contextmanager
def receiver(label, order):
    requests = []
    controls = {"status": 200, "ack": True, "reject_extensions": False,
                "error": None, "drip": False, "release": None, "wrong_ack": False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((request, self.headers.get("Authorization")))
            order.append(label)
            args = request["params"].get("arguments", {})
            assert request["params"]["name"] == "import_conversation"
            result = {"conversation_id": args.get("conversation_id"),
                      "records_new": len(args.get("messages", [])), "pending": 0}
            if controls["ack"]:
                result["ack_through"] = (args.get("messages") or [{}])[-1].get("uuid")
            if controls.get("partial_ack"):
                result.update(ack_through=args["messages"][0]["uuid"],
                              messages_received=len(args["messages"]), records_dropped=1)
            if controls.get("all_dropped") or controls.get("all_dropped_at") == len(requests):
                result.update(ack_through=None, records_new=0,
                              messages_received=len(args["messages"]), records_dropped=len(args["messages"]))
            if controls["wrong_ack"]:
                result["ack_through"] = "a-different-batch"
            error = controls["error"]
            if controls.get("require_message") and (len(args.get("messages", [])) > 2000 or
                    not any(isinstance(row.get("message"), dict) for row in args.get("messages", []))):
                error = "invalid message batch"
            if controls["reject_extensions"] and "native_session_id" in args:
                error = "unexpected keyword argument 'native_session_id'"
            tool_result = ({"isError": True, "content": [{"type": "text", "text": error}]}
                           if error else {"content": [], "structuredContent": result})
            if controls.get("nested_ack") and not error:
                wrapper={"conversation_id":args.get("conversation_id"), "ack_through":None, "result":result}
                if controls.get("text_ack"):
                    tool_result={"content":[{"type":"text","text":json.dumps({"diagnostic":True})},
                                             {"type":"text","text":json.dumps(wrapper)}]}
                else:tool_result={"content":[],"structuredContent":wrapper}
            body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": tool_result}).encode()
            if controls["release"] is not None:
                controls["release"].wait(3)
            self.send_response(500 if controls.get("fail_at") == len(requests) else controls["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                if controls["drip"]:
                    for char in body:
                        self.wfile.write(bytes([char]));self.wfile.flush();time.sleep(0.03)
                else:
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, controls
    finally:
        server.shutdown();server.server_close();thread.join()


def configure(home, local, *, active=None):
    path = home / ".config/memhub-plugin/config.json";path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "sinks": [
        {"name": "cloud", "url": "https://cloud.example.test", "token": "cloud"},
        {"name": "local", "url": local, "token": "local"}],
        "active": ["cloud", "local"] if active is None else active}))
    return path


def payload(home, count=1, *, content="synthetic"):
    path = home / "session.jsonl"
    append(path, 0, count, content=content)
    return {"session_id": SID, "transcript_path": str(path), "entrypoint": "Future Claude"}


def append(path, start, count=1, *, content="synthetic"):
    with path.open("a") as output:
        for index in range(start, start + count):
            output.write(json.dumps({"uuid": f"record-{index}", "type": "user",
                                    "timestamp": "2026-01-01T00:00:00Z",
                                    "message": {"role": "user", "content": content}}) + "\n")


def environment(home, cloud_url):
    guard = home / "guard";guard.mkdir(exist_ok=True)
    (guard / "sitecustomize.py").write_text('''import socket,sys
sys.path.insert(0, ''' + repr(str(routing.SCRIPTS)) + ''')
import mcp_http
original_request=mcp_http.request
def request(url,*args,**kwargs):
    if url == ''' + repr(CLOUD) + ''': url = ''' + repr(cloud_url + "/mcp-server/mcp") + '''
    return original_request(url,*args,**kwargs)
mcp_http.request=request
original_connect=socket.socket.connect
def connect(self,address):
    if not isinstance(address,tuple) or address[0] not in {"127.0.0.1","::1"}:
        raise AssertionError("non-loopback request")
    return original_connect(self,address)
socket.socket.connect=connect
''')
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home),
            "PYTHONPATH": str(guard), "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "CLAUDE_PLUGIN_ROOT": str(routing.ROOT / "plugins/memhub")}


def invoke(home, cloud_url, data, *, script="flush_turn.py", extra=None, expected=0):
    env = environment(home, cloud_url);env.update(extra or {})
    started = time.monotonic()
    result = subprocess.run([sys.executable, str(routing.SCRIPTS / script)], env=env,
                            input=json.dumps(data), text=True, capture_output=True, timeout=8)
    assert result.returncode == expected, (result.stdout, result.stderr)
    assert "Traceback" not in result.stderr
    return time.monotonic() - started


def directory(home, name, local):
    sink = sinks.Sink(name, CLOUD if name == "cloud" else local + "/mcp-server/mcp")
    return capture_context.state_directory(home / ".config/memhub-plugin/turnflush", sink)


def state(home, name, local):
    path = directory(home, name, local) / f"{SID}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def test_membership_local_first_identity_and_single_url_override():
    order = []
    with tempfile.TemporaryDirectory() as td, receiver("local", order) as local, receiver("cloud", order) as cloud:
        home = Path(td);data = payload(home);configure(home, local[0], active=["local"])
        invoke(home, cloud[0], data)
        assert len(local[1]) == 1 and not cloud[1]
        configure(home, local[0]);append(Path(data["transcript_path"]), 1)
        order.clear();invoke(home, cloud[0], data)
        assert order == ["local", "cloud"]
        a, b = routing.imports(local[1]), routing.imports(cloud[1])
        assert a[1]["messages"] == b[0]["messages"][1:]
        assert all(row["native_session_id"] == SID and row["source_surface"] == "Future Claude" for row in a+b)
        assert all(token == "Bearer local" for _,token in local[1])
        assert all(token == "Bearer cloud" for _,token in cloud[1])
        before = len(cloud[1]);append(Path(data["transcript_path"]), 2)
        invoke(home, cloud[0], data, extra={"MEMHUB_MCP_BASE_URL":local[0], "MEMHUB_TOKEN":"override"})
        assert len(cloud[1]) == before and local[1][-1][1] == "Bearer override"


def test_failed_cloud_recovers_without_blocking_three_local_turns():
    for status in [401, 429, 500]:
        order=[]
        with tempfile.TemporaryDirectory() as td, receiver("local", order) as local, receiver("cloud", order) as cloud:
            home=Path(td);data=payload(home);configure(home,local[0]);cloud[2]["status"]=status
            for turn in range(3):
                if turn: append(Path(data["transcript_path"]),turn)
                assert invoke(home,cloud[0],data) < 3
                assert state(home,"local",local[0])["offset"] == Path(data["transcript_path"]).stat().st_size
                assert state(home,"cloud",local[0]).get("offset",0) == 0
            root=home/".config/memhub-plugin/turnflush"
            with patch.object(capture_health,"STATE_DIR",root):
                message,_=capture_health._separate_capture_health(None,sinks.Sink("local",local[0]+"/mcp-server/mcp","local"))
                assert not message
                message,_=capture_health._separate_capture_health(None,sinks.Sink("cloud",CLOUD,"cloud"))
                assert "'cloud'" in message
            cloud[2]["status"]=200;invoke(home,cloud[0],data)
            assert len(local[1]) == 3 and state(home,"cloud",local[0])["offset"] == Path(data["transcript_path"]).stat().st_size
            assert [r["uuid"] for r in routing.imports(cloud[1])[-1]["messages"]] == [f"record-{i}" for i in range(3)]
            invoke(home,cloud[0],data);assert len(cloud[1]) == 4


def test_slow_drip_exits_on_total_deadline_and_never_advances_late():
    order=[]
    with tempfile.TemporaryDirectory() as td, receiver("local", order) as local, receiver("cloud", order) as cloud:
        home=Path(td);data=payload(home);configure(home,local[0]);cloud[2]["drip"]=True
        elapsed=invoke(home,cloud[0],data,extra={"MEMHUB_TURN_FLUSH_TIMEOUT_S":"0.5"})
        assert elapsed < 1.5, elapsed
        assert state(home,"local",local[0])["offset"] > 0
        assert state(home,"cloud",local[0]).get("offset",0) == 0
        assert state(home,"cloud",local[0])["last_error"] == "timeout"
        cloud[2]["drip"]=False;invoke(home,cloud[0],data)
        assert state(home,"cloud",local[0])["offset"] > 0


def test_legacy_cloud_cursor_and_per_sink_lock_dormancy():
    order=[]
    with tempfile.TemporaryDirectory() as td, receiver("local", order) as local, receiver("cloud", order) as cloud:
        home=Path(td);data=payload(home);configure(home,local[0])
        installed=home/"installed";installed.mkdir();(installed/"scripts").symlink_to(routing.SCRIPTS,target_is_directory=True)
        (installed/".mcp.json").write_text(json.dumps({"mcpServers":{"memhub":{"url":CLOUD}}}))
        legacy=home/f".config/memhub-plugin/turnflush/{SID}.json";legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"offset":Path(data["transcript_path"]).stat().st_size}))
        invoke(home,cloud[0],data,extra={"CLAUDE_PLUGIN_ROOT":str(installed)})
        assert len(local[1]) == 1 and not cloud[1]
        # Switch to a separate cloud endpoint state, then hold only its lock.
        target=directory(home,"cloud",local[0]);target.mkdir(parents=True)
        with (target/f"{SID}.lock").open("w") as lock:
            portable_lock.lock_exclusive(lock.fileno(),blocking=False)
            append(Path(data["transcript_path"]),1)
            invoke(home,cloud[0],data,script="turn_flush_prefilter.py")
            invoke(home,cloud[0],data)
            assert len(local[1]) == 2 and not cloud[1]
        (target/f"{SID}.json").write_text(json.dumps({"unsupported":True,"offset":0}))
        invoke(home,cloud[0],data,script="turn_flush_prefilter.py",expected=0)
        cloud[2]["ack"]=False;invoke(home,cloud[0],data)
        assert not state(home,"cloud",local[0])["unsupported"]
        assert state(home,"cloud",local[0])["last_error"]=="unrecognized_response"
        append(Path(data["transcript_path"]),2);invoke(home,cloud[0],data)
        assert len(local[1]) == 3 and len(cloud[1]) == 2
        invoke(home,cloud[0],data,script="turn_flush_prefilter.py",expected=0)


def test_old_cloud_extension_fallback_does_not_change_local_projection():
    order=[]
    with tempfile.TemporaryDirectory() as td, receiver("local", order) as local, receiver("cloud", order) as cloud:
        home=Path(td);data=payload(home);configure(home,local[0]);cloud[2]["reject_extensions"]=True
        invoke(home,cloud[0],data)
        a=routing.imports(local[1])[0];b=routing.imports(cloud[1])
        assert len(b)==2 and "native_session_id" in b[0] and "native_session_id" not in b[1]
        assert "source_surface" in a and "source_surface" not in b[1]
        assert a["messages"] == b[0]["messages"] == b[1]["messages"]
        cloud[2]["reject_extensions"]=False;cloud[2]["error"]="native_session_id had an internal failure"
        append(Path(data["transcript_path"]),1);invoke(home,cloud[0],data)
        assert len(cloud[1])==3 and state(home,"local",local[0])["offset"] > state(home,"cloud",local[0])["offset"]


def test_catchup_chunks_are_bounded_and_partial_tail_waits():
    order=[]
    with tempfile.TemporaryDirectory() as td, receiver("local", order) as local, receiver("cloud", order) as cloud:
        home=Path(td);data=payload(home,2001);configure(home,local[0],active=["local"])
        path=Path(data["transcript_path"]);complete=path.stat().st_size
        with path.open("ab") as output: output.write(b'{"type":"user"')
        invoke(home,cloud[0],data)
        batches=routing.imports(local[1]);assert [len(b["messages"]) for b in batches] == [2000,1]
        assert state(home,"local",local[0])["offset"] == complete
        assert len({r["uuid"] for b in batches for r in b["messages"]}) == 2001
        path.write_text("");local[1].clear();(directory(home,"local",local[0])/f"{SID}.json").unlink()
        append(path,3000,3,content="x"*1_500_000)
        invoke(home,cloud[0],data)
        assert [len(b["messages"]) for b in routing.imports(local[1])] == [2,1]
        assert state(home,"local",local[0])["offset"] == path.stat().st_size


def test_redaction_reuses_values_without_shared_mutation_and_is_bounded():
    rows=[{"uuid":"one","message":{"content":"synthetic"}},{"uuid":"two","message":{"content":"synthetic"}}]
    token=ft._REDACTION_CACHE.set({"items":{},"bytes":0})
    try:
        with patch.object(capture_redaction,"redact_records",side_effect=lambda records:copy.deepcopy(records)) as redactor:
            first=ft._redact_once(rows);first[0]["message"]["content"]="mutated"
            second=ft._redact_once(rows)
            assert second==rows and redactor.call_count==2
            ft._redact_once([{"content":"x"*(9*1024*1024)}])
            assert ft._REDACTION_CACHE.get()["bytes"] <= 8*1024*1024
    finally:
        ft._REDACTION_CACHE.reset(token)


def test_cancelled_auth_worker_cannot_keep_loop_alive_or_write_delivery_state():
    marker=[];release=threading.Event()
    async def run():
        async def task():
            await capture_async.blocking(lambda:release.wait(2))
            marker.append("advanced")
        try:
            await asyncio.wait_for(task(),timeout=0.02)
        except TimeoutError:
            pass
    started=time.monotonic();asyncio.run(run());assert time.monotonic()-started < 0.5
    release.set();time.sleep(0.03);assert marker==[]


def test_overlapping_hooks_serialize_each_destination_and_keep_atomic_state():
    order=[]
    with tempfile.TemporaryDirectory() as td, receiver("local",order) as local, receiver("cloud",order) as cloud:
        home=Path(td);data=payload(home);configure(home,local[0]);env=environment(home,cloud[0])
        release=threading.Event();cloud[2]["release"]=release
        first=subprocess.Popen([sys.executable,str(routing.SCRIPTS/"flush_turn.py")],env=env,
                               stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            first.stdin.write(json.dumps(data));first.stdin.close();first.stdin=None
            deadline=time.monotonic()+3
            while not cloud[1] and time.monotonic()<deadline: time.sleep(0.01)
            assert cloud[1],"first hook must hold its cloud lock"
            invoke(home,cloud[0],data)
            assert len(local[1])==1 and len(cloud[1])==1
        finally:
            release.set();stdout,stderr=first.communicate(timeout=5)
        assert first.returncode==0 and "Traceback" not in stderr
        size=Path(data["transcript_path"]).stat().st_size
        assert state(home,"local",local[0])["offset"]==state(home,"cloud",local[0])["offset"]==size


def test_wrong_batch_acknowledgement_does_not_advance_that_destination():
    order=[]
    with tempfile.TemporaryDirectory() as td, receiver("local",order) as local, receiver("cloud",order) as cloud:
        home=Path(td);data=payload(home);configure(home,local[0]);cloud[2]["wrong_ack"]=True
        invoke(home,cloud[0],data)
        assert state(home,"local",local[0])["offset"] > 0
        assert state(home,"cloud",local[0]).get("offset",0)==0
        cloud[2]["wrong_ack"]=False;invoke(home,cloud[0],data)
        assert state(home,"cloud",local[0])["offset"] > 0


def test_acknowledgements_account_for_explicit_drops_without_accepting_a_wrong_batch():
    records=[{"uuid":"accepted"},{"uuid":"rejected"}]
    response={"conversation_id":SID,"messages_received":2,"records_dropped":1,"ack_through":"accepted"}
    assert capture_context.acknowledges(response,SID,records)
    assert not capture_context.acknowledges({**response,"records_dropped":0},SID,records)
    assert not capture_context.acknowledges({**response,"messages_received":1},SID,records)
    assert not capture_context.acknowledges({**response,"ack_through":"other"},SID,records)
    partial={**response,"messages_received":3}
    three=records+[{"uuid":"remaining"}]
    assert not capture_context.acknowledges(partial,SID,three)
    assert capture_context.acknowledges({**partial,"records_dropped":2},SID,three)
    assert not capture_context.acknowledges({**response,"messages_received":True},SID,records)
    all_dropped={**response,"records_dropped":2,"ack_through":None}
    assert capture_context.acknowledges(all_dropped,SID,[{},{}])
    assert not capture_context.acknowledges({**all_dropped,"conversation_id":"other"},SID,[{},{}])
    assert not capture_context.acknowledges({**all_dropped,"records_dropped":True},SID,[{},{}])


def test_partial_ack_and_insufficient_drop_count_leave_the_destination_cursor_pinned():
    with tempfile.TemporaryDirectory() as td,receiver("local",[]) as local,receiver("cloud",[]) as cloud:
        home=Path(td);data=payload(home,3);configure(home,local[0]);cloud[2]["partial_ack"]=True
        invoke(home,cloud[0],data)
        assert state(home,"local",local[0])["offset"]==Path(data["transcript_path"]).stat().st_size
        assert state(home,"cloud",local[0]).get("offset",0)==0
        cloud[2]["partial_ack"]=False;invoke(home,cloud[0],data)
        assert state(home,"cloud",local[0])["offset"]==Path(data["transcript_path"]).stat().st_size


def test_nested_and_text_acknowledgements_confirm_only_the_submitted_batch():
    order=[]
    with tempfile.TemporaryDirectory() as td,receiver("local",order) as local,receiver("cloud",order) as cloud:
        home=Path(td);data=payload(home);configure(home,local[0],active=["local"])
        local[2].update(nested_ack=True,all_dropped=True)
        invoke(home,cloud[0],data)
        assert state(home,"local",local[0])["offset"]==Path(data["transcript_path"]).stat().st_size
        local[2]["text_ack"]=True;append(Path(data["transcript_path"]),1)
        confirmed=Path(data["transcript_path"]).stat().st_size
        invoke(home,cloud[0],data)
        assert state(home,"local",local[0])["offset"]==confirmed
        append(Path(data["transcript_path"]),2);local[2]["wrong_ack"]=True
        invoke(home,cloud[0],data)
        assert state(home,"local",local[0])["offset"]==confirmed


def test_large_native_records_are_elided_without_stranding_later_turns():
    for tool in (False,True):
        with tempfile.TemporaryDirectory() as td,receiver("local",[]) as local,receiver("cloud",[]) as cloud:
            home=Path(td);data=payload(home);path=Path(data["transcript_path"])
            configure(home,local[0],active=["local"])
            content="x"*(17*1024*1024)
            if tool:content=[{"type":"tool_result","tool_use_id":"large-call","content":content}]
            row={"uuid":"large-record","type":"user","message":{"role":"user","content":content}}
            with path.open("a") as handle:handle.write(json.dumps(row))
            before=path.stat().st_size
            invoke(home,cloud[0],data)
            assert state(home,"local",local[0])["offset"]<before
            with path.open("a") as handle:handle.write("\n")
            append(path,99)
            invoke(home,cloud[0],data)
            received=[r for batch in routing.imports(local[1]) for r in batch["messages"]]
            large=next(r for r in received if r["uuid"]=="large-record")
            assert len(json.dumps(large))<3_500_000 and "elided" in json.dumps(large)
            assert any(r["uuid"]=="record-99" for r in received)
            assert state(home,"local",local[0])["offset"]==path.stat().st_size


def test_attachment_prefixes_replay_native_context_without_skipping_offsets():
    for count,size,message_size in [(1,2_000_000,2_000_000),(2005,10,10)]:
        with tempfile.TemporaryDirectory() as td,receiver("local",[]) as local,receiver("cloud",[]) as cloud:
            home=Path(td);data=payload(home,count=0);path=Path(data["transcript_path"])
            configure(home,local[0],active=["local"]);local[2]["require_message"]=True
            with path.open("a") as output:
                for index in range(count):
                    output.write(json.dumps({"type":"attachment","uuid":f"attachment-{index}",
                                             "attachment":{"content":"x"*size}})+"\n")
            invoke(home,cloud[0],data)
            assert not local[1] and state(home,"local",local[0]).get("offset",0)==0
            with path.open("a") as output:
                output.write(json.dumps({"type":"user","uuid":"command-wrapper",
                    "message":{"role":"user","content":"<command-name>/model</command-name>"}})+"\n")
            invoke(home,cloud[0],data)
            assert not local[1] and state(home,"local",local[0]).get("offset",0)==0
            append(path,1,content="y"*message_size);append(path,2)
            local[2]["fail_at"]=2;invoke(home,cloud[0],data)
            committed=state(home,"local",local[0])["offset"]
            assert 0<committed<path.stat().st_size
            local[2].pop("fail_at");invoke(home,cloud[0],data)
            batches=routing.imports(local[1])
            assert all(len(batch["messages"])<=2000 and any("message" in row for row in batch["messages"]) for batch in batches)
            seen={row["uuid"] for batch in batches for row in batch["messages"]}
            assert seen=={f"attachment-{i}" for i in range(count)}|{"record-1","record-2"}
            assert sum(row["uuid"]=="record-1" for batch in batches for row in batch["messages"])>1
            assert state(home,"local",local[0])["offset"]==path.stat().st_size


def test_single_destination_keeps_legacy_backstop_dormancy():
    with tempfile.TemporaryDirectory() as td,receiver("local",[]) as local,receiver("cloud",[]) as cloud:
        home=Path(td);data=payload(home);configure(home,local[0],active=["cloud"])
        cloud[2]["ack"]=False;invoke(home,cloud[0],data)
        assert state(home,"cloud",local[0])["unsupported"]
        append(Path(data["transcript_path"]),1);invoke(home,cloud[0],data)
        assert len(cloud[1])==1
        invoke(home,cloud[0],data,script="turn_flush_prefilter.py",expected=1)


if __name__ == "__main__":
    for name,fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn();print("PASS",name)
    print("ALL PASS")
