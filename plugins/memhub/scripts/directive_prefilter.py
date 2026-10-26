#!/usr/bin/env python3
"""Bash gate for the PreToolUse directive-recall hook (stdlib only).

The directive hook fires on the identifying handle of the in-flight tool call
(``directive_recall.py``). For Edit / Write / NotebookEdit that handle is a file
path and firing is always worthwhile — you're about to mutate a concrete target.
For **Bash** it is the command string, and Bash is polymorphic: most calls in a
working session are *reads* (``grep``, ``git show``, ``cat``, ``ls``, ``find``)
whose argument lists are DENSE with concrete symbols — exactly the shape that
trips the server-side symbol tripwire, producing a flood of edit-oriented
directives while the agent is merely looking. Reads name the most entities and
benefit the least; writes name the fewest and benefit the most.

So this gate runs BEFORE ``directive_recall.py`` on the Bash path and decides on
``tool_input.command`` alone: exit 0 (recall) only when the command actually
*mutates durable state* — filesystem, repo, packages, migrations, infra — and
exit 1 (skip, emit nothing) for read-only inspection. Precision-first: when a
command is unrecognized or unparseable we SKIP. A missed directive on an odd
command is recoverable and cheap; a false fire costs attention on every call and
risks the agent acting on an irrelevant procedure. This inverts xmem's
recall-first *extraction* bias — at the serving/hook layer, precision wins.

Runs under the system ``python3`` (no uv, no deps) so the expensive
``uv run ... directive_recall.py`` spawn only happens on true mutations. Mirrors
``flush_prefilter.py``'s stdin/exit-code contract: 0 = proceed, 1 = skip.
"""
from __future__ import annotations

import json
import re
import sys

# Verbs whose bare presence means a durable mutation.
_MUTATING_SIMPLE = frozenset({
    "rm", "rmdir", "unlink", "mv", "cp", "mkdir", "touch", "chmod", "chown",
    "chgrp", "ln", "truncate", "dd", "shred", "tee", "rsync", "patch",
    "install", "make", "cmake", "ninja", "gradle", "mvn", "ansible-playbook",
})

# Verbs whose mutation depends on their subcommand. A verb here fires only when
# one of its listed subcommands (the first non-flag token after the verb, or
# ANY token for the package-manager / gh cases) is present.
_MUTATING_SUB = {
    "git": frozenset({
        "commit", "push", "rebase", "merge", "reset", "checkout", "switch",
        "stash", "apply", "cherry-pick", "revert", "clean", "worktree", "tag",
        "am", "mv", "rm", "pull", "restore",
    }),
    "docker": frozenset({
        "build", "run", "compose", "push", "pull", "rm", "rmi", "exec",
        "create", "start", "stop", "kill", "restart", "tag", "prune", "up",
        "down", "commit", "save", "load", "import", "export",
    }),
    "kubectl": frozenset({
        "apply", "delete", "create", "patch", "replace", "scale", "rollout",
        "edit", "label", "annotate", "cordon", "drain", "taint", "set",
        "expose", "run", "exec", "cp",
    }),
    "terraform": frozenset({
        "apply", "destroy", "import", "taint", "untaint", "state", "refresh",
        "fmt", "init",
    }),
    "helm": frozenset({
        "install", "upgrade", "uninstall", "rollback", "delete", "create",
    }),
    "alembic": frozenset({
        "upgrade", "downgrade", "revision", "stamp", "merge", "edit",
    }),
}

# Package managers: mutating when the segment carries a state-changing action
# word (anywhere after the verb — these tools accept flags in any order).
_PKG_MANAGERS = frozenset({
    "pip", "pip3", "uv", "npm", "pnpm", "yarn", "poetry", "pipenv", "brew",
    "cargo", "gem", "go", "bundle", "apt", "apt-get", "dnf", "yum", "conda",
})
_PKG_ACTIONS = frozenset({
    "install", "uninstall", "add", "remove", "rm", "i", "ci", "sync",
    "update", "upgrade", "publish", "link", "unlink", "get",
})

# gh is mutating only when a write action follows (``gh pr create`` mutates,
# ``gh pr view`` reads).
_GH_ACTIONS = frozenset({
    "create", "merge", "close", "delete", "edit", "comment", "ready",
    "review", "add", "remove", "set", "upload", "approve", "lock", "unlock",
    "pin", "rename", "transfer", "sync",
})

# Assignment / wrapper tokens that may precede the real verb inside a segment
# (``GIT_EDITOR=true git commit``, ``sudo -E rm x``, ``timeout 60 make``).
_WRAPPERS = frozenset({
    "env", "nohup", "command", "exec", "time", "timeout", "sudo", "nice",
    "stdbuf", "caffeinate", "xargs", "then", "do", "else", "elif",
})
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Interpreter-invoked mutators the verb parser can't see (verb is ``python`` /
# ``flask`` / etc.): matched against the raw, quote-stripped command.
_GLOBAL_MUTATORS = (
    re.compile(r"manage\.py\s+(?:migrate|makemigrations|flush|loaddata|"
               r"collectstatic|createsuperuser)\b"),
    re.compile(r"\bprisma\s+(?:migrate|db|generate)\b"),
    re.compile(r"\bflask\s+db\s+(?:upgrade|downgrade|migrate|stamp)\b"),
)

# A file-writing redirect: an unquoted > / >> not part of a `2>&1` / `&>` fd
# dup, followed by a target. Best-effort — quotes are stripped first.
_REDIRECT_RE = re.compile(r"(?<![0-9&])>>?\s*[^\s&|;<>()]")

# fd-dups (`2>&1`, `>&2`) and discards to /dev/null carry no durable write and
# must not trip the redirect check; blanked before segmentation.
_DISCARD_RE = re.compile(r"(?:&>|\d?>>?)\s*/dev/null|\d?>&\d?")

# Split a (quote-stripped) command into shell segments on top-level operators.
_SEGMENT_RE = re.compile(r"[;\n()]|\|\|?|&&?")


def _strip_quotes(command: str) -> str:
    """Blank out quoted spans so their contents can't leak verbs / operators.

    ``echo "git commit"`` → ``echo `` (verb is echo, not git); non-nested and
    escape-agnostic, matching the pragmatic level of ``flush_prefilter.py``.
    """
    command = re.sub(r'"[^"]*"', " ", command)
    command = re.sub(r"'[^']*'", " ", command)
    return command


def _segment_verb_and_tokens(segment: str) -> tuple[str, list[str]]:
    """First real verb of a segment + its remaining tokens.

    Skips leading env-assignments and wrapper words (and a wrapper's own flag /
    duration / assignment tokens) so ``sudo -E rm`` resolves to ``rm``.
    """
    tokens = segment.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _ASSIGN_RE.match(tok):
            i += 1
            continue
        base = tok.rsplit("/", 1)[-1]
        if base in _WRAPPERS:
            i += 1
            # consume the wrapper's flags / numeric args / assignments
            while i < len(tokens) and (
                tokens[i].startswith("-")
                or tokens[i].isdigit()
                or _ASSIGN_RE.match(tokens[i])
            ):
                i += 1
            continue
        return base, tokens[i + 1:]
    return "", []


def _first_nonflag(tokens: list[str]) -> str:
    for t in tokens:
        if not t.startswith("-"):
            return t.rsplit("/", 1)[-1]
    return ""


def _segment_mutates(segment: str) -> bool:
    if _REDIRECT_RE.search(segment):
        return True
    verb, rest = _segment_verb_and_tokens(segment)
    if not verb:
        return False
    if verb in _MUTATING_SIMPLE:
        return True
    if verb in _MUTATING_SUB:
        return _first_nonflag(rest) in _MUTATING_SUB[verb]
    if verb in _PKG_MANAGERS:
        bases = {t.rsplit("/", 1)[-1] for t in rest if not t.startswith("-")}
        return bool(bases & _PKG_ACTIONS)
    if verb == "gh":
        bases = {t.rsplit("/", 1)[-1] for t in rest if not t.startswith("-")}
        return bool(bases & _GH_ACTIONS)
    if verb in ("sed", "perl"):
        return any(t == "-i" or t.startswith("-i") for t in rest)
    return False


def command_mutates(command: str) -> bool:
    """True when the command changes durable state (→ recall directives)."""
    stripped = _DISCARD_RE.sub(" ", _strip_quotes(command))
    if any(p.search(stripped) for p in _GLOBAL_MUTATORS):
        return True
    return any(
        _segment_mutates(seg)
        for seg in _SEGMENT_RE.split(stripped)
        if seg.strip()
    )


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        command = str((hook_input.get("tool_input") or {}).get("command", ""))
    except Exception:  # noqa: BLE001 — malformed input → skip (precision-first)
        return 1
    return 0 if command and command_mutates(command) else 1


if __name__ == "__main__":
    raise SystemExit(main())
