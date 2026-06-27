# ADR 0016: In-Process Telemetry and Stateful Resume Tracing

* **Status**: Accepted
* **Date**: 2026-06-27
* **Deciders**: Luis Arteaga & Antigravity

## Context and Problem Statement
The autonomous developer orchestrator has transitioned to a pure Python LangGraph workflow. Telemetry loop and phase spans must be captured directly in-process rather than externally via shell execution boundaries.

Because the orchestrator runs in a containerized environment under a process supervisor, the execution loop can crash, time out, or be paused and resumed. Tracing these stateful workflows across process boundaries is challenging because standard tracing frameworks assume in-memory continuity within a single running process. We need a way to construct and preserve parent-child span hierarchies across restarts without leaving spans hanging open in memory or losing tracing data.

## Decision Drivers
* **Trace Continuity**: Parent loop spans must correctly encompass all child phase spans (planning, test writing, execution, verification) even if the process was killed and restarted.
* **Workspace and Test Hygiene**: Telemetry logs and temporary state files must be fully isolated during test runs to avoid polluting the host codebase or causing resource collisions in concurrent test suites.
* **Radical Simplicity & Graceful Degradation**: Telemetry instrumentation must not impact execution stability. If the OpenTelemetry library is missing or the exporter endpoint is unreachable, tracing must fall back to no-op mode silently.

## Considered Options
* **Option 1: Live Real-Time Span Streaming**
  Open live OpenTelemetry spans at the beginning of the execution loop and keep them active in memory.
  * *Cons*: If the supervisor kills the process due to a timeout or crash, the active parent trace context is lost. Standard trace collectors (like Phoenix or Honeycomb) reject or orphan spans that lack a clean termination signal.
* **Option 2: Disk-Based State Persistence with Retrospective Exporting**
  Persist phase start/end times and metadata to a local, secure JSON file (`telemetry_state.json`) on disk during execution. Upon workflow completion, load this state file, retrospectively initialize the spans with the recorded Unix timestamps, manually reconstruct the parent-child context hierarchy, and export them.

## Decision
We chose **Option 2**.

By writing telemetry checkpoints to disk, we decouple trace recording from the lifetime of a single Python process. 
* On a **fresh run**, we initialize a clean telemetry state file.
* On a **stateful resume**, we call `init_telemetry(reset_state=False)` to preserve the existing telemetry logs on disk. Subsequent phase starts/ends load the existing trace context and append to it.
* At the **end of the workflow** (successful or failed), we compile the state file and emit the traces retrospectively, resolving parent context propagation manually before deleting the temporary state file.

To isolate test runs, we also updated the log directory resolution to respect `AGENT_LOG_PATH`, aligning it with the orchestrator's state persistence module.

### Consequences
* **Pros**:
  * **Complete Crash Resilience**: Spans are never lost or orphaned by process crashes.
  * **Accurate Hierarchies**: Loop-level trace context is reconstructed perfectly across resumes.
  * **Clean Test Isolation**: Testing telemetry with `InMemorySpanExporter` is fully isolated via `AGENT_LOG_PATH` and temporary directories.
* **Cons**:
  * **Deferred Visibility**: Traces are only pushed to the collector at the end of the loop execution rather than streamed in real-time.

## Inspiration & References
* **Temporal IO Stateful Tracing ([Temporal Tracing Guide](https://docs.temporal.io/production-readiness/tracing))**: Stateful workflow orchestration platforms avoid leaving spans open across asynchronous pauses or activity restarts, relying instead on persisted contexts.
* **OpenTelemetry Manual Context Propagation ([OTel Python Docs](https://opentelemetry.io/docs/languages/python/instrumentation/#manually-propagating-context))**: Propagating parent context manually by deserializing identifiers and setting them as span parent contexts is the standard practice for asynchronous message queues and decoupled executors.
