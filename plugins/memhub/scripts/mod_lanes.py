"""Lane claims: the Python half of the mod's hand-over (mods spec §3.3, D2).

When the plugin's mod (``mod/``) serves a Rulebook lane in-process, the
command hook for that lane must step aside or every call is evaluated twice.
The mod says which lanes it serves, for WHICH SESSION, from WHICH PROCESS and
UNTIL WHEN, through ONE environment variable per install, set with
``$.env.set`` (command hooks started after it see the value, spec §1.3):

    MEMHUB_MOD_LANES_STAGING=<session_id>:<expires_epoch_s>:<pid>:<lanes>
    e.g. 8f1c…:1791331524:21242:pre,post,prompt,session

A claim is honoured only while all of these hold, and any doubt serves:

* **The lease is live**: ``time.time() < expires``. The mod writes
  ``now + 90 s`` on every claim write and renews it every 30 s from a
  ``$.clock.every`` timer (mod/claims.ts). Timers die with the module, so a
  mod that crashes, fails a hot reload or is unloaded stops renewing and its
  claim lapses on its own within 90 s; Python then serves. ``$.env`` writes go
  straight to the process environment and the engine never undoes them on
  unload, so nothing else would end such a claim. A missing or garbled expiry
  is no claim. No clock tolerance: the mod's ``$.clock.now`` and this
  ``time.time`` read the same machine clock (verified: ``$.clock.now()`` equals
  ``Date.now()``), and the mod keeps serving a little past the expiry itself,
  so the hand-back overlaps (both evaluate briefly) rather than gaps.
* **The process is the one that claimed**: when both are known, ``<pid>``
  equals ``CLAUDE_PID``, which Claude Code sets on every hook command to its
  own pid. A ``claude`` started inside a session (a Bash call, a script,
  ``claude -p`` from a skill) inherits the variable; if that child's mod did
  not load (``allowManagedModsOnly``, ``--safe-mode``, ``disableAllHooks``, an
  older Claude Code, a load error), honouring the inherited claim would leave
  NOTHING evaluating the rules. The child's hooks carry its own pid.
* **The session is the one that claimed**: ``<session_id>`` equals the
  payload's ``session_id`` — the same guard a second way (a child also has its
  own session id). Subagent tool calls carry their parent's ``session_id``
  (verified on Claude Code 2.1.292: a subagent's PreToolUse payload has the
  parent's ``session_id`` plus its own ``agent_id``), so they stay claimed.
  When the id changes in-process (``/clear``, a resume) the mod re-stamps at
  the next prompt; until then Python serves.

  The one exception is a **served call**: a tool call another session sent
  to this machine to run (remote tools). Its hook payload has ``session_id``
  ``served:<caller>``, never the host's, yet the host's ``tool.call`` chain —
  the mod's lanes included — runs it, and the mod cannot tell it from its own
  (Claude Code 2.1.292: the ``tool.call`` input has no field for it, its
  origin is core, no ``agentId``). So a ``served:`` payload is claimed when
  the pid stamp matches ``CLAUDE_PID``: the mod in this very process
  evaluated it, and Python serving it too would evaluate it twice. Without
  both pids it is served by Python (and evaluated twice, never lost).

A value without all four parts (an older mod's) is no claim.

Per environment because a machine may run both installs, and one install's mod
claiming a lane must never silence the other install's Python hook.

Which environment THIS install is comes from its manifest's ``name`` — the
one byte ``scripts/promote_export.py`` changes, and the same thing the mod
reads from ``$.plugin`` (spec §3.2): ``memhub-staging`` is staging, ``memhub``
is prod. An install whose manifest cannot be read has no environment and
never steps aside: Python keeps serving, which is always safe.

Only the four Rulebook lanes are claimable (``hook_entry.ROUTES`` marks them):
``pre``/``post``/``prompt``/``session`` → ``rulebook_hook.py`` of the same
name. The fire flush (``rulebook_hook.py flush``), capture, md_capture and the
harness are never claimed — they stay the shipping path (spec §3.3, §4.7).
A list from a mod of 0.118.0-0.120.3, still loaded in a session started on
one, also names ``harness``: that name is dropped like any unknown one, so its
Rulebook lanes stay claimed and the harness's Stop runs in Python.

Codex and Cursor never consult this: their hook configs call
``codex_hook_bridge.py`` / ``cursor_capture.cmd``, not ``hook_entry.py``, and
Claude Code's process env does not reach them. As a second line,
``claimed()`` refuses to skip for a payload ``claude_hook_guard`` identifies as
Codex or Cursor.

Stdlib only, and nothing is read when neither variable is set (the case on
every machine without the mod): the hot path pays one ``environ`` lookup.
When one is set, the stamp check is one string split per set variable, before the manifest
is read.
"""
from __future__ import annotations

import json
import os
import time
from typing import Mapping, Optional

LANES = ("pre", "post", "prompt", "session")
ENV_VARS = {"staging": "MEMHUB_MOD_LANES_STAGING", "prod": "MEMHUB_MOD_LANES_PROD"}
STAGING_NAME = "memhub-staging"
PROD_NAME = "memhub"
# Claude Code reads .claude-plugin/plugin.json; version_parity_test keeps the
# others' names equal to it.
_MANIFESTS = (".claude-plugin/plugin.json", "plugin.json")


def plugin_root() -> str:
    """The install this file belongs to (``scripts/`` sits inside it)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install_env(root: Optional[str] = None) -> Optional[str]:
    """``"staging"`` / ``"prod"`` for this install, or None when its manifest
    cannot be read or names neither."""
    root = root or plugin_root()
    for rel in _MANIFESTS:
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                name = json.load(f).get("name")
        except (OSError, ValueError, AttributeError):
            continue
        if name == STAGING_NAME:
            return "staging"
        if name == PROD_NAME:
            return "prod"
        return None
    return None


def parse(value: str) -> frozenset:
    """The lanes a list names: comma-separated, whitespace and case ignored,
    unknown names dropped. Takes the part after the stamp (see ``lanes_for``)."""
    return frozenset(p.strip().lower() for p in (value or "").split(",")) & frozenset(LANES)


SERVED_PREFIX = "served:"
_DIGITS = frozenset("0123456789")


def lanes_for(value: Optional[str], session_id: object,
              now: Optional[float] = None, pid: Optional[str] = None) -> frozenset:
    """The lanes ``value`` (``<session_id>:<expires_epoch_s>:<pid>:<lanes>``)
    claims for a payload of ``session_id``, at ``now`` (default
    ``time.time()``), in the hook process whose ``CLAUDE_PID`` is ``pid``.

    Empty unless the lease is live, the pids agree where both are known, and
    the stamp is the payload's session (or the payload is a served call and
    both pids are known and equal). See the module docstring."""
    if not value or not isinstance(session_id, str) or not session_id:
        return frozenset()
    parts = value.rsplit(":", 3)
    if len(parts) != 4:
        return frozenset()
    stamp, expires, claimer, lanes = parts
    if not expires or not _DIGITS.issuperset(expires):
        return frozenset()
    if (time.time() if now is None else now) >= int(expires):
        return frozenset()
    if claimer and pid and claimer != pid:
        return frozenset()
    if stamp != session_id:
        served_here = (session_id.startswith(SERVED_PREFIX)
                       and bool(claimer) and claimer == pid)
        if not served_here:
            return frozenset()
    return parse(lanes)


def _foreign_host(payload: object, environ: Mapping[str, str]) -> bool:
    try:
        import claude_hook_guard  # noqa: PLC0415 — beside this file, stdlib only
        return (claude_hook_guard.is_codex(payload, environ)
                or claude_hook_guard.is_cursor(payload, environ))
    except Exception:
        return True     # cannot tell → do not skip


def claimed(lane: Optional[str], payload: object = None,
            environ: Optional[Mapping[str, str]] = None,
            root: Optional[str] = None) -> bool:
    """True when this install's mod, in this very process and under a live
    lease, serves ``lane`` for the payload's session, and the Python hook for
    it should exit 0 silently. Any doubt answers False (Python serves)."""
    env = os.environ if environ is None else environ
    if not lane:
        return False
    values = [env.get(v) for v in ENV_VARS.values()]
    if not any(values):
        return False
    sid = payload.get("session_id") if isinstance(payload, dict) else None
    pid = env.get("CLAUDE_PID") or None
    # Cheap first: a value stamped for another session, process or time (or
    # none) cannot claim, whichever install this is, so no manifest is read.
    if not any(lane in lanes_for(v, sid, pid=pid) for v in values):
        return False
    which = install_env(root)
    if which is None or lane not in lanes_for(env.get(ENV_VARS[which]), sid, pid=pid):
        return False
    return not _foreign_host(payload, env)
