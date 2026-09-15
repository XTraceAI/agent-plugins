#!/usr/bin/env python3
"""Exercise native install and previous-version upgrade in disposable host homes.

Uses a local marketplace containing the unmodified selected package. Published
Claude SHA/tag resolution is checked separately by production-compatibility.yml.
Cursor's CLI can load a local package but cannot install/update a single plugin;
that marketplace lifecycle remains explicitly NOT VERIFIED, never inferred from
--plugin-dir or a successful marketplace re-index.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

from release_check_lib import NotVerified, Report, compat, host_version, isolated_env, require, run


def marketplace(root, package, host):
    target = root / "marketplace"
    plugin = target / "plugins/memhub"
    if plugin.exists():
        shutil.rmtree(plugin)
    shutil.copytree(package, plugin, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    folder = ".agents/plugins" if host == "codex" else ".claude-plugin"
    catalog = target / folder / "marketplace.json"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    source = {"source": "local", "path": "./plugins/memhub"} if host == "codex" else "./plugins/memhub"
    entry = {"name": "memhub", "source": source}
    if host == "codex":
        entry["policy"] = {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}
    catalog.write_text(json.dumps({"name": "xtrace-plugins", "owner": {"name": "XTrace"},
                                   "plugins": [entry]}))
    return target


def native_install(root, env, package, host, executable, update=False):
    if host == "cursor":
        raise NotVerified("Cursor marketplace install/update requires a desktop/account runner; local loading is tested separately")
    catalog = marketplace(root, package, host)
    prefix = [executable, "plugin"]
    if not update:
        run(prefix + ["marketplace", "add", str(catalog)], env=env, cwd=root)
    if host == "codex":
        out = run(prefix + ["add", "memhub@xtrace-plugins", "--json"], env=env, cwd=root)
        installed = Path(json.loads(out)["installedPath"])
    else:
        run(prefix + ["update" if update else "install", "memhub@xtrace-plugins"], env=env, cwd=root)
        listing = json.loads(run(prefix + ["list", "--json"], env=env, cwd=root))
        require(isinstance(listing, list), "Claude plugin inventory is not a list")
        matches = [p for p in listing if p.get("id") == "memhub@xtrace-plugins"]
        require(len(matches) == 1, "Claude did not register exactly one MemHub install")
        installed = Path(matches[0]["installPath"])
    require(installed.resolve().is_relative_to(root.resolve()), "installed package escaped the disposable home")
    require(compat.package_version(installed) == compat.package_version(package), "installed version differs from candidate")
    require(compat.package_digest(installed) == compat.package_digest(package), "installed bytes differ from candidate")
    return installed


def lifecycle(host, candidate, previous, executable, report):
    with tempfile.TemporaryDirectory(prefix="memhub-install-") as raw:
        root = Path(raw)
        env = isolated_env(root)
        report.data["host_version"] = report.check("host_cli", lambda: host_version(executable, env, root))
        report.check("fresh_install", lambda: native_install(root, env, candidate, host, executable))
    if previous is None:
        report.blocked(["upgrade"], "no previous immutable release was selected")
        return
    with tempfile.TemporaryDirectory(prefix="memhub-upgrade-") as raw:
        root = Path(raw)
        env = isolated_env(root)

        def upgrade():
            old_version = compat.package_version(previous)
            new_version = compat.package_version(candidate)
            report.data["previous_plugin_version"] = old_version
            report.data["previous_package_sha256"] = compat.package_digest(previous)
            require(tuple(map(int, old_version.split('.'))) < tuple(map(int, new_version.split('.'))),
                    "upgrade baseline must be an older release")
            old = native_install(root, env, previous, host, executable)
            if host == "codex":
                run([sys.executable, str(old / "scripts/setup_codex_hooks.py"), "install"], env=env, cwd=root)
            installed = native_install(root, env, candidate, host, executable, update=True)
            if host == "codex":
                # The existing bridge must resolve the updated cache, without reinstalling it.
                out = run([sys.executable, "-c", "import runpy; d=runpy.run_path(" +
                           repr(str(Path(env['CODEX_HOME']) / 'memhub_hook_bridge.py')) +
                           "); print(d['resolve_plugin_root']())"], env=env, cwd=root)
                require(Path(out.strip()).resolve() == installed.resolve(), "Codex bridge still selects the previous plugin")
        report.check("upgrade", upgrade)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", choices=("codex", "claude", "cursor"), required=True)
    ap.add_argument("--plugin-root", type=Path, required=True)
    ap.add_argument("--previous-root", type=Path)
    ap.add_argument("--executable", required=True)
    ap.add_argument("--source-sha", required=True)
    ap.add_argument("--report", type=Path, required=True)
    args = ap.parse_args()
    report = Report(args.host, args.plugin_root.resolve(), args.source_sha)
    lifecycle(args.host, args.plugin_root.resolve(), args.previous_root, args.executable, report)
    return report.finish(args.report)


if __name__ == "__main__":
    raise SystemExit(main())
