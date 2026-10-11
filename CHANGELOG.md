# Changelog

## 0.2.0 — 2026-10-11

- `aa-cache.sh --stamp` writes `cache_meta.json` and `dirty_at_scan.txt` from git and the clock, so the model no longer writes them by hand.
- Fast path: a trivial task (one file, 5 lines or fewer) skips the stages, shows the exact diff and waits for `y`. Use `--full` to opt out.
- `--branch` creates `aa/<task-slug>` at the start of [E]. The skill still never commits.
- Stage banners are required in every mode, including `--analyse-only`.
- GitHub Copilot and Aider adapters, generated like the Codex and Cursor ones. The CI drift check now also catches untracked adapter files.
- `langgraph_agent/`: the headless `aa-agent` CLI for CI. It is built on LangGraph, checkpoints to SQLite, pauses for approval with `interrupt()` at [P], uses repo-confined tools, and has offline tests in CI. There is also an example GitHub Actions workflow.
- The e2e smoke test has a third run for the fast path.

## 0.1.0 — 2026-10-11

First public version.

- `/analyse-act` skill: six stages (Analyse, Observe, Inspect, Plan, Execute, Validate), with a human approval gate at Plan.
- `.aa/` context cache keyed to the git commit, with HIT/DELTA/MISS status from `aa-cache.sh`. Files that had uncommitted edits when scanned are rescanned on the next run.
- Complexity tiers S/M/L with default file and line caps, plus advisory token and time budgets.
- Session handling: a new task archives the previous session, and resuming with `--from` refuses to run a plan recorded for a different task.
- Claude Code plugin and marketplace manifests. Single-file Codex and Cursor adapters generated from `adapters/src/workflow.md`.
- Tests: `tests/aa-cache.test.sh` (cache script) and `tests/e2e-smoke.sh` (headless run of the real skill). CI runs shellcheck, the cache tests, schema validation, the adapter drift check and plugin validation.
