#!/usr/bin/env python3
"""Cursor's rulebook lanes: team rules on shell commands, file reads and edits.

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

* `preToolUse` for an edit tool -> a `Write`/`Edit` call. cursor-agent's
  `Write` carries the whole new file in `content`; a deny keeps the file as
  it was.
* `postToolUse` / `postToolUseFailure` -> the post lane (`rulebook_hook.py
  post`): what a command printed, an edit that landed, and the exit code a
  "run X before Y" rule needs. A Shell call's `tool_output` is
  `{"output", "exitCode"}`; a failed one fires `postToolUseFailure` with
  `error_message` and no code, which `failure_result` reads as a failure.
  The post lane's words reach the agent as `additional_context`
  (cursor-agent 2026.10.16: `agent_message` and `user_message` do not).
  The same call also feeds what the Codex bridge runs after a tool: a shell
  call that may address GitHub -> `pr_link_trigger.py`, an edit ->
  `artifact_sync_reminder.py`.
* `sessionStart` -> what Claude Code's and Codex's SessionStart give the
  agent: the session-start rules and the Rulebook's index (`rulebook_hook.py
  session`) and the repo's brain brief (`brain_brief.py brief`), as
  `additional_context`, which Cursor hands the model (cursor-agent
  2026.09.10). cursor-agent fires none on `--resume`.

On a deny, Cursor tells the agent `user_message` and drops `agent_message`
(observed for shell, read and edit), so the rule's words and its disclosure
go in `user_message`.

A Cursor subagent runs under its own `conversation_id`. In the IDE (3.25)
its tool hooks carry `parent_tool_call_id`, the parent's `Task` call id, so
the parent is known exactly: the `preToolUse(Task)` this lane recorded under
that call id names it. cursor-agent sends no such field and fires no
`subagentStart`, so there the lane falls back to a little state:
`sessionStart` (which fires for the person's conversation and not for a
subagent's) marks a conversation as main, and a `Task` call in flight marks
any other unmarked conversation as its subagent. Either way that subagent is
passed to the hook as `agent_id`, so `given.agent.main` rules — "delegate big
reads to a cheaper model" — spare the very subagent they asked for.

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GATE_EVENTS = ("beforeShellExecution", "beforeReadFile", "preToolUse")
POST_EVENTS = ("postToolUse", "postToolUseFailure")
# Cursor's edit tools, by the Claude Code tool the hook judges them as. The
# IDE's names are not recorded yet; `Write` is cursor-agent 2026.10.16's.
EDIT_TOOLS = {"Write": "Write", "Edit": "Edit", "StrReplace": "Edit",
              "MultiEdit": "MultiEdit", "search_replace": "Edit"}
FLUSH_EVENTS = ("stop", "sessionEnd")
NOTE_EVENTS = ("sessionStart", "preToolUse", "postToolUse")
TASK_TTL_S = 30 * 60      # a Task left in flight (failed, cancelled) stops counting
READ_TTL_S = 60           # preToolUse Read -> beforeReadFile is milliseconds apart
STALE_S = 24 * 3600
HOOK_TIMEOUT_S = 7        # the Codex bridge's budget for the same call
SESSION_TIMEOUT_S = 5     # ... and for each SessionStart lane
ARTIFACT_TIMEOUT_S = 7
PR_LINK_TIMEOUT_S = 15


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
    """Forget `parent`'s Tasks. `postToolUse(Task)` ends one when it returns
    (cursor-agent 2026.10, the IDE 3.25; 2026.09.15 sent none), but a
    cancelled Task may never return and cursor-agent fires no subagentStop,
    so a Task also ends when its parent acts again — a foreground Task blocks
    its parent — or its session ends."""
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


def parent_of(conv: str, state: Path, now: float, data: dict | None = None) -> str | None:
    """The conversation whose Task spawned `conv`, or None for a main agent.

    Exact when the hook names the spawning call (`parent_tool_call_id`, the
    Cursor IDE) and that call's `preToolUse(Task)` was recorded; otherwise the
    newest unexpired Task of another conversation."""
    if (state / "main" / _key(conv)).exists():
        return None
    call = (data or {}).get("parent_tool_call_id")
    if isinstance(call, str) and call:
        parent = (_read(state / "tasks" / _key(call)) or {}).get("parent")
        if parent and parent != conv:
            return parent
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
    if event == "beforeShellExecution":
        command = data.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        tool, tool_input = "Bash", {"command": command}
    elif event == "preToolUse":
        mapped = _edit_input(data)
        if mapped is None:
            return None             # Shell and Read are gated by their own events
        tool, tool_input = mapped
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
    return _as_claude("PreToolUse", tool, tool_input, data, state, now)


def _edit_input(data: dict) -> tuple[str, dict] | None:
    """(Claude tool, tool_input) for a Cursor edit tool's call, or None."""
    tool = EDIT_TOOLS.get(str(data.get("tool_name") or ""))
    given = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    path = given.get("file_path") or given.get("path") or given.get("target_file")
    if tool is None or not isinstance(path, str) or not path:
        return None
    out = {"file_path": path}
    if tool == "Write":
        content = given.get("content", given.get("contents"))
        if isinstance(content, str):
            out["content"] = content
    else:
        for key, alias in (("old_string", "old_str"), ("new_string", "new_str")):
            value = given.get(key, given.get(alias))
            if isinstance(value, str):
                out[key] = value
        if isinstance(given.get("edits"), list):
            out["edits"] = given["edits"]
    return tool, out


def _shell_response(data: dict) -> dict:
    """A Shell call's `tool_output` (`{"output", "exitCode"}`) as the Bash
    `tool_response` the post lane reads: the exit code is what lets a passing
    command be a receipt and a failing one never arm a rule."""
    raw = data.get("tool_output")
    try:
        doc = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        return {"stdout": raw if isinstance(raw, str) else ""}
    resp = {"stdout": str(doc.get("output") or "")}
    if isinstance(doc.get("exitCode"), int):
        resp["exit_code"] = doc["exitCode"]
    return resp


def post_call(event: str, data: dict, state: Path | None = None,
              now: float | None = None) -> dict | None:
    """The Claude-shaped PostToolUse(/Failure) payload for a Cursor shell
    call or edit, or None when the post lane has nothing to read."""
    state = state or _state_dir()
    now = time.time() if now is None else now
    tool_name = str(data.get("tool_name") or "")
    given = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
    failed = event == "postToolUseFailure"
    if tool_name == "Shell":
        command = given.get("command")
        if not isinstance(command, str) or not command.strip():
            return None
        call = _as_claude("PostToolUseFailure" if failed else "PostToolUse",
                          "Bash", {"command": command}, data, state, now)
        if failed:
            # No exit code on a failure; `failure_result` marks it is_error.
            call["error"] = str(data.get("error_message") or "")
        else:
            call["tool_response"] = _shell_response(data)
        return call
    mapped = _edit_input(data)
    if mapped is None or failed:
        return None                 # an edit that did not land changed nothing
    call = _as_claude("PostToolUse", *mapped, data, state, now)
    call["tool_response"] = {"filePath": mapped[1]["file_path"], "success": True}
    return call


def _as_claude(hook_event: str, tool: str, tool_input: dict, data: dict,
               state: Path, now: float) -> dict:
    """A Claude Code hook payload for this Cursor call, its session being the
    person's conversation when a subagent made the call."""
    conv = str(data.get("conversation_id") or "")
    roots = data.get("workspace_roots") or []
    cwd = data.get("cwd") or (roots[0] if roots else "")
    call = {"hook_event_name": hook_event, "tool_name": tool,
            "tool_input": tool_input, "cwd": cwd or os.getcwd(),
            "session_id": conv, "tool_use_id": data.get("tool_use_id") or "",
            "transcript_path": data.get("transcript_path") or ""}
    parent = parent_of(conv, state, now, data) if conv else None
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
    if event == "preToolUse" and context:
        return {"permission": "allow", "additional_context": context}
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


def to_cursor_post(hook_stdout: bytes) -> dict | None:
    """rulebook_hook's post output for Cursor: everything for the agent rides
    `additional_context`, the one field a post hook delivers."""
    try:
        doc = json.loads(hook_stdout or b"{}")
    except ValueError:
        return None
    specific = doc.get("hookSpecificOutput") or {}
    parts = [doc.get("reason") if doc.get("decision") == "block" else "",
             specific.get("additionalContext") or ""]
    text = "\n\n".join(p for p in parts if p) or doc.get("systemMessage") or ""
    return {"additional_context": text} if text else None


def _run(argv: list[str], payload: bytes, timeout: float) -> bytes:
    """A sibling script's stdout, or b"" when it failed or ran out of time:
    one lane's failure never costs the others their words."""
    try:
        return subprocess.run(
            [sys.executable, str(Path(__file__).with_name(argv[0])), *argv[1:]],
            input=payload, capture_output=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return b""


def _context_of(hook_stdout: bytes) -> str:
    try:
        doc = json.loads(hook_stdout or b"{}")
        return str((doc.get("hookSpecificOutput") or {}).get("additionalContext") or "")
    except (ValueError, AttributeError):
        return ""


def _side_by_side(jobs: list[tuple[list[str], float]], payload: bytes) -> list[bytes]:
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        return list(pool.map(lambda job: _run(job[0], payload, job[1]), jobs))


def post(event: str, data: dict, state: Path | None = None,
         now: float | None = None) -> dict | None:
    """Run the post lane on a Cursor shell call or edit, and what the Codex
    bridge runs beside it; Cursor output or None."""
    call = post_call(event, data, state, now)
    if call is None:
        return None
    jobs = [(["rulebook_hook.py", "post", "--host", "cursor"], HOOK_TIMEOUT_S)]
    if event == "postToolUse":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from codex_hook_bridge import _may_touch_github  # noqa: PLC0415 — beside this file
        if call["tool_name"] == "Bash" and _may_touch_github(call):
            jobs.append((["pr_link_trigger.py", "--host", "cursor"], PR_LINK_TIMEOUT_S))
        elif call["tool_name"] in EDIT_TOOLS.values():
            jobs.append((["artifact_sync_reminder.py"], ARTIFACT_TIMEOUT_S))
    first, *rest = _side_by_side(jobs, json.dumps(call).encode())
    ruled = to_cursor_post(first)
    text = "\n\n".join(t for t in [(ruled or {}).get("additional_context", ""),
                                    *map(_context_of, rest)] if t)
    return {"additional_context": text} if text else None


def session_context(data: dict) -> str | None:
    """The agent's context at a Cursor `sessionStart`: the session-start rules
    and the brain brief, run side by side as the Codex bridge runs them."""
    conv = str(data.get("conversation_id") or "")
    if not conv:
        return None
    roots = data.get("workspace_roots") or []
    call = {"hook_event_name": "SessionStart", "session_id": conv, "source": "startup",
            "cwd": data.get("cwd") or (roots[0] if roots else os.getcwd()),
            "transcript_path": data.get("transcript_path") or ""}
    outs = _side_by_side([(["rulebook_hook.py", "session", "--host", "cursor"], SESSION_TIMEOUT_S),
                          (["brain_brief.py", "brief", "--host", "cursor"], SESSION_TIMEOUT_S)],
                         json.dumps(call).encode())
    return "\n\n".join(t for t in map(_context_of, outs) if t) or None


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
    parent = parent_of(conv, state, now, data)
    if parent:
        call["agent_id"] = conv      # a subagent's end is not the person's turn
    args = ["flush", "final"] if event == "sessionEnd" else ["flush"]
    return args + ["--host", "cursor"], call
