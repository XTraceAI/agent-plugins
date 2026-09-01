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
import subprocess
import sys
import tempfile
import time

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
        # FETCH=0: the session lane otherwise spawns a DETACHED network fetch
        # that races the test and overwrites the seeded book with an empty one.
        env = {"MEMHUB_RULEBOOK_BASE": td, "MEMHUB_RULEBOOK_FETCH": "0"}

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

    # --- message_id_of: the fire's link back to the transcript record ---
    sys.path.insert(0, os.path.dirname(HOOK))
    import rulebook_hook as rb  # noqa: E402
    with tempfile.TemporaryDirectory() as td:
        tp = os.path.join(td, "t.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "uuid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"}) + "\n")
            f.write(json.dumps({"type": "assistant", "uuid": "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"}) + "\n")
        check("message_id_of: the LAST message record's uuid",
              rb.message_id_of({"transcript_path": tp}) == "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb")
        check("message_id_of: no transcript_path -> None",
              rb.message_id_of({}) is None)
        check("message_id_of: missing file -> None (never raises)",
              rb.message_id_of({"transcript_path": os.path.join(td, "nope.jsonl")}) is None)
        with open(tp, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        check("message_id_of: skips an unparseable tail line",
              rb.message_id_of({"transcript_path": tp}) == "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb")
        big = os.path.join(td, "big.jsonl")
        with open(big, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "user", "uuid": "cccccccc-3333-4333-8333-cccccccccccc", "pad": "x" * 200000}) + "\n")
            f.write(json.dumps({"type": "assistant", "uuid": "dddddddd-4444-4444-8444-dddddddddddd"}) + "\n")
        check("message_id_of: reads only the tail of a large transcript",
              rb.message_id_of({"transcript_path": big}) == "dddddddd-4444-4444-8444-dddddddddddd")

    # A transcript interleaves non-message records that carry their own uuid —
    # `attachment` outnumbers real messages in a long session. Picking one of
    # those links the fire to something that is not a message.
    with tempfile.TemporaryDirectory() as td:
        tp = os.path.join(td, "t.jsonl")
        with open(tp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "uuid": "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"}) + "\n")
            f.write(json.dumps({"type": "attachment", "uuid": "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"}) + "\n")
            f.write(json.dumps({"type": "file-history-snapshot", "uuid": "ffffffff-6666-4666-8666-ffffffffffff"}) + "\n")
        check("message_id_of: skips attachment / meta records that carry a uuid",
              rb.message_id_of({"transcript_path": tp}) == "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa")

        # A single record can exceed the initial tail window; the read grows
        # rather than returning nothing.
        huge = os.path.join(td, "huge.jsonl")
        with open(huge, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "uuid": "99999999-7777-4777-8777-999999999999"}) + "\n")
            f.write(json.dumps({"type": "attachment", "uuid": "88888888-8888-4888-8888-888888888888",
                                "pad": "x" * 300000}) + "\n")
        check("message_id_of: grows the window past a >64 KiB record",
              rb.message_id_of({"transcript_path": huge}) == "99999999-7777-4777-8777-999999999999")

        none_f = os.path.join(td, "none.jsonl")
        with open(none_f, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "attachment", "uuid": "77777777-9999-4999-8999-777777777777"}) + "\n")
        check("message_id_of: no message record anywhere -> None",
              rb.message_id_of({"transcript_path": none_f}) is None)

        # The growing window is bounded: `window >= _TAIL_MAX` is checked
        # BEFORE the multiply, so the read stops at 1 MiB (64K -> 256K -> 1M)
        # for a file of any size. A hook on a 5 s budget must never walk a
        # multi-megabyte transcript.
        def _windows(end):
            w, out = rb._TAIL_START, []
            while True:
                out.append(w)
                if max(0, end - w) == 0 or w >= rb._TAIL_MAX:
                    return out
                w *= 4
        check("message_id_of: at most 3 windows, capped at _TAIL_MAX, for any file size",
              all(len(_windows(n)) <= 3 and max(_windows(n)) <= rb._TAIL_MAX
                  for n in (300_000, 5_400_000, 50_000_000, 5_000_000_000)))

        early = os.path.join(td, "early.jsonl")
        with open(early, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "assistant", "uuid": "msg-at-the-very-start"}) + "\n")
            for i in range(40):
                f.write(json.dumps({"type": "attachment", "uuid": "a%d" % i, "pad": "x" * 90000}) + "\n")
        t0 = time.time()
        got = rb.message_id_of({"transcript_path": early})
        check("message_id_of: a >1 MiB file whose only message is at the start "
              "gives up quickly rather than reading it all",
              got is None and (time.time() - t0) < 1.0)

    # --- what the recall lane sends ---------------------------------------
    # /recall is the one lane that carries content: the relevance judge needs
    # the call itself. A command line is also where credentials live, and they
    # are worth nothing to the judge.
    for label, cmd in [
        ("Authorization header", 'curl -H "Authorization: Bearer sk-live-abcdefghij1234567890" https://x'),
        ("credentials in a URL", 'psql "postgres://admin:hunter2@db.internal/prod"'),
        ("--flag=value", "gh auth login --with-token=ghp_AAAABBBBCCCCDDDDEEEEFFFF1111"),
        ("KEY value (space form)", "aws configure set aws_secret_access_key AKIAIOSFODNN7EXAMPLE"),
        ("KEY=value", "export API_KEY=super-secret-value && ./deploy.sh"),
        ("curl -u user:pass", "curl -u user:p4ssw0rd https://x.com"),
        ("a JWT", 'echo "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcdefghijk"'),
        ("our own access key", 'curl -H "x: mhk_AbCdEf123456789" https://x'),
    ]:
        out = rb.redact_secrets(cmd)
        check("recall redacts: %s" % label,
              "<redacted>" in out and "hunter2" not in out and "p4ssw0rd" not in out
              and "super-secret-value" not in out and "ghp_AAAABBBBCCCCDDDDEEEEFFFF1111" not in out
              and "AKIAIOSFODNN7EXAMPLE" not in out, out)

    # Over-redaction is not free: it costs the judge the verb of the command.
    for cmd in ["git push --force origin main", "gh auth login", "kubectl get secrets",
                "npm run build -- --token-budget 500", "pytest tests/ -k rulebook",
                "ls -la ~/.ssh"]:
        check("recall leaves an innocent command intact: %s" % cmd[:34],
              rb.redact_secrets(cmd) == cmd, rb.redact_secrets(cmd))

    check("redaction never raises on empty or None",
          rb.redact_secrets("") == "" and rb.redact_secrets(None) is None)

    check("WIRE_KEYS carries source_message_id (server links the fire to its message)",
          "source_message_id" in rb.WIRE_KEYS)
    check("WIRE_KEYS carries override_reason (a gate override is a fact about the fire)",
          "override_reason" in rb.WIRE_KEYS)

    # --- delivery: the user sees a fire; a gate blocks (§5.3) ------------------
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "gaterepo")
        os.makedirs(os.path.join(repo, ".git"))
        with open(os.path.join(repo, ".git", "HEAD"), "w", encoding="utf-8") as f:
            f.write("ref: refs/heads/main\n")
        seed_book(td, "gaterepo", [
            {"id": "adv", "on": "bash", "rx": r"advisory-cmd", "fire_scope": "session",
             "repo_scope": "any", "text": "Advisory text", "why": "w", "version": 1},
            {"id": "no-force-push", "on": "bash", "rx": r"git\s+push\s+--force", "mode": "gate",
             "fire_scope": "session", "repo_scope": "any", "text": "Never force-push", "why": "w",
             "version": 1},
            {"id": "result-gate", "on": "result", "rx": r"BOOM", "mode": "gate",
             "fire_scope": "session", "repo_scope": "any", "text": "Result rule", "why": "w",
             "version": 1},
        ])
        genv = {"MEMHUB_RULEBOOK_BASE": td, "MEMHUB_RULEBOOK_FETCH": "0"}
        base = {"cwd": repo, "session_id": "g1", "tool_name": "Bash"}

        def outj(out):
            return json.loads(out) if out.strip() else {}

        rc, out = run("pre", dict(base, tool_input={"command": "advisory-cmd"}), genv)
        j = outj(out)
        check("advisory: user sees an XTrace line naming the rule (systemMessage)",
              j.get("systemMessage", "").startswith("XTrace") and "[adv]" in j.get("systemMessage", "")
              and "Advisory text" in j["systemMessage"], out)
        check("advisory: agent context header is branded, no ruler",
              "XTrace Rulebook" in ctx(out) and "📏" not in ctx(out), ctx(out))
        check("advisory: never blocks", "permissionDecision" not in j["hookSpecificOutput"])

        push = dict(base, tool_input={"command": "git push --force origin main"})
        rc, out = run("pre", push, genv)
        j = outj(out)
        hso = j.get("hookSpecificOutput", {})
        check("gate: pre Bash call is DENIED", rc == 0 and hso.get("permissionDecision") == "deny", out)
        check("gate: deny reason carries the statement and the override line",
              "Never force-push" in hso.get("permissionDecisionReason", "")
              and "RULEBOOK_OVERRIDE=" in hso.get("permissionDecisionReason", ""), out)
        check("gate: user line says blocked, branded",
              j.get("systemMessage", "").startswith("XTrace") and "blocked" in j["systemMessage"]
              and "[no-force-push]" in j["systemMessage"], out)
        rc, out = run("pre", push, genv)
        check("gate: the SAME call is gated again — gates are never deduped",
              outj(out).get("hookSpecificOutput", {}).get("permissionDecision") == "deny", out)

        rc, out = run("pre", dict(base, tool_input={
            "command": "RULEBOOK_OVERRIDE='hotfix, approved by lead' git push --force origin main"}), genv)
        j = outj(out)
        check("override: the prefixed call is ALLOWED",
              "permissionDecision" not in j.get("hookSpecificOutput", {}), out)
        check("override: user line records the override reason",
              "overridden" in j.get("systemMessage", "") and "approved by lead" in j["systemMessage"], out)

        for empty in ("RULEBOOK_OVERRIDE= git push --force origin main",
                      "RULEBOOK_OVERRIDE='' git push --force origin main",
                      "RULEBOOK_OVERRIDE='   ' git push --force origin main"):
            rc, out = run("pre", dict(base, tool_input={"command": empty}), genv)
            check("override: an EMPTY reason is not an override — still denied: %s" % empty[:24],
                  outj(out).get("hookSpecificOutput", {}).get("permissionDecision") == "deny", out)
        rc, out = run("pre", dict(base, tool_input={
            "command": "RULEBOOK_OVERRIDE='token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789ab expired' git push --force origin main"}), genv)
        j = outj(out)
        check("override: the reason is redacted before it is recorded or shown",
              "permissionDecision" not in j.get("hookSpecificOutput", {})
              and "ghp_ABCDEFGHIJ" not in json.dumps(j), out)
        for seg in ("cd /tmp && RULEBOOK_OVERRIDE='cd first' git push --force origin main",
                    "git fetch origin; RULEBOOK_OVERRIDE='after semicolon' git push --force origin main",
                    "echo a\nRULEBOOK_OVERRIDE='on line two' git push --force origin main"):
            rc, out = run("pre", dict(base, tool_input={"command": seg}), genv)
            j = outj(out)
            check("override: recognised at the start of the blocked SEGMENT, not only the command: %s" % seg[:22],
                  "permissionDecision" not in j.get("hookSpecificOutput", {})
                  and "overridden" in j.get("systemMessage", ""), out)
        rc, out = run("pre", dict(base, tool_input={"command": "echo \"RULEBOOK_OVERRIDE='x' git push --force origin main\""}), genv)
        j = outj(out)
        check("override: the variable inside a quoted ARGUMENT is not an override (the gate still denies)",
              j.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
              and "overridden" not in j.get("systemMessage", ""), out)
        for quoted in ("echo 'a|RULEBOOK_OVERRIDE=x git push --force origin main'",
                       "echo \"(RULEBOOK_OVERRIDE='x' git push --force origin main)\"",
                       "printf '%s' \"x;RULEBOOK_OVERRIDE=\\\"why\\\" git push --force origin main\""):
            rc, out = run("pre", dict(base, tool_input={"command": quoted}), genv)
            j = outj(out)
            check("override: a segment boundary INSIDE quotes is data, not an override — still denied: %s" % quoted[:20],
                  j.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
                  and "overridden" not in j.get("systemMessage", ""), out)
        rc, out = run("pre", dict(base, tool_input={
            "command": "true|RULEBOOK_OVERRIDE= x; RULEBOOK_OVERRIDE='the real one' git push --force origin main"}), genv)
        j = outj(out)
        check("override: an earlier EMPTY override does not shadow the real one",
              "permissionDecision" not in j.get("hookSpecificOutput", {})
              and "the real one" in j.get("systemMessage", ""), out)
        rc, out = run("pre", dict(base, tool_input={"command": "grep RULEBOOK_OVERRIDE= hook.py"}), genv)
        check("override: the variable name inside an argument is not an override, and matches no gate",
              out.strip() == "", out)

        rc, out = run("post", dict(base, tool_input={"command": "git push --force origin main"},
                                   tool_response={"stdout": "BOOM", "exit_code": 1}), genv)
        j = outj(out)
        check("gate: a result (post) rule marked gate can only advise — nothing to block after the fact",
              "[result-gate]" in ctx(out) and "permissionDecision" not in j["hookSpecificOutput"], out)

        with open(os.path.join(td, "ledger", "fires.jsonl"), encoding="utf-8") as f:
            rows = [json.loads(l) for l in f if l.strip()]
        gate_rows = [r for r in rows if r["rule_id"] == "no-force-push"]
        check("ledger: blocked, overridden and empty-override calls are all mode=gate fires",
              len(gate_rows) == 15 and all(r["mode"] == "gate" for r in gate_rows), str(len(gate_rows)))
        reasons = [r.get("override_reason") for r in gate_rows]
        check("ledger: override_reason only on the overridden fires, secrets redacted",
              reasons[:3] == [None, None, "hotfix, approved by lead"] and reasons[3:6] == [None] * 3
              and reasons[6] and "ghp_ABCDEFGHIJ" not in reasons[6]
              and reasons[7:] == ["cd first", "after semicolon", "on line two", None,
                                  None, None, None, "the real one"], str(reasons))
        check("ledger: advisory fire stays mode=advise",
              [r["mode"] for r in rows if r["rule_id"] == "adv"] == ["advise"])
        check("ledger: override_reason crosses the wire",
              rb.wire_row(gate_rows[2]).get("override_reason") == "hotfix, approved by lead")

        # a stale book (>24 h) degrades the gate to advise and says so once
        import datetime as _dt
        bdir = os.path.join(td, "book")
        bp = os.path.join(bdir, [n for n in os.listdir(bdir) if n.startswith("gaterepo-")][0])
        with open(bp, encoding="utf-8") as f:
            book = json.load(f)
        book["fetched_at"] = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=25)).isoformat()
        with open(bp, "w", encoding="utf-8") as f:
            json.dump(book, f)
        rc, out = run("pre", dict(push, session_id="g-stale"), genv)
        j = outj(out)
        check("gate: a book older than 24 h runs the gate as advise (no deny) and says so",
              "permissionDecision" not in j.get("hookSpecificOutput", {})
              and "refreshed >24 h ago" in ctx(out), out)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    else:
        print("all rulebook hook checks passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
