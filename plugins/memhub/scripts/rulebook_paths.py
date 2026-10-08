"""Where the rulebook keeps its local state — one directory per backend.

The plugin ships twice from one tree: `memhub-staging`, and the public
`memhub` that scripts/promote_export.py makes from it by renaming the
manifests and swapping the MCP configs. Both installs used to keep the cached
book, the fire ledger and the per-session state in ONE directory,
`~/.config/memhub-plugin/rulebook`, with the book keyed by repo name only.
On a machine with both installed (observed 2026-10-05) the prod install's
background refresh overwrote the staging book — 89 rules — with prod's
one-rule book in the middle of a staging session, the two installs' fires
were flushed to whichever backend flushed first, and both read and wrote the
same session state (duplicate fires, doubled hand-offs).

So the state is keyed by the backend the install talks to: the host in the
install's OWN `.mcp.json`, which is the one file promote_export changes and
so is exactly what tells the two apart:

    ~/.config/memhub-plugin/rulebook/<backend host>/{book,ledger,state,...}

`$MEMHUB_RULEBOOK_BASE` still overrides the whole thing verbatim — tests and
operators point it at a directory and get that directory, unkeyed.
`$MEMHUB_MCP_BASE_URL` deliberately does NOT re-key: it is a per-process
override (a test proxy on a random port, a manual flush), and keying on it
would orphan the install's book every time it is set. The install's identity
is its `.mcp.json`.

An install whose `.mcp.json` is unreadable keeps the old unkeyed directory —
what it did before this change — rather than guessing a backend.

The old unkeyed directory (`legacy_base()`) may still hold fires recorded
before the upgrade and never flushed. `drain_legacy` (called by the hook's
flush lane) ships that ledger behind its own `.sent` watermark until it is
empty, and nothing writes a new row there, so nothing recorded before the
upgrade is dropped.

`python3 rulebook_paths.py` prints this install's base (for
/memhub:create-rule's forward test, which writes its redirect claim there).

Stdlib only, and cheap: it reads one small JSON file. The hook's hot path
imports this; it must never import the auth or HTTP modules.
"""
from __future__ import annotations

import json
import os
import re
from urllib.parse import urlsplit

LEGACY_REL = os.path.join(".config", "memhub-plugin", "rulebook")


def plugin_root() -> str:
    """The install this file belongs to. Its `.mcp.json` sits beside `scripts/`.
    Unresolved, like `_memhub_auth._plugin_root`: an install is the directory
    the host loaded, not wherever a symlink in it points."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def backend_key(root: str | None = None) -> str:
    """The backend host named by the install's `.mcp.json`, made safe for a
    directory name (`host:port` → `host_port`), or "" when it cannot be read."""
    try:
        with open(os.path.join(root or plugin_root(), ".mcp.json"), encoding="utf-8") as f:
            servers = json.load(f).get("mcpServers") or {}
        url = next((cfg.get("url") for name, cfg in servers.items()
                    if str(name).lower().startswith("memhub") and isinstance(cfg, dict)), None)
        host = urlsplit(str(url or "")).netloc.rsplit("@", 1)[-1].lower()
    except Exception:
        return ""
    host = re.sub(r"[^a-z0-9._-]", "_", host).strip(".")
    return host[:120]


def legacy_base() -> str:
    """The unkeyed directory every install shared before state was keyed."""
    return os.path.join(os.path.expanduser("~"), LEGACY_REL)


def base(root: str | None = None) -> str:
    """The rulebook's state directory for this install."""
    override = os.environ.get("MEMHUB_RULEBOOK_BASE")
    if override:
        return override
    key = backend_key(root)
    return os.path.join(legacy_base(), key) if key else legacy_base()


def legacy_ledger_base(root: str | None = None) -> str:
    """The unkeyed base whose pre-upgrade ledger still has to be drained, or ""
    when there is none to drain (an override is in force, or this install
    still uses the unkeyed directory itself)."""
    if os.environ.get("MEMHUB_RULEBOOK_BASE"):
        return ""
    old = legacy_base()
    return "" if base(root) == old else old


def _drained(ledger):
    """True once every watermark in the old ledger's `.sent` has reached the
    end of its file — then there is nothing left to send, and the drain costs
    a few stats per flush instead of a flush."""
    try:
        with open(os.path.join(ledger, ".sent"), encoding="utf-8") as f:
            sent = json.load(f)
        for name, key in (("fires.jsonl", "fires_offset"), ("events.jsonl", "events_offset"),
                          ("conversions.jsonl", "conversions_offset")):
            path = os.path.join(ledger, name)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if size and sent.get(key, 0) < size:
                return False
        return True
    except Exception:
        return False    # no or unreadable watermark: not known to be drained


def drain_legacy(hook, final=False):
    """Flush the pre-keying shared ledger with the hook's own flush lane.

    `hook` is rulebook_hook's module namespace (its `globals()`). Every install
    shared `legacy_base()` before state was keyed, so rows recorded there
    before the upgrade and never flushed are still owed to a server. They are
    shipped by the same rules as any flush — behind that ledger's OWN `.sent`
    watermark, advanced only on a 2xx — until the watermarks reach the end of
    its files; nothing writes a new row there, so the drain ends on its own and
    is skipped from then on (`_drained`). Those rows were already mixed across
    installs (that was the bug), so they go where they would have gone before:
    to the first upgraded install that flushes. A row that backend does not
    know is logged in the old ledger's `rejected.jsonl`, never silently skipped.

    A failure is loud where capture_health looks: the flush lane writes its
    breadcrumb under the base it is flushing (the old one), so a crumb it left
    during the drain is re-raised in THIS install's `ledger/.last_error`.
    """
    old = hook.get("LEGACY_BASE") or ""
    ledger = os.path.join(old, "ledger")
    if not old or os.path.normpath(old) == os.path.normpath(hook["_base"]()) \
            or not os.path.isdir(ledger) or _drained(ledger):
        return
    crumb = os.path.join(ledger, ".last_error")
    try:
        before = os.stat(crumb).st_mtime_ns
    except OSError:
        before = None
    failed = None
    hook["_ACTIVE_BASE"] = old
    try:
        hook["flush_fires"](final=final)
    except Exception as exc:    # the old rows stay behind their watermark
        failed = exc
    finally:
        hook["_ACTIVE_BASE"] = ""
    if failed is None:
        try:
            if os.stat(crumb).st_mtime_ns != before:
                with open(crumb, encoding="utf-8") as f:
                    failed = "old shared ledger: " + str(json.load(f).get("error"))
        except Exception:
            pass
    if failed is not None:
        hook["_breadcrumb"]("flush", failed)   # in THIS install's ledger, where health reads

if __name__ == "__main__":
    print(base())
