#!/usr/bin/env python3
"""Rulebook hook — three delivery lanes for team engineering rules.

Lanes (the mode argument):
  session  SessionStart: posture rules (on="session") in full, everything else
           as ONE compact index line. Session start is the weakest attention
           slot (measured 4% vs 88% for in-flight), so it carries worldview,
           never enforcement.
  pre      PreToolUse: proactive advisories at the violation moment (on="bash",
           "edit", "write_stdlib") and the ordering-rule GATE (on="ordering").
  post     PostToolUse: reactive advisories on failing/erroring results
           (on="result"); ordering-rule ARM (edit-family) and RECEIPT (bash).
  fetch    Refresh the server book for one repo (GET /rules?view=hook with
           If-None-Match) into <BASE>/book/<repo>.json. The session lane spawns
           it DETACHED so SessionStart never waits on the network.
  flush    Stop / SessionEnd: POST unsent ledger rows to /fires in batches,
           behind a sent-watermark (ledger/.sent). `flush final` ignores the
           every-N-fires / every-M-minutes throttle.

Book = the server book, fetched once per session and cached with its ETag.
Offline → the cached book; no cache → no rules. There is no local rule file:
rules are authored through the memhub `create_rule` tool.

One book, several rulebooks. A rulebook is a container with its own membership
(container spec §3, §4), and one person can be bound by more than one — an
org-wide book plus their team's. The fetched book is the union of the rules
that bind them, and each rule carries `rulebook_id` and a `rulebook` block
with the book's `name`, `scope` and `member_count`. The server computes no
precedence and stores no conflict edges (D14): it ships those facts and the
hook decides. Here, "wider wins" is an ORDERING and never a suppression —
`book_rank` puts org-wide rules ahead of a three-person book's so that the
per-call MAX_ADVISE cap and the session-start posture budget spend on the
policy that binds the most people first. A rule cut by a cap is logged
`mode="suppressed"`, exactly as before. A backend that predates the container
change sends no book facts at all; every rule then ranks alike, both sorts are
stable, and this build behaves as it did — which is what lets one plugin serve
a migrated and an unmigrated backend.

How a fire reaches people (spec §5.3):
  * The agent gets `additionalContext` — the rule text under an XTrace Rulebook
    header — and the USER gets a `systemMessage` line per rule (`XTrace ▸ …`),
    the one hook field the terminal renders. Without it a fire is invisible to
    the person the rule was written for.
  * `mode: gate` rules BLOCK: a pre-hook Bash call matching a gate rule is
    denied (`permissionDecision: deny`) with the statement and the override
    line. `RULEBOOK_OVERRIDE='<why>' <command>` allows exactly that call and
    records the fire with `override_reason`; the next matching call is gated
    again. Gates are never deduped and never cut by the advisory cap. Only a
    Bash rule can gate (an edit already happened; a result rule runs after the
    fact).
  * A gate is honoured from whatever book is cached, however old. There is no
    timer that turns a gate off: a rule retired on the server disappears at
    the next successful fetch, and a running session refreshes its own book
    once it is an hour old (pre lane, detached, throttled). A stale gate costs
    one `RULEBOOK_OVERRIDE`; a gate that silently stops enforcing because the
    server was unreachable for a day is the failure a gate exists to prevent.

What leaves the machine, exactly:
  * fetch  — the repo directory name, nothing else.
  * fires  — identifiers only: rule id, session, repo, branch, tool, timestamps.
             The matched `excerpt` is written to the LOCAL ledger and is
             stripped before the POST.
  * recall — the anchor lane, and the one exception: the server's relevance
             judge needs the call itself, so it gets the file path, or the
             command line (heredoc bodies dropped, credential shapes redacted,
             truncated to 400 chars). Redaction is a denylist, not a guarantee.
             `MEMHUB_RULEBOOK_RECALL=0` turns this lane off and keeps the rest.

Usage (wired in hooks.json): printf %s "$IN" | python3 rulebook_hook.py {session|pre|post}

State (book cache, ordering state, fire ledger) lives under
$MEMHUB_RULEBOOK_BASE, else ~/.config/memhub-plugin/rulebook. Stdlib only; every failure path exits 0 with no output — a
broken hook must never touch the tool call or the session.

Two engines, one evaluate():
  * matcher rules — `evaluate()` is a pure function of (rule, event), so it
    can be exercised in isolation by the tests.
  * ordering rules — "run X after the last edit, before Y": an obligation
    state machine keyed by (worktree_root, branch, rule), never by session,
    so receipts from subagents and sibling sessions in the same checkout count.

A matcher rule may also carry a `given` block — predicates the call must
satisfy AFTER its regex matched: `repo` facts (branch, what the branch has
changed against its base, a dirty tree) and `user` facts (what the person
typed this session). They are answered by `Probes`, lazily and once per hook
call, from read-only git and the local transcript; a fact that cannot be
established never satisfies a predicate, so the rule stays silent. `given_ok()`
is pure over a Probes, which is how the verifier feeds it fixtures.
"""
import fcntl
import fnmatch
import hashlib
import datetime as _dt
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
from datetime import datetime, timezone

BASE = os.environ.get("MEMHUB_RULEBOOK_BASE") or \
    os.path.expanduser("~/.config/memhub-plugin/rulebook")
MAX_ADVISE = 2          # per tool call — habituation guard
MAX_POSTURE = 15        # spec §2: session_context is hard-capped at 15 rules / ~2k tokens per scope
POSTURE_BUDGET_CHARS = 8000   # ~2k tokens at ~4 chars/token
LOCK_WAIT_S = 0.05      # ordering state lock: fail open past this
LEDGER_SCHEMA = 2       # ledger/fires.jsonl row shape (spec §3.2)
BOOK_DIR = os.path.join(BASE, "book")
REFRESH_AFTER_S = 3600       # pre lane: refresh a book this old in the background…
REFRESH_RETRY_S = 600        # …and retry no more than this often while the server is down
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
    """A command string is two languages in one: the shell that executes and
    the data it carries. Drop heredoc BODY lines; keep every shell line,
    including commands after a terminator. Measured on 57 real transcripts:
    first-`<<` truncation hid 44% of real pushes (`commit -F - <<'MSG' … &&
    git push`); full-string matching made ~half of all fires ghosts.
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


def last_segment(shell):
    """The final command segment of a shell string (split on ;, &&, ||, newline)."""
    parts = [x.strip() for x in re.split(r"&&|\|\||;|\n", shell) if x.strip()]
    return parts[-1] if parts else ""


# ── files a Bash call wrote ─────────────────────────────────────────────────
#
# An edit rule says `event: edit`, and until now that meant the Edit/Write
# tools only. But a model writes files through Bash all the time — `cat > f
# <<EOF` to create, a `python - <<PY … write_text()` to modify, `sed -i` —
# and in auto mode it is TOLD to. Measured on 302 local sessions: 1464 Bash
# writes against 3035 Write/Edit calls; 6 of 22 alembic migrations were
# created with a heredoc. None of those reached an edit rule.
#
# Reading the command line back cannot recover the write (the path lives
# inside the Python program, not the shell), so this reads the DISK instead:
# the pre lane stamps the call, the post lane asks git what changed since and
# feeds each file through the same matcher a Write goes through. One
# mechanism for every shape, including the ones not seen yet. It lands
# AFTER the write, which is the only lane edit rules use anyway (they are
# advise-only by decision; only shell rules gate).

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


def bash_written_files(root, cmd, since):
    """(path, is_new) for each regular file a Bash call left modified or new.

    Scanned: the session's worktree, plus any sibling worktree the command
    names — a `cat > /tmp/wt-x/app/m.py <<EOF` into a scratch worktree is
    the case that motivated this (the file was 30 directories away from
    the session's cwd and in the same repository). Not every worktree: the
    repos this serves carry twenty-odd, and one `git status` each per Bash
    call is a cost nobody asked for.

    `git status` decides what is a candidate (so .gitignore does the
    exclusion — a `.venv` refresh or `node_modules` install is invisible),
    the mtime decides what THIS call touched. Returns [] rather than a
    partial list past BASH_EDIT_MAX_FILES: forty files in one call is a
    generator or a tree rewrite, and forty fires is noise, not advice.
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
            # No slack on the stamp: mtimes are sub-second on APFS/ext4, and a
            # slack would let the PREVIOUS tool call's file count as this one's
            # (the two are often within a second). A coarse filesystem (HFS+,
            # FAT) can miss a write that lands in the stamp's own second —
            # under-count, the safe direction.
            if not stat.S_ISREG(st.st_mode) or st.st_mtime < since:
                continue
            out.append((path, is_new))
            if len(out) > BASH_EDIT_MAX_FILES:
                return []
    return out


def read_edit_body(path, is_new=True):
    """What an edit rule reads for a Bash-written file, matching what it
    reads for the tools: a NEW file is the whole file (a Write), a MODIFIED
    file is the lines this change added (an Edit's new_string) — not the
    file it landed in. Read whole, a one-line comment dropped into
    config.py fired the camelCase rule on every snake_case name already
    there. None when the file is binary (a NUL byte) or past
    BASH_EDIT_MAX_BYTES, or when a modified file's diff cannot be read."""
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
def evaluate(rule, *, hook_phase, tool, cmd="", file_path="", body="", result_text=""):
    """True if `rule` fires on this event. No I/O, no dedup. Ordering rules are not matchers (see
    OrderingEngine)."""
    on = rule.get("on")
    try:
        if hook_phase == "pre" and on == "bash" and tool == "Bash" and cmd:
            # Rules ABOUT payloads (`body_rx`): rx still names the shell shape
            # (`python - <<`), body_rx says what the payload must be about — so
            # a spec file that merely *contains* "python3 - <<" never fires.
            # Legacy `match_heredoc_body` without body_rx matches the whole string.
            shell = shell_only(cmd)
            target = cmd if (rule.get("match_heredoc_body") and not rule.get("body_rx")) else shell
            if not re.search(rule["rx"], target, re.I | re.M):
                return False
            if rule.get("not_rx") and re.search(rule["not_rx"], target, re.I):
                return False
            if rule.get("body_rx"):
                kept = set(shell.split("\n"))
                body_only = "\n".join(l for l in cmd.split("\n") if l not in kept)
                return bool(re.search(rule["body_rx"], body_only, re.I | re.M))
            return True
        if hook_phase == "pre" and on == "edit" and tool in EDIT_TOOLS:
            if re.search(rule["path_rx"], file_path) and not (
                    rule.get("path_not_rx") and re.search(rule["path_not_rx"], file_path)):
                if "content_rx" in rule and not re.search(rule["content_rx"], body, re.M):
                    return False
                # content_not_rx exempts the whole edit — the complied-with
                # form (a suppression that carries its reason, say) must not
                # keep firing once the author has done what the rule asked.
                return not (rule.get("content_not_rx")
                            and re.search(rule["content_not_rx"], body, re.M))
            return False
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
            tail = result_text[-8000:]
            m = re.search(rule["rx"], tail, re.M)
            # exclude_rx exempts the whole result (an exempt test name usually
            # sits outside the matched span), not just the matched substring
            return bool(m) and not (
                rule.get("exclude_rx") and re.search(rule["exclude_rx"], tail, re.M))
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
    LOCK_WAIT_S; past that the hook fails open) and replaces the file atomically.
    An arm and a discharge from two sessions must never overwrite each other —
    those are the two outcomes a gate exists to prevent."""

    def __init__(self, worktree_root, branch):
        os.makedirs(os.path.join(BASE, "state"), exist_ok=True)
        key = hashlib.sha1(worktree_root.encode("utf-8")).hexdigest()[:16]
        self.path = os.path.join(BASE, "state", f"wt-{key}.json")
        self.branch = "*"            # branch is recorded on fires, not used as a key

    def _locked(self):
        lock = open(self.path + ".lock", "a+", encoding="utf-8")
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock
            except OSError:
                if time.monotonic() >= deadline:
                    lock.close()
                    return None
                time.sleep(0.005)

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

    def mark_fired(self, rule_id, fire_id):
        """Remember the open fire in WORKTREE state so a later discharge from
        any session in this checkout converts it."""
        lock = self._locked()
        if lock is None:
            return
        try:
            st = self._read()
            st.setdefault(self.branch, {}).setdefault(
                rule_id, {"count": 0, "last_edit": None})["open_fire"] = fire_id
            self._write(st)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    def feed(self, rule, *, hook_phase, tool, cmd="", file_path="", ok=None):
        """Returns "fired" | "allowed" | "discharged" | None. Mutates state
        under lock; None on lock timeout (fail open)."""
        spec = rule["ordering"]
        armed_by = tuple(spec.get("armed_by_events", ("edit", "write")))
        is_edit = tool in EDIT_TOOLS and hook_phase == "post"
        if is_edit and not any(k in armed_by for k in ("edit", "write")):
            return None
        if is_edit and spec.get("path_rx") and not re.search(spec["path_rx"], file_path):
            return None
        seg = shell_only(cmd) if cmd else ""
        # A Bash call reports ONE exit status. It is the receipt's own status
        # only when the receipt is the final segment and not piped (`pytest |
        # tail` returns tail's status). Earlier segments / pipelines never
        # discharge — under-counting is the safe direction.
        last = last_segment(seg) if seg else ""
        is_receipt = hook_phase == "post" and tool == "Bash" and seg and \
            re.search(spec["required_command_rx"], last) and \
            "|" not in last and not last.rstrip().endswith("&")   # piped / backgrounded: status isn't the suite's
        is_gate = hook_phase == "pre" and tool == "Bash" and seg and \
            re.search(spec["gated_command_rx"], seg)
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
                    # conversion is (worktree, branch)-scoped: a subagent's or
                    # sibling session's receipt converts whichever fire is open
                    rule["_converted_fire"] = s.pop("open_fire", None)
                    self._write(st)
                    return "discharged"
                return None
            # handler 3: the gate — read-only
            if s["count"] >= int(spec.get("min_edits", 1)):
                rule["_gate_msg"] = (
                    f"{s['count']} edit(s) since the last passing "
                    f"'{spec.get('display_name', rule['id'])}' "
                    f"(last: {s['last_edit']}). Run it first.")
                return "fired"
            return "allowed"
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
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
             "dirty": "bool"},
    "user": {"said_rx": "rx", "not_said_rx": "rx"},
}


def given_norm(g):
    """Lint a rule's `given` block off the wire. Returns the block, or None on
    an unknown sub-block, an unknown key, or a value of the wrong kind — and
    None drops the RULE, as rx_ok does. A rule that passed the server's
    allowlist yet fails here must not fire with its predicate silently
    ignored: that is a rule firing when its author said it should not."""
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
    per hook call. Nothing runs unless a rule whose regex already matched
    carries a `given`, each git call is read-only and bounded by
    PROBE_TIMEOUT_S, and nothing here leaves the machine. A probe that fails
    answers None, and None never satisfies a predicate: a rule with a `given`
    it cannot check stays silent, which is the fail-open direction.
    `fixture` pre-answers probes by name — the verifier's and the tests' way
    in, so given_ok() never needs a real repo to be exercised."""

    def __init__(self, root, branch, transcript_path=None, fixture=None, command=""):
        self.root, self._branch, self.tp = root, branch, transcript_path
        self._fix = dict(fixture or {})
        self._memo = {}
        self._cmd = command or ""

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
        import subprocess
        p = subprocess.run(["git", "-C", self.root, *args], capture_output=True,
                           text=True, timeout=PROBE_TIMEOUT_S)
        return p.stdout if p.returncode == 0 else None

    def branch(self):
        return self._get("branch", lambda: self._branch)

    def _named_base(self):
        """The base branch the in-flight command names, e.g. `--base staging`.
        Read off the command rather than guessed, because the command is the
        only place the answer actually exists."""
        m = _BASE_ARG.search(self._cmd)
        return next((g for g in m.groups() if g), "") if m else ""

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

        Guessing `main` first was wrong wherever a repo merges into something
        else: against `origin/main`, a PR onto a long-lived `staging` measures
        the whole staging-vs-main delta instead of the branch, so a
        `diff_lines_gt` rule fires on every PR in that repo no matter how small.
        """
        def compute():
            env = os.environ.get("MEMHUB_RULEBOOK_BASE_BRANCH", "").strip()
            named = self._named_base()
            cands = ([env] if env else [])
            # `--base staging` names a branch, not a ref: try the remote's copy
            # before the local one, which may be stale or absent.
            cands += [f"origin/{named}", named] if named else []
            cands += [r for r in [self._remote_head()] if r]
            cands += ["origin/main", "origin/master", "origin/develop",
                      "main", "master", "develop"]
            for cand in cands:
                if self._git("rev-parse", "--verify", "-q", cand + "^{commit}") is None:
                    continue
                mb = self._git("merge-base", cand, "HEAD")
                return mb.strip() if mb and mb.strip() else None
            return None
        return self._get("base", compute)

    def diff_paths(self):
        """Paths the branch has changed against its base, working tree
        included — committed, staged, unstaged, and untracked files (a new
        test file is usually untracked when the rule asks about it)."""
        def compute():
            mb = self.base()
            if mb is None:
                return None
            tracked = self._git("diff", "--name-only", mb)
            untracked = self._git("ls-files", "--others", "--exclude-standard")
            if tracked is None or untracked is None:
                return None
            return sorted({l.strip() for l in (tracked + "\n" + untracked).split("\n") if l.strip()})
        return self._get("diff_paths", compute)

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


def given_ok(rule, probes):
    """True when every predicate in the rule's `given` holds. Pure over the
    Probes (which memoizes); a probe answering None fails its predicate."""
    g = rule.get("given")
    if not g:
        return True
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
def book_path(repo):
    """Readable name + a hash of the RAW name, so two repos that sanitise to
    the same string ('my repo' / 'my_repo') never share a book."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", repo)[:60] or "norepo"
    h = hashlib.sha1(repo.encode("utf-8")).hexdigest()[:8]
    return os.path.join(BOOK_DIR, f"{safe}-{h}.json")


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
}
_RESULT_KEYS = dict(_MATCHER_KEYS, command_rx="cmd_rx", command_not_rx="cmd_not_rx",
                    content_rx="rx", content_not_rx="exclude_rx")
_SCOPE_MAP = {"turn": "call", "file": "session", "session": "session"}   # warn_once_per → fire_scope
_RESERVED_RULE_KEYS = frozenset({"id", "text", "why", "status", "mode", "_version", "_label",
                                 "on", "repo_scope", "_scope_repos", "_scope_paths",
                                 "_scope_exclude_paths", "anchors", "ordering",
                                 "_rulebook_id", "_book_name", "_book_scope", "_book_members"})


_RX_KEYS = ("rx", "not_rx", "body_rx", "cmd_rx", "cmd_not_rx", "path_rx", "path_not_rx",
            "content_rx", "content_not_rx", "exclude_rx", "converted_rx")
_RX_MAX = 400
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


def _why(r):
    """The parenthetical reason — only when the rule carries one separately;
    server statements already end in 'Why: …'."""
    return f"  _(why: {r['why']})_" if r.get("why") else ""


_BOOK_SCOPES = ("all_org", "explicit")
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
_BOOK_KEYS = ("_rulebook_id", "_book_name", "_book_scope", "_book_members")


def _book_facts(row):
    """The precedence facts the server puts on the wire (container spec §6.4a):
    which rulebook a rule came from, how wide that book's membership is, and
    what the book is called. The server computes NO precedence and stores no
    conflict edges — it ships `scope` and `member_count` and the hook decides
    what "wider" means (D14).

    A backend that predates the rulebook container sends neither key. Every
    rule then carries the same absent facts, `book_rank` returns one value for
    all of them, and the stable sorts below leave book order exactly as it is
    today — which is what makes one plugin build work against both backends."""
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
    mc = b.get("member_count")
    # Bounded, because it is rendered: a four-thousand-digit member_count is
    # valid JSON and would spend the session-start budget on digits alone.
    if isinstance(mc, int) and not isinstance(mc, bool) and 0 <= mc <= _BOOK_MEMBERS_MAX:
        out["_book_members"] = mc
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


def to_hook_rule(row):
    """One `?view=hook` row → the flat shape evaluate()/OrderingEngine read.
    Rows already in the pilot shape (an `on` key) pass through. The book facts
    (`_rulebook_id`, `_book_name`, `_book_scope`, `_book_members`) ride along
    on both paths; they are absent, harmlessly, on a pre-container backend.
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
            if isinstance(r.get("ordering"), dict) and not all(
                    rx_ok(r["ordering"].get(k)) for k in ("required_command_rx", "gated_command_rx")):
                return None
            if "given" in r:
                r["given"] = given_norm(r["given"])
                if r["given"] is None:
                    return None
            return r
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
            return r
        if isinstance(row.get("anchors"), list) and row["anchors"]:
            anchors = [_clean_text(a) for a in row["anchors"] if isinstance(a, str) and a.strip()]
            if not anchors:
                return None
            r["on"] = "anchor"
            r["anchors"] = anchors[:64]
            r["fire_scope"] = "session"
            return r
        if isinstance(row.get("ordering"), dict):
            o = row["ordering"]
            if not all(rx_ok(o.get(k)) for k in ("required_command_rx", "gated_command_rx")):
                return None
            r["on"] = "ordering"
            r["ordering"] = o
            return r
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
        for k, v in m.items():
            if k == "event":
                continue
            if k == "result_rx" and "content_rx" in m:
                continue              # content_rx is the schema key; result_rx is a legacy alias
            dest = keys.get(k, k)
            if dest in _RESERVED_RULE_KEYS:   # a matcher key can never overwrite the row's own fields
                continue
            r[dest] = v
        r["fire_scope"] = _SCOPE_MAP.get(str(r.get("fire_scope", "session")), r.get("fire_scope"))
        if not all(rx_ok(r[k]) for k in _RX_KEYS if k in r):
            return None
        if "given" in r:
            r["given"] = given_norm(r["given"])
            if r["given"] is None:      # unknown key or wrong kind: drop the RULE, as rx_ok does
                return None
        return r
    except Exception:
        return None


def load_rules(repo):
    """The cached server book as hook rules. Returns (rules, "", fetched_at,
    sources) — sources maps rule id → "server" (kept for the audit file)."""
    book = load_book(repo)
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
    the cache is an hour old, refresh it in the background: the child is
    detached, so the lane never waits, and the stamp file keeps a dead server
    from being probed more than once every ten minutes. No cache at all
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
        # ten minutes before the next try costs at most one override on a rule
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
        return any(s == repo or (main and s == main) for s in rule["_scope_repos"])
    if scope == "any":
        return True
    return scope in repo or (gitdir and f"/{scope}/" in gitdir)


# ── server: fetch + flush (lazy imports — the pre/post lanes never pay for them) ──
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


def fetch_book(repo):
    """GET /rules?repo=<repo>&view=hook with If-None-Match.

    No `status=` param: `view=hook` serves ACTIVE rules on its own, and the
    server's filter grammar changed under us once already (a bare
    `status=active` became a 400), taking every book fetch down silently.
    Not sending the parameter is the one form no grammar change can break.
    200 → rewrite the cache; 304 → touch fetched_at (the book is confirmed
    current, which is what §5.3 gate freshness measures); anything else →
    the cache is left exactly as it was."""
    api = _api()
    if not api:
        return
    base, bearer, http = api
    old = load_book(repo) or {}
    hdrs = {"If-None-Match": old["etag"]} if old.get("etag") else {}
    q = "view=hook&repo=" + urllib.parse.quote(repo, safe="")
    try:
        reply = http.rest(f"{base}{API_PATH}/rules?{q}", bearer, "GET", headers=hdrs,
                          timeout=FETCH_TIMEOUT_S)
    except Exception as exc:          # keep the cache; say so where an operator can look
        _breadcrumb("fetch", exc)
        return
    if reply.status == 304 and old:
        _atomic_json(book_path(repo), dict(old, fetched_at=_now()))
    elif reply.status == 200 and isinstance(reply.data, dict) \
            and isinstance(reply.data.get("rules"), list):
        _atomic_json(book_path(repo), {"etag": reply.etag, "fetched_at": _now(),
                                       "rules": reply.data["rules"]})
    else:                             # a 2xx with the wrong shape is a failure too — say so
        _breadcrumb("fetch", f"HTTP {reply.status}: unexpected reply shape")


# ── what leaves the machine on the recall path ─────────────────────────────
#
# `/recall` is the one lane that sends content rather than identifiers: the
# server's relevance judge decides whether an anchor rule applies to THIS call,
# and it cannot do that from a rule id. So the command line goes with it.
#
# A command line is also where credentials live — `curl -H "Authorization:
# Bearer …"`, `psql postgres://user:pw@host`, `--token=…`. Those are worth
# nothing to the judge and must not reach a model, so they are replaced before
# the POST. `shell_only` has already dropped heredoc bodies by this point, so
# what remains is the shell line itself.
#
# This is a denylist and cannot be complete — the docstring and the README say
# so, and `MEMHUB_RULEBOOK_RECALL=0` turns the lane off entirely for anyone who
# would rather not send command text at all. It is a floor, not a guarantee.
_REDACTIONS = (
    # `--token=x`, `--password x`, `API_KEY=x` — the value, not the flag, so the
    # judge still sees that a credential was passed.
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
                r"access[_-]?key|credential))(\s*[=:]\s*|\s+)([^\s\"']+)"),
     r"\1\2<redacted>"),
    # `curl -u user:password`, `-U user:password`.
    (re.compile(r"(?i)(\s-{1,2}(?:u|user)[=\s]+)([^\s:\"']+):([^\s\"']+)"), r"\1\2:<redacted>"),
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


RECALL_TIMEOUT_S = _timeout(1.5)   # inside the PreToolUse hook budget; fail open past it


def recall_anchor_rules(repo, tool, handles, already_fired):
    """POST /recall — the server runs the book's anchor rules through xmem's
    directive funnel (identifier extraction → exact anchor match → the SLM
    relevance judge). Returns the kept rule ids, or [] on ANY failure: an
    anchor being present is not relevance, and a judge outage is never a
    reason to block or slow the call."""
    try:
        api = _api()
        if not api:
            return []
        base, bearer, http = api
        body = {"tool": tool, "args": handles, "repo": repo,
                "already_fired": list(already_fired)[:200], "limit": MAX_ADVISE}
        reply = http.rest(f"{base}{API_PATH}/recall", bearer, "POST", body=body,
                          timeout=RECALL_TIMEOUT_S)
        if reply.status != 200 or not isinstance(reply.data, dict):
            return []
        return [str(r.get("rule_id")) for r in reply.data.get("rules") or []
                if isinstance(r, dict) and r.get("rule_id")]
    except Exception as exc:
        _breadcrumb("recall", exc)
        return []


def spawn_fetch(repo):
    """Refresh the book in a DETACHED child so SessionStart returns at once."""
    import subprocess
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "fetch", repo],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


WIRE_KEYS = ("fire_id", "rule_id", "rule_version", "session_id", "agent_id", "repo",
             "branch", "tool", "hook_phase", "mode", "dedup_key",
             "raw_matches_before_fire", "fired_at", "converted", "converted_at",
             "source_message_id", "override_reason")


def wire_row(row):
    """The v2 ledger row minus `excerpt` (Phase 1: always stripped — the org
    opt-in for excerpts is a server setting the hook does not consult)."""
    return {k: row.get(k) for k in WIRE_KEYS}


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
        return {"fires_offset": 0, "conversions_offset": 0, "last_flush_at": None}


try:
    CONVERSION_HOLD_S = int(os.environ.get("MEMHUB_RULEBOOK_CONVERSION_HOLD_S", 6 * 3600))
except ValueError:
    CONVERSION_HOLD_S = 6 * 3600


def _older_than(iso, seconds):
    """True when `iso` (ledger timestamp) is more than `seconds` in the past;
    an unparseable stamp counts as old so it can never hold the watermark."""
    try:
        ts = _dt.datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
    except Exception:
        return True
    return (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds() > seconds


def pending_batches(sent):
    """Rows to POST = fires past the watermark ∪ fires named by conversions past
    THEIR watermark (each re-sent with converted/converted_at merged — the
    ingest is an upsert on fire_id, so a re-send is an update, never a dup).
    Returns (batches, new_sent): each batch is (rows, sent_after_it) so a
    multi-batch flush advances the watermark per accepted batch and a poison
    batch never makes earlier ones re-send forever. The same fire_id is
    reused on every retry: rows come from the ledger, nothing is minted here.
    Reads past the watermark first (a seek, cheap on every Stop) and only
    indexes the whole ledger when there is something to send."""
    ldir = _ledger_dir()
    fpath, cpath = os.path.join(ldir, "fires.jsonl"), os.path.join(ldir, "conversions.jsonl")
    f_offsets = []
    new_fires, f_end = _read_rows(fpath, sent.get("fires_offset", 0), f_offsets)
    c_offsets = []
    new_convs, c_end = _read_rows(cpath, sent.get("conversions_offset", 0), c_offsets)
    if not new_fires and not new_convs:
        return [], dict(sent, fires_offset=f_end, conversions_offset=c_end)
    # New fires carry their own rows. A NEW conversion may name a fire behind
    # the watermark; only THOSE ids are looked up, streaming the ledger without
    # holding it (bounded by the number of new conversions, not by history).
    by_id = {r["fire_id"]: r for r in new_fires if isinstance(r, dict) and r.get("fire_id")}
    wanted = {c.get("fire_id") for c in new_convs if isinstance(c, dict)} - set(by_id)
    if wanted:
        try:
            with open(fpath, "rb") as f:
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
    # A conversion whose fire is not in the ledger yet (the fire line is still
    # being written, or a rotated ledger) must NOT be passed by the watermark:
    # stop the conversions offset just before the first unresolved one so the
    # next flush sees it again once the fire has landed.
    # The hold is bounded: a conversion older than CONVERSION_HOLD_S whose
    # fire never landed (corrupt or rotated fire line) is dropped so it can
    # never stall the conversions behind it.
    c_start = sent.get("conversions_offset", 0)
    for i, c in enumerate(new_convs):
        if isinstance(c, dict) and c.get("fire_id") and c["fire_id"] not in by_id \
                and not _older_than(c.get("converted_at"), CONVERSION_HOLD_S):
            c_end = c_offsets[i - 1] if i else c_start
            new_convs = new_convs[:i]
            break
    new_sent = dict(sent, fires_offset=f_end, conversions_offset=c_end)
    # Only conversions past THEIR watermark need merging: the two offsets
    # advance together, so an older conversion was shipped with its fire.
    for c in new_convs:
        if isinstance(c, dict) and c.get("fire_id") in by_id and c.get("converted"):
            by_id[c["fire_id"]]["converted"] = True
            by_id[c["fire_id"]]["converted_at"] = c.get("converted_at")
    # (row, fires_offset once this row is accepted); conversion re-sends carry
    # no fires progress of their own, so they inherit the last fire's offset.
    items, seen = [], set()
    for r, off in zip(new_fires, f_offsets):
        if isinstance(r, dict) and r.get("fire_id") and r["fire_id"] not in seen:
            items.append((wire_row(by_id.get(r["fire_id"], r)), off))
            seen.add(r["fire_id"])
    for c in new_convs:
        fid = c.get("fire_id") if isinstance(c, dict) else None
        if fid in by_id and fid not in seen:
            items.append((wire_row(by_id[fid]), None))
            seen.add(fid)
    batches = []
    fo = sent.get("fires_offset", 0) if f_offsets or new_fires else f_end
    # conversions are credited once the last batch that carries ANY converted
    # row (a re-send, or a new fire whose conversion was merged in) is
    # accepted — a later failed batch must still re-merge its conversions
    conv_ids = {c.get("fire_id") for c in new_convs if isinstance(c, dict)}
    last_conv = max([-1] + [i for i, (r, o) in enumerate(items)
                            if o is None or r.get("fire_id") in conv_ids])
    for i in range(0, len(items), FLUSH_BATCH):
        chunk = items[i:i + FLUSH_BATCH]
        fo = max([fo] + [o for _, o in chunk if o is not None])
        last = i + FLUSH_BATCH >= len(items)
        convs_done = last or i + FLUSH_BATCH > last_conv
        batches.append(([r for r, _ in chunk],
                        dict(sent, fires_offset=f_end if last else fo,
                             conversions_offset=c_end if convs_done else c_start)))
    return batches, new_sent


def _log_rejected(rejected, batch):
    """Per-row rejections are logged as given; a bare count (the §4.3 example
    shape) is logged with the batch's fire_ids so the loss is visible even
    though the server did not say which rows."""
    try:
        if isinstance(rejected, list):
            items = [{"rejected": it} for it in rejected]
        elif isinstance(rejected, int) and rejected > 0:
            items = [{"rejected_count": rejected,
                      "batch_fire_ids": [r.get("fire_id") for r in batch]}]
        else:
            items = []
        if items:
            with open(os.path.join(_ledger_dir(), "rejected.jsonl"), "a", encoding="utf-8") as f:
                for it in items:
                    f.write(json.dumps(dict(it, at=_now())) + "\n")
    except Exception:
        pass


def flush_fires(final=False):
    """POST unsent rows in batches. The watermark advances ONLY on a 2xx, so
    a failed batch is retried, verbatim, on the next flush; `rejected` rows
    are logged locally and never retried (they sit behind the watermark).
    One flusher at a time via flock; a second caller simply leaves."""
    ldir = _ledger_dir()
    lock = open(os.path.join(ldir, ".flush.lock"), "a+", encoding="utf-8")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return
    try:
        sent = load_sent()
        batches, new_sent = pending_batches(sent)
        n = sum(len(b) for b, _ in batches)
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
        accepted = 0
        for batch, after in batches:
            try:
                reply = http.rest(f"{base}{API_PATH}/fires", bearer, "POST",
                                  body={"fires": batch}, timeout=FLUSH_TIMEOUT_S)
            except Exception as exc:      # transport/envelope error: retry next flush,
                _breadcrumb("flush", exc)  # but say so where an operator can look
                return
            if reply.status not in (200, 201, 202):
                return                    # watermark stays at the last accepted batch
            data = reply.data if isinstance(reply.data, dict) else {}
            if not isinstance(data.get("accepted"), int):
                return                    # not the §4.3 reply → do not trust it as a receipt
            rej = data.get("rejected")
            n_rej = len(rej) if isinstance(rej, list) else (rej if isinstance(rej, int) else 0)
            if data["accepted"] + n_rej < len(batch):
                # Short-counted: retry — but not forever. The same batch (same
                # first fire_id) short-counting STALL_QUARANTINE_AFTER times in
                # a row is a poison batch: log it as rejected and move past it,
                # so one bad row can never strand every fire behind it.
                key = batch[0].get("fire_id")
                cur = load_sent()             # the on-disk state, including any
                stall = cur.get("stall") or {}  # progress written by earlier batches
                n = (stall.get("n", 0) + 1) if stall.get("key") == key else 1
                if n < STALL_QUARANTINE_AFTER:
                    cur["stall"] = {"key": key, "n": n}
                    _atomic_json(_sent_path(), cur)
                    return
                _log_rejected([{"fire_id": r.get("fire_id"), "reason": "quarantined: short-counted "
                                f"{n}x (accepted {data['accepted']}, rejected {n_rej} of {len(batch)})"}
                               for r in batch], batch)
            else:
                _log_rejected(rej, batch)
            accepted += data["accepted"]
            if (sent.get("stall") or {}).get("key") != batch[0].get("fire_id"):
                after["stall"] = sent.get("stall")   # an accepted batch clears only ITS OWN marker
            else:
                after.pop("stall", None)
            if after.get("stall") is None:
                after.pop("stall", None)
            after["last_flush_at"] = _now()
            after["last_accepted"] = accepted
            _atomic_json(_sent_path(), after)   # per batch: a later failure keeps this progress
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def repo_info(cwd):
    """(repo_basename, worktree_root, gitdir_path, branch) via file reads only."""
    d = cwd
    while d and d != "/":
        g = os.path.join(d, ".git")
        if os.path.isdir(g):
            return os.path.basename(d), d, g, _branch(os.path.join(g, "HEAD"))
        if os.path.isfile(g):   # worktree: "gitdir: /path/to/main/.git/worktrees/x"
            try:
                gitdir = open(g, encoding="utf-8").read().split(":", 1)[1].strip()
            except Exception:
                gitdir = ""
            return os.path.basename(d), d, gitdir, _branch(os.path.join(gitdir, "HEAD"))
        d = os.path.dirname(d)
    return "", "", "", ""


def _branch(head_path):
    try:
        h = open(head_path, encoding="utf-8").read().strip()
        return h.rsplit("/", 1)[-1] if h.startswith("ref:") else "detached"
    except Exception:
        return ""


def state_path(session_id):
    sdir = os.path.join(BASE, "state")
    os.makedirs(sdir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or ""))[:80] or "nosession"
    return os.path.join(sdir, f"{safe}.json")


def load_state(p):
    st = {"fired": [], "counts": {}, "raw": {}, "open": {}}
    try:
        with open(p, encoding="utf-8") as f:
            st.update(json.load(f))
    except Exception:
        pass
    return st


def save_state(p, st):
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


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


def find_override(cmd):
    """(reason, line, raw_token) for the first `RULEBOOK_OVERRIDE=<why>` that
    begins a shell segment and is non-empty, else None. Tokenised per line of
    the shell-only text with shlex (POSIX quoting, operators as their own
    tokens); a line shlex cannot parse contributes nothing. Every candidate is
    tried, so an earlier empty or quoted one cannot shadow the real override."""
    text = re.sub(r"\\\n", " ", shell_only(cmd))        # join continuation lines
    for line in text.split("\n"):
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            toks = list(lex)
        except ValueError:                             # unbalanced quoting
            continue
        at_start = True
        for tok in toks:
            if at_start and tok.startswith(_OVERRIDE_PREFIX):
                reason = tok[len(_OVERRIDE_PREFIX):].strip()
                if reason:
                    return reason, line, _raw_token(line, reason)
            at_start = _segment_op(tok)
    return None


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
    d = os.path.join(BASE, "ledger")
    os.makedirs(d, exist_ok=True)
    sv = os.path.join(d, "schema_version")
    if not os.path.exists(sv):
        with open(sv, "w", encoding="utf-8") as f:
            f.write(f"{LEDGER_SCHEMA}\n")
    return d


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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
    """Subagent transcripts live at <session>/subagents/agent-<id>.jsonl;
    the main agent's do not. NULL = main agent."""
    tp = str(data.get("transcript_path") or "")
    if "/subagents/" in tp:
        return os.path.basename(tp).rsplit(".", 1)[0]
    return None


def log_fires(ctx, rules, *, hook_phase, mode, excerpt, raw_counts=None, dedup_keys=None,
              override_reason=None):
    """One ledger row per (rule, fire) — spec §3.2. Identifiers, not payloads:
    `excerpt` stays in this LOCAL file and never crosses the wire without
    org opt-in. `override_reason` is set on a `mode='gate'` fire the caller
    overrode (§5.3). `rulebook_id` is local too — POST /fires carries no book
    dimension (container spec §6.4), so it is absent from WIRE_KEYS on purpose;
    it is here so a local reader can tell which book a fire came from.
    Returns {rule_id: fire_id} so conversions can point back."""
    ids = {}
    try:
        path = os.path.join(_ledger_dir(), "fires.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for r in rules:
                fid = str(uuid.uuid4())
                ids[r["id"]] = fid
                f.write(json.dumps({
                    "fire_id": fid, "rule_id": r["id"],
                    "rulebook_id": r.get("_rulebook_id"),
                    "rule_version": ctx["rule_version"] if r.get("_version") is None else r["_version"],
                    "session_id": ctx["session"], "agent_id": ctx["agent_id"],
                    "source_message_id": ctx.get("source_message_id"),
                    "repo": ctx["repo"], "branch": ctx["branch"], "tool": ctx["tool"],
                    "hook_phase": hook_phase, "mode": mode,
                    "dedup_key": (dedup_keys or {}).get(r["id"]),
                    "raw_matches_before_fire": (raw_counts or {}).get(r["id"]),
                    "fired_at": _now(),
                    "converted": None, "converted_at": None,
                    "override_reason": override_reason,
                    "excerpt": excerpt[:160],
                }) + "\n")
    except Exception:
        pass
    return ids


def log_conversion(fire_id, how):
    """Append-only sidecar (the fires file is shared across sessions, so it
    is never rewritten in place). A reader merges by fire_id."""
    try:
        with open(os.path.join(_ledger_dir(), "conversions.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps({"fire_id": fire_id, "converted": True,
                                "converted_at": _now(), "how": how}) + "\n")
    except Exception:
        pass


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
            books[rid] = {"name": r.get("_book_name"), "n": 0, "rank": book_rank(r),
                          "scope": r.get("_book_scope"), "members": r.get("_book_members")}
            order.append(rid)
        books[rid]["n"] += 1
    if len(books) < 2:
        return None
    parts = []
    for rid in sorted(order, key=lambda i: (books[i]["rank"], (books[i]["name"] or "").casefold())):
        b = books[rid]
        if b["scope"] == "all_org":
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
    # a cap each of them believes it is under. The wider book spends first
    # (§11), then title, then id: deterministic rather than book order, and
    # every rule past either limit is logged SUPPRESSED so the ledger sees it.
    posture_all = sorted((r for r in in_scope if r.get("on") == "session"),
                         key=lambda r: (book_rank(r),
                                        str(r.get("_label") or r.get("title") or r["id"]).casefold(),
                                        str(r["id"])))
    posture, cut, used = [], [], 0
    for r in posture_all:
        cost = len(r.get("text") or "") + len(r.get("why") or "")
        if len(posture) < MAX_POSTURE and used + cost <= POSTURE_BUDGET_CHARS:
            posture.append(r); used += cost
        else:
            cut.append(r)
    active = [r for r in in_scope if r.get("on") != "session"]
    lines = [f"## {BRAND} Rulebook (team rules — advisory)"]
    for r in posture:
        lines.append(f"- {r['text']}{_why(r)}")
    if active:
        lines.append(
            f"- {len(active)} rule{'s' if len(active) != 1 else ''} armed for "
            f"this repo — they fire inline as you work (proactive on tool "
            f"calls, reactive on errors). Treat a fire as a teammate's note, "
            f"not boilerplate.")
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


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pre"
    if mode == "fetch" and len(sys.argv) > 2:      # detached child: repo on argv
        fetch_book(sys.argv[2])
        return 0
    if mode == "flush":                # needs nothing from the event payload
        try:
            sys.stdin.read()
        except Exception:
            pass
        flush_fires(final="final" in sys.argv[2:])
        return 0
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    cwd = data.get("cwd") or os.getcwd()
    session = data.get("session_id", "")
    repo, root, gitdir, branch = repo_info(cwd)
    if not repo:            # not in a git repo → no rules apply
        return 0
    if mode == "fetch":
        fetch_book(repo)
        return 0
    rules, rule_version, fetched_at, sources = load_rules(repo)
    tool = data.get("tool_name", "")
    ctx = {"session": session, "agent_id": agent_id_of(data), "repo": repo,
           "branch": branch, "tool": tool, "rule_version": rule_version,
           "source_message_id": message_id_of(data)}
    # Repo facts answer about the tree the COMMAND runs in; which rules bind
    # you is still the session's repo, and stays keyed on it.
    cmd_text = (data.get("tool_input") or {}).get("command") or ""
    probe_root, probe_branch = root, branch
    elsewhere = command_root(cwd, cmd_text)
    if elsewhere and elsewhere != root:
        probe_root = elsewhere
        probe_branch = _branch(os.path.join(repo_info(elsewhere)[2], "HEAD"))
    probes = Probes(probe_root, probe_branch, command=cmd_text,
                    transcript_path=data.get("transcript_path"))

    if mode == "session":
        try:        # which source each rule came from — the pilot's merge audit
            _atomic_json(book_path(repo) + ".sources", {"at": _now(), "sources": sources})
        except Exception:
            pass
        session_digest(rules, repo, gitdir, ctx)
        if os.environ.get("MEMHUB_RULEBOOK_FETCH", "1") != "0":
            try:
                spawn_fetch(repo)
            except Exception:
                pass
        return 0
    if mode == "pre":
        maybe_refresh(repo, fetched_at)

    inp = data.get("tool_input") or {}
    sp = state_path(session)
    st = load_state(sp)
    fired_now = []

    cmd = str(inp.get("command", "")) if tool == "Bash" else ""
    override_reason = None            # §5.3: set only by the RULEBOOK_OVERRIDE prefix
    if cmd:
        found = find_override(cmd)    # an empty or quoted one is no override — the gate stands
        if found:
            override_reason = redact_secrets(found[0])[:2000]
            cmd = strip_override(cmd, found)   # rules match the command, not the assignment
    fp = str(inp.get("file_path", ""))
    body = str(inp.get("new_string", "")) + str(inp.get("content", "")) + \
        "\n".join(str(e.get("new_string", "")) for e in (inp.get("edits") or []) if isinstance(e, dict))
    rtext = result_text(data.get("tool_response")) if mode == "post" else ""
    resp = data.get("tool_response") if (mode == "post" and tool == "Bash") else None
    ordering = None
    dedup_keys = {}
    by_id = {r["id"]: r for r in rules}

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
            "body": body, "rtext": rtext, "resp": resp, "via": None}
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
    events.append(real)

    # Conversions: did this call perform the action an earlier fire asked for?
    # Deterministic, under-counts, never over-counts (spec §5.1).
    for rid, fid in list(st["open"].items()):
        r = by_id.get(rid)
        if not r:
            continue
        crx = r.get("converted_rx")
        if mode == "post" and tool == "Bash" and crx and cmd \
                and re.search(crx, shell_only(cmd), re.I | re.M):
            log_conversion(fid, "converted_rx")
            del st["open"][rid]
            st.get("open_file", {}).pop(rid, None)
            continue
        if r.get("on") != "edit" or "content_rx" not in r:
            continue
        for ev in events:
            if ev["phase"] == "pre" and ev["tool"] in EDIT_TOOLS \
                    and ev["fp"] == st.get("open_file", {}).get(rid) \
                    and not evaluate(r, hook_phase="pre", tool=ev["tool"], file_path=ev["fp"],
                                     body=ev["body"]):
                log_conversion(fid, "re-edit-clears")
                del st["open"][rid]
                st.get("open_file", {}).pop(rid, None)
                break

    # Anchor rules (§4.7): one server call per tool call, only when the book has
    # an active anchor rule in scope and the call carries a handle. The server
    # matches anchors AND judges relevance; the hook just injects what it kept.
    anchor_rules = {r["id"]: r for r in rules if r.get("on") == "anchor"
                    and r.get("status", "active") == "active" and scope_ok(r, repo, gitdir)
                    and r["id"] not in st["fired"]}
    handles = {}
    if tool == "Bash" and cmd:
        handles["command"] = redact_secrets(shell_only(cmd))[:400]
    elif tool in EDIT_TOOLS and fp:
        handles["file_path"] = fp
    if mode == "pre" and anchor_rules and handles \
            and os.environ.get("MEMHUB_RULEBOOK_RECALL", "1") != "0":
        for rid in recall_anchor_rules(repo, tool, handles, st["fired"]):
            r = anchor_rules.get(rid)
            if r is not None:
                st["fired"].append(rid)
                dedup_keys[rid] = rid
                fired_now.append(r)

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
                try:
                    ordering = ordering or OrderingEngine(root, branch)
                    ok = bash_ok(ev["resp"], strict=r.get("mode") == "gate") \
                        if ev["resp"] is not None else None
                    outcome = ordering.feed(r, hook_phase=ev["order_phase"], tool=etool, cmd=ecmd,
                                            file_path=efp, ok=ok)
                except Exception:
                    outcome = None
                if outcome == "discharged" and r.get("_converted_fire"):
                    log_conversion(r["_converted_fire"], "discharged")
                elif outcome == "fired":
                    dedup_keys[rid] = f"{rid}@{root}:{branch}"
                    fired_now.append(r)
                    fired_on[rid] = ev
                continue

            if not path_in_scope(r, efp if etool in EDIT_TOOLS else "", root):
                continue
            scope = r.get("fire_scope", "session")
            if ephase == "pre" and etool == "Bash" and r.get("on") == "bash" \
                    and r.get("mode") == "gate":
                scope = "call"        # a gate blocks EVERY matching call — never deduped (§5.3)
            key = rid if not scope.startswith("branch") else f"{rid}:{branch}"
            # the regex first (pure, cheap), the given second (probes run only now)
            matched = evaluate(r, hook_phase=ephase, tool=etool, cmd=ecmd, file_path=efp,
                               body=ebody, result_text=ev["rtext"]) and given_ok(r, probes)
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
            fired_now.append(r)
            fired_on[rid] = ev

    if not fired_now:
        save_state(sp, st)
        return 0

    # §5.3: which of this call's fires are GATES. Only a pre-hook Bash call can
    # be blocked, and only by a rule the cached book says is a gate.
    gate_ids = {r["id"] for r in fired_now
                if mode == "pre" and tool == "Bash" and r.get("on") in ("bash", "ordering")
                and r.get("mode") == "gate"}
    gates = [r for r in fired_now if r["id"] in gate_ids]
    # §11: precedence between books is the hook's, and it is an ORDERING —
    # the wider book's rule is what MAX_ADVISE keeps when two books both fire
    # on one call. Stable, so one book's rules keep their order and a backend
    # with no book facts ranks every rule alike and is unaffected.
    #
    # An anchor rule outranks book width, and is not an exception to "wider
    # wins" so much as a different question. A matcher rule fired because a
    # regex matched; an anchor rule fired because the server spent a round trip
    # and its relevance judge said THIS call. Letting two org-wide regexes
    # displace it throws that judgment away — and the rule is already marked
    # spent for the session by then, so it is not offered again.
    advisories = sorted((r for r in fired_now if r["id"] not in gate_ids),
                        key=lambda r: (0 if r.get("on") == "anchor" else 1, book_rank(r)))
    # the advisory cap never cuts a gate — a silently un-gated push is the one
    # failure a gate exists to prevent
    shown, cut = gates + advisories[:MAX_ADVISE], advisories[MAX_ADVISE:]
    blocked = bool(gates) and override_reason is None
    lines = [f"## {BRAND} Rulebook — BLOCKED" if blocked
             else f"## {BRAND} Rulebook (team rules — advisory, not blocking)"]
    user_lines, deny_lines = [], []

    def _where(r):
        """A fire from a file the Bash call wrote names the file: the model
        knows which file a Write was, but a heredoc's target is buried in
        the command it just ran."""
        ev = fired_on.get(r["id"])
        if not ev or ev.get("via") != "bash":
            return ""
        path = ev["fp"]
        if root and path.startswith(root.rstrip("/") + "/"):
            path = os.path.relpath(path, root)
        return f" _(in `{path}`, written by that command)_"

    for r in shown:
        label = r.get("_label") or r["id"]
        detail = f" — {r['_gate_msg']}" if r.get("_gate_msg") else ""
        if r["id"] not in gate_ids:
            lines.append(f"- **[{label}]** {r['text']}{detail}{_where(r)}{_why(r)}")
            user_lines.append(f"{BRAND} ▸ [{label}] {r['text']}{detail}{_where(r)}")
        elif override_reason is not None:
            lines.append(f"- **[{label}]** {r['text']}{detail}{_why(r)} "
                         f"_(gate overridden: {override_reason})_")
            user_lines.append(f"{BRAND} ⚠ gate overridden — [{label}] {override_reason}")
        else:
            lines.append(f"- **BLOCKED [{label}]** {r['text']}{detail}{_why(r)}")
            user_lines.append(f"{BRAND} ⛔ blocked by [{label}] {r['text']}{detail}")
            deny_lines.append(f"[{label}] {r['text']}{detail}")
    deny = None
    if blocked:
        deny = (f"Blocked by the {BRAND} team rulebook:\n" + "\n".join(f"- {l}" for l in deny_lines)
                + "\nIf this is a legitimate exception, re-run the same command prefixed "
                  "RULEBOOK_OVERRIDE='<why>' — that allows exactly that call and records why.")
        lines.append("_This call was blocked. If it is a legitimate exception, re-run the "
                     "same command prefixed `RULEBOOK_OVERRIDE='<why>'`._")
    try:
        emit("PreToolUse" if mode == "pre" else "PostToolUse", "\n".join(lines),
             user_line="\n".join(user_lines), deny=deny)
    except Exception:
        pass
    raw = {r["id"]: st["raw"].get(r["id"]) for r in fired_now}

    def _excerpt(r):
        """Local-only (never on the wire). A fire from a Bash-written file
        records the file, prefixed so a reader can count how many edits
        arrive through Bash versus the Write tool."""
        ev = fired_on.get(r["id"])
        if ev and ev.get("via") == "bash":
            return f"bash-edit {ev['fp']}"
        return cmd or fp or ""

    ids = {}
    for r in (r for r in shown if r["id"] not in gate_ids):
        ids.update(log_fires(ctx, [r], hook_phase=mode, mode="advise", excerpt=_excerpt(r),
                             raw_counts=raw, dedup_keys=dedup_keys))
    if gates:      # a blocked call and an overridden one are both delivered gate fires
        ids.update(log_fires(ctx, gates, hook_phase=mode, mode="gate", excerpt=cmd or fp or "",
                             raw_counts=raw, dedup_keys=dedup_keys,
                             override_reason=override_reason))
    for r in cut:   # the per-call cap has a cost; make it visible, never silent
        log_fires(ctx, [r], hook_phase=mode, mode="suppressed", excerpt=_excerpt(r),
                  raw_counts=raw, dedup_keys=dedup_keys)
    for r in shown:
        st["raw"][r["id"]] = 0
        if r.get("on") == "ordering" and ordering and ids.get(r["id"]):
            ordering.mark_fired(r["id"], ids[r["id"]])
        elif r.get("converted_rx") or (r.get("on") == "edit" and "content_rx" in r):
            st["open"][r["id"]] = ids.get(r["id"])
            if r.get("on") == "edit":
                ev = fired_on.get(r["id"])
                st.setdefault("open_file", {})[r["id"]] = ev["fp"] if ev else fp
    save_state(sp, st)
    return 0

if __name__ == "__main__":
    try:
        rc = main()
    except BaseException:
        if os.environ.get("MEMHUB_RULEBOOK_DEBUG"):      # stderr only; stdout stays silent
            import traceback
            traceback.print_exc()
        rc = 0
    sys.exit(rc or 0)
