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
import shlex
import sys
from pathlib import Path

# Hard caps per session — a rollout can be enormous and this runs over up to
# --limit of them.
MAX_RECORDS = 20_000
MAX_BYTES = 64 * 1024 * 1024
MAX_TEXT_SCAN = 256 * 1024      # per tool result, for sha hunting
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# Which key holds the path depends on the HOST, not on us: the readers pass
# native tool arguments through unchanged (`readers/cursor.py` puts the tool's
# own `args` straight into `input`), so `file_path` is the Claude spelling and
# assuming it silently gave Cursor sessions no file evidence at all.
EDIT_PATH_KEYS = ("file_path", "path", "notebook_path")
WINDOW_DAYS = 30

_SHA_RX = re.compile(r"\b[0-9a-f]{7,40}\b")
# A sha is "proof" only when the command that printed it MADE the commit.
# `git log`, `git show`, `git diff` and `gh pr view` all display history that
# any session in the repo can see, so a reviewer who reads the branch scores
# the highest-value signal for code they only looked at. An allowlist rather
# than a denylist: the default for an unrecognised command is "not proof",
# because the cost of a wrong author is worse than the cost of a missed one —
# and a real author still scores on files (2 each) and branch (3).
# `push` is deliberately NOT here. A push prints an `old..new` range for
# commits that already existed — often made in an earlier session — so a
# session that only pushed someone else's work scored the top signal plus the
# branch match and was recommended for linking. `apply` and `stash` are out for
# the same reason: neither creates the pull request's commits.
_COMMIT_PRODUCING = re.compile(
    r"(?:^|[;&|(`]|\$\()\s*(?:\w+=\S*\s+)*"
    r"git\b(?:\s+-[cC]\s+\S+)*\s+"
    # `(?![-\w])`, not `\b`: a word boundary also sits before a hyphen, so
    # `\bmerge\b` matched `git merge-base` — a read-only query that prints an
    # existing common-ancestor sha, scored as top-value authorship proof.
    r"(?:commit|cherry-pick|revert|merge|rebase|am)(?![-\w])", re.I)
# Anything that PRINTS shas it did not create. One Bash call is often a chain
# and its result is the combined output, so `git commit -m x && git log` would
# otherwise credit this session with every sha in the log. There is no way to
# split one stdout back into its commands, so a command that both creates and
# displays declines rather than guesses — `git add -A && git commit` still
# counts, because `git add` prints no shas.
_SHA_DISPLAYING = re.compile(
    r"(?:^|[;&|(`]|\$\()\s*(?:\w+=\S*\s+)*"
    r"git\b(?:\s+-[cC]\s+\S+)*\s+"
    r"(?:log|show|rev-parse|rev-list|reflog|describe|cherry|ls-remote|diff|"
    r"blame|shortlog|whatchanged|bisect|branch|tag|status|merge-base|"
    r"merge-tree|name-rev|for-each-ref)(?![-\w])", re.I)
_APPLY_PATCH_PATH = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (.+)")
_GIT_PATHS = re.compile(r"(?:^|[;&|])\s*git\s+(?:add|commit)\b([^;&|\n]*)")
# Flags whose VALUE is a separate word. `git commit -m "docs: update
# README.md"` was handing every word of the message to the path matcher, so a
# session that committed unrelated work scored file evidence for a PR file it
# had never touched — and with branch and window points could outrank a real
# contributor, or push one off the capped list.
_GIT_VALUE_FLAGS = frozenset({
    "-m", "--message", "-F", "--file", "-C", "--reuse-message",
    "-c", "--reedit-message", "--author", "--date", "--squash", "--fixup",
    "--pathspec-from-file", "--gpg-sign", "-S", "--cleanup", "--trailer"})


def _git_pathspecs(argument_text: str) -> list[str]:
    """The PATHSPEC operands of a `git add` / `git commit`, and nothing else.

    Tokenised with `shlex` so a quoted commit message is one word rather than
    several, then walked so a flag's operand is consumed with it.
    """
    try:
        tokens = shlex.split(argument_text, posix=True)
    except ValueError:
        return []
    paths: list[str] = []
    index, only_paths = 0, False
    while index < len(tokens):
        token = tokens[index]
        if only_paths:
            paths.append(token)
            index += 1
            continue
        if token == "--":                     # everything after is a pathspec
            only_paths = True
            index += 1
            continue
        if token.startswith("-"):
            index += 2 if (token in _GIT_VALUE_FLAGS and "=" not in token) else 1
            continue
        paths.append(token)
        index += 1
    return paths
# `-b`/`-B` create a branch with `checkout`; `switch` spells the same thing
# `-c`/`-C` (`--create`/`--force-create`). Codex and Cursor sessions have no
# top-level `gitBranch` to fall back on, so missing `switch -c` cost them the
# branch signal — three points and their second piece of evidence — on the
# very command that creates the PR's branch.
_BRANCH_CMD = re.compile(
    r"(?:^|[;&|])\s*git\s+(?:checkout|switch)\s+"
    r"(?:(?:-b|-B|-c|-C|--create|--force-create)\s+)?"
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

try:                                  # same resolver the rulebook hook uses
    from repo_identity import repo_name as _repo_name_of
except Exception:                     # noqa: BLE001 — degrade to no filtering
    _repo_name_of = None


def _session_repo(cwd: str | None) -> str | None:
    """The repo a session's cwd belongs to, or None if it cannot be resolved.

    Resolved from the checkout (git remote) rather than the directory name, so
    a worktree of the PR's repo still matches.
    """
    if not cwd or _repo_name_of is None:
        return None
    try:
        return _repo_name_of(cwd.rstrip("/"))
    except Exception:                 # noqa: BLE001
        return None


def _norm(path: str) -> str:
    """Repo-relative POSIX form. Strips a literal `./` prefix and leading
    separators — NOT leading dots: `lstrip("./")` turned `.env` into `env` and
    `.github/workflows/ci.yml` into `github/…`, so a session that edited the
    non-hidden path scored false file evidence against a hidden one."""
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


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


def _makes_commits(tool: str, payload: dict) -> bool:
    """Would this call have CREATED the commits whose shas it prints?

    Only the shell can, and only a commit-CREATING git subcommand does. A
    `gh pr view --json commits` (the skill's own step 2), a `git log`, a
    `git show`, and a `git push` — all of these merely display shas that
    already exist and that anyone with the repo can read.
    """
    if tool not in ("Bash", "shell", "local_shell", "exec", "exec_command"):
        return False
    command = payload.get("command") or payload.get("cmd") or ""
    if not isinstance(command, str) or not command:
        return False
    command = command[:MAX_TEXT_SCAN]
    # A command that addressed GitHub is asking about the PR, never making it.
    if pr_link.touches_github("Bash", {"command": command}):
        return False
    # …and a chain that also DISPLAYS shas cannot be told apart in one stdout.
    if _SHA_DISPLAYING.search(command):
        return False
    return bool(_COMMIT_PRODUCING.search(command))


def _tool_calls(records):
    """``(tool, input, result_text, result_is_sha_proof)`` tuples, bounded.

    A result is paired back to the call that produced it (by ``tool_use_id``,
    the way ``mine_sessions.py`` does) so that a sha counts as authorship
    evidence only when the call that printed it MADE the commit. Two ways this
    went wrong before: the skill's own step 2 runs `gh pr view <n> --json
    commits`, so the session running the scan found the PR's shas in its own
    transcript; and a reviewer who ran `git log` on the branch saw them too.
    Both scored the highest-value signal for code they had only read.
    """
    seen = 0
    sha_bearing: dict[str, bool] = {}
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
                    if len(sha_bearing) >= MAX_RECORDS:
                        sha_bearing.clear()
                    sha_bearing[call_id] = _makes_commits(name, payload)
                yield name, payload, "", False
            elif block.get("type") == "tool_result":
                # Default False: a result whose call was not a commit-producing
                # git operation contributes no sha evidence at all.
                is_proof = sha_bearing.pop(block.get("tool_use_id"), False)
                body = block.get("content")
                if isinstance(body, str):
                    yield "", {}, body[:MAX_TEXT_SCAN], is_proof
                elif isinstance(body, list):
                    text = " ".join(str(part.get("text", "")) for part in body
                                    if isinstance(part, dict))
                    yield "", {}, text[:MAX_TEXT_SCAN], is_proof


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

    for tool, payload, result, result_is_sha_proof in _tool_calls(records):
        if tool in EDIT_TOOLS:
            for key in EDIT_PATH_KEYS:
                note_path(payload.get(key))
            edits = payload.get("edits")
            for edit in edits if isinstance(edits, list) else []:
                if isinstance(edit, dict):
                    for key in EDIT_PATH_KEYS:
                        note_path(edit.get(key))
        elif tool == "apply_patch":
            for match in _APPLY_PATCH_PATH.finditer(str(payload.get("input", ""))[:MAX_TEXT_SCAN]):
                note_path(match.group(1))
        command = payload.get("command") or payload.get("cmd") or ""
        if isinstance(command, str) and command:
            command = command[:MAX_TEXT_SCAN]
            for match in _GIT_PATHS.finditer(command):
                for token in _git_pathspecs(match.group(1)):
                    note_path(token)
            for match in _BRANCH_CMD.finditer(command):
                name = match.group(1) or match.group(2) or match.group(3)
                if name:
                    branches.add(name.strip())
        # A sha the session was SHOWN is not a sha it produced.
        if result and sha_prefixes and result_is_sha_proof:
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
    ap.add_argument("--repo", default=None,
                    help="only sessions in this repo (resolved from the git "
                         "remote, so a worktree counts). Path matching is by "
                         "SUFFIX, so without this an unrelated project's "
                         "README.md scores against the PR's.")
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
            # A session in ANOTHER repository can score on this one's files,
            # because `_matches_pr_file` matches by suffix on purpose — an
            # unrelated project's `README.md` or `src/index.ts` would otherwise
            # take a slot on the capped list from a real contributor. A cwd
            # that cannot be resolved is KEPT: dropping it would silently lose
            # candidates, and the user still approves every link.
            if args.repo:
                session_repo = _session_repo(cwd)
                if session_repo is not None and session_repo != args.repo:
                    continue
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
