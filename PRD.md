# Product Requirement Document (PRD) — Stateful Python Orchestrator

## Project: agentic-developer-core — LangGraph & LangChain Migration

This document specifies the architecture and requirements for the stateful autonomous development loop. The orchestrator is built entirely in Python using LangGraph and LangChain, completely eliminating external CLI harnesses like Claude Code or OpenCode.

---

## 1. Objective & High-Level Summary

The goal is to build an autonomous agent developer loop that is robust, stateful, and model-agnostic. 

By implementing the loop entirely in Python:
1. **Full Graph Control**: LangGraph controls both the macro lifecycle (claim, plan, verify, PR, merge) and the micro execution steps (worker agent editing files, running tests, searching).
2. **Robust State & Resume**: Graph state is continuously serialized to `.agent_logs/state.json`. If the execution is interrupted or the container restarts, the orchestrator can resume exactly where it left off, skipping the clean checkout and preserving in-flight work.
3. **No CLI Dependencies**: We avoid wrapping terminal stream interfaces (like Claude Code CLI or OpenCode), resolving issues with unparseable outputs, lack of native OTLP tracing headers, and session file persistence.
4. **Model Agnosticism**: The system can execute using Kimi, DeepSeek, Claude, or other LLMs via standard LangChain integration.

---

## 2. Architectural Decisions

### 2.1 Pure Python LangGraph & LangChain Architecture
Both the high-level orchestration nodes (Claim, Plan, Verify, PR, Merge, Recovery) and the execution logic (Worker) are written in Python. The Worker is a LangChain ReAct agent equipped with custom local file-editing and terminal-execution tools.

### 2.2 Bash = Thin Process Supervisor
`scripts/entrypoint.sh` acts purely as a process launcher:
- Validates necessary environment variables (`OPENROUTER_API_KEY`, `GH_PAT`).
- Performs reachability checks.
- Spawns `python -m orchestrator` and handles process restart and backoff loops.
All git reset, cleaning, and workspace preparation logic is handled programmatically in the Claim/Poll node in Python.

### 2.3 Local-State Serialization for Resume
The graph state is written atomically to `.agent_logs/state.json` on every state transition. Since there is no external CLI, true work resume is simple:
- When the orchestrator starts, it loads `state.json`.
- If an in-progress issue and branch are detected, it checks out the existing branch, cleans up untracked files, and restarts the target phase node from scratch.
- No Docker host mounts for CLI session files are required.

### 2.4 Custom Tool Suite
The ReAct worker agent is equipped with a standard suite of Python tools:
- `read_file(path, start_line, end_line)`: View file contents.
- `patch_file(path, old_string, new_string)`: Apply targeted search-and-replace changes.
- `grep_search(query, path)`: Ripgrep-style search.
- `list_directory(path)`: List files in a directory.
- `run_command(command)`: Execute terminal commands (e.g., tests, linters) in a subprocess.

---

## 3. Component Breakdown

### 3.1 The `orchestrator/` Python Package
```
orchestrator/
├── __main__.py   # Entrypoint; builds/runs graph, loads state, and executes one issue iteration
├── graph.py      # LangGraph state machine compiling nodes, edges, and conditional routing
├── state.py      # TypedDict state schema + atomic load/save functionality to state.json
├── nodes.py      # High-level nodes: Claim, Plan, Verify, PR, Merge, Recovery
├── worker.py     # Worker agent definition (LangChain ReAct agent + Custom tools)
└── tools.py      # Custom Python tool implementations (file read, patch, grep, execution)
```

| Module | Responsibility |
|--------|----------------|
| `graph.py` | Compiles the LangGraph state machine. Routes between Plan, Worker, Verify, Recovery, PR, and Merge. |
| `state.py` | Standard `TypedDict` containing details about the active issue, current node, retry attempts, active branch, and LLM model. Saves/loads state atomically (tmp file + `os.replace`). |
| `nodes.py` | Performs Git operations (clone/checkout/push), GitHub API claims, and checks. Calls the Worker agent. |
| `worker.py` | Sets up the ReAct agent framework, binding the LLM and the tools. |
| `tools.py` | Standard Python code tools for safe file edits, ripgrep, and subprocess running. |

### 3.2 Telemetry Integration
Telemetry spans are created using the existing [scripts/telemetry.py](./scripts/telemetry.py) module. Because the orchestrator runs natively in Python, nodes import the tracing helpers directly, establishing clean parent-child span trees (e.g., `orchestrator_loop` parent containing `orchestrator_phase_plan`, `orchestrator_phase_execute`, etc.) without resorting to shell execution boundaries.

---

## 4. State Schema (`.agent_logs/state.json`)

| Field | Type | Purpose |
|-------|------|---------|
| `issue_number` | `int \| null` | The active claimed issue, or `null`. |
| `status` | `enum` | `idle \| claimed \| planning \| executing \| verifying \| pr_open \| merging \| done \| failed`. |
| `phase` | `str` | Active graph node / phase (for re-entry). |
| `attempts` | `dict` | Count of retries for Verify/Merge phases (max 3). |
| `branch` | `str \| null` | Git feature branch name for resume. |
| `model` | `str` | Active LLM model name. |
| `plan` | `str \| null` | The step-by-step development plan generated by the Plan node. |
| `read_files` | `list` | The list of file paths read by the Worker in the current cycle (for Read-Before-Edit safety verification). |
| `updated_at` | `str` | ISO-8601 timestamp of the last state save. |

---

## 5. Acceptance Criteria

- **AC1. Pure Python Implementation**: No dependencies on external CLI wrappers like Claude Code or OpenCode. The execution runs entirely in Python.
- **AC2. LangGraph State Machine**: Workflow transitions, conditional routing, and phase recovery are modeled as a LangGraph.
- **AC3. Persistent Resume**: State transitions are written atomically to `.agent_logs/state.json`. Interrupted cycles can resume by reading this file, restoring git state, and restarting from the recorded node.
- **AC4. Custom Agent Tools**: The ReAct Worker agent uses custom Python tools for reading, patching, searching, and running tests. Tool output is clean, formatted, and easily consumed by the agent.
- **AC5. Telemetry Integration**: High-level spans are recorded in-process via [scripts/telemetry.py](./scripts/telemetry.py) imports, forming structured span trees.
- **AC6. Test Compatibility**: The supervisor remains thin and compatible with workflow exit-code checks. All validation scripts pass cleanly.
