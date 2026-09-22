# Contributing

If you just want to *use* MemHub, see the [README](README.md).

## Where development happens

Not here. The MemHub plugin is developed in **`XTraceAI/agent-plugins-internal`**,
a private repository, against a staging backend. This repository is the
*release* repository: it holds the production plugin and the three marketplace
catalogs that consumers install from.

`plugins/memhub/` is **generated**. It is an export of internal's
`plugins/memhub-staging/` — the same files, re-tagged to the production
identity (`name: memhub`, `.mcp.json` pointing at `api.memhub.xtrace.ai`) and
nothing else. A hand-written change to it is overwritten by the next
promotion, and `main` is restricted so that promotion pull requests are the
only thing that lands there.

XTrace developers: see `docs/DEVELOPING.md` in the internal repository for the
local loop, the four test layers, and what releasing looks like in every case.

## Reporting a bug

Open an issue here. Please say which host (Claude Code, Codex, Cursor) and
which plugin version — `plugins/memhub/.claude-plugin/plugin.json` carries it,
and a loaded MCP connection reports it as `?memhub_plugin_version=` on its
server URL.

## What still lives in this repository

[RELEASING.md](RELEASING.md) — how a promoted `plugins/memhub/` becomes
something a consumer can install, across the three channels. The pin-and-tag
step is done by a person, from a verified export; the version bump is the ship
event on the unpinned channels.

`.github/workflows/bump-guard.yml` — a change to `plugins/memhub/**` either
bumps the version or carries `bump-exempt`. The unpinned channels cache by
version string, so shipping different bytes under one version means two
installs of "the same" version run different code.
