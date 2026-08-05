# ADR 0034: Hybrid Retry — Hard Rollback on the Final Execute Attempt

## Status
Accepted (refines [ADR-0013](./0013-incremental-refinement-workspace-state-on-retry.md))

## Context
[ADR-0013](./0013-incremental-refinement-workspace-state-on-retry.md) chose pure **Incremental Refinement**: when verification fails, the workspace stays intact and the Worker patches its prior edits, with a hard cap of 3 retries. This is efficient and mirrors how humans debug, but it is all-or-nothing. If the Worker accumulates errors across retries 1–2, the 3rd retry builds on corrupted state — exactly the "retry loop" failure mode where each attempt looks different enough to justify another pass yet makes no real progress.

## Decision
Adopt a **Hybrid Retry** strategy, configurable via `AGENT_RETRY_HARD_RESET_ATTEMPT` (default `3`):

- Execute attempts **below** the threshold stay **incremental** (ADR-0013): the Worker refines its existing edits in place.
- From the threshold attempt **onward**, the Execute-Node reverts tracked-file edits to `HEAD` via `git reset --hard` *before* invoking the Worker, giving it a clean branch state for its final try. After the 3rd attempt also fails, the loop transitions to Recovery unchanged.

The reset is performed in the Execute-Node (which already derives the 1-based attempt index from `attempts["verify"]`), so graph routing is unchanged and Verify-Node stays focused on verification.

### Reset scope: `git reset --hard` only, never `git clean`
Nothing is committed until the PR-Node, so the Test-Writer's tests and stubs are **untracked**. `git clean -fd` would delete them, breaking Test-Driven Development on the final attempt (no tests to satisfy). `git reset --hard HEAD` reverts tracked-file modifications — the primary source of compounding errors — while leaving untracked files (tests, stubs, and any Worker-created new files) intact. The reset target `HEAD` is the clean branch tip established by the Claim-Node checkout.

### Feedback is preserved across the reset
The prior verification output is still injected into the Worker prompt on the reset attempt. "Clean slate" means the *workspace*, not the Worker's *memory* — consistent with the Reflexion pattern cited by ADR-0013, where verbal feedback (not a blank restart) drives self-correction.

## Considered Options
- **Hard rollback on every retry** — rejected by ADR-0013: discards mostly-correct work and forces costly rewrites.
- **Hard rollback + `git clean -fd` on the final attempt** — rejected: destroys the Test-Writer's untracked tests, defeating TDD on the very attempt meant to benefit from it.
- **Commit the Test-Writer output before the execute loop, then `reset --hard` + `clean -fd`** — rejected: would change the BinEval diff inputs (tests would no longer appear in the unstaged diff) and split the single PR commit, a wider blast radius than this change warrants.
- **Progress-based exit instead of a fixed reset point** — appealing (track whether retries reduce the failing-test count) but out of scope here; left as a future refinement.

## Consequences
- **Pro**: the final attempt escapes compounding-error traps without sacrificing the efficiency of incremental retries 1–2.
- **Pro**: model-tunable — weaker models that compound errors faster can lower the threshold; strong models that rarely reach retry 3 are unaffected.
- **Con**: the final attempt re-implements from scratch, raising token cost on that one attempt (an explicit trade, per the issue).
- **Con**: Worker-created *untracked* new files from earlier attempts persist into the final attempt. This is a minor residual: the Worker can read and delete or fix them. Accepted to preserve the Test-Writer's tests.
- **Crash safety / Stateful Resume**: the reset decision is derived from existing `state["attempts"]["verify"]` — no new state field. `git reset --hard HEAD` is idempotent, so a crash-and-resume that re-enters the threshold attempt and re-runs the reset is safe (a no-op on an already-clean tree).

## Inspiration & References
- **Reflexion (Shinn et al., NeurIPS 2023)** — verbal self-reflection outperforms clean-slate restarts; supports keeping feedback while resetting the workspace.
- **Aider's reflection loop** (`aider/coders/base_coder.py`) — edit → validate → reflect → retry with a shared `max_reflections` cap (default 3); the cap is the circuit breaker our 3-retry limit mirrors.
- **"The 3 loops that break AI agents in production" (ODSC)** — the retry loop: "Raising the maximum iteration count does not solve a retry loop… The exit needs to be based on progress." Resetting on the final attempt is a structural escape from the loop, not just a higher cap.
- **"Resets beat refinements" (C. Koch, Gearflow)** — once an LLM drifts on a stuck task, a clean slate converges faster than continued refinement; motivates resetting *late*, after cheap refinements have been exhausted.
- Issue #59 — the originating proposal: incremental for retries 1–2, hard rollback on the 3rd.
