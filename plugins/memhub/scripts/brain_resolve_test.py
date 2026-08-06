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
    """Fake MCP session.

    ``result`` is either one result returned for every tool, or a callable
    ``(name, arguments) -> result`` when a test needs different answers per
    tool or per org — which resolution now needs, since it asks ``list_orgs``
    and then ``list_agent_brains`` once per org until it finds the room.
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


def _orgs(*org_ids):
    """A ``list_orgs`` payload. The FIRST id is the default org."""
    return _Result({"orgs": [
        {"org_id": o, "name": o, "is_default": i == 0}
        for i, o in enumerate(org_ids)
    ]})


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
        # Orgs are enumerated first so the match can be cached WITH its org.
        # The guarantee that matters is below: this happens once, then never
        # again for this repo.
        check("listed brains once", session.calls.count("list_agent_brains"), 1)
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

        # ``list_orgs`` has to answer for real now: a miss is only recorded
        # after a COMPLETE search, so a fixture that cannot enumerate orgs
        # would (correctly) decline to brand this repo room-less.
        def answer(name_, args):
            if name_ == "list_orgs":
                return _orgs("org-default")
            return _Result({"agent_brains": [
                {"name": "Repo: someone/else", "agent_brain_id": "x"}]})

        session = _Session(answer)
        room = run(br.resolve_repo_brain(session, "/repo", "staging"))
        check("no match -> no room", room, None)
        check("miss recorded", rm.resolve_due("/repo", "staging"), False)

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


def test_a_miss_never_clobbers_a_resolved_room():
    """The lookup behind a miss listed brains at some EARLIER moment, so by the
    time it writes, someone else may have resolved or created the room —
    /memhub:onboard racing a background flush is the obvious case. Clobbering
    there would silently send capture to personal memory for the whole TTL,
    right after the user did the thing meant to fix that."""
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
    """Duplicate rooms for one repo happen. Picking whichever came first would
    route this repo's memory into an arbitrary one — invisibly, and differently
    for different teammates."""
    print("duplicate room names")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        session = _Session(_Result({"agent_brains": [
            {"name": NAME, "agent_brain_id": BID},
            {"name": NAME, "agent_brain_id": "22222222-3333-4444-5555-666666666666"},
        ]}))
        check("ambiguous -> no room",
              run(br.resolve_repo_brain(session, "/repo", "staging")), None)
        check("nothing cached", rm.read_room("/repo", "staging"), None)
        # Deliberately NOT a miss: merging the duplicates should take effect on
        # the next flush, not after a 24h TTL.
        check("stays due so a fix applies immediately",
              rm.resolve_due("/repo", "staging"), True)


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


def test_finds_a_room_outside_the_default_org():
    """The failure this exists to fix. A room in a non-default org was
    invisible to resolution — ``list_agent_brains`` lists ONE org — and every
    capture into it then died with "Agent brain not found"."""
    print("cross-org resolution")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)

        def answer(name, args):
            if name == "list_orgs":
                return _orgs("org-default", "org-with-room")
            if args.get("org_id") == "org-with-room":
                return _Result({"agent_brains": [
                    {"name": NAME, "agent_brain_id": BID}]})
            return _Result({"agent_brains": []})  # default org has nothing

        session = _Session(answer)
        room = run(br.resolve_repo_brain(session, "/repo", "staging"))
        check("found it in the non-default org", (room or {}).get("brain_id"), BID)
        check("returned the org too", (room or {}).get("org_id"), "org-with-room")
        cached = rm.read_room("/repo", "staging") or {}
        check("cached the org alongside the id", cached.get("org_id"),
              "org-with-room")
        check("searched the default org first",
              [a.get("org_id") for n, a in session.args if n == "list_agent_brains"],
              ["org-default", "org-with-room"])


def test_a_room_visible_from_several_orgs_is_not_a_duplicate():
    """The SAME brain id seen from two orgs is one room, not an ambiguity.

    Every org is still searched — that is what makes a real cross-org duplicate
    visible — so this also pins the cost: one listing per org, paid once on a
    cache miss and never again.
    """
    print("shared room across orgs")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)

        def answer(name_, args):
            if name_ == "list_orgs":
                return _orgs("org-default", "org-other")
            return _Result({"agent_brains": [
                {"name": NAME, "agent_brain_id": BID}]})

        session = _Session(answer)
        room = run(br.resolve_repo_brain(session, "/repo", "staging"))
        check("resolved to the one brain", (room or {}).get("brain_id"), BID)
        check("recorded the org nearest the user", (room or {}).get("org_id"),
              "org-default")
        check("searched every org", session.calls.count("list_agent_brains"), 2)

        # ...and the cost is paid once.
        session2 = _Session(answer)
        run(br.resolve_repo_brain(session2, "/repo", "staging"))
        check("cache hit costs nothing", session2.calls, [])


def test_a_cross_org_duplicate_is_not_settled_by_org_order():
    """Two DIFFERENT brains sharing this repo's room name, one per org.

    Stopping at the first org with a hit would route to whichever org came
    first — and that order starts from the default org, which follows the last
    org selected in the MemHub app. The target would change when a user merely
    clicks around the UI, and two teammates would send the same repo's memory to
    different brains. So every org is searched before deciding, and a genuine
    ambiguity stays visible.
    """
    print("cross-org duplicates")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        other = "99999999-8888-7777-6666-555555555555"

        def answer(name_, args):
            if name_ == "list_orgs":
                return _orgs("org-default", "org-other")
            which = BID if args.get("org_id") == "org-default" else other
            return _Result({"agent_brains": [
                {"name": NAME, "agent_brain_id": which}]})

        session = _Session(answer)
        room = run(br.resolve_repo_brain(session, "/repo", "staging"))
        check("did not route", room, None)
        check("looked in BOTH orgs before deciding",
              session.calls.count("list_agent_brains"), 2)
        check("cached nothing", rm.read_room("/repo", "staging"), None)
        # Stays DUE, so merging the duplicates takes effect next flush.
        check("still due", rm.resolve_due("/repo", "staging"), True)


def test_an_incomplete_search_does_not_brand_a_repo_room_less():
    """If list_orgs is unavailable, only the default org was searched. Writing
    a miss then would send a repo whose room lives elsewhere to personal memory
    for the whole TTL, on the strength of a look that never happened."""
    print("incomplete search")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)

        def answer(name_, args):
            if name_ == "list_orgs":
                return _Result(None, is_error=True)
            return _Result({"agent_brains": []})  # default org has nothing

        room = run(br.resolve_repo_brain(_Session(answer), "/repo", "staging"))
        check("no room", room, None)
        check("nothing routed", rm.read_room("/repo", "staging"), None)
        # Backed off, not branded: a short pause rather than a day of personal
        # memory — and not a re-query on every single turn either, which is the
        # storm the backoff exists to prevent.
        check("backed off", rm.resolve_due("/repo", "staging"), False)

        data = json.loads(rm.ROOMS_PATH.read_text())
        entry = data["repos"][NAME]["staging"]
        check("no 24h miss was written", "missed_at" in entry, False)
        entry["probed_at"] = time.time() - rm.PROBE_BACKOFF_S - 1
        rm.ROOMS_PATH.write_text(json.dumps(data))
        check("due again in minutes, not a day",
              rm.resolve_due("/repo", "staging"), True)


def test_one_org_failing_to_list_does_not_settle_a_duplicate():
    """The nastiest shape. Two brains share the name across two orgs, and the
    org holding the second transiently fails to list. Exactly one match comes
    back — indistinguishable from an unambiguous hit — and committing it would
    cache an arbitrary pick for a day. A hole in the search has to beat a
    confident-looking result."""
    print("duplicate hidden by an error")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)

        def answer(name_, args):
            if name_ == "list_orgs":
                return _orgs("org-default", "org-broken")
            if args.get("org_id") == "org-broken":
                return _Result(None, is_error=True)   # the hidden duplicate
            return _Result({"agent_brains": [
                {"name": NAME, "agent_brain_id": BID}]})

        room = run(br.resolve_repo_brain(_Session(answer), "/repo", "staging"))
        check("did not commit the visible half", room, None)
        check("cached no brain_id", rm.read_room("/repo", "staging"), None)
        check("backed off rather than branding",
              rm.resolve_due("/repo", "staging"), False)


def test_a_complete_search_still_records_a_miss():
    """The negative cache must survive the change above, or a genuinely
    room-less repo re-queries on every single turn."""
    print("complete search records a miss")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)

        def answer(name_, args):
            if name_ == "list_orgs":
                return _orgs("org-default", "org-other")
            return _Result({"agent_brains": []})

        run(br.resolve_repo_brain(_Session(answer), "/repo", "staging"))
        check("miss recorded", rm.resolve_due("/repo", "staging"), False)


def test_a_room_cached_without_an_org_is_resolved_again():
    """Upgrade path. A cache written before rooms carried their org holds a
    brain_id, so nothing would ever re-ask — and every capture would keep
    resolving that brain in the wrong org, forever."""
    print("legacy cache upgrade")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)
        # Exactly what an older plugin wrote.
        rm.write_room(BID, name=NAME, env="staging")
        entry = rm._load()["repos"][NAME]["staging"]
        entry.pop("org_id", None)
        entry.pop("resolved_at", None)
        rm.ROOMS_PATH.write_text(
            json.dumps({"version": 1, "repos": {NAME: {"staging": entry}}}),
            encoding="utf-8")

        check("due again", rm.resolve_due("/repo", "staging"), True)

        def answer(name, args):
            if name == "list_orgs":
                return _orgs("org-default", "org-with-room")
            if args.get("org_id") == "org-with-room":
                return _Result({"agent_brains": [
                    {"name": NAME, "agent_brain_id": BID}]})
            return _Result({"agent_brains": []})

        run(br.resolve_repo_brain(_Session(answer), "/repo", "staging"))
        check("upgraded in place",
              (rm.read_room("/repo", "staging") or {}).get("org_id"),
              "org-with-room")
        check("and settles", rm.resolve_due("/repo", "staging"), False)


def test_an_unknowable_org_does_not_re_resolve_every_turn():
    """If the backend cannot report orgs, the entry stays org-less. It must
    still be rate-limited, or a silent failure is traded for a per-turn
    round trip on every single flush."""
    print("org-less rate limit")
    with tempfile.TemporaryDirectory() as tmp:
        _isolate(tmp)

        def answer(name, args):
            if name == "list_orgs":
                return _Result(None, is_error=True)
            return _Result({"agent_brains": [
                {"name": NAME, "agent_brain_id": BID}]})

        room = run(br.resolve_repo_brain(_Session(answer), "/repo", "staging"))
        check("still routes without an org", (room or {}).get("brain_id"), BID)
        check("no org recorded", (rm.read_room("/repo", "staging") or {})
              .get("org_id"), None)
        check("not due again immediately", rm.resolve_due("/repo", "staging"),
              False)

        # ...but due again after the TTL, so a fixed backend is picked up.
        data = rm._load()
        data["repos"][NAME]["staging"]["resolved_at"] = 0
        rm.ROOMS_PATH.write_text(json.dumps(data), encoding="utf-8")
        check("due again after the TTL", rm.resolve_due("/repo", "staging"), True)


if __name__ == "__main__":
    for fn in (test_resolves_and_caches, test_no_brain_is_remembered_as_a_miss,
               test_a_miss_never_clobbers_a_resolved_room,
               test_duplicate_room_names_are_not_guessed_between,
               test_never_routes_on_a_fuzzy_or_broken_match,
               test_failures_degrade_to_no_room, test_result_shapes_are_tolerated,
               test_finds_a_room_outside_the_default_org,
               test_a_room_visible_from_several_orgs_is_not_a_duplicate,
               test_a_cross_org_duplicate_is_not_settled_by_org_order,
               test_an_incomplete_search_does_not_brand_a_repo_room_less,
               test_one_org_failing_to_list_does_not_settle_a_duplicate,
               test_a_complete_search_still_records_a_miss,
               test_a_room_cached_without_an_org_is_resolved_again,
               test_an_unknowable_org_does_not_re_resolve_every_turn):
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        raise SystemExit(1)
    print("all passed")
