#!/usr/bin/env python3
"""S0's scorecard: measure the extraction pipeline, do not claim it works.

Three modes, three of the scorecard's rows:

  gold    the judge over the 140 hand-labelled moments in
          `fixtures/clf_set.jsonl` / `clf_gold.json`. Reports precision,
          recall, F1 and the regex baseline on the same items.
          **Precision is not the target and must not be tuned for** — a false
          positive costs one author call, a false negative is a lesson that
          never exists. Precision is measured at the END of the pipeline, by
          the activate-ratio row, not here.
          Since S1 the judge runs on the server behind
          `POST …/harness/classify`, one call per moment.

  router  the router alone over a corpus, no model calls, no cost. Reports how
          often each regex fires and — once you have judged the corpus — what
          fraction of its hits produced a row a human would activate.

  judge   a second reader (fresh headless model, blind) over a run's rows,
          with agreement against the first judge's verdicts.

  The post-session miner and its `review --variant review|mine` measurement
  were removed with path B (the live agent mines in session now); they are
  at commit 1fa1e48 on the S1 branch if the numbers need re-running.

  corpus  router + redacted window + one classifier call per sent turn over a
          corpus directory of canonical turns JSON (what `staging_sessions.py
          corpus` writes). Records each session's flagged moments and reports
          signal rate, kinds, latency and transport errors.

  nudge   what the coding agent would propose from those moments, given the
          session up to the turn and the exact nudge line; writes
          `judge_sheet.md` — every row, numbered, for the hand judgement the
          gate actually turns on.

Nothing here activates a rule or writes to a server.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures as cf
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"


def plugin_scripts() -> Path:
    for cand in (os.environ.get("MEMHUB_PLUGIN_SCRIPTS"),
                 os.path.join(os.environ.get("CLAUDE_PLUGIN_ROOT", ""), "scripts"),
                 str(HERE.parent.parent.parent / "scripts")):
        if cand and Path(cand, "harness_extract.py").is_file():
            return Path(cand)
    sys.exit("harness_extract.py not found; set MEMHUB_PLUGIN_SCRIPTS")


def _load_extract():
    scripts = plugin_scripts()
    sys.path.insert(0, str(scripts))
    import harness_extract                                  # noqa: PLC0415
    return harness_extract


# ------------------------------------------------------------------- gold
def cmd_gold(args) -> None:
    hx = _load_extract()
    items = {}
    with (FIXTURES / "clf_set.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                items[row["id"]] = row
    gold = {int(k): v for k, v in
            json.loads((FIXTURES / "clf_gold.json").read_text())["pos"].items()}
    ids = sorted(items)[:args.limit] if args.limit else sorted(items)
    print(f"judging {len(ids)} moments (gold positives in range: "
          f"{sum(1 for i in ids if i in gold)})")

    def one(item_id):
        it = items[item_id]
        # The same window shape the live judge sees: previous actions, the
        # user's message, the error just before it.
        lines = []
        if it.get("prior_tools"):
            lines.append("AGENT'S ACTIONS IN PREVIOUS TURN (last 4):")
            lines += ["  - " + t for t in it["prior_tools"][-4:]]
        if it.get("prior_asst"):
            lines.append(f"AGENT'S LAST WORDS IN PREVIOUS TURN: {it['prior_asst'][-500:]}")
        lines.append(f"USER'S NEW MESSAGE: {it['user'][:900]}")
        if it.get("last_error"):
            lines.append(f"  ! error just before: {str(it['last_error'])[:250]}")
        reply, dt = hx.server_classify("\n".join(lines), timeout=args.timeout)
        reason = str(reply.get("reason") or "")
        if reason != "classified":
            return item_id, None, 0.0, reason
        verdict = {"signal": bool(reply.get("signal")), "kind": reply.get("kind") or "none"}
        return item_id, verdict, dt, ""

    out, errors, lat = {}, [], []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for n, (item_id, verdict, dt, err) in enumerate(
                pool.map(one, ids), 1):
            if verdict is None:
                errors.append((item_id, err))
            else:
                out[item_id] = verdict
                lat.append(dt)
            if n % 20 == 0:
                print(f"  ... {n}/{len(ids)}", flush=True)

    tp = fp = fn = tn = kind_ok = 0
    fps, fns = [], []
    for i in ids:
        if i not in out:
            continue
        predicted = bool(out[i].get("signal"))
        actual = i in gold
        if predicted and actual:
            tp += 1
            kind_ok += out[i].get("kind") == gold[i]
        elif predicted:
            fp += 1
            fps.append(i)
        elif actual:
            fn += 1
            fns.append(i)
        else:
            tn += 1
    P = tp / max(tp + fp, 1)
    Rc = tp / max(tp + fn, 1)
    F1 = 2 * P * Rc / max(P + Rc, 1e-9)

    # The regex baseline on the SAME items — `cand` is the miner's flag.
    ctp = sum(1 for i in ids if items[i].get("cand") and i in gold)
    cfp = sum(1 for i in ids if items[i].get("cand") and i not in gold)
    cfn = sum(1 for i in ids if not items[i].get("cand") and i in gold)

    print(f"\nscored {len(out)}/{len(ids)}  (model errors: {len(errors)})")
    print(f"judge   : TP {tp} FP {fp} FN {fn} TN {tn} | "
          f"precision {P:.2f} recall {Rc:.2f} F1 {F1:.2f} | "
          f"kind agreement {kind_ok}/{tp}")
    print(f"baseline: precision {ctp / max(ctp + cfp, 1):.2f} "
          f"recall {ctp / max(ctp + cfn, 1):.2f}   (regex only)")
    if lat:
        print(f"latency : median {statistics.median(lat):.1f}s "
              f"p90 {sorted(lat)[int(len(lat) * .9)]:.1f}s")
    if errors:
        print(f"errors  : {collections.Counter(e for _, e in errors).most_common(3)}")
    print(f"\nFALSE NEGATIVES ({len(fns)}) — each is a lesson that would never exist:")
    for i in fns:
        print(f"  #{i} gold={gold[i]} {items[i]['user'][:90]!r}")
    print(f"\nFALSE POSITIVES ({len(fps)}) — each costs one author call, usually refused:")
    for i in fps[:15]:
        print(f"  #{i} [{out[i].get('kind')}] {items[i]['user'][:90]!r}")

    if args.out:
        Path(args.out).write_text(json.dumps({
            "scored": len(out), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(P, 3), "recall": round(Rc, 3),
            "f1": round(F1, 3), "kind_agreement": kind_ok,
            "baseline_precision": round(ctp / max(ctp + cfp, 1), 3),
            "baseline_recall": round(ctp / max(ctp + cfn, 1), 3),
            "model_errors": len(errors),
            "false_negatives": fns, "false_positives": fps,
        }, indent=1), encoding="utf-8")
        print(f"\n-> {args.out}")


# ------------------------------------------------------------------ nudge
_TRACE_TURN = re.compile(r"^== turn (\d+) \|")
_TRACE_SERVER = re.compile(r"^   server \(([\d.]+)s\): (\S+)(?: — .*?)?(?: \| judge=(\S+))?$")


def moments_from_trace(hx, doc: dict, trace_path: Path, rows: list[dict]) -> list[dict]:
    """The classifier-flagged moments of a finished `corpus` run, rebuilt
    from its trace (a run made with `--moments` records them directly)."""
    kind_of = {r["source_ref"]: r.get("_kind") for r in rows}
    sent: dict[int, tuple[bool, str]] = {}
    turn_n = None
    for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _TRACE_TURN.match(line)
        if m:
            turn_n = int(m.group(1))
            continue
        if turn_n is None or turn_n in sent:
            continue
        m = _TRACE_SERVER.match(line)
        if m:
            sent[turn_n] = (hx.judge_said_signal(m.group(2)), m.group(3) or "")
        elif line.startswith(("   DRAFT (", "   server drafted, client refused", "   twin of")):
            sent[turn_n] = (True, kind_of.get(f"{doc.get('session')}#{turn_n}") or "")
    turns = doc.get("turns") or []
    session = doc.get("session") or ""
    out = []
    for i, turn in enumerate(turns):
        flagged = sent.get(turn.get("n"))
        if not flagged or not flagged[0]:
            continue
        prev = turns[i - 1] if i else None
        # A replayed session with no declared repo (NULL agentic_namespace on
        # the conversation row) would lose every row to `state_missing_repo`
        # — 131 server rows and 17 mined rows did in S1's runs. The rubric
        # judges the row, not the stamp; a live session always has a cwd. So
        # the measurement stamps `unresolved` and says so on the row.
        state = hx.stamp_state(session=session, turn=turn, row_engine_target=("", ""),
                               cwd="", hook_version=hx.plugin_version(), env_name="staging",
                               default_repo=doc.get("repo") or "unresolved")
        out.append({"turn": turn.get("n"), "source_ref": f"{session}#{turn.get('n')}",
                    "hint": hx.router_hint(hx.route(turn, prev)), "kind": flagged[1],
                    "state": state})
    return out


NUDGE_SCHEMA = {"type": "object", "properties": {
    "propose": {"type": "boolean"}, "why": {"type": "string"},
    "title": {"type": ["string", "null"]}, "statement": {"type": ["string", "null"]},
    "engine": {"type": ["string", "null"], "enum": ["matcher", "ordering", "anchors", None]},
    "matcher": {"type": ["object", "null"], "properties": {
        "event": {"type": ["string", "null"]}, "command_rx": {"type": ["string", "null"]},
        "command_not_rx": {"type": ["string", "null"]}, "path_rx": {"type": ["string", "null"]},
        "path_not_rx": {"type": ["string", "null"]}, "content_rx": {"type": ["string", "null"]}}},
    "ordering": {"type": ["object", "null"], "properties": {
        "required_command_rx": {"type": ["string", "null"]}, "gated_command_rx": {"type": ["string", "null"]},
        "armed_by_events": {"type": ["array", "null"], "items": {"type": "string"}},
        "display_name": {"type": ["string", "null"]}}},
    "anchors": {"type": ["array", "null"], "items": {"type": "string"}}},
    "required": ["propose", "why", "title", "statement", "engine", "matcher", "ordering", "anchors"]}

NUDGE_SYSTEM = """\
You are the coding agent inside the session shown. The transcript so far is \
yours: you did those actions and wrote those words. A harness line has just \
been injected at the user's next prompt (it is the last thing you see). Decide \
what you would do about it, and only that — do not do the user's new task. If \
you would propose a rule, fill the row exactly as the line asks: propose=true \
and the fields (the engine blocks you do not use are null). If you would say \
nothing, propose=false with a one-sentence why. Answer by calling the \
StructuredOutput tool; never ask a question here — if you would have asked the \
user first, still fill the row you would have proposed and say so in why."""

NUDGE_DIGEST_CHARS = 24000


def cmd_nudge(args) -> None:
    """Path A, minus the person: for every classifier-flagged moment of a
    finished corpus run, a headless agent gets the session UP TO that turn
    (its own context, as a digest) and the exact line the prompt lane would
    inject, and decides whether to propose. Its rows go through the same
    `build_row`, identity and twin checks the live path applies at
    `create_rule`, and land in `<run>/nudge/` with a judge sheet. Nothing is
    filed."""
    hx = _load_extract()
    sys.path.insert(0, str(plugin_scripts()))
    import harness_stop as hs                               # noqa: PLC0415
    run, corpus = Path(args.run), Path(args.corpus)
    out = run / "nudge"
    out.mkdir(parents=True, exist_ok=True)
    engineer_of = {}
    idx = corpus / "index.json"
    if idx.is_file():
        engineer_of = {s["file"]: s["engineer"] for s in json.loads(idx.read_text()).get("sessions", [])}

    jobs = []
    for doc_path in sorted(corpus.glob("*.json")):
        if doc_path.name == "index.json":
            continue
        name = doc_path.stem
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        recorded = run / f"{name}.moments.jsonl"
        trace = run / f"{name}.trace.log"
        if recorded.is_file():
            moments = hx.read_drafts(recorded)
        elif trace.is_file():
            # A run from before the author was removed recorded no moments;
            # rebuild them from its trace.
            drafts = run / f"{name}.drafts.jsonl"
            rows = hx.read_drafts(drafts) if drafts.is_file() else []
            moments = moments_from_trace(hx, doc, trace, rows)
        else:
            continue
        if args.per_session:
            moments = moments[:args.per_session]
        for m in moments:
            jobs.append((name, doc, m))
    if args.limit:
        jobs = jobs[:args.limit]
    print(f"{len(jobs)} moments across {len({j[0] for j in jobs})} sessions, {args.jobs} agents at a time")

    def digest_upto(doc, turn_n):
        lines = []
        for t in doc.get("turns") or []:
            if t.get("n") > turn_n:
                break
            acts = [a.get("brief", "") for a in t.get("tools", [])]
            shown = (acts[:3] + ["  …"] + acts[-3:]) if len(acts) > 6 else acts
            lines.append(f"[turn {t.get('n')}] USER: {(t.get('user') or '')[:600]}")
            lines += ["    " + a[:160] for a in shown]
            errs = [r for r in t.get("results", []) if r.get("error")][:2]
            lines += [f"    ! {(e.get('text') or '')[:200]}" for e in errs]
            lines.append(f"    AGENT: {(t.get('asst') or '')[-500:]}")
        text = "\n".join(lines)
        if len(text) > NUDGE_DIGEST_CHARS:
            text = "…\n" + text[-NUDGE_DIGEST_CHARS:]
        return hx.redact_window(text)

    def one(job):
        name, doc, m = job
        session = doc.get("session") or name
        repo = (m.get("state") or {}).get("repo") or ""
        user = (f"TRANSCRIPT SO FAR:\n{digest_upto(doc, m['turn'])}\n\n"
                f"[the user's next prompt arrives; injected with it:]\n{hs.nudge_line(session, m, repo)}")
        cmd = ["claude", "-p", "--model", args.model, "--safe-mode", "--output-format", "json",
               "--json-schema", json.dumps(NUDGE_SCHEMA), "--no-session-persistence",
               "--disallowedTools", "Bash", "Read", "Edit", "Write", "MultiEdit", "Agent",
               "Grep", "Glob", "WebSearch", "WebFetch", "--append-system-prompt", NUDGE_SYSTEM]
        t0 = time.time()
        err = ""
        try:
            proc = subprocess.run(cmd, input=user, capture_output=True, text=True,
                                  timeout=args.timeout, env=hx.child_env())
            env = json.loads(proc.stdout) if proc.returncode == 0 else {}
            got = env.get("structured_output") or {}
            if not got:
                err = f"exit {proc.returncode}: {(proc.stderr or proc.stdout)[-160:]}"
        except Exception as exc:                            # noqa: BLE001
            got, err = {}, repr(exc)[:200]
        rec = {"name": name, "session": session, "turn": m["turn"], "kind": m.get("kind"),
               "hint": m.get("hint"), "seconds": round(time.time() - t0, 1),
               "propose": bool(got.get("propose")), "why": (got.get("why") or "")[:300],
               "error": err, "engineer": engineer_of.get(f"{name}.json", name.split("__")[0])}
        if got.get("propose"):
            raw = {"title": got.get("title"), "statement": got.get("statement"), "engine": got.get("engine"),
                   "matcher": got.get("matcher"), "ordering": got.get("ordering"), "anchors": got.get("anchors"),
                   "derivable": False, "rationale": got.get("why")}
            if hx.pii_in_row(raw):
                rec["refused"] = "pii_in_row"
            else:
                row, why = hx.build_row(raw, state=dict(m.get("state") or {}), session=session,
                                        turn_n=m["turn"], reason=f"nudge:{m.get('kind') or 'judge'}",
                                        scope_repos=[repo] if repo else [])
                if row is None:
                    rec["refused"] = why
                else:
                    row["_kind"] = m.get("kind"); row["_engineer"] = rec["engineer"]
                    rec["row"] = row
        print(f"  {name} t{m['turn']} {m.get('kind') or '-'}: "
              f"{'PROPOSE' if rec['propose'] else ('ERROR' if err else 'silent')}"
              f"{' → ' + rec['refused'] if rec.get('refused') else ''} {rec['seconds']}s", flush=True)
        return rec

    t0 = time.time()
    recs = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for rec in pool.map(one, jobs):
            recs.append(rec)
    rows, per_session = [], collections.defaultdict(list)
    twins = 0
    for rec in recs:
        row = rec.get("row")
        if not row:
            continue
        if hx.is_twin(row, per_session[rec["session"]]):
            twins += 1; rec["refused"] = "twin_in_session"; rec.pop("row"); continue
        per_session[rec["session"]].append(row); rows.append(row)
    (out / "decisions.json").write_text(json.dumps(recs, indent=1, default=str), encoding="utf-8")
    (out / "rows.json").write_text(json.dumps(rows, indent=1, default=str), encoding="utf-8")
    sessions = {j[0] for j in jobs}
    summary = {
        "moments": len(recs), "sessions": len(sessions),
        "proposed": sum(1 for r in recs if r["propose"]),
        "silent": sum(1 for r in recs if not r["propose"] and not r["error"]),
        "errors": sum(1 for r in recs if r["error"]),
        "refused": dict(collections.Counter(r["refused"] for r in recs if r.get("refused")).most_common()),
        "rows": len(rows), "twins_in_session": twins,
        "rows_per_session_mean": round(len(rows) / max(len(sessions), 1), 2),
        "rows_per_session_max": max((len(v) for v in per_session.values()), default=0),
        "by_kind": {k: f"{sum(1 for r in recs if (r.get('kind') or '-') == k and r.get('row'))}/"
                       f"{sum(1 for r in recs if (r.get('kind') or '-') == k)}"
                    for k in sorted({r.get("kind") or "-" for r in recs})},
        "by_engine": dict(collections.Counter(
            next(k for k in ("matcher", "ordering", "anchors") if k in r) for r in rows)),
        "latency_p50_s": sorted(r["seconds"] for r in recs)[len(recs) // 2] if recs else None,
        "wall_s": round(time.time() - t0, 1),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    write_judge_sheet(out / "judge_sheet.md", rows,
                      {"sessions": len(sessions), "engineers": sorted({r["_engineer"] for r in rows})}, [], [])
    print("\n" + "=" * 62)
    for k, v in summary.items():
        print(f"{k:24} {v}")
    print(f"\n-> {out}/judge_sheet.md")


# ------------------------------------------------------------------ judge
JUDGE_RUBRIC = """\
You are the second judge of a set of drafted team rules. Judge each row AS A \
SET MEMBER, by five criteria; a row is ACTIVATABLE (verdict "A") only if all \
five hold, otherwise REJECT ("R") with one reason word.

1. It would change an action: a reader can name the next command, edit or \
read it would alter. Not a summary, not an observation. (reject: no_action)
2. The trigger can actually match: the regex or anchors would fire on the \
shape of a real future action and would NOT fire on every member of that \
family. Anchors must be identifiers or paths — a bare English word, a repo \
name, or an identifier present in every call recalls everywhere. A trigger \
that matches nothing and one that matches everything are the same reject. \
(reject: unmatchable)
3. It is not derivable: a new engineer would not learn it from the repo, its \
tests, its docs or CLAUDE.md, and it is not already a team rule. (reject: \
derivable)
4. It is not project state: not "PR #N does X", not "we decided Y for this \
ticket", not a restatement of an error message. (reject: project_state)
5. It outlives the session: still true next month; not tied to one line, one \
branch, one PR that will merge, one bug. (reject: one_off)
Also reject a row that duplicates another row in the set (reject: duplicate, \
keep the better-triggered one) or that states something false (reject: wrong).

Be strict: an activated rule fires in every teammate's sessions. Answer for \
every row by calling the StructuredOutput tool; never ask a question."""

JUDGE_SCHEMA = {"type": "object", "properties": {"verdicts": {"type": "array", "items": {
    "type": "object", "properties": {
        "index": {"type": "integer"}, "verdict": {"type": "string", "enum": ["A", "R"]},
        "reason": {"type": "string", "enum": ["", "no_action", "unmatchable", "derivable",
                                              "project_state", "one_off", "duplicate", "wrong"]},
        "why": {"type": "string"}},
    "required": ["index", "verdict", "reason", "why"]}}},
    "required": ["verdicts"]}


def cmd_judge(args) -> None:
    """A second judge over a run's rows: a fresh headless model with the
    rubric and nothing else — no session, no first judge's verdicts — so the
    pass is independent of whoever built the pipeline. Reports the score
    under each judge and their agreement. Not a human; the scorecard says so."""
    rows = json.loads(Path(args.rows).read_text(encoding="utf-8"))
    L = [f"{len(rows)} ROWS:"]
    for i, r in enumerate(rows, 1):
        engine = {k: r[k] for k in ("matcher", "ordering", "anchors") if k in r}
        L += [f"[{i}] {r.get('title', '')}", f"    statement: {r.get('statement', '')}",
              f"    trigger: {json.dumps(engine, ensure_ascii=False)}", ""]
    cmd = ["claude", "-p", "--model", args.model, "--safe-mode", "--output-format", "json",
           "--json-schema", json.dumps(JUDGE_SCHEMA), "--no-session-persistence",
           "--disallowedTools", "Bash", "Read", "Edit", "Write", "MultiEdit", "Agent",
           "Grep", "Glob", "WebSearch", "WebFetch", "--append-system-prompt", JUDGE_RUBRIC]
    hx = _load_extract()
    t0 = time.time()
    proc = subprocess.run(cmd, input="\n".join(L), capture_output=True, text=True,
                          timeout=args.timeout, env=hx.child_env())
    if proc.returncode != 0:
        sys.exit(f"judge failed: exit {proc.returncode}: {proc.stderr[-300:]}")
    env = json.loads(proc.stdout)
    verdicts = (env.get("structured_output") or {}).get("verdicts") or []
    got = {v["index"]: v for v in verdicts if isinstance(v, dict) and isinstance(v.get("index"), int)}
    missing = [i for i in range(1, len(rows) + 1) if i not in got]
    A2 = {i for i, v in got.items() if v.get("verdict") == "A"}
    out = {"model": args.model, "rows": len(rows), "judged": len(got), "missing": missing,
           "A": sorted(A2), "R": {}, "why": {str(i): v.get("why", "")[:200] for i, v in got.items()},
           "score": round(len(A2) / max(len(rows), 1), 3), "seconds": round(time.time() - t0, 1)}
    for i, v in got.items():
        if v.get("verdict") != "A":
            out["R"].setdefault(v.get("reason") or "unspecified", []).append(i)
    print(f"judge 2 ({args.model}): {len(A2)}/{len(rows)} activatable = {out['score']}"
          f" | rejects {({k: len(v) for k, v in out['R'].items()})} | {out['seconds']}s")
    if args.against:
        v1 = json.loads(Path(args.against).read_text(encoding="utf-8"))
        A1 = set(v1["A"])
        both = A1 & A2
        agree = len(both) + len(set(range(1, len(rows) + 1)) - A1 - A2)
        p_o = agree / len(rows)
        p1, p2 = len(A1) / len(rows), len(A2) / len(rows)
        p_e = p1 * p2 + (1 - p1) * (1 - p2)
        kappa = (p_o - p_e) / (1 - p_e) if p_e < 1 else 1.0
        out.update({"judge1_A": sorted(A1), "agreement": round(p_o, 3), "kappa": round(kappa, 3),
                    "both_A": sorted(both), "only_judge1": sorted(A1 - A2), "only_judge2": sorted(A2 - A1),
                    "score_judge1": round(len(A1) / len(rows), 3),
                    "score_both_agree": round(len(both) / len(rows), 3),
                    "score_either": round(len(A1 | A2) / len(rows), 3)})
        print(f"judge 1: {len(A1)}/{len(rows)} = {out['score_judge1']} | agreement {out['agreement']}"
              f" kappa {out['kappa']} | both agree {len(both)} ({out['score_both_agree']})"
              f" | only judge 1 {sorted(A1 - A2)} | only judge 2 {sorted(A2 - A1)}")
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(f"-> {args.out}")


# ----------------------------------------------------------------- router
def cmd_router(args) -> None:
    """Router hits per kind over a corpus. No model calls, no cost.

    Precision cannot be computed here — a hit is 'right' only if the row it
    eventually produces is one a human would activate, and that judgement
    happens after `corpus`. This mode gives the denominator and the sample.
    """
    hx = _load_extract()
    files = sorted(Path(args.corpus).glob("*.json"))
    files = [f for f in files if f.name != "index.json"]
    hits = collections.Counter()
    samples = collections.defaultdict(list)
    turns_total = 0
    hit_turns = 0
    for path in files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        turns = doc.get("turns", [])
        turns_total += len(turns)
        for i, turn in enumerate(turns):
            got = hx.route(turn, turns[i - 1] if i else None)
            if got:
                hit_turns += 1
            for kind, evidence in got:
                hits[kind] += 1
                if len(samples[kind]) < 4:
                    samples[kind].append(
                        f"{path.stem} t{turn.get('n')}: {str(evidence)[:70]}")
    print(f"{len(files)} sessions | {turns_total} turns | "
          f"{hit_turns} turns with a router hit ({hit_turns / max(turns_total, 1):.0%})")
    print(f"{turns_total - hit_turns} turns would go to the judge "
          f"(one model call each)")
    for kind, n in hits.most_common():
        print(f"\n  {kind}: {n}")
        for s in samples[kind]:
            print(f"      {s}")
    if args.out:
        Path(args.out).write_text(json.dumps(
            {"sessions": len(files), "turns": turns_total,
             "hit_turns": hit_turns, "hits": dict(hits),
             "samples": {k: v for k, v in samples.items()}},
            indent=1), encoding="utf-8")
        print(f"\n-> {args.out}")


# ----------------------------------------------------------------- corpus
def run_one(scripts: Path, path: Path, out_dir: Path, args) -> dict:
    name = path.stem
    # A rerun into the same --out must measure THIS run: the extractor appends
    # its moments, and `nudge` reads every line.
    for suffix in (".moments.jsonl", ".drafts.jsonl", ".stats.json", ".trace.log"):
        try:
            (out_dir / f"{name}{suffix}").unlink()
        except FileNotFoundError:
            pass
    cmd = [sys.executable, str(scripts / "harness_extract.py"),
           "--turns", str(path),
           "--out", str(out_dir / f"{name}.moments.jsonl"),
           "--stats", str(out_dir / f"{name}.stats.json"),
           "--trace", str(out_dir / f"{name}.trace.log"),
           "--env", args.env, "--quiet"]
    if args.classify_timeout:
        cmd += ["--classify-timeout", str(args.classify_timeout)]
    if args.pace:
        cmd += ["--pace", str(args.pace)]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        stats = json.loads((out_dir / f"{name}.stats.json").read_text())
    except (OSError, ValueError):
        stats = {"session": name, "moments": 0, "error": (proc.stderr or "")[-200:]}
    stats["file"] = path.name
    stats["wall_s"] = round(time.time() - t0, 1)
    print(f"  done {name}: {stats.get('moments', 0)} moments, "
          f"{stats.get('server_calls', 0)} calls, "
          f"{stats.get('transport_errors', 0)} transport errors, {stats['wall_s']}s",
          flush=True)
    return stats


def cmd_corpus(args) -> None:
    """The client pipeline over a corpus: router, redacted window, one
    classifier call per sent turn, and the flagged moments per session. What
    the agent would do with those moments is `nudge`'s job."""
    scripts = plugin_scripts()
    corpus = Path(args.corpus)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = corpus / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}
    engineer_of = {s["file"]: s["engineer"] for s in index.get("sessions", [])}

    files = sorted(f for f in corpus.glob("*.json") if f.name != "index.json")
    if args.limit:
        files = files[:args.limit]
    print(f"{len(files)} sessions from "
          f"{len(set(engineer_of.values())) or '?'} engineers, "
          f"{args.jobs} at a time")

    t0 = time.time()
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_one, scripts, f, out_dir, args) for f in files]
        for fut in cf.as_completed(futures):
            results.append(fut.result())

    latencies = sorted(x for r in results for x in (r.get("latencies") or []))
    per_session = {r["file"]: r.get("moments", 0) for r in results}
    hinted = sum(r.get("hinted_calls", 0) for r in results)
    sent = sum(r.get("turns_sent", 0) for r in results)
    moments = sum(per_session.values())
    reasons = collections.Counter()
    kinds = collections.Counter()
    for r in results:
        reasons.update(r.get("reasons") or {})
        kinds.update(r.get("kinds") or {})
    summary = {
        "sessions": len(results),
        "engineers": sorted(set(engineer_of.values())) or ["?"],
        "turns": sum(r.get("turns", 0) for r in results),
        "turns_sent": sent,
        "turns_spared_by_router": sum(r.get("turns_spared", 0) for r in results),
        "moments": moments,
        "signal_rate": round(moments / sent, 3) if sent else None,
        "moments_per_session_mean": round(moments / max(len(results), 1), 2),
        "moments_per_session_max": max(per_session.values() or [0]),
        "server_calls": sum(r.get("server_calls", 0) for r in results),
        "transport_errors": sum(r.get("transport_errors", 0) for r in results),
        "reasons": dict(reasons.most_common()),
        "judge_kinds": dict(kinds.most_common()),
        "latency_p50_s": round(latencies[len(latencies) // 2], 1) if latencies else None,
        "latency_p90_s": round(latencies[int(len(latencies) * .9)], 1) if latencies else None,
        "router_hinted_calls": hinted,
        # a hinted call the classifier agreed with ÷ hinted calls
        "router_hint_signal_rate": round(sum(r.get("hinted_signals", 0) for r in results) / hinted, 3)
        if hinted else None,
        "wall_s": round(time.time() - t0, 1),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    print("\n" + "=" * 62)
    for key, value in summary.items():
        print(f"{key:28} {value}")
    print(f"\n-> {out_dir}  (next: `score_s0.py nudge --run {out_dir}` for the agent's rows)")


JUDGE_SHEET_HEADER = """# S0 judge sheet

Every row the pipeline drafted, numbered. Judge each one **as a set member**,
by the rubric below, and write A or R in the verdict column of your own copy.

## Rubric — a row is ACTIVATABLE (A) only if all five hold

1. **It would change an action.** A reader can name the next command, edit or
   read it would alter. Not a summary, not an observation.
2. **The trigger can actually match.** The regex/anchors would fire on the
   shape of a real future action, and would NOT fire on every member of that
   family. A trigger no engine can match is R even if the sentence is wise.
3. **It is not derivable.** A new engineer would not learn it by reading the
   repo, its tests, its docs or CLAUDE.md.
4. **It is not project state.** Not "PR #1233 does X", not "we decided Y for
   this ticket", not a restatement of an error message.
5. **It outlives the session.** Still true next month; not tied to one line
   number, one branch, or one PR that will merge.

Mark R and give the reason from: `no_action`, `unmatchable`, `derivable`,
`project_state`, `one_off`, `duplicate`, `wrong`.

**Two people judge independently, then compare.** The gate is
`rows a human would activate ÷ rows drafted ≥ 0.5`.
"""


def _same_trigger(a: dict, b: dict) -> bool:
    ma, mb = a.get("matcher") or {}, b.get("matcher") or {}
    if ma and mb and ma.get("event") == mb.get("event"):
        for key in ("command_rx", "path_rx", "content_rx"):
            if ma.get(key) and ma.get(key) == mb.get(key):
                return True
    oa, ob = a.get("ordering") or {}, b.get("ordering") or {}
    if oa and ob and oa.get("gated_command_rx") == ob.get("gated_command_rx"):
        return True
    return bool(set(a.get("anchors") or []) & set(b.get("anchors") or []))


def write_judge_sheet(path: Path, rows: list, summary: dict, dupes: list,
                      near: list | None = None) -> None:
    out = [JUDGE_SHEET_HEADER, "",
           f"- rows drafted: **{len(rows)}**",
           f"- sessions: {summary['sessions']} "
           f"({len(summary['engineers'])} engineers)",
           f"- duplicate pairs found across the run: {len(dupes)} lexical, "
           f"{len(near or [])} by trigger/title",
           "", "| # | verdict | reason | engineer | source_ref | title |",
           "|---|---|---|---|---|---|"]
    for n, r in enumerate(rows, 1):
        out.append(f"| {n} |  |  | {r['_engineer']} | `{r['source_ref']}` | "
                   f"{r['title'][:70].replace('|', '/')} |")
    out += ["", "---", "", "## The rows", ""]
    for n, r in enumerate(rows, 1):
        engine = next((k for k in ("matcher", "ordering", "anchors")
                       if k in r), "?")
        out += [
            f"### {n}. {r['title']}",
            "",
            f"- **engineer** {r['_engineer']} · **source_ref** `{r['source_ref']}`"
            f" · **delivery** `{r['delivery']}` · **engine** `{engine}`",
            f"- **repo** `{r['state'].get('repo') or '(unresolved)'}`"
            f" · **branch** `{r['state'].get('branch') or '—'}`"
            f" · **flagged by** `{r.get('_reason', '')[:80]}`"
            f" · **judge kind** `{r.get('_kind') or '—'}`",
            "",
            f"> {r['statement']}",
            "",
            "```json",
            json.dumps({k: r[k] for k in ("matcher", "ordering", "anchors")
                        if k in r}, indent=1),
            "```",
            "",
        ]
    if dupes:
        out += ["## Duplicate pairs (statement similarity > "
                f"{_load_extract().TWIN_THRESHOLD})", ""]
        for a, b, ta, tb in dupes:
            out.append(f"- `{a}` vs `{b}` — {ta} / {tb}")
    if near:
        out += ["", "## Near-duplicate pairs (same trigger or anchor, or title "
                "similarity ≥ 0.6) — judge by hand", ""]
        for a, b, ta, tb in near:
            out.append(f"- `{a}` vs `{b}` — {ta} / {tb}")
    path.write_text("\n".join(out), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="mode", required=True)

    g = sub.add_parser("gold", help="judge recall/precision on the labelled set")
    g.add_argument("--jobs", type=int, default=6)
    g.add_argument("--limit", type=int, default=0)
    g.add_argument("--timeout", type=float, default=0,
                   help="seconds per server call (default: the client's bound)")
    g.add_argument("--out", default="")

    r = sub.add_parser("router", help="router hits over a corpus, no model")
    r.add_argument("--corpus", required=True)
    r.add_argument("--out", default="")

    nd = sub.add_parser("nudge", help="path A minus the person: the agent's decision on every flagged moment")
    nd.add_argument("--run", required=True, help="a `corpus` run directory")
    nd.add_argument("--corpus", required=True)
    nd.add_argument("--model", default="sonnet")
    nd.add_argument("--jobs", type=int, default=4)
    nd.add_argument("--timeout", type=int, default=240)
    nd.add_argument("--limit", type=int, default=0)
    nd.add_argument("--per-session", type=int, default=0)

    j = sub.add_parser("judge", help="a second judge (fresh headless model) over a run's rows")
    j.add_argument("--rows", required=True, help="rows.json of a run")
    j.add_argument("--against", default="", help="the first judge's verdicts JSON, for agreement")
    j.add_argument("--model", default="opus")
    j.add_argument("--timeout", type=int, default=600)
    j.add_argument("--out", default="")

    c = sub.add_parser("corpus", help="router + classifier over a corpus, moments per session")
    c.add_argument("--corpus", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--jobs", type=int, default=2)
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--env", default="staging")
    c.add_argument("--classify-timeout", type=float, default=0,
                   help="seconds for the one server call per turn")
    c.add_argument("--pace", type=float, default=1.0,
                   help="seconds between server calls per session (a personal "
                        "key is capped at 60/min; keep --jobs at 2 or fewer)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    {"gold": cmd_gold, "router": cmd_router, "corpus": cmd_corpus,
     "judge": cmd_judge, "nudge": cmd_nudge}[args.mode](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
