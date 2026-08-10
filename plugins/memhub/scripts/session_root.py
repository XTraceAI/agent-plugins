#!/usr/bin/env python3
"""Resolve the CONVERSATION id for a Claude Code session (stdlib only).

Capture used to send ``conversation_id = session_id``, which is wrong the
moment a session is re-entered. Claude Code does not resume a session in
place: leaving one idle and coming back mints a NEW session id and a NEW
transcript file, into which it COPIES the prior records. Server-side the
conversation row is upserted on ``(workspace_id, user_id, source_id)`` with
``source_id`` being exactly what we send — so the returning user's next
message opened a SECOND conversation, carrying the same copied title as the
first. Two identically-named conversations in the sessions tab, one holding
the work up to the pause and one holding everything after it, with the
overlap extracted twice because the agentic ingest's dedup key
(``(workspace_id, conv_id, source_message_id)``) is conversation-scoped.

**What is stable across a resume is the RECORD UUIDS.** The copy preserves
them (only ``sessionId`` is restamped, and ``parentUuid`` is re-rooted to
null, so nothing in the new file names the session it came from). So the
transcript's own head records are the anchor: index them the first time a
session is seen, and a later session that starts with the same uuids
resolves back to the id the first one registered.

**Why this cannot swallow ``/clear``.** The anchor is CONTENT, never a clock
and never a session id. A cleared session's records are fresh v4 uuids that
appear in no index entry, so the lookup misses and the session becomes its
own root — a new conversation, which is the intent of clearing. Only a
transcript that literally re-contains another's records can link to it, and
only a resume produces that. (A ``/clear`` that stays in the SAME file and
session id — rare, but it happens — already fed one conversation before this
module existed and still does; that behaviour is untouched.)

Branches merge deliberately. A rewind fork also copies the prefix, so it
resolves to the same root and interleaves into one conversation, deduped by
record uuid. That is the chosen semantics: one Claude Code session, one
conversation, however many processes it took.

**Failure is always "behave like before".** Every error path returns the
session id, which is what capture sent for its whole life up to this commit.
A capture hook must never fail loudly, and a mis-resolved conversation is
worse than an unresolved one.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

import atomic_write

# Beside the per-turn cursors — one directory holds all of capture's state,
# and ``capture_health`` already scans it by suffix rather than by name.
STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "turnflush"
INDEX = STATE_DIR / "roots.json"
_LOCK = STATE_DIR / "roots.lock"

# How many uuid-bearing records at the head of a transcript are indexed.
#
# More than one, because the first is not reliably present in the copy: a
# resume taken right after ``/compact`` heads the new file with a freshly
# minted summary record, and the copied originals only start after it. Taking
# a window rather than a single record is what lets those still link.
#
# Not unbounded, because every extra anchor is an index entry per session and
# a longer scan on a cold session, for a rapidly shrinking chance of being the
# one that matches. Forty covers the sidecar preamble (mode, permission-mode,
# file-history snapshots) plus the first several real turns.
ANCHOR_SCAN = 40

# Index hygiene. Entries are tiny (a uuid, an id, a timestamp) but they are
# never otherwise deleted, and a heavy user opens thousands of sessions a
# year. Both bounds are generous: a resume happens within days, so an anchor
# older than this has no chain left to join.
_MAX_AGE_S = 90 * 24 * 3600
_MAX_ENTRIES = 20_000


def head_anchors(transcript_path: str, limit: int = ANCHOR_SCAN) -> list[str]:
    """The first ``limit`` record uuids in the transcript, in file order.

    Reads the HEAD of the file rather than whatever delta a caller happens to
    hold, so the per-turn hook (which sees only bytes since its cursor) and
    the SessionEnd backstop (which sees everything) compute the same anchor
    for the same session and cannot disagree about its conversation.

    Sidecar records — ``mode``, ``custom-title``, the file-history snapshots
    — carry no uuid and are skipped rather than counted; they are exactly the
    preamble that would otherwise consume the window.
    """
    found: list[str] = []
    try:
        with open(transcript_path, "rb") as handle:
            for raw in handle:
                if not raw.endswith(b"\n"):
                    break  # partial trailing write — never a head record
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                uid = record.get("uuid")
                if isinstance(uid, str) and uid:
                    found.append(uid)
                    if len(found) >= limit:
                        break
    except OSError:
        return []
    return found


def _load() -> dict:
    try:
        data = json.loads(INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    anchors = data.get("anchors")
    return anchors if isinstance(anchors, dict) else {}


def _prune(anchors: dict, now: float) -> dict:
    """Drop expired entries, then the oldest until the cap is met."""
    fresh = {
        uid: entry for uid, entry in anchors.items()
        if isinstance(entry, list) and len(entry) == 2
        and isinstance(entry[0], str)
        and isinstance(entry[1], (int, float))
        and now - entry[1] <= _MAX_AGE_S
    }
    if len(fresh) <= _MAX_ENTRIES:
        return fresh
    ordered = sorted(fresh.items(), key=lambda kv: kv[1][1], reverse=True)
    return dict(ordered[:_MAX_ENTRIES])


def resolve(session_id: str, transcript_path: str) -> str:
    """The conversation id to send for this session.

    The session's own id when it starts fresh content, or the id already
    registered by the session it was resumed from.

    Read-modify-write under a dedicated ``flock``, because parallel sessions
    in different repos share this one file and an unlocked merge would lose
    whichever registration finished second — leaving a chain unindexed and
    splitting it on the next resume, which is the bug this exists to fix.

    The lock is separate from the per-turn flush's, and is taken INSIDE it at
    the only site that holds both, so the two can never be acquired in
    opposing orders.
    """
    if not session_id:
        return session_id
    anchors = head_anchors(transcript_path)
    if not anchors:
        # Nothing to key on: a transcript with no uuid-bearing record yet.
        # Registering nothing means the next flush, which will have them,
        # gets the first word.
        return session_id

    lock_fd: int | None = None
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        now = time.time()
        index = _load()
        root = session_id
        for uid in anchors:
            entry = index.get(uid)
            if isinstance(entry, list) and entry and isinstance(entry[0], str) \
                    and entry[0]:
                root = entry[0]
                break  # earliest anchor wins; later ones may be a re-root
        # Register EVERY anchor, hit or miss. On a miss this claims the chain
        # for a brand-new session. On a hit it re-points the copied uuids at
        # the same root and adds whatever the copy trimmed away, so a chain
        # survives being resumed repeatedly even as the head records drift.
        for uid in anchors:
            index[uid] = [root, now]
        atomic_write.publish(
            INDEX, json.dumps({"v": 1, "anchors": _prune(index, now)}))
        return root
    except (OSError, ValueError, TypeError):
        # Degrade to the pre-fix behaviour rather than lose the flush.
        return session_id
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)  # closing releases the flock
            except OSError:
                pass
