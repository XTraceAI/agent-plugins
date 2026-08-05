#!/usr/bin/env python3
"""Cheap gate for the per-turn Stop-hook flush (stdlib only).

The Stop hook fires after EVERY assistant turn, and the real flush costs a
``uv run --with mcp`` spawn — measured at ~0.8s warm, ~1.4s cold, before any
network. Paying that on turns with nothing to send, or while a previous flush
is still in flight, would burn a laptop's battery for no memory. This script
runs under the system python3 (~0.02s, no uv, no deps) and exits non-zero to
skip the expensive stage — the same two-stage shape the commit/PR flush hook
uses (``flush_prefilter.py``).

Skips when:

* ``MEMHUB_TURN_FLUSH`` is set to ``0`` / ``off`` / ``false`` — the opt-out,
* the hook input has no usable ``session_id`` / ``transcript_path``,
* a flush for this session is already running (see below), or
* the transcript has not grown past the cursor — nothing new to ship.

**Fails OPEN.** Any unexpected error exits 0, so a bug here degrades to
"flush every turn" (correct, merely wasteful) rather than "never flush again"
(silent, total capture loss). The lock is re-acquired atomically by the flush
itself, so failing open cannot cause the overlap this gate exists to prevent.

Skipping is free: the cursor advances only after a flush SUCCEEDS, so whatever
this pass declines to send is simply carried by the next turn's flush.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "turnflush"

# A lock older than this is treated as abandoned. Must exceed the flush hook's
# own timeout, or a slow-but-live flush would be reclaimed underneath itself.
_LOCK_STALE_S = 360


def _lock_is_held(lock_path: Path) -> bool:
    """True when another flush for this session is genuinely still running.

    Checked two ways because either alone is wrong: a bare PID check leaks the
    lock forever if the pid was recycled by an unrelated process, and a bare
    age check reclaims a live-but-slow flush. Both must agree.
    """
    try:
        raw = json.loads(lock_path.read_text())
    except (OSError, ValueError):
        return False
    if time.time() - float(raw.get("at", 0)) > _LOCK_STALE_S:
        return False  # abandoned — the flush will reclaim it
    pid = raw.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)  # signal 0 = liveness probe, sends nothing
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by another user
    return True


def main() -> int:
    # Opt-out. This hook runs on every turn of every session, so there has to
    # be a way to stop it that is not "edit the installed plugin". Checked
    # first, before any file work, so disabling it is genuinely free.
    if os.environ.get("MEMHUB_TURN_FLUSH", "").strip().lower() in {"0", "off", "false"}:
        return 1

    raw = sys.stdin.read()
    if not raw.strip():
        return 1
    payload = json.loads(raw)
    session_id = (payload.get("session_id") or "").strip()
    transcript = (payload.get("transcript_path") or "").strip()
    if not session_id or not transcript:
        return 1

    try:
        size = os.path.getsize(transcript)
    except OSError:
        return 1  # not written yet, or gone — nothing to ship
    if size <= 0:
        return 1

    if _lock_is_held(STATE_DIR / f"{session_id}.lock"):
        return 1

    try:
        state = json.loads((STATE_DIR / f"{session_id}.json").read_text())
        offset = int(state.get("offset", 0))
    except (OSError, ValueError, TypeError):
        return 0  # no cursor yet — first turn of the session, always flush

    # Grew → new turns to ship. Shrank → the transcript was rewritten and the
    # offset is meaningless, so the flush must re-send from the top; either way
    # this is not the "nothing changed" case.
    return 0 if size != offset else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail open: better to spawn a pointless flush than to silently stop
        # capturing. Deliberately bare — this must never surface to the user.
        sys.exit(0)
