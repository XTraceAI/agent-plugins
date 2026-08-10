"""Self-test for routing resolution and the routing pin.

The failure guarded here forks one session into two conversations. It is
invisible in production: capture keeps working, nothing errors, and the session
simply appears twice with the same name and half the transcript in each — one
half in the repo's brain, one in personal memory, with the directives from the
second half unscoped and therefore recalled in every repo.

Two rules, and both directions matter:

* the cwd used for routing must be the most recent one that is a REPO, so a
  session that opens in the folder CONTAINING its checkouts and then cds into
  a worktree resolves the worktree — the container is not a repo, and picking
  it loses the room and the namespace together;
* once a room is resolved it is PINNED, so a later flush that resolves nothing
  reuses it instead of degrading to personal memory and minting a second
  conversation.

``test_real_transcripts_resolve_consistently`` replays whatever transcripts are
on the machine and asserts both capture paths agree on each. It skips rather
than fails when there are none, so the suite still runs on CI and on a fresh
checkout.

Run: python3 session_scope_test.py   (stdlib only)
"""
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import session_scope  # noqa: E402

_failures = []


def check(label, got, want):
    if got != want:
        _failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok   {label}")


def _isolate(tmp):
    session_scope.STATE_DIR = Path(tmp) / "state"


def _rec(cwd):
    return {"type": "user", "uuid": str(uuid.uuid4()), "cwd": cwd,
            "message": {"role": "user", "content": "hi"}}


def _git_repo(tmp, name):
    """A real git repo, because the check under test shells out to git."""
    path = Path(tmp) / name
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    return str(path)


# ── cwd selection ─────────────────────────────────────────────────────

def test_container_dir_is_not_chosen(tmp):
    """The exact shape of all six real forks.

    The session opens in the folder that CONTAINS a repo, then cds into it.
    Picking the first cwd picks the container, which is not a repo — so the
    remote lookup fails and the room and namespace vanish together.
    """
    container = str(Path(tmp) / "repos")
    Path(container).mkdir(parents=True, exist_ok=True)
    worktree = _git_repo(tmp, "repos/project-worktree")
    records = [_rec(container), _rec(container), _rec(worktree), _rec(worktree)]
    check("resolves the worktree, not the container",
          session_scope.resolve_cwd(records), worktree)
    check("the old first-cwd rule would have picked the container",
          records[0]["cwd"], container)


def test_most_recent_repo_wins(tmp):
    """A session that moved between two repos routes by where it IS."""
    first = _git_repo(tmp, "one")
    second = _git_repo(tmp, "two")
    check("latest repo wins",
          session_scope.resolve_cwd([_rec(first), _rec(second)]), second)


def test_falls_back_to_most_recent_when_nothing_is_a_repo(tmp):
    """Never None while any record carries a cwd: declining to route is the
    fork, so the old behaviour is the floor, not an error."""
    a = str(Path(tmp) / "nota"); b = str(Path(tmp) / "notb")
    Path(a).mkdir(); Path(b).mkdir()
    check("degrades to the most recent cwd",
          session_scope.resolve_cwd([_rec(a), _rec(b)]), b)


def test_no_cwd_at_all(tmp):
    check("no cwd anywhere", session_scope.resolve_cwd(
        [{"type": "mode"}, {"type": "custom-title"}]), None)


def test_probe_count_is_bounded(tmp):
    """Each probe is a subprocess on a hook with a real time budget."""
    dirs = []
    for i in range(12):
        d = str(Path(tmp) / f"d{i}")
        Path(d).mkdir()
        dirs.append(d)
    calls = []
    real = session_scope._is_repo
    session_scope._is_repo = lambda c: (calls.append(c), False)[1]
    try:
        session_scope.resolve_cwd([_rec(d) for d in dirs])
    finally:
        session_scope._is_repo = real
    check("probes are capped", len(calls) <= session_scope._MAX_CWD_PROBES,
          True)


def test_candidates_are_deduped_most_recent_first(tmp):
    a, b = "/x/a", "/x/b"
    check("dedup preserves most-recent-first",
          session_scope.candidate_cwds([_rec(a), _rec(b), _rec(a)]), [a, b])


def test_both_entry_points_order_identically(tmp):
    """`A, B, A` is the case that separated them.

    Deduping on FIRST occurrence orders that `B, A`; on LAST, `A, B`. The two
    entry points disagreed on exactly this, so a session that returned to a
    directory it had used earlier resolved differently depending on which
    capture path asked — the disagreement is the fork.
    """
    a, b = "/x/a", "/x/b"
    path = Path(tmp) / "t.jsonl"
    with open(path, "wb") as handle:
        for cwd in (a, b, a):
            handle.write((json.dumps(_rec(cwd)) + "\n").encode("utf-8"))
    check("file scan orders by LAST occurrence",
          session_scope.cwds_in_transcript(str(path)), [a, b])
    check("and matches the records-based scan",
          session_scope.cwds_in_transcript(str(path)),
          session_scope.candidate_cwds([_rec(a), _rec(b), _rec(a)]))


def test_transcript_scan_tolerates_junk(tmp):
    """Partial trailing writes and unparseable lines are routine."""
    path = Path(tmp) / "t.jsonl"
    with open(path, "wb") as handle:
        handle.write((json.dumps(_rec("/x/a")) + "\n").encode("utf-8"))
        handle.write(b'{"cwd": not json}\n')
        handle.write(b'{"type":"user","cwd":"/x/b"')  # partial, no newline
    check("junk skipped, good records kept",
          session_scope.cwds_in_transcript(str(path)), ["/x/a"])
    check("missing file is empty, not an error",
          session_scope.cwds_in_transcript(str(Path(tmp) / "nope")), [])


# ── the pin ───────────────────────────────────────────────────────────

def test_pin_survives_a_failed_resolution(tmp):
    """The whole point: one bad lookup must not re-scope the conversation."""
    conv = "conv-1"
    room = {"brain_id": "brain-A", "org_id": "org-1"}
    session_scope.apply_pin(conv, room, "/repo", "myrepo")
    got_room, got_cwd, got_ns = session_scope.apply_pin(conv, None, None, None)
    check("room survives", got_room, room)
    check("cwd survives", got_cwd, "/repo")
    check("namespace survives", got_ns, "myrepo")


def test_pin_beats_a_conflicting_resolution(tmp):
    """A DIFFERENT answer mid-session is a fork arriving by another route."""
    conv = "conv-2"
    session_scope.apply_pin(conv, {"brain_id": "brain-A", "org_id": "o"},
                            "/repo", "myrepo")
    got_room, _cwd, _ns = session_scope.apply_pin(
        conv, {"brain_id": "brain-B", "org_id": "o"}, "/repo", "myrepo")
    check("first resolution wins", got_room["brain_id"], "brain-A")


def test_unrouted_stays_unrouted_until_a_room_appears(tmp):
    """A session with no room must NOT pin 'personal' as a decision — the room
    may simply not have been resolvable yet, and pinning the absence would
    strand the session in personal memory for good."""
    conv = "conv-3"
    room, _c, _n = session_scope.apply_pin(conv, None, "/repo", "myrepo")
    check("no room resolved yet", room, None)
    room, _c, _n = session_scope.apply_pin(
        conv, {"brain_id": "brain-A", "org_id": "o"}, "/repo", "myrepo")
    check("a room that appears later is adopted", room["brain_id"], "brain-A")
    room, _c, _n = session_scope.apply_pin(conv, None, "/repo", "myrepo")
    check("and pinned from then on", room["brain_id"], "brain-A")


def test_clear_room_drops_only_the_room(tmp):
    conv = "conv-4"
    session_scope.apply_pin(conv, {"brain_id": "brain-A", "org_id": "o"},
                            "/repo", "myrepo")
    session_scope.clear_room(conv)
    room, cwd, ns = session_scope.apply_pin(conv, None, None, None)
    check("room forgotten", room, None)
    check("cwd kept", cwd, "/repo")
    check("namespace kept", ns, "myrepo")


def test_pins_are_per_conversation(tmp):
    session_scope.apply_pin("conv-a", {"brain_id": "A", "org_id": "o"},
                            "/a", "a")
    room, _c, _n = session_scope.apply_pin("conv-b", None, "/b", "b")
    check("a pin does not leak to another conversation", room, None)


def test_pin_never_stores_none_over_a_value(tmp):
    conv = "conv-5"
    session_scope.pin(conv, cwd="/repo", namespace="myrepo")
    session_scope.pin(conv, cwd=None, namespace=None)
    check("None does not erase", session_scope.read_pin(conv).get("namespace"),
          "myrepo")


def test_missing_and_corrupt_pin_degrade(tmp):
    check("absent pin reads empty", session_scope.read_pin("nope"), {})
    session_scope.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (session_scope.STATE_DIR / "conv-6.scope.json").write_text("{oh no")
    check("corrupt pin reads empty", session_scope.read_pin("conv-6"), {})
    room, _c, _n = session_scope.apply_pin(
        "conv-6", {"brain_id": "A", "org_id": "o"}, "/r", "r")
    check("and re-pins cleanly", room["brain_id"], "A")


def test_empty_conversation_id_is_inert(tmp):
    session_scope.pin("", cwd="/repo")
    check("no pin written for an empty id", session_scope.read_pin(""), {})


# ── the real thing ────────────────────────────────────────────────────

def _local_transcripts(limit=400):
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return []
    return sorted(root.glob("*/*.jsonl"))[:limit]


def _load(path):
    records = []
    try:
        with open(path, "rb") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    except OSError:
        return []
    return records


def test_real_transcripts_resolve_consistently(tmp):
    """The property, asserted over whatever transcripts this machine has.

    Both capture paths take their FIRST resolution from the whole transcript
    file, so the invariant is that the two entry points — the backstop's
    ``resolve_cwd_from_transcript`` and a records-based ``resolve_cwd`` over
    the same file — cannot disagree. An earlier draft asserted that a DELTA
    window agreed with the whole file; replaying real transcripts showed it
    does not, and cannot in general, which is what drove both paths onto the
    whole-file entry point.

    Also asserts the fix itself: whenever any directory the session used is a
    repo, the resolved one is a repo — never the container it started in.

    Skips rather than fails when there is nothing local to read, so this still
    runs on CI and on a fresh checkout.
    """
    examined = at_risk = 0
    for path in _local_transcripts():
        records = _load(path)
        candidates = session_scope.candidate_cwds(records)
        if not candidates:
            continue
        examined += 1
        from_file = session_scope.resolve_cwd_from_transcript(str(path))
        from_records = session_scope.resolve_cwd(records)
        check(f"{path.stem[:8]}: both entry points agree",
              from_file, from_records)
        oldest = candidates[-1]  # what the whole-file path used to take
        if from_file != oldest:
            at_risk += 1  # the session moved: the old rule would have differed
        if any(session_scope._is_repo(c)
               for c in candidates[:session_scope._MAX_CWD_PROBES]):
            check(f"{path.stem[:8]}: resolved a repo, not a container",
                  session_scope._is_repo(from_file), True)
    if not examined:
        print("  skip no local transcripts to replay")
    else:
        print(f"  ..   replayed {examined} transcripts, "
              f"{at_risk} where the old rule would have picked elsewhere")


if __name__ == "__main__":
    # Discovered from globals(), NOT a hand-maintained list — see
    # ``registration_test`` for the bug that convention exists to prevent.
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            print(_name)
            with tempfile.TemporaryDirectory() as _tmp:
                _isolate(_tmp)
                _fn(_tmp)
    if _failures:
        print(f"\n{len(_failures)} failure(s)")
        raise SystemExit(1)
    print("\nall passed")
