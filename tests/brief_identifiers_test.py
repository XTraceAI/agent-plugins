"""Self-test for the git identifier extractor behind the brief.

The contract under test: only IDENTIFIERS come out — paths and ``PR #N`` /
``ENG-N`` references — and never words.

The git half runs against a throwaway repository built here, so the checks
are hermetic: no dependence on this checkout's branch or history.

Run: python3 tests/brief_identifiers_test.py  (from the repo root; stdlib only).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_TMP_HOME = tempfile.mkdtemp(prefix="brief-ids-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import brief_identifiers as bi  # noqa: E402

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def test_refs_canonical_forms() -> None:
    check("(#179) in a commit subject is PR #179",
          bi.refs_in("fix(md-capture): captured too (v0.49.1) (#179)") == ["PR #179"])
    check("eng-1010 in a branch name is ENG-1010",
          "ENG-1010" in bi.refs_in("fm-feat/eng-1010-facets"))
    check("refs are deduped in order",
          bi.refs_in("PR #5 then #5 then #6") == ["PR #5", "PR #6"])


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}).stdout


def test_from_git_reads_branch_commits_and_diff() -> None:
    repo = Path(tempfile.mkdtemp(prefix="brief-ids-repo-", dir=_TMP_HOME))
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("x", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-q", "-m", "first (#10)")
    # a fake origin/main so the base diff has something to compare against
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "-b", "fm-feat/eng-77-thing")
    (repo / "sub").mkdir()
    (repo / "sub" / "b.py").write_text("y", encoding="utf-8")
    _git(repo, "add", "."); _git(repo, "commit", "-q", "-m", "second touches sub (#11)")
    (repo / "c.txt").write_text("uncommitted", encoding="utf-8")
    _git(repo, "add", "c.txt")

    g = bi.from_git(repo)
    check("root resolves", Path(g["root"]).resolve() == repo.resolve())
    check("branch is read", g["branch"] == "fm-feat/eng-77-thing")
    check("default branch ref found without origin/HEAD", g["base"] == "origin/main")
    check("uncommitted change comes first", g["paths"][0] == "c.txt")
    check("commit paths are included", "sub/b.py" in g["paths"] and "a.py" in g["paths"])
    check("refs from branch name and commit subjects",
          set(g["refs"]) == {"ENG-77", "PR #11", "PR #10"})
    check("outside a repo everything is empty",
          bi.from_git(_TMP_HOME) == {"root": "", "branch": "", "head": "", "base": "",
                                     "paths": [], "refs": []})


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
        raise SystemExit(1)
    print("all brief_identifiers checks passed")
