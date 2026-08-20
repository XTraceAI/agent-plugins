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

from cursor_flush import _persisted, session_uuid, should_flush  # noqa: E402

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
    # Command POSITION, not mention: naming a milestone inside an argument
    # must not fire a whole-transcript send
    for quiet in ("echo 'remember to git commit later'",
                  "grep 'git commit' notes.md",
                  "man git commit",
                  "git log --oneline | grep commit",
                  "echo 'sudo git commit'",
                  "cat prcommit.txt",
                  "git status", "git push", "gh repo view",
                  # the reviewer's edge cases: refs/branches named after the
                  # milestone words must not fire
                  "git push origin commit-branch", "git branch pr-123",
                  "git checkout commit", "git log --oneline commit",
                  "git checkout -b pr-fix", "git diff --stat commit",
                  # read-only gh pr subcommands are not milestones
                  "gh pr list", "gh pr view 12", "gh pr checks", "gh pr diff"):
        assert not should_flush("beforeShellExecution", {"command": quiet},
                                STALE, FRESH, NOW), quiet
    # ...while the wrapper and chained forms agents actually emit still fire
    for loud in ('bash -lc "git commit -m x"',
                 "cd repo && git commit -m x",
                 "sh -c 'gh pr create -f'",
                 # options BETWEEN tool and subcommand: `git -C <dir> commit`
                 # is a routine agent form that adjacency silently skipped
                 "git -C /tmp/x commit -m y",
                 "git --no-pager commit",
                 "gh --repo o/r pr create",
                 # leading wrappers
                 "sudo git commit", "env FOO=1 git commit",
                 "time git commit -m z",
                 "git -c user.name=x commit", "gh pr merge 12 --squash"):
        assert should_flush("beforeShellExecution", {"command": loud},
                            STALE, FRESH, NOW), loud
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


def test_import_must_be_confirmed_before_advancing():
    """A returned call is not a persisted call.

    This backend has shipped a 200-with-nothing-stored mode (records
    dedup-registered without persisting: ack_through null), which is what hid
    Cursor sessions for months. Advancing the watermark on such a reply loses
    the conversation outright when it is a session's LAST flush.
    """
    import json as _json
    import types

    def _res(structured=None, texts=(), is_error=False):
        return types.SimpleNamespace(
            structuredContent=structured, isError=is_error,
            content=[types.SimpleNamespace(text=t) for t in texts])

    confirmed = [
        _res({"conversation_id": "cursor-x", "ack_through": "u1"}),
        _res({"result": {"conversation_id": "c", "ack_through": "u"}}),
        _res(None, [_json.dumps({"conversation_id": "c", "ack_through": "u"})]),
    ]
    # a diagnostic JSON block AHEAD of the ack must not be mistaken for it —
    # that read a healthy server as failing and re-uploaded every event
    confirmed.append(_res(None, [
        _json.dumps({"level": "info", "msg": "queued"}),
        _json.dumps({"conversation_id": "c", "ack_through": "u"})]))
    confirmed.append(_res(None, ["saved!", _json.dumps(
        {"conversation_id": "c", "ack_through": "u"})]))
    unconfirmed = [
        _res({"conversation_id": "c", "ack_through": None, "records_dropped": 6}),
        _res({"conversation_id": "c", "ack_through": "u"}, is_error=True),
        _res(None, ["not json"]),
        _res({"conversation_id": "c"}),          # server predating ack_through
    ]
    for r in confirmed:
        assert _persisted(r), r
    for r in unconfirmed:
        assert not _persisted(r), r
    print("PASS test_import_must_be_confirmed_before_advancing")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
