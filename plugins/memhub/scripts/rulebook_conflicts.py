#!/usr/bin/env python3
"""Flag candidate rules that collide with rules already in the book — BEFORE
they are filed. Deterministic, offline-capable, stdlib only.

Why this lives in the skill and not on the server: the server's re-import
identity is (rulebook, source_ref path, title) and it deliberately never
adopts a rule owned by another document — so a candidate whose title or
matcher collides with an existing rule is filed as a second draft, silently.
The agent running `/memhub:create-rule` or `/memhub:import-claude-md` is the
right place to notice: it is already holding the candidates and the book, and
it can read two statements and say "same rule" better than any key can.

Two inputs, three checks:

  --candidates <file|->   the create_rule bodies you are about to send
  --existing   <file>     the `list_rules` reply (every status; no engine
                          blocks) — title collisions across the WHOLE book
  --repo <name>           the hook view of the ACTIVE book (engine blocks),
                          fetched from the server, or --book <file> to read a
                          cached / test book instead — matcher + anchor
                          collisions against what can actually double-fire

  same_title      normalised title equal (case / punctuation insensitive)
  same_matcher    same event and the same primary pattern (command_rx /
                  path_rx / content_rx / required_command_rx), whitespace-
                  normalised — two actives here fire on the same call
  anchors_overlap the anchor sets intersect (case-insensitive); the
                  intersection is listed so a one-symbol overlap is judged,
                  not auto-flagged

Output: JSON on stdout — per candidate its hits, and the existing rules with
NO deterministic hit (title + statement) so the semantic pass has one list
to read. A human-readable summary goes to stderr. Exit 0 always; a fetch
failure degrades to "active book unavailable" and says so.

Usage:
  rulebook_conflicts.py --candidates cands.json --existing rules.json --repo xmem
  rulebook_conflicts.py --candidates - --book ~/.config/memhub-plugin/rulebook/book/xmem-*.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rulebook_hook import API_PATH, FETCH_TIMEOUT_S, _api, load_book  # noqa: E402

_NON_WORD = re.compile(r"[^\w]+")
_WS = re.compile(r"\s+")
RETIRED = ("deprecated", "superseded")
PRIMARY_RX = ("command_rx", "path_rx", "content_rx")


def normalise_title(title) -> str:
    return _NON_WORD.sub(" ", str(title or "").casefold()).strip()


def _rx(s) -> str:
    return _WS.sub("", str(s or ""))


def matcher_signature(rule: dict):
    """(event, primary pattern) for an agent_hook rule, or None. Ordering
    rules key on their required command. Anchor / posture rules have none."""
    m = rule.get("matcher")
    if isinstance(m, dict) and m.get("event"):
        for k in PRIMARY_RX:
            if m.get(k):
                return (str(m["event"]), _rx(m[k]))
        return None
    o = rule.get("ordering")
    if isinstance(o, dict) and o.get("required_command_rx"):
        return ("ordering", _rx(o["required_command_rx"]))
    return None


def anchor_set(rule: dict) -> set[str]:
    a = rule.get("anchors")
    return {str(x).casefold() for x in a if str(x).strip()} if isinstance(a, list) else set()


def _hit(rule: dict, reasons: list[str], detail=None) -> dict:
    h = {"rule_id": str(rule.get("rule_id") or ""), "title": rule.get("title"),
         "status": rule.get("status") or "active", "reasons": reasons}
    if detail:
        h["anchors_shared"] = sorted(detail)
    return h


def find_conflicts(candidates: list[dict], existing: list[dict], active: list[dict] | None) -> dict:
    """Pure. `existing` = list_rules rows (any status; titles + statements);
    `active` = hook-view rows (engine blocks) or None when unavailable."""
    live = [r for r in existing if r.get("status") not in RETIRED]
    out = []
    hit_ids: set[str] = set()
    for cand in candidates:
        hits: dict[str, dict] = {}
        ct = normalise_title(cand.get("title"))
        for r in live:
            if ct and normalise_title(r.get("title")) == ct:
                hits.setdefault(str(r.get("rule_id")), _hit(r, []))["reasons"].append("same_title")
        if active is not None:
            csig, canch = matcher_signature(cand), anchor_set(cand)
            for r in active:
                rid = str(r.get("rule_id"))
                if csig and matcher_signature(r) == csig:
                    h = hits.setdefault(rid, _hit(r, []))
                    h["reasons"].append("same_matcher")
                    h["status"] = "active"
                shared = canch & anchor_set(r)
                if shared:
                    h = hits.setdefault(rid, _hit(r, [], shared))
                    h["reasons"].append("anchors_overlap")
                    h["anchors_shared"] = sorted(shared)
                    h["status"] = "active"
        hit_ids.update(hits)
        out.append({"title": cand.get("title"), "delivery": cand.get("delivery"),
                    "hits": list(hits.values())})
    unmatched = [{"rule_id": str(r.get("rule_id")), "title": r.get("title"),
                  "status": r.get("status") or "active", "statement": r.get("statement")}
                 for r in live if str(r.get("rule_id")) not in hit_ids]
    return {"candidates": out, "judge_by_statement": unmatched,
            "active_book": "checked" if active is not None else "unavailable"}


def fetch_active(repo: str) -> list[dict] | None:
    """The hook view of the active book, straight from the server, NOT written
    to the hook cache (that cache arms rules; this is a read for a report)."""
    api = _api()
    if not api:
        return None
    base, bearer, http = api
    q = "status=active&view=hook&repo=" + urllib.parse.quote(repo, safe="")
    try:
        reply = http.rest(f"{base}{API_PATH}/rules?{q}", bearer, "GET", timeout=FETCH_TIMEOUT_S)
    except Exception:
        return None
    if reply.status == 200 and isinstance(reply.data, dict) and isinstance(reply.data.get("rules"), list):
        return reply.data["rules"]
    return None


def _load(path: str):
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    data = json.loads(raw)
    if isinstance(data, dict) and isinstance(data.get("rules"), list):
        return data["rules"]
    if isinstance(data, dict) and isinstance(data.get("candidates"), list):
        return data["candidates"]
    if isinstance(data, dict):
        return [data]
    return data


def _summary(report: dict) -> str:
    lines = []
    for c in report["candidates"]:
        if not c["hits"]:
            lines.append(f"  {c['title']}: no deterministic hit")
            continue
        for h in c["hits"]:
            extra = f" shared={','.join(h['anchors_shared'])}" if h.get("anchors_shared") else ""
            lines.append(f"  {c['title']}: {'+'.join(h['reasons'])} -> {h['title']} [{h['status']}]{extra}")
    lines.append(f"  active book: {report['active_book']}; "
                 f"{len(report['judge_by_statement'])} existing rules left to judge by statement")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--candidates", required=True, help="create_rule bodies (JSON list, or - for stdin)")
    ap.add_argument("--existing", help="the list_rules reply (JSON) — all statuses")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--repo", help="fetch the ACTIVE book (hook view) for this repo from the server")
    src.add_argument("--book", help="read a cached / test hook-view book file instead of fetching")
    args = ap.parse_args(argv)

    candidates = _load(args.candidates)
    existing = _load(args.existing) if args.existing else []
    active = None
    if args.book:
        # the documented usage is a glob (the cache file name carries a hash
        # of the repo name); resolve it here so a quoted pattern works too
        matches = sorted(glob.glob(os.path.expanduser(args.book))) or [args.book]
        try:
            with open(matches[0], encoding="utf-8") as f:
                b = json.load(f)
            active = b.get("rules") if isinstance(b, dict) else b
        except Exception as exc:
            print(f"could not read --book {matches[0]}: {exc}", file=sys.stderr)
    elif args.repo:
        active = fetch_active(args.repo)
        if active is None:
            cached = load_book(args.repo)
            active = cached["rules"] if cached else None
            if active is not None:
                print("server unreachable — using the hook's cached active book", file=sys.stderr)
    report = find_conflicts(candidates, existing, active)
    print(json.dumps(report, indent=1))
    print("\nCONFLICTS (deterministic; then judge the rest by statement):\n" + _summary(report),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
