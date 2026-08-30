#!/usr/bin/env python3
"""Proposal miner — transcripts (Claude Code + Codex + Cursor) -> three lanes,
each with a checkable would-apply predicate, backtested over the same traces.

  rule  lane: matcher over tool calls           -> create_rule body
  skill lane: user-intent pattern over turns    -> create_skill stub (+ adoption gap if the skill exists)
  hook  lane: trigger + later OUTCOME in-session -> PreToolUse settings snippet / plugin PR note

Seeds: Claude Code /insights facets (~/.claude/usage-data/facets) when present; CLAUDE.md via --claude-md.
Numbers per candidate: applies-in N/M sessions (by host), precision, sample sessions.
Friction delta: --baseline-date splits facet friction before/after a rulebook change.

Stdlib + the memhub plugin's readers + the real hook evaluate() (never re-implemented).
Usage: mine_sessions.py [--out DIR] [--baseline-date YYYY-MM-DD] [--skills-file list_skills.json] [--repo NAME]
"""
import sys, os, json, re, glob, collections, importlib.util, time, argparse, datetime

ap = argparse.ArgumentParser()
ap.add_argument("--out", default="mine-out")
ap.add_argument("--baseline-date", help="friction before vs after this date (rule activation day)")
ap.add_argument("--skills-file", help="memhub list_skills JSON reply, for skill-lane dedup")
ap.add_argument("--repo", help="only sessions whose cwd basename matches")
ap.add_argument("--claude-md", action="append", default=[], help="CLAUDE.md (repeatable): its imperative sentences become the declared-rule seed")
ap.add_argument("--rule-file", action="append", default=[], help="a create_rule body (matcher or ordering) to backtest as a candidate (repeatable) — used by create-rule / import-claude-md")
ap.add_argument("--facets", help="facets.json YOU wrote from the digests (see SKILL.md step 2) — replaces the need to run /insights")
ap.add_argument("--digest-top", type=int, default=30, help="how many sessions to digest for the facet pass (ranked by corrections, errors, reverts)")
args = ap.parse_args()
os.makedirs(args.out, exist_ok=True)

def _plugin_scripts():
    here = os.path.dirname(os.path.abspath(__file__))
    for c in (os.environ.get("MEMHUB_PLUGIN_SCRIPTS"), os.path.join(os.environ.get("CLAUDE_PLUGIN_ROOT", ""), "scripts"),
              os.path.normpath(os.path.join(here, "..", "..", "..", "scripts"))):   # shipped inside the plugin: skills/rules-from-sessions/scripts -> plugin scripts
        if c and os.path.isfile(os.path.join(c, "rulebook_hook.py")): return c
    cands = glob.glob(os.path.expanduser("~/.claude/plugins/cache/*/memhub*/*/scripts/rulebook_hook.py")) + \
            glob.glob(os.path.expanduser("~/.claude/plugins/*/plugins/memhub*/scripts/rulebook_hook.py")) + \
            glob.glob(os.path.expanduser("~/.codex/plugins/*/memhub*/scripts/rulebook_hook.py"))
    if not cands: sys.exit("memhub plugin scripts not found; set MEMHUB_PLUGIN_SCRIPTS=<plugin>/scripts")
    if len(cands) > 1: print(f"[warn] several memhub plugin copies; using newest: {max(cands, key=os.path.getmtime)}", file=sys.stderr)
    return os.path.dirname(max(cands, key=os.path.getmtime))
P = _plugin_scripts(); sys.path.insert(0, P)
from readers import claude, codex, cursor  # noqa: E402
spec = importlib.util.spec_from_file_location("rh", os.path.join(P, "rulebook_hook.py")); rh = importlib.util.module_from_spec(spec); spec.loader.exec_module(rh)
t0 = time.time()

# ---------------------------------------------------------------- corpus
def sessions():
    for p in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")): yield "claude", p
    for p in glob.glob(os.path.expanduser("~/.codex/sessions/*/*/*/rollout-*.jsonl")): yield "codex", p
    for p in glob.glob(os.path.expanduser("~/.cursor/chats/*/*/store.db")): yield "cursor", p
    for p in glob.glob(os.path.expanduser("~/.cursor/projects/*/agent-transcripts/*/*.jsonl")): yield "cursor", p
R = {"claude": claude, "codex": codex, "cursor": cursor}
corpus, errs = [], collections.Counter()
for host, path in sessions():
    try: recs, meta = R[host].to_canonical(path)
    except Exception: errs[host] += 1; continue
    users, calls, results, ts = [], [], [], None
    for r in recs:
        ts = ts or r.get("timestamp")
        m = r.get("message") or {}; c = m.get("content")
        if r.get("type") == "user" and isinstance(c, str) and not c.startswith("<"): users.append(c)
        if isinstance(c, list):
            for b in c:
                if not isinstance(b, dict): continue
                if r.get("type") == "user" and b.get("type") == "text" and not str(b.get("text", "")).startswith("<"): users.append(b.get("text", ""))
                if b.get("type") == "tool_use":
                    i = b.get("input") or {}; n = b.get("name", ""); cmd = ""; fp = str(i.get("file_path", "")); body = str(i.get("new_string", "")) + str(i.get("content", ""))
                    if n == "Bash": cmd = str(i.get("command", ""))
                    elif n in ("exec_command", "shell_command"): n = "Bash"; cmd = str(i.get("cmd") or i.get("command") or "")   # Codex, as codex_hook_bridge maps it
                    elif n == "apply_patch":
                        n = "Write"; patch = str(i.get("input", "")); mm = re.search(r"\*\*\* (?:Update|Add) File: (.+)", patch); fp = mm.group(1).strip() if mm else ""; body = patch
                    calls.append({"tool": n, "cmd": cmd, "path": fp, "body": body, "n": len(calls)})
                if b.get("type") == "tool_result":
                    cc = b.get("content"); results.append(cc if isinstance(cc, str) else " ".join(str(x.get("text", "")) for x in cc if isinstance(x, dict)))
    cwd = meta.get("cwd") or (recs[0].get("cwd") if recs else "") or ""
    repo = os.path.basename(cwd.rstrip("/")) if cwd else "?"
    if args.repo and repo != args.repo: continue
    sid = meta.get("session_id") or os.path.splitext(os.path.basename(path))[0]
    corpus.append({"id": sid, "host": host, "repo": repo, "start": (ts or "")[:10], "users": users, "calls": calls, "results": results})
CORRECTION = re.compile(r"^(no|nope|wrong|wait|stop)\b|\b(not what i|why did (u|you)|did (u|you) (just )?|actually (read|test|run|check|do)|i said|i meant|revert that|undo that|is (all )?stale|u should|you should|read the (actual|real)|check the (live|actual|latest|agent|other)|this is (prod|staging)|not (prod|staging)|don'?t (code|merge|push|delete|guess)|plan first)\b", re.I)
PASTED = re.compile(r"^(Base directory for this skill|Approach this as|<command-message>|<task-notification>|This session is being continued)", re.I)
ERROR = re.compile(r"Traceback \(most recent call last\)|^Exit code [1-9]|\bexit code [1-9]\b|ModuleNotFoundError|FAILED \(|\d+ failed\b|Permission denied|command not found|<tool_use_error>", re.M)
REVERT = re.compile(r"git\s+(revert|reset|checkout\s+--|restore)\b|--force-with-lease|commit\s+--amend")
def digest(s):
    """Compact, LLM-readable summary of one session — user turns (corrections marked), errors, reverts. ~1-2k chars."""
    turns = [u.strip().replace("\n", " ") for u in s["users"] if u.strip() and not PASTED.search(u.strip()) and len(u) < 1500]   # skip skill preambles / pasted docs
    corr = [t for t in turns if CORRECTION.search(t[:200])]
    errs = [t.replace("\n", " ")[:160] for t in s["results"] if ERROR.search(t)]
    rev = [c["cmd"].replace("\n", " ")[:120] for c in s["calls"] if c["cmd"] and REVERT.search(c["cmd"])]
    tools = collections.Counter(c["tool"] for c in s["calls"])
    score = 3 * len(corr) + min(len(errs), 10) + 2 * len(rev)
    return {"session_id": s["id"], "host": s["host"], "repo": s["repo"], "start": s["start"], "score": score,
            "first_prompt": (turns[0] if turns else "")[:300],
            "user_turns": [{"i": i, "correction": bool(CORRECTION.search(t[:200])), "text": t[:220]} for i, t in enumerate(turns[:25])],
            "tool_counts": dict(tools), "errors": errs[:4], "reverts": rev[:4]}
digests = sorted((digest(s) for s in corpus), key=lambda d: -d["score"])
os.makedirs(os.path.join(args.out, "digests"), exist_ok=True)
for d in digests[:args.digest_top]: json.dump(d, open(os.path.join(args.out, "digests", d["session_id"][:12] + ".json"), "w"), indent=1)
print(f"digests: top {min(args.digest_top, len(digests))} of {len(corpus)} sessions written to {args.out}/digests/ (ranked by corrections/errors/reverts; read these and write facets.json — SKILL.md step 2)")
M = len(corpus); by_host = collections.Counter(s["host"] for s in corpus)
print(f"sessions read: {dict(by_host)} (M={M})  read errors: {dict(errs)}  ({time.time()-t0:.0f}s)")
print("tool calls per host:", {h: sum(len(s['calls']) for s in corpus if s['host'] == h) for h in R})
def sample(s, text): return {"session": s["id"][:8], "host": s["host"], "repo": s["repo"], "text": text.replace("\n", " ")[:110]}

# ---------------------------------------------------------------- seed: /insights facets
FRICTION_VOCAB = ("wrong_approach", "misunderstood_request", "buggy_code", "unverified_claim", "wrong_environment", "wrong_source", "autonomy_overreach", "environment_issue", "tool_failure")
facets = []
if args.facets:   # the facets YOU wrote from the digests (fixed schema; see SKILL.md)
    try:
        for d in json.load(open(args.facets)):
            fr = d.get("friction") or []
            bad = [x.get("category") for x in fr if x.get("category") not in FRICTION_VOCAB]
            if bad: print(f"[warn] facets.json {d.get('session_id','?')[:8]}: unknown friction category {bad} (allowed: {', '.join(FRICTION_VOCAB)})", file=sys.stderr)
            d["friction_counts"] = dict(collections.Counter(x.get("category") for x in fr if x.get("category") in FRICTION_VOCAB))
            d["friction_detail"] = d.get("friction_detail") or "; ".join(x.get("detail", "") for x in fr)
            facets.append(d)
    except Exception as e: print(f"[warn] --facets unreadable: {e}", file=sys.stderr)
for f in glob.glob(os.path.expanduser("~/.claude/usage-data/facets/*.json")):   # optional extra seed if Claude Code /insights was ever run
    try: d = json.load(open(f)); d.setdefault("source", "insights"); facets.append(d)
    except Exception: pass
_start_full = {s["id"]: s["start"] for s in corpus}
def start_of_id(sid):
    """facets.json may carry short ids (the digests print 8/12-char prefixes); match by prefix."""
    sid = str(sid or "")
    if sid in _start_full: return _start_full[sid]
    hits = [v for k, v in _start_full.items() if k.startswith(sid)] if len(sid) >= 6 else []
    return hits[0] if len(hits) == 1 else None
class _StartOf(dict):
    def get(self, k, default=None): return start_of_id(k) or default
start_of = _StartOf()
if facets:
    print(f"\n=== FACETS SEED — {len(facets)} sessions ({sum(1 for d in facets if d.get('source') != 'insights')} from your facets.json, {sum(1 for d in facets if d.get('source') == 'insights')} from /insights); friction_detail by category (cluster these by eye into candidates)")
    cat = collections.Counter(); 
    for d in facets: cat.update(d.get("friction_counts") or {})
    print("  friction_counts:", cat.most_common(8))
    for d in sorted(facets, key=lambda d: -sum((d.get("friction_counts") or {}).values()))[:40]:
        fr = d.get("friction_counts") or {}
        if fr: print(f"  [{d['session_id'][:8]} {start_of.get(d['session_id'],'?')}] {dict(fr)} :: {(d.get('friction_detail') or '')[:220]}")
    if args.baseline_date:
        bd = args.baseline_date; before = [d for d in facets if (start_of.get(d["session_id"]) or "0") < bd]; after = [d for d in facets if (start_of.get(d["session_id"]) or "0") >= bd]
        def rate(ds):
            if not ds: return "n/a"
            fr = collections.Counter(); 
            for d in ds: fr.update(d.get("friction_counts") or {})
            return f"n={len(ds)}  friction/session={sum(fr.values())/len(ds):.2f}  top={fr.most_common(4)}"
        print(f"\n=== FRICTION DELTA around {bd}\n  before: {rate(before)}\n  after : {rate(after)}\n  (facets exist only for sessions /insights sampled — rerun /insights after new sessions accrue)")

# ---------------------------------------------------------------- seed: CLAUDE.md (declared conventions)
IMPERATIVE = re.compile(r"\b(never|always|must|do not|don'?t|before (any|every|pushing|merging|deleting)|required|load-bearing|banned)\b", re.I)
declared = []
for path in args.claude_md:
    try: lines = open(os.path.expanduser(path), encoding="utf-8", errors="replace").read().splitlines()
    except Exception as e: print(f"[warn] --claude-md {path}: {e}", file=sys.stderr); continue
    head = ""
    for ln in lines:
        if ln.startswith("#"): head = ln.strip("# ").strip()
        t = ln.strip("-* ").strip()
        if 30 < len(t) < 400 and IMPERATIVE.search(t) and not t.startswith(("```", "|")): declared.append({"file": path, "heading": head, "text": t})
if declared:
    print(f"\n=== CLAUDE.MD SEED — {len(declared)} declared imperative sentences (map each to a predicate, then the replay says how often it is violated; a sentence with no checkable form is posture)")
    for d in declared[:60]: print(f"  [{os.path.basename(d['file'])} § {d['heading'][:40]}] {d['text'][:160]}")
    if len(declared) > 60: print(f"  … {len(declared)-60} more in proposals.json")
    proposals_seed = [{"lane": "claude_md", "title": d["heading"][:60] or "claude.md", "text": d["text"], "file": d["file"], "action": "map to a matcher (RULE_CANDS) and re-run; or file via import-claude-md with the backtest number as source_ref suffix"} for d in declared]
else: proposals_seed = []

# ---------------------------------------------------------------- lane 1: rules
EDIT = ("Edit", "Write", "MultiEdit", "NotebookEdit")
RULE_CANDS = [   # each: matcher (+ optional requires_prior_rx for "do X before Y" precision)
 {"title": "fetch-before-origin-read", "matcher": {"event": "bash", "command_rx": r"git\s+(log|diff|show|branch|merge-base|rev-list)\b[^\n]*\borigin/", "command_not_rx": r"git\s+fetch|python3?\s+-c\b|\brulebook\b", "warn_once_per": "session"}, "requires_prior_rx": r"git\s+(fetch|pull)\b"},
 {"title": "no-sed-range-delete", "matcher": {"event": "bash", "command_rx": r"sed\s+-i\b[^\n]*'?\s*\d+(,\d+)?d\b", "command_not_rx": r"python3?\s+-c\b|\brulebook\b", "warn_once_per": "session"}},
]
OUTPUT_CANDS = [{"title": "missing-module-fresh-venv", "content_rx": r"ModuleNotFoundError: No module named|MissingGreenlet"}]
for path in args.rule_file:   # a candidate from create-rule / import-claude-md joins the lane it belongs to
    try: body = json.load(open(os.path.expanduser(path)))
    except Exception as e: print(f"[warn] --rule-file {path}: {e}", file=sys.stderr); continue
    m = body.get("matcher") or {}
    if body.get("ordering"): ORDERING_CANDS_EXTRA = globals().setdefault("ORDERING_CANDS_EXTRA", []); ORDERING_CANDS_EXTRA.append({"title": body.get("title", path), "required_rx": body["ordering"]["required_command_rx"], "gated_rx": body["ordering"]["gated_command_rx"], "min_edits": int(body["ordering"].get("min_edits", 1))})
    elif m.get("event") == "output": OUTPUT_CANDS.append({"title": body.get("title", path), "content_rx": m["content_rx"]})
    elif m: RULE_CANDS.append({"title": body.get("title", path), "matcher": m, "requires_prior_rx": body.get("requires_prior_rx")})
def hook_rule(title, matcher):
    return rh.to_hook_rule({"rule_id": title, "title": title, "statement": "", "delivery": "agent_hook", "mode": "advise", "version": 1, "matcher": matcher, "scope_repos": [], "scope_paths": [], "scope_exclude_paths": []})
def replay(rule, requires_prior_rx=None):
    """calls, sessions(by host), precision (sessions where it fired with NO prior required command), samples"""
    calls = collections.Counter(); sess = collections.Counter(); prec_n = prec_d = 0; ex = []
    for s in corpus:
        fired = False; prior = False
        for cl in s["calls"]:
            tool_n = "Bash" if cl["tool"] == "Bash" else ("Write" if cl["tool"] in EDIT else None)
            if not tool_n: continue
            if requires_prior_rx and cl["cmd"] and re.search(requires_prior_rx, cl["cmd"]): prior = True
            try: ok = rh.evaluate(rule, hook_phase="pre", tool=tool_n, cmd=cl["cmd"], file_path=cl["path"], body=cl["body"])
            except Exception: ok = False
            if ok:
                calls[s["host"]] += 1
                if not fired:
                    fired = True; sess[s["host"]] += 1
                    if requires_prior_rx: prec_d += 1; prec_n += (not prior)
                    if len(ex) < 3: ex.append(sample(s, cl["cmd"] or cl["path"]))
    return {"calls": dict(calls), "sessions": dict(sess), "sessions_total": M, "precision": (f"{prec_n}/{prec_d}" if requires_prior_rx else None), "samples": ex}
proposals = []
book = []
for f in glob.glob(os.path.expanduser("~/.config/memhub-plugin/rulebook/book/*.json")):
    try: book += json.load(open(f)).get("rules", [])
    except Exception: pass
seen = {}; live = []
for r in book:   # several cached books (one per repo) can hold different versions of one rule: keep the newest per title
    if r.get("delivery") != "agent_hook": continue
    if r["title"] in seen and seen[r["title"]] >= int(r.get("version") or 0): continue
    seen[r["title"]] = int(r.get("version") or 0); live = [x for x in live if x["title"] != r["title"]] + [r]
print(f"\n=== LANE 1 · RULES — live book replay ({len(live)} hook rules)")
for r in live:
    res = replay(rh.to_hook_rule(r)); flag = "  <- ZERO historical fires: retire candidate" if not res["sessions"] else ""
    print(f"  {r['title']:28s} sessions={res['sessions']} calls={res['calls']}{flag}")
print("--- candidates")
for c in RULE_CANDS:
    res = replay(hook_rule(c["title"], c["matcher"]), c.get("requires_prior_rx"))
    n = sum(res["sessions"].values()); p = f"  precision={res['precision']} (fired with no prior required cmd)" if res["precision"] else ""
    print(f"  {c['title']:28s} applies-in {n}/{M} sessions {res['sessions']}{p}")
    for e in res["samples"][:2]: print(f"     e.g. [{e['host']}/{e['repo']} {e['session']}] {e['text']}")
    proposals.append({"lane": "rule", "title": c["title"], "predicate": c["matcher"], **res, "source_ref": f"sessions@{datetime.date.today()}#{c['title']}|applies {n}/{M}" + (f"|precision {res['precision']}" if res["precision"] else ""), "action": "create_rule(delivery=agent_hook, matcher=predicate) — verify with rulebook_verify.py first"})
for c in OUTPUT_CANDS:
    hit = collections.Counter(); ex = []
    for s in corpus:
        if any(re.search(c["content_rx"], t) for t in s["results"]):
            hit[s["host"]] += 1
            if len(ex) < 3: ex.append(sample(s, next(t for t in s["results"] if re.search(c["content_rx"], t))))
    n = sum(hit.values()); print(f"  {c['title']:28s} applies-in {n}/{M} sessions {dict(hit)}  (output rule)")
    proposals.append({"lane": "rule", "title": c["title"], "predicate": {"event": "output", "content_rx": c["content_rx"]}, "sessions": dict(hit), "sessions_total": M, "samples": ex, "source_ref": f"sessions@{datetime.date.today()}#{c['title']}|applies {n}/{M}", "action": "create_rule(delivery=agent_hook, matcher=predicate)"})

ORDERING_CANDS = [   # "run X (green, unpiped receipt) after edits, before Y" — the engine's own semantics, replayed
 {"title": "tests-before-push", "required_rx": r"\bpytest\b|npm\s+test|run_all\.py", "gated_rx": r"git\s+push\b", "min_edits": 1},
] + globals().get("ORDERING_CANDS_EXTRA", [])
for c in ORDERING_CANDS:
    T = V = 0; ex = []
    for s_ in corpus:
        edits = 0; receipt_ok = False; gated = False; viol = False
        for cl in s_["calls"]:
            if cl["tool"] in EDIT: edits += 1; receipt_ok = False; continue
            cmd = cl["cmd"]
            if not cmd: continue
            last = rh.last_segment(rh.shell_only(cmd)) if hasattr(rh, "last_segment") else cmd
            if re.search(c["required_rx"], last) and "|" not in last: receipt_ok = True; edits = 0   # exit status unknown offline: count an unpiped run as the receipt
            if re.search(c["gated_rx"], cmd):
                gated = True
                if edits >= c["min_edits"] and not receipt_ok:
                    viol = True
                    if len(ex) < 3: ex.append(sample(s_, cmd))
        if gated: T += 1; V += viol
    print(f"  {c['title']:28s} ORDERING: push-after-edits in {T}/{M} sessions; no unpiped test run since the last edit in {V}  (violation rate {V}/{T})")
    for e in ex[:2]: print(f"     e.g. [{e['host']}/{e['repo']} {e['session']}] {e['text']}")
    proposals.append({"lane": "rule", "title": c["title"], "predicate": {"ordering": {"required_command_rx": c["required_rx"], "gated_command_rx": c["gated_rx"], "armed_by_events": ["edit"], "min_edits": c["min_edits"], "display_name": "test suite"}}, "sessions": {"gated": T, "violations": V}, "sessions_total": M, "rate": f"{V}/{T}", "samples": ex, "source_ref": f"sessions@{datetime.date.today()}#{c['title']}|push-after-edits {T}/{M}|violations {V}", "action": "create_rule(delivery=agent_hook, ordering=predicate) — replaces a same-subject matcher rule if one exists"})

# ---------------------------------------------------------------- lane 2: skills
installed = set()
for pat in ("~/.claude/plugins/cache/*/*/*/skills/*/SKILL.md", "~/.claude/plugins/*/plugins/*/skills/*/SKILL.md", "~/.claude/skills/*/SKILL.md", ".claude/skills/*/SKILL.md", "~/.codex/skills/*/SKILL.md", "~/.cursor/skills/*/SKILL.md"):
    installed |= {os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.expanduser(pat))}
if args.skills_file:
    try: installed |= {x["name"] for x in json.load(open(args.skills_file)).get("skills", [])}
    except Exception as e: print(f"[warn] --skills-file unreadable: {e}", file=sys.stderr)
SKILL_INTENTS = [   # intent in a USER turn; `skill` = the name that would serve it (checked against installed)
 {"skill": "pr-babysit",     "intent_rx": r"babysit|watch (the |this )?pr|drive .* to green|until (it'?s )?green"},
 {"skill": "handoff-session","intent_rx": r"hand ?off|hand (this|it) (off|over) to"},
 {"skill": "search-memory",  "intent_rx": r"what do we know|did we decide|search (memhub|memory|the brain)|check (the )?(agent|repo) brain"},
 {"skill": "rules-from-sessions", "intent_rx": r"mine (our|the|past) sessions|propose (rules|skills)|derive rules|backtest"},
]
def invoked(s, name): return any(f"skills/{name}" in u or f"/{name}" in u for u in s["users"])
print(f"\n=== LANE 2 · SKILLS — intent in user turns vs. skill invoked (installed skills found: {len(installed)})")
for c in SKILL_INTENTS:
    intent = [s for s in corpus if any(re.search(c["intent_rx"], u.lower()[:600]) for u in s["users"])]
    inv = [s for s in intent if invoked(s, c["skill"])]
    exists = c["skill"] in installed
    n, k = len(intent), len(inv)
    verdict = ("adoption gap" if exists and n > k else "covered" if exists else "PROPOSE skill") if n else "no signal"
    print(f"  {c['skill']:16s} intent {n}/{M} sessions {dict(collections.Counter(s['host'] for s in intent))}  invoked {k}  exists={exists}  -> {verdict}")
    ex = [sample(s, next(u for u in s["users"] if re.search(c["intent_rx"], u.lower()[:600]))) for s in intent[:3]]
    proposals.append({"lane": "skill", "title": c["skill"], "predicate": c["intent_rx"], "sessions": {"intent": n, "invoked": k}, "sessions_total": M, "exists": exists, "verdict": verdict, "samples": ex, "source_ref": f"sessions@{datetime.date.today()}#{c['skill']}|intent {n}/{M}|invoked {k}", "action": "create_skill (host-agnostic SKILL.md) into the repo brain" if not exists else "improve trigger wording / surface the existing skill"})

# ---------------------------------------------------------------- lane 3: hooks (trigger -> later outcome, same session)
HOOK_CANDS = [   # trigger -> outcome (-> optional repair): all later in the SAME session; repair makes "bad" mean "had to be undone"
 {"title": "pytest-piped-then-push-then-repair", "trigger_rx": r"(pytest|npm\s+test)\b[^\n|&;]*\|", "outcome_rx": r"git\s+push\b", "repair_rx": r"git\s+(revert|commit\s+--amend|push\s+--force)|--force-with-lease|\bfix(es|ed)?\b.*\btest", "block_msg": "test output piped — exit code lost; run the suite unpiped before pushing"},
 {"title": "sed-range-delete-then-revert", "trigger_rx": r"sed\s+-i\b[^\n]*\d+(,\d+)?d\b", "outcome_rx": r"git\s+(checkout|restore|reset)\b", "block_msg": "sed range-delete — use an anchored Edit"},
 {"title": "merge-then-user-pushback", "trigger_rx": r"gh\s+pr\s+merge\b", "outcome_rx": None, "user_rx": r"did (u|you) (just )?merge|why did (u|you) merge", "block_msg": "ask before merging"},
]
print("\n=== LANE 3 · HOOKS — trigger followed by a bad outcome in the same session")
for c in HOOK_CANDS:
    T = B = 0; ex = []
    for s in corpus:
        cmds = [(cl["n"], cl["cmd"]) for cl in s["calls"] if cl["cmd"]]
        ti = next((n for n, cmd in cmds if re.search(c["trigger_rx"], cmd)), None)
        if ti is None: continue
        T += 1
        if c.get("outcome_rx"):
            oi = next((n for n, cmd in cmds if n > ti and re.search(c["outcome_rx"], cmd)), None)
            bad = oi is not None and (not c.get("repair_rx") or any(re.search(c["repair_rx"], cmd) for n, cmd in cmds if n > oi))
        else:
            bad = any(re.search(c["user_rx"], u.lower()) for u in s["users"])
        if bad:
            B += 1
            if len(ex) < 3: ex.append(sample(s, next(cmd for n, cmd in cmds if n == ti)))
    rate = f"{B}/{T}" if T else "0/0"
    print(f"  {c['title']:30s} trigger in {T}/{M} sessions; bad outcome followed in {B}  (rate {rate})")
    for e in ex[:2]: print(f"     e.g. [{e['host']}/{e['repo']} {e['session']}] {e['text']}")
    snippet = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"cmd=$(jq -r .tool_input.command); echo \"$cmd\" | grep -Eq '{c['trigger_rx'].replace(chr(92)+'s','[[:space:]]').replace(chr(92)+'b','')}' && {{ echo '{c['block_msg']}' >&2; exit 2; }}; exit 0"}]}]}}
    proposals.append({"lane": "hook", "title": c["title"], "predicate": {"trigger_rx": c["trigger_rx"], "outcome_rx": c.get("outcome_rx") or c.get("user_rx"), "repair_rx": c.get("repair_rx")}, "sessions": {"trigger": T, "bad_outcome": B}, "sessions_total": M, "rate": rate, "samples": ex, "source_ref": f"sessions@{datetime.date.today()}#{c['title']}|trigger {T}/{M}|bad-outcome {B}", "action": "advise: rulebook rule; block: PreToolUse hook (settings snippet) or plugin PR", "settings_snippet": snippet})

json.dump([{k: v for k, v in s.items() if k != "results"} for s in corpus], open(os.path.join(args.out, "corpus.json"), "w"))
json.dump(proposals + proposals_seed, open(os.path.join(args.out, "proposals.json"), "w"), indent=1)
print(f"\nwrote {args.out}/corpus.json and {args.out}/proposals.json ({len(proposals)} candidates) in {time.time()-t0:.0f}s")
