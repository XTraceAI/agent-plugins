#!/usr/bin/env python3
"""Which repositories MemHub is active in: the per-machine allowlist.

Developers also work on client repositories with their coding agent, and
MemHub must not touch those: no team rules, no capture, no companion. So a
machine can carry a list of the repositories MemHub is FOR:

    ~/.config/memhub-plugin/repos.json
    {"version": 1, "mode": "allowlist", "repos": ["XTraceAI/MemHub-Backend", ...],
     "declined": ["ClientCo/app", ...]}

* **No file** → MemHub is active everywhere. Every install that exists today
  has no file, so an upgrade changes nothing.
* **A file** → the plugin is dormant in any checkout the list leaves out:
  the Rulebook fires nothing there, capture stores nothing, and the companion
  stays hidden. An EMPTY list is dormant everywhere. A file that cannot be
  read as that shape also counts as empty: the person wrote one to narrow
  MemHub, and a typo in it must not widen MemHub to every repository.

An entry is the checkout's identity: ``<org>/<name>`` from the ``origin``
remote (the two last path segments of its URL, ``.git`` dropped, the way
``room_map.room_name`` builds ``Repo: <org>/<name>``), or the bare ``<name>``
of a checkout with no ``origin`` (the main worktree's directory, as
``repo_identity`` names it). Matching is exact and case-insensitive.

``declined`` (ENG-1253) is the repositories the person answered **No** to
when the Claude Code status strip asked whether to enable MemHub there: as
dormant as any unlisted checkout, and never asked about again. ``repos``
wins over ``declined`` where a name is in both. The key is optional, so a
file an older plugin wrote reads as it did, and an older plugin reading a
file with it ignores the key (it reads ``repos`` alone).

Two questions, both answered from the SESSION's or the CALL's checkout,
never from the directory name alone (a worktree is named after its branch):

* ``enabled(path)`` — is MemHub active for the checkout holding ``path``?
  ``hook_entry`` asks it of the session cwd before any whole-session lane
  (capture, the brief, the session and prompt lanes), ``rulebook_hook`` asks
  it of each tool call's own checkout (``repo_of_call``: a session started in
  a parent folder of several repos is judged per call), and
  ``rulebook_mod_cli.py paths`` answers it to the mod as ``enabled``, so the
  TypeScript lanes and the companion honour the same list.
* ``session_enabled(cwd)`` — the same, except that a cwd OUTSIDE any checkout
  is dormant when the file exists: there is no repository to be listed.

Hot path: this runs on every tool call. One ``stat`` of the file when it is
unchanged (the parse is cached by mtime and size), and the checkout's
identity is a few file reads (``.git``, its ``config``), cached per root for
the process. No subprocess, no git. Stdlib only, Python 3.9.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Set, Tuple

FILE_NAME = "repos.json"
VERSION = 1
MODE = "allowlist"

#: path → ((mtime_ns, size), (listed, declined)): both sets lower-cased.
_LOADED: Dict[str, Tuple[Tuple[int, int], Tuple[Set[str], Set[str]]]] = {}
#: root → (identity as written, lower-cased identity)
_IDENTITY: Dict[str, Tuple[str, str]] = {}


# ── where the file is ────────────────────────────────────────────────────────

def config_dir() -> str:
    """The plugin's per-user state directory (``_memhub_auth`` reads the same
    variable for the credentials), ``~/.config/memhub-plugin`` by default."""
    return os.environ.get("MEMHUB_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "memhub-plugin")


def allowlist_path() -> str:
    """``$MEMHUB_REPOS_FILE``, else ``<config_dir>/repos.json``."""
    return os.environ.get("MEMHUB_REPOS_FILE") or os.path.join(config_dir(), FILE_NAME)


# ── the list ─────────────────────────────────────────────────────────────────

def normalize(entry: object) -> str:
    """One entry as it is matched: stripped, ``.git`` dropped, lower-cased,
    slashes collapsed. "" for anything that is not a usable name."""
    if not isinstance(entry, str):
        return ""
    s = entry.strip().strip("/").lower()
    if s.endswith(".git"):
        s = s[:-4]
    s = re.sub(r"/+", "/", s)
    return s if s and " " not in s else ""


def _names(value: object) -> Set[str]:
    return {n for n in (normalize(r) for r in value) if n} if isinstance(value, list) else set()


def parse_both(text: str) -> Optional[Tuple[Set[str], Set[str]]]:
    """``(listed, declined)`` of a repos.json body, lower-cased, or None when
    the body is not the shape this module writes (treated by callers as an
    empty list). A missing or malformed ``declined`` is an empty set: the
    key is optional, and a typo in it must not unlist anything. A name in
    both is listed, not declined."""
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    repos = doc.get("repos")
    if not isinstance(repos, list):
        return None
    listed = _names(repos)
    return listed, _names(doc.get("declined")) - listed


def parse(text: str) -> Optional[Set[str]]:
    """The listed entries of a repos.json body (``parse_both``'s first half)."""
    both = parse_both(text)
    return both[0] if both is not None else None


def load_both(path: Optional[str] = None) -> Optional[Tuple[Set[str], Set[str]]]:
    """``(listed, declined)``: None when there is no file (MemHub active
    everywhere), else the two sets of lower-cased entries (an unreadable or
    malformed file is two empty sets). Cached by the file's mtime and size."""
    path = path or allowlist_path()
    try:
        st = os.stat(path)
    except OSError:
        _LOADED.pop(path, None)
        return None
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _LOADED.get(path)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            parsed = parse_both(f.read())
    except (OSError, UnicodeDecodeError):
        parsed = None
    value = parsed if parsed is not None else (set(), set())
    _LOADED[path] = (stamp, value)
    return value


def load(path: Optional[str] = None) -> Optional[Set[str]]:
    """The allowlist: None when there is no file (MemHub active everywhere),
    else the set of lower-cased listed entries (an unreadable or malformed
    file is the empty set)."""
    both = load_both(path)
    return both[0] if both is not None else None


def load_declined(path: Optional[str] = None) -> Set[str]:
    """The declined entries, lower-cased; empty with no file."""
    both = load_both(path)
    return both[1] if both is not None else set()


def restricted(path: Optional[str] = None) -> bool:
    """True when a repos.json exists (MemHub is limited to the repos it lists)."""
    return load(path) is not None


# ── the checkout's identity ──────────────────────────────────────────────────

def checkout(path: str) -> Tuple[str, str]:
    """``(root, gitdir)`` of the checkout holding ``path`` — the nearest
    ancestor with a ``.git`` directory or a ``.git`` file (a linked worktree,
    whose file names its gitdir) — or ``("", "")`` outside any checkout.
    The walk ``rulebook_hook.repo_info`` does, without the name."""
    d = os.path.abspath(path or "")
    while d:
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return d, g
        if os.path.isfile(g):
            try:
                with open(g, encoding="utf-8") as f:
                    gitdir = f.read().split(":", 1)[1].strip()
                if not os.path.isabs(gitdir):
                    gitdir = os.path.normpath(os.path.join(d, gitdir))
            except (OSError, IndexError, UnicodeDecodeError):
                gitdir = ""
            return d, gitdir
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return "", ""


def _common_gitdir(gitdir: str) -> str:
    """The ``.git`` of the main worktree: ``gitdir`` itself, or where a linked
    worktree's ``commondir`` points (``repo_identity._main_worktree_basename``)."""
    if not gitdir:
        return ""
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as f:
            return os.path.normpath(os.path.join(gitdir, f.read().strip()))
    except OSError:
        return gitdir


_SECTION = re.compile(r'^\s*\[\s*remote\s+"([^"]*)"\s*\]\s*$')
_ANY_SECTION = re.compile(r"^\s*\[")
_URL = re.compile(r"^\s*url\s*=\s*(.+?)\s*$")


def origin_url(gitdir: str) -> str:
    """The ``origin`` remote's URL from the repository's own ``config`` file
    (the main worktree's for a linked worktree), or "" when there is none.
    A file read, not ``git remote get-url``: this runs on every tool call."""
    common = _common_gitdir(gitdir)
    if not common:
        return ""
    try:
        with open(os.path.join(common, "config"), encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""
    in_origin = False
    for line in lines:
        m = _SECTION.match(line)
        if m:
            in_origin = m.group(1) == "origin"
            continue
        if _ANY_SECTION.match(line):
            in_origin = False
            continue
        if in_origin:
            u = _URL.match(line)
            if u:
                return u.group(1).strip().strip('"')
    return ""


def identity_of_url(url: str) -> str:
    """``org/name`` from a remote URL, the way ``room_map.room_name`` reduces
    one: scp-style ``git@host:org/name.git`` and URL-style
    ``https://host/org/name.git`` both keep their LAST two path segments;
    a URL with one segment gives the bare name. "" for an empty URL."""
    path = url.strip().rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if "://" in path:
        path = path.split("://", 1)[1]
        path = path.split("/", 1)[1] if "/" in path else ""
    elif ":" in path:
        path = path.rsplit(":", 1)[-1]
    segments = [s for s in path.split("/") if s]
    return "/".join(segments[-2:])


def identity(root: str, gitdir: str = "") -> str:
    """The checkout's identity as written in repos.json: ``org/name`` from
    ``origin``, else the bare name of the main worktree's directory (a linked
    worktree is named after its branch, never after the repo). "" when
    ``root`` is not a checkout. Cached per root for the process."""
    if not root:
        return ""
    hit = _IDENTITY.get(root)
    if hit is not None:
        return hit[0]
    if not gitdir:
        _root, gitdir = checkout(root)
        if not _root:
            return ""
    name = identity_of_url(origin_url(gitdir))
    if not name:
        common = _common_gitdir(gitdir)
        main = os.path.dirname(common) if os.path.basename(common) == ".git" else ""
        name = os.path.basename((main or root).rstrip(os.sep)) or os.path.basename(root)
    _IDENTITY[root] = (name, normalize(name))
    return name


def allows(name: str, path: Optional[str] = None) -> bool:
    """Whether ``name`` (an identity) is enabled: no file, or listed."""
    listed = load(path)
    if listed is None:
        return True
    return normalize(name) in listed


def is_declined(name: str, path: Optional[str] = None) -> bool:
    """Whether the person answered No to enabling MemHub in ``name``: it is
    in ``declined`` and not in ``repos``. False with no file."""
    return normalize(name) in load_declined(path)


def declined_for(path: str) -> bool:
    """``is_declined`` for the checkout holding ``path``; False outside one."""
    root, gitdir = checkout(path)
    return bool(root) and is_declined(identity(root, gitdir))


# ── the two answers ──────────────────────────────────────────────────────────

def enabled(path: str) -> bool:
    """Is MemHub active for the checkout holding ``path``? True with no file.
    With one, true only for a listed checkout; a ``path`` outside any
    checkout is true as well — there is nothing to judge, and the caller
    that cares (``session_enabled``) says so itself."""
    listed = load()
    if listed is None:
        return True
    root, gitdir = checkout(path)
    if not root:
        return True
    return normalize(identity(root, gitdir)) in listed


def session_enabled(cwd: str) -> bool:
    """``enabled(cwd)``, except that with a file a cwd outside any checkout
    is dormant: a session started in a folder that is no repository (a parent
    of several) is captured for none of them. Its tool calls into a listed
    checkout are still judged, call by call, by the Rulebook lanes."""
    listed = load()
    if listed is None:
        return True
    root, gitdir = checkout(cwd)
    if not root:
        return False
    return normalize(identity(root, gitdir)) in listed


def status(cwd: str) -> Dict[str, object]:
    """One dict for a session: ``restricted`` (a file exists), ``repo`` (the
    cwd's identity, "" outside a checkout), ``enabled``, ``declined`` (the
    person answered No to this repository), and ``path``."""
    root, gitdir = checkout(cwd)
    repo = identity(root, gitdir) if root else ""
    return {"restricted": restricted(), "repo": repo,
            "enabled": session_enabled(cwd),
            "declined": bool(repo) and is_declined(repo),
            "path": allowlist_path()}


# ── what the hooks say when dormant ──────────────────────────────────────────

def plugin_name(scripts_dir: Optional[str] = None) -> str:
    """``memhub`` or ``memhub-staging``, from the manifest beside ``scripts/``
    (``_memhub_auth.plugin_name`` does the same; this one stays stdlib-light
    for the hook entry). ``memhub`` when none is readable."""
    root = os.path.dirname(scripts_dir or os.path.dirname(os.path.abspath(__file__)))
    for rel in ("plugin.json", os.path.join(".claude-plugin", "plugin.json")):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                name = json.load(f).get("name")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(name, str) and name:
            return name
    return "memhub"


def dormant_note(cwd: str, scripts_dir: Optional[str] = None) -> str:
    """The one line SessionStart adds to the model's context in a dormant
    session: that MemHub is inactive here, and how to enable it."""
    skill = "/%s:repos" % plugin_name(scripts_dir)
    info = status(cwd)
    if info["repo"] and info["declined"]:
        return ("MemHub is inactive in this repository: %s is declined in %s (the person "
                "answered No when asked to enable it here). To enable it after all run "
                "`%s add .`; `%s all` enables it for every repository."
                % (info["repo"], info["path"], skill, skill))
    if info["repo"]:
        return ("MemHub is inactive in this repository: %s is not listed in %s. "
                "To enable it here run `%s add .`; `%s all` enables it for every repository."
                % (info["repo"], info["path"], skill, skill))
    return ("MemHub is inactive here: this folder is not inside a Git checkout, and %s "
            "limits MemHub to the repositories it lists. Team rules still apply to tool "
            "calls made inside a listed checkout; `%s list` shows them."
            % (info["path"], skill))


# ── writing (repos.py and the installer) ─────────────────────────────────────

def _clean(names: List[str]) -> Dict[str, str]:
    """normalized → as written: de-duplicated case-insensitively, ``.git`` dropped."""
    seen: Dict[str, str] = {}
    for r in names:
        n = normalize(r)
        if n and n not in seen:
            seen[n] = r.strip().strip("/")
            if seen[n].endswith(".git"):
                seen[n] = seen[n][:-4]
    return seen


def write(repos: List[str], path: Optional[str] = None,
          declined: Optional[List[str]] = None) -> str:
    """Write the allowlist atomically (0600, like the credentials beside it)
    and return its path. Entries keep their spelling, are de-duplicated
    case-insensitively, and are sorted. ``declined`` None keeps the file's
    current declined entries; a listed name is never also declined, so
    listing one clears its decline. The key is written only when there is
    something in it, so a file with no declines is byte-for-byte what every
    version before ENG-1253 wrote."""
    path = path or allowlist_path()
    seen = _clean(repos)
    if declined is None:
        declined = declined_entries(path) or []
    said_no = {k: v for k, v in _clean(declined).items() if k not in seen}
    doc: Dict[str, object] = {"version": VERSION, "mode": MODE,
                              "repos": [seen[k] for k in sorted(seen)]}
    if said_no:
        doc["declined"] = [said_no[k] for k in sorted(said_no)]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    _LOADED.pop(path, None)
    return path


def remove(path: Optional[str] = None) -> bool:
    """Delete the allowlist (MemHub active everywhere). True when one was there."""
    path = path or allowlist_path()
    _LOADED.pop(path, None)
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def _written(path: Optional[str], key: str) -> Optional[List[str]]:
    path = path or allowlist_path()
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeDecodeError):
        return []
    if not isinstance(doc, dict) or not isinstance(doc.get("repos"), list):
        return []
    value = doc.get(key)
    if not isinstance(value, list):
        return []
    return [r.strip() for r in value if isinstance(r, str) and normalize(r)]


def entries(path: Optional[str] = None) -> Optional[List[str]]:
    """The listed entries as written (spelling kept), or None when there is
    no file. A malformed file reads as []."""
    return _written(path, "repos")


def declined_entries(path: Optional[str] = None) -> Optional[List[str]]:
    """The declined entries as written, None with no file, [] for none (or a
    malformed file). A name also listed is not among them."""
    said_no = _written(path, "declined")
    if not said_no:
        return said_no
    listed = {normalize(r) for r in (entries(path) or [])}
    return [r for r in said_no if normalize(r) not in listed]


def allow(name: str, path: Optional[str] = None) -> str:
    """The strip's **Yes** (and ``repos.py add``): list ``name``, clearing any
    decline of it. Creates the file when there is none. Returns the path."""
    return write((entries(path) or []) + [name], path)


def decline(name: str, path: Optional[str] = None) -> str:
    """The strip's **No**: record ``name`` as declined — dormant, and never
    asked about again — and take it off the list if it was on. The file
    exists already when the strip asks (it asks only when ``restricted``);
    with none this writes one whose list is empty, as the installer does.
    Returns the path."""
    key = normalize(name)
    kept = [r for r in (entries(path) or []) if normalize(r) != key]
    return write(kept, path, (declined_entries(path) or []) + [name])


if __name__ == "__main__":
    import sys
    print(json.dumps(status(sys.argv[1] if len(sys.argv) > 1 else os.getcwd())))
