#!/usr/bin/env python3
"""Import an OpenAI Codex session into MemHub team memory.

Reads a Codex *rollout* transcript (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``),
transforms it into the Claude Code record shape (see ``codex_to_claude``), and
hands it to the plugin's ``import_session.py`` — reusing its chunking, session
gist fold-forward, incremental-dedup, namespace resolution, and the SAME OAuth
the /mcp connector uses. The transform is what routes the session to the
tool-aware **agentic** ingestion path with no backend change.

Usage (mcp SDK pulled ephemerally by uv):

    uv run --with 'mcp>=2,<3' python codex/import_codex_session.py --session latest
    uv run --with 'mcp>=2,<3' python codex/import_codex_session.py \
        --session <rollout-path|session-id|latest> \
        [--agent-brain-id <id>] [--conversation-id <id>] [--title "..."] \
        [--url <mcp-url>]

``--session`` accepts a rollout path, a bare Codex session id (searched under
``~/.codex/sessions``), or ``latest`` for the most recently modified rollout.
The conversation id defaults to ``codex-<session-id>`` so re-imports are
incremental (the server watermark folds the gist forward instead of
duplicating).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_to_claude import load_rollout, rollout_to_claude_records  # noqa: E402

_SESSIONS = Path.home() / ".codex" / "sessions"
_IMPORT_SESSION = (Path(__file__).resolve().parent.parent
                   / "plugins" / "memhub" / "scripts" / "import_session.py")

# Codex rollout files are named rollout-<ISO-timestamp>-<uuid>.jsonl.
_ROLLOUT_UUID_RE = re.compile(
    r"-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$")


def rollout_uuid(path) -> str | None:
    """The trailing session UUID of a rollout filename, or None if it doesn't
    match the ``rollout-<ts>-<uuid>`` pattern."""
    m = _ROLLOUT_UUID_RE.search(Path(path).stem)
    return m.group(1) if m else None


def resolve_rollout(session: str) -> tuple[Path | None, str]:
    """Accept a rollout path, ``latest``, or a bare Codex session id (UUID)."""
    p = Path(session).expanduser()
    if p.is_file():
        return p, ""
    if "/" in session and session != "latest":
        return None, f"rollout file not found: {p}"
    files = [Path(f) for f in glob.glob(str(_SESSIONS / "**" / "rollout-*.jsonl"),
                                        recursive=True)]
    if not files:
        return None, f"no Codex rollouts under {_SESSIONS}"
    if session == "latest":
        return max(files, key=lambda f: f.stat().st_mtime), ""
    # Match the session UUID exactly — a partial/fragment id does NOT match (it
    # would risk selecting the wrong session and folding-forward the wrong
    # conversation's gist). Ambiguity is an error, never a largest-file guess.
    sid = session.removesuffix(".jsonl")
    hits = [f for f in files if rollout_uuid(f) == sid]
    if not hits:
        return None, (f"no Codex rollout with session UUID {sid!r} under "
                      f"{_SESSIONS} (pass the full UUID or a rollout path)")
    if len(hits) > 1:
        return None, (f"ambiguous session id {sid!r}: {len(hits)} rollouts match — "
                      "pass the full session UUID or the rollout path")
    return hits[0], ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a Codex session into MemHub.")
    ap.add_argument("--session", required=True,
                    help="rollout path, a bare Codex session id, or 'latest'")
    ap.add_argument("--conversation-id", default=None,
                    help="override (default: codex-<session-id>, for incremental dedup)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--agent-brain-id", default=None)
    ap.add_argument("--namespace", default=None,
                    help="repo scope for captured directives; default resolves "
                         "from the session's cwd via git remote, '' disables")
    ap.add_argument("--url", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="write the transformed transcript and print a summary, "
                         "but do not call the import tool")
    args = ap.parse_args()

    f, err = resolve_rollout(args.session)
    if f is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    rollout = load_rollout(f)
    if not rollout:
        print(f"ERROR: {f} has no valid records", file=sys.stderr)
        return 2
    records, meta = rollout_to_claude_records(rollout)
    if not records:
        print(f"ERROR: nothing to import from {f}", file=sys.stderr)
        return 2

    # Keep conv_id == codex-<session-uuid> regardless of how the session was
    # addressed (meta id, --session latest, or a bare id), so incremental dedup
    # holds. Fall back to the filename UUID, then the stem, if meta lacks the id.
    sid = meta.get("session_id") or rollout_uuid(f) or f.stem
    conv_id = args.conversation_id or f"codex-{sid}"
    title = args.title or meta.get("title")

    n_tool = sum(1 for r in records
                 if isinstance(r["message"].get("content"), list)
                 and r["message"]["content"]
                 and r["message"]["content"][0].get("type") == "tool_use")
    print(f"rollout         : {f}")
    print(f"codex session   : {sid}   (model {meta.get('model')})")
    print(f"records         : {len(records)}  ({n_tool} tool calls)")
    print(f"cwd             : {meta.get('cwd')}")
    print(f"conversation_id : {conv_id}")
    print(f"title           : {title}")
    print("-" * 56)

    body = "".join(json.dumps(r) + "\n" for r in records)

    if args.dry_run:
        # Deterministic path: overwritten on re-run (so dry-runs don't
        # accumulate) and left in place for inspection — that's the point of
        # --dry-run.
        transcript = Path(tempfile.gettempdir()) / f"memhub-codex-dryrun-{sid}.jsonl"
        transcript.write_text(body)
        print(f"[dry-run] wrote {len(records)} records -> {transcript}")
        print("[dry-run] skipping import_conversation (file left for inspection)")
        return 0

    # Real import: a throwaway temp dir, always cleaned up. Named codex-<sid>.jsonl
    # so a bare run (no --conversation-id) still gets a stable, codex-scoped id
    # from the file stem.
    tmpdir = Path(tempfile.mkdtemp(prefix="memhub-codex-"))
    transcript = tmpdir / f"codex-{sid}.jsonl"
    transcript.write_text(body)

    try:
        cmd = [
            # Pinned like every other invocation site. The scripts now target
            # the 2.x API (auth on the httpx2 client, snake_case results), so a
            # 1.x resolution breaks import_session.py at import.
            "uv", "run", "--with", "mcp>=2,<3", "python", str(_IMPORT_SESSION),
            "--session", str(transcript),
            "--conversation-id", conv_id,
        ]
        if title:
            cmd += ["--title", title]
        if args.agent_brain_id:
            cmd += ["--agent-brain-id", args.agent_brain_id]
        if args.namespace is not None:
            cmd += ["--namespace", args.namespace]
        if args.url:
            cmd += ["--url", args.url]

        return subprocess.run(cmd).returncode
    finally:
        # Always clean up the temp transcript — including on the dry-run path.
        try:
            transcript.unlink()
            tmpdir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
