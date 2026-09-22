#!/usr/bin/env python3
"""An author child can never spawn an author child.

The loop (v0.74.0–v0.76.0): the child's `MEMHUB_HARNESS_EXTRACT=0` was
overridden by the install's settings.json `env`, so the child was sensed, its
create-rule turn was flagged, and its Stop forked it — one generation per
generation. `extract_enabled` now refuses on `MEMHUB_HARNESS_CHILD`, which is
one more environment variable, and an environment variable is what failed.

These tests hold the guards that are NOT the environment, each one on its own
with the environment saying the opposite: the child's session id is chosen by
the spawner and on the children list before the child runs; a Stop for a
listed session senses nothing and spawns nothing even with the flag on and the
child flag unset; a moment stamped inside a child is never drained, so debris
from before the list existed cannot start the loop; the depth flag alone
refuses; the machine-wide breaker refuses a third pass whatever asked for it;
and the child's own hand-off prompt is harness text to the sensor. No test
starts a `claude`.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "plugins" / "memhub" / "scripts"))

import harness_extract as hx  # noqa: E402
import harness_stop as hs  # noqa: E402
from harness_stop_test import _Env, _spawns  # noqa: E402


def _moment(turn: int, session: str, repo: str = "repo") -> dict:
    return {"turn": turn, "source_ref": f"{session}#{turn}", "kind": "correction",
            "state": {"session_id": session, "repo": repo, "turn": turn,
                      "at": "2026-09-21T00:00:00Z"}}


def _launch(stdout="HARNESS-RESULT: none x\n"):
    """Run `run_author` against a fake claude; return what it was launched with."""
    seen = {}

    def fake_run(argv, **kw):
        # what the child's FIRST Stop would see: the list, as it stands now
        seen.update(argv=argv, env=kw.get("env") or {},
                    listed_at_launch=hs.is_registered_child(argv[argv.index("--session-id") + 1]))
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    real = hs.subprocess.run
    hs.subprocess.run = fake_run
    try:
        got = hs.run_author("sess", _moment(2, "owner"), "repo", Path("/nonexistent/mcp.json"))
    finally:
        hs.subprocess.run = real
    return got, seen


def test_the_child_is_on_the_list_before_it_runs():
    with _Env():
        (outcome, fields), seen = _launch()
        child = seen["argv"][seen["argv"].index("--session-id") + 1]
        assert seen["listed_at_launch"], "registered after the spawn is registered too late"
        assert fields["child"] == child and outcome == "none"
        assert seen["env"].get(hs.DEPTH_FLAG) == "1"
        rows = hx.read_jsonl(hs.children_path())
        assert rows and rows[-1]["child"] == child and rows[-1]["owner"] == "owner"
        assert rows[-1]["ref"] == "owner#2"
    print("PASS test_the_child_is_on_the_list_before_it_runs")


def test_a_listed_sessions_stop_senses_nothing_with_the_environment_saying_otherwise():
    with _Env() as env:
        tp = env.base / "kid.jsonl"
        tp.write_text("", encoding="utf-8")
        hs.register_child("kid", "owner", "owner#2")
        os.environ["MEMHUB_HARNESS_EXTRACT"] = "1"          # the settings override
        os.environ.pop("MEMHUB_HARNESS_CHILD", None)         # the lost env guard
        os.environ.pop(hs.DEPTH_FLAG, None)
        payload = {"session_id": "kid", "transcript_path": str(tp), "cwd": str(ROOT)}
        assert _spawns(lambda: hs.cmd_stop(payload)) == []
        # and a person's session beside it is still sensed
        payload["session_id"] = "person"
        (env.base / "person.jsonl").write_text("", encoding="utf-8")
        payload["transcript_path"] = str(env.base / "person.jsonl")
        assert len(_spawns(lambda: hs.cmd_stop(payload))) == 1
    print("PASS test_a_listed_sessions_stop_senses_nothing_with_the_environment_saying_otherwise")


def test_a_moment_stamped_inside_a_child_is_never_drained():
    """Debris from an install that had no list: the child's moments are on
    disk, waiting. Draining one would fork the child — the loop's first step."""
    with _Env():
        hs.register_child("kid", "owner", "owner#2")
        hx.append_jsonl(hs.moments_path("kid"), _moment(1, "kid"))
        hx.append_jsonl(hs.moments_path("person"), _moment(1, "person"))
        got = [m["source_ref"] for _, m in hs.pending("person", "repo", time.time())]
        assert got == ["person#1"], got
    print("PASS test_a_moment_stamped_inside_a_child_is_never_drained")


def test_the_depth_flag_alone_refuses():
    with _Env() as env:
        tp = env.base / "x.jsonl"
        tp.write_text("", encoding="utf-8")
        os.environ[hs.DEPTH_FLAG] = "1"
        try:
            payload = {"session_id": "unlisted", "transcript_path": str(tp), "cwd": str(ROOT)}
            assert _spawns(lambda: hs.cmd_stop(payload)) == []
            os.environ[hs.DEPTH_FLAG] = "garbage"
            assert _spawns(lambda: hs.cmd_stop(payload)) == [], "unreadable depth is not a person"
        finally:
            os.environ.pop(hs.DEPTH_FLAG, None)
    print("PASS test_the_depth_flag_alone_refuses")


def test_the_machine_wide_breaker_refuses_a_third_pass():
    with _Env():
        hs._publish(hs.meta_path("s3"), json.dumps({"repo": "repo"}))
        hx.append_jsonl(hs.moments_path("s3"), _moment(1, "s3"))
        for other in ("s1", "s2"):
            hs.take_drain_claim(other, time.time())      # two passes already running
        assert hs.live_drains(time.time()) == 2
        assert _spawns(lambda: hs.hand_off("s3")) == []
        assert not hx.session_file("s3", ".drain.claim").exists(), "no claim taken when refused"
        # one finishes → the next Stop spawns
        hx.session_file("s1", ".drain.claim").unlink()
        assert len(_spawns(lambda: hs.hand_off("s3"))) == 1
    print("PASS test_the_machine_wide_breaker_refuses_a_third_pass")


def test_the_childs_own_prompt_is_harness_text():
    assert hx.is_harness_text(hs.BLOCK_PREFIX + ": turn 3 was flagged as correction.")
    assert hx.is_harness_text(hs.author_prompt("o", _moment(3, "o"), "repo"))
    assert not hx.is_harness_text("why did it reach prod? i meant staging")
    print("PASS test_the_childs_own_prompt_is_harness_text")


def test_the_child_leaves_no_transcript_and_capture_refuses_it_by_the_list():
    """No transcript: nothing to capture, sense or resume, whatever the
    environment says. And a capture hook that IS handed a transcript for a
    listed session refuses it without the env guard."""
    import io  # noqa: PLC0415
    import flush_turn  # noqa: PLC0415
    import transcript_filter as tf  # noqa: PLC0415
    with _Env() as env:
        _, seen = _launch()
        assert "--no-session-persistence" in seen["argv"]
        hs.register_child("kid", "owner", "owner#2")
        os.environ.pop("MEMHUB_HARNESS_CHILD", None)
        assert tf.is_harness_child(session_id="kid")
        assert not tf.is_harness_child(session_id="person")
        tp = env.base / "kid.jsonl"
        tp.write_text("", encoding="utf-8")
        real_stdin, real_acquire = sys.stdin, flush_turn._acquire

        def never(*a, **k):
            raise AssertionError("capture went past the children list")

        sys.stdin = io.StringIO(json.dumps({"session_id": "kid", "transcript_path": str(tp)}))
        flush_turn._acquire = never
        try:
            assert flush_turn.main() == 0
        finally:
            sys.stdin, flush_turn._acquire = real_stdin, real_acquire
    print("PASS test_the_child_leaves_no_transcript_and_capture_refuses_it_by_the_list")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("ALL PASS")
