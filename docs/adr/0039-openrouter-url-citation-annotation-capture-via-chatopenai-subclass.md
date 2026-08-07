# ADR 0039: OpenRouter URL Citation Annotation Capture via ChatOpenAI Subclass

## Status
Accepted (supersedes the "Parse fragility" consequence of [ADR-0026](./0026-web-search-worker-tool-via-openrouter-server-side-search.md); the server-side search approach, tool binding, and forced `tool_choice` from ADR-0026 remain unchanged)

## Context
The `web_search` Worker Tool (ADR-0026) derives its results from a fragile pattern: the model is instructed via a system prompt to append a ```json block of `[{title, url, snippet}]` to its response, and the tool parses it with `json.loads`. When the model omits, truncates, or malforms the block, `_parse_search_results` returns `[]` and the tool surfaces `"No search results found"` — conflating **"search infrastructure broken"** with **"search genuinely returned nothing."** The Worker cannot distinguish the two, so it cannot retry intelligently.

The root cause is that OpenRouter delivers web-search results **exclusively** via `url_citation` annotations on the assistant message (`message.annotations`), not via the message text. The ```json block is a secondary, model-synthesized artifact that duplicates data already present in the annotations — and langchain-openai discards the annotations before the tool ever sees them.

Source-verified against the installed `langchain-openai==1.3.3` (`base.py:200–264`): `_convert_dict_to_message` constructs the `AIMessage` from `content`, `function_call`, `tool_calls`, and `audio` — it **never reads `message.annotations`**. The `_create_chat_result` method (`base.py:1751–1831`) calls `_convert_dict_to_message` and also does not touch annotations. All annotation handling in `base.py` (lines 4275–4948) belongs to the **Responses API** path (`_convert_responses_*` functions); the Chat Completions path — which ADR-0026 deliberately uses (`use_responses_api=False`, `config.py:190`) — has none.

This is not a bug langchain will fix. LangChain's official reference states: *"`ChatOpenAI` targets official OpenAI API specifications only. Non-standard response fields added by third-party providers… are not extracted or preserved. If you are pointing `base_url` at a provider such as OpenRouter… use the corresponding provider-specific LangChain package instead."* The maintainer tracking issue [#34328](https://github.com/langchain-ai/langchain/issues/34328) confirms: *"It is not practical for `ChatOpenAI` to handle each addition that third party providers make to the Chat Completions standard."* The same class of silent data loss is documented for `reasoning_content` in issue [#35059](https://github.com/langchain-ai/langchain/issues/35059).

The data pipeline is intact end-to-end — the break is only in langchain's conversion layer:
1. OpenRouter returns `message.annotations` with `url_citation` entries (verified: [OpenRouter web-search docs](https://openrouter.ai/docs/guides/features/plugins/web-search))
2. The `openai==2.44.0` SDK parses them into typed `Annotation` objects (`chat_completion_message.py:69`: `annotations: Optional[List[Annotation]] = None`); `model_config = {"extra": "allow"}` preserves the OpenRouter-added `content` (snippet) field (empirically verified)
3. `model_dump()` serializes annotations back to plain dicts — they survive
4. `_create_chat_result` receives the dict and calls `_convert_dict_to_message` — **annotations are discarded here** ← the break

The `langchain-openrouter` package (`ChatOpenRouter`) is not installed in this project and, per source-inspection of the planner-core equivalent, also does not capture annotations. Installing it would add a beta dependency (violating ADR-0017's minimal-dependency posture) for a package that does not solve the problem.

The verified fix from agentic-planner-core (PR #55) is a `ChatOpenAI` subclass that overrides `_create_chat_result` to capture annotations before langchain discards them.

## Decision

### 1. `OpenRouterAnnotationChatOpenAI` subclass — `orchestrator/openrouter_chat.py`

A new module containing a `ChatOpenAI` subclass with an **additive** `_create_chat_result` override:

- Call `super()._create_chat_result(response, generation_info)` first — unchanged behavior, produces the `ChatResult` with `AIMessage`s already constructed.
- Re-derive `response_dict` (dict passthrough or `model_dump()`) to access the raw `message["annotations"]` field that `_convert_dict_to_message` skipped.
- For each choice, if `message.get("annotations")` is non-empty, inject it into `generations[i].message.additional_kwargs["annotations"]`.
- **Store all annotations raw** — including non-`url_citation` types (e.g., `file` annotations documented in [pydantic-ai #3657](https://github.com/pydantic/pydantic-ai/issues/3657)). Filtering to `url_citation` is the consumer's job, not the subclass's.
- **Never raise** — if annotation capture fails for any reason (missing key, unexpected shape, IndexError), log a warning and return the super result unmodified. The failure mode degrades to today's behavior (annotations lost), never to a crash.

This subclass is returned by `get_chat_model_from_config` (`config.py:164`) for **all nodes** — Plan, Worker, Test-Writer, PR Judges, BinEval, and web_search. The override is a no-op for responses that carry no annotations (the common case for non-search nodes); it only activates when OpenRouter attaches annotations. This ensures any future node that binds `openrouter:web_search` gets citation capture for free, without needing a separate construction path.

### 2. `web_search` rewrite — annotation-derived results

The `web_search` tool is rewritten to derive results from `url_citation` annotations instead of the JSON-block pattern:

- **Removed**: `_SYSTEM_INSTRUCTION` (the JSON-block append instruction), `_extract_json_block`, `_coerce_text`, `_parse_search_results`, `re` import, `SystemMessage` import.
- The tool reads `response.additional_kwargs.get("annotations", [])`, filters to `type == "url_citation"`, and extracts `{title, url, snippet}`:
  - `url` ← `url_citation["url"]`
  - `title` ← `url_citation.get("title", "No Title")` (title can be missing per [vercel/ai #10199](https://github.com/vercel/ai/issues/10199))
  - `snippet` ← `url_citation.get("content", "")` (OpenRouter adds `content` "if available"; preserved via the SDK's `extra="allow"` config), truncated to `MAX_SNIPPET_CHARS` (300)
- Returns `json.dumps(results)` — same `[{title, url, snippet}]` interface the Worker expects.
- No `SystemMessage` is sent; the forced `tool_choice={"type": "openrouter:web_search"}` guarantees the model executes the search. The `HumanMessage` with the query (and strict-mode domain append) is preserved.

### 3. Honest error signal

When no `url_citation` annotations are present in the response:
```
web_search failed: no url_citation annotations in response for query: '{query}'
```
This replaces the previous `"No search results found for query: {query}"`, which conflated infrastructure failure with genuine no-results. The Worker can now distinguish:
- Exception during invoke → `"Error executing web_search for query '{query}': {e}"` (infrastructure failure)
- No url_citation annotations → `"web_search failed: no url_citation annotations..."` (search produced no citable results — may be broken or genuinely empty)
- Annotations present → JSON results (success)

### 4. Dead code removal + test migration

The removed functions (`_extract_json_block`, `_coerce_text`, `_parse_search_results`) are directly unit-tested in `test_research_tools.py` (lines 62–144). Their removal requires:
- Deleting `TestExtractJsonBlock`, `TestCoerceText`, `TestParseSearchResults` classes and their imports.
- Rewriting `TestWebSearchTool` to mock `additional_kwargs["annotations"]` instead of content-based JSON blocks.
- Adding `TestOpenRouterAnnotationChat` testing the subclass's `_create_chat_result` directly (annotation capture, no-op on absent annotations, never-raise on malformed input).
- Adding tests for the `_extract_url_citations` helper (filtering, field extraction, snippet truncation, missing-title fallback).

## Considered Options

### 1. Switch to `langchain-openrouter`'s `ChatOpenRouter`
Rejected. Not installed in this project (adding it violates ADR-0017's minimal-dependency posture for a beta package). Source-inspection of the planner-core equivalent confirms it also does not capture annotations — the same `_convert_dict_to_message` gap applies. Installing it would add a dependency without solving the problem.

### 2. Switch to the Responses API (`use_responses_api=True`)
Rejected. ADR-0026 deliberately uses Chat Completions (`use_responses_api=False`, `config.py:190`) for OpenRouter compatibility. The Responses API annotation handling in `base.py` (lines 4275–4948) is OpenAI-specific and does not map cleanly to OpenRouter's Chat Completions annotation schema. Switching APIs would be a fundamental change to the model construction path affecting all nodes, not just web_search.

### 3. Keep the JSON-block pattern, add retry logic
Rejected. Adding retries around a fundamentally fragile pattern (model-synthesized JSON) does not address the root cause: the authoritative data (annotations) is already in the response and being discarded. Retries add latency and token cost while preserving the failure-to-distinguish "broken" from "empty" problem. The annotation-based approach eliminates the fragility by construction.

### 4. Confine the subclass to the `web_search` node only (dedicated constructor)
Rejected. `get_chat_model_from_config` is the shared construction path for all nodes (Plan, Worker, Test-Writer, Judges, BinEval, web_search). A dedicated constructor for web_search-only would create two divergent paths, and any future node that binds `openrouter:web_search` would silently re-acquire the original bug. The additive override is a no-op for non-annotation responses, so global application carries no risk — it only adds a dict read that short-circuits when `annotations` is absent.

## Consequences

- **Eliminates parse fragility**: the ADR-0026 consequence *"Parse fragility: the tool depends on the model emitting a parseable ```json block"* is **superseded**. Results are derived from structured annotations (parsed by the openai SDK's typed `Annotation` model), not from model-synthesized free-form text.
- **Honest error signal**: the Worker can now distinguish "search broken" (exception or no annotations) from "search returned results" (annotations present), enabling intelligent retry decisions.
- **Additive and non-disruptive**: the subclass override is a no-op for any response without annotations — Plan, Worker, Test-Writer, Judge, and BinEval nodes are unaffected. The override only activates when OpenRouter attaches annotations to a response.
- **Private-API dependency**: the override targets `_create_chat_result`, a private method. If langchain-openai renames or restructures it in a future version, the override silently degrades to today's behavior (super call fails → `AttributeError` → caught by the existing `except Exception` in `web_search`; other nodes fail loudly and immediately, which is the correct signal for a breaking upstream change). This is the same trade-off accepted by `ChatDeepSeek` (LangChain's own integration), which also overrides `_create_chat_result`.
- **Streaming not covered**: the override targets the non-streaming `_create_chat_result` path. `web_search` uses `invoke()` (non-streaming), so this is correct. The streaming equivalent (`_convert_delta_to_message_chunk`, `base.py:430`) also discards annotations, but no current consumer uses streaming with web_search. This is a known limitation, not a regression (the current code also doesn't handle streaming annotations).
- **SDK validation edge case**: the openai SDK's `AnnotationURLCitation` declares `title: str` as required. If OpenRouter omits `title` (documented in [vercel/ai #10199](https://github.com/vercel/ai/issues/10199)), Pydantic validation fails inside the SDK **before** `_create_chat_result` runs — the subclass cannot rescue it. This is a pre-existing condition (the current code also fails at `invoke()`); the new error path surfaces it as `"Error executing web_search..."` rather than `"No search results found"`, which is strictly more honest.

## Inspiration & References

- **langchain-ai issue [#34328](https://github.com/langchain-ai/langchain/issues/34328)** — Maintainer tracking issue for OpenRouter/LiteLLM/OpenAI compatibility. Confirms: *"It is not practical for `ChatOpenAI` to handle each addition that third party providers make to the Chat Completions standard."* Establishes that provider-specific field capture belongs in a subclass, not in `ChatOpenAI` itself.
- **langchain-ai issue [#35059](https://github.com/langchain-ai/langchain/issues/35059)** — *"ChatOpenAI silently drops `reasoning_content` from OpenAI-compatible providers."* Same root cause (`_convert_dict_to_message` ignoring a provider field), same official answer (use the provider-specific package). Proves this is a documented, recurring pattern, not a one-off.
- **`ChatDeepSeek` (`langchain-deepseek`)** — LangChain's own official integration for an OpenAI-compatible provider. Overrides `_create_chat_result()` to capture DeepSeek-specific `reasoning_content` into `AIMessage.additional_kwargs` — the exact pattern adopted here. Source: [Leeroopedia `ChatDeepSeek` analysis](https://leeroopedia.com/index.php/Implementation:Langchain_ai_Langchain_BaseChatModel_Subclass), [langchain-ai #37390](https://github.com/langchain-ai/langchain/issues/37390). Confirms `_create_chat_result` is LangChain's sanctioned extension point for provider-specific response fields.
- **LangChain official reference — [BaseChatOpenAI](https://reference.langchain.com/python/langchain-openai/chat_models/base)**: *"`ChatOpenAI` targets official OpenAI API specifications only. Non-standard response fields added by third-party providers… are not extracted or preserved. Use the corresponding provider-specific LangChain package instead."*
- **OpenRouter Web Search docs — [plugins/web-search](https://openrouter.ai/docs/guides/features/plugins/web-search)**: the `url_citation` annotation schema (`url`, `title`, `content` [snippet, "added by OpenRouter if available"], `start_index`, `end_index`). Confirms annotations are the sole delivery channel for web-search results.
- **pydantic-ai issue [#3657](https://github.com/pydantic/pydantic-ai/issues/3657)** — OpenRouter emits `type: "file"` annotations (with `url_citation: null`) alongside `url_citation` annotations. Justifies storing all annotations raw and filtering to `url_citation` at consumption.
- **vercel/ai issue [#10199](https://github.com/vercel/ai/issues/10199)** — OpenRouter sometimes omits the `title` field from `url_citation` annotations. Justifies the `title` fallback to `"No Title"`.
- **openai SDK v2.44.0** (`chat_completion_message.py`): `Annotation` and `AnnotationURLCitation` typed models; `model_config = {"extra": "allow"}` preserves OpenRouter's undeclared `content` (snippet) field. Empirically verified: `model_dump()` includes `content` when present.
- **agentic-planner-core PR #55 / ADR-0012** — the verified reference implementation this change ports. The subclass approach was proven in the planner-core codebase before applying it here.
- Extends [ADR-0026](./0026-web-search-worker-tool-via-openrouter-server-side-search.md) (Web Search Worker Tool) — supersedes its "Parse fragility" consequence while preserving its server-side search approach, tool binding, and forced `tool_choice`.
