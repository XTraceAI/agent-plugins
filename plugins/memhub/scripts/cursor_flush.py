#!/usr/bin/env python3
"""Cursor capture: hook-triggered flush of a cursor-agent session into MemHub.

The Cursor analog of ``flush_turn.py``, reusing its whole downstream —
``redact``, ``_memhub_auth.resolve_bearer``, ``room_map``'s git helpers,
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

Cursor IDE 3.17 emits ``afterAgentResponse`` and ``stop`` with exact usage,
whose input_tokens includes cache reads — it is stored net of them
(``_hook_usage``) — and ``beforeSubmitPrompt`` as each prompt is sent; it has not been seen to
fire ``sessionEnd`` when a chat closes, so ``stop`` is its last flush.
Cursor Agent CLI (measured on 2026.10.16) fires no ``beforeSubmitPrompt``,
``afterAgentResponse`` or ``stop`` at all: a CLI turn fires ``sessionStart``
(first invocation only — never on ``--resume`` — with a null
``transcript_path``), ``afterAgentThought``, the tool / shell / read hooks,
and ``sessionEnd`` at the end of EVERY invocation. Its usage lives solely in
caller-owned stream output, so ``sessionEnd`` captures CLI content with
nullable usage rather than estimating it.

The transcript's own clocks are coarse: a user turn's ``<timestamp>`` tag is
minute precision, and nothing else in it is dated. So three hooks contribute
their OWN wall clock (no Cursor payload carries a timestamp): ``sessionStart``
saves ``session_start_at`` without reading the transcript or flushing, and it
dates the session's banner and first prompt when the tag is all they have;
``beforeSubmitPrompt`` queues an exact clock for the prompt it announces; and
``sessionEnd`` dates the final assistant record of the invocation's turn.
``beforeShellExecution`` flushes only on milestone commands (git commit /
gh pr …) so ordinary shell traffic stays quiet. ``afterShellExecution``
captures the exact URL returned by a direct ``gh pr create`` because current
Cursor JSONL carries tool calls but not their results. ``afterFileEdit`` is
debounced. Whatever the trigger set misses, the next flush or an import-session
sweep heals — idempotence carries correctness, hooks carry latency.

Invoked by the portable Cursor launcher, which answers the hook's permission
contract immediately and re-runs this script detached - a slow server must
never hold up the user's shell command.

Runs under bare python3 (stdlib + sibling modules only), same
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
import agent_capture  # noqa: E402
import atomic_write  # noqa: E402
import cursor_agents  # noqa: E402
import portable_lock  # noqa: E402
import mcp_http  # noqa: E402
import pr_provenance  # noqa: E402
import server_caps  # noqa: E402
from _memhub_auth import resolve_bearer, skill_command  # noqa: E402
from readers import cursor as cursor_reader  # noqa: E402
from readers.strict_json import loads as load_json  # noqa: E402
from redact import redact_records, redact_text  # noqa: E402
from transcript_filter import elide_oversized_tool_results  # noqa: E402
from room_map import git_env, git_readonly  # noqa: E402

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
        _save_state(uuid, last_flush_at=now, last_error=reason, last_error_at=now,
                    unsupported=True, unsupported_at=now, fail_streak=0)
        return
    streak = int(st.get("fail_streak") or 0) + 1
    if streak >= MAX_UNCONFIRMED:
        _log(f"{streak} consecutive failed imports ({reason}) — per-event "
             f"flush is dormant for this session; run {skill_command('import-session')} "
             f"to capture it. Re-probes in {DORMANT_RETRY_S / 60:.0f} min.")
        _save_state(uuid, last_flush_at=now, last_error=reason, last_error_at=now,
                    unsupported=True, unsupported_at=now, fail_streak=0)
    else:
        # Sub-threshold: back off via the debounce (last_flush_at), stay in
        # normal mode (unsupported cleared) so the next event keeps counting.
        _save_state(uuid, last_flush_at=now, last_error=reason, last_error_at=now,
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
    "afterShellExecution": "now",    # exact direct gh-pr-create evidence
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
    # IDE only: a sub-agent ended. Its parent's transcript flushes on the
    # afterFileEdit debounce; the agent lane runs either way.
    "subagentStop": "now",
}

# Events after which the agent lane looks for the session's sub-agents
# (cursor_agents): the parent's turn or session ended, a sub-agent ended, or
# a later prompt arrived (a background agent can finish between turns). The
# lane runs after the parent's confirmed save, or on its own when the parent
# had nothing new but was saved before. afterAgentResponse runs it only with
# a parent send; its stop follows at once.
_AGENT_EVENTS = ("stop", "sessionEnd", "subagentStop", "beforeSubmitPrompt")


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


def _read_state(uuid: str, *, strict: bool = False, text: str | None = None) -> dict:
    """``text`` lets a caller that already validated and opened the state file
    parse those exact bytes, instead of reopening the path and racing a swap."""
    try:
        if text is None:
            text = _state_path(uuid).read_text(encoding="utf-8")
        state = load_json(text, strict=strict)
        if strict and (not isinstance(state, dict) or any(
                key in state and not isinstance(state[key], dict)
                for key in ("record_ts", "usage_events"))):
            raise ValueError("invalid saved Cursor observations")
        if strict:
            import datetime
            for target_uuid, stamp in state.get("record_ts", {}).items():
                if (not isinstance(target_uuid, str) or not _UUID_RE.fullmatch(target_uuid)
                        or (stamp is not None and (not isinstance(stamp, str) or
                        datetime.datetime.fromisoformat(
                            stamp.replace("Z", "+00:00")).tzinfo is None))):
                    raise ValueError("invalid saved Cursor timestamp")
            for generation, event in state.get("usage_events", {}).items():
                target_uuid = event.get("target_uuid") if isinstance(event, dict) else None
                if (not isinstance(generation, str) or not _UUID_RE.fullmatch(generation)
                        or not isinstance(target_uuid, str) or not _UUID_RE.fullmatch(target_uuid)
                        or cursor_reader.normalize_usage(event.get("usage")) is None
                        or event.get("basis", _USAGE_BASIS_NET) != _USAGE_BASIS_NET):
                    raise ValueError("invalid saved Cursor usage")
        return state
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            raise
        return {}


def _update_state(uuid: str, update) -> None:
    """Apply ``update(state)`` (mutating it) under the same per-uuid lock as
    ``_save_state``: a field that is itself merged — the subagentStop roster,
    written by parallel children ending together — must be read inside it."""
    _save_state(uuid, _update=update)


def _save_state(uuid: str, **fields) -> None:
    """Merge ``fields`` into this session's state under a per-uuid lock.
    ``_update_state`` is the same with a callback for a read-modify-write.

    atomic_write makes each PUBLISH atomic, but the read-modify-write around
    it is not: the Cursor launcher runs every flush DETACHED, so an
    afterFileEdit and a stop firing close together can both read the old
    state and the later writer silently drops the other's blob_ids or
    last_flush_at — regressing the watermark into redundant re-sends. The
    lock is a separate .lock file, never the state file itself: locking a
    file that is about to be replaced releases the lock with the old inode.
    """
    update = fields.pop("_update", None)
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
        if update is not None:
            update(state)
        state.update(fields)
        atomic_write.publish(_state_path(uuid), json.dumps(state))
    finally:
        if fh is not None:
            try:
                portable_lock.unlock(portable_lock.fileno_of(fh))
            except OSError:
                pass
            fh.close()


def _acquire(uuid: str, blocking: bool = False, *,
             observations: bool = False) -> int | None:
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
    suffix = "observations.lock" if observations else "flush.lock"
    fd = os.open(STATE_DIR / f"{_safe_uuid(uuid)}.{suffix}",
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


def _valid_transcript_path(raw, uuid: str, *, root: Path | None = None) -> tuple[Path | None, str]:
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
        # A caller that anchored the configured root earlier in its run passes
        # that target, so a root retargeted since cannot change the verdict.
        root = _CURSOR_PROJECTS.resolve(strict=True) if root is None else Path(root)
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
    #
    # Readability, not existence, decides. cursor_reader.locate names
    # <session>/store.db without opening it, and a cursor-agent CLI session
    # holds that database open until it exits — so at sessionEnd, the one hook
    # that captures a CLI session, the file is there and unreadable. Choosing
    # it anyway made every cursor-agent session log "store source unreadable —
    # skipping" and reach MemHub never, while its transcript sat beside it
    # fully written. Measured on cursor-agent 2026.09.10 (harness lane
    # harness/drivers/cursor_run.py: 0 of 3 cases captured before, 3 of 3
    # after).
    store, store_err = cursor_reader.locate(uuid)
    if store is not None and store.name == "store.db":
        if cursor_reader.readable(store):
            return "store", store, ""
        store_err = f"store exists but cannot be read yet ({store})"
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


# What a persisted usage_events entry's counters mean. "net" is the
# Anthropic/backend convention: input_tokens EXCLUDES cache reads. An entry
# with no "basis" was saved by an older plugin straight from the IDE hook, so
# it is gross and is converted when applied. A source that is already net
# (cursor-agent's stream-json result, should it ever be forwarded) is stored
# as "net" unchanged and is never subtracted.
_USAGE_BASIS_NET = "net"


def _net_of_cache(usage: dict[str, int]) -> dict[str, int]:
    # Cursor IDE afterAgentResponse/stop report input_tokens GROSS: it
    # includes cache_read_tokens (a 145,087-token turn read 143,616 from
    # cache). Whether it also includes cache WRITES is unverified (every
    # recorded IDE turn wrote 0), so cache_creation_input_tokens is left as is.
    return dict(usage, input_tokens=max(
        0, usage["input_tokens"] - usage["cache_read_input_tokens"]))


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
    # The one place hook usage turns net; the shared normalize_usage and
    # _apply_usage must not subtract again.
    return generation, _net_of_cache(usage)


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
    # If Cursor regenerates a visible turn onto the same deterministic record,
    # retain the latest exact sample rather than summing unlike attempts.
    events = {key: value for key, value in events.items()
              if not (isinstance(value, dict) and
                      value.get("target_uuid") == target_uuid)}
    events[generation] = {"target_uuid": target_uuid, "usage": usage,
                          "basis": _USAGE_BASIS_NET}
    # Native files cannot reconstruct these exact samples. Keep one sample
    # per measured record for later local reads, even after cloud acknowledgement.
    # Like timestamp pins, this map scales with the observed session.
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
        normalized = cursor_reader.normalize_usage(event.get("usage"))
        if normalized is not None and "basis" not in event:
            # Saved gross by a plugin before the basis marker. The entry on
            # disk is never rewritten here, so each read converts it once; a
            # re-sent generation replaces it with a marked net entry.
            normalized = _net_of_cache(normalized)
        elif event.get("basis") != _USAGE_BASIS_NET:
            normalized = None  # a basis this build does not know: unmeasured
        message = record.get("message") if isinstance(record, dict) else None
        if (not isinstance(record, dict) or record.get("type") != "assistant" or
                not isinstance(message, dict) or normalized is None):
            continue
        # Canonical counters, never the saved alias spelling: validate_canonical
        # does not inspect usage, so a camelCase map would otherwise ship as-is.
        message["usage"] = normalized
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


def _parse_iso(value):
    import datetime
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_prompt(record: dict) -> bool:
    """A canonical prompt (or the banner): string content, unlike tool results."""
    message = record.get("message")
    return (record.get("type") == "user" and isinstance(message, dict)
            and isinstance(message.get("content"), str))


def _prompt_digest(text) -> str | None:
    """Whitespace-insensitive identity of a prompt, so a hook clock can be
    matched to its record without keeping the prompt's text in state."""
    if not isinstance(text, str) or not text.strip():
        return None
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:32]


# Hook clocks queued by beforeSubmitPrompt and not yet matched to a record.
# A prompt normally lands at the very next observation; the cap only bounds
# entries whose prompt Cursor never wrote (a blocked or aborted submit).
_MAX_PROMPT_CLOCKS = 8
# How far past the start of its tag's minute a session-start clock may fall
# and still be the same moment: a minute tag covers 60 s, plus hook skew.
_SESSION_START_SLACK_S = 65.0
_BANNER_PREFIX = "[Imported from Cursor"


def _exact_clocks(records: list[dict], state: dict, event: str,
                  hook_at: str | None) -> tuple[dict, list]:
    """Hook wall clocks that beat what the transcript carries, by record uuid.

    Only records not yet dated are eligible (no pin, or an unmeasured one) —
    a pin is never re-dated. Returns the clocks and the queued prompt clocks
    they consumed:

    * ``beforeSubmitPrompt`` clocks go to the newest undated prompt with the
      same text, when its tag is minute-truncated (or absent) and not later
      than the clock. A minute tag says 02:40:00 for a prompt sent 02:40:31.
    * ``session_start_at`` dates the banner and the first prompt, only where
      the tag is absent or the start falls inside the tag's minute — an IDE
      chat opened long before its first prompt keeps the tag.
    * ``sessionEnd`` dates the invocation turn's final assistant record, the
      way exact usage marks its boundary record.
    """
    pins = state.get("record_ts")
    pins = pins if isinstance(pins, dict) else {}

    def undated(rid) -> bool:
        return isinstance(rid, str) and bool(rid) and not pins.get(rid)

    def tag_of(record):
        tag = _parse_iso(record.get("timestamp"))
        if tag is None:
            return None, True
        return tag, tag.second == 0 and tag.microsecond == 0

    clocks: dict = {}
    consumed: list = []
    prompts = [record for record in records if _is_prompt(record)]
    banner = prompts[0] if prompts and str(
        prompts[0]["message"]["content"]).startswith(_BANNER_PREFIX) else None
    asks = [record for record in prompts if record is not banner]

    queued = state.get("prompt_clocks")
    for entry in reversed(queued if isinstance(queued, list) else []):
        at = _parse_iso(entry.get("at")) if isinstance(entry, dict) else None
        if at is None:
            continue
        for record in reversed(asks):
            rid = record.get("uuid")
            if (not undated(rid) or rid in clocks or
                    _prompt_digest(record["message"]["content"]) != entry.get("digest")):
                continue
            tag, coarse = tag_of(record)
            if coarse and (tag is None or at >= tag):
                clocks[rid] = entry["at"]
                consumed.append(entry)
            break

    start_raw = state.get("session_start_at")
    start = _parse_iso(start_raw)
    if start is not None:
        for record in (banner, asks[0] if asks else None):
            rid = record.get("uuid") if record else None
            if not undated(rid) or rid in clocks:
                continue
            tag, coarse = tag_of(record)
            if tag is None or (coarse and 0 <= (start - tag).total_seconds()
                               < _SESSION_START_SLACK_S):
                clocks[rid] = start_raw

    if event == "sessionEnd" and _parse_iso(hook_at) is not None:
        target = _last_assistant_uuid(records)
        if undated(target):
            clocks[target] = hook_at
    return clocks, consumed


def _stamp_records(records: list[dict], prior, now_iso: str | None, *,
                   first_observation: bool,
                   boundary_uuids: frozenset | set = frozenset(),
                   clocks: dict | None = None) -> dict:
    """Date every record with a REAL clock, pinned across re-sends.

    The server persists ``timestamp`` as the row's ``event_date`` — "the
    turn's clock at its source, NULL when unmeasured" (claude_parts,
    ENG-675b). Cursor's artifacts only carry clocks for SOME records (user
    turns' embedded tags, store checkpoint nodes), so this fills the gap with
    the one other real clock available: the moment THIS hook first observed
    the record. Once a session is under observation, any FLUSHING hook's
    sighting qualifies — the record provably exists at that instant, so
    first-seen bounds its production time from above by the gap since the
    previous event. (A debounced afterFileEdit — the one event that reads
    yet declines to send — skips minting so it never rewrites the pin map;
    the next flushing hook dates what it saw, a deferral bounded by the
    debounce interval. Guaranteed non-senders never read at all — see
    ``_event_can_flush``.) Boundary hooks (afterAgentResponse / stop /
    beforeSubmitPrompt) are the common observers, but a mid-turn edit hook
    that flushes and sees a record first dates it TIGHTER than waiting for
    the turn's end would — matching Claude transcripts, whose records carry
    per-record production times, not turn-boundary times. The session's FIRST-EVER observation is the one
    exception: its pre-existing backlog predates any hook by an unknowable
    margin, so those records are marked unmeasured rather than dated (the
    pinning rules below). What first-seen is never: the import/flush clock
    of a whole-session re-send, the degeneracy this replaces (a 44-turn
    production session dated at a single instant).

    Pinning: the first stamp a record ever gets — artifact-carried or
    first-seen — is stored in the session state and re-applied verbatim on
    every later re-send, so a record's date never drifts with the re-flush
    schedule. ``None`` pins mean "observed, no clock": records already present
    at the session's FIRST observation (a mid-session install, a backfill)
    predate any hook by an unknowable margin, so they stay unmeasured rather
    than inheriting "now" — except a ``boundary_uuids`` record, which the
    current hook explicitly dates (its generation just ended). A ``None`` pin
    upgrades if the artifact later supplies a real clock for that record; a
    MINTED pin is never displaced by one. That asymmetry is deliberate: a
    transcript record's embedded tag lives inside its content and exists
    from the record's birth (append-only files cannot grow one later — when
    both exist they are the same instant), while a store clock that APPEARS
    after a record was already observed live belongs to a NEWER checkpoint
    that re-parented the leaf — later than production, so the first-seen
    pin is the tighter bound. Displacement would also let one record ship
    different dates on the live path (pin) and the backstop re-read (reader
    clock) — exactly the flapping pinning exists to prevent.

    ``clocks`` (from ``_exact_clocks``) are hook wall clocks for specific
    records; each beats the record's own minute-precision tag and dates an
    unmeasured pin, like a boundary record. A prompt newly dated here is
    never dated before the record ahead of it: a minute tag that reads
    earlier than the previous turn's pin is raised to that pin, the tightest
    honest bound left, so turns never reorder.

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
    clocks = clocks if isinstance(clocks, dict) and now_iso else {}
    present: set = set()
    latest = None
    for record in records:
        rid = record.get("uuid")
        if not isinstance(rid, str) or not rid:
            continue
        present.add(rid)
        reader_ts = record.get("timestamp")
        reader_ts = reader_ts if isinstance(reader_ts, str) and reader_ts else None
        clock = clocks.get(rid)
        minted = False
        if rid in stamps:
            pinned = stamps[rid]
            if not isinstance(pinned, str) or not pinned:
                pinned = None
            if pinned is None and (clock or reader_ts):
                pinned, minted = clock or reader_ts, True
            elif pinned is None and now_iso and rid in boundary_uuids:
                pinned = now_iso
        else:
            minted = True
            if clock or reader_ts:
                pinned = clock or reader_ts
            elif now_iso and (not first_observation or rid in boundary_uuids):
                pinned = now_iso
            else:
                pinned = None
        if minted and now_iso and pinned and latest and _is_prompt(record):
            moment = _parse_iso(pinned)
            if moment is not None and moment < latest[0]:
                pinned = latest[1]
        if rid not in stamps or pinned != stamps[rid]:
            stamps[rid] = pinned
        moment = _parse_iso(pinned)
        if moment is not None and (latest is None or moment > latest[0]):
            latest = (moment, pinned)
        if pinned:
            record["timestamp"] = pinned
        else:
            record.pop("timestamp", None)
    if len(stamps) > _RECORD_TS_PRUNE_TRIGGER:
        stamps = {rid: pin for rid, pin in stamps.items() if rid in present}
    return stamps


def apply_session_state(records: list[dict], uuid: str, *, strict: bool = False,
                        state_text: str | None = None,
                        state: dict | None = None) -> None:
    """Restore live-observed fidelity onto an out-of-band re-read.

    capture.py (the manual import / sweep backstop for sessions whose
    per-event flush went dormant) re-reads the artifact from scratch, which
    carries neither the first-seen timestamp pins nor the exact hook usage
    this machine observed live. Re-apply both from the session's flush state,
    read-only — nothing here writes state, so a sweep can never perturb the
    live capture's watermarks.
    """
    if not isinstance(uuid, str) or not _UUID_RE.fullmatch(uuid):
        # Same gate main() applies before touching per-session state. The
        # backstop's id comes from a caller-chosen path/meta, so a non-uuid
        # must not select a state file (even a sanitized one) — skipping the
        # restore just leaves the records with their artifact-carried clocks.
        return
    if state is None:
        # ``state`` lets a caller that already validated and parsed the saved
        # observations (or established their absence) apply them without any
        # path being reopened here.
        state = (_read_state(uuid, strict=True, text=state_text) if strict
                 else _read_state(uuid, text=state_text))
    _stamp_records(records, state.get("record_ts"), None,
                   first_observation=True)
    _apply_usage(records, state.get("usage_events"))


def _records_revision(records: list[dict]) -> str:
    return cursor_agents.records_revision(records)


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
                 usage_pending: bool = False,
                 provenance_pending: bool = False) -> bool:
    """Pure gate — WHEN a hook invocation becomes a server call.

    New content is normally required. Pending post-shell PR telemetry is the
    exception: current Cursor transcripts omit tool results, so it must be
    allowed to ride a deduplicated transcript resend until acknowledged.
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
    if not usage_pending and not provenance_pending:
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
    if event == "afterShellExecution":
        return provenance_pending
    if event in ("afterFileEdit", "subagentStop"):
        return now - (state.get("last_flush_at") or 0) > DEBOUNCE_S
    if event in ("afterAgentResponse", "stop", "beforeSubmitPrompt",
                 "sessionEnd"):
        return True
    return False


def _event_can_flush(event: str, payload: dict) -> bool:
    """Purely payload-decidable: can this invocation EVER become a send?

    Non-milestone shell events and unrecognized events cannot — the event
    gate in ``should_flush`` refuses them regardless of content, and pending
    usage does not override it — so ``main`` exits before locking and
    reading the transcript. That is an I/O saving (no parse per shell
    command), but the load-bearing property is OBSERVATION: every event
    that reads the transcript is a potential flusher, so a session with no
    ``record_ts`` in state has provably never been observed with content —
    which is what lets ``first_observation`` be derived from the pin map's
    absence without a separate marker. A quiet READER would break that
    (records arriving between its sighting and the first flush would be
    mis-classed as unknowable backlog — review finding); the only reader
    that declines to flush is a debounced ``afterFileEdit``, and a debounce
    implies a recent flush, hence an existing pin map. ``sessionStart`` is
    refused here too: ``main`` saves its clock first, without reading the
    transcript or touching ``record_ts`` (``_note_hook_clock``).
    """
    if event == "beforeShellExecution":
        cmd = payload.get("command")
        if not isinstance(cmd, str):
            return False
        return bool(_MILESTONE_RE.search(cmd[:_MILESTONE_SCAN_LIMIT]))
    if event == "afterShellExecution":
        return bool(pr_provenance.scan_shell_event(event, payload)[0])
    return event in ("afterFileEdit", "afterAgentResponse", "stop",
                     "beforeSubmitPrompt", "sessionEnd", "subagentStop")


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
    # ValueError covers an origin URL git prints in bytes that do not decode:
    # that is "no namespace", not a flush error counted toward dormancy.
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


async def _flush(uuid: str, source_path: Path, blob_ids: set[str],
                 flush_mode: str = "now", *, source_kind: str = "store",
                 source_revision: str | None = None,
                 records: list[dict], meta: dict,
                 applied_usage: set[str] | None = None,
                 agent_event: str | None = None,
                 deadline: float | None = None) -> bool | None:
    """Send the session; True once the server confirmed it. With
    ``agent_event`` one of the agent events, the session's sub-agents follow
    the confirmed save (``_send_agents``)."""
    # ``records``/``meta`` are REQUIRED: reading, stamping, and persisting
    # pins is main()'s job, in that order, under the session lock. A
    # self-read fallback here shipped records that bypassed that flow —
    # apply-only stamping would leave anything not yet pinned undated
    # (review finding) — so the convenience default was removed rather than
    # given a third stamping semantics.
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
    state = _read_state(uuid)
    pending_pr_urls, accepted_pr_urls, missing_pr_urls = (
        pr_provenance.queued_urls(state, records)
    )
    if missing_pr_urls:
        _log(f"{missing_pr_urls} direct gh pr create result(s) had no "
             "canonical GitHub PR URL")
    if pending_pr_urls or accepted_pr_urls:
        _save_state(
            uuid,
            pending_pr_urls=pending_pr_urls,
            accepted_pr_urls=accepted_pr_urls,
        )
    sendable = redact_records(elide_oversized_tool_results(records))
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
        _log(f"credential resolve failed ({e!r}) — skipping (run {skill_command('login')})")
        _save_state(uuid, last_error="no_credential", fail_streak=0)
        return
    if not bearer:
        _log(f"no usable credential — skipping (run {skill_command('login')})")
        # A local auth gap, not a server failure — never contacted it, so
        # clear any failure run rather than let a login blip tip a session
        # toward dormancy.
        _save_state(uuid, last_error="no_credential", fail_streak=0)
        return
    session = mcp_http.Session(url, bearer, timeout=FLUSH_TIMEOUT_S / 2)

    cwd = meta.get("cwd")
    # cwd is store content, so it is validated before it becomes a
    # `git -C` argument. Off the loop, like resolve_bearer above.
    if _cwd_ok(cwd):
        namespace = await asyncio.to_thread(_namespace_of, cwd)
    else:
        if cwd:
            _log(f"ignoring unusable cwd from Cursor source: {str(cwd)[:60]!r}")
        namespace = None

    # No `agent_brain_id` and no `org_id`, ever: a session is captured into
    # the author's personal memory, never into a brain — the repo's room
    # included. Naming a brain on any flush makes the server pin the whole
    # session to it and re-extract its earlier turns there.
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
    if namespace:
        # Same scope stamp flush_turn sends: directives extracted from this
        # session must recall in this repo's context, not everywhere.
        arguments["namespace"] = namespace
    provenance = pr_provenance.import_provenance(pending_pr_urls)
    if provenance:
        arguments["provenance"] = provenance
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        # Bound a semi-trusted session title (store content, like cwd): a
        # non-str from a corrupt meta is dropped rather than sent as-is, and a
        # runaway length is capped so it can't bloat every re-send.
        #
        # Redacted HERE because the title is derived from RAW records -- the
        # `redact_records` above covers only `sendable`, so a session whose
        # first prompt is `export MEMHUB_TOKEN=mhk_...` would otherwise ship
        # its key as the conversation's NAME, the most visible field there is.
        # Redact BEFORE the cap. Capping first can chop a straddling key
        # below `_SECRET`'s {16,} floor, and the surviving fragment then
        # matches nothing and ships in the clear. Verified both ways.
        arguments["title"] = redact_text(title.strip())[:200]

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
             f"dormant for this session; run {skill_command('import-session')} to "
             "capture it, or upgrade the server")
        _save_state(uuid, last_flush_at=time.time(), unsupported=True,
                    unsupported_at=time.time(), fail_streak=0)
        return
    if verdict != "ok":
        _note_failure(uuid, "compute_budget_exhausted"
                      if mcp_http.is_compute_budget_rejection(res)
                      else "unconfirmed_import")
        return

    # `shipped` was fixed at the end of the transcript read (see above), NOT
    # re-read here: a post-send read would span the whole network round trip.
    # The timestamps land either way — the debounce must still hold after a
    # successful send — but blob_ids only when we could actually verify it.
    ack = mcp_http.ack_of(res, f"cursor-{uuid}")
    reconciled = pr_provenance.acknowledge_confirmed_import(
        pending_pr_urls,
        accepted_pr_urls,
        ack,
    )
    pending_pr_urls, accepted_pr_urls = reconciled
    if pending_pr_urls:
        # The transcript committed, but its optional URL write did not. Keep
        # the source watermark pinned and retry through the existing bounded
        # hook failure/dormancy path.
        _save_state(uuid, pending_pr_urls=pending_pr_urls,
                    accepted_pr_urls=accepted_pr_urls)
        _note_failure(uuid, "unconfirmed_provenance")
        return
    fields = {"last_flush_at": time.time(), "last_ok_at": time.time(),
              "last_error": None,
              # The re-probe worked: this server confirms after all, so the
              # session re-arms instead of staying dormant on old evidence.
              "unsupported": False, "unsupported_at": 0,
              "pending_pr_urls": pending_pr_urls,
              "accepted_pr_urls": accepted_pr_urls,
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
    _log(f"flushed {len(sendable)} records → cursor-{uuid} (personal)")
    if agent_event not in _AGENT_EVENTS + ("afterAgentResponse",):
        return True
    deadline = deadline or time.monotonic() + FLUSH_TIMEOUT_S / 2
    if deadline - time.monotonic() < agent_capture.MIN_SLICE_BUDGET_S:
        # The parent used the budget: starting the agents now would race the
        # caller's own timeout, whose cancel counts against the PARENT.
        _log(f"{agent_event}: no time left for agents — the next agent event sends them")
        return True
    try:
        # Bounded inside the caller's own timeout, and caught: the parent
        # is confirmed, and nothing an agent does may count against it.
        await asyncio.wait_for(
            _send_agents(uuid, agent_event, session=session, url=url,
                         bearer=bearer, namespace=namespace, deadline=deadline),
            timeout=deadline - time.monotonic())
    except Exception as e:  # noqa: BLE001
        _log(f"{agent_event}: agent flush error: {e!r}")
    return True


# ── sub-agents ───────────────────────────────────────────────────────────────
# Each agent a Cursor session spawned is sent as its own conversation,
# ``cursor-<root>/agent-<child>``, linked to ``cursor-<root>``
# (MemHub-Backend agent-sessions spec §4.1, §10.2). Finding them is
# cursor_agents'; sending them is agent_capture's, which owns the sent cache,
# the deadline and the rule that agents go only after the parent's confirmed
# save. A child is never sent as a session of its own (``_child_of``).

def _agents_path(uuid: str) -> Path:
    return STATE_DIR / f"{_safe_uuid(uuid)}.agents"


def _discover_agents(uuid: str, event: str, state: dict) -> list:
    """Every sub-agent of ``uuid`` on either surface. A CLI root is a store
    with meta.json and finds its children among its sibling stores; an IDE
    root is a transcript whose directory holds ``subagents/``."""
    root_ended = event == "sessionEnd"
    agents: list = []
    try:
        agents += cursor_agents.cli_agents(
            uuid, root_source=state.get("source_kind"), root_ended=root_ended, log=_log)
    except Exception as e:  # noqa: BLE001 — agents never cost the parent
        _log(f"agents: CLI discovery failed ({e!r})")
    transcript, _err = _valid_transcript_path(state.get("transcript_path"), uuid)
    if transcript is not None:
        roster = state.get("subagents")
        meta = state.get("cursor_meta") if isinstance(state.get("cursor_meta"), dict) else {}
        try:
            agents += cursor_agents.ide_agents(
                uuid, transcript, roster if isinstance(roster, dict) else {},
                cwd=meta.get("cwd"), root_ended=root_ended, log=_log)
        except Exception as e:  # noqa: BLE001
            _log(f"agents: IDE discovery failed ({e!r})")
    seen: set = set()
    return [a for a in agents if not (a.agent_id in seen or seen.add(a.agent_id))]


async def _send_agents(uuid: str, event: str, *, session, url: str, bearer: str,
                       namespace: str | None, deadline: float):
    """Send the sub-agents of ``uuid`` not yet sent as they are now. Agent
    failures are logged and retried by the next agent event; they never touch
    the session's own ``fail_streak`` or dormancy."""
    state = _read_state(uuid)
    agents = await asyncio.to_thread(_discover_agents, uuid, event, state)
    if not agents:
        return None
    cache = _agents_path(uuid)
    planned = agent_capture.plan(cache, agents)
    if not planned.todo:
        return None
    caps = await asyncio.to_thread(
        server_caps.get, STATE_DIR, url, lambda: mcp_http.list_tools(url, bearer))
    if caps is None:
        _log(f"agents not sent: the server's tools could not be listed "
             f"({len(planned.todo)} waiting)")
        return None

    async def send_one(agent, arguments: dict) -> bool:
        sendable = redact_records(elide_oversized_tool_results(agent.load()))
        if not sendable:
            return True
        call = {**arguments, "messages": sendable,
                "source_platform": cursor_reader.HOST, "flush": "now"}
        if namespace:
            call["namespace"] = namespace
        try:
            res = await session.call_tool(
                "import_conversation", arguments=call,
                timeout=max(5.0, deadline - time.monotonic()))
        except mcp_http.McpError as e:
            _log(f"agent {agent.agent_id[:8]}: import failed: {e}")
            return False
        if _verdict(res, arguments["conversation_id"]) == "ok":
            return True
        if getattr(res, "isError", False) and server_caps.names_agent_argument(
                " ".join(mcp_http.texts_of(res))):
            server_caps.invalidate(STATE_DIR, url)
        return False

    report = await agent_capture.send_agents(
        caps, parent_conv_id=f"cursor-{uuid}", agents=planned.todo,
        cache_path=cache, deadline=deadline, send_one=send_one, log=_log,
        sent=planned.sent)
    if report.failed:
        _log(f"agent {report.failed[0][:8]} not confirmed; {report.deferred} "
             "agent(s) left for the next agent event")
    return report


async def _agents_only(uuid: str, event: str, state: dict, deadline: float) -> None:
    """The agent lane without a parent send: the parent had nothing new, but
    an earlier flush of it was confirmed, so its agents can link to it."""
    try:
        url, bearer = await asyncio.to_thread(resolve_bearer)
    except Exception:  # noqa: BLE001 — local auth gap: nothing to send with
        return
    if not bearer:
        return
    session = mcp_http.Session(url, bearer, timeout=FLUSH_TIMEOUT_S / 2)
    meta = state.get("cursor_meta") if isinstance(state.get("cursor_meta"), dict) else {}
    cwd = meta.get("cwd")
    if not cwd and state.get("source_kind") == "store":
        located, _err = cursor_reader.locate(uuid)
        cwd = cursor_reader.session_cwd(located) if located is not None else None
    namespace = await asyncio.to_thread(_namespace_of, cwd) if _cwd_ok(cwd) else None
    await _send_agents(uuid, event, session=session, url=url, bearer=bearer,
                       namespace=namespace, deadline=deadline)


def _run_agents_only(uuid: str, event: str) -> None:
    """``_agents_only`` for a session whose parent was confirmed before and is
    not dormant; the caller holds its flush lock."""
    state = _read_state(uuid)
    if not state.get("last_ok_at") or state.get("unsupported"):
        return
    _save_state(uuid, agents_lane_at=time.time())
    deadline = time.monotonic() + FLUSH_TIMEOUT_S - 5
    try:
        asyncio.run(asyncio.wait_for(_agents_only(uuid, event, state, deadline),
                                     timeout=FLUSH_TIMEOUT_S))
    except Exception as e:  # noqa: BLE001 — never the parent's failure
        _log(f"{event}: agent flush error: {e!r}")


def _child_lane_due(root: str, event: str) -> bool:
    """Whether a CHILD's hook should run its root's agent lane. A child's
    own turn/stop events always do; its tool hooks (a cursor-agent child
    fires afterFileEdit under its own id on every edit) at most once per
    ``DEBOUNCE_S``, so an edit-heavy child is not re-read and re-sent whole
    on every edit."""
    if event in _AGENT_EVENTS:
        return True
    last = _read_state(root).get("agents_lane_at")
    return not isinstance(last, (int, float)) or time.time() - last > DEBOUNCE_S


def _child_of(uuid: str, payload: dict) -> tuple[bool, str | None]:
    """(is ``uuid`` a sub-agent, its root or None). Decided BEFORE any source
    is chosen, so a child is never flushed as ``cursor-<child>`` whichever of
    its store or transcript happens to be readable: an IDE child's hooks carry
    ``parent_tool_call_id``; a CLI child is a store with no meta.json."""
    is_child, root = cursor_agents.ide_child_root(uuid, payload)
    if is_child:
        return True, root
    return cursor_agents.cli_child_root(uuid)


def _repo_enabled(payload: dict, uuid: str) -> bool:
    """The repository allowlist (``repo_allow``, ``/memhub:repos``) for this
    session's workspace — the same rule the Claude Code hooks apply. A
    restricted machine captures a Cursor session only when its workspace is
    a listed checkout; one with no workspace in view is not captured. Any
    failure here captures, as the Claude Code hooks do."""
    try:
        import repo_allow  # noqa: PLC0415 — beside this file
        if not repo_allow.restricted():
            return True
        roots = payload.get("workspace_roots")
        cwd = next((r for r in roots if isinstance(r, str) and r), None) \
            if isinstance(roots, list) else None
        if not cwd:
            meta = _read_state(uuid).get("cursor_meta")
            cwd = meta.get("cwd") if isinstance(meta, dict) else None
        return bool(cwd) and repo_allow.session_enabled(cwd)
    except Exception:  # noqa: BLE001
        return True


_OBSERVATION_FIELDS = (
    "source_kind", "transcript_path", "cursor_meta", "usage_events",
    "record_ts", "observed_revision",
)


def _observe_session(uuid, event, payload, event_pr_urls, *, observe_usage=True,
                     hook_at: str | None = None):
    """Save native observations before waiting for an upload.

    Only observation readers/writers hold this lock. It is released before
    acquiring the upload lock; a network request never holds it. Preparing
    once also gives the common, uncontended path its upload snapshot.
    """
    fd = _acquire(uuid, blocking=True, observations=True)
    if fd is None:
        return None
    try:
        state = _read_state(uuid)
        pending_pr_urls = pr_provenance.merge_urls(
            state.get("pending_pr_urls") or [], event_pr_urls)
        source_kind, source_path, err = _source_for(uuid, payload, state)
        if source_kind is None or source_path is None:
            _log(f"{event}: {err or 'no readable Cursor source'}")
            return None

        cursor_meta = _payload_meta(payload if observe_usage else {}, state)
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
                    return None
                records, meta = cursor_reader.to_canonical(source_path)
            else:
                records, meta = cursor_reader.to_canonical(
                    source_path, session_id=uuid,
                    cwd=cursor_meta.get("cwd"), model=cursor_meta.get("model"))
        except Exception as e:  # locked/partial/corrupt source — next hook retries
            _log(f"{event}: {source_kind} source unreadable ({e}) — skipping")
            return None

        # Persist source identity and exact usage BEFORE any network work —
        # on EVERY event, flushing or not, so a dormant boundary's usage
        # sample survives to the eventual send. afterAgentResponse and stop
        # duplicate the same generation; replacing that dictionary key makes
        # delivery idempotent, while a later hook can retry an auth/server
        # failure without needing Cursor to repeat usage. Timestamp pins are
        # different: a quiet declined edit does not rewrite the large pin
        # map. An exact usage boundary dates its record even if delivery is
        # dormant; other eligible events retain the normal flush gate.
        # The only event that reads
        # yet declines is a DEBOUNCED afterFileEdit (guaranteed non-senders
        # exit in main() before reading — see _event_can_flush), and a
        # debounce implies a flush ≤DEBOUNCE_S ago, so what it saw is dated
        # by the next flushing hook at most that interval later.
        fields: dict = {"source_kind": source_kind, "cursor_meta": cursor_meta}
        if source_kind == "transcript":
            fields["transcript_path"] = str(source_path)
        usage_events = state.get("usage_events")
        usage_events = dict(usage_events) if isinstance(usage_events, dict) else {}
        boundary_uuids: set[str] = set()
        sample = _hook_usage(event, payload) if observe_usage else None
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
                boundary_uuids = {usage_events[generation]["target_uuid"]}
        elif (observe_usage and event in ("afterAgentResponse", "stop") and
              any(key in payload for key in _HOOK_USAGE_KEYS)):
            _log(f"{event}: malformed token counters or generation_id — "
                 "leaving this turn unmeasured")

        if source_kind == "transcript":
            # Content identity only — computed BEFORE _stamp_records and
            # _apply_usage mutate the records. The send gate answers "is
            # there new CONTENT?"; it must never re-fire because a timestamp
            # pin was minted or upgraded (stamps ride along on whatever send
            # happens, and pending usage forces its own send via
            # usage_pending). Hashing post-stamp would couple the gate's
            # stability to the pin map's — the fragility a review round
            # already caught once on the pin-eviction path.
            source_revision = _records_revision(records)

        fields["observed_revision"] = source_revision or hashlib.sha256(
            "\n".join(sorted(blob_ids)).encode()).hexdigest()
        applied_usage = _apply_usage(records, usage_events)
        sent_usage = set(state.get("sent_usage_generations") or [])
        usage_pending = bool(applied_usage - sent_usage)

        # should_flush reads only watermark/backoff state (transcript_revision,
        # blob_ids, last_flush_at, dormancy) — nothing ``fields`` writes — so
        # the decision comes FIRST and a quiet event saves only the small
        # fields, leaving the pin map untouched on disk. A duplicate
        # afterShellExecution can also reach this point: pending URL telemetry
        # retries through the same bounded send path until acknowledged.
        if sample is None and not should_flush(
                event, payload, state, blob_ids, time.time(),
                source_kind=source_kind, source_revision=source_revision,
                usage_pending=usage_pending,
                provenance_pending=bool(pending_pr_urls)):
            _save_state(uuid, **fields)
            return None

        clocks, consumed = _exact_clocks(records, state, event, hook_at)
        # An invocation's records all exist once its sessionEnd fires, and
        # sessionEnd observes before the upload lock, so they are dated by
        # that clock too — not by a later clock that would put them after the
        # final record it dates.
        fields["record_ts"] = _stamp_records(
            records, state.get("record_ts"),
            hook_at if event == "sessionEnd" and hook_at else _now_iso(),
            first_observation="record_ts" not in state,
            boundary_uuids=boundary_uuids, clocks=clocks)

        def persist(saved: dict) -> None:
            # A queued prompt clock is spent with the pin it became, in one
            # write, so a concurrent beforeSubmitPrompt's entry is never lost.
            saved.update(fields)
            if consumed:
                saved["prompt_clocks"] = [
                    entry for entry in saved.get("prompt_clocks") or []
                    if entry not in consumed]
        _update_state(uuid, persist)
        observed = {**state, **fields}
        return {
            "source_kind": source_kind, "source_path": source_path,
            "blob_ids": blob_ids, "source_revision": source_revision,
            "records": records, "meta": meta, "applied_usage": applied_usage,
            "observation": {key: observed.get(key) for key in _OBSERVATION_FIELDS},
        }
    finally:
        os.close(fd)


def _note_hook_clock(uuid: str, event: str, payload: dict, at: str) -> None:
    """Save a hook's own clock for the records it announces, before anything
    can wait or fail. Writes no ``record_ts``: whether the session was ever
    observed stays derived from the pin map alone (see _event_can_flush)."""
    if event == "sessionStart":
        # Keep the first: only the session's real start dates its banner.
        _update_state(uuid, lambda state: state.setdefault("session_start_at", at))
    elif event == "beforeSubmitPrompt":
        entry = {"at": at, "digest": _prompt_digest(payload.get("prompt"))}

        def queue(state: dict) -> None:
            queued = state.get("prompt_clocks")
            queued = queued if isinstance(queued, list) else []
            state["prompt_clocks"] = (queued + [entry])[-_MAX_PROMPT_CLOCKS:]
        _update_state(uuid, queue)


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    # This hook's own clock, read before any lock wait: no Cursor payload
    # carries a timestamp, and a boundary that waits on a busy upload must
    # still date its records at the moment it fired.
    hook_at = _now_iso()
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
    if not _repo_enabled(payload, uuid):
        _log(f"{event}: workspace is not on this machine's repository "
             "allowlist (/memhub:repos) — not captured")
        return 0
    event_pr_urls, missing_pr_urls = pr_provenance.scan_shell_event(
        event, payload)
    if missing_pr_urls:
        _log(f"{event}: direct gh pr create result had no canonical "
             "GitHub PR URL")
    if event in ("sessionStart", "beforeSubmitPrompt"):
        try:
            _note_hook_clock(uuid, event, payload, hook_at)
        except OSError as e:
            _log(f"{event}: could not save the hook clock ({e!r})")
    if not _event_can_flush(event, payload):
        # A guaranteed non-send (plain shell traffic, unknown event) exits
        # before the lock and the transcript read — no I/O per `ls`, and no
        # quiet OBSERVATION that would skew first_observation (see
        # _event_can_flush).
        return 0
    if event == "subagentStop":
        # Fires under the PARENT; remember what it says of the child before
        # anything can fail, so a later agent event still has it.
        entry = cursor_agents.roster_entry(payload)
        if entry is not None:
            child, fields = entry

            def merge(state: dict) -> None:
                # Under the state lock: two children ending together each
                # add their own entry rather than the later one winning.
                roster = state.get("subagents")
                roster = dict(roster) if isinstance(roster, dict) else {}
                roster[child] = {**(roster.get(child) or {}), **fields}
                state["subagents"] = roster
            _update_state(uuid, merge)
    is_child, root = _child_of(uuid, payload)
    if is_child:
        # Never the child's own session, whichever of its sources is readable.
        _log(f"{event}: {uuid[:8]} is a sub-agent"
             f"{' of ' + root[:8] if root else ''} — captured with its root "
             "session, never as its own")
        if root and _UUID_RE.fullmatch(root) and _child_lane_due(root, event):
            root_lock = _acquire(root, blocking=False)
            if root_lock is not None:
                try:
                    _run_agents_only(root, event)
                finally:
                    os.close(root_lock)
        return 0

    # Only exact hook-only usage and sessionEnd need observation before the
    # upload lock: sessionEnd's clock dates what exists when it fires, and an
    # in-flight send can hold the upload lock while a following --resume
    # invocation writes its turn. Ordinary edits/milestone shells keep the
    # cheap busy-session skip: they neither parse the source nor queue behind
    # the observation lock. Shell results still queue their PR provenance
    # before reading the source.
    prepared = None
    if event == "sessionEnd" or _hook_usage(event, payload) is not None:
        prepared = _observe_session(uuid, event, payload, event_pr_urls,
                                    hook_at=hook_at)
        if prepared is None:
            return 0

    # Serialize the whole check-then-act: reading the watermark, sending, and
    # writing it back must not interleave with another upload. Exact usage
    # already persisted under its own lock and does not wait for this owner.
    # Turn boundaries (stop / beforeSubmitPrompt) are last-chance events — a
    # session may have no later flush — so they WAIT for a concurrent flush
    # rather than skip and never run again. Mid-turn events (edits, milestone
    # shell) skip when busy: another will follow. Once acquired, should_flush
    # re-checks new-blobs, so a waited boundary ships only the delta or no-ops.
    lock_fd = _acquire(
        uuid, blocking=(event in ("afterAgentResponse", "stop",
                                  "beforeSubmitPrompt", "sessionEnd",
                                  "afterShellExecution", "subagentStop")))
    if lock_fd is None:
        _log(f"{event}: another flush is running for this session — skipping")
        return 0
    try:
        outcome = _flush_locked(uuid, event, payload, event_pr_urls, prepared,
                                hook_at=hook_at)
        if outcome == "skipped" and event in _AGENT_EVENTS:
            # Nothing new in the session itself, but a sub-agent may have
            # finished since: the agents of a session saved before still go.
            _run_agents_only(uuid, event)
    finally:
        os.close(lock_fd)  # releases the flock
    return 0


def _flush_locked(uuid: str, event: str, payload: dict, event_pr_urls,
                  prepared, *, hook_at: str | None = None) -> str:
    """The session's own flush, under its flush lock: "flushed" (confirmed),
    "failed" (tried and not confirmed) or "skipped" (nothing to send)."""
    state = _read_state(uuid)
    accepted_pr_urls = pr_provenance.merge_urls(
        state.get("accepted_pr_urls") or [])
    accepted_pr_set = set(accepted_pr_urls)
    prior_pending = pr_provenance.merge_urls(
        state.get("pending_pr_urls") or [])
    pending_pr_urls = [
        url for url in pr_provenance.merge_urls(
            prior_pending, event_pr_urls)
        if url not in accepted_pr_set
    ]
    if pending_pr_urls != prior_pending:
        # Cursor's transcript omits shell results. Persist the hook-only
        # evidence before source reads and network work so any later event
        # can retry it after a crash, missing source, or failed send.
        _save_state(
            uuid,
            pending_pr_urls=pending_pr_urls,
            accepted_pr_urls=accepted_pr_urls,
        )
        state = {
            **state,
            "pending_pr_urls": pending_pr_urls,
            "accepted_pr_urls": accepted_pr_urls,
        }
    if prepared is None:
        prepared = _observe_session(uuid, event, payload, event_pr_urls,
                                    hook_at=hook_at)
        if prepared is None:
            return "skipped"
        state = _read_state(uuid)
    # Another hook may have observed/sent a newer source while this one
    # waited for the upload lock. Refresh only then, without rebinding
    # this already-observed generation to the newer last assistant. Nor
    # its hook clock: records that appeared during the wait are dated
    # now, not at a moment before they existed.
    if any(state.get(key) != value
           for key, value in prepared["observation"].items()):
        prepared = _observe_session(
            uuid, event, payload, event_pr_urls, observe_usage=False)
        if prepared is None:
            return "skipped"
        state = _read_state(uuid)
    source_kind = prepared["source_kind"]
    source_path = prepared["source_path"]
    blob_ids = prepared["blob_ids"]
    source_revision = prepared["source_revision"]
    records = prepared["records"]
    meta = prepared["meta"]
    applied_usage = prepared["applied_usage"]
    pending_usage = applied_usage - set(state.get("sent_usage_generations") or [])
    if not should_flush(
            event, payload, state, blob_ids, time.time(),
            source_kind=source_kind, source_revision=source_revision,
            usage_pending=bool(pending_usage),
            provenance_pending=bool(state.get("pending_pr_urls"))):
        return "skipped"
    try:
        mode = _FLUSH_MODE.get(event, "now")
        confirmed = asyncio.run(asyncio.wait_for(
            _flush(
                uuid, source_path, blob_ids, mode,
                source_kind=source_kind, source_revision=source_revision,
                records=records, meta=meta, applied_usage=applied_usage,
                agent_event=event,
                deadline=time.monotonic() + FLUSH_TIMEOUT_S - 5),
            timeout=FLUSH_TIMEOUT_S))
        return "flushed" if confirmed else "failed"
    except Exception as e:
        # A timeout or any raise past _flush's own handlers (the broad
        # case, including asyncio.wait_for firing) counts toward dormancy
        # too — otherwise a hard-down backend re-parses and re-uploads the
        # whole store on every event forever, and Stop is cooldown-exempt
        # so nothing else bounds it.
        _log(f"{event}: flush error: {e}")
        _note_failure(uuid, f"flush_error: {type(e).__name__}")
        return "failed"


if __name__ == "__main__":
    raise SystemExit(main())
