#!/bin/sh
# aa-cache.sh — report whether the .aa/ context cache can be reused.
#
# Usage: aa-cache.sh [repo_dir]          check the cache
#        aa-cache.sh --stamp [repo_dir]  record a finished scan: write .aa/cache_meta.json
#                                        (HEAD, UTC time, files rescanned) and .aa/dirty_at_scan.txt;
#                                        prints stamped=<hash> ("none" outside git / before 1st commit)
# Check output (key=value lines):
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

mode=check
[ "${1:-}" = "--stamp" ] && { mode=stamp; shift; }
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

# cached hash from cache_meta.json (flat JSON, read with sed)
read_cached() {
  sed -n 's/.*"git_commit_hash"[[:space:]]*:[[:space:]]*"\([0-9a-fA-F]*\)".*/\1/p' "$meta" | head -n 1
}

# sets $dirty and $changed (files changed since $cached, excluding .aa/)
compute_changed() {
  committed=""
  [ -n "$cached" ] && [ -n "$head" ] && committed=$(git -C "$dir" diff --name-only --no-renames "$cached" HEAD 2>/dev/null)
  # porcelain: "XY path" or "XY old -> new"; keep the new path
  dirty=$(git -C "$dir" status --porcelain --untracked-files=all 2>/dev/null | cut -c4- | sed 's/.* -> //; s/^"//; s/"$//' | grep -v '^\.aa/')
  scanned_dirty=""
  [ -f "$aa/dirty_at_scan.txt" ] && scanned_dirty=$(cat "$aa/dirty_at_scan.txt")
  changed=$(printf '%s\n%s\n%s\n' "$committed" "$dirty" "$scanned_dirty" | grep -v '^$' | grep -v '^\.aa/' | sort -u)
}

stamp() {
  mkdir -p "$aa"
  delta=""
  if [ -n "$cached" ]; then
    delta=$(printf '%s\n' "$changed" | grep -v '^$' | sed 's/\\/\\\\/g; s/"/\\"/g; s/.*/"&"/' | paste -sd, -)
  fi
  printf '{\n  "git_commit_hash": "%s",\n  "last_updated": "%s",\n  "modified_files_delta": [%s]\n}\n' \
    "${head:-none}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$delta" > "$meta"
  printf '%s\n' "$dirty" | grep -v '^$' > "$aa/dirty_at_scan.txt"
  echo "stamped=${head:-none}"
  exit 0
}

head=""
cached=""
changed=""
dirty=""

if [ "$mode" = stamp ]; then
  if git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    head=$(git -C "$dir" rev-parse --verify -q HEAD 2>/dev/null) || head=""
    if [ -f "$meta" ] && [ -n "$head" ]; then
      cached=$(read_cached)
      [ -n "$cached" ] && { git -C "$dir" cat-file -e "$cached^{commit}" 2>/dev/null || cached=""; }
    fi
    compute_changed
  fi
  stamp
fi

git -C "$dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 || report MISS not-a-git-repo
head=$(git -C "$dir" rev-parse --verify -q HEAD 2>/dev/null) || { head=""; report MISS no-commits; }
[ -f "$meta" ] || report MISS no-cache
[ -f "$aa/context.md" ] || report MISS no-context-md

cached=$(read_cached)
[ -n "$cached" ] || report MISS no-hash-in-cache
git -C "$dir" cat-file -e "$cached^{commit}" 2>/dev/null || report MISS cached-commit-not-found

compute_changed

if [ -z "$changed" ]; then
  count=0
else
  count=$(printf '%s\n' "$changed" | wc -l | tr -d ' ')
fi

[ "$count" -gt "$max" ] && report MISS "$count-files-changed-exceeds-$max"
[ -f "$aa/schema_cache.json" ] || report DELTA no-schema-cache
[ "$count" -eq 0 ] && report HIT up-to-date
report DELTA "$count-files-changed"
