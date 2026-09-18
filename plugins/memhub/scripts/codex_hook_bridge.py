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
import time
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


# OBSERVED, not reasoned about: `codex plugin add` (Codex 0.154.0, local path
# marketplace) copies a plugin's regular files and SKIPS its symlinks, so the
# staging build — whose scripts/ skills/ hooks/ references/ are relative
# symlinks into ../memhub/ — lands as three files: `.claude-plugin/plugin.json`,
# `.mcp.json`, `mcp.json`. The install reports success and prints the version.
# There is then no `scripts/` at all, resolve_plugin_root() returns None, and
# main() used to exit 0 with no output, no record and no capture. A blind
# `codex exec` session against that install produced: zero rows in the rulebook
# ledger, no codexflush state file, and not one line anywhere naming MemHub.
# The same session against a repaired install produced six fires and a health
# file — so the hooks WERE firing the whole time; they were landing in silence.
#
# Leave a breadcrumb in the shape capture_health already reads, and say so once
# per session. The breadcrumb alone is not enough: capture_health.py lives in
# the very root we could not find, so with no root there is nothing to read it
# back out.
#
# Deliberately NOT restored with it: the wide marketplace search and the
# newest-complete-version fallback. Nothing observed here demonstrates either
# precondition, and it is the ranking across marketplaces that once sent a prod
# user's captures to staging. `_KNOWN_INSTALLS` stays an ordered, unpooled scan.
_STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "codexflush"
_BRIDGE_STATE = _STATE_DIR / "_bridge.json"
_UNRESOLVED = "plugin_root_unresolved"
_UNRESOLVED_MESSAGE = (
    "MemHub: this Codex install is missing the plugin's script files, so "
    "session capture and Rulebook telemetry are OFF. Reinstall the MemHub "
    "plugin, then run the memhub:setup skill to confirm it is healthy."
)


def _record_unresolved() -> None:
    """Record the failure where capture_health looks for one.

    Same ``last_error``/``last_error_at`` shape codex_flush writes, so the
    next healthy session surfaces it through the ordinary health path rather
    than needing a second reporting channel.
    """
    name = None
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="._bridge.", dir=_STATE_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"last_error": _UNRESOLVED,
                       "last_error_at": time.time()}, handle)
        os.replace(name, _BRIDGE_STATE)
        name = None
    except OSError:
        pass          # a breadcrumb is never worth failing a hook over
    finally:
        # A failed flush-on-close or a replace that loses a Windows sharing
        # race would otherwise strand the temp file — once per hook event,
        # forever, in the one directory we ask users to keep.
        if name is not None:
            try:
                os.unlink(name)
            except OSError:
                pass


def _clear_unresolved() -> None:
    """Retract the breadcrumb once a root resolves again.

    A stale ``last_error`` outliving the failure it recorded is its own bug —
    this hook has been bitten by exactly that on the recall lane.
    """
    try:
        _BRIDGE_STATE.unlink(missing_ok=True)
    except OSError:
        pass


def _report_unresolved(action: str, event: str) -> None:
    """One visible line, on SessionStart only — it is once per session.

    Both channels, deliberately. `additionalContext` is the one every other
    SessionStart lane here already uses (`_merge_results`), so it is the one
    this host is known to consume; `systemMessage` is the one a person reads
    rather than the model. Neither has been watched arriving on Codex — the
    observation run that produced the breadcrumb below never completed a turn,
    so nothing rendered — and a notice that exists only in the channel we
    guessed wrong is the same silence this fixes.
    """
    _record_unresolved()
    if action == "dispatch" and event == "SessionStart":
        print(json.dumps({
            "hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": _UNRESOLVED_MESSAGE},
            "systemMessage": _UNRESOLVED_MESSAGE,
        }))


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
        # --host codex here too. The flush lane used to only SHIP rows that
        # already carried their host, so it needed none; #240 made it also
        # CREATE the turn_end/session_end events, and without this they are
        # namespaced `claude` while this session's fires are namespaced
        # `codex` — observed live, and the server folds by (org, session_id),
        # so every one of them folded into nothing.
        _fail_open_job(lambda: _run(root, "rulebook_hook.py", payload,
                                    "flush", "final", "--host", "codex",
                                    timeout=7))


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


def _user_bridge_handles(event: str, payload: bytes) -> bool:
    """Bundled hooks defer to the explicitly installed user bridge, per event."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    runner = home / "memhub_hook_bridge.py"
    if not runner.is_file():
        return False
    try:
        doc = json.loads((home / "hooks.json").read_text())
        tool = json.loads(payload or b"{}").get("tool_name", "")
        for group in doc.get("hooks", {}).get(event, []):
            matcher = group.get("matcher")
            if matcher and not re.search(matcher, tool):
                continue
            for handler in group.get("hooks", []):
                command = handler.get("commandWindows" if os.name == "nt" else "command", "")
                if (handler.get("type") == "command" and
                        str(runner).replace("\\", "/") in command.replace("\\", "/") and
                        "dispatch " + event in command):
                    return True
    except (OSError, ValueError, TypeError, AttributeError, re.error):
        pass
    return False


def main() -> int:
    try:
        action = sys.argv[1] if len(sys.argv) > 1 else ""
        payload = sys.stdin.buffer.read()
        if ("--plugin-hook" in sys.argv and len(sys.argv) > 2
                and _user_bridge_handles(sys.argv[2], payload)):
            return 0
        root = resolve_plugin_root()
        if root is None:
            _report_unresolved(action, sys.argv[2] if len(sys.argv) > 2 else "")
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
        # Retract AFTER dispatching, never before: capture_health runs INSIDE
        # _dispatch, so clearing first meant it could never see the breadcrumb a
        # previous broken session left — and when the root is missing entirely,
        # capture_health cannot run at all (it lives in that same root). Cleared
        # up front, `_bridge.json` was readable by nothing.
        _clear_unresolved()
    except BaseException as exc:  # A memory hook must always fail open.
        print(f"[memhub-codex-bridge] {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
