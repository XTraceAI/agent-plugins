#!/usr/bin/env python3
"""Stable user-hook trampoline for Codex releases without plugin hooks.

The setup skill copies this file to ``$CODEX_HOME/memhub_hook_bridge.py``.
User-level hooks can then survive plugin upgrades: this trampoline resolves the
newest installed MemHub version at invocation time and dispatches into it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_KNOWN_INSTALLS = (
    ("xtrace-plugins", "memhub"),
    ("memhub-internal", "memhub-staging"),
)
_VERSION_PART = re.compile(r"\d+|[A-Za-z]+")


def _version_key(path: Path) -> tuple:
    """Natural ordering for semver and Codex cachebuster directory names."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in _VERSION_PART.findall(path.name)
    )


def resolve_plugin_root() -> Path | None:
    override = os.environ.get("MEMHUB_PLUGIN_ROOT")
    if override:
        root = Path(override).expanduser()
        if (root / "scripts" / "codex_flush.py").is_file():
            return root

    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    cache = codex_home / "plugins" / "cache"
    for marketplace, plugin in _KNOWN_INSTALLS:
        versions = [
            path for path in (cache / marketplace / plugin).glob("*")
            if (path / "scripts" / "codex_flush.py").is_file()
        ]
        if versions:
            return max(versions, key=_version_key)
    return None


def _run(root: Path, script: str, payload: bytes, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / script), *args],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=7,
        check=False,
    )


def _relay(result: subprocess.CompletedProcess) -> None:
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
    if result.stdout:
        sys.stdout.buffer.write(result.stdout)


def _directive(root: Path, payload: bytes, reactive: bool) -> None:
    try:
        hook = json.loads(payload or b"{}")
    except (TypeError, ValueError):
        return

    if reactive:
        gate = _run(root, "reactive_prefilter.py", payload)
        if gate.returncode != 0:
            return
    elif hook.get("tool_name") in {"Bash", "shell", "local_shell"}:
        gate = _run(root, "directive_prefilter.py", payload)
        if gate.returncode != 0:
            return

    _relay(_run(root, "directive_recall.py", payload))


def _artifact_sync(root: Path, payload: bytes) -> None:
    # artifact_sync_reminder already emits Codex/Claude-compatible
    # hookSpecificOutput JSON. Relay it byte-for-byte; wrapping it again would
    # turn the JSON document itself into the model-visible reminder text.
    _relay(_run(root, "artifact_sync_reminder.py", payload))


def _detach_flush(root: Path, payload: bytes, event: str) -> None:
    handle = tempfile.NamedTemporaryFile(
        prefix="memhub-codex-hook-", suffix=".json", delete=False
    )
    try:
        handle.write(payload)
        handle.close()
        wrapper = (
            "import os,subprocess,sys; p=sys.argv[3]; "
            "f=open(p,'rb'); "
            "subprocess.run([sys.executable,sys.argv[1],sys.argv[2]],stdin=f,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "f.close(); os.unlink(p)"
        )
        kwargs = {
            "stdin": subprocess.DEVNULL,
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
        subprocess.Popen(
            [sys.executable, "-c", wrapper,
             str(root / "scripts" / "codex_flush.py"), event, handle.name],
            **kwargs,
        )
    except Exception:
        try:
            handle.close()
            Path(handle.name).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def main() -> int:
    try:
        action = sys.argv[1] if len(sys.argv) > 1 else ""
        payload = sys.stdin.buffer.read()
        root = resolve_plugin_root()
        if root is None:
            return 0
        if action == "directive-pre":
            _directive(root, payload, reactive=False)
        elif action == "directive-post":
            _directive(root, payload, reactive=True)
        elif action == "artifact-sync":
            _artifact_sync(root, payload)
        elif action == "flush" and len(sys.argv) > 2:
            _detach_flush(root, payload, sys.argv[2])
    except BaseException as exc:  # A memory hook must always fail open.
        print(f"[memhub-codex-bridge] {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
