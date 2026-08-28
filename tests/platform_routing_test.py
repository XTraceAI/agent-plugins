#!/usr/bin/env python3
"""Real host provenance reaches every automatic and manual import path."""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

# import_session imports the SDK at module load. Keep this suite stdlib-only so
# the repo's bare-python test command remains useful outside the release venv.
try:
    import mcp  # noqa: F401
except ModuleNotFoundError:
    mcp = types.ModuleType("mcp")
    mcp.__path__ = []
    client = types.ModuleType("mcp.client")
    client.__path__ = []
    session = types.ModuleType("mcp.client.session")
    stream = types.ModuleType("mcp.client.streamable_http")
    session.ClientSession = object
    stream.streamablehttp_client = object
    sys.modules.update({
        "mcp": mcp,
        "mcp.client": client,
        "mcp.client.session": session,
        "mcp.client.streamable_http": stream,
    })

import capture  # noqa: E402
import import_session  # noqa: E402


def test_import_request_uses_requested_platform() -> None:
    for platform in import_session.SOURCE_PLATFORMS:
        args = import_session.import_call_args([], "conversation", platform)
        assert args["source_platform"] == platform
    try:
        import_session.import_call_args([], "conversation", "other")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown platform was accepted")
    provenance = {
        "github_pr_urls": ["https://github.com/xtraceai/agent-plugins/pull/1"],
    }
    args = import_session.import_call_args(
        [], "conversation", "cursor", provenance)
    assert args["provenance"] == provenance
    print("PASS test_import_request_uses_requested_platform")


def test_capture_passes_reader_host_to_import_session() -> None:
    captured: list[list[str]] = []
    original_resolve = capture._resolve
    original_run = capture.subprocess.run

    class Reader:
        def __init__(self, host: str):
            self.HOST = host

        @staticmethod
        def to_canonical(_path):
            return ([{
                "type": "user",
                "uuid": "record-1",
                "timestamp": "2026-08-20T00:00:00Z",
                "message": {"role": "user", "content": "hello"},
            }], {"session_id": "session-1", "cwd": None,
                 "model": None, "title": None})

    def fake_run(command):
        captured.append(command)
        return types.SimpleNamespace(returncode=0)

    args = types.SimpleNamespace(
        host=None, session="session-1", title=None, agent_brain_id=None,
        no_room=True, namespace=None, url=None, conversation_id=None,
        dry_run=False)
    try:
        capture.subprocess.run = fake_run
        for host in ("claude", "codex", "cursor"):
            reader = Reader(host)
            capture._resolve = lambda _args, r=reader: (
                r, Path("/tmp/session.jsonl"), "")
            assert capture.cmd_import(args) == 0
    finally:
        capture._resolve = original_resolve
        capture.subprocess.run = original_run

    assert len(captured) == 3
    for host, command in zip(("claude", "codex", "cursor"), captured):
        index = command.index("--source-platform")
        assert command[index + 1] == host, command
        assert command.count("--no-room") == 1, command
    print("PASS test_capture_passes_reader_host_to_import_session")


def test_capture_claude_dry_run_never_imports() -> None:
    original_resolve = capture._resolve
    original_run = capture.subprocess.run

    class Reader:
        HOST = "claude"

        @staticmethod
        def to_canonical(_path):
            return ([{
                "type": "user",
                "uuid": "record-1",
                "timestamp": "2026-08-23T00:00:00Z",
                "cwd": "/repo",
                "message": {"role": "user", "content": "hello"},
            }], {"session_id": "session-1", "cwd": "/repo"})

    def fail_run(_command):
        raise AssertionError("Claude dry-run launched the importer")

    args = types.SimpleNamespace(
        host="auto", session="session-1", title=None, agent_brain_id=None,
        no_room=False, namespace=None, url=None, conversation_id=None,
        dry_run=True)
    try:
        capture._resolve = lambda _args: (
            Reader(), Path("/sessions/session-1.jsonl"), "")
        capture.subprocess.run = fail_run
        assert capture.cmd_import(args) == 0
    finally:
        capture._resolve = original_resolve
        capture.subprocess.run = original_run
    print("PASS test_capture_claude_dry_run_never_imports")


def test_capture_auto_resolves_one_host_for_bare_id() -> None:
    original_readers = capture.readers.READERS

    class Reader:
        def __init__(self, host: str, path: Path | None):
            self.HOST = host
            self.path = path

        def locate(self, _ref):
            return ((self.path, "") if self.path else
                    (None, f"no {self.HOST} match"))

    expected = Path("/sessions/codex.jsonl")
    try:
        capture.readers.READERS = {
            "claude": Reader("claude", None),
            "codex": Reader("codex", expected),
            "cursor": Reader("cursor", None),
        }
        args = types.SimpleNamespace(host="auto", session="session-1")
        reader, path, err = capture._resolve(args)
        assert reader is capture.readers.READERS["codex"]
        assert path == expected
        assert err == ""
    finally:
        capture.readers.READERS = original_readers
    print("PASS test_capture_auto_resolves_one_host_for_bare_id")


def test_capture_auto_refuses_cross_host_id_and_latest() -> None:
    original_readers = capture.readers.READERS

    class Reader:
        def __init__(self, host: str):
            self.HOST = host

        def locate(self, _ref):
            return Path(f"/sessions/{self.HOST}.jsonl"), ""

    try:
        capture.readers.READERS = {
            "claude": Reader("claude"),
            "codex": Reader("codex"),
            "cursor": Reader("cursor"),
        }
        args = types.SimpleNamespace(host="auto", session="session-1")
        reader, path, err = capture._resolve(args)
        assert reader is None and path is None
        assert "multiple hosts (claude, codex, cursor)" in err

        args.session = "latest"
        reader, path, err = capture._resolve(args)
        assert reader is None and path is None
        assert "'latest' is ambiguous across hosts" in err
    finally:
        capture.readers.READERS = original_readers
    print("PASS test_capture_auto_refuses_cross_host_id_and_latest")


def test_capture_auto_reports_missing_bare_id() -> None:
    original_readers = capture.readers.READERS

    class Reader:
        def __init__(self, host: str):
            self.HOST = host

        def locate(self, _ref):
            return None, f"no {self.HOST} match"

    try:
        capture.readers.READERS = {
            host: Reader(host) for host in ("claude", "codex", "cursor")
        }
        capture.readers.READERS["cursor"].locate = lambda _ref: (None, None)
        args = types.SimpleNamespace(host="auto", session="missing-session")
        reader, path, err = capture._resolve(args)
        assert reader is None and path is None
        assert "cannot find session 'missing-session'" in err

        args.host = "cursor"
        reader, path, err = capture._resolve(args)
        assert reader is None and path is None
        assert err == "cannot find session 'missing-session' for host 'cursor'"
    finally:
        capture.readers.READERS = original_readers
    print("PASS test_capture_auto_reports_missing_bare_id")


def test_capture_auto_preserves_within_host_ambiguity() -> None:
    original_readers = capture.readers.READERS

    class Reader:
        def __init__(self, host: str, result):
            self.HOST = host
            self.result = result

        def locate(self, _ref):
            return self.result

    try:
        capture.readers.READERS = {
            "claude": Reader("claude", (
                None, "ambiguous session id 'shared': 2 matches — pass the path")),
            "codex": Reader("codex", (Path("/sessions/codex.jsonl"), "")),
            "cursor": Reader("cursor", (None, "no cursor match")),
        }
        args = types.SimpleNamespace(host="auto", session="shared")
        reader, path, err = capture._resolve(args)
        assert reader is None and path is None
        assert "claude: ambiguous session id" in err
        assert "pass the session path" in err
    finally:
        capture.readers.READERS = original_readers
    print("PASS test_capture_auto_preserves_within_host_ambiguity")


def test_packaged_import_skill_uses_unified_capture() -> None:
    skill = (ROOT / "plugins" / "memhub" / "skills" / "import-session" /
             "SKILL.md").read_text(encoding="utf-8")
    assert "scripts/capture.py" in skill
    assert "--host auto" in skill
    assert "scripts/import_session.py" not in skill
    assert "Claude Code, Codex, or Cursor" in skill
    print("PASS test_packaged_import_skill_uses_unified_capture")


def test_import_namespace_probe_hardens_transcript_cwd() -> None:
    captured: dict = {}
    original_run = import_session.subprocess.run
    secret_key = "MEMHUB_TEST_BEARER"
    original_secret = os.environ.get(secret_key)

    def fake_run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return types.SimpleNamespace(
            returncode=0,
            stdout="git@github.com:XTraceAI/agent-plugins.git\n",
        )

    try:
        os.environ[secret_key] = "must-not-reach-git"
        import_session.subprocess.run = fake_run
        with tempfile.TemporaryDirectory() as cwd:
            assert import_session._namespace_from_records(
                [{"cwd": cwd}]) == "agent-plugins"
            assert captured["command"] == import_session.git_readonly(cwd) + [
                "remote", "get-url", "origin"]
        assert captured["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert secret_key not in captured["env"]
        captured.clear()
        assert import_session._namespace_from_records(
            [{"cwd": "relative/repo"}]) is None
        assert not captured
    finally:
        import_session.subprocess.run = original_run
        if original_secret is None:
            os.environ.pop(secret_key, None)
        else:
            os.environ[secret_key] = original_secret
    print("PASS test_import_namespace_probe_hardens_transcript_cwd")


if __name__ == "__main__":
    test_import_request_uses_requested_platform()
    test_capture_passes_reader_host_to_import_session()
    test_capture_claude_dry_run_never_imports()
    test_capture_auto_resolves_one_host_for_bare_id()
    test_capture_auto_refuses_cross_host_id_and_latest()
    test_capture_auto_reports_missing_bare_id()
    test_capture_auto_preserves_within_host_ambiguity()
    test_packaged_import_skill_uses_unified_capture()
    test_import_namespace_probe_hardens_transcript_cwd()
    print("ALL PASS")
