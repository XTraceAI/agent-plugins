#!/usr/bin/env python3
"""Regression tests for Cursor importing MemHub's Claude hooks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "memhub"
sys.path.insert(0, str(PLUGIN / "scripts"))

import claude_hook_guard as guard  # noqa: E402
import cursor_capture as capture  # noqa: E402

FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "cursor_hook_payload.json")
    .read_text(encoding="utf-8")
)


def test_cursor_wins_over_claude_compatibility_environment():
    env = {"CLAUDE_PROJECT_DIR": "/tmp/repo",
           "CLAUDE_PLUGIN_ROOT": str(PLUGIN)}
    assert guard.is_cursor(FIXTURE, env)
    assert guard.is_cursor({}, {"CURSOR_VERSION": "2026.08.11"})
    assert guard.is_cursor({}, {"CURSOR_PLUGIN_ROOT": "/tmp/plugin"})
    assert guard.is_cursor(
        {"transcript_path": "C:\\Users\\x\\.cursor\\projects\\s.jsonl"}, {})

    claude = {
        "session_id": "claude-session",
        "hook_event_name": "Stop",
        "transcript_path": "/Users/x/.claude/projects/session.jsonl",
    }
    assert not guard.is_cursor(claude, env)
    assert not guard.is_cursor({}, env)
    # A future Claude payload could gain one generic compatibility field;
    # one lookalike must not be enough to suppress its hooks.
    assert not guard.is_cursor({
        "hook_event_name": "Stop",
        "conversation_id": "claude-conversation",
    }, env)
    print("PASS test_cursor_wins_over_claude_compatibility_environment")


def test_cursor_capture_routes_once_and_claude_continues():
    seen: list[tuple[bytes, str]] = []
    original = guard._spawn_cursor_flush
    guard._spawn_cursor_flush = lambda raw, event: seen.append((raw, event))
    raw = json.dumps(FIXTURE).encode()
    try:
        assert not guard.route("capture", "Stop", FIXTURE, raw, {})
        assert seen == [(raw, "stop")]
        assert not guard.route("ignore", "Stop", FIXTURE, raw, {})
        assert seen == [(raw, "stop")]
        assert not guard.route("capture", "PostToolUse", FIXTURE, raw, {})
        assert seen == [(raw, "stop")]
        assert guard.route("capture", "Stop", {
            "session_id": "claude-session",
            "hook_event_name": "Stop",
        }, b"{}", {})
        assert seen == [(raw, "stop")]
    finally:
        guard._spawn_cursor_flush = original
    print("PASS test_cursor_capture_routes_once_and_claude_continues")


def test_every_claude_handler_is_guarded_and_only_boundaries_capture():
    document = json.loads(
        (PLUGIN / "hooks" / "claude-hooks.json").read_text(encoding="utf-8"))
    commands: list[tuple[str, str]] = []
    for event, groups in document["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                commands.append((event, handler["command"]))
    assert len(commands) == 11
    assert all("claude_hook_guard.py" in command for _, command in commands)
    capture_events = [event for event, command in commands
                      if "claude_hook_guard.py\" capture " in command]
    assert capture_events == ["Stop", "SessionEnd"]
    print("PASS test_every_claude_handler_is_guarded_and_only_boundaries_capture")


def test_large_fallback_payload_uses_file_backed_stdin():
    seen: list[bytes] = []
    original = capture.subprocess.Popen

    def fake_popen(_args, *, stdin, **_kwargs):
        seen.append(stdin.read())

    capture.subprocess.Popen = fake_popen
    raw = b"x" * 1_000_000
    try:
        guard._spawn_cursor_flush(raw, "stop")
    finally:
        capture.subprocess.Popen = original
    assert seen == [raw]
    print("PASS test_large_fallback_payload_uses_file_backed_stdin")


def test_exact_imported_stop_hook_never_runs_claude_flusher():
    if os.name == "nt":
        # claude-hooks.json uses POSIX shell syntax; Windows host launch is a
        # separate host-level smoke, while classification/routing above remain
        # native-Windows coverage.
        print("SKIP test_exact_imported_stop_hook_never_runs_claude_flusher "
              "(POSIX hook command)")
        return
    document = json.loads(
        (PLUGIN / "hooks" / "claude-hooks.json").read_text(encoding="utf-8"))
    command = document["hooks"]["Stop"][0]["hooks"][0]["command"]
    with tempfile.TemporaryDirectory() as td:
        env = {
            **os.environ,
            "HOME": td,
            "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
            # Cursor may export Claude variables while running compatibility
            # hooks; their presence must not change the classification.
            "CLAUDE_PROJECT_DIR": "/tmp/memhub-cursor-probe",
        }
        result = subprocess.run(
            ["bash", "-c", command], input=json.dumps(FIXTURE), text=True,
            capture_output=True, env=env, timeout=10)
        assert result.returncode == 0, result.stderr
        assert not (Path(td) / ".config" / "memhub-plugin" /
                    "turnflush").exists()
    print("PASS test_exact_imported_stop_hook_never_runs_claude_flusher")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
