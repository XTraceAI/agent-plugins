#!/usr/bin/env python3
"""Cross-platform instant-ack launcher for Cursor capture hooks.

Cursor runs plugin hooks through the host shell: POSIX shells on macOS/Linux
and PowerShell on native Windows. A polyglot shell/batch shim selects a bare
Python interpreter; this module normalizes hook stdin and detaches the flusher.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cursor_rulebook  # noqa: E402 — beside this file


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


def spawn_rulebook_flush(payload: bytes, event: str) -> None:
    """Ship the rulebook's fires at a turn's end, detached like the flusher."""
    try:
        planned = cursor_rulebook.flush_call(event, json.loads(payload))
        if planned is None:
            return
        args, call = planned
        script = Path(__file__).resolve().with_name("rulebook_hook.py")
        with tempfile.TemporaryFile() as payload_file:
            payload_file.write(json.dumps(call).encode("utf-8"))
            payload_file.seek(0)
            subprocess.Popen([sys.executable, str(script), *args],
                             stdin=payload_file, **_detached_kwargs())
    except (OSError, ValueError) as exc:
        _log(f"could not launch rulebook flush ({exc!r})")


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
        [sys.executable, str(Path(__file__).with_name("rulebook_hook.py")), "upgrade"],
        input=json.dumps(hook).encode(), capture_output=True, timeout=2, check=False)
    if result.stdout:
        return json.loads(result.stdout).get("hookSpecificOutput", {}).get("additionalContext")
    return None


def capture_context(payload: bytes) -> str | None:
    data = json.loads(payload)
    hook = {"session_id": data.get("conversation_id", "")}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("capture_health.py")),
         "--host", "cursor", "--plugin-root", str(Path(__file__).resolve().parent.parent)],
        input=json.dumps(hook).encode(), capture_output=True, timeout=2, check=False)
    if result.stdout:
        return json.loads(result.stdout).get("systemMessage")
    return None


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    # sessionStart and postToolUse take no permission; the rest are asked one.
    output = {} if event in ("sessionStart", "postToolUse") else {"permission": "allow"}
    try:
        raw = sys.stdin.buffer.read(_MAX_PAYLOAD_BYTES + 1)
        payload = _normalize_payload(raw)
        if payload is not None:
            if event in cursor_rulebook.NOTE_EVENTS + ("beforeReadFile",):
                # Rulebook-only events: capture never read them.
                try:
                    cursor_rulebook.note(event, json.loads(payload))
                except Exception as exc:
                    _log(f"rulebook note failed ({exc!r})")
            else:
                spawn_cursor_flush(payload, event)
            if event in cursor_rulebook.FLUSH_EVENTS:
                spawn_rulebook_flush(payload, event)
                cursor_rulebook.note(event, json.loads(payload))
            ruled = None
            if event in cursor_rulebook.GATE_EVENTS:
                try:
                    ruled = cursor_rulebook.gate(event, json.loads(payload))
                except Exception as exc:
                    _log(f"rulebook gate failed ({exc!r})")
            if ruled and ruled.get("permission") == "deny":
                print(json.dumps(ruled), flush=True)
                return 0
            if ruled:
                output.update(ruled)
            contexts = [ruled["agent_message"]] if ruled and ruled.get("agent_message") else []
            if event == "beforeSubmitPrompt":
                from plugin_compatibility import startup_message
                try:
                    context = startup_message(host="cursor", session=json.loads(payload).get("conversation_id"))
                    if context:
                        contexts.append(context)
                except Exception as exc:
                    _log(f"compatibility check failed ({exc!r})")
            readers = ((capture_context,) if event == "beforeSubmitPrompt" else
                       (upgrade_context,) if event == "beforeShellExecution" else ())
            for read_context in readers:
                try:
                    text = read_context(payload)
                    if text:
                        contexts.append(text)
                except Exception as exc:
                    _log(f"context check failed ({exc!r})")
            context = "\n\n".join(contexts)
            if context:
                output.update(agent_message=context,
                              user_message=output.get("user_message") or context)

    except Exception as exc:
        # Capture observes; only a team rule's deny above may gate a call.
        _log(f"could not launch hook capture ({exc!r})")
    print(json.dumps(output), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
