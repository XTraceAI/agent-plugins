#!/usr/bin/env python3
"""Backtest a candidate rulebook rule against past Claude Code session transcripts.

Replays the rule's matcher over every Bash/Edit/Write tool call recorded in
~/.claude/projects/**/*.jsonl, with the SAME semantics as the live hook
(~/.claude/scripts/rulebook/rulebook_hook.py): pre-heredoc segment for bash
rules unless match_heredoc_body, rx/not_rx with re.I|re.M, path_rx /
path_not_rx / content_rx for edit rules, repo_scope filtering, and
fire_scope=session dedup (one fire per session).

This is the arming gate: a rule is added only after a human reads the
excerpts this prints and judges them. Stdlib only; read-only.

Usage:
  rulebook_backtest.py --rule '<json>'            # candidate rule JSON
  rulebook_backtest.py --rule-file cand.json
  rulebook_backtest.py --id ruff-unpinned         # replay an existing rule
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

RULEBOOK = os.path.expanduser("~/.claude/scripts/rulebook/rulebook.json")


def load_rule(args):
    if args.id:
        with open(RULEBOOK) as f:
            for r in json.load(f)["rules"]:
                if r["id"] == args.id:
                    return r
        sys.exit(f"no rule '{args.id}' in {RULEBOOK}")
    raw = args.rule or open(args.rule_file).read()
    return json.loads(raw)


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


def match_bash(rule, cmd):
    target = cmd if rule.get("match_heredoc_body") else cmd.split("<<", 1)[0]
    if not re.search(rule["rx"], target, re.I | re.M):
        return False
    if rule.get("not_rx") and re.search(rule["not_rx"], target, re.I):
        return False
    return True


def match_edit(rule, fp, body):
    if not re.search(rule["path_rx"], fp):
        return False
    if rule.get("path_not_rx") and re.search(rule["path_not_rx"], fp):
        return False
    if "content_rx" in rule and not re.search(rule["content_rx"], body, re.M):
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--rule")
    g.add_argument("--rule-file")
    g.add_argument("--id")
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--max-excerpts", type=int, default=15)
    ap.add_argument("--exclude-session", default="")
    args = ap.parse_args()

    rule = load_rule(args)
    on = rule.get("on")
    if on not in ("bash", "edit"):
        sys.exit(f"backtest supports on=bash|edit for now (got {on!r}); "
                 "result/write_stdlib rules need paired tool results — arm those "
                 "from live advisory data instead.")
    # Fail fast on a broken regex before scanning anything.
    for k in ("rx", "not_rx", "path_rx", "path_not_rx", "content_rx"):
        if rule.get(k):
            re.compile(rule[k])

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
    scope = rule.get("repo_scope", "any")

    for p in sorted(files):
        session = os.path.basename(p)[:-6]
        if args.exclude_session and session.startswith(args.exclude_session[:8]):
            continue
        try:
            fh = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
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
                if scope == "xmem" and "xmem" not in repo_of(d.get("cwd", "")):
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
                        hit = match_bash(rule, cmd)
                        excerpt = cmd
                    elif on == "edit" and tool in ("Edit", "Write", "MultiEdit"):
                        fp = str(inp.get("file_path", ""))
                        body = str(inp.get("new_string", "")) + str(inp.get("content", ""))
                        if not fp:
                            continue
                        scanned_calls += 1
                        hit = match_edit(rule, fp, body)
                        excerpt = fp
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
    if raw_hits == 0:
        print("\nZERO-FIRE: no hits in the window. Fine for rare/high-blast "
              "tripwires; say so explicitly when arming.", file=sys.stderr)
    else:
        print(f"\nJUDGE THE EXCERPTS: each is the FIRST hit of a session "
              f"({len(session_hits)} sessions hit). Count true vs false "
              "positives by hand before arming.", file=sys.stderr)


if __name__ == "__main__":
    main()
