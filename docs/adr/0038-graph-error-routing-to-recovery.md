# ADR 0038: Graph Error Routing to Recovery on Node Exceptions

## Status
Accepted (restores the [CONTEXT.md](../../CONTEXT.md) Failure Recovery contract; addresses a finding from the 2026 forensic review)

## Context
The graph is compiled with **conditional edges only**, each routed on `state["status"]` (`graph.py:122-186`); there is **no `add_error_edge` / `add_error_edges`**. Every node's failure path currently does:

```python
except Exception as e:
    state["status"] = "failed"
    state["phase"] = "<node>"
    state_module.save(state)
    _safe_telemetry(...)
    raise e          # <-- propagates out of graph.invoke
```

(appears in `plan_node`, `execute_node`, `verify_node`, `pr_node`, `merge_node`, `test_writer_node`). Because the exception propagates out of `graph.invoke` (`__main__.py:55`), the conditional routers never run, so **`recovery_node` is unreachable on any exception path**. `recovery_node` is the only thing that cleans the workspace and restores the issue's GitHub label to `agent-ready` ([CONTEXT.md — Failure Recovery](../../CONTEXT.md)). The result: on the most common failure mode, the issue is left stuck in `agent-in-progress`, the workspace stays dirty, and the label is never reset — a direct violation of the documented contract.

Only the *non-raising* exhaustion paths reach recovery today: `verify_node` exhaustion (`:1140`), BinEval exhaustion (`:1243`), `merge_node` budget exhaustion (`:1884`), and `claim_node` hygiene failure (`:269`). So two failure-handling patterns coexist inconsistently within the same module: raising paths bypass recovery; returning paths reach it. The recovery-on-exception path is also **untested** (the test suite only exercises the returning paths), which is why this defect survived.

## Decision
Make exception paths reach `recovery_node` by **stopping the re-raise** after recording the failure, so the existing `status="failed"` conditional edges route to recovery — no new graph topology, no new state field.

1. **Return instead of raise.** Each node's `except Exception` block records `state["status"]="failed"`, `state["phase"]`, an `state["error"]` message (`f"<node>: {e}"`), saves, emits telemetry, and **`return state`**. The existing routers (`route_after_plan/test_writer/execute/verify/pr/merge`) already map `status=="failed"` → `"recovery"`; `route_after_claim` already maps it too (`graph.py:39-40`). No `add_error_edge` is required.
2. **`claim_node` exception.** `claim_node` sits on `START → claim`; on a claim-phase exception the graph should still reach `recovery_node` (currently `route_after_claim`'s `failed → recovery` branch is dead code for the raising path). With the re-raise removed, that branch becomes live.
3. **Error detail in state.** Add a transient `state["error"]` string so `recovery_node` can surface the root cause in its failure comment/log without parsing a stack trace, and so a resumed run sees the prior failure reason. It is overwritten on each new claim; not a durable schema field beyond the existing `AgentState` flexibility.
4. **Logging retained.** `logger.exception(...)` inside the `except` preserves the full stack trace in logs (today the stack is only surfaced via `__main__.py:76`); observability is not reduced by returning instead of raising.

## Considered Options
- **`add_error_edge` / a graph-level error boundary** — rejected: LangGraph's error edges add topology complexity and a second failure-handling mechanism alongside the existing status-based routers; the return-state approach reuses the already-wired, already-tested routing and touches only the node bodies.
- **Wrap `graph.invoke` in `__main__.py` and call `recovery_node` manually on exception** — rejected: duplicates the recovery contract outside the graph, bypassing state-based routing and telemetry; a second recovery path diverges from the in-graph one.
- **Keep raising, add a global `try/except` per node that converts to `return state`** — equivalent to the chosen approach but more boilerplate; the chosen approach is the minimal change at the existing `except` sites.
- **Leave as-is, accept dirty workspaces** — rejected: violates the Failure Recovery contract and leaves issues stuck `agent-in-progress`, requiring manual human cleanup — unacceptable for an unattended loop.

## Consequences
- **Pro**: the Failure Recovery contract is honored on **every** failure path (exception and exhaustion), not only exhaustion. No more stuck `agent-in-progress` issues or dirty workspaces after a node crash.
- **Pro**: unifies the two inconsistent failure patterns in `nodes.py` into one (record-and-return).
- **Pro**: `route_after_claim`'s currently-dead `failed → recovery` branch becomes live, so even a claim-phase crash recovers.
- **Pro**: the recovery path becomes testable — add a regression test that injects a node exception and asserts `recovery_node` runs and the label is restored (also closes a CI coverage gap).
- **Con**: an exception no longer crashes the process (exit code 1) immediately; instead the loop runs `recovery_node` then exits via `route_after_merge`/`END` with `status="failed"`, which `__main__.py:60` already maps to exit code 1. So the supervisor/CI still sees a non-zero exit — the observable failure signal is preserved.
- **Con**: a bug inside `recovery_node` itself could mask the original exception. Mitigation: `recovery_node` must catch its own errors and still persist `status="failed"` (it already saves state); the original `state["error"]` survives even if recovery partially fails.
- **Risk**: returning a state with `status="failed"` from a node relies on the router mapping being complete. Verified: all six post-claim routers plus `route_after_claim` map `failed → recovery`. `claim` is `START → claim` with a conditional edge only; the `failed → recovery` mapping exists, so the path is covered.

## Inspiration & References
- **LangGraph v1 migration / routing** — conditional edges route on state; `add_error_edge` is an optional mechanism, not required when state-based routing already encodes the target. https://docs.langchain.com/oss/python/migrate/langgraph-v1
- **Circuit-breaker + graceful-degradation posture** — already adopted by ADR-0034/0036: fail to a clean, bounded exit rather than propagating unstructured exceptions. This ADR extends that posture to the exception path itself.
- Restores [CONTEXT.md — Failure Recovery](../../CONTEXT.md) and [ADR-0016](./0016-in-process-telemetry-and-stateful-resume-tracing.md) (stateful resume correctness across phases, including the failed phase).
