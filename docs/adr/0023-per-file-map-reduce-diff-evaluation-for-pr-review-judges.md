# ADR 0023: Per-File Map-Reduce Diff Evaluation for PR Review Judges

* **Status**: Accepted
* **Date**: 2026-07-12
* **Deciders**: Luis Arteaga & The Architect

## Context and Problem Statement

The PR Review Judges (`scripts/review.py`) receive a single truncated diff as the `user` message. When the diff exceeds `MAX_DIFF_CHARS` (250,000 after #47), `truncate_diff()` cuts it and appends a note instructing judges to "return NEEDS REVIEW if you cannot fully evaluate." Per ADR-0014, any `NEEDS REVIEW` unconditionally blocks the merge and triggers Failure Recovery — so a large PR is structurally unmergeable by the autonomous loop.

The immediate fix (#47) raised the limit to 250K and strips whitespace via `git diff -w`, which covers most PRs. But a genuinely large PR (many files with real semantic changes) will still truncate, and the strict `NEEDS REVIEW` on truncation is correct — we don't soften it because the judges must remain a meaningful gate.

This is hard to reverse because it replaces the single-diff LLM call — one call per judge — with an iteration-plus-aggregation pipeline that all four judges, the hidden verdict block (ADR-0019), and the test suite depend on. A future reader will wonder why judges receive multiple per-file chunks instead of one diff blob, and why a character-ratio token estimate is used instead of a real tokenizer.

## Decision Drivers

* **No unmergeable PRs**: the autonomous loop must be able to merge any PR that passes all judges, regardless of diff size.
* **Judge gate integrity**: the strict `NEEDS REVIEW` on truncation is a feature, not a bug. The solution must eliminate truncation, not soften its verdict.
* **Cost discipline**: replacing one call per judge with N×4 calls for every PR would multiply API costs and CI runtime for normal-sized PRs where truncation never triggers.
* **ADR-0019 preservation**: the hidden verdict block format (one verdict per judge) stays unchanged. The internal evaluation path changes; the external contract does not.
* **ADR-0022 interaction**: the Enclosing Function Context enrichment appends function bodies to the diff. The chunking strategy must operate on the enriched payload, not break it.

## Considered Options

* **Option 1: Raise the character limit further**
  Diminishing returns; eventually hits context window limits, especially for the architecture judge whose system prompt includes all ADRs (~14K tokens). Rejected in the issue.

* **Option 2: Per-file map-reduce (atomic per file, one LLM call per file per judge)**
  Each `git diff` file-section becomes one chunk. One LLM call per file per judge. Results aggregated: `FAIL` in any chunk → judge `FAIL`; all `PASS` → judge `PASS`. Cost: N files × 4 judges = up to 4N calls per PR. For normal PRs (1–5 files) this is 4–20 calls vs. the current 4.

* **Option 3: Tiered batch-packing (PR-Agent approach)**
  Files are the atomic unit. Small files are packed into batches under a token budget; one LLM call per batch per judge. A single file exceeding the budget is sub-split (clip with marker). Tiers: (1) if the full diff fits, one call; (2) if not, pack into batches; (3) if a single file overflows, clip it. Cost for normal PRs: 1 batch × 4 judges = 4 calls (same as today). Cost for large PRs: M batches × 4 judges, where M < N.

* **Option 4: CodeRabbit-style agentic exploration**
  A frontier model writes shell commands in a sandbox to investigate what the diff touches, including files outside the diff. Rejected: requires a sandboxed execution environment and a frontier reasoning model per review — architecturally incompatible with the current CI-side `review.py` design.

## Decision

We chose **Option 3: Tiered batch-packing**, adapted to our 4-judge architecture.

### Per-file atomic unit with batch packing

Files are never split unless a single file exceeds the token budget. Small files are packed into one batch under the budget. One LLM call per batch per judge.

### Tiered evaluation path

1. **Single-batch fast path**: if the enriched diff (raw diff + enclosing function context per ADR-0022) fits under the budget, one LLM call per judge. This is the normal case for PRs to this repo (1–5 files).
2. **Multi-batch path**: if the enriched diff exceeds the budget, split into per-file chunks and pack into batches. One LLM call per batch per judge.
3. **Single-file clip**: if a single file's diff (after enrichment) exceeds the budget, clip it with a `[... clipped ...]` marker and include it as its own batch. The judge evaluates the visible portion; if it cannot fully evaluate, it returns `NEEDS REVIEW` — preserving the current strict-on-truncation semantics for the pathological single-file case.

### Verdict aggregation

Per-judge: `FAIL` in any chunk → judge `FAIL`; all chunks `PASS` → judge `PASS`; any `NEEDS REVIEW` → judge `NEEDS REVIEW` (preserving ADR-0014's unconditional block).

### Character-ratio token estimate (no `tiktoken`)

The token budget is estimated using a conservative character-to-token ratio (~4 chars/token) rather than adding `tiktoken` as a dependency. The budget is set with a safety margin to compensate for imprecision. This is sufficient because:

- PRs to this repo touch 1–5 files (per ADR-0022); batch packing is trivial at that scale — there is likely only one batch regardless of precision.
- PR-Agent needs accurate counts because it tightly packs dozens of files near the token limit. We don't operate at that scale.
- Imprecision in the large-PR edge case means more batches (slightly more calls), not incorrect verdicts — the aggregation rule still holds.
- `tiktoken` precision matters when packing many files near the limit; a safety margin achieves the same safety at the cost of occasional extra calls.

### ADR-0019 contract unchanged

The hidden verdict block format (`<!-- llm-pr-review-verdicts ... -->`) is unchanged — one verdict per judge. The internal evaluation path (one call vs. N calls) is invisible to `merge_node`'s parser. ADR-0014's freshness rule (latest review by `submitted_at`) is preserved: all chunks for a single CI run produce one combined review body with one `submitted_at`.

### ADR-0022 interaction

The Enclosing Function Context enrichment (`enrich_diff_with_function_context`) is refactored from a pooled output to a **per-file mapping**: `Dict[str, str]` where the key is the filename and the value is that file's function context blocks. Each file's diff section gets its own context blocks appended inline:

```
## File: 'foo.py'
<foo.py diff section>

--- foo.py :: some_function ---
<function body>

## File: 'bar.py'
<bar.py diff section>
```

This ensures function context travels with its file's chunk during per-file splitting. The previous pooled format (`raw_diff + "\n\n=== ENCLOSING FUNCTION CONTEXT ===\n" + all_blocks`) would orphan the context section when splitting by `diff --git` boundaries. On the single-batch fast path, the per-file blocks are concatenated back into the same logical order, preserving the enriched payload's content for judges.

## Consequences

* **Pros**:
  * No PR is structurally unmergeable due to diff size — the autonomous loop can merge any PR that passes all judges.
  * Normal PRs (1–5 files) incur zero additional cost: one batch × 4 judges = 4 calls, same as today.
  * Large PRs are handled gracefully with bounded cost (M batches × 4 judges, where M < N).
  * ADR-0019 and ADR-0014 contracts are preserved — no changes to `merge_node` or the hidden verdict block.
  * ADR-0022 enrichment is preserved — function context travels with its file's chunk.
* **Negatives**:
  * **Character-ratio imprecision**: the ~4 chars/token estimate may under- or over-estimate actual token usage. Mitigated by a safety margin; worst case is extra batches, not incorrect verdicts.
  * **Aggregation complexity**: the judge evaluation path is no longer a single call — `run_judge` must iterate over chunks and aggregate. This adds testable complexity to `review.py`.
  * **Single-file clip edge case**: a pathological single file that exceeds the budget is still clipped, and the judge may return `NEEDS REVIEW` for it. This is the intended strict-on-truncation behavior, but it means the "no truncation for any realistic PR size" acceptance criterion has an edge-case exception.

## Inspiration & References

* [ADR-0019: Combined PR Review Body with Hidden Verdict Block](./0019-combined-pr-review-body-with-hidden-verdict-block.md) — the posting/parsing contract this decision preserves.
* [ADR-0014: PR Verification and LLM Judge Review Integration](./0014-pr-verification-and-llm-judge-review-integration.md) — the freshness and unconditional-block semantics.
* [ADR-0022: Enclosing Function Context Enrichment](./0022-enclosing-function-context-enrichment-for-pr-review-judges.md) — the enrichment pipeline that runs before chunking.
* [Qodo PR-Agent](https://github.com/qodo-ai/pr-agent) — `pr_agent/algo/pr_processing.py` implements the tiered batch-packing strategy (`pr_generate_extended_diff` → `pr_generate_compressed_diff` → `get_pr_multi_diffs`) that this decision adapts. Their `large_patch_policy` clip fallback informed the single-file clip path.
* [CodeRabbit](https://docs.coderabbit.ai) — considered and rejected; their agentic sandboxed exploration is architecturally incompatible with the current CI-side `review.py` design, but their judge-verifies-before-posting pattern validates the aggregation approach.
* [ToM: Tree-oriented MapReduce for Long-Context Reasoning (EMNLP 2025)](https://aclanthology.org/2025.emnlp-main.899.pdf) — validates the MapReduce pattern (split → map independently → reduce) for long-context LLM reasoning.
