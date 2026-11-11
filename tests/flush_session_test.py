#!/usr/bin/env python3
"""What the whole-transcript flush puts on the wire, and when it gives up.

Run: python3 tests/flush_session_test.py

Covers the arguments assembly and the success/failure contract of one slice —
the parts that decide whether a capture lands in the right brain, under the
right name, and whether a rejected slice stops the run instead of leaving a
hole in the middle of the conversation.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# HOME is redirected BEFORE the import, because this module resolves its
# breadcrumb directory from Path.home() at import time. Without this the tests
# write breadcrumbs into the developer's REAL ~/.config/memhub-plugin — which
# they did, and which would also let a test's fake session id show up in the
# health check's warnings on a real machine. Both spellings: POSIX expanduser
# reads HOME; Windows reads USERPROFILE and never consults HOME.
_TMP_HOME = tempfile.mkdtemp(prefix="flush-session-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME

# The tests live outside the plugin so they are not shipped to users;
# the code under test is still in the plugin's scripts dir.
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import flush_session as fs  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(name)


class FakeBlock:
    def __init__(self, text):
        self.text = text


class FakeResult:
    def __init__(self, structured=None, texts=(), is_error=False):
        self.structuredContent = structured
        self.content = [FakeBlock(t) for t in texts]
        self.isError = is_error


class FakeSession:
    def __init__(self, result):
        self.result = result
        self.sent = []
        self.timeouts = []

    async def call_tool(self, name, arguments=None, timeout=None):
        # `timeout` is recorded, not ignored: each slice is bounded by the
        # budget REMAINING rather than a fixed share, so a fake that silently
        # swallowed it would let that bound regress unnoticed.
        self.sent.append((name, arguments))
        self.timeouts.append(timeout)
        return self.result


OK = FakeResult(structured={"conversation_id": "c1", "messages_received": 3,
                            "path": "agentic"})


def send(result, title=None, namespace=None, index=1, total=1, budget=None):
    session = FakeSession(result)
    args = {"messages": [{"type": "user"}], "conversation_id": "s1",
            "source_platform": "claude"}
    ok = asyncio.run(fs._send(session, args, title, namespace,
                              index, total, budget=budget))
    return ok, (session.sent[0][1] if session.sent else None)


ok, args = send(OK)
check("a bare send succeeds", ok is True)
# A session is captured into personal memory, never into a brain.
check("no brain id is ever sent", "agent_brain_id" not in args)
check("no org is ever sent", "org_id" not in args)
check("nothing invents a title", "title" not in args)

ok, args = send(OK, title="Fix the flush hook", namespace="memhub")
check("the title is sent", args.get("title") == "Fix the flush hook")
check("the namespace is sent", args.get("namespace") == "memhub")


# MCP signals tool failure with isError, NOT an exception. Treating that as
# success would report a capture that never happened.
ok, _ = send(FakeResult(texts=["Agent brain not found"], is_error=True))
check("an isError reply stops the run", ok is False)

# Not an error per the protocol, but not the shape import_conversation
# returns either — a slice that cannot be confirmed did not land.
ok, _ = send(FakeResult(structured={"something": "else"}))
check("an unrecognized body stops the run", ok is False)


# ── one call per slice ────────────────────────────────────────────────
#
# Nothing routes, so there is no "brain not found → re-send unrouted" retry:
# a rejected slice is one call and a stop, and the slice is bounded by the
# budget it was handed.

import json as _json  # noqa: E402 — used by the main() cases below

_s = FakeSession(FakeResult(texts=["Error executing tool import_conversation: "
                                       "Agent brain not found"], is_error=True))
_ok = asyncio.run(fs._send(_s, {"messages": [], "conversation_id": "s1"},
                           None, None, 1, 1, budget=30.0))
check("a rejected slice is sent once", len(_s.sent) == 1)
check("a rejected slice stops the run", _ok is False)
check("the slice is bounded by its budget", _s.timeouts == [30.0])

ok, _ = send(FakeResult(texts=['{"conversation_id": "c1"}']))
check("a JSON text body is understood", ok is True)

ok, _ = send(FakeResult(structured={"result": {"conversation_id": "c1"}}))
check("a FastMCP-wrapped body is unwrapped", ok is True)


import os  # noqa: E402

for raw, want, why in (
    ("", fs._DEFAULT_DEADLINE_S, "absent"),
    ("nonsense", fs._DEFAULT_DEADLINE_S, "malformed"),
    ("0", fs._DEFAULT_DEADLINE_S, "zero"),
    ("-5", fs._DEFAULT_DEADLINE_S, "negative"),
    ("30", 30.0, "an override"),
):
    os.environ["MEMHUB_FLUSH_DEADLINE_S"] = raw
    check(f"{why} deadline resolves to {want}", fs._deadline_s() == want)
os.environ.pop("MEMHUB_FLUSH_DEADLINE_S", None)

# The loop's own deadline must land INSIDE the hard timeout, so a merely-slow
# run stops where it can name what it did not send rather than being
# cancelled where it cannot.
check("the loop gives up before the hard timeout is reached",
      fs._DEFAULT_DEADLINE_S * 0.9 < fs._DEFAULT_DEADLINE_S)


# A backstop that sends nothing is not a degraded capture, it is no capture,
# and these are the sessions per-turn capture already missed.

PAST, FUTURE = 100.0, 1_000_000.0
check("slice 1 goes even past the deadline",
      fs._stop_before_slice(1, PAST + 1, PAST) is False)
check("slice 1 goes when the deadline is already behind us",
      fs._stop_before_slice(1, 1e9, PAST) is False)
check("slice 2 stops past the deadline",
      fs._stop_before_slice(2, PAST + 1, PAST) is True)
check("slice 2 continues before the deadline",
      fs._stop_before_slice(2, PAST, FUTURE) is False)
check("exactly at the deadline stops", fs._stop_before_slice(3, PAST, PAST))

# Stops with time still on the clock, if there is not ENOUGH of it. Bounding
# each slice by the remaining budget means a slice started with two seconds left
# gets a two-second network timeout and fails — reporting a spurious error in
# place of a clean stop that names what was not sent.
check("too little budget left stops before starting",
      fs._stop_before_slice(2, 0.0, fs._MIN_SLICE_BUDGET_S - 1) is True)
check("enough budget left proceeds",
      fs._stop_before_slice(2, 0.0, fs._MIN_SLICE_BUDGET_S + 1) is False)
# ...but the first slice still always goes: a backstop that sends nothing is
# not a degraded capture, it is no capture.
check("the first slice goes regardless",
      fs._stop_before_slice(1, 0.0, 0.0) is False)


# Since 3.11 socket.timeout IS TimeoutError, so a network read that gave up
# inside the client reaches main() indistinguishable BY TYPE from our own
# wall clock. Reporting "timed out after 240s" for a socket that died in 3s
# sends the reader looking for a slow transcript instead of a sick
# connection, so the two are told apart by elapsed time.

def run_main(raises, deadline="0.5"):
    """main() with a stubbed _flush, capturing what it logged."""
    lines = []
    real_flush, real_log = fs._flush, fs._log
    stdin = sys.stdin

    async def fake_flush(session_id, transcript_path, session_ended=False):
        if raises == "slow":
            await asyncio.sleep(10)          # our wall clock wins
        raise TimeoutError("connection timed out")  # an inner socket timeout

    class FakeIn:
        def read(self):
            # json.dumps, NOT %-interpolation: a Windows __file__ carries
            # backslashes, which spliced raw are invalid JSON escapes — the
            # payload would read as malformed and never reach the fake.
            return _json.dumps({"session_id": "s1",
                                "transcript_path": __file__})

    os.environ["MEMHUB_FLUSH_DEADLINE_S"] = deadline
    fs._flush, fs._log, sys.stdin = fake_flush, lines.append, FakeIn()
    try:
        code = fs.main()
    finally:
        fs._flush, fs._log, sys.stdin = real_flush, real_log, stdin
        os.environ.pop("MEMHUB_FLUSH_DEADLINE_S", None)
    return code, " ".join(lines)


code, logged = run_main("slow")
check("the wall clock exits 0", code == 0)
check("the wall clock reports a partial capture, not a crash",
      "timed out after" in logged and "already sent are stored" in logged)

code, logged = run_main("inner")
check("an inner socket timeout exits 0", code == 0)
check("an inner socket timeout is NOT reported as the wall clock",
      "timed out after" not in logged)
check("an inner socket timeout is still reported",
      "skipped" in logged and "TimeoutError" in logged)

# A malformed payload must not make the handler itself raise: `started` and
# `timeout_s` are read there, so assigning them inside the try would turn any
# earlier failure into a NameError traceback in the user's session.


class BadIn:
    def read(self):
        return "{not json"


_stdin, _log = sys.stdin, fs._log
lines = []
sys.stdin, fs._log = BadIn(), lines.append
try:
    check("a malformed payload still exits 0", fs.main() == 0)
finally:
    sys.stdin, fs._log = _stdin, _log
check("a malformed payload is reported, not raised",
      any("skipped" in line for line in lines))


# The harness's authoring pass forks the person's session under a NEW id
# (`claude -p --resume <owner> --fork-session`). Its SessionEnd and commit
# flushes shipped that copy as another conversation, once per pass. The same
# payload without the flag must reach the flush, or this proves nothing.


def _child_run(flag):
    calls = []

    async def fake_flush(session_id, transcript_path, session_ended=False):
        calls.append(session_id)

    class FakeIn:
        def read(self):
            return _json.dumps({"session_id": "fork-1", "transcript_path": __file__})

    real_flush, real_log, stdin = fs._flush, fs._log, sys.stdin
    saved = os.environ.pop("MEMHUB_HARNESS_CHILD", None)
    os.environ["MEMHUB_HARNESS_CHILD"] = flag
    fs._flush, fs._log, sys.stdin = fake_flush, (lambda _m: None), FakeIn()
    try:
        return fs.main(), calls
    finally:
        fs._flush, fs._log, sys.stdin = real_flush, real_log, stdin
        os.environ.pop("MEMHUB_HARNESS_CHILD", None)
        if saved is not None:
            os.environ["MEMHUB_HARNESS_CHILD"] = saved


check("a harness child exits 0 and ships nothing", _child_run("1") == (0, []))
check("the same payload outside a child ships", _child_run("") == (0, ["fork-1"]))


# ── agents and workflow runs (MemHub-Backend agent-sessions spec §10.2) ──
#
# What a full flush sends for a session that spawned Agent-tool agents, MemHub
# harness forks and three dynamic workflows, against each server generation:
# one without agent capture, one with §4.1 only, and one with the details and
# runs. The server's tools are listed once, before the session goes.

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agent_fixture as fx  # noqa: E402
import redact as _redact  # noqa: E402

_AGENTS_41 = {"messages": {}, "agent_id": {}, "parent_conversation_id": {}}
SERVER_OLD = [{"name": "import_conversation", "inputSchema": {"properties": {"messages": {}}}}]
SERVER_41 = [{"name": "import_conversation", "inputSchema": {"properties": dict(_AGENTS_41)}}]
SERVER_FULL = [{"name": "import_conversation", "inputSchema": {"properties": {
    **_AGENTS_41, "parent_agent_id": {}, "workflow_run_id": {}, "agent_status": {},
    "agent_result_preview": {}, "workflow_runs": {}}}}]
DETAIL_ARGS = {"parent_agent_id", "workflow_run_id", "agent_status", "agent_result_preview"}


class RecordingSession:
    def __init__(self, *_a, **_k):
        self.sent = RecordingSession.sent

    async def call_tool(self, name, arguments=None, timeout=None):
        self.sent.append(dict(arguments))
        return FakeResult(structured={"conversation_id": arguments["conversation_id"],
                                      "messages_received": len(arguments["messages"]),
                                      "path": "agentic"})


def flush_tree(tree, tools, state_dir, ended=True):
    """One full flush of ``tree`` against a server listing ``tools``.
    Returns (imports sent, list_tools call count)."""
    RecordingSession.sent = []
    listed = []

    def list_tools(url, bearer, *a, **k):
        listed.append(1)
        return tools

    real = (fs.resolve_bearer, fs.mcp_http.Session, fs.mcp_http.list_tools,
            fs.make_slices, fs._SESSION_STATE_DIR, fs._log)
    fs.resolve_bearer = lambda: ("https://example.invalid/mcp", "tok")
    fs.mcp_http.Session = RecordingSession
    fs.mcp_http.list_tools = list_tools
    # Small slices, so the session goes in several and "the last" means something.
    fs.make_slices = lambda records: real[3](records, chunk_bytes=4000)
    fs._SESSION_STATE_DIR = state_dir
    fs._log = lambda _m: None
    try:
        asyncio.run(fs._flush(fx.SESSION, str(tree.transcript), ended))
    finally:
        (fs.resolve_bearer, fs.mcp_http.Session, fs.mcp_http.list_tools,
         fs.make_slices, fs._SESSION_STATE_DIR, fs._log) = real
    return RecordingSession.sent, len(listed)


def session_slices(sent):
    return [a for a in sent if a["conversation_id"] == fx.SESSION]


def agent_imports(sent):
    return [a for a in sent if a.get("parent_conversation_id") == fx.SESSION]


_root = Path(tempfile.mkdtemp(prefix="flush-agents-"))
_tree = fx.mirror_9b073366(_root / "proj")

# (a) No agent capture on the server: no agents, no runs.
_state = _root / "state-a"
_state.mkdir()
sent, listed = flush_tree(_tree, SERVER_OLD, _state)
check("old server: tools listed once", listed == 1)
check("old server: no agents sent", agent_imports(sent) == [])
check("old server: no workflow_runs sent", not any("workflow_runs" in a for a in sent))
check("old server: the session still goes", len(session_slices(sent)) > 1)

# (b) §4.1 only: 74 agents with only the §4.1 arguments, no runs.
_state = _root / "state-b"
_state.mkdir()
sent, listed = flush_tree(_tree, SERVER_41, _state)
_agents = {a["conversation_id"] for a in agent_imports(sent)}
check("4.1 server: tools listed once", listed == 1)
check("4.1 server: 74 agents sent", len(_agents) == 74)
check("4.1 server: no harness fork sent",
      not any(f in str(_agents) for f in fx.HARNESS_FORKS))
check("4.1 server: no detail arguments",
      not any(DETAIL_ARGS & set(a) for a in agent_imports(sent)))
check("4.1 server: no workflow_runs", not any("workflow_runs" in a for a in sent))
_wf = [a for a in agent_imports(sent) if a.get("agent_type") == "workflow-subagent"]
check("4.1 server: workflow agents go as plain agents",
      len({a["agent_id"] for a in _wf}) == 64
      and not any("parent_tool_use_id" in a for a in _wf))
_cache = _json.loads((_state / f"{fx.SESSION}.agents").read_text())
check("4.1 server: cache keys carry size, status and 'b'",
      _cache[fx.TOOL_AGENTS[0]].endswith(":done:b")
      and _cache["as00"].endswith(":-:b"))

# The same server again: nothing re-sent.
sent, listed = flush_tree(_tree, SERVER_41, _state)
check("4.1 server: an unchanged agent is not re-sent", agent_imports(sent) == [])

# (c) The server gains the details: every agent is re-sent once, with them.
sent, listed = flush_tree(_tree, SERVER_FULL, _state)
check("full server: tools listed once", listed == 1)
_imports = agent_imports(sent)
check("full server: each agent re-sent once on gaining details",
      len({a["agent_id"] for a in _imports}) == 74)
_by = {}
for a in _imports:
    _by.setdefault(a["agent_id"], a)
check("full server: tool agents carry status and result",
      _by[fx.TOOL_AGENTS[0]].get("agent_status") == "done"
      and len(_by[fx.TOOL_AGENTS[0]].get("agent_result_preview", "")) == 2000)
check("full server: workflow agents carry their run, never a status",
      _by["as00"].get("workflow_run_id") == fx.RUN_SPLIT
      and "agent_status" not in _by["as00"]
      and _by["as00"].get("agent_description") == "design:A")
_slices = session_slices(sent)
check("full server: workflow_runs rides only on the last session slice",
      len(_slices) > 1 and "workflow_runs" in _slices[-1]
      and not any("workflow_runs" in a for a in _slices[:-1]))
check("full server: three runs sent", len(_slices[-1]["workflow_runs"]) == 3)
check("full server: never on an agent import",
      not any("workflow_runs" in a for a in _imports))
check("full server: runs are redacted", not _redact.contains_secret(_slices[-1]["workflow_runs"]))
sent, listed = flush_tree(_tree, SERVER_FULL, _state)
check("full server: runs re-sent on every flush, agents not",
      "workflow_runs" in session_slices(sent)[-1] and agent_imports(sent) == [] and listed == 1)

# A legacy integer cache entry never matches: that agent is re-sent once.
_cache = _json.loads((_state / f"{fx.SESSION}.agents").read_text())
_cache[fx.TOOL_AGENTS[1]] = 12345
(_state / f"{fx.SESSION}.agents").write_text(_json.dumps(_cache))
sent, _ = flush_tree(_tree, SERVER_FULL, _state)
check("a legacy cache entry re-sends that agent once",
      {a["agent_id"] for a in agent_imports(sent)} == {fx.TOOL_AGENTS[1]})

# A status change re-keys the cache: a running agent that finishes is re-sent.
_live = Path(tempfile.mkdtemp(prefix="flush-live-"))
_t2 = fx.Tree(_live / "proj")
_t2.records = [fx.assistant("r0", [fx.tool_use("toolu_L", "Agent", prompt="go")], "s0")]
_t2.write_session()
_mid = [fx.user("go", "u"), fx.assistant("r1", [fx.tool_use("t", "Bash", command="ls")], "a",
                                         stop_reason="tool_use")]
_t2.agent("l1", _mid, agentType="general-purpose", toolUseId="toolu_L")
(_live / "state").mkdir()
sent, _ = flush_tree(_t2, SERVER_FULL, _live / "state", ended=False)
check("a running agent is sent as running",
      [a.get("agent_status") for a in agent_imports(sent)] == ["running"])
sent, _ = flush_tree(_t2, SERVER_FULL, _live / "state", ended=True)
check("the same bytes at session end are re-sent as stopped",
      [a.get("agent_status") for a in agent_imports(sent)] == ["stopped"])

# No agents and no runs: the server's tools are not listed at all.
_bare = Path(tempfile.mkdtemp(prefix="flush-bare-"))
_t3 = fx.Tree(_bare / "proj")
_t3.records = [fx.user("hi", "p0")]
_t3.write_session()
(_bare / "state").mkdir()
sent, listed = flush_tree(_t3, SERVER_FULL, _bare / "state")
check("nothing to send beyond the session: no tools listed", listed == 0 and len(sent) == 1)

# A fork's copy of its owner's spawning response never leaves the machine.
_fk = Path(tempfile.mkdtemp(prefix="flush-fork-"))
_t4 = fx.Tree(_fk / "proj")
_spawn = [fx.tool_use("toolu_K", "Agent", subagent_type="fork", prompt="p")]
_t4.records = [fx.assistant("req_owner", _spawn, "s", fx.T0, "tool_use"),
               fx.user([fx.tool_result("toolu_K", "Fork started")], "r")]
_t4.write_session()
_t4.agent("k1", fx.fork_records("k1", "req_owner", "toolu_K", fx.T0, _spawn),
          agentType="fork", toolUseId="toolu_K")
(_fk / "state").mkdir()
for _server in (SERVER_41, SERVER_FULL):
    sent, _ = flush_tree(_t4, _server, _fk / "state")
    _msgs = [m for a in agent_imports(sent) for m in a["messages"]]
    check("fork copies stripped for every server that takes agents",
          _msgs and "req_owner" not in _json.dumps(_msgs)
          and "fork-context-ref" not in _json.dumps(_msgs))

# main() hands the flush whether the session ended.
def _ended_for(payload):
    seen = []

    async def fake_flush(session_id, transcript_path, session_ended=False):
        seen.append(session_ended)

    class FakeIn:
        def read(self):
            return _json.dumps({"session_id": "s1", "transcript_path": __file__, **payload})

    real_flush, real_log, stdin = fs._flush, fs._log, sys.stdin
    fs._flush, fs._log, sys.stdin = fake_flush, (lambda _m: None), FakeIn()
    try:
        fs.main()
    finally:
        fs._flush, fs._log, sys.stdin = real_flush, real_log, stdin
    return seen


check("SessionEnd is a session end", _ended_for({"hook_event_name": "SessionEnd",
                                                 "reason": "exit"}) == [True])
check("a commit is not", _ended_for({"hook_event_name": "PostToolUse",
                                     "tool_input": {"command": "git commit"}}) == [False])


print(f"{'FAIL' if FAILURES else 'PASS'}: flush_session")
for f in FAILURES:
    print(f"  - {f}")
sys.exit(1 if FAILURES else 0)
