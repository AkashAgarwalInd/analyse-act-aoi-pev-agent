# Analyse-Act (AOI-PEV) workflow

**When to use:** only when the user asks for it, by saying "analyse-act", "AOI-PEV", "/analyse-act" or "plan first". Don't apply it on your own to other tasks.

**Fast path:** if the task is trivial (one file, ≤5 lines, no behaviour change beyond the literal ask: a typo, a local rename, a patch/minor dependency bump, a one-line config value) and the user didn't say "full", skip the stages. Print `━━ [F] FAST PATH ━━`, the `file:line` and the exact diff, end with `Reply y to apply, or "full" for the full AOI-PEV run.` and stop. On `y`, make the change and run only the targeted test or lint.

Work in six stages. Print a banner at the start of each stage (`━━ [I] INSPECT · tier M ━━`) and finish each one with a 1–3 line summary and `→ next: [X]`. Keep output to tables, bullets and Mermaid diagrams.

## Stage 0: Cache
- `.aa/` lives at the repo root. Compare `.aa/cache_meta.json → git_commit_hash` with `git rev-parse HEAD`, then count changed files: `git diff --name-only --no-renames <hash> HEAD` plus `git status --porcelain`, ignoring `.aa/`.
- **HIT** (0 changed): load `.aa/context.md` and `.aa/schema_cache.json`, then skip A and O.
- Also add the paths listed in `.aa/dirty_at_scan.txt` (files that had uncommitted edits when last scanned) to the changed set.
- **DELTA** (1–5 changed, no manifest changes): rescan only those files, patch the artifacts and update the hash. If `schema_cache.json` is missing, run [O].
- **MISS**: run A and O in full.
- After any scan, rewrite `cache_meta.json` (`git_commit_hash`, `last_updated`) and `dirty_at_scan.txt` (the current `git status --porcelain --untracked-files=all` paths).
- **Session:** `.aa/current_session.json` holds the task, tier, RCA, approaches, chosen approach and a stage log. When a new task starts, move an existing session to `.aa/sessions/<timestamp>.json`. When resuming, check that the task matches. It stays local: suggest gitignoring it and `.aa/sessions/`, but never edit `.gitignore` yourself.
- **Tier pre-assessment:** guess S/M/L from the task now and confirm it at [I].

## [A] Analyse: repo context
Read the manifests, the top two directory levels and the existing docs. Write `.aa/context.md` containing:
- a tech stack table (language, framework, persistence, test and lint commands)
- a module map
- an HLD Mermaid flowchart
- conventions

Don't read every file. In a monorepo, keep the overview at repo level and add `.aa/context.<package>.md` only for the packages the task touches. Record env var names only, never their values.

## [O] Observe: data flow (task-relevant modules only)
Cover entrypoints, data models and relationships, and one end-to-end flow. Also record where the tests live, how they run, and which existing test cases exercise the flow. Merge this into `.aa/schema_cache.json` (keys: `data_models`, `entrypoints`, `data_flows`, `tests`), and show one sequence, ER or flow diagram.

## [I] Inspect: root cause and blast radius (no solution code)
- Restate the ask and list the acceptance criteria. If something is ambiguous, ask once.
- For a bug, work from symptoms to 2–3 hypotheses to evidence, ending in a root cause with `file:line` and a confidence level.
- Build a Blast Radius Matrix with columns: area | direct/indirect | risk (Low = well tested, Medium = partial or shared, High = untested, public API, migration or security) | why.
- Assign a tier:

| Tier | Max files | Max net LOC | Token guide |
|---|---|---|---|
| S | 2 | 50 | ~20k |
| M | 3 | 150 | ~60k |
| L | 8 | 400 | ~150k; split into phases |

Budgets are advisory. Stage shares: A 15%, O 20%, I 20%, P 10%, E 20%, V 15%. If a stage runs about 1.5× over, say so and ask before continuing. Caps can be exceeded only when the plan flags it and the human approves.

## [P] Plan: human gate
- Write at least 2 genuinely different approaches. For each, give the files, an estimated LOC count, the risk, its pros and cons, and the performance impact. Mark one as (Recommended).
- Flag any cap breach with ⚠.
- End with "Reply A or B (or edit the plan)".
- **STOP. Write no code until the human chooses.**

## [E] Execute: minimal diff
- If the user asked for a branch, first run `git switch -c aa/<task-slug>` (append `-2` on a clash; warn and ask if unrelated uncommitted changes would carry over). Never commit or push.
- Implement only the chosen approach, matching the existing style.
- Stop and ask if you need an unplanned file, a new dependency, a public interface change, or more than 1.5× the planned LOC.
- Do one self-review of the diff: imports and types, root cause addressed, edge cases, no debug code, no helper with fewer than 2 call sites. Fix what it finds once, with no second review loop.

## [V] Validate
- Map each acceptance criterion to its evidence.
- List the edge cases and mark each covered or not.
- Run the targeted tests, then the suite, plus lint and typecheck.
- Allow one auto-fix retry only. On an environmental failure, stop.
- Produce a Manual Verification Checklist for anything automation can't cover with confidence.
- End with a summary card (files, +/− lines, checks run, retry used).

## Rules
- No code before approval.
- Root cause before solution.
- No new dependencies without confirmation.
- No drive-by refactors.
- Never commit or push unless asked.
- Never write secret values into `.aa/`.
