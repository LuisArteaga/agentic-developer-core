import copy
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from orchestrator import state, tools
from orchestrator.tools import (
    grep_search,
    list_directory,
    patch_file,
    read_file,
    run_command,
)


class TestCodebaseTools(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for each test
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_dir_path = Path(self.temp_dir.name).resolve()

        # Override project root for safety checks in tests
        tools._PROJECT_ROOT = self.temp_dir_path

        # We will point AGENT_LOG_PATH to this temporary directory so that state file updates write there
        self.original_env = os.environ.get("AGENT_LOG_PATH")
        os.environ["AGENT_LOG_PATH"] = str(self.temp_dir_path / "logs")

        # Create a clean state file
        self.state_file_path = state.get_state_filepath()
        self.test_state = copy.deepcopy(state.DEFAULT_STATE)
        state.save(self.test_state, self.state_file_path)

        # Create some test files in the temp directory
        self.text_file = self.temp_dir_path / "hello.txt"
        self.text_file.write_text(
            "line one\nline two\nline three\nline four", encoding="utf-8"
        )

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
        self.nested_file.write_text(
            "another line here\nline two matches", encoding="utf-8"
        )

        # Ignored folder
        self.ignored_dir = self.temp_dir_path / ".venv"
        self.ignored_dir.mkdir()
        self.ignored_file = self.ignored_dir / "secret.txt"
        self.ignored_file.write_text(
            "this should not be found by grep", encoding="utf-8"
        )

    def tearDown(self):
        # Restore project root and environment, and clean up
        tools._PROJECT_ROOT = None
        if self.original_env is not None:
            os.environ["AGENT_LOG_PATH"] = self.original_env
        elif "AGENT_LOG_PATH" in os.environ:
            del os.environ["AGENT_LOG_PATH"]

        self.temp_dir.cleanup()

    def test_read_file_success_entire(self):
        """Test read_file reads entire text file successfully and registers it in state."""
        content = read_file(str(self.text_file))
        self.assertEqual(content, "line one\nline two\nline three\nline four")

        # Check that the full-file read range was registered in state.json
        loaded = state.load(self.state_file_path)
        _, rel_str = tools._normalize_path(str(self.text_file))
        self.assertIn(rel_str, loaded["read_files"])
        # Full read (no bounds) authorizes the whole file: [1, total_lines]
        self.assertEqual(loaded["read_files"][rel_str], [[1, 4]])

    def test_read_file_success_slice(self):
        """Test read_file with line boundaries."""
        # start only
        self.assertEqual(
            read_file(str(self.text_file), start_line=2),
            "line two\nline three\nline four",
        )
        # end only
        self.assertEqual(
            read_file(str(self.text_file), end_line=3), "line one\nline two\nline three"
        )
        # both
        self.assertEqual(
            read_file(str(self.text_file), start_line=2, end_line=3),
            "line two\nline three",
        )
        # single line
        self.assertEqual(
            read_file(str(self.text_file), start_line=3, end_line=3), "line three"
        )

    def test_read_file_errors(self):
        """Test error handling in read_file."""
        # Non-existent file
        self.assertIn("does not exist", read_file("nonexistent.txt"))
        # Read directory
        self.assertIn("is a directory", read_file(str(self.temp_dir_path)))
        # Invalid start_line (non-positive value is rejected)
        self.assertIn(
            "must be a positive integer",
            read_file(str(self.text_file), start_line=-1),
        )
        # Negative start_line
        self.assertIn(
            "must be a positive integer", read_file(str(self.text_file), start_line=0)
        )
        self.assertIn(
            "must be a positive integer", read_file(str(self.text_file), start_line=-5)
        )
        # Negative end_line
        self.assertIn(
            "must be a positive integer", read_file(str(self.text_file), end_line=-1)
        )
        # start_line > end_line
        self.assertIn(
            "cannot be greater than",
            read_file(str(self.text_file), start_line=3, end_line=2),
        )
        # start_line out of bounds
        self.assertIn(
            "exceeds total lines", read_file(str(self.text_file), start_line=10)
        )
        # end_line out of bounds
        self.assertIn(
            "exceeds total lines", read_file(str(self.text_file), end_line=10)
        )
        # Bounds on empty file
        self.assertIn("is empty", read_file(str(self.empty_file), start_line=1))
        # Binary file check
        self.assertIn("is a binary file", read_file(str(self.binary_file)))
        # Encoding error
        self.assertIn("cannot be decoded", read_file(str(self.non_utf8_file)))

    def test_read_file_blocked_sensitive_filename(self):
        """Runtime path-safety blocks reading sensitive files (e.g. .env) even when they exist."""
        env_file = self.temp_dir_path / ".env"
        env_file.write_text("SECRET=leak", encoding="utf-8")
        res = read_file(str(env_file))
        self.assertIn("blocked by the path-safety policy", res)
        # Content must not be returned
        self.assertNotIn("leak", res)
        # The blocked path must NOT be registered in read_files (closes the
        # read-then-patch bypass: no membership gained from a blocked read).
        loaded = state.load(self.state_file_path)
        _, rel_str = tools._normalize_path(str(env_file))
        self.assertNotIn(rel_str, loaded["read_files"])

    def test_read_file_blocked_sensitive_directory(self):
        """Runtime path-safety blocks reading files inside forbidden directories (.git)."""
        git_dir = self.temp_dir_path / ".git"
        git_dir.mkdir()
        cfg = git_dir / "config"
        cfg.write_text("contents", encoding="utf-8")
        res = read_file(str(cfg))
        self.assertIn("blocked by the path-safety policy", res)

    def test_read_file_blocked_sensitive_extension(self):
        """Runtime path-safety blocks reading certificate/key files (.pem, .key)."""
        for name in ("token.pem", "private.key", "cert.pfx"):
            f = self.temp_dir_path / name
            f.write_text("secret material", encoding="utf-8")
            self.assertIn(
                "blocked by the path-safety policy", read_file(str(f)), msg=name
            )

    def test_read_file_safe_path_unaffected(self):
        """Legitimate (non-sensitive) absolute paths within the project root still read fine."""
        # Absolute path to a normal file must still work after the safety gate.
        self.assertEqual(
            read_file(str(self.text_file)),
            "line one\nline two\nline three\nline four",
        )

    def test_list_directory_success(self):
        """Test list_directory outputs sorted list with classification and sizes."""
        output = list_directory(str(self.temp_dir_path))
        lines = output.splitlines()
        # Directories first
        self.assertTrue(lines[0].startswith("[DIR] .venv"))
        self.assertTrue(lines[1].startswith("[DIR] logs"))
        self.assertTrue(lines[2].startswith("[DIR] sub"))
        # Then files sorted alphabetically
        self.assertEqual(
            lines[3], f"[FILE] binary.bin ({self.binary_file.stat().st_size} bytes)"
        )
        self.assertEqual(
            lines[4], f"[FILE] empty.txt ({self.empty_file.stat().st_size} bytes)"
        )
        self.assertEqual(
            lines[5], f"[FILE] hello.txt ({self.text_file.stat().st_size} bytes)"
        )
        self.assertEqual(
            lines[6], f"[FILE] latin1.txt ({self.non_utf8_file.stat().st_size} bytes)"
        )

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
        self.assertTrue(
            any("hello.txt:2:line two" in line for line in output.splitlines())
        )
        self.assertTrue(
            any("nested.txt:2:line two matches" in line for line in output.splitlines())
        )

        # Ensure ignored folder matches are NOT present
        self.assertNotIn("secret.txt", output)

    def test_grep_search_success_single_file(self):
        """Test grep_search matches inside a single file."""
        output = grep_search("line three", str(self.text_file))
        self.assertTrue(
            any("hello.txt:3:line three" in line for line in output.splitlines())
        )

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
        self.assertEqual(
            updated_content, "line one\nline modified\nline three\nline four"
        )

    def test_patch_file_read_before_edit_violation(self):
        """Test that patch_file aborts if the file was not read in the current cycle."""
        # Do NOT call read_file
        res = patch_file(str(self.text_file), "line two", "line modified")
        self.assertIn("Read-Before-Edit validation failed", res)

    def test_patch_file_blocked_sensitive_path_even_after_read(self):
        """Runtime path-safety blocks patching sensitive files even when hand-registered in read_files.

        Proves the safety gate runs BEFORE the Read-Before-Edit check, so read_files
        membership (the exact bypass described in issue #60) cannot override the block.
        """
        env_file = self.temp_dir_path / ".env"
        env_file.write_text("SECRET=leak", encoding="utf-8")
        # Hand-register the blocked path to simulate the agent having "read" it,
        # bypassing read_file's own safety gate.
        loaded = state.load(self.state_file_path)
        _, rel_str = tools._normalize_path(str(env_file))
        loaded["read_files"][rel_str] = [[1, 100]]
        state.save(loaded, self.state_file_path)

        res = patch_file(str(env_file), "SECRET=leak", "SECRET=pwned")
        self.assertIn("blocked by the path-safety policy", res)
        # The file must be untouched.
        self.assertEqual(env_file.read_text(encoding="utf-8"), "SECRET=leak")

    def test_patch_file_blocked_sensitive_path_after_attempted_read(self):
        """End-to-end: read_file refuses a sensitive file, so patch_file has no membership and is blocked twice over."""
        env_file = self.temp_dir_path / ".env"
        env_file.write_text("SECRET=leak", encoding="utf-8")
        # read_file itself blocks the sensitive path -> no read_files membership
        self.assertIn("blocked by the path-safety policy", read_file(str(env_file)))
        # patch_file is blocked regardless (path-safety first), not the read error
        res = patch_file(str(env_file), "SECRET=leak", "SECRET=pwned")
        self.assertIn("blocked by the path-safety policy", res)
        self.assertEqual(env_file.read_text(encoding="utf-8"), "SECRET=leak")

    def test_patch_file_blocked_sensitive_directory(self):
        """Runtime path-safety blocks patching files in forbidden directories (.git)."""
        git_dir = self.temp_dir_path / ".git"
        git_dir.mkdir()
        cfg = git_dir / "config"
        cfg.write_text("[core]", encoding="utf-8")
        res = patch_file(str(cfg), "[core]", "[core]\nmalicious = true")
        self.assertIn("blocked by the path-safety policy", res)
        self.assertEqual(cfg.read_text(encoding="utf-8"), "[core]")

    def test_patch_file_safe_path_unaffected(self):
        """Legitimate absolute paths within the project root still patch after the safety gate."""
        read_file(str(self.text_file))
        res = patch_file(str(self.text_file), "line two", "line modified")
        self.assertIn("patched successfully", res)
        self.assertIn("line modified", self.text_file.read_text(encoding="utf-8"))

    def test_patch_file_zero_matches(self):
        """Test that patch_file aborts if old_string is not found."""
        read_file(str(self.text_file))
        res = patch_file(str(self.text_file), "nonexistent line", "replacement")
        self.assertIn("was not found", res)

    def test_patch_file_multiple_matches(self):
        """Test that patch_file aborts if old_string is ambiguous (multiple occurrences)."""
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
        _, rel_str = tools._normalize_path(str(self.binary_file))
        loaded["read_files"][rel_str] = [[1, 100]]
        state.save(loaded, self.state_file_path)

        res = patch_file(str(self.binary_file), "hello", "world")
        self.assertIn("is a binary file", res)

    def test_patch_file_nonexistent(self):
        """Test that patch_file aborts for nonexistent files even if registered in state."""
        # Hand-register a nonexistent file to bypass Read-Before-Edit check
        loaded = state.load(self.state_file_path)
        loaded["read_files"]["ghost.txt"] = [[1, 100]]
        state.save(loaded, self.state_file_path)

        res = patch_file("ghost.txt", "something", "else")
        self.assertIn("does not exist", res)

    def test_read_file_partial_registers_partial_range(self):
        """A partial read registers only the requested line range, not the whole file."""
        read_file(str(self.text_file), start_line=2, end_line=3)
        loaded = state.load(self.state_file_path)
        _, rel_str = tools._normalize_path(str(self.text_file))
        self.assertEqual(loaded["read_files"][rel_str], [[2, 3]])

    def test_patch_file_full_read_authorizes_anywhere(self):
        """Reading the whole file authorizes a patch on any line of it."""
        read_file(str(self.text_file))  # full read -> [[1, 4]]
        res = patch_file(str(self.text_file), "line four", "line four edited")
        self.assertIn("patched successfully", res)

    def test_patch_file_range_scoped_within_read_range(self):
        """A patch whose old_string lies within a partial read range succeeds."""
        read_file(
            str(self.text_file), start_line=2, end_line=3
        )  # reads "line two\nline three"
        res = patch_file(str(self.text_file), "line three", "line three edited")
        self.assertIn("patched successfully", res)

    def test_patch_file_range_scoped_outside_read_range_blocked(self):
        """A patch whose old_string lies outside the partial read range is rejected."""
        read_file(
            str(self.text_file), start_line=2, end_line=3
        )  # only lines 2-3 authorized
        res = patch_file(str(self.text_file), "line four", "line four edited")
        self.assertIn("range validation failed", res)
        self.assertIn("lines 4-4", res)

    def test_patch_file_range_scoped_straddling_read_boundary_blocked(self):
        """An edit spanning the edge of a read range (partly unread) is rejected."""
        read_file(str(self.text_file), start_line=1, end_line=2)  # lines 1-2 authorized
        # old_string spans lines 2-3 (partly outside the read range)
        res = patch_file(str(self.text_file), "line two\nline three", "x\ny")
        self.assertIn("range validation failed", res)

    def test_patch_file_range_recompute_shifts_below_ranges(self):
        """After a patch that inserts lines, ranges below the edit shift so a later edit
        in the shifted region succeeds without a fresh read; the edited region itself
        requires a re-read."""
        big_file = self.temp_dir_path / "big.txt"
        big_file.write_text(
            "\n".join(f"line_{i:02d}" for i in range(1, 21)), encoding="utf-8"
        )  # 20 lines, zero-padded so each old_string is unique
        # Read the whole file -> [[1, 20]]
        read_file(str(big_file))

        # Patch line 2 -> insert 3 lines (delta = +2). "line_02" is unique.
        res = patch_file(str(big_file), "line_02", "line_2a\nline_2b\nline_2c")
        self.assertIn("patched successfully", res)

        # The edited region (old line 2) was rewritten -> re-read required there.
        # Lines below shifted by +2: old line 5 ("line_05") is now at line 7. Editing it
        # should succeed because the shifted range still covers it (no fresh read needed).
        res = patch_file(str(big_file), "line_05", "line_five")
        self.assertIn("patched successfully", res)

        # The edited region (now "line_2a"/"line_2b"/"line_2c") is NOT authorized: the
        # original line-2 span was dropped from the ranges during recomputation.
        res = patch_file(str(big_file), "line_2b", "line_2B")
        self.assertIn("range validation failed", res)

    def test_patch_file_range_recompute_shrink_preserves_above_range(self):
        """After a patch that removes lines, an above-range edit still works and a
        below-range edit works at the shifted location."""
        big_file = self.temp_dir_path / "big2.txt"
        big_file.write_text(
            "\n".join(f"line_{i:02d}" for i in range(1, 21)), encoding="utf-8"
        )
        read_file(str(big_file))  # [[1, 20]]

        # Remove lines 10-12 (3 lines) -> replace with one line (delta = -2).
        res = patch_file(str(big_file), "line_10\nline_11\nline_12", "merged")
        self.assertIn("patched successfully", res)

        # Above the edit: line 1 unchanged location -> still authorized.
        res = patch_file(str(big_file), "line_01", "line_one")
        self.assertIn("patched successfully", res)

        # Below the edit: old line 20 shifted to line 18. "line_20" still exists as text,
        # now at line 18, covered by the shifted range -> authorized.
        res = patch_file(str(big_file), "line_20", "line_twenty")
        self.assertIn("patched successfully", res)

    def test_patch_file_range_recompute_overlapping_splits(self):
        """A full-file range overlapping the edit is split: the edited region is dropped,
        while the untouched above/below portions remain authorized."""
        big_file = self.temp_dir_path / "big3.txt"
        big_file.write_text(
            "\n".join(f"line_{i:02d}" for i in range(1, 11)), encoding="utf-8"
        )  # 10 lines
        read_file(str(big_file))  # [[1, 10]]

        # Edit line 5 (single line, delta 0).
        res = patch_file(str(big_file), "line_05", "line_five")
        self.assertIn("patched successfully", res)

        # Lines 1-4 and 6-10 should remain authorized (split), only line 5 dropped.
        res = patch_file(str(big_file), "line_01", "L01")
        self.assertIn("patched successfully", res)
        res = patch_file(str(big_file), "line_10", "L10")
        self.assertIn("patched successfully", res)

        # The rewritten region (line 5, now "line_five") is not authorized.
        res = patch_file(str(big_file), "line_five", "LINE_FIVE")
        self.assertIn("range validation failed", res)

    def test_patch_file_range_error_names_read_ranges(self):
        """The range validation error reports the currently-read ranges for self-correction."""
        read_file(str(self.text_file), start_line=1, end_line=2)
        res = patch_file(str(self.text_file), "line four", "x")
        self.assertIn("[1-2]", res)
        self.assertIn("read_file", res)

    def test_read_file_repeated_partial_reads_merge_ranges(self):
        """Overlapping/adjacent partial reads merge into a single canonical range."""
        read_file(str(self.text_file), start_line=1, end_line=2)
        read_file(str(self.text_file), start_line=2, end_line=4)
        loaded = state.load(self.state_file_path)
        _, rel_str = tools._normalize_path(str(self.text_file))
        self.assertEqual(loaded["read_files"][rel_str], [[1, 4]])

    def test_patch_file_empty_file_read_reports_not_found_not_unread(self):
        """A read empty file is a known path; patching it fails at uniqueness (not-found),
        not at the path-level 'has not been read' gate."""
        read_file(str(self.empty_file))  # registers [] ranges for the path
        res = patch_file(str(self.empty_file), "anything", "something")
        # Path was read, so we get past the path gate; uniqueness then reports not-found.
        self.assertIn("was not found", res)
        self.assertNotIn("has not been read", res)

    def test_read_file_state_save_failure_is_swallowed(self):
        """A failing state.save during read_file registration is swallowed (read still succeeds)."""
        with unittest.mock.patch(
            "orchestrator.tools.state.save", side_effect=RuntimeError("boom")
        ):
            content = read_file(str(self.text_file))
        self.assertEqual(content, "line one\nline two\nline three\nline four")

    def test_read_file_generic_read_failure(self):
        """A generic OS-level read failure surfaces a 'Failed to read file' error."""
        with unittest.mock.patch("builtins.open", side_effect=OSError("boom")):
            res = read_file(str(self.text_file))
        self.assertIn("Failed to read file", res)

    def test_patch_file_state_load_failure_reports_unread(self):
        """If state.load fails in patch_file, the file is treated as unread."""
        with unittest.mock.patch(
            "orchestrator.tools.state.load", side_effect=RuntimeError("boom")
        ):
            res = patch_file(str(self.text_file), "line two", "x")
        self.assertIn("has not been read", res)

    def test_patch_file_target_is_directory(self):
        """patch_file on a path that is a directory (but registered) reports 'is a directory'."""
        loaded = state.load(self.state_file_path)
        _, rel_str = tools._normalize_path(str(self.temp_dir_path))
        loaded["read_files"][rel_str] = [[1, 10]]
        state.save(loaded, self.state_file_path)

        res = patch_file(str(self.temp_dir_path), "anything", "x")
        self.assertIn("is a directory, not a file", res)

    def test_patch_file_non_utf8_registered(self):
        """patch_file on a registered non-UTF-8 file reports a decode error (not unread)."""
        loaded = state.load(self.state_file_path)
        _, rel_str = tools._normalize_path(str(self.non_utf8_file))
        loaded["read_files"][rel_str] = [[1, 10]]
        state.save(loaded, self.state_file_path)

        res = patch_file(str(self.non_utf8_file), "Hello", "Hi")
        self.assertIn("cannot be decoded", res)

    def test_patch_file_generic_read_failure(self):
        """A generic OS-level read failure in patch_file's read block surfaces an error."""
        loaded = state.load(self.state_file_path)
        _, rel_str = tools._normalize_path(str(self.text_file))
        loaded["read_files"][rel_str] = [[1, 10]]
        state.save(loaded, self.state_file_path)

        real_open = open
        target = str(self.text_file)

        def selective_open(path, *a, **k):
            if str(path) == target:
                raise OSError("boom")
            return real_open(path, *a, **k)

        with unittest.mock.patch("builtins.open", side_effect=selective_open):
            res = patch_file(str(self.text_file), "line two", "x")
        self.assertIn("Failed to read file", res)

    def test_patch_file_write_failure(self):
        """A write failure (e.g. read-only file) surfaces a 'Failed to write' error."""
        read_file(str(self.text_file))
        os.chmod(self.text_file, 0o444)  # read-only
        try:
            res = patch_file(str(self.text_file), "line one", "changed")
            # If running as root chmod is ignored; assert accordingly
            if os.geteuid() == 0:
                self.assertIn("patched successfully", res)
            else:
                self.assertIn("Failed to write to file", res)
        finally:
            os.chmod(self.text_file, 0o644)

    def test_patch_file_recompute_state_save_failure_is_swallowed(self):
        """If state.save fails during the post-edit range recompute, the patch still succeeds."""
        read_file(str(self.text_file))
        with unittest.mock.patch(
            "orchestrator.tools.state.save", side_effect=RuntimeError("boom")
        ):
            res = patch_file(str(self.text_file), "line two", "line modified")
        self.assertIn("patched successfully", res)

    def test_path_traversal_protection(self):
        """Test that paths outside the project root are rejected with Access denied."""
        res = read_file("/etc/passwd")
        self.assertIn("Access denied", res)

        res = list_directory("/etc")
        self.assertIn("Access denied", res)

        res = grep_search("root", "/etc/passwd")
        self.assertIn("Access denied", res)

        res = patch_file("/etc/passwd", "root", "toot")
        self.assertIn("Access denied", res)

    def test_run_command_success(self):
        """Test that run_command executes a simple python command successfully."""
        res = run_command("python3 -c \"print('hello')\"")
        self.assertEqual(res.strip(), "hello")

    def test_run_command_empty_output(self):
        """Test that run_command handles commands with empty output correctly."""
        res = run_command('python3 -c "pass"')
        self.assertEqual(res, "(Command completed successfully with no output.)")

    def test_run_command_parse_error(self):
        """Test that run_command handles malformed command strings gracefully."""
        res = run_command("echo 'unmatched quote")
        self.assertIn("Failed to parse command string", res)

    @unittest.mock.patch("subprocess.run")
    def test_run_command_timeout(self, mock_run):
        """Test that run_command catches subprocess.TimeoutExpired and returns partial output."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["echo", "hi"], timeout=300, output=b"partial execution output"
        )
        res = run_command("echo hi")
        self.assertIn("timed out after 300 seconds", res)
        self.assertIn("partial execution output", res)

    @unittest.mock.patch("subprocess.run")
    def test_run_command_truncation_lines(self, mock_run):
        """Test that run_command truncates output exceeding 150 lines (first 30 + last 100)."""
        import subprocess

        mock_stdout = b"\n".join([f"line {i}".encode() for i in range(1, 201)])
        mock_run.return_value = subprocess.CompletedProcess(
            args=["echo"], returncode=0, stdout=mock_stdout
        )
        res = run_command("echo hi")
        # Unified truncation (ADR-0037 #5): 150-line cap with a first-30 +
        # last-100 window. The old "<= 130 lines" special case is gone.
        self.assertIn("Output truncated", res)
        self.assertIn("exceeded 150 lines", res)
        lines = res.splitlines()
        self.assertEqual(lines[0], "line 1")
        self.assertEqual(lines[29], "line 30")
        self.assertEqual(lines[-1], "line 200")
        self.assertEqual(lines[-100], "line 101")

    @unittest.mock.patch("subprocess.run")
    def test_run_command_truncation_bytes(self, mock_run):
        """Test that run_command truncates output exceeding 10 KB via the byte cap."""
        import subprocess

        # 50 lines of 300 bytes each = 15000 bytes (> 10 KB, but <= 150 lines).
        # Under the unified rule the byte cap applies regardless of line count.
        long_line = b"a" * 300
        mock_stdout = b"\n".join([long_line for _ in range(50)])
        mock_run.return_value = subprocess.CompletedProcess(
            args=["echo"], returncode=0, stdout=mock_stdout
        )
        res = run_command("echo hi")
        self.assertIn("Output truncated", res)
        self.assertIn("exceeded 10 KB limit", res)
        lines = res.splitlines()
        # First full line survives the byte truncation intact.
        self.assertEqual(lines[0], "a" * 300)
        # Result is bounded well below the original 15000 bytes.
        self.assertLess(len(res), 15000)

    @unittest.mock.patch("subprocess.run")
    def test_run_command_non_utf8(self, mock_run):
        """Test that run_command gracefully decodes non-UTF-8 outputs using errors='replace'."""
        import subprocess

        mock_run.return_value = subprocess.CompletedProcess(
            args=["echo"], returncode=0, stdout=b"hello \xff world"
        )
        res = run_command("echo hi")
        self.assertEqual(res, "hello \ufffd world")

    # ------------------------------------------------------------------
    # ADR-0037 layer 1: run_command allowlist + secret-stripped env
    # ------------------------------------------------------------------

    def test_run_command_rejects_disallowed_binary(self):
        """curl (a network binary) is not in the allowlist and is rejected."""
        res = run_command("curl http://evil.example/")
        self.assertIn("not in the run_command allowlist", res)
        self.assertIn("curl", res)
        self.assertIn("fetch_url", res)

    def test_run_command_rejects_unknown_binary(self):
        res = run_command("npm install")
        self.assertIn("not in the run_command allowlist", res)
        self.assertIn("npm", res)

    def test_run_command_rejects_file_reading_binaries(self):
        """cat/ls are excluded from the allowlist: they read arbitrary paths
        with no is_safe_path check and would bypass ADR-0037 layer 2 (a
        prompt-injected Worker could `cat .env` / `ls .git`)."""
        for binary in ("cat", "ls"):
            res = run_command(f"{binary} .env")
            self.assertIn("not in the run_command allowlist", res)
            self.assertIn(binary, res)

    def test_get_workspace_root_honors_env_when_project_root_unset(self):
        """When _PROJECT_ROOT is None, get_workspace_root resolves GITHUB_WORKSPACE
        (relative to the orchestrator package, or absolute)."""
        from orchestrator import tools as _tools

        original_root = _tools._PROJECT_ROOT
        _tools._PROJECT_ROOT = None
        original_ws = os.environ.get("GITHUB_WORKSPACE")
        try:
            # Absolute path is returned resolved.
            os.environ["GITHUB_WORKSPACE"] = str(self.temp_dir_path)
            self.assertEqual(_tools.get_workspace_root(), self.temp_dir_path)
        finally:
            _tools._PROJECT_ROOT = original_root
            if original_ws is not None:
                os.environ["GITHUB_WORKSPACE"] = original_ws
            else:
                os.environ.pop("GITHUB_WORKSPACE", None)

    def test_run_command_allowlist_accepts_curated_binary(self):
        """An allowlisted binary actually executes."""
        res = run_command("echo allowed")
        self.assertEqual(res.strip(), "allowed")

    def test_run_command_allowlist_override(self):
        """AGENT_RUN_COMMAND_ALLOWLIST overrides the default set."""
        os.environ["AGENT_RUN_COMMAND_ALLOWLIST"] = "echo,cat"
        try:
            res = run_command("echo via-override")
            self.assertEqual(res.strip(), "via-override")
        finally:
            del os.environ["AGENT_RUN_COMMAND_ALLOWLIST"]

    @unittest.mock.patch("subprocess.run")
    def test_run_command_env_strips_secrets(self, mock_run):
        """The child env drops named secrets and *_KEY/*_TOKEN vars."""
        mock_run.return_value = unittest.mock.MagicMock(stdout=b"", returncode=0)
        os.environ["GH_PAT"] = "ghp_" + "a" * 36
        os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-" + "b" * 24
        os.environ["MY_DB_PASSWORD"] = "hunter2"
        os.environ["KEEP_ME"] = "kept"
        try:
            run_command("echo hi")
        finally:
            for k in ("GH_PAT", "OPENROUTER_API_KEY", "MY_DB_PASSWORD", "KEEP_ME"):
                os.environ.pop(k, None)
        _, kwargs = mock_run.call_args
        child_env = kwargs["env"]
        self.assertNotIn("GH_PAT", child_env)
        self.assertNotIn("OPENROUTER_API_KEY", child_env)
        self.assertNotIn("MY_DB_PASSWORD", child_env)
        # Non-secret vars survive.
        self.assertEqual(child_env["KEEP_ME"], "kept")
        # PATH is preserved so binaries remain resolvable.
        self.assertIn("PATH", child_env)

    # ------------------------------------------------------------------
    # ADR-0037 layer 2: is_safe_path on grep_search / list_directory
    # ------------------------------------------------------------------

    def test_grep_search_blocked_sensitive_file(self):
        """grep_search(query, path='.env') returns the policy error, not contents."""
        # .env does not need to exist — the safety check precedes existence.
        res = grep_search("OPENROUTER", ".env")
        self.assertIn("blocked by the path-safety policy", res)

    def test_list_directory_blocked_sensitive_dir(self):
        """list_directory(path='.git') returns the policy error, not contents."""
        res = list_directory(".git")
        self.assertIn("blocked by the path-safety policy", res)

    # ------------------------------------------------------------------
    # ADR-0037 layer 6: bounded reads
    # ------------------------------------------------------------------

    def test_read_file_rejects_oversized_file(self):
        """A file exceeding the byte cap returns a bounded error, not contents."""
        from orchestrator.tools import MAX_READ_FILE_BYTES

        big = self.temp_dir_path / "huge.txt"
        big.write_bytes(b"a" * (MAX_READ_FILE_BYTES + 10))
        res = read_file(str(big))
        self.assertIn("exceeds the", res)
        self.assertIn("read cap", res)

    def test_grep_search_caps_match_count(self):
        """grep_search stops at the match cap and appends a truncation note."""
        from orchestrator.tools import MAX_GREP_MATCHES

        many = self.temp_dir_path / "many.txt"
        many.write_text("\n".join("needle" for _ in range(MAX_GREP_MATCHES + 50)))
        res = grep_search("needle", str(many))
        self.assertIn("Results truncated", res)
        self.assertIn(f"{MAX_GREP_MATCHES}-match cap", res)

    # ------------------------------------------------------------------
    # ADR-0037 run_command edge cases (coverage gaps cited by test_coverage judge)
    # ------------------------------------------------------------------

    def test_run_command_empty_string(self):
        """An empty command string yields the empty-command error."""
        res = run_command("")
        self.assertIn("Empty command", res)

    @unittest.mock.patch("subprocess.run")
    def test_run_command_generic_exception(self, mock_run):
        """A non-timeout exception from subprocess.run is caught and reported."""
        mock_run.side_effect = OSError("spawn failed")
        res = run_command("echo hi")
        self.assertIn("Failed to run command", res)
        self.assertIn("spawn failed", res)

    @unittest.mock.patch("subprocess.run")
    def test_run_command_timeout_with_no_output(self, mock_run):
        """A timeout with no captured output returns the no-output timeout error."""
        import subprocess

        mock_run.side_effect = subprocess.TimeoutExpired(
            cmd=["echo", "hi"], timeout=300, output=b""
        )
        res = run_command("echo hi")
        self.assertIn("timed out after 300 seconds with no output", res)


if __name__ == "__main__":
    unittest.main()
