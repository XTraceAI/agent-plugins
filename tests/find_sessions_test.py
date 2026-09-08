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
                   results: list[str] = (), age_s: float = 0.0,
                   path_key: str = "file_path", tool: str = "Edit",
                   result_is_error: bool = False) -> Path:
    project = home / ".claude" / "projects" / cwd.replace("/", "-")
    project.mkdir(parents=True, exist_ok=True)
    rows = [_record(f"{sid}-0", branch, cwd, [{"type": "text", "text": PROSE}])]
    n = 1
    for path in edits:
        rows.append(_record(f"{sid}-{n}", branch, cwd, [{
            "type": "tool_use", "id": f"t{n}", "name": tool,
            "input": {path_key: path, "new_string": PROSE}}]))
        n += 1
    for command in commands:
        rows.append(_record(f"{sid}-{n}", branch, cwd, [{
            "type": "tool_use", "id": f"t{n}", "name": "Bash",
            "input": {"command": command}}]))
        n += 1
    for text in results:
        block = {"type": "tool_result", "tool_use_id": "t1", "content": text}
        if result_is_error:
            block["is_error"] = True
        rows.append({"type": "user", "uuid": f"{sid}-{n}", "cwd": cwd,
                     "timestamp": "2026-09-01T10:00:00Z",
                     "message": {"role": "user", "content": [block]}})
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


def test_edit_paths_are_read_under_every_host_s_spelling():
    """The readers pass NATIVE tool arguments through unchanged — Cursor's own
    `args` go straight into `input` (readers/cursor.py) — so reading only
    `file_path` gave Cursor sessions no file evidence at all, and genuine
    contributors were crowded out by weaker candidates (Codex, PR #182)."""
    for key, tool in (("file_path", "Edit"), ("path", "Write"),
                      ("notebook_path", "NotebookEdit")):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            wt = str(Path(td) / "repo")
            claude_session(home, "editor", wt, branch="unrelated",
                           edits=[f"{wt}/app/x.py", f"{wt}/app/y.py"],
                           path_key=key, tool=tool)
            rc, out, err = run(home, PR_FILES, "--branch", "feat/x", "--host", "claude")
            rows = json.loads(out)
            check(f"an edit under {key!r} counts as file evidence",
                  rows and sorted(rows[0]["evidence"]["files"])
                  == ["app/x.py", "app/y.py"], out[:200])


def test_switch_creates_a_branch_too():
    """`git switch -c` is how the PR's branch is usually made, and Codex and
    Cursor have no top-level `gitBranch` to fall back on — so missing it cost
    those sessions the branch signal entirely (Codex review, PR #182)."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        wt = str(Path(td) / "repo")
        # `branch=None` means no gitBranch record: the Codex/Cursor shape.
        # Every spelling git accepts for the same thing.
        for index, command in enumerate(("git switch -c feat/x",
                                         "git switch -cfeat/x",
                                         "git switch --create=feat/x",
                                         "git checkout -bfeat/x",
                                         # …and through the usual wrappers.
                                         "env FOO=1 git switch -c feat/x",
                                         "sudo -u ci git switch -c feat/x")):
            claude_session(home, f"switcher{index}", wt, branch=None,
                           edits=[f"{wt}/app/x.py"], commands=[command])
        rc, out, _err = run(home, PR_FILES, "--branch", "feat/x", "--host", "claude")
        rows = {r["conversation_id"]: r for r in json.loads(out)}
        for index, command in enumerate(("-c feat/x", "-cfeat/x",
                                         "--create=feat/x", "-bfeat/x",
                                         "env FOO=1 …", "sudo -u ci …")):
            row = rows.get(f"switcher{index}", {})
            check(f"the branch is picked up from `{command}`",
                  row.get("evidence", {}).get("branch_match") is True, out[:240])
            check(f"…so `{command}` scores the file AND the branch (2 + 3)",
                  row.get("score") == 5, out[:240])


def test_a_detached_checkout_is_not_being_on_the_branch():
    """`git switch --detach feat/x` puts HEAD AT that commit without moving
    onto the branch — a reviewer inspecting the PR's tip was scoring the three
    branch points for it (Codex review, PR #182)."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        wt = str(Path(td) / "repo")
        claude_session(home, "peeker", wt, branch=None,
                       commands=["git switch --detach feat/x"])
        claude_session(home, "worker", wt, branch=None,
                       commands=["git switch feat/x"])
        rc, out, _err = run(home, PR_FILES, "--branch", "feat/x", "--host", "claude")
        ids = [r["conversation_id"] for r in json.loads(out)]
        check("a detached checkout scores no branch point", "peeker" not in ids, out[:200])
        check("…while actually switching to it does", "worker" in ids, out[:200])


def test_a_commit_message_is_not_a_list_of_edited_paths():
    """`git commit -m "docs: update README.md"` handed every word of the
    message to the path matcher, so a session that committed unrelated work
    scored file evidence for a PR file it never touched (Codex, PR #182)."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        wt = str(Path(td) / "repo")
        claude_session(home, "talker", wt, branch="unrelated",
                       commands=['git commit -m "docs: update app/x.py and app/y.py"'])
        # …including the bundled spelling, where `-am` is `-a -m`.
        claude_session(home, "bundler", wt, branch="unrelated",
                       commands=['git commit -am "docs: update app/x.py"'])
        claude_session(home, "doer", wt, branch="unrelated",
                       commands=['git commit -m "fix" app/x.py app/y.py'])
        rc, out, err = run(home, PR_FILES, "--branch", "feat/x", "--host", "claude")
        rows = {r["conversation_id"]: r for r in json.loads(out)}
        check("a session that only NAMED the files in a message is not a candidate",
              "talker" not in rows, out[:200])
        check("…nor when the flags were bundled as `-am`",
              "bundler" not in rows, out[:200])
        check("…while one that passed them as pathspecs is",
              sorted(rows.get("doer", {}).get("evidence", {}).get("files", []))
              == ["app/x.py", "app/y.py"], out[:200])


def test_a_hidden_path_keeps_its_leading_dot():
    """`lstrip("./")` strips every leading dot, not a `./` prefix — so
    `.github/workflows/ci.yml` became `github/…` and a session that edited the
    non-hidden path scored false file evidence (Codex review, PR #182)."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        wt = str(Path(td) / "repo")
        # On an unrelated branch, so file evidence is the ONLY thing that
        # could put it on the list.
        claude_session(home, "wrong", wt, branch="unrelated",
                       edits=[f"{wt}/github/workflows/ci.yml", f"{wt}/env"])
        claude_session(home, "right", wt, branch="feat/x",
                       edits=[f"{wt}/.github/workflows/ci.yml", f"{wt}/.env"])
        rc, out, err = run(home, [".github/workflows/ci.yml", ".env"],
                           "--branch", "feat/x", "--host", "claude")
        ids = [r["conversation_id"] for r in json.loads(out)]
        check("the session that edited the HIDDEN paths is a candidate",
              "right" in ids, out)
        check("the session that edited the non-hidden look-alikes is not",
              "wrong" not in ids, out)
        rows = {r["conversation_id"]: r for r in json.loads(out)}
        check("…and the evidence names the paths with their dots intact",
              sorted(rows.get("right", {}).get("evidence", {}).get("files", []))
              == [".env", ".github/workflows/ci.yml"], out)


def test_a_commit_sha_outscores_everything():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        wt = str(Path(td) / "repo")
        # Two PR files + head branch = 4 + 3 = 7.
        claude_session(home, "aaaa-1111", wt, branch="feat/x",
                       edits=[f"{wt}/app/x.py", f"{wt}/app/y.py"])
        # A sha and nothing else = 5, but only from a command that MADE the
        # commit — `git log` merely displays one (Codex review, PR #182).
        claude_session(home, "cccc-3333", wt, branch="other",
                       commands=["git commit -m 'fix the thing'"],
                       results=["[other a1b2c3d4] fix the thing"])
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


def test_a_sha_counts_only_from_the_call_that_MADE_the_commit():
    """A sha is proof of authorship only if the command printing it created the
    commit. Two ways this went wrong: the skill's own step 2 runs `gh pr view
    <n> --json commits`, so the scanning session found the PR's shas in its own
    transcript; and a reviewer running `git log` on the branch saw them too.
    Both scored the top signal for code they had only read (Codex, PR #182)."""
    sha = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        wt = str(Path(td) / "repo")
        claude_session(home, "inspector", wt, branch="unrelated",
                       commands=["gh pr view 42 --json commits -q '.commits[].oid'"],
                       results=[sha])
        # …while the session that actually MADE the commit still counts.
        claude_session(home, "author", wt, branch="unrelated",
                       commands=["git commit -m fix"], results=[f"[wip {sha[:8]}] fix"])
        # …and a session that merely READ the history does not: `git log` shows
        # shas to anyone with the repo, so it is not authorship evidence.
        claude_session(home, "reader", wt, branch="unrelated",
                       commands=["git log --oneline -3"], results=[sha + " fix"])
        # …nor does one that only PUSHED commits an earlier session made: a
        # push prints an old..new range for work it did not write.
        claude_session(home, "pusher", wt, branch="unrelated",
                       commands=["git push origin HEAD"],
                       results=[f"   {sha[:7]}..{sha[8:15]}  feat/x -> feat/x"])
        # …nor one whose single Bash call both committed something unrelated
        # AND printed the log: one stdout cannot be split back into commands.
        claude_session(home, "mixed", wt, branch="unrelated",
                       commands=["git commit -m unrelated && git log --oneline -5"],
                       results=[f"[wip 9999999] unrelated\n{sha[:8]} someone else"])
        # …nor one running a read-only history query whose name merely STARTS
        # with a commit-producing subcommand: `\bmerge\b` matched `merge-base`.
        claude_session(home, "ancestor", wt, branch="unrelated",
                       commands=["git merge-base main HEAD"], results=[sha])
        # …nor one whose commit-producing command failed: `git cherry-pick
        # <pr-sha>` answering `fatal: bad object <pr-sha>` echoes the sha back.
        claude_session(home, "failer", wt, branch="unrelated",
                       commands=[f"git cherry-pick {sha}"],
                       results=[f"fatal: bad object {sha}"],
                       result_is_error=True)
        rc, out, err = run(home, PR_FILES, "--branch", "feat/x", "--sha", sha,
                           "--host", "claude")
        ids = [r["conversation_id"] for r in json.loads(out)]
        check("the PR-inspecting session is not offered as a candidate",
              "inspector" not in ids, out)
        check("…nor is a session that only read the history with `git log`",
              "reader" not in ids, out)
        check("…nor one that only pushed commits it did not write",
              "pusher" not in ids, out)
        check("…nor one whose call both committed and printed the log",
              "mixed" not in ids, out)
        check("…nor one that only ran `git merge-base`",
              "ancestor" not in ids, out)
        check("…nor one whose commit-producing command FAILED",
              "failer" not in ids, out)
        check("…while the session that made the commit still counts",
              "author" in ids, out)


def test_an_oversize_session_is_reported_not_silently_dropped():
    """A transcript too large to parse is skipped for memory reasons — but an
    unexamined session must not look like an examined one that scored zero."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        wt = str(Path(td) / "repo")
        project = home / ".claude" / "projects" / wt.replace("/", "-")
        project.mkdir(parents=True)
        row = json.dumps(_record("h-0", "feat/x", wt, [{
            "type": "tool_use", "id": "t0", "name": "Edit",
            "input": {"file_path": f"{wt}/app/x.py", "new_string": PROSE}}]))
        # Just over the cap, written cheaply.
        with (project / "huge.jsonl").open("w", encoding="utf-8") as handle:
            written = 0
            while written <= 64 * 1024 * 1024:
                handle.write(row + "\n")
                written += len(row) + 1
        rc, out, err = run(home, PR_FILES, "--branch", "feat/x", "--host", "claude")
        check("it is not scanned", rc == 0 and json.loads(out) == [], out[:200])
        check("…and the omission is reported on stderr, naming it",
              "were not scanned" in err and "huge" in err, err)
        check("…while stdout stays valid JSON for the caller",
              isinstance(json.loads(out), list), out[:120])


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


def test_repo_scope_keeps_another_project_off_the_list():
    """`_matches_pr_file` matches by SUFFIX on purpose, so an unrelated
    project's `README.md` scores against the PR's files and can take a slot on
    the capped list from a real contributor (Codex review, PR #182)."""
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        mine = str(Path(td) / "ours")
        theirs = str(Path(td) / "elsewhere")
        for path in (mine, theirs):
            os.makedirs(path)
            subprocess.run(["git", "-C", path, "init", "-q", "-b", "main"],
                           check=True, capture_output=True)
        subprocess.run(["git", "-C", mine, "remote", "add", "origin",
                        "https://github.com/o/ours.git"], check=True, capture_output=True)
        subprocess.run(["git", "-C", theirs, "remote", "add", "origin",
                        "https://github.com/o/elsewhere.git"], check=True, capture_output=True)
        claude_session(home, "ours", mine, branch="feat/x",
                       edits=[f"{mine}/app/x.py", f"{mine}/app/y.py"])
        claude_session(home, "theirs", theirs, branch="feat/x",
                       edits=[f"{theirs}/app/x.py", f"{theirs}/app/y.py"])

        rc, out, _err = run(home, PR_FILES, "--branch", "feat/x", "--host", "claude")
        check("without --repo, the unrelated project scores too",
              "theirs" in [r["conversation_id"] for r in json.loads(out)], out[:200])

        rc, out, _err = run(home, PR_FILES, "--branch", "feat/x", "--host", "claude",
                            "--repo", "ours")
        ids = [r["conversation_id"] for r in json.loads(out)]
        check("with --repo, only this repo's session is a candidate",
              ids == ["ours"], out[:200])


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
