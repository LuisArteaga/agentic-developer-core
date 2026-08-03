"""Unit tests for structural outline extraction (tree-sitter-based)."""

import tempfile
import unittest
from pathlib import Path

from orchestrator.outline import (
    OutlineResult,
    build_outlines,
    build_outlines_for_files,
    extract_outline,
)


class TestExtractOutlinePython(unittest.TestCase):
    """Tests for Python outline extraction: class, method, function, decorators."""

    def _write_temp(self, suffix: str, content: str) -> Path:
        f = tempfile.NamedTemporaryFile(suffix=suffix, mode="w", delete=False)
        f.write(content)
        f.flush()
        f.close()
        self.addCleanup(lambda: Path(f.name).unlink(missing_ok=True))
        return Path(f.name)

    def test_class_with_methods(self):
        code = "class Calculator:\n    def add(self, a: int, b: int) -> int:\n        return a + b\n    def subtract(self, a: int, b: int) -> int:\n        return a - b\n"
        outline = self._write_temp(".py", code)
        result = extract_outline(outline)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("class Calculator:", result)
        self.assertIn("def add(self, a: int, b: int) -> int:", result)
        self.assertIn("def subtract(self, a: int, b: int) -> int:", result)

    def test_top_level_function(self):
        code = "def multiply(a: int, b: int) -> int:\n    return a * b\n"
        outline = self._write_temp(".py", code)
        result = extract_outline(outline)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("def multiply(a: int, b: int) -> int:", result)

    def test_decorators_included(self):
        code = "@dataclass\nclass Foo:\n    @staticmethod\n    def bar(x: int) -> str:\n        return str(x)\n"
        outline = self._write_temp(".py", code)
        result = extract_outline(outline)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("@dataclass", result)
        self.assertIn("@staticmethod", result)

    def test_methods_indented_under_class(self):
        code = "class Foo:\n    def bar(self) -> None:\n        pass\ndef top_level() -> int:\n    return 42\n"
        outline = self._write_temp(".py", code)
        result = extract_outline(outline)
        self.assertIsNotNone(result)
        assert result is not None
        lines = result.split("\n")
        # Find the method line — it should be indented
        method_line = [line for line in lines if "def bar" in line]
        self.assertEqual(len(method_line), 1)
        self.assertTrue(method_line[0].startswith("    "))
        # Top-level function should NOT be indented
        func_line = [line for line in lines if "def top_level" in line]
        self.assertEqual(len(func_line), 1)
        self.assertFalse(func_line[0].startswith("    "))

    def test_unsupported_extension_returns_none(self):
        code = "package main\nfunc main() {}\n"
        outline = self._write_temp(".go", code)
        result = extract_outline(outline)
        self.assertIsNone(result)

    def test_empty_file_returns_none(self):
        outline = self._write_temp(".py", "")
        result = extract_outline(outline)
        self.assertIsNone(result)

    def test_file_with_no_definitions_returns_none(self):
        code = "x = 42\ny = 'hello'\n"
        outline = self._write_temp(".py", code)
        result = extract_outline(outline)
        self.assertIsNone(result)


class TestExtractOutlineTypeScript(unittest.TestCase):
    """Tests for TypeScript/TSX outline extraction: class, method, function, interface, type, enum."""

    def _write_temp(self, suffix: str, content: str) -> Path:
        f = tempfile.NamedTemporaryFile(suffix=suffix, mode="w", delete=False)
        f.write(content)
        f.flush()
        f.close()
        self.addCleanup(lambda: Path(f.name).unlink(missing_ok=True))
        return Path(f.name)

    def test_class_with_methods(self):
        code = "export class Calculator {\n  public add(a: number, b: number): number { return a + b; }\n  public subtract(a: number, b: number): number { return a - b; }\n}\n"
        outline = self._write_temp(".ts", code)
        result = extract_outline(outline)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("class Calculator", result)
        self.assertIn("public add(a: number, b: number): number", result)
        self.assertIn("public subtract(a: number, b: number): number", result)

    def test_interface_collapsed(self):
        code = "export interface Config {\n  x: number;\n  y: string;\n}\n"
        outline = self._write_temp(".ts", code)
        result = extract_outline(outline)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("interface Config", result)
        self.assertIn("{ ... }", result)
        # Members should NOT be expanded
        self.assertNotIn("x: number", result)

    def test_type_alias(self):
        code = 'export type Status = "active" | "inactive";\n'
        outline = self._write_temp(".ts", code)
        result = extract_outline(outline)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("type Status", result)

    def test_enum_collapsed(self):
        code = "export enum Color {\n  Red,\n  Green,\n  Blue,\n}\n"
        outline = self._write_temp(".ts", code)
        result = extract_outline(outline)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("enum Color", result)
        self.assertIn("{ ... }", result)

    def test_function_declaration(self):
        code = "export function multiply(a: number, b: number): number {\n  return a * b;\n}\n"
        outline = self._write_temp(".ts", code)
        result = extract_outline(outline)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("function multiply(a: number, b: number): number", result)

    def test_tsx_uses_tsx_grammar(self):
        """TSX files should parse JSX correctly (no ERROR nodes)."""
        code = "export class Component {\n  render() { return <div>Hello</div>; }\n}\nexport interface Props { name: string; }\n"
        outline = self._write_temp(".tsx", code)
        result = extract_outline(outline)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("class Component", result)
        self.assertIn("render()", result)
        self.assertIn("interface Props", result)

    def test_export_statement_does_not_hide_declarations(self):
        """Declarations wrapped in export_statement should still be captured."""
        code = "export class Foo {}\nexport function bar(): void {}\nexport interface Baz { x: number; }\n"
        outline = self._write_temp(".ts", code)
        result = extract_outline(outline)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("class Foo", result)
        self.assertIn("function bar(): void", result)
        self.assertIn("interface Baz", result)


class TestBuildOutlines(unittest.TestCase):
    """Tests for workspace-level outline building with caps and truncation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.addCleanup(self.temp_dir.cleanup)

    def _create_file(self, rel_path: str, content: str):
        full_path = self.workspace / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

    def test_all_files_fit_no_truncation(self):
        self._create_file("src/a.py", "def foo() -> None:\n    pass\n")
        self._create_file(
            "src/b.py", "class Bar:\n    def baz(self) -> int:\n        return 42\n"
        )
        result = build_outlines(self.workspace, char_cap=30_000)
        self.assertIn("src/a.py", result.outlines)
        self.assertIn("src/b.py", result.outlines)
        self.assertEqual(result.truncated_files, [])

    def test_truncation_drops_whole_files(self):
        # Create enough files to exceed a small cap
        for i in range(20):
            self._create_file(
                f"src/file_{i:02d}.py",
                f"def func_{i}(x: int) -> int:\n    return {i}\n",
            )
        result = build_outlines(self.workspace, char_cap=500)
        self.assertGreater(len(result.truncated_files), 0)
        self.assertIn("[truncated", result.outlines)

    def test_truncation_marker_shows_count(self):
        for i in range(20):
            self._create_file(
                f"src/file_{i:02d}.py",
                f"def func_{i}(x: int) -> int:\n    return {i}\n",
            )
        result = build_outlines(self.workspace, char_cap=500)
        self.assertIn("files omitted", result.outlines)

    def test_ignore_dirs_excluded(self):
        self._create_file("src/main.py", "def main() -> None:\n    pass\n")
        self._create_file(".git/config.py", "def secret() -> None:\n    pass\n")
        self._create_file("node_modules/lib.py", "def lib() -> None:\n    pass\n")
        result = build_outlines(self.workspace, char_cap=30_000)
        self.assertIn("src/main.py", result.outlines)
        self.assertNotIn(".git", result.outlines)
        self.assertNotIn("node_modules", result.outlines)

    def test_unsupported_extensions_skipped(self):
        self._create_file("src/main.py", "def main() -> None:\n    pass\n")
        self._create_file("src/utils.go", "package utils\n")
        self._create_file("src/config.json", '{"key": "value"}\n')
        result = build_outlines(self.workspace, char_cap=30_000)
        self.assertIn("src/main.py", result.outlines)
        self.assertNotIn("utils.go", result.outlines)
        self.assertNotIn("config.json", result.outlines)

    def test_empty_workspace_returns_empty(self):
        result = build_outlines(self.workspace, char_cap=30_000)
        self.assertEqual(result.outlines, "")
        self.assertEqual(result.truncated_files, [])

    def test_zero_budget_drops_all_outlines(self):
        self._create_file("src/a.py", "def foo() -> None:\n    pass\n")
        result = build_outlines(self.workspace, char_cap=0)
        self.assertEqual(result.outlines, "")
        self.assertIn("src/a.py", result.truncated_files)

    def test_per_file_cap_truncates_large_files(self):
        """A single file exceeding OUTLINE_PER_FILE_CAP gets truncated with marker."""
        # Create a file with many definitions to exceed the per-file cap
        defs = "\n".join(
            f"def func_{i}(x: int) -> int:\n    return {i}" for i in range(500)
        )
        self._create_file("big.py", defs)
        result = build_outlines(self.workspace, char_cap=30_000)
        self.assertIn("[...truncated...]", result.outlines)


class TestBuildOutlinesForFiles(unittest.TestCase):
    """Tests for targeted outline fetching (Plan Detail Request)."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name).resolve()
        self.addCleanup(self.temp_dir.cleanup)

    def _create_file(self, rel_path: str, content: str):
        full_path = self.workspace / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

    def test_fetch_existing_file(self):
        self._create_file("src/a.py", "def foo() -> None:\n    pass\n")
        result = build_outlines_for_files(self.workspace, ["src/a.py"])
        self.assertIn("src/a.py", result)
        self.assertIn("def foo", result)

    def test_skip_nonexistent_file(self):
        self._create_file("src/a.py", "def foo() -> None:\n    pass\n")
        result = build_outlines_for_files(
            self.workspace, ["src/a.py", "src/nonexistent.py"]
        )
        self.assertIn("src/a.py", result)
        self.assertNotIn("nonexistent", result)

    def test_skip_unsafe_path(self):
        self._create_file("src/a.py", "def foo() -> None:\n    pass\n")
        result = build_outlines_for_files(
            self.workspace, ["src/a.py", ".env", "../secret.py"]
        )
        self.assertIn("src/a.py", result)
        self.assertNotIn(".env", result)
        self.assertNotIn("secret", result)

    def test_skip_unsupported_extension(self):
        self._create_file("src/a.py", "def foo() -> None:\n    pass\n")
        self._create_file("src/b.go", "package main\n")
        result = build_outlines_for_files(self.workspace, ["src/a.py", "src/b.go"])
        self.assertIn("src/a.py", result)
        self.assertNotIn("src/b.go", result)

    def test_empty_request_returns_empty(self):
        result = build_outlines_for_files(self.workspace, [])
        self.assertEqual(result, "")


class TestOutlineResultDataclass(unittest.TestCase):
    def test_default_empty(self):
        result = OutlineResult(outlines="")
        self.assertEqual(result.outlines, "")
        self.assertEqual(result.truncated_files, [])

    def test_with_truncated_files(self):
        result = OutlineResult(
            outlines="some outlines", truncated_files=["a.py", "b.py"]
        )
        self.assertEqual(result.truncated_files, ["a.py", "b.py"])


class TestParseErrorRecovery(unittest.TestCase):
    """Tree-sitter should emit partial outlines on files with syntax errors."""

    def test_python_with_syntax_error(self):
        f = tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False)
        f.write(
            "class Foo:\n    def bar(self) -> int:\n        return 42\n\ndef valid_func() -> None:\n    pass\n\nclass !!!BROKEN\n"
        )
        f.flush()
        f.close()
        self.addCleanup(lambda: Path(f.name).unlink(missing_ok=True))
        result = extract_outline(Path(f.name))
        # Should still capture valid definitions despite the syntax error
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("class Foo:", result)
        self.assertIn("def valid_func", result)


if __name__ == "__main__":
    unittest.main()
