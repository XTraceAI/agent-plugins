#!/usr/bin/env python3
"""Import a specific coding-agent session into MemHub — a terminal operation.

The transcript is read off disk and shipped straight to the
`import_conversation` MCP tool: the model never re-emits the content, so a
session of ANY size works.

Mirrors the SessionEnd hook's contract exactly:
- raw transcript records passed AS-IS (the tool auto-detects the Claude Code
  shape and runs agentic, tool-aware extraction)
- `conversation_id` = the session id (file stem) by default, so re-imports of
  the same session are INCREMENTAL: the server-side watermark admits only
  records it hasn't seen, and the session gist folds forward instead of
  duplicating.

Auth = the SAME OAuth the /mcp connector uses (shared `_memhub_auth`):
$MEMHUB_TOKEN if set (CI escape hatch), else the cached plugin OAuth token,
else a one-time browser approval. No memhub-cli required.

Usage (mcp SDK pulled ephemerally by uv):
    uv run --with 'mcp<2' python import_session.py --session <session-id-or-path>
        [--conversation-id <id>] [--source-platform claude|codex|cursor]
        [--title "..."] [--url <mcp-url>]

`--session` accepts either a path to a .jsonl transcript or a bare session id,
which is resolved by searching ~/.claude/projects/*/<id>.jsonl.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _memhub_auth import resolve_url_and_auth  # noqa: E402
from room_map import env_for_url, git_env, git_readonly, read_room  # noqa: E402
from session_title import (  # noqa: E402
    custom_title,
    generated_title,
    prompt_title,
)
from transcript_chunks import (  # noqa: E402
    DEFAULT_CHUNK_BYTES,
    slices as make_slices,
)
from redact import redact_records  # noqa: E402
from transcript_filter import drop_command_wrappers  # noqa: E402


SOURCE_PLATFORMS = ("claude", "codex", "cursor")


def import_call_args(messages: list[dict], conversation_id: str,
                     source_platform: str) -> dict:
    """Build the provenance-bearing core of an import request."""
    if source_platform not in SOURCE_PLATFORMS:
        raise ValueError(f"unsupported source platform: {source_platform}")
    return {
        "messages": messages,
        "conversation_id": conversation_id,
        "source_platform": source_platform,
    }


def load_transcript(path: Path) -> tuple[list[dict], int]:
    """Parse a JSONL transcript tolerantly.

    Returns ``(records, malformed_count)`` — malformed lines are skipped, not
    fatal, because real transcripts occasionally carry a truncated final line
    (interrupted write). The caller decides what to do when nothing parses.

    ``encoding="utf-8"`` is NOT optional: transcripts are UTF-8 whatever the OS
    locale is, but a bare ``read_text()`` decodes with the LOCALE codec — on a
    non-UTF-8 default (cp950, cp1252, …) an ordinary em-dash in the transcript
    raises UnicodeDecodeError and every import on that machine dies. And since
    Claude Code is the primary caller, the user has no workaround: the
    permission classifier refuses both `PYTHONUTF8=1 uv run …` and `-X utf8`.
    ``errors="replace"`` extends the tolerant contract above to the decode
    step: one bad byte must not lose the whole session, only the char it hit.
    """
    records: list[dict] = []
    malformed = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            malformed += 1
    return records, malformed


def resolve_session_file(session: str) -> tuple[Path | None, str]:
    """Accept a path, or a bare session id searched under ~/.claude/projects.

    Returns ``(file, error_reason)`` — exactly one is set. A PATH-shaped
    argument (contains a separator) that doesn't exist is its own error;
    it must NOT fall through to the id glob, which would blame the
    projects-dir lookup for a plain file typo.

    Top-level session transcripts only — subagent/workflow .jsonl files live
    in subdirectories and are not sessions. If the same session id exists
    under several project dirs (relocated checkouts), prefer the largest
    file (the most complete transcript).
    """
    p = Path(session).expanduser()
    if p.is_file():
        return p, ""
    if "/" in session:
        return None, f"transcript file not found: {p}"
    sid = session.removesuffix(".jsonl")
    candidates = sorted(
        Path.home().glob(f".claude/projects/*/{sid}.jsonl"),
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    if not candidates:
        return None, (f"no session {sid!r} found under ~/.claude/projects/*/ "
                      "(pass a transcript path instead?)")
    return candidates[0], ""


async def _gist_hash(
    session, agent_brain_id: str | None, org_id: str | None = None,
) -> str | None:
    """Content hash of the session gist (episode starting '## GOAL'), or None.

    ``org_id`` must match the one the import itself uses. A brain is resolved
    inside ONE org, so searching without it falls back to the caller's default
    org and finds nothing for a brain that lives elsewhere — which does not
    read as an error, it reads as "the gist has not appeared yet", and every
    inter-slice wait then burns its full timeout.
    """
    import hashlib
    args = {"query": "GOAL INTENT OUTCOME ROUTE RESUME STATE next step",
            "memory_type": "episodes", "top_k": 5}
    if agent_brain_id:
        args["agent_brain_id"] = agent_brain_id
    if org_id:
        args["org_id"] = org_id
    try:
        res = await session.call_tool("search_memory", arguments=args)
        d = unwrap(res)
        for it in d.get("items", []):
            c = str(it.get("content", "")).lstrip()
            if c.startswith("## GOAL"):
                return hashlib.sha256(c.encode()).hexdigest()
    except Exception as exc:  # noqa: BLE001
        # Still swallowed — a gist read must never fail an import that already
        # succeeded — but not silent: "search failed" and "gist has not
        # appeared yet" produce the same None here, and the caller reacts to
        # None by waiting the full slice timeout. Without this line, a
        # persistent error is indistinguishable from slow extraction for 30
        # minutes per slice boundary. Printed once per call, and only on the
        # error path.
        if not getattr(_gist_hash, "_warned", False):
            _gist_hash._warned = True
            print(f"  NOTE: gist lookup failed ({type(exc).__name__}: "
                  f"{str(exc)[:120]}); slice waits will run to timeout")
    return None


async def _wait_gist_change(
    session, agent_brain_id, prev_hash, timeout=1800, org_id=None,
):
    """Block until the gist appears (prev None) or its content changes
    (fold-forward happened) — the end-of-slice extraction signal. On timeout,
    warn and proceed (the next slice still imports safely; worst case the
    gist upserts race and one fold is lost to last-writer-wins).

    ``org_id`` rides through to the search for the reason in :func:`_gist_hash`:
    without it a cross-org import never observes its own gist, so this waits
    the full ``timeout`` at EVERY slice boundary — 30 minutes each by default,
    turning a chunked import into hours of doing nothing.
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        await asyncio.sleep(20)
        h = await _gist_hash(session, agent_brain_id, org_id)
        if h is not None and h != prev_hash:
            print("  slice extraction complete (gist updated)")
            return h
    print("  WARNING: slice wait timed out; continuing with next slice")
    return prev_hash


def unwrap(result) -> dict:
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    for b in getattr(result, "content", []) or []:
        t = getattr(b, "text", None)
        if t:
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return {"_raw": t}
    return {"_raw": str(result)}


def call_error(result, payload: dict) -> str | None:
    """The server-side failure text of a tool call, or None on success.

    Tool exceptions arrive as ``CallToolResult.isError`` with the message in
    the content blocks — ``unwrap`` can't distinguish that from a successful
    payload, so callers must check this BEFORE trusting the dict.
    """
    if getattr(result, "isError", False):
        return str(payload.get("_raw") or payload.get("error")
                   or json.dumps(payload))
    return None


def _cwd_from_records(records: list[dict]) -> str | None:
    return next((r.get("cwd") for r in records
                 if isinstance(r, dict) and isinstance(r.get("cwd"), str)
                 and r.get("cwd")), None)


def _cwd_ok(cwd: str | None) -> bool:
    """Whether a transcript-provided cwd is safe to pass as ``git -C``."""
    if not isinstance(cwd, str) or not cwd or cwd.startswith("-"):
        return False
    try:
        return Path(cwd).is_absolute() and Path(cwd).is_dir()
    except (OSError, ValueError):
        return False


def _namespace_from_records(records: list[dict]) -> str | None:
    """The session's working context: git remote basename resolved from the
    transcript's ``cwd`` (client-side — the server never derives this, since a
    worktree dir basename would stamp a scope that HIDES directives from the
    canonical repo's scoped recalls). None when it can't be resolved
    confidently — unscoped stores serve everywhere, a wrong scope doesn't."""
    cwd = _cwd_from_records(records)
    if not _cwd_ok(cwd):
        return None
    try:
        out = subprocess.run(
            git_readonly(cwd) + ["remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=2, env=git_env(),
        )
        url = out.stdout.strip()
        if out.returncode == 0 and url:
            return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description="Import a coding-agent session into MemHub.")
    ap.add_argument("--session", required=True,
                    help="path to a .jsonl transcript, or a bare session id")
    ap.add_argument("--conversation-id", default=None,
                    help="override the conversation id. Default (and what you "
                         "almost always want): the session id, which is what "
                         "per-turn capture uses — so the session stays ONE "
                         "conversation per room and re-imports are incremental. "
                         "An id that is not the session's SPLITS that session "
                         "across two conversations; only pass one for a "
                         "transcript that has no session id of its own (e.g. a "
                         "synthesized Codex rollout)")
    ap.add_argument("--source-platform", choices=SOURCE_PLATFORMS,
                    default="claude",
                    help="originating host recorded on the imported session")
    ap.add_argument("--title", default=None)
    ap.add_argument("--agent-brain-id", default=None,
                    help="route the extracted facts/episodes into an agent brain "
                         "(isolated, shareable) instead of raw workspace memory. "
                         "Default: the repo's cached room "
                         "(~/.config/memhub-plugin/rooms.json), if any")
    ap.add_argument("--no-room", action="store_true",
                    help="ignore the repo's cached room and import into personal "
                         "memory")
    ap.add_argument("--namespace", default=None,
                    help="Working-context name for captured directives (the "
                         "repo). Default: resolved from the transcript's cwd "
                         "via the git remote basename; pass '' to disable.")
    ap.add_argument("--org-id", default=None,
                    help="Organization to import into, for accounts in more "
                         "than one. Default: the connection's default org — "
                         "which is why an --agent-brain-id created in ANOTHER "
                         "org fails with 'Agent brain not found'.")
    ap.add_argument("--url", default=None)
    ap.add_argument("--chunk-bytes", type=int, default=DEFAULT_CHUNK_BYTES,
                    help="transcripts larger than this are sent as sequential "
                         "disjoint slices under the same conversation_id "
                         "(server extracts each incrementally; the session "
                         "gist folds forward per slice). 0 disables chunking.")
    ap.add_argument("--slice-timeout", type=int, default=1800,
                    help="max seconds to wait for a slice's extraction "
                         "(detected via the session gist appearing/changing) "
                         "before sending the next slice anyway")
    args = ap.parse_args()

    f, err = resolve_session_file(args.session)
    if f is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    records, malformed = load_transcript(f)
    if malformed:
        # Transcripts can carry a truncated final line (interrupted write) or
        # stray non-JSON noise; one bad line must not abort a 2,000-record
        # import. Skip-and-report, fail only if NOTHING is parseable.
        print(f"WARNING: skipped {malformed} malformed JSONL line(s) in {f}",
              file=sys.stderr)
    if not records:
        print(f"ERROR: {f} contains no valid JSONL records", file=sys.stderr)
        return 2

    conv_id = args.conversation_id or f.stem
    # An override that is not the session's own id opens a SECOND conversation
    # for this session — per-turn capture keys on the session id, so its records
    # and this import's watermark diverge and the session's memory splits across
    # two rows. Legitimate only for a transcript with no session id of its own
    # (a synthesized Codex rollout). Warn rather than refuse: the caller may
    # genuinely be in that case, and this script must stay scriptable.
    if args.conversation_id and args.conversation_id != f.stem:
        print(f"WARNING: --conversation-id {args.conversation_id!r} is not this "
              f"session's id ({f.stem}). Per-turn capture writes under the "
              "session id, so this import opens a SECOND conversation and the "
              "session's memory is split across both. Drop the flag unless this "
              "transcript has no session id of its own.", file=sys.stderr)
    # --namespace wins; '' explicitly disables; default = resolve from records.
    # Resolved from the FULL list, ahead of the filter below: ``cwd`` rides on
    # every user record, including the slash-command ones.
    namespace = (args.namespace if args.namespace is not None
                 else _namespace_from_records(records)) or None

    # Slash-command bookkeeping is transcript plumbing, not conversation. The
    # per-turn path applies the same filter, so a session cannot come out clean
    # or dirty depending on which path happened to capture it.
    kept = drop_command_wrappers(records)
    dropped = len(records) - len(kept)
    records = kept
    if not records:
        print(f"ERROR: {f} holds only slash-command records", file=sys.stderr)
        return 2

    # Third and last upload path, redacting for the same reason it filters: the
    # guarantee is that a captured session never carries a MemHub key, and a
    # guarantee that holds on two paths out of three is not one.
    records = redact_records(records)

    # An explicit --title always wins; otherwise take the name the transcript
    # itself carries, the same way per-turn capture does. Without this a plain
    # `--session X` import lands unnamed even when the client wrote a perfectly
    # good title into the file — and a headless session, which writes no title
    # record at all, is named by what it was asked to do.
    title = args.title or custom_title(records) or generated_title(records) \
        or prompt_title(records) or None

    url, headers, auth = resolve_url_and_auth(args.url)

    # An explicit --agent-brain-id always wins; otherwise fall back to the repo's
    # cached room so a plain `--session X` import lands in team memory instead of
    # personal memory. Keyed by the resolved endpoint's backend, and derived from
    # the TRANSCRIPT's cwd rather than the caller's — the script is often run
    # from a different directory than the session it is importing.
    room = None
    if not args.agent_brain_id and not args.no_room:
        # Only when the transcript says where it ran. read_room(None) would fall
        # back to THIS process's cwd — and this script is routinely run from a
        # different repo than the session it imports, so that would file the
        # session under an unrelated room. Unknown origin → stays personal.
        rec_cwd = _cwd_from_records(records)
        room = read_room(rec_cwd, env_for_url(url)) if rec_cwd else None
        if room:
            args.agent_brain_id = room["brain_id"]

    slices = make_slices(records, args.chunk_bytes) if args.chunk_bytes else [records]
    size = f.stat().st_size
    print(f"session file    : {f}")
    filtered = f"   (+{dropped} slash-command dropped)" if dropped else ""
    print(f"records         : {len(records)}   ({size:,} bytes ≈ {size // 4:,} tokens)"
          f"{filtered}")
    print(f"conversation_id : {conv_id}")
    print(f"source platform : {args.source_platform}")
    if title:
        src = "explicit" if args.title else "from transcript"
        print(f'title           : "{title}"   ({src})')
    print(f"endpoint        : {url}")
    if args.agent_brain_id:
        src = f' (repo room "{room.get("name", "?")}")' if room else ""
        print(f"agent brain     : {args.agent_brain_id}{src}")
    if namespace:
        print(f"namespace       : {namespace}")
    print("-" * 56)

    if len(slices) > 1:
        print(f"chunked import : {len(slices)} slices "
              f"(payload exceeds {args.chunk_bytes:,} bytes; slices are "
              "disjoint and sent sequentially — the gist folds forward "
              "after each)")

    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            prev_gist_hash = await _gist_hash(
                s, args.agent_brain_id, args.org_id)
            for i, sl in enumerate(slices, 1):
                call_args = import_call_args(
                    sl, conv_id, args.source_platform)
                if args.org_id:
                    # Brains are looked up inside ONE org. Without this, an
                    # --agent-brain-id belonging to a non-default org fails
                    # with "Agent brain not found" — which reads like a stale
                    # or deleted id rather than a wrong-org lookup.
                    call_args["org_id"] = args.org_id
                if title:
                    call_args["title"] = title
                if args.agent_brain_id:
                    call_args["agent_brain_id"] = args.agent_brain_id
                if namespace:
                    # Older servers ignore unknown arguments; newer ones
                    # stamp the directive scope from it. Safe either way.
                    call_args["namespace"] = namespace
                if len(slices) > 1:
                    print(f"--- slice {i}/{len(slices)}: {len(sl)} records ---")
                res = await s.call_tool("import_conversation", arguments=call_args)
                payload = unwrap(res)
                print(json.dumps(payload, indent=2))
                err = call_error(res, payload)
                if err:
                    # No success epilogue — a headless caller must see this
                    # as a failed save, not "Queued".
                    label = (f"slice {i}/{len(slices)}" if len(slices) > 1
                             else "import")
                    print(f"ERROR: {label} failed: {err}", file=sys.stderr)
                    if i > 1:
                        print(f"NOTE: slices 1..{i - 1} were already queued; "
                              "re-running after fixing the error is safe "
                              "(the server watermark skips them).",
                              file=sys.stderr)
                    return 1
                if i < len(slices):
                    print(f"waiting for slice {i} extraction "
                          "(gist appear/fold-forward) before next slice ...")
                    prev_gist_hash = await _wait_gist_change(
                        s, args.agent_brain_id, prev_gist_hash,
                        timeout=args.slice_timeout, org_id=args.org_id,
                    )
    print("-" * 56)
    print("Queued. Extraction runs in the background (minutes for large "
          "sessions); facts/episodes/artifacts + the session gist appear in "
          "search_memory as it completes. Re-running the same session later "
          "imports only NEW records (watermark) and folds the gist forward.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
