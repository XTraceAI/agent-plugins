#!/usr/bin/env bash
# Publish plugins/memhub/core/ as a standalone branch the codex repo can pull.
#
# `git subtree pull --prefix=<X> <repo> <ref>` merges the ENTIRE content of
# <ref> into <X>. To hand a consumer just one subdirectory, the source repo has
# to publish a branch whose ROOT is that subdirectory — that is what
# `git subtree split` produces, and that is all this script does.
#
#     scripts/publish_core_split.sh            # build/refresh the branch locally
#     scripts/publish_core_split.sh --push     # ...and push it to origin
#
# Then, in memhub-codex-plugin:  scripts/sync_core.sh
#
# The split is deterministic: re-running it on an unchanged core reproduces the
# same commit ids, so a no-op sync really is a no-op. Sync is ONE-WAY — the
# codex repo pulls and never pushes back. Fix the core here.
set -euo pipefail

PREFIX="plugins/memhub/core"
BRANCH="core-split"
PUSH=0

for arg in "$@"; do
  case "$arg" in
    --push) PUSH=1 ;;
    -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"

if [ ! -d "$PREFIX" ]; then
  echo "ERROR: $PREFIX not found — run this from the plugin repo." >&2
  exit 1
fi

# A split of a dirty tree silently publishes only what is COMMITTED, which is a
# confusing way to ship a stale core. Refuse instead.
if ! git diff --quiet -- "$PREFIX" || ! git diff --cached --quiet -- "$PREFIX"; then
  echo "ERROR: uncommitted changes under $PREFIX — commit them first," >&2
  echo "       otherwise the split would publish the previous core." >&2
  exit 1
fi

# Deliberately NOT --rejoin: rejoin writes a bookkeeping merge commit back onto
# the current branch, which would show up as noise in every PR that happens to
# touch the core. The core is a handful of files, so recomputing the split from
# scratch each time costs nothing and keeps this branch's history clean.
echo "splitting $PREFIX -> $BRANCH ..."
git branch -D "$BRANCH" >/dev/null 2>&1 || true
tip="$(git subtree split --prefix="$PREFIX" -b "$BRANCH" | tail -1)"

echo
echo "branch '$BRANCH' @ ${tip:0:12} now holds $PREFIX at its root:"
git ls-tree --name-only "$BRANCH" | sed 's/^/  /'

if [ "$PUSH" -eq 1 ]; then
  echo
  echo "pushing $BRANCH to origin ..."
  git push origin "$BRANCH"
  echo "done. In memhub-codex-plugin, run: scripts/sync_core.sh"
else
  echo
  echo "not pushed. Re-run with --push, or push manually:"
  echo "  git push origin $BRANCH"
fi
