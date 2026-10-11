#!/bin/sh
# aa-cache.sh — report whether the .aa/ context cache can be reused.
#
# Usage: aa-cache.sh [repo_dir]
# Output (key=value lines):
#   status=HIT|DELTA|MISS
#   reason=<why>
#   head=<current HEAD or empty>
#   cached=<hash in .aa/cache_meta.json or empty>
#   changed=<path>        (zero or more lines; files to rescan)
#
# "changed" = files changed since the cached hash (committed + uncommitted, incl. every
# untracked file) + files listed in .aa/dirty_at_scan.txt (uncommitted when last scanned,
# so the cache may hold content that has since been reverted).
#
# Env: AA_DELTA_MAX (default 5) — max changed files still treated as DELTA.

dir=${1:-.}
max=${AA_DELTA_MAX:-5}
aa="$dir/.aa"
meta="$aa/cache_meta.json"

report() {
  echo "status=$1"
  echo "reason=$2"
  echo "head=$head"
  echo "cached=$cached"
  [ -n "$changed" ] && printf '%s\n' "$changed" | sed 's/^/changed=/'
  exit 0
}

head=""
cached=""
changed=""

git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 || report MISS not-a-git-repo
head=$(git -C "$dir" rev-parse --verify -q HEAD 2>/dev/null) || { head=""; report MISS no-commits; }
[ -f "$meta" ] || report MISS no-cache
[ -f "$aa/context.md" ] || report MISS no-context-md

cached=$(sed -n 's/.*"git_commit_hash"[[:space:]]*:[[:space:]]*"\([0-9a-fA-F]*\)".*/\1/p' "$meta" | head -n 1)
[ -n "$cached" ] || report MISS no-hash-in-cache
git -C "$dir" cat-file -e "$cached^{commit}" 2>/dev/null || report MISS cached-commit-not-found

committed=$(git -C "$dir" diff --name-only --no-renames "$cached" HEAD 2>/dev/null)
# porcelain: "XY path" or "XY old -> new"; keep the new path
dirty=$(git -C "$dir" status --porcelain --untracked-files=all 2>/dev/null | cut -c4- | sed 's/.* -> //; s/^"//; s/"$//')
scanned_dirty=""
[ -f "$aa/dirty_at_scan.txt" ] && scanned_dirty=$(cat "$aa/dirty_at_scan.txt")
changed=$(printf '%s\n%s\n%s\n' "$committed" "$dirty" "$scanned_dirty" | grep -v '^$' | grep -v '^\.aa/' | sort -u)

if [ -z "$changed" ]; then
  count=0
else
  count=$(printf '%s\n' "$changed" | wc -l | tr -d ' ')
fi

[ "$count" -gt "$max" ] && report MISS "$count-files-changed-exceeds-$max"
[ -f "$aa/schema_cache.json" ] || report DELTA no-schema-cache
[ "$count" -eq 0 ] && report HIT up-to-date
report DELTA "$count-files-changed"
