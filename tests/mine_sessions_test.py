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
        anchor = Path(home) / "anchor.json"; anchor.write_text(json.dumps({"title": "probe-anchor", "delivery": "anchor_recall", "anchors": ["ContextBusConfig"]}))
        sess_ord = Path(home) / "sess.json"; sess_ord.write_text(json.dumps({"title": "probe-session-ordering", "ordering": {"required_command_rx": "git\\s+fetch\\b", "gated_command_rx": "git\\s+log\\b[^\\n]*origin/", "armed_by_events": ["session"]}}))
        cands = Path(home) / "cands.json"; cands.write_text(json.dumps([
            {"title": "declared-probe", "matcher": {"event": "bash", "command_rx": "never-happens-xyz\\b"}, "claude_md": {"heading": "Rules", "text": "Never pipe pytest into tail when deciding pass/fail."}, "source_ref": "CLAUDE.md@abc#rules", "did": "Claude did the declared thing", "what": "Claude is warned"},
            "not-a-dict"]))
        book = Path(home) / ".config" / "memhub-plugin" / "rulebook" / "book"; book.mkdir(parents=True)
        (book / "x.json").write_text(json.dumps({"rules": [{"delivery": "agent_hook", "matcher": {"event": "bash", "command_rx": "x"}},   # no title: must be skipped, not fatal
                                                            {"title": "ok-rule", "rule_id": "r1", "delivery": "agent_hook", "mode": "advise", "version": "not-a-number", "matcher": {"event": "bash", "command_rx": "git\\s+push\\b", "warn_once_per": "session"}, "scope_repos": [], "scope_paths": [], "scope_exclude_paths": []}]}))
        p = _run("--out", str(out), "--rule-file", str(cand), "--rule-file", str(partial), "--rule-file", str(anchor), "--rule-file", str(sess_ord), "--candidates", str(cands), "--claude-md", str(md), "--facets", str(facets), home=home)
        ok = p.returncode == 0 and "sessions read" in p.stdout and "probe-rule" in p.stdout and "WHAT CLAUDE.MD DECLARES" in p.stdout and "WHAT WENT WRONG" in p.stdout and "PROPOSED RULES" in p.stdout and "wrong_source" in p.stdout and "unknown friction category ['bogus_label']" in p.stderr and (out / "digests").is_dir()
        print(("ok  " if ok else "FAIL"), "empty HOME: runs, backtests --rule-file, seeds from --claude-md and --facets (fixed vocab enforced), writes digests/"); fails += not ok
        if not ok: print(p.stdout[-800:], p.stderr[-800:])
        rows = json.load(open(out / "proposals.json")) if (out / "proposals.json").is_file() else []
        probe = next((r for r in rows if r.get("title") == "probe-rule"), None)
        ok = probe is not None and probe.get("trigger") == "before_action" and probe.get("delivery") == "agent_hook" and "verdict" in probe
        print(("ok  " if ok else "FAIL"), "proposals.json carries the --rule-file candidate with trigger / delivery / verdict"); fails += not ok
        anchor = next((r for r in rows if r.get("title") == "probe-anchor"), None)
        ok = anchor is not None and anchor.get("trigger") == "on_identifier" and anchor.get("delivery") == "anchor_recall" and "when the name comes up" in p.stdout
        print(("ok  " if ok else "FAIL"), "an anchors body lands under 'When a name comes up' (anchor_recall), unmeasured"); fails += not ok
        sess = next((r for r in rows if r.get("title") == "probe-session-ordering"), None)
        ok = sess is not None and sess.get("needs_engine") is True and "session-armed" in sess.get("verdict", "") and sess["predicate"]["ordering"]["armed_by_events"] == ["session"]
        print(("ok  " if ok else "FAIL"), "a session-armed ordering body is replayed and flagged as needing the engine mode"); fails += not ok
        decl = next((r for r in rows if r.get("title") == "declared-probe"), None)
        ok = decl is not None and decl.get("origin") == "claude_md" and decl["claude_md"]["text"].startswith("Never pipe pytest") and decl["source_ref"] == "CLAUDE.md@abc#rules" and decl["verdict"].startswith("Declared in CLAUDE.md") and "DECLARED IN CLAUDE.MD, NOT BROKEN HERE" in p.stdout
        print(("ok  " if ok else "FAIL"), "--candidates list: a body carrying its CLAUDE.md sentence is origin=claude_md, keeps its source_ref, and lands in the declared-not-broken section"); fails += not ok
        if not ok: print(decl, p.stdout[-600:])
        ok = "ordering needs required_command_rx" in p.stderr and "partial-ordering" not in p.stdout and "ok-rule" in p.stdout
        print(("ok  " if ok else "FAIL"), "partial ordering body → [warn] + skipped; cache row without title skipped; non-numeric version tolerated"); fails += not ok
        if not ok: print(p.stdout[-600:], p.stderr[-400:])
        hooks = [r for r in rows if r.get("lane") == "hook"]
        sed_hook = next((r for r in hooks if r.get("title") == "sed-range-delete-then-revert"), None)
        cmd = ((sed_hook or {}).get("settings_snippet") or {}).get("hooks", {}).get("PreToolUse", [{}])[0].get("hooks", [{}])[0].get("command", "")
        ok = sed_hook is not None and "[0-9]+" in cmd and "\\d" not in cmd and "[[:space:]]" in cmd
        print(("ok  " if ok else "FAIL"), "emitted PreToolUse snippet is POSIX ERE (\\d -> [0-9], \\s -> [[:space:]]), not a Python regex grep -E cannot run"); fails += not ok
        if not ok: print(cmd)
        ok = "several memhub plugin copies" not in p.stderr and "not found" not in p.stderr
        print(("ok  " if ok else "FAIL"), "plugin scripts resolved relative to the skill (no env var, no cache lookup)"); fails += not ok
        if not ok: print("stderr:", p.stderr[-400:])
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
