# ADR 0029: Fire-and-Forget In-Process Metrics Accumulation for Run Observability

* **Status**: Accepted
* **Date**: 2026-07-21
* **Deciders**: Luis Arteaga & The Architect

We added permanent Run Observability instrumentation (Trajectory Length, Plan Alignment, token consumption) that accumulates over real runs and appends one record per completed issue cycle to `.agent_logs/metrics.jsonl`. Two non-obvious decisions shape it.

## Decision 1 — In-memory accumulator, no crash persistence

Metrics are accumulated in a module-level, in-memory `MetricsCollector` singleton that lives for one process (one issue cycle), reset before each `graph.invoke` and written once in `__main__` only when the cycle ends with `status == "done"`. **No metric fields are added to `AgentState`**, so `state.json` stays strictly for Stateful Resume — it is never used for metric accumulation.

This is a deliberate contrast with ADR-0016, where telemetry spans are persisted to disk and retrospectively exported so that crashes never lose trace data. Metrics take the opposite posture: **crashed runs produce no record, by design.** If the process dies, the accumulator is lost and nothing is appended. Reasoning: metrics are *directional trends* over completed runs, not safety-critical state. A partial record from a crashed run would pollute the trend with work that never shipped; discarding it is more honest than persisting it. The whole feature is fire-and-forget — every entry point catches its own errors and logs at debug level, mirroring `_safe_telemetry`, so observability can never break execution.

## Decision 2 — Custom token callback over the canonical `UsageMetadataCallbackHandler`

For the Plan-Node, `.with_structured_output()` returns a Pydantic model, not an `AIMessage`, so `usage_metadata` is not on the return value. The issue proposed a `BaseCallbackHandler` reading `on_llm_end`. We implemented a small custom `TokenUsageCallbackHandler` rather than reusing the canonical `langchain_core.callbacks.UsageMetadataCallbackHandler`.

Reason: the canonical handler keys its records by `message.response_metadata["model_name"]` and **silently drops any entry where that field is absent**. Our stack runs OpenRouter through `ChatOpenAI` with `use_responses_api=False` (the chat-completions path). In that path the `AIMessage` is built by `_convert_dict_to_message`, which does **not** populate `response_metadata["model_name"]` — that value only lives in `ChatResult.llm_output`. So the canonical handler would silently record **zero** planning tokens for our integration. Our custom handler reads `AIMessage.usage_metadata` directly (which *is* populated on every chat-completions `AIMessage`) and falls back to `llm_output["token_usage"]`, with strict `isinstance` checks so a malformed/missing `llm_output` can never leak garbage into the sum. The ReAct path (Execute + Test-Writer) keeps message-list summation of `usage_metadata` — reliable there since the message list is in hand and `usage_metadata` is populated.

The same handler instance is attached to both the initial Plan-Node invoke and the Plan-Detail-Request re-invoke, so planning tokens accumulate across both calls.

## Consequences
* **Pros**: Crashed runs never pollute the trend; `state.json` stays clean; a real provider/integration gap in the canonical handler is sidestepped; the whole layer degrades silently.
* **Cons**: A resumed cycle (crash + restart) undercounts — the pre-crash attempt's metrics are lost. This is acceptable: the resumed attempt's metrics still represent a completed run, and undercounting a recovered cycle is preferable to recording a crashed one.
* Reversibility: switching the accumulator's backing to a crash-resilient sidecar later touches every node's call site (plan/execute/test-writer/pr), so recording this posture now prevents a future contributor from "fixing" it toward partial-record persistence.

## Inspiration & References
* **LangChain `UsageMetadataCallbackHandler`** — [`langchain_core/callbacks/usage.py`](https://github.com/langchain-ai/langchain/blob/main/libs/core/langchain_core/callbacks/usage.py): keys `usage_metadata` by `response_metadata["model_name"]`; silently skips when absent (the root of the silent-zero trap for our stack).
* **`AIMessage.usage_metadata`** — [LangChain Reference](https://reference.langchain.com/python/langchain-core/messages/ai/UsageMetadata): standardized per-message token counts populated by chat-model integrations.
* **ChatOpenRouter token usage** — [LangChain OpenRouter integration docs](https://docs.langchain.com/oss/python/integrations/chat/openrouter): shows `usage_metadata={'input_tokens':..., 'output_tokens':..., 'total_tokens':...}` on the returned `AIMessage`.
* **ADR-0016** (this repo): the crash-resilient, disk-based telemetry pattern that Run Observability deliberately deviates from — and the graceful-degradation posture it shares.
