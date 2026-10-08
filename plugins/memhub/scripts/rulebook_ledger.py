#!/usr/bin/env python3
"""Rotation of the rulebook hook's fire ledgers (ENG-1201). Stdlib only.

`ledger/fires.jsonl` and `events.jsonl` only grew (4 MB on one machine). Rows
BELOW a ledger's `.sent` watermark are confirmed on the server, so they may
go; rows at or past it may not, ever. `rotate` copies [cut, EOF) to a new file,
renames it over the ledger, then moves the watermark down by `cut`.

Two hazards, two guards:
  * an appender racing the copy would write to the old inode after the rename
    and its row would vanish — so every append takes `.append.lock` (held for
    one write, `append_lock`/`release`) and a rotation holds it from the copy
    through the rename;
  * a crash between the rename and the watermark move would leave an offset in
    the old file's coordinates pointing into the new one and SKIP rows — so the
    watermark is never moved blind. Before the rename, `.sent` durably gets an
    intent `rotating: {okey: {file, cut, ino, dev}}` naming the new file;
    `finish` (run after every rotation and at the start of every flush) moves
    the watermark by `cut` only if the file at the path IS that file, and
    otherwise leaves it — the old file is still there, so its offset is still
    right. No row is skipped and none is re-sent.

`fires.jsonl` is not rotated while pre-v0.59 verdicts wait to drain: that
drain (`rulebook_hook.legacy_verdict_batches`) reads fire rows from the start.
The caller holds `.flush.lock`, so no flush reads or moves a watermark
meanwhile. Nothing here raises into a hook.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile

ROTATE_AT = 1 << 20          # rotate a ledger only once it is past 1 MB
MIN_CUT = 256 << 10          # and only when that frees at least this much
ROT_PREFIX = ".rot-"         # the new file while it is being written


def _load_portable_lock():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portable_lock.py")
    spec = importlib.util.spec_from_file_location("_memhub_ledger_portable_lock", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    portable_lock = _load_portable_lock()
except Exception:
    portable_lock = None


def append_lock(ldir):
    """The lock every ledger append and every rotation takes: a handle, or
    None when it cannot be had (the append still happens — a lost row is worse
    than the race this guards, which needs a rotation at that instant)."""
    if portable_lock is None:
        return None
    try:
        f = open(os.path.join(ldir, ".append.lock"), "a+", encoding="utf-8")
    except OSError:
        return None
    try:
        portable_lock.lock_exclusive(f.fileno(), blocking=True)
        return f
    except OSError:
        f.close()
        return None


def release(lock):
    if lock is None:
        return
    try:
        portable_lock.unlock(lock.fileno())
    except Exception:
        pass
    lock.close()


def _sent_path(ldir):
    return os.path.join(ldir, ".sent")


def _load_sent(ldir):
    try:
        with open(_sent_path(ldir), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _fsync_dir(d):
    if os.name == "nt":
        return
    try:
        fd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _durable_json(path, obj):
    """Write-and-rename, flushed to disk before the rename and the directory
    after it: the rotation intent must be on disk before the ledger swap is."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(os.path.dirname(path))


def finish(ldir):
    """Settle a rotation intent left in `.sent` (by this flush, or one that
    died mid-rotation): the watermark moves down by `cut` exactly when the
    ledger now at the path is the rotated file the intent names; otherwise the
    swap never happened and the watermark is already right. Then drop the
    intent and any half-written rotation file."""
    sent = _load_sent(ldir)
    if "rotating" in sent:
        rot = sent.pop("rotating")
        for okey, r in (rot.items() if isinstance(rot, dict) else ()):
            try:
                st = os.stat(os.path.join(ldir, str(r["file"])))
                swapped = (st.st_ino, st.st_dev) == (int(r["ino"]), int(r["dev"]))
            except (OSError, KeyError, TypeError, ValueError):
                swapped = False
            if swapped:
                sent[okey] = max(0, int(sent.get(okey) or 0) - int(r["cut"]))
        _durable_json(_sent_path(ldir), sent)
    try:
        for name in os.listdir(ldir):
            if name.startswith(ROT_PREFIX):
                try:
                    os.unlink(os.path.join(ldir, name))
                except OSError:
                    pass
    except OSError:
        pass


def _legacy_drain_pending(ldir, sent):
    try:
        return int(sent.get("conversions_offset") or 0) < os.path.getsize(
            os.path.join(ldir, "conversions.jsonl"))
    except OSError:
        return False


def cut_at(path, watermark, size, keep_tail):
    """The byte to keep the ledger from: a line start at or below the
    watermark (nothing unsent is ever cut) that leaves the last `keep_tail`
    bytes in place — the hook's `_earliest_fire_after` reads exactly that
    tail, and must see the same rows after a rotation as before. 0 = none."""
    target = min(watermark, size - keep_tail)
    if target <= 0:
        return 0
    if target == watermark:
        return watermark                     # a watermark is always a line end
    with open(path, "rb") as f:
        f.seek(target - 1)
        pos = target - 1
        while True:
            chunk = f.read(64 << 10)
            if not chunk:
                return 0                     # no line start past the target
            i = chunk.find(b"\n")
            if i >= 0:
                return min(pos + i + 1, watermark)
            pos += len(chunk)


def _rotate_one(ldir, fname, okey, keep_tail):
    path = os.path.join(ldir, fname)
    try:
        if os.path.getsize(path) <= ROTATE_AT:
            return False
    except OSError:
        return False
    lock = append_lock(ldir)
    if lock is None:
        return False
    tmp = None
    try:
        sent = _load_sent(ldir)
        if okey == "fires_offset" and _legacy_drain_pending(ldir, sent):
            return False
        size = os.path.getsize(path)        # stable: appenders wait on our lock
        watermark = int(sent.get(okey) or 0)
        if watermark <= 0 or watermark > size:
            return False                    # nothing confirmed, or a reset pending
        cut = cut_at(path, watermark, size, keep_tail)
        if cut < MIN_CUT:
            return False
        fd, tmp = tempfile.mkstemp(dir=ldir, prefix=ROT_PREFIX)
        with os.fdopen(fd, "wb") as out, open(path, "rb") as src:
            src.seek(cut)
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
            st = os.fstat(out.fileno())
        # 1. the intent, durably, BEFORE the swap
        _durable_json(_sent_path(ldir), dict(sent, rotating={okey: {
            "file": fname, "cut": cut, "ino": st.st_ino, "dev": st.st_dev}}))
        # 2. the swap
        os.replace(tmp, path)
        _fsync_dir(ldir)
        return True
    except Exception:
        # No breadcrumb: `.last_error` is one slot, and a rotation hiccup must
        # not overwrite a fetch or flush failure the health check shows. A
        # rotation that did not happen costs disk, never a row.
        return False
    finally:
        # 3. settle the intent whatever happened: the watermark moves only if
        #    the swap landed
        try:
            finish(ldir)
        except Exception:
            pass
        release(lock)


def rotate(ldir, ledgers, keep_tail):
    """Drop rows the server has confirmed from each `(file, offset key)` in
    `ledgers` past ROTATE_AT, keeping the newest `keep_tail` bytes. Caller
    holds `.flush.lock`. Never raises."""
    try:
        finish(ldir)                         # a rotation a crash interrupted
    except Exception:
        return
    for fname, okey in ledgers:
        try:
            _rotate_one(ldir, fname, okey, keep_tail)
        except Exception:
            pass
