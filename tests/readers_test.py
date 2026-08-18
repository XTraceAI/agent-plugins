#!/usr/bin/env python3
"""Per-host reader suite: transform correctness, locate semantics, host
sniffing, and the cross-reader canonical contract.

The readers are the multi-host normalization boundary — every host's sessions
become Claude-shaped records here, and nothing downstream knows hosts exist.
So this suite pins three things:

1. the Codex transform (moved from codex/codex_to_claude.py — that file's
   shim-based suite still runs, proving the shims; THIS file is where the
   coverage lives and grows);
2. locate() semantics per host — exact-id matching, ambiguity refusal,
   'latest';
3. the contract: every reader's to_canonical() output passes ONE shared
   structural validator (readers.validate_canonical). A future reader
   (cursor) gets added to CONTRACT_FIXTURES and inherits the same bar.

Run: python3 readers_test.py   (stdlib only)
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import readers  # noqa: E402
from readers import claude, codex  # noqa: E402


def _line(t, payload):
    return {"timestamp": "2026-01-01T00:00:00Z", "type": t, "payload": payload}


CODEX_SYNTH = [
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
    _line("event_msg", {"type": "task_complete", "last_agent_message": "Fixed it"}),
]

CLAUDE_SYNTH = [
    {"type": "user", "cwd": "/repo/proj",
     "message": {"role": "user", "content": "Fix the bug"}},
    {"type": "assistant", "cwd": "/repo/proj",
     "message": {"role": "assistant", "content": [
         {"type": "tool_use", "id": "toolu_1", "name": "Bash",
          "input": {"command": "ls"}}]}},
    {"type": "user", "cwd": "/repo/proj",
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file.py"}]}},
    {"type": "assistant", "cwd": "/repo/proj",
     "message": {"role": "assistant", "content": [
         {"type": "text", "text": "Done."}]}},
]


def _write_jsonl(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_codex_transform():
    recs, meta = codex.rollout_to_claude_records(CODEX_SYNTH)
    assert meta["session_id"] == "sess-abc" and meta["cwd"] == "/repo/proj", meta
    assert meta["model"] == "gpt-5.3-codex", meta
    assert meta["title"] == "Fix the bug", meta   # user's opening ask wins
    assert meta["host"] == "codex", meta
    # banner + user + thinking + text + tool_use + tool_result
    # (developer msg + AGENTS.md msg + event_msg dupes all dropped)
    kinds = []
    for r in recs:
        c = r["message"]["content"]
        kinds.append(f"{r['message']['role']}:" +
                     ("text" if isinstance(c, str) else c[0]["type"]))
    assert kinds == ["user:text", "user:text", "assistant:thinking",
                     "assistant:text", "assistant:tool_use", "user:tool_result"], kinds
    for r in recs:  # every record carries cwd for namespace resolution
        assert r.get("cwd") == "/repo/proj", r
    assert recs[0]["message"]["content"].startswith("[Imported from OpenAI Codex"), recs[0]
    print("PASS test_codex_transform")


def test_codex_missing_call_id_orphans_uniquely():
    roll = [
        _line("session_meta", {"id": "s", "cwd": "/x"}),
        _line("response_item", {"type": "function_call", "name": "f",
                                "arguments": "{}"}),                 # no call_id
        _line("response_item", {"type": "function_call_output", "output": "a"}),
        _line("response_item", {"type": "function_call_output", "output": "b"}),
    ]
    recs, _ = codex.rollout_to_claude_records(roll)
    ids = [recs[1]["message"]["content"][0]["id"],
           recs[2]["message"]["content"][0]["tool_use_id"],
           recs[3]["message"]["content"][0]["tool_use_id"]]
    assert all(ids) and len(set(ids)) == 3, ids   # never None, never mispaired
    print("PASS test_codex_missing_call_id_orphans_uniquely")


def test_clean_user_text():
    assert codex.clean_user_text("# AGENTS.md instructions for /x\n...") is None
    assert codex.clean_user_text("<environment_context>\n</environment_context>") is None
    ide = ("# Context from my IDE setup:\n\n## Open tabs:\n- a.py\n\n"
           "## My request for Codex:\nFix the flaky test\n\n")
    assert codex.clean_user_text(ide) == "Fix the flaky test"
    assert codex.clean_user_text("# Context from my IDE setup:\n\n## Open tabs:") is None
    assert codex.clean_user_text("just do the thing") == "just do the thing"
    print("PASS test_clean_user_text")


def test_rollout_uuid():
    p = ("/x/2026/02/17/rollout-2026-02-17T17-06-25-"
         "019c6e48-b66c-7881-9301-99c87fc66cf6.jsonl")
    assert codex.rollout_uuid(p) == "019c6e48-b66c-7881-9301-99c87fc66cf6"
    assert codex.rollout_uuid("/x/not-a-rollout.jsonl") is None
    assert codex.rollout_uuid("/x/rollout-2026-partial.jsonl") is None
    print("PASS test_rollout_uuid")


def test_claude_reader_identity_and_locate():
    with tempfile.TemporaryDirectory() as td:
        projects = Path(td) / "projects"
        f = _write_jsonl(projects / "-repo-proj" / "sess-1.jsonl", CLAUDE_SYNTH)
        _write_jsonl(projects / "-repo-proj" / "subagents" / "sub-1.jsonl",
                     CLAUDE_SYNTH)  # subdir = subagent transcript, never listed
        old = claude._PROJECTS
        claude._PROJECTS = projects
        try:
            recs, meta = claude.to_canonical(f)
            assert recs == CLAUDE_SYNTH, "identity transform must not alter records"
            assert meta == {"session_id": "sess-1", "cwd": "/repo/proj",
                            "title": None, "host": "claude"}, meta

            assert [s["id"] for s in claude.list_sessions()] == ["sess-1"]

            hit, err = claude.locate("sess-1")
            assert hit == f and not err
            hit, err = claude.locate("latest")
            assert hit == f
            hit, err = claude.locate("nope")
            assert hit is None and "no Claude session" in err

            # same stem under two projects → refuse, never guess a brain
            _write_jsonl(projects / "-other" / "sess-1.jsonl", CLAUDE_SYNTH)
            hit, err = claude.locate("sess-1")
            assert hit is None and "ambiguous" in err, err
        finally:
            claude._PROJECTS = old
    print("PASS test_claude_reader_identity_and_locate")


def test_codex_locate():
    with tempfile.TemporaryDirectory() as td:
        sessions = Path(td) / "sessions"
        uuid = "019c6e48-b66c-7881-9301-99c87fc66cf6"
        f = _write_jsonl(sessions / "2026" / "01" / "02" /
                         f"rollout-2026-01-02T03-04-05-{uuid}.jsonl", CODEX_SYNTH)
        old = codex._SESSIONS
        codex._SESSIONS = sessions
        try:
            hit, err = codex.locate(uuid)
            assert hit == f and not err, err
            hit, err = codex.locate("latest")
            assert hit == f
            # partial id must NOT match (wrong-session fold-forward risk)
            hit, err = codex.locate(uuid[:8])
            assert hit is None and "no Codex rollout" in err, err
            assert [s["id"] for s in codex.list_sessions()] == [uuid]
        finally:
            codex._SESSIONS = old
    print("PASS test_codex_locate")


def test_sniff():
    assert readers.sniff("/x/.codex/sessions/2026/rollout-a.jsonl") == "codex"
    assert readers.sniff("rollout-2026-01-01T00-00-00-abc.jsonl") == "codex"
    assert readers.sniff(str(Path.home() / ".claude/projects/-x/s.jsonl")) == "claude"
    assert readers.sniff("latest") is None       # ambiguous → caller must pick
    assert readers.sniff("0199-bare-id") is None
    print("PASS test_sniff")


def _claude_fixture():
    with tempfile.TemporaryDirectory() as td:
        f = _write_jsonl(Path(td) / "s.jsonl", CLAUDE_SYNTH)
        return claude.to_canonical(f)


CONTRACT_FIXTURES = {
    "claude": _claude_fixture,
    "codex": lambda: codex.rollout_to_claude_records(CODEX_SYNTH),
}


def test_contract_every_reader_emits_canonical():
    """The cross-reader contract: one validator, every reader passes it.

    A new host's reader joins by adding a fixture here — the test then holds
    it to the same structural bar with no new assertions to write."""
    for host, fixture in CONTRACT_FIXTURES.items():
        records, meta = fixture()
        problems = readers.validate_canonical(records)
        assert not problems, f"{host}: {problems}"
        for key in ("session_id", "cwd", "title", "host"):
            assert key in meta, f"{host} meta missing {key}: {meta}"
        assert json.dumps(records), f"{host}: records must round-trip as JSON"
    print("PASS test_contract_every_reader_emits_canonical")


def test_validate_canonical_catches_breakage():
    assert readers.validate_canonical([]) == ["no records"]
    bad = [{"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "f", "input": {}}]}}]   # missing id
    assert any("tool_use without id" in p for p in readers.validate_canonical(bad))
    print("PASS test_validate_canonical_catches_breakage")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
