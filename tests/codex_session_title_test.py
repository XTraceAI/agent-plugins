#!/usr/bin/env python3
"""What a captured CODEX session gets called, and which source wins.

Run: python3 tests/codex_session_title_test.py

The requirement is fidelity, not prettiness: **MemHub must show the title
Codex's own UI shows.** Codex names every substantive thread itself and writes
that name into the rollout as an ``event_msg``/``thread_name_updated`` record —
and the reader used to ignore it entirely, titling the session with a 150-char
mid-word slice of the first user message instead. Measured on this machine
before the fix, three distinct sessions Codex called *Read* / *Inspect* /
*Review context-sessions endpoints* all landed under one byte-identical title,
and a session Codex called *Fix doc ingest 500* landed as a pasted log line
beginning with a shell prompt glyph.

The record shapes here are verbatim from real rollouts under
``~/.codex/sessions`` and from ``~/.codex/session_index.jsonl``.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from readers import codex as codex_reader  # noqa: E402
from redact import redact_text  # noqa: E402
from session_title import normalize_title  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    if not condition:
        FAILURES.append(name)


# ── rollout record builders (verbatim shapes) ─────────────────────────

def meta_line(sid: str = "019daccf-641d-7243-be7b-7ce39be99773") -> dict:
    return {"timestamp": "2026-04-20T21:32:45.000Z", "type": "session_meta",
            "payload": {"session_id": sid, "id": sid,
                        "cwd": "/Users/dev/xtrace/web",
                        "originator": "codex-tui", "cli_version": "0.146.0"}}


def thread_name(name, sid="019daccf-641d-7243-be7b-7ce39be99773") -> dict:
    return {"timestamp": "2026-04-20T21:32:52.024Z", "type": "event_msg",
            "payload": {"type": "thread_name_updated", "thread_id": sid,
                        "thread_name": name}}


def user(text: str) -> dict:
    return {"timestamp": "2026-04-20T21:32:46.000Z", "type": "response_item",
            "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": text}]}}


def task_complete(msg: str) -> dict:
    return {"timestamp": "2026-04-20T21:33:00.000Z", "type": "event_msg",
            "payload": {"type": "task_complete", "last_agent_message": msg}}


SID = "019daccf-641d-7243-be7b-7ce39be99773"


# ── the sidecar, isolated to a tmpdir ─────────────────────────────────
# The reader reads ~/.codex/session_index.jsonl through a module constant;
# every test below repoints it so the suite never touches the real one and
# behaves the same on a machine that has no Codex install at all.

_TMP = tempfile.TemporaryDirectory()
TMP = Path(_TMP.name)
_REAL_INDEX = codex_reader._SESSION_INDEX


def set_index(*rows: dict, raw: str | None = None) -> None:
    """Point the reader at a sidecar containing exactly ``rows``."""
    path = TMP / "session_index.jsonl"
    if raw is None:
        raw = "".join(json.dumps(r) + "\n" for r in rows)
    path.write_text(raw, encoding="utf-8")
    codex_reader._SESSION_INDEX = path


def no_index() -> None:
    codex_reader._SESSION_INDEX = TMP / "does-not-exist.jsonl"


def title_of(rollout: list[dict], sid: str | None = SID) -> str | None:
    return codex_reader._title(rollout, sid)


# ── 1. the ladder ─────────────────────────────────────────────────────

no_index()

check("Codex's own thread_name beats the first prompt",
      title_of([meta_line(), user("can you do a reading of the "
                                  "context-sessions endpoints for team orgs?"),
                thread_name("Read context-sessions endpoints")])
      == "Read context-sessions endpoints")

# thread_name_updated is an *_updated* record: a regenerated name, or a rename,
# emits another. Last wins — the same rule session_title.generated_title
# applies to Claude's ai-title.
check("the last thread_name wins",
      title_of([meta_line(), thread_name("First guess"),
                thread_name("Review org switcher refresh bug")])
      == "Review org switcher refresh bug")

check("a blank thread_name falls through to the prompt",
      title_of([meta_line(), thread_name("   "), user("fix the flush hook")])
      == "fix the flush hook")

check("a non-string thread_name falls through",
      title_of([meta_line(), thread_name(42), user("fix the flush hook")])
      == "fix the flush hook")

check("no thread_name anywhere falls back to the first prompt",
      title_of([meta_line(), user("fix the flush hook")])
      == "fix the flush hook")

check("the FIRST prompt wins, not the last",
      title_of([meta_line(), user("first thing"), user("second thing")])
      == "first thing")

check("with no user turn, the closing summary is the title",
      title_of([meta_line(), task_complete("Fixed the ingest handler")])
      == "Fixed the ingest handler")

# The opening ask reads as a topic; the closing summary reads as a report.
check("the opening ask still beats the closing summary",
      title_of([meta_line(), user("fix doc ingest"),
                task_complete("I fixed it")]) == "fix doc ingest")

check("an empty rollout yields nothing", title_of([]) is None)
check("junk records do not raise",
      title_of([None, "x", 7, {}, {"payload": None}]) is None)


# ── 2. the sidecar fallback ───────────────────────────────────────────
# THE DESKTOP CASE. Measured: session 019f8238 (Codex Desktop 0.142.5) is named
# "Add memhub claude plugin" in the index while its rollout carries no
# thread_name_updated record at all. Without this lookup those sessions fall
# through to the prompt and MemHub disagrees with what Codex is displaying.

set_index({"id": SID, "thread_name": "Add memhub claude plugin",
           "updated_at": "2026-07-21T01:09:41.034278Z"})
check("the sidecar names a session whose rollout does not",
      title_of([meta_line(), user("/plugin marketplace add XTraceAI/memhub")])
      == "Add memhub claude plugin")

check("the ROLLOUT wins over the sidecar",
      title_of([meta_line(), thread_name("From the rollout")])
      == "From the rollout")

check("a sidecar row for another session is ignored",
      title_of([meta_line(), user("fix the flush hook")], sid="other-sid")
      == "fix the flush hook")

check("no session id means no sidecar lookup",
      title_of([meta_line(), user("fix the flush hook")], sid=None)
      == "fix the flush hook")

set_index({"id": SID, "thread_name": "  "})
check("a blank sidecar name falls through",
      title_of([meta_line(), user("fix the flush hook")])
      == "fix the flush hook")

set_index({"id": SID, "thread_name": "First"}, {"id": SID,
                                                "thread_name": "Second"})
check("the last matching sidecar row wins",
      title_of([meta_line()]) == "Second")


# ── 3. sidecar robustness (a Stop hook must never raise) ──────────────

no_index()
check("a missing index is silent", title_of([meta_line()]) is None)

set_index(raw="not json at all\n{broken\n")
check("malformed lines are skipped", title_of([meta_line()]) is None)

set_index(raw=json.dumps({"id": SID, "thread_name": "Fix doc ingest 500"})
          + "\n{\"id\": \"trunc\", \"thread_na")
check("a torn final line does not lose the good row above it",
      title_of([meta_line()]) == "Fix doc ingest 500")

set_index(raw='["not", "an", "object"]\n'
          + json.dumps({"id": SID, "thread_name": "Fix doc ingest 500"}) + "\n")
check("a non-object row is skipped",
      title_of([meta_line()]) == "Fix doc ingest 500")

set_index(raw="")
check("an empty index is silent", title_of([meta_line()]) is None)

# A directory where the file should be: open() raises OSError, not ValueError.
codex_reader._SESSION_INDEX = TMP
check("an unreadable index is silent", title_of([meta_line()]) is None)

# The index is APPEND-ordered — oldest session first — so the row for the
# session being flushed is always among the newest. Bounding from the front
# would silently kill this lane for any long-time Codex user; the scan must
# keep the TAIL. (An earlier revision of this code read the head, and this
# test passed while asserting the broken behaviour.)
over = "".join(json.dumps({"id": "filler", "thread_name": "x"}) + "\n"
               for _ in range(codex_reader._INDEX_MAX_LINES * 2))
set_index(raw=over + json.dumps({"id": SID, "thread_name": "Newest row"}) + "\n")
check("a row past the bound is still found when it is the newest",
      title_of([meta_line()]) == "Newest row")
# ...and the bound still holds: a row older than the window is out of reach.
old_far = json.dumps({"id": SID, "thread_name": "Too old"}) + "\n"
padding = "".join(json.dumps({"id": "filler", "thread_name": "x"}) + "\n"
                  for _ in range(codex_reader._INDEX_MAX_LINES))
set_index(raw=old_far + padding * 2)
check("the scan is bounded", title_of([meta_line()]) is None)

# The bound is on BYTES READ, not just lines retained: the read seeks to the
# tail, so a huge index costs the same as a small one. A line-count bound would
# still stream the whole file from disk on every unnamed-rollout flush.
big = "x" * (codex_reader._INDEX_TAIL_BYTES + 2_000_000)
set_index(raw=big + "\n" + json.dumps({"id": SID, "thread_name": "Newest"}) + "\n")
_t0 = time.perf_counter()
_got = title_of([meta_line()])
_elapsed = time.perf_counter() - _t0
check("a huge index is still read from the tail", _got == "Newest")
check("one unterminated line cannot blow the read up", _elapsed < 1.0)
check("the byte bound comfortably covers the line cap",
      codex_reader._INDEX_TAIL_BYTES > codex_reader._INDEX_MAX_LINES * 141)

# A seek lands mid-record on any real index; the leading fragment is half a
# row and must not be parsed as one. Build it so the seam falls inside the
# filler rows, with the wanted row intact after it.
filler_row = json.dumps({"id": "filler", "thread_name": "x" * 40}) + "\n"
seam = filler_row * ((codex_reader._INDEX_TAIL_BYTES // len(filler_row)) + 50)
set_index(raw=seam + json.dumps({"id": SID,
                                 "thread_name": "After the seam"}) + "\n")
check("a row split by the seek point is not mistaken for a record",
      title_of([meta_line()]) == "After the seam")

# A multi-byte character straddling the seek offset must not raise.
set_index(raw=("é" * codex_reader._INDEX_TAIL_BYTES) + "\n"
          + json.dumps({"id": SID, "thread_name": "Après"}) + "\n")
check("a character split by the seek point does not raise",
      title_of([meta_line()]) == "Après")


# ── 4. verbatim vs normalized ─────────────────────────────────────────
# A thread_name is what Codex DISPLAYS. Reshaping it would reintroduce exactly
# the disagreement this whole change exists to remove, so it is passed through
# untouched — no 80-char cap, no ellipsis, no whitespace collapse.

no_index()
LONG_NAME = "Review the org switcher refresh bug and the session " \
            "attribution regression that came with it"
check("a long thread_name is NOT truncated",
      title_of([meta_line(), thread_name(LONG_NAME)]) == LONG_NAME)
check("a long thread_name gets no ellipsis",
      not title_of([meta_line(), thread_name(LONG_NAME)]).endswith("…"))
check("a thread_name's internal spacing is NOT collapsed",
      title_of([meta_line(), thread_name("Fix   doc    ingest")])
      == "Fix   doc    ingest")
# "Verbatim" means not RESHAPING Codex's name — it does not mean surrendering
# the two invariants the old `splitlines()[0][:150]` gave for free.
check("a thread_name is forced to one line",
      title_of([meta_line(), thread_name("line1\nline2")]) == "line1")
check("a CR cannot overwrite the terminal echo",
      "\r" not in title_of([meta_line(), thread_name("safe\r\nEVIL")]))
check("an absurd thread_name is bounded",
      len(title_of([meta_line(), thread_name("A" * 5000)]))
      == codex_reader._MAX_NAME)
check("the bound matches what the send sites cap at",
      codex_reader._MAX_NAME == 200)

check("a thread_name is stripped",
      title_of([meta_line(), thread_name("  Fix doc ingest 500  ")])
      == "Fix doc ingest 500")

# A DERIVED title is ours, not Codex's, and gets the shared shaping.
LONG_ASK = ("can you do a reading of the context-sessions endpoints for team "
            "orgs? I want to do the following: right now, the user is able to "
            "query the policy and get a result back")
derived = title_of([meta_line(), user(LONG_ASK)])
check("a long prompt is truncated", len(derived) <= 80)
check("truncation marks itself", derived.endswith("…"))
check("truncation breaks on a word",
      " " not in derived[-2:] and LONG_ASK.startswith(derived[:-1].rstrip()))
check("the old 150-char mid-word cut is gone",
      not derived.startswith(LONG_ASK[:150]))

# THE PASTED-LOG SESSION, verbatim from rollout-2026-05-04…019df592.
PASTED = ('❯ I am getting these errors from doc ingest:\n\n'
          '    2026-05-05T00:16:28.620Z    INFO: 10.0.1.121:44566 - "POST '
          '/v1/documents HTTP/1.1" 500 Internal Server Error')
pasted_title = title_of([meta_line(), user(PASTED)])
check("a pasted log's column padding is collapsed",
      "    " not in pasted_title)
check("a pasted log title still fits", len(pasted_title) <= 80)
# ...and with Codex's own name present, none of that shaping is even reached.
check("Codex's name replaces the pasted log entirely",
      title_of([meta_line(), user(PASTED),
                thread_name("Fix doc ingest 500")]) == "Fix doc ingest 500")


# ── 5. the three-identical-titles regression ──────────────────────────
# Measured before the fix: three separate sessions, one byte-identical title.

SHARED_ASK = ("can you do a reading of the context-sessions endpoints for "
              "team orgs? I want to do the following: right now, the user is "
              "able to query the policy and get a result")
names = ["Read context-sessions endpoints",
         "Inspect context-sessions endpoints",
         "Review context-sessions endpoints"]
titles = [title_of([meta_line(), user(SHARED_ASK), thread_name(n)])
          for n in names]
check("three sessions with one ask get three distinct titles",
      titles == names and len(set(titles)) == 3)


# ── 6. normalize_title itself ─────────────────────────────────────────

check("normalize_title collapses newlines",
      normalize_title("fix   the\n\nflush  hook") == "fix the flush hook")
check("normalize_title keeps a short string", normalize_title("ok") == "ok")
check("normalize_title on exactly 80 is untouched",
      normalize_title("x" * 80) == "x" * 80)
check("normalize_title never exceeds the limit",
      len(normalize_title("x " * 200)) <= 80)
check("normalize_title on nothing yields nothing",
      normalize_title(None) is None and normalize_title("") is None
      and normalize_title("   ") is None and normalize_title(42) is None)


# ── 7. the title is redacted before it is sent ────────────────────────
# The title is derived from RAW records — `redact_records` covers only the
# messages — so a first prompt carrying a secret would ship as the
# conversation's NAME, the most visible field there is. The reader itself is
# deliberately not the place that redacts; the three send sites are.

SECRET = "export MEMHUB_TOKEN=mhk_live_abcdefghijklmnopqrstuvwxyz123456"
leaky = title_of([meta_line(), user(SECRET)])
check("the reader does not redact (the send sites do)",
      leaky is not None and "mhk_live_abcdef" in leaky)
check("redact_text scrubs the title", "mhk_live_abcdef"
      not in (redact_text(leaky) or ""))

# Guard the call actually being there: this leak is invisible in behaviour
# until someone's key is the name of a conversation.
SEND_SITES = {
    "codex_flush.py": 'redact_text(title.strip())[:200]',
    "cursor_flush.py": 'redact_text(title.strip())[:200]',
    "import_session.py": 'redact_text(args.title)',
    # argv is world-readable while the child runs, so the title must already
    # be clean before it is handed over — not only inside the child.
    "capture.py": 'redact_text(meta["title"])',
}
for filename, expected in SEND_SITES.items():
    source = (SCRIPTS / filename).read_text(encoding="utf-8")
    check(f"{filename} redacts the title it sends", expected in source)

# Redaction must come BEFORE the cap. Capping first can chop a straddling key
# below _SECRET's {16,} floor, after which it matches nothing and ships clear.
STRADDLE = "x" * 180 + " mhk_live_abcdefghijklmnopqrstuvwxyz"
check("redact-then-cap scrubs a straddling key",
      "mhk_live_abcdefghij" not in redact_text(STRADDLE)[:200])
check("cap-then-redact would NOT (this is why the order matters)",
      "mhk_live_abcdefghij" in redact_text(STRADDLE[:200]))
for filename in ("codex_flush.py", "cursor_flush.py"):
    source = (SCRIPTS / filename).read_text(encoding="utf-8")
    check(f"{filename} redacts before capping",
          "redact_text(title.strip())[:200]" in source
          and "redact_text(title.strip()[:200])" not in source)


# ── 7b. Cursor gets the same shaping ──────────────────────────────────
# Cursor has no host-generated name, so its title is always derived — which
# means the normalization is the whole of its fix. It also used to raise
# IndexError on a whitespace-only ask ("   ".strip().splitlines() == []).

from readers import cursor as cursor_reader  # noqa: E402

LONG_CURSOR_ASK = ("please refactor the per-turn flush hook so that it stops "
                   "re-uploading the whole transcript on every single turn")
cursor_title = cursor_reader.normalize_title(LONG_CURSOR_ASK)
check("a long Cursor ask is cut to <= 80 with an ellipsis",
      len(cursor_title) <= 80 and cursor_title.endswith("…"))
check("Cursor's reader uses the shared normalizer",
      cursor_reader.normalize_title is normalize_title)
check("a whitespace-only Cursor ask no longer raises",
      cursor_reader.normalize_title("   ") is None)


# ── 8. against every real rollout on this machine ─────────────────────
# The acceptance test for the stated goal: for every session Codex has named,
# what we send must equal that name. Gated on the store existing, so a CI box
# with no Codex install skips rather than fails.

codex_reader._SESSION_INDEX = _REAL_INDEX
def _mtime(p: Path) -> float:
    try:  # a broken symlink under the store must not abort the suite
        return p.stat().st_mtime
    except OSError:
        return 0.0


real = sorted(codex_reader.sessions_root().glob("**/*.jsonl"),
              key=_mtime, reverse=True)[:200] \
    if codex_reader.sessions_root().is_dir() else []
if real:
    index_names: dict[str, str] = {}
    try:
        for line in _REAL_INDEX.read_text(encoding="utf-8",
                                          errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(row, dict) and isinstance(row.get("id"), str) \
                    and isinstance(row.get("thread_name"), str):
                index_names[row["id"]] = row["thread_name"].strip()
    except Exception:  # noqa: BLE001
        pass

    named = derived_count = mismatched = 0
    for path in real:
        try:
            rollout = codex_reader.load_rollout(path)
            if not rollout:
                continue
            _records, meta = codex_reader.rollout_to_claude_records(rollout)
        except Exception as exc:  # noqa: BLE001
            # A real rollout must never raise: this runs in a Stop hook.
            check(f"{path.name} reads without raising ({exc!r})", False)
            continue
        got, sid = meta.get("title"), meta.get("session_id")
        expected = index_names.get(sid or "")
        if expected:
            named += 1
            if got != expected:
                mismatched += 1
                print(f"  MISMATCH {sid}: sent {got!r}, Codex shows "
                      f"{expected!r}")
        elif got:
            derived_count += 1
        check(f"{path.name}: title within the send cap",
              got is None or len(got) <= 200)
    print(f"real rollouts: {len(real)} scanned; {named} named by Codex, "
          f"{derived_count} titled from their first prompt")
    if named:
        check("every Codex-named session is sent under Codex's own name",
              mismatched == 0)
    else:
        print("real rollouts: Codex has named none of them on this machine — "
              "the ladder is pinned by the synthetic checks above")
else:
    print("real rollouts: none found, skipped")


_TMP.cleanup()
print(f"{'FAIL' if FAILURES else 'PASS'}: codex_session_title")
for f in FAILURES:
    print(f"  - {f}")
sys.exit(1 if FAILURES else 0)
