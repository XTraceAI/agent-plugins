#!/usr/bin/env python3
"""Cursor capture: hook-triggered flush of a cursor-agent session into MemHub.

The Cursor analog of ``flush_turn.py``, reusing its whole downstream —
``redact``, ``_memhub_auth.resolve_bearer``, ``brain_resolve``, ``room_map``,
``mcp_http`` — and differing in exactly the two places Cursor differs:

1. **Data source**: legacy Cursor/CLI sessions use the full-fidelity v1
   ``store.db``; current Cursor IDE sessions use the hook-provided JSONL under
   ``~/.cursor/projects``. Source choice is sticky per session so record UUIDs
   never shift if a second representation appears later.
2. **Watermark**: stores compare content-addressed blob ids; transcripts use a
   canonical-content digest. Exact completed-turn hook usage is persisted by
   generation id before the network call and folded into the final assistant
   record, so retries retain telemetry and duplicate ``afterAgentResponse`` /
   ``stop`` delivery never double-counts it. Per-record timestamps follow the
   same discipline: Cursor's artifacts carry real clocks for only some
   records, so each record is dated the moment a hook FIRST OBSERVES it and
   that pin is persisted before the network call (``_stamp_records``) — a
   re-send ships the original dates, never the re-send's wall clock.

The full canonical transcript is re-sent each flush under the constant
``conversation_id cursor-<uuid>``; the SERVER's watermark folds re-sends
forward (the same property Codex re-imports rely on). The local blob-id set
only decides WHETHER to flush, so a lost state file costs a redundant send,
never a lost or duplicated conversation.

Cursor IDE 3.17 emits ``afterAgentResponse`` and ``stop`` with exact usage;
Cursor Agent CLI 2026.08 emits only ``sessionStart`` / ``sessionEnd`` hooks and
keeps usage solely in caller-owned stream output. ``sessionEnd`` therefore
captures CLI content with nullable usage rather than estimating it.
``beforeShellExecution`` flushes only on milestone commands (git commit /
gh pr …) so ordinary shell traffic stays quiet; ``afterFileEdit`` is
debounced. Whatever the trigger set misses, the next flush or an
import-session sweep heals — idempotence carries correctness, hooks carry
latency.

Invoked by the portable Cursor launcher, which answers the hook's permission
contract immediately and re-runs this script detached - a slow server must
never hold up the user's shell command.

Runs under bare python3 (stdlib + sibling modules only — no mcp SDK), same
discipline as flush_turn.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atomic_write  # noqa: E402
import portable_lock  # noqa: E402
import mcp_http  # noqa: E402
from _memhub_auth import resolve_bearer  # noqa: E402
from brain_resolve import resolve_repo_brain  # noqa: E402
from readers import cursor as cursor_reader  # noqa: E402
from redact import redact_records  # noqa: E402
from room_map import env_for_url, git_env, git_readonly  # noqa: E402

STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "cursorflush"
_CURSOR_PROJECTS = Path.home() / ".cursor" / "projects"

# afterFileEdit fires on every edit; flushing each one would hammer the
# server mid-turn. Milestones and turn boundaries bypass this.
DEBOUNCE_S = 120.0
FLUSH_TIMEOUT_S = 240.0
# Dormancy (an unconfirmable server — see _verdict) must not be a one-way
# door: going dormant means never flushing again, so nothing can ever observe
# that the server was upgraded. Re-probe occasionally instead. Long enough
# that an unfixed server costs one wasted upload an hour, short enough that a
# fix takes effect inside a working session.
DORMANT_RETRY_S = 1800.0
# Cap on how long a last-chance boundary waits for a concurrent flush before
# giving up (see _acquire) — bounds blocked-process lifetime.
LOCK_WAIT_S = 60.0
# "Unconfirmed" is retried because it is usually transient. A server that
# returns it FOREVER (always records_dropped>0, say) would otherwise re-parse,
# re-redact and re-upload the whole transcript on every event for the life of
# the session, bounded only by the debounce. After this many consecutive
# unconfirmed replies the session goes dormant like an unsupported one — and
# re-probes on the same timer, so a fixed server still heals.
MAX_UNCONFIRMED = 5


def _note_failure(uuid: str, reason: str) -> None:
    """Record a SERVER-CONTACTED failure, escalating to dormancy after
    MAX_UNCONFIRMED consecutive ones — rate-limit, McpError, timeout, or an
    unconfirmed import. Counting only the "unconfirmed verdict" left a
    hard-down backend (always rate-limited / erroring) hammering the server on
    every event forever, since dormancy never triggered.

    "Consecutive" means consecutive CONTACTED attempts since the last success:
    a confirmed import clears the streak, and so does any outcome that never
    reached the server (an empty redaction, a missing credential), so those
    neutral no-ops cannot slowly accumulate a healthy session into dormancy.

    The streak resets to 0 the moment the session goes dormant, so each
    re-probe after DORMANT_RETRY_S gets a FRESH budget of attempts — a
    flaky server that recovers on the 2nd or 3rd try still gets those tries.
    A confirmed import clears everything (see _flush's success path).
    """
    now = time.time()
    st = _read_state(uuid)
    if st.get("unsupported"):
        # A re-probe of a dormant session FAILED: stay dormant, reset the
        # timer — a persistently-down server is attempted exactly once per
        # DORMANT_RETRY_S, not given a fresh MAX_UNCONFIRMED budget each
        # window that would let it hammer between windows.
        _save_state(uuid, last_flush_at=now, last_error=reason,
                    unsupported=True, unsupported_at=now, fail_streak=0)
        return
    streak = int(st.get("fail_streak") or 0) + 1
    if streak >= MAX_UNCONFIRMED:
        _log(f"{streak} consecutive failed imports ({reason}) — per-event "
             f"flush is dormant for this session; run /memhub:import-session "
             f"to capture it. Re-probes in {DORMANT_RETRY_S / 60:.0f} min.")
        _save_state(uuid, last_flush_at=now, last_error=reason,
                    unsupported=True, unsupported_at=now, fail_streak=0)
    else:
        # Sub-threshold: back off via the debounce (last_flush_at), stay in
        # normal mode (unsupported cleared) so the next event keeps counting.
        _save_state(uuid, last_flush_at=now, last_error=reason,
                    unsupported=False, fail_streak=streak)


# The milestone must be in COMMAND POSITION, not merely mentioned: `.*` with
# DOTALL matched `git log --oneline | grep commit` and even
# `echo "remember to git commit"`, firing a full flush on ordinary traffic.
# Command position = start of the text, just after a shell separator, or
# inside a `bash -lc "..."` wrapper (how agents usually deliver shell calls,
# so a plain ^ anchor would miss real milestones).
_MILESTONE_RE = re.compile(
    # Position: start of text, after a shell separator, or inside a
    # `bash -lc "..."` wrapper (how agents deliver shell calls).
    r"""(?:^|[;&|]\s*|\b(?:ba)?sh\s+-[a-z]*c\s*['"]?)\s*"""
    # Leading wrappers an agent may prepend.
    r"""(?:(?:sudo|env|command|time|nice)\s+(?:[A-Za-z_]\w*=\S*\s+)*)*"""
    # Options BETWEEN the tool and the subcommand: `git -C <dir> commit` is a
    # routine agent form, and requiring adjacency silently skipped it.
    # `gh pr` alone also matched read-only listings (`gh pr list`, `gh pr
    # view`), each buying a whole-transcript send; only PR ACTIONS are
    # milestones.
    r"""(?:git(?:\s+-{1,2}[\w-]+(?:=\S+)?(?:\s+[^\s-]\S*)?)*\s+commit\b"""
    r"""|gh(?:\s+-{1,2}[\w-]+(?:=\S+)?(?:\s+[^\s-]\S*)?)*"""
    r"""\s+pr\s+(?:create|merge|ready|edit|close|reopen|comment)\b)""")

# Bound on how much of a command we scan for a milestone. The regex above is
# linear (bounded quantifiers, no nested repetition — a 200 KB input scans in
# ~2 ms), so this is NOT backtracking protection; it is only a sanity limit on
# a pathological multi-megabyte argument. 16 KiB clears any realistic agent
# command — including a long `bash -lc "cd <deep/path> && … && git commit"`
# whose verb lands well past the old 512-byte cap — by a wide margin.
_MILESTONE_SCAN_LIMIT = 16384

# Server-side extraction mode per event — mirrors the Claude design, where
# flush_turn buffers ("auto") and flush_session drains ("now" — the server
# default) at turn/session end and commit/PR boundaries. If every event sent
# "auto", a small IDE session that stays open would never cross the drain
# threshold: records buffer forever, ack_through stays null, and the session
# never materializes in the Sessions view. Verified live. Boundaries must
# drain; only mid-turn edits are debounce candidates.
_FLUSH_MODE = {
    "beforeShellExecution": "now",   # only fires on milestone commands (gate)
    "afterAgentResponse": "now",     # completed turn + exact usage
    "stop": "now",                   # turn boundary
    "beforeSubmitPrompt": "now",     # previous turn is definitely complete
    "sessionEnd": "now",             # CLI/content backstop
    # TEMPORARILY "now", not "auto": staging showed auto-buffered records get
    # content-registered by dedup WITHOUT persisting — if the buffer never
    # drains (small session, no later "now"), the records become permanently
    # unimportable under ANY conversation id (records_new: 0 on a virgin id,
    # ack_through: null forever). Until that backend bug is fixed, losing
    # episode batching is the lesser evil than losing records. Revert to
    # "auto" when the server folds dedup registration into the drain.
    "afterFileEdit": "now",
}


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [cursor-flush] {msg}\n"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        log = STATE_DIR / "log"
        # Cap by rewrite-on-threshold: a background hook's log must never
        # grow unbounded, and losing old lines is the acceptable direction.
        if log.exists() and log.stat().st_size > 256_000:
            tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            log.write_text("\n".join(tail) + "\n", encoding="utf-8")
        with open(log, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _safe_uuid(uuid: str) -> str:
    """A uuid safe as a filename component. Identity comes from the hook
    payload (session_id / transcript_path's parent), so separators and ``..``
    are flattened — otherwise the state and .lock files could be published
    outside STATE_DIR. Real Cursor session ids are plain uuids and pass
    through unchanged."""
    import re as _re
    return _re.sub(r"[^A-Za-z0-9._-]", "_", uuid)[:80]


def _state_path(uuid: str) -> Path:
    return STATE_DIR / f"{_safe_uuid(uuid)}.json"


def _read_state(uuid: str) -> dict:
    try:
        return json.loads(_state_path(uuid).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(uuid: str, **fields) -> None:
    """Merge ``fields`` into this session's state under a per-uuid lock.

    atomic_write makes each PUBLISH atomic, but the read-modify-write around
    it is not: the Cursor launcher runs every flush DETACHED, so an
    afterFileEdit and a stop firing close together can both read the old
    state and the later writer silently drops the other's blob_ids or
    last_flush_at — regressing the watermark into redundant re-sends. The
    lock is a separate .lock file, never the state file itself: locking a
    file that is about to be replaced releases the lock with the old inode.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _state_path(uuid).with_suffix(".lock")
    try:
        fh = open(lock_path, "w", encoding="utf-8")
    except OSError:  # unwritable state dir — publish unserialized rather
        fh = None    # than lose the write entirely
    try:
        if fh is not None:
            portable_lock.lock_exclusive(portable_lock.fileno_of(fh))
        state = _read_state(uuid)
        state.update(fields)
        atomic_write.publish(_state_path(uuid), json.dumps(state))
    finally:
        if fh is not None:
            try:
                portable_lock.unlock(portable_lock.fileno_of(fh))
            except OSError:
                pass
            fh.close()


def _acquire(uuid: str, blocking: bool = False) -> int | None:
    """Per-session flock. Non-blocking returns None when held; blocking WAITS.

    Turn-boundary events (stop / beforeSubmitPrompt) pass blocking=True: a
    boundary that lost this lock to a concurrent flush might be the session's
    last event and never retry. The peer's flush is bounded by
    asyncio.wait_for, and the boundary runs detached, so waiting is cheap;
    should_flush re-checks after acquiring so nothing is re-sent.


    Both a milestone shell event and a turn boundary can fire within the same
    second, and the launcher detaches each into its own process - so two
    flushes would read the same watermark, both re-read and re-redact the whole
    transcript, both upload it, and then race to write the watermark back. The
    loser of this lock is redundant by construction: the holder is sending the
    same (or newer) content.

    flock rather than a lockfile because the kernel owns the lifetime — it
    releases on process exit however that happens, so there is no such thing as
    a stale one to reclaim. The caller must keep the fd OPEN; closing releases.

    A DISTINCT file from _save_state's .lock: flock is per open-file-
    description, so taking both on one path would deadlock this process
    against itself.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # O_CLOEXEC is belt-and-braces: CPython has made os.open fds
    # non-inheritable by default since PEP 446 (3.4), so a subprocess cannot
    # already pin this lock past our exit — the flag states the invariant in
    # code so a future refactor cannot quietly drop it. getattr because the
    # constant is Unix-only and the capture scripts run on native Windows.
    fd = os.open(STATE_DIR / f"{_safe_uuid(uuid)}.flush.lock",
                 os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o600)
    if not blocking:
        try:
            portable_lock.lock_exclusive(fd, blocking=False)
            return fd
        except OSError:
            os.close(fd)
            return None
    # BOUNDED wait, never an unbounded flock(LOCK_EX): a live peer stuck
    # OUTSIDE its asyncio.wait_for (a huge synchronous parse, a wedged
    # subprocess) holds the lock past the network deadline, and an unbounded
    # wait would pile up one blocked detached process per boundary event. So
    # poll the non-blocking lock up to LOCK_WAIT_S — long enough to wait out a
    # normal concurrent flush, short enough to cap a stuck one — then give up
    # (the import-session sweep is the backstop). A dead peer releases the
    # flock via the kernel, so this returns the instant that happens.
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            portable_lock.lock_exclusive(fd, blocking=False)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.05)


# A cursor session id is a uuid; the transcript_path fallback must look like
# one, not a layout constant. Identity extraction is deliberately looser than
# transcript-file authorization below: a dir-less filename can still identify
# a legacy store, but is not permission to read an unrecognized file layout.
# `…/agent-transcripts/<uuid>/<uuid>.jsonl` puts the uuid at BOTH the parent dir
# and the file stem. Accept the parent, then the stem, then give up.
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                      r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def session_uuid(payload: dict) -> str | None:
    """The session identity a hook carries. ``session_id`` when present; else
    a uuid derived from ``transcript_path`` — never a directory-name constant.

    This parses identity only. ``_valid_transcript_path`` separately enforces
    the one native layout authorized for transcript reads.
    """
    sid = payload.get("session_id") or payload.get("conversation_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp:
        p = Path(tp)
        for cand in (p.parent.name, p.stem):
            if _UUID_RE.fullmatch(cand):
                return cand
        _log(f"transcript_path yields no session uuid ({tp!r}) — skipping "
             f"rather than mis-key state on a layout constant")
    return None


def _valid_transcript_path(raw, uuid: str) -> tuple[Path | None, str]:
    """Resolve a hook transcript without granting arbitrary-file upload.

    A project can invoke the launcher itself and controls hook JSON. Requiring
    Cursor's observed native root plus the UUID in both directory and filename
    keeps a forged payload from turning this capture path into a local file
    reader. A UUID-looking filename directly under ``agent-transcripts`` may
    identify a session, but that unobserved layout is intentionally not enough
    to authorize reading it.
    ``resolve`` on both sides makes a symlink escape compare outside the root.
    """
    if not isinstance(raw, str) or not raw:
        return None, "hook payload has no transcript_path"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        return None, "transcript_path is not absolute"
    if candidate.parent.name != uuid or candidate.name != f"{uuid}.jsonl":
        return None, "transcript_path does not match the session UUID"
    try:
        resolved = candidate.resolve(strict=True)
        root = _CURSOR_PROJECTS.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None, "transcript_path does not resolve under ~/.cursor/projects"
    if not resolved.is_file():
        return None, "transcript_path is not a regular file"
    return resolved, ""


def _source_for(uuid: str, payload: dict, state: dict
                ) -> tuple[str | None, Path | None, str]:
    """Choose one native representation and keep that choice sticky."""
    sticky = state.get("source_kind")
    if sticky == "store":
        path, err = cursor_reader.locate(uuid)
        if path is not None and path.name == "store.db":
            return "store", path, ""
        return None, None, err or "sticky Cursor store is unavailable"
    if sticky == "transcript":
        raw = state.get("transcript_path") or payload.get("transcript_path")
        path, err = _valid_transcript_path(raw, uuid)
        return ("transcript", path, "") if path is not None else (None, None, err)

    # Prefer the richer store when both exist (current CLI; older IDE). A
    # current IDE session has no store, so the validated hook transcript wins.
    store, store_err = cursor_reader.locate(uuid)
    if store is not None and store.name == "store.db":
        return "store", store, ""
    transcript, transcript_err = _valid_transcript_path(
        payload.get("transcript_path"), uuid)
    if transcript is not None:
        return "transcript", transcript, ""
    return None, None, transcript_err or store_err


def _payload_meta(payload: dict, state: dict) -> dict:
    prior = state.get("cursor_meta")
    meta = dict(prior) if isinstance(prior, dict) else {}
    model = payload.get("model")
    if isinstance(model, str) and model.strip():
        meta["model"] = model.strip()[:200]
    roots = payload.get("workspace_roots")
    if isinstance(roots, list):
        cwd = next((root for root in roots
                    if isinstance(root, str) and root), None)
        if cwd:
            meta["cwd"] = cwd[:4096]
    return meta


_HOOK_USAGE_KEYS = {
    "inputTokens", "input_tokens", "outputTokens", "output_tokens",
    "cacheReadTokens", "cache_read_tokens", "cache_read_input_tokens",
    "cacheWriteTokens", "cache_write_tokens", "cache_creation_input_tokens",
}


def _hook_usage(event: str, payload: dict
                ) -> tuple[str, dict[str, int]] | None:
    if event not in ("afterAgentResponse", "stop"):
        return None
    if not any(key in payload for key in _HOOK_USAGE_KEYS):
        return None
    generation = payload.get("generation_id")
    usage = cursor_reader.normalize_usage(payload)
    if (not isinstance(generation, str) or
            not _UUID_RE.fullmatch(generation) or usage is None):
        return None
    return generation, usage


def _assistant_text(record: dict) -> str:
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(block.get("text", "") for block in content
                     if isinstance(block, dict) and block.get("type") == "text")


def _normalized_assistant_text(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _last_assistant_uuid(records: list[dict], expected_text=None) -> str | None:
    last_uuid: str | None = None
    turn_text: list[str] = []
    for record in reversed(records):
        message = record.get("message")
        # Canonical prompt/banner records have string content, while tool
        # results are user records with list content and remain inside a turn.
        # Raw Cursor list-form prompts are normalized to strings by the reader.
        if (record.get("type") == "user" and isinstance(message, dict) and
                isinstance(message.get("content"), str)):
            break
        if (record.get("type") != "assistant" or
                not isinstance(record.get("uuid"), str)):
            continue
        if last_uuid is None:
            # Usage is turn-level and belongs on the final assistant record,
            # including a tool_use-only turn. This matches _canonicalize(),
            # which puts persisted native usage on emitted[-1]. Text below is
            # an association guard, not the destination-record selector.
            last_uuid = record["uuid"]
        text = _assistant_text(record)
        if text:
            turn_text.append(text)
    if last_uuid is None or not isinstance(expected_text, str):
        return last_uuid
    expected = _normalized_assistant_text(expected_text)
    if not expected:
        return last_uuid
    individual = {_normalized_assistant_text(text) for text in turn_text}
    combined = _normalized_assistant_text("\n".join(reversed(turn_text)))
    return last_uuid if expected == combined or expected in individual else None


def _usage_events_with(state: dict, generation: str, target_uuid: str,
                       usage: dict[str, int]) -> dict:
    raw = state.get("usage_events")
    events = dict(raw) if isinstance(raw, dict) else {}
    prior = events.get(generation)
    if (isinstance(prior, dict) and
            isinstance(prior.get("target_uuid"), str)):
        # afterAgentResponse and stop repeat one generation. The detached stop
        # process can wait behind another flush while a new turn begins, so its
        # then-current "last assistant" is not authoritative. Once observed,
        # a generation stays bound to its original deterministic record UUID.
        target_uuid = prior["target_uuid"]
    # Updating an existing dict key preserves its old insertion position.
    # Pop first so even an oversized recovered state cannot evict the sample
    # being refreshed when the bounded map drops its oldest entries below.
    events.pop(generation, None)
    # If Cursor regenerates a visible turn onto the same deterministic record,
    # retain the latest exact sample rather than summing unlike attempts.
    events = {key: value for key, value in events.items()
              if not (isinstance(value, dict) and
                      value.get("target_uuid") == target_uuid)}
    events[generation] = {"target_uuid": target_uuid, "usage": usage}
    # Long-running chats must not grow hook state without bound. Removing old
    # entries cannot corrupt server totals: confirmed UUIDs are immutable and
    # a later re-send is folded by server dedup.
    while len(events) > 512:
        events.pop(next(iter(events)))
    return events


def _apply_usage(records: list[dict], usage_events) -> set[str]:
    by_uuid = {record.get("uuid"): record for record in records}
    applied: set[str] = set()
    if not isinstance(usage_events, dict):
        return applied
    for generation, event in usage_events.items():
        if not isinstance(event, dict):
            continue
        record = by_uuid.get(event.get("target_uuid"))
        usage = event.get("usage")
        message = record.get("message") if isinstance(record, dict) else None
        if (not isinstance(record, dict) or record.get("type") != "assistant" or
                not isinstance(message, dict) or
                cursor_reader.normalize_usage(usage) is None):
            continue
        message["usage"] = usage
        applied.add(generation)
    return applied


# Prune TRIGGER — deliberately not a hard cap — for the per-record timestamp
# pin map persisted in session state. When the map outgrows this, entries
# whose record is NO LONGER in the transcript are dropped (a store checkpoint
# restore shifts the deterministic index-based uuids, stranding the old
# ones). Pins for records still being re-sent are NEVER evicted, at ANY map
# size: dropping one re-dates a live record at the next flush's wall clock,
# the exact degeneracy this module exists to prevent (review finding on the
# original FIFO cap). The map therefore scales with the live artifact, by
# design: one ~70-byte entry per record, against a flush that already
# re-reads, re-hashes, and re-uploads the ENTIRE transcript on every event —
# at the session size where this map hurts (~10^5 records ≈ 7 MB state), the
# per-event whole-transcript flush is the cliff, and it arrives first.
_RECORD_TS_PRUNE_TRIGGER = 8192


def _now_iso() -> str:
    now = time.time()
    return (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
            + f".{int(now * 1000) % 1000:03d}Z")


def _stamp_records(records: list[dict], prior, now_iso: str | None, *,
                   first_observation: bool,
                   boundary_uuids: frozenset | set = frozenset()) -> dict:
    """Date every record with a REAL clock, pinned across re-sends.

    The server persists ``timestamp`` as the row's ``event_date`` — "the
    turn's clock at its source, NULL when unmeasured" (claude_parts,
    ENG-675b). Cursor's artifacts only carry clocks for SOME records (user
    turns' embedded tags, store checkpoint nodes), so this fills the gap with
    the one other real clock available: the moment THIS hook first observed
    the record. Boundary hooks (afterAgentResponse / stop /
    beforeSubmitPrompt) fire when a turn actually completes, so first-seen is
    the turn's own wall clock to within one hook interval — never the
    import/flush clock of a whole-session re-send, which is the degeneracy
    this replaces (a 44-turn production session dated at a single instant).

    Pinning: the first stamp a record ever gets — artifact-carried or
    first-seen — is stored in the session state and re-applied verbatim on
    every later re-send, so a record's date never drifts with the re-flush
    schedule. ``None`` pins mean "observed, no clock": records already present
    at the session's FIRST observation (a mid-session install, a backfill)
    predate any hook by an unknowable margin, so they stay unmeasured rather
    than inheriting "now" — except a ``boundary_uuids`` record, which the
    current hook explicitly dates (its generation just ended). A ``None`` pin
    upgrades if the artifact later supplies a real clock for that record.

    With ``now_iso=None`` this only APPLIES existing pins (read-only replay
    for out-of-band senders); no new stamps are minted. Mutates ``records``
    in place; returns the updated pin map for persisting.

    A pin whose record is still in ``records`` is NEVER evicted: transcripts
    are append-only, so early records are re-sent on every flush, and losing
    their pin would re-mint "now" for them — re-dating an early turn at a
    much-later wall clock (review finding on the original FIFO cap). Only
    pins ORPHANED by the artifact (a checkpoint restore shifting the
    index-derived uuids) are pruned, and only once the map outgrows
    ``_RECORD_TS_PRUNE_TRIGGER`` — which is a prune trigger, not a bound;
    the map deliberately scales with the live artifact (see the constant).
    """
    stamps: dict = dict(prior) if isinstance(prior, dict) else {}
    present: set = set()
    for record in records:
        rid = record.get("uuid")
        if not isinstance(rid, str) or not rid:
            continue
        present.add(rid)
        reader_ts = record.get("timestamp")
        reader_ts = reader_ts if isinstance(reader_ts, str) and reader_ts else None
        if rid in stamps:
            pinned = stamps[rid]
            if not isinstance(pinned, str) or not pinned:
                pinned = None
            if pinned is None and reader_ts:
                stamps[rid] = pinned = reader_ts
            elif pinned is None and now_iso and rid in boundary_uuids:
                stamps[rid] = pinned = now_iso
        else:
            if reader_ts:
                pinned = reader_ts
            elif now_iso and (not first_observation or rid in boundary_uuids):
                pinned = now_iso
            else:
                pinned = None
            stamps[rid] = pinned
        if pinned:
            record["timestamp"] = pinned
        else:
            record.pop("timestamp", None)
    if len(stamps) > _RECORD_TS_PRUNE_TRIGGER:
        stamps = {rid: pin for rid, pin in stamps.items() if rid in present}
    return stamps


def apply_session_state(records: list[dict], uuid: str) -> None:
    """Restore live-observed fidelity onto an out-of-band re-read.

    capture.py (the manual import / sweep backstop for sessions whose
    per-event flush went dormant) re-reads the artifact from scratch, which
    carries neither the first-seen timestamp pins nor the exact hook usage
    this machine observed live. Re-apply both from the session's flush state,
    read-only — nothing here writes state, so a sweep can never perturb the
    live capture's watermarks.
    """
    state = _read_state(uuid)
    _stamp_records(records, state.get("record_ts"), None,
                   first_observation=True)
    _apply_usage(records, state.get("usage_events"))


def _records_revision(records: list[dict]) -> str:
    encoded = json.dumps(records, ensure_ascii=True, separators=(",", ":"),
                         sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def current_blob_ids(store_db: Path) -> set[str]:
    import sqlite3
    # 3s busy_timeout: Cursor is often mid-write (a checkpoint restore, an
    # active turn), and without it sqlite raises "database is locked"
    # instantly. On the _flush read path that holds the watermark, so a
    # consistently-busy store would re-send the whole transcript on every
    # event; a short wait turns a transient lock into a brief pause instead.
    con = sqlite3.connect(f"file:{store_db}?mode=ro", uri=True, timeout=3.0)
    try:
        con.execute("PRAGMA busy_timeout=3000")
        return {row[0] for row in con.execute("SELECT id FROM blobs")}
    finally:
        con.close()


def should_flush(event: str, payload: dict, state: dict,
                 blob_ids: set[str], now: float, *,
                 source_kind: str = "store",
                 source_revision: str | None = None,
                 usage_pending: bool = False) -> bool:
    """Pure gate — WHEN a hook invocation becomes a server call.

    New-blobs is a precondition for every event: without new content a flush
    is a guaranteed no-op round trip. On top of that, each event has its own
    threshold: milestones always ship (the commit/PR boundary is the whole
    point of milestone capture), turn boundaries ship, plain edits debounce,
    and non-milestone shell commands never trigger (too chatty).
    """
    # Dormancy gates EVERY event including the turn boundaries: a
    # persistently-down server is re-probed once per DORMANT_RETRY_S, never
    # hammered per-turn (boundaries can fire many times a minute). A boundary
    # whose tail falls inside a dormant window is captured by the
    # import-session sweep, not by attempting a full flush every turn against
    # an already-struggling backend. A successful re-probe clears the flag.
    if state.get("unsupported") and (
            now - (state.get("unsupported_at") or 0) < DORMANT_RETRY_S):
        return False
    # Pending usage deliberately retries an unchanged transcript, but never
    # bypasses the global dormancy gate above or the event-specific debounce
    # below. Failed delivery is therefore bounded by MAX_UNCONFIRMED and then
    # by DORMANT_RETRY_S instead of becoming a per-hook upload loop.
    if not usage_pending:
        if source_kind == "transcript":
            if not source_revision or source_revision == state.get(
                    "transcript_revision"):
                return False
        elif blob_ids <= set(state.get("blob_ids") or []):
            return False
    if event == "beforeShellExecution":
        # The command is untrusted payload and need not be a str — a host
        # version could send argv as a list or a dict, and `(list or "")[:512]`
        # slices the list, then re.search on it raises TypeError, which would
        # escape should_flush and kill the hook. Non-str → no milestone.
        cmd = payload.get("command")
        if not isinstance(cmd, str):
            return False
        # Bound the match input (see _MILESTONE_SCAN_LIMIT): the regex is
        # linear, so this only guards against a pathological megabyte-long
        # argument — it is large enough that a real `git commit`/`gh pr` verb,
        # even behind a long wrapper prefix, is never truncated away.
        return bool(_MILESTONE_RE.search(cmd[:_MILESTONE_SCAN_LIMIT]))
    if event == "afterFileEdit":
        return now - (state.get("last_flush_at") or 0) > DEBOUNCE_S
    if event in ("afterAgentResponse", "stop", "beforeSubmitPrompt",
                 "sessionEnd"):
        return True
    return False


# Reading a remote URL means reading the TARGET repo's own config — that is
# the data we want — but a repo's local config can also carry execution
# primitives (core.fsmonitor runs a command, credential helpers run on
# network access, hooksPath redirects hooks). The store's cwd is
# semi-trusted, so those are disarmed explicitly rather than relying on
# `remote get-url` not happening to reach them today. Verified: a repo whose
# core.fsmonitor is a command runs it under plain git and does not here.
def _verdict(res, expected_conversation_id: str | None = None) -> str:
    """``"ok"`` | ``"unconfirmed"`` | ``"unsupported"`` for an import reply.

    MCP reports tool failure through isError, not an exception, and this
    backend has shipped a mode where records are dedup-registered WITHOUT
    being persisted (records_dropped>0, ack_through null) — the failure that
    hid Cursor sessions for months. So a returned call is not a stored call.

    The three-way split exists because a server that OMITS ack_through poses
    a dilemma neither binary answer settles: treat it as confirmed and a
    server that silently dropped everything costs the session (a session's
    last flush has no later event to retry); treat it as unconfirmed and the
    watermark never advances, so every event re-uploads the whole transcript
    forever. flush_turn already answered this on the Claude path — go
    DORMANT: hold the watermark, stop flushing this session, and let the
    idempotent import-session sweep carry it. No loss, no loop.
    """
    if getattr(res, "isError", False):
        _log(f"server rejected the import: {mcp_http.texts_of(res)[:1]}")
        return "unconfirmed"
    ack = mcp_http.ack_of(res, expected_conversation_id)
    if ack is None:
        _log("import response unrecognized — holding the watermark")
        return "unconfirmed"
    if ack.get("records_dropped"):
        _log(f"server dropped {ack['records_dropped']} record(s) — "
             f"holding the watermark")
        return "unconfirmed"
    if "ack_through" not in ack:
        return "unsupported"
    if not ack["ack_through"]:
        # Present and null: a server that KNOWS the field telling us it
        # stored nothing. Retrying is right — this is transient.
        _log("import NOT confirmed (ack_through null) — holding the "
             "watermark so the next event re-sends")
        return "unconfirmed"
    return "ok"


def _cwd_ok(cwd: str | None) -> bool:
    """``cwd`` is read out of the cursor STORE, so it is session content, not
    a trusted path. Anything handed to `git -C` must be an absolute existing
    directory that cannot be read as an option — git honors the local config
    of whatever repository it is pointed at."""
    # isinstance FIRST: the store is content, not a contract — a corrupt or
    # hostile meta.json can carry a list or a number here, and .startswith
    # would raise AttributeError, which the clause below does not catch and
    # which would cost the whole flush.
    if not isinstance(cwd, str) or not cwd or cwd.startswith("-"):
        return False
    try:
        return Path(cwd).is_absolute() and Path(cwd).is_dir()
    except (OSError, ValueError):
        return False


def _namespace_of(cwd: str | None) -> str | None:
    """Git-remote basename for the session's cwd — the working-context scope
    stamp, resolved client-side exactly like flush_turn._namespace (a
    worktree directory's basename would HIDE directives from the canonical
    repo's recalls)."""
    if not _cwd_ok(cwd):
        return None
    try:
        out = subprocess.run(
            git_readonly(cwd) + ["remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2, env=git_env(),
        )
        url = out.stdout.strip()
        if out.returncode == 0 and url:
            return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    return None


async def _flush(uuid: str, source_path: Path, blob_ids: set[str],
                 flush_mode: str = "now", *, source_kind: str = "store",
                 source_revision: str | None = None,
                 records: list[dict] | None = None, meta: dict | None = None,
                 applied_usage: set[str] | None = None) -> None:
    if records is None or meta is None:
        records, meta = cursor_reader.to_canonical(source_path)
        # main() stamps before calling; this self-read path must not ship
        # unpinned records (their dates would vary with the re-read).
        apply_session_state(records, uuid)
    # Close the read span IMMEDIATELY — before redaction and the network call,
    # which together can run for many seconds. Blobs present at both the gate
    # and here existed for the whole span the payload was built from, so their
    # content is in it; that pair is the honest watermark. Taken after the
    # send instead, this read would span the entire round trip, and a
    # checkpoint restore in that window could shrink the intersection to
    # nearly nothing and re-send the same content on every later hook.
    # Bound BEFORE the try so its definedness never depends on how broad the
    # clause below happens to be — that breadth has already changed twice
    # under review, and the reader below must not be one edit away from an
    # UnboundLocalError.
    shipped: set[str] | None = None
    try:
        if source_kind == "store":
            shipped = blob_ids & current_blob_ids(source_path)
    except Exception as e:  # noqa: BLE001 — see below
        # Unreadable mid-flush: we cannot say which blobs survived the read,
        # so the watermark is left ALONE rather than advanced to the gate set
        # — claiming the full set would mark blobs shipped that a checkpoint
        # restore may have removed before the payload was built. Cost is one
        # redundant re-send on the next event, the documented safe direction.
        # BROAD on purpose. This read exists only to decide what to record
        # in the watermark — a local optimization. Letting any failure out of
        # here would unwind past redact/send and drop an upload we already
        # have the records for, trading a redundant re-send (cheap, the
        # server folds it forward) for a lost one (only healed by a later
        # hook). Narrowing to sqlite3.Error/OSError left TypeError,
        # MemoryError and driver-specific classes escaping into exactly that.
        # The visibility this catch would otherwise cost is bought back by
        # the log below rather than by a narrower clause: a transient
        # locked/mid-write store is the expected case, while a PERSISTENT
        # failure (a schema change renaming `blobs`) re-sends the whole
        # transcript on every hook forever and has to be findable.
        _log(f"blob read failed after the transcript read ({e!r}) — watermark "
             f"held; if this repeats every flush, the store schema has moved "
             f"and readers/cursor.py needs updating")
        shipped = None
    sendable = redact_records(records)
    if not sendable:
        if records:
            _log(f"all {len(records)} record(s) redacted away — nothing to "
                 f"send (check redact rules if this recurs on real content)")
        # Redaction is structure-preserving today, so a non-empty input cannot
        # become empty. Keep the watermark held for that defensive future case:
        # advancing it would make content unrecoverable after an over-broad
        # redaction rule is fixed. A genuinely empty transcript has no content
        # to lose, so mark its revision examined and avoid parsing it again at
        # every turn boundary until Cursor writes something new.
        fields = {"last_flush_at": time.time(), "fail_streak": 0}
        if (source_kind == "transcript" and source_revision and not records):
            fields["transcript_revision"] = source_revision
        _save_state(uuid, **fields)
        return

    try:
        url, bearer = await asyncio.to_thread(resolve_bearer)
    except Exception as e:  # noqa: BLE001
        # Auth resolution is LOCAL (token cache, refresh) — a failure here is
        # the no_credential case, not a contacted-server failure, so it must
        # not count toward dormancy. Clear the streak and skip, exactly like
        # the no-bearer path below.
        _log(f"credential resolve failed ({e!r}) — skipping (run /memhub:login)")
        _save_state(uuid, last_error="no_credential", fail_streak=0)
        return
    if not bearer:
        _log("no usable credential — skipping (run /memhub:login)")
        # A local auth gap, not a server failure — never contacted it, so
        # clear any failure run rather than let a login blip tip a session
        # toward dormancy.
        _save_state(uuid, last_error="no_credential", fail_streak=0)
        return
    env = env_for_url(url)
    session = mcp_http.Session(url, bearer, timeout=FLUSH_TIMEOUT_S / 2)

    cwd = meta.get("cwd")
    # Both derive from cwd alone and neither feeds the other, so they run
    # CONCURRENTLY: one is a network round trip, the other a `git remote
    # get-url` subprocess with a 2s budget (off the loop, like resolve_bearer
    # above). Awaiting them in series spent the flush deadline twice over for
    # no ordering reason.
    # ONE guard for both consumers: cwd is store content, and
    # resolve_repo_brain resolves it as a path too — validating only inside
    # _namespace_of would have left that half open.
    if _cwd_ok(cwd):
        try:
            room, namespace = await asyncio.gather(
                resolve_repo_brain(session, cwd, env),
                asyncio.to_thread(_namespace_of, cwd),
            )
        except Exception as e:  # noqa: BLE001
            # resolve_repo_brain is documented never to raise (its body is a
            # broad except returning None), so this is belt-and-braces — but
            # if it ever did, ABORT and retry rather than either of the wrong
            # answers: degrading room to None would route the FIRST receive to
            # personal LTM and set the conversation's partition there stickily,
            # and letting it propagate would count a routing hiccup as an
            # import failure toward dormancy. A clean return does neither.
            # Not _note_failure: a routing hiccup is local, not a
            # server-import failure, so it must not count toward dormancy or
            # degrade room to None (which would mis-home the partition). The
            # afterFileEdit debounce already rate-limits the frequent event;
            # boundaries retry next turn.
            _log(f"room/namespace resolve failed transiently ({e!r}) — "
                 f"retrying next event")
            # Advance the debounce (like the empty-redaction / no-credential
            # paths): a persistent resolve failure — a wedged git subprocess,
            # a repeatedly-failing brain resolve — would otherwise re-run the
            # full parse + redact + 2s git probe on every afterFileEdit. And
            # like those paths, CLEAR fail_streak: the server was never
            # contacted, so this neutral no-op must not preserve a prior run
            # of contacted failures that a later single failure tips into
            # dormancy (see _note_failure's documented contract).
            _save_state(uuid, last_error="resolve_error",
                        last_flush_at=time.time(), fail_streak=0)
            return
    else:
        if cwd:
            _log(f"ignoring unusable cwd from Cursor source: {str(cwd)[:60]!r}")
        room, namespace = None, None

    arguments = {
        "messages": sendable,
        # Host-namespaced so server-side watermarks never collide across
        # hosts, matching the codex importer's convention.
        "conversation_id": f"cursor-{uuid}",
        # The agentic path detects by STRUCTURE; the records carry a Cursor
        # provenance banner (see readers/cursor.py).
        "source_platform": cursor_reader.HOST,
        "flush": flush_mode,
    }
    if room:
        arguments["agent_brain_id"] = room["brain_id"]
        if room.get("org_id"):
            arguments["org_id"] = room["org_id"]
    if namespace:
        # Same scope stamp flush_turn sends: directives extracted from this
        # session must recall in this repo's context, not everywhere.
        arguments["namespace"] = namespace
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        # Bound a semi-trusted session title (store content, like cwd): a
        # non-str from a corrupt meta is dropped rather than sent as-is, and a
        # runaway length is capped so it can't bloat every re-send.
        arguments["title"] = title.strip()[:200]

    try:
        res = await session.call_tool("import_conversation",
                                      arguments=arguments)
    except mcp_http.McpRateLimited as e:
        _log(f"rate limited: {e}")
        _note_failure(uuid, "rate_limited")
        return
    except mcp_http.McpError as e:
        _log(f"import failed: {e}")
        _note_failure(uuid, f"mcp_error: {str(e)[:80]}")
        return

    # A returned call is NOT a persisted call. MCP signals tool failure with
    # isError rather than an exception, and this backend has shipped a
    # 200-with-nothing-stored mode before (records dedup-registered without
    # persisting: records_dropped>0, ack_through null — the same failure that
    # hid Cursor sessions for months). Advancing the watermark on such a reply
    # marks the blobs shipped, and since a session's LAST flush has no later
    # event to re-send it, that is a silently lost conversation. So: confirm
    # the ack, or hold the watermark and let the next event re-send. Mirrors
    # flush_turn's discipline on the Claude path.
    verdict = _verdict(res, f"cursor-{uuid}")
    if verdict == "unsupported":
        # Dormant for this session rather than looping: the watermark stays
        # put (nothing is claimed shipped) and no further flush runs, so an
        # unconfirmable server costs redundant work exactly once instead of
        # on every event. /memhub:import-session still captures the session.
        _log("server does not report ack_through — per-event flush is "
             "dormant for this session; run /memhub:import-session to "
             "capture it, or upgrade the server")
        _save_state(uuid, last_flush_at=time.time(), unsupported=True,
                    unsupported_at=time.time(), fail_streak=0)
        return
    if verdict != "ok":
        _note_failure(uuid, "unconfirmed_import")
        return

    # `shipped` was fixed at the end of the transcript read (see above), NOT
    # re-read here: a post-send read would span the whole network round trip.
    # The timestamps land either way — the debounce must still hold after a
    # successful send — but blob_ids only when we could actually verify it.
    fields = {"last_flush_at": time.time(), "last_ok_at": time.time(),
              "last_error": None,
              # The re-probe worked: this server confirms after all, so the
              # session re-arms instead of staying dormant on old evidence.
              "unsupported": False, "unsupported_at": 0,
              "fail_streak": 0}
    if source_kind == "store":
        # On a CONFIRMED import, advance the watermark even when the post-read
        # failed (shipped is None): the server acked the payload built from the
        # gate set. A checkpoint restore's newly-added blobs still differ next
        # time, while removed blobs cannot be recovered by withholding state.
        fields["blob_ids"] = sorted(
            shipped if shipped is not None else blob_ids)
        if shipped is None:
            _log("store unreadable after the transcript read — watermark "
                 "advanced to the gate set on this confirmed import")
    elif source_revision:
        fields["transcript_revision"] = source_revision
    if applied_usage is not None:
        fields["sent_usage_generations"] = sorted(applied_usage)
    _save_state(uuid, **fields)
    _log(f"flushed {len(sendable)} records → cursor-{uuid}"
         + (f" (room {room['brain_id'][:8]}…)" if room else " (personal)"))


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}

    uuid = session_uuid(payload)
    if not uuid:
        keys = sorted(str(key) for key in payload)
        _log(f"{event}: no session identity in payload (keys={keys!r}) — "
             "skipping")
        return 0
    if not _UUID_RE.fullmatch(uuid):
        # session_uuid returns session_id/conversation_id verbatim, and
        # cursor_reader.locate treats a resolvable value as a PATH — so a
        # non-uuid id (a path, "..") could otherwise open an arbitrary
        # sqlite file and upload its blobs. The store lookup requires a real
        # session uuid; the transcript_path fallback is already uuid-checked.
        _log(f"{event}: session id {uuid!r} is not a uuid — refusing the "
             f"store lookup")
        return 0

    # Serialize the whole check-then-act: reading the watermark, sending, and
    # writing it back must not interleave with a concurrent flush for this
    # session, or both upload the same transcript and race the watermark.
    # Turn boundaries (stop / beforeSubmitPrompt) are last-chance events — a
    # session may have no later flush — so they WAIT for a concurrent flush
    # rather than skip and never run again. Mid-turn events (edits, milestone
    # shell) skip when busy: another will follow. Once acquired, should_flush
    # re-checks new-blobs, so a waited boundary ships only the delta or no-ops.
    lock_fd = _acquire(
        uuid, blocking=(event in ("afterAgentResponse", "stop",
                                  "beforeSubmitPrompt", "sessionEnd")))
    if lock_fd is None:
        _log(f"{event}: another flush is running for this session — skipping")
        return 0
    try:
        state = _read_state(uuid)
        source_kind, source_path, err = _source_for(uuid, payload, state)
        if source_kind is None or source_path is None:
            _log(f"{event}: {err or 'no readable Cursor source'}")
            return 0

        cursor_meta = _payload_meta(payload, state)
        blob_ids: set[str] = set()
        source_revision: str | None = None
        try:
            if source_kind == "store":
                blob_ids = current_blob_ids(source_path)
                if not blob_ids:
                    # A readable empty store is mid-rebuild. Leave every
                    # watermark untouched so the next hook retries it.
                    _log(f"{event}: store reports zero blobs (rebuilding?) — "
                         "skipping")
                    return 0
                records, meta = cursor_reader.to_canonical(source_path)
            else:
                records, meta = cursor_reader.to_canonical(
                    source_path, session_id=uuid,
                    cwd=cursor_meta.get("cwd"), model=cursor_meta.get("model"))
        except Exception as e:  # locked/partial/corrupt source — next hook retries
            _log(f"{event}: {source_kind} source unreadable ({e}) — skipping")
            return 0

        # Persist source identity, exact usage, and timestamp pins BEFORE any
        # network work. afterAgentResponse and stop duplicate the same
        # generation; replacing that dictionary key makes delivery idempotent,
        # while a later hook can retry an auth/server failure without needing
        # Cursor to repeat usage. Timestamp pins likewise: a record's date is
        # fixed the moment it is first OBSERVED, so a failed send retries with
        # the same stamps instead of re-dating the records at retry time.
        fields: dict = {"source_kind": source_kind, "cursor_meta": cursor_meta}
        if source_kind == "transcript":
            fields["transcript_path"] = str(source_path)
        usage_events = state.get("usage_events")
        usage_events = dict(usage_events) if isinstance(usage_events, dict) else {}
        boundary_uuids: set[str] = set()
        sample = _hook_usage(event, payload)
        if sample is not None:
            generation, usage = sample
            expected_text = payload.get("text") if event == "afterAgentResponse" else None
            target = _last_assistant_uuid(records, expected_text)
            if target is None:
                _log(f"{event}: exact usage has no matching final assistant "
                     "record — leaving this turn unmeasured")
            else:
                usage_events = _usage_events_with(
                    state, generation, target, usage)
                fields["usage_events"] = usage_events
                # This hook explicitly dates that record: its generation ended
                # NOW, so it gets a real clock even in a first-observation
                # backlog (where everything else stays unmeasured).
                boundary_uuids = {target}
        elif (event in ("afterAgentResponse", "stop") and
              any(key in payload for key in _HOOK_USAGE_KEYS)):
            _log(f"{event}: malformed token counters or generation_id — "
                 "leaving this turn unmeasured")

        fields["record_ts"] = _stamp_records(
            records, state.get("record_ts"), _now_iso(),
            first_observation="record_ts" not in state,
            boundary_uuids=boundary_uuids)
        if source_kind == "transcript":
            # AFTER stamping: the revision must hash the shape that ships.
            # Pins make it stable across invocations — it moves only when a
            # new record (uuid) appears, which is precisely "new content".
            source_revision = _records_revision(records)

        _save_state(uuid, **fields)
        state.update(fields)
        applied_usage = _apply_usage(records, usage_events)
        sent_usage = set(state.get("sent_usage_generations") or [])
        usage_pending = bool(applied_usage - sent_usage)

        if not should_flush(
                event, payload, state, blob_ids, time.time(),
                source_kind=source_kind, source_revision=source_revision,
                usage_pending=usage_pending):
            return 0
        try:
            mode = _FLUSH_MODE.get(event, "now")
            asyncio.run(asyncio.wait_for(
                _flush(
                    uuid, source_path, blob_ids, mode,
                    source_kind=source_kind, source_revision=source_revision,
                    records=records, meta=meta, applied_usage=applied_usage),
                timeout=FLUSH_TIMEOUT_S))
        except Exception as e:
            # A timeout or any raise past _flush's own handlers (the broad
            # case, including asyncio.wait_for firing) counts toward dormancy
            # too — otherwise a hard-down backend re-parses and re-uploads the
            # whole store on every event forever, and Stop is cooldown-exempt
            # so nothing else bounds it.
            _log(f"{event}: flush error: {e}")
            _note_failure(uuid, f"flush_error: {type(e).__name__}")
    finally:
        os.close(lock_fd)  # releases the flock
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
