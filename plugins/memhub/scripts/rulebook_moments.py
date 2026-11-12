#!/usr/bin/env python3
"""The hook's newer moments, kept out of `rulebook_hook.py` (ENG-1235, ENG-1236).

`rulebook_hook.py` runs as `__main__` and sits at the directory export's
per-file size limit, so what these need from it (its state dir, its clock,
its atomic writer) is passed in; nothing here imports the hook.

* the base-branch probe cache: `Probes.base()` memoised ACROSS hook calls in
  a per-checkout file, keyed on HEAD, a `stat` fingerprint of the refs and
  the base the command names — a dozen git subprocesses (~250 ms) on every
  call that matched a `given` rule become one `rev-parse` and a few stats;
* the prompt lane's refresh policy: a book merely a minute old is refreshed
  detached and served as cached; only an old or missing book is waited for;
* UserPromptExpansion: the command the person typed, recorded so the submit
  that carries its expansion is known to be a body, not their words;
* the wire projections, the Bash-result helpers, the breadcrumb and the
  transcript tail readers: pure helpers the hook calls through wrappers;
* PostToolUseFailure: the host's `error` as the post lane's result text and
  response, so output rules fire on a failed command and a failure is never
  an ordering receipt.
* command-armed ordering rules (`armed_by_events: ["command"]`): what a
  successful Bash call arms, and the gate's words when it is collected.

Stdlib only; every function fails open.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

PROBE_CACHE_TTL_S = 600      # a cached base answer is trusted this long at most
PROMPT_BLOCK_AFTER_S = 600   # prompt lane: block on a fetch only past this; younger books refresh detached
EXPANSION_WINDOW_S = 5.0     # a command expanded this recently is what the next prompt submit carries

_EXIT_LINE = re.compile(r"\A\s*Exit code (\d+)[ \t]*\n?")


# ── a Bash result ───────────────────────────────────────────────────────────

def result_text(resp):
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, dict):
        parts = [v for k in ("stderr", "stdout", "output", "error", "text")
                 if isinstance((v := resp.get(k)), str) and v]
        return "\n".join(parts) if parts else json.dumps(resp, ensure_ascii=False)
    return str(resp)


# ── Codex: what its hook payloads leave out ─────────────────────────────────
# Codex's PostToolUse `tool_response` for a shell call is the command's bare
# output: no exit code, no error flag (seen on codex-cli 0.163). `bash_ok` then
# has only its text proxy, so a failed `git mv` reads as green and arms a
# command rule, and a gate receipt (strict: wants an explicit 0) never
# discharges — every gate ordering rule stays armed for the session. The
# rollout the payload names records the same call, keyed by the payload's
# `tool_use_id`, with its `exit_code` — and was found already written when the
# hook ran (52 of 53 calls checked live). Not always: on CI (2026-10-19) a
# failed `git mv` still armed a gate, the line evidently landing just after the
# hook read. So a Codex whose rollout does write `CommandExecution` items gets
# a short wait for this call's; one that writes none (0.146) is never waited on.
_CODEX_TAIL_BYTES = 4 * 1024 * 1024
_CODEX_CHUNK = 64 * 1024
_CODEX_WAIT_S = 0.6
_CODEX_POLL_S = 0.05


def codex_resp(data, resp, *, sleep=time.sleep, clock=time.monotonic):
    """`resp` with the exit code Codex left out, as `{"output", "exit_code"}`;
    `resp` unchanged when it is not a bare string, the transcript is not a
    Codex rollout, or the call's `CommandExecution` is not found. Only the
    rollout is trusted: the output is the command's own text, and a line in it
    saying "Exit code: 0" is no receipt. Never raises."""
    if not isinstance(resp, str):
        return resp
    try:
        tp, cid = data.get("transcript_path"), data.get("tool_use_id")
        if not (isinstance(tp, str) and isinstance(cid, str) and cid
                and os.path.basename(tp).startswith("rollout-")):
            return resp
        deadline, since = clock() + _CODEX_WAIT_S, 0
        while True:
            code, writes_them, since = _codex_scan(tp, cid, since)
            if code is not None:
                return {"output": resp, "exit_code": code}
            if not writes_them or clock() >= deadline:
                return resp
            sleep(_CODEX_POLL_S)
    except Exception:
        return resp


def _codex_scan(tp, cid, since=0):
    """(this call's exit code or None, whether this Codex writes
    `CommandExecution` items, where the next scan starts). A first scan reads
    the tail newest first; a later one only the whole lines written since."""
    key, seen = cid.encode(), since > 0
    with open(tp, "rb") as f:
        # Newest first, a chunk at a time: the call was just written, so
        # a hit is near the end and a long rollout is not read whole.
        end = f.seek(0, os.SEEK_END)
        floor, carry = max(since, end - _CODEX_TAIL_BYTES), b""
        f.seek(max(floor, end - _CODEX_CHUNK))
        tail = f.read(end - max(floor, end - _CODEX_CHUNK))
        cut = tail.rfind(b"\n")
        # Only whole lines are read: a line still being written waits for the
        # next scan rather than being passed over for good.
        whole = end - len(tail) + cut + 1 if cut >= 0 else (floor if since else end)
        end = whole
        while end > floor:
            start = max(floor, end - _CODEX_CHUNK)
            f.seek(start)
            block = f.read(end - start) + carry
            lines = block.split(b"\n")
            carry = lines[0] if start > floor else b""
            for raw in reversed(lines[1:] if start > floor else lines):
                if b'"CommandExecution"' in raw:
                    seen = True
                code = _codex_exit(raw, key, cid)
                if code is not None:
                    return code, True, whole
            end = start
    return None, seen, max(whole, since)


def _codex_exit(raw, key, cid):
    if key not in raw or b"CommandExecution" not in raw:
        return None
    try:
        p = json.loads(raw).get("payload") or {}
    except ValueError:
        return None
    item = p.get("item") if isinstance(p.get("item"), dict) else {}
    code = item.get("exit_code")
    if (item.get("type") == "CommandExecution" and item.get("id") == cid
            and isinstance(code, int) and not isinstance(code, bool)):
        return code
    return None


def codex_prompt(p):
    """A rollout `response_item` payload's prompt text if it is a message the
    person typed, else None: Codex also writes AGENTS.md and environment
    context as `user` messages (readers.codex.clean_user_text strips them)."""
    if not (isinstance(p, dict) and p.get("type") == "message" and p.get("role") == "user"):
        return None
    text = "\n".join(str(c.get("text") or "") for c in p.get("content") or []
                     if isinstance(c, dict))
    try:
        from readers.codex import clean_user_text  # noqa: PLC0415 — beside this file
    except Exception:
        return None
    return clean_user_text(text)


def bash_ok(resp, *, strict=False):
    """Did the Bash call succeed? Uses exit_code when the harness supplies it.
    Without one, the text proxy (same as the transcript replayer) is a guess a
    command's own output could forge — so `strict=True` (used for GATE-mode
    receipts) returns False unless an explicit exit_code says 0."""
    if resp is None:                      # no result at all is never a receipt
        return False
    if isinstance(resp, dict):
        if isinstance(resp.get("exit_code"), int):
            return resp["exit_code"] == 0
        if resp.get("is_error") or resp.get("isError"):
            return False
    if strict:
        return False
    txt = result_text(resp)
    # text proxy, anchored to pytest/traceback vocabulary — a green run whose
    # output merely mentions "error:" must not be mistaken for red
    return not re.search(
        r"(^|\n)(FAILED|ERROR)\b|\b\d+ (failed|errors?)\b|\nTraceback \(most recent call last\)"
        r"|(^|\n)npm ERR!|(^|\n)error(\[E\d+\])?:", txt)


# ── the breadcrumb ──────────────────────────────────────────────────────────

def breadcrumb(path, what, exc, *, now, atomic_json):
    """`path` (ledger/.last_error) — the one place a silent backstop failure is visible."""
    try:
        atomic_json(path, {"at": now(), "what": what, "error": str(exc)[:300]})
    except Exception:
        pass


def breadcrumb_clear(path, what):
    """Retract the breadcrumb once ``what``'s own lane has worked again.

    Without this a lane that records no success of its own — the judge — leaves
    a single blip standing until some OTHER lane happens to succeed. The judge
    runs on PreToolUse and the book is only refetched at SessionStart, so one
    1.5 s timeout mid-session would produce a health banner at the next session
    start, long after the lane had recovered. A warning that outlives its cause
    is the failure mode this file exists to avoid.

    Only clears a crumb this lane wrote: another lane's failure is still real.
    """
    try:
        with open(path, encoding="utf-8") as f:
            crumb = json.load(f)
        if not isinstance(crumb, dict) or crumb.get("what") != what:
            return
        os.unlink(path)
    except Exception:      # no crumb, unreadable, or already gone — all fine
        pass


# ── the wire ────────────────────────────────────────────────────────────────

WIRE_KEYS = ("fire_id", "rule_id", "rule_version", "session_id", "agent_id", "worktree",
             "repo", "branch", "tool", "hook_phase", "mode", "dedup_key",
             "raw_matches_before_fire", "fired_at", "source_message_id", "override_reason",
             "judge_score", "judge_verdict")
_JUDGE_WIRE_KEYS = ("judge_score", "judge_verdict")

# What the hook observed AFTER a fire (rule-fire-events-spec §2). Facts, never
# verdicts: `receipt` (an ordering rule's green run in this checkout),
# `converted` (a rule's converted_rx matched a command in this session),
# `dismissed` (the person set a rule aside, with the reason), `turn_end` (the
# session finished a turn), `session_end`. The server folds these into each
# fire's outcome; nothing here decides one.
EVENT_WIRE_KEYS = ("event_id", "kind", "rule_id", "session_id", "agent_id", "worktree",
                   "repo", "branch", "reason", "at")


# Capture namespaces a conversation id per host: Claude sends the session uuid
# bare (flush_turn.py), Codex sends `codex-<uuid>` (codex_flush.py) and Cursor
# `cursor-<uuid>` (cursor_flush.py). A fire carries the raw hook-payload
# session id, so on Claude it matches the captured conversation by accident and
# on the others it cannot match at all — every Codex fire shows "Not linked
# yet" (ENG-1075). Namespace it HERE, at the wire, and nowhere else: ordering
# state, obligations, dedup keys and state_path all key off the raw id, and
# renaming that mid-session would strand an in-flight session's own state.
def wire_session_id(session_id, host):
    """The session id as CAPTURE wrote it, so the server can join the two.

    Delegates to `pr_link.conversation_id_for`, which already owns this exact
    projection for the PR-link lane — a second copy is how the two lanes come
    to disagree about what a Codex session is called, and it already handles
    the cases a fresh one forgets (surrounding whitespace, a host spelled
    `Codex`, an id that already carries its prefix).

    Imported lazily and on a fail-open path: only the flush lane projects rows,
    so the per-call pre/post lanes never pay for the import, and a fire that
    cannot be namespaced still ships (unlinked) rather than taking the hook
    down.
    """
    try:
        import pr_link
        return pr_link.conversation_id_for(host, session_id) or session_id
    except Exception:
        return session_id


def wire_row(row):
    """The v2 ledger row minus `excerpt` (Phase 1: always stripped — the org
    opt-in for excerpts is a server setting the hook does not consult).

    `host` is local-only, like `rulebook_id`: it is not a wire field, it is
    how this projection knows which namespace the session id belongs in.

    The two judge keys are sent only on a row whose fire was judged (it has a
    `judge_verdict`). A fire nobody judged posts the row shape it always did,
    which a server older than the judge columns also accepts.
    """
    out = {k: row.get(k) for k in WIRE_KEYS}
    if row.get("judge_verdict") is None:
        for k in _JUDGE_WIRE_KEYS:
            out.pop(k, None)
    out["session_id"] = wire_session_id(out.get("session_id"), row.get("host"))
    return out


def event_wire_row(row):
    """Same projection as `wire_row`, and the same namespacing.

    The server folds these into each fire's outcome BY SESSION, so an event
    that names the session differently from the fire it answers folds into
    nothing. `host` is local-only here too.
    """
    out = {k: row.get(k) for k in EVENT_WIRE_KEYS}
    out["session_id"] = wire_session_id(out.get("session_id"), row.get("host"))
    return out


def effect_wire_row(row):
    """A window as the server takes it (fire_effect.WIRE_KEYS); the server
    finds the fire by id, so nothing here is namespaced."""
    import fire_effect  # noqa: PLC0415 — beside this file
    return fire_effect.wire_row(row)



# ── the probe cache ─────────────────────────────────────────────────────────


def refs_fingerprint(gitdir):
    """What `base()` depends on besides HEAD, read with `stat` only: the
    checkout's HEAD file, and the common dir's packed refs, FETCH_HEAD (a
    fetch rewrites it) and the loose-ref directories (a branch created or
    deleted). None when there is no gitdir — then nothing is cached."""
    if not gitdir:
        return None
    common = gitdir
    try:
        with open(os.path.join(gitdir, "commondir"), encoding="utf-8") as f:
            common = os.path.normpath(os.path.join(gitdir, f.read().strip()))
    except OSError:
        pass
    parts = []
    for path in (os.path.join(gitdir, "HEAD"), os.path.join(common, "packed-refs"),
                 os.path.join(common, "FETCH_HEAD"),
                 os.path.join(common, "refs", "remotes", "origin"),
                 os.path.join(common, "refs", "heads")):
        try:
            parts.append(str(os.stat(path).st_mtime_ns))
        except OSError:
            parts.append("-")
    return ":".join(parts)


def cached(path, name, key, compute, *, now, age_s, atomic_json, ttl=PROBE_CACHE_TTL_S):
    """`compute()` memoised in the JSON file at `path` under `name`, valid
    while the stored key equals `key` and the entry is younger than `ttl`.
    A corrupt or unreadable file is the cold path; a write that fails is
    silent. `key` None → not cached."""
    if key is not None:
        try:
            with open(path, encoding="utf-8") as f:
                hit = (json.load(f) or {}).get(name)
            if isinstance(hit, dict) and hit.get("key") == key and age_s(hit.get("at")) < ttl:
                return hit.get("value")
        except Exception:
            pass
    value = compute()
    if key is not None:
        try:
            cur = {}
            try:
                with open(path, encoding="utf-8") as f:
                    cur = json.load(f) or {}
            except Exception:
                cur = {}
            cur[name] = {"key": key, "at": now(), "value": value}
            os.makedirs(os.path.dirname(path), exist_ok=True)
            atomic_json(path, cur)
        except Exception:
            pass
    return value


# ── the prompt lane's refresh ───────────────────────────────────────────────


def refresh_for_prompt(age, *, blocking, detached, block_after=PROMPT_BLOCK_AFTER_S, refresh_after=60):
    """Which refresh a prompt gets for a book `age` seconds old: `blocking()`
    past `block_after` (or with no book at all, age inf), `detached()` past
    `refresh_after`, nothing for a fresh book. The prompt lane used to block
    310-390 ms (a 304 costs the round trip too) whenever the book was a
    minute old, which is every prompt after a pause; the pre lane already
    refreshes in the background every minute during work. What this gives
    up: a prompt-armed rule activated in the last ten minutes misses this
    prompt."""
    if age >= block_after:
        return blocking()
    if age >= refresh_after:
        detached()
    return None


# ── UserPromptExpansion ─────────────────────────────────────────────────────


def note_expansion(path, data, *, now, atomic_json):
    """Record the command the person typed and its expansion type at `path`."""
    try:
        atomic_json(path, {"command": str(data.get("command_name") or "")[:200],
                           "type": str(data.get("expansion_type") or "")[:40],
                           "prompt": str(data.get("prompt") or "")[:400],
                           "at": now()})
    except Exception:
        pass


def recent_expansion(path, *, age_s, window=EXPANSION_WINDOW_S):
    """True when a command expanded within `window` seconds: the prompt now
    being submitted is its body. The two events' order is not documented,
    so this is a window, not a handshake; the harness-prompt regex still
    applies when the submit lands first."""
    try:
        with open(path, encoding="utf-8") as f:
            rec = json.load(f)
        return isinstance(rec, dict) and age_s(rec.get("at")) < window
    except Exception:
        return False


# ── the transcript tail ─────────────────────────────────────────────────────


def tail_records(path, start_window, max_window):
    """Parsed JSONL records from the end of `path`, newest first, reading a
    growing tail (`start_window` bytes, ×4 up to `max_window`) rather than
    the whole file: transcripts grow to megabytes and the hook has a 5 s
    budget. A consumer returns as soon as it has its answer; exhausting the
    generator means the whole allowed tail held nothing."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        window = start_window
        while True:
            start = max(0, end - window)
            f.seek(start)
            lines = f.read(end - start).splitlines()
            if start:
                lines = lines[1:]          # a non-zero start cut the first line in half
            for raw in reversed(lines):
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    yield rec
            if start == 0 or window >= max_window:
                return
            window *= 4


def message_id_of(transcript_path, message_types, start_window, max_window):
    """The uuid of the last transcript record whose `type` is one of
    `message_types`; None on any problem."""
    try:
        for rec in tail_records(transcript_path, start_window, max_window):
            if rec.get("type") in message_types:
                uid = rec.get("uuid")
                if isinstance(uid, str) and uid:
                    return uid
    except Exception:
        pass
    return None


def turn_end_from_transcript(transcript_path, before, start_window, max_window):
    """The timestamp (local ISO, microseconds) of the latest assistant
    `end_turn` record written no later than `before` (ISO), or None."""
    try:
        from datetime import datetime
        limit = datetime.fromisoformat(before)
        for rec in tail_records(transcript_path, start_window, max_window):
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict) or msg.get("stop_reason") != "end_turn":
                continue
            try:
                ts = datetime.fromisoformat(str(rec.get("timestamp")).replace("Z", "+00:00"))
            except Exception:
                continue
            if ts.tzinfo is None or ts > limit:
                continue
            return ts.astimezone().isoformat(timespec="microseconds")
    except Exception:
        pass
    return None


# ── PostToolUseFailure ──────────────────────────────────────────────────────


def failure_result(data):
    """PostToolUseFailure's `error` as the post lane's (result text, response).
    The host writes `Exit code N` as the error's first line for a shell
    command; a content_rx anchored with `^` reads the first line, so the
    error body goes first and the exit line last. The response carries the
    exit code and `is_error`, so `bash_ok` never takes a failure for a
    receipt."""
    err = str(data.get("error") or "")
    m = _EXIT_LINE.match(err)
    body = err[m.end():] if m else err
    code = int(m.group(1)) if m else None
    text = body + (f"\nExit code {code}" if m else "")
    resp = {"stderr": body, "is_error": True}
    if code is not None:
        resp["exit_code"] = code
    return text, resp


def user_turns_of(tp, max_bytes, chars, keep):
    """What the person typed this session, oldest first. A transcript `user`
    record is also how tool results and injected context arrive, so this keeps
    only real prompts: no `toolUseResult`, no `tool_result` block, no `isMeta`
    row, no compaction summary. A Codex rollout's prompts are its `user`
    messages less injected context (`codex_prompt`). A substring pre-filter keeps it to one
    json.loads per candidate line. None when there is no transcript — and
    None never satisfies a `user` predicate."""
    try:
        size = os.path.getsize(tp)
        turns = []
        with open(tp, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()                       # the cut line
            for raw in f:
                if b'"user"' not in raw or b'"type"' not in raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if rec.get("type") == "response_item":     # a Codex rollout
                    text = codex_prompt(rec.get("payload"))
                    if text and text.strip():
                        turns.append(text.strip()[:chars])
                    continue
                if rec.get("type") != "user" or rec.get("isMeta") \
                        or rec.get("isCompactSummary") or "toolUseResult" in rec:
                    continue
                c = (rec.get("message") or {}).get("content")
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list):
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
                        continue
                    text = "\n".join((b.get("text") or "") for b in c
                                     if isinstance(b, dict) and b.get("type") == "text")
                else:
                    continue
                text = text.strip()
                if text:
                    turns.append(text[:chars])
        return turns[-keep:]
    except Exception:
        return None


# ── command-armed ordering rules ────────────────────────────────────────────
#
# `armed_by_events: ["command"]` + `armed_by_command_rx`: a Bash command that
# matches arms the obligation the way an edit does (the per-checkout count in
# `OrderingEngine`), `required_command_rx` clears it, `gated_command_rx` is
# where it is collected. "You ran `git mv`; run the tests before you push."
# The required command clears only on a passing run (exit 0): `rg`/`grep`
# exit 1 on a CLEAN search, so "grep for the old name" cannot be the required
# command — it would clear only when it finds leftovers. The hook passes its own shell helpers in (`executes`,
# `receipt_segments`, `unquoted`, `command_fires`), so the arming reads a
# command exactly as the receipt does: heredoc bodies dropped, quoted spans
# blanked, leading `FOO=1` assignments ignored, a `!`-negated segment never.

_SEP_RX = re.compile(r"&&|\|\||;|\n|\||(?<![>&])&(?![>&])")   # rulebook_shell._SEPARATOR_RX


def command_armed(spec):
    """Does this ordering block arm on a command?"""
    return isinstance(spec, dict) and "command" in tuple(
        spec.get("armed_by_events", ("edit", "write"))) and bool(spec.get("armed_by_command_rx"))


def _arm_text(part, rx, unquoted):
    m = re.search(rx, unquoted(part))
    return " ".join((m.group(0) if m else part).split())[:60]


def _pieces(seg):
    """Every segment of `seg`, split outside quotes on any separator, as
    (separator before it, segment) — "" before the first."""
    blank = _blank(seg)
    out, pos, sep = [], 0, ""
    for m in _SEP_RX.finditer(blank):
        out.append((sep, seg[pos:m.start()]))
        pos, sep = m.end(), m.group(0)
    out.append((sep, seg[pos:]))
    return [(j, p) for j, p in out if p.strip()]


def command_arms(seg, spec, *, executes, receipt_segments, unquoted, **_):
    """What a Bash call that did not fail arms: the text of the last segment
    that runs `armed_by_command_rx`, or "".

    ANY segment arms, whatever joins it: `git rm -r old || true` and
    `git mv a b; git status` succeed on the LAST segment's status, and the
    move most likely ran — reading only the segments the exit status vouches
    for skipped the obligation in exactly the case it exists for. A move that
    failed behind `|| true` arms too: that over-arms (one extra required run),
    the safe direction, and the same reading the pre lane gives `;`
    (`arms_before_gate`). A call that FAILED arms nothing (the caller's `ran`).

    A required command AFTER the arming segment leaves nothing owed only when
    the exit status vouches for it (`receipt_segments(whole_chain=True)`:
    `git mv a b && rg a`, or `git mv a b; rg a` with `rg` last)."""
    if not command_armed(spec):
        return ""
    rx, req = spec["armed_by_command_rx"], spec["required_command_rx"]
    parts = [p.strip() for _, p in _pieces(seg)]
    at = None
    for i, part in enumerate(parts):
        if executes(part, rx):
            at = i
    if at is None:
        return ""
    vouched = {p.strip() for p in receipt_segments(seg, whole_chain=True)}
    if any(p in vouched and executes(p, req) for p in parts[at + 1:]):
        return ""
    return _arm_text(parts[at], rx, unquoted)


def _runs_gated(sep, part, gated, unquoted, command_fires):
    """Does this one segment run the gated command? Read the way the whole-call
    gate (`is_gate`) reads it: from the segment's own start, leading space
    dropped, and with its separator before it — so either branch of a
    command-position pattern like `(?:^|[;&|(]\\s*)git\\s+push` matches. Quoted spans are blanked
    (`unquoted`): `echo "git push"` and `git commit -m 'git push later'`
    mention the gated command, they do not run it."""
    text = unquoted(part)
    return command_fires(gated, text.lstrip(), flags=0) or \
        bool(sep) and command_fires(gated, sep + text, flags=0)


def arms_before_gate(seg, spec, *, executes, unquoted, command_fires, **_):
    """In the PRE lane: does this one call run an arming command and then the
    gated one, with no required command between (`git mv a b && git push`)?
    The arming has not happened yet, so the count cannot see it; without this
    the push in the same call would go through. Every joiner counts — after a
    `;` the gated command runs whether or not the arming one passed, which
    over-gates (the safe direction). Returns the arming text, or ""."""
    if not command_armed(spec):
        return ""
    rx, req, gated = (spec["armed_by_command_rx"], spec["required_command_rx"],
                      spec["gated_command_rx"])
    pending = ""
    for sep, part in _pieces(seg):
        if pending and _runs_gated(sep, part, gated, unquoted, command_fires):
            return pending
        if executes(part, rx):
            pending = _arm_text(part, rx, unquoted)
        elif executes(part, req):
            pending = ""
    return ""


def _blank(text):
    from rulebook_shell import blank_quoted  # noqa: PLC0415 — beside this file
    return blank_quoted(text)


def command_gate_msg(slot, name, seg=None, spec=None, **kw):
    """The gate's words for a command-armed obligation.

    Without `seg`: the count already reached `min_edits` and the last arming
    was a command (`slot["last_cmd"]`). With it: the count has not, and this
    call would arm and then run the gated command (`arms_before_gate`) — ""
    when it does not, or when one more arming still falls short."""
    if seg is None:
        return (f"{slot['count']} change(s) since the last passing '{name}' "
                f"(last: ran `{slot['last_cmd']}`). Run it first.")
    now = arms_before_gate(seg, spec, **kw)
    if not now or slot["count"] + 1 < int(spec.get("min_edits", 1)):
        return ""
    return (f"this call runs `{now}` before the gated command, with no passing "
            f"'{name}' after it. Run it first.")
