"""rules-from-sessions: the shipped miner must parse, resolve the plugin's scripts/ RELATIVE
to its own location (no env var), and run to completion on an empty HOME (zero
sessions, no book, no facets) — the state a fresh teammate is in."""
from __future__ import annotations
import json, os, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "memhub" / "skills" / "rules-from-sessions" / "scripts" / "mine_sessions.py"

def _run(*extra, home):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_PLUGIN_ROOT", "MEMHUB_PLUGIN_SCRIPTS")}
    env["HOME"] = home
    return subprocess.run([sys.executable, str(SCRIPT), *extra], capture_output=True, text=True, env=env, cwd=home, timeout=120)

def main() -> int:
    fails = 0
    with tempfile.TemporaryDirectory() as home:
        p = _run("--help", home=home)
        ok = p.returncode == 0 and "--rule-file" in p.stdout and "--claude-md" in p.stdout
        print(("ok  " if ok else "FAIL"), "--help lists --rule-file / --claude-md"); fails += not ok

        out = Path(home) / "mine-out"
        cand = Path(home) / "cand.json"
        cand.write_text(json.dumps({"title": "probe-rule", "matcher": {"event": "bash", "command_rx": "git\\s+push\\b", "warn_once_per": "session"}}))
        md = Path(home) / "CLAUDE.md"; md.write_text("# Rules\n\n- Never pipe pytest into tail when deciding pass/fail.\n")
        facets = Path(home) / "facets.json"
        facets.write_text(json.dumps([{"session_id": "abc", "host": "codex", "repo": "r", "underlying_goal": "g", "outcome": "mostly",
                                       "friction": [{"category": "wrong_source", "detail": "answered from README", "evidence_turn": 2}, {"category": "bogus_label", "detail": "x"}],
                                       "corrections": ["read the code"]}]))
        partial = Path(home) / "partial.json"; partial.write_text(json.dumps({"title": "partial-ordering", "ordering": {"gated_command_rx": "git push"}}))
        book = Path(home) / ".config" / "memhub-plugin" / "rulebook" / "book"; book.mkdir(parents=True)
        (book / "x.json").write_text(json.dumps({"rules": [{"delivery": "agent_hook", "matcher": {"event": "bash", "command_rx": "x"}},   # no title: must be skipped, not fatal
                                                            {"title": "ok-rule", "rule_id": "r1", "delivery": "agent_hook", "mode": "advise", "version": "not-a-number", "matcher": {"event": "bash", "command_rx": "git\\s+push\\b", "warn_once_per": "session"}, "scope_repos": [], "scope_paths": [], "scope_exclude_paths": []}]}))
        p = _run("--out", str(out), "--rule-file", str(cand), "--rule-file", str(partial), "--claude-md", str(md), "--facets", str(facets), home=home)
        ok = p.returncode == 0 and "sessions read" in p.stdout and "probe-rule" in p.stdout and "CLAUDE.MD SEED" in p.stdout and "FACETS SEED" in p.stdout and "wrong_source" in p.stdout and "unknown friction category ['bogus_label']" in p.stderr and (out / "digests").is_dir()
        print(("ok  " if ok else "FAIL"), "empty HOME: runs, backtests --rule-file, seeds from --claude-md and --facets (fixed vocab enforced), writes digests/"); fails += not ok
        if not ok: print(p.stdout[-800:], p.stderr[-800:])
        ok = (out / "proposals.json").is_file() and any(r.get("title") == "probe-rule" for r in json.load(open(out / "proposals.json")))
        print(("ok  " if ok else "FAIL"), "proposals.json carries the --rule-file candidate"); fails += not ok
        ok = "ordering needs required_command_rx" in p.stderr and "partial-ordering" not in p.stdout and "ok-rule" in p.stdout
        print(("ok  " if ok else "FAIL"), "partial ordering body → [warn] + skipped; cache row without title skipped; non-numeric version tolerated"); fails += not ok
        if not ok: print(p.stdout[-600:], p.stderr[-400:])
        ok = "several memhub plugin copies" not in p.stderr and "not found" not in p.stderr
        print(("ok  " if ok else "FAIL"), "plugin scripts resolved relative to the skill (no env var, no cache lookup)"); fails += not ok
        if not ok: print("stderr:", p.stderr[-400:])
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
