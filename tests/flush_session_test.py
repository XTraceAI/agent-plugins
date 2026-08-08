#!/usr/bin/env python3
"""What the whole-transcript flush puts on the wire, and when it gives up.

Run: uv run --with 'mcp<2' python plugins/memhub/scripts/flush_session_test.py
(the module imports the MCP SDK through ``_memhub_auth``)

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
# health check's warnings on a real machine.
_TMP_HOME = tempfile.mkdtemp(prefix="flush-session-test-")
os.environ["HOME"] = _TMP_HOME

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
    """Records what was sent and replies with whatever it was primed with."""

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


def send(result, room=None, title=None, namespace=None, index=1, total=1):
    session = FakeSession(result)
    args = {"messages": [{"type": "user"}], "conversation_id": "s1",
            "source_platform": "claude"}
    ok = asyncio.run(fs._send(session, args, room, title, namespace,
                              index, total))
    return ok, (session.sent[0][1] if session.sent else None)


# ── what goes on the wire ─────────────────────────────────────────────

ok, args = send(OK)
check("a bare send succeeds", ok is True)
check("no room means no brain id", "agent_brain_id" not in args)
check("no room means no org", "org_id" not in args)
check("nothing invents a title", "title" not in args)

# A brain resolves inside exactly ONE org. Sending the id without its org is
# how every capture into a non-default-org room failed with "Agent brain not
# found" — the bug this path had and the per-turn path had already fixed.
ok, args = send(OK, room={"brain_id": "b1", "org_id": "o1"})
check("the room's brain id is sent", args.get("agent_brain_id") == "b1")
check("the org that OWNS the room rides along", args.get("org_id") == "o1")

ok, args = send(OK, room={"brain_id": "b1"})
check("an org-less room still routes", args.get("agent_brain_id") == "b1")
check("an org-less room sends no org", "org_id" not in args)

ok, args = send(OK, title="Fix the flush hook", namespace="memhub")
check("the title is sent", args.get("title") == "Fix the flush hook")
check("the namespace is sent", args.get("namespace") == "memhub")


# ── the success / failure contract ────────────────────────────────────

# MCP signals tool failure with isError, NOT an exception. Treating that as
# success would report a capture that never happened.
ok, _ = send(FakeResult(texts=["Agent brain not found"], is_error=True))
check("an isError reply stops the run", ok is False)

# Not an error per the protocol, but not the shape import_conversation
# returns either — a slice that cannot be confirmed did not land.
ok, _ = send(FakeResult(structured={"something": "else"}))
check("an unrecognized body stops the run", ok is False)

ok, _ = send(FakeResult(texts=['{"conversation_id": "c1"}']))
check("a JSON text body is understood", ok is True)

ok, _ = send(FakeResult(structured={"result": {"conversation_id": "c1"}}))
check("a FastMCP-wrapped body is unwrapped", ok is True)


# ── the deadline ──────────────────────────────────────────────────────

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


# ── the first slice always goes ───────────────────────────────────────
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


# ── which timeout is being reported ───────────────────────────────────
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

    async def fake_flush(session_id, transcript_path):
        if raises == "slow":
            await asyncio.sleep(10)          # our wall clock wins
        raise TimeoutError("connection timed out")  # an inner socket timeout

    class FakeIn:
        def read(self):
            return ('{"session_id": "s1", "transcript_path": "%s"}'
                    % __file__)

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


print(f"{'FAIL' if FAILURES else 'PASS'}: flush_session")
for f in FAILURES:
    print(f"  - {f}")
sys.exit(1 if FAILURES else 0)
