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
/analyse-act "<task>" --full          # never take the fast path, even for a trivial change
/analyse-act "<task>" --branch        # create branch aa/<task-slug> at the start of [E]
/analyse-act "<task>" --guided        # also pause after [A], [O], [I] to review each or add context
```

(Installed as a plugin, the command is `/analyse-act:analyse-act`.)

**This invocation:** `$ARGUMENTS`
Parse it as: the task text (quoted or not) plus any flags above. If there is no task text and no `--from`, ask for the task before doing anything.

**Modes.** By default the run is approval-gated: [A]/[O]/[I] run without stopping (reusing the cache), and the run stops at [P]. `--guided` also stops after each analysis stage that ran ([A], [O], a DELTA rescan, [I]) so the human can review it, add context or stop. No mode skips the [P] gate.

A bare reply of `A`, `B`, … after the [P] gate means "approved, execute that approach". A bare `y` after the fast-path gate means "apply that change"; `full` means "run all stages instead".

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

`--refresh` forces MISS. Before acting on the status, check the **Fast path** below: a trivial task skips scanning whatever the status.

After any scan ([A]/[O] or a DELTA patch), record it with `sh "${CLAUDE_SKILL_DIR}/scripts/aa-cache.sh" --stamp .` — never hand-write `cache_meta.json` or `dirty_at_scan.txt`.

## Session (`.aa/current_session.json`)

- **New task** (no `--from`): if a session file exists for a different task, move it to `.aa/sessions/<YYYYMMDD-HHMMSS>.json`, then start a fresh one.
- **`--from <stage>`**: load the session. If it is missing, or its `task` doesn't match the one given, say so and ask whether to start over from [I] — never execute a plan recorded for another task. Read every `note` on its `stage_log` entries (from guided pauses, yours or `aa-agent`'s): they are the human's corrections and override the earlier findings in every stage you run.
- Append to `stage_log` at the end of each stage (schema: `schemas/session.schema.json`).
- The headless runner (`aa-agent`) reads and writes the same file. It writes it at each guided stage pause too, so a run stopped there can be continued here with `--from <next stage>`. A session it left at `awaiting_approval` can be continued here with `--from P`, and `aa-agent run --from-session` continues one you left at the [P] gate.
- Get every timestamp (session archive names, `at`, `last_updated`) from the clock: `date -u +%Y%m%d-%H%M%S` for file names, `date -u +%Y-%m-%dT%H:%M:%SZ` for JSON fields. Never invent times.

## Tier pre-assessment (right after Stage 0)

Guess a tier from the task text and the cached context (one line, e.g. `tier S (pre-assessed): one-function bug`). It sets the budget for [A]/[O] and whether [O] can be skipped. [I] confirms or changes it with evidence.

## Fast path (trivial changes)

Right after the tier pre-assessment, decide whether the task is trivial. It is only if **all** hold:
- one file, ≤5 changed lines;
- no behaviour change beyond the literal ask: a typo, a rename local to one file, a one-line config value;
- you can name the exact location without scanning the repo.

Dependency bumps never qualify: they touch the manifest and a lockfile, and can change behaviour. If anything is uncertain, or `--full` is set, run the normal stages. `--guided` doesn't change this: a trivial task still takes the fast path (there are no analysis stages to pause after); add `--full` to get the guided stages. Otherwise:

```
━━ [F] FAST PATH · cache HIT ━━
Change: README.md:2 — "teh" → "the"
<the exact diff>
Reply y to apply, or "full" for the full AOI-PEV run.
```

**STOP** and wait (see **Gates**). Don't run [A]/[O]/[I]/[P], don't write `.aa/` or a session file. On `y`: run [E] (with `--branch` if given) and [V] limited to targeted tests or lint, still one retry. On `full`: start at Stage 0 as a normal run.

## Gates (every place the run waits for the human)

Always print the text first: the full report and the usual reply line (`Reply A or B…`, `Reply y…`, `continue?`). Then, **if the AskUserQuestion tool is available**, call it once so the human can click instead of typing. Its answer is the human's reply: act on it in this same turn exactly as if they had typed it. If the tool isn't available, or the call is denied, fails or comes back without an answer, end the turn and wait for the typed reply as before. Never call it again for the same gate, and never treat a missing answer as approval.

| Gate | header | options (recommended first, its label ending in "(Recommended)") | preview |
|---|---|---|---|
| [P] plan | `Approach` | one per approach, `A · <title>`; at most 4, so with more show the top 4 (the rest stay in the text matrix) | files, est. LOC, risk, a 3–6 line sketch of the change |
| Fast path | `Fast path` | `Apply`, `Full run` | the exact diff |
| Drift ([E]) | `Drift` | `Continue`, `Stop and hand off` | what drifted: the extra files, LOC or scope |
| Over budget | `Budget` | `Continue`, `Stop` | stage, tokens used vs budget |
| Stage (`--guided`) | `Stage` | `Continue`, `Stop` | the stage summary |

An option means the same as the typed reply (`A`, `y`, `full`, `continue`, `stop`). Free text ("Other") at [P] that is just an approach id (`C`, `b`) chooses that approach, including one beyond the four shown. Any other free text is a plan edit: record it as `plan_edit`, choose the approach it names (else the recommended one), and echo the edited approach in one line before [E]. If an edit adds files, LOC or a dependency the approach didn't have, show the changed approach and ask again instead of executing. At a guided stage gate, free text other than `continue`/`stop` is context: record it as `note` on that stage's `stage_log` entry (after [A], [O] or a DELTA rescan, also append `- after [A]: <note>` or `- after [O]: <note>` under a `## Human corrections` heading at the end of `.aa/context.md`, so a later cache HIT keeps it), treat it as a correction that overrides your earlier findings from the next stage on, and continue. Free text at any other gate is read as a typed reply. Clicking is approval for rule 1; nothing changes before it.

## Stage banner (print at the start of every stage)

```
━━ [I] INSPECT · tier M · cache HIT · stage budget ~12k tok ━━
```

This applies in every mode, including the fast path, `--analyse-only` and `--from`: the banner is how the run (and the e2e test) reports cache status, so never fold a stage into a free-form answer.

End every stage with a 1–3 line **Stage summary** and `→ next: [X]`. Keep stage output tight: tables, bullets, Mermaid diagrams — no essays.

## Stages — load the reference only when entering that stage

| Stage | Reference | Output | Stop? |
|---|---|---|---|
| [A] Analyse  | `references/analyse.md`  | `.aa/context.md` (stack, module map, HLD Mermaid) | only if `--guided` |
| [O] Observe  | `references/observe.md`  | `.aa/schema_cache.json` + LLD/flow diagram for the relevant area | only if `--guided` |
| [I] Inspect  | `references/inspect.md`  | RCA, Blast Radius Matrix, tier → `.aa/current_session.json` | if `--guided` or `--analyse-only` |
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

Budgets are **advisory** — you can't meter tokens exactly, so estimate from files read and output produced. At a stage boundary, if you are clearly over (≈1.5×), say `⚠ over budget (stage I ~2×) — continue?` and wait (see **Gates**). Never cut corners silently to fit.

Caps are defaults. Exceeding one is allowed **only** if the [P] matrix flags it (`⚠ exceeds tier M cap: 5 files`) and the human approves it.

## Non-negotiable rules

1. **No code before approval** — the human's [P] choice, or `y` at the fast-path gate. [A]–[P] are read-only on source files (writing `.aa/` is fine).
2. **Root cause before solution.** [I] must name the cause with `file:line` evidence, or explicitly say it is unconfirmed and what would confirm it.
3. **≥2 genuinely different approaches** at [P], one marked *(Recommended)*. "Do nothing / config-only" counts if real.
4. **Simplest thing that works.** No new dependencies without explicit confirmation. No new helper/abstraction unless used at ≥2 call sites. No drive-by refactors or formatting changes.
5. **One self-review pass** in [E]; **one auto-fix retry** in [V]. After that, stop looping — report and hand off a Manual Verification Checklist.
6. **Drift = pause.** If execution needs files, behaviour or scope not in the approved plan, stop and ask before continuing.
7. **Never commit, push, or edit `.gitignore`** unless the user asks. Create a branch only with `--branch` (see `references/execute.md`).
8. **Ask the human** for manual testing when automation would need heavy setup (real services, UI, devices) or wouldn't give real confidence.
9. **No secrets in `.aa/`.** Never copy values of env vars, keys, tokens, passwords or connection strings into `.aa/` files — `context.md` is meant to be committed. Write names only (`DATABASE_URL`), never values.
