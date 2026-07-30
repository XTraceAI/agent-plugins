#!/usr/bin/env python3
"""Resolve the repo's MemHub room (agent brain) ONCE and cache it in
`.claude/memhub-room.json`, so every writer routes to the same brain.

Before this, each writer re-derived the room independently: five SKILL.md files
each told the agent to build the name `Repo: <org>/<name>` and exact-match it in
`list_agent_brains`, while the two AUTOMATIC capture paths (the SessionEnd hook
and the commit/PR flush) passed only `namespace` and so never reached a brain at
all — their memories landed in personal memory. A cached id makes the routing a
property of the repo rather than something each caller rediscovers (and
occasionally gets wrong).

The cache is repo-local and MEANT to be committed: the room is shared team-wide,
so checking it in routes a teammate's first session correctly with no lookup, and
git worktrees inherit it from the branch instead of each needing their own copy.

Entries are keyed by BACKEND, because prod and staging are separate deployments
with different brain ids for the same repo — one flat id would silently write to
the wrong backend's brain on whichever install didn't match:

    {"version": 1, "rooms": {
       "production": {"brain_id": "<uuid>", "name": "Repo: XTraceAI/memhub-claude-plugin"},
       "staging":    {"brain_id": "<uuid>", "name": "Repo: XTraceAI/memhub-claude-plugin"}}}

    # what room does this repo route to? (prints the brain id, or nothing)
    python3 room_map.py show [--cwd <dir>] [--env production|staging]

    # record the room after resolving it via list_agent_brains/create_agent_brain
    python3 room_map.py set --brain-id <uuid> [--name "Repo: org/name"]

    # the conventional room NAME for this repo, for the exact-match lookup
    python3 room_map.py name

Reads NEVER raise: background hooks call `read_room()` on every flush, and a
malformed or absent cache must degrade to "no room" (import stays personal),
never disturb the user's session.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOM_RELPATH = Path(".claude") / "memhub-room.json"

#: Fallback when the backend can't be determined. Prod is the safe default: the
#: staging install is the deliberate opt-in, so a mis-detected env writes to the
#: brain the user almost certainly meant.
DEFAULT_ENV = "production"


def env_for_url(url: str) -> str:
    """Map an MCP endpoint to a cache key. Substring, not host equality, so a
    `MEMHUB_MCP_BASE_URL` override pointing at any staging host still keys to
    staging rather than silently sharing production's entry."""
    return "staging" if "staging" in url.lower() else "production"


def current_env() -> str:
    """The backend THIS plugin install talks to.

    `default_url()` reads the plugin's own .mcp.json (falling back to its
    install path), so a script running from the memhub-staging install resolves
    staging without the caller having to know which install it is.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _memhub_auth import default_url

        return env_for_url(default_url())
    except Exception:  # noqa: BLE001 — unreadable config must not break routing
        return DEFAULT_ENV


def repo_root(cwd: str | Path | None = None) -> Path | None:
    """The git toplevel for `cwd`, or None outside a repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd or Path.cwd()), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    root = out.stdout.strip()
    return Path(root) if out.returncode == 0 and root else None


def _no_remote_name(root: Path, cwd: str | Path | None) -> str:
    """The no-remote fallback from references/repo-brain.md §2.

    Keyed on the MAIN worktree, not the current one: `--show-toplevel` returns
    the *linked worktree's* path, so using it would give every worktree of one
    repo a different name — the exact split the room cache exists to prevent.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd or root), "rev-parse",
             "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        common = out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        common = ""
    if not common:
        return f"Repo: {root.name}"
    # Normalize the three layouts the reference lists — dropping the parent
    # unconditionally would name a bare repo after its CONTAINING directory.
    p = Path(common)
    if p.name == ".git":
        p = p.parent
    return f"Repo: {p.name.removesuffix('.git') or root.name}"


def room_name(cwd: str | Path | None = None) -> str | None:
    """`Repo: <org>/<name>` from the git remote — the conventional room name.

    Implements references/repo-brain.md §1: derived from the REMOTE, not the
    directory, so every worktree of a repo resolves to one room. Case is
    preserved (`XTraceAI/xmem` and `xtraceai/xmem` are different brains).
    """
    root = repo_root(cwd)
    if root is None:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return _no_remote_name(root, cwd)
    url = out.stdout.strip()
    if out.returncode != 0 or not url:
        return _no_remote_name(root, cwd)
    # Both remote shapes reduce to org/name: scp-style `git@host:org/name.git`
    # and URL-style `https://host/org/name.git`. Self-hosted hosts can nest
    # deeper, so keep only the LAST two segments.
    path = url.rstrip("/").removesuffix(".git")
    if ":" in path and "//" not in path:
        path = path.rsplit(":", 1)[-1]
    path = "/".join([s for s in path.split("/") if s][-2:])
    return f"Repo: {path}" if path else _no_remote_name(root, cwd)


def _load(path: Path) -> dict:
    empty = {"version": 1, "rooms": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("rooms"), dict):
        return empty
    return data


def read_room(cwd: str | Path | None = None, env: str | None = None) -> dict | None:
    """The cached room for this repo+backend, or None. Never raises."""
    root = repo_root(cwd)
    if root is None:
        return None
    rooms = _load(root / ROOM_RELPATH).get("rooms") or {}
    entry = rooms.get(env or current_env())
    if not isinstance(entry, dict) or not entry.get("brain_id"):
        return None
    return entry


def write_room(
    brain_id: str,
    name: str | None = None,
    cwd: str | Path | None = None,
    env: str | None = None,
) -> Path:
    root = repo_root(cwd)
    if root is None:
        sys.exit("not a git repository — the room cache is repo-local")
    path = root / ROOM_RELPATH
    data = _load(path)
    rooms = data.get("rooms")
    if not isinstance(rooms, dict):
        rooms = {}
    entry = {"brain_id": brain_id}
    resolved_name = name or room_name(root)
    if resolved_name:
        entry["name"] = resolved_name
    # Only this backend's entry is replaced — a repo used from both installs
    # keeps both ids.
    rooms[env or current_env()] = entry
    data["rooms"] = rooms
    data.setdefault("version", 1)

    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    # Atomic for the same reason artifact_map.py is: a half-written cache reads
    # as "no room", which would silently send a session to personal memory.
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path


def cmd_show(args: argparse.Namespace) -> int:
    entry = read_room(args.cwd, args.env)
    if entry is None:
        # Silent + exit 1: callers test the exit code or the empty stdout, and
        # "no room yet" is an ordinary state, not an error to report.
        return 1
    if args.json:
        print(json.dumps(entry))
    else:
        print(entry["brain_id"])
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    path = write_room(args.brain_id, args.name, args.cwd, args.env)
    env = args.env or current_env()
    print(f"room for {env}: {args.brain_id} -> {path}")
    return 0


def cmd_name(args: argparse.Namespace) -> int:
    name = room_name(args.cwd)
    if not name:
        return 1
    print(name)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Shared via `parents=` rather than on the top-level parser, so the flags
    # read naturally AFTER the subcommand (`show --cwd X`), which is how the
    # hooks and skills invoke this.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", help="directory inside the repo (default: cwd)")
    common.add_argument("--env", choices=["production", "staging"],
                        help="backend to read/write (default: this install's)")
    sub = parser.add_subparsers(dest="command", required=True)

    show = sub.add_parser("show", parents=[common],
                          help="print the cached brain id, if any")
    show.add_argument("--json", action="store_true", help="print the whole entry")
    show.set_defaults(func=cmd_show)

    setter = sub.add_parser("set", parents=[common], help="cache the resolved room")
    setter.add_argument("--brain-id", required=True)
    setter.add_argument("--name", help="the room's exact name")
    setter.set_defaults(func=cmd_set)

    naming = sub.add_parser("name", parents=[common],
                            help="print the conventional room name")
    naming.set_defaults(func=cmd_name)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
