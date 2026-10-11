#!/bin/sh
# Regenerate the single-file Codex, Cursor, Copilot and Aider adapters from adapters/src/workflow.md.
# CI runs this and fails if adapters/ changes (drift check).
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
src="$root/adapters/src/workflow.md"

mkdir -p "$root/adapters/codex" "$root/adapters/cursor" "$root/adapters/copilot" "$root/adapters/aider"
cp "$src" "$root/adapters/codex/AGENTS.md"
cp "$src" "$root/adapters/copilot/copilot-instructions.md"
cp "$src" "$root/adapters/aider/CONVENTIONS.md"

{
  printf -- '---\n'
  printf 'description: Analyse-Act (AOI-PEV) staged workflow; use only when the user asks for analyse-act / AOI-PEV / plan-first work\n'
  printf 'alwaysApply: false\n'
  printf -- '---\n\n'
  cat "$src"
} > "$root/adapters/cursor/analyse-act.mdc"

echo "adapters rebuilt from adapters/src/workflow.md"
