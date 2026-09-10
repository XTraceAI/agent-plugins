#!/usr/bin/env bash
# Run the installed-plugin test contract without reading account configuration.
set -euo pipefail
cd "$(dirname "$0")/.."
command -v python3 >/dev/null
command -v uv >/dev/null
check_home=$(mktemp -d "${TMPDIR:-/tmp}/plugin-check.XXXXXX")
trap 'rm -rf "$check_home"' EXIT
mkdir "$check_home/tmp"
check_env=(env -i "PATH=$PATH" "HOME=$check_home" "XDG_CONFIG_HOME=$check_home/.config"
  "TMPDIR=$check_home/tmp" "UV_CACHE_DIR=${UV_CACHE_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/uv}"
  "PYTHONUTF8=1" "PYTHONDONTWRITEBYTECODE=1")
python3 -m venv --without-pip "$check_home/plain"
"${check_env[@]}" "PATH=$check_home/plain/bin:$PATH" python3 tests/run_all.py
"${check_env[@]}" "PATH=$check_home/plain/bin:$PATH" bash tests/test_flush_hook.sh
"${check_env[@]}" uv run --with 'mcp<2' python tests/run_all.py
"${check_env[@]}" uv run --with 'mcp<2' bash tests/test_flush_hook.sh
