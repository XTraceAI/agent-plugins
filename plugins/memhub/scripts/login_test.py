"""Self-test for the login command's non-network logic.

The browser flow and the server round-trip need a human and a live backend, so
what is covered here is everything that decides whether the OUTPUT is honest:

* a grant with no refresh token is reported as a failure to renew, not as a
  clean login — getting this wrong is precisely the bug that killed capture for
  a day, reported at the moment it becomes true instead of a day later;
* `--force` removes the cached token (and only that environment's);
* `--status` and `--force` are refused together, because one must never open a
  browser and the other exists to.

Run: python3 login_test.py   (stdlib only — the mcp SDK is imported lazily
inside the functions this test does not call).
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

_TMP_HOME = tempfile.mkdtemp(prefix="login-test-")
os.environ["HOME"] = _TMP_HOME

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

PROD = "https://api.memhub.xtrace.ai/mcp-server/mcp"
STAGING = "https://api.staging.memhub.xtrace.ai/mcp-server/mcp"

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok ' if got == want else 'FAIL'} {label}")


def _import():
    """Import login.py with the mcp SDK present.

    Skips cleanly when it is absent so this file stays runnable under a bare
    python3 like its siblings; the assertions below need the module object.
    """
    try:
        import login  # noqa: PLC0415
        return login
    except ImportError as exc:
        print(f"  skip (mcp SDK unavailable: {exc})")
        return None


def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _write_token(login, url: str, *, refresh: bool, exp: float | None = None) -> Path:
    path = login.token_cache_path(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"access_token": _jwt(exp if exp is not None else time.time() + 3600)}
    if refresh:
        body["refresh_token"] = "rt-value"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_renewal_report(login) -> None:
    print("\nrenewal report")
    _write_token(login, PROD, refresh=True)
    ok, detail = login._renewal_report(PROD)
    check("refresh token -> renewable", ok, True)
    check("mentions the stored token", "refresh token stored" in detail, True)

    # The outage case: the login works right now and is dead tomorrow.
    _write_token(login, PROD, refresh=False)
    ok, detail = login._renewal_report(PROD)
    check("no refresh token -> not renewable", ok, False)
    check("says it cannot renew", "cannot renew" in detail, True)

    login.token_cache_path(PROD).unlink()
    ok, detail = login._renewal_report(PROD)
    check("missing cache -> not renewable", ok, False)


def test_cache_is_per_environment(login) -> None:
    print("\ncache keying")
    prod = _write_token(login, PROD, refresh=True)
    staging = _write_token(login, STAGING, refresh=True)
    check("prod and staging use different files", prod != staging, True)
    # Different Auth0 tenants issue non-interchangeable tokens; one file would
    # have a staging login silently overwrite a prod one.
    prod.unlink()
    check("removing one leaves the other", staging.exists(), True)
    staging.unlink()


def test_duration_format(login) -> None:
    print("\nduration formatting")
    check("hours and minutes", login._fmt_duration(3600 * 2 + 60 * 5), "2h05m")
    check("minutes only", login._fmt_duration(600), "10m")
    check("already expired reads as zero", login._fmt_duration(-500), "0m")


def test_force_restores_the_token_when_login_fails(login) -> None:
    """--force must not leave the user with nothing.

    Clearing the cache is what makes --force meaningful — a still-valid token
    would otherwise be reused and no re-auth would happen. But the likeliest
    use is force-refreshing a WORKING login to pick up a newly granted scope,
    and if the browser flow then fails (tab closed, timeout, offline) deleting
    up front would trade a working login for none at all.

    Driven through the real entry point with a guaranteed-to-fail verify, so
    the rollback is exercised by the same ``finally`` production uses.
    """
    print("\n--force rollback")
    import asyncio

    original = _write_token(login, PROD, refresh=True).read_text(encoding="utf-8")
    stash = login.token_cache_path(PROD).with_suffix(".json.prelogin")

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("browser flow failed")

    real_verify, real_url = login._verify, login.default_url
    login._verify, login.default_url = _boom, lambda: PROD
    try:
        rc = asyncio.run(login._run(status_only=False, force=True))
    finally:
        login._verify, login.default_url = real_verify, real_url

    check("failed login reports failure", rc, 1)
    check("the previous token is back", login.token_cache_path(PROD).exists(), True)
    check("and is byte-identical",
          login.token_cache_path(PROD).read_text(encoding="utf-8"), original)
    check("no stash is left behind", stash.exists(), False)
    login.token_cache_path(PROD).unlink()


def test_force_does_not_trust_an_unverified_token_file(login) -> None:
    """A token FILE is not a successful login.

    The SDK writes the cache the moment the grant returns, so a flow that then
    dies during verification — or a truncated write — leaves a file behind that
    proves nothing. Deciding "a new token landed" from `cache.exists()` would
    discard the known-good backup in favour of something never shown to work,
    which is the same foot-gun the stash exists to prevent, moved one step
    later. Only a verified login may discard the stash.
    """
    print("\n--force does not trust an unverified file")
    import asyncio

    original = _write_token(login, PROD, refresh=True).read_text(encoding="utf-8")
    cache = login.token_cache_path(PROD)
    stash = cache.with_suffix(".json.prelogin")

    async def _write_then_fail(*_args, **_kwargs):
        # Exactly what a grant-then-die looks like on disk.
        cache.write_text('{"access_token": "truncated', encoding="utf-8")
        raise RuntimeError("verification failed after the token was written")

    real_verify, real_url = login._verify, login.default_url
    login._verify, login.default_url = _write_then_fail, lambda: PROD
    try:
        rc = asyncio.run(login._run(status_only=False, force=True))
    finally:
        login._verify, login.default_url = real_verify, real_url

    check("reports failure", rc, 1)
    check("the good token is restored over the partial write",
          cache.read_text(encoding="utf-8"), original)
    check("no stash is left behind", stash.exists(), False)
    cache.unlink()


def test_failure_slugs_all_have_health_messages() -> None:
    """Every reason `flush_turn` can record must render a specific message.

    The generic fallback in `capture_health._message` exists for forward
    compatibility, not as a place for slugs to land silently. This caught
    `unrecognized_response`, which was written by the flush but absent from the
    health vocabulary, so it degraded to "the capture hook failed" — a weaker
    diagnostic in the one feature whose entire purpose is diagnosis.
    """
    print("\nslug vocabulary")
    import re

    import capture_health as ch

    source = (SCRIPTS / "flush_turn.py").read_text(encoding="utf-8")
    # BOTH spellings. The in-flush paths pass the slug as a literal, while the
    # top-level handler picks one into `reason` first — scanning only the call
    # site silently covered two of five slugs while appearing to cover all of
    # them, which is a worse failure than not testing this at all.
    slugs = set(re.findall(r'_mark_failure\(\s*session_id,\s*"([a-z_]+)"', source))
    slugs |= set(re.findall(r'reason,\s*detail\s*=\s*"([a-z_]+)"', source))
    check("every known slug is discovered", len(slugs) >= 5, True)
    for slug in sorted(slugs):
        check(f"{slug!r} has a health message", slug in ch._REASONS, True)


def test_flag_conflict() -> None:
    print("\nflag conflict")
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "login.py"), "--status", "--force"],
        capture_output=True, text=True, env=dict(os.environ, HOME=_TMP_HOME))
    check("--status --force is refused", out.returncode != 0, True)
    check("explains why", "contradictory" in out.stderr, True)


if __name__ == "__main__":
    login = _import()
    if login is not None:
        for test in (test_renewal_report, test_cache_is_per_environment,
                     test_duration_format,
                     test_force_restores_the_token_when_login_fails,
                     test_force_does_not_trust_an_unverified_token_file):
            test(login)
        test_failure_slugs_all_have_health_messages()
        test_flag_conflict()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall login checks passed")
