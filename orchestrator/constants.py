"""Shared constants for workspace walking and file filtering.

Used by both the directory tree generator (nodes.py) and the structural
outline extractor (outline.py) to ensure consistent ignore behavior.
"""

IGNORE_DIRS = {
    ".git",
    ".venv",
    ".agent_logs",
    ".agents",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
}

# Distinct exit code for security-block failures (ADR-0044). In the POSIX
# user-defined range (no special shell meaning), chosen to be memorable and
# distinct from the generic crash exit 1. Consumed by the process supervisor
# (scripts/entrypoint.py) to branch security blocks away from exponential
# backoff and into the circuit-breaker path. Mirrored in scripts/entrypoint.py
# because the supervisor is a zero-dependency process that does not import the
# orchestrator package (ADR-0015).
SECURITY_BLOCK_EXIT_CODE = 42
