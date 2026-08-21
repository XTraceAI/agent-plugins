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

# The tests live outside the plugin so they are not shipped to users;
# the code under test is still in the plugin's scripts dir.
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
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
        repo = _repo(tmp, "git@github.com:XTraceAI/agent-plugins.git")
        check("scp-style remote", rm.room_name(repo),
              "Repo: XTraceAI/agent-plugins")
        _git(repo, "remote", "set-url", "origin",
             "https://github.com/XTraceAI/agent-plugins.git")
        check("https remote", rm.room_name(repo),
              "Repo: XTraceAI/agent-plugins")

        _git(repo, "remote", "set-url", "origin",
             "ssh://git@github.com/XTraceAI/agent-plugins.git")
        check("ssh:// remote", rm.room_name(repo),
              "Repo: XTraceAI/agent-plugins")
        # Self-hosted hosts nest deeper; only the last two segments are the key.
        _git(repo, "remote", "set-url", "origin",
             "https://gitlab.example.com/group/subgroup/repo.git")
        check("deep path -> last two segments", rm.room_name(repo),
              "Repo: subgroup/repo")
        # Case is the tiebreaker between two distinct brains — never lowercase.
        _git(repo, "remote", "set-url", "origin", "git@github.com:XTraceAI/xmem.git")
        check("case preserved", rm.room_name(repo), "Repo: XTraceAI/xmem")
        # A port must not be mistaken for the scp `host:path` separator.
        _git(repo, "remote", "set-url", "origin", "ssh://git@host:22/org/name.git")
        check("ssh port kept out of the name", rm.room_name(repo), "Repo: org/name")
        # Org-less scp remote (self-hosted). One segment is CORRECT here: the
        # remote is still stable per clone, where the no-remote fallback isn't.
        _git(repo, "remote", "set-url", "origin", "git@host:name.git")
        check("org-less remote -> one segment", rm.room_name(repo), "Repo: name")

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


def test_git_local_config_execution_is_disarmed() -> None:
    """Capture supplies cwd, so every git probe must neutralize repo config."""
    print("git hardening")
    cmd = rm.git_readonly("/tmp/untrusted-repo")
    check("git command keeps cwd out of option position",
          cmd[:3], ["git", "-C", "/tmp/untrusted-repo"])
    check("fsmonitor command disabled",
          ["-c", "core.fsmonitor="] in [cmd[i:i + 2]
                                        for i in range(len(cmd) - 1)], True)
    check("hooks redirected",
          ["-c", "core.hooksPath=/dev/null"] in [cmd[i:i + 2]
                                                   for i in range(len(cmd) - 1)],
          True)
    check("credential helpers disabled",
          ["-c", "credential.helper="] in [cmd[i:i + 2]
                                            for i in range(len(cmd) - 1)], True)
    check("ext transports disabled",
          ["-c", "protocol.ext.allow=never"] in [cmd[i:i + 2]
                                                  for i in range(len(cmd) - 1)],
          True)

    env = rm.git_env()
    check("system config disabled", env.get("GIT_CONFIG_NOSYSTEM"), "1")
    check("terminal prompts disabled", env.get("GIT_TERMINAL_PROMPT"), "0")
    check("askpass disabled", env.get("GIT_ASKPASS"), "")
    check("ssh askpass disabled", env.get("SSH_ASKPASS"), "")
    check("bearer-bearing process env is not inherited",
          "MEMHUB_ACCESS_TOKEN" in env, False)

    # Prove the highest-risk primitive, not just the argv shape. Git executes a
    # repo-local core.fsmonitor command under an ordinary status probe; the
    # shared helper must suppress the same command. Git for Windows has
    # different shebang dispatch, so its portable contract is pinned above.
    if os.name != "nt":
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = _repo(tmp, None)
            marker = tmp / "PWNED"
            hook = tmp / "fsmonitor"
            hook.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('executed')\n"
            )
            hook.chmod(0o700)
            _git(repo, "config", "--local", "core.fsmonitor", str(hook))

            subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                           check=True, capture_output=True, text=True)
            check("poisoned repo executes under plain git", marker.exists(), True)

            marker.unlink(missing_ok=True)
            subprocess.run(rm.git_readonly(repo) + ["status", "--porcelain"],
                           env=rm.git_env(), check=True, capture_output=True,
                           text=True)
            check("hardened probe suppresses poisoned config",
                  marker.exists(), False)


def test_write_then_read_per_backend() -> None:
    print("write/read")
    _fresh_rooms()
    with tempfile.TemporaryDirectory() as td:
        repo = _repo(Path(td), "git@github.com:XTraceAI/agent-plugins.git")
        rm.write_room(PROD, cwd=repo, env="production")
        rm.write_room(STAGING, cwd=repo, env="staging")

        check("prod id", (rm.read_room(repo, "production") or {}).get("brain_id"), PROD)
        check("staging id", (rm.read_room(repo, "staging") or {}).get("brain_id"), STAGING)
        check("name reported", (rm.read_room(repo, "production") or {}).get("name"),
              "Repo: XTraceAI/agent-plugins")

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
              ["Repo: XTraceAI/agent-plugins", "Repo: XTraceAI/xmem"])
        check("both backends present",
              sorted(raw["repos"]["Repo: XTraceAI/agent-plugins"]),
              ["production", "staging"])

        # THE constraint: a brain id is account state. Nothing may be written
        # into the working tree — this plugin is installed into public repos.
        stray = [p for p in repo.rglob("*") if ".git" not in p.parts]
        check("nothing written into the repo", stray, [])


def test_reads_never_raise() -> None:
    print("degradation")
    _fresh_rooms()
    with tempfile.TemporaryDirectory() as td:
        repo = _repo(Path(td), "git@github.com:XTraceAI/agent-plugins.git")
        key = "Repo: XTraceAI/agent-plugins"
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


def test_concurrent_writers_dont_lose_entries() -> None:
    """One file holds every repo, so a lost update silently unroutes a repo."""
    print("concurrency")
    _fresh_rooms()
    script = SCRIPTS / "room_map.py"
    with tempfile.TemporaryDirectory() as td:
        # 12 writers, each claiming a DIFFERENT repo, launched together. Without
        # a lock the read-modify-write window drops most of them.
        procs = []
        for i in range(12):
            procs.append(subprocess.Popen(
                [sys.executable, str(script), "set",
                 "--brain-id", f"{i:08d}-1111-4111-8111-111111111111",
                 "--name", f"Repo: org/repo{i}", "--env", "production"],
                cwd=td, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        errs = [p.communicate()[1] for p in procs]
        check("all writers exited 0", [p.returncode for p in procs], [0] * 12)
        if any(errs):
            check("no writer stderr", [e for e in errs if e], [])

        raw = json.loads(rm.ROOMS_PATH.read_text())
        check("every repo survived", sorted(raw["repos"]),
              sorted(f"Repo: org/repo{i}" for i in range(12)))


def test_non_string_brain_id_is_rejected() -> None:
    """A hand-edited cache must not hand callers something they can't format."""
    print("type guard")
    _fresh_rooms()
    with tempfile.TemporaryDirectory() as td:
        repo = _repo(Path(td), "git@github.com:XTraceAI/xmem.git")
        rm.ROOMS_PATH.parent.mkdir(parents=True, exist_ok=True)
        rm.ROOMS_PATH.write_text(json.dumps(
            {"version": 1, "repos": {"Repo: XTraceAI/xmem":
                                     {"production": {"brain_id": 12345}}}}))
        # Truthy but not a str — callers do brain_id[:8], which would raise.
        check("numeric brain_id -> None", rm.read_room(repo, "production"), None)


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
    script = SCRIPTS / "room_map.py"
    with tempfile.TemporaryDirectory() as td:
        repo = _repo(Path(td), "git@github.com:XTraceAI/agent-plugins.git")
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
        check("name", out.stdout.strip(), "Repo: XTraceAI/agent-plugins")


def test_forget_room() -> None:
    """A cached id the server disowns must be droppable.

    `write_miss` deliberately refuses to overwrite a resolved id, which is right
    for a lookup that came back empty and wrong for a backend that answered
    "Agent brain not found" about the id we just sent. Without a way to forget,
    those two rules composed into a cache entry nothing could ever correct.
    """
    print("forget")
    _fresh_rooms()
    with tempfile.TemporaryDirectory() as td:
        repo = _repo(Path(td), "git@github.com:XTraceAI/agent-plugins.git")
        rm.write_room(PROD, cwd=repo, env="production")
        rm.write_room(STAGING, cwd=repo, env="staging")

        check("forget reports the drop", rm.forget_room(repo, "production"), True)
        check("the forgotten backend is gone", rm.read_room(repo, "production"), None)
        # Prod and staging hold different ids for the same repo; only the
        # backend that rejected the id is discredited.
        check("the other backend survives",
              (rm.read_room(repo, "staging") or {}).get("brain_id"), STAGING)

        # Nothing to drop is not an error — two flushes can race on the same
        # rejection and the second must not raise inside a capture hook.
        check("forgetting twice is harmless", rm.forget_room(repo, "production"), False)

        # No `missed_at`: the next resolution should genuinely re-ask. Branding
        # the repo room-less here would be inferring absence from an error
        # message rather than from a listing that actually came back empty.
        raw = json.loads(rm.ROOMS_PATH.read_text())
        entry = raw["repos"]["Repo: XTraceAI/agent-plugins"]
        check("no negative cache is invented", sorted(entry), ["staging"])
        check("re-resolution is due", rm.resolve_due(repo, "production"), True)

        # Dropping the last backend removes the repo outright rather than
        # leaving `{}` behind — `resolve_due` reads them identically and the
        # file stays legible.
        rm.forget_room(repo, "staging")
        raw = json.loads(rm.ROOMS_PATH.read_text())
        check("an empty repo entry is removed", raw["repos"], {})

        # Another repo's rooms are untouched by any of it.
        other = _repo(Path(td) / "sub", "git@github.com:XTraceAI/xmem.git")
        rm.write_room(PROD, cwd=other, env="production")
        rm.forget_room(repo, "production")
        check("other repos are untouched",
              (rm.read_room(other, "production") or {}).get("brain_id"), PROD)


def test_forget_outside_a_repo_is_safe() -> None:
    """Never raises. This runs inside a capture hook, so a cache that cannot be
    corrected must degrade to the old behaviour, not take the flush down."""
    print("forget/no-repo")
    _fresh_rooms()
    with tempfile.TemporaryDirectory() as td:
        plain = Path(td) / "not-a-repo"
        plain.mkdir()
        check("no repo means nothing to forget", rm.forget_room(plain, "production"),
              False)


if __name__ == "__main__":
    for test in (test_room_name_from_remote,
                 test_git_local_config_execution_is_disarmed,
                 test_write_then_read_per_backend,
                 test_forget_room, test_forget_outside_a_repo_is_safe,
                 test_reads_never_raise, test_concurrent_writers_dont_lose_entries,
                 test_non_string_brain_id_is_rejected, test_env_keying,
                 test_cli):
        test()
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nall room_map checks passed")
