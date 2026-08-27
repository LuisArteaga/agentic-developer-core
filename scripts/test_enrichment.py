#!/usr/bin/env python3
"""Unit tests for scripts/enrichment.py — enclosing function context enrichment.

Tests cover: hunk extraction, tree-sitter boundary detection, truncation at
15K chars, non-Python file skip, multi-file enrichment, and the INC-001
false-positive regression scenario.
"""

import sys
import tempfile
import unittest
from pathlib import Path

# Make enrichment.py importable from the same directory.
scripts_dir = Path(__file__).resolve().parent
sys.path.insert(0, str(scripts_dir))

from enrichment import (  # noqa: E402
    _parse_hunks,
    enrich_diff_with_function_context,
)


class TestParseHunks(unittest.TestCase):
    """Unit tests for _parse_hunks — extracting (filename, start_line) from diffs."""

    def test_single_file_single_hunk(self):
        diff = (
            "diff --git a/foo.py b/foo.py\n"
            "index abc..def 100644\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -10,7 +10,7 @@ def existing_function():\n"
        )
        result = _parse_hunks(diff)
        self.assertEqual(result, [("foo.py", 10)])

    def test_multi_file_multi_hunk(self):
        diff = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,5 +1,6 @@\n"
            "diff --git a/b.py b/b.py\n"
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -20,3 +20,4 @@\n"
            "@@ -30,3 +31,3 @@\n"
        )
        result = _parse_hunks(diff)
        self.assertEqual(result, [("a.py", 1), ("b.py", 20), ("b.py", 31)])

    def test_skips_binary_files(self):
        diff = (
            "diff --git a/image.png b/image.png\n"
            "Binary files a/image.png and b/image.png differ\n"
            "diff --git a/foo.py b/foo.py\n"
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -5,3 +5,4 @@\n"
        )
        result = _parse_hunks(diff)
        self.assertEqual(result, [("foo.py", 5)])

    def test_skips_deleted_files(self):
        diff = (
            "diff --git a/deleted.py a/deleted.py\n"
            "deleted file mode 100644\n"
            "index abc..000000\n"
            "--- a/deleted.py\n"
            "@@ -1,5 +0,0 @@\n"
        )
        result = _parse_hunks(diff)
        self.assertEqual(result, [])

    def test_empty_diff(self):
        self.assertEqual(_parse_hunks(""), [])

    def test_hunk_without_count(self):
        diff = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n"
        result = _parse_hunks(diff)
        self.assertEqual(result, [("foo.py", 1)])


class TestEnrichDiffWithFunctionContext(unittest.TestCase):
    """Integration tests for full enrichment pipeline using a temp workspace."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_file(self, path: str, content: str):
        abs_path = self.workspace / path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

    def _diff(self, filepath: str, start: int, count: int = 1) -> str:
        return (
            f"diff --git a/{filepath} b/{filepath}\n"
            f"--- a/{filepath}\n"
            f"+++ b/{filepath}\n"
            f"@@ -{start},{count} +{start},{count} @@\n"
        )

    def test_enrich_single_function(self):
        """AC: correctly extracts the enclosing function for a single hunk."""
        self._write_file(
            "app.py",
            "def greet(name):\n"
            "    return f'Hello, {name}!'\n"
            "\n"
            "def farewell(name):\n"
            "    return f'Goodbye, {name}!'\n",
        )
        diff = self._diff("app.py", 1)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertIn("=== ENCLOSING FUNCTION CONTEXT ===", enriched)
        self.assertIn("--- app.py :: greet ---", enriched)
        self.assertIn("def greet(name):", enriched)
        self.assertIn("return f'Hello, {name}!'", enriched)
        self.assertNotIn("farewell", enriched)
        self.assertTrue(enriched.startswith(diff))

    def test_enrich_class_method(self):
        """AC: correctly extracts enclosing class method."""
        self._write_file(
            "app.py",
            "class Calculator:\n"
            "    def add(self, a, b):\n"
            "        return a + b\n"
            "\n"
            "    def multiply(self, a, b):\n"
            "        return a * b\n",
        )
        diff = self._diff("app.py", 3)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertIn("--- app.py :: add ---", enriched)
        self.assertIn("def add(self, a, b):", enriched)
        self.assertIn("return a + b", enriched)
        self.assertNotIn("multiply", enriched)

    def test_non_python_file_skipped(self):
        """AC: non-Python files (JSON, YAML, MD) are skipped gracefully."""
        self._write_file("config.json", '{"key": "value"}')
        diff = self._diff("config.json", 1)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertEqual(enriched, diff)

    def test_truncation_at_15k_chars(self):
        """AC: per-file context truncated at 15,000 chars with marker."""
        # Create a function with a body well over 15,000 chars
        large_body = "        pass\n" * 5000  # ~45,000 chars
        self._write_file(
            "app.py",
            "def large_func():\n" + large_body,
        )
        diff = self._diff("app.py", 1)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertIn("[... truncated ...]", enriched)
        context_start = enriched.find("=== ENCLOSING FUNCTION CONTEXT ===")
        context_block = enriched[context_start:]
        # Context block should be around 15K + overhead (headers, truncation marker)
        self.assertLess(len(context_block), 25_000)

    def test_multi_file_enrichment(self):
        """AC: multiple files in the diff each get their own context block."""
        self._write_file("a.py", "def func_a():\n    return 1\n")
        self._write_file("b.py", "def func_b():\n    return 2\n")
        diff = self._diff("a.py", 1) + self._diff("b.py", 1)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertIn("--- a.py :: func_a ---", enriched)
        self.assertIn("--- b.py :: func_b ---", enriched)

    def test_deduplicate_same_function_multiple_hunks(self):
        """AC: multiple hunks in the same function produce only one context block."""
        self._write_file(
            "app.py",
            "def func():\n    x = 1\n    y = 2\n    z = 3\n    return x + y + z\n",
        )
        # Two hunks within the same function
        diff = (
            "diff --git a/app.py b/app.py\n"
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,3 @@\n"
            "@@ -3,2 +4,2 @@\n"
        )
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        # The function should appear only once
        occurrences = enriched.count("--- app.py :: func ---")
        self.assertEqual(occurrences, 1)

    def test_file_not_in_workspace_skipped(self):
        """AC: file referenced in diff but missing from workspace is skipped."""
        diff = self._diff("nonexistent.py", 1)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertEqual(enriched, diff)

    def test_inc001_regression(self):
        """Regression test: INC-001 false positive scenario.

        Two assertFalse lines with different IPs in the same test function
        should not produce a false duplicate finding — the full function body
        makes the IP distinction obvious.
        """
        self._write_file(
            "test_ip.py",
            "def test_ip_ranges():\n"
            "    assert is_private_ip('127.0.0.1')\n"
            "    assert not is_link_local_ip('169.254.169.254')\n",
        )
        diff = (
            "diff --git a/test_ip.py b/test_ip.py\n"
            "--- a/test_ip.py\n"
            "+++ b/test_ip.py\n"
            "@@ -1,2 +1,2 @@\n"
        )
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        # The function body should contain both IP addresses, making the
        # distinction visible to the judge.
        self.assertIn("127.0.0.1", enriched)
        self.assertIn("169.254.169.254", enriched)

    def test_no_hunks_returns_diff_unchanged(self):
        """Edge case: diff with file headers but no hunks returns unchanged."""
        # A diff with only the header line (no @@ block)
        enriched = enrich_diff_with_function_context(
            "diff --git a/foo.py b/foo.py\n", str(self.workspace)
        )
        self.assertNotIn("=== ENCLOSING FUNCTION CONTEXT ===", enriched)

    def test_path_traversal_blocked(self):
        """Edge case: path outside workspace is skipped."""
        self._write_file("../outside.py", "def escape():\n    pass\n")
        diff = self._diff("../outside.py", 1)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertNotIn("escape", enriched)

    def test_decorated_function(self):
        """Edge case: function with decorators is correctly extracted."""
        self._write_file(
            "decorated.py",
            "@app.route('/test')\n"
            "@login_required\n"
            "def my_view():\n"
            "    return 'hello'\n"
            "\n"
            "def other():\n"
            "    pass\n",
        )
        diff = self._diff("decorated.py", 3)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertIn("--- decorated.py :: my_view ---", enriched)
        self.assertIn("@app.route('/test')", enriched)
        self.assertIn("return 'hello'", enriched)
        self.assertNotIn("other", enriched)

    def test_decorated_function_hunk_at_decorator_line(self):
        """Edge case: hunk line points at the decorator line, not the def line.

        Covers the decorated_definition branch where the walker encounters the
        decorated_definition node first (when the hunk start is on a decorator).
        """
        self._write_file(
            "decorated.py",
            "@app.route('/test')\n"
            "@login_required\n"
            "def my_view():\n"
            "    return 'hello'\n",
        )
        # Hunk line 1 = the @app.route decorator line
        diff = self._diff("decorated.py", 1)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertIn("--- decorated.py :: my_view ---", enriched)
        self.assertIn("@app.route('/test')", enriched)

    def test_decorated_class_body_line(self):
        """Regression: changed line inside a decorated class must not crash.

        ``@dataclass``-decorated classes wrap a ``class_definition``, not a
        ``function_definition``. The walker used to assume a function and
        hit ``assert name_node is not None`` on the decorated node, killing
        the judge run for any diff touching a dataclass field.
        """
        self._write_file(
            "domain.py",
            "from dataclasses import dataclass\n"
            "\n"
            "\n"
            "@dataclass(frozen=True)\n"
            "class Isa95Hierarchy:\n"
            '    """Optional equipment hierarchy labels."""\n'
            "\n"
            "    enterprise: str | None = None\n"
            "    site: str | None = None\n",
        )
        # Hunk line 8 = the ``enterprise`` field inside the class body.
        diff = self._diff("domain.py", 8)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertIn("--- domain.py :: Isa95Hierarchy ---", enriched)
        self.assertIn("@dataclass(frozen=True)", enriched)
        self.assertIn("enterprise: str | None = None", enriched)

    def test_decorated_class_hunk_at_decorator_line(self):
        """Regression: decorator-line hunk on a class resolves the class name."""
        self._write_file(
            "domain.py",
            "@dataclass(frozen=True)\nclass Point:\n    x: float\n    y: float\n",
        )
        # Hunk line 1 = the @dataclass decorator line.
        diff = self._diff("domain.py", 1)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertIn("--- domain.py :: Point ---", enriched)
        self.assertIn("x: float", enriched)

    def test_plain_class_body_line_has_no_function_context(self):
        """Changed lines in undecorated classes keep producing no context."""
        self._write_file(
            "model.py",
            "class Config:\n    name: str = 'x'\n",
        )
        diff = self._diff("model.py", 1)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertNotIn("=== ENCLOSING FUNCTION CONTEXT ===", enriched)

    def test_parse_diff_without_b_path(self):
        """Edge case: diff header with fewer than 4 tokens produces no hunks."""
        enriched = enrich_diff_with_function_context(
            "diff --git only_a_path\n", str(self.workspace)
        )
        self.assertEqual(enriched, "diff --git only_a_path\n")

    def test_unreadable_file_skipped(self):
        """Edge case: file exists but can't be read (OSError) is skipped."""
        test_file = "locked.py"
        self._write_file(test_file, "def secret():\n    pass\n")
        abs_path = self.workspace / test_file
        # Make file unreadable
        abs_path.chmod(0o000)
        diff = self._diff(test_file, 1)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        self.assertNotIn("=== ENCLOSING FUNCTION CONTEXT ===", enriched)
        # Restore permissions so cleanup works
        abs_path.chmod(0o644)

    def test_no_context_blocks_returns_diff_unchanged(self):
        """Edge case: hunks exist but no enclosing function is found."""
        self._write_file(
            "module.py",
            "# Just module-level code, no functions\nimport os\nx = 1\n",
        )
        diff = self._diff("module.py", 1)
        enriched = enrich_diff_with_function_context(diff, str(self.workspace))
        # File exists, .py passes, but line 1 has no enclosing function
        self.assertNotIn("=== ENCLOSING FUNCTION CONTEXT ===", enriched)


if __name__ == "__main__":
    unittest.main()
