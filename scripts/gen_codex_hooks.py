#!/usr/bin/env python3
"""Generate plugins/memhub/hooks/codex-hooks.json from claude-hooks.json.

Codex clones Claude's hook contract, but hook trust is per command definition.
The Codex file therefore folds MemHub's behaviors into one dispatcher per event
instead of exposing every directive, artifact, and capture subprocess as its
own approval. Capture still routes through ``codex_flush.py`` inside the bridge
because Codex sessions are rollout files, not Claude transcripts.

Generated, checked in, and pinned by tests/codex_hooks_parity_test.py — the
generator is the single place the Claude→Codex delta lives, so a directive
hook edited in claude-hooks.json cannot silently drift out of the Codex file
(the parity test fails until this is re-run).

Matchers remain supersets because live Codex releases have used both native
and Claude-compatible tool names. A matcher that cannot match is inert.

Run: python3 scripts/gen_codex_hooks.py   (writes the file, prints a diff note)
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_HOOKS_DIR = ROOT / "plugins" / "memhub" / "hooks"
# The Claude file is claude-hooks.json after the multi-host rename (PR #65)
# and hooks.json before it — same content either way. Resolving both keeps
# the generator and its parity test working on every branch of the stack.
CLAUDE_HOOKS = next(p for p in (_HOOKS_DIR / "claude-hooks.json",
                                _HOOKS_DIR / "hooks.json") if p.exists())
CODEX_HOOKS = _HOOKS_DIR / "codex-hooks.json"

_ROOT = '${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}'
_UNIX_DISPATCH = (
    'IN=$(cat); R="' + _ROOT + '"; '
    'P=$(command -v python3 || command -v python); '
    'if [ -n "$P" ] && [ -n "$R" ] '
    '&& [ -f "$R/scripts/codex_hook_bridge.py" ]; then '
    'printf %s "$IN" | "$P" "$R/scripts/codex_hook_bridge.py" dispatch {event}; '
    'fi'
)
_WINDOWS_DISPATCH = (
    'if defined PLUGIN_ROOT '
    '(py -3 "%PLUGIN_ROOT%\\scripts\\codex_hook_bridge.py" dispatch {event}) '
    'else if defined CLAUDE_PLUGIN_ROOT '
    '(py -3 "%CLAUDE_PLUGIN_ROOT%\\scripts\\codex_hook_bridge.py" dispatch {event})'
)
_ALL_TOOLS = "^(Edit|MultiEdit|Write|NotebookEdit|apply_patch|Bash|shell|local_shell)$"
# PostToolUse also carries the PR-link check, which fires on GitHub MCP tool
# calls as well as shell ones. The SERVER segment must name GitHub — the same
# anchor pr_link.py uses — so a tool called `mcp__notes__github_summary` never
# starts the dispatcher. It is deliberately coarse about WHERE "github" sits,
# because the server segment can contain underscores — `github_enterprise`, or any plugin-provided server such as `plugin_github_github`; `pr_link.is_github_mcp_tool` is the precise gate and
# this only decides whether a process starts at all. Only this bundled manifest is widened: the
# compatibility bridge in references/codex-hooks-bridge.json is a file the user
# already trusted in ~/.codex/hooks.json, and widening it would cost a
# re-trust for a detection they can do with /memhub:link-pr.
_POST_TOOLS = ("^(Edit|MultiEdit|Write|NotebookEdit|apply_patch|Bash|shell|"
               "local_shell|mcp__.*[Gg]it[Hh]ub.*__.*)$")


# Claude-only scripts: they read Claude's transcript store, or arm a Claude-only
# loop, and must never mount on Codex, whose capture routes through codex_flush.
#
# This is an ASSERTED list, not a filter: `generate` below builds the Codex
# document from a fixed template and never copies Claude's commands, so nothing
# here is dropped from anything — `tests/codex_hooks_parity_test.py` is the only
# consumer, and it asserts none of these names appears in the generated file.
# The comment used to claim a "generator drop-filter" that does not exist,
# which is the kind of reassurance that makes the next person stop looking.
CLAUDE_ONLY_CAPTURE = (
    "flush_turn.py",
    "flush_session.py",
    "turn_flush_prefilter.py",
    "pr_babysit_trigger.py",
    # The merged PR-lane entry point CALLS pr_babysit_trigger, so it must never
    # become the Codex path either. Codex reaches the link lane through
    # codex_hook_bridge, which already folds its jobs into one context and
    # calls pr_link_trigger directly.
    "pr_post_context.py",
)


def generate(claude_hooks: dict) -> dict:
    """The Codex hooks document derived from the Claude one."""
    source = json.dumps(claude_hooks)
    for required in ("directive_recall.py", "artifact_sync_reminder.py"):
        if required not in source:
            raise ValueError(f"Claude hooks no longer expose {required}")

    def handler(event: str, timeout: int, status: str | None = None) -> dict:
        value = {
            "type": "command",
            "timeout": timeout,
            "command": _UNIX_DISPATCH.replace("{event}", event),
            "commandWindows": _WINDOWS_DISPATCH.replace("{event}", event),
        }
        if status:
            value["statusMessage"] = status
        return value

    return {"hooks": {
        "PreToolUse": [{
            "matcher": _ALL_TOOLS,
            "hooks": [handler(
                "PreToolUse", 8, "MemHub: checking for relevant directives"
            )],
        }],
        "PostToolUse": [{
            "matcher": _POST_TOOLS,
            "hooks": [handler(
                "PostToolUse", 16, "MemHub: checking memory context"
            )],
        }],
        "Stop": [{"hooks": [handler("Stop", 30)]}],
    }}


def main() -> int:
    doc = generate(json.loads(CLAUDE_HOOKS.read_text(encoding="utf-8")))
    text = json.dumps(doc, indent=2) + "\n"
    old = CODEX_HOOKS.read_text(encoding="utf-8") if CODEX_HOOKS.exists() else ""
    CODEX_HOOKS.write_text(text, encoding="utf-8")
    print("codex-hooks.json " + ("unchanged" if old == text else "REGENERATED"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
