#!/usr/bin/env python3
"""Setup and trampoline tests for the Codex user-hooks bridge (stdlib only)."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "memhub" / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


setup = _load("setup_codex_hooks")
bridge = _load("codex_hook_bridge")


def test_install_preserves_other_hooks_and_is_idempotent():
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        original = {
            "description": "mine",
            "hooks": {
                "PreToolUse": [{
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo mine"}],
                }],
                "SessionStart": [{
                    "hooks": [{"type": "command", "command": "echo start"}],
                }],
            },
        }
        (home / "hooks.json").write_text(json.dumps(original), encoding="utf-8")
        changed, expected, backup = setup.install(home)
        assert changed and expected == 6 and backup and backup.exists()
        installed = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        assert installed["description"] == "mine"
        assert installed["hooks"]["SessionStart"] == original["hooks"]["SessionStart"]
        assert installed["hooks"]["PreToolUse"][0] == original["hooks"]["PreToolUse"][0]
        assert (home / "memhub_hook_bridge.py").is_file()
        installed_text = json.dumps(installed)
        assert str(home) in installed_text
        assert "${CODEX_HOME" not in installed_text
        assert setup.status(home) == (True, 6, 6)

        again, count, second_backup = setup.install(home)
        assert not again and count == 6 and second_backup is None
        assert json.loads((home / "hooks.json").read_text(encoding="utf-8")) == installed
    print("PASS test_install_preserves_other_hooks_and_is_idempotent")


def test_rapid_reinstalls_keep_distinct_backups():
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        hooks = home / "hooks.json"
        hooks.write_text(json.dumps({"description": "first"}), encoding="utf-8")
        _, _, first_backup = setup.install(home)
        first_bytes = first_backup.read_bytes()

        doc = json.loads(hooks.read_text(encoding="utf-8"))
        doc["description"] = "second"
        doc["hooks"]["Stop"][0]["hooks"][0]["commandWindows"] = "py -3 broken.py"
        hooks.write_text(json.dumps(doc), encoding="utf-8")
        _, _, second_backup = setup.install(home)

        assert first_backup != second_backup
        assert first_backup.read_bytes() == first_bytes
        assert json.loads(second_backup.read_text(encoding="utf-8"))["description"] == "second"
    print("PASS test_rapid_reinstalls_keep_distinct_backups")


def test_install_replaces_legacy_bridge_without_duplicates():
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        legacy = {
            "hooks": {
                "Stop": [{"hooks": [{
                    "type": "command",
                    "command": "python ~/.codex/plugins/cache/xtrace-plugins/memhub/0.26/scripts/codex_flush.py",
                }]}],
                "PostToolUse": [{
                    "matcher": "Bash",
                    "hooks": [
                        {"type": "command", "command": "echo user"},
                        {"type": "command", "command": (
                            "run ~/.codex/plugins/cache/xtrace-plugins/memhub/"
                            "0.26/scripts/codex_flush.py"
                        )},
                    ],
                }],
            }
        }
        (home / "hooks.json").write_text(json.dumps(legacy), encoding="utf-8")
        setup.install(home)
        installed = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        text = json.dumps(installed)
        assert "plugins/cache/xtrace-plugins" not in text
        assert "0.26/scripts/codex_flush.py" not in text
        assert "echo user" in text
        assert setup.status(home) == (True, 6, 6)
    print("PASS test_install_replaces_legacy_bridge_without_duplicates")


def test_remove_preserves_unrelated_hooks():
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        setup.install(home)
        doc = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        doc["hooks"]["Stop"][0]["hooks"].append(
            {"type": "command", "command": "echo keep"}
        )
        (home / "hooks.json").write_text(json.dumps(doc), encoding="utf-8")
        assert setup.uninstall(home)
        cleaned = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        assert "echo keep" in json.dumps(cleaned)
        assert "memhub_hook_bridge.py" not in json.dumps(cleaned)
        assert not (home / "memhub_hook_bridge.py").exists()
    print("PASS test_remove_preserves_unrelated_hooks")


def test_remove_from_empty_home_is_a_noop():
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        assert not setup.uninstall(home)
        assert not (home / "hooks.json").exists()
    print("PASS test_remove_from_empty_home_is_a_noop")


def test_malformed_existing_file_is_never_overwritten():
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        path = home / "hooks.json"
        path.write_text("{broken", encoding="utf-8")
        try:
            setup.install(home)
        except setup.SetupError:
            pass
        else:
            raise AssertionError("malformed hooks.json should stop setup")
        assert path.read_text(encoding="utf-8") == "{broken"
    print("PASS test_malformed_existing_file_is_never_overwritten")


def test_runner_selects_latest_known_install():
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        base = home / "plugins" / "cache" / "xtrace-plugins" / "memhub"
        for version in ("0.9.0", "0.10.0", "0.10.0+codex.local-2"):
            scripts = base / version / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "codex_flush.py").write_text("", encoding="utf-8")
        old = os.environ.get("CODEX_HOME")
        try:
            os.environ["CODEX_HOME"] = str(home)
            assert bridge.resolve_plugin_root() == base / "0.10.0+codex.local-2"
        finally:
            if old is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = old
    print("PASS test_runner_selects_latest_known_install")


def test_runner_relays_directive_and_artifact_context():
    with tempfile.TemporaryDirectory() as raw:
        plugin = Path(raw)
        scripts = plugin / "scripts"
        scripts.mkdir()
        (scripts / "codex_flush.py").write_text("", encoding="utf-8")
        (scripts / "directive_prefilter.py").write_text(
            "import sys; sys.stdin.read(); raise SystemExit(0)\n", encoding="utf-8"
        )
        (scripts / "directive_recall.py").write_text(
            "import json,sys; json.load(sys.stdin); "
            "print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse',"
            "'additionalContext':'proof'}}))\n",
            encoding="utf-8",
        )
        (scripts / "artifact_sync_reminder.py").write_text(
            "import json,sys; sys.stdin.read(); "
            "print(json.dumps({'hookSpecificOutput':{'hookEventName':'PostToolUse',"
            "'additionalContext':'version the linked artifact'}}))\n",
            encoding="utf-8",
        )
        env = {**os.environ, "MEMHUB_PLUGIN_ROOT": str(plugin)}
        payload = json.dumps({
            "tool_name": "Bash", "tool_input": {"command": "touch x"}
        }).encode()
        directive = subprocess.run(
            [sys.executable, str(SCRIPTS / "codex_hook_bridge.py"), "directive-pre"],
            input=payload, capture_output=True, env=env, check=True,
        )
        assert json.loads(directive.stdout)["hookSpecificOutput"]["additionalContext"] == "proof"
        artifact = subprocess.run(
            [sys.executable, str(SCRIPTS / "codex_hook_bridge.py"), "artifact-sync"],
            input=payload, capture_output=True, env=env, check=True,
        )
        result = json.loads(artifact.stdout)["hookSpecificOutput"]
        assert result == {
            "hookEventName": "PostToolUse",
            "additionalContext": "version the linked artifact",
        }
    print("PASS test_runner_relays_directive_and_artifact_context")


def test_status_checks_materialized_windows_commands():
    with tempfile.TemporaryDirectory() as raw:
        home = Path(raw)
        setup.install(home)
        doc = json.loads((home / "hooks.json").read_text(encoding="utf-8"))
        doc["hooks"]["Stop"][0]["hooks"][0]["commandWindows"] = "py -3 broken.py"
        (home / "hooks.json").write_text(json.dumps(doc), encoding="utf-8")
        healthy, actual, expected = setup.status(home)
        assert not healthy and actual == expected == 6
    print("PASS test_status_checks_materialized_windows_commands")


def test_every_shell_alias_uses_the_directive_prefilter():
    with tempfile.TemporaryDirectory() as raw:
        plugin = Path(raw)
        scripts = plugin / "scripts"
        scripts.mkdir()
        (scripts / "codex_flush.py").write_text("", encoding="utf-8")
        # Read-only shell calls must stop at this non-zero gate. If an alias
        # bypasses it, directive_recall's proof output exposes the regression.
        (scripts / "directive_prefilter.py").write_text(
            "import sys; sys.stdin.read(); raise SystemExit(1)\n", encoding="utf-8"
        )
        (scripts / "directive_recall.py").write_text(
            "print('SHOULD_NOT_RUN')\n", encoding="utf-8"
        )
        env = {**os.environ, "MEMHUB_PLUGIN_ROOT": str(plugin)}
        for tool_name in ("Bash", "shell", "local_shell"):
            payload = json.dumps({
                "tool_name": tool_name, "tool_input": {"command": "ls"}
            }).encode()
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "codex_hook_bridge.py"),
                 "directive-pre"],
                input=payload, capture_output=True, env=env, check=True,
            )
            assert result.stdout == b"", tool_name
    print("PASS test_every_shell_alias_uses_the_directive_prefilter")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
