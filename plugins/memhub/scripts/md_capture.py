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
import ntpath
import os
import re
import sys
import tempfile
from pathlib import Path

STATE_PREFIX = "memhub-md-capture-"
# Per-user, 0700 — the same home the other per-session state lives in
# (flush_turn / codex_flush / cursor_flush). NOT the shared temp dir: the
# flusher uploads every path in `dirty`, so a world-writable, predictable
# state file would let another local user pick what gets shipped.
STATE_DIR = Path.home() / ".config" / "memhub-plugin" / "mdcapture"
PATH_KEYS = ("file_path", "notebook_path")
UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# Size floor in bytes, from the backtest's size distribution: real specs ran
# 9.2–32 KB; PR-body drafts topped out at 5.8 KB, READMEs and CLAUDE.md at
# ~5 KB. 6 KB catches 7/7 specs and leaks nothing that the location vetoes
# don't already remove. A frontmatter opt-in bypasses the floor; it only
# gates INFERRED captures.
MIN_BYTES = 6_000

# Upper bound. A generated markdown dump (a 200 MB log rendered as a table)
# is not a deliverable, and reading + hashing + redacting + shipping it on
# every Stop is a memory and timeout cliff on the async hook path. Judged a
# non-candidate so it leaves the retry list rather than recurring.
MAX_BYTES = 2_000_000

# Veto locations — by meaning, not by customer layout. Claude's own state under
# ~/.claude (auto-memory was half of all .md writes), the harness scratchpad,
# and OS temp dirs. Matched on the resolved absolute path.
VETO_PARTS = ("/.claude/", "/scratchpad/", "/tmp/", "/private/tmp/", "/var/folders/",
              "/node_modules/", "/.git/")
VETO_NAMES = {"CLAUDE.md", "AGENTS.md", "MEMORY.md"}
FRONTMATTER_OPT_IN = re.compile(r"^memhub:\s*artifact\s*$", re.M)


def _is_usable_windows_temp_root(path_key: str) -> bool:
    drive, tail = ntpath.splitdrive(path_key.replace("/", "\\"))
    return bool(drive and tail.strip("\\"))


def _windows_temp_roots() -> tuple[str, ...]:
    candidates = [os.environ.get("TEMP"), os.environ.get("TMP")]
    try:
        candidates.append(tempfile.gettempdir())
    except OSError:
        pass
    system_root = os.environ.get("SystemRoot")
    if system_root:
        candidates.append(os.path.join(system_root, "Temp"))
    roots = []
    for raw in candidates:
        if not raw:
            continue
        try:
            normalized = (
                os.path.abspath(raw).replace("\\", "/").rstrip("/").casefold()
            )
        except (OSError, TypeError, ValueError):
            continue
        if (
            normalized
            and _is_usable_windows_temp_root(normalized)
            and normalized not in roots
        ):
            roots.append(normalized)
    return tuple(roots)


WINDOWS_TEMP_ROOTS = _windows_temp_roots() if os.name == "nt" else ()


def _is_windows_temp_path(
    path_key: str, roots: tuple[str, ...] | None = None
) -> bool:
    for root in WINDOWS_TEMP_ROOTS if roots is None else roots:
        if not _is_usable_windows_temp_root(root):
            continue
        if path_key == root or path_key.startswith(root + "/"):
            return True
    return False


def state_path(session_id: str) -> Path | None:
    sid = UNSAFE.sub("", session_id or "")
    if not sid:
        return None
    return STATE_DIR / f"{STATE_PREFIX}{sid}.json"


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
    # Born 0700: mkdir under a 077 umask so no directory on the path is ever
    # world-readable, even briefly. The chmod covers a leaf that already
    # existed with a wider mode; if THAT fails we still write (capture must
    # never block an edit) but say so.
    prior = os.umask(0o077)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    finally:
        os.umask(prior)
    try:
        os.chmod(p.parent, 0o700)
    except OSError as e:
        print(f"[memhub-md-capture] state dir not 0700: {e}", file=sys.stderr)
    # Unique temp per writer: the sync collector and the async flusher can
    # write the same session's state at once, and a shared ".tmp" name lets
    # one truncate the other mid-write and publish a torn file.
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state))
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    # Match semantic path segments on every host. ``str(WindowsPath)`` uses
    # backslashes, while the denylist is intentionally slash-delimited.
    s = str(path).replace("\\", "/")
    if os.name == "nt":
        s = s.casefold()
        if _is_windows_temp_path(s):
            return False, "veto Windows temp root"
    if path.suffix.lower() != ".md":
        return False, "not markdown"
    name = path.name.casefold() if os.name == "nt" else path.name
    veto_names = {item.casefold() for item in VETO_NAMES} if os.name == "nt" else VETO_NAMES
    if name in veto_names:
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
    if size > MAX_BYTES:
        return False, f"above size cap ({size} > {MAX_BYTES})"
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
    # resolve() is non-strict: it canonicalises the existing prefix (symlinks,
    # `..`) whether or not the leaf exists yet, so the first Write (file absent)
    # and later Edits (file present) map to ONE key. A create-then-edit used to
    # store two keys and save the file twice under two names.
    try:
        key = str(path.resolve())
    except OSError:
        key = os.path.abspath(str(path))
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
