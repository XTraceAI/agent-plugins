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
* the flush already found this server cannot buffer per turn (dormant),
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
from pathlib import Path

# The locking shim is a sibling module, not a package: pin this script's own
# directory first so the import holds however this file is loaded (hooks run
# it as a script, where sys.path[0] already covers it, but tests and embeds
# do not).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import portable_lock  # noqa: E402
import capture_context  # noqa: E402
import sinks  # noqa: E402

STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "turnflush"


def _lock_is_held(lock_path: Path) -> bool:
    """True when another flush for this session is still running.

    Probes the same ``flock`` the flush takes: acquire non-blocking, and if that
    succeeds nobody held it — release immediately and say so. No pid liveness or
    age heuristics, because the kernel already releases the lock when a flush
    dies, however it dies. A missing file means no flush has ever run.

    The gap between releasing this probe and the flush taking the lock for real
    is harmless: the flush re-acquires atomically and skips if it loses.
    """
    if not lock_path.exists():
        return False
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        portable_lock.lock_exclusive(fd, blocking=False)
    except OSError:
        return True  # someone is holding it
    finally:
        os.close(fd)  # also drops the lock if we took it
    return False


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
    if not capture_context.valid_session_id(session_id) or not transcript:
        return 1

    try:
        size = os.path.getsize(transcript)
    except OSError:
        return 1  # not written yet, or gone — nothing to ship
    if size <= 0:
        return 1

    selected = sinks.resolve_capture_sinks()
    key = capture_context.session_file_key(session_id)
    for sink in selected:
        state_dir = capture_context.state_directory(STATE_DIR, sink)
        if _lock_is_held(state_dir / f"{key}.lock"):
            continue
        try:
            state = json.loads((state_dir / f"{key}.json").read_text(encoding="utf-8"))
            offset = int(state.get("offset", 0))
        except (OSError, ValueError, TypeError):
            return 0
        if (len(selected) > 1 or not state.get("unsupported")) and size != offset:
            return 0
    return 1  # Every active destination is caught up, dormant or already flushing.


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Fail open: better to spawn a pointless flush than to silently stop
        # capturing. Deliberately bare — this must never surface to the user.
        sys.exit(0)
