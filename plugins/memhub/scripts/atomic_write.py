#!/usr/bin/env python3
"""Publish a file so no reader ever sees it half-written (stdlib only).

**Why this is one function and not three copies.** Every durable thing this
plugin keeps — the OAuth token cache, the access key, the per-session capture
cursor — is written by more than one process: the per-turn flush, the SessionEnd
backstop (which does not take the per-turn flock), and the PreToolUse directive
check that fires on every file edit. All three needed the same care, so all
three grew their own copy of it, and the copies drifted: a review round found
the same non-atomic write in one file, and the same bug was sitting in the other
two under slightly different spellings.

The failure this prevents is silent in the worst way. A reader that catches a
truncated token cache does not raise — it concludes there is no usable
credential and skips, which is indistinguishable from "not logged in". A
truncated cursor file is a corrupt cursor.

Two properties, and both matter:

* **the temp name is per process.** A shared ``<name>.tmp`` is not atomic under
  concurrency at all: two writers open it with ``O_TRUNC``, interleave, and one
  renames a spliced document — or fails outright because the other's rename
  removed the file underneath it. Renaming a private temp means each writer
  publishes something it wrote whole, and losing the race just means being
  overwritten by an equally valid document.
* **the mode is set at creation**, not chmod'd afterwards. Writing first leaves
  a secret at the process umask — 0644 on a default setup — for a window that
  costs nothing to close.

Run the self-test:  python3 tests/concurrent_writes_test.py
"""
from __future__ import annotations

import os
from pathlib import Path


def publish(path: Path, text: str, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` atomically, creating it with ``mode``.

    Raises whatever the filesystem raises, after cleaning up its temp file —
    callers differ on whether a failed write should be fatal, so that decision
    stays with them. What does not vary, and so lives here, is that a failure
    must not leave debris in the user's config directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
        # os.open applies `mode` only when it CREATES the file. A leftover temp
        # — from a crashed process whose pid has since been reused — keeps
        # whatever mode it already had, so a secret could be published at 0644
        # while this code looked like it set 0600. fchmod on the open
        # descriptor settles it either way, and on the fd rather than the path
        # so there is no window for a swap in between.
        # Native Windows has no fchmod (POSIX modes don't map to ACLs; files
        # under the user profile are private by default there) — chmod on the
        # path is the closest gesture and the leftover-temp race it reopens
        # does not exist on Windows, where O_TRUNC already emptied the temp.
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)
        else:
            os.chmod(tmp, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp.replace(path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
