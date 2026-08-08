#!/usr/bin/env python3
"""Authenticate this plugin install to MemHub — the command behind /memhub:login.

**Why a dedicated command.** The plugin's hooks authenticate from a token cache
that only a FOREGROUND script can create (see ``_memhub_auth``: a background
hook must never open a browser, so it can only ever consume a token someone
else minted). Until this script existed, nothing in the product provisioned
that token on purpose — it appeared as a side effect of running some unrelated
foreground script, and the advice given to a stuck user was "run
/memhub:import-session", which is a different operation that does real,
unrequested work and can fail for reasons having nothing to do with auth. That
made the one signal you were trying to read — am I authenticated? — impossible
to read cleanly.

**What it checks beyond "did the browser flow succeed".** A login that works
today and dies tomorrow is not a successful login. If the authorization server
issues no refresh token, the access token simply expires (24h here) and every
hook goes quiet until someone logs in again — which is exactly how per-turn
capture died on production for a full day without a symptom. So this reports
renewal as a first-class result, at the moment the token is minted, rather than
leaving it to be discovered a day later by its absence.

Usage (all optional):
    login.py             log in if needed, then verify and report
    login.py --status    report only; never opens a browser
    login.py --force     discard the cached token and re-run the browser flow

Which backend it targets follows the INSTALL: run from the ``memhub`` plugin it
authenticates production, from ``memhub-staging`` it authenticates staging.
They are different Auth0 tenants with separate caches, so logging into one says
nothing about the other.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pak  # noqa: E402 — stdlib-only, sits beside this file
from pak import PakError  # noqa: E402

# The MCP SDK logs the OAuth flow's exception WITH a traceback before letting it
# propagate. Under --status a missing token is an expected, reported outcome —
# not a crash — and a stack trace above a one-line "NOT LOGGED IN" reads like
# the tool broke. Same silencer the capture hooks use.
logging.getLogger("mcp.client.auth").setLevel(logging.CRITICAL)

from _memhub_auth import (  # noqa: E402
    NonInteractiveAuthRequired,
    _access_token_expiry,
    default_url,
    resolve_url_and_auth,
    token_cache_path,
)
from room_map import env_for_url  # noqa: E402


def _fmt_duration(seconds: float) -> str:
    """Human duration. Days once there are any — a 90-day key rendered as
    "2159h59m" is technically right and unreadable, and the number people need
    to sanity-check is "about three months", not the hour count."""
    seconds = max(0, int(seconds))
    days, rem = seconds // 86400, seconds % 86400
    hours, minutes = rem // 3600, (rem % 3600) // 60
    if days:
        return f"{days}d{hours:02d}h"
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _renewal_report(url: str) -> tuple[bool, str]:
    """``(ok, description)`` for whether this login can renew itself.

    Read from the cache the SDK just wrote, because the grant is the only
    authority on what was actually issued — asking for ``offline_access`` and
    receiving it are different things, and the difference is invisible until
    the access token lapses.
    """
    try:
        cached = json.loads(token_cache_path(url).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False, "unknown (token cache unreadable)"
    if not cached.get("refresh_token"):
        return False, "NONE — this login cannot renew itself"
    exp = _access_token_expiry(cached.get("access_token") or "")
    if exp is None:
        return True, "automatic (refresh token stored)"
    return True, (f"automatic (refresh token stored; access token valid "
                  f"{_fmt_duration(exp - time.time())})")


async def _verify(url: str, headers, auth) -> int:
    """Prove the credential actually works, and return the tool count.

    A cached token file is not proof of anything — it can be expired, revoked,
    or issued by the wrong tenant. ``list_tools`` is the cheapest call that
    exercises the full path (transport, auth, server) with no side effects.
    """
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            return len((await session.list_tools()).tools)


def _describe_expiry(record: dict | None) -> str:
    remaining = pak.expires_in_s(record)
    if remaining is None:
        return "does not expire"
    return f"expires in {_fmt_duration(remaining)}"


def _report_key(url: str, record: dict | None) -> int:
    """Print the state of the access key that authenticated this run."""
    if not record:
        print("credential  : access key (details unavailable)")
        return 0
    print(f"credential  : access key '{record.get('label')}' "
          f"({_describe_expiry(record)})")
    print("renewal     : n/a — a key does not refresh; "
          "/memhub:login mints a new one when this lapses")
    return 0


def _ensure_key(url: str, env: str) -> bool:
    """Mint (or reuse) this machine's access key using the fresh OAuth token.

    Returns whether a key is now in place — which decides whether the OAuth
    token's own renewal is still worth reporting.

    Best-effort by design. A failure here is NOT a failed login: OAuth just
    verified, so capture works today either way, and turning a successful login
    into an error over an optimisation would be the wrong trade. It is reported
    plainly so the user knows they are still on the short-lived credential.
    """
    try:
        cached = json.loads(token_cache_path(url).read_text(encoding="utf-8"))
        bearer = cached.get("access_token")
        if not bearer:
            raise PakError("no access token to authorise minting with")
        record, how = pak.ensure(url, bearer)
    except PakError as exc:
        print(f"access key  : NOT created ({exc})")
        print("              capture will keep using the OAuth token, which "
              "expires; re-run /memhub:login when it does.")
        return False
    except Exception as exc:  # noqa: BLE001 — never fail a good login over this
        print(f"access key  : NOT created ({type(exc).__name__}: {exc})")
        return False

    verb = {"reused": "reusing", "replaced": "replaced orphaned key",
            "minted": "created"}.get(how, how)
    print(f"access key  : {verb} '{record.get('label')}' "
          f"({_describe_expiry(record)})")
    print(f"              the {env} hooks now authenticate with this key "
          "instead of the expiring OAuth token.")
    return True


async def _run(status_only: bool, force: bool) -> int:
    url = default_url()
    env = env_for_url(url)
    cache = token_cache_path(url)

    # --force must move the old credential OUT OF THE WAY, not destroy it.
    #
    # Clearing the cache is what makes --force mean anything: a still-valid
    # cached token would otherwise be used happily and no re-authentication
    # would happen at all. But deleting outright is a durability foot-gun, and
    # it bites hardest in the most likely case — force-refreshing a WORKING
    # token to pick up a newly granted scope. If the browser flow then fails
    # (tab closed, timeout, no network), the user has traded a working login
    # for none at all. So it is set aside and restored unless a verified login
    # replaces it.
    stash: Path | None = None
    if force and cache.exists():
        stash = cache.with_suffix(".json.prelogin")
        cache.replace(stash)
        print(f"set aside the cached {env} token (restored if login fails)")

    # The stored key has to go too, and it is the more important half now: a
    # valid key short-circuits auth entirely, so leaving it would make --force
    # confirm the exact credential the user is trying to replace and never
    # reach the browser at all. Dropping the LOCAL copy is safe on its own —
    # `pak.ensure` revokes the server-side key it finds under this label before
    # minting, so the cap keeps counting real credentials.
    key_stash: dict | None = None
    if force:
        key_stash = pak.load(url)
        if key_stash:
            pak.forget(url)
            print(f"set aside the stored {env} access key "
                  f"'{key_stash.get('label')}' (restored if login fails)")

    # Set only after the new credential has been VERIFIED against the server.
    # The stash is discarded on this flag and not on `cache.exists()`, because
    # existence is not success: the SDK writes the token as soon as the grant
    # returns, so a flow that dies during verification — or a truncated write —
    # leaves a file behind that proves nothing. Keying on existence would trade
    # a known-good credential for an unproven one, which is the same durability
    # foot-gun the stash exists to prevent, just moved one step later.
    verified = False

    def _restore() -> None:
        """Reinstate the old credentials unless a verified login replaced them."""
        # The key first: if the login failed, the old key is very likely still
        # valid server-side (nothing revokes it until `pak.ensure` runs, which
        # only happens after verification), so putting it back restores working
        # capture rather than merely restoring a file.
        if key_stash and not (verified and pak.load(url)):
            pak.save(url, key_stash)
            print(f"login did not complete — restored the previous {env} access key")

        if not (stash and stash.exists()):
            return
        if verified and cache.exists():
            stash.unlink()  # superseded by a login we actually proved works
        else:
            # Atomic rename, so it also overwrites any unverified remnant the
            # failed flow left at `cache`.
            stash.replace(cache)
            print(f"login did not complete — restored the previous {env} token")

    print(f"environment : {env} ({url})")

    # ``finally``, so the stash is resolved on EVERY exit — a clean failure, an
    # unexpected raise, or success. _restore() is a no-op once a new token has
    # landed, which makes "always call it" the correct rule rather than a
    # per-branch judgement that a later edit could forget.
    try:
        try:
            # The returned url is DISCARDED, deliberately. resolve_url_and_auth
            # echoes back exactly the url it was given (it only substitutes
            # default_url() when passed None, and we pass one), so rebinding it
            # here would add nothing while making `cache`, computed above from
            # the same url, look like it might refer to a different file than
            # the one the SDK writes. It cannot; keeping one binding is what
            # makes that obvious rather than merely true.
            _, headers, auth = resolve_url_and_auth(url, interactive=not status_only)
        except Exception as exc:  # noqa: BLE001 — report, never traceback
            print(f"status      : FAILED to prepare auth ({exc})")
            return 1

        # Three sources now, not two. Inferring from `headers` alone reported a
        # stored access key as "$MEMHUB_TOKEN" — naming the wrong credential in
        # the one command whose job is telling you which credential you are on.
        if os.environ.get("MEMHUB_TOKEN", "").strip():
            source = "bearer ($MEMHUB_TOKEN)"
        elif headers:
            source = "stored access key (mhk_)"
        else:
            source = "browser OAuth (plugin client)"
        print(f"mode        : {source}")

        try:
            tools = await _verify(url, headers, auth)
        except BaseException as exc:  # noqa: BLE001 — anyio wraps failures in groups
            if _is_noninteractive(exc):
                # --status only. Says nothing about whether a browser login
                # WOULD work; it reports that no usable token is cached now.
                print("status      : NOT LOGGED IN (no usable cached token)")
                print("fix         : run /memhub:login")
                return 1
            print(f"status      : FAILED ({type(exc).__name__}: {exc})")
            return 1

        # Proven against the server — only now may the stash be discarded.
        verified = True
        print(f"status      : OK — server exposes {tools} tools")

        if os.environ.get("MEMHUB_TOKEN", "").strip():
            # Provisioned outside this flow entirely; nothing here owns its
            # lifecycle, so there is no renewal story to tell and no key to mint.
            print("renewal     : n/a ($MEMHUB_TOKEN is supplied explicitly)")
            return 0

        if headers:
            # A stored access key answered — this login had nothing to do but
            # confirm it still works.
            return _report_key(url, pak.load(url))

        # OAuth verified. Trade that short-lived session for a durable key, so
        # the hooks — which can never open a browser — stop depending on a
        # credential that expires inside a day and needs a refresh they cannot
        # perform from a cold process.
        if _ensure_key(url, env):
            # The OAuth token is now a bootstrap artefact, not the credential
            # anything runs on. Reporting its renewal here would describe the
            # wrong thing — and worse, an "issued no refresh token" warning
            # would send the user to fix a tenant setting that no longer has
            # any bearing on whether capture keeps working.
            print("renewal     : not needed — the key is the credential now; "
                  "/memhub:login mints a fresh one when it lapses")
            return 0

        ok, detail = _renewal_report(url)
        print(f"renewal     : {detail}")
        if not ok:
            # Not a failed login — the token works right now. It is a login
            # with a known expiry date and no recovery, which is worth saying
            # loudly here rather than discovering as silence tomorrow.
            print()
            print("WARNING: the authorization server issued no refresh token, so this")
            print("login will stop working when the access token expires, and memory")
            print("capture will go quiet until someone runs /memhub:login again.")
            print("To fix it at the source, enable 'Allow Offline Access' on the")
            print(f"{env} API in Auth0 and make sure 'offline_access' appears in the")
            print("server's advertised scopes_supported, then re-run /memhub:login --force.")
        return 0
    finally:
        _restore()


def _is_noninteractive(exc: BaseException) -> bool:
    """True if NonInteractiveAuthRequired is anywhere in the exception tree.

    The MCP client runs auth inside anyio task groups, so the raise surfaces
    wrapped in ExceptionGroups or chained as __cause__ rather than bare. Same
    walk as the capture hooks use, for the same reason.
    """
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, NonInteractiveAuthRequired):
            return True
        stack.extend(getattr(current, "exceptions", ()) or ())
        for link in (current.__cause__, current.__context__):
            if link is not None:
                stack.append(link)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authenticate this MemHub plugin install.")
    parser.add_argument("--status", action="store_true",
                        help="report only; never opens a browser")
    parser.add_argument("--force", action="store_true",
                        help="discard the cached token and log in again")
    args = parser.parse_args()
    if args.status and args.force:
        parser.error("--status and --force are contradictory: --status must "
                     "never open a browser, and --force exists to open one.")
    return asyncio.run(_run(args.status, args.force))


if __name__ == "__main__":
    raise SystemExit(main())
