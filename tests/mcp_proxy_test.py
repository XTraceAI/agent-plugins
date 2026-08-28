"""Self-test for the stdio MCP proxy.

Covers what the proxy decides on its own — the part a live run against the
server does not exercise: how a missing or rejected credential, a rate limit
and a notification are answered, and that a reply keeps the client's id. The
wire path itself is `mcp_http.forward`, tested live (initialize, tools/list,
tools/call) before this landed.

Run: python3 tests/mcp_proxy_test.py  (stdlib only, no network).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import mcp_http  # noqa: E402
import mcp_proxy as proxy  # noqa: E402

URL = "https://example.invalid/mcp-server/mcp"
failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {'ok ' if got == want else 'FAIL'} {label}")


def _forward_ok(url, bearer, envelope):
    return {"jsonrpc": "2.0", "id": 999, "result": {"echo": envelope["method"]}}


def _forward_raise(exc):
    def _f(url, bearer, envelope):
        raise exc
    return _f


def test_no_credential():
    print("\nno credential")
    reply = proxy.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                         resolve=lambda **kw: (URL, None), forward=_forward_ok)
    check("request gets an error", reply["error"]["code"], proxy.ERR_NO_CREDENTIAL)
    check("error names the fix", "/memhub:login" in reply["error"]["message"], True)
    check("keeps the client's id", reply["id"], 1)
    reply = proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"},
                         resolve=lambda **kw: (URL, None), forward=_forward_ok)
    check("notification stays silent", reply, None)


def test_forwarded_reply_keeps_id():
    print("\nforwarded reply")
    reply = proxy.handle({"jsonrpc": "2.0", "id": "abc", "method": "tools/list"},
                         resolve=lambda **kw: (URL, "mhk_x"), forward=_forward_ok)
    check("result relayed", reply["result"], {"echo": "tools/list"})
    check("id is the client's, not the server's", reply["id"], "abc")
    reply = proxy.handle({"jsonrpc": "2.0", "method": "notifications/initialized"},
                         resolve=lambda **kw: (URL, "mhk_x"),
                         forward=lambda *a: None)
    check("acknowledged notification stays silent", reply, None)
    reply = proxy.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                         resolve=lambda **kw: (URL, "mhk_x"),
                         forward=lambda *a: None)
    check("empty body to a request is an error, not silence",
          reply["error"]["code"], proxy.ERR_TRANSPORT)
    reply = proxy.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                         resolve=lambda **kw: (URL, "mhk_x"),
                         forward=lambda *a: [{"jsonrpc": "2.0", "id": 4}])
    check("non-object reply is an error, not a crash",
          reply["error"]["code"], proxy.ERR_TRANSPORT)


def test_lock_only_on_refresh():
    print("\nlock scope")
    calls = []
    def resolve(refresh=True):
        calls.append(refresh)
        return (URL, "mhk_x") if not refresh else (URL, "oauth")
    proxy.handle({"jsonrpc": "2.0", "id": 5, "method": "ping"},
                 resolve=resolve, forward=_forward_ok)
    check("key path never asks for a refresh", calls, [False])
    calls.clear()
    def resolve_oauth_only(refresh=True):
        calls.append(refresh)
        return (URL, "oauth" if refresh else None)
    reply = proxy.handle({"jsonrpc": "2.0", "id": 6, "method": "ping"},
                         resolve=resolve_oauth_only, forward=_forward_ok)
    check("OAuth-cache install re-checks under the lock, then refreshes",
          calls, [False, False, True])
    check("and is served", "result" in reply, True)


def test_transport_errors():
    print("\ntransport errors")
    req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    reply = proxy.handle(req, resolve=lambda **kw: (URL, "stale"),
                         forward=_forward_raise(mcp_http.McpError("nope", 401)))
    check("401 → credential error", reply["error"]["code"], proxy.ERR_NO_CREDENTIAL)
    check("401 names the fix", "/memhub:login" in reply["error"]["message"], True)
    reply = proxy.handle(req, resolve=lambda **kw: (URL, "k"),
                         forward=_forward_raise(mcp_http.McpRateLimited("slow", 3.0)))
    check("429 → rate-limit error", reply["error"]["code"], proxy.ERR_RATE_LIMITED)
    reply = proxy.handle(req, resolve=lambda **kw: (URL, "k"),
                         forward=_forward_raise(mcp_http.McpError("boom", 500)))
    check("500 → transport error", reply["error"]["code"], proxy.ERR_TRANSPORT)
    reply = proxy.handle({"jsonrpc": "2.0", "method": "notifications/x"},
                         resolve=lambda **kw: (URL, "k"),
                         forward=_forward_raise(mcp_http.McpError("boom", 500)))
    check("failed notification stays silent", reply, None)


def test_stdio_loop_answers_before_exit():
    """EOF on stdin must not discard replies still being produced: the
    process joins its workers, so every request written gets its line back."""
    print("\nstdio loop")
    lines = "\n".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": "ping"})
                      for i in range(5)) + "\nnot json\n"
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "mcp_proxy.py")], input=lines,
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
             "CLAUDE_PLUGIN_ROOT": str(SCRIPTS.parent)})
    replies = [json.loads(l) for l in result.stdout.splitlines() if l.strip()]
    check("exit clean", result.returncode, 0)
    check("one reply per request plus the parse error", len(replies), 6)
    check("all requests answered", sorted(r["id"] for r in replies if r["id"] is not None),
          [0, 1, 2, 3, 4])
    check("unauthenticated install says so",
          all(r["error"]["code"] == proxy.ERR_NO_CREDENTIAL
              for r in replies if r["id"] is not None), True)
    check("bad JSON gets a parse error",
          [r["error"]["code"] for r in replies if r["id"] is None], [proxy.ERR_PARSE])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print()
    if failures:
        print(f"{len(failures)} FAILED"); [print(" ", f) for f in failures]
        sys.exit(1)
    print("all mcp_proxy checks passed")
