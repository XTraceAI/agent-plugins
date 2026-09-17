#!/usr/bin/env python3
"""Cross-platform instant-ack launcher for Cursor capture hooks.

Cursor runs plugin hooks through the host shell: POSIX shells on macOS/Linux
and PowerShell on native Windows. A polyglot shell/batch shim selects a bare
Python interpreter; this module normalizes hook stdin and detaches the flusher.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024


def _log(message: str) -> None:
    try:
        path = (Path.home() / ".config" / "memhub-plugin" /
                "cursorflush" / "log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"[cursor-flush] launcher: {message}\n"
            )
    except OSError:
        pass


def _detached_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            # Cursor owns hook processes with a Windows job object. Without
            # breakaway, it can reap the detached flusher with the hook.
            | getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        )
    else:
        kwargs["start_new_session"] = True
    return kwargs


def spawn_cursor_flush(raw: bytes, event: str) -> None:
    """Launch a flusher from an in-memory payload without blocking on PIPE."""
    script = Path(__file__).resolve().with_name("cursor_flush.py")
    kwargs = _detached_kwargs()

    try:
        # Popen duplicates this seeked handle into the child before returning.
        # Closing the parent's handle is therefore safe on POSIX and Windows,
        # and avoids PIPE backpressure for large hook payloads.
        with tempfile.TemporaryFile() as payload_file:
            payload_file.write(raw)
            payload_file.seek(0)
            subprocess.Popen(
                [sys.executable, str(script), event],
                stdin=payload_file, **kwargs)
    except OSError as exc:
        _log(f"could not launch cursor_flush.py ({exc!r})")


def _normalize_payload(raw: bytes) -> bytes | None:
    """Return canonical UTF-8 JSON for the detached child's secure stdin."""
    if len(raw) > _MAX_PAYLOAD_BYTES:
        _log("hook payload exceeds 4 MiB - capture skipped")
        return None

    def valid_json(encoding: str) -> bytes | None:
        try:
            text = raw.decode(encoding)
            json.loads(text)
            return text.encode("utf-8")
        except (UnicodeError, json.JSONDecodeError):
            return None

    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        decoded = valid_json("utf-16")
        if decoded is not None:
            return decoded
    else:
        # Prefer valid UTF-8 before consulting a byte-pattern heuristic.
        decoded = valid_json("utf-8-sig")
        if decoded is not None:
            return decoded

        # Windows PowerShell can produce BOM-less UTF-16. JSON's ASCII
        # punctuation makes that encoding visible through its aligned NULs;
        # valid_json prevents coincidental byte patterns from being accepted.
        if len(raw) >= 4 and len(raw) % 2 == 0:
            even = raw[0::2]
            odd = raw[1::2]
            if (odd.count(0) * 4 >= len(odd) * 3 and
                    even.count(0) * 4 <= len(even)):
                decoded = valid_json("utf-16le")
                if decoded is not None:
                    return decoded
            if (even.count(0) * 4 >= len(even) * 3 and
                    odd.count(0) * 4 <= len(odd)):
                decoded = valid_json("utf-16be")
                if decoded is not None:
                    return decoded

    _log("hook payload is not valid UTF-8/UTF-16 JSON - capture skipped")
    return None


def upgrade_context(payload: bytes) -> str | None:
    data = json.loads(payload)
    roots = data.get("workspace_roots") or []
    hook = {"cwd": data.get("cwd") or (roots[0] if roots else os.getcwd()),
            "session_id": data.get("conversation_id", ""), "tool_name": "Bash"}
    result = subprocess.run(
        # `conversation_id` is passed through bare above, while cursor_flush
        # uploads the session as `cursor-<uuid>` — the mismatch that leaves
        # every Codex fire unlinked (ENG-1075) would be Cursor's too.
        #
        # Inert today, and deliberately so: the `upgrade` lane returns before
        # rulebook_hook builds its ctx, so --host is not read here and Cursor
        # has no rule-evaluation lane to log a fire from. It is passed because
        # this is the one place Cursor names itself to the hook; the namespace
        # itself is enforced in rulebook_hook.wire_session_id, which already
        # handles `cursor`.
        [sys.executable, str(Path(__file__).with_name("rulebook_hook.py")),
         "upgrade", "--host", "cursor"],
        input=json.dumps(hook).encode(), capture_output=True, timeout=2, check=False)
    if result.stdout:
        return json.loads(result.stdout).get("hookSpecificOutput", {}).get("additionalContext")
    return None


def health_context(payload: bytes) -> str | None:
    """Capture-health for the Cursor lane, at most once per session.

    Cursor wires no session-start event, so there is nowhere to run the health
    check the way Claude and Codex do — which is why a Cursor capture failure
    previously reached the user through no channel at all. This rides the
    message `beforeShellExecution` already injects rather than adding a hook
    event (a manifest change costs every user a re-trust).

    Once per session: the marker bounds it to one subprocess per session
    instead of one per shell call, and capture_health's own staleness window
    and retraction rules still decide whether there is anything to say.
    """
    data = json.loads(payload)
    session = str(data.get("conversation_id") or "").strip()
    if not session:
        return None
    state_dir = Path.home() / ".config" / "memhub-plugin" / "cursorflush"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session)[:80]
    marker = state_dir / f"{safe}.health"
    try:
        if marker.exists():
            return None
        state_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
    except OSError:
        return None          # never let a marker failure cost the health line
    roots = data.get("workspace_roots") or []
    hook = {"cwd": data.get("cwd") or (roots[0] if roots else os.getcwd()),
            "session_id": session}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("capture_health.py")),
         "--host", "cursor"],
        input=json.dumps(hook).encode(), capture_output=True, timeout=3, check=False)
    if not result.stdout:
        return None
    return json.loads(result.stdout).get(
        "hookSpecificOutput", {}).get("additionalContext")


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    output = {"permission": "allow"}
    try:
        raw = sys.stdin.buffer.read(_MAX_PAYLOAD_BYTES + 1)
        payload = _normalize_payload(raw)
        if payload is not None:
            spawn_cursor_flush(payload, event)
            if event == "beforeShellExecution":
                parts = []
                for lane in (upgrade_context, health_context):
                    try:
                        text = lane(payload)
                    except Exception as exc:   # one lane must not cost the other
                        _log(f"{lane.__name__} failed ({exc!r})")
                        text = None
                    if text:
                        parts.append(text)
                context = "\n\n".join(parts)
                if context:
                    output.update(agent_message=context, user_message=context)
    except Exception as exc:
        # Capture observes; it must never gate the user's prompt or command.
        _log(f"could not launch hook capture ({exc!r})")
    print(json.dumps(output), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
