#!/usr/bin/env python3
"""Quarantine: a gate the post lane could only advise, enforced at the next door.

A gate-mode `edit` rule refuses a Write, an Edit, or a Bash command whose
target it can read before the command runs. A file a command writes in a way
the command line does not show (`python fix.py`, a `write_text()` in a
heredoc, a code generator) is only found AFTER the write, by the post lane's
disk scan (`bash_written_files`), and there is nothing left to refuse — so
that fire used to be an advisory and nothing more.

Now it also opens a VIOLATION: (rule id, file) recorded in the session's
state file (`quarantine`, one of the delta-merged obligation keys beside
`armed` and `spec_pending`, so concurrent hooks of one session never drop or
resurrect one). While a violation is open:

  * a Bash command that runs `git commit`, `git push` or `gh pr create` in the
    violating file's checkout is BLOCKED. The block is an ordinary gate fire of
    the violated rule, injected into the pre lane's fire pass after the judge
    (`on_call`), so it is disclosed, deduped, overridden and written to the
    ledger exactly as every other gate is;
  * the agent's Stop is BLOCKED (`decision: block`) once per violation per
    turn — never when `stop_hook_active` (the continuation a Stop block itself
    asked for), and never twice for one violation in one turn — and ADVISED
    after that (`stop`, this file's own `__main__`). A turn is the host's
    `turn_id` (Codex) or the person's latest prompt in the transcript (Claude
    Code); with neither, at most one block per violation per REBLOCK_S.

A violation CLOSES when the file no longer matches the rule (re-checked when a
later call writes the file, and always before a block is decided), when the
file is deleted or reverted to HEAD, when the rule left the book, was paused
or is no longer a gate, or by the existing override: `RULEBOOK_OVERRIDE='<why>'`
on the blocked command (recorded on the gate's ledger row like any other
override), or `RULEBOOK_OVERRIDE='[<label>] <why>'` on any shell command
(also recorded, as the `dismissed` event the named form always posts).

A re-check reads the file as the post lane read it (`read_edit_body`: a new
file whole, a modified one's lines added against HEAD) and asks `evaluate`.
It does not re-ask a rule's `given` facts; a file it cannot read any more
(binary, too large, a git error) closes the violation — the hook fails open.

`rulebook_hook.py` runs as `__main__` at its export size limit, so it passes
its own namespace in (`h`, its `globals()`); the Stop lane imports it.

    printf %s "$STOP_PAYLOAD" | python3 rulebook_quarantine.py stop [--host codex]

Stdlib only; every path fails open and silent.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

KEY = "quarantine"
FILES_NAMED = 3          # files named on one fire; the rest are counted
TAIL_START = 64 * 1024   # transcript tail read for the turn key, growing ×4…
TAIL_MAX = 1024 * 1024   # …up to this
REBLOCK_S = 600          # no turn key at all: a violation blocks a Stop at most this often
TURN_ID_CHARS = 200

# The doors a violation must not pass. Universal git / GitHub CLI verbs, not
# any one repository's habits: `git [-C dir|-c k=v|--git-dir dir|--flag]…
# commit|push` and `gh pr create`, as the command word of a shell segment:
# after `;`, `&&`, `||`, `|`, `(`, `{`, a backtick, a newline, the shell
# keywords that start a command (`then`, `do`, `else`, `elif`, `if`, `while`,
# `until`, `time`), a `!`, and `VAR=x` assignments. Matched on the command
# with quoted spans blanked (their quotes kept, so `-C "$WT"` is one blanked
# argument), heredoc bodies and comments removed, so `echo "git push"`,
# `grep 'git commit'` and `echo git push` are no doors.
#
# A door that cannot publish anything is no door: `--dry-run` and `--help`/`-h`
# on any of the three, and push's `-n` (commit's `-n` is --no-verify, a real
# commit). Looked for in the rest of that segment only.
#
# KNOWN AND ACCEPTED RESIDUAL (the fail-open side): a door run under a
# wrapper command (`timeout 60 git push`, `sudo git push`, `xargs git push`),
# through `eval`/`sh -c '…'` (a quoted span), or a script that runs git
# itself (`./release.sh`) is not seen. There is no list of wrappers to keep.
_SEG = (r"(?:^|[;&|({`\n])\s*(?:(?:then|do|else|elif|if|while|until|time)\s+)*"
        r"(?:!\s*)?(?:[A-Za-z_]\w*=\S*\s+)*")
_ARG = r"(?:\"[^\"]*\"|'[^']*'|\S+)"
_REST = r"(?=[^;&|\n]*?\s(?:{})(?=[\s;&|)}}]|$))"
_INERT = _REST.format("--dry-run|--help|-h")
_INERT_PUSH = _REST.format("--dry-run|--help|-h|-n")
GATED_RX = (_SEG + r"git(?:\s+-[Cc]\s*" + _ARG + r"|\s+--(?:git-dir|work-tree|namespace)\s+" + _ARG
            + r"|\s+--[\w-]+(?:=\S+)?)*\s+(?:commit(?![\w-])(?!" + _INERT + r")"
            r"|push(?![\w-])(?!" + _INERT_PUSH + r"))"
            r"|" + _SEG + r"gh\s+pr\s+create(?![\w-])(?!" + _INERT + r")")

OPEN_MSG = ("this file is now QUARANTINED — committing, pushing, opening a PR and ending "
            "the turn are blocked until it no longer matches this rule")


def key(rule_id, path):
    return json.dumps([str(rule_id), path])


def violations(st):
    """The session's open violations, `{key: record}` (a malformed value reads as none)."""
    q = st.get(KEY)
    if not isinstance(q, dict):
        q = st[KEY] = {}
    return q


def gated(h, cmd):
    """Does this shell command run a door (commit / push / PR create)?"""
    try:
        return bool(cmd) and h["command_fires"](GATED_RX, h["unquoted"](h["shell_only"](cmd)), flags=0)
    except Exception:
        return False


def _rel(path, root):
    if root and path.startswith(root.rstrip("/") + "/"):
        return os.path.relpath(path, root)
    return path


def recheck(h, rec, repo, root, rules):
    """The rule (as the book has it now) when the file still violates it, else None."""
    path = rec.get("file") or ""
    try:
        if not os.path.isfile(path):
            return None                                  # deleted (or never a file)
        book = h["_file_book"](path, repo, root, rules, False)
        if not book:
            return None
        rule = next((r for r in book[0] if r.get("id") == rec.get("rule_id")), None)
        if not rule or rule.get("status", "active") != "active" or rule.get("mode") != "gate" \
                or rule.get("on") != "edit" or not h["path_in_scope"](rule, book[2], book[1]):
            return None
        body = h["read_edit_body"](path, bool(rec.get("new", True)))
        # None: unreadable now (fail open). "": a modified file with nothing
        # added against HEAD — reverted, or only lines taken away.
        if not body:
            return None
        if not h["evaluate"](rule, hook_phase="pre", tool="Write", file_path=book[2], body=body):
            return None
        return rule
    except Exception:
        return None


def record(st, rule_id, path, root, new):
    """Open (or re-open) one violation; returns its key. The path is kept as
    the post lane spelled it (the worktree's own spelling), so the mod's
    engine writes the same key; comparisons go through `_same_file`. A
    re-open keeps the turn its Stop was already blocked in: writing the file
    again is not a second violation to stop the same turn for."""
    k = key(rule_id, path)
    q = violations(st)
    was = q.get(k) if isinstance(q.get(k), dict) else {}
    q[k] = {"rule_id": str(rule_id), "file": path, "root": root or "",
            "new": bool(new), "stopped": was.get("stopped")}
    if was.get("stopped_at") is not None:
        q[k]["stopped_at"] = was["stopped_at"]
    return k


def _same_file(a, b):
    return a == b or os.path.realpath(a) == os.path.realpath(b)


def open_from_fires(st, fired, fired_on):
    """Open a violation for each post-lane fire of a gate-mode edit rule on a
    file a Bash command already wrote; tag the fire so its line says so.
    Returns the keys opened."""
    opened = []
    for r in fired:
        ev = fired_on.get(r.get("id")) or {}
        if r.get("mode") != "gate" or r.get("on") != "edit" or ev.get("via") != "bash":
            continue
        opened.append(record(st, r["id"], ev.get("fp") or "", ev.get("root"), ev.get("new", True)))
        r["_gate_msg"] = OPEN_MSG
    return opened


def on_call(h, st, *, mode, tool, cmd, events, fired, fired_on, rules, repo, root, cmd_root,
            label, reason):
    """The pre/post lanes' one call (after the judge, before the gates are
    picked). Returns the synthetic gate fires to append to this call's fires:
    one per violated rule, a copy of it as a `bash` gate whose detail names
    the file(s)."""
    q = violations(st)
    if mode == "post":
        opened = set(open_from_fires(st, fired, fired_on))
        touched = [ev["fp"] for ev in events if ev.get("fp") and ev.get("tool") in h["EDIT_TOOLS"]]
        for k, rec in list(q.items()):
            if k not in opened and any(_same_file(rec.get("file") or "", t) for t in touched) \
                    and not recheck(h, rec, repo, root, rules):
                del q[k]
        return []
    if mode != "pre" or tool != "Bash" or not q:
        return []
    door = gated(h, cmd)
    by_rule = {}
    for k, rec in list(q.items()):
        rid = rec.get("rule_id")
        names_it = label is not None and reason and any(
            label == str(x.get(f) or "").lower() for x in rules if x.get("id") == rid
            for f in ("id", "_label"))
        here = door and (not rec.get("root") or h["_same_dir"](rec["root"], cmd_root or root))
        if not (here or names_it):
            continue
        rule = recheck(h, rec, repo, root, rules)
        if rule is None:
            del q[k]
            continue
        names_it = names_it or (label is not None and reason and label in (
            str(rule.get("id")).lower(), str(rule.get("_label") or "").lower()))
        if reason is not None and (names_it or (label is None and here)):
            del q[k]          # the override answers it; the gate below records why
        if here:
            by_rule.setdefault(rid, (rule, []))[1].append(rec)
    out = []
    already = {r.get("id") for r in fired}
    for rid, (rule, recs) in by_rule.items():
        if rid in already:
            continue
        paths = [f"`{_rel(x['file'], x.get('root') or root)}`" for x in recs]
        more = f" and {len(paths) - FILES_NAMED} more" if len(paths) > FILES_NAMED else ""
        out.append(dict(rule, on="bash", _gate_msg=(
            f"{', '.join(paths[:FILES_NAMED])}{more} still violate{'s' if len(paths) == 1 else ''} it "
            f"(written by an earlier shell command, after which the write could not be refused). "
            f"Fix or revert the file before you commit, push or open a PR")))
    return out


# ── Stop ────────────────────────────────────────────────────────────────────


def turn_of(data):
    """What one turn is, for "once per violation per turn": the host's own
    turn id when its Stop payload carries one (Codex: `turn_id`, a required
    field of its stop.command.input schema), else the uuid of the person's
    latest prompt in a Claude Code transcript, else None."""
    tid = data.get("turn_id")
    if isinstance(tid, str) and tid.strip():
        return "turn:" + tid.strip()[:TURN_ID_CHARS]
    return turn_key(data.get("transcript_path"))


def turn_key(transcript_path):
    """The uuid of the person's latest prompt in the transcript, or None
    without one (no transcript, unreadable, or not Claude Code's format)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import rulebook_moments  # noqa: PLC0415 — beside this file
        for rec in rulebook_moments.tail_records(transcript_path, TAIL_START, TAIL_MAX):
            if rec.get("type") != "user" or rec.get("isMeta") or "toolUseResult" in rec:
                continue
            c = (rec.get("message") or {}).get("content")
            if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                continue
            uid = rec.get("uuid")
            if isinstance(uid, str) and uid:
                return uid
    except Exception:
        pass
    return None


def stop(h, data, emit=print):
    """The Stop lane. Re-checks every open violation, then blocks the stop
    (once per violation per turn, never on a stop-hook continuation) or
    advises. Every fire goes to the ledger and to the person's terminal."""
    session = str(data.get("session_id") or "")
    if not session or str(data.get("agent_id") or "").strip():
        return 0                       # a subagent's Stop is not the session's turn
    sp = h["state_path"](session)
    st = h["load_state"](sp)
    if not violations(st):
        return 0
    before = h["snapshot_arming"](st)
    repo, root, _gitdir, branch = h["repo_of_call"](data)
    rules, rule_version, _fetched, _src = h["load_rules"](repo) if repo else ([], "", None, {})
    q = violations(st)
    still = []
    for k, rec in list(q.items()):
        rule = recheck(h, rec, repo, root, rules)
        if rule is None:
            del q[k]
        else:
            still.append((rec, rule))
    if not still:
        h["save_state"](sp, st, before=before)
        return 0
    # Once per violation per turn. With no turn key at all (no turn id, no
    # readable Claude Code transcript) nothing tells one turn from the next,
    # so a violation blocks at most once per REBLOCK_S instead: loop safety
    # never rests on the host sending `stop_hook_active` alone.
    turn = turn_of(data)
    now = time.time()

    def due(rec):
        if turn is not None:
            return rec.get("stopped") != turn
        try:
            return now - float(rec.get("stopped_at") or 0) >= REBLOCK_S
        except (TypeError, ValueError):
            return True

    block = not data.get("stop_hook_active") and any(due(rec) for rec, _ in still)
    by_rule = {}
    for rec, rule in still:
        by_rule.setdefault(rule["id"], (rule, []))[1].append(rec)
        if block:
            rec["stopped"], rec["stopped_at"] = turn, now
    ctx = {"session": session, "agent_id": None, "repo": repo or None, "branch": branch,
           "tool": "Stop", "rule_version": rule_version, "source_message_id": None,
           "worktree": h["worktree_key"](root), "host": h["_host_arg"](), "tool_use_id": None}
    agent, user, disclose = [], [], []
    for rid, (rule, recs) in by_rule.items():
        label = rule.get("_label") or rid
        paths = ", ".join(f"`{_rel(x['file'], x.get('root') or root)}`" for x in recs)
        agent.append(f"- [{label}] {rule.get('text', '')} — still violated by {paths}")
        line = h["disclosure_line"](rule, blocked=block)
        disclose.append(line)
        user += [line, f"   {h['BRAND']} {'⛔' if block else '▸'} quarantined [{label}] {paths}"]
        h["log_fires"](ctx, [rule], hook_phase="stop", mode="gate" if block else "advise",
                       excerpt=f"quarantine {', '.join(x['file'] for x in recs)}")
    h["save_state"](sp, st, before=before)
    named = next(iter(by_rule.values()))[0] if len(by_rule) == 1 else {}
    how = ("Fix each file so it no longer matches its rule, or revert or delete it. If it is a "
           "legitimate exception, say why on your next shell command — "
           f"`RULEBOOK_OVERRIDE='[{named.get('_label') or named.get('id') or '<label>'}] <why>' "
           "<command>` — which records the reason and lifts the quarantine.")
    if block:
        emit(json.dumps({"decision": "block", "systemMessage": "\n".join(user), "reason": (
            f"Not done yet — the {h['BRAND']} team rulebook has files in quarantine. Each was "
            "written by a shell command in a way the rule could only see afterwards:\n"
            + "\n".join(agent) + f"\n{how}" + h["disclosure_instruction"](disclose))}))
    else:
        emit(json.dumps({"systemMessage": "\n".join(user + [
            f"   {h['BRAND']} ▸ still quarantined after this turn: commits, pushes and PRs stay "
            "blocked until these files are fixed"])}))
    return 0


def _maybe_open(session):
    """Cheap pre-check, before the hook module is imported (~30 ms): could
    this session have a violation open? False only when the state file under
    the real base says it has none and no forward-test redirect is in place
    (one may move the session's state to a private base). Any doubt: True."""
    try:
        import rulebook_paths  # noqa: PLC0415 — beside this file
        base = rulebook_paths.base()
        if os.path.exists(os.path.join(base, "pretest-redirect.json")):
            return True
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", session)[:80] or "nosession"
        try:
            with open(os.path.join(base, "state", f"{safe}.json"), encoding="utf-8") as f:
                doc = json.load(f)
        except FileNotFoundError:
            return False
        return not isinstance(doc, dict) or bool(doc.get(KEY))
    except Exception:
        return True


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] != "stop":
        return 0
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    if not isinstance(data, dict):
        return 0
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        if not _maybe_open(str(data.get("session_id") or "")):
            return 0
        import rulebook_hook as rh  # noqa: PLC0415 — beside this file
        rh.set_active_base(str(data.get("cwd") or ""))
        return stop(vars(rh), data)
    except Exception:
        if os.environ.get("MEMHUB_RULEBOOK_DEBUG"):
            import traceback
            traceback.print_exc()
        return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
