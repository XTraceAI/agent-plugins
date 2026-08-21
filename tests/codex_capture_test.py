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

import asyncio
import sys
import tempfile
import types
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
    # Command POSITION, not mention: these name the milestone but don't run it
    for quiet in ("echo 'remember to git commit later'",
                  "grep 'git commit' notes.md",
                  "man git commit",
                  "git log --oneline | grep commit",
                  "echo 'sudo git commit'",
                  "cat prcommit.txt",
                  "git status", "git push", "gh repo view",
                  "git push origin commit-branch", "git branch pr-123",
                  "git checkout commit", "git log --oneline commit",
                  "git checkout -b pr-fix", "git diff --stat commit",
                  "gh pr list", "gh pr view 12", "gh pr checks", "gh pr diff"):
        assert not codex_flush.should_flush(
            "PostToolUse", {"tool_input": {"command": quiet}}, STALE, GROWN), quiet
    # ...while the real shapes still fire, including Codex's shell wrapper and
    # a milestone chained after another command
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
        assert codex_flush.should_flush(
            "PostToolUse", {"tool_input": {"command": loud}}, STALE, GROWN), loud
    # A milestone verb behind a long wrapper prefix must still fire: the scan
    # bound is far larger than the old 512 bytes, which truncated this shape.
    long_commit = ('bash -lc "cd /' + "very/long/path/" * 60
                   + " && git commit -m done\"")
    assert len(long_commit) > 512
    assert codex_flush.should_flush(
        "PostToolUse", {"tool_input": {"command": long_commit}}, STALE, GROWN), \
        "milestone past old 512 cap"
    assert not codex_flush.should_flush("UserPromptSubmit", {}, STALE, GROWN)
    print("PASS test_milestone_gates_posttooluse")


def test_locate_rollout_identity():
    uuid = "019c6e48-b66c-7881-9301-99c87fc66cf6"
    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        saved = codex_flush.codex_reader._SESSIONS
        codex_flush.codex_reader._SESSIONS = root
        try:
            f = root / f"rollout-2026-01-02T03-04-05-{uuid}.jsonl"
            f.write_text('{"type":"session_meta","payload":{"id":"x"}}\n',
                         encoding="utf-8")
            # transcript_path preferred, uuid lifted from the filename
            p, sid = codex_flush.locate_rollout({"transcript_path": str(f)})
            assert p == f and sid == uuid, (p, sid)
            # a payload path OUTSIDE the rollout store is refused, even when
            # it exists — capture must never read (and upload) arbitrary
            # files an attacker names in the payload
            with tempfile.NamedTemporaryFile(suffix=".jsonl") as outside:
                p, sid = codex_flush.locate_rollout(
                    {"transcript_path": outside.name})
                assert p is None and sid is None, (p, sid)
            # SAME session, different payload shapes → SAME identity. A
            # payload can carry transcript_path on one event and only
            # session_id on the next; two identities would mean two state
            # files, a reset watermark and a full re-upload.
            _, sid_via_path = codex_flush.locate_rollout(
                {"transcript_path": str(f)})
            _, sid_via_id = codex_flush.locate_rollout({"session_id": uuid})
            assert sid_via_path == sid_via_id == uuid, (sid_via_path, sid_via_id)
            # session_id reaches the filesystem too: codex_reader.locate
            # accepts a PATH as well as a uuid, so an unconstrained bare-id
            # branch would read and upload any readable file named here
            for hostile in ("/etc/passwd", str(Path(__file__).resolve())):
                p, sid = codex_flush.locate_rollout({"session_id": hostile})
                assert p is None and sid is None, (hostile, p, sid)
            # MISMATCHED pair where session_id resolves to a DIFFERENT real
            # session: refuse. Deferring to session_id would flush that other
            # session; deferring to transcript_path would flush its one — both
            # fold the wrong conversation forward, so pick NEITHER.
            other = "019c7f00-0000-7000-8000-000000000000"
            g = root / f"rollout-2026-01-03T00-00-00-{other}.jsonl"
            g.write_text('{"type":"session_meta","payload":{"id":"y"}}\n',
                         encoding="utf-8")
            p, sid = codex_flush.locate_rollout(
                {"transcript_path": str(g), "session_id": uuid})
            assert p is None and sid is None, (p, sid)
            # A CONFLICT where session_id then fails to resolve must also
            # REFUSE — flushing transcript_path's rollout would upload the
            # wrong session's content under this trigger's room. The sweep
            # catches the real session later.
            p, sid = codex_flush.locate_rollout(
                {"transcript_path": str(g), "session_id": "does-not-exist"})
            assert p is None and sid is None, (p, sid)
            # transcript_path names a CONTAINED but uuid-less rollout (identity
            # unconfirmable) WHILE session_id names a real session: agreement
            # cannot be verified, so refuse rather than fold transcript_path's
            # content forward under a contradicted id. (An unparseable filename
            # must not bypass the disagreement guard.)
            uuidless = root / "rollout-no-uuid.jsonl"
            uuidless.write_text('{"type":"session_meta","payload":{"id":"z"}}\n',
                                encoding="utf-8")
            assert codex_flush.codex_reader.rollout_uuid(uuidless) is None
            p, sid = codex_flush.locate_rollout(
                {"transcript_path": str(uuidless), "session_id": uuid})
            assert p is None and sid is None, (p, sid)
            # ...but the SAME uuid-less transcript_path with NO session_id stays
            # trusted once contained — nothing contradicts it
            p, _s = codex_flush.locate_rollout({"transcript_path": str(uuidless)})
            assert p == uuidless, p
            # agreeing pair still takes the fast path
            p, sid = codex_flush.locate_rollout(
                {"transcript_path": str(f), "session_id": uuid})
            assert p == f and sid == uuid, (p, sid)
            # neither field → refuse (never guess 'latest')
            p, sid = codex_flush.locate_rollout({})
            assert p is None and sid is None
        finally:
            codex_flush.codex_reader._SESSIONS = saved
    print("PASS test_locate_rollout_identity")


def test_import_verdicts_and_dormancy():
    """A returned call is not a stored call — and an unconfirmable server
    must cost neither the session nor an upload loop.

    The uuid-less-records bug returned 200 with records_dropped>0 and
    ack_through null, persisting nothing. A server that OMITS ack_through is
    the harder case: trusting it risks a session's last flush (Codex has no
    SessionEnd hook), distrusting it re-uploads the whole rollout forever.
    Answer, matching flush_turn on the Claude path: dormancy.
    """
    import json as _json
    import types

    def _res(structured=None, texts=(), is_error=False):
        return types.SimpleNamespace(
            structuredContent=structured, isError=is_error,
            content=[types.SimpleNamespace(text=t) for t in texts])

    for ok in (_res({"conversation_id": "codex-x", "ack_through": "u1"}),
               _res({"result": {"conversation_id": "c", "ack_through": "u"}}),
               _res(None, [_json.dumps({"conversation_id": "c",
                                        "ack_through": "u"})]),
               _res(None, [_json.dumps({"level": "info"}),
                           _json.dumps({"conversation_id": "c",
                                        "ack_through": "u"})])):
        assert codex_flush._verdict(ok) == "ok", ok
    for bad in (_res({"conversation_id": "c", "ack_through": None}),
                _res({"conversation_id": "c", "ack_through": None,
                      "records_dropped": 6}),
                _res({"conversation_id": "c", "records_dropped": 3}),
                _res({"conversation_id": "c", "ack_through": "u"},
                     is_error=True),
                _res(None, ["not json"])):
        assert codex_flush._verdict(bad) == "unconfirmed", bad
    assert codex_flush._verdict(_res({"conversation_id": "c"})) == "unsupported"
    # ...but an ack-less WRAPPER must not shadow a null-ack payload beside it
    assert codex_flush._verdict(_res(
        {"conversation_id": "c",
         "result": {"conversation_id": "c", "ack_through": None}})) == "unconfirmed"

    # Dormancy gates EVERY event within the window, INCLUDING Stop: a
    # persistently-down server is re-probed once per DORMANT_RETRY_S, never
    # hammered per-turn. Stop's last-chance property is served by the
    # 60s-cooldown exemption (a transient blip ships before dormancy) and by
    # the import-session sweep — not by exempting Stop from dormancy, which
    # would let an active session hammer a down server on every turn.
    import time as _time
    now = _time.time()
    dormant = {"unsupported": True, "unsupported_at": now, "rollout_size": 0}
    for event in ("Stop", "PostToolUse"):
        assert not codex_flush.should_flush(
            event, {"tool_input": {"command": "git commit -m x"}},
            dormant, GROWN), event
    # after the window, ONE re-probe is allowed through (any event)
    past = {**dormant, "unsupported_at": now - codex_flush.DORMANT_RETRY_S - 1}
    assert codex_flush.should_flush("Stop", {}, past, GROWN)
    # a SHRUNK rollout resets rather than blocking capture forever
    assert codex_flush.should_flush("Stop", {}, {"rollout_size": 9_000}, 100)
    print("PASS test_import_verdicts_and_dormancy")


def test_real_platform_is_unconditional():
    seen: list[dict] = []
    originals = {
        "to_canonical": codex_flush.codex_reader.to_canonical,
        "redact_records": codex_flush.redact_records,
        "resolve_bearer": codex_flush.resolve_bearer,
        "session": codex_flush.mcp_http.Session,
        "save_state": codex_flush._save_state,
        "log": codex_flush._log,
    }

    class Session:
        def __init__(self, _url, _bearer, **_kwargs):
            pass

        async def call_tool(self, _name, arguments):
            seen.append(arguments)
            return types.SimpleNamespace(
                structuredContent={
                    "conversation_id": arguments["conversation_id"],
                    "ack_through": "record-1",
                },
                content=[], isError=False)

    try:
        codex_flush.codex_reader.to_canonical = lambda _path: ([{
            "type": "user", "uuid": "record-1",
            "message": {"role": "user", "content": "hello"},
        }], {"cwd": None, "title": None})
        codex_flush.redact_records = lambda records: records
        codex_flush.mcp_http.Session = Session
        codex_flush._save_state = lambda *_args, **_kwargs: None
        codex_flush._log = lambda *_args, **_kwargs: None
        for url in ("https://api.memhub.xtrace.ai/mcp-server/mcp",
                    "https://api.staging.memhub.xtrace.ai/mcp-server/mcp"):
            codex_flush.resolve_bearer = lambda u=url: (u, "token")
            asyncio.run(codex_flush._flush(
                "session-1", Path("/tmp/rollout.jsonl"), 100))
    finally:
        codex_flush.codex_reader.to_canonical = originals["to_canonical"]
        codex_flush.redact_records = originals["redact_records"]
        codex_flush.resolve_bearer = originals["resolve_bearer"]
        codex_flush.mcp_http.Session = originals["session"]
        codex_flush._save_state = originals["save_state"]
        codex_flush._log = originals["log"]

    assert [args["source_platform"] for args in seen] == ["codex", "codex"]
    print("PASS test_real_platform_is_unconditional")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
