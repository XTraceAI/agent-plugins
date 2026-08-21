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

Name = frontmatter ``title:`` > first ``# H1`` > filename stem. The agent keeps
titles stable across rewrites (the Artifact tool asks it to), so the name is
a usable version key. Type = frontmatter ``type:`` > ``spec`` when the name or
path says so > ``document``.

Runs via ``uv run --with 'mcp<2'`` (needs the SDK), fire-and-forget from the
Stop hook. NEVER FAILS LOUDLY: any error exits 0 quietly — memory capture must
not disturb the session. Each path is retried on the next Stop only if the
file changed (content hash in state), so a server blip costs one turn, and a
flaky server never re-saves an unchanged file.
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
from md_capture import frontmatter, is_candidate, load_state, save_state  # noqa: E402
from redact import redact_text  # noqa: E402
from room_map import env_for_url, read_room, repo_root  # noqa: E402

TAG = "auto-captured"
MAX_PER_TURN = 5          # a turn that rewrote 40 .md files is a migration, not deliverables
TIMEOUT_S = 20.0


def _log(msg: str) -> None:
    print(f"[memhub-md-capture] {msg}", file=sys.stderr)


def _fm_field(fm: str, key: str) -> str | None:
    m = re.search(rf"^{key}:\s*(.+?)\s*$", fm, re.M)
    return m.group(1).strip().strip("\"'") if m else None


def derive_name(path: Path, text: str) -> str:
    fm = frontmatter(text)
    t = _fm_field(fm, "title")
    if t:
        return t[:150]
    m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    if m:
        return m.group(1).strip()[:150]
    return path.stem.replace("_", " ").replace("-", " ")[:150]


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


async def _save(session, call_args: dict) -> dict:
    from mcp.client.session import ClientSession  # noqa: F401  (type only)
    res = await session.call_tool("save_artifact", arguments=call_args)
    texts = [c.text for c in getattr(res, "content", []) if getattr(c, "type", "") == "text"]
    try:
        return json.loads(texts[0]) if texts else {}
    except ValueError:
        return {"_raw": texts[0][:200] if texts else ""}


async def flush(session_id: str) -> None:
    state = load_state(session_id)
    dirty = list(state.get("dirty") or [])
    if not dirty:
        return
    saved = state.setdefault("saved", {})   # path -> content digest
    todo: list[tuple[Path, str, str]] = []
    for raw in dirty:
        p = Path(raw)
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        ok, why = is_candidate(p, size=len(text.encode("utf-8")), text=text)
        if not ok:
            _log(f"skip {p.name}: {why}")
            continue
        d = _digest(text)
        if saved.get(raw) == d:
            continue                         # unchanged since last successful save
        todo.append((p, text, d))
    # Whatever happens below, this turn's dirty list is consumed; unsaved
    # entries come back only if the file is edited again (collector re-adds).
    state["dirty"] = []
    if len(todo) > MAX_PER_TURN:
        _log(f"{len(todo)} candidates > cap {MAX_PER_TURN}; saving the {MAX_PER_TURN} largest")
        todo = sorted(todo, key=lambda t: -len(t[1]))[:MAX_PER_TURN]
    if not todo:
        save_state(session_id, state)
        return

    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    url, headers, auth = resolve_url_and_auth(None, interactive=False)
    env = env_for_url(url)
    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for p, text, d in todo:
                name = derive_name(p, text)
                body = redact_text(text)
                call_args: dict = {
                    "name": name,
                    "content": body,
                    "artifact_type": derive_type(p, text, name),
                    "tags": [TAG],
                    "rationale": f"auto-captured from session {session_id[:8]} ({p.name}); "
                                 f"re-save with save_artifact.py to publish",
                }
                room = read_room(p.parent, env) if repo_root(p.parent) is not None else None
                if room:
                    call_args["agent_brain_id"] = room["brain_id"]
                try:
                    out = await asyncio.wait_for(_save(s, call_args), timeout=TIMEOUT_S)
                    saved[str(p)] = d
                    _log(f"saved '{name}' ({len(body):,} chars) → "
                         f"{room['name'] if room else 'personal memory'}"
                         + (f" id={out.get('artifact_id') or out.get('id')}" if isinstance(out, dict) else ""))
                except Exception as e:  # noqa: BLE001
                    _log(f"save failed for {p.name}: {type(e).__name__}: {str(e)[:120]}")
    save_state(session_id, state)


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
