#!/usr/bin/env python3
"""Personal access key (``mhk_…``) lifecycle for the plugin (stdlib only).

**Why the plugin mints one at all.** Every hook here runs as a cold background
process that must never open a browser, so it can only ever consume a
credential something else provisioned. OAuth gives one that expires in ~24h and
needs a refresh dance the MCP SDK cannot perform from a cold start — which is
how per-turn capture died silently for a day on production. A personal access
key has no such cycle: it is a static bearer, valid until it expires on a
schedule we choose or is revoked.

The trick is that minting one needs no separate credential. The access token
the browser flow already produces is accepted by ``/v1/developer/access-tokens``
(verified against staging), so ``/memhub:login`` can log in once and hand the
hooks something durable — no curl, no pasting, and no waiting on a Settings
page to ship.

**Deliberately stdlib-only.** The whole point of a static bearer is that using
it needs no SDK, so this module must not drag one in: it is imported by the
health check, which runs before the user's first prompt under a bare python3.
Measured, ``uv run --with 'mcp<2'`` costs ~1.1s against ~0.07s here.

**One key per machine, by label.** The secret is returned exactly once, so a
key we did not store is unrecoverable — and the account holds at most five.
Minting per login would exhaust that in a week. Instead each machine claims a
stable label and reuses its stored secret; a same-label key we have no secret
for is an orphan from a lost cache, and is revoked before minting its
replacement so the cap counts real credentials rather than debris.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

CACHE_DIR = Path.home() / ".config" / "memhub-plugin"

# Long enough not to be a chore, short enough that a leaked key is not forever.
# Cheap to renew precisely because /memhub:login mints it programmatically.
DEFAULT_LIFETIME_DAYS = 90

# Capture needs write (import_conversation); recall needs only read. Both are
# requested because a capture-less install is not what anyone is asking for,
# but a read-only key remains a legitimate manual configuration — the write
# tools reject it and the health check reports why.
DEFAULT_SCOPES = ("memory:read", "memory:write")

# The server's own cap. Named so the error path can explain the number.
MAX_KEYS = 5

_TIMEOUT_S = 20


def api_base(mcp_url: str) -> str:
    """The REST origin for an MCP endpoint — same host, no path.

    Derived rather than configured: they are the same deployment, and a second
    setting is a second thing that can point at the wrong environment.
    """
    parts = urlparse(mcp_url)
    return f"{parts.scheme}://{parts.netloc}"


def key_path(mcp_url: str) -> Path:
    """Where this backend's key lives — keyed by host, like the OAuth cache.

    Production and staging issue non-interchangeable keys, so they must never
    share a file.
    """
    return CACHE_DIR / f"pak-{urlparse(mcp_url).netloc.replace(':', '_')}.json"


def default_label() -> str:
    """A stable per-machine name, so this machine can find its own key later.

    Hostname-derived rather than random: the label is the only handle we have
    on a key whose secret we have lost, and re-running login on the same
    machine must resolve to the same one rather than accumulating debris.
    """
    host = (os.environ.get("MEMHUB_PAK_LABEL")
            or f"claude-code-{socket.gethostname().split('.')[0]}")
    return host[:64]


# ── local storage ─────────────────────────────────────────────────────

def load(mcp_url: str) -> dict | None:
    """The stored key record, or None. Never raises."""
    try:
        data = json.loads(key_path(mcp_url).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and data.get("secret") else None


def save(mcp_url: str, record: dict) -> None:
    """Persist the key at 0600, written atomically.

    Atomic because a torn write leaves a file that parses as JSON but carries a
    truncated secret — which would authenticate as nothing while looking, to
    every check we have, exactly like a healthy install.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = key_path(mcp_url)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def forget(mcp_url: str) -> None:
    key_path(mcp_url).unlink(missing_ok=True)


def expires_in_s(record: dict | None) -> float | None:
    """Seconds until the stored key lapses; None if unknown or non-expiring."""
    if not record:
        return None
    raw = record.get("expires_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # Stored as the server returned it: ISO-8601, Zulu.
        stamp = time.strptime(raw.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        return time.mktime(stamp) - time.timezone - time.time()
    except (ValueError, OverflowError):
        return None


# ── server API ────────────────────────────────────────────────────────

class PakError(RuntimeError):
    """A key operation failed, with a message fit to show a person."""


def _call(base: str, bearer: str, method: str, path: str,
          body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{base}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {bearer}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode(errors="replace")[:200]
        raise PakError(f"{method} {path} failed ({e.code}): {detail}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise PakError(f"{method} {path} failed: {e}") from e
    # REST envelope: {"code": 0, "msg": "ok", "data": …}. A non-zero code is a
    # failure the transport reported as 200, so it must not read as success.
    if isinstance(payload, dict) and payload.get("code") not in (0, None):
        raise PakError(f"{method} {path}: {payload.get('msg') or payload}")
    return payload.get("data") if isinstance(payload, dict) else payload


def list_keys(base: str, bearer: str) -> list[dict]:
    data = _call(base, bearer, "GET", "/v1/developer/access-tokens")
    return [k for k in (data or []) if isinstance(k, dict)]


def revoke(base: str, bearer: str, token_id: str) -> None:
    _call(base, bearer, "DELETE", f"/v1/developer/access-tokens/{token_id}")


def mint(base: str, bearer: str, label: str,
         scopes=DEFAULT_SCOPES, lifetime_days: int = DEFAULT_LIFETIME_DAYS) -> dict:
    """Create a key and return ``{secret, id, label, expires_at, scopes}``.

    The secret is returned by the server exactly once, so the caller must
    persist what comes back — there is no second chance to read it.
    """
    expires_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + lifetime_days * 86400))
    data = _call(base, bearer, "POST", "/v1/developer/access-tokens", {
        "label": label, "scopes": list(scopes), "expires_at": expires_at,
    })
    secret = (data or {}).get("secret")
    meta = (data or {}).get("access_token") or {}
    if not secret:
        raise PakError("the server returned no secret for the new key")
    return {
        "secret": secret,
        "id": meta.get("id"),
        "label": meta.get("label") or label,
        "scopes": meta.get("scopes") or list(scopes),
        "expires_at": meta.get("expires_at") or expires_at,
        "created_at": meta.get("created_at"),
    }


def _is_live(key: dict) -> bool:
    return not key.get("revoked_at")


def ensure(mcp_url: str, bearer: str, label: str | None = None) -> tuple[dict, str]:
    """Return ``(record, how)`` — the key this machine should use.

    ``how`` is ``reused`` when the stored key is still good, ``replaced`` when a
    same-label orphan had to be cleared first, or ``minted``.

    Reuse is decided from the LOCAL record, because the server cannot help: it
    never shows a secret twice, so a key listed under our label whose secret we
    do not hold is unusable no matter how healthy it looks. Revoking it before
    minting is what keeps the five-key cap counting credentials that exist
    rather than ghosts of lost caches.
    """
    label = label or default_label()
    base = api_base(mcp_url)

    stored = load(mcp_url)
    if stored and stored.get("label") == label:
        remaining = expires_in_s(stored)
        if remaining is None or remaining > 0:
            return stored, "reused"

    keys = list_keys(base, bearer)
    orphans = [k for k in keys
               if k.get("label") == label and _is_live(k) and k.get("id")]
    for orphan in orphans:
        revoke(base, bearer, orphan["id"])

    live = [k for k in keys if _is_live(k) and k.get("label") != label]
    if len(live) >= MAX_KEYS:
        raise PakError(
            f"you already hold {len(live)} live access keys, the maximum is "
            f"{MAX_KEYS}. Revoke one you no longer use, then run "
            f"/memhub:login again. Existing labels: "
            f"{', '.join(sorted(str(k.get('label')) for k in live))}")

    record = mint(base, bearer, label)
    save(mcp_url, record)
    return record, ("replaced" if orphans else "minted")
