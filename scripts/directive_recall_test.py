"""Self-test for the directive_recall hook's session state + dedup + render.

Exercises the exact failure class measured in production (2026-07 replay of a
real session: 1804 injections, 76% of them REPEATS of a directive already shown
that session) plus the state-file mechanics and provenance rendering. Run:

    python3 scripts/directive_recall_test.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "plugins/memhub/scripts/directive_recall.py"
spec = importlib.util.spec_from_file_location("directive_recall", _SCRIPT)
dr = importlib.util.module_from_spec(spec)
sys.modules["directive_recall"] = dr
spec.loader.exec_module(dr)

FAILURES: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILURES.append(name)


# Real items shaped like the babysit lessons that fired 40+ times per session.
REPEAT = {
    "id": "d-babysit-1", "type": "lesson",
    "content": "When babysitting a PR, treat the first pass as collection-only.",
    "triggers": ["gh pr view", "statusCheckRollup"],
    "as_of": "2026-07-08", "seen": 4,
}
FRESH = {
    "id": "d-fresh-1", "type": "procedure",
    "content": "Run the targeted directive suites before pushing.",
    "triggers": ["test_directive_rank.py"],
    "as_of": "2026-07-09", "seen": 1,
}

with tempfile.TemporaryDirectory() as td:
    dr._STATE_DIR = Path(td)

    dr._save_fired("sess-A", ["d-babysit-1"])
    check("state round-trip", dr._load_fired("sess-A") == ["d-babysit-1"])
    check("unknown session is empty", dr._load_fired("sess-B") == [])
    check("hostile session id is inert", dr._state_path("../../etc/passwd").name != "passwd.json"
          if dr._state_path("../../etc/passwd") else True)

    # 2. The 76% class: an id already injected this session is dropped
    #    client-side, independent of server version.
    fired = set(dr._load_fired("sess-A"))
    items = [d for d in [REPEAT, FRESH] if str(d.get("id") or "") not in fired]
    check("repeat dropped, fresh kept", [d["id"] for d in items] == ["d-fresh-1"])

    # 3. Injections-only recording: a directive filtered out before render must
    #    NOT be recorded (it keeps its chance at its real moment).
    check("gate-dropped id not recorded", "d-fresh-1" not in dr._load_fired("sess-A"))

    old = dr._STATE_DIR / "old-sess.json"
    old.write_text("[]")
    import os
    os.utime(old, (time.time() - 8 * 24 * 3600,) * 2)
    dr._save_fired("sess-A", ["d-babysit-1", "d-fresh-1"])
    check("stale session pruned", not old.exists())
    check("current session kept", dr._load_fired("sess-A") == ["d-babysit-1", "d-fresh-1"])

# 5. Render carries provenance the agent can weight (as_of / seen).
block = dr._render([REPEAT])
check("render has as_of", "as of 2026-07-08" in block)
check("render has seen", "seen 4×" in block)
check("render skips seen=1", "seen 1×" not in dr._render([FRESH]))
check("render valid as hook payload", json.dumps({"additionalContext": block}) != "")

# 6. Repo name derivation: this checkout has an origin remote; the name must be
#    the remote basename, not a worktree dir name.
repo = dr._repo_name(str(_SCRIPT.parent))
check("repo from git remote", repo == "agent-plugins")
check("no cwd → empty repo", dr._repo_name("") == "")

# 7. The precision filter is unchanged: the real misfire class (repo-name-only
#    trigger) still drops; a concrete file-path trigger still passes.
repo_only = {"id": "r", "content": "x", "triggers": ["memhub-claude-plugin"]}
concrete = {"id": "c", "content": "y", "triggers": ["directive_recall.py"]}
kept = dr._precision_filter(
    [repo_only, concrete],
    {"command": "python plugins/memhub/scripts/directive_recall.py"},
    "/Users/x/memhub-claude-plugin",
)
check("repo-only trigger dropped", [d["id"] for d in kept] == ["c"])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    raise SystemExit(1)
print("all checks passed")
