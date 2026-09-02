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

So this runs the candidate through `rulebook_hook.to_hook_rule()`,
`evaluate()`, `given_ok()` and `OrderingEngine` — the same code the live hook
uses, never a re-implementation — against cases the author supplies. Exit 0 =
every case behaved; exit 1 = at least one did not, and the table says which.

  rulebook_verify.py --rule '<create_rule JSON>' \
      --fires 'gh pr merge 7' \
      --silent 'grep -rn "gh pr merge" docs/' \
      --silent 'ls -la'

  rulebook_verify.py --rule-file cand.json --cases cases.json

For an `edit` / `write` rule, a case is `path::content`:

  --fires '/repo/src/db.py::conn = connect(url, verify=False)' \
  --silent '/repo/src/db.py::conn = connect(url)'

A rule with a `given` block needs the facts it asks about. Give them once for
every case (`--branch`, `--diff-path`, `--diff-lines`, `--dirty`,
`--user-said`), or per case in a `--cases` file, where a case may be an
object: {"case": "git push", "branch": "main", "diff_paths": [...],
"diff_lines": 620, "dirty": true, "user_said": ["please push it"]}.
No git runs and no transcript is read — the fixture IS the repo.

An `ordering` rule is verified as a SEQUENCE of steps joined by ` >> `, ending
in the gated call; the case fires when that call is gated:

  --fires  'edit:src/a.py >> gate:git push'
  --silent 'edit:src/a.py >> ok:pytest tests/ >> gate:git push'
  --fires  'edit:src/a.py >> red:pytest tests/ >> gate:git push'

Stdlib only; no network; no server concepts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rulebook_hook as H  # noqa: E402  (path set above so the engine is importable)

_FIXTURE_KEYS = ("branch", "diff_paths", "diff_lines", "dirty", "user_said")


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


def _case(raw, base_fixture: dict | None) -> tuple[str, dict]:
    """A case is a string, or an object carrying its own facts. Per-case facts
    override the ones given for every case."""
    fixture = dict(base_fixture or {})
    if isinstance(raw, dict):
        fixture.update({k: raw[k] for k in _FIXTURE_KEYS if k in raw})
        raw = str(raw.get("case", ""))
    return str(raw), fixture


def _probes(fixture: dict) -> "H.Probes":
    """A Probes that answers from the fixture and never touches git or a
    transcript: a fact the fixture does not give is None, which is exactly
    what the live hook sees when a probe fails — the rule stays silent."""
    pre = {"branch": fixture.get("branch"),
           "diff_paths": list(fixture["diff_paths"]) if "diff_paths" in fixture else None,
           "diff_lines": fixture.get("diff_lines"),
           "dirty": fixture.get("dirty"),
           "user_turns": list(fixture["user_said"]) if "user_said" in fixture else None,
           "base": None}
    return H.Probes("", pre["branch"], fixture=pre)


def _ordering_fires(hook_rule: dict, raw: str) -> bool:
    """Replay `step >> step >> gate:cmd` through the real OrderingEngine in a
    throwaway state dir. Steps: `edit:<path>`, `ok:<cmd>` (green receipt),
    `red:<cmd>` (red receipt), `gate:<cmd>` (the gated pre-call). The case
    fires when the LAST gate step is gated."""
    steps = [s.strip() for s in raw.split(">>") if s.strip()]
    rule = dict(hook_rule)
    outcome = None
    saved = H.BASE
    with tempfile.TemporaryDirectory() as td:
        H.BASE = td
        try:
            eng = H.OrderingEngine("/verify-worktree", "*")
            for step in steps:
                kind, _, arg = step.partition(":")
                kind, arg = kind.strip(), arg.strip()
                if kind == "edit":
                    outcome = eng.feed(rule, hook_phase="post", tool="Edit", file_path=arg)
                elif kind in ("ok", "red"):
                    outcome = eng.feed(rule, hook_phase="post", tool="Bash", cmd=arg,
                                       ok=(kind == "ok"))
                elif kind == "gate":
                    outcome = eng.feed(rule, hook_phase="pre", tool="Bash", cmd=arg)
                else:
                    raise ValueError("unknown ordering step %r (edit: | ok: | red: | gate:)" % step)
        finally:
            H.BASE = saved
    return outcome == "fired"


def _fires(hook_rule: dict, raw: str, fixture: dict | None = None) -> bool:
    on = hook_rule.get("on")
    if on == "ordering":
        return _ordering_fires(hook_rule, raw)
    path, content = _split_case(raw)
    if on in ("edit", "write", "write_stdlib"):
        hit = H.evaluate(hook_rule, hook_phase="pre", tool="Edit",
                         file_path=path or "/repo/file.py", body=content)
    elif on in ("result", "output"):
        hit = H.evaluate(hook_rule, hook_phase="post", tool="Bash",
                         cmd=path or "pytest", result_text=content)
    else:
        hit = H.evaluate(hook_rule, hook_phase="pre", tool="Bash", cmd=content)
    return bool(hit) and H.given_ok(hook_rule, _probes(fixture or {}))


def _load_failure(rule: dict, out: list[str]) -> None:
    matcher = rule.get("matcher") or {}
    for key, pat in matcher.items():
        if key.endswith("_rx") and not H.rx_ok(pat):
            why = ("longer than %d characters" % H._RX_MAX) if len(str(pat)) > H._RX_MAX \
                else "does not compile, or backtracks catastrophically"
            out.append("             %s: %s" % (key, why))
    given = matcher.get("given")
    if given is not None and H.given_norm(given) is None:
        known = ", ".join("%s.%s" % (b, k) for b, ks in H._GIVEN.items() for k in ks)
        out.append("             given: unknown key or wrong value kind (known: %s)" % known)
    if not any(l.startswith("             ") for l in out):
        out.append("             no engine block, or two of them, or a missing id")


def verify(rule: dict, fires: list, silent: list,
           fixture: dict | None = None) -> tuple[bool, bool, list[str]]:
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
        _load_failure(rule, out)
        return False, False, out
    out.append("LOAD   ok    the hook loads it (patterns compile, within bounds)")

    if hook_rule.get("on") == "session":
        out.append("NOTE         a session_context rule has no matcher — nothing to fire-test")
        return True, False, out

    ok = True
    for raw in fires:
        case, fx = _case(raw, fixture)
        try:
            hit = _fires(hook_rule, case, fx)
        except ValueError as exc:
            out.append("FIRES  FAIL  %s" % exc)
            ok = False
            continue
        ok &= hit
        out.append("FIRES  %-5s %s" % ("ok" if hit else "FAIL", case[:88]))
    for raw in silent:
        case, fx = _case(raw, fixture)
        try:
            hit = _fires(hook_rule, case, fx)
        except ValueError as exc:
            out.append("SILENT FAIL  %s" % exc)
            ok = False
            continue
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


def _bool(v: str) -> bool:
    if v.lower() in ("1", "true", "yes"):
        return True
    if v.lower() in ("0", "false", "no"):
        return False
    raise argparse.ArgumentTypeError("expected true or false, got %r" % v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--rule", help="the candidate as the JSON you would send to create_rule")
    src.add_argument("--rule-file", help="that JSON, in a file")
    ap.add_argument("--fires", action="append", default=[],
                    metavar="CASE", help="must fire (repeatable); 'path::content' for edit rules; "
                                         "'step >> … >> gate:cmd' for ordering rules")
    ap.add_argument("--silent", action="append", default=[],
                    metavar="CASE", help="must NOT fire (repeatable)")
    ap.add_argument("--cases", help='JSON file: {"fires": [...], "silent": [...]}; a case may be '
                                    'an object {"case": …, "branch": …, "diff_paths": […], …}')
    ap.add_argument("--no-self-mention", action="store_true",
                    help="skip the generated grep / python -c cases")
    fx = ap.add_argument_group("given facts (apply to every case; a --cases object overrides)")
    fx.add_argument("--branch", help="the checked-out branch name")
    fx.add_argument("--diff-path", action="append", default=None, metavar="PATH",
                    help="a path the branch has changed (repeatable; none given = no changes known)")
    fx.add_argument("--diff-lines", type=int, help="added+deleted lines against the base")
    fx.add_argument("--dirty", type=_bool, help="whether the working tree has changes")
    fx.add_argument("--user-said", action="append", default=None, metavar="TEXT",
                    help="a user turn from this session (repeatable; none given = no transcript)")
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

    fixture: dict = {}
    if args.branch is not None:
        fixture["branch"] = args.branch
    if args.diff_path is not None:
        fixture["diff_paths"] = args.diff_path
    if args.diff_lines is not None:
        fixture["diff_lines"] = args.diff_lines
    if args.dirty is not None:
        fixture["dirty"] = args.dirty
    if args.user_said is not None:
        fixture["user_said"] = args.user_said

    fires, silent = list(args.fires), list(args.silent)
    if args.cases:
        with open(args.cases, encoding="utf-8") as f:
            extra = json.load(f)
        fires += list(extra.get("fires") or [])
        silent += list(extra.get("silent") or [])
    generated = [] if args.no_self_mention else _self_mention(rule)
    silent += generated

    ok, testable, lines = verify(rule, fires, silent, fixture)
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
