#!/usr/bin/env python3
"""Cursor's rulebook lane: team rules that gate a shell command or a file read.

Claude Code and Codex run `rulebook_hook.py pre` on PreToolUse. Cursor has no
PreToolUse that sees what a read pulls, so this maps Cursor's own hooks onto
the same Claude-shaped call and back:

* `beforeShellExecution` -> a `Bash` call. Cursor sends `cwd: ""`, so the
  workspace root stands in, and `cat`/`head`/`sed` become the hook's
  synthetic reads exactly as they do on Claude Code.
* `beforeReadFile` -> a `Read` call. Cursor's `preToolUse` for Read omits the
  offset/limit the agent asked for (observed on cursor-agent 2026.09.15), so
  the line count comes from `content` — the slice Cursor is about to hand the
  model. A deny there keeps that slice out of the model.

On a deny, Cursor tells the agent `user_message` and drops `agent_message`
(both events), so the rule's words and its disclosure go in `user_message`.

A Cursor subagent runs under its own `conversation_id`, with no field naming
its parent, and `subagentStart` does not fire in cursor-agent. So the lane
keeps a little state: `sessionStart` (which fires for the person's
conversation and not for a subagent's) marks a conversation as main, and a
`Task` call in flight marks any other unmarked conversation as its subagent.
That subagent is passed to the hook as `agent_id`, so `given.agent.main`
rules — "delegate big reads to a cheaper model" — spare the very subagent
they asked for.

A read is gated only when the agent's own Read tool asked for that file a
moment ago (`preToolUse` Read): `beforeReadFile` also fires for files the
person attached, and those are theirs to send.

Fail-open everywhere: any error answers allow, as capture always has.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

GATE_EVENTS = ("beforeShellExecution", "beforeReadFile")
FLUSH_EVENTS = ("stop", "sessionEnd")
NOTE_EVENTS = ("sessionStart", "preToolUse", "postToolUse")
TASK_TTL_S = 30 * 60      # a Task left in flight (failed, cancelled) stops counting
READ_TTL_S = 60           # preToolUse Read -> beforeReadFile is milliseconds apart
STALE_S = 24 * 3600
HOOK_TIMEOUT_S = 7        # the Codex bridge's budget for the same call


def _state_dir() -> Path:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import rulebook_paths  # noqa: PLC0415 — beside this file
        base = rulebook_paths.base()
    except Exception:
        base = (os.environ.get("MEMHUB_RULEBOOK_BASE")
                or os.path.expanduser("~/.config/memhub-plugin/rulebook"))
    return Path(base) / "cursor"


def _key(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _sweep(directory: Path, now: float) -> None:
    try:
        for entry in directory.iterdir():
            if now - entry.stat().st_mtime > STALE_S:
                entry.unlink()
    except OSError:
        pass


def note(event: str, data: dict, state: Path | None = None, now: float | None = None) -> None:
    """Record what later gates need: which conversation is the person's,
    which Task is in flight, which file the agent's Read tool asked for."""
    state = state or _state_dir()
    now = time.time() if now is None else now
    conv = str(data.get("conversation_id") or "")
    if not conv:
        return
    tool = data.get("tool_name")
    if event == "sessionStart":
        _write(state / "main" / _key(conv), {"conversation": conv, "at": now})
        _sweep(state / "main", now)
    elif event == "preToolUse" and tool == "Task":
        call = str(data.get("tool_use_id") or conv)
        _write(state / "tasks" / _key(call), {"parent": conv, "at": now})
        _sweep(state / "tasks", now)
    elif event == "postToolUse" and tool == "Task":
        call = str(data.get("tool_use_id") or conv)
        try:
            (state / "tasks" / _key(call)).unlink()
        except OSError:
            pass
    elif event == "preToolUse" and tool == "Read":
        path = str((data.get("tool_input") or {}).get("file_path") or "")
        if path:
            _write(state / "reads" / _key(conv + "\0" + path), {"at": now})
            _sweep(state / "reads", now)
    elif event in FLUSH_EVENTS:
        _end_tasks(conv, state)


def _end_tasks(parent: str, state: Path) -> None:
    """Forget `parent`'s Tasks. cursor-agent sends no postToolUse for a Task
    (nor subagentStop, observed 2026.09.15), so a Task also ends when its
    parent acts again — a Task blocks its parent — or its session ends."""
    try:
        entries = list((state / "tasks").iterdir())
    except OSError:
        return
    for entry in entries:
        if (_read(entry) or {}).get("parent") == parent:
            try:
                entry.unlink()
            except OSError:
                pass


def parent_of(conv: str, state: Path, now: float) -> str | None:
    """The conversation whose Task spawned `conv`, or None for a main agent."""
    if (state / "main" / _key(conv)).exists():
        return None
    newest = None
    try:
        entries = list((state / "tasks").iterdir())
    except OSError:
        return None
    for entry in entries:
        doc = _read(entry) or {}
        parent, at = doc.get("parent"), doc.get("at") or 0
        if parent and parent != conv and now - at < TASK_TTL_S:
            if newest is None or at > newest[1]:
                newest = (parent, at)
    return newest[0] if newest else None


def _agent_asked(conv: str, path: str, state: Path, now: float) -> bool:
    doc = _read(state / "reads" / _key(conv + "\0" + path))
    return bool(doc) and now - (doc.get("at") or 0) < READ_TTL_S


def _line_count(content: str) -> int:
    return content.count("\n") + (1 if content and not content.endswith("\n") else 0)


def hook_call(event: str, data: dict, state: Path, now: float) -> dict | None:
    """The Claude-shaped PreToolUse payload for this Cursor call, or None
    when there is nothing to judge."""
    conv = str(data.get("conversation_id") or "")
    roots = data.get("workspace_roots") or []
    cwd = data.get("cwd") or (roots[0] if roots else "")
    if event == "beforeShellExecution":
        command = data.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        tool, tool_input = "Bash", {"command": command}
    else:
        path = data.get("file_path")
        content = data.get("content")
        if not isinstance(path, str) or not path or not _agent_asked(conv, path, state, now):
            return None
        tool, tool_input = "Read", {"file_path": path}
        if isinstance(content, str):
            # What Cursor is about to hand the model; read_facts takes the
            # smaller of this and the file's length.
            tool_input["limit"] = _line_count(content)
    call = {"hook_event_name": "PreToolUse", "tool_name": tool,
            "tool_input": tool_input, "cwd": cwd or os.getcwd(),
            "session_id": conv, "tool_use_id": data.get("tool_use_id") or "",
            "transcript_path": data.get("transcript_path") or ""}
    parent = parent_of(conv, state, now) if conv else None
    if parent:
        # Fires and ordering state belong to the person's session, as a
        # Claude Code subagent's do; agent_id marks the call as not-main.
        call["session_id"], call["agent_id"] = parent, conv
    elif conv:
        _end_tasks(conv, state)
    return call


def to_cursor(event: str, hook_stdout: bytes) -> dict | None:
    """rulebook_hook's PreToolUse output, in the shape this Cursor hook takes."""
    try:
        doc = json.loads(hook_stdout or b"{}")
    except ValueError:
        return None
    specific = doc.get("hookSpecificOutput") or {}
    context = specific.get("additionalContext") or ""
    user_line = doc.get("systemMessage") or ""
    if specific.get("permissionDecision") == "deny":
        reason = specific.get("permissionDecisionReason") or "Blocked by the team rulebook."
        text = "\n\n".join(t for t in (reason, context) if t)
        # On a deny Cursor shows the agent user_message only (agent_message
        # is dropped, observed for both events), so the rule's words and the
        # disclosure it asks for go there.
        return {"permission": "deny", "user_message": text, "agent_message": text}
    if event == "beforeShellExecution" and (context or user_line):
        out = {"permission": "allow"}
        if context:
            out["agent_message"] = context
        if user_line:
            out["user_message"] = user_line
        return out
    return None


def gate(event: str, data: dict, state: Path | None = None, now: float | None = None) -> dict | None:
    """Cursor output for a gated call (deny, or allow with the rule's words),
    or None to leave the call alone."""
    state = state or _state_dir()
    now = time.time() if now is None else now
    call = hook_call(event, data, state, now)
    if call is None:
        return None
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("rulebook_hook.py")), "pre",
         "--host", "cursor"],
        input=json.dumps(call).encode(), capture_output=True,
        timeout=HOOK_TIMEOUT_S, check=False)
    return to_cursor(event, result.stdout)


def flush_call(event: str, data: dict, state: Path | None = None,
               now: float | None = None) -> tuple[list[str], dict] | None:
    """(argv tail, payload) for `rulebook_hook.py flush` at a Cursor turn's or
    session's end: what ships this machine's fires and closes their turns, as
    the Stop hook does on Claude Code and Codex. None when there is no
    conversation to name."""
    conv = str(data.get("conversation_id") or "")
    if not conv:
        return None
    state = state or _state_dir()
    now = time.time() if now is None else now
    roots = data.get("workspace_roots") or []
    call = {"session_id": conv, "cwd": data.get("cwd") or (roots[0] if roots else os.getcwd()),
            "hook_event_name": "SessionEnd" if event == "sessionEnd" else "Stop",
            "transcript_path": data.get("transcript_path") or ""}
    parent = parent_of(conv, state, now)
    if parent:
        call["agent_id"] = conv      # a subagent's end is not the person's turn
    args = ["flush", "final"] if event == "sessionEnd" else ["flush"]
    return args + ["--host", "cursor"], call
