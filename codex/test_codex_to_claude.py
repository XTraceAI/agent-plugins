#!/usr/bin/env python3
"""Tests for codex_to_claude. Run: python3 codex/test_codex_to_claude.py

Covers each Codex record type against exact Claude-shaped output, plus a smoke
run over a real rollout if one is present under ~/.codex/sessions."""
from __future__ import annotations

import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from codex_to_claude import (  # noqa: E402
    rollout_to_claude_records, load_rollout, clean_user_text,
)


def test_clean_user_text():
    assert clean_user_text("# AGENTS.md instructions for /x\n...") is None
    assert clean_user_text("<environment_context>\n <cwd>/x</cwd>\n</environment_context>") is None
    # IDE wrapper: extract the real ask under the request heading
    ide = ("# Context from my IDE setup:\n\n## Open tabs:\n- a.py\n\n"
           "## My request for Codex:\nFix the flaky test\n\n")
    assert clean_user_text(ide) == "Fix the flaky test"
    # IDE context with no request heading = context-only refresh → dropped
    assert clean_user_text("# Context from my IDE setup:\n\n## Open tabs:\n- a.py") is None
    assert clean_user_text("just do the thing") == "just do the thing"
    assert clean_user_text("   ") is None
    print("PASS test_clean_user_text")


def _line(t, payload):
    return {"timestamp": "2026-01-01T00:00:00Z", "type": t, "payload": payload}


SYNTH = [
    _line("session_meta", {"id": "sess-abc", "cwd": "/repo/proj",
                           "originator": "codex_cli", "cli_version": "0.1"}),
    _line("response_item", {"type": "message", "role": "developer",
                            "content": [{"type": "input_text", "text": "<permissions>"}]}),
    _line("response_item", {"type": "message", "role": "user",
                            "content": [{"type": "input_text",
                                         "text": "# AGENTS.md instructions for /repo\n..."}]}),
    _line("turn_context", {"model": "gpt-5.3-codex"}),
    _line("event_msg", {"type": "user_message", "message": "duplicate UI text — ignored"}),
    _line("response_item", {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": "Fix the bug"}]}),
    _line("response_item", {"type": "reasoning",
                            "summary": [{"type": "summary_text", "text": "**Planning**"}],
                            "encrypted_content": "OPAQUE=="}),
    _line("response_item", {"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "On it."}]}),
    _line("response_item", {"type": "function_call", "name": "exec_command",
                            "arguments": '{"cmd":"ls"}', "call_id": "call_1"}),
    _line("response_item", {"type": "function_call_output", "call_id": "call_1",
                            "output": "file.py"}),
    _line("response_item", {"type": "custom_tool_call", "name": "apply_patch",
                            "input": "*** Begin Patch\n...", "call_id": "call_2"}),
    _line("response_item", {"type": "custom_tool_call_output", "call_id": "call_2",
                            "output": '{"output":"Success"}'}),
    _line("event_msg", {"type": "task_complete", "last_agent_message": "Fixed the bug in file.py"}),
]


def test_synthetic():
    recs, meta = rollout_to_claude_records(SYNTH)

    assert meta["session_id"] == "sess-abc", meta
    assert meta["cwd"] == "/repo/proj", meta
    assert meta["model"] == "gpt-5.3-codex", meta
    assert meta["title"] == "Fix the bug", meta  # prefers the user's opening ask

    # banner + user + thinking + text + tool_use + tool_result + tool_use + tool_result
    # (developer msg + AGENTS.md msg + event_msg dupes all dropped)
    kinds = [_kind(r) for r in recs]
    assert kinds == [
        "user:text", "user:text",           # banner, real user turn
        "assistant:thinking", "assistant:text",
        "assistant:tool_use", "user:tool_result",
        "assistant:tool_use", "user:tool_result",
    ], kinds

    # every record carries cwd (namespace resolution) and nested message
    for r in recs:
        assert r.get("cwd") == "/repo/proj", r
        assert "message" in r and "role" in r["message"], r

    tu = recs[4]["message"]["content"][0]
    assert tu == {"type": "tool_use", "id": "call_1", "name": "exec_command",
                  "input": {"cmd": "ls"}}, tu
    # custom_tool_call raw (non-JSON) input is wrapped, not lost
    tu2 = recs[6]["message"]["content"][0]
    assert tu2["name"] == "apply_patch" and tu2["id"] == "call_2"
    assert tu2["input"] == {"input": "*** Begin Patch\n..."}, tu2
    tr = recs[5]["message"]["content"][0]
    assert tr == {"type": "tool_result", "tool_use_id": "call_1",
                  "content": "file.py"}, tr

    # the second real user turn is the banner-free "Fix the bug"
    assert recs[1]["message"]["content"] == "Fix the bug", recs[1]
    # reasoning kept summary only, dropped encrypted_content
    assert recs[2]["message"]["content"][0] == {"type": "thinking",
                                                "thinking": "**Planning**"}
    print("PASS test_synthetic")


def test_missing_call_id():
    """Malformed tool records without call_id must never emit id=None, and an
    id-less output must ORPHAN with a unique id rather than mispair — including
    two id-less outputs not collapsing onto one call."""
    roll = [
        _line("session_meta", {"id": "s", "cwd": "/x"}),
        _line("response_item", {"type": "function_call", "name": "f",
                               "arguments": "{}"}),  # no call_id
        _line("response_item", {"type": "function_call_output", "output": "a"}),  # no call_id
        _line("response_item", {"type": "function_call_output", "output": "b"}),  # no call_id
    ]
    recs, _ = rollout_to_claude_records(roll)
    tu = recs[1]["message"]["content"][0]
    tr1 = recs[2]["message"]["content"][0]
    tr2 = recs[3]["message"]["content"][0]
    ids = [tu["id"], tr1["tool_use_id"], tr2["tool_use_id"]]
    assert all(i for i in ids), ids
    assert len(set(ids)) == 3, ids                # all distinct: no mispair, no dup-link
    print("PASS test_missing_call_id")


def test_session_arg():
    from codex_notify import _session_arg
    assert _session_arg({"type": "agent-turn-complete"}) == "latest"
    assert _session_arg({"session-id": "019c6e48-..."}) == "019c6e48-..."
    assert _session_arg({"rollout_path": "/x/rollout-...jsonl"}) == "/x/rollout-...jsonl"
    # rollout path preferred over a bare id when both present
    assert _session_arg({"rollout-path": "/p.jsonl", "session_id": "u"}) == "/p.jsonl"
    assert _session_arg({"session_id": "   "}) == "latest"  # blank ignored
    print("PASS test_session_arg")


def test_rollout_uuid():
    from import_codex_session import rollout_uuid
    p = "/x/2026/02/17/rollout-2026-02-17T17-06-25-019c6e48-b66c-7881-9301-99c87fc66cf6.jsonl"
    assert rollout_uuid(p) == "019c6e48-b66c-7881-9301-99c87fc66cf6"
    assert rollout_uuid("/x/not-a-rollout.jsonl") is None
    assert rollout_uuid("/x/rollout-2026-partial.jsonl") is None
    print("PASS test_rollout_uuid")


def _kind(r):
    m = r["message"]
    c = m["content"]
    if isinstance(c, str):
        return f"{m['role']}:text"
    return f"{m['role']}:{c[0]['type']}"


def _looks_agentic(recs):
    """The detector wants records with a nested message AND tool-call/tool-result
    blocks. Assert both are present."""
    has_msg = any("message" in r for r in recs)
    has_tool = any(isinstance(r["message"]["content"], list)
                   and r["message"]["content"][0].get("type") in ("tool_use", "tool_result")
                   for r in recs if isinstance(r.get("message"), dict)
                   and isinstance(r["message"].get("content"), list))
    return has_msg and has_tool


def test_real_smoke():
    files = sorted(glob.glob(os.path.expanduser(
        "~/.codex/sessions/**/rollout-*.jsonl"), recursive=True),
        key=os.path.getsize, reverse=True)
    if not files:
        print("SKIP test_real_smoke (no local Codex sessions)")
        return
    big = files[0]
    recs, meta = rollout_to_claude_records(load_rollout(big))
    assert recs, "no records produced from real rollout"
    assert _looks_agentic(recs), "output would not trip the agentic detector"
    # round-trips as JSON (what import_conversation receives)
    json.dumps(recs)
    n_tool = sum(1 for r in recs if isinstance(r["message"].get("content"), list)
                 and r["message"]["content"][0].get("type") == "tool_use")
    print(f"PASS test_real_smoke ({os.path.basename(big)}): "
          f"{len(recs)} records, {n_tool} tool_use, cwd={meta['cwd']}, "
          f"title={meta['title']!r}")


if __name__ == "__main__":
    test_clean_user_text()
    test_synthetic()
    test_missing_call_id()
    test_session_arg()
    test_rollout_uuid()
    test_real_smoke()
    print("ALL PASS")
