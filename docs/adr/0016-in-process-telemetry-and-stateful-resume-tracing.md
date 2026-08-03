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
* **Radical Simplicity & Graceful Degradation**: Telemetry instrumentation must not impact execution stability. If the OpenTelemetry library is missing, tracing falls back to no-op mode silently. If the library is present but the OTLP endpoint is absent, tracing degrades to **local-only** JSONL logging — the `TracerProvider` and `LocalJSONLFileSpanProcessor` remain active; only remote OTLP export is skipped. Local observability is a first-class, always-on concern; "no-op" protects execution, not visibility.

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

## Amendment (2026-07): Decoupling Local JSONL from OTLP Export

### Problem
The original Decision Driver #3 stated that tracing "must fall back to no-op mode silently" when the exporter endpoint is unreachable. The implementation in `init_telemetry()` took this literally: when `OTEL_EXPORTER_OTLP_ENDPOINT` was empty, the function returned early *before* calling `trace.set_tracer_provider(provider)`. This meant the `TracerProvider` was never registered globally, disabling not just OTLP export but also the crash-resistant `LocalJSONLFileSpanProcessor`. Local JSONL traces — a first-class observability concern — were silently killed alongside remote export.

### Resolution
Decision Driver #3 has been sharpened (see above) to distinguish two degradation tiers:
1. **OTel library missing** → no-op mode (unchanged).
2. **Library present, OTLP endpoint absent** → local-only JSONL logging. The provider is always registered; only the OTLP `BatchSpanProcessor` is conditionally attached.

This aligns with the OpenTelemetry SDK's multi-processor design: multiple `SpanProcessor`s on a single `TracerProvider` are independent — a failed or absent OTLP exporter never blocks the local file processor. The canonical pattern is to register the provider early and unconditionally, then attach processors conditionally.

### Scope
This is a bug fix against intent, not a reversal of the original decision. The disk-based state persistence and retrospective exporting architecture (Option 2) is unchanged. Only the provider registration coupling is corrected.

## Inspiration & References
* **Temporal IO Stateful Tracing ([Temporal Tracing Guide](https://docs.temporal.io/production-readiness/tracing))**: Stateful workflow orchestration platforms avoid leaving spans open across asynchronous pauses or activity restarts, relying instead on persisted contexts.
* **OpenTelemetry Manual Context Propagation ([OTel Python Docs](https://opentelemetry.io/docs/languages/python/instrumentation/#manually-propagating-context))**: Propagating parent context manually by deserializing identifiers and setting them as span parent contexts is the standard practice for asynchronous message queues and decoupled executors.
* **OpenTelemetry Python SDK — `SpanProcessor` Independence ([OTel Python SDK `export` module](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.export.html))**: Multiple `SpanProcessor`s on a single `TracerProvider` operate independently. A `SimpleSpanProcessor` (local file) and a `BatchSpanProcessor` (OTLP) do not block or interfere with each other.
* **OpenTelemetry Python Cookbook — Provider Registration ([OTel Cookbook](https://opentelemetry.io/docs/languages/python/cookbook))**: The canonical pattern is to create the `TracerProvider`, add all processors, and call `set_tracer_provider()` before any tracing code runs.
* **`set_tracer_provider()` `Once()` Guard ([OTel Python `trace` module source](https://opentelemetry-python.readthedocs.io/en/latest/_modules/opentelemetry/trace.html))**: The global provider can only be set once; subsequent calls log a warning and are no-op. This justifies the existing "already registered provider" guard in `init_telemetry()`.
* **"No Tracer Provider Configured" Pattern ([OneUptime](https://oneuptime.com/blog/post/2026-02-06-fix-no-tracer-provider-configured-warnings/view))**: Registering the provider before application code starts creating spans is the recommended practice to avoid silent no-op tracers.
