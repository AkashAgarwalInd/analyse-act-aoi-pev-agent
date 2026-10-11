# Changelog

## Unreleased

- Guided mode (`--guided`, skill and `aa-agent`): the run also stops after each analysis stage that ran ([A], [O], a DELTA rescan, [I]) so you can review it, add context or stop. Context reaches every later stage's prompt (`aa-agent resume --note "…"`, which continues, or `n` with `-i`), and is saved on that stage's `stage_log` entry in `.aa/current_session.json`, so it carries over between the skill and `aa-agent` in both directions. A pause shows as `status=awaiting_stage_decision`. The example CI workflow has `guided` and `note` inputs. A trivial task still takes the fast path unless `--full` is given. The default stays approval-gated, and no mode skips the [P] gate.
- Clickable gates: in Claude Code, the [P] plan gate uses AskUserQuestion, so you can click an approach (with a preview of its files, LOC and risk) or type changes to the plan. The fast-path, drift and budget gates are clickable too. The text reply still works, and is what headless runs and the other editors use.
- `aa-agent run -i` / `resume -i` answers each pause in the terminal and carries on in the same process. `q` leaves the thread paused. It turns itself off without a TTY or in CI.
- `aa-agent resume --edit` at a budget or drift pause is now an error instead of being silently ignored.
- At a drift pause, `-i` lists each changed file with its LOC, and `d` shows the full diff.
- A stage that crashed or was stopped with Ctrl-C can be re-run with `aa-agent resume --thread <id>`; `status` reports it as `interrupted`. A crash during [E] is never retried, since its half-made edits would hide from the drift check. A crash or Ctrl-C prints `status=interrupted thread=<id>`, so the example CI workflow saves the thread, and it now restores artifacts from failed runs too: rerun it with only the thread id to retry. Ctrl-C exits 130 instead of printing a traceback.
- README: demo recordings of the clickable gates and `aa-agent -i` (`docs/demo/`).
- Without `-i`, a drift pause now lists each changed file with its LOC as well.

## 0.2.0 — 2026-10-11

- `aa-cache.sh --stamp` writes `cache_meta.json` and `dirty_at_scan.txt` from git and the clock, so the model no longer writes them by hand.
- Fast path: a trivial task (one file, 5 lines or fewer, never a dependency bump) skips the stages, shows the exact diff and waits for `y`. Use `--full` to opt out.
- `--branch` creates `aa/<task-slug>` at the start of [E]. The skill still never commits.
- Stage banners are required in every mode, including `--analyse-only`.
- GitHub Copilot and Aider adapters, generated like the Codex and Cursor ones. The CI drift check now also catches untracked adapter files.
- `langgraph_agent/`: the headless `aa-agent` CLI for CI, built on LangGraph with SQLite checkpoints.
  - It pauses with `interrupt()` at the [P] gate. The human can choose an approach and also edit it (`--edit`).
  - It pauses for a continue/stop decision when a stage runs more than 1.5× its token budget, or when the [E] diff departs from the plan or the tier caps.
  - The cache works as it does in the skill: HIT, DELTA (rescan only the changed files) or MISS, plus `--refresh`.
  - The tools stay inside the repo. Git is read-only with an option allowlist, and only `--check` commands can run.
  - `resume` refuses if HEAD has moved since the plan was made.
  - It shares `.aa/current_session.json` with the skill (`run --from-session`).
  - There is an example GitHub Actions workflow, and the offline tests run in CI.
- The e2e smoke test covers the fast path, approving it with `y`, and `--branch`.
- CI installs check-jsonschema so the stamped `cache_meta.json` is validated against its schema, and the shell scripts are shellcheck-clean.

## 0.1.0 — 2026-10-11

First public version.

- `/analyse-act` skill: six stages (Analyse, Observe, Inspect, Plan, Execute, Validate), with a human approval gate at Plan.
- `.aa/` context cache keyed to the git commit, with HIT/DELTA/MISS status from `aa-cache.sh`. Files that had uncommitted edits when scanned are rescanned on the next run.
- Complexity tiers S/M/L with default file and line caps, plus advisory token and time budgets.
- Session handling: a new task archives the previous session, and resuming with `--from` refuses to run a plan recorded for a different task.
- Claude Code plugin and marketplace manifests. Single-file Codex and Cursor adapters generated from `adapters/src/workflow.md`.
- Tests: `tests/aa-cache.test.sh` (cache script) and `tests/e2e-smoke.sh` (headless run of the real skill). CI runs shellcheck, the cache tests, schema validation, the adapter drift check and plugin validation.
