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
          Since S1 the judge runs on the server behind `POST …/harness/draft`
          (MemHub #1249) and is not separately callable: a moment is "signal"
          when the reply is anything but `no_signal`, and a positive also runs
          the author, so this mode spends author calls too.

  router  the router alone over a corpus, no model calls, no cost. Reports how
          often each regex fires and — once you have judged the corpus — what
          fraction of its hits produced a row a human would activate.

  corpus  the whole pipeline over a corpus directory of canonical turns JSON
          (what `staging_sessions.py corpus` writes), N sessions at a time.
          Reports drafts per session, duplicates, refusal reasons, timeouts,
          cost and wall time, and writes `judge_sheet.md` — every drafted row,
          numbered, for the hand judgement the gate actually turns on.

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
        reply, dt = hx.server_draft("\n".join(lines), timeout=args.timeout)
        reason = str(reply.get("reason") or "")
        if reason in hx.CLIENT_REASONS or reason in ("judge_failed", "disabled"):
            return item_id, None, 0.0, reason
        # The judge said yes whenever the author ran — every reason but
        # `no_signal` is an author outcome, refusal or draft.
        verdict = {"signal": reason != "no_signal", "kind": reply.get("kind") or "none",
                   "reason": reason}
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


# ----------------------------------------------------------------- review
def _load_stop():
    scripts = plugin_scripts()
    sys.path.insert(0, str(scripts))
    import harness_stop                                     # noqa: PLC0415
    return harness_stop


_TRACE_TURN = re.compile(r"^== turn (\d+) \|")
_TRACE_SERVER = re.compile(r"^   server \(([\d.]+)s\): (\S+)(?: — .*?)?(?: \| judge=(\S+))?$")


def moments_from_trace(hx, doc: dict, trace_path: Path, rows: list[dict]) -> list[dict]:
    """The classifier-flagged moments of a finished `corpus` run, rebuilt
    from its trace: a turn was a signal when the server ran the author on it
    (any reason but no_signal / an outage), whether or not a row came back.
    Runs made after this mode existed record moments directly
    (`--moments`); this reads the ones that did not."""
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
        state = hx.stamp_state(session=session, turn=turn, row_engine_target=("", ""),
                               cwd="", hook_version=hx.plugin_version(), env_name="staging",
                               default_repo=doc.get("repo") or "")
        hits = hx.route(turn, prev)
        out.append({"turn": turn.get("n"), "source_ref": f"{session}#{turn.get('n')}",
                    "hint": hx.router_hint(hits), "kind": flagged[1], "reason": "replay",
                    "window": hx.redact_window(hx.build_window(turn, prev, state)),
                    "state": state})
    return out


def cmd_review(args) -> None:
    """The post-session review (§4.3) over a corpus run — the stage AFTER
    the classifier, where an agent with the whole session decides what a
    human sees. The activate ratio a reviewer meets is the ratio after this
    stage, not before it. Two variants, one headless `claude -p` per session
    each, disarmed (`--safe-mode`, MEMHUB_HARNESS_CHILD=1):

      --variant review   keep/drop over the rows the SERVER author drafted
      --variant mine     the local agent AUTHORS from the classifier-flagged
                         moments, with no server drafts in front of it

    Nothing is synced or filed: rows land in `<run>/<variant>/`. With
    `--verdicts` (the hand judgement over `rows.json` indices) the
    after-review ratio of the `review` variant is computed here; `mine`
    writes a fresh judge sheet, since its rows are new.
    """
    hx = _load_extract()
    hs = _load_stop()
    run = Path(args.run)
    corpus = Path(args.corpus)
    out = run / args.variant
    drafts_dir = out / "state"
    if drafts_dir.exists():
        import shutil                                       # noqa: PLC0415
        shutil.rmtree(drafts_dir)
    drafts_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MEMHUB_HARNESS_DRAFTS"] = str(drafts_dir)
    rows_all = json.loads((run / "rows.json").read_text(encoding="utf-8"))
    index_of: dict = collections.defaultdict(list)
    for i, r in enumerate(rows_all, 1):
        index_of[(r["source_ref"], r["title"])].append(i)

    jobs = []
    for doc_path in sorted(corpus.glob("*.json")):
        if doc_path.name == "index.json":
            continue
        name = doc_path.stem
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
        drafts = run / f"{name}.drafts.jsonl"
        rows = hx.read_drafts(drafts) if drafts.is_file() else []
        # Two corpus files can carry one session id (the same session
        # captured twice); the replay keys its state by corpus file so they
        # never share a drafts or reviewed file. `source_ref` keeps the id.
        key = name
        n_moments = 0
        if args.variant == "review":
            if not rows:
                continue
            hx.drafts_path(key).write_text(drafts.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            trace = run / f"{name}.trace.log"
            moments = moments_from_trace(hx, doc, trace, rows) if trace.is_file() else []
            if not moments:
                continue
            for m in moments:
                hx.append_draft(hs.moments_path(key), m)
            n_moments = len(moments)
        repo = doc.get("repo") or ((rows[0].get("state") or {}).get("repo") if rows else "") or ""
        hs.save_meta(key, repo=repo, turns_path=str(doc_path), cwd="",
                     drafts=len(rows) if args.variant == "review" else 0,
                     reviewed_through=0, mined_through=0)
        jobs.append((name, key, len(rows) if args.variant == "review" else 0, n_moments))
    print(f"{len(jobs)} sessions, variant={args.variant}, {args.jobs} reviews at a time")

    def one(job):
        name, key, n, n_moments = job
        t0 = time.time()
        got = hs.review(key, moment="replay")
        reviewed = hx.read_drafts(hs.reviewed_path(key))
        (out / f"{name}.reviewed.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reviewed), encoding="utf-8")
        meta = hs.load_meta(key)
        print(f"  {name}: {n} drafts, {n_moments} moments -> {len(reviewed)} rows"
              f"{' (review failed)' if got < 0 else ''} {round(time.time() - t0)}s"
              f"{' refused ' + json.dumps(meta.get('mining_refused')) if meta.get('mining_refused') else ''}",
              flush=True)
        return {"name": name, "drafts": n, "moments": n_moments,
                "kept": len(reviewed), "failed": got < 0,
                "gave_up": bool(meta.get("review_gave_up")),
                "refused": meta.get("mining_refused") or {}, "kept_rows": reviewed}

    t0 = time.time()
    results = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for res in pool.map(one, jobs):
            results.append(res)

    kept_rows = [r for res in results for r in res["kept_rows"]]
    kept_idx = sorted({i for r in kept_rows for i in index_of.get((r["source_ref"], r["title"]), [])})
    refused = collections.Counter()
    for res in results:
        for why, n in res["refused"].items():
            refused[why] += n
    summary = {
        "variant": args.variant,
        "sessions_reviewed": len(results),
        "reviews_failed": sum(1 for r in results if r["failed"]),
        "drafts_in": sum(r["drafts"] for r in results),
        "moments_in": sum(r["moments"] for r in results),
        "rows_out": len(kept_rows),
        "rows_per_session_max": max((r["kept"] for r in results), default=0),
        "rows_per_session_mean": round(sum(r["kept"] for r in results) / max(len(results), 1), 2),
        "kept_indices": kept_idx,
        "mining_refused": dict(refused.most_common()),
        "supersedes_set": sum(1 for r in kept_rows if r.get("supersedes_rule_id")),
        "by_engine": dict(collections.Counter(
            next(k for k in ("matcher", "ordering", "anchors") if k in r) for r in kept_rows)),
        "wall_s": round(time.time() - t0, 1),
    }
    if args.variant == "mine":
        engineer_of = {}
        idx = corpus / "index.json"
        if idx.is_file():
            engineer_of = {s["file"]: s["engineer"]
                           for s in json.loads(idx.read_text()).get("sessions", [])}
        for res in results:
            for r in res["kept_rows"]:
                r["_engineer"] = engineer_of.get(f"{res['name']}.json", res["name"].split("__")[0])
        (out / "rows.json").write_text(json.dumps(kept_rows, indent=1), encoding="utf-8")
        write_judge_sheet(out / "judge_sheet.md", kept_rows,
                          {"sessions": len(results), "engineers": sorted({r["_engineer"] for r in kept_rows})},
                          [], [])
    if args.verdicts and args.variant == "review":
        v = json.loads(Path(args.verdicts).read_text(encoding="utf-8"))
        A = set(v["A"])
        R = {i: why for why, ids in v["R"].items() for i in ids}
        kept_set = set(kept_idx)
        summary.update({
            "activatable_before": len(A), "drafted_before": len(rows_all),
            "ratio_before": round(len(A) / max(len(rows_all), 1), 3),
            "activatable_after": len(A & kept_set), "kept_after": len(kept_set),
            "ratio_after": round(len(A & kept_set) / max(len(kept_set), 1), 3),
            "activatable_dropped_by_review": sorted(A - kept_set),
            "rejects_kept_by_review": {str(i): R[i] for i in sorted(kept_set - A)},
            "rejects_dropped_by_reason": dict(collections.Counter(
                R[i] for i in R if i not in kept_set).most_common()),
        })
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print("\n" + "=" * 62)
    for k, v in summary.items():
        if k not in ("kept_indices",):
            print(f"{k:32} {v}")
    print(f"\n-> {out}/summary.json")


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
    # A rerun into the same --out must measure THIS run: the extractor opens
    # its drafts file in append mode, and the aggregate below reads every
    # line, so last run's rows would be counted again (Codex, #191/#192).
    for suffix in (".drafts.jsonl", ".stats.json", ".trace.log"):
        try:
            (out_dir / f"{name}{suffix}").unlink()
        except FileNotFoundError:
            pass
    cmd = [sys.executable, str(scripts / "harness_extract.py"),
           "--turns", str(path),
           "--out", str(out_dir / f"{name}.drafts.jsonl"),
           "--stats", str(out_dir / f"{name}.stats.json"),
           "--trace", str(out_dir / f"{name}.trace.log"),
           "--budget", str(args.budget), "--env", args.env, "--quiet"]
    if args.draft_timeout:
        cmd += ["--draft-timeout", str(args.draft_timeout)]
    if args.pace:
        cmd += ["--pace", str(args.pace)]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        stats = json.loads((out_dir / f"{name}.stats.json").read_text())
    except (OSError, ValueError):
        stats = {"session": name, "rows": 0, "error": (proc.stderr or "")[-200:]}
    stats["file"] = path.name
    stats["wall_s"] = round(time.time() - t0, 1)
    print(f"  done {name}: {stats.get('rows', 0)} rows, "
          f"{stats.get('server_calls', 0)} calls, "
          f"{stats.get('transport_errors', 0)} transport errors, {stats['wall_s']}s",
          flush=True)
    return stats


def cmd_corpus(args) -> None:
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
        futures = [pool.submit(run_one, scripts, f, out_dir, args)
                   for f in files]
        for fut in cf.as_completed(futures):
            results.append(fut.result())

    # ---- aggregate
    rows_all = []
    for res in results:
        drafts = out_dir / f"{Path(res['file']).stem}.drafts.jsonl"
        if not drafts.exists():
            continue
        for line in drafts.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                row["_session_file"] = res["file"]
                row["_engineer"] = engineer_of.get(res["file"], "?")
                rows_all.append(row)

    refusals = collections.Counter()
    for res in results:
        for reason, n in (res.get("refusals") or {}).items():
            refusals[reason] += n
    per_session = {r["file"]: r.get("rows", 0) for r in results}
    over_budget = {f: n for f, n in per_session.items() if n > args.budget}

    # Duplicates ACROSS the whole run, not just within one session: two
    # engineers hitting the same trap is exactly the twin the server-side
    # check exists for, and the scorecard should say whether it happens.
    # Two passes, both reported: the lexical one the in-run twin check uses
    # (statement Jaccard), and a looser one over what the rows would DO — the
    # same trigger regex or a shared anchor, or a near-identical title — which
    # is what S0's hand judgement found and the lexical pass missed (Finding 5).
    hx = _load_extract()
    dupes, near = [], []
    for i, a in enumerate(rows_all):
        for b in rows_all[i + 1:]:
            pair = (a["source_ref"], b["source_ref"], a["title"][:60], b["title"][:60])
            if hx.similarity(a["statement"], b["statement"]) > hx.TWIN_THRESHOLD:
                dupes.append(pair)
            elif _same_trigger(a, b) or hx.similarity(a["title"], b["title"]) >= 0.6:
                near.append(pair)
    latencies = [x for r in results for x in (r.get("latencies") or [])]
    latencies.sort()

    engineers = sorted({r["_engineer"] for r in rows_all}) or ["?"]
    router_authored = dict(collections.Counter(
        k for r in results for k, n in (r.get("router_authored") or {}).items()
        for _ in range(n)).most_common())
    router_refused = dict(collections.Counter(
        k for r in results for k, n in (r.get("router_refused") or {}).items()
        for _ in range(n)).most_common())
    hinted = sum(r.get("hinted_calls", 0) for r in results)
    summary = {
        "sessions": len(results),
        "engineers": sorted(set(engineer_of.values())) or ["?"],
        "engineers_with_rows": engineers,
        "turns": sum(r.get("turns", 0) for r in results),
        "turns_sent": sum(r.get("turns_sent", 0) for r in results),
        "turns_spared_by_router": sum(r.get("turns_spared", 0) for r in results),
        "rows": len(rows_all),
        "rows_per_session_max": max(per_session.values() or [0]),
        "rows_per_session_mean": round(
            sum(per_session.values()) / max(len(per_session), 1), 2),
        "sessions_over_budget": over_budget,
        "duplicate_pairs_lexical": len(dupes),
        "duplicate_pairs_trigger_or_title": len(near),
        "server_calls": sum(r.get("server_calls", 0) for r in results),
        "transport_errors": sum(r.get("transport_errors", 0) for r in results),
        "latency_p50_s": round(latencies[len(latencies) // 2], 1) if latencies else None,
        "latency_p90_s": round(latencies[int(len(latencies) * .9)], 1) if latencies else None,
        "judge_kinds": dict(collections.Counter(
            k for r in results for k, n in (r.get("kinds") or {}).items()
            for _ in range(n)).most_common()),
        "twins_dropped_in_run": sum(r.get("twins", 0) for r in results),
        "refusals": dict(refusals.most_common()),
        "router_hinted_calls": hinted,
        "router_authored": router_authored,
        "router_refused": router_refused,
        # rows from router-hinted turns ÷ router-hinted calls: the scorecard's
        # "router regex precision" row, measured the same way as S0's
        "router_precision": round(sum(router_authored.values()) / hinted, 3) if hinted else None,
        "wall_s": round(time.time() - t0, 1),
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=1), encoding="utf-8")
    (out_dir / "rows.json").write_text(
        json.dumps(rows_all, indent=1), encoding="utf-8")
    write_judge_sheet(out_dir / "judge_sheet.md", rows_all, summary, dupes, near)

    print("\n" + "=" * 62)
    for key in ("sessions", "engineers", "turns", "turns_sent",
                "turns_spared_by_router", "rows",
                "rows_per_session_max", "rows_per_session_mean",
                "sessions_over_budget", "duplicate_pairs_lexical",
                "duplicate_pairs_trigger_or_title", "server_calls",
                "transport_errors", "latency_p50_s", "latency_p90_s",
                "router_hinted_calls", "router_precision",
                "twins_dropped_in_run", "wall_s"):
        print(f"{key:32} {summary[key]}")
    print(f"{'refusals':26} {summary['refusals']}")
    print(f"\n-> {out_dir}/judge_sheet.md   (hand-judge this; the gate turns on it)")


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

    rv = sub.add_parser("review", help="the post-session review over a corpus run's drafts")
    rv.add_argument("--run", required=True, help="a `corpus` run directory (rows.json, *.drafts.jsonl)")
    rv.add_argument("--corpus", required=True)
    rv.add_argument("--verdicts", default="", help="hand judgement JSON over rows.json indices")
    rv.add_argument("--jobs", type=int, default=3)
    rv.add_argument("--variant", choices=("review", "mine"), default="review")

    c = sub.add_parser("corpus", help="the whole pipeline over a corpus")
    c.add_argument("--corpus", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--jobs", type=int, default=6)
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--budget", type=int, default=8)
    c.add_argument("--env", default="staging")
    c.add_argument("--draft-timeout", type=float, default=0,
                   help="seconds for the one server call per turn")
    c.add_argument("--pace", type=float, default=1.0,
                   help="seconds between server calls per session (a personal "
                        "key is capped at 60/min; with --jobs 1 this keeps under it)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    {"gold": cmd_gold, "router": cmd_router, "corpus": cmd_corpus,
     "review": cmd_review}[args.mode](args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
