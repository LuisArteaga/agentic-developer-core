# ADR 0008: Phase-Level Resume and Graph State Planning

## Status
Superseded by [ADR 0009: Pure Python LangGraph Orchestration without CLI Harnesses](../0009-pure-python-langgraph-orchestrator.md)

## Context
In an autonomous development loop running inside containerized environments (like GitHub Actions or Docker containers), the process can be killed, timed out, or restarted. We need a way to resume the execution loop safely. 

Furthermore, we need a method to transfer the development plan generated during the `Plan` phase to the `Execute` (Worker) phase.

## Decision
We will implement a lightweight, workspace-centric state persistence design:

1. **Phase-Level Resume**: We will record high-level metadata (such as the active issue, the git branch name, and the active phase like `planning` or `executing`) in `.agent_logs/state.json`. Upon restart, we checkout the active branch, clean up untracked files, and restart the target phase node from scratch.
2. **Workspace Inspection**: The Worker agent will use the checked-out git branch as its primary source of truth. Since file changes are written to the branch, the Worker will inspect the current file states (helped by the Read-Before-Edit rule) to resume its coding tasks, rather than replaying internal chat history.
3. **Graph State Planning**: The development plan will be stored as a string field (`plan`) within the LangGraph State object, which automatically gets serialized into the global `state.json` file.

## Rejected Alternatives

### 1. Full Graph Checkpoint Serialization (SqliteSaver/JsonlSaver)
We rejected serializing the full LangGraph checkpoint tree (including LLM chat history and internal memory states) to SQLite or JSONL on the host.
- **Why**: Under the *Lazy Coding Principle*, full history serialization adds significant overhead, requires DB file maintenance, and is prone to synchronization errors if files are modified on disk but not in the DB. Since the modified code remains in the Git branch, the workspace state naturally guides the agent on resume.

### 2. Local Markdown Plan Files (`plan.md` / `plan.json`)
We rejected writing the plan to a separate physical file in the repository (like `plan.md`).
- **Why**: Writing a separate file splits the agent state between `state.json` and `plan.md`, introducing risks of state drift and requiring manual file cleanup. Because this is a backend autonomous loop, the plan does not need to be formatted as an interactive IDE file for human editing mid-run. Keeping it inside the Graph State is cleaner and simpler.

## Consequences

### Pros
- **Radical Simplicity**: No database checkpointers, complex serialization logic, or lock management are required.
- **Single Source of Truth**: All orchestrator state resides in `state.json`, and all code progress resides in the Git branch.
- **Robustness**: Hard container resets do not corrupt DB states; checking out the Git branch is a safe, standard operation.

### Cons
- **Re-Execution Latency**: When resuming, the Worker agent must re-evaluate the codebase files and re-plan its micro steps because its conversational history is not restored.
