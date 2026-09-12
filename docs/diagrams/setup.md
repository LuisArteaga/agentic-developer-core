# Diagram: Setup flow

How to get from an empty machine to a running orchestrator cycle, and what a
started run produces. One diagram; rendered natively by GitHub.

```mermaid
flowchart TD
    subgraph PREREQ["1. Prerequisites"]
        P1["Docker with gVisor runsc runtime<br/>runc is a dev-only override<br/>Python 3.12"]
        P2["Target repository<br/>issues labeled agent-ready"]
    end

    subgraph CONFIG["2. Configuration"]
        C1[".env - copy of .env.example<br/>OPENROUTER_API_KEY and GH_PAT<br/>GITHUB_REPOSITORY - GITHUB_WORKSPACE<br/>AGENT_LOG_PATH"]
        C2["config/factory.json<br/>flat node-to-model map<br/>per-node model, routing, budgets"]
    end

    P1 --> START["docker run agentic-developer-core"]
    P2 --> START
    C1 --> START
    C2 --> START

    START --> S1["Process Supervisor<br/>scripts/entrypoint.py<br/>zero dependencies"]

    S1 --> S2{"Environment validation<br/>OPENROUTER_API_KEY and GH_PAT present?"}
    S2 -->|"no"| SX["Exit 1 - nothing started"]
    S2 -->|"yes"| S3["Reachability check<br/>api.github.com + openrouter.ai<br/>3 retries, SKIP_REACHABILITY to skip"]

    S3 -->|"unreachable"| SX
    S3 -->|"reachable"| S4["Sandbox runtime preflight<br/>python -m orchestrator.preflight<br/>daemon, kernel, runsc registered<br/>fail closed"]

    S4 -->|"preflight failed"| SX
    S4 -->|"preflight passed"| S5["Spawn python -m orchestrator<br/>one cycle per spawn"]

    S5 --> R1["Load .env, then state.json<br/>resume an in-flight cycle if present"]
    R1 --> R2["Init telemetry<br/>fresh run resets, resume appends"]
    R2 --> R3["graph.invoke<br/>claim, plan, test-writer, execute,<br/>verify, PR, merge"]

    R3 -->|"cycle done"| A1["Run artifacts<br/>.agent_logs/state.json for resume<br/>telemetry JSONL + OTLP export<br/>worker trace sidecars<br/>metrics.jsonl for completed cycles"]
    R3 -->|"idle, no eligible issue"| A2["Status idle - supervisor sleeps<br/>AGENT_POLL_INTERVAL, default 10 s<br/>then respawns"]

    A1 --> LOOP["Supervisor reruns immediately<br/>or polls until the next<br/>agent-ready issue appears"]
    A2 --> LOOP
    LOOP --> S5
```

## Grounded in

- **ADR-0015** — Python-based Process Supervisor: environment validation,
  reachability checks, resilient spawn loop (`scripts/entrypoint.py`).
- **ADR-0018** — flat `config/factory.json` schema, resolved per node by
  `resolve_model_config()` (`orchestrator/config.py`).
- **ADR-0056** (FR-1..FR-4) — Execution Sandbox on Docker + gVisor; the
  fail-closed runtime preflight (`orchestrator/preflight.py`, spawned as a
  subprocess so the supervisor stays zero-dependency).
- **Code** — `scripts/entrypoint.py` (validate → reachability → preflight →
  spawn → backoff/poll), `orchestrator/__main__.py` (`.env` load, state load,
  telemetry init, `graph.invoke`), `orchestrator/state.py` (`.agent_logs/`
  layout).

## Notes

- The sandbox runtime preflight refuses startup when gVisor is not registered
  with the Docker daemon — a missing runtime is a configuration error, never a
  silent degradation to shared-kernel containers.
- A run "produces" both in-repo artifacts (the PR on the target repository)
  and local sidecars under `.agent_logs/`; `metrics.jsonl` only receives a
  record for fully completed cycles.
- Configurable values shown: `AGENT_POLL_INTERVAL` (default 10 s),
  `AGENT_LOG_PATH` (default `.agent_logs`), `SKIP_REACHABILITY`,
  `AGENT_SANDBOX_RUNTIME` (default `runsc`).
