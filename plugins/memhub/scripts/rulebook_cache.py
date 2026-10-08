#!/usr/bin/env python3
"""Per-call caches for the rulebook hook (stdlib only).

Every tool call starts a fresh `rulebook_hook.py` process. Two of the things
it computed each time were the same answer from the same inputs:

* the book's rows normalised by `to_hook_rule` (every regex re-linted), and
* the checkout's repo name, which costs a `git remote get-url` subprocess.

Both are kept on disk here, keyed on everything the answer depends on, so a
call whose inputs have not changed reads one small file instead. A missing,
corrupt or mismatched cache file is a miss and nothing else: the caller
computes the answer exactly as it would without this module. Only the hook's
own process uses it (`rulebook_hook._RUN_CACHES`); an importer of the hook
never writes these files.
"""
import hashlib
import json
import os

NORM_CACHE_V = 1
REPO_NAMES_FILE = "repo-names.json"
REPO_NAMES_MAX = 256        # entries kept; a full map drops its oldest half
_REPO_NAME_MEMO = {}        # (root, gitdir) → name, for this process


def _stat_key(path):
    try:
        st = os.stat(path)
        return [st.st_mtime_ns, st.st_size, st.st_ino]
    except OSError:
        return None


def _write_json(path, obj):
    # Imported here: only a miss writes, and pathlib is ~1 ms a hit never pays.
    from pathlib import Path

    from atomic_write import publish
    publish(Path(path), json.dumps(obj), mode=0o600)


def hook_identity(hook_file, version, claimed):
    """Everything `to_hook_rule` reads besides the row: the hook file and the
    `spec_owns` lint it imports (path, mtime, size — so a plugin upgrade or a
    local edit changes it), the hook version `degradation` compares against,
    and whether a forward-test claim is active (it decides whether a
    candidate row survives)."""
    here = os.path.dirname(os.path.abspath(hook_file))
    out = [NORM_CACHE_V, os.path.abspath(hook_file),
           list(version) if version else None, bool(claimed)]
    for name in (os.path.basename(hook_file), "spec_owns.py"):
        st = _stat_key(os.path.join(here, name))
        out.append(st[:2] if st else None)
    return out


def _book_of(raw):
    """A book file's bytes as the book, or None when they are not one —
    the same test `rulebook_hook.load_book` applies."""
    try:
        b = json.loads(raw)
        return b if isinstance(b, dict) and isinstance(b.get("rules"), list) else None
    except Exception:
        return None


def cached_rules(path, normalise, hook_file, version, claimed):
    """`normalise(book)` for the book at `path`, kept beside it in
    `<path>.norm` (not `.json`: nothing that scans `book/*.json` reads it).

    The key is the sha1 of the book's exact bytes plus `hook_identity`. It is
    hashed from the same bytes that are parsed, so a book replaced mid-call
    can never be cached under the other one's key. `normalise` returns the
    hook's `(rules, "", fetched_at, sources)`; a hit rebuilds `sources`
    from the rules, which `normalise` already de-duplicated by id."""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return normalise(None)
    key = [hashlib.sha1(raw).hexdigest(), hook_identity(hook_file, version, claimed)]
    try:
        with open(path + ".norm", encoding="utf-8") as f:
            c = json.load(f)
        rules = c.get("rules") if c.get("key") == key else None
        if isinstance(rules, list) and all(isinstance(r, dict) and r.get("id") for r in rules):
            return rules, "", c.get("fetched_at"), {r["id"]: "server" for r in rules}
    except Exception:
        pass
    book = _book_of(raw)
    out = normalise(book)
    if book is not None:
        try:
            _write_json(path + ".norm", {"key": key, "fetched_at": out[2], "rules": out[0]})
        except Exception:
            pass
    return out


def repo_name_key(root, gitdir):
    """The files `git -C root remote get-url origin` answers from, as stat
    facts, or None when they cannot be named (no gitdir).

    That is the checkout's own config (the COMMON dir's for a linked
    worktree, plus its `config.worktree`), the `commondir` pointer itself,
    the global and system configs (an `insteadOf` there rewrites the URL),
    and the GIT_* variables that relocate any of them. `git remote set-url`
    rewrites the common config, so it changes this key. Files pulled in by
    an `[include]` are not followed."""
    if not gitdir:
        return None
    common = gitdir
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as f:
            common = os.path.normpath(os.path.join(gitdir, f.read().strip()))
    except OSError:
        pass
    home = os.path.expanduser("~")
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    files = [os.path.join(common, "config"), os.path.join(gitdir, "config.worktree"),
             os.path.join(gitdir, "commondir"), os.path.join(home, ".gitconfig"),
             os.path.join(xdg, "git", "config"), "/etc/gitconfig"]
    env = sorted([k, v] for k, v in os.environ.items()
                 if k.startswith("GIT_CONFIG") or k in ("GIT_DIR", "GIT_COMMON_DIR"))
    return [[f, _stat_key(f)] for f in files] + [env]


def repo_name(base, root, gitdir, resolve):
    """`resolve(root, gitdir)` → (name, settled), remembered across hook
    processes in `<base>/repo-names.json` so the common call spawns no `git`.
    An entry is used only while `repo_name_key` is unchanged, and only a name
    git itself settled is stored (a failed or timed-out lookup is retried).
    A cache read or write failure is a miss; `resolve`'s own errors propagate."""
    memo = _REPO_NAME_MEMO.get((root, gitdir))
    if memo is not None:
        return memo
    ident = root + "\0" + gitdir
    path = os.path.join(base, REPO_NAMES_FILE)
    key = repo_name_key(root, gitdir)
    names = {}
    if key is not None:
        try:
            with open(path, encoding="utf-8") as f:
                names = json.load(f)
            hit = names.get(ident) if isinstance(names, dict) else None
            if isinstance(hit, dict) and hit.get("key") == key \
                    and isinstance(hit.get("name"), str) and hit["name"]:
                _REPO_NAME_MEMO[(root, gitdir)] = hit["name"]
                return hit["name"]
        except Exception:
            pass
    name, settled = resolve(root, gitdir)
    _REPO_NAME_MEMO[(root, gitdir)] = name
    if key is not None and settled:
        try:
            names = names if isinstance(names, dict) else {}
            names.pop(ident, None)
            if len(names) >= REPO_NAMES_MAX:
                names = dict(list(names.items())[REPO_NAMES_MAX // 2:])
            names[ident] = {"key": key, "name": name}
            _write_json(path, names)
        except Exception:
            pass
    return name
