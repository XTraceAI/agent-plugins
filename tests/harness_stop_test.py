#!/usr/bin/env python3
"""Tests for the Stop sensor (harness-tied-memory-spec §4.1–§4.4).

What these protect: with the flag off nothing happens at all; with it on the
hook returns in milliseconds and every piece of work happens in a detached
child; a failure and its fix are one moment; the review keeps or drops rows
and never rewrites or activates one; the sync files rows `proposed` with the
five-key stamp and never passes `activate`. No test reaches a server or a
model — the two boundaries (`server_draft`, `subprocess.run` for the review,
`mcp_http.call_tool` for the sync) are substituted.
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
        for mode in ("stop", "session", "extract", "review", "sync", "idle", "pr-open"):
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "harness_stop.py"), mode,
                 "--session", "s", "--transcript", str(tp), "--pr", "1"],
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
            assert hs.main(["stop"]) == 0 and hs.main(["extract", "--session", "s",
                                                        "--transcript", str(tp)]) == 0
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
        assert modes == ["extract", "idle"], modes
        for args, kw in seen:
            assert args[1].endswith("harness_stop.py")
            assert "--session" in args and "sess" in args
            assert kw["env"]["MEMHUB_HARNESS_CHILD"] == "1"
            if hasattr(os, "setsid"):
                assert kw["start_new_session"] is True
        # a live idle waiter is not spawned twice
        hs.save_meta("sess", idle_pid=os.getpid())
        seen.clear()
        hx.subprocess.Popen = lambda args, **kw: seen.append((args, kw))
        try:
            hs.cmd_stop({"session_id": "sess", "transcript_path": str(tp), "cwd": ""})
        finally:
            hx.subprocess.Popen = real
        assert [a[2] for a, _ in seen] == ["extract"]
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


def test_extract_drafts_the_last_turn_once():
    with _Env() as env:
        repo = _git_repo(env.base)
        tp = env.base / "s.jsonl"
        _transcript(tp, [
            ("do the thing", "done", [("Bash", {"command": "ls"}, "a\nb", False)]),
            ("no, i mean on staging", "right, staging",
             [("Bash", {"command": "git status"}, "clean", False)]),
        ])
        calls = []
        real = hx.server_draft
        hx.server_draft = lambda w, hint="", repo="", timeout=0: (
            calls.append({"window": w, "hint": hint, "repo": repo}),
            ({"drafted": True, "reason": "drafted", "kind": "correction", "row": _row()}, 0.1))[1]
        try:
            assert hs.cmd_extract("sess", str(tp), str(repo)) == 0
            # Stop fired twice for the same turn: no second call
            assert hs.cmd_extract("sess", str(tp), str(repo)) == 0
        finally:
            hx.server_draft = real
        assert len(calls) == 1
        assert calls[0]["hint"] == "wrong_target"
        assert "USER'S NEW MESSAGE: no, i mean on staging" in calls[0]["window"]
        assert "PREVIOUS USER MESSAGE: do the thing" in calls[0]["window"]
        rows = hx.read_drafts(hx.drafts_path("sess"))
        assert len(rows) == 1 and rows[0]["source_ref"] == "sess#2"
        state = rows[0]["state"]
        assert state["repo"] == "repo" and state["turn"] == 2
        assert state["branch"] and state["head_sha"], state
        assert state["env"] in ("staging", "production", "unknown")
        meta = hs.load_meta("sess")
        assert meta["last_turn"] == 2 and meta["drafts"] == 1
        assert meta["repo"] == "repo"
        # a third turn is a new moment
        _transcript(tp, [
            ("do the thing", "done", []),
            ("no, i mean on staging", "right", []),
            ("we already have one of those", "ah", []),
        ])
        hx.server_draft = lambda w, hint="", repo="", timeout=0: (
            calls.append({"hint": hint}),
            ({"drafted": False, "reason": "project_state", "kind": "correction"}, 0.1))[1]
        try:
            hs.cmd_extract("sess", str(tp), str(repo))
        finally:
            hx.server_draft = real
        assert len(calls) == 2 and calls[1]["hint"] == "reuse_correction"
        assert hs.load_meta("sess")["last_turn"] == 3
        assert len(hx.read_drafts(hx.drafts_path("sess"))) == 1
    print("PASS test_extract_drafts_the_last_turn_once")


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


# ------------------------------------------------------------- the review
class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _drafts(session, rows):
    path = hx.drafts_path(session)
    for r in rows:
        hx.append_draft(path, r)
    return path


def _draft_row(n, session="sess"):
    row, why = hx.build_row(_row(n), state={"repo": "repo", "session_id": session,
                                            "turn": n, "hook_version": "0.53.0",
                                            "at": "2026-09-09T00:00:00Z", "branch": "b",
                                            "head_sha": "abc", "pr_number": None,
                                            "env": "staging"},
                            session=session, turn_n=n, reason="router:x",
                            scope_repos=["repo"])
    assert row, why
    return row


def test_the_review_keeps_or_drops_and_never_rewrites_or_activates():
    with _Env() as env:
        hs.save_meta("sess", repo="repo", cwd=str(env.base))
        _drafts("sess", [_draft_row(1), _draft_row(2), _draft_row(3)])
        seen = {}
        real = hs.subprocess.run

        def fake(cmd, **kw):
            seen.update(cmd=cmd, kw=kw)
            return _Proc(stdout=json.dumps({"structured_output": {
                "keep": [{"index": 3, "supersedes_rule_id": "not-in-book", "why": "good"},
                         {"index": 1, "supersedes_rule_id": None, "why": "fine"},
                         {"index": 9, "supersedes_rule_id": None, "why": "bogus"}],
                "drop": [{"index": 2, "why": "duplicate of 1"}]}}))

        hs.subprocess.run = fake
        try:
            kept = hs.review("sess", moment="test", pr_number=42)
        finally:
            hs.subprocess.run = real
        assert kept == 2
        cmd = seen["cmd"]
        assert cmd[:2] == ["claude", "-p"] and "--safe-mode" in cmd
        assert "--json-schema" in cmd and "--no-session-persistence" in cmd
        assert seen["kw"]["env"]["MEMHUB_HARNESS_CHILD"] == "1"
        assert "CLAUDECODE" not in seen["kw"]["env"]
        assert seen["kw"]["timeout"] == hs.REVIEW_TIMEOUT_S
        assert "BUDGET: keep at most 8" in seen["kw"]["input"]
        assert "[3] T3" in seen["kw"]["input"]
        rows = hx.read_drafts(hs.reviewed_path("sess"))
        assert [r["source_ref"] for r in rows] == ["sess#3", "sess#1"]
        # never rewritten, never activated, stamped with the PR
        assert rows[0]["statement"] == _draft_row(3)["statement"]
        assert rows[0]["supersedes_rule_id"] is None, "an id not in the book is dropped"
        assert all("activate" not in r for r in rows)
        assert all(r["state"]["pr_number"] == 42 for r in rows)
        meta = hs.load_meta("sess")
        assert meta["reviewed_through"] == 3 and meta["dropped"] == 1
        assert meta["pr_number"] == 42
        # nothing new: no second model call
        hs.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no"))
        try:
            assert hs.review("sess", moment="again") == -1
        finally:
            hs.subprocess.run = real
    print("PASS test_the_review_keeps_or_drops_and_never_rewrites_or_activates")


def test_a_failed_review_leaves_the_drafts_and_gives_up_after_three():
    with _Env():
        hs.save_meta("sess", repo="repo")
        _drafts("sess", [_draft_row(1)])
        real = hs.subprocess.run
        hs.subprocess.run = lambda *a, **k: _Proc(returncode=1, stderr="boom")
        try:
            for attempt in (1, 2):
                assert hs.review("sess", moment="t") == -1
                meta = hs.load_meta("sess")
                assert meta.get("reviewed_through", 0) == 0
                assert meta["review_failures"] == attempt
            assert hs.review("sess", moment="t") == -1
        finally:
            hs.subprocess.run = real
        meta = hs.load_meta("sess")
        assert meta["reviewed_through"] == 1 and meta.get("review_gave_up")
        assert not hs.reviewed_path("sess").exists()
        assert len(hx.read_drafts(hx.drafts_path("sess"))) == 1, "drafts stay for a human"
        # an off-contract reply is a failure too, never a partial review
        _drafts("sess", [_draft_row(2)])
        for out in (_Proc(stdout="not json"),
                    _Proc(stdout=json.dumps({"result": "I would need more context"})),
                    _Proc(stdout=json.dumps({"is_error": True, "result": "rate limited"}))):
            hs.subprocess.run = lambda *a, **k: out
            try:
                assert hs.review("sess", moment="t") == -1
            finally:
                hs.subprocess.run = real
        assert not hs.reviewed_path("sess").exists()
    print("PASS test_a_failed_review_leaves_the_drafts_and_gives_up_after_three")


def test_the_review_mines_rows_from_flagged_moments():
    """Direction from the owner: the classifier is the server's, the lesson
    mining is the LOCAL agent's. The review receives the flagged moments and
    authors rows itself; each row is stamped from its moment, PII-checked
    and twin-checked exactly like a server row, and the budget is shared
    with kept drafts."""
    with _Env() as env:
        hs.save_meta("sess", repo="repo", cwd=str(env.base))
        state = {"repo": "repo", "session_id": "sess", "turn": 5, "hook_version": "0.53.0",
                 "at": "2026-09-10T00:00:00Z", "branch": "b", "head_sha": "abc",
                 "pr_number": None, "env": "staging"}
        for n in (5, 6, 7):
            hx.append_draft(hs.moments_path("sess"), {
                "turn": n, "source_ref": f"sess#{n}", "hint": "", "kind": "correction",
                "reason": "no_engine", "window": f"USER'S NEW MESSAGE: moment {n}",
                "state": dict(state, turn=n)})
        seen = {}
        real = hs.subprocess.run

        def fake(cmd, **kw):
            seen.update(kw=kw)
            return _Proc(stdout=json.dumps({"structured_output": {
                "keep": [], "drop": [],
                "author": [
                    {"moment": 1, "title": "Fetch before origin reads",
                     "statement": "When reading origin/* refs, run git fetch first because a stale ref answers wrong.",
                     "engine": "matcher",
                     "matcher": {"event": "bash", "command_rx": "git\\s+log\\s+\\S*origin/",
                                 "command_not_rx": None, "path_rx": None, "path_not_rx": None, "content_rx": None},
                     "ordering": None, "anchors": None, "supersedes_rule_id": None, "rationale": "r"},
                    {"moment": 2, "title": "Colleague path",
                     "statement": "When editing /Users/colleague/dev/x, stop because it is not yours to edit.",
                     "engine": "anchors", "matcher": None, "ordering": None,
                     "anchors": ["/Users/colleague/dev/x"], "supersedes_rule_id": None, "rationale": "r"},
                    {"moment": 3, "title": "No engine",
                     "statement": "When doing the thing, do the other thing first because reasons abound.",
                     "engine": "anchors", "matcher": None, "ordering": None,
                     "anchors": ["the staging database"], "supersedes_rule_id": None, "rationale": "r"},
                    {"moment": 9, "title": "Bogus", "statement": "x" * 30, "engine": "anchors",
                     "matcher": None, "ordering": None, "anchors": ["a.py"],
                     "supersedes_rule_id": None, "rationale": "r"},
                ]}}))

        hs.subprocess.run = fake
        try:
            got = hs.review("sess", moment="test")
        finally:
            hs.subprocess.run = real
        assert got == 1, got
        assert "[M1] turn 5" in seen["kw"]["input"] and "moment 7" in seen["kw"]["input"]
        rows = hx.read_drafts(hs.reviewed_path("sess"))
        assert len(rows) == 1
        row = rows[0]
        assert row["source_ref"] == "sess#5" and row["state"]["turn"] == 5
        assert row["_reason"] == "local:correction" and row["_review"]["authored"]
        assert row["matcher"] == {"event": "bash", "command_rx": "git\\s+log\\s+\\S*origin/"}
        assert all(row["state"].get(k) for k in hs.STATE_KEYS)
        meta = hs.load_meta("sess")
        assert meta["mined_through"] == 3
        assert meta["mining_refused"] == {"pii_in_row": 1, "anchors_not_identifiers": 1,
                                           "moment_out_of_range": 1}, meta["mining_refused"]
        assert (Path(os.environ["MEMHUB_HARNESS_DRAFTS"]) / "sess.verdict.json").exists()
        # nothing new: no second call
        hs.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("no"))
        try:
            assert hs.review("sess", moment="again") == -1
        finally:
            hs.subprocess.run = real
    print("PASS test_the_review_mines_rows_from_flagged_moments")


def test_extract_records_the_flagged_moment_for_the_local_miner():
    with _Env() as env:
        repo = _git_repo(env.base)
        tp = env.base / "s.jsonl"
        _transcript(tp, [("do it", "done", []), ("no, i mean on staging", "ok", [])])
        real = hx.server_draft
        # the server's author refused — the moment is still the local agent's to mine
        hx.server_draft = lambda w, hint="", repo="", timeout=0: (
            {"drafted": False, "reason": "project_state", "kind": "correction"}, 0.1)
        try:
            hs.cmd_extract("sess", str(tp), str(repo))
        finally:
            hx.server_draft = real
        moments = hx.read_drafts(hs.moments_path("sess"))
        assert len(moments) == 1 and moments[0]["turn"] == 2
        assert moments[0]["kind"] == "correction" and moments[0]["hint"] == "wrong_target"
        assert "USER'S NEW MESSAGE: no, i mean on staging" in moments[0]["window"]
        assert moments[0]["state"]["repo"] == "repo"
        assert not hx.drafts_path("sess").exists()
        # a no_signal turn records nothing
        _transcript(tp, [("do it", "done", []), ("no, i mean on staging", "ok", []),
                         ("thanks", "np", [])])
        hx.server_draft = lambda w, hint="", repo="", timeout=0: (
            {"drafted": False, "reason": "no_signal", "kind": None}, 0.1)
        try:
            hs.cmd_extract("sess", str(tp), str(repo))
        finally:
            hx.server_draft = real
        assert len(hx.read_drafts(hs.moments_path("sess"))) == 1
    print("PASS test_extract_records_the_flagged_moment_for_the_local_miner")


# ---------------------------------------------------------------- the sync
class _Block:
    def __init__(self, text):
        self.text = text


class _Res:
    def __init__(self, structured=None, text=None, is_error=False):
        self.structured = structured
        self.content = [_Block(text)] if text else []
        self.is_error = is_error


def test_sync_files_rows_proposed_with_the_stamp_and_never_activates():
    with _Env():
        hs.save_meta("sess", repo="repo")
        rows = [dict(_draft_row(1), _review={"index": 1}),
                dict(_draft_row(2), _review={"index": 2}),
                dict(_draft_row(3), _review={"index": 3})]
        rows[2]["state"] = dict(rows[2]["state"], hook_version="")   # a stampless row
        hs.write_rows(hs.reviewed_path("sess"), rows)
        import mcp_http  # noqa: PLC0415
        calls = []
        answers = iter([
            _Res(structured={"rule_id": "11111111-1111-1111-1111-111111111111",
                             "status": "proposed"}),
            _Res(text="twin of rule 2222", is_error=True),
        ])
        real_call, real_auth = mcp_http.call_tool, hs.sync.__globals__.get("resolve_bearer")
        mcp_http.call_tool = lambda url, bearer, name, args, timeout=0: (
            calls.append((name, args, timeout)), next(answers))[1]
        import _memhub_auth  # noqa: PLC0415
        real_resolve = _memhub_auth.resolve_bearer
        _memhub_auth.resolve_bearer = lambda url=None, refresh=True: ("https://h/mcp", "mhk_x")
        try:
            filed = hs.sync("sess")
        finally:
            mcp_http.call_tool = real_call
            _memhub_auth.resolve_bearer = real_resolve
        assert filed == 1
        assert [c[0] for c in calls] == ["create_rule", "create_rule"]
        body = calls[0][1]
        assert body["source"] == "session_draft" and body["source_ref"] == "sess#1"
        assert "activate" not in body and "status" not in body and "mode" not in body
        assert all(body["state"].get(k) for k in hs.STATE_KEYS)
        assert body["delivery"] == "agent_hook" and body["matcher"]["command_rx"] == "cmd1\\b"
        assert not any(k.startswith("_") for k in body), body
        assert calls[0][2] == hs.SYNC_TIMEOUT_S
        after = hx.read_drafts(hs.reviewed_path("sess"))
        assert after[0]["rule_id"] == "11111111-1111-1111-1111-111111111111"
        assert after[1].get("_sync_refused", "").startswith("twin")
        assert after[2]["_sync_refused"].startswith("state_missing:hook_version")
        # the watermark holds: a second pass sends nothing
        mcp_http.call_tool = lambda *a, **k: (_ for _ in ()).throw(AssertionError("resent"))
        _memhub_auth.resolve_bearer = lambda url=None, refresh=True: ("https://h/mcp", "mhk_x")
        try:
            assert hs.sync("sess") == 0
        finally:
            mcp_http.call_tool = real_call
            _memhub_auth.resolve_bearer = real_resolve
        # a transport failure is retried next time, not marked refused
        hs.write_rows(hs.reviewed_path("sess"), [dict(_draft_row(4), _review={"index": 4})])
        mcp_http.call_tool = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        _memhub_auth.resolve_bearer = lambda url=None, refresh=True: ("https://h/mcp", "mhk_x")
        try:
            assert hs.sync("sess") == 0
        finally:
            mcp_http.call_tool = real_call
            _memhub_auth.resolve_bearer = real_resolve
        row = hx.read_drafts(hs.reviewed_path("sess"))[0]
        assert row.get("_sync_error") and not row.get("rule_id") and not row.get("_sync_refused")
    print("PASS test_sync_files_rows_proposed_with_the_stamp_and_never_activates")


# ---------------------------------------------------------------- idle
def test_the_idle_waiter_reviews_after_silence_and_exits():
    """§4.3: a session silent for IDLE_S is reviewed once; a transcript that
    keeps moving keeps the waiter waiting; a deleted one ends the wait."""
    with _Env() as env:
        tp = env.base / "s.jsonl"
        tp.write_text("", encoding="utf-8")
        old = (hs.IDLE_S, hs.IDLE_POLL_S, hs.review_and_sync)
        moments = []
        hs.IDLE_S, hs.IDLE_POLL_S = 0.3, 0.05
        hs.review_and_sync = lambda session, moment, pr_number=None: moments.append(moment)
        try:
            t0 = time.time()
            os.utime(tp, (t0 - 10, t0 - 10))          # already silent
            assert hs.cmd_idle("sess", str(tp), "") == 0
            assert moments == ["idle"] and time.time() - t0 < 5
            assert hs.load_meta("sess")["idle_pid"] == os.getpid()
            # a transcript still being written is not idle yet
            moments.clear()
            tp.unlink()
            assert hs.cmd_idle("sess", str(tp), "") == 0
            assert moments == ["idle"], "a vanished transcript ends the wait with one review"
        finally:
            hs.IDLE_S, hs.IDLE_POLL_S, hs.review_and_sync = old
    print("PASS test_the_idle_waiter_reviews_after_silence_and_exits")


def test_the_sensor_never_spells_activate():
    """Activation is a human act (§2, §5.1). The word must not be in the
    sensor at all, so no future edit can pass it by accident."""
    src = (SCRIPTS / "harness_stop.py").read_text(encoding="utf-8")
    import re  # noqa: PLC0415
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"[\"']activate[\"']\s*:", code), "activate must never be a sent key"
    assert "activate=" not in code
    print("PASS test_the_sensor_never_spells_activate")


# ------------------------------------------------------------ session start
def test_session_start_reviews_syncs_and_announces_once():
    with _Env() as env:
        repo = _git_repo(env.base)
        name = hs.repo_of(str(repo))
        assert name == "repo", name
        # an earlier session with un-reviewed drafts → review spawned
        hs.save_meta("old1", repo=name, drafts=2, reviewed_through=0)
        # one with reviewed rows not yet sent → sync spawned
        hs.save_meta("old2", repo=name, drafts=1, reviewed_through=1)
        hs.write_rows(hs.reviewed_path("old2"), [dict(_draft_row(1, "old2"))])
        # one filed and not yet announced → one systemMessage line
        hs.save_meta("old3", repo=name, drafts=1, reviewed_through=1)
        hs.write_rows(hs.reviewed_path("old3"),
                      [dict(_draft_row(1, "old3"), rule_id="r-1", title="Fetch before origin reads")])
        # another repo's session is not this repo's business
        hs.save_meta("other", repo="elsewhere", drafts=5, reviewed_through=0)
        seen = []
        real = hx.subprocess.Popen
        hx.subprocess.Popen = lambda args, **kw: seen.append(args)
        import io  # noqa: PLC0415
        out = io.StringIO()
        real_stdout = sys.stdout
        sys.stdout = out
        try:
            rc = hs.cmd_session({"session_id": "new", "cwd": str(repo)})
        finally:
            sys.stdout = real_stdout
            hx.subprocess.Popen = real
        assert rc == 0
        spawned = sorted((a[2], a[a.index("--session") + 1]) for a in seen)
        assert spawned == [("review", "old1"), ("sync", "old2")], spawned
        msg = json.loads(out.getvalue())["systemMessage"]
        assert "1 rule(s) proposed" in msg and "Fetch before origin reads" in msg
        assert hs.load_meta("old3")["announced"] is True
        # second start: announced already, nothing printed
        out = io.StringIO()
        sys.stdout = out
        hx.subprocess.Popen = lambda args, **kw: None
        try:
            hs.cmd_session({"session_id": "new2", "cwd": str(repo)})
        finally:
            sys.stdout = real_stdout
            hx.subprocess.Popen = real
        assert out.getvalue() == ""
    print("PASS test_session_start_reviews_syncs_and_announces_once")


# -------------------------------------------------------------- PR open
def test_pr_create_spawns_the_review_only_with_the_flag_on():
    import pr_babysit_trigger as pt  # noqa: PLC0415
    seen = []
    real = pt.subprocess.Popen
    pt.subprocess.Popen = lambda args, **kw: seen.append((args, kw))
    try:
        payload = {"session_id": "sess", "tool_input": {"command": "gh pr create -f"},
                   "tool_response": {"stdout": "https://github.com/o/r/pull/77\n"}}
        os.environ.pop("MEMHUB_HARNESS_EXTRACT", None)
        pt._harness_review(payload, "https://github.com/o/r/pull/77")
        assert not seen
        os.environ["MEMHUB_HARNESS_EXTRACT"] = "1"
        pt._harness_review(payload, "https://github.com/o/r/pull/77")
        pt._harness_review({"session_id": ""}, "https://github.com/o/r/pull/77")
    finally:
        pt.subprocess.Popen = real
        os.environ.pop("MEMHUB_HARNESS_EXTRACT", None)
    assert len(seen) == 1
    args, kw = seen[0]
    assert args[1].endswith("harness_stop.py") and args[2] == "pr-open"
    assert args[args.index("--pr") + 1] == "77"
    assert kw["env"]["MEMHUB_HARNESS_CHILD"] == "1"
    print("PASS test_pr_create_spawns_the_review_only_with_the_flag_on")


def test_the_hooks_are_wired_behind_the_guard():
    doc = json.loads((ROOT / "plugins" / "memhub" / "hooks" / "claude-hooks.json")
                     .read_text(encoding="utf-8"))
    wired = {}
    for event, groups in doc["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                if "harness_stop.py" in handler["command"]:
                    wired[event] = handler
    assert set(wired) == {"Stop", "SessionStart"}, set(wired)
    assert wired["Stop"].get("async") is True
    assert '"stop"' not in wired["Stop"]["command"]   # the mode is a bare word
    assert wired["Stop"]["command"].rstrip("; fi").endswith("harness_stop.py\" stop")
    assert wired["SessionStart"]["command"].rstrip("; fi").endswith("harness_stop.py\" session")
    assert wired["SessionStart"]["timeout"] <= 5
    for h in wired.values():
        assert "claude_hook_guard.py\" ignore" in h["command"]
    print("PASS test_the_hooks_are_wired_behind_the_guard")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
