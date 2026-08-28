#!/usr/bin/env python3
"""Backtest a candidate rulebook rule against past Claude Code session transcripts.

Replays the rule over every Bash/Edit/Write tool call recorded in
~/.claude/projects/**/*.jsonl through the live hook's own `evaluate()`
(imported from rulebook_hook.py beside this file) — never a re-implementation,
so the backtest exercises exactly the code that will run. Adds repo_scope
filtering and fire_scope=session dedup (one fire per session).

This is the arming gate: a rule is added only after a human reads the
excerpts this prints and judges them. Stdlib only; read-only.

Usage:
  rulebook_backtest.py --rule '<json>'            # the SAME JSON you will send
                                                  # to create_rule: either the
                                                  # full call body ({"delivery":
                                                  # "agent_hook","matcher":{...},
                                                  # "scope_repos":[...]}) or a
                                                  # bare matcher ({"event":
                                                  # "bash","command_rx":...}).
                                                  # Hook-shaped rows (on/rx/…)
                                                  # are still accepted.
  rulebook_backtest.py --rule-file cand.json
  [--days 30] [--projects ~/.claude/projects] [--max-excerpts 15]
  [--exclude-session <uuid>]                      # ALWAYS pass the current
                                                  # session id: this session's
                                                  # own transcript contains the
                                                  # candidate regex and would
                                                  # self-match.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# the ONE matcher implementation + the ONE server→hook shape conversion
from rulebook_hook import _RX_KEYS, evaluate, scope_ok, to_hook_rule  # noqa: E402

SUPPORTED_ON = ("bash", "edit", "write_stdlib", "result")


def load_rule(args):
    if args.rule_file:
        try:
            with open(args.rule_file, encoding="utf-8") as f:
                raw = f.read()
        except OSError as exc:
            sys.exit(f"cannot read --rule-file {args.rule_file}: {exc}")
    else:
        raw = args.rule
    try:
        cand = json.loads(raw)
    except ValueError as exc:
        sys.exit(f"the candidate rule is not valid JSON: {exc}")
    return normalise_rule(cand)


def normalise_rule(cand):
    """Accept the exact JSON the agent will send to `create_rule` (a full call
    body with `matcher`/`ordering`, or a bare matcher dict with `event`) and
    convert it through the hook's own to_hook_rule() — so the backtest runs
    the rule the server will store, not a hand-translated twin. Hook-shaped
    rows (an `on` key) pass through; on=write is the edit family."""
    if not isinstance(cand, dict):
        sys.exit("the candidate rule must be a JSON object")
    if "on" in cand:
        rule = dict(cand)
        if rule.get("on") == "write":
            rule["on"] = "edit"
        return rule
    row = dict(cand)
    if "matcher" not in row and "ordering" not in row:
        if "event" in row:
            row = {"matcher": row}          # bare matcher block
        else:
            sys.exit("the candidate rule needs `event` (a matcher) or `on` (hook shape); "
                     "anchor rules use --triggers, session_context rules have no backtest.")
    row.setdefault("rule_id", "candidate")
    row.setdefault("statement", row.get("title") or "candidate")
    rule = to_hook_rule(row)
    if rule is None:
        sys.exit("the candidate rule was rejected by the hook's loader (to_hook_rule): "
                 "check the matcher keys and that every *_rx compiles, is < 400 chars, "
                 "and nests no quantifiers.")
    return rule


def repo_of(cwd):
    """Repo segment of a cwd: the path component after /xtrace/, else basename."""
    if not cwd:
        return ""
    parts = cwd.rstrip("/").split("/")
    if "xtrace" in parts:
        i = parts.index("xtrace")
        if i + 1 < len(parts):
            return parts[i + 1]
    return parts[-1] if parts else ""




def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--rule")
    g.add_argument("--rule-file")
    g.add_argument("--triggers", help="comma-separated trigger identifiers "
                   "(directive mode): replayed as case-insensitive substring "
                   "matches over commands, paths, and edited content — the "
                   "honest offline approximation of entity-trigger firing")
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--max-excerpts", type=int, default=15)
    ap.add_argument("--exclude-session", default="")
    args = ap.parse_args()

    if args.triggers:
        trigs = [t.strip() for t in args.triggers.split(",") if t.strip()]
        if not trigs:
            sys.exit("--triggers is empty: an empty alternation would match every command")
        rule = {"id": "directive-triggers", "on": "bash",
                "rx": "|".join(re.escape(t) for t in trigs),
                "match_heredoc_body": False,
                "fire_scope": "session", "repo_scope": "any",
                "_directive_mode": True}
    else:
        rule = load_rule(args)
    on = rule.get("on")
    if on not in SUPPORTED_ON:
        sys.exit(f"backtest supports on=bash|edit|write|write_stdlib|result (got {on!r}); "
                 "session-lane posture rules are not matchers and have no backtest.")
    # Fail fast on a broken regex before scanning anything — every regex key
    # the hook knows, so a bad body_rx/cmd_rx/exclude_rx errors out instead
    # of silently reading as zero-fire.
    for k in _RX_KEYS:
        if rule.get(k):
            try:
                re.compile(rule[k])
            except re.error as exc:
                sys.exit(f"regex in {k!r} does not compile: {exc}")

    import time
    cutoff = time.time() - args.days * 86400
    files = []
    for root, _dirs, names in os.walk(args.projects):
        for n in names:
            if n.endswith(".jsonl"):
                p = os.path.join(root, n)
                if os.path.getmtime(p) >= cutoff:
                    files.append(p)

    scanned_calls = 0
    raw_hits = 0
    session_hits = {}   # session_file -> [excerpt, ...]
    excerpts = []

    for p in sorted(files):
        session = os.path.basename(p)[:-6]
        # match on the prefix the caller GAVE (a full uuid excludes exactly one
        # session; a short one is the caller's choice) — never truncate to 8
        if args.exclude_session and session.startswith(args.exclude_session):
            continue
        try:
            fh = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        # `result` rules need the paired tool_result: index every result by
        # tool_use_id first (parallel calls interleave — never pair by adjacency).
        results = {}
        if on == "result":
            with open(p, encoding="utf-8", errors="replace") as rf:
                for line in rf:
                    if '"tool_result"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    for c in (d.get("message") or {}).get("content") or []:
                        if isinstance(c, dict) and c.get("type") == "tool_result":
                            txt = c.get("content")
                            if isinstance(txt, list):
                                txt = "\n".join(x.get("text", "") for x in txt if isinstance(x, dict))
                            results[c.get("tool_use_id")] = str(txt or "")
        with fh:
            for line in fh:
                if '"tool_use"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                # same scope test the live hook applies: server scope_repos
                # (any names) exact-match the session's repo; "any" → all
                if not scope_ok(rule, repo_of(d.get("cwd", "")), ""):
                    continue
                for c in (d.get("message") or {}).get("content") or []:
                    if not (isinstance(c, dict) and c.get("type") == "tool_use"):
                        continue
                    tool = c.get("name")
                    inp = c.get("input") or {}
                    hit = False
                    excerpt = ""
                    if on == "bash" and tool == "Bash":
                        cmd = str(inp.get("command", ""))
                        if not cmd:
                            continue
                        scanned_calls += 1
                        hit = evaluate(rule, hook_phase="pre", tool="Bash", cmd=cmd)
                        excerpt = cmd
                    elif rule.get("_directive_mode") and tool in ("Edit", "Write", "MultiEdit"):
                        fp = str(inp.get("file_path", ""))
                        body = str(inp.get("new_string", "")) + str(inp.get("content", ""))
                        scanned_calls += 1
                        hit = bool(re.search(rule["rx"], fp + "\n" + body, re.I))
                        excerpt = fp
                    elif on == "edit" and tool in ("Edit", "Write", "MultiEdit"):
                        fp = str(inp.get("file_path", ""))
                        body = str(inp.get("new_string", "")) + str(inp.get("content", ""))
                        if not fp:
                            continue
                        scanned_calls += 1
                        hit = evaluate(rule, hook_phase="pre", tool=tool, file_path=fp, body=body)
                        excerpt = fp
                    elif on == "write_stdlib" and tool == "Write":
                        fp = str(inp.get("file_path", ""))
                        body = str(inp.get("content", ""))
                        scanned_calls += 1
                        hit = evaluate(rule, hook_phase="pre", tool=tool, file_path=fp, body=body)
                        excerpt = fp
                    elif on == "result" and tool == "Bash":
                        cmd = str(inp.get("command", ""))
                        rtext = results.get(c.get("id"), "")
                        if not rtext:
                            continue
                        scanned_calls += 1
                        hit = evaluate(rule, hook_phase="post", tool=tool, cmd=cmd, result_text=rtext)
                        excerpt = cmd
                    if hit:
                        raw_hits += 1
                        first = p not in session_hits
                        session_hits.setdefault(p, []).append(excerpt)
                        if first and len(excerpts) < args.max_excerpts:
                            excerpts.append({
                                "session": session[:8],
                                "ts": d.get("timestamp", ""),
                                "repo": repo_of(d.get("cwd", "")),
                                "excerpt": excerpt[:200],
                            })

    fire_scope = rule.get("fire_scope", "session")
    fires = raw_hits if fire_scope == "call" else len(session_hits)
    print(json.dumps({
        "rule_id": rule.get("id"),
        "window_days": args.days,
        "sessions_scanned": len(files),
        "tool_calls_scanned": scanned_calls,
        "raw_hits": raw_hits,
        "sessions_with_hit": len(session_hits),
        "fires_at_fire_scope": fires,
        "fire_scope": fire_scope,
        "excerpts": excerpts,
    }, indent=1))
    # Verdict summary for the skill's table / report. The backtest is the
    # CLIENT-side arming gate; the server does not store it and create_rule
    # does not take it. Fill judged_tp / judged_fp AFTER reading the excerpts.
    print("\nVerdict summary for your report (judged_tp + judged_fp = "
          "sessions you read):", file=sys.stderr)
    print(json.dumps({"backtest": {
        "sessions": len(files), "hits": len(session_hits), "days": args.days,
        "judged_tp": 0, "judged_fp": 0,
        "note": "FILL judged_tp/judged_fp from the excerpts before filing",
    }}, indent=1))
    if raw_hits == 0:
        print("\nZERO-FIRE: no hits in the window. Fine for rare/high-blast "
              "tripwires; say so explicitly when arming.", file=sys.stderr)
    else:
        print(f"\nJUDGE THE EXCERPTS: each is the FIRST hit of a session "
              f"({len(session_hits)} sessions hit). Count true vs false "
              "positives by hand before arming.", file=sys.stderr)


if __name__ == "__main__":
    main()
