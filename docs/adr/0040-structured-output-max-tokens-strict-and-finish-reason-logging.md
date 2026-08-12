# ADR 0040: Structured-Output max_tokens, strict Mode, and finish_reason Logging

## Status

Accepted

## Context

The `plan_node` and `_run_bineval` functions invoke LLM structured output via
`with_structured_output` without explicit `max_tokens`, without `strict=True`,
and without capturing `finish_reason` on parse failure. A complex issue can
produce a large `DevelopmentPlan` JSON that exceeds the model's default output
budget, causing silent truncation (`finish_reason=length`), a JSON parse error,
and an unnecessary recovery cycle. For `BinEvalResult`, a truncation would be
silently masked as a soft-gate PASS — a PR could merge ungraded with no
distinguishable signal that truncation occurred.

The same root cause was identified in agentic-planner-core #56; this repository's
failure handling is safer (recovery / soft-gate rather than silent skip), but
the latent truncation risk remains.

## Decision

1. **Explicit `max_tokens` on structured-output nodes.** Add a `max_tokens`
   field to the flat factory.json schema (ADR-0018), resolved by
   `resolve_model_config` and passed to the `ChatOpenAI` constructor via
   `get_chat_model_from_config`. Set `max_tokens: 8192` for `plan` and
   `max_tokens: 4096` for `bin_eval`. Non-structured-output nodes (execute,
   test_writer, judges) do not set `max_tokens` — they use the model/provider
   default, avoiding truncation of ReAct reasoning or judge evaluations.

2. **`strict=True` on `with_structured_output`.** Both `DevelopmentPlan` and
   `BinEvalResult` schemas are called with `strict=True` alongside the default
   `function_calling` method. This enforces schema adherence at the provider
   level, reducing malformed-output parse failures. Both schemas are compatible
   with strict mode: all fields are simple types or `Literal` enums, and the
   one defaulted field (`requested_files` with `default_factory=list`) is
   handled by LangChain making it required in the generated JSON schema.

3. **`include_raw=True` for `finish_reason` capture.** Both calls use
   `include_raw=True`, which changes the return type from a Pydantic model to
   `{"raw": AIMessage, "parsed": ..., "parsing_error": ...}`. On parse failure,
   `finish_reason` is extracted from `raw.response_metadata` and logged. This
   makes `BinEval` truncation (`finish_reason=length`) distinguishable from a
   genuine soft-gate PASS, and gives the recovery path diagnostic context for
   `DevelopmentPlan` failures.

Existing failure routing is preserved: `DevelopmentPlan` parse failure still
raises into the node's exception handler (→ `recovery_node`, ADR-0038);
`BinEvalResult` parse failure still returns `None` (soft-gate PASS).

## Consequences

- The `include_raw=True` return type is a dict, not a Pydantic model. A shared
  helper (`_extract_structured_output` / `_invoke_structured_with_raw`) isolates
  the extraction and logging so call sites remain clean.
- `max_tokens` follows the same env-override inheritance pattern as
  `temperature` and `options`: inherited from factory only when the env
  override model matches the factory entry; otherwise `None`.
- `strict=True` requires all schema fields to be present in the model's output.
  Fields with Pydantic defaults (e.g. `requested_files`) must still be populated
  by the model (as an empty list if appropriate). This is a non-issue in
  practice but is a constraint on future schema additions.

## Inspiration & References

- **LangChain `with_structured_output` reference**: `strict` parameter works
  with `function_calling` and `json_schema` methods; `include_raw=True` returns
  `{"raw", "parsed", "parsing_error"}` dict.  
  https://reference.langchain.com/python/langchain-openai/chat_models/base/BaseChatOpenAI/with_structured_output
- **OpenRouter LangChain tutorial**: "Both `bind_tools` and
  `with_structured_output` accept `strict=True` to force schema adherence.
  `strict` works with the `function_calling` and `json_schema` methods."  
  https://openrouter.ai/blog/tutorials/langchain-chatopenrouter-setup
- **OpenRouter API reference**: `finish_reason` is normalized to `stop`,
  `length`, `tool_calls`, `content_filter`, or `error`. `max_tokens` truncation
  produces `finish_reason=length`.  
  https://openrouter.ai/docs/api_reference/overview
- **Together AI structured-outputs docs**: documents the `max_tokens` /
  `finish_reason=length` truncation pattern for structured JSON.  
  https://docs.together.ai/docs/inference/chat/structured-outputs
- **agentic-planner-core #56**: same root cause (no `max_tokens` on structured
  output), different and more severe failure handling.
