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
refuses; the machine-wide breaker holds under concurrent Stops and its slots are
kernel-held locks that a dead holder cannot keep; a pre-list child's
moment is found by its transcript and quarantined, never authored;
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


def test_the_machine_wide_breaker_holds_under_concurrent_stops():
    """Count-then-claim let every concurrent Stop count the same number and
    all spawn (Codex, #275). The slot is the count: of N Stops racing, at
    most MAX_LIVE_DRAINS hold one."""
    import threading  # noqa: PLC0415
    with _Env():
        for n in range(1, 9):
            hs._publish(hs.meta_path(f"s{n}"), json.dumps({"repo": "repo"}))
            hx.append_jsonl(hs.moments_path(f"s{n}"), _moment(1, f"s{n}"))
        spawned = []
        real = hx.subprocess.Popen
        hx.subprocess.Popen = lambda args, **kw: spawned.append(args)
        gate = threading.Barrier(8)

        def stop(n):
            gate.wait()
            hs.hand_off(f"s{n}")

        threads = [threading.Thread(target=stop, args=(n,)) for n in range(1, 9)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            hx.subprocess.Popen = real
        assert len(spawned) == hs.MAX_LIVE_DRAINS, len(spawned)
        assert hs.live_drains() == hs.MAX_LIVE_DRAINS
        # the child is handed the lock fds, and only those
        for args in spawned:
            fds = [int(x) for x in args[args.index("--lease-fds") + 1].split(",")]
            assert len(fds) == 2 and all(os.fstat(fd) for fd in fds)
        # a refused Stop released its per-session claim, so it can retry later
        held = {a[a.index("--session") + 1] for a in spawned}
        for n in range(1, 9):
            probe = hs.take_drain_claim(f"s{n}")
            assert (probe is None) == (f"s{n}" in held), n
            if probe:
                probe.close()
        # a finished pass drops its fds; the next Stop takes the freed slot
        hs._HELD.pop(1).close()                 # the first spawn's slot
        spawned.clear()
        hx.subprocess.Popen = lambda args, **kw: spawned.append(args)
        try:
            free = next(n for n in range(1, 9) if f"s{n}" not in held)
            hs.hand_off(f"s{free}")
        finally:
            hx.subprocess.Popen = real
        assert len(spawned) == 1
    print("PASS test_the_machine_wide_breaker_holds_under_concurrent_stops")


def test_a_pre_list_childs_moment_is_quarantined_not_authored():
    """v0.74.0–v0.76.0 kept no list. Their children's moments are on disk with
    nothing marking them but the transcript, which holds the hand-off as a
    human-role message (Codex, #275). Nothing here registers the child by
    hand: the pass has to find it."""
    with _Env() as env:
        projects = env.base / "projects" / "-some-cwd"
        projects.mkdir(parents=True)
        old_cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(env.base)
        try:
            # a legacy child: the owner's history, then the hand-off, then its own turn
            legacy = projects / "legacykid.jsonl"
            legacy.write_text("\n".join(json.dumps(r) for r in [
                {"type": "user", "message": {"content": "fix the flaky test"}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}},
                {"type": "user", "message": {"content": hs.author_prompt("legacykid", _moment(1, "owner"), "repo")}},
                {"type": "assistant", "message": {"content": [{"type": "text", "text": "HARNESS-RESULT: none x"}]}},
            ]) + "\n", encoding="utf-8")
            # a person's session that happens to QUOTE the sentence in a tool result
            person = projects / "person.jsonl"
            person.write_text("\n".join(json.dumps(r) for r in [
                {"type": "user", "message": {"content": "why did the child say that?"}},
                {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1",
                                                          "content": hs.CHILD_MARK + ": there is no one"}]}},
            ]) + "\n", encoding="utf-8")
            hs._publish(hs.meta_path("drainer"), json.dumps({"repo": "repo"}))
            hx.append_jsonl(hs.moments_path("legacykid"), _moment(3, "legacykid"))
            hx.append_jsonl(hs.moments_path("person"), _moment(2, "person"))
            authored = []
            real_run, real_cfg = hs.run_author, hs.write_child_mcp_config
            hs.run_author = lambda s, m, r, c, **kw: (authored.append(m["source_ref"]),
                                                      ("none", {"detail": "x"}))[1]
            hs.write_child_mcp_config = lambda d: (Path(d) / "mcp.json", "http://x")
            try:
                hs.cmd_author("drainer", ["legacykid#3", "person#2"])
            finally:
                hs.run_author, hs.write_child_mcp_config = real_run, real_cfg
            assert authored == ["person#2"], authored
            assert hs.is_registered_child("legacykid") and not hs.is_registered_child("person")
            rows = hx.read_jsonl(hs.moments_path("legacykid"))
            q = [r for r in rows if r.get("outcome") == "quarantined"]
            assert q and q[0]["handed"] == "legacykid#3"
            # listed now: the Stop hook skips it without reading any transcript
            assert [m["source_ref"] for _, m in hs.pending("drainer", "repo", time.time())] == []
            # and a quarantined row is not something the person is told about
            assert hs.report_outcomes("legacykid") == ""
        finally:
            if old_cfg is None:
                os.environ.pop("CLAUDE_CONFIG_DIR", None)
            else:
                os.environ["CLAUDE_CONFIG_DIR"] = old_cfg
    print("PASS test_a_pre_list_childs_moment_is_quarantined_not_authored")


def test_a_dead_holders_slot_is_freed_by_the_kernel_and_a_live_ones_is_not():
    """No staleness heuristic, no unlink: the slot is an advisory lock on an
    open file. Three shapes of file-based lease each left a race for a burst
    of Stops (Codex, #275 ×3); a lock a dead process held is released by the
    kernel, and a lock a live process holds cannot be taken by anyone."""
    with _Env():
        base = hx.harness_dir()
        base.mkdir(parents=True, exist_ok=True)
        slot = base / "drain.slot-0"
        # a holder that died mid-pass: lock taken, process gone, no release code ran
        code = ("import os,sys; sys.path.insert(0, %r); import portable_lock as pl\n"
                "f = open(%r, 'a+'); pl.lock_exclusive(f.fileno(), blocking=False)\n"
                "print('locked', flush=True); os._exit(0)") % (str(ROOT / "plugins" / "memhub" / "scripts"), str(slot))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        assert "locked" in out.stdout, out
        assert hs.take_lease(slot) is not None, "a dead holder's lock was not released"
        hs._HELD.clear()
        # a live holder in another process: nobody can take it while it lives
        code = ("import os,sys,time; sys.path.insert(0, %r); import portable_lock as pl\n"
                "f = open(%r, 'a+'); pl.lock_exclusive(f.fileno(), blocking=False)\n"
                "print('locked', flush=True); sys.stdin.readline()") % (str(ROOT / "plugins" / "memhub" / "scripts"), str(slot))
        holder = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "locked"
            assert hs.take_lease(slot) is None, "took a slot a live process holds"
            assert hs.live_drains() == 1
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
        got = hs.take_lease(slot)
        assert got is not None
        got.close()
        # the lock is inherited by a child that is handed the fd, and released
        # when that child exits — the shape the Stop hook relies on
        mine = hs.take_lease(slot)
        code = "import os,sys; os.fstat(int(sys.argv[1])); sys.stdin.readline()"
        kid = subprocess.Popen([sys.executable, "-c", code, str(mine.fileno())],
                               pass_fds=(mine.fileno(),), stdin=subprocess.PIPE, text=True)
        try:
            mine.close()                        # the hook exits; the child still holds it
            time.sleep(0.2)
            assert hs.take_lease(slot) is None, "lock did not survive the parent closing"
        finally:
            kid.stdin.close()
            kid.wait(timeout=30)
        got = hs.take_lease(slot)
        assert got is not None, "lock did not release when the child exited"
        got.close()
    print("PASS test_a_dead_holders_slot_is_freed_by_the_kernel_and_a_live_ones_is_not")


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
