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

# The tests live outside the plugin so they are not shipped to users;
# the code under test is still in the plugin's scripts dir.
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
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
    """Fake MCP session.

    ``result`` is either one result returned for every tool, or a callable
    ``(name, arguments) -> result`` when a test needs different answers per
    tool.
    """

    def __init__(self, result, record=None):
        self._result = result
        self.calls = record if record is not None else []
        self.args = []

    async def call_tool(self, name, arguments=None):
        self.calls.append(name)
        self.args.append((name, dict(arguments or {})))
        result = (self._result(name, arguments or {})
                  if callable(self._result) else self._result)
        if isinstance(result, Exception):
            raise result
        return result


NAME = "Repo: XTraceAI/agent-plugins"
BID = "11111111-2222-3333-4444-555555555555"


def _isolate(tmp):
    rm.ROOMS_PATH = Path(tmp) / "rooms.json"
    br.read_room = rm.read_room
    br.resolve_due = rm.resolve_due
    br.write_miss = rm.write_miss
    br.write_room = rm.write_room
    br.room_name = lambda cwd=None: NAME
    rm.room_name = lambda cwd=None: NAME


def run(coro):
    # asyncio.run, not get_event_loop().run_until_complete: implicit loop
    # creation was removed in Python 3.14, where the old spelling raises
    # "There is no current event loop".
    return asyncio.run(coro)


def test_resolves_and_caches():
    print("resolve on cache miss")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        session = _Session(_Result({"agent_brains": [
            {"name": "Repo: XTraceAI/other", "agent_brain_id": "nope"},
            {"name": NAME, "agent_brain_id": BID, "org_id": "org-with-room"},
        ]}))
        room = run(br.resolve_repo_brain(session, "/repo", "staging"))
        check("found the exact match", (room or {}).get("brain_id"), BID)
        check("returned the org the row names", (room or {}).get("org_id"),
              "org-with-room")
        check("one call, and it is the repo lookup", session.calls,
              ["list_agent_brains"])
        check("asked with repo=<org>/<name>, no org walk", session.args[0][1],
              {"repo": "XTraceAI/agent-plugins"})
        cached = rm.read_room("/repo", "staging") or {}
        check("cached for next time", cached.get("brain_id"), BID)
        check("cached with its org", cached.get("org_id"), "org-with-room")
        check("and settled", rm.resolve_due("/repo", "staging"), False)

        session2 = _Session(_Result({"agent_brains": []}))
        room = run(br.resolve_repo_brain(session2, "/repo", "staging"))
        check("cache hit does not call the server", session2.calls, [])
        check("cache hit still routes", (room or {}).get("brain_id"), BID)


def test_no_brain_is_remembered_as_a_miss():
    """Without a negative entry every save would re-query for a repo that
    simply has no room."""
    print("negative cache")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        answer = _Result({"agent_brains": []})
        session = _Session(answer)
        room = run(br.resolve_repo_brain(session, "/repo", "staging"))
        check("no match -> no room", room, None)
        check("miss recorded", rm.resolve_due("/repo", "staging"), False)
        entry = rm._load()["repos"][NAME]["staging"]
        check("a 24h miss, not a probe backoff", "missed_at" in entry, True)

        session2 = _Session(answer)
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


def test_a_listing_that_ignored_repo_is_still_name_matched():
    """A backend that did not honour ``repo=`` hands back every brain. The
    one-row answer is not "the repo's brain" unless it carries the room name."""
    print("repo filter not trusted blindly")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        session = _Session(_Result({"agent_brains": [
            {"name": "Some newest brain", "agent_brain_id": "newest"}]}))
        check("an unrelated sole row is not routed to",
              run(br.resolve_repo_brain(session, "/repo", "staging")), None)
        check("nothing cached", rm.read_room("/repo", "staging"), None)


def test_a_miss_never_clobbers_a_resolved_room():
    """The lookup behind a miss listed brains at some EARLIER moment, so by the
    time it writes, someone else may have resolved or created the room —
    /memhub:onboard racing a background save is the obvious case."""
    print("miss vs resolved id")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        rm.write_room(BID, name=NAME, env="staging")
        rm.write_miss("/repo", "staging")          # the racing loser
        check("resolved id survives",
              (rm.read_room("/repo", "staging") or {}).get("brain_id"), BID)
        check("still not due", rm.resolve_due("/repo", "staging"), False)

        # A miss on a repo with nothing cached is still recorded.
        rm.room_name = lambda cwd=None: "Repo: XTraceAI/other"
        rm.write_miss("/other", "staging")
        check("plain miss still recorded",
              rm.resolve_due("/other", "staging"), False)


def test_duplicate_room_names_are_not_guessed_between():
    """Duplicate rooms for one repo happen — now across orgs too, since one
    call sees every org. Picking whichever came first would route this repo's
    memory into an arbitrary one — invisibly, and differently for different
    teammates."""
    print("duplicate room names")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        session = _Session(_Result({"agent_brains": [
            {"name": NAME, "agent_brain_id": BID, "org_id": "org-a"},
            {"name": NAME, "agent_brain_id": "22222222-3333-4444-5555-666666666666",
             "org_id": "org-b"},
        ]}))
        check("ambiguous -> no room",
              run(br.resolve_repo_brain(session, "/repo", "staging")), None)
        check("nothing cached", rm.read_room("/repo", "staging"), None)
        # Deliberately NOT a miss: merging the duplicates should take effect on
        # the next save, not after a 24h TTL.
        check("stays due so a fix applies immediately",
              rm.resolve_due("/repo", "staging"), True)


def test_one_brain_listed_twice_is_not_a_duplicate():
    print("same brain, two rows")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        session = _Session(_Result({"agent_brains": [
            {"name": NAME, "agent_brain_id": BID},
            {"name": NAME, "agent_brain_id": BID, "org_id": "org-confirmed"},
        ]}))
        room = run(br.resolve_repo_brain(session, "/repo", "staging"))
        check("resolved to the one brain", (room or {}).get("brain_id"), BID)
        check("took the sighting that names its org", (room or {}).get("org_id"),
              "org-confirmed")


def test_failures_degrade_to_no_room():
    """A capture hook must never fail because a lookup did — and a lookup
    that got no answer must not brand the repo room-less for a day."""
    print("failure handling")
    for label, session in [
        ("tool error", _Session(_Result({}, is_error=True))),
        ("exception", _Session(RuntimeError("network down"))),
    ]:
        with tempfile.TemporaryDirectory() as tmp:
            _isolate(tmp)
            check(f"{label} -> no room",
                  run(br.resolve_repo_brain(session, "/repo", "staging")), None)
            entry = rm._load()["repos"].get(NAME, {}).get("staging", {})
            check(f"{label} -> no 24h miss", "missed_at" in entry, False)
            check(f"{label} -> backed off for minutes",
                  rm.resolve_due("/repo", "staging"), False)
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        check("unparseable payload -> no room",
              run(br.resolve_repo_brain(_Session(_Result(None, text="not json")),
                                        "/repo", "staging")), None)


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


def test_a_room_cached_without_an_org_is_resolved_again():
    """Upgrade path: an org-less entry is re-asked on the TTL clock, and a
    row that names its org then settles it."""
    print("legacy cache upgrade")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        rm.write_room(BID, name=NAME, env="staging")
        entry = rm._load()["repos"][NAME]["staging"]
        entry.pop("org_id", None)
        entry.pop("resolved_at", None)
        rm.ROOMS_PATH.write_text(
            json.dumps({"version": 1, "repos": {NAME: {"staging": entry}}}),
            encoding="utf-8")

        check("due again", rm.resolve_due("/repo", "staging"), True)
        run(br.resolve_repo_brain(_Session(_Result({"agent_brains": [
            {"name": NAME, "agent_brain_id": BID, "org_id": "org-with-room"}]})),
            "/repo", "staging"))
        check("upgraded in place",
              (rm.read_room("/repo", "staging") or {}).get("org_id"),
              "org-with-room")
        check("and settles", rm.resolve_due("/repo", "staging"), False)


def test_an_org_less_row_does_not_re_resolve_every_turn():
    """If the rows carry no org, the entry stays org-less. It still routes
    (the server derives the org from the brain id) and is rate-limited, or a
    per-save round trip would replace a cache hit."""
    print("org-less rate limit")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        room = run(br.resolve_repo_brain(_Session(_Result({"agent_brains": [
            {"name": NAME, "agent_brain_id": BID}]})), "/repo", "staging"))
        check("still routes without an org", (room or {}).get("brain_id"), BID)
        check("no org recorded", (rm.read_room("/repo", "staging") or {})
              .get("org_id"), None)
        check("not due again immediately", rm.resolve_due("/repo", "staging"),
              False)
        data = rm._load()
        data["repos"][NAME]["staging"]["resolved_at"] = 0
        rm.ROOMS_PATH.write_text(json.dumps(data), encoding="utf-8")
        check("due again after the TTL", rm.resolve_due("/repo", "staging"), True)


def test_a_remote_less_room_is_asked_by_its_bare_name():
    print("no-remote room name")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        br.room_name = lambda cwd=None: "Repo: scratch"
        session = _Session(_Result({"agent_brains": []}))
        run(br.resolve_repo_brain(session, "/repo", "staging"))
        check("repo= carries the bare name", session.args[0][1], {"repo": "scratch"})
        br.room_name = lambda cwd=None: NAME


def test_is_missing_brain():
    """Which server error licenses forgetting a cached room.

    This is the trigger for the only path that deletes a resolved id, so both
    directions matter: too narrow and a dead entry stays dead (capture fails on
    every turn, which is the bug this shipped to fix); too broad and a transient
    outage throws away a good room.
    """
    print("is_missing_brain")
    check("the bare sentence", br.is_missing_brain("Agent brain not found"), True)
    # What the server actually sends — the tool name is wrapped around it.
    check("the server's wrapped form", br.is_missing_brain(
        "Error executing tool import_conversation: Agent brain not found"), True)
    check("case does not matter",
          br.is_missing_brain("AGENT BRAIN NOT FOUND"), True)
    # Callers pass the list of text blocks straight off an MCP result.
    check("a list of blocks", br.is_missing_brain(
        ["something else", "Agent brain not found"]), True)

    # Everything below says nothing about whether the brain exists. Forgetting
    # a room over any of these would drop a good cache entry over a blip.
    check("auth failure is not a missing brain",
          br.is_missing_brain("Not authenticated"), False)
    check("a server error is not a missing brain",
          br.is_missing_brain("500 Internal Server Error"), False)
    check("a missing ARTIFACT is not a missing brain",
          br.is_missing_brain("Artifact not found"), False)
    check("no detail at all", br.is_missing_brain([]), False)
    check("none is not a match", br.is_missing_brain(None), False)
    # Blocks can carry a None text; the extractor upstream filters those, but
    # this must not raise if one slips through.
    check("a null block does not raise", br.is_missing_brain([None]), False)


if __name__ == "__main__":
    for fn in (test_resolves_and_caches, test_no_brain_is_remembered_as_a_miss,
               test_a_miss_never_clobbers_a_resolved_room,
               test_duplicate_room_names_are_not_guessed_between,
               test_one_brain_listed_twice_is_not_a_duplicate,
               test_never_routes_on_a_fuzzy_or_broken_match,
               test_a_listing_that_ignored_repo_is_still_name_matched,
               test_failures_degrade_to_no_room, test_result_shapes_are_tolerated,
               test_a_room_cached_without_an_org_is_resolved_again,
               test_an_org_less_row_does_not_re_resolve_every_turn,
               test_a_remote_less_room_is_asked_by_its_bare_name,
               test_is_missing_brain):
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        raise SystemExit(1)
    print("all passed")
