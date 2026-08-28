#!/usr/bin/env python3
"""PostToolUse(Edit|Write|MultiEdit|NotebookEdit) hook: when the agent edits a
file linked to a canonical artifact, remind it to VERSION that artifact rather
than publish a parallel one.

Why this exists: retrieval is semantic, so a stale artifact can rank ABOVE its
own correction. Observed 2026-07-20 — an over-read "AppWorld ON tripled partial
progress" artifact scored 0.596 for "does memory help?" while its correction
("within the noise floor") scored 0.466, so a fresh agent read the wrong
conclusion first. `save_artifact` already versions by canonical `name`; what
was missing is a prompt to use it when the underlying code moves.

Hooks cannot call MCP tools, so this only REMINDS — the agent runs the
`save_artifact.py` upload itself. That is deliberate: the version bump stays
visible and auditable instead of team memory being rewritten on every
keystroke. The reminder names the SCRIPT, not a raw `save_artifact` call:
the server rejects any `parent_id` that is not the lineage's current head
(`parent_stale`), and the skill forbids re-emitting file contents — so the
right move is "re-upload the spec FILE under the same name" and let the
server chain it onto the latest version.

The links live in the edited file's repo at `.claude/artifact-map.json`:

    {"version": 1, "links": [
      {"glob": "appworld/{run,agent,worker}.py",
       "artifact_id": "...", "artifact_name": "...",
       "path": "docs/specs/appworld-harness.md"}]}

`path` is the repo-relative location of the artifact's own file (the spec),
which is what the upload command takes. No brain id lives in the map: a brain
id is account state, not project state (see `room_map.py`), and the upload
script resolves the repo's room itself.

Any failure (no map, bad JSON, bad glob, unreadable state) exits 0 with no
output — a reminder hook must never block an edit.
"""

from __future__ import annotations  # hooks run under system python3 (3.9 on macOS)

import json
import re
import sys
import tempfile
from pathlib import Path

MAP_RELPATH = Path(".claude") / "artifact-map.json"
PATH_KEYS = ("file_path", "notebook_path")
# Session-debounce state lives beside other per-session temp files. Keyed by
# session id so a new session re-reminds; sanitized because the id reaches the
# filesystem.
STATE_PREFIX = "memhub-artifact-sync-"
UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _edited_path(payload: dict) -> Path | None:
    """The absolute path this tool call wrote, or None."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            path = Path(value)
            if not path.is_absolute():
                cwd = payload.get("cwd")
                if not isinstance(cwd, str) or not cwd:
                    return None
                path = Path(cwd) / path
            return path
    return None


def _git_root(start: Path) -> Path | None:
    """Nearest ancestor holding a .git entry (dir for a checkout, file for a
    worktree). Walks the path lexically — the edited file itself may not exist
    on disk yet (Write creates it after the hook input is captured)."""
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _expand_braces(pattern: str) -> list[str]:
    """`a/{x,y}.py` -> ['a/x.py', 'a/y.py']. Innermost-first, no nesting
    support beyond what repeated passes resolve."""
    match = re.search(r"\{([^{}]*)\}", pattern)
    if not match:
        return [pattern]
    head, tail = pattern[: match.start()], pattern[match.end() :]
    out = []
    for option in match.group(1).split(","):
        out.extend(_expand_braces(f"{head}{option}{tail}"))
    return out


def _to_regex(pattern: str) -> str:
    """Glob -> regex with POSIX path semantics: `*` and `?` stop at `/`, `**`
    crosses directories."""
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


def _matches(glob: str, relpath: str) -> bool:
    """`glob` may hold `|`-separated alternatives, each with braces/`*`/`**`."""
    for alternative in glob.split("|"):
        alternative = alternative.strip()
        if not alternative:
            continue
        for expanded in _expand_braces(alternative):
            try:
                if re.fullmatch(_to_regex(expanded), relpath):
                    return True
            except re.error:
                continue
    return False


def _load_links(root: Path) -> list[dict]:
    try:
        data = json.loads((root / MAP_RELPATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    links = data.get("links") if isinstance(data, dict) else None
    if not isinstance(links, list):
        return []
    return [link for link in links if isinstance(link, dict)]


def _state_file(session_id: str) -> Path:
    key = UNSAFE.sub("_", session_id)[:64] or "nosession"
    return Path(tempfile.gettempdir()) / f"{STATE_PREFIX}{key}.json"


def _already_reminded(state: Path) -> set[str]:
    try:
        seen = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return set()
    return set(seen) if isinstance(seen, list) else set()


def _record(state: Path, seen: set[str]) -> None:
    try:
        state.write_text(json.dumps(sorted(seen)), encoding="utf-8")
    except OSError as exc:
        # Losing the debounce means a duplicate reminder, not a broken edit.
        print(f"[artifact-sync] could not persist debounce state: {exc}", file=sys.stderr)


def link_for_path(root: Path, relpath: str) -> dict | None:
    """The link whose own `path` IS this file (the artifact's source file),
    or None. Used by the markdown auto-capture to leave a hand-saved lineage
    alone. Never raises."""
    for link in _load_links(root):
        if isinstance(link.get("path"), str) and link["path"] == relpath:
            return link
    return None


def _room_hint(root: Path) -> str:
    """Best-effort name of the edited repo's cached room, for the reminder
    text. The brain is resolved HERE, at reminder time, from the user's own
    room cache — never read from the repo tree."""
    try:
        from room_map import read_room
        room = read_room(root)
    except Exception:  # noqa: BLE001 — a hint, never a reason to fail an edit
        return ""
    if not room:
        return ""
    return f' (it routes to the repo room "{room["name"]}" automatically)'


def _reminder(relpath: str, link: dict, root: Path) -> str:
    name = link.get("artifact_name") or "(unnamed artifact)"
    spec_path = link.get("path") if isinstance(link.get("path"), str) else "<the artifact's file>"
    return (
        f'⚠️ Artifact-sync: you edited {relpath}, governed by canonical artifact\n'
        f'   "{name}" (id {link["artifact_id"]}).\n'
        "   If this change alters anything that artifact asserts, edit its file and\n"
        "   re-upload it under the SAME name — the server versions by name, so no\n"
        "   parent id is needed (and a stale one is rejected):\n"
        "     uv run --with 'mcp<2' python \"$CLAUDE_PLUGIN_ROOT/scripts/save_artifact.py\" \\\n"
        f'       --file "{spec_path}" --name "{name}" --rationale "<why this version>"\n'
        f"   Do NOT create a new artifact and do NOT re-emit the file's contents{_room_hint(root)}.\n"
        "   If a prior conclusion is now wrong, state the correction explicitly so the\n"
        "   new version supersedes it in retrieval."
    )


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return

    edited = _edited_path(payload)
    if edited is None:
        return
    root = _git_root(edited)
    if root is None:
        return
    try:
        relpath = edited.relative_to(root).as_posix()
    except ValueError:
        return

    links = [
        link
        for link in _load_links(root)
        if isinstance(link.get("glob"), str)
        and isinstance(link.get("artifact_id"), str)
        and _matches(link["glob"], relpath)
    ]
    if not links:
        return

    session_id = payload.get("session_id")
    state = _state_file(session_id if isinstance(session_id, str) else "")
    seen = _already_reminded(state)

    fresh = []
    for link in links:
        if link["artifact_id"] in seen:
            continue
        seen.add(link["artifact_id"])
        fresh.append(link)
    if not fresh:
        return

    # Emit BEFORE persisting the debounce, and only persist if the emit
    # succeeded. Recording first means a killed process (10s hook timeout,
    # SIGTERM) or a failed write (BrokenPipeError, UnicodeEncodeError on the
    # ⚠️/em-dash under a non-UTF-8 stdout locale) leaves the artifact marked
    # reminded for the rest of the session while the agent never saw it — a
    # permanent silent miss. This ordering fails the other way, toward a
    # duplicate reminder, which is the acceptable failure mode.
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "\n\n".join(
                        _reminder(relpath, link, root) for link in fresh
                    ),
                }
            }
        ),
        flush=True,
    )
    _record(state, seen)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # never fail an edit over a reminder
        print(f"[artifact-sync] hook error, skipping: {exc}", file=sys.stderr)
