#!/usr/bin/env python3
"""Maintain `.claude/artifact-map.json` — the repo-local index of which source
files a canonical artifact (usually a spec) governs.

The artifact-sync PostToolUse hook reads this map to remind the agent to
VERSION the linked artifact when it edits a mapped file. Written by `/memhub:spec`
at init/revise time so the index is a byproduct of spec-driven development
rather than a second thing to maintain by hand.

    # link (or re-link) an artifact to the files it governs
    python3 artifact_map.py add --artifact-id <id> --brain-id <id> \\
        --name "Spec: Retry policy" --glob "app/retry.py|app/**/backoff.py"

    # what does this repo map, and what governs a given file?
    python3 artifact_map.py list [--for app/retry.py]

`add` is idempotent per artifact id: an existing link with the same
`artifact_id` is replaced, so re-running after a spec revision just refreshes
the globs. Paths are repo-relative POSIX; `--glob` accepts `*`, `**`, `{a,b}`
braces, and `|`-separated alternatives (same semantics as the hook).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from artifact_sync_reminder import MAP_RELPATH, _matches  # noqa: E402


def _repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit("not a git repository — the artifact map is repo-local")
    return Path(out)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "links": []}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        sys.exit(f"{path} is unreadable ({exc}) — fix or delete it before writing")
    if not isinstance(data, dict) or not isinstance(data.get("links"), list):
        sys.exit(f"{path} has no `links` array — fix or delete it before writing")
    return data


def cmd_add(args: argparse.Namespace) -> int:
    path = _repo_root() / MAP_RELPATH
    data = _load(path)
    link = {
        "glob": args.glob,
        "brain_id": args.brain_id,
        "artifact_id": args.artifact_id,
        "artifact_name": args.name,
    }
    links = [l for l in data["links"] if l.get("artifact_id") != args.artifact_id]
    replaced = len(links) != len(data["links"])
    links.append(link)
    data["links"] = links
    data.setdefault("version", 1)

    path.parent.mkdir(parents=True, exist_ok=True)
    # ensure_ascii=False: artifact names carry em-dashes/arrows and the map is
    # read by humans in diffs.
    body = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    # Atomic: a half-written map is unreadable, and _load refuses to write over
    # an unreadable map — so an interrupted write would strand every later
    # `add` behind a manual repair. Temp sibling + os.replace is atomic within
    # the same filesystem.
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    verb = "updated" if replaced else "linked"
    print(f'{verb} "{args.name}" -> {args.glob} in {MAP_RELPATH}')
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    data = _load(_repo_root() / MAP_RELPATH)
    links = data["links"]
    if args.for_path:
        links = [l for l in links if _matches(str(l.get("glob", "")), args.for_path)]
        if not links:
            print(f"{args.for_path} is not linked to any artifact")
            return 0
    if not links:
        print("no artifact links in this repo")
        return 0
    for link in links:
        print(f'{link.get("artifact_name", "(unnamed)")}')
        print(f'  glob:     {link.get("glob")}')
        print(f'  artifact: {link.get("artifact_id")}')
        print(f'  brain:    {link.get("brain_id")}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="link an artifact to the files it governs")
    add.add_argument("--artifact-id", required=True)
    add.add_argument("--brain-id", required=True)
    add.add_argument("--name", required=True, help="the artifact's exact name")
    add.add_argument("--glob", required=True, help="repo-relative glob(s), `|`-separated")
    add.set_defaults(func=cmd_add)

    listing = sub.add_parser("list", help="show this repo's artifact links")
    listing.add_argument("--for", dest="for_path", help="repo-relative path to look up")
    listing.set_defaults(func=cmd_list)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
