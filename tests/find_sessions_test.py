"""find_sessions.py — ranking the sessions that plausibly wrote a PR's code.

Two properties matter more than the ranking itself:

* **Nothing but evidence leaves the script.** Its output goes straight into
  model context, and a session transcript can exceed a million tokens. The
  fixtures below carry a distinctive sentence of prose; if it ever appears in
  stdout, the caps and the paths-only contract have been broken.
* **Repo-relative matching.** A session's checkout is a different absolute
  path from the PR's file list — often a different worktree — and that
  mismatch is the whole reason this feature exists.

Runs against a synthesized `$HOME`; no network, no real transcripts.

Run: python3 tests/find_sessions_test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "plugins" / "memhub" / "skills" / "find-contributing-sessions"
          / "scripts" / "find_sessions.py")
SCRIPTS = ROOT / "plugins" / "memhub" / "scripts"

# If this sentence ever reaches stdout, the script is echoing transcript text.
PROSE = "Zebras drafted the quarterly onboarding memorandum unaided."

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok ' if condition else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def _record(uuid: str, branch: str | None, cwd: str, blocks: list) -> dict:
    record = {"type": "assistant", "uuid": uuid, "cwd": cwd,
              "timestamp": "2026-09-01T10:00:00Z",
              "message": {"role": "assistant", "content": blocks}}
    if branch:
        record["gitBranch"] = branch
    return record


def claude_session(home: Path, sid: str, cwd: str, *, branch: str | None,
                   edits: list[str] = (), commands: list[str] = (),
                   results: list[str] = (), age_s: float = 0.0) -> Path:
    project = home / ".claude" / "projects" / cwd.replace("/", "-")
    project.mkdir(parents=True, exist_ok=True)
    rows = [_record(f"{sid}-0", branch, cwd, [{"type": "text", "text": PROSE}])]
    n = 1
    for path in edits:
        rows.append(_record(f"{sid}-{n}", branch, cwd, [{
            "type": "tool_use", "id": f"t{n}", "name": "Edit",
            "input": {"file_path": path, "new_string": PROSE}}]))
        n += 1
    for command in commands:
        rows.append(_record(f"{sid}-{n}", branch, cwd, [{
            "type": "tool_use", "id": f"t{n}", "name": "Bash",
            "input": {"command": command}}]))
        n += 1
    for text in results:
        rows.append({"type": "user", "uuid": f"{sid}-{n}", "cwd": cwd,
                     "timestamp": "2026-09-01T10:00:00Z",
                     "message": {"role": "user", "content": [{
                         "type": "tool_result", "tool_use_id": "t1",
                         "content": text}]}})
        n += 1
    path = project / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def run(home: Path, files: list[str], *args: str) -> tuple[int, str, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                     encoding="utf-8") as handle:
        handle.write("\n".join(files) + "\n")
        listing = handle.name
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--files-from", listing, *args],
            capture_output=True, text=True, encoding="utf-8", timeout=120,
            env={**os.environ, "HOME": str(home), "USERPROFILE": str(home),
                 "PYTHONUTF8": "1", "MEMHUB_PLUGIN_SCRIPTS": str(SCRIPTS)})
        return proc.returncode, proc.stdout, proc.stderr
    finally:
        os.unlink(listing)


PR_FILES = ["app/x.py", "app/y.py", "docs/readme.md"]


def test_the_session_that_edited_the_files_on_the_head_branch_wins():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        # An absolute path from a DIFFERENT worktree than the PR's: the suffix
        # is what must match.
        wt = str(Path(td) / "checkouts" / "repo-feat")
        claude_session(home, "aaaa-1111", wt, branch="feat/x",
                       edits=[f"{wt}/app/x.py", f"{wt}/app/y.py"])
        claude_session(home, "bbbb-2222", str(Path(td) / "checkouts" / "repo"),
                       branch="main", edits=[f"{td}/checkouts/repo/other/z.py"])
        rc, out, err = run(home, PR_FILES, "--branch", "feat/x", "--base", "main",
                           "--host", "claude")
        rows = json.loads(out) if out.strip() else []
        check("exit 0 with JSON", rc == 0 and isinstance(rows, list), out + err)
        check("the head-branch editor is first",
              rows and rows[0]["conversation_id"] == "aaaa-1111", out)
        check("its evidence names the PR files it touched, repo-relative",
              rows and sorted(rows[0]["evidence"]["files"]) == ["app/x.py", "app/y.py"],
              out)
        check("…and the branch match",
              rows and rows[0]["evidence"]["branch_match"] is True, out)
        check("the main-only session that touched nothing scores out",
              all(r["conversation_id"] != "bbbb-2222" for r in rows), out)


def test_a_commit_sha_outscores_everything():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        wt = str(Path(td) / "repo")
        # Two PR files + head branch = 4 + 3 = 7.
        claude_session(home, "aaaa-1111", wt, branch="feat/x",
                       edits=[f"{wt}/app/x.py", f"{wt}/app/y.py"])
        # A sha and nothing else = 5, but the sha is proof, so it must appear.
        claude_session(home, "cccc-3333", wt, branch="other",
                       commands=["git log --oneline"],
                       results=["a1b2c3d4 fix the thing"])
        rc, out, _ = run(home, PR_FILES, "--branch", "feat/x",
                         "--sha", "a1b2c3d4e5f6a7b8", "--host", "claude")
        rows = json.loads(out)
        by_id = {r["conversation_id"]: r for r in rows}
        check("the sha session is a candidate at all", "cccc-3333" in by_id, out)
        check("…and its evidence records the sha",
              by_id.get("cccc-3333", {}).get("evidence", {}).get("shas") == ["a1b2c3d4"],
              out)
        check("a sha alone scores 5",
              by_id.get("cccc-3333", {}).get("score") == 5, out)


def test_a_session_only_ever_on_the_base_branch_is_pushed_below_zero():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        wt = str(Path(td) / "repo")
        claude_session(home, "dddd-4444", wt, branch="main",
                       commands=["git switch main"])
        rc, out, _ = run(home, PR_FILES, "--branch", "feat/x", "--base", "main",
                         "--host", "claude")
        check("it is not offered as a candidate", json.loads(out) == [], out)


def test_no_transcript_text_ever_reaches_stdout():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        wt = str(Path(td) / "repo")
        claude_session(home, "aaaa-1111", wt, branch="feat/x",
                       edits=[f"{wt}/app/x.py"],
                       commands=[f"echo '{PROSE}'"],
                       results=[PROSE])
        rc, out, err = run(home, PR_FILES, "--branch", "feat/x", "--host", "claude")
        check("there is a candidate to report", json.loads(out), out)
        check("the fixture's prose appears nowhere in stdout", PROSE not in out, out)
        check("…nor in stderr", PROSE not in err, err)


def test_the_caps_hold_on_an_enormous_session():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        wt = str(Path(td) / "repo")
        project = home / ".claude" / "projects" / wt.replace("/", "-")
        project.mkdir(parents=True)
        rows = [_record(f"big-{i}", "feat/x", wt, [{
            "type": "tool_use", "id": f"t{i}", "name": "Edit",
            "input": {"file_path": f"{wt}/app/x.py", "new_string": PROSE}}])
            for i in range(50_000)]
        (project / "eeee-5555.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        started = time.time()
        rc, out, err = run(home, PR_FILES, "--branch", "feat/x", "--host", "claude")
        elapsed = time.time() - started
        check("a 50k-record session still exits 0", rc == 0, err[-500:])
        check("…and is bounded, not unbounded", elapsed < 90, f"{elapsed:.1f}s")
        check("…and reports it once, with the file it edited",
              json.loads(out)[0]["evidence"]["files"] == ["app/x.py"], out[:400])


def test_bad_input_is_a_clean_error():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        home.mkdir()
        rc, out, err = run(home, [], "--branch", "feat/x")
        check("an empty file list → exit 2, no traceback",
              rc == 2 and "Traceback" not in err, err)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--files-from", "/nope/nope.txt"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": str(home), "PYTHONUTF8": "1",
                 "MEMHUB_PLUGIN_SCRIPTS": str(SCRIPTS)})
        check("a missing file list → exit 2, no traceback",
              proc.returncode == 2 and "Traceback" not in proc.stderr, proc.stderr)


if __name__ == "__main__":
    print("find_sessions")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
    else:
        print("all find_sessions checks passed")
    sys.exit(1 if failures else 0)
