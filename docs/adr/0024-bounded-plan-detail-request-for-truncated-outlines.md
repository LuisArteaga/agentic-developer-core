# ADR 0024: Bounded Plan Detail Request for Truncated Outlines

## Status
Accepted

## Context
The Plan-Node appends Structural Outlines to its prompt, capped at 30,000 characters (~7,500 tokens). When the combined directory tree + outlines exceed the cap, whole-file outlines are dropped in tree-walk order. A dropped outline means the planner lacks the vocabulary to name specific classes/methods in those files — a real Localization information loss the planner cannot self-recover from (the Plan-Node is not a ReAct agent; it has no tools to search the codebase).

## Decision
We add an optional `requested_files: List[str]` field (default empty) to the `DevelopmentPlan` structured output schema. When the outlines are truncated, the prompt surfaces the list of truncated files to the planner. If the planner populates `requested_files`, the Plan-Node fetches full outlines for those files (validating each against the filesystem and path-safety rules, silently skipping hallucinated or non-existent requests), enriches the prompt, and re-invokes the LLM exactly once. The second plan replaces the first entirely — no merging.

The bound (max one follow-up) is enforced in code, not by the LLM. The `requested_files` field in the second response is ignored — no recursion.

## Considered Options

### 1. Raise the cap so truncation rarely triggers
Rejected. The "Lost in the Middle" (Liu et al.) result and subsequent context-rot studies show that stuffing more content into the prompt degrades quality — every irrelevant or low-signal outline is actively harmful. A larger cap trades localization precision for context quality.

### 2. Aider-style PageRank ranking to pre-select the right files
Rejected for v1. Aider's repo-map ranking builds a dependency graph (~600–780 lines of core logic), runs personalized PageRank, and binary-searches to fit a token budget. This is the principled solution but is overkill for the small-to-medium repos this orchestrator currently targets. Tree-walk (alphabetical) truncation is acknowledged as a known limitation; graph-based ranking is recorded as a future enhancement.

### 3. Make the Plan-Node a ReAct agent that can search the codebase
Rejected. The Plan-Node is deliberately a single-pass planner: it receives a static snapshot and produces a structured plan. Giving it tools would blur the boundary between Planning (Localization — *where* to work) and Execution (Edit Synthesis — *how* to make the change), which ADR-0012 and CONTEXT.md keep strictly separated. The Worker is the ReAct agent; the Plan-Node is not.

### 4. Bounded follow-up (chosen)
A single targeted re-invocation is the right-sized middle ground: the planner gets one chance to articulate a specific gap, the system fills it, and the planner produces a better plan. This is a safety net for the ~5% of cases where tree-walk truncation drops a file the planner genuinely needs — not the primary mechanism for getting the right files into context.

## Consequences
- The `DevelopmentPlan` schema gains a field. Existing `state.json` files without `requested_files` deserialize correctly (Pydantic `default_factory=list`). Removing the field later would be a breaking change.
- One additional LLM call in the worst case (when truncation occurs AND the planner requests files). No call in the common case (small/medium repos where the cap is not hit).
- The bound is deterministic and code-enforced, satisfying the infinite-loop prevention requirement documented in agentic loop research.
