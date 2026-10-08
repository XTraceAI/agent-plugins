#!/usr/bin/env python3
"""Tests for the harness-tied memory sensor (`harness_stop.py`).

What these protect (harness-tied-memory-spec §4.0–4.3.1): with the flag off
nothing happens at all; the Stop of turn N+1 judges turn N — never the turn
that just stopped, never a turn twice — and on a signal continues the agent
ONCE with one short line naming the `handoff` command, which prints a
launch prompt naming that one moment, its stamp command and turn N+1; a Stop
that is a launch's continuation, a subagent's, or the end of a harness prompt
(a task notification, a loop wakeup) judges nothing; an interrupted turn and
its verbatim resend are judged once; a classifier failure blocks nothing; the
`stamp` command prints the stamp `create_rule` needs; nothing is written but
`stop.log` and a per-session read offset, which changes no turn and no window; nothing in the sensor sends `activate`. No test reaches a server:
`server_classify` is substituted.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import harness_extract as hx  # noqa: E402
import harness_stop as hs  # noqa: E402
import hook_entry  # noqa: E402


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


def _records(path: Path, turns):
    """Transcript records for (user, asst, tools) turns. `user` may also be a
    dict: a raw record to append as it is (a notification, an isMeta body)."""
    recs, n = [], 0
    for item in turns:
        if isinstance(item, dict):
            recs.append(item)
            continue
        user, asst, tools = item
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
    return recs


def _transcript(path: Path, turns):
    path.write_text("\n".join(json.dumps(r) for r in _records(path, turns)) + "\n",
                    encoding="utf-8")


def _moment(turn: int, **extra) -> dict:
    m = {"source_ref": f"sess#{turn}", "turn": turn, "kind": "correction",
         "derivable": False, "state": {"repo": "repo"},
         "transcript": "/t/sess.jsonl", "cwd": "/w"}
    m.update(extra)
    return m


@contextlib.contextmanager
def _classifier(reply):
    """Substitute the one server call; yields the windows it was sent."""
    sent = []
    real = hx.server_classify
    hx.server_classify = lambda window, repo="", timeout=0: (sent.append(window), (reply, 0.1))[1]
    try:
        yield sent
    finally:
        hx.server_classify = real


def _stop(env, tp: Path, *, other_sensor_ran: bool = False, **payload) -> dict | None:
    """Run cmd_stop in-process; the hook output it printed, or None.

    Each call is a fresh machine unless `other_sensor_ran`: the one-per-machine
    judgement claims (`judged/`) of earlier calls are forgotten, so a test that
    replays one Stop sees it judged again."""
    if not other_sensor_ran:
        shutil.rmtree(env.base / "harness" / "judged", ignore_errors=True)
    body = {"session_id": "sess", "transcript_path": str(tp), "cwd": str(env.base)}
    body.update(payload)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert hs.cmd_stop(body) == 0
    text = out.getvalue().strip()
    return json.loads(text) if text else None


SIGNAL = {"signal": True, "reason": "classified", "kind": "correction", "derivable": False}


# ---------------------------------------------------------------- the flag
def test_with_the_flag_off_nothing_happens():
    with _Env() as env:
        tp = env.base / "s.jsonl"
        _transcript(tp, [("i mean staging", "ok", []), ("thanks", "ok", [])])
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "harness_stop.py"), "stop"],
            input=json.dumps({"session_id": "s", "transcript_path": str(tp), "cwd": str(ROOT)}),
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, MEMHUB_HARNESS_EXTRACT="0"))
        assert proc.returncode == 0 and proc.stdout == "", proc.stdout
        assert not (env.base / "harness").exists()
    print("PASS test_with_the_flag_off_nothing_happens")


# ---------------------------------------------------------- one turn late
def test_the_stop_after_a_turn_judges_that_turn_and_blocks_once():
    with _Env() as env:
        tp = env.base / "sess.jsonl"
        _transcript(tp, [("deploy it", "deployed to prod", []),
                         ("no, i mean staging", "redeployed to staging", []),
                         ("thanks, now the tests", "tests pass", [])])
        with _classifier(SIGNAL) as sent:
            block = _stop(env, tp)
        assert len(sent) == 1, "one classifier call per Stop"
        assert "USER'S NEW MESSAGE: no, i mean staging" in sent[0], sent[0]
        assert "PREVIOUS USER MESSAGE: deploy it" in sent[0], "turn N-1 is the window's context"
        # turn N+1 is not judged yet: its message rides along only as what the
        # person said next, and its reply is not in the window at all
        assert sent[0].rstrip().endswith(
            "USER'S FOLLOWING MESSAGE (what the person said next): thanks, now the tests"), sent[0]
        assert sent[0].count("thanks, now the tests") == 1 and "tests pass" not in sent[0]
        # model-only context, never a block: a block's reason is printed to the person
        assert "decision" not in block and "reason" not in block, block
        out = block["hookSpecificOutput"]
        assert out["hookEventName"] == "Stop", out
        # Claude Code prints a Stop hook's context to the person in full, so it
        # is ONE line written for them and nothing else: no command, no
        # instruction, no prompt. What the agent does with it travels at
        # SessionStart, where it is not shown.
        line = out["additionalContext"]
        assert line == f"{hs.HANDOFF_PREFIX} 2, drafting it in the background.", line
        # the verdict the line leaves out is in the log, where `handoff` reads it
        log = (env.base / "harness" / "stop.log").read_text()
        assert "launch sess t2: fork requested (correction) derivable=0" in log, log
        # nothing is stored but the log, the session's read offset and the
        # judged turn's claim
        assert sorted(p.name for p in (env.base / "harness").iterdir()) == ["judged", "offsets", "stop.log"]
        assert [p.name for p in (env.base / "harness" / "offsets").iterdir()] == ["sess.json"]
        assert [p.name for p in (env.base / "harness" / "judged" / "sess").iterdir()] == ["2"]
    print("PASS test_the_stop_after_a_turn_judges_that_turn_and_blocks_once")


def test_no_signal_or_a_failed_call_blocks_nothing():
    with _Env() as env:
        tp = env.base / "sess.jsonl"
        _transcript(tp, [("a", "b", []), ("c", "d", [])])
        for reply in ({"signal": False, "reason": "classified"},
                      {"signal": False, "reason": "transport_error"}):
            with _classifier(reply) as sent:
                assert _stop(env, tp) is None
            assert len(sent) == 1
        log = (env.base / "harness" / "stop.log").read_text()
        assert "reason=transport_error" in log, "an outage is logged, never read as quiet"
        # the window's shape is logged with each verdict: size, results, next message
        assert " results=0 next=yes" in log and " window=" in log, log
    print("PASS test_no_signal_or_a_failed_call_blocks_nothing")


def test_stops_that_do_not_end_a_persons_turn_judge_nothing():
    with _Env() as env:
        tp = env.base / "sess.jsonl"
        two = [("fix it", "fixed", []), ("i mean the other file", "done", [])]
        with _classifier(SIGNAL) as sent:
            _transcript(tp, two[:1])
            assert _stop(env, tp) is None, "one turn has no turn before it"
            _transcript(tp, two)
            assert _stop(env, tp, stop_hook_active=True) is None, "the launch's continuation"
            assert _stop(env, tp, agent_id="a1") is None, "a subagent's Stop"
            # the fork's completion notice, then the agent's reply to it: a new
            # Stop whose prompt is the harness's, so turn 1 is NOT judged again
            _transcript(tp, two + [
                {"type": "user", "uuid": "n1", "message": {"content":
                    "<task-notification>MemHub harness fork: filed T</task-notification>"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "noted"}]}}])
            assert _stop(env, tp) is None
        assert sent == [], sent
    print("PASS test_stops_that_do_not_end_a_persons_turn_judge_nothing")


def test_an_interrupted_turn_and_its_resend_are_judged_once():
    with _Env() as env:
        tp = env.base / "sess.jsonl"
        turns = [("setup", "ok", []), ("stop patching, fix it properly", "", []),
                 ("stop patching, fix it properly", "fixed properly", [])]
        _transcript(tp, turns)
        with _classifier(SIGNAL) as sent:
            assert _stop(env, tp) is None, "turn 2 is resent as turn 3"
        assert sent == []
        _transcript(tp, turns + [("thanks", "ok", [])])
        with _classifier(SIGNAL) as sent:
            block = _stop(env, tp)
        assert block and f"{hs.HANDOFF_PREFIX} 3," in block["hookSpecificOutput"]["additionalContext"], block
    print("PASS test_an_interrupted_turn_and_its_resend_are_judged_once")


def test_a_skill_body_is_neither_a_turn_nor_a_prompt():
    """Claude Code records a skill's body as an isMeta user record after the
    Skill call. Read as the person's message it was 3% of all flags in the
    2026-09-29 replay; read as a prompt, it would stop the next Stop judging."""
    with _Env() as env:
        tp = env.base / "sess.jsonl"
        _transcript(tp, [("use the skill", "running", []),
                         {"type": "user", "isMeta": True, "uuid": "m1",
                          "message": {"content": "## Page contract \u2014 read before your first publish"}},
                         ("no, the other one", "ok", [])])
        turns, last = hx.read_transcript(tp)
        assert [t["user"] for t in turns] == ["use the skill", "no, the other one"], turns
        assert last == turns[-1]["uuid"]
        with _classifier(SIGNAL) as sent:
            assert _stop(env, tp) is not None
        assert "USER'S NEW MESSAGE: use the skill" in sent[0]
    print("PASS test_a_skill_body_is_neither_a_turn_nor_a_prompt")


def test_a_lesson_in_the_following_message_hands_over_that_turn_once():
    """The judge says the lesson is in the person's following message (`at`):
    the turn handed to the fork is N+1, the one this Stop ends, and the next
    Stop — judging N+1 as `new` — does not hand it over again (spec §10.8)."""
    with _Env() as env:
        tp = env.base / "sess.jsonl"
        turns = [("deploy it", "deployed to prod", []),
                 ("ok, do it", "done", []),
                 ("no, i mean staging", "redeployed to staging", [])]
        _transcript(tp, turns)
        with _classifier(dict(SIGNAL, at="following")) as sent:
            block = _stop(env, tp)
        assert "USER'S NEW MESSAGE: ok, do it" in sent[0], "turn 2 is the one judged"
        line = block["hookSpecificOutput"]["additionalContext"]
        assert line == f"{hs.HANDOFF_PREFIX} 3, drafting it in the background.", line
        log = (env.base / "harness" / "stop.log").read_text()
        assert "judge sess t2: signal=True" in log and "at=following" in log, log
        assert "launch sess t3: fork requested (correction) derivable=0" in log, log
        # the person goes on; the next Stop judges turn 3 and finds the same lesson
        _transcript(tp, turns + [("thanks", "ok", [])])
        with _classifier(dict(SIGNAL, at="new")) as sent:
            again = _stop(env, tp)
        assert "USER'S NEW MESSAGE: no, i mean staging" in sent[0], sent[0]
        assert again is None, again
        log = (env.base / "harness" / "stop.log").read_text()
        assert "launch sess t3: skipped, already handed over" in log, log
        assert log.count("fork requested") == 1, log
    print("PASS test_a_lesson_in_the_following_message_hands_over_that_turn_once")


def test_a_reply_without_at_reads_as_the_judged_turn():
    """A server that predates `at` answers without it: the judged turn is the
    moment, exactly as before. An unknown value reads the same way."""
    for reply in (SIGNAL, dict(SIGNAL, at="sideways")):
        with _Env() as env:
            tp = env.base / "sess.jsonl"
            _transcript(tp, [("deploy it", "deployed to prod", []),
                             ("no, i mean staging", "redeployed", []),
                             ("thanks", "ok", [])])
            with _classifier(reply):
                block = _stop(env, tp)
            assert block["hookSpecificOutput"]["additionalContext"].startswith(
                f"{hs.HANDOFF_PREFIX} 2,"), (reply, block)
    print("PASS test_a_reply_without_at_reads_as_the_judged_turn")


def test_a_fork_for_the_last_turn_is_not_told_of_a_next_message():
    """A turn handed over on `at=following` is the one the Stop just ended: the
    person has said nothing after it, and the fork's prompt says so instead of
    pointing at a message that does not exist."""
    with _Env() as env:
        proj = env.base / "claude" / "projects" / "-w"
        proj.mkdir(parents=True)
        tp = proj / "sess.jsonl"
        _transcript(tp, [("deploy it", "deployed to prod", []),
                         ("no, i mean staging", "redeployed to staging", [])])
        out = subprocess.run(
            [sys.executable, str(SCRIPTS / "harness_stop.py"), "handoff", "--moment", "sess#2",
             "--kind", "correction"], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, MEMHUB_HARNESS_EXTRACT="0", CLAUDE_CONFIG_DIR=str(env.base / "claude")))
        assert out.returncode == 0, out.stderr
        assert "has said nothing after turn 2 yet" in out.stdout, out.stdout
        assert "turn 3, is what happened" not in out.stdout, out.stdout
    print("PASS test_a_fork_for_the_last_turn_is_not_told_of_a_next_message")


def test_the_handoff_command_prints_the_fork_instruction():
    """The command the Stop's one line names. It finds the session's transcript
    under the host's config dir, rebuilds the moment, and prints the launch
    instruction; it is the agent's tool, so it runs with the flag off and is
    loud when it cannot answer."""
    with _Env() as env:
        proj = env.base / "claude" / "projects" / "-w"
        proj.mkdir(parents=True)
        tp = proj / "sess.jsonl"
        _transcript(tp, [("deploy it", "deployed to prod", []),
                         ("no, i mean staging", "redeployed to staging", []),
                         ("thanks", "ok", [])])
        run = lambda *args: subprocess.run(  # noqa: E731
            [sys.executable, str(SCRIPTS / "harness_stop.py"), "handoff", *args],
            capture_output=True, text=True, timeout=30,
            env=dict(os.environ, MEMHUB_HARNESS_EXTRACT="0", CLAUDE_CONFIG_DIR=str(env.base / "claude")))
        ok = run("--moment", "sess#2", "--kind", "correction")
        assert ok.returncode == 0, ok.stderr
        reason = ok.stdout
        assert reason.startswith(hs.BLOCK_PREFIX), reason
        assert f"{hs.FORK_MARK}, moment sess#2" in reason, reason
        assert "flagged as correction" in reason and "turn 3, is what happened after it" in reason, reason
        assert f"stamp --transcript \"{tp}\" --turn 2" in reason, reason
        assert 'says "none" or "failed", say nothing' in reason, "an unfiled rule is not news"
        assert "author" not in reason.lower(), "how to file lives in the skill"
        assert "may already be written down" in run("--moment", "sess#2", "--derivable").stdout
        # with no flags — how the session rule calls it — the label comes from
        # the `launch` line the Stop logged; with none, the prompt says "a signal"
        assert "flagged as a signal" in run("--moment", "sess#2").stdout
        hx.log_path("stop.log").parent.mkdir(parents=True, exist_ok=True)
        hx.log_path("stop.log").write_text(
            "2026-09-30 18:00:00 launch sess t2: fork requested (claim_challenge) derivable=1\n")
        logged = run("--moment", "sess#2").stdout
        assert "flagged as claim_challenge" in logged and "may already be written down" in logged, logged
        for args, why in ((("--moment", "sess#9"), "no turn 9"),
                          (("--moment", "nosuch#1"), "no transcript"),
                          (("--moment", "sess#2; rm -rf x"), "not a moment ref")):
            bad = run(*args)
            assert bad.returncode == 2 and why in bad.stderr and bad.stdout == "", (args, bad)
        # a kind that is not a plain label never reaches the prompt
        assert "`touch x`" not in run("--moment", "sess#2", "--kind", "`touch x`").stdout
    print("PASS test_the_handoff_command_prints_the_fork_instruction")


def test_session_start_carries_the_standing_rule():
    """The rule that keeps the agent from narrating a launch goes out at
    SessionStart, a channel the person is not shown; with the flag off, or for
    a subagent, nothing is said."""
    run = lambda flag, payload: subprocess.run(  # noqa: E731
        [sys.executable, str(SCRIPTS / "harness_stop.py"), "session"], input=json.dumps(payload),
        capture_output=True, text=True, timeout=30, env=dict(os.environ, MEMHUB_HARNESS_EXTRACT=flag))
    for source in ("startup", "clear", "resume", "compact"):
        on = run("1", {"session_id": "s", "source": source})
        assert on.returncode == 0, on.stderr
        out = json.loads(on.stdout)["hookSpecificOutput"]
        rule = out["additionalContext"]
        assert out["hookEventName"] == "SessionStart" and rule == hs.session_rule("s"), (source, out)
    # the rule names the line the Stop prints and the command to run for it,
    # with this session's id; the line itself carries only the turn
    assert f'"{hs.HANDOFF_PREFIX} N"' in rule and 'harness_stop.py" handoff --moment s#N`' in rule, rule
    assert "say nothing more about it" in rule
    assert hs.handoff_line(_moment(2)).startswith(hs.HANDOFF_PREFIX)
    assert run("0", {"session_id": "s"}).stdout == "", "flag off: silent"
    assert run("1", {"session_id": "s", "agent_id": "a1"}).stdout == "", "a subagent gets no rule"
    assert run("1", {"session_id": "s; rm -rf x"}).stdout == "", "an id unsafe for a command line gets no rule"
    print("PASS test_session_start_carries_the_standing_rule")


def test_one_turn_is_judged_once_per_machine():
    """A machine with both installs (memhub and memhub) runs two Stops
    over one transcript: the first to claim the turn judges it, the other asks
    no classifier and hands off nothing."""
    with _Env() as env:
        tp = env.base / "sess.jsonl"
        _transcript(tp, [("deploy it", "deployed to prod", []),
                         ("no, i mean staging", "redeployed to staging", []),
                         ("thanks", "ok", [])])
        with _classifier(SIGNAL) as sent:
            first = _stop(env, tp)
            second = _stop(env, tp, other_sensor_ran=True)
        assert first is not None and second is None, second
        assert len(sent) == 1, "one classifier call for the turn on the machine"
        log = (env.base / "harness" / "stop.log").read_text()
        assert log.count("judge sess t2: skipped, judged by another sensor") == 1, log
        assert log.count("fork requested") == 1, log
        # a directory that cannot be written judges anyway: a sensor never fails closed
        os.environ["MEMHUB_HARNESS_DIR"] = "/dev/null/harness"
        try:
            assert hs._claim_judgement("sess", 2) is True
        finally:
            os.environ["MEMHUB_HARNESS_DIR"] = str(env.base / "harness")
    print("PASS test_one_turn_is_judged_once_per_machine")


def test_a_session_started_before_the_rule_moved_back_gets_the_line():
    """A session started on 0.118.0-0.120.3 was told the standing rule at its
    first prompt, under a `told/` marker, and only when the mod did not serve
    the harness. Those markers mean nothing now: every flagged turn gets the
    line, told marker or not, and the subcommands only those releases called
    (`prompt`, the mod's `judge` and `relay`) are gone and answer nothing on
    stdout."""
    for marker in (True, False):
        with _Env() as env:
            tp = env.base / "sess.jsonl"
            _transcript(tp, [("deploy it", "deployed to prod", []),
                             ("no, i mean staging", "redeployed to staging", []),
                             ("thanks", "ok", [])])
            if marker:
                stale = env.base / "harness" / "told" / "staging-sess"
                stale.parent.mkdir(parents=True)
                stale.touch()
            with _classifier(SIGNAL):
                out = _stop(env, tp)
            assert out["hookSpecificOutput"]["additionalContext"].startswith(
                f"{hs.HANDOFF_PREFIX} 2,"), (marker, out)
    with _Env():
        for argv in (["prompt"], ["judge", "--json"], ["relay", "--moment", "sess#2"]):
            proc = subprocess.run([sys.executable, str(SCRIPTS / "harness_stop.py"), *argv],
                                  input="{}", capture_output=True, text=True, timeout=30)
            assert proc.returncode == 0 and proc.stdout == "", (argv, proc)
    print("PASS test_a_session_started_before_the_rule_moved_back_gets_the_line")


def test_a_moment_unsafe_for_a_command_line_hands_off_nothing():
    """The session id is the host's string, and it goes into the command the
    session rule asks the agent to run."""
    assert hs.handoff_line(_moment(2)) is not None
    for bad in (_moment(2, source_ref="sess#2; curl x"), _moment(2, source_ref="a b#2")):
        assert hs.handoff_line(bad) is None, bad
    print("PASS test_a_moment_unsafe_for_a_command_line_hands_off_nothing")


def test_the_stamp_command_prints_the_turns_stamp():
    with _Env() as env:
        tp = env.base / "sess.jsonl"
        _transcript(tp, [("a", "b", []), ("c", "d", [])])
        run = lambda turn: subprocess.run(  # noqa: E731
            [sys.executable, str(SCRIPTS / "harness_stop.py"), "stamp", "--transcript", str(tp),
             "--turn", str(turn), "--cwd", str(env.base)],
            capture_output=True, text=True, timeout=30, env=dict(os.environ, MEMHUB_HARNESS_EXTRACT="0"))
        ok = run(2)
        assert ok.returncode == 0, ok.stderr
        stamp = json.loads(ok.stdout)
        assert stamp["session_id"] == "sess" and stamp["turn"] == 2, stamp
        for key in ("repo", "hook_version", "at", "env"):
            assert key in stamp, (key, stamp)
        missing = run(9)
        assert missing.returncode == 2 and "no turn 9" in missing.stderr, missing
    print("PASS test_the_stamp_command_prints_the_turns_stamp")


def test_a_filed_rule_links_to_its_page_in_the_env_it_was_filed_in():
    """`?open=` is the rule modal, not `?rule=`, which filters fire history.
    Only when the plugin points at the same MemHub the rule was filed in."""
    import _memhub_auth
    real = _memhub_auth.default_url
    rid = "846c4331-1b1d-4295-afe9-18156f44f1df"
    try:
        _memhub_auth.default_url = lambda: "https://api.staging.memhub.xtrace.ai"
        assert hs.rule_url(rid, "staging") == f"https://staging.mem.xtrace.ai/studio/rulebook?open={rid}"
        _memhub_auth.default_url = lambda: "https://api.memhub.xtrace.ai"
        assert hs.rule_url(rid, "staging") == ""
    finally:
        _memhub_auth.default_url = real
    print("PASS test_a_filed_rule_links_to_its_page_in_the_env_it_was_filed_in")


_ENTRY = 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hook_entry.py" '


def _harness_handlers():
    """{event: (handler, route)} for the hooks whose route runs harness_stop.py."""
    doc = json.loads((ROOT / "plugins" / "memhub" / "hooks" / "claude-hooks.json").read_text(encoding="utf-8"))
    wired = {}
    for event, groups in doc["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                assert handler["command"].startswith(_ENTRY), handler["command"]
                route = hook_entry.ROUTES[tuple(handler["command"][len(_ENTRY):].split(" "))]
                if any(lane.run[0] == "harness_stop.py" for lane in route.lanes):
                    wired[event] = (handler, route)
    return wired


def _stub_root(td, harness_stop_src):
    """A plugin root holding the real dispatcher, a guard that passes
    everything, and the given harness_stop.py."""
    root = Path(td) / "plugin"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "hook_entry.py").write_bytes((SCRIPTS / "hook_entry.py").read_bytes())
    (root / "scripts" / "claude_hook_guard.py").write_text("def route(*_args):\n    return True\n")
    (root / "scripts" / "harness_stop.py").write_text(harness_stop_src)
    return root


def test_the_hooks_are_wired_behind_the_guard():
    wired = _harness_handlers()
    # Stop judges and hands off; SessionStart carries the standing rule the
    # person does not see. The prompt lane handed 19 moments for 0 proposals
    # and is gone.
    assert set(wired) == {"Stop", "SessionStart"}, set(wired)
    assert [lane.run for lane in wired["SessionStart"][1].lanes] == [("harness_stop.py", "session")]
    assert all(route.harness for _, route in wired.values()), "the same flag gates both"
    # synchronous: the classifier's verdict decides whether this Stop blocks,
    # and an async hook's stdout could not block the stop
    handler = wired["Stop"][0]
    assert not handler.get("async") and handler["timeout"] <= 10
    assert [lane.run for lane in wired["Stop"][1].lanes] == [("harness_stop.py", "stop")]
    for _, route in wired.values():
        assert [lane.guard for lane in route.lanes] == ["ignore"]
    print("PASS test_the_hooks_are_wired_behind_the_guard")


def test_the_hook_command_starts_nothing_unless_the_flag_is_on():
    if os.name == "nt":
        print("SKIP test_the_hook_command_starts_nothing_unless_the_flag_is_on (POSIX hook command)")
        return
    commands = [handler["command"] for handler, _ in _harness_handlers().values()]
    assert len(commands) == 2, "the Stop lane and the SessionStart lane"
    with tempfile.TemporaryDirectory() as td:
        ran = Path(td) / "ran"
        root = _stub_root(td, "import sys\nsys.stdin.read()\nopen(%r, 'a').write(sys.argv[1] + ' ')\n" % str(ran))
        # Default ON: blank runs, as does an on spelling. An off spelling and
        # anything unrecognised stop here, before harness_stop starts — on
        # costs the person a classifier call per flagged turn and an authoring
        # run per moment on their own quota, so a typo must not start it.
        for value, runs in (("", True), ("anything-else", False),
                            ("0", False), ("off", False), ("no", False),
                            ("OFF", False), ("False", False), ("NO", False),
                            ("1", True), ("on", True), ("TRUE", True), ("Yes", True)):
            env = {k: v for k, v in os.environ.items() if k != "MEMHUB_HARNESS_EXTRACT"}
            env.update(CLAUDE_PLUGIN_ROOT=str(root), MEMHUB_HARNESS_EXTRACT=value)
            for command in commands:
                proc = subprocess.run(["bash", "-c", command], input="{}", text=True,
                                      capture_output=True, env=env, timeout=10)
                assert proc.returncode == 0, proc.stderr
            got = sorted(ran.read_text().split()) if ran.exists() else []
            assert got == (["session", "stop"] if runs else []), (value, got)
            if ran.exists():
                ran.unlink()
    print("PASS test_the_hook_command_starts_nothing_unless_the_flag_is_on")


# One table for every copy of the MEMHUB_HARNESS_EXTRACT switch. None = unset.
# Python's str.strip() drops ten ASCII characters (space, \t \n \v \f \r and
# \x1c-\x1f), so each one pads a value here: the Stop hook's shell gate used
# to compare the raw value, and `" 1 "` meant on in Python and off in the hook
# (public #270's Codex review). Non-ASCII whitespace is deliberately absent:
# both gates trim only these ten (str.strip() would also drop U+00A0 and
# friends), so a flag padded with those reads as unrecognised, which is off.
_ASCII_WS = "".join(chr(i) for i in range(128) if chr(i).isspace())


EXTRACT_FLAG_TABLE = (
    [("1", True), (" 1 ", True), ("1\n", True), ("on", True), ("ON ", True),
     ("true", True), ("yes", True), ("Yes", True), ("TRUE", True),
     ("\ton\r\n", True), ("\x1c1\x1f", True), ("\v yes \f", True),
     ("", True), (" ", True), ("\n", True), (None, True),
     ("0", False), ("off", False), ("no", False), ("onn", False),
     ("2", False), ("1 1", False), ("o n", False), ("\xa01", False),
     (" 0 ", False), ("anything-else", False)]
    + [(ws + "0" + ws, False) for ws in _ASCII_WS]
    + [(ws + "1" + ws, True) for ws in _ASCII_WS]
    + [(ws + "on", True) for ws in _ASCII_WS]
    + [("true" + ws, True) for ws in _ASCII_WS])


def test_every_extract_gate_reads_the_flag_the_same_way():
    """`extract_enabled` and the hooks' gate in hook_entry.py are one switch.
    Each value in the table goes to every copy — `hook_entry.harness_enabled`
    directly, and the REAL command strings from claude-hooks.json under every
    POSIX shell on the box, with a stub plugin root that records whether it
    got past the gate — and all of them must give the table's answer. Only
    the Claude hooks carry a copy: the Codex file is generated without the
    harness lane and Cursor never wires it (asserted here)."""
    hooks = ROOT / "plugins" / "memhub" / "hooks"
    for other in ("codex-hooks.json", "cursor-hooks.json"):
        assert "HARNESS" not in (hooks / other).read_text(encoding="utf-8"), other
    commands = [handler["command"] for handler, _ in _harness_handlers().values()]
    # the Stop lane and the SessionStart lane: one switch behind both
    assert len(commands) == 2, commands
    shells = [] if os.name == "nt" else [
        s for s in ("/bin/sh", "/bin/dash", "/usr/bin/dash", "/bin/bash") if os.path.exists(s)]
    wrong = []
    with tempfile.TemporaryDirectory() as td:
        ran = Path(td) / "ran"
        root = _stub_root(td, "import sys\nsys.stdin.read()\nopen(%r, 'a').write('x')\n" % str(ran))
        base = {k: v for k, v in os.environ.items()
                if k not in ("MEMHUB_HARNESS_EXTRACT", "MEMHUB_HARNESS_CHILD")}
        base["CLAUDE_PLUGIN_ROOT"] = str(root)
        for value, want in EXTRACT_FLAG_TABLE:
            env = dict(base) if value is None else dict(base, MEMHUB_HARNESS_EXTRACT=value)
            got = {"extract_enabled": hx.extract_enabled(env),
                   "hook_entry.harness_enabled": hook_entry.harness_enabled(env)}
            for shell in shells:
                for n, command in enumerate(commands):
                    proc = subprocess.run([shell, "-c", command], input="{}", text=True,
                                          capture_output=True, env=env, timeout=10)
                    assert proc.returncode == 0, (shell, value, proc.stderr)
                    got["hook %d under %s" % (n, shell)] = ran.exists()
                    if ran.exists():
                        ran.unlink()
            wrong += [(value, gate, answer) for gate, answer in got.items() if answer != want]
    assert not wrong, "%d answers disagree with the table (value, gate, answer):\n%s" % (
        len(wrong), "\n".join(map(repr, wrong)))
    print("PASS test_every_extract_gate_reads_the_flag_the_same_way (%d values; shells: %s)"
          % (len(EXTRACT_FLAG_TABLE), ", ".join(shells) or "none"))


def test_the_sensor_never_sends_activate():
    import re  # noqa: PLC0415
    src = (SCRIPTS / "harness_stop.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"[\"']activate[\"']\s*:", code)
    assert "activate=" not in code and "call_tool" not in code
    print("PASS test_the_sensor_never_sends_activate")


def test_a_recorded_block_is_not_a_turn():
    """Claude Code records the block's reason as an isMeta `user` record
    (`Stop hook feedback:\\n<reason>`, seen on 2.1.270). Read as a human
    message it would renumber every later turn and hand the classifier the
    harness talking to itself."""
    with _Env() as env:
        tp = env.base / "s.jsonl"
        reason = hs.fork_reason(_moment(1), "staging", "repo")
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
        # the stopped turn ends at the feedback record: the next Stop must not
        # hand the classifier the harness's own flow as turn 1 (Codex, #230)
        assert turns[0]["asst"] == "done" and turns[0]["tools"] == [] and turns[0]["results"] == [], turns[0]
    print("PASS test_a_recorded_block_is_not_a_turn")


def test_a_recorded_stop_context_ends_the_turn():
    """The hand-off is the Stop hook's `additionalContext`, which Claude Code
    records as an `attachment` record (hook_additional_context, hookEvent Stop;
    probed on 2.1.286), not a user record. It ends the stopped turn exactly as
    a block's reason did: the fork launch that follows is not the person's
    turn. Other events' context (PreToolUse, SessionStart) lands mid-turn and
    ends nothing."""
    with _Env() as env:
        tp = env.base / "s.jsonl"
        ctx = hs.fork_reason(_moment(1), "staging", "repo")

        def att(event, text):
            return {"type": "attachment", "attachment": {
                "type": "hook_additional_context", "content": [text],
                "hookName": event, "hookEvent": event}}

        recs = [{"type": "user", "uuid": "u1", "message": {"content": "fix the deploy"}},
                att("PreToolUse", "## XTrace Rulebook (team rules)"),
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "c0", "name": "Bash", "input": {"command": "make deploy"}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "c0", "content": "ok"}]}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
                att("Stop", ctx),
                # the continuation it asked for: the fork launch is not turn 1's
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "c1", "name": "Agent",
                     "input": {"subagent_type": "fork", "prompt": "MemHub harness fork, moment s#1"}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": "launched"}]}},
                {"type": "user", "uuid": "u2", "message": {"content": "now the tests"}}]
        tp.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        turns = hx.turns_from_transcript(tp)
        assert [t["user"] for t in turns] == ["fix the deploy", "now the tests"], turns
        assert [t["tools"][0]["tool"] if t["tools"] else None for t in turns][:1] == ["Bash"], turns[0]
        assert all(x["tool"] != "Agent" for x in turns[0]["tools"]), "the launch is not turn 1's"
        assert turns[0]["asst"] == "done", turns[0]
    print("PASS test_a_recorded_stop_context_ends_the_turn")


# ------------------------------------------------------------- read offset
def _growing_session():
    """Nine turns with tools, errors and a closed error arc, so a window
    carries every part a full read would build."""
    out = []
    for i in range(1, 10):
        tools = [("Bash", {"command": f"make t{i}"}, f"boom {i}", True),
                 ("Bash", {"command": f"make t{i}"}, f"ok {i}", False)] if i % 2 else \
                [("Edit", {"file_path": f"/w/f{i}.py"}, "edited", False)]
        out.append((f"message {i}: no, use staging {i}", f"reply {i}", tools))
    return out


def _windows_without_stamp_time(sent):
    import re as _re
    return [_re.sub(r'"at": "[^"]*"', '"at": ""', w) for w in sent]


def test_an_offset_read_builds_the_same_turns_and_window_as_a_full_read():
    with _Env() as env:
        tp = env.base / "sess.jsonl"
        turns = _growing_session()
        offsets = env.base / "harness" / "offsets" / "sess.json"
        for k in range(2, len(turns) + 1):
            _transcript(tp, turns[:k])            # the session grows, append-only
            full, full_last = hx.read_transcript(tp)
            with _classifier({"signal": False, "reason": "classified"}) as resumed:
                _stop(env, tp)
            saved = json.loads(offsets.read_text())
            # the offset never passes a turn the next window may need: the
            # newest turn (judged at the next Stop) and its four lead-in turns
            assert saved["offset"] <= full[-min(hs.LEAD_TURNS, len(full))]["offset"], (k, saved)
            assert saved["offset"] <= full[-1]["offset"], (k, saved)
            part, part_last = hs.read_turns("sess", str(tp))
            assert part_last == full_last, k
            assert part == full[-len(part):], f"turn {k}: resumed turns differ from a full read"
            assert len(part) >= min(hs.LEAD_TURNS, len(full)), (k, len(part))
            assert part[-1]["n"] == k, "numbering counts from the session start"
            offsets.unlink()                      # the same Stop, from a full read
            with _classifier({"signal": False, "reason": "classified"}) as fresh:
                _stop(env, tp)
            assert _windows_without_stamp_time(resumed) == _windows_without_stamp_time(fresh), k
        # the last read really did start mid-file
        saved = json.loads(offsets.read_text())
        assert saved["offset"] > 0 and saved["before"] == len(turns) - hs.LEAD_TURNS, saved
    print("PASS test_an_offset_read_builds_the_same_turns_and_window_as_a_full_read")


def test_a_shrunk_or_rewritten_transcript_reads_from_the_start():
    with _Env() as env:
        tp = env.base / "sess.jsonl"
        _transcript(tp, _growing_session())
        with _classifier({"signal": False, "reason": "classified"}):
            _stop(env, tp)
        offsets = env.base / "harness" / "offsets" / "sess.json"
        assert json.loads(offsets.read_text())["offset"] > 0
        # a shorter, different transcript under the same path
        _transcript(tp, [("deploy it", "deployed to prod", []),
                         ("no, i mean staging", "redeployed", []),
                         ("thanks", "ok", [])])
        with _classifier(SIGNAL) as sent:
            block = _stop(env, tp)
        assert block and f"{hs.HANDOFF_PREFIX} 2," in block["hookSpecificOutput"]["additionalContext"], block
        assert "USER'S NEW MESSAGE: no, i mean staging" in sent[0], sent[0]
        # a same-size rewrite whose bytes at the offset are another record:
        # the saved turn is not where it was, so the read starts over
        _transcript(tp, _growing_session())
        with _classifier({"signal": False, "reason": "classified"}):
            _stop(env, tp)
        rec = json.loads(offsets.read_text())
        offsets.write_text(json.dumps(dict(rec, uuid="someone-else")))
        turns, _ = hs.read_turns("sess", str(tp))
        assert turns[0]["n"] == 1 and len(turns) == 9, "a mismatched offset is a full read"
    print("PASS test_a_shrunk_or_rewritten_transcript_reads_from_the_start")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
