# ADR 0047: Split Per-Gate Verify Attempt Counters

## Status

Accepted (supersedes the shared-counter decision in
[ADR-0027](./0027-pre-pr-bineval-soft-semantic-gate.md); refines the
execute-attempt derivation in
[ADR-0034](./0034-hybrid-retry-hard-rollback-on-final-attempt.md) and the
per-cycle budget reset in
[ADR-0036](./0036-closed-post-pr-judge-feedback-loop.md)).

## Context and Problem Statement

ADR-0027 deliberately used a **single shared counter** `attempts["verify"]` for
both failure modes inside `verify_node`: deterministic `make verify` failures
and BinEval soft-gate FAILs (max 3 total). The intent was a simple global bound
on the Execute→Verify loop — a Worker could not spin on either gate forever.

That decision starved the BinEval gate in production. On orchestrator run
2026-08-14 (Target Repository issue #13), two `make verify` failures — a purely
mechanical gap (`make: *** No rule to make target 'verify'`, the skeleton
workspace had no Makefile yet) — drove the shared counter to 2. On attempt 3
`make verify` passed, the first BinEval FAIL incremented the counter to 3, and
the issue transitioned to `failed`. The Worker **never received BinEval
structured feedback a single time** — the semantic gate got zero effective
retries, defeating ADR-0027's purpose (cheap in-cycle self-correction *before*
the post-PR hard gate).

The root cause is that the two gates fail for categorically different reasons
and a shared budget lets one failure category silently deplete the other's:

- **`make verify`** fails on deterministic, mechanical problems (missing
  Makefile, syntax error, failing test). These are *infrastructure/behavioral*
  failures.
- **BinEval** fails on semantic quality problems (unnecessary abstraction,
  scope creep, ADR violation). These are *judgment* failures the soft gate
  exists to cheaply self-correct before the expensive post-PR judges run.

A shared budget couples two independent failure modes: a flaky or
mis-configured `make verify` (transient, mechanical) can exhaust the retries
available for BinEval (semantic, the gate's actual purpose).

The shared counter had a **third** duty too: ADR-0034's Hybrid Retry derives
the 1-based execute attempt index from `attempts["verify"]` to decide when to
hard-reset tracked files. Splitting the counter therefore touches the
Execute-Node, not just the Verify-Node.

## Decision

Split the single shared counter into **two independent per-gate budgets**, each
capped at `VERIFY_MAX_ATTEMPTS` (constant, default 3):

- `attempts["verify_cmd"]` — incremented only on a `make verify` failure.
- `attempts["bineval"]` — incremented only on a BinEval FAIL (genuine FAIL;
  degraded PASS from LLM/infra failure still does not increment any counter,
  preserving ADR-0027's graceful-degradation contract).

### Bounding the total loop

Each gate is independently capped at `VERIFY_MAX_ATTEMPTS`. The worst case is an
alternating failure sequence (verify-fail, bineval-fail, verify-fail, …): one
gate exhausts its cap after at most `2 * VERIFY_MAX_ATTEMPTS - 1` loop-backs to
execute (e.g. 5 with the default cap of 3). A separate global counter is
redundant — the per-gate caps *are* the documented bound, and the derived
ceiling is the total-iteration guarantee the issue requires.

### Hybrid Retry (ADR-0034) intact

The Execute-Node derives its 1-based attempt index as the **sum** of both
per-gate counters:

```python
attempt = attempts.get("verify_cmd", 0) + attempts.get("bineval", 0) + 1
```

This is semantically identical to the old `attempts["verify"] + 1` (both gates
loop back to execute on failure, so their failures must all count toward the
execute attempt). The hard-reset threshold (`AGENT_RETRY_HARD_RESET_ATTEMPT`,
default 3) and the reset mechanism (`git reset --hard HEAD`, tracked files only)
are unchanged. The only observable change is that the *maximum* execute attempts
rise from 3 to ~5 in the pathological alternating case — which is the explicit
intent of this issue (BinEval must get its retries).

### Counter resets

- **Full verify success** (`make verify` pass AND BinEval pass/skip/infra-fallback):
  `_reset_verify_success` resets **both** `verify_cmd` and `bineval` to 0 and
  clears feedback, granting a fresh budget — same semantics as before, now
  applied to both keys.
- **Merge-fix cycle** (ADR-0036): `merge_node` resets **both** per-gate counters
  when granting a fresh per-cycle verify budget. A stale `bineval=2` from the
  pre-PR phase must not carry into a merge-fix cycle and trigger spurious
  exhaustion.
- **New issue cycle** (Claim-Node): `state["attempts"] = {}` already clears all
  keys; no change needed beyond the comment.

### Edge cases (per the issue)

- `make verify` fails 3× without ever reaching BinEval → `verify_cmd` hits its
  cap, issue fails. BinEval budget untouched (parity with today).
- `make verify` passes on attempt 1, BinEval fails 3× → Worker gets 2
  BinEval-feedback retries (parity with today).
- Mixed sequence → each gate's counter increments independently; total bounded
  by the derived ceiling above.
- BinEval infrastructure failure (LLM error → degraded PASS, `bineval_degraded=True`)
  does not increment any counter (current behavior preserved).
- Empty-diff skip path resets counters and transitions to PR (unchanged).

## Considered Options

- **Separate per-gate counters + a redundant global total counter.** *Rejected*:
  the global cap is mathematically implied by the per-gate caps (worst case
  `2*cap-1`), so a third synced counter is dead state that can drift from the
  two sources of truth. The issue's "global cap" edge case is satisfied by the
  derived ceiling, not by a stored field.
- **A single shared counter with a higher cap (e.g. 5).** *Rejected*: raises the
  BinEval budget only incidentally and still couples the two failure modes — a
  run of `make verify` failures could still consume the entire raised budget
  before BinEval runs once. The starvation is structural, not a tuning problem.
- **Reset the shared counter when `make verify` first passes.** *Rejected*: this
  is the behavior the issue explicitly rules out ("the shared counter's original
  intent: cap total iterations"). Resetting on make-verify-pass would let a
  Worker spin on BinEval up to 3× per make-verify pass, potentially unbounded
  across make-verify passes. Per-gate caps bound both without coupling them.
- **A weighted shared budget** (e.g. make-verify counts 1, BinEval counts 0.5).
  *Rejected*: non-integer bookkeeping for no real benefit over independent caps,
  and harder to reason about than two plain integers.

## Consequences

- **Pro:** BinEval can no longer be starved by `make verify` failures. The
  semantic gate always receives its full `VERIFY_MAX_ATTEMPTS` retries
  regardless of how many make-verify failures preceded it — restoring
  ADR-0027's intended in-cycle self-correction.
- **Pro:** `make verify` likewise keeps its full budget regardless of BinEval
  failures (symmetric; the coupling is broken in both directions).
- **Con:** the maximum total Execute→Verify iterations rise from 3 to up to
  `2*VERIFY_MAX_ATTEMPTS-1` (5 with the default) in the pathological
  alternating-failure case. This is the explicit trade: more total iterations to
  guarantee each gate its independent retries. Token cost rises proportionally
  only in that worst case; the common case (one gate fails) is unchanged.
- **Neutral:** Hybrid Retry (ADR-0034) is preserved — the threshold keys off the
  derived execute attempt, which is the sum. The reset semantics and crash-safety
  (`git reset --hard HEAD` idempotency, Stateful Resume deriving from existing
  state) are unchanged; no new state field is introduced, only two renamed keys.
- **Neutral:** Verification Feedback remains the single feedback channel for both
  gates (truncated output, max 150 lines / 10 KB). The feedback content already
  distinguishes the source (make-verify stdout vs BinEval structured check list),
  so the Worker can tell which gate failed.
- **Migration:** existing `state.json` files from in-flight runs carry the old
  `attempts["verify"]` key. The Execute-Node derivation
  (`verify_cmd + bineval + 1`) reads `.get(..., 0)` for both new keys, so a
  stale `verify` key is silently ignored (treated as 0) and the next
  make-verify/BinEval failure starts the new counters at 1. No state migration
  is required; a mid-cycle resume simply gets a fresh per-gate budget.

## Inspiration & References

- **Google SRE — Error Budget Policy for Service Reliability** (T2, accessed
  2026-08-14): error budgets exist precisely so that one category of failure
  does not silently consume the reliability allowance meant for another; the
  control mechanism diverts attention to the failing category rather than
  letting an unrelated failure exhaust a shared budget.
  https://sre.google/workbook/error-budget-policy/
  *Verification: official Google SRE Workbook; directly motivates per-category
  budgets over a shared pool.*
- **Circuit Breaker Pattern — Azure Architecture Center** (T1, accessed
  2026-08-14): "the retry logic should be sensitive to any exceptions" — retry
  policy must distinguish failure types rather than treat all failures
  identically against one counter. The retry pattern and the circuit breaker
  address different failure classes and should not share a single threshold.
  https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker
  *Verification: official Microsoft Azure Architecture Center pattern
  reference; retry-sensitivity-to-failure-type is the documented guidance.*
- **`tenacity` — `stop_after_attempt(n)`** (T1, accessed 2026-08-14): the
  canonical Python retry library bounds retries with an independent per-call
  `stop_after_attempt(3)`, not a shared global counter — each retried operation
  owns its budget. Mirrors the per-gate independent cap adopted here.
  https://github.com/jd/tenacity
  *Verification: tenacity is the de-facto Python retry library; per-call
  `stop_after_attempt` is its documented bounded-retry primitive.*
- **ADR-0027** (the superseded shared-counter decision), **ADR-0034** (Hybrid
  Retry — execute-attempt derivation), **ADR-0036** (merge-fix per-cycle budget
  reset).
