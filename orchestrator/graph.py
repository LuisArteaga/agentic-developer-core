import logging

from langgraph.graph import END, START, StateGraph

from orchestrator.nodes import (
    claim_node,
    execute_node,
    merge_node,
    plan_node,
    pr_node,
    recovery_node,
    test_writer_node,
    verify_node,
)
from orchestrator.state import AgentState

logger = logging.getLogger("orchestrator.graph")


def route_after_claim(state: AgentState) -> str:
    """Routes execution after the Claim-Node, supporting both fresh runs and resume paths."""
    status = state.get("status")
    phase = state.get("phase")

    if status == "idle":
        return "end"
    elif status == "claimed" or status == "planning":
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
        logger.warning(
            "Unknown status '%s' in route_after_claim. Defaulting to plan.", status
        )
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
    """Routes execution after the Verify-Node based on success, retry, or failure status.

    The Verify-Node encompasses both the deterministic `make verify` command and
    the Pre-PR BinEval Review (soft semantic gate). Both failure modes share the
    `attempts["verify"]` counter and signal their outcome via `status`:
      - "executing" → make verify OR BinEval failed with retries remaining → execute
      - "failed"   → make verify OR BinEval exhausted attempts (3/3) → recovery
      - "verifying" → make verify AND BinEval passed (or was skipped / infra-fallback)
                     → pr (Pull Request creation)
    No graph-level change was needed: BinEval PASS/FAIL is signaled through the
    existing `status` field, so this router already handles the transition.
    """
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
    """Routes execution after the Merge-Node.

    Closes the post-PR judge-feedback loop (ADR-0036): when ``merge_node`` parses
    actionable PR Review Judge verdicts (FAIL / NEEDS REVIEW, ADR-0014) and the
    merge-fix budget is not exhausted, it signals a retry by setting
    ``status="executing"`` + ``phase="merge_fix"``. This router then returns
    ``"execute"`` so the Worker receives the judge findings via the existing
    ``state["feedback"]`` injection path, re-enters ``pr_node`` for a ``fix:``
    commit, and re-polls the merge. The remaining statuses are unchanged:
    ``failed`` -> ``recovery`` (incl. cap-exhaustion escalation and poll
    timeout), ``done`` -> ``end``.
    """
    status = state.get("status")
    if status == "failed":
        return "recovery"
    if status == "executing":
        # Merge-fix retry: actionable judge feedback with budget remaining.
        return "execute"
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
        "recovery": "recovery",
    },
)

builder.add_conditional_edges(
    "plan", route_after_plan, {"test_writer": "test_writer", "recovery": "recovery"}
)

builder.add_conditional_edges(
    "test_writer",
    route_after_test_writer,
    {"execute": "execute", "recovery": "recovery"},
)

builder.add_conditional_edges(
    "execute", route_after_execute, {"verify": "verify", "recovery": "recovery"}
)

builder.add_conditional_edges(
    "verify",
    route_after_verify,
    {"execute": "execute", "recovery": "recovery", "pr": "pr"},
)

builder.add_conditional_edges(
    "pr", route_after_pr, {"merge": "merge", "recovery": "recovery"}
)

builder.add_conditional_edges(
    "merge",
    route_after_merge,
    {"end": END, "execute": "execute", "recovery": "recovery"},
)

builder.add_edge("recovery", END)

# Expose compiled app instance
graph = builder.compile()
