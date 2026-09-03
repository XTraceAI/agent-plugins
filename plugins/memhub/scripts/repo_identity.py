#!/usr/bin/env python3
"""The name of the repo a session works in — never the directory it sits in.

A worktree's directory is named after the BRANCH. Claude Code Desktop gives
every session one automatically (`<project-root>/.claude/worktrees/<name>`),
`claude --worktree` and the fleet scripts make them by the dozen, and each one
has a different basename than the repo it belongs to. Anything keyed on that
basename fragments per branch: the rulebook fetched a book for a repo called
`gate-ttl`, and `scope_repos: ["xmem"]` — matched by exact string on the server
(crud.team_memory_rules._rule_in_repo) — reached none of them.

The capture path settled this already; `directive_recall._git_remote_basename`
carries the rule in its docstring ("a directory basename is NEVER a scope
name"). This module is that policy with a file-read fallback, so the rulebook
hook — which runs on every tool call and resolves the repo before it knows
whether it has any work to do — need not pay for a subprocess to be correct.

Resolution order, first hit wins:

1. **The origin remote's basename.** Canonical: survives a rename, a second
   clone, and a cwd that is the parent of several repos.
2. **The main worktree's basename.** Pure file reads. Covers the offline and
   no-remote cases, and is the fast path when git is slow or absent.
3. **The directory's own basename.** What every caller did before; kept so a
   plain checkout with no remote behaves exactly as it always has.
"""
import os
import subprocess

_CACHE: dict[str, str] = {}


def _remote_basename(directory):
    """`origin`'s URL basename, or "". Half a second is the whole budget: a
    hook that fires on every tool call cannot wait on a slow filesystem or a
    credential prompt, and step 2 is standing by."""
    try:
        out = subprocess.run(["git", "-C", directory, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=0.5)
    except (OSError, subprocess.SubprocessError):
        return ""          # git missing, or timed out — the file read decides
    url = out.stdout.strip()
    if out.returncode != 0 or not url:
        return ""
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def _main_worktree_basename(gitdir):
    """The basename of the repo a LINKED worktree belongs to, or "".

    `gitdir` is `<main>/.git/worktrees/<name>`, and the `commondir` file in it
    points back at `<main>/.git` (usually as `../..`). Requiring that the
    resolved path is literally named `.git` is what keeps a submodule out:
    its commondir is `<super>/.git/modules/<sub>`, whose parent would yield
    the meaningless `modules`."""
    if not gitdir:
        return ""
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as f:
            common = os.path.normpath(os.path.join(gitdir, f.read().strip()))
    except OSError:
        return ""          # a main worktree has no commondir — nothing to fix
    if os.path.basename(common) != ".git":
        return ""
    return os.path.basename(os.path.dirname(common))


def repo_name(directory, gitdir="", fallback=""):
    """The repo `directory` belongs to. `gitdir` is the `.git` path a caller
    has already resolved (rulebook_hook.repo_info reads it anyway); pass it to
    skip re-deriving one. `fallback` overrides step 3 for a caller whose own
    basename is not `directory`'s.

    Memoized per (directory, gitdir): the pre lane resolves the same session
    cwd on every tool call, and step 1 is a subprocess."""
    if not directory:
        return fallback
    key = (directory, gitdir)
    if key in _CACHE:
        return _CACHE[key]
    name = (_remote_basename(directory)
            or _main_worktree_basename(gitdir)
            or fallback
            or os.path.basename(directory.rstrip("/")))
    _CACHE[key] = name
    return name
