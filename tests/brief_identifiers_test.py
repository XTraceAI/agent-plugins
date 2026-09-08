"""Self-test for the identifier extractor behind the brief and the prompt hook.

The contract under test: only IDENTIFIERS come out — paths, symbols that exist
in the repo, ``PR #N`` / ``ENG-N``, quoted error strings — and never words. A
prompt about "what we did yesterday" yields nothing, so the hook that consumes
this stays silent and free on it.

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


REPO_TOKENS = {
    "plugins/memhub/scripts/brain_brief.py", "brain_brief.py", "brain_brief",
    "plugins/memhub/scripts/room_map.py", "room_map.py", "room_map",
    "tests/brain_brief_test.py", "brain_brief_test.py", "brain_brief_test",
    "plugins", "memhub", "scripts", "tests", "README.md", "readme",
}


def test_paths_symbols_refs_errors() -> None:
    found = bi.from_prompt(
        "Fix plugins/memhub/scripts/brain_brief.py so room_map.read_room copes with "
        "PR #182 and ENG-1010; the error was \"Agent brain not found\" and "
        "`ModuleNotFoundError: No module named mcp` showed up too.",
        REPO_TOKENS,
    )
    check("a repo-relative path is a path",
          "plugins/memhub/scripts/brain_brief.py" in found["paths"])
    check("Module.name is a symbol when the module is in the repo",
          "room_map.read_room" in found["symbols"])
    check("PR #N is a ref", "PR #182" in found["refs"])
    check("ENG-N is a ref", "ENG-1010" in found["refs"])
    check("a quoted failure message is an error string",
          "Agent brain not found" in found["errors"])
    check("a backticked *Error line is an error string",
          any(e.startswith("ModuleNotFoundError") for e in found["errors"]))


def test_bare_basename_needs_the_repo() -> None:
    found = bi.from_prompt("look at brain_brief.py and at wibble.py", REPO_TOKENS)
    check("a bare basename that IS a repo file is a path", "brain_brief.py" in found["paths"])
    check("a bare basename that is NOT a repo file is dropped", "wibble.py" not in found["paths"])
    check("the path's own stem is not reported again as a symbol",
          "brain_brief" not in found["symbols"])


def test_snake_case_must_occur_in_the_repo() -> None:
    found = bi.from_prompt("call brain_brief_test then update_config and do_it", REPO_TOKENS)
    check("a snake_case token in the repo is a symbol", "brain_brief_test" in found["symbols"])
    check("a snake_case token absent from the repo is not", "update_config" not in found["symbols"])
    check("a short snake token is not", "do_it" not in found["symbols"])


def test_negatives_yield_nothing() -> None:
    for prompt in (
        "please summarise what we did yesterday",
        "Why is the build slow? Think about it.",
        "see https://github.com/XTraceAI/agent-plugins/pull/9999 for context",
        "version 1.2.3 is out",
        "\"this is a long quoted sentence with nothing wrong in it\"",
    ):
        found = bi.from_prompt(prompt, REPO_TOKENS)
        check(f"nothing in: {prompt[:48]!r}",
              not any(found[k] for k in ("paths", "symbols", "refs", "errors")))
    check("entities_for of nothing is nothing", bi.entities_for([], []) == [])


def test_refs_canonical_forms() -> None:
    check("(#179) in a commit subject is PR #179",
          bi.refs_in("fix(md-capture): captured too (v0.49.1) (#179)") == ["PR #179"])
    check("eng-1010 in a branch name is ENG-1010",
          "ENG-1010" in bi.refs_in("fm-feat/eng-1010-facets"))
    check("refs are deduped in order",
          bi.refs_in("PR #5 then #5 then #6") == ["PR #5", "PR #6"])


def test_entities_are_relative_path_plus_basename() -> None:
    ents = bi.entities_for(["plugins/memhub/scripts/brain_brief.py"], ["PR #1"],
                           ["room_map.read_room"], ["Agent brain not found"])
    check("full relative path is an entity", "plugins/memhub/scripts/brain_brief.py" in ents)
    check("basename is an entity", "brain_brief.py" in ents)
    check("refs, symbols and errors follow", ents[-3:] == ["PR #1", "room_map.read_room",
                                                           "Agent brain not found"])
    many = bi.entities_for([f"dir/f{i}.py" for i in range(400)], [])
    check("the entity list is capped under the server's limit", len(many) <= bi.MAX_ENTITIES)


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
    toks = bi.repo_files(repo)
    check("repo_files carries paths, basenames and stems",
          {"sub/b.py", "b.py", "a.py"} <= toks and "a" not in toks)


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
