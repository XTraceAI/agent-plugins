"""Filesystem enumeration with explicit read failures for discovery consumers."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path


def paths(root: Path, pattern: tuple[str, ...], on_error) -> list[Path]:
    """Walk only the selected layout, reporting inaccessible or skipped trees.

    A leading ** supports dated Codex directories. Symlinked subdirectories are
    reported as incomplete instead of following cycles or claiming empty data.
    The root itself may be a configured symlink.
    """
    found = []
    recursive = pattern[0] == "**"
    try:
        if not root.is_dir():
            raise FileNotFoundError(str(root))
        for parent, directories, files in os.walk(root, onerror=on_error):
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
                found.extend(parent / name for name in files
                             if fnmatch.fnmatchcase(name, pattern[-1]))
    except OSError as error:
        on_error(error)
    return found
