"""Self-test for the SessionStart repo-brain brief.

Covers the properties that decide whether this hook is safe to run before every
session's first prompt:

* ``brief`` makes NO network call and imports no transport module — it runs on
  the synchronous SessionStart path, where every millisecond is one the user
  waits;
* an unonboarded repo (no cached room) is SILENT, because a hook that nags in
  every checkout that is not the user's own is a hook they turn off;
* the agent-facing ``additionalContext`` is emitted every session, while the
  user-facing ``systemMessage`` fires only when the brain CHANGES — the
  every-session-noise failure ``capture_health`` exists to avoid;
* the brief tells the truth when per-turn capture is switched off, rather than
  claiming writes land in a brain they do not reach;
* a cached overview is injected and clipped, so a long digest cannot become a
  silent per-session token tax;
* ``refresh`` is throttled and never raises, whatever the cache contains;
* the map is drawn from the Index when the digest has one, else from the
  footer counts; the prose clip is short;
* apply / recall pointers come from a cache a detached child refreshes, are
  rendered once per session (served ids are shared with directive_recall),
  and are the first thing cut under the budget — never the map;
* the prompt hook is silent and free on a prompt with no identifier, fires
  one recall otherwise, and delivers a pending pointer cache exactly once.

Run: python3 tests/brain_brief_test.py  (from the repo root; stdlib only).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Redirect HOME before importing so the module's import-time CACHE_DIR resolves
# inside the sandbox and no test can read or write the real ~/.config. Both
# spellings: POSIX expanduser reads HOME; Windows reads USERPROFILE and never
# consults HOME.
_TMP_HOME = tempfile.mkdtemp(prefix="brain-brief-test-")
os.environ["HOME"] = _TMP_HOME
os.environ["USERPROFILE"] = _TMP_HOME
os.environ.pop("MEMHUB_STATE_DIR", None)
os.environ.pop("MEMHUB_TURN_FLUSH", None)
os.environ.pop("MEMHUB_MCP_BASE_URL", None)

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins" / "memhub" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import brain_brief  # noqa: E402
import room_map  # noqa: E402

BRAIN = "4e95c672-d00c-4cfd-8b7a-c2bd384fe53f"
OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {label}")
    if not cond:
        _failures.append(label)


def _run(cmd: str, payload: dict) -> dict:
    """Invoke the script the way the hook does, and parse what it printed."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "brain_brief.py"), cmd],
        input=json.dumps(payload), capture_output=True, text=True,
        env={**os.environ, "HOME": _TMP_HOME, "USERPROFILE": _TMP_HOME},
    )
    check(f"{cmd}: exit 0", proc.returncode == 0)
    out = proc.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        check(f"{cmd}: stdout is JSON", False)
        return {}


def _stub_room(monkey: dict | None) -> None:
    """Pin what `read_room` returns — the tests are about the brief, not about
    room resolution, which room_map_test.py already covers."""
    brain_brief.room_map.read_room = lambda cwd=None, env=None: monkey  # type: ignore[assignment]
    brain_brief.room_map.current_env = lambda: "staging"  # type: ignore[assignment]


def _ctx(out: dict) -> str:
    return str(out.get("hookSpecificOutput", {}).get("additionalContext") or "")


# ── silence where silence is correct ───────────────────────────────────────
_stub_room(None)
out = brain_brief.cmd_brief({"cwd": "/tmp/nowhere"})
check("unonboarded repo prints nothing", out == 0)

# ── the brief itself ───────────────────────────────────────────────────────
room = {"brain_id": BRAIN, "name": "Repo: XTraceAI/agent-plugins"}
_stub_room(room)

import io  # noqa: E402
import contextlib  # noqa: E402


def _brief(payload: dict | None = None) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        brain_brief.cmd_brief(payload or {"cwd": "/repo"})
    raw = buf.getvalue().strip()
    return json.loads(raw) if raw else {}

first = _brief()
check("names the brain in agent context", BRAIN in _ctx(first))
check("names the room in agent context",
      "Repo: XTraceAI/agent-plugins" in _ctx(first))
check("states it is the default target", "DEFAULT target" in _ctx(first))
check("first resolution reaches the USER", "systemMessage" in first)

second = _brief()
check("agent context is emitted every session", BRAIN in _ctx(second))
check("unchanged brain does NOT re-notify the user",
      "systemMessage" not in second)

_stub_room({"brain_id": OTHER, "name": "Repo: XTraceAI/other"})
changed = _brief()
check("a CHANGED brain notifies the user again", "systemMessage" in changed)

# ── truthfulness about writes ──────────────────────────────────────────────
_stub_room(room)
os.environ["MEMHUB_TURN_FLUSH"] = "0"
off = _brief()
check("says so when capture is switched off", "capture is OFF" in _ctx(off))
os.environ.pop("MEMHUB_TURN_FLUSH")
on = _brief()
check("says sessions are captured when it is on",
      "captured into it automatically" in _ctx(on))

# ── the overview: injected, and clipped ────────────────────────────────────
cache = brain_brief._cache_path("staging", BRAIN)
cache.parent.mkdir(parents=True, exist_ok=True)
cache.write_text(json.dumps({
    "overview": "The repo is a Claude Code plugin marketplace.",
    "refreshed_at": time.time(),
}), encoding="utf-8")
with_ov = _brief()
check("a cached overview is injected",
      "Claude Code plugin marketplace" in _ctx(with_ov))

cache.write_text(json.dumps({
    "overview": "x" * (brain_brief._MAX_OVERVIEW_CHARS + 500),
    "refreshed_at": time.time(),
}), encoding="utf-8")
clipped = _brief()
check("a long overview is clipped, not injected whole",
      "truncated" in _ctx(clipped)
      and len(_ctx(clipped)) < brain_brief._MAX_OVERVIEW_CHARS + 1200)

cache.unlink()
missing = _brief()
check("no cached overview points at the tool instead of asserting emptiness",
      "get_brain_overview" in _ctx(missing))

# ── refresh: throttled, and never fatal ────────────────────────────────────
cache.write_text(json.dumps({"overview": "cached", "refreshed_at": time.time()}),
                 encoding="utf-8")
check("fresh cache is left alone", brain_brief._is_fresh(cache))
cache.write_text(json.dumps({"overview": "old", "refreshed_at": 1.0}),
                 encoding="utf-8")
check("a stale cache is refetched", not brain_brief._is_fresh(cache))
cache.write_text("not json at all", encoding="utf-8")
check("a corrupt cache reads as stale, not as a crash",
      not brain_brief._is_fresh(cache))
check("a corrupt cache does not break the brief", "systemMessage" in _brief()
      or BRAIN in _ctx(_brief()))

_stub_room(None)
check("refresh with no room is a no-op", brain_brief.cmd_refresh({}) == 0)

# An unwritable cache would be permanently stale, and Stop fires every turn —
# so without this the 6-hourly digest fetch becomes a per-turn network call
# forever. `brief` reads only from the cache, so a fetch that cannot be stored
# buys nothing: the honest response is not to make it.
_stub_room(room)
_real_dir = brain_brief.CACHE_DIR
# A cache dir whose PARENT is a regular file is unwritable on every platform.
# ("/proc/…" is only guaranteed unwritable on Linux; on Windows that path
# would simply be created under the drive root.)
_blocker = Path(_TMP_HOME) / "cache-blocker"
_blocker.write_text("", encoding="utf-8")
brain_brief.CACHE_DIR = _blocker / "memhub"
check("an unwritable cache is detected", not brain_brief._cache_is_writable())


def _boom(*a, **k):  # pragma: no cover - must never be reached
    raise AssertionError("refresh attempted a network call it could not persist")


_saved_extract = brain_brief._extract_overview
brain_brief._extract_overview = _boom
check("unwritable cache skips the network call entirely",
      brain_brief.cmd_refresh({"cwd": "/repo"}) == 0)
brain_brief._extract_overview = _saved_extract
brain_brief.CACHE_DIR = _real_dir
check("a writable cache still refreshes", brain_brief._cache_is_writable())


# ── the result shape: the envelope is UNWRAPPED, not cached whole ──────────
class _Block:
    def __init__(self, text):
        self.text = text


class _Res:
    def __init__(self, content=(), structured=None):
        self.content = list(content)
        self.structured = structured
        self.is_error = False


DIGEST = "# Repo: XTraceAI/x — Overview\nThis brain is dominated by …"

# What staging actually returns: a text block holding a JSON envelope. Caching
# it verbatim looked like it worked — a plausible-looking 3.5KB cache appeared —
# while injecting JSON punctuation as "what this brain knows".
envelope = _Res([_Block(json.dumps({"agent_brain_id": BRAIN,
                                    "overview": DIGEST}))])
check("a JSON envelope is unwrapped to the digest",
      brain_brief._extract_overview(envelope) == DIGEST)
check("structuredContent wins when present",
      brain_brief._extract_overview(
          _Res([_Block("ignored")], {"overview": DIGEST})) == DIGEST)
check("a bare text block is taken as the digest",
      brain_brief._extract_overview(_Res([_Block(DIGEST)])) == DIGEST)
check("an uncompiled digest (null) reads as a cache miss",
      brain_brief._extract_overview(
          _Res([_Block(json.dumps({"overview": None}))])) == "")
check("no content at all is a cache miss", brain_brief._extract_overview(_Res()) == "")

# ── the SessionStart budget: no network module on the brief path ───────────
probe = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r);\n"
     "import json, io, contextlib\n"
     "import brain_brief\n"
     "brain_brief.room_map.read_room = lambda cwd=None, env=None: None\n"
     "buf = io.StringIO()\n"
     "with contextlib.redirect_stdout(buf): brain_brief.cmd_brief({})\n"
     "print(json.dumps(sorted(m for m in sys.modules\n"
     "      if m in ('mcp_http', '_memhub_auth', 'urllib.request'))))"
     % str(SCRIPTS)],
    capture_output=True, text=True,
    env={**os.environ, "HOME": _TMP_HOME, "USERPROFILE": _TMP_HOME},
)
loaded = json.loads(probe.stdout.strip() or "[]") if probe.returncode == 0 else ["?"]
check("brief imports no transport/auth module", loaded == [])

# ── the map: Index lines, footer counts, prose clip ────────────────────────
import copy  # noqa: E402

import brief_budget  # noqa: E402
import brief_identifiers  # noqa: E402
import served_state  # noqa: E402

FOOTERED = ("# Repo: X — Overview\nProse here.\n\n## Key topics\n- a\n\n---\n"
            "_2553 facts · 1058 episodes · 51 artifacts (excl. this manifest)._")
prose, idx, counts = brain_brief._split_overview(FOOTERED)
check("footer counts are parsed", counts == ("2,553", "1,058", "51"))
check("no Index section means no index lines", idx == [])
check("the digest's own H1 is dropped from the prose", "# Repo" not in prose)
check("the footer is not prose", "facts ·" not in prose and "Prose here." in prose)
check("a big brain's 5000+ counts survive",
      brain_brief._split_overview("x\n_5000+ facts · 5000+ episodes · 967 artifacts._")[2]
      == ("5,000+", "5,000+", "967"))

INDEXED = ("# T\nProse\n\n## Index\nbrain\n├── Facts 1\n├── Episodes 2\n└── Artifacts 3\n"
           "    ├── Specs 4\n    └── Docs 5\n    extra 6\n\n## Gotchas\n- g\n\n---\n"
           "_1 facts · 2 episodes · 3 artifacts._")
prose, idx, counts = brain_brief._split_overview(INDEXED)
check("an Index section is lifted out whole", idx[0] == "brain" and len(idx) == 7)
check("the prose keeps what follows the Index", "Gotchas" in prose and "Specs" not in prose)
m = "\n".join(brain_brief._render_map(BRAIN, INDEXED))
check("the map is the five-line top of the Index",
      "    ├── Specs 4" in m and "Docs 5" not in m and "(full Index" in m)
check("with an Index the counts tree is not drawn too", "Facts      " not in m)
m = "\n".join(brain_brief._render_map(BRAIN, FOOTERED))
check("without an Index the map is drawn from the footer counts",
      "├── Facts        2,553" in m and 'memory_type="episodes")' in m)
check("no digest at all points at get_brain_overview",
      "No compiled overview cached yet" in "\n".join(brain_brief._render_map(BRAIN, "")))
m = "\n".join(brain_brief._render_map(BRAIN, "just prose, no footer"))
check("prose without a footer is still shown, without the no-overview line",
      "just prose" in m and "No compiled overview" not in m)
m = "\n".join(brain_brief._render_map(BRAIN, "P" * 5000))
check("the prose clip is 600 chars", "truncated" in m
      and len(m) < brain_brief._MAX_OVERVIEW_CHARS + 200)

# ── assembly under budget: recall goes first, then apply, never the map ────
HEAD = ["H"]
MAP = ["## Map", "m1", "m2"]
APPLY = ["## Apply"] + [f"• a{i}" for i in range(5)]
RECALL = ["## Recall"] + [f"• r{i}" for i in range(5)] + ["(open one: …)"]
full = brain_brief._assemble(HEAD, MAP, copy.copy(APPLY), copy.copy(RECALL), 10_000)
check("under budget nothing is cut", "• r4" in full and brain_brief._TRIMMED_FOOTER not in full)
just_apply = len("\n".join(HEAD + [""] + MAP + [""] + APPLY))
tight = brain_brief._assemble(HEAD, MAP, copy.copy(APPLY), copy.copy(RECALL), just_apply + 40)
check("recall pointers are dropped before any apply pointer",
      "• a4" in tight and "• r" not in tight and "## Recall" not in tight)
check("a cut brief says so", tight.endswith(brain_brief._TRIMMED_FOOTER))
tighter = brain_brief._assemble(HEAD, MAP, copy.copy(APPLY), copy.copy(RECALL), 10)
check("the map is never cut", "m2" in tighter and "• a" not in tighter
      and tighter.endswith(brain_brief._TRIMMED_FOOTER))

# ── brief + pointer cache: rendered, served, not repeated, refreshed ───────
_stub_room(room)
FAKE_GIT = {"root": "/repo", "branch": "b", "head": "h1", "base": "origin/main",
            "paths": ["x.py"], "refs": []}
brain_brief.brief_identifiers.from_git = lambda cwd: dict(FAKE_GIT)  # type: ignore[assignment]
spawned: list[str] = []
_real_spawn = brain_brief._spawn_pointers
brain_brief._spawn_pointers = lambda cwd: spawned.append(cwd)  # type: ignore[assignment]

ctx = _ctx(_brief({"cwd": "/repo", "session_id": "s1"}))
check("no pointer cache: no Apply/Recall, but the worker is spawned",
      "## Apply" not in ctx and spawned == ["/repo"])
PCACHE = brain_brief._pointers_path("staging", BRAIN, "/repo")
POINTERS = {
    "computed_at": time.time(), "head": "h1", "branch": "b", "base": "origin/main",
    "apply": [{"id": "d1", "type": "lesson", "text": "lesson one", "as_of": "2026-09-01", "match": "x.py"},
              {"id": "d2", "type": "procedure", "text": "proc two", "as_of": "", "match": ""}],
    "recall": [{"id": "e1", "type": "episode", "text": "episode one", "match": "PR #1"},
               {"id": "a1", "type": "artifact", "text": "artifact one", "match": "x.py"}],
}
brain_brief._write_json(PCACHE, POINTERS)
spawned.clear()
ctx = _ctx(_brief({"cwd": "/repo", "session_id": "s1"}))
check("a fresh cache renders Apply", "## Apply" in ctx and "• [lesson] lesson one — 2026-09-01 · on x.py [d1]" in ctx)
check("a fresh cache renders Recall & Consult", "## Recall & Consult" in ctx and "[artifact] artifact one" in ctx)
check("every rendered id joins the served list",
      set(served_state.load_ids(served_state.STATE_DIR, "s1")) == {"d1", "d2", "e1", "a1"})
check("a fresh cache on the same HEAD is not recomputed", spawned == [])
ctx = _ctx(_brief({"cwd": "/repo", "session_id": "s1"}))
check("served ids are not rendered again", "## Apply" not in ctx and "## Recall" not in ctx)
ctx = _ctx(_brief({"cwd": "/repo", "session_id": "s2"}))
check("another session still sees them", "[d1]" in ctx and "[a1]" in ctx)
brain_brief._write_json(PCACHE, {**POINTERS, "head": "h0"})
spawned.clear(); _brief({"cwd": "/repo", "session_id": "s3"})
check("HEAD moved: the worker is spawned", spawned == ["/repo"])
brain_brief._write_json(PCACHE, {**POINTERS, "branch": "other"})
spawned.clear(); ctx = _ctx(_brief({"cwd": "/repo", "session_id": "s4"}))
check("a cache computed for another branch is neither rendered nor kept",
      "## Apply" not in ctx and spawned == ["/repo"])
brain_brief._write_json(PCACHE, {**POINTERS, "computed_at": time.time() - 2 * 86400})
spawned.clear(); ctx = _ctx(_brief({"cwd": "/repo", "session_id": "s5"}))
check("a stale cache is neither rendered nor trusted", "## Apply" not in ctx and spawned == ["/repo"])
brain_brief._write_json(PCACHE, POINTERS)

# the real spawner: disabled by env, else a detached `pointers` child
brain_brief._spawn_pointers = _real_spawn
popen_calls: list[list[str]] = []
_real_popen = brain_brief.subprocess.Popen
brain_brief.subprocess.Popen = lambda argv, **kw: popen_calls.append(argv)  # type: ignore[assignment]
os.environ["MEMHUB_BRIEF_POINTERS"] = "0"
brain_brief._spawn_pointers("/repo")
check("MEMHUB_BRIEF_POINTERS=0 spawns nothing", popen_calls == [])
os.environ.pop("MEMHUB_BRIEF_POINTERS")
brain_brief._spawn_pointers("/repo")
check("otherwise a detached `pointers` child is started, with no session in its argv",
      len(popen_calls) == 1 and popen_calls[0][2:] == ["pointers", "/repo"])
brain_brief.subprocess.Popen = _real_popen
brain_brief._spawn_pointers = lambda cwd: spawned.append(cwd)  # type: ignore[assignment]

# ── the worker builds a SHARED cache: no session filters baked in ──────────
import types  # noqa: E402
recorded: dict = {}
_fake_auth = types.ModuleType("_memhub_auth")
_fake_auth.resolve_bearer = lambda *a, **k: ("https://x", "bearer")  # type: ignore[attr-defined]
sys.modules["_memhub_auth"] = _fake_auth


def _fake_recall_items(url, bearer, brain_id, repo, entities, served, session_id, limit, timeout):
    recorded.update(served=list(served), session_id=session_id, limit=limit)
    return [{"id": f"w{i}", "type": "lesson", "content": f"lesson {i}", "triggers": ["x.py"]}
            for i in range(limit)]


_saved = (brain_brief._recall_items, brain_brief._search_items, brain_brief._repo_name)
brain_brief._recall_items = _fake_recall_items  # type: ignore[assignment]
brain_brief._search_items = lambda *a, **k: []  # type: ignore[assignment]
brain_brief._repo_name = lambda root: "r"  # type: ignore[assignment]
served_state.add_ids(served_state.STATE_DIR, "s1", ["w0"])
check("worker exits clean", brain_brief.cmd_pointers("/repo") == 0)
check("the worker sends no served list and no session id",
      recorded.get("served") == [] and recorded.get("session_id") == "")
wcache = brain_brief._read_json(PCACHE)
check("the cache holds twice the cap so the per-session filter still fills a block",
      recorded.get("limit") == 2 * brain_brief._MAX_APPLY
      and len(wcache.get("apply") or []) == 2 * brain_brief._MAX_APPLY)
ctx = _ctx(_brief({"cwd": "/repo", "session_id": "s1"}))
check("a session's served ids are filtered at render time, not in the cache",
      "[w0]" not in ctx and ctx.count("\n• ") == brain_brief._MAX_APPLY)
brain_brief._recall_items, brain_brief._search_items, brain_brief._repo_name = _saved
del sys.modules["_memhub_auth"]
brain_brief._write_json(PCACHE, POINTERS)

# ── the budget: one env var, 2:1, trimmed footer ───────────────────────────
os.environ["MEMHUB_BRIEF_TOKEN_BUDGET"] = "300"
check("budget is tokens × 4 chars, split 2:1",
      (brief_budget.total_chars(), brief_budget.brief_chars(), brief_budget.rulebook_chars())
      == (1200, 800, 400))
ctx = _ctx(_brief({"cwd": "/repo", "session_id": "s6"}))
check("over budget the brief is trimmed, map intact",
      ctx.endswith(brain_brief._TRIMMED_FOOTER) and "## Map" in ctx and BRAIN in ctx)
check("only pointers that survived the trim are marked served",
      set(served_state.load_ids(served_state.STATE_DIR, "s6")) == set(brain_brief._ids_in(ctx))
      and len(brain_brief._ids_in(ctx)) < 4)
os.environ.pop("MEMHUB_BRIEF_TOKEN_BUDGET")
os.environ["MEMHUB_BRIEF_TOKEN_BUDGET"] = "garbage"
check("an unparsable budget falls back to the default",
      brief_budget.total_tokens() == brief_budget.DEFAULT_TOKENS)
os.environ.pop("MEMHUB_BRIEF_TOKEN_BUDGET")

# ── the prompt hook ────────────────────────────────────────────────────────
brain_brief.room_map.repo_root = lambda cwd=None: Path("/repo")  # type: ignore[assignment]
brain_brief.brief_identifiers.repo_files = lambda root: {"room_map", "room_map.py"}  # type: ignore[assignment]
brain_brief.brief_identifiers.current_branch = lambda root: "b"  # type: ignore[assignment]
recall_calls: list[dict] = []
PROMPT_ITEMS = [
    {"id": "p1", "type": "lesson", "content": "lesson about room_map", "as_of": "2026-09-02",
     "triggers": ["room_map.read_room"]},
    {"id": "p2", "type": "lesson", "content": "second", "triggers": ["other"]},
    {"id": "p3", "type": "procedure", "content": "third", "triggers": []},
    {"id": "p4", "type": "lesson", "content": "fourth", "triggers": []},
]


def _fake_prompt_recall(brain_id, root, entities, served, session_id):
    recall_calls.append({"entities": entities, "served": list(served)})
    return [d for d in PROMPT_ITEMS if d["id"] not in served][:brain_brief._MAX_PROMPT]


brain_brief._prompt_recall = _fake_prompt_recall  # type: ignore[assignment]


def _prompt(payload: dict) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        brain_brief.cmd_prompt(payload)
    raw = buf.getvalue().strip()
    return json.loads(raw) if raw else {}


# s1 has already seen the cache, so only the prompt's own identifiers matter
out = _prompt({"cwd": "/repo", "session_id": "s1", "prompt": "summarise what we did yesterday"})
check("a prompt with no identifier makes no recall and prints nothing",
      out == {} and recall_calls == [])
out = _prompt({"cwd": "/repo", "session_id": "s1",
               "prompt": "why does room_map.read_room return None for PR #182?"})
pctx = _ctx(out)
check("identifiers in the prompt fire one recall on them",
      len(recall_calls) == 1 and {"room_map.read_room", "PR #182"} <= set(recall_calls[0]["entities"]))
check("the hook answers as UserPromptSubmit",
      out.get("hookSpecificOutput", {}).get("hookEventName") == "UserPromptSubmit")
check("at most three pointers", pctx.count("\n• ") + pctx.startswith("• ") <= 3 and "[p4]" not in pctx)
check("the matched trigger is shown", "on room_map.read_room [p1]" in pctx)
check("prompt pointers join the served list",
      {"p1", "p2", "p3"} <= set(served_state.load_ids(served_state.STATE_DIR, "s1")))
out = _prompt({"cwd": "/repo", "session_id": "s1", "prompt": "again: room_map.read_room"})
check("served pointers are not repeated: only the unseen one remains",
      "[p4]" in _ctx(out) and "[p1]" not in _ctx(out))
out = _prompt({"cwd": "/repo", "session_id": "s1", "prompt": "again: room_map.read_room"})
check("and once everything is served the hook is silent", out == {})

LONG_ITEMS = [{"id": f"L{i}", "type": "lesson", "content": "w" * 400, "triggers": []} for i in range(3)]
brain_brief._prompt_recall = lambda *a, **k: LONG_ITEMS  # type: ignore[assignment]
pctx = _ctx(_prompt({"cwd": "/repo", "session_id": "s7", "prompt": "see room_map.read_room"}))
check("the prompt block stays under 600 chars, cut from the end",
      0 < len(pctx) <= brain_brief._PROMPT_MAX_CHARS and "[L0]" in pctx and "[L2]" not in pctx)
brain_brief._prompt_recall = _fake_prompt_recall  # type: ignore[assignment]

# the pointer cache the brief could not deliver arrives on the first prompt — once
out = _prompt({"cwd": "/repo", "session_id": "s8", "prompt": "no identifiers here"})
check("a pending pointer cache is delivered by the prompt hook",
      "## Apply" in _ctx(out) and "[a1]" in _ctx(out))
out = _prompt({"cwd": "/repo", "session_id": "s8", "prompt": "no identifiers here"})
check("…and only once per refresh", out == {})
brain_brief._write_json(PCACHE, {**POINTERS, "computed_at": time.time() + 1,
                                 "apply": [{"id": "d9", "type": "lesson", "text": "new", "as_of": "", "match": ""}],
                                 "recall": []})
out = _prompt({"cwd": "/repo", "session_id": "s8", "prompt": "no identifiers here"})
check("a refreshed cache is delivered again, minus served ids",
      "[d9]" in _ctx(out) and "[d1]" not in _ctx(out))
brain_brief._write_json(PCACHE, {**POINTERS, "computed_at": time.time() + 2, "branch": "other"})
spawned.clear()
out = _prompt({"cwd": "/repo", "session_id": "s9", "prompt": "no identifiers here"})
check("a pending cache for another branch is not delivered by the prompt hook, and is refreshed",
      out == {} and spawned == ["/repo"]
      and served_state.load_ids(served_state.STATE_DIR, "s9") == [])
brain_brief._write_json(PCACHE, POINTERS)

# a budget cut at SessionStart leaves the marker alone, so the prompt delivers the rest
os.environ["MEMHUB_BRIEF_TOKEN_BUDGET"] = "300"
bctx = _ctx(_brief({"cwd": "/repo", "session_id": "s10"}))
shown_at_start = set(brain_brief._ids_in(bctx))
check("the trimmed brief showed some but not all cached pointers",
      bctx.endswith(brain_brief._TRIMMED_FOOTER) and 0 < len(shown_at_start) < 4)
check("a partial delivery does not advance the cache marker",
      served_state.load_marker(served_state.STATE_DIR, "s10", "brief") == {})
os.environ.pop("MEMHUB_BRIEF_TOKEN_BUDGET")
out = _prompt({"cwd": "/repo", "session_id": "s10", "prompt": "no identifiers here"})
rest = set(brain_brief._ids_in(_ctx(out)))
check("the first prompt delivers exactly the pointers the budget cut",
      rest and rest.isdisjoint(shown_at_start) and shown_at_start | rest == {"d1", "d2", "e1", "a1"})
check("…and then the marker is advanced",
      served_state.load_marker(served_state.STATE_DIR, "s10", "brief").get("computed_at")
      == POINTERS["computed_at"])
out = _prompt({"cwd": "/repo", "session_id": "s10", "prompt": "no identifiers here"})
check("nothing is left to deliver after that", out == {})

# ── no network on the brief path — with a room AND a pointer cache present ─
probe = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r)\n"
     "import json, io, contextlib, time\n"
     "import brain_brief\n"
     "brain_brief.room_map.read_room = lambda cwd=None, env=None: %r\n"
     "brain_brief.room_map.current_env = lambda: 'staging'\n"
     "brain_brief.brief_identifiers.from_git = lambda cwd: %r\n"
     "buf = io.StringIO()\n"
     "with contextlib.redirect_stdout(buf): brain_brief.cmd_brief({'cwd': '/repo', 'session_id': 'probe'})\n"
     "ctx = json.loads(buf.getvalue())['hookSpecificOutput']['additionalContext']\n"
     "print(json.dumps([sorted(m for m in sys.modules if m in ('mcp_http', '_memhub_auth', 'urllib.request', 'http.client')), '## Apply' in ctx]))"
     % (str(SCRIPTS), room, FAKE_GIT)],
    capture_output=True, text=True,
    env={**os.environ, "HOME": _TMP_HOME, "USERPROFILE": _TMP_HOME, "MEMHUB_BRIEF_POINTERS": "0"},
)
loaded, rendered = json.loads(probe.stdout.strip() or '[["?"], false]') if probe.returncode == 0 else (["?"], False)
check("a full brief (map + cached pointers) loads no transport/auth module",
      loaded == [] and rendered)

brain_brief.brief_identifiers.from_git = brief_identifiers.from_git  # type: ignore[assignment]

# ── the CLI, as the hook actually invokes it ───────────────────────────────
cli = _run("brief", {"cwd": _TMP_HOME})
check("CLI on a non-repo dir stays quiet", cli == {})
check("CLI refresh on a non-repo dir exits clean",
      _run("refresh", {"cwd": _TMP_HOME}) == {})

print()
if _failures:
    print(f"{len(_failures)} FAILED: {', '.join(_failures)}")
    raise SystemExit(1)
print("all brain_brief checks passed")
