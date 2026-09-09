#!/usr/bin/env python3
"""Harness-tied extraction (harness-tied-memory-spec §4.2) — the detached extractor.

A *lesson* is a rule an agent proposed from a session and a human activates.
This script drafts one. It never activates anything, never fires anything, and
never writes to a server: drafts land in a local JSONL that the post-session
review (§4.3) reads.

The pipeline is three stages, cheapest first:

    router       deterministic regexes over the turn — no model, no cost
    judge        one small-model call, only when the router did not decide
    author       one stronger-model call, on a signal — or REFUSE

Everything is bounded and fail-open. A model call gets ONE attempt (judge 30 s,
author 120 s); a timeout, a bad exit or unparseable output yields no row, never
a partial one. Every refusal is logged with its reason, because the refusals are
the measurement: S0 judges the drafted set by hand, and a row that should not
exist costs more than a row that never got authored.

Run modes:

    --transcript PATH     a local Claude Code session .jsonl
    --turns PATH          canonical turns JSON (what the staging adapter emits,
                          so a teammate's session replays with no local file)
    --spawn               detach and return immediately; the caller is a hook
    --no-model            router only, no model calls — used to measure router
                          precision without spending anything

Stdlib only, like every other script in this plugin. The model path is the
headless `claude` CLI: the plugin cannot assume an API key or a pip install,
and the CLI is the path S0 measured. Child CLI runs carry
MEMHUB_HARNESS_CHILD=1 so `claude_hook_guard.py` disarms every hook for them —
without it the extractor's own sessions get captured into the repo brain, which
is a thing that has already happened once.
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

# Spec §4.2 bounds. They are the CONTRACT, and the defaults, but the headless
# CLI pays a fixed startup cost the direct-API path does not, so a judge call
# can lose the race at 30 s on a slow link. Overridable so a measurement run
# can report what the pipeline does at the bound AND what it does when it is
# not starved — see the S0 scorecard's judge-timeout row.
JUDGE_TIMEOUT_S = int(os.environ.get("MEMHUB_HARNESS_JUDGE_TIMEOUT", "30"))
AUTHOR_TIMEOUT_S = int(os.environ.get("MEMHUB_HARNESS_AUTHOR_TIMEOUT", "120"))
DEFAULT_BUDGET = 8            # §4.3 step 4: drafts per session
JUDGE_MODEL = "haiku"
AUTHOR_MODEL = "sonnet"

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
                       "cwd": rec.get("cwd", "")}
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
# in the mined corpus; S0 reports the precision of the set (scorecard row 5),
# because a router hit skips the judge and goes straight to an author call.
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

ROUTER_KINDS = ("retraction", "claim_no_receipt", "standing_rule_request",
                "reuse_correction", "wrong_target", "gate_override",
                "error_arc")

# Detected, counted, and NOT authored.
#
# `claim_no_receipt` is a real signal — it is the moment the one surviving row
# of the prototype came from ("verify before agreeing the PR was merged"). But
# spec §1 and §5.1 already decided it is not a *rules* row: its trigger is the
# agent's own claim, not a command, so a bash matcher on `gh pr view` fires
# after the agent already did the right thing and nags on every other
# `gh pr view`. It becomes a built-in Stop check in the hook (§9 Q4), which
# costs zero model calls.
#
# It is also, by a distance, the most common router hit — 233 of 277 on the S0
# corpus, 84%. Authoring them would have spent ~$25 of Sonnet calls to collect
# refusals for rows the spec says cannot exist.
NOT_AUTHORED = {"claim_no_receipt"}


def error_arcs(turn: dict) -> list[dict]:
    """Closed error arcs (§4.1): a tool error on target T, then a later
    success on the same T inside the same turn. The pair is the lesson —
    what failed and what fixed it — and an arc that never closed is just a
    failure, so it does not qualify."""
    arcs = []
    failed: dict[str, dict] = {}
    for r in turn.get("results", []):
        tgt = (r.get("target") or "")[:200]
        if not tgt:
            continue
        if r.get("error"):
            failed.setdefault(tgt, r)
        elif tgt in failed:
            arcs.append({"signature": failed[tgt]["text"][:200],
                         "target": tgt, "fix": tgt})
            del failed[tgt]
    return arcs


def route(turn: dict, prev: dict | None) -> list[tuple[str, str]]:
    """Reasons this turn may hold a lesson. Empty = ask the judge."""
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
    for arc in error_arcs(turn):
        for rx, name in TRAPS:
            if re.search(rx, arc["signature"]):
                hits.append(("error_arc", name))
                break
    return hits


# ------------------------------------------------------------------ window
def build_window(turn: dict, prev: dict | None, state: dict) -> str:
    """§4.2 step 1. Previous user message, the previous turn's last 4 actions
    and last words, this user message, this turn's first 6 actions, errors,
    closed error arcs, final words, one state line.

    Tool output is IN the window and is untrusted — it can shape a draft,
    which is exactly why a draft is a proposal a human reads and never a fire.
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
    for arc in error_arcs(turn)[:2]:
        L.append(f"  ~ closed error arc on {arc['target'][:80]!r}: "
                 f"{arc['signature'][:150]}")
    L.append(f"AGENT'S FINAL WORDS THIS TURN: {(turn.get('asst') or '')[-700:]}")
    return "\n".join(L)


# ------------------------------------------------------------- model calls
JUDGE_PROMPT = (HERE / "prompts" / "harness_judge.txt")
AUTHOR_PROMPT = (HERE / "prompts" / "harness_author.txt")

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "signal": {"type": "boolean"},
        "kind": {"type": "string",
                 "enum": ["standing_rule", "correction", "claim_challenge",
                          "error_arc", "tribal", "none"]},
        "derivable": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["signal", "kind", "derivable", "rationale"],
}

AUTHOR_SCHEMA = {
    "type": "object",
    "properties": {
        "draft": {"type": "boolean"},
        "refusal_reason": {"type": "string"},
        "title": {"type": "string"},
        "statement": {"type": "string"},
        "engine": {"type": "string",
                   "enum": ["matcher", "ordering", "anchors", "none"]},
        "matcher": {"type": "object", "properties": {
            "event": {"type": "string",
                      "enum": ["bash", "edit", "output", "read"]},
            "command_rx": {"type": "string"},
            "command_not_rx": {"type": "string"},
            "path_rx": {"type": "string"},
            "path_not_rx": {"type": "string"},
            "content_rx": {"type": "string"},
        }},
        "ordering": {"type": "object", "properties": {
            "required_command_rx": {"type": "string"},
            "gated_command_rx": {"type": "string"},
            "armed_by_events": {"type": "array", "items": {"type": "string"}},
            "display_name": {"type": "string"},
        }},
        "anchors": {"type": "array", "items": {"type": "string"}},
        "derivable": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["draft", "refusal_reason", "title", "statement", "engine",
                 "derivable", "rationale"],
}


class ModelError(Exception):
    """A bounded model call that produced nothing usable. Never a partial row."""


def _child_env() -> dict:
    """Environment for a headless child.

    MEMHUB_HARNESS_CHILD=1 is the load-bearing one: the child runs `claude -p`
    inside a repo, which fires SessionStart / Stop / capture hooks, and without
    the flag a replay lands in the repo brain and shows up on the fleet board.
    CLAUDECODE / CLAUDE_CODE_ENTRYPOINT are dropped so the child does not
    believe it is nested inside this session.
    """
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    env["MEMHUB_HARNESS_CHILD"] = "1"
    return env


def call_model(system: str, user: str, schema: dict, model: str,
               timeout: int) -> tuple[dict, float]:
    """One bounded attempt at the headless CLI. Raises ModelError otherwise.

    No retry on purpose: the caller is a detached best-effort extractor, a
    second attempt doubles the cost of the failure mode it is trying to avoid,
    and a missing draft is recoverable (the next review moment re-runs) while a
    half-parsed one is not.
    """
    cmd = [
        "claude", "-p", "--model", model,
        # --safe-mode disables every customization for the child: CLAUDE.md,
        # skills, MCP servers, and — the reason it is here — plugins and hooks,
        # including THIS plugin's. Without it the child is an ordinary Claude
        # session in a repo, so capture flushes its turns into the repo brain
        # and it appears on the fleet board as an agent nobody started. That
        # happened during S0's own measurement runs.
        #
        # MEMHUB_HARNESS_CHILD (set in _child_env) is the belt to this
        # suspenders: --safe-mode is a host flag that only Claude Code has and
        # only while it keeps the flag, whereas the env var is honoured by
        # claude_hook_guard.py on every host and survives a child that must
        # keep its customizations for some other reason.
        "--safe-mode",
        "--output-format", "json",
        "--json-schema", json.dumps(schema),
        "--no-session-persistence",
        "--disallowedTools", "Bash", "Read", "Edit", "Write", "MultiEdit",
        "Agent", "Grep", "Glob", "WebSearch", "WebFetch",
        "--append-system-prompt", system,
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True,
                              timeout=timeout, env=_child_env())
    except subprocess.TimeoutExpired:
        raise ModelError(f"timeout after {timeout}s")
    except (OSError, ValueError) as exc:
        raise ModelError(f"spawn failed: {exc!r}")
    dt = round(time.time() - t0, 1)
    if proc.returncode != 0:
        raise ModelError(f"exit {proc.returncode}: "
                         f"{(proc.stderr or '')[-200:].strip()}")
    try:
        envelope = json.loads(proc.stdout)
    except ValueError:
        raise ModelError(f"unparseable stdout: {(proc.stdout or '')[:200]}")
    if envelope.get("is_error"):
        raise ModelError(f"cli error: {str(envelope.get('result'))[:200]}")
    # `structured_output` is present only when the model actually called the
    # StructuredOutput tool. When it answered in prose instead, `result` is
    # that prose and parsing it fails — which is the correct outcome: no row.
    payload = envelope.get("structured_output")
    if payload is None:
        payload = envelope.get("result")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            raise ModelError(f"result not JSON: {payload[:200]}")
    if not isinstance(payload, dict):
        raise ModelError(f"result not an object: {type(payload).__name__}")
    return payload, dt


def _prompt(path: Path, fallback: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return fallback


def judge(win: str, timeout: int = 0) -> tuple[dict, float]:
    return call_model(_prompt(JUDGE_PROMPT, _JUDGE_FALLBACK), win,
                      JUDGE_SCHEMA, JUDGE_MODEL, timeout or JUDGE_TIMEOUT_S)


def author(win: str, reason: str, timeout: int = 0) -> tuple[dict, float]:
    return call_model(_prompt(AUTHOR_PROMPT, _AUTHOR_FALLBACK),
                      f"WHY FLAGGED: {reason}\n\n{win}",
                      AUTHOR_SCHEMA, AUTHOR_MODEL, timeout or AUTHOR_TIMEOUT_S)


_JUDGE_FALLBACK = "Classify whether this moment carries a durable lesson. JSON only."
_AUTHOR_FALLBACK = "Author one lesson row, or refuse. JSON only."


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
    engine does not exist — §2, "refused when no engine can be filled"."""
    engine = raw.get("engine")
    if engine == "matcher":
        m = dict(raw.get("matcher") or {})
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
        o = dict(raw.get("ordering") or {})
        if not _rx_ok(o.get("required_command_rx")):
            return "", {}, "ordering_required_rx_unusable"
        if not _rx_ok(o.get("gated_command_rx")):
            return "", {}, "ordering_gated_rx_unusable"
        events = o.get("armed_by_events")
        if not isinstance(events, list) or not events:
            o["armed_by_events"] = ["edit"]
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
    """(row, refusal_reason). Either a complete row or nothing."""
    if not raw.get("draft"):
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
    """The `state` §2 says the harness stamps and nobody types.

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
    # §4.2: when a turn touches two repos and the lesson names neither, carry
    # the ambiguity so the reviewer sees it instead of a confident wrong value.
    if len(touched) > 1 and repo in ("", None):
        state["touched_repos"] = touched
    elif len(touched) > 1 and target == "":
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
    return Path(base) / f"{session}.jsonl"


def append_draft(path: Path, row: dict) -> None:
    """Append-only, one row per line, never the cached book — `fetch_book`
    rewrites that file wholesale on every 200 (§4.2 step 5)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


# --------------------------------------------------------------------- run
class Trace:
    def __init__(self, path: str = "", quiet: bool = False):
        self.fh = open(path, "a", encoding="utf-8") if path else None
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


def run(doc: dict, args) -> dict:
    trace = Trace(args.trace, quiet=args.quiet)
    session = doc.get("session") or "unknown"
    cwd = doc.get("cwd") or ""
    turns = doc.get("turns") or []
    out_path = drafts_path(session, args.out)
    hook_version = args.hook_version or plugin_version()
    declared_repo = doc.get("repo") or ""

    stats = {
        "session": session, "engineer": doc.get("engineer", ""),
        "source": doc.get("source", ""), "turns": len(turns),
        "router_hits": 0, "router_hit_turns": 0, "judge_calls": 0,
        "judge_yes": 0, "author_calls": 0, "rows": 0,
        "refusals": {}, "router_authored": {}, "router_refused": {},
        "model_errors": 0, "judge_timeouts": 0, "author_timeouts": 0,
        "twins": 0, "budget_stops": 0, "stop_check_moments": {},
        "seconds": 0.0,
    }
    t_start = time.time()
    kept: list[dict] = []
    trace(f"session {session[:12]} | {len(turns)} turns | source "
          f"{doc.get('source')} | drafts -> {out_path}")

    def note_refusal(reason: str, router_kind: str) -> None:
        stats["refusals"][reason] = stats["refusals"].get(reason, 0) + 1
        if router_kind:
            stats["router_refused"][router_kind] = \
                stats["router_refused"].get(router_kind, 0) + 1

    for i, turn in enumerate(turns):
        if args.turn and turn.get("n") != args.turn:
            continue
        prev = turns[i - 1] if i else None
        hits = route(turn, prev)
        if hits:
            stats["router_hits"] += len(hits)
            stats["router_hit_turns"] += 1
        trace(f"\n== turn {turn.get('n')} | user: "
              f"{(turn.get('user') or '')[:70]!r} | router: {hits or '-'}")

        reasons: list[tuple[str, str]] = []
        if hits:
            for kind, _ in hits:
                if kind in NOT_AUTHORED:
                    stats["stop_check_moments"][kind] = \
                        stats["stop_check_moments"].get(kind, 0) + 1
            reasons = [(k, f"router:{k}:{v[:60]}") for k, v in hits
                       if k not in NOT_AUTHORED]
            if not reasons:
                trace("   -> claim-shaped: counted for the built-in Stop "
                      "check (spec §1), not authored")
                continue
        elif not args.no_model:
            try:
                verdict, dt = judge(build_window(turn, prev, {}),
                                    args.judge_timeout)
                stats["judge_calls"] += 1
            except ModelError as exc:
                stats["model_errors"] += 1
                stats["judge_timeouts"] += "timeout" in str(exc)
                trace(f"   judge failed: {exc}")
                continue
            trace(f"   judge ({dt}s): signal={verdict.get('signal')} "
                  f"kind={verdict.get('kind')} derivable={verdict.get('derivable')}"
                  f" | {str(verdict.get('rationale', ''))[:140]}")
            if verdict.get("signal") and not verdict.get("derivable"):
                stats["judge_yes"] += 1
                reasons = [("", f"judge:{verdict.get('kind')}:"
                                f"{str(verdict.get('rationale', ''))[:80]}")]
        if not reasons:
            trace("   -> no signal, nothing authored")
            continue
        if args.no_model:
            trace(f"   -> router-only mode, {len(reasons)} reason(s) not authored")
            continue

        for router_kind, reason in reasons[:2]:
            if len(kept) >= args.budget:
                stats["budget_stops"] += 1
                trace(f"   budget {args.budget} reached, not authoring")
                break
            state_probe = stamp_state(
                session=session, turn=turn, row_engine_target=("", ""),
                cwd=cwd, hook_version=hook_version, env_name=args.env,
                default_repo=declared_repo)
            try:
                raw, dt = author(build_window(turn, prev, state_probe), reason,
                                 args.author_timeout)
                stats["author_calls"] += 1
            except ModelError as exc:
                stats["model_errors"] += 1
                stats["author_timeouts"] += "timeout" in str(exc)
                trace(f"   author failed: {exc}")
                continue

            state = stamp_state(
                session=session, turn=turn,
                row_engine_target=engine_target(raw, turn), cwd=cwd,
                hook_version=hook_version, env_name=args.env,
                default_repo=declared_repo)
            scope = [state["repo"]] if state.get("repo") else []
            row, refusal = build_row(raw, state=state, session=session,
                                     turn_n=turn.get("n"), reason=reason,
                                     scope_repos=scope)
            if row is None:
                note_refusal(refusal, router_kind)
                trace(f"   author refused ({dt}s) [{refusal}]: "
                      f"{str(raw.get('rationale', ''))[:140]}")
                continue
            twin = is_twin(row, kept)
            if twin:
                stats["twins"] += 1
                note_refusal("twin_in_run", router_kind)
                trace(f"   twin of {twin['source_ref']} — dropped: "
                      f"{row['title'][:70]}")
                continue
            kept.append(row)
            stats["rows"] += 1
            if router_kind:
                stats["router_authored"][router_kind] = \
                    stats["router_authored"].get(router_kind, 0) + 1
            append_draft(out_path, row)
            trace(f"   DRAFT ({dt}s) [{row['delivery']}] {row['title'][:80]}\n"
                  f"      {row['statement'][:220]}\n"
                  f"      repo={state['repo']} branch={state['branch']} "
                  f"engine={ {k: v for k, v in row.items() if k in ('matcher', 'ordering', 'anchors')} }")

    stats["seconds"] = round(time.time() - t_start, 1)
    trace(f"\nROWS {stats['rows']} | judge {stats['judge_calls']} "
          f"| author {stats['author_calls']} | refusals {stats['refusals']} "
          f"| twins {stats['twins']} | {stats['seconds']}s")
    trace.close()
    if args.stats:
        Path(args.stats).write_text(
            json.dumps(stats, indent=1, default=str), encoding="utf-8")
    return stats


# -------------------------------------------------------------------- main
def spawn_detached(argv: list[str]) -> int:
    """Re-exec without the --spawn flag, fully detached.

    The caller is a hook with a millisecond budget: it must return before the
    first model call, and the child must survive the session ending.
    """
    args = [sys.executable, str(Path(__file__).resolve())] + \
        [a for a in argv if a != "--spawn"]
    log_dir = Path(os.environ.get("MEMHUB_HARNESS_LOG_DIR") or
                   (Path.home() / ".config" / "memhub-plugin" / "harness"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "extract.log", "a", encoding="utf-8")
    except OSError:
        log = subprocess.DEVNULL
    try:
        kwargs = {"stdin": subprocess.DEVNULL, "stdout": log, "stderr": log,
                  "env": _child_env(), "close_fds": True}
        if hasattr(os, "setsid"):
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
    p.add_argument("--judge-timeout", type=int, default=0,
                   help=f"seconds for a judge call (default {JUDGE_TIMEOUT_S}, spec §4.2)")
    p.add_argument("--author-timeout", type=int, default=0,
                   help=f"seconds for an author call (default {AUTHOR_TIMEOUT_S}, spec §4.2)")
    p.add_argument("--no-model", action="store_true",
                   help="router only — no model calls, nothing authored")
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
