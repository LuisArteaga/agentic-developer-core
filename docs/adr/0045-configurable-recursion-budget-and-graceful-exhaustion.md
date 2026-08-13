# ADR 0045: Configurable Worker Recursion Budget and Graceful GraphRecursionError Handling

## Status

Accepted

## Context

The worker agent's reasoning budget was a hardcoded magic constant: `config = {"recursion_limit": 30}` in `orchestrator/worker.py`. In a LangGraph `create_agent` ReAct loop each think→tool round consumes **2 supersteps** (agent node + tools node), so 30 ≈ only **15 tool iterations**. For non-trivial tasks this is too tight: the Test-Writer burned through productive tool calls, hit the wall mid-task, raised `GraphRecursionError`, and the node's generic `except Exception` marked the whole run `failed` → Recovery reset the issue. The graph "finished successfully" while the task actually failed — the crash guard (designed to catch infinite loops) was being treated as a hard task failure, discarding a productive partial trajectory and the entire issue attempt.

Research consensus (verified below):

- The default `recursion_limit` is a **crash guard, not a real budget**: "complex graphs may hit the default limit naturally" (official LangGraph docs).
- Raising the limit alone is a **known anti-pattern** — "if there's a genuine cycle, a bigger cap just delays the crash."
- The recommended posture is a **three-layer defense**: (1) state-based early exits, (2) `recursion_limit` as a crash guard, (3) `GraphRecursionError` try/except at the call site for a user-friendly fallback.

Two constraints shaped the design:

- `resolve_model_config()` is the existing per-node config resolution point; the budget must be co-located there, not a parallel config path or a magic constant in the worker.
- The Worker Trace sidecar (ADR-0016) and Run Observability metrics (ADR-0029) must still run on the partial trajectory of an exhausted run — "collect what trajectory exists."

## Decision

Implement the three-layer defense for the worker's reasoning budget.

### Layer 2 — Configurable crash guard (per-node `recursion_limit`)

Resolve `recursion_limit` per node via `resolve_model_config()` (ADR-0018), co-located with the Model Config — not a magic constant. A new `DEFAULT_RECURSION_LIMIT = 50` constant in `orchestrator/config.py` (≈25 tool iterations; LangGraph's own default of 25 is a crash guard for infinite loops). The resolver returns `"recursion_limit": int` in every Model Config dict, via a `_resolve_recursion_limit(node_name, factory_cfg)` helper with precedence mirroring the model resolution precedence:

1. Node-specific env: `{NODE}_RECURSION_LIMIT` (e.g. `EXECUTE_RECURSION_LIMIT`)
2. General env: `AGENT_RECURSION_LIMIT`
3. `factory.json` `"recursion_limit"` for the node
4. `DEFAULT_RECURSION_LIMIT`

This supports **distinct budgets per node** (execute vs test_writer) without code changes, and keeps the budget co-located with the rest of the per-node config. A malformed env value is logged and ignored, degrading to the next precedence tier rather than crashing. `get_chat_model_from_config()` ignores the field (it is consumed by the Worker, not the LLM client) — mirroring the precedent set by `max_tokens` (ADR-0040), a non-model field already co-resolved in Model Config.

### Layer 3 — Graceful exhaustion catch (call-site fallback)

`execute_worker` executes the agent via `agent.stream(..., stream_mode="values")` instead of `agent.invoke(...)`, accumulating the last yielded graph state. The stream loop is wrapped in `try/except GraphRecursionError`:

- On exhaustion, the **partial trajectory** is the message list from the last yielded state (`last_state["messages"]`). This is necessary because `GraphRecursionError` is a plain `RecursionError` subclass and **carries no state** (verified from the installed langgraph 1.2.6 source) — `agent.invoke()` would raise with no way to recover the partial trajectory, whereas streaming yields the state up to the failing superstep.
- A deterministic `[RECURSION_LIMIT_EXHAUSTED]` message (with node, attempt, recursion_limit, tool-invocation count, and a one-line last-action summary) is **returned** instead of letting the exception propagate. The caller treats this as a normal failed worker result.
- The Worker Trace sidecar and `_record_run_metrics` run on the partial trajectory (ADR-0016 / ADR-0029 honored).
- A single exhaustion counts as **one failed attempt** — the existing retry loops stay intact (Hybrid Retry for Execute, ADR-0034; the 3-attempt loop for Test-Writer). Only repeated exhaustion across all attempts surfaces as failure. Distinct from the generic `except Exception` → Recovery path.
- Zero tool invocations on exhaustion (an empty/immediate loop — possible LangGraph v1 infinite-loop regression, langchain-ai/langgraph#6731) is logged at **ERROR** so it is detectable, while still degrading gracefully rather than crashing. Productive-but-long exhaustion is logged at WARNING.

Switching from `invoke` to `stream(stream_mode="values")` does **not** change LLM call semantics: the prebuilt agent's model node invokes the LLM via `await model_.ainvoke(messages)` regardless of whether the graph is invoked or streamed (verified in `langchain/agents/factory.py`). Streaming only changes how graph state is surfaced to the caller.

### Layer 1 — Early-exit signal (System Prompt)

The `SYSTEM_PROMPT` gains a "BUDGET AWARENESS (SELF-TERMINATE EARLY)" section steering the agent to stop exploring and return the best partial result *before* hitting the hard cap when it senses it is running long, rather than being interrupted mid-tool-call. The guidance is qualitative (the agent cannot count its own supersteps) and robust to config changes — it does not hardcode the numeric limit into the prompt.

## Consequences

- A worker that runs out of reasoning budget no longer crashes the whole orchestrator run into Recovery. The issue attempt is preserved as a failed attempt within the existing retry loop.
- The partial trajectory of an exhausted run is observable: the Worker Trace sidecar and TL / token metrics are recorded from it, so directional trends (ADR-0029) reflect real exhaustion events instead of silent discards.
- The budget is configurable per node and overridable by environment, co-located with Model Config — no magic constant in the worker, and adding a new node's budget requires no resolver changes (ADR-0018 flat-schema benefit).
- `GraphRecursionError` is still **logged** (WARNING/ERROR with node and attempt) — only its propagation behavior changes. Genuine infinite-loop bugs are not masked: the zero-tool-invocation case is logged prominently at ERROR.

## Considered Options

### 1. Raise the hardcoded limit only
Rejected — the documented anti-pattern. "If there's a genuine cycle, a bigger cap just delays the crash." Also leaves the magic constant in the worker and the crash-into-Recovery behavior intact.

### 2. Catch `GraphRecursionError` but keep `agent.invoke()` (no streaming)
Rejected — `GraphRecursionError` carries no state (plain `RecursionError`, verified from source). With `invoke()`, the partial trajectory is unrecoverable, so the Worker Trace sidecar and metrics could not run on it, violating ADR-0016 / ADR-0029 and the issue's "collect what trajectory exists" requirement. A callback-handler reconstruction of the trajectory was also rejected as lossy: tool-result callbacks do not yield `ToolMessage` objects, so `count_tool_invocations` (which counts `ToolMessage` instances) would record an incorrect TL.

### 3. Parallel config path for the budget (e.g. a separate `resolve_recursion_limit` resolver invoked outside `resolve_model_config`)
Rejected — violates the issue's explicit constraint to extend `resolve_model_config()` rather than introducing a parallel config path, and forfeits the co-location benefit (the budget would live away from the rest of the per-node config).

### 4. Inject the numeric limit into the System Prompt
Rejected — the agent cannot count its own supersteps, so a precise number adds coupling to the config value without actionable precision. Qualitative self-termination guidance is robust to config changes.

### 5. Mask `GraphRecursionError` silently (no logging)
Rejected — violates the issue's constraint that the error must still be logged so genuine infinite-loop bugs (and the LangGraph v1 regression) remain detectable.

## Inspiration & References

- **LangGraph — `GRAPH_RECURSION_LIMIT`** (official documentation) — T2
  https://docs.langchain.com/oss/python/langgraph/errors/GRAPH_RECURSION_LIMIT
  Accessed: 2026-08-13. Verified: fetched and confirmed — documents that `GraphRecursionError` is raised when a `StateGraph` "reached the maximum number of steps before hitting a stop condition", that "this is often due to an infinite loop", but critically that "complex graphs may hit the default limit naturally", and that a higher `recursion_limit` can be passed into the `config` object. Confirms the default limit is a crash guard, not a real budget.

- **Installed `langgraph` 1.2.6 source — `langgraph.errors.GraphRecursionError`** — T1 (authoritative primary, source code)
  `from langgraph.errors import GraphRecursionError; inspect.getsource(...)`
  Accessed: 2026-08-13. Verified: read directly via `inspect.getsource` — `GraphRecursionError` is a bare `class GraphRecursionError(RecursionError): pass` subclass with no state attributes. This is the empirical basis for rejecting Option 2 (invoke-based recovery): the exception carries no partial state, so the trajectory must be captured via streaming.

- **Installed `langchain` 1.3.14 source — `langchain/agents/factory.py`** — T1 (authoritative primary, source code)
  Accessed: 2026-08-13. Verified: read directly — the prebuilt `create_agent` model node executes `output = await model_.ainvoke(messages)` unconditionally, regardless of whether the graph is driven via `.invoke()` or `.stream()`. Confirms that switching to `stream(stream_mode="values")` does not change LLM call semantics.

- **Localized experiment** (this repository, ephemeral) — T1 (empirical)
  A scratch script drove a `create_agent` with a non-terminating fake model at `recursion_limit=3`: `stream(stream_mode="values")` yielded growing state chunks (1→2→3→4 messages) and then raised `GraphRecursionError`, with the last chunk holding the partial trajectory (HumanMessage, AIMessage, ToolMessage, AIMessage). A terminating model via `stream` returned the final `AIMessage.content`. Verified: run and observed directly on 2026-08-13.

- **LangGraph Cycles & Recursion Limits: Control Agent Loops** (machinelearningplus) — T3
  https://machinelearningplus.com/gen-ai/langgraph-cycles-recursion-limits-agent-loops
  Accessed: 2026-08-13. Verified: fetched via `web_search` — articulates the three-layer defense adopted here ("1. State-based exits … 2. `recursion_limit` — a hard cap at call time as a crash guard … 3. `GraphRecursionError` handling — try/except at the call site for user-friendly fallbacks"), the superstep accounting (each think→tool round = 2 supersteps), and the recommendation to always set the limit by hand for live systems rather than leaning on the default.

- **ADR-0016: Worker Trace Sidecar** (this repository) — T1 (internal canonical)
  ./0016-in-process-telemetry-and-stateful-resume-tracing.md
  Accessed: 2026-08-13. Verified: read in-repo — establishes that the Worker Trace sidecar is a pure sidecar that must be written even on failure paths and never impacts execution. Drives the requirement to run the sidecar on the partial trajectory of a gracefully-exhausted run.

- **ADR-0029: Fire-and-Forget In-Process Metrics Accumulation** (this repository) — T1 (internal canonical)
  ./0029-fire-and-forget-in-process-metrics-accumulation.md
  Accessed: 2026-08-13. Verified: read in-repo — establishes Run Observability (TL, token consumption) and the graceful-degradation posture. Drives the requirement to record metrics on the partial trajectory.

- **ADR-0018: Flat Factory.json Schema** (this repository) — T1 (internal canonical)
  ./0018-flat-factory-json-schema-over-grouped-node-taxonomy.md
  Accessed: 2026-08-13. Verified: read in-repo — establishes `resolve_model_config()` as the single per-node config resolution point and the flat-schema benefit that adding a node requires no resolver changes. Justifies co-locating `recursion_limit` in the resolved Model Config rather than a parallel path.

- **ADR-0034: Hybrid Retry Hard-Rollback on Final Attempt** (this repository) — T1 (internal canonical)
  ./0034-hybrid-retry-hard-rollback-on-final-attempt.md
  Accessed: 2026-08-13. Verified: read in-repo — establishes the Execute retry loop that a graceful exhaustion must integrate with as one failed attempt rather than a full Recovery reset.

> **Search-integrity note (ADR-0041):** web search for the three-layer defense pattern succeeded (machinelearningplus, T3, verified via `web_search`); the official LangGraph docs were verified via `fetch_url`. The langgraph/langchain behavior claims are verified directly from installed source code (T1) and a localized experiment (T1, empirical). The issue body additionally cites blog/forum references (mohitnagaraj.in, the LangChain forum) that were not independently re-fetched; they are consistent with the verified sources but are not cited here as independently verified.
