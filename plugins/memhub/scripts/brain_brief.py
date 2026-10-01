#!/usr/bin/env python3
"""Orient a session on the repo's agent brain — map, recall & consult.

**Why this exists.** The plugin routes every ARTIFACT write to the repo's brain
(``save_artifact`` and the ``.md`` auto-capture resolve the room from
``rooms.json``); sessions go to the author's personal memory. Nothing said so, and reads defaulted elsewhere, so the one
question that matters at the start of a session — *what does this project
already know that bears on what I am about to do?* — had no cheap answer.

This hook answers it in two checkpoints (navigation spec §4, "Session
start"), under one budget, keyed on **identifiers, never on similarity**:

1. **Map** — what the brain holds: the five-line top of the compiled
   overview's Index (counts + drill commands) and a short clip of its prose.
2. **Recall & Consult** — the brain's artifacts and your own sessions that
   *name* what this branch touches: its PR / ENG numbers and the basenames of
   the files in ``git diff --name-only origin/<default>`` ∪ the last 20
   commits' paths. Search is asked with the identifier strings, and a hit
   survives only if its title or abstract contains one of them as an exact
   token. Nothing is kept for being "about" the branch.

Team rules are not rendered here: ``rulebook_hook.py`` owns that channel.
Per-prompt semantic injection is deliberately absent: Tencent's teamai-cli
retired that channel as noisy and low-hit-rate (the repo brain holds the
research note).

**Four subcommands, because they have different constraints.**

``brief`` runs on ``SessionStart``, SYNCHRONOUS, blocking the first prompt.
It is stdlib-only and makes NO network call: the map comes from the overview
cache, recall from the pointer cache, and it spawns ``pointers`` in a
detached child to refresh that cache. Why a cache and not one live call: the
host drops a hook's whole output past its 5 s timeout, and recall is two
round trips (episode and artifact searches) that cannot be bounded to 2 s
from inside a stdlib process without also risking the map — the part that
must never be lost. The detached child has no deadline pressure; what it
writes is read by the first prompt's hook, so the cost is one turn of latency,
never a session.

``pointers`` is that child: git → identifiers → search → cache.

``prompt`` runs on ``UserPromptSubmit`` and makes no network call: it
delivers the pointer cache the brief could not (it landed after SessionStart
printed, or the budget cut part of it) — once per refresh. With nothing
pending it prints nothing.

``refresh`` runs on ``Stop`` (async): fetches ``get_brain_overview`` into the
overview cache, throttled to 6 h since the digest moves on the order of days.

**Nothing repeated.** Every id rendered — by any of these — joins the
session's served list and is filtered client-side from later renders.

**On speaking to the user.** ``systemMessage`` fires only when the resolved
brain CHANGES; the agent-facing ``additionalContext`` is emitted every session.

Run the self-test:  python3 tests/brain_brief_test.py  (from the repo root)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import atomic_write  # noqa: E402
import brief_budget  # noqa: E402
import brief_identifiers  # noqa: E402
import room_map  # noqa: E402
import served_state  # noqa: E402

#: Where the compiled overview is cached, keyed by backend AND brain so a prod
#: and a staging brain — different databases, non-interchangeable ids — can
#: never serve each other's digest.
CACHE_DIR = Path(
    os.environ.get("MEMHUB_STATE_DIR")
    or Path.home() / ".config" / "memhub-plugin"
) / "overview"

#: How stale a cached overview may be before ``refresh`` refetches it.
_MAX_AGE_S = 6 * 3600

#: The prose clip. The map (Index lines) carries the structure now; the prose
#: is a taste, with a pointer to the tool that returns the whole digest.
_MAX_OVERVIEW_CHARS = 600
_MAX_INDEX_LINES = 5

#: Pointer cache: rendered while younger than this and on the same branch;
#: re-computed when HEAD moved or it is older than the refresh interval.
_POINTERS_MAX_AGE_S = 24 * 3600
_POINTERS_REFRESH_S = 15 * 60

_MAX_RECALL = 5
_POINTER_TEXT_CHARS = 160
_SEARCH_TOP_K = 20
_TIMEOUT_S = 20.0           # detached worker / Stop refresh

_TRIMMED_FOOTER = "… trimmed to budget"
#: A rendered pointer ends in ``[id]``; the served list is read back off the
#: final text, so an id cut by the budget is never marked as shown.
_POINTER_ID_RE = re.compile(r"^• .*\[([^\[\]\n]+)\]$", re.M)

#: The manifest footer every compiled digest carries — ``_2553 facts · 1058
#: episodes · 51 artifacts (excl. this manifest)._``; a big brain says ``5000+``.
_FOOTER_RE = re.compile(
    r"_?\s*(\d[\d,]*\+?)\s+facts\s+·\s+(\d[\d,]*\+?)\s+episodes\s+·\s+(\d[\d,]*\+?)\s+artifacts[^\n]*"
)


def _cache_path(env: str, brain_id: str) -> Path:
    return CACHE_DIR / f"{env}-{brain_id}.json"


def _pointers_path(env: str, brain_id: str, root: str) -> Path:
    key = hashlib.sha256(str(root).encode("utf-8", "replace")).hexdigest()[:12]
    return CACHE_DIR / "pointers" / f"{env}-{brain_id}-{key}.json"


def _announced_path() -> Path:
    """Which brain we last told the USER about, per repo+backend."""
    return CACHE_DIR / "announced.json"


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> bool:
    """Best effort; ``True`` when the bytes actually landed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The shared publisher: pid-suffixed temp file, because Stop fires per
        # turn in EVERY session and parallel worktrees would race on one name.
        atomic_write.publish(path, json.dumps(data))
        return True
    except Exception:  # noqa: BLE001
        return False


def _cache_is_writable() -> bool:
    """Can we persist a refresh at all? A fetch whose result cannot be stored
    buys nothing, and an unwritable cache is permanently stale."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        probe = CACHE_DIR / ".write-probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


def _capture_is_off() -> bool:
    return (os.environ.get("MEMHUB_TURN_FLUSH") or "").strip() == "0"


def _cwd_from(payload: dict) -> str:
    cwd = str(payload.get("cwd") or "").strip()
    return cwd or os.getcwd()


def _session_id(payload: dict) -> str:
    return str(payload.get("session_id") or "").strip()


def _served(session_id: str) -> list[str]:
    return served_state.load_ids(served_state.STATE_DIR, session_id)


def _mark_served(session_id: str, ids: list[str]) -> None:
    if session_id and ids:
        served_state.add_ids(served_state.STATE_DIR, session_id, ids)


def _ids_in(context: str) -> list[str]:
    """The ids of the pointers that survived into ``context``."""
    return _POINTER_ID_RE.findall(context)


# ── the map ────────────────────────────────────────────────────────────────

def _count(raw: str) -> str:
    """``"2553"`` → ``"2,553"``, ``"5000+"`` → ``"5,000+"``."""
    digits = raw.replace(",", "").rstrip("+")
    return f"{int(digits):,}" + ("+" if raw.endswith("+") else "") if digits.isdigit() else raw


def _split_overview(text: str) -> tuple[str, list[str], tuple[str, str, str] | None]:
    """``(prose, index_lines, counts)`` out of a cached digest.

    ``index_lines`` is the top of an ``## Index`` section when the backend
    ships one (the Index PR renders it from rows); ``counts`` is parsed from
    the manifest footer every digest already carries, so the map can be drawn
    from what is cached today. The prose is what is left.
    """
    text = (text or "").strip()
    if not text:
        return "", [], None
    counts = None
    m = _FOOTER_RE.search(text)
    if m:
        counts = tuple(_count(g) for g in m.groups())  # type: ignore[assignment]
        text = (text[:m.start()] + text[m.end():]).rstrip()
    index_lines: list[str] = []
    im = re.search(r"^## Index[^\n]*\n", text, re.M)
    if im:
        rest = text[im.end():]
        nm = re.search(r"^## ", rest, re.M)
        body = rest[:nm.start()] if nm else rest
        index_lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
        text = (text[:im.start()] + (rest[nm.start():] if nm else "")).rstrip()
    text = re.sub(r"\n-{3,}\s*$", "", text).rstrip()
    text = re.sub(r"^# [^\n]*\n+", "", text)   # the head line already names the brain
    return text, index_lines, counts


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= _MAX_OVERVIEW_CHARS:
        return text
    return text[:_MAX_OVERVIEW_CHARS].rstrip() + (
        "\n… (truncated — call get_brain_overview for the full digest)"
    )


def _render_map(brain_id: str, overview: str) -> list[str]:
    prose, index_lines, counts = _split_overview(overview)
    lines = ["## Map — what this brain holds"]
    if index_lines:
        lines += index_lines[:_MAX_INDEX_LINES]
        if len(index_lines) > _MAX_INDEX_LINES:
            lines.append("… (full Index: get_brain_overview)")
    elif counts:
        # Only artifacts live in a brain now: sessions (episodes) are personal
        # and never searchable by agent_brain_id, and facts are not a kind
        # search_memory takes. The footer still carries all three counts.
        _facts, _episodes, artifacts = counts
        lines += [
            f"brain ({artifacts} artifacts)",
            f'└── Artifacts  {artifacts:>7}  → search_memory(query=…, '
            f'agent_brain_id="{brain_id[:8]}…", kind="artifact"); open one: read_memory(id)',
            "full digest: get_brain_overview",
        ]
    clipped = _clip(prose)
    if clipped:
        lines.append(clipped)
    if len(lines) == 1:
        lines.append(
            "No compiled overview cached yet — call `get_brain_overview` with "
            "that id if you need to know what it already holds."
        )
    return lines


# ── pointers ───────────────────────────────────────────────────────────────

def _first_line(text: str, limit: int = _POINTER_TEXT_CHARS) -> str:
    """One line of pointer text. An HTML deliverable (spec A0: the body is a
    file bundle) is named by its <title>, never by its doctype."""
    text = str(text or "")
    if text.lstrip()[:1] == "<":
        m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
        text = (m.group(1) if m else re.sub(r"<[^>]+>", " ", text)) + " (html)"
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _dedupe_text(items: list[dict], key: str = "text") -> list[dict]:
    """Drop later items whose text opens like an earlier one's. The server
    returns near-identical rows under different ids (a procedure captured
    twice, a brief saved per babysit pass); one pointer each is plenty."""
    seen: set[str] = set()
    out = []
    for d in items:
        k = " ".join(str(d.get(key) or "").casefold().split())[:50]
        if k and k in seen:
            continue
        seen.add(k)
        out.append(d)
    return out


def _pointer(item: dict, kind: str = "") -> str:
    text = _first_line(item.get("text") or item.get("content") or "")
    tail = []
    as_of = str(item.get("as_of") or "").strip()
    if as_of:
        tail.append(as_of)
    match = str(item.get("match") or "").strip()
    if match:
        tail.append(f"on {match}")
    suffix = f" — {' · '.join(tail)}" if tail else ""
    prefix = f"[{kind}] " if kind else ""
    return f"• {prefix}{text}{suffix} [{item.get('id', '')}]"


def _recall_lines(items: list[dict]) -> list[str]:
    if not items:
        return []
    return ["## Recall & Consult — episodes/artifacts naming this branch's identifiers"] + [
        _pointer(d, str(d.get("type") or "").lower()) for d in items
    ] + ["(open one: read_memory(id) — an artifact or a session)"]


def _assemble(head: list[str], map_lines: list[str], recall: list[str],
              limit: int) -> str:
    """Join the sections under ``limit`` chars. Recall pointers are dropped
    (from the end); the head and map never are. A heading left with nothing
    under it goes too, and a footer says so whenever anything was cut."""
    cut = False

    def render() -> str:
        parts = [*head]
        for section in (map_lines, recall):
            if section:
                parts.append("")
                parts.extend(section)
        if cut:
            parts.append(_TRIMMED_FOOTER)
        return "\n".join(parts)

    for section in (recall,):
        while section and len(render()) > limit:
            pointers = [i for i, ln in enumerate(section) if ln.startswith("• ")]
            if pointers:
                del section[pointers[-1]]
            else:
                section.clear()
            cut = True
        if section and not any(ln.startswith("• ") for ln in section):
            section.clear()   # a heading with no pointer under it says nothing
    return render()


def _unserved(items: list[dict], served: list[str], cap: int) -> list[dict]:
    seen = set(served)
    out = []
    for d in items:
        i = str(d.get("id") or "").strip()
        if i and i not in seen:
            seen.add(i)
            out.append(d)
        if len(out) >= cap:
            break
    return out


def _pointers_usable(cache: dict, branch: str | None = None) -> bool:
    """Young enough to render, and (when a branch is given) computed for it."""
    try:
        age = time.time() - float(cache.get("computed_at") or 0)
    except (TypeError, ValueError):
        return False
    if age >= _POINTERS_MAX_AGE_S:
        return False
    return branch is None or cache.get("branch") == branch


def _pointers_need_refresh(cache: dict, git: dict) -> bool:
    if not cache:
        return True
    try:
        age = time.time() - float(cache.get("computed_at") or 0)
    except (TypeError, ValueError):
        return True
    return (age > _POINTERS_REFRESH_S or cache.get("head") != git.get("head")
            or cache.get("branch") != git.get("branch"))


def _spawn_pointers(cwd: str) -> None:
    """Refresh the pointer cache in a DETACHED child so SessionStart returns
    at once. ``MEMHUB_BRIEF_POINTERS=0`` disables the worker entirely."""
    if (os.environ.get("MEMHUB_BRIEF_POINTERS") or "").strip() == "0":
        return
    try:
        kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                        "stderr": subprocess.DEVNULL, "close_fds": True}
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0))
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "pointers", cwd],
                         **kwargs)
    except Exception:  # noqa: BLE001 — a missing refresh is a staler brief, never a failed one
        pass


def _mark_delivered(session_id: str, cache: dict, offered: list[str], context: str) -> None:
    """Advance the once-per-refresh marker only when EVERY pointer the cache
    offered this session made it into ``context``. A budget cut leaves the
    marker alone, so the next prompt delivers the remainder — served ids
    already keep the shown ones from repeating."""
    if not session_id:
        return
    if set(offered) <= set(_ids_in(context)):
        served_state.save_marker(served_state.STATE_DIR, session_id, "brief",
                                 {"computed_at": cache.get("computed_at")})


def _cached_pointer_section(cache: dict, session_id: str) -> list[str]:
    """The recall lines from a pointer cache, minus what this session has
    already seen. The cache is shared by every session of the checkout, so
    this is where the per-session filter lives. An ``apply`` list a cache
    written by an older plugin still carries is ignored."""
    recall = _unserved(list(cache.get("recall") or []), _served(session_id), _MAX_RECALL)
    return _recall_lines(recall)


# ── brief: SessionStart, stdlib only, no network ───────────────────────────

def cmd_brief(payload: dict) -> int:
    cwd = _cwd_from(payload)
    room = room_map.read_room(cwd)
    if not room:
        # No room cached for this repo+backend. Deliberately silent: an
        # unonboarded repo is the common case in any checkout that is not the
        # user's own, and a hook that nags there is a hook they turn off.
        return 0

    env = room_map.current_env()
    brain_id = room["brain_id"]
    name = room.get("name") or "this repo's brain"
    session_id = _session_id(payload)

    writes = (
        "Per-turn capture is OFF (MEMHUB_TURN_FLUSH=0), so this session is not "
        "being captured."
        if _capture_is_off() else
        "Sessions are captured into your personal memory, never into this "
        "brain; team knowledge reaches it as artifacts."
    )
    head = [
        f"MemHub: this repo's agent brain is **{name}** (`{brain_id}`, {env}).",
        writes,
        "It is the DEFAULT target for memory in this repo — pass it as "
        "`agent_brain_id` when you search, save an artifact, or record a "
        "decision, so what you write is findable where the next session will "
        "look. Another brain is still reachable by naming it explicitly.",
    ]
    cached = _read_json(_cache_path(env, brain_id))
    map_lines = _render_map(brain_id, str(cached.get("overview") or ""))

    # Recall: from the pointer cache, refreshed by a detached child.
    recall: list[str] = []
    cache: dict = {}
    git = brief_identifiers.from_git(cwd)
    if git.get("root"):
        cache = _read_json(_pointers_path(env, brain_id, git["root"]))
        if cache and _pointers_usable(cache, git.get("branch")):
            recall = _cached_pointer_section(cache, session_id)
        if _pointers_need_refresh(cache, git):
            _spawn_pointers(cwd)

    offered = _ids_in("\n".join(recall))
    context = _assemble(head, map_lines, recall, brief_budget.brief_chars())
    _mark_served(session_id, _ids_in(context))   # only what survived the budget
    if cache:
        _mark_delivered(session_id, cache, offered, context)
    out: dict = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    # Speak to the USER only when the brain changed. Every session would be
    # noise, and noise is how capture_health's warning would get scrolled past.
    announced = _read_json(_announced_path())
    key = f"{room.get('name') or cwd}|{env}"
    if announced.get(key) != brain_id:
        out["systemMessage"] = (
            f"🧠 MemHub: memory in this repo defaults to {name} "
            f"({brain_id[:8]}…, {env}) — reads and writes land there unless "
            "you name another brain."
        )
        announced[key] = brain_id
        _write_json(_announced_path(), announced)

    print(json.dumps(out), flush=True)
    return 0


# ── tool-call helpers (network paths only) ─────────────────────────────────

def _tool_payload(res) -> dict | None:
    """The dict a tool returned, or ``None`` on error / unparseable."""
    if getattr(res, "isError", False) or getattr(res, "is_error", False):
        return None
    out = getattr(res, "structuredContent", None)
    if out is None:
        out = getattr(res, "structured", None)
    if isinstance(out, dict) and isinstance(out.get("result"), dict) and "items" not in out:
        out = out["result"]
    if not isinstance(out, dict):
        for b in getattr(res, "content", None) or []:
            text = getattr(b, "text", None)
            if text:
                try:
                    out = json.loads(text)
                    break
                except json.JSONDecodeError:
                    continue
    return out if isinstance(out, dict) else None


def _token_hit(identifier: str, text: str) -> bool:
    """Exact, boundary-aware containment — a grep, not a similarity."""
    ident = identifier.strip()
    if not ident:
        return False
    if ident.upper().startswith("PR #"):
        n = ident.split("#", 1)[1]
        return re.search(rf"(?<![\w/])#{re.escape(n)}(?!\d)", text) is not None
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(ident)}(?![A-Za-z0-9_])",
                     text, re.I) is not None


def _pointer_text(item: dict) -> str:
    """What a search pointer says about itself: title and abstract. Pointer
    hits carry no body (``include_content`` is left off on purpose — the
    detached worker fetches up to 40 hits and bodies would multiply that)."""
    return "\n".join(str(item.get(k) or "") for k in ("title", "abstract") if item.get(k))


def _search_items(url: str, bearer: str, brain_id: str, identifiers: list[str],
                  timeout: float) -> list[dict]:
    """Artifacts in the repo brain and the caller's own sessions that NAME one
    of ``identifiers``.

    Two searches, because the kinds live in different places: artifacts
    inside the brain (``kind="artifact"`` + ``agent_brain_id``), sessions in
    the caller's personal memory (``kind="episode"`` with NO
    ``agent_brain_id`` — the server refuses episodes scoped to a brain).
    Search is asked with the identifier strings; a hit is kept only when its
    title or abstract contains one of them as an exact token. Everything else
    is rejected, however well it scored.
    """
    import mcp_http
    if not identifiers:
        return []
    query = " ".join(identifiers[:8])
    kept: list[dict] = []
    for kind, scope in (("episode", {}), ("artifact", {"agent_brain_id": brain_id})):
        try:
            res = mcp_http.call_tool(url, bearer, "search_memory", {
                "query": query, "kind": kind, "top_k": _SEARCH_TOP_K, **scope,
            }, timeout=timeout)
        except Exception:  # noqa: BLE001
            continue
        out = _tool_payload(res)
        for item in (out or {}).get("items") or []:
            if not isinstance(item, dict):
                continue
            text = _pointer_text(item)
            match = next((i for i in identifiers if _token_hit(i, text)), "")
            if not match:
                continue
            kept.append({
                "id": str(item.get("id") or ""),
                "type": str(item.get("kind") or kind),
                "text": _first_line(item.get("title") or item.get("abstract") or ""),
                "as_of": str(item.get("as_of") or ""),
                "match": match,
                "score": item.get("score"),
            })
    kept.sort(key=lambda d: -(d["score"] if isinstance(d.get("score"), (int, float)) else 0.0))
    return kept


# ── pointers: detached worker, network ─────────────────────────────────────

def cmd_pointers(cwd: str) -> int:
    room = room_map.read_room(cwd)
    if not room:
        return 0
    env = room_map.current_env()
    brain_id = room["brain_id"]
    git = brief_identifiers.from_git(cwd)
    if not git.get("root") or not _cache_is_writable():
        return 0
    path = _pointers_path(env, brain_id, git["root"])
    record: dict = {
        "computed_at": time.time(), "head": git["head"], "branch": git["branch"],
        "base": git["base"], "identifiers": {"paths": git["paths"][:20], "refs": git["refs"]},
        "recall": [],
    }
    basenames = [p.rsplit("/", 1)[-1] for p in git["paths"]]
    recall_ids = brief_identifiers._dedupe(git["refs"] + basenames)[:16]
    if not recall_ids:
        _write_json(path, record)   # nothing to search on; do not respawn every session
        return 0

    from _memhub_auth import resolve_bearer
    try:
        url, bearer = resolve_bearer()
    except Exception:  # noqa: BLE001
        bearer = None
    if not bearer:
        record["error"] = "no credential"
        _write_json(path, record)
        return 0

    # NO session filters here. The cache is shared by every session of this
    # checkout (keyed env + brain + root), so a served list baked in would
    # hide pointers from the NEXT session. It is applied at render time, per
    # session; the cache holds twice the cap so that filter still leaves a
    # full block.
    record["recall"] = _dedupe_text([
        d for d in _search_items(url, bearer, brain_id, recall_ids, _TIMEOUT_S)
        if d["id"]
    ])[:_MAX_RECALL * 2]
    _write_json(path, record)
    return 0


# ── prompt: UserPromptSubmit, deliver what the brief could not ────────────

def cmd_prompt(payload: dict) -> int:
    cwd = _cwd_from(payload)
    room = room_map.read_room(cwd)
    if not room:
        return 0
    root = room_map.repo_root(cwd)
    if root is None:
        return 0
    env = room_map.current_env()
    brain_id = room["brain_id"]
    session_id = _session_id(payload)

    # The pointer cache the brief could not deliver (it landed after
    # SessionStart printed, or the budget cut part of it) — rendered until
    # every pointer has been, and only when it was computed for the branch
    # the checkout is on NOW: a `git switch` since the brief must not inject
    # the old branch's pointers.
    recall: list[str] = []
    cache = _read_json(_pointers_path(env, brain_id, str(root)))
    branch = brief_identifiers.current_branch(root)
    marker = served_state.load_marker(served_state.STATE_DIR, session_id, "brief")
    if cache and cache.get("computed_at") != marker.get("computed_at"):
        if _pointers_usable(cache, branch or None):
            recall = _cached_pointer_section(cache, session_id)
        elif branch and cache.get("branch") != branch:
            _spawn_pointers(cwd)
    if not recall:
        return 0
    pending_ids = _ids_in("\n".join(recall))
    context = _assemble([], [], recall, brief_budget.brief_chars())
    _mark_served(session_id, _ids_in(context))   # only what survived
    _mark_delivered(session_id, cache, pending_ids, context)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": context,
    }}), flush=True)
    return 0


# ── refresh: Stop hook, async, network, throttled ──────────────────────────

def _extract_overview(res) -> str:
    """The digest TEXT out of a ``get_brain_overview`` result.

    Three shapes, in order of preference, because the server does not promise
    one: ``structuredContent`` when present, then a text block that is really
    a JSON envelope (what staging actually returns), then a text block that is
    the digest itself. The envelope case matters: taking the block verbatim
    caches ~3.5KB of JSON punctuation and injects THAT as "what this brain
    knows".
    """
    structured = getattr(res, "structured", None)
    if structured is None:
        structured = getattr(res, "structuredContent", None)
    if isinstance(structured, dict):
        text = structured.get("overview") or structured.get("text")
        if isinstance(text, str) and text.strip():
            return text

    for block in getattr(res, "content", None) or []:
        raw = getattr(block, "text", None)
        if not raw:
            continue
        raw = str(raw)
        try:
            envelope = json.loads(raw)
        except Exception:  # noqa: BLE001
            return raw
        if isinstance(envelope, dict):
            text = envelope.get("overview") or envelope.get("text")
            return text if isinstance(text, str) else ""
        return raw
    return ""


def _is_fresh(path: Path) -> bool:
    cached = _read_json(path)
    try:
        return (time.time() - float(cached.get("refreshed_at") or 0)) < _MAX_AGE_S
    except Exception:  # noqa: BLE001
        return False


def cmd_refresh(payload: dict) -> int:
    cwd = _cwd_from(payload)
    room = room_map.read_room(cwd)
    if not room:
        return 0
    env = room_map.current_env()
    brain_id = room["brain_id"]
    path = _cache_path(env, brain_id)
    if _is_fresh(path):
        return 0
    # Checked BEFORE the network call: the throttle is the cache's own mtime,
    # so an unwritable cache is permanently stale and would refetch every turn.
    if not _cache_is_writable():
        return 0

    # Imported here, not at module scope: `brief` runs on the synchronous
    # SessionStart path and must not pay for auth/transport modules.
    import mcp_http
    from _memhub_auth import resolve_bearer

    try:
        url, bearer = resolve_bearer()
        if not bearer:
            return 0
        res = mcp_http.call_tool(
            url, bearer, "get_brain_overview",
            {"agent_brain_id": brain_id}, timeout=_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001
        return 0

    if getattr(res, "isError", False) or getattr(res, "is_error", False):
        return 0

    overview = _extract_overview(res)
    if not overview.strip():
        return 0

    _write_json(path, {
        "brain_id": brain_id,
        "env": env,
        "name": room.get("name") or "",
        "overview": overview,
        "refreshed_at": time.time(),
    })
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "brief"
    if cmd == "pointers":
        return cmd_pointers(sys.argv[2] if len(sys.argv) > 2 else os.getcwd())
    try:
        raw = sys.stdin.read()
    except Exception:  # noqa: BLE001
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if cmd == "refresh":
        return cmd_refresh(payload)
    if cmd == "prompt":
        return cmd_prompt(payload)
    return cmd_brief(payload)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        # Orientation must never be the reason a session fails to start.
        sys.exit(0)
