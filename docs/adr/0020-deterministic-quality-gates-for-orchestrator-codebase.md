# Deterministic Quality Gates for the Orchestrator's Own Codebase

The orchestrator's own Python codebase (`orchestrator/`, `scripts/`) is gated by deterministic tooling — ruff (lint + format), mypy (type-check), pytest (tests) — via `make verify`. This does not contradict the Language Agnosticism principle: that principle applies exclusively to the *target repositories* the autonomous loop verifies, whose language is unknown and whose deterministic checks are their own responsibility. The orchestrator itself is a known-language (Python) codebase and can be held to deterministic standards.

Supply-chain and coverage tools (semgrep, pip-audit, coverage gate) run at pre-commit and CI only — not in `make verify` — because they audit environment/trend state the autonomous Worker cannot act on deterministically mid-cycle, and `make verify` is invoked in-cycle up to 3× by the Verify-Node. Pre-commit hooks prioritise developer flow speed over exhaustive scanning; the trade-off is that security-sensitive checks (semgrep, pip-audit) run later in the CI pipeline rather than blocking the developer at commit time.

## Inspiration & References
* **Bernát Gábor — "Defense in Depth: A Practical Guide to Python Supply Chain Security"** ([blog](https://bernat.tech/posts/securing-python-supply-chain)): recommends `pip-audit` in CI, hash-pinning dependencies, and treating supply-chain scanning as a CI-only defense layer.
* **Greg Świtowski — "pre-commit vs CI"** ([blog](https://switowski.com/blog/pre-commit-vs-ci)): argues pre-commit is developer convenience while CI is the enforcement layer that "cannot be forgotten."
