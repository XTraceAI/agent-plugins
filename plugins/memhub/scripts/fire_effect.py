#!/usr/bin/env python3
"""The window around each fire the agent SAW, for the server's effect judge.

MemHub's `POST /v1/team/rulebook/fire-effects` (harness-self-improvement-spec
§2) asks, per fire, whether showing the rule helped the work, cost steps, or
changed nothing. It can only answer from what happened AFTER the fire, and
that is in the session transcript on this machine. This module builds one
window per fire and `rulebook_hook.py` ships it through the ledger flush,
exactly as fires and events go:

  Stop (own turn, async)   `on_turn_end`: every shown fire of this session
                           since the last Stop → a window from the turn that
                           just ended, held in `state/<session>.effects.json`
                           until the person's next message is known.
  UserPromptSubmit         `on_prompt`: the person's message is the strongest
                           evidence the judge has ("thanks" vs "that's not
                           what I asked"); attach it and move the windows to
                           `ledger/effects.jsonl`. A wake-up or a /command is
                           not the person's reaction, so it attaches nothing.
  SessionEnd               `on_session_end`: ship what is still waiting with
                           `turn_ended_session: true`.

The Stop lane runs beside the next turn, so the prompt can land BEFORE the
windows are built: `on_prompt` leaves the prompt and its time in the state
file, and `on_turn_end` attaches it when it is newer than the turn's end.

One window is:

  person_request        the message that started the turn           ≤ 2000 chars
  before                the turn up to the fired-on call             ≤ 10 entries
  fired_on_call         the call the rule fired on                   ≤ 300 chars
  after_the_rule        from there to the end of the turn            ≤ 45 entries / 9000 chars
  next_person_message   the person's next message                    ≤ 1500 chars
  turn_ended_session    true when there was no next message

entries are {kind: person|agent|call|result, text}; agent text ≤ 1200, a
call or a result head ≤ 300. The caps are the server's: it REJECTS a longer
row rather than truncating. `fired_on_call` is kept apart from the rest
because the agent wrote it before it saw the rule — a judge shown it inside
`after` credits the rule with the call it fired on.

The fire's anchor is `tool_use_id`, recorded on the LOCAL fire row by
`log_fires` (never on the wire); a prompt-lane fire anchors on the prompt
itself. A fire whose anchor is not in the turn (a Stop that never ran, a
transcript rotated away) gets no window: nothing is guessed.

Every text goes through the caller's redactor BEFORE it is cut, as the judge
lane's turn does; with no redactor nothing is built. `MEMHUB_RULEBOOK_EFFECT=0`
turns the whole thing off. Stdlib only; never raises into a hook.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))

CAPS = {"person": 2000, "agent": 1200, "call": 300, "result": 300}
BEFORE_ENTRIES = 10
AFTER_ENTRIES = 45
AFTER_CHARS = 9000
FIRED_CHARS = 300
NEXT_CHARS = 1500
MAX_ENTRIES = 400             # of the turn, the LAST ones — a fire late in a long turn still anchors
MAX_BYTES = 16 * 1024 * 1024  # transcript larger than this: only its tail is read
MAX_PENDING = 64              # windows held per session between a Stop and the next prompt
_SLACK = 4

LEDGER = "effects.jsonl"
VERDICTS = "verdicts.jsonl"      # ledger/: what the server's judge said, one row per reply verdict
REPAIRS = "repairs.json"         # state/: {rule_id: epoch} — the rules a repair was handed for
REPAIR_MIN_P_COST = 0.8          # a cost verdict below this is read, never acted on (the judge is weak on cost)
REPAIR_COOLDOWN_S = 14 * 86400   # one open repair per rule per machine; later costs attach, never spawn
SHOWN_MODES = ("advise", "gate")
WIRE_KEYS = ("fire_id", "person_request", "before", "fired_on_call", "after_the_rule",
             "next_person_message", "turn_ended_session")


def enabled(environ=None) -> bool:
    v = (os.environ if environ is None else environ).get("MEMHUB_RULEBOOK_EFFECT", "")
    return v.strip().lower() not in ("0", "false", "no", "off")


# ── the transcript ──────────────────────────────────────────────────────────


def _hx():
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import harness_extract  # noqa: PLC0415 — beside this file
    return harness_extract


def _cut(text, cap, redact):
    """Redacted, then cut: a credential straddling the cut would no longer
    have the shape a denylist recognises."""
    text = (text or "")[:cap * _SLACK]
    try:
        text = redact(text) or ""
    except Exception:
        return ""          # a text that could not be redacted is not sent
    return text[:cap]


def _lines(path):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size > MAX_BYTES:
            f.seek(size - MAX_BYTES)
            f.readline()
        return f.read().splitlines()


def _call_text(name, tool_input):
    i = tool_input if isinstance(tool_input, dict) else {}
    what = str(i.get("command", "")) if name == "Bash" else str(i.get("file_path", "") or "")
    if not what:
        try:
            what = json.dumps(i, default=str) if i else ""
        except Exception:
            what = ""
    return f"{name}: {' '.join(what.split())}"


def read_turn(path, redact):
    """The last person turn of the transcript at `path`, oldest first:
    `[{kind, text, tid}]`, the person's message first (`tid` None), then each
    agent text, call and result head with the call's `tool_use_id` as `tid`.
    Attachments, `isMeta` records and harness text are skipped — at Stop the
    turn ends at the end of the file. `[]` when nothing can be read."""
    if not path or redact is None:
        return []
    try:
        hx = _hx()
        lines = _lines(str(path))
    except Exception:
        return []
    groups, kept = [], 0
    try:
        for raw in reversed(lines):
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("isSidechain"):
                continue
            kind = rec.get("type")
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
            content = msg.get("content")
            if kind == "assistant" and isinstance(content, list):
                if kept >= MAX_ENTRIES:
                    continue
                entries = []
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text" and str(b.get("text") or "").strip():
                        t = _cut(hx._SYS_BLOCK.sub("", str(b["text"])).strip(), CAPS["agent"], redact)
                        if t:
                            entries.append({"kind": "agent", "text": t, "tid": None})
                    elif b.get("type") == "tool_use":
                        t = _cut(_call_text(str(b.get("name") or ""), b.get("input")), CAPS["call"], redact)
                        if t:
                            entries.append({"kind": "call", "text": t, "tid": b.get("id")})
                groups.append(entries)
                kept += len(entries)
                continue
            if kind != "user":
                continue
            if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                if kept >= MAX_ENTRIES:
                    continue
                entries = []
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    body = b.get("content")
                    body = body if isinstance(body, str) else hx._text_of(body)
                    body = hx._SYS_BLOCK.sub("", body or "").strip()
                    t = _cut(" | ".join(body[:CAPS["result"] * _SLACK].splitlines()), CAPS["result"], redact)
                    if t:
                        entries.append({"kind": "result", "text": t, "tid": b.get("tool_use_id")})
                groups.append(entries)
                kept += len(entries)
                continue
            text = hx._SYS_BLOCK.sub("", hx._text_of(content)).strip()
            if rec.get("isMeta") or not text or hx.is_harness_text(text):
                continue              # a skill body, a blocked Stop's feedback, a wake-up
            person = _cut(text, CAPS["person"], redact)
            turn = [{"kind": "person", "text": person, "tid": None}] if person else []
            for entries in reversed(groups):
                turn.extend(entries)
            return turn[:1] + turn[1:][-MAX_ENTRIES:]
    except Exception:
        return []
    return []


# ── one fire → one window ───────────────────────────────────────────────────


def _entry(e, room=None):
    text = e["text"] if room is None else e["text"][:max(0, room)]
    return {"kind": e["kind"], "text": text}


def window_for(entries, fire):
    """The window for `fire` out of the turn's `entries`, or None when the
    fire's anchor is not in this turn."""
    if not entries or entries[0]["kind"] != "person":
        return None
    person, body = entries[0]["text"], entries[1:]
    if fire.get("hook_phase") == "prompt":
        before, fired, after = [], f"UserPromptSubmit: {person}", body
    else:
        tid = fire.get("tool_use_id")
        if not tid:
            return None
        i = next((k for k, e in enumerate(body) if e["kind"] == "call" and e.get("tid") == tid), None)
        if i is None:
            return None
        before, fired, after = body[max(0, i - BEFORE_ENTRIES):i], body[i]["text"], body[i + 1:]
    kept, room = [], AFTER_CHARS
    for e in after[:AFTER_ENTRIES]:
        if room <= 0:
            break
        kept.append(_entry(e, room))
        room -= len(e["text"])
    return {"person_request": person, "before": [_entry(e) for e in before],
            "fired_on_call": fired[:FIRED_CHARS], "after_the_rule": kept,
            "next_person_message": "", "turn_ended_session": False}


# ── the session's fires since the last Stop ─────────────────────────────────


def _ts(s):
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def session_fires(fires_path, session, since):
    """This session's SHOWN fires (advise or gate, not a session-start
    exposure) with `fired_at` after `since`, oldest first. Reads the whole
    local ledger: it rotates at 1 MB and this runs in an async Stop."""
    out = []
    floor = _ts(since) if since else None
    try:
        with open(fires_path, "rb") as f:
            for line in f:
                if not line.endswith(b"\n"):
                    break
                try:
                    r = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if not isinstance(r, dict) or r.get("session_id") != session:
                    continue
                if r.get("mode") not in SHOWN_MODES or r.get("hook_phase") == "session" or r.get("probe"):
                    continue
                at = _ts(r.get("fired_at"))
                if at is None or (floor is not None and at <= floor):
                    continue
                out.append(r)
    except FileNotFoundError:
        pass
    except Exception:
        return []
    out.sort(key=lambda r: r.get("fired_at") or "")
    return out


# ── the state file and the ledger ───────────────────────────────────────────


def _safe_name(session):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(session or ""))[:80] or "nosession"


def pending_path(sdir, session):
    return os.path.join(sdir, f"{_safe_name(session)}.effects.json")


def _load(p):
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _save(p, st):
    tmp = f"{p}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass


def _ledger_lock(ldir):
    try:
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        import rulebook_ledger  # noqa: PLC0415 — beside this file
        return rulebook_ledger, rulebook_ledger.append_lock(ldir)
    except Exception:
        return None, None


def ship(ldir, windows, *, next_text, ended):
    """Append the windows to `effects.jsonl`, each with the next message and
    whether the session ended. Returns how many were written."""
    if not windows:
        return 0
    L, lock = _ledger_lock(ldir)
    n = 0
    try:
        with open(os.path.join(ldir, LEDGER), "a", encoding="utf-8") as f:
            for w in windows:
                row = {k: w.get(k) for k in WIRE_KEYS}
                row["next_person_message"] = next_text or ""
                row["turn_ended_session"] = bool(ended)
                f.write(json.dumps(row) + "\n")
                n += 1
    except Exception:
        pass
    finally:
        if L is not None:
            L.release(lock)
    return n


def wire_row(row):
    """The ledger row as the server takes it (nothing else is on the row)."""
    return {k: row.get(k) for k in WIRE_KEYS}


# ── the three moments ───────────────────────────────────────────────────────


def _collect(ldir, st, session, transcript_path, redact):
    """Windows for the session's shown fires past the watermark, from the
    last turn of the transcript; moves the watermark."""
    fires = session_fires(os.path.join(ldir, "fires.jsonl"), session, st.get("watermark"))
    windows = []
    if fires:
        entries = read_turn(transcript_path, redact)
        for f in fires:
            w = window_for(entries, f)
            if w is not None:
                windows.append({"fire_id": f.get("fire_id"), **w})
        st["watermark"] = max(f.get("fired_at") or "" for f in fires) or st.get("watermark")
    return windows


def on_turn_end(ldir, sdir, session, transcript_path, ended_at, redact):
    """Build a window for every shown fire of `session` since the last Stop
    from the turn that just ended. Windows still waiting from an earlier turn
    ship without a next message (the prompt that followed them was not the
    person's). Returns the number of windows built."""
    if not enabled() or not session:
        return 0
    p = pending_path(sdir, session)
    st = _load(p)
    ship(ldir, st.pop("pending", None) or [], next_text="", ended=False)
    windows = _collect(ldir, st, session, transcript_path, redact)
    lp = st.get("last_prompt") if isinstance(st.get("last_prompt"), dict) else None
    prompt_at, end_at = _ts(lp.get("at")) if lp else None, _ts(ended_at)
    if windows and prompt_at is not None and end_at is not None and prompt_at >= end_at:
        ship(ldir, windows, next_text=lp.get("text", ""), ended=False)   # it already arrived
    elif windows:
        st["pending"] = windows[-MAX_PENDING:]
    _save(p, st)
    return len(windows)


def on_prompt(ldir, sdir, session, text, at, redact, *, person=True):
    """The person's message arrived: attach it to the waiting windows and
    ship them. `person=False` (a wake-up, a /command, another agent's
    message) attaches nothing and keeps them waiting."""
    if not enabled() or not session:
        return 0
    p = pending_path(sdir, session)
    st = _load(p)
    if not person:
        return 0
    cut = _cut(text, NEXT_CHARS, redact) if redact is not None else ""
    st["last_prompt"] = {"text": cut, "at": at}
    pend = st.pop("pending", None) or []
    n = ship(ldir, pend, next_text=cut, ended=False)
    _save(p, st)
    return n


def on_session_end(ldir, sdir, session, transcript_path=None, redact=None):
    """Ship what is still waiting: there is no next message. The last turn's
    fires are collected here too — the Stop lane that would have built their
    windows runs async beside this one and may not have run yet (a `-p`
    session ends on its first Stop). A fire both lanes window is one row the
    server takes twice and judges once."""
    if not enabled() or not session:
        return 0
    p = pending_path(sdir, session)
    st = _load(p)
    pend = st.pop("pending", None) or []
    if transcript_path and redact is not None:
        pend += _collect(ldir, st, session, transcript_path, redact)
    n = ship(ldir, pend, next_text="", ended=True)
    st.pop("last_prompt", None)
    _save(p, st)
    return n


# ── live repair: the judge's verdicts, and the cost fires worth a repair ─────


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def record_verdicts(ldir, verdicts):
    """Append a `/fire-effects` reply's `verdicts` to `ledger/verdicts.jsonl`,
    one row each: `{fire_id, effect, p_helped, p_cost, at}`. The server
    answers one verdict per accepted FIRE (a re-sent fire gets its stored
    one), so a fire may appear more than once across replies; readers take
    the newest. Returns how many were written; never raises."""
    rows = []
    for v in verdicts or []:
        if not isinstance(v, dict) or not v.get("fire_id") or v.get("effect") not in EFFECTS:
            continue
        rows.append({"fire_id": str(v["fire_id"]), "effect": v["effect"],
                     "p_helped": _num(v.get("effect_helped")), "p_cost": _num(v.get("effect_cost")),
                     "at": dt.datetime.now(dt.timezone.utc).isoformat()})
    if not rows:
        return 0
    L, lock = _ledger_lock(ldir)
    try:
        with open(os.path.join(ldir, VERDICTS), "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    except Exception:
        return 0
    finally:
        if L is not None:
            L.release(lock)
    return len(rows)


EFFECTS = ("helped", "neutral", "cost")


def _verdicts_from(path, offset):
    """Complete rows of `verdicts.jsonl` from byte `offset`; (rows, end)."""
    rows, end = [], offset
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            for line in f:
                if not line.endswith(b"\n"):
                    break
                end += len(line)
                try:
                    r = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                if isinstance(r, dict) and r.get("fire_id"):
                    rows.append(r)
    except FileNotFoundError:
        pass
    except Exception:
        return [], offset
    return rows, end


def pending_repair(ldir, sdir, session, *, now=None, min_p_cost=REPAIR_MIN_P_COST):
    """The one fire of `session` worth a repair since this was last asked, or
    None: a verdict of `cost` at or above `min_p_cost`, on a shown fire of
    this session, for a rule no repair was handed for on this machine within
    REPAIR_COOLDOWN_S. Consumes every new verdict it reads (the session's
    watermark moves) and, when it answers, marks the rule handed — so the
    next Stop never answers the same cost twice, and a second cost on the
    same rule attaches to the open repair instead of spawning another.
    `{fire_id, rule_id, title, p_cost, tool_use_id, hook_phase, fired_at}`.
    Never raises."""
    if not session:
        return None
    try:
        now = time.time() if now is None else now
        p = os.path.join(sdir, f"{_safe_name(session)}.repairs.json")
        st = _load(p)
        rows, end = _verdicts_from(os.path.join(ldir, VERDICTS), int(st.get("verdicts_offset") or 0))
        if not rows:
            return None
        st["verdicts_offset"] = end
        fires = {f.get("fire_id"): f for f in session_fires(os.path.join(ldir, "fires.jsonl"), session, None)}
        rp = os.path.join(sdir, REPAIRS)
        handed = _load(rp)
        latest = {}
        for r in rows:                      # the newest verdict per fire wins
            latest[r["fire_id"]] = r
        best = None
        for fid, r in latest.items():
            f = fires.get(fid)
            if f is None or r.get("effect") != "cost" or (r.get("p_cost") or 0) < min_p_cost:
                continue
            at = handed.get(str(f.get("rule_id")))
            if isinstance(at, (int, float)) and now - at < REPAIR_COOLDOWN_S:
                continue
            if best is None or (r.get("p_cost") or 0) > (best[1].get("p_cost") or 0):
                best = (f, r)
        if best is None:
            _save(p, st)
            return None
        f, r = best
        handed[str(f.get("rule_id"))] = now
        _save(rp, handed)
        _save(p, st)
        return {"fire_id": f.get("fire_id"), "rule_id": f.get("rule_id"), "title": f.get("title") or "",
                "p_cost": r.get("p_cost"), "tool_use_id": f.get("tool_use_id"),
                "hook_phase": f.get("hook_phase"), "fired_at": f.get("fired_at")}
    except Exception:
        return None
