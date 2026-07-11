import logging

from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from orchestrator import tools as codebase_tools

# Set up logging
logger = logging.getLogger("orchestrator.worker")


# Define the 5 custom tools wrapped for LangChain ReAct Agent
@tool
def read_file(
    path: str, start_line: int | None = None, end_line: int | None = None
) -> str:
    """Read contents of a file, with optional 1-based start_line and end_line bounds (inclusive).

    You MUST call read_file to inspect a file's contents before you can modify it using patch_file.
    """
    return codebase_tools.read_file(path, start_line=start_line, end_line=end_line)


@tool
def list_directory(path: str) -> str:
    """List the contents of a directory, sorted alphabetically with directories first, followed by files."""
    return codebase_tools.list_directory(path)


@tool
def grep_search(query: str, path: str) -> str:
    """Search for the literal query string inside the target path (recursively if directory)."""
    return codebase_tools.grep_search(query, path)


@tool
def patch_file(path: str, old_string: str, new_string: str) -> str:
    """Perform exact search-and-replace of old_string with new_string.

    You can only call patch_file on a file after you have read it using read_file in the current cycle.
    The old_string MUST match exactly once in the file (Ambiguity Abort rule). Include enough surrounding
    context lines in old_string to make it unique. Do not attempt to rewrite the entire file.
    """
    return codebase_tools.patch_file(path, old_string=old_string, new_string=new_string)


@tool
def run_command(command: str) -> str:
    """Execute a shell command safely in a subprocess with shell=False.

    Use this tool to run tests (e.g., `make verify` or python test runner) to verify that your changes
    are correct and do not break the build.
    """
    return codebase_tools.run_command(command)


def get_worker_tools() -> list:
    """Return the list of wrapped LangChain tools for the worker agent."""
    return [read_file, list_directory, grep_search, patch_file, run_command]


# System Prompt incorporating all behavior guardrails
SYSTEM_PROMPT = (
    "You are a professional autonomous software engineer worker agent. "
    "Your objective is to solve the claimed codebase issue by following the provided step-by-step plan. "
    "To do this safely and correctly, you must strictly adhere to the following rules:\n\n"
    "1. READ-BEFORE-EDIT CONSTRAINT:\n"
    "   You must programmatically read a file's contents using the `read_file` tool *before* you make any "
    "modifications to it using `patch_file`. If you attempt to edit a file without reading it first in this "
    "execution cycle, the tool will return a validation error. Do not guess file contents or rely on stale context.\n\n"
    "2. EXACT BLOCK-BASED PATCHING:\n"
    "   To modify a file, use the `patch_file` tool. It performs an exact search-and-replace of `old_string` with `new_string`.\n"
    "   - The `old_string` must match EXACTLY ONE occurrence in the file. If it matches zero or multiple times, "
    "the tool will fail. If you get a 'multiple matches' error, include more surrounding context lines in `old_string` to make the match unique.\n"
    "   - Do NOT rewrite entire files. Under the Lazy Coding Principle, keep your patches targeted, precise, and minimal.\n\n"
    "3. VERIFICATION:\n"
    "   After making any code modifications, you should run the project's test suite or verify script using the "
    "`run_command` tool (typically by running `make verify` or running specific python tests) to ensure your changes "
    "are correct and do not introduce regressions. If tests fail, diagnose and fix the issue.\n\n"
    "4. NO HIGH-LEVEL GRAPH ROUTING OR GIT OPERATIONS:\n"
    "   Your responsibility is purely local codebase editing and verification. Do NOT attempt to run git commands "
    "like `git commit`, `git push`, or use the GitHub CLI to create or merge pull requests. Those high-level lifecycle "
    "phases are handled automatically by other nodes in the orchestrator graph after you exit.\n\n"
    "Work carefully, keep your changes minimal, and ensure the test suite passes before concluding your work."
)


def execute_worker(
    issue_description: str, plan: str, node_name: str = "execute"
) -> str:
    """Execute the worker agent using LangGraph's prebuilt ReAct agent.

    Args:
        issue_description: The description of the issue to solve.
        plan: The step-by-step development plan.
        node_name: The orchestrator node name whose model config to resolve
            via resolve_model_config() (default "execute"). Test-Writer callers
            pass "test_writer" to use the test-writer model.

    Returns:
        The final response text from the agent.
    """
    from orchestrator.config import resolve_model_config, get_chat_model_from_config

    cfg = resolve_model_config(node_name)
    logger.info(
        "Initializing worker agent (node=%s) with model: %s", node_name, cfg["model"]
    )

    llm = get_chat_model_from_config(cfg)
    tools = get_worker_tools()

    # Compile the prebuilt ReAct agent
    # We pass the system prompt as the 'prompt' parameter
    agent = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)

    # Formulate the user message combining issue and plan
    user_message = (
        f"Please solve the following issue:\n\n"
        f"=== ISSUE DESCRIPTION ===\n"
        f"{issue_description}\n\n"
        f"=== DEVELOPMENT PLAN ===\n"
        f"{plan}\n\n"
        f"Start by exploring the codebase to locate the files and read them before editing."
    )

    # We execute the agent with a recursion limit of 30 steps (max_iterations equivalent in LangGraph)
    # to prevent infinite loops.
    config = {"recursion_limit": 30}

    logger.info("Starting worker execution loop...")
    # LangGraph Pregel.invoke overload mismatch; config dict works at runtime.
    result = agent.invoke({"messages": [("user", user_message)]}, config=config)  # type: ignore[call-overload]

    # Extract the last message from the result
    final_message = result["messages"][-1]
    return final_message.content
