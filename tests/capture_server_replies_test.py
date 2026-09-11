#!/usr/bin/env python3
"""Family E: what every import reply does to the Codex and Cursor watermark.

The pure half is pinned elsewhere — ``_verdict`` classifies a reply and
``should_flush`` refuses a dormant session (codex_capture_test,
cursor_capture_test). What was never pinned is the whole transition: a reply
arrives, and the state file and breadcrumb log end up saying WHAT. Those two
files are the only place a user can see a capture failure (``capture_health``
reads neither host's state), so each row here asserts on the real files, not
on a stub of them — they are the observation points the reliability matrix
names (docs/specs/codex-cursor-reliability-matrix.md, family E; row ids match).

Only the network edge is stubbed: the credential, the MCP session, and the
Codex rollout parse. ``cwd`` is None, so no room lookup and no git run.

Run: python3 tests/capture_server_replies_test.py  (stdlib only)
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

# HOME is redirected BEFORE the imports: both flushers fix STATE_DIR — the
# state files and breadcrumb logs this suite reads back — from Path.home() at
# import time. Both spellings: POSIX expanduser reads HOME; Windows reads
# USERPROFILE and never consults HOME.
_TMP_HOME = tempfile.mkdtemp(prefix="capture-server-replies-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
atexit.register(shutil.rmtree, _TMP_HOME, ignore_errors=True)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import codex_flush  # noqa: E402
import cursor_flush  # noqa: E402
import mcp_http  # noqa: E402

assert str(codex_flush.STATE_DIR).startswith(_TMP_HOME), codex_flush.STATE_DIR
assert str(cursor_flush.STATE_DIR).startswith(_TMP_HOME), cursor_flush.STATE_DIR

URL = "https://example.test/mcp"
RECORDS = [{"type": "user", "uuid": "record-1",
            "message": {"role": "user", "content": "hello"}}]
META = {"cwd": None, "title": None}
MILESTONE = {"tool_input": {"command": "git commit -m x"}}


# ── the server edge ──────────────────────────────────────────────────────────

def reply(structured=None, texts=(), is_error=False):
    return types.SimpleNamespace(
        structuredContent=structured, isError=is_error,
        content=[types.SimpleNamespace(text=t) for t in texts])


class Server:
    """A scripted MCP endpoint. Each step is a reply to return, an exception
    to raise, or ``("sleep", seconds)``; every call is recorded."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls: list[dict] = []
        server = self

        class _Session:
            def __init__(self, _url, _bearer, **_kwargs):
                pass

            async def call_tool(self, _name, arguments):
                server.calls.append(arguments)
                step = server.steps.pop(0)
                if isinstance(step, BaseException):
                    raise step
                if isinstance(step, tuple) and step[0] == "sleep":
                    await asyncio.sleep(step[1])
                    return reply()
                return step

        self.session = _Session


@contextlib.contextmanager
def serving(*steps, bearer="token"):
    server = Server(steps)
    saved = (mcp_http.Session, codex_flush.resolve_bearer,
             cursor_flush.resolve_bearer, codex_flush.codex_reader.to_canonical)
    mcp_http.Session = server.session
    codex_flush.resolve_bearer = lambda: (URL, bearer)
    cursor_flush.resolve_bearer = lambda: (URL, bearer)
    codex_flush.codex_reader.to_canonical = lambda _path: (
        [dict(r) for r in RECORDS], dict(META))
    try:
        yield server
    finally:
        (mcp_http.Session, codex_flush.resolve_bearer,
         cursor_flush.resolve_bearer,
         codex_flush.codex_reader.to_canonical) = saved


# ── the two hosts, behind one shape ──────────────────────────────────────────

class _Host:
    module = None
    watermark = ""
    prefix = ""
    stop = ""

    def __init__(self, row: str):
        self.id = f"{row}-{self.prefix}"
        self.conv = f"{self.prefix}-{self.id}"
        self._log_mark = self._log_size()

    # observation points
    def state(self) -> dict:
        return self.module._read_state(self.id)

    def set_state(self, **fields) -> None:
        self.module._save_state(self.id, **fields)

    def _log_size(self) -> int:
        log = self.module.STATE_DIR / "log"
        return log.stat().st_size if log.exists() else 0

    def new_log(self) -> str:
        """Breadcrumb text appended since the last call (or construction)."""
        log = self.module.STATE_DIR / "log"
        data = log.read_bytes() if log.exists() else b""
        text = data[self._log_mark:].decode("utf-8", errors="replace")
        self._log_mark = len(data)
        return text

    # replies
    def ack(self, **extra):
        return reply({"conversation_id": self.conv, "ack_through": "u1", **extra})


class Codex(_Host):
    module = codex_flush
    watermark = "rollout_size"
    prefix = "codex"
    stop = "Stop"

    def __init__(self, row: str):
        super().__init__(row)
        self.size = 0

    def flush(self, event="Stop", payload=None):
        """One hook invocation over new rollout bytes; returns the watermark a
        confirmed import would record."""
        self.size += 100
        codex_flush._flush_locked(event, payload or {}, self.id,
                                  Path("/tmp/rollout.jsonl"), self.size)
        return self.size

    def gate_open(self) -> bool:
        return codex_flush.should_flush("Stop", {}, self.state(), self.size + 100)


class Cursor(_Host):
    module = cursor_flush
    watermark = "transcript_revision"
    prefix = "cursor"
    stop = "stop"

    def __init__(self, row: str):
        super().__init__(row)
        self.rev = 0

    def flush(self):
        """One flush of a new transcript revision; returns the watermark a
        confirmed import would record. (cursor_flush.main owns the gate, the
        timeout and the broad handler — rows that need those say so.)"""
        self.rev += 1
        revision = f"rev-{self.rev}"
        asyncio.run(cursor_flush._flush(
            self.id, Path("/tmp/transcript.jsonl"), set(),
            source_kind="transcript", source_revision=revision,
            records=[dict(r) for r in RECORDS], meta=dict(META)))
        return revision

    def gate_open(self) -> bool:
        return cursor_flush.should_flush(
            "stop", {}, self.state(), set(), time.time(),
            source_kind="transcript", source_revision=f"rev-{self.rev + 1}")


def hosts(row: str):
    return (Codex(row), Cursor(row))


def confirmed(host):
    """A first, confirmed flush: the watermark a later failure must hold."""
    with serving(host.ack()):
        mark = host.flush()
    assert host.state()[host.watermark] == mark, host.state()
    host.new_log()
    return mark


def assert_held_failure(host, mark, reason, log_text):
    st = host.state()
    assert st[host.watermark] == mark, (host.id, st)
    assert st["fail_streak"] == 1, (host.id, st)
    assert st["last_error"] == reason, (host.id, st)
    assert not st.get("unsupported"), (host.id, st)
    log = host.new_log()
    assert log_text in log, (host.id, log_text, log)


# ── E1–E16 ───────────────────────────────────────────────────────────────────

def test_e01_confirmed_ack_advances_the_watermark():
    for host in hosts("e01"):
        with serving(host.ack()) as server:
            mark = host.flush()
        st = host.state()
        assert len(server.calls) == 1
        assert st[host.watermark] == mark, st
        assert st["fail_streak"] == 0 and st["last_error"] is None, st
        assert st["last_ok_at"] > 0 and not st["unsupported"], st
        log = host.new_log()
        assert f"flushed 1 records → {host.conv} (personal)" in log, log
    print("PASS test_e01_confirmed_ack_advances_the_watermark")


def test_e02_null_ack_holds_the_watermark():
    for host in hosts("e02"):
        mark = confirmed(host)
        with serving(reply({"conversation_id": host.conv, "ack_through": None})):
            host.flush()
        assert_held_failure(host, mark, "unconfirmed_import",
                            "import NOT confirmed (ack_through null)")
        if isinstance(host, Cursor):
            # Cursor's backoff between failures is the debounce stamp
            assert host.state()["last_flush_at"] > 0
    print("PASS test_e02_null_ack_holds_the_watermark")


def test_e03_dropped_records_hold_even_beside_an_ack():
    for host in hosts("e03"):
        mark = confirmed(host)
        with serving(host.ack(records_dropped=6)):
            host.flush()
        assert_held_failure(host, mark, "unconfirmed_import",
                            "server dropped 6 record(s)")
    print("PASS test_e03_dropped_records_hold_even_beside_an_ack")


def test_e04_server_error_holds_and_names_its_text():
    detail = "Error executing tool import_conversation: boom"
    for host in hosts("e04"):
        mark = confirmed(host)
        with serving(reply(texts=[detail], is_error=True)):
            host.flush()
        assert_held_failure(host, mark, "unconfirmed_import",
                            f"server rejected the import: [{detail!r}]")
    print("PASS test_e04_server_error_holds_and_names_its_text")


def test_e04b_textless_server_error_logs_an_empty_list():
    # `server rejected the import: []` means the error reply carried NO text.
    # It is the shape a test fixture produces, and the one that filled the
    # real logs before the capture suites were sandboxed (ENG-1034).
    for host in hosts("e04b"):
        mark = confirmed(host)
        with serving(reply(is_error=True)):
            host.flush()
        assert_held_failure(host, mark, "unconfirmed_import",
                            "server rejected the import: []")
    print("PASS test_e04b_textless_server_error_logs_an_empty_list")


def test_e05_unparseable_reply_holds():
    for host in hosts("e05"):
        mark = confirmed(host)
        with serving(reply(texts=["not json"])):
            host.flush()
        assert_held_failure(host, mark, "unconfirmed_import",
                            "import response unrecognized")
    print("PASS test_e05_unparseable_reply_holds")


def test_e06_ack_for_another_conversation_holds():
    for host in hosts("e06"):
        mark = confirmed(host)
        other = reply({"conversation_id": f"{host.prefix}-other",
                       "ack_through": "u1"})
        with serving(other):
            host.flush()
        assert_held_failure(host, mark, "unconfirmed_import",
                            "import response unrecognized")
    print("PASS test_e06_ack_for_another_conversation_holds")


def test_e07_missing_ack_field_goes_dormant_at_once():
    for host in hosts("e07"):
        mark = confirmed(host)
        before = time.time()
        with serving(reply({"conversation_id": host.conv})):
            host.flush()
        st = host.state()
        assert st[host.watermark] == mark, st
        assert st["unsupported"] is True and st["unsupported_at"] >= before, st
        assert st["fail_streak"] == 0, st
        assert "server does not report ack_through" in host.new_log()
        assert not host.gate_open(), st
    print("PASS test_e07_missing_ack_field_goes_dormant_at_once")


def test_e08_rate_limit_counts_toward_dormancy():
    for host in hosts("e08"):
        mark = confirmed(host)
        with serving(mcp_http.McpRateLimited("slow down")):
            host.flush()
        assert_held_failure(host, mark, "rate_limited", "rate limited: slow down")
    print("PASS test_e08_rate_limit_counts_toward_dormancy")


def test_e09_transport_error_counts_toward_dormancy():
    # 401 lands in the same generic bucket as a 5xx: neither flusher tells
    # an auth failure apart (matrix finding FE-3).
    for host in hosts("e09"):
        mark = confirmed(host)
        with serving(mcp_http.McpError("unauthorized", status=401)):
            host.flush()
        assert_held_failure(host, mark, "mcp_error: unauthorized",
                            "import failed: unauthorized")
    print("PASS test_e09_transport_error_counts_toward_dormancy")


def test_e10_missing_credential_is_neutral():
    for host in hosts("e10"):
        mark = confirmed(host)
        host.set_state(fail_streak=3)
        with serving(bearer=None) as server:
            host.flush()
        st = host.state()
        assert server.calls == [], server.calls
        assert st[host.watermark] == mark, st
        assert st["last_error"] == "no_credential", st
        assert st["fail_streak"] == 0, st
        assert "no usable credential" in host.new_log()
    print("PASS test_e10_missing_credential_is_neutral")


def _null(host):
    return reply({"conversation_id": host.conv, "ack_through": None})


def _go_dormant(host):
    for attempt in range(1, codex_flush.MAX_UNCONFIRMED + 1):
        with serving(_null(host)):
            host.flush()
        st = host.state()
        if attempt < codex_flush.MAX_UNCONFIRMED:
            assert st["fail_streak"] == attempt and not st["unsupported"], st
    return host.state()


def test_e11_five_failures_go_dormant():
    assert codex_flush.MAX_UNCONFIRMED == cursor_flush.MAX_UNCONFIRMED == 5
    for host in hosts("e11"):
        mark = confirmed(host)
        st = _go_dormant(host)
        assert st[host.watermark] == mark, st
        assert st["unsupported"] is True and st["fail_streak"] == 0, st
        assert "5 consecutive failed imports (unconfirmed_import)" in host.new_log()
        assert not host.gate_open(), st
    print("PASS test_e11_five_failures_go_dormant")


def test_e12_failed_reprobe_stays_dormant_without_a_fresh_budget():
    for host in hosts("e12"):
        mark = confirmed(host)
        _go_dormant(host)
        host.set_state(unsupported_at=time.time() - codex_flush.DORMANT_RETRY_S - 5)
        assert host.gate_open(), host.state()
        before = time.time()
        with serving(_null(host)) as server:
            host.flush()
        st = host.state()
        assert len(server.calls) == 1
        assert st[host.watermark] == mark, st
        assert st["unsupported"] is True, st
        assert st["unsupported_at"] >= before, st   # the timer restarted
        assert st["fail_streak"] == 0, st
        assert not host.gate_open(), st
    print("PASS test_e12_failed_reprobe_stays_dormant_without_a_fresh_budget")


def test_e13_confirmed_reprobe_rearms_the_session():
    for host in hosts("e13"):
        confirmed(host)
        _go_dormant(host)
        host.set_state(unsupported_at=time.time() - codex_flush.DORMANT_RETRY_S - 5)
        with serving(host.ack()):
            mark = host.flush()
        st = host.state()
        assert st[host.watermark] == mark, st
        assert st["unsupported"] is False and st["unsupported_at"] == 0, st
        assert st["fail_streak"] == 0, st
        assert host.gate_open(), st
    print("PASS test_e13_confirmed_reprobe_rearms_the_session")


def test_e14_codex_cooldown_skips_milestones_but_not_stop():
    host = Codex("e14")
    confirmed(host)
    with serving(_null(host)):
        host.flush()
    host.new_log()
    with serving() as server:        # no steps: any call would fail the pop
        host.flush("PostToolUse", MILESTONE)
    assert server.calls == [], server.calls
    assert "PostToolUse: unconfirmed_import" in host.new_log()
    with serving(host.ack()) as server:
        mark = host.flush("Stop")
    assert len(server.calls) == 1
    assert host.state()["rollout_size"] == mark
    print("PASS test_e14_codex_cooldown_skips_milestones_but_not_stop")


def test_e14b_cooldown_is_logged_with_its_window():
    host = Codex("e14b")
    confirmed(host)
    with serving(_null(host)):
        host.flush()
    host.new_log()
    with serving():
        host.flush("PostToolUse", MILESTONE)
    log = host.new_log()
    assert f"cooling down ({codex_flush.ERROR_COOLDOWN_S:.0f}s)" in log, log
    print("PASS test_e14b_cooldown_is_logged_with_its_window")


def test_e15_codex_timeout_counts_toward_dormancy():
    host = Codex("e15")
    mark = confirmed(host)
    saved = codex_flush.FLUSH_TIMEOUT_S
    codex_flush.FLUSH_TIMEOUT_S = 0.2
    try:
        with serving(("sleep", 5)):
            host.flush()
    finally:
        codex_flush.FLUSH_TIMEOUT_S = saved
    assert_held_failure(host, mark, "flush_error: TimeoutError",
                        "Stop: flush error")
    print("PASS test_e15_codex_timeout_counts_toward_dormancy")


def test_e16_connection_drop_mid_call():
    host = Codex("e16")
    mark = confirmed(host)
    with serving(ConnectionResetError("peer reset")):
        host.flush()
    assert_held_failure(host, mark, "flush_error: ConnectionResetError",
                        "Stop: flush error: peer reset")

    # Cursor's _flush lets a non-MCP transport error out: main() owns the
    # broad handler that turns it into flush_error (not automated here —
    # matrix finding FE-5). Pin the half this suite can reach: it escapes, and
    # nothing claims the content shipped.
    host = Cursor("e16")
    mark = confirmed(host)
    with serving(ConnectionResetError("peer reset")):
        try:
            host.flush()
        except ConnectionResetError:
            pass
        else:
            raise AssertionError("cursor _flush swallowed a transport error")
    assert host.state()["transcript_revision"] == mark, host.state()
    print("PASS test_e16_connection_drop_mid_call")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
