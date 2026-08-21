"""Self-test for markdown artifact capture (collector + flush rule).

Encodes the 2026-08-21 backtest that justified the feature: over 31 sessions,
67 distinct .md writes split into Claude auto-memory (32), scratch (15), real
specs (7, median 25 KB), PR-body drafts (5), READMEs (3), skill docs (3),
CLAUDE.md (2). The rule must admit the specs and reject every other class.

Run: python3 md_capture_test.py   (stdlib only; the flush's network path is
not exercised — derive_name/derive_type and the state contract are).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import md_capture as mc  # noqa: E402

HOOK = SCRIPTS / "md_capture.py"
FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS += 1
        print(f"  FAIL {msg}")


try:
    import md_capture_flush as f
    have_flush = True
except ModuleNotFoundError:
    have_flush = False
    print("note: mcp SDK not installed — flush-side checks skipped (run via uv run --with 'mcp<2')")

BIG = 9_000   # the smallest real spec in the backtest was 9,156 bytes
SMALL = 5_800 # the largest PR-body draft was 5,760 bytes

# ---- the classifier, one row per backtest category ------------------------
print("is_candidate — backtest categories")
cases = [
    # (path, size, text, expect, label)
    ("/Users/me/xtrace/MemHub-Backend/docs/specs/serving-ledger-spec.md", BIG, "# Serving ledger\n", True, "real spec, 25KB-class → capture"),
    ("/Users/me/xtrace/xmem/STATE_REVALIDATION_SPEC_V1.md", BIG, "", True, "spec at repo root (no docs/) → capture"),
    ("/Users/me/.claude/projects/-Users-me-xtrace-xmem/memory/foo.md", BIG, "", False, "Claude auto-memory → veto"),
    ("/Users/me/.claude/projects/x/memory/MEMORY.md", BIG, "", False, "MEMORY.md → veto"),
    ("/private/tmp/claude-501/x/scratchpad/RUBRIC.md", BIG, "", False, "scratchpad → veto"),
    ("/tmp/notes.md", BIG, "", False, "/tmp → veto"),
    ("/Users/me/memory-hub/dead-code-sweep-pr.md", SMALL, "", False, "PR body draft, 5KB → below floor"),
    ("/Users/me/repo/README.md", SMALL, "", False, "README edit, 4KB → below floor"),
    ("/Users/me/repo/CLAUDE.md", BIG, "", False, "CLAUDE.md → veto name"),
    ("/Users/me/repo/AGENTS.md", BIG, "", False, "AGENTS.md → veto name"),
    ("/Users/me/repo/plugins/x/skills/onboard/SKILL.md", SMALL, "", False, "skill doc, small → below floor"),
    ("/Users/me/repo/notes/design.md", SMALL, "---\nmemhub: artifact\ntitle: Retry design\n---\n# Retry", True, "small but frontmatter opt-in → capture"),
    ("/Users/me/repo/notes/design.md", SMALL, "---\ntitle: Retry design\n---\n", False, "frontmatter WITHOUT memhub: artifact → below floor"),
    ("/Users/me/repo/src/thing.py", BIG, "", False, "not markdown"),
    ("/Users/me/repo/node_modules/pkg/README.md", BIG, "", False, "node_modules → veto"),
]
for path, size, text, expect, label in cases:
    ok, why = mc.is_candidate(Path(path), size=size, text=text or None)
    check(ok == expect, f"{label}  [{why}]")

# ---- frontmatter scanner ---------------------------------------------------
print("frontmatter")
check(mc.frontmatter("---\na: 1\n---\nbody") == "\na: 1", "extracts block")
check(mc.frontmatter("# no fm") == "", "no block → empty")
check(mc.frontmatter("---\nunterminated") == "", "unterminated → empty")

# ---- collector: records path once per session, never fails ----------------
print("collector (subprocess, real hook contract)")
with tempfile.TemporaryDirectory() as td:
    sid = "sess-md-capture-test"
    env = {**os.environ, "TMPDIR": td}
    # Python's tempfile honours TMPDIR; the state file lands in td.
    def run(payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)
    # NOT under td: macOS temp dirs live in /var/folders, which the rule
    # vetoes on purpose. The collector doesn't need the file to exist.
    spec = "/Users/me/repo/docs/x-spec.md"
    for _ in range(3):
        r = run({"session_id": sid, "cwd": td, "tool_name": "Edit", "tool_input": {"file_path": spec}})
        check(r.returncode == 0 and r.stdout == "", "edit → exit 0, silent")
    r = run({"session_id": sid, "cwd": td, "tool_name": "Write", "tool_input": {"file_path": str(Path(td)/"scratchpad"/"n.md")}})
    check(r.returncode == 0, "scratch write → exit 0")
    r = run({"session_id": sid, "tool_name": "Write", "tool_input": {"file_path": "/Users/me/.claude/x.md"}})
    check(r.returncode == 0, "veto write → exit 0")
    r = run({"session_id": sid, "tool_name": "Write", "tool_input": {"file_path": str(Path(td)/"a.py")}})
    check(r.returncode == 0, "non-md → exit 0")
    r = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True, env=env)
    check(r.returncode == 0 and r.stdout == "", "garbage stdin → exit 0, silent")
    import tempfile as _t
    _t.tempdir = None
    os.environ["TMPDIR"] = td
    state = mc.load_state(sid)
    check(state["dirty"] == [spec], f"state holds the spec exactly once: {state['dirty']}")

    if have_flush:
        # race: a path added to `dirty` while a flush is in flight must survive the write-back
        sid2 = "sess-md-flush-race"
        os.environ["TMPDIR"] = td
        mc.save_state(sid2, {"dirty": ["/Users/me/repo/a.md"], "saved": {}})
        mc.save_state(sid2, {"dirty": ["/Users/me/repo/a.md", "/Users/me/repo/b.md"], "saved": {}})  # collector appended b mid-flight
        f._persist(sid2, processed={"/Users/me/repo/a.md"}, saved={"/Users/me/repo/a.md": "abc"})
        st = mc.load_state(sid2)
        check(st["dirty"] == ["/Users/me/repo/b.md"] and st["saved"] == {"/Users/me/repo/a.md": "abc"},
              f"flush write-back merges, does not clobber: {st}")

# ---- flush-side derivations (no network) ----------------------------------
print("flush derivations")
if have_flush:
    p = Path("/r/docs/rulebook-detector-spec.md")
    check(f.derive_name(p, "---\ntitle: Rulebook detectors\n---\n# Other") == "Rulebook detectors", "name: frontmatter title wins")
    check(f.derive_name(p, "# Rulebook v2 — detectors\n", Path("/r")) == "Rulebook v2 — detectors (docs/rulebook-detector-spec.md)", "name: H1 + relpath (inferred names are qualified)")
    check(f.derive_name(p, "no heading", Path("/r")) == "rulebook detector spec (docs/rulebook-detector-spec.md)", "name: stem + relpath")
    check(f.derive_name(Path("/elsewhere/x.md"), "# T", Path("/r")) == "T (x.md)", "name: outside root → basename only")

    check(f.derive_type(p, "", "Rulebook detectors") == "spec", "type: 'spec' in path → spec")
    check(f.derive_type(Path("/r/notes.md"), "---\ntype: runbook\n---", "x") == "runbook", "type: frontmatter wins")
    check(f.derive_type(Path("/r/retry-design.md"), "", "Retry") == "design_doc", "type: design → design_doc")
    check(f.derive_type(Path("/r/notes.md"), "", "Notes") == "document", "type: default document")

print()
print("FAILED" if FAILS else "ALL PASSED", f"({FAILS} failures)")
sys.exit(1 if FAILS else 0)
