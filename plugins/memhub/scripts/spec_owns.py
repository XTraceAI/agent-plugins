"""The ``owns:`` frontmatter convention — one parser, every consumer (ENG-1097).

A spec is a markdown file under ``docs/specs/`` (or the repo's configured ``spec_dir``)
that opens with YAML frontmatter naming the code paths it documents::

    ---
    spec: billing_spec
    owns:
      - app/services/cap_service.py
      - app/api/v1/endpoints/webhooks/
    last_verified_at: <commit-sha-or-null>
    ---

``docs/SPEC_DRIFT.md`` describes the discipline; ``docs/specs/spec-driven-repos.md`` makes
the convention the product contract. This module is the ONLY place the frontmatter is
parsed and owns-matched server-side: the local CI script (``.github/scripts/
check_spec_drift.py``), the ``spec_drift`` review lens (``spec_retrieval.repo_specs``) and
the ``spec_audit`` routine all read through it, so "which spec owns this file" has one
answer. The plugin's hook carries a PORT of the same rules (hooks run under system
python with no app deps) pinned by a shared parity fixture.

Pure: no DB, no LLM, no settings. Reads a directory or a tarball and returns
``SpecFile`` rows; matches repo-relative POSIX paths.
"""
from __future__ import annotations

import io
import posixpath
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple, Union

DEFAULT_SPEC_DIR = "docs/specs"
#: A spec file larger than this is skipped rather than read: the lens budgets prompt
#: chars per spec anyway, and an archive member this size is a bomb, not a document.
MAX_SPEC_BYTES = 512 * 1024
#: Upper bound on the ``spec_dir`` config value (schema-validated, re-checked here).
MAX_SPEC_DIR_LEN = 128
#: A spec under a directory with this name governs nothing. ``docs/specs/retired/`` is
#: the design record for code that was removed — "read them for why, never for what"
#: (its README) — and several still carry the ``owns:`` of the files they used to
#: describe. Excluding the folder by name is what lets a spec be retired by MOVING it,
#: without also editing its frontmatter.
RETIRED_DIR = "retired"

#: The block opens the file and closes on the first line that is exactly ``---``. Line
#: endings are ``\n`` or ``\r\n`` (a spec committed from Windows is still a spec), and
#: the closing fence may be the last line of the file with no newline after it.
FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)
LIST_ITEM_RE = re.compile(r"^\s*-\s*(.+?)\s*$")
#: A ``# comment`` after a value — a ``#`` preceded by whitespace, as YAML defines it, so
#: ``app/x#y.py`` keeps its ``#``.
_TRAILING_COMMENT_RE = re.compile(r"\s+#.*$")


@dataclass
class SpecFile:
    """One spec as the convention sees it."""

    path: str                         # repo-relative POSIX path of the spec file itself
    name: str                         # ``spec:`` key, else the file stem
    owns: List[str] = field(default_factory=list)   # normalized repo-relative paths
    last_verified_at: Optional[str] = None
    text: str = ""


def parse_frontmatter(text: str) -> Optional[dict]:
    """Minimal YAML frontmatter parser — handles the subset our specs use.

    Scalars (``key: value``), flat lists (``key:`` followed by ``- item`` lines, or the
    flow form ``key: [a, b]``), one layer of matching quotes, ``# comments``, CRLF and a
    BOM. No nesting, no escapes, no ``yaml.load`` — a spec file is repo content and a
    parser that executes tags is not something to point at it. Returns ``None`` when the
    file does not open with a frontmatter block.
    """
    m = FRONTMATTER_RE.match(text.lstrip("\ufeff"))   # a UTF-8 BOM is not content
    if not m:
        return None
    body = m.group(1)
    out: dict = {}
    current_key: Optional[str] = None
    for line in body.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = LIST_ITEM_RE.match(line)
        if item and current_key is not None:
            out.setdefault(current_key, []).append(_yaml_scalar(item.group(1)))
            continue
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            key = key.strip()
            val = _TRAILING_COMMENT_RE.sub("", val).strip()
            if not val:
                current_key = key
                out[key] = []
            elif val.startswith("[") and val.endswith("]"):
                # A flow list ``key: [a, b]`` — the other way YAML spells a flat list.
                items = [_yaml_scalar(x) for x in val[1:-1].split(",")]
                out[key] = [x for x in items if x]
                current_key = None
            else:
                out[key] = _yaml_scalar(val)
                current_key = None
    return out


def strip_frontmatter(text: str) -> str:
    """The document without its frontmatter block (and BOM); the text itself when it
    has none."""
    t = text.lstrip("\ufeff")
    m = FRONTMATTER_RE.match(t)
    return t[m.end():] if m else t


def _yaml_scalar(raw: str) -> str:
    """A plain scalar as YAML would read it: trailing comment dropped, one layer of
    matching quotes removed. Enough for a path or a name; no escapes, no multi-line."""
    v = _TRAILING_COMMENT_RE.sub("", raw).strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v


def safe_repo_relpath(raw: object) -> Optional[str]:
    """Normalize a repo-relative POSIX path, or ``None`` when it is not one.

    Refuses absolute paths, ``..`` segments, backslashes and empties. Strips a leading
    ``./`` and a trailing ``/`` (directory ownership is expressed by the matcher, not the
    slash). This is the ONE place an ``owns:`` entry or a ``spec_dir`` is judged safe;
    a tarball can carry any member name, and a spec's ``owns:`` list is repo content.
    """
    if not isinstance(raw, str):
        return None
    p = raw.strip()
    if not p or "\\" in p or p.startswith("/") or "\x00" in p:
        return None
    if len(p) > 1024:
        return None
    norm = posixpath.normpath(p)
    if norm in (".", "") or norm.startswith("../") or norm == ".." or norm.startswith("/"):
        return None
    return norm


def safe_spec_dir(raw: object) -> Optional[str]:
    """``spec_dir`` as the config accepts it: a safe relpath of bounded length."""
    if isinstance(raw, str) and len(raw) > MAX_SPEC_DIR_LEN:
        return None
    return safe_repo_relpath(raw)


def path_under(changed: Union[str, Path], owned: Union[str, Path]) -> bool:
    """True if ``changed`` IS ``owned`` or is nested under it (directory ownership)."""
    c = safe_repo_relpath(str(changed).replace("\\", "/") if isinstance(changed, Path) else changed)
    o = safe_repo_relpath(str(owned).replace("\\", "/") if isinstance(owned, Path) else owned)
    if c is None or o is None:
        return False
    return c == o or c.startswith(o + "/")


def spec_from_text(path: str, text: str) -> Optional[SpecFile]:
    """A ``SpecFile`` from a file's text, or ``None`` when it carries no frontmatter.

    A frontmatter block with neither ``spec:`` nor ``owns:`` is not a spec under the
    convention (a guide with ``title:`` frontmatter, say) and is skipped too. Unsafe
    ``owns:`` entries are dropped — never followed — so one bad line cannot make a spec
    claim a path outside the repo.
    """
    fm = parse_frontmatter(text)
    if not fm or ("spec" not in fm and "owns" not in fm):
        return None
    owns_raw = fm.get("owns")
    if isinstance(owns_raw, str):
        owns_raw = [owns_raw]         # ``owns: app/x.py`` — one path, not a list, still owned
    owns: List[str] = []
    if isinstance(owns_raw, list):
        for entry in owns_raw:
            norm = safe_repo_relpath(entry)
            if norm is not None and norm not in owns:
                owns.append(norm)
    name = fm.get("spec") if isinstance(fm.get("spec"), str) else None
    lva = fm.get("last_verified_at")
    if not isinstance(lva, str) or lva.strip().lower() in ("", "null", "none", "~"):
        lva = None
    return SpecFile(
        path=path,
        name=name or posixpath.splitext(posixpath.basename(path))[0],
        owns=owns,
        last_verified_at=lva,
        text=text,
    )


def _is_spec_member(rel: str, spec_dir: str) -> bool:
    if not rel.endswith(".md") or not rel.startswith(spec_dir + "/"):
        return False
    inner = rel[len(spec_dir) + 1:]
    return RETIRED_DIR not in inner.split("/")[:-1]


def load_specs_from_tree(root: Union[str, Path], spec_dir: str = DEFAULT_SPEC_DIR) -> List[SpecFile]:
    """Every spec under ``root/spec_dir`` (recursive), sorted by path.

    ``root`` is a checkout or an extracted tarball. A bad ``spec_dir`` yields nothing
    rather than raising: the caller treats "no specs" as the stable no-input case.
    """
    sd = safe_spec_dir(spec_dir)
    if sd is None:
        return []
    base = Path(root) / sd
    if not base.is_dir() or not base.resolve().is_relative_to(Path(root).resolve()):
        return []
    out: List[SpecFile] = []
    for f in sorted(base.rglob("*.md")):
        if not f.resolve().is_relative_to(Path(root).resolve()) or not f.is_file() or f.stat().st_size > MAX_SPEC_BYTES:
            continue
        rel = f.relative_to(Path(root)).as_posix()
        if not _is_spec_member(rel, sd):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        spec = spec_from_text(rel, text)
        if spec is not None:
            out.append(spec)
    return out


def load_specs_from_tarball(tarball: bytes, spec_dir: str = DEFAULT_SPEC_DIR) -> List[SpecFile]:
    """Every spec under ``spec_dir`` in a GitHub source tarball, WITHOUT extracting it.

    GitHub archives wrap the tree in one top-level ``<repo>-<sha>/`` directory, which is
    stripped. Only regular members are read, each capped at ``MAX_SPEC_BYTES``; nothing
    touches the filesystem, so a hostile member name (absolute, ``..``, a link) has
    nothing to escape into — it is simply not a spec path and is skipped.
    """
    sd = safe_spec_dir(spec_dir)
    if sd is None or not tarball:
        return []
    out: List[SpecFile] = []
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as t:
        for member in t:
            if not member.isreg() or member.size > MAX_SPEC_BYTES:
                continue
            parts = member.name.split("/", 1)
            if len(parts) != 2:
                continue
            rel = safe_repo_relpath(parts[1])
            if rel is None or not _is_spec_member(rel, sd):
                continue
            fh = t.extractfile(member)
            if fh is None:
                continue
            try:
                text = fh.read().decode("utf-8")
            except UnicodeDecodeError:
                continue
            spec = spec_from_text(rel, text)
            if spec is not None:
                out.append(spec)
    out.sort(key=lambda s: s.path)
    return out


def owning_specs(
    changed_paths: Iterable[str], specs: Sequence[SpecFile], *, exclude_prefix: Optional[str] = DEFAULT_SPEC_DIR,
) -> List[Tuple[SpecFile, List[str]]]:
    """``[(spec, touched_paths)]`` for every spec that owns at least one changed path.

    ``exclude_prefix`` keeps spec files themselves (and anything else under the spec
    dir) from counting as owned code — the same carve-out the CI script makes for
    ``docs/``. Order follows ``specs``; touched paths follow ``changed_paths``. Multiple
    specs may own one path and each one is reported (``docs/SPEC_DRIFT.md``).
    """
    changed: List[str] = []
    for raw in changed_paths:
        p = safe_repo_relpath(raw)
        if p is None or p in changed:
            continue
        if exclude_prefix and path_under(p, exclude_prefix):
            continue
        changed.append(p)
    out: List[Tuple[SpecFile, List[str]]] = []
    for spec in specs:
        touched = [c for c in changed if any(path_under(c, o) for o in spec.owns)]
        if touched:
            out.append((spec, touched))
    return out


def spec_file_changed(spec: SpecFile, changed_paths: Iterable[str]) -> bool:
    """Was the spec's own file part of the change?"""
    target = safe_repo_relpath(spec.path)
    return any(safe_repo_relpath(p) == target for p in changed_paths)
