#!/usr/bin/env python3
"""Which local sessions plausibly wrote the code in a pull request?

    find_sessions.py --files-from <path> --branch <head-ref> [--base <base-ref>]
                     [--sha <oid>]... [--created-at <iso>] [--host all|claude|codex|cursor]
                     [--limit 200] [--max-candidates 10] [--json]

Prints ranked candidates as JSON: paths, branches, shas and counts — and
**never transcript content**. A session transcript can exceed a million
tokens, and this output goes straight into model context, so nothing that is
not evidence leaves this script.

Stdlib plus the memhub plugin's own readers, the same machinery
`skills/rules-from-sessions/scripts/mine_sessions.py` uses. Candidates are
candidates: the skill shows them to the user and links only what is approved.

Repo-relative matching, not absolute paths. A worktree's absolute prefix
differs from the PR's file list, and that mismatch is the whole reason this
feature exists — so an edited path counts when it ENDS with one of the PR's
paths on a segment boundary.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from pathlib import Path

# Hard caps per session — a rollout can be enormous and this runs over up to
# --limit of them.
MAX_RECORDS = 20_000
MAX_BYTES = 64 * 1024 * 1024
MAX_TEXT_SCAN = 256 * 1024      # per tool result, for sha hunting
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
WINDOW_DAYS = 30

_SHA_RX = re.compile(r"\b[0-9a-f]{7,40}\b")
_APPLY_PATCH_PATH = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (.+)")
_GIT_PATHS = re.compile(r"(?:^|[;&|])\s*git\s+(?:add|commit)\b([^;&|\n]*)")
_BRANCH_CMD = re.compile(
    r"(?:^|[;&|])\s*git\s+(?:checkout|switch)\s+(?:-b\s+|-B\s+)?"
    r"(?:'([^']+)'|\"([^\"]+)\"|([^\s;&|'\"-][^\s;&|]*))")


def _plugin_scripts() -> str:
    """The installed plugin's scripts/ dir — the readers live there."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.environ.get("MEMHUB_PLUGIN_SCRIPTS"),
        os.path.join(os.environ.get("CLAUDE_PLUGIN_ROOT", ""), "scripts"),
        # shipped inside the plugin: skills/<skill>/scripts -> plugin scripts
        os.path.normpath(os.path.join(here, "..", "..", "..", "scripts")),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(candidate, "capture.py")):
            return candidate
    found = (glob.glob(os.path.expanduser(
                 "~/.claude/plugins/cache/*/memhub*/*/scripts/capture.py"))
             + glob.glob(os.path.expanduser(
                 "~/.claude/plugins/*/plugins/memhub*/scripts/capture.py"))
             + glob.glob(os.path.expanduser(
                 "~/.codex/plugins/*/memhub*/scripts/capture.py")))
    if not found:
        sys.exit("memhub plugin scripts not found; set MEMHUB_PLUGIN_SCRIPTS=<plugin>/scripts")
    return os.path.dirname(max(found, key=os.path.getmtime))


sys.path.insert(0, _plugin_scripts())
import pr_link  # noqa: E402
import readers  # noqa: E402


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip().lstrip("./")


def _matches_pr_file(edited: str, pr_files: dict[str, str]) -> str | None:
    """The PR file this edited path is, matched by suffix on a / boundary.

    Suffix-matching rather than resolving absolutes: the session's checkout is
    a different directory from the PR's, and often a different worktree.
    """
    edited = _norm(edited)
    if not edited:
        return None
    for key, original in pr_files.items():
        if edited == key or edited.endswith("/" + key):
            return original
    return None


def _tool_calls(records):
    """``(tool, input, result_text, result_is_pr_metadata)`` tuples, bounded.

    A result is paired back to the call that produced it (by ``tool_use_id``,
    the way ``mine_sessions.py`` does) for one reason: a commit sha printed by
    a command that was ASKING GitHub about the pull request is not evidence
    that this session wrote it. The skill's own step 2 runs `gh pr view <n>
    --json commits`, so without this the session running the scan finds the
    PR's shas in its own transcript and ranks itself top on "proof".
    """
    seen = 0
    metadata_calls: dict[str, bool] = {}
    for record in records:
        if seen >= MAX_RECORDS:
            return
        message = record.get("message") if isinstance(record, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            seen += 1
            if seen >= MAX_RECORDS:
                return
            if block.get("type") == "tool_use":
                payload = block.get("input") or {}
                name = block.get("name") or ""
                call_id = block.get("id")
                if isinstance(call_id, str):
                    if len(metadata_calls) >= MAX_RECORDS:
                        metadata_calls.clear()
                    # `touches_github` is exactly the question: did this call
                    # ask GitHub about a pull request? Its output is metadata.
                    # `git log` and `git push` do NOT touch GitHub, so the shas
                    # they print still count.
                    metadata_calls[call_id] = pr_link.touches_github(name, payload)
                yield name, payload, "", False
            elif block.get("type") == "tool_result":
                is_metadata = metadata_calls.pop(block.get("tool_use_id"), False)
                body = block.get("content")
                if isinstance(body, str):
                    yield "", {}, body[:MAX_TEXT_SCAN], is_metadata
                elif isinstance(body, list):
                    text = " ".join(str(part.get("text", "")) for part in body
                                    if isinstance(part, dict))
                    yield "", {}, text[:MAX_TEXT_SCAN], is_metadata


def _evidence(records, pr_files: dict[str, str], branch: str, shas: set[str]) -> dict:
    """What this session shows: PR files edited, branches seen, PR shas seen."""
    files: list[str] = []
    branches: set[str] = set()
    hit_shas: list[str] = []
    sha_prefixes = {s[:7] for s in shas if len(s) >= 7}

    def note_path(value):
        if not isinstance(value, str):
            return
        hit = _matches_pr_file(value, pr_files)
        if hit and hit not in files:
            files.append(hit)

    for tool, payload, result, result_is_pr_metadata in _tool_calls(records):
        if tool in EDIT_TOOLS:
            note_path(payload.get("file_path"))
            for edit in (payload.get("edits") or []) if isinstance(payload.get("edits"), list) else []:
                if isinstance(edit, dict):
                    note_path(edit.get("file_path"))
        elif tool == "apply_patch":
            for match in _APPLY_PATCH_PATH.finditer(str(payload.get("input", ""))[:MAX_TEXT_SCAN]):
                note_path(match.group(1))
        command = payload.get("command") or payload.get("cmd") or ""
        if isinstance(command, str) and command:
            command = command[:MAX_TEXT_SCAN]
            for match in _GIT_PATHS.finditer(command):
                for token in match.group(1).split():
                    if not token.startswith("-"):
                        note_path(token.strip("'\""))
            for match in _BRANCH_CMD.finditer(command):
                name = match.group(1) or match.group(2) or match.group(3)
                if name:
                    branches.add(name.strip())
        # A sha the session was TOLD by GitHub is not a sha it produced.
        if result and sha_prefixes and not result_is_pr_metadata:
            for match in _SHA_RX.finditer(result):
                token = match.group(0)
                if token[:7] in sha_prefixes and token not in hit_shas:
                    hit_shas.append(token)

    return {"files": files, "branches": branches, "shas": hit_shas}


def _session_branches(records) -> set[str]:
    """Claude records carry the branch on the record itself."""
    out = set()
    for record in records:
        value = record.get("gitBranch") if isinstance(record, dict) else None
        if isinstance(value, str) and value:
            out.add(value)
    return out


def _score(evidence: dict, branch: str, base: str | None, in_window: bool) -> tuple[int, dict]:
    files = evidence["files"]
    branches = evidence["branches"]
    shas = evidence["shas"]
    branch_match = bool(branch) and branch in branches
    score = 0
    if shas:
        score += 5                      # a PR commit sha appearing here is proof
    score += min(len(files), 4) * 2     # each PR file edited, capped at 8
    if branch_match:
        score += 3
    if in_window:
        score += 1
    if (base and base in branches and not branch_match and not files):
        score -= 5                      # only ever on the base branch, touched nothing
    return score, {"shas": shas, "files": files,
                   "branch_match": branch_match, "in_window": in_window}


def _created_at_epoch(raw: str | None) -> float | None:
    if not raw:
        return None
    import datetime
    try:
        return datetime.datetime.fromisoformat(
            raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--files-from", required=True,
                    help="a file of the PR's paths, one per line")
    ap.add_argument("--branch", default="", help="the PR's head ref")
    ap.add_argument("--base", default=None, help="the PR's base ref")
    ap.add_argument("--sha", action="append", default=[],
                    help="a commit oid on the PR (repeatable)")
    ap.add_argument("--created-at", default=None, help="the PR's createdAt, ISO-8601")
    ap.add_argument("--host", default="all", choices=["all", *readers.READERS])
    ap.add_argument("--limit", type=int, default=200,
                    help="how many recent sessions per host to scan")
    ap.add_argument("--max-candidates", type=int, default=10)
    ap.add_argument("--json", action="store_true",
                    help="accepted for symmetry; output is always JSON")
    args = ap.parse_args()

    try:
        raw = Path(args.files_from).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"ERROR: cannot read {args.files_from}: {exc}", file=sys.stderr)
        return 2
    pr_files = {}
    for line in raw.splitlines():
        norm = _norm(line)
        if norm:
            pr_files.setdefault(norm, line.strip())
    if not pr_files:
        print(f"ERROR: no paths in {args.files_from}", file=sys.stderr)
        return 2

    shas = {s.strip().lower() for s in args.sha if s and s.strip()}
    created = _created_at_epoch(args.created_at)
    window_start = (created - WINDOW_DAYS * 86400) if created else None

    hosts = list(readers.READERS) if args.host == "all" else [args.host]
    rows = []
    skipped: list[str] = []
    for host in hosts:
        reader = readers.reader_for(host)
        if reader is None:
            continue
        try:
            listed = reader.list_sessions(limit=args.limit)
        except Exception:  # noqa: BLE001 — one unreadable host must not blind the rest
            continue
        for session in listed:
            path = session.get("path")
            try:
                # The readers parse a whole transcript into memory, so a size
                # guard is the only bound available here. It stays — but a
                # session dropped by it is REPORTED rather than silently
                # absent: the ones that blow the cap are long sessions, which
                # are exactly the ones likely to have done the work.
                if os.path.getsize(path) > MAX_BYTES:
                    skipped.append(f"{host} {session.get('id')} "
                                   f"({os.path.getsize(path) // (1024 * 1024)} MiB)")
                    continue
                records, _meta = reader.to_canonical(path)
            except Exception:  # noqa: BLE001 — a corrupt session is skipped, never fatal
                continue
            if not records:
                continue
            evidence = _evidence(records, pr_files, args.branch, shas)
            evidence["branches"] |= _session_branches(records)
            mtime = session.get("mtime") or 0
            in_window = bool(created) and window_start <= mtime <= created + 86400
            score, detail = _score(evidence, args.branch, args.base, in_window)
            if score <= 0:
                continue
            try:
                cwd = reader.session_cwd(path)
            except Exception:  # noqa: BLE001
                cwd = None
            rows.append({
                "conversation_id": pr_link.conversation_id_for(host, session["id"]),
                "session_id": session["id"], "host": host, "cwd": cwd,
                "mtime": mtime, "score": score, "evidence": detail,
            })

    rows.sort(key=lambda r: (-r["score"], -(r["mtime"] or 0)))
    print(json.dumps(rows[:args.max_candidates], indent=2))
    if skipped:
        # stderr, so the JSON contract on stdout is untouched — the skill
        # reads this and tells the user what was NOT looked at.
        print(f"note: {len(skipped)} session(s) larger than "
              f"{MAX_BYTES // (1024 * 1024)} MiB were not scanned: "
              + ", ".join(skipped[:10]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
