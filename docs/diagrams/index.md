# Architecture Diagrams

Mermaid.js diagrams documenting the Orchestrator from complementary viewpoints.
GitHub renders every diagram natively — there are no images and no external
services. Each diagram lives in its own file (one diagram per file) so a
deep link always points at exactly one picture, and each file lists the ADRs
and source files it is grounded in.

| Diagram | Viewpoint | Grounded in |
| --- | --- | --- |
| [Setup flow](setup.md) | Getting a run up: prerequisites, configuration, keys, the Process Supervisor startup chain, and what a started run produces. | ADR-0015, ADR-0018, ADR-0056 |
| [Usage process](process.md) | The intended usage process: define an issue → orchestrator cycle (plan → test-writer → execute → verify) → PR verification + judge review → merge, retry, or resume. | ADR-0009, ADR-0010, ADR-0014, ADR-0036 |
| [Architecture overview](architecture-overview.md) | Coarse component view: LangGraph orchestrator loop, Worker ReAct agent, state persistence, telemetry sidecar, run metrics, Execution Sandbox, PR judge review. | ADR-0009, ADR-0011, ADR-0016, ADR-0029, ADR-0056, ADR-0059 |
| [Worker reasoning budget](detailed-worker-budget.md) | The three-layer defense around the Worker's ReAct loop: Tool Loop Detection middleware, the Recursion Budget crash guard, and graceful exhaustion. | ADR-0045, ADR-0046, ADR-0053 |
| [Retry & workspace state](detailed-retry-workspace.md) | Hybrid Retry: incremental refinement vs. hard rollback on the final attempt, per-gate verify budgets, the Workspace Snapshot, and the post-PR Merge-Fix Loop. | ADR-0013, ADR-0034, ADR-0047, ADR-0053, ADR-0036 |
| [Telemetry pipeline](detailed-telemetry.md) | Telemetry sidecar: disk-state persistence → retrospective span compilation → local JSONL + OTLP export (Langfuse or generic endpoint), including the deferred-visibility limitation. | ADR-0016, ADR-0029, ADR-0044 |
| [PR judge review](detailed-judge-review.md) | PR verification + judge review: diff enrichment and batching, the hidden verdict block, merge polling with merge-fix and check-run fail-fast triage. | ADR-0014, ADR-0019, ADR-0022, ADR-0023, ADR-0036, ADR-0055, ADR-0059 |

## Revision note

Diagrams reflect `main` as of 2026-09 (after ADR-0059: quality gates and the
judge pipeline are consumed from the quality-gates-toolkit at `v1.7.0`).
Behavior that is configurable is drawn with its current default and marked as
configurable — thresholds can drift; the source ADRs and code files listed in
each diagram are the ground truth. Where code and an ADR could disagree, the
diagram follows the code and flags the divergence in its source list.
