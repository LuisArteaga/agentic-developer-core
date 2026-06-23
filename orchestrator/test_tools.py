import copy
import os
import tempfile
import unittest
from pathlib import Path
from orchestrator import state
from orchestrator.tools import read_file, list_directory, grep_search, patch_file

class TestCodebaseTools(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for each test
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = Path(self.temp_dir.name).resolve()
        
        # We will point AGENT_LOG_PATH to this temporary directory so that state file updates write there
        self.original_env = os.environ.get("AGENT_LOG_PATH")
        os.environ["AGENT_LOG_PATH"] = str(self.temp_dir_path / "logs")
        
        # Create a clean state file
        self.state_file_path = state.get_state_filepath()
        self.test_state = copy.deepcopy(state.DEFAULT_STATE)
        state.save(self.test_state, self.state_file_path)

        # Create some test files in the temp directory
        self.text_file = self.temp_dir_path / "hello.txt"
        self.text_file.write_text("line one\nline two\nline three\nline four", encoding="utf-8")
        
        self.empty_file = self.temp_dir_path / "empty.txt"
        self.empty_file.write_text("", encoding="utf-8")
        
        # Binary file with null bytes
        self.binary_file = self.temp_dir_path / "binary.bin"
        self.binary_file.write_bytes(b"hello\0world")
        
        # Non-UTF-8 file (Latin-1 encoded special characters)
        self.non_utf8_file = self.temp_dir_path / "latin1.txt"
        with open(self.non_utf8_file, "wb") as f:
            f.write(b"Hello \xff World")

        # Nested folder structure
        self.sub_dir = self.temp_dir_path / "sub"
        self.sub_dir.mkdir()
        self.nested_file = self.sub_dir / "nested.txt"
        self.nested_file.write_text("another line here\nline two matches", encoding="utf-8")
        
        # Ignored folder
        self.ignored_dir = self.temp_dir_path / ".venv"
        self.ignored_dir.mkdir()
        self.ignored_file = self.ignored_dir / "secret.txt"
        self.ignored_file.write_text("this should not be found by grep", encoding="utf-8")

    def tearDown(self):
        # Restore environment and clean up
        if self.original_env is not None:
            os.environ["AGENT_LOG_PATH"] = self.original_env
        elif "AGENT_LOG_PATH" in os.environ:
            del os.environ["AGENT_LOG_PATH"]
            
        self.temp_dir.cleanup()

    def test_read_file_success_entire(self):
        """Test read_file reads entire text file successfully and registers it in state."""
        content = read_file(str(self.text_file))
        self.assertEqual(content, "line one\nline two\nline three\nline four")
        
        # Check that path was registered in state.json
        loaded = state.load(self.state_file_path)
        resolved_read_files = [str(Path(p).resolve()) for p in loaded["read_files"]]
        self.assertIn(str(self.text_file.resolve()), resolved_read_files)

    def test_read_file_success_slice(self):
        """Test read_file with line boundaries."""
        # start only
        self.assertEqual(read_file(str(self.text_file), start_line=2), "line two\nline three\nline four")
        # end only
        self.assertEqual(read_file(str(self.text_file), end_line=3), "line one\nline two\nline three")
        # both
        self.assertEqual(read_file(str(self.text_file), start_line=2, end_line=3), "line two\nline three")
        # single line
        self.assertEqual(read_file(str(self.text_file), start_line=3, end_line=3), "line three")

    def test_read_file_errors(self):
        """Test error handling in read_file."""
        # Non-existent file
        self.assertIn("does not exist", read_file("nonexistent.txt"))
        # Read directory
        self.assertIn("is a directory", read_file(str(self.temp_dir_path)))
        # Invalid start_line type
        self.assertIn("must be a positive integer", read_file(str(self.text_file), start_line="one")) # type: ignore
        # Negative start_line
        self.assertIn("must be a positive integer", read_file(str(self.text_file), start_line=0))
        self.assertIn("must be a positive integer", read_file(str(self.text_file), start_line=-5))
        # Negative end_line
        self.assertIn("must be a positive integer", read_file(str(self.text_file), end_line=-1))
        # start_line > end_line
        self.assertIn("cannot be greater than", read_file(str(self.text_file), start_line=3, end_line=2))
        # start_line out of bounds
        self.assertIn("exceeds total lines", read_file(str(self.text_file), start_line=10))
        # end_line out of bounds
        self.assertIn("exceeds total lines", read_file(str(self.text_file), end_line=10))
        # Bounds on empty file
        self.assertIn("is empty", read_file(str(self.empty_file), start_line=1))
        # Binary file check
        self.assertIn("is a binary file", read_file(str(self.binary_file)))
        # Encoding error
        self.assertIn("cannot be decoded", read_file(str(self.non_utf8_file)))

    def test_list_directory_success(self):
        """Test list_directory outputs sorted list with classification and sizes."""
        output = list_directory(str(self.temp_dir_path))
        lines = output.splitlines()
        # Directories first
        self.assertTrue(lines[0].startswith("[DIR] .venv"))
        self.assertTrue(lines[1].startswith("[DIR] logs"))
        self.assertTrue(lines[2].startswith("[DIR] sub"))
        # Then files sorted alphabetically
        self.assertEqual(lines[3], f"[FILE] binary.bin ({self.binary_file.stat().st_size} bytes)")
        self.assertEqual(lines[4], f"[FILE] empty.txt ({self.empty_file.stat().st_size} bytes)")
        self.assertEqual(lines[5], f"[FILE] hello.txt ({self.text_file.stat().st_size} bytes)")
        self.assertEqual(lines[6], f"[FILE] latin1.txt ({self.non_utf8_file.stat().st_size} bytes)")

    def test_list_directory_empty(self):
        """Test empty directory returns descriptive string."""
        empty_dir = self.temp_dir_path / "empty_dir"
        empty_dir.mkdir()
        self.assertEqual(list_directory(str(empty_dir)), "(empty directory)")

    def test_list_directory_errors(self):
        """Test list_directory error handling."""
        self.assertIn("does not exist", list_directory("nonexistent_dir"))
        self.assertIn("is a file", list_directory(str(self.text_file)))

    def test_grep_search_success_recursive(self):
        """Test grep_search finds matches in text files recursively, ignoring special folders."""
        # Find "line two" in hello.txt and sub/nested.txt
        output = grep_search("line two", str(self.temp_dir_path))
        # Verify relative path matches
        self.assertTrue(any("hello.txt:2:line two" in line for line in output.splitlines()))
        self.assertTrue(any("nested.txt:2:line two matches" in line for line in output.splitlines()))
        
        # Ensure ignored folder matches are NOT present
        self.assertNotIn("secret.txt", output)

    def test_grep_search_success_single_file(self):
        """Test grep_search matches inside a single file."""
        output = grep_search("line three", str(self.text_file))
        self.assertTrue(any("hello.txt:3:line three" in line for line in output.splitlines()))

    def test_grep_search_no_matches(self):
        """Test grep_search output when no matches are found."""
        output = grep_search("xyzabc123", str(self.temp_dir_path))
        self.assertIn("No matches found", output)

    def test_grep_search_errors(self):
        """Test grep_search path validation."""
        self.assertIn("does not exist", grep_search("test", "nonexistent_path"))

    def test_patch_file_success(self):
        """Test successful patch_file execution after reading the file first."""
        # 1. Read the file first to register it in state
        read_file(str(self.text_file))
        
        # 2. Patch a unique line
        res = patch_file(str(self.text_file), "line two", "line modified")
        self.assertIn("patched successfully", res)
        
        # 3. Read it again to verify content
        updated_content = read_file(str(self.text_file))
        self.assertEqual(updated_content, "line one\nline modified\nline three\nline four")

    def test_patch_file_read_before_edit_violation(self):
        """Test that patch_file aborts if the file was not read in the current cycle."""
        # Do NOT call read_file
        res = patch_file(str(self.text_file), "line two", "line modified")
        self.assertIn("Read-Before-Edit validation failed", res)

    def test_patch_file_zero_matches(self):
        """Test that patch_file aborts if target_content is not found."""
        read_file(str(self.text_file))
        res = patch_file(str(self.text_file), "nonexistent line", "replacement")
        self.assertIn("was not found", res)

    def test_patch_file_multiple_matches(self):
        """Test that patch_file aborts if target_content is ambiguous (multiple occurrences)."""
        # Create a file with duplicate lines
        dup_file = self.temp_dir_path / "duplicate.txt"
        dup_file.write_text("duplicate\nsome other text\nduplicate", encoding="utf-8")
        
        read_file(str(dup_file))
        res = patch_file(str(dup_file), "duplicate", "single replacement")
        self.assertIn("matches multiple times", res)

    def test_patch_file_binary(self):
        """Test that patch_file aborts when targeting a binary file."""
        # Hand-register the binary file to bypass Read-Before-Edit check
        loaded = state.load(self.state_file_path)
        loaded["read_files"].append(str(self.binary_file.resolve()))
        state.save(loaded, self.state_file_path)
        
        res = patch_file(str(self.binary_file), "hello", "world")
        self.assertIn("is a binary file", res)

    def test_patch_file_nonexistent(self):
        """Test that patch_file aborts for nonexistent files even if registered in state."""
        # Hand-register a nonexistent file to bypass Read-Before-Edit check
        loaded = state.load(self.state_file_path)
        loaded["read_files"].append("ghost.txt")
        state.save(loaded, self.state_file_path)
        
        res = patch_file("ghost.txt", "something", "else")
        self.assertIn("does not exist", res)

if __name__ == "__main__":
    unittest.main()
