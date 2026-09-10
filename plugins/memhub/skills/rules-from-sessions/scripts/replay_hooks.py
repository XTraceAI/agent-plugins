#!/usr/bin/env python3
"""Replay one recorded session through the INSTALLED hook and the extractor —
offline, one command, nothing written to any server.

    replay_hooks.py --session <id|path>                  a local Claude Code session
    replay_hooks.py --turns   <turns.json>               a staging session (no local file)
    replay_hooks.py --session <id> --hooks-only          just the rulebook hook
    replay_hooks.py --turns   <t.json> --extract-only    just the extraction pipeline

Two halves, and they answer different questions:

  hooks    feeds the recorded events to `rulebook_hook.py` one at a time, in an
           ISOLATED state root, with `transcript_path` growing exactly as it did
           live. Answers "what would the book have said, and when" — fires,
           blocks, latency, and whether a fire preceded each correction. Reads
           the hook; never changes it.

  extract  runs `harness_extract.py` over the same session. Answers "what would
           the harness have drafted". Drafts go to a LOCAL file under --out.

The hook half needs a local transcript, because the hook's whole job is to read
one. A staging session has no transcript, so `--turns` runs the extract half
only, and says so rather than pretending it replayed hooks.

Nothing here activates a rule, files a proposal, or writes to staging.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
# The installed hook runs on Read too (the read lane, v0.49), and the author
# can write `matcher.event = "read"`; a replay that skipped Read calls
# reported those rules as never firing (Codex, #191).
REPLAYED_TOOLS = EDIT_TOOLS | {"Bash", "Read"}
SYS = re.compile(r"<system-reminder>.*?</system-reminder>"
                 r"|<task-notification>.*?</task-notification>", re.S)
# Correction shape, for the "did a fire precede this?" column only. Not the
# router, not scored.
CORR = re.compile(
    r"^(no|nope|wrong|wait|stop|nah)\b"
    r"|\b(i mean|i said|i meant|u are (still )?wrong|why did (u|you)"
    r"|check staging|on staging|test on staging"
    r"|have u (actually |really )?(run|test|fix|verif)"
    r"|is it (actually |really )?working|so have u|did u (just|actually)"
    r"|we already have|don'?t we already|already exists|try again"
    r"|still (not|broken|fail))\b", re.I)


def plugin_scripts() -> str:
    """The installed plugin's scripts directory. Same resolution order
    `mine_sessions.py` uses — shipped copy first, then an installed one."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.environ.get("MEMHUB_PLUGIN_SCRIPTS"),
                 os.path.join(os.environ.get("CLAUDE_PLUGIN_ROOT", ""), "scripts"),
                 os.path.normpath(os.path.join(here, "..", "..", "..", "scripts"))):
        if cand and os.path.isfile(os.path.join(cand, "rulebook_hook.py")):
            return cand
    found = (glob.glob(os.path.expanduser(
                "~/.claude/plugins/cache/*/memhub*/*/scripts/rulebook_hook.py"))
             + glob.glob(os.path.expanduser(
                "~/.claude/plugins/*/plugins/memhub*/scripts/rulebook_hook.py"))
             + glob.glob(os.path.expanduser(
                "~/.codex/plugins/*/memhub*/scripts/rulebook_hook.py")))
    if not found:
        sys.exit("memhub plugin scripts not found; set MEMHUB_PLUGIN_SCRIPTS")
    if len(found) > 1:
        print(f"[warn] several memhub copies; using newest: "
              f"{max(found, key=os.path.getmtime)}", file=sys.stderr)
    return os.path.dirname(max(found, key=os.path.getmtime))


def locate_session(ref: str) -> Path:
    """A session id, a prefix of one, or a path."""
    p = Path(os.path.expanduser(ref))
    if p.is_file():
        return p
    matches = sorted(
        glob.glob(os.path.expanduser(f"~/.claude/projects/*/{ref}*.jsonl")),
        key=os.path.getmtime)
    if not matches:
        sys.exit(f"no local transcript for {ref!r}")
    return Path(matches[-1])


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in (content or [])
                     if isinstance(b, dict) and b.get("type") == "text")


# ------------------------------------------------------------- hook replay
def run_hook(hook: Path, phase: str, payload: dict, env: dict, cwd: str,
             timeout: int = 8):
    t0 = time.time()
    try:
        proc = subprocess.run([sys.executable, str(hook), phase],
                              input=json.dumps(payload), capture_output=True,
                              text=True, env=env, cwd=cwd, timeout=timeout)
        out, err = proc.stdout.strip(), proc.stderr.strip()[-300:]
    except subprocess.TimeoutExpired:
        out, err = "", "TIMEOUT"
    except OSError as exc:
        out, err = "", f"SPAWN {exc!r}"
    dt = time.time() - t0
    parsed = None
    if out:
        try:
            parsed = json.loads(out)
        except ValueError:
            parsed = {"raw": out[:300]}
    return dt, parsed, err


def fired(out) -> tuple[list[str], bool]:
    if not isinstance(out, dict):
        return [], False
    spec = out.get("hookSpecificOutput", {}) or {}
    ctx = spec.get("additionalContext", "") or ""
    msg = out.get("systemMessage", "") or ""
    # The hook names a fired rule on BOTH channels (additionalContext and
    # systemMessage), so one fire appears at least twice in the concatenation;
    # count each label once per response (Codex, #191).
    titles = list(dict.fromkeys(re.findall(r"\[([^\]]{3,80})\]", ctx + " " + msg)))
    blocked = (spec.get("permissionDecision") == "deny") or ("Blocked" in msg)
    return titles, blocked


def replay_hooks(transcript: Path, scripts: str, keep: bool) -> dict:
    hook = Path(scripts) / "rulebook_hook.py"
    records = [json.loads(l) for l in
               transcript.read_text(errors="replace").splitlines() if l.strip()]
    cwd = next((r.get("cwd") for r in records if isinstance(r.get("cwd"), str)), None)
    if not cwd or not Path(cwd).exists():
        print(f"[hooks] session cwd missing ({cwd!r}); skipping hook replay")
        return {}

    base = Path(tempfile.mkdtemp(prefix="memhub-replay-"))
    real_book = Path.home() / ".config" / "memhub-plugin" / "rulebook" / "book"
    if real_book.is_dir():
        shutil.copytree(real_book, base / "book")
    else:
        (base / "book").mkdir()
    (base / "ledger").mkdir()
    (base / "state").mkdir()
    # An isolated state root, fetch and recall off: the replay must not mutate
    # the real ledger, must not reach the server, and must not depend on it.
    env = dict(os.environ, MEMHUB_RULEBOOK_BASE=str(base),
               MEMHUB_RULEBOOK_FETCH="0", MEMHUB_RULEBOOK_RECALL="0",
               CLAUDE_PROJECT_DIR=cwd)

    sid = str(uuid.uuid4())
    tpath = base / f"{sid}.jsonl"
    common = {"session_id": sid, "transcript_path": str(tpath), "cwd": cwd}
    calls, lat, user_turns = [], [], []
    pending: dict[str, tuple] = {}
    turn = 0

    dt, out, _err = run_hook(hook, "session",
                             dict(common, hook_event_name="SessionStart"),
                             env, cwd)
    lat.append(dt)
    start_ctx = ""
    if isinstance(out, dict):
        start_ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")

    with tpath.open("a", encoding="utf-8") as tf:
        for rec in records:
            # The hook sees exactly the prefix it would have seen live.
            tf.write(json.dumps(rec) + "\n")
            tf.flush()
            kind = rec.get("type")
            content = (rec.get("message") or {}).get("content")
            if kind == "user":
                if isinstance(content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in content):
                    for b in content:
                        if not (isinstance(b, dict)
                                and b.get("type") == "tool_result"):
                            continue
                        got = pending.pop(b.get("tool_use_id"), None)
                        if not got:
                            continue
                        _t, name, inp = got
                        body = b.get("content")
                        body = body if isinstance(body, str) else text_of(body)
                        if name == "Bash":
                            resp = ({"stdout": "", "stderr": body, "interrupted": False}
                                    if b.get("is_error")
                                    else {"stdout": body, "stderr": "", "interrupted": False})
                        else:
                            resp = body
                        dt, out, err = run_hook(
                            hook, "post",
                            dict(common, hook_event_name="PostToolUse",
                                 tool_name=name, tool_input=inp,
                                 tool_response=resp), env, cwd)
                        lat.append(dt)
                        calls.append({"turn": turn, "phase": "post",
                                      "tool": name, "dt": dt, "out": out,
                                      "err": err})
                    continue
                txt = SYS.sub("", text_of(content)).strip()
                if not txt or txt.startswith(("<", "#")) \
                        or "Base directory for this skill" in txt:
                    continue
                turn += 1
                user_turns.append({"turn": turn,
                                   "text": txt[:160].replace("\n", " "),
                                   "correction": bool(CORR.search(txt[:300]))})
            elif kind == "assistant" and isinstance(content, list):
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    name = b.get("name")
                    inp = b.get("input") or {}
                    if name not in REPLAYED_TOOLS:
                        continue
                    pending[b.get("id")] = (turn, name, inp)
                    dt, out, err = run_hook(
                        hook, "pre",
                        dict(common, hook_event_name="PreToolUse",
                             tool_name=name, tool_input=inp), env, cwd)
                    lat.append(dt)
                    calls.append({"turn": turn, "phase": "pre", "tool": name,
                                  "dt": dt, "out": out, "err": err,
                                  "cmd": str(inp.get("command")
                                             or inp.get("file_path") or "")[:80]})

    fires = collections.Counter()
    per_turn = collections.defaultdict(list)
    blocks = 0
    for call in calls:
        titles, blocked = fired(call["out"])
        for t in titles:
            fires[t] += 1
            per_turn[call["turn"]].append(t)
        if blocked:
            blocks += 1
            per_turn[call["turn"]].append("BLOCK")
    ledger = base / "ledger" / "fires.jsonl"
    n_ledger = sum(1 for _ in ledger.open()) if ledger.exists() else 0

    print(f"\n[hooks] {transcript.stem[:8]} cwd={cwd}")
    print(f"[hooks] calls {len(calls)} "
          f"(pre {sum(1 for c in calls if c['phase'] == 'pre')}, "
          f"post {sum(1 for c in calls if c['phase'] == 'post')}) | "
          f"latency p50 {statistics.median(lat) * 1000:.0f}ms "
          f"p90 {sorted(lat)[int(len(lat) * .9)] * 1000:.0f}ms "
          f"max {max(lat) * 1000:.0f}ms | "
          f"timeouts {sum(1 for c in calls if c['err'] == 'TIMEOUT')}")
    print(f"[hooks] session-start context {len(start_ctx)} chars | "
          f"fires {sum(fires.values())} | blocks {blocks} | "
          f"ledger rows {n_ledger}")
    print(f"[hooks] fires by rule: {dict(fires.most_common(8))}")
    print("[hooks] user turns (C = correction-shaped) and what fired before:")
    for u in user_turns:
        before = per_turn.get(u["turn"] - 1, [])
        print(f"  {'C' if u['correction'] else ' '} t{u['turn']:>2} "
              f"fired-before={before[:3] if before else '-'} | {u['text'][:90]}")

    if keep:
        print(f"[hooks] state root kept at {base}")
    else:
        shutil.rmtree(base, ignore_errors=True)
    return {"calls": len(calls), "fires": sum(fires.values()),
            "blocks": blocks, "ledger_rows": n_ledger,
            "latency_p90_ms": round(sorted(lat)[int(len(lat) * .9)] * 1000),
            "user_turns": len(user_turns),
            "correction_turns": sum(1 for u in user_turns if u["correction"])}


# --------------------------------------------------------------- extractor
def replay_extract(scripts: str, *, transcript: Path | None,
                   turns_json: Path | None, out_dir: Path, budget: int,
                   no_model: bool, env_name: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = (turns_json.stem if turns_json else transcript.stem)
    drafts = out_dir / f"{name}.drafts.jsonl"
    stats = out_dir / f"{name}.stats.json"
    trace = out_dir / f"{name}.trace.log"
    cmd = [sys.executable, str(Path(scripts) / "harness_extract.py"),
           "--out", str(drafts), "--stats", str(stats), "--trace", str(trace),
           "--budget", str(budget), "--env", env_name]
    cmd += ["--turns", str(turns_json)] if turns_json \
        else ["--transcript", str(transcript)]
    if no_model:
        cmd.append("--no-model")
    print(f"\n[extract] {' '.join(cmd[2:])}")
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        print(f"[extract] exit {proc.returncode}")
    try:
        return json.loads(stats.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--session", help="local session id, id prefix, or path")
    src.add_argument("--turns", help="canonical turns JSON (staging adapter)")
    p.add_argument("--out", default="replay-out", help="output directory")
    p.add_argument("--budget", type=int, default=8)
    p.add_argument("--env", default="staging",
                   help="environment name for the state stamp")
    p.add_argument("--hooks-only", action="store_true")
    p.add_argument("--extract-only", action="store_true")
    p.add_argument("--no-model", action="store_true",
                   help="router only — no model calls")
    p.add_argument("--keep-state", action="store_true",
                   help="keep the isolated hook state root for inspection")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    scripts = plugin_scripts()
    out_dir = Path(args.out)
    transcript = locate_session(args.session) if args.session else None
    turns_json = Path(args.turns) if args.turns else None
    summary: dict = {"scripts": scripts}

    if not args.extract_only:
        if transcript is None:
            print("[hooks] skipped: a staging session has no local transcript, "
                  "and the hook's job is to read one.")
        else:
            summary["hooks"] = replay_hooks(transcript, scripts, args.keep_state)
    if not args.hooks_only:
        summary["extract"] = replay_extract(
            scripts, transcript=transcript, turns_json=turns_json,
            out_dir=out_dir, budget=args.budget, no_model=args.no_model,
            env_name=args.env)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "replay_summary.json").write_text(
        json.dumps(summary, indent=1, default=str), encoding="utf-8")
    print(f"\n-> {out_dir}/replay_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
