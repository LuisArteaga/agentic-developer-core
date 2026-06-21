# ADR 0005: Pure Python LangGraph Orchestration without CLI Harnesses

## Status
Accepted

## Context
The previous architecture (outlined in earlier drafts of ADR-0005) proposed a hybrid orchestrator model where a Python LangGraph process managed the high-level loop (poll, plan, verify, PR, merge) and spawned the Claude Code CLI as a subprocess worker to perform file-editing and local testing. 

However, invoking the Claude Code CLI (or similar CLI harnesses like OpenCode) headlessly in an autonomous loop introduces several critical complexities:
1. **State & Session Persistence**: Claude Code CLI keeps its transcript and session state in a container-internal directory (`/home/claude/.claude/projects/`). Retaining this across container crashes requires persistent Docker host mounts.
2. **Resumability**: Running `--resume` headlessly over OpenRouter using non-Anthropic models is undocumented and brittle.
3. **Parsing Overhead**: Communicating with a CLI via stdin/stdout stream-wrapping is fragile and error-prone compared to direct API calls.
4. **Vendor Lock-in**: CLI harnesses are often tied to specific model providers (e.g., Claude Code CLI is heavily optimized for Anthropic's Claude 3.7 Sonnet).

We need a simpler, fully programmatic, and model-agnostic way to execute code edits and tests.

## Decision
We will completely eliminate the Claude Code CLI and all other external CLI harnesses (like OpenCode) from the orchestrator's execution path. 

Instead, the entire system will be implemented as a pure Python application:
1. **LangGraph** will model the stateful orchestrator graph.
2. The **Worker (Execute Node)** will be a Python-based LangChain ReAct agent.
3. The ReAct agent will perform code modifications and terminal checks using a custom suite of Python tools (e.g., `read_file`, `patch_file`, `grep_search`, `run_tests`).

## Consequences

### Pros
- **Simplified Infrastructure**: No persistent Docker mounts are required for `/home/claude/.claude/projects/`.
- **True Stateful Resume**: Resuming after a container restart is trivial. The entire agent state is modeled in LangGraph's state schema and can be serialized to a simple local file (`state.json`) or database, and restarted from the exact node.
- **Model Agnosticism**: The worker can use any LLM supported by LangChain (Kimi, DeepSeek, Claude, GPT, etc.) without interface compatibility issues.
- **Robust Tool Execution**: Tools are executed natively in Python, returning structured outputs directly to the model.

### Cons
- **Tool Development**: We must implement and test custom file-editing tools (such as regex-based search-and-replace or diff patching) to ensure the LLM can modify code accurately without causing formatting issues.
- **Loss of Built-in Smart Features**: We lose the out-of-the-box shell intelligence, automatic terminal command retries, and interactive recovery mechanisms built into Claude Code CLI.
