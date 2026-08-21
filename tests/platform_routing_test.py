#!/usr/bin/env python3
"""Real host provenance reaches every automatic and manual import path."""
from __future__ import annotations

import sys
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
        namespace=None, url=None, conversation_id=None, dry_run=False)
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
    print("PASS test_capture_passes_reader_host_to_import_session")


if __name__ == "__main__":
    test_import_request_uses_requested_platform()
    test_capture_passes_reader_host_to_import_session()
    print("ALL PASS")
