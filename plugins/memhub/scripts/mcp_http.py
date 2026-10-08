#!/usr/bin/env python3
"""Minimal MCP client over streamable HTTP — stdlib only.

**Why this exists.** The hooks loaded the MCP Python SDK, and paid for it on
every invocation: measured 1.09s warm to start an interpreter with the SDK
resolved and imported, against 0.07s for a bare python3. Three of the hooks that paid it
were SYNCHRONOUS — the (since retired) PreToolUse directive check had no
prefilter, so every single file edit waited on interpreter start and dependency
resolution before the hook had made a single network call.

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

**Calls take a static bearer.** A background hook can only ever consume a
credential someone else provisioned, and every call here is one header. The
interactive browser login that MINTS such a credential is at the bottom of this
file (``oauth_authorize``): the SDK's ``OAuthClientProvider`` flow, step for
step, so the plugin needs no SDK and no ``uv`` anywhere — not even to log in.

Results mimic the SDK's shape — ``.content[].text``, ``.structuredContent``,
``.isError`` — so call sites keep their existing response handling and this
change stays a transport swap rather than a rewrite of every caller.

Run the self-test:  python3 tests/mcp_http_test.py
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from plugin_version import request_headers, upgrade_message

# The version this client was written and verified against. Sent so the server
# can negotiate; it echoed the same back.
PROTOCOL_VERSION = "2025-06-18"

_DEFAULT_TIMEOUT_S = 60.0

# What a foreground script ported off the SDK should pass as ``timeout``. The
# SDK's streamable client waited up to 300s for a reply to arrive (its
# ``sse_read_timeout``), and a large ``import_conversation`` or
# ``save_artifact`` can legitimately take longer than the 60s a background hook
# is willing to wait. A port that silently shortened that would turn a slow
# save into a failed one.
SDK_READ_TIMEOUT_S = 300.0


class McpError(RuntimeError):
    """A call failed at the transport or protocol level.

    ``status`` is the HTTP status when there was one, so callers can tell a
    credential problem (401/403) from a server fault from a local failure.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class PluginUpgradeRequired(McpError):
    """Structured rulebook policy rejection; safe fields only, no raw body."""

    def __init__(self, minimum_version: str, scope="plugin_operations"):
        self.minimum_version = minimum_version
        super().__init__(
            upgrade_message(minimum_version, scope=scope), 426)


def _raise_upgrade(raw, url=None, bearer=None):
    """Accept only the bounded, validated policy payload; never API shell text."""
    try:
        if isinstance(raw, str) and raw.startswith("Error executing tool "):
            raw = raw.partition(": ")[2]
        payload = json.loads(raw) if isinstance(raw, (bytes, str)) else raw
        policy = payload.get("data", {})
        minimum = policy.get("minimum_version")
        if (policy.get("error_code") == "PLUGIN_UPGRADE_REQUIRED"
                and isinstance(minimum, str)
                and re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", minimum)):
            if url and bearer and policy.get("scope") == "plugin_operations":
                from plugin_compatibility import record
                record(url, bearer, minimum)
            raise PluginUpgradeRequired(minimum, scope=policy.get("scope", "rulebook_fetch"))
    except (ValueError, AttributeError, TypeError):
        pass


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
    """One content block, shaped like the SDK's ``TextContent``.

    ``.type`` is carried because the SDK's blocks have it and a caller ported
    from the SDK may filter on it: md_capture_flush did
    (``getattr(c, "type", "") == "text"``), and without the attribute every
    block was dropped — each save read as an empty reply and nothing was ever
    captured. Callers should prefer ``texts_of``, which needs neither.
    """

    def __init__(self, text: str | None, type: str | None = None):  # noqa: A002 — mirrors the SDK
        self.text = text
        self.type = type or ("text" if isinstance(text, str) else None)

    def __repr__(self) -> str:
        # The SDK's TextContent repr, so `str(result)` below reads the same.
        return (f"TextContent(type={self.type!r}, text={self.text!r}, "
                "annotations=None, meta=None)")


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

    def __str__(self) -> str:
        # The SDK's pydantic `str()`. Not cosmetic: the artifact and import
        # scripts fall back to `{"_raw": str(result)}` for a reply with no
        # content and print it, and a port must not turn that line into
        # `<mcp_http.ToolResult object at 0x…>`.
        return (f"meta=None content={self.content!r} "
                f"structuredContent={self.structuredContent!r} "
                f"isError={self.isError!r}")


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
    from plugin_compatibility import before_operation
    before_operation(url, bearer)
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
            **request_headers(),
        })

    try:
        with _opener().open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raw_error = exc.read(16384)
        exc.close()
        if exc.code == 426:
            _raise_upgrade(raw_error, url, bearer)
        detail = raw_error.decode("utf-8", errors="replace")[:200]
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
        _raise_upgrade(error, url, bearer)
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


class RestReply:
    """One plain-HTTP reply: ``status`` (200/202/304/...), the ``etag`` header
    if any, and ``data`` — the decoded JSON body with the REST ``{code, msg,
    data}`` envelope unwrapped, or None when there was no body (304)."""

    def __init__(self, status: int, etag: str | None, data):
        self.status = status
        self.etag = etag
        self.data = data


def rest(url: str, bearer: str, method: str = "GET", body: dict | None = None,
         headers: dict | None = None, timeout: float = _DEFAULT_TIMEOUT_S) -> RestReply:
    """One REST call over the SAME transport as the MCP path — same opener
    (never follows a redirect, so the bearer never leaves the host it was
    issued for), same cleartext refusal, same error taxonomy.

    Exists so the rulebook hook, which speaks plain REST (``GET /rules`` with
    ``If-None-Match``, ``POST /fires``), does not grow a second HTTP client
    beside this one. ``304`` and ``202`` are ordinary replies here, not
    errors; ``McpError`` carries the status for everything 4xx/5xx.
    """
    require_secure(url)
    from plugin_compatibility import before_operation
    before_operation(url, bearer)
    hdrs = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    # Callers cannot accidentally report a downloaded manifest as loaded code.
    hdrs = {k: v for k, v in hdrs.items() if k.lower() != "x-memhub-plugin-version"}
    hdrs.update(request_headers())
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=hdrs)
    try:
        with _opener().open(req, timeout=timeout) as resp:
            status = resp.status
            etag = resp.headers.get("ETag")
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return RestReply(304, exc.headers.get("ETag"), None)
        raw_error = exc.read(16384)
        exc.close()
        if exc.code == 426:
            _raise_upgrade(raw_error, url, bearer)
        detail = raw_error.decode("utf-8", errors="replace")[:200]
        if exc.code == 429:
            raw_ra = exc.headers.get("Retry-After")
            try:
                retry_after = float(raw_ra) if raw_ra else None
            except ValueError:
                retry_after = None
            raise McpRateLimited(f"rate limited: {detail}", retry_after) from exc
        raise McpError(f"{method} {url} failed ({exc.code}): {detail}", exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise McpError(f"{method} {url} failed: {exc}") from exc
    if not raw.strip():
        return RestReply(status, etag, None)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise McpError(f"{method} {url}: reply was not JSON: {raw[:200]!r}") from exc
    # The REST API wraps in {"code": 0, "msg": "ok", "data": …}; a non-zero
    # code is a failure the transport reported as 2xx and must not read as one.
    if isinstance(payload, dict) and "code" in payload:
        if payload.get("code") != 0:                 # error envelope (null counts), data or not
            raise McpError(f"{method} {url}: {payload.get('msg') or payload}", status)
        if "data" in payload:
            payload = payload["data"]
    return RestReply(status, etag, payload)


def call_tool(url: str, bearer: str, name: str, arguments: dict,
              timeout: float = _DEFAULT_TIMEOUT_S) -> ToolResult:
    """Invoke a tool and return an SDK-shaped result."""
    result = request(url, bearer, "tools/call",
                     {"name": name, "arguments": arguments}, timeout)
    raise_for_upgrade_result(result, url, bearer)
    blocks = [_Block(b.get("text"), b.get("type")) if isinstance(b, dict) else _Block(None)
              for b in (result.get("content") or [])]
    return ToolResult(blocks, result.get("structuredContent"),
                      bool(result.get("isError")))


def raise_for_upgrade_result(result, url=None, bearer=None):
    """Identical handling for result dictionaries and attribute-style result objects."""
    def field(obj, name, default=None):
        return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)

    if field(result, "isError", False):
        _raise_upgrade({"data": field(result, "structuredContent")}, url, bearer)
        for block in field(result, "content", []) or []:
            text = field(block, "text")
            if isinstance(text, str):
                _raise_upgrade(text[:16384], url, bearer)


def list_tools(url: str, bearer: str,
               timeout: float = _DEFAULT_TIMEOUT_S) -> list[dict]:
    """Tool descriptors — used to prove a credential actually works."""
    return (request(url, bearer, "tools/list", None, timeout).get("tools")
            or [])


def texts_of(res) -> list[str]:
    """The text blocks of a tool result, in order."""
    return [t for t in (getattr(b, "text", None)
                        for b in getattr(res, "content", []) or []) if t]


def ack_of(res, expected_conversation_id: str | None = None) -> dict | None:
    """The import ack carried by a tool result, or None if it carries none.

    A server may answer with structuredContent, a FastMCP ``result`` wrapper,
    one or more JSON text blocks, or a mix — and the ack is not necessarily
    the FIRST parseable one: a diagnostic object ahead of it would otherwise
    be mistaken for the answer and read as "unrecognized", making a healthy
    server look like a failing one and re-uploading the transcript on every
    event. So every candidate is collected and the one that actually looks
    like an ack (carries ``conversation_id``) wins.

    Lives here rather than in each capture script because both flushers need
    exactly this and a bug in it is a bug in both.
    """
    candidates: list[dict] = []
    out = getattr(res, "structuredContent", None)

    def _add(d):
        # A FastMCP `result` wrapper is unwrapped so a nested ack is still
        # considered — but the id is NEVER synthesized onto it. An ack must
        # carry its OWN conversation_id to be matched: proving the ENVELOPE
        # is ours does not prove the ENVELOPE'S nested result is (a batched
        # wrapper could echo our id on the envelope while its result acks a
        # different conversation). So an id-less inner result is excluded by
        # the expected-id filter — the conservative, unconfirmed direction.
        # This backend returns conversation_id and ack_through together in
        # the same object, so a real confirming ack always carries its id;
        # the id-less-inner shape is hypothetical, and its worst case is a
        # harmless re-send (the server dedups).
        candidates.append(d)
        inner = d.get("result")
        if isinstance(inner, dict):
            candidates.append(inner)

    if isinstance(out, dict):
        _add(out)
    for text in texts_of(res):
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            _add(parsed)
    # Among the shapes that look like an ack, prefer one that actually
    # CONFIRMS persistence. Order alone is not enough: a wrapper can carry
    # conversation_id at the top level with a null ack while the real,
    # acknowledged payload sits in its ``result`` — picking by position would
    # then report a healthy import as unconfirmed and re-upload the whole
    # transcript on every later event.
    acks = [c for c in candidates if "conversation_id" in c]
    if expected_conversation_id is not None:
        # An ack only confirms OUR import. The server echoes back the
        # client-supplied conversation_id (MemHub-Backend mcp_server.py: the
        # ack's `conversation_id` is the request's `conv`), so an ack naming
        # a different id — a batched or diagnostic echo for another
        # conversation — must not advance THIS session's watermark. A
        # mismatch drops to no-ack, i.e. "unconfirmed": hold and retry, the
        # safe direction.
        acks = [c for c in acks
                if c.get("conversation_id") == expected_conversation_id]
    for c in acks:
        if c.get("ack_through"):
            return c
    # Nothing confirms. Prefer a candidate that at least CARRIES the
    # ack_through key over one that omits it: "present but null" means a
    # server that knows the field and stored nothing (transient — retry),
    # while "absent" means a server that cannot report at all (structural).
    # Callers act very differently on those, so an ack-less wrapper must not
    # shadow a null-ack payload sitting beside it.
    for c in acks:
        if "ack_through" in c:
            return c
    return acks[0] if acks else None


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

    async def call_tool(self, name: str, arguments: dict | None = None,
                        timeout: float | None = None):
        """``timeout`` overrides the session default for THIS call.

        Needed because a caller working to a wall-clock deadline must bound each
        call by the time it has LEFT, not by a fixed fraction of the budget: a
        slice that starts just under the deadline would otherwise run a whole
        per-call timeout past it.
        """
        import asyncio  # noqa: PLC0415 — only needed on this path

        return await asyncio.to_thread(
            call_tool, self._url, self._bearer, name, arguments or {},
            self._timeout if timeout is None else timeout)


def is_compute_budget_rejection(res) -> bool:
    """Recognize the billing gate, never a hook timeout or a successful reply."""
    if not getattr(res, "isError", False):
        return False
    return any("your organization has used its monthly compute budget" in text.lower()
               for text in texts_of(res))


# ---------------------------------------------------------------------------
# Interactive OAuth — the SDK's ``OAuthClientProvider`` flow, in stdlib.
#
# The last thing the SDK was loaded for. It is ported rather than redesigned:
# the same discovery order, the same scope and ``resource`` choices, the same
# PKCE parameters, the same token request, and the same error names and
# messages — ``login.py`` prints ``type(exc).__name__`` to the user, so
# ``OAuthFlowError: State parameter mismatch: …`` reads exactly as it did.
# Read against mcp 1.30 ``client/auth/oauth2.py`` and ``utils.py``.
#
# Deliberately NOT ported: dynamic client registration and URL-based client
# ids. The plugin's Auth0 client is pre-registered (``.mcp.json`` names it), so
# the SDK never reached either branch. Neither is the SDK's refresh, which could
# not work from a cold process anyway (see ``_memhub_auth``); the stdlib refresh
# shim there already replaced it.
# ---------------------------------------------------------------------------

# Metadata GETs and the token POST ran on the SDK's httpx client, whose timeout
# was 30s; the browser round trip itself is bounded by the callback handler.
_OAUTH_TIMEOUT_S = 30.0


class OAuthFlowError(McpError):
    """The authorization flow could not proceed. Named after the SDK's."""


class OAuthTokenError(McpError):
    """The token endpoint refused the exchange or answered nonsense."""


def www_authenticate_field(header: str | None, name: str) -> str | None:
    """One ``name="value"`` (or unquoted) field of a WWW-Authenticate header."""
    if not header:
        return None
    match = re.search(rf'{name}=(?:"([^"]+)"|([^\s,]+))', header)
    return (match.group(1) or match.group(2)) if match else None


def _http(url: str, method: str = "GET", data: bytes | None = None,
          headers: dict | None = None,
          timeout: float = _OAUTH_TIMEOUT_S) -> tuple[int, dict, bytes]:
    """``(status, headers, body)`` for ANY status — the OAuth steps branch on it.

    Same no-redirect opener as every other call: discovery documents and the
    token endpoint are not followed across a redirect either (the SDK did not
    follow them), so a 30x comes back as its status. A network failure raises.
    """
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    try:
        with _opener().open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read(16384)
        exc.close()
        return exc.code, dict(exc.headers.items()), body
    except McpError as exc:
        if exc.status is not None:      # a refused redirect
            return exc.status, {}, b""
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise McpError(f"{method} {url} failed: {exc}") from exc


def _json_object(body: bytes) -> dict | None:
    try:
        value = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _canonical_url(url: str) -> str:
    """A URL the way the SDK's ``AnyHttpUrl`` renders it — lowercase scheme and
    host, ``/`` for an empty path — so issuers and resources compare as strings."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(
        scheme=parts.scheme.lower(), netloc=parts.netloc.lower(),
        path=parts.path or "/"))


def _resource_url(url: str) -> str:
    """RFC 8707 canonical resource: lowercase scheme/host, no fragment."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parts._replace(
        scheme=parts.scheme.lower(), netloc=parts.netloc.lower(), fragment=""))


def _resource_allowed(requested: str, configured: str) -> bool:
    """Same origin, and ``requested``'s path under ``configured``'s."""
    r, c = urllib.parse.urlparse(requested), urllib.parse.urlparse(configured)
    if (r.scheme.lower(), r.netloc.lower()) != (c.scheme.lower(), c.netloc.lower()):
        return False
    rp = r.path if r.path.endswith("/") else r.path + "/"
    cp = c.path if c.path.endswith("/") else c.path + "/"
    return rp.startswith(cp)


def _issuers_match(a: str, b: str) -> bool:
    """String equality, except a root issuer with and without its ``/``."""
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    parts = urllib.parse.urlparse(shorter)
    return longer == f"{shorter}/" and shorter == f"{parts.scheme}://{parts.netloc}"


def _origin(url: str) -> str:
    parts = urllib.parse.urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def auth_challenge(url: str, timeout: float = _OAUTH_TIMEOUT_S) -> str | None:
    """The WWW-Authenticate header the server answers an UNAUTHENTICATED call with.

    The SDK learned where to authenticate from the 401 on its first request;
    a port that starts the flow without having made one asks the same question
    here. No credential is sent, so nothing needs the https check. None when
    the server did not challenge (the flow then falls back to well-known URLs,
    exactly as the SDK did for a challenge without ``resource_metadata``).
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    status, headers, _ = _http(
        url, "POST", json.dumps(payload).encode("utf-8"),
        {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": PROTOCOL_VERSION, **request_headers()},
        timeout)
    if status not in (401, 403):
        return None
    return next((v for k, v in headers.items()
                 if k.lower() == "www-authenticate"), None)


class OAuthDiscovery:
    """What discovery found: protected-resource metadata (``prm``), the chosen
    authorization server, its metadata, and the ``scope`` / ``resource`` the
    authorization request will carry (either may be None, as in the SDK)."""

    def __init__(self, prm, auth_server_url, metadata, scope, resource):
        self.prm = prm
        self.auth_server_url = auth_server_url
        self.metadata = metadata
        self.scope = scope
        self.resource = resource

    def endpoint(self, server_url: str, name: str) -> str:
        """``authorization_endpoint`` / ``token_endpoint``, else the SDK's
        fallback of ``/authorize`` / ``/token`` on the MCP server's origin."""
        value = (self.metadata or {}).get(f"{name}_endpoint")
        if isinstance(value, str) and value:
            return value
        return urllib.parse.urljoin(_origin(server_url),
                                    "/authorize" if name == "authorization" else "/token")


def discover_oauth(url: str, www_authenticate: str | None = None,
                   timeout: float = _OAUTH_TIMEOUT_S) -> OAuthDiscovery:
    """Steps 1–3 of the SDK flow: protected-resource metadata (RFC 9728, with
    the SEP-985 fallbacks), authorization-server metadata (RFC 8414 / OIDC),
    and the scope selection strategy. Raises ``OAuthFlowError`` where the SDK did.
    """
    parsed = urllib.parse.urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    proto = {"MCP-Protocol-Version": PROTOCOL_VERSION}

    # Step 1 — protected resource metadata.
    prm_urls = []
    challenge_url = www_authenticate_field(www_authenticate, "resource_metadata")
    if challenge_url:
        prm_urls.append(challenge_url)
    if parsed.path and parsed.path != "/":
        prm_urls.append(urllib.parse.urljoin(
            base, f"/.well-known/oauth-protected-resource{parsed.path}"))
    prm_urls.append(urllib.parse.urljoin(base, "/.well-known/oauth-protected-resource"))

    prm = None
    prm_failed = None
    for candidate in prm_urls:
        status, _, body = _http(candidate, headers=proto, timeout=timeout)
        if status >= 500 or status == 429:
            prm_failed = status
        doc = _json_object(body) if status == 200 else None
        servers = (doc or {}).get("authorization_servers")
        if (doc and isinstance(doc.get("resource"), str)
                and isinstance(servers, list) and servers
                and all(isinstance(s, str) for s in servers)):
            prm = doc
            break
    else:
        if prm_failed is not None:
            # A server error says nothing about whether the resource publishes
            # metadata, so it must not send the flow down the legacy path.
            raise OAuthFlowError(
                f"Protected resource metadata request failed: HTTP {prm_failed}")

    resource = _resource_url(url)
    auth_server_url = None
    if prm is not None:
        prm_resource = _canonical_url(prm["resource"])
        if not _resource_allowed(resource, prm_resource):
            raise OAuthFlowError(
                f"Protected resource {prm_resource} does not match expected {resource}")
        resource = prm_resource
        auth_server_url = _canonical_url(prm["authorization_servers"][0])

    # Step 2 — authorization server metadata, path-aware, OAuth then OIDC.
    expected_issuer = auth_server_url or _canonical_url(base)
    if not auth_server_url:
        asm_urls = [f"{base}/.well-known/oauth-authorization-server"]
    else:
        auth = urllib.parse.urlparse(auth_server_url)
        auth_base = f"{auth.scheme}://{auth.netloc}"
        path = auth.path.rstrip("/")
        if auth.path and auth.path != "/":
            asm_urls = [urllib.parse.urljoin(auth_base, f"/.well-known/oauth-authorization-server{path}"),
                        urllib.parse.urljoin(auth_base, f"/.well-known/openid-configuration{path}"),
                        urllib.parse.urljoin(auth_base, f"{path}/.well-known/openid-configuration")]
        else:
            asm_urls = [urllib.parse.urljoin(auth_base, "/.well-known/oauth-authorization-server"),
                        urllib.parse.urljoin(auth_base, "/.well-known/openid-configuration")]

    metadata = None
    for candidate in asm_urls:
        status, _, body = _http(candidate, headers=proto, timeout=timeout)
        if status == 200:
            doc = _json_object(body)
            if doc and all(isinstance(doc.get(k), str) and doc.get(k)
                           for k in ("issuer", "authorization_endpoint", "token_endpoint")):
                # RFC 8414 §3.3: the document must name the issuer it was
                # discovered for, or it is somebody else's.
                if not _issuers_match(_canonical_url(doc["issuer"]), expected_issuer):
                    raise OAuthFlowError(
                        "Authorization server metadata issuer mismatch: "
                        f"{doc['issuer']} != {expected_issuer}")
                metadata = doc
                break
            continue                    # unparseable: try the next candidate
        if 300 <= status < 500:
            continue                    # not served here (redirects not followed)
        break                           # a server error ends discovery

    # Step 3 — scope: the challenge's, else the resource's, else the server's.
    scope = www_authenticate_field(www_authenticate, "scope")
    if scope is None:
        for doc in (prm, metadata):
            supported = (doc or {}).get("scopes_supported")
            if isinstance(supported, list):
                scope = " ".join(str(s) for s in supported)
                break

    return OAuthDiscovery(prm, auth_server_url, metadata, scope,
                          resource if prm is not None else None)


def pkce_pair() -> tuple[str, str]:
    """``(code_verifier, code_challenge)``: 128 unreserved characters, S256."""
    import base64  # noqa: PLC0415 — only the login path needs these
    import hashlib  # noqa: PLC0415
    import secrets  # noqa: PLC0415
    import string  # noqa: PLC0415

    alphabet = string.ascii_letters + string.digits + "-._~"
    verifier = "".join(secrets.choice(alphabet) for _ in range(128))
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode().rstrip("=")


def oauth_token(raw) -> dict:
    """A token response validated the way the SDK's ``OAuthToken`` model did.

    Returns exactly its five fields, in its order, ``token_type`` normalised to
    ``Bearer`` — the shape the token cache has always held, which
    ``_memhub_auth`` and ``login.py`` read back. Raises ``OAuthTokenError``.
    """
    doc = raw if isinstance(raw, dict) else _json_object(raw)
    if doc is None:
        raise OAuthTokenError(f"Invalid token response: {raw[:200]!r}")
    token_type = doc.get("token_type", "Bearer")
    if isinstance(token_type, str):
        token_type = token_type.title()
    expires_in = doc.get("expires_in")
    if isinstance(expires_in, str) and expires_in.strip().isdigit():
        expires_in = int(expires_in)
    elif isinstance(expires_in, float) and expires_in.is_integer():
        expires_in = int(expires_in)
    problems = [name for name, ok in (
        ("access_token", isinstance(doc.get("access_token"), str)),
        ("token_type", token_type == "Bearer"),
        ("expires_in", expires_in is None
         or (isinstance(expires_in, int) and not isinstance(expires_in, bool))),
        ("scope", doc.get("scope") is None or isinstance(doc.get("scope"), str)),
        ("refresh_token", doc.get("refresh_token") is None
         or isinstance(doc.get("refresh_token"), str)),
    ) if not ok]
    if problems:
        raise OAuthTokenError(
            f"Invalid token response: bad {', '.join(problems)}")
    return {"access_token": doc["access_token"], "token_type": "Bearer",
            "expires_in": expires_in, "scope": doc.get("scope"),
            "refresh_token": doc.get("refresh_token")}


def oauth_token_json(token: dict) -> str:
    """The cache file's text, byte-identical to the SDK's ``model_dump_json``."""
    return json.dumps(oauth_token(token), separators=(",", ":"),
                      ensure_ascii=False)


async def _maybe_await(value):
    import inspect  # noqa: PLC0415

    return await value if inspect.isawaitable(value) else value


async def oauth_authorize(url: str, client_id: str, redirect_uri: str,
                          redirect_handler, callback_handler, *,
                          www_authenticate: str | None = None,
                          discovery: OAuthDiscovery | None = None,
                          timeout: float = _OAUTH_TIMEOUT_S) -> dict:
    """The browser login: discovery, PKCE authorization, code exchange.

    ``redirect_handler(auth_url)`` opens the browser (or raises — a background
    caller's ``NonInteractiveAuthRequired`` propagates untouched, no longer
    buried in an anyio ExceptionGroup); ``callback_handler()`` returns
    ``(code, state)``. Either may be sync or async — the ones in
    ``_memhub_auth`` are coroutines and are passed unchanged.

    ``www_authenticate`` is the challenge from a 401 the caller already holds;
    without one the server is asked (``auth_challenge``). Returns the token in
    ``oauth_token`` shape. Storing it is the caller's job, as it was the
    SDK's ``TokenStorage``'s — ``oauth_token_json`` gives the cache text.
    """
    import secrets  # noqa: PLC0415

    if discovery is None:
        if www_authenticate is None:
            www_authenticate = auth_challenge(url, timeout)
        discovery = discover_oauth(url, www_authenticate, timeout)

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(32)
    params = {"response_type": "code", "client_id": client_id,
              "redirect_uri": redirect_uri, "state": state,
              "code_challenge": challenge, "code_challenge_method": "S256"}
    # RFC 8707 `resource` only when protected-resource metadata was found: the
    # SDK's other trigger was the negotiated protocol version, and its first
    # (unauthenticated) request had not negotiated one.
    if discovery.resource:
        params["resource"] = discovery.resource
    if discovery.scope:
        params["scope"] = discovery.scope
    auth_url = f"{discovery.endpoint(url, 'authorization')}?{urllib.parse.urlencode(params)}"
    await _maybe_await(redirect_handler(auth_url))

    code, returned_state = await _maybe_await(callback_handler())
    if returned_state is None or not secrets.compare_digest(returned_state, state):
        raise OAuthFlowError(f"State parameter mismatch: {returned_state} != {state}")
    if not code:
        raise OAuthFlowError("No authorization code received")

    token_url = discovery.endpoint(url, "token")
    # The code and verifier together are a credential for the next minute;
    # the same cleartext rule as every bearer this module sends.
    require_secure(token_url)
    form = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "client_id": client_id,
            "code_verifier": verifier}
    if discovery.resource:
        form["resource"] = discovery.resource
    status, _, body = _http(
        token_url, "POST", urllib.parse.urlencode(form).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"}, timeout)
    if status != 200:
        raise OAuthTokenError(
            f"Token exchange failed ({status}): {body.decode('utf-8', 'replace')}")
    return oauth_token(body)


class DeviceFlowUnavailable(OAuthFlowError):
    """This authorization server or client does not offer the device grant.

    Not a failed login: the caller falls back to the browser flow."""


_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


async def oauth_device_authorize(url: str, client_id: str, show_code, *,
                                 www_authenticate: str | None = None,
                                 discovery: OAuthDiscovery | None = None,
                                 approval_timeout: float = 300.0,
                                 timeout: float = _OAUTH_TIMEOUT_S,
                                 sleep=None) -> dict:
    """The device login (RFC 8628): no localhost callback, so no port to lose.

    The browser flow needs a listener on the pre-registered callback port,
    which ``/mcp``'s own sign-in uses too, and which a container, an SSH
    session or a second login in flight cannot give it. Here the person
    approves a short code on the authorization server's page and this polls
    the token endpoint until they do.

    ``show_code(verification_uri_complete, user_code)`` tells the person
    (opens the page, prints the code). Raises ``DeviceFlowUnavailable`` when
    the server publishes no device endpoint or refuses this client the grant,
    ``OAuthFlowError`` when the person denies it or the code expires. Returns
    the token in ``oauth_token`` shape, like ``oauth_authorize``.
    """
    import asyncio  # noqa: PLC0415
    import time  # noqa: PLC0415

    sleep = sleep or asyncio.sleep
    if discovery is None:
        if www_authenticate is None:
            www_authenticate = auth_challenge(url, timeout)
        discovery = discover_oauth(url, www_authenticate, timeout)
    device_url = (discovery.metadata or {}).get("device_authorization_endpoint")
    if not isinstance(device_url, str) or not device_url:
        raise DeviceFlowUnavailable("no device_authorization_endpoint")
    require_secure(device_url)

    form = {"client_id": client_id}
    if discovery.scope:
        form["scope"] = discovery.scope
    # No `audience` / `resource`: Auth0 rejects the MCP URL as an API name
    # ("Service not found") and issues the tenant's default audience without
    # one, which is the token the browser flow ends up with too.
    status, _, body = _http(
        device_url, "POST", urllib.parse.urlencode(form).encode(),
        {"Content-Type": "application/x-www-form-urlencoded"}, timeout)
    doc = _json_object(body) or {}
    if status != 200:
        if doc.get("error") in ("unauthorized_client", "unsupported_grant_type"):
            raise DeviceFlowUnavailable(doc.get("error"))
        raise OAuthFlowError(
            f"Device authorization failed ({status}): {doc.get('error_description') or doc.get('error') or ''}")
    device_code, user_code = doc.get("device_code"), doc.get("user_code")
    if not (isinstance(device_code, str) and isinstance(user_code, str)):
        raise OAuthFlowError("Device authorization answered without a code")
    page = doc.get("verification_uri_complete") or doc.get("verification_uri")
    await _maybe_await(show_code(page, user_code))

    interval = float(doc.get("interval") or 5)
    expires = min(float(doc.get("expires_in") or approval_timeout), approval_timeout)
    deadline = time.monotonic() + expires
    token_url = discovery.endpoint(url, "token")
    require_secure(token_url)
    poll = {"grant_type": _DEVICE_GRANT, "device_code": device_code,
            "client_id": client_id}
    while True:
        await sleep(interval)
        if time.monotonic() >= deadline:
            raise OAuthFlowError(
                f"Sign-in was not approved within {int(expires)}s. Run login again.")
        status, _, body = _http(
            token_url, "POST", urllib.parse.urlencode(poll).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"}, timeout)
        if status == 200:
            return oauth_token(body)
        error = (_json_object(body) or {}).get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error == "access_denied":
            raise OAuthFlowError("Sign-in was declined in the browser.")
        if error == "expired_token":
            raise OAuthFlowError("The sign-in code expired. Run login again.")
        raise OAuthTokenError(
            f"Token exchange failed ({status}): {body.decode('utf-8', 'replace')[:200]}")
