#!/usr/bin/env python3
"""The Rulebook's `reply` lane: a team rule over what the agent SAID at the
end of its turn, not over a tool call.

A `matcher.event: "reply"` rule carries

  reply_rx             the claim, matched against the turn's final assistant
                       text (case-insensitive, multiline);
  evidence_command_rx  optional: a shell command that would back the claim.

At Stop the rule fires when the claim matches and NO shell command of this
turn both matched `evidence_command_rx` and succeeded. A command whose result
says it failed (`is_error`, a non-zero `Exit code`, Codex's `exit_code`) is not
evidence; one whose result the transcript does not hold yet (a background
job) is given the benefit of the doubt. Without `evidence_command_rx` the rule
fires on the claim alone.

Text the agent QUOTED is not a claim it made: fenced and indented code
blocks, inline code spans, blockquote lines and quoted spans ("…", “…”, '…') are blanked before
`reply_rx` runs (`claim_text`). So "I did not run `gh pr merge`" or a pasted
log line saying "tests passed" fires nothing.

What a fire does:

  gate    the Stop is blocked (`decision: block`) with the rule's statement as
          the reason, so the agent goes on and either runs the command that
          backs the claim or retracts it. At most once per turn: a Stop with
          `stop_hook_active` (the continuation this lane, or any other Stop
          hook, asked for) evaluates nothing, and a turn already stopped once
          (`reply_blocked` in the session state, keyed on the record that
          opened the turn) gets the rule as advice instead. When the
          transcript does not show where the turn opened (no transcript, or
          its start is past the TAIL_BYTES read), that key does not exist,
          so the gate advises instead of blocking. Either way a host that does
          not set `stop_hook_active` cannot loop.
  advise  the person sees it at once (`systemMessage`), and the model is told
          at the start of the NEXT turn: the note is left under the rulebook
          state dir (`state/reply-<session>.json`) and the UserPromptSubmit
          route `rulebook_reply` (`deliver`, below) hands it over as
          `additionalContext`. A Stop's own `additionalContext` would not do:
          Claude Code continues the agent on it (harness_stop.py, probed on
          2.1.286), which is a gate in all but name.

Every fire is disclosed on both channels and written to the ledger with
`hook_phase: "stop"`, `tool: "Stop"`, exactly as the other lanes' fires are.
The rule judge is NOT asked (`reply` is not in rulebook_hook's `_JUDGED_ON`):
its context describes a tool call about to run, and the "call" here would be
the agent's own prose, which the judge's gate policy never lets lift a gate.
A reply rule is decided by its patterns alone until a judge bench on reply
fires says otherwise.

`rulebook_hook.py` sits at the directory export's per-file size limit, so the
lane lives here and is handed the hook's namespace (`globals()`), as
`rulebook_paths.drain_legacy` is: it reuses the hook's state, dedup, judge,
ledger and disclosure code rather than restating any of it. The transcript
reader below is standalone and reads both Claude Code and Codex transcripts.

Stdlib only. Every path fails open: a broken lane lets the agent stop.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

HOOK_PHASE = "stop"           # what the ledger row says; free text on the server
TOOL = "Stop"
REPLY_CHARS = 20000           # the final text is matched on its last this-many characters
TAIL_BYTES = 8 * 1024 * 1024  # the transcript is read from its end, at most this much
MAX_COMMANDS = 400            # shell calls of one turn kept for the evidence check
EXCERPT_CHARS = 200
PENDING_TTL_S = 6 * 3600      # an advisory not delivered by then is dropped unread
QUOTE_SPAN_CHARS = 300        # a quoted span longer than this is prose, not a quote

# What this lane's block reason opens with. A host that records the reason as
# a plain user message (not Claude Code's `isMeta` feedback record) must not
# have it read as the person starting a new turn.
BLOCK_MARK = "Rulebook (team rule — this reply was stopped)"

# Codex's shell tools, as codex_hook_bridge._SHELL_TOOLS names them.
SHELL_TOOLS = frozenset({"Bash", "shell", "local_shell", "exec_command", "shell_command"})


# ── what counts as a claim ──────────────────────────────────────────────────

_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]{0,3}\1[ \t]*$|\Z)", re.M | re.S)
_INLINE_CODE = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)", re.S)
_BLOCKQUOTE = re.compile(r"^[ \t]*>.*$", re.M)
_INDENTED = re.compile(r"(?:    |\t)")
_LIST_ITEM = re.compile(r"[ \t]*(?:[-*+]|\d{1,9}[.)])[ \t]")


def _blank_indented(text):
    """CommonMark's indented code block: lines indented four spaces (or a
    tab) that follow a blank line, with the blank lines between them. Not
    when the paragraph above is a list item: there an indented paragraph is
    the item's continuation, which is prose. A line indented without a blank
    line before it is a lazy continuation of the paragraph, also prose."""
    out, in_code, prev_blank, in_list = [], False, True, False
    for ln in text.split("\n"):
        if not ln.strip():
            out.append(ln)
            prev_blank = True
            continue
        if _INDENTED.match(ln) and (in_code or (prev_blank and not in_list)):
            out.append(" ")
            in_code, prev_blank = True, False
            continue
        in_code = False
        if not _INDENTED.match(ln):
            in_list = bool(_LIST_ITEM.match(ln))
        out.append(ln)
        prev_blank = False
    return "\n".join(out)


_QUOTED = re.compile(
    r'"[^"\n]{1,%d}"|“[^”\n]{1,%d}”|(?<![\w\'])\'[^\'\n]{1,%d}\'(?![\w])'
    % (QUOTE_SPAN_CHARS, QUOTE_SPAN_CHARS, QUOTE_SPAN_CHARS))


def claim_text(reply):
    """The reply with everything the agent QUOTED blanked: fenced blocks
    (an unclosed fence runs to the end), indented code blocks, inline code,
    blockquote lines and quoted spans. Each is replaced by one space, so the
    words on either side never join into a match neither of them was. Only
    the tail is kept."""
    text = _FENCE.sub(" ", str(reply or "")[-REPLY_CHARS:])
    text = _blank_indented(text)
    for rx in (_BLOCKQUOTE, _INLINE_CODE, _QUOTED):
        text = rx.sub(" ", text)
    return text


def claim(rule, text):
    """The match object of the rule's claim pattern in `text`, or None."""
    try:
        return re.search(rule["rx"], text, re.I | re.M)
    except (re.error, KeyError, TypeError):
        return None


def excerpt(text, m):
    """The line the claim is on, one line, at most EXCERPT_CHARS around it."""
    start = text.rfind("\n", 0, m.start()) + 1
    end = text.find("\n", m.end())
    line = " ".join(text[start:end if end >= 0 else len(text)].split())
    if len(line) <= EXCERPT_CHARS:
        return line
    at = max(0, line.find(m.group(0)) - EXCERPT_CHARS // 2)
    return line[at:at + EXCERPT_CHARS]


def backed(rule, commands, executes, shell_only=None):
    """Did a shell command of this turn back the claim? True when one matched
    the rule's evidence pattern and did not fail. `ok` is True, False, or None
    when the transcript holds no result for the call (given the benefit of
    the doubt). No evidence pattern means nothing can back the claim.

    The pattern is matched the way an ordering rule's receipt is (`executes`:
    quoted spans, comments and leading env assignments are not the command;
    `shell_only`: a heredoc body is not either), so `echo "pytest"` is not a
    test run. Case-sensitive, as `required_command_rx` is. An UNQUOTED mention
    (`echo pytest`) still counts — the receipt's own accepted residual."""
    rx = rule.get("evidence_rx")
    if not rx:
        return False
    for c in commands:
        if c.get("ok") is False:
            continue
        cmd = str(c.get("cmd") or "")
        try:
            target = shell_only(cmd) if shell_only else cmd
            if executes(target, rx):
                return True
        except Exception:
            continue
    return False


def fires(rule, reply, commands, executes, shell_only=None):
    """(the claim's match or None) — None also when the claim was backed.
    The whole matcher, without `given` and dedup: what the verifier runs."""
    text = claim_text(reply)
    m = claim(rule, text)
    if not m or backed(rule, commands, executes, shell_only):
        return None
    return m


# ── the turn, from the transcript ───────────────────────────────────────────

def _hx():
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import harness_extract  # noqa: PLC0415 — beside this file; owns "a person's message"
    return harness_extract


def _codex_clean(text):
    """Is this Codex user message the person's? `readers.codex.clean_user_text`
    owns the answer; a broken import falls back to its two loudest cases."""
    try:
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        from readers.codex import clean_user_text  # noqa: PLC0415
        return bool(clean_user_text(text))
    except Exception:
        t = text.lstrip()
        return bool(t) and not t.startswith(("<", "# AGENTS.md"))


# Codex writes a shell result's status as a header ABOVE the output
# (`Exit code: 1` / `Process exited with code 1`, then `Output:`), as JSON
# with `metadata.exit_code`, or as `<tool> failed: …` when the call never ran.
# Only the header is read: a command's own output saying "exit code 1" (a test
# named for it, a log line) says nothing about how the command exited.
_CODEX_EXIT = re.compile(r"^(?:Exit code:?|Process exited with code)[ \t]*(-?\d+)[ \t]*$", re.M)
_CODEX_FAILED = re.compile(r"\A\s*\w+ failed:")
_CODEX_HEADER_CHARS = 1000


def _codex_ok(body):
    """True/False from a Codex tool result's status header or metadata, None
    when it states no exit (a session still running)."""
    if isinstance(body, list):
        text = "\n".join(str(b.get("text") or "") for b in body if isinstance(b, dict))
    elif isinstance(body, dict):
        text = json.dumps(body)
    else:
        text = str(body or "")
    try:
        doc = json.loads(text) if text.lstrip().startswith("{") else None
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else doc
        code = meta.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool):
            return code == 0
        return None
    if _CODEX_FAILED.match(text):
        return False
    at = text.find("Output:")
    header = text[:at] if 0 <= at <= _CODEX_HEADER_CHARS else text[:_CODEX_HEADER_CHARS]
    m = _CODEX_EXIT.search(header)
    return int(m.group(1)) == 0 if m else None


def _codex_command(args):
    a = args if isinstance(args, dict) else {}
    cmd = a.get("command", a.get("cmd", ""))
    if isinstance(cmd, list) and all(isinstance(p, str) for p in cmd):
        if len(cmd) == 3 and os.path.basename(cmd[0]) in ("bash", "sh", "zsh", "dash") \
                and cmd[1] in ("-c", "-lc", "-cl"):
            return cmd[2]
        return " ".join(cmd)
    return cmd if isinstance(cmd, str) else ""


# A user-role record Claude Code writes when IT opens a turn rather than a
# person typing one: a slash command (a /loop tick is one), a background
# task's notification, a CI-monitor event, another session's message. Anchored
# at the start, as rulebook_hook.harness_prompt is. Such a record also arrives
# MID-turn (queued while the agent was between tool calls: 76 of 366 task
# notifications in local transcripts, 2026-10-16), so it opens a turn only
# when the record before it shows the agent idle (`_opens_turn`).
_TURN_WRAPPER = re.compile(
    r"\s*(?:<(?:command-message|command-name|task-notification|ci-monitor-event)>"
    r"|Another Claude session sent a message:)")


def _opens_turn(rec):
    """Given the conversational record just BEFORE a wrapper record: True when
    the agent was idle there (the wrapper opened a turn), False when it was
    mid-turn (a tool call or result is open), None when this record says
    neither (a meta record — keep looking)."""
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    content = msg.get("content")
    if rec.get("type") == "assistant":
        blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        return not blocks or blocks[-1].get("type") not in ("tool_use", "thinking",
                                                             "redacted_thinking")
    if rec.get("type") != "user":
        return None
    if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    return None if rec.get("isMeta") else True


def _rec_id(raw):
    return hashlib.sha1(raw).hexdigest()[:16]


def _lines(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.readline()
        return f.read().splitlines()


def read_turn(path):
    """`{"found", "turn_id", "reply", "commands": [{"cmd", "ok"}]}` for the
    turn the transcript at `path` ends with.

    `reply` is the assistant text AFTER the turn's last tool call or result —
    what the person is left reading. `commands` are the turn's shell calls,
    oldest first, each with `ok` from its result (None without one). The turn
    starts at the record that opened it — the person's message
    (`harness_extract`'s statement of one, for Claude Code; `readers.codex`'s
    for a Codex rollout) or a turn-opening wrapper the agent was idle for
    (`_TURN_WRAPPER`) — or at a blocked Stop's feedback, whichever is later.
    `found` is False when no start was seen. `turn_id` names the record that
    opened the turn (a hash of it), found by walking on past a blocked Stop:
    the lane's own guard that one turn is stopped once at most, whatever the
    host says about `stop_hook_active`. It is "" when that record is not in
    the part of the transcript read. Never raises."""
    out = {"found": False, "turn_id": "", "reply": "", "commands": []}
    if not path:
        return out
    try:
        lines = _lines(str(path))
        hx = _hx()
    except Exception:
        return out
    tail, results, calls = [], {}, []
    open_tail = True          # walking back: no tool call or result seen yet
    frozen = False            # past a blocked Stop: only the person's message is still wanted
    wrapper = None            # a turn-opening wrapper, until the record before it says idle or not
    for raw in reversed(lines):
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        kind = rec.get("type")
        if wrapper is not None:
            idle = _opens_turn(rec)
            if idle is None:
                continue
            if idle:
                break                                     # the wrapper opened this turn
            wrapper = None                                # it arrived mid-turn: walk on
        if kind == "response_item":                       # a Codex rollout
            pl = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            pt = pl.get("type")
            if pt == "message":
                text = "\n".join(str(b.get("text") or "") for b in (pl.get("content") or [])
                                 if isinstance(b, dict))
                if pl.get("role") == "assistant" and open_tail and not frozen and text.strip():
                    tail.append(text)
                elif pl.get("role") == "user" and BLOCK_MARK in text:
                    out["found"] = frozen = True          # this lane's own block reason
                elif pl.get("role") == "user" and _codex_clean(text):
                    out["found"], out["turn_id"] = True, _rec_id(raw)
                    break
            elif frozen:
                continue
            elif pt in ("function_call", "custom_tool_call"):
                open_tail = False
                if pl.get("name") in SHELL_TOOLS and len(calls) < MAX_COMMANDS:
                    args = pl.get("arguments", pl.get("input"))
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {"command": args}
                    calls.append((str(pl.get("call_id") or ""), _codex_command(args)))
            elif pt in ("function_call_output", "custom_tool_call_output"):
                open_tail = False
                results[str(pl.get("call_id") or "")] = _codex_ok(pl.get("output"))
            continue
        if kind == "attachment":
            att = rec.get("attachment")
            if isinstance(att, dict) and att.get("type") == "hook_additional_context" \
                    and att.get("hookEvent") == "Stop":
                out["found"] = frozen = True              # a Stop hook continued the agent here
            continue
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        content = msg.get("content")
        if kind == "assistant" and isinstance(content, list):
            if frozen:
                continue
            for b in reversed(content):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and open_tail and str(b.get("text") or "").strip():
                    tail.append(str(b["text"]))
                elif b.get("type") == "tool_use":
                    open_tail = False
                    if b.get("name") in SHELL_TOOLS and len(calls) < MAX_COMMANDS:
                        calls.append((str(b.get("id") or ""), _codex_command(b.get("input"))))
            continue
        if kind != "user":
            continue
        if isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            if frozen:
                continue
            open_tail = False
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    # Claude Code marks every failed Bash result `is_error:
                    # true` (69 of 69 `Exit code N` results in local
                    # transcripts, 2026-10-16); the output text is never read,
                    # so a test named "returns exit code 1" stays green.
                    results[str(b.get("tool_use_id") or "")] = b.get("is_error") is not True
            continue
        whole = hx._text_of(content)
        if not rec.get("isMeta") and _TURN_WRAPPER.match(whole):
            wrapper = raw
            continue
        text = hx._SYS_BLOCK.sub("", whole).strip()
        if rec.get("isMeta"):
            if text.startswith(hx.STOP_FEEDBACK_PREFIX):
                out["found"] = frozen = True              # a blocked Stop: the turn restarts here
            continue
        if hx.is_harness_text(text):
            continue
        if BLOCK_MARK in text[:400]:
            out["found"] = frozen = True                  # this lane's own block reason
            continue
        out["found"], out["turn_id"] = True, _rec_id(raw)
        break
    if wrapper is not None:       # it opened the turn (or the file): it is the start
        out["found"], out["turn_id"] = True, _rec_id(wrapper)
    out["reply"] = "\n".join(reversed(tail))[-REPLY_CHARS:]
    out["commands"] = [{"cmd": c, "ok": results.get(cid)} for cid, c in reversed(calls)]
    return out


# ── the pending advisory ────────────────────────────────────────────────────

def _safe(session):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(session or ""))[:80] or "nosession"


def pending_path(state_dir, session):
    return os.path.join(state_dir, f"reply-{_safe(session)}.json")


def leave_note(state_dir, session, text, now=None):
    """The advisory for the next turn. One per session; a later Stop's note
    replaces an undelivered one (it already says everything still owed)."""
    path = pending_path(state_dir, session)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"session": str(session), "at": now or time.time(), "text": text}, f)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def take_note(state_dir, session, now=None):
    """The note left for `session`, removed as it is read; "" if none, or too old."""
    path = pending_path(state_dir, session)
    claimed = f"{path}.{os.getpid()}.taking"
    try:
        os.replace(path, claimed)      # one reader wins; a second finds nothing
    except OSError:
        return ""
    try:
        with open(claimed, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = None
    finally:
        try:
            os.unlink(claimed)
        except OSError:
            pass
    if not isinstance(doc, dict) or doc.get("session") != str(session):
        return ""
    at = doc.get("at")
    if not isinstance(at, (int, float)) or (now or time.time()) - at > PENDING_TTL_S:
        return ""
    text = doc.get("text")
    return text if isinstance(text, str) else ""


def deliver(data, state_dir):
    """UserPromptSubmit: hand the model the advisory a reply rule left at the
    last Stop. Prints nothing when there is none."""
    session = str((data or {}).get("session_id") or "")
    if not session:
        return 0
    text = take_note(state_dir, session)
    if text:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": text}}))
    return 0


# ── the lane ────────────────────────────────────────────────────────────────

def lane(h, rules, *, repo, root, gitdir, branch, session, ctx, data):
    """Stop: fire the `reply` rules the turn's final text matches. `h` is
    rulebook_hook's namespace. Returns the hook's exit status (always 0)."""
    if data.get("stop_hook_active") or str(data.get("agent_id") or "").strip():
        return 0          # never block twice in a row; a subagent's Stop is not the turn's end
    live = [r for r in rules if r.get("on") == "reply" and r.get("status", "active") == "active"
            and h["scope_ok"](r, repo, gitdir) and h["path_in_scope"](r, "", root)]
    if not live:
        return 0
    tp = data.get("transcript_path")
    turn = read_turn(tp)
    reply = data.get("last_assistant_message")
    if not (isinstance(reply, str) and reply.strip()):
        reply = turn["reply"]
    text = claim_text(reply)
    if not text.strip():
        return 0
    ctx = dict(ctx, tool=TOOL)
    sp = h["state_path"](session)
    st = h["load_state"](sp)
    before = h["snapshot_arming"](st)
    probes = h["Probes"](root, branch, transcript_path=tp, agent_id=ctx["agent_id"])
    marks = {"fired": list(st["fired"]), "counts": dict(st["counts"]),
             "spec_pending": dict(st["spec_pending"])}
    fired, keys, hits = [], {}, {}
    for r in live:
        rid = r["id"]
        gate = r.get("mode") == "gate" and not r.get("_degraded")
        scope = "call" if gate else r.get("fire_scope", "session")   # a gate is never deduped
        key = rid if not str(scope).startswith("branch") else f"{rid}:{branch}"
        m = claim(r, text)
        matched = bool(m) and not backed(r, turn["commands"], h["executes"], h["shell_only"]) \
            and h["given_ok"](r, probes)
        if scope != "call" and key in st["fired"]:
            if matched:
                st["raw"][rid] = st["raw"].get(rid, 0) + 1
            continue
        if not matched:
            continue
        st["raw"][rid] = st["raw"].get(rid, 0) + 1
        if scope != "call":
            st["fired"].append(key)
        keys[rid], hits[rid] = key, excerpt(text, m)
        fired.append(r)
    if not fired:
        h["save_state"](sp, st, before=before)
        return 0
    fired_at = h["_now"]()
    raw = {r["id"]: st["raw"].get(r["id"]) for r in fired}
    judged = None         # not judged: see the module docstring
    fired = sorted(fired, key=h["book_rank"])
    gates = [r for r in fired if r.get("mode") == "gate" and not r.get("_degraded")]
    unkeyed = bool(gates) and not turn["turn_id"]
    if unkeyed or (gates and st.get("reply_blocked") == turn["turn_id"]):
        gates = []        # stopped once already, or no key to hold it to once: it advises
    if gates:
        st["reply_blocked"] = turn["turn_id"]
    shown = (gates + [r for r in fired if r not in gates])[:max(h["MAX_ADVISE"], len(gates))]
    cut = [r for r in fired if r not in shown]
    brand = h["BRAND"]
    lines, user_lines, disclosures = [], [], []
    for r in shown:
        label = r.get("_label") or r["id"]
        blocked = r in gates
        said_line = f"  _(your reply: \"{hits[r['id']]}\")_"
        if blocked:
            lines.append(f"- **BLOCKED [{label}]** {r['text']}{h['_why'](r)}")
        else:
            lines.append(f"- **[{label}]** {r['text']}{h['_why'](r)}")
        lines.append(said_line)
        if unkeyed and r.get("mode") == "gate":
            lines.append("  _(advice only — where this turn began is not in the transcript, "
                         "so a block could not be held to once per turn.)_")
        stale_key = f"_degraded:{r['id']}"
        if r.get("_degraded") and stale_key not in st["fired"]:
            st["fired"].append(stale_key)
            lines.append(f"  _(advice only — {r['_degraded']}. Update the {brand} plugin "
                         f"to let this rule gate.)_")
        line = h["disclosure_line"](r, blocked=blocked)
        disclosures.append(line)
        user_lines.extend([line, f"   {brand} ▸ [{label}] {r['text']}"])
    if gates:
        head = f"## {brand} {BLOCK_MARK}"
        tail = ("Your last reply makes a claim no command in this turn shows. Before you "
                "finish, either run the command that shows it and report what it printed, "
                "or say plainly that it is not confirmed. This turn will not be stopped again.")
    else:
        head = f"## {brand} Rulebook (team rules — about your last reply; advisory, not blocking)"
        tail = ("Your last reply made a claim no command in that turn showed. If it still "
                "matters, confirm it with the command or correct it for the person.")
    # A block is disclosed to the person and echoed; an advisory fire is shown
    # to nobody and goes to the agent alone, as on the tool lanes (CREDIT_NOTE).
    body = "\n".join([head, *lines, tail,
                      h["disclosure_instruction"](disclosures) if gates else h["CREDIT_NOTE"]])
    try:
        if gates:
            print(json.dumps({"decision": "block", "reason": body,
                              "systemMessage": "\n".join(user_lines)}))
        else:
            # The hosts with a delivering UserPromptSubmit route (Codex's bridge
            # has one from 0.138; an older install's note just goes unread).
            if ctx.get("host") in (None, "claude", "codex"):
                leave_note(os.path.dirname(sp), session, body)
    except Exception:
        pass
    for r in shown:
        h["log_fires"](ctx, [r], hook_phase=HOOK_PHASE, mode="gate" if r in gates else "advise",
                       excerpt=hits[r["id"]], raw_counts=raw, dedup_keys=keys,
                       fired_at=fired_at, judge=judged)
        st["raw"][r["id"]] = 0
    for r in cut:
        h["log_fires"](ctx, [r], hook_phase=HOOK_PHASE, mode="suppressed", excerpt="",
                       raw_counts=raw, dedup_keys=keys, fired_at=fired_at, judge=judged)
        h["unfire"](st, r, keys.get(r["id"]), marks, reset_raw=True)   # cut, not spent
    h["save_state"](sp, st, before=before)
    return 0


def main(argv=None):
    """`rulebook_reply.py deliver` — the UserPromptSubmit route. The Stop half
    runs inside rulebook_hook.py (`reply` mode), which owns the book."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["deliver"]:
        return 0
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return 0
    try:
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        import rulebook_paths  # noqa: PLC0415 — beside this file
        state_dir = os.path.join(rulebook_paths.base(), "state")
    except Exception:
        return 0
    return deliver(data if isinstance(data, dict) else {}, state_dir)


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException:
        rc = 0
    sys.exit(rc or 0)
