#!/bin/sh
# Spike C probe: log every cursor hook invocation's stdin + env, allow everything.
LOG="$HOME/.config/memhub-plugin/cursor-hook-probe.log"
IN=$(cat)
{
  echo "=== $(date '+%H:%M:%S') argv=[$*]"
  env | grep -iE "cursor|hook|plugin" | head -5
  printf '%s\n' "$IN"
} >> "$LOG" 2>&1
printf '{"permission":"allow"}\n'
