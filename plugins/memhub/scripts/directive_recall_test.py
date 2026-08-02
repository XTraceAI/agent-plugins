"""Self-test for directive_recall's reactive path — against the REAL case.

The measured motivator (2026-07-14): a dangling-$ref lesson anchored on
``openapi-typescript`` / ``app/memory/openapi.py`` never fired when
``npm run gen:types`` failed, because the command line only shows the npm
alias. These tests replay that exact case through the gate functions.

Run: python3 directive_recall_test.py  (stdlib only — the mcp import in
directive_recall is lazy, inside _recall).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import directive_recall as dr  # noqa: E402

F6_LESSON = {
    "id": "f6", "type": "lesson",
    "content": "When openapi-typescript fails on Memory.details oneOf, add the missing detail schema to app/memory/openapi.py.",
    "triggers": ["openapi-typescript", "app/memory/openapi.py", "DirectiveDetails"],
}
GEN_TYPES_ARGS = {"command": "npm run gen:types"}
GEN_TYPES_ERROR = (
    "✨ openapi-typescript 7.13.0\n"
    " ✘  Can't resolve $ref at #/components/schemas/Memory/properties/details/oneOf/3\n"
    "Error: Can't resolve $ref at #/components/schemas/Memory/properties/details/oneOf/3"
)
CWD = "/Users/felixmeng/xtrace/memory-sdk-ts"


def test_pretool_path_misses_the_alias():
    # The original miss, pinned: on the PreToolUse handle alone the lesson is
    # (correctly) precision-dropped — its anchors aren't in "npm run gen:types".
    assert dr._precision_filter([F6_LESSON], GEN_TYPES_ARGS, CWD) == []


def test_reactive_haystack_fires_at_the_failure_site():
    kept = dr._precision_filter([F6_LESSON], GEN_TYPES_ARGS, CWD, GEN_TYPES_ERROR)
    assert kept == [F6_LESSON]


def test_error_output_gates_success_and_extracts_tail():
    assert dr._error_output({"tool_response": "All checks passed!"}) is None
    assert dr._error_output({}) is None
    tail = dr._error_output({"tool_response": {"stdout": "x" * 5000 + GEN_TYPES_ERROR}})
    assert tail and "Can't resolve $ref" in tail and len(tail) <= dr._MAX_OUTPUT_CHARS


def test_error_regexes_stay_in_sync():
    import reactive_prefilter as rp
    assert rp._ERROR_RE.pattern == dr._ERROR_RE.pattern


# --- ranked cap + why-fired (2026-08-02 audit follow-ups) -------------------

def _mk(i, triggers, seen=1):
    return {"id": i, "type": "lesson", "content": f"lesson {i}",
            "triggers": triggers, "seen": seen}


def test_generic_git_words_cannot_carry_a_match():
    # `origin` / `staging` word-tokens alone drove 1,461 audited injections:
    # a directive anchored on the origin/staging PATH must not fire on a mere
    # `git push origin <branch>` …
    d = _mk("w", ["origin/staging"])
    assert dr._precision_filter([d], {"command": "git push origin fm-feat/x"}, "") == []
    # … but must still fire where the full path is literally named — the
    # audited in-place-rebase failure is exactly this call.
    kept = dr._precision_filter([d], {"command": "git rebase origin/staging"}, "")
    assert kept and kept[0]["_match"] == "origin/staging"


def test_rank_prefers_specific_match_then_seen_and_caps():
    specific = _mk("a", ["swerex/deployment/modal.py"])
    generic = _mk("b", ["modal"], seen=9)
    unverified = _mk("c", None)
    args = {"command": "uv run python swerex/deployment/modal.py --deploy"}
    kept = dr._precision_filter([unverified, generic, specific], args, "")
    top = dr._rank(kept)[:dr._MAX_DIRECTIVES]
    assert [d["id"] for d in top] == ["a", "b"]  # match beats unverified; specificity beats seen
    assert dr._MAX_DIRECTIVES == 2


def test_render_shows_the_matched_trigger():
    d = _mk("a", ["swerex/deployment/modal.py", "boto3"], seen=3)
    kept = dr._precision_filter([d], {"command": "vim swerex/deployment/modal.py"}, "")
    out = dr._render(kept)
    assert "fired on: swerex/deployment/modal.py" in out and "seen 3×" in out


def test_handle_cache_roundtrip(tmp_dir=None):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        orig = dr._STATE_DIR
        dr._STATE_DIR = Path(td)
        try:
            sid = "sess-abc"
            key = dr._handle_key("Edit", {"file_path": "app/main.py"})
            assert key == "Edit:app/main.py"
            assert dr._load_handles(sid) == []
            dr._save_handles(sid, [key])
            assert dr._load_handles(sid) == [key]
            # Bash commands normalize whitespace; non-handle args disable caching.
            assert dr._handle_key("Bash", {"command": "git  status\n"}) == "Bash:git status"
            assert dr._handle_key("Grep", {"pattern": "x"}) == ""
        finally:
            dr._STATE_DIR = orig


def _run_main(hook_input: dict, recall_result, td: str) -> tuple[int, str, list[str]]:
    """Drive main() end-to-end with _recall stubbed; returns (rc, stdout, cache)."""
    import io
    import contextlib
    orig_recall, orig_stdin, orig_dir = dr._recall, sys.stdin, dr._STATE_DIR
    dr._STATE_DIR = Path(td)

    async def fake_recall(*a, **kw):
        return recall_result

    dr._recall = fake_recall
    sys.stdin = io.StringIO(json.dumps(hook_input))
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = dr.main()
        return rc, buf.getvalue(), dr._load_handles(hook_input.get("session_id", ""))
    finally:
        dr._recall, sys.stdin, dr._STATE_DIR = orig_recall, orig_stdin, orig_dir


def test_failed_recall_does_not_poison_the_handle_cache():
    """A recall FAILURE must stay distinguishable from an empty result.

    ``_recall`` returns None on server error / unparseable payload and [] when
    the server genuinely matched nothing. Caching a failure would suppress that
    file/command for the whole session over a transient blip.
    """
    import tempfile
    hook = {"tool_name": "Edit", "tool_input": {"file_path": "app/main.py"},
            "cwd": "/tmp", "session_id": "cache-test"}
    with tempfile.TemporaryDirectory() as td:
        rc, out, cache = _run_main(hook, None, td)  # failure
        assert rc == 0 and out == "" and cache == []
    with tempfile.TemporaryDirectory() as td:
        rc, out, cache = _run_main(hook, [], td)  # genuine empty → cache it
        assert rc == 0 and out == "" and cache == ["Edit:app/main.py"]


def test_hit_injects_and_caches_the_handle():
    import tempfile
    hook = {"tool_name": "Edit", "tool_input": {"file_path": "app/main.py"},
            "cwd": "/tmp", "session_id": "hit-test"}
    d = _mk("z", ["app/main.py"])
    with tempfile.TemporaryDirectory() as td:
        rc, out, cache = _run_main(hook, [d], td)
        assert rc == 0 and cache == ["Edit:app/main.py"]
        payload = json.loads(out)["hookSpecificOutput"]
        assert payload["hookEventName"] == "PreToolUse"
        assert "fired on: app/main.py" in payload["additionalContext"]


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if fails else 0)
