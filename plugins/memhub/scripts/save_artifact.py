#!/usr/bin/env python3
"""Store an artifact from a FILE (or stdin) — a terminal operation.

The point: the artifact body is read off disk / the pipe and shipped straight
to the `save_artifact` MCP tool. The model never re-emits the content token by
token — it just runs this with a path, the same way it would `cat` a file.

Auth = the plugin's one credential (shared `_memhub_auth`):
$MEMHUB_TOKEN if set (CI escape hatch), else the cached plugin OAuth token,
else a one-time browser approval. No memhub-cli required.

Run (mcp SDK pulled ephemerally by uv):
    uv run --with 'mcp<2' python scripts/save_artifact.py \
        --file spec.md --name "Retry Policy Spec" --type spec \
        [--agent-brain-id <id>] [--parent-id <id>] [--rationale "..."] \
        [--tags a,b]

    # or pipe terminal output straight in:
    pytest -q | uv run --with 'mcp<2' python scripts/save_artifact.py \
        --stdin --name "test run 2026-06-09" --type runbook

Endpoint resolution (so the script hits the SAME server the plugin connector
uses, by construction): --url > $MEMHUB_MCP_BASE_URL(+$MEMHUB_MCP_SERVER_PATH) >
the plugin's .mcp.json `mcpServers.*.url` > a default derived from the plugin
install path (prod for `memhub`, staging for `memhub-staging`). There is no
fixed fallback env — see `_memhub_auth.default_url`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _memhub_auth import resolve_url_and_auth  # noqa: E402
from brain_resolve import resolve_repo_brain  # noqa: E402
from room_map import env_for_url, read_room, repo_root  # noqa: E402


def unwrap(result) -> dict:
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_raw": text}
    return {"_raw": str(result)}


async def main() -> int:
    ap = argparse.ArgumentParser(description="Store a file/stdin as a MemHub artifact.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="path to the artifact body")
    src.add_argument("--stdin", action="store_true", help="read body from stdin")
    ap.add_argument("--name", required=True, help="artifact title (re-using a name versions it)")
    ap.add_argument("--type", default="document", help="artifact_type (spec/design_doc/runbook/...)")
    ap.add_argument("--agent-brain-id", default=None,
                    help="agent brain to save into. Default: the repo's room — "
                         "cached in ~/.config/memhub-plugin/rooms.json, or resolved "
                         "from the server on a cache miss")
    ap.add_argument("--no-room", action="store_true",
                    help="ignore the repo's cached room and save into personal "
                         "workspace memory")
    ap.add_argument("--parent-id", default=None, help="version an existing artifact by id")
    ap.add_argument("--rationale", default=None, help="why this version supersedes the last")
    ap.add_argument("--tags", default=None, help="comma-separated tags")
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    # The mcp SDK is imported AFTER argparse, not at module scope, so `--help`
    # (and the test suite) work under a bare python3 without `uv run --with`.
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    if not args.stdin and not args.file.is_file():
        print(f"ERROR: file not found: {args.file}", file=sys.stderr)
        return 2
    # Explicit utf-8 on BOTH inputs: the default codec is the OS locale, so on a
    # non-UTF-8 box (cp950, cp1252, …) a piped or on-disk artifact carrying an
    # em-dash either mangles or raises. Unlike the transcript readers this does
    # NOT swallow decode errors — an artifact is the user's content and silently
    # replacing bytes in it would save a corrupted document under their name.
    try:
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # already-wrapped or non-reconfigurable stream
        pass
    try:
        content = sys.stdin.read() if args.stdin else args.file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        src = "stdin" if args.stdin else str(args.file)
        print(f"ERROR: {src} is not valid UTF-8 ({exc.reason}); convert it first",
              file=sys.stderr)
        return 2
    if not content.strip():
        print("ERROR: artifact body is empty", file=sys.stderr)
        return 2

    call_args: dict = {"name": args.name, "content": content, "artifact_type": args.type}
    if args.agent_brain_id:
        call_args["agent_brain_id"] = args.agent_brain_id
    if args.parent_id:
        call_args["parent_id"] = args.parent_id
    if args.rationale:
        call_args["rationale"] = args.rationale
    if args.tags:
        call_args["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]

    url, headers, auth = resolve_url_and_auth(args.url)

    # An explicit --agent-brain-id wins; otherwise route to the repo's cached
    # room so a spec saved from a repo lands where teammates search.
    #
    # The FILE's repo first — a doc living in repo Y is Y's, even when invoked
    # from elsewhere — then the caller's repo, which covers the common case of
    # saving an ad-hoc file (a rendered page, a download) from a temp path while
    # working in a repo. Neither resolves → personal memory.
    #
    # A file sitting inside an UNRELATED repo therefore routes to that repo's
    # room. That is why the destination is printed below and the skill reports
    # it: automatic routing is only safe if it's visible. Use --no-room to
    # override.
    #
    # The cache is read first; on a miss the room is resolved from the server
    # over the session opened below (the same `resolve_repo_brain` the capture
    # hooks use), so a hand-saved artifact lands in the repo room whenever one
    # exists — not only after something else happened to cache it.
    room = None
    room_cwd: Path | None = None
    want_room = not args.agent_brain_id and not args.no_room
    env = env_for_url(url)
    if want_room:
        file_dir = None if args.stdin else args.file.resolve().parent
        if file_dir is not None and repo_root(file_dir) is not None:
            # The file lives in a repo — that repo is authoritative, and if it
            # has no cached room the artifact stays personal. Falling back to
            # the caller here would file repo Y's doc into repo X's room, since
            # "no room cached" and "not in a repo" are both None from read_room.
            room_cwd = file_dir
        room = read_room(room_cwd, env)

    src_desc = "stdin" if args.stdin else str(args.file)
    print(f"source   : {src_desc}  ({len(content):,} chars)")
    print(f"name     : {args.name}   type={args.type}")
    print(f"endpoint : {url}")

    async with streamablehttp_client(url, headers=headers, auth=auth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if want_room and room is None and room_cwd is not None:
                # Cache miss inside a repo: ask the server, the same exact-name
                # lookup the capture hooks do. room_cwd is None only when the
                # file is outside any repo — never resolve from the process
                # cwd, that would file it into an unrelated repo's room. A
                # lookup failure is not a reason to lose the save: fall back
                # to personal memory and say so.
                try:
                    room = await resolve_repo_brain(session, room_cwd, env)
                except Exception as exc:  # noqa: BLE001 — degrade, never abort the save
                    print(f"room     : lookup failed ({exc.__class__.__name__}); saving to personal memory")
                    room = None
            if room:
                call_args["agent_brain_id"] = room["brain_id"]
            if call_args.get("agent_brain_id"):
                origin = f' (repo room "{room.get("name", "?")}")' if room else ""
                print(f"brain    : {call_args['agent_brain_id']}{origin}")
            print("-" * 56)
            res = await session.call_tool("save_artifact", arguments=call_args)
            out = unwrap(res)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
