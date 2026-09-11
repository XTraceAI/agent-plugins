"""Bounded background capture can exit without joining blocked network threads.

The worker owns blocking transport, authentication and preparation; cursor writes remain on
the awaiting event loop. Cancelling an await never schedules a later cursor
advance. Daemon workers cannot keep the short-lived hook process alive.
"""
from __future__ import annotations

import asyncio
import contextvars
import threading

import mcp_http


async def blocking(function, *args):
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    context = contextvars.copy_context()

    def complete(value, error):
        if not result.done():
            if error is None:
                result.set_result(value)
            else:
                result.set_exception(error)

    def worker():
        try:
            value, error = context.run(function, *args), None
        except BaseException as caught:
            value, error = None, caught
        try:
            loop.call_soon_threadsafe(complete, value, error)
        except RuntimeError:
            pass  # The hook finished; a late transport result cannot commit state.

    threading.Thread(target=worker, daemon=True).start()
    return await result


async def resource(function, *, close):
    """Acquire in the existing worker, disposing any unclaimed late result."""
    guard = threading.Lock()
    pending = []
    abandoned = False

    def acquire():
        value = function()
        with guard:
            if not abandoned:
                pending.append(value)
                return value
        close(value)
        return None

    try:
        value = await blocking(acquire)
        with guard:
            pending.clear()  # Ownership transfers to this awaiting caller.
        return value
    finally:
        with guard:
            abandoned = True
            unclaimed = list(pending)
            pending.clear()
        for value in unclaimed:
            close(value)


class Session:
    def __init__(self, url, bearer, timeout):
        self.url, self.bearer, self.timeout = url, bearer, timeout

    async def call_tool(self, name, arguments=None, timeout=None):
        return await blocking(mcp_http.call_tool, self.url, self.bearer, name,
                              arguments or {}, self.timeout if timeout is None else timeout)


async def publish_state(path, state):
    """Stage JSON off-loop; only the awaiting lock owner publishes progress.

    A cancelled worker may finish its private staging file, then removes it.
    It never replaces the destination. Unique staging names also keep a late
    worker separate from another attempt in this process.
    """
    import json
    import os
    import time
    import uuid
    import atomic_write

    staged = path.with_name(f"{path.name}.{uuid.uuid4().hex[:16]}.tmp")
    cancelled = threading.Event()

    def discard():
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass

    def prepare():
        try:
            atomic_write._sweep_stale_tmps(path)
            atomic_write.publish(staged, json.dumps(state))
        finally:
            if cancelled.is_set():
                discard()

    try:
        await blocking(prepare)
        # One atomic syscall on the owner. Windows sharing retries yield to
        # the caller's timeout instead of blocking in atomic_write.replace.
        deadline = time.monotonic() + 10.0
        while True:
            try:
                staged.replace(path)
                break
            except OSError as error:
                if (os.name != "nt" or getattr(error, "winerror", None) not in (5, 32, 33)
                        or time.monotonic() >= deadline):
                    raise
                await asyncio.sleep(0.004)
    finally:
        cancelled.set()
        discard()
