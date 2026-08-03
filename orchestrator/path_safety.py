"""Centralized path safety validation for the orchestrator.

Extracted from nodes.py to serve as a shared, acyclic security utility.
Both plan_node (primary validation at the trust boundary) and outline.py
(defense-in-depth) import from this module.
"""

from pathlib import Path

_FORBIDDEN_NAMES = {
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "credentials",
    "passwd",
    "shadow",
    "authorized_keys",
}

_FORBIDDEN_DIRS = {".git", ".venv", ".agent_logs", ".agents", "node_modules"}


def is_safe_path(path_str: str) -> bool:
    """Verifies that a path is safe and does not point to sensitive configuration or credential files."""
    p = Path(path_str)
    if p.is_absolute() or ".." in p.parts:
        return False

    for part in p.parts:
        if part in _FORBIDDEN_DIRS:
            return False
        part_stem = Path(part).stem
        if part in _FORBIDDEN_NAMES or part_stem in _FORBIDDEN_NAMES:
            return False
        if part.endswith((".pem", ".key", ".pkcs12", ".pfx")):
            return False

    return True
