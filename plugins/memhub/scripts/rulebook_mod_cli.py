#!/usr/bin/env python3
"""The three things the plugin's mod asks the Python Rulebook for (mods spec
§4.1, §4.7). The mod runs these through ``$.process.run``; nothing else
should. Every subcommand reuses ``rulebook_hook``'s own functions — there is
no second implementation of auth, ledger rows or book keying here — so the mod
and the command hooks read and write the same files the same way.

``rulebook_hook.py`` sits at the plugin directory's per-file size limit, so
this lives beside it and imports it rather than adding subcommands to it.

``--env staging|prod`` names the install the mod believes it is part of; it
must match this install (``mod_lanes.install_env``: ``memhub-staging`` →
staging, ``memhub`` → prod) or the command exits 2 and prints nothing on
stdout. A mismatch is a mod bug, and answering for the wrong backend would be
worse than answering nothing.

api-info
--------
    python3 scripts/rulebook_mod_cli.py api-info --env staging

stdout, exactly one JSON line, nothing else ever:

    {"base": "https://api.example", "bearer": "<secret>"}   signed in   (exit 0)
    {}                                                        signed out  (exit 0)
    {"error": "<one line>"}                                   could not resolve (exit 1)

``base`` is the REST origin (``pak.api_base`` of the install's MCP url; the
Rulebook API is ``<base>/v1/team/rulebook``). ``bearer`` is what
``rulebook_hook._api()`` resolves — an explicit token
(``_memhub_auth.explicit_token``), else the stored access key ``/memhub:login``
minted, else a cached OAuth token — non-interactively and without refreshing,
exactly as every hook lane does. Anything the resolution writes to stdout is diverted to
stderr, so the bearer is the only secret on stdout and stdout is only the line.
The mod keeps it in module memory, never in ``$.state``/``$.store``.

log
---
    python3 scripts/rulebook_mod_cli.py log --env staging  < rows.json

Appends fire and event rows to this install's ledger through
``rulebook_hook.log_fires`` / ``log_event`` — the same functions, lock and
files (``<base>/ledger/fires.jsonl``, ``events.jsonl``) the hook lanes use —
so the unclaimed ``rulebook_hook.py flush`` lane ships them unchanged. Rows
are stamped ``host: "claude"`` (the mod only runs in Claude Code).

stdin, one JSON object. Top-level keys are the call's context (the hook's
``ctx``); every fire or event may override any of them for itself:

    {
      "session": "<session_id>",            required
      "cwd": "/path/to/checkout",           optional: binds the forward-test
                                            redirect as the hook does, and fills
                                            repo / branch / worktree when absent
      "repo": "...", "branch": "...",       optional (else from cwd, else null)
      "worktree": "<16 hex>",               optional (else from cwd, else null)
      "tool": "Bash",                       optional
      "agent_id": null,                     optional; a subagent's id
      "source_message_id": null,            optional
      "rule_version": "",                   optional; a fire's own wins
      "fires": [
        {"rule_id": "r-1",                  required
         "hook_phase": "pre",               required: pre | post | prompt | anchor | …
         "mode": "advise",                  required: advise | gate (as the hook writes)
         "rulebook_id": "b-1",              optional, local only
         "rule_version": "7",               optional
         "excerpt": "git push -f",          optional, local only, cut to 160
         "dedup_key": "...", "raw_matches_before_fire": 0,
         "override_reason": null, "fired_at": "<iso, µs>",
         "judge_verdict": "fit", "judge_score": 0.91}      optional
      ],
      "events": [
        {"kind": "receipt",                 required: receipt | converted |
                                            dismissed | turn_end | session_end
         "rule_id": "r-1", "reason": null, "at": "<iso, µs>",
         "worktree": null}                  optional, handed to log_event as
                                            the hook lanes hand it: a receipt
                                            without one (absent or null) is
                                            session-scoped and answers only
                                            this session's fires; any other
                                            kind without one gets the call's
                                            checkout
      ]
    }

stdout, one JSON line: ``{"fire_ids": [...], "event_ids": [...], "rejected":
[{"kind": "fire"|"event", "index": i, "error": "..."}]}``. ``fire_ids`` /
``event_ids`` line up with the input lists; an entry is null when the row was
rejected or the ledger could not be written (the hook's writers fail silent;
null is how that shows here). Exit 0 when every row has an id, 1 otherwise,
2 on unusable input.

paths
-----
    python3 scripts/rulebook_mod_cli.py paths --env staging --cwd /path [--repo NAME]

stdout, one JSON line, so the mod never re-derives keying in TypeScript:

    {"env": "staging", "plugin_root": "...", "base": "...",
     "repo": "MemHub-Backend", "root": "/path", "branch": "main",
     "worktree": "<16 hex>", "book": "<base>/book/MemHub-Backend-1a2b3c4d.json",
     "ledger": "<base>/ledger", "state": "<base>/state"}

``repo``/``root``/``branch``/``worktree`` are ``repo_of_call({"cwd": cwd})``
— what the hook lanes resolve for a call made there — and ``book`` is
``book_path(repo)`` after ``set_active_base(cwd)``, so a §4b forward test's
private book is honoured. ``--repo`` names the repo instead (root, branch and
worktree are then null). No repo → ``repo`` "" and ``book`` null. Nothing is
created on disk.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import mod_lanes  # noqa: E402

HOST = "claude"
EVENT_KINDS = ("receipt", "converted", "dismissed", "turn_end", "session_end")
_CTX_KEYS = ("repo", "branch", "worktree", "tool", "agent_id", "source_message_id",
             "rule_version")


def _out(obj) -> None:
    sys.stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _hook():
    """rulebook_hook, set up as its own ``__main__`` sets itself up."""
    import rulebook_hook as rh  # noqa: PLC0415
    if rh._rc is None:
        try:
            import rulebook_cache  # noqa: PLC0415
            rh._rc = rulebook_cache
        except Exception:
            pass
    return rh


# ── api-info ────────────────────────────────────────────────────────────────

def cmd_api_info(_args) -> int:
    try:
        with contextlib.redirect_stdout(sys.stderr):
            api = _hook()._api()
    except Exception as exc:  # noqa: BLE001 — reported, never a traceback on stdout
        _out({"error": f"{type(exc).__name__}: {exc}".splitlines()[0][:300]})
        return 1
    if not api:
        _out({})
        return 0
    base, bearer, _http = api
    _out({"base": base, "bearer": bearer, "studio": _studio_of(base)})
    return 0


def _studio_of(base: str) -> str:
    """The MemHub Studio web app paired with this REST base — the one mapping
    login's guide link and harness_stop.rule_url() already key on
    (plugin_onboarding._ORIGINS) — or "" for an API with no known web app.
    The mod links a rule's Studio page from it without a host table of its own."""
    try:
        from plugin_onboarding import _ORIGINS  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — no link is an answer, not a failure
        return ""
    return _ORIGINS.get(str(base).rstrip("/"), "")


# ── log ─────────────────────────────────────────────────────────────────────

def _ctx(rh, doc):
    ctx = {"session": doc.get("session"), "host": HOST}
    for k in _CTX_KEYS:
        ctx[k] = doc.get(k)
    if ctx["rule_version"] is None:
        ctx["rule_version"] = ""          # what load_rules gives the hook
    cwd = doc.get("cwd")
    if isinstance(cwd, str) and cwd:
        rh.set_active_base(cwd)
        if not all(doc.get(k) for k in ("repo", "branch", "worktree")):
            repo, root, _gitdir, branch = rh.repo_of_call({"cwd": cwd})
            if doc.get("repo") is None:
                ctx["repo"] = repo or None
            if doc.get("branch") is None:
                ctx["branch"] = branch or None
            if "worktree" not in doc:
                ctx["worktree"] = rh.worktree_key(root)
    return ctx


def _with(ctx, row):
    return dict(ctx, **{k: row[k] for k in _CTX_KEYS if k in row})


def _log_fire(rh, ctx, row):
    for key in ("rule_id", "hook_phase", "mode"):
        if not isinstance(row.get(key), str) or not row[key]:
            raise ValueError(f"{key} must be a non-empty string")
    rid = row["rule_id"]
    rule = {"id": rid, "_rulebook_id": row.get("rulebook_id"),
            "_version": row.get("rule_version")}
    judge = None
    if row.get("judge_verdict") is not None:
        judge = {rid: {"verdict": row["judge_verdict"], "p_fit": row.get("judge_score")}}
    one = lambda k: {rid: row[k]} if row.get(k) is not None else None  # noqa: E731
    ctx = _with(ctx, row)
    ctx["rule_version"] = ctx.get("rule_version") or ""
    ids = rh.log_fires(
        ctx, [rule], hook_phase=row["hook_phase"], mode=row["mode"],
        excerpt=str(row.get("excerpt") or ""),
        raw_counts=one("raw_matches_before_fire"), dedup_keys=one("dedup_key"),
        override_reasons=one("override_reason"), fired_at=row.get("fired_at"),
        judge=judge)
    return ids.get(rid)


def _log_event(rh, ctx, row):
    kind = row.get("kind")
    if kind not in EVENT_KINDS:
        raise ValueError(f"kind must be one of {', '.join(EVENT_KINDS)}")
    kw = {"rule_id": row.get("rule_id"), "reason": row.get("reason"), "at": row.get("at")}
    if "worktree" in row:
        kw["worktree"] = row["worktree"]
    base = dict(ctx, **{k: row[k] for k in _CTX_KEYS if k in row and k != "worktree"})
    return rh.log_event(base, kind, **kw)


def cmd_log(_args) -> int:
    try:
        doc = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        print(f"rulebook_mod_cli log: stdin is not JSON ({exc})", file=sys.stderr)
        return 2
    if not isinstance(doc, dict) or not isinstance(doc.get("session"), str) \
            or not doc["session"]:
        print("rulebook_mod_cli log: stdin needs an object with a non-empty "
              '"session"', file=sys.stderr)
        return 2
    fires, events = doc.get("fires"), doc.get("events")
    fires = [] if fires is None else fires
    events = [] if events is None else events
    if not isinstance(fires, list) or not isinstance(events, list):
        print('rulebook_mod_cli log: "fires" and "events" must be lists', file=sys.stderr)
        return 2
    rh = _hook()
    if rh.portable_lock is None:
        print("rulebook_mod_cli log: portable_lock is unavailable; the hook "
              "records no ledger rows without it", file=sys.stderr)
    with contextlib.redirect_stdout(sys.stderr):
        ctx = _ctx(rh, doc)
        out = {"fire_ids": [], "event_ids": [], "rejected": []}
        for kind, rows, fn, key in (("fire", fires, _log_fire, "fire_ids"),
                                    ("event", events, _log_event, "event_ids")):
            for i, row in enumerate(rows):
                try:
                    if not isinstance(row, dict):
                        raise ValueError("row must be an object")
                    out[key].append(fn(rh, ctx, row))
                except Exception as exc:  # noqa: BLE001
                    out[key].append(None)
                    out["rejected"].append({"kind": kind, "index": i, "error": str(exc)})
    _out(out)
    ok = all(out["fire_ids"]) and all(out["event_ids"])
    return 0 if ok else 1


# ── paths ───────────────────────────────────────────────────────────────────

def cmd_paths(args) -> int:
    if not args.cwd and not args.repo:
        print("rulebook_mod_cli paths: --cwd or --repo is required", file=sys.stderr)
        return 2
    rh = _hook()
    with contextlib.redirect_stdout(sys.stderr):
        base = rh.set_active_base(args.cwd or "")
        if args.repo:
            repo, root, branch, worktree = args.repo, None, None, None
        else:
            repo, root, _gitdir, branch = rh.repo_of_call({"cwd": args.cwd})
            worktree = rh.worktree_key(root)
            root, branch = root or None, branch or None
        book = rh.book_path(repo) if repo else None
    _out({"env": args.env, "plugin_root": mod_lanes.plugin_root(), "base": base,
          "repo": repo or "", "root": root, "branch": branch, "worktree": worktree,
          "book": book, "ledger": os.path.join(base, "ledger"),
          "state": os.path.join(base, "state")})
    return 0


# ── entry ───────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rulebook_mod_cli.py")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("api-info", "log", "paths"):
        p = sub.add_parser(name)
        p.add_argument("--env", required=True, choices=sorted(mod_lanes.ENV_VARS))
        if name == "paths":
            p.add_argument("--cwd", default="")
            p.add_argument("--repo", default="")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if not args.cmd:
        ap.print_usage(sys.stderr)
        return 2
    mine = mod_lanes.install_env()
    if args.env != mine:
        print(f"rulebook_mod_cli: --env {args.env} but this install is "
              f"{mine or 'unidentifiable'}", file=sys.stderr)
        return 2
    return {"api-info": cmd_api_info, "log": cmd_log, "paths": cmd_paths}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
