#!/usr/bin/env python3
"""Cursor capture gate + identity tests (stdlib only).

``cursor_flush.py``'s network half reuses flush_turn's proven downstream
(redact / auth / brain_resolve / mcp_http), so what needs pinning here is the
part that is NEW: when a hook invocation becomes a server call
(``should_flush``) and how a hook payload names its session
(``session_uuid``). Both are pure functions for exactly this reason.

Run: python3 cursor_capture_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

from cursor_flush import session_uuid, should_flush  # noqa: E402

NOW = 1_787_000_000.0
FRESH = {"a", "b"}          # two blobs in the store
SHIPPED = {"blob_ids": ["a", "b"], "last_flush_at": NOW - 300}
STALE = {"blob_ids": ["a"], "last_flush_at": NOW - 300}   # "b" is new


def test_no_new_blobs_never_flushes():
    for event in ("stop", "afterFileEdit", "beforeShellExecution", "beforeSubmitPrompt"):
        assert not should_flush(event, {"command": "git commit -m x"},
                                SHIPPED, FRESH, NOW), event
    print("PASS test_no_new_blobs_never_flushes")


def test_milestone_gates_shell_events():
    assert should_flush("beforeShellExecution", {"command": "git commit -m x"},
                        STALE, FRESH, NOW)
    assert should_flush("beforeShellExecution", {"command": "gh pr create -f"},
                        STALE, FRESH, NOW)
    # ordinary shell traffic stays quiet even with new content
    assert not should_flush("beforeShellExecution", {"command": "ls -la"},
                            STALE, FRESH, NOW)
    # "commit" as a mere substring of another word must not trigger
    assert not should_flush("beforeShellExecution", {"command": "echo recommitted"},
                            STALE, FRESH, NOW)
    print("PASS test_milestone_gates_shell_events")


def test_edit_debounce():
    recent = {"blob_ids": ["a"], "last_flush_at": NOW - 5}
    assert not should_flush("afterFileEdit", {}, recent, FRESH, NOW)
    assert should_flush("afterFileEdit", {}, STALE, FRESH, NOW)   # 300s ago
    assert should_flush("afterFileEdit", {}, {}, FRESH, NOW)      # never flushed
    print("PASS test_edit_debounce")


def test_turn_boundaries_flush_on_new_content():
    assert should_flush("stop", {}, STALE, FRESH, NOW)
    assert should_flush("beforeSubmitPrompt", {}, STALE, FRESH, NOW)
    assert not should_flush("unknownEvent", {}, STALE, FRESH, NOW)
    print("PASS test_turn_boundaries_flush_on_new_content")


def test_session_uuid_sources():
    assert session_uuid({"session_id": "u-1"}) == "u-1"
    assert session_uuid({"conversation_id": "c-2"}) == "c-2"
    # falls back to the transcript_path's session directory
    tp = "/x/.cursor/projects/slug/agent-transcripts/u-3/u-3.jsonl"
    assert session_uuid({"transcript_path": tp}) == "u-3"
    assert session_uuid({"session_id": "  "}) is None   # blank → no identity
    assert session_uuid({}) is None
    print("PASS test_session_uuid_sources")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
