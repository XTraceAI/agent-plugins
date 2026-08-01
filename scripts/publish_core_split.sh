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
    # Print the header comment block, stopping at the first non-comment line —
    # a fixed line range drifts the moment the header is edited.
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

cd "$(git rev-parse --show-toplevel)"

if [ ! -d "$PREFIX" ]; then
  echo "ERROR: $PREFIX not found — run this from the plugin repo." >&2
  exit 1
fi

# A split of a dirty tree silently publishes only what is COMMITTED, which is a
# confusing way to ship a stale core. Refuse instead. Untracked files count:
# a brand-new core module is invisible to `git diff` and is exactly the thing
# you'd be surprised to find missing downstream.
untracked="$(git ls-files -o --exclude-standard -- "$PREFIX")"
if ! git diff --quiet -- "$PREFIX" || ! git diff --cached --quiet -- "$PREFIX" \
   || [ -n "$untracked" ]; then
  echo "ERROR: uncommitted changes under $PREFIX — commit them first," >&2
  echo "       otherwise the split would publish the previous core." >&2
  [ -n "$untracked" ] && echo "       untracked: $(echo "$untracked" | tr '\n' ' ')" >&2
  exit 1
fi

# Deliberately NOT --rejoin: rejoin writes a bookkeeping merge commit back onto
# the current branch, which would show up as noise in every PR that happens to
# touch the core. The core is a handful of files, so recomputing the split from
# scratch each time costs nothing and keeps this branch's history clean.
echo "splitting $PREFIX -> $BRANCH ..."

# Compute the split into a scratch branch FIRST, so the identity check below
# compares real history instead of guessing from filenames.
#
# -q: subtree streams a per-commit progress counter that scrolls the real output
# away. Read the tip from the branch afterwards rather than parsing stdout,
# whose format ("<sha>" vs "Created branch '<name>'") is not stable.
TMP="${BRANCH}-split-$$"
git branch -D "$TMP" >/dev/null 2>&1 || true
git subtree split -q --prefix="$PREFIX" -b "$TMP" >/dev/null
tip="$(git rev-parse "$TMP")"
trap 'git branch -D "$TMP" >/dev/null 2>&1 || true' EXIT

if git show-ref -q --verify "refs/heads/$BRANCH"; then
  # `git subtree split` is deterministic and history-preserving, so a genuine
  # PREVIOUS split of this prefix is an ancestor of the new tip (equal when the
  # core hasn't changed). Anything else is an unrelated branch that merely
  # shares the name — and `-D` discards unmerged commits, so refuse.
  #
  # Comparing file NAMES is not enough: a branch whose root happens to hold
  # files called room_map.py etc. would pass that check and lose real work.
  if ! git merge-base --is-ancestor "$BRANCH" "$tip"; then
    echo "ERROR: branch '$BRANCH' exists but is not an ancestor of the split" >&2
    echo "       just computed from $PREFIX — it is not a previous split of" >&2
    echo "       this prefix, so replacing it could discard real work." >&2
    echo "       existing: $(git rev-parse --short "$BRANCH")   new: ${tip:0:7}" >&2
    echo "       Rename or delete it yourself, then re-run." >&2
    exit 1
  fi
  if [ "$(git rev-parse "$BRANCH")" = "$tip" ]; then
    echo "  '$BRANCH' already up to date ($(git rev-parse --short "$BRANCH"))"
  else
    echo "  advancing '$BRANCH' $(git rev-parse --short "$BRANCH") -> ${tip:0:7}"
  fi
fi

# -f moves the ref forward without ever needing -D, so there is no window in
# which the branch does not exist.
git branch -f "$BRANCH" "$tip"

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
