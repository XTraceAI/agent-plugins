# MemHub shared core

The **host-agnostic** half of the MemHub plugin: auth, session upload, and repo→room
routing. Nothing in here knows what a Claude Code hook is.

This directory is the unit that gets vendored into
[`XTraceAI/memhub-codex-plugin`](https://github.com/XTraceAI/memhub-codex-plugin)
as `memhub_core/` via `git subtree`. It exists as its own directory for exactly
one reason: **`git subtree` syncs a directory prefix, and it cannot sync a subset
of a directory.** These three modules used to sit in `../scripts/` beside a dozen
Claude-only hook scripts, which made them unsyncable.

| File | What it is |
|---|---|
| `_memhub_auth.py` | Endpoint resolution + OAuth (PKCE) against Auth0 — the SAME credentials the `/mcp` connector uses |
| `import_session.py` | The upload path: read a JSONL transcript, chunk it, ship it via `import_conversation`, fold the session gist forward |
| `room_map.py` | Per-user repo→room cache (`~/.config/memhub-plugin/rooms.json`), keyed by repo and backend |
| `room_map_test.py`, `core_boundary_test.py` | Stdlib self-tests; both run in the plugin repo *and* in a vendored copy |

## Rules

**1. Edit the core here, never in a consumer repo.** The codex repo pulls this
directory; it never pushes back. A fix made there is a fix that gets clobbered by
the next `git subtree pull`. This is the whole point of the split — one copy, one
place to change it.

**2. Flat co-location is load-bearing.** Every module resolves its siblings with
`sys.path.insert(0, Path(__file__).resolve().parent)` and then a flat
`from room_map import ...`. There is no `__init__.py` and there must not be one:
turning this into a package changes every import site in both repos, and is a
separate refactor.

**3. No Claude-only imports.** `core_boundary_test.py` enforces this by AST-walking
every module (including deferred imports inside functions) and rejecting anything
outside the stdlib, `mcp`, and the core itself. It also pins the two *sanctioned*
Claude references — `$CLAUDE_PLUGIN_ROOT` in `_memhub_auth`, and the
`~/.claude/projects` session glob in `import_session` — so new coupling has to be
argued for in review instead of arriving by accident.

**4. `../scripts/` reaches these files by symlink, not by copy.** Every hook
command string and SKILL.md path still says `${CLAUDE_PLUGIN_ROOT}/scripts/<name>.py`,
and keeps working because `scripts/_memhub_auth.py → ../core/_memhub_auth.py`.
A real file at either path would silently shadow the core and drift from it —
the same failure `plugins/memhub-staging/`'s symlinks prevent. Also enforced by
`core_boundary_test.py`.

## The contract a host repo must satisfy

`_memhub_auth.build_oauth()` reads `<plugin_root>/.mcp.json` for the OAuth
`clientId`, `callbackPort`, and `authServerMetadataUrl` — and it does **not**
catch a read failure. `_plugin_root()` is `$CLAUDE_PLUGIN_ROOT` when Claude Code
sets it, otherwise `Path(__file__).parent.parent`.

So **any repo vendoring this core must place a plugin-shaped `.mcp.json` one
directory above it.** In this repo that's `plugins/memhub/.mcp.json` (prod) and
`plugins/memhub-staging/.mcp.json` (staging). In the codex repo it's the repo
root, next to `memhub_core/`. Without it, `default_url()` falls through to a
path-substring heuristic that raises when the path contains neither `memhub` nor
`staging`, and OAuth fails outright with a `FileNotFoundError`.

Note that `--url` overrides the *endpoint only* — the OAuth client still comes
from `.mcp.json`. Pointing a consumer at a different backend means editing that
file, not passing a flag.

## Running the tests

```bash
python3 plugins/memhub/core/room_map_test.py
python3 plugins/memhub/core/core_boundary_test.py
```

Both are stdlib-only and network-free. `core_boundary_test.py` reports whether
it is running in the plugin repo or a vendored copy, and skips the
plugin-layout checks in the latter.

## Publishing to the codex repo

```bash
scripts/publish_core_split.sh          # split this dir onto the core-split branch
scripts/publish_core_split.sh --push   # ...and push it to origin
```

Then, in the codex repo: `scripts/sync_core.sh`. See that script and
`../../../README.md` for the round trip.
