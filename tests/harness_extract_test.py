#!/usr/bin/env python3
"""Tests for the harness-tied memory client half (`harness_extract.py`).

What these protect: the transcript reader tells a person's turn from the
harness's own text; the window is redacted before it is sent; the classifier
gets one bounded call and every failure is "no signal" with a reason; nothing
writes a rule. No test reaches a server: `_api` is substituted.
"""
from __future__ import annotations

import json
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




# -------------------------------------------------------------- transcript
def test_harness_text_is_not_a_persons_turn_but_pasted_markup_is():
    assert hx.is_harness_text("This session is being continued from a previous "
                              "conversation that ran out of context. i mean staging")
    assert hx.is_harness_text("Skill /loop is already loaded above")
    assert hx.is_harness_text("Base directory for this skill: /tmp/x")
    assert hx.is_harness_text("<local-command-caveat>Caveat</local-command-caveat>")
    assert hx.is_harness_text("[Request interrupted by user for tool use]")
    assert hx.is_harness_text("Another Claude session sent a message:\n<agent-message from=\"a6a5\">"
                              "[Subagent hand-back] The text below is the final report")
    assert hx.is_harness_text("MemHub harness fork: 3 filed, 1 none, 0 failed")
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




def test_error_arcs_close_on_a_later_success():
    unclosed = _turn(results=[_result("Bash", "pytest x", True, "boom")])
    assert hx.error_arcs(unclosed) == []
    closed = _turn(results=[_result("Bash", "pytest x", True, "ModuleNotFoundError: No module named 'y'"),
                            _result("Bash", "pytest x", False, "ok")])
    arcs = hx.error_arcs(closed)
    assert len(arcs) == 1 and arcs[0]["cost"] == 1
    assert arcs[0]["signature"].startswith("ModuleNotFoundError")
    print("PASS test_error_arcs_close_on_a_later_success")


# ------------------------------------------------------------------ window
def test_the_window_carries_its_slots_and_is_redacted():
    prev = _turn(1, user="do the thing", asst="done",
                 tools=[dict(_tool("Bash", f"cmd{i}"), id=f"p{i}") for i in range(14)],
                 results=[dict(_result("Bash", "cmd13", False, "13 passed\nline2\nline3\nline4"), id="p13")])
    cur = _turn(2, user="no, on staging — see /Users/colleague/dev/x and mail x@y.io",
                asst="ran it with --token=abcdef123456 done",
                tools=[dict(_tool("Bash", f"new{i}"), id=f"n{i}") for i in range(14)],
                results=[dict(_result("Bash", "new0", True, "user_email=dana@example.com home=/home/dana"), id="n0"),
                         dict(_result("Bash", "new0", False, "ok"), id="n1")])
    older = [_turn(-1, user="first ask"), _turn(0, user="second ask")]
    win = hx.build_window(cur, prev, {"repo": "R"}, earlier=older, following="ok, merge it")
    assert "EARLIER USER MESSAGES" in win and "first ask" in win and "second ask" in win
    assert "PREVIOUS USER MESSAGE: do the thing" in win
    # the previous turn keeps its LAST 12 actions, this turn its FIRST 12
    assert "Bash: cmd2\n" in win and "Bash: cmd13\n" in win
    assert "Bash: cmd0\n" not in win and "Bash: cmd1\n" not in win
    assert "new11" in win and "new12" not in win
    # a result rides with the call it answers, matched by id: success, error, first 3 lines only
    assert "-> ok: 13 passed" in win and "line3" in win and "line4" not in win
    assert "-> ERROR: user_email" in win
    assert "closed error arc" in win and "STATE:" in win
    assert win.rstrip().endswith("USER'S FOLLOWING MESSAGE (what the person said next): ok, merge it")
    assert "FOLLOWING MESSAGE" not in hx.build_window(cur, prev, {"repo": "R"})
    red = hx.redact_window(win)
    for leak in ("colleague", "x@y.io", "dana@example.com", "/home/dana", "abcdef123456"):
        assert leak not in red, leak
    assert "~/dev/x" in red and "<email>" in red
    assert hx.redact_window("") == ""
    print("PASS test_the_window_carries_its_slots_and_is_redacted")


def test_the_largest_window_fits_the_server_limit():
    """Every slot at its cap still fits: the client cuts from the end, and the
    end is the following message."""
    big = "x" * 5000
    def turn(n, tag):
        return _turn(n, user=big, asst=big,
                     tools=[{"tool": "Bash", "brief": "Bash: " + "y" * 300, "target": f"t{i}", "id": f"{tag}{i}"}
                            for i in range(30)],
                     results=[{"tool": "Bash", "target": f"t{i}", "error": True, "text": big, "id": f"{tag}{i}"}
                              for i in range(30)])
    win = hx.build_window(turn(2, "c"), turn(1, "p"), {"repo": "R" * 200},
                          earlier=[turn(-1, "a"), turn(0, "b")], following=big)
    assert len(win) < hx.WINDOW_MAX_CHARS, len(win)
    print("PASS test_the_largest_window_fits_the_server_limit")


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
        reply, _ = hx.server_classify("W", repo="R", timeout=7)
    finally:
        hx._api = real
    assert reply["signal"] is False and reply["reason"] == "classified"
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["method"] == "POST" and call["url"] == "https://h/v1/team/rulebook/harness/classify"
    assert call["body"] == {"window": "W", "repo": "R"}
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


def test_the_flag_is_on_by_default_and_only_an_explicit_value_stops_it():
    """Default ON again (it was v0.69.0 through v0.75.x, then opt-in until
    0.120.x): the harness ships to everyone.

    An unset or blank variable is an install that never chose, so it is on.
    Anything set and unrecognised stays off: on costs the person a classifier
    call per turn and a fork per flagged turn on THEIR quota, so a value
    someone typed meaning off must not start the spend."""
    assert hx.extract_enabled({}), "unset is on"
    for off in ("0", "off", "false", "no", "OFF", "False", " 0 ", "NO",
                "anything-unrecognised", "\xa01"):
        assert not hx.extract_enabled({"MEMHUB_HARNESS_EXTRACT": off}), off
    for on in ("", " ", "1", "on", "true", "YES", " 1 ", "True"):
        assert hx.extract_enabled({"MEMHUB_HARNESS_EXTRACT": on}), on
    print("PASS test_the_flag_is_on_by_default_and_only_an_explicit_value_stops_it")


def test_an_authoring_child_is_never_sensed_whatever_the_flag_says():
    """The case judge starts `claude` with MEMHUB_HARNESS_CHILD=1, and Claude
    Code applies a settings.json `env` over what a process inherits — so an
    install that opted in with EXTRACT=1 there would sense the judge's own
    session. The child flag is the switch that setting cannot reach."""
    for flag in ("1", "true", "yes"):
        for extract in ("", "1", "on"):
            env = {"MEMHUB_HARNESS_CHILD": flag, "MEMHUB_HARNESS_EXTRACT": extract}
            assert not hx.extract_enabled(env), env
    for flag in ("", "0", "off"):
        assert hx.extract_enabled({"MEMHUB_HARNESS_CHILD": flag, "MEMHUB_HARNESS_EXTRACT": "1"}), flag
    print("PASS test_an_authoring_child_is_never_sensed_whatever_the_flag_says")




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
