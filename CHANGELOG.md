# Changelog

## 0.1.0 — 2026-10-11

First public version.

- `/analyse-act` skill: six stages (Analyse, Observe, Inspect, Plan, Execute, Validate), with a human approval gate at Plan.
- `.aa/` context cache keyed to the git commit, with HIT/DELTA/MISS status from `aa-cache.sh`. Files that had uncommitted edits when scanned are rescanned on the next run.
- Complexity tiers S/M/L with default file and line caps, plus advisory token and time budgets.
- Session handling: a new task archives the previous session, and resuming with `--from` refuses to run a plan recorded for a different task.
- Claude Code plugin and marketplace manifests. Single-file Codex and Cursor adapters generated from `adapters/src/workflow.md`.
- Tests: `tests/aa-cache.test.sh` (cache script) and `tests/e2e-smoke.sh` (headless run of the real skill). CI runs shellcheck, the cache tests, schema validation, the adapter drift check and plugin validation.
