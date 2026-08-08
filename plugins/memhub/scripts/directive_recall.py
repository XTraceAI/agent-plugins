"""PreToolUse hook — recall situated directives for the in-flight tool call.

Fires before Edit / Write / Bash. Reads the hook JSON (``tool_name`` +
``tool_input``), asks MemHub's ``recall_directives`` tool which lessons /
procedures fire on the concrete identifiers in that call (file paths, commands,
symbols), and injects any hits back as ``additionalContext`` so the agent sees
them BEFORE it acts. This is the serving half of procedural memory — fire on the
symbols you're touching mid-task, not on the opening prompt.

**Server funnel + fail-open.** The server runs the full v4 precision funnel
(symbol tripwire → contextual match semantics → LLM relevance gate, fail-open
past its 0.8s budget). The whole hook is best-effort: on a slow call, an auth
gap, or any error we emit nothing and exit 0 — a memory lookup must NEVER block
or break the agent's tool call. A tight internal timeout bounds the wait; the
hooks.json ``timeout`` is the hard backstop.

**Session already_fired.** A directive injects at most once per session: the
ids of directives actually INJECTED (not merely recalled — a gate-dropped
candidate keeps its chance at its real moment) persist in a per-session state
file and are (a) deduped client-side, which works against any server version,
and (b) sent to the server so its funnel can spend the budget on fresh
candidates. Repeats measured as 76% of all injection noise.

**Repo scope.** The repo name (git remote basename, else cwd basename) is sent
as ``repo``: the server scopes recall to directives learned there (legacy
unscoped rows still pass) and discounts the repo's own name as a trigger.

**Self-echo exclusion.** The Claude Code ``session_id`` is sent so the server
can skip directives minted from this session's own conversation. A 90-site
transcript audit (2026-08-02) found replayed same-session lessons — delivered
minutes after the agent already applied them — were the dominant ignored-
injection class; excluding them is a server-side filter, this client just
supplies the id (with a legacy retry for servers predating the param).

**First-touch-once + ranked cap.** The proactive path recalls at most once per
identifying handle (file path / command string) per session — repeat touches
re-bought the same answer at ~2s a call with zero measured conversions — and
injects at most ``_MAX_DIRECTIVES`` survivors, ranked by match specificity,
each rendered with the concrete trigger that fired (``fired on: …``) so the
agent can validate applicability in one glance.

**Reactive (PostToolUse) recall on failure.** The same script serves a second
hook: when a tool call FAILS, the error text itself is the richest firing
signal — a traceback names the module, a codegen error names the schema path,
an npm failure names the underlying binary an alias hid. On PostToolUse input
(detected by ``tool_response``) the script fires only when the output looks
like a failure, sends its tail as ``output`` so the server can extract
identifiers from it, and widens the client precision gate's haystack with the
same text (a lesson anchored on the CAUSE must survive even when the command
line only shows an alias). Measured motivator: a captured dangling-``$ref``
lesson anchored on ``openapi-typescript`` never fired at the failure site
because the command said ``npm run gen:types``.

**Client-side precision gate.** ``_precision_filter`` re-imposes the concrete
trigger-in-handle contract before injection — transitional belt-and-braces for
servers predating the match-semantics funnel; fail-open.

Invoked as: ``uv run --with 'mcp<2' python directive_recall.py`` with the PreToolUse
hook JSON on stdin.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _memhub_auth import resolve_bearer  # noqa: E402

# Bound on the recall round-trip. This runs synchronously before the tool, so a
# hung server can't be allowed to stall the agent; the server's own LLM-gate
# budget (0.8s, fail-open) fits inside with headroom. Fail-open on hit.
_RECALL_TIMEOUT_S = 2.5
# Transcript audit (2026-08-02, 90 firing sites): 5-item blocks get skimmed —
# the one costly miss was the right directive buried under leftovers. Fewer,
# ranked, each with its concrete match shown.
_MAX_DIRECTIVES = 2

# The firing signal for a tool call is its identifying handle — the file path
# for an edit/write, the command for Bash — NOT the file body or diff hunks.
_ID_ARG_KEYS = ("file_path", "notebook_path", "command")
_MAX_ARG_CHARS = 500

# Reactive path: the TAIL of a failing output carries the error (tracebacks and
# tool failures print last); cap what we ship. Fire only on a clear failure —
# a quiet PostToolUse must cost nothing.
_MAX_OUTPUT_CHARS = 1500
_ERROR_RE = re.compile(
    r"(?:Traceback \(most recent call last\)|\b[A-Z][a-zA-Z]*Error\b"
    r"|\bERROR\b|\bError\b|error:|✘|npm ERR!|FAILED\b|fatal:|Exception\b"
    r"|command not found|No such file or directory)"
)

# Session already_fired state: one small JSON list per session id, pruned by
# age so the directory can't grow unbounded across months of sessions.
_STATE_DIR = Path.home() / ".claude" / ".memhub" / "directive_fired"
_STATE_MAX_AGE_S = 7 * 24 * 3600
_MAX_FIRED_SENT = 1024


def _log(msg: str) -> None:
    print(f"[memhub-directive] {msg}", file=sys.stderr)


# --- session already_fired state -------------------------------------------

def _state_path(session_id: str) -> Path | None:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")
    return (_STATE_DIR / f"{sid}.json") if sid else None


def _load_fired(session_id: str) -> list[str]:
    """Ids injected earlier this session (empty on any problem — a lost state
    file only means a directive may fire once more, never a broken hook)."""
    path = _state_path(session_id)
    if not path:
        return []
    try:
        ids = json.loads(path.read_text(encoding="utf-8"))
        return [str(i) for i in ids if str(i).strip()] if isinstance(ids, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_fired(session_id: str, ids: list[str]) -> None:
    """Persist the injected-id list; opportunistically prune stale sessions."""
    path = _state_path(session_id)
    if not path:
        return
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ids[-_MAX_FIRED_SENT:]), encoding="utf-8")
        cutoff = time.time() - _STATE_MAX_AGE_S
        for old in _STATE_DIR.glob("*.json"):
            if old != path and old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    except OSError:
        pass  # state is an optimization, never worth failing the hook


# --- per-session first-touch handle cache ----------------------------------
# One recall per identifying handle per session (PreToolUse only): once we've
# asked the server about a given file path or command string, re-asking on
# every later touch re-buys the same answer at ~2s a call — the audit measured
# repeat-handle recalls as pure latency with zero conversions. The reactive
# (failure) path bypasses this cache: a repeated failing command is exactly
# when recall must fire again. A transient recall error does NOT record the
# handle, so the next touch retries.

def _handles_path(session_id: str) -> Path | None:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")
    return (_STATE_DIR / f"{sid}-handles.json") if sid else None


def _handle_key(tool: str, recall_args: dict, cwd: str = "") -> str:
    """The handle identity for the cache: the file path for an edit, the
    normalized command for Bash. Empty string disables caching for the call.

    Qualified by the absolute ``cwd``, not the repo name, for two reasons: it
    actually distinguishes checkouts (two worktrees of one repo share a remote
    basename but never a cwd, and a relative ``app/main.py`` exists in both),
    and reading it costs nothing — deriving a repo name here would spawn a git
    subprocess on every call, including the cache hits this exists to make free.
    """
    for k in _ID_ARG_KEYS:
        v = recall_args.get(k)
        if isinstance(v, str) and v:
            # Hash the FULL normalized handle: prefix-truncating collided two
            # long pipelines differing only past the cut, silently skipping
            # recall on the second. Readable head kept for eyeballing the
            # state file; the digest is what makes the key injective.
            norm = " ".join(v.split())
            digest = hashlib.sha256(norm.encode("utf-8", "replace")).hexdigest()[:16]
            return f"{cwd}:{tool}:{norm[:120]}:{digest}"
    return ""


def _load_handles(session_id: str) -> list[str]:
    path = _handles_path(session_id)
    if not path:
        return []
    try:
        keys = json.loads(path.read_text(encoding="utf-8"))
        return [str(k) for k in keys] if isinstance(keys, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_handles(session_id: str, keys: list[str]) -> None:
    path = _handles_path(session_id)
    if not path:
        return
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(keys[-_MAX_FIRED_SENT:]), encoding="utf-8")
    except OSError:
        pass  # the cache is an optimization, never worth failing the hook


def _repo_name(cwd: str) -> str:
    """The repo this session works in: git remote basename (stable across
    worktrees like ``xmem-directive-golden``), else the cwd basename."""
    if not cwd:
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=0.5,
        )
        url = out.stdout.strip()
        if out.returncode == 0 and url:
            return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name


def _error_output(hook_input: dict) -> str | None:
    """The failing output's tail, or ``None`` when this isn't a failure.

    PostToolUse's ``tool_response`` shape varies by tool (string, dict with
    stdout/stderr, structured error) — stringify defensively, keep the tail
    (errors print last), and gate on failure markers so a quiet success never
    costs a recall round-trip.
    """
    resp = hook_input.get("tool_response")
    if resp is None:
        return None
    if isinstance(resp, dict):
        parts = [v for k in ("stderr", "stdout", "output", "error", "text")
                 if isinstance(v := resp.get(k), str) and v]
        # Raw parts; the dumps fallback keeps non-ASCII (the ✘ failure marker)
        # matchable.
        text = "\n".join(parts) if parts else json.dumps(resp, ensure_ascii=False)
    else:
        text = str(resp)
    tail = text[-_MAX_OUTPUT_CHARS:].strip()
    if not tail or not _ERROR_RE.search(tail):
        return None
    return tail


def _recall_args(tool_input: dict) -> dict:
    """Trim tool_input to what the tripwire should fire on.

    Sending the whole ``tool_input`` ships large ``content`` / ``new_string``
    blobs to the server on every Edit/Write and lets symbols buried in the new
    content spuriously match directives. So prefer the identifying handles
    (``file_path`` / ``command``); for a tool we don't special-case, fall back to
    a size-capped copy so recall still has something concrete to fire on.
    """
    ids = {
        k: v for k in _ID_ARG_KEYS
        if isinstance(v := tool_input.get(k), str) and v
    }
    if ids:
        return ids
    return {
        k: (v[:_MAX_ARG_CHARS] if isinstance(v, str) else v)
        for k, v in tool_input.items()
    }


# --- client-side precision gate -------------------------------------------
# Always-on tokens that must never be a directive's sole anchor: English/keyword
# filler + (dynamically) the repo name from cwd. A trigger of just one of these
# would fire on ~every call, which is the exact noise this gate removes.
_GENERIC_TOKENS = frozenset({
    "true", "false", "none", "null", "self", "this", "that", "with",
    "from", "when", "into", "your", "code", "file", "path", "main",
    "test", "tests", "todo", "temp", "data",
    # Workspace-universal git words (audit: `origin/staging` word-tokens alone
    # drove 1,461 injections). The full path token "origin/staging" stays
    # matchable, so a directive anchored on it still fires on commands that
    # literally name it — just not on every `git push origin <branch>`.
    "origin", "staging", "master", "branch", "commit",
})
_MIN_TOKEN_LEN = 4


def _repo_tokens(cwd: str) -> set[str]:
    """Always-on tokens derived from the working dir (the repo name + parts).

    A trigger equal to the repo (e.g. ``MemHub-Backend`` → ``memhub`` /
    ``backend``) matches essentially every call, so it can't stand alone.
    """
    base = Path(cwd).name.lower() if cwd else ""
    if not base:
        return set()
    toks = {base}
    toks.update(w for w in re.split(r"[^a-z0-9]+", base) if len(w) >= _MIN_TOKEN_LEN)
    return toks


def _trigger_tokens(trigger: str) -> set[str]:
    """Concrete, matchable tokens for one trigger entity.

    The full string, plus (for a path) its basename and extension-less stem,
    plus long identifier words. Short fragments are dropped so ``app`` / ``py``
    can't drive a spurious match.
    """
    t = trigger.strip().lower()
    if not t:
        return set()
    toks = {t}
    if "/" in t or "." in t:
        base = t.rsplit("/", 1)[-1]
        toks.add(base)
        if "." in base:
            toks.add(base.rsplit(".", 1)[0])
    toks.update(w for w in re.split(r"[^a-z0-9_]+", t) if len(w) >= 5)
    return {w for w in toks if len(w) >= _MIN_TOKEN_LEN}


def _precision_filter(
    items: list[dict], args: dict, cwd: str, extra_haystack: str = "",
) -> list[dict]:
    """Keep only directives that concretely match the handle we fired on.

    An item survives when it declares no triggers (unverifiable → trusted) or
    when at least one of its non-generic trigger tokens is a substring of the
    call's identifying handle (command / file_path). On the reactive path the
    failing output joins the haystack (``extra_haystack``): a lesson anchored
    on the CAUSE named in the error must survive even when the command line
    only shows an alias. Fail-open: any error returns ``items`` unchanged, so
    the gate can never suppress the feature.

    Survivors are annotated in place with ``_match`` — the declared trigger
    whose longest token hit the haystack — which drives both the ranked
    truncation to ``_MAX_DIRECTIVES`` and the rendered "fired on" line, so the
    agent can validate applicability in one glance.
    """
    try:
        haystack = " ".join(
            v.lower() for v in list(args.values()) + [extra_haystack]
            if isinstance(v, str) and v
        )
        if not haystack:
            return items
        blocked = _GENERIC_TOKENS | _repo_tokens(cwd)
        kept: list[dict] = []
        for d in items:
            triggers = d.get("triggers")
            if not isinstance(triggers, list) or not triggers:
                d.pop("_match", None)
                d.pop("_match_len", None)  # both, or _rank sorts on a stale length
                kept.append(d)  # no declared triggers → can't verify → keep
                continue
            best_tok, best_trg = "", ""
            for t in triggers:
                if not isinstance(t, str):
                    continue
                for tok in _trigger_tokens(t):
                    if tok not in blocked and tok in haystack and len(tok) > len(best_tok):
                        best_tok, best_trg = tok, t
            if best_tok:
                d["_match"] = best_trg
                d["_match_len"] = len(best_tok)
                kept.append(d)
        return kept
    except Exception:  # noqa: BLE001 — the gate must never break the hook
        return items


def _rank(items: list[dict]) -> list[dict]:
    """Order survivors so the ranked cap keeps the most defensible ones:
    concretely-verified matches first, more specific (longer) matched tokens
    before generic ones, then more-often-confirmed directives. Stable, so the
    server's own ordering breaks remaining ties."""
    try:
        return sorted(
            items,
            key=lambda d: (
                -int("_match" in d),
                -int(d.get("_match_len") or 0),
                -(d.get("seen") if isinstance(d.get("seen"), int) else 0),
            ),
        )
    except Exception:  # noqa: BLE001 — ranking must never break the hook
        return items


def _render(items: list[dict]) -> str:
    """Plain-English context block from the recalled directives."""
    lines = ["## 📋 Relevant team directives for this action",
             "(situated lessons/procedures that fired on what you're touching — "
             "act on them)"]
    for d in items:
        kind = str(d.get("type", "directive")).upper()
        text = str(d.get("content", "")).strip()
        # "Why fired": the one trigger that concretely hit this call beats a
        # trigger list the agent would have to cross-check itself.
        matched = str(d.get("_match") or "").strip()
        anchor = (f"fired on: {matched}" if matched
                  else ", ".join(str(t) for t in (d.get("triggers") or [])[:4]))
        if anchor and not matched:
            anchor = f"triggers: {anchor}"
        # Provenance the agent can weight instead of re-verifying: when the
        # directive was last confirmed and how often it has been observed.
        prov = []
        if d.get("as_of"):
            prov.append(f"as of {d['as_of']}")
        if isinstance(d.get("seen"), int) and d["seen"] > 1:
            prov.append(f"seen {d['seen']}×")
        suffix = ""
        if anchor or prov:
            suffix = "  _(" + "; ".join(p for p in (anchor, *prov) if p) + ")_"
        lines.append(f"- **[{kind}]** {text}{suffix}")
    return "\n".join(lines)


def _parse_recall_result(res) -> list[dict] | None:
    """Directives from a tool result, or ``None`` when there was NO answer.

    Failure is narrow on purpose: an ``isError`` result or a payload we cannot
    parse into a dict at all. A well-formed dict IS an answer even when
    ``items`` is absent or not a list — a server may spell "nothing matched" as
    ``null`` / ``{}``, and calling that a failure would stop the handle cache
    from ever recording it, turning every later touch into a fresh ~2s recall.
    """
    if getattr(res, "isError", False):
        texts = [t for t in (getattr(b, "text", None)
                 for b in getattr(res, "content", []) or []) if t]
        _log(f"recall FAILED: {(texts[0] if texts else 'no detail')[:160]}")
        return None  # failure ≠ empty: don't let it poison the cache
    out = getattr(res, "structuredContent", None)
    if isinstance(out, dict) and isinstance(out.get("result"), dict) \
            and "items" not in out:
        out = out["result"]  # FastMCP sometimes wraps in {"result": …}
    if not isinstance(out, dict):
        for b in getattr(res, "content", []) or []:
            text = getattr(b, "text", None)
            if text:
                try:
                    out = json.loads(text)
                    break
                except json.JSONDecodeError:
                    continue
    if not isinstance(out, dict):
        _log("recall returned an unparseable payload")
        return None
    items = out.get("items")
    return items if isinstance(items, list) else []


async def _recall(
    tool: str, args: dict, repo: str, fired: list[str], output: str | None = None,
    session_id: str = "",
) -> list[dict] | None:
    """Recalled directives, or ``None`` when the recall itself FAILED.

    The distinction is load-bearing for the first-touch handle cache: a genuine
    empty result (``[]``) means "asked, nothing matched" and is worth caching,
    while a server error or an unparseable response means we never got an
    answer — caching that would suppress this handle for the rest of the
    session on a transient blip.
    """
    import mcp_http

    # NO REFRESH on this path, and no thread either — both are deliberate.
    #
    # This hook is SYNCHRONOUS with an 8s budget and fires before every file
    # edit, so it must not attempt a token refresh: that is two blocking urllib
    # calls, ~25s of socket timeout. Offloading to a thread does not help, and
    # believing it did was a mistake corrected here — measured, a
    # `wait_for(to_thread(...), 2.5)` around an 8s blocking call returns after
    # 8.01s, because cancelling the future does not stop the thread and
    # `asyncio.run` then joins the executor on the way out. A blocking call
    # simply cannot be time-bounded from the outside.
    #
    # So it reads the cached credential and nothing else. Stale token, no
    # recall this once — the async capture hooks have the budget to renew it,
    # and a missed context lookup costs infinitely less than a stalled edit.
    url, bearer = resolve_bearer(refresh=False)
    if not bearer:
        # No credential is a failed recall, not an empty one — the distinction
        # the docstring above turns on. Returning [] would cache "nothing
        # matched" against this handle for the rest of the session.
        return None
    # Stateless server, no handshake — one round trip. This hook is
    # SYNCHRONOUS and ungated on Edit/Write, so it ran before every single
    # file edit; the SDK's startup was pure latency in that path.
    session = mcp_http.Session(url, bearer, timeout=_RECALL_TIMEOUT_S)
    arguments: dict = {"tool": tool, "args": args}
    if output:
        # Reactive path: the server extracts identifiers from the
        # failing output too (`output` predates repo/already_fired,
        # so it needs no legacy-retry handling).
        arguments["output"] = output
    if repo:
        arguments["repo"] = repo
    if fired:
        arguments["already_fired"] = fired[-_MAX_FIRED_SENT:]
    if session_id:
        # Self-echo exclusion: the server skips directives minted from
        # this session's own conversation — the audit's dominant noise
        # class (lessons replayed minutes after the agent applied them).
        arguments["session_id"] = session_id
    # A transport failure is a FAILED recall, never an empty one. The
    # difference is the whole contract of this function: `[]` means "asked,
    # nothing matched" and gets cached against this handle for the session,
    # so letting a 429 or a dropped connection reach that path would suppress
    # recall on this handle for the rest of the session over a blip.
    #
    # It also fails OPEN. A recall that cannot happen must not block the edit
    # the user is making — the hook contributes context, it does not gate work.
    try:
        res = await session.call_tool("recall_directives", arguments=arguments)
    except mcp_http.McpError as e:
        _log(f"recall unavailable ({e}) — proceeding without directives")
        return None
    if getattr(res, "isError", False) and (repo or fired or session_id):
        # Rolling-upgrade compat: a server predating the repo /
        # already_fired / session_id params rejects unknown arguments.
        # Retry once legacy-shaped — client-side dedup still covers
        # repeats.
        texts = [t for t in (getattr(b, "text", None)
                 for b in getattr(res, "content", []) or []) if t]
        detail = (texts[0] if texts else "")[:200]
        if re.search(r"unexpected|repo|already_fired|session_id|validation",
                     detail, re.I):
            _log("server predates repo/already_fired/session_id; "
                 "retrying legacy")
            # Keep `output`: it predates the params being rejected and
            # is the reactive path's whole firing signal — dropping it
            # would silently reduce a failure recall to command-line
            # matching, the exact miss the reactive hook exists to fix.
            legacy: dict = {"tool": tool, "args": args}
            if output:
                legacy["output"] = output
            try:
                res = await session.call_tool("recall_directives",
                                              arguments=legacy)
            except mcp_http.McpError as e:
                _log(f"legacy retry unavailable ({e})")
                return None
    return _parse_recall_result(res)


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        tool = hook_input.get("tool_name") or ""
        args = hook_input.get("tool_input") or {}
        if not tool or not isinstance(args, dict):
            return 0
        # PostToolUse input carries the tool's result: this is the reactive
        # path — recall on FAILURES, where the error text names identifiers
        # (the true cause) that the command line never showed. On a success
        # (or a PreToolUse call) `output` stays None and nothing changes.
        reactive = "tool_response" in hook_input
        output = _error_output(hook_input) if reactive else None
        if reactive and not output:
            return 0  # successful tool call — a quiet PostToolUse costs nothing
        cwd = hook_input.get("cwd") or ""
        session_id = str(hook_input.get("session_id") or "")
        fired = _load_fired(session_id)
        recall_args = _recall_args(args)
        # First-touch-once: skip the round-trip when this session already
        # recalled on this exact handle (proactive path only — a failure must
        # always be allowed to re-fire reactively).
        handle = "" if reactive else _handle_key(tool, recall_args, cwd)
        handles = _load_handles(session_id) if handle else []
        if handle and handle in handles:
            return 0
        # Only now derive the repo — it spawns a git subprocess, so it must sit
        # AFTER the cache early-out or every cached hit pays for it.
        items = asyncio.run(
            asyncio.wait_for(
                _recall(tool, recall_args, _repo_name(cwd), fired, output,
                        session_id),
                _RECALL_TIMEOUT_S,
            )
        )
        if items is None:
            return 0  # recall FAILED — never cached, so a blip can't suppress
        # Belt-and-braces dedup for servers predating already_fired — repeats
        # were 76% of all injection noise, so this must not depend on the
        # server version.
        fired_set = set(fired)
        items = [d for d in items if str(d.get("id") or "") not in fired_set]
        # Re-impose the symbol-tripwire contract for servers predating the
        # match-semantics funnel: only triggers that concretely hit this call —
        # where "this call" includes the failing output on the reactive path.
        # The emit is self-contained and CANNOT raise past this block: a render
        # or stdout failure must not decide whether the handle gets cached.
        # Placement alone can't satisfy both sides — cache before the emit and a
        # transient crash costs an injection; cache after and a DETERMINISTIC
        # one (broken stdout, an unstringifiable payload, _save_fired raising a
        # non-OSError) re-buys a ~2.5s recall on every later touch of that
        # handle. Containing the failure removes the dilemma: whatever happens
        # in here, we asked the server once and we record that.
        try:
            items = _precision_filter(items, recall_args, cwd, output or "")
            items = _rank(items)[:_MAX_DIRECTIVES]
            if items:
                _log(f"{len(items)} directive(s) fired for {tool}"
                     + (" (reactive, on failure output)" if output else ""))
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse" if reactive else "PreToolUse",
                        "additionalContext": _render(items),
                    }
                }))
                # Record INJECTIONS only, and only after a successful emit — a
                # recalled-but-not-shown directive keeps its chance at its real
                # moment later in the session.
                new_ids = [str(d["id"]) for d in items
                           if str(d.get("id") or "").strip()]
                if new_ids and session_id:
                    _save_fired(session_id, fired + new_ids)
        except Exception as e:  # noqa: BLE001 — the emit is best-effort like the rest
            _log(f"emit skipped ({type(e).__name__}: {str(e)[:120]})")
        # We got an ANSWER, so the handle is recorded — including an empty answer
        # and one whose candidates were all dropped. Re-asking re-buys the same
        # answer (drops are deterministic, `already_fired` only grows), so the
        # deliberate trade-off is that a handle answered once isn't re-asked this
        # session. Only a FAILED recall (`items is None`, returned above) stays
        # uncached, so a transport blip can still retry.
        if handle and session_id:
            _save_handles(session_id, handles + [handle])
    # BaseException (not Exception): anyio task groups can surface a
    # BaseExceptionGroup (e.g. auth cancelling siblings). This hook is
    # best-effort — never fail or block the tool call. Emit nothing, exit 0.
    except BaseException as e:  # noqa: BLE001 — never fail the hook
        _log(f"skipped ({type(e).__name__}: {str(e)[:120]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
