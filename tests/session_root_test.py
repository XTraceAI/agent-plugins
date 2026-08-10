"""Self-test for chain-root conversation resolution.

The failure this guards is invisible in production: capture keeps working,
nothing errors, and the only symptom is a second conversation in the sessions
tab with the same name as the first. So the linking rules are asserted here.

Two directions matter equally and pull against each other:

* a RESUMED session must resolve to the conversation its parent registered —
  otherwise the duplicate this module exists to prevent comes back;
* a CLEARED session must NOT, ever — the anchor is content, so fresh records
  must stay unlinked, and a bug that over-links would silently merge unrelated
  work into one conversation, which is worse than the split it fixed.

The fixtures mirror what Claude Code actually writes, verified against real
transcripts in ``~/.claude/projects``: a resume copies the parent's records
with their uuids INTACT, restamps ``sessionId`` to the new session, and
re-roots ``parentUuid`` to null so nothing in the file names its parent.

Run: python3 session_root_test.py   (stdlib only)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import session_root  # noqa: E402

_failures = []


def check(label, got, want):
    if got != want:
        _failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


# ── fixtures ──────────────────────────────────────────────────────────

def _sidecar(kind, session_id):
    """A UI bookkeeping record. Carries no uuid — the preamble the anchor
    window has to skip rather than spend itself on."""
    return {"type": kind, "sessionId": session_id}


def _turn(session_id, parent=None, uid=None, text="hi"):
    return {"type": "user", "uuid": uid or str(uuid.uuid4()),
            "parentUuid": parent, "sessionId": session_id,
            "timestamp": "2026-08-10T00:00:00.000Z",
            "message": {"role": "user", "content": text}}


def _write(path, records):
    with open(path, "wb") as fh:
        for record in records:
            fh.write((json.dumps(record) + "\n").encode("utf-8"))


def _fresh_session(tmp, turns=6, title="ENG-1 do the thing"):
    """A session as Claude Code starts one: sidecars, then original turns."""
    sid = str(uuid.uuid4())
    records = [_sidecar("custom-title", sid), _sidecar("mode", sid)]
    parent = None
    for i in range(turns):
        rec = _turn(sid, parent=parent, text=f"{title} {i}")
        records.append(rec)
        parent = rec["uuid"]
    path = Path(tmp) / f"{sid}.jsonl"
    _write(path, records)
    return sid, path, records


def _resume_of(tmp, records, copy=None):
    """What Claude Code writes when an idle session is re-entered.

    Copies the parent's records with their uuids INTACT, restamps sessionId,
    re-roots the head to ``parentUuid: null``, then appends new turns.
    """
    sid = str(uuid.uuid4())
    copied = []
    for i, record in enumerate(records[:copy] if copy else records):
        clone = dict(record)
        if "sessionId" in clone:
            clone["sessionId"] = sid
        if i == 0:
            clone["parentUuid"] = None
        copied.append(clone)
    tail = [_turn(sid, text="and now the message after the pause")]
    path = Path(tmp) / f"{sid}.jsonl"
    _write(path, copied + tail)
    return sid, path


def _isolate(tmp):
    """Point the module's index at a scratch dir, not the real config."""
    session_root.STATE_DIR = Path(tmp) / "state"
    session_root.INDEX = session_root.STATE_DIR / "roots.json"
    session_root._LOCK = session_root.STATE_DIR / "roots.lock"


# ── tests ─────────────────────────────────────────────────────────────

def test_fresh_session_is_its_own_root(tmp):
    sid, path, _ = _fresh_session(tmp)
    check("fresh session keys on its own id",
          session_root.resolve(sid, str(path)), sid)


def test_resume_rejoins_the_parent(tmp):
    sid, path, records = _fresh_session(tmp)
    root = session_root.resolve(sid, str(path))
    rid, rpath = _resume_of(tmp, records)
    check("resumed session rejoins the original conversation",
          session_root.resolve(rid, str(rpath)), root)
    check("resume did NOT adopt its own session id",
          session_root.resolve(rid, str(rpath)) == rid, False)


def test_resume_of_a_resume(tmp):
    """A session re-entered twice stays ONE conversation, not three."""
    sid, path, records = _fresh_session(tmp)
    root = session_root.resolve(sid, str(path))
    r1, p1 = _resume_of(tmp, records)
    session_root.resolve(r1, str(p1))
    r1_records = [json.loads(line) for line in open(p1, "rb") if line.strip()]
    r2, p2 = _resume_of(tmp, r1_records)
    check("second resume still resolves to the original root",
          session_root.resolve(r2, str(p2)), root)


def test_clear_does_not_link(tmp):
    """The case that must never over-link.

    A cleared session's records are fresh uuids that appear in no index
    entry, so it becomes its own root — a NEW conversation, which is what
    clearing means.
    """
    _sid_a, path_a, _ = _fresh_session(tmp)
    root_a = session_root.resolve(_sid_a, str(path_a))
    sid_b, path_b, _ = _fresh_session(tmp)  # same repo, same title, new content
    root_b = session_root.resolve(sid_b, str(path_b))
    check("cleared session is its own root", root_b, sid_b)
    check("cleared session did not join the previous one",
          root_b == root_a, False)


def test_resume_after_compact_still_links(tmp):
    """A resume whose copy starts PAST the head still links.

    ``/compact`` heads the new file with a freshly minted summary record, so
    the original head uuid is absent. The anchor window is why the copied
    records that follow it still match.
    """
    sid, path, records = _fresh_session(tmp, turns=10)
    root = session_root.resolve(sid, str(path))
    rid = str(uuid.uuid4())
    summary = _turn(rid, text="[compact summary]")  # brand-new uuid
    copied = []
    for record in records[6:]:  # the head records did not survive the compact
        clone = dict(record)
        clone["sessionId"] = rid
        copied.append(clone)
    rpath = Path(tmp) / f"{rid}.jsonl"
    _write(rpath, [summary] + copied + [_turn(rid, text="after")])
    check("post-compact resume still finds the chain",
          session_root.resolve(rid, str(rpath)), root)


def test_repeat_resolution_is_stable(tmp):
    """Resolving twice must not re-root the chain onto the later session.

    Both capture paths resolve independently and repeatedly; if a second call
    could return a different id the two hooks would write to two
    conversations.
    """
    sid, path, records = _fresh_session(tmp)
    root = session_root.resolve(sid, str(path))
    rid, rpath = _resume_of(tmp, records)
    first = session_root.resolve(rid, str(rpath))
    again = session_root.resolve(rid, str(rpath))
    once_more = session_root.resolve(sid, str(path))
    check("repeat resolve of the resume is stable", again, first)
    check("parent still resolves to the same root", once_more, root)


def test_head_anchors_skips_sidecars(tmp):
    sid, path, _ = _fresh_session(tmp, turns=3)
    got = session_root.head_anchors(str(path))
    check("sidecar records contribute no anchors", len(got), 3)
    check("anchors are uuids", all(isinstance(a, str) for a in got), True)


def test_partial_trailing_line_is_ignored(tmp):
    """Claude Code appends while the hook reads; a half-written final line is
    routine, not corruption."""
    sid = str(uuid.uuid4())
    path = Path(tmp) / f"{sid}.jsonl"
    good = _turn(sid)
    with open(path, "wb") as fh:
        fh.write((json.dumps(good) + "\n").encode("utf-8"))
        fh.write(b'{"type":"user","uuid":"tru')
    check("partial line dropped", session_root.head_anchors(str(path)),
          [good["uuid"]])


def test_missing_transcript_degrades_to_session_id(tmp):
    sid = str(uuid.uuid4())
    check("unreadable transcript falls back to the session id",
          session_root.resolve(sid, str(Path(tmp) / "nope.jsonl")), sid)


def test_uuidless_transcript_degrades(tmp):
    """A transcript holding only sidecars registers nothing, so the next
    flush — which will have real records — gets the first word."""
    sid = str(uuid.uuid4())
    path = Path(tmp) / f"{sid}.jsonl"
    _write(path, [_sidecar("mode", sid), _sidecar("custom-title", sid)])
    check("anchorless transcript keys on the session id",
          session_root.resolve(sid, str(path)), sid)
    check("nothing was indexed", session_root.INDEX.exists(), False)


def test_index_is_pruned(tmp):
    """Entries expire, so the index cannot grow without bound."""
    sid, path, records = _fresh_session(tmp)
    session_root.resolve(sid, str(path))
    stale = json.loads(session_root.INDEX.read_text())
    aged = time.time() - session_root._MAX_AGE_S - 60
    stale["anchors"] = {uid: [root, aged]
                        for uid, (root, _ts) in stale["anchors"].items()}
    stale["anchors"]["keeper"] = ["conv-keeper", time.time()]
    session_root.INDEX.write_text(json.dumps(stale))
    sid2, path2, _ = _fresh_session(tmp)
    session_root.resolve(sid2, str(path2))
    after = json.loads(session_root.INDEX.read_text())["anchors"]
    check("expired anchors dropped",
          any(uid in after for uid in
              [r["uuid"] for r in records if "uuid" in r]), False)
    check("fresh anchors kept", "keeper" in after, True)


def test_corrupt_index_degrades(tmp):
    """A truncated or garbage index must not take capture down with it."""
    sid, path, _ = _fresh_session(tmp)
    session_root.STATE_DIR.mkdir(parents=True, exist_ok=True)
    session_root.INDEX.write_text("{not json at all")
    check("corrupt index falls back to the session id, then re-registers",
          session_root.resolve(sid, str(path)), sid)


def test_concurrent_registration_does_not_lose_entries(tmp):
    """Parallel sessions share one index file.

    An unlocked read-modify-write loses whichever registration finished
    second, leaving that chain unindexed — so it would split on its next
    resume, which is precisely the bug. Assert every writer survives.
    """
    sessions = [_fresh_session(tmp) for _ in range(8)]
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "import session_root, pathlib\n"
        "session_root.STATE_DIR = pathlib.Path(%r)\n"
        "session_root.INDEX = session_root.STATE_DIR / 'roots.json'\n"
        "session_root._LOCK = session_root.STATE_DIR / 'roots.lock'\n"
        "print(session_root.resolve(sys.argv[1], sys.argv[2]))\n"
    ) % (str(SCRIPTS), str(session_root.STATE_DIR))
    runner = Path(tmp) / "concurrent.py"
    runner.write_text(script)
    procs = [subprocess.Popen([sys.executable, str(runner), sid, str(path)],
                              stdout=subprocess.PIPE, text=True)
             for sid, path, _ in sessions]
    for proc in procs:
        proc.wait()
    index = json.loads(session_root.INDEX.read_text())["anchors"]
    missing = [sid for sid, _path, records in sessions
               if not any(r.get("uuid") in index for r in records)]
    check("every concurrent session registered its anchors", missing, [])


if __name__ == "__main__":
    # Discovered from globals(), NOT a hand-maintained list — the convention
    # the rest of this suite follows, for the reason spelled out in
    # ``registration_test``: a test that is defined but never registered never
    # runs, and the file reports green over code it did not execute.
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            print(_name)
            # A fresh index per test. These assert LINKING, so a shared index
            # would let one test's anchors decide another's outcome — and the
            # over-linking direction is exactly what must not be masked.
            with tempfile.TemporaryDirectory() as _tmp:
                _isolate(_tmp)
                _fn(_tmp)
    if _failures:
        print(f"\n{len(_failures)} failure(s)")
        raise SystemExit(1)
    print("\nall passed")
