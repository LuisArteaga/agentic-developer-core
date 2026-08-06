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

# Forbidden credential/config file names and certificate extensions, all
# lower-cased so the comparison in is_safe_path is case-insensitive: on a
# case-insensitive filesystem (macOS/Windows) `.ENV` or `.Git` would otherwise
# bypass a case-sensitive blocklist (ADR-0037 quick win).
_FORBIDDEN_EXTENSIONS = (".pem", ".key", ".pkcs12", ".pfx")


def is_safe_path(path_str: str) -> bool:
    """Verifies that a path is safe and does not point to sensitive configuration or credential files."""
    p = Path(path_str)
    if p.is_absolute() or ".." in p.parts:
        return False

    for part in p.parts:
        # Case-insensitive comparison: normalize each path component to lower
        # case before matching against the forbidden sets/extensions.
        part_lower = part.lower()
        if part_lower in _FORBIDDEN_DIRS:
            return False
        stem_lower = Path(part_lower).stem
        if part_lower in _FORBIDDEN_NAMES or stem_lower in _FORBIDDEN_NAMES:
            return False
        if part_lower.endswith(_FORBIDDEN_EXTENSIONS):
            return False

    return True
