"""Persistent upgrade status and bounded recovery; never touches capture cursors."""
from __future__ import annotations

from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import atomic_write
from plugin_version import ACTIVE_PLUGIN_VERSION, upgrade_message

STATE_DIR = Path.home() / ".config/memhub-plugin/compatibility"
RECHECK_SECONDS = 60
_checking = ContextVar("plugin_compatibility_check", default=False)


def _path(url, bearer):
    parts = urlsplit(url)
    key = hashlib.sha256(f"{parts.scheme}://{parts.netloc}|{bearer}".encode()).hexdigest()
    return STATE_DIR / (key + ".json")


def status(url, bearer):
    try:
        value = json.loads(_path(url, bearer).read_text(encoding="utf-8"))
        minimum = value.get("minimum_version")
        if isinstance(minimum, str) and re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", minimum):
            return value
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def record(url, bearer, minimum):
    try:
        atomic_write.publish(_path(url, bearer), json.dumps({
            "minimum_version": minimum, "active_version": ACTIVE_PLUGIN_VERSION,
            "checked_at": time.time(),
        }))
    except OSError:
        pass  # The current rejection still reaches the caller if disk is full.


def check(url, bearer, *, timeout=2):
    """Clear a refusal only after authenticated status AND a protected read.

    A no-op update, unavailable release, failed restart, or temporary server
    failure leaves the status and every queued capture intact.
    """
    import mcp_http

    old = status(url, bearer)
    def retain():
        if old:
            record(url, bearer, old["minimum_version"])
        return status(url, bearer)

    token = _checking.set(True)
    try:
        parts = urlsplit(url)
        endpoint = f"{parts.scheme}://{parts.netloc}/v1/plugin/compatibility"
        data = mcp_http.rest(endpoint, bearer, timeout=timeout).data
        if not isinstance(data, dict) or not isinstance(data.get("operations_enforced"), bool):
            return retain()
        minimum = data.get("minimum_version")
        if data["operations_enforced"] and data.get("supported") is not True:
            if isinstance(minimum, str) and re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", minimum):
                record(url, bearer, minimum)
            return status(url, bearer)
        if data.get("current_version") != ACTIVE_PLUGIN_VERSION:
            return retain()
        # Only recovery needs the extra protected read; startup status checks
        # do not enumerate user data when there was no rejection to clear.
        if old:
            result = mcp_http.call_tool(url, bearer, "list_orgs", {}, timeout=timeout)
            if result.isError:
                return retain()
            try:
                _path(url, bearer).unlink()
            except FileNotFoundError:
                pass
        return None
    except (mcp_http.McpError, OSError, ValueError):
        if old:
            record(url, bearer, old["minimum_version"])
        return status(url, bearer)
    finally:
        _checking.reset(token)


def before_operation(url, bearer):
    if _checking.get() or urlsplit(url).path == "/v1/plugin/compatibility":
        return
    blocked = status(url, bearer)
    if not blocked:
        return
    try:
        age = time.time() - float(blocked.get("checked_at", 0))
    except (TypeError, ValueError):
        age = RECHECK_SECONDS
    if age >= RECHECK_SECONDS or blocked.get("active_version") != ACTIVE_PLUGIN_VERSION:
        blocked = check(url, bearer)
    if blocked:
        from mcp_http import PluginUpgradeRequired
        raise PluginUpgradeRequired(blocked["minimum_version"])


def startup_message(host=None, session=None):
    from _memhub_auth import resolve_bearer
    try:
        url, bearer = resolve_bearer(refresh=False)
        if not bearer:
            return None
        blocked = status(url, bearer)
        marker = None
        healthy_signature = json.dumps(["", ACTIVE_PLUGIN_VERSION])
        if session:
            marker = _path(url, bearer).with_suffix("." + hashlib.sha256(str(session).encode()).hexdigest()[:16])
            try:
                if not blocked and marker.read_text(encoding="utf-8") == healthy_signature:
                    return None
            except OSError:
                pass
        recent = False
        if blocked:
            try:
                recent = (time.time() - float(blocked.get("checked_at", 0)) < RECHECK_SECONDS
                          and blocked.get("active_version") == ACTIVE_PLUGIN_VERSION)
            except (ValueError, TypeError):
                pass
        if not recent:
            blocked = check(url, bearer)
        if not blocked:
            if marker:
                try:
                    atomic_write.publish(marker, healthy_signature)
                except OSError:
                    pass
            from plugin_updates import available_message
            return available_message(host)
        if marker:
            signature = json.dumps([blocked["minimum_version"], ACTIVE_PLUGIN_VERSION])
            try:
                if marker.read_text(encoding="utf-8") == signature:
                    return None
            except OSError:
                pass
            try:
                atomic_write.publish(marker, signature)
            except OSError:
                pass
        return upgrade_message(blocked["minimum_version"], host=host)
    except Exception:
        # Compatibility failure cannot block unrelated coding-agent work.
        return None
