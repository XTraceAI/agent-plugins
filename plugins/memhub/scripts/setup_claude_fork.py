#!/usr/bin/env python3
"""Turn on Claude Code's fork agent for the harness (stdlib only).

The harness's Stop hook hands a flagged turn to a background fork of the
session (`harness_stop.fork_reason`). An interactive terminal session has the
fork agent by default; the desktop app, the Agent SDK and `claude -p` do not
unless `CLAUDE_CODE_FORK_SUBAGENT=1` is set, and there the launch fails with
"Agent type 'fork' not found" after the person was already told a rule was
being drafted (seen on desktop 2.1.284, 2026-10-01).

A plugin cannot ship settings, so `/memhub:onboard` writes this one key into
the person's own `settings.json`, because they ran onboard. Nothing here runs
from a hook.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_extract as hx  # noqa: E402
from setup_codex_hooks import SetupError, _load_json, _write_atomic  # noqa: E402

KEY = "CLAUDE_CODE_FORK_SUBAGENT"
_ON = ("1", "true", "yes", "on")


def _claude_home(value: str | None = None) -> Path:
    return Path(value or os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()


def _env_of(doc: dict, path: Path) -> dict:
    env = doc.get("env", {})
    if not isinstance(env, dict):
        raise SetupError(f"{path} field 'env' must be an object")
    return env


def _is_on(value: object) -> bool:
    return str(value).strip().lower() in _ON


def _settings(home: Path) -> Path:
    # A dotfiles-managed settings.json is often a symlink; edit its target
    # rather than replace the link with a regular file.
    return (home / "settings.json").resolve()


def _harness_on(env: dict) -> bool:
    # Claude Code applies settings.json `env` over the inherited environment,
    # and settings is where an install opts out, so a plain terminal run of this
    # script sees the harness on or off just as a session does.
    return hx.extract_enabled({**os.environ, **{k: str(v) for k, v in env.items()}})


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    fd, backup_name = tempfile.mkstemp(
        prefix=f"settings.json.memhub-backup-{time.strftime('%Y%m%d-%H%M%S')}-",
        dir=path.parent,
    )
    os.close(fd)
    backup = Path(backup_name)
    shutil.copy2(path, backup)
    return backup


def install(home: Path) -> tuple[str, Path | None]:
    """Returns (state, backup). `state` is one of:

    - "not needed": the harness is off, so nothing would launch a fork;
    - "already set": the key is on;
    - "left off": the key is present and off. That is the person's own choice
      and this never overrides it;
    - "set": written now.
    """
    path = _settings(home)
    doc = _load_json(path)
    env = _env_of(doc, path)
    if not _harness_on(env):
        return "not needed", None
    if KEY in env:
        return ("already set" if _is_on(env[KEY]) else "left off"), None
    old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    backup = _backup(path)
    _write_atomic(path, {**doc, "env": {**env, KEY: "1"}}, old_mode)
    return "set", backup


def uninstall(home: Path) -> tuple[bool, Path | None]:
    """(removed, backup). The file is backed up before it is rewritten."""
    path = _settings(home)
    doc = _load_json(path)
    env = _env_of(doc, path)
    if KEY not in env:
        return False, None
    cleaned = {k: v for k, v in doc.items() if k != "env"}
    rest = {k: v for k, v in env.items() if k != KEY}
    if rest:
        cleaned["env"] = rest
    mode = stat.S_IMODE(path.stat().st_mode)
    backup = _backup(path)
    _write_atomic(path, cleaned, mode)
    return True, backup


def status(home: Path) -> tuple[bool, str]:
    """(harness on, the key's state in settings.json: "on", "off" or "unset")."""
    path = _settings(home)
    env = _env_of(_load_json(path), path)
    state = "unset" if KEY not in env else ("on" if _is_on(env[KEY]) else "off")
    return _harness_on(env), state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "remove"), nargs="?", default="install")
    parser.add_argument("--claude-home")
    args = parser.parse_args()
    home = _claude_home(args.claude_home)
    path = _settings(home)
    try:
        if args.action == "install":
            state, backup = install(home)
            if state == "not needed":
                print(f"MemHub fork agent: not needed ({hx.FLAG} is off in the environment "
                      f"and in {path} env, so no rule drafts run)")
                return 0
            if state == "left off":
                print(f"MemHub fork agent: LEFT OFF — {KEY} is set off in {path}; "
                      "rule drafts will not run outside the terminal until it is 1")
                return 1
            print(f"MemHub fork agent: {state} ({KEY}=1)")
            print(f"settings: {path}")
            if backup:
                print(f"backup: {backup}")
            if state == "set":
                print("next: restart Claude Code; the setting is read when a session starts")
            return 0
        if args.action == "remove":
            removed, backup = uninstall(home)
            print("MemHub fork agent: " + ("removed" if removed else "not set"))
            if backup:
                print(f"backup: {backup}")
            return 0
        harness_on, state = status(home)
        if not harness_on:
            print(f"MemHub fork agent: not needed ({hx.FLAG} is off); {KEY} is {state}")
            return 0
        if state == "on":
            print(f"MemHub fork agent: OK ({KEY}=1 in {path})")
            return 0
        print(f"MemHub fork agent: NOT SET ({KEY} is {state} in {path}) — rule drafts "
              "run in the terminal only; the desktop app, the Agent SDK and claude -p need it")
        return 1
    except (SetupError, OSError) as exc:
        print(f"MemHub fork agent: ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
