# [I] Inspect — root cause & blast radius

**Objective:** know *why* before deciding *how*. No solution code in this stage.
**Budget:** 20% of the run budget.

## Steps

1. **Restate the ask** in one sentence, plus explicit acceptance criteria (bullets). If the ask is ambiguous in a way that changes the fix, ask the user now — one batched question, not a drip.
2. **Bug:** symptoms → 2–3 hypotheses → evidence for/against each (read code, logs, tests, `git log -p`/`git blame` on suspect lines) → root cause with `file:line`. If you can reproduce cheaply (a failing test or one command), do it.
   **Feature:** where it plugs in, what existing code it extends, what contracts it touches.
3. **Blast Radius Matrix:**
   - *Direct* — files/functions that will change.
   - *Indirect* — callers, API contracts, DB queries/migrations, config, other services, UI.
   - *Risk* per row — **Low** (well tested, internal), **Medium** (partial tests or public-ish interface), **High** (untested, public API, data migration, auth/payments/security).
4. **Confirm or change the pre-assessed tier** S/M/L (see SKILL.md) unless `--tier` was given. Justify in one line — e.g. raise it when a High-risk untested caller needs a new test.

## Output — in chat

```
Root cause: <one sentence>
Evidence:   src/auth/token.ts:42 — expiry compared in seconds vs ms
Confidence: High | Medium (unconfirmed: <what would confirm>) | Low

Acceptance criteria:
- …

Blast radius
| Area                         | Type     | Risk   | Why                        |
|------------------------------|----------|--------|----------------------------|
| src/auth/token.ts:isExpired  | direct   | Low    | covered by token.test.ts   |
| middleware/requireAuth       | indirect | Medium | all protected routes       |

Tier: M — 2 files, touches shared auth middleware
```

Write `task`, `tier`, `acceptance_criteria`, `rca`, `blast_radius` to `.aa/current_session.json` (schema: `schemas/session.schema.json`) and append to `stage_log`.

If `--analyse-only`: stop here with the summary. Otherwise → [P].
