# ADR 0033: Line-Range-Scoped Read-Before-Edit Constraint

## Status
Accepted (refines the Read-Before-Edit scope of [ADR 0006](./0006-block-based-patching-with-read-before-edit.md))

## Context
ADR-0006 introduced a programmatic Read-Before-Edit guard: the orchestrator state tracks every file the Worker reads in the current execution cycle (`read_files`), and `patch_file` refuses to edit a path that was not read. That guard is **path-scoped** — reading *any portion* of a file authorizes modifications to the *whole* file for the rest of the cycle.

This leaves a safety gap for autonomous (headless) operation, where no human reviews the diff before the PR:

- The Worker reads function A (lines 10–30) and then patches function B (lines 50–70) in the same file, relying on conversation memory rather than a fresh read of B. The path-level check passes (the file *was* read), so an edit grounded in stale or never-seen lines can be applied.
- Downstream quality gates (Verify, BinEval, PR Review Judges) may catch it, but upstream prevention is weaker than it could be.

The proposal: extend `read_files` from `list[str]` (paths) to a mapping of `path → list of [start, end]` inclusive line ranges, and have `patch_file` validate that the `old_string`'s line span falls within a previously read range.

### Industry baseline
This is deliberately *stricter* than mainstream agentic editors:

- **Anthropic's `text_editor` `str_replace`** does not track viewed ranges at all. Safety rests on the model having the content in context (so `old_str` is accurate) plus the *uniqueness check* (exactly one match). `view` / `view_range` only controls what enters context; there is no programmatic gate that `old_str` lies in a viewed range. ([Anthropic Text editor tool docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool))
- **Aider** search/replace blocks likewise rely on exact matching and layered (exact-then-fuzzy) matching, with no read-range tracking. ([Aider edit formats](https://aider.chat/docs/more/edit-formats.html))

Those tools assume either a human reviewer or short contexts where earlier reads remain live. Our loop runs headlessly across long trajectories where early reads can scroll out of context; range-scoping adds a cheap, deterministic upstream prevention layer (defense-in-depth) on top of the existing uniqueness gate.

## Decision
Extend the Read-Before-Edit guard from path-scoped to **line-range-scoped**:

1. **State schema**: `read_files` becomes `dict[str, list[list[int]]]` — each path maps to a list of `[start, end]` 1-based inclusive line ranges that have been read. Legacy `list[str]` state migrates to `{}` on load (safe: `read_files` is reset on phase start and on stateful resume, so in-flight legacy state is stale anyway).

2. **`read_file` registration**: a full read (no bounds) registers `[1, total_lines]`; a partial read registers `[start_line, end_line]` (or `[start_line, total_lines]` / `[1, end_line]` when only one bound is given). Reads merge into the existing ranges (overlapping/adjacent ranges collapse into a canonical disjoint set).

3. **`patch_file` range validation**: after the existing uniqueness (count) check, the tool locates the unique `old_string` match and computes its 1-based inclusive line span `[L_start, L_end]`. The edit is authorized iff `[L_start, L_end]` is fully covered by the union of the file's read ranges. On failure, the error names the edit's line span and the currently-read ranges so the Worker can self-correct with a targeted `read_file` in one retry.

4. **Post-edit range recomputation (line-drift handling)**: a successful patch shifts line numbers. Rather than clearing all ranges for the file (which would over-block legitimate multi-edit workflows and inflate Trajectory Length), ranges are recomputed precisely around the edited region:
   - Ranges **entirely above** the edit keep their line numbers (content above is untouched).
   - Ranges **entirely below** the edit shift by `delta = new_total_lines − old_total_lines` (content below moves in lockstep; since only one region is replaced, the shift at the edit point equals the total line-count delta).
   - Ranges **overlapping** the edit are split: the above-portion is kept, the below-portion is shifted, and the overlapped span (the rewritten content) is dropped — forcing a fresh read before re-editing that region.

Points 1 (block-based search-and-replace) and 2 (ambiguity abort) of ADR-0006 are unchanged.

## Consequences

### Pros
- **Narrows the stale-edit gap**: a partial read no longer authorizes edits to unread regions of the same file — the specific scenario described in issue #57 is now blocked deterministically, upstream of the PR gates.
- **Cheap and deterministic**: pure integer-interval arithmetic, no LLM in the loop, no extra tool calls in the common read-whole-file-then-edit path.
- **Preserves the common workflow**: reading a whole file still authorizes edits anywhere in it, so typical edit flows are unaffected; the gate only bites on partial reads.

### Cons
- **False-positive blocking on partial-read refactors**: a Worker that reads one region and edits an adjacent (unread) region is blocked and must re-read. The issue's trade-off table rates this "Medium"; mitigated by the split/shift recomputation, which keeps untouched regions valid across edits.
- **State complexity**: the schema carries intervals instead of bare paths, plus range merge/containment/recompute helpers. Bounded and fully unit-tested.
- **Trajectory Length impact**: range failures add `read_file` retries. Expected to be rare (the common path is full-file reads); observable via the existing TL metric.

## Inspiration & References
- [ADR 0006: Block-Based Patching with Read-Before-Edit](./0006-block-based-patching-with-read-before-edit.md) — the path-scoped guard this refines.
- [Anthropic Text editor tool — `str_replace` / `view_range`](https://platform.claude.com/docs/en/agents-and-tools/tool-use/text-editor-tool) — reference for the `str_replace` uniqueness model and 1-based `view_range` line bounds. Notably does **not** enforce read-range gating; we adopt a stricter stance for headless autonomy.
- [Anthropic SWE-bench Sonnet announcement](https://www.anthropic.com/news/swe-bench-sonnet) — documents the single-match `old_str`/`new_str` reliability strategy this tool builds on.
- [Aider edit formats](https://aider.chat/docs/more/edit-formats.html) and [Code Surgery: How AI Assistants Make Precise Edits](https://fabianhertwig.com/blog/coding-assistants-file-edits) — survey of search/replace formats and the "exact-then-fuzzy" matching convention; confirm that line-range read tracking is not an established industry pattern, framing this as a deliberate, domain-specific hardening.
