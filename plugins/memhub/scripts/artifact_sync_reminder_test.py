"""Self-test for the artifact-sync reminder hook.

Covers the spec's acceptance criteria: mapped edit reminds with the exact
save_artifact call, unmapped edit is silent, N files -> one reminder per
artifact (session debounce), and a missing/invalid map is a clean no-op.

Run: python3 artifact_sync_reminder_test.py  (stdlib only).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_sync_reminder as asr  # noqa: E402

HOOK = Path(__file__).resolve().parent / "artifact_sync_reminder.py"

BRAIN = "968e7580-50f8-43d9-828b-0dfb4b1f3342"
ARTIFACT = "6dfde613-66d1-5abe-bd8e-af34b3ffd866"
MAP = {
    "version": 1,
    "links": [
        {
            "glob": "appworld/{run,agent,worker}.py",
            "brain_id": BRAIN,
            "artifact_id": ARTIFACT,
            "artifact_name": "AppWorld eval harness + results (canonical)",
        },
        {
            "glob": "xmem/**/reanchor.py|xmem/serve/entities.py",
            "brain_id": BRAIN,
            "artifact_id": "8f7331dd-022c-57c2-9ac0-229b93bc8747",
            "artifact_name": "Directive-anchoring handoff",
        },
    ],
}


# --- glob semantics (spec §4: braces, **, alternation, non-match) -----------

def test_brace_expansion():
    glob = "appworld/{run,agent,worker}.py"
    assert asr._matches(glob, "appworld/run.py")
    assert asr._matches(glob, "appworld/worker.py")
    assert not asr._matches(glob, "appworld/other.py")


def test_alternation():
    glob = "xmem/ingest/reanchor.py|xmem/serve/entities.py"
    assert asr._matches(glob, "xmem/ingest/reanchor.py")
    assert asr._matches(glob, "xmem/serve/entities.py")
    assert not asr._matches(glob, "xmem/serve/other.py")


def test_star_stops_at_slash_but_doublestar_crosses():
    assert asr._matches("xmem/*.py", "xmem/run.py")
    assert not asr._matches("xmem/*.py", "xmem/serve/run.py")
    assert asr._matches("xmem/**/run.py", "xmem/serve/deep/run.py")
    # **/ must also match zero directories.
    assert asr._matches("xmem/**/run.py", "xmem/run.py")


def test_non_match_is_not_a_substring_match():
    # fullmatch, not search: a mapped path must not fire on a longer path.
    assert not asr._matches("appworld/run.py", "vendor/appworld/run.py")


# --- end-to-end through the real hook process ------------------------------

def _repo(tmp: Path, write_map: str | None) -> Path:
    root = tmp / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "appworld").mkdir()
    if write_map is not None:
        (root / ".claude").mkdir()
        (root / ".claude" / "artifact-map.json").write_text(write_map)
    return root


def _run(root: Path, relpath: str, session: str, tmpdir: Path) -> str:
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(root / relpath)},
        "cwd": str(root),
        "session_id": session,
    }
    env = {**os.environ, "TMPDIR": str(tmpdir)}  # redirect the debounce state
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"hook must never fail an edit: {proc.stderr}"
    return proc.stdout.strip()


def test_mapped_edit_emits_the_literal_save_artifact_call():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = _repo(tmp, json.dumps(MAP))
        out = _run(root, "appworld/run.py", "sess-a", tmp)
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert f'parent_id="{ARTIFACT}"' in context
        assert f'agent_brain_id="{BRAIN}"' in context
        assert "appworld/run.py" in context
        assert "Do NOT create a new artifact" in context


def test_unmapped_edit_is_silent():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = _repo(tmp, json.dumps(MAP))
        assert _run(root, "appworld/README.md", "sess-b", tmp) == ""


def test_same_artifact_reminds_once_per_session():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = _repo(tmp, json.dumps(MAP))
        assert _run(root, "appworld/run.py", "sess-c", tmp) != ""
        # Second file, same artifact, same session -> debounced.
        assert _run(root, "appworld/worker.py", "sess-c", tmp) == ""
        # A new session starts fresh.
        assert _run(root, "appworld/worker.py", "sess-d", tmp) != ""


def test_missing_and_malformed_map_are_clean_noops():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        assert _run(_repo(tmp / "a", None), "appworld/run.py", "s1", tmp) == ""
        assert _run(_repo(tmp / "b", "{not json"), "appworld/run.py", "s2", tmp) == ""
        assert _run(_repo(tmp / "c", '{"links": "nope"}'), "appworld/run.py", "s3", tmp) == ""


def test_failed_emit_does_not_burn_the_debounce():
    # Pinned: the debounce is persisted only AFTER the reminder is out. If it
    # were recorded first, a killed process or a failing write (BrokenPipe,
    # UnicodeEncodeError under a non-UTF-8 locale) would mark the artifact
    # reminded while the agent never saw it — a permanent silent miss for the
    # rest of the session. Failing the emit here must leave no state behind.
    import io

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = _repo(tmp, json.dumps(MAP))
        payload = {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(root / "appworld/run.py")},
            "cwd": str(root),
            "session_id": "sess-emit-fail",
        }
        state = Path(tempfile.gettempdir()) / f"{asr.STATE_PREFIX}sess-emit-fail.json"
        state.unlink(missing_ok=True)

        def boom(*_args, **_kwargs):
            raise BrokenPipeError("hook consumer went away")

        real_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        asr.print = boom  # module global shadows the builtin
        try:
            try:
                asr.main()
            except BrokenPipeError:
                pass  # the __main__ guard swallows this in the real hook
        finally:
            sys.stdin = real_stdin
            del asr.print

        assert not state.exists(), "debounce was recorded despite a failed emit"


# --- artifact_map.py writes what the hook reads -----------------------------

def _git_init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)


def _map_cli(root: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK.parent / "artifact_map.py"), *argv],
        cwd=str(root),
        capture_output=True,
        text=True,
    )


def test_map_add_is_idempotent_per_artifact_and_feeds_the_hook():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        root = tmp / "repo"
        _git_init(root)
        (root / "app").mkdir()

        add = ["add", "--artifact-id", ARTIFACT, "--brain-id", BRAIN,
               "--name", "Spec: Retry policy", "--glob", "app/retry.py"]
        assert _map_cli(root, *add).returncode == 0
        # Re-linking the same artifact with new globs replaces, never appends.
        proc = _map_cli(root, *add[:-1], "app/{retry,backoff}.py")
        assert proc.returncode == 0, proc.stderr
        written = json.loads((root / ".claude" / "artifact-map.json").read_text())
        assert len(written["links"]) == 1

        # The hook picks the map up with no further wiring.
        out = _run(root, "app/backoff.py", "sess-map", tmp)
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert f'parent_id="{ARTIFACT}"' in context
        assert _run(root, "app/unrelated.py", "sess-map2", tmp) == ""


def test_map_list_for_path_reports_the_governing_artifact():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        _git_init(root)
        _map_cli(root, "add", "--artifact-id", ARTIFACT, "--brain-id", BRAIN,
                 "--name", "Spec: Retry policy", "--glob", "app/**/retry.py")
        hit = _map_cli(root, "list", "--for", "app/deep/retry.py")
        assert "Spec: Retry policy" in hit.stdout
        miss = _map_cli(root, "list", "--for", "app/other.py")
        assert "not linked to any artifact" in miss.stdout


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
