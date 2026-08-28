# Releasing the MemHub plugins

How a merge to `main` becomes something a public consumer can install, for
`XTraceAI/agent-plugins` — across three channels. Current as of 2026-08-16.

## The one thing to internalise

**The `ref`/`sha` pin only guards Claude.** The Codex catalog
(`.agents/plugins/marketplace.json`) and the Cursor catalog
(`.cursor-plugin/marketplace.json`) use bare path sources with no pin field.
On those channels, **a version bump reaching `main` is the ship event** —
delivered when the client next refreshes the marketplace, cached by version
string.

Channel summary:

| Channel | Catalog | Pin | Ships when |
|---|---|---|---|
| Claude Code | `.claude-plugin/marketplace.json` (git-subdir) | ref + sha | the pin moves |
| Codex | `.agents/plugins/marketplace.json` (local path) | none | version bump on `main` |
| Cursor team marketplace | `.cursor-plugin/marketplace.json` (local path) | none | version bump on `main` |
| Cursor official directory | submitted repo link | manual review, every update | after review clears |

## The invariant (CI-enforced)

Unpinned channels snapshot `main` at install time and cache under the version
string. If shipped code changes without a bump, two installs of the same
version run different bytes. Therefore:

**Any PR that changes `plugins/memhub/**` either bumps the version or waits
for the release train.** Between releases, shipped plugin code on `main` is
immutable.

`.github/workflows/bump-guard.yml` enforces this: a PR touching
`plugins/memhub/**` fails unless the manifest version changed in that PR, or
the PR carries the `bump-exempt` label (a deliberate assertion that the
change is inert — docs or packaging that ships behavior to nobody). The
guard also runs the manifest parity and test-registration suites.

## The procedure

1. **Verify BEFORE any bump merges.** The bump is the release on two
   channels. On the exact `origin/main` SHA:

   ```bash
   git fetch origin --tags
   git log --oneline <last-tag>..origin/main      # what am I actually shipping?
   git diff --stat <last-tag>..origin/main        # does it touch plugins/?
   git worktree add --detach /tmp/rel origin/main
   cd /tmp/rel && uv run --with 'mcp<2' python tests/run_all.py
   ```

2. **Merge the release PR bumping ALL manifests in lockstep**
   (`tests/version_parity_test.py` enforces):
   - `plugins/memhub/plugin.json` (Agent Plugins 1.0 root)
   - `plugins/memhub/.claude-plugin/plugin.json`
   - `plugins/memhub/.codex-plugin/plugin.json`
   - `plugins/memhub/.cursor-plugin/plugin.json`
   - `plugins/memhub-staging/.claude-plugin/plugin.json`

   Claude AND Codex key install caches by version — no bump, no delivery.
   The moment this merges, Cursor and Codex are live.

3. **Tag the release commit** — `memhub--v<version>`, on the verified SHA.
   `claude plugin tag plugins/memhub` validates manifest/marketplace
   agreement; `git tag memhub--v<version> <sha>` works for non-HEAD commits.
4. **Push the tag FIRST** (the Claude pin cannot resolve a missing ref).
5. **Move `ref` + `sha`** in `.claude-plugin/marketplace.json` by PR.
6. **Smoke-test each channel:** Claude reinstall + restart; `codex plugin
   marketplace update && codex plugin add memhub`; Cursor marketplace
   refresh, hooks visible in Hooks settings.
7. **Submit the Cursor-official update** (when listed) — every update is
   manually reviewed; expect lag; keep the server compatible one plugin
   version back.

### Ordering is load-bearing, twice

- **Claude:** the remote tag must exist before the `marketplace.json` pin
  change reaches `main`, or `/plugin marketplace update` resolves a missing
  `ref` and `memhub` becomes uninstallable until the tag lands.
- **Cursor + Codex:** there is no pin to hide behind — everything you used
  to check "before tagging" happens before the bump PR merges.

## The staging build is different, and must stay different

`memhub-staging` is deliberately **not** in any public catalog. It lives in
the internal manifest at `plugins/.claude-plugin/marketplace.json` and
installs from a local clone:

```text
/plugin marketplace add <path-to-repo>/plugins
/plugin install memhub-staging@memhub-internal
```

- **The staging entry can never use `git-subdir`.** That fetch pulls only
  the plugin's own subdirectory, and
  `plugins/memhub-staging/{skills,hooks,scripts,references}` are symlinks
  into `../memhub/`. They arrive dangling — observed live on PR #56: the
  install came up with no `scripts/` directory at all and still reported
  success. A path source copies from the local clone and dereferences the
  symlinks.
- **Also nonconforming under Agent Plugins 1.0:** the spec requires
  symlinks to resolve inside the plugin root; staging's escape to
  `../memhub/` disqualifies it from any spec-conformant installer. One more
  reason it never enters the Codex/Cursor catalogs.
- **Staging is an rsync of `main`'s working tree, not a git branch.** The
  `staging` git branch is being retired; the internal marketplace at
  `~/.claude/plugins/memhub-internal-marketplace` is refreshed by copying
  the plugin directory out of the checkout, dereferencing the symlinks:

  ```sh
  rsync -aL --delete --exclude __pycache__ \
    plugins/memhub-staging/ \
    ~/.claude/plugins/memhub-internal-marketplace/plugins/memhub-staging/
  ```

  If the version did not change, the version-keyed cache dir
  (`~/.claude/plugins/cache/memhub-internal/memhub-staging/<version>/`) is
  never re-fetched, so rsync the same tree into it as well — otherwise
  `/plugin update` reports success and installs nothing. Restart the
  session afterwards; a running session does not pick up the new files.

`plugins/memhub/` holds real files with no symlinks of its own, which is
exactly why the PUBLIC entry is safe to pin via `git-subdir`.

## Gotchas worth knowing before you hit them

- **Version-keyed caches on BOTH Claude and Codex**
  (`~/.claude/plugins/cache/…/<version>/`,
  `~/.codex/plugins/cache/…/<version>/`). No bump, no delivery — on either.
- **Cursor Teams/Enterprise org policy silently blocks** unofficial
  marketplaces and local installs. Official-directory listing is the
  enterprise allowlist path, not vanity.
- **A marketplace manifest must be named exactly** per host —
  `.claude-plugin/marketplace.json`, `.cursor-plugin/marketplace.json`,
  `.agents/plugins/marketplace.json`. Three catalogs, three schemas, no
  sharing. A differently-named file in `.claude-plugin/` parses as a
  *plugin* manifest.
- **Both plugins register an MCP server named `memhub`** — install prod or
  staging, never both. A repo that pins one backend in project settings
  must explicitly set the other to `false`.
- **Plugin enable/disable does not apply to a running session** — on any
  host. Restart before judging a change.
- **Cursor directory names are unique kebab-case** — claim `memhub` early.
- **`claude plugin validate <path>`** covers the Claude manifests; the
  parity tests plus a fresh smoke install per host are the pre-flight
  elsewhere. Codex's official directory has no self-serve publishing yet —
  the git marketplace is the sanctioned path.

## Verify the commit you tag — do not trust the PR title

Added after nearly getting this wrong on 0.26.0: PR #60, titled as doc
cleanup, changed shipped auth and capture code. Tag the SHA you tested, not
`HEAD` of whatever you had checked out. With unpinned channels this matters
*before the bump merges*, not just before tagging.

## Release log

- **0.26.0** — `d26634e`, tagged 2026-08-10, Claude only. Room-brain
  fallback (#57), SessionStart repo-brain brief (#58), doc/comment cleanup
  (#60).
- **0.26.1** — `fa82029`, tagged 2026-08-10, Claude only. Skill corrections
  (#62).

## Open items before the first multi-host release

- Host manifests + AP core: PR #65 (`feat/multihost-ap-core`).
- The two new root catalogs (`.agents/plugins/`, `.cursor-plugin/`) — land
  LAST, after the 0.27.0 bump/tag/pin, so the first state Codex/Cursor can
  ever resolve is a released version.
- Cursor official-directory submission (claims the `memhub` name).
