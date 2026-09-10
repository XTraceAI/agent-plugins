#!/usr/bin/env python3
"""Harness-tied extraction (harness-tied-memory-spec §4.2) — the client half.

A *lesson* is a rule an agent proposed from a session and a human activates.
This module drafts one. It never activates anything, never fires anything, and
never files a rule: drafts land in a local JSONL that the post-session review
(§4.3, `harness_stop.py review`) reads.

The pipeline, cheapest first:

    router       deterministic regexes over the turn — no model, no cost, and
                 never a decision (S0 measured it at 0.015 precision): it
                 decides which turns are worth a call and labels the ones it
                 sends, that is all
    window       §4.2 step 1, REDACTED before it leaves the machine
    server       ONE bounded POST to MemHub `/v1/team/rulebook/harness/draft`
                 — the judge and the author run there (MemHub #1249); the
                 reply is a row shaped for `create_rule`, or a one-word refusal
    stamp        the nine-field state the harness writes and nobody types
    row          complete, or it does not exist

S0 ran the judge and author through the headless `claude` CLI from this file.
That half is gone: no API key, no structured output, no author bound and no
token accounting live on the client any more. A refusal from the server —
`no_signal`, `derivable`, `bad_anchors`, `matches_everything`,
`project_state`, `judge_failed`, `author_failed`, `disabled` — is the ordinary
outcome (198 of 229 author calls in S0) and is recorded, never retried.

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

DEFAULT_BUDGET = 8            # §4.3 step 4: drafts per session

# The server's own bounds are judge 20 s + author 90 s. One attempt, and this
# is the whole wait: a call that outlives it produced nothing usable, and a
# missing draft is recoverable (the next review moment re-runs) while a
# half-parsed one is not.
DRAFT_TIMEOUT_S = float(os.environ.get("MEMHUB_HARNESS_DRAFT_TIMEOUT", "120"))
DRAFT_PATH = "/v1/team/rulebook/harness/draft"
WINDOW_MAX_CHARS = 24576      # the server refuses a longer body (schema max)

# Everything else the server may answer is a REFUSAL — the machinery working.
# These three are the client failing to ask, and are counted apart so an
# outage is never read as "the author refused".
CLIENT_REASONS = ("no_credential", "transport_error", "bad_reply")

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


def server_draft(window: str, hint: str = "", repo: str = "",
                 timeout: float = 0) -> tuple[dict, float]:
    """ONE bounded POST. Returns ({drafted, reason, kind?, row?}, seconds).

    Fail-open by construction: every failure is a reply with `drafted: false`
    and a CLIENT_REASONS reason, so a caller counts an outage apart from a
    refusal and never has to catch anything. No retry — the caller is a
    detached best-effort extractor, and a second attempt doubles the cost of
    the failure it is trying to avoid.
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
        return ({"drafted": False, "reason": "no_credential",
                 "detail": repr(exc)[:200]}, round(time.time() - t0, 1))
    if not api:
        return ({"drafted": False, "reason": "no_credential"},
                round(time.time() - t0, 1))
    base, bearer, http = api
    try:
        reply = http.rest(f"{base}{DRAFT_PATH}", bearer, "POST", body=body,
                          timeout=timeout or DRAFT_TIMEOUT_S)
    except Exception as exc:                 # noqa: BLE001 — one attempt
        return ({"drafted": False, "reason": "transport_error",
                 "detail": str(exc)[:200]}, round(time.time() - t0, 1))
    dt = round(time.time() - t0, 1)
    data = reply.data
    if not (isinstance(data, dict) and isinstance(data.get("drafted"), bool)
            and isinstance(data.get("reason"), str)):
        return ({"drafted": False, "reason": "bad_reply",
                 "detail": f"HTTP {reply.status}: {str(data)[:120]}"}, dt)
    if data["drafted"] and not isinstance(data.get("row"), dict):
        return ({"drafted": False, "reason": "bad_reply",
                 "detail": "drafted without a row"}, dt)
    return data, dt


# ------------------------------------------------------------- row contract
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


def engine_target(raw: dict, turn: dict) -> tuple[str, str]:
    """The action the lesson is about, for the per-action state stamp."""
    engine = raw.get("engine")
    if engine == "matcher":
        m = raw.get("matcher") or {}
        if m.get("event") == "bash":
            for action in turn.get("tools", []):
                if action.get("tool") == "Bash" and _rx_ok(m.get("command_rx")):
                    if re.search(m["command_rx"], action.get("target", ""), re.I):
                        return "Bash", action.get("target", "")
            return "Bash", ""
        for action in turn.get("tools", []):
            if action.get("tool") in ("Edit", "Write", "MultiEdit", "Read"):
                return action["tool"], action.get("target", "")
        return "Edit", ""
    if engine == "anchors":
        anchors = [a for a in (raw.get("anchors") or []) if isinstance(a, str)]
        for action in turn.get("tools", []):
            tgt = action.get("target", "")
            if any(a in tgt for a in anchors):
                return action.get("tool", ""), tgt
    return "", ""


def new_stats(doc: dict) -> dict:
    return {
        "session": doc.get("session") or "unknown", "engineer": doc.get("engineer", ""),
        "source": doc.get("source", ""), "turns": len(doc.get("turns") or []),
        "router_hits": 0, "router_hit_turns": 0, "stop_check_moments": {},
        "turns_sent": 0, "turns_spared": 0, "hinted_calls": 0,
        "server_calls": 0, "rows": 0, "kinds": {},
        "refusals": {}, "router_authored": {}, "router_refused": {},
        "transport_errors": 0, "twins": 0, "budget_stops": 0,
        "latencies": [], "seconds": 0.0,
    }


def extract_turn(turn: dict, prev: dict | None, *, doc: dict, args,
                 kept: list[dict], stats: dict, trace, out_path: Path,
                 arcs: list[dict] | None = None,
                 pr_number=None) -> dict | None:
    """One turn through router → window → server → stamp → row.

    Returns the drafted row, or None. Shared by the replay loop below and the
    live Stop sensor, so what is measured is what runs.
    """
    session = doc.get("session") or "unknown"
    cwd = doc.get("cwd") or ""
    hook_version = args.hook_version or plugin_version()
    declared_repo = doc.get("repo") or ""

    def note_refusal(reason: str, router_kind: str) -> None:
        stats["refusals"][reason] = stats["refusals"].get(reason, 0) + 1
        if router_kind:
            stats["router_refused"][router_kind] = \
                stats["router_refused"].get(router_kind, 0) + 1

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
        # rules row: counted, and the call spared.
        stats["turns_spared"] += 1
        trace("   -> claim-shaped: counted for the built-in Stop check, not sent")
        return None
    if args.no_model:
        trace(f"   -> router-only mode, not sent (hint={hint or '-'})")
        return None
    if len(kept) >= args.budget:
        stats["budget_stops"] += 1
        trace(f"   budget {args.budget} reached, not sent")
        return None

    state_probe = stamp_state(
        session=session, turn=turn, row_engine_target=("", ""),
        cwd=cwd, hook_version=hook_version, env_name=args.env,
        default_repo=declared_repo, pr_number=pr_number)
    window = redact_window(build_window(turn, prev, state_probe, arcs))
    reply, dt = server_draft(window, hint, repo=state_probe.get("repo", ""),
                             timeout=args.draft_timeout)
    stats["server_calls"] += 1
    stats["turns_sent"] += 1
    stats["hinted_calls"] += bool(hint)
    stats["latencies"].append(dt)
    kind = reply.get("kind")
    if kind:
        stats["kinds"][kind] = stats["kinds"].get(kind, 0) + 1
    if not reply.get("drafted"):
        reason = str(reply.get("reason") or "refused")
        note_refusal(reason, hint)
        if reason in CLIENT_REASONS:
            stats["transport_errors"] += 1
        trace(f"   server ({dt}s): {reason}"
              f"{' — ' + str(reply.get('detail'))[:120] if reply.get('detail') else ''}"
              f"{' | judge=' + str(kind) if kind else ''}")
        return None

    raw = reply["row"]
    state = stamp_state(
        session=session, turn=turn,
        row_engine_target=engine_target(raw, turn), cwd=cwd,
        hook_version=hook_version, env_name=args.env,
        default_repo=declared_repo, pr_number=pr_number)
    scope = [state["repo"]] if state.get("repo") else []
    reason = f"router:{hint}" if hint else f"judge:{kind}"
    row, refusal = build_row(raw, state=state, session=session,
                             turn_n=turn.get("n"), reason=reason,
                             scope_repos=scope)
    if row is None:
        note_refusal(refusal, hint)
        trace(f"   server drafted, client refused ({dt}s) [{refusal}]: "
              f"{str(raw.get('title', ''))[:100]}")
        return None
    twin = is_twin(row, kept)
    if twin:
        stats["twins"] += 1
        note_refusal("twin_in_run", hint)
        trace(f"   twin of {twin['source_ref']} — dropped: {row['title'][:70]}")
        return None
    row["_kind"] = kind
    kept.append(row)
    stats["rows"] += 1
    if hint:
        stats["router_authored"][hint] = stats["router_authored"].get(hint, 0) + 1
    append_draft(out_path, row)
    trace(f"   DRAFT ({dt}s) [{row['delivery']}] {row['title'][:80]}\n"
          f"      {row['statement'][:220]}\n"
          f"      repo={state['repo']} branch={state['branch']} "
          f"engine={ {k: v for k, v in row.items() if k in ('matcher', 'ordering', 'anchors')} }")
    return row


def run(doc: dict, args) -> dict:
    trace = Trace(args.trace, quiet=args.quiet)
    session = doc.get("session") or "unknown"
    turns = doc.get("turns") or []
    out_path = drafts_path(session, args.out)
    stats = new_stats(doc)
    t_start = time.time()
    kept: list[dict] = []
    trace(f"session {session[:12]} | {len(turns)} turns | source "
          f"{doc.get('source')} | drafts -> {out_path}")

    for i, turn in enumerate(turns):
        if args.turn and turn.get("n") != args.turn:
            continue
        prev = turns[i - 1] if i else None
        extract_turn(turn, prev, doc=doc, args=args, kept=kept, stats=stats,
                     trace=trace, out_path=out_path)

    stats["seconds"] = round(time.time() - t_start, 1)
    trace(f"\nROWS {stats['rows']} | sent {stats['turns_sent']} "
          f"| spared {stats['turns_spared']} | refusals {stats['refusals']} "
          f"| transport errors {stats['transport_errors']} "
          f"| twins {stats['twins']} | {stats['seconds']}s")
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
    p.add_argument("--out", default="", help="drafts file (default: ~/.config/memhub-plugin/drafts/<session>.jsonl)")
    p.add_argument("--stats", default="", help="write a JSON run summary here")
    p.add_argument("--trace", default="", help="append the trace to this file")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                   help=f"max drafts per session (default {DEFAULT_BUDGET})")
    p.add_argument("--env", default=os.environ.get("MEMHUB_ENV", "staging"),
                   help="environment name for the state stamp")
    p.add_argument("--hook-version", default="",
                   help="hook version for the state stamp (default: plugin.json)")
    p.add_argument("--draft-timeout", type=float, default=0,
                   help=f"seconds for the one server call (default {DRAFT_TIMEOUT_S:g})")
    p.add_argument("--no-model", "--router-only", dest="no_model",
                   action="store_true",
                   help="router only — no server calls, nothing drafted")
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
