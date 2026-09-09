#!/usr/bin/env python3
"""Adapter: MemHub staging `team_memory_messages` rows -> the replay window.

Every number in the harness-tied-memory work so far comes from one engineer's
machine, which is the honest weakness of the whole design. Teammates' sessions
are already captured on staging, and the rows keep everything the replay window
needs — `role`, `ordinal`, and the `parts[]` render script with each tool call's
`input` and `output`. Only oversized tool results are elided. So a teammate's
session replays with NO local transcript.

READ ONLY, on purpose and by construction:
  * the connection is opened with `SET SESSION CHARACTERISTICS AS TRANSACTION
    READ ONLY` (psycopg's `readonly=True`), so the server refuses a write;
  * every statement in this file is a SELECT;
  * nothing here calls MemHub's API, mints a rule, or saves an artifact.
Drafts from a replayed session go to a LOCAL file. Nothing this script touches
gets written back to staging.

The DSN comes from `SUPABASE_DATABASE_URL` in a `.env` file you name (default:
`~/xtrace/MemHub-Backend/.env.staging`) or from `MEMHUB_STAGING_DSN`. It is
never printed, never logged, and never written into an output file.

Usage:
  staging_sessions.py list  [--min-turns 6] [--since 2026-08-01] [--limit 40]
  staging_sessions.py fetch --conv <conv_id> --out turns.json
  staging_sessions.py corpus --out DIR [--sessions 20] [--min-engineers 2]

`corpus` is the one S0 uses: it picks correction-bearing sessions spread across
engineers and writes one canonical turns JSON per session, which
`harness_extract.py --turns` consumes unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_ENV_FILE = "~/xtrace/MemHub-Backend/.env.staging"

# The same harness-noise filters the local reader uses. A `<task-notification>`
# or a skill body is not a human turn, and counting one as a correction would
# inflate every number in the scorecard.
_SYS_BLOCK = re.compile(
    r"<system-reminder>.*?</system-reminder>"
    r"|<task-notification>.*?</task-notification>"
    r"|<command-name>.*?</command-name>"
    r"|<local-command-stdout>.*?</local-command-stdout>",
    re.S,
)
_HARNESS_PREFIX = ("Base directory for this skill",
                   "Continue from where you left off",
                   "Caveat: The messages below",
                   # A /loop wakeup re-injects the skill body as a "user"
                   # message every tick. Counting one as a human turn drafts
                   # rules from the harness talking to itself.
                   "Skill /", "Skill ",
                   # /compact re-injects its summary as a "user" message; it
                   # quotes the whole conversation, so every router regex
                   # matches it at once.
                   "This session is being continued from a previous conversation",
                   "<", "#")

# Correction shape, for CANDIDATE SELECTION ONLY. This is not the router and
# it is not scored: it decides which sessions are worth spending model calls
# on, and a session it misses simply is not in the sample.
CORRECTION = re.compile(
    r"^(no|nope|wrong|wait|stop|nah)\b"
    r"|\b(i mean|i meant|i said|u are (still )?wrong|you are (still )?wrong"
    r"|why did (u|you)|check staging|on staging|test on staging"
    r"|have u (actually |really )?(run|test|fix|verif)"
    r"|is it (actually |really )?working|so have u|did u (just|actually)"
    r"|we already have|don'?t we already|already exists|try again"
    r"|still (not|broken|fail)|that'?s not what)\b", re.I)


# ----------------------------------------------------------------- secrets
def read_dsn(env_file: str) -> str:
    """The staging DSN. Returned, never printed."""
    dsn = os.environ.get("MEMHUB_STAGING_DSN", "").strip()
    if dsn:
        return dsn
    path = Path(os.path.expanduser(env_file))
    if not path.is_file():
        sys.exit(f"no DSN: set MEMHUB_STAGING_DSN or provide {env_file}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("SUPABASE_DATABASE_URL"):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit(f"SUPABASE_DATABASE_URL not found in {env_file}")


def connect(env_file: str):
    """A read-only connection. psycopg2 (v2) or psycopg (v3), whichever is
    importable — this is offline analysis tooling, not a hook, so a driver is
    a fair dependency; the plugin's hook path stays stdlib-only."""
    dsn = read_dsn(env_file)
    try:
        import psycopg2                                    # noqa: PLC0415
    except ImportError:
        psycopg2 = None
    if psycopg2 is not None:
        conn = psycopg2.connect(dsn, connect_timeout=20)
        conn.set_session(readonly=True, autocommit=True)
        return conn
    try:
        import psycopg                                     # noqa: PLC0415
    except ImportError:
        sys.exit("needs psycopg2 or psycopg; e.g. run under "
                 "~/xtrace/MemHub-Backend/.venv/bin/python")
    return psycopg.connect(dsn, connect_timeout=20, autocommit=True,
                           options="-c default_transaction_read_only=on")


def engineer_label(user_id) -> str:
    """A stable, non-identifying label for an engineer, so the scorecard can
    say "≥2 engineers" without putting teammates' user ids in a PR body."""
    digest = hashlib.sha256(str(user_id).encode()).hexdigest()
    return "eng-" + digest[:6]


# ------------------------------------------------------- rows -> the window
def _is_harness_text(txt: str) -> bool:
    return (not txt) or txt.startswith(_HARNESS_PREFIX)


def _brief(name: str, tool_input) -> str:
    i = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        return f"Bash: {str(i.get('command', ''))[:200]}"
    if name in ("Edit", "Write", "MultiEdit", "Read", "NotebookEdit"):
        return f"{name}: {i.get('file_path', '')}"
    return f"{name}: {json.dumps(i, default=str)[:100]}"


def _target(name: str, tool_input) -> str:
    i = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        return str(i.get("command", ""))
    return str(i.get("file_path", "") or "")


def _output_text(part: dict) -> str:
    """The tool result as text, from whichever shape the row carries.

    `output` is `{stdout, stderr}` for Bash, a bare string or `{text}` for the
    rest, and an error also lands in `errorText`. An elided oversized result
    reads as empty here — which is correct: the window then shows the call and
    not its body, rather than a lie about what came back.
    """
    out = part.get("output")
    chunks = []
    if isinstance(out, dict):
        for key in ("stdout", "stderr", "text", "content"):
            val = out.get(key)
            if isinstance(val, str) and val.strip():
                chunks.append(val)
    elif isinstance(out, str):
        chunks.append(out)
    err = part.get("errorText")
    if isinstance(err, str) and err.strip() and err not in chunks:
        chunks.append(err)
    return "\n".join(chunks)


def _parts_of(row_parts, row_tool_calls) -> list[dict]:
    """`parts` is the faithful record; `tool_calls` is the lossy projection it
    supersedes. Prefer parts, fall back for rows written before it existed."""
    if isinstance(row_parts, list):
        return row_parts
    if isinstance(row_tool_calls, list):
        out = []
        for tc in row_tool_calls:
            if not isinstance(tc, dict):
                continue
            if tc.get("type") == "tool_call":
                args = tc.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {"_raw": args}
                out.append({"type": f"tool-{tc.get('tool_name', '?')}",
                            "input": args,
                            "state": "output-error" if tc.get("is_error")
                                     else "output-available",
                            "output": tc.get("result")})
            elif tc.get("type") == "text":
                out.append({"type": "text", "text": tc.get("text", "")})
        return out
    return []


def rows_to_turns(rows: list[dict]) -> list[dict]:
    """Message rows in ordinal order -> the canonical turn list.

    A turn is one human message plus everything the agent did before the next
    one — identical to how the local reader groups a .jsonl, so the same
    window builder serves both sources.
    """
    turns: list[dict] = []
    cur: dict | None = None
    for row in rows:
        role = row.get("role")
        parts = _parts_of(row.get("parts"), row.get("tool_calls"))
        if role == "user":
            txt = _SYS_BLOCK.sub("", row.get("content") or "").strip()
            if _is_harness_text(txt):
                continue
            cur = {"n": len(turns) + 1, "user": txt, "tools": [],
                   "results": [], "asst": "",
                   "ts": str(row.get("event_date") or ""), "cwd": ""}
            turns.append(cur)
            continue
        if role != "assistant" or cur is None:
            continue
        text = (row.get("content") or "").strip()
        if text:
            cur["asst"] = text
        for part in parts:
            if not isinstance(part, dict):
                continue
            ptype = str(part.get("type") or "")
            if ptype == "text" and part.get("text", "").strip():
                cur["asst"] = part["text"]
                continue
            if not ptype.startswith("tool-"):
                continue
            name = ptype[len("tool-"):]
            tool_input = part.get("input")
            cur["tools"].append({"tool": name,
                                 "brief": _brief(name, tool_input),
                                 "target": _target(name, tool_input)})
            body = _output_text(part)
            cur["results"].append({
                "tool": name,
                "target": _target(name, tool_input),
                "error": part.get("state") == "output-error"
                         or bool(part.get("errorText")),
                "text": body[:600],
            })
    return turns


# --------------------------------------------------------------- discovery
LIST_SQL = """
select m.conv_id,
       c.user_id,
       coalesce(c.name, '') as name,
       coalesce(c.source_id, '') as source_id,
       count(*) as msgs,
       count(*) filter (where m.role = 'user') as user_msgs,
       min(m.created_at)::date as first_day,
       max(m.created_at)::date as last_day
from team_memory_messages m
join team_conversations c
  on c.conv_id = m.conv_id and c.workspace_id = m.workspace_id
where c.conversation_source_platform = 'claude'
  and m.created_at >= %(since)s
group by 1, 2, 3, 4
having count(*) filter (where m.role = 'user') >= %(min_turns)s
order by max(m.created_at) desc
limit %(limit)s
"""

FETCH_SQL = """
select m.ordinal, m.role, m.content, m.parts, m.tool_calls, m.event_date
from team_memory_messages m
where m.conv_id = %(conv)s
order by m.ordinal
"""

# `agentic_namespace` is the repo name the CLIENT resolved from its git remote
# for this session. It is the only trustworthy repo label on a staging row:
# the server never derives one from a cwd, and this adapter must not either —
# a worktree basename would scope a lesson to a repo that does not exist.
# NULL for a conversation whose client sent no namespace, and "" is the honest
# answer there.
META_SQL = """
select c.user_id, coalesce(c.name, ''), coalesce(c.source_id, ''),
       coalesce(c.agentic_namespace, '')
from team_conversations c
where c.conv_id = %(conv)s
limit 1
"""


def fetch_meta(cur, conv: str) -> tuple | None:
    cur.execute(META_SQL, {"conv": conv})
    return cur.fetchone()


def fetch_session(conn, conv: str) -> dict:
    cur = conn.cursor()
    meta = fetch_meta(cur, conv)
    cur.execute(FETCH_SQL, {"conv": conv})
    cols = ("ordinal", "role", "content", "parts", "tool_calls", "event_date")
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    turns = rows_to_turns(rows)
    user_id, name, source_id, scope_repo = (meta or (None, "", "", ""))
    # `source_id` is the platform session id, namespaced `cb:<uuid>:<session>`
    # when the conversation was imported into a brain.
    session_id = source_id.split(":")[-1] if source_id else str(conv)
    return {
        "session": session_id or str(conv),
        "conv_id": str(conv),
        "source": "staging",
        "engineer": engineer_label(user_id),
        "title": name,
        "repo": scope_repo or "",
        "cwd": "",
        "turns": turns,
    }


def correction_turns(turns: list[dict]) -> int:
    return sum(1 for t in turns if CORRECTION.search((t.get("user") or "")[:300]))


# ------------------------------------------------------------------- modes
def cmd_list(conn, args) -> None:
    cur = conn.cursor()
    cur.execute(LIST_SQL, {"since": args.since, "min_turns": args.min_turns,
                           "limit": args.limit})
    print(f"{'conv_id':38} {'engineer':10} {'msgs':>6} {'usr':>4}  last        title")
    for conv, user_id, name, _sid, msgs, usr, _first, last in cur.fetchall():
        print(f"{str(conv):38} {engineer_label(user_id):10} {msgs:6} "
              f"{usr:4}  {last}  {name[:44]}")
    cur.close()


def cmd_fetch(conn, args) -> None:
    doc = fetch_session(conn, args.conv)
    Path(args.out).write_text(json.dumps(doc, indent=1, default=str),
                              encoding="utf-8")
    print(f"{doc['session']} [{doc['engineer']}] {len(doc['turns'])} turns "
          f"({correction_turns(doc['turns'])} correction-shaped) -> {args.out}")


def drop_injected_turns(docs: list[dict], out_dir: Path,
                        index: list[dict]) -> int:
    """Remove 'user' turns whose text appears VERBATIM in another session.

    A human does not type the same 300 characters into two different sessions.
    The harness does: skill bodies, `/loop` prompts, and design-brief preambles
    are injected in the `user` role and read exactly like a standing
    instruction — "Approach this as the design lead… avoid templated designs"
    tripped `standing_rule_request` in three sessions at once. Prefix lists
    cannot keep up with these; identity across sessions catches them all, and
    catches nothing a person actually wrote.

    Short messages are exempt: "continue", "yes", "merge it" repeat legitimately.
    """
    counts: dict[str, int] = {}
    for doc in docs:
        for text in {t.get("user", "") for t in doc.get("turns", [])}:
            if len(text) >= 200:
                counts[text] = counts.get(text, 0) + 1
    injected = {t for t, n in counts.items() if n > 1}
    if not injected:
        return 0
    dropped = 0
    for doc in docs:
        kept = [t for t in doc.get("turns", [])
                if t.get("user", "") not in injected]
        dropped += len(doc["turns"]) - len(kept)
        for i, turn in enumerate(kept, 1):
            turn["n"] = i
        doc["turns"] = kept
        entry = next((e for e in index if e["session"] == doc["session"]), None)
        if entry:
            path = out_dir / entry["file"]
            path.write_text(json.dumps(doc, indent=1, default=str),
                            encoding="utf-8")
            entry["turns"] = len(kept)
            entry["correction_turns"] = correction_turns(kept)
    return dropped


def cmd_corpus(conn, args) -> None:
    """Correction-bearing sessions, spread across engineers.

    Round-robin by engineer rather than "the N biggest": the biggest sessions
    all belong to the same two people, and a corpus that is 18 sessions from
    one engineer and 2 from another does not answer the question S0 asks.
    """
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cur = conn.cursor()
    cur.execute(LIST_SQL, {"since": args.since, "min_turns": args.min_turns,
                           "limit": args.scan})
    candidates = cur.fetchall()
    cur.close()
    print(f"scanned {len(candidates)} conversations since {args.since}")

    by_engineer: dict[str, list] = {}
    for row in candidates:
        by_engineer.setdefault(engineer_label(row[1]), []).append(row)

    picked: list[dict] = []
    index: list[dict] = []
    engineers = sorted(by_engineer, key=lambda e: -len(by_engineer[e]))
    print("engineers with candidates: " +
          ", ".join(f"{e}({len(by_engineer[e])})" for e in engineers))

    cursor_at = {e: 0 for e in engineers}
    while len(picked) < args.sessions:
        progressed = False
        for eng in engineers:
            if len(picked) >= args.sessions:
                break
            rows = by_engineer[eng]
            while cursor_at[eng] < len(rows):
                row = rows[cursor_at[eng]]
                cursor_at[eng] += 1
                progressed = True
                conv = str(row[0])
                doc = fetch_session(conn, conv)
                ncorr = correction_turns(doc["turns"])
                if len(doc["turns"]) < args.min_turns or ncorr < args.min_corrections:
                    print(f"  skip {conv[:8]} [{eng}] turns={len(doc['turns'])} "
                          f"corrections={ncorr}")
                    continue
                path = out_dir / f"{eng}__{conv[:8]}.json"
                path.write_text(json.dumps(doc, indent=1, default=str),
                                encoding="utf-8")
                picked.append(doc)
                index.append({"engineer": eng, "conv_id": conv,
                              "session": doc["session"], "file": path.name,
                              "turns": len(doc["turns"]),
                              "correction_turns": ncorr,
                              "title": doc["title"][:80]})
                print(f"  keep {conv[:8]} [{eng}] turns={len(doc['turns'])} "
                      f"corrections={ncorr}  {doc['title'][:50]}")
                break
        if not progressed:
            break

    dropped = drop_injected_turns(picked, out_dir, index)
    if dropped:
        print(f"\ndropped {dropped} injected turns (identical 'user' text in "
              f"2+ sessions — skill bodies and loop prompts, not typed)")

    seen = sorted({d["engineer"] for d in index})
    (out_dir / "index.json").write_text(
        json.dumps({"sessions": index, "engineers": seen}, indent=1),
        encoding="utf-8")
    print(f"\n{len(index)} sessions from {len(seen)} engineers ({', '.join(seen)}) "
          f"-> {out_dir}/index.json")
    if len(seen) < args.min_engineers:
        print(f"WARNING: fewer than {args.min_engineers} engineers — the "
              f"scorecard's ≥2-engineer condition is NOT met.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                   help=f"file holding SUPABASE_DATABASE_URL (default {DEFAULT_ENV_FILE})")
    p.add_argument("--since", default="2026-06-01",
                   help="only conversations with messages on/after this date")
    p.add_argument("--min-turns", type=int, default=6,
                   help="minimum human turns for a session to be usable")
    sub = p.add_subparsers(dest="mode", required=True)

    ls = sub.add_parser("list", help="list candidate sessions")
    ls.add_argument("--limit", type=int, default=40)

    fe = sub.add_parser("fetch", help="one session -> canonical turns JSON")
    fe.add_argument("--conv", required=True)
    fe.add_argument("--out", required=True)

    co = sub.add_parser("corpus", help="a spread corpus for the scorecard")
    co.add_argument("--out", required=True, help="output directory")
    co.add_argument("--sessions", type=int, default=20)
    co.add_argument("--scan", type=int, default=200,
                    help="how many conversations to consider")
    co.add_argument("--min-corrections", type=int, default=1)
    co.add_argument("--min-engineers", type=int, default=2)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect(args.env_file)
    try:
        {"list": cmd_list, "fetch": cmd_fetch, "corpus": cmd_corpus}[args.mode](
            conn, args)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
