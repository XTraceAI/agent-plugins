"""File locking that works on POSIX and native Windows.

The capture scripts guard their state files with ``fcntl.flock`` — a module
that does not exist on Windows, so ``import fcntl`` killed every one of them
at import time on a native-Windows host (found live: the plugin's capture
never worked there). This shim is the one place that knows both platforms;
callers keep flock semantics.

POSIX: ``fcntl.flock`` exactly as before. Windows: ``msvcrt.locking`` on one
byte at offset 0 — advisory like flock, released on unlock or when the fd
closes. Blocking semantics approximate flock under a 60-second budget:
``LK_LOCK`` only retries for ~10 seconds and then raises, so a blocking
acquire loops it — under real contention (a fleet of parallel agents, the
reason these locks exist) ten seconds is reachable, and a serialization
primitive that crashes its caller under load is worse than one that waits.
The budget is the other side of that coin: a lock still refused after a
full minute is wedged or un-grantable, and raising then beats hanging the
calling hook forever. See ``lock_exclusive`` for the errno classification.
"""
from __future__ import annotations

import os

try:  # POSIX
    import fcntl as _fcntl

    def lock_exclusive(fd: int, blocking: bool = True) -> None:
        """Take an exclusive lock. Non-blocking raises OSError when held."""
        flags = _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB)
        _fcntl.flock(fd, flags)

    def unlock(fd: int) -> None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)

except ImportError:  # native Windows
    import errno as _errno
    import time as _time

    import msvcrt as _msvcrt

    def _at_start(fd: int) -> None:
        # msvcrt locks a byte RANGE from the current position; pin it so
        # lock and unlock always name the same byte.
        os.lseek(fd, 0, os.SEEK_SET)

    def lock_exclusive(fd: int, blocking: bool = True) -> None:
        """Take an exclusive lock. Non-blocking raises OSError when held.

        Blocking mode waits out contention like flock — under a 60s wall
        budget. Three regimes, one per failure class:
        * EBADF/EINVAL raise immediately: the CALL is broken (bad fd or
          arguments) and no retry can ever succeed.
        * Any other errno is treated as contention, because the CRT's
          mapping for a held lock (EACCES/EDEADLK today) is not a contract
          across CRT versions — crashing a caller under genuine contention
          would be worse than waiting.
        * The deadline converts "waits like flock" into "waits, bounded":
          measured fleet contention resolves in well under a second and
          LK_LOCK itself sleeps ~10s per attempt, so a full minute of
          refusal means a wedged holder or an un-grantable lock (ACLs) —
          raising then beats hanging a capture hook (or pegging a core at
          the 10ms poll) forever."""
        _at_start(fd)
        if not blocking:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            return
        deadline = _time.monotonic() + 60.0
        while True:
            try:
                _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
                return
            except OSError as e:
                if e.errno in (_errno.EBADF, _errno.EINVAL):
                    raise
                if _time.monotonic() >= deadline:
                    raise
                _time.sleep(0.01)

    def unlock(fd: int) -> None:
        _at_start(fd)
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)


def fileno_of(f) -> int:
    """Accept a raw fd or anything with .fileno() (room_map passes a handle)."""
    return f if isinstance(f, int) else f.fileno()
