"""Identifiers a branch is about — from git. Never words.

The brief keys its recall lookup on **identifiers**: file paths and PR /
issue numbers. Not on what the branch is "about". Per-prompt semantic
injection is the channel Tencent's teamai-cli retired as "noisy and
low-hit-rate" (the repo brain holds the research note); the ambient channel
that survived is identifier-keyed, so this module is the whole of what gets
extracted.

``from_git`` — what this branch touches: ``git diff --name-only
origin/<default>`` plus the paths of the last 20 commits, and the PR / ENG
numbers in the branch name and those commits' subjects.

Stdlib only: ``from_git`` runs on the synchronous SessionStart path and
``current_branch`` on the synchronous UserPromptSubmit path.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import room_map  # noqa: E402

GIT_TIMEOUT_S = 2.0
RECENT_COMMITS = 20
MAX_PATHS = 120

_PR_RE = re.compile(r"(?:\bPR\s*)?#(\d{1,7})\b")
_ENG_RE = re.compile(r"\bENG-(\d{1,7})\b", re.I)


# ── git ────────────────────────────────────────────────────────────────────

def _git(root: str | Path, *args: str) -> str:
    try:
        out = subprocess.run(
            room_map.git_readonly(root) + list(args), env=room_map.git_env(),
            capture_output=True, text=True, timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def default_branch_ref(root: str | Path) -> str:
    """``origin/<default>`` — from origin/HEAD, else the first of main/master
    that exists as a remote-tracking ref; "" when neither does (no fetch here)."""
    ref = _git(root, "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD").strip()
    if ref:
        return ref
    for cand in ("origin/main", "origin/master"):
        if _git(root, "rev-parse", "-q", "--verify", cand).strip():
            return cand
    return ""


def current_branch(root: str | Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()


def refs_in(text: str) -> list[str]:
    """PR / ENG references, rendered canonically (``PR #182``, ``ENG-1010``)."""
    out: list[str] = []
    for m in _PR_RE.finditer(text or ""):
        out.append(f"PR #{m.group(1)}")
    for m in _ENG_RE.finditer(text or ""):
        out.append(f"ENG-{m.group(1)}")
    return _dedupe(out)


def from_git(cwd: str | Path) -> dict:
    """``{root, branch, head, base, paths, refs}`` for the checkout at ``cwd``.

    ``paths`` are repo-relative, most immediate first: uncommitted changes,
    then the paths of the last 20 commits, then the working tree's whole diff
    against ``origin/<default>``. ``refs`` come from the branch name and those commits'
    subjects. Everything is empty outside a repo; nothing here raises.
    """
    root = room_map.repo_root(cwd)
    if root is None:
        return {"root": "", "branch": "", "head": "", "base": "",
                "paths": [], "refs": []}
    branch = current_branch(root)
    head = _git(root, "rev-parse", "HEAD").strip()
    base = default_branch_ref(root)
    # Most immediate first, because MAX_PATHS is a cap: the working tree's
    # own changes, then the last commits' paths, then the whole delta against
    # the default branch (which on a long-lived branch is hundreds of files).
    paths: list[str] = _git(root, "diff", "--name-only", "HEAD").splitlines()
    subjects: list[str] = []
    log = _git(root, "log", f"-{RECENT_COMMITS}", "--name-only",
               "--format=%x1e%s")
    for block in log.split("\x1e"):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        subjects.append(lines[0])
        paths += lines[1:]
    if base:
        paths += _git(root, "diff", "--name-only", base).splitlines()
    paths = _dedupe(p.strip() for p in paths if p.strip())[:MAX_PATHS]
    refs = refs_in(" ".join([branch, *subjects]))
    return {"root": str(root), "branch": branch, "head": head, "base": base,
            "paths": paths, "refs": refs}


def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out
