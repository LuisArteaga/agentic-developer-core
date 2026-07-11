# Deterministic Quality Gates for the Orchestrator's Own Codebase

The orchestrator's own Python codebase (`orchestrator/`, `scripts/`) is gated by deterministic tooling — ruff (lint + format), mypy (type-check), pytest (tests) — via `make verify`, mirroring the proven pattern from `agentic-planner-core`. This does not contradict the Language Agnosticism principle: that principle applies exclusively to the *target repositories* the autonomous loop verifies, whose language is unknown and whose deterministic checks are their own responsibility. The orchestrator itself is a known-language (Python) codebase and can be held to deterministic standards.

Supply-chain and coverage tools (semgrep, pip-audit, coverage gate) run at pre-commit and CI only — not in `make verify` — because they audit environment/trend state the autonomous Worker cannot act on deterministically mid-cycle, and `make verify` is invoked in-cycle up to 3× by the Verify-Node.
