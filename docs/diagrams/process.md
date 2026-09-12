# Diagram: Usage process

The intended usage process from the operator's viewpoint: define an issue,
the orchestrator cycle processes it autonomously, the PR is verified and
judge-reviewed, then the loop merges, retries, or resumes. One diagram;
rendered natively by GitHub.

```mermaid
flowchart TD
    U1["Operator defines an issue<br/>label agent-ready<br/>Blocked by section for dependencies"] --> C["Claim<br/>dependencies closed? else label agent-blocked<br/>label agent-in-progress<br/>branch + workspace prep"]

    C --> PL["Plan<br/>issue + codebase snapshot<br/>Localization: target files + intents<br/>tree-sitter outlines, Python and TS"]

    PL --> TW["Test-Writer<br/>unit tests + stubs first<br/>Test-Driven Development"]

    TW --> EX["Execute<br/>ReAct Worker edits the workspace<br/>self-corrects on Verification Feedback"]

    EX --> V["Verify<br/>deterministic gate in the Execution Sandbox<br/>AGENT_VERIFY_COMMAND, default make verify<br/>then BinEval soft semantic gate"]

    V -->|"gate passed"| PR["Pull Request<br/>deterministic body: plan rationale,<br/>diff stat, coverage tail"]

    V -->|"fail, gate budget left<br/>3 per gate"| EX

    V -->|"gate budget exhausted"| REC["Recovery<br/>workspace cleaned<br/>label back to agent-ready"]

    PR --> M["Merge polling<br/>CI checks + PR Review Judge verdicts"]

    M -->|"verdicts FAIL or NEEDS REVIEW<br/>merge-fix budget left, default 3"| EX
    M -->|"merged"| D["Issue closed - cycle done"]
    M -->|"poll timeout or judge infra failure"| REC

    REC --> U1

    subgraph RESUME["Resumability"]
        ST["state.json persists every transition<br/>a crash or restart resumes at the<br/>recorded phase with the branch intact"]
    end

    C -.-> ST
    PL -.-> ST
    TW -.-> ST
    EX -.-> ST
    V -.-> ST
    PR -.-> ST
    M -.-> ST
```

## Grounded in

- **ADR-0009** — pure Python LangGraph orchestration; the phase machine is the
  graph in `orchestrator/graph.py`, nodes in `orchestrator/nodes.py`.
- **ADR-0010** — Test-First loop: Claim → Plan → Test-Writer → Execute →
  Verify → PR → Merge.
- **ADR-0012 / ADR-0017** — structured JSON planning with path-safety
  validation; tree-sitter Structural Outlines for Python/TypeScript.
- **ADR-0014 / ADR-0019** — PR verification and LLM judge review; hidden
  verdict block parsed during Merge Polling.
- **ADR-0036** — bounded post-PR Merge-Fix Loop (`AGENT_PR_FIX_MAX`, default 3).
- **ADR-0047** — per-gate verify attempt budgets (`verify_cmd`, `bineval`),
  each capped at 3.
- **ADR-0016** — Stateful Resume via `.agent_logs/state.json`.
- **Code** — `orchestrator/graph.py` (routers), `orchestrator/nodes.py`
  (node bodies), `orchestrator/state.py`.

## Notes

- Status values persisted along the way: `idle | claimed | planning |
  executing | verifying | pr_open | merging | done | failed`.
- The Merge-Fix Loop and Hybrid Retry both re-enter Execute; they differ in
  trigger (judge verdicts vs. local verify failures) and budget
  (`AGENT_PR_FIX_MAX` vs. the per-gate verify caps) — see
  [detailed-retry-workspace.md](detailed-retry-workspace.md).
- No-Judge Merge Mode (`AGENT_JUDGE_ENABLED=false`, ADR-0054) replaces verdict
  polling with a human-scale merge window (default 24 h, resumable pause on
  expiry) for target repositories without the Reusable Judge Workflow.
