#!/usr/bin/env python3
"""Tests for the staging-session adapter (harness-tied-memory-spec §7, S0).

No test here opens a database. Everything below the connection is pure:
message rows in, the replay window out — which is the point, because the rows
are the only part of a teammate's session we ever get to see.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "plugins" / "memhub" / "skills" / "rules-from-sessions"
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import staging_sessions as ss  # noqa: E402


def _user(ordinal, content):
    return {"ordinal": ordinal, "role": "user", "content": content,
            "parts": None, "tool_calls": None, "event_date": None}


def _assistant(ordinal, content="", parts=None, tool_calls=None):
    return {"ordinal": ordinal, "role": "assistant", "content": content,
            "parts": parts, "tool_calls": tool_calls, "event_date": None}


def _tool_part(name, tool_input, stdout="", error=False):
    part = {"type": f"tool-{name}", "input": tool_input,
            "state": "output-error" if error else "output-available",
            "output": {"stdout": "" if error else stdout, "stderr": ""}}
    if error:
        part["errorText"] = stdout
    return part


def test_rows_become_the_same_turn_shape_the_local_reader_produces():
    """The staging path and the local path must feed ONE window builder.
    Two shapes would mean two definitions of a turn, and the scorecard would
    be comparing a teammate's sessions against different arithmetic."""
    rows = [
        _user(0, "add the retry"),
        _assistant(1, parts=[{"type": "step-start"}]),
        _assistant(2, parts=[{"type": "text", "text": "on it"},
                             _tool_part("Bash", {"command": "uv run pytest"},
                                        "2 passed")]),
        _assistant(3, parts=[_tool_part("Edit", {"file_path": "a/b.py"},
                                        "ok")]),
        _user(4, "no, on staging"),
        _assistant(5, parts=[{"type": "text", "text": "switching"}]),
    ]
    turns = ss.rows_to_turns(rows)
    assert [t["n"] for t in turns] == [1, 2]
    assert turns[0]["user"] == "add the retry"
    assert [t["tool"] for t in turns[0]["tools"]] == ["Bash", "Edit"]
    assert turns[0]["tools"][0]["target"] == "uv run pytest"
    assert turns[0]["tools"][1]["target"] == "a/b.py"
    assert turns[0]["asst"] == "on it"
    assert turns[1]["user"] == "no, on staging"
    assert turns[1]["asst"] == "switching"

    # the same fields the extractor's own reader emits, so `--turns` and
    # `--transcript` are interchangeable
    import harness_extract as hx
    for key in ("n", "user", "tools", "results", "asst", "ts", "cwd"):
        assert key in turns[0], key
    assert hx.build_window(turns[1], turns[0], {}).count("USER'S NEW MESSAGE") == 1
    print("PASS test_rows_become_the_same_turn_shape_the_local_reader_produces")


def test_tool_errors_survive_the_round_trip():
    """The error arc is half the signal (§4.1); an error that arrives as a
    plain result makes every arc look like it never happened."""
    rows = [
        _user(0, "run it"),
        _assistant(1, parts=[
            _tool_part("Bash", {"command": "pytest x"},
                       "ModuleNotFoundError: No module named 'y'", error=True),
            _tool_part("Bash", {"command": "pytest x"}, "1 passed")]),
    ]
    turns = ss.rows_to_turns(rows)
    results = turns[0]["results"]
    assert [r["error"] for r in results] == [True, False]
    assert "ModuleNotFoundError" in results[0]["text"]

    import harness_extract as hx
    arcs = hx.error_arcs(turns[0])
    assert len(arcs) == 1 and arcs[0]["target"] == "pytest x"
    assert "error_arc" in dict(hx.route(turns[0], None))
    print("PASS test_tool_errors_survive_the_round_trip")


def test_output_shapes_all_read_and_an_elided_result_reads_empty():
    assert "hello" in ss._output_text({"output": {"stdout": "hello"}})
    assert "oops" in ss._output_text({"output": {"stderr": "oops"}})
    assert "plain" in ss._output_text({"output": "plain"})
    assert "body" in ss._output_text({"output": {"text": "body"}})
    assert "boom" in ss._output_text({"output": {}, "errorText": "boom"})
    # An oversized tool result is elided server-side. Reading empty is the
    # honest answer — the window then shows the call without its body, rather
    # than inventing one.
    assert ss._output_text({"output": None}) == ""
    assert ss._output_text({}) == ""
    print("PASS test_output_shapes_all_read_and_an_elided_result_reads_empty")


def test_legacy_tool_calls_rows_still_replay():
    """`parts` supersedes `tool_calls`, but rows written before it exists are
    most of the older corpus — dropping them would silently shrink the sample
    to whoever upgraded first."""
    rows = [
        _user(0, "go"),
        _assistant(1, tool_calls=[
            {"type": "text", "text": "sure"},
            {"type": "tool_call", "tool_name": "Bash",
             "arguments": json.dumps({"command": "ls"}),
             "result": "a\nb", "is_error": False},
            {"type": "tool_call", "tool_name": "Read",
             "arguments": {"file_path": "x.py"},
             "result": "Error: not found", "is_error": True}]),
    ]
    turns = ss.rows_to_turns(rows)
    assert [t["tool"] for t in turns[0]["tools"]] == ["Bash", "Read"]
    assert turns[0]["tools"][0]["target"] == "ls"
    assert turns[0]["results"][1]["error"] is True
    assert turns[0]["asst"] == "sure"
    # a stringified arguments blob that is not JSON must not lose the call
    rows[1]["tool_calls"][1]["arguments"] = "{broken"
    assert ss.rows_to_turns(rows)[0]["tools"][0]["tool"] == "Bash"
    print("PASS test_legacy_tool_calls_rows_still_replay")


def test_harness_text_is_never_a_human_turn():
    rows = [
        _user(0, "real question"),
        _assistant(1, content="answer"),
        _user(2, "Skill /loop is already loaded above; instructions unchanged."),
        _user(3, "<task-notification><task-id>x</task-id></task-notification>"),
        _user(4, "This session is being continued from a previous conversation "
                 "that ran out of context. Summary: the user said i mean "
                 "staging and we already have a filter."),
        _user(5, "second real question"),
    ]
    turns = ss.rows_to_turns(rows)
    assert [t["user"] for t in turns] == ["real question", "second real question"]
    print("PASS test_harness_text_is_never_a_human_turn")


def test_injected_turns_are_dropped_by_cross_session_identity():
    """A person does not type the same 300 characters into two sessions; the
    harness does. A skill preamble injected in the user role reads exactly
    like a standing instruction and tripped `standing_rule_request` in three
    sessions at once."""
    body = ("Approach this as the design lead at a small studio. " * 8)
    docs = [
        {"session": "s1", "turns": [{"n": 1, "user": body},
                                    {"n": 2, "user": "make it blue"}]},
        {"session": "s2", "turns": [{"n": 1, "user": body},
                                    {"n": 2, "user": "ship it"}]},
    ]
    index = [{"session": "s1", "file": "s1.json", "turns": 2,
              "correction_turns": 0},
             {"session": "s2", "file": "s2.json", "turns": 2,
              "correction_turns": 0}]
    with tempfile.TemporaryDirectory() as td:
        dropped = ss.drop_injected_turns(docs, Path(td), index)
        assert dropped == 2
        assert [t["user"] for t in docs[0]["turns"]] == ["make it blue"]
        # turn numbers are renumbered, or the window's "previous turn" is wrong
        assert docs[0]["turns"][0]["n"] == 1
        assert index[0]["turns"] == 1
        written = json.loads((Path(td) / "s1.json").read_text())
        assert len(written["turns"]) == 1

    # A short repeated message is a human being terse, not an injection.
    docs = [{"session": "a", "turns": [{"n": 1, "user": "continue"}]},
            {"session": "b", "turns": [{"n": 1, "user": "continue"}]}]
    with tempfile.TemporaryDirectory() as td:
        assert ss.drop_injected_turns(docs, Path(td), []) == 0
    print("PASS test_injected_turns_are_dropped_by_cross_session_identity")


def test_engineer_labels_are_stable_and_not_identifying():
    """The scorecard has to say "5 engineers" in a PR body without naming
    teammates or pasting their user ids."""
    a = ss.engineer_label("b943cddc-ee2f-44fe-9c73-c2fa54a21a8f")
    assert a == ss.engineer_label("b943cddc-ee2f-44fe-9c73-c2fa54a21a8f")
    assert a != ss.engineer_label("a0127dea-697e-4ea8-8f71-f409be014b49")
    assert a.startswith("eng-") and len(a) == 10
    assert "b943cddc" not in a
    print("PASS test_engineer_labels_are_stable_and_not_identifying")


def test_the_adapter_only_reads():
    """Read-only is a property of the file, not a promise in a docstring: no
    statement in it may write, and no MemHub write tool may appear."""
    source = (SKILL / "scripts" / "staging_sessions.py").read_text(encoding="utf-8")
    lowered = source.lower()
    for forbidden in ("insert into", "update ", "delete from", "drop table",
                      "alter table", "truncate", "create_rule", "save_artifact",
                      "add_memory", "add_directive"):
        assert forbidden not in lowered, forbidden
    assert "readonly=True" in source or "readonly=true" in lowered
    assert "default_transaction_read_only=on" in source
    # …and the DSN must never reach stdout.
    assert "print(dsn" not in lowered and "print(d)" not in lowered
    print("PASS test_the_adapter_only_reads")


def test_correction_selection_is_documented_as_selection_only():
    """The candidate filter is not the router and must not be mistaken for a
    measured number — a session it misses simply is not in the sample."""
    assert ss.CORRECTION.search("no, i meant staging")
    assert ss.CORRECTION.search("we already have a filter for that")
    assert not ss.CORRECTION.search("please add a retry to the client")
    source = (SKILL / "scripts" / "staging_sessions.py").read_text(encoding="utf-8")
    assert "CANDIDATE SELECTION ONLY" in source
    print("PASS test_correction_selection_is_documented_as_selection_only")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
