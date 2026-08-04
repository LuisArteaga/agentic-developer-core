# BinEval Code-Grading Rubric

The Pre-PR Review (BinEval) grades a candidate pull request diff against the
following **10 binary (pass/fail) checks** across four dimensions. Each check is
either PASS or FAIL — there are no partial scores. The overall verdict is PASS
only when **every** check passes.

BinEval runs *after* the Target Repository's deterministic `make verify` passes
and *before* a Pull Request is created. It is a **soft gate**: it catches
semantic issues that deterministic checks cannot (unnecessary abstraction,
missing edge-case handling, ADR violations) and gives the Worker a chance to
self-correct in-cycle. The hard merge gate remains the post-PR Review Judges.

Inputs to the grading prompt are language-agnostic: the issue body, the
generated plan, the git diff, and the loaded ADRs (if any). BinEval never assumes
the Target Repository's programming language — language-specific deterministic
checks are `make verify`'s responsibility (see Language Agnosticism in
CONTEXT.md).

## Dimension 1 — Completeness

- **1.1 — Acceptance criteria addressed**: Every acceptance criterion enumerated
  in the issue is addressed by the diff. A criterion is "addressed" when the diff
  contains a change that plausibly satisfies it; a deferred/deferred-to-later
  criterion is FAIL unless the issue explicitly permits it.
- **1.2 — Edge cases handled**: Edge cases enumerated in the issue (or reasonably
  implied by the change) are handled or explicitly justified as out of scope.
  Silent ignoring of a named edge case is FAIL.
- **1.3 — Scope discipline**: The diff modifies only files within the planned
  scope — no unrelated, drive-by, or cosmetic changes outside the plan's target
  files. (Replaces the planner-core "technically implementable" check, adapted
  for code grading.)

## Dimension 2 — Simplicity (Radical Simplicity, ADR-0009)

- **2.1 — No unnecessary abstraction**: No premature generalization, speculative
  interfaces, or abstraction layers introduced without an immediate consumer.
  A concrete solution that satisfies the requirement is preferred.
- **2.2 — No dead code or debris**: No unused imports, unreachable branches,
  commented-out code, leftover debugging artifacts (print statements, TODO
  stubs), or orphaned helpers.
- **2.3 — Minimal change**: Changes are the minimal set required by the
  requirement — no scope creep, no unrelated refactors bundled in, no
  gold-plating. Refactors not required by the issue are FAIL.

## Dimension 3 — ADR Compliance

- **3.1 — Complies with existing ADRs**: Changes do not contradict any
  documented Architecture Decision Record loaded from the Target Repository's
  `docs/adr/`. If a change touches a concern governed by an ADR, it must follow
  that ADR's decision.
- **3.2 — No undocumented contradictory decision**: No new architectural decision
  is introduced that silently overrides or contradicts an existing ADR. A
  legitimate new decision must be recorded as a new ADR, not smuggled in via
  code.

> **No-ADRs policy**: When the Target Repository has no `docs/adr/` directory (or
> it is empty), checks 3.1 and 3.2 auto-PASS — there is nothing to comply with.

## Dimension 4 — Robustness

- **4.1 — Error and edge paths handled**: Failure paths are handled explicitly —
  no unhandled exceptions, no swallowed errors that should surface, graceful
  degradation where the codebase's conventions require it. Input validation is
  present where applicable.
- **4.2 — No regression risk**: The change does not introduce an obvious
  regression to existing functionality — no removed safety checks, no broadened
  scope of existing code paths without justification, no breaking signature
  changes to public interfaces used elsewhere.

## Output contract

Return a `BinEvalResult` containing one `BinEvalCheck` per check (10 total),
each with `id`, `dimension`, `description`, `passed` (bool), and `reasoning`.
`overall_pass` is true iff every check passed. When a check FAILs, `reasoning`
must cite the specific diff hunk or file that fails it so the Worker can address
it directly.
