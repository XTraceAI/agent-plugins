#!/usr/bin/env python3
"""Remind an author when edited code belongs to a git-authored spec."""
from __future__ import annotations
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from spec_owns import load_specs_from_tree, owning_specs, DEFAULT_SPEC_DIR
PATH_KEYS = ("file_path", "notebook_path")
STATE_PREFIX = "memhub-spec-reminder-"
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



def main():
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        return
    edited = _edited_path(payload)
    root = _git_root(edited) if edited else None
    if root is None:
        return
    relpath = edited.relative_to(root).as_posix()
    spec_dir = os.environ.get("MEMHUB_SPEC_DIR", DEFAULT_SPEC_DIR)
    hits = owning_specs([relpath], load_specs_from_tree(root, spec_dir), exclude_prefix=spec_dir)
    state = _state_file(str(payload.get("session_id") or ""))
    seen = _already_reminded(state)
    messages = []
    for spec, paths in hits:
        key = str(root) + ":" + spec.path
        if key in seen:
            continue
        seen.add(key)
        messages.append(f"{spec.path} owns {relpath} — update the spec in the same change if behavior changes. Git is the authored source; do not upload a second copy.")
    if messages:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n".join(messages)}}), flush=True)
        _record(state, seen)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[spec-reminder] skipped: {type(exc).__name__}", file=sys.stderr)
