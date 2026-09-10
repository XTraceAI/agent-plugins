#!/usr/bin/env python3
"""Tests for the harness extractor (harness-tied-memory-spec §4.2).

The contract these protect: a bounded, fail-open, detached extractor that
emits COMPLETE rows or none at all, never fires anything, never files a rule,
and never writes anywhere but its local drafts file.

No test here reaches a server. The network boundary is `server_classify` (one
POST), and every test that needs a verdict substitutes one — a test suite
that spends money and needs a network is a test suite people stop running.
"""
from __future__ import annotations

import json
import os
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


# ------------------------------------------------------------------ router
def test_router_ignores_harness_generated_user_text():
    """A /compact summary quotes the WHOLE conversation back in the user role.

    Every router regex matches it at once — one compaction manufactured a hit
    on every kind simultaneously in the S0 corpus. Harness text is not a human
    turn and must never reach the router.
    """
    compaction = ("This session is being continued from a previous "
                  "conversation that ran out of context. Summary: the user "
                  "said i mean staging, we already have a filter, and i was "
                  "wrong about the branch.")
    assert hx.is_harness_text(compaction)
    assert hx.is_harness_text("Skill /loop is already loaded above")
    assert hx.is_harness_text("Base directory for this skill: /tmp/x")
    assert hx.is_harness_text("")
    # …and a real correction that merely resembles one is still a human turn.
    assert not hx.is_harness_text("we already have a filter llm, "
                                  "why can't we improve that prompt?")
    print("PASS test_router_ignores_harness_generated_user_text")


def test_router_kinds_fire_on_their_shape():
    hits = dict(hx.route(_turn(user="don't we already have a helper for this?"),
                         None))
    assert "reuse_correction" in hits

    hits = dict(hx.route(_turn(asst="I was wrong — it never merged."), None))
    assert "retraction" in hits

    # A claim with a receipt in the recent actions is NOT a hit.
    with_receipt = _turn(asst="Fixed and all tests pass.",
                         tools=[_tool("Bash", "uv run pytest tests/")])
    assert "claim_no_receipt" not in dict(hx.route(with_receipt, None))
    without = _turn(asst="Fixed and all tests pass.",
                    tools=[_tool("Bash", "echo hi")])
    assert "claim_no_receipt" in dict(hx.route(without, None))
    print("PASS test_router_kinds_fire_on_their_shape")


def test_claim_moments_are_counted_but_never_sent():
    """Spec §1/§5.1: the claim-shaped lesson is a built-in Stop check, not a
    rules row. It was 76% of router hits on the S0 corpus, and a turn whose
    only hit is claim-shaped is the call the router spares."""
    assert "claim_no_receipt" in hx.NOT_AUTHORED
    assert "retraction" not in hx.NOT_AUTHORED
    only_claim = hx.route(_turn(asst="Fixed.", tools=[_tool("Bash", "ls")]), None)
    assert only_claim and hx.router_hint(only_claim) == ""
    both = hx.route(_turn(user="i mean staging", asst="Fixed.",
                          tools=[_tool("Bash", "ls")]), None)
    assert hx.router_hint(both) == "wrong_target"
    print("PASS test_claim_moments_are_counted_but_never_sent")


def test_error_arc_needs_to_close():
    """A failure that never got fixed is a failure, not a lesson: the pair
    (what broke, what fixed it) is the whole content."""
    unclosed = _turn(results=[_result("Bash", "pytest x", True, "boom")])
    assert hx.error_arcs(unclosed) == []
    closed = _turn(results=[_result("Bash", "pytest x", True,
                                    "ModuleNotFoundError: No module named 'y'"),
                            _result("Bash", "pytest x", False, "ok")])
    arcs = hx.error_arcs(closed)
    assert len(arcs) == 1 and arcs[0]["target"] == "pytest x"
    assert arcs[0]["cost"] == 1
    assert "error_arc" in dict(hx.route(closed, None))
    print("PASS test_error_arc_needs_to_close")


def test_a_costly_arc_routes_without_a_known_trap():
    """§4.0: a closed arc that cost ≥ 5 tool calls is routed whatever its
    signature. The hook's PostToolUse pairing hands such arcs in live; they
    join the transcript's, deduplicated by target."""
    quiet = _turn(results=[_result("Bash", "make x", True, "some new error"),
                           _result("Bash", "make x", False, "ok")])
    assert "error_arc" not in dict(hx.route(quiet, None))
    live = [{"signature": "some new error", "target": "make x", "fix": "make x",
             "cost": 6}]
    hits = hx.route(quiet, None, arcs=live)
    assert dict(hits).get("error_arc") == "cost-6"
    assert sum(1 for k, _ in hits if k == "error_arc") == 1, "one arc, one hit"
    print("PASS test_a_costly_arc_routes_without_a_known_trap")


# ------------------------------------------------------------------ window
def test_window_carries_the_spec_slots():
    prev = _turn(1, user="do the thing", asst="done",
                 tools=[_tool("Bash", f"cmd{i}") for i in range(6)])
    cur = _turn(2, user="no, on staging", asst="right, staging",
                tools=[_tool("Bash", f"new{i}") for i in range(9)],
                results=[_result("Bash", "new0", True, "connection refused"),
                         _result("Bash", "new0", False, "ok")])
    win = hx.build_window(cur, prev, {"repo": "R"})
    assert "PREVIOUS USER MESSAGE: do the thing" in win
    assert "USER'S NEW MESSAGE: no, on staging" in win
    # last 4 of the previous turn, first 6 of this one — §4.2 step 1
    assert "cmd2" in win and "cmd1" not in win
    assert "new5" in win and "new6" not in win
    assert "connection refused" in win
    assert "closed error arc" in win
    assert "STATE:" in win
    print("PASS test_window_carries_the_spec_slots")


def test_the_window_is_redacted_before_it_leaves_the_machine():
    """S0 Finding 1, the first thing S1 carries. Tool output is in the window
    and untrusted; the author read a colleague's username out of an
    org-members listing and built a regex from it. Every shape that leaked
    is gone before the POST, and a credential shape the rulebook hook's own
    recall lane already strips is gone too."""
    cur = _turn(2, user="check /Users/colleague/dev/thing and mail x@y.io",
                asst="ran it with --token=abcdef123456 done",
                tools=[_tool("Bash", "curl -H 'Authorization: Bearer eyJabc.def.ghi' "
                                     "https://h/x mhk_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123")],
                results=[_result("Bash", "psql", True,
                                 "user_email=dana@example.com home=/home/dana")])
    win = hx.redact_window(hx.build_window(cur, None, {"repo": "R"}))
    for leak in ("colleague", "x@y.io", "dana@example.com", "/home/dana",
                 "abcdef123456", "eyJabc", "mhk_ABCDEF"):
        assert leak not in win, leak
    assert "~/dev/thing" in win and "<email>" in win
    # …and the window is still a window: the correction itself survives.
    assert "USER'S NEW MESSAGE: check ~/dev/thing" in win
    assert hx.redact_window("") == ""
    print("PASS test_the_window_is_redacted_before_it_leaves_the_machine")


# ------------------------------------------------------------- row contract
_STATE = {"repo": "XTraceAI/MemHub-Backend", "session_id": "s1", "turn": 3,
          "hook_version": "0.53.0", "at": "2026-09-09T00:00:00Z"}


def _row(**over):
    """A row as the server hands it back — nulls for the unused engines."""
    base = {"title": "T",
            "statement": "When running X on staging, do Y first, because Z.",
            "engine": "matcher", "delivery": "agent_hook",
            "matcher": {"event": "bash", "command_rx": r"git worktree add\s+-b",
                        "command_not_rx": None, "path_rx": None,
                        "path_not_rx": None, "content_rx": None},
            "ordering": None, "anchors": None,
            "derivable": False, "rationale": "r"}
    base.update(over)
    return base


def _build(raw, state=None):
    return hx.build_row(raw, state=dict(state or _STATE), session="s1",
                        turn_n=3, reason="router:x", scope_repos=["R"])


def test_a_row_is_complete_or_it_does_not_exist():
    row, why = _build(_row())
    assert row and not why
    assert row["source"] == "session_draft"
    assert row["source_ref"] == "s1#3"
    assert row["delivery"] == "agent_hook"
    # the server's nulls do not survive into the filed matcher
    assert row["matcher"] == {"event": "bash", "command_rx": r"git worktree add\s+-b"}
    # Nothing in a drafted row may imply it is live: mode and status are the
    # server's, and activation is a human act.
    assert "mode" not in row and "status" not in row

    for bad, expected in (
        (_row(draft=False, refusal_reason="project_state"), "project_state"),
        (_row(derivable=True), "derivable_from_repo"),
        (_row(statement="too short"), "statement_empty"),
        (_row(engine="none", matcher=None), "no_engine"),
    ):
        row, why = _build(bad)
        assert row is None and why == expected, (why, expected)
    print("PASS test_a_row_is_complete_or_it_does_not_exist")


def test_an_unusable_engine_is_a_refusal_not_a_partial_row():
    """The row refuses to exist without a trigger an engine can match."""
    # a regex that does not compile
    row, why = _build(_row(matcher={"event": "bash", "command_rx": "git ("}))
    assert row is None and why == "matcher_command_rx_unusable"
    # an edit matcher with no path
    row, why = _build(_row(matcher={"event": "edit"}))
    assert row is None and why == "matcher_path_rx_unusable"
    # an anchor that is a topic phrase, not an identifier — it would recall
    # everywhere, which is the failure mode anchors are prone to
    row, why = _build(_row(engine="anchors", matcher=None,
                           anchors=["the staging database", "ab"]))
    assert row is None and why == "anchors_not_identifiers"
    row, why = _build(_row(engine="anchors", matcher=None,
                           anchors=[".env.staging", "SUPABASE_DATABASE_URL"]))
    assert row and row["delivery"] == "anchor_recall"
    assert row["anchors"] == [".env.staging", "SUPABASE_DATABASE_URL"]
    # an optional regex that does not compile is dropped, not fatal
    row, why = _build(_row(matcher={"event": "bash", "command_rx": "git push",
                                    "command_not_rx": "(("}))
    assert row and "command_not_rx" not in row["matcher"]

    # An ordering armed by an event no lane emits is filed, reviewed,
    # activated — and never fires. `armed_by_events: ["bash"]` looks
    # reasonable and killed an otherwise-correct row on the S0 corpus.
    ordering = {"required_command_rx": "gh pr view",
                "gated_command_rx": "gh pr merge",
                "armed_by_events": ["bash"], "display_name": "d"}
    row, why = _build(_row(engine="ordering", matcher=None, ordering=ordering))
    assert row is None and why == "ordering_armed_by_unknown"
    ordering["armed_by_events"] = ["bash", "prompt"]
    row, why = _build(_row(engine="ordering", matcher=None, ordering=ordering))
    assert row and row["ordering"]["armed_by_events"] == ["prompt"]
    print("PASS test_an_unusable_engine_is_a_refusal_not_a_partial_row")


def test_a_stampless_draft_is_refused_client_side():
    """The server refuses one with `state_required` (§5.1). Sending it anyway
    burns a round trip to be told what we already know."""
    for key in ("repo", "session_id", "turn", "hook_version", "at"):
        state = dict(_STATE)
        state[key] = ""
        row, why = _build(_row(), state)
        assert row is None and why == f"state_missing_{key}", (key, why)
    print("PASS test_a_stampless_draft_is_refused_client_side")


def test_the_stamp_has_its_nine_fields():
    state = hx.stamp_state(session="s", turn={"n": 4, "tools": [], "results": []},
                           row_engine_target=("", ""), cwd="",
                           hook_version="0.53.0", env_name="staging",
                           default_repo="R", pr_number=12)
    assert set(state) == {"repo", "branch", "head_sha", "pr_number", "env",
                          "hook_version", "session_id", "turn", "at"}
    assert state["pr_number"] == 12 and state["turn"] == 4
    print("PASS test_the_stamp_has_its_nine_fields")


def test_a_cross_repo_turn_carries_its_ambiguity():
    """§4.2 resolves the stamp per ACTION, and `engine_target` picks the action
    the authored engine matched — which on a cross-repo turn is routinely the
    wrong one. A lesson about the plugin repo, drafted from a turn whose first
    matching Bash call had `cd …/other-repo`, stamps the other repo, files into
    its rulebook, and scopes the lesson to a repo it does not apply to.

    The stamp cannot tell which repo the lesson is *about*. It can tell that
    the turn touched two, and say so.
    """
    here = str(ROOT)
    other = str(ROOT.parent)
    turn = {"n": 1, "user": "u", "asst": "a", "results": [], "tools": [
        {"tool": "Bash", "target": f"cd {here} && git status"},
        {"tool": "Bash", "target": f"cd {other} && ls"},
    ]}
    state = hx.stamp_state(session="s", turn=turn,
                           row_engine_target=("Bash", f"cd {here} && git status"),
                           cwd="", hook_version="0.53.0", env_name="staging")
    touched = state.get("touched_repos")
    # Either the machine resolves both directories to repos (then the
    # ambiguity must be carried) or it resolves at most one (then there is
    # none to carry) — never a confident single value hiding a second repo.
    resolved = {hx.resolve_repo("Bash", t["target"], "")[0] for t in turn["tools"]}
    resolved.discard("")
    if len(resolved) > 1:
        assert touched and len(touched) > 1, state
    assert state["session_id"] == "s" and state["turn"] == 1

    # A single-repo turn stays clean — no noise field for the reviewer.
    solo = {"n": 1, "user": "u", "asst": "a", "results": [],
            "tools": [{"tool": "Bash", "target": f"cd {here} && git status"}]}
    state = hx.stamp_state(session="s", turn=solo, row_engine_target=("", ""),
                           cwd="", hook_version="0.53.0", env_name="staging")
    assert "touched_repos" not in state
    print("PASS test_a_cross_repo_turn_carries_its_ambiguity")


def test_a_staging_replay_never_stamps_the_replaying_machines_repo():
    """A teammate's session has no local cwd. Resolving from `""` must not fall
    through to `os.path.abspath("")`, which is wherever the replay happens to
    be running — that would stamp every teammate's lesson with the reviewer's
    own repo."""
    assert hx.resolve_repo("", "", "") == ("", "")
    state = hx.stamp_state(
        session="s", turn={"n": 2, "tools": [], "results": []},
        row_engine_target=("", ""), cwd="", hook_version="0.53.0",
        env_name="staging", default_repo="XTraceAI/MemHub-Backend")
    assert state["repo"] == "XTraceAI/MemHub-Backend"
    assert state["branch"] == "" and state["head_sha"] == ""
    print("PASS test_a_staging_replay_never_stamps_the_replaying_machines_repo")


# ------------------------------------------------------------------- twins
def test_twins_are_dropped_within_a_run():
    a = {"statement": "When running git worktree add -b, check the branch "
                      "does not already exist, because local state persists.",
         "source_ref": "s#1", "title": "a",
         "matcher": {"event": "bash", "command_rx": "git worktree add"}}
    b = {"statement": "When running git worktree add -b, check the branch "
                      "does not already exist, because state persists locally.",
         "source_ref": "s#2", "title": "b",
         "matcher": {"event": "bash", "command_rx": "git worktree add"}}
    c = {"statement": "When editing the ECS task definition, redeploy the "
                      "service or nothing changes in the running container.",
         "source_ref": "s#3", "title": "c",
         "anchors": ["task-definition.json"]}
    assert hx.is_twin(b, [a]) is a
    assert hx.is_twin(c, [a, b]) is None
    # identical matcher regexes are twins even when the prose diverges
    d = {"statement": "Completely different words about unrelated subjects.",
         "source_ref": "s#4", "title": "d",
         "matcher": {"event": "bash", "command_rx": "git worktree add"}}
    assert hx.is_twin(d, [a]) is a
    print("PASS test_twins_are_dropped_within_a_run")


# ------------------------------------------------------------ server bounds
class _Reply:
    def __init__(self, data, status=200):
        self.data, self.status, self.etag = data, status, None


class _Http:
    """A stand-in for mcp_http: records the one POST and answers as told."""

    def __init__(self, answer=None, raise_exc=None):
        self.calls, self.answer, self.raise_exc = [], answer, raise_exc

    def rest(self, url, bearer, method="GET", body=None, headers=None, timeout=0):
        self.calls.append({"url": url, "bearer": bearer, "method": method,
                           "body": body, "timeout": timeout})
        if self.raise_exc:
            raise self.raise_exc
        return self.answer


def _with_api(http, bearer="mhk_x"):
    real = hx._api
    hx._api = (lambda: ("https://h", bearer, http)) if http is not None else (lambda: None)
    return real


def test_the_classifier_gets_one_bounded_post_and_never_raises():
    http = _Http(answer=_Reply({"signal": False, "reason": "classified", "kind": None}))
    real = _with_api(http)
    try:
        reply, dt = hx.server_classify("W", hint="wrong_target", repo="R", timeout=7)
    finally:
        hx._api = real
    assert reply["signal"] is False and reply["reason"] == "classified"
    assert len(http.calls) == 1, "one attempt, never a retry"
    call = http.calls[0]
    assert call["method"] == "POST" and call["url"].endswith(hx.CLASSIFY_PATH)
    assert hx.CLASSIFY_PATH == "/v1/team/rulebook/harness/classify"
    assert call["body"] == {"window": "W", "hint": "wrong_target", "repo": "R"}
    assert call["timeout"] == 7

    # a transport failure, a wrong-shaped reply — including the removed draft
    # contract — are all "no signal" with a reason the caller can count
    for http, expected in (
        (_Http(raise_exc=RuntimeError("POST failed (503)")), "transport_error"),
        (_Http(answer=_Reply({"ok": True})), "bad_reply"),
        (_Http(answer=_Reply({"drafted": True, "reason": "drafted", "row": {}})), "bad_reply"),
        (_Http(answer=_Reply("not an object")), "bad_reply"),
    ):
        real = _with_api(http)
        try:
            reply, _ = hx.server_classify("W")
        finally:
            hx._api = real
        assert reply["signal"] is False and reply["reason"] == expected, reply
        assert reply["reason"] in hx.CLIENT_REASONS
        assert len(http.calls) == 1
    real = _with_api(None)
    try:
        reply, _ = hx.server_classify("W")
    finally:
        hx._api = real
    assert reply == {"signal": False, "reason": "no_credential"}
    http = _Http(answer=_Reply({"signal": False, "reason": "classified"}))
    real = _with_api(http)
    try:
        hx.server_classify("x" * (hx.WINDOW_MAX_CHARS + 500))
    finally:
        hx._api = real
    assert len(http.calls[0]["body"]["window"]) == hx.WINDOW_MAX_CHARS
    print("PASS test_the_classifier_gets_one_bounded_post_and_never_raises")


def _args(td, *extra):
    return hx.build_parser().parse_args(
        ["--turns", str(Path(td) / "t.json"), "--quiet",
         "--out", str(Path(td) / "m.jsonl"), *extra])


def test_a_signal_becomes_a_stamped_moment_and_no_signal_writes_nothing():
    with tempfile.TemporaryDirectory() as td:
        doc = {"session": "sess", "repo": "R", "turns": [
            _turn(1, user="hello", asst="hi"),
            _turn(2, user="no, i mean staging", asst="ok, staging"),
            _turn(3, user="we already have one", asst="right"),
        ]}
        (Path(td) / "t.json").write_text(json.dumps(doc), encoding="utf-8")
        answers = iter([
            _Reply({"signal": False, "reason": "classified"}),
            _Reply({"signal": True, "reason": "classified", "kind": "correction",
                    "derivable": True}),
            _Reply({"signal": False, "reason": "judge_failed"}),
        ])
        http = _Http()
        http.rest = lambda *a, **k: (http.calls.append(k), next(answers))[1]
        real = _with_api(http)
        try:
            args = _args(td)
            stats = hx.run(hx.load_session(args), args)
        finally:
            hx._api = real
        assert stats["server_calls"] == 3 and stats["moments"] == 1
        assert stats["reasons"] == {"classified": 2, "judge_failed": 1}
        assert stats["transport_errors"] == 0
        assert stats["hinted_calls"] == 2 and stats["hinted_signals"] == 1
        assert http.calls[1]["body"]["hint"] == "wrong_target"
        assert "STATE:" in http.calls[1]["body"]["window"]
        moments = hx.read_drafts(Path(td) / "m.jsonl")
        assert len(moments) == 1
        m = moments[0]
        assert m["turn"] == 2 and m["source_ref"] == "sess#2"
        assert m["kind"] == "correction" and m["hint"] == "wrong_target"
        assert m["derivable"] is True
        assert m["state"]["repo"] == "R" and m["state"]["turn"] == 2
        assert "USER'S NEW MESSAGE: no, i mean staging" in m["window"]
    print("PASS test_a_signal_becomes_a_stamped_moment_and_no_signal_writes_nothing")


def test_an_outage_is_counted_apart_from_a_quiet_moment():
    with tempfile.TemporaryDirectory() as td:
        doc = {"session": "sess", "repo": "R",
               "turns": [_turn(1, user="i mean staging", asst="ok")]}
        (Path(td) / "t.json").write_text(json.dumps(doc), encoding="utf-8")
        http = _Http(raise_exc=OSError("connection refused"))
        real = _with_api(http)
        try:
            args = _args(td)
            stats = hx.run(hx.load_session(args), args)
        finally:
            hx._api = real
        assert stats["transport_errors"] == 1 and stats["moments"] == 0
        assert stats["reasons"] == {"transport_error": 1}
        assert not (Path(td) / "m.jsonl").exists()
    print("PASS test_an_outage_is_counted_apart_from_a_quiet_moment")


def test_there_is_no_author_on_either_side():
    """The larger model was removed from the server (MemHub-Backend, the
    classify endpoint). The client must not call the old route or expect a
    row from it: the agent that lived the turn is the author."""
    src = (PLUGIN / "scripts" / "harness_extract.py").read_text(encoding="utf-8")
    for token in ("server_draft", "harness/draft", "DRAFT_PATH", "reply[\"row\"]"):
        assert token not in src, token
    print("PASS test_there_is_no_author_on_either_side")


# ------------------------------------------------------------------ drafts
def test_drafts_are_appended_to_their_own_file_never_the_book():
    with tempfile.TemporaryDirectory() as td:
        # The cached book is rewritten wholesale by fetch_book on every 200,
        # so a draft written there is a draft deleted on the next fetch.
        path = hx.drafts_path("sess-1", "")
        assert "drafts" in str(path) and "book" not in str(path)

        target = Path(td) / "nested" / "s.jsonl"
        hx.append_draft(target, {"title": "one"})
        hx.append_draft(target, {"title": "two"})
        target.open("a").write("{broken\n")
        assert [r["title"] for r in hx.read_drafts(target)] == ["one", "two"]
        assert hx.read_drafts(Path(td) / "missing.jsonl") == []

        os.environ["MEMHUB_HARNESS_DRAFTS"] = td
        try:
            assert hx.drafts_path("abc") == Path(td) / "abc.jsonl"
            # a session id is a filename component and nothing else
            assert hx.drafts_path("../x/../../etc") == Path(td) / ".._x_.._.._etc.jsonl"
        finally:
            del os.environ["MEMHUB_HARNESS_DRAFTS"]
    print("PASS test_drafts_are_appended_to_their_own_file_never_the_book")


def test_a_broken_session_file_fails_open():
    """§6: every path fails open and silent. A hook that raises is a hook that
    breaks the user's session."""
    with tempfile.TemporaryDirectory() as td:
        missing = Path(td) / "nope.jsonl"
        assert hx.main(["--transcript", str(missing)]) == 0
        bad = Path(td) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        assert hx.main(["--turns", str(bad)]) == 0
    print("PASS test_a_broken_session_file_fails_open")


def test_router_only_mode_spends_nothing():
    """--no-model is what measures router precision; if it could reach the
    server the measurement would cost money and the number would be
    unreproducible."""
    with tempfile.TemporaryDirectory() as td:
        doc = {"session": "s", "repo": "R", "turns": [
            _turn(1, user="don't we already have that?", asst="ok")]}
        (Path(td) / "t.json").write_text(json.dumps(doc), encoding="utf-8")
        real = hx._api

        def forbidden():
            raise AssertionError("--no-model must not reach the server")

        hx._api = forbidden
        try:
            args = hx.build_parser().parse_args(
                ["--turns", str(Path(td) / "t.json"), "--no-model", "--quiet",
                 "--out", str(Path(td) / "d.jsonl")])
            stats = hx.run(hx.load_session(args), args)
        finally:
            hx._api = real
        assert stats["moments"] == 0 and stats["server_calls"] == 0
        assert stats["router_hit_turns"] == 1
    print("PASS test_router_only_mode_spends_nothing")


def test_spawn_returns_without_running_the_pipeline():
    """The caller is a hook with a millisecond budget: it must return before
    the first network call and the child must outlive the session."""
    seen = {}
    real = hx.subprocess.Popen

    def fake(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)

    hx.subprocess.Popen = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            os.environ["MEMHUB_HARNESS_LOG_DIR"] = td
            try:
                assert hx.main(["--transcript", "/nope.jsonl", "--spawn"]) == 0
            finally:
                del os.environ["MEMHUB_HARNESS_LOG_DIR"]
    finally:
        hx.subprocess.Popen = real
    assert "--spawn" not in seen["args"], "the child must not re-spawn forever"
    assert "--transcript" in seen["args"]
    assert seen["kwargs"]["env"]["MEMHUB_HARNESS_CHILD"] == "1"
    assert "CLAUDECODE" not in seen["kwargs"]["env"]
    if hasattr(os, "setsid"):
        assert seen["kwargs"]["start_new_session"] is True
    print("PASS test_spawn_returns_without_running_the_pipeline")


def test_the_flag_is_off_by_default():
    assert not hx.extract_enabled({})
    assert not hx.extract_enabled({"MEMHUB_HARNESS_EXTRACT": "0"})
    assert not hx.extract_enabled({"MEMHUB_HARNESS_EXTRACT": "off"})
    for on in ("1", "on", "true", "YES"):
        assert hx.extract_enabled({"MEMHUB_HARNESS_EXTRACT": on}), on
    print("PASS test_the_flag_is_off_by_default")


def test_no_model_call_survives_in_the_client():
    """The judge and the author run on the server (MemHub #1249). Nothing in
    the client half may spawn `claude` or name a model: a stale copy of S0's
    CLI path would spend money twice and answer from the wrong prompt."""
    src = (PLUGIN / "scripts" / "harness_extract.py").read_text(encoding="utf-8")
    for token in ('"claude", "-p"', "--json-schema", "AUTHOR_MODEL", "JUDGE_MODEL",
                  "harness_judge.txt", "harness_author.txt"):
        assert token not in src, token
    assert not (PLUGIN / "scripts" / "prompts" / "harness_judge.txt").exists()
    print("PASS test_no_model_call_survives_in_the_client")


def test_cli_smoke_runs_without_a_model():
    with tempfile.TemporaryDirectory() as td:
        doc = {"session": "smoke", "repo": "R",
               "turns": [_turn(1, user="hello", asst="hi")]}
        src = Path(td) / "t.json"
        src.write_text(json.dumps(doc), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(PLUGIN / "scripts" / "harness_extract.py"),
             "--turns", str(src), "--no-model", "--quiet",
             "--out", str(Path(td) / "d.jsonl"),
             "--stats", str(Path(td) / "s.json")],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert json.loads((Path(td) / "s.json").read_text())["moments"] == 0
    print("PASS test_cli_smoke_runs_without_a_model")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
