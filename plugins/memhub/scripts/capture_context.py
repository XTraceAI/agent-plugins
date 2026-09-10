"""One hook invocation's capture destination; foreground services stay separate.

Context variables carry selection across existing async helpers and worker
threads without mutating module state or environment variables. Direct helper
calls retain their legacy defaults; executable hook entrypoints opt in.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import hashlib
from pathlib import Path

import _memhub_auth
import brain_resolve
import room_map
from sinks import Sink, SinkConfigError, resolve_capture_auth, resolve_capture_sink

_current: ContextVar[Sink | None] = ContextVar("capture_destination", default=None)
_legacy: ContextVar[bool] = ContextVar("capture_legacy_state", default=True)
_room_env: ContextVar[str | None] = ContextVar("capture_room_env", default=None)
_payload: ContextVar[dict | None] = ContextVar("capture_hook_payload", default=None)


def entrypoint(function):
    @wraps(function)
    def selected(*args, **kwargs):
        try:
            sink = resolve_capture_sink()
            legacy = sink is not None and sink.name == "cloud" and sink.url == _memhub_auth.default_url()
        except SinkConfigError as error:
            print(f"[memhub-capture] {error}; capture deferred")
            return 0
        except Exception:
            print("[memhub-capture] destination unavailable; capture deferred")
            return 0
        if sink is None:
            return 0
        token = _current.set(sink)
        legacy_token = _legacy.set(legacy)
        room_token = _room_env.set(_capture_room_env(sink))
        payload_token = _payload.set(None)
        try:
            return function(*args, **kwargs)
        finally:
            _payload.reset(payload_token)
            _room_env.reset(room_token)
            _legacy.reset(legacy_token)
            _current.reset(token)
    return selected


def observe(payload: dict) -> None:
    _payload.set(payload if isinstance(payload, dict) else {})


def state_directory(root: Path, sink: Sink | None = None) -> Path:
    legacy = (sink.name == "cloud" and sink.url == _memhub_auth.default_url()) if sink else _legacy.get()
    sink = sink or _current.get()
    if sink is None or legacy:
        return root  # Only the unchanged installed cloud owns the old cursor.
    endpoint = hashlib.sha256(sink.url.encode("utf-8")).hexdigest()[:24]
    return root / sink.name / endpoint


def valid_session_id(value) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 256 and value not in {".", ".."}
            and all(character.isascii() and (character.isalnum() or character in "._-")
                    for character in value))


def resolve_bearer():
    sink = _current.get()
    return resolve_capture_auth(sink) if sink is not None else _memhub_auth.resolve_bearer()


def _capture_room_env(sink: Sink) -> str:
    if sink.is_local:
        return "local"
    try:
        if sink.url == _memhub_auth._plugin_mcp_config()["url"]:
            return room_map.env_for_url(sink.url)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
        pass
    return "capture-" + hashlib.sha256(sink.url.encode("utf-8")).hexdigest()


def env_for_url(url: str) -> str:
    sink = _current.get()
    return (_room_env.get() or _capture_room_env(sink)) if sink else room_map.env_for_url(url)


async def resolve_repo_brain(session, cwd, env):
    if env == "local":
        return None
    return await brain_resolve.resolve_repo_brain(session, cwd, env)


def identity(native_id: str, metadata=None) -> dict:
    """Add local identity without changing older cloud import envelopes."""
    sink = _current.get()
    if sink is None or not sink.is_local:
        return {}
    result = {"native_session_id": native_id}
    payload = _payload.get() or {}
    try:
        metadata = metadata() if callable(metadata) else (metadata or {})
    except (OSError, ValueError, TypeError, OverflowError):
        metadata = {}
    for source in (payload.get("source_surface"), payload.get("entrypoint"),
                   metadata.get("source_surface")):
        if isinstance(source, str) and source.strip():
            result["source_surface"] = source
            break
    return result
