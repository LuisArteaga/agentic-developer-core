import copy
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.state import DEFAULT_STATE, get_state_filepath, load, save


class TestStatePersistence(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for each test
        self.test_dir = tempfile.TemporaryDirectory()
        self.test_dir_path = Path(self.test_dir.name)
        self.state_file = self.test_dir_path / "state.json"
        
        # Suppress logging warnings during tests to keep stdout clean
        logging.getLogger("orchestrator.state").setLevel(logging.CRITICAL)

    def tearDown(self):
        # Clean up the temporary directory
        self.test_dir.cleanup()

    def test_default_state(self):
        """Verify the shape and default values of DEFAULT_STATE."""
        self.assertIsNone(DEFAULT_STATE["issue_number"])
        self.assertEqual(DEFAULT_STATE["status"], "idle")
        self.assertEqual(DEFAULT_STATE["phase"], "")
        self.assertEqual(DEFAULT_STATE["attempts"], {})
        self.assertIsNone(DEFAULT_STATE["branch"])
        self.assertEqual(DEFAULT_STATE["model"], "")
        self.assertIsNone(DEFAULT_STATE["plan"])
        self.assertEqual(DEFAULT_STATE["updated_at"], "")

    def test_save_and_load_normal(self):
        """Verify that state saves and loads correctly under normal circumstances."""
        test_state = copy.deepcopy(DEFAULT_STATE)
        test_state["issue_number"] = 42
        test_state["status"] = "executing"
        test_state["phase"] = "worker_loop"
        test_state["attempts"] = {"verifying": 1}
        test_state["branch"] = "feat/issue-42-test"
        test_state["model"] = "gemini-2.5-pro"
        test_state["plan"] = "1. test\n2. verify"

        save(test_state, self.state_file)

        # The state_file should exist
        self.assertTrue(self.state_file.exists())

        # Load and verify
        loaded_state = load(self.state_file)
        self.assertEqual(loaded_state["issue_number"], 42)
        self.assertEqual(loaded_state["status"], "executing")
        self.assertEqual(loaded_state["phase"], "worker_loop")
        self.assertEqual(loaded_state["attempts"], {"verifying": 1})
        self.assertEqual(loaded_state["branch"], "feat/issue-42-test")
        self.assertEqual(loaded_state["model"], "gemini-2.5-pro")
        self.assertEqual(loaded_state["plan"], "1. test\n2. verify")
        # updated_at should have been populated
        self.assertTrue(loaded_state["updated_at"])

    def test_missing_directory_creation(self):
        """Verify that save() automatically creates the parent directory if it's missing."""
        nested_dir = self.test_dir_path / "nested" / "logs"
        nested_state_file = nested_dir / "state.json"

        test_state = copy.deepcopy(DEFAULT_STATE)
        save(test_state, nested_state_file)

        self.assertTrue(nested_state_file.exists())
        loaded_state = load(nested_state_file)
        self.assertEqual(loaded_state["status"], "idle")

    def test_corrupt_json_fallback(self):
        """Verify that loading corrupted JSON falls back to DEFAULT_STATE."""
        with open(self.state_file, "w", encoding="utf-8") as f:
            f.write("{invalid_json: true")

        loaded_state = load(self.state_file)
        self.assertEqual(loaded_state, DEFAULT_STATE)

    def test_empty_file_fallback(self):
        """Verify that loading an empty file falls back to DEFAULT_STATE."""
        with open(self.state_file, "w", encoding="utf-8") as f:
            f.write("")

        loaded_state = load(self.state_file)
        self.assertEqual(loaded_state, DEFAULT_STATE)

    def test_partial_json_fallback(self):
        """Verify that loading state with missing keys falls back to DEFAULT_STATE."""
        partial_data = {
            "issue_number": 3,
            "status": "planning",
            # missing rest of the keys
        }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(partial_data, f)

        loaded_state = load(self.state_file)
        self.assertEqual(loaded_state, DEFAULT_STATE)

    def test_invalid_status_fallback(self):
        """Verify that loading state with an invalid status string falls back to DEFAULT_STATE."""
        bad_state = copy.deepcopy(DEFAULT_STATE)
        # Using cast/ignore since type system would check this, but simulating raw JSON write
        bad_state["status"] = "unknown_status"  # type: ignore

        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(bad_state, f)

        loaded_state = load(self.state_file)
        self.assertEqual(loaded_state, DEFAULT_STATE)

    def test_invalid_attempts_type_fallback(self):
        """Verify that loading state with attempts not as dict falls back to DEFAULT_STATE."""
        bad_state = copy.deepcopy(DEFAULT_STATE)
        bad_state["attempts"] = "not a dict"  # type: ignore

        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(bad_state, f)

        loaded_state = load(self.state_file)
        self.assertEqual(loaded_state, DEFAULT_STATE)


class TestGetStateFilepath(unittest.TestCase):
    @patch("orchestrator.state.os.access")
    def test_workspace_logs_selected(self, mock_access):
        """Test that /workspace/.agent_logs is selected if it exists and is writable."""
        mock_access.return_value = True
        original_exists = Path.exists
        try:
            Path.exists = lambda self: str(self) == "/workspace/.agent_logs"
            filepath = get_state_filepath()
            self.assertEqual(filepath, Path("/workspace/.agent_logs/state.json"))
        finally:
            Path.exists = original_exists

    @patch("orchestrator.state.os.access")
    @patch("orchestrator.state.Path.mkdir")
    def test_local_logs_selected(self, mock_mkdir, mock_access):
        """Test that project_root/.agent_logs is selected if workspace logs do not exist."""
        mock_access.return_value = True
        original_exists = Path.exists
        try:
            Path.exists = lambda self: False
            filepath = get_state_filepath()
            self.assertTrue(str(filepath).endswith(".agent_logs/state.json"))
            mock_mkdir.assert_called()
        finally:
            Path.exists = original_exists

    @patch("orchestrator.state.os.access")
    @patch("orchestrator.state.Path.mkdir")
    def test_fallback_to_tmp(self, mock_mkdir, mock_access):
        """Test fallback to /tmp/agent_logs if both workspace and local logs are not writable."""
        mock_access.return_value = False
        original_exists = Path.exists
        try:
            Path.exists = lambda self: False
            filepath = get_state_filepath()
            self.assertEqual(filepath, Path("/tmp/agent_logs/state.json"))
        finally:
            Path.exists = original_exists


if __name__ == "__main__":
    unittest.main()
