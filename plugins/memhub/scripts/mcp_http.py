#!/usr/bin/env python3
"""Minimal MCP client over streamable HTTP — stdlib only.

**Why this exists.** The hooks loaded the MCP Python SDK, and paid for it on
every invocation: measured 1.09s warm for `uv run --with 'mcp<2' python -c
"import mcp"` against 0.07s for a bare python3. Three of the hooks that paid it
are SYNCHRONOUS — the PreToolUse directive check has no prefilter, so every
single file edit waited on interpreter start and dependency resolution before
the hook had made a single network call.

The SDK was there almost entirely for OAuth: `OAuthClientProvider` does PKCE,
token storage and refresh, which is the genuinely hard part. Once the plugin
mints a personal access key, authentication is one static header and that whole
reason evaporates. What remains is the transport, and the transport turns out
to be small.

**Verified against the live server, not assumed:**

* no session is negotiated — the server returns no ``Mcp-Session-Id`` and does
  not want one back;
* ``initialize`` is NOT required. A fresh process can call a tool directly and
  get a result, so this does ONE round trip where the SDK does three
  (initialize → notifications/initialized → the call);
* replies come back as SSE frames (``event: message`` / ``data: {json}``) even
  for a plain request/response call, so both framings are handled.

**Static bearer only, by design.** Anything needing an interactive browser flow
still belongs to the SDK, in ``login.py``. This is the path a background hook
takes, and a background hook can only ever consume a credential someone else
provisioned.

Results mimic the SDK's shape — ``.content[].text``, ``.structuredContent``,
``.isError`` — so call sites keep their existing response handling and this
change stays a transport swap rather than a rewrite of every caller.

Run the self-test:  python3 tests/mcp_http_test.py
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

# The version this client was written and verified against. Sent so the server
# can negotiate; it echoed the same back.
PROTOCOL_VERSION = "2025-06-18"

_DEFAULT_TIMEOUT_S = 60.0


class McpError(RuntimeError):
    """A call failed at the transport or protocol level.

    ``status`` is the HTTP status when there was one, so callers can tell a
    credential problem (401/403) from a server fault from a local failure.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class McpNoResponse(McpError):
    """The stream carried progress frames but no result and no error.

    Its own type because the callers' vocabulary distinguishes "the reply made
    no sense" from "something unexpected happened", and this is the former: we
    reached the server, it streamed, and no answer arrived. Folding it into a
    generic transport error would describe the wrong thing to whoever reads the
    breadcrumb afterwards.
    """


class McpRateLimited(McpError):
    """429. A key runs at one seat's throughput, and a fleet of parallel
    sessions flushing every turn can genuinely reach it.

    Broken out because it is TRANSIENT and expected under load: callers should
    retry rather than report it as a failure, and the health check must not
    describe it as "the server rejected the upload" — that reads as a fault in
    the session's content when it means "too fast, come back".
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message, status=429)
        self.retry_after = retry_after


class _Block:
    """One content block. Only ``.text`` is consumed by this codebase."""

    def __init__(self, text: str | None):
        self.text = text


class ToolResult:
    """Deliberately shaped like the SDK's result object.

    Callers read ``.content[].text``, ``.structuredContent`` and ``.isError``;
    matching those names keeps this a transport swap instead of a rewrite of
    every response handler, which is the difference between a reviewable diff
    and a risky one.
    """

    def __init__(self, content, structured, is_error: bool):
        self.content = content
        self.structuredContent = structured  # noqa: N815 — mirrors the SDK
        self.isError = is_error  # noqa: N815 — mirrors the SDK


def _parse_sse(body: str) -> list[dict]:
    """JSON payloads out of an SSE stream.

    A ``data:`` field may be split across consecutive lines, which the spec says
    to join with newlines — so lines are accumulated per event and only decoded
    at the blank line that terminates it. Anything undecodable is skipped rather
    than raised: one malformed frame should not lose a well-formed one.
    """
    messages: list[dict] = []
    pending: list[str] = []

    def _flush() -> None:
        if not pending:
            return
        try:
            messages.append(json.loads("\n".join(pending)))
        except ValueError:
            pass
        pending.clear()

    for line in body.splitlines():
        if line.startswith("data:"):
            pending.append(line[5:].lstrip())
        elif not line.strip():
            _flush()
    _flush()
    return messages


def _decode(body: str, content_type: str) -> dict:
    """The JSON-RPC envelope from a response body, whichever framing arrived."""
    if "text/event-stream" in (content_type or ""):
        messages = _parse_sse(body)
        if not messages:
            raise McpError(f"no JSON-RPC message in SSE reply: {body[:200]!r}")
        # The response to our request is the last frame carrying a result or an
        # error; servers may interleave notifications ahead of it.
        for message in reversed(messages):
            if "result" in message or "error" in message:
                return message
        # Progress frames but no answer. Returning the last notification instead
        # would yield an empty `result`, which the capture hooks read as an
        # "unrecognized response" — a diagnosis that blames the server's reply
        # shape when the truth is that no reply arrived. Both paths leave the
        # cursor unmoved, so the difference is entirely in what the breadcrumb
        # tells a human afterwards.
        raise McpNoResponse(
            f"SSE stream carried no result or error frame "
            f"({len(messages)} notification-only frame(s))")
    try:
        return json.loads(body or "{}")
    except ValueError as exc:
        raise McpError(f"reply was not JSON: {body[:200]!r}") from exc


def require_secure(url: str) -> None:
    """Refuse to put a credential on a cleartext connection.

    The endpoint ultimately comes from ``$MEMHUB_MCP_BASE_URL`` or the plugin's
    ``.mcp.json``, so an ``http://`` value — misconfigured or planted — would
    send the bearer in the clear while everything still appeared to work.

    Loopback is exempt: it never leaves the machine, and refusing it would make
    a local backend impossible to develop against.

    Lives here rather than beside its first caller because there are now two
    credential-carrying paths — the MCP endpoint and the access-key REST API —
    and two copies of a security check is one copy too many.
    """
    parts = urllib.parse.urlparse(url)
    if parts.scheme == "https":
        return
    if (parts.hostname or "").lower() in ("localhost", "127.0.0.1", "::1"):
        return
    raise McpError(
        f"refusing to send credentials to {parts.scheme}://{parts.netloc} in "
        "cleartext — https is required. Check $MEMHUB_MCP_BASE_URL.")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects instead of following them.

    ``urlopen``'s default opener follows 30x and copies the request headers
    onto the new request — including ``Authorization``. A redirect to another
    host would therefore hand our bearer to that host, silently, while the call
    still appeared to succeed. The SDK's httpx client does not follow redirects
    by default, so following them was a behaviour change smuggled in with the
    transport swap.

    An MCP endpoint has no reason to redirect, so refusing loses nothing and
    turns a credential leak into a visible error.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise McpError(
            f"refusing to follow a {code} redirect to {newurl!r} — that would "
            "resend the credential to another host", code)


_OPENER = None


def _opener():
    """A module-wide opener that never redirects. Built once, lazily."""
    global _OPENER
    if _OPENER is None:
        _OPENER = urllib.request.build_opener(_NoRedirects)
    return _OPENER


def request(url: str, bearer: str, method: str, params: dict | None = None,
            timeout: float = _DEFAULT_TIMEOUT_S) -> dict:
    """One JSON-RPC call. Returns the ``result`` object.

    Raises ``McpError`` (or ``McpRateLimited``) on anything that is not a
    well-formed successful reply.
    """
    require_secure(url)
    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            # BOTH framings must be advertised: the server replies with SSE
            # even for a plain call, and rejects a request that will not take it.
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        })

    try:
        with _opener().open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = (exc.read() or b"").decode("utf-8", errors="replace")[:200]
        if exc.code == 429:
            raw = exc.headers.get("Retry-After")
            try:
                retry_after = float(raw) if raw else None
            except ValueError:
                retry_after = None
            raise McpRateLimited(f"rate limited: {detail}", retry_after) from exc
        raise McpError(f"{method} failed ({exc.code}): {detail}", exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise McpError(f"{method} failed: {exc}") from exc

    envelope = _decode(body, content_type)
    if "error" in envelope:
        error = envelope["error"] or {}
        # The code goes in the MESSAGE, not on an attribute. An earlier revision
        # carried it as `exc.rpc_code` so callers could classify auth failures
        # delivered inside a 200 envelope — but this server does not deliver
        # them that way. Probed: a garbage, empty, or malformed bearer all
        # return HTTP 401 with `{"error": "invalid_token"}`, which the existing
        # status-based classification already handles.
        #
        # So the attribute was API surface nothing could act on — the same dead
        # design as an unread `retry_after`. The code still reaches the log and
        # the breadcrumb, where it is actually read, by being in the text.
        code = error.get("code")
        detail = error.get("message") or error
        raise McpError(f"{method}: {detail}"
                       + (f" (rpc code {code})" if code is not None else ""))
    # `envelope.get("result") or {}` silently turned a MISSING result into an
    # empty one — and callers cannot tell those apart. For a tools/call that
    # meant an empty ToolResult with isError=False, i.e. a reply that never
    # arrived being read as a successful empty answer. A reply carrying neither
    # result nor error is a protocol violation and should say so.
    if "result" not in envelope:
        raise McpNoResponse(
            f"{method}: reply carried neither a result nor an error")
    result = envelope["result"]
    # A non-object result would be a protocol violation for the methods used
    # here; every caller reads it with .get(), so refuse rather than hand back
    # something that will fail confusingly one frame later.
    if not isinstance(result, dict):
        raise McpError(f"{method}: result was {type(result).__name__}, "
                       "expected an object")
    return result


def call_tool(url: str, bearer: str, name: str, arguments: dict,
              timeout: float = _DEFAULT_TIMEOUT_S) -> ToolResult:
    """Invoke a tool and return an SDK-shaped result."""
    result = request(url, bearer, "tools/call",
                     {"name": name, "arguments": arguments}, timeout)
    blocks = [_Block(b.get("text") if isinstance(b, dict) else None)
              for b in (result.get("content") or [])]
    return ToolResult(blocks, result.get("structuredContent"),
                      bool(result.get("isError")))


def list_tools(url: str, bearer: str,
               timeout: float = _DEFAULT_TIMEOUT_S) -> list[dict]:
    """Tool descriptors — used to prove a credential actually works."""
    return (request(url, bearer, "tools/list", None, timeout).get("tools")
            or [])


class Session:
    """An SDK-compatible ``call_tool`` over this transport.

    Exists so the callers that take a "session" — most importantly
    ``brain_resolve.resolve_repo_brain`` — keep working untouched. Matching the
    SDK's coroutine signature is what makes this change a transport swap rather
    than a refactor reaching into every consumer.

    There is no connection and nothing to open or close: the server is
    stateless, so a Session is just the endpoint and the credential.

    The call runs in a worker thread, not inline. `urllib` is blocking, and
    `brain_resolve` deliberately lists orgs concurrently under
    ``asyncio.gather`` — running the requests inline would silently serialise
    them and undo that, turning a parallel lookup into an N-round-trip one.
    """

    def __init__(self, url: str, bearer: str,
                 timeout: float = _DEFAULT_TIMEOUT_S):
        self._url = url
        self._bearer = bearer
        self._timeout = timeout

    async def call_tool(self, name: str, arguments: dict | None = None):
        import asyncio  # noqa: PLC0415 — only needed on this path

        return await asyncio.to_thread(
            call_tool, self._url, self._bearer, name, arguments or {},
            self._timeout)
