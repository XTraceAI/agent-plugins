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

import asyncio
import io
import json
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import cursor_flush  # noqa: E402
import cursor_capture  # noqa: E402
from cursor_flush import (  # noqa: E402
    DORMANT_RETRY_S, _verdict, session_uuid, should_flush)

NOW = 1_787_000_000.0
FRESH = {"a", "b"}          # two blobs in the store
SHIPPED = {"blob_ids": ["a", "b"], "last_flush_at": NOW - 300}
STALE = {"blob_ids": ["a"], "last_flush_at": NOW - 300}   # "b" is new


def test_cross_platform_launcher_acknowledges_and_detaches():
    seen: list[tuple[bytes, str]] = []
    original_spawn = cursor_capture.spawn_cursor_flush
    original_stdin = sys.stdin
    original_stdout = sys.stdout
    try:
        cursor_capture.spawn_cursor_flush = lambda raw, event: seen.append(
            (raw, event))
        sys.stdin = io.TextIOWrapper(io.BytesIO(b'{"session_id":"s"}'))
        sys.stdout = io.StringIO()
        original_argv = sys.argv
        sys.argv = ["cursor_capture.py", "stop"]
        try:
            assert cursor_capture.main() == 0
        finally:
            sys.argv = original_argv
        output = sys.stdout.getvalue()
    finally:
        cursor_capture.spawn_cursor_flush = original_spawn
        sys.stdin = original_stdin
        sys.stdout = original_stdout

    assert seen == [(b'{"session_id":"s"}', "stop")]
    assert json.loads(output) == {"permission": "allow"}
    print("PASS test_cross_platform_launcher_acknowledges_and_detaches")


def test_detached_temporary_stdin_survives_parent_close():
    """The detached child must retain stdin after the launcher returns.

    A delayed reader makes every parent ``TemporaryFile`` close before the
    child consumes it. Repeating the real spawn path catches Windows handle
    inheritance regressions that a mocked ``Popen`` cannot expose.
    """
    attempts = 24
    original_file = cursor_capture.__file__
    with tempfile.TemporaryDirectory(prefix="memhub-cursor-stdin-") as td:
        root = Path(td)
        results = root / "detached results"
        results.mkdir()
        helper = root / "cursor_flush.py"
        helper.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "import time\n"
            "time.sleep(0.1)\n"
            "Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())\n",
            encoding="utf-8",
        )

        expected: dict[Path, bytes] = {}
        cursor_capture.__file__ = str(root / "cursor_capture.py")
        try:
            for attempt in range(attempts):
                output = results / f"payload-{attempt:02d}.bin"
                payload = (f"attempt-{attempt:02d}\n".encode("ascii")
                           + bytes(range(256)) * 384)
                expected[output] = payload
                cursor_capture.spawn_cursor_flush(payload, str(output))
        finally:
            cursor_capture.__file__ = original_file

        deadline = time.monotonic() + 20
        pending = list(expected)
        while pending and time.monotonic() < deadline:
            pending = [path for path, payload in expected.items()
                       if not path.exists()
                       or path.stat().st_size != len(payload)]
            if pending:
                time.sleep(0.02)

        assert not pending, f"detached children did not finish: {pending}"
        for path, payload in expected.items():
            assert path.read_bytes() == payload, path

    print("PASS test_detached_temporary_stdin_survives_parent_close")


def test_cursor_manifest_uses_one_portable_launcher_per_event():
    document = json.loads(
        (ROOT / "plugins" / "memhub" / "hooks" / "cursor-hooks.json")
        .read_text(encoding="utf-8"))
    assert set(document["hooks"]) == {
        "beforeShellExecution", "afterFileEdit", "stop", "beforeSubmitPrompt"
    }
    for event, handlers in document["hooks"].items():
        assert len(handlers) == 1, event
        command = handlers[0]["command"]
        # Cursor's current plugin-hook contract resolves relative commands
        # from the plugin root. One launcher avoids cross-process double flush.
        assert command == f"./hooks/cursor_capture.cmd {event}"
        assert "tee" not in command
    launcher = (ROOT / "plugins" / "memhub" / "hooks" /
                "cursor_capture.cmd").read_text(encoding="utf-8")
    assert not (ROOT / "plugins" / "memhub" / "hooks" /
                "cursor_capture.sh").exists()
    assert launcher.startswith(":; ")
    assert "cursor-root" not in launcher
    assert "stable_root" not in launcher
    assert "cleanup_stage" not in launcher
    assert ".memhub-cursor-hook-" not in launcher
    assert ":launch\nwhere py" in launcher
    assert launcher.count('"%ROOT%\\scripts\\cursor_capture.py" %*') == 2
    assert launcher.count("if errorlevel 1 goto allow") == 2
    print("PASS test_cursor_manifest_uses_one_portable_launcher_per_event")


def test_direct_payload_normalizes_encodings_and_rejects_invalid():
    expected = b'{"session_id":"s"}'
    assert cursor_capture._normalize_payload(expected) == expected
    assert cursor_capture._normalize_payload(
        b"\xff\xfe" + expected.decode().encode("utf-16le")) == expected
    assert cursor_capture._normalize_payload(
        expected.decode().encode("utf-16le")) == expected
    assert cursor_capture._normalize_payload(
        expected.decode().encode("utf-16be")) == expected
    escaped_nul = b'{"value":"\\u0000"}'
    assert cursor_capture._normalize_payload(escaped_nul) == escaped_nul
    assert cursor_capture._normalize_payload(b"a\x00b\x00") is None
    oversized = b"{" + b" " * cursor_capture._MAX_PAYLOAD_BYTES + b"}"
    assert cursor_capture._normalize_payload(oversized) is None
    print("PASS test_direct_payload_normalizes_encodings_and_rejects_invalid")


def test_real_platform_is_unconditional():
    seen: list[dict] = []
    originals = {
        "to_canonical": cursor_flush.cursor_reader.to_canonical,
        "current_blob_ids": cursor_flush.current_blob_ids,
        "redact_records": cursor_flush.redact_records,
        "resolve_bearer": cursor_flush.resolve_bearer,
        "session": cursor_flush.mcp_http.Session,
        "save_state": cursor_flush._save_state,
        "log": cursor_flush._log,
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
        cursor_flush.cursor_reader.to_canonical = lambda _path: ([{
            "type": "user", "uuid": "record-1",
            "message": {"role": "user", "content": "hello"},
        }], {"cwd": None, "title": None})
        cursor_flush.current_blob_ids = lambda _path: {"blob-1"}
        cursor_flush.redact_records = lambda records: records
        cursor_flush.mcp_http.Session = Session
        cursor_flush._save_state = lambda *_args, **_kwargs: None
        cursor_flush._log = lambda *_args, **_kwargs: None
        for url in ("https://api.memhub.xtrace.ai/mcp-server/mcp",
                    "https://api.staging.memhub.xtrace.ai/mcp-server/mcp"):
            cursor_flush.resolve_bearer = lambda u=url: (u, "token")
            asyncio.run(cursor_flush._flush(
                "session-1", Path("/tmp/store.db"), {"blob-1"}))
    finally:
        cursor_flush.cursor_reader.to_canonical = originals["to_canonical"]
        cursor_flush.current_blob_ids = originals["current_blob_ids"]
        cursor_flush.redact_records = originals["redact_records"]
        cursor_flush.resolve_bearer = originals["resolve_bearer"]
        cursor_flush.mcp_http.Session = originals["session"]
        cursor_flush._save_state = originals["save_state"]
        cursor_flush._log = originals["log"]

    assert [args["source_platform"] for args in seen] == ["cursor", "cursor"]
    print("PASS test_real_platform_is_unconditional")


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
    # A milestone verb behind a long wrapper prefix (a real `bash -lc "cd
    # <deep path> && … && git commit"`) must still fire: the scan bound is far
    # larger than the old 512 bytes, which truncated exactly this shape.
    long_commit = ('bash -lc "cd /' + "very/long/path/" * 60
                   + " && git commit -m done\"")
    assert len(long_commit) > 512
    assert should_flush("beforeShellExecution", {"command": long_commit},
                        STALE, FRESH, NOW), "milestone past old 512 cap"
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
    # a payload id is the identity verbatim (the server namespaces it)
    assert session_uuid({"session_id": "u-1"}) == "u-1"
    assert session_uuid({"conversation_id": "c-2"}) == "c-2"
    # the transcript_path fallback must yield a real session UUID, from the
    # session directory or the file stem...
    u = "019c6e48-b66c-7881-9301-99c87fc66cf6"
    assert session_uuid(
        {"transcript_path": f"/x/agent-transcripts/{u}/{u}.jsonl"}) == u
    assert session_uuid({"transcript_path": f"/x/agent-transcripts/{u}.jsonl"}) == u
    # ...never a layout constant: a host that drops the per-session dir would
    # otherwise mis-key every session onto "agent-transcripts"
    assert session_uuid({"transcript_path": "/x/agent-transcripts/sess.jsonl"}) is None
    assert session_uuid({"session_id": "  "}) is None   # blank → no identity
    assert session_uuid({}) is None
    print("PASS test_session_uuid_sources")


def test_import_verdicts_and_dormancy():
    """A returned call is not a stored call — and an unconfirmable server
    must cost neither the session nor an upload loop.

    The backend has shipped a 200-with-nothing-stored mode (records
    dedup-registered without persisting), which is what hid Cursor sessions
    for months. A server that OMITS ack_through is the harder case: trusting
    it risks losing a session's last flush, distrusting it re-uploads the
    whole transcript on every event forever. Answer (flush_turn's, on the
    Claude path): go dormant — hold the watermark AND stop flushing.
    """
    import json as _json
    import types

    def _res(structured=None, texts=(), is_error=False):
        return types.SimpleNamespace(
            structuredContent=structured, isError=is_error,
            content=[types.SimpleNamespace(text=t) for t in texts])

    ok = [
        _res({"conversation_id": "cursor-x", "ack_through": "u1"}),
        _res({"result": {"conversation_id": "c", "ack_through": "u"}}),
        _res(None, [_json.dumps({"conversation_id": "c", "ack_through": "u"})]),
        # a diagnostic block ahead of the ack must not be mistaken for it
        _res(None, [_json.dumps({"level": "info"}),
                    _json.dumps({"conversation_id": "c", "ack_through": "u"})]),
        # nor may a null-ack wrapper outrank the real ack inside its result
        _res({"conversation_id": "c", "ack_through": None,
              "result": {"conversation_id": "c", "ack_through": "u9"}}),
    ]
    unconfirmed = [
        _res({"conversation_id": "c", "ack_through": None}),
        _res({"conversation_id": "c", "ack_through": None, "records_dropped": 6}),
        _res({"conversation_id": "c", "records_dropped": 3}),
        _res({"conversation_id": "c", "ack_through": "u"}, is_error=True),
        _res(None, ["not json"]),
    ]
    for r in ok:
        assert _verdict(r) == "ok", r
    for r in unconfirmed:
        assert _verdict(r) == "unconfirmed", r
    # the field ABSENT (not null) is the dormancy case
    assert _verdict(_res({"conversation_id": "c"})) == "unsupported"
    # ...but an ack-less WRAPPER must not shadow a null-ack payload beside
    # it: that misread a transient failure as a structural one and went
    # dormant on a live server.
    assert _verdict(_res({"conversation_id": "c",
                          "result": {"conversation_id": "c",
                                     "ack_through": None}})) == "unconfirmed"

    # An ack must confirm OUR conversation, not merely SOME conversation: the
    # server echoes the client-supplied id, so an ack naming a different one
    # (a batched/diagnostic echo) must not advance this session's watermark.
    assert _verdict(_res({"conversation_id": "cursor-mine", "ack_through": "u"}),
                    "cursor-mine") == "ok"
    assert _verdict(_res({"conversation_id": "cursor-other", "ack_through": "u"}),
                    "cursor-mine") == "unconfirmed"

    # A dormant session stops flushing on EVERY event within the window,
    # turn boundaries included: re-probing once per DORMANT_RETRY_S is what
    # keeps a down server from being hammered per-turn. The final tail that
    # falls in a dormant window is caught by the import-session sweep, and a
    # transient blip ships before the streak ever reaches dormancy.
    dormant = {"unsupported": True, "unsupported_at": 1_000.0}
    for event in ("afterFileEdit", "beforeShellExecution",
                  "stop", "beforeSubmitPrompt"):
        assert not should_flush(event, {"command": "git commit -m x"},
                                dormant, {"new-blob"}, 1_000.0), event
    # ...but dormancy is NOT a one-way door: going dormant means never
    # flushing again, so nothing could otherwise observe that the server was
    # upgraded. After the re-probe window one flush is allowed through, and a
    # confirmed import clears the flag.
    assert should_flush("stop", {}, dormant, {"new-blob"},
                        1_000.0 + DORMANT_RETRY_S + 1)
    assert should_flush("stop", {}, {"unsupported": False}, {"new-blob"}, 5_000.0)
    print("PASS test_import_verdicts_and_dormancy")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
