#!/usr/bin/env python3
"""Regression tests for Cursor importing MemHub's Claude hooks."""
from __future__ import annotations

import builtins
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

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
    assert len(commands) == 19   # + UserPromptSubmit (brain_brief.py prompt),
                                 # + PostToolUse (pr_link_trigger.py); SessionEnd
                                 # carries capture AND the fire flush in ONE
                                 # handler, because they must run in that order.
    assert all("claude_hook_guard.py" in command for _, command in commands)
    capture_events = [event for event, command in commands
                      if "claude_hook_guard.py\" capture " in command]
    assert capture_events == ["Stop", "SessionEnd"]
    print("PASS test_every_claude_handler_is_guarded_and_only_boundaries_capture")


def test_harness_children_are_not_captured_routed_or_rule_checked():
    """The extractor and the post-session review run headless `claude -p`
    INSIDE a repo (harness-tied-memory-spec §4.2/§4.3), so this plugin's hooks
    fire for them. Without suppression a replay's turns are captured into the
    repo brain and the child shows up on the fleet board as an agent nobody
    started — which happened during S0's own measurement runs.

    Suppression means the guard reports "do not run the handler", i.e. a
    NON-ZERO exit, exactly as for a Cursor payload. Every hook command invokes
    the guard as an `if` condition, so the shell command still exits 0: the
    hook does nothing rather than failing.
    """
    claude = {
        "session_id": "harness-child",
        "hook_event_name": "Stop",
        "transcript_path": "/Users/x/.claude/projects/session.jsonl",
    }
    child = {"MEMHUB_HARNESS_CHILD": "1"}
    assert guard.is_harness_child(child)
    assert not guard.is_harness_child({})
    assert not guard.is_harness_child({"MEMHUB_HARNESS_CHILD": ""})

    # Every event, both actions, ordinary Claude payload: never continue.
    for action in ("ignore", "capture"):
        for event in ("SessionStart", "UserPromptSubmit", "PreToolUse",
                      "PostToolUse", "Stop", "SessionEnd"):
            assert not guard.route(action, event, claude, b"{}", child), \
                (action, event)
    # …and without the variable the same payload runs normally.
    assert guard.route("capture", "Stop", claude, b"{}", {})

    # A harness child must not launch the Cursor fallback flush either: that
    # is a capture path too, and "not captured" has to mean every path.
    seen: list = []
    original = guard._spawn_cursor_flush
    guard._spawn_cursor_flush = lambda raw, event: seen.append(event)
    try:
        assert not guard.route("capture", "Stop", FIXTURE, b"{}",
                               {**child, "CURSOR_VERSION": "2026.08.11"})
        assert seen == [], "harness child must produce no capture on any path"
    finally:
        guard._spawn_cursor_flush = original
    print("PASS test_harness_children_are_not_captured_routed_or_rule_checked")


def test_harness_child_suppression_holds_through_the_real_hook_command():
    if os.name == "nt":
        print("SKIP test_harness_child_suppression_holds_through_the_real_hook_command "
              "(POSIX hook command)")
        return
    document = json.loads(
        (PLUGIN / "hooks" / "claude-hooks.json").read_text(encoding="utf-8"))
    command = document["hooks"]["Stop"][0]["hooks"][0]["command"]
    claude = {"session_id": "harness-child", "hook_event_name": "Stop",
              "transcript_path": "/Users/x/.claude/projects/s.jsonl"}
    with tempfile.TemporaryDirectory() as td:
        result = subprocess.run(
            ["bash", "-c", command], input=json.dumps(claude), text=True,
            capture_output=True, timeout=30,
            env={**os.environ, "HOME": td,
                 "CLAUDE_PLUGIN_ROOT": str(PLUGIN),
                 "MEMHUB_HARNESS_CHILD": "1"})
        # The hook itself must still succeed — fail open, do nothing.
        assert result.returncode == 0, result.stderr
        assert not (Path(td) / ".config" / "memhub-plugin" /
                    "turnflush").exists()
    print("PASS test_harness_child_suppression_holds_through_the_real_hook_command")


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


def test_missing_cursor_launcher_never_disables_claude_hooks():
    original_import = builtins.__import__

    def fail_cursor_capture(name, *args, **kwargs):
        if name == "cursor_capture":
            raise ImportError("partial install")
        return original_import(name, *args, **kwargs)

    builtins.__import__ = fail_cursor_capture
    with tempfile.TemporaryDirectory() as temp:
        try:
            with mock.patch.object(guard.Path, "home",
                                   return_value=Path(temp)):
                assert not guard.route("capture", "Stop", FIXTURE, b"{}", {})
        finally:
            builtins.__import__ = original_import
        log = (Path(temp) / ".config" / "memhub-plugin" /
               "cursorflush" / "log").read_text(encoding="utf-8")
        assert "guard import failed (ImportError('partial install'))" in log
    print("PASS test_missing_cursor_launcher_never_disables_claude_hooks")


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


def test_session_end_flushes_fires_after_capture_and_regardless_of_it():
    """The fire flush must run STRICTLY after capture, in the same handler.

    A rule that fires at SessionStart reaches the server long before the
    conversation exists, so its row is stored with no session; only a later
    capture can link it, and session end is the last chance a short session
    gets. Claude Code runs every hook for an event in PARALLEL, so two separate
    handlers raced — roughly one session in six ended with its SessionStart
    fires permanently sessionless. One handler, two stages, in order.

    Sequenced with `;`, never `&&`: a capture that fails must not swallow the
    flush. Fires are watermarked in the ledger, so a flush that never runs only
    defers them — but a flush that runs BEFORE capture links nothing.
    """
    if os.name == "nt":
        print("SKIP test_session_end_flushes_fires_after_capture_and_regardless_of_it "
              "(POSIX hook command)")
        return
    document = json.loads(
        (PLUGIN / "hooks" / "claude-hooks.json").read_text(encoding="utf-8"))
    groups = document["hooks"]["SessionEnd"]
    assert len(groups) == 1 and len(groups[0]["hooks"]) == 1
    command = groups[0]["hooks"][0]["command"]

    def order_for(capture_rc: int) -> list[str]:
        with tempfile.TemporaryDirectory() as td:
            scripts = Path(td) / "plugin" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "claude_hook_guard.py").write_text("raise SystemExit(0)\n")
            (scripts / "flush_session.py").write_text(
                "import os, sys\n"
                "open(os.environ['ORDER'], 'a').write('capture\\n')\n"
                f"raise SystemExit({capture_rc})\n")
            (scripts / "rulebook_hook.py").write_text(
                "import os\n"
                "open(os.environ['ORDER'], 'a').write('fire-flush\\n')\n")
            order = Path(td) / "order"
            order.touch()
            result = subprocess.run(
                ["bash", "-c", command], input=json.dumps(FIXTURE), text=True,
                capture_output=True, timeout=30,
                env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(scripts.parent),
                     "ORDER": str(order)})
            assert result.returncode == 0, result.stderr
            return order.read_text().split()

    assert order_for(0) == ["capture", "fire-flush"]
    # …and a capture that fails still leaves the fires flushed.
    assert order_for(1) == ["capture", "fire-flush"]
    print("PASS test_session_end_flushes_fires_after_capture_and_regardless_of_it")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
