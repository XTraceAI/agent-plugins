#!/usr/bin/env python3
"""The work behind /memhub:onboard — one script, so setup is one command.

    onboard.py run   [--repo DIR] [--host claude-code|codex|cursor]
    onboard.py rules --activate | --propose [--repo DIR]

``run`` signs in if needed, finds or creates the repo's brain, saves the repo's
documents into it, and prepares the starter rules. It prints plain lines meant
for the person and ends with the rules it would turn on. ``rules`` files those
rules into the person's own personal scope: switched on with ``--activate``
(the one question the skill asks), left waiting in Studio with ``--propose``.

**Why a script and not the model calling tools.** The model's ``memhub`` tools
connect once per session, before sign-in, so a first-time customer had to
``/mcp`` → Reconnect halfway through. This script opens its own session with the
plugin's key — the same way ``save_artifact.py`` does — so nothing needs a
reconnect, and every failure is turned into one sentence here, with the detail
in ``onboard.log`` rather than on the customer's screen.

**Why the rules can be switched on without an admin.** They are filed with
``create_rule(scope="personal")``: the person's own rulebook, which MemHub
makes on the first rule filed into it and which applies to them alone. Its
owner is its admin (MemHub-Backend ``is_book_admin``, rulebook-scopes S8), so
``activate=True`` is theirs to make. The rules reach nobody else.

Spec: docs/specs/one-command-onboarding.md. Stdlib only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import atomic_write  # noqa: E402
import rule_decide  # noqa: E402
from _memhub_auth import (  # noqa: E402
    _CACHE_DIR,
    NonInteractiveAuthRequired,
    default_url,
    open_session,
    resolve_url_and_auth,
    skill_command,
)
from brain_resolve import _brains_in, _payload  # noqa: E402
from onboard_docs import scan as scan_docs  # noqa: E402
from plugin_onboarding import guide_url  # noqa: E402
from room_map import env_for_url, read_room, repo_root, room_name, write_room  # noqa: E402

STARTER = HERE.parent / "skills" / "start-rulebook" / "scripts" / "starter_rulebook.py"
SAVE_ARTIFACT = HERE / "save_artifact.py"
LOGIN = HERE / "login.py"

# The five universal rules (spec § The five starter rules). Order is the order
# they are shown in. A rule the catalog drops for this repo (no remote default
# branch, no tests folder) is left out rather than filed with a guessed value.
ONBOARD_RULE_IDS = ("stage-secrets", "git-discard-work", "push-main", "no-verify",
                    "no-skip-to-green")
DOC_TOPIC = "docs"


class Stop(Exception):
    """A step cannot go on; the message is the one sentence the person sees."""


# ── state and log ──────────────────────────────────────────────────────────

def _state_path() -> Path:
    return _CACHE_DIR / "onboard_state.json"


def load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write.publish(_state_path(), json.dumps(state, indent=2, sort_keys=True))


def log(msg: str) -> None:
    """Detail for us, never for the customer's screen."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with (_CACHE_DIR / "onboard.log").open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def say(msg: str) -> None:
    print(msg, flush=True)


# ── MCP calls with the plugin's own key ────────────────────────────────────

class ToolRefused(Exception):
    """A tool answered with an error; its text goes to the log."""


async def _session(url: str):
    _, headers, auth = resolve_url_and_auth(url, interactive=False)
    return await open_session(url, headers, auth, timeout=60)


async def call(session, tool: str, args: dict, expected: str) -> dict:
    res = await session.call_tool(tool, arguments=args)
    payload = _payload(res, expected)
    if getattr(res, "isError", False):
        text = " ".join(getattr(b, "text", "") or "" for b in getattr(res, "content", []) or [])
        log(f"{tool} refused: {text[:500]}")
        raise ToolRefused(text)
    return payload


# ── steps ──────────────────────────────────────────────────────────────────

async def ensure_signed_in(url: str, host: str | None):
    """A session, signing in first if this machine has no credential."""
    try:
        return await _session(url)
    except NonInteractiveAuthRequired:
        pass
    except RuntimeError as exc:
        if "no MemHub credential" not in str(exc):
            raise
    say("Signing in to MemHub…")
    cmd = [sys.executable, str(LOGIN)] + (["--host", host] if host else [])
    # Inherit stdout: the sign-in code and link have to reach the person while
    # login.py waits for their approval.
    if subprocess.run(cmd).returncode != 0:
        raise Stop("Sign-in did not finish. Run this command again to retry.")
    try:
        return await _session(url)
    except Exception as exc:  # noqa: BLE001
        log(f"session after login: {exc!r}")
        raise Stop("Signed in, but MemHub did not accept the new key. "
                   f"Run {skill_command('login')} and then this command again.") from exc


def _slug(name: str) -> str:
    return name.split(":", 1)[1].strip() if name.startswith("Repo:") else name


async def ensure_brain(session, root: Path, env: str, repo_state: dict) -> dict:
    if repo_state.get("brain_id"):
        return repo_state
    name = room_name(root)
    if not name:
        raise Stop("This folder is not a git repository. Run onboarding from inside one.")
    slug = _slug(name)
    has_remote = "/" in slug
    # Exact name match either way: a teammate may have made the brain already,
    # and a rerun must find the one this script made.
    # `repo` takes a bare name too, which is how a no-remote brain is found;
    # `query` would not do: it ranks by overview, and a new brain has none.
    listing = await call(session, "list_agent_brains", {"repo": slug}, "agent_brains")
    found = [b for b in _brains_in(listing) if b.get("name") == name]
    if found:
        # Duplicates happen. Keep the one this machine already routes to, else
        # the oldest — never make another.
        cached = (read_room(root, env) or {}).get("brain_id")
        brain = next((b for b in found if b.get("agent_brain_id") == cached),
                     min(found, key=lambda b: b.get("created_at") or ""))
        created = False
    else:
        args = {"name": name, "category": "repo",
                "description": f"Team memory for {slug}: its docs, decisions and specs."}
        if has_remote:
            args["repo"] = slug
        brain = await call(session, "create_agent_brain", args, "agent_brain_id")
        created = True
    brain_id = brain.get("agent_brain_id") or brain.get("id")
    if not brain_id:
        log(f"brain payload without id: {brain!r}")
        raise Stop("MemHub did not return the repo's brain. Run this command again.")
    org_id = brain.get("org_id")
    write_room(brain_id, name=name, cwd=root, env=env, org_id=org_id)
    repo_state.update({"brain_id": brain_id, "brain_name": name, "brain_created": created,
                       **({"org_id": org_id} if org_id else {})})
    return repo_state


def save_docs(root: Path, url: str, repo_state: dict) -> dict:
    if "docs_saved" in repo_state:
        return repo_state
    # Every document, however short: a 70-byte design note is still the repo's
    # own words, and an empty first brain is the worst first impression.
    docs = scan_docs(root, min_bytes=1)["docs"]
    saved, failed = [], []
    for d in docs:
        cmd = [sys.executable, str(SAVE_ARTIFACT), "--file", str(root / d["path"]),
               "--name", d["name"], "--type", d["type"], "--tags", d["type"],
               "--topic", DOC_TOPIC, "--url", url]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=root, timeout=180)
        if proc.returncode == 0:
            saved.append(d["path"])
        else:
            failed.append(d["path"])
            log(f"save {d['path']} failed: {(proc.stderr or proc.stdout)[-500:]}")
    repo_state["docs_saved"] = saved
    repo_state["docs_failed"] = failed
    return repo_state


def prepare_rules(root: Path) -> list[dict]:
    """The starter rules that apply to this repo, each proven by its cases."""
    with tempfile.TemporaryDirectory() as out:
        proc = subprocess.run([sys.executable, str(STARTER), "all", "--repo", str(root),
                               "--out", out], capture_output=True, text=True, timeout=120)
        try:
            cands = {c["id"]: c for c in json.loads(Path(out, "candidates.json").read_text(encoding="utf-8"))}
            ok = {v["id"] for v in json.loads(Path(out, "verified.json").read_text(encoding="utf-8"))
                  if v.get("ok")}
        except (OSError, ValueError, KeyError):
            log(f"starter_rulebook failed ({proc.returncode}): {proc.stderr[-800:]}")
            return []
    rules = []
    for rid in ONBOARD_RULE_IDS:
        if rid in cands and rid in ok:
            rules.append({"id": rid, "body": cands[rid]["body"]})
    return rules


# ── commands ───────────────────────────────────────────────────────────────

async def cmd_run(args) -> int:
    url = default_url()
    env = env_for_url(url)
    root = repo_root(Path(args.repo)) or Path(args.repo).resolve()
    state = load_state()
    envs = state.setdefault(env, {})
    repo_state = envs.setdefault("repos", {}).setdefault(str(root), {})
    starter = envs.setdefault("starter", {})
    try:
        session = await ensure_signed_in(url, args.host)
        say("✓ Signed in")
        fresh = not repo_state.get("brain_id")
        await ensure_brain(session, root, env, repo_state)
        save_state(state)
        verb = "Created" if fresh and repo_state.get("brain_created") else "Using"
        say(f"✓ {verb} brain {repo_state['brain_name']} (private to you until you share it)")
        save_docs(root, url, repo_state)
        save_state(state)
        n = len(repo_state["docs_saved"])
        say(f"✓ Saved {n} doc{'s' if n != 1 else ''} to the brain"
            + (f" ({len(repo_state['docs_failed'])} could not be saved)" if repo_state["docs_failed"] else ""))
        if starter.get("rule_ids"):
            say(f"✓ Starter rules already set up ({len(starter['rule_ids'])})")
            repo_state["done_at"] = repo_state.get("done_at") or time.time()
            save_state(state)
            say("RULES: none")
            return 0
        rules = prepare_rules(root)
        starter["pending"] = rules
        save_state(state)
        if not rules:
            say("RULES: none")
            return 0
        say(f"RULES: {len(rules)} of {len(ONBOARD_RULE_IDS)} starter rules apply to this repo. "
            "Each one stops your agent from running the command it names:")
        for r in rules:
            say(f"  • {r['body']['title']}")
        return 0
    except Stop as exc:
        say(f"✗ {exc}")
        return 1
    except ToolRefused as exc:
        say(f"✗ MemHub refused a setup step: {str(exc)[:200]}. Run this command again; "
            "details are in onboard.log.")
        return 1
    except Exception:  # noqa: BLE001 — one sentence for the person, the trace for us
        log(traceback.format_exc())
        say("✗ Setup hit an unexpected error. Run this command again; details are in "
            f"{_CACHE_DIR / 'onboard.log'}.")
        return 1


async def cmd_rules(args) -> int:
    url = default_url()
    env = env_for_url(url)
    root = repo_root(Path(args.repo)) or Path(args.repo).resolve()
    state = load_state()
    envs = state.setdefault(env, {})
    starter = envs.setdefault("starter", {})
    repo_state = envs.setdefault("repos", {}).setdefault(str(root), {})
    pending = starter.get("pending") or []
    if not pending:
        say("No starter rules waiting.")
        return 0
    try:
        session = await _session(url)
        filed = starter.setdefault("rule_ids", {})
        statuses = []
        for r in pending:
            if r["id"] in filed:
                continue
            # Personal scope: the rules reach only this person, so they may
            # switch them on themselves. No book is named or made here — the
            # server gets or creates the person's own one.
            body = dict(r["body"], scope="personal", activate=bool(args.activate))
            try:
                reply = await call(session, "create_rule", body, "rule_id")
            except ToolRefused as exc:
                # The server refuses a same-book twin (#1462): the rule is
                # already there from an earlier run on another machine.
                if "already" in str(exc).lower():
                    filed[r["id"]] = None
                    continue
                say(f"✗ {r['body']['title']}: not filed ({str(exc)[:160]})")
                continue
            rule_id = reply.get("rule_id") or reply.get("id")
            if reply.get("rulebook_id"):    # the personal book, made on the first rule
                starter["rulebook_id"] = reply["rulebook_id"]
            status = reply.get("status", "proposed")
            # A re-file comes back `unchanged` and writes nothing, activate
            # included: a rule left proposed by an earlier "Not now" is
            # switched on the way the companion's Activate button does it.
            if args.activate and status != "active" and rule_id:
                status = rule_decide.decide(rule_id, "activate").get("outcome", status)
            filed[r["id"]] = rule_id
            statuses.append(status)
            save_state(state)
        starter.pop("pending", None)
        repo_state["done_at"] = time.time()
        save_state(state)
        on = sum(1 for s in statuses if s == "active")
        if args.activate:
            say(f"✓ {on} starter rule{'s' if on != 1 else ''} on, for you only")
        else:
            n = len(statuses)
            say(f"✓ {n} starter rule{'s' if n != 1 else ''} filed, off until you turn "
                f"{'them' if n != 1 else 'it'} on in Studio")
        guide = guide_url(url) or ""
        studio = guide.rsplit("/plugin", 1)[0]
        if studio:
            say(f"  Studio: {studio}")
        return 0
    except Exception:  # noqa: BLE001
        log(traceback.format_exc())
        say("✗ The rules could not be filed. Run this command again; details are in "
            f"{_CACHE_DIR / 'onboard.log'}.")
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--repo", default=".")
    run.add_argument("--host", choices=("claude-code", "codex", "cursor"))
    rules = sub.add_parser("rules")
    rules.add_argument("--repo", default=".")
    which = rules.add_mutually_exclusive_group(required=True)
    which.add_argument("--activate", action="store_true")
    which.add_argument("--propose", action="store_true")
    args = ap.parse_args(argv)
    return asyncio.run(cmd_run(args) if args.cmd == "run" else cmd_rules(args))


if __name__ == "__main__":
    raise SystemExit(main())
