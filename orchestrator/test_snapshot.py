"""Unit tests for the Workspace Snapshot builder (issue #124).

Covers: directory tree building/ignores, plan-target extraction (including
malformed/legacy plan degradation), snapshot composition (outlines included,
unsafe/missing targets skipped), deterministic truncation notes, and budget
enforcement.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator.snapshot import (
    SNAPSHOT_MAX_CHARS,
    build_directory_tree,
    build_workspace_snapshot,
    extract_plan_target_files,
)


def _make_plan(*target_files_groups: list[str]) -> str:
    """Build a serialized DevelopmentPlan-like JSON string."""
    return json.dumps(
        {
            "rationale": "r",
            "tasks": [
                {
                    "step_number": i + 1,
                    "action": "patch",
                    "description": "d",
                    "target_files": group,
                }
                for i, group in enumerate(target_files_groups)
            ],
        }
    )


class TestBuildDirectoryTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)

    def test_lists_files_with_hierarchy(self):
        (self.ws / "src").mkdir()
        (self.ws / "src" / "main.py").write_text("x = 1\n")
        (self.ws / "README.md").write_text("hi\n")
        tree = build_directory_tree(self.ws)
        self.assertIn("src/", tree)
        self.assertIn("main.py", tree)
        self.assertIn("README.md", tree)

    def test_ignored_dirs_absent(self):
        (self.ws / ".git").mkdir()
        (self.ws / ".venv").mkdir()
        (self.ws / "__pycache__").mkdir()
        (self.ws / ".venv" / "bin").mkdir()
        (self.ws / "keep.py").write_text("")
        tree = build_directory_tree(self.ws)
        self.assertNotIn(".git", tree)
        self.assertNotIn(".venv", tree)
        self.assertNotIn("__pycache__", tree)
        self.assertIn("keep.py", tree)

    def test_empty_workspace(self):
        self.assertEqual(build_directory_tree(self.ws), "(empty directory)")

    def test_unreadable_directory_is_skipped_not_fatal(self):
        # The walker's defensive branch (snapshot.py iterdir except-path): an
        # unreadable directory degrades to an empty listing instead of
        # crashing snapshot construction on the Worker's critical prompt path.
        with mock.patch("pathlib.Path.iterdir", side_effect=PermissionError("denied")):
            tree = build_directory_tree(self.ws)
        self.assertEqual(tree, "(empty directory)")


class TestExtractPlanTargetFiles(unittest.TestCase):
    def test_unique_order_preserved(self):
        plan = _make_plan(["b.py", "a.py"], ["a.py", "c.py"])
        self.assertEqual(extract_plan_target_files(plan), ["b.py", "a.py", "c.py"])

    def test_malformed_json_returns_empty(self):
        self.assertEqual(extract_plan_target_files("{not json"), [])

    def test_non_dict_json_returns_empty(self):
        self.assertEqual(extract_plan_target_files("[1, 2, 3]"), [])
        self.assertEqual(extract_plan_target_files("42"), [])

    def test_legacy_prose_plan_returns_empty(self):
        self.assertEqual(
            extract_plan_target_files("1. Read the file\n2. Patch it\n"), []
        )

    def test_none_and_empty_return_empty(self):
        self.assertEqual(extract_plan_target_files(None), [])
        self.assertEqual(extract_plan_target_files(""), [])


class TestBuildWorkspaceSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = Path(self._tmp.name)
        (self.ws / "app.py").write_text(
            "class Greeter:\n"
            "    def greet(self, name: str) -> str:\n"
            "        return name\n"
        )

    def test_contains_header_tree_and_outline(self):
        snap = build_workspace_snapshot(_make_plan(["app.py"]), workspace_path=self.ws)
        self.assertTrue(snap.startswith("=== WORKSPACE SNAPSHOT ==="))
        self.assertIn("Directory tree:", snap)
        self.assertIn("app.py", snap)
        self.assertIn("class Greeter:", snap)
        self.assertIn("def greet(self, name: str) -> str:", snap)

    def test_outlines_section_absent_without_resolvable_targets(self):
        # Unsupported extension, missing file: nothing resolvable -> no
        # outlines section at all, but the tree remains.
        snap = build_workspace_snapshot(
            _make_plan(["notes.md", "ghost.py"]), workspace_path=self.ws
        )
        self.assertIn("Directory tree:", snap)
        self.assertNotIn("Structural outlines", snap)

    def test_unsafe_path_is_skipped(self):
        snap = build_workspace_snapshot(_make_plan([".env"]), workspace_path=self.ws)
        self.assertNotIn("Structural outlines", snap)

    def test_duplicate_targets_produce_single_entry(self):
        snap = build_workspace_snapshot(
            _make_plan(["app.py"], ["app.py"]), workspace_path=self.ws
        )
        self.assertEqual(snap.count("class Greeter:"), 1)

    def test_malformed_plan_degrades_to_tree_only(self):
        snap = build_workspace_snapshot("prose plan", workspace_path=self.ws)
        self.assertIn("=== WORKSPACE SNAPSHOT ===", snap)
        self.assertIn("Directory tree:", snap)
        self.assertNotIn("Structural outlines", snap)

    def test_total_budget_enforced_with_final_truncation_note(self):
        snap = build_workspace_snapshot(
            _make_plan(["app.py"]), workspace_path=self.ws, max_chars=200
        )
        marker = (
            "\n[workspace snapshot truncated to fit budget — explore with "
            "list_directory/grep_search as needed]"
        )
        self.assertLessEqual(len(snap), 200 + len(marker))
        self.assertIn("[workspace snapshot truncated to fit budget", snap)

    def test_non_fitting_outline_reported_as_omitted(self):
        # A generous per-file outline plus a tight overall cap: the tree still
        # fits, but the outline entry cannot — the file must be named in the
        # omission note rather than silently dropped.
        big = "class C:\n" + "".join(
            f"    def m{i}(self, a: int) -> int:\n        return a\n" for i in range(40)
        )
        (self.ws / "big.py").write_text(big)
        snap = build_workspace_snapshot(
            _make_plan(["big.py"]), workspace_path=self.ws, max_chars=300
        )
        self.assertIn("Directory tree:", snap)
        self.assertIn("outline(s) omitted for: big.py", snap)
        self.assertNotIn("def m0(", snap)

    def test_smaller_later_file_still_included_after_skip(self):
        (self.ws / "big.py").write_text(
            "class Big:\n"
            + "".join(f"    def m{i}(self) -> None:\n        pass\n" for i in range(40))
        )
        (self.ws / "tiny.py").write_text("def t() -> None:\n    pass\n")
        snap = build_workspace_snapshot(
            _make_plan(["big.py", "tiny.py"]),
            workspace_path=self.ws,
            max_chars=400,
        )
        self.assertIn("outline(s) omitted for: big.py", snap)
        self.assertIn("tiny.py", snap)
        self.assertIn("def t() -> None:", snap)

    def test_outline_exceeding_per_file_cap_is_sliced_with_marker(self):
        # A single file whose outline alone exceeds OUTLINE_PER_FILE_CAP is
        # deterministically sliced with the same marker build_outlines uses.
        huge = "class Huge:\n" + "".join(
            f"    def method_{i}(self, alpha: int, beta: int) -> int:\n"
            f"        return alpha + {i}\n"
            for i in range(200)
        )
        (self.ws / "huge.py").write_text(huge)
        snap = build_workspace_snapshot(
            _make_plan(["huge.py"]),
            workspace_path=self.ws,
            max_chars=SNAPSHOT_MAX_CHARS * 2,
        )
        self.assertIn("huge.py", snap)
        self.assertIn("[...truncated...]", snap)
        self.assertNotIn("method_199(", snap)

    def test_truncate_tree_drops_whole_lines_and_notes(self):
        from orchestrator.snapshot import _truncate_tree

        tree = "\n".join(f"line{i}" for i in range(50))
        result = _truncate_tree(tree, 40)
        note = "\n[directory tree truncated to fit snapshot budget]"
        self.assertLessEqual(len(result), 40 + len(note))
        self.assertTrue(result.startswith("line0\n"))
        self.assertIn("[directory tree truncated to fit snapshot budget]", result)
        # Whole-line semantics: the last kept line is complete, never clipped.
        kept_lines = result.split("\n")[:-1]
        self.assertTrue(all(line.startswith("line") for line in kept_lines))

    def test_truncate_tree_short_tree_unchanged(self):
        from orchestrator.snapshot import _truncate_tree

        tree = "a.py\nb.py"
        self.assertEqual(_truncate_tree(tree, 100), tree)

    def test_existing_target_without_extractable_outline_is_skipped(self):
        # A target that passes validation but yields no outline (empty source)
        # is silently skipped — no empty outline entry, no crash.
        (self.ws / "blank.py").write_text("")
        snap = build_workspace_snapshot(
            _make_plan(["blank.py"]), workspace_path=self.ws
        )
        self.assertIn("Directory tree:", snap)
        self.assertNotIn("Structural outlines", snap)

    def test_plan_tasks_missing_or_malformed_degrade_to_tree_only(self):
        snap = build_workspace_snapshot(
            '{"rationale": "no tasks key"}', workspace_path=self.ws
        )
        self.assertNotIn("Structural outlines", snap)
        snap = build_workspace_snapshot(
            '{"tasks": ["not-a-dict", {"target_files": "not-a-list"}]}',
            workspace_path=self.ws,
        )
        self.assertEqual(extract_plan_target_files('{"tasks": 5}'), [])
        self.assertNotIn("Structural outlines", snap)


if __name__ == "__main__":
    unittest.main()
