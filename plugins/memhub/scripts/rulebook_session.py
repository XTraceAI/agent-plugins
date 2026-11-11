#!/usr/bin/env python3
"""`given.session`: conditions on the agent session itself, not the repo.

A matcher rule of any engine may carry

    "given": {"session": {"context_tokens_gt": 300000,   # context the model saw
                          "idle_seconds_gt": 300,        # quiet before this response
                          "turn_tool_calls_gt": 40,      # tool calls this turn
                          "repeat_count_gt": 2,          # this rule matched > 2 times
                          "repeat_key": "command"}}      # ...for the same command

and fires only when every condition holds. Kept out of `rulebook_hook.py`,
which sits at the export's per-file size limit; the hook reaches this through
`given_ok` and the two `Probes` it builds, nothing else.

The facts, and where each comes from
------------------------------------
* `context_tokens` — the newest assistant message's `usage`:
  input + cache_creation + cache_read tokens, i.e. what the model was sent on
  that call. Claude Code writes one transcript record per content block and
  repeats the SAME usage on each, so a message is taken once by `message.id`
  (never summed). Synthetic messages (`model: "<synthetic>"`, an API error
  the client wrote itself) were never sent to a model and are skipped.
* `idle_seconds` — the quiet before this response: the first record of the
  newest assistant message minus the last record of the one before it — the
  gap a prompt cache sees between two calls. At a prompt (nothing answered
  yet), or when the call being checked is not yet in the transcript, it is
  now minus the newest assistant message.
* `turn_tool_calls` — distinct `tool_use` ids since the last prompt the person
  typed (tool results and injected meta rows are not prompts), plus the call
  being checked if the transcript does not carry it yet. Zero at a prompt.
  When the turn began before the part of the transcript read here (see the
  tail below), the count is a LOWER bound, which can only make a `_gt`
  condition fail, never fire wrongly.
* `repeat_count` — how many tool calls (or prompts) this rule has matched in
  this session (its pattern and every other `given` condition), this one
  included, per `repeat_key`: `rule` (default, any match), `command` (the
  same Bash command, whitespace-normalised) or `file` (the same file path).
  One call counts once: a Bash write is judged by the pre lane and again by
  the post lane's disk scan, and both carry the call's `tool_use_id`, so the
  second look reads the count without adding to it. A call with no
  `tool_use_id` counts every look. Kept in local state; never read from the
  transcript.

Whose transcript: a subagent's call carries `agent_id`, and Claude Code sends
the PARENT session's file as its `transcript_path` (rulebook_hook
`agent_id_of`). The parent's context, idleness and tool calls say nothing
about the subagent, so the subagent's own file is used —
`agent_transcript_path` when the payload has one, a `transcript_path` already
under `/subagents/`, or `<dir of transcript_path>/<session>/subagents/
agent-<agent_id>.jsonl`, where Claude Code writes it (checked on disk
2026-10-16: the hook's `agent_id` is that file's name). None of them on disk:
the three transcript facts are unknown for that call. `repeat_count` reads no
transcript and is kept per session, subagents included.

The mod (in-process lanes) has no transcript path; it asks this module through
`rulebook_mod_state.py` (`mod_holds`), which finds the session's file under
the host's config dir (`find_transcript`) and answers exactly as the hook.

Speed: the transcript is never read whole. The first look reads its last
TAIL_BYTES; after that only the bytes appended since, from an offset cached
per session in `<state>/<session>.session-facts.json`. A rotated or shrunk
file starts over from the tail.

Hosts: Claude Code transcripts carry all three transcript facts. A Codex
rollout carries `token_count` events and tool calls, so `context_tokens`
(Codex's `last_token_usage.input_tokens`, which already includes cached
input) and `turn_tool_calls` (from the person's prompt — a `user_message` event,
or on 0.163+ the prompt's own `user` message — to now) are read from it;
it has no per-response id to measure idleness between, so `idle_seconds` is
unknown there. A Cursor transcript carries no usage and no timestamps: every
transcript fact is unknown. An unknown fact satisfies no condition, so the
rule does not fire. `repeat_count` needs no transcript and works everywhere.

Stdlib only; every function fails closed (unknown → condition false).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import datetime

KEYS = ("context_tokens_gt", "idle_seconds_gt", "turn_tool_calls_gt",
        "repeat_count_gt", "repeat_key")
REPEAT_KEYS = ("rule", "command", "file")
# Upper bounds, the same the server validates (validation.py SESSION_GIVEN_MAX).
MAX = {"context_tokens_gt": 10_000_000, "idle_seconds_gt": 30 * 86400,
       "turn_tool_calls_gt": 100_000, "repeat_count_gt": 100_000}

TAIL_BYTES = 1 << 20        # the first look at a transcript reads this much of its end
INCR_MAX = 16 << 20         # more appended than this since the last look: re-read the tail
REPEAT_CAP = 2000           # repeat counters kept per session
_TOOLS_CAP = 5000           # tool ids remembered for the current turn
_AGENT_TX_CAP = 8           # subagent transcripts whose read offsets are cached per session
_ID_RX = re.compile(r"[A-Za-z0-9_-]{1,80}")

_SKIP_USER = (b'"tool_result"', b'"toolUseResult"')
_WANT = (b'"assistant"', b'"user"', b'"payload"')
_CODEX_TOOL_ITEMS = ("function_call", "custom_tool_call", "local_shell_call")


def kind_ok(key, value):
    """Is `value` well-formed for `session.<key>`? The hook's `given_norm`
    already checked the int kinds; this adds the bounds and the enum."""
    if key == "repeat_key":
        return value in REPEAT_KEYS
    hi = MAX.get(key)
    return (hi is not None and isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= hi)


# ── local state ─────────────────────────────────────────────────────────────


def facts_path(base, session):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session or ""))[:80] or "nosession"
    return os.path.join(base, "state", f"{safe}.session-facts.json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except Exception:
        return {}


def _save(path, doc):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path),
                                   prefix="." + os.path.basename(path) + ".")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
    except Exception:
        pass


# ── the transcript ──────────────────────────────────────────────────────────


def _ts(raw):
    try:
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return d.timestamp() if d.tzinfo is not None else None
    except Exception:
        return None


def _real_prompt(rec):
    """A `user` record the person typed (the rule `user_turns_of` uses)."""
    if rec.get("isMeta") or rec.get("isCompactSummary") or "toolUseResult" in rec:
        return False
    c = (rec.get("message") or {}).get("content")
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return False
        return any(isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
                   for b in c)
    return False


def _usage_tokens(u):
    if not isinstance(u, dict):
        return None
    n = 0
    for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        v = u.get(k, 0)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return None
        n += v
    return n


def _fresh():
    return {"fmt": None, "cur": None, "prev": None, "tools": [], "turn_seen": False}


def _add_tool(tx, tid):
    if isinstance(tid, str) and tid and tid not in tx["tools"] and len(tx["tools"]) < _TOOLS_CAP:
        tx["tools"].append(tid)


def _claude(tx, rec):
    t = rec.get("type")
    if t == "user":
        if _real_prompt(rec):
            tx["fmt"] = "claude"
            tx["tools"], tx["turn_seen"] = [], True
        return
    if t != "assistant":
        return
    msg = rec.get("message")
    if not isinstance(msg, dict) or msg.get("model") == "<synthetic>":
        return
    tx["fmt"] = "claude"
    ts = _ts(rec.get("timestamp"))
    mid = msg.get("id") or rec.get("requestId") or rec.get("uuid")
    cur = tx["cur"]
    if cur is None or cur.get("id") != mid:
        if cur is not None:
            tx["prev"] = {"id": cur.get("id"), "last": cur.get("last")}
        cur = tx["cur"] = {"id": mid, "first": ts, "last": ts, "ctx": None}
    elif ts is not None:
        cur["last"] = ts
    # Repeated on every record of one message: taken, never added.
    tok = _usage_tokens(msg.get("usage"))
    if tok is not None:
        cur["ctx"] = tok
    for b in msg.get("content") or []:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            _add_tool(tx, b.get("id"))


def _codex(tx, rec):
    p = rec.get("payload")
    if not isinstance(p, dict):
        return
    kind, pt = rec.get("type"), p.get("type")
    if (kind == "event_msg" and pt == "user_message") or (
            kind == "response_item" and pt == "message" and p.get("role") == "user"
            and _prompt(p)):
        # 0.163 writes no `user_message` event; the prompt's own `user`
        # message (less injected context) opens the turn on every version.
        tx["fmt"] = "codex"
        tx["tools"], tx["turn_seen"] = [], True
    elif kind == "event_msg" and pt == "token_count":
        info = p.get("info")
        last = info.get("last_token_usage") if isinstance(info, dict) else None
        v = last.get("input_tokens") if isinstance(last, dict) else None
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            tx["fmt"] = "codex"
            # Codex's input_tokens already counts cached input; no id to
            # measure idleness between, so `first`/`last` stay unknown.
            tx["cur"] = {"id": None, "first": None, "last": None, "ctx": v}
    elif kind == "response_item" and pt in _CODEX_TOOL_ITEMS:
        tx["fmt"] = "codex"
        _add_tool(tx, p.get("call_id") or p.get("id"))


def _prompt(p):
    try:
        import rulebook_moments  # noqa: PLC0415 — beside this file
        return rulebook_moments.codex_prompt(p)
    except Exception:
        return None


def _feed(tx, data):
    """Fold whole JSONL lines in `data` into `tx`."""
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        if not any(m in raw for m in _WANT):
            continue                                 # no record kind read here
        if b'"user"' in raw and any(s in raw for s in _SKIP_USER):
            continue                                 # a tool result: no state change
        try:
            rec = json.loads(raw)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        if "payload" in rec and rec.get("type") in ("event_msg", "response_item"):
            _codex(tx, rec)
        else:
            _claude(tx, rec)


def own_transcript(tp, session, agent, data):
    """The transcript of the agent making this call (see the header), or None
    when a subagent's own file cannot be found."""
    if not agent:
        return tp or None
    atp = data.get("agent_transcript_path")
    if isinstance(atp, str) and atp:
        return atp
    if tp and "/subagents/" in str(tp):
        return tp
    if not tp or not _ID_RX.fullmatch(str(session or "")) or not _ID_RX.fullmatch(str(agent)):
        return None
    cand = os.path.join(os.path.dirname(str(tp)), session, "subagents", f"agent-{agent}.jsonl")
    return cand if os.path.isfile(cand) else None


def find_transcript(session):
    """The main transcript of a Claude Code session, by id, under the host's
    config dir (`<CLAUDE_CONFIG_DIR or ~/.claude>/projects/*/<session>.jsonl`);
    the newest wins. For the mod, which is not handed a path."""
    if not _ID_RX.fullmatch(str(session or "")):
        return None
    root = os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "projects")
    found = glob.glob(os.path.join(glob.escape(root), "*", f"{session}.jsonl"))
    try:
        return max(found, key=os.path.getmtime) if found else None
    except OSError:
        return None


def scan(tp, cache, slot="tx"):
    """`cache[slot]` brought up to date with `tp`; returns it, or None when
    the transcript cannot be read. Reads only what was appended since the last
    look, or the tail on the first look."""
    try:
        st = os.stat(tp)
    except Exception:
        return None
    tx = cache.get(slot) if isinstance(cache.get(slot), dict) else None
    ident = [os.path.realpath(tp), st.st_ino]
    off = tx.get("off") if tx else None
    if not tx or tx.get("ident") != ident or not isinstance(off, int) \
            or off > st.st_size or st.st_size - off > INCR_MAX:
        tx = dict(_fresh(), ident=ident, off=max(0, st.st_size - TAIL_BYTES), cut=None)
        tx["cut"] = tx["off"] > 0
    if st.st_size == tx["off"]:
        cache[slot] = tx
        return tx
    try:
        with open(tp, "rb") as f:
            f.seek(tx["off"])
            data = f.read(st.st_size - tx["off"])
    except Exception:
        return None
    if tx.get("cut"):
        nl = data.find(b"\n")            # the tail began mid-line
        if nl < 0:
            return None
        tx["off"] += nl + 1
        data = data[nl + 1:]
        tx["cut"] = False
    end = data.rfind(b"\n")              # a line still being written waits for next time
    if end < 0:
        cache[slot] = tx
        return tx
    _feed(tx, data[:end])
    tx["off"] += end + 1
    cache[slot] = tx
    return tx


def transcript_facts(tx, *, at_prompt, tool_use_id, now):
    """{context_tokens, idle_seconds, turn_tool_calls} from a scanned `tx`;
    each None when this transcript cannot say."""
    out = {"context_tokens": None, "idle_seconds": None, "turn_tool_calls": None}
    if not tx or tx.get("fmt") not in ("claude", "codex"):
        return out
    cur, prev = tx.get("cur") or {}, tx.get("prev") or {}
    out["context_tokens"] = cur.get("ctx")
    tools = list(tx.get("tools") or [])
    if at_prompt:
        out["turn_tool_calls"] = 0
    else:
        written = not tool_use_id or tool_use_id in tools
        if tx["fmt"] == "claude" and tool_use_id and not written:
            tools.append(tool_use_id)
        out["turn_tool_calls"] = len(tools)
    if tx["fmt"] == "claude":
        if at_prompt or not written:
            last = cur.get("last")
            out["idle_seconds"] = None if last is None else max(0, int(now - last))
        elif cur.get("first") is not None and prev.get("last") is not None:
            out["idle_seconds"] = max(0, int(cur["first"] - prev["last"]))
    return out


# ── the predicate ───────────────────────────────────────────────────────────


def _repeat_value(key, ev):
    if key == "rule":
        return ""
    cmd, path = (ev or ("", ""))[:2]
    if key == "command":
        v = " ".join(str(cmd or "").split())
    else:
        v = os.path.realpath(path) if path else ""
    return v or None


def holds(spec, rule, probes, ev, *, base, host, now=None):
    """Every `session` condition in `spec` holds for this call.

    `probes` answers from its fixture first (`session` → the transcript facts
    dict, `repeat_count` → an int), which is how the verifier and tests run
    this with no transcript and no state. Live, `probes.call` is
    `(session_id, hook_payload)`, set by the hook where it builds them.
    `ev` is `(command, file_path)` for the event being checked, None at a
    prompt. Called LAST in `given_ok`, so a repeat is counted only when the
    pattern and every other condition matched, and once per tool call."""
    now = time.time() if now is None else now
    call = getattr(probes, "call", None) or ("", {})
    session, data = call[0], call[1] if isinstance(call[1], dict) else {}
    path = facts_path(base, session) if session else None
    fix = getattr(probes, "_fix", {}) or {}
    agent = getattr(probes, "_agent_id", None)
    want = [k for k in ("context_tokens_gt", "idle_seconds_gt", "turn_tool_calls_gt") if k in spec]
    if want:
        def compute():
            tp = own_transcript(probes.tp, session, agent, data)
            if host == "cursor" or not path or not tp:
                return None
            slot = f"tx@{agent}" if agent else "tx"
            cache = _load(path)
            tx = scan(tp, cache, slot)
            if tx is not None:
                fresh = _load(path)          # keep repeat counts another call wrote meanwhile
                fresh.pop(slot, None)
                fresh[slot] = tx             # most recent last
                for k in [k for k in fresh if k.startswith("tx@")][:-_AGENT_TX_CAP]:
                    fresh.pop(k, None)
                _save(path, fresh)
            return transcript_facts(
                tx, at_prompt=data.get("hook_event_name") == "UserPromptSubmit",
                tool_use_id=str(data.get("tool_use_id") or "") or None, now=now)
        facts = probes._get("session", compute) or {}
        for k in want:
            have = facts.get(k[:-3])
            if not isinstance(have, int) or not have > spec[k]:
                return False
    if "repeat_count_gt" in spec:
        key = spec.get("repeat_key", "rule")
        if "repeat_count" in fix:
            n = fix["repeat_count"]
        else:
            v = _repeat_value(key, ev)
            if v is None or not path:
                return False
            h = hashlib.sha256(v.encode("utf-8", "replace")).hexdigest()[:16]
            slot = f"{rule.get('id')}|{key}|{h}"
            doc = _load(path)
            counts = doc.get("repeat") if isinstance(doc.get("repeat"), dict) else {}
            # [count, the tool call that last counted]: the pre lane and the
            # post lane's disk scan of one Bash write are one call, one count.
            prev = counts.pop(slot, None)
            n, last = prev if isinstance(prev, list) and len(prev) == 2 and isinstance(prev[0], int) \
                else (0, None)
            tuid = str(data.get("tool_use_id") or "") or None
            if not (tuid and tuid == last):
                n += 1
            counts[slot] = [n, tuid]               # most recent last
            while len(counts) > REPEAT_CAP:
                counts.pop(next(iter(counts)))
            doc["repeat"] = counts
            _save(path, doc)
        if not isinstance(n, int) or not n > spec["repeat_count_gt"]:
            return False
    return True


def mod_holds(op, *, base, probes_cls):
    """`rulebook_mod_state.py`'s `session` op: `holds` for one rule, asked by
    the mod (which has no transcript path) after the rule's pattern and every
    other `given` condition matched. The session's transcript is found by id
    once and kept in the session's facts file."""
    session = str(op.get("session") or "")
    rule = op.get("rule") if isinstance(op.get("rule"), dict) else {}
    spec = ((rule.get("given") or {}).get("session")) if isinstance(rule.get("given"), dict) else None
    if not session or not isinstance(spec, dict) or not spec:
        return False
    path = facts_path(base, session)
    tp = None
    if any(k in spec for k in ("context_tokens_gt", "idle_seconds_gt", "turn_tool_calls_gt")):
        doc = _load(path)
        tp = doc.get("tp") if isinstance(doc.get("tp"), str) and os.path.isfile(doc["tp"]) else None
        if tp is None:
            tp = find_transcript(session)
            if tp:
                doc = _load(path)
                doc["tp"] = tp
                _save(path, doc)
    agent = op.get("agent_id")
    agent = str(agent).strip()[:64] if isinstance(agent, str) and agent.strip() else None
    probes = probes_cls(None, None, transcript_path=tp, agent_id=agent)
    probes.call = (session, {"hook_event_name": op.get("hook_event_name"),
                             "tool_use_id": op.get("tool_use_id")})
    ev = op.get("ev")
    ev = (str(ev[0] or ""), str(ev[1] or "")) if isinstance(ev, list) and len(ev) == 2 else None
    return bool(holds(spec, rule, probes, ev, base=base, host="claude"))
