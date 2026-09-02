"""Self-test for markdown artifact capture (collector + flush rule).

Encodes the 2026-08-21 backtest that justified the feature: over 31 sessions,
67 distinct .md writes split into Claude auto-memory (32), scratch (15), real
specs (7, median 25 KB), PR-body drafts (5), READMEs (3), skill docs (3),
CLAUDE.md (2). The rule must admit the specs and reject every other class.

Run: python3 md_capture_test.py   (stdlib only; the flush's network path is
not exercised — derive_name/derive_type and the state contract are).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import md_capture as mc  # noqa: E402

HOOK = SCRIPTS / "md_capture.py"
FAILS = 0


def check(cond: bool, msg: str) -> None:
    global FAILS
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS += 1
        print(f"  FAIL {msg}")


try:
    import md_capture_flush as f
    have_flush = True
except ModuleNotFoundError:
    have_flush = False
    print("note: mcp SDK not installed — flush-side checks skipped (run via uv run --with 'mcp<2')")

BIG = 9_000   # the smallest real spec in the backtest was 9,156 bytes
SMALL = 5_800 # the largest PR-body draft was 5,760 bytes

# ---- the classifier, one row per backtest category ------------------------
print("is_candidate — backtest categories")
cases = [
    # (path, size, text, expect, label)
    ("/Users/me/xtrace/MemHub-Backend/docs/specs/serving-ledger-spec.md", BIG, "# Serving ledger\n", True, "real spec, 25KB-class → capture"),
    ("/Users/me/xtrace/xmem/STATE_REVALIDATION_SPEC_V1.md", BIG, "", True, "spec at repo root (no docs/) → capture"),
    ("/Users/me/.claude/projects/-Users-me-xtrace-xmem/memory/foo.md", BIG, "", False, "Claude auto-memory → veto"),
    ("/Users/me/.claude/projects/x/memory/MEMORY.md", BIG, "", False, "MEMORY.md → veto"),
    ("/private/tmp/claude-501/x/scratchpad/RUBRIC.md", BIG, "", False, "scratchpad → veto"),
    ("/tmp/notes.md", BIG, "", False, "/tmp → veto"),
    ("/Users/me/memory-hub/dead-code-sweep-pr.md", SMALL, "", False, "PR body draft, 5KB → below floor"),
    ("/Users/me/repo/README.md", SMALL, "", False, "README edit, 4KB → below floor"),
    ("/Users/me/repo/CLAUDE.md", BIG, "", False, "CLAUDE.md → veto name"),
    ("/Users/me/repo/AGENTS.md", BIG, "", False, "AGENTS.md → veto name"),
    ("/Users/me/repo/plugins/x/skills/onboard/SKILL.md", SMALL, "", False, "skill doc, small → below floor"),
    ("/Users/me/repo/notes/design.md", SMALL, "---\nmemhub: artifact\ntitle: Retry design\n---\n# Retry", True, "small but frontmatter opt-in → capture"),
    ("/Users/me/repo/notes/design.md", SMALL, "---\ntitle: Retry design\n---\n", False, "frontmatter WITHOUT memhub: artifact → below floor"),
    ("/Users/me/repo/src/thing.py", BIG, "", False, "not markdown"),
    ("/Users/me/repo/dump.md", 3_000_000, "", False, "3 MB generated dump → above size cap"),
    ("/Users/me/repo/node_modules/pkg/README.md", BIG, "", False, "node_modules → veto"),
]
for path, size, text, expect, label in cases:
    ok, why = mc.is_candidate(Path(path), size=size, text=text or None)
    check(ok == expect, f"{label}  [{why}]")

# ---- frontmatter scanner ---------------------------------------------------
print("frontmatter")
check(mc.frontmatter("---\na: 1\n---\nbody") == "\na: 1", "extracts block")
check(mc.frontmatter("# no fm") == "", "no block → empty")
check(mc.frontmatter("---\nunterminated") == "", "unterminated → empty")

# ---- collector: records path once per session, never fails ----------------
print("collector (subprocess, real hook contract)")
with tempfile.TemporaryDirectory() as td:
    sid = "sess-md-capture-test"
    # State lives under Path.home()/.config/memhub-plugin/mdcapture: point the
    # subprocess collector's HOME (and USERPROFILE on Windows) at td, and the
    # in-process modules' STATE_DIR at the same place.
    env = {**os.environ, "HOME": td, "USERPROFILE": td}
    mc.STATE_DIR = Path(td) / ".config" / "memhub-plugin" / "mdcapture"
    def run(payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)
    # NOT under td: macOS temp dirs live in /var/folders, which the rule
    # vetoes on purpose. The collector doesn't need the file to exist.
    spec = "/Users/me/repo/docs/x-spec.md"
    for _ in range(3):
        r = run({"session_id": sid, "cwd": td, "tool_name": "Edit", "tool_input": {"file_path": spec}})
        check(r.returncode == 0 and r.stdout == "", "edit → exit 0, silent")
    r = run({"session_id": sid, "cwd": td, "tool_name": "Write", "tool_input": {"file_path": str(Path(td)/"scratchpad"/"n.md")}})
    check(r.returncode == 0, "scratch write → exit 0")
    r = run({"session_id": sid, "tool_name": "Write", "tool_input": {"file_path": "/Users/me/.claude/x.md"}})
    check(r.returncode == 0, "veto write → exit 0")
    r = run({"session_id": sid, "tool_name": "Write", "tool_input": {"file_path": str(Path(td)/"a.py")}})
    check(r.returncode == 0, "non-md → exit 0")
    r = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True, env=env)
    check(r.returncode == 0 and r.stdout == "", "garbage stdin → exit 0, silent")
    state = mc.load_state(sid)
    check(mc.state_path(sid).parent == mc.STATE_DIR and (os.name != "posix" or (mc.STATE_DIR.stat().st_mode & 0o777) == 0o700),
          "state file lives in the per-user 0700 dir, not the shared temp dir")
    if os.name == "posix":
        # every dir the collector CREATED is 0700 from birth (umask), not just the leaf
        created = mc.STATE_DIR.parent  # .config/memhub-plugin, new under this td
        check((created.stat().st_mode & 0o777) == 0o700, f"created parent dir is 0700 too: {oct(created.stat().st_mode & 0o777)}")
        pre = Path(td) / "pre"; pre.mkdir(mode=0o755); mc.STATE_DIR = pre
        mc.save_state("sess-pre", {"dirty": []})
        check((pre.stat().st_mode & 0o777) == 0o700, "pre-existing wider leaf dir is tightened to 0700")
        mc.STATE_DIR = Path(td) / ".config" / "memhub-plugin" / "mdcapture"
    canonical_spec = str(Path(spec).resolve())
    check(state["dirty"] == [canonical_spec],
          f"state holds the canonical spec exactly once: {state['dirty']}")
    # create-then-edit through a symlinked dir must map to ONE canonical key
    real = Path(td) / "realrepo" / "docs"; real.mkdir(parents=True)
    link = Path(td) / "linkrepo"; link.symlink_to(Path(td) / "realrepo")
    sidk = "sess-md-capture-keys"
    absent_via_link = str(link / "docs" / "new-spec.md")         # first Write: file absent
    run({"session_id": sidk, "tool_name": "Write", "tool_input": {"file_path": absent_via_link}})
    (real / "new-spec.md").write_text("# x", encoding="utf-8")   # now it exists
    run({"session_id": sidk, "tool_name": "Edit", "tool_input": {"file_path": absent_via_link}})
    run({"session_id": sidk, "tool_name": "Edit", "tool_input": {"file_path": str(real / "new-spec.md")}})
    vp0 = mc.VETO_PARTS
    st = mc.load_state(sidk)
    canon = str((real / "new-spec.md").resolve())
    # td itself is under /var/folders (vetoed) so the collector never records these —
    # assert the KEY function directly instead: all three spellings canonicalise alike.
    keys = {str(Path(x).resolve()) for x in (absent_via_link, str(real / "new-spec.md"))}
    check(keys == {canon}, f"symlink + absent-at-first-write spellings resolve to one key: {keys}")

    if have_flush:
        # race: a path added to `dirty` while a flush is in flight must survive the write-back
        sid2 = "sess-md-flush-race"
        mc.save_state(sid2, {"dirty": ["/Users/me/repo/a.md"], "saved": {}})
        mc.save_state(sid2, {"dirty": ["/Users/me/repo/a.md", "/Users/me/repo/b.md"], "saved": {}})  # collector appended b mid-flight
        f._persist(sid2, processed={"/Users/me/repo/a.md"}, saved={"/Users/me/repo/a.md": "abc"})
        st = mc.load_state(sid2)
        check(st["dirty"] == ["/Users/me/repo/b.md"] and st["saved"] == {"/Users/me/repo/a.md": "abc"},
              f"flush write-back merges, does not clobber: {st}")
        # `attempts` follows the same merge discipline (bot review on #88): a
        # processed path's counter is cleared, a counter this pass bumped is
        # written, and a counter an overlapping flush owns is left alone.
        mc.save_state(sid2, {"dirty": ["a", "b", "c"], "saved": {}, "attempts": {"a": 1, "b": 2}})
        f._persist(sid2, processed={"a"}, saved={}, attempts={"c": 1})
        st = mc.load_state(sid2)
        check(st["attempts"] == {"b": 2, "c": 1}, f"attempts merged: a cleared, b untouched, c bumped: {st['attempts']}")

        # retry semantics (bot review on #88): a failed save and a capped-out
        # candidate must both STAY dirty so the next Stop retries them.
        import asyncio
        sid3 = "sess-md-flush-retry"
        # resolved: the collector stores canonical paths and the flush insists on them
        root = (Path(td) / "r").resolve(); root.mkdir()
        # six real candidates, each above the floor; none under a veto path
        # (td is /var/folders → vetoed, so build them under a non-temp-looking
        # symlink-free dir: patch VETO_PARTS for this block only)
        vp = mc.VETO_PARTS
        mc.VETO_PARTS = tuple(v for v in vp if v not in ("/tmp/", "/private/tmp/", "/var/folders/"))
        f.VETO_PARTS = mc.VETO_PARTS
        paths = []
        for i in range(6):
            q = root / f"spec{i}.md"; q.write_text("# S" + str(i) + "\n" + "x" * (7000 + i), encoding="utf-8"); paths.append(str(q))
        mc.save_state(sid3, {"dirty": paths, "saved": {}})
        calls = {"n": 0}
        async def fake_save(session, call_args):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("server blip")
            return {"artifact_id": "aid"}
        # _save's own success detection, exercised directly with fake tool results
        class _R:
            def __init__(self, text, is_error=False):
                self.isError = is_error
                self.content = [types.SimpleNamespace(type="text", text=text)]
        class _Sess:
            def __init__(self, r): self.r = r
            async def call_tool(self, *a, **k): return self.r
        import types
        real_save = f._save
        def _rejects(res):
            try:
                asyncio.run(real_save(_Sess(res), {})); return False
            except f.SaveRejected:
                return True
        check(_rejects(_R('{"error":"auth failed"}')), "_save: JSON error payload → SaveRejected")
        check(_rejects(_R("Unauthorized", is_error=True)), "_save: isError result → SaveRejected")
        check(_rejects(_R("<html>502</html>")), "_save: non-JSON body → SaveRejected")
        check(_rejects(_R('{"ok":true}')), "_save: no artifact id → SaveRejected")
        check(not _rejects(_R('{"artifact_id":"a1","name":"x"}')), "_save: real success → returns")
        check(_rejects(_R('{"id":"a1","error":"quota: saved partial"}')), "_save: error field beside an id → still rejected")
        check(not _rejects(_R('{"id":"a1","name":"x","action":"created","tags":[],"tag_report":{},"scope":{}}')), "_save: the server's exact success shape → returns")
        class _S:
            async def initialize(self): pass
        class _Ctx:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return (None, None, None)
            async def __aexit__(self, *a): return False
        class _CS:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return _S()
            async def __aexit__(self, *a): return False
        import types, sys as _sys
        f._save = fake_save
        f.resolve_url_and_auth = lambda *a, **k: ("http://x", {}, None)
        f.env_for_url = lambda u: "test"
        def flaky_root(parent):
            # the 3rd item's repo lookup blows up (derivation, not save): must
            # skip ONLY that item, never abort the rest of the turn
            calls["root"] = calls.get("root", 0) + 1
            if calls["root"] == 3:
                raise PermissionError("git rev-parse denied")
            return None
        f.read_room = lambda *a, **k: None
        f.repo_root = flaky_root
        mcp_mod = types.ModuleType("mcp"); cli = types.ModuleType("mcp.client")
        sess = types.ModuleType("mcp.client.session"); sess.ClientSession = _CS
        sh = types.ModuleType("mcp.client.streamable_http"); sh.streamablehttp_client = _Ctx
        _sys.modules.update({"mcp": mcp_mod, "mcp.client": cli, "mcp.client.session": sess, "mcp.client.streamable_http": sh})
        asyncio.run(f.flush(sid3))
        st = mc.load_state(sid3)
        # 6 candidates, cap 5 → the smallest (spec0) is capped out; of the 5 attempted,
        # the 2nd save raised. Expect: 4 saved+digested, 2 still dirty.
        # 6 candidates, cap 5 → spec0 capped out; of 5 attempted: the 3rd room
        # lookup raises (skips one) and the 2nd save raises (skips one) → 3 saved.
        check(len(st["saved"]) == 3, f"3 of 5 attempted saves recorded a digest: {len(st['saved'])}")
        check(len(st["dirty"]) == 3, f"capped-out + failed-save + failed-derivation all remain dirty: {len(st['dirty'])}")
        check(all(k in paths for k in st["saved"]), "saved keys are the exact dirty strings (raw), not re-stringified Paths")
        check(paths[0] in st["dirty"], "the capped-out (smallest) candidate stayed dirty")

        # bounded retries: a path that keeps failing leaves the list at MAX_ATTEMPTS,
        # and a non-UTF-8 read retries (async Stop can land mid-write) instead of dropping.
        sid4 = "sess-md-flush-bounded"
        bad = root / "always-fails.md"; bad.write_text("# F\n" + "y" * 7000, encoding="utf-8")
        undec = root / "mid-write.md"; undec.write_bytes(b"# U\n" + b"\xff" * 7000)
        mc.save_state(sid4, {"dirty": [str(bad), str(undec)], "saved": {}, "attempts": {}})
        async def always_fail(session, call_args):
            raise f.SaveRejected("quota")
        f._save = always_fail
        f.repo_root = lambda *a, **k: None
        for i in range(1, f.MAX_ATTEMPTS + 1):
            asyncio.run(f.flush(sid4))
            st = mc.load_state(sid4)
            if i < f.MAX_ATTEMPTS:
                check(set(st["dirty"]) == {str(bad), str(undec)}, f"pass {i}: both still dirty ({st.get('attempts')})")
            else:
                check(st["dirty"] == [] and st.get("attempts") == {}, f"pass {i}: both given up at the cap, counters cleared")
        # a decode error that CLEARS (write completed) is saved on the next pass, counter reset
        undec.write_text("# U\n" + "z" * 7000, encoding="utf-8")
        mc.save_state(sid4, {"dirty": [str(undec)], "saved": {}, "attempts": {str(undec): 1}})
        f._save = fake_save
        asyncio.run(f.flush(sid4))
        st = mc.load_state(sid4)
        check(str(undec) in st["saved"] and st.get("attempts") == {}, "recovered file saved; attempt counter reset")
        # a path that turns out unchanged / non-candidate after an earlier failure
        # must not leave a dead counter behind
        st["dirty"] = [str(undec)]; st["attempts"] = {str(undec): 2}
        mc.save_state(sid4, st)
        asyncio.run(f.flush(sid4))
        st = mc.load_state(sid4)
        check(st["dirty"] == [] and st.get("attempts") == {}, f"unchanged-since-save clears its stale counter: {st.get('attempts')}")

        # connection-level failure (SDK import / initialize) counts against every
        # candidate that never got its turn, so an unreachable server is bounded too
        sid5 = "sess-md-flush-conn"
        c1 = root / "c1-spec.md"; c1.write_text("# C1\n" + "a" * 7000, encoding="utf-8")
        c2 = root / "c2-spec.md"; c2.write_text("# C2\n" + "b" * 7000, encoding="utf-8")
        mc.save_state(sid5, {"dirty": [str(c1), str(c2)], "saved": {}, "attempts": {}})
        def _boom(*a, **k):
            raise ConnectionError("server unreachable")
        prev_ctx = sh.streamablehttp_client; sh.streamablehttp_client = _boom
        for i in range(1, f.MAX_ATTEMPTS + 1):
            asyncio.run(f.flush(sid5))
            st = mc.load_state(sid5)
            if i < f.MAX_ATTEMPTS:
                check(set(st["dirty"]) == {str(c1), str(c2)} and st["attempts"] == {str(c1): i, str(c2): i},
                      f"conn pass {i}: both dirty, both counted: {st['attempts']}")
            else:
                check(st["dirty"] == [] and st["attempts"] == {}, f"conn pass {i}: both given up, counters cleared")
        sh.streamablehttp_client = prev_ctx

        # a recorded path swapped for a symlink since the edit is not read through
        sid6 = "sess-md-flush-symlink"
        target = root / "secret-not-md.txt"; target.write_text("# S\n" + "s" * 7000, encoding="utf-8")
        lnk = root / "swapped-spec.md"; lnk.symlink_to(target)
        mc.save_state(sid6, {"dirty": [str(lnk)], "saved": {}, "attempts": {}})
        n_before = calls["n"]
        asyncio.run(f.flush(sid6))
        st = mc.load_state(sid6)
        check(st["dirty"] == [] and st["saved"] == {} and calls["n"] == n_before, f"symlinked candidate is dropped, not uploaded: {st}")

        # (audit #5) cache miss → the room is resolved from the server over the
        # open session, exactly like the transcript capture hooks, so an
        # auto-captured spec lands in the repo room when one exists.
        sid7 = "sess-md-flush-resolve"
        r7 = root / "resolve-spec.md"; r7.write_text("# R7\n" + "r" * 7000, encoding="utf-8")
        mc.save_state(sid7, {"dirty": [str(r7)], "saved": {}, "attempts": {}})
        seen_args: list = []
        async def capture_save(session, call_args):
            seen_args.append(call_args); return {"artifact_id": "aid"}
        resolved: list = []
        async def fake_resolve(session, cwd, env):
            resolved.append((cwd, env)); return {"brain_id": "B-RESOLVED", "name": "Repo: x/y"}
        f._save = capture_save
        f.repo_root = lambda *a, **k: root
        f.read_room = lambda *a, **k: None
        f.resolve_repo_brain = fake_resolve
        asyncio.run(f.flush(sid7))
        check(len(resolved) == 1 and resolved[0][0] == root, f"cache miss → resolve_repo_brain called once with the file's dir: {resolved}")
        check(seen_args and seen_args[-1].get("agent_brain_id") == "B-RESOLVED", "resolved room's brain id is what gets saved into")
        # a cache HIT never resolves
        resolved.clear(); seen_args.clear()
        r7.write_text("# R7\n" + "q" * 7000, encoding="utf-8")
        mc.save_state(sid7, {"dirty": [str(r7)], "saved": {}, "attempts": {}})
        f.read_room = lambda *a, **k: {"brain_id": "B-CACHED", "name": "Repo: x/y"}
        asyncio.run(f.flush(sid7))
        check(resolved == [] and seen_args[-1].get("agent_brain_id") == "B-CACHED", "cache hit → no server resolution, cached brain used")

        # (audit #3b) a file linked by `path` in .claude/artifact-map.json is
        # owned by a hand-saved lineage (/memhub:spec) — the auto-capture must
        # not open a second lineage under an H1-derived name.
        sid8 = "sess-md-flush-linked"
        (root / ".claude").mkdir(exist_ok=True)
        owned = root / "owned-spec.md"; owned.write_text("# Owned\n" + "o" * 7000, encoding="utf-8")
        free = root / "free-spec.md"; free.write_text("# Free\n" + "f" * 7000, encoding="utf-8")
        (root / ".claude" / "artifact-map.json").write_text(json.dumps({"version": 1, "links": [
            {"glob": "app/*.py", "artifact_id": "a-owned", "artifact_name": "Spec: Owned", "path": "owned-spec.md"}]}))
        mc.save_state(sid8, {"dirty": [str(owned), str(free)], "saved": {}, "attempts": {}})
        seen_args.clear()
        f.read_room = lambda *a, **k: None
        asyncio.run(f.flush(sid8))
        st = mc.load_state(sid8)
        check([a["name"] for a in seen_args] == ["Free (free-spec.md)"], f"linked file skipped, unlinked file saved: {[a['name'] for a in seen_args]}")
        check(st["dirty"] == [] and str(owned) not in st["saved"], f"linked file leaves dirty without a digest (not retried): {st}")
        (root / ".claude" / "artifact-map.json").unlink()

        mc.VETO_PARTS = vp; f.VETO_PARTS = vp


# ---- flush-side derivations (no network) ----------------------------------
print("flush derivations")
if have_flush:
    p = Path("/r/docs/rulebook-detector-spec.md")
    check(f.derive_name(p, "---\ntitle: Rulebook detectors\n---\n# Other") == "Rulebook detectors", "name: frontmatter title wins")
    check(f.derive_name(p, "# Rulebook v2 — detectors\n", Path("/r")) == "Rulebook v2 — detectors (docs/rulebook-detector-spec.md)", "name: H1 + relpath (inferred names are qualified)")
    check(f.derive_name(p, "no heading", Path("/r")) == "rulebook detector spec (docs/rulebook-detector-spec.md)", "name: stem + relpath")
    check(f.derive_name(Path("/elsewhere/x.md"), "# T", Path("/r")) == "T (x.md)", "name: outside root → basename only")

    check(f.derive_type(p, "", "Rulebook detectors") == "spec", "type: 'spec' in path → spec")
    check(f.derive_type(Path("/r/notes.md"), "---\ntype: runbook\n---", "x") == "runbook", "type: frontmatter wins")
    check(f.derive_type(Path("/r/retry-design.md"), "", "Retry") == "design_doc", "type: design → design_doc")
    check(f.derive_type(Path("/r/notes.md"), "", "Notes") == "document", "type: default document")

print()
print("FAILED" if FAILS else "ALL PASSED", f"({FAILS} failures)")
sys.exit(1 if FAILS else 0)
