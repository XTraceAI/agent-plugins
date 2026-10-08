#!/usr/bin/env python3
"""Rulebook hook — three delivery lanes for team engineering rules.

Lanes (the mode argument):
  session  SessionStart: posture rules (on="session") in full, everything else
           as ONE compact index line. Session start is the weakest attention
           slot, so it carries worldview, never enforcement.
  pre      PreToolUse: advisories at the violation moment (on="bash", "edit",
           "read", "write_stdlib") and the ordering-rule GATE (on="ordering").
           A read rule sees the Read tool AND shell reads (cat/head/sed -n …).
  post     PostToolUse: advisories on failing results (on="result");
           ordering-rule ARM (edit-family) and RECEIPT (bash).
  fetch    Refresh the server book for one repo (GET /rules?view=hook with
           If-None-Match) into <BASE>/book/<repo>.json. Spawned DETACHED.
  flush    Stop / SessionEnd: POST unsent ledger rows to /fires in batches
           behind a sent-watermark (ledger/.sent); `flush final` ignores the
           throttle. Open obligations still unconverted at the second Stop
           after their fire are first recorded `converted=false`; a later
           conversion still wins (the server keeps a true over a false).

Book = the server book, cached with its ETag. SessionStart re-fetches a stale
one BEFORE the digest renders; the pre lane refreshes it in the background once
it is a minute old. Offline → the cached book; no cache → no rules. Rules are
authored through the memhub `create_rule` tool, never a local file.

One book, several rulebooks: the fetched book is the union of every rulebook
that binds the person, each rule carrying `rulebook_id` and a `rulebook` block
(`name`, `scope`, `member_count`). The server computes no precedence (D14);
`book_rank` orders wider books first so the caps spend on the policy binding
the most people — an ordering, never a suppression. A backend that sends no
book facts ranks every rule alike, and the stable sorts keep old behaviour.

How a fire reaches people (spec §5.3):
  * Every fire is DISCLOSED as `📏 Rule fired: <rule, ≤ 20 words>` (`⛔️` when
    a gate stopped the call): to the user in `systemMessage`, and to the agent,
    told to echo the byte-identical line, since only the agent's copy reaches
    the transcript. `disclosure_line` builds both.
  * `mode: gate` rules BLOCK (`permissionDecision: deny`) with the override
    their lane accepts: Bash takes `RULEBOOK_OVERRIDE='<why>' <command>`; an
    edit takes a `rulebook-override[<rule>]: <why>` marker in the content
    (named, because it stays in the diff); a Read is retried narrower,
    delegated, or run as an overridden `cat`. The fire records its
    `override_reason`; the next matching call is gated again. Gates are never
    deduped nor capped. Result rules and the shell-written-files lane run
    after the fact and cannot gate.
  * A gate is honoured from whatever book is cached, however old: a stale gate
    costs one override, a gate that silently lapses offline is the failure a
    gate exists to prevent.

What leaves the machine, exactly:
  * fetch  — the repo name, nothing else.
  * fires  — identifiers only (rule, session, repo, branch, tool, timestamps,
             judge score/verdict). The `excerpt` stays in the LOCAL ledger.
  * anchors — nothing. Anchor rules are matched on this machine against the
             cached book; `MEMHUB_RULEBOOK_RECALL=0` turns the lane off.
  * judge  — `POST /judge` sends the person's message (≤ 2000 chars), the
             stripped turn, the call (≤ 600 chars) and the fired rule ids,
             through the harness window's denylists — a floor, not a
             guarantee. An org without `rule_judge` gets `disabled`, and the
             hook then asks at most once per ten minutes per machine. One
             verdict per rule per turn is cached. `MEMHUB_RULEBOOK_JUDGE=0`
             turns it off.

Usage (wired in hooks.json): printf %s "$IN" | python3 rulebook_hook.py {session|pre|post}

State lives under $MEMHUB_RULEBOOK_BASE, else rulebook_paths.base().
Stdlib only; every failure path exits 0 with no output.

Two engines, one evaluate():
  * matcher rules — `evaluate()` is a pure function of (rule, event).
  * ordering rules — "run X after the last edit, before Y": an obligation
    state machine keyed by (worktree_root, branch, rule), never by session,
    so receipts from subagents and sibling sessions count.

A matcher rule's `given` block adds predicates checked AFTER its regex matched
— `repo`, `user`, `file` and `agent` facts, answered lazily by `Probes`. A
fact that cannot be established never satisfies a predicate. `given_ok()` is
pure over a Probes and the event's read facts.
"""
import fnmatch
import hashlib
import importlib.util
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone

def _load_shim(name):
    """A packaged shim beside this file, loaded without broadening module
    search; None when it cannot be."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
        spec = importlib.util.spec_from_file_location("_memhub_rulebook_" + name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


portable_lock = _load_shim("portable_lock")
repo_identity = _load_shim("repo_identity")
rulebook_ledger = _load_shim("rulebook_ledger")   # ledger rotation, append lock

try:  # one dir per backend, so staging and prod never share one (rulebook_paths.py)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rulebook_paths as _rbp
    BASE, LEGACY_BASE = _rbp.base(), _rbp.legacy_ledger_base()
except Exception:
    _rbp, LEGACY_BASE = None, ""
    BASE = os.environ.get("MEMHUB_RULEBOOK_BASE") or os.path.expanduser("~/.config/memhub-plugin/rulebook")
MAX_ADVISE = 2          # per tool call — habituation guard
MAX_POSTURE = 15        # spec §2: session_context is hard-capped at 15 rules, across every book
# One budget with the session-start brief (MEMHUB_BRIEF_TOKEN_BUDGET, default
# 2,500 tokens, split 2:3 brief:rulebook — navigation spec §4); this is the
# rulebook's three fifths. The literal fallback only covers a broken sibling import.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from brief_budget import rulebook_chars as _rulebook_chars
    POSTURE_BUDGET_CHARS = _rulebook_chars()
except Exception:
    POSTURE_BUDGET_CHARS = 6000   # 2,500 tokens × 4 chars × 3/5
RESULT_WINDOW_CHARS = 8000    # result lane: scanned at EACH end, not just the tail
LOCK_WAIT_S = 0.05      # ordering state lock: fail open past this
LEDGER_SCHEMA = 2       # ledger/fires.jsonl row shape (spec §3.2)
BOOK_DIR = os.path.join(BASE, "book")

# ── forward-test redirect (spec §4.3.3) ─────────────────────────────────────
#
# /memhub:create-rule's §4b test arms its candidate in a PRIVATE base instead
# of the shared book every session reads. The redirect is keyed on the session
# cwd (the test sub-agent's scratch worktree), so only its own calls read the
# doctored book. The cwd is the host's, not the model's, so it is the trust
# boundary (see `_acted_on_dir`); a redirect file not owned by the user, or
# group-/world-writable, is ignored.
REDIRECT_NAME = "pretest-redirect.json"
REDIRECT_MAX_AGE_S = 3600    # a forgotten redirect stops steering anything after an hour
#: "" = no claim, use BASE. A LAZY handle, never a snapshot of BASE: the
#: tests rebind BASE after import, and a snapshot would quietly send their
#: ledger and state writes to the REAL base instead.
_ACTIVE_BASE = ""


def _base():
    """The base this process reads and writes under."""
    return _ACTIVE_BASE or BASE
REFRESH_AFTER_S = 60         # pre lane: refresh a book this old in the background…
REFRESH_RETRY_S = 60         # …and retry no more than this often while the server is down
SESSION_FETCH_TIMEOUT_S = 1.0   # session lane: the ONE blocking fetch, and only on a stale book
API_PATH = "/v1/team/rulebook"


def _timeout(default):
    """Network timeouts, overridable for tests; a bad value is the default."""
    try:
        v = float(os.environ.get("MEMHUB_RULEBOOK_TIMEOUT_S", ""))
        return v if v > 0 else default
    except ValueError:
        return default


FETCH_TIMEOUT_S = _timeout(5.0)    # detached child; bounds how long a dead server is probed
FLUSH_TIMEOUT_S = _timeout(20.0)   # per batch, inside an async 60 s hook
FLUSH_EVERY_FIRES = 10       # Stop-hook throttle: flush when this many rows wait…
FLUSH_EVERY_S = 300          # …or this long has passed since the last flush
FLUSH_BATCH = 200
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
READ_TOOLS = ("Read",)
BASH_READ_MAX_FILES = 8            # read segments named per Bash call; past that it is a script, not a read
READ_COUNT_MAX_BYTES = 64 << 20    # lines are counted this far; a file past it is over any threshold anyone sets
# A Bash call that wrote files is an edit too (see `bash_written_files`).
BASH_EDIT_MAX_FILES = 40            # more than this in one call is a generator, not an edit
BASH_EDIT_MAX_BYTES = 512 * 1024    # per file; bigger is data, not source
BASH_EDIT_MAX_STATUS = 4000         # `git status` entries; past that the tree is too noisy to read
BASH_EDIT_MARKS_KEPT = 8            # pre-call timestamps kept per session (parallel calls)
# Tree rewrites: every touched file has a new mtime but nobody EDITED it, and an
# edit rule read against a checked-out file is a fire about someone else's code.
_TREE_REWRITE_RX = re.compile(
    r"(?:^|[;&|(]\s*)git\s+(?:-C\s+\S+\s+)?(?:checkout|switch|stash|merge|rebase|pull|reset"
    r"|cherry-pick|revert|apply|am|restore|worktree)\b", re.M)
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {
    "abc", "argparse", "ast", "asyncio", "base64", "collections", "contextlib",
    "csv", "dataclasses", "datetime", "enum", "functools", "glob", "hashlib",
    "io", "itertools", "json", "logging", "math", "os", "pathlib", "re",
    "shutil", "signal", "socket", "sqlite3", "string", "subprocess", "sys",
    "tempfile", "textwrap", "threading", "time", "traceback", "types",
    "typing", "unittest", "urllib", "uuid", "warnings",
}
LOCAL_PKGS = {"xmem", "evaluation", "tests", "app", "scripts"}


# ── shell-only segment ──────────────────────────────────────────────────────
_HD_OPEN = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")   # delimiter must be a word, so `x << 2` is a shift


def shell_only(cmd):
    """Drop heredoc BODY lines; keep every shell line, including commands
    after a terminator (`commit -F - <<'MSG' … && git push`).
    Known edge: a bit-shift in a multi-line command can arm a bogus skip."""
    out, skip_until = [], None
    for line in cmd.split("\n"):
        if skip_until is not None:
            if line.strip() == skip_until:
                skip_until = None
            continue
        out.append(line)
        m = _HD_OPEN.search(line)
        if m:
            skip_until = m.group(2)
    return "\n".join(out)


# Everything `_SEPARATOR_RX` knows EXCEPT the single pipe, which is left in
# so a piped command stays one segment for the receipt test below to refuse.
# A bare `&` belongs here: `git fetch & true` is two commands, and reading it
# as one let `true`'s exit 0 vouch for a fetch that was still running.
_LAST_SEG_SPLIT_RX = re.compile(r"&&|\|\||;|\n|(?<![>&])&(?![>&])")


def last_segment(shell):
    """The final command segment of a shell string (split on ;, &&, ||, a
    background `&`, newline). Separators are located in the blanked copy, so
    one written inside a quoted argument is the data it is, and the segment
    itself is sliced out of the original."""
    text = trim_terminators(shell)
    end = 0
    for m in _LAST_SEG_SPLIT_RX.finditer(blank_quoted(text)):
        end = m.end()
    return text[end:].strip()


def and_only_segments(shell):
    """The segments of a chain joined ONLY by `&&`, or [] when it is anything
    else (a pipe, a `;`, a `||`, a background `&`, a second line).

    Such a chain that exits 0 ran every segment successfully, so for that
    shape only the call's exit status is each segment's own.

    Separators are classified on the BLANKED copy: `npm test -- --grep 'a|b'
    && git push` is a chain whose test DID pass, and gating it would fire at
    someone who has complied."""
    shell = trim_terminators(shell)
    blank = blank_quoted(shell)
    # Any separator that is NOT `&&` disqualifies the chain — asked of
    # `_SEPARATOR_RX`, which knows a redirection `2>&1` is not a background `&`.
    if any(m.group(0) != "&&" for m in _SEPARATOR_RX.finditer(blank)):
        return []
    out, pos = [], 0
    for m in _AND_RX.finditer(blank):
        out.append(shell[pos:m.start()])
        pos = m.end()
    out.append(shell[pos:])
    return [x.strip() for x in out if x.strip()]


# Quoted spans, blanked to spaces IN PLACE. `shlex` would tokenise properly
# but throws on the half-quoted strings real commands contain, and this only
# needs the contents neutralised, not the tokens. Length-preserving on
# purpose: the separator scans below find operators in the blanked copy and
# slice the ORIGINAL at those offsets, so quoted text survives intact.
# A double-quoted span honours backslash escapes; a single-quoted span has
# none in POSIX shell.
_QUOTED_SINGLE = r"'[^']*'"
_QUOTED_DOUBLE = r'"(?:\\.|[^"\\])*"'
_QUOTED_RX = re.compile(_QUOTED_SINGLE + "|" + _QUOTED_DOUBLE)
# `&&` and `||` first, so the lone-operator alternatives only see what is
# left. A standalone `&` backgrounds the command to its left and the next one
# runs anyway — `gh pr view -R other & git push` is TWO commands. The
# lookarounds keep redirection out: `2>&1`, `cmd >&2`, `cmd &> log`.
_SEPARATOR_RX = re.compile(r"&&|\|\||;|\n|\||(?<![>&])&(?![>&])")
_AND_RX = re.compile(r"&&")
_ESCAPE_RX = re.compile(r"\\.", re.S)   # a backslash escape, outside quotes
# A `#` starts a comment at the start of a word: after whitespace, an
# operator, or a grouping paren/brace. NOT after `{` — `${#files}` is a length.
_COMMENT_RX = re.compile(r"(?<![^\s;&|()}])#[^\n]*")
# A trailing `;` or newline ends the last command; it does not start an empty
# one (`pytest;` is a run of pytest). A trailing `&` is NOT a terminator.
_TRAILING_TERMINATOR_RX = re.compile(r"[\s;\n]+$")


def trim_terminators(shell):
    return _TRAILING_TERMINATOR_RX.sub("", shell or "")


def blank_quoted(text):
    """`text` with the contents of quoted spans — and every backslash escape
    outside them — replaced by spaces, character for character, so every
    offset still points at the same place.

    A `\\|` is not a pipe: an escape is data, just as quoting is."""
    return _COMMENT_RX.sub(lambda m: " " * len(m.group(0)), blank_syntax(text))


def blank_syntax(text):
    """Quotes and escapes blanked, comments LEFT IN PLACE, length preserved.

    Exposed for `strip_comments`. Escapes become a NON-space placeholder,
    because `\\ ` joins two words in bash (`.title\\ #literal` is no comment)."""
    blanked = _QUOTED_RX.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2)
                             + m.group(0)[-1], text or "")
    return _ESCAPE_RX.sub("\x01\x01", blanked)


def unquoted(text):
    """`text` with the CONTENTS of quoted spans removed.

    Used on the two paths that let a call OUT of a gate (self-discharge and
    the receipt), where an argument that merely spells a command
    (`echo 'git fetch' && git log origin/main`) must not count. A genuinely
    quoted receipt costs an extra gate rather than a missed one."""
    return blank_quoted(text)


def executes(segment, rx):
    """Does this segment run the command `rx` describes?

    SYNTAX ONLY: blanks quoted spans and comments, drops leading `FOO=1`
    assignments and grouping braces, and searches the rest. There is no list
    of runners, so `timeout 300 pytest` and any unknown wrapper discharge.

    KNOWN AND ACCEPTED RESIDUAL: an UNQUOTED mention (`echo pytest`)
    discharges; a quoted one or a comment does not. The obligation is
    advisory and the recorded `RULEBOOK_OVERRIDE=` door exists anyway; what
    is closed is the accidental bypass."""
    # Grouping is not part of a command's name — `(git fetch -q)` is a receipt.
    text = strip_leading_assignments(
        unquoted(segment or "").strip("(){} \t")).strip()
    # `!` inverts a pipeline's status — a negated segment vouches for nothing.
    if text.startswith("!"):
        return False
    return bool(text) and bool(re.search(rx, text))


def self_discharging(shell, spec):
    """Does this one call run the required command BEFORE the gated one, in a
    chain whose single exit status vouches for the required part?

    `&&`-only, with the required segment first: only then can the gated
    segment not run unless the required one ran and passed."""
    segs = and_only_segments(shell)
    required = next((i for i, part in enumerate(segs)
                     if executes(part, spec["required_command_rx"])), None)
    if required is None:
        return False
    return any(command_fires(spec["gated_command_rx"], unquoted(part), flags=0)
               for part in segs[required + 1:])


def receipt_segments(shell, whole_chain=False):
    """The segments of `shell` whose success the call's exit status vouches
    for: the last segment, unpiped, not backgrounded. `whole_chain` widens it
    to every segment of an `&&`-only chain (see `and_only_segments`), which
    the session- and prompt-armed rules need: their required command comes
    FIRST (`git fetch -q && git log origin/main`)."""
    shell = trim_terminators(shell)
    if whole_chain:
        segs = and_only_segments(shell)
        if segs:
            return segs
    # The last segment is a receipt only if it NECESSARILY ran: after `||` it
    # ran only when the one before it FAILED (`true || git fetch`).
    # Joiners use `last_segment`'s splitter (a single `|` is not one).
    joiners = [m.group(0) for m in _LAST_SEG_SPLIT_RX.finditer(blank_quoted(shell))]
    if joiners and joiners[-1] == "||":
        return []
    # A call ending in a background `&` yields an empty last segment — no
    # receipt, since `git fetch &` exits 0 from launching the job.
    last = last_segment(shell)
    # On the blanked copy: `npm test -- --grep 'a|b'` is not a pipeline.
    if last and "|" not in blank_quoted(last):
        return [last]
    return []


# ── a leading assignment is not part of the command ─────────────────────────
#
# `FOO=1 git push` execs `git push`, and a rule has to read it the same way,
# or an ANCHORED rule (`^git\s+push`) is silently bypassed by any prefix —
# including a refused `RULEBOOK_OVERRIDE=` that `strip_override` leaves in.
_ASSIGN_TOKEN_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S*)\s*")
_ASSIGN_NAME_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def strip_leading_assignments(shell):
    """`shell` with the env assignments that BEGIN a segment removed, byte for
    byte identical everywhere else.

    Tokenised per line by `find_override`'s shlex walk, so a quoted
    assignment is data and an unparseable line is handed back untouched. A
    run is stripped whole (`FOO=1 BAR=2 git push` -> `git push`)."""
    out = []
    for line in shell.split("\n"):
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            toks = list(lex)
        except ValueError:                  # unbalanced quoting: not ours to rewrite
            out.append(line)
            continue
        targets, at_start = [], True
        for tok in toks:
            if at_start and _ASSIGN_NAME_RX.match(tok):
                targets.append(tok)         # stay at_start: assignments come in runs
                continue
            at_start = _segment_op(tok)
        stripped = line
        for val in targets:
            for m in _ASSIGN_TOKEN_RX.finditer(stripped):
                try:
                    if shlex.split(m.group(0))[0] != val:
                        continue
                except (ValueError, IndexError):
                    continue
                stripped = stripped[:m.start()] + stripped[m.end():]
                break
        out.append(stripped)
    return "\n".join(out)


def strip_comments(text):
    """`text` with `#` comments blanked and quotes left INTACT, length
    preserved.

    A rule may see inside quotes, so unlike `blank_quoted` only comments are
    blanked (found via `blank_syntax`). A `#` that STARTS a word comments out
    the rest of the line; mid-word (`%h#%s`, a URL fragment) it does not."""
    out = list(text or "")
    for m in _COMMENT_RX.finditer(blank_syntax(text)):
        for i in range(m.start(), m.end()):
            out[i] = " "
    return "".join(out)


def command_fires(rx, text, not_rx=None, flags=re.I | re.M):
    """Does `rx` match this command, given that a leading env assignment is not
    part of it, and that a `#` comment is not part of it either?

    Comments are blanked HERE, the one place both the matcher lane and the
    ordering gate ask "does this command match".

    The command is read as BOTH forms — as written, and with leading
    assignments removed — and `rx` fires when EITHER matches, so a prefix
    cannot bypass an anchored rule while a rule about the assignment itself
    still fires. `not_rx` is a VETO across the same pair, checked first, so a
    prefix cannot delete the token an exemption keys on."""
    text = strip_comments(text)
    forms = [text]
    bare = strip_leading_assignments(text)
    if bare != text:
        forms.append(bare)
    if not_rx and any(re.search(not_rx, f, re.I) for f in forms):
        return False
    return any(re.search(rx, f, flags) for f in forms)


# ── files a Bash call wrote ─────────────────────────────────────────────────
#
# A model writes files through Bash too (`cat > f <<EOF`, `sed -i`, a Python
# heredoc). The command line cannot recover the path, so this reads the DISK:
# the pre lane stamps the call, the post lane asks git what changed since and
# feeds each file through the same matcher a Write goes through. It lands
# AFTER the write, so it advises only.

def _worktrees(root):
    """Every worktree of `root`'s repository, `root` first. Empty on any failure."""
    try:
        p = subprocess.run(["git", "-C", root, "worktree", "list", "--porcelain"],
                           capture_output=True, text=True, timeout=3)
    except Exception:
        return [root]
    if p.returncode != 0:
        return [root]
    seen, real = [root], {os.path.realpath(root)}
    for line in p.stdout.splitlines():
        if line.startswith("worktree ") and os.path.realpath(line[9:]) not in real:
            seen.append(line[9:])
            real.add(os.path.realpath(line[9:]))
    return seen


def _names_of(path):
    """The spellings a command might use for `path`: as given, resolved, and —
    macOS — with or without the `/private` prefix git resolves /tmp and /var to."""
    names = {path, os.path.realpath(path)}
    for n in list(names):
        if n.startswith("/private/"):
            names.add(n[len("/private"):])
    return names


_PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$")
_PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+?)\s*$")


def apply_patch_files(inp, cwd):
    """(path, is_new, added text) for each file a Codex `apply_patch` names.

    Codex's patch arrives in `tool_input.command` with no `file_path` (paths
    may be cwd-relative); read here, it reaches the edit lane and arms
    ordering obligations like Claude's Edit. A moved file is judged at its
    new path; a deleted one is an edit with nothing added."""
    patch = inp.get("command") if isinstance(inp, dict) else None
    if not isinstance(patch, str) or "*** Begin Patch" not in patch:
        return []
    files, current = [], None
    for line in patch.splitlines():
        head = _PATCH_FILE.match(line)
        if head:
            current = [head.group(2), head.group(1) == "Add", []]
            files.append(current)
            continue
        move = _PATCH_MOVE.match(line)
        if move and current is not None:
            current[0] = move.group(1)
        elif current is not None and line.startswith("+"):
            current[2].append(line[1:])
    base = cwd or os.getcwd()
    return [(os.path.normpath(os.path.join(base, path)), is_new, "\n".join(added))
            for path, is_new, added in files[:BASH_EDIT_MAX_FILES]]


def bash_written_files(root, cmd, since):
    """(path, is_new) for each regular file a Bash call left modified or new.

    Scanned: the session's worktree, plus any sibling worktree the command
    names (not all of them — one `git status` each per call is too costly).
    `git status` decides candidates (so .gitignore excludes), the mtime what
    THIS call touched. Past BASH_EDIT_MAX_FILES it returns [] — a generator
    or tree rewrite, not an edit.
    """
    if not root or since is None or _TREE_REWRITE_RX.search(shell_only(cmd or "")):
        return []
    roots = [w for w in _worktrees(root)
             if w == root or any(n in (cmd or "") for n in _names_of(w))]
    out = []
    for wt in roots:
        try:
            p = subprocess.run(["git", "-C", wt, "status", "--porcelain=v1", "-z",
                                "--untracked-files=all"], capture_output=True, timeout=5)
        except Exception:
            continue
        if p.returncode != 0:
            continue
        entries = p.stdout.split(b"\0")
        if len(entries) > BASH_EDIT_MAX_STATUS:
            continue
        skip_next = False
        for e in entries:
            if skip_next:              # the OLD name of a rename/copy: a bare path, no code
                skip_next = False
                continue
            if len(e) < 4:
                continue
            code, rel = e[:2], e[3:]
            skip_next = code[0:1] in (b"R", b"C")
            if b"D" in code:
                continue
            is_new = code == b"??" or code[0:1] == b"A"
            path = os.path.join(wt, rel.decode("utf-8", "replace"))
            try:
                st = os.stat(path)
            except OSError:
                continue
            # No slack on the stamp, or the PREVIOUS call's file would count;
            # a coarse filesystem can under-count, the safe direction.
            if not stat.S_ISREG(st.st_mode) or st.st_mtime < since:
                continue
            out.append((path, is_new))
            if len(out) > BASH_EDIT_MAX_FILES:
                return []
    return out


# ── files a Bash call READS into context ────────────────────────────────────
#
# A read rule (`event: read`) is about what enters the model's context: the
# Read tool, and `cat`/`head`/`tail`/`less`/`more`/`sed` on a path (most of
# the largest tool results are whole-file shell dumps).
#
# Read from the COMMAND, not the disk, because this lane must gate. A pipe or
# redirect is not a read into context, and every doubt resolves to "no read
# here" — under-count, never false-fire. `cd` is tracked segment by segment
# (`cd <repo> && cat spec.md`).

_READ_CMDS = ("cat", "head", "tail", "less", "more", "sed")
# stdout going somewhere other than the context: `> f`, `>> f`, `&> f`, and a
# heredoc opener (its stdin is data). `2>/dev/null` and `2>&1` are not that.
_REDIRECT_RX = re.compile(r"(?<![0-9&<])>(?!&)|&>|<<")
_SED_RANGE_RX = re.compile(r"^(\d+)(?:,(\+)?(\d+|\$))?p$")
_LINES_FLAG_RX = re.compile(r"^(?:-n|--lines=)(\d+)$|^-(\d+)$")


def _head_tail_lines(args):
    """(lines printed, indices of the flag VALUES) for `head`/`tail`: 10 by
    default, `-N`, `-n N`, `-nN`, `--lines=N`. (None, None) for a form this
    does not name (`-c` bytes, `-n +N`) — not a read this can measure."""
    n, i, used = 10, 0, set()
    while i < len(args):
        a = args[i]
        if a in ("-c", "--bytes") or a.startswith("--bytes=") or a.startswith("-c"):
            return None, None
        if a in ("-n", "--lines"):
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                return None, None
            n, used, i = int(args[i + 1]), used | {i + 1}, i + 2
            continue
        m = _LINES_FLAG_RX.match(a)
        if m:
            n = int(m.group(1) or m.group(2))
        i += 1
    return n, used


def _sed_lines(args):
    """(lines printed, indices that are not files) for a `sed` that prints
    to stdout. `-n 'A,Bp'` is a range, `-n 'Ap'` one line, `-n '1,$p'` the
    whole file; `sed s/a/b/ f` with no -n prints every line. `-i` writes in
    place and prints nothing, and a script this cannot read is not a read —
    both answer (None, None)."""
    if any(a == "-i" or a.startswith("-i") or a == "--in-place" for a in args):
        return None, None
    quiet, script, used, i = False, None, set(), 0
    while i < len(args):
        a = args[i]
        if a in ("-n", "--quiet", "--silent"):
            quiet = True
        elif a in ("-e", "--expression", "-f", "--file"):
            if i + 1 < len(args):
                used.add(i + 1)
                if a in ("-e", "--expression"):
                    script = args[i + 1]
            i += 2
            continue
        elif a.startswith("-"):
            pass
        elif script is None:
            script, used = a, used | {i}
        i += 1
    if script is None:
        return None, None
    if not quiet:
        return None, used               # prints the whole file, transformed
    m = _SED_RANGE_RX.match(script.replace(" ", ""))
    if not m:
        return None, None               # `/x/,/y/p` and friends: not a read we can measure
    a, plus, b = int(m.group(1)), m.group(2), m.group(3)
    if b is None:
        return 1, used
    if b == "$":
        return None, used
    return (int(b) + 1 if plus else max(int(b) - a + 1, 0)), used


def bash_reads(cwd, cmd):
    """(path, pulled) for each file a Bash call would print into the context;
    `pulled` is the line count the form asks for, None meaning every line.
    Relative paths resolve where the command runs, `cd` included. Anything
    this cannot name is not a read — the list is empty on every doubt."""
    out = []
    here = os.path.expanduser(cwd or "") or os.getcwd()
    for seg in re.split(r"&&|\|\||;|\n", shell_only(cmd or "")):
        seg = strip_leading_assignments(seg.strip()).strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        if not toks:
            continue
        if toks[0] == "cd":
            target = os.path.expanduser(toks[1]) if len(toks) > 1 else os.path.expanduser("~")
            here = target if os.path.isabs(target) else os.path.normpath(os.path.join(here, target))
            continue
        if "|" in seg or _REDIRECT_RX.search(seg):
            continue
        name = os.path.basename(toks[0])
        if name not in _READ_CMDS:
            continue
        args, pulled, used = toks[1:], None, set()
        if name in ("head", "tail"):
            pulled, used = _head_tail_lines(args)
            if used is None:
                continue
        elif name == "sed":
            pulled, used = _sed_lines(args)
            if used is None:
                continue
        # a token with `<` or `>` in it is a redirection operand (`2>/dev/null`),
        # never a file to read
        files = [a for i, a in enumerate(args)
                 if i not in used and a != "-" and not a.startswith("-") and "<" not in a and ">" not in a]
        for f in files:
            path = os.path.expanduser(f)
            if not os.path.isabs(path):
                path = os.path.normpath(os.path.join(here, path))
            out.append((path, pulled))
            if len(out) >= BASH_READ_MAX_FILES:
                return out
    return out


def read_facts(path, pulled=None, offset=None, limit=None, total=None):
    """{"lines": n, "bytes": b} — what this read would pull into the context:
    the file's length, narrowed by `offset`/`limit` or a shell form's line
    count. None (which satisfies no `given.file` predicate) for a non-regular
    file. `total` pre-answers the line count, for the verifier."""
    try:
        size = None
        if total is None:
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode):
                return None
            size, total, seen, last = st.st_size, 0, 0, b"\n"
            with open(path, "rb") as f:
                while seen < READ_COUNT_MAX_BYTES:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    total += chunk.count(b"\n")
                    seen += len(chunk)
                    last = chunk[-1:]
            if seen and last != b"\n":
                total += 1                # a last line without a newline is still a line
        total = int(total)
        n = total
        if offset is not None:
            try:
                n = max(total - max(int(offset), 1) + 1, 0)
            except (TypeError, ValueError):
                pass
        for cap in (limit, pulled):
            if cap is not None:
                try:
                    n = min(n, max(int(cap), 0))
                except (TypeError, ValueError):
                    pass
        nbytes = None
        if size is not None:
            nbytes = size if n >= total else (int(size * n / total) if total else 0)
        return {"lines": n, "bytes": nbytes}
    except Exception:
        return None


def added_lines(old, new):
    """The lines of `new` that `old` does not have — what an Edit wrote, by the
    same set test the bash lane's `body_rx` uses for a heredoc. A line moved or
    kept verbatim is not added; one changed by a character is."""
    kept = set(str(old or "").split("\n"))
    return "\n".join(line for line in str(new or "").split("\n") if line not in kept)


def edit_added_text(inp):
    """What an Edit / MultiEdit / Write call ADDED, for an edit rule's
    `content_rx`: each new_string minus its own old_string's lines, a Write's
    whole content (its previous file is not read here). Same order and joins
    as the call's body."""
    edits = [e for e in (inp.get("edits") or []) if isinstance(e, dict)]
    return added_lines(inp.get("old_string"), inp.get("new_string", "")) + \
        str(inp.get("content", "")) + \
        "\n".join(added_lines(e.get("old_string"), e.get("new_string", "")) for e in edits)


def read_edit_body(path, is_new=True):
    """What an edit rule reads for a Bash-written file, matching what it
    reads for the tools: a NEW file is the whole file (a Write), a MODIFIED
    file only the lines this change added (an Edit's new_string). None when
    binary, past BASH_EDIT_MAX_BYTES, or the diff cannot be read."""
    try:
        with open(path, "rb") as f:
            raw = f.read(BASH_EDIT_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > BASH_EDIT_MAX_BYTES or b"\0" in raw:
        return None
    if is_new:
        return raw.decode("utf-8", "replace")
    try:      # against HEAD, so a `git add` inside the same call changes nothing
        p = subprocess.run(["git", "-C", os.path.dirname(path), "diff", "HEAD", "--no-color",
                            "--no-ext-diff", "-U0", "--", path],
                           capture_output=True, timeout=5)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    added = [l[1:] for l in p.stdout.decode("utf-8", "replace").split("\n")
             if l.startswith("+") and not l.startswith("+++")]
    return "\n".join(added)


# ── matcher engine: pure ────────────────────────────────────────────────────
def evaluate(rule, *, hook_phase, tool, cmd="", file_path="", body="", result_text="", prompt="",
             added=None):
    """True if `rule` fires on this event. No I/O, no dedup. Ordering rules are not matchers (see
    OrderingEngine). `added` is what an edit ADDED (`edit_added_text`); None reads `body`."""
    on = rule.get("on")
    try:
        if hook_phase == "prompt" and on == "prompt" and prompt:
            # The RAW prompt, harness wrappers included (unlike `harness_prompt`).
            if not re.search(rule["rx"], prompt, re.I | re.M):
                return False
            return not (rule.get("not_rx") and re.search(rule["not_rx"], prompt, re.I | re.M))
        if hook_phase == "pre" and on == "bash" and tool == "Bash" and cmd:
            # Rules ABOUT payloads (`body_rx`): rx still names the shell shape
            # (`python - <<`), body_rx says what the payload must be about — so
            # a spec file that merely *contains* "python3 - <<" never fires.
            # Legacy `match_heredoc_body` without body_rx matches the whole string.
            shell = shell_only(cmd)
            target = cmd if (rule.get("match_heredoc_body") and not rule.get("body_rx")) else shell
            if not command_fires(rule["rx"], target, rule.get("not_rx")):
                return False
            if rule.get("body_rx"):
                kept = set(shell.split("\n"))
                body_only = "\n".join(l for l in cmd.split("\n") if l not in kept)
                return bool(re.search(rule["body_rx"], body_only, re.I | re.M))
            return True
        if hook_phase == "pre" and on == "edit" and tool in EDIT_TOOLS:
            # The server files an edit rule with path_rx OR content_rx, so a
            # content-only rule has no path_rx: it means any path, not none.
            if re.search(rule.get("path_rx") or "", file_path) and not (
                    rule.get("path_not_rx") and re.search(rule["path_not_rx"], file_path)):
                # content_rx asks what this edit WROTE: a line the old text
                # already had is not this edit's doing (an Edit that kept a
                # `create_task` call was blocked as if it added one).
                written = body if added is None else added
                if "content_rx" in rule and not re.search(rule["content_rx"], written, re.M):
                    return False
                # content_not_rx exempts the whole edit (the complied-with form).
                return not (rule.get("content_not_rx")
                            and re.search(rule["content_not_rx"], body, re.M))
            return False
        if hook_phase == "pre" and on == "read" and tool in READ_TOOLS and file_path:
            # Which file, not how much (size is a `given.file` fact). A shell
            # read honours the rule's command exemption.
            if rule.get("path_rx") and not re.search(rule["path_rx"], file_path):
                return False
            if rule.get("path_not_rx") and re.search(rule["path_not_rx"], file_path):
                return False
            if cmd and rule.get("not_rx") and re.search(rule["not_rx"], cmd, re.I):
                return False
            return True
        if hook_phase == "pre" and on == "write_stdlib" and tool == "Write" \
                and file_path.endswith(".py") and "scratchpad" not in file_path \
                and not (rule.get("path_not_rx") and re.search(rule["path_not_rx"], file_path)) \
                and len(body) >= rule.get("min_chars", 800):
            mods = set(re.findall(r"^(?:import|from)\s+([A-Za-z_]\w*)", body, re.M))
            return bool(mods) and not {m for m in mods if m not in STDLIB and m not in LOCAL_PKGS}
        if hook_phase == "post" and on == "result" and result_text:
            if rule.get("cmd_rx") and not re.search(rule["cmd_rx"], cmd, re.I):
                return False
            if rule.get("cmd_not_rx") and cmd and re.search(rule["cmd_not_rx"], cmd, re.I):
                return False          # the server's command_not_rx, honoured on the post lane too
            # Scan BOTH ends of a long result (pytest prints the traceback at
            # the top, the summary at the bottom), as separate spans.
            if len(result_text) <= 2 * RESULT_WINDOW_CHARS:
                spans = (result_text,)
            else:
                spans = (result_text[:RESULT_WINDOW_CHARS],
                         result_text[-RESULT_WINDOW_CHARS:])
            # exclude_rx exempts the whole result, over the same spans
            if rule.get("exclude_rx") and any(
                    re.search(rule["exclude_rx"], sp, re.M) for sp in spans):
                return False
            return any(re.search(rule["rx"], sp, re.M) for sp in spans)
    except Exception:
        return False
    return False


# ── ordering engine: obligation state machine ───────────────────────────────


class OrderingEngine:
    """State file per worktree root; inside it {"*": {rule_id: {count, last_edit}}}.
    Keyed by WORKTREE, not branch: a working tree carries uncommitted edits
    across `git checkout -b`, so a branch-keyed obligation would vanish on a
    branch switch before the push. Sibling branches share it (over-gates
    slightly — the safe direction).
    Every read-modify-write holds an exclusive flock on a sidecar lock (bounded
    LOCK_WAIT_S, then fail open) and replaces the file atomically, so an arm
    and a discharge from two sessions never overwrite each other.

    Arming counters ONLY: a receipt is posted as an event carrying this
    checkout's key and the server matches it to the rule's fires
    (rule-fire-events-spec §4)."""

    def __init__(self, worktree_root, branch):
        os.makedirs(os.path.join(_base(), "state"), exist_ok=True)
        self.path = os.path.join(_base(), "state",
                                 f"wt-{worktree_key(worktree_root)}.json")
        self.branch = "*"            # branch is recorded on fires, not used as a key

    def _locked(self):
        if portable_lock is None:
            # Matcher gates need no shared state; only ordering gates fail open.
            return None
        deadline = time.monotonic() + LOCK_WAIT_S
        replaced = 0
        while True:
            lock = open(self.path + ".lock", "a+", encoding="utf-8")
            try:
                portable_lock.lock_exclusive(lock.fileno(), blocking=False)
            except OSError:
                lock.close()
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.005)
                continue
            if portable_lock.still_at(lock.fileno(), self.path + ".lock") \
                    or (replaced >= 2 and time.monotonic() >= deadline):
                # Past the deadline after two reopens, keep it (unstable
                # inode numbers would otherwise spin).
                return lock
            # state_sweep.py deleted this lock file after we opened it: reopen.
            replaced += 1
            portable_lock.unlock(lock.fileno())
            lock.close()

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, st):
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".wt-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, self.path)

    def feed(self, rule, *, hook_phase, tool, cmd="", file_path="", ok=None, armed=None):
        """Returns "fired" | "allowed" | "discharged" | None. Mutates state
        under lock; None on lock timeout (fail open).

        `armed` is "session" or "prompt" when the obligation was armed in the
        SESSION's own state (read by the caller): such an obligation belongs
        to one session, not the shared worktree state."""
        spec = rule["ordering"]
        armed_by = tuple(spec.get("armed_by_events", ("edit", "write")))
        by_call = any(k in armed_by for k in ("session", "prompt"))
        is_edit = tool in EDIT_TOOLS and hook_phase == "post"
        if is_edit and not any(k in armed_by for k in ("edit", "write")):
            return None
        if is_edit and spec.get("path_rx") and not re.search(spec["path_rx"], file_path):
            return None
        seg = shell_only(cmd) if cmd else ""
        # A Bash call reports ONE exit status, the receipt's own only for an
        # unpiped final segment; session/prompt-armed rules read an `&&`-only
        # chain whole (`receipt_segments`).
        is_receipt = hook_phase == "post" and tool == "Bash" and seg and \
            any(executes(part, spec["required_command_rx"])
                for part in receipt_segments(seg, whole_chain=by_call))
        is_gate = hook_phase == "pre" and tool == "Bash" and seg and \
            command_fires(spec["gated_command_rx"], seg, flags=0)
        # One call that runs the required command BEFORE the gated one
        # discharges its own obligation (`git fetch -q && git log origin/main`).
        # ORDER and JOINER both have to hold (see `self_discharging`).
        if is_gate and by_call and self_discharging(seg, spec):
            return None
        if not (is_edit or is_receipt or is_gate):
            return None

        lock = self._locked()
        if lock is None:
            return None
        try:
            st = self._read()
            s = st.setdefault(self.branch, {}).setdefault(
                rule["id"], {"count": 0, "last_edit": None})
            if is_edit:                                   # handler 1: mutation arms
                s["count"] += 1
                s["last_edit"] = file_path
                self._write(st)
                return None
            if is_receipt:                                # handler 2: green receipt
                if ok is True:                            # a red run never discharges
                    s["count"] = 0
                    # Older builds kept open fire ids here; posted without a
                    # checkout key, a `receipt` cannot reach them, so they go
                    # to the caller as legacy conversions (#240).
                    legacy = [f for f in [s.get("open_fire")] + list(s.get("open_fires") or []) if f]
                    rule["_legacy_fires"] = list(dict.fromkeys(legacy))
                    for k in ("open_fire", "open_fires", "resolved_fires"):
                        s.pop(k, None)
                    self._write(st)
                    return "discharged"
                return None
            # handler 3: the gate — read-only
            name = spec.get("display_name", rule["id"])
            if s["count"] >= int(spec.get("min_edits", 1)):
                rule["_gate_msg"] = (
                    f"{s['count']} edit(s) since the last passing "
                    f"'{name}' (last: {s['last_edit']}). Run it first.")
                return "fired"
            if armed:
                since = ("this session started" if armed == "session"
                         else "your prompt armed this rule")
                rule["_gate_msg"] = f"no passing '{name}' since {since}. Run it first."
                return "fired"
            return "allowed"
        finally:
            portable_lock.unlock(lock.fileno())
            lock.close()


def bash_ok(resp, *, strict=False):
    """Did the Bash call succeed? Uses exit_code when the harness supplies it.
    Without one, the text proxy (same as the transcript replayer) is a guess a
    command's own output could forge — so `strict=True` (used for GATE-mode
    receipts) returns False unless an explicit exit_code says 0."""
    if resp is None:                      # no result at all is never a receipt
        return False
    if isinstance(resp, dict):
        if isinstance(resp.get("exit_code"), int):
            return resp["exit_code"] == 0
        if resp.get("is_error") or resp.get("isError"):
            return False
    if strict:
        return False
    txt = result_text(resp)
    # text proxy, anchored to pytest/traceback vocabulary — a green run whose
    # output merely mentions "error:" must not be mistaken for red
    return not re.search(
        r"(^|\n)(FAILED|ERROR)\b|\b\d+ (failed|errors?)\b|\nTraceback \(most recent call last\)"
        r"|(^|\n)npm ERR!|(^|\n)error(\[E\d+\])?:", txt)


# ── given: predicates a matched rule must also satisfy ──────────────────────
PROBE_TIMEOUT_S = 1.0        # per git call; a probe past it answers None, never blocks the call
_TURNS_MAX_BYTES = 16 * 1024 * 1024   # transcript larger than this: only its tail is read
_TURNS_KEEP = 200            # most recent user turns kept
_TURN_CHARS = 2000           # per turn
# value kinds per key — the same allowlist the server validates at authoring
_GIVEN = {
    "repo": {"branch_rx": "rx", "branch_not_rx": "rx", "diff_lines_gt": "int",
             "diff_files_gt": "int", "diff_paths_rx": "rx", "diff_paths_none_rx": "rx",
             "dirty": "bool", "spec_untouched": "bool", "spec_dir": "str"},
    "user": {"said_rx": "rx", "not_said_rx": "rx"},
    # what a read would pull into context — answered per EVENT (`read_facts`),
    # not per call, so a `cat a b` is measured file by file
    "file": {"lines_gt": "int", "bytes_gt": "int"},
    # main agent vs subagent (transcript under <session>/subagents/);
    # `main: true` lets a delegate's reads pass.
    "agent": {"main": "bool"},
}


_VERSION_RX = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_HOOK_VERSION = []          # memo: the manifest is read at most once per process


def version_tuple(v):
    """`major.minor.patch` as a comparable tuple, or None.

    Strict on purpose: three ASCII-digit components, no prerelease."""
    m = _VERSION_RX.match(v.strip()) if isinstance(v, str) else None
    return tuple(int(g) for g in m.groups()) if m else None


def hook_version():
    """This hook's own version, from the plugin manifest beside it.

    None when unreadable — and an unknown version satisfies no
    `min_hook_version`, so the rule degrades rather than gates."""
    if not _HOOK_VERSION:
        override = os.environ.get("MEMHUB_RULEBOOK_HOOK_VERSION")
        if override is not None:
            _HOOK_VERSION.append(version_tuple(override))
        else:
            try:
                manifest = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", ".claude-plugin", "plugin.json")
                with open(manifest, encoding="utf-8") as f:
                    _HOOK_VERSION.append(version_tuple(json.load(f).get("version")))
            except Exception:
                _HOOK_VERSION.append(None)
    return _HOOK_VERSION[0]


# Every key of an `ordering` block this hook knows how to honour; a rule
# written for a NEWER hook degrades (see `degradation`).
_ORDERING_KEYS = frozenset({"required_command_rx", "gated_command_rx", "armed_by_events",
                            "armed_by_rx", "min_edits", "display_name", "path_rx"})
_ARMED_BY_EVENTS = frozenset({"edit", "write", "session", "prompt"})


def given_unsupported(g):
    """The first `block.key` of a `given` this hook does not know, or "".

    Version skew, unlike `given_norm`'s malformed-value check; the same
    `_GIVEN` table answers both."""
    if not isinstance(g, dict):
        return ""
    for block, spec in g.items():
        kinds = _GIVEN.get(block)
        if kinds is None:
            return str(block)[:40]
        if isinstance(spec, dict):
            for k in spec:
                if k not in kinds:
                    return f"{block}.{str(k)[:40]}"
    return ""


def given_supported(g):
    """`g` with the blocks and keys this hook does not know removed. What is
    left is checked; the rule is advise-only and says why (`degradation`), so
    nothing is dropped quietly."""
    out = {}
    for block, spec in g.items():
        kinds = _GIVEN.get(block)
        if kinds is None or not isinstance(spec, dict):
            continue
        kept = {k: v for k, v in spec.items() if k in kinds}
        if kept:
            out[block] = kept
    return out


def ordering_unsupported(o):
    """The first `ordering` key — or arming event — this hook does not know."""
    if not isinstance(o, dict):
        return ""
    for k in o:
        if k not in _ORDERING_KEYS:
            return f"ordering.{str(k)[:40]}"
    events = o.get("armed_by_events")
    if isinstance(events, (list, tuple)):
        for ev in events:
            if ev not in _ARMED_BY_EVENTS:
                return f"ordering.armed_by_events:{str(ev)[:40]}"
    return ""


def degradation(row, given=None, ordering=None):
    """Why this hook cannot honour `row` in full, or "" when it can.

    `given` and `ordering` are the rule's RAW blocks. Only the caller knows
    where they sit: a server row keeps `given` inside its `matcher`, a pilot
    row at the top level.

    A rule newer than this hook (`min_hook_version` above ours, or a key we
    do not know) would otherwise run as if its condition were met. It
    degrades instead: advises, never gates, and says so once per session.

    SCOPE: FORWARD skew only. A hook older than this check cannot protect
    itself; that needs the server (hence `fetch_book` sends `hook_version`)."""
    want_raw = row.get("min_hook_version")
    if want_raw is not None:
        have = hook_version()
        want = version_tuple(want_raw)
        if want is None:
            return f"min_hook_version {str(want_raw)[:20]!r} is not major.minor.patch"
        if have is None:
            return (f"this rule needs hook {'.'.join(map(str, want))} and this hook "
                    "cannot read its own version")
        if have < want:
            return (f"this rule needs hook {'.'.join(map(str, want))}; this is "
                    f"{'.'.join(map(str, have))}")
    unknown = given_unsupported(given) or ordering_unsupported(ordering)
    return f"this hook does not understand `{unknown}`" if unknown else ""


def given_norm(g):
    """Lint a rule's `given` block off the wire. Returns the block, or None on
    an unknown sub-block, an unknown key, or a value of the wrong kind — and
    None drops the RULE, as rx_ok does, rather than firing with its
    predicate silently ignored."""
    if not isinstance(g, dict) or not g:
        return None
    out = {}
    for block, spec in g.items():
        kinds = _GIVEN.get(block)
        if kinds is None or not isinstance(spec, dict) or not spec:
            return None
        for k, v in spec.items():
            kind = kinds.get(k)
            if kind == "rx":
                if not rx_ok(v):
                    return None
            elif kind == "int":
                if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                    return None
            elif kind == "str":
                from spec_owns import safe_spec_dir
                if safe_spec_dir(v) is None:
                    return None
            elif kind == "bool":
                if not isinstance(v, bool):
                    return None
            else:
                return None
        out[block] = dict(spec)
    return out


def user_turns_of(tp):
    """What the person typed this session, oldest first. A transcript `user`
    record is also how tool results and injected context arrive, so this keeps
    only real prompts: no `toolUseResult`, no `tool_result` block, no `isMeta`
    row, no compaction summary. A substring pre-filter keeps it to one
    json.loads per candidate line. None when there is no transcript — and
    None never satisfies a `user` predicate."""
    if not tp:
        return None
    try:
        size = os.path.getsize(tp)
        turns = []
        with open(tp, "rb") as f:
            if size > _TURNS_MAX_BYTES:
                f.seek(size - _TURNS_MAX_BYTES)
                f.readline()                       # the cut line
            for raw in f:
                if b'"user"' not in raw or b'"type"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if rec.get("type") != "user" or rec.get("isMeta") \
                        or rec.get("isCompactSummary") or "toolUseResult" in rec:
                    continue
                c = (rec.get("message") or {}).get("content")
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                        continue
                    text = "\n".join((b.get("text") or "") for b in c
                                     if isinstance(b, dict) and b.get("type") == "text")
                else:
                    continue
                text = text.strip()
                if text:
                    turns.append(text[:_TURN_CHARS])
        return turns[-_TURNS_KEEP:]
    except Exception:
        return None


_ARG = r"(?:'([^']*)'|\"([^\"]*)\"|([^\s;&|]+))"
_BASE_ARG = re.compile(r"--base[=\s]+" + _ARG)
# A plain branch name, and nothing that reaches elsewhere in history: no rev
# syntax (`~ ^ : @{…}`), no path traversal, no leading dash.
_BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
# `help cd`: `cd [-L|[-P [-e]] [-@]] [dir]`. The options were being captured
# AS the directory, so `cd -P ../Other` matched nothing and the call read as
# local.
CD_SEGMENT = re.compile(r"^\s*cd(?:\s+-[LPe@]+)*\s+" + _ARG + r"\s*$")
CD_BARE = re.compile(r"^\s*cd(?:\s+-[LPe@]+)*\s*$")
# `-R` / `--repo` is read only off a `gh` segment: to grep, cp and rsync the
# same flag means --recursive, and `grep -R foo/bar .` would otherwise name a
# repo nobody mentioned.
# `gh pr view --help`: `-R, --repo [HOST/]OWNER/REPO`. The short flag also
# takes its value attached (`-Racme/repo`), which is the form a shell alias
# usually ends up with.
REPO_ARG = re.compile(r"(?:^|\s)(?:-R\s*=?\s*|--repo\s*=?\s*)" + _ARG)
GH_SEGMENT = re.compile(r"^gh\b")
# `git -h`: `git [-C <path>] [--git-dir=<path>] [--work-tree=<path>] …`, and
# GIT_DIR / GIT_WORK_TREE do the same from the environment. Each points git at
# a tree this cannot name as a repo, so each refuses.
GIT_C_RX = re.compile(r"(?:^|\s)(?:-C(?:[=\s]|$)|--git-dir\b|--work-tree\b)")
GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
# `[HOST/]OWNER/REPO`, host KEPT. Dropping it was wrong in the one case the
# host exists to distinguish: with a single local checkout of
# `github.com/acme/repo`, `-R ghe.corp/acme/repo` matched it unambiguously and
# its branch and diff answered for a repository on another server. Not
# missing the repo, which is what the earlier spelling fix was about —
# confidently naming the wrong one.
SLUG = re.compile(r"^(?:[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
ORIGIN_SECTION = re.compile(r'^\[\s*remote\s+"origin"\s*\]', re.I)
CONFIG_URL = re.compile(r"^url\s*=\s*(.+)$", re.I)
SIBLINGS_MAX = 128     # CHECKOUTS examined, not entries listed: a fleet parent
                        # holds sixty-odd worktrees beside a pile of scratch
                        # directories, and capping the listing cut the tail of
                        # it alphabetically
SIBLINGS_BUDGET_S = 0.5   # measured: 5.6 ms warm over 68 worktrees, ~1 s the
                           # first time the directory is walked at all. Past
                           # this the scan gives up and answers "no checkout",
                           # which is silence — never a slow tool call.


_CD_PREFIX = re.compile(r"^\s*cd\s+" + _ARG + r"\s*(?:&&|;)")


def command_root(cwd, command):
    """The worktree the command actually runs in, when it says so itself.

    A hook payload carries the SESSION's cwd, but an agent working across
    worktrees runs `cd <other-repo> && …` in a single call — and then every
    repo fact answered from the session's cwd describes the wrong tree. Only a
    leading `cd` counts: it is the form that redirects the whole command, and
    guessing at one buried mid-pipeline would answer with a directory the
    command may never reach. Returns "" when there is no such prefix or it
    does not resolve to a worktree, and the caller keeps the session's root.
    """
    m = _CD_PREFIX.match(command or "")
    if not m:
        return ""
    path = next((g for g in m.groups() if g), "")
    if not path:
        return ""
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd or "", path)
    if not os.path.isdir(path):
        return ""
    return repo_info(path)[1]


class Probes:
    """The facts a `given` block asks about, answered lazily and at most once
    per hook call, read-only, bounded by PROBE_TIMEOUT_S, never leaving the
    machine. A failed probe answers None, which satisfies no predicate (fail
    open). `fixture` pre-answers probes by name, for the verifier and tests."""

    def __init__(self, root, branch, transcript_path=None, fixture=None, command="",
                 agent_id=None):
        self.root, self._branch, self.tp = root, branch, transcript_path
        self._fix = dict(fixture or {})
        self._memo = {}
        self._cmd = command or ""
        self._agent_id = agent_id

    def _get(self, key, compute):
        if key in self._fix:
            return self._fix[key]
        if key not in self._memo:
            try:
                self._memo[key] = compute()
            except Exception:
                self._memo[key] = None
        return self._memo[key]

    def _git(self, *args):
        # `git -C ""` would resolve against the HOOK process's cwd, which is
        # nobody's checkout in particular. A probe with no root answers None.
        if not self.root:
            return None
        import subprocess
        p = subprocess.run(["git", "-C", self.root, *args], capture_output=True,
                           text=True, timeout=PROBE_TIMEOUT_S)
        return p.stdout if p.returncode == 0 else None

    def branch(self):
        return self._get("branch", lambda: self._branch)

    def _named_base(self):
        """The base branch the in-flight command names (`--base staging`), when
        it is a branch this rule may honestly be measured against.

        The command is written by the party the rule gates, so a named base is
        CHECKED. Refused (falling through to the remote default, which
        over-measures): rev syntax rather than a plain remote branch name; a
        base with nothing to merge into it (`merge-base == HEAD`, e.g. your own
        branch, which measures zero); more than one distinct base named. This
        closes the SILENT bypass; `RULEBOOK_OVERRIDE=` is the recorded one.
        """
        named = {next((g for g in m.groups() if g), "")
                 for m in _BASE_ARG.finditer(self._cmd)}
        named.discard("")
        if len(named) != 1:
            return ""
        base = named.pop()
        if not _BRANCH_NAME.match(base) or base == "HEAD":
            return ""
        # A real remote branch, not just any rev that happens to resolve.
        if self._git("rev-parse", "--verify", "-q",
                     f"refs/remotes/origin/{base}^{{commit}}") is None:
            return ""
        mb = (self._git("merge-base", f"origin/{base}", "HEAD") or "").strip()
        head = (self._git("rev-parse", "HEAD") or "").strip()
        return "" if not mb or not head or mb == head else base

    def _remote_head(self):
        """`refs/remotes/origin/HEAD` — the remote's own default branch, set by
        clone. Absent in plenty of checkouts, hence the name list after it."""
        out = self._git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        return (out or "").strip()

    def base(self):
        """Merge-base with the branch this one will be compared against, in the
        order that gets it RIGHT rather than the order that is cheapest:
        MEMHUB_RULEBOOK_BASE_BRANCH, the base the command itself names
        (`gh pr create --base staging`), the remote's own default
        (`refs/remotes/origin/HEAD`), then the usual names as a last guess.
        None when no candidate exists (a fresh repo) — every diff probe then
        answers None too.

        With nothing explicit, the guess is the NEAREST usual candidate (fewest
        commits from its merge-base to HEAD), so a branch cut from `staging`
        is not measured against `main` (MemHub-Backend #1436). Ties keep the
        order above.

        A NON-default branch that already CONTAINS HEAD (merge-base == HEAD)
        is skipped, as in `_named_base`, since it would measure zero. The
        DEFAULT containing HEAD is kept: there the empty committed diff is the
        truth and probes measure the working tree.
        """
        def compute():
            env = os.environ.get("MEMHUB_RULEBOOK_BASE_BRANCH", "").strip()
            named = self._named_base()
            explicit = ([env] if env else [])
            # `--base staging` names a branch, not a ref: try the remote's copy
            # before the local one, which may be stale or absent.
            explicit += [f"origin/{named}", named] if named else []
            for cand in explicit:
                mb = self._merge_base(cand)
                if mb:
                    return mb
            head = (self._git("rev-parse", "HEAD") or "").strip()
            usual = [r for r in [self._remote_head()] if r]
            usual += ["origin/main", "origin/master", "origin/develop", "origin/staging",
                      "main", "master", "develop", "staging"]
            nearest, best, default = None, None, None
            for cand in usual:
                mb = self._merge_base(cand)
                if not mb:
                    continue
                if default is None:
                    default = cand  # the first usual branch that resolves
                if mb == head and cand != default:
                    continue
                out = self._git("rev-list", "--count", f"{mb}..HEAD")
                try:
                    n = int((out or "").strip())
                except ValueError:
                    continue
                if best is None or n < best:
                    nearest, best = mb, n
            return nearest
        return self._get("base", compute)

    def _merge_base(self, cand):
        """`merge-base(cand, HEAD)` when `cand` resolves to a commit, else None."""
        if self._git("rev-parse", "--verify", "-q", cand + "^{commit}") is None:
            return None
        mb = self._git("merge-base", cand, "HEAD")
        return mb.strip() if mb and mb.strip() else None

    def diff_paths(self):
        """Paths the branch has changed against its base, working tree
        included — committed, staged, unstaged, and untracked files (a new
        test file is usually untracked when the rule asks about it)."""
        def compute():
            mb = self.base()
            if mb is None:
                return None
            tracked = self._git("diff", "--name-only", "--no-renames", mb)
            untracked = self._git("ls-files", "--others", "--exclude-standard")
            if tracked is None or untracked is None:
                return None
            return sorted({l.strip() for l in (tracked + "\n" + untracked).split("\n") if l.strip()})
        return self._get("diff_paths", compute)

    def active_spec_paths(self, spec_dir="docs/specs"):
        def compute():
            from spec_owns import load_specs_from_tree
            return {s.path for s in load_specs_from_tree(self.root, spec_dir)}
        return self._get("active_spec_paths:" + spec_dir, compute)

    def untouched_specs(self, spec_dir="docs/specs"):
        """`[(spec, paths)]` for each owning spec the branch left alone, naming
        only the changed paths no UPDATED spec already answers for (a shared
        file's other owners are not flagged — ENG-1153)."""
        def compute():
            from spec_owns import load_specs_from_tree, owning_specs, spec_file_changed
            paths = self.diff_paths()
            if paths is None:
                return None
            try:
                owning = owning_specs(
                    paths, load_specs_from_tree(self.root, spec_dir), exclude_prefix=spec_dir
                )
            except (OSError, ValueError):
                return None
            updated = {s.path for s, _ in owning if spec_file_changed(s, paths)}
            answered = {p for s, touched in owning if s.path in updated for p in touched}
            out = []
            for s, touched in owning:
                left = [p for p in touched if p not in answered]
                if s.path not in updated and left:
                    out.append((s, left))
            return out
        return self._get("untouched_specs:" + spec_dir, compute)

    def diff_lines(self):
        """Added + deleted lines against the base, working tree included;
        untracked files count their line total (at most 200 files, 1 MiB each)."""
        def compute():
            mb = self.base()
            if mb is None:
                return None
            out = self._git("diff", "--numstat", mb)
            if out is None:
                return None
            n = 0
            for line in out.split("\n"):
                parts = line.split("\t")
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    n += int(parts[0]) + int(parts[1])
            untracked = self._git("ls-files", "--others", "--exclude-standard") or ""
            for p in [l.strip() for l in untracked.split("\n") if l.strip()][:200]:
                try:
                    with open(os.path.join(self.root, p), "rb") as f:
                        n += f.read(1 << 20).count(b"\n")
                except Exception:
                    pass
            return n
        return self._get("diff_lines", compute)

    def dirty(self):
        def compute():
            out = self._git("status", "--porcelain")
            return None if out is None else bool(out.strip())
        return self._get("dirty", compute)

    def user_turns(self):
        return self._get("user_turns", lambda: user_turns_of(self.tp))

    def agent_main(self):
        """True for the main agent, False inside a subagent — read off the
        transcript path, the one place the harness says which this is."""
        return self._get("agent_main", lambda: self._agent_id is None)


def given_ok(rule, probes, read=None):
    """True when every predicate in the rule's `given` holds. Pure over the
    Probes (which memoizes) and `read`, the event's own `read_facts`; a probe
    answering None — or a `file` predicate with no facts — fails."""
    g = rule.get("given")
    if not g:
        return True
    for k, v in (g.get("file") or {}).items():
        have = (read or {}).get({"lines_gt": "lines", "bytes_gt": "bytes"}.get(k, ""))
        if have is None or not have > v:
            return False
    for k, v in (g.get("agent") or {}).items():
        if k == "main":
            m = probes.agent_main()
            if m is None or m != v:
                return False
    for k, v in (g.get("repo") or {}).items():
        if k == "branch_rx":
            b = probes.branch()
            if not b or not re.search(v, b):
                return False
        elif k == "branch_not_rx":
            b = probes.branch()
            if not b or re.search(v, b):
                return False
        elif k == "diff_lines_gt":
            n = probes.diff_lines()
            if n is None or not n > v:
                return False
        elif k == "diff_files_gt":
            ps = probes.diff_paths()
            if ps is None or not len(ps) > v:
                return False
        elif k == "diff_paths_rx":
            ps = probes.diff_paths()
            if ps is None or not any(re.search(v, p) for p in ps):
                return False
        elif k == "diff_paths_none_rx":
            ps = probes.diff_paths()
            if ps is None or any(re.search(v, p) for p in ps):
                return False
        elif k == "spec_untouched":
            untouched = probes.untouched_specs((g.get("repo") or {}).get("spec_dir", "docs/specs"))
            if untouched is None or bool(untouched) != v:
                return False
        elif k == "dirty":
            d = probes.dirty()
            if d is None or d != v:
                return False
    for k, v in (g.get("user") or {}).items():
        turns = probes.user_turns()
        if turns is None:
            return False
        said = any(re.search(v, t, re.I) for t in turns)
        if (k == "said_rx" and not said) or (k == "not_said_rx" and said):
            return False
    return True


# ── plumbing ────────────────────────────────────────────────────────────────
def _redirect_base(cwd):
    """The private base a §4b forward test claimed for `cwd`, or "".

    Every failure — missing, unreadable, malformed, stale, someone else's file,
    a writable-by-others file, a cwd outside the claimed prefix — reads as "no
    redirect" and the real base is used. A broken redirect must never take a
    session's rules away from it."""
    if not cwd:
        return ""
    try:
        p = os.path.join(BASE, REDIRECT_NAME)
        st = os.stat(p)
        if st.st_uid != os.getuid() or (st.st_mode & 0o022):
            return ""                       # not ours, or writable by others
        if time.time() - st.st_mtime > REDIRECT_MAX_AGE_S:
            return ""                       # forgotten by an interrupted run
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        base, prefix = str(d.get("base") or ""), str(d.get("cwd_prefix") or "")
        if not base or not prefix or not os.path.isdir(base):
            return ""
        here = os.path.normpath(os.path.abspath(cwd))
        # Both spellings must hold, as in `_acted_on_dir`: a symlink must not
        # be able to pull a session into the test's book or out of it.
        if not (_under(here, os.path.normpath(os.path.abspath(prefix)))
                and _under(os.path.realpath(here), os.path.realpath(prefix))):
            return ""
        return base
    except Exception:
        return ""


def set_active_base(cwd):
    """Bind this process to the base its `cwd` belongs to. Called once, after
    the payload is parsed and before any book is read."""
    global _ACTIVE_BASE
    _ACTIVE_BASE = _redirect_base(cwd)
    return _base()


def book_path(repo):
    """Readable name + a hash of the RAW name, so two repos that sanitise to
    the same string ('my repo' / 'my_repo') never share a book.

    Under `_base()`, which is the real base unless a forward test has claimed
    this cwd (`set_active_base`)."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", repo)[:60] or "norepo"
    h = hashlib.sha1(repo.encode("utf-8")).hexdigest()[:8]
    return os.path.join(_base(), "book", f"{safe}-{h}.json")


def load_book(repo):
    """The cached server book {etag, fetched_at, rules} or None. Pure file read."""
    try:
        with open(book_path(repo), encoding="utf-8") as f:
            b = json.load(f)
        return b if isinstance(b, dict) and isinstance(b.get("rules"), list) else None
    except Exception:
        return None


def _atomic_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


_MATCHER_KEYS = {   # server matcher block (§3.1) → the hook's flat pilot keys
    "command_rx": "rx", "command_not_rx": "not_rx", "content_not_rx": "content_not_rx",
    "warn_once_per": "fire_scope", "result_rx": "rx",
    "prompt_rx": "rx", "prompt_not_rx": "not_rx",
}
_RESULT_KEYS = dict(_MATCHER_KEYS, command_rx="cmd_rx", command_not_rx="cmd_not_rx",
                    content_rx="rx", content_not_rx="exclude_rx")
# Every matcher key this hook has code for — the server's §3.1 allowlist as
# of 0.54, plus the legacy `result_rx` alias. `predicts_rx` is on it although
# nothing here reads it: the server defines it as inert (it never fires
# anything), so not reading it changes no outcome. A key absent from this set
# is a predicate the rule's author meant and this hook cannot evaluate; the
# rule degrades to advice rather than fire as if the condition held.
_MATCHER_KNOWN = frozenset({
    "event", "command_rx", "command_not_rx", "content_rx", "content_not_rx",
    "path_rx", "path_not_rx", "match_heredoc_body", "body_rx", "warn_once_per",
    "converted_rx", "predicts_rx", "min_chars", "result_rx",
    "prompt_rx", "prompt_not_rx",   # event "prompt" (0.99.0): the UserPromptSubmit lane
    "given",            # rides inside the matcher block; linted by `given_norm` below
})


def matcher_unsupported(m):
    """The first matcher key this hook has no code for, or "". The hook
    degrades such a rule to advice; the verifier refuses it outright, since
    to an author it is a typo. One list, asked from both places."""
    if not isinstance(m, dict):
        return ""
    return next((k for k in m if k not in _MATCHER_KNOWN), "")
_PROMPT_ONLY_KEYS = frozenset({"prompt_rx", "prompt_not_rx"})
# what a prompt rule reads besides its own patterns; every other matcher key
# is about a tool call, which a prompt is not
_PROMPT_SHARED_KEYS = frozenset({"warn_once_per", "given", "predicts_rx"})
_SCOPE_MAP = {"turn": "call", "file": "session", "session": "session"}   # warn_once_per → fire_scope
_RESERVED_RULE_KEYS = frozenset({"id", "text", "why", "status", "mode", "_version", "_label",
                                 "on", "repo_scope", "_scope_repos", "_scope_paths",
                                 "_scope_exclude_paths", "anchors", "ordering",
                                 "_rulebook_id", "_book_name", "_book_scope", "_book_members",
                                 "_book_kind", "_book_label",
                                 "min_hook_version", "_degraded"})


_RX_KEYS = ("rx", "not_rx", "body_rx", "cmd_rx", "cmd_not_rx", "path_rx", "path_not_rx",
            "content_rx", "content_not_rx", "exclude_rx", "converted_rx")
# The server's cap (validation.MAX_REGEX); it floors any pattern past 400 at
# min_hook_version 0.88.0, so every hook that receives one must load it.
_RX_MAX = 2000
# (a+)+, (\d+)+$, (a|a)+, (.*), .*.* — the classic backtracking shapes. A
# denylist, not a proof: stdlib `re` has no timeout, and a bounded matcher
# (worker + wall clock) is the Phase 2 answer named in §5.1.
_RX_NESTED = re.compile(r"\([^()]*[+*|][^()]*\)\s*[+*{]|\(\.\*\)|(\.\*){2,}")


def rx_ok(pat):
    """Load-time lint for a pattern that came off the wire (§5.1 fallback):
    must compile, stay short, and avoid the nested-quantifier shapes that
    backtrack catastrophically. A rejected pattern drops the RULE, never the
    hook — a server book can advise, it cannot stall a tool call."""
    if not isinstance(pat, str) or len(pat) > _RX_MAX or _RX_NESTED.search(pat):
        return False
    try:
        re.compile(pat)
    except re.error:
        return False
    return True


_TEXT_MAX = 400
STALL_QUARANTINE_AFTER = 3   # identical short-counted batch this many times → quarantine it


def _version_of(v):
    """A rule version is an int or a short string; anything else is unknown."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and 0 < len(v) <= 40:
        return v
    return None


def _one_line(v):
    """Server rule prose is display data, not instructions: one line, no
    control characters. A newline would let a rule forge an advisory line of
    its own, and a raw `\x1b[2J` clears the reader's terminal — this text
    reaches both the model's context and the user's `systemMessage`."""
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]+", " ", str(v or ""))).strip()


def _clean_text(v):
    """`_one_line`, length-capped for the fields that enter the context."""
    return _one_line(v)[:_TEXT_MAX]


_SPEC_LIST_MAX = 3      # specs named on a `spec_untouched` fire; the rest are counted
_SPEC_OWNED_MAX = 5     # owned paths named per spec, agent context only


def _spec_untouched_text(text, hits):
    """A `spec_untouched` fire's rule text and its agent-only detail, each ONE
    line. The spec list rides inline on the rule text — it reaches both the
    `- **[label]** …` bullet and the user's one-line `systemMessage`, so a
    newline here would break both — capped at `_SPEC_LIST_MAX` with the rest
    counted. Which owned paths each spec answers for is detail for the agent,
    not the user's terminal line, so it is returned separately."""
    shown = hits[:_SPEC_LIST_MAX]
    more = len(hits) - len(shown)
    names = ", ".join(_one_line(spec.path) for spec, _ in shown)
    if more > 0:
        names += f" (+{more} more)"
    if not names:
        return text, ""
    owned = []
    for spec, paths in shown:
        head = ", ".join(_one_line(p) for p in paths[:_SPEC_OWNED_MAX])
        if len(paths) > _SPEC_OWNED_MAX:
            head += f" (+{len(paths) - _SPEC_OWNED_MAX} more)"
        owned.append(f"{_one_line(spec.path)} owns {head}")
    detail = f"  _(changed owned paths: {'; '.join(owned)})_" if owned else ""
    return f"{text.rstrip()} Specs not updated: {names}.", detail


def _why(r):
    """The parenthetical reason — only when the rule carries one separately;
    server statements already end in 'Why: …'."""
    return f"  _(why: {r['why']})_" if r.get("why") else ""


_BOOK_SCOPES = ("all_org", "explicit")
_BOOK_KINDS = ("org", "workspace", "personal")
_BOOK_NAME_MAX = 120
_BOOK_ID_MAX = 64            # a UUID is 36; longer is rejected, never truncated
# An id is rejected, not repaired: it is a dedup key and a ledger column, so a
# cleaned one would silently be a different book.
_ID_OK = re.compile(r"[^\x00-\x1f\x7f]{1,%d}" % _BOOK_ID_MAX)
_BOOK_MEMBERS_MAX = 10 ** 9  # an org, not a number the server chose to render
# Hook-internal, and derivable ONLY from the server's `rulebook` block. A row
# is server data: left alone, a row that simply spells these keys itself would
# name its own precedence — and `_book_members: "many"` would take the whole
# lane down through book_rank. They are stripped on the way in.
_BOOK_KEYS = ("_rulebook_id", "_book_name", "_book_scope", "_book_members",
              "_book_kind", "_book_label")


def _book_facts(row):
    """The precedence facts the server puts on the wire (container spec §6.4a):
    which rulebook a rule came from, how wide that book's membership is, and
    what the book is called. The server computes NO precedence and stores no
    conflict edges — it ships `scope` and `member_count` and the hook decides
    what "wider" means (D14). A pre-container backend sends none: `book_rank`
    is then one value for all rules and book order is unchanged. `label`
    (rulebook-scopes) replaces a scope book's stored name ("personal:<id>")."""
    b = row.get("rulebook")
    b = b if isinstance(b, dict) else {}
    out = {}
    rid = row.get("rulebook_id") or b.get("rulebook_id")
    rid = rid.strip() if isinstance(rid, str) else ""
    if _ID_OK.fullmatch(rid):        # `{1,64}` rejects the empty string itself
        out["_rulebook_id"] = rid
    name = _clean_text(b.get("name"))[:_BOOK_NAME_MAX]
    if name:
        out["_book_name"] = name
    if b.get("scope") in _BOOK_SCOPES:
        out["_book_scope"] = b["scope"]
    if b.get("kind") in _BOOK_KINDS:
        out["_book_kind"] = b["kind"]
    label = _clean_text(b.get("label"))[:_BOOK_NAME_MAX]
    if label:
        out["_book_label"] = label
    mc = b.get("member_count")
    # Bounded, because it is rendered: a four-thousand-digit member_count is
    # valid JSON and would spend the session-start budget on digits alone.
    if isinstance(mc, int) and not isinstance(mc, bool) and 0 <= mc <= _BOOK_MEMBERS_MAX:
        out["_book_members"] = mc
    return out


def posture_order(rules):
    """Session rules in the order they claim the session budget: the books
    take TURNS — each book's first rule, then each book's second, and so on —
    with the wider book first in every round (`book_rank`, §11) and, inside a
    book, title then id. Deterministic, whatever order the book arrived in.

    Not "the wider book spends first": an org-wide book with more session
    rules than the budget holds then left a repo's own book nothing, in that
    repo (MemHub-Backend's 4 were cut in every session there, 2026-10-07).
    Rules with no book facts form one book and keep the old order."""
    books = {}
    for r in rules:
        books.setdefault(str(r.get("_rulebook_id") or ""), []).append(r)
    for rs in books.values():
        rs.sort(key=lambda r: (str(r.get("_label") or r.get("title") or r["id"]).casefold(),
                               str(r["id"])))
    order = sorted(books.items(), key=lambda kv: (book_rank(kv[1][0]), kv[0]))
    out = []
    for i in range(max((len(rs) for _, rs in order), default=0)):
        out += [rs[i] for _, rs in order if i < len(rs)]
    return out


def book_rank(rule):
    """Precedence between books, and nothing else: a rule from a book that
    binds the whole org outranks one from a book of three (§11 — "wider member
    scope wins" is the hook's call to make, not the server's).

    It orders; it never suppresses. Two rules that both fire both fire — the
    rank only decides which one the MAX_ADVISE cap keeps, and the cut ones are
    already logged `mode="suppressed"` to the ledger. Every sort using it is
    STABLE, so rules within one book — and every rule from a backend that
    sends no book facts — keep the order the book gave them."""
    members = rule.get("_book_members")
    return (0 if rule.get("_book_scope") == "all_org" else 1,
            -members if isinstance(members, int) and not isinstance(members, bool) else 0)


def _norm_given(r):
    """Normalise `r["given"]` in place; False when the RULE must be dropped.

    A value of the wrong kind is a malformed rule and drops it, exactly as
    `rx_ok` does — the hook's age changes nothing about it. A block or key
    this hook does not know is version skew instead: what it can check stays
    and is checked, what it cannot is removed, and `_degrade` makes the rule
    advise-only and says which key it could not read. A `given` with nothing
    left is removed entirely rather than left as an empty block that would
    read as "no condition, all good"."""
    raw = r["given"]
    if not isinstance(raw, dict):
        return False
    # A known BLOCK whose value is not a dict of predicates is malformed, and
    # `given_supported` skipped it exactly as it skips an unknown block — so
    # `{"repo": 42}` left nothing supported, no skew reported, and the rule
    # loaded with its condition silently removed. Checked before the strip,
    # because after it the two are indistinguishable.
    for block, spec in raw.items():
        if block in _GIVEN and not (isinstance(spec, dict) and spec):
            return False
    supported = given_supported(raw)
    kept = given_norm(supported) if supported else None
    # The two failures are judged separately, because a rule can carry both.
    # A KNOWN key with a value of the wrong kind is malformed and drops the
    # rule however new the hook — and an unsupported key sitting beside it
    # used to suppress that, so `{"future_key": true, "branch_rx": 42}` had
    # its whole `given` removed and fired unconditionally. Skew must not
    # launder a malformed predicate.
    if supported and kept is None:
        return False
    if kept:
        r["given"] = kept
    else:
        r.pop("given", None)
    return True


#: The id /memhub:create-rule's forward test (§4b.3) gives the candidate it arms.
#:
#: Such a row is UNFILED and UNREVIEWED, and the invariant is NOT "it never
#: gates" — a gate candidate has to gate, or the test cannot show the author
#: what their rule will do to the team. The invariant is that it never escapes
#: the base that claimed it. Inside a claim the sub-agent is the only thing it
#: can reach and the gate is the point; outside one, in the book every session
#: on this machine reads, the row has no business existing at all.
CANDIDATE_ID_PREFIX = "candidate-"


def _degrade(row, r, given=None, unknown_matcher=""):
    """Mark `r` advise-only when this hook cannot honour `row` in full."""
    if r is None:
        return None
    r.pop("min_hook_version", None)      # answered here; never a matcher field
    # A forward-test candidate is honoured ONLY under the claim that armed it,
    # where the sub-agent is the only thing it can reach — gate included, since
    # proving what a gate does to a real call is the whole point of the test.
    # Read from the shared base it is a row nobody filed, reviewed or chose,
    # reaching every session on this machine: dropped, not degraded, because
    # advising on a rule that does not exist is still serving it.
    #
    # `deny` is decided from a plain file on disk with no server round-trip, so
    # this is the only place the confinement can be enforced rather than
    # intended.
    if str(r.get("id") or "").startswith(CANDIDATE_ID_PREFIX) and not _ACTIVE_BASE:
        return None
    why = degradation(row, given, r.get("ordering"))
    if not why and unknown_matcher:
        why = f"this hook does not understand `matcher.{unknown_matcher}`"
    if not why:
        return r
    r["_degraded"] = why
    r["mode"] = "advise"        # §5.3: a gate the hook cannot fully read is not a gate
    return r


def ordering_rx_ok(o):
    """Every pattern in an `ordering` block passes the wire lint.

    `armed_by_rx` is optional but is a pattern off the same wire as the other
    two, and it runs in the PROMPT lane — synchronous, before the person's
    words reach the model, on a five-second hook timeout. An uncompilable one
    raises and a catastrophic one runs out the clock; either way the outer
    handler swallows it and NOTHING arms for that prompt, this rule and every
    valid rule after it. `rx_ok` already refuses both shapes."""
    if not all(rx_ok(o.get(k)) for k in ("required_command_rx", "gated_command_rx")):
        return False
    return rx_ok(o["armed_by_rx"]) if "armed_by_rx" in o else True


def to_hook_rule(row):
    """One `?view=hook` row → the flat shape evaluate()/OrderingEngine read.
    Rows already in the pilot shape (an `on` key) pass through. The book facts
    (`_rulebook_id`, `_book_*`) ride along on both paths; they are absent,
    harmlessly, on a pre-container backend.
    Never raises on a malformed row: returns None and the row is skipped."""
    try:
        if not isinstance(row, dict):
            return None
        if "on" in row:
            r = {k: v for k, v in row.items() if k not in _BOOK_KEYS}
            r.setdefault("id", row.get("rule_id"))
            r.setdefault("_version", _version_of(row.get("version")))
            r.update(_book_facts(row))   # the block is the only source of these
            # Prose off the wire, on either shape: no control bytes reach a
            # terminal. Length is capped too — EXCEPT `text`/`why` on a session
            # rule, the one field pair POSTURE_BUDGET_CHARS actually measures,
            # where truncating would serve an oversized rule the budget exists
            # to drop. Every other field is measured by no budget at all: an
            # advisory's text is rendered straight into the pre/post lane, a
            # gate is never cut by the advisory cap, and a label is never
            # measured — so they take the cap the server shape already gets.
            _cap = _one_line if r.get("on") == "session" else _clean_text
            for k in ("text", "why"):
                if k in r:
                    r[k] = _cap(r[k])
            for k in ("_label", "_gate_msg"):
                if k in r:
                    r[k] = _clean_text(r[k])
            if not r.get("id") or not all(rx_ok(r[k]) for k in _RX_KEYS if k in r):
                return None           # same regex lint as the server shape
            if isinstance(r.get("ordering"), dict) and not ordering_rx_ok(r["ordering"]):
                return None
            raw_given = r.get("given")
            if "given" in r and not _norm_given(r):
                return None
            return _degrade(row, r, raw_given)
        r = {"id": row.get("rule_id") or row.get("id"),
             "text": _clean_text(row.get("statement") or row.get("title")),
             "why": _clean_text(row.get("why")), "status": row.get("status", "active"),
             "_label": _clean_text(row.get("title")) or None,
             "mode": row.get("mode", "advise"), "_version": _version_of(row.get("version"))}
        r.update(_book_facts(row))   # built from scratch here, so nothing to strip
        if not r["id"]:
            return None
        scopes = [str(x) for x in (row.get("scope_repos") or []) if x]
        r["repo_scope"] = "any"
        if scopes:
            r["_scope_repos"] = scopes
        for k in ("scope_paths", "scope_exclude_paths"):   # §3.1 globs; see path_in_scope
            globs = [x for x in (row.get(k) or []) if isinstance(x, str) and x.strip()]
            if globs:
                r["_" + k] = globs[:64]
        # v2.4: anchor rules carry their own identifiers; session rules carry nothing
        if row.get("delivery") == "session_context":
            r["on"] = "session"
            return _degrade(row, r)
        if isinstance(row.get("anchors"), list) and row["anchors"]:
            anchors = [_clean_text(a) for a in row["anchors"] if isinstance(a, str) and a.strip()]
            if not anchors:
                return None
            r["on"] = "anchor"
            r["anchors"] = anchors[:64]
            r["fire_scope"] = "session"
            return _degrade(row, r)
        if isinstance(row.get("ordering"), dict):
            o = row["ordering"]
            if not ordering_rx_ok(o):
                return None
            r["on"] = "ordering"
            r["ordering"] = o
            return _degrade(row, r)
        m = row.get("matcher")
        if not isinstance(m, dict):
            return None
        # the server names the tool-result event "output" (§3.1); the hook's
        # post lane calls it "result" and reads content_* as the result pattern.
        # A server "write" rule is an edit-family rule here: the pre lane's
        # on="edit" branch already covers EDIT_TOOLS (Write included).
        ev = m.get("event") or "bash"
        r["on"] = {"output": "result", "write": "edit"}.get(ev, ev)
        keys = _RESULT_KEYS if r["on"] == "result" else _MATCHER_KEYS
        # A predicate this hook has no code for. Copying it through and
        # letting `evaluate` ignore it would run the rule as if the condition
        # held — the forward-skew failure `degradation` exists to name. Same
        # treatment as an unknown `given` key; `matcher_unsupported` is the
        # one statement of "known", and the verifier asks it too.
        unknown = matcher_unsupported(m)
        for k, v in m.items():
            if k == "event":
                continue
            if k not in _MATCHER_KNOWN:
                continue
            if k == "result_rx" and "content_rx" in m:
                continue              # content_rx is the schema key; result_rx is a legacy alias
            # `prompt_rx` and `command_rx` land on the same `rx`: each event
            # reads only its own, so a row carrying both cannot let dict order
            # decide which pattern the rule matches.
            if (k in _PROMPT_ONLY_KEYS) != (r["on"] == "prompt") and k not in _PROMPT_SHARED_KEYS:
                continue
            dest = keys.get(k, k)
            if dest in _RESERVED_RULE_KEYS:   # a matcher key can never overwrite the row's own fields
                continue
            r[dest] = v
        r["fire_scope"] = _SCOPE_MAP.get(str(r.get("fire_scope", "session")), r.get("fire_scope"))
        if not all(rx_ok(r[k]) for k in _RX_KEYS if k in r):
            return None
        if r["on"] == "prompt":
            # A prompt rule with no pattern would fire on every prompt — a
            # session note wearing the wrong delivery. Say which prompts, or
            # the rule is not one. And it only advises: the prompt has already
            # been sent, so there is no call to refuse. That is the event's
            # nature, not skew, so it is no `_degraded` ("update the plugin
            # to let this rule gate" would be a promise no version keeps).
            if not r.get("rx"):
                return None
            r["mode"] = "advise"
        raw_given = r.get("given")
        if "given" in r and not _norm_given(r):
            return None
        return _degrade(row, r, raw_given, unknown_matcher=unknown)
    except Exception:
        return None


_rc = None      # rulebook_cache, imported under __main__ only


def load_rules(repo):
    """The cached server book as hook rules. Returns (rules, "", fetched_at,
    sources) — sources maps rule id → "server" (kept for the audit file)."""
    if upgrade_status(repo):
        return _norm_rules(None)
    try:
        if _rc:
            return _rc.cached_rules(book_path(repo), _norm_rules, __file__, hook_version(), _ACTIVE_BASE)
    except Exception:
        pass
    return _norm_rules(load_book(repo))


def _norm_rules(book):
    rules, sources = [], {}
    for row in (book or {}).get("rules", []):
        r = to_hook_rule(row)
        if r and r["id"] not in sources:
            rules.append(r)
            sources[r["id"]] = "server"
    return rules, "", (book or {}).get("fetched_at"), sources


def _age_s(iso):
    """Seconds since a stamp written by `_now()`; unparseable or missing → inf."""
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(str(iso))).total_seconds()
    except Exception:
        return float("inf")


def maybe_refresh(repo, fetched_at):
    """A session outlives its SessionStart fetch — a /loop or an overnight
    babysit runs for days on the book it started with, and a gate flipped
    back to advise on the server would keep blocking it until restart. Once
    the cache is a minute old, refresh it in the background: the child is
    detached, so the lane never waits, and the stamp file keeps a dead server
    from being probed more than once a minute. No cache at all
    counts as infinitely old, so a session whose start-up fetch failed gets
    retried here too."""
    if os.environ.get("MEMHUB_RULEBOOK_FETCH", "1") == "0":
        return
    if _age_s(fetched_at) < REFRESH_AFTER_S:
        return
    stamp = book_path(repo) + ".refresh"
    try:
        with open(stamp, encoding="utf-8") as f:
            if _age_s(json.load(f).get("at")) < REFRESH_RETRY_S:
                return
    except Exception:
        pass
    try:
        # The stamp records the ATTEMPT, so it goes first: a fork that fails
        # under resource pressure must not be retried on every tool call, and
        # a minute before the next try costs at most one override on a rule
        # the server has since retired. The other order was tried and reverted.
        _atomic_json(stamp, {"at": _now()})
        spawn_fetch(repo)
    except Exception:
        pass


def path_in_scope(rule, path, root=""):
    """The server's §3.1 path scope, mirrored (crud.path_in_scope): in-scope AND
    NOT excluded, fnmatch against the path relative to the worktree root and,
    as the server does, against `*/<glob>`. A path-scoped rule needs a path
    to match at all — a Bash call carries none, so an include-scoped rule
    never fires there and an exclude-only one always may."""
    inc = rule.get("_scope_paths") or []
    exc = rule.get("_scope_exclude_paths") or []
    if not inc and not exc:
        return True
    if not path:
        return not inc
    cands = {path}
    if root and path.startswith(root.rstrip("/") + "/"):
        cands.add(os.path.relpath(path, root))

    def hit(g):
        return any(fnmatch.fnmatch(c, g) or fnmatch.fnmatch(c, f"*/{g}") for c in cands)
    return ((not inc) or any(hit(g) for g in inc)) and not any(hit(g) for g in exc)


def scope_ok(rule, repo, gitdir):
    scope = rule.get("repo_scope", "any")
    if rule.get("_scope_repos"):        # server list: this checkout's name or its main
        parts = gitdir.split("/") if gitdir else []   # checkout's (…/<main>/.git/worktrees/x)
        main = parts[parts.index(".git") - 1] if ".git" in parts and parts.index(".git") > 0 else ""
        # Folded, and folded on the SERVER too (crud._rule_in_repo): the name
        # in the rule was typed by a person, the name here was resolved from a
        # remote URL on someone's machine. Matching them exactly makes
        # "memhub-backend" and "MemHub-Backend" different repositories, and the
        # rule then just never fires, with nothing anywhere saying why. Folding
        # on one side only would be worse than neither: the server would ship a
        # rule this would then discard.
        here = {repo.casefold(), main.casefold()} - {""}
        return any(s.casefold() in here for s in rule["_scope_repos"])
    if scope == "any":
        return True
    return scope in repo or (gitdir and f"/{scope}/" in gitdir)


# ── server: fetch + flush (lazy imports — the pre/post lanes pay for them only
#    when an upgrade notice is on disk; see upgrade_status) ──

# plugin_compatibility.STATE_DIR, named here because importing that module
# pulls in _memhub_auth (~35 ms); tests hold the two equal. When the module is
# already loaded (a caller in this process, a test that repoints it), its own
# value wins.
_COMPAT_DIR = os.path.normpath(os.path.join(os.path.expanduser("~"), ".config", "memhub-plugin", "compatibility"))


def _compat_dir():
    mod = sys.modules.get("plugin_compatibility")
    return str(getattr(mod, "STATE_DIR", _COMPAT_DIR)) if mod else _COMPAT_DIR


def _api():
    """(rest_base, bearer, mcp_http) or None. Non-interactive: a hook can only
    spend a credential /memhub:login already minted."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import mcp_http
    import pak
    from _memhub_auth import resolve_bearer
    url, bearer = resolve_bearer(refresh=False)
    if not bearer:
        return None
    return pak.api_base(url), bearer, mcp_http


def _upgrade_scope(api):
    base, bearer, _ = api
    return hashlib.sha256((base + "\0" + bearer).encode()).hexdigest()


def upgrade_status(repo):
    # A rejection from capture/search also suspends cached rules, even if a
    # fresh Rulebook cache would otherwise avoid a network fetch this session.
    # Both notices are local files, and the credential only says WHICH file
    # is ours; with none on disk the answer is None without resolving it.
    try:
        has_compat = any(n.endswith(".json") for n in os.listdir(_compat_dir()))
    except OSError:
        has_compat = False
    if not has_compat and not os.path.exists(book_path(repo) + ".upgrade"):
        return None
    try:
        api = _api()
        if api:
            from plugin_compatibility import status
            notice = status(api[0], api[1])
            if notice:
                return {**notice, "scope": _upgrade_scope(api)}
    except Exception:
        pass
    try:
        with open(book_path(repo) + ".upgrade", encoding="utf-8") as f:
            notice = json.load(f)
        api = _api()
        if (api and notice.get("scope") == _upgrade_scope(api)
                and re.fullmatch(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}",
                                 notice.get("minimum_version", ""))):
            return notice
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _clear_upgrade(repo):
    try:
        os.unlink(book_path(repo) + ".upgrade")
    except FileNotFoundError:
        pass


def show_upgrade(repo, session, event):
    notice = upgrade_status(repo)
    if not notice:
        return False
    # A known rejection suspends cached rules; it never denies unrelated tools.
    # Bound repeated context to once per session/policy/installed version.
    version = hook_version()
    key = [notice["scope"], notice["minimum_version"], list(version or ())]
    seen_path = book_path(repo) + ".notice-" + hashlib.sha256(str(session).encode()).hexdigest()[:16]
    seen = None
    try:
        with open(seen_path, encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        pass
    if event == "SessionStart" or seen != key:
        current = ".".join(map(str, version)) if version else "unknown"
        minimum = notice["minimum_version"]
        # Two audiences, two texts. The USER's line (`systemMessage`, shown by
        # the terminal with no model in between) says what to do. The AGENT's
        # copy is a status record: facts, effect, remedy — and no instruction
        # about what the agent should say. The one text that used to serve
        # both ended "Tell the user this upgrade is required; do not report
        # the cached rules as current", and Sonnet 4.6 read exactly that
        # sentence as the mark of an injected message ("legitimate hook
        # telemetry reports facts; it does not instruct the agent on what to
        # say") and refused to relay the notice at all. A model that is
        # handed the facts relays them on its own; a model that is ordered to
        # may not. Both texts keep the three tokens the release checks look
        # for: the error code, the version, "restart this agent session".
        context = (f"MemHub plugin status: PLUGIN_UPGRADE_REQUIRED\n"
                   f"installed: {current} · minimum supported by the server: {minimum}\n"
                   "effect: rulebook synchronization is unavailable; the cached team rules are "
                   "suspended and not in effect for this session\n"
                   "remedy: update the MemHub plugin with the host's plugin manager, then "
                   "restart this agent session")
        user_line = (f"⚠️ MemHub plugin {current} is unsupported (PLUGIN_UPGRADE_REQUIRED): update to "
                     f"{minimum} or newer with your host's plugin manager, then restart this agent "
                     "session. Team rules are suspended until then.")
        emit(event, context, user_line=user_line)
        _atomic_json(seen_path, key)
    return True


def fetch_book(repo, timeout=None):
    """GET /rules?repo=<repo>&view=hook with If-None-Match.

    No `status=` param: `view=hook` serves ACTIVE rules on its own, and a
    server filter-grammar change once turned it into a silent 400.
    200 → rewrite the cache; 304 → touch fetched_at (confirmed current, what
    §5.3 gate freshness measures); anything else → the cache is untouched.

    `hook_version` rides along so the SERVER can enforce `min_hook_version`
    for hooks too old to check it themselves (see `degradation`); an older
    hook sends none. The shared backend contract verifies this query field.
    A structured 426 suspends cached rules and records an agent-visible upgrade
    notice. Only a subsequent valid 200/304 response clears that notice."""
    api = _api()
    if not api:
        return
    base, bearer, http = api
    old = load_book(repo) or {}
    hdrs = {"If-None-Match": old["etag"]} if old.get("etag") else {}
    q = "view=hook&repo=" + urllib.parse.quote(repo, safe="")
    have = hook_version()
    if have:
        q += "&hook_version=" + ".".join(str(n) for n in have)
    try:
        reply = http.rest(f"{base}{API_PATH}/rules?{q}", bearer, "GET", headers=hdrs,
                          timeout=timeout or FETCH_TIMEOUT_S)
    except Exception as exc:          # keep cache bytes, but never hide a policy rejection
        if isinstance(exc, getattr(http, "PluginUpgradeRequired", ())):
            _atomic_json(book_path(repo) + ".upgrade", {
                "scope": _upgrade_scope(api), "minimum_version": exc.minimum_version,
            })
        _breadcrumb("fetch", exc)
        return
    if reply.status == 304 and old:
        _clear_upgrade(repo)
        _atomic_json(book_path(repo), dict(old, fetched_at=_now()))
    elif reply.status == 200 and isinstance(reply.data, dict) \
            and isinstance(reply.data.get("rules"), list):
        _atomic_json(book_path(repo), {"etag": reply.etag, "fetched_at": _now(),
                                       "rules": reply.data["rules"]})
        _clear_upgrade(repo)
    else:                             # a 2xx with the wrong shape is a failure too — say so
        _breadcrumb("fetch", f"HTTP {reply.status}: unexpected reply shape")


# ── credentials out of command text ────────────────────────────────────────
#
# A command line is where credentials live — `curl -H "Authorization: Bearer
# …"`, `psql postgres://user:pw@host`, `--token=…`. Wherever command text is
# about to leave the machine (the judge's call excerpt, an override reason,
# the harness window) those values are replaced first.
#
# This is a denylist and cannot be complete — the docstring and the README say
# so. It is a floor, not a guarantee.
_REDACTIONS = (
    # `--token=x`, `--password x`, `API_KEY=x` — the value, not the flag, so the
    # judge still sees that a credential was passed. A quoted value
    # (`--token='x'`, `PGPASSWORD="two words"`) goes whole, quotes and all.
    #
    # `auth` is deliberately NOT in this list even though it names plenty of
    # real secrets: it also names `gh auth login`, `--auth-mode`, `auth0_sub`,
    # and eating the word after those costs the judge the verb of the command
    # for nothing. The `Authorization:` header has its own rule below, which is
    # where `auth` actually carries a credential.
    # The key must END with the credential word. Allowing a trailing suffix
    # matched `--token-budget 500` and ate the number, which is not a secret and
    # is exactly the kind of over-redaction that degrades the judge on ordinary
    # commands. `aws_secret_access_key`, `--with-token` and `API_KEY` all still
    # match, because each ends with one.
    (re.compile(r"(?i)\b([a-z0-9_-]*(?:secret|passwd|password|token|api[_-]?key|"
                r"access[_-]?key|credential))(\s*[=:]\s*|\s+)('[^']*'|\"[^\"]*\"|[^\s\"']+)"),
     r"\1\2<redacted>"),
    # `curl -u user:password`, `-U user:password`, a quoted password
    # (`-u user:'pass word'`), and a quoted pair (`-u 'user:pass word'`).
    (re.compile(r"(?i)(\s-{1,2}(?:u|user)[=\s]+)([^\s:\"']+):('[^']*'|\"[^\"]*\"|[^\s\"']+)"),
     r"\1\2:<redacted>"),
    (re.compile(r"(?i)(\s-{1,2}(?:u|user)[=\s]+)(['\"])([^:'\"]+):([^'\"]*)\2"),
     r"\1\2\3:<redacted>\2"),
    # Authorization / Proxy-Authorization headers, with or without a scheme.
    (re.compile(r"(?i)(authorization\s*:\s*)(?:bearer|basic|token)?\s*[^\s\"']+"),
     r"\1<redacted>"),
    # Credentials inline in a URL: scheme://user:pw@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^\s:/@]+):([^\s@]+)@"), r"\1\2:<redacted>@"),
    # Vendor-shaped keys, which are recognisable on their own.
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"), "<redacted>"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}"), "<redacted>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted>"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "<redacted>"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+"), "<redacted>"),
    (re.compile(r"\bmhk_[A-Za-z0-9_-]{8,}"), "<redacted>"),
)


def redact_secrets(text):
    """Strip credential-shaped values from a command before it is sent.

    Order matters: the URL rule must run before the vendor-key rules, or a
    password that happens to look like a key is rewritten first and the
    surrounding `user:…@host` shape no longer matches.
    """
    if not text:
        return text
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


# Anchor rules (§4.7), matched here against the cached book. An anchor fires
# when it appears in the call's command or path as a WHOLE identifier: not
# preceded by an identifier character other than `/` (so `SKILL.md` matches
# `skills/x/SKILL.md` and a relative path matches the tail of an absolute one),
# and not followed by any (so `agent-plugins` does not match
# `agent-plugins-internal`). An anchor ending in `/` names a directory and
# also matches any path under it. Case-sensitive, literal. Whether the rule fits the
# call is the judge's question (`judge_fires`), not this one's.
_ANCHOR_RX = {}


def anchor_rx(anchor):
    """The compiled whole-identifier pattern for one anchor, once per run;
    None for an anchor with nothing left to match (a bare `/`)."""
    if anchor not in _ANCHOR_RX:
        core = anchor.rstrip("/")
        tail = r"(?:/|(?![A-Za-z0-9_./-]))" if anchor.endswith("/") else r"(?![A-Za-z0-9_./-])"
        _ANCHOR_RX[anchor] = re.compile(
            r"(?<![A-Za-z0-9_.-])" + re.escape(core) + tail) if core else None
    return _ANCHOR_RX[anchor]


def anchor_hits(rule, text):
    """The anchors of `rule` that appear in `text` as whole identifiers."""
    if not text:
        return []
    out = []
    for a in rule.get("anchors") or []:
        rx = anchor_rx(a) if isinstance(a, str) else None
        if rx is not None and rx.search(text):
            out.append(a)
    return out


# ── the rule judge (rule-judge-spec §3, §5) ────────────────────────────────
#
# A pattern says a rule MIGHT apply; it matches words, not situations. After
# the fire pass the hook asks the server whether each matched rule fits the
# turn in hand, and the answer is recorded on the rule's fire row. Whether the
# answer also changes what is shown is the server's call, per reply: `enforce`
# false (shadow) changes nothing here.
#
# This is the lane that sends content: the call's command or path, the
# person's message and the stripped turn. Everything goes through
# `judge_redact` first, and `MEMHUB_RULEBOOK_JUDGE=0` turns the lane off.
JUDGE_TIMEOUT_S = _timeout(1.5)    # inside the hook budget; past it nothing is judged
JUDGE_MAX_RULES = 10               # the server refuses a longer `rules`
JUDGE_BACKOFF_S = 600              # an org with the judge off is asked this often, not per call
JUDGE_CALL_CHARS = 600
JUDGE_MATCHED_CHARS = 200
JUDGE_RESULT_CHARS = 3000          # the output an output rule matched; the server takes 4000
JUDGE_ID_CHARS = 200               # repo / session_id / turn_id, the server's bound
JUDGE_VERDICTS = frozenset({"fit", "no_fit", "timeout", "failed", "no_rule", "disabled"})
# Matcher rules and anchor rules. Not `ordering` (an obligation the state
# machine armed is not a pattern's guess), and the session and prompt lanes
# never reach the fire pass that calls this.
_JUDGED_ON = frozenset({"bash", "edit", "read", "result", "anchor"})


def judge_redact(text):
    """Every text the judge lane sends goes through this: `redact.py`'s MemHub
    keys and its identity pass (home directories, e-mail addresses), then the
    command-line credential shapes of `redact_secrets` — the three denylists
    the harness window already uses, in the same order. Raises if `redact.py`
    cannot be imported; the lane then sends nothing."""
    if not text:
        return ""
    import redact  # noqa: PLC0415 — beside this file
    return redact_echoes(text, redact_secrets(redact.redact_identities(redact.redact_text(text))))


def judge_result_excerpt(rule, rtext):
    """The part of a tool's output an OUTPUT rule fired on, for the judge.

    The judge reads the turn from the transcript, and the call's result is
    written there after this hook returns — so without this the judge saw the
    command and "output pattern" and nothing else, and held back nearly every
    output fire (24 of 25 locally, real `timeout: command not found` among
    them). Centred on the rule's own match, searched the way `evaluate` does
    (`re.M`, over the same head and tail spans); the tail when nothing matches.
    Not redacted here: the caller redacts every field it sends."""
    if not rtext:
        return ""
    half = JUDGE_RESULT_CHARS // 2
    if len(rtext) <= 2 * RESULT_WINDOW_CHARS:
        spans = ((0, rtext),)
    else:
        spans = ((0, rtext[:RESULT_WINDOW_CHARS]),
                 (len(rtext) - RESULT_WINDOW_CHARS, rtext[-RESULT_WINDOW_CHARS:]))
    for offset, span in spans:
        try:
            m = re.search(rule["rx"], span, re.M) if rule.get("rx") else None
        except (re.error, TypeError):
            m = None
        if m:
            at = offset + m.start()
            lo = max(0, at - half)
            return rtext[lo:lo + JUDGE_RESULT_CHARS]
    return rtext[-JUDGE_RESULT_CHARS:]


_ECHO_TOKEN = re.compile(r"[A-Za-z0-9_\-./+]{8,}")


def redact_echoes(original, redacted):
    """Redact every further copy of a value the denylists redacted once.

    The denylists find a secret by its position (`Bearer X`, `--api-key=X`),
    so the same X written bare elsewhere in the text, say in the person's own
    message ("X is the staging key"), was sent verbatim. A token that occurs
    fewer times after redaction than before was a secret somewhere, so it goes
    everywhere. Tokens under 8 characters are left alone: too common to be
    worth the false positives."""
    if not original or not redacted:
        return redacted
    return scrub_tokens(redacted, redacted_tokens(original, redacted))


def redacted_tokens(original, redacted):
    """The tokens of `original` that redaction removed at least one copy of."""
    if not original or not redacted:
        return set()
    return {tok for tok in set(_ECHO_TOKEN.findall(original))
            if redacted.count(tok) < original.count(tok)}


def scrub_tokens(text, tokens):
    for tok in tokens:
        if text and tok in text:
            text = text.replace(tok, "<redacted>")
    return text


def judgeable(rule):
    """Is this fire one the judge is asked about? A matcher or anchor rule the
    server can load — its id is a UUID, which a local or test rule's is not."""
    if rule.get("on") not in _JUDGED_ON:
        return False
    try:
        uuid.UUID(str(rule.get("id")))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def judge_matched_on(rule, ev, handle, root):
    """What matched, in words, for one fired rule. An anchor rule: the anchors
    of the rule that appear in `handle` (the command or path of the call). A
    matcher rule: its event, as the server names it, and the file when the
    event that fired it was an edit or a read of one. Not yet redacted."""
    if rule.get("on") == "anchor":
        hit = anchor_hits(rule, handle)
        return "anchor '%s'" % ", ".join(hit) if hit else "anchor"
    out = "%s pattern" % ("output" if rule.get("on") == "result" else rule.get("on"))
    path = (ev or {}).get("fp") or ""
    if path and (ev or {}).get("tool") in EDIT_TOOLS + READ_TOOLS:
        if root and path.startswith(root.rstrip("/") + "/"):
            path = os.path.relpath(path, root)
        out += f" in {path}"
    return out


def _judge_backoff_path():
    return os.path.join(_base(), "judge.json")


def judge_backed_off():
    """True while a recent reply said the judge is off for this org (or the
    server has no `/judge` at all). Shared by every session on the machine."""
    try:
        with open(_judge_backoff_path(), encoding="utf-8") as f:
            until = json.load(f).get("judge_disabled_until")
        return isinstance(until, (int, float)) and time.time() < until
    except Exception:
        return False


def _judge_back_off():
    try:
        _atomic_json(_judge_backoff_path(),
                     {"judge_disabled_until": time.time() + JUDGE_BACKOFF_S})
    except Exception:
        pass


def ask_judge(body):
    """POST /judge. Returns `({rule_id: {"verdict", "show", "p_fit"}}, enforce)`
    for the requested rules the reply answered, or None on ANY client failure —
    no credential, timeout, a non-200, a reply off the contract — in which case
    nothing was judged and the caller shows and blocks exactly as it would
    have. A reply whose verdicts are all `disabled`, and a 404 from a server
    that predates the route, start the `JUDGE_BACKOFF_S` backoff; the 404
    returns None without a breadcrumb, since nothing is broken. Never raises."""
    try:
        api = _api()
        if not api:
            return None
        base, bearer, http = api
        try:
            reply = http.rest(f"{base}{API_PATH}/judge", bearer, "POST", body=body,
                              timeout=JUDGE_TIMEOUT_S)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                _judge_back_off()
                return None
            raise
        data = reply.data
        if reply.status != 200 or not isinstance(data, dict) \
                or not isinstance(data.get("enforce"), bool) \
                or not isinstance(data.get("verdicts"), list):
            raise ValueError(f"HTTP {reply.status}: unexpected reply shape")
        asked = {r["rule_id"] for r in body["rules"]}
        out = {}
        for v in data["verdicts"]:
            # One malformed verdict makes the reply untrustworthy as a whole. A
            # score outside [0, 1] matters twice: `POST /fires` rejects the row.
            p = v.get("p_fit") if isinstance(v, dict) else None
            if not isinstance(v, dict) or v.get("verdict") not in JUDGE_VERDICTS \
                    or not isinstance(v.get("show"), bool) \
                    or not (p is None or (isinstance(p, (int, float)) and not isinstance(p, bool)
                                          and 0 <= p <= 1)):
                raise ValueError("unexpected verdict shape")
            rid = str(v.get("rule_id"))
            if rid in asked:
                out[rid] = {"verdict": v["verdict"], "show": v["show"], "p_fit": p}
        _breadcrumb_clear("judge")
        if out and all(v["verdict"] == "disabled" for v in out.values()):
            _judge_back_off()
        return out, data["enforce"]
    except Exception as exc:
        _breadcrumb("judge", exc)
        return None


def judge_fires(st, data, *, repo, session, tool, cmd, fp, root, fired_now, fired_on):
    """Ask whether each judgeable rule in `fired_now` fits the current turn.

    Returns `(verdicts, held, fresh)`: `verdicts` is {rule_id: {"verdict",
    "p_fit"}} for every fire that has a judgement (to record on its row);
    `held` is the set of rule ids to hold back — `show` false under a reply
    with `enforce` true, never otherwise; `fresh` is the subset of `held`
    whose verdict arrived on THIS call rather than from the turn's cache.

    One verdict per rule per turn: answers are kept in `st["judge"]` as
    {turn_id: {rule_id: {"verdict", "show", "p_fit"}}}, only for the current
    turn, and a rule found there is not sent again. `show` is stored as
    acted on (true for every rule of a shadow reply). `disabled` is not
    cached — the backoff file covers it — and nothing is cached when the turn
    has no id. At most `JUDGE_MAX_RULES` uncached rules are sent; the rest,
    and every rule of a failed call, are simply absent from `verdicts`.
    Never raises: any failure is "nothing judged"."""
    try:
        if os.environ.get("MEMHUB_RULEBOOK_JUDGE", "1") == "0":
            return {}, set(), set()
        todo = [r for r in fired_now if judgeable(r)]
        if not todo:
            return {}, set(), set()
        import rule_judge_turn  # noqa: PLC0415 — beside this file
        turn = rule_judge_turn.read_turn(data.get("transcript_path"), redact=judge_redact)
        turn_id = str(turn.get("turn_id") or "")[:JUDGE_ID_CHARS]
        cache = st.get("judge") if isinstance(st.get("judge"), dict) else {}
        known = cache.get(turn_id) if turn_id else None
        known = dict(known) if isinstance(known, dict) else {}
        ask = [r for r in todo if str(r["id"]) not in known][:JUDGE_MAX_RULES]
        answered = {}
        if ask and not judge_backed_off():
            handle = shell_only(cmd) if tool == "Bash" and cmd else fp
            # An output rule fired on this call's result, which the transcript
            # does not hold yet: hand the judge the part it fired on.
            results = [{"kind": "result", "text": judge_redact(judge_result_excerpt(
                r, (fired_on.get(r["id"]) or {}).get("rtext") or ""))}
                for r in ask if r.get("on") == "result"]
            results = [e for e in results if e["text"]][:1]
            # The server refuses more than MAX_ENTRIES; the newest are kept.
            prior = (turn.get("turn") or [])[len(results) - rule_judge_turn.MAX_ENTRIES:] \
                if results else (turn.get("turn") or [])
            body = {
                "repo": str(repo or "")[:JUDGE_ID_CHARS],
                "session_id": str(session or "")[:JUDGE_ID_CHARS],
                "turn_id": turn_id,
                "person_request": turn.get("person_request") or "",
                "turn": prior + results,
                "call": {"tool": str(tool or "")[:200],
                         "text": judge_redact(handle)[:JUDGE_CALL_CHARS]},
                "rules": [{"rule_id": str(r["id"]),
                           "matched_on": judge_redact(judge_matched_on(
                               r, fired_on.get(r["id"]), handle, root))[:JUDGE_MATCHED_CHARS]}
                          for r in ask],
            }
            # Each field was redacted on its own, so a secret the call shows in a
            # credential position (`--api-key=X`) and the person's message shows
            # bare ("X is the key") would survive in the message. Carry the
            # call's redacted values across the whole body.
            secrets = redacted_tokens(handle, judge_redact(handle))
            if secrets:
                body["person_request"] = scrub_tokens(body["person_request"], secrets)
                body["turn"] = [dict(e, text=scrub_tokens(e.get("text") or "", secrets))
                                if isinstance(e, dict) else e for e in body["turn"]]
            reply = ask_judge(body)
            if reply is not None:
                got, enforce = reply
                # Shadow: the answer is recorded and acted on as "show".
                answered = {rid: dict(v, show=v["show"] or not enforce) for rid, v in got.items()}
        verdicts, held, fresh = {}, set(), set()
        for r in todo:
            rid = str(r["id"])
            v = answered.get(rid) or known.get(rid)
            if not isinstance(v, dict) or v.get("verdict") not in JUDGE_VERDICTS:
                continue
            verdicts[r["id"]] = {"verdict": v["verdict"], "p_fit": v.get("p_fit")}
            if v.get("show") is False:
                held.add(r["id"])
                if rid in answered:
                    fresh.add(r["id"])
        if turn_id:
            known.update({rid: v for rid, v in answered.items() if v["verdict"] != "disabled"})
            st["judge"] = {turn_id: known}
        return verdicts, held, fresh
    except Exception as exc:
        _breadcrumb("judge", exc)
        return {}, set(), set()


def unfire(st, rule, key, before, *, reset_raw):
    """Undo the per-session marks the fire pass wrote for `rule` on this call,
    so a rule the judge held back can fire again in a later turn. `before` is
    the state as it was when the fire pass started; `key` is the rule's
    `dedup_keys` entry.

      st["fired"]         the one dedup mark this call appended (the anchor
                          lane's rule id, or the matcher lane's key) — without
                          this the rule is spent for the session by a fire
                          nobody was shown.
      st["counts"]        a `counter:N` rule's hit, put back, so the next
                          match reaches N again rather than passing it.
      st["spec_pending"]  the entry a `spec_untouched` fire filed, put back
                          as it was: nobody was asked to touch those specs.
      st["raw"]           with `reset_raw`, zeroed — the suppressed row this
                          call writes carries the count, exactly as a shown
                          fire's row does. Without it (a repeat the turn's
                          cache answered, which writes no row) the match stays
                          counted for the rule's next row.
    """
    rid = rule["id"]
    if key is not None and st["fired"].count(key) > before["fired"].count(key):
        for i in range(len(st["fired"]) - 1, -1, -1):
            if st["fired"][i] == key:
                del st["fired"][i]
                break
    if rid in before["counts"]:
        st["counts"][rid] = before["counts"][rid]
    else:
        st["counts"].pop(rid, None)
    for k in [k for k in st["spec_pending"] if k not in before["spec_pending"]
              or st["spec_pending"][k] != before["spec_pending"][k]]:
        try:
            mine = json.loads(k)[0] == rid
        except Exception:
            mine = False
        if not mine:
            continue
        if k in before["spec_pending"]:
            st["spec_pending"][k] = before["spec_pending"][k]
        else:
            del st["spec_pending"][k]
    if reset_raw:
        st["raw"][rid] = 0


def spawn_fetch(repo):
    """Refresh the book in a DETACHED child so SessionStart returns at once."""
    import subprocess
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "fetch", repo],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


# The fire row on the wire (rulebook-fire-ledger-spec §3, contract C3, plus
# the additive `worktree` of rule-fire-events-spec §2). No `converted` /
# `converted_at`: an advise fire's outcome is the SERVER's to compute from the
# events this hook posts, and a hook that sent one would only be honoured at
# the lowest precedence as a legacy verdict. `override_reason` stays — on a
# GATE row it is the excuse given on the blocked call, the row's own record.
# `judge_score` / `judge_verdict` (rule-judge-spec §4) are on a row only when
# the judge answered for that fire; see `wire_row`.
WIRE_KEYS = ("fire_id", "rule_id", "rule_version", "session_id", "agent_id", "worktree",
             "repo", "branch", "tool", "hook_phase", "mode", "dedup_key",
             "raw_matches_before_fire", "fired_at", "source_message_id", "override_reason",
             "judge_score", "judge_verdict")
_JUDGE_WIRE_KEYS = ("judge_score", "judge_verdict")

# What the hook observed AFTER a fire (rule-fire-events-spec §2). Facts, never
# verdicts: `receipt` (an ordering rule's green run in this checkout),
# `converted` (a rule's converted_rx matched a command in this session),
# `dismissed` (the person set a rule aside, with the reason), `turn_end` (the
# session finished a turn), `session_end`. The server folds these into each
# fire's outcome; nothing here decides one.
EVENT_WIRE_KEYS = ("event_id", "kind", "rule_id", "session_id", "agent_id", "worktree",
                   "repo", "branch", "reason", "at")


# Capture namespaces a conversation id per host: Claude sends the session uuid
# bare (flush_turn.py), Codex sends `codex-<uuid>` (codex_flush.py) and Cursor
# `cursor-<uuid>` (cursor_flush.py). A fire carries the raw hook-payload
# session id, so on Claude it matches the captured conversation by accident and
# on the others it cannot match at all — every Codex fire shows "Not linked
# yet" (ENG-1075). Namespace it HERE, at the wire, and nowhere else: ordering
# state, obligations, dedup keys and state_path all key off the raw id, and
# renaming that mid-session would strand an in-flight session's own state.
def wire_session_id(session_id, host):
    """The session id as CAPTURE wrote it, so the server can join the two.

    Delegates to `pr_link.conversation_id_for`, which already owns this exact
    projection for the PR-link lane — a second copy is how the two lanes come
    to disagree about what a Codex session is called, and it already handles
    the cases a fresh one forgets (surrounding whitespace, a host spelled
    `Codex`, an id that already carries its prefix).

    Imported lazily and on a fail-open path: only the flush lane projects rows,
    so the per-call pre/post lanes never pay for the import, and a fire that
    cannot be namespaced still ships (unlinked) rather than taking the hook
    down.
    """
    try:
        import pr_link
        return pr_link.conversation_id_for(host, session_id) or session_id
    except Exception:
        return session_id


def wire_row(row):
    """The v2 ledger row minus `excerpt` (Phase 1: always stripped — the org
    opt-in for excerpts is a server setting the hook does not consult).

    `host` is local-only, like `rulebook_id`: it is not a wire field, it is
    how this projection knows which namespace the session id belongs in.

    The two judge keys are sent only on a row whose fire was judged (it has a
    `judge_verdict`). A fire nobody judged posts the row shape it always did,
    which a server older than the judge columns also accepts.
    """
    out = {k: row.get(k) for k in WIRE_KEYS}
    if row.get("judge_verdict") is None:
        for k in _JUDGE_WIRE_KEYS:
            out.pop(k, None)
    out["session_id"] = wire_session_id(out.get("session_id"), row.get("host"))
    return out


def event_wire_row(row):
    """Same projection as `wire_row`, and the same namespacing.

    The server folds these into each fire's outcome BY SESSION, so an event
    that names the session differently from the fire it answers folds into
    nothing. `host` is local-only here too.
    """
    out = {k: row.get(k) for k in EVENT_WIRE_KEYS}
    out["session_id"] = wire_session_id(out.get("session_id"), row.get("host"))
    return out


def worktree_key(root):
    """The 16-hex checkout id every fire and receipt carries — the same key
    the ordering engine files its state under, so a receipt run by another
    session of this checkout can answer this session's fire on the server."""
    return hashlib.sha1(root.encode("utf-8")).hexdigest()[:16] if root else None


def _read_rows(path, start=0, offsets=None):
    """Complete JSON lines from byte `start`; returns (rows, end_offset) where
    end_offset stops before any partial trailing line. `offsets`, if given,
    receives each row's end offset so a caller can watermark per row."""
    rows, end = [], start
    try:
        if start > os.path.getsize(path):    # ledger rewritten/rotated: restart, never strand
            rows, end = [], 0
        with open(path, "rb") as f:
            f.seek(end)
            for line in f:
                if not line.endswith(b"\n"):
                    break
                end += len(line)
                try:
                    rows.append(json.loads(line.decode("utf-8")))
                except Exception:
                    continue
                if offsets is not None:
                    offsets.append(end)
    except FileNotFoundError:
        pass
    return rows, end


def _breadcrumb(what, exc):
    """ledger/.last_error — the one place a silent backstop failure is visible."""
    try:
        _atomic_json(os.path.join(_ledger_dir(), ".last_error"),
                     {"at": _now(), "what": what, "error": str(exc)[:300]})
    except Exception:
        pass


def _breadcrumb_clear(what):
    """Retract the breadcrumb once ``what``'s own lane has worked again.

    Without this a lane that records no success of its own — the judge — leaves
    a single blip standing until some OTHER lane happens to succeed. The judge
    runs on PreToolUse and the book is only refetched at SessionStart, so one
    1.5 s timeout mid-session would produce a health banner at the next session
    start, long after the lane had recovered. A warning that outlives its cause
    is the failure mode this file exists to avoid.

    Only clears a crumb this lane wrote: another lane's failure is still real.
    """
    path = os.path.join(_ledger_dir(), ".last_error")
    try:
        with open(path, encoding="utf-8") as f:
            crumb = json.load(f)
        if not isinstance(crumb, dict) or crumb.get("what") != what:
            return
        os.unlink(path)
    except Exception:      # no crumb, unreadable, or already gone — all fine
        pass


def _sent_path():
    return os.path.join(_ledger_dir(), ".sent")


def load_sent():
    try:
        with open(_sent_path(), encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("not a dict")
        return d
    except Exception:
        return {"fires_offset": 0, "events_offset": 0, "last_flush_at": None}


# The two ledgers the flush lane ships, each behind its own byte watermark in
# `.sent`: (file, offset key, endpoint, body key, id key, wire shape). Fires
# go first so the events that answer them usually find them landed; the
# server serialises the two ingests either way (rule-fire-events-spec §6).
LEDGERS = (
    ("fires.jsonl", "fires_offset", "/fires", "fires", "fire_id", wire_row),
    ("events.jsonl", "events_offset", "/fire-events", "events", "event_id", event_wire_row),
)


def pending_batches(sent, ledger=LEDGERS[0]):
    """Rows of one ledger past its watermark, as `(batches, new_sent)`: each
    batch is `(rows, sent_after_it)` so a multi-batch flush advances the
    watermark per accepted batch and a poison batch never makes earlier ones
    re-send forever. The same ids are reused on every retry: rows come from
    the ledger, nothing is minted here. Reads past the watermark only (a
    seek, cheap on every Stop). Nothing is merged: a fire row is what the
    hook wrote when it fired, and what happened after it is its own rows in
    the events ledger."""
    fname, okey, _path, _body, idkey, wire = ledger
    offsets = []
    rows, end = _read_rows(os.path.join(_ledger_dir(), fname), sent.get(okey, 0), offsets)
    new_sent = dict(sent, **{okey: end})
    items, seen = [], set()
    for r, off in zip(rows, offsets):
        if isinstance(r, dict) and r.get(idkey) and r[idkey] not in seen:
            items.append((wire(r), off))
            seen.add(r[idkey])
    batches = []
    for i in range(0, len(items), FLUSH_BATCH):
        chunk = items[i:i + FLUSH_BATCH]
        last = i + FLUSH_BATCH >= len(items)
        batches.append(([r for r, _ in chunk],
                        dict(sent, **{okey: end if last else chunk[-1][1]})))
    return batches, new_sent


# What a hook before v0.59 left behind: `conversions.jsonl`, one verdict per
# line ({fire_id, converted, converted_at, how, override_reason?}), shipped by
# re-sending the fire row with the verdict merged. Rows past the old
# `conversions_offset` were recorded — offline, or held by the throttle — and
# never posted; abandoning them would leave their fires unresolved on the
# server for good (Codex, #240). They are drained once, the old way: the
# server still takes a verdict on POST /fires (as a lowest-precedence
# `client_verdict`, rule-fire-events-spec §5). Nothing writes this file any
# more, so the drain ends when the offset reaches its end.
LEGACY_CONVERSIONS = ("conversions.jsonl", "conversions_offset", "/fires", "fires", "fire_id", None)


def legacy_verdict_batches(sent):
    """Unsent pre-v0.59 verdicts as fire rows with `converted` /
    `converted_at` / `override_reason` merged — `(batches, new_sent)` in the
    shape of :func:`pending_batches`. A verdict whose fire is not in the
    fires ledger cannot be posted and is passed by; the fires ledger is
    scanned once for the ids named, bounded by the number of unsent rows."""
    ldir = _ledger_dir()
    cpath = os.path.join(ldir, "conversions.jsonl")
    if not os.path.isfile(cpath):
        return [], sent
    offsets = []
    convs, end = _read_rows(cpath, sent.get("conversions_offset", 0), offsets)
    new_sent = dict(sent, conversions_offset=end)
    wanted = {c.get("fire_id") for c in convs if isinstance(c, dict) and c.get("fire_id")}
    by_id = {}
    if wanted:
        try:
            with open(os.path.join(ldir, "fires.jsonl"), "rb") as f:
                for line in f:
                    if not line.endswith(b"\n"):
                        break
                    try:
                        r = json.loads(line.decode("utf-8"))
                    except Exception:
                        continue
                    if isinstance(r, dict) and r.get("fire_id") in wanted:
                        by_id[r["fire_id"]] = r
                        wanted.discard(r["fire_id"])
                        if not wanted:
                            break
        except FileNotFoundError:
            pass
    items, seen = [], {}
    for c, off in zip(convs, offsets):
        fid = c.get("fire_id") if isinstance(c, dict) else None
        if fid not in by_id:
            continue
        row = seen.get(fid)
        if row is None:
            row = dict(wire_row(by_id[fid]), converted=None, converted_at=None)
            seen[fid] = row
            items.append((row, off))
        # the old sidecar's own merge: a true is never downgraded by a false
        if c.get("converted"):
            row["converted"], row["converted_at"] = True, c.get("converted_at")
        elif c.get("converted") is False and row.get("converted") is None:
            row["converted"], row["converted_at"] = False, c.get("converted_at")
        if c.get("override_reason") and not row.get("override_reason"):
            row["override_reason"] = c["override_reason"]
    batches = []
    for i in range(0, len(items), FLUSH_BATCH):
        chunk = items[i:i + FLUSH_BATCH]
        last = i + FLUSH_BATCH >= len(items)
        batches.append(([r for r, _ in chunk],
                        dict(sent, conversions_offset=end if last else chunk[-1][1])))
    if not items:
        # nothing postable past the watermark: advance it, or an orphan row
        # would be re-read on every flush forever
        return [([], new_sent)] if convs else [], new_sent
    return batches, new_sent


def _log_rejected(rejected, batch, idkey="fire_id"):
    """Per-row rejections are logged as given; a bare count (the §4.3 example
    shape) is logged with the batch's ids so the loss is visible even
    though the server did not say which rows."""
    try:
        if isinstance(rejected, list):
            items = [{"rejected": it} for it in rejected]
        elif isinstance(rejected, int) and rejected > 0:
            items = [{"rejected_count": rejected,
                      "batch_ids": [r.get(idkey) for r in batch]}]
        else:
            items = []
        if items:
            with open(os.path.join(_ledger_dir(), "rejected.jsonl"), "a", encoding="utf-8") as f:
                for it in items:
                    f.write(json.dumps(dict(it, at=_now())) + "\n")
    except Exception:
        pass


def flush_fires(final=False):
    """POST unsent rows of both ledgers in batches. Each watermark advances
    ONLY on a 2xx, so a failed batch is retried, verbatim, on the next flush;
    `rejected` rows are logged locally and never retried (they sit behind
    the watermark). One flusher at a time via flock; a second caller simply
    leaves. `final` (SessionEnd) ignores the throttle."""
    if portable_lock is None:
        # Retain the ledger rather than advancing it without process exclusivity.
        return
    ldir = _ledger_dir()
    lock = open(os.path.join(ldir, ".flush.lock"), "a+", encoding="utf-8")
    try:
        portable_lock.lock_exclusive(lock.fileno(), blocking=False)
    except OSError:
        lock.close()
        return
    try:
        L = rulebook_ledger   # drop confirmed rows from a ledger past 1 MB
        L and L.rotate(ldir, [x[:2] for x in LEDGERS], _LEDGER_TAIL_BYTES)
        sent = load_sent()
        plan = [(ledger, pending_batches(sent, ledger)[0]) for ledger in LEDGERS]
        legacy, legacy_sent = legacy_verdict_batches(sent)
        if legacy and not any(b for b, _ in legacy):
            # only orphans past the old watermark: retire them without a POST
            _atomic_json(_sent_path(), dict(load_sent(), conversions_offset=legacy_sent["conversions_offset"]))
            legacy = []
        if legacy:
            # after the fires (their rows must be there for the verdict to
            # land on), before the events
            plan.insert(1, (LEGACY_CONVERSIONS, legacy))
        n = sum(len(b) for _, batches in plan for b, _ in batches)
        if not n:
            return
        if not final:
            last = sent.get("last_flush_at")
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
            except Exception:
                age = float("inf")
            if n < FLUSH_EVERY_FIRES and age < FLUSH_EVERY_S:
                return
        api = _api()
        if not api:
            return
        base, bearer, http = api
        for ledger, batches in plan:
            if not _post_batches(ledger, batches, base, bearer, http):
                return       # this ledger's watermark stays; the other waits for the next flush
    finally:
        portable_lock.unlock(lock.fileno())
        lock.close()


def _post_batches(ledger, batches, base, bearer, http):
    """Ship one ledger's batches; True when every batch was accepted. The
    per-batch `.sent` write is re-read from disk first so progress another
    ledger's batches wrote in this same flush is never rolled back."""
    _fname, okey, path, body_key, idkey, _wire = ledger
    accepted = 0
    for batch, after in batches:
        try:
            reply = http.rest(f"{base}{API_PATH}{path}", bearer, "POST",
                              body={body_key: batch}, timeout=FLUSH_TIMEOUT_S)
        except Exception as exc:      # transport/envelope error: retry next flush,
            _breadcrumb("flush", exc)  # but say so where an operator can look
            return False
        if reply.status not in (200, 201, 202):
            return False              # watermark stays at the last accepted batch
        data = reply.data if isinstance(reply.data, dict) else {}
        if not isinstance(data.get("accepted"), int):
            return False              # not the §4.3 reply → do not trust it as a receipt
        rej = data.get("rejected")
        n_rej = len(rej) if isinstance(rej, list) else (rej if isinstance(rej, int) else 0)
        cur = load_sent()             # the on-disk state, including any
        stall = cur.get("stall") or {}  # progress written by earlier batches
        key = batch[0].get(idkey)
        if data["accepted"] + n_rej < len(batch):
            # Short-counted: retry — but not forever. The same batch (same
            # first id) short-counting STALL_QUARANTINE_AFTER times in a row
            # is a poison batch: log it as rejected and move past it, so one
            # bad row can never strand every row behind it.
            n = (stall.get("n", 0) + 1) if stall.get("key") == key else 1
            if n < STALL_QUARANTINE_AFTER:
                cur["stall"] = {"key": key, "n": n}
                _atomic_json(_sent_path(), cur)
                return False
            _log_rejected([{idkey: r.get(idkey), "reason": "quarantined: short-counted "
                            f"{n}x (accepted {data['accepted']}, rejected {n_rej} of {len(batch)})"}
                           for r in batch], batch, idkey)
        else:
            _log_rejected(rej, batch, idkey)
        accepted += data["accepted"]
        # only THIS ledger's watermark moves; the other's is whatever is on disk
        cur[okey] = after[okey]
        if stall.get("key") == key:
            cur.pop("stall", None)    # an accepted batch clears only ITS OWN marker
        cur["last_flush_at"] = _now()
        cur["last_accepted"] = accepted
        _atomic_json(_sent_path(), cur)   # per batch: a later failure keeps this progress
    return True


def repo_info(cwd):
    """(repo_name, worktree_root, gitdir_path, branch).

    The name is the REPO's, not the directory's: a worktree directory is named
    after the branch, so keying the book on it fetched one book per branch and
    matched `scope_repos: ["xmem"]` in none of them (`repo_identity` carries
    the resolution order). Every other field stays physical — `root` is what
    path scope and the diff probes measure, and ordering state must key on
    THIS checkout, not on the repo it belongs to."""
    d = os.path.abspath(cwd or "")
    while d:
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return _repo_name(d, g), d, g, _branch(os.path.join(g, "HEAD"))
        if os.path.isfile(g):   # worktree: "gitdir: /path/to/main/.git/worktrees/x"
            try:
                gitdir = open(g, encoding="utf-8").read().split(":", 1)[1].strip()
                if not os.path.isabs(gitdir):    # `git worktree --relative-paths`
                    gitdir = os.path.normpath(os.path.join(d, gitdir))
            except Exception:
                gitdir = ""
            return (_repo_name(d, gitdir), d, gitdir,
                    _branch(os.path.join(gitdir, "HEAD")))
        parent = os.path.dirname(d)
        if parent == d:  # POSIX, drive, and UNC roots are fixed points.
            break
        d = parent
    return "", "", "", ""


def _repo_name(root, gitdir):
    """The repo `root` belongs to, degrading to its basename when the shim is
    unavailable. A hook that cannot name the repo must still deliver every
    rule that binds every repo."""
    if repo_identity is None:
        return os.path.basename(root)
    try:
        if _rc:
            return _rc.repo_name(BASE, root, gitdir, repo_identity.resolve)
        return repo_identity.repo_name(root, gitdir)
    except Exception:
        return os.path.basename(root)


_SEED_MAX_HOPS = 64   # a Write names a new dir a few levels deep, never thousands


def _under(path, base):
    """True when `path` is `base` or sits beneath it. `join(base, "")` is the
    only spelling of the prefix that is right at a POSIX root ("/"), a Windows
    drive root ("C:\\") and an ordinary directory alike, and it keeps a sibling
    that merely shares a name prefix (/a/bc vs /a/b) out."""
    return path == base or path.startswith(os.path.join(base, ""))


def _acted_on_dir(cwd, inp):
    """The directory of the file this call acts on, in the SESSION's own path
    space, or "" when the payload names none this session may reach.

    Payload data must not steer where the hook looks, so the session cwd is
    the trust boundary. Containment is checked TWICE, and both must hold:

    * lexically, on the unresolved path — because that is the path
      `repo_info` actually walks up from. A symlink OUTSIDE cwd whose target
      is inside it passes a resolved-only check while its lexical parents
      still lead somewhere else entirely, which would hand `root` (and so
      `git -C root`, which honors a repo's local config) to a checkout the
      session never opened;
    * and again once symlinks are resolved — so a link UNDER cwd cannot
      smuggle the lookup out of it.

    Requiring both also pins the value to ONE path space per session: an
    absolute path spelled differently from cwd (/var vs /private/var, an
    automounted home) fails the lexical test and falls back to the cwd
    answer. That matters because `root` keys OrderingEngine state
    (`{rid}@{root}:{branch}`), and one worktree reached two ways would split
    into two keys and silently re-arm its ordering rules."""
    if not (isinstance(inp, dict) and cwd):
        return ""
    base = os.path.normpath(cwd)
    for key in ("file_path", "notebook_path"):      # each judged on its own:
        fp = inp.get(key)                           # a junk file_path must not
        if not (isinstance(fp, str) and fp):        # hide a good notebook_path
            continue
        try:
            d = os.path.dirname(fp.replace("\\", "/"))
            if not os.path.isabs(d):    # relative to the SESSION's cwd, never ours
                d = os.path.join(cwd, d)
            d = os.path.normpath(d)
            if not _under(d, base):
                continue
            probe, hops = d, 0          # a Write may name a directory not created yet
            while not os.path.exists(probe) and os.path.dirname(probe) != probe \
                    and hops < _SEED_MAX_HOPS:
                probe, hops = os.path.dirname(probe), hops + 1
            if _under(os.path.realpath(probe), os.path.realpath(cwd)):
                return d
        except (OSError, ValueError):   # payload strings are untrusted (NUL -> ValueError)
            continue
    return ""


def repo_of_call(data):
    """(repo, root, gitdir, branch) for the checkout this CALL works in: the
    acted-on file's first, the session cwd's second, else all empty.

    The acted-on path outranks cwd because of the worktree-parent workflow —
    an agent running from a directory that CONTAINS many checkouts and editing
    files inside them. cwd resolves nothing there, and gating the rulebook on
    it alone left every rule silently inert for the whole session while the
    edited file sat in a real worktree the entire time. Worktrees themselves
    were never the problem: `repo_info` reads the `.git` FILE and `scope_ok`
    maps the gitdir back to the main checkout, so a rule scoped to the repo
    matches from any of its worktrees once the walk starts in the right place.

    A Bash call carries no path and keeps the cwd answer, so a non-git cwd
    with nothing acted on stays silent exactly as before."""
    cwd = data.get("cwd") or os.getcwd()
    seed = _acted_on_dir(cwd, data.get("tool_input") or {})
    if not seed and data.get("tool_name") == "apply_patch":
        # Codex edits name their files inside the patch text, not in file_path,
        # so the parent-folder workflow above resolved nothing on Codex and its
        # edit rules stayed inert. The first patched file that passes the same
        # containment checks decides the checkout.
        try:
            for path, _new, _added in apply_patch_files(data.get("tool_input") or {}, cwd):
                seed = _acted_on_dir(cwd, {"file_path": path})
                if seed:
                    break
        except (OSError, ValueError):   # payload strings are untrusted
            seed = ""
    if seed:
        info = repo_info(seed)
        if info[0]:
            return info
    info = repo_info(cwd)
    if info[0]:
        return info
    # Desktop may normalize exec_command to Bash and omit workdir. A leading
    # literal `cd ... &&` is explicit context; never guess from sibling repos,
    # mid-command changes, or shell expansions. Keep repository-rooted behavior.
    inp = data.get("tool_input") or {}
    command = inp.get("command") if isinstance(inp, dict) else None
    if data.get("tool_name") == "Bash" and isinstance(command, str):
        match = _CD_PREFIX.match(command)
        if match and match.group(0).rstrip().endswith("&&"):
            target = next((g for g in match.groups() if g), "")
            if target and not any(c in target for c in "$`*?[]{}~\\\n\r"):
                root = command_root(cwd, command)
                if root:
                    return repo_info(root)
    return info


_HEAD_REF = re.compile(r"^ref:\s*refs/heads/(.+)$")


def _branch(head_path):
    """The checked-out branch, whole. `refs/heads/feat/x` is the branch
    `feat/x`, not `x`: splitting on the last slash truncated every branch
    named with the usual `feat/` / `fix/` / `chore/` prefix. That was
    invisible while the value only keyed dedup and rode along on fires, and
    became load-bearing when `given.repo.branch_rx` started deciding whether
    a rule fires — `^feat/` could never match."""
    try:
        h = open(head_path, encoding="utf-8").read().strip()
        m = _HEAD_REF.match(h)
        if m:
            return m.group(1)
        # a symbolic ref outside refs/heads (rare) still is not a detached HEAD
        return h.split(":", 1)[1].strip() if h.startswith("ref:") else ("detached@" + h if re.fullmatch(r"[0-9a-f]{40,64}", h) else "detached")
    except Exception:
        return ""


def state_path(session_id):
    sdir = os.path.join(_base(), "state")
    os.makedirs(sdir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or ""))[:80] or "nosession"
    return os.path.join(sdir, f"{safe}.json")


def load_state(p):
    st = {"fired": [], "counts": {}, "raw": {}, "armed": {},
          "armed_once": [], "armed_version": {}, "spec_pending": {}}
    try:
        with open(p, encoding="utf-8") as f:
            st.update(json.load(f))
    except Exception:
        pass
    if not isinstance(st.get("armed"), dict):   # a file an older hook wrote
        st["armed"] = {}
    # Older hooks kept outcome bookkeeping here — first a conversion watch
    # keyed by RULE (`open`/`open_file`), then one record per FIRE
    # (`obligations`, with the `stops` counter and `armed_fire` beside it).
    # All of it is dropped, not migrated: the server folds outcomes from the
    # events ledger now (rule-fire-events-spec §4), and a verdict an old
    # hook had not yet written is nothing the server was ever told.
    for k in ("open", "open_file", "closed", "open_at", "last_fire",
              "obligations", "stops", "armed_fire"):
        st.pop(k, None)
    if not isinstance(st.get("armed_once"), list):
        st["armed_once"] = []
    if not isinstance(st.get("armed_version"), dict):
        st["armed_version"] = {}
    if not isinstance(st.get("spec_pending"), dict):
        st["spec_pending"] = {}
    return st


def stale_arming(st, rule):
    """Was this rule's session arming written against a DIFFERENT version of
    the rule than the one now loaded? An arming records the version it was
    made for; a rule that has since been refreshed under the same id carries
    an obligation no event of this session armed. Only an arming that
    recorded a version can be stale — one an older hook wrote is kept."""
    rid = rule["id"]
    if rid not in st["armed"] or rid not in st["armed_version"]:
        return False
    return st["armed_version"][rid] != rule.get("_version")


def drop_arming(st, rid):
    """Forget a session arming. Which fire the discharge answers is not kept
    here: the receipt is posted as an event and the server matches it."""
    st["armed"].pop(rid, None)
    st["armed_version"].pop(rid, None)


# The session's obligation keys. A hook process reads the whole state file,
# works, and writes the whole file back; two hooks of ONE session can overlap
# (parallel tool calls, a sub-agent's calls), and the second writer's
# snapshot used to put back an arming the first had just discharged — so a
# gated command stayed blocked after its required command had run. These
# keys are therefore merged by DELTA under a lock: what this process armed
# is added, what it discharged is removed, and everything else is whatever
# is on disk now.
_ARMING_KEYS = ("armed", "armed_version")
_APPEND_KEYS = ("armed_once",)      # only ever appended to
_DELTA_KEYS = _ARMING_KEYS + ("spec_pending",)


def snapshot_arming(st):
    """What the delta-merged keys looked like when this process loaded the
    state — the baseline `save_state` diffs against. Records are copied, so
    a flag set on one after the snapshot reads as a change."""
    return {k: {kk: (dict(vv) if isinstance(vv, dict) else vv)
                for kk, vv in (st.get(k) or {}).items()}
            for k in _DELTA_KEYS}


def _state_lock(p):
    """Exclusive lock on the session state's sidecar, or None past
    LOCK_WAIT_S (the hook fails open — a plain write, today's behaviour)."""
    if portable_lock is None:
        return None
    deadline = time.monotonic() + LOCK_WAIT_S
    replaced = 0
    while True:
        try:
            lock = open(p + ".lock", "a+", encoding="utf-8")
        except Exception:
            return None
        try:
            portable_lock.lock_exclusive(lock.fileno(), blocking=False)
        except OSError:
            lock.close()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.005)
            continue
        if portable_lock.still_at(lock.fileno(), p + ".lock") \
                or (replaced >= 2 and time.monotonic() >= deadline):
            # Keep what we hold once it still mismatches after two reopens
            # past the deadline: the sweep deletes a lock file once, so only a
            # filesystem whose inode numbers are not stable gets here, and it
            # would otherwise spin.
            return lock
        # state_sweep.py deleted this lock file after we opened it: reopen.
        replaced += 1
        portable_lock.unlock(lock.fileno())
        lock.close()


def _write_json_atomic(p, st):
    """Replace the file in one step. A reader that loads without the lock
    (every tool hook does) must never see a truncated file: it would parse
    nothing, default everything, and write those defaults back over the
    session's dedup and counters."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), prefix="." + os.path.basename(p) + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def save_state(p, st, before=None):
    """Write the session state. With `before` (a `snapshot_arming` taken at
    load), the arming keys are merged by delta against the file as it is NOW,
    under the session lock, so a concurrent hook's write cannot resurrect an
    obligation this one discharged, nor drop one this one armed."""
    lock = _state_lock(p) if before is not None else None
    try:
        if lock is not None:
            cur = load_state(p)
            for k in _DELTA_KEYS:
                if k not in before:                    # a snapshot an older caller took
                    continue
                merged = dict(cur.get(k) or {})
                for key in before[k]:
                    if key not in st[k]:
                        merged.pop(key, None)          # discharged / resolved by this process
                for key, v in st[k].items():
                    if key not in before[k]:
                        merged[key] = v                # armed or fired here
                    elif before[k][key] != v:
                        merged[key] = v                # re-versioned here
                st[k] = merged
            for k in _APPEND_KEYS:
                seen = list(cur.get(k) or [])
                st[k] = seen + [x for x in st[k] if x not in seen]
        _write_json_atomic(p, st)
    except Exception:
        pass
    finally:
        if lock is not None:
            try:
                portable_lock.unlock(lock.fileno())
            except Exception:
                pass
            lock.close()


# The wrappers Claude Code puts around a prompt IT generated rather than one a
# person typed: a slash command's expansion, a skill body, a resumed session's
# continuation, a loop wake-up, a background task's notification. Anchored at
# the START and never searched — a prompt that merely QUOTES one of these is a
# person talking about them, which is exactly what a conversation about this
# hook looks like. `transcript_filter._OPENS_WITH_WRAPPER` makes the same
# statement about the same tags for the capture path; this is the hook's own
# copy, because a hook on the prompt path must not grow an import to read one
# regex.
_HARNESS_PROMPT_RX = re.compile(
    r"\s*(?:<(?:command-name|command-message|command-args|local-command-stdout"
    r"|local-command-stderr|local-command-caveat|system-reminder|task-notification)>"
    r"|This session is being continued"
    r"|Caveat: The messages below"
    r"|Base directory for this skill:"
    r"|Another Claude session sent a message:)")

# Another agent's message: a subagent's hand-back, or another session's
# SendMessage, delivered as a user-role prompt. The same sentence
# `harness_extract` already treats as harness. Nobody typed it, so unlike a
# wake-up (`<task-notification>`, which `prompt` rules exist to speak at) it
# is no moment for a prompt rule either: a rule written for "the person asked
# X" fired on a subagent's report that merely mentioned X.
_AGENT_MESSAGE_RX = re.compile(r"\s*Another Claude session sent a message:")


def agent_message(text):
    """True when this UserPromptSubmit is another agent's message — see
    `_AGENT_MESSAGE_RX`. It neither arms an obligation (it is harness text)
    nor fires a `prompt` rule."""
    return bool(_AGENT_MESSAGE_RX.match(text or ""))


def harness_prompt(text):
    """True when this UserPromptSubmit carries something the HARNESS wrote.

    Only what a PERSON typed may arm an obligation: a rule armed by the word
    "staging" in a prompt exists because someone said they were asking about
    staging, and a skill body or a loop wake-up that happens to contain the
    word said nothing of the kind. It would arm the rule for the rest of the
    session with nobody having asked for it.

    Every marker here is STRUCTURED — a wrapper tag, or a sentence the client
    emits verbatim. An ordinary English prefix is not a marker however
    harness-like it reads: `Approach this as` was one, and it silenced
    "Approach this as a staging incident", a real person asking exactly the
    question a staging rule exists for. Suppressing a genuine prompt is the
    worse error of the two, because the rule then never arms and nothing
    anywhere says why."""
    return bool(_HARNESS_PROMPT_RX.match(text or ""))


def session_scoped(rule):
    """Is this ordering rule's obligation the SESSION's rather than the
    checkout's? True when it is armed by the session or by a prompt — the two
    events that happen to one session and not to a worktree."""
    spec = (rule.get("ordering") or {}) if rule.get("on") == "ordering" else {}
    return any(k in tuple(spec.get("armed_by_events", ("edit", "write")))
               for k in ("session", "prompt"))


def arms_on(rule, event, prompt=""):
    """Does `event` arm this ordering rule?

    The one statement of it: `arm_obligations` asks it for the live lanes and
    `rulebook_verify` asks it for a `--fires` case, so a rule the verifier
    says fires is a rule the hook arms. Two copies of this predicate would
    let the authoring tool bless a rule the engine never arms."""
    if rule.get("on") != "ordering":
        return False
    spec = rule.get("ordering") or {}
    if event not in tuple(spec.get("armed_by_events", ("edit", "write"))):
        return False
    if event == "prompt":
        # A prompt lane with no pattern would arm on every prompt, which is a
        # session-armed rule wearing the wrong label. Say which prompts, or
        # arm on none.
        rx = spec.get("armed_by_rx")
        return bool(rx) and bool(re.search(rx, prompt, re.I))
    return True


def arm_obligations(rules, repo, gitdir, session, event, prompt=""):
    """Record the ordering rules THIS event arms, in the session's own state
    file — the one the pre lane already loads and reads.

    `armed_by_events` used to mean the edit family alone, so the only
    obligations the engine could carry were "you changed something, now run
    the suite". The two shapes it could not carry are the ones a team asks for
    most: armed for the whole session ("fetch before you read `origin/*`") and
    armed by what the person just said ("you are asking about staging — probe
    it before you answer"). Both arm at a moment that is not a tool call, so
    both write here rather than into the worktree state the edit lane keeps."""
    arming = [r for r in rules
              if r.get("status", "active") == "active" and scope_ok(r, repo, gitdir)
              and arms_on(r, event, prompt)]
    if not arming:
        return []
    sp = state_path(session)
    st = load_state(sp)
    before = snapshot_arming(st)
    armed = []
    for r in arming:
        rid = r["id"]
        # SessionStart is not once per session. Claude Code fires it again on
        # resume, on `/clear` and after a compaction, under the SAME session
        # id — `capture_health._already_warned` exists for the same reason. A
        # plain re-arm would resurrect an obligation the session had already
        # discharged, so a rule the agent satisfied at the start blocks again
        # an hour later with nothing having changed. Whether this session has
        # EVER been armed by this event is recorded apart from whether it is
        # armed right now.
        #
        # Only `session` is once-only. A second prompt that raises the subject
        # again is a second question and deserves its own probe, so the prompt
        # lane re-arms by design.
        if event == "session":
            once = f"session:{rid}"
            if once in st.setdefault("armed_once", []):
                continue
            st["armed_once"].append(once)   # only the once-only event is recorded here
        st["armed"].setdefault(rid, event)   # first arming wins; re-arming is a no-op
        # The arming belongs to the rule AS IT READ when the prompt matched.
        # A rule refreshed under the same id — `armed_by_rx` changed from
        # `staging` to `production`, say — is a different obligation, and the
        # pre lane drops an arming whose version no longer matches rather
        # than block a call no prompt ever armed for the new text.
        st.setdefault("armed_version", {})[rid] = r.get("_version")
        armed.append(rid)
    save_state(sp, st, before=before)
    return armed


def result_text(resp):
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        parts = [v for k in ("stderr", "stdout", "output", "error", "text")
                 if isinstance((v := resp.get(k)), str) and v]
        return "\n".join(parts) if parts else json.dumps(resp, ensure_ascii=False)
    return str(resp)


BRAND = "XTrace"
# `RULEBOOK_OVERRIDE='<why>' <command>` — a shell env-assignment prefix, so the
# command still runs as typed; the hook only reads the reason and strips the
# assignment before matching. Recognised at the start of the command OR of
# any shell segment (after `&&`, `;`, `|`, `(`, a newline): the agent writes
# `cd repo && RULEBOOK_OVERRIDE='why' git push`, and the gate it is
# answering fired on that segment (found live, e2e 2026-09-01 — a start-only
# anchor left the override unread and the call blocked).
#
# Quoting is the SHELL's, not a regex's: the shell-only text (heredoc bodies
# are data) is tokenised with `shlex` in POSIX mode, so `echo 'a|RULEBOOK_
# OVERRIDE=x git push'` is one quoted word, an apostrophe in an earlier
# heredoc cannot flip the state of a later line, and an unbalanced quote is a
# parse error → no override → the gate stands (fail closed). A grep whose
# ARGUMENT mentions the variable is not an override: the token after `grep`
# is not at a segment start. An EMPTY reason is not an override either
# (`RULEBOOK_OVERRIDE= git push --force` stays gated): the reason is the
# whole price of passing a gate, and it crosses the wire, so it is run
# through `redact_secrets` like everything else that leaves the machine.
_OVERRIDE_PREFIX = "RULEBOOK_OVERRIDE="
# the raw assignment token (quotes intact), used to strip exactly the one
# token find_override validated — never every look-alike in the command
_OVERRIDE_TOKEN_RX = re.compile(r"RULEBOOK_OVERRIDE=(?:'[^']*'|\"[^\"]*\"|\S*)\s*")


def _segment_op(tok):
    return bool(tok) and all(ch in ";&|(" for ch in tok)


def _raw_token(line, reason):
    """The raw text of the assignment on `line` whose shlex value is `reason`
    (quotes intact, trailing space included), or None."""
    for m in _OVERRIDE_TOKEN_RX.finditer(line):
        try:
            val = shlex.split(m.group(0))[0][len(_OVERRIDE_PREFIX):]
        except (ValueError, IndexError):
            continue
        if val.strip() == reason:
            return m.group(0)
    return None


def strip_override(cmd, found):
    """`cmd` with exactly the validated assignment removed — nothing else. A
    look-alike inside a quoted argument elsewhere (`-m 'about
    RULEBOOK_OVERRIDE=…'`) is data the rules must still see intact.

    Preferred: the token on the line find_override read it from, when that
    line occurs verbatim in `cmd`. Otherwise (the line was a joined `\\`
    continuation, so it differs from the raw text) the first raw token whose
    shlex value IS the validated reason — a look-alike with a different value
    is never touched. Either way, one occurrence."""
    reason, line, raw = found
    if raw and line in cmd:
        return cmd.replace(line, line.replace(raw, "", 1), 1)
    for m in _OVERRIDE_TOKEN_RX.finditer(cmd):
        try:
            val = shlex.split(m.group(0))[0][len(_OVERRIDE_PREFIX):]
        except (ValueError, IndexError):
            continue
        if val.strip() == reason:
            return cmd[:m.start()] + cmd[m.end():]
    return cmd


def _tokens(text):
    """shlex tokens for `text`, or None when it does not parse."""
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:                                 # unbalanced quoting
        return None


def _logical_lines(text):
    """(text, tokens) per line, rejoining lines that only parse together.

    A heredoc inside a command substitution splits one shell line across
    several physical ones: `--body "$(cat <<'EOF'` leaves its double quote
    open, and the closing `)"` sits after the body — so neither line parses
    alone while the two together do. Joining is what the shell does anyway.

    This matters because of what skipping an unparseable line COSTS. It was
    silently dropping the override on the commonest gated command there is —
    `gh pr create` with a heredoc body — so the gate denied the call and the
    documented way past it did nothing. A gate whose override cannot be
    reached is a wall.

    A line that parses no better joined with everything after it yields
    ``None`` tokens and the caller skips it: an override we cannot read as
    shell is still not an override.
    """
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        for j in range(i, len(lines)):
            chunk = " ".join(lines[i:j + 1])
            toks = _tokens(chunk)
            if toks is not None:
                yield chunk, toks
                i = j + 1
                break
        else:
            yield lines[i], None
            i += 1


def find_override(cmd):
    """(reason, line, raw_token) for the first `RULEBOOK_OVERRIDE=<why>` that
    begins a shell segment and is non-empty, else None. Tokenised per line of
    the shell-only text with shlex (POSIX quoting, operators as their own
    tokens); a line that parses nowhere, even joined with what follows it,
    contributes nothing (see :func:`_logical_lines`). Every candidate is tried,
    so an earlier empty or quoted one cannot shadow the real override."""
    text = re.sub(r"\\\n", " ", shell_only(cmd))        # join continuation lines
    for line, toks in _logical_lines(text):
        if toks is None:
            continue
        at_start = True
        for tok in toks:
            if at_start and tok.startswith(_OVERRIDE_PREFIX):
                reason = tok[len(_OVERRIDE_PREFIX):].strip()
                if reason:
                    return reason, line, _raw_token(line, reason)
            at_start = _segment_op(tok)
    return None


# `rulebook-override: <why>` — the edit lane's override. A tool call has no
# prefix to carry a reason the way a shell command does, so it travels in the
# only channel an edit already has: the content. Written as a comment in
# whatever the file's language uses, anywhere in the new text.
#
# Unlike the shell prefix this one is NOT stripped before rules match. The
# marker is part of the file the author is writing — a reviewer reads it in the
# diff, and the next edit of that line is already answered. That is the trade:
# a command override is one-shot, an edit override is a durable annotation.
# An EMPTY reason is not an override, exactly as for the shell prefix, and the
# reason is redacted before it is recorded or shown.
_EDIT_OVERRIDE_RX = re.compile(
    r"rulebook-override(?:\[([^\]\r\n]*)\])?\s*:[ \t]*(\S[^\r\n]*)", re.I)
# One comment CLOSER, if the marker ends the line inside a block comment. A
# blind `rstrip("*/->}")` ate the last character of any reason that honestly
# ended in one of them ("the palette lives in {tokens}"), which is a silent
# corruption of the one field a person wrote by hand.
_COMMENT_CLOSE_RX = re.compile(r"\s*(?:\*/|-->|--\}\}|\}\}|#\}|\*\))\s*$")


def find_edit_override(body):
    """Every `rulebook-override[<rule>]: <why>` marker in the new content, as
    ``{rule-name-lowered: reason}`` — first marker wins per name. Empty is not
    an override, and neither is the UNNAMED form: it is returned under ``""``
    only so the deny can tell the author to name the rule.

    A marker must name its rule. The bare form is not shorthand the hook is
    being strict about — its meaning is unstable. Excusing "the gate" reads
    fine the day it is written and silently starts excusing a DIFFERENT rule
    the day a teammate authors a second edit gate over the same line, and the
    line it sits on is a standing exemption from then on. It is also the form
    that content copied from somewhere else can satisfy by accident.

    Deliberately loose about what precedes the word: `//`, `#`, `<!--` and `*`
    are all comment openers somewhere, and a marker the author meant is worth
    more than a syntax the hook guessed."""
    found = {}
    for m in _EDIT_OVERRIDE_RX.finditer(body or ""):
        reason = _COMMENT_CLOSE_RX.sub("", m.group(2).strip()).strip()
        if not reason:
            continue
        found.setdefault((m.group(1) or "").strip().lower(), reason)
    return found


# §3.2: one sentence, two symbols. `⛔️` keeps its existing meaning — this call
# was STOPPED — and every other fire, including a gate someone overrode, takes
# `📏`. The sentence is identical either way, so the shape is one recognisable
# thing and the symbol is what says whether work was actually halted.
# Appended under the advisories of an emitting call (never under a gate alone):
# the call has already run, so the reason for going on without the advice
# travels on the NEXT shell command, naming the rule. One constant, so a test
# can pin the block around it without re-typing it.
ADVISE_FEEDBACK_HINT = (
    "_If you go on without following one of these, say why on your next shell command — "
    "`RULEBOOK_OVERRIDE='[<label>] <why>' <command>` — so the reason is recorded against "
    "that rule instead of silence._")
DISCLOSE_ADVISORY = "📏"
DISCLOSE_BLOCKED = "⛔️"
DISCLOSE_PREFIX = "Rule fired: "
_DESC_WORDS = 20
_DESC_CHARS = 120
# Backticks and asterisks are markup and are dropped; a code span's CONTENT is
# what the reader wants. Underscores are NOT stripped: rule titles name files
# and symbols far more often than they use underscore emphasis, and stripping
# them turned "never edit `snake_case_name.py`" into "never edit
# snakecasename.py" and `__init__.py` into `init.py` — a wrong statement, in
# the terminal and in the transcript the agent echoes it into.
_EMPHASIS_RX = re.compile(r"[*`]+")


def disclosure_desc(rule):
    """The rule in 20 words or fewer: its title, else its statement, else its
    id. One line, never wrapped by us — the terminal and the transcript both
    get exactly this."""
    for key in ("_label", "text"):
        raw = rule.get(key)
        if isinstance(raw, str) and raw.strip():
            break
    else:
        raw = str(rule.get("id") or "")
    text = " ".join(_EMPHASIS_RX.sub("", raw).split())
    words = text.split(" ")
    clipped = len(words) > _DESC_WORDS
    text = " ".join(words[:_DESC_WORDS])
    if len(text) > _DESC_CHARS:
        cut = text[:_DESC_CHARS].rsplit(" ", 1)[0] or text[:_DESC_CHARS]
        text, clipped = cut, True
    return (text + "…") if clipped and text else text


def disclosure_line(rule, blocked=False):
    """The line the user is shown AND the line the agent is told to echo.

    ONE function for both on purpose (§3.4): the terminal showing one string
    while the agent is told to say a different one would be worse than either
    channel alone."""
    marker = DISCLOSE_BLOCKED if blocked else DISCLOSE_ADVISORY
    return f"{marker} {DISCLOSE_PREFIX}{disclosure_desc(rule)}"


def disclosure_instruction(lines):
    """What goes at the end of `additionalContext`, once per emitting call.

    The `systemMessage` copy is deterministic but invisible to everything
    downstream; this copy is the one that lands in the transcript, and so the
    only one session capture, /memhub:start-rulebook, a handoff or a PR
    comment can ever see. Neither alone is enough."""
    quoted = "\n".join(lines)
    return ("\n_Disclose these to the user. Begin your next reply with the following "
            "line(s), verbatim and each on its own line, before anything else — including "
            "before any tool call narration:_\n" + quoted +
            "\n_This is how the team sees its rules working. Do not paraphrase, do not merge "
            "them into a sentence, and do not omit one because it did not change what you were "
            "going to do — a rule that fired and changed nothing is exactly the rule the team "
            "needs to hear about._")


def emit(event_name, text, *, user_line=None, deny=None):
    """One JSON document on stdout. `text` reaches the agent (additionalContext);
    `user_line` reaches the USER (systemMessage — the one field the terminal
    shows); `deny` blocks the call (PreToolUse permissionDecision) with that
    reason. Callers pass all three at once for a gate, the first two for an
    advisory."""
    hso = {"hookEventName": event_name, "additionalContext": text}
    if deny:
        hso["permissionDecision"] = "deny"
        hso["permissionDecisionReason"] = deny
    out = {"hookSpecificOutput": hso}
    if user_line:
        out["systemMessage"] = user_line
    print(json.dumps(out))


def _ledger_dir():
    d = os.path.join(_base(), "ledger")
    os.makedirs(d, exist_ok=True)
    sv = os.path.join(d, "schema_version")
    if not os.path.exists(sv):
        with open(sv, "w", encoding="utf-8") as f:
            f.write(f"{LEDGER_SCHEMA}\n")
    return d


_LEDGER_TAIL_BYTES = 256 << 10   # the fires of the last few minutes, whatever the session count


def _earliest_fire_after(session, since):
    """The earliest `fired_at` of `session` in the fires ledger's tail that
    is later than `since` (ISO instants compare as text within one offset;
    the ledger is one machine's, so they share it), or None."""
    try:
        path = os.path.join(_ledger_dir(), "fires.jsonl")
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - _LEDGER_TAIL_BYTES))
            chunk = f.read()
        best = None
        for line in chunk.split(b"\n")[1 if size > _LEDGER_TAIL_BYTES else 0:]:
            if not line.strip():
                continue
            try:
                r = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            at = r.get("fired_at") if isinstance(r, dict) else None
            if r.get("session_id") == session and isinstance(at, str) and at > since \
                    and (best is None or at < best):
                best = at
        return best
    except Exception:
        return None


def turn_end_at(session, started, transcript_path=None):
    """When the turn whose Stop spawned this process ended.

    The Stop hook is asynchronous, so this process may start after the next
    turn has begun and even fired. The one record of when the turn REALLY
    ended is the transcript: a turn ends with an assistant message whose
    `stop_reason` is `end_turn` (a tool call mid-turn is `tool_use`), and
    the harness writes it before the Stop. The latest such record no later
    than `started` is this turn's end — a fire of the next turn, however
    early it landed, comes after it (Codex, #240).

    Without a transcript (the Codex bridge, an operator's manual flush):
    now, unless a fire of the session was recorded after `started` — then
    one tick before the earliest of those, which bounds the race to the
    hook's own startup window."""
    ended = _turn_end_from_transcript(transcript_path, started)
    if ended:
        return ended
    at = _now()
    early = _earliest_fire_after(session, started)
    if early and early < at:
        at = _just_before(early)
    return at


def _turn_end_from_transcript(tp, before):
    """The timestamp (local ISO, microseconds) of the latest assistant
    `end_turn` record in the transcript's tail written no later than
    `before`, or None. Reads the tail the way `message_id_of` does."""
    if not tp:
        return None
    try:
        limit = datetime.fromisoformat(before)
    except Exception:
        return None
    try:
        with open(tp, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            window = _TAIL_START
            while True:
                start = max(0, end - window)
                f.seek(start)
                lines = f.read(end - start).splitlines()
                if start:
                    lines = lines[1:]
                for raw in reversed(lines):
                    if not raw.strip():
                        continue
                    try:
                        rec = json.loads(raw)
                    except Exception:
                        continue
                    if rec.get("type") != "assistant":
                        continue
                    msg = rec.get("message")
                    if not isinstance(msg, dict) or msg.get("stop_reason") != "end_turn":
                        continue
                    try:
                        ts = datetime.fromisoformat(str(rec.get("timestamp")).replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if ts.tzinfo is None or ts > limit:
                        continue
                    return ts.astimezone().isoformat(timespec="microseconds")
                if start == 0 or window >= _TAIL_MAX:
                    return None
                window *= 4
    except Exception:
        return None


def _just_before(iso):
    """The instant one microsecond before `iso` — the earliest tick the
    server can tell apart from it."""
    try:
        return (datetime.fromisoformat(iso) - timedelta(microseconds=1)).isoformat(timespec="microseconds")
    except Exception:
        return iso


def _now():
    """Microseconds, not seconds: the server orders a fire and the events
    that may answer it by these instants, and an equal instant counts (a
    same-call dismissal is stamped with its fire's). At second precision an
    event from one hook invocation and a fire of the same rule from the
    NEXT invocation in that second would tie, and an earlier conversion
    could answer a later fire (Codex, #240)."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")


# Records that ARE messages. A transcript interleaves many other kinds —
# `attachment` alone outnumbers real messages in a long session, and
# `system` / `file-history-snapshot` / meta rows appear throughout. Several
# carry their own `uuid`, so "the last record with a uuid" is usually not the
# message the tool call belongs to.
_MESSAGE_TYPES = ("user", "assistant")
# Start at 64 KiB and grow: a single record can exceed it (a large tool result
# or an assistant turn with embedded content), and a window that lands mid-record
# would otherwise yield nothing at all.
_TAIL_START = 64 * 1024
_TAIL_MAX = 1024 * 1024


def message_id_of(data):
    """The transcript record the tool call belongs to — the server resolves it
    to the stored message. Reads the tail of the JSONL rather than the whole
    file: these grow to megabytes and this runs on a 5 s hook budget. Any
    problem returns None; the link is optional and never blocks a fire."""
    tp = str(data.get("transcript_path") or "")
    if not tp:
        return None
    try:
        with open(tp, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            window = _TAIL_START
            while True:
                start = max(0, end - window)
                f.seek(start)
                lines = f.read(end - start).splitlines()
                # A non-zero start almost certainly cut the first line in half.
                if start:
                    lines = lines[1:]
                for raw in reversed(lines):
                    if not raw.strip():
                        continue
                    try:
                        rec = json.loads(raw)
                    except Exception:
                        continue
                    if rec.get("type") not in _MESSAGE_TYPES:
                        continue
                    uid = rec.get("uuid")
                    if isinstance(uid, str) and uid:
                        return uid
                if start == 0 or window >= _TAIL_MAX:
                    return None
                window *= 4
    except Exception:
        return None


def agent_id_of(data):
    """NULL = main agent. A subagent's call carries `agent_id` (and
    `agent_type`) at the top of the hook input — verified live 2026-09-07,
    where `transcript_path` is the PARENT session's file, so the path alone
    says "main" for every subagent call. Older builds had no such field and
    wrote the subagent's transcript at <session>/subagents/agent-<id>.jsonl;
    that form is still read second. `given.agent.main` and the ledger's
    `agent_id` both hang off this answer."""
    aid = data.get("agent_id")
    if isinstance(aid, str) and aid.strip():
        return aid.strip()[:64]
    tp = str(data.get("transcript_path") or "")
    if "/subagents/" in tp:
        return os.path.basename(tp).rsplit(".", 1)[0]
    return None


def log_fires(ctx, rules, *, hook_phase, mode, excerpt, raw_counts=None, dedup_keys=None,
              override_reasons=None, fired_at=None, judge=None):
    """One ledger row per (rule, fire) — spec §3.2. Identifiers, not payloads:
    `excerpt` stays in this LOCAL file and never crosses the wire without
    org opt-in. `override_reasons` is {rule_id: why} for the GATES this call
    excused, so a row records the reason for ITS rule — one call can excuse one
    gate and be blocked by another (§5.3). `rulebook_id` is local too — POST /fires carries no book
    dimension (container spec §6.4), so it is absent from WIRE_KEYS on purpose;
    it is here so a local reader can tell which book a fire came from.
    `fired_at` lets a caller stamp a fire and the event that answers it on
    the same call (a same-call dismissal) with one instant.
    `judge` is {rule_id: {"verdict", "p_fit"}} from `judge_fires`: a rule in it
    gets `judge_verdict` and `judge_score` (the score may be null) on its row;
    a rule not in it gets neither key.
    Returns {rule_id: fire_id} so events can point back."""
    if portable_lock is None:
        # Enforcement still runs, but do not create telemetry that cannot drain.
        return {}
    ids, L = {}, rulebook_ledger
    lock = None
    try:
        path = os.path.join(_ledger_dir(), "fires.jsonl")
        lock = L and L.append_lock(_ledger_dir())   # no rotation mid-append
        with open(path, "a", encoding="utf-8") as f:
            for r in rules:
                fid = str(uuid.uuid4())
                ids[r["id"]] = fid
                verdict = (judge or {}).get(r["id"])
                judged = {"judge_score": verdict.get("p_fit"),
                          "judge_verdict": verdict.get("verdict")} if verdict else {}
                f.write(json.dumps({
                    "fire_id": fid, "rule_id": r["id"],
                    "rulebook_id": r.get("_rulebook_id"),
                    "rule_version": ctx["rule_version"] if r.get("_version") is None else r["_version"],
                    "session_id": ctx["session"], "agent_id": ctx["agent_id"],
                    "worktree": ctx.get("worktree"),
                    # Local-only (see wire_row): which host's namespace this
                    # session id belongs to, recorded at fire time because the
                    # flush that ships it may be a different invocation.
                    "host": ctx.get("host"),
                    "source_message_id": ctx.get("source_message_id"),
                    "repo": ctx["repo"], "branch": ctx["branch"], "tool": ctx["tool"],
                    "hook_phase": hook_phase, "mode": mode,
                    "dedup_key": (dedup_keys or {}).get(r["id"]),
                    "raw_matches_before_fire": (raw_counts or {}).get(r["id"]),
                    "fired_at": fired_at or _now(),
                    "override_reason": (override_reasons or {}).get(r["id"]),
                    "excerpt": excerpt[:160],
                    **judged,
                }) + "\n")
    except Exception:
        pass
    finally:
        L and L.release(lock)
    return ids


def log_event(ctx, kind, *, rule_id=None, reason=None, worktree=None, at=None):
    """Append one observation to `events.jsonl` (rule-fire-events-spec §2).
    `worktree` defaults to the call's checkout; pass `None` explicitly for a
    receipt that must answer THIS session's fires only (a session- or
    prompt-armed ordering rule — the sibling session down the hall fetching
    does not answer for this one; the server matches a receipt with no
    checkout by session). Never a verdict: which fire this answers, and
    whether it does, is the server's fold. Fails silent like every ledger
    write — an event that could not be appended is an outcome that stays
    null, never a blocked call."""
    if portable_lock is None:
        return None
    try:
        row = {"event_id": str(uuid.uuid4()), "kind": kind, "rule_id": rule_id,
               "session_id": ctx["session"], "agent_id": ctx.get("agent_id"),
               "host": ctx.get("host"),        # local-only; see event_wire_row
               "worktree": worktree if worktree is not None or kind == "receipt" else ctx.get("worktree"),
               "repo": ctx.get("repo"), "branch": ctx.get("branch"),
               "reason": reason, "at": at or _now()}
        L = rulebook_ledger
        lock = L and L.append_lock(_ledger_dir())   # no rotation mid-append
        try:
            with open(os.path.join(_ledger_dir(), "events.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        finally:
            L and L.release(lock)
        return row["event_id"]
    except Exception:
        return None


def log_legacy_conversion(fire_id, how):
    """A verdict for a fire a PRE-v0.59 hook posted (no checkout key on the
    server, so no event can answer it): one line in the old sidecar, which
    `legacy_verdict_batches` re-posts with the fire the old way. The only
    writer of that file this hook has, and only for those fires."""
    if portable_lock is None:
        return
    try:
        with open(os.path.join(_ledger_dir(), "conversions.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"fire_id": fire_id, "converted": True,
                                "converted_at": _now(), "how": how}) + "\n")
    except Exception:
        pass


# `RULEBOOK_OVERRIDE='[<label>] <why>'` — the label rides INSIDE the value.
# `RULEBOOK_OVERRIDE[label]=…` reads as an array subscript to zsh and fails
# the command before the hook ever sees it; a plain assignment is valid in
# every shell the hook runs under.
_NAMED_REASON_RX = re.compile(r"^\[([^\]\r\n]+)\]\s*(.*)$", re.S)


def split_named_override(reason):
    """`[<label>] <why>` → (label lowered, why); a bare reason → (None, reason)."""
    m = _NAMED_REASON_RX.match(reason or "")
    if not m:
        return None, reason
    return m.group(1).strip().lower(), m.group(2).strip()


def named_rules(label, rules, pool=None):
    """The rules `label` addresses among `pool` (default: the whole book).

    An exact rule id anywhere in the book wins outright: when `label` IS a
    rule's id, only that rule is meant — even on a call where some OTHER
    rule's displayed title happens to be the same word, and even when that
    rule is the one firing right now. Only a label that is nobody's id is
    matched by title. One statement of it, used by the gate excuse, the
    same-call dismissal and the later dismissal alike, so `[<rule id>]`
    always means the same rule (Codex, #240)."""
    pool = rules if pool is None else pool
    if any(str(r["id"]).lower() == label for r in rules):
        return [r for r in pool if str(r["id"]).lower() == label]
    return [r for r in pool if str(r.get("_label") or r["id"]).lower() == label]


def resolve_dismissals(rules, dismissals):
    """Each `{label: why}` → the ONE rule it names, for a `dismissed` event.
    Returns `(resolved, ambiguous)`: `[(rule, why)]` and `[(label, n)]`.

    Resolved against the whole book: this hook no longer knows which fires
    are pending (the server does), and a dismissal of a rule with no fire to
    answer is simply an event nothing folds. Titles are not unique across
    the union of books, so a label that fits more than one rule records
    nothing and is reported back as ambiguous, so the agent can name the
    rule by its id — and an exact id outranks a displayed label, as the gate
    resolver does, so that recovery works even when one rule's id equals
    another's label."""
    done, ambiguous = [], []
    for label, why in dismissals.items():
        if not label or not why:
            continue
        hits = named_rules(label, rules)
        if len(hits) > 1:
            ambiguous.append((label, len(hits)))
        elif hits:
            done.append((hits[0], why))
    return done, ambiguous


def _dismissal_lines(set_aside, ambiguous):
    """The acknowledgement, on both channels: (agent lines, user lines)."""
    agent = [f"_Recorded: [{label}] set aside — {why}_" for label, why in set_aside]
    user = [f"{BRAND} ▸ [{label}] set aside: {why}" for label, why in set_aside]
    for label, n in ambiguous:
        agent.append(f"_`[{label}]` fits {n} rules with a fire pending — nothing recorded; "
                     "name one by its rule id instead_")
        user.append(f"{BRAND} ▸ [{label}] fits {n} rules — name one by its rule id")
    return agent, user


# §2. A CONSTANT, not a template: no rule counts, no repo name, nothing that
# changes between sessions, so a reader who has seen it once can skip it.
# ~65 words / ~420 chars, charged to every session that has any rule, and
# deliberately NOT counted against POSTURE_BUDGET_CHARS — that budget bounds
# rule CONTENT, and this framing is what makes the content usable. Do not let
# it past ~500 chars without deciding that trade again.
SESSION_PREAMBLE = (
    "These are your team's engineering rules — standing instructions from your teammates, "
    "carrying the same weight as this repo's CLAUDE.md. Follow them as you would CLAUDE.md: "
    "they are how this team works, not suggestions to weigh. When one fires, you MUST disclose "
    "it to the user on its own line, exactly `📏 Rule fired: <the rule, in 20 words or fewer>`, "
    "before anything else in that reply."
)

MAX_BOOKS_NAMED = 6       # session-start roster: bounded, like every other context spend
ROSTER_MAX_CHARS = 1000   # …and bounded again in bytes, since it is not charged to the budget


def books_line(carried):
    """"Which policies bind me?" — answered once, at session start, and only
    when more than one book is in play. A single-book team sees exactly what
    it saw before; in-flight fires never carry the book name, because the
    name is not what makes the advice actionable and that slot is the scarce
    one. Books are listed widest first, the same order precedence uses.

    `carried` is what this session actually got — the posture rules that fit
    the budget plus the armed rules — never the in-scope set. The budget is
    spent widest-first, so a narrow book can contribute nothing; telling the
    agent it is holding fifteen of that book's notes when it is holding none
    is a worse failure than saying nothing at all."""
    books, order = {}, []
    for r in carried:
        rid = r.get("_rulebook_id")
        if not rid:
            continue
        if rid not in books:
            books[rid] = {"name": r.get("_book_label") or r.get("_book_name"), "n": 0,
                          "rank": book_rank(r), "kind": r.get("_book_kind"),
                          "scope": r.get("_book_scope"), "members": r.get("_book_members")}
            order.append(rid)
        books[rid]["n"] += 1
    if len(books) < 2:
        return None
    parts = []
    for rid in sorted(order, key=lambda i: (books[i]["rank"], (books[i]["name"] or "").casefold())):
        b = books[rid]
        # An org/personal label already says who it reaches.
        if b["kind"] in ("org", "personal"):
            who = None
        elif b["scope"] == "all_org":
            who = "org-wide"
        elif isinstance(b["members"], int):
            who = f"{b['members']} member{'s' if b['members'] != 1 else ''}"
        else:
            who = None
        bits = ", ".join([x for x in (who, f"{b['n']} rule{'s' if b['n'] != 1 else ''}") if x])
        parts.append(f"{b['name'] or 'unnamed rulebook'} ({bits})")
    extra = len(parts) - MAX_BOOKS_NAMED
    shown = parts[:MAX_BOOKS_NAMED]
    if extra > 0:
        shown.append(f"and {extra} more")
    # bounded like every other context spend: the budget above is a hard cap
    # and this line is not charged to it
    return ("- _From " + " · ".join(shown))[:ROSTER_MAX_CHARS] + "._"


def session_digest(rules, repo, gitdir, ctx):
    in_scope = [r for r in rules if scope_ok(r, repo, gitdir) and r.get("status", "active") == "active"]
    if not in_scope:
        return
    # Spec §2: at most MAX_POSTURE session rules and ~2k tokens per scope.
    # ONE budget across every book the caller is in (container spec §13.1) —
    # books do not know about each other, so four of them could otherwise blow
    # a cap each of them believes it is under. The books take turns
    # (`posture_order`), and every rule past either limit is logged SUPPRESSED
    # so the ledger sees it.
    posture_all = posture_order([r for r in in_scope if r.get("on") == "session"])
    posture, cut, used = [], [], 0
    for r in posture_all:
        cost = len(r.get("text") or "") + len(r.get("why") or "")
        if len(posture) < MAX_POSTURE and used + cost <= POSTURE_BUDGET_CHARS:
            posture.append(r); used += cost
        else:
            cut.append(r)
    active = [r for r in in_scope if r.get("on") != "session"]
    # A rule this hook cannot read in full advises instead of gating and says
    # so on its first fire — but a rule whose only `armed_by_events` value is
    # an event this hook has no lane for never fires at all, so that notice
    # has nowhere to land and the rule is exactly as silent as it was before
    # `min_hook_version` existed.
    #
    # It is surfaced HERE rather than at the gated command. Firing it there
    # would mean firing a rule whose arming condition this hook cannot
    # evaluate — the precise failure the degradation machinery exists to
    # prevent — and it would repeat on every matching call. Session start is
    # where "once per session" already lives, and the fact the reader needs is
    # not the rule, it is that their plugin is too old to run it.
    stale = [r for r in in_scope if r.get("_degraded")]
    lines = [f"## {DISCLOSE_ADVISORY} Rulebook (team rules — advisory)", SESSION_PREAMBLE]
    for r in posture:
        lines.append(f"- {r['text']}{_why(r)}")
    if active:
        lines.append(
            f"- {len(active)} rule{'s' if len(active) != 1 else ''} armed for "
            f"this repo — they fire inline as you work (proactive on tool "
            f"calls, reactive on errors). Treat a fire as a teammate's note, "
            f"not boilerplate.")
    if stale:
        names = ", ".join(sorted(str(r.get("_label") or r["id"]) for r in stale)[:5])
        lines.append(
            f"- {len(stale)} rule{'s' if len(stale) != 1 else ''} in your book "
            f"need{'' if len(stale) != 1 else 's'} a newer {BRAND} plugin than "
            f"this one ({names}) — {'they run' if len(stale) != 1 else 'it runs'} "
            f"as advice and cannot gate. Update the plugin to get "
            f"{'them' if len(stale) != 1 else 'it'} back.")
    roster = books_line(posture + active)
    if roster:
        lines.append(roster)
    emit("SessionStart", "\n".join(lines))
    if posture:
        log_fires(ctx, posture, hook_phase="session", mode="advise", excerpt="")
    if cut:
        log_fires(ctx, cut, hook_phase="session", mode="suppressed", excerpt="",
                  dedup_keys={r["id"]: f"{r['id']}@session" for r in cut},
                  raw_counts={r["id"]: 0 for r in cut})


def refresh_if_stale(repo, rules, fetched_at, sources):
    """(rules, fetched_at, sources), with the book re-fetched first when it is
    old enough to be wrong.

    Only when stale: a book younger than the pre lane's refresh window is
    current, so the common case starts nothing (no child). A stale one is
    worth waiting for, bounded by
    SESSION_FETCH_TIMEOUT_S — `fetch_book` leaves the cache untouched on every
    failure path, so a timeout proceeds with exactly what we already had.

    Shared by the two lanes that get ONE look at their trigger. The session
    digest is a session's only view of the book; a prompt is the only chance a
    prompt-armed rule gets. Both were written this way; only one of them had
    the code."""
    if os.environ.get("MEMHUB_RULEBOOK_FETCH", "1") == "0":
        return rules, fetched_at, sources
    try:
        if _age_s(fetched_at) >= REFRESH_AFTER_S:
            fetch_book(repo, timeout=SESSION_FETCH_TIMEOUT_S)
            rules, _, fetched_at, sources = load_rules(repo)
    except Exception:
        pass
    return rules, fetched_at, sources


def _host_arg(argv=None):
    """`--host claude|codex|cursor` — which agent host this call came from.

    Same flag the other hook scripts already take (`capture_health.py --host`,
    `pr_link_trigger.py --host codex`). Defaults to claude, whose manifest
    passes nothing, so existing behaviour is byte-for-byte unchanged.
    """
    args = sys.argv[1:] if argv is None else argv
    for i, arg in enumerate(args):
        if arg == "--host" and i + 1 < len(args):
            value = args[i + 1]
            if value in ("claude", "codex", "cursor"):
                return value
        elif arg.startswith("--host="):
            value = arg.split("=", 1)[1]
            if value in ("claude", "codex", "cursor"):
                return value
    return "claude"


def prompt_lane(rules, repo, root, gitdir, branch, session, ctx, data, text):
    """Fire the `prompt` matcher rules this UserPromptSubmit matches — the one
    moment a wake-up (a `/loop` tick, a `<task-notification>`) reaches the
    hook at all, since what the agent does next is plain text and a tool this
    hook never sees. Advise only (`to_hook_rule` forces it): the prompt is
    already sent, so there is nothing to refuse. Same envelope, dedup and
    ledger as the pre lane, with `hook_phase: prompt` and the event as the
    tool, so the server folds these fires like any other."""
    # A prompt carries no path, so path scope reads it as a Bash call does:
    # an include-scoped rule never fires, an exclude-only one may.
    live = [r for r in rules if r.get("on") == "prompt" and r.get("status", "active") == "active"
            and scope_ok(r, repo, gitdir) and path_in_scope(r, "", root)]
    if not live:
        return
    ctx = dict(ctx, tool="UserPromptSubmit")
    sp = state_path(session)
    st = load_state(sp)
    before = snapshot_arming(st)
    probes = Probes(root, branch, transcript_path=data.get("transcript_path"),
                    agent_id=ctx["agent_id"])
    fired, dedup_keys = [], {}
    for r in live:
        rid = r["id"]
        scope = r.get("fire_scope", "session")
        key = rid if not scope.startswith("branch") else f"{rid}:{branch}"
        matched = evaluate(r, hook_phase="prompt", tool="UserPromptSubmit", prompt=text) \
            and given_ok(r, probes)
        if scope != "call" and key in st["fired"]:
            if matched:
                st["raw"][rid] = st["raw"].get(rid, 0) + 1   # what dedup swallowed
            continue
        if not matched:
            continue
        st["raw"][rid] = st["raw"].get(rid, 0) + 1
        if scope != "call":
            st["fired"].append(key)
        dedup_keys[rid] = key
        fired.append(r)
    if not fired:
        save_state(sp, st, before=before)
        return
    fired.sort(key=book_rank)
    shown, cut = fired[:MAX_ADVISE], fired[MAX_ADVISE:]
    lines = [f"## {BRAND} Rulebook (team rules — advisory, not blocking)"]
    user_lines, disclosures = [], []
    for r in shown:
        label = r.get("_label") or r["id"]
        lines.append(f"- **[{label}]** {r['text']}{_why(r)}")
        stale_key = f"_degraded:{r['id']}"
        if r.get("_degraded") and stale_key not in st["fired"]:
            st["fired"].append(stale_key)
            lines.append(f"  _(advice only — {r['_degraded']}. Update the "
                         f"{BRAND} plugin to let this rule gate.)_")
        line = disclosure_line(r)
        disclosures.append(line)
        user_lines.extend([line, f"   {BRAND} ▸ [{label}] {r['text']}"])
    lines.append(ADVISE_FEEDBACK_HINT)
    lines.append(disclosure_instruction(disclosures))
    try:
        emit("UserPromptSubmit", "\n".join(lines), user_line="\n".join(user_lines))
    except Exception:
        pass
    fired_at = _now()
    raw = {r["id"]: st["raw"].get(r["id"]) for r in fired}
    # The excerpt is local-only, and a prompt is the person's words: record
    # which wrapper matched, not what they wrote around it.
    for r in shown:
        m = re.search(r["rx"], text, re.I | re.M)
        log_fires(ctx, [r], hook_phase="prompt", mode="advise", excerpt=m.group(0) if m else "",
                  raw_counts=raw, dedup_keys=dedup_keys, fired_at=fired_at)
    for r in cut:
        log_fires(ctx, [r], hook_phase="prompt", mode="suppressed", excerpt="",
                  raw_counts=raw, dedup_keys=dedup_keys, fired_at=fired_at)
    for r in shown:
        st["raw"][r["id"]] = 0
    save_state(sp, st, before=before)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pre"
    codex_pre = mode == "codex-pre"
    if codex_pre:
        mode = "pre"
    if mode == "fetch" and len(sys.argv) > 2:      # detached child: repo on argv
        fetch_book(sys.argv[2])
        return 0
    if mode == "book-path":
        # For /memhub:create-rule's live forward-test (§4.2), which arms a
        # candidate by editing this exact file. It asks the hook where the book
        # is rather than recomputing the hash: a second implementation of
        # book_path in a skill would drift from the one the hook reads, and the
        # test would then doctor a file nothing loads.
        repo = sys.argv[2] if len(sys.argv) > 2 else ""
        if not repo.strip():
            print("usage: rulebook_hook.py book-path <repo> [cwd]", file=sys.stderr)
            return 2
        # The optional cwd answers "where does a call made THERE read its book
        # from" — which is what a forward test needs, and what tells it whether
        # its redirect took effect before it writes a candidate anywhere.
        set_active_base(sys.argv[3] if len(sys.argv) > 3 else "")
        print(book_path(repo))
        return 0
    if mode == "flush":
        started = _now()          # before anything else: the Stop happened no later than this
        final = "final" in sys.argv[2:]
        # The payload's one useful field: which session's open obligations to
        # close before the rows go out. Anything unreadable → just flush.
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except Exception:
            data = {}
        session = data.get("session_id") if isinstance(data, dict) else None
        # A turn is the PERSON's turn: a subagent's Stop (top-level agent_id,
        # as harness_stop.py reads it) and a Stop re-entered while a stop hook
        # is already running (`stop_hook_active`) are not turns, or one parent
        # turn would count as two and the server would close its fires a
        # turn early. The event says only that a turn ended; the server
        # counts them (rule-fire-events-spec §3: the second one after a fire
        # closes it), so a Stop that lands late still lands after the fire.
        own_turn = isinstance(data, dict) and not data.get("stop_hook_active") \
            and not str(data.get("agent_id") or "").strip()
        if session and own_turn:
            try:
                repo, root, _gitdir, branch = repo_of_call(data)
            except Exception:
                repo, root, branch = "", "", ""
            # Which lifecycle event this is comes from the PAYLOAD, not from
            # `final`: `final` only lifts the flush throttle, and the Codex
            # bridge passes it on every Stop (Codex has no SessionEnd hook to
            # flush from), so reading it as "the session ended" would close
            # every Codex advisory at the end of the turn it fired in. A
            # payload that names no event (an operator's manual `flush
            # final`) is taken at its word.
            hook_event = str(data.get("hook_event_name") or "")
            if hook_event == "SessionEnd" or (final and not hook_event):
                kind = "session_end"
            else:
                kind = "turn_end"
            # The Stop hook runs ASYNC beside the next turn: a fire of turn
            # N+1 can land in the ledger before this process stamps turn N's
            # end, and the server would then count this end as the first one
            # after that fire and close it a turn early (Codex, #240). The
            # turn ended before this process started, so it ended before any
            # fire this session recorded after `started`; stamp it one tick
            # before the earliest of those.
            # `host` too: this lane builds its ctx inline rather than reusing
            # the one `main` assembles, and without it `event_wire_row` has no
            # namespace to apply. Observed live — a Codex session's FIRES said
            # `codex-<uuid>` while its own turn_end event said `<uuid>`, and the
            # server folds events into fires by (org, session_id), so every
            # turn_end folded into nothing.
            log_event({"session": session, "agent_id": None, "repo": repo or None,
                       "branch": branch or None, "worktree": worktree_key(root),
                       "host": _host_arg()}, kind,
                      at=turn_end_at(session, started, data.get("transcript_path")))
        flush_fires(final=final)
        if _rbp:
            _rbp.drain_legacy(globals(), final)  # the pre-keying shared ledger
        # Once a day, delete the plugin's local state nothing will read again
        # (state_sweep.py). Only on the real install: an overridden base is a
        # test, or create-rule's private forward-test base, and neither may
        # reach into ~/.config/memhub-plugin.
        if not os.environ.get("MEMHUB_RULEBOOK_BASE"):
            try:
                import state_sweep  # noqa: PLC0415 — beside this file
                state_sweep.maybe_sweep()
            except Exception:
                pass
        return 0
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    session = data.get("session_id", "")
    # `repo_of_call` derives this too, but the probe root below still needs
    # the SESSION's directory: a `cd` or `-C` in the command is resolved
    # against where the terminal is, not against the edited file's checkout.
    cwd = data.get("cwd") or os.getcwd()
    # Before any book is read: a §4b forward test may have claimed this cwd,
    # in which case every book read below comes from its private base instead
    # of the shared one. No claim (the overwhelming case) → the real base.
    set_active_base(cwd)
    repo, root, gitdir, branch = repo_of_call(data)
    if not repo:            # never invent a repository for a projectless call
        if mode == "session" and _host_arg() == "codex":
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    "MemHub Rulebook: this task starts outside a Git repository. "
                    "Codex may omit exec_command.workdir from hook payloads, so "
                    "formal rules cannot identify that checkout. For repository "
                    "shell commands, use an explicit, shell-quoted absolute "
                    "`cd <repo> && ...` prefix (including when workdir is set), "
                    "or start the task in the repository."
                )}}))
        return 0
    if mode == "fetch":
        fetch_book(repo)
        return 0
    if mode == "upgrade":
        book = load_book(repo) or {}
        maybe_refresh(repo, None if upgrade_status(repo) else book.get("fetched_at"))
        show_upgrade(repo, session, "PreToolUse")
        return 0
    rules, rule_version, fetched_at, sources = load_rules(repo)
    # A Codex bridge trusted before SessionStart was wired (three handlers)
    # never ran the session lane's fetch. Refresh before a first/stale
    # pre-call so such an install cannot silently miss a gate.
    if codex_pre and _age_s(fetched_at) >= REFRESH_AFTER_S:
        rules, fetched_at, sources = refresh_if_stale(repo, rules, fetched_at, sources)
    tool = data.get("tool_name", "")
    ctx = {"session": session, "agent_id": agent_id_of(data), "repo": repo,
           "branch": branch, "tool": tool, "rule_version": rule_version,
           "source_message_id": message_id_of(data), "worktree": worktree_key(root),
           "host": _host_arg()}
    if mode == "prompt":
        # UserPromptSubmit. It arms ordering rules silently: anything printed
        # here is injected above the person's own words, and an arming is not
        # news — the fire at the gated command is. The one thing it SAYS is a
        # `prompt` matcher rule (0.99.0), whose whole point is to speak at
        # this moment — below, after the arming.
        #
        # The book is refreshed FIRST, on the same terms the session lane
        # uses. A prompt is the only chance a prompt-armed rule gets: evaluate
        # it against a stale book and the matching prompt is GONE, so a rule
        # activated while the session sat idle stays unarmed and silently
        # permits its gated commands until somebody happens to raise the
        # subject again. The pre lane's detached refresh cannot help — it
        # lands after the prompt it needed to see.
        text = str(data.get("prompt") or "")
        if text and not harness_prompt(text):
            rules, fetched_at, sources = refresh_if_stale(repo, rules, fetched_at, sources)
            arm_obligations(rules, repo, gitdir, session, "prompt", prompt=text)
        elif text:
            # A wake-up is not worth making the session wait for the server:
            # it recurs, and the detached refresh reaches the next one.
            maybe_refresh(repo, fetched_at)
        if text and not agent_message(text):
            prompt_lane(rules, repo, root, gitdir, branch, session, ctx, data, text)
        return 0
    # Repo facts answer about the tree the COMMAND runs in; which rules bind
    # you is still the session's repo, and stays keyed on it.
    #
    # Only a Bash call carries a shell command, and only a shell command can
    # `cd` or name a `--base`. Another tool's input may hold a field called
    # `command` meaning something else entirely, and reading that one as shell
    # would point the diff probes at a tree the call never touches. The gate
    # and override paths below already restrict themselves to Bash; this reads
    # the same field, so it is restricted the same way.
    cmd_text = ((data.get("tool_input") or {}).get("command") or "") if tool == "Bash" else ""
    probe_root, probe_branch = root, branch
    elsewhere = command_root(cwd, cmd_text)
    if elsewhere and elsewhere != root:
        probe_root = elsewhere
        probe_branch = _branch(os.path.join(repo_info(elsewhere)[2], "HEAD"))
    probes = Probes(probe_root, probe_branch, command=cmd_text,
                    transcript_path=data.get("transcript_path"), agent_id=ctx["agent_id"])

    if mode == "session":
        # Fetch BEFORE rendering, but only when the book is old enough to be
        # wrong. The digest is a session's only view of the book, and rendering
        # from the cache first made it show the PREVIOUS session's rules: a rule
        # activated or paused on the server needed two session starts to appear
        # or to go away. A book younger than the pre lane's refresh window is
        # already current, so SessionStart fetches nothing and spawns
        # nothing — the common case, since the pre lane refreshed it minutes
        # ago. Only a stale book is worth waiting for, and never longer than
        # SESSION_FETCH_TIMEOUT_S: `fetch_book` leaves the cache untouched on
        # every failure path, so a timeout renders exactly what we already had.
        rules, fetched_at, sources = refresh_if_stale(repo, rules, fetched_at, sources)
        try:        # which source each rule came from — the pilot's merge audit
            _atomic_json(book_path(repo) + ".sources", {"at": _now(), "sources": sources})
        except Exception:
            pass
        if show_upgrade(repo, session, "SessionStart"):
            return 0
        session_digest(rules, repo, gitdir, ctx)
        arm_obligations(rules, repo, gitdir, session, "session")
        return 0
    if mode == "pre":
        maybe_refresh(repo, fetched_at)
    if show_upgrade(repo, session, "PreToolUse" if mode == "pre" else "PostToolUse"):
        return 0

    inp = data.get("tool_input") or {}
    sp = state_path(session)
    st = load_state(sp)
    before = snapshot_arming(st)      # `save_state` merges the arming keys by delta
    fired_now = []

    cmd = str(inp.get("command", "")) if tool == "Bash" else ""
    override_reason = None            # §5.3: set only by the RULEBOOK_OVERRIDE prefix
    if cmd:
        found = find_override(cmd)    # an empty or quoted one is no override — the gate stands
        if found:
            override_reason = redact_secrets(found[0])[:2000]
            cmd = strip_override(cmd, found)   # rules match the command, not the assignment
    # `RULEBOOK_OVERRIDE='[<label>] <why>'` names its rule: a gate on THIS
    # call by that label is excused; otherwise it is the agent setting an
    # earlier advisory aside, and the reason is recorded on that rule's fire.
    override_label = None
    if override_reason is not None:
        raw_reason = override_reason
        override_label, override_reason = split_named_override(override_reason)
        if override_label is not None and not override_reason:
            override_label, override_reason = None, None   # `'[x]'` alone: no reason, no override
        elif override_label is not None and not any(
                override_label in (str(r["id"]).lower(), str(r.get("_label") or r["id"]).lower())
                for r in rules):
            # A bracket that names no rule in the book is part of the reason
            # (`'[WIP] hotfix, CI green'`), not an address: the unnamed form,
            # exactly as it read before labels existed.
            override_label, override_reason = None, raw_reason
    fp = str(inp.get("file_path", ""))
    body = str(inp.get("new_string", "")) + str(inp.get("content", "")) + \
        "\n".join(str(e.get("new_string", "")) for e in (inp.get("edits") or []) if isinstance(e, dict))
    added = edit_added_text(inp)
    # Codex's apply_patch: one Edit/Write event per file it names (below).
    patched = apply_patch_files(inp, cwd) if tool == "apply_patch" else []
    if patched:
        fp = patched[0][0]
        body = "\n".join(text for _, _, text in patched)
        added = None
    # §5.3: the edit lane's own override. Which gates it actually excuses is
    # decided once the gates are known — a marker naming a rule excuses that
    # rule only.
    edit_markers = ({k: redact_secrets(v)[:2000] for k, v in find_edit_override(body).items()}
                    if mode == "pre" and (tool in EDIT_TOOLS or patched) else {})
    rtext = result_text(data.get("tool_response")) if mode == "post" else ""
    resp = data.get("tool_response") if (mode == "post" and tool == "Bash") else None
    ordering = None
    dedup_keys = {}

    # The events this call is. The tool call itself, always; and for a Bash
    # call that wrote files, one synthetic Write per file, so an edit rule sees
    # a heredoc / write_text() / sed -i the way it sees the Write tool. The
    # edit lane of `evaluate` is a pre-phase lane and the ordering engine arms
    # on a post-phase edit, so a synthetic event carries both phases.
    # Synthetic edits go FIRST: inside the command the writes happened before
    # its final segment, so a `python fix.py && pytest` must read as edit,
    # then receipt — the other order would arm an obligation the same call
    # already discharged.
    real = {"tool": tool, "phase": mode, "order_phase": mode, "cmd": cmd, "fp": fp,
            "body": body, "added": added, "rtext": rtext, "resp": resp, "via": None, "read": None}
    events = []
    if tool == "Bash":
        marks = st.setdefault("bash_t0", {})
        call_key = str(data.get("tool_use_id") or "last")
        if mode == "pre":
            marks[call_key] = time.time()
            for k in list(marks)[:-BASH_EDIT_MARKS_KEPT]:
                marks.pop(k, None)
        else:
            t0 = marks.pop(call_key, None)
            if t0 is None and call_key != "last":
                t0 = marks.pop("last", None)
            wants_edits = any(r.get("on") in ("edit", "ordering") and r.get("status", "active") == "active"
                              for r in rules)
            if t0 is not None and wants_edits:
                for path, is_new in bash_written_files(root, cmd, t0):
                    text = read_edit_body(path, is_new)
                    if text is None:
                        continue
                    events.append({"tool": "Write", "phase": "pre", "order_phase": "post",
                                   "cmd": "", "fp": path, "body": text, "rtext": "",
                                   "resp": None, "via": "bash"})
    # A patch is an edit tool call, not a file a command already wrote: its
    # events keep the call's own phase, exactly like Claude's Edit. The edit
    # lane judges it at pre, where a gate can still refuse it (via None), and
    # the ordering engine arms at post — which Codex sends only for a patch
    # that applied, so a failed or refused patch arms nothing.
    for path, is_new, text in patched:
        events.append({"tool": "Write" if is_new else "Edit", "phase": mode, "order_phase": mode,
                       "cmd": "", "fp": path, "body": text, "rtext": rtext, "resp": None,
                       "via": None, "read": None})
    events.append(real)
    # Reads (§5.1): what this call would pull into the context. The Read tool
    # is the call itself; a Bash call contributes one synthetic Read per file
    # a cat/head/tail/less/more/sed segment names, in the pre phase, because
    # unlike a written file these have not happened yet and CAN be refused.
    # Measured only when a read rule is armed: a line count is one file walk.
    wants_reads = mode == "pre" and any(r.get("on") == "read" and r.get("status", "active") == "active"
                                        for r in rules)
    if wants_reads and tool in READ_TOOLS and fp:
        real["read"] = read_facts(fp, offset=inp.get("offset"), limit=inp.get("limit"))
    elif wants_reads and tool == "Bash" and cmd:
        for path, pulled in bash_reads(cwd, cmd):
            events.append({"tool": "Read", "phase": "pre", "order_phase": "pre", "cmd": cmd,
                           "fp": path, "body": "", "rtext": "", "resp": None,
                           "via": "bash-read", "read": read_facts(path, pulled=pulled)})

    # Conversions (rule-fire-events-spec §2): did this call run the action a
    # rule's `converted_rx` names? Posted as a `converted` event for EVERY
    # such rule, whether or not it fired — this hook no longer knows what is
    # pending, and does not need to: the server ignores a conversion with no
    # earlier fire of the rule in this session, and a fire the Stop lane
    # already closed is still answered by an action timestamped before the
    # close. Deterministic, under-counts, never over-counts (spec §5.1).
    # Collected here, POSTED after the fire pass below: a call that both fires
    # a rule and matches its converted_rx (a failing-tests output rule whose
    # signal is the same `pytest`) must not convert the fire it just caused —
    # the two would share one second-precision instant, and an event at the
    # fire's instant answers it. The old obligation logic converted only fires
    # already open; skipping the rules THIS call fires is the same statement.
    converted_hits = []
    # The branch diff costs ~20 git calls (finding the base branch is most of
    # it), so it is only taken when a `spec_untouched` entry for this root and
    # branch is waiting to be answered; with none, the loop below finds nothing.
    if mode == "post" and any(
            isinstance(p, dict) and p.get("root") == probe_root and p.get("branch") == probe_branch
            for p in st["spec_pending"].values()):
        changed = probes.diff_paths()
        if changed is not None:
            for rule in rules:
                pending_key = json.dumps([rule["id"], probe_root, probe_branch])
                pending = st["spec_pending"].get(pending_key)
                if (isinstance(pending, dict) and pending.get("root") == probe_root
                        and pending.get("branch") == probe_branch
                        and pending.get("paths") and all(path in changed and path in (probes.active_spec_paths(pending.get("spec_dir", "docs/specs")) or set()) for path in pending["paths"])
                        and scope_ok(rule, repo, gitdir)):
                    converted_hits.append(rule["id"])
                    del st["spec_pending"][pending_key]
    if mode == "post" and tool == "Bash" and cmd:
        stripped = strip_comments(shell_only(cmd))
        for r in rules:
            crx = r.get("converted_rx")
            if crx and r.get("status", "active") == "active" and scope_ok(r, repo, gitdir):
                try:
                    hit = re.search(crx, stripped, re.I | re.M)
                except re.error:
                    hit = None
                if hit:
                    converted_hits.append(r["id"])
    # (A `content_rx` rule's re-edit conversion is deferred — spec §4: the
    # fire would need to know its file, and the wire row does not carry it.)

    # A named override, or an edit marker, that names no gate on this call is
    # the agent setting an earlier advisory aside. Collected here, before the
    # fire pass: the command carrying it usually fires nothing itself.
    # Only the SHELL form dismisses. An edit marker is a durable annotation
    # that stays in the file: every later edit that carries the line would
    # dismiss whatever fire of that rule is pending anywhere in the session —
    # the "content copied from elsewhere" hazard the gate lane refuses the
    # unnamed marker for. A marker still excuses the gate on its own call.
    dismissals = {}
    if mode == "pre" and override_label is not None:
        dismissals[override_label] = override_reason

    # What the fire pass below is about to mark, as it stands now — so a rule
    # the judge holds back can have exactly its own marks undone (`unfire`).
    marks = {"fired": list(st["fired"]), "counts": dict(st["counts"]),
             "spec_pending": dict(st["spec_pending"])}

    # Anchor rules (§4.7): matched locally, against the command (heredoc
    # bodies dropped, credentials redacted, ≤ 400 chars — the text the judge is
    # shown) or the edited file's path. Same filters the server applied: active,
    # repo scope, path scope, not yet fired this session. Book order; the
    # advisory cap below keeps at most MAX_ADVISE of them on screen.
    # `MEMHUB_RULEBOOK_RECALL=0` turns the lane off.
    handle, apath = "", ""
    if tool == "Bash" and cmd:
        handle = redact_secrets(shell_only(cmd))[:400]
    elif (tool in EDIT_TOOLS or patched) and fp:
        handle = apath = fp
    if mode == "pre" and handle and os.environ.get("MEMHUB_RULEBOOK_RECALL", "1") != "0":
        n_anchor = 0
        for r in rules:
            if n_anchor >= MAX_ADVISE:
                break
            if r.get("on") != "anchor" or r.get("status", "active") != "active" \
                    or r["id"] in st["fired"] or not scope_ok(r, repo, gitdir) \
                    or not path_in_scope(r, apath, root) \
                    or not anchor_hits(r, handle):
                continue
            st["fired"].append(r["id"])
            dedup_keys[r["id"]] = r["id"]
            fired_now.append(r)
            n_anchor += 1

    fired_on = {}          # rule id → the event that fired it (its path, for the ledger and the line)
    for ev in events:
        etool, ephase, ecmd, efp, ebody = ev["tool"], ev["phase"], ev["cmd"], ev["fp"], ev["body"]
        for r in rules:
            if r.get("on") in ("session", "anchor") or not scope_ok(r, repo, gitdir) \
                    or r.get("status", "active") != "active":   # draft = not armed (§6)
                continue
            rid = r["id"]
            if rid in fired_on:
                continue

            if r.get("on") == "ordering":
                # Path scope says which EDITS arm the obligation. The receipt
                # and the gated call are Bash and carry no path, so they are
                # never filtered here — only an edit outside the scope is.
                if etool in EDIT_TOOLS and not path_in_scope(r, efp, root):
                    continue
                if stale_arming(st, r):
                    # Armed for an earlier version of this rule. The rule that
                    # arms on `staging` and the one that now arms on
                    # `production` share an id and nothing else; no prompt of
                    # this session matched the new one, so it is not armed.
                    drop_arming(st, rid)
                try:
                    ordering = ordering or OrderingEngine(root, branch)
                    ok = bash_ok(ev["resp"], strict=r.get("mode") == "gate") \
                        if ev["resp"] is not None else None
                    outcome = ordering.feed(r, hook_phase=ev["order_phase"], tool=etool, cmd=ecmd,
                                            file_path=efp, ok=ok, armed=st["armed"].get(rid))
                except Exception:
                    outcome = None
                if outcome == "discharged":
                    # The session's own arming is discharged here, not in the
                    # worktree state: it was never written there.
                    drop_arming(st, rid)
                    # The green receipt, as a fact for the server to fold. A
                    # checkout-armed rule's receipt carries the checkout key,
                    # so it answers every fire of the rule in this checkout —
                    # this session's and any sibling's. A session- or
                    # prompt-armed rule's obligation belongs to THIS session
                    # (the sibling down the hall fetching does not answer for
                    # this one), so its receipt carries no checkout and the
                    # server matches it by session.
                    log_event(ctx, "receipt", rule_id=rid,
                              worktree=None if session_scoped(r) else ctx["worktree"])
                    for legacy_fid in r.pop("_legacy_fires", None) or []:
                        log_legacy_conversion(legacy_fid, "discharged")
                elif outcome == "fired":
                    dedup_keys[rid] = f"{rid}@{root}:{branch}"
                    fired_now.append(r)
                    fired_on[rid] = ev
                continue

            if not path_in_scope(r, efp if etool in EDIT_TOOLS + READ_TOOLS else "", root):
                continue
            scope = r.get("fire_scope", "session")
            # A gate blocks EVERY matching call — never deduped (§5.3), in
            # any lane the hook can refuse: a Bash command, an edit the tool
            # has not written yet, or a read — the Read tool's own, or one a
            # Bash segment is about to make. `via` guards the difference: a
            # synthetic EDIT event is a file a command ALREADY wrote, so it
            # cannot be refused and keeps its rule's ordinary dedup; a
            # synthetic READ has not happened, and refusing the command is
            # refusing the read.
            if ephase == "pre" and r.get("mode") == "gate" and ev.get("via") in (None, "bash-read") \
                    and ((etool == "Bash" and r.get("on") == "bash")
                         or (etool in EDIT_TOOLS and r.get("on") == "edit")
                         or (etool in READ_TOOLS and r.get("on") == "read")):
                scope = "call"
            key = rid if not scope.startswith("branch") else f"{rid}:{branch}"
            # the regex first (pure, cheap), the given second (probes run only now)
            matched = evaluate(r, hook_phase=ephase, tool=etool, cmd=ecmd, file_path=efp,
                               body=ebody, result_text=ev["rtext"], added=ev.get("added")) \
                and given_ok(r, probes, read=ev.get("read"))
            if scope != "call" and not scope.startswith("counter") and key in st["fired"]:
                if matched:
                    st["raw"][rid] = st["raw"].get(rid, 0) + 1   # what dedup swallowed
                continue
            if not matched:
                continue
            st["raw"][rid] = st["raw"].get(rid, 0) + 1
            if scope.startswith("counter"):
                try:
                    threshold = int(scope.split(":", 1)[1])
                except (IndexError, ValueError):
                    threshold = 1               # a malformed scope must not silence the whole call
                st["counts"][rid] = st["counts"].get(rid, 0) + 1
                if st["counts"][rid] != threshold:   # fire exactly once, at the Nth hit
                    continue
            st["fired"].append(key)
            dedup_keys[rid] = key
            spec_given = (r.get("given") or {}).get("repo") or {}
            if spec_given.get("spec_untouched"):
                hits = probes.untouched_specs(spec_given.get("spec_dir", "docs/specs")) or []
                st["spec_pending"][json.dumps([rid, probe_root, probe_branch])] = {"root": probe_root, "branch": probe_branch, "spec_dir": spec_given.get("spec_dir", "docs/specs"), "paths": [spec.path for spec, _ in hits]}
                r = dict(r)
                r["text"], r["_spec_detail"] = _spec_untouched_text(r["text"], hits)
            fired_now.append(r)
            fired_on[rid] = ev

    # One instant for everything this call records: its fires, and the
    # conversions it observed. A conversion for a rule this call ALSO fires
    # (a second failing pytest for a rule whose signal is `pytest`) is stamped
    # one microsecond earlier: it answers the rule's earlier fires — the old
    # obligation logic converted what was already open — and not the fire
    # this call is about to cause. Everything else shares the instant.
    fired_at = _now()
    fired_ids = {r["id"] for r in fired_now}
    for rid in converted_hits:
        log_event(ctx, "converted", rule_id=rid,
                  at=_just_before(fired_at) if rid in fired_ids else fired_at)

    def _excerpt(r):
        """Local-only (never on the wire). A fire from a Bash-written file
        records the file, prefixed so a reader can count how many edits
        arrive through Bash versus the Write tool."""
        ev = fired_on.get(r["id"])
        if ev and ev.get("via") == "bash":
            return f"bash-edit {ev['fp']}"
        if ev and ev.get("via") == "bash-read":
            return f"bash-read {ev['fp']}"
        return cmd or fp or ""

    # The rule judge (rule-judge-spec §5): does each matched rule fit this
    # turn? Every judged fire carries the answer on its row. Only a reply with
    # `enforce` on can put a rule in `held`, and a held rule leaves `fired_now`
    # HERE — before the gates are picked, before the advisory cap is spent and
    # before any disclosure line is built — so it is not shown, blocks
    # nothing and takes nobody's slot. Its fire is still recorded, as
    # `suppressed` with the verdict, once per turn: that row is how a rule the
    # judge wrongly hid gets found. A repeat match later in the same turn is
    # answered from the turn's cache and writes no second row.
    #
    # A held rule's dedup is ROLLED BACK (`unfire`): the fire pass has already
    # marked it spent for the session, and a rule that did not fit this turn
    # must be able to fire in the next one. Shadow, a failed call, the lane
    # switched off: `held` is empty and nothing below this block changes.
    judged, held, fresh = judge_fires(st, data, repo=repo, session=session, tool=tool, cmd=cmd,
                                      fp=fp, root=root, fired_now=fired_now, fired_on=fired_on)
    for r in [r for r in fired_now if r["id"] in held]:
        if r["id"] in fresh:
            log_fires(ctx, [r], hook_phase=mode, mode="suppressed", excerpt=_excerpt(r),
                      raw_counts={r["id"]: st["raw"].get(r["id"])}, dedup_keys=dedup_keys,
                      fired_at=fired_at, judge=judged)
        unfire(st, r, dedup_keys.get(r["id"]), marks, reset_raw=r["id"] in fresh)
    fired_now = [r for r in fired_now if r["id"] not in held]

    def _dismiss(dismissals):
        """Post a `dismissed` event per rule the override names; returns the
        acknowledgement lines' inputs. Which fires it answers — every fire of
        the rule in this session before it, closed or not — is the server's
        fold (rule-fire-events-spec §3)."""
        resolved, ambiguous = resolve_dismissals(rules, dismissals) if dismissals else ([], [])
        recorded = []
        for r, why in resolved:
            log_event(ctx, "dismissed", rule_id=r["id"], reason=why)
            recorded.append((r.get("_label") or r["id"], why))
        return recorded, ambiguous

    if not fired_now:
        set_aside, ambiguous = _dismiss(dismissals)
        save_state(sp, st, before=before)
        if set_aside or ambiguous:
            # Say so on both channels: a recorded reason nobody can see is
            # the silence this exists to replace.
            agent_lines, user_lines = _dismissal_lines(set_aside, ambiguous)
            try:
                emit("PreToolUse", "\n".join(agent_lines), user_line="\n".join(user_lines))
            except Exception:
                pass
        return 0

    # §5.3: which of this call's fires are GATES. Only a call the hook sees
    # BEFORE it runs can be blocked — a pre-hook Bash command, or a pre-hook
    # edit, which `evaluate` matches against `tool_input`, the content the tool
    # is about to write. A fire from the synthetic lane (`via == "bash"`, a file
    # discovered after a command wrote it) is post-hoc whatever its rule says,
    # so it advises: there is nothing left to refuse.
    def _gateable(r):
        if mode != "pre" or r.get("mode") != "gate":
            return False
        via = (fired_on.get(r["id"]) or {}).get("via")
        if via == "bash":
            return False
        if tool == "Bash":
            return r.get("on") in ("bash", "ordering") or (r.get("on") == "read" and via == "bash-read")
        if tool in READ_TOOLS:
            return r.get("on") == "read"
        # A patch at pre has not applied yet, so it can be refused like an Edit.
        return (tool in EDIT_TOOLS or bool(patched)) and r.get("on") == "edit"

    gate_ids = {r["id"] for r in fired_now if _gateable(r)}

    # Which gates this call actually excused, and why — per RULE, not per call.
    # A Bash override is a one-shot prefix on one command: it passes that call,
    # every gate on it, and is gone. An edit marker LANDS in the file, so the
    # same generosity would make one line a standing exemption from every edit
    # gate that ever fires on it — including rules written after the marker,
    # whose author never saw it. So an edit marker excuses only the rule it
    # NAMES (`rulebook-override[no-hex]: …`, the label the deny line shows).
    # The unnamed form never excuses anything, however few gates fired: it
    # would mean a different thing on the day a second edit gate is authored
    # over the same line, and it is the form that content copied from
    # elsewhere satisfies by accident.
    def _label_of(r):
        return str(r.get("_label") or r["id"]).lower()

    overridden = {}
    # Titles are not unique across the union of books (the server matches no
    # titles at filing time), so a NAMED excuse that fits more than one gate
    # on this call excuses none of them — fail closed — and the deny says to
    # name the rule by its id, which the named form also takes.
    gates_here = [r for r in fired_now if r["id"] in gate_ids]
    label_count = {}
    for r in gates_here:
        label_count[_label_of(r)] = label_count.get(_label_of(r), 0) + 1
    ambiguous_gate = None                # (label, n) when a named excuse fit n gates

    def _named_gates(label):
        return named_rules(label, rules, gates_here)

    if override_reason is not None and override_label is None:
        overridden = {r["id"]: override_reason for r in gates_here}
    elif override_reason is not None:
        # the named shell form excuses exactly the gate it names, as an edit
        # marker does; a label that names no gate here stays a dismissal
        named = _named_gates(override_label)
        if len(named) == 1:
            overridden[named[0]["id"]] = override_reason
            dismissals.pop(override_label, None)
        elif named:
            ambiguous_gate = (override_label, len(named))
            dismissals.pop(override_label, None)
    elif edit_markers:
        for label, why in edit_markers.items():
            named = _named_gates(label) if label else []
            if len(named) == 1:
                overridden[named[0]["id"]] = why
            elif named:
                ambiguous_gate = (label, len(named))
    gates = [r for r in fired_now if r["id"] in gate_ids]
    # §11: precedence between books is the hook's, and it is an ORDERING —
    # the wider book's rule is what MAX_ADVISE keeps when two books both fire
    # on one call. Stable, so one book's rules keep their order and a backend
    # with no book facts ranks every rule alike and is unaffected.
    #
    # An anchor rule outranks book width: it fires at most once per session
    # (it is marked spent as soon as it matches), so letting two org-wide
    # regexes displace it means it is never offered again.
    advisories = sorted((r for r in fired_now if r["id"] not in gate_ids),
                        key=lambda r: (0 if r.get("on") == "anchor" else 1, book_rank(r)))
    # the advisory cap never cuts a gate — a silently un-gated push is the one
    # failure a gate exists to prevent
    shown, cut = gates + advisories[:MAX_ADVISE], advisories[MAX_ADVISE:]
    # The named form on the very call that FIRES the advisory: the agent knew
    # the command would trip it and said why up front. The fire and its
    # `dismissed` event are stamped with one instant, so the event is never
    # earlier than the fire it answers. Resolved against the advisories SHOWN
    # (a fire the cap cut is a suppressed row with no outcome to give), exact
    # id first, and never when the same override already excused a gate: one
    # named override answers one rule.
    # A label that names a rule firing on THIS call is answered here and only
    # here — a gate excused above, an advisory dismissed at its own fire
    # below, a cut one with no outcome to give — never also as a dismissal
    # of the rule's earlier fires, which the event at the fire's instant
    # already answers server-side.
    if override_label is not None and named_rules(override_label, rules, fired_now):
        dismissals.pop(override_label, None)
    set_aside, ambiguous = _dismiss(dismissals)
    same_call = {}
    gate_took_it = any(r["id"] in overridden
                       for r in named_rules(override_label, rules, gates)) if override_label else False
    if override_label is not None and not gate_took_it:
        here = named_rules(override_label, rules, [r for r in shown if r["id"] not in gate_ids])
        if len(here) == 1:
            same_call[here[0]["id"]] = override_reason
            ack = (here[0].get("_label") or here[0]["id"], override_reason)
            if ack not in set_aside:
                set_aside.append(ack)
        elif here:
            ambiguous.append((override_label, len(here)))
    blocked = any(r["id"] not in overridden for r in gates)
    lines = [f"## {BRAND} Rulebook — BLOCKED" if blocked
             else f"## {BRAND} Rulebook (team rules — advisory, not blocking)"]
    user_lines, deny_lines = [], []
    if set_aside or ambiguous:
        agent_ack, user_ack = _dismissal_lines(set_aside, ambiguous)
        lines.extend(agent_ack)
        user_lines.extend(user_ack)

    def _where(r):
        """A fire from a file the Bash call wrote names the file: the model
        knows which file a Write was, but a heredoc's target is buried in
        the command it just ran."""
        ev = fired_on.get(r["id"])
        if not ev or ev.get("via") not in ("bash", "bash-read"):
            return ""
        path = ev["fp"]
        if root and path.startswith(root.rstrip("/") + "/"):
            path = os.path.relpath(path, root)
        if ev.get("via") == "bash-read":
            n = (ev.get("read") or {}).get("lines")
            return f" _(`{path}`, {n} lines, read by that command)_" if n is not None \
                else f" _(`{path}`, read by that command)_"
        return f" _(in `{path}`, written by that command)_"

    # §3: every fire is disclosed, on BOTH channels. The disclosure line comes
    # first and today's line is kept verbatim beneath it, indented — so nothing
    # a user recognises is lost, and the brand stays off the line the agent is
    # told to echo into the transcript.
    disclosures = []
    for r in shown:
        label = r.get("_label") or r["id"]
        detail = f" — {r['_gate_msg']}" if r.get("_gate_msg") else ""
        # A rule this hook could not read in full ran as advice. Say so with
        # the fire, once per session per rule — through `st["fired"]`, the
        # same dedup every `fire_scope: session` rule already uses, so this
        # cannot disagree with it about what "once per session" means.
        # Held until AFTER this rule's own bullet, below. Emitted here it landed
        # ABOVE the bullet — which for every rule but the first reads as a note
        # on the PREVIOUS one, crediting one rule's degradation to another.
        stale_key = f"_degraded:{r['id']}"
        note = ""
        if r.get("_degraded") and stale_key not in st["fired"]:
            st["fired"].append(stale_key)
            note = (f"  _(advice only — {r['_degraded']}. Update the "
                    f"{BRAND} plugin to let this rule gate.)_")
        blocked_here = r["id"] in gate_ids and r["id"] not in overridden
        if r["id"] not in gate_ids:
            lines.append(f"- **[{label}]** {r['text']}{detail}{_where(r)}{_why(r)}{r.get('_spec_detail', '')}")
            detail_line = f"{BRAND} ▸ [{label}] {r['text']}{detail}{_where(r)}"
        elif r["id"] in overridden:
            why = overridden[r["id"]]
            lines.append(f"- **[{label}]** {r['text']}{detail}{_where(r)}{_why(r)}{r.get('_spec_detail', '')} "
                         f"_(gate overridden: {why})_")
            detail_line = f"{BRAND} ⚠ gate overridden — [{label}] {why}"
        else:
            lines.append(f"- **BLOCKED [{label}]** {r['text']}{detail}{_where(r)}{_why(r)}{r.get('_spec_detail', '')}")
            detail_line = f"{BRAND} ⛔ blocked by [{label}] {r['text']}{detail}{_where(r)}"
            # a label shared by two gates is no address; give the id alongside
            ident = f" (rule id {r['id']})" if label_count.get(_label_of(r), 0) > 1 else ""
            deny_lines.append(f"[{label}]{ident} {r['text']}{detail}{_where(r)}")
        if note:
            lines.append(note)
        # A gate that was overridden still FIRED and the call still ran, so it
        # takes 📏; ⛔️ is reserved for a call that was actually stopped.
        line = disclosure_line(r, blocked=blocked_here)
        disclosures.append(line)
        user_lines.append(line)
        user_lines.append("   " + detail_line)
    # The advisory's feedback channel. The call has already run, so a reason
    # travels on the NEXT shell command, naming the rule — what turns a silent
    # non-conversion into a recorded one the rule's reviewer can read.
    if any(r["id"] not in gate_ids for r in shown):
        lines.append(ADVISE_FEEDBACK_HINT)
    deny = None
    if blocked:
        # Each lane names the override it actually accepts: an Edit tool call
        # has no shell prefix to carry one, and telling the agent to re-run a
        # command it never ran would leave the gate with no way past.
        still = [r for r in gates if r["id"] not in overridden]
        if tool == "Bash":
            how = ("re-run the same command prefixed RULEBOOK_OVERRIDE='<why>' — that allows "
                   "exactly that call and records why")
        elif tool in READ_TOOLS:
            # No prefix and no content on a Read call. The ways past are the
            # ways the rule wants: a narrower read, or a delegate whose ANSWER
            # comes back instead of the file. The recorded override rides the
            # Bash lane, which is the one that can carry a reason.
            how = ("read only the part you need (`offset`/`limit`), or hand the question to a "
                   "subagent so its answer, not the file, enters this context; if the whole "
                   "file must be read here, run RULEBOOK_OVERRIDE='<why>' cat <path> in Bash — "
                   "that allows exactly that read and records why")
        else:
            # Always the named form: a marker stays in the file, so it has to
            # say which rule it answers to the reader who finds it later.
            named = ", ".join(f"`rulebook-override[{r.get('_label') or r['id']}]: <why>`"
                              for r in still)
            how = (f"add a comment naming the rule you are excusing ({named}) — each allows "
                   "that one rule, records why, and stays in the diff for the next reader")
            if "" in edit_markers:
                how += (". A `rulebook-override:` with no rule in brackets excuses nothing — "
                        "it would mean something different as soon as a second edit gate "
                        "covers this line")
        if ambiguous_gate:
            how = (f"`[{ambiguous_gate[0]}]` fits {ambiguous_gate[1]} of this call's gates, so it "
                   f"excused none — name the one you mean by its rule id, "
                   f"`[<rule id>] <why>`, in the same override form; {how}")
        deny = (f"Blocked by the {BRAND} team rulebook:\n" + "\n".join(f"- {l}" for l in deny_lines)
                + f"\nIf this is a legitimate exception, {how}.")
        lines.append(f"_This call was blocked. If it is a legitimate exception, {how}._")
    # Last, so it is the instruction the agent reads on the way out — and after
    # the override guidance, which is what it needs first when a gate stood.
    if disclosures:
        lines.append(disclosure_instruction(disclosures))
    try:
        emit("PreToolUse" if mode == "pre" else "PostToolUse", "\n".join(lines),
             user_line="\n".join(user_lines), deny=deny)
    except Exception:
        pass
    raw = {r["id"]: st["raw"].get(r["id"]) for r in fired_now}

    ids = {}
    for r in (r for r in shown if r["id"] not in gate_ids):
        # An advise row carries no reason: a same-call dismissal is the event
        # below, and the row's outcome columns are the server's to write.
        ids.update(log_fires(ctx, [r], hook_phase=mode, mode="advise", excerpt=_excerpt(r),
                             raw_counts=raw, dedup_keys=dedup_keys, fired_at=fired_at,
                             judge=judged))
    if gates:      # a blocked call and an overridden one are both delivered gate fires
        ids.update(log_fires(ctx, gates, hook_phase=mode, mode="gate", excerpt=cmd or fp or "",
                             raw_counts=raw, dedup_keys=dedup_keys,
                             override_reasons=overridden, fired_at=fired_at, judge=judged))
    for r in cut:   # the per-call cap has a cost; make it visible, never silent
        log_fires(ctx, [r], hook_phase=mode, mode="suppressed", excerpt=_excerpt(r),
                  raw_counts=raw, dedup_keys=dedup_keys, fired_at=fired_at, judge=judged)
    for r in shown:
        st["raw"][r["id"]] = 0
        if ids.get(r["id"]) and r["id"] in same_call:
            # dismissed on the call that fired it — an event at the fire's
            # own instant, which the server folds before anything later
            log_event(ctx, "dismissed", rule_id=r["id"], reason=same_call[r["id"]], at=fired_at)
    save_state(sp, st, before=before)
    return 0

if __name__ == "__main__":
    try:
        import rulebook_cache as _rc
    except Exception:
        pass
    try:
        rc = main()
    except BaseException:
        if os.environ.get("MEMHUB_RULEBOOK_DEBUG"):      # stderr only; stdout stays silent
            import traceback
            traceback.print_exc()
        rc = 0
    sys.exit(rc or 0)
