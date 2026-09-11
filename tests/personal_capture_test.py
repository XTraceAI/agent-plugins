#!/usr/bin/env python3
"""Sessions are captured into personal memory — never into a brain.

The server keys a captured conversation by the brain the import names:
``cb:<brain>:<sid>`` when routed, the bare ``<sid>`` otherwise. Capture used to
pick a room per flush, so a session whose room decision changed mid-session —
it started outside its repo, its worktree was deleted, a room lookup succeeded
late — was stored as TWO conversations with the same name. Every fork observed
had exactly that shape.

Not routing sessions at all removes the fork by construction, and these tests
pin it. The two Claude flush paths are driven for real, with a room CACHED for
the session's repo, and must still send no brain and ask the server nothing but
the import. The other session capture paths are checked at the source.

Run: python3 tests/personal_capture_test.py   (stdlib only)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

# Before any plugin import: several modules resolve their state paths from the
# environment at import time, and none of this may touch the real config dir.
_TMP_HOME = tempfile.mkdtemp(prefix="personal-capture-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
os.environ["MEMHUB_ROOMS_FILE"] = str(Path(_TMP_HOME) / "rooms.json")

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import flush_session as fs  # noqa: E402
import flush_turn as ft  # noqa: E402
import room_map  # noqa: E402

SID = "11111111-2222-4333-8444-555555555555"
URL = "https://memhub.example.test/mcp-server/mcp"

_failures: list[str] = []


def check(label, got, want):
    if got != want:
        _failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


# ── fixtures ──────────────────────────────────────────────────────────

def _repo(root: Path) -> str:
    """A real checkout with an origin, so namespace and room name resolve."""
    repo = root / "checkout"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "git@github.com:ExampleOrg/example-repo.git"],
                   check=True, capture_output=True)
    return str(repo)


def _cache_room(repo: str) -> None:
    """A room cached for the repo on both backends — the state that USED to
    route every capture into the brain."""
    for env in ("production", "staging"):
        room_map.write_room("brain-cached", cwd=repo, env=env, org_id="org-1")


def _append(path: Path, cwd: str, *uuids: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for uid in uuids:
            fh.write(json.dumps({
                "type": "user", "uuid": uid, "cwd": cwd, "sessionId": SID,
                "message": {"role": "user", "content": f"turn {uid}"},
            }) + "\n")


class Recorder:
    """Stands in for ``mcp_http.Session``: records every tool call and answers
    an import with a well-formed durable acknowledgement."""

    calls: list[tuple[str, dict]] = []

    def __init__(self, _url, _bearer, **_kwargs):
        pass

    async def call_tool(self, name, arguments=None, timeout=None):
        arguments = dict(arguments or {})
        Recorder.calls.append((name, arguments))
        messages = arguments.get("messages") or []
        structured = {
            "conversation_id": arguments.get("conversation_id"),
            "ack_through": messages[-1].get("uuid") if messages else None,
            "messages_received": len(messages),
            "path": "agentic",
        }
        return types.SimpleNamespace(structuredContent=structured, content=[],
                                     isError=False)


def _patched(module, state_attr, state_dir):
    """Swap in the recorder, a credential and a private state dir."""
    saved = (module.mcp_http.Session, module.resolve_bearer,
             getattr(module, state_attr), module._log)
    module.mcp_http.Session = Recorder
    module.resolve_bearer = lambda: (URL, "token")
    setattr(module, state_attr, state_dir)
    module._log = lambda _message: None

    def restore():
        (module.mcp_http.Session, module.resolve_bearer,
         _state, module._log) = saved
        setattr(module, state_attr, _state)
    return restore


def _check_personal(label, calls):
    check(f"{label}: something was imported", bool(calls), True)
    check(f"{label}: the server is asked for nothing but the import",
          sorted({name for name, _ in calls}), ["import_conversation"])
    check(f"{label}: no import names a brain",
          [a for _, a in calls if "agent_brain_id" in a], [])
    check(f"{label}: no import names an org",
          [a for _, a in calls if "org_id" in a], [])
    check(f"{label}: one conversation id throughout",
          sorted({a.get("conversation_id") for _, a in calls}), [SID])


# ── the two Claude paths, driven for real ────────────────────────────

def test_per_turn_flush_stays_personal_as_the_session_moves(tmp: Path):
    """The dominant fork shape: a session opens in a directory that is not a
    repo, then works inside one whose room is cached. Both flushes must land
    in the same place — personal — under one conversation id."""
    repo = _repo(tmp)
    _cache_room(repo)
    container = tmp / "not-a-repo"
    container.mkdir()
    transcript = tmp / f"{SID}.jsonl"
    Recorder.calls = []
    restore = _patched(ft, "STATE_DIR", tmp / "turnflush")
    try:
        _append(transcript, str(container), "u1")
        asyncio.run(ft._flush(SID, str(transcript)))
        _append(transcript, repo, "u2", "u3")
        asyncio.run(ft._flush(SID, str(transcript)))
    finally:
        restore()
    check("both turns were sent", len(Recorder.calls), 2)
    _check_personal("per-turn", Recorder.calls)
    check("the repo turn still carries its namespace",
          Recorder.calls[-1][1].get("namespace"), "example-repo")


def test_backstop_stays_personal_with_a_cached_room(tmp: Path):
    repo = _repo(tmp)
    _cache_room(repo)
    transcript = tmp / f"{SID}.jsonl"
    _append(transcript, repo, "u1", "u2")
    Recorder.calls = []
    restore = _patched(fs, "_SESSION_STATE_DIR", tmp / "turnflush")
    try:
        asyncio.run(fs._flush(SID, str(transcript)))
    finally:
        restore()
    _check_personal("backstop", Recorder.calls)
    check("the backstop still carries the namespace",
          Recorder.calls[-1][1].get("namespace"), "example-repo")


# ── every other session capture path, at the source ──────────────────

def test_no_session_capture_path_can_name_a_brain(tmp: Path):
    """Codex, Cursor and the manual import are not driven here (their
    entrypoints need a host store or the mcp SDK), so the property is held at
    the source: no session capture path can put a brain on the wire or ask
    for one. Comments may explain the rule; code may not break it."""
    for name in ("flush_turn.py", "flush_session.py", "codex_flush.py",
                 "cursor_flush.py", "import_session.py"):
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        for token in ('"agent_brain_id"', "resolve_repo_brain", "read_room(",
                      "forget_room("):
            check(f"{name} has no {token}", token in source, False)
    for name in ("flush_turn.py", "flush_session.py", "codex_flush.py",
                 "cursor_flush.py"):
        source = (SCRIPTS / name).read_text(encoding="utf-8")
        check(f"{name} sends no org_id", '"org_id"' in source, False)
    capture = (SCRIPTS / "capture.py").read_text(encoding="utf-8")
    for flag in ("--agent-brain-id", "--no-room"):
        check(f"capture.py import has no {flag}", flag in capture, False)


if __name__ == "__main__":
    # Discovered from globals(), NOT a hand-maintained list — see
    # ``registration_test`` for the bug that convention exists to prevent.
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            print(_name)
            with tempfile.TemporaryDirectory() as _tmp:
                _fn(Path(_tmp))
    if _failures:
        print(f"\n{len(_failures)} failure(s)")
        raise SystemExit(1)
    print("\nall passed")
