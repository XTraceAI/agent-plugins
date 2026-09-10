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
import time
from pathlib import Path

import _memhub_auth
import brain_resolve
import room_map
from sinks import Sink, SinkConfigError, resolve_capture_auth, resolve_capture_sink
from sinks import resolve_capture_sinks

_current: ContextVar[Sink | None] = ContextVar("capture_destination", default=None)
_legacy: ContextVar[bool] = ContextVar("capture_legacy_state", default=True)
_budget: ContextVar[float | None] = ContextVar("capture_time_budget", default=None)
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
        with bind(sink, legacy=legacy):
            return function(*args, **kwargs)
    return selected


@contextmanager
def bind(sink: Sink, *, legacy: bool | None = None, budget: float | None = None,
         room_env: str | None = None):
    if legacy is None:
        legacy = sink.name == "cloud" and sink.url == _memhub_auth.default_url()
    token = _current.set(sink)
    legacy_token = _legacy.set(legacy)
    room_token = _room_env.set(room_env or _capture_room_env(sink))
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


def is_legacy():
    """Whether this call owns the unchanged installed cloud destination."""
    return _legacy.get()


def time_budget():
    return _budget.get()


def deliver(payload, run_sink, timeout):
    """Freeze active destinations and reserve each a share of one deadline."""
    selected = resolve_capture_sinks()
    if not isinstance(payload, dict) or not selected:
        return
    try:
        installed_url = _memhub_auth.default_url()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, AttributeError):
        installed_url = None
    destinations = [(sink, sink.name == "cloud" and sink.url == installed_url,
                     _capture_room_env(sink))
                    for sink in sorted(selected, key=lambda sink: not sink.is_local)]
    deadline = time.monotonic() + timeout
    for index, (sink, legacy, room_env) in enumerate(destinations):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        with bind(sink, legacy=legacy, room_env=room_env,
                  budget=remaining / (len(destinations) - index)):
            run_sink(payload)


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


async def import_conversation(session, arguments, *, timeout=None):
    """Retry explicit old-cloud argument rejection once with its legacy envelope."""
    import mcp_http
    deadline = None if timeout is None else time.monotonic() + timeout
    kwargs = {} if timeout is None else {"timeout": timeout}
    result = await session.call_tool("import_conversation", arguments=arguments, **kwargs)
    sink = _current.get()
    if sink is None or sink.is_local or not getattr(result, "isError", False):
        return result
    text = " ".join(mcp_http.texts_of(result)).lower()
    fields = {"native_session_id", "source_surface"}.intersection(arguments)
    if fields and any(field in text for field in fields) and any(marker in text for marker in (
            "unexpected keyword argument", "extra inputs are not permitted",
            "extra inputs not permitted", "unknown argument", "additional properties")):
        legacy = {key: value for key, value in arguments.items() if key not in fields}
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return result
            kwargs["timeout"] = remaining
        return await session.call_tool("import_conversation", arguments=legacy, **kwargs)
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
    if ids and ack == ids[-1]:
        return True
    dropped = out.get("records_dropped")
    received = out.get("messages_received")
    if (type(dropped) is not int or not 0 < dropped <= len(records)
            or type(received) is not int or received != len(records)):
        return False
    if ack is None:
        return dropped == len(records)
    prefix = next((index + 1 for index, record in enumerate(records)
                   if isinstance(record, dict) and record.get("uuid") == ack), None)
    return prefix is not None and prefix + dropped == len(records)
