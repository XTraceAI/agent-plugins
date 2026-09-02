#!/usr/bin/env python3
"""Flag candidate rules that collide with rules already in the book — BEFORE
they are filed. Deterministic, offline-capable, stdlib only.

Why this lives in the skill and not on the server: the server's re-import
identity is (rulebook, source_ref path, title) and it deliberately never
adopts a rule owned by another document — so a candidate whose title or
matcher collides with an existing rule is filed as a second draft, silently.
The agent running `/memhub:create-rule` or `/memhub:rules-from-sessions` is the
right place to notice: it is already holding the candidates and the book, and
it can read two statements and say "same rule" better than any key can.

Two inputs, three checks:

  --candidates <file|->   the create_rule bodies you are about to send
  --existing   <file>     the `list_rules` reply (every status; no engine
                          blocks) — title collisions across every rulebook
                          you can see, so pass it WITHOUT a rulebook_id
  --repo <name>           the hook view of the ACTIVE book (engine blocks),
                          fetched from the server, or --book <file> to read a
                          cached / test book instead — matcher + anchor
                          collisions against what can actually double-fire
  --rulebook-id <id>      the book the candidates will be filed into

A rulebook is a container with its own membership, and one person can be bound
by several (container spec §3, §4) — so both inputs span more than one book and
a candidate can collide with a rule it will never share a book with. Every hit
names its book. A hit outside `--rulebook-id` is marked `cross_book`, because
`supersedes_rule_id` retires a rule WITHIN one book and cannot reach it: those
two rules will both fire on the same call until a human retires one side.

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
_ID_OK = re.compile(r"[^\x00-\x1f\x7f]{1,64}")   # a UUID is 36; no control bytes, ever
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


def book_of(rule: dict) -> dict:
    """Which rulebook a row came from. A rulebook is a container with its own
    membership (container spec §3), so both inputs now span SEVERAL books: the
    hook view is every book that binds the author, and `list_rules` with no
    `rulebook_id` is every book they can see. Rows from a pre-container
    backend carry nothing here and read as one unnamed book, which is what
    they were."""
    if not isinstance(rule, dict):
        return {}
    b = rule.get("rulebook")
    b = b if isinstance(b, dict) else {}
    rid = rule.get("rulebook_id") or b.get("rulebook_id")
    out: dict = {}
    # An id is REJECTED rather than cleaned. It renders (the summary falls back
    # to it when a book has no name), so a newline in it forges a line just as
    # a newline in the name would — but it is also compared against
    # `--rulebook-id` and used as a dedup key, so rewriting it would corrupt
    # the equality the whole cross_book decision rests on. A real one is a UUID.
    if isinstance(rid, str) and rid.strip() and _ID_OK.fullmatch(rid.strip()):
        out["rulebook_id"] = rid.strip()
    # A book name is typed by a teammate and lands in the agent's context via
    # this report: one line, no control characters, capped — the same handling
    # rulebook_hook.py gives server prose. A newline here would let a name
    # forge a line of the summary below.
    name = _WS.sub(" ", re.sub(r"[\x00-\x1f\x7f]+", " ", str(b.get("name") or ""))).strip()
    if name:
        out["name"] = name[:120]
    return out


def _learn_book(hit: dict, rule: dict) -> dict:
    """A rule can be hit twice — once through `existing` (titles) and once
    through `active` (matchers) — and only one of those rows may carry the
    `rulebook` block. The book is a property of the RULE, so the first row to
    know it wins, whichever pass that was. Without this a title+matcher
    collision (the likeliest real one) keeps the empty book from the title
    pass, reads as same-book, and is handed the `supersedes_rule_id` line that
    §7 says must never appear across books."""
    if "rulebook" not in hit:
        book = book_of(rule)
        if book:
            hit["rulebook"] = book
    return hit


def _hit(rule: dict, reasons: list[str], detail=None) -> dict:
    h = {"rule_id": str(rule.get("rule_id") or ""), "title": rule.get("title"),
         "status": rule.get("status") or "active", "reasons": reasons}
    book = book_of(rule)
    if book:
        h["rulebook"] = book
    if detail:
        h["anchors_shared"] = sorted(detail)
    return h


def find_conflicts(candidates: list[dict], existing: list[dict], active: list[dict] | None,
                   target_rulebook_id: str | None = None) -> dict:
    """Pure. `existing` = list_rules rows (any status; titles + statements);
    `active` = hook-view rows (engine blocks) or None when unavailable.

    `target_rulebook_id` is the book the candidates will be FILED into. A hit
    in another book is marked `cross_book`, and that changes the remedy, not
    just the wording: `supersedes_rule_id` retires a rule inside one book, so
    it cannot answer a collision with a book you are not writing to. Those
    collide anyway — both books bind the author, so both rules reach the same
    call — and the only fixes are a human one (retire one side, move the rule,
    narrow a scope). Saying "supersede it" there would be advice that fails."""
    live = [r for r in existing if r.get("status") not in RETIRED]
    out = []
    hit_ids: set[str] = set()
    for cand in candidates:
        hits: dict[str, dict] = {}
        ct = normalise_title(cand.get("title"))
        for r in live:
            if ct and normalise_title(r.get("title")) == ct:
                _learn_book(hits.setdefault(str(r.get("rule_id")), _hit(r, [])), r)["reasons"] \
                    .append("same_title")
        if active is not None:
            csig, canch = matcher_signature(cand), anchor_set(cand)
            for r in active:
                rid = str(r.get("rule_id"))
                if csig and matcher_signature(r) == csig:
                    # a rule in the hook view is active by definition; _hit
                    # already records that for rows list_rules didn't cover
                    _learn_book(hits.setdefault(rid, _hit(r, [])), r)["reasons"] \
                        .append("same_matcher")
                shared = canch & anchor_set(r)
                if shared:
                    h = _learn_book(hits.setdefault(rid, _hit(r, [], shared)), r)
                    h["reasons"].append("anchors_overlap")
                    h["anchors_shared"] = sorted(shared)
        hit_ids.update(hits)
        out.append({"title": cand.get("title"), "delivery": cand.get("delivery"),
                    "hits": list(hits.values())})
    unmatched = [dict({"rule_id": str(r.get("rule_id")), "title": r.get("title"),
                       "status": r.get("status") or "active", "statement": r.get("statement")},
                      **({"rulebook": book_of(r)} if book_of(r) else {}))
                 for r in live if str(r.get("rule_id")) not in hit_ids]
    books = sorted({b["rulebook_id"]: b for b in
                    (book_of(r) for r in list(existing) + list(active or []))
                    if b.get("rulebook_id")}.values(), key=lambda b: b.get("name") or "")
    # Three states, not two. `False` must mean "same book, CONFIRMED", because
    # it is what licenses the copyable supersede line — so a report that cannot
    # know says `None` and gets a question instead of an answer. The case that
    # matters: a migrated backend where the caller forgot --rulebook-id. The
    # rows carry their books, the hit is very possibly cross-book, and treating
    # unknown as same-book would hand out the one instruction §7 forbids.
    # Whether a book dimension EXISTS is decided by the key being present, not
    # by it parsing. Otherwise rejecting a malformed id downgrades the report
    # to "pre-container, one implicit book" — and hands back the supersede line
    # that rejecting it was meant to withhold.
    book_dimension = any(isinstance(r, dict) and (r.get("rulebook") or r.get("rulebook_id"))
                         for r in list(existing) + list(active or []))
    for c in out:
        for h in c["hits"]:
            rid = h.get("rulebook", {}).get("rulebook_id")
            if target_rulebook_id and rid:
                h["cross_book"] = rid != target_rulebook_id
            elif not book_dimension:   # no book anywhere: one implicit book, as before
                h["cross_book"] = False
            else:
                h["cross_book"] = None
    return {"candidates": out, "judge_by_statement": unmatched, "rulebooks": books,
            "target_rulebook_id": target_rulebook_id,
            "active_book": "checked" if active is not None else "unavailable"}


def fetch_active(repo: str) -> list[dict] | None:
    """The hook view of the active book, straight from the server, NOT written
    to the hook cache (that cache arms rules; this is a read for a report)."""
    api = _api()
    if not api:
        return None
    base, bearer, http = api
    q = "view=hook&repo=" + urllib.parse.quote(repo, safe="")
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
            book = h.get("rulebook", {}).get("name") or h.get("rulebook", {}).get("rulebook_id")
            where = f" in {book}" if book else ""
            lines.append(f"  {c['title']}: {'+'.join(h['reasons'])} -> {h['title']} "
                         f"[{h['status']}]{where}{extra}")
            if h.get("cross_book"):
                lines.append("    ANOTHER RULEBOOK — supersedes_rule_id cannot reach it. Both books "
                             "bind you, so both rules fire on the same call: tell the user and let a "
                             "human retire one side or narrow its scope.")
            elif h.get("cross_book") is None:
                lines.append("    WHICH RULEBOOK? — re-run with --rulebook-id <destination> before "
                             "superseding anything. supersedes_rule_id reaches a rule in the SAME "
                             "book only, and this report cannot tell whether it is the same book.")
            elif h["rule_id"]:
                lines.append(f"    if this is the same rule: create_rule supersedes_rule_id=\"{h['rule_id']}\"")
    if len(report.get("rulebooks") or []) > 1:
        lines.append("  spans " + str(len(report["rulebooks"])) + " rulebooks: "
                     + ", ".join(b.get("name") or b["rulebook_id"] for b in report["rulebooks"]))
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
    ap.add_argument("--rulebook-id", help="the rulebook the candidates will be FILED into; a hit in "
                                          "another book is flagged cross_book (supersede cannot reach it)")
    args = ap.parse_args(argv)

    candidates = _load(args.candidates)
    existing = _load(args.existing) if args.existing else []
    active = None
    if args.book:
        # the documented usage is a glob (the cache file name carries a hash
        # of the repo name); resolve it here so a quoted pattern works too
        matches = sorted(glob.glob(os.path.expanduser(args.book))) or [args.book]
        if len(matches) > 1:
            print(f"--book matched {len(matches)} files; using {matches[0]} "
                  f"(skipped: {', '.join(matches[1:])}) — pass one file to disambiguate",
                  file=sys.stderr)
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
    report = find_conflicts(candidates, existing, active, args.rulebook_id)
    print(json.dumps(report, indent=1))
    print("\nCONFLICTS (deterministic; then judge the rest by statement):\n" + _summary(report),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
