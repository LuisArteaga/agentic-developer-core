# ADR 0022: Enclosing Function Context Enrichment for PR Review Judges

* **Status**: Accepted (amended by ADR-0032)
* **Date**: 2026-07-12
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

The PR Review Judges (`scripts/review.py`) receive only the raw `git diff` as input — changed lines plus ±3 lines of surrounding context. This has two structural weaknesses:

1. **Missing semantic context:** A diff hunk shows a fragment of a function, not the full function body. A judge cannot reliably determine whether added lines are true duplicates, whether assertions are meaningful, or whether a taint flow is safe — without seeing the enclosing function.

2. **False positives:** During development of a similar LLM judge system, the `architecture` judge flagged a "duplicate assertion" in a test file. The two `assertFalse` lines used different IP addresses (`127.0.0.1` vs `169.254.169.254`), but the diff's ±3 line window didn't show enough surrounding context for the judge to distinguish them. The full test function body would have made the distinction obvious.

This is hard to reverse because it changes `review.py`'s input pipeline (diff → diff + context blocks), introduces tree-sitter as a CI-side dependency for the first time, and establishes an enrichment contract that all four judges depend on. A future reader will wonder why judges receive function bodies beyond the diff, and why tree-sitter is used instead of regex.

## Decision Drivers

* **False-positive reduction:** Judges need enough surrounding code to make semantically correct verdicts, not just syntactically valid ones.
* **Token efficiency:** Full changed files would add ~+306% token overhead (measured on planner PR #41). The enrichment must stay well under +100%.
* **ADR-0017 consistency:** That ADR already chose tree-sitter for structural parsing and explicitly rejected regex as "fragile, high-maintenance." Using regex in `review.py` would contradict that rationale without a compelling reason.
* **Language scope:** The judges evaluate PRs to the Orchestrator Repository, which is Python. Non-Python files in the diff (JSON, YAML, Markdown) have no `def`/`class` boundaries to extract.

## Decision

### Enclosing-function enrichment (pr-agent approach)

Extend `scripts/review.py` with a function `enrich_diff_with_function_context(diff, workspace_dir)` that:

1. Parses each `@@`-hunk from the diff to identify changed files and line numbers.
2. Reads the original file from the workspace checkout (`GITHUB_WORKSPACE`).
3. Uses tree-sitter to parse the file's AST and find the enclosing function or class method for each hunk's start line.
4. Extracts the full function/method body (start byte to end byte).
5. Appends all extracted bodies as a single `=== ENCLOSING FUNCTION CONTEXT ===` block after the raw diff, with `--- <file> :: <function_name> ---` headers.

The enriched payload is: `raw_diff + "\n\n=== ENCLOSING FUNCTION CONTEXT ===\n" + context_blocks`. The raw diff is never modified — context is a clearly delimited separate block, mirroring the existing `=== REPOSITORY ARCHITECTURE CONTEXT ===` pattern.

### All four judges receive enriched context

`syntax_lint`, `test_coverage`, `architecture`, and `security` all receive the enriched payload. The enrichment runs once in `main()` before the judge loop; the same enriched diff goes to all judges. Even `syntax_lint` benefits — naming convention checks need to see class/function declarations, which may be outside the ±3 line hunk window.

### Tree-sitter for boundary detection (not regex)

Function boundary detection uses `tree-sitter-language-pack` (per ADR-0017) — the same dependency already chosen for the Plan-Node's Structural Outline extraction. This avoids the known fragility of regex-based boundary detection:

- `def ` or `class ` in string literals/comments would be false boundaries.
- Tab/space mixing could cause incorrect indentation-based scope detection.
- Multi-line signatures and decorators need AST-level parsing for correct boundaries.

### Per-file truncation at 15,000 characters

Each file's extracted context is capped at 15,000 characters with a `[... truncated ...]` marker. No total cap across all files. In practice, PRs to this repo touch 1–5 files; 5 × 15K = 75K chars (~18K tokens), well within the context windows of all models in `factory.json`.

### Truncate diff first, enrich from workspace files

The enrichment pipeline in `main()` is:

1. `diff = sys.stdin.read()`
2. `diff = truncate_diff(diff)` — caps raw diff at `MAX_DIFF_CHARS` (250,000)
3. `enriched = enrich_diff_with_function_context(diff, workspace_dir)` — parses hunks from truncated diff, reads function bodies from workspace files on disk
4. `enriched_diff = diff + context_blocks`
5. Judges receive `enriched_diff`

Function bodies are always complete because they come from disk, not from the diff text. Hunks lost to diff truncation don't get enriched — their diff lines aren't visible to the judges either, so enriching them would be confusing.

### No file-type exclusion

Test files are enriched the same as source files. The INC-001 false positive was specifically in a test file — excluding tests would reintroduce the exact bug that motivated this feature. The enrichment function works on `@@` hunks, not file paths; no file-type filter is applied.

### Non-Python files gracefully skipped

Files without parseable `def`/`class` boundaries (JSON, YAML, Markdown, empty files) produce no context block. The tree-sitter parser for Python will simply find no function nodes in non-Python files; those files are silently skipped.

### Dependency on issue #27

`tree-sitter-language-pack` is not yet installed in this repo. ADR-0017 accepted it but the outline extractor was never implemented. Issue #27 is open to implement it. This enrichment depends on tree-sitter being installed first — the new issue should be marked as blocked by #27.

## Considered Options

* **Full changed files (send entire file content for each changed file):** Trivial to implement, eliminates all context-related false positives. Rejected: ~+306% token overhead (diff ~10,800 tokens, all files ~44,100 tokens). Single large files can dwarf the entire diff.

* **Regex-based boundary detection (planner's original ADR-0011 approach):** stdlib `re` only, search for `def`/`class` lines. Rejected: contradicts ADR-0017's rationale ("hand-rolled regex parsers would create significant maintenance burden and fragility"). False boundaries from string literals, tab/space fragility, nested scope miscounting. A future engineer seeing tree-sitter in ADR-0017 and regex in `review.py` would wonder why.

* **AST + 1-hop call-graph (CodeRabbit approach):** Parse AST, build call graph, include direct callers/callees. Best context depth, handles cross-file dependencies. Rejected: heavy implementation (multi-file AST traversal, import resolution, graph construction). Current judge criteria (duplicates, test quality, YAGNI, security taint) are fully addressable with enclosing-function context. Escalation to call-graph can be a future ADR if needed.

* **Enrich in CI workflow (pre-step before review.py):** A separate shell/Python step would pipe enriched diff into `review.py` via stdin. Rejected: splits review logic across two contexts, untestable without running CI. The enrichment is review logic and belongs in `review.py` where it can be unit-tested with a temp workspace.

* **Modify diff hunks inline (rewrite `@@` boundaries):** Expand hunk boundaries to include the full function, producing a pseudo-diff. Rejected: produces a diff that doesn't match `git diff` output, risks confusing the LLM with invalid hunk metadata. The raw diff must stay untouched for logging and telemetry.

* **Total context cap across all files:** e.g., 50K chars total, truncate largest files first. Rejected: adds complexity (truncation policy) for marginal benefit. The per-file limit already bounds the worst case; pathological PRs are handled by the existing diff truncation.

## Consequences

* **Pros:**
  * False positives caused by insufficient diff context (duplicate detection, assertion quality, taint analysis) are reduced — judges see the full enclosing function.
  * Token overhead stays at ~+30–50% (planner's estimate) — acceptable for all models in `factory.json`.
  * Consistent with ADR-0017: tree-sitter is used for structural parsing, not regex.
  * Testable: `enrich_diff_with_function_context` is a pure function testable with a temp workspace.
  * Non-Python files gracefully skipped — no special-casing needed.
* **Negatives:**
  * Introduces `tree-sitter-language-pack` as a CI-side dependency for the first time (blocked by issue #27). `review.py` loses its "stdlib-only" characteristic.
  * Cross-file problems (e.g., changed signature breaks caller in another file) are still not detected. Can be escalated to AST + call-graph in a future ADR.
  * Per-file 15K limit means very large functions may be truncated, losing some context. Accepted: truncated context is still more useful than ±3 diff lines.

## Inspiration & References

* [pr-agent (Qodo/CodiumAI)](https://pr-agent.ai) — `allow_dynamic_context`, enclosing-component strategy, asymmetric context window.
* [CodeRabbit](https://coderabbit.ai) — AST + 1-hop call-graph as a further evolution.
* [Graphite — "How much context do AI code reviews need?"](https://graphite.com/guides/ai-code-review-context-full-repo-vs-diff) — recommends hybrid "diff + relevant slices" strategy capturing ~80–90% of needed context.
* [Tencent — "Reducing False Positives in Static Bug Detection with LLMs" (arXiv)](https://arxiv.org/html/2601.18844v1) — hybrid LLM + static analysis eliminates 94–98% of false positives.
* [ADR-0017: tree-sitter-language-pack for Structural Outlines](./0017-tree-sitter-language-pack-for-structural-outlines.md) — the tree-sitter dependency decision this enrichment builds on.
* [ADR-0014: PR Verification and LLM Judge Review Integration](./0014-pr-verification-and-llm-judge-review-integration.md) — the merge gate this enrichment improves.
* [ADR-0019: Combined PR Review Body with Hidden Verdict Block](./0019-combined-pr-review-body-with-hidden-verdict-block.md) — the verdict contract this enrichment is transparent to.
