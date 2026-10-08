#!/usr/bin/env python3
"""Delete the plugin's local state files once nothing will read them again.

Nothing else in the plugin deletes these, so they only grew: on one machine
(2026-09-30) 4,743 files under rulebook/, 1,319 under turnflush/ and 275 under
mdcapture/, most of them months-old sessions and 3,053 lock files guarding
nothing. The sweep runs from the async Stop `rulebook_hook.py flush` lane at
most once a day (`.sweep-at`), so it never costs a turn.

Age is the only signal it trusts. A session's end is not one: SessionEnd does
not fire on every exit, and a resumed session reuses its id. So a data file
goes only once nothing has written it for its age below (`MAX_AGE_DAYS` unless
it says otherwise); a lock file whose data file is gone, and an arcs file,
go at any age:

  rulebook/[<backend>/]state/…       the install's own directory and the
                                     unkeyed one installs shared before
                                     state was keyed by backend
  rulebook/state/<session>.json      per-session dedup and armings, kept
                                     `RULEBOOK_AGE_DAYS`: session-armed rules
                                     are armed only at SessionStart, so a
                                     session left open and idle that long and
                                     then continued loses them until it restarts
  rulebook/state/wt-<hex>.json       per-checkout arming counters, same age: an
                                     "N edits since the last passing test" gate
                                     stops counting for a checkout idle that long
  turnflush/<id>.{json,sessionflush.json,health,lock}
                                     capture's upload cursor and breadcrumbs;
                                     never the newest `CAPTURE_KEEP` cursors,
                                     which capture_health reads for its
                                     next-session failure banner
  mdcapture/memhub-md-capture-*.json markdown-capture bookkeeping
  mdcapture/memhub-md-capture-*.activity
                                     its activity marker (mtime only)
  overview/pointers/*.json           a cache, refetched on use
  rulebook/state/*.arcs.json         no writer since plugin 0.95.0: any age
  harness/offsets/<session>.json     the harness Stop's transcript read offset;
                                     losing one costs one full read, nothing else

A lock file goes only while the sweep holds its lock, and only when the file
it guards is gone: every locker re-checks after acquiring that it locked the
file now at the path (`portable_lock.still_at`), so deleting one cannot let
two holders in. codexflush/ and cursorflush/ are left alone — Cursor's state
pins first-seen timestamps that a later import re-applies — and so is the
rulebook's fire ledger, which the flush lane uploads and rotates itself, under
its own locks (`rulebook_ledger.rotate`).

Fails open and silent, like every hook path. `--dry-run` prints what it would
delete; `--force` ignores the once-a-day marker.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    import portable_lock  # noqa: E402
except Exception:  # pragma: no cover — a sweep with no locks deletes no lock
    portable_lock = None

MAX_AGE_DAYS = 7
#: The rulebook's session and checkout state live longer: losing it disarms
#: rules (see the module docstring), and these files are small.
RULEBOOK_AGE_DAYS = 30
#: capture_health reads the newest 40 turnflush cursors (and any from the last
#: 24 h) for its failure banner; the sweep never touches those.
CAPTURE_KEEP = 40
SWEEP_EVERY_S = 20 * 3600
MARKER = ".sweep-at"


def state_root() -> Path:
    return Path(os.environ.get("MEMHUB_STATE_DIR")
                or (Path.home() / ".config" / "memhub-plugin"))


def rulebook_roots() -> list[Path]:
    """Every rulebook state directory this install may have left files in:
    its own, keyed by backend (rulebook_paths.py), and the unkeyed one every
    install shared before that, whose old session files nothing else will
    ever clean up. `$MEMHUB_RULEBOOK_BASE` names exactly one."""
    override = os.environ.get("MEMHUB_RULEBOOK_BASE")
    if override:
        return [Path(override)]
    shared = state_root() / "rulebook"
    try:
        import rulebook_paths  # noqa: PLC0415 — beside this file
        key = rulebook_paths.backend_key()
    except Exception:
        key = ""
    return [shared / key, shared] if key else [shared]


def harness_root() -> Path:
    return Path(os.environ.get("MEMHUB_HARNESS_DIR") or (state_root() / "harness"))


def _age_days(path: Path, now: float) -> float:
    try:
        return (now - path.stat().st_mtime) / 86400
    except OSError:
        return -1.0


def _unlink(path: Path, dry: bool, gone: list) -> None:
    try:
        if not dry:
            path.unlink()
        gone.append(path)
    except OSError:
        pass


def _unlink_lock(lock: Path, dry: bool, gone: list) -> None:
    """Delete a lock file only while holding its lock; a held one is in use."""
    if not lock.exists() or portable_lock is None:
        return
    try:
        fd = os.open(lock, os.O_RDWR)
    except OSError:
        return
    try:
        try:
            portable_lock.lock_exclusive(fd, blocking=False)
        except OSError:
            return  # someone holds it right now
        if portable_lock.still_at(fd, lock):
            _unlink(lock, dry, gone)  # a dry run reports only what it could take
        portable_lock.unlock(fd)
    finally:
        os.close(fd)


def sweep(now: float | None = None, dry: bool = False) -> list[Path]:
    """Delete what is past its age; returns the paths it removed."""
    now = time.time() if now is None else now
    gone: list[Path] = []
    for root in rulebook_roots():
        state = root / "state"
        # rulebook/state: sessions, checkouts, the error-arc files 0.95.0 stopped writing
        for data in sorted(state.glob("*.json")) if state.is_dir() else []:
            if data.name.endswith(".arcs.json"):
                _unlink(data, dry, gone)
                continue
            if _age_days(data, now) > RULEBOOK_AGE_DAYS:
                _unlink(data, dry, gone)
        # a lock whose file is gone guards nothing
        removed = set(gone)
        for lock in sorted(state.glob("*.lock")) if state.is_dir() else []:
            guarded = lock.with_suffix("")
            if guarded in removed or not guarded.exists():
                _unlink_lock(lock, dry, gone)

    # turnflush: whole sessions, never the newest cursors capture_health reads
    tf = state_root() / "turnflush"
    if tf.is_dir():
        cursors = sorted((p for p in tf.glob("*.json") if not p.name.endswith(".sessionflush.json")),
                         key=lambda p: _age_days(p, now))  # newest first; a vanished one (-1) harmlessly first
        keep = {p.name.split(".", 1)[0] for p in cursors[:CAPTURE_KEEP]}
        sessions: dict[str, list[Path]] = {}
        for p in tf.iterdir():
            if not p.is_file() or p.name == "log":
                continue
            sessions.setdefault(p.name.split(".", 1)[0], []).append(p)
        for sid, files in sessions.items():
            if sid in keep:
                continue
            data = [f for f in files if f.suffix != ".lock"]
            if data and min(_age_days(f, now) for f in data) <= MAX_AGE_DAYS:
                continue  # something of this session was written recently
            for f in data:
                _unlink(f, dry, gone)
            for f in files:
                if f.suffix == ".lock":
                    _unlink_lock(f, dry, gone)

    # markdown capture bookkeeping and the pointer cache
    for pattern in ("mdcapture/memhub-md-capture-*.json",
                    "mdcapture/memhub-md-capture-*.activity",
                    "overview/pointers/*.json"):
        for p in state_root().glob(pattern):
            if _age_days(p, now) > MAX_AGE_DAYS:
                _unlink(p, dry, gone)
    # the harness Stop's per-session read offsets
    for p in harness_root().glob("offsets/*.json"):
        if _age_days(p, now) > MAX_AGE_DAYS:
            _unlink(p, dry, gone)
    return gone


def maybe_sweep(now: float | None = None) -> int:
    """Run `sweep` at most once per SWEEP_EVERY_S. Never raises."""
    try:
        now = time.time() if now is None else now
        marker = state_root() / MARKER
        try:
            if now - marker.stat().st_mtime < SWEEP_EVERY_S:
                return 0
        except OSError:
            pass
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return len(sweep(now))
    except Exception:
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    if not args.force and not args.dry_run:
        print(f"swept {maybe_sweep()} file(s)")
        return 0
    gone = sweep(dry=args.dry_run)
    root = state_root()
    for p in gone:
        try:
            print(p.relative_to(root))
        except ValueError:
            print(p)
    print(f"{'would delete' if args.dry_run else 'deleted'} {len(gone)} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
