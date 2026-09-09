#!/usr/bin/env python3
"""Tell the user when memory capture has stopped working (stdlib only).

**Why this exists.** The hooks that capture sessions — the per-turn ``Stop``
flush and the ``SessionEnd`` backstop — are declared ``async: true``, and Claude
Code surfaces an async hook's stdout NOWHERE: not to the user, not to the agent.
Those scripts are also written to never fail loudly, because a memory hook must
never disturb a coding session. Correct in isolation, the two rules compose into
a system that cannot report its own death: every failure path ended in a
``print`` nobody could read and an ``exit 0``.

``flush_turn.py`` writes WHY it failed into its per-session
state (``last_error``/``last_error_at``). This script runs on ``SessionStart``,
which is synchronous, so its output is real: it reports through
``systemMessage`` — the one hook field Claude Code shows to the USER — and
mirrors it into ``additionalContext`` so the agent can answer "is my memory
working?" without re-deriving any of this.

**It has to be silent when things are fine.** A health check that speaks every
session is one the user learns to scroll past, and then it is worth nothing on
the day it matters. So: no output at all on the healthy path, and no output when
capture is switched off on purpose (``MEMHUB_TURN_FLUSH=0``) — that is a
configuration, not a fault.

Stdlib only and no network: this runs before the user's first prompt, and every
millisecond here is one they wait. Measured ~20ms. Never raises — a broken
health check must not be the thing that breaks a session.

Run the self-test:  python3 tests/capture_health_test.py  (from the repo root;
tests live outside the plugin so they are not shipped to installs)
"""
from __future__ import annotations

import base64
import json
import re
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

CACHE_DIR = Path.home() / ".config" / "memhub-plugin"
STATE_DIR = CACHE_DIR / "turnflush"
# The rulebook keeps its own tree, relocatable together for tests (the hook
# reads the same variable).
RULEBOOK_DIR = Path(os.environ.get("MEMHUB_RULEBOOK_BASE")
                    or os.path.expanduser("~/.config/memhub-plugin/rulebook"))

# How far back a recorded failure still counts as news. A breadcrumb from last
# week describes a problem that has probably already been fixed (or a machine
# that has moved on), and reporting it would train the user to ignore this.
_STALE_AFTER_S = 24 * 3600

# Ceiling on the state files examined, newest first. The dir grows by one file
# per session forever, and this runs in the user's startup path; an unbounded
# scan would get slower every day for information the newest few already carry.
_MAX_STATE_FILES = 40

# How close an unrenewable token must be to lapsing before this check speaks.
# `login.py` already names the tenant fix at mint time; repeating it every
# session for the token's whole life would be that message as wallpaper. A few
# hours is enough warning to act and short enough that most sessions never see
# it. Sized against the ~24h access token these grants issue.
_NO_REFRESH_WARN_WITHIN_S = 6 * 3600

# Failures worth interrupting someone over, and what to say. Only ``auth`` is
# truly terminal — no retry can mint a token, so capture stays dead until a
# human re-authenticates. The rest are reported because they persisted, not
# because a single occurrence means anything.
# Deliberately absent: the server-too-old dormancy. It is a degrade with a
# working fallback rather than a break, it can never be retracted (a dormant
# session runs no further flush), and being environmental it recurs on every
# new session — so routing it here would put a banner on every session start
# for a day. `flush_turn` records it as `unsupported` instead and does not
# breadcrumb it.
#
# Every slug ``flush_turn._mark_failure`` can write needs an entry here; the
# generic fallback exists for forward compatibility, not as a place for slugs
# to land silently. `flush_turn_test` asserts the two stay in step, because a
# drifted vocabulary quietly degrades the exact diagnostic this feature is for.
_REASONS = {
    "auth": "the plugin's saved login expired and could not be renewed",
    "server_rejected": "the server rejected the last upload",
    # Backpressure, not a fault: a key runs at one seat's throughput and a fleet
    # flushing every turn can reach it. Worded so it does not read as "your
    # session was refused" — the cursor is unmoved and the next turn retries.
    "rate_limited": "capture is being throttled and is retrying on its own",
    # Distinct from `auth`, and the distinction is the whole point: a new login
    # mints an equivalent credential, so pointing there would not converge.
    "forbidden": ("the credential is valid but not permitted to write here "
                  "(check its scopes and org access)"),
    "unrecognized_response": "the server sent a reply the plugin could not read",
    "unconfirmed_provenance": ("the session reached memory, but its captured "
                               "pull-request URL was not acknowledged"),
    "timeout": "the server stopped responding",
    # Ran out of its own clock, not a fault of the server or the credential.
    # Named separately because the alternative was reporting whatever error the
    # attempt BEFORE it produced, which pointed at a cause that had already been
    # dealt with.
    "budget_exhausted": "capture ran out of time before it finished sending",
    "error": "the capture hook hit an unexpected error",
}


def _env_host() -> str | None:
    """Host of the MemHub backend this plugin talks to, or None.

    Read from the INSTALLED plugin's own ``.mcp.json`` (prod ``memhub`` and
    ``memhub-staging`` are separate installs against separate Auth0 tenants and
    separate token caches), so the health of the wrong environment is never
    reported. Mirrors ``_memhub_auth.default_url``'s precedence, minus the
    fallbacks that need the mcp SDK — this module must stay importable under a
    bare python3.
    """
    base = os.environ.get("MEMHUB_MCP_BASE_URL")
    if base:
        return urlparse(base).netloc or None
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return None
    try:
        servers = json.loads(
            (Path(root) / ".mcp.json").read_text(encoding="utf-8")
        ).get("mcpServers", {})
    except (OSError, ValueError, TypeError):
        return None
    for name, cfg in servers.items():
        if name.lower().startswith("memhub") and isinstance(cfg, dict):
            return urlparse(cfg.get("url") or "").netloc or None
    return None


def _jwt_exp(access_token: str) -> float | None:
    """The ``exp`` claim from a JWT, or None if it isn't a decodable JWT.

    Read, never verified — the resource server does the real validation. We only
    need to know whether this token is already worthless. Deliberately the same
    approach as ``_memhub_auth._access_token_expiry``: reading the token's OWN
    claim rather than the file's mtime keeps the answer immune to a copy,
    restore, or sync that would make an expired token look freshly issued.
    """
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 — opaque/non-JWT → expiry unknowable
        return None


# A 90-day key is not news at day 30. This is the window where saying so is
# actionable rather than noise — and unlike the OAuth "cannot renew" case, the
# fix here genuinely is one command, because /memhub:login mints a fresh key.
_KEY_EXPIRY_WARN_WITHIN_S = 7 * 86400


def _stored_key(host: str) -> str | None | object:
    """Verdict for a stored access key, or None if there is no key to judge.

    Returns the sentinel ``_HEALTHY`` when a key exists and is fine — distinct
    from ``None`` (no key at all), because the caller must be able to tell "the
    key is good, stop looking" from "fall through to the OAuth cache".
    """
    try:
        import pak  # noqa: PLC0415 — stdlib-only, beside this file
        record = pak.load(_mcp_url_for(host))
        if not record:
            return None
        remaining = pak.expires_in_s(record)
        if remaining is None:
            return _HEALTHY  # non-expiring key
        if remaining <= 0:
            return "key_expired"
        if remaining <= _KEY_EXPIRY_WARN_WITHIN_S:
            return "key_expiring"
        return _HEALTHY
    except Exception:  # noqa: BLE001 — a broken key module means "no key"
        return None


def _mcp_url_for(host: str) -> str:
    """Reconstruct the endpoint `pak` keys its storage by.

    `pak` keys on the URL's netloc, which is exactly the `host` this module
    already resolved, so the path is irrelevant to the lookup.
    """
    return f"https://{host}/mcp-server/mcp"


class _Healthy:
    """Sentinel: a credential was found and it is fine."""


_HEALTHY = _Healthy()


def _token_problem(host: str) -> str | None:
    """The reason capture cannot authenticate to ``host``, or None if it can.

    The presence of a refresh token is checked INDEPENDENTLY of expiry, because
    the two facts are known with different confidence. Whether the token has
    lapsed depends on reading an ``exp`` we may not be able to decode; whether
    it can ever renew itself is simply whether a refresh token is there. A
    short-circuit on expiry first would let an unreadable ``exp`` return
    "healthy" and throw away the second fact entirely — including for a token
    that provably could never recover.

    The states:

    * no cache file — never authenticated here, or it was cleared;
    * expired WITH a refresh token — healthy, and deliberately silent: the
      flush renews it before the SDK runs. This is the normal overnight path
      and warning about it every morning is how a banner becomes wallpaper;
    * expired WITHOUT one — broken now, and nothing automatic recovers it;
    * not known-expired but WITHOUT one — works today, dead within the access
      token's lifetime, and no retry can save it. Reported for the same reason
      ``login.py`` reports renewal at mint time: this is exactly the state that
      killed production capture, and the day of warning it buys is the whole
      difference between a fix and an outage.

    An unreadable ``exp`` alone is never treated as a fault — that would be
    guessing — and it does not suppress the refresh-token fact either.
    """
    if os.environ.get("MEMHUB_TOKEN", "").strip():
        return None  # explicit bearer (incl. an mhk_ key) — nothing here applies

    # A stored access key outranks the OAuth cache, exactly as it does in
    # `resolve_url_and_auth`. Judging the OAuth token while the hooks
    # authenticate with a key would report on a credential nothing uses — and
    # would fire the "cannot renew" warning at someone whose setup is now
    # immune to that entire failure mode.
    key = _stored_key(host)
    if key is _HEALTHY:
        return None            # a good key; the OAuth cache is irrelevant now
    if key == "key_expiring":
        return "key_expiring"  # still the live credential, just not for long

    # An EXPIRED key is not the end of the story, because `resolve_url_and_auth`
    # does not treat it as one — it falls through to the OAuth cache, which may
    # still work. Short-circuiting here announced "capture is not running" at
    # people whose capture was running fine. So mirror the fallback: judge OAuth
    # too, and only speak if that is broken as well.
    try:
        cached = json.loads(
            (CACHE_DIR / f"tokens-{host.replace(':', '_')}.json")
            .read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        # Nothing to fall back to. Name the KEY when there was one — someone
        # whose key lapsed has plainly authenticated before, and telling them
        # they never did describes the wrong problem even though the remedy
        # happens to match.
        return "key_expired" if key == "key_expired" else "never"
    renewable = bool(cached.get("refresh_token"))
    exp = _jwt_exp(cached.get("access_token") or "")
    if exp is not None and time.time() >= exp:
        return None if renewable else "unrenewable"
    if renewable:
        return None
    # Unrenewable but still working. This is a SCHEDULED outage, and who says so
    # depends on when: `login.py` reports it at mint time, with the tenant fix
    # named, which is when it is cheapest to act on. Repeating that here every
    # session for the token's whole life would just be that same message as
    # wallpaper. So this check stays quiet until the outage is actually close —
    # or until we cannot tell how close it is, where silence would be a guess.
    if exp is None or exp - time.time() <= _NO_REFRESH_WARN_WITHIN_S:
        return "no_refresh"
    return None


def _recent_failure() -> tuple[str, float] | None:
    """The newest still-relevant ``(reason, when)`` recorded by a flush.

    Newest-first and returns on the first hit: an older breadcrumb cannot say
    anything the newest one doesn't, and stopping early is what keeps a
    long-lived state dir off the startup path.
    """
    try:
        files = sorted(STATE_DIR.glob("*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    cutoff = time.time() - _STALE_AFTER_S
    for path in files[:_MAX_STATE_FILES]:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(state, dict):
            continue
        reason, when = state.get("last_error"), state.get("last_error_at")
        if not reason or not isinstance(when, (int, float)) or when < cutoff:
            continue
        # A later success retracts the failure — from EITHER capture path, not
        # just the file the failure was recorded in.
        #
        # The two hooks keep separate files (they share no lock, so they must
        # not share a mutable one). Retracting only within a file meant a
        # backstop failure kept warning even after per-turn capture had gone on
        # working — and the question this check answers is "is capture
        # working?", not "did this particular path once stumble?".
        if _succeeded_since(path, when):
            continue
        return str(reason), float(when)
    return None


def _session_of(path: Path) -> str:
    """The session id a state file belongs to, whichever hook wrote it.

    `<id>.json` from the per-turn flush, `<id>.sessionflush.json` from the
    backstop — both name the same session, which is what makes one path's
    success able to speak for the other's failure.
    """
    name = path.name[: -len(".json")] if path.name.endswith(".json") else path.name
    return name[: -len(".sessionflush")] if name.endswith(".sessionflush") else name


def _succeeded_since(path: Path, when: float) -> bool:
    """Whether any capture path recorded a success for this session after
    ``when``. Reads only that session's files, so the scan stays bounded."""
    session = _session_of(path)
    for candidate in (STATE_DIR / f"{session}.json",
                      STATE_DIR / f"{session}.sessionflush.json"):
        try:
            state = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(state, dict):
            continue
        ok_at = state.get("last_ok_at")
        if isinstance(ok_at, (int, float)) and ok_at >= when:
            return True
    return False


# Every rulebook lane that can leave a breadcrumb. Declared once, and asserted
# in `capture_health_test.py`: a lane added here without its own branch in
# `_message` falls through to a generic line that names no cause and offers no
# true fix, which is how a recall timeout came to tell people their login was
# broken. The test fails until the new lane says something true.
LANES = ("fetch", "flush", "recall")


def _rulebook_problem() -> tuple[str, float] | None:
    """``(what, when)`` for a rulebook lane that is failing right now.

    The rulebook fails SILENTLY by construction: every lane exits 0 and the
    cache is left as it was, so a book frozen weeks ago is indistinguishable
    from a current one, and a client that has never fetched at all looks
    exactly like an org with nothing to say. `ledger/.last_error` is the only
    trace, and nobody reads a file they do not know exists.

    Deliberately NOT reported: no book, or a book with no rules. A team whose
    rulebook is empty is not broken, and warning there would train people to
    ignore this line — the one thing a health check cannot afford.
    """
    try:
        raw = (RULEBOOK_DIR / "ledger" / ".last_error").read_text(encoding="utf-8")
        crumb = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(crumb, dict):
        return None
    what, at = crumb.get("what"), crumb.get("at")
    if what not in LANES or not isinstance(at, str):
        return None
    try:                       # the hook writes a local-offset ISO stamp
        when = datetime.fromisoformat(at).timestamp()
    except ValueError:
        return None
    if when < time.time() - _STALE_AFTER_S:
        return None
    # A later success on the SAME lane retracts it. Without this a single blip
    # would warn for a day; with a fetch-only check a recovered flush would too.
    if _lane_recovered(str(what), when):
        return None
    # The known shape worth naming separately: without a personal access key the
    # client falls back to an OAuth token and no lane sends an org header, so
    # every rulebook call is refused. Minting a key is the fix, and it works.
    # Keyed on the server's own wording, never on a bare "401" + "key" (the word
    # "key" is in half of all JSON error bodies).
    text = str(crumb.get("error") or "")
    if _AUTH_REFUSAL.search(text):
        return "auth", when
    return str(what), when


_AUTH_REFUSAL = re.compile(r"X-Org-Id|access key|no key configured", re.I)


def _lane_recovered(what: str, when: float) -> bool:
    """Has the lane that failed at ``when`` succeeded since?

    fetch  — a book confirmed after the error (`book/*.json:fetched_at`).
    flush  — an accepted batch after the error (`ledger/.sent:last_flush_at`,
             written by the hook only when the server accepted rows).
    recall — clears its own crumb on the next 200 (`_breadcrumb_clear`), so a
             surviving crumb already means no recall has succeeded since. The
             book check below still stands as a second witness: recall only
             runs on PreToolUse, so a session that has issued none of them
             would otherwise carry the last one's blip forever.
    """
    def _stamp_after(path: Path, key: str) -> bool:
        try:
            got = json.loads(path.read_text(encoding="utf-8")).get(key)
            return isinstance(got, str) and datetime.fromisoformat(got).timestamp() >= when
        except (OSError, ValueError, TypeError, AttributeError):
            return False
    if what == "flush":
        return _stamp_after(RULEBOOK_DIR / "ledger" / ".sent", "last_flush_at")
    try:
        return any(_stamp_after(b, "fetched_at") for b in (RULEBOOK_DIR / "book").glob("*.json"))
    except OSError:
        return False


def _message(host: str, token_problem: str | None,
             failure: tuple[str, float] | None,
             rulebook: tuple[str, float] | None = None) -> str | None:
    """The warning to show, or None when there is nothing worth saying.

    The token check leads when it fires, because it names a cause the user can
    act on and it is true right now — a breadcrumb only proves something was
    broken when it was written.
    """
    # /memhub:login, not /memhub:import-session — importing a session is a
    # different operation that does real unrequested work and can fail for
    # reasons unrelated to auth, which muddies the very signal being reported.
    fix = ("Run /memhub:login to authenticate "
           "(the plugin has its own login, separate from /mcp).")
    if token_problem == "never":
        return (f"MemHub capture is not authenticated for {host}, so this "
                f"session is not being saved to memory. {fix}")
    if token_problem == "unrenewable":
        return (f"MemHub capture is broken: the saved login for {host} expired "
                f"and cannot be renewed automatically, so nothing from this "
                f"session is reaching memory. {fix}")
    if token_problem == "key_expired":
        return (f"MemHub capture is not running: its access key for {host} has "
                f"expired, so nothing from this session is reaching memory. {fix}")
    if token_problem == "key_expiring":
        # Unlike the OAuth "cannot renew" case, re-authenticating genuinely
        # fixes this — /memhub:login mints a replacement — so it names the fix.
        return (f"MemHub capture's access key for {host} expires soon. {fix}")
    if token_problem == "no_refresh":
        # Deliberately NOT phrased as "expired" — it works right now. And
        # deliberately NOT told to re-authenticate: logging in again cannot
        # produce a refresh token the authorization server declined to issue,
        # so `fix` here would send the user round a loop that never converges —
        # banner, re-login, identical state, banner. Advice that cannot work is
        # worse than no advice, because it spends the user's trust proving it.
        return (f"MemHub capture for {host} will stop soon: its login has no "
                f"refresh token, so it cannot renew itself. Re-authenticating "
                f"will not help — the server is not issuing refresh tokens. "
                f"Enable 'Allow Offline Access' on that API, or set "
                f"$MEMHUB_TOKEN to a personal access key (mhk_…), which the "
                f"hooks use directly and which does not expire.")
    if failure:
        reason, when = failure
        detail = _REASONS.get(reason, "the capture hook failed")
        ago = max(0, int((time.time() - when) / 60))
        when_txt = f"{ago}m ago" if ago < 120 else f"{ago // 60}h ago"
        # NOT "check /mcp" — the connector is a separate token store, so its
        # status says nothing about capture health. Sending someone there to
        # diagnose this would contradict the whole reason this check exists.
        if reason == "auth":
            tail = fix
        elif reason == "budget_exhausted":
            # Not a credential question at all, so `--status` would send them
            # to inspect the one thing that was definitely fine. The session is
            # partially captured and finishing it is a different command.
            tail = ("Nothing is broken — later flushes continue it; run "
                    "/memhub:import-session to finish that session now.")
        elif reason == "unconfirmed_provenance":
            tail = "A later capture hook will retry the URL automatically."
        else:
            tail = ("It may have recovered since; "
                    "run /memhub:login --status to check.")
        return (f"MemHub capture last failed {when_txt} — {detail}. {tail}")

    # Last, and only when capture itself is healthy: capture failing is the
    # bigger loss, and two warnings in one banner get read as none.
    if rulebook:
        what, when = rulebook
        ago = max(0, int((time.time() - when) / 60))
        when_txt = f"{ago}m ago" if ago < 120 else f"{ago // 60}h ago"
        if what == "auth":
            return ("Your team's rules are not reaching this session — the "
                    f"rulebook was refused {when_txt} because the plugin has no "
                    "access key of its own. Any rules already cached keep "
                    f"showing, but new or retired ones will not. {fix}")
        if what == "fetch":
            return ("Your team's rules are not refreshing — the last check "
                    f"failed {when_txt}, so this session is using a cached copy. "
                    "Rules activated or retired since then are not reflected. "
                    "Run /memhub:login --status to check.")
        if what == "flush":
            return ("Your team's rules are showing normally, but which ones "
                    f"fired is not reaching the server (last attempt {when_txt}), "
                    "so the team cannot see whether they are useful. "
                    "Run /memhub:login --status to check.")
        if what == "recall":
            # NOT told to check login: this lane runs on a 1.5 s budget inside
            # PreToolUse and the overwhelming majority of its failures are a
            # slow round trip, not a credential — an auth refusal is caught
            # above by `_AUTH_REFUSAL` and reported as such. Sending someone to
            # re-authenticate over a timeout spends their trust proving the
            # advice was wrong. It also says what is and is not lost, because
            # the cached rules keep firing normally the whole time.
            return ("Your team's rules are showing, but the ones tied to "
                    f"specific files or commands went unanswered {when_txt}, so "
                    "a few may not have been raised. Nothing needs fixing if "
                    "the next lookup succeeds.")
        return ("A team-rule lookup failed "
                f"{when_txt}; advice tied to specific files or commands may be "
                "missing from this session. Run /memhub:login --status to check.")
    return None


def _already_warned(session_id: str, signature: str) -> bool:
    """True if this exact warning was already shown for this session.

    ``SessionStart`` fires again on resume and on /clear, and repeating the same
    banner within one session is how a useful warning becomes wallpaper. Keyed
    by signature too, so a DIFFERENT problem still gets through.

    Fails toward warning: an unreadable or unwritable marker means the user
    might see it twice, which is strictly better than never seeing it because
    a debounce file could not be written.
    """
    if not session_id:
        return False
    marker = STATE_DIR / f"{session_id}.health"
    try:
        if marker.read_text(encoding="utf-8").strip() == signature:
            return True
    except OSError:
        pass
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text(signature, encoding="utf-8")
    except OSError:
        pass
    return False


def main() -> int:
    # Capture switched off on purpose is not a fault. Checked first so the
    # opt-out is genuinely free and genuinely silent.
    if os.environ.get("MEMHUB_TURN_FLUSH", "").strip().lower() in {"0", "off", "false"}:
        return 0

    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        payload = {}
    session_id = str(payload.get("session_id") or "").strip()

    host = _env_host()
    if not host:
        return 0  # not running as an installed plugin — nothing to judge

    token_problem = _token_problem(host)
    failure = _recent_failure()
    rulebook = _rulebook_problem()
    message = _message(host, token_problem, failure, rulebook)
    if not message:
        return 0

    # Debounce on WHAT IS WRONG, not on the rendered sentence. The breadcrumb
    # message embeds a "12m ago", so the text changes between every SessionStart
    # — and SessionStart fires again on resume and /clear. The cause is what
    # should be shown once; only a genuinely DIFFERENT problem should interrupt
    # again.
    signature = (f"{host}|{token_problem or ''}|{failure[0] if failure else ''}"
                 f"|{rulebook[0] if rulebook else ''}")
    if _already_warned(session_id, signature):
        return 0

    print(json.dumps({
        # The channel that reaches the USER. Everything else this hook could
        # emit goes only to the model.
        "systemMessage": f"⚠️  {message}",
        # And to the agent, so "is my memory working?" is answerable without
        # re-deriving any of it.
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"MemHub capture health: {message}",
        },
    }), flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        # A health check must never be the reason a session fails to start.
        sys.exit(0)
