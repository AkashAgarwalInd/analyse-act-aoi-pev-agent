#!/bin/sh
# Tests for skills/analyse-act/scripts/aa-cache.sh. Usage: sh tests/aa-cache.test.sh
set -u
root=$(cd "$(dirname "$0")/.." && pwd)
script="$root/skills/analyse-act/scripts/aa-cache.sh"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
fails=0

# expect <name> <status> <reason> [changed-count]
expect() {
  out=$(sh "$script" "$repo")
  status=$(printf '%s\n' "$out" | sed -n 's/^status=//p')
  reason=$(printf '%s\n' "$out" | sed -n 's/^reason=//p')
  n=$(printf '%s\n' "$out" | grep -c '^changed=')
  if [ "$status" = "$2" ] && [ "$reason" = "$3" ] && { [ $# -lt 4 ] || [ "$n" = "$4" ]; }; then
    echo "ok   $1"
  else
    echo "FAIL $1: got status=$status reason=$reason changed=$n"; printf '%s\n' "$out" | sed 's/^/     /'
    fails=$((fails + 1))
  fi
}

write_cache() {
  mkdir -p "$repo/.aa"
  echo ctx > "$repo/.aa/context.md"
  echo '{}' > "$repo/.aa/schema_cache.json"
  printf '{\n  "git_commit_hash": "%s",\n  "last_updated": "2026-01-01T00:00:00Z"\n}\n' \
    "$(git -C "$repo" rev-parse HEAD)" > "$repo/.aa/cache_meta.json"
}

repo="$work/plain"; mkdir -p "$repo"
expect "non-git dir" MISS not-a-git-repo

repo="$work/repo"; mkdir -p "$repo"; cd "$repo" || exit 1
git init -q; git config user.email t@t; git config user.name t
expect "no commits" MISS no-commits

for i in 1 2 3 4 5 6 7; do echo "$i" > "f$i.txt"; done
git add .; git commit -qm init
expect "no cache" MISS no-cache

write_cache
expect "hit" HIT up-to-date 0

rm .aa/schema_cache.json
expect "missing schema cache" DELTA no-schema-cache 0
echo '{}' > .aa/schema_cache.json

echo x >> f1.txt; git mv f2.txt g2.txt; git commit -qam c2; echo y >> f3.txt
expect "delta (rename counts old+new)" DELTA 4-files-changed 4

mkdir newdir; echo a > newdir/a; echo b > newdir/b
expect "untracked dir counts each file" MISS 6-files-changed-exceeds-5 6
AA_DELTA_MAX=10 expect "threshold override" DELTA 6-files-changed 6
rm -rf newdir

git add -A; git commit -qm c3; write_cache
echo f5.txt > .aa/dirty_at_scan.txt
expect "dirty-at-scan file rescanned" DELTA 1-files-changed 1
rm .aa/dirty_at_scan.txt

echo z > "sp ace.txt"
expect "path with space" DELTA 1-files-changed 1
rm "sp ace.txt"

sed 's/"git_commit_hash": "[^"]*"/"git_commit_hash": "deadbeef"/' .aa/cache_meta.json > m && mv m .aa/cache_meta.json
expect "unknown cached commit" MISS cached-commit-not-found

if [ "$fails" -ne 0 ]; then echo "$fails failed"; exit 1; fi
echo "all passed"
