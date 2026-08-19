#!/usr/bin/env python3
"""Generate plugins/memhub/hooks/codex-hooks.json from claude-hooks.json.

Codex clones Claude's hook contract (same hooks.json shape, same
``${CLAUDE_PLUGIN_ROOT}``), so the directive and artifact-sync hooks carry
over verbatim EXCEPT for tool names in matchers — and capture is rewired to
``codex_flush.py`` (Codex sessions are rollout files, not Claude transcripts,
so Claude's flush chain would read the wrong store).

Generated, checked in, and pinned by tests/codex_hooks_parity_test.py — the
generator is the single place the Claude→Codex delta lives, so a directive
hook edited in claude-hooks.json cannot silently drift out of the Codex file
(the parity test fails until this is re-run).

Matchers are SUPERSETS during the verification window: Codex's docs name its
tools shell/apply_patch, but Clay's production codex hooks match "Bash" —
suggesting a Claude-compat alias at hook-match time. Until a live session
pins it (Spike B), match both vocabularies; a matcher that can't match is
inert, never harmful.

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

# Claude matcher → Codex superset matcher (see module docstring).
MATCHER_MAP = {
    "Bash": "Bash|shell|local_shell",
    "^(Edit|MultiEdit|Write|NotebookEdit)$":
        "^(Edit|MultiEdit|Write|NotebookEdit|apply_patch)$",
    "^(Edit|MultiEdit|Write|NotebookEdit|Bash)$":
        "^(Edit|MultiEdit|Write|NotebookEdit|apply_patch|shell|local_shell|Bash)$",
}

_FLUSH = ('IN=$(cat); if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ]; then printf %s "$IN" '
          '| python3 "${CLAUDE_PLUGIN_ROOT}/scripts/codex_flush.py" {event}; fi')


def generate(claude_hooks: dict) -> dict:
    """The Codex hooks document derived from the Claude one."""
    src = claude_hooks["hooks"]
    out: dict = {"hooks": {}}

    def _remap(entries: list) -> list:
        remapped = []
        for entry in entries:
            e = json.loads(json.dumps(entry))  # deep copy
            if "matcher" in e:
                e["matcher"] = MATCHER_MAP.get(e["matcher"], e["matcher"])
            remapped.append(e)
        return remapped

    # Directive recall (PreToolUse) + reactive check / artifact-sync
    # (PostToolUse) carry over — same scripts, remapped matchers. Claude's
    # PostToolUse flush + babysit entries are dropped: capture routes through
    # codex_flush below, and pr-babysit's loop is unverified on Codex.
    out["hooks"]["PreToolUse"] = _remap(src["PreToolUse"])
    post = []
    for entry in src["PostToolUse"]:
        cmd = entry["hooks"][0]["command"]
        if "flush_session.py" in cmd or "pr_babysit_trigger.py" in cmd:
            continue
        post.append(entry)
    post = _remap(post)
    # Milestone flush: PostToolUse on shell-ish tools; codex_flush's own gate
    # keeps it to git commit / gh pr commands, so no matcher-level filtering.
    post.append({
        "matcher": "Bash|shell|local_shell",
        "hooks": [{
            "type": "command", "async": True, "timeout": 300,
            "statusMessage": "MemHub: flushing session memory",
            "command": _FLUSH.replace("{event}", "PostToolUse"),
        }],
    })
    out["hooks"]["PostToolUse"] = post

    # Turn boundary: Codex Stop ≈ Claude Stop; codex_flush gates on rollout
    # growth so an idle Stop costs one stat().
    out["hooks"]["Stop"] = [{
        "hooks": [{
            "type": "command", "async": True, "timeout": 300,
            "command": _FLUSH.replace("{event}", "Stop"),
        }],
    }]
    # No SessionEnd in Codex's event list; Stop + milestone + idempotent
    # re-import carry correctness (same tiering as Cursor).
    return out


def main() -> int:
    doc = generate(json.loads(CLAUDE_HOOKS.read_text(encoding="utf-8")))
    text = json.dumps(doc, indent=2) + "\n"
    old = CODEX_HOOKS.read_text(encoding="utf-8") if CODEX_HOOKS.exists() else ""
    CODEX_HOOKS.write_text(text, encoding="utf-8")
    print("codex-hooks.json " + ("unchanged" if old == text else "REGENERATED"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
