#!/usr/bin/env python3
"""Tests for the harness-tied memory client half (`harness_extract.py`).

What these protect: the router tells a person's turn from the harness's own
text; the window is redacted before it is sent; the classifier gets one
bounded call and every failure is "no signal" with a reason; a signal becomes
a stamped moment in a private file; nothing writes a rule. No test reaches a
server: `_api` is substituted.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "memhub"
sys.path.insert(0, str(PLUGIN / "scripts"))

import harness_extract as hx  # noqa: E402


def _turn(n=1, user="", asst="", tools=(), results=()):
    return {"n": n, "user": user, "asst": asst, "tools": list(tools),
            "results": list(results), "ts": "", "cwd": ""}


def _tool(name, target):
    return {"tool": name, "brief": f"{name}: {target}", "target": target}


def _result(name, target, error, text):
    return {"tool": name, "target": target, "error": error, "text": text}


class _Trace(list):
    def __call__(self, msg):
        self.append(msg)


# ------------------------------------------------------------------ router
def test_harness_text_is_not_a_persons_turn_but_pasted_markup_is():
    assert hx.is_harness_text("This session is being continued from a previous "
                              "conversation that ran out of context. i mean staging")
    assert hx.is_harness_text("Skill /loop is already loaded above")
    assert hx.is_harness_text("Base directory for this skill: /tmp/x")
    assert hx.is_harness_text("<local-command-caveat>Caveat</local-command-caveat>")
    assert hx.is_harness_text("[Request interrupted by user for tool use]")
    assert hx.is_harness_text("")
    # a person's prompt may begin with a heading or pasted markup
    assert not hx.is_harness_text("# Plan\nfix the flag test")
    assert not hx.is_harness_text("<div className=\"x\"> renders wrong, why?")
    assert not hx.is_harness_text("we already have a filter, why build another?")
    print("PASS test_harness_text_is_not_a_persons_turn_but_pasted_markup_is")


def test_a_markdown_prompt_keeps_its_own_actions():
    with tempfile.TemporaryDirectory() as td:
        recs = [
            {"type": "user", "uuid": "u1", "message": {"content": "run the tests"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
            {"type": "user", "uuid": "u2", "message": {"content": "# Plan\nnow the migration"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "alembic upgrade head"}}]}},
        ]
        path = Path(td) / "s.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
        turns = hx.turns_from_transcript(path)
        assert len(turns) == 2
        assert turns[0]["tools"] == []
        assert turns[1]["tools"][0]["target"] == "alembic upgrade head"
    print("PASS test_a_markdown_prompt_keeps_its_own_actions")


def test_router_kinds_fire_on_their_shape():
    assert "reuse_correction" in dict(hx.route(_turn(user="don't we already have a helper?"), None))
    assert "retraction" in dict(hx.route(_turn(asst="I was wrong — it never merged."), None))
    with_receipt = _turn(asst="Fixed and all tests pass.", tools=[_tool("Bash", "uv run pytest")])
    assert "claim_no_receipt" not in dict(hx.route(with_receipt, None))
    without = _turn(asst="Fixed and all tests pass.", tools=[_tool("Bash", "echo hi")])
    hits = hx.route(without, None)
    assert "claim_no_receipt" in dict(hits) and hx.router_hint(hits) == ""
    both = hx.route(_turn(user="i mean staging", asst="Fixed.", tools=[_tool("Bash", "ls")]), None)
    assert hx.router_hint(both) == "wrong_target"
    print("PASS test_router_kinds_fire_on_their_shape")


def test_error_arcs_close_and_a_costly_one_routes():
    unclosed = _turn(results=[_result("Bash", "pytest x", True, "boom")])
    assert hx.error_arcs(unclosed) == []
    closed = _turn(results=[_result("Bash", "pytest x", True, "ModuleNotFoundError: No module named 'y'"),
                            _result("Bash", "pytest x", False, "ok")])
    arcs = hx.error_arcs(closed)
    assert len(arcs) == 1 and arcs[0]["cost"] == 1
    assert dict(hx.route(closed, None))["error_arc"] == "missing-module"
    quiet = _turn(results=[_result("Bash", "make x", True, "new error"),
                           _result("Bash", "make x", False, "ok")])
    assert "error_arc" not in dict(hx.route(quiet, None))
    live = [{"signature": "new error", "target": "make x", "fix": "make x", "cost": 6}]
    hits = hx.route(quiet, None, arcs=live)
    assert dict(hits)["error_arc"] == "cost-6"
    assert sum(1 for k, _ in hits if k == "error_arc") == 1, "one arc, one hit"
    print("PASS test_error_arcs_close_and_a_costly_one_routes")


# ------------------------------------------------------------------ window
def test_the_window_carries_its_slots_and_is_redacted():
    prev = _turn(1, user="do the thing", asst="done", tools=[_tool("Bash", f"cmd{i}") for i in range(6)])
    cur = _turn(2, user="no, on staging — see /Users/colleague/dev/x and mail x@y.io",
                asst="ran it with --token=abcdef123456 done",
                tools=[_tool("Bash", f"new{i}") for i in range(9)],
                results=[_result("Bash", "new0", True, "user_email=dana@example.com home=/home/dana"),
                         _result("Bash", "new0", False, "ok")])
    win = hx.build_window(cur, prev, {"repo": "R"})
    assert "PREVIOUS USER MESSAGE: do the thing" in win
    assert "cmd2" in win and "cmd1" not in win
    assert "new5" in win and "new6" not in win
    assert "closed error arc" in win and "STATE:" in win
    red = hx.redact_window(win)
    for leak in ("colleague", "x@y.io", "dana@example.com", "/home/dana", "abcdef123456"):
        assert leak not in red, leak
    assert "~/dev/x" in red and "<email>" in red
    assert hx.redact_window("") == ""
    print("PASS test_the_window_carries_its_slots_and_is_redacted")


# -------------------------------------------------------------- classifier
class _Reply:
    def __init__(self, data, status=200):
        self.data, self.status, self.etag = data, status, None


class _Http:
    def __init__(self, answer=None, raise_exc=None):
        self.calls, self.answer, self.raise_exc = [], answer, raise_exc

    def rest(self, url, bearer, method="GET", body=None, headers=None, timeout=0):
        self.calls.append({"url": url, "method": method, "body": body, "timeout": timeout})
        if self.raise_exc:
            raise self.raise_exc
        return self.answer


def _with_api(http):
    real = hx._api
    hx._api = (lambda: ("https://h", "mhk_x", http)) if http is not None else (lambda: None)
    return real


def test_the_classifier_gets_one_bounded_post_and_never_raises():
    http = _Http(answer=_Reply({"signal": False, "reason": "classified"}))
    real = _with_api(http)
    try:
        reply, _ = hx.server_classify("W", hint="wrong_target", repo="R", timeout=7)
    finally:
        hx._api = real
    assert reply["signal"] is False and reply["reason"] == "classified"
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["method"] == "POST" and call["url"] == "https://h/v1/team/rulebook/harness/classify"
    assert call["body"] == {"window": "W", "hint": "wrong_target", "repo": "R"}
    assert call["timeout"] == 7
    for http, expected in (
        (_Http(raise_exc=RuntimeError("503")), "transport_error"),
        (_Http(answer=_Reply({"ok": True})), "bad_reply"),
        (_Http(answer=_Reply({"drafted": True, "row": {}})), "bad_reply"),
        (_Http(answer=_Reply("not an object")), "bad_reply"),
    ):
        real = _with_api(http)
        try:
            reply, _ = hx.server_classify("W")
        finally:
            hx._api = real
        assert reply["signal"] is False and reply["reason"] == expected, reply
        assert len(http.calls) == 1
    real = _with_api(None)
    try:
        assert hx.server_classify("W")[0] == {"signal": False, "reason": "no_credential"}
    finally:
        hx._api = real
    http = _Http(answer=_Reply({"signal": False, "reason": "classified"}))
    real = _with_api(http)
    try:
        hx.server_classify("x" * (hx.WINDOW_MAX_CHARS + 500))
    finally:
        hx._api = real
    assert len(http.calls[0]["body"]["window"]) == hx.WINDOW_MAX_CHARS
    print("PASS test_the_classifier_gets_one_bounded_post_and_never_raises")


def _run(td, turn, prev, http):
    out = Path(td) / "m.jsonl"
    stats, trace = hx.new_stats(), _Trace()
    real = _with_api(http)
    try:
        got = hx.extract_turn(turn, prev, session="sess", cwd="", repo="R", env_name="staging",
                              stats=stats, trace=trace, out_path=out, hook_version="0.54.0")
    finally:
        hx._api = real
    return got, stats, trace, out


def test_a_signal_becomes_a_private_stamped_moment():
    with tempfile.TemporaryDirectory() as td:
        http = _Http(answer=_Reply({"signal": True, "reason": "classified",
                                    "kind": "correction", "derivable": True}))
        prev = _turn(1, user="do it", asst="done")
        turn = _turn(2, user="no, i mean staging — ping dana@example.com", asst="ok")
        got, stats, trace, out = _run(td, turn, prev, http)
        assert got and stats["moments"] == 1 and stats["turns_sent"] == 1
        assert http.calls[0]["body"]["hint"] == "wrong_target"
        assert "dana@example.com" not in http.calls[0]["body"]["window"]
        rows = hx.read_jsonl(out)
        assert len(rows) == 1
        m = rows[0]
        assert (m["turn"], m["source_ref"], m["kind"], m["hint"], m["derivable"]) == (
            2, "sess#2", "correction", "wrong_target", True)
        for key in ("repo", "session_id", "turn", "hook_version", "at"):
            assert m["state"].get(key) not in (None, ""), key
        assert m["state"]["repo"] == "R" and m["state"]["session_id"] == "sess"
        assert "window" not in m, "the moment does not keep a copy of the session"
        if os.name != "nt":
            assert stat.S_IMODE(out.stat().st_mode) == 0o600
        # the trace names steps, never the prompt
        assert not any("staging" in line or "dana" in line for line in trace)
    print("PASS test_a_signal_becomes_a_private_stamped_moment")


def test_no_signal_an_outage_and_a_bare_claim_write_nothing():
    with tempfile.TemporaryDirectory() as td:
        quiet = _Http(answer=_Reply({"signal": False, "reason": "classified"}))
        got, stats, _, out = _run(td, _turn(1, user="thanks"), None, quiet)
        assert got is None and stats["reason"] == "classified" and not out.exists()
        down = _Http(raise_exc=OSError("connection refused"))
        got, stats, _, out = _run(td, _turn(1, user="i mean staging"), None, down)
        assert got is None and stats["transport_errors"] == 1 and not out.exists()
        claim_only = _Http(answer=_Reply({"signal": True, "reason": "classified", "kind": "x"}))
        got, stats, _, out = _run(td, _turn(1, user="ok", asst="Deployed.",
                                            tools=[_tool("Bash", "ls")]), None, claim_only)
        assert got is None and stats["turns_spared"] == 1 and claim_only.calls == []
    print("PASS test_no_signal_an_outage_and_a_bare_claim_write_nothing")


# ------------------------------------------------------------------- stamp
def test_the_stamp_never_guesses_a_repo_and_names_a_cross_repo_turn():
    assert hx.resolve_repo("", "", "") == ("", "")
    state = hx.stamp_state(session="s", turn={"n": 2, "tools": []}, cwd="",
                           hook_version="0.54.0", env_name="staging", default_repo="R")
    assert set(state) >= {"repo", "branch", "head_sha", "pr_number", "env",
                          "hook_version", "session_id", "turn", "at"}
    assert state["repo"] == "R" and state["branch"] == "" and state["turn"] == 2
    here, other = str(ROOT), str(ROOT.parent)
    turn = {"n": 1, "tools": [{"tool": "Bash", "target": f"cd {here} && git status"},
                              {"tool": "Bash", "target": f"cd {other} && ls"}]}
    resolved = {hx.resolve_repo("Bash", t["target"], "")[0] for t in turn["tools"]} - {""}
    state = hx.stamp_state(session="s", turn=turn, cwd="", hook_version="0.54.0", env_name="staging")
    if len(resolved) > 1:
        assert len(state.get("touched_repos") or []) > 1, state
    print("PASS test_the_stamp_never_guesses_a_repo_and_names_a_cross_repo_turn")


# ------------------------------------------------------------------- files
def test_session_files_are_confined_and_broken_lines_are_skipped():
    with tempfile.TemporaryDirectory() as td:
        os.environ["MEMHUB_HARNESS_DIR"] = td
        try:
            assert hx.session_file("abc", ".meta.json") == Path(td) / "abc.meta.json"
            assert hx.session_file("../../etc", ".x").parent == Path(td)
            target = hx.session_file("abc", ".moments.jsonl")
            hx.append_jsonl(target, {"turn": 1})
            with target.open("a") as fh:
                fh.write("{broken\n")
            hx.append_jsonl(target, {"turn": 2})
            assert [r["turn"] for r in hx.read_jsonl(target)] == [1, 2]
            assert hx.read_jsonl(Path(td) / "missing.jsonl") == []
        finally:
            del os.environ["MEMHUB_HARNESS_DIR"]
    print("PASS test_session_files_are_confined_and_broken_lines_are_skipped")


def test_the_flag_is_off_by_default():
    assert not hx.extract_enabled({})
    for off in ("0", "off", "false", ""):
        assert not hx.extract_enabled({"MEMHUB_HARNESS_EXTRACT": off}), off
    for on in ("1", "on", "true", "YES"):
        assert hx.extract_enabled({"MEMHUB_HARNESS_EXTRACT": on}), on
    print("PASS test_the_flag_is_off_by_default")


def test_spawn_detaches_and_returns():
    seen = {}
    real = hx.subprocess.Popen
    hx.subprocess.Popen = lambda args, **kw: seen.update(args=args, kw=kw)
    try:
        with tempfile.TemporaryDirectory() as td:
            os.environ["MEMHUB_HARNESS_DIR"] = td
            try:
                assert hx.spawn_detached(["extract", "--session", "s"],
                                         script=Path("/x/harness_stop.py"), log_name="stop.log") == 0
            finally:
                del os.environ["MEMHUB_HARNESS_DIR"]
    finally:
        hx.subprocess.Popen = real
    assert seen["args"][1:] == ["/x/harness_stop.py", "extract", "--session", "s"]
    if os.name != "nt":
        assert seen["kw"]["start_new_session"] is True
    print("PASS test_spawn_detaches_and_returns")


def test_nothing_in_the_client_writes_a_rule():
    src = (PLUGIN / "scripts" / "harness_extract.py").read_text(encoding="utf-8")
    for token in ("create_rule(", "call_tool", "harness/draft", '"activate"', "claude -p"):
        assert token not in src, token
    print("PASS test_nothing_in_the_client_writes_a_rule")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
