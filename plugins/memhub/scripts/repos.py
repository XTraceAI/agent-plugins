#!/usr/bin/env python3
"""Manage which repositories MemHub is active in (``repo_allow.py``'s list).

    python3 repos.py list                 what is listed, and this checkout's state
    python3 repos.py add [<org/name>|.]   list a repository (`.`: the current checkout)
    python3 repos.py remove [<org/name>|.]
    python3 repos.py all                  delete the list: MemHub active everywhere
    python3 repos.py status [--json]      this checkout only, one line

The list is ``~/.config/memhub-plugin/repos.json`` (``$MEMHUB_REPOS_FILE``
moves it). No file means MemHub is active in every repository; a file means
it is dormant in every repository the file leaves out — no team rules, no
capture, no companion. ``add`` creates the file when there is none, which
turns every OTHER repository off: the command says so.

An entry is the checkout's identity, ``<org>/<name>`` from its ``origin``
remote (``XTraceAI/MemHub-Backend``), or the bare directory name of a
checkout with no remote. ``add .`` and ``remove .`` derive it from the
current directory (or ``--cwd``), so nobody has to spell it.

The file may also carry ``declined`` (ENG-1253): repositories the person
answered **No** to when the Claude Code status strip asked, at a session
start in an unlisted checkout, whether to enable MemHub there. A declined
repository is as dormant as any unlisted one and is never asked about again.
``list`` and ``status`` show it; ``add`` clears a decline (the repository is
then listed); ``remove`` of a declined repository clears the decline too, so
the strip asks again there.

Stdlib only, Python 3.9. Exit 0 on success, 2 on a usage error, 1 when the
file cannot be written.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import repo_allow  # noqa: E402


def _skill() -> str:
    return "/%s:repos" % repo_allow.plugin_name(HERE)


def _here(cwd: str):
    """(root, identity) of the checkout holding ``cwd``, or ("", "")."""
    root, gitdir = repo_allow.checkout(cwd)
    return (root, repo_allow.identity(root, gitdir)) if root else ("", "")


def _resolve(arg: str, cwd: str) -> str:
    """The identity an argument names: ``.`` (or nothing) is this checkout."""
    if arg in ("", "."):
        root, name = _here(cwd)
        if not root:
            sys.exit("repos: %s is not inside a Git checkout; name the repository "
                     "as <org>/<name> instead" % cwd)
        return name
    name = arg.strip().strip("/")
    if name.endswith(".git"):
        name = name[:-4]
    if not repo_allow.normalize(name) or "://" in name or ":" in name or name.startswith("-"):
        sys.exit("repos: %r is not a repository name; use <org>/<name> as the origin "
                 "remote spells it, or `.` for this checkout" % arg)
    return name


def cmd_list(args) -> int:
    listed = repo_allow.entries()
    root, mine = _here(args.cwd)
    if listed is None:
        print("MemHub is active in every repository (no %s)." % repo_allow.allowlist_path())
    elif not listed:
        print("MemHub is active in no repository: %s lists none." % repo_allow.allowlist_path())
    else:
        print("MemHub is active only in these repositories (%s):" % repo_allow.allowlist_path())
        for name in listed:
            print("  %s" % name)
    said_no = repo_allow.declined_entries() or []
    if said_no:
        print("Declined (asked at a session start, answered No; MemHub is inactive there "
              "and does not ask again):")
        for name in said_no:
            print("  %s" % name)
    if root:
        on = repo_allow.enabled(root)
        state = "enabled" if on else "declined" if repo_allow.is_declined(mine) else "NOT enabled"
        print("This checkout (%s) is %s." % (mine, state))
        if not on:
            print("Enable it: %s add ." % _skill())
    else:
        print("This directory is not inside a Git checkout.")
    return 0


def cmd_add(args) -> int:
    name = _resolve(args.repo, args.cwd)
    listed = repo_allow.entries()
    fresh = listed is None
    before = {repo_allow.normalize(r) for r in (listed or [])}
    if repo_allow.normalize(name) in before:
        print("%s is already listed." % name)
        return 0
    was_declined = repo_allow.is_declined(name)
    path = repo_allow.allow(name)
    print("Added %s to %s.%s" % (name, path, " Its decline is cleared." if was_declined else ""))
    if fresh:
        print("MemHub is now active ONLY in the listed repositories; every other "
              "repository is dormant (no rules, no capture, no companion). "
              "`%s all` makes it active everywhere again." % _skill())
    print("Open sessions pick this up at their next tool call; the Claude Code mod "
          "and companion at the next session start.")
    return 0


def cmd_remove(args) -> int:
    name = _resolve(args.repo, args.cwd)
    listed = repo_allow.entries()
    if listed is None:
        print("There is no %s: MemHub is active everywhere, and nothing is listed to remove. "
              "`%s add <org/name>` starts a list." % (repo_allow.FILE_NAME, _skill()))
        return 0
    key = repo_allow.normalize(name)
    kept = [r for r in listed if repo_allow.normalize(r) != key]
    if len(kept) == len(listed):
        said_no = repo_allow.declined_entries() or []
        still = [r for r in said_no if repo_allow.normalize(r) != key]
        if len(still) < len(said_no):
            repo_allow.write(listed, declined=still)
            print("%s is no longer declined. MemHub stays inactive there, and the Claude Code "
                  "status strip may ask again whether to enable it; `%s add` enables it now."
                  % (name, _skill()))
            return 0
        print("%s is not listed." % name)
        return 0
    repo_allow.write(kept)
    print("Removed %s. MemHub is now dormant there." % name)
    if not kept:
        print("The list is empty: MemHub is dormant in every repository. "
              "`%s all` enables it everywhere; `%s add .` enables it here." % (_skill(), _skill()))
    return 0


def cmd_all(args) -> int:
    if repo_allow.remove():
        print("Removed %s: MemHub is active in every repository." % repo_allow.allowlist_path())
    else:
        print("MemHub is already active in every repository (no %s)." % repo_allow.FILE_NAME)
    return 0


def cmd_status(args) -> int:
    info = repo_allow.status(args.cwd)
    if args.json:
        print(json.dumps(info))
        return 0
    if not info["restricted"]:
        print("active everywhere (no %s)" % repo_allow.FILE_NAME)
    elif not info["repo"]:
        print("inactive here: not inside a Git checkout (%s limits MemHub to listed repositories)"
              % info["path"])
    elif info["enabled"]:
        print("active: %s is listed" % info["repo"])
    elif info["declined"]:
        print("inactive: %s is declined in %s (answered No when asked; `%s add .` enables it)"
              % (info["repo"], info["path"], _skill()))
    else:
        print("inactive: %s is not listed in %s" % (info["repo"], info["path"]))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="repos.py", description=__doc__.splitlines()[0])
    ap.add_argument("--cwd", default=os.getcwd(), help="the checkout `.` means (default: cwd)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("list").set_defaults(fn=cmd_list)
    for name, fn in (("add", cmd_add), ("remove", cmd_remove)):
        p = sub.add_parser(name)
        p.add_argument("repo", nargs="?", default=".")
        p.set_defaults(fn=fn)
    sub.add_parser("all").set_defaults(fn=cmd_all)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if not args.cmd:
        return cmd_list(args)
    try:
        return args.fn(args)
    except OSError as exc:
        print("repos: could not write %s: %s" % (repo_allow.allowlist_path(), exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
