#!/usr/bin/env python3
"""Per-turn session flush — fired by the Stop hook after every assistant turn.

Ships only the transcript BYTES WRITTEN SINCE THE LAST SUCCESSFUL FLUSH, using
``flush="auto"`` so the server stores each turn durably on arrival but extracts
only once the accumulated span is batch-sized. Extracting per turn would hand
the batch extractor a 2-5 event fragment and shred the episode boundaries it
derives; buffering server-side keeps episodes whole while making each turn
durable the moment it happens.

**Why a byte cursor and not "re-send the transcript".** Re-sending the whole
file every turn needs no client state, and the server's uuid watermark makes it
correct — but it re-uploads the entire session on each turn, so total upload
grows with the SQUARE of the session length. On a long coding session that is
gigabytes to say what a few kilobytes would. So the client sends its delta and
the server holds what it has not yet extracted.

**The cursor advances only on success.** Every uncertainty resolves toward
re-sending: no cursor, a shrunken file, a failed call, an ambiguous response.
The server dedups incoming records against (extracted ∪ buffered), so
over-sending costs bandwidth while under-sending leaves a gap nothing will ever
notice. That asymmetry is the whole reason this file is written the way it is.

**The delta is BOUNDED, and the cursor tracks what was sent, not what was
read.** Individual records have long been capped; the aggregate was not. So a
session whose pending delta outgrew the server's request limit got a 413 on
every turn, could not advance a cursor past bytes that were never sent, and
re-sent the same ever-growing delta forever — per-turn capture dead for the
rest of that session's life, on exactly the long sessions worth keeping. The
delta now goes through the same splitter the whole-transcript paths use and
only the first slice ships, with the cursor landing on the last record in it.
A backlog drains over the next few turns because steady-state deltas are
kilobytes; ``flush_session`` remains the backstop for whatever a session ends
before reaching.

**One flush per session at a time.** The server expects a session's turns to
arrive in order, so an advisory ``flock`` keeps two hooks from overlapping when
one turn finishes before the previous flush has returned. Losing that race is
not an error: the cursor did not move, so the next turn's flush carries both.

Discipline mirrors the other capture hooks: THIS SCRIPT NEVER FAILS LOUDLY.
Any error exits 0 quietly — memory capture must never disturb the session.

Auth = the plugin's OWN token cache (shared ``_memhub_auth``) — a different
store from the /mcp connector's, so being connected in /mcp does NOT mean this
hook can authenticate. Non-interactive: a per-turn background hook must never
pop a browser, so it can only consume a token ``/memhub:login`` already minted.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import portable_lock  # noqa: E402

# Stdlib-only and side-effect free, so it imports at module scope like the
# rest of the cursor/tail logic and stays testable under a bare python3.
from session_title import (  # noqa: E402
    custom_title,
    generated_title,
    prompt_title,
)
from redact import redact_records, redact_text  # noqa: E402
import transcript_chunks  # noqa: E402
from transcript_filter import (  # noqa: E402
    elide_oversized_tool_results,
    is_command_wrapper,
)

# All at module scope now. These used to be deferred into :func:`_flush`
# because ``_memhub_auth`` dragged in the mcp SDK and this module has to stay
# importable under a bare python3 — that is what lets the cursor/tail/lock
# logic, where the silent failures live, be tested without the dependency.
# Nothing here needs the SDK any more, so the indirection went with it.
import atomic_write  # noqa: E402
import mcp_http  # noqa: E402
import pr_provenance  # noqa: E402
from _memhub_auth import resolve_bearer  # noqa: E402
from brain_resolve import is_missing_brain, resolve_repo_brain  # noqa: E402
from room_map import env_for_url, forget_room  # noqa: E402

STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "turnflush"

# Cap on one flush's whole round-trip. The flock is held for its duration and
# the prefilter skips while held, so this is also the longest a hung server
# can stall capture for the session.
_DEFAULT_FLUSH_TIMEOUT_S = 60.0

# Floor for the per-turn payload cap (see ``_shrink_slice``). Below this a 413
# has stopped meaning "the batch is too big" and started meaning "this ONE
# record is", and halving further only delays the step-over that actually
# unsticks the session. Well under any plausible request limit, and still
# roomy enough to carry an ordinary turn whole.
_MIN_SLICE_BYTES = 256_000

# A refusal for SIZE that did not arrive as an HTTP 413. Today's server answers
# with the status — verified live — and that is the path worth trusting. But a
# proxy in front of it, or a server that reports the refusal inside a 200
# JSON-RPC envelope or as an `isError` tool result, delivers the same thing
# with no status to branch on; treated as a generic fault, the cap never comes
# down and the session stalls exactly as it did before the batch was bounded.
# Phrases only, and no bare "413": a status code is a plausible substring of an
# unrelated message, and mistaking some other failure for this one puts the
# session on the shrink ladder for no reason.
_SIZE_REFUSAL_PHRASES = ("too large", "too big", "entity too large",
                         "payload_too_large", "body exceeded",
                         "request body limit")


def _is_size_refusal(detail: str) -> bool:
    """Whether an error the transport could not classify is a size refusal."""
    text = (detail or "").lower()
    return any(phrase in text for phrase in _SIZE_REFUSAL_PHRASES)


def _flush_timeout_s() -> float:
    """The round-trip cap, from the env with the default as a floor.

    Read at CALL time and never allowed to raise. Parsing this at import
    time meant a non-numeric or empty override crashed the module before the
    handler that keeps this hook quiet could run — a traceback in the user's
    session, which is the one thing this script must never produce.

    Zero or negative is rejected rather than honoured: it would time every
    flush out instantly, so the cursor would never advance and per-turn
    capture would be silently dead. ``MEMHUB_TURN_FLUSH=0`` is how you turn
    this off; a timeout of nothing is a misconfiguration, not an intent.
    """
    raw = (os.environ.get("MEMHUB_TURN_FLUSH_TIMEOUT_S") or "").strip()
    if not raw:
        return _DEFAULT_FLUSH_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_FLUSH_TIMEOUT_S
    return value if value > 0 else _DEFAULT_FLUSH_TIMEOUT_S

# UI bookkeeping the client writes for its own display: mode switches, the
# generated title, queue state. They carry no content and no ``message``, and
# a batch made only of them is REJECTED by the server — with no ``message``
# among the records it reads the batch as plain chat and fails role
# validation. Consuming them avoids a round-trip that could never succeed.
#
# ``attachment`` is deliberately absent from this set. It has no ``message``
# either, so an attachment-only delta is rejected the same way — but an
# attachment is real content (a pasted file, an image). Leaving the cursor
# pinned means the next turn re-sends it alongside the message records that
# make the batch valid. One wasted call beats dropping the file.
_INERT_RECORD_TYPES = frozenset({
    "mode", "last-prompt", "pr-link", "queue-operation",
    "permission-mode", "ai-title", "custom-title",
    "file-history-snapshot", "file-history-delta",
})


def _log(msg: str) -> None:
    print(f"[memhub-turn] {msg}")


# ── cursor ────────────────────────────────────────────────────────────

def _read_state(session_id: str) -> dict:
    try:
        state = json.loads((STATE_DIR / f"{session_id}.json").read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _read_cursor(state: dict, size: int) -> int:
    """Byte offset to resume from — 0 whenever the cursor cannot be trusted.

    A file SMALLER than the cursor means the transcript was rewritten under us,
    so the offset points into different content and every byte must be re-sent.
    Returning 0 is always safe (the server dedups); returning a stale offset
    would skip records permanently.
    """
    try:
        offset = int(state.get("offset", 0))
    except (ValueError, TypeError):
        return 0
    return offset if 0 <= offset <= size else 0


def _save_state(session_id: str, **fields) -> None:
    """Merge ``fields`` into the session's state and write it atomically.

    MERGE, not replace: the session remembers several things resolved at
    different moments — the cursor, the repo it ran in, its namespace, its
    title — and a flush that only knows some of them must not erase the rest.

    Atomic, because a torn read that reported a larger offset than was actually
    shipped would skip those records for good.
    """
    state = _read_state(session_id)
    state.update(fields)
    state["at"] = time.time()
    # Atomic even though the flock makes this hook the only writer of this file
    # today: the state carries the cursor, and a torn one means re-sent or
    # skipped records. Cheap insurance against the next hook that needs to
    # write here — the SessionEnd backstop briefly did, and the lost-update it
    # caused is why it now keeps its own file.
    #
    # 0600 by default: not a secret exactly, but it holds the session title, the
    # repo path and server error text, none of which needs to be world-readable.
    atomic_write.publish(STATE_DIR / f"{session_id}.json", json.dumps(state))


# Everything below exists because this hook is `async: true`, and Claude Code
# surfaces an async hook's stdout NOWHERE — not to the user, not to the agent.
# So every failure path here ended in a print nobody could read, and the state
# dir could not tell the two cases apart either: a session whose flushes all
# failed left a `.lock` and no `.json`, byte-identical to a session where the
# hook never ran.
#
# The failure is written down where a SYNCHRONOUS hook can find it:
# `capture_health.py` reads these fields on SessionStart and reports them via
# `systemMessage`, the one channel that reaches the user.

def _mark_failure(session_id: str, reason: str, detail: str = "") -> None:
    """Record WHY this flush did not ship, for `capture_health.py` to surface.

    Never raises and never touches ``offset``: a breadcrumb must not be able to
    corrupt the cursor it is reporting on. ``reason`` is a stable slug the
    health check branches on; ``detail`` is truncated free text for the human.
    """
    try:
        _save_state(session_id, last_error=reason,
                    last_error_detail=detail[:200] or None,
                    last_error_at=time.time())
    except OSError:
        pass  # a breadcrumb is never worth failing the hook over


def _mark_success(session_id: str, **fields) -> None:
    """Save ``fields`` and clear any recorded failure in the same write.

    One write, not two: a success that advanced the cursor but left the error
    behind would have the health check crying wolf for the rest of the session,
    and a crash between two writes would make that permanent.
    """
    _save_state(session_id, last_ok_at=time.time(), last_error=None,
                last_error_detail=None, last_error_at=None, **fields)


# ── lock ──────────────────────────────────────────────────────────────

def _acquire(session_id: str) -> int | None:
    """Claim this session's flush slot via ``flock``, or return None.

    Returns the held file descriptor — the caller must keep it OPEN for the
    lock to hold, and closing it releases.

    ``flock`` rather than an ``O_EXCL`` lockfile because the kernel owns the
    lifetime: it releases on process exit however that happens, including a
    SIGKILL. A lockfile needs staleness heuristics to recover from a crashed
    flush, and reclaiming a stale one is inherently racy — two hooks can both
    judge it abandoned and both take it, which is exactly the overlap the lock
    exists to prevent. There is no such thing as a stale flock.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(STATE_DIR / f"{session_id}.lock",
                 os.O_CREAT | os.O_RDWR, 0o600)
    try:
        portable_lock.lock_exclusive(fd, blocking=False)
    except OSError:
        os.close(fd)
        return None  # another flush holds it; ours is redundant anyway
    return fd


# ── flush ─────────────────────────────────────────────────────────────

def _read_tail(transcript: str, offset: int) -> tuple[list[dict], list[int], int]:
    """Records appended since ``offset``, the byte offset each one ENDS at, and
    the offset actually consumed.

    Opened in binary and decoded per line so the returned offsets are true BYTE
    counts — a character count would drift on any non-ASCII turn and silently
    mis-seek the next flush.

    The final line is routinely a PARTIAL write, because Claude Code is still
    appending while this runs. That is the expected case, not corruption: stop
    at the last complete line and leave the cursor before the partial one, so
    the next flush picks the record up whole.

    ``ends`` is what lets a flush that ships only PART of the delta still move
    the cursor — to the last record it actually sent. Without it the cursor can
    only move by the whole delta, which is the all-or-nothing that made an
    oversized delta unsendable for the rest of a session's life.
    """
    records: list[dict] = []
    ends: list[int] = []
    consumed = offset
    with open(transcript, "rb") as fh:
        fh.seek(offset)
        for raw in fh:
            if not raw.endswith(b"\n"):
                break  # partial trailing write — resume here next turn
            consumed += len(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue  # unparseable but complete: skip, keep the offset
            records.append(record)
            # Appended together, never independently: an ``ends`` that drifted
            # out of step with ``records`` would move the cursor past a record
            # that was never sent — the one failure this whole module is
            # written to make impossible.
            ends.append(consumed)
    return records, ends, consumed


def _slice_bytes(state: dict) -> int:
    """Bytes one per-turn payload may carry.

    ``transcript_chunks.DEFAULT_CHUNK_BYTES`` — the same figure the whole-
    transcript paths use — unless a 413 already taught THIS session that this
    server's limit is lower. Clamped on the way out so a hand-edited or
    corrupt state file cannot widen the cap past the default or narrow it below
    the floor.
    """
    try:
        value = int(state.get("slice_bytes") or 0)
    except (TypeError, ValueError):
        return transcript_chunks.DEFAULT_CHUNK_BYTES
    if value <= 0:
        return transcript_chunks.DEFAULT_CHUNK_BYTES
    return max(_MIN_SLICE_BYTES,
               min(value, transcript_chunks.DEFAULT_CHUNK_BYTES))


def _record_bytes(record) -> int:
    """Serialized size of one record — the same measure ``transcript_chunks``
    slices by, so the two agree about what "too big" means.

    Never raises, and an unmeasurable record reports 0: every use of this is a
    decision about whether to DROP a record, and an unknown size must resolve
    toward keeping it.
    """
    try:
        return len(json.dumps(record, separators=(",", ":"), default=str))
    except (TypeError, ValueError):
        return 0


def _bounded(sendable: list, ends: list[int], consumed: int,
             chunk_bytes: int, one_record: bool = False) -> tuple[list, int]:
    """``(what to send now, the byte offset it ends at)``.

    Individually-oversized records were already handled upstream by
    ``elide_oversized_tool_results``. What was never bounded is the AGGREGATE:
    once a session's pending delta crossed the server's request limit, every
    flush returned 413, the cursor could not advance past what was never sent,
    and the same ever-growing delta was re-sent every turn — per-turn capture
    dead for the rest of that session. Six sessions on one machine at once.

    The same splitter ``flush_session`` and ``import_session`` use, so there is
    exactly one definition of "a payload one call can carry". Only the FIRST
    slice goes: this hook is one call per turn, on a 60s budget, holding the
    session's flock while the prefilter skips behind it. A backlog still
    drains, because steady-state per-turn deltas are kilobytes — 3.5 MB a turn
    catches up far faster than a session produces.

    When the delta fits one payload the offset is the FULL consumed span, not
    the last record's end, so records the filters dropped are consumed too
    rather than re-read on every later turn.

    ``one_record`` is the last rung: a server that refused even a floor-sized
    slice gets one record at a time, the smallest payload that exists. Without
    it the cap bottoms out and the delta is re-sliced identically every turn —
    the same permanent stall, moved from the server's limit down to our floor.
    """
    if one_record:
        first = sendable[:1]
    else:
        # Only the first slice is ever sent, so only the first is built.
        payloads = transcript_chunks.slices(sendable, chunk_bytes, max_slices=1)
        first = payloads[0] if payloads else []
    first = _with_a_message(first, sendable)
    if len(first) >= len(sendable):
        return sendable, consumed
    return first, ends[len(first) - 1]


def _carries_message(record) -> bool:
    return isinstance(record, dict) and isinstance(record.get("message"), dict)


def _with_a_message(batch: list, sendable: list) -> list:
    """``batch``, widened until it carries at least one message-bearing record.

    The server reads a batch with no ``message`` among its records as plain
    chat and fails role validation — the same contract ``_INERT_RECORD_TYPES``
    is written around. Before the delta was bounded that could not bite: the
    whole delta went in one request, so the records that make it valid were
    always in it, and ``attachment`` is deliberately OUTSIDE the inert set on
    exactly that reasoning ("the next turn re-sends it alongside the message
    records that make the batch valid").

    Bounding broke that assumption. A first slice of nothing but attachments or
    sidecars is rejected, and the rejection is ``server_rejected``, not a 413 —
    so it neither advances the cursor nor reaches the shrink ladder. The next
    turn slices the identical delta identically and is refused again, forever:
    the permanent stall this module was changed to remove, reintroduced through
    a different door.

    Widening can overshoot the byte cap. That is the right trade and the one
    ``transcript_chunks.slices`` already makes for an oversized record: a
    payload the server MIGHT refuse beats one it MUST refuse, and this is no
    bigger than the single request the unbounded code sent every turn anyway.

    When the delta carries no message-bearing record at all, the batch is
    returned untouched — there is nothing to widen to, and the pre-existing
    behaviour (send it, be refused, keep the cursor, carry it again next turn
    once a real record has arrived) is already the documented answer.
    """
    if any(_carries_message(r) for r in batch):
        return batch
    for i in range(len(batch), len(sendable)):
        if _carries_message(sendable[i]):
            return sendable[:i + 1]
    return batch


def _shrink_slice(session_id: str, state: dict, batch: list,
                  batch_consumed: int) -> None:
    """React to a refusal for SIZE: send less next turn, or step over a record
    that can never be sent.

    The ladder is: halve the cap → one record per turn → step over that record
    if it is the thing that is too big. Each rung exists because the one above
    it has run out, and the last two are what keep this from being the original
    bug at a lower threshold.

    **Halving is sticky**, because a request limit is a property of the server:
    re-probing the full cap after every success buys one guaranteed-wasted
    round trip per turn and learns nothing new.

    **A single-record batch skips the ladder entirely.**
    ``transcript_chunks.slices`` never splits inside a record, so
    ``slices([R], n) == [[R]]`` for every ``n`` — lowering the cap cannot
    change what the next turn sends. Walking the ladder anyway cost four
    full-size uploads, each one refused, with the whole delta frozen behind
    them.

    **Stepping over a record is gated on the RECORD's own size**, not on the
    cap having bottomed out. Inferring "unsendable" from the cap alone deleted
    a 67-byte record on one spurious 413 — and because the cap is sticky, a
    session that had ever been driven to the floor stayed in delete-on-413 mode
    for the rest of its life, which is precisely the silent permanent loss the
    cursor rule exists to prevent. A record above the floor is one no payload
    we can build will carry; below it, the server is refusing something
    minimal, which says nothing about the record. So: drop the first, keep the
    second, and let the SessionEnd backstop have it.

    Never raises: it is reached from a failure path that must still exit 0.
    """
    if len(batch) <= 1:
        record = batch[0] if batch else None
        if record is not None and _record_bytes(record) > _MIN_SLICE_BYTES:
            _log("one record is larger than this server will accept at any "
                 "size — skipping it so the rest of the session keeps "
                 "capturing")
            # One write, not two. The failure is already breadcrumbed by the
            # caller; this replaces the detail with what actually happened and
            # advances the cursor in the same atomic publish, so a crash
            # between them cannot leave a cursor that skipped a record nobody
            # knows about.
            _save_state(session_id, offset=batch_consumed,
                        last_error="payload_too_large",
                        last_error_detail="skipped one record the server "
                                          "would not accept at any size",
                        last_error_at=time.time())
            return
        # Small, and still refused. Nothing smaller can be built, so per-turn
        # capture cannot proceed — but the record is NOT the problem, and
        # deleting an ordinary turn on one bad answer is the loss this rule
        # exists to prevent. Keep it. The breadcrumb stands and SessionEnd
        # still captures the session whole.
        _log("the server refused a payload this small — keeping the record; "
             "session-end capture still applies")
        return
    current = _slice_bytes(state)
    if current > _MIN_SLICE_BYTES:
        shrunk = max(_MIN_SLICE_BYTES, current // 2)
        _log(f"payload cap {current:,}B was still too large — halving to "
             f"{shrunk:,}B for the rest of this session")
        _save_state(session_id, slice_bytes=shrunk)
        return
    # At the floor and still refused as a BATCH. Halving is spent, but one
    # payload smaller than a floor-sized slice does exist: a single record.
    # Without this rung the delta is re-sliced identically every turn and the
    # session stalls for good — the same defect this module was changed to
    # remove, relocated from the server's 4 MiB limit down to our own floor.
    if not state.get("one_record"):
        _log("still refused at the smallest slice — sending one record per "
             "turn for the rest of this session")
        _save_state(session_id, one_record=True)
        return
    # The last rung is spent, and the two requirements are now jointly
    # unsatisfiable: a batch must carry a message-bearing record for the server
    # to parse it at all, and must be under the limit for the server to accept
    # it — and because the cursor is a single byte offset, only a PREFIX can
    # ever be sent. When the shortest message-bearing prefix is over the limit,
    # no valid batch exists, so `_with_a_message` widening past `one_record` is
    # not a bug to route around: there is nothing smaller that would be legal.
    #
    # Retrying it re-sends the identical payload every turn forever, which is
    # the stall this module exists to remove. Go dormant instead: the prefilter
    # reads this flag and stops spawning doomed flushes. The
    # `payload_too_large` breadcrumb already written stands, so the health
    # banner still says what happened.
    #
    # Dormancy is the least-bad answer here, NOT a rescue, and the difference
    # matters to whoever reads this next. SessionEnd usually does recover the
    # session — it re-sends the whole transcript against its own cursor — but
    # it is not a guarantee in this particular state: `flush_session` slices at
    # a fixed `DEFAULT_CHUNK_BYTES`, has no 413 handling of its own, and stops
    # on the first rejected slice, so a prefix that is unsendable here can be
    # unsendable there too. Nothing this hook can do changes that: the cursor
    # is one byte offset, so only a PREFIX can be sent, and a prefix that must
    # carry a message record yet cannot fit the limit has no legal form.
    # Retrying would not capture it either — it would only burn a round trip
    # per turn. Stepping over the prefix WOULD rescue the rest of the session,
    # at the cost of deleting an `attachment`, which this module deliberately
    # keeps outside `_INERT_RECORD_TYPES` so it is never dropped; widening the
    # step-over that far is a policy call and is deliberately not made here.
    _log("the smallest legal batch is still refused — per-turn capture is "
         "dormant for this session; session-end capture still applies")
    _save_state(session_id, unsupported=True)


def _titles(records: list[dict], state: dict) -> tuple[str | None, str | None]:
    """``(title_to_send, custom_title_to_remember)`` for this delta.

    Three sources, in strict precedence — see ``session_title`` for the
    measurements behind the order:

    1. the name the USER gave the session, which the client keeps re-emitting
       the stale generated title alongside, so it must win by TYPE rather than
       by whichever record came last;
    2. the name Claude Code generated, freshest first — it is regenerated as
       the session develops, so this delta's beats the remembered one;
    3. failing both, the session's first real prompt. Only a client that never
       writes a title record at all reaches here, which in practice means a
       headless run — without it those import unnamed.

    Each is remembered, so a delta that carries no title at all keeps sending
    the one already resolved rather than reverting to a fresh guess.

    Harvested even from deltas we do not send: the title records are inert, so
    a title often arrives in a batch that is consumed without a server call.
    Remembering it there is what makes it available to the next real flush.
    """
    custom = custom_title(records) or state.get("custom_title") or None
    generated = generated_title(records) or state.get("title") or None
    title = custom or generated or prompt_title(records) or None
    # Redacted HERE, at the source, and not at the send site. A title is derived
    # from the RAW records — the redaction downstream only covers `sendable` —
    # so a session whose first prompt is `export MEMHUB_TOKEN=mhk_…` would ship
    # its key as the conversation's NAME: the most visible field there is, and
    # metadata that redaction was supposed to have covered. Doing it here also
    # keeps the copy persisted into state clean, which matters because that copy
    # is re-sent on every later flush.
    return (redact_text(title) if title else None,
            redact_text(custom) if custom else None)


def _namespace(records: list[dict]) -> tuple[str | None, str | None]:
    """(cwd, git-remote basename) — the session's working-context scope.

    Resolved client-side from the transcript's cwd, never server-side: a
    worktree directory's basename would stamp a scope that HIDES directives
    from the canonical repo's recalls.
    """
    cwd = next((r.get("cwd") for r in records
                if isinstance(r, dict) and isinstance(r.get("cwd"), str)
                and r.get("cwd")), None)
    if not cwd:
        return None, None
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2,
        )
        url = out.stdout.strip()
        if out.returncode == 0 and url:
            return cwd, url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    return cwd, None


async def _flush(session_id: str, transcript_path: str) -> None:

    size = os.path.getsize(transcript_path)
    state = _read_state(session_id)
    offset = _read_cursor(state, size)
    records, ends, consumed = _read_tail(transcript_path, offset)
    if not records:
        return

    pending_pr_urls, accepted_pr_urls, missing_pr_urls = (
        pr_provenance.queued_urls(state, records)
    )
    if missing_pr_urls:
        _log(f"{missing_pr_urls} direct gh pr create result(s) had no "
             "canonical GitHub PR URL")
    if pending_pr_urls or accepted_pr_urls:
        # Persist URL telemetry before network work. Its transcript cursor stays
        # pinned until the backend explicitly acknowledges the URL below.
        _save_state(
            session_id,
            pending_pr_urls=pending_pr_urls,
            accepted_pr_urls=accepted_pr_urls,
        )

    # Only user / assistant / attachment records carry ``cwd`` — the UI sidecar
    # types (mode, last-prompt, ai-title, …) never do. A delta made up solely of
    # sidecars therefore resolves no cwd, and without the remembered one this
    # flush would route to personal memory AND key the conversation on the
    # un-namespaced source_id — splitting one session across two conversations,
    # half in the repo's room and half outside it. So the first flush that
    # resolves a cwd remembers it for the rest of the session.
    # Checked BEFORE resolving cwd, because resolving shells out to git and an
    # inert delta should cost nothing at all.
    # Slash-command bookkeeping never leaves the machine. Dropped from what is
    # SENT, not from what is read: the metadata harvest below still sees every
    # record, and the cursor still advances past these, because they are
    # deliberately never shipped rather than deferred.
    # Redact AFTER filtering and before anything leaves the machine. Applied to
    # what is SENT rather than what is read, so the cursor still advances past
    # a record whose secret was stripped — the alternative would pin the cursor
    # on any turn that mentioned a key and stall capture permanently.
    # Oversized tool results are elided BEFORE redaction, so the secret scan
    # runs over what will actually be sent, and before slicing would matter:
    # a single record the server can never accept would otherwise pin the
    # cursor and stall this session's capture for good.
    # INDEX-PRESERVING, so every sendable record still knows the transcript
    # byte offset it ends at. Dropping the wrappers through the predicate
    # ``drop_command_wrappers`` is built from — rather than calling the dropper
    # and losing which record came from where — is what lets a bounded batch
    # advance the cursor to the last record it actually shipped.
    # ``session_title`` already reads the filter this way, so this is the
    # module's own idiom rather than a second mechanism.
    # ``elide_oversized_tool_results`` and ``redact_records`` are both 1:1,
    # including on their never-raise fallbacks (each returns the list it was
    # handed), so the correspondence survives them.
    keep = [i for i, r in enumerate(records) if not is_command_wrapper(r)]
    sendable = redact_records(
        elide_oversized_tool_results([records[i] for i in keep])
    )
    sendable_ends = [ends[i] for i in keep]

    if not sendable or all(
        isinstance(r, dict) and r.get("type") in _INERT_RECORD_TYPES
        for r in sendable
    ):
        # The title usually arrives in exactly this kind of batch, so read it
        # before dropping the records on the floor.
        inert_title, inert_custom = _titles(records, state)
        fields = {"offset": consumed}
        if inert_title:
            fields["title"] = inert_title
        if inert_custom:
            fields["custom_title"] = inert_custom
        # Plain ``_save_state``, NOT ``_mark_success`` — deliberately. This
        # branch returns above ``resolve_url_and_auth`` and never touches the
        # network, so reaching it says nothing about whether the server or the
        # credential is healthy. Clearing ``last_error`` here would retract a
        # real, still-unresolved failure on the strength of a purely local
        # no-op, and inert deltas are common enough (the title arrives in one)
        # that a broken session would routinely erase its own alarm. Only a
        # committed round-trip is evidence of recovery, which is why exactly
        # one call site clears the error.
        _save_state(session_id, **fields)
        return

    # Bounded AFTER the inert check, so a delta made only of sidecars is
    # consumed whole rather than sliced — and before anything costs a round
    # trip, because the size of what we are about to send is the reason this
    # hook stopped working on long sessions.
    batch, batch_consumed = _bounded(sendable, sendable_ends, consumed,
                                     _slice_bytes(state),
                                     bool(state.get("one_record")))

    # Each falls back INDEPENDENTLY. Tying the namespace's fallback to the cwd's
    # left a real gap: a delta can carry a cwd while git resolution fails on it
    # (a timeout, or a checkout with no origin). Then cwd is set, the fallback is
    # skipped, and the namespace is silently None for that flush — so its
    # directives extract unscoped and are recalled in every repo, even though an
    # earlier flush had already resolved the name.
    cwd, namespace = _namespace(records)
    if not cwd:
        cwd = state.get("cwd") or None
    if not namespace:
        # Remembered, so this is a dict lookup rather than re-running git.
        namespace = state.get("namespace") or None

    # This delta's title if it carries one, else whatever we last saw.
    title, custom = _titles(records, state)
    # In a THREAD, because resolving can renew the token — two blocking urllib
    # calls, up to ~25s of socket timeout. `asyncio.wait_for` cannot cancel a
    # synchronous call, so run inline it would pin the event loop AND hold the
    # flock past the flush deadline, blocking every later turn's capture for
    # the session. Offloading is what makes the timeout mean anything here.
    url, bearer = await asyncio.to_thread(resolve_bearer)
    if not bearer:
        # Not an error — the state a background hook must degrade quietly on.
        # Raised rather than returned so the one handler in main() records the
        # breadcrumb, keeping every failure path reported the same way.
        raise _NoCredential("no usable credential (key, token or cached login)")
    # Resolved AFTER the url — prod and staging hold different brain ids for the
    # same repo. Only when the TRANSCRIPT said where it ran: a hook can fire
    # from a different repo than the session's, and an unknown origin must
    # degrade to personal memory rather than guess a room.
    env = env_for_url(url)

    # No connection to open: the server is stateless, so a Session is just
    # the endpoint and the credential. Verified against the live server —
    # it negotiates no session id and does not require `initialize`, so this
    # is ONE round trip where the SDK did three.
    # Per call, and deliberately less than the whole flush budget: this hook
    # makes TWO calls on a cold cache — the room lookup and the import — so
    # granting each the full timeout lets a stalled lookup consume the budget
    # and the import, the only call that actually captures anything, never
    # happens. Half guarantees the second call still gets a turn.
    session = mcp_http.Session(url, bearer, timeout=_flush_timeout_s() / 2)
    # No `initialize` handshake: verified against the live server, a fresh
    # process can call a tool directly and get a result. Dropping it removes two
    # of the three round trips this hook used to make per turn.
    # Cached hit is a dict lookup; a miss asks the server once and
    # caches the answer, so this is not a per-turn round-trip.
    room = await resolve_repo_brain(session, cwd, env) if cwd else None
    arguments = {
        "messages": batch,
        "conversation_id": session_id,
        "source_platform": "claude",
        # The whole point: durable on arrival, extracted in batches.
        "flush": "auto",
    }
    if room:
        arguments["agent_brain_id"] = room["brain_id"]
        # The org that OWNS the room, when it is not the caller's
        # default. A brain resolves inside exactly ONE org, and the
        # default follows whichever org was last selected in the MemHub
        # app — so it changes under a running session. Sending the id
        # without its org is how every capture into such a room failed
        # with "Agent brain not found": silently, because this hook is
        # async and its output goes nowhere, and indistinguishably from
        # a deleted brain.
        if room.get("org_id"):
            arguments["org_id"] = room["org_id"]
    if namespace:
        arguments["namespace"] = namespace
    if title:
        # Re-sent on every flush so a regenerated title updates the
        # conversation rather than sticking at whatever the first
        # turn happened to be called.
        arguments["title"] = title
    provenance = pr_provenance.import_provenance(pending_pr_urls)
    if provenance:
        arguments["provenance"] = provenance
    # Transport failures are CLASSIFIED here, not left to the catch-all in
    # main(). Falling through to it recorded every one as reason="error" — a
    # permanent-looking breadcrumb — which is actively wrong for the two cases
    # that are not faults at all: a 429 is backpressure this design expects
    # (one seat's throughput, a fleet flushing every turn), and a 401 is a
    # revoked or lapsed credential whose fix is one command. Both were being
    # reported to the user as "the capture hook hit an unexpected error".
    #
    # The cursor is unmoved in every branch, so all of them retry next turn.
    # Set by ``_import`` when the server refused the payload for its SIZE. The
    # reaction lives at the call site because it needs to know how many records
    # went — a slice of one cannot be made smaller, and that is the difference
    # between "send less next turn" and "step over this record".
    too_large = False

    async def _import(args: dict):
        """Send one import, classifying transport failures. None = already
        reported, and the caller must return without touching the cursor.

        A closure rather than the straight-line ladder it replaces because this
        hook can now send TWICE — once routed to the repo's room, and again
        unrouted when the server says that room does not exist. A 401 or a 429
        must be classified identically on both, and keeping one copy is what
        guarantees that; a second inline ladder is exactly how the two drift.
        """
        nonlocal too_large
        try:
            return await session.call_tool("import_conversation", arguments=args)
        except mcp_http.McpRateLimited as e:
            wait = f" (retry-after {e.retry_after:.0f}s)" if e.retry_after else ""
            _log(f"rate limited{wait} — the next turn retries (cursor unmoved)")
            _mark_failure(session_id, "rate_limited", str(e))
        except mcp_http.McpNoResponse as e:
            # Reached the server, it streamed, no answer came. That is a reply
            # we could not use — the same bucket as a body we could not read —
            # not a generic fault.
            _log(f"no response frame: {e}")
            _mark_failure(session_id, "unrecognized_response", str(e))
        except mcp_http.McpError as e:
            if e.status == 401:
                # Unauthenticated: no credential, or one the server won't
                # accept. /memhub:login mints a new one, so the advice
                # converges.
                _log("credential rejected; run /memhub:login — skipping")
                _mark_failure(session_id, "auth",
                              "server rejected the credential (401)")
            elif e.status == 403:
                # Authorized-but-forbidden. Re-logging in mints an equivalent
                # credential and changes NOTHING, so telling them to is the
                # same non-converging loop the `no_refresh` advice was fixed
                # for. The cause is scope or org access, and that is what to
                # name.
                _log("credential lacks permission (403) — check the key's "
                     "scopes and that it can reach this brain's org; skipping")
                _mark_failure(session_id, "forbidden", str(e))
            elif e.status == 413:
                # The server refused the payload for its SIZE. The batch is
                # bounded now, so reaching here means either this server's
                # limit is lower than the cap we guessed, or the slice is ONE
                # record and cannot be split at all. Reported under its own
                # slug because the generic `error` advice — "run
                # /memhub:login --status" — sent the last person who hit this
                # to inspect the one thing that was definitely fine, and they
                # read past the banner for hours.
                _log(f"payload refused as too large "
                     f"({len(args.get('messages') or [])} rec)")
                _mark_failure(session_id, "payload_too_large", str(e))
                too_large = True
            elif _is_size_refusal(str(e)):
                # Same refusal, no status to read it from — see
                # ``_SIZE_REFUSAL_PHRASES``.
                _log(f"payload refused as too large (no status): {e}")
                _mark_failure(session_id, "payload_too_large", str(e))
                too_large = True
            else:
                _log(f"transport error: {e}")
                _mark_failure(session_id, "error", str(e))
        return None

    def _texts(result) -> list[str]:
        return [t for t in (getattr(b, "text", None)
                            for b in getattr(result, "content", []) or []) if t]

    res = await _import(arguments)
    if res is None:
        if too_large:
            _shrink_slice(session_id, state, batch, batch_consumed)
        return

    # MCP signals tool failure via isError + a message, NOT an
    # exception. Without this the cursor would advance past records the
    # server rejected, losing them permanently.
    texts = _texts(res)

    # A cached room the backend does not have must not cost the turn. Routing
    # already degrades to long-term memory when NO room is cached — but that
    # check reads the CACHE, not the server, so a present-but-wrong id sailed
    # past it and the flush simply died. That is how a `production` entry
    # holding a staging brain id took out per-turn capture and the SessionEnd
    # backstop together, for days: every turn re-sent an id the server had
    # already rejected, and `write_miss` refused to overwrite it, so nothing
    # could ever correct the cache. Forget the id — so the next turn resolves
    # honestly — and send again unrouted, which is where the no-room path
    # would have put this turn anyway.
    if getattr(res, "isError", False) and room and is_missing_brain(texts):
        _log(f"room {room['brain_id'][:8]} does not exist on this backend — "
             "dropping it from the cache and flushing to long-term memory")
        forget_room(cwd, env)
        room = None
        arguments.pop("agent_brain_id", None)
        arguments.pop("org_id", None)
        res = await _import(arguments)
        if res is None:
            if too_large:
                _shrink_slice(session_id, state, batch, batch_consumed)
            return
        texts = _texts(res)

    if getattr(res, "isError", False):
        detail = (texts[0] if texts else "no detail")[:200]
        if _is_size_refusal(detail):
            # A size refusal delivered as a tool error rather than a transport
            # status. Reported and reacted to identically, or the cap never
            # comes down on a server that answers this way.
            _log(f"payload refused as too large (tool error): {detail}")
            _mark_failure(session_id, "payload_too_large", detail)
            _shrink_slice(session_id, state, batch, batch_consumed)
            return
        _log(f"flush FAILED: {detail}")
        _mark_failure(session_id, "server_rejected", detail)
        return

    out = getattr(res, "structuredContent", None)
    if isinstance(out, dict) and "conversation_id" not in out \
            and isinstance(out.get("result"), dict):
        out = out["result"]  # FastMCP wraps some returns
    if not isinstance(out, dict):
        for text in texts:
            try:
                out = json.loads(text)
                break
            except json.JSONDecodeError:
                continue
    if not isinstance(out, dict) or "conversation_id" not in out:
        # Unrecognized body: do NOT advance. Re-sending is free.
        detail = (texts[0] if texts else "")[:120]
        _log(f"response unrecognized: {detail!r}")
        _mark_failure(session_id, "unrecognized_response", detail)
        return

    # ``ack_through`` is only returned by a server that performed the
    # durable receive this hook depends on. Without it we are talking to
    # an older server that queues the import in the background and
    # treats every turn as an immediate extraction — per-turn LLM cost,
    # and the episode fragmentation the batching exists to avoid. Say so
    # loudly; silently doing the expensive wrong thing is worse than
    # noisy logs a user can act on.
    if "ack_through" not in out:
        # This server queues the import in the background instead of
        # committing it before replying, so a well-formed response
        # does NOT mean the records are durable — advancing on it
        # could drop them. It also extracts every turn immediately,
        # which is the cost this hook exists to avoid. Go dormant for
        # the session rather than pay for the wrong behaviour: the
        # commit/PR and SessionEnd hooks still capture it.
        _log("server has no per-turn support (no ack_through) — "
             "disabling per-turn flush for this session; commit/PR "
             "and session-end capture still apply. Upgrade the MemHub "
             "server to enable it.")
        # ``unsupported`` and NOT a failure breadcrumb. This is a
        # deliberate degrade, not a break: per-turn goes dormant while
        # the commit/PR and SessionEnd paths keep capturing, so there
        # is nothing the user must drop what they are doing to fix.
        #
        # It also cannot be retracted. Dormancy means no further flush
        # runs, so no success can ever clear a breadcrumb — and because
        # the condition is environmental, every NEW session rediscovers
        # it and warns again. That is a banner on every session start
        # for a day, about a known state with a working fallback, which
        # is precisely how a warning becomes wallpaper and stops being
        # read on the day it matters.
        #
        # Surfacing it properly needs a once-ever channel keyed by
        # server, not the per-session one; until then the log line
        # above records it.
        # Clears THIS path's stale error, and deliberately does NOT stamp
        # `last_ok_at`.
        #
        # Stamping one was a bug: since the health check retracts a failure when
        # any path reports success for the same session, a success recorded here
        # would silently retract a REAL failure the SessionEnd backstop had
        # recorded — reintroducing the invisible capture failure this whole
        # series exists to remove, and doing it from a branch that captured
        # nothing.
        #
        # Clearing the error is still right: dormancy means no later per-turn
        # flush runs to retract it, so an older error would be stranded forever.
        # But this branch speaks only for itself. It reached the server and got
        # an answer; it did not capture anything, so it is in no position to
        # vouch for another path.
        _save_state(session_id, unsupported=True, last_error=None,
                    last_error_detail=None, last_error_at=None)
        return

    # Committed server-side — only now is it safe to move the cursor.
    # ``custom_title`` is stored SEPARATELY from the title that was
    # sent, and only when there is one: it is the one source a later
    # delta must not be able to override, and merging a None over it
    # would let the next ``ai-title`` — which the client keeps
    # emitting with the pre-rename value — take the name back.
    reconciled = pr_provenance.acknowledge_confirmed_import(
        pending_pr_urls,
        accepted_pr_urls,
        out,
    )
    pending_pr_urls, accepted_pr_urls = reconciled
    if pending_pr_urls:
        _save_state(session_id, pending_pr_urls=pending_pr_urls,
                    accepted_pr_urls=accepted_pr_urls)
        _mark_failure(
            session_id,
            "unconfirmed_provenance",
            "backend did not acknowledge the captured PR URL",
        )
        return
    _mark_success(session_id, offset=batch_consumed,
                  last_uuid=out.get("ack_through"), cwd=cwd,
                  namespace=namespace, title=title,
                  pending_pr_urls=pending_pr_urls,
                  accepted_pr_urls=accepted_pr_urls,
                  **({"custom_title": custom} if custom else {}))
    # Sent count, not read count — and the byte span is the span the cursor
    # just advanced past, which is now the SENT span rather than the whole
    # delta. The filtered records are the difference between read and sendable,
    # and ``held`` is the rest of the delta this turn deliberately left for the
    # next one, so an under-sent delta stays diagnosable from the log alone.
    filtered = len(records) - len(sendable)
    held = len(sendable) - len(batch)
    _log(f"+{len(batch)} rec ({batch_consumed - offset}B) "
         + (f"filtered={filtered} " if filtered else "")
         + (f"held={held} " if held else "")
         + f"new={out.get('records_new')} pending={out.get('pending')} "
         f"draining={out.get('draining')}")


class _NoCredential(RuntimeError):
    """No usable bearer. Replaces the SDK's NonInteractiveAuthRequired.

    A plain exception now, not something buried in an anyio task group, so the
    handler recognises it by type instead of walking an exception tree — which
    is what the SDK's wrapping forced.
    """


# `_auth_required` used to live here: thirty lines walking an exception tree
# for NonInteractiveAuthRequired, because the SDK raised it inside anyio task
# groups and it surfaced wrapped in ExceptionGroups or chained as __cause__.
# Resolving the credential ourselves means the miss is now a plain exception
# raised on our own stack, so `isinstance` is the whole check.


def main() -> int:
    lock_fd: int | None = None
    # Bound BEFORE the try so the handler can always write a breadcrumb. Reading
    # stdin or parsing it is itself a failure path, and a NameError raised from
    # inside the one handler that exists to keep this hook quiet would surface
    # the traceback it was written to prevent.
    session_id = ""
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        session_id = (hook_input.get("session_id") or "").strip()
        transcript_path = (hook_input.get("transcript_path") or "").strip()
        if not session_id or not transcript_path \
                or not Path(transcript_path).exists():
            return 0
        lock_fd = _acquire(session_id)
        if lock_fd is None:
            return 0  # a flush is already in flight; its successor carries ours
        # Bounded, because the lock is held for the whole round-trip and the
        # prefilter skips every later turn while it is held. Without a cap, one
        # hung request would stall capture for this session until the hook's own
        # 300s timeout — five minutes of turns silently not shipping. A minute
        # is far longer than a small delta needs and an order of magnitude
        # tighter than that. Timing out is safe: the cursor has not moved, so
        # the next turn re-sends.
        timeout_s = _flush_timeout_s()
        asyncio.run(asyncio.wait_for(
            _flush(session_id, transcript_path), timeout=timeout_s))
    # BaseException, not Exception: anyio mixes CancelledError into task
    # groups, producing a BaseExceptionGroup that an Exception handler would
    # miss — killing the hook with a traceback in the user's session.
    except BaseException as e:  # noqa: BLE001 — never fail the hook
        if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
            _log(f"timed out after {_flush_timeout_s():.0f}s — the next turn retries (cursor unmoved)")
            reason, detail = "timeout", f"no response in {_flush_timeout_s():.0f}s"
        elif isinstance(e, _NoCredential):
            _log("no usable credential; run /memhub:login "
                 "(or set MEMHUB_TOKEN) to enable per-turn capture — skipping")
            # The one failure the user must act on personally, and the one that
            # stays broken forever until they do: no retry can mint a token.
            reason, detail = "auth", "no usable cached OAuth token"
        else:
            _log(f"skipped ({type(e).__name__}: {e})")
            reason, detail = "error", f"{type(e).__name__}: {e}"
        # Only with a session to file it under, and never at the cost of the
        # quiet exit — a breadcrumb that raised would defeat this whole handler.
        if session_id:
            try:
                _mark_failure(session_id, reason, detail)
            except BaseException:  # noqa: BLE001
                pass
    finally:
        if lock_fd is not None:
            # Closing releases the flock. The kernel would do this at exit
            # anyway; doing it here keeps the held window tight.
            try:
                os.close(lock_fd)
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
