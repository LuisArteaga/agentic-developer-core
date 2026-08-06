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

## Amendment (2026-08): Synchronous OTLP Export via SimpleSpanProcessor

### Problem
The OTLP remote exporter was attached as a `BatchSpanProcessor`, which buffers ended spans and flushes them on a 5s background timer (or on `force_flush()`). **No `force_flush()` or `shutdown()` is ever called** in the orchestrator loop — `end_orchestrator_loop` creates and ends all spans in `_export_recorded_spans()` and the process may exit immediately after. A batched processor can therefore lose the entire workflow's remote trace if the process exits before the next background flush, since spans only end at workflow completion (the architecture is fully retrospective).

### Resolution
The OTLP exporter is now wrapped in a `SimpleSpanProcessor`, which exports each ended span **synchronously** on the calling thread inside `on_end`. By the time `_export_recorded_spans()` returns, every span has already been pushed to the OTLP endpoint, so no `force_flush()` is required and nothing is lost on process exit. The `LocalJSONLFileSpanProcessor` crash-resilient fallback is unchanged.

Trade-off: `SimpleSpanProcessor` performs the OTLP HTTP call on the calling thread, so a slow or unreachable endpoint blocks `_export_recorded_spans` up to the exporter timeout per span (normal case: milliseconds). This only affects workflow-exit latency, never in-run execution, and a failed export is caught and logged by the processor (never crashes the orchestrator). The graceful-degradation contract (endpoint absent → no OTLP processor attached) is unchanged.

### Important limitation — this does NOT give live in-run visibility
Because spans are still created and ended **retrospectively** in `_export_recorded_spans()` (called once, from `end_orchestrator_loop`), this swap yields immediate export **at workflow end** and exit-safety, but traces remain invisible *during* a long run. True live streaming would require emitting each phase span at `end_orchestrator_phase` time. That is a separate, ADR-governed decision because every feasible approach regresses a guarantee of the original Option 2:

- **Option A — live loop span (open in memory):** create the loop span at `start_orchestrator_loop` and end it at `end_orchestrator_loop`. Phase spans emitted live as its children. Regression: the open loop span is lost on a crash (never ended/exported) — exactly the orphaned-parent failure Option 2 was chosen to avoid.
- **Option B — retrospective loop span + live phase spans:** emit phase spans live with a persisted loop span as remote parent. Infeasible via the public OTel API: `Tracer.start_span` auto-generates each span's own `span_id` (only *parent* references can be injected via a remote `SpanContext`). A retrospective loop span would therefore receive an SDK-generated `span_id` distinct from any value its live children reference, breaking the parent-child link the existing hierarchy tests assert.

A hybrid requiring a forced span_id would depend on private/fragile SDK APIs and is out of scope. The retrospective-to-live transition is deferred until its regression (lost loop span on crash) is an accepted trade-off.

## Inspiration & References
* **Temporal IO Stateful Tracing ([Temporal Tracing Guide](https://docs.temporal.io/production-readiness/tracing))**: Stateful workflow orchestration platforms avoid leaving spans open across asynchronous pauses or activity restarts, relying instead on persisted contexts.
* **OpenTelemetry Manual Context Propagation ([OTel Python Docs](https://opentelemetry.io/docs/languages/python/instrumentation/#manually-propagating-context))**: Propagating parent context manually by deserializing identifiers and setting them as span parent contexts is the standard practice for asynchronous message queues and decoupled executors.
* **OpenTelemetry Python SDK — `SpanProcessor` Independence ([OTel Python SDK `export` module](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.export.html))**: Multiple `SpanProcessor`s on a single `TracerProvider` operate independently. A `SimpleSpanProcessor` (local file) and a `BatchSpanProcessor` (OTLP) do not block or interfere with each other.
* **OpenTelemetry Python Cookbook — Provider Registration ([OTel Cookbook](https://opentelemetry.io/docs/languages/python/cookbook))**: The canonical pattern is to create the `TracerProvider`, add all processors, and call `set_tracer_provider()` before any tracing code runs.
* **`set_tracer_provider()` `Once()` Guard ([OTel Python `trace` module source](https://opentelemetry-python.readthedocs.io/en/latest/_modules/opentelemetry/trace.html))**: The global provider can only be set once; subsequent calls log a warning and are no-op. This justifies the existing "already registered provider" guard in `init_telemetry()`.
* **"No Tracer Provider Configured" Pattern ([OneUptime](https://oneuptime.com/blog/post/2026-02-06-fix-no-tracer-provider-configured-warnings/view))**: Registering the provider before application code starts creating spans is the recommended practice to avoid silent no-op tracers.
* **OpenTelemetry Python `SimpleSpanProcessor` ([OTel Python SDK `export` module](https://opentelemetry-python.readthedocs.io/en/latest/sdk/trace.export.html))**: "passes ended spans directly to the configured SpanExporter" — synchronous export on the calling thread. Chosen over `BatchSpanProcessor` (buffers, 5s background flush per the [Tracing SDK spec defaults](https://opentelemetry.io/docs/specs/otel/trace/sdk): `scheduledDelayMillis=5000`) because no `force_flush()` is called in the loop, so a batched processor could lose spans on quick process exit.
* **Langfuse — "Integrate with an existing OpenTelemetry setup" ([Langfuse docs](https://langfuse.com/faq/all/existing-otel-setup))**: Langfuse's own canonical example wires `new SimpleSpanProcessor(new OTLPTraceExporter(...))` alongside its processor — validating synchronous export as a supported, real-time pattern (Langfuse v4 advertises real-time ingestion).
* **CNCF — "OpenTelemetry best practices (overview part 2/2)" ([CNCF blog](https://www.cncf.io/blog/2020/06/26/opentelemetry-best-practices-overview-part-2-2/))**: Documents the two export strategies — `SimpleSpanProcessor` submits a span on every finish; `BatchSpanProcessor` buffers until a flush event (buffer-full or `schedule_delay`) — and their throughput/latency trade-offs.
