#!/usr/bin/env python3
"""Cross-platform instant-ack launcher for Cursor capture hooks.

Cursor runs plugin hooks through the host shell: POSIX shells on macOS/Linux
and PowerShell on native Windows. A polyglot shell/batch shim selects a bare
Python interpreter; this module owns the staged payload and detached flusher.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


_STAGED_NAME_RE = re.compile(r"\.memhub-cursor-hook-[A-Za-z0-9_-]*\.json")
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


def _read_staged_payload(path_arg: str, home: Path | None = None) -> bytes | None:
    """Read one manifest-staged payload without following an arbitrary path."""
    path = Path(path_arg)
    home = (home or Path.home()).resolve()
    should_unlink = False
    try:
        resolved = path.resolve(strict=True)
        if (os.path.normcase(str(resolved.parent)) !=
                os.path.normcase(str(home))):
            return None
        if not _STAGED_NAME_RE.fullmatch(resolved.name) or path.is_symlink():
            return None
        should_unlink = True
        # POSIX tee honors the user's umask and may create 0644. Tighten both
        # short-lived copies before reading; Windows keeps its profile ACLs.
        for staged_path in (path, path.with_suffix(".out")):
            try:
                os.chmod(staged_path, 0o600)
            except OSError:
                pass
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            raw = handle.read(_MAX_PAYLOAD_BYTES + 1)
        if len(raw) > _MAX_PAYLOAD_BYTES:
            _log("staged hook payload exceeds 4 MiB — capture skipped")
            return None
    except OSError as exc:
        _log(f"could not read staged hook payload ({exc!r})")
        return None
    finally:
        if should_unlink:
            for staged_path in (path, path.with_suffix(".out")):
                try:
                    staged_path.unlink()
                except OSError:
                    pass

    try:
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw.decode("utf-16").encode("utf-8")
        return raw.decode("utf-8-sig").encode("utf-8")
    except UnicodeError:
        _log("staged hook payload is not UTF-8/UTF-16 — capture skipped")
        return None


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        # Direct invocations carry JSON on stdin; manifest invocations have
        # already consumed it through tee and provide the completed file.
        piped = sys.stdin.buffer.read()
        staged = (_read_staged_payload(sys.argv[2])
                  if len(sys.argv) > 2 else None)
        spawn_cursor_flush(staged if staged is not None else piped, event)
    except Exception as exc:
        # Capture observes; it must never gate the user's prompt or command.
        _log(f"could not stage hook payload ({exc!r})")
    print(json.dumps({"permission": "allow"}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
