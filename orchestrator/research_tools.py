"""Web Search Worker Tool.

A LangChain ``@tool`` that lets the ReAct Worker research library APIs, error
messages, or unfamiliar patterns inline during the Execute phase. The tool is a
structured-output wrapper around OpenRouter's server-side ``web_search`` tool:
it binds ``openrouter:web_search`` to a cheap flash model (resolved via
``resolve_model_config("web_search")``), forces ``tool_choice`` to guarantee
search execution, instructs the model to append a JSON block of results, and
parses that block into ``[{title, url, snippet}]`` with snippets truncated to
300 chars.

Search runs server-side at OpenRouter, so SSRF risk is near-zero (the tool
makes no direct outbound HTTP to retrieved URLs — that is the URL Fetch tool's
job, issue #39). This is the proven pattern ported from agentic-planner-core's
``planner/nodes/web_search.py`` and ``planner/tools/research.py`` (ADR-0009
Radical Simplicity), wrapped as an on-demand Worker Tool rather than a static
graph node (ADR-0011 ReAct pattern).
"""

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from orchestrator.config import get_chat_model_from_config, resolve_model_config
from orchestrator.sources_config import SourcesConfig, load_sources_config

logger = logging.getLogger("orchestrator.research_tools")

# Match planner-core: snippets are truncated to keep tool results concise and
# bound the tokens injected back into the Worker's ReAct context.
MAX_SNIPPET_CHARS = 300

_SYSTEM_INSTRUCTION = (
    "You are a technical researcher. You MUST use the openrouter:web_search tool "
    "to execute a search for the requested query. Do not try to answer without using the tool.\n\n"
    "After you perform the search and receive the results, synthesize your findings. "
    "At the end of your response, you MUST append a JSON block representing the list of "
    "search results you found. Snippets must be truncated to be concise "
    f"(max {MAX_SNIPPET_CHARS} chars per snippet).\n\n"
    "The JSON block must start with ```json and end with ```, and look exactly like this:\n"
    "[\n"
    "  {\n"
    '    "title": "Result Title",\n'
    '    "url": "http://example.com/result",\n'
    f'    "snippet": "A brief summary of findings (max {MAX_SNIPPET_CHARS} chars)"\n'
    "  }\n"
    "]"
)


def _build_search_tool_parameters(sources: SourcesConfig) -> dict[str, Any]:
    """Build the OpenRouter ``web_search`` tool ``parameters`` object from the
    loaded ``SourcesConfig``. Only includes parameters that are set, mirroring
    planner/nodes/web_search.py."""
    sp = sources.search
    params: dict[str, Any] = {"engine": sp.engine}
    if sp.search_context_size:
        params["search_context_size"] = sp.search_context_size
    if sp.max_results:
        params["max_results"] = sp.max_results
    if sp.max_total_results:
        params["max_total_results"] = sp.max_total_results
    # Strict mode restricts the search to configured domains.
    if sources.strict and sources.domains:
        params["allowed_domains"] = list(sources.domains)
    if sp.excluded_domains:
        params["excluded_domains"] = list(sp.excluded_domains)
    return params


def _extract_json_block(text: str) -> str:
    """Extract a JSON array block from Markdown output.

    Prefers a fenced ```json block; falls back to the first ``[...]`` array
    pattern. Ported verbatim from planner/nodes/web_search.py.
    """
    match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"(\[.*\])", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _coerce_text(content: Any) -> str:
    """Coerce an LLM response ``content`` (str or list of content blocks) to a
    single string for JSON extraction."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content) if content is not None else ""


def _parse_search_results(text_content: str) -> list[dict[str, str]]:
    """Parse the model's structured JSON output into ``[{title, url, snippet}]``,
    truncating snippets to ``MAX_SNIPPET_CHARS``. Returns ``[]`` on parse
    failure or when the output is not a list of URL-bearing objects."""
    results: list[dict[str, str]] = []
    try:
        json_text = _extract_json_block(text_content)
        parsed = json.loads(json_text)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse web_search JSON output: %s", e)
        return []

    if not isinstance(parsed, list):
        logger.warning("web_search JSON output was not a list: %r", type(parsed))
        return []

    for item in parsed:
        if isinstance(item, dict) and "url" in item:
            snippet = item.get("snippet", "")
            if len(snippet) > MAX_SNIPPET_CHARS:
                snippet = snippet[: MAX_SNIPPET_CHARS - 3] + "..."
            results.append(
                {
                    "title": item.get("title", "No Title"),
                    "url": item.get("url", ""),
                    "snippet": snippet,
                }
            )
    return results


@tool
def web_search(query: str) -> str:
    """Search the web for current information.

    Use this tool when you encounter an unfamiliar API, a library version change,
    or an error message you cannot resolve from the codebase alone. Returns a JSON
    list of results: [{"title": str, "url": str, "snippet": str}].
    """
    sources = load_sources_config()
    cfg = resolve_model_config("web_search")
    logger.info(
        "Running web_search for query: %s (model=%s, engine=%s, strict=%s)",
        query,
        cfg["model"],
        sources.search.engine,
        sources.strict,
    )
    llm = get_chat_model_from_config(cfg)

    tool_definition: dict[str, Any] = {
        "type": "openrouter:web_search",
        "parameters": _build_search_tool_parameters(sources),
    }
    # Bind the server-side tool and force tool_choice to guarantee the model
    # actually executes the search rather than answering from memory.
    llm_with_tools = llm.bind(
        tools=[tool_definition],
        tool_choice={"type": "openrouter:web_search"},
    )

    user_message = f"Search Query: {query}"
    if sources.strict and sources.domains:
        user_message += (
            "\nAllowed Domains (search is strictly restricted to these): "
            f"{', '.join(sources.domains)}"
        )

    try:
        response = llm_with_tools.invoke(
            [
                SystemMessage(content=_SYSTEM_INSTRUCTION),
                HumanMessage(content=user_message),
            ]
        )
    except Exception as e:
        logger.error("web_search invocation failed: %s", e)
        return f"Error executing web_search for query '{query}': {e}"

    text_content = _coerce_text(response.content)
    results = _parse_search_results(text_content)
    if not results:
        return f"No search results found for query: {query}"
    return json.dumps(results, ensure_ascii=False)
