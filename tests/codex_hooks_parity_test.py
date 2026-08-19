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

from gen_codex_hooks import CLAUDE_HOOKS, CODEX_HOOKS, generate  # noqa: E402


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
    for forbidden in ("flush_turn.py", "flush_session.py",
                      "turn_flush_prefilter.py", "pr_babysit_trigger.py"):
        assert forbidden not in text, forbidden
    assert "codex_flush.py" in text
    print("PASS test_no_claude_only_capture_leaked")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
