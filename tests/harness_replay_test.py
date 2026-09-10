#!/usr/bin/env python3
"""Tests for the replay tooling around the harness: `replay_hooks.py` and
`score_s0.py`. Nothing here reaches a server or a model."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins" / "memhub" / "skills" / "rules-from-sessions" / "scripts"
sys.path.insert(0, str(SKILL))

import replay_hooks as rp  # noqa: E402
import score_s0 as sc  # noqa: E402


def test_a_fire_is_counted_once_per_hook_response():
    """The hook names a fired rule on both channels; the replay must not
    count the label twice (Codex, #191)."""
    out = {"hookSpecificOutput": {"additionalContext": "- **[fetch-first]** text\n"
                                                        "📏 Rule fired: [fetch-first]"},
           "systemMessage": "📏 Rule fired: fetch first\n   XTrace ▸ [fetch-first] text"}
    titles, blocked = rp.fired(out)
    assert titles == ["fetch-first"], titles
    assert not blocked
    titles, blocked = rp.fired({"systemMessage": "Blocked by [gate-a] and [gate-a]"})
    assert titles == ["gate-a"] and blocked
    print("PASS test_a_fire_is_counted_once_per_hook_response")


def test_read_calls_are_replayed():
    assert "Read" in rp.REPLAYED_TOOLS and "Bash" in rp.REPLAYED_TOOLS
    assert rp.EDIT_TOOLS <= rp.REPLAYED_TOOLS
    print("PASS test_read_calls_are_replayed")


def test_a_corpus_rerun_does_not_count_last_runs_moments():
    """The extractor appends; a rerun into the same --out must start clean
    (Codex, #191/#192)."""
    with tempfile.TemporaryDirectory() as td:
        corpus, out = Path(td) / "corpus", Path(td) / "out"
        corpus.mkdir(); out.mkdir()
        (corpus / "s.json").write_text(json.dumps({"session": "s", "repo": "R", "turns": [
            {"n": 1, "user": "hello", "asst": "hi", "tools": [], "results": []}]}))
        stale = out / "s.moments.jsonl"
        stale.write_text(json.dumps({"turn": 1}) + "\n")
        old_drafts = out / "s.drafts.jsonl"
        old_drafts.write_text("{}\n")
        (out / "s.stats.json").write_text("{}")

        class Args:
            env, classify_timeout, pace = "staging", 0, 0

        real = sc.subprocess.run
        seen = []
        sc.subprocess.run = lambda cmd, **kw: (seen.append(cmd),
                                               real([sys.executable, "-c", "pass"], **kw))[1]
        try:
            stats = sc.run_one(sc.plugin_scripts(), corpus / "s.json", out, Args())
        finally:
            sc.subprocess.run = real
        assert not stale.exists() and not old_drafts.exists()
        assert seen and "--turns" in seen[0] and "--budget" not in seen[0]
        assert seen[0][seen[0].index("--out") + 1].endswith("s.moments.jsonl")
        assert stats["moments"] == 0
    print("PASS test_a_corpus_rerun_does_not_count_last_runs_moments")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
