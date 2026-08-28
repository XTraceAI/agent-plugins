#!/usr/bin/env python3
"""The plugin's MCP server entry: a stdio proxy to the MemHub HTTP endpoint.

**Why the model's MCP tools go through here instead of straight to the
server.** With ``.mcp.json`` pointing at the remote URL, Claude Code ran its
own OAuth flow and kept the token in its own credential store. The hooks
cannot read that store, so they authenticate separately, through the personal
access key ``/memhub:login`` mints. Two logins for one backend, and the failure
that produced: ``/mcp`` shows connected while capture is silently
unauthenticated.

This proxy is the tool side of that pair moved onto the hook side's
credential. Claude Code starts it as a local stdio server; it forwards every
JSON-RPC message to the HTTP endpoint with the same bearer the hooks use
(``_memhub_auth.resolve_bearer``) and relays the reply. One credential,
provisioned once by ``/memhub:login``, serves the tools and the hooks alike.

Not logged in is reported, not hidden: every request gets a JSON-RPC error
naming ``/memhub:login``, so the reason shows up in ``/mcp`` instead of a
bare connection failure. The bearer is resolved per request, so a login that
happens while the session is open takes effect without a restart.

Stdlib only — this process starts with every session.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_http  # noqa: E402
from _memhub_auth import resolve_bearer  # noqa: E402

NOT_LOGGED_IN = ("MemHub: not logged in — run /memhub:login. The plugin's "
                 "tools and hooks share that one credential.")

# JSON-RPC reserved range is -32768..-32000; these sit just outside it.
ERR_NO_CREDENTIAL = -32001
ERR_TRANSPORT = -32002
ERR_RATE_LIMITED = -32003
ERR_INVALID = -32600
ERR_PARSE = -32700

_stdout_lock = threading.Lock()
# Credential resolution is serialized: on an install still on the OAuth cache
# (no access key yet), resolving refreshes a stale token, and two threads
# refreshing at once with the same refresh token would have Auth0 revoke the
# whole token family under rotation. The second caller waits and finds the
# refreshed cache instead.
_auth_lock = threading.Lock()


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def handle(envelope: dict, resolve=resolve_bearer,
           forward=mcp_http.forward) -> dict | None:
    """The reply to one client envelope, or None when none is owed.

    ``resolve`` and ``forward`` are parameters so the self-test can exercise
    the classification without a credential file or a network.
    """
    msg_id = envelope.get("id")
    is_notification = "id" not in envelope
    # Fast path first: a stored access key or $MEMHUB_TOKEN is a file read and
    # needs no lock. Only an install still on the OAuth cache reaches the
    # refresh, and only that is serialized.
    url, bearer = resolve(refresh=False)
    if not bearer:
        with _auth_lock:
            # Re-check under the lock: a thread that waited here finds the
            # token the previous holder just refreshed and must not refresh
            # again with a refresh token that has already been rotated.
            url, bearer = resolve(refresh=False)
            if not bearer:
                url, bearer = resolve()
    if not bearer:
        # A notification carries no id, so there is nothing to answer it with;
        # the next request will say why.
        return None if is_notification else _error(
            msg_id, ERR_NO_CREDENTIAL, NOT_LOGGED_IN)
    try:
        reply = forward(url, bearer, envelope)
    except mcp_http.McpRateLimited as exc:
        return None if is_notification else _error(
            msg_id, ERR_RATE_LIMITED, str(exc))
    except mcp_http.McpError as exc:
        if is_notification:
            return None
        if exc.status in (401, 403):
            return _error(msg_id, ERR_NO_CREDENTIAL,
                          f"MemHub rejected the credential ({exc.status}) — "
                          "run /memhub:login to mint a fresh one.")
        return _error(msg_id, ERR_TRANSPORT, str(exc))
    if is_notification:
        return None
    if reply is None:
        # An empty body is the ack a notification gets; to a REQUEST it is a
        # reply that never came, and saying nothing would leave the client
        # waiting on this id forever.
        return _error(msg_id, ERR_TRANSPORT,
                      f"{envelope.get('method')}: server returned no reply")
    if not isinstance(reply, dict):
        return _error(msg_id, ERR_TRANSPORT,
                      f"{envelope.get('method')}: reply was "
                      f"{type(reply).__name__}, expected a JSON-RPC object")
    # The server answers with our id, but pin it anyway: a mismatched id would
    # be a reply the client can never correlate.
    reply["id"] = msg_id
    return reply


def _emit(reply: dict) -> None:
    line = json.dumps(reply, ensure_ascii=False)
    with _stdout_lock:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _serve_line(line: str) -> None:
    try:
        envelope = json.loads(line)
    except ValueError:
        _emit(_error(None, ERR_PARSE, "request was not JSON"))
        return
    if not isinstance(envelope, dict):
        # Batches are legal JSON-RPC but no MCP client here sends them.
        _emit(_error(None, ERR_INVALID, "expected a single JSON-RPC object"))
        return
    try:
        reply = handle(envelope)
    except Exception as exc:  # noqa: BLE001 — one bad request must not kill the server
        reply = None if "id" not in envelope else _error(
            envelope.get("id"), ERR_TRANSPORT, f"proxy error: {exc}")
    if reply is not None:
        _emit(reply)


def main() -> int:
    # Requests are served on threads because a tools/call can take the full
    # transport timeout, and the client keeps pinging meanwhile. They are
    # joined at EOF: a reply still in flight when the client closes stdin
    # would otherwise die with the process, unanswered.
    workers: list[threading.Thread] = []
    for line in sys.stdin:
        if not line.strip():
            continue
        worker = threading.Thread(target=_serve_line, args=(line,), daemon=True)
        worker.start()
        workers.append(worker)
        workers = [w for w in workers if w.is_alive()]
    for worker in workers:
        worker.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
