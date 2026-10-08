"""Shared auth for the plugin's scripts and hooks.

**This is a SEPARATE token store from the /mcp connector's.** Both use the same
Auth0 client (the ``clientId`` in the plugin's ``.mcp.json``), which makes them
easy to assume are interchangeable — they are not. Claude Code keeps the /mcp
connector's tokens in its own credential store; every token here is written by
exactly one place, ``OAuthFlow.authorize`` below (and its refresh shim), into
``~/.config/memhub-plugin/tokens-<host>.json``.

The consequence is the whole reason ``/memhub:login`` exists: a user who
installs the plugin and authenticates in ``/mcp`` gets working MCP tools and a
completely unauthenticated capture pipeline. The hooks call
``resolve_url_and_auth(interactive=False)``, find no token here, and — because a
background hook must never pop a browser — skip in silence. Nothing can mint
this token except a FOREGROUND run of a plugin script, so provisioning it must
be something the user is told to do, not something they stumble into.

Resolution order:
1. An explicit bearer (``explicit_token``): the plugin's ``memhub_token``
   userConfig option, which Claude Code hands its hooks as
   ``$CLAUDE_PLUGIN_OPTION_MEMHUB_TOKEN``, else ``$MEMHUB_TOKEN`` (read only
   by ``_memhub_env_token``, which the directory build omits) — CI /
   headless runs.
2. A stored personal access key (``pak``), minted by ``/memhub:login``.
3. OAuth (PKCE, public client) against the MemHub MCP server, using the same
   ``clientId`` / ``callbackPort`` the plugin's ``.mcp.json`` declares for the
   /mcp connector. First run opens the browser once (exactly like
   authenticating in /mcp); tokens are cached at
   ``~/.config/memhub-plugin/tokens-<host>.json`` (0600). A stale access
   token is refreshed proactively by ``_refresh_cached_token_if_stale``
   (below) before anything is sent — see that function for why the refresh
   the MCP SDK used to own could not be relied on from a cold process.

Usage — the HOOKS take the non-interactive path:

    from _memhub_auth import resolve_bearer
    url, bearer = resolve_bearer()          # None when nothing is usable
    mcp_http.call_tool(url, bearer, ...)

FOREGROUND scripts that may open a browser take the interactive one:

    url, headers, auth = resolve_url_and_auth(url)
    session = await open_session(url, headers, auth)
    await session.call_tool("save_artifact", arguments={...})

Everything here is stdlib: the browser flow is ``mcp_http.oauth_authorize``.

Self-check:  python3 _memhub_auth.py
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

# `mcp_http` (and with it the browser flow) is imported LAZILY, inside the
# functions that send something. Hooks import this file on every invocation,
# several of them synchronously, and most only need a bearer string.

# $MEMHUB_CONFIG_DIR moves the credentials (token cache, access key) so a
# harness can sign in fresh without touching this machine's real key.
_CACHE_DIR = Path(os.environ.get("MEMHUB_CONFIG_DIR")
                  or Path.home() / ".config" / "memhub-plugin")

# The ``memhub_token`` userConfig option in ``.claude-plugin/plugin.json``.
# Claude Code keeps the value (sensitive) in its secure store and exports it to
# HOOK processes only, as ``CLAUDE_PLUGIN_OPTION_<KEY>`` uppercased — not to
# the MCP ``headersHelper``, not to commands run through the Bash tool, and not
# on Codex or Cursor, all of which therefore fall through to the next source.
_USER_CONFIG_TOKEN_ENV = "CLAUDE_PLUGIN_OPTION_MEMHUB_TOKEN"


def explicit_token() -> tuple[str, str] | None:
    """``(token, source)`` for a bearer the user supplied outside /memhub:login.

    The userConfig option first, then ``$MEMHUB_TOKEN``; both outrank the stored
    access key, exactly as ``$MEMHUB_TOKEN`` alone always has. An option left
    empty is unset, not an empty credential. ``source`` names which one won, for
    the status lines that must say which credential is in use.

    ``$MEMHUB_TOKEN`` is read by ``_memhub_env_token`` and nowhere else. The
    Claude plugin directory build omits that module — a listed plugin may not
    pick a credential up from the user's environment — and there the import
    fails and only the option applies. Every other build ships it.
    """
    token = os.environ.get(_USER_CONFIG_TOKEN_ENV, "").strip()
    if token:
        return token, "plugin option memhub_token"
    try:
        from _memhub_env_token import env_token  # noqa: PLC0415 — absent in the directory build
    except ImportError:
        return None
    return env_token()


def _headless_hint() -> str:
    """The explicit credential a headless user can set on THIS build.

    ``$MEMHUB_TOKEN`` where ``_memhub_env_token`` ships; the ``memhub_token``
    plugin option in the directory build, which ignores the variable — naming
    it there would send the user to a setting that does nothing.
    """
    try:
        import _memhub_env_token  # noqa: F401, PLC0415 — absent in the directory build
    except ImportError:
        return "the memhub_token plugin option"
    return "$MEMHUB_TOKEN"


def _plugin_root() -> Path:
    """The installed plugin dir, whose manifests and ``.mcp.json`` say what it is.

    Claude sets ``$CLAUDE_PLUGIN_ROOT``, but Cursor's compatibility loader can
    set it to a different installed plugin. Trust the variable only when its
    auth module is this running file.

    When no trustworthy root is present (a standalone script or Cursor), use
    this file's unresolved location.
    """
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root:
        candidate = Path(root)
        try:
            if (candidate / "scripts" / Path(__file__).name).samefile(__file__):
                return candidate
        except OSError:
            pass
    # A copied install passes samefile above because __file__ is in that copy;
    # reaching here means the advertised root does not own this module.
    return Path(__file__).parent.parent


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
    """The backend this install talks to: the url in its own ``.mcp.json``.

    ``$MEMHUB_MCP_BASE_URL`` overrides it, and nothing else is consulted. The
    plugin's directory says nothing about the backend: the public ``memhub`` is
    an export of the ``memhub-staging`` tree with a different ``.mcp.json``. So
    an unreadable config raises rather than guessing. Background-hook callers
    (flush_session) wrap resolve_url_and_auth() in a
    top-level ``except BaseException`` and exit 0 quietly; the raise only
    surfaces to a foreground script, where failing loud beats silently talking
    to the wrong backend.
    """
    base = os.environ.get("MEMHUB_MCP_BASE_URL")
    if base:
        path = os.environ.get("MEMHUB_MCP_SERVER_PATH", "/mcp-server/mcp")
        return f"{base.rstrip('/')}{path}"
    try:
        url = _plugin_mcp_config().get("url")
        if url:
            # Host-owned MCP connections use the version in their loaded config.
            # Python transports report their own import-time version in a header.
            parts = urllib.parse.urlsplit(url)
            query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                     if k != "memhub_plugin_version"]
            url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Cannot determine the MemHub backend: {_plugin_root() / '.mcp.json'} "
            f"is unreadable ({type(exc).__name__}). Set MEMHUB_MCP_BASE_URL explicitly."
        ) from exc
    if not url:
        raise RuntimeError(
            f"Cannot determine the MemHub backend: {_plugin_root() / '.mcp.json'} "
            "names no url. Set MEMHUB_MCP_BASE_URL explicitly.")
    return url


def plugin_name() -> str:
    """This install's plugin name: ``memhub``, or ``memhub-staging``.

    Every host namespaces the plugin's skills by it, so a message telling the
    user which command to run must use it: ``/memhub:login`` does not exist on
    a staging install. Read from the installed manifests, which
    tests/version_parity_test.py keeps in agreement; ``memhub`` when none is
    readable.
    """
    root = _plugin_root()
    for rel in ("plugin.json", ".claude-plugin/plugin.json",
                ".codex-plugin/plugin.json", ".cursor-plugin/plugin.json"):
        try:
            name = json.loads((root / rel).read_text(encoding="utf-8")).get("name")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(name, str) and name:
            return name
    return "memhub"


def skill_command(skill: str) -> str:
    """How the user runs ``skill`` on this install, e.g. ``/memhub-staging:login``."""
    return f"/{plugin_name()}:{skill}"


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


class NonInteractiveAuthRequired(RuntimeError):
    """Raised instead of opening a browser when interactive=False.

    Background callers must never pop a browser at the user — they catch this
    and degrade quietly. With ``_refresh_cached_token_if_stale`` running
    first, a cached token with a live refresh token is renewed before anything
    is sent, so this is only reached when there is no usable cached token at
    all (never authenticated, or the refresh token itself is dead).
    """


class OAuthFlow:
    """The plugin's OAuth client — what the SDK's ``OAuthClientProvider`` was.

    A pre-registered public client (``.mcp.json``'s ``clientId`` /
    ``callbackPort``, the same one /mcp uses), so there is no dynamic
    registration and nothing to persist but the token. ``bearer()`` hands back
    the cached token or runs the browser flow; ``authorize()`` always runs it
    and is what a 401 on a cached token triggers, as the SDK's did.
    """

    def __init__(self, url: str, interactive: bool = True):
        cfg = _plugin_mcp_config()
        oauth_cfg = cfg.get("oauth", {})
        self.client_id = oauth_cfg.get("clientId")
        self.port = int(oauth_cfg.get("callbackPort", 8765))
        if not self.client_id:
            raise RuntimeError(".mcp.json has no oauth.clientId")
        self.url = url
        self.interactive = interactive
        self.redirect_uri = f"http://localhost:{self.port}/callback"

    async def redirect_handler(self, auth_url: str) -> None:
        if not self.interactive:
            raise NonInteractiveAuthRequired(
                "no cached OAuth token and interactive auth is disabled"
            )
        print(f"Opening browser to authenticate (same flow as /mcp)...\n  {auth_url}")
        webbrowser.open(auth_url)

    async def authorize(self, www_authenticate: str | None = None) -> str:
        """Run the browser flow, cache the token, return its access token."""
        if not self.interactive:
            # Before any network: discovery would only end at the same refusal.
            raise NonInteractiveAuthRequired(
                "no cached OAuth token and interactive auth is disabled"
            )
        import atomic_write  # noqa: PLC0415 — stdlib, beside this file
        import mcp_http  # noqa: PLC0415

        token = None
        # The device code first: it needs no localhost callback, so a busy
        # callback port (an /mcp sign-in mid-flow, a second login), a container
        # or an SSH session cannot break it. The browser flow stays as the
        # fallback for a server that does not offer the grant.
        if os.environ.get("MEMHUB_LOGIN_FLOW", "").strip().lower() != "browser":
            discovery = mcp_http.discover_oauth(
                self.url, www_authenticate or mcp_http.auth_challenge(self.url))
            try:
                token = await mcp_http.oauth_device_authorize(
                    self.url, self.client_id, _show_device_code,
                    discovery=discovery,
                    approval_timeout=float(os.environ.get("MEMHUB_OAUTH_TIMEOUT", "300")))
            except mcp_http.DeviceFlowUnavailable:
                token = None
        if token is None:
            token = await mcp_http.oauth_authorize(
                self.url, self.client_id, self.redirect_uri,
                self.redirect_handler, _make_callback_handler(self.port),
                www_authenticate=www_authenticate)
        # Same writer as every other credential here: atomic and created 0600,
        # so a hook reading concurrently never catches it half-written.
        atomic_write.publish(token_cache_path(self.url),
                             mcp_http.oauth_token_json(token))
        return token["access_token"]

    async def bearer(self) -> str:
        """The cached access token, else a fresh one from the browser flow."""
        return _cached_access_token(self.url) or await self.authorize()


def _show_device_code(page: str | None, user_code: str) -> None:
    """Open the approval page with the code filled in, and print the code.

    The page asks the person to confirm the code matches; the printed line is
    what they compare it with, and what they type on another device when this
    one has no browser."""
    print(f"Sign in to MemHub: confirm the code {user_code} in your browser.\n  {page}",
          flush=True)
    if page:
        webbrowser.open(page)


def build_oauth(url: str, interactive: bool = True) -> OAuthFlow:
    """The OAuth client for ``url`` (see ``OAuthFlow``)."""
    return OAuthFlow(url, interactive=interactive)


def _make_callback_handler(port: int):
    """Factory for the localhost OAuth-redirect waiter (module-level so tests
    can exercise it directly). Each returned coroutine uses ONLY per-call
    state — a second OAuth round in the same process waits for ITS redirect,
    never replaying a stale code."""

    async def callback_handler() -> tuple[str, str | None]:
        from plugin_onboarding import active_completion, send_callback_response

        completion = active_completion.get()
        result: dict = {}
        done = threading.Event()

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                q = parse_qs(parsed.query)
                code = q.get("code", [None])[0]
                error = q.get("error", [None])[0]
                if code is None and error is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                result["code"] = code
                result["state"] = q.get("state", [None])[0]
                result["error"] = error
                if completion is not None:
                    completion.callback_received = True
                # Unblock the flow for state validation, PKCE exchange and storage.
                # Hold the browser response until the foreground command verifies
                # the credential. Closing the listening socket does not close this
                # accepted request socket.
                done.set()
                try:
                    if code and not error and completion is not None:
                        finished = completion.finished.wait(120)
                        send_callback_response(
                            self, succeeded=finished and completion.succeeded,
                            destination=completion.destination,
                        )
                    else:
                        # Callers outside login.py have no verified completion
                        # signal. A callback code must never be called a success.
                        send_callback_response(self, succeeded=bool(code) and not error)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # A closed browser must not discard a valid login.
                finally:
                    if completion is not None:
                        completion.response_sent.set()

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
                        f"approval, or set {_headless_hint()} for headless use."
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

    This is the endpoint the MCP SDK *failed* to reach on a cold refresh (it had no
    discovered ``oauth_metadata`` yet, so it POSTed the refresh to
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
    except Exception:  # noqa: BLE001 — best-effort; caller falls back to OAuthFlow
        return None


def _refresh_cached_token_if_stale(url: str) -> None:
    """Renew a stale cached access token BEFORE anything is sent. No-op on
    success paths that don't need it; never raises.

    Why this exists — the MCP SDK's ``OAuthClientProvider``, which the
    foreground scripts used until they moved to ``OAuthFlow``, could not refresh a
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
    write the fresh token back, leaving a valid token to send.

    ``OAuthFlow`` has no refresh of its own, so this is still the only one.

    Best-effort throughout: a missing cache, no refresh token, undiscoverable
    endpoint, or a failed refresh all fall through to ``OAuthFlow`` (which
    opens a browser when interactive, or degrades quietly when not).
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
        return  # still valid per its own exp — use it as-is

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
    # keys (e.g. id_token) the cache's token shape (``mcp_http.oauth_token``)
    # does not carry. Auth0 omits refresh_token when rotation is off; keep the old one.
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
    # and the PreToolUse hooks, which fire on tool calls — so a
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
    """``(url, bearer)`` for a NON-INTERACTIVE caller.

    This is what every hook uses. It returns the same credential a foreground
    ``open_session`` would put on the wire, without ever opening a browser:

    1. ``explicit_token()`` — the ``memhub_token`` plugin option, else
       ``$MEMHUB_TOKEN``; an explicit bearer, CI/headless;
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

    explicit = explicit_token()
    if explicit:
        return url, explicit[0]

    secret = _stored_pak_secret(url)
    if secret is not None:
        return url, secret

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


def _stored_pak_secret(url: str) -> str | None:
    """Return only a secret that is safe to put in a bearer header."""
    record = _stored_pak(url)
    if not record:
        return None
    # isinstance, not just truthy: f-string would turn a malformed value into
    # a nonsense credential and fail as a puzzling 401 rather than as "no key".
    secret = record.get("secret")
    return secret if isinstance(secret, str) and secret else None


def resolve_url_and_auth(url: str | None = None, interactive: bool = True):
    """Return ``(url, headers, auth)`` for ``open_session``.

    An explicit token (``explicit_token``: the ``memhub_token`` plugin option,
    else $MEMHUB_TOKEN) wins as a plain bearer header — CI/headless escape
    hatch. Otherwise an ``OAuthFlow`` that reuses the cached token or runs the
    one-time browser flow. With interactive=False (background callers) the
    browser flow raises NonInteractiveAuthRequired instead of opening a tab;
    cached/refreshed tokens still work.

    ``headers`` carries ``Authorization`` only for a static bearer (explicit
    token or stored key), so a caller that must not use the OAuth cache — as
    ``rule_decide`` must not — can tell them apart by it alone.

    Before handing back an ``OAuthFlow`` we proactively renew a stale cached
    token (see ``_refresh_cached_token_if_stale``) — nothing else can do this
    from a cold process, which once silently broke the commit/PR flush hooks.
    """
    from plugin_version import request_headers

    url = url or default_url()
    explicit = explicit_token()
    if explicit:
        return url, {"Authorization": f"Bearer {explicit[0]}", **request_headers()}, None

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
    secret = _stored_pak_secret(url)
    if secret is not None:
        return url, {"Authorization": f"Bearer {secret}", **request_headers()}, None

    _refresh_cached_token_if_stale(url)
    return url, request_headers(), build_oauth(url, interactive=interactive)


class AuthedSession:
    """``mcp_http.Session`` plus the SDK's one OAuth recovery.

    ``call_tool`` / ``list_tools`` run over the stdlib transport. When the
    bearer came from ``OAuthFlow`` and the server answers 401 — a cached token
    revoked, or issued by the other tenant — the browser flow runs and the call
    is retried ONCE, which is what the SDK's ``OAuthClientProvider`` did. A
    static bearer (explicit token, stored key) has no such recovery: its 401 is
    the caller's failure, as it always was.
    """

    def __init__(self, url: str, bearer: str, flow: OAuthFlow | None,
                 timeout: float):
        self.url = url
        self.bearer = bearer
        self._flow = flow
        self._timeout = timeout

    async def _with_reauth(self, op):
        import mcp_http  # noqa: PLC0415

        try:
            return await op(self.bearer)
        except mcp_http.McpError as exc:
            if self._flow is None or exc.status != 401:
                raise
        self.bearer = await self._flow.authorize()
        return await op(self.bearer)

    async def call_tool(self, name: str, arguments: dict | None = None,
                        timeout: float | None = None):
        import mcp_http  # noqa: PLC0415

        return await self._with_reauth(
            lambda bearer: mcp_http.Session(self.url, bearer, self._timeout)
            .call_tool(name, arguments, timeout))

    async def list_tools(self) -> list[dict]:
        """Tool descriptors (one page, as the SDK's ``list_tools`` returned)."""
        import mcp_http  # noqa: PLC0415

        return await self._with_reauth(
            lambda bearer: asyncio.to_thread(
                mcp_http.list_tools, self.url, bearer, self._timeout))


async def open_session(url: str, headers: dict, auth: OAuthFlow | None,
                       timeout: float | None = None) -> AuthedSession:
    """A session for ``resolve_url_and_auth``'s result — where the SDK's
    ``streamablehttp_client`` + ``ClientSession`` used to be opened.

    The bearer is resolved here: the static one in ``headers``, or the OAuth
    token (cached, else from the browser flow — or NonInteractiveAuthRequired).
    ``timeout`` defaults to the SDK's 300s read timeout, not the hooks' 60s: a
    large save or import legitimately takes longer than a hook would wait.
    """
    import mcp_http  # noqa: PLC0415

    value = str((headers or {}).get("Authorization", ""))
    if value.startswith("Bearer ") and value[len("Bearer "):]:
        bearer, flow = value[len("Bearer "):], None
    elif auth is not None:
        bearer, flow = await auth.bearer(), auth
    else:
        raise RuntimeError("no MemHub credential to open a session with")
    return AuthedSession(url, bearer, flow,
                         mcp_http.SDK_READ_TIMEOUT_S if timeout is None else timeout)


if __name__ == "__main__":
    async def _check():
        url, headers, auth = resolve_url_and_auth()
        print(f"endpoint : {url}")
        explicit = explicit_token()
        mode = (f"bearer ({explicit[1]})" if explicit
                else "stored access key" if auth is None else "oauth (plugin client)")
        print(f"mode     : {mode}")
        session = await open_session(url, headers, auth)
        tools = await session.list_tools()
        print(f"AUTH OK — server exposes {len(tools)} tools")

    raise SystemExit(asyncio.run(_check()))
