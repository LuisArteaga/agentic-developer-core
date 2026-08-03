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
