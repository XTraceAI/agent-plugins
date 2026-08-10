#!/usr/bin/env python3
"""Orient a session on the repo's agent brain — the default read/write target.

**Why this exists.** The plugin already routes every WRITE to the repo's brain:
``flush_turn``, ``flush_session`` and ``save_artifact`` all resolve the room from
``rooms.json`` before they send anything. Nothing ever said so. The agent could
not name the brain it was filling, and the user could not either, so the one
question that matters at the start of a session — *what does this project
already know?* — had no cheap answer and usually went unasked.

Worse, the two halves disagreed. Writes defaulted to the brain while reads
defaulted to personal memory (``search_memory`` omits ``agent_brain_id`` unless
the user names a brain), so the plugin filled a brain it then did not read.

This hook closes the loop from the session's side: it names the brain and hands
the agent the compiled overview — the brain's own description of the repo — as
context, before the first prompt.

**Two subcommands, because they have opposite constraints.**

``brief`` runs on ``SessionStart``, which is SYNCHRONOUS and blocks the user's
first prompt. It is stdlib-only and makes NO network call: everything it prints
comes from ``rooms.json`` and a local cache, so the cost is a couple of file
reads. ``capture_health.py`` sets that bar for this event and it is the right
one — every millisecond here is one the user waits.

``refresh`` runs on ``Stop``, which is async and already does network work. It
fetches ``get_brain_overview`` and writes the cache ``brief`` reads. Throttled,
because the overview is a digest that changes on the order of days, not turns.

**On speaking to the user.** ``capture_health`` argues, correctly, that a hook
which speaks every session is one the user learns to scroll past. So the
``systemMessage`` — the channel that reaches the USER — fires only when the
resolved brain CHANGES (first resolution included). The agent-facing
``additionalContext`` is emitted every session: it costs the user nothing, and
the agent knowing where memory lives is the entire point.

Run the self-test:  python3 tests/brain_brief_test.py  (from the repo root;
tests live outside the plugin so they are not shipped to installs)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import room_map  # noqa: E402

#: Where the compiled overview is cached, keyed by backend AND brain so a prod
#: and a staging brain — different databases, non-interchangeable ids — can
#: never serve each other's digest.
CACHE_DIR = Path(
    os.environ.get("MEMHUB_STATE_DIR")
    or Path.home() / ".config" / "memhub-plugin"
) / "overview"

#: How stale a cached overview may be before ``refresh`` refetches it. The
#: overview is a digest of a whole brain; it moves on the order of days. Making
#: this small would put a network round trip on a hook that runs every turn to
#: re-fetch text that did not change.
_MAX_AGE_S = 6 * 3600

#: Overview text is injected into every session's context. A brain with a long
#: digest should not silently become a per-session token tax, so it is clipped
#: with a pointer to the tool that returns the whole thing.
_MAX_OVERVIEW_CHARS = 1800

_TIMEOUT_S = 20.0


def _cache_path(env: str, brain_id: str) -> Path:
    return CACHE_DIR / f"{env}-{brain_id}.json"


def _announced_path() -> Path:
    """Which brain we last told the USER about, per repo+backend.

    Separate from the overview cache: that is keyed by brain, this is keyed by
    the room, and the whole question it answers is "did the brain change?".
    """
    return CACHE_DIR / "announced.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> bool:
    """Best effort; ``True`` when the bytes actually landed.

    A cache that cannot be written is a slower session, never a broken one, so
    the failure is swallowed — but it is REPORTED, because a refresh that cannot
    persist its result is worth skipping entirely rather than repeating.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception:  # noqa: BLE001
        return False


def _cache_is_writable() -> bool:
    """Can we persist a refresh at all?

    ``refresh`` is throttled by the mtime of the file it writes, so a cache
    directory that cannot be written degrades into "always stale" — and Stop
    fires every turn, which would turn the 6-hourly digest fetch into a network
    call on EVERY turn, forever. Since ``brief`` reads only from the cache, a
    fetch whose result cannot be stored buys nothing at all, so the honest
    response to an unwritable cache is to not make the call.
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        probe = CACHE_DIR / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


def _capture_is_off() -> bool:
    """Per-turn capture disabled on purpose (``MEMHUB_TURN_FLUSH=0``).

    Worth knowing before claiming writes land in the brain: with the flush off
    they do not, and a brief that says otherwise is simply wrong.
    """
    return (os.environ.get("MEMHUB_TURN_FLUSH") or "").strip() == "0"


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_OVERVIEW_CHARS:
        return text
    return text[:_MAX_OVERVIEW_CHARS].rstrip() + (
        "\n… (truncated — call get_brain_overview for the full digest)"
    )


def _cwd_from(payload: dict) -> str:
    """The session's directory, which decides WHICH repo's room we resolve.

    Falls back to the process cwd: on SessionStart the hook runs in the session's
    directory anyway, so the fallback names the same repo rather than a wrong one.
    """
    cwd = str(payload.get("cwd") or "").strip()
    return cwd or os.getcwd()


# ── brief: SessionStart, stdlib only, no network ───────────────────────────

def cmd_brief(payload: dict) -> int:
    cwd = _cwd_from(payload)
    room = room_map.read_room(cwd)
    if not room:
        # No room cached for this repo+backend. Deliberately silent: an
        # unonboarded repo is the common case in any checkout that is not the
        # user's own, and a hook that nags there is a hook they turn off.
        return 0

    env = room_map.current_env()
    brain_id = room["brain_id"]
    name = room.get("name") or "this repo's brain"

    cached = _read_json(_cache_path(env, brain_id))
    overview = _clip(str(cached.get("overview") or ""))

    writes = (
        "Per-turn capture is OFF (MEMHUB_TURN_FLUSH=0), so nothing is being "
        "written there this session."
        if _capture_is_off() else
        "Sessions in this repo are captured into it automatically."
    )
    context = [
        f"MemHub: this repo's agent brain is **{name}** (`{brain_id}`, {env}).",
        writes,
        "It is the DEFAULT target for memory in this repo — pass it as "
        "`agent_brain_id` when you search, save an artifact, or record a "
        "decision, so what you write is findable where the next session will "
        "look. Another brain is still reachable by naming it explicitly.",
    ]
    if overview:
        context.append(
            f"\nWhat this brain already knows about the repo:\n{overview}"
        )
    else:
        context.append(
            "\nNo compiled overview cached yet — call `get_brain_overview` "
            "with that id if you need to know what it already holds."
        )

    out: dict = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(context),
        }
    }

    # Speak to the USER only when the brain changed. Every session would be
    # noise, and noise is how capture_health's warning would get scrolled past.
    announced = _read_json(_announced_path())
    key = f"{room.get('name') or cwd}|{env}"
    if announced.get(key) != brain_id:
        out["systemMessage"] = (
            f"🧠 MemHub: memory in this repo defaults to {name} "
            f"({brain_id[:8]}…, {env}) — reads and writes land there unless "
            "you name another brain."
        )
        announced[key] = brain_id
        _write_json(_announced_path(), announced)

    print(json.dumps(out), flush=True)
    return 0


# ── refresh: Stop hook, async, network, throttled ──────────────────────────

def _extract_overview(res) -> str:
    """The digest TEXT out of a ``get_brain_overview`` result.

    Three shapes, in order of preference, because the server does not promise
    one: ``structuredContent`` when present, then a text block that is really a
    JSON envelope (``{"agent_brain_id": …, "overview": "# …"}`` — what staging
    actually returns), then a text block that is the digest itself.

    The envelope case is the one worth naming: taking the block verbatim caches
    ~3.5KB of JSON punctuation and field names and injects THAT into every
    session as "what this brain knows". It looks like it works — a cache file
    appears and it is full of plausible text — which is why this is parsed
    rather than trusted.
    """
    structured = getattr(res, "structured", None)
    if isinstance(structured, dict):
        text = structured.get("overview") or structured.get("text")
        if isinstance(text, str) and text.strip():
            return text

    for block in getattr(res, "content", None) or []:
        raw = getattr(block, "text", None)
        if not raw:
            continue
        raw = str(raw)
        try:
            envelope = json.loads(raw)
        except Exception:  # noqa: BLE001
            return raw
        if isinstance(envelope, dict):
            text = envelope.get("overview") or envelope.get("text")
            # An uncompiled digest is a literal null here; "" reads as a cache
            # miss downstream, which is the honest answer.
            return text if isinstance(text, str) else ""
        return raw
    return ""


def _is_fresh(path: Path) -> bool:
    cached = _read_json(path)
    try:
        return (time.time() - float(cached.get("refreshed_at") or 0)) < _MAX_AGE_S
    except Exception:  # noqa: BLE001
        return False


def cmd_refresh(payload: dict) -> int:
    cwd = _cwd_from(payload)
    room = room_map.read_room(cwd)
    if not room:
        return 0
    env = room_map.current_env()
    brain_id = room["brain_id"]
    path = _cache_path(env, brain_id)
    if _is_fresh(path):
        return 0
    # Checked BEFORE the network call, not after: the throttle is the cache's
    # own mtime, so an unwritable cache is permanently stale and would refetch
    # every single turn.
    if not _cache_is_writable():
        return 0

    # Imported here, not at module scope: `brief` runs on the synchronous
    # SessionStart path and must not pay for auth/transport modules it never
    # uses.
    import mcp_http
    from _memhub_auth import resolve_bearer

    try:
        url, bearer = resolve_bearer()
        if not bearer:
            return 0
        res = mcp_http.call_tool(
            url, bearer, "get_brain_overview",
            {"agent_brain_id": brain_id}, timeout=_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001
        # A digest we could not fetch is a session without the overview, which
        # `brief` already handles. Never let it disturb the turn.
        return 0

    if getattr(res, "is_error", False):
        return 0

    overview = _extract_overview(res)
    # A brain whose digest has not compiled yet returns null. Caching "" would
    # be indistinguishable from a cache miss, which is exactly right: `brief`
    # then tells the agent to fetch it rather than asserting the brain is empty.
    if not overview.strip():
        return 0

    _write_json(path, {
        "brain_id": brain_id,
        "env": env,
        "name": room.get("name") or "",
        "overview": overview,
        "refreshed_at": time.time(),
    })
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "brief"
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if cmd == "refresh":
        return cmd_refresh(payload)
    return cmd_brief(payload)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        # Orientation must never be the reason a session fails to start.
        sys.exit(0)
