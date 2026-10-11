#!/bin/sh
# End-to-end smoke test: runs the real skill headlessly on a fixture repo.
# Costs real tokens (~2 agent runs). Usage: sh tests/e2e-smoke.sh
#   1. fresh repo, bug task   → cache MISS, .aa/ written, stops at [P], no source edits
#   2. new task, --analyse-only → cache HIT, previous session archived, no source edits
# shellcheck disable=SC2016  # check() evals its single-quoted condition later, on purpose
set -u
root=$(cd "$(dirname "$0")/.." && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
fails=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi; }

run() {
  (cd "$work/repo" && claude -p --plugin-dir "$root" --permission-mode acceptEdits \
    --allowedTools "Bash Read Write Edit Glob Grep Skill" --max-turns 40 "$1") > "$work/$2" 2>&1
}

mkdir -p "$work/repo" && (cd "$work/repo" && sh "$root/tests/fixtures/token-bug.sh")

echo "run 1: bug task on fresh repo"
run '/analyse-act:analyse-act "users get logged out about a minute before their token expires"' run1.txt
check "context.md written"         '[ -f "$work/repo/.aa/context.md" ]'
check "schema_cache.json written"  '[ -f "$work/repo/.aa/schema_cache.json" ]'
check "session written"            '[ -f "$work/repo/.aa/current_session.json" ]'
check "reached plan gate"          'grep -q "\[P\] PLAN" "$work/run1.txt"'
check "offered approach B"         'grep -q "\[B\]" "$work/run1.txt"'
check "no source edits"            'git -C "$work/repo" diff --quiet'

: > "$work/repo/.aa/dirty_at_scan.txt"   # run 1 scanned a clean tree

echo "run 2: new task, --analyse-only"
run '/analyse-act:analyse-act "sessionCleanup has no tests; what would it take to cover it?" --analyse-only' run2.txt
check "printed [I] banner"         'grep -q "\[I\] INSPECT" "$work/run2.txt"'
check "cache HIT reported"         'grep -q "HIT" "$work/run2.txt"'
check "skipped [A] banner"         '! grep -q "\[A\] ANALYSE" "$work/run2.txt"'
check "old session archived"       'ls "$work/repo/.aa/sessions/"*.json >/dev/null 2>&1'
check "stopped before [P]"         '! grep -q "\[P\] PLAN" "$work/run2.txt"'
check "no source edits"            'git -C "$work/repo" diff --quiet'

if [ "$fails" -ne 0 ]; then
  echo "$fails failed — transcripts: $work (kept)"; trap - EXIT; exit 1
fi
echo "all passed"
