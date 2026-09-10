#!/usr/bin/env python3
"""The Stop sensor for harness-tied memory (spec §4.1–§4.4) — FLAGGED OFF.

Nothing in this file runs unless `MEMHUB_HARNESS_EXTRACT` is on. With it on,
a session drafts lessons at each turn's Stop, a post-session review turns the
drafts into proposals, and the proposals are filed `proposed` for a human to
activate. Nothing here fires a rule and nothing here activates one.

Modes (argv[1]):

  stop      Stop hook entry — returns in milliseconds. Checks the flag, spawns
            `extract` DETACHED for the turn that just ended, and, once per
            session, the `idle` waiter. Stdin: the host's Stop payload.
  session   SessionStart hook entry. Drafts left un-reviewed by an earlier
            session in this repo → spawns `review`; reviewed rows left unsent
            → spawns `sync`; and tells the author, in one systemMessage line,
            what an earlier review proposed (§4.3 step 5).
  pr-open   Spawned by `pr_babysit_trigger.py` after a `gh pr create` that
            returned a URL: review this session's drafts now, stamped with
            the PR number.
  extract   (child) one turn: router → window → POST → stamp → local draft.
            Shares `harness_extract.extract_turn` with the replay, so what the
            scorecard measured is what runs.
  idle      (child) waits for 30 minutes of transcript silence, then reviews.
  review    (child) the post-session review — headless `claude -p` over the
            session's drafts, its digest and the cached book. Keeps or drops;
            never rewrites a row and never activates. Runs under
            `MEMHUB_HARNESS_CHILD=1` so `claude_hook_guard.py` disarms every
            hook for it — without that the review captures itself into the
            repo brain and shows on the fleet board (it has happened).
  sync      (child) reviewed rows → `create_rule(state=…)` over MCP, behind a
            per-row watermark (the reply's `rule_id` is written back). Rows
            land `proposed`. `activate` is never passed.

Files, under $MEMHUB_HARNESS_DRAFTS (default ~/.config/memhub-plugin/drafts):

  <session>.jsonl            drafts, append-only (harness_extract.append_draft)
  <session>.meta.json        what the sensor knows about the session: repo,
                             cwd, transcript, last extracted turn, how many
                             drafts the review has seen, the PR number
  <session>.reviewed.jsonl   rows the review kept, each with its sync status

Every path fails open and silent (§6): a broken sensor must never touch the
tool call or the session. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import harness_extract as hx  # noqa: E402

IDLE_S = int(os.environ.get("MEMHUB_HARNESS_IDLE_S", "1800"))      # §4.3: 30 min
IDLE_POLL_S = int(os.environ.get("MEMHUB_HARNESS_IDLE_POLL_S", "60"))
IDLE_GIVE_UP_S = 12 * 3600      # a waiter that never sees silence still exits
REVIEW_TIMEOUT_S = int(os.environ.get("MEMHUB_HARNESS_REVIEW_TIMEOUT", "180"))
REVIEW_GIVE_UP_AFTER = 3        # failed review attempts before the drafts are let go
REVIEW_MODEL = os.environ.get("MEMHUB_HARNESS_REVIEW_MODEL", "sonnet")
REVIEW_DIGEST_CHARS = 20000     # the session, as the reviewer sees it
REVIEW_BOOK_ROWS = 60           # cached rules offered for `supersedes_rule_id`
SYNC_TIMEOUT_S = float(os.environ.get("MEMHUB_HARNESS_SYNC_TIMEOUT", "30"))
SYNC_BATCH = 20                 # rows per sync child
STATE_KEYS = ("repo", "session_id", "turn", "hook_version", "at")   # §5.1
# What the sync sends, and nothing else. Local bookkeeping (`_reason`,
# `_rationale`, `_kind`, `_review`, `_sync*`) never crosses the wire.
WIRE_KEYS = ("title", "statement", "delivery", "matcher", "ordering", "anchors",
             "source", "source_ref", "state", "scope_repos", "supersedes_rule_id")


# --------------------------------------------------------------- plumbing
def _log(msg: str) -> None:
    try:
        path = hx.log_path("stop.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _base() -> Path:
    return hx.drafts_path("x").parent


def _safe(session: str) -> str:
    return hx.drafts_path(session).stem


def meta_path(session: str) -> Path:
    return _base() / f"{_safe(session)}.meta.json"


def reviewed_path(session: str) -> Path:
    return _base() / f"{_safe(session)}.reviewed.jsonl"


def load_meta(session: str) -> dict:
    try:
        got = json.loads(meta_path(session).read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def _publish(path: Path, text: str) -> None:
    """Atomic, 0600 — `atomic_write.publish` beside this file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import atomic_write  # noqa: PLC0415
        atomic_write.publish(path, text)
    except Exception:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


def save_meta(session: str, **fields) -> dict:
    meta = load_meta(session)
    meta.update(fields)
    meta["session_id"] = session
    _publish(meta_path(session), json.dumps(meta, indent=1, default=str))
    return meta


def write_rows(path: Path, rows: list[dict]) -> None:
    _publish(path, "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n"
                           for r in rows))


def _spawn(mode: str, *args: str, log_name: str = "stop.log") -> int:
    return hx.spawn_detached([mode, *args], script=Path(__file__).resolve(),
                             log_name=log_name)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def env_name() -> str:
    """Which MemHub the stamp's `env` names — derived from the plugin's own
    backend URL, never configured separately (the prod/staging split lives in
    the install, and a second setting is a second thing to point wrong)."""
    try:
        from _memhub_auth import default_url  # noqa: PLC0415
        host = default_url()
    except Exception:
        return "unknown"
    return "staging" if "staging" in host else "production"


def repo_of(cwd: str) -> str:
    rh = hx._hook()
    if rh is None or not cwd:
        return ""
    try:
        return rh.repo_info(cwd)[0] or ""
    except Exception:
        return ""


def _read_payload() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _args(**over) -> argparse.Namespace:
    """The replay's argument shape, for `extract_turn`."""
    base = {"hook_version": "", "env": env_name(), "budget": hx.DEFAULT_BUDGET,
            "no_model": False, "draft_timeout": 0, "out": "", "quiet": True,
            "trace": "", "stats": "", "turn": None}
    base.update(over)
    return argparse.Namespace(**base)


# --------------------------------------------------------------- stop lane
def cmd_stop(payload: dict) -> int:
    """Millisecond budget: two Popen calls at most, no reading of the
    transcript, no network."""
    session = str(payload.get("session_id") or "").strip()
    transcript = str(payload.get("transcript_path") or "").strip()
    cwd = str(payload.get("cwd") or "").strip()
    if not session or not transcript or not os.path.isfile(transcript):
        return 0
    if payload.get("stop_hook_active"):
        # The host re-entered Stop because a Stop hook asked it to continue.
        # The turn is the same turn; the first firing already spawned for it.
        return 0
    _spawn("extract", "--session", session, "--transcript", transcript,
           "--cwd", cwd)
    if not _alive(load_meta(session).get("idle_pid")):
        _spawn("idle", "--session", session, "--transcript", transcript,
               "--cwd", cwd)
    return 0


def cmd_extract(session: str, transcript: str, cwd: str) -> int:
    """The child. One turn — the last one in the transcript — through the
    same path the replay measures."""
    try:
        turns = hx.turns_from_transcript(transcript)
    except (OSError, ValueError) as exc:
        _log(f"extract {session[:8]}: cannot read transcript: {exc!r}")
        return 0
    if not turns:
        return 0
    last, prev = turns[-1], (turns[-2] if len(turns) > 1 else None)
    meta = load_meta(session)
    marker = f"{last.get('n')}:{last.get('uuid', '')}"
    if meta.get("last_extracted") == marker:
        return 0                          # Stop fired twice for one turn
    rh = hx._hook()
    arcs = rh.take_error_arcs(session) if rh is not None \
        and hasattr(rh, "take_error_arcs") else []
    repo = repo_of(cwd or last.get("cwd") or "")
    doc = {"session": session, "cwd": cwd or last.get("cwd") or "",
           "repo": repo, "source": "claude-live", "turns": turns}
    out_path = hx.drafts_path(session)
    kept = hx.read_drafts(out_path)
    stats = hx.new_stats(doc)
    trace = hx.Trace(str(hx.log_path("extract.log")), quiet=True)
    try:
        hx.extract_turn(last, prev, doc=doc, args=_args(), kept=kept, stats=stats,
                        trace=trace, out_path=out_path, arcs=arcs,
                        pr_number=meta.get("pr_number"))
    finally:
        trace.close()
    save_meta(session, repo=repo, cwd=doc["cwd"], transcript_path=transcript,
              last_extracted=marker, last_turn=last.get("n"),
              last_stop_at=time.time(), drafts=len(hx.read_drafts(out_path)))
    _log(f"extract {session[:8]} t{last.get('n')}: sent={stats['turns_sent']} "
         f"rows={stats['rows']} refusals={stats['refusals']} arcs={len(arcs)}")
    return 0


def cmd_idle(session: str, transcript: str, cwd: str) -> int:
    """Wait for IDLE_S of transcript silence, review once, exit."""
    save_meta(session, idle_pid=os.getpid())
    started = time.time()
    while True:
        time.sleep(IDLE_POLL_S)
        try:
            mtime = os.path.getmtime(transcript)
        except OSError:
            break                         # the transcript is gone: nothing to wait for
        if time.time() - mtime >= IDLE_S:
            break
        if time.time() - started > IDLE_GIVE_UP_S:
            _log(f"idle {session[:8]}: gave up after {IDLE_GIVE_UP_S}s")
            return 0
    review_and_sync(session, moment="idle")
    return 0


# ------------------------------------------------------------- the review
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"},
            "supersedes_rule_id": {"type": ["string", "null"]},
            "why": {"type": "string"}},
            "required": ["index", "supersedes_rule_id", "why"]}},
        "drop": {"type": "array", "items": {"type": "object", "properties": {
            "index": {"type": "integer"}, "why": {"type": "string"}},
            "required": ["index", "why"]}},
    },
    "required": ["keep", "drop"],
}

REVIEW_PROMPT = """\
You are the post-session reviewer for an engineering team's coding-agent \
harness. A session drafted lessons — proposed team rules — one at a time, \
without seeing the whole session. You see all of them, a digest of the \
session, and the team's current rulebook. Decide, for each draft, KEEP or DROP.

DROP a draft when:
- the session later abandoned or reversed the path it came from;
- it records project state or narrative (what this PR does, what was decided \
for this ticket) rather than a behaviour that repeats;
- it duplicates another draft in this set — keep the one with the better \
trigger, drop the rest;
- it restates a rule already in the rulebook without changing it.

KEEP a draft that updates or corrects an existing rule, and set \
`supersedes_rule_id` to that rule's id (only an id from the rulebook you were \
shown; otherwise null).

Keep at most the budget stated. You never rewrite a draft and you never \
activate anything: a human reads every row you keep. Answer with JSON only, \
by calling the StructuredOutput tool; never ask a question."""


def session_digest(session: str, meta: dict) -> str:
    """The session as prose the reviewer can read in one pass: each human
    turn and the agent's final words, most recent last, bounded."""
    tp = meta.get("transcript_path") or ""
    turns: list[dict] = []
    if tp and os.path.isfile(tp):
        try:
            turns = hx.turns_from_transcript(tp)
        except (OSError, ValueError):
            turns = []
    lines = []
    for t in turns:
        lines.append(f"t{t.get('n')} USER: {(t.get('user') or '')[:300]}\n"
                     f"     AGENT: {(t.get('asst') or '')[-300:]}")
    text = "\n".join(lines)
    if len(text) > REVIEW_DIGEST_CHARS:
        text = "…\n" + text[-REVIEW_DIGEST_CHARS:]
    return hx.redact_window(text) if text else "(no transcript available)"


def cached_book(repo: str) -> list[dict]:
    rh = hx._hook()
    if rh is None or not repo:
        return []
    try:
        rules, _v, _at, _src = rh.load_rules(repo)
    except Exception:
        return []
    out = []
    for r in rules[:REVIEW_BOOK_ROWS]:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        out.append({"id": str(r["id"]), "title": str(r.get("_label") or r["id"])[:80],
                    "statement": str(r.get("text") or "")[:200],
                    "status": r.get("status", "active")})
    return out


def review_input(drafts: list[dict], digest: str, book: list[dict],
                 budget: int) -> str:
    L = [f"BUDGET: keep at most {budget} of the {len(drafts)} drafts.", "",
         "DRAFTS:"]
    for i, r in enumerate(drafts, 1):
        engine = {k: r[k] for k in ("matcher", "ordering", "anchors") if k in r}
        L += [f"[{i}] {r.get('title', '')}",
              f"    statement: {r.get('statement', '')}",
              f"    trigger: {json.dumps(engine, ensure_ascii=False)}",
              f"    from: {r.get('source_ref', '')} ({r.get('_reason', '')})"]
    L += ["", "RULEBOOK (existing rules, for supersedes_rule_id):"]
    L += [f"- {b['id']} [{b['status']}] {b['title']}: {b['statement']}" for b in book] \
        or ["  (none cached)"]
    L += ["", "SESSION DIGEST:", digest]
    return "\n".join(L)


class ReviewError(Exception):
    """A bounded review that produced nothing usable. Drafts stay put."""


def call_review(user: str, cwd: str = "", timeout: int = 0) -> dict:
    """ONE headless `claude -p` attempt, disarmed twice over.

    `--safe-mode` keeps the child from loading this plugin (and every other
    customization); `MEMHUB_HARNESS_CHILD=1` in the environment is honoured by
    `claude_hook_guard.py` on every host. Both, because the first is a
    Claude-Code-only flag and the second is what survives a host that drops it.
    """
    cmd = ["claude", "-p", "--model", REVIEW_MODEL, "--safe-mode",
           "--output-format", "json", "--json-schema", json.dumps(REVIEW_SCHEMA),
           "--no-session-persistence",
           "--disallowedTools", "Bash", "Read", "Edit", "Write", "MultiEdit",
           "Agent", "Grep", "Glob", "WebSearch", "WebFetch",
           "--append-system-prompt", REVIEW_PROMPT]
    kwargs = {"input": user, "capture_output": True, "text": True,
              "timeout": timeout or REVIEW_TIMEOUT_S, "env": hx.child_env()}
    if cwd and os.path.isdir(cwd):
        kwargs["cwd"] = cwd
    try:
        proc = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        raise ReviewError(f"timeout after {kwargs['timeout']}s")
    except (OSError, ValueError) as exc:
        raise ReviewError(f"spawn failed: {exc!r}")
    if proc.returncode != 0:
        raise ReviewError(f"exit {proc.returncode}: {(proc.stderr or '')[-200:].strip()}")
    try:
        envelope = json.loads(proc.stdout)
    except ValueError:
        raise ReviewError(f"unparseable stdout: {(proc.stdout or '')[:200]}")
    if not isinstance(envelope, dict) or envelope.get("is_error"):
        raise ReviewError(f"cli error: {str(envelope)[:200]}")
    payload = envelope.get("structured_output")
    if payload is None:
        payload = envelope.get("result")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            raise ReviewError(f"result not JSON: {payload[:200]}")
    if not isinstance(payload, dict) or not isinstance(payload.get("keep"), list):
        raise ReviewError("off-contract reply")
    return payload


def apply_review(drafts: list[dict], verdict: dict, book: list[dict],
                 budget: int, pr_number=None) -> tuple[list[dict], list[dict]]:
    """(kept rows, dropped rows). Rows are never rewritten — a kept row is the
    server-validated row plus `supersedes_rule_id` (an id from the cached
    book only), the PR number when known, and a `_review` note."""
    known = {b["id"] for b in book}
    kept, dropped = [], []
    seen = set()
    for item in verdict.get("keep") or []:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if not isinstance(idx, int) or not 1 <= idx <= len(drafts) or idx in seen:
            continue
        seen.add(idx)
        if len(kept) >= budget:
            break
        row = dict(drafts[idx - 1])
        sup = item.get("supersedes_rule_id")
        row["supersedes_rule_id"] = sup if isinstance(sup, str) and sup in known else None
        if pr_number is not None:
            row["state"] = dict(row.get("state") or {}, pr_number=pr_number)
        row["_review"] = {"index": idx, "why": str(item.get("why") or "")[:300],
                          "at": time.time()}
        kept.append(row)
    for i, row in enumerate(drafts, 1):
        if i not in seen:
            why = next((str(d.get("why") or "")[:300] for d in (verdict.get("drop") or [])
                        if isinstance(d, dict) and d.get("index") == i), "not kept")
            dropped.append(dict(row, _review={"index": i, "why": why}))
    return kept, dropped


def review(session: str, *, moment: str, pr_number=None) -> int:
    """§4.3. Returns the number of rows kept, or -1 when nothing was reviewed."""
    meta = load_meta(session)
    if pr_number is not None:
        meta = save_meta(session, pr_number=pr_number)
    drafts = hx.read_drafts(hx.drafts_path(session))
    seen = int(meta.get("reviewed_through") or 0)
    new = drafts[seen:]
    if not new:
        return -1
    already = len(hx.read_drafts(reviewed_path(session)))
    budget = max(0, hx.DEFAULT_BUDGET - already)
    book = cached_book(meta.get("repo") or "")
    digest = session_digest(session, meta)
    try:
        verdict = call_review(review_input(new, digest, book, budget),
                              cwd=meta.get("cwd") or "")
    except ReviewError as exc:
        failures = int(meta.get("review_failures") or 0) + 1
        _log(f"review {session[:8]} ({moment}): {exc} [attempt {failures}]")
        if failures >= REVIEW_GIVE_UP_AFTER:
            # Three bounded attempts is the retry budget. The drafts stay in
            # their file for a human; the review stops spending on them.
            save_meta(session, reviewed_through=len(drafts), review_failures=0,
                      reviewed_at=time.time(), review_gave_up=True)
        else:
            save_meta(session, review_failures=failures)
        return -1
    kept, dropped = apply_review(new, verdict, book, budget, pr_number=meta.get("pr_number"))
    if kept:
        with reviewed_path(session).open("a", encoding="utf-8") as fh:
            for row in kept:
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    save_meta(session, reviewed_through=len(drafts), reviewed_at=time.time(),
              review_failures=0, review_moment=moment,
              dropped=int(meta.get("dropped") or 0) + len(dropped))
    _log(f"review {session[:8]} ({moment}): {len(new)} new drafts, kept {len(kept)}, "
         f"dropped {len(dropped)}")
    return len(kept)


# ------------------------------------------------------------------- sync
def _rulebook_for(repo: str) -> str | None:
    """§3.3: the client picks from its cached book — the book most of the
    repo's active rules live in. None when the cache carries no book facts
    (an older backend), and the server then decides."""
    rh = hx._hook()
    if rh is None or not repo:
        return None
    try:
        rules, _v, _at, _src = rh.load_rules(repo)
    except Exception:
        return None
    counts: dict[str, int] = {}
    for r in rules:
        rid = r.get("_rulebook_id") if isinstance(r, dict) else None
        if rid:
            counts[str(rid)] = counts.get(str(rid), 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


def wire_row(row: dict, rulebook_id: str | None) -> dict:
    body = {k: row[k] for k in WIRE_KEYS if k in row and row[k] not in (None, [], {})}
    body["source"] = "session_draft"
    if rulebook_id:
        body["rulebook_id"] = rulebook_id
    # `activate` is never sent, whatever the row says: activation is a human act.
    body.pop("activate", None)
    return body


_PERMANENT = re.compile(r"state_required|hook_version|twin|already|validation|invalid"
                        r"|refused|not allowed|too long|unknown rulebook|not a member", re.I)


def _rule_id_of(res) -> str | None:
    structured = getattr(res, "structured", None)
    if isinstance(structured, dict):
        for key in ("rule_id", "id"):
            if structured.get(key):
                return str(structured[key])
        inner = structured.get("result")
        if isinstance(inner, dict) and inner.get("rule_id"):
            return str(inner["rule_id"])
    for text in (getattr(b, "text", None) for b in getattr(res, "content", []) or []):
        if not text:
            continue
        try:
            got = json.loads(text)
        except ValueError:
            m = re.search(r"rule_id[\"']?\s*[:=]\s*[\"']?([0-9a-f-]{36})", text)
            if m:
                return m.group(1)
            continue
        if isinstance(got, dict):
            for key in ("rule_id", "id"):
                if got.get(key):
                    return str(got[key])
    return None


def sync(session: str) -> int:
    """Reviewed rows → create_rule, each once. Returns rows filed this pass."""
    path = reviewed_path(session)
    rows = hx.read_drafts(path)
    pending = [r for r in rows if not r.get("rule_id") and not r.get("_sync_refused")]
    if not pending:
        return 0
    try:
        from _memhub_auth import resolve_bearer  # noqa: PLC0415
        import mcp_http  # noqa: PLC0415
        url, bearer = resolve_bearer(refresh=False)
    except Exception as exc:
        _log(f"sync {session[:8]}: no credential ({exc!r})")
        return 0
    if not bearer:
        _log(f"sync {session[:8]}: no credential")
        return 0
    repo = (load_meta(session).get("repo") or "")
    rulebook_id = _rulebook_for(repo)
    filed = 0
    for row in pending[:SYNC_BATCH]:
        state = row.get("state") or {}
        missing = [k for k in STATE_KEYS if state.get(k) in (None, "")]
        if missing:
            row["_sync_refused"] = f"state_missing:{','.join(missing)}"
            continue
        try:
            res = mcp_http.call_tool(url, bearer, "create_rule",
                                     wire_row(row, rulebook_id), timeout=SYNC_TIMEOUT_S)
        except Exception as exc:          # transport: leave it for the next Stop
            row["_sync_error"] = str(exc)[:200]
            _log(f"sync {session[:8]}: {row.get('source_ref')} transport: {str(exc)[:120]}")
            continue
        if getattr(res, "is_error", False):
            text = " ".join(mcp_http.texts_of(res))[:300]
            if _PERMANENT.search(text):
                row["_sync_refused"] = text
            else:
                row["_sync_error"] = text
            _log(f"sync {session[:8]}: {row.get('source_ref')} refused: {text[:120]}")
            continue
        rid = _rule_id_of(res)
        row["rule_id"] = rid or "filed"
        row["synced_at"] = time.time()
        row.pop("_sync_error", None)
        filed += 1
    write_rows(path, rows)
    if filed:
        _log(f"sync {session[:8]}: filed {filed} row(s) as proposed")
    return filed


def review_and_sync(session: str, *, moment: str, pr_number=None) -> None:
    review(session, moment=moment, pr_number=pr_number)
    sync(session)


# ---------------------------------------------------------- session start
def _sessions_in(repo: str) -> list[dict]:
    out = []
    try:
        for p in _base().glob("*.meta.json"):
            meta = load_meta(p.name[:-len(".meta.json")])
            if meta.get("session_id") and (not repo or meta.get("repo") == repo):
                out.append(meta)
    except OSError:
        pass
    return out


def cmd_session(payload: dict) -> int:
    cwd = str(payload.get("cwd") or "").strip() or os.getcwd()
    repo = repo_of(cwd)
    if not repo:
        return 0
    announce = []
    for meta in _sessions_in(repo):
        sid = meta["session_id"]
        if sid == payload.get("session_id"):
            continue
        drafts = int(meta.get("drafts") or 0)
        if drafts > int(meta.get("reviewed_through") or 0):
            _spawn("review", "--session", sid, "--moment", "session-start")
            continue
        rows = hx.read_drafts(reviewed_path(sid))
        if any(not r.get("rule_id") and not r.get("_sync_refused") for r in rows):
            _spawn("sync", "--session", sid)
        filed = [r for r in rows if r.get("rule_id")]
        if filed and not meta.get("announced"):
            announce += [str(r.get("title") or "")[:60] for r in filed]
            save_meta(sid, announced=True)
    if announce:
        titles = "; ".join(announce[:5]) + ("; …" if len(announce) > 5 else "")
        line = (f"MemHub harness: {len(announce)} rule(s) proposed from an earlier "
                f"session in this repo, waiting for a reviewer in Studio — {titles}")
        print(json.dumps({"systemMessage": line}))
    return 0


# ------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("mode", choices=("stop", "session", "pr-open", "extract",
                                    "idle", "review", "sync"))
    p.add_argument("--session", default="")
    p.add_argument("--transcript", default="")
    p.add_argument("--cwd", default="")
    p.add_argument("--pr", type=int, default=None)
    p.add_argument("--moment", default="manual")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    if not hx.extract_enabled():
        if args.mode in ("stop", "session", "pr-open"):
            try:
                sys.stdin.read()          # drain the hook payload, say nothing
            except Exception:
                pass
        return 0
    if args.mode == "stop":
        return cmd_stop(_read_payload())
    if args.mode == "session":
        return cmd_session(_read_payload())
    if args.mode == "pr-open":
        if args.session and args.pr:
            review_and_sync(args.session, moment="pr-open", pr_number=args.pr)
        return 0
    if not args.session:
        return 0
    if args.mode == "extract":
        return cmd_extract(args.session, args.transcript, args.cwd)
    if args.mode == "idle":
        return cmd_idle(args.session, args.transcript, args.cwd)
    if args.mode == "review":
        review_and_sync(args.session, moment=args.moment, pr_number=args.pr)
        return 0
    if args.mode == "sync":
        sync(args.session)
        return 0
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except BaseException:                 # noqa: BLE001 — §6: silent, exit 0
        if os.environ.get("MEMHUB_HARNESS_DEBUG"):
            import traceback
            traceback.print_exc()
        rc = 0
    sys.exit(rc or 0)
