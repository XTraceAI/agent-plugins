"""Self-test for the access-key lifecycle.

The server calls are faked, so what is asserted is the part that can go quietly
wrong: which key gets used, what happens to a key whose secret we lost, and
whether the five-key cap is respected. Getting these wrong does not raise — it
burns the user's key allowance or silently authenticates with nothing.

Run: python3 pak_test.py  (stdlib only).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="pak-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["MEMHUB_PAK_LABEL"] = "test-machine"

# The tests live outside the plugin so they are not shipped to users;
# the code under test is still in the plugin's scripts dir.
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import pak  # noqa: E402

MCP = "https://api.staging.memhub.xtrace.ai/mcp-server/mcp"
BASE = "https://api.staging.memhub.xtrace.ai"

failures: list[str] = []
calls: list[tuple] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok ' if got == want else 'FAIL'} {label}")


def _iso(offset_days: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + offset_days * 86400))


def _fake_server(existing: list[dict], mint_id: str = "new-id"):
    """Install a fake transport and record what the module asks it to do."""
    calls.clear()
    state = {"keys": list(existing)}

    def _call(base, bearer, method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return state["keys"]
        if method == "DELETE":
            tid = path.rsplit("/", 1)[-1]
            for k in state["keys"]:
                if k.get("id") == tid:
                    k["revoked_at"] = _iso(0)
            return {}
        if method == "POST":
            return {"secret": "mhk_" + "x" * 43,
                    "access_token": {"id": mint_id, "label": body["label"],
                                     "scopes": body["scopes"],
                                     "expires_at": body["expires_at"],
                                     "created_at": _iso(0), "revoked_at": None}}
        raise AssertionError(method)

    pak._call = _call
    return state


def _reset():
    pak.forget(MCP)


def test_paths_and_labels():
    print("\npaths and labels")
    check("api base strips the mcp path", pak.api_base(MCP), BASE)
    check("key file is per host",
          pak.key_path(MCP).name, "pak-api.staging.memhub.xtrace.ai.json")
    check("prod and staging differ",
          pak.key_path(MCP) != pak.key_path("https://api.memhub.xtrace.ai/mcp-server/mcp"),
          True)
    check("label honours the override", pak.default_label(), "test-machine")


def test_credentials_never_go_over_cleartext():
    """The OAuth token sent to this API can MINT credentials.

    `mcp_url` can come from $MEMHUB_MCP_BASE_URL, so a misconfigured or planted
    `http://` value would otherwise put that token on the wire in cleartext,
    silently, while everything appeared to work.
    """
    print("\ntls is required")
    check("https is fine", pak.api_base(MCP), BASE)
    for label, url in [
        ("plain http", "http://api.staging.memhub.xtrace.ai/mcp-server/mcp"),
        ("http on a lookalike host", "http://evil.example.com/mcp-server/mcp"),
    ]:
        try:
            pak.api_base(url)
            check(f"rejects {label}", False, True)
        except pak.PakError as exc:
            check(f"rejects {label}", True, True)
            check(f"{label} names the env var", "MEMHUB_MCP_BASE_URL" in str(exc),
                  True)

    # Loopback never leaves the machine, and refusing it would make a local
    # backend impossible to develop against.
    for label, url in [("localhost", "http://localhost:8080/mcp-server/mcp"),
                       ("127.0.0.1", "http://127.0.0.1:8080/mcp-server/mcp")]:
        try:
            pak.api_base(url)
            check(f"allows {label}", True, True)
        except pak.PakError:
            check(f"allows {label}", False, True)


def test_store_roundtrip():
    print("\nstorage")
    _reset()
    check("missing reads as None", pak.load(MCP), None)
    pak.save(MCP, {"secret": "mhk_abc", "label": "test-machine"})
    check("round-trips", pak.load(MCP)["secret"], "mhk_abc")
    check("mode is 0600", oct(pak.key_path(MCP).stat().st_mode)[-3:], "600")

    # A record with no secret is not a credential, however well-formed.
    pak.save(MCP, {"label": "test-machine"})
    check("secretless record reads as None", pak.load(MCP), None)

    pak.key_path(MCP).write_text("{not json", encoding="utf-8")
    check("corrupt file reads as None", pak.load(MCP), None)
    _reset()


def test_expiry():
    print("\nexpiry")
    check("absent -> unknown", pak.expires_in_s({"secret": "x"}), None)
    check("null -> unknown", pak.expires_in_s({"expires_at": None}), None)
    check("garbage -> unknown", pak.expires_in_s({"expires_at": "soon"}), None)
    future = pak.expires_in_s({"expires_at": _iso(10)})
    check("future is positive", future is not None and future > 0, True)
    past = pak.expires_in_s({"expires_at": _iso(-10)})
    check("past is negative", past is not None and past < 0, True)


def test_expiry_is_utc_regardless_of_local_zone():
    """Expiry must not drift with the machine's timezone or DST.

    The original computed `mktime(struct) - time.timezone`, which reads a UTC
    struct as LOCAL time and then corrects with the zone's NON-DST offset —
    measured at exactly -3600s for a summer expiry under America/Los_Angeles
    and 0 for a winter one. Correct half the year is the worst shape a bug can
    have, so this pins both halves in a DST zone.
    """
    print("\nexpiry is UTC")
    import calendar
    import importlib
    import os as _os

    previous = _os.environ.get("TZ")
    try:
        _os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        importlib.reload(pak)
        for label, raw in [("summer (DST)", "2026-07-15T12:00:00Z"),
                           ("winter (no DST)", "2026-12-15T12:00:00Z")]:
            true_epoch = calendar.timegm(
                time.strptime(raw.replace("Z", ""), "%Y-%m-%dT%H:%M:%S"))
            got = pak.expires_in_s({"expires_at": raw}) + time.time()
            check(f"{label} has no skew", round(got - true_epoch), 0)
    finally:
        if previous is None:
            _os.environ.pop("TZ", None)
        else:
            _os.environ["TZ"] = previous
        time.tzset()
        importlib.reload(pak)


def test_every_timestamp_shape_the_server_might_send():
    """The spelling of the timestamp is the server's choice, not ours.

    A rejected stamp is not loud — it reads as "no known expiry", i.e. a key
    that never appears to lapse — so the parser has to accept what it is
    actually handed. Fractional seconds are not hypothetical: this very API
    returns `created_at` as `…T22:11:58.575503Z`.
    """
    print("\ntimestamp shapes")
    import calendar

    want = calendar.timegm(time.strptime("2026-11-05T00:00:00",
                                         "%Y-%m-%dT%H:%M:%S"))
    for label, raw in [
        ("zulu", "2026-11-05T00:00:00Z"),
        ("lowercase z", "2026-11-05T00:00:00z"),
        ("offset with colon", "2026-11-05T00:00:00+00:00"),
        ("offset without colon", "2026-11-05T00:00:00+0000"),
        ("naive (read as UTC)", "2026-11-05T00:00:00"),
        ("surrounding whitespace", "  2026-11-05T00:00:00Z  "),
    ]:
        check(label, pak.parse_utc(raw), float(want))

    # A non-UTC offset must be honoured, not ignored.
    check("non-zero offset is applied",
          pak.parse_utc("2026-11-05T05:30:00+05:30"), float(want))

    # Fractional seconds — the shape this API actually emits on created_at.
    frac = pak.parse_utc("2026-11-05T00:00:00.575503Z")
    check("fractional seconds parse", frac is not None and abs(frac - want) < 1,
          True)

    # A date with no time is accepted as midnight UTC. Being permissive fails
    # SAFE here: an unparseable stamp means "no known expiry", which every
    # caller reads as healthy forever, so guessing midnight beats refusing.
    check("date only -> midnight UTC", pak.parse_utc("2026-11-05"), float(want))

    for label, raw in [("empty", ""), ("prose", "next tuesday"),
                       ("nonsense", "T::Z")]:
        check(f"unparseable: {label}", pak.parse_utc(raw), None)


def test_cap_is_checked_before_anything_is_revoked():
    """A machine must never be left with no key and no way to mint one.

    Revoking this machine's orphan and only then discovering the cap is full
    destroys the one credential it had. Refusing first leaves that key working,
    which is strictly the better failure.
    """
    print("\ncap is checked first")
    _reset()
    keys = [{"id": f"k{i}", "label": f"other-{i}", "revoked_at": None}
            for i in range(pak.MAX_KEYS)]
    keys.append({"id": "mine", "label": "test-machine", "revoked_at": None})
    _fake_server(keys)
    try:
        pak.ensure(MCP, "bearer")
        check("refuses", False, True)
    except pak.PakError:
        check("refuses", True, True)
    check("did not revoke this machine's key",
          [c for c in calls if c[0] == "DELETE"], [])
    check("did not mint", [c for c in calls if c[0] == "POST"], [])


def test_a_failed_orphan_revoke_does_not_block_minting():
    """Revoking an orphan is housekeeping, not the goal.

    One stale id that 404s on delete must not block every future login on this
    machine — that turns tidying-up into a permanent capture outage.
    """
    print("\nfailed orphan revoke")
    _reset()
    _fake_server([{"id": "ghost", "label": "test-machine", "revoked_at": None}])
    real = pak._call

    def _revoke_explodes(base, bearer, method, path, body=None):
        if method == "DELETE":
            calls.append((method, path, body))
            raise pak.PakError("DELETE failed (404): gone")
        return real(base, bearer, method, path, body)

    pak._call = _revoke_explodes
    try:
        record, how = pak.ensure(MCP, "bearer")
        check("still minted", how, "replaced")
        check("key is stored", bool(pak.load(MCP)), True)
    except pak.PakError as exc:
        check(f"still minted (raised {exc})", False, True)
    finally:
        pak._call = real


def test_reuses_a_good_stored_key():
    print("\nreuse")
    _reset()
    pak.save(MCP, {"secret": "mhk_stored", "label": "test-machine",
                   "id": "old", "expires_at": _iso(30)})
    _fake_server([])
    record, how = pak.ensure(MCP, "bearer")
    check("reused", how, "reused")
    check("same secret", record["secret"], "mhk_stored")
    # The whole point of reuse: no allowance spent, no server round-trip.
    check("no server calls at all", calls, [])


def test_expired_stored_key_is_replaced():
    print("\nexpired stored key")
    _reset()
    pak.save(MCP, {"secret": "mhk_old", "label": "test-machine",
                   "id": "old", "expires_at": _iso(-1)})
    _fake_server([{"id": "old", "label": "test-machine", "revoked_at": None}])
    record, how = pak.ensure(MCP, "bearer")
    check("replaced", how, "replaced")
    check("new secret stored", pak.load(MCP)["secret"], record["secret"])
    check("the stale one was revoked",
          ("DELETE", "/v1/developer/access-tokens/old", None) in calls, True)


def test_orphan_is_revoked_before_minting():
    print("\norphaned key")
    _reset()
    # Server has a key under our label, but we hold no secret for it — a lost
    # cache. It can never be used again, so it must not keep occupying a slot.
    _fake_server([{"id": "ghost", "label": "test-machine", "revoked_at": None}])
    record, how = pak.ensure(MCP, "bearer")
    check("replaced", how, "replaced")
    check("ghost revoked",
          ("DELETE", "/v1/developer/access-tokens/ghost", None) in calls, True)
    check("a key is stored afterwards", bool(pak.load(MCP)), True)


def test_mints_when_nothing_exists():
    print("\nfirst mint")
    _reset()
    _fake_server([])
    record, how = pak.ensure(MCP, "bearer")
    check("minted", how, "minted")
    check("nothing was revoked",
          [c for c in calls if c[0] == "DELETE"], [])
    posted = [c for c in calls if c[0] == "POST"][0][2]
    check("asks for read and write",
          sorted(posted["scopes"]), ["memory:read", "memory:write"])
    check("sets an expiry", bool(posted.get("expires_at")), True)
    check("secret persisted", pak.load(MCP)["secret"], record["secret"])


def test_cap_is_reported_not_hit():
    print("\nfive-key cap")
    _reset()
    others = [{"id": f"k{i}", "label": f"other-{i}", "revoked_at": None}
              for i in range(pak.MAX_KEYS)]
    _fake_server(others)
    try:
        pak.ensure(MCP, "bearer")
        check("raises before minting", False, True)
    except pak.PakError as exc:
        check("raises before minting", True, True)
        check("names the limit", str(pak.MAX_KEYS) in str(exc), True)
        check("lists what is in the way", "other-0" in str(exc), True)
    check("did not mint anyway", [c for c in calls if c[0] == "POST"], [])

    # Revoked keys are not live and must not count toward the cap.
    _reset()
    dead = [{"id": f"k{i}", "label": f"other-{i}", "revoked_at": _iso(-1)}
            for i in range(pak.MAX_KEYS)]
    _fake_server(dead)
    _, how = pak.ensure(MCP, "bearer")
    check("revoked keys do not block", how, "minted")


def test_envelope_errors_surface():
    print("\nerror envelope")
    _reset()

    def _boom(base, bearer, method, path, body=None):
        raise pak.PakError("POST failed (403): forbidden")

    real = pak._call
    pak._call = _boom
    try:
        pak.ensure(MCP, "bearer")
        check("propagates as PakError", False, True)
    except pak.PakError as exc:
        check("propagates as PakError", "403" in str(exc), True)
    finally:
        pak._call = real


if __name__ == "__main__":
    real_call = pak._call
    for test in (test_paths_and_labels, test_credentials_never_go_over_cleartext,
                 test_store_roundtrip, test_expiry,
                 test_expiry_is_utc_regardless_of_local_zone,
                 test_every_timestamp_shape_the_server_might_send,
                 test_cap_is_checked_before_anything_is_revoked,
                 test_a_failed_orphan_revoke_does_not_block_minting,
                 test_reuses_a_good_stored_key, test_expired_stored_key_is_replaced,
                 test_orphan_is_revoked_before_minting, test_mints_when_nothing_exists,
                 test_cap_is_reported_not_hit, test_envelope_errors_surface):
        test()
    pak._call = real_call
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall pak checks passed")
