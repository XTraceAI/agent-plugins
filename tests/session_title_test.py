#!/usr/bin/env python3
"""What a captured session gets called, and which source wins.

Run: python3 plugins/memhub/scripts/session_title_test.py

The record shapes here are verbatim from real transcripts under
``~/.claude/projects``, because both bugs this guards against are bugs of
shape: a headless session that emits no title record at all, and a renamed
session that emits the STALE generated one beside the new name.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# The tests live outside the plugin so they are not shipped to users;
# the code under test is still in the plugin's scripts dir.
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from session_title import (  # noqa: E402
    custom_title,
    generated_title,
    prompt_title,
)

FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(name)


def user(text, **extra) -> dict:
    record = {
        "type": "user",
        "uuid": "u1",
        "cwd": "/Users/dev/xtrace/web",
        "message": {"role": "user", "content": text},
    }
    record.update(extra)
    return record


AI = {"type": "ai-title", "aiTitle": "Test updated brain MCP feature",
      "sessionId": "s1"}
CUSTOM = {"type": "custom-title", "customTitle": "better manifest and idex",
          "sessionId": "s1"}


# ── the explicit titles ───────────────────────────────────────────────

check("generated title is read", generated_title([AI]) == AI["aiTitle"])
check("custom title is read", custom_title([CUSTOM]) == CUSTOM["customTitle"])
check("no title records yields nothing",
      generated_title([user("hi")]) is None
      and custom_title([user("hi")]) is None)

# Claude Code regenerates its title as the session develops, so within one
# batch the last ai-title is the current one.
check("the last generated title wins", generated_title([
    AI, {"type": "ai-title", "aiTitle": "Newer title"},
]) == "Newer title")

# THE RENAME BUG. Measured on a real renamed session: 130 ai-title records all
# carrying the pre-rename name, interleaved with 47 custom-title records
# carrying the new one — and an ai-title was LAST. Reading "whichever came
# last" returns the name the user replaced, so precedence must be by TYPE.
RENAMED = [AI, CUSTOM, AI]
check("a rename is not overwritten by the stale generated title",
      custom_title(RENAMED) == "better manifest and idex"
      and generated_title(RENAMED) == "Test updated brain MCP feature")

check("blank titles are ignored",
      generated_title([AI, {"type": "ai-title", "aiTitle": "   "}])
      == AI["aiTitle"])
check("a non-string title is ignored",
      generated_title([{"type": "ai-title", "aiTitle": 42}]) is None)
check("junk records do not raise",
      generated_title([None, "x", 7, {}]) is None)


# ── the prompt fallback (the headless case) ───────────────────────────

check("the first user prompt becomes the title",
      prompt_title([user("Reply with exactly: ok")])
      == "Reply with exactly: ok")

check("the FIRST prompt wins, not the last",
      prompt_title([user("first thing"), user("second thing")])
      == "first thing")

# Tool results are typed ``user`` and carry no prose.
check("a tool result is not a prompt", prompt_title([
    {"type": "user", "toolUseResult": {"stdout": "ok"},
     "message": {"role": "user", "content": [
         {"type": "tool_result", "content": "ok"}]}},
    user("the real question"),
]) == "the real question")

# So are sidechain records (subagent turns).
check("a sidechain turn is not a prompt",
      prompt_title([user("subagent prompt", isSidechain=True),
                    user("the real question")]) == "the real question")

# ``isMeta`` is how the client writes its OWN output into the transcript —
# a /context dump would otherwise title the session "## Context Usage …".
check("a client meta record is not a prompt",
      prompt_title([user("## Context Usage\n\n**Model:** claude-fable-5",
                         isMeta=True),
                    user("the real question")]) == "the real question")

check("a slash command is not a prompt", prompt_title([
    user("<command-name>/model</command-name>\n"
         "<command-message>model</command-message>\n"
         "<command-args></command-args>"),
    user("the real question"),
]) == "the real question")

# A system-reminder rides along inside a real message; the title should be
# what the person typed, not the client's injected block.
check("an injected reminder is stripped, the prose kept", prompt_title([
    user("<system-reminder>Background context here.</system-reminder>\n"
         "fix the flush hook"),
]) == "fix the flush hook")

check("a reminder-only record is skipped", prompt_title([
    user("<system-reminder>Only bookkeeping.</system-reminder>"),
    user("the real question"),
]) == "the real question")

check("whitespace is collapsed",
      prompt_title([user("  fix   the\n\nflush  hook  ")])
      == "fix the flush hook")

check("no prompt at all yields nothing",
      prompt_title([AI, CUSTOM]) is None)

# Block-list content, which is what a message with an attachment looks like.
check("block content is read",
      prompt_title([user([{"type": "text", "text": "block form prompt"}])])
      == "block form prompt")

LONG = ("please refactor the per-turn flush hook so that it stops "
        "re-uploading the whole transcript on every single turn")
truncated = prompt_title([user(LONG)])
check("a long prompt is truncated", len(truncated) <= 80)
check("truncation marks itself", truncated.endswith("…"))
check("truncation breaks on a word", " " not in truncated[-2:]
      and LONG.startswith(truncated[:-1].rstrip()))
check("an exactly-80-char prompt is untouched",
      prompt_title([user("x" * 80)]) == "x" * 80)


# ── against every real transcript on this machine ─────────────────────

def _carries_title_record(records) -> bool:
    """A raw shape scan, independent of the parsers under test: does any
    record LOOK like a usable title? Gating on this rather than on the
    machine keeps the check meaningful everywhere — a host used only
    headlessly (CI, e2e boxes) legitimately has no titled session at all."""
    for r in records:
        if not isinstance(r, dict):
            continue
        if r.get("type") == "ai-title":
            value = r.get("aiTitle")
        elif r.get("type") == "custom-title":
            value = r.get("customTitle")
        else:
            continue
        if isinstance(value, str) and value.strip():
            return True
    return False


root = Path.home() / ".claude" / "projects"
real = sorted(root.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime,
              reverse=True)[:200] if root.is_dir() else []
if real:
    titled = untitled = named_by_prompt = 0
    any_title_records = False
    for path in real:
        records = []
        for line in open(path, "rb"):
            try:
                records.append(json.loads(line))
            except Exception:  # noqa: BLE001
                continue
        any_title_records = any_title_records or _carries_title_record(records)
        explicit = custom_title(records) or generated_title(records)
        if explicit:
            titled += 1
            continue
        untitled += 1
        if prompt_title(records):
            named_by_prompt += 1
    print(f"real transcripts: {titled} carry a title record; of the "
          f"{untitled} that do not, {named_by_prompt} are named by their "
          "first prompt")
    # The fallback exists for sessions the client never titles. If it fires
    # for none of them it is not doing its job; if it fires for all of THEM
    # while explicit titles vanish, precedence broke.
    if any_title_records:
        check("real sessions carry explicit titles", titled > 0)
    else:
        print("real transcripts: none carries a title record on this machine "
              "(headless-only host) — extraction is pinned by the synthetic "
              "checks above")
    check("the fallback names sessions that have no title record",
          untitled == 0 or named_by_prompt > 0)
else:
    print("real transcripts: none found, skipped")


print(f"{'FAIL' if FAILURES else 'PASS'}: session_title")
for f in FAILURES:
    print(f"  - {f}")
sys.exit(1 if FAILURES else 0)
