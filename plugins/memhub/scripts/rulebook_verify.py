#!/usr/bin/env python3
"""Check a candidate rule against the engine that will actually run it.

A rule can be well-formed, match the command you had in mind, and still be
wrong in ways nobody sees until it is live and nagging the whole team:

  * it fires on a command that merely MENTIONS the pattern (`grep "git push"`),
    which is the largest false-fire class we have measured;
  * it still fires after the author fixes the thing it asked for, so complying
    with the rule does not silence it;
  * its pattern is too long or backtracks, so the hook drops the WHOLE rule at
    load time — silently, on every teammate's machine.

So this runs the candidate through `rulebook_hook.to_hook_rule()` and
`evaluate()` — the same functions the live hook uses, never a re-implementation —
against cases the author supplies. Exit 0 = every case behaved; exit 1 = at
least one did not, and the table says which.

  rulebook_verify.py --rule '<create_rule JSON>' \
      --fires 'gh pr merge 7' \
      --silent 'grep -rn "gh pr merge" docs/' \
      --silent 'ls -la'

  rulebook_verify.py --rule-file cand.json --cases cases.json

For an `edit` / `write` rule, a case is `path::content`:

  --fires '/repo/src/db.py::conn = connect(url, verify=False)' \
  --silent '/repo/src/db.py::conn = connect(url)'

Stdlib only; no network; no server concepts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rulebook_hook as H  # noqa: E402  (path set above so the engine is importable)


# `create_rule` takes the matcher nested; the hook reads a flat row. Build the
# `?view=hook` shape so `to_hook_rule` does the same translation it does live.
def _hook_row(rule: dict) -> dict:
    row = dict(rule)
    row.setdefault("rule_id", "candidate")
    row.setdefault("status", "active")
    row.setdefault("version", 1)
    row.setdefault("delivery", "agent_hook" if (rule.get("matcher") or rule.get("ordering"))
                   else "session_context")
    return row


def _split_case(raw: str) -> tuple[str, str]:
    """`path::content` for edit/write rules; everything else is a command."""
    if "::" in raw:
        path, _, content = raw.partition("::")
        return path, content
    return "", raw


def _fires(hook_rule: dict, raw: str) -> bool:
    on = hook_rule.get("on")
    path, content = _split_case(raw)
    if on in ("edit", "write", "write_stdlib"):
        return H.evaluate(hook_rule, hook_phase="pre", tool="Edit",
                          file_path=path or "/repo/file.py", body=content)
    if on in ("result", "output"):
        return H.evaluate(hook_rule, hook_phase="post", tool="Bash",
                          cmd=path or "pytest", result_text=content)
    return H.evaluate(hook_rule, hook_phase="pre", tool="Bash", cmd=content)


def verify(rule: dict, fires: list[str], silent: list[str]) -> tuple[bool, bool, list[str]]:
    """(every case behaved, the rule is fire-testable at all, the report lines).

    A `session_context` rule has no matcher, so it is not fire-testable and
    must not be held to the "prove it fires" requirement.
    """
    out: list[str] = []

    # 1. The load gate. A rule that does not survive this never runs at all,
    #    and the hook says nothing when it drops one.
    hook_rule = H.to_hook_rule(_hook_row(rule))
    if hook_rule is None:
        out.append("LOAD   FAIL  the hook would drop this rule at load time")
        matcher = rule.get("matcher") or {}
        for key, pat in matcher.items():
            if key.endswith("_rx") and not H.rx_ok(pat):
                why = ("longer than %d characters" % H._RX_MAX) if len(str(pat)) > H._RX_MAX \
                    else "does not compile, or backtracks catastrophically"
                out.append("             %s: %s" % (key, why))
        if not any(l.startswith("             ") for l in out):
            out.append("             no engine block, or two of them, or a missing id")
        return False, False, out
    out.append("LOAD   ok    the hook loads it (patterns compile, within bounds)")

    if hook_rule.get("on") == "session":
        out.append("NOTE         a session_context rule has no matcher — nothing to fire-test")
        return True, False, out

    ok = True
    for case in fires:
        hit = _fires(hook_rule, case)
        ok &= hit
        out.append("FIRES  %-5s %s" % ("ok" if hit else "FAIL", case[:88]))
    for case in silent:
        hit = _fires(hook_rule, case)
        ok &= not hit
        out.append("SILENT %-5s %s" % ("FAIL" if hit else "ok", case[:88]))
    return ok, True, out


def _literal_of(rx: str) -> str:
    """A plausible command fragment the pattern would match.

    Only for generating the self-mention cases, so it does not need to be a
    regex inverse — it needs to be something a person would actually type.
    Whitespace classes become a space, anchors and word boundaries drop out,
    escapes lose their backslash, and anything still regex-shaped (a class, a
    group, a quantifier) means we cannot guess honestly, so we give up rather
    than emit a nonsense literal that passes vacuously.
    """
    out, i = [], 0
    while i < len(rx):
        c = rx[i]
        if c == "\\" and i + 1 < len(rx):
            nxt = rx[i + 1]
            if nxt == "s":
                out.append(" ")
            elif nxt in "bAZzGB<>":
                pass                      # a zero-width assertion types as nothing
            elif nxt.isalnum():
                return ""                 # \d, \w, \S … — a class, not a literal
            else:
                out.append(nxt)           # an escaped literal: \. \- \/ …
            i += 2
            if i < len(rx) and rx[i] in "+*?":
                i += 1                    # the quantifier on what we just took
            continue
        if c in "[](){}|.*+?^$":
            return ""                     # real regex structure — do not guess
        out.append(c)
        i += 1
    return " ".join("".join(out).split())


def _self_mention(rule: dict) -> list[str]:
    """The cases an author reliably forgets. A pattern almost never wants to
    match a command that only quotes it — searching for a rule's own trigger is
    how you investigate it, and firing there trains people to ignore the rule."""
    matcher = rule.get("matcher") or {}
    rx = matcher.get("command_rx")
    if not rx or matcher.get("event") not in (None, "bash"):
        return []
    literal = _literal_of(rx)
    if len(literal) < 4:
        return []                         # nothing honest to build a case from
    return ['grep -rn "%s" .' % literal,
            "python3 -c 'print(\"%s\")'" % literal]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--rule", help="the candidate as the JSON you would send to create_rule")
    src.add_argument("--rule-file", help="that JSON, in a file")
    ap.add_argument("--fires", action="append", default=[],
                    metavar="CASE", help="must fire (repeatable); 'path::content' for edit rules")
    ap.add_argument("--silent", action="append", default=[],
                    metavar="CASE", help="must NOT fire (repeatable)")
    ap.add_argument("--cases", help='JSON file: {"fires": [...], "silent": [...]}')
    ap.add_argument("--no-self-mention", action="store_true",
                    help="skip the generated grep / python -c cases")
    args = ap.parse_args()

    raw = args.rule
    if args.rule_file:
        with open(args.rule_file, encoding="utf-8") as f:
            raw = f.read()
    try:
        rule = json.loads(raw)
    except ValueError as exc:
        print("the rule is not valid JSON: %s" % exc, file=sys.stderr)
        return 2

    fires, silent = list(args.fires), list(args.silent)
    if args.cases:
        with open(args.cases, encoding="utf-8") as f:
            extra = json.load(f)
        fires += list(extra.get("fires") or [])
        silent += list(extra.get("silent") or [])
    generated = [] if args.no_self_mention else _self_mention(rule)
    silent += generated

    ok, testable, lines = verify(rule, fires, silent)
    print("\n".join(lines))

    if generated:
        print("\n(the last %d SILENT cases were generated: a rule should not fire on a\n"
              " command that merely mentions its own trigger)" % len(generated))
    # Independent of the cases above: an author who supplied none has not shown
    # the rule can trigger at all, and needs telling even when something else
    # already failed.
    if testable and not fires:
        print("\nNo --fires case given, so nothing proved this rule CAN fire. Add one.")
        ok = False
    if ok:
        print("\nAll cases behaved. Worth adding one more --silent: the form of the\n"
              "problem AFTER someone fixes it. A rule that still fires once you have\n"
              "complied is one people learn to ignore.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
