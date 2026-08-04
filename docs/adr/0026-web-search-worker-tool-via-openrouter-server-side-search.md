# ADR 0026: Web Search Worker Tool via OpenRouter Server-Side Search

## Status
Accepted

## Context
The ReAct Worker (ADR-0011) can edit code and run verification but cannot resolve knowledge gaps that live outside the codebase — an unfamiliar library API, a breaking change in a new release, or an opaque error message. Without a research capability the Worker either guesses or stalls. We need an on-demand **Web Search** Worker Tool that lets the agent research inline during the Execute phase, following the ReAct pattern (the agent decides *when* to search based on what it observes), not a static pipeline stage.

The design space splits on *where the search runs*:

1. **Direct HTTP search API** (Tavily, Serper, Brave) — the tool calls a search vendor directly from the orchestrator process, then returns results.
2. **OpenRouter server-side `web_search` tool** — the tool binds OpenRouter's `"type": "openrouter:web_search"` to a cheap flash model and forces `tool_choice`; the *model* executes the search server-side at OpenRouter and returns synthesized results.

This is a near-zero-SSRF environment (the orchestrator runs inside a container acting on an arbitrary Target Repository; outbound HTTP to attacker-influenced URLs is a real risk). The Worker already has a separate URL Fetch tool (#39) that *does* make direct outbound HTTP and therefore needs full SSRF protection — we want Web Search to avoid that surface entirely.

## Decision
We implement the **Web Search** Worker Tool (`orchestrator/research_tools.py`) as a structured-output wrapper around OpenRouter's server-side `web_search` tool, porting the proven pattern from agentic-planner-core's `planner/nodes/web_search.py` and `planner/tools/research.py` (ADR-0009 Radical Simplicity — do not invent a new search implementation).

Concretely:
- The `web_search` `@tool` is added to `get_worker_tools()` (ADR-0011) alongside the existing five tools.
- The tool model is resolved via `resolve_model_config("web_search")` (ADR-0018 Per-Node Model Routing) — a cheap, tool-calling-capable flash model configured in `config/factory.json`.
- The tool binds `{"type": "openrouter:web_search", "parameters": {...}}` and forces `tool_choice={"type": "openrouter:web_search"}` to guarantee the model actually executes the search rather than answering from memory.
- The model is instructed to append a ```json block of results; the tool parses it into `[{title, url, snippet}]`, truncating snippets to 300 chars.
- Domain allowlisting and search parameters live in `config/sources.toml`, parsed and validated by a ported Pydantic `SourcesConfig` (`orchestrator/sources_config.py`), loaded once at startup and cached. This config file is shared with the URL Fetch tool (#39).
- A missing/malformed `sources.toml` degrades to non-strict defaults (engine: `auto`, no domain restrictions) with a warning; a *present* file with `strict = true` and no domains raises `ValueError` at load time (ported from planner-core's validator).
- The Worker system prompt is updated to inform the agent that `web_search` exists and should be used only on genuine knowledge gaps, not for things readable from the codebase.

The "tool that calls a model" shape is deliberate: the search executes server-side at OpenRouter, so the orchestrator makes **no direct outbound HTTP to retrieved URLs** — that SSRF surface is confined to the separate URL Fetch tool (#39), which is built with full SSRF protection. Search loops are bounded by the existing ReAct `recursion_limit: 30`.

### Deviation from planner-core: TOML over YAML

The config is parsed with stdlib `tomllib` (Python 3.11+; this project requires `>=3.12`) rather than the YAML parser planner-core uses. The Pydantic schema is unchanged from the YAML port; only the parser differs. Rationale: ADR-0017 documents a deliberate minimal-dependency philosophy ("urllib over requests, 3 deps in `pyproject.toml`"); promoting an undeclared transitive `pyyaml` to an explicit 5th direct dependency for a config-file reader violates that posture when stdlib can do the job. The only cost is that the config is no longer copy-pasteable between this repo and planner-core — a one-time conversion friction, not an ongoing burden. TOML represents this config shape (a `strict` bool, two string lists, and a `[search]` table) cleanly.

## Considered Options

### 1. Direct HTTP search API (Tavily / Serper / Brave)
Rejected. Requires a third search-vendor API key (new secret to manage and rotate), introduces direct outbound HTTP from the orchestrator (SSRF surface to guard), and adds a vendor dependency. It also bypasses the model's ability to synthesize multiple searches — a raw result list is less useful to the ReAct agent than the model-synthesized, snippet-capped JSON block the server-side tool returns. We would be reinventing the synthesis + truncation logic the model already does for us.

### 2. LangChain `TavilySearchResults` / community search tool
Rejected. Same key-management and outbound-HTTP concerns as option 1, plus a LangChain-community dependency that adds another moving part. The server-side approach keeps the search path entirely inside the OpenRouter integration we already depend on (ADR-0009, ADR-0018).

### 3. Expose the search as an orchestrator graph node instead of a Worker Tool
Rejected. The issue and the ReAct pattern (ADR-0011) require the *agent* to decide when to search based on what it observes — not a static pipeline stage. A graph node would force a search on every cycle, wasting tokens when the codebase already contains the answer, and breaks the on-demand contract that keeps trajectories short.

## Consequences

- **Near-zero SSRF for search**: Web Search makes no direct outbound HTTP to retrieved URLs; the orchestrator's only new outbound call is the OpenRouter chat-completions call it was already making. SSRF protection is confined to the URL Fetch tool (#39).
- **Model-agnostic**: any tool-calling-capable model registered for the `web_search` node in `config/factory.json` works; switching search providers is an `engine` field in `sources.toml`, not a code change.
- **Latency / cost**: each `web_search` call is a nested LLM invocation (a second model runs the search). This is acceptable because search is on-demand and bounded by `recursion_limit: 30`; the `web_search` node uses a cheap flash model.
- **Parse fragility**: the tool depends on the model emitting a parseable ```json block. Failures degrade to a "No search results found" message rather than crashing the Worker (graceful degradation), and the forced `tool_choice` makes the model actually run the search.
- **Shared config**: `config/sources.toml` and `SourcesConfig` are shared with the URL Fetch tool (#39); both tools load the same cached config, so domain policy is consistent across research tools.

## Inspiration & References

- **OpenRouter Server Tools — Web Search** (official docs): the `openrouter:web_search` tool type, `parameters` (`engine`, `max_results`, `max_total_results`, `search_context_size`, `allowed_domains`, `excluded_domains`), and the requirement that the bound model supports tool calling — https://openrouter.ai/docs/guides/features/server-tools and https://openrouter.ai/docs/guides/features/server-tools/web-search
- **OpenRouter blog — "Consistent Web Search and Fetch Across Every Model"**: confirms server-side search executes at OpenRouter (model decides when/how often to search) and `max_total_results` caps cumulative results across multiple in-request searches — https://openrouter.ai/blog/announcements/agentic-web-tools
- **agentic-planner-core** (`planner/nodes/web_search.py`, `planner/tools/research.py`, `planner/config.py`): the proven implementation this tool ports — `bind(tools=[{"type": "openrouter:web_search", ...}], tool_choice={"type": "openrouter:web_search"})`, the ```json synthesis instruction, 300-char snippet truncation, and the `SourcesConfig` strict-mode validator.
- **ADR-0009** (Radical Simplicity / pure-Python LangGraph) and **ADR-0011** (prebuilt `create_react_agent`) govern the port-and-wrap posture: reuse the proven pattern, expose the capability as an on-demand Worker Tool, do not invent a new search implementation or a static pipeline stage.
- **ADR-0018** (flat `factory.json` Per-Node Model Routing) governs resolving the tool's flash model via `resolve_model_config("web_search")`.
