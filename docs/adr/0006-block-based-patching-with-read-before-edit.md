# ADR 0006: Block-Based Patching with Programmatic Read-Before-Edit Constraint

## Status
Accepted

## Context
Code-editing models (like Claude 3.7 Sonnet) perform best when modifying files via exact search-and-replace strings (`old_string` -> `new_string`). However, this is prone to two primary failure modes:
1. **Outdated edits (Stale context)**: The model suggests an edit based on what it remembers from earlier in the conversation, but files on disk have changed (e.g. due to other edits or tool runs).
2. **Ambiguity**: If a target block appears multiple times in a file, replacing it blindly can result in incorrect or broken code.

To solve this, Anthropic's Claude Code uses a strict editing workflow requiring the agent to read files before editing, and failing when search strings are not unique.

## Decision
We will implement our file-editing tool (`patch_file`) and the orchestrator state to enforce these safety guards programmatically:

1. **Exact Block-Based Search-and-Replace**: The `patch_file` tool will accept `old_string` and `new_string`. It will perform an exact match lookup on the target file.
2. **Ambiguity Abort**: If the `old_string` matches multiple times in the file, or matches zero times, the tool will abort and return a descriptive error instructing the model to provide more context lines.
3. **Programmatic Read-Before-Edit**: The orchestrator's LangGraph state will keep track of all files read by the Worker in the current task execution cycle (`read_files` list). If the Worker invokes `patch_file` on a path not present in `read_files`, the tool will abort with a validation error, forcing the agent to read the file first.

## Consequences

### Pros
- **Prevention of Hallucinated Edits**: The agent cannot guess changes or use stale internal versions of a file; it must synchronize its state with the actual file on disk by calling `read_file` first.
- **Explicit Context Windows**: Forces the model to keep its context updated.
- **Safety**: Avoids corrupting files through ambiguous multi-matching string replaces.

### Cons
- **Additional Tool Call Overhead**: The agent must execute at least two tool calls (`read_file` then `patch_file`) for every file it wishes to modify, increasing token usage and latency.
- **State Complexity**: The orchestrator state must now carry a list of read file paths and clear it at the beginning of each planning/execution cycle.
