# [E] Execute — minimal diff, one self-review

**Objective:** implement exactly the approved approach. Nothing more.
**Budget:** 20% of the run budget; scales with the plan's LOC estimate.

## Steps

0. **Only with `--branch`:** create the working branch before any edit.
   - Name: `aa/<task-slug>` — kebab-case from the task, ≤40 chars after `aa/`. If it exists, append `-2`, `-3`, ….
   - If `git status --porcelain` shows changes the plan doesn't cover, say they will carry over to the new branch and ask before continuing.
   - `git switch -c <name>`. Record `branch` in the session. Never commit or push.
1. Re-read the chosen approach from `.aa/current_session.json` (fast path: from the change you showed).
2. Make the edits. Match surrounding code style, naming, error handling and comment density. Add/adjust the tests named in the plan.
3. **Drift check while editing.** Stop and ask the user (see **Gates** in SKILL.md) if you need to:
   - touch a file not in the plan,
   - exceed the planned LOC by > 50% or breach the tier cap,
   - add a dependency, change a public interface, or alter behaviour beyond the acceptance criteria.
4. **One self-review pass** over your own diff (`git diff`):
   - [ ] imports / exports / types correct; code compiles or parses
   - [ ] root cause actually addressed (not just the symptom)
   - [ ] null/empty/boundary inputs, error paths handled where relevant
   - [ ] no debug prints, dead code, unrelated formatting
   - [ ] no helper/abstraction with < 2 call sites; no new deps
   - [ ] matches the approved approach
   Fix what this pass finds **once**. Don't start a second review loop.

## Output

```
━━ [E] EXECUTE · approach A ━━
Branch: aa/users-logged-out-early (only with --branch)
Changed: src/auth/token.ts (+6 −2), src/auth/token.test.ts (+12)
Plan vs actual: 1 file / ~8 LOC planned → 2 files / 18 LOC (test file was planned)
Self-review: fixed missing ms conversion in refresh path; nothing else found
→ next: [V]
```

Append to `stage_log`. Then → [V].
