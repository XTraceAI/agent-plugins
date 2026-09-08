"""`capture.py current` — which session am I, and when to refuse to answer.

The whole point of this command is that it does NOT trust "newest .jsonl by
mtime". Several transcripts get touched in the same window, and a wrong answer
here links a pull request to somebody else's work. So the cwd match is the
content signature, staleness is a hard cut, and ambiguity is an error rather
than a guess.

Every case runs against a synthesized `$HOME` holding fake Claude, Codex and
Cursor session stores; nothing reads the real one and nothing reaches a
network.

Run: python3 tests/capture_current_test.py   (stdlib only)
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
CAPTURE = ROOT / "plugins" / "memhub" / "scripts" / "capture.py"

# A Codex rollout filename carries a real UUID, and the reader matches that
# shape exactly — a fixture with a made-up id would silently exercise the
# stem fallback instead of the path this command relies on.
CODEX_UUID = "01234567-89ab-cdef-0123-456789abcdef"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok ' if condition else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def claude_session(home: Path, sid: str, cwd: str, age_s: float = 0.0) -> Path:
    project = home / ".claude" / "projects" / cwd.replace("/", "-")
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{sid}.jsonl"
    path.write_text("".join(json.dumps({
        "type": "user", "uuid": f"{sid}-{i}", "cwd": cwd,
        "message": {"role": "user", "content": "hi"}}) + "\n" for i in range(3)),
        encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def codex_session(home: Path, sid: str, cwd: str, age_s: float = 0.0) -> Path:
    day = home / ".codex" / "sessions" / "2026" / "09" / "07"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-09-07T10-00-00-{sid}.jsonl"
    path.write_text(
        json.dumps({"timestamp": "2026-09-07T10:00:00Z", "type": "session_meta",
                    "payload": {"id": sid, "cwd": cwd}}) + "\n"
        + json.dumps({"timestamp": "2026-09-07T10:00:01Z", "type": "response_item",
                      "payload": {"type": "message", "role": "user",
                                  "content": [{"type": "input_text", "text": "hi"}]}}) + "\n",
        encoding="utf-8")
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))
    return path


def cursor_session(home: Path, sid: str, cwd: str, age_s: float = 0.0) -> Path:
    d = home / ".cursor" / "chats" / "w1" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "store.db").write_bytes(b"")
    (d / "meta.json").write_text(json.dumps({
        "cwd": cwd, "updatedAtMs": int((time.time() - age_s) * 1000)}),
        encoding="utf-8")
    return d / "store.db"


def run(home: Path, *args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, str(CAPTURE), "current", *args],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home),
             "PYTHONUTF8": "1"})
    return proc.returncode, proc.stdout, proc.stderr


def test_a_single_match_is_the_answer():
    with tempfile.TemporaryDirectory() as td:
        home, cwd = Path(td) / "home", str(Path(td) / "repo")
        os.makedirs(cwd)
        claude_session(home, "aaaa-1111", cwd)
        rc, out, err = run(home, "--cwd", cwd, "--json")
        row = json.loads(out) if out.strip() else {}
        check("exit 0 and the bare Claude id",
              rc == 0 and row.get("conversation_id") == "aaaa-1111"
              and row.get("host") == "claude", out + err)
        check("…and it reports the session's own cwd, not the encoded dir",
              row.get("cwd") == cwd, out)
        rc, out, _ = run(home, "--cwd", cwd)
        check("the human form is two lines: id then host",
              rc == 0 and out.splitlines() == ["aaaa-1111", "claude"], repr(out))


def test_non_claude_hosts_are_namespaced():
    for label, make, prefix, sid in (
            ("codex", codex_session, "codex-", CODEX_UUID),
            ("cursor", cursor_session, "cursor-", "bbbb-2222")):
        with tempfile.TemporaryDirectory() as td:
            home, cwd = Path(td) / "home", str(Path(td) / "repo")
            os.makedirs(cwd)
            make(home, sid, cwd)
            rc, out, err = run(home, "--cwd", cwd, "--json")
            row = json.loads(out) if out.strip() else {}
            check(f"{label} → {prefix}<uuid>, the id capture already sends",
                  rc == 0 and row.get("conversation_id") == prefix + sid,
                  out + err)


def test_a_sibling_worktree_is_not_this_session():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        mine, sibling = str(Path(td) / "repo"), str(Path(td) / "repo-wt")
        os.makedirs(mine)
        os.makedirs(sibling)
        claude_session(home, "cccc-3333", sibling)
        rc, out, err = run(home, "--cwd", mine)
        check("a session in a sibling worktree does not match → exit 3",
              rc == 3 and out.strip() == "" and "no live session" in err,
              out + err)


def test_a_stale_transcript_is_a_different_session():
    with tempfile.TemporaryDirectory() as td:
        home, cwd = Path(td) / "home", str(Path(td) / "repo")
        os.makedirs(cwd)
        claude_session(home, "dddd-4444", cwd, age_s=7200)
        rc, out, err = run(home, "--cwd", cwd)
        check("only a stale match → exit 3", rc == 3 and out.strip() == "", out + err)
        rc, out, _ = run(home, "--cwd", cwd, "--max-age-s", "10000")
        check("…and a wider window finds it",
              rc == 0 and out.splitlines()[0] == "dddd-4444", out)


def test_two_live_sessions_in_one_directory_refuse_rather_than_guess():
    with tempfile.TemporaryDirectory() as td:
        home, cwd = Path(td) / "home", str(Path(td) / "repo")
        os.makedirs(cwd)
        claude_session(home, "eeee-5555", cwd, age_s=0)
        claude_session(home, "ffff-6666", cwd, age_s=10)
        rc, out, err = run(home, "--cwd", cwd)
        check("two matches 10s apart → exit 4", rc == 4, out + err)
        check("…listing both, so the caller can ask",
              "eeee-5555" in err and "ffff-6666" in err, err)
        check("…and nothing is printed on stdout to be mistaken for an answer",
              out.strip() == "", out)


def test_candidates_from_two_hosts_are_ambiguous_too():
    # Nothing in a directory says which host the caller is running under.
    with tempfile.TemporaryDirectory() as td:
        home, cwd = Path(td) / "home", str(Path(td) / "repo")
        os.makedirs(cwd)
        claude_session(home, "aaaa-1111", cwd, age_s=0)
        codex_session(home, CODEX_UUID, cwd, age_s=600)
        rc, out, err = run(home, "--cwd", cwd)
        check("two hosts → exit 4 however far apart their clocks are", rc == 4, err)
        check("…both are named",
              "aaaa-1111" in err and f"codex-{CODEX_UUID}" in err, err)
        rc, out, _ = run(home, "--cwd", cwd, "--host", "claude")
        check("…and naming the host resolves it",
              rc == 0 and out.splitlines()[0] == "aaaa-1111", out)


def test_a_session_that_moved_is_found_where_it_ended():
    # A session can `cd`; the LAST record naming a directory is the one that
    # matters, so a mid-session move must not leave `current` pointing at the
    # directory the session started in.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td) / "home"
        started, ended = str(Path(td) / "repo-a"), str(Path(td) / "repo-b")
        os.makedirs(started)
        os.makedirs(ended)
        path = claude_session(home, "gggg-7777", started)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "user", "uuid": "gggg-7777-9", "cwd": ended,
                "message": {"role": "user", "content": "moved"}}) + "\n")
        rc, out, err = run(home, "--cwd", ended)
        check("it is found under the directory it ended in",
              rc == 0 and out.splitlines()[0] == "gggg-7777", out + err)
        rc, _, _ = run(home, "--cwd", started)
        check("…and not under the one it started in", rc == 3)


def test_a_busy_machine_does_not_hide_the_running_session():
    """The readers sort globally by mtime, so capping the enumeration before
    the cwd filter made the live session invisible whenever the host had that
    many newer transcripts in other worktrees (Codex review, PR #182)."""
    with tempfile.TemporaryDirectory() as td:
        home, cwd = Path(td) / "home", str(Path(td) / "repo")
        os.makedirs(cwd)
        # The one we want is the OLDEST of the fresh sessions.
        claude_session(home, "aaaa-1111", cwd, age_s=300)
        for index in range(120):
            other = str(Path(td) / f"other{index}")
            os.makedirs(other)
            claude_session(home, f"bbbb-{index:04d}", other, age_s=index)
        rc, out, err = run(home, "--cwd", cwd, "--host", "claude")
        check("the running session is still found behind 120 newer ones",
              rc == 0 and out.splitlines()[:1] == ["aaaa-1111"], out + err[:200])


def test_an_empty_home_is_a_clean_refusal():
    with tempfile.TemporaryDirectory() as td:
        home, cwd = Path(td) / "home", str(Path(td) / "repo")
        os.makedirs(cwd)
        home.mkdir()
        rc, out, err = run(home, "--cwd", cwd)
        check("no sessions anywhere → exit 3, no traceback",
              rc == 3 and out.strip() == "" and "Traceback" not in err, err)


if __name__ == "__main__":
    print("capture current")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
    else:
        print("all capture current checks passed")
    sys.exit(1 if failures else 0)
