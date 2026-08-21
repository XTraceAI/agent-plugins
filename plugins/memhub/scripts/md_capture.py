#!/usr/bin/env python3
"""PostToolUse(Edit|MultiEdit|Write) collector for markdown artifact capture.

Records which ``.md`` files this session wrote so the Stop hook
(``md_capture_flush.py``) can save each one ONCE per turn as a draft artifact.
Nothing leaves the machine from here — this script only appends a path to a
per-session state file and exits. It runs under the system ``python3``
(stdlib only, 3.9 on macOS) on every edit, so it has to be cheap and it must
never block or fail the edit: every error path exits 0 with no output.

Why capture at all: specs and reports an agent writes are the session's
deliverables, and the transcript extractor routinely produces zero artifacts
from a session that wrote three of them (measured 2026-08-20). Why only
``.md``: a five-day backtest over 31 sessions found 67 distinct ``.md``
writes, of which 7 were real documents — and those 7 were the only ones over
~10 KB outside Claude's own memory and scratch directories. So the rule is
deliberately dumb: markdown, past a size floor, not in a veto location. A
file may opt in below the floor with ``memhub: artifact`` in its YAML
frontmatter. Customers' directory layouts are not consulted — ``docs/`` is
our convention, not theirs.

Why two stages: the edit hook sees one write; the deliverable is the file's
state when the agent STOPS. A spec edited nine times in a turn is one
artifact, not nine. The flush reads the file off disk at Stop.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

STATE_PREFIX = "memhub-md-capture-"
PATH_KEYS = ("file_path", "notebook_path")
UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Size floor in bytes, from the backtest's size distribution: real specs ran
# 9.2–32 KB; PR-body drafts topped out at 5.8 KB, READMEs and CLAUDE.md at
# ~5 KB. 6 KB catches 7/7 specs and leaks nothing that the location vetoes
# don't already remove. A frontmatter opt-in bypasses the floor; it only
# gates INFERRED captures.
MIN_BYTES = 6_000

# Veto locations — by meaning, not by customer layout. Claude's own state under
# ~/.claude (auto-memory was half of all .md writes), the harness scratchpad,
# and OS temp dirs. Matched on the resolved absolute path.
VETO_PARTS = ("/.claude/", "/scratchpad/", "/tmp/", "/private/tmp/", "/var/folders/",
              "/node_modules/", "/.git/")
VETO_NAMES = {"CLAUDE.md", "AGENTS.md", "MEMORY.md"}
FRONTMATTER_OPT_IN = re.compile(r"^memhub:\s*artifact\s*$", re.M)


def state_path(session_id: str) -> Path | None:
    sid = UNSAFE.sub("", session_id or "")
    if not sid:
        return None
    return Path(tempfile.gettempdir()) / f"{STATE_PREFIX}{sid}.json"


def load_state(session_id: str) -> dict:
    p = state_path(session_id)
    if p is None or not p.exists():
        return {"dirty": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("dirty"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"dirty": []}


def save_state(session_id: str, state: dict) -> None:
    p = state_path(session_id)
    if p is None:
        return
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, p)


def edited_path(payload: dict) -> Path | None:
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


def frontmatter(text: str) -> str:
    """The YAML frontmatter block, or '' — only the first 8 KB is scanned."""
    head = text[:8192]
    if not head.startswith("---"):
        return ""
    end = head.find("\n---", 3)
    return head[3:end] if end > 0 else ""


def is_candidate(path: Path, size: int | None = None, text: str | None = None) -> tuple[bool, str]:
    """(capture?, reason). Pure so the flush can re-check the on-disk state."""
    s = str(path)
    if path.suffix.lower() != ".md":
        return False, "not markdown"
    if path.name in VETO_NAMES:
        return False, f"veto name {path.name}"
    for part in VETO_PARTS:
        if part in s:
            return False, f"veto location {part}"
    if text is not None and FRONTMATTER_OPT_IN.search(frontmatter(text)):
        return True, "frontmatter opt-in"
    if size is None:
        return False, "size unknown"
    if size < MIN_BYTES:
        return False, f"below size floor ({size} < {MIN_BYTES})"
    return True, f"markdown >= {MIN_BYTES} bytes"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id")
    path = edited_path(payload)
    if not isinstance(session_id, str) or path is None:
        return 0
    # Only the cheap, content-free checks run here; size and frontmatter are
    # judged at flush time from the file's final on-disk state.
    ok, _ = is_candidate(path, size=MIN_BYTES)  # size floor deferred → treat as passing
    if not ok:
        return 0
    key = str(path.resolve()) if path.exists() else str(path)
    state = load_state(session_id)
    if key not in state["dirty"]:
        state["dirty"].append(key)
        save_state(session_id, state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — a collector must never fail an edit
        sys.exit(0)
