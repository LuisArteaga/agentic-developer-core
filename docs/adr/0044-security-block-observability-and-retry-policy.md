# ADR 0044: Security-Block Observability, Quarantine, and Circuit Breaker

## Status

Accepted

## Context

When the orchestrator blocks a plan or runtime path for security reasons (e.g. `.env` in `target_files`), the run stops hard and is **indistinguishable from a crash**:

- The Plan-Node raises a `ValueError("Security Block: ...")` → caught by the generic exception handler → `status="failed"` → recovery → exit code 1.
- The process supervisor (`scripts/entrypoint.py`) treats exit 1 as a crash and applies exponential backoff, then re-claims the same issue on the next iteration → **tight loop on a permanently-failing issue**.
- The incident is **not searchable** in Langfuse (no tag, no span event, no ERROR status on the relevant span).

Research consensus: security blocks are **permanent, non-retriable** failures. They must be quarantined, made searchable, and circuit-broken — not retried with backoff.

## Decision

Introduce a four-layer security-block handling strategy: distinct exit code, issue quarantine, Langfuse observability, and a supervisor circuit breaker.

### 1. Distinct exit code (C1)

A security-block failure sets `state["error"]` with the prefix `security_block:` (e.g. `security_block: .env`), distinct from the generic `plan: ...` / `execute: ...` error prefixes. The Plan-Node handles the block directly at the block site (telemetry + error prefix + early return) rather than raising a `ValueError` caught by the generic handler, so the prefix is not overwritten.

`orchestrator/__main__.py` maps a `security_block:`-prefixed failure to exit code **42** (`SECURITY_BLOCK_EXIT_CODE`), distinct from the generic crash exit 1. The supervisor branches on this code.

### 2. Quarantine (C2)

`recovery_node` detects the `security_block:` prefix and labels the issue **`agent-blocked`** (reusing the existing label, auto-created by `_add_label`) instead of `agent-ready`. The claim scan's Step 2 (which scans `agent-ready`) skips it; the existing Unblocking Scan (Step 1, scans `agent-blocked`) remains for human-initiated unblocks. A human can manually remove `agent-blocked` to re-queue the issue after remediation.

Non-security crashes keep the existing `agent-ready` reset + exponential-backoff path (unchanged).

**Circuit breaker** in `scripts/entrypoint.py`: a `SecurityBlockTracker` (rolling-window `collections.deque` of timestamps) tracks security-block exits. If **≥5** security blocks occur within a 60-minute window, the supervisor halts with a `CIRCUIT_BREAKER` log and exit code 2. This prevents an active prompt-injection attack from churning across multiple quarantined issues. Threshold and window are configurable: `AGENT_SECURITY_BLOCK_THRESHOLD` (default 5), `AGENT_SECURITY_BLOCK_WINDOW` (default 3600s).

### 3. Langfuse observability (C3)

A `record_security_block(layer, blocked_path, issue_number)` helper in `scripts/telemetry.py` records the breach to the telemetry `_state`. During retrospective span export (`_export_recorded_spans`), the record is attached to the matching phase span (by timestamp interval) as:

- A `security.block` span event with attributes (`security.layer`, `security.blocked_path`, `security.issue_number`, `security.severity`).
- Span status ERROR (overriding the phase's own exit-code-derived status — a runtime refusal during an otherwise-successful execute is still a breach).
- The `langfuse.trace.tags` attribute set to `["security-block"]` on both the phase span and the loop span.

This makes the breach searchable in Langfuse via `tag:security-block` (trace-level filter), visible as an ERROR in error views, and inspectable in trace detail via the span event.

**Adaptation from the issue's proposed design (Framework-First Research, ADR-0042):** the issue proposed `trace.get_current_span().add_event(...)` — a pattern that would be a **no-op** in this project. Spans are created *retrospectively* in `_export_recorded_spans` (ADR-0016), not live via `start_as_current_span`. There is no active recording span at the time tools or nodes run. The `record_security_block` helper buffers the event to `_state`, and `_export_recorded_spans` attaches it during export. The native alternative (`start_as_current_span` live spans) was evaluated and rejected because it would require restructuring the entire telemetry architecture (retrospective spans exist to provide crash-resistant JSONL logging + OTLP export without in-run flushing — ADR-0016).

**Langfuse attribute correction:** the issue proposed `span.set_attribute("langfuse.tags", [...])`. The correct key is **`langfuse.trace.tags`** (type `string[]`), per the Langfuse OTel attribute mapping. `langfuse.tags` (without the `.trace.` segment) would land in the unfilterable `metadata.attributes` catch-all, failing the acceptance criterion (`tag:security-block` filter in Langfuse UI).

### 4. Retry / quarantine / alert matrix

| Failure type | Retried? | Mechanism | Langfuse? | Quarantine? |
|---|---|---|---|---|
| Security block (plan/runtime) | No | quarantine issue (`agent-blocked`), exit 42 | tag + event + ERROR | yes |
| Transient crash (network/OOM) | Yes | exp. backoff + jitter (existing) | no | no |
| Verify/test failure | Yes | Hybrid Retry (ADR-0034) | no | no |
| ≥5 security blocks / 60min | No | circuit-breaker: halt supervisor (exit 2) | tag + event + ERROR | n/a |

## Consequences

- **No tight loops on security blocks**: a blocked issue is quarantined and skipped by the claim scan; the supervisor does not re-claim it with backoff.
- **Searchable breaches**: `tag:security-block` in Langfuse returns all security-block incidents, with the blocked path, layer, and issue number in the span event attributes.
- **Attack resilience**: the circuit breaker halts the supervisor if an active injection attack triggers ≥5 blocks in 60 minutes, preventing resource churn.
- **Exit code contract**: exit 42 is a cross-process contract between `__main__.py` (producer) and `entrypoint.py` (consumer), mirrored in both modules because the supervisor is zero-dependency and does not import the orchestrator package (ADR-0015).

## Considered Options

### 1. Issue's proposed `trace.get_current_span()` telemetry (status quo of the proposal)
Rejected — no-op in this project's retrospective-span architecture (ADR-0016). See Framework-First Research note above.

### 2. `langfuse.tags` attribute (issue's literal proposal)
Rejected — incorrect key per Langfuse OTel docs. `langfuse.trace.tags` is the trace-level tags attribute that maps to the filterable Langfuse tags field.

### 3. Persist the circuit-breaker counter across supervisor restarts
Rejected — a supervisor restart is itself a reset event. Persisting would require filesystem state, violating the supervisor's zero-dependency, stateless design (ADR-0015). The rolling window resets on restart, which is the desired behavior.

### 4. Real-time webhook alerting on security blocks
Rejected (out of scope) — Langfuse search is the chosen post-hoc signal. Real-time alerting is deferred to a future enhancement if operational latency demands it.

### 5. Record telemetry on the inner grep walked-file silent prune
Deferred — the `grep_search` inner `is_safe_path` check (which silently skips a sensitive file found during a directory walk) is a defense-in-depth prune, not a refusal returned to the agent. Recording telemetry there could be noisy (a single grep on a directory containing `.env`, `.git/`, etc. would emit multiple events). The 4 explicit refusal-return sites are instrumented; the silent prune is left for a future hardening if a concrete exfiltration vector demonstrates its need.

## Inspiration & References

- **Langfuse — OpenTelemetry (OTEL) for LLM Observability** (official documentation) — T1
  https://langfuse.com/integrations/native/opentelemetry
  Accessed: 2026-07-22. Verified: fetched and confirmed — documents the `langfuse.trace.tags` attribute (string[]) as the trace-level tags field mapped from OTel, and the requirement to propagate trace-level attributes to every span for reliable filtering. Also documents that unmapped attributes land in the unfilterable `metadata.attributes` catch-all, confirming the correction from `langfuse.tags` to `langfuse.trace.tags`.

- **ADR-0016: In-Process Telemetry and Stateful Resume Tracing** (this repository) — T1 (internal canonical)
  ./0016-in-process-telemetry-and-stateful-resume-tracing.md
  Accessed: 2026-07-22. Verified: read in-repo; establishes the retrospective span-creation architecture (spans built in `_export_recorded_spans` from timestamp records, not live via `start_as_current_span`) that necessitated the adaptation from the issue's proposed `trace.get_current_span()` pattern.

- **ADR-0035: Runtime Path-Safety Validation in Worker Tools** (this repository) — T1 (internal canonical)
  ./0035-runtime-path-safety-validation-in-worker-tools.md
  Accessed: 2026-07-22. Verified: read in-repo; defines the runtime path-safety block sites (`read_file`, `list_directory`, `grep_search`, `patch_file`) instrumented with telemetry in this ADR, and the `is_safe_path` blocklist shared between plan and runtime layers.

- **ADR-0042: Framework-First Research Protocol** (this repository) — T1 (internal canonical)
  ./0042-framework-first-research-protocol-before-custom-implementation.md
  Accessed: 2026-07-22. Verified: read in-repo; establishes the requirement to check whether the adopted framework provides a native solution before custom implementation — the principle that surfaced the `trace.get_current_span()` no-op issue.

- **Beyond Lists: Using Python `deque` for Real-Time Sliding Windows** (Towards Data Science) — T3
  https://towardsdatascience.com/beyond-lists-using-python-deque-for-real-time-sliding-windows
  Accessed: 2026-07-22. Verified: fetched; corroborates the `deque` `append` + `popleft` sliding-window pattern as the standard O(1) amortized approach, and notes CPython thread-safety of `deque` operations. T3 — used as supporting context for the implementation pattern, not as sole basis for any decision.
