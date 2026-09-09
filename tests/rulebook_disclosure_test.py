"""Every rulebook fire is disclosed, in a fixed shape, on both channels.

A fire that silently steered the work is indistinguishable from no fire at all
— to the user watching, to the transcript, and to everything downstream that
reads the transcript. So two things are pinned here:

* the ``systemMessage`` leads with `📏 Rule fired: …` (or `⛔️` when the call
  was actually stopped) and keeps today's branded detail line beneath it, so
  nothing a user recognises today is lost;
* the lines the agent is told to echo are **string-equal** to the
  ``systemMessage`` first lines, because they come from one function. A
  divergence between what the terminal shows and what the agent says would be
  worse than either channel alone.

The hook runs as a subprocess against a seeded book cache in a tmpdir, with
`MEMHUB_RULEBOOK_FETCH=0` so nothing reaches a network.

Run: python3 tests/rulebook_disclosure_test.py   (stdlib only)
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "plugins" / "memhub" / "scripts" / "rulebook_hook.py"

_spec = importlib.util.spec_from_file_location("rulebook_hook", HOOK)
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok ' if condition else 'FAIL'} {label}"
          + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


# ------------------------------------------------------------ the pure part

def test_the_description_is_the_title_then_the_statement_then_the_id():
    check("a title is used as-is",
          hook.disclosure_line({"_label": "Run the suite before pushing", "id": "r1"})
          == "📏 Rule fired: Run the suite before pushing")
    check("no title → the statement",
          hook.disclosure_line({"text": "Never force-push a shared branch", "id": "r1"})
          == "📏 Rule fired: Never force-push a shared branch")
    check("neither → the id",
          hook.disclosure_line({"id": "r-42"}) == "📏 Rule fired: r-42")
    check("an empty title falls through to the statement",
          hook.disclosure_line({"_label": "   ", "text": "the statement", "id": "r1"})
          == "📏 Rule fired: the statement")


def test_the_description_is_normalised_to_one_bounded_line():
    # Both limits apply, and whichever bites first wins: 20 words is the
    # headline rule, 120 characters is the backstop for long words.
    got = hook.disclosure_desc({"text": " ".join("ab" for _ in range(60))})
    check("clipped to 20 words when the words are short",
          got.rstrip("…").split(" ") == ["ab"] * 20, got)
    check("…and marked as clipped", got.endswith("…"), got)

    got = hook.disclosure_desc({"text": " ".join(f"word{i}" for i in range(60))})
    check("never more than 20 words and never more than 120 chars",
          len(got.rstrip("…").split(" ")) <= 20 and len(got) <= 121, got)

    got = hook.disclosure_desc({"_label": "x" * 300})
    check("a 300-char title is cut at 120", len(got) <= 121, f"{len(got)}: {got}")
    check("…and marked", got.endswith("…"), got)

    got = hook.disclosure_desc({"_label": "long " * 40})
    check("a long title is cut on a word boundary, not mid-word",
          got.rstrip("…").endswith("long"), got)

    got = hook.disclosure_desc({"text": "a\nb\tc   d"})
    check("newlines and runs of whitespace collapse to single spaces",
          got == "a b c d", repr(got))

    got = hook.disclosure_desc({"text": "**bold** and `code` and text"})
    check("markdown markup is stripped, its content kept",
          got == "bold and code and text", repr(got))

    # Underscores survive on purpose. A rule title names a file or a symbol far
    # more often than it uses underscore emphasis, and stripping them made the
    # disclosure say something untrue — in the terminal AND in the transcript
    # the agent is told to echo it into.
    got = hook.disclosure_desc({"_label": "never edit `snake_case_name.py`"})
    check("a backticked identifier keeps its underscores",
          got == "never edit snake_case_name.py", repr(got))
    got = hook.disclosure_desc({"_label": "use __init__.py not __main__.py"})
    check("dunder filenames survive intact",
          got == "use __init__.py not __main__.py", repr(got))

    check("a short description is not marked as clipped",
          not hook.disclosure_desc({"_label": "short one"}).endswith("…"))


def test_blocked_takes_the_stop_symbol_and_the_same_sentence():
    rule = {"_label": "Never force-push to main", "id": "r1"}
    advisory = hook.disclosure_line(rule, blocked=False)
    blocked = hook.disclosure_line(rule, blocked=True)
    check("blocked uses ⛔️", blocked.startswith("⛔️ Rule fired: "), blocked)
    check("advisory uses 📏", advisory.startswith("📏 Rule fired: "), advisory)
    check("the sentence is identical either way",
          advisory.split(" ", 1)[1] == blocked.split(" ", 1)[1])


# ------------------------------------------------------------ the hook itself

# Server-shape (`?view=hook`) rows — the shape the hook caches and the only
# one that carries `title`, which is what `_label` (and so the disclosure
# line) is built from.
def _row(rid, title, statement, matcher, **extra):
    row = {"rule_id": rid, "title": title, "statement": statement,
           "delivery": "agent_hook", "mode": "advise", "version": 1,
           "status": "active", "scope_repos": [], "matcher": matcher}
    row.update(extra)
    return row


RULES = {
    "adv": _row("adv", "Run the suite before pushing",
                "Run the tests before you push — a red main blocks everyone.",
                {"event": "bash", "command_rx": "advisory-cmd"}),
    "adv2": _row("adv2", "Rebase, never merge",
                 "Rebase your topic branch instead of merging main into it.",
                 {"event": "bash", "command_rx": "advisory-cmd"}),
    "gate": _row("gate", "Never force-push to main",
                 "Never force-push a shared branch.",
                 {"event": "bash", "command_rx": r"git\s+push\s+--force"},
                 mode="gate"),
    "posture": {"rule_id": "posture", "title": "Small PRs",
                "statement": "Keep pull requests under 500 lines.",
                "delivery": "session_context", "version": 1,
                "status": "active", "scope_repos": []},
}


def seed(base: Path, repo: str, ids: list[str]) -> None:
    book = Path(hook.book_path(repo).replace(hook.BASE, str(base)))
    book.parent.mkdir(parents=True, exist_ok=True)
    book.write_text(json.dumps({
        "etag": '"v1"', "fetched_at": "2099-01-01T00:00:00+00:00",
        "rules": [RULES[i] for i in ids]}), encoding="utf-8")


def run(base: Path, repo: str, mode: str, payload: dict) -> dict:
    proc = subprocess.run(
        [sys.executable, str(HOOK), mode], input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "PYTHONUTF8": "1", "MEMHUB_RULEBOOK_BASE": str(base),
             "MEMHUB_RULEBOOK_FETCH": "0", "MEMHUB_RULEBOOK_RECALL": "0",
             "MEMHUB_TOKEN": ""})
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


def _repo(td: str) -> tuple[Path, str]:
    """A real git checkout — the hook resolves rules from one."""
    repo_dir = Path(td) / "repo"
    repo_dir.mkdir()
    for args in (["init", "-q", "-b", "main"],
                 ["remote", "add", "origin", "https://github.com/o/disclose-test.git"]):
        subprocess.run(["git", "-C", str(repo_dir), *args], check=True,
                       capture_output=True)
    return repo_dir, "disclose-test"


def test_an_advisory_fire_leads_with_the_disclosure_line():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "rb"
        repo_dir, repo = _repo(td)
        seed(base, repo, ["adv"])
        out = run(base, repo, "pre", {"cwd": str(repo_dir), "session_id": "s1",
                                      "tool_name": "Bash",
                                      "tool_input": {"command": "advisory-cmd"}})
        msg = out.get("systemMessage", "")
        lines = msg.split("\n")
        check("the first line is the disclosure",
              lines[:1] == ["📏 Rule fired: Run the suite before pushing"], repr(msg))
        check("the second line is today's branded detail, indented",
              len(lines) > 1 and lines[1].startswith("   XTrace ▸ [Run the suite")
              and "a red main blocks everyone" in lines[1], repr(msg))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        check("the agent context still carries the rule bullet",
              "- **[Run the suite before pushing]**" in ctx, ctx)
        check("…and ends with the echo instruction",
              ctx.rstrip().endswith("needs to hear about._"), ctx[-200:])
        check("…which forbids paraphrasing and omitting",
              "Do not paraphrase" in ctx and "did not change what you were going to do" in ctx)


def test_the_echoed_lines_are_string_equal_to_what_the_terminal_shows():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "rb"
        repo_dir, repo = _repo(td)
        seed(base, repo, ["adv", "adv2"])
        out = run(base, repo, "pre", {"cwd": str(repo_dir), "session_id": "s1",
                                      "tool_name": "Bash",
                                      "tool_input": {"command": "advisory-cmd"}})
        msg, ctx = out.get("systemMessage", ""), \
            out["hookSpecificOutput"]["additionalContext"]
        # Every OTHER line of the systemMessage is a disclosure; the ones
        # between are the indented detail lines.
        shown = [l for l in msg.split("\n") if not l.startswith("   ")]
        quoted = [l for l in ctx.split("\n") if l.startswith(("📏 ", "⛔️ "))]
        check("two rules → two stanzas", len(shown) == 2, repr(msg))
        check("the echoed lines equal the shown ones, exactly",
              shown == quoted, f"{shown!r} != {quoted!r}")
        check("…and the existing order is preserved",
              shown[0].endswith("Run the suite before pushing"), repr(shown))


def test_a_blocked_gate_discloses_with_the_stop_symbol_and_still_denies():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "rb"
        repo_dir, repo = _repo(td)
        seed(base, repo, ["gate"])
        out = run(base, repo, "pre", {"cwd": str(repo_dir), "session_id": "s1",
                                      "tool_name": "Bash",
                                      "tool_input": {"command": "git push --force origin main"}})
        hso = out.get("hookSpecificOutput", {})
        msg = out.get("systemMessage", "")
        check("the call is still denied", hso.get("permissionDecision") == "deny", str(out))
        check("the deny reason is unchanged",
              "Never force-push a shared branch" in hso.get("permissionDecisionReason", "")
              and "RULEBOOK_OVERRIDE=" in hso.get("permissionDecisionReason", ""),
              hso.get("permissionDecisionReason", ""))
        check("the user's first line is the blocked disclosure",
              msg.startswith("⛔️ Rule fired: Never force-push to main"), repr(msg))
        check("…with today's branded blocked line beneath it",
              "\n   XTrace ⛔ blocked by [Never force-push to main]" in msg, repr(msg))


def test_an_overridden_gate_fired_but_did_not_stop_the_call():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "rb"
        repo_dir, repo = _repo(td)
        seed(base, repo, ["gate"])
        out = run(base, repo, "pre", {
            "cwd": str(repo_dir), "session_id": "s1", "tool_name": "Bash",
            "tool_input": {"command":
                "RULEBOOK_OVERRIDE='rebasing my own topic branch' git push --force origin main"}})
        msg = out.get("systemMessage", "")
        check("the call is allowed",
              "permissionDecision" not in out.get("hookSpecificOutput", {}), str(out))
        check("the disclosure uses 📏, not ⛔️ — the rule fired, the work was not halted",
              msg.startswith("📏 Rule fired: Never force-push to main"), repr(msg))
        check("…and the reason is still shown beneath it",
              "rebasing my own topic branch" in msg, repr(msg))


def test_session_start_introduces_the_rules_and_discloses_nothing():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "rb"
        repo_dir, repo = _repo(td)
        seed(base, repo, ["posture", "adv"])
        out = run(base, repo, "session", {"cwd": str(repo_dir), "session_id": "s1"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        check("the heading takes the ruler",
              ctx.startswith("## 📏 Rulebook (team rules — advisory)"), ctx[:80])
        check("the preamble says rules carry CLAUDE.md's standing",
              "same weight as this repo's CLAUDE.md" in ctx, ctx[:600])
        check("…and mandates the disclosure shape",
              "📏 Rule fired: <the rule, in 20 words or fewer>" in ctx, ctx[:600])
        check("the posture bullet is unchanged",
              "- Keep pull requests under 500 lines." in ctx, ctx)
        check("the armed-rules line is unchanged",
              "1 rule armed for this repo" in ctx, ctx)
        check("NO posture rule is dressed as a fire",
              "Rule fired:" not in ctx.replace(
                  "📏 Rule fired: <the rule, in 20 words or fewer>", ""), ctx)
        check("session start reaches the agent only, not the terminal",
              "systemMessage" not in out, str(out))


def test_the_preamble_stays_inside_its_budget():
    # ~65 words / ~420 chars, charged to every session that has any rule. It is
    # deliberately not counted against POSTURE_BUDGET_CHARS, which bounds rule
    # CONTENT — so this is the only thing keeping it honest.
    check("the preamble is under 500 chars",
          len(hook.SESSION_PREAMBLE) < 500, str(len(hook.SESSION_PREAMBLE)))
    check("it is a constant: no repo name, no counts, no formatting slots",
          "{" not in hook.SESSION_PREAMBLE and "}" not in hook.SESSION_PREAMBLE)


def test_book_path_is_answered_by_the_hook_that_reads_it():
    proc = subprocess.run([sys.executable, str(HOOK), "book-path", "o/r"],
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONUTF8": "1"})
    check("book-path prints exactly what book_path() computes",
          proc.returncode == 0 and proc.stdout.strip() == hook.book_path("o/r"),
          proc.stdout + proc.stderr)
    for empty in ("", "   "):
        proc = subprocess.run([sys.executable, str(HOOK), "book-path", empty],
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PYTHONUTF8": "1"})
        check(f"an empty repo argument exits non-zero ({empty!r})",
              proc.returncode != 0 and proc.stdout.strip() == "", proc.stdout)
    proc = subprocess.run([sys.executable, str(HOOK), "book-path"],
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONUTF8": "1"})
    check("a missing repo argument exits non-zero", proc.returncode != 0, proc.stdout)


if __name__ == "__main__":
    print("rulebook disclosure")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"\n{name}")
            fn()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
    else:
        print("all rulebook disclosure checks passed")
    sys.exit(1 if failures else 0)
