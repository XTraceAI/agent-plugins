#!/usr/bin/env python3
"""Keep Claude plugin hooks safe when Cursor imports them for compatibility.

Cursor can load an installed Claude Code plugin alongside its native Cursor
plugin. Its compatibility hooks receive Cursor-shaped payloads, even though
they also contain Claude-compatible fields such as ``session_id`` and
``transcript_path``. Passing those payloads to the Claude transcript flushers
uploads the wrong record shape and creates a misleading Claude failure
breadcrumb.

Every command in ``hooks/claude-hooks.json`` calls this guard first. Claude
payloads return success so the original command continues unchanged. Cursor
payloads stop the Claude command; capture boundaries additionally launch the
native ``cursor_flush.py`` path as an idempotent fallback. Native and fallback
capture may both fire, but they share the same per-session lock and watermark.

The guard has a second, unrelated job: suppressing the plugin for sessions the
plugin itself started (``MEMHUB_HARNESS_CHILD``). Same mechanism, opposite
origin — Cursor is a foreign host wearing Claude's payload shape; a harness
child is this plugin's own headless ``claude -p``, which is a real Claude
session and would otherwise be captured as one.

Exit codes: 0 means "run the Claude handler", non-zero means "skip it". Every
hook command invokes the guard as an ``if`` condition, so a skip still leaves
the shell command itself exiting 0 — the hook never fails, it just does
nothing.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Mapping

_CURSOR_ENV_MARKERS = (
    "CURSOR_PLUGIN_ROOT",
    "CURSOR_VERSION",
    "CURSOR_TRANSCRIPT_PATH",
    "CURSOR_SESSION_ID",
)
# The harness extractor and the post-session review run headless `claude -p`
# INSIDE a repo (harness-tied-memory-spec §4.2/§4.3), so this plugin's own
# hooks fire for them: a replay's turns get captured into the repo brain, the
# rulebook routes on them, and the session shows up on the fleet board as an
# agent nobody started. It has happened. The extractor exports this variable
# for every child it spawns, and every hook event then does nothing at all.
_HARNESS_CHILD_ENV = "MEMHUB_HARNESS_CHILD"
_CURSOR_EVENTS = {
    "aftermcpexecution",
    "afterfileedit",
    "aftershellexecution",
    "beforemcpexecution",
    "beforeshellexecution",
    "beforesubmitprompt",
    "pretooluse",
    "posttooluse",
    "stop",
}


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_cursor(payload: object,
              environ: Mapping[str, str] | None = None) -> bool:
    """Return whether this is a Cursor hook invocation.

    Cursor indicators intentionally win even when ``CLAUDE_PLUGIN_ROOT`` or
    ``CLAUDE_PROJECT_DIR`` is present: those variables are expected when
    Cursor runs a third-party Claude plugin and therefore cannot identify the
    originating host.
    """
    env = os.environ if environ is None else environ
    if any(_nonempty_string(env.get(name)) for name in _CURSOR_ENV_MARKERS):
        return True
    if not isinstance(payload, dict):
        return False
    if _nonempty_string(payload.get("cursor_version")):
        return True

    transcript = payload.get("transcript_path")
    if _nonempty_string(transcript):
        normalized = str(transcript).replace("\\", "/")
        if "/.cursor/" in normalized:
            return True

    event = payload.get("hook_event_name")
    event_name = event.strip().lower() if isinstance(event, str) else ""
    identity_markers = sum((
        _nonempty_string(payload.get("conversation_id")),
        _nonempty_string(payload.get("generation_id")),
        isinstance(payload.get("workspace_roots"), list),
    ))
    return event_name in _CURSOR_EVENTS and identity_markers >= 2


def _cursor_event(source_event: str) -> str | None:
    # Claude's terminal boundaries both mean the Cursor turn is complete.
    if source_event.lower() in {"stop", "sessionend"}:
        return "stop"
    return None


def _spawn_cursor_flush(raw: bytes, event: str) -> None:
    """Keep a partial Cursor install from disabling unrelated Claude hooks."""
    try:
        from cursor_capture import spawn_cursor_flush
    except Exception as exc:
        try:
            path = (Path.home() / ".config" / "memhub-plugin" /
                    "cursorflush" / "log")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"[cursor-flush] guard import failed ({exc!r})\n"
                )
        except OSError:
            pass
        return
    spawn_cursor_flush(raw, event)


def is_harness_child(environ: Mapping[str, str] | None = None) -> bool:
    """Whether this hook fired inside a process the harness itself started."""
    env = os.environ if environ is None else environ
    return _nonempty_string(env.get(_HARNESS_CHILD_ENV))


def route(action: str, source_event: str, payload: object, raw: bytes,
          environ: Mapping[str, str] | None = None) -> bool:
    """Return True when the caller should continue its Claude handler."""
    # Checked before the Cursor branch and before any fallback launch: a
    # harness child must produce NO capture on any path, including the Cursor
    # fallback flush, which would otherwise write the replay under a second
    # host's shape. Nothing is captured, routed or rule-checked for it.
    if is_harness_child(environ):
        return False
    if not is_cursor(payload, environ):
        return True
    if action == "capture":
        event = _cursor_event(source_event)
        if event:
            _spawn_cursor_flush(raw, event)
    return False


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"ignore", "capture"}:
        # A malformed hook command must not bypass the guard and run a
        # host-specific handler against an unknown payload.
        return 1
    raw = sys.stdin.buffer.read()
    try:
        payload: object = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    # Returning non-zero is intentional: every hook command invokes us as an
    # ``if`` condition, so false skips the Claude body while the overall shell
    # command still exits successfully.
    return 0 if route(sys.argv[1], sys.argv[2], payload, raw) else 1


if __name__ == "__main__":
    raise SystemExit(main())
