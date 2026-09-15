#!/usr/bin/env bash
# Versions are reviewed changes, never unpinned "latest". Only GitHub Linux runners.
set -euo pipefail
host_tools="$RUNNER_TEMP/memhub-host-tools"
mkdir -p "$host_tools"
case "$1" in
  codex)
    npm install --prefix "$host_tools" @openai/codex@0.153.4
    printf '%s\n' "$host_tools/node_modules/.bin" >> "$GITHUB_PATH"
    ;;
  claude)
    npm install --prefix "$host_tools" @anthropic-ai/claude-code@2.1.271
    printf '%s\n' "$host_tools/node_modules/.bin" >> "$GITHUB_PATH"
    ;;
  cursor)
    curl --fail --silent --show-error --location --proto '=https' \
      https://downloads.cursor.com/lab/2026.09.10-fd3934a/linux/x64/agent-cli-package.tar.gz \
      --output "$host_tools/cursor.tar.gz"
    mkdir -p "$host_tools/cursor"
    tar -xzf "$host_tools/cursor.tar.gz" --strip-components=1 -C "$host_tools/cursor"
    printf '%s\n' "$host_tools/cursor" >> "$GITHUB_PATH"
    ;;
  *) echo 'unknown release-test host' >&2; exit 2 ;;
esac
