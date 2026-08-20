#!/usr/bin/env python3
"""Cursor capture: hook-triggered flush of a cursor-agent session into MemHub.

The Cursor analog of ``flush_turn.py``, reusing its whole downstream —
``redact``, ``_memhub_auth.resolve_bearer``, ``brain_resolve``, ``room_map``,
``mcp_http`` — and differing in exactly the two places Cursor differs:

1. **Data source**: Cursor hooks hand a ``transcript_path`` JSONL, but that
   file is deliberately Claude-shaped-yet-LOSSY (no tool results, no ids, no
   reasoning — verified live). So the hook is used only for TRIGGER +
   IDENTITY: the session uuid names the full-fidelity store
   (``~/.cursor/chats/<hash>/<uuid>/store.db``), read through
   ``readers.cursor`` into canonical records.
2. **Watermark**: the store is content-addressed, so "anything new?" is a
   set comparison on blob ids — which also makes checkpoint restores safe
   (a new root over mostly-old blobs), where a byte offset would lie.

The full canonical transcript is re-sent each flush under the constant
``conversation_id cursor-<uuid>``; the SERVER's watermark folds re-sends
forward (the same property Codex re-imports rely on). The local blob-id set
only decides WHETHER to flush, so a lost state file costs a redundant send,
never a lost or duplicated conversation.

Events (from Spike C, cursor-agent 2026.08): ``beforeShellExecution`` and
``afterFileEdit`` are the ones that fire in ``-p`` mode; ``stop`` and
``beforeSubmitPrompt`` are wired opportunistically for hosts where they do.
``beforeShellExecution`` flushes only on milestone commands (git commit /
gh pr …) so ordinary shell traffic stays quiet; ``afterFileEdit`` is
debounced. Whatever the trigger set misses, the next flush or an
import-session sweep heals — idempotence carries correctness, hooks carry
latency.

Invoked by ``hooks/cursor_capture.sh``, which answers the hook's permission
contract INSTANTLY and re-runs this script detached — a slow server must
never hold up the user's shell command.

Runs under bare python3 (stdlib + sibling modules only — no mcp SDK), same
discipline as flush_turn.
"""
from __future__ import annotations

import asyncio
import json
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
from room_map import env_for_url  # noqa: E402

STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "cursorflush"

# afterFileEdit fires on every edit; flushing each one would hammer the
# server mid-turn. Milestones and turn boundaries bypass this.
DEBOUNCE_S = 120.0
FLUSH_TIMEOUT_S = 240.0

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

# Server-side extraction mode per event — mirrors the Claude design, where
# flush_turn buffers ("auto") and flush_session drains ("now" — the server
# default) at session end and commit/PR boundaries. Cursor has NO session-end
# hook, so if every event sent "auto" a small session would NEVER cross the
# drain threshold: records buffer forever, ack_through stays null, and the
# session never materializes in the Sessions view. Verified live. Boundaries
# must drain; only mid-turn edits buffer.
_FLUSH_MODE = {
    "beforeShellExecution": "now",   # only fires on milestone commands (gate)
    "stop": "now",                   # turn boundary
    "beforeSubmitPrompt": "now",     # previous turn is definitely complete
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
    it is not: cursor_capture.sh runs every flush DETACHED, so an
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


def session_uuid(payload: dict) -> str | None:
    """The session identity a hook carries. ``session_id`` when present;
    else derived from ``transcript_path`` (…/agent-transcripts/<uuid>/…)."""
    sid = payload.get("session_id") or payload.get("conversation_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    tp = payload.get("transcript_path")
    if isinstance(tp, str) and tp:
        return Path(tp).parent.name or None
    return None


def current_blob_ids(store_db: Path) -> set[str]:
    import sqlite3
    con = sqlite3.connect(f"file:{store_db}?mode=ro", uri=True)
    try:
        return {row[0] for row in con.execute("SELECT id FROM blobs")}
    finally:
        con.close()


def should_flush(event: str, payload: dict, state: dict,
                 blob_ids: set[str], now: float) -> bool:
    """Pure gate — WHEN a hook invocation becomes a server call.

    New-blobs is a precondition for every event: without new content a flush
    is a guaranteed no-op round trip. On top of that, each event has its own
    threshold: milestones always ship (the commit/PR boundary is the whole
    point of milestone capture), turn boundaries ship, plain edits debounce,
    and non-milestone shell commands never trigger (too chatty).
    """
    if blob_ids <= set(state.get("blob_ids") or []):
        return False
    if event == "beforeShellExecution":
        return bool(_MILESTONE_RE.search(payload.get("command") or ""))
    if event == "afterFileEdit":
        return now - (state.get("last_flush_at") or 0) > DEBOUNCE_S
    if event in ("stop", "beforeSubmitPrompt"):
        return True
    return False


def _cwd_ok(cwd: str | None) -> bool:
    """``cwd`` is read out of the cursor STORE, so it is session content, not
    a trusted path. Anything handed to `git -C` must be an absolute existing
    directory that cannot be read as an option — git honors the local config
    of whatever repository it is pointed at."""
    try:
        return bool(cwd) and not cwd.startswith("-") and \
            Path(cwd).is_absolute() and Path(cwd).is_dir()
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
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2,
        )
        url = out.stdout.strip()
        if out.returncode == 0 and url:
            return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    return None


async def _flush(uuid: str, store_db: Path, blob_ids: set[str],
                 flush_mode: str = "now") -> None:
    records, meta = cursor_reader.to_canonical(store_db)
    # Close the read span IMMEDIATELY — before redaction and the network call,
    # which together can run for many seconds. Blobs present at both the gate
    # and here existed for the whole span the payload was built from, so their
    # content is in it; that pair is the honest watermark. Taken after the
    # send instead, this read would span the entire round trip, and a
    # checkpoint restore in that window could shrink the intersection to
    # nearly nothing and re-send the same content on every later hook.
    try:
        shipped = blob_ids & current_blob_ids(store_db)
    except Exception:
        # Unreadable mid-flush: we cannot say which blobs survived the read,
        # so the watermark is left ALONE rather than advanced to the gate set
        # — claiming the full set would mark blobs shipped that a checkpoint
        # restore may have removed before the payload was built. Cost is one
        # redundant re-send on the next event, the documented safe direction.
        shipped = None
    sendable = redact_records(records)
    if not sendable:
        return

    url, bearer = await asyncio.to_thread(resolve_bearer)
    if not bearer:
        _log("no usable credential — skipping (run /memhub:login)")
        _save_state(uuid, last_error="no_credential")
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
        room, namespace = await asyncio.gather(
            resolve_repo_brain(session, cwd, env),
            asyncio.to_thread(_namespace_of, cwd),
        )
    else:
        if cwd:
            _log(f"ignoring unusable cwd from store: {str(cwd)[:60]!r}")
        room, namespace = None, None

    arguments = {
        "messages": sendable,
        # Host-namespaced so server-side watermarks never collide across
        # hosts, matching the codex importer's convention.
        "conversation_id": f"cursor-{uuid}",
        # The agentic path detects by STRUCTURE; the records carry a Cursor
        # provenance banner (see readers/cursor.py).
        "source_platform": "claude",
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
    if meta.get("title"):
        arguments["title"] = meta["title"]

    try:
        await session.call_tool("import_conversation", arguments=arguments)
    except mcp_http.McpRateLimited as e:
        _log(f"rate limited: {e} — a later hook retries (state unmoved)")
        _save_state(uuid, last_error="rate_limited")
        return
    except mcp_http.McpError as e:
        _log(f"import failed: {e}")
        _save_state(uuid, last_error=str(e)[:200])
        return

    # `shipped` was fixed at the end of the transcript read (see above), NOT
    # re-read here: a post-send read would span the whole network round trip.
    # The timestamps land either way — the debounce must still hold after a
    # successful send — but blob_ids only when we could actually verify it.
    fields = {"last_flush_at": time.time(), "last_ok_at": time.time(),
              "last_error": None}
    if shipped is not None:
        fields["blob_ids"] = sorted(shipped)
    else:
        _log("store unreadable after the transcript read — watermark held, "
             "next event re-sends")
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
        _log(f"{event}: no session identity in payload — skipping")
        return 0

    store_db, err = cursor_reader.locate(uuid)
    if store_db is None:
        _log(f"{event}: {err}")
        return 0

    try:
        blob_ids = current_blob_ids(store_db)
    except Exception as e:  # locked/corrupt db mid-write — next hook retries
        _log(f"{event}: store unreadable ({e}) — skipping")
        return 0
    if not blob_ids:
        # NOT the same as "nothing new": the empty set is a subset of every
        # watermark, so falling through would read as "up to date" and skip
        # SILENTLY. A readable store with zero blobs is one mid-rebuild (a
        # checkpoint restore) — say so, and leave the watermark untouched so
        # the next event re-reads and ships whatever appears.
        _log(f"{event}: store reports zero blobs (rebuilding?) — skipping")
        return 0

    state = _read_state(uuid)
    if not should_flush(event, payload, state, blob_ids, time.time()):
        return 0

    try:
        mode = _FLUSH_MODE.get(event, "now")
        asyncio.run(asyncio.wait_for(_flush(uuid, store_db, blob_ids, mode),
                                     timeout=FLUSH_TIMEOUT_S))
    except Exception as e:
        _log(f"{event}: flush error: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
