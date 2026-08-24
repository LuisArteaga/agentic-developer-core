# ADR 0030: Pipe CI Coverage Output Into the Test Coverage PR Judge

* **Status**: Superseded by [ADR 0052: Deterministic Diff Coverage Gate](../0052-deterministic-diff-coverage-gate.md)
* **Date**: 2026-07
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

The Test Coverage PR Review Judge (introduced in #37, per [ADR-0019](./0019-combined-pr-review-body-with-hidden-verdict-block.md)) evaluates test coverage from the git diff alone — Q1 (Test Presence) and Q2 (Test Quality/Assertions). That is a *syntactic* proxy: it can see that tests were added and that they contain assertions, but it cannot tell whether the **changed production code is actually exercised by passing tests**. A PR can add a trivial test (satisfying Q1/Q2) while leaving the new logic uncovered, and the judge will PASS it — exactly the gap issue #45 calls out.

The fix is to feed the judge the CI test + coverage output produced by the repository's own test suite, so it can judge real execution and per-line coverage. This is a hard merge-gate judge (ADR-0014), so the new input must **strengthen** the gate without ever weakening it when the CI data is absent.

This decision fixes the **transport contract** (how CI output reaches `scripts/review.py`) and the **prompt contract** (how the Test Coverage judge consumes it), while preserving ADR-0014 (commit-timestamp alignment) and ADR-0019 (combined review body + hidden verdict block). Both contracts are hard to reverse because they couple two independently-deployed surfaces — the CI workflow (writer of the CI output file) and the judge in `scripts/review.py` (reader) — across the GitHub Actions boundary; drift between them silently degrades a hard merge gate.

## Decision Drivers

* **Real coverage signal over diff heuristics**: judge whether changed lines are exercised by *passing* tests, not merely that tests exist.
* **Language Agnosticism preserved**: the judge must not run language-specific tooling itself; it consumes CI output produced by the target repository's own test suite.
* **Hard-gate safety**: when CI output is absent, the judge must degrade to its current diff-only behavior — never spuriously `FAIL`/`NEEDS REVIEW` (which would block merges on infrastructure gaps).
* **ADR-0014 / ADR-0019 untouched**: the hidden verdict block and review-timestamp alignment must not change.
* **Bounded context**: LLM-as-judge best practice favours focused, bounded context; the coverage report must be capped to keep the judge reliable.

## Considered Options

* **Option 1: Multi-section stdin contract** — pipe `git diff ... ; echo MARKER ; cat ci_output | scripts/review.sh`.
  *Rejected*: stdin is the primary input for *all four* judges; injecting CI output there forces every judge to carry it and couples the pipeline to a new delimiter format. Reviewers other than Test Coverage would receive irrelevant data, increasing cost and noise.
* **Option 2: Inline environment variable holding the CI output text** — `CI_COVERAGE_OUTPUT="..."`.
  *Rejected*: coverage/test output can be multi-KB; large env vars are a poor fit for GitHub Actions and harder to inspect/debug than a file.
* **Option 3: File-path environment variable** — CI writes the output to a file; `CI_COVERAGE_OUTPUT_PATH` points at it; `scripts/review.py` reads and augments only the Test Coverage judge.
  *Chosen*: the stdin contract stays diff-only (no delimiter coupling), the file holds arbitrary size, the data is debuggable on the runner, and only the Test Coverage judge is augmented.

## Decision

We chose **Option 3**.

### Transport contract

1. The PR-checks workflow defines a job-level env var `CI_COVERAGE_OUTPUT_PATH = ${{ github.workspace }}/ci_coverage_output.txt`.
2. The Pytest step runs `pytest --cov=orchestrator --cov=scripts --cov-report=term-missing --cov-fail-under=89 2>&1 | tee "$CI_COVERAGE_OUTPUT_PATH"` with `set -o pipefail` so the step still fails on test/coverage-gate failure while the file is always written. The `term-missing` report adds per-file missing-line numbers — the per-line signal the judge needs. `--cov=scripts` is added so the judge code itself (`scripts/review.py`, production code run in CI) appears in the coverage report the judge consumes — otherwise the judge would flag every PR that changes `scripts/review.py` as "absent from the coverage report" (exactly the finding that surfaced when this feature was first applied to its own PR). This matches the existing convention of measuring test files alongside production code (the prior `--cov=orchestrator` already measured `orchestrator/test_*.py`); the `--cov-fail-under=89` gate stays green.
3. The LLM review step runs `if: always()` (unchanged), so it reads the file even when earlier steps failed. `scripts/review.py` resolves the path from the env var, reads the file, and **only** augments the Test Coverage judge.

Scoping to the Pytest + coverage output (rather than the full `make verify`) is deliberate: lint/format/type-check output is the Syntax/Lint and Architecture judges' domain, not the Test Coverage judge's. Feeding it would bloat the prompt with off-domain noise. This is the right-sized capture for the judge's scope (Radical Simplicity); broadening capture later is a localized `tee` extension behind the same env var.

### Prompt contract

The base `SYSTEM_PROMPT_TEST_COVERAGE` (Q1/Q2) is unchanged. When CI output is available, `main()` appends a `=== CI VERIFICATION & COVERAGE OUTPUT ===` section to the Test Coverage prompt only, carrying the output and two new criteria:

* **Q3 (Test Execution)** — fail when tests fail/error (changed code not exercised by passing tests).
* **Q4 (Coverage of Changed Code)** — using the coverage report's missing-line information, report changed production lines that are NOT covered.

These are scored under the base prompt's existing SCORING RULE (any Q3/Q4 failure ⇒ overall `FAIL`).

The per-judge augmentation dispatch (syntax verification, architecture context, CI coverage output) is extracted out of the untested `main()` entrypoint into a pure, fully unit-tested `augment_judge_prompt(...)` function. This mirrors the `entrypoint.py` pattern (logic extracted into testable functions; `main()` kept thin). It keeps the changed wiring covered by passing tests — important because, with `--cov=scripts`, the Test Coverage judge now sees `scripts/review.py` in the coverage report and checks the diff's changed lines against the report's missing lines.

### Truncation

CI output is tail-bounded to `CI_COVERAGE_OUTPUT_MAX_CHARS` (default 15000, env-overridable as `CI_COVERAGE_OUTPUT_MAX_CHARS`). This is a **deliberate deviation** from `clip_chunk`'s head-truncation (ADR-0023): pytest prints the per-file coverage table and the final pass/fail summary at the *end* of its stream — the most evaluative signal — so the tail is preserved and the head (test progress dots) is dropped. A note marks the truncation.

### Graceful degradation

When `CI_COVERAGE_OUTPUT_PATH` is unset, the file is missing/empty, or a read error occurs, `load_ci_coverage_output()` returns `""` and no augmentation is produced — the judge evaluates Q1/Q2 from the diff alone, exactly as today. The hard gate is never weakened by CI-data presence, and never spuriously triggered by CI-data absence.

### External contracts preserved

The hidden verdict block (ADR-0019) and the `merge_node` timestamp-aligned parser (ADR-0014) are untouched: the change is purely to the Test Coverage judge's *input*, not to its verdict shape or the review body contract. The judge still emits `PASS`/`FAIL`/`NEEDS REVIEW` into the same hidden block.

## Consequences

* **Pros**:
  * Real coverage signal — the gate now catches PRs where new code is added without exercising tests, even when the overall `--cov-fail-under` gate passes.
  * No stdin-contract coupling — the diff-only stdin contract is preserved; CI data rides a separate, opt-in file path.
  * Graceful degradation — missing CI data leaves behaviour identical to today; no new false blocks.
  * Language-agnostic — the judge consumes CI output; it runs no tools.
* **Negatives**:
  * **Coupling**: the CI workflow (writer) and `scripts/review.py` (reader) must keep the `CI_COVERAGE_OUTPUT_PATH` env-var name and the file in lockstep. Mitigated by the job-level env var, tests on both surfaces, and this ADR.
  * **Tail-truncation trade-off**: very large failing-test output may lose detailed early tracebacks from the head. The tail still carries the short failure summary + coverage table, which is sufficient for the judge's coverage focus; broadening capture is a localized change.

## Inspiration & References

* **LLM-as-Judge best practices (Patronus AI — [LLM As a Judge: Tutorial and Best Practices](https://www.patronus.ai/llm-testing/llm-as-a-judge))**: explicit criteria + binary pass/fail + bounded, focused context. Q3/Q4 are explicit binary criteria; the output is capped.
* **Arize — [LLM as a Judge Primer](https://arize.com/guides/llm-as-a-judge)**: keep judge context under a bounded token budget; binary verdicts are more stable than numeric scales. Motivates `CI_COVERAGE_OUTPUT_MAX_CHARS`.
* **DeepEval — [LLM-as-a-Judge in CI/CD](https://deepeval.com/blog/llm-as-a-judge)**: LLM judges are most useful when run continuously in CI/CD as regression checks — the Test Coverage judge is exactly that.
* **GitHub Docs — [Store information in variables](https://docs.github.com/actions/learn-github-actions/variables)** and community consensus (e.g., [Matthew Rich — Job Outputs & Environment Files](https://matthewrich.com/2022/10/13/github-actions-job-output)): use **files** to pass large data between steps, env vars for small config. Motivates the file-path env var over an inline env var.
* **[ADR-0019](./0019-combined-pr-review-body-with-hidden-verdict-block.md)** — the precedent for documenting a CI↔`review.py` coupling as an ADR to prevent silent drift; this ADR mirrors that discipline for the new transport contract.
* **[ADR-0023](./0023-per-file-map-reduce-diff-evaluation-for-pr-review-judges.md)** — `clip_chunk`'s head-truncation semantics, which this decision deliberately adapts to tail-truncation for coverage output (justified above).
* **[ADR-0014](./0014-pr-verification-and-llm-judge-review-integration.md)** — the commit-timestamp alignment preserved unchanged.
