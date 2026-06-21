# ADR 0005: Hybrid LangGraph Orchestrator with Claude Code Worker

## Status
Superseded by [ADR 0009: Pure Python LangGraph Orchestration without CLI Harnesses](./0009-pure-python-langgraph-orchestrator.md)

## Context
We need to coordinate the high-level orchestrator process (polling, planning, verifying, pull request creation, and merging) with the actual code-writing process (Worker). 

## Decision
The orchestrator will run a Python LangGraph process. For code-writing and local test execution, it will spawn the Claude Code CLI as a subprocess worker and persist the session transcripts.

## Consequences
- Requires a persistent Docker host mount for `/home/claude/.claude/projects/` to retain session transcripts.
- Relies on streaming log parsing to evaluate worker progress.
- Hard to resume headlessly on non-Anthropic models.
