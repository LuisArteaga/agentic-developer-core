"""Enclosing function context enrichment for LLM judge prompts.

Parses a git diff to identify changed files and line ranges, then reads the
original file from disk and uses tree-sitter to extract the full body of the
enclosing function or class method for each hunk. Appends the extracted bodies
as a separate block after the raw diff.

Usage:
    from enrichment import enrich_diff_with_function_context

    diff = sys.stdin.read()
    enriched = enrich_diff_with_function_context(diff, workspace_dir)
    # judges receive enriched (raw_diff + context block)

Design per ADR-0022:
    - All four judges receive enriched context
    - Python only; non-Python files gracefully skipped
    - Per-file truncation at 15,000 chars
    - Raw diff never modified — context is a separate block
    - Test files enriched the same as source files
"""

import logging
import re
import os

from tree_sitter_language_pack import get_parser

logger = logging.getLogger(__name__)

# Regex to match unified diff hunk headers: @@ -start,count +start,count @@
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# Per-file context character limit (ADR-0022: 15,000 chars)
_PER_FILE_CONTEXT_CHARS = 15_000

# Truncation marker appended when a function body exceeds the per-file limit
_TRUNCATION_MARKER = "\n[... truncated ...]\n"


def _parse_hunks(diff: str) -> list[tuple[str, int]]:
    """Parse a unified diff string and extract (filename, hunk_start_line) pairs.

    Returns a list of (filename, start_line) tuples for each hunk in the diff.
    The start_line is the destination (b/) line number from the hunk header.
    Only 'diff --git' sections with a 'b/' path are processed; deleted files
    (no b/ path) are skipped. Binary file diffs are skipped.

    Args:
        diff: A unified git diff string.

    Returns:
        List of (filename, start_line) for each hunk.
    """
    hunks: list[tuple[str, int]] = []
    current_file: str | None = None

    for line in diff.splitlines():
        # Detect file header
        if line.startswith("diff --git "):
            # Parse "diff --git a/path b/path" or "diff --git a/path b/newpath"
            tokens = line.split()
            if len(tokens) >= 4:
                b_path = tokens[-1]
                if b_path.startswith("b/"):
                    current_file = b_path[2:]
                else:
                    current_file = None
            else:
                current_file = None
            continue

        if current_file is None:
            continue

        # Detect binary file indicator
        if line.startswith("Binary files "):
            current_file = None
            continue

        # Detect hunk header
        m = _HUNK_HEADER_RE.match(line)
        if m:
            dest_start = int(m.group(3))
            hunks.append((current_file, dest_start))

    return hunks


def _get_enclosing_function_for_line(
    source_bytes: bytes, line_number: int
) -> tuple[str | None, str | None]:
    """Find the enclosing function or class method for a given line number.

    Uses tree-sitter to parse the source and traverse the AST upward from the
    given line to find the nearest enclosing function_definition or
    class_definition node (specifically class methods). Returns the function
    name and full body text.

    Args:
        source_bytes: The raw source file content as bytes.
        line_number: 1-based line number to find the enclosing function for.

    Returns:
        (function_name, function_body) tuple, or (None, None) if no enclosing
        function/method is found.
    """
    parser = get_parser("python")
    tree = parser.parse(source_bytes)

    node = tree.root_node

    # Walk up from the deepest AST node at the target line to find a function or
    # class method definition.
    deepest = node.descendant_for_point_range(
        (line_number - 1, 0), (line_number - 1, 0)
    )

    # Traverse ancestors to find enclosing function_definition or
    # decorated_definition (which wraps decorated functions or classes).
    current = deepest
    while current is not None and current != node:
        if current.type in ("function_definition", "decorated_definition"):
            # For decorated_definition, the named node is the wrapped
            # function or class — a @dataclass-decorated class wraps a
            # class_definition, not a function_definition.
            if current.type == "decorated_definition":
                named_node = next(
                    (
                        child
                        for child in current.children
                        if child.type in ("function_definition", "class_definition")
                    ),
                    None,
                )
                # Use the decorated_definition's byte range to include decorators
                body_node = current
            else:
                named_node = current
                # Check if this function_definition is inside a decorated_definition
                candidate = current.parent
                if candidate is not None and candidate.type == "decorated_definition":
                    body_node = candidate
                else:
                    body_node = current

            # A decorated_definition wrapping neither a function nor a class
            # carries no usable name — keep walking instead of crashing. The
            # python grammar always names functions and classes today, so
            # these guards only guard against parser-version drift.
            if named_node is None:  # pragma: no cover
                current = current.parent
                continue
            name_node = named_node.child_by_field_name("name")
            if name_node is None:  # pragma: no cover
                current = current.parent
                continue
            fn_name = source_bytes[name_node.start_byte : name_node.end_byte].decode(
                "utf-8", errors="replace"
            )

            # Extract full function body (including decorators if decorated)
            body = source_bytes[body_node.start_byte : body_node.end_byte].decode(
                "utf-8", errors="replace"
            )
            return fn_name, body

        current = current.parent

    return None, None


def _truncate_context_block(block: str, budget: int) -> str:
    """Truncate a single file's context block to *budget* characters.

    Appends a truncation marker when the block is too long.
    """
    if len(block) <= budget:
        return block
    return block[:budget] + _TRUNCATION_MARKER


def enrich_diff_with_function_context(diff: str, workspace_dir: str) -> str:
    """Enrich a git diff with enclosing function context using tree-sitter.

    Parses each ``@@``-hunk from the diff, reads the original file from the
    workspace checkout, uses tree-sitter to find the enclosing function or class
    method, and appends the full function body as a separate block.

    The raw diff is never modified — context is appended as a clearly
    delimited ``=== ENCLOSING FUNCTION CONTEXT ===`` block.

    Args:
        diff: A unified git diff string (from stdin or ``git diff``).
        workspace_dir: Path to the workspace checkout directory
            (``GITHUB_WORKSPACE``).

    Returns:
        The enriched string: ``raw_diff + "\\n\\n=== ENCLOSING FUNCTION CONTEXT ===\\n" +
        context_blocks``. If no hunks are found or enrichment produces no
        context, the original diff is returned unchanged.
    """
    hunks = _parse_hunks(diff)
    if not hunks:
        return diff

    # Deduplicate by (file, function_name) so we don't include the same
    # function body multiple times for multiple hunks in the same function.
    seen: set[tuple[str, str]] = set()
    context_blocks: list[str] = []

    for filename, hunk_line in hunks:
        # Anti-path-traversal: resolve the full path and verify it stays
        # within the workspace directory (security gate).
        filepath = os.path.normpath(os.path.join(workspace_dir, filename))
        resolved = os.path.realpath(filepath)
        workspace_resolved = os.path.realpath(workspace_dir)
        if (
            not resolved.startswith(workspace_resolved + os.sep)
            and resolved != workspace_resolved
        ):
            logger.debug("Skipping path outside workspace: %s", filepath)
            continue

        if not os.path.isfile(resolved):
            continue

        # Only process Python files
        if not filename.endswith(".py"):
            continue

        try:
            with open(resolved, "rb") as f:
                source_bytes = f.read()
        except OSError:
            logger.debug("Could not read file %s for enrichment", filepath)
            continue

        fn_name, fn_body = _get_enclosing_function_for_line(source_bytes, hunk_line)
        if fn_name is None or fn_body is None:
            continue

        key = (filename, fn_name)
        if key in seen:
            continue
        seen.add(key)

        truncated_body = _truncate_context_block(fn_body, _PER_FILE_CONTEXT_CHARS)
        context_blocks.append(f"--- {filename} :: {fn_name} ---\n{truncated_body}")

    if not context_blocks:
        return diff

    enriched = (
        diff + "\n\n=== ENCLOSING FUNCTION CONTEXT ===\n" + "\n\n".join(context_blocks)
    )
    return enriched
