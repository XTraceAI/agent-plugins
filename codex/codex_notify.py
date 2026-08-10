#!/usr/bin/env python3
"""Codex `notify` handler — best-effort auto-capture of Codex sessions.

Wire it up in ``~/.codex/config.toml``::

    notify = ["python3", "/absolute/path/to/codex/codex_notify.py"]

Codex invokes the notify program with a single JSON argument describing an
event. On a turn/session-completion event this fires
``import_codex_session.py --session latest`` **detached** (fire-and-forget), so
Codex is never blocked. Re-importing is incremental (the server watermark folds
the session gist forward instead of duplicating), and a debounce caps auto-
imports to one per ``_MIN_INTERVAL_S`` seconds so a burst of turns doesn't
re-send the whole rollout each time.

This is deliberately conservative: unknown event shapes, parse failures, and
missing files all exit 0 silently. Auto-capture is a convenience; the manual
`import_codex_session.py` is always the reliable path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Events that mean "a turn finished, the rollout has new content worth banking".
_CAPTURE_EVENTS = {"agent-turn-complete", "turn-complete", "session-complete"}

# Debounce: Codex fires turn-complete after EVERY turn, and each import re-reads,
# re-transforms and re-sends the whole rollout (the server dedups, but the work
# is wasted). Cap auto-imports to one per this many seconds. Tradeoff: a session
# whose final turn lands inside the window is captured on the next fire or a
# manual import — nothing is lost permanently (re-import is incremental).
_MIN_INTERVAL_S = 120
_MARKER = Path.home() / ".config" / "memhub-plugin" / "codex-notify-last"


def _debounced() -> bool:
    try:
        return time.time() - _MARKER.stat().st_mtime < _MIN_INTERVAL_S
    except OSError:
        return False  # no marker yet (or unreadable) → not debounced


def _session_arg(event: dict) -> str:
    """The specific session to import — from the notify event when it names one,
    else ``latest``.

    ``--session latest`` picks the globally-newest rollout by mtime, which is the
    RIGHT session only when a single Codex session is active; with concurrent
    sessions it could import an unrelated one. Codex's notify payload shape
    varies by version, so try the plausible identifier fields (a rollout path or
    a session UUID) and fall back to ``latest``."""
    for k in ("rollout-path", "rollout_path", "session-id", "session_id",
              "conversation-id", "conversation_id", "thread-id", "thread_id"):
        v = event.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "latest"


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    try:
        event = json.loads(sys.argv[-1])
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(event, dict):
        return 0
    etype = event.get("type") or event.get("event")
    if etype not in _CAPTURE_EVENTS:
        return 0

    if _debounced():
        return 0  # a recent auto-import already covers the latest turns

    importer = Path(__file__).resolve().parent / "import_codex_session.py"
    if not importer.is_file():
        return 0

    # Detached, output discarded — never slow or crash the Codex session.
    try:
        subprocess.Popen(
            ["uv", "run", "--with", "mcp", "python", str(importer),
             "--session", _session_arg(event)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(importer.parent.parent),
            env={**os.environ},
        )
    except OSError:
        return 0  # launch failed — do NOT stamp, so the next turn retries

    # Stamp only after a successful launch, so a failed/never-started import
    # doesn't debounce (and drop) the next _MIN_INTERVAL_S of turns. Codex
    # turn-complete events are sequential, so there's no concurrent-launch race.
    try:
        _MARKER.parent.mkdir(parents=True, exist_ok=True)
        _MARKER.touch()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
