#!/usr/bin/env python3
"""Unified session entry point across hosts — list and import any host's
sessions through one command, using the per-host readers.

    python3 capture.py list [--host all|claude|codex] [--limit N]

    uv run --with 'mcp<2' python capture.py import --session <ref> \
        [--host auto|claude|codex] [--conversation-id <id>] [--title "..."] \
        [--agent-brain-id <id>] [--namespace <ns>] [--url <mcp-url>] [--dry-run]

``--session`` accepts a transcript/rollout path, a bare session id, or
``latest``. ``--host auto`` (default) sniffs the host from a path shape; a
bare id or ``latest`` is ambiguous across hosts and requires an explicit
``--host`` (importing the wrong host's "latest" would fold-forward the wrong
conversation's gist — refuse, never guess).

Claude sessions are already canonical, so import execs ``import_session.py``
on the located path directly. Other hosts transform via their reader first,
then hand the canonical transcript to the SAME ``import_session.py`` — one
pipeline, per-host front doors. Conversation ids for non-Claude hosts are
namespaced ``<host>-<session-id>`` so server-side watermarks stay per-host.

The mcp SDK pin (``uv run --with 'mcp<2'``) matches every other invocation
site: mcp 2.x renamed streamablehttp_client, breaking import_session.py's
transport. ``list`` is stdlib-only and runs under bare python3.

Hook-event dispatch (``capture.py <host> <event>``) intentionally does NOT
live here yet — it lands with the first non-Claude hooks file, so no dead
dispatch table ships in between.
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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


def _resolve(args) -> tuple[object | None, Path | None, str]:
    """(reader, path, err) for the requested session."""
    host = args.host
    if host == "auto":
        host = readers.sniff(args.session)
        if host is None:
            return None, None, (
                f"cannot infer the host from {args.session!r} — a bare id or "
                "'latest' is ambiguous across hosts; pass --host claude|codex")
    r = readers.reader_for(host)
    if r is None:
        return None, None, f"unknown host {host!r} (known: {', '.join(readers.READERS)})"
    path, err = r.locate(args.session)
    if path is None:
        return None, None, err
    return r, path, ""


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
    if args.namespace is not None:
        passthrough += ["--namespace", args.namespace]
    if args.url:
        passthrough += ["--url", args.url]

    def run_import(transcript: Path, conv_id: str | None) -> int:
        cmd = ["uv", "run", "--with", "mcp<2", "python", str(_IMPORT_SESSION),
               "--session", str(transcript)]
        if conv_id:
            cmd += ["--conversation-id", conv_id]
        return subprocess.run(cmd + passthrough).returncode

    if r.HOST == claude_reader.HOST:
        # Already canonical — import_session.py reads the transcript in place.
        return run_import(path, args.conversation_id)

    records, meta = r.to_canonical(path)
    if not records:
        print(f"ERROR: nothing to import from {path}", file=sys.stderr)
        return 2
    problems = readers.validate_canonical(records)
    if problems:
        print(f"ERROR: transform produced non-canonical records: {problems[:3]}",
              file=sys.stderr)
        return 2

    # Keep conv_id == <host>-<session-uuid> however the session was addressed,
    # so incremental dedup holds across re-imports.
    sid = meta.get("session_id") or path.stem
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

    ip = sub.add_parser("import", help="import one session into MemHub")
    ip.add_argument("--session", required=True,
                    help="transcript/rollout path, bare session id, or 'latest'")
    ip.add_argument("--host", default="auto", choices=["auto", *readers.READERS])
    ip.add_argument("--conversation-id", default=None)
    ip.add_argument("--title", default=None)
    ip.add_argument("--agent-brain-id", default=None)
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
