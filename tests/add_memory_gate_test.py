#!/usr/bin/env python3
"""add_memory is denied exactly when this plugin is already capturing the session.

Run: python3 tests/add_memory_gate_test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "memhub"
SCRIPT = PLUGIN / "scripts" / "add_memory_gate.py"
sys.path.insert(0, str(PLUGIN / "scripts"))

# Redirect HOME before importing, so the credential stores these tests write
# (and the ones capture_health reads at import time) are scratch, never real.
_TMP_HOME = tempfile.mkdtemp(prefix="add-memory-gate-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
HOST = "api.staging.memhub.xtrace.ai"
os.environ["MEMHUB_MCP_BASE_URL"] = f"https://{HOST}"
os.environ.pop("MEMHUB_TOKEN", None)
os.environ.pop("MEMHUB_TURN_FLUSH", None)

import add_memory_gate as gate  # noqa: E402
import capture_health as ch  # noqa: E402
import pak  # noqa: E402

# The names the tool actually arrived under in live sessions: a claude.ai
# MemHub connector, and this plugin's own server in both builds.
OBSERVED_NAMES = (
    "mcp__claude_ai_Xtrace__add_memory",
    "mcp__plugin_memhub-staging_memhub__add_memory",
    "mcp__plugin_memhub_memhub__add_memory",
)


def _payload(name: str = OBSERVED_NAMES[0], **overrides) -> dict:
    payload = {
        "session_id": "s-1",
        "hook_event_name": "PreToolUse",
        "transcript_path": "/Users/x/.claude/projects/p/s-1.jsonl",
        "tool_name": name,
        "tool_input": {"user_message": "q", "assistant_message": "a"},
    }
    payload.update(overrides)
    return payload


def _with_key() -> None:
    pak.save(ch._mcp_url_for(HOST), {"secret": "mhk_x", "label": "test"})


def _clear_credentials() -> None:
    pak.forget(ch._mcp_url_for(HOST))
    token = ch.CACHE_DIR / f"tokens-{HOST}.json"
    if token.exists():
        token.unlink()


def _denies(out: dict | None) -> bool:
    return bool(out) and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_denies_when_capture_is_live():
    _clear_credentials()
    _with_key()
    out = gate.decide(_payload())
    assert _denies(out), out
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "save_artifact" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "save_artifact" in out["systemMessage"]
    print("PASS test_denies_when_capture_is_live")


def test_every_observed_server_name_is_gated_and_nothing_else():
    _clear_credentials()
    _with_key()
    for name in OBSERVED_NAMES:
        assert _denies(gate.decide(_payload(name))), name
    # A tool that merely shares the prefix, a built-in, and another server's
    # add_memory with a different signature are not MemHub's add_memory.
    for name in ("mcp__notes__add_memory_note", "Bash", "mcp__memhub__save_artifact"):
        assert gate.decide(_payload(name)) is None, name
    assert gate.decide(_payload(tool_input={"text": "x"})) is None
    assert gate.decide(_payload(tool_input="user_message")) is None
    print("PASS test_every_observed_server_name_is_gated_and_nothing_else")


def test_allows_when_capture_is_switched_off():
    _clear_credentials()
    _with_key()
    for value in ("0", "off", "FALSE", " false "):
        assert gate.decide(_payload(), {"MEMHUB_TURN_FLUSH": value}) is None, value
    # Any other value leaves capture on.
    assert _denies(gate.decide(_payload(), {"MEMHUB_TURN_FLUSH": "1"}))
    print("PASS test_allows_when_capture_is_switched_off")


def test_allows_without_a_working_credential():
    _clear_credentials()
    assert gate.decide(_payload()) is None  # never logged in
    # A lapsed key with no OAuth fallback captures nothing either.
    pak.save(ch._mcp_url_for(HOST), {"secret": "mhk_x", "label": "test",
                                     "expires_at": "2000-01-01T00:00:00Z"})
    assert gate.decide(_payload()) is None
    # An OAuth token that can renew is a working credential.
    _clear_credentials()
    ch.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (ch.CACHE_DIR / f"tokens-{HOST}.json").write_text(json.dumps({
        "access_token": "opaque", "refresh_token": "r"}), encoding="utf-8")
    assert _denies(gate.decide(_payload()))
    print("PASS test_allows_without_a_working_credential")


def test_allows_without_a_transcript():
    _clear_credentials()
    _with_key()
    for transcript in (None, "", "   "):
        payload = _payload(transcript_path=transcript)
        assert gate.decide(payload) is None, transcript
    print("PASS test_allows_without_a_transcript")


def test_registered_for_every_observed_name_behind_the_guard():
    doc = json.loads((PLUGIN / "hooks" / "claude-hooks.json")
                     .read_text(encoding="utf-8"))
    entries = [(group["matcher"], hook["command"])
               for group in doc["hooks"]["PreToolUse"]
               for hook in group["hooks"]
               if "add_memory_gate.py" in hook["command"]]
    assert len(entries) == 1, entries
    matcher, command = entries[0]
    for name in OBSERVED_NAMES:
        assert re.search(matcher, name), name
    for name in ("mcp__notes__add_memory_note", "Bash", "add_memory"):
        assert not re.search(matcher, name), name
    # Cursor imports these hooks too; the guard must run first.
    assert command.index("claude_hook_guard.py") < command.index("add_memory_gate.py")
    assert "ignore PreToolUse" in command
    print("PASS test_registered_for_every_observed_name_behind_the_guard")


def _run(stdin: str, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT)], input=stdin, capture_output=True,
        text=True, timeout=30, env=dict(os.environ, **env))


def test_script_denies_and_fails_open():
    _clear_credentials()
    _with_key()
    done = _run(json.dumps(_payload()))
    assert done.returncode == 0, done.stderr
    assert _denies(json.loads(done.stdout)), done.stdout
    for garbage in ("{{{", "", "[]", json.dumps({"tool_name": 3})):
        done = _run(garbage)
        assert done.returncode == 0 and done.stdout == "", (garbage, done)
    print("PASS test_script_denies_and_fails_open")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
