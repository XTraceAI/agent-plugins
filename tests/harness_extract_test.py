#!/usr/bin/env python3
"""Tests for the harness extractor (harness-tied-memory-spec §4.2).

The contract these protect: a bounded, fail-open, detached extractor that
emits COMPLETE rows or none at all, never fires anything, and never writes
anywhere but its local drafts file.

No test here calls a model. The model boundary is `call_model`, and every test
that needs a verdict substitutes one — a test suite that spends money and needs
a network is a test suite people stop running.
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


def test_claim_moments_are_counted_but_never_authored():
    """Spec §1/§5.1: the claim-shaped lesson is a built-in Stop check, not a
    rules row. It was 84% of router hits on the S0 corpus, so authoring it
    would have bought a pile of refusals at Sonnet prices."""
    assert "claim_no_receipt" in hx.NOT_AUTHORED
    assert "retraction" not in hx.NOT_AUTHORED
    print("PASS test_claim_moments_are_counted_but_never_authored")


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
    assert "error_arc" in dict(hx.route(closed, None))
    print("PASS test_error_arc_needs_to_close")


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


# ------------------------------------------------------------- row contract
_STATE = {"repo": "XTraceAI/MemHub-Backend", "session_id": "s1", "turn": 3,
          "hook_version": "0.53.0", "at": "2026-09-09T00:00:00Z"}


def _row(**over):
    base = {"draft": True, "refusal_reason": "", "title": "T",
            "statement": "When running X on staging, do Y first, because Z.",
            "engine": "matcher",
            "matcher": {"event": "bash", "command_rx": r"git worktree add\s+-b"},
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
    row, why = _build(_row(engine="anchors",
                           anchors=["the staging database", "ab"]))
    assert row is None and why == "anchors_not_identifiers"
    row, why = _build(_row(engine="anchors",
                           anchors=[".env.staging", "SUPABASE_DATABASE_URL"]))
    assert row and row["delivery"] == "anchor_recall"
    assert row["anchors"] == [".env.staging", "SUPABASE_DATABASE_URL"]
    # an optional regex that does not compile is dropped, not fatal
    row, why = _build(_row(matcher={"event": "bash", "command_rx": "git push",
                                    "command_not_rx": "(("}))
    assert row and "command_not_rx" not in row["matcher"]
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


# ------------------------------------------------------------ model bounds
class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def test_a_bounded_call_gets_one_attempt_and_no_partial_row():
    calls = []
    real = hx.subprocess.run

    def fake(cmd, **kwargs):
        calls.append((cmd, kwargs))
        raise hx.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    hx.subprocess.run = fake
    try:
        try:
            hx.call_model("sys", "user", {}, "haiku", 30)
            raise AssertionError("timeout must raise ModelError")
        except hx.ModelError as exc:
            assert "timeout" in str(exc)
        assert len(calls) == 1, "one attempt, never a retry"
        assert calls[0][1]["timeout"] == 30

        # a non-zero exit, unparseable stdout, and prose instead of JSON all
        # produce no row rather than half of one
        for proc in (_Proc(returncode=1, stderr="boom"),
                     _Proc(stdout="not json"),
                     _Proc(stdout=json.dumps({"result": "I need more context"})),
                     _Proc(stdout=json.dumps({"is_error": True,
                                              "result": "rate limited"}))):
            hx.subprocess.run = lambda *a, **k: proc
            try:
                hx.call_model("s", "u", {}, "haiku", 5)
                raise AssertionError(f"should have raised for {proc.stdout!r}")
            except hx.ModelError:
                pass
    finally:
        hx.subprocess.run = real
    print("PASS test_a_bounded_call_gets_one_attempt_and_no_partial_row")


def test_children_are_disarmed_and_isolated():
    """Two independent mechanisms, because this bug already shipped once: a
    replay's own `claude -p` sessions were captured into the repo brain and
    showed up on the fleet board."""
    env = hx._child_env()
    assert env["MEMHUB_HARNESS_CHILD"] == "1"
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env

    seen = {}
    real = hx.subprocess.run
    hx.subprocess.run = lambda cmd, **kw: (
        seen.update(cmd=cmd, env=kw.get("env")),
        _Proc(stdout=json.dumps({"structured_output": {"ok": True}})))[1]
    try:
        got, _dt = hx.call_model("sys", "user", {}, "haiku", 5)
    finally:
        hx.subprocess.run = real
    assert got == {"ok": True}
    assert "--safe-mode" in seen["cmd"], "the child must not load this plugin"
    assert seen["env"]["MEMHUB_HARNESS_CHILD"] == "1"
    print("PASS test_children_are_disarmed_and_isolated")


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
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert [json.loads(l)["title"] for l in lines] == ["one", "two"]

        os.environ["MEMHUB_HARNESS_DRAFTS"] = td
        try:
            assert hx.drafts_path("abc") == Path(td) / "abc.jsonl"
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
    """--no-model is what measures router precision; if it could reach a model
    the measurement would cost money and the number would be unreproducible."""
    with tempfile.TemporaryDirectory() as td:
        doc = {"session": "s", "repo": "R", "turns": [
            _turn(1, user="don't we already have that?", asst="ok")]}
        src = Path(td) / "t.json"
        src.write_text(json.dumps(doc), encoding="utf-8")
        real = hx.subprocess.run

        def forbidden(*a, **k):
            raise AssertionError("--no-model must not call a model")

        hx.subprocess.run = forbidden
        try:
            args = hx.build_parser().parse_args(
                ["--turns", str(src), "--no-model", "--quiet",
                 "--out", str(Path(td) / "d.jsonl")])
            stats = hx.run(hx.load_session(args), args)
        finally:
            hx.subprocess.run = real
        assert stats["rows"] == 0
        assert stats["judge_calls"] == 0 and stats["author_calls"] == 0
        assert stats["router_hit_turns"] == 1
    print("PASS test_router_only_mode_spends_nothing")


def test_spawn_returns_without_running_the_pipeline():
    """The caller is a hook with a millisecond budget: it must return before
    the first model call and the child must outlive the session."""
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
    if hasattr(os, "setsid"):
        assert seen["kwargs"]["start_new_session"] is True
    print("PASS test_spawn_returns_without_running_the_pipeline")


def test_the_shipped_prompts_exist_and_name_the_contract():
    judge = hx.JUDGE_PROMPT.read_text(encoding="utf-8")
    author = hx.AUTHOR_PROMPT.read_text(encoding="utf-8")
    # The judge must not be tuned for precision — that is the whole design.
    assert "Recall matters more than precision" in judge
    for kind in ("standing_rule", "correction", "claim_challenge",
                 "error_arc", "tribal"):
        assert kind in judge, kind
    # The author must offer exactly the three engines a lesson can use (§2).
    for engine in ("matcher", "ordering", "anchors"):
        assert f'engine="{engine}"' in author, engine
    assert "procedure" not in author.lower().split("refuse")[0]
    # Both must force the structured-output tool: a prose answer is a lost row.
    assert "StructuredOutput" in judge and "StructuredOutput" in author
    print("PASS test_the_shipped_prompts_exist_and_name_the_contract")


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
        assert json.loads((Path(td) / "s.json").read_text())["rows"] == 0
    print("PASS test_cli_smoke_runs_without_a_model")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
