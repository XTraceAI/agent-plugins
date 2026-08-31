#!/usr/bin/env python3
"""Proposal miner — transcripts (Claude Code + Codex + Cursor) -> rule proposals grouped by
how the rulebook would fire them, each backtested over the same traces:

  At session start          session_context   no command shape, or a matcher that would nag
  Before a command or edit  agent_hook        matcher over the command / edit, or an ordering
  After an error            agent_hook        pattern over the tool result
  When a name comes up      anchor_recall     server-judged; listed, not replayed

Plus: skills users retype by hand, and block candidates (a command later undone / questioned).
Seeds: your facets.json (what went wrong per session), Claude Code /insights facets when present, CLAUDE.md via --claude-md.
Numbers per row: applies-in N/M sessions (by host), precision = real misses, samples, and a one-line verdict.
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
    print(f"\n=== WHAT WENT WRONG — {len(facets)} sessions with facets ({sum(1 for d in facets if d.get('source') != 'insights')} from your facets.json, {sum(1 for d in facets if d.get('source') == 'insights')} from /insights). Cluster the details into candidate sentences; a cluster with no command shape becomes a session-start note")
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
        print(f"\n=== DID FRICTION SHRINK? facet friction per session before vs after {bd}\n  before: {rate(before)}\n  after : {rate(after)}\n  (facets exist only for sessions /insights sampled — rerun /insights after new sessions accrue)")

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
    print(f"\n=== WHAT CLAUDE.MD DECLARES — {len(declared)} imperative sentences. Map each to a matcher / ordering and re-run: the replay says how often the declared rule is actually broken. A sentence with no checkable form is a session-start note")
    for d in declared[:60]: print(f"  [{os.path.basename(d['file'])} § {d['heading'][:40]}] {d['text'][:160]}")
    if len(declared) > 60: print(f"  … {len(declared)-60} more in proposals.json")
    proposals_seed = [{"lane": "claude_md", "title": d["heading"][:60] or "claude.md", "text": d["text"], "file": d["file"], "action": "map to a matcher (RULE_CANDS / ORDERING_CANDS) and re-run; or file via import-claude-md with the backtest number as source_ref suffix"} for d in declared]
else: proposals_seed = []

# ---------------------------------------------------------------- proposals: one row each, grouped by HOW the rulebook would fire it
# The rulebook has four ways to deliver a rule. Every proposal lands in exactly one, and the
# replay tells you what each would have done across the sessions on this machine.
TRIGGERS = {   # key -> (heading shown to the user, rulebook `delivery`, one-line meaning)
 "session_start": ("At session start", "session_context", "shown once when a session opens; for behaviour with no command shape, or where a matcher would mostly hit sessions that had already complied"),
 "before_action": ("Before a command or edit", "agent_hook", "PreToolUse: a pattern over the command / edit body, or an ordering ('green X after edits, before Y')"),
 "after_error":   ("After an error", "agent_hook", "PostToolUse: a pattern over the tool result"),
 "on_identifier": ("When a name comes up", "anchor_recall", "PreToolUse on a matching identifier; the server matches and judges relevance, so it cannot be replayed offline"),
}
EDIT = ("Edit", "Write", "MultiEdit", "NotebookEdit")
NOT_MENTION = r"python3?\s+-c\b|\brulebook\b"   # exempt the tooling that merely mentions a trigger
RULE_CANDS = [   # before_action matchers; `requires_prior_rx` turns "do X before Y" into a real-miss count (fired with no earlier X this session)
 {"title": "no-sed-range-delete", "matcher": {"event": "bash", "command_rx": r"sed\s+-i\b[^\n]*'?\s*\d+(,\d+)?d\b", "command_not_rx": NOT_MENTION, "warn_once_per": "session"}},
 {"title": "no-pr-merge", "matcher": {"event": "bash", "command_rx": r"gh\s+pr\s+merge\s+\d+", "command_not_rx": NOT_MENTION + r"|claude\s+-p|--help", "warn_once_per": "session"}},
 {"title": "no-force-push", "matcher": {"event": "bash", "command_rx": r"(^|[;&|(]\s*)git\s+push\b[^\n|]*(\s--force\b|\s-f\b)", "command_not_rx": r"--force-with-lease|" + NOT_MENTION, "warn_once_per": "session"}},
 {"title": "no-stash-in-worktree", "matcher": {"event": "bash", "command_rx": r"(^|[;&|(]\s*)git\s+stash\b", "command_not_rx": r"stash\s+list|" + NOT_MENTION, "warn_once_per": "session"}},
]
OUTPUT_CANDS = [   # after_error: a signature in the tool result. Anchor to the line start — prose that MENTIONS the error is the main false hit.
 {"title": "missing-module-fresh-venv", "content_rx": r"^ModuleNotFoundError: No module named '[^']+'\s*$|^ImportError while loading conftest|^sqlalchemy\.exc\.MissingGreenlet"},
 {"title": "timeout-not-on-macos", "content_rx": r"^(\(eval\)|zsh|bash|sh)(:\d+)?: command not found: timeout\s*$|^timeout: command not found"},
 {"title": "kwarg-signature-mismatch", "content_rx": r"TypeError: [^\n]*unexpected keyword argument"},
]
ORDERING_CANDS = [   # "X must have run (green) before Y". armed_by "edit": the engine's own semantics (edits arm, an unpiped last-segment X discharges).
 {"title": "tests-before-push", "required_rx": r"\bpytest\b|npm\s+test|run_all\.py", "gated_rx": r"git\s+push\b", "min_edits": 1, "armed_by": "edit"},
 # armed_by "session": armed from the first call; X anywhere in an `&&` chain counts (a chain that exits 0 proves X did). The shipped engine
 # cannot run this yet (it is only edit-armed) — the row reports what the mode WOULD do, so the plugin change has its evidence.
 {"title": "fetch-before-origin-read", "required_rx": r"git\s+(fetch|pull)\b", "gated_rx": r"git\s+(log|diff|show|branch|merge-base|rev-list)\b[^\n]*\borigin/", "armed_by": "session"},
]
ANCHOR_CANDS = []   # on_identifier rows come from --rule-file bodies that carry `anchors`; nothing is replayed for them
for path in args.rule_file:   # a candidate from create-rule / import-claude-md joins the trigger it belongs to
    try: body = json.load(open(os.path.expanduser(path)))
    except Exception as e: print(f"[warn] --rule-file {path}: {e}", file=sys.stderr); continue
    m = body.get("matcher") or {}
    if body.get("ordering"):
        o = body["ordering"] if isinstance(body["ordering"], dict) else {}
        if not (o.get("required_command_rx") and o.get("gated_command_rx")):   # a partial draft must not take the whole run down
            print(f"[warn] --rule-file {path}: ordering needs required_command_rx and gated_command_rx — skipped", file=sys.stderr); continue
        try: min_edits = int(o.get("min_edits", 1))
        except (TypeError, ValueError): min_edits = 1
        armed = "session" if "session" in (o.get("armed_by_events") or []) else "edit"
        ORDERING_CANDS.append({"title": body.get("title", path), "required_rx": o["required_command_rx"], "gated_rx": o["gated_command_rx"], "min_edits": min_edits, "armed_by": armed})
    elif isinstance(body.get("anchors"), list) and body["anchors"]: ANCHOR_CANDS.append({"title": body.get("title", path), "anchors": body["anchors"]})
    elif m.get("event") == "output": OUTPUT_CANDS.append({"title": body.get("title", path), "content_rx": m["content_rx"]})
    elif m: RULE_CANDS.append({"title": body.get("title", path), "matcher": m, "requires_prior_rx": body.get("requires_prior_rx")})
def hook_rule(title, matcher):
    return rh.to_hook_rule({"rule_id": title, "title": title, "statement": "", "delivery": "agent_hook", "mode": "advise", "version": 1, "matcher": matcher, "scope_repos": [], "scope_paths": [], "scope_exclude_paths": []})
def replay(rule, requires_prior_rx=None):
    """fired sessions (by host), calls, real misses (fired with NO earlier required command this session), samples"""
    calls = collections.Counter(); sess = collections.Counter(); misses = 0; ex = []
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
                    if requires_prior_rx: misses += (not prior)
                    if len(ex) < 3: ex.append(sample(s, cl["cmd"] or cl["path"]))
    return {"calls": dict(calls), "fired": dict(sess), "fired_n": sum(sess.values()), "real_misses": (misses if requires_prior_rx else None), "samples": ex}
def verdict(row):
    """One plain sentence a reviewer can act on. The thresholds are the ones the team measured: <3 sessions is noise, <50 % real misses is a nag."""
    n = row["fired_n"]
    if row["trigger"] == "on_identifier": return "unmeasured — anchor rules are matched and judged on the server; file only with a stated reason"
    if row.get("needs_engine"): return f"needs the session-armed ordering mode (plugin change) — until then this can only be a session note; the mode would fire on {n} real misses"
    if n == 0: return "no evidence — never would have fired here; skip unless it guards a known incident"
    if n < 3: return f"rare — {n} session(s); skip, or file only if one miss is expensive"
    if row.get("real_misses") is not None and row["real_misses"] < n / 2:
        return f"would nag — {n - row['real_misses']} of {n} fires hit sessions that had already done it; file as a session note (session_context) instead"
    return f"file — would have fired in {n} of {M} sessions" + (f", {row['real_misses']} of them real misses" if row.get("real_misses") is not None else "")
def fmt_hosts(d): return ", ".join(f"{h} {n}" for h, n in sorted(d.items())) or "none"
today = datetime.date.today()
proposals = []
def add(row):
    row["delivery"] = TRIGGERS[row["trigger"]][1]; row["sessions_total"] = M; row["verdict"] = verdict(row)
    row.setdefault("source_ref", f"sessions@{today}#{row['title']}|applies {row['fired_n']}/{M}" + (f"|precision {row['real_misses']}/{row['fired_n']}" if row.get("real_misses") is not None else ""))
    proposals.append(row); return row
def show(row):
    n = row["fired_n"]; extra = f"  precision={row['real_misses']}/{n} real misses" if row.get("real_misses") is not None else ""
    print(f"  {row['title']:28s} applies-in {n}/{M} sessions ({fmt_hosts(row['fired'])}){extra}\n     -> {row['verdict']}")
    for e in row["samples"][:2]: print(f"     e.g. [{e['host']}/{e['repo']} {e['session']}] {e['text']}")

# ---- rules already in the book, replayed
book = []
for f in glob.glob(os.path.expanduser("~/.config/memhub-plugin/rulebook/book/*.json")):
    try: book += json.load(open(f)).get("rules", [])
    except Exception: pass
seen = {}; live = []
for r in book:   # several cached books (one per repo) can hold different versions of one rule: keep the newest per title
    title = r.get("title") if isinstance(r, dict) else None
    if not title or r.get("delivery") != "agent_hook": continue   # a partial/corrupt cache row is skipped, not fatal
    try: ver = int(r.get("version") or 0)
    except (TypeError, ValueError): ver = 0
    if title in seen and seen[title] >= ver: continue
    seen[title] = ver; live = [x for x in live if x.get("title") != title] + [r]
print(f"\n=== RULES ALREADY IN THE BOOK — {len(live)} hook rules replayed over {M} sessions (a rule that never fired is a retire candidate)")
for r in live:
    hr = rh.to_hook_rule(r); res = replay(hr) if hr and hr.get("on") in ("bash", "edit", "write_stdlib") else {"fired": {}, "calls": {}, "fired_n": 0}
    flag = "  <- never fired: retire candidate" if not res["fired"] else ""
    print(f"  {r['title']:28s} fired in {res['fired_n']} sessions ({fmt_hosts(res['fired'])}), {sum(res['calls'].values())} calls{flag}")

# ---- proposals, by trigger
print(f"\n=== PROPOSED RULES — grouped by how the rulebook would fire them ({M} sessions replayed with the real hook)")
for k, (head, deliv, meaning) in TRIGGERS.items(): print(f"  {head:26s} delivery={deliv:16s} {meaning}")

print(f"\n--- {TRIGGERS['before_action'][0]} (delivery=agent_hook; matcher over the command / edit, or an ordering)")
for c in RULE_CANDS:
    hr = hook_rule(c["title"], c["matcher"])
    if not hr: print(f"  {c['title']:28s} [warn] matcher rejected by the hook (bad regex or shape) — fix before filing"); continue
    res = replay(hr, c.get("requires_prior_rx"))
    row = add({"trigger": "before_action", "title": c["title"], "predicate": c["matcher"], **res, "action": "create_rule(delivery=agent_hook, matcher=predicate) — run rulebook_verify.py first"})
    if row.get("real_misses") is not None and row["fired_n"] >= 3 and row["real_misses"] < row["fired_n"] / 2: row["trigger"] = "session_start"; row["delivery"] = "session_context"
    show(row)
def chain_receipt(shell, rx):
    """Session-armed receipt: the required command ran in ANY unpiped segment of an earlier call. Offline the exit status is
    unknown either way, so this is the same 'an unpiped run counts' assumption the edit-armed replay makes — just not last-segment-only,
    because in practice X sits first in a chain (`git fetch -q && git log origin/…`) in ~97 % of sessions."""
    return any(re.search(rx, seg) and "|" not in seg for seg in re.split(r"&&|\|\||;|\n", shell))
for c in ORDERING_CANDS:
    T = V = Vany = 0; ex = []
    for s_ in corpus:
        edits = 0 if c["armed_by"] == "edit" else 1; receipt_ok = False; ran_any = False; gated = False; viol = False; violany = False
        for cl in s_["calls"]:
            if cl["tool"] in EDIT:
                if c["armed_by"] == "edit": edits += 1; receipt_ok = False; ran_any = False
                continue
            cmd = cl["cmd"]
            if not cmd: continue
            shell = rh.shell_only(cmd); last = rh.last_segment(shell) if hasattr(rh, "last_segment") else shell
            if re.search(c["required_rx"], shell): ran_any = True
            ok = (re.search(c["required_rx"], last) and "|" not in last) if c["armed_by"] == "edit" else chain_receipt(shell, c["required_rx"])
            if ok: receipt_ok = True; edits = 0   # exit status unknown offline: an unpiped run counts as the receipt
            if re.search(c["gated_rx"], shell) and not (c["armed_by"] == "session" and re.search(c["required_rx"], shell)):
                gated = True
                if edits >= c.get("min_edits", 1) and not receipt_ok:
                    viol = True
                    if len(ex) < 3: ex.append(sample(s_, cmd))
                if edits >= c.get("min_edits", 1) and not ran_any: violany = True
        if gated: T += 1; V += viol; Vany += violany
    pred = {"ordering": {"required_command_rx": c["required_rx"], "gated_command_rx": c["gated_rx"], "armed_by_events": [c["armed_by"]], "min_edits": c.get("min_edits", 1), "display_name": c["title"]}}
    row = add({"trigger": "before_action", "title": c["title"], "predicate": pred, "fired": {"gated": T, "violations": V, "no_run_at_all": Vany}, "fired_n": V, "real_misses": None,   # a piped run is a legitimate fire (exit code lost), so no nag demotion for orderings
               "needs_engine": c["armed_by"] == "session", "samples": ex, "source_ref": f"sessions@{today}#{c['title']}|gated {T}/{M}|violations {V}",
               "action": "create_rule(delivery=agent_hook, ordering=predicate)" + (" — inert until the plugin ships a session-armed ordering mode" if c["armed_by"] == "session" else "")})
    if c["armed_by"] == "edit": print(f"  {c['title']:28s} ORDERING: gate reached after edits in {T}/{M} sessions; {V} had no green unpiped run since the last edit ({Vany} ran nothing at all, {V - Vany} ran it piped/chained — exit code lost)")
    else: print(f"  {c['title']:28s} ORDERING (session-armed): gate reached in {T}/{M} sessions; {V} had no unpiped '{c['required_rx']}' earlier in the session ({Vany} ran it never, {V - Vany} only piped — exit code lost)")
    print(f"     -> {row['verdict']}")
    for e in ex[:2]: print(f"     e.g. [{e['host']}/{e['repo']} {e['session']}] {e['text']}")

print(f"\n--- {TRIGGERS['after_error'][0]} (delivery=agent_hook, matcher event=output)")
for c in OUTPUT_CANDS:
    hit = collections.Counter(); ex = []
    for s in corpus:
        t = next((t for t in s["results"] if re.search(c["content_rx"], t, re.M)), None)
        if t is not None:
            hit[s["host"]] += 1
            if len(ex) < 3: ex.append(sample(s, re.search(c["content_rx"], t, re.M).group(0)))
    show(add({"trigger": "after_error", "title": c["title"], "predicate": {"event": "output", "content_rx": c["content_rx"]}, "fired": dict(hit), "fired_n": sum(hit.values()), "real_misses": None, "samples": ex, "action": "create_rule(delivery=agent_hook, matcher=predicate)"}))

demoted = [p for p in proposals if p["trigger"] == "session_start"]
print(f"\n--- {TRIGGERS['session_start'][0]} (delivery=session_context)")
print("  Rows land here when a matcher exists but would nag (listed above with their numbers), or when the behaviour has no command shape at all —")
print("  those come from the friction clusters in the facets section (wrong_source, unverified_claim, misunderstood_request …) and you write them by hand.")
for p in demoted: print(f"  {p['title']:28s} moved here: {p['verdict']}")
if not demoted: print("  (no matcher was demoted this run)")

if ANCHOR_CANDS:
    print(f"\n--- {TRIGGERS['on_identifier'][0]} (delivery=anchor_recall) — not replayable offline")
    for c in ANCHOR_CANDS: show(add({"trigger": "on_identifier", "title": c["title"], "predicate": {"anchors": c["anchors"]}, "fired": {}, "fired_n": 0, "real_misses": None, "samples": [], "action": "create_rule(delivery=anchor_recall, anchors=…)"}))

# ---------------------------------------------------------------- skills your team keeps retyping
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
print(f"\n=== SKILLS — what users ask for in their own words vs. the skill they actually invoked ({len(installed)} skills installed)")
for c in SKILL_INTENTS:
    intent = [s for s in corpus if any(re.search(c["intent_rx"], u.lower()[:600]) for u in s["users"])]
    inv = [s for s in intent if invoked(s, c["skill"])]
    exists = c["skill"] in installed
    n, k = len(intent), len(inv)
    verdict_ = ("skill exists but was retyped by hand — make it easier to find" if exists and n > k else "covered" if exists else "PROPOSE this skill") if n else "no signal"
    print(f"  {c['skill']:20s} asked for in {n}/{M} sessions ({fmt_hosts(collections.Counter(s['host'] for s in intent))}), invoked in {k}  -> {verdict_}")
    ex = [sample(s, next(u for u in s["users"] if re.search(c["intent_rx"], u.lower()[:600]))) for s in intent[:3]]
    proposals.append({"lane": "skill", "title": c["skill"], "predicate": c["intent_rx"], "sessions": {"intent": n, "invoked": k}, "sessions_total": M, "exists": exists, "verdict": verdict_, "samples": ex, "source_ref": f"sessions@{today}#{c['skill']}|intent {n}/{M}|invoked {k}", "action": "create_skill (host-agnostic SKILL.md) into the repo brain" if not exists else "improve trigger wording / surface the existing skill"})

# ---------------------------------------------------------------- block candidates: a command that was later undone or questioned
HOOK_CANDS = [   # trigger -> outcome (-> optional repair): all later in the SAME session; repair makes "bad" mean "had to be undone"
 {"title": "pytest-piped-then-push-then-repair", "trigger_rx": r"(pytest|npm\s+test)\b[^\n|&;]*\|", "outcome_rx": r"git\s+push\b", "repair_rx": r"git\s+(revert|commit\s+--amend|push\s+--force)|--force-with-lease|\bfix(es|ed)?\b.*\btest", "block_msg": "test output piped — exit code lost; run the suite unpiped before pushing"},
 {"title": "sed-range-delete-then-revert", "trigger_rx": r"sed\s+-i\b[^\n]*\d+(,\d+)?d\b", "outcome_rx": r"git\s+(checkout|restore|reset)\b", "block_msg": "sed range-delete — use an anchored Edit"},
 {"title": "merge-then-user-pushback", "trigger_rx": r"gh\s+pr\s+merge\b", "outcome_rx": None, "user_rx": r"did (u|you) (just )?merge|why did (u|you) merge", "block_msg": "ask before merging"},
]
print("\n=== BLOCK CANDIDATES — a command that was later undone, or that the user questioned, in the same session (advise = a rule above; block = a PreToolUse hook)")
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
    print(f"  {c['title']:34s} happened in {T}/{M} sessions; went bad afterwards in {B}  (rate {rate})")
    for e in ex[:2]: print(f"     e.g. [{e['host']}/{e['repo']} {e['session']}] {e['text']}")
    snippet = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"cmd=$(jq -r .tool_input.command); echo \"$cmd\" | grep -Eq '{c['trigger_rx'].replace(chr(92)+'s','[[:space:]]').replace(chr(92)+'b','')}' && {{ echo '{c['block_msg']}' >&2; exit 2; }}; exit 0"}]}]}}
    proposals.append({"lane": "hook", "title": c["title"], "predicate": {"trigger_rx": c["trigger_rx"], "outcome_rx": c.get("outcome_rx") or c.get("user_rx"), "repair_rx": c.get("repair_rx")}, "sessions": {"trigger": T, "bad_outcome": B}, "sessions_total": M, "rate": rate, "samples": ex, "source_ref": f"sessions@{today}#{c['title']}|trigger {T}/{M}|bad-outcome {B}", "action": "advise: rulebook rule; block: PreToolUse hook (settings snippet) or plugin PR", "settings_snippet": snippet})

json.dump([{k: v for k, v in s.items() if k != "results"} for s in corpus], open(os.path.join(args.out, "corpus.json"), "w"))
json.dump(proposals + proposals_seed, open(os.path.join(args.out, "proposals.json"), "w"), indent=1)
print(f"\nwrote {args.out}/corpus.json and {args.out}/proposals.json ({len(proposals)} candidates) in {time.time()-t0:.0f}s")
