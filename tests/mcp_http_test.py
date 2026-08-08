"""Self-test for the stdlib MCP transport.

The wire format was established by probing the live server, so what is asserted
here is that the parser handles what that probe showed — SSE framing for plain
request/response calls — plus the shapes a server is allowed to send that we
happened not to see. Getting these wrong is not loud: a mis-parsed reply reads
as "unrecognized response", which the capture hooks treat as a failure and
retry forever.

Run: python3 tests/mcp_http_test.py  (stdlib only, no network).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mcp_http as m  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok ' if got == want else 'FAIL'} {label}")


def _sse(*objects) -> str:
    return "".join(f"event: message\ndata: {json.dumps(o)}\n\n" for o in objects)


def test_sse_parsing():
    print("\nSSE parsing")
    body = _sse({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
    check("single frame", m._parse_sse(body), [{"jsonrpc": "2.0", "id": 1,
                                                "result": {"ok": True}}])

    # The spec allows a data field split across lines, joined with newlines.
    split = 'event: message\ndata: {"a":\ndata: 1}\n\n'
    check("multi-line data is joined", m._parse_sse(split), [{"a": 1}])

    # A malformed frame must not lose a well-formed one.
    mixed = 'event: message\ndata: {oops\n\n' + _sse({"b": 2})
    check("bad frame skipped, good one kept", m._parse_sse(mixed), [{"b": 2}])

    check("no frames", m._parse_sse(""), [])


def test_decode_picks_the_response():
    print("\nenvelope selection")
    # Servers may interleave notifications ahead of the actual reply; the frame
    # carrying result/error is the answer, not merely the last one.
    body = _sse({"jsonrpc": "2.0", "method": "notifications/progress"},
                {"jsonrpc": "2.0", "id": 1, "result": {"v": 9}})
    check("skips notifications",
          m._decode(body, "text/event-stream")["result"], {"v": 9})

    # Plain JSON framing must work too — the server uses it for 202 replies and
    # is free to use it for others.
    plain = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"v": 1}})
    check("plain json", m._decode(plain, "application/json")["result"], {"v": 1})

    # Progress frames but no answer must RAISE, not yield a bogus empty result.
    # Returning the notification gave `result: {}`, which the capture hooks read
    # as "unrecognized response" — blaming the server's reply shape when the
    # truth is that no reply arrived.
    progress_only = _sse({"jsonrpc": "2.0", "method": "notifications/progress"},
                         {"jsonrpc": "2.0", "method": "notifications/progress"})
    try:
        m._decode(progress_only, "text/event-stream")
        check("notification-only stream raises", False, True)
    except m.McpError as exc:
        check("notification-only stream raises", True, True)
        check("says what was missing", "no result or error" in str(exc), True)

    for label, body, ctype in [
        ("empty SSE", "", "text/event-stream"),
        ("non-json body", "<html>502</html>", "application/json"),
    ]:
        try:
            m._decode(body, ctype)
            check(f"{label} raises", False, True)
        except m.McpError:
            check(f"{label} raises", True, True)


def test_tool_result_shape():
    print("\nresult shape")
    captured = {}

    def fake_request(url, bearer, method, params=None, timeout=None):
        captured.update(method=method, params=params)
        return {"content": [{"type": "text", "text": "hello"}],
                "structuredContent": {"conversation_id": "c1"},
                "isError": False}

    real, m.request = m.request, fake_request
    try:
        res = m.call_tool("https://x/mcp", "mhk_x", "import_conversation", {"a": 1})
    finally:
        m.request = real

    check("calls tools/call", captured["method"], "tools/call")
    check("passes name and args", captured["params"],
          {"name": "import_conversation", "arguments": {"a": 1}})
    # These attribute names mirror the SDK on purpose — every caller's response
    # handling depends on them, which is what kept this a transport swap.
    check("content[].text", res.content[0].text, "hello")
    check("structuredContent", res.structuredContent, {"conversation_id": "c1"})
    check("isError", res.isError, False)

    def error_request(*a, **k):
        return {"content": [{"type": "text", "text": "nope"}], "isError": True}

    m.request = error_request
    try:
        res = m.call_tool("https://x/mcp", "k", "t", {})
        check("isError surfaces", res.isError, True)
    finally:
        m.request = real


def test_rate_limit_is_its_own_error():
    print("\nrate limiting")
    # 429 is transient and expected under load — a fleet flushing every turn
    # shares one seat's throughput. It must not be reported as a content fault.
    err = m.McpRateLimited("slow down", retry_after=12.0)
    check("is an McpError", isinstance(err, m.McpError), True)
    check("carries status", err.status, 429)
    check("carries retry_after", err.retry_after, 12.0)


def test_jsonrpc_error_raises():
    print("\njson-rpc errors")
    real = m._decode
    m._decode = lambda body, ctype: {"jsonrpc": "2.0", "id": 1,
                                     "error": {"code": -32601,
                                               "message": "no such tool"}}
    try:
        # Stub the OPENER, not urlopen: requests go through a module-wide
        # opener that refuses redirects, so patching urlopen would miss.
        class _Resp:
            headers = {"Content-Type": "application/json"}
            status = 200

            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class _Opener:
            def open(self, req, timeout=None): return _Resp()

        real_opener, m._OPENER = m._OPENER, _Opener()
        try:
            m.request("https://x/mcp", "k", "tools/call", {})
            check("raises on json-rpc error", False, True)
        except m.McpError as exc:
            check("raises on json-rpc error", "no such tool" in str(exc), True)
        finally:
            m._OPENER = real_opener
    finally:
        m._decode = real


def test_redirects_are_refused():
    """A redirect must never carry the bearer to another host.

    urlopen's default opener follows 30x and copies request headers onto the
    new request, Authorization included — so a redirect would hand the
    credential to wherever it pointed, silently, while the call still looked
    successful. The SDK's httpx client does not follow redirects, so following
    them would have been a behaviour change smuggled in with the swap.
    """
    print("\nredirects")
    handler = m._NoRedirects()
    try:
        handler.redirect_request(None, None, 302, "Found", {},
                                 "https://evil.example.com/mcp")
        check("refuses to follow", False, True)
    except m.McpError as exc:
        check("refuses to follow", True, True)
        check("names the target", "evil.example.com" in str(exc), True)
        check("carries the status", exc.status, 302)

    # And the opener is actually wired to it.
    check("opener installs the handler",
          any(isinstance(h, m._NoRedirects) for h in m._opener().handlers), True)


if __name__ == "__main__":
    for test in (test_sse_parsing, test_decode_picks_the_response,
                 test_tool_result_shape, test_rate_limit_is_its_own_error,
                 test_jsonrpc_error_raises, test_redirects_are_refused):
        test()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall mcp_http checks passed")
