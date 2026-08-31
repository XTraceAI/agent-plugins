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
import sys, os, json, re, glob, collections, importlib.util, time, argparse, datetime, shlex

ap = argparse.ArgumentParser()
ap.add_argument("--out", default="mine-out")
ap.add_argument("--baseline-date", help="friction before vs after this date (rule activation day)")
ap.add_argument("--skills-file", help="memhub list_skills JSON reply, for skill-lane dedup")
ap.add_argument("--repo", help="only sessions whose cwd basename matches")
ap.add_argument("--claude-md", action="append", default=[], help="CLAUDE.md (repeatable): its imperative sentences become the declared-rule seed")
ap.add_argument("--rule-file", action="append", default=[], help="a create_rule body (matcher / ordering / anchors) to backtest as a candidate (repeatable) — used by create-rule")
ap.add_argument("--candidates", action="append", default=[], help="a JSON LIST of create_rule bodies (repeatable) — the checks you derived from CLAUDE.md in step 2; each may carry `claude_md: {heading, text}` (its origin sentence), `did`, `what`, `quote_rx`, `source_ref`")
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
    proposals_seed = [{"lane": "claude_md", "title": d["heading"][:60] or "claude.md", "text": d["text"], "file": d["file"], "action": "give it a check (matcher / ordering / anchors) in a --candidates list and re-run; the replay says how often the declared rule is actually broken"} for d in declared]
else: proposals_seed = []

# ---------------------------------------------------------------- proposals: one row each, grouped by HOW the rulebook would fire it
# The rulebook has four ways to deliver a rule. Every proposal lands in exactly one, and the
# replay tells you what each would have done across the sessions on this machine.
TRIGGERS = {   # key -> (plain phrase shown to the user, rulebook `delivery`, one-line meaning)
 "session_start": ("shown at session start", "session_context", "a sentence Claude sees once when a session opens — for behaviour that has no command shape"),
 "before_action": ("fires at the command", "agent_hook", "Claude is warned at the command or edit, before it runs"),
 "after_error":   ("fires on the error", "agent_hook", "Claude is told what to do when this error shows up in a tool result"),
 "on_identifier": ("fires when the name comes up", "anchor_recall", "the server matches identifiers and judges relevance — cannot be replayed offline"),
}
EDIT = ("Edit", "Write", "MultiEdit", "NotebookEdit")
NOT_MENTION = r"python3?\s+-c\b|\brulebook\b"   # exempt the tooling that merely mentions a trigger
# Every candidate carries the two things a user needs besides the count: `what` (what changes with the rule on) and
# `claude_md_rx` (how to find the CLAUDE.md sentence that already declares it, if any). `requires_prior_rx` turns a
# "do X before Y" matcher into a genuine-miss count: fired with NO earlier X in that session.
RULE_CANDS = [
 {"title": "no-sed-range-delete", "matcher": {"event": "bash", "command_rx": r"sed\s+-i\b[^\n]*'?\s*\d+(,\d+)?d\b", "command_not_rx": NOT_MENTION, "warn_once_per": "session"},
  "did": "Claude deleted a line range with `sed -i`", "what": "Claude is warned at the sed range-delete and pointed to an anchored edit", "claude_md_rx": r"sed\s+-i", "quote_rx": r"\bsed\b|deleted|lines"},
 {"title": "no-pr-merge", "matcher": {"event": "bash", "command_rx": r"gh\s+pr\s+merge\s+\d+", "command_not_rx": NOT_MENTION + r"|claude\s+-p|--help", "warn_once_per": "session"},
  "did": "Claude merged a PR on its own", "what": "Claude is warned at `gh pr merge` that humans merge", "claude_md_rx": r"humans? merge|drives? merges|(never|don'?t) merge (a |the )?PR|leave the PR open", "quote_rx": r"\bmerged?\b"},
 {"title": "no-force-push", "matcher": {"event": "bash", "command_rx": r"(^|[;&|(]\s*)git\s+push\b[^\n|]*(\s--force\b|\s-f\b)", "command_not_rx": r"--force-with-lease|" + NOT_MENTION, "warn_once_per": "session"},
  "did": "Claude force-pushed", "what": "Claude is warned at the force-push; `--force-with-lease` on its own branch stays allowed", "claude_md_rx": r"force[- ]push|--force\b", "quote_rx": r"force[- ]?push|--force"},
 {"title": "no-stash-in-worktree", "matcher": {"event": "bash", "command_rx": r"(^|[;&|(]\s*)git\s+stash\b", "command_not_rx": r"stash\s+list|" + NOT_MENTION, "warn_once_per": "session"},
  "did": "Claude ran `git stash` (in a worktree that pushes onto the shared stash stack)", "what": "Claude is warned at `git stash` and pointed to `git diff origin/<base>` instead", "claude_md_rx": r"\bstash\b", "quote_rx": r"\bstash\b|worktree"},
]
OUTPUT_CANDS = [   # fires on the error: a signature in the tool result. Anchor to the line start — prose that MENTIONS the error is the main false hit.
 {"title": "missing-module-fresh-venv", "content_rx": r"^ModuleNotFoundError: No module named '[^']+'\s*$|^ImportError while loading conftest|^sqlalchemy\.exc\.MissingGreenlet",
  "did": "Claude hit a missing-module import (wrong venv or extras not installed)", "what": "Claude is told to run inside the repo venv (`uv run`) and install the extras before debugging the import", "claude_md_rx": r"uv pip install -e|install (the )?(dev )?extras|wrong venv", "quote_rx": r"\bvenv\b|ModuleNotFound|missing module|uv run"},
 {"title": "timeout-not-on-macos", "content_rx": r"^(\(eval\)|zsh|bash|sh)(:\d+)?: command not found: timeout\s*$|^timeout: command not found",
  "did": "Claude called GNU `timeout`, which isn't installed on macOS", "what": "Claude is told to use a Python-side timeout instead of the missing binary", "claude_md_rx": r"\btimeout\b", "quote_rx": r"timeout"},
 {"title": "kwarg-signature-mismatch", "content_rx": r"TypeError: [^\n]*unexpected keyword argument",
  "did": "Claude called a function with a keyword it no longer accepts", "what": "Claude is told to re-read the signature in-file before retrying", "claude_md_rx": r"signature mismatch|re-read its signature|kwarg names", "quote_rx": r"signature|kwarg|argument"},
]
ORDERING_CANDS = [   # "X must have run (green) before Y". armed_by "edit": the engine's own semantics (edits arm, an unpiped last-segment X discharges).
 {"title": "tests-before-push", "required_rx": r"\bpytest\b|npm\s+test|run_all\.py", "gated_rx": r"git\s+push\b", "min_edits": 1, "armed_by": "edit",
  "did": "Claude pushed without a passing test run after its edits", "what": "Claude is warned at `git push` if no passing, unpiped test run followed its edits", "claude_md_rx": r"pre-push audit|before every push|before pushing|run the full test suite", "quote_rx": r"(so many|why)[^.]{0,30}(errors|bugs)|tests? (fail|broke|didn)|\bbroke\b|run the tests"},
 # armed_by "session": armed from the first call; X anywhere in a chain counts. The shipped engine cannot run this yet (it is only
 # edit-armed) — the row reports what the mode WOULD do, so the plugin change has its evidence.
 {"title": "fetch-before-origin-read", "required_rx": r"git\s+(fetch|pull)\b", "gated_rx": r"git\s+(log|diff|show|branch|merge-base|rev-list)\b[^\n]*\borigin/", "armed_by": "session",
  "did": "Claude read `origin/*` without a `git fetch` earlier in the session", "what": "Claude is warned at the `origin/*` read if no fetch ran this session", "claude_md_rx": r"git fetch|fetch (origin|first)|fetch before", "quote_rx": r"origin/|latest (origin|main|staging)|remote branch|\bstale\b|fetch first|get latest|pull (from )?origin"},
]
ANCHOR_CANDS = []   # rows that fire when a name comes up: --rule-file bodies carrying `anchors`; nothing is replayed for them
bodies = []
for path in args.rule_file:   # one create_rule body per file (create-rule's backtest)
    try: bodies.append((path, json.load(open(os.path.expanduser(path)))))
    except Exception as e: print(f"[warn] --rule-file {path}: {e}", file=sys.stderr)
for path in args.candidates:   # a JSON list of bodies (the CLAUDE.md checks you derived in step 2)
    try:
        lst = json.load(open(os.path.expanduser(path)))
        if not isinstance(lst, list): raise ValueError("expected a JSON list of create_rule bodies")
        bodies += [(f"{path}[{i}]", b) for i, b in enumerate(lst) if isinstance(b, dict)]
    except Exception as e: print(f"[warn] --candidates {path}: {e}", file=sys.stderr)
for path, body in bodies:   # each joins the trigger it belongs to
    m = body.get("matcher") or {}
    cm = body.get("claude_md") if isinstance(body.get("claude_md"), dict) and str(body["claude_md"].get("text", "")).strip() else None   # the origin sentence, when the author knows it; an empty dict is no origin
    extra = {"did": body.get("did") or "Claude did this", "what": body.get("what") or (body.get("statement") or "").split(" Why:")[0][:160] or "Claude is warned at the matching command or edit",
             "claude_md_rx": body.get("claude_md_rx"), "quote_rx": body.get("quote_rx"), "claude_md_given": cm, "source_ref": body.get("source_ref")}
    if body.get("ordering"):
        o = body["ordering"] if isinstance(body["ordering"], dict) else {}
        if not (o.get("required_command_rx") and o.get("gated_command_rx")):   # a partial draft must not take the whole run down
            print(f"[warn] --rule-file {path}: ordering needs required_command_rx and gated_command_rx — skipped", file=sys.stderr); continue
        try: min_edits = int(o.get("min_edits", 1))
        except (TypeError, ValueError): min_edits = 1
        armed = "session" if "session" in (o.get("armed_by_events") or []) else "edit"
        ORDERING_CANDS.append({"title": body.get("title", path), "required_rx": o["required_command_rx"], "gated_rx": o["gated_command_rx"], "min_edits": min_edits, "armed_by": armed, **extra})   # noqa: E501 — one row per candidate
    elif isinstance(body.get("anchors"), list) and body["anchors"]: ANCHOR_CANDS.append({"title": body.get("title", path), "anchors": body["anchors"], **extra})
    elif m.get("event") == "output": OUTPUT_CANDS.append({"title": body.get("title", path), "content_rx": m["content_rx"], **extra})
    elif m: RULE_CANDS.append({"title": body.get("title", path), "matcher": m, "requires_prior_rx": body.get("requires_prior_rx"), **extra})
def hook_rule(title, matcher):
    return rh.to_hook_rule({"rule_id": title, "title": title, "statement": "", "delivery": "agent_hook", "mode": "advise", "version": 1, "matcher": matcher, "scope_repos": [], "scope_paths": [], "scope_exclude_paths": []})
def replay(rule, requires_prior_rx=None):
    """fired sessions (by host + ids), calls, genuine misses (fired with NO earlier required command that session), samples"""
    calls = collections.Counter(); sess = collections.Counter(); ids = []; misses = 0; ex = []
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
                    fired = True; sess[s["host"]] += 1; ids.append(s["id"])
                    if requires_prior_rx: misses += (not prior)
                    if len(ex) < 3: ex.append(sample(s, cl["cmd"] or cl["path"]))
    return {"calls": dict(calls), "fired": dict(sess), "fired_n": sum(sess.values()), "fired_ids": ids, "real_misses": (misses if requires_prior_rx else None), "samples": ex}

# ---- what it cost: corrections, reverts and facet friction in the sessions a rule would have fired in
_corr_by = {}
for s in corpus:
    c = [u.strip().replace("\n", " ") for u in s["users"] if u.strip() and not PASTED.search(u.strip()) and len(u) < 1500 and CORRECTION.search(u.strip()[:200])]
    if c: _corr_by[s["id"]] = c
_rev = {s["id"] for s in corpus if any(c["cmd"] and REVERT.search(c["cmd"]) for c in s["calls"])}
_facet_by = {str(d.get("session_id", "")): d for d in facets if d.get("session_id")}
def facet_of(sid):
    if sid in _facet_by: return _facet_by[sid]
    hits = [v for k, v in _facet_by.items() if len(k) >= 6 and (sid.startswith(k) or k.startswith(sid))]
    return hits[0] if len(hits) == 1 else None   # an ambiguous prefix attaches nothing rather than the wrong session
def _safe_rx(rx, what):
    """--rule-file bodies carry their own regexes; a malformed one is reported once and ignored, never a crash."""
    if not rx: return None
    try: return re.compile(rx, re.I)
    except re.error as e: print(f"[warn] {what}: bad regex {rx!r} ({e}) — ignored", file=sys.stderr); return None
def evidence(ids, quote_rx=None):
    """Counts are per session (any topic — a correction there is not necessarily about this rule); quotes are only kept when they mention the rule's topic."""
    quotes = []; nc = nr = 0; fr = collections.Counter(); qrx = _safe_rx(quote_rx, "quote_rx")
    for i in ids:
        f = facet_of(i); q = list((f or {}).get("corrections") or []) + list(_corr_by.get(i) or [])
        if q: nc += 1
        on_topic = [x for x in q if qrx and qrx.search(str(x))]
        if on_topic: quotes.append({"session": i[:8], "text": str(on_topic[0])[:110]})
        if i in _rev: nr += 1
        for k, v in ((f or {}).get("friction_counts") or {}).items(): fr[k] += v
    return {"corrected_in": nc, "reverted_in": nr, "quotes": quotes[:2], "friction": dict(fr.most_common(3))}
def declared_for(rx, title):
    """The CLAUDE.md sentence that already says this, if any — matched by the candidate's own `claude_md_rx`, never by guesswork."""
    crx = _safe_rx(rx, "claude_md_rx")
    hits = [d for d in declared if crx and crx.search(d["text"])]   # explicit regex only — a loose match names the wrong sentence as the origin
    return hits[0] if hits else None

def verdict(row):
    """One decision a user can act on. Thresholds are what the team measured: <3 sessions is noise, <50 % genuine is a nag."""
    n = row["fired_n"]
    if row["trigger"] == "on_identifier": return "Unmeasured — anchor rules are matched and judged on the server; turn on only with a stated reason"
    if row.get("needs_engine"):   # before the declared checks: a session-armed ordering must never be filed as a plain rule the engine cannot run
        return f"Turn on as a session-start note — firing at the command needs a plugin change (session-armed ordering); it would then catch {n} sessions" + (" (declared in CLAUDE.md, not broken here)" if row.get("claude_md") and n < 3 else "")
    if row.get("claude_md") and n == 0: return "Declared in CLAUDE.md, not broken here — 0 fires in these sessions; file it only if you want CLAUDE.md fully in the book"
    if row.get("claude_md") and n < 3: return f"Declared in CLAUDE.md, rarely broken here — {n} session{'s' if n != 1 else ''}; file it as a declared rule if you want it in the book"
    if n == 0: return "Skip — never would have fired in these sessions"
    if n < 3: return f"Skip — too rare ({n} session{'s' if n != 1 else ''}); revisit if one miss is expensive"
    if row.get("real_misses") is not None and row["real_misses"] < n / 2:
        return f"Turn on as a session-start note — at the command it would nag: {n - row['real_misses']} of {n} fires hit sessions that had already done it"
    return "Turn on"
def why_text(row):
    n = row["fired_n"]; d = row.get("claude_md")
    happened = row.get("did", "this happened")
    if row.get("real_misses") is not None and row["trigger"] != "session_start":
        happened += f" — {row['real_misses']} genuine ({n - row['real_misses']} had already done it)"
    if row.get("breakdown"): happened += f" ({row['breakdown']})"
    if d: return f'your CLAUDE.md says "{d["text"]}" (§ {d["heading"]}) — broken anyway in {n} of {M} sessions: {happened}'
    return f"not in CLAUDE.md. From your sessions: {happened} in {n} of {M} sessions"
def fmt_hosts(d): return ", ".join(f"{h} {n}" for h, n in sorted(d.items())) or "none"
today = datetime.date.today()
proposals = []
def add(row):
    row["delivery"] = TRIGGERS[row["trigger"]][1]; row["sessions_total"] = M
    row.setdefault("what", "Claude is warned at the matching command or edit")
    row["evidence"] = evidence(row.get("fired_ids", []), row.pop("quote_rx", None))
    given = row.pop("claude_md_given", None)
    d = given or declared_for(row.pop("claude_md_rx", None), row["title"])
    row["claude_md"] = {"heading": str(d.get("heading", "")), "text": str(d.get("text", "")).lstrip("# ").strip()[:140]} if d else None
    row["origin"] = "claude_md" if d else "sessions"
    row["verdict"] = verdict(row)
    n = row["fired_n"]
    row["bucket"] = ("unmeasured" if row["trigger"] == "on_identifier" else "declared_unbroken" if row.get("claude_md") and n < 3
                     else "note" if row["verdict"].startswith("Turn on as a session-start note") else "skip" if row["verdict"].startswith("Skip") else "on")
    # a session-armed ordering that is ALSO declared-and-unbroken is listed with the declared ones (its verdict still says the engine can't run it)
    if row["verdict"].startswith("Turn on as a session-start note"): row["trigger"] = "session_start"; row["delivery"] = "session_context"
    row["why"] = why_text(row)   # after the flip: a session-start note's reason must not describe command-level misses
    row["statement"] = f"{row['what']}. Why: {row['why']}"
    row["action"] = f"create_rule(delivery={row['delivery']}" + (", matcher/ordering=predicate)" if row["delivery"] == "agent_hook" else ", anchors=predicate)" if row["delivery"] == "anchor_recall" else ")")
    row.setdefault("source_ref", (f"claude_md@{today}#{row['title']}" if d else f"sessions@{today}#{row['title']}") + f"|applies {row['fired_n']}/{M}" + (f"|precision {row['real_misses']}/{row['fired_n']}" if row.get("real_misses") is not None else ""))
    proposals.append(row); return row
def show(row):
    n = row["fired_n"]; ev = row["evidence"]
    print(f"\n  {row['title']}   [{TRIGGERS[row['trigger']][0]}]")
    print(f"     Why: {row['why']}")
    if n:
        cost = f"of those sessions, {ev['corrected_in']} had you correcting Claude and {ev['reverted_in']} had a revert (any topic)"
        if ev["quotes"]: cost += " — on this topic you said: " + "; ".join(f'"{q["text"]}" ({q["session"]})' for q in ev["quotes"])
        if ev["friction"]: cost += f"; friction noted there: {', '.join(f'{k} ×{v}' for k, v in ev['friction'].items())}"
        print(f"     Cost: {cost}")
    print(f"     With it on: {row['what']}")
    print(f"     → {row['verdict']}")
    extra = f" · precision={row['real_misses']}/{n}" if row.get("real_misses") is not None else ""
    print(f"     evidence: applies-in {n}/{M} sessions ({fmt_hosts(row['fired'])}){extra}" + (f" · e.g. {row['samples'][0]['text'][:90]}" if row.get("samples") else ""))

# ---- rules already on: did they fire in your past sessions?
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
print(f"\n=== RULES ALREADY ON — {len(live)} hook rules in your book, replayed over your {M} past sessions (one that never fired is a retire candidate)")
for r in live:
    hr = rh.to_hook_rule(r); res = replay(hr) if hr and hr.get("on") in ("bash", "edit", "write_stdlib") else {"fired": {}, "calls": {}, "fired_n": 0}
    flag = "  <- never fired: retire candidate" if not res["fired"] else ""
    print(f"  {r['title']:28s} fired in {res['fired_n']} sessions ({fmt_hosts(res['fired'])}), {sum(res['calls'].values())} calls{flag}")
if not live: print("  (none)")

# ---- build every proposal first, then print the summary, then the rows by trigger
rows = []
for c in RULE_CANDS:
    hr = hook_rule(c["title"], c["matcher"])
    if not hr: print(f"  [warn] {c['title']}: matcher rejected by the hook (bad regex or shape) — fix before filing", file=sys.stderr); continue
    res = replay(hr, c.get("requires_prior_rx"))
    rows.append(add({"trigger": "before_action", "title": c["title"], "predicate": c["matcher"], "did": c.get("did"), "what": c.get("what"), "claude_md_rx": c.get("claude_md_rx"), "quote_rx": c.get("quote_rx"), "claude_md_given": c.get("claude_md_given"), **({"source_ref": c["source_ref"]} if c.get("source_ref") else {}), **res}))
def chain_receipt(shell, rx):
    """Session-armed receipt: the required command ran in ANY unpiped segment of an earlier call. Offline the exit status is
    unknown either way, so this is the same 'an unpiped run counts' assumption the edit-armed replay makes — just not last-segment-only,
    because in practice X sits first in a chain (`git fetch -q && git log origin/…`) in ~97 % of sessions."""
    return any(re.search(rx, seg) and "|" not in seg for seg in re.split(r"&&|\|\||;|\n", shell))
for c in ORDERING_CANDS:
    T = V = Vany = 0; ex = []; ids = []
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
        if viol: ids.append(s_["id"])
    pred = {"ordering": {"required_command_rx": c["required_rx"], "gated_command_rx": c["gated_rx"], "armed_by_events": [c["armed_by"]], "min_edits": c.get("min_edits", 1), "display_name": c["title"]}}
    breakdown = f"{Vany} never ran it, {V - Vany} ran it piped so its exit code was lost" if V else ""
    rows.append(add({"trigger": "before_action", "title": c["title"], "predicate": pred, "did": c.get("did"), "what": c.get("what"), "claude_md_rx": c.get("claude_md_rx"), "quote_rx": c.get("quote_rx"), "claude_md_given": c.get("claude_md_given"),
                     "fired": {"gated": T, "violations": V, "no_run_at_all": Vany}, "fired_n": V, "fired_ids": ids, "real_misses": None, "breakdown": breakdown,
                     "needs_engine": c["armed_by"] == "session", "samples": ex, "source_ref": c.get("source_ref") or f"{'claude_md' if (c.get('claude_md_given') or declared_for(c.get('claude_md_rx'), c['title'])) else 'sessions'}@{today}#{c['title']}|gated {T}/{M}|violations {V}"}))
for c in OUTPUT_CANDS:
    hit = collections.Counter(); ex = []; ids = []
    for s in corpus:
        t = next((t for t in s["results"] if re.search(c["content_rx"], t, re.M)), None)
        if t is not None:
            hit[s["host"]] += 1; ids.append(s["id"])
            if len(ex) < 3: ex.append(sample(s, re.search(c["content_rx"], t, re.M).group(0)))
    rows.append(add({"trigger": "after_error", "title": c["title"], "predicate": {"event": "output", "content_rx": c["content_rx"]}, "did": c.get("did"), "what": c.get("what"), "claude_md_rx": c.get("claude_md_rx"), "quote_rx": c.get("quote_rx"), "claude_md_given": c.get("claude_md_given"), **({"source_ref": c["source_ref"]} if c.get("source_ref") else {}),
                     "fired": dict(hit), "fired_n": sum(hit.values()), "fired_ids": ids, "real_misses": None, "samples": ex}))
for c in ANCHOR_CANDS:
    rows.append(add({"trigger": "on_identifier", "title": c["title"], "predicate": {"anchors": c["anchors"]}, "did": c.get("did"), "what": c.get("what"), "claude_md_rx": c.get("claude_md_rx"), "claude_md_given": c.get("claude_md_given"), **({"source_ref": c["source_ref"]} if c.get("source_ref") else {}), "fired": {}, "fired_n": 0, "fired_ids": [], "real_misses": None, "samples": []}))

on = [r for r in rows if r["bucket"] == "on"]        # fires at the command / on the error: catches it before it lands
notes = [r for r in rows if r["bucket"] == "note"]   # a note is read once; it does not catch anything at the moment
in_md = [r for r in on + notes if r.get("claude_md")]
corrected = len({sid for r in on + notes for sid in r.get("fired_ids", []) if (facet_of(sid) or {}).get("corrections") or sid in _corr_by})
print(f"\n=== WHAT THESE RULES WOULD HAVE CHANGED IN YOUR LAST {M} SESSIONS")
print(f"  {len(on)} rules would have caught a mistake before it happened — {sum(r['fired_n'] for r in on)} session-moments in total.")
if notes: print(f"  {len(notes)} more would be session-start notes (read once; they do not fire at the moment): {', '.join(r['title'] + ' (' + str(r['fired_n']) + ')' for r in notes)}.")
if declared:
    print(f"  {len(in_md)} of them are already written in your CLAUDE.md — and were still broken {sum(r['fired_n'] for r in in_md)} times. CLAUDE.md is read once; a rule fires at the command.")
    print(f"  {len(on) + len(notes) - len(in_md)} guard things CLAUDE.md never mentions.")
else: print("  (pass --claude-md to see which of these your CLAUDE.md already declares)")
print(f"  You corrected Claude afterwards in {corrected} of the sessions where they would have fired.")
skipped = [r for r in rows if r["bucket"] == "skip"]
if skipped: print(f"  Skipped as too rare or unseen: {', '.join(r['title'] + ' (' + str(r['fired_n']) + ')' for r in skipped)}.")
declared_unbroken = [r for r in rows if r["bucket"] == "declared_unbroken"]
if declared_unbroken: print(f"  Declared in CLAUDE.md but not (or barely) broken here: {len(declared_unbroken)} — listed at the end; file them only if you want CLAUDE.md fully in the book.")
NOTE_CAP = 5
if len(notes) > NOTE_CAP: print(f"  !! {len(notes)} session-start notes is more than the cap of {NOTE_CAP}. A note is what CLAUDE.md already is — give the rest a command, error, or identifier shape, or drop them.")
# coverage: of the friction the facets recorded, how much sits in a session at least one proposed rule would have fired in — and what is left over (the next candidates)
own = [d for d in facets if d.get("source") != "insights" and d.get("friction")]
if own:
    touched = {sid for r in on + notes for sid in r.get("fired_ids", [])}
    items = [(d, fr) for d in own for fr in d["friction"] if isinstance(fr, dict) and fr.get("category") in FRICTION_VOCAB]
    cov = [(d, fr) for d, fr in items if any(k in touched for k in (d.get("session_id"), *[t for t in touched if str(t).startswith(str(d.get("session_id"))[:12])]))]
    left = [(d, fr) for d, fr in items if (d, fr) not in cov]
    print(f"  Coverage: {len(cov)} of {len(items)} friction items in your facets sit in a session where one of these rules would have fired; {len(left)} do not.")
    if left:
        lc = collections.Counter(fr["category"] for _, fr in left)
        print(f"  Not covered, by kind: {', '.join(f'{k} ×{v}' for k, v in lc.most_common(5))} — the next candidates (give each a command / error / identifier shape, or accept it as a one-off):")
        for d, fr in left[:6]: print(f"     [{str(d.get('session_id',''))[:8]}] {fr.get('detail','')[:150]}")

print(f"\n=== PROPOSED RULES — each with why it exists, what it cost you, and what changes with it on")
for k, (head, deliv, meaning) in TRIGGERS.items():
    group = [r for r in rows if r["trigger"] == k]
    if not group and k in ("on_identifier",): continue
    print(f"\n--- {head}  ({deliv}: {meaning})")
    if k == "session_start":
        print("  Also here: the friction clusters from WHAT WENT WRONG that have no command shape (reasoning from a README, claiming 'done' without a live run, guessing which repo was meant) — you write those by hand, with the session count and the user's words as the reason.")
    for r in group:
        if r["bucket"] == "declared_unbroken": continue   # shown in their own section below
        show(r)
    if not group: print("  (none from the replay)")
if declared_unbroken:
    print(f"\n=== DECLARED IN CLAUDE.MD, NOT BROKEN HERE — {len(declared_unbroken)} checks with 0–2 fires in {M} sessions (a sentence in a file, not a problem in your sessions; file as declared rules only if you want CLAUDE.md fully in the book)")
    for r in declared_unbroken: print(f"  {r['title']:28s} {r['fired_n']} fires · CLAUDE.md § {r['claude_md']['heading'][:50]}: \"{r['claude_md']['text'][:90]}\"" + ("  (ordering: the engine cannot run it yet)" if r.get("needs_engine") else ""))

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
def to_ere(rx):
    """Python `re` -> the ERE `grep -E` runs, or None. Classes are translated; `\\b` is KEPT — both GNU grep and the
    macOS stock BSD grep (2.6.0-FreeBSD) honour it in -E mode, and dropping it would make `npm test\\b` match `npm testing`.
    A pattern with inline flags, lazy quantifiers or lookarounds has no faithful ERE form and must not become a
    silently-broken hook."""
    if re.search(r"\(\?|\*\?|\+\?|\\[AZzGpP]", rx): return None
    return rx.replace("\\s", "[[:space:]]").replace("\\S", "[^[:space:]]").replace("\\d", "[0-9]").replace("\\D", "[^0-9]") \
             .replace("\\w", "[[:alnum:]_]").replace("\\W", "[^[:alnum:]_]")
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
    ere = to_ere(c["trigger_rx"])   # Python re -> POSIX ERE for grep -E; None when the pattern has no faithful ERE form
    snippet = ({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": f"cmd=$(jq -r .tool_input.command); printf '%s' \"$cmd\" | grep -Eq {shlex.quote(ere)} && {{ echo {shlex.quote(c['block_msg'])} >&2; exit 2; }}; exit 0"}]}]}}   # every interpolated value is shell-quoted
               if ere else {"note": f"no PreToolUse snippet: the pattern {c['trigger_rx']!r} uses constructs POSIX ERE cannot express; file it as a rulebook rule instead"})
    if ere and "\\b" in ere: snippet["requires"] = "GNU or BSD grep (-E with \\b word boundaries) — the developer machine's grep, not busybox"
    proposals.append({"lane": "hook", "title": c["title"], "predicate": {"trigger_rx": c["trigger_rx"], "outcome_rx": c.get("outcome_rx") or c.get("user_rx"), "repair_rx": c.get("repair_rx")}, "sessions": {"trigger": T, "bad_outcome": B}, "sessions_total": M, "rate": rate, "samples": ex, "source_ref": f"sessions@{today}#{c['title']}|trigger {T}/{M}|bad-outcome {B}", "action": "advise: rulebook rule; block: PreToolUse hook (settings snippet) or plugin PR", "settings_snippet": snippet})

json.dump([{k: v for k, v in s.items() if k != "results"} for s in corpus], open(os.path.join(args.out, "corpus.json"), "w"))
json.dump(proposals + proposals_seed, open(os.path.join(args.out, "proposals.json"), "w"), indent=1)
print(f"\nwrote {args.out}/corpus.json and {args.out}/proposals.json ({len(proposals)} candidates) in {time.time()-t0:.0f}s")
