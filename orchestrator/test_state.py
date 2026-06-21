import copy
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
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
        self.assertEqual(DEFAULT_STATE["read_files"], [])
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
        test_state["read_files"] = ["orchestrator/state.py", "main.py"]

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
        self.assertEqual(loaded_state["read_files"], ["orchestrator/state.py", "main.py"])
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

    def test_invalid_read_files_type_fallback(self):
        """Verify that loading state with read_files not as list falls back to DEFAULT_STATE."""
        bad_state = copy.deepcopy(DEFAULT_STATE)
        bad_state["read_files"] = "not a list"  # type: ignore

        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(bad_state, f)

        loaded_state = load(self.state_file)
        self.assertEqual(loaded_state, DEFAULT_STATE)


class TestGetStateFilepath(unittest.TestCase):
    def setUp(self):
        self.original_env = os.environ.get("AGENT_LOG_PATH")

    def tearDown(self):
        if self.original_env is not None:
            os.environ["AGENT_LOG_PATH"] = self.original_env
        elif "AGENT_LOG_PATH" in os.environ:
            del os.environ["AGENT_LOG_PATH"]

    def test_custom_agent_log_path(self):
        """Test that get_state_filepath uses the path from AGENT_LOG_PATH env var when set."""
        os.environ["AGENT_LOG_PATH"] = "/custom/path/to/logs"
        filepath = get_state_filepath()
        self.assertEqual(filepath, Path("/custom/path/to/logs/state.json"))

    def test_relative_agent_log_path(self):
        """Test that relative paths in AGENT_LOG_PATH are resolved against the project root."""
        os.environ["AGENT_LOG_PATH"] = "custom_relative"
        filepath = get_state_filepath()
        project_root = Path(__file__).resolve().parent.parent
        self.assertEqual(filepath, project_root / "custom_relative" / "state.json")

    def test_default_agent_log_path_when_unset(self):
        """Test that get_state_filepath defaults to .agent_logs relative to project root when env var is unset."""
        if "AGENT_LOG_PATH" in os.environ:
            del os.environ["AGENT_LOG_PATH"]
        filepath = get_state_filepath()
        project_root = Path(__file__).resolve().parent.parent
        self.assertEqual(filepath, project_root / ".agent_logs" / "state.json")


if __name__ == "__main__":
    unittest.main()
