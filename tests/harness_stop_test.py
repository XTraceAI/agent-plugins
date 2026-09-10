#!/usr/bin/env python3
"""Tests for the Stop sensor (harness-tied-memory-spec §4.1–§4.2, path A).

What these protect: with the flag off nothing happens at all; with it on the
Stop hook returns in milliseconds and the classifier call happens in a
detached child; a failure and its fix are one moment; a flagged moment is
handed to the agent once, at the next prompt, with its stamp; nothing in the
sensor sends `activate`. No test reaches a server — the one boundary,
`harness_extract.server_classify`, is substituted.
"""
from __future__ import annotations

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

ON = {"MEMHUB_HARNESS_EXTRACT": "1"}


class _Env:
    """Point every state file the sensor and the hook write at a temp dir."""

    def __init__(self):
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)
        self.saved = {k: os.environ.get(k) for k in
                      ("MEMHUB_HARNESS_DRAFTS", "MEMHUB_HARNESS_LOG_DIR",
                       "MEMHUB_RULEBOOK_BASE", "MEMHUB_HARNESS_EXTRACT",
                       "MEMHUB_RULEBOOK_FETCH", "MEMHUB_RULEBOOK_RECALL")}

    def __enter__(self):
        os.environ["MEMHUB_HARNESS_DRAFTS"] = str(self.base / "drafts")
        os.environ["MEMHUB_HARNESS_LOG_DIR"] = str(self.base / "log")
        os.environ["MEMHUB_RULEBOOK_BASE"] = str(self.base / "rulebook")
        os.environ["MEMHUB_HARNESS_EXTRACT"] = "1"
        os.environ["MEMHUB_RULEBOOK_FETCH"] = "0"
        os.environ["MEMHUB_RULEBOOK_RECALL"] = "0"
        # The hook reads MEMHUB_RULEBOOK_BASE at import; a child process gets
        # it fresh, the test process must re-import to follow the temp dir.
        import importlib  # noqa: PLC0415
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
        self.td.cleanup()


def _transcript(path: Path, turns: list[tuple[str, str, list]]) -> None:
    """(user, assistant, [(tool, input, result_text, is_error)]) per turn."""
    recs = []
    n = 0
    for user, asst, tools in turns:
        n += 1
        recs.append({"type": "user", "uuid": f"u{n}", "cwd": str(path.parent),
                     "message": {"content": user}})
        blocks = []
        for i, (tool, inp, out, err) in enumerate(tools):
            tid = f"t{n}-{i}"
            recs.append({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": tool, "input": inp}]}})
            recs.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": tid, "content": out,
                 "is_error": err}]}})
        recs.append({"type": "assistant", "message": {"content": [
            {"type": "text", "text": asst}]}})
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")


def _git_repo(td: Path) -> Path:
    repo = td / "repo"
    repo.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@x",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@x", HOME=str(td),
               GIT_CONFIG_NOSYSTEM="1")
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                       capture_output=True)
    return repo


def _row(n=1):
    return {"title": f"T{n}", "statement": f"When doing thing {n}, do the other "
                                           f"thing {n} first because reason {n}.",
            "engine": "matcher", "delivery": "agent_hook",
            "matcher": {"event": "bash", "command_rx": f"cmd{n}\\b", "command_not_rx": None,
                        "path_rx": None, "path_not_rx": None, "content_rx": None},
            "ordering": None, "anchors": None, "derivable": False, "rationale": "r"}


# ---------------------------------------------------------------- the flag
def test_with_the_flag_off_nothing_happens():
    """Every entry point is a no-op: no spawn, no output, no state, exit 0."""
    with _Env() as env:
        os.environ["MEMHUB_HARNESS_EXTRACT"] = "0"
        tp = env.base / "s.jsonl"
        _transcript(tp, [("i mean staging", "ok", [])])
        for mode in ("stop", "prompt", "extract"):
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "harness_stop.py"), mode,
                 "--session", "s", "--transcript", str(tp)],
                input=json.dumps({"session_id": "s", "transcript_path": str(tp),
                                  "cwd": str(ROOT)}),
                capture_output=True, text=True, timeout=30,
                env=dict(os.environ, MEMHUB_HARNESS_EXTRACT="0"))
            assert proc.returncode == 0 and proc.stdout == "", (mode, proc.stdout)
        # no state, no log, no draft: the directories were never even created
        assert not (env.base / "drafts").exists() and not (env.base / "log").exists()
        # …and in-process, the same answer without a spawn
        seen = []
        import io  # noqa: PLC0415
        real, real_stdin = hx.subprocess.Popen, sys.stdin
        hx.subprocess.Popen = lambda *a, **k: seen.append(a)
        sys.stdin = io.StringIO("{}")     # the flag-off path drains the payload
        try:
            assert hs.main(["stop"]) == 0 and hs.main(["prompt"]) == 0 and \
            hs.main(["extract", "--session", "s", "--transcript", str(tp)]) == 0
        finally:
            hx.subprocess.Popen, sys.stdin = real, real_stdin
        assert not seen and not (env.base / "drafts").exists()
    print("PASS test_with_the_flag_off_nothing_happens")


# ------------------------------------------------------------- stop lane
def test_stop_spawns_detached_and_returns():
    with _Env() as env:
        tp = env.base / "s.jsonl"
        tp.write_text("", encoding="utf-8")
        seen = []
        real = hx.subprocess.Popen
        hx.subprocess.Popen = lambda args, **kw: seen.append((args, kw))
        try:
            t0 = time.time()
            rc = hs.cmd_stop({"session_id": "sess", "transcript_path": str(tp),
                              "cwd": str(ROOT)})
            dt = time.time() - t0
        finally:
            hx.subprocess.Popen = real
        assert rc == 0 and dt < 0.5, dt
        modes = [a[2] for a, _ in seen]
        assert modes == ["extract"], modes
        for args, kw in seen:
            assert args[1].endswith("harness_stop.py")
            assert "--session" in args and "sess" in args
            assert kw["env"]["MEMHUB_HARNESS_CHILD"] == "1"
            if hasattr(os, "setsid"):
                assert kw["start_new_session"] is True
        # a re-entered Stop (stop_hook_active) is the same turn: nothing
        seen.clear()
        hx.subprocess.Popen = lambda args, **kw: seen.append((args, kw))
        try:
            hs.cmd_stop({"session_id": "sess", "transcript_path": str(tp),
                         "stop_hook_active": True})
            hs.cmd_stop({"session_id": "", "transcript_path": str(tp)})
            hs.cmd_stop({"session_id": "x", "transcript_path": "/nope"})
        finally:
            hx.subprocess.Popen = real
        assert not seen
    print("PASS test_stop_spawns_detached_and_returns")


def _classify(calls, reply):
    return lambda w, hint="", repo="", timeout=0: (
        calls.append({"window": w, "hint": hint, "repo": repo}), (reply, 0.1))[1]


def test_extract_classifies_the_last_turn_once():
    with _Env() as env:
        repo = _git_repo(env.base)
        tp = env.base / "s.jsonl"
        _transcript(tp, [
            ("do the thing", "done", [("Bash", {"command": "ls"}, "a\nb", False)]),
            ("no, i mean on staging", "right, staging",
             [("Bash", {"command": "git status"}, "clean", False)]),
        ])
        calls = []
        real = hx.server_classify
        hx.server_classify = _classify(calls, {"signal": True, "reason": "classified",
                                               "kind": "correction", "derivable": False})
        try:
            assert hs.cmd_extract("sess", str(tp), str(repo)) == 0
            # Stop fired twice for the same turn: no second call
            assert hs.cmd_extract("sess", str(tp), str(repo)) == 0
        finally:
            hx.server_classify = real
        assert len(calls) == 1
        assert calls[0]["hint"] == "wrong_target"
        assert "USER'S NEW MESSAGE: no, i mean on staging" in calls[0]["window"]
        assert "PREVIOUS USER MESSAGE: do the thing" in calls[0]["window"]
        moments = hx.read_drafts(hs.moments_path("sess"))
        assert len(moments) == 1 and moments[0]["source_ref"] == "sess#2"
        state = moments[0]["state"]
        assert state["repo"] == "repo" and state["turn"] == 2
        assert state["branch"] and state["head_sha"], state
        assert state["env"] in ("staging", "production", "unknown")
        meta = hs.load_meta("sess")
        assert meta["last_turn"] == 2 and meta["repo"] == "repo"
        assert not hx.drafts_path("sess").exists(), "nothing is drafted any more"
        # a third turn is a new moment to classify
        _transcript(tp, [
            ("do the thing", "done", []),
            ("no, i mean on staging", "right", []),
            ("we already have one of those", "ah", []),
        ])
        hx.server_classify = _classify(calls, {"signal": False, "reason": "classified"})
        try:
            hs.cmd_extract("sess", str(tp), str(repo))
        finally:
            hx.server_classify = real
        assert len(calls) == 2 and calls[1]["hint"] == "reuse_correction"
        assert hs.load_meta("sess")["last_turn"] == 3
        assert len(hx.read_drafts(hs.moments_path("sess"))) == 1
    print("PASS test_extract_classifies_the_last_turn_once")


# ------------------------------------------------------------ error arcs
def _post(repo: Path, session: str, cmd: str, resp: dict):
    payload = {"session_id": session, "cwd": str(repo), "tool_name": "Bash",
               "hook_event_name": "PostToolUse", "tool_input": {"command": cmd},
               "tool_response": resp}
    proc = subprocess.run([sys.executable, str(SCRIPTS / "rulebook_hook.py"), "post"],
                          input=json.dumps(payload), capture_output=True, text=True,
                          timeout=30, env=dict(os.environ))
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_a_failure_and_its_fix_are_one_moment():
    """§4.1: the hook's post lane pairs a Bash failure with the later success
    on the same command; the Stop sensor drains the pair and routes it."""
    with _Env() as env:
        repo = _git_repo(env.base)
        sys.path.insert(0, str(SCRIPTS))
        import rulebook_hook as rh  # noqa: PLC0415
        os.environ["MEMHUB_HARNESS_EXTRACT"] = "1"
        _post(repo, "arc", "pytest tests/x.py",
              {"stdout": "", "stderr": "ModuleNotFoundError: No module named 'y'",
               "exit_code": 1})
        _post(repo, "arc", "uv pip install y", {"stdout": "ok", "exit_code": 0})
        _post(repo, "arc", "pytest tests/x.py", {"stdout": "1 passed", "exit_code": 0})
        st = rh.load_state(rh.state_path("arc"))
        assert "pytest tests/x.py" not in st.get("arcs_open", {})
        closed = st.get("arcs_closed") or []
        assert len(closed) == 1, st
        assert closed[0]["target"] == "pytest tests/x.py"
        assert "ModuleNotFoundError" in closed[0]["signature"]
        assert closed[0]["cost"] == 2
        # drained once, then gone; the open set is emptied with it
        _post(repo, "arc", "make x", {"stdout": "", "stderr": "boom", "exit_code": 1})
        arcs = rh.take_error_arcs("arc")
        assert len(arcs) == 1 and arcs[0]["target"] == "pytest tests/x.py"
        assert rh.take_error_arcs("arc") == []
        st = rh.load_state(rh.state_path("arc"))
        assert not st.get("arcs_open") and not st.get("arcs_closed")
        # …and the router sees it as an error_arc on a turn that shows no error
        hits = hx.route({"n": 1, "user": "ok", "asst": "done", "tools": [], "results": []},
                        None, arcs=arcs)
        assert dict(hits).get("error_arc") == "missing-module"
        # with the flag off the lane records nothing
        os.environ["MEMHUB_HARNESS_EXTRACT"] = "0"
        _post(repo, "off", "pytest", {"stdout": "", "stderr": "E", "exit_code": 1})
        _post(repo, "off", "pytest", {"stdout": "ok", "exit_code": 0})
        st = rh.load_state(rh.state_path("off"))
        assert "arcs_open" not in st and "arcs_closed" not in st, st
    print("PASS test_a_failure_and_its_fix_are_one_moment")


def test_extract_records_the_flagged_moment_for_the_agent():
    with _Env() as env:
        repo = _git_repo(env.base)
        tp = env.base / "s.jsonl"
        _transcript(tp, [("do it", "done", []), ("no, i mean on staging", "ok", [])])
        calls = []
        real = hx.server_classify
        hx.server_classify = _classify(calls, {"signal": True, "reason": "classified",
                                               "kind": "correction", "derivable": True})
        try:
            hs.cmd_extract("sess", str(tp), str(repo))
        finally:
            hx.server_classify = real
        moments = hx.read_drafts(hs.moments_path("sess"))
        assert len(moments) == 1 and moments[0]["turn"] == 2
        assert moments[0]["kind"] == "correction" and moments[0]["hint"] == "wrong_target"
        assert moments[0]["derivable"] is True
        assert "USER'S NEW MESSAGE: no, i mean on staging" in moments[0]["window"]
        assert moments[0]["state"]["repo"] == "repo" and moments[0]["state"]["turn"] == 2
        # an outage on the next turn records nothing — it is not a quiet moment,
        # and it is not a signal either
        _transcript(tp, [("do it", "done", []), ("no, i mean on staging", "ok", []),
                         ("thanks", "np", [])])
        hx.server_classify = _classify(calls, {"signal": False, "reason": "judge_failed"})
        try:
            hs.cmd_extract("sess", str(tp), str(repo))
        finally:
            hx.server_classify = real
        assert len(hx.read_drafts(hs.moments_path("sess"))) == 1
    print("PASS test_extract_records_the_flagged_moment_for_the_agent")


# ------------------------------------------------------------- prompt lane
def _moment(n, kind="correction", hint="wrong_target", session="sess"):
    return {"turn": n, "source_ref": f"{session}#{n}", "hint": hint, "kind": kind,
            "reason": "no_engine", "window": f"USER'S NEW MESSAGE: moment {n}",
            "state": {"repo": "repo", "session_id": session, "turn": n, "hook_version": "0.54.0",
                      "at": "2026-09-10T00:00:00Z", "branch": "b", "head_sha": "abc",
                      "pr_number": None, "env": "staging"}}


def _prompt(payload):
    import io  # noqa: PLC0415
    out, real_stdout, real_stdin = io.StringIO(), sys.stdout, sys.stdin
    sys.stdout, sys.stdin = out, io.StringIO(json.dumps(payload))
    try:
        rc = hs.main(["prompt"])
    finally:
        sys.stdout, sys.stdin = real_stdout, real_stdin
    return rc, out.getvalue()


def test_the_next_prompt_hands_the_flagged_moment_to_the_agent_once():
    """Path A: the agent that lived the turn is the miner. One line, the
    stamp verbatim, the moment marked handed whether or not it files."""
    with _Env():
        hs.save_meta("sess", repo="repo", last_turn=2)
        hx.append_draft(hs.moments_path("sess"), _moment(2))
        rc, out = _prompt({"session_id": "sess", "prompt": "ok now run the migration"})
        assert rc == 0
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "turn 2" in ctx and "correction" in ctx and "router: wrong_target" in ctx
        assert '"session_id": "sess"' in ctx and '"turn": 2' in ctx, "the stamp rides verbatim"
        assert 'source_ref="sess#2"' in ctx and 'scope_repos=["repo"]' in ctx
        assert "Never pass activate" in ctx and "proposed" in ctx
        assert "create_rule" in ctx
        moments = hx.read_drafts(hs.moments_path("sess"))
        assert moments[0].get("handed_at"), "handed, so it is never nudged twice"
        assert hs.load_meta("sess")["nudges"] == 1
        # the next prompt: nothing left to hand
        rc, out = _prompt({"session_id": "sess", "prompt": "and the tests"})
        assert rc == 0 and out == ""
    print("PASS test_the_next_prompt_hands_the_flagged_moment_to_the_agent_once")


def test_a_stale_moment_a_harness_prompt_and_the_cap_are_never_nudged():
    with _Env():
        # stale: the session moved on NUDGE_MAX_AGE_TURNS turns before the prompt lane saw it
        hs.save_meta("sess", repo="repo", last_turn=9)
        hx.append_draft(hs.moments_path("sess"), _moment(2))
        assert _prompt({"session_id": "sess", "prompt": "next"}) == (0, "")
        assert not hx.read_drafts(hs.moments_path("sess"))[0].get("handed_at")
        # fresh, but the prompt is the harness talking to itself
        hs.save_meta("sess", last_turn=3)
        hx.append_draft(hs.moments_path("sess"), _moment(3))
        assert _prompt({"session_id": "sess", "prompt": "Skill /loop is running"}) == (0, "")
        # the newest fresh moment is the one handed, one per prompt
        hx.append_draft(hs.moments_path("sess"), _moment(4, kind="error_arc", hint="error_arc"))
        hs.save_meta("sess", last_turn=4)
        rc, out = _prompt({"session_id": "sess", "prompt": "go on"})
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "turn 4" in ctx and "turn 3" not in ctx
        # the per-session cap
        hs.save_meta("sess", nudges=hs.NUDGE_CAP_PER_SESSION, last_turn=5)
        hx.append_draft(hs.moments_path("sess"), _moment(5))
        assert _prompt({"session_id": "sess", "prompt": "more"}) == (0, "")
        # no session, no file: silence
        assert _prompt({"prompt": "x"}) == (0, "")
        assert _prompt({"session_id": "nobody", "prompt": "x"}) == (0, "")
    print("PASS test_a_stale_moment_a_harness_prompt_and_the_cap_are_never_nudged")


def test_a_subagents_stop_never_becomes_a_nudge():
    with _Env() as env:
        tp = env.base / "s.jsonl"
        tp.write_text("", encoding="utf-8")
        seen = []
        real = hx.subprocess.Popen
        hx.subprocess.Popen = lambda args, **kw: seen.append(args)
        try:
            hs.cmd_stop({"session_id": "sess", "transcript_path": str(tp),
                         "cwd": "", "agent_id": "agent-7f"})
        finally:
            hx.subprocess.Popen = real
        assert not seen
    print("PASS test_a_subagents_stop_never_becomes_a_nudge")


def test_the_nudge_line_carries_no_identity_and_no_activate():
    line = hs.nudge_line("sess", _moment(2), "repo")
    assert "activate" in line and "Never pass activate" in line
    assert "/Users/" not in line and "@" not in line
    assert len(line) < 1600, "one line of context, not a prompt"
    print("PASS test_the_nudge_line_carries_no_identity_and_no_activate")


def test_the_nudge_says_when_the_classifier_thinks_it_is_already_written_down():
    with_opinion = hs.nudge_line("sess", dict(_moment(2), derivable=True), "repo")
    assert "may already be written down in the repo" in with_opinion
    assert "may already be written down" not in hs.nudge_line("sess", _moment(2), "repo")
    print("PASS test_the_nudge_says_when_the_classifier_thinks_it_is_already_written_down")


def test_the_hooks_are_wired_behind_the_guard():
    doc = json.loads((ROOT / "plugins" / "memhub" / "hooks" / "claude-hooks.json")
                     .read_text(encoding="utf-8"))
    wired = {}
    for event, groups in doc["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                if "harness_stop.py" in handler["command"]:
                    wired[event] = handler
    assert set(wired) == {"Stop", "UserPromptSubmit"}, set(wired)
    assert wired["Stop"].get("async") is True
    assert wired["Stop"]["command"].rstrip("; fi").endswith("harness_stop.py\" stop")
    assert wired["UserPromptSubmit"]["command"].rstrip("; fi").endswith("harness_stop.py\" prompt")
    assert wired["UserPromptSubmit"]["timeout"] <= 5
    for h in wired.values():
        assert "claude_hook_guard.py\" ignore" in h["command"]
    print("PASS test_the_hooks_are_wired_behind_the_guard")


def test_the_sensor_never_spells_activate_as_a_sent_key():
    """Activation is a human act (§2, §5.1). The sensor sends nothing itself
    now — the agent files through create_rule — and the one place the word
    appears is the line telling the agent never to pass it."""
    src = (SCRIPTS / "harness_stop.py").read_text(encoding="utf-8")
    import re  # noqa: PLC0415
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"[\"']activate[\"']\s*:", code)
    assert "activate=" not in code and "call_tool" not in code
    print("PASS test_the_sensor_never_spells_activate_as_a_sent_key")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
