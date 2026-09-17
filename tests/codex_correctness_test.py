#!/usr/bin/env python3
"""Codex lane correctness: guardian threads, and fire ids that can be joined.

Two defects, both observed rather than inferred (stdlib only, no network,
tmpdir-isolated):

* ENG-1070 #2 — Codex's own threads (subagent, guardian review, memory
  consolidation) were captured as if they were the person's sessions: 103 of
  298 captured Codex conversations in staging, and zero on any other platform.
* ENG-1075 — capture uploads a Codex session as `codex-<uuid>` while a rule
  fire reported the bare `<uuid>`, so the two could never be joined.

Run: python3 codex_correctness_test.py
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import codex_flush  # noqa: E402
import rulebook_hook  # noqa: E402
from readers import codex as codex_reader  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


bridge = _load("codex_hook_bridge")
setup = _load("setup_codex_hooks")


def _rollout(**payload) -> list[dict]:
    return [{"type": "session_meta", "payload": {"id": "s1", **payload}}]


def test_a_thread_the_person_started_is_captured():
    assert codex_reader.is_own_thread(_rollout(thread_source="user"))


def test_a_rollout_predating_thread_source_is_still_captured():
    # cli < ~0.142 wrote no thread_source at all. Absent means the person's,
    # or this fix would silently stop capturing every older session.
    assert codex_reader.is_own_thread(_rollout())
    assert codex_reader.is_own_thread(_rollout(thread_source=""))
    assert codex_reader.thread_source_of(_rollout()) is None


def test_codex_talking_to_itself_is_not_captured():
    # The literals are upstream's: `enum ThreadSource` in
    # codex-rs/protocol/src/protocol.rs — User | Subagent | GuardianReview |
    # Feature(String) | MemoryConsolidation. Note `guardian_review`, not
    # `guardian`: an invented literal would match nothing.
    for source in ("subagent", "guardian_review", "memory_consolidation"):
        assert not codex_reader.is_own_thread(_rollout(thread_source=source)), source


def test_an_unfamiliar_feature_surface_is_captured_not_dropped():
    # `Feature(String)` is an OPEN variant — any string Codex has not named
    # parses into it, and a Feature thread is the PERSON's. Codex's own review
    # of this PR reported a user-started web thread with
    # thread_source="codex_web_code_review"; an allowlist would have dropped it,
    # and the watermark advance would have made that unrecoverable.
    for source in ("codex_web_code_review", "something-shipped-tomorrow"):
        assert codex_reader.is_own_thread(_rollout(thread_source=source)), source
    # Non-string junk must not crash the header read. Treating it as the
    # person's is a DELIBERATE hole in the allowlist: fail open, because
    # dropping real work costs more than importing one stray review.
    assert codex_reader.thread_source_of(_rollout(thread_source=17)) is None
    assert codex_reader.is_own_thread(_rollout(thread_source=17))


def test_thread_source_is_read_from_the_rollout_on_disk():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "rollout-x.jsonl"
        path.write_text(json.dumps(_rollout(thread_source="subagent")[0]) + "\n",
                        encoding="utf-8")
        assert codex_reader.thread_source_of_path(path) == "subagent"


def test_a_guardian_rollout_is_never_parsed_or_uploaded():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "rollout-g.jsonl"
        path.write_text(json.dumps(_rollout(thread_source="subagent")[0]) + "\n",
                        encoding="utf-8")
        saved = {}
        def _boom(*a, **k):                 # nothing past the gate may run
            raise AssertionError("guardian rollout reached the parse/upload path")
        with patch.object(codex_flush, "STATE_DIR", Path(td)), \
                patch.object(codex_flush.codex_reader, "to_canonical", _boom), \
                patch.object(codex_flush, "_save_state",
                             lambda sid, **f: saved.update(f)):
            asyncio.run(codex_flush._flush("g", path, 10))
        assert saved.get("skipped_thread_source") == "subagent", saved


def test_an_unreadable_header_still_captures():
    # Fail OPEN: a header we cannot read is not evidence of a bot thread, and
    # dropping real work is worse than importing one stray review.
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "missing.jsonl"
        reached = {}
        with patch.object(codex_flush, "STATE_DIR", Path(td)), \
                patch.object(codex_flush.codex_reader, "thread_source_of_path",
                             side_effect=OSError("nope")), \
                patch.object(codex_flush.codex_reader, "to_canonical",
                             side_effect=lambda *a, **k: reached.setdefault("yes", True) and ([], {})):
            with contextlib.suppress(TypeError, AttributeError, ValueError, OSError):
                asyncio.run(codex_flush._flush("u", path, 10))
        assert reached.get("yes"), "an unreadable header must not block capture"


def test_an_unfamiliar_kind_reaches_the_parse_instead_of_being_skipped():
    # Regression for the P1 Codex raised on this PR: an unfamiliar Feature
    # surface must be CAPTURED, not skipped. If it were skipped the watermark
    # would advance and the session could never be recovered.
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "rollout-n.jsonl"
        path.write_text(json.dumps(_rollout(thread_source="codex_web_code_review")[0]) + "\n",
                        encoding="utf-8")
        reached = {}
        def _mark(*a, **k):
            reached["yes"] = True
            raise RuntimeError("stop after the gate")
        with patch.object(codex_flush, "STATE_DIR", Path(td)), \
                patch.object(codex_flush.codex_reader, "to_canonical", _mark), \
                patch.object(codex_flush, "_save_state", lambda sid, **f: None):
            with contextlib.suppress(RuntimeError):
                asyncio.run(codex_flush._flush("n", path, 10))
    assert reached.get("yes"), "an unfamiliar Feature thread must still be captured"


def test_a_named_bot_thread_is_skipped_quietly_and_clears_a_stale_failure():
    # Routine. And a session that failed an upload before being reclassified
    # must not keep warning forever about a retry that will never come.
    # Note the literal: upstream spells it `guardian_review`.
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "rollout-g2.jsonl"
        path.write_text(json.dumps(_rollout(thread_source="guardian_review")[0]) + "\n",
                        encoding="utf-8")
        saved = {}
        with patch.object(codex_flush, "STATE_DIR", Path(td)), \
                patch.object(codex_flush, "_save_state",
                             lambda sid, **f: saved.update(f)):
            asyncio.run(codex_flush._flush("g2", path, 10))
    assert saved["last_error"] is None and saved["fail_streak"] == 0, saved


def test_only_the_named_bot_kinds_are_refused():
    # Everything outside the denylist is captured — that is the whole point of
    # denying by name against an open type.
    for source in codex_reader.BOT_THREAD_SOURCES:
        assert not codex_reader.is_own_thread(_rollout(thread_source=source)), source
    for source in codex_reader.KNOWN_OWN_THREAD_SOURCES:
        assert codex_reader.is_own_thread(_rollout(thread_source=source)), source


def test_discovery_does_not_offer_codex_threads_the_person_never_started():
    # A guardian review CONTAINS the conversation it reviewed, so it scores
    # like a strong authorship match on the very evidence ranking uses.
    with tempfile.TemporaryDirectory() as td:
        own = Path(td) / "rollout-own.jsonl"
        own.write_text(json.dumps(_rollout(thread_source="user")[0]) + "\n", encoding="utf-8")
        bot = Path(td) / "rollout-bot.jsonl"
        bot.write_text(json.dumps(_rollout(thread_source="subagent")[0]) + "\n", encoding="utf-8")
        assert codex_reader.is_own_thread_path(own)
        assert not codex_reader.is_own_thread_path(bot)


# --------------------------------------------------------------------------
# ENG-1075 — a fire must name the session the way capture named it
# --------------------------------------------------------------------------

def test_claude_fires_keep_the_bare_session_id():
    # flush_turn.py uploads `conversation_id: session_id`, unprefixed.
    assert rulebook_hook.wire_session_id("abc-123", "claude") == "abc-123"
    assert rulebook_hook.wire_session_id("abc-123", None) == "abc-123"


def test_codex_and_cursor_fires_carry_captures_namespace():
    assert rulebook_hook.wire_session_id("abc-123", "codex") == "codex-abc-123"
    assert rulebook_hook.wire_session_id("abc-123", "cursor") == "cursor-abc-123"


def test_namespacing_is_idempotent():
    # A payload that already arrives namespaced must not become codex-codex-.
    assert rulebook_hook.wire_session_id("codex-abc", "codex") == "codex-abc"
    assert rulebook_hook.wire_session_id("cursor-abc", "cursor") == "cursor-abc"


def test_namespacing_leaves_empty_and_non_string_ids_alone():
    assert rulebook_hook.wire_session_id("", "codex") == ""
    assert rulebook_hook.wire_session_id(None, "codex") is None


def test_wire_row_namespaces_from_the_row_and_never_ships_host():
    row = {"fire_id": "f1", "rule_id": "r1", "session_id": "abc", "host": "codex"}
    wire = rulebook_hook.wire_row(row)
    assert wire["session_id"] == "codex-abc"
    # `host` is local-only, like rulebook_id — the server defines neither.
    assert "host" not in wire
    assert "host" not in rulebook_hook.WIRE_KEYS


def test_a_ledger_row_without_a_host_is_unchanged():
    # Rows written before this change carry no host; they must ship as-is.
    wire = rulebook_hook.wire_row({"fire_id": "f1", "session_id": "abc"})
    assert wire["session_id"] == "abc"


def test_the_two_lanes_share_one_definition_of_a_conversation_id():
    # pr_link.conversation_id_for already owned this projection. A second copy
    # is how the PR-link lane and the fire lane come to disagree about what a
    # Codex session is called, so wire_session_id delegates to it — and
    # inherits the cases a fresh implementation forgets.
    import pr_link
    for host, sid in (("codex", "abc"), ("cursor", "abc"), ("claude", "abc"),
                      ("codex", "codex-abc"), ("codex", "  abc  "),
                      ("Codex", "abc"), (None, "abc")):
        assert rulebook_hook.wire_session_id(sid, host) == \
            (pr_link.conversation_id_for(host, sid) or sid), (host, sid)
    # The two the local copy got wrong before delegating:
    assert rulebook_hook.wire_session_id("  abc  ", "codex") == "codex-abc"
    assert rulebook_hook.wire_session_id("abc", "Codex") == "codex-abc"


def test_host_flag_parsing():
    assert rulebook_hook._host_arg(["pre", "--host", "codex"]) == "codex"
    assert rulebook_hook._host_arg(["pre", "--host=cursor"]) == "cursor"
    # Default and rejection of anything unrecognised — a wrong prefix links
    # nothing, which is worse than no prefix at all.
    assert rulebook_hook._host_arg(["pre"]) == "claude"
    assert rulebook_hook._host_arg(["pre", "--host", "bogus"]) == "claude"
    assert rulebook_hook._host_arg(["pre", "--host"]) == "claude"


def test_the_codex_bridge_tells_the_hook_which_host_it_is():
    with patch.object(bridge, "_run") as run:
        bridge._rulebook_result(Path("/plugin"), b"{}", "pre")
    args = run.call_args[0]
    # Adjacency, not membership: ("--host", "codex") must arrive as a pair.
    assert args[-2:] == ("--host", "codex"), args


def test_the_event_lane_is_namespaced_too():
    # main's #240 added a second fire path (events.jsonl -> /fire-events) that
    # the server folds into each fire's outcome BY SESSION. It carried the bare
    # id, exactly as the fire lane did — the same defect in a second place. An
    # event naming the session differently from the fire it answers folds into
    # nothing.
    row = {"event_id": "e1", "kind": "receipt", "rule_id": "r1",
           "session_id": "abc", "host": "codex"}
    wire = rulebook_hook.event_wire_row(row)
    assert wire["session_id"] == "codex-abc", wire
    assert "host" not in wire                      # local-only, as on a fire
    assert "host" not in rulebook_hook.EVENT_WIRE_KEYS


def test_both_lanes_name_a_session_identically():
    # The fold is by session id, so the two lanes disagreeing IS the bug.
    for host in ("claude", "codex", "cursor", None):
        fire = rulebook_hook.wire_row({"session_id": "s1", "host": host})
        event = rulebook_hook.event_wire_row({"session_id": "s1", "host": host})
        assert fire["session_id"] == event["session_id"], host


def test_remove_is_a_true_inverse_of_install():
    # Observed live: on a machine with no hooks.json, `remove` left a
    # {"hooks": {}} stub behind — a file the user never had.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        hooks = home / "hooks.json"
        assert not hooks.exists()
        setup.install(home)
        assert hooks.exists(), "install should create it"
        setup.uninstall(home)
        assert not hooks.exists(), "remove left a stub behind"
        assert not (home / "memhub_hook_bridge.py").exists()


def test_remove_keeps_a_hooks_file_holding_someone_elses_hooks():
    # The inverse must not become "delete the user's hooks.json".
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        hooks = home / "hooks.json"
        hooks.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "echo not-ours"}]}]}}),
            encoding="utf-8")
        setup.install(home)
        setup.uninstall(home)
        assert hooks.exists(), "removed a file holding someone else's hooks"
        assert "not-ours" in hooks.read_text(encoding="utf-8")


def test_a_stale_bridge_does_not_report_as_not_installed():
    # Observed live right after a plugin upgrade: "NOT INSTALLED (4/4
    # handlers)" while capture was demonstrably working. Only the copied runner
    # was stale, and that wording reads as "nothing is set up".
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        setup.install(home)
        (home / "memhub_hook_bridge.py").write_text("# stale\n", encoding="utf-8")
        healthy, actual, expected, hooks_ok, runner_ok = setup.status(home)
        assert not healthy and hooks_ok and not runner_ok and actual == expected
        with patch.object(sys, "argv", ["s", "status", "--codex-home", str(home)]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                setup.main()
    text = out.getvalue()
    assert "STALE BRIDGE" in text, text
    assert "NOT INSTALLED" not in text, text


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
