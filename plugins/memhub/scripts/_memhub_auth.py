"""Shared auth for the plugin's scripts and hooks.

**This is a SEPARATE token store from the /mcp connector's.** Both use the same
Auth0 client (the ``clientId`` in the plugin's ``.mcp.json``), which makes them
easy to assume are interchangeable — they are not. Claude Code keeps the /mcp
connector's tokens in its own credential store; every token here is written by
exactly one place, ``_FileTokenStorage`` below, into
``~/.config/memhub-plugin/tokens-<host>.json``.

The consequence is the whole reason ``/memhub:login`` exists: a user who
installs the plugin and authenticates in ``/mcp`` gets working MCP tools and a
completely unauthenticated capture pipeline. The hooks call
``resolve_url_and_auth(interactive=False)``, find no token here, and — because a
background hook must never pop a browser — skip in silence. Nothing can mint
this token except a FOREGROUND run of a plugin script, so provisioning it must
be something the user is told to do, not something they stumble into.

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

Usage — the HOOKS take the stdlib path and never load the SDK:

    from _memhub_auth import resolve_bearer
    url, bearer = resolve_bearer()          # None when nothing is usable
    mcp_http.call_tool(url, bearer, ...)

Only the interactive browser flow still needs the SDK, via
``resolve_url_and_auth`` / ``build_oauth``, and only ``login.py`` calls it.

Self-check:  uv run --with 'mcp<2' python _memhub_auth.py
"""
from __future__ import annotations

import asyncio
import base64
import errno
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# The mcp SDK is imported LAZILY, inside `build_oauth`, and nowhere else.
#
# It is needed for exactly one thing: the interactive browser flow, whose PKCE
# and token-exchange machinery is genuinely worth not hand-rolling. Everything
# else here — reading a cached token, refreshing it, resolving a bearer — is
# stdlib already.
#
# Keeping it out of module scope is what lets the hooks import this file under a
# bare python3. Measured, that is 0.07s against 1.09s for `uv run --with
# 'mcp<2'`, and three of the hooks paying that cost are SYNCHRONOUS — the
# PreToolUse directive check has no prefilter, so it was a second of latency on
# every single file edit.

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
    servers = json.loads(cfg.read_text(encoding="utf-8")).get("mcpServers", {})
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


def token_cache_path(url: str) -> Path:
    """Where this backend's cached OAuth token lives.

    Keyed by HOST, because prod and staging are different Auth0 tenants issuing
    tokens that are not interchangeable — sharing one file would have a staging
    login silently overwrite a prod one. Public because ``login.py`` inspects
    the token this module just wrote (to report whether it can ever be renewed)
    and must key it identically; the keying used to be spelled out separately at
    each use, which is exactly how two copies drift.
    """
    return _CACHE_DIR / f"tokens-{urlparse(url).netloc.replace(':', '_')}.json"


def _file_token_storage(url: str, client_id: str, redirect_uri: str):
    """The SDK's ``TokenStorage`` over our cache file.

    Defined INSIDE a function because it subclasses an SDK type, and a
    subclass at module scope would force the import that this module exists to
    avoid — the whole point being that a hook can import this file under a bare
    python3. It is only ever constructed by ``build_oauth``, which already has
    the SDK loaded.
    """
    from mcp.client.auth import TokenStorage
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    class _FileTokenStorage(TokenStorage):
        """Token cache keyed by server host; client info seeded statically from
        .mcp.json so the SDK skips dynamic client registration (the Auth0 app is
        a pre-registered public client — same one /mcp uses)."""

        def __init__(self):
            self._path = token_cache_path(url)

        async def get_tokens(self):
            try:
                return OAuthToken.model_validate_json(
                    self._path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return None

        async def set_tokens(self, tokens) -> None:
            # Same writer as every other credential here. Written-then-chmod'd
            # left the token at the process umask — 0644 by default — for a
            # window, and non-atomically, so a hook reading concurrently could
            # catch it half-written and conclude there was no credential.
            import atomic_write  # noqa: PLC0415 — stdlib, beside this file

            atomic_write.publish(self._path, tokens.model_dump_json())

        async def get_client_info(self):
            return OAuthClientInformationFull(
                client_id=client_id,
                redirect_uris=[redirect_uri],
                token_endpoint_auth_method="none",
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
            )

        async def set_client_info(self, info) -> None:
            return None  # static public client — nothing to persist

    return _FileTokenStorage()


class NonInteractiveAuthRequired(RuntimeError):
    """Raised instead of opening a browser when interactive=False.

    Background hooks must never pop a browser at the user — they catch this
    and degrade quietly. With ``_refresh_cached_token_if_stale`` running
    first, a cached token with a live refresh token is renewed before the
    SDK runs, so this is only reached when there is no usable cached token
    at all (never authenticated, or the refresh token itself is dead).
    """


def build_oauth(url: str, interactive: bool = True):
    """The SDK's OAuth provider — the ONE place the mcp package is required.

    Only ``login.py`` reaches here now. The hooks resolve a static bearer
    instead (see ``resolve_bearer``) and never load the SDK at all.
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

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
        storage=_file_token_storage(url, client_id, redirect_uri),
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
        # The metadata URL itself must be https: it is fetched over the network
        # and its answer decides where a long-lived refresh token gets POSTed.
        if urlparse(meta_url).scheme != "https":
            return None
        with urllib.request.urlopen(meta_url, timeout=10) as resp:
            endpoint = json.loads(resp.read()).get("token_endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            return None
        # SAME ORIGIN as the document that named it. The refresh token is the
        # most durable credential this plugin holds, and without this check a
        # tampered discovery document could name any host and we would POST it
        # there — the document is fetched from the network, so it is not ours
        # to trust the way .mcp.json is.
        #
        # Verified non-breaking against both live tenants: staging and prod each
        # serve a token_endpoint on their own discovery host.
        if urlparse(endpoint).netloc != urlparse(meta_url).netloc:
            # SAY SO. Rejecting silently would stop refresh, and a token that
            # stops refreshing dies quietly a day later — the precise failure
            # this plugin's health machinery exists to eliminate, reintroduced
            # by the guard meant to make things safer. Auth0 serves the token
            # endpoint on the discovery host (checked on both tenants, custom
            # domains included by design), so reaching this line means either a
            # tampered document or a deployment shape nobody has seen — and
            # both are worth a line someone can find.
            print(f"[memhub-auth] refusing token_endpoint "
                  f"{urlparse(endpoint).netloc!r}: not the origin that named it "
                  f"({urlparse(meta_url).netloc!r}). Token refresh is disabled "
                  "until this is resolved.", file=sys.stderr)
            return None
        return endpoint
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
    path = token_cache_path(url)
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — no/unreadable cache → nothing to refresh
        return
    if not isinstance(cached, dict):
        return  # valid JSON, wrong shape — nothing to refresh from
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
    # The refresh token — a long-lived credential — is POSTed here, and the
    # endpoint comes from a discovery document named in .mcp.json rather than
    # from anything we control. Same rule as every other credentialed call:
    # https, or don't send it.
    if urlparse(token_endpoint).scheme != "https":
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
    # Type-checked before merging. These fields are echoed straight back onto
    # the wire as a bearer, and a malformed response — a dict where a string
    # belongs — would be written to the cache and then formatted into an
    # Authorization header, failing later as a puzzling 401 rather than here as
    # a bad refresh. Skipping a wrong-typed field keeps the previous, valid one.
    for k in ("access_token", "scope", "token_type"):
        if isinstance(fresh.get(k), str) and fresh[k]:
            updated[k] = fresh[k]
    if isinstance(fresh.get("expires_in"), (int, float)):
        updated["expires_in"] = fresh["expires_in"]
    if isinstance(fresh.get("refresh_token"), str) and fresh["refresh_token"]:
        updated["refresh_token"] = fresh["refresh_token"]
    # A refresh that returned no usable access token is not a refresh. Writing
    # the old document back would be harmless but pointless; bailing keeps the
    # cache untouched and lets the caller fall through to the existing token.
    if not isinstance(updated.get("access_token"), str):
        return
    # ATOMIC, and that matters more now than it used to. Several hooks resolve
    # a credential concurrently — the per-turn flush, the SessionEnd backstop,
    # and the PreToolUse directive check, which fires on every edit — so a
    # plain write_text leaves a window where another process reads a truncated
    # file. That reader does not fail loudly: it decides there is no usable
    # credential and skips, so a torn write reads exactly like "not logged in"
    # and capture goes dark for that call.
    #
    # Written to a temp file and renamed, so a reader sees either the old token
    # or the new one. Created 0600 by os.open rather than chmod'd afterwards,
    # so the secret is never briefly world-readable. Two writers racing is
    # harmless: both wrote a valid token, and rename picks one whole.
    # Several hooks refresh concurrently — the per-turn flush, the SessionEnd
    # backstop, and the PreToolUse check that fires on every edit — so a torn
    # write here is not hypothetical, and a reader catching one concludes there
    # is no usable credential and skips. See `atomic_write` for why the temp
    # name has to be per-process rather than shared.
    #
    # Best-effort, like the rest of this function: a refresh that cannot be
    # persisted leaves the previous token in place for the caller to use.
    try:
        import atomic_write  # noqa: PLC0415 — stdlib, beside this file

        atomic_write.publish(path, json.dumps(updated))
    except OSError:
        return


def _cached_access_token(url: str) -> str | None:
    """The cached OAuth access token if it is present and not expired.

    Pure file read — no network, so it is safe on a latency budget.
    """
    try:
        cached = json.loads(token_cache_path(url).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(cached, dict):
        return None
    access = cached.get("access_token")
    if not isinstance(access, str) or not access:
        return None
    exp = _access_token_expiry(access)
    if exp is not None and time.time() >= exp:
        return None
    return access


def resolve_bearer(url: str | None = None,
                   refresh: bool = True) -> tuple[str, str | None]:
    """``(url, bearer)`` for a NON-INTERACTIVE caller — stdlib only, no SDK.

    This is what every hook uses now. It returns the same credential the SDK
    would have ended up putting on the wire, without loading the SDK to get
    there:

    1. ``$MEMHUB_TOKEN`` — explicit bearer, CI/headless;
    2. a stored personal access key — the normal path once /memhub:login has
       run, and a static string with no lifecycle to manage;
    3. the cached OAuth access token, refreshed first if stale. Worth keeping
       even though a key supersedes it: an install that has not re-logged-in
       since keys existed keeps working instead of going dark on upgrade, and
       the refresh shim was already pure stdlib.

    ``bearer`` is None when there is nothing usable, which is not an error —
    it is the state a background hook must degrade quietly on. The caller skips,
    and `capture_health` is what tells the user, on a synchronous hook, where
    saying it actually reaches them.
    """
    url = url or default_url()

    token = os.environ.get("MEMHUB_TOKEN", "").strip()
    if token:
        return url, token

    record = _stored_pak(url)
    # isinstance, not just truthy: the secret is formatted straight into an
    # Authorization header, so a non-string would be rendered by f-string into
    # a nonsense credential and fail as a puzzling 401 rather than as "no key".
    if record and isinstance(record.get("secret"), str):
        return url, record["secret"]

    # Renew before reading: the cached access token is short-lived, and this
    # shim is the only thing that ever renews it from a cold process.
    #
    # ``refresh=False`` exists for callers on a LATENCY BUDGET, and it is not a
    # micro-optimisation — it is the only real bound available to them. A
    # refresh makes two blocking urllib calls (~25s of socket timeout), and
    # offloading it with ``asyncio.to_thread`` does NOT make it cancellable:
    # measured, a `wait_for(to_thread(...), 2.5)` around an 8s blocking call
    # returned after 8.01s, not 2.5s — cancelling the future does not stop the
    # thread, and the await does not finish until the thread does. So a
    # synchronous hook cannot time-bound a refresh at all; it can only decline
    # to attempt one.
    #
    # Declining is safe because the refresh is not this caller's job. The
    # async capture hooks run with budgets that accommodate it and will renew
    # the token; a caller that skips simply goes without a credential for one
    # invocation, which for a best-effort context lookup means one recall
    # missed rather than an edit stalled.
    if refresh:
        _refresh_cached_token_if_stale(url)
    # ONE reader for both branches. They were written separately and had already
    # started to diverge — the same two-copies-of-one-rule pattern that produced
    # most of this PR's bugs — and here the drift would be silent: a stricter
    # check on one path means capture works from one hook and not another, with
    # nothing to indicate why.
    return url, _cached_access_token(url)


def _stored_pak(url: str) -> dict | None:
    """This backend's stored access key, if it exists and has not lapsed.

    Imported lazily and guarded: ``pak`` is stdlib-only and sits beside this
    file, but auth must not become the reason a hook dies. A missing or broken
    key module simply means "no key", and the OAuth path still applies.
    """
    try:
        import pak  # noqa: PLC0415 — local, stdlib-only
        record = pak.load(url)
        if not record:
            return None
        remaining = pak.expires_in_s(record)
        return record if remaining is None or remaining > 0 else None
    except Exception:  # noqa: BLE001
        return None


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

    # A stored personal access key, minted by /memhub:login. Preferred over the
    # OAuth cache because it is a STATIC bearer: no expiry inside a session, no
    # refresh, and therefore none of the cold-process failure modes that made a
    # background hook's credential unreliable. Checked before the OAuth path so
    # an install that has one never touches the refresh machinery at all.
    #
    # An expired key deliberately falls THROUGH to OAuth rather than failing:
    # the OAuth cache is the older credential and may still work, and a
    # degraded-but-working capture beats a confident dead end. The health check
    # reports the lapsed key either way.
    record = _stored_pak(url)
    if record:
        return url, {"Authorization": f"Bearer {record['secret']}"}, None

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
