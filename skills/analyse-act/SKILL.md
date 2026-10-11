---
name: analyse-act
description: "Staged engineering workflow (AOI-PEV) for bug fixes, features, refactors and root-cause analysis: Analyse repo → Observe data flow → Inspect root cause & blast radius → Plan ≥2 approaches (human approval gate) → Execute minimal diff → Validate edge cases. Use when the user runs /analyse-act, asks to 'use AOI-PEV' or 'analyse-act', or wants a careful, plan-first fix/feature with RCA before coding. Caches repo context in .aa/ keyed by git commit so repeat runs skip re-scanning."
---

# /analyse-act — Analyse → Act (AOI-PEV)

Two pillars, six stages. Each stage does one job, shows its output, and hands off.

```
ANALYSE (understand)                 ACT (change)
[A] Analyse  → [O] Observe → [I] Inspect  ║  [P] Plan ──(human gate)──→ [E] Execute → [V] Validate
 repo & HLD     data & LLD    RCA & blast ║   ≥2 approaches              minimal diff   tests & edge cases
```

## Usage

```
/analyse-act "<task>"                 # full run; stops at the [P] gate for approval
/analyse-act "<task>" --analyse-only  # stop after [I] (RCA / impact report only)
/analyse-act "<task>" --from P        # resume at a stage (A|O|I|P|E|V) using .aa/current_session.json
/analyse-act "<task>" --tier S|M|L    # force complexity tier instead of auto-assigning
/analyse-act "<task>" --refresh       # ignore cache, run [A] and [O] fully
```

(Installed as a plugin, the command is `/analyse-act:analyse-act`.)

**This invocation:** `$ARGUMENTS`
Parse it as: the task text (quoted or not) plus any flags above. If there is no task text and no `--from`, ask for the task before doing anything.

A bare reply of `A`, `B`, … after the [P] gate means "approved, execute that approach".

## Stage 0 — Cache check (always first)

Run the bundled script from the target repo root:

```sh
sh "${CLAUDE_SKILL_DIR}/scripts/aa-cache.sh" .
```

If `${CLAUDE_SKILL_DIR}` is empty in your environment, use this skill's base directory (the folder containing this SKILL.md).

It prints `status=HIT|DELTA|MISS`, `reason=`, `head=`, `cached=`, and `changed=<path>` lines. Then read `references/caching.md` only if status is DELTA or MISS, or `.aa/` needs writing.

| status | Action |
|---|---|
| HIT   | Read `.aa/context.md` + `.aa/schema_cache.json`. **Skip [A] and [O].** Go to [I]. |
| DELTA | Read the cached files, rescan only the `changed=` files, patch the artifacts, update the hash. `reason=no-schema-cache` → run [O] only. Go to [I]. |
| MISS  | Run [A] and [O] fully, write `.aa/`. |

`--refresh` forces MISS.

## Session (`.aa/current_session.json`)

- **New task** (no `--from`): if a session file exists for a different task, move it to `.aa/sessions/<YYYYMMDD-HHMMSS>.json`, then start a fresh one.
- **`--from <stage>`**: load the session. If it is missing, or its `task` doesn't match the one given, say so and ask whether to start over from [I] — never execute a plan recorded for another task.
- Append to `stage_log` at the end of each stage (schema: `schemas/session.schema.json`).
- Get every timestamp (session archive names, `at`, `last_updated`) from the clock: `date -u +%Y%m%d-%H%M%S` for file names, `date -u +%Y-%m-%dT%H:%M:%SZ` for JSON fields. Never invent times.

## Tier pre-assessment (right after Stage 0)

Guess a tier from the task text and the cached context (one line, e.g. `tier S (pre-assessed): one-function bug`). It sets the budget for [A]/[O] and whether [O] can be skipped. [I] confirms or changes it with evidence.

## Stage banner (print at the start of every stage)

```
━━ [I] INSPECT · tier M · cache HIT · stage budget ~12k tok ━━
```

This applies in every mode, including `--analyse-only` and `--from`: the banner is how the run (and the e2e test) reports cache status, so never fold a stage into a free-form answer.

End every stage with a 1–3 line **Stage summary** and `→ next: [X]`. Keep stage output tight: tables, bullets, Mermaid diagrams — no essays.

## Stages — load the reference only when entering that stage

| Stage | Reference | Output | Stop? |
|---|---|---|---|
| [A] Analyse  | `references/analyse.md`  | `.aa/context.md` (stack, module map, HLD Mermaid) | no |
| [O] Observe  | `references/observe.md`  | `.aa/schema_cache.json` + LLD/flow diagram for the relevant area | no |
| [I] Inspect  | `references/inspect.md`  | RCA, Blast Radius Matrix, tier → `.aa/current_session.json` | only if `--analyse-only` |
| [P] Plan     | `references/plan.md`     | ≥2 approaches + Decision Matrix | **ALWAYS STOP — wait for human** |
| [E] Execute  | `references/execute.md`  | Minimal diff + one self-review | stop if drifting from plan |
| [V] Validate | `references/validate.md` | Requirement check, edge cases, tests, manual checklist, final card | end |

## Complexity tiers (pre-assessed after Stage 0, confirmed at [I], shown in the [P] matrix)

| Tier | Max files | Max net LOC | Approaches | Run budget (guide) | Wall clock (guide) | Notes |
|---|---|---|---|---|---|---|
| S | 2 | 50  | 2 (brief) | ~20k tok  | ~5 min  | [O] may be skipped if the cache already covers the affected area |
| M | 3 | 150 | 2+        | ~60k tok  | ~15 min | default |
| L | 8 | 400 | 2+        | ~150k tok | ~40 min | must propose splitting into phases, each its own P→E→V cycle |

**Stage budget = run budget × share:** A 15% · O 20% · I 20% · P 10% · E 20% · V 15%. With a cache HIT, A/O cost ~0 and their share moves to I and E.

Budgets are **advisory** — you can't meter tokens exactly, so estimate from files read and output produced. At a stage boundary, if you are clearly over (≈1.5×), say `⚠ over budget (stage I ~2×) — continue?` and wait. Never cut corners silently to fit.

Caps are defaults. Exceeding one is allowed **only** if the [P] matrix flags it (`⚠ exceeds tier M cap: 5 files`) and the human approves it.

## Non-negotiable rules

1. **No code before approval.** [A]–[P] are read-only on source files (writing `.aa/` is fine).
2. **Root cause before solution.** [I] must name the cause with `file:line` evidence, or explicitly say it is unconfirmed and what would confirm it.
3. **≥2 genuinely different approaches** at [P], one marked *(Recommended)*. "Do nothing / config-only" counts if real.
4. **Simplest thing that works.** No new dependencies without explicit confirmation. No new helper/abstraction unless used at ≥2 call sites. No drive-by refactors or formatting changes.
5. **One self-review pass** in [E]; **one auto-fix retry** in [V]. After that, stop looping — report and hand off a Manual Verification Checklist.
6. **Drift = pause.** If execution needs files, behaviour or scope not in the approved plan, stop and ask before continuing.
7. **Never commit, push, or edit `.gitignore`** unless the user asks.
8. **Ask the human** for manual testing when automation would need heavy setup (real services, UI, devices) or wouldn't give real confidence.
9. **No secrets in `.aa/`.** Never copy values of env vars, keys, tokens, passwords or connection strings into `.aa/` files — `context.md` is meant to be committed. Write names only (`DATABASE_URL`), never values.
