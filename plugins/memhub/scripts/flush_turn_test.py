"""Self-test for the per-turn flush's cursor, tail read and lock.

These cover the failures that are SILENT in production: a byte offset that
drifts skips records forever, and a lock that does not hold lets two flushes
race. Neither surfaces as an error — the session just quietly stops being
captured correctly — so they are asserted here instead.

Run: python3 flush_turn_test.py   (stdlib only; the mcp import in flush_turn
is lazy, inside _flush.)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flush_turn as ft  # noqa: E402

HERE = Path(__file__).resolve().parent
PREFILTER = HERE / "turn_flush_prefilter.py"

_failures = []


def check(label, got, want):
    if got != want:
        _failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


def _rec(uid, text="hi"):
    return {"type": "user", "uuid": uid, "message": {"role": "user", "content": text}}


def _write(path, records, partial=None):
    with open(path, "wb") as fh:
        for r in records:
            fh.write((json.dumps(r) + "\n").encode())
        if partial is not None:
            fh.write(partial.encode())  # no trailing newline
    return os.path.getsize(path)


# ── _read_tail ────────────────────────────────────────────────────────

def test_tail():
    print("_read_tail")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.jsonl"

        size = _write(p, [_rec("a"), _rec("b")])
        recs, consumed = ft._read_tail(str(p), 0)
        check("reads all from 0", [r["uuid"] for r in recs], ["a", "b"])
        check("consumes whole file", consumed, size)

        # Resuming from the cursor must yield ONLY the new record.
        recs, _ = ft._read_tail(str(p), size)
        check("nothing new at eof", recs, [])

        prev = size
        size = _write(p, [_rec("a"), _rec("b"), _rec("c")])
        recs, consumed = ft._read_tail(str(p), prev)
        check("resumes at cursor", [r["uuid"] for r in recs], ["c"])
        check("consumed == size", consumed, size)


def test_tail_is_bytes_not_chars():
    """The offset must be a BYTE count. Counting characters drifts on any
    non-ASCII turn — and a drifting cursor mis-seeks into the middle of a line
    on the next flush, silently dropping records from then on."""
    print("_read_tail — non-ASCII")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.jsonl"
        # Emoji + CJK: many bytes per character.
        size = _write(p, [_rec("a", "🎉 fix the café — 日本語テキスト")])
        recs, consumed = ft._read_tail(str(p), 0)
        check("one record", len(recs), 1)
        check("byte-exact consume", consumed, size)
        check("consumed > char count", consumed > len(json.dumps(recs[0])), True)

        recs, _ = ft._read_tail(str(p), consumed)
        check("no re-read after non-ASCII", recs, [])


def test_tail_stops_before_partial_line():
    """Claude Code is still appending while the hook runs, so the last line is
    routinely a partial write. It must be left for the next flush, not parsed
    as garbage and skipped past."""
    print("_read_tail — partial trailing write")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.jsonl"
        complete = json.dumps(_rec("a")) + "\n"
        _write(p, [_rec("a")], partial='{"type":"user","uu')
        recs, consumed = ft._read_tail(str(p), 0)
        check("skips the partial record", [r["uuid"] for r in recs], ["a"])
        check("cursor stops before it", consumed, len(complete.encode()))

        # Once the line lands whole, the next flush picks it up.
        _write(p, [_rec("a"), _rec("b")])
        recs, _ = ft._read_tail(str(p), consumed)
        check("completed line is not lost", [r["uuid"] for r in recs], ["b"])


# ── _read_cursor ──────────────────────────────────────────────────────

def test_cursor_trust():
    print("_read_cursor")
    with tempfile.TemporaryDirectory() as d:
        ft.STATE_DIR = Path(d)
        check("no cursor -> 0", ft._read_cursor("s1", 500), 0)

        ft._write_cursor("s1", 120, "u-1")
        check("round-trips", ft._read_cursor("s1", 500), 120)

        # A file smaller than the cursor means the transcript was rewritten:
        # the offset now points into different content, so every byte must be
        # re-sent. Trusting it would skip records permanently.
        check("shrunken file -> 0", ft._read_cursor("s1", 50), 0)
        check("exactly at eof kept", ft._read_cursor("s1", 120), 120)

        (Path(d) / "s2.json").write_text("{not json")
        check("corrupt cursor -> 0", ft._read_cursor("s2", 500), 0)


# ── lock ──────────────────────────────────────────────────────────────

def test_lock():
    print("_acquire")
    with tempfile.TemporaryDirectory() as d:
        ft.STATE_DIR = Path(d)
        first = ft._acquire("s1")
        check("first acquires", first is not None, True)
        check("second is refused", ft._acquire("s1"), None)

        first.unlink()
        again = ft._acquire("s1")
        check("re-acquires once released", again is not None, True)
        again.unlink()

        # A crashed flush must not wedge capture for the rest of the session.
        lock = Path(d) / "s2.lock"
        lock.write_text(json.dumps({"pid": os.getpid(), "at": time.time() - 10_000}))
        check("stale lock reclaimed", ft._acquire("s2") is not None, True)


# ── prefilter ─────────────────────────────────────────────────────────

def _prefilter(payload, state_dir, **env_extra):
    env = dict(os.environ, HOME=str(state_dir), **env_extra)
    env.pop("MEMHUB_TURN_FLUSH", None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(PREFILTER)], input=json.dumps(payload),
        capture_output=True, text=True, env=env,
    ).returncode


def test_prefilter():
    print("turn_flush_prefilter (0 = flush, 1 = skip)")
    with tempfile.TemporaryDirectory() as home:
        state = Path(home) / ".config" / "memhub-plugin" / "turnflush"
        state.mkdir(parents=True)
        t = Path(home) / "t.jsonl"
        size = _write(t, [_rec("a")])
        base = {"session_id": "s1", "transcript_path": str(t)}

        check("no cursor -> flush", _prefilter(base, home), 0)
        check("missing fields -> skip", _prefilter({}, home), 1)

        # The opt-out has to work without touching the installed plugin, and
        # has to be checked before any file work so disabling it is free.
        for off in ("0", "off", "false", "FALSE"):
            check(f"MEMHUB_TURN_FLUSH={off} -> skip",
                  _prefilter(base, home, MEMHUB_TURN_FLUSH=off), 1)
        check("MEMHUB_TURN_FLUSH=1 -> flush",
              _prefilter(base, home, MEMHUB_TURN_FLUSH="1"), 0)
        check("absent transcript -> skip",
              _prefilter({"session_id": "s1", "transcript_path": "/nope"}, home), 1)

        (state / "s1.json").write_text(json.dumps({"offset": size}))
        check("cursor at eof -> skip", _prefilter(base, home), 1)

        _write(t, [_rec("a"), _rec("b")])
        check("file grew -> flush", _prefilter(base, home), 0)

        (state / "s1.json").write_text(json.dumps({"offset": 10_000}))
        check("file shrank -> flush", _prefilter(base, home), 0)

        # A live lock means a flush is in flight; skipping costs nothing
        # because the cursor has not moved.
        (state / "s1.json").write_text(json.dumps({"offset": 0}))
        (state / "s1.lock").write_text(
            json.dumps({"pid": os.getpid(), "at": time.time()}))
        check("live lock -> skip", _prefilter(base, home), 1)

        (state / "s1.lock").write_text(
            json.dumps({"pid": os.getpid(), "at": time.time() - 10_000}))
        check("stale lock -> flush", _prefilter(base, home), 0)

        (state / "s1.lock").write_text(
            json.dumps({"pid": 999_999_999, "at": time.time()}))
        check("dead pid -> flush", _prefilter(base, home), 0)


if __name__ == "__main__":
    for fn in (test_tail, test_tail_is_bytes_not_chars,
               test_tail_stops_before_partial_line, test_cursor_trust,
               test_lock, test_prefilter):
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        raise SystemExit(1)
    print("all passed")
