"""Workspace Snapshot composition for the Worker prompt (issue #124).

Builds a compact, budget-bounded orientation frame injected into the
Execute/Test-Writer user message: the directory tree plus Structural Outlines
of the plan-localized target files. Baseline orientation therefore costs zero
tool calls — exploration tools remain available for anything the snapshot does
not cover (Aider's repo map is the canonical precedent: a tree-sitter-derived,
budget-capped signature map that serves as a hint, not ground truth).

The snapshot is a *hint*, not authorization: the Read-Before-Edit Constraint
(ADR-0006 / ADR-0033) is untouched — the Worker must still read files in-cycle
before patching them, and this module never touches the read_files registry.
"""

import json
import logging
import os
from pathlib import Path

from orchestrator.constants import IGNORE_DIRS
from orchestrator.outline import (
    OUTLINE_PER_FILE_CAP,
    _resolve_outline_targets,
    extract_outline,
)
from orchestrator.tools import get_workspace_root

logger = logging.getLogger("orchestrator.snapshot")

# Total character budget for the composed snapshot (tree + outlines + notes).
# Bounds the token cost added to every Execute/Test-Writer attempt; env-
# overridable following the BINEVAL_*_MAX_CHARS pattern.
SNAPSHOT_MAX_CHARS = int(os.getenv("AGENT_SNAPSHOT_MAX_CHARS", "20000"))

# Sub-budget for the directory tree so one huge workspace cannot starve the
# targeted outlines. The tree truncates by dropping whole lines (deepest /
# last-walked first in deterministic walk order).
SNAPSHOT_TREE_MAX_CHARS = 6000


def build_directory_tree(workspace_path: Path) -> str:
    """Generates a text-based visual tree of the workspace directory, ignoring common build and environment folders."""
    ignore_dirs = IGNORE_DIRS
    lines = []

    def walk(directory: Path, prefix: str = ""):
        try:
            # Sort directories first, then files alphabetically
            entries = sorted(
                list(directory.iterdir()),
                key=lambda x: (not x.is_dir(), x.name.lower()),
            )
        except Exception:
            return

        entries = [e for e in entries if e.name not in ignore_dirs]
        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            lines.append(
                f"{prefix}{connector}{entry.name}{'/' if entry.is_dir() else ''}"
            )
            if entry.is_dir():
                new_prefix = prefix + ("    " if is_last else "│   ")
                walk(entry, new_prefix)

    walk(workspace_path)
    return "\n".join(lines) if lines else "(empty directory)"


def extract_plan_target_files(plan_text: str | None) -> list[str]:
    """Extract the unique, order-preserved target files from a serialized DevelopmentPlan.

    The Plan-Node persists ``state["plan"]`` as ``DevelopmentPlan.model_dump_json()``;
    each task carries ``target_files`` (the Localization). Returns an empty list on
    any parse failure (legacy prose plans, malformed JSON) so callers degrade to a
    tree-only snapshot instead of raising.
    """
    if not plan_text:
        return []
    try:
        data = json.loads(plan_text)
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []

    files: list[str] = []
    tasks = data.get("tasks", [])
    if not isinstance(tasks, list):
        return []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        target_files = task.get("target_files") or []
        if not isinstance(target_files, list):
            continue
        for file_path in target_files:
            if isinstance(file_path, str) and file_path and file_path not in files:
                files.append(file_path)
    return files


def _truncate_tree(tree: str, char_cap: int) -> str:
    """Deterministically truncate the tree to ``char_cap`` by dropping whole lines."""
    if len(tree) <= char_cap:
        return tree
    kept: list[str] = []
    used = 0
    for line in tree.split("\n"):
        line_len = len(line) + 1  # +1 for the newline
        if used + line_len > char_cap:
            break
        kept.append(line)
        used += line_len
    return "\n".join(kept) + "\n[directory tree truncated to fit snapshot budget]"


def build_workspace_snapshot(
    plan_text: str | None,
    workspace_path: Path | None = None,
    max_chars: int | None = None,
) -> str:
    """Compose the full WORKSPACE SNAPSHOT section for the Worker user message.

    Fill order is deterministic: header → directory tree → per-file Structural
    Outlines of the plan's target files (in first-appearance plan order, per
    file capped at OUTLINE_PER_FILE_CAP). Outline entries that do not fit are
    skipped and reported in an explicit truncation note — later, smaller files
    still get included. Never raises on workspace or plan problems: every
    failure degrades to whatever sections could be built, because this runs on
    the Worker's critical prompt path (ADR-0016 graceful-degradation culture).
    """
    cap = SNAPSHOT_MAX_CHARS if max_chars is None else max_chars

    ws = workspace_path if workspace_path is not None else get_workspace_root()
    parts: list[str] = ["=== WORKSPACE SNAPSHOT ==="]

    tree = build_directory_tree(ws)
    tree = _truncate_tree(tree, SNAPSHOT_TREE_MAX_CHARS)
    parts.append("Directory tree:")
    parts.append(tree)

    target_files = extract_plan_target_files(plan_text)
    if target_files:
        resolved = _resolve_outline_targets(ws, target_files)
        # Extract first, emit the section header only if at least one target
        # yields an outline — never ship an empty "Structural outlines" section.
        entries: list[tuple[str, str]] = []
        for rel_path, full_path in resolved:
            outline = extract_outline(full_path)
            if outline is None:
                continue
            if len(outline) > OUTLINE_PER_FILE_CAP:
                outline = outline[:OUTLINE_PER_FILE_CAP] + "\n[...truncated...]"
            entries.append((rel_path, f"{rel_path}:\n{outline}"))
        if entries:
            parts.append(
                "Structural outlines of the plan's target files "
                "(signatures only — hints, not file contents):"
            )
            omitted: list[str] = []
            for rel_path, entry in entries:
                # Exact join arithmetic: "\n\n".join adds 2 chars per gap, so
                # appending one entry costs len(entry) + 2 * len(parts).
                projected = sum(len(p) for p in parts) + 2 * len(parts) + len(entry)
                if projected > cap:
                    omitted.append(rel_path)
                    continue
                parts.append(entry)
            if omitted:
                shown = ", ".join(omitted[:10])
                extra = "" if len(omitted) <= 10 else ", …"
                parts.append(
                    f"[truncated to fit snapshot budget — outline(s) omitted for: "
                    f"{shown}{extra}]"
                )

    snapshot = "\n\n".join(parts)
    if len(snapshot) > cap:
        snapshot = (
            snapshot[:cap]
            + "\n[workspace snapshot truncated to fit budget — explore with "
            "list_directory/grep_search as needed]"
        )
    return snapshot
