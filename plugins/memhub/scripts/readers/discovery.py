"""Filesystem enumeration with explicit read failures for discovery consumers."""
from __future__ import annotations

import fnmatch
import os
import stat
from pathlib import Path


def _entries(parent: Path, observe):
    """One directory's names, charged one at a time.

    ``os.walk`` materializes a directory's whole listing before it yields
    anything, so a caller that bounds work per entry has already paid for
    every name in the widest directory before it is consulted once. This
    collects the same two lists from ``scandir``, but calls ``observe``
    before each name is kept, so the bound is reached on the entry that
    exceeds it rather than after the directory has been read out.

    Classification matches ``os.walk``: an entry is a directory when
    ``is_dir()`` says so, which follows a symlink, and an entry whose kind
    cannot be determined is treated as a file. Both are what the caller then
    inspects with ``lstat`` and ``is_symlink``.
    """
    directories, files = [], []
    with os.scandir(parent) as entries:
        for entry in entries:
            if observe is not None:
                observe()
            try:
                is_directory = entry.is_dir()
            except OSError:
                is_directory = False
            (directories if is_directory else files).append(entry.name)
    return directories, files


def _bounded_walk(root: Path, on_error, observe):
    """``os.walk(top_down=True)``'s traversal, one directory entry at a time.

    Yields the same ``(parent, directories, files)`` triples in the same
    order, honours the caller pruning ``directories`` in place, and reports
    an unreadable directory through ``on_error`` and continues, exactly as
    ``os.walk``'s ``onerror`` does.
    """
    pending = [root]
    while pending:
        parent = pending.pop()
        try:
            directories, files = _entries(parent, observe)
        except OSError as error:
            on_error(error)
            continue
        yield parent, directories, files
        # The caller may have pruned ``directories`` in place; descend into
        # what is left, first entry first.
        pending.extend(parent / name for name in reversed(directories))


def paths(root: Path, pattern: tuple[str, ...], on_error, observe=None) -> list[Path]:
    """Walk only the selected layout, reporting inaccessible or skipped trees.

    A leading ** supports dated Codex directories. Symlinked subdirectories are
    reported as incomplete instead of following cycles or claiming empty data.
    The root itself may be a configured symlink.

    ``observe`` is called once for every directory entry this walk considers,
    before that entry is collected. A caller that must stay inside a budget
    enforces it there, during enumeration rather than after it: an unbounded
    tree is stopped by whatever ``observe`` raises, which travels out of the
    walk rather than being reported as an inaccessible path. Supplying it also
    selects the incremental traversal, because ``os.walk`` reads a whole
    directory before anything can be charged for it.
    """
    found = []
    recursive = pattern[0] == "**"
    try:
        if not root.is_dir():
            raise FileNotFoundError(str(root))
        walk = (os.walk(root, onerror=on_error) if observe is None
                else _bounded_walk(root, on_error, observe))
        for parent, directories, files in walk:
            parent = Path(parent)
            depth = len(parent.relative_to(root).parts)
            keep = []
            for name in directories:
                child = parent / name
                selected = recursive or (depth < len(pattern) - 1 and
                                          fnmatch.fnmatchcase(name, pattern[depth]))
                if not selected:
                    continue
                if child.is_symlink():
                    on_error(OSError("symlinked discovery directory", str(child)))
                else:
                    keep.append(name)
            directories[:] = keep
            if recursive or depth == len(pattern) - 1:
                for name in files:
                    if not fnmatch.fnmatchcase(name, pattern[-1]):
                        continue
                    child = parent / name
                    try:
                        mode = child.lstat().st_mode
                    except OSError as error:
                        on_error(error)
                        continue
                    if stat.S_ISLNK(mode):
                        on_error(OSError("symlinked discovery file", str(child)))
                    elif not stat.S_ISREG(mode):
                        on_error(OSError("non-regular discovery file", str(child)))
                    else:
                        found.append(child)
    except OSError as error:
        on_error(error)
    return found
