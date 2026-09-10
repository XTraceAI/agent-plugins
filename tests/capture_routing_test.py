"""Real capture entrypoints select only their configured destination."""
from __future__ import annotations

import contextlib
import copy
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

import readers_test as fixtures
import capture_context
import capture_health
import sinks
import _memhub_auth as auth
from readers import codex, cursor

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/memhub/scripts"
SID = "11111111-2222-4333-8444-555555555555"


@contextlib.contextmanager
def receiver():
    requests = []
    controls = {"status": 200, "ack": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((request, self.headers.get("Authorization")))
            args = request.get("params", {}).get("arguments", {})
            result = {"conversation_id": args.get("conversation_id"),
                      "records_new": len(args.get("messages", [])),
                      "records_received": len(args.get("messages", [])),
                      "pending": 0, "draining": False}
            if controls["ack"]:
                result["ack_through"] = (args.get("messages") or [{}])[-1].get("uuid")
            body = json.dumps({"jsonrpc": "2.0", "id": request.get("id"),
                               "result": {"structuredContent": result, "content": []}}).encode()
            self.send_response(controls["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, controls
    finally:
        server.shutdown();server.server_close();thread.join()


def config(home, url, *, name="local", active=None):
    path = home / ".config/memhub-plugin/config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "sinks": [
        {"name": name, "url": url, "token": "local"}],
        "active": [name] if active is None else active}))
    return path


def invoke(home, script, payload, *args, override=None):
    guard = home / "guard"
    guard.mkdir(exist_ok=True)
    (guard / "sitecustomize.py").write_text('''import builtins,socket
original_import=builtins.__import__
def imported(name,*args,**kwargs):
    if name == "mcp" or name.startswith("mcp."): raise AssertionError("SDK import")
    return original_import(name,*args,**kwargs)
builtins.__import__=imported
original_connect=socket.socket.connect
def connect(self,address):
    if not isinstance(address,tuple) or address[0] not in {"127.0.0.1","::1"}:
        raise AssertionError("non-loopback request")
    return original_connect(self,address)
socket.socket.connect=connect
''')
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home),
           "PYTHONPATH": str(guard), "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "CLAUDE_PLUGIN_ROOT": str(ROOT / "plugins/memhub")}
    if override:
        env.update(override)
    result = subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                            input=json.dumps(payload), env=env, text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return result


def sources(home):
    claude_path = fixtures._write_jsonl(home / ".claude/projects/example/session.jsonl",
                                       copy.deepcopy(fixtures.CLAUDE_SYNTH))
    codex_rows = copy.deepcopy(fixtures.CODEX_SYNTH)
    codex_rows[0]["payload"].update(id=SID, originator="Future Desktop")
    codex_path = fixtures._write_jsonl(home / f".codex/sessions/2026/01/01/rollout-2026-01-01T00-00-00-{SID}.jsonl",
                                      codex_rows)
    cursor_path = fixtures._make_cursor_transcript(home / ".cursor/projects", uuid=SID)
    return [("flush_turn.py", [], {"session_id": SID, "transcript_path": str(claude_path),
                                    "entrypoint": "Future Claude"}, claude_path, "claude", SID),
            ("flush_session.py", [], {"session_id": SID, "transcript_path": str(claude_path),
                                       "source_surface": "Future Claude"}, claude_path, "claude", SID),
            ("codex_flush.py", ["Stop"], {"session_id": SID, "transcript_path": str(codex_path)},
             codex_path, "codex", "codex-" + SID),
            ("cursor_flush.py", ["stop"], {"session_id": SID, "transcript_path": str(cursor_path),
                                           "workspace_roots": ["/repo/proj"]},
             cursor_path, "cursor", "cursor-" + SID)]


def imports(requests):
    assert all(request["params"]["name"] == "import_conversation" for request, _ in requests), \
        "local capture must not query cloud rooms"
    return [request["params"]["arguments"] for request, _ in requests]


def test_all_real_hooks_use_config_without_environment_and_preserve_identity():
    with tempfile.TemporaryDirectory() as td, receiver() as (url, requests, _):
        home = Path(td);config_path = config(home, url)
        before_config = config_path.read_bytes()
        for script, args, payload, path, host, conversation in sources(home):
            before = path.read_bytes();count = len(requests)
            invoke(home, script, payload, *args)
            sent = imports(requests[count:])
            assert sent, script
            assert all(item["conversation_id"] == conversation for item in sent)
            assert all(item["native_session_id"] == SID for item in sent)
            assert all(item["source_platform"] == host for item in sent)
            expected_surface = {"claude": "Future Claude", "codex": "Future Desktop", "cursor": "cursor-ide"}[host]
            assert all(item["source_surface"] == expected_surface for item in sent)
            assert all("agent_brain_id" not in item and "org_id" not in item for item in sent)
            assert path.read_bytes() == before
        assert all(token == "Bearer local" for _, token in requests)
        assert config_path.read_bytes() == before_config


def test_new_endpoint_never_inherits_another_destinations_cursor():
    with tempfile.TemporaryDirectory() as td, receiver() as first, receiver() as second:
        home = Path(td);url_a, received_a, _ = first;url_b, received_b, _ = second
        config(home, url_a)
        script, args, payload, path, *_ = sources(home)[0]
        legacy = home / f".config/memhub-plugin/turnflush/{SID}.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"offset": path.stat().st_size, "unsupported": True}))
        before = legacy.read_bytes()
        invoke(home, "turn_flush_prefilter.py", payload)
        invoke(home, script, payload, *args)
        assert len(received_a) == 1
        config(home, url_b)  # Same name, different endpoint: start independently.
        invoke(home, "turn_flush_prefilter.py", payload)
        invoke(home, script, payload, *args)
        assert len(received_b) == 1
        assert imports(received_a)[0]["messages"] == imports(received_b)[0]["messages"]
        assert legacy.read_bytes() == before
        assert len(list(legacy.parent.glob(f"local/*/{SID}.json"))) == 2


def test_explicit_url_and_token_override_config_for_every_hook():
    with tempfile.TemporaryDirectory() as td, receiver() as first, receiver() as second:
        home = Path(td);url_a, received_a, _ = first;url_b, received_b, _ = second
        config(home, url_a)
        for script, args, payload, *_ in sources(home):
            invoke(home, script, payload, *args, override={"MEMHUB_MCP_BASE_URL": url_b,
                                                         "MEMHUB_TOKEN": "override"})
        assert received_a == [] and len(received_b) == 4
        assert all(token == "Bearer override" for _, token in received_b)
        imports(received_b)


def test_disabled_or_unknown_selection_sends_nothing():
    with tempfile.TemporaryDirectory() as td, receiver() as (url, requests, _):
        home = Path(td)
        for active in ([], ["missing"]):
            config(home, url, active=active)
            for script, args, payload, *_ in sources(home):
                invoke(home, script, payload, *args)
        assert requests == []


def test_missing_surface_stays_unknown_and_failed_ack_holds_the_cursor():
    with tempfile.TemporaryDirectory() as td, receiver() as (url, requests, controls):
        home = Path(td);config(home, url)
        script, args, payload, *_ = sources(home)[0]
        payload.pop("entrypoint")
        controls["status"] = 500
        invoke(home, script, payload, *args)
        state_path = next((home / ".config/memhub-plugin/turnflush/local").glob(f"*/{SID}.json"))
        assert json.loads(state_path.read_text()).get("offset", 0) == 0
        controls["status"] = 200
        invoke(home, script, payload, *args)
        assert json.loads(state_path.read_text())["offset"] > 0
        assert all("source_surface" not in item for item in imports(requests))


def test_cursor_pins_stay_shared_while_delivery_progress_is_separate():
    with tempfile.TemporaryDirectory() as td, receiver() as first, receiver() as second:
        home = Path(td);url_a, received_a, _ = first;url_b, received_b, _ = second
        config(home, url_a)
        script, args, payload, *_ = sources(home)[3]
        payload.update(generation_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                       input_tokens=20120, output_tokens=48, cache_read_tokens=1024, cache_write_tokens=9)
        invoke(home, script, payload, *args)
        shared_path = home / f".config/memhub-plugin/cursorflush/{SID}.json"
        shared_before = json.loads(shared_path.read_text())
        assert shared_before["record_ts"] and shared_before["usage_events"]
        assert "transcript_revision" not in shared_before and "sent_usage_generations" not in shared_before
        config(home, url_b)
        invoke(home, script, payload, *args)
        shared_after = json.loads(shared_path.read_text())
        assert shared_after["record_ts"] == shared_before["record_ts"]
        assert shared_after["usage_events"] == shared_before["usage_events"]
        assert imports(received_a)[0]["messages"] == imports(received_b)[0]["messages"]
        assert len(list(shared_path.parent.glob(f"local/*/{SID}.json"))) == 2


def test_capture_and_cloud_service_health_are_distinct():
    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {}, clear=True):
        home = Path(td);root = home / "turnflush"
        sink = sinks.Sink("local", "http://127.0.0.1:47421/mcp-server/mcp", "local")
        directory = capture_context.state_directory(root, sink);directory.mkdir(parents=True)
        (directory / f"{SID}.json").write_text(json.dumps({"last_error": "timeout", "last_error_at": time.time()}))
        # A cloud success cannot retract a different destination's failure.
        (root / f"{SID}.sessionflush.json").write_text(json.dumps({"last_ok_at": time.time() + 1}))
        with patch.object(capture_health, "STATE_DIR", root), \
                patch.object(capture_health, "_token_problem", return_value="expired"), \
                patch.object(capture_health, "_rulebook_problem", return_value=None), \
                patch.object(auth, "default_url", return_value="https://cloud.example.test/mcp-server/mcp"):
            message, signature = capture_health._separate_capture_health("cloud.example.test", sink)
            assert "Capture destination 'local'" in message and "Cloud services" in message
            assert "does not establish cloud authentication" in message
            assert "capture:local:" in signature and ":timeout" in signature and "cloud:cloud.example.test:expired" in signature
            assert auth.default_url() == "https://cloud.example.test/mcp-server/mcp"


def test_selected_state_and_cloud_service_routes_are_frozen_independently():
    import asyncio
    import types
    import brain_brief
    import directive_recall
    import login
    import save_artifact
    import md_capture_flush
    cloud = "https://cloud.example.test/mcp-server/mcp"
    local = sinks.Sink("local", "http://127.0.0.1:47421/mcp-server/mcp", "local")
    seen = []
    class Session:
        def __init__(self, url, bearer, **kwargs):
            seen.append((url, bearer))
        async def call_tool(self, *args, **kwargs):
            return types.SimpleNamespace(content=[types.SimpleNamespace(text='{"directives":[]}')], isError=False)
    @capture_context.entrypoint
    def run():
        root = Path("synthetic-state")
        directory = capture_context.state_directory(root)
        assert capture_context.resolve_bearer() == (local.url, "local")
        for module in [login, save_artifact, md_capture_flush]:
            assert module.resolve_url_and_auth(interactive=False) == (cloud, {"Authorization":"Bearer cloud-token"}, None)
        asyncio.run(directive_recall._recall("Edit", {}, "", []))
        assert brain_brief._prompt_recall("synthetic-brain", "", ["example.py"], [], "synthetic") == []
        with patch.object(auth, "default_url", return_value="https://changed.example.test/mcp"):
            assert capture_context.state_directory(root) == directory
        return 0
    with patch.dict(os.environ, {}, clear=True), \
            patch.object(capture_context, "resolve_capture_sink", return_value=local), \
            patch.object(auth, "default_url", return_value=cloud), \
            patch.object(auth, "_refresh_cached_token_if_stale", return_value=None), \
            patch.object(auth, "_stored_pak", return_value={"secret":"cloud-token"}), \
            patch.object(__import__("mcp_http"), "Session", Session), \
            patch.object(brain_brief, "_recall_items", side_effect=lambda url,bearer,*args: seen.append((url,bearer)) or []):
        assert run() == 0
        assert seen == [(cloud, "cloud-token"), (cloud, "cloud-token")]
        assert capture_context.state_directory(Path("synthetic-state")) == Path("synthetic-state")


def test_legacy_cloud_state_is_frozen_for_the_whole_invocation():
    cloud = sinks.Sink("cloud", "https://cloud.example.test/mcp")
    @capture_context.entrypoint
    def run():
        with patch.object(auth, "default_url", return_value="https://changed.example.test/mcp"):
            assert capture_context.state_directory(Path("synthetic-state")) == Path("synthetic-state")
        return 0
    with patch.object(capture_context, "resolve_capture_sink", return_value=cloud), \
            patch.object(auth, "default_url", return_value=cloud.url):
        assert run() == 0


def test_claude_identity_cannot_escape_the_destination_state_directory():
    with tempfile.TemporaryDirectory() as td, receiver() as (url, requests, _):
        home = Path(td);config(home, url)
        for script,args,payload,*_ in sources(home)[:2]:
            for sid in ["..", "../outside", "/outside", "a/b", "a\\b", "bad\x00id"]:
                invoke(home, script, {**payload, "session_id": sid}, *args)
        assert not requests
        assert not (home / ".config/memhub-plugin/turnflush").exists()


def test_missing_or_corrupt_file_keeps_installed_endpoint_capture():
    with tempfile.TemporaryDirectory() as td, receiver() as (url, requests, _):
        home = Path(td);path = config(home, url)
        installed = home / "installed-memhub";installed.mkdir()
        (installed / "scripts").symlink_to(SCRIPTS, target_is_directory=True)
        (installed / ".mcp.json").write_text(json.dumps({"mcpServers":{"memhub":{"url":url + "/mcp-server/mcp"}}}))
        for corrupt in [None, "{broken"]:
            if corrupt is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(corrupt)
            script,args,payload,*_ = sources(home)[1]
            invoke(home,script,payload,*args,override={"CLAUDE_PLUGIN_ROOT":str(installed),"MEMHUB_TOKEN":"legacy"})
        assert len(requests)==2 and all(token=="Bearer legacy" for _,token in requests)
        imports(requests)


def test_remote_room_lookup_keeps_installed_cloud_cache_separate():
    import asyncio
    import types
    import room_map
    import brain_resolve
    remote=sinks.Sink("remote","https://another.example.test/mcp","remote")
    cloud="https://cloud.example.test/mcp"
    room_name="Repo: example/project"
    class Session:
        async def call_tool(self,name,arguments):
            if name=="list_orgs":
                result={"orgs":[{"org_id":"remote-org","is_default":True}]}
            else:
                result={"agent_brains":[{"id":"remote-brain","name":room_name}]}
            return types.SimpleNamespace(structuredContent=result,content=[],isError=False)
    @capture_context.entrypoint
    def run():
        env=capture_context.env_for_url(remote.url)
        assert env.startswith("capture-")
        resolved=asyncio.run(capture_context.resolve_repo_brain(Session(),"/synthetic",env))
        assert resolved["brain_id"]=="remote-brain"
        assert room_map.read_room("/synthetic",env)["brain_id"]=="remote-brain"
        assert room_map.read_room("/synthetic","production")["brain_id"]=="installed-brain"
        with patch.object(auth,"_plugin_mcp_config",return_value={"url":remote.url}):
            assert capture_context.env_for_url(remote.url)==env,"room routing is frozen"
    with tempfile.TemporaryDirectory() as td, patch.object(room_map,"ROOMS_PATH",Path(td)/"rooms.json"), \
            patch.object(room_map,"room_name",return_value=room_name), \
            patch.object(brain_resolve,"room_name",return_value=room_name), \
            patch.object(auth,"_plugin_mcp_config",return_value={"url":cloud}), \
            patch.object(capture_context,"resolve_capture_sink",return_value=remote):
        room_map.write_room("installed-brain",name=room_name,env="production",org_id="installed-org")
        run()


def test_renewable_capture_health_is_silent_without_network_or_cross_origin_trust():
    import base64
    expired="header."+base64.urlsafe_b64encode(json.dumps({"exp":1}).encode()).decode().rstrip("=")+".signature"
    cloud="https://cloud.example.test/mcp"
    selected=sinks.Sink("alias","https://CLOUD.example.test:443/mcp")
    installed={"url":cloud,"oauth":{"clientId":"synthetic","authServerMetadataUrl":"https://auth.example.test/metadata"}}
    with tempfile.TemporaryDirectory() as td, patch.dict(os.environ,{},clear=True), \
            patch.object(auth,"_CACHE_DIR",Path(td)),patch.object(auth,"_plugin_mcp_config",return_value=installed), \
            patch.object(auth,"_refresh_cached_token_if_stale",side_effect=AssertionError("health cannot refresh")), \
            patch.object(capture_health,"STATE_DIR",Path(td)/"turnflush"):
        cache=auth.token_cache_path(cloud)
        cache.write_text(json.dumps({"access_token":expired,"refresh_token":"renewable"}))
        message,_=capture_health._separate_capture_health(None,selected)
        assert not message
        for value in [None,"",{},17]:
            cache.write_text(json.dumps({"access_token":expired,"refresh_token":value}))
            message,_=capture_health._separate_capture_health(None,selected)
            assert "no usable credential" in message
        cache.write_text(json.dumps({"access_token":expired,"refresh_token":"renewable"}))
        message,_=capture_health._separate_capture_health(None,sinks.Sink("other","https://other.example.test/mcp"))
        assert "no usable credential" in message
        directory=capture_context.state_directory(capture_health.STATE_DIR,selected);directory.mkdir(parents=True)
        (directory/f"{SID}.json").write_text(json.dumps({"last_error":"timeout","last_error_at":time.time()}))
        message,_=capture_health._separate_capture_health(None,selected)
        assert "'alias'" in message and "no usable credential" not in message


def test_same_named_endpoint_changes_do_not_suppress_new_health_warnings():
    import io
    first=sinks.Sink("local","http://127.0.0.1:47421/mcp","synthetic")
    second=sinks.Sink("local","http://127.0.0.1:47422/mcp","synthetic")
    with tempfile.TemporaryDirectory() as td, patch.object(capture_health,"STATE_DIR",Path(td)), \
            patch.object(capture_health,"_env_host",return_value=None), \
            patch.object(capture_health,"_recent_failure",return_value=("timeout",time.time())):
        for sink,expected in [(first,True),(first,False),(second,True),(second,False)]:
            output=io.StringIO()
            with patch.object(sinks,"resolve_capture_sink",return_value=sink), \
                    patch.object(sys,"stdin",io.StringIO(json.dumps({"session_id":SID}))), \
                    contextlib.redirect_stdout(output):
                assert capture_health.main()==0
            assert bool(output.getvalue()) is expected
        marker=(Path(td)/f"{SID}.health").read_text()
        assert "127.0.0.1" not in marker and "synthetic" not in marker


def test_disabled_or_invalid_delivery_keeps_shared_cursor_observations_for_later_capture():
    generation="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    for active in [[],["missing"]]:
        with tempfile.TemporaryDirectory() as td,receiver() as (url,requests,_):
            home=Path(td);config(home,url,active=active)
            script,args,payload,path,*_=sources(home)[3]
            shared=home/f".config/memhub-plugin/cursorflush/{SID}.json";shared.parent.mkdir(parents=True)
            held={"transcript_revision":"existing-cloud-progress","sent_usage_generations":["previous"],"unsupported":True}
            shared.write_text(json.dumps(held))
            payload.update(generation_id=generation,input_tokens=20120,output_tokens=48,
                           cache_read_tokens=1024,cache_write_tokens=9)
            invoke(home,script,payload,*args)
            saved=json.loads(shared.read_text())
            assert not requests and saved["record_ts"] and saved["usage_events"][generation]
            assert all(saved[key]==value for key,value in held.items())
            assert not (shared.parent/"local").exists()
            config(home,url)
            followup={key:value for key,value in payload.items() if key not in {
                "generation_id","input_tokens","output_tokens","cache_read_tokens","cache_write_tokens"}}
            invoke(home,script,followup,*args)
            assert len(requests)==1
            records=imports(requests)[0]["messages"]
            measured=[row["message"]["usage"] for row in records if row.get("message",{}).get("usage")]
            assert any(usage.get("output_tokens")==48 and usage.get("input_tokens")==20120 for usage in measured)
            after=json.loads(shared.read_text())
            assert after["record_ts"]==saved["record_ts"] and after["usage_events"]==saved["usage_events"]


def test_configured_installed_cloud_token_is_healthy_without_account_login():
    with tempfile.TemporaryDirectory() as td:
        home=Path(td);installed=home/"installed";installed.mkdir()
        (installed/"scripts").symlink_to(SCRIPTS,target_is_directory=True)
        cloud="https://cloud.example.test/mcp-server/mcp"
        (installed/".mcp.json").write_text(json.dumps({"mcpServers":{"memhub":{"url":cloud}}}))
        config=home/".config/memhub-plugin/config.json";config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"version":1,"sinks":[{"name":"cloud","url":cloud,"token":"explicit-capture"}],"active":["cloud"]}))
        env={"PATH":os.environ.get("PATH",""),"HOME":str(home),"USERPROFILE":str(home),
             "CLAUDE_PLUGIN_ROOT":str(installed),"PYTHONDONTWRITEBYTECODE":"1"}
        result=subprocess.run([sys.executable,str(SCRIPTS/"capture_health.py")],env=env,
                              input=json.dumps({"session_id":SID}),text=True,capture_output=True,timeout=8)
        assert result.returncode==0 and "Traceback" not in result.stderr,result.stderr
        assert "Cloud services" in result.stdout,result.stdout
        assert "Capture destination" not in result.stdout and "session is not being saved" not in result.stdout,result.stdout


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print("ALL PASS")
