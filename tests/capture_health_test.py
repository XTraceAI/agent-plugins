"""Self-test for the capture health check.

Covers the properties that decide whether this is worth having at all:

* the healthy path is SILENT — a check that speaks every session gets ignored,
  and then says nothing useful on the day it matters;
* an expired token WITH a refresh token is healthy (that is the normal
  overnight path, renewed automatically before the SDK runs), while an expired
  token WITHOUT one is the terminal case that went unnoticed for a day;
* a recorded failure is reported, but a stale one and a retracted one are not;
* the warning reaches ``systemMessage``, which is the only hook field Claude
  Code shows to the USER — emitting it anywhere else is the original bug;
* nothing here ever raises, whatever the state dir contains.

Run: python3 capture_health_test.py  (stdlib only).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

# Redirect HOME before importing, so the module's import-time CACHE_DIR /
# STATE_DIR resolve inside the sandbox and no test can read or write the real
# ~/.config/memhub-plugin. Exported so the CLI subprocess below inherits it.
# Both spellings: POSIX expanduser reads HOME; Windows reads USERPROFILE and
# never consults HOME — without it these tests would RESET the real state dir.
_TMP_HOME = tempfile.mkdtemp(prefix="capture-health-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
os.environ.pop("MEMHUB_TOKEN", None)
os.environ.pop("MEMHUB_TURN_FLUSH", None)
os.environ.pop("MEMHUB_MCP_BASE_URL", None)

# The tests live outside the plugin so they are not shipped to users;
# the code under test is still in the plugin's scripts dir.
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import capture_health as ch  # noqa: E402

HOST = "api.memhub.xtrace.ai"

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok ' if got == want else 'FAIL'} {label}")


def _jwt(exp: float) -> str:
    """A token that is a real JWT only as far as the ``exp`` claim — which is
    all this module reads (it never verifies a signature; the server does)."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _write_token(*, exp: float, refresh: bool) -> None:
    ch.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (ch.CACHE_DIR / f"tokens-{HOST}.json").write_text(json.dumps({
        "access_token": _jwt(exp),
        "refresh_token": "rt-value" if refresh else None,
    }), encoding="utf-8")


def _iso(days: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                         time.gmtime(time.time() + days * 86400))


def _write_state(name: str, **fields) -> None:
    ch.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (ch.STATE_DIR / f"{name}.json").write_text(json.dumps(fields),
                                               encoding="utf-8")


def _reset() -> None:
    for d in (ch.CACHE_DIR, ch.STATE_DIR):
        if d.exists():
            for p in d.iterdir():
                if p.is_file():
                    p.unlink()


def test_token_states() -> None:
    print("\ntoken states")
    _reset()
    check("no cache file -> never", ch._token_problem(HOST), "never")

    _write_token(exp=time.time() + 3600, refresh=True)
    check("live token -> healthy", ch._token_problem(HOST), None)

    # The normal overnight case. Renewed by _refresh_cached_token_if_stale
    # before the SDK runs, so warning here would be crying wolf every morning.
    _write_token(exp=time.time() - 3600, refresh=True)
    check("expired but renewable -> healthy", ch._token_problem(HOST), None)

    # The real outage: no refresh token, so nothing automatic can recover.
    _write_token(exp=time.time() - 3600, refresh=False)
    check("expired unrenewable -> problem",
          ch._token_problem(HOST), "unrenewable")

    # Works today, no way to renew: the state that killed production. Reported
    # once the outage is CLOSE — `login.py` already names the tenant fix at mint
    # time, so repeating it every session for the token's whole life would just
    # be that message as wallpaper.
    _write_token(exp=time.time() + 3600, refresh=False)
    check("unrenewable and expiring soon -> no_refresh",
          ch._token_problem(HOST), "no_refresh")

    _write_token(exp=time.time() + ch._NO_REFRESH_WARN_WITHIN_S + 3600,
                 refresh=False)
    check("unrenewable but not due for hours -> silent",
          ch._token_problem(HOST), None)

    # An unreadable exp is never a fault on its own — that would be guessing —
    # but it must not suppress the refresh-token fact, which is known either
    # way. Short-circuiting on expiry first threw that away.
    ch.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (ch.CACHE_DIR / f"tokens-{HOST}.json").write_text(
        json.dumps({"access_token": "opaque", "refresh_token": None}),
        encoding="utf-8")
    check("undecodable exp + no refresh -> no_refresh",
          ch._token_problem(HOST), "no_refresh")

    (ch.CACHE_DIR / f"tokens-{HOST}.json").write_text(
        json.dumps({"access_token": "opaque", "refresh_token": "rt"}),
        encoding="utf-8")
    check("undecodable exp + refresh -> healthy",
          ch._token_problem(HOST), None)

    _write_token(exp=time.time() - 3600, refresh=False)
    os.environ["MEMHUB_TOKEN"] = "explicit-bearer"
    check("explicit bearer overrides cache", ch._token_problem(HOST), None)
    os.environ.pop("MEMHUB_TOKEN")


def test_stored_key_outranks_the_oauth_cache() -> None:
    """A stored access key is the credential; the OAuth cache stops mattering.

    Judging the OAuth token while the hooks authenticate with a key would report
    on something nothing uses — and would fire "cannot renew" at a setup that is
    now immune to that entire failure mode.
    """
    print("\nstored access key")
    import pak

    _reset()
    # The worst case for the old logic: an OAuth token that is expired AND
    # unrenewable sitting next to a perfectly good key.
    _write_token(exp=time.time() - 3600, refresh=False)
    pak.save(ch._mcp_url_for(HOST),
             {"secret": "mhk_x", "label": "test",
              "expires_at": _iso(days=30)})
    check("good key wins over a dead OAuth token",
          ch._token_problem(HOST), None)

    # An expired key is NOT terminal on its own: resolve_url_and_auth falls
    # through to the OAuth cache, so capture may well still be running. The
    # health check has to model that fallback or it announces an outage to
    # someone whose capture is fine.
    pak.save(ch._mcp_url_for(HOST),
             {"secret": "mhk_x", "label": "test", "expires_at": _iso(days=-1)})
    _write_token(exp=time.time() + 3600, refresh=True)
    check("expired key + working OAuth -> silent",
          ch._token_problem(HOST), None)

    _write_token(exp=time.time() - 3600, refresh=False)
    check("expired key + dead OAuth -> reported",
          ch._token_problem(HOST), "unrenewable")

    # Nothing to fall back to: name the key, because someone whose key lapsed
    # has plainly authenticated before and "never authenticated" is the wrong
    # problem even though the remedy coincides.
    (ch.CACHE_DIR / f"tokens-{HOST}.json").unlink()
    check("expired key + no OAuth at all -> key_expired",
          ch._token_problem(HOST), "key_expired")
    _write_token(exp=time.time() - 3600, refresh=False)

    pak.save(ch._mcp_url_for(HOST),
             {"secret": "mhk_x", "label": "test", "expires_at": _iso(days=2)})
    check("key expiring soon is reported",
          ch._token_problem(HOST), "key_expiring")

    pak.save(ch._mcp_url_for(HOST), {"secret": "mhk_x", "label": "test"})
    check("key with no expiry is healthy", ch._token_problem(HOST), None)

    # Both key messages name /memhub:login, because unlike the OAuth
    # "cannot renew" case, re-running it genuinely does mint a replacement.
    for state in ("key_expired", "key_expiring"):
        msg = ch._message(HOST, state, None)
        check(f"{state} names the fix",
              bool(msg and "/memhub:login" in msg), True)

    pak.forget(ch._mcp_url_for(HOST))
    check("no key -> falls back to the OAuth cache",
          ch._token_problem(HOST), "unrenewable")
    _reset()


def test_breadcrumbs() -> None:
    print("\nbreadcrumbs")
    _reset()
    check("no state -> nothing", ch._recent_failure(), None)

    now = time.time()
    _write_state("s1", last_error="auth", last_error_at=now - 60)
    got = ch._recent_failure()
    check("fresh failure reported", got and got[0], "auth")

    # Older than the staleness window: describes a problem that has probably
    # already been fixed, and reporting it trains the user to ignore this.
    _reset()
    _write_state("s1", last_error="auth",
                 last_error_at=now - ch._STALE_AFTER_S - 60)
    check("stale failure ignored", ch._recent_failure(), None)

    _reset()
    _write_state("s1", last_error="timeout", last_error_at=now - 300,
                 last_ok_at=now - 60)
    check("retracted by later success", ch._recent_failure(), None)

    # ACROSS the two capture paths. They keep separate files — they share no
    # lock, so they must not share a mutable one — and retracting only within a
    # file left the backstop warning after per-turn capture had gone on working.
    # The question is "is capture working?", not "did one path once stumble?".
    _reset()
    _write_state("s2.sessionflush", last_error="rate_limited",
                 last_error_at=now - 300)
    got = ch._recent_failure()
    check("a backstop failure alone is reported", got and got[0], "rate_limited")

    _write_state("s2", last_ok_at=now - 60)
    check("a per-turn success retracts it", ch._recent_failure(), None)

    # ...and the reverse direction, so neither path is privileged.
    _reset()
    _write_state("s3", last_error="auth", last_error_at=now - 300)
    _write_state("s3.sessionflush", last_ok_at=now - 60)
    check("a backstop success retracts a per-turn failure",
          ch._recent_failure(), None)

    # Dormancy is not a success. The per-turn hook clears its OWN stale error
    # when it goes dormant on an old server, but must not stamp `last_ok_at` —
    # that would retract a REAL failure the backstop recorded for the same
    # session, from a branch that captured nothing.
    _reset()
    _write_state("s5.sessionflush", last_error="auth", last_error_at=now - 60)
    _write_state("s5", unsupported=True)          # dormant: no last_ok_at
    got = ch._recent_failure()
    check("dormancy does not retract a real backstop failure",
          got and got[0], "auth")

    # An OLDER success must not retract a NEWER failure.
    _reset()
    _write_state("s4.sessionflush", last_error="error", last_error_at=now - 60)
    _write_state("s4", last_ok_at=now - 300)
    got = ch._recent_failure()
    check("an older success does not retract", got and got[0], "error")

    _reset()
    _write_state("s1", last_error="timeout", last_error_at=now - 60,
                 last_ok_at=now - 300)
    got = ch._recent_failure()
    check("failure after last success stands", got and got[0], "timeout")

    # Newest wins — an older breadcrumb cannot say anything the newest doesn't.
    _reset()
    _write_state("old", last_error="timeout", last_error_at=now - 600)
    time.sleep(0.01)
    _write_state("new", last_error="auth", last_error_at=now - 60)
    got = ch._recent_failure()
    check("newest breadcrumb wins", got and got[0], "auth")


def test_messages() -> None:
    print("\nmessages")
    check("healthy is silent", ch._message(HOST, None, None), None)

    msg = ch._message(HOST, "unrenewable", None)
    check("unrenewable names the fix",
          bool(msg and "/memhub:login" in msg), True)
    check("unrenewable names the host", bool(msg and HOST in msg), True)

    msg = ch._message(HOST, "never", None)
    check("never-authed names the fix",
          bool(msg and "/memhub:login" in msg), True)

    msg = ch._message(HOST, "no_refresh", None)
    check("no_refresh warns before it breaks", bool(msg), True)
    # It works right now; calling it expired would be false and would teach the
    # user that the banner overstates things.
    check("no_refresh does not claim it already expired",
          "expired" not in (msg or "").split("expires")[0], True)
    # It must NOT send the user to re-authenticate. Logging in again cannot
    # produce a refresh token the authorization server declined to issue, so
    # that advice sends them round a loop that never converges — banner,
    # re-login, identical state, banner. Advice that cannot work is worse than
    # no advice, because it spends the user's trust proving it.
    check("no_refresh does not tell them to re-login",
          "/memhub:login" not in (msg or ""), True)
    check("no_refresh names the remedy that can actually work",
          bool(msg and "Offline Access" in msg and "mhk_" in msg), True)

    # The token problem is true right now; a breadcrumb only proves something
    # was broken when it was written. So the token check leads.
    msg = ch._message(HOST, "unrenewable", ("timeout", time.time()))
    check("token problem outranks breadcrumb",
          bool(msg and "cannot be renewed" in msg), True)

    msg = ch._message(HOST, None, ("server_rejected", time.time() - 120))
    check("breadcrumb reported when token is fine",
          bool(msg and "rejected" in msg), True)

    msg = ch._message(HOST, None, ("wat_is_this", time.time()))
    check("unknown reason still produces a message", bool(msg), True)

    # Running out of the hook's own clock is not a credential question, so the
    # generic "--status" tail would send someone to inspect the one thing that
    # was definitely fine — the same misdirection this whole check exists to
    # remove. It names the command that actually finishes the session instead.
    msg = ch._message(HOST, None, ("budget_exhausted", time.time()))
    check("budget exhaustion names the clock",
          bool(msg and "ran out of time" in msg), True)
    check("budget exhaustion does not send them to --status",
          bool(msg and "--status" not in msg), True)
    check("budget exhaustion names the finishing command",
          bool(msg and "import-session" in msg), True)


def test_debounce() -> None:
    print("\ndebounce")
    _reset()
    check("first warning passes", ch._already_warned("sess-1", "sig-a"), False)
    check("same warning suppressed", ch._already_warned("sess-1", "sig-a"), True)
    check("different warning passes", ch._already_warned("sess-1", "sig-b"), False)
    check("other session passes", ch._already_warned("sess-2", "sig-a"), False)
    # No session id means no debounce file to key on; warning twice beats
    # silently swallowing it.
    check("missing session id never suppresses",
          ch._already_warned("", "sig-a"), False)


def _run(payload: dict, env_extra: dict | None = None) -> str:
    """Run the hook as the harness does — a subprocess fed JSON on stdin."""
    env = dict(os.environ, HOME=_TMP_HOME, USERPROFILE=_TMP_HOME,
               CLAUDE_PLUGIN_ROOT=str(_plugin_root()))
    env.pop("MEMHUB_MCP_BASE_URL", None)
    env.update(env_extra or {})
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "capture_health.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=env)
    check("exit code is 0", out.returncode, 0)
    return out.stdout.strip()


def _plugin_root() -> Path:
    """A fake install whose .mcp.json points at prod, so _env_host resolves."""
    root = Path(_TMP_HOME) / "plugin-root"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"memhub": {"url": f"https://{HOST}/mcp-server/mcp"}}
    }), encoding="utf-8")
    return root


def test_end_to_end() -> None:
    print("\nend to end")
    _reset()
    _write_token(exp=time.time() + 3600, refresh=True)
    check("healthy prints nothing", _run({"session_id": "e2e-ok"}), "")

    _reset()
    _write_token(exp=time.time() - 3600, refresh=False)
    out = _run({"session_id": "e2e-bad"})
    payload = json.loads(out)
    # The whole point: this field, and not merely additionalContext, is what
    # Claude Code shows to the user. Emitting only to the model is the bug.
    check("warns via systemMessage",
          "systemMessage" in payload and "MemHub" in payload["systemMessage"],
          True)
    check("also told to the agent",
          "capture health" in
          payload["hookSpecificOutput"]["additionalContext"].lower(), True)
    check("repeat in same session is suppressed",
          _run({"session_id": "e2e-bad"}), "")

    # Deliberately switched off is a configuration, not a fault.
    check("opt-out is silent",
          _run({"session_id": "e2e-off"}, {"MEMHUB_TURN_FLUSH": "0"}), "")


def test_never_raises() -> None:
    print("\nrobustness")
    _reset()
    ch.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (ch.STATE_DIR / "corrupt.json").write_text("{not json", encoding="utf-8")
    (ch.STATE_DIR / "notadict.json").write_text('"a string"', encoding="utf-8")
    (ch.CACHE_DIR / f"tokens-{HOST}.json").write_text("{{{", encoding="utf-8")
    try:
        ch._recent_failure()
        ch._token_problem(HOST)
        check("corrupt state does not raise", True, True)
    except Exception as exc:  # noqa: BLE001
        check(f"corrupt state does not raise ({exc})", False, True)

    check("garbage stdin exits clean", _run({}) is not None, True)


def _rulebook_crumb(what: str, *, ago_min: float, error: str = "boom") -> None:
    """Write `ledger/.last_error` the way the rulebook hook writes it."""
    from datetime import datetime, timedelta
    d = ch.RULEBOOK_DIR / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    when = datetime.now().astimezone() - timedelta(minutes=ago_min)
    (d / ".last_error").write_text(json.dumps({
        "at": when.isoformat(timespec="seconds"), "what": what, "error": error,
    }), encoding="utf-8")


def _rulebook_book(*, fetched_ago_min: float, rules: int = 3) -> None:
    from datetime import datetime, timedelta
    d = ch.RULEBOOK_DIR / "book"
    d.mkdir(parents=True, exist_ok=True)
    when = datetime.now().astimezone() - timedelta(minutes=fetched_ago_min)
    (d / "repo-abcd1234.json").write_text(json.dumps({
        "etag": "e", "fetched_at": when.isoformat(timespec="seconds"),
        "rules": [{"rule_id": str(i)} for i in range(rules)],
    }), encoding="utf-8")


def _rulebook_sent(*, flushed_ago_min: float) -> None:
    """Write `ledger/.sent` the way the hook writes it after an ACCEPTED batch."""
    d = ch.RULEBOOK_DIR / "ledger"
    d.mkdir(parents=True, exist_ok=True)
    at = datetime.fromtimestamp(time.time() - flushed_ago_min * 60).astimezone().isoformat()
    (d / ".sent").write_text(json.dumps({"fires_offset": 1, "conversions_offset": 0, "last_flush_at": at}), encoding="utf-8")


def _clear_rulebook() -> None:
    import shutil
    shutil.rmtree(ch.RULEBOOK_DIR, ignore_errors=True)


def test_rulebook_health() -> None:
    print("rulebook")
    _clear_rulebook()

    # The silence cases matter most: a health line people learn to ignore is
    # worse than no line, and most installs legitimately have no rulebook.
    check("no rulebook tree at all is silent", ch._rulebook_problem(), None)
    _rulebook_book(fetched_ago_min=5, rules=0)
    check("a book with zero rules is not a fault", ch._rulebook_problem(), None)
    _rulebook_book(fetched_ago_min=60 * 24 * 30)
    check("an old book with no error is silent", ch._rulebook_problem(), None)
    _clear_rulebook()

    _rulebook_crumb("fetch", ago_min=10)
    got = ch._rulebook_problem()
    check("a recent fetch failure is reported", got and got[0], "fetch")
    _rulebook_crumb("fetch", ago_min=60 * 30)
    check("a failure older than the staleness window is dropped",
          ch._rulebook_problem(), None)

    # A later success retracts it — otherwise one blip warns for a day.
    _rulebook_crumb("fetch", ago_min=30)
    _rulebook_book(fetched_ago_min=5)
    check("a book fetched AFTER the error retracts it", ch._rulebook_problem(), None)
    _rulebook_book(fetched_ago_min=90)
    got = ch._rulebook_problem()
    check("a book fetched BEFORE the error does not retract it", got and got[0], "fetch")
    _clear_rulebook()

    # The known failure shape gets its own, actionable wording.
    _rulebook_crumb("fetch", ago_min=5, error='400 {"msg":"Missing X-Org-Id header"}')
    got = ch._rulebook_problem()
    check("a missing-org-header refusal is classified as auth", got and got[0], "auth")

    _rulebook_crumb("flush", ago_min=5)
    check("a flush failure is reported", (ch._rulebook_problem() or ("",))[0], "flush")
    _clear_rulebook()

    # Recall has no book of its own; a stale blip must not outlive the lane's
    # recovery. The hook clears the crumb on its next 200 — see the client
    # test — and a book confirmed since is the second witness.
    _rulebook_crumb("recall", ago_min=5)
    check("a recent recall failure is reported",
          (ch._rulebook_problem() or ("",))[0], "recall")
    _rulebook_book(fetched_ago_min=1)
    check("a book confirmed after a recall blip retracts it",
          ch._rulebook_problem(), None)
    _clear_rulebook()
    _rulebook_crumb("nonsense", ago_min=5)
    check("an unknown lane is ignored", ch._rulebook_problem(), None)
    (ch.RULEBOOK_DIR / "ledger" / ".last_error").write_text("{not json", encoding="utf-8")
    check("a corrupt breadcrumb never raises", ch._rulebook_problem(), None)
    _clear_rulebook()

    # Wording: names the consequence, and capture outranks it.
    msg = ch._message(HOST, None, None, ("auth", time.time() - 300))
    check("the auth wording says rules are not arriving", "rules are not reaching" in msg, True)
    check("the auth wording names the fix", "/memhub:login" in msg, True)
    msg = ch._message(HOST, None, None, ("fetch", time.time() - 300))
    check("the fetch wording says the copy is cached", "cached copy" in msg, True)
    msg = ch._message(HOST, None, None, ("flush", time.time() - 300))
    check("the flush wording says rules still show", "showing normally" in msg, True)

    # A recall timeout is not a credential problem, and the banner that shipped
    # for it told people to go check their login. Nothing about that is true.
    msg = ch._message(HOST, None, None, ("recall", time.time() - 300))
    check("the recall wording does not send the user to login",
          "/memhub:login" in msg, False)
    check("the recall wording says the cached rules still show",
          "rules are showing" in msg, True)
    msg = ch._message(HOST, None, None,
                      ("auth", time.time() - 300))
    check("an auth-shaped recall refusal still names the fix",
          "/memhub:login" in msg, True)
    both = ch._message(HOST, "never", None, ("fetch", time.time() - 300))
    check("a capture problem outranks a rulebook one", "capture is not authenticated" in both, True)



if __name__ == "__main__":
    for test in (test_token_states, test_stored_key_outranks_the_oauth_cache,
                 test_breadcrumbs, test_messages,
                 test_debounce, test_rulebook_health, test_end_to_end,
                 test_never_raises):
        test()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall capture_health checks passed")

    # --- review findings on #129: auth wording, and per-lane retraction
    _clear_rulebook()
    _rulebook_crumb("fetch", ago_min=5, error="401 {\"error\":\"missing key 'foo'\"}")
    got = ch._rulebook_problem()
    check("a 401 that merely mentions 'key' is NOT the auth lane", got and got[0], "fetch")
    _rulebook_crumb("fetch", ago_min=5, error="401 refused: the plugin has no access key")
    got = ch._rulebook_problem()
    check("the server's access-key wording IS the auth lane", got and got[0], "auth")
    _clear_rulebook()
    _rulebook_crumb("flush", ago_min=30)
    _rulebook_book(fetched_ago_min=5)
    got = ch._rulebook_problem()
    check("a later FETCH does not retract a flush failure", got and got[0], "flush")
    _rulebook_sent(flushed_ago_min=5)
    check("a later accepted flush retracts it", ch._rulebook_problem(), None)
    _clear_rulebook()
    _rulebook_crumb("flush", ago_min=30)
    _rulebook_sent(flushed_ago_min=60)
    got = ch._rulebook_problem()
    check("a flush success BEFORE the error does not retract it", got and got[0], "flush")
    _clear_rulebook()
    _rulebook_crumb("recall", ago_min=30)
    _rulebook_book(fetched_ago_min=5)
    check("recall rides the fetch path: a later confirmed book retracts it", ch._rulebook_problem(), None)
    _clear_rulebook()


