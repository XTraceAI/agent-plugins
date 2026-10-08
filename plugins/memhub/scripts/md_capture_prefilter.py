#!/usr/bin/env python3
"""Cheap gate for the markdown-capture Stop flush (stdlib only).

``md_capture_flush.py`` runs after EVERY assistant turn. On most turns it
finds nothing: it imports the MCP client, auth and room modules, then runs
``git status`` in every repo the session worked in, all to learn that no
markdown changed. This script runs first (the ``prefilter`` of the
``Stop md_capture_flush`` route in ``hook_entry.py``) and exits non-zero to
skip that work when the flush provably — or, for the one approximation named
below, very nearly provably — has nothing to do. Same two-stage shape as
``turn_flush_prefilter.py``.

The flush has two sources of work, and the gate answers for both:

* ``dirty`` — paths the Edit/Write collector recorded. Any entry → run.
* the git sweep — ``.md`` files modified or untracked in git and newer than
  the session's start stamp (``since``), which is how a file written through
  Bash is found. The flush sweeps only when ``since`` is set.

So it skips when:

* the payload has no usable ``session_id`` (the flush returns at once), or
* there is no readable state for the session, or it has no ``since`` and an
  empty ``dirty`` — the flush would return without sweeping. Exact; or
* ``dirty`` is empty and the LAST sweep was idle (``state["sweep"]``, written
  by the flush: every ``git status`` succeeded and every path it saw reached
  a final outcome), it ran for this Stop's ``cwd``, no tool call the
  collector watches (Bash, Edit, MultiEdit, Write) has run since it started
  — the collector touches ``memhub-md-capture-<sid>.activity`` on every one
  — and it is less than ``IDLE_TTL_S`` old.

The approximation is in that last case: a file changed by something the
collector does not watch (the human's editor, a background job, another
tool) after an idle sweep is picked up by the next Stop that follows a
watched tool call, or the first one after ``IDLE_TTL_S``, rather than by the
very next Stop. Nothing is dropped unless the session ends inside that
window — and the sweep was only ever a best-effort net for such files.

**Fails OPEN.** A missing activity marker, a malformed ``sweep`` record, or any
unexpected error runs the flush: a bug here costs a wasted process, never a
lost capture.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import md_capture  # noqa: E402

# How long an idle sweep stands in for a fresh one, bounding how late a file
# changed outside the watched tools can be swept.
IDLE_TTL_S = 300
# mtime granularity: HFS+ and some network filesystems store whole seconds, so
# a call in the same second as the sweep's start must count as after it.
MTIME_SLACK_S = 2.0

RUN, SKIP = 0, 1


def decide(payload: object, now: float | None = None) -> int:
    if not isinstance(payload, dict):
        return SKIP          # the flush's main() would read no session_id
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return SKIP
    session_id = session_id.strip()
    path = md_capture.state_path(session_id)
    if path is None or not path.exists():
        return SKIP          # load_state → {"dirty": []}, no stamp, no sweep
    state = md_capture.load_state(session_id)
    if state.get("dirty"):
        return RUN
    if not isinstance(state.get("since"), (int, float)):
        return SKIP          # sweep_repos returns [] without a stamp
    sweep = state.get("sweep")
    if not isinstance(sweep, dict) or sweep.get("idle") is not True:
        return RUN
    at = sweep.get("at")
    if not isinstance(at, (int, float)):
        return RUN
    cwd = payload.get("cwd")
    if sweep.get("cwd") != (cwd if isinstance(cwd, str) else ""):
        return RUN           # a different sweep root than the idle pass had
    now = time.time() if now is None else now
    if not 0 <= now - at < IDLE_TTL_S:
        return RUN
    marker = md_capture.activity_path(session_id)
    try:
        touched = marker.stat().st_mtime if marker is not None else None
    except OSError:
        touched = None
    if touched is None or touched + MTIME_SLACK_S >= at:
        return RUN           # a watched tool call ran since that sweep began
    return SKIP


def main() -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        return SKIP
    try:
        payload = json.loads(raw)
    except ValueError:
        return SKIP          # the flush's main() catches the same and exits 0
    return decide(payload)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — fail open: a wasted flush, never a lost one
        sys.exit(RUN)
