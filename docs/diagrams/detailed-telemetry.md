# Diagram: Telemetry pipeline

The telemetry sidecar: spans are checkpointed to disk during the run and
compiled retrospectively at workflow end, then exported to a local JSONL
file and, when configured, an OTLP endpoint (Langfuse Cloud or a generic
collector). Includes the deferred-visibility limitation. One diagram;
rendered natively by GitHub.

```mermaid
flowchart TD
    INIT["init_telemetry<br/>fresh run resets the state file<br/>resume keeps existing trace context"] --> RUN

    subgraph RUN["During the run - disk checkpoints only"]
        PH["start / end orchestrator loop and phase<br/>orchestrator_loop, orchestrator_phase_*<br/>phase times + metadata persisted"]
        DISK["telemetry_state.json on disk<br/>survives crashes and restarts"]
        SB["Security block event<br/>recorded with ERROR status + tag"]
    end

    PH --> DISK
    SB --> DISK

    DISK --> END["Workflow ends<br/>end_orchestrator_loop - always, success or failure"]

    END --> COMPILE["Retrospective span compilation<br/>spans created with recorded timestamps,<br/>parent-child hierarchy reconstructed manually"]

    COMPILE --> CHECK{"OTel library installed?"}

    CHECK -->|"no"| NOOP["No-op mode<br/>execution is never impacted<br/>no local JSONL either"]

    CHECK -->|"yes"| EXPORT["TracerProvider registered unconditionally<br/>processors attached conditionally"]

    EXPORT --> JSONL["LocalJSONLFileSpanProcessor<br/>local JSONL trace file<br/>always on - first-class"]

    EXPORT --> LF{"Langfuse keys set?<br/>both LANGFUSE_PUBLIC_KEY and<br/>LANGFUSE_SECRET_KEY"}

    LF -->|"yes"| LANGFUSE["OTLP export to Langfuse Cloud<br/>Basic auth + session baggage<br/>langfuse.session.id groups one issue cycle"]

    LF -->|"no"| OTEL["OTLP export to OTEL_EXPORTER_OTLP_ENDPOINT<br/>Docker default host.docker.internal:4318<br/>SimpleSpanProcessor - synchronous, exit-safe"]

    subgraph LIMIT["Deferred visibility limitation"]
        LIM["Spans become visible only at workflow end<br/>no live in-run view - by design:<br/>an open in-memory loop span would be<br/>lost on a crash, the exact failure<br/>the disk-based design avoids"]
    end

    END -.-> LIM
```

## Grounded in

- **ADR-0016** — disk-based state persistence with retrospective exporting;
  the 2026-07 amendment (local JSONL decoupled from OTLP export) and the
  2026-08 amendment (synchronous export via `SimpleSpanProcessor`, which does
  not create live in-run visibility).
- **ADR-0029** — run metrics are the deliberate contrast: in-memory,
  no crash persistence, one record per completed cycle.
- **ADR-0044** — security-block span events with ERROR status and the
  `security-block` tag.
- **Code** — `scripts/telemetry.py` (`init_telemetry`, `start_/end_orchestrator_loop`,
  `start_/end_orchestrator_phase`, `record_security_block`,
  `_export_recorded_spans`), `scripts/redaction.py` (secret redaction before
  persistence/export).

## Notes

- Worker and Test-Writer ReAct conversations are separate JSONL sidecars
  (`.agent_logs/`, post-hoc debugging only) — they are never loaded back into
  the loop and are not part of the OTLP span export.
- A failed or absent OTLP exporter never blocks the local file processor;
  tracing never breaks execution.
- Configurable values shown: `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
  (Langfuse export), `OTEL_EXPORTER_OTLP_ENDPOINT` (generic OTLP; defaulted
  to `host.docker.internal:4318/v1/traces` only inside Docker when unset).
