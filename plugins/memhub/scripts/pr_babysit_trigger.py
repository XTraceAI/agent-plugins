#!/usr/bin/env python3
"""PostToolUse(Bash) hook: after a successful `gh pr create`, inject context
telling Claude to start a self-paced loop running /memhub:pr-babysit on the
new PR. Emits nothing (hook is a no-op) unless the command was a PR creation
whose output contains a PR URL."""

import json
import re
import sys

PR_URL = re.compile(r"https?://[^/\s\"\\]+/[^/\s\"\\]+/[^/\s\"\\]+/pull/\d+")
# Quoted segments are stripped before matching so a search pattern like
# grep "gh pr create" can never look like a PR creation.
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
# `gh ... pr create` only with `gh` at command position — start of string or
# after a separator (&&, ;, |, subshell paren, backtick, newline). Before
# `gh`, only env-var assignments and common wrappers (env, sudo, nohup,
# command, exec, timeout) with their flags/duration args are tolerated; any
# other leading token (grep, rg, echo…) keeps the command from matching.
# Flags are allowed between `gh` and `pr create` but never across a
# separator (`gh repo view && foo pr create` must not match).
GH_PR_CREATE = re.compile(
    r"(?:^|[;&|`\n(]|\$\()\s*"
    r"(?:(?:\w+=\S*|env|sudo|nohup|command|exec|timeout|--?[\w=:,.-]+|\d[\w.]*)\s+)*"
    r"gh\b[^|;&\n]*?\bpr\s+create\b"
)


# A heredoc BODY is prose, not command text. `python3 - <<'PY' … PY` and
# `git commit -F - <<'MSG' … MSG` whose body merely contains the words
# `gh pr create` armed a babysit loop on whatever pull request the output
# happened to name — 19 such commands in a 15,134-call sample of real
# sessions. Kept byte-identical to pr_link.strip_heredocs and pinned by a
# shared-corpus agreement test; not imported, because this module is a hook
# entry point and a cross-hook import is a coupling neither wants.
MAX_HEREDOC_SCAN_CHARS = 256 * 1024
# An identifier tag only: `2 << 3` is arithmetic, not a heredoc.
HEREDOC_OPEN = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def strip_heredocs(command: str) -> str:
    """The command with every heredoc BODY removed, the opening line kept."""
    if "<<" not in command:
        return command
    lines = command[:MAX_HEREDOC_SCAN_CHARS].split("\n")
    kept = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kept.append(line)
        index += 1
        for match in HEREDOC_OPEN.finditer(line):
            tag = match.group(2)
            while index < len(lines) and lines[index].strip() != tag:
                index += 1
            index += 1
    return "\n".join(kept)


def is_pr_create(command: str) -> bool:
    return bool(GH_PR_CREATE.search(QUOTED.sub(" ", strip_heredocs(command))))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not is_pr_create(command):
        return

    # Only arm the loop if a PR URL actually came back — a failed
    # `gh pr create` produces no URL and should stay silent. gh prints the
    # new PR's URL to stdout; stderr and other fields can echo unrelated or
    # pre-existing PR URLs (e.g. "a pull request ... already exists"), so a
    # dict response without a string stdout yields nothing rather than a
    # serialized-whole-dict scan. A bare-string response has no field
    # structure to respect, so it is scanned as-is.
    response = payload.get("tool_response")
    if isinstance(response, dict):
        stdout = response.get("stdout")
        blob = stdout if isinstance(stdout, str) else ""
    elif isinstance(response, str):
        blob = response
    else:
        blob = ""
    match = PR_URL.search(blob)
    if not match:
        return
    url = match.group(0)

    context = (
        f"A pull request was just created: {url} . MemHub PR-babysit policy: "
        "unless the user asked not to babysit PRs (in this session or in memory), "
        "start a self-paced loop NOW that babysits it — invoke the loop skill with "
        f'no interval and the prompt "/memhub:pr-babysit {url}". Each loop pass '
        "polls the PR's review bots (Cursor bugbot, OpenAI Codex) and CI, fixes "
        "real findings, pushes, and when the PR is clean saves the whole fixing "
        "process to the repo's MemHub agent brain and ends the loop. Tell the "
        "user the babysit loop is running and that saying 'stop the loop' ends it."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
