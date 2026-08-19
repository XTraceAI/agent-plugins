#!/usr/bin/env python3
"""Codex capture gate + identity tests (stdlib only).

Same shape as cursor_capture_test: the network half is the shared, proven
machinery, so what needs pinning is what is NEW — when a hook invocation
becomes a server call (``should_flush``, keyed on rollout byte growth) and
how a payload names its rollout (``locate_rollout``: path preferred, bare id
resolved through the reader, neither → refuse rather than guess).

Run: python3 codex_capture_test.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import codex_flush  # noqa: E402

GROWN, SHIPPED = 2_000, {"rollout_size": 2_000}
STALE = {"rollout_size": 1_000}   # 1000 new bytes since last flush


def test_no_growth_never_flushes():
    for event in ("Stop", "PostToolUse"):
        assert not codex_flush.should_flush(
            event, {"tool_input": {"command": "git commit -m x"}}, SHIPPED, GROWN), event
    print("PASS test_no_growth_never_flushes")


def test_stop_ships_growth():
    assert codex_flush.should_flush("Stop", {}, STALE, GROWN)
    assert codex_flush.should_flush("Stop", {}, {}, GROWN)   # first flush
    print("PASS test_stop_ships_growth")


def test_milestone_gates_posttooluse():
    # string-form command (Claude-shaped payload)
    assert codex_flush.should_flush(
        "PostToolUse", {"tool_input": {"command": "git commit -m x"}}, STALE, GROWN)
    # list-form command (Codex Responses shape) — normalized by join
    assert codex_flush.should_flush(
        "PostToolUse", {"tool_input": {"command": ["gh", "pr", "create", "-f"]}},
        STALE, GROWN)
    assert not codex_flush.should_flush(
        "PostToolUse", {"tool_input": {"command": "ls -la"}}, STALE, GROWN)
    assert not codex_flush.should_flush(
        "PostToolUse", {"tool_input": {"command": "echo recommitted"}}, STALE, GROWN)
    assert not codex_flush.should_flush("UserPromptSubmit", {}, STALE, GROWN)
    print("PASS test_milestone_gates_posttooluse")


def test_locate_rollout_identity():
    uuid = "019c6e48-b66c-7881-9301-99c87fc66cf6"
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / f"rollout-2026-01-02T03-04-05-{uuid}.jsonl"
        f.write_text('{"type":"session_meta","payload":{"id":"x"}}\n', encoding="utf-8")
        # transcript_path preferred, uuid lifted from the filename
        p, sid = codex_flush.locate_rollout({"transcript_path": str(f)})
        assert p == f and sid == uuid, (p, sid)
        # neither field → refuse (never guess 'latest')
        p, sid = codex_flush.locate_rollout({})
        assert p is None and sid is None
    print("PASS test_locate_rollout_identity")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
