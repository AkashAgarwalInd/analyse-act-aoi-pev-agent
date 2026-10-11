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

`context.md` may end with a `## Human corrections` section: notes the human gave after [A]/[O] in guided mode. They override the findings above them. When you patch or rewrite `context.md`, keep that section as it is, unless the rescan shows a correction no longer holds (then drop that line and say so).

## Reading the script output

`scripts/aa-cache.sh` compares `.aa/cache_meta.json → git_commit_hash` with `HEAD`, and counts files changed since then — committed **and** uncommitted, excluding `.aa/` itself.

- **HIT** (`reason=up-to-date`): load `context.md` + `schema_cache.json`; skip [A] and [O]. If `head` ≠ `cached` (e.g. an empty commit), just run `--stamp`.
- **DELTA** (1–5 files, threshold via `AA_DELTA_MAX`):
  1. Read the cached artifacts.
  2. Read only the `changed=` files (deleted files: drop them from the artifacts).
  3. Patch affected sections of `context.md` / entries of `schema_cache.json`. If a change touches a manifest (`package.json`, `pyproject.toml`, `go.mod`, `Cargo.toml`, …) or adds a new top-level directory, treat as MISS instead.
  4. Run `aa-cache.sh --stamp .` to record the new hash and delta.
- **MISS** (`no-cache`, `not-a-git-repo`, `cached-commit-not-found`, or > threshold): run [A] then [O] fully.
  - In a non-git directory, still write `.aa/`; `--stamp` records `git_commit_hash: "none"`, so every future run will be MISS unless the user passes `--from I`.

## Recording a scan: `aa-cache.sh --stamp`

After [A]/[O] or a delta update, **don't write `cache_meta.json` or `dirty_at_scan.txt` yourself**. Run:

```sh
sh "${CLAUDE_SKILL_DIR}/scripts/aa-cache.sh" --stamp .
```

It writes both files deterministically and prints `stamped=<hash>`:

- `cache_meta.json` (schema: `schemas/cache_meta.schema.json`, kept flat because the script reads it with `sed`): `git_commit_hash` = `git rev-parse HEAD` (`"none"` outside git or before the first commit), `last_updated` from the UTC clock, `modified_files_delta` = the files changed since the previous stamp.
- `dirty_at_scan.txt`: files with uncommitted edits right now (excluding `.aa/`), one per line, empty if the tree is clean. The script always rescans these next time, so content from edits that were later reverted can't linger as a false HIT.

Stamp only **after** the artifacts are written. It never touches `context.md`, `schema_cache.json` or the session file.
