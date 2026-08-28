#!/usr/bin/env python3
"""Git provenance resolution and durable record-branch pinning."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import git_provenance  # noqa: E402


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, encoding="utf-8")
    return result.stdout.strip()


def _repo(root: Path) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "MemHub Tests")
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "checkout", "-b", "feature/session-provenance")
    _git(repo, "remote", "add", "origin", "git@github.com:XTraceAI/demo.git")
    return repo, _git(repo, "rev-parse", "HEAD")


def test_real_attached_and_detached_snapshots():
    with tempfile.TemporaryDirectory() as td:
        repo, commit = _repo(Path(td))
        observed, branch = git_provenance.snapshot_branch(str(repo))
        assert observed and branch == "feature/session-provenance"
        assert git_provenance.snapshot(str(repo)) == {
            "branch": "feature/session-provenance",
            "commit_hash": commit,
            "repository_url": "git@github.com:XTraceAI/demo.git",
        }

        _git(repo, "checkout", "--detach", commit)
        observed, branch = git_provenance.snapshot_branch(str(repo))
        assert observed and branch is None
        detached = git_provenance.snapshot(str(repo))
        assert "branch" not in detached, detached
        assert detached["commit_hash"] == commit
        assert detached["repository_url"].endswith("XTraceAI/demo.git")
    print("PASS test_real_attached_and_detached_snapshots")


def test_native_priority_and_fail_closed_validation():
    native = {
        "branch": "native/topic",
        "commit_hash": "A" * 40,
        "repository_url": "https://github.com/XTraceAI/native.git",
    }
    assert git_provenance.resolve("/does/not/exist", native) == {
        "branch": "native/topic",
        "commit_hash": "a" * 40,
        "repository_url": "https://github.com/XTraceAI/native.git",
    }
    for invalid in (None, "", " HEAD", "HEAD", "topic..other",
                    "topic.lock", "topic with space", "-topic"):
        assert git_provenance.normalize_branch(invalid) is None, invalid
    assert git_provenance.resolve("/does/not/exist", {
        "branch": "HEAD", "commit_hash": "not-a-sha",
        "repository_url": "bad\nurl",
    }) == {}

    with tempfile.TemporaryDirectory() as td:
        repo, commit = _repo(Path(td))
        detached_native = git_provenance.resolve(str(repo), {"branch": "HEAD"})
        assert "branch" not in detached_native, detached_native
        assert detached_native["commit_hash"] == commit
        assert detached_native["repository_url"].endswith("XTraceAI/demo.git")
    print("PASS test_native_priority_and_fail_closed_validation")


def test_probe_failure_is_unavailable_not_detached():
    original = git_provenance.subprocess.run
    try:
        git_provenance.subprocess.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("git", 2))
        observed, branch = git_provenance.snapshot_branch(str(Path.cwd()))
        assert not observed and branch is None
        assert git_provenance.snapshot(str(Path.cwd())) == {}
    finally:
        git_provenance.subprocess.run = original
    print("PASS test_probe_failure_is_unavailable_not_detached")


def test_unavailable_probe_preserves_reader_native_branch():
    records = [{"uuid": "u1", "gitBranch": "native/topic"}]
    pins = git_provenance.pin_record_branches(
        records, None, observed=False, branch=None)
    assert pins == {"u1": "native/topic"}
    assert records[0]["gitBranch"] == "native/topic"
    print("PASS test_unavailable_probe_preserves_reader_native_branch")


def test_record_pins_survive_retry_and_branch_switch():
    first = [{"uuid": "u1"}, {"uuid": "u2"}]
    pins = git_provenance.pin_record_branches(
        first, None, observed=True, branch="feature/one")
    assert pins == {"u1": "feature/one", "u2": "feature/one"}
    assert [record["gitBranch"] for record in first] == [
        "feature/one", "feature/one"]

    grown = [{"uuid": "u1"}, {"uuid": "u2"}, {"uuid": "u3"}]
    pins = git_provenance.pin_record_branches(
        grown, pins, observed=True, branch="feature/two")
    assert pins == {
        "u1": "feature/one", "u2": "feature/one", "u3": "feature/two"}
    assert [record["gitBranch"] for record in grown] == [
        "feature/one", "feature/one", "feature/two"]

    detached = [{"uuid": "u1"}, {"uuid": "u2"}, {"uuid": "u3"},
                {"uuid": "u4", "gitBranch": "stale/native"}]
    pins = git_provenance.pin_record_branches(
        detached, pins, observed=True, branch=None)
    assert pins["u4"] is None and "gitBranch" not in detached[-1]
    print("PASS test_record_pins_survive_retry_and_branch_switch")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
