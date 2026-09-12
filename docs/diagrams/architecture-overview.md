# Diagram: Architecture overview

Coarse component view of the Orchestrator: the supervisor, the LangGraph
loop, the Worker agent, persistence, observability, isolation, and the PR
judge pipeline. One diagram; rendered natively by GitHub.

```mermaid
flowchart TD
    subgraph HOST["Container host"]
        SUP["Process Supervisor<br/>scripts/entrypoint.py<br/>spawn, backoff, idle poll"]
        PRE["Sandbox preflight<br/>orchestrator/preflight.py"]
    end

    SUP --> PRE
    SUP -->|"python -m orchestrator"| CLI["Orchestrator CLI<br/>orchestrator/__main__.py<br/>load .env + state, run graph"]

    subgraph LOOP["LangGraph orchestrator loop - orchestrator/graph.py"]
        G["claim, plan, test_writer, execute,<br/>verify, pr, merge, recovery<br/>status-based conditional edges"]
    end

    CLI --> G

    subgraph CFG["Model routing"]
        RES["resolve_model_config<br/>orchestrator/config.py"]
        FAC["config/factory.json<br/>flat node-to-model map"]
    end

    FAC --> RES
    RES -->|"per-node LLM client"| G

    subgraph WORKER["Worker ReAct agent"]
        AG["create_react_agent<br/>orchestrator/worker.py"]
        TOOLS["Worker Tools<br/>read_file, patch_file, grep_search,<br/>list_directory, run_command<br/>orchestrator/tools.py"]
        RESEARCH["Research tools<br/>web_search, fetch_url<br/>orchestrator/research_tools.py"]
        MW["LoopDetectionMiddleware<br/>tool-call loop guard"]
    end

    G -->|"execute + test_writer phases"| AG
    AG --> TOOLS
    AG --> RESEARCH
    MW --> AG

    subgraph PERSIST["State and logs"]
        STATE["state.py<br/>.agent_logs/state.json<br/>atomic save per transition"]
        TRACE["Worker + Test-Writer traces<br/>.agent_logs JSONL sidecars<br/>post-hoc only"]
    end

    G --> STATE
    AG --> TRACE

    subgraph OBS["Observability"]
        TEL["Telemetry sidecar<br/>scripts/telemetry.py<br/>disk state, retrospective export"]
        MET["Run metrics<br/>orchestrator/metrics.py<br/>.agent_logs/metrics.jsonl"]
    end

    G --> TEL
    G --> MET
    TEL -->|"OTLP - Langfuse or generic endpoint"| EXT1["OTLP collector<br/>optional"]

    subgraph ISO["Execution Sandbox"]
        SBX["SandboxRunner<br/>orchestrator/sandbox.py<br/>Docker + gVisor runsc<br/>credential-free, ephemeral"]
    end

    G -->|"verify command"| SBX

    GITHUB["Target repository on GitHub<br/>issues, branches, PRs, reviews"]

    G -->|"REST API via GH PAT"| GITHUB

    subgraph CI["Target or origin repository CI"]
        TK["quality-gates-toolkit composite<br/>deterministic gates + cost gate"]
        REV["PR Review Judges<br/>scripts/review.py<br/>4 judges, hidden verdict block"]
    end

    GITHUB -->|"PR push event"| TK
    TK --> REV
    REV -->|"combined review with<br/>hidden verdict block"| GITHUB
    G -->|"merge polling parses verdicts"| GITHUB
```

## Grounded in

- **ADR-0009 / ADR-0010 / ADR-0011** — pure Python LangGraph orchestrator,
  test-first phase order, prebuilt ReAct agent for the Worker.
- **ADR-0015** — Process Supervisor as container entrypoint.
- **ADR-0016** — in-process telemetry, disk-based state, retrospective export.
- **ADR-0018** — flat `config/factory.json` model routing.
- **ADR-0029** — fire-and-forget run metrics accumulation.
- **ADR-0037 / ADR-0043** — Worker tool trust boundary; subprocess env
  allowlist.
- **ADR-0056** — split-plane Execution Sandbox (Docker + gVisor runsc).
- **ADR-0050 / ADR-0059** — Reusable Judge Workflow delivery; quality-gates
  toolkit as canonical gate provider (v1.7.0).
- **Code** — `scripts/entrypoint.py`, `orchestrator/__main__.py`,
  `orchestrator/graph.py`, `orchestrator/nodes.py`, `orchestrator/worker.py`,
  `orchestrator/tools.py`, `orchestrator/research_tools.py`,
  `orchestrator/state.py`, `orchestrator/config.py`, `orchestrator/metrics.py`,
  `orchestrator/sandbox.py`, `scripts/telemetry.py`, `scripts/review.py`,
  `.github/workflows/ci.yml`.

## Notes

- The Worker holds no orchestrator credentials: the sandbox is credential-free
  and git commits/pushes remain control-plane operations of the PR node.
- The Merge node never runs the judges itself — it only polls the verdicts the
  CI-side `scripts/review.py` posted under the trusted judge identity.
- `.agent_logs/state.json` is strictly for Stateful Resume; run metrics are
  deliberately not persisted across crashes (ADR-0029).
