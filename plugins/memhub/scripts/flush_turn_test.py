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
        check("no cursor -> 0", ft._read_cursor(ft._read_state("s1"), 500), 0)

        ft._save_state("s1", offset=120, last_uuid="u-1", cwd="/repo",
                       namespace="my-project")
        check("round-trips", ft._read_cursor(ft._read_state("s1"), 500), 120)
        # Remembered so a delta of sidecar-only records (which never carry cwd)
        # still routes to the repo's room instead of personal memory.
        check("cwd remembered", ft._read_state("s1").get("cwd"), "/repo")
        # Merging, not replacing: a later flush that only knows the offset must
        # not erase the repo, namespace and title resolved earlier.
        ft._save_state("s1", offset=200)
        check("merge keeps cwd", ft._read_state("s1").get("cwd"), "/repo")
        check("merge keeps namespace",
              ft._read_state("s1").get("namespace"), "my-project")
        check("merge updates offset", ft._read_state("s1").get("offset"), 200)
        ft._save_state("s1", offset=120)
        # Remembered too, so the fallback never re-shells out to git.
        check("namespace remembered",
              ft._read_state("s1").get("namespace"), "my-project")

        # A file smaller than the cursor means the transcript was rewritten:
        # the offset now points into different content, so every byte must be
        # re-sent. Trusting it would skip records permanently.
        check("shrunken file -> 0", ft._read_cursor(ft._read_state("s1"), 50), 0)
        check("exactly at eof kept", ft._read_cursor(ft._read_state("s1"), 120), 120)

        (Path(d) / "s2.json").write_text("{not json")
        check("corrupt cursor -> 0", ft._read_cursor(ft._read_state("s2"), 500), 0)


def test_title_is_harvested_from_the_transcript():
    """Claude Code already generates a session title and writes it as an
    ``ai-title`` record. Without reading it the automatic capture paths import
    every session unnamed, and the sessions list is a wall of untitled rows.

    The last one wins, because Claude Code regenerates the title as a session
    develops."""
    print("_titles")
    title = lambda recs, state=None: ft._titles(recs, state or {})[0]  # noqa: E731
    # Nothing to go on: no title record, and no user prose to fall back to.
    check("none when absent", title([{"type": "assistant", "uuid": "a"}]), None)
    check("reads aiTitle",
          title([{"type": "ai-title", "aiTitle": "Review legacy skill code"}]),
          "Review legacy skill code")
    check("last one wins", title([
        {"type": "ai-title", "aiTitle": "first guess"},
        _rec("a"),
        {"type": "ai-title", "aiTitle": "better title"},
    ]), "better title")
    check("blank is not a title",
          title([{"type": "ai-title", "aiTitle": "   "}]), None)
    # ai-title is an inert type, so the title usually arrives in a batch that is
    # consumed WITHOUT a server call — it has to be read before dropping it.
    check("an ai-title batch is inert",
          _all_inert([{"type": "ai-title", "aiTitle": "x"}]), True)


def test_a_title_already_resolved_survives_a_delta_without_one():
    """Most deltas carry no title record at all. Recomputing from each one
    would drop the name the session already has — and, once there is a prompt
    fallback, replace it with whatever that delta happened to open with."""
    print("remembered titles")
    state = {"title": "Review legacy skill code"}
    check("a remembered title is reused",
          ft._titles([_rec("a")], state)[0], "Review legacy skill code")
    check("a fresh generated title beats the remembered one",
          ft._titles([{"type": "ai-title", "aiTitle": "regenerated"}],
                     state)[0], "regenerated")
    check("a remembered title beats the prompt fallback",
          ft._titles([_rec("a", "some later question")], state)[0],
          "Review legacy skill code")


def test_a_rename_outranks_the_stale_generated_title():
    """When the user renames a session the client writes a ``custom-title``
    record and KEEPS EMITTING the pre-rename ``ai-title`` beside it, often
    last. Measured on a real renamed session: 130 stale ai-title records
    interleaved with 47 custom-title ones. So precedence is by TYPE, and the
    rename is remembered separately — otherwise the next ai-title-only delta
    takes the old name back."""
    print("rename precedence")
    renamed = [{"type": "ai-title", "aiTitle": "stale generated"},
               {"type": "custom-title", "customTitle": "what the user chose"},
               {"type": "ai-title", "aiTitle": "stale generated"}]
    sent, custom = ft._titles(renamed, {})
    check("the rename wins over a later stale ai-title",
          sent, "what the user chose")
    check("the rename is handed back to be remembered",
          custom, "what the user chose")
    # The next delta carries only the stale generated title.
    check("a remembered rename is not taken back",
          ft._titles([{"type": "ai-title", "aiTitle": "stale generated"}],
                     {"title": "what the user chose",
                      "custom_title": "what the user chose"})[0],
          "what the user chose")
    # Same shape as ai-title: no ``message``, so a batch of them alone is
    # rejected by the server and must be consumed rather than sent.
    check("a custom-title batch is inert",
          _all_inert([{"type": "custom-title", "customTitle": "x"}]), True)


def test_a_headless_session_is_named_by_its_first_prompt():
    """A session started through the SDK or ``claude -p`` emits no title
    record at all — title generation is a UI feature — so capture worked and
    the session still arrived unnamed. Falling back to its first prompt is
    what gives it a name; it is LAST in precedence, so it can never displace
    a title the client did write."""
    print("headless fallback")
    check("no title record falls back to the prompt",
          ft._titles([_rec("a", "Reply with exactly: ok")], {})[0],
          "Reply with exactly: ok")
    check("a generated title still wins over the prompt",
          ft._titles([_rec("a", "Reply with exactly: ok"),
                      {"type": "ai-title", "aiTitle": "Generated"}], {})[0],
          "Generated")


def _all_inert(records):
    return all(r.get("type") in ft._INERT_RECORD_TYPES for r in records)


def test_inert_only_delta_is_consumed_but_attachments_are_not():
    """A delta of pure UI bookkeeping (ai-title, mode, …) is rejected by the
    server — no ``message`` means it reads as plain chat and fails role
    validation — so sending it can never succeed and would re-fail every turn.
    Consume it.

    An attachment record has no ``message`` either and is rejected the same way
    (verified against the server), but it carries real content. It must NOT be
    consumed: leaving the cursor pinned lets the next turn re-send it with the
    message records that make the batch valid."""
    print("inert vs attachment deltas")
    sidecars = [{"type": "ai-title", "title": "x"}, {"type": "mode", "mode": "y"}]
    check("pure UI bookkeeping is inert", _all_inert(sidecars), True)
    check("an attachment is NOT inert",
          _all_inert([{"type": "attachment", "uuid": "a1"}]), False)
    check("attachment among sidecars keeps the delta live",
          _all_inert(sidecars + [{"type": "attachment", "uuid": "a1"}]), False)
    check("a real turn keeps it live", _all_inert(sidecars + [_rec("a")]), False)


def test_timeout_override_never_breaks_the_hook():
    """The override is parsed at CALL time and floors at the default. Parsing it
    at import meant a bad value crashed the module before the handler that keeps
    this hook quiet could run — a traceback in the user's session. And 0 would
    time every flush out instantly, silently killing capture; MEMHUB_TURN_FLUSH=0
    is how you disable this, a timeout of nothing is a misconfiguration."""
    print("timeout override")
    d = ft._DEFAULT_FLUSH_TIMEOUT_S
    for raw, want, label in [
        (None, d, "unset -> default"),
        ("", d, "empty -> default"),
        ("   ", d, "blank -> default"),
        ("abc", d, "non-numeric -> default, no raise"),
        ("0", d, "zero -> default, not instant-timeout"),
        ("-5", d, "negative -> default"),
        ("12.5", 12.5, "valid float honoured"),
        ("90", 90.0, "valid int honoured"),
    ]:
        if raw is None:
            os.environ.pop("MEMHUB_TURN_FLUSH_TIMEOUT_S", None)
        else:
            os.environ["MEMHUB_TURN_FLUSH_TIMEOUT_S"] = raw
        check(label, ft._flush_timeout_s(), want)
    os.environ.pop("MEMHUB_TURN_FLUSH_TIMEOUT_S", None)


# ── lock ──────────────────────────────────────────────────────────────

def test_lock():
    """flock, so the kernel owns the lifetime. There is no stale-lock case to
    test because there is no such thing: a crashed flush releases on exit,
    including SIGKILL. That removes the reclaim path entirely, and with it the
    race where two hooks both judge a lock abandoned and both take it."""
    print("_acquire")
    with tempfile.TemporaryDirectory() as d:
        ft.STATE_DIR = Path(d)
        first = ft._acquire("s1")
        check("first acquires", first is not None, True)
        check("second is refused while held", ft._acquire("s1"), None)
        check("a different session is unaffected", ft._acquire("s2") is not None, True)

        os.close(first)
        again = ft._acquire("s1")
        check("re-acquires once released", again is not None, True)
        os.close(again)

        # The crash case: a child holds it, then dies. The kernel releases.
        code = ("import fcntl,os,sys;"
                "fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR, 0o600);"
                "fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)")
        held = subprocess.Popen([sys.executable, "-c", code + ";import time;time.sleep(30)",
                                 str(Path(d) / "s3.lock")])
        time.sleep(0.4)
        check("held by a live process", ft._acquire("s3"), None)
        held.kill(); held.wait()
        check("released when that process dies", ft._acquire("s3") is not None, True)


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

        # Dormant after the flush found a server without per-turn support.
        (state / "s1.json").write_text(json.dumps({"offset": 0, "unsupported": True}))
        check("unsupported server -> skip", _prefilter(base, home), 1)
        (state / "s1.json").write_text(json.dumps({"offset": 0}))

        # A live flock means a flush is in flight; skipping costs nothing
        # because the cursor has not moved.
        (state / "s1.json").write_text(json.dumps({"offset": 0}))
        lock = state / "s1.lock"
        code = ("import fcntl,os,sys;"
                "fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR, 0o600);"
                "fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB);"
                "import time;time.sleep(30)")
        held = subprocess.Popen([sys.executable, "-c", code, str(lock)])
        time.sleep(0.4)
        check("held flock -> skip", _prefilter(base, home), 1)

        # The crash case needs no staleness rule: the kernel released it.
        held.kill(); held.wait()
        check("holder died -> flush", _prefilter(base, home), 0)

        # An unlocked leftover file is not a lock.
        check("leftover lock file -> flush", _prefilter(base, home), 0)


if __name__ == "__main__":
    for fn in (test_tail, test_tail_is_bytes_not_chars,
               test_tail_stops_before_partial_line, test_cursor_trust,
               test_inert_only_delta_is_consumed_but_attachments_are_not,
               test_timeout_override_never_breaks_the_hook,
               test_title_is_harvested_from_the_transcript,
               test_a_title_already_resolved_survives_a_delta_without_one,
               test_a_rename_outranks_the_stale_generated_title,
               test_a_headless_session_is_named_by_its_first_prompt,
               test_lock, test_prefilter):
        fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        raise SystemExit(1)
    print("all passed")
