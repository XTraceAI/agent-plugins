#!/usr/bin/env python3
"""Find a repo's documents worth keeping in its brain, and upload the chosen ones.

``/memhub:onboard`` stocks a new repo brain with what the repo already knows.
This script is the repo-agnostic half of that: it assumes NO directory layout
(``docs/`` is one team's convention), so it lists every markdown document the
repo tracks, grouped by folder, and scores each from its own name and content.
The skill shows the folders to the user and uploads what they pick.

  scan   [--root DIR] [--out FILE]   list candidates by folder; write the full
                                     list as JSON (the manifest ``upload`` takes)
  upload --manifest FILE [--only-folder F ...] [--path P ...] [--min-score N]
                                     save each selected entry via save_artifact.py

Names and types come from ``md_capture_flush.derive_name`` / ``derive_type`` —
the functions automatic capture uses — so a document uploaded here and later
edited by an agent is ONE artifact lineage, not two.

Stdlib only; ``upload`` shells out to ``save_artifact.py`` (which needs ``uv``),
one file at a time, and reports every failure by path instead of stopping.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from md_capture_flush import derive_name, derive_type  # noqa: E402
from room_map import git_env, repo_root  # noqa: E402
from spec_owns import DEFAULT_SPEC_DIR, safe_spec_dir  # noqa: E402

_SAVE_ARTIFACT = Path(__file__).resolve().parent / "save_artifact.py"

DOC_SUFFIXES = (".md", ".mdx", ".markdown")
MIN_BYTES = 400            # below this a document is a stub or a redirect
MAX_BYTES = 2_000_000      # the same ceiling automatic capture uses

# Directories that hold other people's documents, or generated ones.
SKIP_DIRS = {
    "node_modules", "vendor", "vendors", "third_party", "third-party", "dist",
    "build", "target", "out", ".git", ".venv", "venv", "site-packages",
    "__pycache__", ".next", ".tox", "coverage", "Pods",
}
# Files that are not knowledge about the system.
SKIP_NAMES = {
    "changelog", "changes", "history", "license", "licence", "notice",
    "code_of_conduct", "security", "authors", "contributors", "codeowners",
    "pull_request_template", "issue_template",
}
# Instructions addressed to a coding agent. These become RULES through
# /memhub:start-rulebook; an artifact copy would be read once, like the file is.
AGENT_FILES = {"claude.md", "agents.md", "gemini.md", "memory.md", "copilot-instructions.md"}
AGENT_DIRS = {".claude", ".cursor", ".codex", ".agents"}

# Words that mark a document as design knowledge, in a name or a title.
_STRONG = re.compile(
    r"spec|design|architect|adr\b|rfc|proposal|decision|runbook|playbook|"
    r"protocol|schema|contract|roadmap|postmortem|post-mortem|incident", re.I)
_USEFUL = re.compile(
    r"guide|handbook|howto|how-to|overview|contributing|develop|onboard|"
    r"deploy|release|migration|testing|api|plan|faq|troubleshoot|concept", re.I)
_SHELVED = re.compile(r"(^|/)(archive[ds]?|retired|deprecated|obsolete|old|legacy|attic)(/|$)", re.I)


def _tracked(root: Path) -> list[Path]:
    """Documents the repo tracks; every document under ``root`` when it is not
    a git repo (or git cannot be run)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"], capture_output=True,
            timeout=30, env=git_env())
        if proc.returncode == 0:
            names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]
            return [root / n for n in names if n.lower().endswith(DOC_SUFFIXES)]
    except (OSError, subprocess.SubprocessError):
        pass
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        found += [Path(dirpath) / f for f in filenames if f.lower().endswith(DOC_SUFFIXES)]
    return found


def _skip_reason(rel: Path) -> str | None:
    parts = [p.lower() for p in rel.parts]
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return "vendored or generated directory"
    if any(p in AGENT_DIRS for p in parts[:-1]) or parts[-1] in AGENT_FILES:
        return "agent instructions (rules come from /memhub:start-rulebook)"
    if rel.stem.lower().replace("-", "_") in SKIP_NAMES:
        return "not system knowledge"
    return None


def _readme_links(root: Path) -> set[str]:
    """Repo-relative paths the root README links to — a team's own statement
    of which documents matter."""
    for name in ("README.md", "readme.md", "Readme.md", "README.mdx"):
        p = root / name
        if p.is_file():
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return set()
            out = set()
            for target in re.findall(r"\]\(([^)#\s]+)", text):
                if "://" not in target:
                    out.add(Path(target.lstrip("./")).as_posix())
            return out
    return set()


def _score(rel: Path, title: str, text: str, linked: bool) -> tuple[int, list[str]]:
    why: list[str] = []
    score = 0
    hay = f"{rel.as_posix()} {title}"
    if _STRONG.search(hay):
        score += 3; why.append("design/spec wording")
    elif _USEFUL.search(hay):
        score += 2; why.append("guide wording")
    if len(rel.parts) == 1 and rel.stem.lower() == "readme":
        score += 3; why.append("root README")
    if linked:
        score += 2; why.append("linked from README")
    headings = len(re.findall(r"^#{1,4}\s+\S", text, re.M))
    if headings >= 4:
        score += 1; why.append(f"{headings} headings")
    if len(text) >= 6000:
        score += 1; why.append("substantial")
    # A folder a team moved documents INTO to get them out of the way. Still
    # listed — the user may want the history — but never "likely important".
    if _SHELVED.search("/".join(rel.parts[:-1])):
        score = min(score, 2); why.append("shelved folder")
    return score, why


def scan(root: Path) -> dict:
    root = root.resolve()
    spec_dir = safe_spec_dir(os.environ.get("MEMHUB_SPEC_DIR", DEFAULT_SPEC_DIR)) or DEFAULT_SPEC_DIR
    linked = _readme_links(root)
    docs, skipped = [], {}
    for p in sorted(_tracked(root)):
        try:
            rel = p.relative_to(root)
            size = p.stat().st_size
        except (OSError, ValueError):
            continue
        reason = _skip_reason(rel)
        if reason is None and size < MIN_BYTES:
            reason = "stub"
        if reason is None and size > MAX_BYTES:
            reason = "too large"
        if reason is not None:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped["unreadable"] = skipped.get("unreadable", 0) + 1
            continue
        name = derive_name(p, text, root)
        score, why = _score(rel, name, text, rel.as_posix() in linked)
        docs.append({
            "path": rel.as_posix(),
            "folder": rel.parent.as_posix() if len(rel.parts) > 1 else ".",
            "bytes": size,
            "name": name,
            "type": derive_type(p, text, name),
            "score": score,
            "why": why,
            "in_spec_dir": rel.as_posix().startswith(spec_dir + "/"),
        })
    return {"root": str(root), "spec_dir": spec_dir, "docs": docs, "skipped": skipped}


def cmd_scan(args) -> int:
    root = Path(args.root)
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2
    top = repo_root(root)
    result = scan(top if top is not None else root)
    docs = result["docs"]
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"root     : {result['root']}")
    print(f"documents: {len(docs)}   (skipped: "
          + (", ".join(f"{n} {why}" for why, n in sorted(result['skipped'].items())) or "none") + ")")
    if not docs:
        return 0
    folders: dict[str, list[dict]] = {}
    for d in docs:
        folders.setdefault(d["folder"], []).append(d)
    print("-" * 72)
    # Folders that hold the most design knowledge first.
    for folder, items in sorted(folders.items(),
                                key=lambda kv: -sum(i["score"] for i in kv[1])):
        likely = sum(1 for i in items if i["score"] >= 3)
        kb = sum(i["bytes"] for i in items) // 1024
        mirror = "   [spec dir: may already be mirrored]" if items[0]["in_spec_dir"] else ""
        print(f"{folder}/   {len(items)} docs, {likely} likely important, {kb} KB{mirror}")
        for i in sorted(items, key=lambda i: -i["score"])[: args.per_folder]:
            print(f"    {i['score']:>2}  {Path(i['path']).name}  — {i['name'][:70]}")
        if len(items) > args.per_folder:
            print(f"        … +{len(items) - args.per_folder} more (see --out)")
    if args.out:
        print("-" * 72)
        print(f"manifest : {args.out}")
    return 0


def _selected(docs: list[dict], args) -> list[dict]:
    folders = {f.rstrip("/") or "." for f in (args.only_folder or [])}
    paths = set(args.path or [])
    out = []
    for d in docs:
        if paths or folders:
            in_folder = any(d["folder"] == f or d["folder"].startswith(f + "/") for f in folders)
            if not (d["path"] in paths or in_folder):
                continue
        # An explicitly named path is the user's choice; the score floor only
        # thins out a folder.
        if d["path"] not in paths and d["score"] < args.min_score:
            continue
        if d["path"] in set(args.exclude or []):
            continue
        skip_folders = {f.rstrip("/") or "." for f in (args.exclude_folder or [])}
        if any(d["folder"] == f or d["folder"].startswith(f + "/") for f in skip_folders):
            continue
        out.append(d)
    return out


def cmd_upload(args) -> int:
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        root, docs = Path(manifest["root"]), manifest["docs"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"ERROR: cannot read manifest {args.manifest}: {exc}", file=sys.stderr)
        return 2
    chosen = _selected(docs, args)
    if not chosen:
        print("ERROR: the selection matched no documents; nothing was uploaded",
              file=sys.stderr)
        return 2
    print(f"uploading {len(chosen)} document(s) from {root}")
    if args.dry_run:
        for d in chosen:
            print(f"  [dry-run] {d['path']}  →  {d['name']}  ({d['type']})")
        return 0
    failed: list[tuple[str, str]] = []
    for n, d in enumerate(chosen, 1):
        tags = args.tags or _folder_tags(d)
        cmd = ["uv", "run", "--with", "mcp<2", "python", str(_SAVE_ARTIFACT),
               "--file", str(root / d["path"]), "--name", d["name"],
               "--type", d["type"], "--tags", tags]
        if args.url:
            cmd += ["--url", args.url]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            err = "" if proc.returncode == 0 else (
                (proc.stderr.strip().splitlines() or proc.stdout.strip().splitlines()
                 or [f"exit {proc.returncode}"])[-1])
        except (OSError, subprocess.SubprocessError) as exc:
            err = f"{type(exc).__name__}: {exc}"
        if err:
            failed.append((d["path"], err))
            print(f"  [{n}/{len(chosen)}] FAILED  {d['path']}: {err}")
        else:
            print(f"  [{n}/{len(chosen)}] saved   {d['path']}")
    print("-" * 72)
    print(f"saved {len(chosen) - len(failed)} of {len(chosen)}")
    for path, err in failed:
        print(f"FAILED: {path}: {err}", file=sys.stderr)
    return 1 if failed else 0


def _folder_tags(d: dict) -> str:
    """Tags from what the document IS and where it lives — never invented
    topic words. The server normalises them to snake_case."""
    tags = [d["type"]]
    if d["folder"] != ".":
        tags.append(d["folder"].split("/")[-1])
    return ",".join(dict.fromkeys(t for t in tags if t))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("scan", help="list candidate documents by folder")
    sp.add_argument("--root", default=".")
    sp.add_argument("--out", help="write the full candidate list (the upload manifest) here")
    sp.add_argument("--per-folder", type=int, default=5, help="rows printed per folder")
    sp.set_defaults(func=cmd_scan)
    up = sub.add_parser("upload", help="upload the selected documents")
    up.add_argument("--manifest", required=True)
    up.add_argument("--only-folder", action="append",
                    help="a folder to upload (repeatable; includes subfolders); '.' is the repo root")
    up.add_argument("--path", action="append", help="one document to upload (repeatable)")
    up.add_argument("--exclude", action="append", help="one document to leave out (repeatable)")
    up.add_argument("--exclude-folder", action="append",
                    help="a folder to leave out, subfolders included (repeatable)")
    up.add_argument("--min-score", type=int, default=0,
                    help="within the chosen folders, skip documents scoring below this")
    up.add_argument("--tags", default=None, help="tags for every upload (default: type + folder)")
    up.add_argument("--url", default=None)
    up.add_argument("--dry-run", action="store_true")
    up.set_defaults(func=cmd_upload)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
