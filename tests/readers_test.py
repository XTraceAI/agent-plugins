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
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import readers  # noqa: E402
from readers import claude, codex, cursor  # noqa: E402


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
    _line("response_item", {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": (
                                "<recommended_plugins>\nplugin list\n"
                                "</recommended_plugins>\n"
                                "# AGENTS.md instructions\n\n"
                                "<INSTRUCTIONS>\nrepo rule\n</INSTRUCTIONS>\n"
                                "<environment_context>\nrepo metadata\n"
                                "</environment_context>"
                            )}]}),
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
    _line("event_msg", {"type": "token_count", "info": {
        "total_token_usage": {
            "input_tokens": 100, "cached_input_tokens": 60,
            "cache_write_input_tokens": 10, "output_tokens": 20,
            "reasoning_output_tokens": 5, "total_tokens": 120,
        },
        "last_token_usage": {
            "input_tokens": 100, "cached_input_tokens": 60,
            "cache_write_input_tokens": 10, "output_tokens": 20,
            "reasoning_output_tokens": 5, "total_tokens": 120,
        },
    }}),
    _line("event_msg", {"type": "task_complete", "last_agent_message": "Fixed it"}),
]

# Real Claude records carry uuid + timestamp natively — the server's agentic
# parser SKIPS uuid-less records (replay-dedup key) and dates turns from
# timestamp, so the identity reader's fixtures must carry both, like reality.
CLAUDE_SYNTH = [
    {"type": "user", "cwd": "/repo/proj", "uuid": "u-1",
     "timestamp": "2026-08-18T10:00:00.000Z",
     "message": {"role": "user", "content": "Fix the bug"}},
    {"type": "assistant", "cwd": "/repo/proj", "uuid": "u-2",
     "timestamp": "2026-08-18T10:00:01.000Z",
     "message": {"role": "assistant", "content": [
         {"type": "tool_use", "id": "toolu_1", "name": "Bash",
          "input": {"command": "ls"}}]}},
    {"type": "user", "cwd": "/repo/proj", "uuid": "u-3",
     "timestamp": "2026-08-18T10:00:02.000Z",
     "message": {"role": "user", "content": [
         {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file.py"}]}},
    {"type": "assistant", "cwd": "/repo/proj", "uuid": "u-4",
     "timestamp": "2026-08-18T10:00:03.000Z",
     "message": {"role": "assistant", "content": [
         {"type": "text", "text": "Done."}]}},
]


def _write_jsonl(path: Path, records) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, encoding="utf-8").stdout.strip()


def _git_repo(root: Path) -> Path:
    repo = root / "git-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "MemHub Tests")
    (repo / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "feature/reader-fallback")
    _git(repo, "remote", "add", "origin", "https://github.com/XTraceAI/demo.git")
    return repo


def test_codex_transform():
    recs, meta = codex.rollout_to_claude_records(CODEX_SYNTH)
    assert meta["session_id"] == "sess-abc" and meta["cwd"] == "/repo/proj", meta
    assert meta["model"] == "gpt-5.3-codex", meta
    assert meta["title"] == "Fix the bug", meta   # user's opening ask wins
    assert meta["host"] == "codex", meta
    # user + thinking + text + tool_use + tool_result
    # (developer msg + AGENTS.md/app context + event_msg dupes all dropped)
    kinds = []
    for r in recs:
        c = r["message"]["content"]
        kinds.append(f"{r['message']['role']}:" +
                     ("text" if isinstance(c, str) else c[0]["type"]))
    assert kinds == ["user:text", "assistant:thinking", "assistant:text",
                     "assistant:tool_use", "user:tool_result"], kinds
    for r in recs:  # every record carries cwd for namespace resolution
        assert r.get("cwd") == "/repo/proj", r
    assert recs[0]["message"]["content"] == "Fix the bug", recs[0]
    # 0.27.4 used index 0 for its banner and index 1 for the app context.
    # The real ask keeps legacy index 2 so incremental re-import is deduplicated.
    assert recs[0]["uuid"] == str(uuid.uuid5(
        uuid.NAMESPACE_URL, "memhub:codex:sess-abc:2")), recs[0]
    assert recs[3]["message"]["model"] == "gpt-5.3-codex", recs[3]
    assert recs[3]["message"]["usage"] == {
        "input_tokens": 30,
        "output_tokens": 20,
        "cache_read_input_tokens": 60,
        "cache_creation_input_tokens": 10,
    }, recs[3]
    print("PASS test_codex_transform")


def test_codex_native_git_provenance_is_emitted_without_identity_changes():
    rollout = [dict(record) for record in CODEX_SYNTH]
    rollout[0] = _line("session_meta", {
        **CODEX_SYNTH[0]["payload"],
        "git": {
            "branch": "feature/native-codex",
            "commit_hash": "a" * 40,
            "repository_url": "https://github.com/XTraceAI/agent-plugins.git",
        },
    })
    baseline, _ = codex.rollout_to_claude_records(CODEX_SYNTH)
    records, meta = codex.rollout_to_claude_records(rollout)
    assert meta["git"] == rollout[0]["payload"]["git"]
    assert all(record["gitBranch"] == "feature/native-codex"
               for record in records)
    assert [record["uuid"] for record in records] == [
        record["uuid"] for record in baseline]
    print("PASS test_codex_native_git_provenance_is_emitted_without_identity_changes")


def test_codex_and_cursor_readers_fallback_to_local_git_without_guessing():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        repo = _git_repo(root)
        rollout = [
            _line("session_meta", {"id": "git-fallback", "cwd": str(repo)}),
            _line("response_item", {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Fallback"}],
            }),
        ]
        codex_records, codex_meta = codex.rollout_to_claude_records(rollout)

        sid = "11111111-2222-4333-8444-555555555555"
        transcript = _write_jsonl(root / f"{sid}.jsonl", [{
            "role": "user", "message": {"content": "Fallback"},
        }])
        cursor_records, cursor_meta = cursor.to_canonical(
            transcript, session_id=sid, cwd=str(repo))

        for records, meta in ((codex_records, codex_meta),
                              (cursor_records, cursor_meta)):
            assert meta["git"]["branch"] == "feature/reader-fallback", meta
            assert meta["git"]["repository_url"].endswith("XTraceAI/demo.git")
            assert all(record["gitBranch"] == "feature/reader-fallback"
                       for record in records)

        _git(repo, "checkout", "--detach", "HEAD")
        detached, detached_meta = cursor.to_canonical(
            transcript, session_id=sid, cwd=str(repo))
        assert "branch" not in detached_meta["git"], detached_meta
        assert all("gitBranch" not in record for record in detached)
    print("PASS test_codex_and_cursor_readers_fallback_to_local_git_without_guessing")


def test_codex_usage_uses_cumulative_deltas():
    """Repeated last-usage broadcasts do not double count; a later cumulative
    advance attaches only its delta and cache tokens are not counted as fresh.
    """
    first = {
        "input_tokens": 100, "cached_input_tokens": 60,
        "cache_write_input_tokens": 10, "output_tokens": 20,
        "reasoning_output_tokens": 5, "total_tokens": 120,
    }
    second = {
        "input_tokens": 180, "cached_input_tokens": 100,
        "cache_write_input_tokens": 20, "output_tokens": 35,
        "reasoning_output_tokens": 8, "total_tokens": 215,
    }
    roll = [
        _line("session_meta", {"id": "usage", "cwd": "/x"}),
        _line("turn_context", {"model": "gpt-test"}),
        _line("response_item", {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "one"}]}),
        _line("event_msg", {"type": "token_count", "info": {
            "total_token_usage": first, "last_token_usage": first}}),
        # Codex can rebroadcast unchanged last_token_usage on rate-limit updates.
        _line("event_msg", {"type": "token_count", "info": {
            "total_token_usage": first, "last_token_usage": first}}),
        _line("response_item", {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "two"}]}),
        _line("event_msg", {"type": "token_count", "info": {
            "total_token_usage": second, "last_token_usage": second}}),
    ]
    recs, _ = codex.rollout_to_claude_records(roll)
    assistants = [r for r in recs if r["type"] == "assistant"]
    assert assistants[0]["message"]["usage"] == {
        "input_tokens": 30, "output_tokens": 20,
        "cache_read_input_tokens": 60,
        "cache_creation_input_tokens": 10,
    }, assistants[0]
    assert assistants[1]["message"]["usage"] == {
        "input_tokens": 30, "output_tokens": 15,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 10,
    }, assistants[1]
    print("PASS test_codex_usage_uses_cumulative_deltas")


def test_codex_recovers_wrapped_ask_without_shifting_legacy_ids():
    wrapped = (
        "# AGENTS.md instructions\n\n"
        "<INSTRUCTIONS>\nrules\n</INSTRUCTIONS>\n"
        "<environment_context>\nrepo\n</environment_context>\n"
        "Fix the wrapped bug"
    )
    roll = [
        _line("session_meta", {"id": "recovered", "cwd": "/x"}),
        _line("response_item", {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": wrapped}]}),
        _line("response_item", {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "done"}]}),
    ]
    recs, meta = codex.rollout_to_claude_records(roll)
    assert meta["title"] == "Fix the wrapped bug", meta
    assert [r["message"]["role"] for r in recs] == ["user", "assistant"], recs
    assert recs[0]["message"]["content"] == "Fix the wrapped bug", recs[0]
    assert recs[0]["uuid"] == str(uuid.uuid5(
        uuid.NAMESPACE_URL, "memhub:codex:recovered:recovered-user:1")), recs[0]
    # 0.27.4 dropped the wrapped ask, then assigned legacy index 1 to this
    # assistant. Recovering the ask must not move that acknowledged identity.
    assert recs[1]["uuid"] == str(uuid.uuid5(
        uuid.NAMESPACE_URL, "memhub:codex:recovered:1")), recs[1]
    print("PASS test_codex_recovers_wrapped_ask_without_shifting_legacy_ids")


def test_codex_simple_cli_records_keep_pre_banner_removal_ids():
    roll = [
        _line("session_meta", {"id": "simple", "cwd": "/x"}),
        _line("response_item", {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": "go"}]}),
        _line("response_item", {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "done"}]}),
    ]
    recs, _ = codex.rollout_to_claude_records(roll)
    assert len(recs) == 2, recs
    assert [r["uuid"] for r in recs] == [
        str(uuid.uuid5(uuid.NAMESPACE_URL, "memhub:codex:simple:1")),
        str(uuid.uuid5(uuid.NAMESPACE_URL, "memhub:codex:simple:2")),
    ], recs
    print("PASS test_codex_simple_cli_records_keep_pre_banner_removal_ids")


def test_codex_recommended_plugins_with_ask_keeps_emitted_legacy_slot():
    roll = [
        _line("session_meta", {"id": "recommended-ask", "cwd": "/x"}),
        _line("response_item", {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": (
                                    "<recommended_plugins>\nplugins\n"
                                    "</recommended_plugins>\nFix the real bug"
                                )}]}),
        _line("response_item", {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "done"}]}),
    ]
    recs, meta = codex.rollout_to_claude_records(roll)
    assert meta["title"] == "Fix the real bug", meta
    assert recs[0]["message"]["content"] == "Fix the real bug", recs[0]
    # Unlike AGENTS/environment-led items, 0.27.4 emitted this whole wrapper
    # as legacy index 1. Reuse that UUID; a recovered-user UUID would duplicate
    # the ask on an incremental re-import and shift the assistant off index 2.
    assert [r["uuid"] for r in recs] == [
        str(uuid.uuid5(uuid.NAMESPACE_URL, "memhub:codex:recommended-ask:1")),
        str(uuid.uuid5(uuid.NAMESPACE_URL, "memhub:codex:recommended-ask:2")),
    ], recs
    print("PASS test_codex_recommended_plugins_with_ask_keeps_emitted_legacy_slot")


def test_codex_usage_without_assistant_gets_stable_record():
    total = {
        "input_tokens": 12, "cached_input_tokens": 8,
        "cache_write_input_tokens": 0, "output_tokens": 2,
        "reasoning_output_tokens": 0, "total_tokens": 14,
    }
    roll = [
        _line("session_meta", {"id": "usage-only", "cwd": "/x"}),
        _line("turn_context", {"model": "gpt-test"}),
        _line("response_item", {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": "go"}]}),
        _line("event_msg", {"type": "token_count", "info": {
            "total_token_usage": total, "last_token_usage": total}}),
    ]
    first, _ = codex.rollout_to_claude_records(roll)
    second, _ = codex.rollout_to_claude_records(roll)
    assert not readers.validate_canonical(first), readers.validate_canonical(first)
    usage_record = first[-1]
    assert usage_record["type"] == "assistant", usage_record
    assert usage_record["message"]["content"] == [{"type": "text", "text": ""}]
    assert usage_record["message"]["model"] == "gpt-test"
    assert usage_record["message"]["usage"] == {
        "input_tokens": 4, "output_tokens": 2,
        "cache_read_input_tokens": 8,
        "cache_creation_input_tokens": 0,
    }, usage_record
    assert usage_record["uuid"] == second[-1]["uuid"]

    # If a later rollout append adds real assistant output, the already-sent
    # usage row remains with the same uuid and the new row does not inherit its
    # counters (which would double-count after incremental import).
    grown = roll + [
        _line("response_item", {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "done"}]})
    ]
    later, _ = codex.rollout_to_claude_records(grown)
    assert later[-2]["uuid"] == usage_record["uuid"]
    assert "usage" not in later[-1]["message"], later[-1]
    print("PASS test_codex_usage_without_assistant_gets_stable_record")


def test_codex_second_usage_without_output_does_not_hit_prior_assistant():
    first = {
        "input_tokens": 10, "cached_input_tokens": 4,
        "cache_write_input_tokens": 0, "output_tokens": 2,
        "reasoning_output_tokens": 0, "total_tokens": 12,
    }
    second = {
        "input_tokens": 18, "cached_input_tokens": 7,
        "cache_write_input_tokens": 0, "output_tokens": 3,
        "reasoning_output_tokens": 0, "total_tokens": 21,
    }
    roll = [
        _line("session_meta", {"id": "hidden-usage", "cwd": "/x"}),
        _line("response_item", {"type": "message", "role": "assistant",
                                "content": [{"type": "output_text", "text": "one"}]}),
        _line("event_msg", {"type": "token_count", "info": {
            "total_token_usage": first, "last_token_usage": first}}),
        # A hidden model request advances usage without an assistant item.
        _line("event_msg", {"type": "token_count", "info": {
            "total_token_usage": second, "last_token_usage": second}}),
    ]
    recs, _ = codex.rollout_to_claude_records(roll)
    assistants = [r for r in recs if r["type"] == "assistant"]
    assert assistants[0]["message"]["usage"] == {
        "input_tokens": 6, "output_tokens": 2,
        "cache_read_input_tokens": 4,
        "cache_creation_input_tokens": 0,
    }, assistants[0]
    assert assistants[1]["message"]["content"] == [{"type": "text", "text": ""}]
    assert assistants[1]["message"]["usage"] == {
        "input_tokens": 5, "output_tokens": 1,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 0,
    }, assistants[1]
    print("PASS test_codex_second_usage_without_output_does_not_hit_prior_assistant")


def test_codex_missing_call_id_orphans_uniquely():
    roll = [
        _line("session_meta", {"id": "s", "cwd": "/x"}),
        _line("response_item", {"type": "function_call", "name": "f",
                                "arguments": "{}"}),                 # no call_id
        _line("response_item", {"type": "function_call_output", "output": "a"}),
        _line("response_item", {"type": "function_call_output", "output": "b"}),
    ]
    recs, _ = codex.rollout_to_claude_records(roll)
    ids = [recs[0]["message"]["content"][0]["id"],
           recs[1]["message"]["content"][0]["tool_use_id"],
           recs[2]["message"]["content"][0]["tool_use_id"]]
    assert all(ids) and len(set(ids)) == 3, ids   # never None, never mispaired
    print("PASS test_codex_missing_call_id_orphans_uniquely")


def test_clean_user_text():
    assert codex.clean_user_text("# AGENTS.md instructions for /x\n...") is None
    assert codex.clean_user_text("<environment_context>\n</environment_context>") is None
    app_context = (
        "<recommended_plugins>\nplugins\n</recommended_plugins>\n"
        "# AGENTS.md instructions\n\n"
        "<INSTRUCTIONS>\nrules\n</INSTRUCTIONS>\n"
        "<environment_context>\nrepo\n</environment_context>"
    )
    assert codex.clean_user_text(app_context) is None
    assert codex.clean_user_text(app_context + "\nFix the real bug") == "Fix the real bug"
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
    assert readers.sniff("/x/.cursor/chats/h/u/store.db") == "cursor"
    assert readers.sniff(
        "/x/.cursor/projects/p/agent-transcripts/u/u.jsonl") == "cursor"
    assert readers.sniff("/anywhere/store.db") == "cursor"
    assert readers.sniff("latest") is None       # ambiguous → caller must pick
    assert readers.sniff("0199-bare-id") is None
    print("PASS test_sniff")


# ---- cursor fixtures: a synthetic store.db in the observed v1 format ----

CURSOR_MESSAGES = [
    {"role": "system", "content": "You are an AI coding assistant..."},
    {"role": "user", "content": [{"type": "text", "text":
        "<timestamp>Tuesday, Aug 18</timestamp>\n<user_query>\nFix the bug\n</user_query>"}],
     "providerOptions": {"cursor": {"requestId": "r1"}}},
    {"role": "assistant", "content": [
        {"type": "reasoning", "text": "", "signature": "OPAQUE"},
        {"type": "text", "text": "On it."},
        {"type": "tool-call", "toolCallId": "call-1\nfc_0", "toolName": "Shell",
         "args": {"command": "ls"}}],
     "usage": {"inputTokens": 50, "outputTokens": 12,
               "cacheReadTokens": 20, "cacheWriteTokens": 3},
     "providerOptions": {"cursor": {"modelName": "cursor-test-model"}}},
    {"role": "tool", "content": [
        {"type": "tool-result", "toolCallId": "call-1\nfc_0", "toolName": "Shell",
         "result": "file.py"}]},
]

CURSOR_TRANSCRIPT = [
    {"role": "user", "message": {"content": [{"type": "text", "text":
        "<timestamp>Sunday, Aug 23, 2026, 11:16 AM (UTC-7)</timestamp>\n"
        "<user_query>\nReply once\n</user_query>"}]}},
    {"role": "assistant", "message": {"content": [
        {"type": "text", "text": "FIRST"}]}},
    {"type": "turn_ended", "status": "success"},
    {"role": "user", "message": {"content": [{"type": "text", "text":
        "<timestamp>Sunday, Aug 23, 2026, 11:18 AM (UTC-7)</timestamp>\n"
        "<user_query>\nEdit it\n</user_query>"}]}},
    {"role": "assistant", "message": {"content": [
        {"type": "text", "text": "Working."},
        {"type": "tool_use", "name": "Write",
         "input": {"path": "/repo/proj/out.txt", "contents": "ok\n"}}]}},
    {"role": "assistant", "message": {"content": [
        {"type": "text", "text": "DONE"}]}},
    {"type": "turn_ended", "status": "success"},
]


def _make_cursor_transcript(base: Path,
                            uuid: str = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee") -> Path:
    path = base / "project" / "agent-transcripts" / uuid / f"{uuid}.jsonl"
    return _write_jsonl(path, CURSOR_TRANSCRIPT)


def _make_cursor_store(base: Path, uuid: str = "11111111-2222-3333-4444-555555555555",
                       cwd: str = "/repo/proj", schema_version: int = 1,
                       node_clocks: bool = True) -> Path:
    """Build a store.db + meta.json mirroring the observed format: JSON leaf
    blobs id'd by sha256, an interior node, and a root whose protobuf field 1
    lists child hashes in order."""
    import hashlib
    import sqlite3 as sq
    d = base / "19ee0000deadbeef" / uuid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps({
        "schemaVersion": schema_version, "cwd": cwd,
        "createdAtMs": 1787116704338, "updatedAtMs": 1787116723901,
        "hasConversation": True}), encoding="utf-8")

    def blob(data: bytes) -> tuple[str, bytes]:
        return hashlib.sha256(data).hexdigest(), data

    leaves = [blob(json.dumps(m).encode()) for m in CURSOR_MESSAGES]

    def _varint(v: int) -> bytes:
        out = b""
        while True:
            b7 = v & 0x7F
            v >>= 7
            out += bytes([b7 | (0x80 if v else 0)])
            if not v:
                return out

    def node(children: list[str], ts_ms: int | None = None,
             extra: bytes = b"") -> tuple[str, bytes]:
        data = b"".join(b"\x0a\x20" + bytes.fromhex(h) for h in children)
        if ts_ms is not None:
            # field 26 varint — the checkpoint wall clock (recognized by
            # ms-epoch RANGE in the reader, not field number)
            data += b"\xd0\x01" + _varint(ts_ms)
        data += extra
        return hashlib.sha256(data).hexdigest(), data

    # interior node (first checkpoint) holds the first two messages; root
    # (second checkpoint, 12s later) adds the rest + trailing metadata —
    # mimics the real store: leaves inherit their nearest ancestor's clock.
    # node_clocks=False builds the same tree with NO readable wall clocks
    # (the field churns with cursor versions, so the range heuristic can
    # find nothing) — the shape that used to trigger the updatedAtMs
    # fallback.
    interior = node([leaves[0][0], leaves[1][0]],
                    ts_ms=1_787_116_704_000 if node_clocks else None)
    root = node([interior[0], leaves[2][0], leaves[3][0]],
                ts_ms=1_787_116_716_000 if node_clocks else None,
                extra=b"\xb2\x01\x03cli")

    con = sq.connect(d / "store.db")
    con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    for h, data in leaves + [interior, root]:
        con.execute("INSERT INTO blobs VALUES (?, ?)", (h, data))
    con.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(
        {"agentId": uuid, "latestRootBlobId": root[0], "name": "New Agent"}),))
    con.commit()
    con.close()
    return d / "store.db"


def test_cursor_clean_user_text():
    assert cursor._clean_user_text(
        "<timestamp>t</timestamp>\n<user_query>\nFix it\n</user_query>") == "Fix it"
    assert cursor._clean_user_text(
        "<user_info>\nOS: mac\n</user_info>\n\nplain ask") == "plain ask"
    assert cursor._clean_user_text("<user_info>\nonly context\n</user_info>") is None
    assert cursor._clean_user_text("just do the thing") == "just do the thing"
    assert cursor._clean_user_text("  ") is None
    print("PASS test_cursor_clean_user_text")


def test_cursor_reader_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        db = _make_cursor_store(Path(td))
        recs, meta = cursor.to_canonical(db)
        assert meta == {"session_id": "11111111-2222-3333-4444-555555555555",
                        "cwd": "/repo/proj", "model": "cursor-test-model",
                        "title": "Fix the bug", "host": "cursor"}, meta
        kinds = []
        for r in recs:
            c = r["message"]["content"]
            kinds.append(f"{r['message']['role']}:" +
                         ("text" if isinstance(c, str) else c[0]["type"]))
        # banner, cleaned ask, text (empty reasoning dropped), tool_use, tool_result
        assert kinds == ["user:text", "user:text", "assistant:text",
                         "assistant:tool_use", "user:tool_result"], kinds
        assert recs[0]["message"]["content"].startswith("[Imported from Cursor"), recs[0]
        assert recs[1]["message"]["content"] == "Fix the bug", recs[1]
        tu = recs[3]["message"]["content"][0]
        assert tu == {"type": "tool_use", "id": "call-1\nfc_0", "name": "Shell",
                      "input": {"command": "ls"}}, tu
        assert recs[3]["message"]["model"] == "cursor-test-model", recs[3]
        assert recs[3]["message"]["usage"] == {
            "input_tokens": 50, "output_tokens": 12,
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 3,
        }, recs[3]
        tr = recs[4]["message"]["content"][0]
        assert tr == {"type": "tool_result", "tool_use_id": "call-1\nfc_0",
                      "content": "file.py"}, tr
        for r in recs:
            assert r.get("cwd") == "/repo/proj", r
            assert r.get("uuid"), r          # server replay-dedup contract
            assert r.get("timestamp"), r     # every fixture node carries a clock
        # A real timeline, not one instant: the banner carries meta.json's
        # createdAtMs, and leaves inherit their checkpoint node's clock —
        # first two messages the first checkpoint, the rest the second.
        stamps = {r["timestamp"] for r in recs}
        assert len(stamps) == 3, stamps
    print("PASS test_cursor_reader_end_to_end")


def test_cursor_store_without_node_clocks_stays_unmeasured():
    """A store whose checkpoint nodes carry no readable wall clock yields
    UNDATED message records — never meta.json's ``updatedAtMs``. That
    fallback dated every otherwise-undated record at flush-adjacent time,
    collapsing a session's event_date onto one instant (the production
    degeneracy this pins against regressing). The banner alone keeps
    ``createdAtMs`` — a real session-start clock the artifact does carry."""
    with tempfile.TemporaryDirectory() as td:
        db = _make_cursor_store(Path(td), node_clocks=False)
        recs, _ = cursor.to_canonical(db)
        assert recs[0]["message"]["content"].startswith("[Imported from Cursor")
        assert recs[0]["timestamp"] == "2026-08-19T05:18:24.338Z", recs[0]
        for r in recs[1:]:
            assert "timestamp" not in r, r
        assert not readers.validate_canonical(recs)
    print("PASS test_cursor_store_without_node_clocks_stays_unmeasured")


def test_cursor_transcript_reader_end_to_end():
    with tempfile.TemporaryDirectory() as td:
        path = _make_cursor_transcript(Path(td))
        recs, meta = cursor.to_canonical(
            path, cwd="/repo/proj", model="cursor-test-hook-model")
        assert meta == {
            "session_id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "cwd": "/repo/proj", "model": "cursor-test-hook-model",
            "title": "Reply once", "host": "cursor",
        }, meta
        kinds = []
        for record in recs:
            content = record["message"]["content"]
            kinds.append(
                f"{record['message']['role']}:" +
                ("text" if isinstance(content, str) else content[0]["type"]))
        assert kinds == [
            "user:text", "user:text", "assistant:text", "user:text",
            "assistant:text", "assistant:tool_use", "assistant:text",
        ], kinds
        assert recs[1]["message"]["content"] == "Reply once"
        assert recs[3]["message"]["content"] == "Edit it"
        tool = recs[5]["message"]["content"][0]
        assert tool == {
            "type": "tool_use", "id": "cursor-call-5", "name": "Write",
            "input": {"path": "/repo/proj/out.txt", "contents": "ok\n"},
        }, tool
        assert recs[6]["message"]["content"][0]["text"] == "DONE"
        assert recs[6]["message"]["model"] == "cursor-test-hook-model"
        assert "usage" not in recs[6]["message"]
        # Real-or-absent timestamps: each user turn keeps its OWN embedded
        # tag (banner inherits the earliest), and the assistant records —
        # which the transcript carries no clock for — stay UNDATED rather
        # than inheriting a carried-forward or file-time stamp (the collapse
        # this reader used to produce; the live flush dates them first-seen).
        assert [record.get("timestamp") for record in recs] == [
            "2026-08-23T18:16:00.000Z",   # banner ← earliest tag
            "2026-08-23T18:16:00.000Z",   # "Reply once" — own tag
            None,                          # assistant: no source clock
            "2026-08-23T18:18:00.000Z",   # "Edit it" — own tag
            None, None, None,              # assistant records of turn 2
        ], [record.get("timestamp") for record in recs]
        again, _ = cursor.to_canonical(
            path, cwd="/repo/proj", model="cursor-test-hook-model")
        assert [record["uuid"] for record in again] == [
            record["uuid"] for record in recs]
        assert not readers.validate_canonical(recs)
    print("PASS test_cursor_transcript_reader_end_to_end")


def test_cursor_transcript_defers_partial_tail_and_rejects_bad_lines():
    with tempfile.TemporaryDirectory() as td:
        path = _make_cursor_transcript(Path(td))
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        recs, _ = cursor.to_canonical(path)
        assert recs[-1]["message"]["content"][0]["text"] == "DONE"
        with path.open("ab") as handle:
            handle.write(b'\n{"role":"assistant","message":')
        recs, _ = cursor.to_canonical(path)
        assert recs[-1]["message"]["content"][0]["text"] == "DONE"
        path.write_bytes(path.read_bytes() + b"\nnot-json\n")
        try:
            cursor.to_canonical(path)
        except ValueError as exc:
            assert "invalid JSON" in str(exc)
        else:
            raise AssertionError("terminated invalid transcript line must fail")
    print("PASS test_cursor_transcript_defers_partial_tail_and_rejects_bad_lines")


def test_cursor_usage_normalization_is_exact():
    assert cursor.normalize_usage({
        "input_tokens": 100, "output_tokens": 7,
        "cache_read_tokens": 40, "cache_write_tokens": 3,
    }) == {
        "input_tokens": 100, "output_tokens": 7,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 3,
    }
    assert cursor.normalize_usage({"inputTokens": 5}) == {
        "input_tokens": 5, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }
    for invalid in ({}, {"input_tokens": -1}, {"output_tokens": True},
                    {"cache_read_tokens": "4"}):
        assert cursor.normalize_usage(invalid) is None, invalid
    print("PASS test_cursor_usage_normalization_is_exact")


def test_cursor_locate_and_list():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db = _make_cursor_store(root / "chats")
        transcript = _make_cursor_transcript(root / "projects")
        old_chats, old_projects = cursor._CHATS, cursor._PROJECTS
        cursor._CHATS = root / "chats"
        cursor._PROJECTS = root / "projects"
        try:
            hit, err = cursor.locate("11111111-2222-3333-4444-555555555555")
            assert hit == db and not err, err
            hit, err = cursor.locate("latest")
            assert hit == transcript
            hit, err = cursor.locate("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
            assert hit == transcript and not err
            hit, err = cursor.locate("nope")
            assert hit is None and "no cursor session" in err
            sessions = cursor.list_sessions()
            assert [s["id"] for s in sessions] == [
                "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                "11111111-2222-3333-4444-555555555555",
            ]
            assert sessions[1]["cwd"] == "/repo/proj"
        finally:
            cursor._CHATS, cursor._PROJECTS = old_chats, old_projects
    print("PASS test_cursor_locate_and_list")


def test_cursor_schema_version_guard():
    with tempfile.TemporaryDirectory() as td:
        db = _make_cursor_store(Path(td), schema_version=2)
        try:
            cursor.to_canonical(db)
        except ValueError as e:
            assert "schemaVersion" in str(e)
            print("PASS test_cursor_schema_version_guard")
            return
        raise AssertionError("schemaVersion 2 must be refused, not misparsed")


def _claude_fixture():
    with tempfile.TemporaryDirectory() as td:
        f = _write_jsonl(Path(td) / "s.jsonl", CLAUDE_SYNTH)
        return claude.to_canonical(f)


def _cursor_fixture():
    with tempfile.TemporaryDirectory() as td:
        return cursor.to_canonical(_make_cursor_store(Path(td)))


CONTRACT_FIXTURES = {
    "claude": _claude_fixture,
    "codex": lambda: codex.rollout_to_claude_records(CODEX_SYNTH),
    "cursor": _cursor_fixture,
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
    # Timestamps: ABSENT is legal (server stores NULL event_date =
    # "unmeasured"); present-but-unparseable is a reader formatting bug.
    undated = [{"type": "user", "uuid": "u1",
                "message": {"role": "user", "content": "hi"}}]
    assert not readers.validate_canonical(undated), readers.validate_canonical(undated)
    garbled = [{"type": "user", "uuid": "u1", "timestamp": "Sunday, Aug 23",
                "message": {"role": "user", "content": "hi"}}]
    assert any("unparseable timestamp" in p
               for p in readers.validate_canonical(garbled))
    print("PASS test_validate_canonical_catches_breakage")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
