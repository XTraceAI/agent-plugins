#!/usr/bin/env python3
"""Tell the user when memory capture has stopped working (stdlib only).

**Why this exists.** The hooks that capture sessions — the per-turn ``Stop``
flush and the ``SessionEnd`` backstop — are declared ``async: true``, and Claude
Code surfaces an async hook's stdout NOWHERE: not to the user, not to the agent.
Those scripts are also written to never fail loudly, because a memory hook must
never disturb a coding session. Correct in isolation, the two rules compose into
a system that cannot report its own death: every failure path ended in a
``print`` nobody could read and an ``exit 0``.

That is not hypothetical. A prod OAuth token cached before the server advertised
``offline_access`` came back with no refresh token, so it could never be renewed.
Twenty-four hours later it expired, and every subsequent flush raised
``NonInteractiveAuthRequired``, logged politely into the void, and exited 0.
Per-turn capture was dead for a full day across every session in the affected
repo. The MCP connector kept working the whole time — it holds its own token,
in Claude Code's store rather than this plugin's — so the product looked alive
while nothing was being saved. Nothing anywhere said a word.

**The fix.** ``flush_turn.py`` now writes WHY it failed into its per-session
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

Run the self-test:  python3 capture_health_test.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

CACHE_DIR = Path.home() / ".config" / "memhub-plugin"
STATE_DIR = CACHE_DIR / "turnflush"

# How far back a recorded failure still counts as news. A breadcrumb from last
# week describes a problem that has probably already been fixed (or a machine
# that has moved on), and reporting it would train the user to ignore this.
_STALE_AFTER_S = 24 * 3600

# Ceiling on the state files examined, newest first. The dir grows by one file
# per session forever, and this runs in the user's startup path; an unbounded
# scan would get slower every day for information the newest few already carry.
_MAX_STATE_FILES = 40

# Failures worth interrupting someone over, and what to say. Only ``auth`` is
# truly terminal — no retry can mint a token, so capture stays dead until a
# human re-authenticates. The rest are reported because they persisted, not
# because a single occurrence means anything.
_REASONS = {
    "auth": "the plugin's saved login expired and could not be renewed",
    "server_rejected": "the server rejected the last upload",
    "server_too_old": "the server is too old for per-turn capture",
    "timeout": "the server stopped responding",
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


def _token_problem(host: str) -> str | None:
    """The reason capture cannot authenticate to ``host``, or None if it can.

    Three states matter, and only two of them are faults:

    * no cache file — never authenticated on this machine, or it was cleared;
    * expired WITH a refresh token — fine, and deliberately silent: the flush
      renews it before the SDK runs, which is the normal overnight path;
    * expired WITHOUT one — terminal. Nothing automatic can recover it, which
      is exactly the case that went unnoticed for a day.

    A token whose ``exp`` cannot be read is treated as healthy: refusing to
    guess beats warning about a working setup, and a genuine failure will show
    up as a breadcrumb anyway.
    """
    if os.environ.get("MEMHUB_TOKEN", "").strip():
        return None  # explicit bearer wins; nothing here applies
    try:
        cached = json.loads(
            (CACHE_DIR / f"tokens-{host.replace(':', '_')}.json")
            .read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "never"
    exp = _jwt_exp(cached.get("access_token") or "")
    if exp is None or time.time() < exp:
        return None
    return None if cached.get("refresh_token") else "unrenewable"


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
        # A later success in the SAME session retracts the failure. The flush
        # clears both fields together, but a state file written by an older
        # build carries the error with no clearing logic behind it.
        ok_at = state.get("last_ok_at")
        if isinstance(ok_at, (int, float)) and ok_at >= when:
            continue
        return str(reason), float(when)
    return None


def _message(host: str, token_problem: str | None,
             failure: tuple[str, float] | None) -> str | None:
    """The warning to show, or None when there is nothing worth saying.

    The token check leads when it fires, because it names a cause the user can
    act on and it is true right now — a breadcrumb only proves something was
    broken when it was written.
    """
    fix = ("Run /memhub:import-session once to re-authenticate "
           "(it opens the same browser flow as /mcp).")
    if token_problem == "never":
        return (f"MemHub capture is not authenticated for {host}, so this "
                f"session is not being saved to memory. {fix}")
    if token_problem == "unrenewable":
        return (f"MemHub capture is broken: the saved login for {host} expired "
                f"and cannot be renewed automatically, so nothing from this "
                f"session is reaching memory. {fix}")
    if failure:
        reason, when = failure
        detail = _REASONS.get(reason, "the capture hook failed")
        ago = max(0, int((time.time() - when) / 60))
        when_txt = f"{ago}m ago" if ago < 120 else f"{ago // 60}h ago"
        tail = fix if reason == "auth" else (
            "It may have recovered since; check /mcp if this repeats.")
        return (f"MemHub capture last failed {when_txt} — {detail}. {tail}")
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

    message = _message(host, _token_problem(host), _recent_failure())
    if not message:
        return 0

    if _already_warned(session_id, message):
        return 0

    print(json.dumps({
        # The channel that reaches the USER. Everything else this hook could
        # emit goes only to the model, which is how the original bug hid.
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
