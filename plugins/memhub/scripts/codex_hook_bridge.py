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
import shlex
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
_SHELL_TOOLS = {"Bash", "shell", "local_shell", "exec_command", "shell_command"}
_GATE_TIMEOUT_S = 1
_RECALL_TIMEOUT_S = 6
_ARTIFACT_TIMEOUT_S = 7
_PR_LINK_TIMEOUT_S = 15
# SessionStart runs three children in parallel under one 8 s handler budget.
# Each is stdlib-only and network-free on its synchronous path (the rulebook
# lane's one blocking fetch is capped at SESSION_FETCH_TIMEOUT_S = 1 s and
# only happens on a stale book); the budgets below are ceilings, not costs.
_SESSION_TIMEOUT_S = 5
_HEALTH_TIMEOUT_S = 3
# A GitHub MCP server — the same coarse test the hook manifests use, so a tool
# called `mcp__notes__github_summary` is not mistaken for a GitHub client while
# a server whose name contains underscores (`github_enterprise`, or any
# plugin-provided one) still dispatches. `pr_link.is_github_mcp_tool` is the
# precise gate; this only decides whether the trigger runs at all.
# Only the plugin-bundled manifest (hooks/codex-hooks.json) dispatches these;
# the compatibility bridge in references/codex-hooks-bridge.json still lists
# shell and edit tools only, because widening it costs the user a re-trust of
# a file already in ~/.codex/hooks.json.
_GITHUB_MCP_RX = re.compile(r"(?i)^mcp__.*github.*__")


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


# Claude's manifest keeps the PR-link check behind a shell `case` byte filter,
# so an ordinary shell call never starts a python process. Codex multiplexes
# ONE PostToolUse handler, so there is nowhere in the manifest to put that —
# without this, every shell call paid a subprocess (measured: +16 ms wall,
# +37 ms CPU, +5.5 MB RSS each, and +159 ms per batch at 8 concurrent calls).
# `touches_github` reads only the tool name and the command, so filtering on
# the command alone is both cheaper and more precise than Claude's whole-stdin
# `case`. Measured cost: 0.2-0.9 us on a real command, 186 us on a
# pathological 8 KB one.
_PR_LINK_HINT = re.compile(r"(?i)\bgh\b|github|api/v3|repos/.{0,120}?pulls")
_HINT_SCAN_CHARS = 8192


def _may_touch_github(hook: dict) -> bool:
    """Cheap pre-filter: could this shell call possibly address GitHub?

    Fails OPEN on any shape it does not recognise — a missed detection is a
    silently unlinked pull request, and this filter exists to save a process,
    not to make decisions.
    """
    tool_input = hook.get("tool_input")
    if not isinstance(tool_input, dict):
        return True
    command = tool_input.get("command")
    if command is None:
        command = tool_input.get("cmd")
    if isinstance(command, list):          # Codex `shell` passes an argv array
        command = " ".join(part for part in command if isinstance(part, str))
    if not isinstance(command, str):
        return True
    return bool(_PR_LINK_HINT.search(command[:_HINT_SCAN_CHARS]))


def _pr_link_result(root: Path, payload: bytes) -> subprocess.CompletedProcess:
    return _run(
        root,
        "pr_link_trigger.py",
        payload,
        "--host",
        "codex",
        timeout=_PR_LINK_TIMEOUT_S,
    )


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
    jobs = [lambda: _directive_result(root, payload, reactive=True),
            lambda: _rulebook_result(root, payload, "post")]
    if tool in _EDIT_TOOLS:
        jobs.append(lambda: _artifact_sync_result(root, payload))
    # The MCP branch needs no byte scan — the tool NAME is the filter.
    if (tool in _SHELL_TOOLS and _may_touch_github(hook)) or (
            isinstance(tool, str) and _GITHUB_MCP_RX.match(tool)):
        jobs.append(lambda: _pr_link_result(root, payload))
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

    _merge_results(results, "PostToolUse")


def _rulebook_payload(payload: bytes) -> bytes:
    hook = json.loads(payload or b"{}")
    inp = hook.get("tool_input") or {}
    if hook.get("tool_name") in _SHELL_TOOLS and isinstance(inp, dict):
        command = inp.get("command", inp.get("cmd", ""))
        if isinstance(command, list) and all(isinstance(p, str) for p in command):
            # Codex's shell tool uses argv; only unwrap a real shell -c form.
            if (len(command) == 3 and Path(command[0]).name in {"bash", "sh", "zsh", "dash"}
                    and command[1] in {"-c", "-lc", "-cl"}):
                command = command[2]
            else:
                command = shlex.join(command)
        hook = {**hook, "tool_name": "Bash", "tool_input": {**inp, "command": command}}
        if isinstance(inp.get("workdir"), str) and inp["workdir"]:
            hook["cwd"] = inp["workdir"]
    return json.dumps(hook).encode()


def _rulebook_result(root: Path, payload: bytes, mode: str) -> subprocess.CompletedProcess:
    # --host codex: capture uploads this session as `codex-<uuid>`, so a fire
    # reported under the bare uuid can never be joined to it (ENG-1075).
    return _run(root, "rulebook_hook.py", _rulebook_payload(payload),
                "codex-pre" if mode == "pre" else mode, "--host", "codex",
                timeout=_SESSION_TIMEOUT_S if mode == "session" else 7)


def _merge_results(results, event: str) -> None:
    contexts, messages = [], []
    output = {"hookEventName": event}
    for result in results:
        if result is None:
            continue
        context = _additional_context(result)
        if context:
            contexts.append(context)
        try:
            doc = json.loads(result.stdout or b"{}")
            specific = doc.get("hookSpecificOutput", {})
            if specific.get("permissionDecision") == "deny":
                output.update(permissionDecision="deny",
                              permissionDecisionReason=specific.get("permissionDecisionReason", "Rulebook denied this call"))
            if isinstance(doc.get("systemMessage"), str) and doc["systemMessage"]:
                messages.append(doc["systemMessage"])
        except (ValueError, TypeError, AttributeError):
            pass
    if contexts:
        output["additionalContext"] = "\n\n".join(contexts)
    if contexts or messages or output.get("permissionDecision"):
        doc = {"hookSpecificOutput": output}
        if messages:
            doc["systemMessage"] = "\n\n".join(messages)
        print(json.dumps(doc))


def _dispatch(root: Path, payload: bytes, event: str) -> None:
    try:
        hook = json.loads(payload or b"{}")
    except (TypeError, ValueError):
        return
    if not isinstance(hook, dict):
        return
    if event == "SessionStart":
        # The same three scripts Claude's SessionStart entry runs, unchanged:
        # they key on session_id and cwd, which Codex's SessionStart payload
        # also carries. capture_health is told the host so it reads Codex's
        # flush state and finds the package without CLAUDE_PLUGIN_ROOT.
        jobs = [
            lambda: _rulebook_result(root, payload, "session"),
            lambda: _run(root, "brain_brief.py", payload, "brief",
                         timeout=_SESSION_TIMEOUT_S),
            lambda: _run(root, "capture_health.py", payload, "--host", "codex",
                         "--plugin-root", str(root), timeout=_HEALTH_TIMEOUT_S),
        ]
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(_fail_open_job, jobs))
        _merge_results(results, event)
    elif event == "PreToolUse":
        jobs = [
            lambda: _rulebook_result(root, payload, "pre"),
            lambda: _directive_result(root, payload, reactive=False),
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_fail_open_job, jobs))
        _merge_results(results, event)
    elif event == "PostToolUse":
        _dispatch_post(root, payload, hook)
    elif event == "Stop":
        _detach_flush(root, payload, "Stop")
        _fail_open_job(lambda: _run(root, "rulebook_hook.py", payload, "flush", "final", timeout=7))


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
