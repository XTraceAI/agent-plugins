#!/usr/bin/env python3
"""Where a session's capture WRITES — resolved once, then pinned (stdlib only).

The server namespaces a conversation by the brain it was routed to::

    effective_source_id = f"cb:{cb_id}:{conv_id}" if cb_id else conv_id

so the routing decision is not just about scope — it is part of the
conversation's IDENTITY. A session that flushes unrouted and then routed does
not move; it FORKS into two rows with the same title, the same session id, and
disjoint halves — half its memory in the repo's room, half in personal, and the
directives from the unrouted half minted with no scope, so they recall in every
repo instead of this one.

Two independent causes, both fixed here.

**1. The paths disagreed about ``cwd``.** Routing is derived from a working
directory read out of the transcript, and the two capture paths read different
ones: the per-turn hook took the first cwd in its DELTA (the current directory),
while the whole-transcript backstop took the first cwd in the WHOLE FILE — the
directory the session STARTED in. Those differ on any session opened in a
directory that CONTAINS several checkouts or worktrees and then moved into one
of them. That container is not a git repo, so ``git remote get-url origin``
fails on it and BOTH the room lookup and the namespace come back empty — which
is the signature every forked conversation carries, the two missing together
rather than separately.

So pick the most RECENT cwd that is actually a repo, rather than the first one
seen. Recency is what makes both paths agree on a session that moved, and the
is-a-repo test is what keeps a container directory from being chosen at all.
Verified against real transcripts: every session that had forked this way began
in such a directory, and both windows agree on all of them after the change.

**2. Resolution could fail after it had already succeeded.** A git timeout, a
worktree deleted mid-session, a momentarily unreachable server — any of them
turned a routed session unrouted for one flush, which is all it takes to mint
the second row. So the answer is PINNED: the first flush that resolves a room
writes it down, and later flushes that resolve nothing reuse it instead of
degrading to personal memory.

The pin is keyed by CONVERSATION id (see ``session_root``), not session id, so
the per-turn hook and the backstop share one answer, and a resumed session keeps
writing where its chain already writes. That is a deliberate exception to those
two hooks otherwise sharing no state: the backstop keeps its own cursor
precisely so it still works when per-turn capture is broken, but identity is not
something they may disagree about — a second opinion here is the bug.

**A pinned room is only ever dropped deliberately.** ``forget_room`` clears it
when the SERVER says the brain does not exist, because then the pin names
something real that is gone. Nothing else may retract it.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path  # noqa: F401 — used in the type hints below

import atomic_write

STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "turnflush"

# How many distinct candidate directories are probed with git before giving up.
#
# Each probe is a subprocess with a 2s timeout, and this runs on a capture hook
# whose whole round-trip is budgeted in tens of seconds. A session that hopped
# through more than a handful of repos is not a shape worth spending the budget
# on — the most recent few cover every real case, and the fallback below is
# still the old behaviour rather than nothing.
_MAX_CWD_PROBES = 5


def _is_repo(cwd: str) -> bool:
    """True when ``cwd`` is inside a git work tree.

    ``rev-parse`` rather than ``remote get-url``: this only has to reject the
    container directory. Whether a repo has an ``origin`` is the room lookup's
    business, and a repo without one must still be preferred over a directory
    that is not a repo at all.
    """
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip() == "true"


def candidate_cwds(records) -> list[str]:
    """Distinct cwds in ``records``, MOST RECENT FIRST.

    Deduped so a session that alternates between two directories costs two
    probes rather than one per turn.
    """
    seen: list[str] = []
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        cwd = record.get("cwd")
        if isinstance(cwd, str) and cwd and cwd not in seen:
            seen.append(cwd)
    return seen


def resolve_cwd(records) -> str | None:
    """The working directory this session's routing should be derived from.

    The most recent cwd that is a git repo; failing that, the most recent cwd
    at all. Never None when any record carries one — falling back to the old
    behaviour beats declining to route, because an unrouted flush is the fork.
    """
    candidates = candidate_cwds(records)
    if not candidates:
        return None
    for cwd in candidates[:_MAX_CWD_PROBES]:
        if _is_repo(cwd):
            return cwd
    return candidates[0]


def namespace_for(cwd: str | None) -> str | None:
    """The git remote basename for ``cwd`` — the session's directive scope.

    One copy, because both capture paths and the manual import derive this and
    three copies of a rule are three chances to disagree about the scope a
    directive is recalled under.

    From the REMOTE, never the directory name: a worktree's basename would
    stamp a scope that hides its directives from the canonical repo's recalls.
    """
    if not cwd:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    url = out.stdout.strip()
    if out.returncode != 0 or not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def cwds_in_transcript(transcript_path: str) -> list[str]:
    """Distinct cwds in the whole transcript file, MOST RECENT FIRST.

    Parses only far enough to reach ``cwd``: the file can run to tens of MB and
    this is called on a capture hook, so the cheap substring test rejects the
    overwhelming majority of lines (tool results, sidecars) before any JSON is
    decoded.
    """
    # Ordered by LAST occurrence, which is what "most recent first" means once
    # reversed — a session that returns to a directory it used earlier has that
    # directory as its current one, not its oldest. Deduping on FIRST occurrence
    # instead ordered `A, B, A` as `B, A` rather than `A, B`, which made this
    # disagree with ``candidate_cwds`` on any session that moved back and forth
    # — reintroducing, between the two entry points, exactly the disagreement
    # this function exists to remove. Re-seen values move to the end; the
    # distinct count is a handful, so the linear remove costs nothing.
    order: list[str] = []
    try:
        with open(transcript_path, "rb") as handle:
            for raw in handle:
                if b'"cwd"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                cwd = record.get("cwd")
                if isinstance(cwd, str) and cwd:
                    if cwd in order:
                        order.remove(cwd)
                    order.append(cwd)
    except OSError:
        return []
    order.reverse()
    return order


def resolve_cwd_from_transcript(transcript_path: str) -> str | None:
    """``resolve_cwd`` over the WHOLE transcript, not a caller's window.

    The two capture paths see different windows of the same session — one the
    whole file, the other only the bytes since its cursor — and a subset window
    cannot in general agree with a superset one. When the recent cwds are all
    non-repos (deleted worktrees, most often) the delta window falls back to a
    non-repo while the whole file finds an older repo further back, and the two
    paths route differently: the exact disagreement that forks a conversation.

    So the FIRST resolution of a session is taken from the whole file by both
    paths, which makes them agree by construction. It costs one full read per
    session, not per turn — every later flush reads the pin instead.
    """
    candidates = cwds_in_transcript(transcript_path)
    if not candidates:
        return None
    for cwd in candidates[:_MAX_CWD_PROBES]:
        if _is_repo(cwd):
            return cwd
    return candidates[0]


# ── the pin ───────────────────────────────────────────────────────────

def _path(conversation_id: str) -> Path:
    # Slashes and dots cannot reach here — conversation ids are session uuids
    # or a chain root that is one — but a traversal in a filename is worth one
    # cheap guard rather than an argument about who validated it.
    safe = "".join(c for c in conversation_id if c.isalnum() or c in "-_")
    return STATE_DIR / f"{safe}.scope.json"


def read_pin(conversation_id: str) -> dict:
    """This conversation's pinned routing, or ``{}``. Never raises."""
    if not conversation_id:
        return {}
    try:
        data = json.loads(_path(conversation_id).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def pin(conversation_id: str, **fields) -> None:
    """Merge ``fields`` into this conversation's pin, under a lock.

    Locked because both capture paths write here and a read-modify-write that
    loses the other's update would drop exactly the field that keeps them
    agreeing. ``None`` values are dropped rather than stored, so a flush that
    resolved less than a previous one cannot erase what it did not learn.
    """
    if not conversation_id:
        return
    keep = {k: v for k, v in fields.items() if v is not None}
    if not keep:
        return
    lock_fd: int | None = None
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(STATE_DIR / f"{_path(conversation_id).stem}.lock",
                          os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = read_pin(conversation_id)
        state.update(keep)
        state["at"] = time.time()
        atomic_write.publish(_path(conversation_id), json.dumps(state))
    except (OSError, ValueError, TypeError):
        pass  # a pin is never worth failing a flush over
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass


def clear_room(conversation_id: str) -> None:
    """Forget the pinned brain — ONLY for a brain the server disowned.

    Keeps ``cwd``/``namespace``: the directory did not stop being real just
    because the room did.
    """
    if not conversation_id:
        return
    state = read_pin(conversation_id)
    if not state:
        return
    lock_fd: int | None = None
    try:
        lock_fd = os.open(STATE_DIR / f"{_path(conversation_id).stem}.lock",
                          os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state = read_pin(conversation_id)
        state.pop("brain_id", None)
        state.pop("org_id", None)
        state["at"] = time.time()
        atomic_write.publish(_path(conversation_id), json.dumps(state))
    except (OSError, ValueError, TypeError):
        pass
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass


def apply_pin(conversation_id: str, room, cwd, namespace):
    """Reconcile a freshly resolved routing against the pinned one.

    Returns the ``(room, cwd, namespace)`` to actually use, and records
    anything newly learned.

    The pin WINS on the room. A resolution that came back empty is the failure
    this exists to absorb, and a resolution that came back DIFFERENT would move
    the conversation's identity mid-session — which is the fork, arriving by a
    different route. Whichever flush resolved first decides, and the rest of the
    session follows it.
    """
    pinned = read_pin(conversation_id)
    if pinned.get("brain_id"):
        room = {"brain_id": pinned["brain_id"], "org_id": pinned.get("org_id")}
    elif room and room.get("brain_id"):
        pin(conversation_id, brain_id=room.get("brain_id"),
            org_id=room.get("org_id"))
    # cwd and namespace are pinned too, and for the same reason: the namespace
    # becomes the directive scope, and a flush that loses it mints directives
    # that recall in EVERY repo. Unlike the room these are cheap to relearn, so
    # a fresh value is preferred and the pin only fills a gap.
    cwd = cwd or pinned.get("cwd") or None
    namespace = namespace or pinned.get("namespace") or None
    pin(conversation_id, cwd=cwd, namespace=namespace)
    return room, cwd, namespace
