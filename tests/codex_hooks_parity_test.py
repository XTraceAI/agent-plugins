#!/usr/bin/env python3
"""codex-hooks.json must be exactly what the generator produces.

The Codex hooks file is GENERATED from claude-hooks.json (scripts/
gen_codex_hooks.py) so the directive/artifact hooks can never drift between
hosts. This pins the check-in to the generator's output — edit
claude-hooks.json or the generator, re-run it, commit both.

Run: python3 codex_hooks_parity_test.py   (stdlib only)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
BRIDGE_HOOKS = (ROOT / "plugins" / "memhub" / "references" /
                "codex-hooks-bridge.json")

from gen_codex_hooks import (  # noqa: E402
    CLAUDE_HOOKS, CLAUDE_ONLY_CAPTURE, CODEX_HOOKS, generate)


def test_checked_in_file_matches_generator():
    want = generate(json.loads(CLAUDE_HOOKS.read_text(encoding="utf-8")))
    got = json.loads(CODEX_HOOKS.read_text(encoding="utf-8"))
    assert got == want, ("codex-hooks.json is stale — run "
                         "python3 scripts/gen_codex_hooks.py and commit")
    print("PASS test_checked_in_file_matches_generator")


def test_no_claude_only_capture_leaked():
    text = CODEX_HOOKS.read_text(encoding="utf-8")
    # Claude's flush chain reads Claude transcripts — it must never mount on
    # Codex, where sessions are rollout files read by codex_flush.
    # Same constant the generator filters on — the guard and the filter
    # cannot disagree about which scripts are Claude-only.
    for forbidden in CLAUDE_ONLY_CAPTURE:
        assert forbidden not in text, forbidden
    assert "codex_hook_bridge.py" in text
    print("PASS test_no_claude_only_capture_leaked")


def test_user_bridge_covers_live_codex_capabilities():
    bridge = json.loads(BRIDGE_HOOKS.read_text(encoding="utf-8"))["hooks"]
    assert set(bridge) == {"PreToolUse", "PostToolUse", "Stop"}
    assert all(len(groups) == 1 for groups in bridge.values())
    text = json.dumps(bridge)
    for action in ("dispatch PreToolUse", "dispatch PostToolUse",
                   "dispatch Stop"):
        assert action in text, action
    assert text.count("memhub_hook_bridge.py") == 6  # Unix + Windows, 3 handlers
    print("PASS test_user_bridge_covers_live_codex_capabilities")


def test_bundled_hooks_require_only_three_approvals():
    hooks = json.loads(CODEX_HOOKS.read_text(encoding="utf-8"))["hooks"]
    assert set(hooks) == {"PreToolUse", "PostToolUse", "Stop"}
    handlers = [handler for groups in hooks.values() for group in groups
                for handler in group["hooks"]]
    assert len(handlers) == 3
    assert all("codex_hook_bridge.py" in handler["command"]
               for handler in handlers)
    print("PASS test_bundled_hooks_require_only_three_approvals")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
