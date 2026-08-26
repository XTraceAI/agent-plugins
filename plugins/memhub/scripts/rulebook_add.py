#!/usr/bin/env python3
"""Validate and append one rule to the local rulebook (~/.claude/scripts/rulebook/rulebook.json).

The write half of /memhub:create-rule. Validates against the live hook's
matcher contract, refuses duplicate ids, backs up the rulebook, and writes
atomically. New rules always land as advisory — the hook has no blocking
path, and tier promotion is a separate, admin-only, evidence-gated step.

Usage:
  rulebook_add.py --rule '<json>' [--dry-run] [--rulebook <path>]
  echo '<json>' | rulebook_add.py [--dry-run]

Extra provenance fields (brain, source_ref, backtest, created_at) are stored
verbatim; the hook ignores unknown keys. Exits non-zero with a plain-English
reason on any validation failure.
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

RULEBOOK = os.path.expanduser("~/.claude/scripts/rulebook/rulebook.json")

REQUIRED_BY_ON = {
    "bash": ["rx"],
    "edit": ["path_rx"],
    "result": ["rx"],
    "session": [],   # posture rule: served at SessionStart, no matcher
}
OPTIONAL_RX = ["not_rx", "path_rx", "path_not_rx", "content_rx", "cmd_rx", "exclude_rx", "rx"]
KNOWN_SCOPES = ("call", "session", "branch")


def fail(msg):
    sys.exit(f"REJECTED: {msg}")


def validate(rule, existing):
    for field in ("id", "on", "text", "why", "fire_scope", "repo_scope"):
        if not rule.get(field):
            fail(f"missing required field '{field}'")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", rule["id"]):
        fail(f"id '{rule['id']}' must be kebab-case")
    if rule["id"] in {r["id"] for r in existing}:
        fail(f"id '{rule['id']}' already exists in the rulebook")
    on = rule["on"]
    if on not in REQUIRED_BY_ON:
        fail(f"on='{on}' — the hook evaluates bash | edit | result | session "
             "(write_stdlib is bespoke; edit it by hand)")
    for field in REQUIRED_BY_ON[on]:
        if not rule.get(field):
            fail(f"on='{on}' requires '{field}'")
    for field in OPTIONAL_RX:
        if rule.get(field):
            try:
                re.compile(rule[field])
            except re.error as e:
                fail(f"'{field}' does not compile: {e}")
    fs = rule["fire_scope"]
    if fs not in KNOWN_SCOPES and not re.fullmatch(r"counter:\d+", fs):
        fail(f"fire_scope '{fs}' — use call | session | branch | counter:N")
    if rule["repo_scope"] not in ("xmem", "any"):
        fail("repo_scope must be 'xmem' or 'any' (the only values the hook checks)")
    if rule.get("mode") == "gate" or rule.get("tier") == "gate":
        fail("new rules always land advisory; gating is a separate admin step")
    if len(rule["text"]) > 160:
        fail("text is the one-line advisory shown in-session; keep it <=160 chars")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule")
    ap.add_argument("--rulebook", default=RULEBOOK)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    raw = args.rule if args.rule else sys.stdin.read()
    try:
        rule = json.loads(raw)
    except ValueError as e:
        fail(f"rule is not valid JSON: {e}")

    try:
        with open(args.rulebook, encoding="utf-8") as f:
            book = json.load(f)
        book["rules"]
    except FileNotFoundError:
        fail(f"no rulebook at {args.rulebook} — create it as {{\"version\": 1, \"rules\": []}} first")
    except (ValueError, KeyError, TypeError) as e:
        fail(f"rulebook at {args.rulebook} is not valid ({e}); fix it before adding rules")
    validate(rule, book["rules"])
    rule.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "rule": rule}, indent=1))
        return

    backup = f"{args.rulebook}.bak.{time.strftime('%Y%m%d%H%M%S')}"
    shutil.copy2(args.rulebook, backup)
    book["rules"].append(rule)
    tmp = args.rulebook + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(book, f, indent=1)
        f.write("\n")
    os.replace(tmp, args.rulebook)
    print(json.dumps({"ok": True, "rule_id": rule["id"],
                      "rules_total": len(book["rules"]), "backup": backup},
                     indent=1))
    print("\nLIVE NOW: the hook re-reads the rulebook on every tool call — "
          "no restart needed.", file=sys.stderr)


if __name__ == "__main__":
    main()
