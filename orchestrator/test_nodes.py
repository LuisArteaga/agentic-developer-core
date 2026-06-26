import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from orchestrator import state as state_module
from orchestrator.state import AgentState, DEFAULT_STATE
from orchestrator.nodes import claim_node

class TestClaimNode(unittest.TestCase):
    def setUp(self):
        # Create temp directory for workspace
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        
        # Create temp directory for state logs
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()
        
        # Set environment variables for testing
        self.original_env = {}
        vars_to_set = {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "AGENT_LABEL_READY": "agent-ready",
            "AGENT_LABEL_IN_PROGRESS": "agent-in-progress",
            "AGENT_LABEL_BLOCKED": "agent-blocked",
            "AGENT_MODE": "local"
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v
            
        # Initialize a real Git repository in the temp workspace
        subprocess.run(["git", "init", "-b", "main"], cwd=str(self.workspace_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.workspace_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.workspace_dir), check=True)
        
        # Add an initial commit so HEAD exists
        self.initial_file = self.workspace_dir / "README.md"
        self.initial_file.write_text("# Test Repo", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=str(self.workspace_dir), check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(self.workspace_dir), check=True)

    def tearDown(self):
        # Restore environment variables
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
                
        # Clean up directories
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()
        
        # Reset state module internal cache/path if any (resolved dynamically, so no need)

    @patch("orchestrator.nodes._run_gh")
    def test_normal_claim_flow(self, mock_gh):
        """Test a normal claim flow: polls, finds one ready, claims it, resets workspace, and creates branch."""
        # Setup mock behavior for gh commands
        def gh_side_effect(args):
            cmd_str = " ".join(args)
            if "issue list" in cmd_str and "agent-blocked" in cmd_str:
                # No blocked issues
                return "[]"
            elif "issue list" in cmd_str and "agent-ready" in cmd_str:
                # One ready issue
                return json.dumps([
                    {
                        "number": 10,
                        "title": "Add database migration",
                        "body": "We need to add a migration script."
                    }
                ])
            elif "issue view 10" in cmd_str and "labels" in cmd_str:
                # Verify labels check: still has agent-ready
                return json.dumps({"labels": [{"name": "agent-ready"}]})
            elif "issue edit 10" in cmd_str:
                # Label transition edit
                return ""
            raise ValueError(f"Unexpected gh call: {args}")

        mock_gh.side_effect = gh_side_effect

        # Create a dirty untracked file to verify workspace cleanup runs on fresh claim
        dirty_file = self.workspace_dir / "dirty.txt"
        dirty_file.write_text("should be cleaned", encoding="utf-8")
        self.assertTrue(dirty_file.exists())

        # Run claim node
        initial_state = DEFAULT_STATE.copy()
        new_state = claim_node(initial_state)

        # Verify state updates
        self.assertEqual(new_state["issue_number"], 10)
        self.assertEqual(new_state["status"], "claimed")
        self.assertEqual(new_state["phase"], "claimed")
        self.assertEqual(new_state["branch"], "feat/issue-10")

        # Verify state file was saved
        saved_state = state_module.load()
        self.assertEqual(saved_state["issue_number"], 10)
        self.assertEqual(saved_state["status"], "claimed")

        # Verify workspace hygiene: dirty file is cleaned and we are on the new feature branch
        self.assertFalse(dirty_file.exists())
        
        # Check current git branch
        branch_res = subprocess.run(["git", "branch", "--show-current"], cwd=str(self.workspace_dir), capture_output=True, text=True, check=True)
        self.assertEqual(branch_res.stdout.strip(), "feat/issue-10")

    @patch("orchestrator.nodes._run_gh")
    def test_resume_flow(self, mock_gh):
        """Test resume flow: detects resume, skips clean/reset, checks out branch, preserves dirty files."""
        # Create the feature branch and a dirty file in it
        subprocess.run(["git", "checkout", "-b", "feat/issue-10"], cwd=str(self.workspace_dir), check=True, capture_output=True)
        
        dirty_file = self.workspace_dir / "in_flight.py"
        dirty_file.write_text("print('in flight')", encoding="utf-8")
        
        # Switch back to main to simulate entering from elsewhere
        subprocess.run(["git", "checkout", "main"], cwd=str(self.workspace_dir), check=True, capture_output=True)

        # Setup state to simulate resume
        resume_state = DEFAULT_STATE.copy()
        resume_state["issue_number"] = 10
        resume_state["status"] = "executing"
        resume_state["phase"] = "executing"
        resume_state["branch"] = "feat/issue-10"
        state_module.save(resume_state)

        # Run claim node
        new_state = claim_node(resume_state)

        # Verify state is unchanged
        self.assertEqual(new_state["issue_number"], 10)
        self.assertEqual(new_state["status"], "executing")

        # Verify we checked out the branch and the dirty file was PRESERVED
        self.assertTrue(dirty_file.exists())
        self.assertEqual(dirty_file.read_text(encoding="utf-8"), "print('in flight')")
        
        branch_res = subprocess.run(["git", "branch", "--show-current"], cwd=str(self.workspace_dir), capture_output=True, text=True, check=True)
        self.assertEqual(branch_res.stdout.strip(), "feat/issue-10")
        
        # gh should not have been called since we skip polling on resume
        mock_gh.assert_not_called()

    @patch("orchestrator.nodes._run_gh")
    def test_blocked_issue_detection(self, mock_gh):
        """Test that a ready issue with an open dependency gets transitioned to agent-blocked."""
        # Setup mock behavior
        def gh_side_effect(args):
            cmd_str = " ".join(args)
            if "issue list" in cmd_str and "agent-blocked" in cmd_str:
                return "[]"
            elif "issue list" in cmd_str and "agent-ready" in cmd_str:
                # Issue 11 is ready, but blocked by Issue 5
                return json.dumps([
                    {
                        "number": 11,
                        "title": "Add user dashboard",
                        "body": "Depends on ## Blocked by\n- #5"
                    }
                ])
            elif "issue view 5" in cmd_str and "state" in cmd_str:
                # Dependency issue 5 is still OPEN
                return json.dumps({"state": "OPEN"})
            elif "issue edit 11" in cmd_str and "agent-blocked" in cmd_str:
                # Transition to blocked
                return ""
            raise ValueError(f"Unexpected gh call: {args}")

        mock_gh.side_effect = gh_side_effect

        new_state = claim_node(DEFAULT_STATE.copy())

        # State should remain idle since the only ready issue was blocked
        self.assertEqual(new_state["status"], "idle")
        self.assertIsNone(new_state["issue_number"])
        
        # Verify the transition edit was called on issue 11
        edit_calls = [call for call in mock_gh.mock_calls if "edit" in str(call) and "11" in str(call)]
        self.assertTrue(len(edit_calls) > 0)

    @patch("orchestrator.nodes._run_gh")
    def test_self_healing_unblock(self, mock_gh):
        """Test that a blocked issue with all closed dependencies gets transitioned to agent-ready."""
        # Setup mock behavior
        def gh_side_effect(args):
            cmd_str = " ".join(args)
            if "issue list" in cmd_str and "agent-blocked" in cmd_str:
                # Issue 12 is blocked by 5 and 6
                return json.dumps([
                    {
                        "number": 12,
                        "body": "## Blocked by\n- #5\n- #6"
                    }
                ])
            elif "issue view 5" in cmd_str and "state" in cmd_str:
                # Dependency 5 is closed
                return json.dumps({"state": "CLOSED"})
            elif "issue view 6" in cmd_str and "state" in cmd_str:
                # Dependency 6 is closed
                return json.dumps({"state": "CLOSED"})
            elif "issue edit 12" in cmd_str and "agent-ready" in cmd_str:
                # Unblock transition
                return ""
            elif "issue list" in cmd_str and "agent-ready" in cmd_str:
                # No ready issues (simulate that it gets unblocked but we don't claim it in this run yet)
                return "[]"
            raise ValueError(f"Unexpected gh call: {args}")

        mock_gh.side_effect = gh_side_effect

        new_state = claim_node(DEFAULT_STATE.copy())

        self.assertEqual(new_state["status"], "idle")
        
        # Verify unblock call was made
        unblock_calls = [call for call in mock_gh.mock_calls if "edit" in str(call) and "12" in str(call) and "agent-ready" in str(call)]
        self.assertTrue(len(unblock_calls) > 0)

    @patch("orchestrator.nodes._run_gh")
    def test_empty_queue(self, mock_gh):
        """Test that if there are no ready or blocked issues, the state transitions to idle."""
        mock_gh.return_value = "[]"

        new_state = claim_node(DEFAULT_STATE.copy())
        self.assertEqual(new_state["status"], "idle")
        self.assertIsNone(new_state["issue_number"])

    @patch("orchestrator.nodes._run_gh")
    def test_race_condition(self, mock_gh):
        """Test race condition: issue 10 missing ready label in view check, so it claims issue 11 instead."""
        def gh_side_effect(args):
            cmd_str = " ".join(args)
            if "issue list" in cmd_str and "agent-blocked" in cmd_str:
                return "[]"
            elif "issue list" in cmd_str and "agent-ready" in cmd_str:
                # Return two issues
                return json.dumps([
                    {"number": 10, "title": "Task 1", "body": ""},
                    {"number": 11, "title": "Task 2", "body": ""}
                ])
            elif "issue view 10" in cmd_str and "labels" in cmd_str:
                # Issue 10 was already claimed by someone else (does not have agent-ready)
                return json.dumps({"labels": [{"name": "in-progress"}]})
            elif "issue view 11" in cmd_str and "labels" in cmd_str:
                # Issue 11 is still ready
                return json.dumps({"labels": [{"name": "agent-ready"}]})
            elif "issue edit 11" in cmd_str:
                # Claim issue 11
                return ""
            raise ValueError(f"Unexpected gh call: {args}")

        mock_gh.side_effect = gh_side_effect

        new_state = claim_node(DEFAULT_STATE.copy())

        # State should have claimed issue 11 (skipping 10)
        self.assertEqual(new_state["issue_number"], 11)
        self.assertEqual(new_state["status"], "claimed")
        self.assertEqual(new_state["branch"], "feat/issue-11")

if __name__ == "__main__":
    unittest.main()
