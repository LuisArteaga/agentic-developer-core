# ADR 0011: Prebuilt LangGraph ReAct Agent for Worker Node

## Status
Accepted

## Context
The orchestrator's Worker node (implemented in `orchestrator/worker.py`) is responsible for executing local codebase modifications and running verification tests to solve claimed issues. To achieve this, the Worker must run a ReAct (Reasoning and Acting) loop: invoking the LLM, parsing tool calls, executing the corresponding local tools (`read_file`, `patch_file`, `list_directory`, `grep_search`, `run_command`), returning tool outputs to the LLM, and repeating until a final answer is produced.

We evaluated three architectural approaches for executing this ReAct loop:
1. **Legacy LangChain `AgentExecutor`**: The traditional, class-based executor.
2. **Custom Python ReAct Loop**: A hand-written `while` loop managing model invocations, tool execution, and error handling natively in Python.
3. **LangGraph Prebuilt `create_react_agent`**: A compiled state subgraph provided by the LangGraph framework.

We weighed these options across three key engineering dimensions: Software Architecture, Information Security, and ML/AI Engineering.

## Decision
We decided to use **LangGraph's prebuilt `create_react_agent`** as the execution framework for the Worker node.

The decision was driven by the following consensus:
* **Telemetry and Tracing (Architecture)**: Operating the Worker as a compiled LangGraph subgraph ensures that telemetry tools (such as OpenTelemetry and LangSmith) can trace the entire execution hierarchically. The individual tool execution turns are recorded as child spans under the parent `execute_worker` node, preserving full visibility. A custom Python loop would appear as a single "black box" span, losing detailed step traces.
* **Tool-Calling Reliability (ML Engineering)**: Modern LLMs are trained natively on tool-calling APIs (returning structured JSON payloads) rather than parsing text-based ReAct prompts (e.g. `Action: / Action Input: `). LangGraph's prebuilt agent natively supports provider-specific tool-calling integrations and handles the formatting of `ToolMessage` and `AIMessage` history correctly, eliminating brittle, hand-written parsing code.
* **Delegated Safety (Security)**: The core safety constraints—such as the **Read-Before-Edit Constraint** and the **Block-Based Patching Uniqueness Check**—are programmatically enforced inside the tools themselves (`orchestrator/tools.py`), rather than the agent executor. Therefore, a stateless framework-provided executor does not compromise security boundaries.
* **Radical Simplicity**: We completely rejected legacy LangChain `AgentExecutor` to avoid deprecated code paths. We rejected a custom loop to minimize boilerplate code, preventing bugs in multi-turn message synchronization and tool-error propagation.

## Consequences

### Pros
* **Unified Telemetry**: Full step-by-step tracing of the worker's reasoning and tool calls is preserved in the OTel span tree.
* **Robustness**: Native tool-calling support is highly reliable and compatible with any LLM supported by LangChain.
* **Zero Boilerplate**: Avoids writing and maintaining 50+ lines of custom loop control and parsing code.
* **Modern Standard**: Aligns with the modern LangGraph ecosystem.

### Cons
* **Framework Dependency**: We rely on LangGraph's prebuilt graph implementation and its specific step-limit handling (which returns a fallback message instead of raising a catchable exception on recursion limit).

## Rejected Alternatives

### 1. Custom Python ReAct Loop
* **Why Rejected**: Implementing a custom loop that robustly parses different models' tool-calling formats, formats message histories, handles parallel tool execution, and propagates tool errors back to the model is highly complex and error-prone. It violates the **Lazy Coding Principle** by reinventing well-tested framework code.

### 2. Legacy LangChain `AgentExecutor`
* **Why Rejected**: The traditional `AgentExecutor` is deprecated in the LangChain ecosystem. Relying on it introduces technical debt and conflicts with the modern LangGraph architecture used for the parent orchestrator.
