# Caching protocol — `.aa/`

Goal: never re-scan a repo you already understand. `.aa/` lives at the **target repo root**.

| File | Written by | Purpose | Commit it? |
|---|---|---|---|
| `cache_meta.json`     | after [A]/[O] or a delta | commit hash the cache reflects | yes (with context.md) |
| `context.md`          | [A] | stack, module map, HLD | **recommended** — shared team context |
| `schema_cache.json`   | [O] | models, entrypoints, flows, test commands | optional |
| `dirty_at_scan.txt`   | after [A]/[O] or a delta | files that had uncommitted edits when scanned | with cache_meta |
| `context.<pkg>.md`    | [A], monorepos only | per-package context, created on demand | like context.md |
| `current_session.json`| [I] [P] [E] [V] | this task's state for resuming | **no** — local only |
| `sessions/`           | new task | archived previous sessions | **no** |

On first write, *suggest* (do not apply) this `.gitignore` line to the user:

```
.aa/current_session.json
.aa/sessions/
```

Never write secret values (env values, keys, tokens, connection strings) into any `.aa/` file — names only.

## Reading the script output

`scripts/aa-cache.sh` compares `.aa/cache_meta.json → git_commit_hash` with `HEAD`, and counts files changed since then — committed **and** uncommitted, excluding `.aa/` itself.

- **HIT** (`reason=up-to-date`): load `context.md` + `schema_cache.json`; skip [A] and [O]. If `head` ≠ `cached` (e.g. an empty commit), just update the hash.
- **DELTA** (1–5 files, threshold via `AA_DELTA_MAX`):
  1. Read the cached artifacts.
  2. Read only the `changed=` files (deleted files: drop them from the artifacts).
  3. Patch affected sections of `context.md` / entries of `schema_cache.json`. If a change touches a manifest (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, …) or adds a new top-level directory, treat as MISS instead.
  4. Rewrite `cache_meta.json` with the new hash and `modified_files_delta`.
- **MISS** (`no-cache`, `not-a-git-repo`, `cached-commit-not-found`, or > threshold): run [A] then [O] fully.
  - In a non-git directory, still write `.aa/`, but set `git_commit_hash` to `"none"`; every future run will be MISS unless the user passes `--from I`.

## Writing `cache_meta.json`

Validate against `schemas/cache_meta.schema.json`. Keep it flat — the script reads it with `sed`.

```json
{
  "git_commit_hash": "<output of git rev-parse HEAD>",
  "last_updated": "2026-10-11T10:00:00Z",
  "modified_files_delta": ["src/auth/service.ts"]
}
```

## Writing `dirty_at_scan.txt`

The hash records HEAD, but if you scanned files with uncommitted edits, the cache reflects those edits. Whenever you write `cache_meta.json`, also write `.aa/dirty_at_scan.txt` with the output of:

```sh
git status --porcelain --untracked-files=all | cut -c4- | sed 's/.* -> //' | grep -v '^\.aa/'
```

(one path per line; write an empty file if the tree was clean). The script always rescans these, so content from edits that were later reverted can't linger as a false HIT.
