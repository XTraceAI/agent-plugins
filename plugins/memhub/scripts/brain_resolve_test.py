"""Self-test for the repo-brain auto-resolve and its negative cache.

The failure this guards against is silent: capture lands in personal memory
while the team's repo brain sits empty, and nothing reports an error. So these
assert the routing decisions rather than any error path.

Run: python3 brain_resolve_test.py   (stdlib only)
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_resolve as br  # noqa: E402
import room_map as rm  # noqa: E402

_failures = []


def check(label, got, want):
    if got != want:
        _failures.append(label)
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


class _Result:
    """Stand-in for an MCP tool result."""

    def __init__(self, payload=None, is_error=False, text=None):
        self.structuredContent = payload
        self.isError = is_error
        self.content = [type("B", (), {"text": text})()] if text else []


class _Session:
    def __init__(self, result, record=None):
        self._result = result
        self.calls = record if record is not None else []

    async def call_tool(self, name, arguments=None):
        self.calls.append(name)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


NAME = "Repo: XTraceAI/memhub-claude-plugin"
BID = "11111111-2222-3333-4444-555555555555"


def _isolate(tmp):
    """Point room_map at a scratch cache and pin the repo name."""
    rm.ROOMS_PATH = Path(tmp) / "rooms.json"
    br.read_room = rm.read_room
    br.resolve_due = rm.resolve_due
    br.write_miss = rm.write_miss
    br.write_room = rm.write_room
    br.room_name = lambda cwd=None: NAME
    rm.room_name = lambda cwd=None: NAME


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_resolves_and_caches():
    print("resolve on cache miss")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        session = _Session(_Result({"agent_brains": [
            {"name": "Repo: XTraceAI/other", "agent_brain_id": "nope"},
            {"name": NAME, "agent_brain_id": BID},
        ]}))
        room = run(br.resolve_repo_brain(session, "/repo", "staging"))
        check("found the exact match", (room or {}).get("brain_id"), BID)
        check("asked the server once", session.calls, ["list_agent_brains"])
        check("cached for next time",
              (rm.read_room("/repo", "staging") or {}).get("brain_id"), BID)

        # Second call must be a local lookup — no second round-trip.
        session2 = _Session(_Result({"agent_brains": []}))
        room = run(br.resolve_repo_brain(session2, "/repo", "staging"))
        check("cache hit does not call the server", session2.calls, [])
        check("cache hit still routes", (room or {}).get("brain_id"), BID)


def test_no_brain_is_remembered_as_a_miss():
    """Without a negative entry every turn would re-query for a repo that
    simply has no room."""
    print("negative cache")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        session = _Session(_Result({"agent_brains": [
            {"name": "Repo: someone/else", "agent_brain_id": "x"}]}))
        room = run(br.resolve_repo_brain(session, "/repo", "staging"))
        check("no match -> no room", room, None)
        check("miss recorded", rm.resolve_due("/repo", "staging"), False)

        session2 = _Session(_Result({"agent_brains": []}))
        run(br.resolve_repo_brain(session2, "/repo", "staging"))
        check("does not re-ask inside the TTL", session2.calls, [])

        # A brain created later must still be picked up once the TTL lapses.
        data = json.loads(rm.ROOMS_PATH.read_text())
        data["repos"][NAME]["staging"]["missed_at"] = time.time() - rm.MISS_TTL_S - 1
        rm.ROOMS_PATH.write_text(json.dumps(data))
        check("due again after the TTL", rm.resolve_due("/repo", "staging"), True)


def test_never_routes_on_a_fuzzy_or_broken_match():
    print("match strictness")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        # Similar names must not win — a wrong room is worse than no room,
        # because it lands teammate-visible content somewhere unexpected.
        session = _Session(_Result({"agent_brains": [
            {"name": NAME.lower(), "agent_brain_id": "wrong-case"},
            {"name": NAME + " (old)", "agent_brain_id": "wrong-suffix"},
        ]}))
        check("near-miss names are ignored",
              run(br.resolve_repo_brain(session, "/repo", "staging")), None)

    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        session = _Session(_Result({"agent_brains": [
            {"name": NAME, "agent_brain_id": 12345}]}))
        check("a non-string id is not a target",
              run(br.resolve_repo_brain(session, "/repo", "staging")), None)


def test_failures_degrade_to_no_room():
    """A capture hook must never fail because a lookup did."""
    print("failure handling")
    for label, session in [
        ("tool error", _Session(_Result({}, is_error=True))),
        ("exception", _Session(RuntimeError("network down"))),
        ("unparseable payload", _Session(_Result(None, text="not json"))),
    ]:
        with tempfile.TemporaryDirectory() as tmp:
            _isolate(tmp)
            check(f"{label} -> no room",
                  run(br.resolve_repo_brain(session, "/repo", "staging")), None)


def test_result_shapes_are_tolerated():
    print("payload shapes")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        wrapped = _Session(_Result({"result": {"agent_brains": [
            {"name": NAME, "agent_brain_id": BID}]}}))
        check("FastMCP {result: …} wrapper",
              (run(br.resolve_repo_brain(wrapped, "/repo", "staging")) or {}).get("brain_id"),
              BID)
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        as_text = _Session(_Result(
            None, text=json.dumps({"agent_brains": [
                {"name": NAME, "agent_brain_id": BID}]})))
        check("JSON in a text block",
              (run(br.resolve_repo_brain(as_text, "/repo", "staging")) or {}).get("brain_id"),
              BID)


if __name__ == "__main__":
    for fn in (test_resolves_and_caches, test_no_brain_is_remembered_as_a_miss,
               test_never_routes_on_a_fuzzy_or_broken_match,
               test_failures_degrade_to_no_room, test_result_shapes_are_tolerated):
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        raise SystemExit(1)
    print("all passed")
