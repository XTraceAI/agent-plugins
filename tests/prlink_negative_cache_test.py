#!/usr/bin/env python3
"""The PR-link negative cache: what is worth storing, for how long, and for whom.

This suite exists because of one incident. On 2026-09-08 a machine cached
``{"enabled": false, "github_connected": false}`` for a repo whose org had
GitHub connected the entire time — the ``SESSION_PR_LINKING`` flag was simply
still off, and the server returns its disabled answer from a feature-flag gate
*before it runs a single query*, with ``github_connected`` left at its dataclass
default. The plugin filed that default as a durable "this org disconnected
GitHub" fact and latched it for 24h. When the flag was turned on, the hook stayed
silent for the rest of the day: no request, no error, and no breadcrumb (those
are written only on exceptions).

So three properties are pinned here, and each one alone would have prevented it:

1. an ``enabled:false`` reply is never cached, and never served even from an
   entry already on disk (spec §4.4);
2. the window is 30 minutes, not a day, and is overridable per machine;
3. the key carries the IDENTITY, because which org answers is decided by the
   bearer — not by the deployment and repo alone.

Nothing here reaches a network: ``check()`` runs against a stubbed
``mcp_http.rest``, and ``$HOME`` is redirected before the import so every write
lands in a tmpdir.

Run: python3 tests/prlink_negative_cache_test.py   (stdlib only)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "memhub" / "scripts"

_HOME = tempfile.mkdtemp(prefix="memhub-prlink-ttl-home-")
os.environ["HOME"] = _HOME
os.environ["USERPROFILE"] = _HOME
# A stray credential would let check() try a real request; an ambient TTL
# override would silently steer every boundary case below.
os.environ.pop("MEMHUB_TOKEN", None)
os.environ.pop("MEMHUB_PRLINK_NEGATIVE_TTL_S", None)

sys.path.insert(0, str(SCRIPTS))
import mcp_http  # noqa: E402
import pr_link  # noqa: E402

failures: list[str] = []

API = "https://api.example.test"
DISCONNECTED = {"enabled": True, "github_connected": False,
                "connect_url": "https://app.example.test/i"}
DISABLED = {"enabled": False, "github_connected": False}
CONNECTED = {"enabled": True, "github_connected": True, "repo_in_install": True}


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok ' if condition else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


class _Reply:
    def __init__(self, status: int, data: object) -> None:
        self.status, self.data, self.etag = status, data, None


def _with_stub(rest, fn, bearer: str = "mhk_test"):
    """Run `fn` with a stubbed transport and a chosen credential."""
    import _memhub_auth

    real_rest, real_resolve = mcp_http.rest, _memhub_auth.resolve_bearer
    mcp_http.rest = rest
    _memhub_auth.resolve_bearer = lambda url=None, refresh=True: (
        "https://api.example.test/mcp/", bearer)
    try:
        return fn()
    finally:
        mcp_http.rest = real_rest
        _memhub_auth.resolve_bearer = real_resolve


def _tmp_state():
    """A fresh STATE_DIR, restored afterwards. Used as a context manager."""
    class _Ctx:
        def __enter__(self):
            self.td = tempfile.TemporaryDirectory()
            self.real = pr_link.STATE_DIR
            pr_link.STATE_DIR = Path(self.td.name) / "prlink"
            return pr_link.STATE_DIR

        def __exit__(self, *exc):
            pr_link.STATE_DIR = self.real
            self.td.cleanup()
            return False
    return _Ctx()


# --------------------------------------------------------------- the window

def test_the_window_is_thirty_minutes():
    check("NEGATIVE_TTL_S is 30 minutes", pr_link.NEGATIVE_TTL_S == 30 * 60,
          str(pr_link.NEGATIVE_TTL_S))
    with _tmp_state():
        now = time.time()
        pr_link._store_negative(API, DISCONNECTED, now)
        check("a fresh entry is served",
              pr_link._cached_negative(API, now) == DISCONNECTED)
        check("…at 29 minutes it is still served",
              pr_link._cached_negative(API, now + 29 * 60) == DISCONNECTED)
        check("…at 31 minutes it is not",
              pr_link._cached_negative(API, now + 31 * 60) is None)
        # The bug this replaces: 23.6h old was 'fresh' against a 24h TTL.
        check("…and the old 24h window is long gone",
              pr_link._cached_negative(API, now + 23.6 * 3600) is None)
        check("a clock that jumped a week BACK does not pin it forever",
              pr_link._cached_negative(API, now - 7 * 86400) is None)


def test_the_window_is_overridable_per_machine():
    with _tmp_state():
        now = time.time()
        pr_link._store_negative(API, DISCONNECTED, now)
        cases = [
            ("a wider window is honoured", "7200", now + 3600, True),
            ("a narrower one is too", "60", now + 120, False),
            ("0 disables serving from the cache", "0", now, False),
            ("junk falls back to the default", "half an hour", now + 60, True),
            ("…so does a negative", "-1", now + 60, True),
            ("…so does NaN", "nan", now + 60, True),
            ("an empty value falls back", "", now + 60, True),
            # These two must be probed PAST the default window: at now+60 a
            # served answer proves nothing, because the default serves there
            # too — the assertion would hold with the upper guard deleted.
            ("an infinity falls back, not to forever", "inf", now + 86400, False),
            ("…so does an absurd value", "1e12", now + 86400, False),
            ("…and both still serve inside the default", "inf", now + 60, True),
            ("whitespace is tolerated", "  7200  ", now + 3600, True),
        ]
        for label, raw, at, served in cases:
            os.environ["MEMHUB_PRLINK_NEGATIVE_TTL_S"] = raw
            try:
                got = pr_link._cached_negative(API, at) is not None
            finally:
                os.environ.pop("MEMHUB_PRLINK_NEGATIVE_TTL_S", None)
            check(f"{label} ({raw!r})", got is served, f"served={got}")
        check("the env name is exported for callers",
              pr_link.NEGATIVE_TTL_ENV == "MEMHUB_PRLINK_NEGATIVE_TTL_S")


# ------------------------------------------------ what is worth storing

def test_a_disabled_reply_is_never_cached():
    """The incident, exactly: `enabled:false` carries no finding at all."""
    calls: list[str] = []

    def rest(url, *a, **k):
        calls.append(url)
        return _Reply(200, DISABLED)

    with _tmp_state() as state:
        first = _with_stub(rest, lambda: pr_link.check(
            "https://github.com/o/r/pull/1"))
        check("the disabled reply still reaches the caller", first == DISABLED)
        check("…but nothing is written to the cache",
              not state.exists() or not list(state.glob("*.json")),
              str(list(state.glob("*.json")) if state.exists() else []))
        _with_stub(rest, lambda: pr_link.check("https://github.com/o/r/pull/2"))
        check("…so the next call asks the server again", len(calls) == 2,
              str(len(calls)))


def test_a_disabled_entry_already_on_disk_is_not_served():
    """A planted `enabled:false` entry never silences anything again.

    Planted at the key `check()` genuinely reads — same repo, same credential.
    Getting that wrong makes the whole second half vacuous: `check()` would go
    to the network because it found NO file, not because it refused this one.
    """
    with _tmp_state() as state:
        now = time.time()
        state.mkdir(parents=True)
        scope = "github.com/o/r"
        ident = hashlib.sha256(b"mhk_test").hexdigest()[:16]
        # The exact payload from the 2026-09-08 incident, brand new.
        pr_link._store_negative(API, DISABLED, now, scope, ident)
        planted = pr_link._cache_path(API, scope, ident)
        check("the entry is on disk", planted.exists(), str(planted))
        check("…and is refused however fresh it is",
              pr_link._cached_negative(API, now, scope, ident) is None)

        calls: list[str] = []

        def rest(url, *a, **k):
            calls.append(url)
            return _Reply(200, CONNECTED)

        # check() must reach the server despite that entry sitting at the very
        # key it looks up — this is what PR #760 needed and did not have.
        got = _with_stub(rest, lambda: pr_link.check(
            "https://github.com/o/r/pull/760"))
        check("check() reads that key, finds it unusable, and asks the server",
              len(calls) == 1, str(calls))
        check("…and returns the live, connected answer", got == CONNECTED)
        check("…and the connected reply is not written over it",
              json.loads(planted.read_text(encoding="utf-8"))["answer"] == DISABLED)
        check("…leaving exactly the one planted file",
              len(list(state.glob("*.json"))) == 1,
              str([q.name for q in state.glob("*.json")]))


def test_only_a_connected_orgs_disconnect_is_cached():
    with _tmp_state():
        now = time.time()
        check("enabled:true + github_connected:false is a finding",
              pr_link._cacheable(DISCONNECTED))
        check("enabled:false is not, whatever else it says",
              not pr_link._cacheable(DISABLED))
        check("…not even when it claims a connection",
              not pr_link._cacheable({"enabled": False, "github_connected": True}))
        check("a connected reply is not cacheable", not pr_link._cacheable(CONNECTED))
        check("a reply that omits `enabled` is still judged on the connection",
              pr_link._cacheable({"github_connected": False}))
        for junk in (None, [], "x", 3, {"github_connected": "false"},
                     # `0 == False` is True, so an `==` comparison would call
                     # this cacheable. The `is False` identity check stops it.
                     {"github_connected": 0}):
            check(f"{junk!r} is not cacheable", not pr_link._cacheable(junk))

        # And the stored shape is still validated on read.
        pr_link._store_negative(API, DISCONNECTED, now)
        path = pr_link._cache_path(API)
        for label, body in (("corrupt json", "{ not json"), ("a list", "[1,2]"),
                            ("no 'at'", '{"answer": {"github_connected": false}}'),
                            ("'at' is a string", '{"at": "yesterday", "answer": {}}'),
                            ("answer is not a dict", '{"at": 1, "answer": "x"}'),
                            ("empty", "")):
            path.write_text(body, encoding="utf-8")
            check(f"{label} is ignored, not fatal",
                  pr_link._cached_negative(API, time.time()) is None)


def test_what_we_cache_never_diverges_from_what_we_would_say():
    """Everything `_cacheable` accepts is something `context_for` would advise on.

    `context_for` goes silent on exactly `enabled is False`, so `_cacheable`
    tests that same identity rather than truthiness. The two must decide the
    same reply the same way: a reply we cache but would not advise on is a
    silent hole, and one we advise on but never cache pays a round trip forever.
    This is also why `{"enabled": 0}` is cacheable — `context_for` advises on it
    too. Real JSON never produces it; what matters is that neither side guesses.
    """
    replies = [
        {"enabled": True, "github_connected": False, "connect_url": "u"},
        {"enabled": False, "github_connected": False},
        {"github_connected": False, "connect_url": "u"},
        {"enabled": 0, "github_connected": False, "connect_url": "u"},
        {"enabled": None, "github_connected": False, "connect_url": "u"},
        {"enabled": True, "github_connected": True, "repo_in_install": True},
    ]
    for reply in replies:
        advises = pr_link.context_for(
            reply, "https://github.com/o/r/pull/1", "sess", created=False) is not None
        cacheable = pr_link._cacheable(reply)
        check(f"{reply} — silenced iff enabled is False",
              advises is not (reply.get("enabled") is False),
              f"advises={advises}")
        if cacheable:
            check(f"{reply} — cacheable, and we would advise on it", advises)
        check(f"{reply} — never cached while silenced",
              not (cacheable and reply.get("enabled") is False))


# ------------------------------------------------------------- the identity

def test_the_key_is_scoped_to_the_credential():
    with _tmp_state():
        now = time.time()
        pr_link._store_negative(API, DISCONNECTED, now, "github.com/o/r", "aaaa")
        check("the identity that stored it reads it back",
              pr_link._cached_negative(API, now, "github.com/o/r", "aaaa")
              == DISCONNECTED)
        check("a DIFFERENT credential does not",
              pr_link._cached_negative(API, now, "github.com/o/r", "bbbb") is None)
        check("neither does the same credential on another repo",
              pr_link._cached_negative(API, now, "github.com/o/other", "aaaa") is None)
        check("nor the same credential on another deployment",
              pr_link._cached_negative("https://api.other.test", now,
                                       "github.com/o/r", "aaaa") is None)
        check("three distinct inputs give three distinct files",
              len({pr_link._cache_path(API, "r", "a"),
                   pr_link._cache_path(API, "r", "b"),
                   pr_link._cache_path(API, "s", "a")}) == 3)


def test_a_token_swap_re_asks_the_server():
    """End-to-end: the same repo, two credentials, two requests."""
    calls: list[str] = []

    def rest(url, *a, **k):
        calls.append(url)
        return _Reply(200, DISCONNECTED)

    with _tmp_state() as state:
        _with_stub(rest, lambda: pr_link.check("https://github.com/o/r/pull/1"),
                   bearer="mhk_first")
        check("the first credential's answer is cached",
              len(list(state.glob("*.json"))) == 1)
        _with_stub(rest, lambda: pr_link.check("https://github.com/o/r/pull/2"),
                   bearer="mhk_first")
        check("…and it is reused for that credential", len(calls) == 1, str(calls))
        _with_stub(rest, lambda: pr_link.check("https://github.com/o/r/pull/3"),
                   bearer="mhk_second")
        check("a re-login asks again rather than inheriting the silence",
              len(calls) == 2, str(calls))
        check("…and files its own entry beside it",
              len(list(state.glob("*.json"))) == 2)


def test_a_malformed_credential_never_reaches_the_breadcrumb():
    """A token with a non-UTF-8 byte must not be hashed by a raising encode.

    `os.environ` decodes with surrogateescape, so one stray byte gives a str
    holding a lone surrogate. A bare `.encode("utf-8")` raises
    UnicodeEncodeError — whose args carry THE WHOLE TOKEN — and `breadcrumb`
    would persist that repr forever in an append-only file.
    """
    secret = "mhk_live_" + "S" * 24
    mangled = (secret.encode() + b"\xff").decode("utf-8", "surrogateescape")

    def rest(url, *a, **k):
        return _Reply(200, DISCONNECTED)

    with _tmp_state() as state:
        got = _with_stub(rest, lambda: pr_link.check(
            "https://github.com/o/r/pull/1"), bearer=mangled)
        check("the check still completes", got == DISCONNECTED, repr(got))
        crumb = state / "breadcrumb"
        text = crumb.read_text(encoding="utf-8") if crumb.exists() else ""
        check("no breadcrumb was written at all", text == "", text[:200])
        check("…and the token is nowhere in the state dir",
              all(secret not in q.read_text(encoding="utf-8", errors="replace")
                  and secret not in q.name for q in state.iterdir()))


def test_the_breadcrumb_is_redacted():
    """It is the one file that persists exception text, so it gets screened."""
    secret = "mhk_" + "b" * 32
    with _tmp_state() as state:
        pr_link.breadcrumb("probe", RuntimeError(f"auth failed for {secret}"))
        text = (state / "breadcrumb").read_text(encoding="utf-8")
        check("the credential is redacted out", secret not in text, text[:200])
        check("…and the line is still useful", "probe" in text and "mhk_" in text,
              text[:200])

        class Hostile:
            def __repr__(self):
                raise ValueError("no repr for you")

        pr_link.breadcrumb("hostile", Hostile())
        check("an exception whose repr raises is still not fatal",
              "hostile" in (state / "breadcrumb").read_text(encoding="utf-8"))


def test_dead_entries_are_pruned():
    """The key carries the credential, and OAuth tokens rotate — so without a
    prune the directory gains a permanently dead file per rotation, forever."""
    with _tmp_state() as state:
        now = time.time()
        state.mkdir(parents=True)
        stale = []
        for ident in ("old1", "old2"):
            pr_link._store_negative(API, DISCONNECTED, now, "r", ident)
            path = pr_link._cache_path(API, "r", ident)
            os.utime(path, (now - 5 * 3600, now - 5 * 3600))
            stale.append(path)
        fresh = pr_link._cache_path(API, "r", "new")
        pr_link._store_negative(API, DISCONNECTED, now, "r", "new")
        check("the stale entries are gone", not any(q.exists() for q in stale),
              str([q.name for q in stale if q.exists()]))
        check("…and the fresh one is untouched", fresh.exists())
        # An entry inside twice the window is still servable, so it must stay.
        keep = pr_link._cache_path(API, "r", "recent")
        pr_link._store_negative(API, DISCONNECTED, now, "r", "recent")
        os.utime(keep, (now - 40 * 60, now - 40 * 60))
        pr_link._store_negative(API, DISCONNECTED, now, "r", "trigger")
        check("an entry inside 2x the window survives", keep.exists())


def test_a_zero_ttl_writes_nothing_either():
    """`0` disables the cache, not just reads — otherwise a pinned machine
    accumulates entries it will never once serve."""
    calls: list[str] = []

    def rest(url, *a, **k):
        calls.append(url)
        return _Reply(200, DISCONNECTED)

    with _tmp_state() as state:
        os.environ["MEMHUB_PRLINK_NEGATIVE_TTL_S"] = "0"
        try:
            _with_stub(rest, lambda: pr_link.check("https://github.com/o/r/pull/1"))
            _with_stub(rest, lambda: pr_link.check("https://github.com/o/r/pull/2"))
        finally:
            os.environ.pop("MEMHUB_PRLINK_NEGATIVE_TTL_S", None)
        check("nothing was written",
              not state.exists() or not list(state.glob("*.json")),
              str(list(state.glob("*.json")) if state.exists() else []))
        check("…and every call went to the server", len(calls) == 2, str(calls))


def test_the_credential_never_lands_on_disk():
    secret = "mhk_super_secret_value_do_not_store"

    def rest(url, *a, **k):
        return _Reply(200, DISCONNECTED)

    with _tmp_state() as state:
        _with_stub(rest, lambda: pr_link.check("https://github.com/o/r/pull/1"),
                   bearer=secret)
        blobs = list(state.glob("*.json"))
        check("an entry was written", len(blobs) == 1)
        for path in blobs:
            check("the credential is not in the file name",
                  secret not in path.name, path.name)
            check("…nor in its contents", secret not in
                  path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    print("prlink_negative_cache")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
    else:
        print("all prlink_negative_cache checks passed")
    sys.exit(1 if failures else 0)
