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
"""
from __future__ import annotations

import json
import os
import subprocess
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


def _log(message: str) -> None:
    try:
        path = (Path.home() / ".config" / "memhub-plugin" /
                "cursorflush" / "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"[cursor-flush] compatibility fallback: {message}\n"
            )
    except OSError:
        pass


def _spawn_cursor_flush(raw: bytes, event: str) -> None:
    script = Path(__file__).resolve().with_name("cursor_flush.py")
    kwargs: dict[str, object] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(
            [sys.executable, str(script), event], **kwargs)
        if process.stdin is not None:
            try:
                process.stdin.write(raw)
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()
    except OSError as exc:
        _log(f"could not launch cursor_flush.py ({exc!r})")


def route(action: str, source_event: str, payload: object, raw: bytes,
          environ: Mapping[str, str] | None = None) -> bool:
    """Return True when the caller should continue its Claude handler."""
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
