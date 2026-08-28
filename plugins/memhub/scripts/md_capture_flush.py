#!/usr/bin/env python3
"""Stop hook: save each markdown file this turn wrote as a DRAFT artifact.

Pairs with ``md_capture.py`` (the PostToolUse collector). That script records
paths; this one, at turn end, reads each file's FINAL on-disk state, applies
the capture rule (``.md``, >= size floor or frontmatter opt-in, not a veto
location), and ships it to ``save_artifact`` — routed to the repo's room the
same way ``save_artifact.py`` routes, so an auto-captured spec lands where a
hand-saved one would.

Draft semantics: the server has no status column, so a capture is marked by
the ``auto-captured`` tag and a rationale naming the session. Re-saving the
same ``name`` versions it (server behaviour), so an agent or human publishing
the file later with ``save_artifact.py`` supersedes the draft in place rather
than sitting beside it — the failure the artifact-sync reminder exists for.
A file already linked in the repo's ``.claude/artifact-map.json`` (by its
``path``) is SKIPPED outright: that lineage is hand-saved via ``/memhub:spec``
under its own name, and a draft named from the H1 would open a second one.

Name = frontmatter ``title:`` > first ``# H1`` > filename stem. The agent keeps
titles stable across rewrites (the Artifact tool asks it to), so the name is
a usable version key. Type = frontmatter ``type:`` > ``spec`` when the name or
path says so > ``document``.

Runs via ``uv run --with 'mcp<2'`` (needs the SDK), fire-and-forget from the
Stop hook. NEVER FAILS LOUDLY: any error exits 0 quietly — memory capture must
not disturb the session. A path leaves the retry list only when it was saved
(content hash recorded), judged a non-candidate, or is unchanged since its
last save — so a server blip or the per-turn cap costs one turn, and a flaky
server never re-saves an unchanged file.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _memhub_auth import resolve_url_and_auth  # noqa: E402
from artifact_sync_reminder import MAP_RELPATH, link_for_path  # noqa: E402
from brain_resolve import resolve_repo_brain  # noqa: E402
from md_capture import MAX_BYTES, frontmatter, is_candidate, load_state, save_state  # noqa: E402
from redact import redact_text  # noqa: E402
from room_map import env_for_url, read_room, repo_root  # noqa: E402

TAG = "auto-captured"
MAX_PER_TURN = 5          # a turn that rewrote 40 .md files is a migration, not deliverables
TIMEOUT_S = 20.0
# Retries are bounded per path, not unbounded: a path that keeps failing —
# a persistent server rejection, a file that never decodes — leaves the list
# after this many failed passes with a log line, instead of re-reading and
# re-sending it on every Stop for the rest of the session. Reset on success.
MAX_ATTEMPTS = 3


def _log(msg: str) -> None:
    print(f"[memhub-md-capture] {msg}", file=sys.stderr)


def _fm_field(fm: str, key: str) -> str | None:
    m = re.search(rf"^{key}:\s*(.+?)\s*$", fm, re.M)
    return m.group(1).strip().strip("\"'") if m else None


def derive_name(path: Path, text: str, root: Path | None = None) -> str:
    """Artifact name = the version key. A frontmatter title is author-chosen
    and used verbatim. An H1 or filename is INFERRED and gets the repo-relative
    path appended: ``save_artifact`` versions by name within a brain, so two
    docs that both open with ``# Overview`` would otherwise chain into one
    lineage and the wrong one would become "latest"."""
    fm = frontmatter(text)
    t = _fm_field(fm, "title")
    if t:
        return t[:150]
    m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    base = m.group(1).strip() if m else path.stem.replace("_", " ").replace("-", " ")
    try:
        rel = str(path.relative_to(root)) if root else path.name
    except ValueError:
        rel = path.name
    return f"{base} ({rel})"[:150]


def derive_type(path: Path, text: str, name: str) -> str:
    fm = frontmatter(text)
    t = _fm_field(fm, "type") or _fm_field(fm, "artifact_type")
    if t:
        return t
    hay = f"{path.name} {name}".lower()
    if "spec" in hay:
        return "spec"
    if any(w in hay for w in ("design", "rfc", "adr", "proposal")):
        return "design_doc"
    if any(w in hay for w in ("runbook", "playbook", "howto", "how-to")):
        return "runbook"
    return "document"


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


class SaveRejected(RuntimeError):
    """The server answered, but not with a saved artifact."""


async def _save(session, call_args: dict) -> dict:
    """Call save_artifact and return its parsed payload, or raise.

    Only a confirmed success returns: an ``isError`` result, a non-JSON body,
    or a JSON body carrying an error field all raise ``SaveRejected`` so the
    caller leaves the path dirty. Treating any dict as success recorded the
    digest on an auth/quota rejection and silently lost the capture.
    """
    res = await session.call_tool("save_artifact", arguments=call_args)
    texts = [c.text for c in getattr(res, "content", []) if getattr(c, "type", "") == "text"]
    body = texts[0] if texts else ""
    if getattr(res, "isError", False):
        raise SaveRejected(f"tool error: {body[:160]}")
    try:
        out = json.loads(body) if body else {}
    except ValueError:
        raise SaveRejected(f"non-JSON reply: {body[:160]}")
    if not isinstance(out, dict):
        raise SaveRejected(f"unexpected reply shape: {type(out).__name__}")
    # The server's success payload is exactly {id, name, action, tags,
    # tag_report, scope}; failures raise (→ isError). An error field is
    # therefore never part of a success, and wins over any id beside it.
    if any(k in out for k in ("error", "detail", "_raw")):
        raise SaveRejected(f"rejected: {json.dumps(out)[:160]}")
    if not (out.get("artifact_id") or out.get("id")):
        raise SaveRejected(f"no artifact id in reply: {json.dumps(out)[:160]}")
    return out


async def flush(session_id: str) -> None:
    state = load_state(session_id)
    dirty = list(state.get("dirty") or [])
    if not dirty:
        return
    saved = dict(state.get("saved") or {})   # path -> content digest
    attempts = dict(state.get("attempts") or {})   # path -> consecutive failures
    attempts0 = dict(attempts)                       # to write back only what changed
    # `processed` is built from OUTCOMES, not from the input list: a path leaves
    # `dirty` only when it was saved, judged a non-candidate, or is unchanged
    # since its last save. Capped-out candidates and failed saves stay in
    # `dirty` so the next Stop retries them without needing another edit.
    processed: set[str] = set()
    todo: list[tuple[str, Path, str, str]] = []   # (raw key, path, text, digest)
    for raw in dirty:
        p = Path(raw)
        if not p.is_file():
            processed.add(raw)                   # deleted/moved: nothing to retry
            continue
        # The collector stored the CANONICAL path. If it no longer resolves to
        # itself, something on the way was swapped for a symlink since — the
        # veto was judged on the recorded path, so don't read through it.
        try:
            canonical = not p.is_symlink() and p.resolve() == p
        except (OSError, RuntimeError):     # symlink loop etc.: this item only
            canonical = False
        if not canonical:
            _log(f"skip {p.name}: no longer canonical")
            processed.add(raw)
            continue
        try:
            nbytes = p.stat().st_size
        except OSError:
            processed.add(raw)
            continue
        if nbytes > MAX_BYTES:
            # stat-gated BEFORE read_text: the cap must not cost a full read
            _log(f"skip {p.name}: above size cap ({nbytes} > {MAX_BYTES})")
            processed.add(raw)
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            # The Stop hook is async: this read can land mid-write. Retry on
            # the next Stop rather than drop — bounded by MAX_ATTEMPTS.
            _bump(attempts, raw, processed, f"unreadable ({type(e).__name__})")
            continue
        ok, why = is_candidate(p, size=nbytes, text=text)   # UTF-8: on-disk bytes == encoded length
        if not ok:
            _log(f"skip {p.name}: {why}")
            processed.add(raw)
            continue
        d = _digest(text)
        if saved.get(raw) == d:
            processed.add(raw)                   # unchanged since last successful save
            continue
        todo.append((raw, p, text, d))
    if len(todo) > MAX_PER_TURN:
        _log(f"{len(todo)} candidates > cap {MAX_PER_TURN}; saving the {MAX_PER_TURN} largest, "
             f"the rest retry next Stop")
        todo = sorted(todo, key=lambda t: -len(t[2]))[:MAX_PER_TURN]
    if not todo:
        _persist(session_id, processed, saved, _changed(attempts0, attempts))
        return

    pending = {raw for raw, _, _, _ in todo}   # not yet attempted this pass
    try:
        # Lazy SDK imports INSIDE the guard: if they fail, `finally` still
        # persists the non-candidate / unchanged decisions made above.
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        url, headers, auth = resolve_url_and_auth(None, interactive=False)
        env = env_for_url(url)
        async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                for raw, p, text, d in todo:
                    pending.discard(raw)
                    # The whole per-item body is guarded, not just the save:
                    # a malformed room file or odd content must skip ONE item
                    # (which stays dirty), never the rest of the turn.
                    try:
                        root = repo_root(p.parent)
                        if root is not None and _hand_saved(root, p):
                            _log(f"skip {p.name}: linked in {MAP_RELPATH} — its lineage is hand-saved")
                            processed.add(raw)
                            continue
                        name = derive_name(p, text, root)
                        body = redact_text(text)
                        call_args: dict = {
                            "name": name,
                            "content": body,
                            "artifact_type": derive_type(p, text, name),
                            "tags": [TAG],
                            "rationale": f"auto-captured from session {session_id[:8]} ({p.name}); "
                                         f"re-save with save_artifact.py to publish",
                        }
                        room = None
                        if root is not None:
                            # Cache first; on a miss, resolve from the server
                            # over the session already open (same as the
                            # transcript capture hooks) so an auto-captured
                            # spec lands in the repo room when one exists.
                            room = read_room(p.parent, env) or \
                                await resolve_repo_brain(s, p.parent, env)
                        if room:
                            call_args["agent_brain_id"] = room["brain_id"]
                        out = await asyncio.wait_for(_save(s, call_args), timeout=TIMEOUT_S)
                    except Exception as e:  # noqa: BLE001 — stays in dirty, retried next Stop
                        _bump(attempts, raw, processed, f"{type(e).__name__}: {str(e)[:120]}")
                        continue
                    # Keyed on `raw` — the exact string in `dirty` — so the
                    # dedup lookup and the `_persist` removal both match it.
                    saved[raw] = d
                    processed.add(raw)
                    attempts.pop(raw, None)
                    _log(f"saved '{name}' ({len(body):,} chars) → "
                         f"{room['name'] if room else 'personal memory'} "
                         f"id={out.get('artifact_id') or out.get('id')}")
    except Exception as e:  # noqa: BLE001 — connection-level: SDK import, auth, initialize
        # Nothing per-item ran, so nothing was bumped. Count this pass against
        # every candidate that never got its turn, or an unreachable server
        # would have us re-read and re-encode all of them on every Stop with
        # no MAX_ATTEMPTS ceiling.
        # Type only: an auth / transport exception can carry the URL or a
        # header in its message, and this line goes to stderr.
        for raw in pending:
            _bump(attempts, raw, processed, f"connection: {type(e).__name__}")
    finally:
        # Persist whatever was decided even if the connection itself failed:
        # non-candidates drop out, successes record their digest, everything
        # else remains dirty for the next Stop.
        _persist(session_id, processed, saved, _changed(attempts0, attempts))


def _hand_saved(root: Path, p: Path) -> bool:
    """True when the repo's artifact map links this file by ``path`` — a
    lineage ``/memhub:spec`` owns under its own name. Never raises."""
    try:
        return link_for_path(root, p.relative_to(root).as_posix()) is not None
    except Exception:  # noqa: BLE001 — a lookup must not cost a capture
        return False


def _bump(attempts: dict, raw: str, processed: set, why: str) -> None:
    """Count a failed pass for `raw`; give up (mark processed) at the cap."""
    n = attempts.get(raw, 0) + 1
    attempts[raw] = n
    name = Path(raw).name
    if n >= MAX_ATTEMPTS:
        _log(f"giving up on {name} after {n} failed passes: {why}")
        processed.add(raw)
        attempts.pop(raw, None)
    else:
        _log(f"will retry {name} next Stop ({n}/{MAX_ATTEMPTS}): {why}")


def _changed(before: dict, after: dict) -> dict:
    """The attempt counters this pass bumped (a key it cleared is in `processed`)."""
    return {k: n for k, n in after.items() if before.get(k) != n}


def _persist(session_id: str, processed: set, saved: dict, attempts: dict | None = None) -> None:
    """Write back by MERGING into a fresh read, never from the snapshot taken
    before the network window. The Stop hook is async, so the collector keeps
    appending to the same file while a save is in flight; persisting the
    stale snapshot would drop those paths (flush_turn solves the same race
    with flock — this is the lock-free equivalent for a list-and-dict)."""
    fresh = load_state(session_id)
    fresh["dirty"] = [d for d in fresh.get("dirty") or [] if d not in processed]
    fresh.setdefault("saved", {}).update(saved)
    # `attempts` follows the same merge discipline: only the keys THIS pass
    # decided are written. Every processed path is terminal, so its counter
    # goes (a non-candidate/unchanged/deleted outcome after an earlier
    # failure would otherwise leave a dead entry forever); a still-dirty path
    # carries this pass's count. Keys this pass never touched keep whatever
    # an overlapping flush wrote.
    fa = fresh.setdefault("attempts", {})
    for k in processed:
        fa.pop(k, None)
    for k, n in (attempts or {}).items():
        if k not in processed:
            fa[k] = n
    save_state(session_id, fresh)


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        session_id = (hook_input.get("session_id") or "").strip()
        if not session_id:
            return 0
        asyncio.run(asyncio.wait_for(flush(session_id), timeout=TIMEOUT_S * (MAX_PER_TURN + 1)))
    except Exception as e:  # noqa: BLE001 — never disturb the session
        try:
            _log(f"flush aborted: {type(e).__name__}: {str(e)[:120]}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
