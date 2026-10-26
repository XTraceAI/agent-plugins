#!/usr/bin/env python3
"""Precise stage-2 prefilter for the commit/PR flush hook (stdlib only).

The hooks.json stage-1 shell gate is a crude substring test over the WHOLE
hook JSON — cheap, but it passes when e.g. a `cat`'s stdout merely contains
"git commit". This script reads the hook input and decides on
``tool_input.command`` ALONE: exit 0 (flush) only when the command actually
runs a commit / PR action, including flagged variants (`git -C <path>
commit`, `git -c k=v commit`) and compound commands (`cd x && git commit`).

Runs under the system python3 (no uv, no deps) so the expensive
`uv run ... flush_session.py` spawn only happens on true triggers.
"""
from __future__ import annotations

import json
import re
import sys

# One non-separator character — every token below is segment-bounded so no
# pattern can cross a pipe: `git log --oneline | grep commit` won't fire.
_TOK = r"[^\s;&|()]"
# Tokens that may legitimately precede the command word INSIDE one segment:
# env-var assignments (`GIT_EDITOR=true git commit`) and wrapper commands
# that exec their argument (`env VAR=1 git ...`, `nohup git ...`,
# `timeout 60 git ...`, `sudo -E git ...`). Each wrapper may carry flag /
# duration / assignment tokens. Anything else before `git` (e.g. `echo`)
# still blocks the match, keeping `echo git commit` silent.
# Assignment values: a balanced quoted string is consumed WHOLE, so a value
# that merely mentions the phrase (`MSG="please git commit" ls`) cannot leak
# a bare `git` into match position — and `GIT_EDITOR="true" git commit`
# still works because the quoted value is swallowed before `git` is read.
# The (?!["']) lookahead is load-bearing: without it the regex engine
# backtracks into the unquoted branch and consumes `MSG="please` as a value,
# leaking the quoted string's interior (`git commit ...`) into match
# position. Quote-opened values MUST take the balanced-quote branch.
_ASSIGN = rf"""[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|(?!["']){_TOK}*)"""
_WRAPPER = r"(?:env|nohup|command|exec|time|timeout|sudo|nice|stdbuf|caffeinate)"
_PREFIX = rf"(?:(?:{_ASSIGN}|{_WRAPPER})\s+(?:(?:-{_TOK}*|\d+{_TOK}*|{_ASSIGN})\s+)*)*"
# A new shell segment (start, or after ; & | parens), optional assignment /
# wrapper prefixes, then `git`, then any number of GLOBAL-FLAG groups, then
# `commit`. A flag group is a `-`-initiated token plus at most one non-flag
# argument (`-C <path>`, `-c k=v`, `--git-dir=x`, `-p`), so arbitrarily many
# `git -c key=value` pairs still match — the old `{0,4}` token cap silently
# skipped real commits carrying 3+ pairs. Requiring each group to OPEN with
# `-` is also what keeps `git diff main commit` (commit as a ref/path word)
# from firing: a bare non-flag token between `git` and `commit` blocks the
# match. The flag ARGUMENT must not start with `-` so runs of standalone
# flags parse unambiguously instead of pairing up via backtracking.
# The subcommand must end its TOKEN, not just a regex word (`\b` holds
# between `commit` and `-msg`, so `git commit-tree` / `git commit-msg`
# would fire on a \b boundary). End-of-token = end of string or a
# whitespace/separator character.
_EOT = rf"(?!{_TOK})"
_GIT_COMMIT = re.compile(
    rf"(?:^|[;&|()]\s*){_PREFIX}git(?:\s+-{_TOK}*(?:\s+(?!-){_TOK}+)?)*?\s+commit{_EOT}"
)
_GH_PR = re.compile(
    rf"(?:^|[;&|()]\s*){_PREFIX}gh\s+pr\s+(?:create|merge){_EOT}"
)


def should_flush(command: str) -> bool:
    return bool(_GIT_COMMIT.search(command) or _GH_PR.search(command))


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        command = str((hook_input.get("tool_input") or {}).get("command", ""))
    except Exception:  # noqa: BLE001 — malformed input → don't flush
        return 1
    return 0 if should_flush(command) else 1


if __name__ == "__main__":
    raise SystemExit(main())
