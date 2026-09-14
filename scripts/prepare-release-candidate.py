#!/usr/bin/env python3
"""Resolve actual host package and its previous tagged version, without moving tags."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from release_check_lib import compat, require


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def resolve(host, temp):
    sha = git("rev-parse", "HEAD")
    if host == "claude":
        entries = [p for p in json.loads(Path(".claude-plugin/marketplace.json").read_text())["plugins"]
                   if p["name"] == "memhub"]
        require(len(entries) == 1, "expected exactly one Claude marketplace entry")
        source = entries[0]["source"]
        require(source.get("source") == "git-subdir" and source.get("url") == "https://github.com/XTraceAI/agent-plugins.git"
                and source.get("path") == "plugins/memhub", "unexpected Claude source")
        sha, tag = source["sha"], source["ref"]
        require(re.fullmatch(r"[0-9a-f]{40}", sha) and re.fullmatch(r"memhub--v[0-9]+\.[0-9]+\.[0-9]+", tag),
                "Claude release must use immutable SHA and version tag")
        require(git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}") == sha, "Claude tag and SHA disagree")
    candidate = temp / "candidate"
    subprocess.run(["git", "worktree", "add", "--detach", str(candidate), sha], check=True, capture_output=True)
    version = tuple(map(int, compat.package_version(candidate / "plugins/memhub").split('.')))
    previous = []
    # Claude release tags can point to independent release commits, not ancestors
    # of the current pin. Version order, not ancestry, defines the upgrade path.
    for tag in git("tag", "--list", "memhub--v*").splitlines():
        match = re.fullmatch(r"memhub--v([0-9]+)\.([0-9]+)\.([0-9]+)", tag)
        if match and tuple(map(int, match.groups())) < version:
            previous.append((tuple(map(int, match.groups())), tag))
    require(previous, "no older tagged release exists for the upgrade check")
    baseline_tag = max(previous)[1]
    baseline_sha = git("rev-parse", f"refs/tags/{baseline_tag}^{{commit}}")
    baseline = temp / "previous"
    subprocess.run(["git", "worktree", "add", "--detach", str(baseline), baseline_sha], check=True, capture_output=True)
    require(compat.package_version(baseline / "plugins/memhub") == baseline_tag.removeprefix("memhub--v"),
            "baseline tag and package version disagree")
    return {"RELEASE_PACKAGE": str(candidate / "plugins/memhub"), "RELEASE_SHA": sha,
            "PREVIOUS_PACKAGE": str(baseline / "plugins/memhub"), "PREVIOUS_SHA": baseline_sha}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", choices=("codex", "claude", "cursor"), required=True)
    ap.add_argument("--temp", type=Path, required=True)
    args = ap.parse_args()
    args.temp.mkdir(parents=True, exist_ok=True)
    result = resolve(args.host, args.temp.resolve())
    with open(os.environ["GITHUB_ENV"], "a") as out:
        for key, value in result.items():
            require("\n" not in value and "\r" not in value, "unsafe workflow environment value")
            out.write(f"{key}={value}\n")
