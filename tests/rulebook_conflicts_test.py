"""Self-test for rulebook_conflicts.py — the pre-filing collision check the
rulebook skills run. Pure functions + the CLI, offline. Run: python3 rulebook_conflicts_test.py"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "plugins", "memhub", "scripts")
sys.path.insert(0, SCRIPTS)
import rulebook_conflicts as rc  # noqa: E402

FAILS = 0


def check(cond, msg):
    global FAILS
    if not cond:
        FAILS += 1
        print("FAIL:", msg)


EXISTING = [  # list_rules shape: all statuses, no engine blocks
    {"rule_id": "a1", "title": "Pre-push audit", "status": "active", "statement": "Read the diff before pushing."},
    {"rule_id": "d1", "title": "no-force-push", "status": "draft", "statement": "Never bare --force."},
    {"rule_id": "x1", "title": "pre push audit", "status": "deprecated", "statement": "old twin"},
    {"rule_id": "s1", "title": "public-sdk-posture", "status": "active", "statement": "Design for an external consumer."},
]
ACTIVE = [  # hook view: engine blocks, active only, no status field
    {"rule_id": "a1", "title": "Pre-push audit", "delivery": "agent_hook",
     "matcher": {"event": "bash", "command_rx": r"\bgit\s+push\b", "warn_once_per": "session"}},
    {"rule_id": "a2", "title": "consumer-grep", "delivery": "anchor_recall",
     "anchors": ["ContextBusConfig", "formats.py"]},
    {"rule_id": "s1", "title": "public-sdk-posture", "delivery": "session_context"},
]


def main() -> int:
    # 1. same_title across statuses, case/punctuation-insensitive, retired rows ignored
    r = rc.find_conflicts([{"title": "pre-push AUDIT", "delivery": "agent_hook"}], EXISTING, None)
    hits = r["candidates"][0]["hits"]
    check([h["rule_id"] for h in hits] == ["a1"], f"same_title should hit a1 only (not deprecated x1): {hits}")
    check(hits[0]["reasons"] == ["same_title"], hits)
    check(r["active_book"] == "unavailable", r["active_book"])

    # 2. same_matcher: same event + same primary regex (whitespace-normalised), different title
    cand = {"title": "Push needs a diff read", "delivery": "agent_hook",
            "matcher": {"event": "bash", "command_rx": r"\bgit\s+push\b "}}
    r = rc.find_conflicts([cand], EXISTING, ACTIVE)
    hits = r["candidates"][0]["hits"]
    check(len(hits) == 1 and hits[0]["rule_id"] == "a1" and hits[0]["reasons"] == ["same_matcher"], hits)
    check(hits[0]["status"] == "active", hits)

    # 3. a different event with the same pattern is NOT a matcher collision
    cand["matcher"] = {"event": "edit", "path_rx": r"\bgit\s+push\b"}
    r = rc.find_conflicts([cand], EXISTING, ACTIVE)
    check(r["candidates"][0]["hits"] == [], r["candidates"][0]["hits"])

    # 4. anchors_overlap lists the shared identifiers, case-insensitive
    cand = {"title": "config via preset", "delivery": "anchor_recall", "anchors": ["contextbusconfig", "preset("]}
    r = rc.find_conflicts([cand], EXISTING, ACTIVE)
    hits = r["candidates"][0]["hits"]
    check(len(hits) == 1 and hits[0]["rule_id"] == "a2" and hits[0]["reasons"] == ["anchors_overlap"]
          and hits[0]["anchors_shared"] == ["contextbusconfig"], hits)

    # 5. a candidate can hit the same rule for two reasons; reasons accumulate
    cand = {"title": "Pre push audit", "delivery": "agent_hook",
            "matcher": {"event": "bash", "command_rx": r"\bgit\s+push\b"}}
    r = rc.find_conflicts([cand], EXISTING, ACTIVE)
    hits = r["candidates"][0]["hits"]
    check(len(hits) == 1 and sorted(hits[0]["reasons"]) == ["same_matcher", "same_title"], hits)

    # 6. judge_by_statement = every live existing rule with no hit, with its statement
    unmatched = {u["rule_id"] for u in r["judge_by_statement"]}
    check(unmatched == {"d1", "s1"}, unmatched)
    check(all("statement" in u for u in r["judge_by_statement"]), r["judge_by_statement"])

    # 7. ordering rules key on required_command_rx
    act = [{"rule_id": "o1", "title": "tests before push", "delivery": "agent_hook",
            "ordering": {"required_command_rx": r"pytest", "gated_command_rx": r"git push"}}]
    cand = {"title": "run tests first", "delivery": "agent_hook",
            "ordering": {"required_command_rx": "pytest", "gated_command_rx": "git push"}}
    r = rc.find_conflicts([cand], [], act)
    check(r["candidates"][0]["hits"] and r["candidates"][0]["hits"][0]["reasons"] == ["same_matcher"], r)

    # 8. CLI: --book file + --existing file + stdin candidates; JSON on stdout, summary on stderr, exit 0
    with tempfile.TemporaryDirectory() as td:
        book = os.path.join(td, "book.json")
        ex = os.path.join(td, "existing.json")
        with open(book, "w", encoding="utf-8") as f:
            json.dump({"etag": "x", "fetched_at": 0, "rules": ACTIVE}, f)
        with open(ex, "w", encoding="utf-8") as f:
            json.dump({"rules": EXISTING, "count": len(EXISTING)}, f)
        cands = json.dumps([{"title": "no force push", "delivery": "agent_hook",
                             "matcher": {"event": "bash", "command_rx": r"--force"}}])
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "rulebook_conflicts.py"),
                            "--candidates", "-", "--existing", ex, "--book", book],
                           input=cands, capture_output=True, text=True,
                           env={**os.environ, "MEMHUB_RULEBOOK_BASE": td})
        check(p.returncode == 0, p.stderr)
        out = json.loads(p.stdout)
        check(out["candidates"][0]["hits"][0]["rule_id"] == "d1"
              and out["candidates"][0]["hits"][0]["reasons"] == ["same_title"], out)
        check(out["active_book"] == "checked", out["active_book"])
        check("CONFLICTS" in p.stderr and "no-force-push [draft]" in p.stderr, p.stderr)

        # 8b. --book accepts a quoted glob (the documented usage)
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "rulebook_conflicts.py"),
                            "--candidates", "-", "--book", os.path.join(td, "bo*.json")],
                           input=cands, capture_output=True, text=True,
                           env={**os.environ, "MEMHUB_RULEBOOK_BASE": td})
        check(p.returncode == 0 and json.loads(p.stdout)["active_book"] == "checked", p.stderr)

        # 8c. a multi-match glob names the file it chose and the ones it skipped
        with open(os.path.join(td, "book2.json"), "w", encoding="utf-8") as f:
            json.dump({"rules": []}, f)
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "rulebook_conflicts.py"),
                            "--candidates", "-", "--book", os.path.join(td, "book*.json")],
                           input=cands, capture_output=True, text=True,
                           env={**os.environ, "MEMHUB_RULEBOOK_BASE": td})
        check(p.returncode == 0 and "matched 2 files" in p.stderr and "book2.json" in p.stderr, p.stderr)

        # 9. unreadable --book degrades to "unavailable", still exit 0
        p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "rulebook_conflicts.py"),
                            "--candidates", "-", "--book", os.path.join(td, "missing.json")],
                           input=cands, capture_output=True, text=True,
                           env={**os.environ, "MEMHUB_RULEBOOK_BASE": td})
        check(p.returncode == 0 and json.loads(p.stdout)["active_book"] == "unavailable", p.stderr)

    if FAILS:
        print(f"{FAILS} rulebook_conflicts checks failed")
        return 1
    print("all rulebook_conflicts checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
