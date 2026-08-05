# ADR 0032: Enrichment Module Extraction and BinEval Extension

* **Status**: Accepted
* **Date**: 2026-08-04
* **Deciders**: Luis Arteaga & The Architect
* **Amends**: ADR-0022, ADR-0023

## Context and Problem Statement

ADR-0022 specified that `enrich_diff_with_function_context` would be added
directly to `scripts/review.py`, and that enrichment would run once in
`main()` before the judge loop. ADR-0023 later called for refactoring the
enrichment output from a pooled string to a per-file mapping
(`Dict[str, str]`) to prevent context from being orphaned during per-file
splitting.

During implementation (issue #53), three deviations from the documented
decisions were necessary:

1. **Module location**: The enrichment function was placed in a new module
   `scripts/enrichment.py` rather than inline in `scripts/review.py`.
2. **Output format**: The pooled string format was kept (not refactored to
   `Dict[str, str]`), but enrichment was moved inside `run_judge` and
   applied per-chunk (per-batch) instead of once in `main()`.
3. **BinEval extension**: Enrichment was also applied to BinEval
   (`_run_bineval_phase` in `orchestrator/nodes.py`), which ADR-0022 did
   not specify and ADR-0027 does not mention.

These deviations are hard to reverse (they change the module structure and
the evaluation pipeline), surprising without context (a future reader will
wonder why enrichment lives in its own module and why BinEval is enriched),
and the result of real trade-offs (code reuse vs. ADR-0022's location,
pooled-per-chunk vs. ADR-0023's per-file mapping).

## Decision Drivers

* **Code reuse**: BinEval (`orchestrator/nodes.py`) needs the same
  enclosing-function context the PR Review Judges need. Placing the function
  in `scripts/enrichment.py` allows both `scripts/review.py` and
  `orchestrator/nodes.py` to import it without either depending on the other.
  Inlining it in `review.py` would force `nodes.py` to import from
  `scripts/review.py` — a layering inversion (the orchestrator importing from
  a CI script).
* **ADR-0023 orphan prevention**: The pooled format orphans context when
  `split_diff_by_file` runs in `run_judge`'s multi-batch path. ADR-0023
  proposed `Dict[str, str]` to fix this. An equivalent fix is to enrich
  *after* splitting — each batch (already file-scoped) gets its own context
  block appended. This avoids changing the function's return type and the
  external contract (ADR-0019's hidden verdict block), while achieving the
  same goal: context travels with its file's chunk.
* **BinEval context quality**: BinEval grades the same diff the PR judges
  grade. The same false-positive risk (±3 line window too narrow to judge
  duplication, assertion quality, or taint) applies. Extending enrichment to
  BinEval is a consistent application of ADR-0022's rationale to the soft
  gate, not a new design.

## Decision

### 1. Enrichment lives in `scripts/enrichment.py`

`enrich_diff_with_function_context(diff, workspace_dir)` is defined in
`scripts/enrichment.py`, not `scripts/review.py`. Both `scripts/review.py`
(via the `_enrich_chunk` helper) and `orchestrator/nodes.py` import it.
This amends ADR-0022's "Extend `scripts/review.py` with a function..." to
"Extend the scripts package with a module `scripts/enrichment.py`
containing the function...".

### 2. Pooled format, applied per-chunk inside `run_judge`

The enrichment function returns a pooled string
(`raw_diff + context_block`), unchanged from ADR-0022's format. The fix
for ADR-0023's orphan problem is structural, not type-level: enrichment is
applied *inside* `run_judge` after `split_diff_by_file` and
`pack_into_batches`, so each batch receives its own enrichment block. The
fast path (single batch) enriches the whole diff as one chunk.

This amends ADR-0023's "refactored from a pooled output to a per-file
mapping: `Dict[str, str]`" to "applied per-chunk inside `run_judge` using
the pooled format, achieving the same context-travels-with-chunk
guarantee without changing the function's return type."

### 3. BinEval receives enriched context

`_run_bineval_phase` in `orchestrator/nodes.py` calls
`enrich_diff_with_function_context` on the workspace diff before passing
it to `_run_bineval`. This extends ADR-0022's enrichment to the BinEval
soft gate (ADR-0027). ADR-0027's input specification ("issue body, plan,
git diff, ADRs") is amended to include enclosing function context as
part of the git diff input.

## Considered Options

* **Inline in `scripts/review.py`, import from `nodes.py`**: rejected
  because `orchestrator/nodes.py` importing from `scripts/review.py`
  inverts the dependency direction (orchestrator → CI script).
* **Inline in `orchestrator/`, import from `review.py`**: rejected
  because `scripts/review.py` runs in CI and already adds `scripts/` to
  `sys.path`; importing from `orchestrator/` in CI context adds path
  complexity. `scripts/enrichment.py` is importable from both contexts.
* **Implement ADR-0023's `Dict[str, str]` return type**: rejected
  because it changes the function's contract, requires all callers to
  reassemble the string, and provides no benefit over per-chunk
  enrichment — the orphan problem is solved structurally.

## Consequences

* **Pros:**
  * Single enrichment function shared by both evaluation paths (PR judges
    and BinEval), no duplication.
  * Pooled format preserved — no contract change for ADR-0019's hidden
    verdict block or downstream consumers.
  * BinEval gets the same context quality improvement as the PR judges.
  * `scripts/enrichment.py` is independently testable with a temp
    workspace (ADR-0022's testability goal preserved).
* **Negatives:**
  * ADR-0022's "Extend `scripts/review.py`" is no longer literally true;
    future readers must consult this ADR to understand the module split.
  * ADR-0023's `Dict[str, str]` return type is not implemented; the
    per-chunk approach is an equivalent-but-different solution.
  * BinEval enrichment adds token overhead to the soft gate (bounded by
    the same 15K per-file limit).

## Inspiration & References

* [ADR-0022: Enclosing Function Context Enrichment for PR Review Judges](./0022-enclosing-function-context-enrichment-for-pr-review-judges.md)
  — the original enrichment decision this ADR amends.
* [ADR-0023: Per-File Map-Reduce Diff Evaluation for PR Review Judges](./0023-per-file-map-reduce-diff-evaluation-for-pr-review-judges.md)
  — the orphan problem this ADR solves structurally instead of
  type-level.
* [ADR-0027: Pre-PR BinEval Soft Semantic Gate](./0027-pre-pr-bineval-soft-semantic-gate.md)
  — the BinEval soft gate this ADR extends with enrichment.
* [ADR-0031: Separation of Concerns in Review LLM Call Path](./0031-separation-of-concerns-in-review-llm-call-path.md)
  — the precedent for extracting helpers out of `review.py` into focused
  modules.
