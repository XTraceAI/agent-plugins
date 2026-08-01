"""Shared auth for the plugin's terminal scripts — the SAME OAuth the /mcp
connector uses, so there is no separate CLI token to provision.

Resolution order:
1. ``$MEMHUB_TOKEN`` — explicit bearer for CI / headless runs.
2. OAuth (PKCE, public client) against the MemHub MCP server, using the same
   ``clientId`` / ``callbackPort`` the plugin's ``.mcp.json`` declares for the
   /mcp connector. First run opens the browser once (exactly like
   authenticating in /mcp); tokens are cached at
   ``~/.config/memhub-plugin/tokens-<host>.json`` (0600). A stale access
   token is refreshed proactively by ``_refresh_cached_token_if_stale``
   (below) before the SDK runs — see that function for why the SDK's own
   ``OAuthClientProvider`` refresh can't be relied on from a cold process.

Usage from a sibling script:

    from _memhub_auth import resolve_url_and_auth
    url, headers, auth = resolve_url_and_auth()
    async with streamablehttp_client(url, headers=headers, auth=auth) as ...

Self-check:  uv run --with mcp python _memhub_auth.py
"""
from __future__ import annotations

import asyncio
import base64
import errno
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

_CACHE_DIR = Path.home() / ".config" / "memhub-plugin"


def _plugin_root() -> Path:
    """The installed plugin dir — prod ``memhub`` or ``memhub-staging``.

    Prefer ``$CLAUDE_PLUGIN_ROOT`` (set by Claude Code, authoritative). When it
    is unset (a standalone script run) fall back to this file's location — but
    UNRESOLVED: ``scripts/`` is symlinked into the memhub-staging plugin, so
    ``Path(__file__).resolve()`` would collapse the symlink to the prod
    ``memhub`` dir and read the wrong ``.mcp.json`` (a staging install would
    then auth against and talk to prod). The unresolved path keeps the real
    plugin identity.
    """
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    return Path(root) if root else Path(__file__).parent.parent


def _plugin_mcp_config() -> dict:
    """The memhub server entry from the plugin's .mcp.json (url, oauth)."""
    cfg = _plugin_root() / ".mcp.json"
    servers = json.loads(cfg.read_text()).get("mcpServers", {})
    name = next((k for k in servers if k.lower().startswith("memhub")),
                next(iter(servers)) if len(servers) == 1 else None)
    if not name:
        raise RuntimeError(f"no memhub server entry in {cfg}")
    return servers[name]


def default_url() -> str:
    base = os.environ.get("MEMHUB_MCP_BASE_URL")
    if base:
        path = os.environ.get("MEMHUB_MCP_SERVER_PATH", "/mcp-server/mcp")
        return f"{base.rstrip('/')}{path}"
    try:
        url = _plugin_mcp_config().get("url")
        if url:
            return url
    except Exception:  # noqa: BLE001
        pass
    # .mcp.json was unreadable/corrupt. Don't guess a fixed URL — a single
    # hardcoded env is wrong for one of the two installs (this module is shared
    # with memhub-staging). Derive the backend from the plugin PATH: the install
    # dir is .../<plugin-name>/<version>/, so the version basename says nothing —
    # match the whole path, which always contains "memhub" for a real install, so
    # the raise below is unreachable in practice. Background-hook callers
    # (flush_session, directive_recall) wrap resolve_url_and_auth() in a
    # top-level `except BaseException` and exit 0 quietly, so even if it did fire
    # it degrades soft there; the raise only ever surfaces to a foreground
    # script, where failing loud beats silently talking to the wrong backend.
    root = str(_plugin_root()).lower()
    if "staging" in root:
        return "https://api.staging.memhub.xtrace.ai/mcp-server/mcp"
    if "memhub" in root:
        return "https://api.memhub.xtrace.ai/mcp-server/mcp"
    raise RuntimeError(
        "Cannot determine the MemHub backend: .mcp.json is unreadable and the "
        f"plugin path ({_plugin_root()}) is unrecognized. "
        "Set MEMHUB_MCP_BASE_URL explicitly."
    )


class _FileTokenStorage(TokenStorage):
    """Token cache keyed by server host; client info seeded statically from
    .mcp.json so the SDK skips dynamic client registration (the Auth0 app is
    a pre-registered public client — same one /mcp uses)."""

    def __init__(self, url: str, client_id: str, redirect_uri: str):
        host = urlparse(url).netloc.replace(":", "_")
        self._path = _CACHE_DIR / f"tokens-{host}.json"
        self._client_id = client_id
        self._redirect_uri = redirect_uri

    async def get_tokens(self) -> OAuthToken | None:
        try:
            return OAuthToken.model_validate_json(self._path.read_text())
        except Exception:  # noqa: BLE001
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._path.write_text(tokens.model_dump_json())
        self._path.chmod(0o600)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return OAuthClientInformationFull(
            client_id=self._client_id,
            redirect_uris=[self._redirect_uri],
            token_endpoint_auth_method="none",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
        )

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        return None  # static public client — nothing to persist


class NonInteractiveAuthRequired(RuntimeError):
    """Raised instead of opening a browser when interactive=False.

    Background hooks must never pop a browser at the user — they catch this
    and degrade quietly. With ``_refresh_cached_token_if_stale`` running
    first, a cached token with a live refresh token is renewed before the
    SDK runs, so this is only reached when there is no usable cached token
    at all (never authenticated, or the refresh token itself is dead).
    """


def build_oauth(url: str, interactive: bool = True) -> OAuthClientProvider:
    cfg = _plugin_mcp_config()
    oauth_cfg = cfg.get("oauth", {})
    client_id = oauth_cfg.get("clientId")
    port = int(oauth_cfg.get("callbackPort", 8765))
    if not client_id:
        raise RuntimeError(".mcp.json has no oauth.clientId")
    redirect_uri = f"http://localhost:{port}/callback"

    async def redirect_handler(auth_url: str) -> None:
        if not interactive:
            raise NonInteractiveAuthRequired(
                "no cached OAuth token and interactive auth is disabled"
            )
        print(f"Opening browser to authenticate (same flow as /mcp)...\n  {auth_url}")
        webbrowser.open(auth_url)

    return OAuthClientProvider(
        server_url=url,
        client_metadata=OAuthClientMetadata(
            client_name="MemHub Claude Plugin scripts",
            redirect_uris=[redirect_uri],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=_FileTokenStorage(url, client_id, redirect_uri),
        redirect_handler=redirect_handler,
        callback_handler=_make_callback_handler(port),
    )


def _make_callback_handler(port: int):
    """Factory for the localhost OAuth-redirect waiter (module-level so tests
    can exercise it directly). Each returned coroutine uses ONLY per-call
    state — a second OAuth round in the same process waits for ITS redirect,
    never replaying a stale code."""

    async def callback_handler() -> tuple[str, str | None]:
        result: dict = {}
        done = threading.Event()

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = parse_qs(urlparse(self.path).query)
                code = q.get("code", [None])[0]
                error = q.get("error", [None])[0]
                if code is None and error is None:
                    # favicon / browser prefetch / stray probe — NOT the
                    # OAuth redirect; keep waiting.
                    self.send_response(404)
                    self.end_headers()
                    return
                result["code"] = code
                result["state"] = q.get("state", [None])[0]
                result["error"] = error
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h3>MemHub plugin authenticated."
                    b" You can close this tab.</h3></body></html>"
                    if code else
                    b"<html><body><h3>Authentication failed - see terminal."
                    b"</h3></body></html>"
                )
                done.set()

            def log_message(self, *args):
                return

        # The callback port is FIXED (it's part of the pre-registered OAuth
        # client's redirect URI), so on "address already in use" we cannot
        # fall back to another port — we wait for the holder (a parallel
        # script run or an in-flight /mcp authentication) to release it,
        # then fail with guidance instead of a raw OSError traceback.
        bind_deadline = time.monotonic() + float(
            os.environ.get("MEMHUB_OAUTH_BIND_TIMEOUT", "30")
        )
        while True:
            try:
                server = HTTPServer(("localhost", port), _Handler)
                break
            except OSError as e:
                # Retry ONLY "address in use" — a live listener that may
                # release the port. Permission/interface errors won't heal
                # with waiting; surface them immediately, undisguised.
                if e.errno != errno.EADDRINUSE:
                    raise
                if time.monotonic() >= bind_deadline:
                    raise RuntimeError(
                        f"OAuth callback port {port} is busy — another memhub "
                        "script or an /mcp authentication is mid-flow. Finish "
                        "that approval (or wait a moment) and re-run; the port "
                        "comes from .mcp.json oauth.callbackPort."
                    ) from e
                await asyncio.sleep(1.0)
        server.timeout = 1  # let handle_request tick so the loop can exit

        def serve():
            # server_close() in the finally below can race a handle_request
            # that's mid-poll on the listening socket; swallow the resulting
            # OSError so the user sees ONE clean error, not a daemon-thread
            # traceback interleaved with it.
            try:
                while not done.is_set():
                    server.handle_request()
            except OSError:
                pass

        t = threading.Thread(target=serve, daemon=True)
        t.start()
        # Wait for the browser round-trip without blocking the event loop —
        # but never forever: a closed tab, blocked localhost, or a headless
        # box without $MEMHUB_TOKEN must end in a clear error, not a hang.
        approval_timeout = float(os.environ.get("MEMHUB_OAUTH_TIMEOUT", "300"))
        deadline = time.monotonic() + approval_timeout
        try:
            while not done.is_set():
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"OAuth approval timed out after {int(approval_timeout)}s "
                        "(no browser redirect received; override via "
                        "$MEMHUB_OAUTH_TIMEOUT). Re-run and complete the browser "
                        "approval, or set $MEMHUB_TOKEN for headless use."
                    )
                await asyncio.sleep(0.2)
        finally:
            done.set()  # stop the serve thread
            server.server_close()
        if result.get("error"):
            raise RuntimeError(
                f"authorization server returned error: {result['error']}"
            )
        if not result.get("code"):
            raise RuntimeError("OAuth callback carried no authorization code")
        return result["code"], result.get("state")

    return callback_handler


# Refresh a cached access token this many seconds BEFORE it actually expires,
# so a token that is technically-still-valid but about to lapse mid-request is
# renewed up front rather than 401-ing on the wire.
_REFRESH_SKEW_S = 300


def _access_token_expiry(access_token: str) -> float | None:
    """The ``exp`` (epoch seconds) from a JWT access token's payload, or None
    if it isn't a decodable JWT / carries no ``exp``.

    We only READ the claim to decide whether to refresh — the resource server
    still does the real signature/expiry validation — so no verification key is
    needed. Using the token's own ``exp`` makes the staleness check immune to
    filesystem mtime games (a cp / restore / sync / editor touch that would
    otherwise make an expired token look freshly-issued).
    """
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64url padding
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return float(exp) if exp is not None else None
    except Exception:  # noqa: BLE001 — opaque/non-JWT token → treat as unknown
        return None


def _auth_token_endpoint() -> str | None:
    """The auth server's real ``token_endpoint`` (Auth0), discovered from the
    ``oauth.authServerMetadataUrl`` in the plugin's ``.mcp.json``.

    This is the endpoint the SDK *fails* to reach on a cold refresh (it has no
    discovered ``oauth_metadata`` yet, so it POSTs the refresh to
    ``<resource-server>/token`` instead). We resolve it ourselves.
    """
    try:
        meta_url = _plugin_mcp_config().get("oauth", {}).get("authServerMetadataUrl")
        if not meta_url:
            return None
        with urllib.request.urlopen(meta_url, timeout=10) as resp:
            return json.loads(resp.read()).get("token_endpoint")
    except Exception:  # noqa: BLE001 — best-effort; caller falls back to SDK
        return None


def _refresh_cached_token_if_stale(url: str) -> None:
    """Renew a stale cached access token BEFORE the SDK runs. No-op on success
    paths that don't need it; never raises.

    Why this exists — the MCP SDK's ``OAuthClientProvider`` cannot refresh a
    *reloaded* token from a cold process (as every commit/PR hook is), for two
    compounding reasons:

      1. ``_initialize()`` loads the cached token but never calls
         ``update_token_expiry()``, so ``token_expiry_time`` stays ``None`` and
         ``is_token_valid()`` reports an already-expired access token as valid.
         The pre-emptive refresh branch is skipped; the stale token is sent and
         401s.
      2. Even when a refresh *is* attempted, ``oauth_metadata`` is ``None``
         until the post-401 discovery runs, so ``_refresh_token()`` falls back
         to ``urljoin(server_url, "/token")`` — the resource server, not the
         auth server — and the refresh fails. The SDK then escalates to a FULL
         authorization-code grant, which a background (``interactive=False``)
         hook converts into ``NonInteractiveAuthRequired`` and skips.

    Net effect without this shim: the hook works only while the cached access
    token is inside its short lifetime, then silently stops until the next
    interactive ``/mcp`` or terminal-script auth re-seeds it. So we do the
    refresh here — against the *correct* auth-server ``token_endpoint`` — and
    write the fresh token back, leaving the SDK a valid token to send.

    Best-effort throughout: a missing cache, no refresh token, undiscoverable
    endpoint, or a failed refresh all fall through to the SDK's own flow
    (which opens a browser when interactive, or degrades quietly when not).
    """
    host = urlparse(url).netloc.replace(":", "_")
    path = _CACHE_DIR / f"tokens-{host}.json"
    try:
        cached = json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — no/unreadable cache → nothing to refresh
        return
    refresh_token = cached.get("refresh_token")
    if not refresh_token:
        return

    # Staleness gate, off the token's OWN ``exp`` claim (not file mtime, which
    # a cp/restore/sync can reset and make an expired token look fresh). Skip
    # the network round-trip while the token is still comfortably valid; if
    # exp can't be read (opaque token / no claim), fall through and refresh.
    access_token = cached.get("access_token") or ""
    exp = _access_token_expiry(access_token)
    if exp is not None and time.time() < exp - _REFRESH_SKEW_S:
        return  # still valid per its own exp — let the SDK use it as-is

    token_endpoint = _auth_token_endpoint()
    client_id = _plugin_mcp_config().get("oauth", {}).get("clientId")
    if not token_endpoint or not client_id:
        return

    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }).encode()
    req = urllib.request.Request(
        token_endpoint, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                return
            fresh = json.loads(resp.read())
    except Exception:  # noqa: BLE001 — dead refresh token, network, etc.
        return

    # Carry the new fields onto the existing cache shape only — don't introduce
    # keys (e.g. id_token) the SDK's OAuthToken model wasn't already validating
    # here. Auth0 omits refresh_token when rotation is off; keep the old one.
    updated = dict(cached)
    for k in ("access_token", "expires_in", "scope", "token_type"):
        if k in fresh:
            updated[k] = fresh[k]
    if fresh.get("refresh_token"):
        updated["refresh_token"] = fresh["refresh_token"]
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(updated))
        path.chmod(0o600)
    except OSError:
        return


def resolve_url_and_auth(url: str | None = None, interactive: bool = True):
    """Return (url, headers, auth) for streamablehttp_client.

    $MEMHUB_TOKEN (if set) wins as a plain bearer header — CI/headless escape
    hatch. Otherwise an OAuthClientProvider that reuses the cached token,
    refreshes it, or runs the one-time browser flow. With interactive=False
    (background hooks) the browser flow raises NonInteractiveAuthRequired
    instead of opening a tab; cached/refreshed tokens still work.

    Before handing off to the SDK we proactively renew a stale cached token
    (see ``_refresh_cached_token_if_stale``) — the SDK cannot do this itself
    from a cold process, which silently broke the commit/PR flush hooks.
    """
    url = url or default_url()
    token = os.environ.get("MEMHUB_TOKEN", "").strip()
    if token:
        return url, {"Authorization": f"Bearer {token}"}, None
    _refresh_cached_token_if_stale(url)
    return url, None, build_oauth(url, interactive=interactive)


if __name__ == "__main__":
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _check():
        url, headers, auth = resolve_url_and_auth()
        print(f"endpoint : {url}")
        print(f"mode     : {'bearer ($MEMHUB_TOKEN)' if headers else 'oauth (plugin client)'}")
        async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = await s.list_tools()
                print(f"AUTH OK — server exposes {len(tools.tools)} tools")

    raise SystemExit(asyncio.run(_check()))
