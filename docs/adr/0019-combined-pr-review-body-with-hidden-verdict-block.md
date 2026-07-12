# ADR 0019: Combined PR Review Body with Hidden Verdict Block

* **Status**: Accepted
* **Date**: 2026-07-11
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

Issue #37 expands the PR Review Judges from 2 (Security, Architecture) to 4 (Syntax/Lint, Test Coverage, Architecture, Security). The existing `scripts/review.py` posts *discrete* GitHub reviews — one review body per failing judge, or a single aggregate approval — and `merge_node` in `orchestrator/nodes.py` parses each judge's verdict from a *separate* review body by matching human-readable string markers (`### LLM PR Review - Security:`, `### LLM PR Review - Architecture Compliance:`).

With four judges the discrete approach degrades: four independent reviews per cycle, four timestamps to reconcile under [ADR-0014](./0014-pr-verification-and-llm-judge-review-integration.md)'s commit-timestamp alignment, and a parser that must scan every review and take the latest occurrence of each marker. The issue explicitly asks for "a single structured comment" summarising all four judges. This decision fixes the **posting contract** (one combined review body per CI run) and the **parsing contract** (how `merge_node` extracts the four verdicts from that single body while preserving ADR-0014's `submitted_at >= ref_time` freshness rule).

This is hard to reverse because the contract couples two independently-deployed components — the CI-side `scripts/review.py` (writer of the body) and the orchestrator-side `merge_node` (parser) — across different execution contexts (GitHub Actions vs. the orchestrator container). Drift between writer and parser silently corrupts the hard merge gate.

## Decision Drivers

* **Parseability**: `merge_node` must extract four verdicts deterministically; parsing must not break on emoji, markdown table drift, or rendering differences between GitHub's review body formats.
* **Human Readability**: the PR review comment must remain useful to a human reviewer skimming the PR.
* **ADR-0014 Preservation**: only reviews validating the latest commit (`submitted_at` >= committer/push time) are evaluated; a stale review must never block or unblock a merge.
* **Single Source of Freshness**: with one combined review per CI run, all four verdicts share a single `submitted_at`, making timestamp alignment simpler and more correct than reconciling four per-judge timestamps.
* **All Judges Always Run**: the issue's edge cases ("each judge is invoked as a separate LLM call", "LLM call failure for any judge returns NEEDS REVIEW") presuppose all four judges execute on every run — no early skipping.

## Considered Options

* **Option 1: Discrete per-judge reviews (extend current scheme to 4)**
  Post up to four separate review bodies (or one aggregate approval), each carrying one judge's verdict via a heading marker; `merge_node` scans all reviews and takes the latest marker per judge.
* **Option 2: Single combined review body parsed via markdown-table regex**
  Post one body containing a planner-style summary table (`| Judge | Status | Details |`); `merge_node` parses the table cells with regex.
* **Option 3: Single combined review body with a hidden machine-parseable verdict block**
  Post one body containing a human-readable summary table **plus** a hidden HTML-comment block (`<!-- llm-pr-review-verdicts ... -->`) with one `judge_key: VERDICT` line per judge; `merge_node` extracts the comment block and parses the lines deterministically.

## Decision

We chose **Option 3**.

`scripts/review.py` posts exactly one GitHub review per CI run. The body contains:
1. A human-readable summary table and per-judge detail sections, for human reviewers.
2. A hidden HTML-comment verdict block of the form:
   ```
   <!-- llm-pr-review-verdicts
   syntax_lint: PASS
   test_coverage: FAIL
   architecture: PASS
   security: NEEDS REVIEW
   -->
   ```
   Verdicts are `PASS`, `FAIL`, or `NEEDS REVIEW` (the latter also covers LLM-call failure, per the issue's safe-block edge case). The review action is `approve` when all four are `PASS`, else `request-changes` (subject to `submit_github_review`'s existing self-author downgrade to `--comment`).

`merge_node` rewrites its verdict parser: fetch the PR reviews, keep only those authored by the trusted judge user with `submitted_at >= ref_time` (ADR-0014 preserved), select the **latest** qualifying review by `submitted_at`, and parse all four verdicts from *that single body's* hidden block. Any `FAIL` or `NEEDS REVIEW` among the four blocks the merge and triggers Failure Recovery — no bypass flags (ADR-0014).

This decouples presentation from parsing: the visible table may evolve (emoji, column order, collapsed sections) without breaking the gate, while the hidden block is a stable machine contract. A single review's `submitted_at` anchors all four verdicts, so ADR-0014 freshness is enforced once per cycle rather than per judge.

### Consequences
* **Pros**:
  * Robust parsing — the hidden block is immune to markdown/emoji drift that would break table-cell regex.
  * Simpler, more correct ADR-0014 alignment — one review, one freshness anchor for all four verdicts.
  * Lower GitHub API noise — one review per cycle instead of up to four.
  * Human readability preserved — the visible table stands on its own; the hidden block is invisible to readers.
* **Negatives**:
  * **Coupling**: `scripts/review.py` (writer) and `merge_node` (parser) must keep the hidden-block format in lockstep. A change to the block shape requires coordinated edits across CI and the orchestrator. Mitigated by a shared, tested format and unit tests on both sides (see Branch 10 of the #37 design).
  * **Hidden contract**: a reader inspecting the body in GitHub sees the table but not the parsing contract; the block is self-naming (`llm-pr-review-verdicts`) but the writer↔parser coupling is not obvious from the body alone. This ADR exists to make that coupling explicit.

## Note on Fail-Fast

A cost-optimisation strategy would skip `test_coverage`, `architecture`, and `security` when `syntax_lint` returns `FAIL`. We deliberately **do not** adopt fail-fast: the issue's edge cases presuppose all four judges always run ("each judge is invoked as a separate LLM call"; "LLM call failure for any judge returns NEEDS REVIEW"), and a `syntax_lint` `FAIL` already blocks the merge regardless of the other judges. There is no `SKIPPED` verdict state in this repo's contract. Running all four judges on every cycle ensures the Worker receives complete feedback across all dimensions in a single retry, rather than fixing one failure at a time across multiple iterations.

## Inspiration & References
* [ADR-0014: PR Verification and LLM Judge Review Integration](./0014-pr-verification-and-llm-judge-review-integration.md) — the commit-timestamp review alignment this decision preserves and simplifies.
* [ADR-0018: Flat Factory.json Schema](./0018-flat-factory-json-schema-over-grouped-node-taxonomy.md) — the per-node model routing that makes per-judge model selection a flat-key lookup.
* **GitHub Community Discussion #27939** — "Ability to add custom metadata to pull requests": documents HTML comments in PR bodies as a known community workaround for GitHub's lack of a native metadata API.
* **GitHub REST API — Reviews endpoint** ([docs](https://docs.github.com/en/rest/pulls/reviews)): confirms the review body is a free-text field fully under writer/reader control, including HTML comments.
