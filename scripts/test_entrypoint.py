#!/usr/bin/env python3
"""Unit tests for the scripts/entrypoint.py process supervisor."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

# Add scripts directory to path to import entrypoint
scripts_dir = Path(__file__).resolve().parent
if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))

import entrypoint  # noqa: E402


class EntrypointTests(unittest.TestCase):
    def setUp(self):
        # Backup environment
        self.original_env = dict(os.environ)

    def tearDown(self):
        # Restore environment
        os.environ.clear()
        os.environ.update(self.original_env)

    @patch("sys.exit")
    def test_validate_environment_success(self, mock_exit):
        """Should pass if both required env vars are present."""
        os.environ["OPENROUTER_API_KEY"] = "some_key"
        os.environ["GH_PAT"] = "some_token"
        entrypoint.validate_environment()
        mock_exit.assert_not_called()

    @patch("sys.exit")
    def test_validate_environment_missing_keys(self, mock_exit):
        """Should fail fast and call sys.exit(1) if env vars are missing."""
        mock_exit.side_effect = SystemExit
        if "OPENROUTER_API_KEY" in os.environ:
            del os.environ["OPENROUTER_API_KEY"]
        os.environ["GH_PAT"] = "some_token"
        with self.assertRaises(SystemExit):
            entrypoint.validate_environment()
        mock_exit.assert_called_once_with(1)

    @patch("entrypoint.check_url")
    def test_verify_reachability_skipped(self, mock_check_url):
        """Should return immediately if SKIP_REACHABILITY is set."""
        os.environ["SKIP_REACHABILITY"] = "true"
        entrypoint.verify_reachability()
        mock_check_url.assert_not_called()

    @patch("entrypoint.check_url")
    @patch("sys.exit")
    def test_verify_reachability_success(self, mock_exit, mock_check_url):
        """Should pass reachability if all targets check out on first try."""
        if "SKIP_REACHABILITY" in os.environ:
            del os.environ["SKIP_REACHABILITY"]
        mock_check_url.return_value = True
        entrypoint.verify_reachability()
        mock_exit.assert_not_called()
        self.assertEqual(mock_check_url.call_count, 2)

    @patch("time.sleep")
    @patch("entrypoint.check_url")
    @patch("sys.exit")
    def test_verify_reachability_retry_success(
        self, mock_exit, mock_check_url, mock_sleep
    ):
        """Should retry up to 3 times and succeed if a target becomes reachable."""
        if "SKIP_REACHABILITY" in os.environ:
            del os.environ["SKIP_REACHABILITY"]
        # Fail once, then succeed
        mock_check_url.side_effect = [False, True, True]
        entrypoint.verify_reachability()
        mock_exit.assert_not_called()
        mock_sleep.assert_called_once_with(2)

    @patch("time.sleep")
    @patch("entrypoint.check_url")
    @patch("sys.exit")
    def test_verify_reachability_fails(self, mock_exit, mock_check_url, mock_sleep):
        """Should fail and exit 1 if a target fails all 3 reachability attempts."""
        mock_exit.side_effect = SystemExit
        if "SKIP_REACHABILITY" in os.environ:
            del os.environ["SKIP_REACHABILITY"]
        mock_check_url.return_value = False
        with self.assertRaises(SystemExit):
            entrypoint.verify_reachability()
        mock_exit.assert_called_once_with(1)

    @patch("entrypoint.get_state_filepath")
    def test_read_agent_status_defaults_if_no_file(self, mock_get_path):
        """Should default to 'idle' if the state file does not exist."""
        mock_get_path.return_value = Path("non_existent_file.json")
        status = entrypoint.read_agent_status()
        self.assertEqual(status, "idle")

    @patch("entrypoint.get_state_filepath")
    def test_read_agent_status_reads_correctly(self, mock_get_path):
        """Should successfully parse status from state file json."""
        temp_state = Path("temp_state.json")
        mock_get_path.return_value = temp_state
        try:
            with open(temp_state, "w", encoding="utf-8") as f:
                f.write('{"status": "claimed"}')
            status = entrypoint.read_agent_status()
            self.assertEqual(status, "claimed")
        finally:
            if temp_state.exists():
                temp_state.unlink()

    @patch("entrypoint.read_agent_status")
    @patch("subprocess.Popen")
    @patch("time.sleep")
    def test_run_iteration_success_idle(self, mock_sleep, mock_popen, mock_status):
        """Should return no-exit and sleep for poll_interval if success and status is idle."""
        mock_status.return_value = "idle"
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        crashes, should_exit, code = entrypoint.run_iteration(
            consecutive_crashes=0, run_once=False, poll_interval=10.0
        )

        self.assertEqual(crashes, 0)
        self.assertFalse(should_exit)
        self.assertIsNone(code)
        mock_sleep.assert_called_once_with(10.0)

    @patch("entrypoint.read_agent_status")
    @patch("subprocess.Popen")
    @patch("time.sleep")
    def test_run_iteration_success_active(self, mock_sleep, mock_popen, mock_status):
        """Should return no-exit and sleep for 1.0s if success and status is active."""
        mock_status.return_value = "claimed"
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        crashes, should_exit, code = entrypoint.run_iteration(
            consecutive_crashes=0, run_once=False, poll_interval=10.0
        )

        self.assertEqual(crashes, 0)
        self.assertFalse(should_exit)
        self.assertIsNone(code)
        mock_sleep.assert_called_once_with(1.0)

    @patch("subprocess.Popen")
    @patch("time.sleep")
    def test_run_iteration_success_run_once(self, mock_sleep, mock_popen):
        """Should return exit with code 0 and no sleep if success and run_once is enabled."""
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        crashes, should_exit, code = entrypoint.run_iteration(
            consecutive_crashes=0, run_once=True, poll_interval=10.0
        )

        self.assertEqual(crashes, 0)
        self.assertTrue(should_exit)
        self.assertEqual(code, 0)
        mock_sleep.assert_not_called()

    @patch("random.uniform")
    @patch("time.time")
    @patch("subprocess.Popen")
    @patch("time.sleep")
    def test_run_iteration_crash_startup(
        self, mock_sleep, mock_popen, mock_time, mock_random
    ):
        """Should increment crashes, sleep for initial backoff, and return no-exit on startup crash."""
        mock_random.return_value = 0.0  # Jitter = 0
        # Startup crash: process duration < 30 seconds
        mock_time.side_effect = [0.0, 5.0]

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 1
        mock_popen.return_value = mock_proc

        crashes, should_exit, code = entrypoint.run_iteration(
            consecutive_crashes=0, run_once=False, poll_interval=10.0
        )

        self.assertEqual(crashes, 1)
        self.assertFalse(should_exit)
        self.assertIsNone(code)
        mock_sleep.assert_called_once_with(1.0)  # initial_delay (1.0s) + jitter (0.0s)

    @patch("random.uniform")
    @patch("time.time")
    @patch("subprocess.Popen")
    @patch("time.sleep")
    def test_run_iteration_crash_runtime_reset(
        self, mock_sleep, mock_popen, mock_time, mock_random
    ):
        """Should reset consecutive crashes on runtime crash (>30s) and sleep for initial backoff."""
        mock_random.return_value = 0.0
        # Runtime crash: process duration > 30 seconds (40.0s)
        mock_time.side_effect = [0.0, 40.0]

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 1
        mock_popen.return_value = mock_proc

        # Started with 3 consecutive crashes, but duration > 30s resets it to 0, then increments to 1
        crashes, should_exit, code = entrypoint.run_iteration(
            consecutive_crashes=3, run_once=False, poll_interval=10.0
        )

        self.assertEqual(crashes, 1)
        self.assertFalse(should_exit)
        self.assertIsNone(code)
        mock_sleep.assert_called_once_with(1.0)

    @patch("random.uniform")
    @patch("time.time")
    @patch("subprocess.Popen")
    @patch("time.sleep")
    def test_run_iteration_crash_escalating_backoff(
        self, mock_sleep, mock_popen, mock_time, mock_random
    ):
        """Should increment crashes and sleep for escalating exponential backoff."""
        mock_random.return_value = 0.0
        mock_time.side_effect = [0.0, 5.0]

        mock_proc = MagicMock()
        mock_proc.wait.return_value = 1
        mock_popen.return_value = mock_proc

        # Starts with 2 consecutive crashes -> becomes 3 -> delay = 2^(3-1) * 1.0 = 4.0s
        crashes, should_exit, code = entrypoint.run_iteration(
            consecutive_crashes=2, run_once=False, poll_interval=10.0
        )

        self.assertEqual(crashes, 3)
        self.assertFalse(should_exit)
        self.assertIsNone(code)
        mock_sleep.assert_called_once_with(4.0)

    @patch("subprocess.Popen")
    @patch("time.sleep")
    def test_run_iteration_crash_run_once(self, mock_sleep, mock_popen):
        """Should return exit with code 1 and no sleep if crash and run_once is enabled."""
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 1
        mock_popen.return_value = mock_proc

        crashes, should_exit, code = entrypoint.run_iteration(
            consecutive_crashes=0, run_once=True, poll_interval=10.0
        )

        self.assertEqual(crashes, 1)
        self.assertTrue(should_exit)
        self.assertEqual(code, 1)
        mock_sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
