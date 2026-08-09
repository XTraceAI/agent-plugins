# Contributing

For XTrace developers working on the MemHub plugin itself. If you just want to
*use* MemHub, see the [README](README.md) — everything below is internal.

## Two backends, two plugins

`plugins/memhub/` points at production; `plugins/memhub-staging/` points at
staging. They exist as separate plugin directories for one reason: `.mcp.json`
env-var expansion (`${VAR:-default}`) covers `url`, `command`, `args`, `env`,
and `headers`, but **not** the nested `oauth` fields (`clientId`,
`authServerMetadataUrl`). Prod and staging are different Auth0 tenants with
different OAuth clients, so a single plugin entry cannot toggle between them at
runtime.

Everything else is shared: `skills/`, `hooks/`, `scripts/`, and `references/`
in `plugins/memhub-staging/` are **relative symlinks** into `../memhub/`, so the
two builds never drift. A path-source install dereferences them into real files;
a `git-subdir` install does not — see "Installing the staging build" below.

`plugins/memhub/` holds the real files and has no symlinks of its own, which is
what makes it safe to publish via `git-subdir`.

Both plugins register an MCP server literally named `memhub` — that neutral name
is what lets the skills be env-agnostic (`mcp__memhub__*`). It also means the two
**collide**: install one or the other, never both.

## Branches

| Branch | Role |
|---|---|
| `main` | Release. What public consumers install from. |
| `staging` | Integration. Where plugin work lands first. |

Feature branches cut from `staging`. Promotion to production is a merge
`staging → main`, a version bump, and a new release tag.

## Installing the staging build

Staging is deliberately **not** in the public marketplace — public consumers
should only ever see one installable MemHub plugin. It lives in a second
marketplace manifest at `plugins/.claude-plugin/marketplace.json`, installed
from your local clone.

Check out the branch you want to run (normally `staging`), disable or uninstall
the public `memhub` first (they collide), then:

```text
/plugin marketplace add <path-to-this-repo>/plugins
/plugin install memhub-staging@memhub-internal
```

Two constraints shaped this layout, both worth knowing before you "simplify" it:

- **The staging entry must use a path source, not `git-subdir`.** A `git-subdir`
  fetch pulls only the plugin's own subdirectory, so
  `plugins/memhub-staging/{skills,hooks,scripts,references}` — which are
  symlinks into `../memhub/` — arrive dangling and the plugin is silently
  broken. A path source copies from your local clone, where the symlink targets
  exist, and dereferences them into real files.
- **The marketplace root is `plugins/`, not the repo root.** Plugin sources are
  resolved relative to the directory containing `.claude-plugin/` and may not
  contain `..`. Rooting this marketplace at `plugins/` lets it say
  `./memhub-staging` while leaving the existing `../memhub/*` symlinks
  untouched.

Because it installs from your working tree, the staging build reflects whatever
branch is checked out — it does not track `staging` on its own. Re-run
`/plugin marketplace update memhub-internal` after switching branches.

Then `/mcp` → `memhub` → **Authenticate** against the staging tenant. Prod and
staging issue non-interchangeable tokens; they're cached in separate files keyed
by host under `~/.config/memhub-plugin/`.

### Switching a repo between backends

Enable state is per scope, and **project settings win over user settings**. A
repo that pins one backend must explicitly opt *out* of the other, or the
user-scope default leaks in and you get two `memhub` servers:

```jsonc
// <repo>/.claude/settings.json
{
  "enabledPlugins": {
    "memhub@memhub": true,
    "memhub-staging@memhub-internal": false   // ← required, not optional
  }
}
```

Plugin enable/disable does not apply to a running session — restart Claude Code.

Note that the room cache (`~/.config/memhub-plugin/rooms.json`) is keyed by repo
*and* by backend, so switching env changes which agent brain a repo resolves to.
A repo with no entry for the backend you switched to will re-resolve its room on
the next session.

## Cutting a release

The public `memhub` entry is pinned to a tag via a `git-subdir` source, so
merging to `main` does **not** ship anything on its own. Publishing is explicit:

1. Merge `staging → main`.
2. Bump `version` in **both** `plugins/memhub/.claude-plugin/plugin.json` and
   `plugins/memhub-staging/.claude-plugin/plugin.json` — they must stay in
   lockstep.
3. Tag the release: `claude plugin tag plugins/memhub` creates
   `memhub--v<version>` and validates that the manifest and the marketplace
   entry agree.
4. Update `ref` **and** `sha` in the `memhub` entry of
   `.claude-plugin/marketplace.json` to the new tag and its commit.
5. Push the tag and `main`.

Step 4 is the one that actually ships. Until `ref`/`sha` move, consumers keep
getting the previous release — which is the point: `main` can be mid-development
without breaking anyone.

Version bumps matter beyond bookkeeping: the plugin cache is keyed by version,
so a fix does not reach an existing install until the version changes.
