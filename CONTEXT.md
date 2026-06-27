# Context: Agentic Developer Core

This document serves as the canonical glossary of domain concepts for the autonomous development loop. It is devoid of implementation details (such as LangGraph nodes or CLI options) and focuses purely on terminology.

## Glossary

- **Autonomous Developer Loop**: The end-to-end lifecycle of an issue being processed by the agent. It spans from claiming a ticket to planning, execution, verification, pull request creation, and merging.
- **Orchestrator**: The high-level coordinator that manages the lifecycle of the loop, guiding work through the distinct phases and handling high-level routing, errors, and task assignment.
- **Worker**: The active agent that performs the actual code-writing, searching, and code modifications within the codebase workspace.
- **Workspace Hygiene**: The practice of cleaning and resetting the workspace (e.g., git cleaning, directory resetting) to guarantee a consistent environment.
- **Stateful Resume**: The capability of the loop to recover from a crash or system restart and resume execution from the exact phase and task it was working on, preserving in-progress work.
- **Verification**: The process of running tests, linters, or other build checks to ensure that modifications meet quality standards and do not introduce regressions.
- **Verification Feedback**: The truncated execution output (maximum 150 lines or 10 KB) captured from a failed verification subprocess. This feedback is persisted in the orchestrator state and fed back to the worker agent in subsequent execution turns to guide self-correction.
- **Block-Based Patching**: The method of modifying code by replacing an exact, unique block of lines (`old_string`) with a replacement block (`new_string`), rather than using line numbers or diff syntax.
- **Planning**: The phase of the autonomous developer loop where the orchestrator uses a language model to analyze a claimed issue alongside the codebase structure to produce a step-by-step development plan.
- **Process Supervisor**: A lightweight, zero-dependency Python wrapper (implemented in `scripts/entrypoint.py`) that acts as the entrypoint for the orchestrator container. It validates essential environment variables, verifies network reachability to external services, and manages a resilient execution loop for the orchestrator with exponential crash backoff.
- **Read-Before-Edit Constraint**: A strict safety rule requiring the Worker to programmatically read a file's contents in the current cycle before applying any patches to it, eliminating hallucinated or stale edits. Reading any portion of a file authorizes modifications to the whole file for the duration of the cycle, whereas search queries do not grant modification authority. The record of read files is reset at the start of each phase and upon stateful resume.
- **Blocked by**: A structured markdown section (`## Blocked by`) in an issue body containing references to other issues (e.g., `- #123`). The Orchestrator parses this section, transitioning the issue's label to `agent-blocked` if any referenced issues are open, and restoring it to `agent-ready` once all dependencies are closed.
- **Target Repository**: The remote GitHub repository containing the issues to resolve and the codebase to modify (specified via `GITHUB_REPOSITORY`).
- **Workspace Isolation**: The sandboxing of all worker file edits and command executions inside the local directory specified by `GITHUB_WORKSPACE`, keeping it separate from the orchestrator's own codebase and state logs.
- **Self-Healing Claim**: The two-step programmatic polling process in the Claim-Node that automatically unblocks issues when their dependencies close (transitioning them from `agent-blocked` to `agent-ready`), and claims eligible issues (transitioning them from `agent-ready` to `agent-in-progress`).
- **Pull Request Creation**: The phase of the autonomous developer loop where a pull request is opened to propose the verified codebase modifications for review.
- **Merge Polling**: The process of repeatedly checking the merge status of an open pull request until it is successfully merged or a timeout occurs.
- **Failure Recovery**: The safety process triggered upon total execution failure or timeout, which cleans up local changes and restores the issue's GitHub label back to its ready state, allowing for future attempts.


