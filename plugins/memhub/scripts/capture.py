#!/usr/bin/env python3
"""Unified session entry point across hosts — list and import any host's
sessions through one command, using the per-host readers.

    python3 capture.py list [--host all|claude|codex|cursor] [--limit N]

    python3 capture.py current [--host auto|claude|codex|cursor] [--cwd PATH] \
        [--max-age-s 1800] [--json]

    uv run --with 'mcp<2' python capture.py import --session <ref> \
        [--host auto|claude|codex|cursor] [--conversation-id <id>] [--title "..."] \
        [--agent-brain-id <id>] [--no-room] [--namespace <ns>] [--url <mcp-url>] \
        [--dry-run]

``--session`` accepts a transcript/rollout path, a bare session id, or
``latest``. ``--host auto`` (default) sniffs the host from a path shape or
accepts a bare id when exactly one installed host owns it. ``latest`` and ids
found under multiple hosts require an explicit ``--host`` (importing the wrong
session would fold-forward the wrong conversation's gist — refuse, never
guess).

Claude sessions are already canonical, so import execs ``import_session.py``
on the located path directly. Other hosts transform via their reader first,
then hand the canonical transcript to the SAME ``import_session.py`` — one
pipeline, per-host front doors. Conversation ids for non-Claude hosts are
namespaced ``<host>-<session-id>`` so server-side watermarks stay per-host.

``current`` prints the RUNNING session's conversation id — host-namespaced the
way the capture client sends it — for callers that need to name this session to
the server and have no other way to learn it (`/memhub:link-pr`). It matches on
the session's own working directory rather than trusting "newest .jsonl by
mtime", and it REFUSES rather than guesses: two live sessions in one worktree
is real, and picking the newer one would silently link the wrong one.

The mcp SDK pin (``uv run --with 'mcp<2'``) matches every other invocation
site: mcp 2.x renamed streamablehttp_client, breaking import_session.py's
transport. ``list`` and ``current`` are stdlib-only and run under bare
python3. Automatic capture deliberately keeps per-host flush entry points
because each host has different trigger and watermark semantics; this command
unifies listing and manual import, where the behavior is genuinely shared.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pr_link  # noqa: E402
import readers  # noqa: E402
from readers import claude as claude_reader  # noqa: E402

_IMPORT_SESSION = Path(__file__).resolve().parent / "import_session.py"


def cmd_list(args) -> int:
    hosts = list(readers.READERS) if args.host == "all" else [args.host]
    rows = []
    for h in hosts:
        r = readers.reader_for(h)
        if r is None:
            print(f"ERROR: unknown host {h!r} (known: {', '.join(readers.READERS)})",
                  file=sys.stderr)
            return 2
        rows.extend(r.list_sessions(limit=args.limit))
    rows.sort(key=lambda s: s["mtime"], reverse=True)
    if not rows:
        print("no sessions found")
        return 0
    for s in rows[:args.limit]:
        ts = datetime.datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")
        print(f"{s['host']:<7} {ts}  {s['id']}  {s.get('cwd') or ''}")
    return 0


# Two sessions whose transcripts were written this close together are two
# live sessions, not one live and one finished — there is no evidence here
# that separates them, so `current` refuses.
_TIE_WINDOW_S = 120
# How many recent sessions to ENUMERATE before filtering. The readers sort
# globally by mtime, so a small cap made the running session invisible whenever
# that host had this many newer transcripts in other worktrees — and `current`
# then answered "no live session" for a session that was right there. The
# freshness cut below is what keeps this cheap: it drops stale rows using the
# mtime already in the listing, so only the handful within `--max-age-s` are
# opened to read their cwd.
_CANDIDATE_SCAN = 500


def _real(path: str | None) -> str | None:
    if not isinstance(path, str) or not path:
        return None
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, ValueError):
        return None


def _candidates(hosts: list[str], target: str, max_age_s: float, now: float) -> list[dict]:
    """Sessions whose OWN cwd is ``target`` and whose transcript is fresh.

    The cwd match is the content signature. The freshness cut is the other
    half: the running session's transcript was written seconds ago, so
    anything stale is a different session that merely shares the directory.
    """
    rows: list[dict] = []
    for host in hosts:
        reader = readers.reader_for(host)
        if reader is None:
            continue
        try:
            listed = reader.list_sessions(limit=_CANDIDATE_SCAN)
        except Exception:  # noqa: BLE001 — one unreadable host must not blind the rest
            continue
        for row in listed:
            mtime = row.get("mtime") or 0
            # Cheap first: this comes from the listing, so a stale session
            # costs nothing. Only survivors get their transcript opened.
            if now - mtime > max_age_s:
                continue
            try:
                cwd = reader.session_cwd(row["path"])
            except Exception:  # noqa: BLE001
                cwd = None
            if _real(cwd) != target:
                continue
            rows.append({"session_id": row["id"], "host": host, "path": row["path"],
                         "cwd": cwd, "mtime": mtime})
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def _describe(row: dict, now: float) -> str:
    age = max(0, int(now - (row["mtime"] or 0)))
    return (f"{row['host']:<7} {pr_link.conversation_id_for(row['host'], row['session_id'])}"
            f"  {age}s ago  {row['cwd']}")


def cmd_current(args) -> int:
    now = time.time()
    target = _real(args.cwd or os.getcwd())
    if target is None:
        print(f"ERROR: cannot resolve {args.cwd or os.getcwd()!r}", file=sys.stderr)
        return 2
    hosts = list(readers.READERS) if args.host == "auto" else [args.host]
    rows = _candidates(hosts, target, args.max_age_s, now)

    if not rows:
        print(f"no live session found for {target}", file=sys.stderr)
        return 3

    tied = [r for r in rows if rows[0]["mtime"] - (r["mtime"] or 0) <= _TIE_WINDOW_S]
    # Candidates from more than one host are ambiguous however far apart their
    # clocks are: nothing here says which host the caller is running under.
    if len(tied) > 1 or len({r["host"] for r in rows}) > 1:
        ambiguous = rows if len({r["host"] for r in rows}) > 1 else tied
        print(f"ambiguous: {len(ambiguous)} live sessions in {target} — pass the one you mean",
              file=sys.stderr)
        for row in ambiguous:
            print("  " + _describe(row, now), file=sys.stderr)
        if args.json:
            print(json.dumps([{
                "conversation_id": pr_link.conversation_id_for(r["host"], r["session_id"]),
                "session_id": r["session_id"], "host": r["host"],
                "cwd": r["cwd"], "mtime": r["mtime"]} for r in ambiguous]))
        return 4

    row = rows[0]
    conv_id = pr_link.conversation_id_for(row["host"], row["session_id"])
    if args.json:
        print(json.dumps({"conversation_id": conv_id, "session_id": row["session_id"],
                          "host": row["host"], "cwd": row["cwd"], "mtime": row["mtime"]}))
    else:
        print(conv_id)
        print(row["host"])
    return 0


def _resolve(args) -> tuple[object | None, Path | None, str]:
    """(reader, path, err) for the requested session."""
    host = args.host
    if host == "auto":
        host = readers.sniff(args.session)
        if host is None:
            if args.session == "latest":
                return None, None, (
                    "'latest' is ambiguous across hosts; pass "
                    "--host claude|codex|cursor")
            matches: list[tuple[object, Path]] = []
            ambiguities: list[str] = []
            # Every reader matches the complete native session UUID, never a
            # prefix. Accept one exact installed-session match; any duplicate
            # within or across hosts remains an error below.
            for candidate in readers.READERS.values():
                candidate_path, candidate_err = candidate.locate(args.session)
                if candidate_path is not None:
                    matches.append((candidate, candidate_path))
                elif (isinstance(candidate_err, str) and
                      "ambiguous" in candidate_err.lower()):
                    ambiguities.append(f"{candidate.HOST}: {candidate_err}")
            if ambiguities:
                return None, None, (
                    f"session {args.session!r} is ambiguous: " +
                    "; ".join(ambiguities) + "; pass the session path")
            if len(matches) == 1:
                return matches[0][0], matches[0][1], ""
            if len(matches) > 1:
                found = ", ".join(candidate.HOST for candidate, _ in matches)
                return None, None, (
                    f"session {args.session!r} exists under multiple hosts "
                    f"({found}); pass --host claude|codex|cursor")
            return None, None, (
                f"cannot find session {args.session!r} under Claude, Codex, "
                "or Cursor; pass a session path or an explicit --host")
    r = readers.reader_for(host)
    if r is None:
        return None, None, f"unknown host {host!r} (known: {', '.join(readers.READERS)})"
    path, err = r.locate(args.session)
    if path is None:
        if not isinstance(err, str) or not err:
            err = f"cannot find session {args.session!r} for host {host!r}"
        return None, None, err
    return r, path, ""


def _restore_cursor_state(records: list, sid: str) -> None:
    """Best-effort fidelity restore for a Cursor import — never fatal.

    Cursor artifacts carry clocks/usage for only some records; the live
    flush observed the rest and pinned them in its session state. This
    import is the documented backstop for sessions whose per-event flush
    went dormant — re-apply those pins (read-only) so the backstop
    preserves the same per-turn fidelity the live path ships. It is an
    ENHANCEMENT of the import, not a precondition: if cursor_flush or one
    of its sibling modules cannot even import in this environment, the
    import proceeds with artifact-carried clocks rather than aborting.
    """
    try:
        import cursor_flush
        cursor_flush.apply_session_state(records, sid)
    except Exception as e:  # noqa: BLE001 — degrade, never kill the import
        print(f"warning: live-capture state restore failed ({e!r}); "
              "importing with artifact-carried clocks only", file=sys.stderr)


def _session_sid(meta: dict, path: Path) -> str:
    """The session uuid for state lookups and the conv-id suffix.

    ``meta["session_id"]`` when the reader set it (it always does today);
    otherwise derived from the path the way the readers themselves do it —
    a ``store.db``'s uuid is its session DIRECTORY name, every other native
    layout carries it in the file stem.
    """
    sid = meta.get("session_id")
    if isinstance(sid, str) and sid:
        return sid
    return path.parent.name if path.name == "store.db" else path.stem


def cmd_import(args) -> int:
    r, path, err = _resolve(args)
    if r is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    passthrough: list[str] = []
    if args.title:
        passthrough += ["--title", args.title]
    if args.agent_brain_id:
        passthrough += ["--agent-brain-id", args.agent_brain_id]
    if args.no_room:
        passthrough.append("--no-room")
    if args.namespace is not None:
        passthrough += ["--namespace", args.namespace]
    if args.url:
        passthrough += ["--url", args.url]

    def run_import(transcript: Path, conv_id: str | None) -> int:
        cmd = ["uv", "run", "--with", "mcp<2", "python", str(_IMPORT_SESSION),
               "--session", str(transcript),
               "--source-platform", r.HOST]
        if conv_id:
            cmd += ["--conversation-id", conv_id]
        return subprocess.run(cmd + passthrough).returncode

    if r.HOST == claude_reader.HOST:
        # Already canonical — import_session.py reads the transcript in place.
        if args.dry_run:
            records, meta = r.to_canonical(path)
            if not records:
                print(f"ERROR: nothing to import from {path}", file=sys.stderr)
                return 2
            sid = meta.get("session_id") or path.stem
            conv_id = args.conversation_id or sid
            print(f"source          : {path}")
            print(f"claude session  : {sid}")
            print(f"records         : {len(records)}")
            print(f"cwd             : {meta.get('cwd')}")
            print(f"conversation_id : {conv_id}")
            print("-" * 56)
            print("[dry-run] Claude transcript is already canonical; "
                  "skipping import_conversation")
            return 0
        return run_import(path, args.conversation_id)

    records, meta = r.to_canonical(path)
    if not records:
        print(f"ERROR: nothing to import from {path}", file=sys.stderr)
        return 2
    # The session id keys the conversation id AND (for Cursor) the flush-state
    # lookup below. Readers always set meta["session_id"], but the defensive
    # fallback must still be the real session uuid: a store.db's uuid is its
    # DIRECTORY name — the file stem is just "store", which would silently
    # no-op the state restore and mis-scope conv_id to "cursor-store".
    sid = _session_sid(meta, path)
    if r.HOST == "cursor":
        _restore_cursor_state(records, sid)
    problems = readers.validate_canonical(records)
    if problems:
        print(f"ERROR: transform produced non-canonical records: {problems[:3]}",
              file=sys.stderr)
        return 2

    # Keep conv_id == <host>-<session-uuid> however the session was addressed,
    # so incremental dedup holds across re-imports.
    conv_id = args.conversation_id or f"{r.HOST}-{sid}"
    if not args.title and meta.get("title"):
        passthrough += ["--title", meta["title"]]

    n_tool = sum(1 for rec in records
                 if isinstance(rec["message"].get("content"), list)
                 and rec["message"]["content"]
                 and rec["message"]["content"][0].get("type") == "tool_use")
    print(f"source          : {path}")
    print(f"{r.HOST} session   : {sid}   (model {meta.get('model')})")
    print(f"records         : {len(records)}  ({n_tool} tool calls)")
    print(f"cwd             : {meta.get('cwd')}")
    print(f"conversation_id : {conv_id}")
    print("-" * 56)

    body = "".join(json.dumps(rec) + "\n" for rec in records)

    if args.dry_run:
        # Deterministic path: overwritten on re-run (so dry-runs don't
        # accumulate) and left in place for inspection.
        transcript = Path(tempfile.gettempdir()) / f"memhub-{r.HOST}-dryrun-{sid}.jsonl"
        transcript.write_text(body, encoding="utf-8")
        print(f"[dry-run] wrote {len(records)} records -> {transcript}")
        print("[dry-run] skipping import_conversation (file left for inspection)")
        return 0

    # Real import: a throwaway temp dir, always cleaned up. Named
    # <host>-<sid>.jsonl so even a --conversation-id-less run gets a stable,
    # host-scoped id from the file stem.
    tmpdir = Path(tempfile.mkdtemp(prefix=f"memhub-{r.HOST}-"))
    transcript = tmpdir / f"{r.HOST}-{sid}.jsonl"
    transcript.write_text(body, encoding="utf-8")
    try:
        return run_import(transcript, conv_id)
    finally:
        try:
            transcript.unlink()
            tmpdir.rmdir()
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", help="list sessions across hosts")
    lp.add_argument("--host", default="all", choices=["all", *readers.READERS])
    lp.add_argument("--limit", type=int, default=20)
    lp.set_defaults(fn=cmd_list)

    cp = sub.add_parser("current", help="print the running session's conversation id")
    cp.add_argument("--host", default="auto", choices=["auto", *readers.READERS])
    cp.add_argument("--cwd", default=None,
                    help="the directory to match on; default is the current one")
    cp.add_argument("--max-age-s", type=float, default=1800.0,
                    help="a transcript untouched for longer is a different session")
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(fn=cmd_current)

    ip = sub.add_parser("import", help="import one session into MemHub")
    ip.add_argument("--session", required=True,
                    help="transcript/rollout path, bare session id, or 'latest'")
    ip.add_argument("--host", default="auto", choices=["auto", *readers.READERS])
    ip.add_argument("--conversation-id", default=None)
    ip.add_argument("--title", default=None)
    ip.add_argument("--agent-brain-id", default=None)
    ip.add_argument("--no-room", action="store_true",
                    help="ignore the repo's cached room and import into "
                         "workspace memory")
    ip.add_argument("--namespace", default=None,
                    help="repo scope for captured directives; default resolves "
                         "from the session's cwd via git remote, '' disables")
    ip.add_argument("--url", default=None)
    ip.add_argument("--dry-run", action="store_true")
    ip.set_defaults(fn=cmd_import)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
