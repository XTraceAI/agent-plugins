#!/usr/bin/env python3
"""Rulebook hook — three delivery lanes for team engineering rules.

Lanes (the mode argument):
  session  SessionStart: posture rules (on="session") in full, everything else
           as ONE compact index line. Session start is the weakest attention
           slot (measured 4% vs 88% for in-flight), so it carries worldview,
           never enforcement.
  pre      PreToolUse: proactive advisories at the violation moment (on="bash",
           "edit", "read", "write_stdlib") and the ordering-rule GATE (on="ordering").
           A read rule sees the Read tool AND the shell forms that pull a file
           into context (cat/head/tail/less/more/sed -n on a path).
  post     PostToolUse: reactive advisories on failing/erroring results
           (on="result"); ordering-rule ARM (edit-family) and RECEIPT (bash).
  fetch    Refresh the server book for one repo (GET /rules?view=hook with
           If-None-Match) into <BASE>/book/<repo>.json. The session lane spawns
           it DETACHED so SessionStart never waits on the network.
  flush    Stop / SessionEnd: POST unsent ledger rows to /fires in batches,
           behind a sent-watermark (ledger/.sent). `flush final` ignores the
           every-N-fires / every-M-minutes throttle.

Book = the server book, cached with its ETag. SessionStart re-fetches a stale
one BEFORE the digest renders (a fresh one just spawns the detached child), and
the pre lane refreshes it in the background once it is a minute old.
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
  * Every fire is DISCLOSED, on both channels, in one shape:
    `📏 Rule fired: <the rule, in 20 words or fewer>` — `⛔️` when a gate
    actually stopped the call. The USER sees it as the first line of the
    `systemMessage` stanza, above the detail line this hook has always shown
    (`XTrace ▸ …`); the AGENT is told, in `additionalContext`, to echo the
    byte-identical line at the top of its reply. Both are needed: the first is
    deterministic but invisible to everything downstream, and the second is the
    only copy that reaches the transcript session capture, a handoff or a PR
    comment can read. One function (`disclosure_line`) builds both, because a
    terminal showing one string while the agent says another would be worse
    than either channel alone.
  * The agent also gets the rule text under an XTrace Rulebook header, as
    before. Without the `systemMessage` a fire is invisible to the person the
    rule was written for.
  * `mode: gate` rules BLOCK: a pre-hook call matching a gate rule is denied
    (`permissionDecision: deny`) with the statement and the override its lane
    accepts. A Bash call takes `RULEBOOK_OVERRIDE='<why>' <command>`, which
    allows exactly that call; an edit takes a `rulebook-override[<rule>]: <why>`
    marker in the content, which allows that write and stays in the diff. The
    edit marker must name its rule BECAUSE it stays: an unnamed one would mean
    a different thing the day a second edit gate covers that line, and it is
    the form that content copied from elsewhere satisfies by accident. A Read
    tool call has neither a prefix nor content, so a blocked read is retried
    narrower (`offset`/`limit`), delegated to a subagent, or — when the whole
    file must enter THIS context — run as `RULEBOOK_OVERRIDE='<why>' cat
    <path>`, which is the Bash lane's override and records like one. Either way the fire
    records its own `override_reason`, and the next matching call is gated
    again. Gates are never deduped and never cut by the advisory cap. All
    three lanes gate because the hook sees them BEFORE they run — an edit rule
    matches `tool_input`, the content the tool is about to write, and a read
    rule the path a call is about to pull in. A result
    rule runs after the fact and cannot gate, and neither can the synthetic
    lane that finds files a shell command already wrote.
  * A gate is honoured from whatever book is cached, however old. There is no
    timer that turns a gate off: a rule retired on the server disappears at
    the next successful fetch, and a running session refreshes its own book
    once it is a minute old (pre lane, detached, throttled). A stale gate costs
    one `RULEBOOK_OVERRIDE`; a gate that silently stops enforcing because the
    server was unreachable for a day is the failure a gate exists to prevent.

What leaves the machine, exactly:
  * fetch  — the repo name (the origin remote's basename, else the directory's),
             nothing else.
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
changed against its base, a dirty tree), `user` facts (what the person
typed this session), `file` facts (how much a read would pull into context)
and `agent` facts (main agent or subagent). They are answered by `Probes`,
lazily and once per hook call, from read-only git, the local transcript and
the file the call names; a fact that cannot be established never satisfies a
predicate, so the rule stays silent. `given_ok()` is pure over a Probes and
the event's read facts, which is how the verifier feeds it fixtures.
"""
import fnmatch
import hashlib
import datetime as _dt
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
from datetime import datetime, timezone

def _load_portable_lock():
    """Load only the packaged lock shim without broadening module search."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portable_lock.py")
    spec = importlib.util.spec_from_file_location("_memhub_rulebook_portable_lock", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load portable lock shim from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    portable_lock = _load_portable_lock()
except Exception:
    portable_lock = None


def _load_repo_identity():
    """Load only the packaged repo-name shim without broadening module search."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repo_identity.py")
    spec = importlib.util.spec_from_file_location("_memhub_rulebook_repo_identity", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load repo identity shim from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    repo_identity = _load_repo_identity()
except Exception:
    repo_identity = None

BASE = os.environ.get("MEMHUB_RULEBOOK_BASE") or \
    os.path.expanduser("~/.config/memhub-plugin/rulebook")
MAX_ADVISE = 2          # per tool call — habituation guard
MAX_POSTURE = 15        # spec §2: session_context is hard-capped at 15 rules / ~2k tokens per scope
# One budget with the session-start brief (MEMHUB_BRIEF_TOKEN_BUDGET, default
# 2,500 tokens, split 2:1 brief:rulebook — navigation spec §4); this is the
# rulebook's third. The literal fallback only covers a broken sibling import.
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from brief_budget import rulebook_chars as _rulebook_chars
    POSTURE_BUDGET_CHARS = _rulebook_chars()
except Exception:
    POSTURE_BUDGET_CHARS = 3333   # 2,500 tokens × 4 chars ÷ 3
RESULT_WINDOW_CHARS = 8000    # result lane: scanned at EACH end, not just the tail
LOCK_WAIT_S = 0.05      # ordering state lock: fail open past this
LEDGER_SCHEMA = 2       # ledger/fires.jsonl row shape (spec §3.2)
BOOK_DIR = os.path.join(BASE, "book")
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

    Such a chain that exits 0 ran every one of its segments and every one of
    them succeeded — so for that shape, and only that shape, the call's single
    exit status is each segment's own. Everywhere else the last unpiped
    segment is still the only one the status belongs to.

    Separators are classified on the BLANKED copy, so an operator character
    inside a quoted argument is the data it is. This was deliberately left
    quote-unaware once, on the reasoning that mis-reading a quoted `|` only
    disqualifies a chain and so costs an extra gate. That reasoning was
    wrong: `npm test -- --grep 'a|b' && git push` is a chain whose test DID
    run and pass, and refusing to see it fires the gate at someone who has
    complied. A rule that fires when you have already done the thing is the
    one people learn to ignore — the same point `rulebook_verify` presses on
    every author."""
    shell = trim_terminators(shell)
    blank = blank_quoted(shell)
    # Any separator that is NOT `&&` disqualifies the chain — asked of
    # `_SEPARATOR_RX`, the one place that knows what a separator is. The raw
    # character scan this replaces called `git fetch 2>&1 && git log
    # origin/main` a broken chain, because a redirection `&` looks like a
    # background `&` to a scan that only reads characters. That gated a call
    # whose fetch had run and passed — the same false gate on a complying
    # caller that the quoted-operator fix removed two rounds ago, from the
    # other direction.
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
# A double-quoted span honours backslash escapes, so `"a \\"b\\" c"` is ONE
# span — ending it at the first `\\"` put the rest of the argument back into
# the shell grammar, where a `|` inside it became an operator. A
# single-quoted span has no escapes at all in POSIX shell (a backslash is
# literal and a `'` cannot appear), so it stays the simpler pattern.
_QUOTED_SINGLE = r"'[^']*'"
_QUOTED_DOUBLE = r'"(?:\\.|[^"\\])*"'
_QUOTED_RX = re.compile(_QUOTED_SINGLE + "|" + _QUOTED_DOUBLE)
QUOTED_SINGLE_RX = re.compile(_QUOTED_SINGLE)
# `&&` and `||` first, so the lone-operator alternatives only see what is
# left. A standalone `&` backgrounds the command to its left and the next one
# runs anyway — `gh pr view -R other & git push` is TWO commands. The
# lookarounds keep redirection out: `2>&1`, `cmd >&2`, `cmd &> log`.
_SEPARATOR_RX = re.compile(r"&&|\|\||;|\n|\||(?<![>&])&(?![>&])")
_AND_RX = re.compile(r"&&")
_ESCAPE_RX = re.compile(r"\\.", re.S)   # a backslash escape, outside quotes
# A `#` starts a comment at the start of a word: after whitespace, an
# operator, or a grouping paren/brace. NOT after `{` — `${#files}` is the
# length expansion, and reading its `#` as a comment blanked the rest of the
# line, so `n=${#files}; git push` reached the gate with no push in it.
_COMMENT_RX = re.compile(r"(?<![^\s;&|()}])#[^\n]*")
# A trailing `;` or newline ends the last command; it does not start an empty
# one. `pytest;` is a run of pytest, and splitting on that `;` made the last
# segment "" — a passing receipt refused, and the gate fired on a caller
# who had complied. A trailing `&` is NOT a terminator: it backgrounds.
_TRAILING_TERMINATOR_RX = re.compile(r"[\s;\n]+$")


def trim_terminators(shell):
    return _TRAILING_TERMINATOR_RX.sub("", shell or "")


def blank_quoted(text):
    """`text` with the contents of quoted spans — and every backslash escape
    outside them — replaced by spaces, character for character, so every
    offset still points at the same place.

    A `\\|` is not a pipe. `gh pr view --jq .title\\|ascii_downcase -R
    acme/other` is one command, and reading its escaped pipe as an operator
    left the `-R` in a fragment that no longer began with `gh`. Quoting was
    only ever half of "this character is data"."""
    return _COMMENT_RX.sub(lambda m: " " * len(m.group(0)), blank_syntax(text))


def blank_syntax(text):
    """Quotes and escapes blanked, comments LEFT IN PLACE, length preserved.

    The stage before comment blanking, exposed because `strip_comments` needs
    to find comments and `blank_quoted`'s output no longer has any. Escapes
    become a NON-space placeholder: blanking `\\ ` to a real space made
    `--jq .title\\ #literal` look like `#literal` starts a word, and `\\ `
    joins two words in bash, so the placeholder has to join them here too."""
    blanked = _QUOTED_RX.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 2)
                             + m.group(0)[-1], text or "")
    return _ESCAPE_RX.sub("\x01\x01", blanked)


def unquoted(text):
    """`text` with the CONTENTS of quoted spans removed.

    A regex over a whole segment cannot tell a command from an argument that
    merely spells one, and everywhere else in this hook that only over-fires.
    On the two paths that let a call OUT of a gate — the self-discharge
    exemption and the receipt — it lets it out instead, which is the one
    direction that must not happen: `echo 'git fetch' && git log origin/main`
    and `grep 'git fetch' setup.sh && git log origin/main` would both read the
    stale ref with the obligation cleared. Blanking quoted text costs a
    receipt whose command is genuinely quoted (`pytest "tests/x"`), and that
    costs an extra gate rather than a missed one."""
    return blank_quoted(text)


CMD_WRAPPERS = frozenset({"env", "command", "builtin", "exec", "sudo", "doas",
                           "nohup", "time", "nice", "stdbuf", "setsid",
                           "sh", "bash", "zsh", "dash", "ksh"})
# `cd` is a shell builtin, so only the wrappers that run BUILTINS can carry it
# — `sudo cd x` cannot move this shell and `env cd x` fails outright.
CD_WRAPPERS = frozenset({"command", "builtin"})
EXPANSION_RX = re.compile(r"[$`]")   # `$VAR`, `${…}`, `$(…)`, backticks
CMD_PREFIXES = frozenset({"!", "if", "elif", "then", "else", "while", "until", "do"})
BLOCK_END = frozenset({"fi", "done", "esac", "}", ";;"})
COMPOUND = frozenset({"case", "select", "coproc"})



def executes(segment, rx):
    """Does this segment run the command `rx` describes?

    SYNTAX ONLY. The hook understands shell syntax — quotes, comments,
    `&&`/`||`/`;`/pipes/background — and nothing about what any command
    DOES. So this blanks quoted spans and comments, drops leading `FOO=1`
    assignments and grouping braces, and searches the rest. There is no list
    of runners: `timeout 300 pytest`, `.venv/bin/pytest`, `caffeinate -i
    pytest` and the next wrapper nobody thought of all discharge, because the
    alternative — a list of commands known to run their argument — was wrong
    for every wrapper not on it, and a missed receipt blocks someone who
    complied.

    KNOWN AND ACCEPTED RESIDUAL: an UNQUOTED mention discharges. `echo
    pytest` clears a test obligation; `grep -n pytest README.md` clears it.
    A quoted one does not (`echo 'git fetch'`, `git commit -m 'ran pytest'`),
    and a comment does not. This is accepted because the obligation is
    advisory — whether the run was SUFFICIENT (right tests, right args) was
    never knowable here either, and a caller who wants past a gate has the
    recorded `RULEBOOK_OVERRIDE=` door already. What the hook closes is the
    accidental bypass a quoted string or a comment produces; an unquoted
    `echo pytest` is not a shape anyone types by accident."""
    # Grouping is not part of a command's name — `(git fetch -q)` runs the
    # fetch and propagates its status, so it is as good a receipt as the bare
    # form.
    text = strip_leading_assignments(
        unquoted(segment or "").strip("(){} \t")).strip()
    # `!` inverts a pipeline's status: `! git fetch && git log origin/main`
    # reaches the log only when the fetch FAILED, and the exit code of `!
    # pytest` is green exactly when the tests were not. Still syntax, not
    # command knowledge — a negated segment vouches for nothing.
    if text.startswith("!"):
        return False
    return bool(text) and bool(re.search(rx, text))


def self_discharging(shell, spec):
    """Does this one call run the required command BEFORE the gated one, in a
    chain whose single exit status vouches for the required part?

    `&&`-only, and the required segment must come first: those are the two
    conditions under which the gated segment cannot run unless the required
    one already ran and passed. Anything else — a `||`, a `;`, a pipe, or the
    required command written after the gated one — is a command the gate is
    there for."""
    segs = and_only_segments(shell)
    required = next((i for i, part in enumerate(segs)
                     if executes(part, spec["required_command_rx"])), None)
    if required is None:
        return False
    return any(command_fires(spec["gated_command_rx"], unquoted(part), flags=0)
               for part in segs[required + 1:])


def receipt_segments(shell, whole_chain=False):
    """The segments of `shell` whose success the call's exit status vouches
    for. Today's answer — the last segment, unpiped, not backgrounded — is
    what an arbitrary command line can support. `whole_chain` widens it to
    every segment of an `&&`-only chain, which is sound (see
    `and_only_segments`) and is what the session- and prompt-armed rules
    need: the shape they are about puts the required command FIRST
    (`git fetch -q && git log origin/main`) and never last."""
    shell = trim_terminators(shell)
    if whole_chain:
        segs = and_only_segments(shell)
        if segs:
            return segs
    # The last segment is a receipt only if it NECESSARILY ran. Reached
    # through `||` it ran only when the one before it FAILED, so `true || git
    # fetch` exits 0 from `true` and never fetches — and taking that as a
    # receipt discharged the obligation with the required command unrun. `&&`
    # and `;` both guarantee it ran, and then the call's status is its own.
    #
    # Joiners are read with `last_segment`'s own splitter (single `|` is not
    # one of them, which is what the pipe test below still relies on).
    joiners = [m.group(0) for m in _LAST_SEG_SPLIT_RX.finditer(blank_quoted(shell))]
    if joiners and joiners[-1] == "||":
        return []
    # A call that ENDS in a background `&` now yields an empty last segment —
    # the separator is the final token — and an empty one is no receipt. That
    # is right: `git fetch &` exits 0 from launching the job, not from the
    # fetch, which may still be running or about to fail.
    last = last_segment(shell)
    # On the blanked copy, like everything else that asks whether a character
    # is an operator. `npm test -- --grep 'a|b'` is not a pipeline, and
    # reading it as one refused a receipt for a test that had passed —
    # leaving the obligation armed and blocking the push. This is the FOURTH
    # finding from raw-vs-blanked (rounds 7, 13, 15, 16); `last_segment`
    # itself was fixed last round and this line beside it was left reading
    # raw.
    if last and "|" not in blank_quoted(last):
        return [last]
    return []


# ── a leading assignment is not part of the command ─────────────────────────
#
# `FOO=1 git push` execs `git push` — bash strips the assignment before it
# looks up the command, and a rule has to read it the same way. Otherwise an
# ANCHORED rule is silently bypassed: `^git\s+push` never sees a command that
# begins with an assignment, so the call runs with no deny and no fire, which
# is the one outcome a gate exists to prevent.
#
# `strip_override` already makes exactly this statement about the single
# RULEBOOK_OVERRIDE token ("rules match the command, not the assignment"). It
# just cannot make it when `find_override` REFUSED the token — and the refused
# shape is `RULEBOOK_OVERRIDE=` with an empty reason, which is what the deny
# message invites the caller to type.
_ASSIGN_TOKEN_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|\S*)\s*")
_ASSIGN_NAME_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def strip_leading_assignments(shell):
    """`shell` with the env assignments that BEGIN a segment removed, byte for
    byte identical everywhere else.

    Tokenised per line by the same shlex walk `find_override` uses, so an
    assignment inside a quoted argument (`echo 'A=1 git push'`) is data and
    stays put, and a line shlex cannot parse is handed back untouched. A run is
    stripped whole (`FOO=1 BAR=2 git push` -> `git push`), because bash treats
    all of it as the command's environment."""
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

    `blank_quoted` answers "which characters are syntax", and blanks quoted
    content along the way — right for finding operators, wrong for matching a
    rule, which is deliberately allowed to see inside quotes. This asks only
    the comment question, off `blank_syntax` (which has not blanked comments
    yet), then blanks that span in the original.

    A `#` that STARTS a word comments out the rest of the line. `git fetch #
    git log origin/main` runs only the fetch, and the gate was matching the
    commented `git log` and blocking a compliant call. Mid-word — `%h#%s`, a
    URL fragment — is not a comment at all."""
    out = list(text or "")
    for m in _COMMENT_RX.finditer(blank_syntax(text)):
        for i in range(m.start(), m.end()):
            out[i] = " "
    return "".join(out)


def command_fires(rx, text, not_rx=None, flags=re.I | re.M):
    """Does `rx` match this command, given that a leading env assignment is not
    part of it, and that a `#` comment is not part of it either?

    Comments are blanked HERE rather than at each caller: this is the one
    function that answers "does this command match", for the matcher lane and
    the ordering gate alike, and blanking in `blank_quoted` only reached the
    callers that happened to use it. `git fetch # git log origin/main` runs
    only the fetch, and the gate was reading the commented `git log` and
    blocking a compliant call.

    The command is read as BOTH forms — as written, and with the assignments
    that begin a segment removed. `rx` fires when EITHER matches, so an anchored
    rule stops being bypassed by a prefix while a rule written to catch the
    assignment itself (`AWS_SECRET_ACCESS_KEY=`) still fires on the raw text.

    `not_rx` is a VETO across the same pair, checked first: an exemption its
    author wrote against either shape exempts the call. Testing it per-form
    instead would let a prefix delete the very token the exemption keys on, so
    `FOO=1 cmd` would defeat an exemption that `cmd` honours — stripping would
    become a way to BREAK an exemption, which is the opposite of the point."""
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


# ── files a Bash call READS into context ────────────────────────────────────
#
# A read rule (`event: read`) is about what enters the model's context. The
# Read tool is one door; `cat`, `head`, `tail`, `less`, `more` and `sed` on a
# path are the other, and in auto mode the model is TOLD to use them.
# Measured on 14 days of local sessions: 3,123 Read calls with a median file
# of 22 lines, against 1,113 bare cat/head/tail segments — and 67 of the 80
# largest tool results were whole-file shell dumps. A rule that watched only
# the Read tool would have missed every one of those.
#
# Read from the COMMAND, not the disk after the fact: this lane must gate, and
# a gate can only refuse a call it sees before it runs. A pipe (`cat f |
# grep`) or a redirect (`cat f > out`) is not a read into context and is
# skipped, and so is any form this parser cannot name — every doubt resolves
# to "no read here", which under-counts and never false-fires. `cd` is
# tracked segment by segment, because `cd <repo> && cat spec.md` is how a
# real command reads a relative path, and it is the form a prefix-anchored
# hook (`^cat`) misses entirely.

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
    the file's length, narrowed by the Read tool's `offset`/`limit` or by the
    line count a shell form asks for. None when the path is not a regular
    file, and None never satisfies a `given.file` predicate: a rule about a
    file it cannot measure stays silent. `total` pre-answers the line count —
    the verifier's way in, so a case needs no real file."""
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
            if not command_fires(rule["rx"], target, rule.get("not_rx")):
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
        if hook_phase == "pre" and on == "read" and tool in READ_TOOLS and file_path:
            # Which file, not how much: size is a `given.file` fact, answered
            # per event, so the same rule reads the same on the Read tool and
            # on a `cat`. A read that came through a shell command honours the
            # rule's command exemption — the veto a bash rule gets.
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
            # A long result puts the two things a rule looks for at OPPOSITE
            # ends: pytest prints the traceback at the top and the failure
            # summary at the bottom, so a tail-only window silently misses
            # every exception in a run long enough to need a window at all.
            # Scan both ends, as separate spans so no pattern can match across
            # the gap between them.
            if len(result_text) <= 2 * RESULT_WINDOW_CHARS:
                spans = (result_text,)
            else:
                spans = (result_text[:RESULT_WINDOW_CHARS],
                         result_text[-RESULT_WINDOW_CHARS:])
            # exclude_rx exempts the whole result (an exempt test name usually
            # sits outside the matched span), not just the matched substring —
            # so it is checked over the same spans the match is drawn from
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
    LOCK_WAIT_S; past that the hook fails open) and replaces the file atomically.
    An arm and a discharge from two sessions must never overwrite each other —
    those are the two outcomes a gate exists to prevent."""

    def __init__(self, worktree_root, branch):
        os.makedirs(os.path.join(BASE, "state"), exist_ok=True)
        key = hashlib.sha1(worktree_root.encode("utf-8")).hexdigest()[:16]
        self.path = os.path.join(BASE, "state", f"wt-{key}.json")
        self.branch = "*"            # branch is recorded on fires, not used as a key

    def _locked(self):
        if portable_lock is None:
            # Matcher gates need no shared state; only ordering gates fail open.
            return None
        lock = open(self.path + ".lock", "a+", encoding="utf-8")
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                portable_lock.lock_exclusive(lock.fileno(), blocking=False)
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
            portable_lock.unlock(lock.fileno())
            lock.close()

    def feed(self, rule, *, hook_phase, tool, cmd="", file_path="", ok=None, armed=None):
        """Returns "fired" | "allowed" | "discharged" | None. Mutates state
        under lock; None on lock timeout (fail open).

        `armed` is the event that armed this obligation OUTSIDE the worktree
        state — "session" or "prompt" — read by the caller from the SESSION's
        own state file. A session- or prompt-armed obligation belongs to one
        session (each session must fetch before it reads `origin/*`; the
        sibling session down the hall fetching does not answer for this one),
        so it cannot live in the worktree state every session shares."""
        spec = rule["ordering"]
        armed_by = tuple(spec.get("armed_by_events", ("edit", "write")))
        by_call = any(k in armed_by for k in ("session", "prompt"))
        is_edit = tool in EDIT_TOOLS and hook_phase == "post"
        if is_edit and not any(k in armed_by for k in ("edit", "write")):
            return None
        if is_edit and spec.get("path_rx") and not re.search(spec["path_rx"], file_path):
            return None
        seg = shell_only(cmd) if cmd else ""
        # A Bash call reports ONE exit status. It is the receipt's own status
        # only when the receipt is the final segment and not piped (`pytest |
        # tail` returns tail's status). Earlier segments / pipelines never
        # discharge — under-counting is the safe direction. A session- or
        # prompt-armed rule reads an `&&`-only chain whole instead
        # (`receipt_segments`): its required command sits FIRST in that chain,
        # never last, and a chain that exits 0 vouches for every segment.
        is_receipt = hook_phase == "post" and tool == "Bash" and seg and \
            any(executes(part, spec["required_command_rx"])
                for part in receipt_segments(seg, whole_chain=by_call))
        is_gate = hook_phase == "pre" and tool == "Bash" and seg and \
            command_fires(spec["gated_command_rx"], seg, flags=0)
        # One call that runs the required command BEFORE the gated one
        # discharges its own obligation (`git fetch -q && git log
        # origin/main`), and blocking it would be a gate firing on the very
        # call that satisfies it.
        #
        # ORDER and JOINER both have to hold. Accepting the required pattern
        # anywhere in the string — which the miner's offline replay does, and
        # which this did to agree with it — exempts two commands that are
        # exactly what the rule exists to catch: `git log origin/main && git
        # fetch` reads the stale ref and fetches afterwards, and `git fetch ||
        # git log origin/main` runs the stale read PRECISELY when the fetch
        # failed. Agreeing with a replay's approximation is not worth a hole
        # in the gate; the replay counts a few more sessions than the engine
        # gates, and that is the right direction for the two to differ in.
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
                    # For an EDIT-armed rule, conversion is (worktree,
                    # branch)-scoped: a subagent's or sibling session's
                    # receipt converts whichever fire is open, because the
                    # obligation belongs to the checkout.
                    #
                    # A session- or prompt-armed one belongs to the SESSION,
                    # so its open fire is not here to convert — the caller
                    # holds it in session state. Popping the shared slot let
                    # one session's compliance mark ANOTHER session's fire
                    # converted, and two concurrent fires overwrote the single
                    # slot so attribution followed execution order rather than
                    # who complied.
                    if not by_call:
                        rule["_converted_fire"] = s.pop("open_fire", None)
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


# ── error-arc pairing (harness-tied-memory-spec §4.1) ───────────────────────
#
# A tool error on target T and a later success on the same T are ONE moment —
# what broke and what fixed it — not two. The post lane already sees every
# Bash result, so the pair is recorded here, into the session state the Stop
# sensor (`harness_stop.py`) drains at the end of the turn: a closed arc is
# routed as a signal, an arc still open at Stop was just a failure, and the
# open set is emptied so nothing pairs across turns. Behind the harness flag
# because it is harness bookkeeping: with the flag off the lane is byte-for-byte
# what it was. +≤5 ms — a dict update, no I/O beyond the state file already
# being saved.
HARNESS_FLAG = "MEMHUB_HARNESS_EXTRACT"    # the one S1 switch; `harness_extract.extract_enabled` is the same test
ARCS_OPEN_MAX = 20            # distinct failing targets tracked per session
ARCS_CLOSED_MAX = 20          # closed arcs waiting for the Stop sensor


def harness_extract_on(environ=None):
    env = os.environ if environ is None else environ
    return str(env.get(HARNESS_FLAG, "")).strip().lower() in ("1", "on", "true", "yes")


def pair_error_arc(st, cmd, resp):
    """Record a Bash failure on `cmd`, or close the arc a success on the same
    command completes. `cost` is the number of Bash results between the two —
    the spec routes an arc that cost ≥ 5 calls whatever its signature."""
    key = shell_only(cmd or "").strip()[:200]
    if not key:
        return
    n = st["arc_calls"] = int(st.get("arc_calls") or 0) + 1
    opened = st.setdefault("arcs_open", {})
    if bash_ok(resp):
        first = opened.pop(key, None)
        if first is not None:
            closed = st.setdefault("arcs_closed", [])
            closed.append({"signature": first.get("signature", ""), "target": key,
                           "fix": key, "cost": n - int(first.get("call") or n),
                           "at": _now()})
            del closed[:-ARCS_CLOSED_MAX]
        return
    if key not in opened:
        opened[key] = {"signature": result_text(resp)[:200], "call": n}
        for stale in list(opened)[:-ARCS_OPEN_MAX]:
            opened.pop(stale, None)


def take_error_arcs(session_id):
    """The closed arcs since the last take, and a fresh open set — called by the
    Stop sensor once per turn. Never raises; an unreadable state is no arcs."""
    try:
        sp = state_path(session_id)
        st = load_state(sp)
        closed = list(st.pop("arcs_closed", None) or [])
        had_open = bool(st.pop("arcs_open", None))
        if closed or had_open:
            save_state(sp, st)
        return closed
    except Exception:
        return []


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
    # what a read would pull into context — answered per EVENT (`read_facts`),
    # not per call, so a `cat a b` is measured file by file
    "file": {"lines_gt": "int", "bytes_gt": "int"},
    # main agent vs subagent (transcript under <session>/subagents/). A rule
    # about the main context's budget says `main: true`, and a subagent's
    # reads pass — delegation is the way past the rule, so it must not gate
    # the delegate.
    "agent": {"main": "bool"},
}


_VERSION_RX = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_HOOK_VERSION = []          # memo: the manifest is read at most once per process


def version_tuple(v):
    """`major.minor.patch` as a comparable tuple, or None.

    Strict on purpose: three ASCII-digit components and nothing else. There is
    no prerelease in this plugin's history to support, and a grammar that
    admits one buys a pile of ordering questions ("is 1.0.0-rc older than
    1.0.0?") to answer a version string nobody publishes."""
    m = _VERSION_RX.match(v.strip()) if isinstance(v, str) else None
    return tuple(int(g) for g in m.groups()) if m else None


def hook_version():
    """This hook's own version, from the plugin manifest beside it.

    None when the manifest is missing, unreadable, or does not carry a
    `major.minor.patch` — and an unknown version satisfies no
    `min_hook_version`, so a hook that cannot say what it is degrades a rule
    rather than gating on a condition it may not understand."""
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


# Every key of an `ordering` block this hook knows how to honour. A rule may
# be written for a NEWER one: the engine reads a block with `spec.get(...)`,
# so an unknown key is silently ignored and the rule runs as if the author
# had not written it. That is the 0.40.1 shape — see `degradation`.
_ORDERING_KEYS = frozenset({"required_command_rx", "gated_command_rx", "armed_by_events",
                            "armed_by_rx", "min_edits", "display_name", "path_rx"})
_ARMED_BY_EVENTS = frozenset({"edit", "write", "session", "prompt"})


def given_unsupported(g):
    """The first `block.key` of a `given` this hook does not know, or "".

    A different question from `given_norm`'s: that one refuses a value of the
    wrong KIND, which is a malformed rule however new the hook. This one finds
    a predicate written for a hook we are not, which is version skew — and the
    same `_GIVEN` table answers both, so there is no second list to drift."""
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

    A rule may be newer than the hook reading it, and until now that was
    silent in the worst direction: the engine reads an `ordering` block with
    `spec.get(...)`, so a key it does not know is ignored and the rule runs as
    if the condition were satisfied. Version 0.40.1 did exactly that with a
    `given` — five spurious overrides in one session, and nothing anywhere
    said the hook had not read the rule it was enforcing.

    A rule that says so itself (`min_hook_version`) and one that merely
    carries a key we do not know are the same fact, so they degrade the same
    way: the rule advises, never gates, and says once per session that this is
    what happened.

    SCOPE, stated plainly: this protects FORWARD skew — this hook reading a
    rule written for a later one. It cannot protect a hook OLDER than the
    field itself, because the check is code that only the newer hook has: 0.53
    loads a rule floored at 0.54 and its ordering engine ignores the condition
    it cannot read. Closing that needs the server to serve an advice-only
    representation to a hook below the floor, which is why `fetch_book` sends
    `hook_version`; the server half is not in this repo."""
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
    per hook call. Nothing runs unless a rule whose regex already matched
    carries a `given`, each git call is read-only and bounded by
    PROBE_TIMEOUT_S, and nothing here leaves the machine. A probe that fails
    answers None, and None never satisfies a predicate: a rule with a `given`
    it cannot check stays silent, which is the fail-open direction.
    `fixture` pre-answers probes by name — the verifier's and the tests' way
    in, so given_ok() never needs a real repo to be exercised."""

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

        Read off the command because that is the only place the answer exists —
        but the command is written by the party the rule gates, so a named base
        is CHECKED, never taken on trust. Three ways it is refused, each of
        which falls through to the remote default and so OVER-measures rather
        than under-measures:

        * **Not a plain remote branch name.** Rev syntax (`HEAD`, `abc123`,
          `main~40`) is not a base a PR can merge into, and would let the
          comparison point be moved anywhere in history.
        * **Nothing to merge into it** — `merge-base(base, HEAD) == HEAD`. This
          is the bypass that matters: naming your OWN branch, or any descendant
          of it, makes the diff measure zero and a 5,000-line branch reads as
          empty. A PR onto such a base would be empty too, so no honest call
          names one.
        * **More than one distinct base named.** `… --base <mine> || … --base
          staging` would probe the first and open the second. If a command
          cannot say plainly what it merges into, it does not get to choose.

        None of this makes the gate proof against the party running the shell —
        nothing here could, and `RULEBOOK_OVERRIDE=` is the sanctioned way past
        it precisely because it is RECORDED. What this closes is the silent
        version: a bypass that leaves no fire and no reason behind it.
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
    "given",            # rides inside the matcher block; linted by `given_norm` below
})


def matcher_unsupported(m):
    """The first matcher key this hook has no code for, or "". The hook
    degrades such a rule to advice; the verifier refuses it outright, since
    to an author it is a typo. One list, asked from both places."""
    if not isinstance(m, dict):
        return ""
    return next((k for k in m if k not in _MATCHER_KNOWN), "")
_SCOPE_MAP = {"turn": "call", "file": "session", "session": "session"}   # warn_once_per → fire_scope
_RESERVED_RULE_KEYS = frozenset({"id", "text", "why", "status", "mode", "_version", "_label",
                                 "on", "repo_scope", "_scope_repos", "_scope_paths",
                                 "_scope_exclude_paths", "anchors", "ordering",
                                 "_rulebook_id", "_book_name", "_book_scope", "_book_members",
                                 "min_hook_version", "_degraded"})


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


def _degrade(row, r, given=None, unknown_matcher=""):
    """Mark `r` advise-only when this hook cannot honour `row` in full."""
    if r is None:
        return None
    r.pop("min_hook_version", None)      # answered here; never a matcher field
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
            dest = keys.get(k, k)
            if dest in _RESERVED_RULE_KEYS:   # a matcher key can never overwrite the row's own fields
                continue
            r[dest] = v
        r["fire_scope"] = _SCOPE_MAP.get(str(r.get("fire_scope", "session")), r.get("fire_scope"))
        if not all(rx_ok(r[k]) for k in _RX_KEYS if k in r):
            return None
        raw_given = r.get("given")
        if "given" in r and not _norm_given(r):
            return None
        return _degrade(row, r, raw_given, unknown_matcher=unknown)
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


def fetch_book(repo, timeout=None):
    """GET /rules?repo=<repo>&view=hook with If-None-Match.

    No `status=` param: `view=hook` serves ACTIVE rules on its own, and the
    server's filter grammar changed under us once already (a bare
    `status=active` became a 400), taking every book fetch down silently.
    Not sending the parameter is the one form no grammar change can break.
    200 → rewrite the cache; 304 → touch fetched_at (the book is confirmed
    current, which is what §5.3 gate freshness measures); anything else →
    the cache is left exactly as it was.

    `hook_version` rides along so the SERVER can enforce a rule's
    `min_hook_version`. That floor cannot be enforced only here, and saying so
    plainly: the check lives in code that exists only in the hook it is
    protecting against. A 0.53 hook does not know the field, loads the rule
    anyway, and its ordering engine ignores the condition it cannot read — the
    exact 0.40.1 shape. What the local check buys is FORWARD skew (this hook
    reading a rule written for a later one); the backstop for hooks already
    installed has to be the server serving them an advice-only representation,
    and it cannot do that without being told who is asking. An older hook
    sends no version, which is itself the signal that it predates the field.
    Unknown query parameters are ignored by every backend this has run
    against, so sending it costs nothing while the server side is unbuilt."""
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
    relevance judge). Returns the kept server ROWS (not ids: the reply
    carries title/statement/version/anchors, which is a whole rule, and the
    caller needs them for a rule its cached book does not have yet), or [] on
    ANY failure: an anchor being present is not relevance, and a judge outage
    is never a reason to block or slow the call."""
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
        # The lane's only record of working. Zero kept rules is still a success:
        # what is being retracted is "recall is failing", not "a rule matched".
        _breadcrumb_clear("recall")
        return [r for r in reply.data.get("rules") or []
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


def _breadcrumb_clear(what):
    """Retract the breadcrumb once ``what``'s own lane has worked again.

    Without this a lane that records no success of its own — recall — leaves a
    single blip standing until some OTHER lane happens to succeed. Recall runs
    on PreToolUse and the book is only refetched at SessionStart, so one 1.5 s
    timeout mid-session reliably produced a health banner at the next session
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
        portable_lock.unlock(lock.fileno())
        lock.close()


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
    if seed:
        info = repo_info(seed)
        if info[0]:
            return info
    return repo_info(cwd)


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
        return h.split(":", 1)[1].strip() if h.startswith("ref:") else "detached"
    except Exception:
        return ""


def state_path(session_id):
    sdir = os.path.join(BASE, "state")
    os.makedirs(sdir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session_id or ""))[:80] or "nosession"
    return os.path.join(sdir, f"{safe}.json")


def load_state(p):
    st = {"fired": [], "counts": {}, "raw": {}, "open": {}, "armed": {},
          "armed_once": [], "armed_fire": {}, "armed_version": {}}
    try:
        with open(p, encoding="utf-8") as f:
            st.update(json.load(f))
    except Exception:
        pass
    if not isinstance(st.get("armed"), dict):   # a file an older hook wrote
        st["armed"] = {}
    if not isinstance(st.get("armed_once"), list):
        st["armed_once"] = []
    if not isinstance(st.get("armed_fire"), dict):
        st["armed_fire"] = {}
    if not isinstance(st.get("armed_version"), dict):
        st["armed_version"] = {}
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
    """Forget a session arming and the open fire it carries; returns that
    fire's id, if any, so the caller can close it in the ledger."""
    st["armed"].pop(rid, None)
    st["armed_version"].pop(rid, None)
    return st.setdefault("armed_fire", {}).pop(rid, None)


# The session's obligation keys. A hook process reads the whole state file,
# works, and writes the whole file back; two hooks of ONE session can overlap
# (parallel tool calls, a sub-agent's calls), and the second writer's
# snapshot used to put back an arming the first had just discharged — so a
# gated command stayed blocked after its required command had run. These
# keys are therefore merged by DELTA under a lock: what this process armed
# is added, what it discharged is removed, and everything else is whatever
# is on disk now.
_ARMING_KEYS = ("armed", "armed_fire", "armed_version")
_APPEND_KEYS = ("armed_once",)      # only ever appended to


def snapshot_arming(st):
    """What the arming keys looked like when this process loaded the state —
    the baseline `save_state` diffs against."""
    return {k: dict(st.get(k) or {}) for k in _ARMING_KEYS}


def _state_lock(p):
    """Exclusive lock on the session state's sidecar, or None past
    LOCK_WAIT_S (the hook fails open — a plain write, today's behaviour)."""
    if portable_lock is None:
        return None
    try:
        lock = open(p + ".lock", "a+", encoding="utf-8")
    except Exception:
        return None
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            portable_lock.lock_exclusive(lock.fileno(), blocking=False)
            return lock
        except OSError:
            if time.monotonic() >= deadline:
                lock.close()
                return None
            time.sleep(0.005)


def save_state(p, st, before=None):
    """Write the session state. With `before` (a `snapshot_arming` taken at
    load), the arming keys are merged by delta against the file as it is NOW,
    under the session lock, so a concurrent hook's write cannot resurrect an
    obligation this one discharged, nor drop one this one armed."""
    lock = _state_lock(p) if before is not None else None
    try:
        if lock is not None:
            cur = load_state(p)
            for k in _ARMING_KEYS:
                merged = dict(cur.get(k) or {})
                for rid in before[k]:
                    if rid not in st[k]:
                        merged.pop(rid, None)          # discharged by this process
                for rid, v in st[k].items():
                    if rid not in before[k] or before[k][rid] != v:
                        merged[rid] = v                # armed or re-versioned here
                st[k] = merged
            for k in _APPEND_KEYS:
                seen = list(cur.get(k) or [])
                st[k] = seen + [x for x in st[k] if x not in seen]
        with open(p, "w", encoding="utf-8") as f:
            json.dump(st, f)
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
    r"|Base directory for this skill:)")


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
    only one session capture, /memhub:rules-from-sessions, a handoff or a PR
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
              override_reasons=None):
    """One ledger row per (rule, fire) — spec §3.2. Identifiers, not payloads:
    `excerpt` stays in this LOCAL file and never crosses the wire without
    org opt-in. `override_reasons` is {rule_id: why} for the gates this call
    excused, so a row records the reason for ITS rule — one call can excuse one
    gate and be blocked by another (§5.3). `rulebook_id` is local too — POST /fires carries no book
    dimension (container spec §6.4), so it is absent from WIRE_KEYS on purpose;
    it is here so a local reader can tell which book a fire came from.
    Returns {rule_id: fire_id} so conversions can point back."""
    if portable_lock is None:
        # Enforcement still runs, but do not create telemetry that cannot drain.
        return {}
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
                    "override_reason": (override_reasons or {}).get(r["id"]),
                    "excerpt": excerpt[:160],
                }) + "\n")
    except Exception:
        pass
    return ids


def log_conversion(fire_id, how):
    """Append-only sidecar (the fires file is shared across sessions, so it
    is never rewritten in place). A reader merges by fire_id."""
    if portable_lock is None:
        return
    try:
        with open(os.path.join(_ledger_dir(), "conversions.jsonl"), "a",
                  encoding="utf-8") as f:
            f.write(json.dumps({"fire_id": fire_id, "converted": True,
                                "converted_at": _now(), "how": how}) + "\n")
    except Exception:
        pass


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
    already current, so the common case keeps the detached spawn and pays
    nothing. A stale one is worth waiting for, bounded by
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
        else:
            spawn_fetch(repo)
    except Exception:
        pass
    return rules, fetched_at, sources


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "pre"
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
            print("usage: rulebook_hook.py book-path <repo>", file=sys.stderr)
            return 2
        print(book_path(repo))
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
    session = data.get("session_id", "")
    # `repo_of_call` derives this too, but the probe root below still needs
    # the SESSION's directory: a `cd` or `-C` in the command is resolved
    # against where the terminal is, not against the edited file's checkout.
    cwd = data.get("cwd") or os.getcwd()
    repo, root, gitdir, branch = repo_of_call(data)
    if not repo:            # nothing this call touches is in a git repo → no rules apply
        return 0
    if mode == "fetch":
        fetch_book(repo)
        return 0
    rules, rule_version, fetched_at, sources = load_rules(repo)
    tool = data.get("tool_name", "")
    ctx = {"session": session, "agent_id": agent_id_of(data), "repo": repo,
           "branch": branch, "tool": tool, "rule_version": rule_version,
           "source_message_id": message_id_of(data)}
    if mode == "prompt":
        # UserPromptSubmit. It arms and says nothing: anything printed here is
        # injected above the person's own words, and an arming is not news —
        # the fire at the gated command is.
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
        # already current, so it keeps the detached spawn and SessionStart pays
        # nothing — the common case, since the pre lane refreshed it minutes
        # ago. Only a stale book is worth waiting for, and never longer than
        # SESSION_FETCH_TIMEOUT_S: `fetch_book` leaves the cache untouched on
        # every failure path, so a timeout renders exactly what we already had.
        rules, fetched_at, sources = refresh_if_stale(repo, rules, fetched_at, sources)
        try:        # which source each rule came from — the pilot's merge audit
            _atomic_json(book_path(repo) + ".sources", {"at": _now(), "sources": sources})
        except Exception:
            pass
        session_digest(rules, repo, gitdir, ctx)
        arm_obligations(rules, repo, gitdir, session, "session")
        return 0
    if mode == "pre":
        maybe_refresh(repo, fetched_at)

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
    fp = str(inp.get("file_path", ""))
    body = str(inp.get("new_string", "")) + str(inp.get("content", "")) + \
        "\n".join(str(e.get("new_string", "")) for e in (inp.get("edits") or []) if isinstance(e, dict))
    # §5.3: the edit lane's own override. Which gates it actually excuses is
    # decided once the gates are known — a marker naming a rule excuses that
    # rule only.
    edit_markers = ({k: redact_secrets(v)[:2000] for k, v in find_edit_override(body).items()}
                    if mode == "pre" and tool in EDIT_TOOLS else {})
    rtext = result_text(data.get("tool_response")) if mode == "post" else ""
    resp = data.get("tool_response") if (mode == "post" and tool == "Bash") else None
    if resp is not None and cmd and harness_extract_on():
        try:
            pair_error_arc(st, cmd, resp)     # §4.1; drained by harness_stop.py at Stop
        except Exception:
            pass
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
            "body": body, "rtext": rtext, "resp": resp, "via": None, "read": None}
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

    # Conversions: did this call perform the action an earlier fire asked for?
    # Deterministic, under-counts, never over-counts (spec §5.1).
    for rid, fid in list(st["open"].items()):
        r = by_id.get(rid)
        if not r:
            continue
        crx = r.get("converted_rx")
        if mode == "post" and tool == "Bash" and crx and cmd \
                and re.search(crx, strip_comments(shell_only(cmd)), re.I | re.M):
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
        for row in recall_anchor_rules(repo, tool, handles, st["fired"]):
            rid = str(row.get("rule_id"))
            # Prefer the cached rule — it carries scope and the book facts the
            # wire row omits. Otherwise build one from the reply: the server
            # matched this rule, judged it relevant and scoped it to this repo,
            # and dropping it because our book predates it is exactly how a
            # newly activated anchor rule stayed silent until the next fetch.
            # Safe unseen: recall rules can only advise (server §4.7) and
            # `to_hook_rule` defaults `mode` to advise, so no gate arrives here.
            r = anchor_rules.get(rid) or to_hook_rule(row)
            if r is not None and r.get("on") == "anchor":
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
                    # worktree state: it was never written there. Its open
                    # fire lives beside it for the same reason — a sibling
                    # session sharing the checkout must not convert it.
                    fid = drop_arming(st, rid)
                    if fid:
                        log_conversion(fid, "discharged")
                if outcome == "discharged" and r.get("_converted_fire"):
                    log_conversion(r["_converted_fire"], "discharged")
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
                               body=ebody, result_text=ev["rtext"]) \
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
            fired_now.append(r)
            fired_on[rid] = ev

    if not fired_now:
        save_state(sp, st, before=before)
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
        return tool in EDIT_TOOLS and r.get("on") == "edit"

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
    if override_reason is not None:
        overridden = {r["id"]: override_reason for r in fired_now if r["id"] in gate_ids}
    elif edit_markers:
        for r in (r for r in fired_now if r["id"] in gate_ids):
            named = edit_markers.get(_label_of(r)) or edit_markers.get(str(r["id"]).lower())
            if named:
                overridden[r["id"]] = named
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
    blocked = any(r["id"] not in overridden for r in gates)
    lines = [f"## {BRAND} Rulebook — BLOCKED" if blocked
             else f"## {BRAND} Rulebook (team rules — advisory, not blocking)"]
    user_lines, deny_lines = [], []

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
        stale_key = f"_degraded:{r['id']}"
        if r.get("_degraded") and stale_key not in st["fired"]:
            st["fired"].append(stale_key)
            lines.append(f"  _(advice only — {r['_degraded']}. Update the "
                         f"{BRAND} plugin to let this rule gate.)_")
        blocked_here = r["id"] in gate_ids and r["id"] not in overridden
        if r["id"] not in gate_ids:
            lines.append(f"- **[{label}]** {r['text']}{detail}{_where(r)}{_why(r)}")
            detail_line = f"{BRAND} ▸ [{label}] {r['text']}{detail}{_where(r)}"
        elif r["id"] in overridden:
            why = overridden[r["id"]]
            lines.append(f"- **[{label}]** {r['text']}{detail}{_where(r)}{_why(r)} "
                         f"_(gate overridden: {why})_")
            detail_line = f"{BRAND} ⚠ gate overridden — [{label}] {why}"
        else:
            lines.append(f"- **BLOCKED [{label}]** {r['text']}{detail}{_where(r)}{_why(r)}")
            detail_line = f"{BRAND} ⛔ blocked by [{label}] {r['text']}{detail}{_where(r)}"
            deny_lines.append(f"[{label}] {r['text']}{detail}{_where(r)}")
        # A gate that was overridden still FIRED and the call still ran, so it
        # takes 📏; ⛔️ is reserved for a call that was actually stopped.
        line = disclosure_line(r, blocked=blocked_here)
        disclosures.append(line)
        user_lines.append(line)
        user_lines.append("   " + detail_line)
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

    ids = {}
    for r in (r for r in shown if r["id"] not in gate_ids):
        ids.update(log_fires(ctx, [r], hook_phase=mode, mode="advise", excerpt=_excerpt(r),
                             raw_counts=raw, dedup_keys=dedup_keys))
    if gates:      # a blocked call and an overridden one are both delivered gate fires
        ids.update(log_fires(ctx, gates, hook_phase=mode, mode="gate", excerpt=cmd or fp or "",
                             raw_counts=raw, dedup_keys=dedup_keys,
                             override_reasons=overridden))
    for r in cut:   # the per-call cap has a cost; make it visible, never silent
        log_fires(ctx, [r], hook_phase=mode, mode="suppressed", excerpt=_excerpt(r),
                  raw_counts=raw, dedup_keys=dedup_keys)
    for r in shown:
        st["raw"][r["id"]] = 0
        if r.get("on") == "ordering" and session_scoped(r) and ids.get(r["id"]):
            st.setdefault("armed_fire", {})[r["id"]] = ids[r["id"]]
        elif r.get("on") == "ordering" and ordering and ids.get(r["id"]):
            ordering.mark_fired(r["id"], ids[r["id"]])
        elif r.get("converted_rx") or (r.get("on") == "edit" and "content_rx" in r):
            st["open"][r["id"]] = ids.get(r["id"])
            if r.get("on") == "edit":
                ev = fired_on.get(r["id"])
                st.setdefault("open_file", {})[r["id"]] = ev["fp"] if ev else fp
    save_state(sp, st, before=before)
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
