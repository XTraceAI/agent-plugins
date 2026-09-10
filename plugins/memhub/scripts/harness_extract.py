#!/usr/bin/env python3
"""Harness-tied extraction (harness-tied-memory-spec §4.2) — the client half.

A *lesson* is a rule an agent proposed from a session and a human activates.
This module does not write one. It decides which moments of a session are
worth the coding agent's attention and records them; the agent that lived the
turn writes the lesson at the next prompt (`harness_stop.py prompt`).

The pipeline, cheapest first:

    router       deterministic regexes over the turn — no model, no cost, and
                 never a decision (S0 measured it at 0.015 precision): it
                 labels the moments it sends, that is all
    window       §4.2 step 1, REDACTED before it leaves the machine
    classifier   ONE bounded POST to MemHub `/v1/team/rulebook/harness/classify`:
                 is this moment worth handing to the agent, and of what kind
    moment       on a signal, the turn, kind, router hint and the nine-field
                 stamp, appended locally for the prompt lane to hand over

There is no author here and none on the server: S0 authored rows with
`claude -p`, S1 briefly with a server model, and both were removed once the
live agent — which has the whole session and the person — became the author.
`build_row`, `pii_in_row` and `is_twin` stay, because `score_s0.py nudge`
measures the agent's rows against the same checks `create_rule` applies.

Run modes (the replay CLI; the live Stop sensor is `harness_stop.py`):

    --transcript PATH     a local Claude Code session .jsonl
    --turns PATH          canonical turns JSON (what the staging adapter emits,
                          so a teammate's session replays with no local file)
    --spawn               detach and return immediately; the caller is a hook
    --no-model            router only, no server calls — measures the router
                          without spending anything

Stdlib only, like every other script in this plugin. The server call rides the
credential `/memhub:login` already minted (`_memhub_auth` + `mcp_http`), the
same way the rulebook hook fetches its book.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The server's judge is bounded at 20 s. One attempt, and this is the whole
# wait: a call that outlives it produced nothing, and a moment the classifier
# never answered for is simply not handed to the agent.
CLASSIFY_TIMEOUT_S = float(os.environ.get("MEMHUB_HARNESS_CLASSIFY_TIMEOUT", "30"))
CLASSIFY_PATH = "/v1/team/rulebook/harness/classify"
WINDOW_MAX_CHARS = 24576      # the server refuses a longer body (schema max)

# The client failing to ask, counted apart from the server's own
# `judge_failed` / `disabled`, so an outage is never read as "nothing here".
CLIENT_REASONS = ("no_credential", "transport_error", "bad_reply")
# Every reason that means no verdict was reached.
NOT_A_SIGNAL = ("no_signal", "judge_failed", "disabled") + CLIENT_REASONS


def judge_said_signal(reason: str) -> bool:
    """For traces written before the author was removed (`score_s0.py nudge`
    reads old runs): any reason but these was an author outcome, so the judge
    had said signal. A current reply carries `signal` itself."""
    return bool(reason) and reason not in NOT_A_SIGNAL + ("classified",)


FLAG = "MEMHUB_HARNESS_EXTRACT"
_ON = ("1", "on", "true", "yes")


def extract_enabled(environ=None) -> bool:
    """The one switch for all of S1. Default OFF."""
    env = os.environ if environ is None else environ
    return str(env.get(FLAG, "")).strip().lower() in _ON


# Text the harness generated, not the human. §4.2 excludes it from the "user"
# role: a loop wakeup or a skill body is not a correction, and treating one as
# a signal drafts rules nobody asked for.
_SYS_BLOCK = re.compile(
    r"<system-reminder>.*?</system-reminder>"
    r"|<task-notification>.*?</task-notification>"
    r"|<command-name>.*?</command-name>"
    r"|<local-command-stdout>.*?</local-command-stdout>",
    re.S,
)
_HARNESS_PREFIX = (
    "Base directory for this skill",
    "Continue from where you left off",
    "Caveat: The messages below",
    # A /loop wakeup re-injects the skill body as a "user" message on every
    # tick. Counting one as a human turn drafts rules from the harness talking
    # to itself.
    "Skill /",
    "Skill ",
    # /compact re-injects its own summary as a "user" message. It is the
    # harness narrating the session back to itself, and every regex in the
    # router matches it — it quotes the whole conversation, corrections and
    # all, so one compaction manufactured a signal on every kind at once.
    "This session is being continued from a previous conversation",
    "<",
    "#",
)


# --------------------------------------------------------------- transcript
def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(
        b.get("text", "") for b in (content or [])
        if isinstance(b, dict) and b.get("type") == "text"
    )


def _brief(name: str, tool_input: dict) -> str:
    """One line naming the action, short enough to put many in a window."""
    i = tool_input or {}
    if name == "Bash":
        return f"Bash: {str(i.get('command', ''))[:200]}"
    if name in ("Edit", "Write", "MultiEdit", "Read", "NotebookEdit"):
        return f"{name}: {i.get('file_path', '')}"
    return f"{name}: {json.dumps(i, default=str)[:100]}"


def _target_of(name: str, tool_input: dict) -> str:
    """What the action addressed — a path, or a command. Used for error arcs
    (same target failing then succeeding) and for the state stamp."""
    i = tool_input or {}
    if name == "Bash":
        return str(i.get("command", ""))
    return str(i.get("file_path", "") or "")


def is_harness_text(txt: str) -> bool:
    if not txt:
        return True
    return txt.startswith(_HARNESS_PREFIX)


def turns_from_transcript(path) -> list[dict]:
    """A Claude Code .jsonl → the canonical turn list.

    A turn is one human message plus everything the agent did before the next
    one. Tool results arrive as `user` records and belong to the turn in
    progress, not to a new one.
    """
    turns: list[dict] = []
    cur: dict | None = None
    names: dict[str, str] = {}
    inputs: dict[str, dict] = {}
    with open(path, errors="replace", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            kind = rec.get("type")
            content = (rec.get("message") or {}).get("content")
            if kind == "user":
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content):
                    if cur is None:
                        continue
                    for b in content:
                        if not (isinstance(b, dict)
                                and b.get("type") == "tool_result"):
                            continue
                        body = b.get("content")
                        body = body if isinstance(body, str) else _text_of(body)
                        tid = b.get("tool_use_id")
                        cur["results"].append({
                            "tool": names.get(tid, "?"),
                            "target": _target_of(names.get(tid, ""),
                                                 inputs.get(tid, {})),
                            "error": bool(b.get("is_error")),
                            "text": (body or "")[:600],
                        })
                    continue
                txt = _SYS_BLOCK.sub("", _text_of(content)).strip()
                if is_harness_text(txt):
                    continue
                cur = {"n": len(turns) + 1, "user": txt, "tools": [],
                       "results": [], "asst": "",
                       "ts": rec.get("timestamp", ""),
                       "cwd": rec.get("cwd", ""),
                       "uuid": rec.get("uuid", "")}
                turns.append(cur)
            elif kind == "assistant" and cur is not None and isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        nm = b.get("name", "")
                        inp = b.get("input") or {}
                        names[b.get("id")] = nm
                        inputs[b.get("id")] = inp
                        cur["tools"].append({
                            "tool": nm, "brief": _brief(nm, inp),
                            "target": _target_of(nm, inp),
                        })
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        cur["asst"] = b["text"]
    return turns


def load_session(args) -> dict:
    """Either input shape → {session, source, cwd, repo, engineer, turns}."""
    if args.turns:
        doc = json.loads(Path(args.turns).read_text(encoding="utf-8"))
        doc.setdefault("source", "turns-json")
        doc.setdefault("turns", [])
        for i, t in enumerate(doc["turns"], 1):
            t.setdefault("n", i)
            for key, default in (("tools", []), ("results", []),
                                 ("asst", ""), ("user", ""), ("ts", ""),
                                 ("cwd", doc.get("cwd", ""))):
                t.setdefault(key, default)
        return doc
    path = Path(args.transcript)
    turns = turns_from_transcript(path)
    cwd = next((t.get("cwd") for t in turns if t.get("cwd")), "")
    return {"session": args.session or path.stem, "source": "claude-local",
            "cwd": cwd, "repo": "", "engineer": "", "turns": turns}


# ------------------------------------------------------------------ router
# Deterministic. Each pattern is a claim about a moment that carried a lesson
# in the mined corpus. S0 measured the set at 0.015 precision (68 hits bought
# one row), so a hit is a HINT the judge sees, never a decision that skips it.
RETRACT = re.compile(
    r"\b(i was wrong|my mistake|correction:|turns out|let me correct"
    r"|i re-?checked,? and|actually,? (it|that|the)\b.{0,40}\bnot\b)\b", re.I)
CLAIM = re.compile(
    r"\b(fixed|merged|deployed|verified|all (tests )?pass(ed|ing)?"
    r"|is live|works now|ready to merge)\b", re.I)
RECEIPT = re.compile(
    r"gh pr (view|checks)|pytest|uv run|git (log|status|diff)|curl "
    r"|REAL_EXIT|exit code|npm test|pnpm test", re.I)
RULEREQ = re.compile(
    r"\b(create|add|make) (a |an )?(rule|lesson)\b"
    r"|\bnever\b.{0,60}\balways\b|\balways\b.{0,60}\bnever\b"
    r"|\bfrom now on\b|\bevery time\b.{0,40}\b(you|u)\b", re.I)
REUSE = re.compile(
    r"\b(we already have|already exists|don'?t we already"
    r"|i thought we already|why (are u|are you|did u|did you) (re)?building)\b",
    re.I)
OVERRIDE = re.compile(r"RULEBOOK_OVERRIDE=", re.I)
WRONG_TARGET = re.compile(
    r"\b(i mean|i meant|not (that|the) (repo|branch|env|brain|one)"
    r"|check staging|on staging|it'?s (on |in )?(staging|prod))\b", re.I)

# An error whose cause is a trap in THIS environment, not a typo. A trap
# repeats for the next engineer; a typo does not.
TRAPS = [
    (r"ModuleNotFoundError|No module named", "missing-module"),
    (r"command not found: (timeout|rg|pg_isready|psql|gtimeout)",
     "missing-binary"),
    (r"unexpected keyword argument", "kwarg-drift"),
    (r"Blocked by the XTrace team rulebook", "gate-block"),
    (r"MissingGreenlet|greenlet_spawn", "async-context"),
    (r"relation \"[^\"]+\" does not exist", "schema-drift"),
    (r"could not translate host name|connection refused", "wrong-endpoint"),
]
# §4.0: a closed arc is routed on its own when it cost this many tool calls,
# whatever its signature — a trap that took five calls to get past is a trap
# whether or not this list knows its name.
ARC_COST_ROUTES = 5

ROUTER_KINDS = ("retraction", "claim_no_receipt", "standing_rule_request",
                "reuse_correction", "wrong_target", "gate_override",
                "error_arc")

# Detected, counted, and NOT sent.
#
# `claim_no_receipt` is a real signal — it is the moment the one surviving row
# of the prototype came from ("verify before agreeing the PR was merged"). But
# spec §1 and §5.1 already decided it is not a *rules* row: its trigger is the
# agent's own claim, not a command, so a bash matcher on `gh pr view` fires
# after the agent already did the right thing and nags on every other
# `gh pr view`. It becomes a built-in Stop check in the hook (§9 Q4), which
# costs zero model calls.
#
# It is also, by a distance, the most common router hit — 223 of 292 on the S0
# corpus. A turn whose ONLY hit is claim-shaped is not sent: that is the ~25%
# of turns the router spares a call, and the number S1 keeps comparable.
NOT_AUTHORED = {"claim_no_receipt"}

# What can arm an `ordering` rule. "edit" is what the hook understands today;
# "session" and "prompt" arrive with the §4.5 hook fixes. Anything else is the
# author inventing an event, and an ordering armed by an event no lane emits
# never fires.
ARMED_BY_EVENTS = ("edit", "session", "prompt")


def error_arcs(turn: dict) -> list[dict]:
    """Closed error arcs (§4.1): a tool error on target T, then a later
    success on the same T inside the same turn. The pair is the lesson —
    what failed and what fixed it — and an arc that never closed is just a
    failure, so it does not qualify. `cost` is the number of tool results
    between the failure and its fix."""
    arcs = []
    failed: dict[str, tuple[dict, int]] = {}
    for i, r in enumerate(turn.get("results", [])):
        tgt = (r.get("target") or "")[:200]
        if not tgt:
            continue
        if r.get("error"):
            failed.setdefault(tgt, (r, i))
        elif tgt in failed:
            first, at = failed.pop(tgt)
            arcs.append({"signature": first["text"][:200],
                         "target": tgt, "fix": tgt, "cost": i - at})
    return arcs


def route(turn: dict, prev: dict | None,
          arcs: list[dict] | None = None) -> list[tuple[str, str]]:
    """Reasons this turn may hold a lesson. Empty = ask the judge with no hint.

    `arcs` are closed error arcs the hook's PostToolUse pairing recorded live
    (`rulebook_hook.py post`, §4.1); they join the ones the transcript shows.
    """
    hits: list[tuple[str, str]] = []
    asst = turn.get("asst") or ""
    user = turn.get("user") or ""
    tools = [t.get("brief", "") for t in turn.get("tools", [])]

    m = RETRACT.search(asst)
    if m:
        hits.append(("retraction", m.group(0)))
    m = CLAIM.search(asst)
    if m and not any(RECEIPT.search(t) for t in tools[-8:]):
        hits.append(("claim_no_receipt", m.group(0)))
    if RULEREQ.search(user):
        hits.append(("standing_rule_request", user[:80]))
    if REUSE.search(user):
        hits.append(("reuse_correction", user[:80]))
    if WRONG_TARGET.search(user):
        hits.append(("wrong_target", user[:80]))
    if any(OVERRIDE.search(t) for t in tools):
        hits.append(("gate_override", ""))
    seen = set()
    for arc in error_arcs(turn) + list(arcs or []):
        key = (arc.get("target") or "")[:200]
        if key in seen:
            continue
        name = ""
        for rx, trap in TRAPS:
            if re.search(rx, arc.get("signature") or ""):
                name = trap
                break
        if not name and (arc.get("cost") or 0) >= ARC_COST_ROUTES:
            name = f"cost-{arc.get('cost')}"
        if name:
            seen.add(key)
            hits.append(("error_arc", name))
    return hits


def router_hint(hits: list[tuple[str, str]]) -> str:
    """The label the server's judge sees. One word, the first authored kind."""
    for kind, _ in hits:
        if kind not in NOT_AUTHORED:
            return kind
    return ""


# ------------------------------------------------------------------ window
def build_window(turn: dict, prev: dict | None, state: dict,
                 arcs: list[dict] | None = None) -> str:
    """§4.2 step 1. Previous user message, the previous turn's last 4 actions
    and last words, this user message, this turn's first 6 actions, errors,
    closed error arcs, final words, one state line.

    Tool output is IN the window and is untrusted — it can shape a draft,
    which is exactly why a draft is a proposal a human reads and never a
    fire, and why `redact_window` runs on it before it leaves the machine.
    """
    L = [f"STATE: {json.dumps(state, default=str)}"]
    if prev:
        L.append(f"PREVIOUS USER MESSAGE: {(prev.get('user') or '')[:600]}")
        acts = [t.get("brief", "") for t in prev.get("tools", [])][-4:]
        L.append("AGENT'S ACTIONS IN PREVIOUS TURN (last 4):")
        L += ["  - " + a for a in acts] or ["  (none)"]
        L.append("AGENT'S LAST WORDS IN PREVIOUS TURN: "
                 f"{(prev.get('asst') or '')[-500:]}")
    L.append(f"USER'S NEW MESSAGE: {(turn.get('user') or '')[:900]}")
    acts = [t.get("brief", "") for t in turn.get("tools", [])][:6]
    L.append("AGENT'S ACTIONS IN THIS TURN (first 6):")
    L += ["  - " + a for a in acts] or ["  (none)"]
    errs = [r for r in turn.get("results", []) if r.get("error")][:3]
    for e in errs:
        L.append(f"  ! error from {e.get('tool')}: {(e.get('text') or '')[:250]}")
    for arc in (error_arcs(turn) + list(arcs or []))[:2]:
        L.append(f"  ~ closed error arc on {arc['target'][:80]!r}: "
                 f"{(arc.get('signature') or '')[:150]}")
    L.append(f"AGENT'S FINAL WORDS THIS TURN: {(turn.get('asst') or '')[-700:]}")
    return "\n".join(L)


# -------------------------------------------------------------- redaction
# S0 Finding 1: the author baked a colleague's username into a rule regex,
# read out of an org-members query INSIDE the replayed session. Tool output is
# in the window and is untrusted. The server refuses an identity in a trigger
# as a second layer; this is the first, and it is not optional.
def redact_window(text: str) -> str:
    """Credentials, identities and command-line secrets out; never raises.

    Three existing denylists, in the order they were written for: `redact.py`
    (MemHub keys, the capture pipeline's guarantee), its identity pass (home
    directories and e-mail addresses — the shapes Finding 1 leaked), and the
    rulebook hook's command-line credential shapes (`--token=…`,
    `Authorization:`, vendor keys), which already guard the one other lane
    that sends content to a model. A denylist is a floor, not a guarantee.
    """
    if not text:
        return text
    out = text
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import redact  # noqa: PLC0415
        out = redact.redact_text(out)
        out = redact.redact_identities(out)
    except Exception:
        pass
    rh = _hook()
    if rh is not None:
        try:
            out = rh.redact_secrets(out)
        except Exception:
            pass
    return out


# ------------------------------------------------------------ server draft
def _api():
    """(rest_base, bearer, mcp_http) or None. Non-interactive: a detached
    child can only spend a credential /memhub:login already minted — the same
    resolution `rulebook_hook.fetch_book` uses."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    import mcp_http  # noqa: PLC0415
    import pak  # noqa: PLC0415
    from _memhub_auth import resolve_bearer  # noqa: PLC0415
    url, bearer = resolve_bearer(refresh=False)
    if not bearer:
        return None
    return pak.api_base(url), bearer, mcp_http


def server_classify(window: str, hint: str = "", repo: str = "",
                    timeout: float = 0) -> tuple[dict, float]:
    """ONE bounded POST. Returns ({signal, reason, kind?, derivable?}, seconds).

    Fail-open by construction: every failure is a reply with `signal: false`
    and a CLIENT_REASONS reason, so a caller counts an outage apart from a
    quiet moment and never has to catch anything. No retry — the caller is a
    detached best-effort child, and a second attempt doubles the cost of the
    failure it is trying to avoid.
    """
    t0 = time.time()
    body = {"window": window[:WINDOW_MAX_CHARS]}
    if hint:
        body["hint"] = hint[:64]
    if repo:
        body["repo"] = repo[:200]
    try:
        api = _api()
    except Exception as exc:                 # noqa: BLE001 — a hook path
        return ({"signal": False, "reason": "no_credential",
                 "detail": repr(exc)[:200]}, round(time.time() - t0, 1))
    if not api:
        return ({"signal": False, "reason": "no_credential"}, round(time.time() - t0, 1))
    base, bearer, http = api
    try:
        reply = http.rest(f"{base}{CLASSIFY_PATH}", bearer, "POST", body=body,
                          timeout=timeout or CLASSIFY_TIMEOUT_S)
    except Exception as exc:                 # noqa: BLE001 — one attempt
        return ({"signal": False, "reason": "transport_error",
                 "detail": str(exc)[:200]}, round(time.time() - t0, 1))
    dt = round(time.time() - t0, 1)
    data = reply.data
    if not (isinstance(data, dict) and isinstance(data.get("signal"), bool)
            and isinstance(data.get("reason"), str)):
        return ({"signal": False, "reason": "bad_reply",
                 "detail": f"HTTP {reply.status}: {str(data)[:120]}"}, dt)
    return data, dt


# ------------------------------------------------------------- row contract
# The server refuses a drafted row whose trigger or statement carries an
# identity (`_pii_in_trigger`, S0 Finding 1). A row the LOCAL agent authors
# never passes through that service — `create_rule` validates shape, not
# identity — so the same coarse test runs here: a `/Users/` or `/home/` path
# whose next segment has two consecutive letters, or an e-mail address,
# probed as written and with regex quoting collapsed.
_PII_PATTERNS = (
    re.compile(r"/(?:Users|home)/[^/\s]*[A-Za-z]{2}[^/\s]*"),
    re.compile(r"[\w.\-+]+@[\w\-]+\.[A-Za-z]{2,}"),
)


def _unescape(text: str) -> str:
    return re.sub(r"\[(.)\]", r"\1", re.sub(r"\\(.)", r"\1", text))


def pii_in_row(row: dict) -> str:
    """The first identity-shaped fragment in what a row matches or shows,
    or ''."""
    hay: list[str] = []
    for block in ("matcher", "ordering"):
        node = row.get(block)
        if isinstance(node, dict):
            hay += [v for v in node.values() if isinstance(v, str)]
    hay += [a for a in (row.get("anchors") or []) if isinstance(a, str)]
    hay += [row[k] for k in ("statement", "title") if isinstance(row.get(k), str)]
    for text in hay:
        for probe in (text, _unescape(text)):
            for pat in _PII_PATTERNS:
                hit = pat.search(probe)
                if hit:
                    return hit.group(0)
    return ""


def _rx_ok(pattern) -> bool:
    if not isinstance(pattern, str) or not pattern.strip():
        return False
    try:
        re.compile(pattern)
    except re.error:
        return False
    return True


def validate_engine(raw: dict) -> tuple[str, dict, str]:
    """(delivery, engine_body, refusal_reason). A row without a fillable
    engine does not exist — §2, "refused when no engine can be filled". The
    server validated the row it sent; this is the client refusing to write
    down anything it could not itself match, whatever the server said."""
    engine = raw.get("engine")
    if engine == "matcher":
        m = {k: v for k, v in dict(raw.get("matcher") or {}).items()
             if v not in (None, "")}
        event = m.get("event")
        if event not in ("bash", "edit", "output", "read"):
            return "", {}, "matcher_event_missing"
        need = {"bash": "command_rx", "edit": "path_rx",
                "output": "content_rx", "read": "path_rx"}[event]
        if not _rx_ok(m.get(need)):
            return "", {}, f"matcher_{need}_unusable"
        for key in ("command_rx", "command_not_rx", "path_rx", "path_not_rx",
                    "content_rx"):
            if key in m and not _rx_ok(m[key]):
                if key == need:
                    return "", {}, f"matcher_{key}_unusable"
                m.pop(key)
        return "agent_hook", {"matcher": m}, ""
    if engine == "ordering":
        o = {k: v for k, v in dict(raw.get("ordering") or {}).items()
             if v not in (None, "")}
        if not _rx_ok(o.get("required_command_rx")):
            return "", {}, "ordering_required_rx_unusable"
        if not _rx_ok(o.get("gated_command_rx")):
            return "", {}, "ordering_gated_rx_unusable"
        # Only three events can arm an ordering: "edit" today, plus "session"
        # and "prompt" from the §4.5 hook fixes. The author invents others —
        # `armed_by_events: ["bash"]` looks reasonable and is silently never
        # armed, so the rule is filed, reviewed, activated, and then never
        # fires. Found on the S0 corpus: a confirm-review-threads-before-merge
        # row, correct in every other respect, was dead on arrival.
        events = [e for e in (o.get("armed_by_events") or [])
                  if e in ARMED_BY_EVENTS]
        if not events:
            return "", {}, "ordering_armed_by_unknown"
        o["armed_by_events"] = events
        return "agent_hook", {"ordering": o}, ""
    if engine == "anchors":
        anchors = [a.strip() for a in (raw.get("anchors") or [])
                   if isinstance(a, str) and len(a.strip()) >= 3]
        # An anchor is an identifier or a path the server can spot in a call.
        # A phrase is a topic, and a topic anchor recalls everywhere.
        anchors = [a for a in anchors if " " not in a][:8]
        if not anchors:
            return "", {}, "anchors_not_identifiers"
        return "anchor_recall", {"anchors": anchors}, ""
    return "", {}, "no_engine"


def build_row(raw: dict, *, state: dict, session: str, turn_n: int,
              reason: str, scope_repos: list[str]) -> tuple[dict | None, str]:
    """(row, refusal_reason). Either a complete row or nothing.

    `raw` is the row the server drafted (or, in tests, one shaped like it).
    A `draft: false` or `derivable: true` row is still refused here — the
    server does the same, and a client that trusts the wire less than it
    trusts itself costs nothing."""
    if "draft" in raw and not raw.get("draft"):
        return None, (raw.get("refusal_reason") or "author_refused")
    if raw.get("derivable"):
        return None, "derivable_from_repo"
    statement = (raw.get("statement") or "").strip()
    if len(statement) < 20:
        return None, "statement_empty"
    title = (raw.get("title") or "").strip() or statement[:60]
    delivery, body, why = validate_engine(raw)
    if not delivery:
        return None, why
    for key in ("repo", "session_id", "turn", "hook_version", "at"):
        if state.get(key) in (None, ""):
            return None, f"state_missing_{key}"
    row = {
        "title": title[:120],
        "statement": statement,
        "delivery": delivery,
        "source": "session_draft",
        "source_ref": f"{session}#{turn_n}",
        "state": state,
        "supersedes_rule_id": None,
        "scope_repos": scope_repos,
        # local-only bookkeeping, stripped before create_rule (§4.4)
        "_reason": reason,
        "_rationale": (raw.get("rationale") or "")[:400],
    }
    row.update(body)
    return row, ""


# ------------------------------------------------------------- state stamp
def _git(root: str, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


_HOOK_MODULE: list = []      # [] = not tried, [None] = tried and unavailable


def _hook():
    """rulebook_hook, imported once, lazily, and never fatally.

    Resolved once because `resolve_repo` runs per ACTION and a turn can carry
    50+ of them: re-inserting HERE on sys.path each time grows that list without
    bound for the life of the process.

    The extractor reads the hook; it does not change it.
    """
    if _HOOK_MODULE:
        return _HOOK_MODULE[0]
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import rulebook_hook  # noqa: PLC0415
        _HOOK_MODULE.append(rulebook_hook)
    except Exception:
        _HOOK_MODULE.append(None)
    return _HOOK_MODULE[0]


# Repo resolution walks the filesystem (a worktree's .git back to its main
# checkout) and shells out to git. The answer for one (tool, target, cwd) does
# not change inside a run, and `stamp_state` asks for every action in a turn,
# twice — once to probe and once for the authored row.
_REPO_CACHE: dict = {}


def resolve_repo(tool: str, target: str, cwd: str) -> tuple[str, str]:
    """(repo_name, worktree_root) for the action a lesson is about.

    §4.2 step 1: the stamp is resolved PER ACTION. A second repo worked on from
    the same terminal would otherwise stamp the session's repo, file into its
    rulebook and scope the lesson to a repo it does not apply to.

    `rulebook_hook.resolve_target_repo` is the shared resolver §4.5 exports;
    the hook-fixes PR owns it and it is not on main yet, so this degrades to
    the pieces that ARE on main (`command_root` for a leading `cd`, then
    `repo_info` on the path or the cwd) rather than re-implementing it.
    """
    key = (tool, target, cwd)
    if key in _REPO_CACHE:
        return _REPO_CACHE[key]
    got = _resolve_repo_uncached(tool, target, cwd)
    _REPO_CACHE[key] = got
    return got


def _resolve_repo_uncached(tool: str, target: str, cwd: str) -> tuple[str, str]:
    rh = _hook()
    if rh is None:
        return "", ""
    shared = getattr(rh, "resolve_target_repo", None)
    if callable(shared):
        try:
            got = shared(tool, {"command": target} if tool == "Bash"
                         else {"file_path": target}, cwd)
            if isinstance(got, dict) and got.get("repo"):
                return got.get("repo", ""), got.get("root", "")
        except Exception:
            pass
    try:
        root = ""
        if tool == "Bash" and target:
            root = rh.command_root(cwd, target) or ""
        elif target and os.path.isabs(target):
            root = os.path.dirname(target)
        root = root or cwd
        if not root:
            return "", ""
        name, worktree, _gitdir, _branch = rh.repo_info(root)
        return name or "", worktree or ""
    except Exception:
        return "", ""


def stamp_state(*, session: str, turn: dict, row_engine_target: tuple[str, str],
                cwd: str, hook_version: str, env_name: str,
                default_repo: str = "", pr_number=None) -> dict:
    """The `state` §2 says the harness stamps and nobody types — nine fields.

    branch and head_sha are read AT DRAFT TIME from the resolved repo: a
    sibling session in the same checkout can move the branch before the review
    runs.

    `default_repo` is the repo the SESSION declared — for a staging replay
    there is no local checkout to resolve against, and the client-supplied
    namespace on the conversation row is the only trustworthy label. It is a
    fallback, never an override: a per-action resolution that succeeds wins,
    because that is the whole point of §4.2 step 1.
    """
    tool, target = row_engine_target
    repo, root = resolve_repo(tool, target, cwd)
    if not repo:
        repo, root = resolve_repo("", "", cwd)
    if not repo:
        repo, root = default_repo, ""

    touched: list[str] = []
    for action in turn.get("tools", []):
        name, _ = resolve_repo(action.get("tool", ""),
                               action.get("target", ""), cwd)
        if name and name not in touched:
            touched.append(name)

    state = {
        "repo": repo,
        "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD") if root else "",
        "head_sha": _git(root, "rev-parse", "HEAD")[:12] if root else "",
        "pr_number": pr_number,
        "env": env_name,
        "hook_version": hook_version,
        "session_id": session,
        "turn": turn.get("n"),
        "at": _dt.datetime.now(_dt.timezone.utc)
              .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    # §4.2 says to carry `touched_repos` when a turn touches two repos and the
    # lesson names neither. We carry it whenever the turn touched more than
    # one, which is broader, on purpose: "the lesson names neither" is exactly
    # the judgement this code cannot make. `engine_target` picks the action the
    # authored engine matched, and on a cross-repo turn that is routinely the
    # wrong one — a lesson about the plugin repo, drafted from a turn whose
    # first matching Bash call had `cd …/xmem`, stamps `repo: xmem` and would
    # file into xmem's rulebook. Measured on the S0 corpus: 6 of 7 early rows
    # from cross-repo sessions carried a repo the lesson was not about.
    #
    # A reviewer who sees the list can fix the scope in one click. A reviewer
    # shown one confident wrong repo has no signal that anything is wrong.
    if len(touched) > 1:
        state["touched_repos"] = touched
    return state


def plugin_version() -> str:
    try:
        manifest = HERE.parent / ".claude-plugin" / "plugin.json"
        return json.loads(manifest.read_text(encoding="utf-8")).get(
            "version", "0.0.0")
    except (OSError, ValueError):
        return "0.0.0"


# ------------------------------------------------------------------- dedup
_WORD = re.compile(r"[a-z_][a-z_0-9]{2,}")


def similarity(a: str, b: str) -> float:
    A = set(_WORD.findall((a or "").lower()))
    B = set(_WORD.findall((b or "").lower()))
    if not A or not B:
        return 0.0
    return len(A & B) / len(A | B)


TWIN_THRESHOLD = 0.5


def is_twin(row: dict, kept: list[dict]) -> dict | None:
    """A twin within one run. The cross-teammate twin check is server-side
    (§5.1); this one keeps a single run from filing N copies of one trap."""
    for other in kept:
        if similarity(row["statement"], other["statement"]) > TWIN_THRESHOLD:
            return other
        if row.get("matcher") and other.get("matcher"):
            if row["matcher"].get("command_rx") == other["matcher"].get("command_rx") \
                    and row["matcher"].get("command_rx"):
                return other
        if row.get("anchors") and other.get("anchors"):
            if set(row["anchors"]) & set(other["anchors"]):
                return other
    return None


# ------------------------------------------------------------------- drafts
def drafts_path(session: str, override: str = "") -> Path:
    if override:
        return Path(override)
    base = os.environ.get("MEMHUB_HARNESS_DRAFTS") or \
        str(Path.home() / ".config" / "memhub-plugin" / "drafts")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(session or ""))[:80] or "nosession"
    return Path(base) / f"{safe}.jsonl"


def moments_file(session: str) -> Path:
    """Where a session's classifier-flagged moments live."""
    return drafts_path(session).with_name(f"{drafts_path(session).stem}.moments.jsonl")


def append_draft(path: Path, row: dict) -> None:
    """Append-only, one row per line, never the cached book — `fetch_book`
    rewrites that file wholesale on every 200 (§4.2 step 5)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_drafts(path: Path) -> list[dict]:
    """Every row in a drafts file; a broken line is skipped, never fatal."""
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


# --------------------------------------------------------------------- run
class Trace:
    def __init__(self, path: str = "", quiet: bool = False):
        self.fh = None
        if path:
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                self.fh = open(path, "a", encoding="utf-8")
            except OSError:
                self.fh = None            # a trace that cannot be written is no trace
        self.quiet = quiet
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        self.lines.append(msg)
        if not self.quiet:
            print(msg, flush=True)
        if self.fh:
            self.fh.write(msg + "\n")
            self.fh.flush()

    def close(self) -> None:
        if self.fh:
            self.fh.close()


_EVENT_TOOLS = {"bash": ("Bash",), "output": ("Bash",),
                "edit": ("Edit", "Write", "MultiEdit", "NotebookEdit"),
                "read": ("Read",)}


def new_stats(doc: dict) -> dict:
    return {
        "session": doc.get("session") or "unknown", "engineer": doc.get("engineer", ""),
        "source": doc.get("source", ""), "turns": len(doc.get("turns") or []),
        "router_hits": 0, "router_hit_turns": 0, "stop_check_moments": {},
        "turns_sent": 0, "turns_spared": 0, "hinted_calls": 0, "hinted_signals": 0,
        "server_calls": 0, "moments": 0, "kinds": {}, "reasons": {},
        "transport_errors": 0, "latencies": [], "seconds": 0.0,
    }


def extract_turn(turn: dict, prev: dict | None, *, doc: dict, args,
                 stats: dict, trace, out_path: Path,
                 arcs: list[dict] | None = None) -> dict | None:
    """One turn through router → window → classifier → moment.

    Returns the moment written to `out_path`, or None. Shared by the replay
    loop below and the live Stop sensor, so what is measured is what runs.
    """
    session = doc.get("session") or "unknown"
    cwd = doc.get("cwd") or ""
    hook_version = args.hook_version or plugin_version()

    hits = route(turn, prev, arcs)
    if hits:
        stats["router_hits"] += len(hits)
        stats["router_hit_turns"] += 1
    trace(f"\n== turn {turn.get('n')} | user: "
          f"{(turn.get('user') or '')[:70]!r} | router: {hits or '-'}")
    for kind, _ in hits:
        if kind in NOT_AUTHORED:
            stats["stop_check_moments"][kind] = \
                stats["stop_check_moments"].get(kind, 0) + 1
    hint = router_hint(hits)
    if hits and not hint:
        # The claim-shaped moment is the built-in Stop check (spec §1), not a
        # lesson: counted, and the call spared.
        stats["turns_spared"] += 1
        trace("   -> claim-shaped: counted for the built-in Stop check, not sent")
        return None
    if args.no_model:
        trace(f"   -> router-only mode, not sent (hint={hint or '-'})")
        return None

    state = stamp_state(session=session, turn=turn, row_engine_target=("", ""),
                        cwd=cwd, hook_version=hook_version, env_name=args.env,
                        default_repo=doc.get("repo") or "")
    window = redact_window(build_window(turn, prev, state, arcs))
    reply, dt = server_classify(window, hint, repo=state.get("repo", ""),
                                timeout=getattr(args, "classify_timeout", 0))
    if getattr(args, "pace", 0):
        # Replay only. A personal access key is capped at one human's
        # throughput (60/min); a replay is not a human and must not look like
        # ten of them. The live sensor makes one call per turn.
        time.sleep(args.pace)
    stats["server_calls"] += 1
    stats["turns_sent"] += 1
    stats["hinted_calls"] += bool(hint)
    stats["latencies"].append(dt)
    reason = str(reply.get("reason") or "")
    stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
    if reason in CLIENT_REASONS:
        stats["transport_errors"] += 1
    if not reply.get("signal"):
        trace(f"   classifier ({dt}s): no signal [{reason}]"
              f"{' — ' + str(reply.get('detail'))[:120] if reply.get('detail') else ''}")
        return None
    kind = reply.get("kind")
    stats["kinds"][kind] = stats["kinds"].get(kind, 0) + 1
    stats["moments"] += 1
    stats["hinted_signals"] += bool(hint)
    moment = {"turn": turn.get("n"), "source_ref": f"{session}#{turn.get('n')}",
              "hint": hint, "kind": kind, "derivable": reply.get("derivable"),
              "window": window, "state": state}
    append_draft(out_path, moment)
    trace(f"   classifier ({dt}s): SIGNAL {kind}{' (derivable?)' if reply.get('derivable') else ''}"
          f" → moment for the agent")
    return moment


def run(doc: dict, args) -> dict:
    trace = Trace(args.trace, quiet=args.quiet)
    session = doc.get("session") or "unknown"
    turns = doc.get("turns") or []
    out_path = Path(args.out) if args.out else moments_file(session)
    stats = new_stats(doc)
    t_start = time.time()
    trace(f"session {session[:12]} | {len(turns)} turns | source "
          f"{doc.get('source')} | moments -> {out_path}")

    for i, turn in enumerate(turns):
        if args.turn and turn.get("n") != args.turn:
            continue
        prev = turns[i - 1] if i else None
        extract_turn(turn, prev, doc=doc, args=args, stats=stats, trace=trace,
                     out_path=out_path)

    stats["seconds"] = round(time.time() - t_start, 1)
    trace(f"\nMOMENTS {stats['moments']} | sent {stats['turns_sent']} "
          f"| spared {stats['turns_spared']} | reasons {stats['reasons']} "
          f"| transport errors {stats['transport_errors']} | {stats['seconds']}s")
    trace.close()
    if args.stats:
        Path(args.stats).write_text(
            json.dumps(stats, indent=1, default=str), encoding="utf-8")
    return stats


# -------------------------------------------------------------------- main
def child_env() -> dict:
    """Environment for anything this pipeline spawns.

    MEMHUB_HARNESS_CHILD=1 is the load-bearing one: a child that runs
    `claude -p` inside a repo (the §4.3 review) fires SessionStart / Stop /
    capture hooks, and without the flag a replay lands in the repo brain and
    shows up on the fleet board. CLAUDECODE / CLAUDE_CODE_ENTRYPOINT are
    dropped so the child does not believe it is nested inside this session.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    env["MEMHUB_HARNESS_CHILD"] = "1"
    return env


_child_env = child_env       # the S0 name; tests and the review use both


def log_path(name: str = "extract.log") -> Path:
    log_dir = Path(os.environ.get("MEMHUB_HARNESS_LOG_DIR") or
                   (Path.home() / ".config" / "memhub-plugin" / "harness"))
    return log_dir / name


def spawn_detached(argv: list[str], script: Path | None = None,
                   log_name: str = "extract.log") -> int:
    """Re-exec `argv` under `script` (default: this file), fully detached.

    The caller is a hook with a millisecond budget: it must return before the
    first network call, and the child must survive the session ending.
    """
    args = [sys.executable, str(script or Path(__file__).resolve())] + \
        [a for a in argv if a != "--spawn"]
    log_file = log_path(log_name)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_file, "a", encoding="utf-8")
    except OSError:
        log = subprocess.DEVNULL
    try:
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log,
                  "env": child_env(), "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0))
        elif hasattr(os, "setsid"):
            kwargs["start_new_session"] = True
        subprocess.Popen(args, **kwargs)
    except OSError:
        return 0        # fail open and silent, like every hook path (§6)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--transcript", help="a local Claude Code session .jsonl")
    src.add_argument("--turns", help="canonical turns JSON (staging adapter)")
    p.add_argument("--session", default="", help="session id (default: filename)")
    p.add_argument("--turn", type=int, help="only this turn index")
    p.add_argument("--out", default="",
                   help="moments file (default: ~/.config/memhub-plugin/drafts/<session>.moments.jsonl)")
    p.add_argument("--stats", default="", help="write a JSON run summary here")
    p.add_argument("--trace", default="", help="append the trace to this file")
    p.add_argument("--env", default=os.environ.get("MEMHUB_ENV", "staging"),
                   help="environment name for the state stamp")
    p.add_argument("--hook-version", default="",
                   help="hook version for the state stamp (default: plugin.json)")
    p.add_argument("--classify-timeout", type=float, default=0,
                   help=f"seconds for the one server call (default {CLASSIFY_TIMEOUT_S:g})")
    p.add_argument("--pace", type=float, default=0,
                   help="seconds to wait after each server call (replay only; "
                        "a personal key is capped at 60 calls/min)")
    p.add_argument("--no-model", "--router-only", dest="no_model",
                   action="store_true",
                   help="router only — no server calls")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--spawn", action="store_true",
                   help="detach and return immediately (hook callers)")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    if args.spawn:
        return spawn_detached(argv)
    try:
        doc = load_session(args)
    except (OSError, ValueError) as exc:
        print(f"[harness-extract] cannot read session: {exc!r}", file=sys.stderr)
        return 0        # fail open (§6)
    run(doc, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
