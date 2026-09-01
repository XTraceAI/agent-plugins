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
    fact), and only from a book fetched within 24 h — older caches run it as
    `advise` and say so once per session.

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
"""
import fcntl
import hashlib
import datetime as _dt
import json
import os
import re
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
BOOK_MAX_AGE_S = 24 * 3600   # §5.3: a gate from an older cache degrades to advise
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
                return "content_rx" not in rule or bool(re.search(rule["content_rx"], body, re.M))
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
_RESERVED_RULE_KEYS = frozenset({"id", "text", "why", "status", "mode", "_version", "_label", "on", "repo_scope", "_scope_repos", "anchors", "ordering"})


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


def _clean_text(v):
    """Server rule prose is display data, not instructions: one line, no
    control characters, length-capped before it enters the model context."""
    t = re.sub(r"[\x00-\x1f\x7f]+", " ", str(v or ""))
    t = re.sub(r"\s+", " ", t).strip()
    return t[:_TEXT_MAX]


def _why(r):
    """The parenthetical reason — only when the rule carries one separately;
    server statements already end in 'Why: …'."""
    return f"  _(why: {r['why']})_" if r.get("why") else ""


def to_hook_rule(row):
    """One `?view=hook` row → the flat shape evaluate()/OrderingEngine read.
    Rows already in the pilot shape (an `on` key) pass through. Never raises
    on a malformed row: returns None and the row is skipped."""
    try:
        if not isinstance(row, dict):
            return None
        if "on" in row:
            r = dict(row)
            r.setdefault("id", row.get("rule_id"))
            r.setdefault("_version", _version_of(row.get("version")))
            if not r.get("id") or not all(rx_ok(r[k]) for k in _RX_KEYS if k in r):
                return None           # same regex lint as the server shape
            if isinstance(r.get("ordering"), dict) and not all(
                    rx_ok(r["ordering"].get(k)) for k in ("required_command_rx", "gated_command_rx")):
                return None
            return r
        r = {"id": row.get("rule_id") or row.get("id"),
             "text": _clean_text(row.get("statement") or row.get("title")),
             "why": _clean_text(row.get("why")), "status": row.get("status", "active"),
             "_label": _clean_text(row.get("title")) or None,
             "mode": row.get("mode", "advise"), "_version": _version_of(row.get("version"))}
        if not r["id"]:
            return None
        scopes = [str(x) for x in (row.get("scope_repos") or []) if x]
        r["repo_scope"] = "any"
        if scopes:
            r["_scope_repos"] = scopes
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


def effective_mode(rule, fetched_at, now=None):
    """`gate` is honoured only from a book fetched (200 or 304) within the last
    24 h (§5.3); anything else is `advise`. """
    mode = rule.get("mode", "advise")
    if mode != "gate":
        return "advise"
    try:
        ts = datetime.fromisoformat(str(fetched_at))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        return "gate" if (now - ts).total_seconds() <= BOOK_MAX_AGE_S else "advise"
    except Exception:
        return "advise"


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
# answering fired on that segment (found live, e2e 2026-09-01 — the
# start-only anchor left the override unread and the call blocked). A grep
# whose ARGUMENT mentions the variable is still not an override: the token
# after `grep` is not at a segment start. An EMPTY reason is not an override
# either (`RULEBOOK_OVERRIDE= git push --force` stays gated): the reason is
# the whole price of passing a gate, and it crosses the wire, so it is run
# through `redact_secrets` like everything else that leaves the machine.
_OVERRIDE_RX = re.compile(
    r"(?:^|[;&|(]\s*)(?P<assign>RULEBOOK_OVERRIDE=(?:'(?P<a>[^']*)'|\"(?P<b>[^\"]*)\"|(?P<c>\S*))\s+)",
    re.M)


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
    overrode (§5.3). Returns {rule_id: fire_id} so conversions can point back."""
    ids = {}
    try:
        path = os.path.join(_ledger_dir(), "fires.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            for r in rules:
                fid = str(uuid.uuid4())
                ids[r["id"]] = fid
                f.write(json.dumps({
                    "fire_id": fid, "rule_id": r["id"],
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


def session_digest(rules, repo, gitdir, ctx):
    in_scope = [r for r in rules if scope_ok(r, repo, gitdir) and r.get("status", "active") == "active"]
    if not in_scope:
        return
    # Spec §2: at most MAX_POSTURE session rules and ~2k tokens per scope.
    # Deterministic (by title, then id) rather than book order, and every rule
    # past either limit is logged SUPPRESSED so the ledger sees it.
    posture_all = sorted((r for r in in_scope if r.get("on") == "session"),
                         key=lambda r: (str(r.get("_label") or r.get("title") or r["id"]).casefold(), str(r["id"])))
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

    inp = data.get("tool_input") or {}
    sp = state_path(session)
    st = load_state(sp)
    fired_now = []

    cmd = str(inp.get("command", "")) if tool == "Bash" else ""
    override_reason = None            # §5.3: set only by the RULEBOOK_OVERRIDE prefix
    if cmd:
        m = _OVERRIDE_RX.search(cmd)
        reason = (m.group("a") or m.group("b") or m.group("c") or "").strip() if m else ""
        if reason:                    # an empty reason is no override — the gate stands
            override_reason = redact_secrets(reason)[:2000]
            # rules match the command, not the assignment: drop just that token
            cmd = cmd[:m.start("assign")] + cmd[m.end("assign"):]
    fp = str(inp.get("file_path", ""))
    body = str(inp.get("new_string", "")) + str(inp.get("content", "")) + \
        "\n".join(str(e.get("new_string", "")) for e in (inp.get("edits") or []) if isinstance(e, dict))
    rtext = result_text(data.get("tool_response")) if mode == "post" else ""
    resp = data.get("tool_response") if (mode == "post" and tool == "Bash") else None
    ordering = None
    dedup_keys = {}
    by_id = {r["id"]: r for r in rules}

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
        elif mode == "pre" and r.get("on") == "edit" and "content_rx" in r \
                and tool in EDIT_TOOLS and fp == st.get("open_file", {}).get(rid) \
                and not evaluate(r, hook_phase="pre", tool=tool, file_path=fp, body=body):
            log_conversion(fid, "re-edit-clears")
            del st["open"][rid]
            st.get("open_file", {}).pop(rid, None)

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

    for r in rules:
        if r.get("on") in ("session", "anchor") or not scope_ok(r, repo, gitdir) \
                or r.get("status", "active") != "active":   # draft = not armed (§6)
            continue
        rid = r["id"]

        if r.get("on") == "ordering":
            try:
                ordering = ordering or OrderingEngine(root, branch)
                ok = bash_ok(resp, strict=r.get("mode") == "gate") if resp is not None else None
                outcome = ordering.feed(r, hook_phase=mode, tool=tool, cmd=cmd,
                                        file_path=fp, ok=ok)
            except Exception:
                outcome = None
            if outcome == "discharged" and r.get("_converted_fire"):
                log_conversion(r["_converted_fire"], "discharged")
            elif outcome == "fired":
                dedup_keys[rid] = f"{rid}@{root}:{branch}"
                fired_now.append(r)
            continue

        scope = r.get("fire_scope", "session")
        if mode == "pre" and tool == "Bash" and r.get("on") == "bash" \
                and effective_mode(r, fetched_at) == "gate":
            scope = "call"        # a gate blocks EVERY matching call — never deduped (§5.3)
        key = rid if not scope.startswith("branch") else f"{rid}:{branch}"
        if scope != "call" and not scope.startswith("counter") and key in st["fired"]:
            if evaluate(r, hook_phase=mode, tool=tool, cmd=cmd, file_path=fp,
                        body=body, result_text=rtext):
                st["raw"][rid] = st["raw"].get(rid, 0) + 1   # what dedup swallowed
            continue
        if not evaluate(r, hook_phase=mode, tool=tool, cmd=cmd, file_path=fp,
                        body=body, result_text=rtext):
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

    if not fired_now:
        save_state(sp, st)
        return 0

    # §5.3: which of this call's fires are GATES. Only a pre-hook Bash call can
    # be blocked, and only by a rule the fresh book says is a gate.
    gate_ids = {r["id"] for r in fired_now
                if mode == "pre" and tool == "Bash" and r.get("on") in ("bash", "ordering")
                and effective_mode(r, fetched_at) == "gate"}
    gates = [r for r in fired_now if r["id"] in gate_ids]
    advisories = [r for r in fired_now if r["id"] not in gate_ids]
    # the advisory cap never cuts a gate — a silently un-gated push is the one
    # failure a gate exists to prevent
    shown, cut = gates + advisories[:MAX_ADVISE], advisories[MAX_ADVISE:]
    blocked = bool(gates) and override_reason is None
    lines = [f"## {BRAND} Rulebook — BLOCKED" if blocked
             else f"## {BRAND} Rulebook (team rules — advisory, not blocking)"]
    user_lines, deny_lines = [], []
    for r in shown:
        label = r.get("_label") or r["id"]
        detail = f" — {r['_gate_msg']}" if r.get("_gate_msg") else ""
        if r["id"] not in gate_ids:
            lines.append(f"- **[{label}]** {r['text']}{detail}{_why(r)}")
            user_lines.append(f"{BRAND} ▸ [{label}] {r['text']}{detail}")
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
    # §5.3: a gate from a stale book runs as advise and says so once per session.
    degraded = [r["id"] for r in shown if r.get("mode") == "gate"
                and effective_mode(r, fetched_at) == "advise"]
    if degraded and not st.get("degrade_noted"):
        st["degrade_noted"] = True
        lines.append("- _(team rulebook last refreshed >24 h ago — gate rules "
                     "run as advisories until the next successful fetch)_")
    try:
        emit("PreToolUse" if mode == "pre" else "PostToolUse", "\n".join(lines),
             user_line="\n".join(user_lines), deny=deny)
    except Exception:
        pass
    raw = {r["id"]: st["raw"].get(r["id"]) for r in fired_now}
    excerpt = cmd or fp or ""
    ids = log_fires(ctx, [r for r in shown if r["id"] not in gate_ids], hook_phase=mode,
                    mode="advise", excerpt=excerpt, raw_counts=raw, dedup_keys=dedup_keys)
    if gates:      # a blocked call and an overridden one are both delivered gate fires
        ids.update(log_fires(ctx, gates, hook_phase=mode, mode="gate", excerpt=excerpt,
                             raw_counts=raw, dedup_keys=dedup_keys,
                             override_reason=override_reason))
    if cut:   # the per-call cap has a cost; make it visible, never silent
        log_fires(ctx, cut, hook_phase=mode, mode="suppressed", excerpt=excerpt,
                  raw_counts=raw, dedup_keys=dedup_keys)
    for r in shown:
        st["raw"][r["id"]] = 0
        if r.get("on") == "ordering" and ordering and ids.get(r["id"]):
            ordering.mark_fired(r["id"], ids[r["id"]])
        elif r.get("converted_rx") or (r.get("on") == "edit" and "content_rx" in r):
            st["open"][r["id"]] = ids.get(r["id"])
            if r.get("on") == "edit":
                st.setdefault("open_file", {})[r["id"]] = fp
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
