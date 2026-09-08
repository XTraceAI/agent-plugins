"""Identifiers a session is about — from git and from the prompt. Never words.

The brief and the prompt hook key every lookup on **identifiers**: file paths,
symbols that exist in the repo, PR / issue numbers, quoted error strings. Not
on what the prompt is "about". Per-prompt semantic injection is the channel
Tencent's teamai-cli retired as "noisy and low-hit-rate" (the repo brain holds
the research note); the ambient channel that survived is identifier-keyed, so
this module is the whole of what gets extracted, and a prompt with nothing in
it gets nothing.

Two sources:

* ``from_git`` — what this branch touches: ``git diff --name-only
  origin/<default>`` plus the paths of the last 20 commits, and the PR / ENG
  numbers in the branch name and those commits' subjects.
* ``from_prompt`` — the same classes of token found in one user prompt, with
  symbols kept only when they occur in the repo's ``git ls-files`` set (a
  cheap, exact membership test — no fuzzy anything).

Stdlib only: the extractor runs on the synchronous SessionStart and
UserPromptSubmit paths.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import room_map  # noqa: E402

try:  # the one authoritative error-marker regex (directive_recall's twin)
    from reactive_prefilter import _ERROR_RE
except Exception:  # noqa: BLE001 — never let a missing sibling break extraction
    _ERROR_RE = re.compile(r"(?:Traceback|\b[A-Z][a-zA-Z]*Error\b|error:|fatal:"
                           r"|command not found|No such file or directory)")

GIT_TIMEOUT_S = 2.0
RECENT_COMMITS = 20
MAX_PATHS = 120
MAX_ENTITIES = 200          # server caps `entities` at 256
MIN_SYMBOL_LEN = 6
MAX_ERROR_LEN = 200
MIN_ERROR_LEN = 8

_PR_RE = re.compile(r"(?:\bPR\s*)?#(\d{1,7})\b")
_ENG_RE = re.compile(r"\bENG-(\d{1,7})\b", re.I)
_PATH_RE = re.compile(r"(?<![\w@])((?:[\w.-]+/)+[\w.-]+|[\w-]+\.[A-Za-z0-9]{1,8})")
_DOTTED_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
_SNAKE_RE = re.compile(r"\b([A-Za-z0-9]+(?:_[A-Za-z0-9]+)+)\b")
_QUOTED_RE = re.compile(r"\"([^\"\n]{%d,%d})\"|'([^'\n]{%d,%d})'|`([^`\n]{%d,%d})`"
                        % ((MIN_ERROR_LEN, MAX_ERROR_LEN) * 3))
_URL_RE = re.compile(r"https?://\S+")
# A quoted string is an error string when the authoritative marker regex hits
# OR it reads like a failure message — "Agent brain not found" carries no
# capitalised *Error token, and it is exactly what a user pastes.
_ERRORISH_RE = re.compile(
    r"\b(?:not found|failed|denied|refused|invalid|unexpected|missing|timed? ?out"
    r"|unauthori[sz]ed|forbidden|cannot|can't|unable to)\b", re.I)


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


def repo_files(root: str | Path) -> set[str]:
    """Every path, basename, stem and directory component of ``git ls-files``
    — the vocabulary a prompt symbol must belong to."""
    toks: set[str] = set()
    for line in _git(root, "ls-files").splitlines():
        p = line.strip()
        if not p:
            continue
        toks.add(p)
        parts = p.split("/")
        base = parts[-1]
        toks.add(base)
        stem = base.rsplit(".", 1)[0] if "." in base else base
        if len(stem) >= 4:
            toks.add(stem)
        toks.update(d for d in parts[:-1] if len(d) >= 4)
    return toks


# ── the entity list recall fires on ────────────────────────────────────────

def entities_for(paths: list[str], refs: list[str],
                 symbols: list[str] | None = None,
                 errors: list[str] | None = None) -> list[str]:
    """Relative path + basename per path (the server's own canonical forms —
    an absolute path would miss), then refs, symbols, error strings.

    Refs, symbols and errors are reserved FIRST under the cap: a large
    refactor's hundred paths would otherwise fill every slot and the branch's
    PR / ENG numbers — the reference-keyed half of Apply — would never reach
    the server."""
    tail = _dedupe([*refs, *(symbols or []), *(errors or [])])[:MAX_ENTITIES]
    seen = set(tail)
    out: list[str] = []
    for p in paths:
        rel = p.replace("\\", "/").strip("/")
        for cand in (rel, rel.rsplit("/", 1)[-1]):
            if cand and cand not in seen and len(out) < MAX_ENTITIES - len(tail):
                seen.add(cand)
                out.append(cand)
    return out + tail


# ── the prompt ─────────────────────────────────────────────────────────────

def from_prompt(prompt: str, repo_tokens: set[str]) -> dict:
    """``{paths, symbols, refs, errors}`` found in one prompt — each exact.

    A path is kept when it names a repo file, or is slashed with an extension
    or a repo directory in it (never bare prose like ``yes/no``); a symbol
    (``Module.name`` / ``snake_case``) only when it, or the module half of it,
    is in ``repo_tokens`` and it is at least six characters; refs and quoted
    error strings as they stand. URLs are stripped first so their path-like
    tails cannot pass as files.
    """
    text = _URL_RE.sub(" ", prompt or "")
    paths: list[str] = []
    for m in _PATH_RE.finditer(text):
        tok = m.group(1).strip(".,;:()[]{}")
        if not tok or tok.startswith("."):
            continue
        if tok in repo_tokens or _looks_like_repo_path(tok, repo_tokens):
            if re.search(r"[A-Za-z]", tok) and not tok.replace(".", "").isdigit():
                paths.append(tok)
    symbols: list[str] = []
    for m in _DOTTED_RE.finditer(text):
        whole = m.group(0)
        if whole in paths or len(whole) < MIN_SYMBOL_LEN:
            continue
        if whole in repo_tokens or m.group(1) in repo_tokens:
            symbols.append(whole)
    for m in _SNAKE_RE.finditer(text):
        tok = m.group(1)
        if len(tok) >= MIN_SYMBOL_LEN and tok in repo_tokens and tok not in paths:
            symbols.append(tok)
    errors: list[str] = []
    for m in _QUOTED_RE.finditer(text):
        s = next((g for g in m.groups() if g), "").strip()
        if s and (_ERROR_RE.search(s) or _ERRORISH_RE.search(s)):
            errors.append(s)
    paths = _dedupe(paths)
    stems = {p.rsplit("/", 1)[-1].rsplit(".", 1)[0] for p in paths} | set(paths)
    return {
        "paths": paths,
        "symbols": _dedupe(s for s in symbols if s not in stems),
        "refs": refs_in(text),
        "errors": _dedupe(errors),
    }


def _looks_like_repo_path(tok: str, repo_tokens: set[str]) -> bool:
    """A slashed token is a path when its last segment carries an extension
    or some segment is a repo file / directory. "client/server", "yes/no"
    and "input/output" are prose, and prose must stay silent and free."""
    if "/" not in tok:
        return False
    segs = [s for s in tok.split("/") if s]
    if not segs:
        return False
    if re.search(r"\.[A-Za-z0-9]{1,8}$", segs[-1]):
        return True
    return any(s in repo_tokens for s in segs)


def _dedupe(items) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for i in items:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out
