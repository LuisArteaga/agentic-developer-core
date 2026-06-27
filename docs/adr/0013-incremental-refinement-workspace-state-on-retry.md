# ADR 0013: Incremental Refinement Workspace State on Retry

## Status
Accepted

## Context
When the autonomous developer loop's verification phase (`Verify-Node`) fails, the orchestrator triggers a retry by routing back to the execution phase (`Execute-Node`). The worker agent is tasked with fixing the code changes using the captured test execution feedback.

An important architectural choice is how to manage the local filesystem workspace state during this loop transition. We evaluated two primary approaches:
1. **Hard Rollback**: Performing a `git reset --hard` back to the clean feature branch state before restarting the worker.
2. **Incremental Refinement**: Leaving the modified files in the workspace as-is, allowing the worker to read its previous edits and patch them.

We weighed these options across token efficiency, success rates, and context preservation.

## Decision
We decided to adopt **Incremental Refinement** (Alternative 2). The local filesystem state will remain intact when the graph loops back from `Verify-Node` to `Execute-Node`.

This decision is based on the following key points:
- **Refinement Efficiency**: Leaving the in-progress edits on disk allows the worker to perform localized, targeted corrections. The worker can read the modified files using the `read_file` tool and apply a precise patch using `patch_file`, which is highly efficient.
- **Token Conservation**: A hard rollback requires the worker to regenerate the entire code implementation from scratch, dramatically increasing LLM token consumption and the risk of the model failing to reproduce successful portions of the previous attempt.
- **TDD Alignment**: In a Test-Driven Development (TDD) loop, a human developer does not delete their code when a test fails; they refine the existing implementation. This incremental approach closely mirrors human software engineering practices.

To mitigate the risk of compounding errors or the worker getting stuck on its own corrupted edits:
- We enforce a hard limit of **3 retries** tracked in the state's `attempts` dictionary.
- We capture the raw, truncated stdout/stderr from the failed verification and present it to the worker as a structured warning block to guide its correction.

## Consequences

### Pros
- **High Success Rate for Complex Fixes**: The worker can build upon its prior work, making it far easier to fix minor syntax or logic bugs.
- **Reduced Cost and Latency**: Regenerating only the necessary diffs consumes fewer tokens and executes faster than rewriting entire files.
- **State Preservation**: In-flight changes and debugging stubs are preserved, maintaining full context.

### Cons
- **Risk of Compounding Errors**: If the worker introduces severe syntax errors or corrupts a file, it must diagnose and fix those errors on top of the original issue. The 3-attempt limit acts as a circuit breaker for these scenarios.

## Rejected Alternatives

### 1. Hard Rollback on Retry
- **Why Rejected**: Resetting the git workspace to a clean branch state deletes all progress made during the execution turn. If the initial implementation was 90% correct but had a single typo, a hard rollback discards the 90% correct code and forces the LLM to write it all again. This is expensive, slow, and highly prone to introducing new bugs.

---

## Inspiration & References
- **Aider AI Pair Programmer ([Aider Architecture](https://aider.chat))**: Aider operates directly on the active git workspace, applying incremental changes in-place and letting the user run tests. If tests fail, Aider refines the existing files rather than rolling them back, proving the high efficiency of incremental editing.
- **SWE-agent ([SWE-agent Paper](https://arxiv.org/abs/2405.15793))**: While benchmark environments sometimes use clean sandboxes for evaluation consistency, real-world development loops benefit heavily from the "Observe-Think-Act" paradigm where state persists across turns, allowing the agent to react dynamically to compiler and test outputs.
