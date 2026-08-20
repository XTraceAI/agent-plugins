#!/usr/bin/env python3
"""Install or remove MemHub's user-level Codex hooks bridge (stdlib only)."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
import shlex
import shutil
import stat
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
BRIDGE_SOURCE = PLUGIN_ROOT / "references" / "codex-hooks-bridge.json"
RUNNER_SOURCE = SCRIPT_DIR / "codex_hook_bridge.py"
_RUNNER_NAME = "memhub_hook_bridge.py"
_LEGACY_MARKERS = (
    "codex_flush.py",
    "directive_recall.py",
    "artifact_sync_reminder.py",
)


class SetupError(RuntimeError):
    pass


def _codex_home(value: str | None = None) -> Path:
    return Path(value or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def _load_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return {} if default is None else default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"{path} must contain a JSON object")
    return value


def _is_memhub_handler(handler: object) -> bool:
    if not isinstance(handler, dict):
        return False
    command = handler.get("command")
    if not isinstance(command, str):
        return False
    normalized = command.replace("\\", "/")
    return _RUNNER_NAME in normalized or (
        "xtrace-plugins/memhub" in normalized
        and any(marker in normalized for marker in _LEGACY_MARKERS)
    )


def _without_memhub(groups: object) -> list:
    if not isinstance(groups, list):
        raise SetupError("each hooks event must contain a list")
    kept = []
    for group in groups:
        if not isinstance(group, dict):
            kept.append(group)
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            kept.append(group)
            continue
        remaining = [handler for handler in handlers if not _is_memhub_handler(handler)]
        if remaining:
            kept.append({**group, "hooks": remaining})
    return kept


def merge_hooks(current: dict, bridge: dict) -> dict:
    merged = dict(current)
    hooks = current.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SetupError("hooks.json field 'hooks' must be an object")
    out_hooks = dict(hooks)
    bridge_hooks = bridge.get("hooks")
    if not isinstance(bridge_hooks, dict):
        raise SetupError("bundled bridge is malformed")
    for event, groups in bridge_hooks.items():
        out_hooks[event] = _without_memhub(out_hooks.get(event, [])) + groups
    merged["hooks"] = out_hooks
    return merged


def _bridge_for_home(bridge: dict, home: Path) -> dict:
    """Pin both platform commands to the requested Codex home."""
    materialized = json.loads(json.dumps(bridge))
    unix_placeholder = '"${CODEX_HOME:-$HOME/.codex}/' + _RUNNER_NAME + '"'
    unix_runner = shlex.quote(str(home / _RUNNER_NAME))
    for groups in materialized.get("hooks", {}).values():
        for group in groups:
            for handler in group.get("hooks", []):
                command = handler.get("command")
                if isinstance(command, str):
                    handler["command"] = command.replace(
                        unix_placeholder, unix_runner
                    )
                command = handler.get("commandWindows")
                if isinstance(command, str):
                    handler["commandWindows"] = command.replace(
                        "%CODEX_HOME%", str(home)
                    )
    return materialized


def remove_hooks(current: dict) -> dict:
    if "hooks" not in current:
        return current
    merged = dict(current)
    hooks = current.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SetupError("hooks.json field 'hooks' must be an object")
    cleaned = {}
    for event, groups in hooks.items():
        remaining = _without_memhub(groups)
        if remaining:
            cleaned[event] = remaining
    merged["hooks"] = cleaned
    return merged


def _write_atomic(path: Path, value: dict, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, mode if mode is not None else 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _copy_runner(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(RUNNER_SOURCE, tmp)
        os.chmod(tmp, 0o700)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _handler_count(doc: dict) -> int:
    return sum(
        1
        for groups in doc.get("hooks", {}).values()
        if isinstance(groups, list)
        for group in groups
        if isinstance(group, dict)
        for handler in group.get("hooks", [])
        if _is_memhub_handler(handler)
    )


def _handler_actions(doc: dict) -> Counter:
    actions = Counter()
    for event, groups in doc.get("hooks", {}).items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for handler in group.get("hooks", []):
                if not _is_memhub_handler(handler):
                    continue
                actions[(
                    event,
                    group.get("matcher"),
                    handler.get("command"),
                    handler.get("commandWindows"),
                )] += 1
    return actions


def install(home: Path) -> tuple[bool, int, Path | None]:
    hooks_path = home / "hooks.json"
    runner_path = home / _RUNNER_NAME
    current = _load_json(hooks_path)
    bridge = _bridge_for_home(_load_json(BRIDGE_SOURCE), home)
    merged = merge_hooks(current, bridge)
    runner_changed = not runner_path.exists() or (
        runner_path.read_bytes() != RUNNER_SOURCE.read_bytes()
    )
    changed = merged != current or runner_changed
    backup = None
    # Publish the runner before any hook can reference it. If this copy fails,
    # hooks.json remains byte-for-byte unchanged instead of pointing at a
    # missing or stale bridge.
    if runner_changed:
        _copy_runner(runner_path)
    if changed and hooks_path.exists() and merged != current:
        fd, backup_name = tempfile.mkstemp(
            prefix=f"hooks.json.memhub-backup-{time.strftime('%Y%m%d-%H%M%S')}-",
            dir=hooks_path.parent,
        )
        os.close(fd)
        backup = Path(backup_name)
        shutil.copy2(hooks_path, backup)
    if merged != current:
        old_mode = stat.S_IMODE(hooks_path.stat().st_mode) if hooks_path.exists() else 0o600
        _write_atomic(hooks_path, merged, old_mode)
    return changed, _handler_count(bridge), backup


def uninstall(home: Path) -> bool:
    hooks_path = home / "hooks.json"
    runner_path = home / _RUNNER_NAME
    current = _load_json(hooks_path)
    cleaned = remove_hooks(current)
    changed = cleaned != current or runner_path.exists()
    if cleaned != current:
        old_mode = stat.S_IMODE(hooks_path.stat().st_mode) if hooks_path.exists() else 0o600
        _write_atomic(hooks_path, cleaned, old_mode)
    runner_path.unlink(missing_ok=True)
    return changed


def status(home: Path) -> tuple[bool, int, int]:
    current = _load_json(home / "hooks.json")
    bridge = _bridge_for_home(_load_json(BRIDGE_SOURCE), home)
    expected = _handler_count(bridge)
    actual = _handler_count(current)
    runner = home / _RUNNER_NAME
    runner_ok = runner.exists() and runner.read_bytes() == RUNNER_SOURCE.read_bytes()
    return runner_ok and _handler_actions(current) == _handler_actions(bridge), actual, expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "status", "remove"), nargs="?", default="install")
    parser.add_argument("--codex-home")
    args = parser.parse_args()
    home = _codex_home(args.codex_home)
    try:
        if args.action == "install":
            changed, count, backup = install(home)
            print(f"MemHub Codex hooks: {'installed' if changed else 'already current'} ({count} handlers)")
            print(f"hooks: {home / 'hooks.json'}")
            if backup:
                print(f"backup: {backup}")
            print("next: open /hooks in Codex and trust the MemHub hooks")
            return 0
        if args.action == "remove":
            print("MemHub Codex hooks: " + ("removed" if uninstall(home) else "not installed"))
            return 0
        healthy, actual, expected = status(home)
        print(f"MemHub Codex hooks: {'OK' if healthy else 'NOT INSTALLED'} ({actual}/{expected} handlers)")
        return 0 if healthy else 1
    except SetupError as exc:
        print(f"MemHub Codex hooks: ERROR: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
