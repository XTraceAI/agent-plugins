"""Per-session list of memory ids already shown to the agent.

One list per Claude Code session, shared by every hook that injects memory:
``directive_recall`` (its ``already_fired``), the session-start brief and the
prompt hook. An id that any of them rendered is not rendered again by any of
the others, and is sent back to the server as ``already_fired`` where the tool
accepts it — the "nothing repeated" item of the navigation spec (§1.5).

Stdlib only, deliberately: the brief runs on the synchronous SessionStart path
and must not pay for a transport module to read a list of strings.

The file layout predates this module (``~/.claude/.memhub/directive_fired/
<session>.json``, a JSON list) and is kept as is so a plugin upgrade mid-week
neither loses the running sessions' state nor doubles it.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

STATE_DIR = Path.home() / ".claude" / ".memhub" / "directive_fired"
MAX_AGE_S = 7 * 24 * 3600
MAX_IDS = 1024


def path_for(state_dir: Path, session_id: str, suffix: str = "") -> Path | None:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")
    return (state_dir / f"{sid}{suffix}.json") if sid else None


def load_ids(state_dir: Path, session_id: str) -> list[str]:
    """Ids served earlier this session — empty on any problem, because a lost
    state file only means an item may be shown once more, never a broken hook."""
    path = path_for(state_dir, session_id)
    if not path:
        return []
    try:
        ids = json.loads(path.read_text(encoding="utf-8"))
        return [str(i) for i in ids if str(i).strip()] if isinstance(ids, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_ids(state_dir: Path, session_id: str, ids: list[str]) -> None:
    """Persist the served list; opportunistically prune stale sessions."""
    path = path_for(state_dir, session_id)
    if not path:
        return
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ids[-MAX_IDS:]), encoding="utf-8")
        cutoff = time.time() - MAX_AGE_S
        for old in state_dir.glob("*.json"):
            if old != path and old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    except OSError:
        pass  # state is an optimization, never worth failing the hook


def add_ids(state_dir: Path, session_id: str, new_ids: list[str]) -> list[str]:
    """Append ``new_ids`` (deduped, order kept) and return the full list."""
    have = load_ids(state_dir, session_id)
    seen = set(have)
    for i in new_ids:
        s = str(i or "").strip()
        if s and s not in seen:
            have.append(s)
            seen.add(s)
    save_ids(state_dir, session_id, have)
    return have


# ── small per-session markers (same directory, same lifetime) ─────────────

def load_marker(state_dir: Path, session_id: str, name: str) -> dict:
    path = path_for(state_dir, session_id, f"-{name}")
    if not path:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_marker(state_dir: Path, session_id: str, name: str, data: dict) -> None:
    path = path_for(state_dir, session_id, f"-{name}")
    if not path:
        return
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
