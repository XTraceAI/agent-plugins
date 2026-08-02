#!/usr/bin/env python3
"""Enumerate (and optionally delete) workspace memories captured today.

Dry-run by default: lists facts/episodes/artifacts captured >= --since in the
caller's workspace, deduped by id. With --execute it deletes them via the
delete_fact / delete_episode / delete_artifact MCP tools.

Auth = the SAME OAuth the /mcp connector uses (shared `_memhub_auth`):
$MEMHUB_TOKEN if set, else the cached plugin OAuth token, else a one-time
browser approval.

    uv run --with 'mcp>=1.28,<2' python scripts/purge_today.py --since 2026-06-09           # dry-run
    uv run --with 'mcp>=1.28,<2' python scripts/purge_today.py --since 2026-06-09 --execute # delete
"""
from __future__ import annotations
import argparse, asyncio, json, sys
from pathlib import Path
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"))
from _memhub_auth import resolve_url_and_auth  # noqa: E402

# Broad, diverse queries to surface today's items across both test imports.
QUERIES = [
    "memhub plugin save-artifact terminal upload hook install",
    "trajectory gist skill induction artifact guard extractor",
    "context agent creation discovery flow plan",
    "mcp staging import conversation agentic dedup",
    "branch commit pull request endpoint .mcp.json",
    "xmem skill design source of truth episode fact",
    "tool call result file dump narration ProviderConfig",
    "memory quality facts episodes artifacts assessment",
]
DELETE_TOOL = {"fact": "delete_fact", "episode": "delete_episode", "artifact": "delete_artifact"}
ID_ARG = {"fact": "fact_id", "episode": "episode_id", "artifact": "artifact_id"}


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
    return {}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="created_after (ISO date)")
    ap.add_argument("--execute", action="store_true", help="actually delete (default: dry-run)")
    ap.add_argument("--keep-name", action="append", default=[],
                    help="skip artifacts whose content/name contains this substring")
    args = ap.parse_args()
    url, headers, auth = resolve_url_and_auth()
    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            # enumerate
            found: dict[str, dict] = {}  # id -> {type, content}
            for mtype in ("facts", "episodes", "artifacts"):
                for q in QUERIES:
                    res = await s.call_tool("search_memory", arguments={
                        "query": q, "memory_type": mtype, "top_k": 50,
                        "created_after": args.since,
                    })
                    for it in (unwrap(res).get("items") or []):
                        iid = it.get("id")
                        if iid and iid not in found:
                            found[iid] = {"type": it.get("type"), "content": str(it.get("content"))[:90]}

            by_type: dict[str, list] = {"fact": [], "episode": [], "artifact": []}
            for iid, meta in found.items():
                t = meta["type"]
                if t in by_type:
                    if t == "artifact" and any(k.lower() in meta["content"].lower() for k in args.keep_name):
                        continue
                    by_type[t].append((iid, meta["content"]))

            total = sum(len(v) for v in by_type.values())
            print(f"{'EXECUTE' if args.execute else 'DRY-RUN'} — since {args.since} — "
                  f"{total} items ({len(by_type['fact'])} facts, "
                  f"{len(by_type['episode'])} episodes, {len(by_type['artifact'])} artifacts)")
            for t in ("fact", "episode", "artifact"):
                print(f"\n## {t}s ({len(by_type[t])})")
                for iid, snip in by_type[t]:
                    print(f"  {iid}  {snip.replace(chr(10),' ')}")

            if not args.execute:
                print("\n(dry-run; re-run with --execute to delete)")
                return 0

            # delete
            print("\n--- deleting ---")
            ok = err = 0
            for t in ("fact", "episode", "artifact"):
                for iid, _ in by_type[t]:
                    try:
                        res = await s.call_tool(DELETE_TOOL[t], arguments={ID_ARG[t]: iid})
                        out = unwrap(res)
                        ok += 1
                        print(f"  deleted {t} {iid} -> {out.get('scope', out)}")
                    except Exception as e:  # noqa: BLE001
                        err += 1
                        print(f"  FAILED {t} {iid}: {e}")
            print(f"\ndone: {ok} deleted, {err} failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
