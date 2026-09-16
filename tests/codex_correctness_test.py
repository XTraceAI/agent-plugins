#!/usr/bin/env python3
"""Codex lane correctness: silent capture death, guardian threads, fire ids.

Three defects, one suite (stdlib only, no network, tmpdir-isolated):

* ENG-1070 #1 — a Codex install whose plugin files are missing used to exit 0
  in silence on every hook event, and the installer's own `status` printed OK
  while capture was dead.
* ENG-1070 #2 — Codex's internal guardian/subagent review threads were
  captured as if they were the person's sessions (103 of 298 in staging).
* ENG-1075 — a rule fire reported the bare session uuid while capture uploads
  the same session as `codex-<uuid>`, so the two could never be joined.

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


@contextlib.contextmanager
def _bridge_state(tmp: Path):
    """Point the bridge's breadcrumb at a tmpdir — never the real state dir."""
    with patch.object(bridge, "_STATE_DIR", tmp), \
            patch.object(bridge, "_BRIDGE_STATE", tmp / "_bridge.json"):
        yield tmp / "_bridge.json"


# --------------------------------------------------------------------------
# ENG-1070 #1 — a broken install must not fail silently
# --------------------------------------------------------------------------

def test_unresolved_root_records_a_breadcrumb_capture_health_can_read():
    with tempfile.TemporaryDirectory() as td:
        with _bridge_state(Path(td)) as crumb:
            bridge._record_unresolved()
            state = json.loads(crumb.read_text(encoding="utf-8"))
    # Exactly the shape capture_health._recent_failure() looks for.
    assert state["last_error"] == "plugin_root_unresolved", state
    assert isinstance(state["last_error_at"], (int, float)), state


def test_unresolved_root_says_so_once_on_session_start():
    with tempfile.TemporaryDirectory() as td:
        with _bridge_state(Path(td)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                bridge._report_unresolved("dispatch", "SessionStart")
    doc = json.loads(out.getvalue())
    assert doc["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "capture" in doc["systemMessage"].lower()
    assert "memhub:setup" in doc["systemMessage"]


def test_unresolved_root_stays_quiet_on_every_other_event():
    # A per-tool-call complaint would be noise in the model's context; the
    # breadcrumb still lands so health can report it later.
    for action, event in (("dispatch", "PreToolUse"), ("dispatch", "PostToolUse"),
                          ("dispatch", "Stop"), ("flush", "Stop"), ("", "")):
        with tempfile.TemporaryDirectory() as td:
            with _bridge_state(Path(td)) as crumb:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    bridge._report_unresolved(action, event)
                assert crumb.exists(), (action, event)
        assert out.getvalue() == "", (action, event)


def test_a_resolved_root_retracts_a_stale_breadcrumb():
    # A last_error outliving the failure it recorded is its own bug.
    with tempfile.TemporaryDirectory() as td:
        with _bridge_state(Path(td)) as crumb:
            bridge._record_unresolved()
            assert crumb.exists()
            bridge._clear_unresolved()
            assert not crumb.exists()
            bridge._clear_unresolved()          # idempotent, never raises


def test_main_exits_zero_and_reports_when_no_plugin_root():
    with tempfile.TemporaryDirectory() as td:
        with _bridge_state(Path(td)) as crumb, \
                patch.object(bridge, "resolve_plugin_root", return_value=None), \
                patch.object(sys, "argv", ["b", "dispatch", "SessionStart"]), \
                patch.object(sys, "stdin", types_stdin(b"{}")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = bridge.main()
        assert rc == 0                      # a memory hook never blocks the host
        assert crumb.exists()
    assert json.loads(out.getvalue())["systemMessage"]


def types_stdin(payload: bytes):
    """Minimal stdin stand-in: main() only reads sys.stdin.buffer."""
    return type("S", (), {"buffer": io.BytesIO(payload)})()


def test_status_refuses_to_say_ok_while_capture_is_dead():
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        setup.install(home)
        # No plugin cache under this home, and the real _plugin_root runs:
        # patching it out was how the env-leak below went unnoticed.
        hooks_ok, actual, expected, root = setup.status(home)
        # The hooks really are wired — that is exactly the trap: the old
        # status() returned True here and the user believed capture worked.
        assert hooks_ok and actual == expected and root is None
        with patch.object(sys, "argv", ["s", "status", "--codex-home", str(home)]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = setup.main()
    # Wired handlers alone are no longer a passing grade. (What the headline
    # itself says is test_a_wired_bridge_is_still_reported_as_wired's job.)
    assert rc == 1, out.getvalue()
    assert "plugin: NOT FOUND" in out.getvalue()


def test_status_asks_with_the_hooks_environment_not_the_callers():
    # The setup skill always runs with PLUGIN_ROOT / CLAUDE_PLUGIN_ROOT set
    # (skills/setup/SKILL.md), while the user-level bridge is invoked with
    # neither. Resolving under OUR env answered "plugin found" for an install
    # where every real hook event finds nothing — the check defeated itself.
    import os
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        setup.install(home)
        for var in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "MEMHUB_PLUGIN_ROOT"):
            with patch.dict(os.environ, {var: str(SCRIPTS.parent)}):
                assert setup.status(home)[3] is None, var
                # …and the caller's environment is restored afterwards.
                assert os.environ[var] == str(SCRIPTS.parent)


def test_status_judges_the_home_it_was_asked_about():
    # `status --codex-home X` must judge X, not whatever ~/.codex holds.
    import os
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        setup.install(home)
        with patch.dict(os.environ, {"CODEX_HOME": str(Path.home() / ".codex")}):
            assert setup.status(home)[3] is None
            assert os.environ["CODEX_HOME"] == str(Path.home() / ".codex")


def test_a_wired_bridge_is_still_reported_as_wired():
    # The headline describes the HOOKS. Saying "NOT INSTALLED (4/4 handlers)"
    # invites the setup skill to reinstall and re-trust correct handlers.
    with tempfile.TemporaryDirectory() as td:
        home = Path(td)
        setup.install(home)
        with patch.object(sys, "argv", ["s", "status", "--codex-home", str(home)]):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = setup.main()
    text = out.getvalue()
    assert "MemHub Codex hooks: OK" in text, text      # hooks really are wired
    assert "plugin: NOT FOUND" in text, text           # …but capture is dead
    assert rc == 1, text                               # so the exit code fails


# --------------------------------------------------------------------------
# ENG-1070 #2 — Codex's own threads are not the person's sessions
# --------------------------------------------------------------------------

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
    for source in ("subagent", "guardian", "collab", "automation"):
        assert not codex_reader.is_own_thread(_rollout(thread_source=source)), source


def test_an_unknown_thread_source_is_excluded_by_the_allowlist():
    # The point of an allowlist: a value Codex has not shipped yet is held
    # out by default rather than captured because it wasn't on a denylist.
    assert not codex_reader.is_own_thread(_rollout(thread_source="something-new"))
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


def test_an_unrecognised_thread_kind_is_reported_not_skipped_in_silence():
    # The allowlist's one real risk: if Codex renames the marker for ordinary
    # sessions, EVERY session falls outside it. A quiet skip would rebuild the
    # silent-capture-death bug on a new axis, so this one reaches health.
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "rollout-n.jsonl"
        path.write_text(json.dumps(_rollout(thread_source="brand-new-kind")[0]) + "\n",
                        encoding="utf-8")
        saved = {}
        with patch.object(codex_flush, "STATE_DIR", Path(td)), \
                patch.object(codex_flush, "_save_state",
                             lambda sid, **f: saved.update(f)):
            asyncio.run(codex_flush._flush("n", path, 10))
    assert saved["last_error"] == "unknown_thread_source", saved
    assert saved["last_error_at"] > 0, saved
    # capture_health must have real words for it, not the generic
    # "run /memhub:login --status" advice — this is not a credential problem.
    health = _load("capture_health")
    assert "unknown_thread_source" in health._REASONS
    assert "plugin_root_unresolved" in health._REASONS


def test_a_known_bot_thread_is_skipped_quietly_and_clears_a_stale_failure():
    # Routine. And a session that failed an upload before being reclassified
    # must not keep warning forever about a retry that will never come.
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "rollout-g2.jsonl"
        path.write_text(json.dumps(_rollout(thread_source="guardian")[0]) + "\n",
                        encoding="utf-8")
        saved = {}
        with patch.object(codex_flush, "STATE_DIR", Path(td)), \
                patch.object(codex_flush, "_save_state",
                             lambda sid, **f: saved.update(f)):
            asyncio.run(codex_flush._flush("g2", path, 10))
    assert saved["last_error"] is None and saved["fail_streak"] == 0, saved


def test_the_report_only_set_never_widens_the_gate():
    # Naming a kind silences an alarm; it must never admit a thread.
    for source in codex_reader.KNOWN_BOT_THREAD_SOURCES:
        assert not codex_reader.is_own_thread(_rollout(thread_source=source)), source


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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
