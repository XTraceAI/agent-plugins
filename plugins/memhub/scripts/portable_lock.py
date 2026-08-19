"""File locking that works on POSIX and native Windows.

The capture scripts guard their state files with ``fcntl.flock`` — a module
that does not exist on Windows, so ``import fcntl`` killed every one of them
at import time on a native-Windows host (found live: the plugin's capture
never worked there). This shim is the one place that knows both platforms;
callers keep flock semantics.

POSIX: ``fcntl.flock`` exactly as before. Windows: ``msvcrt.locking`` on one
byte at offset 0 — advisory like flock, released on unlock or when the fd
closes. One honest semantic difference: a BLOCKING acquire on Windows
(``LK_LOCK``) retries for ~10 seconds and then raises, where flock waits
forever. Every blocking use in this codebase guards a sub-second critical
section, so ten seconds of patience is indistinguishable from forever there —
documented so nobody builds a long-held blocking lock on top of this without
reading it.
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
    import msvcrt as _msvcrt

    def _at_start(fd: int) -> None:
        # msvcrt locks a byte RANGE from the current position; pin it so
        # lock and unlock always name the same byte.
        os.lseek(fd, 0, os.SEEK_SET)

    def lock_exclusive(fd: int, blocking: bool = True) -> None:
        """Take an exclusive lock. Non-blocking raises OSError when held.

        Blocking mode retries ~10s (msvcrt LK_LOCK), then raises — see the
        module docstring for why that is acceptable here."""
        _at_start(fd)
        _msvcrt.locking(fd, _msvcrt.LK_LOCK if blocking else _msvcrt.LK_NBLCK, 1)

    def unlock(fd: int) -> None:
        _at_start(fd)
        _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)


def fileno_of(f) -> int:
    """Accept a raw fd or anything with .fileno() (room_map passes a handle)."""
    return f if isinstance(f, int) else f.fileno()
