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
from pathlib import Path

# Redirect HOME before importing, so the module's import-time CACHE_DIR /
# STATE_DIR resolve inside the sandbox and no test can read or write the real
# ~/.config/memhub-plugin. Exported so the CLI subprocess below inherits it.
_TMP_HOME = tempfile.mkdtemp(prefix="capture-health-test-")
os.environ["HOME"] = _TMP_HOME
os.environ.pop("MEMHUB_TOKEN", None)
os.environ.pop("MEMHUB_TURN_FLUSH", None)
os.environ.pop("MEMHUB_MCP_BASE_URL", None)

sys.path.insert(0, str(Path(__file__).resolve().parent))
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

    # A freshly minted unrenewable token is not yet news.
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

    pak.save(ch._mcp_url_for(HOST),
             {"secret": "mhk_x", "label": "test", "expires_at": _iso(days=-1)})
    check("expired key is reported", ch._token_problem(HOST), "key_expired")

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

    # A later success in the same session retracts the failure.
    _reset()
    _write_state("s1", last_error="timeout", last_error_at=now - 300,
                 last_ok_at=now - 60)
    check("retracted by later success", ch._recent_failure(), None)

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
    env = dict(os.environ, HOME=_TMP_HOME, CLAUDE_PLUGIN_ROOT=str(_plugin_root()))
    env.pop("MEMHUB_MCP_BASE_URL", None)
    env.update(env_extra or {})
    out = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "capture_health.py")],
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


if __name__ == "__main__":
    for test in (test_token_states, test_stored_key_outranks_the_oauth_cache,
                 test_breadcrumbs, test_messages,
                 test_debounce, test_end_to_end, test_never_raises):
        test()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall capture_health checks passed")
