"""Self-test for the rulebook hook's three delivery lanes.

Covers the properties that make this safe to ship in everyone's harness:

* every failure path is SILENT and exit-0 — a broken hook must never touch a
  tool call or a session (missing rulebook, corrupt JSON, garbage stdin,
  non-git cwd);
* the session lane serves posture rules in full and everything else as one
  index line — never the whole book;
* the pre lane matches the shell-only segment (heredoc bodies stripped,
  shell after terminators kept), honors not_rx, and dedupes per
  fire_scope=session (the habituation guard);
* `shell_only` + `evaluate()` are pure and importable — the tests replay
  them, so what is tested is what runs;
* ordering rules arm on edits, discharge on a GREEN receipt only, gate the
  push, and keep state per (worktree, branch) — shared by sibling sessions and
  subagents, never leaking across branches;
* the post lane fires on failing result text, gated by cmd_rx;
* repo_scope filters rules to the repo the session is in;
* MEMHUB_RULEBOOK_BASE relocates the book cache, state and ledger together, so
  tests never touch the developer's real state.

Run: python3 rulebook_hook_test.py  (stdlib only).
"""
from __future__ import annotations

import json
import os
import re
import io
import subprocess
import sys
import tempfile

HOOK = os.path.join(os.path.dirname(__file__), "..",
                    "plugins", "memhub", "scripts", "rulebook_hook.py")
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def run(mode: str, payload: dict, env_extra: dict) -> tuple[int, str]:
    env = dict(os.environ, **env_extra)
    p = subprocess.run([sys.executable, HOOK, mode], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=30)
    return p.returncode, p.stdout


def seed_book(base, repo_name, rules):
    """Write a cached server book for `repo_name` under `base` (what the fetch
    lane would have cached). Rows in the pilot shape (an `on` key) pass
    straight through to_hook_rule."""
    import datetime as _dt
    import hashlib
    d = os.path.join(base, "book")
    os.makedirs(d, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", repo_name)[:60]
    h = hashlib.sha1(repo_name.encode("utf-8")).hexdigest()[:8]
    with open(os.path.join(d, f"{safe}-{h}.json"), "w", encoding="utf-8") as f:
        json.dump({"etag": "seed", "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                   "rules": rules}, f)


def ctx(out: str) -> str:
    if not out.strip():
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "xmem")           # fake git repo named xmem
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/test-branch\n")
        other = os.path.join(td, "otherrepo")
        os.makedirs(os.path.join(other, ".git"))

        rules = {"version": 1, "rules": [
            {"id": "posture-one", "on": "session", "fire_scope": "session",
             "repo_scope": "xmem", "text": "Posture text", "why": "posture why"},
            {"id": "bash-rule", "on": "bash", "rx": r"forbidden-cmd",
             "not_rx": r"allowed-context", "fire_scope": "session",
             "repo_scope": "any", "text": "Bash advisory", "why": "w"},
            {"id": "xmem-only", "on": "bash", "rx": r"xmem-only-cmd",
             "fire_scope": "session", "repo_scope": "xmem",
             "text": "Xmem advisory", "why": "w"},
            {"id": "post-rule", "on": "result", "rx": r"BOOM-ERROR",
             "cmd_rx": r"pytest", "fire_scope": "session", "repo_scope": "any",
             "text": "Post advisory", "why": "w"},
            {"id": "draft-rule", "on": "bash", "rx": r"forbidden-cmd", "status": "draft",
             "fire_scope": "session", "repo_scope": "any", "text": "DRAFT TEXT", "why": "w"},
        ]}
        for r in rules["rules"]:
            r["version"] = 1
        seed_book(td, "xmem", rules["rules"])
        seed_book(td, "otherrepo", rules["rules"])
        env = {"MEMHUB_RULEBOOK_BASE": td}

        # --- fail-open properties -----------------------------------------
        rc, out = run("pre", {"cwd": "/", "tool_name": "Bash",
                              "tool_input": {"command": "forbidden-cmd"}}, env)
        check("non-git cwd is silent", rc == 0 and out.strip() == "")

        rc, out = run("session", {"cwd": repo},
                      {"MEMHUB_RULEBOOK_BASE": os.path.join(td, "empty-base")})
        check("no cached book is silent exit-0", rc == 0 and out.strip() == "")

        badbase = os.path.join(td, "bad-base")
        seed_book(badbase, "xmem", [])
        with open(os.path.join(badbase, "book", os.listdir(os.path.join(badbase, "book"))[0]), "w", encoding="utf-8") as f:
            f.write("{not json")
        rc, out = run("pre", {"cwd": repo, "tool_name": "Bash",
                              "tool_input": {"command": "forbidden-cmd"}},
                      {"MEMHUB_RULEBOOK_BASE": badbase})
        check("corrupt cached book is silent exit-0", rc == 0 and out.strip() == "")

        p = subprocess.run([sys.executable, HOOK, "pre"], input="}}garbage",
                           capture_output=True, text=True,
                           env=dict(os.environ, **env), timeout=30)
        check("garbage stdin is silent exit-0",
              p.returncode == 0 and p.stdout.strip() == "")

        # --- session lane --------------------------------------------------
        rc, out = run("session", {"cwd": repo, "session_id": "s1"}, env)
        c = ctx(out)
        check("session: posture rule served in full", "Posture text" in c)
        check("session: active rules -> index line, not full text",
              "3 rules armed" in c and "Bash advisory" not in c, c)

        rc, out = run("session", {"cwd": other, "session_id": "s1"}, env)
        c = ctx(out)
        check("session: repo_scope filters posture + count",
              "Posture text" not in c and "2 rules armed" in c, c)

        # --- pre lane ------------------------------------------------------
        base = {"cwd": repo, "session_id": "s2", "tool_name": "Bash"}
        rc, out = run("pre", dict(base, tool_input={"command": "run forbidden-cmd now"}), env)
        check("pre: bash rule fires", "[bash-rule]" in ctx(out))
        check("pre: a status=draft rule never fires (not activated = unarmed)", "[draft-rule]" not in ctx(out))

        rc, out = run("pre", dict(base, tool_input={"command": "run forbidden-cmd now"}), env)
        check("pre: fire_scope=session dedupes the second call", out.strip() == "")

        rc, out = run("pre", dict(base, session_id="s3",
                                  tool_input={"command": "forbidden-cmd in allowed-context"}), env)
        check("pre: not_rx exempts", out.strip() == "")

        rc, out = run("pre", dict(base, session_id="s4",
                                  tool_input={"command": "cat > f <<'EOF'\nforbidden-cmd\nEOF"}), env)
        check("pre: heredoc body is not matched by default", out.strip() == "")

        rc, out = run("pre", {"cwd": other, "session_id": "s5", "tool_name": "Bash",
                              "tool_input": {"command": "xmem-only-cmd"}}, env)
        check("pre: repo_scope=xmem stays silent in another repo", out.strip() == "")

        # --- post lane -----------------------------------------------------
        rc, out = run("post", dict(base, session_id="s6",
                                   tool_input={"command": "uv run pytest tests/"},
                                   tool_response={"stdout": "BOOM-ERROR here"}), env)
        check("post: result rule fires on failing output", "[post-rule]" in ctx(out))

        rc, out = run("post", dict(base, session_id="s7",
                                   tool_input={"command": "ls"},
                                   tool_response={"stdout": "BOOM-ERROR here"}), env)
        check("post: cmd_rx gates the result rule", out.strip() == "")

        # --- ledger --------------------------------------------------------
        ledger = os.path.join(td, "ledger", "fires.jsonl")
        check("ledger written beside the relocated rulebook", os.path.isfile(ledger))
        if os.path.isfile(ledger):
            with open(ledger, encoding="utf-8") as f:
                rows = [json.loads(l) for l in f if l.strip()]
            ids = [r["rule_id"] for r in rows]
            check("ledger v2: one row per rule, rule_id column",
                  "bash-rule" in ids and "post-rule" in ids and all("rules" not in r for r in rows))
            check("ledger v2: client-minted fire_id, unique",
                  len({r["fire_id"] for r in rows}) == len(rows) and all(len(r["fire_id"]) == 36 for r in rows))
            check("ledger v2: hook_phase and mode are separate columns",
                  {r["hook_phase"] for r in rows} >= {"pre", "post", "session"} and
                  all(r["mode"] == "advise" for r in rows))
            check("ledger v2: full session_id, rule_version, tz-aware fired_at",
                  all(r["session_id"] in ("s1", "s2", "s6") for r in rows) and
                  all(r["rule_version"] == 1 for r in rows) and
                  all(re.search(r"([+-]\d\d:\d\d|Z)$", r["fired_at"]) for r in rows))
            check("ledger v2: schema_version file stamped",
                  open(os.path.join(td, "ledger", "schema_version"), encoding="utf-8").read().strip() == "2")

        # --- session lane: spec cap (15 / ~2k tokens), deterministic, logged ---
        seed_book(td, "capsrepo", [
            {"id": f"post-{i:02d}", "on": "session", "repo_scope": "any", "text": f"POSTURE {i:02d}", "why": "w", "title": f"Posture {i:02d}"}
            for i in range(17)] + [
            {"id": "post-big", "on": "session", "repo_scope": "any", "text": "BIG " * 3000, "why": "w", "title": "Posture 00 big"}])
        caps = os.path.join(td, "capsrepo"); os.makedirs(os.path.join(caps, ".git"))
        rc, out = run("session", {"cwd": caps, "session_id": "cap1"}, env)
        shown = [i for i in range(17) if f"POSTURE {i:02d}" in ctx(out)]
        check("session: at most 15 posture rules, chosen by title", shown == list(range(15)), str(shown))
        check("session: a rule that would blow the ~2k-token budget is not served", "BIG BIG" not in ctx(out))
        with open(os.path.join(td, "ledger", "fires.jsonl"), encoding="utf-8") as f:
            srows = [json.loads(l) for l in f if '"cap1"' in l]
        sup = sorted(r["rule_id"] for r in srows if r["mode"] == "suppressed")
        check("session: every rule past the cap or budget is logged suppressed with a session dedup key",
              sup == ["post-15", "post-16", "post-big"] and all(r["dedup_key"] == r["rule_id"] + "@session" for r in srows if r["mode"] == "suppressed"), str(sup))

        # --- cap → suppressed rows; converted_rx → conversions sidecar --------
        seed_book(td, "xmem", rules["rules"] + [
                {"id": f"cap-{i}", "on": "bash", "rx": r"capcmd", "fire_scope": "session",
                 "repo_scope": "any", "text": f"cap {i}", "why": "w",
                 **({"converted_rx": r"do-the-thing"} if i == 0 else {})}
                for i in range(3)])
        cenv = env
        cb = {"cwd": repo, "session_id": "c1", "tool_name": "Bash"}
        rc, out = run("pre", dict(cb, tool_input={"command": "capcmd"}), cenv)
        check("cap: at most MAX_ADVISE rules shown", ctx(out).count("[cap-") == 2)
        run("pre", dict(cb, tool_input={"command": "capcmd again"}), cenv)   # deduped → raw count
        run("post", dict(cb, tool_input={"command": "now do-the-thing"},
                         tool_response={"stdout": "ok"}), cenv)
        with open(os.path.join(td, "ledger", "fires.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip() and '"cap-' in l]
        modes = {r["rule_id"]: r["mode"] for r in rows}
        check("cap: the cut rule is logged mode=suppressed, never silently dropped",
              modes == {"cap-0": "advise", "cap-1": "advise", "cap-2": "suppressed"}, str(modes))
        check("cap: raw_matches_before_fire recorded", all(r["raw_matches_before_fire"] == 1 for r in rows))
        conv = os.path.join(td, "ledger", "conversions.jsonl")
        with open(conv, encoding="utf-8") as f:
            convs = [json.loads(l) for l in f if l.strip()]
        fid0 = next(r["fire_id"] for r in rows if r["rule_id"] == "cap-0")
        check("converted_rx: follow-up command writes a conversion for that fire_id",
              [c["fire_id"] for c in convs] == [fid0] and convs[0]["how"] == "converted_rx", str(convs))

        # --- shell_only + evaluate(): the pure engine ---
        sys.path.insert(0, os.path.dirname(HOOK))
        import rulebook_hook as H  # noqa: E402
        so = H.shell_only
        check("shell_only: plain command unchanged", so("git push origin main") == "git push origin main")
        check("shell_only: heredoc body dropped",
              "forbidden" not in so("cat > f <<'EOF'\nforbidden\nEOF"))
        chained = "git commit -F - <<'MSG'\nfix: forbidden\nMSG\ngit push -u origin fm"
        check("shell_only: shell AFTER a heredoc terminator is kept (the 44% FN class)",
              "git push -u origin fm" in so(chained) and "fix: forbidden" not in so(chained))
        check("shell_only: unquoted and <<- delimiters", "secret" not in so("cat <<-EOF\nsecret\nEOF\nls"))
        check("shell_only: a numeric bit-shift is not a heredoc", so("x=$((1 << 2))\ngit push") == "x=$((1 << 2))\ngit push")
        push = {"on": "bash", "rx": r"git\s+push"}
        check("evaluate: push after heredoc fires",
              H.evaluate(push, hook_phase="pre", tool="Bash", cmd=chained))
        check("evaluate: push only inside a body does not fire",
              not H.evaluate(push, hook_phase="pre", tool="Bash",
                             cmd="cat > n.md <<'EOF'\nrun git push\nEOF"))
        check("evaluate: match_heredoc_body opts in",
              H.evaluate(dict(push, match_heredoc_body=True), hook_phase="pre",
                         tool="Bash", cmd="cat > n.md <<'EOF'\nrun git push\nEOF"))
        hd = {"on": "bash", "rx": r"python3?\s+-?\s*<<", "match_heredoc_body": True, "body_rx": r"results\.json"}
        check("evaluate: body_rx rule — rx on the shell line, body_rx on the payload",
              H.evaluate(hd, hook_phase="pre", tool="Bash", cmd="python3 - <<'PY'\nload('results.json')\nPY")
              and not H.evaluate(hd, hook_phase="pre", tool="Bash", cmd="python3 - <<'PY'\nprint(1)\nPY")
              and not H.evaluate(hd, hook_phase="pre", tool="Bash",
                                 cmd="cat > spec.md <<'MD'\nuse python3 - << for results.json\nMD"))
        ws = {"on": "write_stdlib", "min_chars": 10, "path_not_rx": r"/tests?/"}
        check("evaluate: write_stdlib honours path_not_rx",
              H.evaluate(ws, hook_phase="pre", tool="Write", file_path="/r/pkg/m.py", body="import os\nimport re\nx=1")
              and not H.evaluate(ws, hook_phase="pre", tool="Write", file_path="/r/tests/t.py", body="import os\nimport re\nx=1"))
        check("bash_ok: exit_code wins; None is never ok; 'error:' in green output is ok",
              H.bash_ok({"exit_code": 0, "stdout": "3 failed earlier but fixed"}) and not H.bash_ok({"exit_code": 1})
              and not H.bash_ok(None) and H.bash_ok({"stdout": "warning: error: handled gracefully\n5 passed"})
              and not H.bash_ok({"stdout": "== 2 failed, 3 passed =="})
              and not H.bash_ok({"stdout": "collected 0 items / 1 error"})
              and not H.bash_ok({"stdout": "npm ERR! code ELIFECYCLE"})
              and not H.bash_ok({"stdout": "error[E0308]: mismatched types"}))
        check("bash_ok strict: a gate-mode receipt needs an explicit exit_code (text cannot forge green)",
              not H.bash_ok({"stdout": "5 passed"}, strict=True) and H.bash_ok({"stdout": "5 passed", "exit_code": 0}, strict=True))
        check("last_segment: receipt only counts as the final unpiped segment",
              H.last_segment("cd x && uv run pytest tests/architecture -q") == "uv run pytest tests/architecture -q"
              and H.last_segment("pytest tests/architecture; git push") == "git push")
        check("evaluate: broken regex is False, never raises",
              H.evaluate({"on": "bash", "rx": "("}, hook_phase="pre", tool="Bash", cmd="x") is False)

        # --- ordering engine: arm / receipt / gate, keyed by worktree ---------
        seed_book(td, "wtrepo", [
                {"id": "audit-before-push", "on": "ordering", "repo_scope": "any",
                 "ordering": {"required_command_rx": r"pytest\s+\S*tests/architecture",
                              "gated_command_rx": r"git\s+push",
                              "armed_by_events": ["edit", "write"],
                              "min_edits": 1, "display_name": "the architecture suite"},
                 "text": "Run the architecture suite before pushing", "why": "w"}])
        oenv = env
        wt = os.path.join(td, "wtrepo")
        os.makedirs(os.path.join(wt, ".git"))
        with open(os.path.join(wt, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/feat\n")
        edit_ev = {"cwd": wt, "session_id": "o1", "tool_name": "Edit",
                   "tool_input": {"file_path": os.path.join(wt, "pkg", "gate.py")}}
        suite = {"cwd": wt, "session_id": "o1", "tool_name": "Bash",
                 "tool_input": {"command": "uv run pytest tests/architecture -q"}}
        pushev = {"cwd": wt, "session_id": "o1", "tool_name": "Bash",
                  "tool_input": {"command": "git push -u origin feat"}}

        rc, out = run("pre", pushev, oenv)
        check("ordering: push with nothing armed → silent", out.strip() == "")
        run("post", edit_ev, oenv)                                   # arm
        rc, out = run("pre", pushev, oenv)
        check("ordering: push while armed → fires and names the file",
              "[audit-before-push]" in ctx(out) and "gate.py" in ctx(out), ctx(out))
        run("post", dict(suite, tool_response={"stdout": "1 failed", "exit_code": 1}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: RED suite run is not a receipt", "[audit-before-push]" in ctx(out))
        run("post", dict(suite, tool_input={"command": "uv run pytest tests/architecture -q; echo done"},
                         tool_response={"stdout": "3 passed\ndone", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: a receipt that is NOT the last segment does not discharge (exit status isn't its own)",
              "[audit-before-push]" in ctx(out))
        run("post", dict(suite, tool_input={"command": "uv run pytest tests/architecture -q | tail -3"},
                         tool_response={"stdout": "3 passed", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: a PIPED receipt does not discharge (pipe masks the status)",
              "[audit-before-push]" in ctx(out))
        run("post", dict(suite, tool_response={"stdout": "3 passed", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: green run discharges → push allowed", out.strip() == "")
        run("post", edit_ev, oenv)                                   # re-arm
        rc, out = run("pre", dict(pushev, session_id="o2-sibling"), oenv)
        check("ordering: state is per worktree, not per session (sibling sees the arm)",
              "[audit-before-push]" in ctx(out))
        run("post", dict(suite, session_id="subagent-9",
                         tool_response={"stdout": "ok", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: a SUBAGENT's green receipt discharges the parent's obligation",
              out.strip() == "")
        run("post", edit_ev, oenv)
        with open(os.path.join(wt, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/other\n")
        rc, out = run("pre", pushev, oenv)
        check("ordering: obligation survives `git checkout -b` (keyed by worktree, not branch)",
              "[audit-before-push]" in ctx(out))
        run("post", dict(suite, tool_input={"command": "uv run pytest tests/architecture -q &"},
                         tool_response={"stdout": "", "exit_code": 0}), oenv)
        rc, out = run("pre", pushev, oenv)
        check("ordering: a BACKGROUNDED receipt does not discharge", "[audit-before-push]" in ctx(out))
        run("post", dict(suite, tool_response={"stdout": "3 passed", "exit_code": 0}), oenv)
        oledger = os.path.join(td, "ledger", "fires.jsonl")
        with open(os.path.join(td, "ledger", "conversions.jsonl"), encoding="utf-8") as f:
            hows = [json.loads(l)["how"] for l in f if l.strip()]
        check("ordering: a discharge after a fire converts that fire (the conversion signal)",
              hows.count("discharged") == 3, str(hows))
        statefiles = [n for n in os.listdir(os.path.join(td, "state")) if n.startswith("wt-") and n.endswith(".json")]
        check("ordering: one state file per worktree, atomic (no temp leftovers)",
              len(statefiles) == 1 and not any(n.startswith(".wt-") for n in os.listdir(os.path.join(td, "state"))))

        # --- C3: the transcript marker (message_ref) ------------------------
        # The hook's payload carries no message id, so the marker is read from
        # the TAIL of transcript_path. It must be exact where it can be, and
        # null — never an exception — where it cannot.
        mbase = os.path.join(td, "msgref")
        seed_book(mbase, "xmem", [
            {"id": "mark-rule", "on": "bash", "rx": "marker-cmd", "fire_scope": "call",
             "repo_scope": "any", "text": "Marker advisory", "version": 1},
            {"id": "mark-result", "on": "result", "rx": "MARK-BOOM", "fire_scope": "call",
             "repo_scope": "any", "text": "Marker result advisory", "version": 1},
            {"id": "mark-posture", "on": "session", "repo_scope": "any",
             "text": "Marker posture", "version": 1},
        ])
        menv = {"MEMHUB_RULEBOOK_BASE": mbase}
        mledger = os.path.join(mbase, "ledger", "fires.jsonl")

        def transcript(name, records):
            path = os.path.join(td, name)
            with open(path, "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")
            return path

        def asst(uid, blocks):
            return {"type": "assistant", "uuid": uid, "message": {"content": blocks}}

        def refs(session):
            if not os.path.isfile(mledger):
                return []
            with open(mledger, encoding="utf-8") as f:
                return [json.loads(l)["message_ref"] for l in f
                        if l.strip() and json.loads(l)["session_id"] == session]

        U1 = "11111111-1111-1111-1111-111111111111"
        U2 = "22222222-2222-2222-2222-222222222222"
        U3 = "33333333-3333-3333-3333-333333333333"
        tp = transcript("t-exact.jsonl", [
            asst(U1, [{"type": "thinking", "thinking": "…"}]),
            asst(U2, [{"type": "tool_use", "name": "Bash", "input": {"command": "marker-cmd"}}]),
            asst(U3, [{"type": "text", "text": "trailing turn"}]),
        ])
        mev = {"cwd": repo, "tool_name": "Bash", "transcript_path": tp,
               "tool_input": {"command": "marker-cmd"}}
        run("pre", dict(mev, session_id="m1"), menv)
        check("message_ref: the tool_use record for THIS tool wins over a newer turn",
              refs("m1") == [U2], str(refs("m1")))

        tp2 = transcript("t-fallback.jsonl", [
            asst(U1, [{"type": "tool_use", "name": "Read", "input": {}}]),
            asst(U2, [{"type": "text", "text": "no tool_use for Bash here"}]),
        ])
        run("pre", dict(mev, session_id="m2", transcript_path=tp2), menv)
        check("message_ref: no name match falls back to the newest assistant record",
              refs("m2") == [U2], str(refs("m2")))

        run("post", dict(mev, session_id="m3",
                         tool_input={"command": "marker-cmd"},
                         tool_response={"stdout": "MARK-BOOM"}), menv)
        check("message_ref: pre and post resolve the SAME uuid for one call",
              refs("m3") == [U2], str(refs("m3")))

        # a single Write's tool_use record can exceed the 64 KiB first pass —
        # exactly the calls most likely to fire an edit rule
        big = transcript("t-big.jsonl", [
            asst(U1, [{"type": "text", "text": "small earlier turn"}]),
            asst(U3, [{"type": "tool_use", "name": "Bash",
                       "input": {"command": "marker-cmd " + "y" * 90000}}]),
        ])
        check("message_ref: the oversize record really does exceed the first window",
              os.path.getsize(big) > 64 * 1024)
        run("pre", dict(mev, session_id="m4", transcript_path=big), menv)
        check("message_ref: a tool_use record larger than 64 KiB is still found (1 MiB escalation)",
              refs("m4") == [U3], str(refs("m4")))

        degraded = {
            "m5": None,                                          # no transcript_path at all
            "m6": os.path.join(td, "does-not-exist.jsonl"),      # missing file
            "m7": transcript("t-empty.jsonl", []),               # empty file
        }
        with open(os.path.join(td, "t-garbage.jsonl"), "w", encoding="utf-8") as f:
            f.write("not json at all\n{\"type\": \"assistant\", trunc")
        degraded["m8"] = os.path.join(td, "t-garbage.jsonl")
        ok_silent = True
        for sid, path in degraded.items():
            ev = dict(mev, session_id=sid)
            ev["transcript_path"] = path
            if path is None:
                ev.pop("transcript_path")
            rc, out = run("pre", ev, menv)
            ok_silent = ok_silent and rc == 0 and "[mark-rule]" in ctx(out) and refs(sid) == [None]
        check("message_ref: missing/unreadable/empty/truncated transcript → null, rule still fires, exit 0",
              ok_silent)

        rc, out = run("session", {"cwd": repo, "session_id": "m9", "transcript_path": tp}, menv)
        check("message_ref: the session lane has no tool call, so no marker",
              "Marker posture" in ctx(out) and refs("m9") == [None], str(refs("m9")))

        check("message_ref: on the wire (C3), and a pre-existing row without it reads null",
              "message_ref" in H.WIRE_KEYS and H.wire_row({"fire_id": "f"})["message_ref"] is None)
        # C3 froze `message_ref`; the shipped ingest reads `source_message_id`
        # and silently stores null for the other name. The wire carries both.
        w = H.wire_row({"fire_id": "f", "message_ref": "u-1"})
        check("message_ref: the marker goes out under BOTH contract names, same value",
              w["message_ref"] == "u-1" and w["source_message_id"] == "u-1", str(w))
        check("message_ref: a legacy ledger row without the marker sends null under both",
              H.wire_row({"fire_id": "f"})["source_message_id"] is None)

        # --- fetch: the query C5 turned into a 400 -------------------------
        seen = {}

        class _Reply:
            status, data, etag = 200, {"rules": []}, "etag-1"

        class _Http:
            @staticmethod
            def rest(url, bearer, method, headers=None, timeout=None, body=None):
                seen["url"] = url
                return _Reply()

        fbase = os.path.join(td, "fetch")
        H.BASE, H.BOOK_DIR = fbase, os.path.join(fbase, "book")
        H._api = lambda: ("https://example.invalid", "tok", _Http)
        H.fetch_book("xmem")
        check("fetch: the hook view is requested with NO status filter "
              "(`status=active` is a 400 since C5, swallowed as 'keep the cache')",
              "view=hook" in seen.get("url", "") and "status=" not in seen.get("url", ""),
              seen.get("url", "<not called>"))

        # --- scope_paths / scope_exclude_paths (server shape → hook shape) --
        pbase = os.path.join(td, "pathscope")
        seed_book(pbase, "xmem", [
            {"rule_id": "scoped", "title": "Scoped", "statement": "Scoped advisory",
             "delivery": "agent_hook", "version": 1,
             "matcher": {"event": "edit", "path_rx": r"\.py$", "warn_once_per": "turn"},
             "scope_paths": ["src/**", "tests/*.py"],
             "scope_exclude_paths": ["**/vendor/**"]},
            {"rule_id": "scoped-bash", "title": "Scoped bash", "statement": "Scoped bash advisory",
             "delivery": "agent_hook", "version": 1,
             "matcher": {"event": "bash", "command_rx": "scoped-cmd", "warn_once_per": "turn"},
             "scope_paths": ["src/**"]},
            {"rule_id": "scoped-posture", "title": "Scoped posture",
             "statement": "Scoped posture advisory", "delivery": "session_context",
             "version": 1, "scope_paths": ["src/**"]},
        ])
        penv = {"MEMHUB_RULEBOOK_BASE": pbase}

        def edits(rel_target, sid):
            rc, out = run("pre", {"cwd": repo, "session_id": sid, "tool_name": "Write",
                                  "tool_input": {"file_path": os.path.join(repo, rel_target),
                                                 "content": "x = 1\n"}}, penv)
            return "Scoped advisory" in ctx(out)

        check("scope_paths: an included path fires", edits("src/a.py", "p1"))
        check("scope_paths: `src/**` crosses directories", edits("src/deep/b.py", "p2"))
        check("scope_paths: a path outside every glob is silent — the bug this fixes",
              not edits("docs/a.py", "p3"))
        check("scope_paths: `tests/*.py` does NOT cross a directory",
              not edits("tests/sub/b.py", "p4") and edits("tests/a.py", "p5"))
        check("scope_exclude_paths: exclusion wins over inclusion",
              not edits("src/vendor/x.py", "p6"))

        rc, out = run("pre", {"cwd": repo, "session_id": "p7", "tool_name": "Write",
                              "tool_input": {"file_path": os.path.join(td, "elsewhere", "a.py"),
                                             "content": "x = 1\n"}}, penv)
        check("scope_paths: a path outside the worktree matches no repo-relative glob",
              "Scoped advisory" not in ctx(out))

        rc, out = run("pre", {"cwd": repo, "session_id": "p8", "tool_name": "Bash",
                              "tool_input": {"command": "scoped-cmd now"}}, penv)
        check("scope_paths: a call with NO path is in scope (a scope the hook cannot "
              "evaluate never silences a rule)", "Scoped bash advisory" in ctx(out))

        rc, out = run("session", {"cwd": repo, "session_id": "p9"}, penv)
        check("scope_paths: the session digest has no path, so a path-scoped posture rule shows",
              "Scoped posture advisory" in ctx(out))

        check("scope_paths: absent from a rule → nothing changes",
              H.scope_ok({"repo_scope": "any"}, "xmem", "", "docs/a.py"))
        # a scope the hook cannot evaluate must never silence a rule, and a
        # malformed one must not raise where the caller is not wrapped
        check("scope_paths: an unusable glob list is ignored, not fatal, not silencing",
              H.path_in_scope({"_scope_paths": [["unhashable"]]}, "src/a.py")
              and H.path_in_scope({"_scope_paths": ["/", ""]}, "src/a.py"))
        check("scope_paths: one usable glob among junk still narrows",
              H.path_in_scope({"_scope_paths": ["/", "src/**"]}, "src/a.py")
              and not H.path_in_scope({"_scope_paths": ["/", "src/**"]}, "docs/a.py"))

        # A scope glob is wire data any teammate can author, and the pre lane
        # has a 5 s budget. The obvious `*` → `[^/]*` regex translation took
        # >6 s on `*a*a*a…b` against a long path; the matcher is deliberately
        # not a regex so no pattern can backtrack. This is that regression gate.
        import time as _t
        worst = "src/" + "a" * 255 + "/x.py"
        t0 = _t.perf_counter()
        for _ in range(50):
            H.path_in_scope({"_scope_paths": ["*" + "a*" * 19 + "b",
                                              "**/**/**/**/**/**/**/**/zzz"]}, worst)
        span = (_t.perf_counter() - t0) * 1000 / 50
        check("scope_paths: a pathological glob stays linear (no catastrophic backtracking)",
              span < 5.0, f"{span:.3f} ms per call")

        check("scope_paths: a symlinked worktree root still resolves (macOS /tmp)",
              H.rel_path(os.path.join(os.path.realpath(td), "xmem", "src", "a.py"),
                         os.path.join(td, "xmem"), td) == "src/a.py")

        # decision 8, on the wire this time: a null file_path is NO path, not
        # the literal string "None", which would look in-worktree and exclude
        rc, out = run("pre", {"cwd": repo, "session_id": "p10", "tool_name": "Bash",
                              "tool_input": {"command": "scoped-cmd", "file_path": None}}, penv)
        check("scope_paths: a null file_path is no path at all, and never silences a rule",
              "Scoped bash advisory" in ctx(out), ctx(out))

        # --- message_ref: the cases the adversarial review found -------------
        tp_bad = transcript("t-badmsg.jsonl", [
            asst(U1, [{"type": "tool_use", "name": "Bash", "input": {}}]),
            {"type": "assistant", "uuid": U2, "message": ["not a dict"]},
        ])
        check("message_ref: one malformed record does not discard the whole scan",
              H.message_ref({"transcript_path": tp_bad}, "Bash") == U1)

        tp_par = transcript("t-parallel.jsonl", [
            asst(U1, [{"type": "tool_use", "name": "Bash", "input": {"command": "first"}}]),
            asst(U2, [{"type": "tool_use", "name": "Bash", "input": {"command": "second"}}]),
        ])
        check("message_ref: parallel calls to the SAME tool resolve by input, not by recency",
              H.message_ref({"transcript_path": tp_par}, "Bash", {"command": "first"}) == U1
              and H.message_ref({"transcript_path": tp_par}, "Bash", {"command": "second"}) == U2)
        check("message_ref: an unmatched input still falls back to the newest same-tool record",
              H.message_ref({"transcript_path": tp_par}, "Bash", {"command": "third"}) == U2)

        # The whole latency argument is that a call which fires nothing never
        # touches the transcript. Assert it, rather than trusting the ordering.
        reads = []
        real_ref = H.message_ref
        H.message_ref = lambda *a, **k: (reads.append(1), real_ref(*a, **k))[1]
        H.BASE, H.BOOK_DIR = mbase, os.path.join(mbase, "book")
        for cmd, label in (("nothing here matches", "quiet"), ("marker-cmd", "fires")):
            sys.stdin = io.StringIO(json.dumps(
                {"cwd": repo, "session_id": f"lat-{label}", "transcript_path": tp,
                 "tool_name": "Bash", "tool_input": {"command": cmd}}))
            out_buf, sys.stdout = sys.stdout, io.StringIO()
            try:
                H.main()
            finally:
                sys.stdout = out_buf
        H.message_ref = real_ref
        sys.stdin = sys.__stdin__
        check("message_ref: a call that fires nothing never reads the transcript",
              len(reads) == 1, f"{len(reads)} reads for 1 quiet + 1 firing call")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    else:
        print("all rulebook hook checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
