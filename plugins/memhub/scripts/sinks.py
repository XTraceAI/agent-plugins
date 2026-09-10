"""Explicit capture destinations, independent of cloud-service authentication.

This module only reads configuration. Callers must opt in; importing it does
not change _memhub_auth defaults or install/update any account configuration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import _memhub_auth
import mcp_http

CONFIG_PATH = Path.home() / ".config/memhub-plugin/config.json"
DEFAULT_MCP_PATH = "/mcp-server/mcp"
_MAX_CONFIG_BYTES = 64 * 1024


class SinkConfigError(ValueError):
    """Invalid explicit destination or selection; messages contain no secrets."""


def _name(value) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 64
            or not value.isascii()
            or any(not (char.isalnum() or char in "_-") for char in value)):
        raise SinkConfigError("capture sink names must use letters, digits, '_' or '-'")
    return value


def _endpoint(value) -> str:
    try:
        if (not isinstance(value, str) or not value or "\\" in value
                or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
            raise ValueError()
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.fragment or parts.port == 0):
            raise ValueError()
        mcp_http.require_secure(value)
    except (ValueError, mcp_http.McpError):
        raise SinkConfigError("capture endpoint requires HTTPS or literal loopback HTTP") from None
    return value


def _token(value) -> str | None:
    if value is None:
        return None
    if (not isinstance(value, str) or not value or not value.isascii()
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise SinkConfigError("capture credential is not a valid bearer value")
    return value


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlsplit(_endpoint(url))
    return parts.scheme, parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


@dataclass(frozen=True)
class Sink:
    name: str
    url: str = field(repr=False)  # Complete MCP endpoint, like resolve_bearer().
    token: str | None = field(default=None, repr=False)

    def __post_init__(self):
        _name(self.name)
        _endpoint(self.url)
        _token(self.token)

    @property
    def mcp_path(self) -> str:
        parts = urlsplit(self.url)
        return parts.path + ("?" + parts.query if parts.query else "")

    @property
    def is_local(self) -> bool:
        return urlsplit(self.url).hostname in {"localhost", "127.0.0.1", "::1"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result


def load_config(path: Path | None = None) -> dict | None:
    """Read version-one configuration; missing/corrupt files use legacy defaults.

    A structurally valid file with an unsafe endpoint or ambiguous destination
    is an explicit configuration error, not permission to contact a fallback.
    """
    try:
        with (path or CONFIG_PATH).open("rb") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            return None
        config = json.loads(raw, object_pairs_hook=_unique_object)
    except (OSError, ValueError, RecursionError):
        return None
    if isinstance(config, dict) and type(config.get("version")) is int and config["version"] != 1:
        raise SinkConfigError("unsupported capture configuration version")
    if (not isinstance(config, dict) or type(config.get("version")) is not int
            or not isinstance(config.get("sinks"), list)
            or not isinstance(config.get("active"), list)):
        return None
    registry = {}
    for item in config["sinks"]:
        if not isinstance(item, dict):
            raise SinkConfigError("capture sink entries must be objects")
        name = _name(item.get("name"))
        if name in registry:
            raise SinkConfigError("capture sink names must be unique")
        base = _endpoint(item.get("url"))
        if urlsplit(base).query:
            raise SinkConfigError("capture base URL cannot contain a query")
        path = item.get("mcp_path", DEFAULT_MCP_PATH)
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise SinkConfigError("capture MCP path must start with a single slash")
        registry[name] = Sink(name, base.rstrip("/") + path, item.get("token"))
    active = tuple(dict.fromkeys(_name(name) for name in config["active"]))
    return {"sinks": registry, "active": active}


def _selection() -> tuple[tuple[str, ...], dict[str, Sink]]:
    # An explicit URL bypasses even an invalid config or named selection.
    if os.environ.get("MEMHUB_MCP_BASE_URL"):
        sink = Sink("env", _memhub_auth.default_url())
        return (sink.name,), {sink.name: sink}
    config = load_config()
    registry = config["sinks"] if config is not None else {}
    if "MEMHUB_SINKS" in os.environ:
        raw = os.environ["MEMHUB_SINKS"].strip()
        names = tuple(dict.fromkeys(_name(name.strip()) for name in raw.split(","))) if raw else ()
    else:
        names = config["active"] if config is not None else ("cloud",)
    for name in names:
        if name == "cloud" and name not in registry:
            registry[name] = Sink("cloud", _memhub_auth.default_url())
        if name not in registry:
            raise SinkConfigError("unknown active capture sink name")
    return names, registry


def active_sink_names() -> tuple[str, ...]:
    return _selection()[0]


def resolve_capture_sink() -> Sink | None:
    """Select one destination, or None for an explicitly empty active list.

    Multiple active destinations are rejected until a caller implements
    independent delivery; silently choosing the first would lose capture.
    """
    names, registry = _selection()
    if len(names) > 1:
        raise SinkConfigError("multiple active capture sinks require independent delivery")
    return registry[names[0]] if names else None


def resolve_capture_auth(sink: Sink, *, refresh: bool = True) -> tuple[str, str | None]:
    """Resolve credentials for this selected URL without changing service defaults."""
    _endpoint(sink.url)
    explicit = os.environ.get("MEMHUB_TOKEN", "").strip()
    if explicit:
        return sink.url, _token(explicit)
    if sink.token is not None:
        return sink.url, _token(sink.token)
    # OAuth refresh metadata belongs to the installed plugin's backend. An
    # explicit sink can use its own stored PAK/current token, but must not send
    # its refresh token to the installed backend's different authorization server.
    try:
        same_backend = _origin(_memhub_auth._plugin_mcp_config()["url"]) == _origin(sink.url)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
        same_backend = False
    return _memhub_auth.resolve_bearer(sink.url, refresh=refresh and same_backend)
