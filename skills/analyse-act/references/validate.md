# [V] Validate — requirements, edge cases, tests

**Objective:** evidence that the change works and didn't break neighbours — or an honest handoff when evidence needs a human.
**Budget:** 15% of the run budget.

## Steps

1. **Requirements:** map each acceptance criterion from [I] to evidence (test name, command output, or "manual").
2. **Edge cases:** list the specific ones for this change — e.g. empty/null, boundary values, unicode, concurrency, timezone, large inputs, permissions, retries, backwards compatibility with stored data. Mark each covered / not covered.
3. **Run checks** using commands from `context.md`/`schema_cache.json`: targeted tests first, then the relevant suite, plus lint/typecheck if cheap.
4. **On failure:** if it is caused by your change, fix it **once** and re-run. If it still fails, or the failure is environmental (missing services, credentials, flaky infra), stop and report — no further loops.
5. **Manual Verification Checklist** — produce one when any acceptance criterion or High-risk blast-radius row isn't covered by automation (UI, real external APIs, devices, deploy config, heavy setup).

## Output — final card

```
━━ [V] VALIDATE ━━
Requirements
  ✅ expired tokens rejected            token.test.ts › "rejects expired"
  ✅ token expiring in <1s handled      token.test.ts › "sub-second expiry"
  🟡 refresh flow in browser            manual (see checklist)

Edge cases: ✅ exp=now  ✅ exp missing  ✅ exp in past  ⬜ clock skew (not in scope)

Checks: npm test -- token  → 14 passed · npm run lint → clean
Retry used: no

Manual Verification Checklist
  [ ] Log in, wait 60s past expiry, reload — expect redirect to /login
  [ ] Refresh token before expiry — expect session continues

Summary: 2 files, +18 −2. Root cause fixed at source. Not committed.
```

Append to `stage_log`, set the session's final status. Don't commit unless asked.
