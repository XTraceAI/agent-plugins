"""Self-test for the per-turn flush's cursor, tail read and lock.

These cover the failures that are SILENT in production: a byte offset that
drifts skips records forever, and a lock that does not hold lets two flushes
race. Neither surfaces as an error — the session just quietly stops being
captured correctly — so they are asserted here instead.

Run: python3 flush_turn_test.py   (stdlib only; the mcp import in flush_turn
is lazy, inside _flush.)
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

# The tests live outside the plugin so they are not shipped to users;
# the code under test is still in the plugin's scripts dir.
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import flush_turn as ft  # noqa: E402

HERE = SCRIPTS
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


def test_only_a_server_round_trip_retracts_a_failure():
    """A recorded failure survives every local-only state write.

    This is a REGRESSION LOCK, and the tempting change it forbids looks like a
    bug fix: the inert-delta branch advances the cursor with a plain
    ``_save_state``, so a session that failed and then consumed an inert delta
    still reads as failing. Making that branch clear the error would be wrong.
    It returns above ``resolve_url_and_auth`` and never contacts the server, so
    it is not evidence that anything recovered — and inert deltas are common
    (the title usually arrives in one), so a genuinely broken session would
    routinely erase its own alarm and go back to failing in silence.

    Only ``_mark_success``, reached solely after a committed round-trip,
    retracts a failure."""
    print("failure retraction")
    original = ft.STATE_DIR
    with tempfile.TemporaryDirectory() as tmp:
        ft.STATE_DIR = Path(tmp)
        try:
            sid = "retract"
            ft._mark_failure(sid, "auth", "no usable cached OAuth token")
            check("failure is recorded", ft._read_state(sid).get("last_error"), "auth")

            # Exactly what the inert-delta branch does.
            ft._save_state(sid, offset=512, title="t")
            state = ft._read_state(sid)
            check("a local-only save advances the cursor", state.get("offset"), 512)
            check("but does NOT retract the failure",
                  state.get("last_error"), "auth")
            check("and records no false success", state.get("last_ok_at"), None)

            ft._mark_success(sid, offset=1024)
            state = ft._read_state(sid)
            check("a committed round-trip clears the error",
                  state.get("last_error"), None)
            check("it clears the detail too",
                  state.get("last_error_detail"), None)
            check("and stamps the success",
                  isinstance(state.get("last_ok_at"), float), True)
            check("while keeping the cursor", state.get("offset"), 1024)

            # The health check must agree with the state it reads.
            ft._mark_failure(sid, "timeout", "no response")
            check("a fresh failure after a success stands again",
                  ft._read_state(sid).get("last_error"), "timeout")
        finally:
            ft.STATE_DIR = original


def test_a_title_never_carries_a_credential():
    """A title is derived from the RAW records, so it needs its own redaction.

    The batch redaction downstream only covers `sendable`. A session whose
    first prompt is `export MEMHUB_TOKEN=mhk_…` would otherwise ship that key
    as the conversation's NAME — the most visible field there is, and metadata
    the redaction pass was supposed to have covered. The stored copy matters
    too: it is re-sent on every later flush.
    """
    print("title redaction")
    secret = "mhk_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0Uv2"

    title, custom = ft._titles(
        [{"type": "user", "uuid": "u1",
          "message": {"role": "user", "content": f"export MEMHUB_TOKEN={secret}"}}],
        {})
    check("a prompt-derived title is redacted", secret in (title or ""), False)
    check("but a title is still produced", bool(title), True)

    title, custom = ft._titles([{"type": "custom-title", "customTitle": f"key {secret}"}], {})
    check("a user's own title is redacted", secret in (title or ""), False)
    check("the remembered custom title is too", secret in (custom or ""), False)

    # A remembered title from an older build could carry one; it is re-sent
    # every flush, so it has to be cleaned on the way out as well.
    title, _ = ft._titles([], {"title": f"stale {secret}"})
    check("a remembered title is redacted", secret in (title or ""), False)

    check("ordinary titles are untouched",
          ft._titles([{"type": "custom-title", "customTitle": "fix the auth bug"}], {})[0],
          "fix the auth bug")


def test_pr_url_queue_survives_auth_failure_and_clears_on_ack():
    """Trusted tool output is durable before auth and removed only on server ack."""
    print("PR URL provenance retry")
    url = "https://github.com/xtraceai/agent-plugins/pull/321"
    originals = {
        "state_dir": ft.STATE_DIR,
        "namespace": ft._namespace,
        "resolve_bearer": ft.resolve_bearer,
        "resolve_repo_brain": ft.resolve_repo_brain,
        "env_for_url": ft.env_for_url,
        "session": ft.mcp_http.Session,
        "git_resolve": ft.git_provenance.resolve,
        "log": ft._log,
    }
    seen = []

    class Session:
        def __init__(self, _url, _bearer, **_kwargs):
            pass

        async def call_tool(self, _name, arguments):
            seen.append(arguments)
            return types.SimpleNamespace(
                structuredContent={
                    "conversation_id": "session-pr",
                    "ack_through": "u1",
                    "provenance_received": {"github_pr_urls": [url]},
                },
                content=[], isError=False,
            )

    async def no_room(_session, _cwd, _env):
        return None

    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "session.jsonl"
        _write(transcript, [
            {"type": "assistant", "uuid": "u0", "cwd": "/repo",
             "message": {"role": "assistant", "content": [{
                 "type": "tool_use", "id": "create", "name": "Bash",
                 "input": {"command": "gh pr create --fill"},
             }]}},
            {"type": "user", "uuid": "u1", "cwd": "/repo",
             "message": {"role": "user", "content": [{
                 "type": "tool_result", "tool_use_id": "create",
                 "content": f"Created {url}",
             }]}},
        ])
        ft.STATE_DIR = Path(tmp) / "state"
        ft._namespace = lambda _records: ("/repo", "agent-plugins")
        ft.resolve_repo_brain = no_room
        ft.env_for_url = lambda _url: "staging"
        ft.git_provenance.resolve = lambda _cwd: {
            "branch": "feature/pr-link",
            "repository_url": "https://github.com/XTraceAI/agent-plugins.git",
        }
        ft._log = lambda _message: None
        try:
            ft.resolve_bearer = lambda: ("https://example.test/mcp", None)
            try:
                asyncio.run(ft._flush("session-pr", str(transcript)))
            except ft._NoCredential:
                pass
            state = ft._read_state("session-pr")
            check("auth failure keeps URL pending", state.get("pending_pr_urls"), [url])
            check("auth failure keeps cursor pinned", state.get("offset"), None)

            ft.resolve_bearer = lambda: ("https://example.test/mcp", "token")
            ft.mcp_http.Session = Session
            asyncio.run(ft._flush("session-pr", str(transcript)))
            state = ft._read_state("session-pr")
            check("ack clears pending URL", state.get("pending_pr_urls"), [])
            check("ack remembers accepted URL", state.get("accepted_pr_urls"), [url])
            check("provenance sent once", seen[0].get("provenance"), {
                "github_pr_urls": [url],
                "git": {"repository_url":
                        "https://github.com/XTraceAI/agent-plugins.git"},
            })
        finally:
            ft.STATE_DIR = originals["state_dir"]
            ft._namespace = originals["namespace"]
            ft.resolve_bearer = originals["resolve_bearer"]
            ft.resolve_repo_brain = originals["resolve_repo_brain"]
            ft.env_for_url = originals["env_for_url"]
            ft.mcp_http.Session = originals["session"]
            ft.git_provenance.resolve = originals["git_resolve"]
            ft._log = originals["log"]


def test_inert_filtered_delta_preserves_pr_url_evidence():
    """Out-of-band PR evidence survives even when its records are not sent.

    Slash-command filtering and redaction are allowed to consume a delta without
    a server round-trip. The raw paired tool result still has an independent
    lifetime: advancing the transcript offset must first make its exact URL
    durable for a later turn or SessionEnd replay.
    """
    print("inert filtered PR URL provenance")
    url = "https://github.com/xtraceai/agent-plugins/pull/654"
    originals = {
        "state_dir": ft.STATE_DIR,
        "drop_command_wrappers": ft.drop_command_wrappers,
        "resolve_bearer": ft.resolve_bearer,
    }

    with tempfile.TemporaryDirectory() as tmp:
        transcript = Path(tmp) / "session.jsonl"
        size = _write(transcript, [
            {"type": "assistant", "uuid": "u0",
             "message": {"role": "assistant", "content": [{
                 "type": "tool_use", "id": "create", "name": "Bash",
                 "input": {"command": "gh pr create --fill"},
             }]}},
            {"type": "user", "uuid": "u1",
             "message": {"role": "user", "content": [{
                 "type": "tool_result", "tool_use_id": "create",
                 "content": f"Created {url}",
             }]}},
        ])
        ft.STATE_DIR = Path(tmp) / "state"
        ft.drop_command_wrappers = lambda _records: []

        def unexpected_auth():
            raise AssertionError("an inert delta must not resolve auth")

        ft.resolve_bearer = unexpected_auth
        try:
            asyncio.run(ft._flush("session-inert-pr", str(transcript)))
            state = ft._read_state("session-inert-pr")
            check("inert delta advances its offset", state.get("offset"), size)
            check("inert delta keeps URL pending",
                  state.get("pending_pr_urls"), [url])
            check("inert delta has no accepted URLs",
                  state.get("accepted_pr_urls"), [])
        finally:
            ft.STATE_DIR = originals["state_dir"]
            ft.drop_command_wrappers = originals["drop_command_wrappers"]
            ft.resolve_bearer = originals["resolve_bearer"]


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

# The concurrent holder locks through portable_lock — the exact primitive the
# shipped code uses — so this simulation means the same thing on POSIX (flock)
# and native Windows (msvcrt.locking), where `import fcntl` does not exist.
# It prints one byte once the lock is HELD; waiting for that byte replaces a
# fixed sleep that raced slow process spawns.
_HOLDER = (
    "import os,sys;"
    "sys.path.insert(0, sys.argv[2]);"
    "import portable_lock;"
    "fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR, 0o600);"
    "portable_lock.lock_exclusive(fd, blocking=False);"
    "sys.stdout.write('L');sys.stdout.flush();"
    "import time;time.sleep(30)"
)


def _hold_lock_in_child(lock_path: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock_path), str(SCRIPTS)],
        stdout=subprocess.PIPE)
    if proc.stdout.read(1) != b"L":  # blocks until the child holds the lock
        raise RuntimeError("lock-holder child died before taking the lock")
    return proc


def _acquire_when_released(session_id: str, timeout_s: float = 5.0):
    """Windows releases a dead holder's byte-range lock a beat after the
    process object signals (documented: 'depends upon available system
    resources'); POSIX flock releases synchronously. Poll briefly so the
    assertion is about WHETHER the kernel releases, not how fast."""
    deadline = time.time() + timeout_s
    while True:
        fd = ft._acquire(session_id)
        if fd is not None or time.time() >= deadline:
            return fd
        time.sleep(0.05)


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
        other = ft._acquire("s2")
        check("a different session is unaffected", other is not None, True)
        if other is not None:
            # Windows cannot delete the tempdir while an fd holds a file in it.
            os.close(other)

        os.close(first)
        again = ft._acquire("s1")
        check("re-acquires once released", again is not None, True)
        os.close(again)

        held = _hold_lock_in_child(Path(d) / "s3.lock")
        check("held by a live process", ft._acquire("s3"), None)
        held.kill(); held.wait()
        reclaimed = _acquire_when_released("s3")
        check("released when that process dies", reclaimed is not None, True)
        if reclaimed is not None:
            os.close(reclaimed)


# ── prefilter ─────────────────────────────────────────────────────────

def _prefilter(payload, state_dir, **env_extra):
    # Both spellings, because the prefilter resolves its state dir from
    # Path.home(): POSIX expanduser reads HOME, Windows reads USERPROFILE
    # and never consults HOME.
    env = dict(os.environ, HOME=str(state_dir), USERPROFILE=str(state_dir),
               **env_extra)
    env.pop("MEMHUB_TURN_FLUSH", None)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(PREFILTER)], input=json.dumps(payload),
        capture_output=True, text=True, env=env,
    ).returncode


def _flush_when_released(payload, home, timeout_s: float = 5.0) -> int:
    """0 as soon as the dead holder's lock is gone — Windows can release it a
    beat late (see _acquire_when_released), POSIX is immediate."""
    deadline = time.time() + timeout_s
    while True:
        rc = _prefilter(payload, home)
        if rc == 0 or time.time() >= deadline:
            return rc
        time.sleep(0.05)


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
        held = _hold_lock_in_child(state / "s1.lock")
        check("held flock -> skip", _prefilter(base, home), 1)

        # The crash case needs no staleness rule: the kernel released it.
        held.kill(); held.wait()
        check("holder died -> flush", _flush_when_released(base, home), 0)

        # An unlocked leftover file is not a lock.
        check("leftover lock file -> flush", _prefilter(base, home), 0)


if __name__ == "__main__":
    # Discovered from globals(), NOT a hand-maintained tuple. A hand-maintained
    # tuple already silently dropped tests before. A registration list that
    # must be edited in a second place is a list that will eventually disagree
    # with the file, and the failure is invisible: the suite still passes,
    # just over less.
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print()
    if _failures:
        print(f"{len(_failures)} FAILED")
        raise SystemExit(1)
    print("all passed")
