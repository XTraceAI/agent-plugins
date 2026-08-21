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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_KNOWN_INSTALLS = (
    ("xtrace-plugins", "memhub"),
    ("memhub-internal", "memhub-staging"),
)
_VERSION_PART = re.compile(r"\d+|[A-Za-z]+")
_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit", "apply_patch"}
_SHELL_TOOLS = {"Bash", "shell", "local_shell"}
_GATE_TIMEOUT_S = 1
_RECALL_TIMEOUT_S = 6
_ARTIFACT_TIMEOUT_S = 7


def _version_key(path: Path) -> tuple:
    """Natural ordering for semver and Codex cachebuster directory names."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.lower())
        for part in _VERSION_PART.findall(path.name)
    )


def resolve_plugin_root() -> Path | None:
    for variable in ("MEMHUB_PLUGIN_ROOT", "PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        override = os.environ.get(variable)
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


def _run(
    root: Path,
    script: str,
    payload: bytes,
    *args: str,
    timeout: float = 7,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / script), *args],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _relay(result: subprocess.CompletedProcess) -> None:
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
    if result.stdout:
        sys.stdout.buffer.write(result.stdout)


def _directive_result(
    root: Path, payload: bytes, reactive: bool
) -> subprocess.CompletedProcess | None:
    try:
        hook = json.loads(payload or b"{}")
    except (TypeError, ValueError):
        return

    if reactive:
        gate = _run(
            root, "reactive_prefilter.py", payload, timeout=_GATE_TIMEOUT_S
        )
        if gate.returncode != 0:
            return
    elif hook.get("tool_name") in _SHELL_TOOLS:
        gate = _run(
            root, "directive_prefilter.py", payload, timeout=_GATE_TIMEOUT_S
        )
        if gate.returncode != 0:
            return

    return _run(
        root, "directive_recall.py", payload, timeout=_RECALL_TIMEOUT_S
    )


def _directive(root: Path, payload: bytes, reactive: bool) -> None:
    result = _directive_result(root, payload, reactive)
    if result is not None:
        _relay(result)


def _artifact_sync_result(root: Path, payload: bytes) -> subprocess.CompletedProcess:
    return _run(
        root,
        "artifact_sync_reminder.py",
        payload,
        timeout=_ARTIFACT_TIMEOUT_S,
    )


def _artifact_sync(root: Path, payload: bytes) -> None:
    # artifact_sync_reminder already emits Codex/Claude-compatible
    # hookSpecificOutput JSON. Relay it byte-for-byte; wrapping it again would
    # turn the JSON document itself into the model-visible reminder text.
    _relay(_artifact_sync_result(root, payload))


def _additional_context(result: subprocess.CompletedProcess | None) -> str | None:
    if result is None:
        return None
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
    if not result.stdout:
        return None
    try:
        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
    except (KeyError, TypeError, ValueError) as exc:
        print(f"[memhub-codex-bridge] invalid hook output: {exc}", file=sys.stderr)
        return None
    return context if isinstance(context, str) and context else None


def _fail_open_job(job):
    try:
        return job()
    except BaseException as exc:
        print(
            f"[memhub-codex-bridge] {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None


def _dispatch_post(root: Path, payload: bytes, hook: dict) -> None:
    tool = hook.get("tool_name")
    jobs = [lambda: _directive_result(root, payload, reactive=True)]
    if tool in _EDIT_TOOLS:
        jobs.append(lambda: _artifact_sync_result(root, payload))
    if tool in _SHELL_TOOLS:
        _fail_open_job(lambda: _detach_flush(root, payload, "PostToolUse"))

    if len(jobs) == 1:
        results = [_fail_open_job(jobs[0])]
    else:
        # The old layout ran these as separate handlers. Preserve that latency
        # profile while folding their output into one valid JSON document.
        with ThreadPoolExecutor(max_workers=len(jobs)) as executor:
            futures = [executor.submit(_fail_open_job, job) for job in jobs]
            results = [future.result() for future in futures]

    contexts = [context for result in results
                if (context := _additional_context(result))]
    if contexts:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n\n".join(contexts),
            }
        }))


def _dispatch(root: Path, payload: bytes, event: str) -> None:
    try:
        hook = json.loads(payload or b"{}")
    except (TypeError, ValueError):
        return
    if not isinstance(hook, dict):
        return
    if event == "PreToolUse":
        _directive(root, payload, reactive=False)
    elif event == "PostToolUse":
        _dispatch_post(root, payload, hook)
    elif event == "Stop":
        _detach_flush(root, payload, "Stop")


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
        if action == "dispatch" and len(sys.argv) > 2:
            _dispatch(root, payload, sys.argv[2])
        elif action == "directive-pre":
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
