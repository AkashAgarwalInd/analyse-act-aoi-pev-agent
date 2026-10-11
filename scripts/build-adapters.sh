#!/bin/sh
# Regenerate the single-file Codex and Cursor adapters from adapters/src/workflow.md.
# CI runs this and fails if adapters/ changes (drift check).
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
src="$root/adapters/src/workflow.md"

cp "$src" "$root/adapters/codex/AGENTS.md"

{
  printf -- '---\n'
  printf 'description: Analyse-Act (AOI-PEV) staged workflow; use only when the user asks for analyse-act / AOI-PEV / plan-first work\n'
  printf 'alwaysApply: false\n'
  printf -- '---\n\n'
  cat "$src"
} > "$root/adapters/cursor/analyse-act.mdc"

echo "adapters rebuilt from adapters/src/workflow.md"
