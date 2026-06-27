import logging
from langgraph.graph import StateGraph, START, END
from orchestrator.state import AgentState
from orchestrator.nodes import (
    claim_node,
    plan_node,
    test_writer_node,
    execute_node,
    verify_node,
    pr_node,
    merge_node,
    recovery_node,
)

logger = logging.getLogger("orchestrator.graph")

def route_after_claim(state: AgentState) -> str:
    """Routes execution after the Claim-Node, supporting both fresh runs and resume paths."""
    status = state.get("status")
    phase = state.get("phase")
    
    if status == "idle":
        return "end"
    elif status == "claimed":
        return "plan"
    elif status == "planning":
        return "plan"
    elif status == "executing" and phase == "test_writing":
        return "test_writer"
    elif status == "executing":
        return "execute"
    elif status == "verifying":
        return "verify"
    elif status == "pr_open":
        return "pr"
    elif status == "merging":
        return "merge"
    elif status == "failed":
        return "recovery"
    elif status == "done":
        return "end"
    else:
        logger.warning("Unknown status '%s' in route_after_claim. Defaulting to plan.", status)
        return "plan"

def route_after_plan(state: AgentState) -> str:
    """Routes execution after the Plan-Node to Test-Writer Node (Test-First/TDD)."""
    if state.get("status") == "failed":
        return "recovery"
    return "test_writer"

def route_after_test_writer(state: AgentState) -> str:
    """Routes execution after the Test-Writer Node to Execute-Node."""
    if state.get("status") == "failed":
        return "recovery"
    return "execute"

def route_after_execute(state: AgentState) -> str:
    """Routes execution after the Execute-Node."""
    if state.get("status") == "failed":
        return "recovery"
    return "verify"

def route_after_verify(state: AgentState) -> str:
    """Routes execution after the Verify-Node based on success, retry, or failure status."""
    status = state.get("status")
    if status == "executing":
        return "execute"
    elif status == "failed":
        return "recovery"
    else:
        return "pr"

def route_after_pr(state: AgentState) -> str:
    """Routes execution after the PR-Node."""
    if state.get("status") == "failed":
        return "recovery"
    return "merge"

def route_after_merge(state: AgentState) -> str:
    """Routes execution after the Merge-Node."""
    if state.get("status") == "failed":
        return "recovery"
    return "end"


# Compile the LangGraph state machine
builder = StateGraph(AgentState)

# Add all nodes
builder.add_node("claim", claim_node)
builder.add_node("plan", plan_node)
builder.add_node("test_writer", test_writer_node)
builder.add_node("execute", execute_node)
builder.add_node("verify", verify_node)
builder.add_node("pr", pr_node)
builder.add_node("merge", merge_node)
builder.add_node("recovery", recovery_node)

# Define transitions and edges
builder.add_edge(START, "claim")

builder.add_conditional_edges(
    "claim",
    route_after_claim,
    {
        "end": END,
        "plan": "plan",
        "test_writer": "test_writer",
        "execute": "execute",
        "verify": "verify",
        "pr": "pr",
        "merge": "merge",
        "recovery": "recovery"
    }
)

builder.add_conditional_edges(
    "plan",
    route_after_plan,
    {
        "test_writer": "test_writer",
        "recovery": "recovery"
    }
)

builder.add_conditional_edges(
    "test_writer",
    route_after_test_writer,
    {
        "execute": "execute",
        "recovery": "recovery"
    }
)

builder.add_conditional_edges(
    "execute",
    route_after_execute,
    {
        "verify": "verify",
        "recovery": "recovery"
    }
)

builder.add_conditional_edges(
    "verify",
    route_after_verify,
    {
        "execute": "execute",
        "recovery": "recovery",
        "pr": "pr"
    }
)

builder.add_conditional_edges(
    "pr",
    route_after_pr,
    {
        "merge": "merge",
        "recovery": "recovery"
    }
)

builder.add_conditional_edges(
    "merge",
    route_after_merge,
    {
        "end": END,
        "recovery": "recovery"
    }
)

builder.add_edge("recovery", END)

# Expose compiled app instance
graph = builder.compile()
