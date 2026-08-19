#!/usr/bin/env python3
"""Resolve the repo's MemHub room (agent brain) ONCE and cache it, so every
writer routes to the same brain.

Before this, each writer re-derived the room independently: five SKILL.md files
each told the agent to build the name `Repo: <org>/<name>` and exact-match it in
`list_agent_brains`, while the two AUTOMATIC capture paths (the SessionEnd hook
and the commit/PR flush) passed only `namespace` and so never reached a brain at
all — their memories landed in personal memory. A cached id makes the routing a
property of the repo rather than something each caller rediscovers (and
occasionally gets wrong).

The cache lives in the user's own config dir, NEVER inside the repo:

    ~/.config/memhub-plugin/rooms.json

A brain id is account state, not project state. Writing it into the working tree
would push a private id into whatever repo the user is in — including public
ones — and make every user decide whether to commit it. Keying on the repo's
canonical room NAME (derived from the git remote) also means all worktrees of a
repo share one entry automatically, with no dependence on which branch is
checked out.

Entries are keyed by repo, then by BACKEND — prod and staging are separate
deployments with different brain ids for the same repo, so one flat id would
silently write to the wrong backend's brain on whichever install didn't match:

    {"version": 1, "repos": {
       "Repo: XTraceAI/agent-plugins": {
          "production": {"brain_id": "<uuid>"},
          "staging":    {"brain_id": "<uuid>"}}}}

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
import contextlib
import json
import os
import subprocess
import time
import sys
from pathlib import Path

#: Shared with _memhub_auth's token cache — this is already the plugin's
#: per-user state dir, and the room id belongs there for the same reason the
#: OAuth token does: it is account state, not project state.
#: $MEMHUB_ROOMS_FILE relocates it (tests, CI, sandboxed homes).
ROOMS_PATH = Path(
    os.environ.get("MEMHUB_ROOMS_FILE")
    or Path.home() / ".config" / "memhub-plugin" / "rooms.json"
)

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


@contextlib.contextmanager
def _locked():
    """Serialize the load→mutate→replace window across processes.

    One file now holds every repo, so a plain read-modify-write can lose an
    update: two writers both read, both replace, and the first writer's new
    entry is gone. `os.replace` makes each write atomic but does nothing about
    the window around it. Parallel agents across repos (a fleet) make this
    reachable, so take an exclusive lock and re-read inside it.

    A separate .lock file, not the cache itself — locking the file you are
    about to replace releases the lock with the old inode.
    """
    ROOMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        import portable_lock
    except ImportError:  # native Windows — unsupported platform, degrade
        yield
        return
    with open(ROOMS_PATH.with_name(ROOMS_PATH.name + ".lock"), "w",
              encoding="utf-8") as fh:
        portable_lock.lock_exclusive(portable_lock.fileno_of(fh))
        try:
            yield
        finally:
            portable_lock.unlock(portable_lock.fileno_of(fh))


def _load() -> dict:
    empty = {"version": 1, "repos": {}}
    try:
        data = json.loads(ROOMS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("repos"), dict):
        return empty
    return data


def _write_atomic(data: dict) -> None:
    """Replace the cache file with ``data``, all-or-nothing.

    A half-written cache reads as "no room", which would silently send sessions
    to personal memory until someone noticed. Unique temp name so two writers
    cannot share one temp file. Raises ``OSError`` on failure — every caller
    holds the lock and decides for itself whether a failed write is worth
    propagating.
    """
    tmp = ROOMS_PATH.with_name(f"{ROOMS_PATH.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, ROOMS_PATH)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def read_room(cwd: str | Path | None = None, env: str | None = None) -> dict | None:
    """The cached room for this repo+backend, or None. Never raises.

    ``cwd=None`` means "the calling process's directory". Only pass None when
    that is genuinely the repo you mean (e.g. reading a body from stdin). A
    caller that knows the content's true origin — a transcript's ``cwd``, a
    file's directory — must skip the lookup when that origin is unknown rather
    than passing None, or an unrelated repo's room gets used.
    """
    key = room_name(cwd)
    if key is None:
        return None
    repo = _load()["repos"].get(key)
    if not isinstance(repo, dict):
        return None
    entry = repo.get(env or current_env())
    if not isinstance(entry, dict):
        return None
    # `isinstance(str)`, not just truthiness: a hand-edited cache can hold a
    # number here, and callers format the id (`brain_id[:8]`). Returning a
    # non-str would turn a successful write into a crash in the logging line.
    brain_id = entry.get("brain_id")
    if not isinstance(brain_id, str) or not brain_id:
        return None
    return {**entry, "brain_id": brain_id, "name": key}


def write_room(
    brain_id: str,
    name: str | None = None,
    cwd: str | Path | None = None,
    env: str | None = None,
    org_id: str | None = None,
) -> Path:
    key = name or room_name(cwd)
    if key is None:
        sys.exit("not a git repository — nothing to key a room on")

    with _locked():
        # Re-read INSIDE the lock: anything another writer added since this
        # process started must survive our replace.
        data = _load()
        repo = data["repos"].get(key)
        if not isinstance(repo, dict):
            repo = {}
        # Only this backend's entry is replaced — a repo used from both installs
        # keeps both ids, and other repos' entries are untouched.
        # ``resolved_at`` stamps every write, including one that could not
        # determine the org. It is what bounds the org re-probe below to once a
        # day instead of once a turn.
        entry = {"brain_id": brain_id, "resolved_at": time.time()}
        # The org that OWNS the brain, recorded because a brain is resolved
        # inside exactly one org and the caller's default org is not
        # necessarily that one — it follows whichever org was last selected in
        # the MemHub app, so it changes under a running session. Without this,
        # every capture into a room outside the default org fails with
        # "Agent brain not found".
        if isinstance(org_id, str) and org_id:
            entry["org_id"] = org_id
        repo[env or current_env()] = entry
        data["repos"][key] = repo
        data.setdefault("version", 1)
        _write_atomic(data)
    return ROOMS_PATH


# How long a "this repo has no brain" answer is trusted before we look again.
# Without a negative entry every capture would re-query on every turn for repos
# that simply have no room; with one that never expires, a brain created next
# week would never be picked up.
MISS_TTL_S = 24 * 60 * 60

# How long an INCOMPLETE lookup (the server could not answer for every org)
# suppresses the next one. Deliberately short: nothing was learned, so this is
# only a rate limit on retrying, not a claim about the repo. Without it a
# sustained backend outage turns a once-a-day negative lookup into a round trip
# on every single turn — hammering a backend that is already struggling.
PROBE_BACKOFF_S = 5 * 60


def write_probe_backoff(
    cwd: str | Path | None = None, env: str | None = None,
) -> None:
    """Record that a lookup was attempted but could not complete.

    Distinct from :func:`write_miss`, which asserts "this repo has no room" and
    is trusted for a day. An incomplete search asserts nothing — some org could
    not be listed — so it must not brand the repo room-less; it only stops the
    next few turns from re-asking.

    Never touches ``brain_id`` or ``missed_at``: an entry that already routes
    must keep routing, and a real miss must keep its own clock.
    """
    key = room_name(cwd)
    if key is None:
        return
    try:
        with _locked():
            data = _load()
            repo = data["repos"].get(key)
            if not isinstance(repo, dict):
                repo = {}
            slot = repo.get(env or current_env())
            if not isinstance(slot, dict):
                slot = {}
            slot["probed_at"] = time.time()
            repo[env or current_env()] = slot
            data["repos"][key] = repo
            data.setdefault("version", 1)
            _write_atomic(data)
    except Exception:  # noqa: BLE001 — a rate limit must never fail a capture
        return


def resolve_due(cwd: str | Path | None = None, env: str | None = None) -> bool:
    """True when this repo's room should be looked up on the server.

    False when a brain id is already cached (nothing to do) or when a recent
    lookup found none (do not re-ask on every turn). A miss older than
    :data:`MISS_TTL_S` is due again, so a brain created later is picked up.

    An entry cached WITHOUT an ``org_id`` is due again, so a cache written
    before rooms carried their org gets upgraded rather than failing forever.
    Without that, an existing install stays broken after the fix ships: the id
    is present, so nothing would ever re-ask, and every capture keeps resolving
    the brain in the wrong org.

    That re-probe is rate-limited by ``resolved_at`` on the same
    :data:`MISS_TTL_S` clock as a miss. A backend that cannot report orgs at all
    would otherwise leave the entry permanently org-less and re-resolve on EVERY
    turn — trading a silent failure for a per-turn round trip.
    """
    key = room_name(cwd)
    if key is None:
        return False  # not a git repo — there is no room to resolve
    entry = _load()["repos"].get(key)
    entry = entry.get(env or current_env()) if isinstance(entry, dict) else None
    if not isinstance(entry, dict):
        return True
    # A recent INCOMPLETE probe suppresses the next one whatever else the entry
    # says. Checked first because it is a rate limit on ASKING, and it applies
    # to every reason for asking — an unresolved repo, and an org-less entry
    # waiting to be upgraded. Without it in front, the upgrade path re-probes
    # on every turn for as long as the backend stays unable to answer.
    try:
        if (time.time() - float(entry.get("probed_at", 0))) < PROBE_BACKOFF_S:
            return False
    except (TypeError, ValueError):
        pass
    if isinstance(entry.get("brain_id"), str) and entry["brain_id"]:
        if isinstance(entry.get("org_id"), str) and entry["org_id"]:
            return False
        try:
            return (time.time() - float(entry.get("resolved_at", 0))) > MISS_TTL_S
        except (TypeError, ValueError):
            return True
    try:
        return (time.time() - float(entry.get("missed_at", 0))) > MISS_TTL_S
    except (TypeError, ValueError):
        return True


def write_miss(cwd: str | Path | None = None, env: str | None = None) -> None:
    """Record that this repo has no room on this backend, as of now.

    Stored WITHOUT a ``brain_id`` so :func:`read_room` keeps returning None and
    every existing caller behaves exactly as before — this is a rate limit on
    asking, not a routing decision.

    A miss NEVER overwrites a resolved id. The lookup that produced this miss
    listed the brains at some earlier moment, so by the time it writes, someone
    else may have resolved or created the room — `/memhub:onboard` racing a
    background flush is the obvious case. Clobbering there would silently send
    capture to personal memory for the whole TTL, immediately after the user did
    the very thing meant to fix that. Checked inside the lock, so the read and
    the decision cannot be split.
    """
    key = room_name(cwd)
    if key is None:
        return
    with _locked():
        data = _load()
        repo = data["repos"].get(key)
        if not isinstance(repo, dict):
            repo = {}
        existing = repo.get(env or current_env())
        if isinstance(existing, dict) and isinstance(existing.get("brain_id"), str) \
                and existing["brain_id"]:
            return  # someone resolved it while we were looking — theirs wins
        repo[env or current_env()] = {"missed_at": time.time()}
        data["repos"][key] = repo
        data.setdefault("version", 1)
        try:
            _write_atomic(data)
        except OSError:
            # A rate limit that could not be recorded costs one extra lookup
            # next turn. Never worth failing the capture that called this.
            pass


def forget_room(cwd: str | Path | None = None, env: str | None = None) -> bool:
    """Drop this repo's cached brain for ONE backend. True if an entry went.

    The one thing :func:`write_miss` deliberately refuses to do, and for a good
    reason: a lookup that came back empty is weak evidence — it may have raced
    someone resolving the room — so it must never clobber an id somebody
    resolved on purpose. But a backend answering "Agent brain not found" for the
    id we just SENT is not weak evidence. That is the backend stating this id is
    not a brain it has.

    Without this the two rules composed into a cache that could not be
    corrected. A ``production`` entry holding a staging brain id re-resolved on
    every turn, found nothing, wrote a miss that declined to overwrite the id,
    and handed the same dead id back to the caller (``brain_resolve`` returns
    the stale ``room`` on every failure path) — so every flush re-sent it and
    every flush was rejected. Per-turn capture AND the SessionEnd backstop both
    failed that way for days, because both only fall back to long-term memory
    when there is NO room, never when the room turns out not to exist.

    Scoped to one env, because prod and staging hold different ids for the same
    repo and only the backend that answered is discredited. Leaves no
    ``missed_at``: the next resolution should genuinely re-ask, and if the room
    really is absent that lookup writes its own miss and the day-long quiet
    starts then, on evidence rather than on this inference.

    Never raises — this runs inside a capture hook, so a cache that cannot be
    corrected must degrade to the old behaviour rather than take the flush down.
    """
    key = room_name(cwd)
    if key is None:
        return False
    try:
        with _locked():
            data = _load()
            repo = data["repos"].get(key)
            if not isinstance(repo, dict):
                return False
            if repo.pop(env or current_env(), None) is None:
                return False
            # A repo left with no backends at all is removed outright rather
            # than kept as an empty object: `resolve_due` reads them
            # identically, and the file stays legible to whoever opens it next.
            if repo:
                data["repos"][key] = repo
            else:
                data["repos"].pop(key, None)
            data.setdefault("version", 1)
            _write_atomic(data)
        return True
    except Exception:  # noqa: BLE001 — never fail a capture over the cache
        return False


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
