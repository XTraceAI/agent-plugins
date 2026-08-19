"""File locking that works on POSIX and native Windows.

The capture scripts guard their state files with ``fcntl.flock`` — a module
that does not exist on Windows, so ``import fcntl`` killed every one of them
at import time on a native-Windows host (found live: the plugin's capture
never worked there). This shim is the one place that knows both platforms;
callers keep flock semantics.

POSIX: ``fcntl.flock`` exactly as before. Windows: ``msvcrt.locking`` on one
byte at offset 0 — advisory like flock, released on unlock or when the fd
closes. Blocking semantics are made to MATCH flock: ``LK_LOCK`` only retries
for ~10 seconds and then raises, so a blocking acquire loops it until the
lock is granted — under real contention (a fleet of parallel agents, the
reason these locks exist) ten seconds is reachable, and a serialization
primitive that crashes its caller under load is worse than one that waits.
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

        Blocking mode waits like flock does — but only on CONTENTION.
        LK_LOCK gives up after ~10s of internal 1/s retries and raises
        EDEADLK (or EACCES); those two errnos mean "still held", so loop
        them until granted. Any other errno (EBADF, EINVAL — a broken fd,
        not a held lock) re-raises immediately: retrying a permanent error
        would spin the caller forever, not serialize it. The tiny sleep is
        belt-and-braces against a contention raise that returns fast."""
        _at_start(fd)
        if not blocking:
            _msvcrt.locking(fd, _msvcrt.LK_NBLCK, 1)
            return
        while True:
            try:
                _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
                return
            except OSError as e:
                if e.errno not in (_errno.EACCES, _errno.EDEADLK):
                    raise
                _time.sleep(0.01)

    def unlock(fd: int) -> None:
        _at_start(fd)
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)


def fileno_of(f) -> int:
    """Accept a raw fd or anything with .fileno() (room_map passes a handle)."""
    return f if isinstance(f, int) else f.fileno()
