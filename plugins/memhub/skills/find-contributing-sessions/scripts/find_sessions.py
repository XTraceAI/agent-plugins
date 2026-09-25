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
`skills/start-rulebook/scripts/mine_sessions.py` uses. Candidates are
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
# `git add` / `git commit` found by TOKENS, not by a regex anchored on `git`
# at command position. That regex missed `git -C /repo add README.md`,
# `env FOO=1 git add …` and `sudo git commit -- …` — all of which agents write
# — so the files those commands touched scored no evidence at all (Codex
# review, PR #182). `_git_branches` already walks tokens this way; this reuses
# the same wrapper handling so the two cannot disagree about what "a git
# command" is.
_GIT_PATH_SUBCOMMANDS = frozenset({"add", "commit"})


def _git_path_arguments(command: str) -> list[str]:
    """The argument text of every `git add` / `git commit` in this command."""
    found: list[str] = []
    for segment in re.split(r"[;&|]+", command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        tokens = _after_wrappers(tokens)
        if len(tokens) < 2 or _basename(tokens[0]) != "git":
            continue
        rest = tokens[1:]
        while rest and rest[0].startswith("-"):     # `git -C dir add …`
            rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
        if not rest or rest[0] not in _GIT_PATH_SUBCOMMANDS:
            continue
        found.append(shlex.join(rest[1:]) if hasattr(shlex, "join")
                     else " ".join(rest[1:]))
    return found
# Flags whose VALUE is a separate word. `git commit -m "docs: update
# README.md"` was handing every word of the message to the path matcher, so a
# session that committed unrelated work scored file evidence for a PR file it
# had never touched — and with branch and window points could outrank a real
# contributor, or push one off the capped list.
_GIT_VALUE_FLAGS = frozenset({
    "-m", "--message", "-F", "--file", "-C", "--reuse-message",
    "-c", "--reedit-message", "--author", "--date", "--squash", "--fixup",
    "--pathspec-from-file", "--gpg-sign", "-S", "--cleanup", "--trailer"})
# The same letters, for BUNDLED runs: `git commit -am "msg"` is `-a -m msg`,
# and an exact-token test left the message to be read as a pathspec.
_GIT_VALUE_SHORT = frozenset("mFCcSt")


def _git_consumes_operand(token: str) -> bool:
    if token in _GIT_VALUE_FLAGS:
        return True
    if token.startswith("--") or not token.startswith("-") or token == "-":
        return False
    # getopt: the first value-taking letter consumes the rest of the token if
    # there is any, otherwise the next token.
    for position, char in enumerate(token[1:], start=1):
        if char in _GIT_VALUE_SHORT:
            return position == len(token) - 1
    return False


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
            index += 2 if ("=" not in token and _git_consumes_operand(token)) else 1
            continue
        paths.append(token)
        index += 1
    return paths
# Which branch a `git checkout` / `git switch` names. Read as TOKENS rather
# than by regex: git accepts `-c feat`, `-cfeat` and `--create=feat` for the
# same thing, and a regex chasing each spelling missed the ones it had not been
# told about — Codex and Cursor sessions have no top-level `gitBranch`, so a
# missed `switch -c` costs them the branch signal entirely.
_BRANCH_SUBCOMMANDS = ("checkout", "switch")
_BRANCH_CREATE_FLAGS = frozenset({"-b", "-B", "-c", "-C",
                                  "--create", "--force-create"})
_BRANCH_VALUE_FLAGS = _BRANCH_CREATE_FLAGS | {"--start-point", "-t", "--track",
                                              "--orphan"}
# `git switch --detach feat/x` puts HEAD AT that commit without moving onto the
# branch, so the session is not "on" it — a reviewer inspecting the PR's tip
# was scoring the three branch points for it.
_BRANCH_DETACH_FLAGS = frozenset({"--detach", "-d"})


_WRAPPERS = frozenset({"cd", "env", "sudo", "doas", "time", "nohup", "command",
                       "exec", "timeout", "stdbuf"})
# Wrapper flags that take a SEPARATE operand — `env -u NAME`, `sudo -u USER`,
# `timeout -s SIG`. Skipping only the wrapper's name left the operand to be
# read as the executable, so `env FOO=1 git switch -c feat/x` found no git.
_WRAPPER_VALUE_FLAGS = frozenset({"-u", "--unset", "-g", "--group", "-p",
                                  "--prompt", "-U", "-C", "-s", "--signal",
                                  "-k", "--kill-after"})
_ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=")


def _basename(token: str) -> str:
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def _after_wrappers(tokens: list[str]) -> list[str]:
    """The command proper, with leading wrappers and their operands removed."""
    index, after_wrapper = 0, False
    while index < len(tokens):
        token = tokens[index]
        if _ASSIGNMENT.match(token):
            index += 1
            continue
        if _basename(token) in _WRAPPERS:
            index, after_wrapper = index + 1, True
            continue
        if token.startswith("-"):
            index += 2 if (after_wrapper and token in _WRAPPER_VALUE_FLAGS
                           and "=" not in token) else 1
            continue
        if after_wrapper and token[:1].isdigit():        # `timeout 5 …`
            index += 1
            continue
        break
    return tokens[index:]


def _git_branches(command: str) -> list[str]:
    """Every branch named by a checkout/switch in this command."""
    found: list[str] = []
    for segment in re.split(r"[;&|]+", command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        tokens = _after_wrappers(tokens)
        if len(tokens) < 2 or _basename(tokens[0]) != "git":
            continue
        rest = tokens[1:]
        while rest and rest[0].startswith("-"):     # `git -C dir switch …`
            rest = rest[2:] if rest[0] in ("-C", "-c") else rest[1:]
        if not rest or rest[0] not in _BRANCH_SUBCOMMANDS:
            continue
        if any(t in _BRANCH_DETACH_FLAGS for t in rest[1:]):
            continue                # detaching is not being on the branch
        index = 1
        while index < len(rest):
            token = rest[index]
            if token == "--":
                break
            # A create flag NAMES the branch, and what follows it is the
            # START POINT, not a second branch: `git switch -c review-copy
            # feat/x` puts the session on `review-copy` and reads `feat/x`.
            # Recording both scored a session for a branch it only branched
            # FROM (Codex review, PR #182) — so stop this segment once the
            # created branch is known.
            if token.startswith("--") and "=" in token:
                flag, _, value = token.partition("=")
                if flag in _BRANCH_CREATE_FLAGS and value:
                    found.append(value)
                    break
                index += 1
                continue
            if token in _BRANCH_VALUE_FLAGS:
                if index + 1 < len(rest) and token in _BRANCH_CREATE_FLAGS:
                    found.append(rest[index + 1])
                    break
                index += 2
                continue
            if (token.startswith("-") and not token.startswith("--")
                    and len(token) > 2 and token[:2] in _BRANCH_CREATE_FLAGS):
                found.append(token[2:])             # `-cfeat/x`
                break
            if token.startswith("-"):
                index += 1
                continue
            found.append(token)                     # `git switch feat/x`
            break
    return [b for b in found if b]


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
                # …and a commit-producing command that FAILED created nothing.
                # `git cherry-pick <pr-sha>` answering `fatal: bad object
                # <pr-sha>` echoes the sha straight back, so a reviewer who ran
                # it scored five proof points for a commit that never existed.
                if block.get("is_error") is True or block.get("isError") is True:
                    is_proof = False
                body = block.get("content")
                if isinstance(body, str):
                    yield "", {}, body[:MAX_TEXT_SCAN], is_proof
                elif isinstance(body, list):
                    text = " ".join(str(part.get("text", "")) for part in body
                                    if isinstance(part, dict))
                    yield "", {}, text[:MAX_TEXT_SCAN], is_proof


def _features(records) -> dict:
    """Everything a session shows that does NOT depend on which PR is asked
    about: the paths it edited, the branches it named, and the sha-shaped
    tokens printed by calls that made commits — each in first-seen order.

    Walking the tool calls (shlex-splitting every command, pairing results to
    calls) is the expensive part of scoring. Done per PR, a 25-PR batch was
    ~18× slower than one PR on a real history — minutes, past a host's
    command timeout — so it is done once per session and `_match` does only
    the cheap suffix / prefix comparison per PR.
    """
    paths: list[str] = []
    seen_paths: set[str] = set()
    branches: set[str] = set()
    sha_tokens: list[str] = []
    seen_tokens: set[str] = set()

    def note_path(value):
        if isinstance(value, str) and value not in seen_paths:
            seen_paths.add(value)
            paths.append(value)

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
            for argument_text in _git_path_arguments(command):
                for token in _git_pathspecs(argument_text):
                    note_path(token)
            for name in _git_branches(command):
                branches.add(name.strip())
        # A sha the session was SHOWN is not a sha it produced.
        if result and result_is_sha_proof:
            for match in _SHA_RX.finditer(result):
                token = match.group(0)
                if token not in seen_tokens:
                    seen_tokens.add(token)
                    sha_tokens.append(token)

    return {"paths": paths, "branches": branches, "sha_tokens": sha_tokens}


def _match(features: dict, pr_files: dict[str, str], shas: set[str]) -> dict:
    """One PR's evidence from a session's features."""
    files: list[str] = []
    for value in features["paths"]:
        hit = _matches_pr_file(value, pr_files)
        if hit and hit not in files:
            files.append(hit)
    sha_prefixes = {s[:7] for s in shas if len(s) >= 7}
    hit_shas = ([t for t in features["sha_tokens"] if t[:7] in sha_prefixes]
                if sha_prefixes else [])
    return {"files": files, "branches": set(features["branches"]), "shas": hit_shas}


def _evidence(records, pr_files: dict[str, str], branch: str, shas: set[str]) -> dict:
    """What this session shows: PR files edited, branches seen, PR shas seen."""
    return _match(_features(records), pr_files, shas)


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


def _pr_files(lines) -> dict[str, str]:
    """Normalised path -> the PR's own spelling, first one wins."""
    pr_files: dict[str, str] = {}
    for line in lines:
        if not isinstance(line, str):
            continue
        norm = _norm(line)
        if norm:
            pr_files.setdefault(norm, line.strip())
    return pr_files


def _target(pr_files: dict[str, str], branch: str, base: str | None,
            shas, created_at: str | None, repo: str | None) -> dict:
    """One pull request as the scan sees it."""
    created = _created_at_epoch(created_at)
    return {"files": pr_files, "branch": branch or "", "base": base,
            "shas": {s.strip().lower() for s in shas if s and s.strip()},
            "created": created,
            "window_start": (created - WINDOW_DAYS * 86400) if created else None,
            "repo": repo}


def _repo_matches(session_repo: str | None, wanted: str | None, fold: bool) -> bool:
    if not wanted or session_repo is None:
        return True
    if fold:
        return session_repo.casefold() == wanted.casefold()
    return session_repo == wanted


def _scan(targets: list[dict], hosts: list[str], limit: int,
          fold_repo: bool = False) -> tuple[list[list[dict]], list[str], int]:
    """Rank local sessions against every target in ONE pass over them.

    Each transcript is parsed once, however many pull requests it is scored
    against: parsing is the expensive part, and a batch of 25 PRs over 200
    sessions would otherwise read every transcript 25 times.

    Returns ``(rows per target, over-cap sessions, sessions parsed)``.
    """
    rows: list[list[dict]] = [[] for _ in targets]
    skipped: list[str] = []
    parsed = 0
    for host in hosts:
        reader = readers.reader_for(host)
        if reader is None:
            continue
        try:
            listed = reader.list_sessions(limit=limit)
        except Exception:  # noqa: BLE001 — one unreadable host must not blind the rest
            continue
        own_thread = getattr(reader, "is_own_thread_path", None)
        for session in listed:
            path = session.get("path")
            # A Codex guardian review CONTAINS a copy of the conversation it is
            # reviewing, so it scores like a strong authorship match on the very
            # evidence this ranks by. It wrote none of the PR. Bounded header
            # read; an unreadable one keeps the candidate.
            try:
                if own_thread is not None and not own_thread(path):
                    continue
            except Exception:  # noqa: BLE001
                pass
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
            parsed += 1
            features = _features(records)
            features["branches"] |= _session_branches(records)
            mtime = session.get("mtime") or 0
            resolved = False
            cwd = session_repo = None
            for index, target in enumerate(targets):
                evidence = _match(features, target["files"], target["shas"])
                created = target["created"]
                in_window = (bool(created)
                             and target["window_start"] <= mtime <= created + 86400)
                score, detail = _score(evidence, target["branch"], target["base"],
                                       in_window)
                if score <= 0:
                    continue
                if not resolved:            # once per session, and only if it scored
                    resolved = True
                    try:
                        cwd = reader.session_cwd(path)
                    except Exception:  # noqa: BLE001
                        cwd = None
                    session_repo = _session_repo(cwd) if any(
                        t["repo"] for t in targets) else None
                # A session in ANOTHER repository can score on this one's
                # files, because `_matches_pr_file` matches by suffix on
                # purpose — an unrelated project's `README.md` or
                # `src/index.ts` would otherwise take a slot on the capped list
                # from a real contributor. A cwd that cannot be resolved is
                # KEPT: dropping it would silently lose candidates, and the
                # user still approves every link.
                if not _repo_matches(session_repo, target["repo"], fold_repo):
                    continue
                rows[index].append({
                    "conversation_id": pr_link.conversation_id_for(host, session["id"]),
                    "session_id": session["id"], "host": host, "cwd": cwd,
                    "mtime": mtime, "score": score, "evidence": detail,
                })
    for found in rows:
        found.sort(key=lambda r: (-r["score"], -(r["mtime"] or 0)))
    return rows, skipped, parsed


def _report_skipped_sessions(skipped: list[str]) -> None:
    if skipped:
        # stderr, so the JSON contract on stdout is untouched — the skill
        # reads this and tells the user what was NOT looked at.
        print(f"note: {len(skipped)} session(s) larger than "
              f"{MAX_BYTES // (1024 * 1024)} MiB were not scanned: "
              + ", ".join(skipped[:10]), file=sys.stderr)


# --- batch mode: many pull requests, facts collected here ------------------

MAX_BATCH_PRS = 25
GH_TIMEOUT_S = 60
# gh pr view --json files stops at 100; at or past that, ask the REST API.
GH_FILES_PAGE = 100
# A pasted PR URL is often a tab of it (`/files`, `/commits/<sha>`), and gh
# accepts `http://` too; both name the same pull request.
_PR_URL = re.compile(r"^https?://([^/\s]+)/([^/\s]+)/([^/\s]+)/pull/(\d+)"
                     r"(?:/(?:files|commits|checks|changes)(?:/[^\s]*)?)?/?$", re.I)
# Set by `_batch` to what shutil.which found: on Windows that can be a
# `gh.cmd` shim, which subprocess would not find from the bare name.
_GH_EXE = "gh"


def _parse_pr_url(raw: str) -> dict | None:
    """``https://<host>/<owner>/<repo>/pull/<n>`` — query/fragment and a tab
    suffix dropped, host KEPT (an enterprise PR is a different pull request,
    not a typo) apart from `www.github.com`, which IS github.com. `_batch`
    then skips any host but github.com: MemHub links only github.com PRs."""
    text = (raw or "").strip().split("#", 1)[0].split("?", 1)[0]
    match = _PR_URL.match(text)
    if not match:
        return None
    host, owner, repo, number = match.groups()
    if host.casefold() == "www.github.com":
        host = "github.com"
    return {"url": f"https://{host}/{owner}/{repo}/pull/{int(number)}",
            "host": host, "owner": owner, "repo": repo, "number": int(number)}


def _gh(args: list[str]) -> tuple[int, str]:
    """Run gh; ``(exit code, stdout)``. Its stderr is never echoed — it can
    carry a token hint or a URL the user did not ask to see.

    Decoded as UTF-8 explicitly: gh writes UTF-8, and Windows' locale default
    (cp1252) raises on bytes such as those of `Á` or most Cyrillic — one PR
    title would otherwise have crashed the whole batch.
    """
    import subprocess
    try:
        done = subprocess.run([_GH_EXE, *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=GH_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, ""
    except (OSError, ValueError):
        return 127, ""
    return done.returncode, done.stdout or ""


def _collect_pr(pr: dict) -> tuple[dict | None, str | None]:
    """The PR's facts from gh, by URL — ``(facts, None)`` or ``(None, reason)``.

    The URL, never the number: `gh pr view <n>` resolves a number against the
    CURRENT checkout, so a PR in another repository would have been scanned
    for the wrong branch, files and commits (Codex, #182).
    """
    code, out = _gh(["pr", "view", pr["url"], "--json",
                     "url,title,headRefName,baseRefName,createdAt,files,commits"])
    if code != 0:
        return None, "gh_failed"
    try:
        view = json.loads(out)
    except ValueError:
        return None, "gh_failed"
    if not isinstance(view, dict):
        return None, "gh_failed"
    returned = _parse_pr_url(str(view.get("url") or ""))
    if returned is None:
        return None, "url_mismatch"
    if returned["url"].casefold() != pr["url"].casefold():
        # Same host and number under another owner/repo is GitHub following a
        # rename or transfer: the same pull request, but MemHub may know it by
        # either name, so say so rather than silently scanning one and linking
        # the other.
        if (returned["host"].casefold() == pr["host"].casefold()
                and returned["number"] == pr["number"]):
            pr["canonical_url"] = returned["url"]
            return None, "url_redirected"
        return None, "url_mismatch"
    files = [f.get("path") for f in view.get("files") or [] if isinstance(f, dict)]
    if len(files) >= GH_FILES_PAGE:
        api = ["api", f"repos/{pr['owner']}/{pr['repo']}/pulls/{pr['number']}/files",
               "--paginate", "-q", ".[].filename"]
        code, listed = _gh(api)
        if code == 0 and listed.strip():
            files = listed.splitlines()
        else:
            print(f"note: files_truncated {pr['url']} — the file list stops at "
                  f"{len(files)}", file=sys.stderr)
    pr_files = _pr_files(files)
    if not pr_files:
        return None, "no_files"
    shas = [c.get("oid") for c in view.get("commits") or [] if isinstance(c, dict)]
    facts = _target(pr_files, str(view.get("headRefName") or ""),
                    view.get("baseRefName") or None,
                    [s for s in shas if isinstance(s, str)],
                    view.get("createdAt"), pr["repo"])
    facts.update({"pr_url": pr["url"], "repo_full": f"{pr['owner']}/{pr['repo']}",
                  "number": pr["number"], "title": view.get("title") or "",
                  "head_ref": facts["branch"], "base_ref": facts["base"]})
    return facts, None


def _batch(args) -> int:
    import shutil
    raw_urls: list[str] = list(args.pr_url or [])
    if args.prs_from:
        try:
            text = Path(args.prs_from).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"ERROR: cannot read {args.prs_from}: {exc}", file=sys.stderr)
            return 2
        raw_urls += [line.strip() for line in text.splitlines()
                     if line.strip() and not line.strip().startswith("#")]
    if not raw_urls:
        print("ERROR: no pull request URLs given", file=sys.stderr)
        return 2
    global _GH_EXE
    found = shutil.which("gh")
    if found is None:
        print("ERROR: gh not found — the GitHub CLI reads each PR's files, "
              "branch and commits", file=sys.stderr)
        return 2
    _GH_EXE = found

    skipped_prs: list[dict] = []
    wanted: list[dict] = []
    seen: set[str] = set()
    for raw in raw_urls:
        pr = _parse_pr_url(raw)
        if pr is None:
            skipped_prs.append({"pr_url": raw, "reason": "invalid_url"})
            continue
        key = pr["url"].casefold()
        if key in seen:
            continue
        seen.add(key)
        if pr["host"].casefold() != "github.com":
            # MemHub's link_pr accepts github.com PRs only, so scanning one here
            # would rank candidates nobody can link. Before the cap, and before
            # gh is ever asked.
            skipped_prs.append({"pr_url": pr["url"], "reason": "unsupported_host"})
            continue
        if len(wanted) >= args.max_prs:
            skipped_prs.append({"pr_url": pr["url"], "reason": "over_cap"})
            continue
        wanted.append(pr)

    targets: list[dict] = []
    for pr in wanted:
        facts, reason = _collect_pr(pr)
        if facts is None:
            entry = {"pr_url": pr["url"], "reason": reason}
            if pr.get("canonical_url"):
                entry["canonical_url"] = pr["canonical_url"]
            skipped_prs.append(entry)
        else:
            targets.append(facts)

    hosts = list(readers.READERS) if args.host == "all" else [args.host]
    rows, skipped, parsed = (_scan(targets, hosts, args.limit, fold_repo=True)
                             if targets else ([], [], 0))
    print(json.dumps({
        "prs": [{"pr_url": t["pr_url"], "repo": t["repo_full"], "number": t["number"],
                 "title": t["title"], "head_ref": t["head_ref"],
                 "base_ref": t["base_ref"],
                 "candidates": found[:args.max_candidates]}
                for t, found in zip(targets, rows)],
        "skipped_prs": skipped_prs,
        "sessions_scanned": parsed,
    }, indent=2))
    _report_skipped_sessions(skipped)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--files-from", default=None,
                    help="single mode: a file of ONE PR's paths, one per line")
    ap.add_argument("--pr-url", action="append", default=[],
                    help="batch mode: a PR URL (repeatable); its facts are read "
                         "with gh")
    ap.add_argument("--prs-from", default=None,
                    help="batch mode: a file of PR URLs, one per line")
    ap.add_argument("--max-prs", type=int, default=MAX_BATCH_PRS,
                    help="batch mode: how many PRs to scan at most")
    ap.add_argument("--branch", default=None, help="the PR's head ref")
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
    args = ap.parse_args(argv)

    batch = bool(args.pr_url or args.prs_from)
    if batch == bool(args.files_from):
        print("ERROR: give either --files-from (one PR) or --pr-url/--prs-from "
              "(a batch), not both and not neither", file=sys.stderr)
        return 2
    if batch:
        # In a batch every PR brings its own branch, base, shas, date and repo;
        # a single value here would silently apply to all of them.
        stray = [flag for flag, value in (
            ("--branch", args.branch), ("--base", args.base), ("--sha", args.sha),
            ("--created-at", args.created_at), ("--repo", args.repo)) if value]
        if stray:
            print(f"ERROR: {', '.join(stray)} belong to --files-from mode; in a "
                  "batch each PR's own values are read with gh", file=sys.stderr)
            return 2
        if args.max_prs < 1:
            print("ERROR: --max-prs must be at least 1", file=sys.stderr)
            return 2
        return _batch(args)

    try:
        raw = Path(args.files_from).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"ERROR: cannot read {args.files_from}: {exc}", file=sys.stderr)
        return 2
    pr_files = _pr_files(raw.splitlines())
    if not pr_files:
        print(f"ERROR: no paths in {args.files_from}", file=sys.stderr)
        return 2

    target = _target(pr_files, args.branch or "", args.base, args.sha,
                     args.created_at, args.repo)
    hosts = list(readers.READERS) if args.host == "all" else [args.host]
    rows, skipped, _parsed = _scan([target], hosts, args.limit)
    print(json.dumps(rows[0][:args.max_candidates], indent=2))
    _report_skipped_sessions(skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
