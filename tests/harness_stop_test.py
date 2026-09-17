#!/usr/bin/env python3
"""Tests for the harness-tied memory sensor (`harness_stop.py`).

What these protect: with the flag off nothing happens at all; with it on the
Stop hook returns at once and takes the turn's error arcs at the boundary; a
detached child classifies each turn exactly once; a failure and its fix are
one moment; the child classifies the turn that stopped even when the next
prompt has already landed; a flagged moment BLOCKS a later Stop of the main
agent once, with its stamp, the continuation's own Stop passes, and a moment
the child appends meanwhile is never lost; the recorded block never becomes a
turn; the proposal is scoped to the repository the turn worked in; subagents
never take a moment; nothing in the sensor sends `activate`. No test reaches a
server: `server_classify` is substituted.
"""
from __future__ import annotations

import importlib
import io
import pathlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import harness_extract as hx  # noqa: E402
import harness_stop as hs  # noqa: E402


class _Env:
    """Every file the sensor and the hook write goes to a temp dir."""

    KEYS = ("MEMHUB_HARNESS_DIR", "MEMHUB_RULEBOOK_BASE", "MEMHUB_HARNESS_EXTRACT",
            "MEMHUB_RULEBOOK_FETCH", "MEMHUB_RULEBOOK_RECALL")

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)
        self.saved = {k: os.environ.get(k) for k in self.KEYS}
        os.environ["MEMHUB_HARNESS_DIR"] = str(self.base / "harness")
        os.environ["MEMHUB_RULEBOOK_BASE"] = str(self.base / "rulebook")
        os.environ["MEMHUB_HARNESS_EXTRACT"] = "1"
        os.environ["MEMHUB_RULEBOOK_FETCH"] = "0"
        os.environ["MEMHUB_RULEBOOK_RECALL"] = "0"
        # the hook reads MEMHUB_RULEBOOK_BASE at import
        if "rulebook_hook" in sys.modules:
            importlib.reload(sys.modules["rulebook_hook"])
        hx._HOOK_MODULE.clear()
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        hx._HOOK_MODULE.clear()
        self.td.cleanup()


def _transcript(path: Path, turns):
    recs, n = [], 0
    for user, asst, tools in turns:
        n += 1
        recs.append({"type": "user", "uuid": f"u{n}", "cwd": str(path.parent),
                     "message": {"content": user}})
        for i, (tool, inp, out, err) in enumerate(tools):
            tid = f"t{n}-{i}"
            recs.append({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": tool, "input": inp}]}})
            recs.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": err}]}})
        recs.append({"type": "assistant", "message": {"content": [{"type": "text", "text": asst}]}})
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")


def _git_repo(td: Path, name: str = "repo") -> Path:
    repo = td / name
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@x", HOME=str(td), GIT_CONFIG_NOSYSTEM="1")
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)
    return repo


def _classify(calls, reply):
    return lambda w, hint="", repo="", timeout=0: (
        calls.append({"window": w, "hint": hint}), (reply, 0.1))[1]


def _spawns(fn):
    seen = []
    real = hx.subprocess.Popen
    hx.subprocess.Popen = lambda args, **kw: seen.append((args, kw))
    try:
        fn()
    finally:
        hx.subprocess.Popen = real
    return seen


# ---------------------------------------------------------------- the flag
def test_with_the_flag_off_nothing_happens():
    with _Env() as env:
        os.environ["MEMHUB_HARNESS_EXTRACT"] = "0"
        tp = env.base / "s.jsonl"
        _transcript(tp, [("i mean staging", "ok", [])])
        for mode in ("stop", "extract"):
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "harness_stop.py"), mode,
                 "--session", "s", "--transcript", str(tp)],
                input=json.dumps({"session_id": "s", "transcript_path": str(tp), "cwd": str(ROOT)}),
                capture_output=True, text=True, timeout=30,
                env=dict(os.environ, MEMHUB_HARNESS_EXTRACT="0"))
            assert proc.returncode == 0 and proc.stdout == "", (mode, proc.stdout)
        assert not (env.base / "harness").exists()
    print("PASS test_with_the_flag_off_nothing_happens")


# --------------------------------------------------------------- stop lane
def test_stop_spawns_one_detached_child_and_returns():
    with _Env() as env:
        tp = env.base / "s.jsonl"
        tp.write_text("", encoding="utf-8")
        t0 = time.time()
        seen = _spawns(lambda: hs.cmd_stop({"session_id": "sess", "transcript_path": str(tp),
                                            "cwd": str(ROOT)}))
        assert time.time() - t0 < 0.5
        assert len(seen) == 1
        args, kw = seen[0]
        assert args[1].endswith("harness_stop.py") and args[2] == "extract"
        assert args[args.index("--session") + 1] == "sess"
        assert args[args.index("--upto") + 1] == "0", "the boundary is the size at Stop"
        assert "--arcs" not in args, "no arcs recorded, none passed"
        if hasattr(os, "setsid"):
            assert kw["start_new_session"] is True
        # a re-entered Stop, a subagent's Stop, and a missing transcript: nothing
        for payload in ({"session_id": "sess", "transcript_path": str(tp), "stop_hook_active": True},
                        {"session_id": "sess", "transcript_path": str(tp), "agent_id": "agent-7f"},
                        {"session_id": "sess", "transcript_path": "/nope"},
                        {"session_id": "", "transcript_path": str(tp)}):
            assert _spawns(lambda: hs.cmd_stop(payload)) == [], payload
    print("PASS test_stop_spawns_one_detached_child_and_returns")


def _post(repo: Path, session: str, cmd: str, resp: dict, agent_id: str = ""):
    payload = {"session_id": session, "cwd": str(repo), "tool_name": "Bash",
               "hook_event_name": "PostToolUse", "tool_input": {"command": cmd},
               "tool_response": resp}
    if agent_id:
        payload["agent_id"] = agent_id
    proc = subprocess.run([sys.executable, str(SCRIPTS / "rulebook_hook.py"), "post"],
                          input=json.dumps(payload), capture_output=True, text=True,
                          timeout=30, env=dict(os.environ))
    assert proc.returncode == 0, proc.stderr


def test_a_failure_and_its_fix_are_one_moment_taken_at_the_boundary():
    with _Env() as env:
        repo = _git_repo(env.base)
        import rulebook_hook as rh  # noqa: PLC0415
        _post(repo, "arc", "pytest tests/x.py",
              {"stdout": "", "stderr": "ModuleNotFoundError: No module named 'y'", "exit_code": 1})
        _post(repo, "arc", "uv pip install y", {"stdout": "ok", "exit_code": 0})
        _post(repo, "arc", "pytest tests/x.py", {"stdout": "1 passed", "exit_code": 0})
        _post(repo, "arc", "make x", {"stdout": "", "stderr": "boom", "exit_code": 1})
        if os.name != "nt":
            import stat  # noqa: PLC0415
            mode = stat.S_IMODE(os.stat(rh.arcs_path("arc")).st_mode)
            assert mode == 0o600, f"a failed command's text is private, got {oct(mode)}"
        # the session state the rulebook merges by delta is not where arcs live
        assert "arcs" not in json.dumps(rh.load_state(rh.state_path("arc")))
        tp = env.base / "s.jsonl"
        tp.write_text("", encoding="utf-8")
        seen = _spawns(lambda: hs.cmd_stop({"session_id": "arc", "transcript_path": str(tp)}))
        args = seen[0][0]
        arcs_file = Path(args[args.index("--arcs") + 1])
        arcs = json.loads(arcs_file.read_text())
        assert len(arcs) == 1 and arcs[0]["target"] == "pytest tests/x.py"
        assert "ModuleNotFoundError" in arcs[0]["signature"] and arcs[0]["cost"] == 2
        # taken once: the open failure is cleared with it, nothing pairs across turns
        assert rh.take_error_arcs("arc") == []
        _post(repo, "arc", "make x", {"stdout": "ok", "exit_code": 0})
        assert rh.take_error_arcs("arc") == []
        # a subagent's failure and fix are its own: its Stop is ignored, so the
        # main agent's next Stop must not take them
        _post(repo, "arc", "pytest tests/y.py", {"stdout": "", "stderr": "E boom", "exit_code": 1},
              agent_id="agent-7f")
        _post(repo, "arc", "pytest tests/y.py", {"stdout": "1 passed", "exit_code": 0}, agent_id="agent-7f")
        assert rh.take_error_arcs("arc") == [], "subagent arcs never reach the main turn"
        # the router sees the arc on a turn whose transcript shows no error
        hits = hx.route({"n": 1, "user": "ok", "asst": "done", "tools": [], "results": []}, None, arcs=arcs)
        assert dict(hits)["error_arc"] == "missing-module"
        # a blocked continuation's arcs are drained at its own Stop, which spawns
        # nothing: the next ordinary turn must not inherit them (Codex, #230)
        _post(repo, "arc", "make y", {"stdout": "", "stderr": "E boom", "exit_code": 1})
        _post(repo, "arc", "make y", {"stdout": "ok", "exit_code": 0})
        assert _spawns(lambda: hs.cmd_stop({"session_id": "arc", "transcript_path": str(tp),
                                            "stop_hook_active": True})) == []
        assert rh.take_error_arcs("arc") == [], "the continuation's arcs were drained"
        # with the flag off the hook records nothing
        os.environ["MEMHUB_HARNESS_EXTRACT"] = "0"
        _post(repo, "off", "pytest", {"stdout": "", "stderr": "E", "exit_code": 1})
        assert not os.path.exists(rh.arcs_path("off"))
    print("PASS test_a_failure_and_its_fix_are_one_moment_taken_at_the_boundary")


def test_extract_classifies_the_last_turn_once_and_consumes_its_arcs():
    with _Env() as env:
        repo = _git_repo(env.base)
        tp = env.base / "s.jsonl"
        _transcript(tp, [("do the thing", "done", [("Bash", {"command": "ls"}, "a", False)]),
                         ("no, i mean on staging", "right", [("Bash", {"command": "git status"}, "clean", False)])])
        arcs_file = hx.session_file("sess", ".arcs-1.json")
        arcs_file.parent.mkdir(parents=True, exist_ok=True)
        arcs_file.write_text(json.dumps([{"signature": "boom", "target": "make", "fix": "make", "cost": 6}]))
        calls = []
        real = hx.server_classify
        hx.server_classify = _classify(calls, {"signal": True, "reason": "classified",
                                               "kind": "correction", "derivable": True})
        try:
            assert hs.cmd_extract("sess", str(tp), str(repo), str(arcs_file)) == 0
            # a second Stop for the same turn, or a racing child: no second call
            assert hs.cmd_extract("sess", str(tp), str(repo)) == 0
        finally:
            hx.server_classify = real
        assert len(calls) == 1 and calls[0]["hint"] == "wrong_target"
        assert "closed error arc on 'make'" in calls[0]["window"]
        assert not arcs_file.exists(), "the arcs file is consumed"
        moments = hx.read_jsonl(hs.moments_path("sess"))
        assert len(moments) == 1 and moments[0]["source_ref"] == "sess#2"
        assert moments[0]["derivable"] is True
        state = moments[0]["state"]
        assert state["repo"] == "repo" and state["turn"] == 2 and state["branch"] and state["head_sha"]
        assert hs.load_meta("sess")["last_turn"] == 2
        # the next turn is its own claim; an outage records nothing
        _transcript(tp, [("do the thing", "done", []), ("no, i mean on staging", "right", []),
                         ("thanks", "np", [])])
        hx.server_classify = _classify(calls, {"signal": False, "reason": "judge_failed"})
        try:
            hs.cmd_extract("sess", str(tp), str(repo))
        finally:
            hx.server_classify = real
        assert len(calls) == 2 and len(hx.read_jsonl(hs.moments_path("sess"))) == 1
        claims = list(hx.harness_dir().glob("sess.turn-*.claim"))
        assert len(claims) == 1, "older claims are cleared"
    print("PASS test_extract_classifies_the_last_turn_once_and_consumes_its_arcs")


def test_extract_takes_the_turn_that_stopped_not_the_prompt_queued_after_it():
    with _Env() as env:
        repo = _git_repo(env.base)
        tp = env.base / "s.jsonl"
        _transcript(tp, [("do the thing", "done", []),
                         ("no, i mean on staging", "right", [("Bash", {"command": "git status"}, "clean", False)])])
        upto = tp.stat().st_size                     # Stop fired here
        with tp.open("a", encoding="utf-8") as fh:
            # the stopped turn's last words flush late, then a queued prompt lands,
            # all before the detached child opens the file
            fh.write(json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "late reply"}]}}) + "\n")
            fh.write(json.dumps({"type": "user", "uuid": "u3", "cwd": str(repo),
                                 "message": {"content": "now deploy it"}}) + "\n")
        calls = []
        real = hx.server_classify
        hx.server_classify = _classify(calls, {"signal": True, "reason": "classified",
                                               "kind": "correction"})
        try:
            hs.cmd_extract("sess", str(tp), str(repo), "", upto)
            assert len(calls) == 1 and "i mean on staging" in calls[0]["window"]
            assert "late reply" in calls[0]["window"] and "now deploy it" not in calls[0]["window"]
            assert [m["turn"] for m in hx.read_jsonl(hs.moments_path("sess"))] == [2]
            # the next turn's own Stop is not blocked by a claim taken for it early
            with tp.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "starting on it"}]}}) + "\n")
            hs.cmd_extract("sess", str(tp), str(repo), "", tp.stat().st_size)
            assert len(calls) == 2 and "now deploy it" in calls[1]["window"]
        finally:
            hx.server_classify = real
        assert hs.load_meta("sess")["last_turn"] == 3
    print("PASS test_extract_takes_the_turn_that_stopped_not_the_prompt_queued_after_it")


# ------------------------------------------------------------- the handoff
def _moment(n, kind="correction", hint="wrong_target", session="sess"):
    return {"turn": n, "source_ref": f"{session}#{n}", "hint": hint, "kind": kind,
            "state": {"repo": "repo", "session_id": session, "turn": n, "hook_version": "0.54.0",
                      "at": "2026-09-10T00:00:00Z", "branch": "b", "head_sha": "abc",
                      "pr_number": None, "env": "staging"}}


def _stop(**payload):
    """The Stop hook end to end through `main`, the child's spawn substituted."""
    tp = hx.harness_dir().parent / "stop.jsonl"
    if not tp.exists():
        tp.write_text("", encoding="utf-8")
    payload = dict({"session_id": "sess", "transcript_path": str(tp)}, **payload)
    out, real_stdout, real_stdin = io.StringIO(), sys.stdout, sys.stdin
    sys.stdout, sys.stdin = out, io.StringIO(json.dumps(payload))
    real_popen = hx.subprocess.Popen
    hx.subprocess.Popen = lambda args, **kw: None
    try:
        rc = hs.main(["stop"])
    finally:
        sys.stdout, sys.stdin = real_stdout, real_stdin
        hx.subprocess.Popen = real_popen
    return rc, out.getvalue()


def _reason(out: str) -> str:
    doc = json.loads(out)
    assert doc["decision"] == "block", doc     # top-level, verified live on Claude Code 2.1.270
    return doc["reason"]


def test_a_later_stop_blocks_on_the_moment_once():
    with _Env():
        hs.save_meta("sess", repo="repo", last_turn=2)
        hx.append_jsonl(hs.moments_path("sess"), _moment(2))
        # a subagent's Stop, carrying the parent session id, takes nothing; nor
        # does the continuation's own Stop, or every block would chain another
        assert _stop(agent_id="agent-7f") == (0, "")
        assert _stop(stop_hook_active=True) == (0, "")
        assert len(hx.read_jsonl(hs.moments_path("sess"))) == 1, "nothing handed"
        rc, out = _stop()
        reason = _reason(out)
        assert rc == 0 and reason.startswith(hs.BLOCK_PREFIX)
        assert "turn 2" in reason and "correction" in reason and "router: wrong_target" in reason
        assert '"session_id"' not in reason and 'source_ref="sess#2"' in reason
        assert 'scope_repos=["repo"]' in reason and "create-rule skill" in reason
        # "Never pass activate" moved into the skill with the rest of the
        # manual; this line is a pointer (Codex, #244)
        assert "activate" not in reason
        rows = hx.read_jsonl(hs.moments_path("sess"))
        assert [r.get("handed") for r in rows] == [None, "sess#2"], "handed by appending"
        assert "handed_at" not in rows[0], "the moment row is never rewritten"
        assert _stop() == (0, ""), "one block per moment"
    print("PASS test_a_later_stop_blocks_on_the_moment_once")


def test_a_fast_child_never_makes_the_block_hand_the_stopping_turn():
    """The handoff is chosen before the child is spawned. A child that wins the
    race and appends THIS turn's moment at once must not become the block
    (Codex, #230)."""
    with _Env():
        hs.save_meta("sess", repo="repo", last_turn=2)
        hx.append_jsonl(hs.moments_path("sess"), _moment(2))
        tp = hx.harness_dir().parent / "race.jsonl"
        tp.write_text("", encoding="utf-8")
        out, real_stdout, real_popen = io.StringIO(), sys.stdout, hx.subprocess.Popen

        def child_wins_the_race(args, **kw):
            hx.append_jsonl(hs.moments_path("sess"), _moment(3))

        hx.subprocess.Popen, sys.stdout = child_wins_the_race, out
        try:
            hs.cmd_stop({"session_id": "sess", "transcript_path": str(tp)})
        finally:
            hx.subprocess.Popen, sys.stdout = real_popen, real_stdout
        reason = _reason(out.getvalue())
        assert "turn 2" in reason and "turn 3" not in reason, reason[:80]
        assert [r.get("handed") for r in hx.read_jsonl(hs.moments_path("sess"))
                if r.get("handed")] == ["sess#2"]
    print("PASS test_a_fast_child_never_makes_the_block_hand_the_stopping_turn")


def test_stale_capped_and_missing_moments_never_block():
    with _Env():
        hs.save_meta("sess", repo="repo", last_turn=9)
        hx.append_jsonl(hs.moments_path("sess"), _moment(2))
        assert _stop() == (0, ""), "a stale moment"
        hs.save_meta("sess", last_turn=4)
        hx.append_jsonl(hs.moments_path("sess"), _moment(3))
        hx.append_jsonl(hs.moments_path("sess"), _moment(4, kind="error_arc", hint="error_arc"))
        reason = _reason(_stop()[1])
        assert "turn 4" in reason and "turn 3" not in reason
        for i in range(hs.HANDOFF_CAP_PER_SESSION):
            hx.append_jsonl(hs.moments_path("sess"), {"handed": f"sess#old{i}", "at": 0})
        hs.save_meta("sess", last_turn=5)
        hx.append_jsonl(hs.moments_path("sess"), _moment(5))
        assert _stop() == (0, ""), "the session cap"
        assert _stop(session_id="") == (0, "")
        assert _stop(session_id="nobody") == (0, "")
    print("PASS test_stale_capped_and_missing_moments_never_block")


def test_a_moment_the_child_appends_while_a_prompt_is_handed_is_kept():
    with _Env():
        path = hs.moments_path("sess")
        hs.save_meta("sess", repo="repo", last_turn=2)
        hx.append_jsonl(path, _moment(2))
        real = hx.read_jsonl

        def read_then_the_child_appends(p):
            rows = real(p)
            hx.append_jsonl(p, _moment(3))          # the extract child lands in the gap
            return rows

        hx.read_jsonl = read_then_the_child_appends
        try:
            rc, out = _stop()
        finally:
            hx.read_jsonl = real
        assert rc == 0 and "turn 2" in _reason(out)
        assert [r.get("turn") for r in hx.read_jsonl(path) if not r.get("handed")] == [2, 3]
        hs.save_meta("sess", last_turn=3)
        assert "turn 3" in _reason(_stop()[1])
    print("PASS test_a_moment_the_child_appends_while_a_prompt_is_handed_is_kept")


def test_the_proposal_is_scoped_to_the_repo_the_turn_worked_in():
    with _Env() as env:
        alpha, beta = _git_repo(env.base, "alpha"), _git_repo(env.base, "beta")
        kw = {"session": "s", "cwd": str(alpha), "hook_version": "0.54.0", "env_name": "staging"}
        # a session rooted in alpha whose turn worked only in beta
        only_beta = {"n": 1, "tools": [{"tool": "Bash", "target": f"cd {beta} && make test"}]}
        state = hx.stamp_state(turn=only_beta, **kw)
        assert state["repo"] == "beta" and "touched_repos" not in state, state
        line = hs.block_reason("s", {"turn": 1, "state": state}, "alpha")
        assert 'scope_repos=["beta"]' in line and "repositories" not in line
        # `git -C <path>` points a command at another repository as a leading `cd` does
        for target in (f"git -C {beta} status", f"cd {alpha} && git -C ../beta log -1",
                       f"GIT_PAGER=cat git -c core.pager=cat -C {beta} diff"):
            got = hx.stamp_state(turn={"n": 4, "tools": [{"tool": "Bash", "target": target}]}, **kw)
            assert got["repo"] == "beta", (target, got)
        # a turn that worked in both names both and asks for the narrowing
        both = {"n": 2, "tools": [{"tool": "Bash", "target": f"cd {beta} && make test"},
                                  {"tool": "Edit", "target": str(alpha / "x.py")}]}
        state = hx.stamp_state(turn=both, **kw)
        assert state["repo"] == "alpha" and state["touched_repos"] == ["beta", "alpha"], state
        line = hs.block_reason("s", {"turn": 2, "state": state}, "alpha")
        assert 'scope_repos=["beta", "alpha"]' in line and "worked in 2 repositories" in line
        # an action that addresses nothing leaves the session's own repo
        idle = {"n": 3, "tools": [{"tool": "TodoWrite", "target": ""}]}
        assert hx.stamp_state(turn=idle, **kw)["repo"] == "alpha"
        assert hs.block_reason("s", {"turn": 3, "state": {}}, "alpha").count('scope_repos=["alpha"]') == 1
    print("PASS test_the_proposal_is_scoped_to_the_repo_the_turn_worked_in")


def test_each_stop_reads_from_the_cursor_not_from_byte_zero():
    with _Env() as env:
        repo = _git_repo(env.base)
        tp = env.base / "s.jsonl"
        steps = [(f"step {i}", f"did {i}", [("Bash", {"command": f"echo {i}"}, "ok", False)])
                 for i in range(1, 5)]
        calls, starts = [], []
        real_classify, real_read = hx.server_classify, hx.turns_from_transcript

        def spy(path, start=0, before=0):
            starts.append(start)
            return real_read(path, start=start, before=before)

        hx.server_classify = _classify(calls, {"signal": False, "reason": "classified"})
        hx.turns_from_transcript = spy
        try:
            _transcript(tp, steps[:3])
            hs.cmd_extract("sess", str(tp), str(repo), "", tp.stat().st_size)
            assert starts == [0], starts
            assert hs.load_meta("sess")["scan"]["uuid"] == "u2"
            _transcript(tp, steps[:4])
            hs.cmd_extract("sess", str(tp), str(repo), "", tp.stat().st_size)
            assert len(starts) == 2 and starts[1] > 0, "the second Stop resumes from the cursor"
            assert "PREVIOUS USER MESSAGE: step 3" in calls[-1]["window"]
            assert "USER'S NEW MESSAGE: step 4" in calls[-1]["window"]
            assert hs.load_meta("sess")["last_turn"] == 4, "numbered as a full read would"
            # a transcript replaced under the cursor falls back to a full read
            _transcript(tp, [("other", "x", [])] + steps[:4])
            hs.cmd_extract("sess", str(tp), str(repo), "", tp.stat().st_size)
            assert starts[-1] == 0 and hs.load_meta("sess")["last_turn"] == 5, starts
        finally:
            hx.server_classify, hx.turns_from_transcript = real_classify, real_read
    print("PASS test_each_stop_reads_from_the_cursor_not_from_byte_zero")


def test_the_newest_turn_is_handed_whatever_order_the_children_finished_in():
    with _Env():
        hs.save_meta("sess", repo="repo", last_turn=5)
        hx.append_jsonl(hs.moments_path("sess"), _moment(5))      # turn 5's classifier answered first
        hx.append_jsonl(hs.moments_path("sess"), _moment(4))
        reason = _reason(_stop()[1])
        assert "turn 5" in reason and "turn 4" not in reason, reason[:80]
    print("PASS test_the_newest_turn_is_handed_whatever_order_the_children_finished_in")


def test_an_older_turns_child_never_moves_the_meta_back():
    with _Env():
        hs.save_meta("sess", advance_turn=5, repo="new")
        hs.save_meta("sess", advance_turn=4, repo="old")      # the older child finishes last
        meta = hs.load_meta("sess")
        assert meta["last_turn"] == 5 and meta["repo"] == "new", meta
        # the read and the write are one critical section: another child cannot enter it
        real, tried = hs.load_meta, []

        def load_while_another_child_tries(session):
            if not tried:
                tried.append(hs._meta_lock(session))
            return real(session)

        hs.load_meta = load_while_another_child_tries
        try:
            hs.save_meta("sess", advance_turn=6)
        finally:
            hs.load_meta = real
        assert tried == [None], "the lock is held across the read-merge-write"
        lock = hs._meta_lock("sess")
        assert lock is not None, "and released after it"
        hs._release(lock)
        assert hs.load_meta("sess")["last_turn"] == 6
    print("PASS test_an_older_turns_child_never_moves_the_meta_back")


def test_the_block_reason():
    line = hs.block_reason("sess", _moment(2), "repo")
    assert line.startswith(hs.BLOCK_PREFIX)
    # the privacy guards stay on the line itself: it reaches both the model and
    # the person. "Never pass activate" moved into the skill (Codex, #244).
    assert "/Users/" not in line and "@" not in line
    # HOW to file lives in skills/create-rule/SKILL.md. The prompt-lane line
    # restated it and drew five of six Codex findings on #222 (dropped whatever
    # it did not copy, drifted from whatever it did), so none of it is here.
    assert "create-rule skill" in line
    for restated in ("list_rulebooks", "include_retired", "supersedes_rule_id",
                     "anchor_recall", "--fires", "mode"):
        assert restated not in line, restated
    # The line is a POINTER now. Claude Code renders a blocking Stop reason to
    # the person as "Stop hook feedback:" and there is no Stop channel that
    # reaches only the model (Codex, #244), so every instruction that does not
    # have to be here lives in skills/create-rule/SKILL.md instead. What stays
    # is what only this line knows: which turn, and where the skill is.
    assert "create-rule skill" in line
    assert 'source_ref="sess#2"' in line and "scope_repos=" in line
    # The verdict lane is gone. Filed-per-block is already `handed` rows here
    # against `session_draft` rules on the server, and whether a rule HELPS is
    # the fire-event fold's question. Recording non-events cost seven of the
    # fifteen findings on #244 and bought a ratio that was already free.
    assert "verdict" not in line and "--why" not in line and "<<" not in line
    assert "say nothing to the person about this turn" in line
    # the manual moved out and must NOT be restated here
    for moved in ("not already a RULE", "restating the docs", "without asking",
                  "activate", "state=", '"hook_version"'):
        assert moved not in line, moved
    # 1130 -> 347. A pointer, not a manual.
    assert len(line) < 400, len(line)
    # ...and the skill must actually carry what the line dropped, or the two
    # halves diverge silently and the agent gets neither.
    skill = (pathlib.Path(__file__).resolve().parent.parent / "plugins" / "memhub"
             / "skills" / "create-rule" / "SKILL.md").read_text(encoding="utf-8")
    # markdown wraps; the check is about content, not where the lines break,
    # and reflowing prose to satisfy a substring test is the wrong direction
    skill = " ".join(skill.replace("*", "").replace("`", "").split())
    assert "conflict is still reported to the person" not in skill
    assert "memhub-verdict" not in skill and "--why-file" not in skill
    for owed in ("not already a RULE", "does NOT disqualify it",
                 "restating the docs", "ask the person nothing at all",
                 "Never pass activate", "Step 4b.6", "Step 0 (which rulebook)",
                 "Do NOT rebuild it from", 'source="session_draft"',
                 # each exception the harness path takes must be stated, or the
                 # agent hits a mandatory step it cannot satisfy (Codex, #244)

                 # nothing on a non-filing path reaches the person (Codex, #244)
                 "hears about the turn only when a rule was filed",
                 "File nothing, say nothing, stop", "same_matcher",
                 # one invariant beats enumerating every step that talks: five
                 # rounds of this PR were the next unexempted one (Codex, #244)
                 "nothing on this path reaches the person",
                 "including ones added after this was written",
                 "A live verification that runs and fails is terminal",
                 # an idempotent re-import ended WITH a rule (Codex, #244)
                 "unchanged: true is a filing, not a blocker",
                 "source_ref is passed EXACTLY as the harness line gives it"):
        assert owed in skill, owed
    assert "may already be written down" in hs.block_reason("sess", dict(_moment(2), derivable=True), "repo")
    assert "may already be written down" not in line
    print("PASS test_the_block_reason")





def test_the_block_shows_the_person_a_status_line_not_the_instructions():
    """`reason` is the model's channel and the host also renders it; the
    person's channel is `systemMessage`. The hook blocks BEFORE the agent has
    decided, so it can only name what is being looked at — the outcome line is
    the agent's own reply."""
    with _Env():
        hs.save_meta("sess", repo="repo", last_turn=2)
        hx.append_jsonl(hs.moments_path("sess"), _moment(2))
        rc, out = _stop()
        payload = json.loads(out)
        assert rc == 0 and payload["decision"] == "block"
        assert payload["systemMessage"] == "MemHub: reviewing turn 2 for a team rule"
        assert "create-rule skill" not in payload["systemMessage"]
    print("PASS test_the_block_shows_the_person_a_status_line_not_the_instructions")



def test_a_recorded_block_is_not_a_turn():
    """Claude Code records the block's reason as an isMeta `user` record
    (`Stop hook feedback:\\n<reason>`, seen on 2.1.270). Read as a human
    message it would renumber every later turn and hand the classifier the
    harness talking to itself."""
    with _Env() as env:
        tp = env.base / "s.jsonl"
        reason = hs.block_reason("sess", _moment(1), "repo")
        recs = [{"type": "user", "uuid": "u1", "message": {"content": "fix the deploy"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
                {"type": "user", "isMeta": True, "uuid": "m1",
                 "message": {"content": f"Stop hook feedback:\n{reason}"}},
                # the blocked continuation: its actions and verdict are not turn 1's
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "c1", "name": "Bash",
                     "input": {"command": "python3 rulebook_verify.py --rule-file cand.json"}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": "E boom", "is_error": True}]}},
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "No rule from turn 1: project state"}]}},
                {"type": "user", "uuid": "u2", "message": {"content": "now the tests"}},
                # a person may TYPE those words; without isMeta it is their turn (Codex, #230)
                {"type": "user", "uuid": "u3",
                 "message": {"content": "Stop hook feedback: why did it block me?"}}]
        tp.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        turns = hx.turns_from_transcript(tp)
        assert [t["user"] for t in turns] == ["fix the deploy", "now the tests",
                                              "Stop hook feedback: why did it block me?"], turns
        assert [t["n"] for t in turns] == [1, 2, 3]
        # the stopped turn ends at the feedback record: a late extractor must not
        # hand the classifier the harness's own flow as turn 1 (Codex, #230)
        assert turns[0]["asst"] == "done" and turns[0]["tools"] == [] and turns[0]["results"] == [], turns[0]
    print("PASS test_a_recorded_block_is_not_a_turn")


def test_the_hooks_are_wired_behind_the_guard():
    doc = json.loads((ROOT / "plugins" / "memhub" / "hooks" / "claude-hooks.json").read_text(encoding="utf-8"))
    wired = {}
    for event, groups in doc["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                if "harness_stop.py" in handler["command"]:
                    wired[event] = handler
    # one lane: the prompt lane handed 19 moments for 0 proposals and is gone
    assert set(wired) == {"Stop"}, set(wired)
    # synchronous: the transcript size and the error arcs it takes ARE the
    # boundary, and an async hook's stdout could not block the stop
    assert not wired["Stop"].get("async") and wired["Stop"]["timeout"] <= 10
    assert wired["Stop"]["command"].rstrip("; fi").endswith('harness_stop.py" stop')
    for h in wired.values():
        assert 'claude_hook_guard.py" ignore' in h["command"]
    print("PASS test_the_hooks_are_wired_behind_the_guard")


def test_the_hook_commands_start_nothing_unless_the_flag_is_on():
    if os.name == "nt":
        print("SKIP test_the_hook_commands_start_nothing_unless_the_flag_is_on (POSIX hook command)")
        return
    doc = json.loads((ROOT / "plugins" / "memhub" / "hooks" / "claude-hooks.json").read_text(encoding="utf-8"))
    commands = [h["command"] for groups in doc["hooks"].values() for g in groups
                for h in g["hooks"] if "harness_stop.py" in h["command"]]
    assert len(commands) == 1
    with tempfile.TemporaryDirectory() as td:
        root, ran = Path(td) / "plugin", Path(td) / "ran"
        (root / "scripts").mkdir(parents=True)
        (root / "scripts" / "claude_hook_guard.py").write_text("import sys\nsys.stdin.read()\n")
        (root / "scripts" / "harness_stop.py").write_text(
            "import sys\nsys.stdin.read()\nopen(%r, 'a').write(sys.argv[1] + ' ')\n" % str(ran))
        for value, runs in (("", False), ("0", False), ("off", False), ("no", False),
                            ("1", True), ("on", True), ("TRUE", True), ("Yes", True)):
            env = {k: v for k, v in os.environ.items() if k != "MEMHUB_HARNESS_EXTRACT"}
            env.update(CLAUDE_PLUGIN_ROOT=str(root), MEMHUB_HARNESS_EXTRACT=value)
            for command in commands:
                proc = subprocess.run(["bash", "-c", command], input="{}", text=True,
                                      capture_output=True, env=env, timeout=10)
                assert proc.returncode == 0, proc.stderr
            got = sorted(ran.read_text().split()) if ran.exists() else []
            assert got == (["stop"] if runs else []), (value, got)
            if ran.exists():
                ran.unlink()
    print("PASS test_the_hook_commands_start_nothing_unless_the_flag_is_on")


def test_the_sensor_never_sends_activate():
    import re  # noqa: PLC0415
    src = (SCRIPTS / "harness_stop.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"[\"']activate[\"']\s*:", code)
    assert "activate=" not in code and "call_tool" not in code
    print("PASS test_the_sensor_never_sends_activate")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
