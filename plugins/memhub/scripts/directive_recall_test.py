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


def test_no_trigger_branch_clears_both_match_fields():
    """A dict reused across filter passes must not keep a stale `_match_len`,
    which would misorder it against its unmatched peers in `_rank`."""
    d = _mk("a", ["swerex/deployment/modal.py"])
    dr._precision_filter([d], {"command": "vim swerex/deployment/modal.py"}, "")
    assert d["_match_len"] > 0
    d["triggers"] = None  # now unverifiable
    dr._precision_filter([d], {"command": "vim other.py"}, "")
    assert "_match" not in d and "_match_len" not in d


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
            key = dr._handle_key("Edit", {"file_path": "app/main.py"}, "/w/xmem")
            assert key.startswith("/w/xmem:Edit:app/main.py:")
            assert dr._load_handles(sid) == []
            dr._save_handles(sid, [key])
            assert dr._load_handles(sid) == [key]
            # Bash commands normalize whitespace; non-handle args disable caching.
            assert dr._handle_key("Bash", {"command": "git  status\n"}, "/w") \
                == dr._handle_key("Bash", {"command": "git status"}, "/w")
            assert dr._handle_key("Grep", {"pattern": "x"}, "/w") == ""
            # Long commands differing only past the readable head must NOT
            # collide — a false cache hit silently skips recall on the second.
            long_a = "uv run python x.py " + "a" * 400 + " --flag-one"
            long_b = "uv run python x.py " + "a" * 400 + " --flag-two"
            assert (dr._handle_key("Bash", {"command": long_a}, "/w")
                    != dr._handle_key("Bash", {"command": long_b}, "/w"))
            # Two WORKTREES of one repo share a remote basename but not a cwd:
            # the same relative path in each must not share a cache slot.
            assert (dr._handle_key("Edit", {"file_path": "app/main.py"}, "/w/xmem")
                    != dr._handle_key("Edit", {"file_path": "app/main.py"}, "/w/xmem-wt2"))
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
    expected = dr._handle_key("Edit", {"file_path": "app/main.py"}, "/tmp")
    with tempfile.TemporaryDirectory() as td:
        rc, out, cache = _run_main(hook, None, td)  # failure
        assert rc == 0 and out == "" and cache == []
    with tempfile.TemporaryDirectory() as td:
        rc, out, cache = _run_main(hook, [], td)  # genuine empty → cache it
        assert rc == 0 and out == "" and cache == [expected]


def test_cache_hit_costs_no_recall_and_no_git_subprocess():
    """A cached handle must short-circuit before ANY expensive work.

    `_repo_name` shells out to git; if it (or the recall) runs on a cache hit,
    the cache stops being the latency win it exists to be.
    """
    import io
    import tempfile
    hook = {"tool_name": "Edit", "tool_input": {"file_path": "app/main.py"},
            "cwd": "/tmp", "session_id": "hit-free"}
    key = dr._handle_key("Edit", {"file_path": "app/main.py"}, "/tmp")

    def _boom(*a, **kw):
        raise AssertionError("ran on a cache hit")

    git_calls = []
    saved = (dr._repo_name, dr._recall, sys.stdin, dr._STATE_DIR)
    try:
        with tempfile.TemporaryDirectory() as td:
            dr._STATE_DIR = Path(td)
            dr._save_handles("hit-free", [key])
            dr._repo_name = lambda cwd: git_calls.append(cwd) or "repo"
            dr._recall = _boom
            sys.stdin = io.StringIO(json.dumps(hook))
            assert dr.main() == 0
        assert git_calls == [], f"_repo_name spawned git on a cache hit: {git_calls}"
    finally:
        dr._repo_name, dr._recall, sys.stdin, dr._STATE_DIR = saved


def test_render_crash_is_contained_and_still_caches():
    """A post-recall crash must neither escape nor un-cache the handle.

    The emit is best-effort like the rest of the hook: it logs and moves on. The
    handle is still recorded, because a DETERMINISTIC emit failure (broken
    stdout, an unstringifiable payload) would otherwise re-buy a ~2.5s recall on
    every later touch — the latency cliff the cache exists to prevent. Only a
    failed RECALL stays uncached; that case is covered above.
    """
    import io
    import tempfile
    hook = {"tool_name": "Edit", "tool_input": {"file_path": "app/main.py"},
            "cwd": "/tmp", "session_id": "render-boom"}
    saved = (dr._render, dr._recall, sys.stdin, dr._STATE_DIR)

    async def fake_recall(*a, **kw):
        return [_mk("a", ["app/main.py"])]

    def _boom(_items):
        raise RuntimeError("render exploded")

    try:
        with tempfile.TemporaryDirectory() as td:
            dr._STATE_DIR = Path(td)
            dr._recall, dr._render = fake_recall, _boom
            sys.stdin = io.StringIO(json.dumps(hook))
            assert dr.main() == 0  # contained: still fails open, no traceback
            expected = dr._handle_key("Edit", {"file_path": "app/main.py"}, "/tmp")
            # …and recorded, so a deterministic crash can't re-buy the recall.
            assert dr._load_handles("render-boom") == [expected]
    finally:
        dr._render, dr._recall, sys.stdin, dr._STATE_DIR = saved


def test_empty_shapes_are_answers_not_failures():
    """Only a MISSING payload is a failure; an odd empty shape is an answer.

    A server that spells "nothing matched" as `{"items": null}` (or omits the
    key) must still cache the handle — classifying it as failure would re-buy
    a ~2s recall on every later touch for the whole session.
    """
    class _Res:
        content = []

        def __init__(self, sc, is_error=False):
            self.structuredContent = sc
            self.isError = is_error

    parse = dr._parse_recall_result
    assert parse(_Res({"items": [{"id": "a"}]})) == [{"id": "a"}]
    assert parse(_Res({"items": []})) == []
    assert parse(_Res({"items": None})) == []              # odd empty → answer
    assert parse(_Res({"count": 0})) == []                 # key absent → answer
    assert parse(_Res({"result": {"items": []}})) == []    # FastMCP wrap
    assert parse(_Res(None)) is None                       # no payload → failure
    assert parse(_Res({"items": []}, is_error=True)) is None  # isError → failure


def test_hit_injects_and_caches_the_handle():
    import tempfile
    hook = {"tool_name": "Edit", "tool_input": {"file_path": "app/main.py"},
            "cwd": "/tmp", "session_id": "hit-test"}
    d = _mk("z", ["app/main.py"])
    expected = dr._handle_key("Edit", {"file_path": "app/main.py"}, "/tmp")
    with tempfile.TemporaryDirectory() as td:
        rc, out, cache = _run_main(hook, [d], td)
        assert rc == 0 and cache == [expected]
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
