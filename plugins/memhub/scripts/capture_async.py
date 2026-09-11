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


class Session:
    def __init__(self, url, bearer, timeout):
        self.url, self.bearer, self.timeout = url, bearer, timeout

    async def call_tool(self, name, arguments=None, timeout=None):
        return await blocking(mcp_http.call_tool, self.url, self.bearer, name,
                              arguments or {}, self.timeout if timeout is None else timeout)
