#!/usr/bin/env python3
"""The Rulebook state writes the mod's engine cannot make safely itself.

The mod (plugins/memhub-staging/mod/engine/) holds a session's dedup state in
its own memory, but two files are SHARED with every Python hook on the
machine and must be written under the same lock they use:

  * ``state/wt-<worktree_key>.json`` — the per-checkout ordering obligations
    (``OrderingEngine``), shared by every session of the checkout;
  * ``state/<session>.json`` — the session's ARMINGS (``armed``,
    ``armed_version``, ``armed_once``), shared with the Python lanes of the
    same session (a lane the mod released mid-session is served by Python,
    which must see what the mod armed, and the other way round).

Both are guarded by ``fcntl.flock`` on a sidecar (``portable_lock``), which
the mod's ``$.fs`` cannot take. So every WRITE goes through this script,
which calls ``rulebook_hook``'s own functions — the same lock, the same delta
merge, the same fail-open on lock timeout. Reads stay in the mod: both files
are replaced atomically, so a lockless read never sees a torn file.

    python3 scripts/rulebook_mod_state.py < '{"cwd": "...", "ops": [...]}'

ops (each answered in order, one result per op):

  {"op": "feed", "root", "rule", "hook_phase", "tool", "cmd", "file_path", "ok", "armed"}
      → {"outcome", "gate_msg", "legacy_fires"}   OrderingEngine(root).feed(...)
  {"op": "arm", "session", "event", "prompt", "rules", "repo", "gitdir"}
      → {"armed": [rule ids]}                     arm_obligations(...)
  {"op": "drop", "session", "rule_ids"}
      → {}                                        drop_arming + save_state (delta merge)
  {"op": "specs", "root", "spec_dir", "paths"}
      → {"active": [spec paths], "untouched": [[spec path, [paths]]]}
                                                  Probes.untouched_specs over `paths`

stdout: one JSON line ``{"results": [...]}``; a failed op is ``{"error": "..."}``.
Exit 0 unless stdin is unusable (2).
"""
from __future__ import annotations

import contextlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _feed(rh, op):
    rule = dict(op["rule"])
    outcome = rh.OrderingEngine(op["root"], "*").feed(
        rule, hook_phase=op["hook_phase"], tool=op["tool"], cmd=op.get("cmd") or "",
        file_path=op.get("file_path") or "", ok=op.get("ok"), armed=op.get("armed"))
    return {"outcome": outcome, "gate_msg": rule.get("_gate_msg"),
            "legacy_fires": rule.get("_legacy_fires")}


def _arm(rh, op):
    armed = rh.arm_obligations(op["rules"], op.get("repo") or "", op.get("gitdir") or "",
                               op["session"], op["event"], prompt=op.get("prompt") or "")
    return {"armed": armed}


def _drop(rh, op):
    sp = rh.state_path(op["session"])
    st = rh.load_state(sp)
    before = rh.snapshot_arming(st)
    for rid in op.get("rule_ids") or []:
        rh.drop_arming(st, rid)
    rh.save_state(sp, st, before=before)
    return {}


def _specs(rh, op):
    from spec_owns import load_specs_from_tree, owning_specs, spec_file_changed  # noqa: PLC0415
    root, spec_dir, paths = op["root"], op.get("spec_dir") or "docs/specs", list(op["paths"])
    specs = load_specs_from_tree(root, spec_dir)
    owning = owning_specs(paths, specs, exclude_prefix=spec_dir)
    updated = {s.path for s, _ in owning if spec_file_changed(s, paths)}
    answered = {p for s, touched in owning if s.path in updated for p in touched}
    out = []
    for s, touched in owning:
        left = [p for p in touched if p not in answered]
        if s.path not in updated and left:
            out.append([s.path, left])
    return {"active": sorted({s.path for s in specs}), "untouched": out}


OPS = {"feed": _feed, "arm": _arm, "drop": _drop, "specs": _specs}


def main() -> int:
    try:
        doc = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 2
    if not isinstance(doc, dict) or not isinstance(doc.get("ops"), list):
        return 2
    results = []
    with contextlib.redirect_stdout(sys.stderr):
        import rulebook_hook as rh  # noqa: PLC0415
        rh.set_active_base(str(doc.get("cwd") or ""))
        for op in doc["ops"]:
            try:
                results.append(OPS[op["op"]](rh, op))
            except Exception as exc:  # noqa: BLE001 — reported per op
                results.append({"error": f"{type(exc).__name__}: {exc}"[:300]})
    sys.stdout.write(json.dumps({"results": results}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
