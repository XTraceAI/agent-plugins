"""Self-test for the repo->room cache.

Covers what the routing depends on: the room name comes from the REMOTE (so
worktrees agree), prod and staging keep separate ids, reads of a missing or
corrupt cache degrade to "no room" instead of raising, and a write leaves the
other backend's entry alone.

Run: python3 room_map_test.py  (stdlib only).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Redirect the cache BEFORE importing room_map (it resolves ROOMS_PATH at
# import time) so no test can touch the real ~/.config/memhub-plugin. Exported,
# not just patched, so the CLI subprocesses below inherit the same isolation.
_TMP_HOME = tempfile.mkdtemp(prefix="room-map-test-")
os.environ["MEMHUB_ROOMS_FILE"] = str(Path(_TMP_HOME) / "rooms.json")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import room_map as rm  # noqa: E402


def _fresh_rooms() -> None:
    """Start a test from an empty cache — one file holds every repo, so state
    leaks between tests otherwise."""
    rm.ROOMS_PATH.unlink(missing_ok=True)

PROD = "11111111-1111-4111-8111-111111111111"
STAGING = "22222222-2222-4222-8222-222222222222"

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok  {label}")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _repo(tmp: Path, remote: str | None) -> Path:
    repo = tmp / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    if remote:
        _git(repo, "remote", "add", "origin", remote)
    return repo


def test_room_name_from_remote() -> None:
    print("room_name")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # scp-style and URL-style remotes must reduce to the same room, and a
        # .git suffix must not leak into the name.
        repo = _repo(tmp, "git@github.com:XTraceAI/memhub-claude-plugin.git")
        check("scp-style remote", rm.room_name(repo),
              "Repo: XTraceAI/memhub-claude-plugin")
        _git(repo, "remote", "set-url", "origin",
             "https://github.com/XTraceAI/memhub-claude-plugin.git")
        check("https remote", rm.room_name(repo),
              "Repo: XTraceAI/memhub-claude-plugin")

        _git(repo, "remote", "set-url", "origin",
             "ssh://git@github.com/XTraceAI/memhub-claude-plugin.git")
        check("ssh:// remote", rm.room_name(repo),
              "Repo: XTraceAI/memhub-claude-plugin")
        # Self-hosted hosts nest deeper; only the last two segments are the key.
        _git(repo, "remote", "set-url", "origin",
             "https://gitlab.example.com/group/subgroup/repo.git")
        check("deep path -> last two segments", rm.room_name(repo),
              "Repo: subgroup/repo")
        # Case is the tiebreaker between two distinct brains — never lowercase.
        _git(repo, "remote", "set-url", "origin", "git@github.com:XTraceAI/xmem.git")
        check("case preserved", rm.room_name(repo), "Repo: XTraceAI/xmem")

    with tempfile.TemporaryDirectory() as td:
        repo = _repo(Path(td), None)
        check("no remote -> basename", rm.room_name(repo), "Repo: repo")

        # repo-brain.md §2: a linked worktree must resolve to the SAME room as
        # its main worktree. Using `--show-toplevel` here would give each
        # worktree its own name and fork the repo's memory.
        (repo / "f.txt").write_text("x")
        _git(repo, "add", "f.txt")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        wt = Path(td) / "linked-worktree"
        _git(repo, "worktree", "add", "-q", "-b", "wt", str(wt))
        check("worktree agrees with main", rm.room_name(wt), "Repo: repo")

    with tempfile.TemporaryDirectory() as td:
        check("outside a repo -> None", rm.room_name(Path(td)), None)


def test_write_then_read_per_backend() -> None:
    print("write/read")
    _fresh_rooms()
    with tempfile.TemporaryDirectory() as td:
        repo = _repo(Path(td), "git@github.com:XTraceAI/memhub-claude-plugin.git")
        rm.write_room(PROD, cwd=repo, env="production")
        rm.write_room(STAGING, cwd=repo, env="staging")

        check("prod id", (rm.read_room(repo, "production") or {}).get("brain_id"), PROD)
        check("staging id", (rm.read_room(repo, "staging") or {}).get("brain_id"), STAGING)
        check("name reported", (rm.read_room(repo, "production") or {}).get("name"),
              "Repo: XTraceAI/memhub-claude-plugin")

        # Re-writing one backend must not disturb the other — a repo used from
        # both installs holds both ids.
        rm.write_room(PROD.replace("1", "9"), cwd=repo, env="production")
        check("staging survives prod rewrite",
              (rm.read_room(repo, "staging") or {}).get("brain_id"), STAGING)

        # A second repo must not clobber the first — one file holds all repos.
        other = _repo(Path(td) / "sub", "git@github.com:XTraceAI/xmem.git")
        rm.write_room(STAGING, cwd=other, env="production")
        check("other repo isolated",
              (rm.read_room(repo, "production") or {}).get("brain_id"),
              PROD.replace("1", "9"))

        raw = json.loads(rm.ROOMS_PATH.read_text())
        check("version stamped", raw.get("version"), 1)
        check("keyed by room name", sorted(raw["repos"]),
              ["Repo: XTraceAI/memhub-claude-plugin", "Repo: XTraceAI/xmem"])
        check("both backends present",
              sorted(raw["repos"]["Repo: XTraceAI/memhub-claude-plugin"]),
              ["production", "staging"])

        # THE constraint: a brain id is account state. Nothing may be written
        # into the working tree — this plugin is installed into public repos.
        stray = [p for p in repo.rglob("*") if ".git" not in p.parts]
        check("nothing written into the repo", stray, [])


def test_reads_never_raise() -> None:
    print("degradation")
    _fresh_rooms()
    with tempfile.TemporaryDirectory() as td:
        repo = _repo(Path(td), "git@github.com:XTraceAI/memhub-claude-plugin.git")
        key = "Repo: XTraceAI/memhub-claude-plugin"
        check("no cache -> None", rm.read_room(repo, "production"), None)

        path = rm.ROOMS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        # A half-written cache is the realistic corruption (interrupted write).
        path.write_text('{"version": 1, "repos": {"Repo: XTrace')
        check("corrupt cache -> None", rm.read_room(repo, "production"), None)

        path.write_text('{"version": 1, "repos": []}')
        check("wrong shape -> None", rm.read_room(repo, "production"), None)

        path.write_text(json.dumps({"version": 1, "repos": {key: {"production": {}}}}))
        check("entry without brain_id -> None", rm.read_room(repo, "production"), None)

        path.write_text(json.dumps(
            {"version": 1, "repos": {"Repo: someone/else": {"production":
                                                            {"brain_id": PROD}}}}))
        check("another repo's entry is not used",
              rm.read_room(repo, "production"), None)
        path.unlink()

    with tempfile.TemporaryDirectory() as td:
        check("outside a repo -> None", rm.read_room(Path(td), "production"), None)


def test_env_keying() -> None:
    print("env_for_url")
    check("staging host",
          rm.env_for_url("https://api.staging.memhub.xtrace.ai/mcp-server/mcp"),
          "staging")
    check("prod host",
          rm.env_for_url("https://api.memhub.xtrace.ai/mcp-server/mcp"),
          "production")
    check("case-insensitive",
          rm.env_for_url("https://API.STAGING.memhub.xtrace.ai/mcp"), "staging")


def test_cli() -> None:
    print("cli")
    _fresh_rooms()
    script = Path(__file__).resolve().parent / "room_map.py"
    with tempfile.TemporaryDirectory() as td:
        repo = _repo(Path(td), "git@github.com:XTraceAI/memhub-claude-plugin.git")
        # `show` with nothing cached: exit 1 and silence, so callers can test
        # the exit code without parsing an error message.
        out = subprocess.run(
            [sys.executable, str(script), "show", "--cwd", str(repo),
             "--env", "production"],
            capture_output=True, text=True)
        check("show (empty) rc", out.returncode, 1)
        check("show (empty) stdout", out.stdout, "")

        subprocess.run(
            [sys.executable, str(script), "set", "--brain-id", PROD,
             "--cwd", str(repo), "--env", "production"],
            capture_output=True, text=True, check=True)
        out = subprocess.run(
            [sys.executable, str(script), "show", "--cwd", str(repo),
             "--env", "production"],
            capture_output=True, text=True)
        check("show rc", out.returncode, 0)
        check("show prints bare id", out.stdout.strip(), PROD)

        out = subprocess.run(
            [sys.executable, str(script), "name", "--cwd", str(repo)],
            capture_output=True, text=True)
        check("name", out.stdout.strip(), "Repo: XTraceAI/memhub-claude-plugin")


if __name__ == "__main__":
    for test in (test_room_name_from_remote, test_write_then_read_per_backend,
                 test_reads_never_raise, test_env_keying, test_cli):
        test()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall room_map checks passed")
