# [P] Plan — approaches & human gate

**Objective:** pick the simplest robust change, with the human, before touching code.
**Budget:** 10% of the run budget.

## Steps

1. Draft **≥2 genuinely different** approaches. Useful axes: inline fix vs shared fix; fix at source vs at boundary; code vs config; patch now vs migrate properly. Don't pad with a strawman.
2. For each: files touched, est. net LOC, risk (from the blast radius), tradeoffs, performance impact (only if real — otherwise "negligible").
3. Check each against the tier caps. Flag any breach with ⚠.
4. Recommend one. Prefer fewer files, smaller diff, existing patterns — unless that leaves the root cause unfixed.
5. For tier L: split into phases; each phase gets its own P→E→V.

## Output — Decision Matrix

```
━━ [P] PLAN · tier M · caps: ≤3 files, ≤150 LOC ━━

[A] Normalise units in isExpired()  (Recommended)
    Files: src/auth/token.ts (1)          Diff: ~8 LOC     Risk: Low
    + fixes root cause at source; existing tests cover it
    − none significant
    Perf: negligible

[B] Convert at middleware boundary
    Files: middleware/requireAuth.ts, src/auth/token.ts (2)   Diff: ~25 LOC   Risk: Medium
    + leaves token lib untouched
    − every other caller of isExpired() stays buggy
    Perf: negligible

Tests planned: add case "token expiring in <1s" to token.test.ts
Not doing: refactoring token storage (out of scope)

Reply A or B (or edit the plan) to continue.
```

Save `approaches` to `.aa/current_session.json`, append to `stage_log`.

## ⛔ STOP

End your turn here. Do not write code. Wait for the human's choice. When they reply, record `chosen` (and any edits they made) in the session, then → [E].
