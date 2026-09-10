"""One hook invocation's capture destination; foreground services stay separate.

Context variables carry selection across existing async helpers and worker
threads without mutating module state or environment variables. Direct helper
calls retain their legacy defaults; executable hook entrypoints opt in.
"""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
import hashlib
from pathlib import Path

import _memhub_auth
import brain_resolve
import room_map
from sinks import Sink, SinkConfigError, resolve_capture_auth, resolve_capture_sink

_current: ContextVar[Sink | None] = ContextVar("capture_destination", default=None)
_legacy: ContextVar[bool] = ContextVar("capture_legacy_state", default=True)
_budget: ContextVar[float | None] = ContextVar("capture_time_budget", default=None)
_room_env: ContextVar[str | None] = ContextVar("capture_room_env", default=None)
_payload: ContextVar[dict | None] = ContextVar("capture_hook_payload", default=None)


def entrypoint(function=None, *, observe_when_unselected=False):
    if function is None:
        return lambda target: entrypoint(target, observe_when_unselected=observe_when_unselected)

    def unselected(*args, **kwargs):
        return function(*args, **kwargs, observations_only=True) if observe_when_unselected else 0

    @wraps(function)
    def selected(*args, **kwargs):
        try:
            sink = resolve_capture_sink()
            legacy = sink is not None and sink.name == "cloud" and sink.url == _memhub_auth.default_url()
        except SinkConfigError as error:
            print(f"[memhub-capture] {error}; capture deferred")
            return unselected(*args, **kwargs)
        except Exception:
            print("[memhub-capture] destination unavailable; capture deferred")
            return unselected(*args, **kwargs)
        if sink is None:
            return unselected(*args, **kwargs)
        with bind(sink, legacy=legacy):
            return function(*args, **kwargs)
    return selected


@contextmanager
def bind(sink: Sink, *, legacy: bool | None = None, budget: float | None = None):
    if legacy is None:
        legacy = sink.name == "cloud" and sink.url == _memhub_auth.default_url()
    token = _current.set(sink)
    legacy_token = _legacy.set(legacy)
    room_token = _room_env.set(_capture_room_env(sink))
    budget_token = _budget.set(budget)
    payload_token = _payload.set(None)
    try:
        yield
    finally:
        _payload.reset(payload_token)
        _budget.reset(budget_token)
        _room_env.reset(room_token)
        _legacy.reset(legacy_token)
        _current.reset(token)


def time_budget():
    return _budget.get()


def session(url, bearer, timeout):
    import capture_async
    import mcp_http
    factory = capture_async.Session if _current.get() is not None else mcp_http.Session
    return factory(url, bearer, timeout=timeout)


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


def identity(native_id: str, metadata=None, *, include_cloud=False) -> dict:
    """Add local identity without changing older cloud import envelopes."""
    sink = _current.get()
    if sink is None or (not sink.is_local and not include_cloud):
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


async def import_conversation(session, arguments):
    """Retry explicit old-cloud argument rejection once with its legacy envelope."""
    import mcp_http
    result = await session.call_tool("import_conversation", arguments=arguments)
    sink = _current.get()
    if sink is None or sink.is_local or not getattr(result, "isError", False):
        return result
    text = " ".join(mcp_http.texts_of(result)).lower()
    fields = {"native_session_id", "source_surface"}.intersection(arguments)
    if fields and any(field in text for field in fields) and any(marker in text for marker in (
            "unexpected keyword argument", "extra inputs are not permitted",
            "extra inputs not permitted", "unknown argument", "additional properties")):
        legacy = {key: value for key, value in arguments.items() if key not in fields}
        return await session.call_tool("import_conversation", arguments=legacy)
    return result


def acknowledges(out, conversation_id, records, *, require_durable=True):
    """Validate an echoed batch, including explicit stored-or-dropped accounting."""
    if out.get("conversation_id") != conversation_id:
        return False
    if "ack_through" not in out:
        return not require_durable  # Only a legacy whole-session backstop allows this.
    ids = [record.get("uuid") for record in records
           if isinstance(record, dict) and record.get("uuid")]
    ack = out["ack_through"]
    if ids and len(ids) == len(records) and ack == ids[-1]:
        return True
    dropped = out.get("records_dropped")
    received = out.get("messages_received")
    if (type(dropped) is not int or not 0 < dropped <= len(records)
            or type(received) is not int or received != len(records)):
        return False
    if ack is None:
        return dropped == len(records)
    prefix = next((index + 1 for index, identity in enumerate(ids)
                   if identity == ack), None)
    return prefix is not None and prefix + dropped == len(records)
