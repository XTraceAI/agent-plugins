"""Shared, credential-free reporting and process isolation for release checks."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

SPEC = importlib.util.spec_from_file_location(
    "production_compatibility", Path(__file__).with_name("check-production-compatibility.py"))
compat = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compat)


class NotVerified(Exception):
    """A prerequisite or a host capability is unavailable, never a pass."""


def require(value, message):
    compat.require(value, message)


def isolated_env(root):
    # No developer auth, saved sessions, GitHub token, or inherited plugin overrides.
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR") if k in os.environ}
    for name in ("home", "codex", "claude", "tmp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(root / "home"), CODEX_HOME=str(root / "codex"),
               CLAUDE_CONFIG_DIR=str(root / "claude"), TMPDIR=str(root / "tmp"),
               XDG_CONFIG_HOME=str(root / "home/.config"),
               PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", LANG="en_US.UTF-8")
    return env


def run(command, *, env, cwd, stdin=None, timeout=120):
    # Never stream candidate-controlled output into CI logs (it can contain secrets).
    with subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=True) as proc:
        try:
            out, err = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise compat.GateError("command exceeded its time budget") from None
    require(proc.returncode == 0, "host command failed; raw output withheld")
    return out


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def host_version(executable, env, cwd):
    version = run([executable, "--version"], env=env, cwd=cwd, timeout=20).strip()
    require(re.fullmatch(r"[A-Za-z0-9.() /_:+-]{1,100}", version), "unrecognized host version response")
    return version


def json_lines(text):
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except ValueError:
            pass
    return rows


class Report:
    def __init__(self, host, package, source_sha):
        require(re.fullmatch(r"[0-9a-f]{40}", source_sha), "source SHA must be immutable")
        self.package = package
        self.data = {"schema_version": 1, "host": host, "source_sha": source_sha,
                     "plugin_version": compat.package_version(package),
                     "package_sha256": compat.package_digest(package), "checks": {}}

    def check(self, name, fn):
        start = time.monotonic()
        try:
            value = fn()
            require(value is not False, "check returned a negative result")
            result = {"status": "passed"}
        except NotVerified as exc:
            value = None
            result = {"status": "not_verified", "reason": str(exc)}
        except compat.GateError as exc:
            value = None
            result = {"status": "failed", "reason": str(exc)}
        except Exception:
            value = None
            result = {"status": "failed", "reason": "unexpected check failure; raw output withheld"}
        result["seconds"] = round(time.monotonic() - start, 2)
        self.data["checks"][name] = result
        print(f"{name}: {result['status']}")
        return value

    def blocked(self, names, reason):
        for name in names:
            self.data["checks"][name] = {"status": "not_verified", "reason": reason}

    def finish(self, path):
        if not self.data["checks"]:
            self.blocked(["release_evidence"], "no release checks ran")
        self.check("package_unchanged", lambda: require(
            compat.package_digest(self.package) == self.data["package_sha256"],
            "the candidate package changed during verification"))
        self.data["ok"] = all(c["status"] == "passed" for c in self.data["checks"].values())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.data, indent=2) + "\n")
        return 0 if self.data["ok"] else 1
