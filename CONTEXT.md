# Context: Agentic Developer Core

This document serves as the canonical glossary of domain concepts for the autonomous development loop. It is devoid of implementation details (such as LangGraph nodes or CLI options) and focuses purely on terminology.

## Glossary

- **Autonomous Developer Loop**: The end-to-end lifecycle of an issue being processed by the agent. It spans from claiming a ticket to planning, execution, verification, pull request creation, and merging.
- **Orchestrator**: The high-level coordinator that manages the lifecycle of the loop, guiding work through the distinct phases and handling high-level routing, errors, and task assignment.
- **Worker**: The active agent that performs the actual code-writing, searching, and code modifications within the codebase workspace.
- **Workspace Hygiene**: The practice of cleaning and resetting the workspace (e.g., git cleaning, directory resetting) to guarantee a consistent environment.
- **Stateful Resume**: The capability of the loop to recover from a crash or system restart and resume execution from the exact phase and task it was working on, preserving in-progress work.
- **Verification**: The process of running tests, linters, or other build checks to ensure that modifications meet quality standards and do not introduce regressions.
- **Block-Based Patching**: The method of modifying code by replacing an exact, unique block of lines (`old_string`) with a replacement block (`new_string`), rather than using line numbers or diff syntax.
- **Read-Before-Edit Constraint**: A strict safety rule requiring the Worker to programmatically read a file's contents in the current cycle before applying any patches to it, eliminating hallucinated or stale edits.
- **Blocked by**: A structured markdown section (`## Blocked by`) in an issue body containing references to other issues (e.g., `- #123`). The Orchestrator parses this section, labeling the active issue as `blocked` if any referenced issues are open, and clearing it once all dependencies are closed.

