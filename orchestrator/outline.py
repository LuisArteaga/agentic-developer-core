"""Structural Outline extraction via tree-sitter.

Produces source-like outlines (class/method/function signatures with parameter
lists and return types) for Python and TypeScript/TSX files. Used by the
Plan-Node as a Localization aid — outlines give the planner the vocabulary to
name specific symbols so the Worker starts closer to the target.

Outlines are hints, not ground truth. The Worker's Read-Before-Edit Constraint
remains the safety net against hallucinated edits.
"""

import logging
from dataclasses import dataclass, field
from importlib import resources  # nosemgrep
from pathlib import Path

from tree_sitter import Language, Node, Query, QueryCursor
from tree_sitter_language_pack import get_language, get_parser

from orchestrator.constants import IGNORE_DIRS
from orchestrator.path_safety import is_safe_path

logger = logging.getLogger(__name__)

OUTLINE_CHAR_CAP = 30_000
OUTLINE_PER_FILE_CAP = 8_000

_EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
}

_QUERY_CACHE: dict[str, Query] = {}


@dataclass
class OutlineResult:
    outlines: str
    truncated_files: list[str] = field(default_factory=list)


def _load_query(lang_name: str) -> Query:
    # Cache per actual language — a Query is bound to a specific Language object.
    # TSX and TypeScript share the same .scm file but need separate Query objects.
    if lang_name in _QUERY_CACHE:
        return _QUERY_CACHE[lang_name]

    query_key = "typescript" if lang_name in ("typescript", "tsx") else lang_name
    query_file = f"{query_key}-tags.scm"
    query_text = (
        resources.files("orchestrator.queries").joinpath(query_file).read_text()
    )
    language: Language = get_language(lang_name)
    query = Query(language, query_text)
    _QUERY_CACHE[lang_name] = query
    return query


def _get_definition_matches(
    source: bytes, lang_name: str
) -> list[tuple[int, str, Node]]:
    """Parse source and return ordered definition matches.

    Returns a list of (pattern_index, capture_name, node) tuples in source
    order. Only @definition.* captures are included.
    """
    parser = get_parser(lang_name)
    tree = parser.parse(source)
    query = _load_query(lang_name)
    cursor = QueryCursor(query)
    matches = cursor.matches(tree.root_node)

    result: list[tuple[int, str, Node]] = []
    for match in matches:
        pattern_index = match[0]
        captures = match[1]
        for cap_name, nodes in captures.items():
            if cap_name.startswith("definition."):
                for node in nodes:
                    result.append((pattern_index, cap_name, node))
    return result


def _render_python_outline(source: str, matches: list[tuple[int, str, Node]]) -> str:
    """Render a source-like outline for Python.

    Classes are expanded (methods indented). Decorators are included verbatim
    above the signature. Signatures are the first source line of the definition
    node, with body elided as ``: ...``.
    """
    lines = source.split("\n")
    output: list[str] = []

    # Track which definition nodes (by start row) have been rendered,
    # to avoid rendering a method both inside its class and standalone.
    rendered_starts: set[int] = set()

    def _collect_decorators(row: int) -> tuple[list[str], int]:
        decs: list[str] = []
        cr = row - 1
        while cr >= 0 and lines[cr].strip().startswith("@"):
            decs.insert(0, lines[cr].strip())
            row = cr
            cr -= 1
        return decs, row

    for _pattern_idx, cap_name, node in matches:
        start_row = node.start_point[0]
        end_row = node.end_point[0]

        if start_row in rendered_starts:
            continue

        if cap_name == "definition.class":
            decs, _ = _collect_decorators(start_row)
            output.extend(decs)
            output.append(lines[start_row].strip())
            rendered_starts.add(start_row)

            # Render methods nested inside this class
            for _pi2, cn2, node2 in matches:
                if cn2 != "definition.function":
                    continue
                m_start = node2.start_point[0]
                m_end = node2.end_point[0]
                if m_start > start_row and m_end <= end_row:
                    if m_start in rendered_starts:
                        continue
                    m_decs, _ = _collect_decorators(m_start)
                    for dl in m_decs:
                        output.append(f"    {dl}")
                    output.append(f"    {lines[m_start].strip()}: ...")
                    rendered_starts.add(m_start)

        elif cap_name == "definition.function":
            decs, _ = _collect_decorators(start_row)
            output.extend(decs)
            output.append(f"{lines[start_row].strip()}: ...")
            rendered_starts.add(start_row)

    return "\n".join(output)


def _render_typescript_outline(
    source: str, matches: list[tuple[int, str, Node]]
) -> str:
    """Render a source-like outline for TypeScript/TSX.

    Classes are expanded (methods indented). Interfaces, type aliases, and
    enums are collapsed to header + ``{ ... }``.
    """
    lines = source.split("\n")
    output: list[str] = []

    rendered_starts: set[int] = set()

    def _extract_sig(line: str) -> str:
        sig = line.strip()
        if "{" in sig:
            sig = sig[: sig.index("{")].strip()
        elif ";" in sig:
            sig = sig[: sig.index(";")].strip()
        return sig

    for _pattern_idx, cap_name, node in matches:
        start_row = node.start_point[0]
        end_row = node.end_point[0]

        if start_row in rendered_starts:
            continue

        if cap_name == "definition.class":
            output.append(lines[start_row].strip())
            rendered_starts.add(start_row)

            for _pi2, cn2, node2 in matches:
                if cn2 != "definition.method":
                    continue
                m_start = node2.start_point[0]
                m_end = node2.end_point[0]
                if m_start > start_row and m_end <= end_row:
                    if m_start in rendered_starts:
                        continue
                    sig = _extract_sig(lines[m_start])
                    output.append(f"  {sig};")
                    rendered_starts.add(m_start)

        elif cap_name in ("definition.interface", "definition.enum"):
            header = _extract_sig(lines[start_row])
            output.append(f"{header} {{ ... }}")
            rendered_starts.add(start_row)

        elif cap_name == "definition.type":
            header = _extract_sig(lines[start_row])
            output.append(f"{header};")
            rendered_starts.add(start_row)

        elif cap_name == "definition.function":
            sig = _extract_sig(lines[start_row])
            output.append(f"{sig};")
            rendered_starts.add(start_row)

    return "\n".join(output)


def extract_outline(file_path: Path) -> str | None:
    """Extract a structural outline from a single source file.

    Returns the formatted outline string, or None for unsupported extensions.
    Emits partial outlines on parse errors (tree-sitter error recovery).
    """
    ext = file_path.suffix
    lang_name = _EXTENSION_MAP.get(ext)
    if lang_name is None:
        return None

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("Could not read %s for outline: %s", file_path, e)
        return None

    if not source.strip():
        return None

    source_bytes = source.encode("utf-8")

    try:
        matches = _get_definition_matches(source_bytes, lang_name)
    except Exception as e:
        logger.warning("Could not parse %s for outline: %s", file_path, e)
        return None

    if not matches:
        return None

    if lang_name == "python":
        rendered = _render_python_outline(source, matches)
    else:
        rendered = _render_typescript_outline(source, matches)

    if not rendered.strip():
        return None

    return rendered


def _walk_source_files(workspace_path: Path) -> list[Path]:
    """Walk the workspace and return sorted .py/.ts/.tsx files, respecting IGNORE_DIRS."""
    result: list[Path] = []

    def walk(directory: Path) -> None:
        try:
            entries = sorted(
                list(directory.iterdir()),
                key=lambda x: (not x.is_dir(), x.name.lower()),
            )
        except Exception:
            return

        entries = [e for e in entries if e.name not in IGNORE_DIRS]
        for entry in entries:
            if entry.is_dir():
                walk(entry)
            elif entry.suffix in _EXTENSION_MAP:
                result.append(entry)

    walk(workspace_path)
    return result


def build_outlines(workspace_path: Path, char_cap: int) -> OutlineResult:
    """Build structural outlines for the entire workspace.

    Walks the workspace, extracts outlines per source file, and applies the
    character cap. Whole-file outlines are dropped in tree-walk order
    (last-generated dropped first) to fit the cap.

    Returns an OutlineResult with the outlines text and a list of truncated files.
    """
    files = _walk_source_files(workspace_path)

    # First pass: extract all outlines
    file_outlines: list[tuple[str, str]] = []  # (relative_path, outline_text)
    for file_path in files:
        outline = extract_outline(file_path)
        if outline is None:
            continue
        try:
            rel_path = str(file_path.relative_to(workspace_path))
        except ValueError:
            rel_path = str(file_path)

        # Apply per-file cap
        if len(outline) > OUTLINE_PER_FILE_CAP:
            outline = outline[:OUTLINE_PER_FILE_CAP] + "\n[...truncated...]"

        file_outlines.append((rel_path, outline))

    if not file_outlines:
        return OutlineResult(outlines="", truncated_files=[])

    # Second pass: build the formatted string, dropping whole files from the
    # end if we exceed the cap
    truncated_files: list[str] = []
    included: list[str] = []

    # Calculate total size if we include everything
    total_size = sum(len(f"{rp}:\n{o}\n\n") for rp, o in file_outlines)

    if total_size <= char_cap:
        # Everything fits
        for rel_path, outline in file_outlines:
            included.append(f"{rel_path}:\n{outline}")
    else:
        # Include files in tree-walk order until budget is exhausted.
        # Once a file doesn't fit, all remaining files are truncated —
        # strict tree-walk-order dropping (last-generated dropped first).
        budget = char_cap
        dropped = False
        for rel_path, outline in file_outlines:
            if dropped:
                truncated_files.append(rel_path)
                continue
            entry = f"{rel_path}:\n{outline}"
            entry_size = len(entry) + 2  # +2 for \n\n separator
            if entry_size <= budget:
                included.append(entry)
                budget -= entry_size
            else:
                truncated_files.append(rel_path)
                dropped = True

    outlines_text = "\n\n".join(included)

    if truncated_files and included:
        count = len(truncated_files)
        outlines_text += (
            f"\n\n[truncated — {count} file{'s' if count != 1 else ''} omitted]"
        )

    return OutlineResult(outlines=outlines_text, truncated_files=truncated_files)


def build_outlines_for_files(workspace_path: Path, file_paths: list[str]) -> str:
    """Fetch full outlines for specific files (Plan Detail Request).

    No cap — these are targeted requests. Path safety is validated defensively
    (defense-in-depth) in addition to the primary validation in plan_node.
    """
    included: list[str] = []
    workspace_resolved = workspace_path.resolve()

    for rel_path in file_paths:
        if not is_safe_path(rel_path):
            logger.warning("Skipping unsafe path in outline request: %s", rel_path)
            continue

        full_path = workspace_path / rel_path
        if not full_path.exists() or not full_path.is_file():
            logger.debug("Skipping non-existent file in outline request: %s", rel_path)
            continue

        # Resolve symlinks and verify the real path stays within the workspace
        # (defense against symlink-based workspace escape via prompt injection)
        try:
            resolved = full_path.resolve()
            resolved.relative_to(workspace_resolved)
        except (ValueError, OSError):
            logger.warning(
                "Skipping path outside workspace in outline request: %s", rel_path
            )
            continue

        ext = full_path.suffix
        if ext not in _EXTENSION_MAP:
            logger.debug(
                "Skipping unsupported extension in outline request: %s", rel_path
            )
            continue

        outline = extract_outline(full_path)
        if outline is None:
            continue

        included.append(f"{rel_path}:\n{outline}")

    return "\n\n".join(included)
