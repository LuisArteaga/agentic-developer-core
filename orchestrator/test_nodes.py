import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

from orchestrator import state as state_module
from orchestrator.nodes import (
    claim_node,
    execute_node,
    plan_node,
    verify_node,
)
from orchestrator.state import DEFAULT_STATE, AgentState


def _raw_structured_response(parsed, finish_reason="stop", parsing_error=None):
    """Build an include_raw=True structured-output dict for test mocks.

    Mirrors the dict shape returned by LangChain's
    ``with_structured_output(..., include_raw=True).invoke(...)``.
    """
    raw_msg = MagicMock()
    raw_msg.response_metadata = {"finish_reason": finish_reason}
    return {"raw": raw_msg, "parsed": parsed, "parsing_error": parsing_error}


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
            "AGENT_MODE": "local",
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v

        # Initialize a real Git repository in the temp workspace
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(self.workspace_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(self.workspace_dir),
            check=True,
        )

        # Add an initial commit so HEAD exists
        self.initial_file = self.workspace_dir / "README.md"
        self.initial_file.write_text("# Test Repo", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"], cwd=str(self.workspace_dir), check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=str(self.workspace_dir),
            check=True,
        )

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

    @patch("orchestrator.nodes._github_api_request")
    def test_normal_claim_flow(self, mock_api):
        """Test a normal claim flow: polls, finds one ready, claims it, resets workspace, and creates branch."""

        # Setup mock behavior for GitHub API requests
        def api_side_effect(method, path, body=None):
            if method == "GET":
                if "/issues" in path and "labels=agent-blocked" in path:
                    # No blocked issues
                    return []
                elif "/issues" in path and "labels=agent-ready" in path:
                    # One ready issue
                    return [
                        {
                            "number": 10,
                            "title": "Add database migration",
                            "body": "We need to add a migration script.",
                        }
                    ]
                elif path.endswith("/issues/10"):
                    # Verify labels check: still has agent-ready
                    return {"labels": [{"name": "agent-ready"}]}
            elif method in ("POST", "DELETE"):
                if "/issues/10/labels" in path:
                    # Label transition edits
                    return {}
            raise ValueError(f"Unexpected API call: {method} {path} {body}")

        mock_api.side_effect = api_side_effect

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
        branch_res = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(self.workspace_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(branch_res.stdout.strip(), "feat/issue-10")

    @patch("orchestrator.nodes.start_orchestrator_loop")
    @patch("orchestrator.nodes._github_api_request")
    def test_claim_node_wires_branch_to_telemetry(self, mock_api, mock_start_loop):
        """claim_node passes issue_number and branch to start_orchestrator_loop.

        Regression test for issue #34: the telemetry call site in the claim
        node must wire both issue_number and branch_name so the Langfuse
        session ID is constructed correctly.
        """

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if "labels=agent-blocked" in path:
                    return []
                elif "labels=agent-ready" in path:
                    return [
                        {
                            "number": 10,
                            "title": "Add database migration",
                            "body": "We need to add a migration script.",
                        }
                    ]
                elif path.endswith("/issues/10"):
                    return {"labels": [{"name": "agent-ready"}]}
            elif method in ("POST", "DELETE"):
                if "/issues/10/labels" in path:
                    return {}
            raise ValueError(f"Unexpected API call: {method} {path} {body}")

        mock_api.side_effect = api_side_effect

        initial_state = DEFAULT_STATE.copy()
        claim_node(initial_state)

        mock_start_loop.assert_called_once_with(issue_number=10, branch="feat/issue-10")

    @patch("orchestrator.nodes._github_api_request")
    def test_resume_flow(self, mock_api):
        """Test resume flow: detects resume, skips clean/reset, checks out branch, preserves dirty files."""
        # Create the feature branch and a dirty file in it
        subprocess.run(
            ["git", "checkout", "-b", "feat/issue-10"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )

        dirty_file = self.workspace_dir / "in_flight.py"
        dirty_file.write_text("print('in flight')", encoding="utf-8")

        # Switch back to main to simulate entering from elsewhere
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )

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

        branch_res = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(self.workspace_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(branch_res.stdout.strip(), "feat/issue-10")

        # GitHub API should not have been called since we skip polling on resume
        mock_api.assert_not_called()

    @patch("orchestrator.nodes._github_api_request")
    def test_blocked_issue_detection(self, mock_api):
        """Test that a ready issue with an open dependency gets transitioned to agent-blocked."""

        # Setup mock behavior
        def api_side_effect(method, path, body=None):
            if method == "GET":
                if "/issues" in path and "labels=agent-blocked" in path:
                    return []
                elif "/issues" in path and "labels=agent-ready" in path:
                    # Issue 11 is ready, but blocked by Issue 5
                    return [
                        {
                            "number": 11,
                            "title": "Add user dashboard",
                            "body": "Depends on ## Blocked by\n- #5",
                        }
                    ]
                elif path.endswith("/issues/5"):
                    # Dependency issue 5 is still open (OPEN)
                    return {"state": "open"}
            elif method in ("POST", "DELETE"):
                if "/issues/11/labels" in path:
                    # Transition edits
                    return {}
            raise ValueError(f"Unexpected API call: {method} {path} {body}")

        mock_api.side_effect = api_side_effect

        new_state = claim_node(DEFAULT_STATE.copy())

        # State should remain idle since the only ready issue was blocked
        self.assertEqual(new_state["status"], "idle")
        self.assertIsNone(new_state["issue_number"])

        # Verify the transition edit was called on issue 11 (adding agent-blocked)
        edit_calls = [
            call
            for call in mock_api.mock_calls
            if "11" in str(call) and "agent-blocked" in str(call)
        ]
        self.assertTrue(len(edit_calls) > 0)

    @patch("orchestrator.nodes._github_api_request")
    def test_self_healing_unblock(self, mock_api):
        """Test that a blocked issue with all closed dependencies gets transitioned to agent-ready."""

        # Setup mock behavior
        def api_side_effect(method, path, body=None):
            if method == "GET":
                if "/issues" in path and "labels=agent-blocked" in path:
                    # Issue 12 is blocked by 5 and 6
                    return [{"number": 12, "body": "## Blocked by\n- #5\n- #6"}]
                elif path.endswith("/issues/5"):
                    # Dependency 5 is closed
                    return {"state": "closed"}
                elif path.endswith("/issues/6"):
                    # Dependency 6 is closed
                    return {"state": "closed"}
                elif "/issues" in path and "labels=agent-ready" in path:
                    # No ready issues (simulate that it gets unblocked but we don't claim it in this run yet)
                    return []
            elif method in ("POST", "DELETE"):
                if "/issues/12/labels" in path:
                    # Unblock transition edits
                    return {}
            raise ValueError(f"Unexpected API call: {method} {path} {body}")

        mock_api.side_effect = api_side_effect

        new_state = claim_node(DEFAULT_STATE.copy())

        self.assertEqual(new_state["status"], "idle")

        # Verify unblock call was made (adding agent-ready to issue 12)
        unblock_calls = [
            call
            for call in mock_api.mock_calls
            if "12" in str(call) and "agent-ready" in str(call)
        ]
        self.assertTrue(len(unblock_calls) > 0)

    @patch("orchestrator.nodes._github_api_request")
    def test_empty_queue(self, mock_api):
        """Test that if there are no ready or blocked issues, the state transitions to idle."""
        mock_api.return_value = []

        new_state = claim_node(DEFAULT_STATE.copy())
        self.assertEqual(new_state["status"], "idle")
        self.assertIsNone(new_state["issue_number"])

    @patch("orchestrator.nodes._github_api_request")
    def test_race_condition(self, mock_api):
        """Test race condition: issue 10 missing ready label in view check, so it claims issue 11 instead."""

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if "/issues" in path and "labels=agent-blocked" in path:
                    return []
                elif "/issues" in path and "labels=agent-ready" in path:
                    # Return two issues
                    return [
                        {"number": 10, "title": "Task 1", "body": ""},
                        {"number": 11, "title": "Task 2", "body": ""},
                    ]
                elif path.endswith("/issues/10"):
                    # Issue 10 was already claimed by someone else (does not have agent-ready)
                    return {"labels": [{"name": "in-progress"}]}
                elif path.endswith("/issues/11"):
                    # Issue 11 is still ready
                    return {"labels": [{"name": "agent-ready"}]}
            elif method in ("POST", "DELETE"):
                if "/issues/11/labels" in path:
                    # Claim issue 11
                    return {}
            raise ValueError(f"Unexpected API call: {method} {path} {body}")

        mock_api.side_effect = api_side_effect

        new_state = claim_node(DEFAULT_STATE.copy())

        # State should have claimed issue 11 (skipping 10)
        self.assertEqual(new_state["issue_number"], 11)
        self.assertEqual(new_state["status"], "claimed")
        self.assertEqual(new_state["branch"], "feat/issue-11")

    @patch("orchestrator.nodes.start_orchestrator_loop")
    @patch("orchestrator.nodes._github_api_request")
    def test_claim_oldest_lowest_numbered_issue(self, mock_api, mock_start_loop):
        """Claim Scan claims the lowest-numbered non-blocked ready issue.

        Regression test for issue #109: the GitHub REST API default sort is
        ``created`` + ``desc`` (newest first). The ready issues are returned
        here in that non-ascending order to verify the defensive in-code sort
        enforces oldest-first claiming regardless of API ordering.
        """

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if "/issues" in path and "labels=agent-blocked" in path:
                    return []
                elif "/issues" in path and "labels=agent-ready" in path:
                    # API default sort (desc) returns newest first; the claim
                    # node must re-sort to oldest-first and claim #10.
                    return [
                        {"number": 30, "title": "Task 30", "body": ""},
                        {"number": 20, "title": "Task 20", "body": ""},
                        {"number": 10, "title": "Task 10", "body": ""},
                    ]
                elif path.endswith("/issues/10"):
                    # Concurrency check: #10 still has the ready label
                    return {"labels": [{"name": "agent-ready"}]}
            elif method in ("POST", "DELETE"):
                if "/issues/10/labels" in path:
                    return {}
            raise ValueError(f"Unexpected API call: {method} {path} {body}")

        mock_api.side_effect = api_side_effect

        new_state = claim_node(DEFAULT_STATE.copy())

        # The lowest-numbered ready issue (#10) must be claimed, not #30.
        self.assertEqual(new_state["issue_number"], 10)
        self.assertEqual(new_state["status"], "claimed")
        self.assertEqual(new_state["branch"], "feat/issue-10")

        # No label-transition edits should have touched the higher-numbered
        # issues (#20, #30) since the loop breaks on the first claim.
        non_claimed_edits = [
            call
            for call in mock_api.mock_calls
            if ("/issues/20/labels" in str(call) or "/issues/30/labels" in str(call))
        ]
        self.assertEqual(non_claimed_edits, [])


class TestPlanNode(unittest.TestCase):
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
            "AGENT_MODE": "local",
            # Isolate the verify command so the default ('make verify') path is
            # exercised deterministically regardless of any AGENT_VERIFY_COMMAND
            # exported in the surrounding shell (e.g. 'python -m pytest tests/').
            # A None value means "ensure this var is unset" (see the loop below).
            "AGENT_VERIFY_COMMAND": None,
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

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

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes._github_api_request")
    def test_plan_node_success(self, mock_github_api, mock_get_chat_model):
        """Test successful Plan-Node execution: fetches issue, calls LLM, and serializes Pydantic plan."""
        from orchestrator.nodes import DevelopmentPlan, PlanningTask

        # Setup mock GitHub API response
        mock_github_api.return_value = {
            "title": "Add DB migration",
            "body": "We need a migration script for user profiles.",
        }

        # Setup mock LLM and structured output
        mock_llm = unittest.mock.MagicMock()
        mock_structured_llm = unittest.mock.MagicMock()

        mock_get_chat_model.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm

        mock_plan = DevelopmentPlan(
            rationale="We need to add a migration script.",
            tasks=[
                PlanningTask(
                    step_number=1,
                    action="read",
                    description="Read schema file",
                    target_files=["schema.py"],
                ),
                PlanningTask(
                    step_number=2,
                    action="patch",
                    description="Add migration",
                    target_files=["migration.py"],
                ),
            ],
        )
        mock_structured_llm.invoke.return_value = _raw_structured_response(mock_plan)

        # Setup initial state with some pre-existing read_files to verify it gets reset per ADR-0006
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["model"] = "gpt-4o"
        state["read_files"] = {"some_old_file.py": [[1, 5]]}
        state_module.save(state)

        # Run plan node
        new_state = plan_node(state)

        # Verify state transitions and read_files reset
        self.assertEqual(new_state["status"], "planning")
        self.assertEqual(new_state["phase"], "planning")
        self.assertEqual(new_state["read_files"], {})
        self.assertIsNotNone(new_state["plan"])
        self.assertEqual(new_state["model"], "gpt-4o")

        # Verify state file was saved
        saved_state = state_module.load()
        self.assertEqual(saved_state["status"], "planning")
        self.assertEqual(saved_state["read_files"], {})

        # Verify serialized plan structure
        assert new_state["plan"] is not None
        plan_data = json.loads(new_state["plan"])
        self.assertEqual(plan_data["rationale"], "We need to add a migration script.")
        self.assertEqual(len(plan_data["tasks"]), 2)
        self.assertEqual(plan_data["tasks"][0]["description"], "Read schema file")
        self.assertEqual(plan_data["tasks"][0]["target_files"], ["schema.py"])

    def test_plan_node_missing_issue(self):
        """Test that Plan-Node raises ValueError if 'issue_number' is not set in the state."""
        state = DEFAULT_STATE.copy()
        state["issue_number"] = None

        with self.assertRaises(ValueError) as ctx:
            plan_node(state)
        self.assertIn("issue_number", str(ctx.exception))

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes._github_api_request")
    def test_plan_node_llm_failure(self, mock_github_api, mock_get_chat_model):
        """Test that Plan-Node handles LLM failures by recording 'failed' and
        routing to recovery (return state, no re-raise — ADR-0038)."""
        # Setup mock GitHub API response
        mock_github_api.return_value = {
            "title": "Add DB migration",
            "body": "We need a migration script for user profiles.",
        }

        # Setup mock LLM to raise an exception when invoked
        mock_llm = unittest.mock.MagicMock()
        mock_structured_llm = unittest.mock.MagicMock()

        mock_get_chat_model.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm

        mock_structured_llm.invoke.side_effect = RuntimeError("OpenRouter API error")

        # Setup initial state
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state_module.save(state)

        # ADR-0038: the node records the failure and returns state (no raise)
        # so the status="failed" conditional edges route to recovery_node.
        result = plan_node(state)
        self.assertIs(result, state)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "planning")
        self.assertIn("plan: OpenRouter API error", result["error"] or "")

        # Verify state is persisted as 'failed' at 'planning' phase
        saved_state = state_module.load()
        self.assertEqual(saved_state["status"], "failed")
        self.assertEqual(saved_state["phase"], "planning")

    def test_plan_node_security_path_validation(self):
        """Test that the _is_safe_path helper correctly identifies safe and unsafe/forbidden paths."""
        from orchestrator.nodes import _is_safe_path

        # Safe paths
        self.assertTrue(_is_safe_path("src/main.py"))
        self.assertTrue(_is_safe_path("README.md"))
        self.assertTrue(_is_safe_path("orchestrator/nodes.py"))
        self.assertTrue(_is_safe_path("tests/test_something.py"))

        # Safe paths: committed template variants whose stem matches a
        # forbidden name (e.g. .env.example — placeholder, no secrets)
        self.assertTrue(_is_safe_path(".env.example"))
        self.assertTrue(_is_safe_path(".env.template"))
        self.assertTrue(_is_safe_path(".env.dist"))
        self.assertTrue(_is_safe_path("config/.env.sample"))

        # Unsafe paths: absolute paths
        self.assertFalse(_is_safe_path("/etc/passwd"))
        self.assertFalse(_is_safe_path("/absolute/path/file.txt"))

        # Unsafe paths: directory traversal
        self.assertFalse(_is_safe_path("../outside.py"))
        self.assertFalse(_is_safe_path("src/../../outside.py"))

        # Unsafe paths: forbidden directories
        self.assertFalse(_is_safe_path(".git/config"))
        self.assertFalse(
            _is_safe_path(".venv/lib/python3.12/site-packages/something.py")
        )
        self.assertFalse(_is_safe_path("src/.agent_logs/state.json"))
        self.assertFalse(_is_safe_path(".agents/AGENTS.md"))

        # Unsafe paths: forbidden filenames / credentials
        self.assertFalse(_is_safe_path(".env"))
        self.assertFalse(_is_safe_path("src/.env"))
        self.assertFalse(_is_safe_path(".env.production"))
        self.assertFalse(_is_safe_path("ssh_keys/id_rsa"))
        self.assertFalse(_is_safe_path("id_rsa.pub"))
        self.assertFalse(_is_safe_path("credentials.txt"))
        self.assertFalse(_is_safe_path("config/id_ed25519"))

        # Unsafe paths: sensitive extensions
        self.assertFalse(_is_safe_path("certs/private.key"))
        self.assertFalse(_is_safe_path("auth/token.pem"))
        self.assertFalse(_is_safe_path("cert.pfx"))

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes._github_api_request")
    def test_plan_node_security_injection_blocking(
        self, mock_github_api, mock_get_chat_model
    ):
        """Test that Plan-Node catches prompt injection attempts in the generated plan and aborts with a security block."""
        from orchestrator.nodes import DevelopmentPlan, PlanningTask

        # Setup mock GitHub API response
        mock_github_api.return_value = {
            "title": "Malicious Issue",
            "body": "System prompt override attempt.",
        }

        # Setup mock LLM and structured output returning a malicious plan targeting .env
        mock_llm = unittest.mock.MagicMock()
        mock_structured_llm = unittest.mock.MagicMock()
        mock_get_chat_model.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm

        mock_malicious_plan = DevelopmentPlan(
            rationale="Attacker steered reasoning.",
            tasks=[
                PlanningTask(
                    step_number=1,
                    action="read",
                    description="Exfiltrate environment variables",
                    target_files=[".env"],  # Unsafe file!
                )
            ],
        )
        mock_structured_llm.invoke.return_value = _raw_structured_response(
            mock_malicious_plan
        )

        # Setup initial state
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state_module.save(state)

        # ADR-0044: a security block is now handled directly at the block site
        # (not raised as a ValueError caught by the generic except handler).
        # The error carries the ``security_block:`` prefix so recovery and
        # __main__ can branch on it (quarantine label + exit code 42).
        result = plan_node(state)
        self.assertIs(result, state)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "planning")
        self.assertTrue(
            (result["error"] or "").startswith("security_block:"),
            f"Expected 'security_block:' prefix, got: {result['error']!r}",
        )
        self.assertIn(".env", result["error"] or "")

        # Verify state is persisted as 'failed' at 'planning' phase
        saved_state = state_module.load()
        self.assertEqual(saved_state["status"], "failed")
        self.assertEqual(saved_state["phase"], "planning")

    def test_directory_tree_generation(self):
        """Test that the directory tree generator outputs correct structure and honors ignored directories."""
        # Create a directory structure in the temp workspace
        (self.workspace_dir / "src").mkdir()
        (self.workspace_dir / "src" / "utils").mkdir()
        (self.workspace_dir / "src" / "main.py").write_text(
            "print('main')", encoding="utf-8"
        )
        (self.workspace_dir / "src" / "utils" / "helper.py").write_text(
            "def help(): pass", encoding="utf-8"
        )
        (self.workspace_dir / "README.md").write_text("# Readme", encoding="utf-8")

        # Create ignored directories
        (self.workspace_dir / ".git").mkdir()
        (self.workspace_dir / ".venv").mkdir()
        (self.workspace_dir / ".venv" / "bin").mkdir()
        (self.workspace_dir / ".venv" / "lib").mkdir()

        from orchestrator.nodes import _get_directory_tree

        # Generate tree
        tree = _get_directory_tree(self.workspace_dir)

        # Verify output contains files/folders and shows hierarchy
        self.assertIn("README.md", tree)
        self.assertIn("src/", tree)
        self.assertIn("utils/", tree)
        self.assertIn("helper.py", tree)
        self.assertIn("main.py", tree)

        # Verify ignored folders are completely absent
        self.assertNotIn(".git", tree)
        self.assertNotIn(".venv", tree)
        self.assertNotIn("lib", tree)

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes._github_api_request")
    def test_plan_node_requested_files_triggers_followup(
        self, mock_github_api, mock_get_chat_model
    ):
        """Test that requested_files in the first plan triggers a second LLM call (ADR-0024)."""
        from orchestrator.nodes import DevelopmentPlan, PlanningTask

        mock_github_api.return_value = {
            "title": "Refactor module",
            "body": "Refactor the calculator module.",
        }

        mock_llm = unittest.mock.MagicMock()
        mock_structured_llm = unittest.mock.MagicMock()
        mock_get_chat_model.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm

        # First plan requests a file, second plan is the final version
        first_plan = DevelopmentPlan(
            rationale="Need more detail.",
            tasks=[
                PlanningTask(
                    step_number=1,
                    action="read",
                    description="Read file",
                    target_files=["schema.py"],
                )
            ],
            requested_files=["src/main.py"],
        )
        second_plan = DevelopmentPlan(
            rationale="Now I have the full picture.",
            tasks=[
                PlanningTask(
                    step_number=1,
                    action="patch",
                    description="Patch main.py",
                    target_files=["src/main.py"],
                )
            ],
        )
        mock_structured_llm.invoke.side_effect = [
            _raw_structured_response(first_plan),
            _raw_structured_response(second_plan),
        ]

        # Create the requested file so build_outlines_for_files returns content
        (self.workspace_dir / "src").mkdir(exist_ok=True)
        (self.workspace_dir / "src" / "main.py").write_text(
            "def main() -> None:\n    pass\n", encoding="utf-8"
        )

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state_module.save(state)

        new_state = plan_node(state)

        # LLM should have been called twice
        self.assertEqual(mock_structured_llm.invoke.call_count, 2)

        # The second plan should be the one persisted
        assert new_state["plan"] is not None
        plan_data = json.loads(new_state["plan"])
        self.assertEqual(plan_data["rationale"], "Now I have the full picture.")
        self.assertEqual(plan_data["tasks"][0]["target_files"], ["src/main.py"])

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes._github_api_request")
    def test_plan_node_no_requested_files_no_followup(
        self, mock_github_api, mock_get_chat_model
    ):
        """Test that empty requested_files does not trigger a follow-up call."""
        from orchestrator.nodes import DevelopmentPlan, PlanningTask

        mock_github_api.return_value = {
            "title": "Simple fix",
            "body": "Fix a typo.",
        }

        mock_llm = unittest.mock.MagicMock()
        mock_structured_llm = unittest.mock.MagicMock()
        mock_get_chat_model.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm

        mock_plan = DevelopmentPlan(
            rationale="Simple fix.",
            tasks=[
                PlanningTask(
                    step_number=1,
                    action="patch",
                    description="Fix typo",
                    target_files=["README.md"],
                )
            ],
        )
        mock_structured_llm.invoke.return_value = _raw_structured_response(mock_plan)

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state_module.save(state)

        plan_node(state)

        # LLM should have been called only once
        self.assertEqual(mock_structured_llm.invoke.call_count, 1)

    def test_development_plan_has_requested_files_field(self):
        """Test that DevelopmentPlan schema includes requested_files with default empty list."""
        from orchestrator.nodes import DevelopmentPlan

        plan = DevelopmentPlan(rationale="test", tasks=[])
        self.assertEqual(plan.requested_files, [])

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes._github_api_request")
    def test_plan_node_with_structured_output_uses_strict_and_include_raw(
        self, mock_github_api, mock_get_chat_model
    ):
        """AC: with_structured_output is called with strict=True and include_raw=True."""
        from orchestrator.nodes import DevelopmentPlan, PlanningTask

        mock_github_api.return_value = {"title": "t", "body": "b"}

        mock_llm = unittest.mock.MagicMock()
        mock_structured_llm = unittest.mock.MagicMock()
        mock_get_chat_model.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm

        mock_plan = DevelopmentPlan(
            rationale="r",
            tasks=[
                PlanningTask(
                    step_number=1,
                    action="patch",
                    description="d",
                    target_files=["a.py"],
                )
            ],
        )
        mock_structured_llm.invoke.return_value = _raw_structured_response(mock_plan)

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state_module.save(state)

        plan_node(state)

        mock_llm.with_structured_output.assert_called_once_with(
            DevelopmentPlan, strict=True, include_raw=True
        )

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes._github_api_request")
    def test_plan_node_parse_failure_logs_finish_reason(
        self, mock_github_api, mock_get_chat_model
    ):
        """AC: parse-failure log includes finish_reason; plan failure still
        routes to recovery (status=failed)."""
        from orchestrator.nodes import DevelopmentPlan

        mock_github_api.return_value = {"title": "t", "body": "b"}

        mock_llm = unittest.mock.MagicMock()
        mock_structured_llm = unittest.mock.MagicMock()
        mock_get_chat_model.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm

        # Simulate a truncation: parsed is None, parsing_error set, finish_reason=length.
        mock_structured_llm.invoke.return_value = _raw_structured_response(
            None, finish_reason="length", parsing_error=ValueError("truncated JSON")
        )

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state_module.save(state)

        result = plan_node(state)

        # Failure still routes to recovery (ADR-0038).
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "planning")
        # finish_reason appears in the error message.
        self.assertIn("length", result["error"] or "")
        # finish_reason is logged.
        mock_llm.with_structured_output.assert_called_once_with(
            DevelopmentPlan, strict=True, include_raw=True
        )


class TestExecuteNode(unittest.TestCase):
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
            "AGENT_MODE": "local",
            # Isolate the verify command so the default ('make verify') path is
            # exercised deterministically regardless of any AGENT_VERIFY_COMMAND
            # exported in the surrounding shell (e.g. 'python -m pytest tests/').
            # A None value means "ensure this var is unset" (see the loop below).
            "AGENT_VERIFY_COMMAND": None,
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

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

    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_execute_node_success(self, mock_github_api, mock_execute_worker):
        """Test successful execution of Execute-Node: fetches issue details, resets read_files, and invokes worker."""
        # Setup mock GitHub API response
        mock_github_api.return_value = {
            "title": "Fix a bug",
            "body": "There is a bug in main.py.",
        }

        # Setup initial state
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state["read_files"] = {"old_file.py": [[1, 5]]}
        state_module.save(state)

        # Run execute node
        new_state = execute_node(state)

        # Verify status transitions, read_files reset
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "executing")
        self.assertEqual(new_state["read_files"], {})

        # Verify execute_worker was called with correct parameters
        mock_execute_worker.assert_called_once_with(
            "Title: Fix a bug\n\nThere is a bug in main.py.",
            '{"rationale": "...", "tasks": []}',
            issue_number=10,
            attempt=1,
        )

        # Verify state file was saved
        saved_state = state_module.load()
        self.assertEqual(saved_state["status"], "executing")
        self.assertEqual(saved_state["read_files"], {})

    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_execute_node_with_feedback(self, mock_github_api, mock_execute_worker):
        """Test that Execute-Node appends previous verification feedback to the worker prompt on retry."""
        mock_github_api.return_value = {
            "title": "Fix a bug",
            "body": "There is a bug in main.py.",
        }

        # Setup initial state with feedback
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state["feedback"] = "AssertionError: 2 != 3 in test_main.py"
        state_module.save(state)

        # Run execute node
        execute_node(state)

        # Verify execute_worker was called with the feedback appended
        expected_issue_description = (
            "Title: Fix a bug\n\nThere is a bug in main.py.\n\n"
            "=== PREVIOUS EXECUTION FAILURE ===\n"
            "The previous attempt failed verification. Please analyze the following test/validation output and fix the issues:\n"
            "AssertionError: 2 != 3 in test_main.py"
        )
        mock_execute_worker.assert_called_once_with(
            expected_issue_description,
            '{"rationale": "...", "tasks": []}',
            issue_number=10,
            attempt=1,
        )

    @patch("orchestrator.nodes._github_api_request")
    def test_execute_node_failure(self, mock_github_api):
        """Test that Execute-Node records 'failed' and routes to recovery on
        worker error (return state, no re-raise — ADR-0038)."""
        # Setup GitHub API to raise an exception
        mock_github_api.side_effect = RuntimeError("API rate limit")

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state_module.save(state)

        # ADR-0038: the node records the failure and returns state (no raise)
        # so the status="failed" conditional edges route to recovery_node.
        result = execute_node(state)
        self.assertIs(result, state)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "executing")
        self.assertIn("execute: API rate limit", result["error"] or "")

        # Verify state is persisted to failed at executing phase
        saved_state = state_module.load()
        self.assertEqual(saved_state["status"], "failed")
        self.assertEqual(saved_state["phase"], "executing")

    @patch("orchestrator.nodes.reset_hard")
    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_execute_node_no_hard_reset_on_early_attempts(
        self, mock_github_api, mock_execute_worker, mock_reset_hard
    ):
        """Early execute attempts (below the hard-reset threshold) stay incremental: no reset."""
        mock_github_api.return_value = {"title": "Fix a bug", "body": "body"}

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        # attempts["verify_cmd"]=0 -> first execute attempt (1) -> below threshold (3)
        state_module.save(state)

        execute_node(state)

        mock_reset_hard.assert_not_called()
        mock_execute_worker.assert_called_once_with(
            unittest.mock.ANY,
            '{"rationale": "...", "tasks": []}',
            issue_number=10,
            attempt=1,
        )

    @patch("orchestrator.nodes.reset_hard")
    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_execute_node_hard_reset_on_final_attempt(
        self, mock_github_api, mock_execute_worker, mock_reset_hard
    ):
        """The 3rd execute attempt (default threshold) hard-resets tracked files before the Worker runs."""
        mock_github_api.return_value = {"title": "Fix a bug", "body": "body"}

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        # attempts["verify_cmd"]=2 -> execute attempt 3 -> meets default threshold
        state["attempts"] = {"verify_cmd": 2}
        state_module.save(state)

        execute_node(state)

        # reset_hard called exactly once, to HEAD, on the resolved workspace
        self.assertEqual(mock_reset_hard.call_count, 1)
        call_args, call_kwargs = mock_reset_hard.call_args
        self.assertEqual(call_args[1], "HEAD")
        self.assertEqual(Path(call_args[0]), self.workspace_dir)
        self.assertEqual(call_kwargs, {})
        # Worker still invoked on attempt 3 with prior feedback injected
        mock_execute_worker.assert_called_once_with(
            unittest.mock.ANY,
            '{"rationale": "...", "tasks": []}',
            issue_number=10,
            attempt=3,
        )

    @patch("orchestrator.nodes.reset_hard")
    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_execute_node_hard_reset_threshold_configurable(
        self, mock_github_api, mock_execute_worker, mock_reset_hard
    ):
        """AGENT_RETRY_HARD_RESET_ATTEMPT lowers the reset point for weaker models."""
        mock_github_api.return_value = {"title": "Fix a bug", "body": "body"}

        self.original_env["AGENT_RETRY_HARD_RESET_ATTEMPT"] = os.environ.get(
            "AGENT_RETRY_HARD_RESET_ATTEMPT"
        )
        os.environ["AGENT_RETRY_HARD_RESET_ATTEMPT"] = "2"

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        # attempts["verify_cmd"]=1 -> execute attempt 2 -> meets configured threshold
        state["attempts"] = {"verify_cmd": 1}
        state_module.save(state)

        execute_node(state)

        mock_reset_hard.assert_called_once()
        call_args, _ = mock_reset_hard.call_args
        self.assertEqual(call_args[1], "HEAD")
        mock_execute_worker.assert_called_once_with(
            unittest.mock.ANY,
            '{"rationale": "...", "tasks": []}',
            issue_number=10,
            attempt=2,
        )

    @patch("orchestrator.nodes.reset_hard")
    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_execute_node_hard_reset_disabled_when_threshold_high(
        self, mock_github_api, mock_execute_worker, mock_reset_hard
    ):
        """A threshold above the max retry count disables hard reset (pure ADR-0013 incremental)."""
        mock_github_api.return_value = {"title": "Fix a bug", "body": "body"}

        self.original_env["AGENT_RETRY_HARD_RESET_ATTEMPT"] = os.environ.get(
            "AGENT_RETRY_HARD_RESET_ATTEMPT"
        )
        os.environ["AGENT_RETRY_HARD_RESET_ATTEMPT"] = "99"

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state["attempts"] = {"verify_cmd": 2}  # attempt 3, but threshold 99 -> no reset
        state_module.save(state)

        execute_node(state)

        mock_reset_hard.assert_not_called()
        mock_execute_worker.assert_called_once_with(
            unittest.mock.ANY,
            '{"rationale": "...", "tasks": []}',
            issue_number=10,
            attempt=3,
        )

    @patch("orchestrator.nodes.reset_hard")
    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_execute_node_attempt_derived_from_both_gate_counters(
        self, mock_github_api, mock_execute_worker, mock_reset_hard
    ):
        """ADR-0047: the execute attempt index is the SUM of the per-gate verify
        counters (verify_cmd + bineval + 1), so a BinEval-only failure history
        still advances the Hybrid Retry threshold. verify_cmd=1 + bineval=1 ->
        attempt 3 -> meets the default hard-reset threshold.
        """
        mock_github_api.return_value = {"title": "Fix a bug", "body": "body"}

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state["attempts"] = {"verify_cmd": 1, "bineval": 1}
        state_module.save(state)

        execute_node(state)

        self.assertEqual(mock_reset_hard.call_count, 1)
        mock_execute_worker.assert_called_once_with(
            unittest.mock.ANY,
            '{"rationale": "...", "tasks": []}',
            issue_number=10,
            attempt=3,
        )


class TestVerifyNode(unittest.TestCase):
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
            "AGENT_MODE": "local",
            # Isolate the verify command so the default ('make verify') path is
            # exercised deterministically regardless of any AGENT_VERIFY_COMMAND
            # exported in the surrounding shell (e.g. 'python -m pytest tests/').
            # A None value means "ensure this var is unset" (see the loop below).
            "AGENT_VERIFY_COMMAND": None,
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

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

    @patch("orchestrator.nodes._get_workspace_diff", return_value="")
    @patch("orchestrator.nodes.subprocess.run")
    def test_verify_node_success(self, mock_subprocess_run, _mock_diff):
        """Test successful verification: exit code 0, clears feedback and resets attempts."""
        # Mock subprocess run to return success
        mock_res = unittest.mock.MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = b"All 10 tests passed."
        mock_subprocess_run.return_value = mock_res

        # Setup initial state with existing attempts and feedback
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["attempts"] = {"verify_cmd": 2}
        state["feedback"] = "some previous error"
        state_module.save(state)

        # Run verify node
        new_state = verify_node(state)

        # Verify state changes: status 'verifying', attempts reset to 0, feedback cleared
        self.assertEqual(new_state["status"], "verifying")
        self.assertEqual(new_state["phase"], "verifying")
        self.assertEqual(new_state["attempts"]["verify_cmd"], 0)
        self.assertIsNone(new_state["feedback"])

        # Verify subprocess was called correctly (default command: 'make verify')
        mock_subprocess_run.assert_called_once_with(
            ["make", "verify"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self.workspace_dir,
            timeout=300,
            shell=False,
        )

    @patch("orchestrator.nodes.subprocess.run")
    def test_verify_node_failure_retry(self, mock_subprocess_run):
        """Test verification failure with retry: increments attempts, saves feedback, transitions to executing."""
        mock_res = unittest.mock.MagicMock()
        mock_res.returncode = 1
        mock_res.stdout = b"AssertionError: 1 != 2"
        mock_subprocess_run.return_value = mock_res

        # Setup initial state
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["attempts"] = {"verify_cmd": 1}
        state_module.save(state)

        # Run verify node
        new_state = verify_node(state)

        # Verify state: status becomes 'executing' for retry, attempts incremented to 2, feedback saved
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "verifying")
        self.assertEqual(new_state["attempts"]["verify_cmd"], 2)
        self.assertEqual(new_state["feedback"], "AssertionError: 1 != 2")

    @patch("orchestrator.nodes.subprocess.run")
    def test_verify_node_failure_max_retries(self, mock_subprocess_run):
        """Test verification failure exceeding max retries: status transitions to failed."""
        mock_res = unittest.mock.MagicMock()
        mock_res.returncode = 1
        mock_res.stdout = b"AssertionError: 1 != 2"
        mock_subprocess_run.return_value = mock_res

        # Setup initial state at 2 attempts
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["attempts"] = {"verify_cmd": 2}
        state_module.save(state)

        # Run verify node (this is attempt 3)
        new_state = verify_node(state)

        # Verify state: status becomes 'failed', attempts incremented to 3
        self.assertEqual(new_state["status"], "failed")
        self.assertEqual(new_state["phase"], "verifying")
        self.assertEqual(new_state["attempts"]["verify_cmd"], 3)
        self.assertEqual(new_state["feedback"], "AssertionError: 1 != 2")

    @patch("orchestrator.nodes.subprocess.run")
    def test_verify_node_timeout(self, mock_subprocess_run):
        """Test verification command timing out: captures timeout error, increments attempts."""
        import subprocess

        mock_subprocess_run.side_effect = subprocess.TimeoutExpired(
            cmd=["make", "verify"], timeout=300, output=b"Starting tests...\n"
        )

        # Setup initial state
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state_module.save(state)

        # Run verify node
        new_state = verify_node(state)

        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["attempts"]["verify_cmd"], 1)
        assert new_state["feedback"] is not None
        self.assertIn("timed out after 300 seconds", new_state["feedback"])
        self.assertIn("Starting tests...", new_state["feedback"])

    @patch("orchestrator.nodes.subprocess.run")
    def test_verify_node_large_output_under_line_limit(self, mock_subprocess_run):
        """Test that verify_node correctly truncates output exceeding 10 KB even if it is under the line limit."""
        # Create a single extremely long line of 12 KB
        large_content = "A" * 12000
        mock_res = unittest.mock.MagicMock()
        mock_res.returncode = 1
        mock_res.stdout = large_content.encode("utf-8")
        mock_subprocess_run.return_value = mock_res

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state_module.save(state)

        new_state = verify_node(state)

        # Verify that the saved feedback is under 10 KB (10240 bytes)
        assert new_state["feedback"] is not None
        feedback_bytes = new_state["feedback"].encode("utf-8")
        self.assertTrue(len(feedback_bytes) <= 10240)
        self.assertIn("exceeded 10 KB limit", new_state["feedback"])


def _make_bineval_check(check_id, dimension, description, passed, reasoning="ok"):
    from orchestrator.nodes import BinEvalCheck

    return BinEvalCheck(
        id=check_id,
        dimension=dimension,
        description=description,
        passed=passed,
        reasoning=reasoning,
    )


def _all_pass_result():
    from orchestrator.nodes import BinEvalResult

    checks = [
        _make_bineval_check("1.1", "Completeness", "Acceptance criteria", True),
        _make_bineval_check("1.2", "Completeness", "Edge cases", True),
        _make_bineval_check("1.3", "Completeness", "Scope discipline", True),
        _make_bineval_check("2.1", "Simplicity", "No unnecessary abstraction", True),
        _make_bineval_check("2.2", "Simplicity", "No dead code", True),
        _make_bineval_check("2.3", "Simplicity", "Minimal change", True),
        _make_bineval_check("3.1", "ADR Compliance", "Complies with ADRs", True),
        _make_bineval_check("3.2", "ADR Compliance", "No contradictory decision", True),
        _make_bineval_check("4.1", "Robustness", "Error paths handled", True),
        _make_bineval_check("4.2", "Robustness", "No regression risk", True),
    ]
    return BinEvalResult(checks=checks, summary="all good")


def _result_with_fail(check_id, dimension, description, reasoning):
    result = _all_pass_result()
    for check in result.checks:
        if check.id == check_id:
            check.passed = False
            check.reasoning = reasoning
            break
    return result


class TestBinEvalPhase(unittest.TestCase):
    """Tests for the Pre-PR BinEval soft gate inside verify_node."""

    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()
        self.original_env = {}
        for k, v in {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "AGENT_MODE": "local",
            # Isolate the verify command so the default ('make verify') path is
            # exercised deterministically regardless of any AGENT_VERIFY_COMMAND
            # exported in the surrounding shell. None means "ensure unset".
            "AGENT_VERIFY_COMMAND": None,
        }.items():
            self.original_env[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()

    def _state(self, verify_cmd=0, bineval=0):
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["attempts"] = {"verify_cmd": verify_cmd, "bineval": bineval}
        state["plan"] = '{"tasks": []}'
        state_module.save(state)
        return state

    def _mock_make_verify_pass(self):
        mock_res = unittest.mock.MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = b"All tests passed."
        return mock_res

    @patch("orchestrator.nodes._load_adrs", return_value="")
    @patch("orchestrator.nodes._github_api_request")
    @patch(
        "orchestrator.nodes._get_workspace_diff",
        return_value="diff --git a/f.py b/f.py\n+pass",
    )
    @patch("orchestrator.nodes.subprocess.run")
    def test_bineval_pass_resets_attempts(self, mock_run, _diff, mock_gh, _adrs):
        from orchestrator.nodes import verify_node

        mock_run.return_value = self._mock_make_verify_pass()
        mock_gh.return_value = {"body": "issue body"}
        with patch("orchestrator.nodes._run_bineval", return_value=_all_pass_result()):
            state = self._state(bineval=2)
            new_state = verify_node(state)
        self.assertEqual(new_state["status"], "verifying")
        self.assertEqual(new_state["attempts"]["bineval"], 0)
        self.assertEqual(new_state["attempts"]["verify_cmd"], 0)
        self.assertIsNone(new_state["feedback"])

    @patch("orchestrator.nodes._load_adrs", return_value="")
    @patch("orchestrator.nodes._github_api_request")
    @patch("orchestrator.nodes._get_workspace_diff", return_value="diff content")
    @patch("orchestrator.nodes.subprocess.run")
    def test_bineval_fail_retries_with_structured_feedback(
        self, mock_run, _diff, mock_gh, _adrs
    ):
        from orchestrator.nodes import verify_node

        mock_run.return_value = self._mock_make_verify_pass()
        mock_gh.return_value = {"body": "issue body"}
        fail = _result_with_fail(
            "2.1",
            "Simplicity",
            "No unnecessary abstraction",
            "introduces speculative interface in f.py",
        )
        with patch("orchestrator.nodes._run_bineval", return_value=fail):
            state = self._state(bineval=0)
            new_state = verify_node(state)
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["attempts"]["bineval"], 1)
        self.assertIsNotNone(new_state["feedback"])
        assert new_state["feedback"] is not None
        self.assertIn("[2.1]", new_state["feedback"])
        self.assertIn("speculative interface", new_state["feedback"])

    @patch("orchestrator.nodes._load_adrs", return_value="")
    @patch("orchestrator.nodes._github_api_request")
    @patch("orchestrator.nodes._get_workspace_diff", return_value="diff content")
    @patch("orchestrator.nodes.subprocess.run")
    def test_bineval_fail_exhaustion_transitions_failed(
        self, mock_run, _diff, mock_gh, _adrs
    ):
        from orchestrator.nodes import verify_node

        mock_run.return_value = self._mock_make_verify_pass()
        mock_gh.return_value = {"body": "issue body"}
        fail = _result_with_fail(
            "4.2", "Robustness", "No regression risk", "removes safety check"
        )
        with patch("orchestrator.nodes._run_bineval", return_value=fail):
            state = self._state(bineval=2)
            new_state = verify_node(state)
        self.assertEqual(new_state["status"], "failed")
        self.assertEqual(new_state["attempts"]["bineval"], 3)
        assert new_state["feedback"] is not None
        self.assertIn("[4.2]", new_state["feedback"])

    @patch("orchestrator.nodes._load_adrs", return_value="")
    @patch("orchestrator.nodes._github_api_request")
    @patch("orchestrator.nodes._get_workspace_diff", return_value="diff content")
    @patch("orchestrator.nodes.subprocess.run")
    def test_bineval_llm_failure_treated_as_pass(self, mock_run, _diff, mock_gh, _adrs):
        from orchestrator.nodes import verify_node

        mock_run.return_value = self._mock_make_verify_pass()
        mock_gh.return_value = {"body": "issue body"}
        with patch("orchestrator.nodes._run_bineval", return_value=None):
            state = self._state(bineval=1)
            new_state = verify_node(state)
        self.assertEqual(new_state["status"], "verifying")
        self.assertEqual(new_state["attempts"]["bineval"], 0)
        self.assertIsNone(new_state["feedback"])

    @patch("orchestrator.nodes._github_api_request")
    @patch("orchestrator.nodes._get_workspace_diff", return_value="")
    @patch("orchestrator.nodes.subprocess.run")
    def test_bineval_skipped_on_empty_diff(self, mock_run, _diff, mock_gh):
        from orchestrator.nodes import verify_node

        mock_run.return_value = self._mock_make_verify_pass()
        state = self._state(bineval=2)
        new_state = verify_node(state)
        # Empty diff -> skip BinEval -> PASS path -> reset + PR transition.
        self.assertEqual(new_state["status"], "verifying")
        self.assertEqual(new_state["attempts"]["bineval"], 0)
        self.assertIsNone(new_state["feedback"])
        # BinEval never reached the issue fetch.
        mock_gh.assert_not_called()

    @patch("orchestrator.nodes._load_adrs", return_value="")
    @patch("orchestrator.nodes._github_api_request")
    @patch("orchestrator.nodes.subprocess.run")
    def test_bineval_phase_enriches_diff_before_grading(self, mock_run, mock_gh, _adrs):
        """AC: _run_bineval_phase enriches the diff with function context."""
        from orchestrator.nodes import verify_node

        # Write a Python file in the workspace so enrichment can find it
        (self.workspace_dir / "target.py").write_text(
            "def my_func():\n    return 42\n", encoding="utf-8"
        )

        mock_run.return_value = self._mock_make_verify_pass()
        mock_gh.return_value = {"body": "issue body"}

        # Return a diff that references the workspace file
        diff_with_hunk = (
            "diff --git a/target.py b/target.py\n"
            "--- a/target.py\n"
            "+++ b/target.py\n"
            "@@ -1,1 +1,1 @@\n"
        )
        with patch(
            "orchestrator.nodes._get_workspace_diff", return_value=diff_with_hunk
        ):
            captured_diffs: list[str] = []

            def capture_bineval(issue_body, plan, diff, adrs):
                captured_diffs.append(diff)
                return _all_pass_result()

            with patch("orchestrator.nodes._run_bineval", side_effect=capture_bineval):
                state = self._state(bineval=0)
                new_state = verify_node(state)

        # The diff passed to _run_bineval must be enriched
        self.assertEqual(len(captured_diffs), 1)
        self.assertIn("=== ENCLOSING FUNCTION CONTEXT ===", captured_diffs[0])
        self.assertIn("--- target.py :: my_func ---", captured_diffs[0])
        self.assertEqual(new_state["status"], "verifying")

    def test_apply_no_adr_autopass_forces_adr_checks_pass(self):
        from orchestrator.nodes import _apply_no_adr_autopass

        result = _result_with_fail(
            "3.1", "ADR Compliance", "Complies with ADRs", "violates ADR-0009"
        )
        _apply_no_adr_autopass(result)
        for check in result.checks:
            if check.dimension == "ADR Compliance":
                self.assertTrue(check.passed)
        # Non-ADR checks are untouched.
        non_adr = [c for c in result.checks if c.dimension != "ADR Compliance"]
        self.assertTrue(all(c.passed for c in non_adr))

    @patch("orchestrator.nodes._load_adrs", return_value="")
    @patch("orchestrator.nodes._github_api_request")
    @patch("orchestrator.nodes._get_workspace_diff", return_value="diff content")
    @patch("orchestrator.nodes.subprocess.run")
    def test_bineval_not_starved_by_make_verify_failures(
        self, mock_run, _diff, mock_gh, _adrs
    ):
        """AC1 (issue #123): a BinEval FAIL after >=1 prior make-verify failures
        leaves at least one retry that injects BinEval structured feedback.

        Reproduces the 2026-08-14 sequence: verify-fail, verify-fail,
        verify-pass+bineval-fail. Under the old shared counter this transitioned
        to 'failed' (3/3) and the Worker never saw BinEval feedback. With the
        split per-gate counters (ADR-0047) the BinEval budget is independent, so
        the issue stays 'executing' with structured feedback injected.
        """
        from orchestrator.nodes import verify_node

        fail_res = unittest.mock.MagicMock(returncode=1, stdout=b"make: *** No rule")
        pass_res = self._mock_make_verify_pass()
        mock_run.side_effect = [fail_res, fail_res, pass_res]
        mock_gh.return_value = {"body": "issue body"}
        fail = _result_with_fail(
            "2.1", "Simplicity", "No unnecessary abstraction", "speculative iface"
        )

        state = self._state()
        with patch("orchestrator.nodes._run_bineval", return_value=fail):
            # Attempt 1: make verify fails -> verify_cmd=1, executing.
            state = verify_node(state)
            self.assertEqual(state["status"], "executing")
            self.assertEqual(state["attempts"]["verify_cmd"], 1)
            self.assertEqual(state["attempts"]["bineval"], 0)
            # Attempt 2: make verify fails again -> verify_cmd=2, executing.
            state = verify_node(state)
            self.assertEqual(state["status"], "executing")
            self.assertEqual(state["attempts"]["verify_cmd"], 2)
            self.assertEqual(state["attempts"]["bineval"], 0)
            # Attempt 3: make verify passes, BinEval FAILs -> bineval=1, STILL
            # executing (not failed) with structured BinEval feedback injected.
            state = verify_node(state)
        self.assertEqual(state["status"], "executing")
        self.assertEqual(state["attempts"]["verify_cmd"], 2)
        self.assertEqual(state["attempts"]["bineval"], 1)
        assert state["feedback"] is not None
        self.assertIn("[2.1]", state["feedback"])
        self.assertIn("speculative iface", state["feedback"])

    @patch("orchestrator.nodes._load_adrs", return_value="")
    @patch("orchestrator.nodes._github_api_request")
    @patch("orchestrator.nodes._get_workspace_diff", return_value="diff content")
    @patch("orchestrator.nodes.subprocess.run")
    def test_mixed_sequence_counters_independent_and_bounded(
        self, mock_run, _diff, mock_gh, _adrs
    ):
        """Edge case (issue #123): a mixed verify-fail / bineval-fail sequence
        increments each gate's counter independently, and the total stays
        bounded — here make-verify exhausts its own cap (3) and transitions to
        'failed' even though BinEval was never exhausted.
        """
        from orchestrator.nodes import verify_node

        fail_res = unittest.mock.MagicMock(returncode=1, stdout=b"AssertionError")
        pass_res = self._mock_make_verify_pass()
        # Sequence: vf, vp(bf), vf, vf  -> verify_cmd 1,2,3 (failed); bineval 1.
        mock_run.side_effect = [fail_res, pass_res, fail_res, fail_res]
        mock_gh.return_value = {"body": "issue body"}
        bineval_fail = _result_with_fail(
            "4.2", "Robustness", "No regression risk", "removes safety check"
        )

        state = self._state()
        with patch("orchestrator.nodes._run_bineval", return_value=bineval_fail):
            state = verify_node(state)  # vf -> verify_cmd=1
            self.assertEqual(state["status"], "executing")
            state = verify_node(state)  # vp+bf -> bineval=1
            self.assertEqual(state["status"], "executing")
            self.assertEqual(state["attempts"]["bineval"], 1)
            state = verify_node(state)  # vf -> verify_cmd=2
            self.assertEqual(state["status"], "executing")
            self.assertEqual(state["attempts"]["verify_cmd"], 2)
            state = verify_node(state)  # vf -> verify_cmd=3 -> failed
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["attempts"]["verify_cmd"], 3)
        self.assertEqual(state["attempts"]["bineval"], 1)


class TestBinEvalHelpers(unittest.TestCase):
    """Direct unit tests for the BinEval helper functions that the phase-level
    tests patch out: _load_grading_rubric, _load_adrs, _run_bineval.
    """

    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()

    def tearDown(self):
        self.workspace_temp.cleanup()

    # -- _load_grading_rubric -------------------------------------------------

    def test_load_grading_rubric_returns_file_contents(self):
        from orchestrator import nodes

        with patch.object(nodes, "_RUBRIC_PATH", self.workspace_dir / "rubric.md"):
            (self.workspace_dir / "rubric.md").write_text(
                "# RUBRIC BODY\n", encoding="utf-8"
            )
            self.assertIn("# RUBRIC BODY", nodes._load_grading_rubric())

    def test_load_grading_rubric_missing_file_returns_empty(self):
        from orchestrator import nodes

        with patch.object(nodes, "_RUBRIC_PATH", self.workspace_dir / "missing.md"):
            self.assertEqual(nodes._load_grading_rubric(), "")

    # -- _load_adrs -----------------------------------------------------------

    def test_load_adrs_no_adr_directory_returns_empty(self):
        from orchestrator.nodes import _load_adrs

        self.assertEqual(_load_adrs(self.workspace_dir), "")

    def test_load_adrs_empty_directory_returns_empty(self):
        from orchestrator.nodes import _load_adrs

        (self.workspace_dir / "docs" / "adr").mkdir(parents=True)
        self.assertEqual(_load_adrs(self.workspace_dir), "")

    def test_load_adrs_concatenates_sorted_files(self):
        from orchestrator.nodes import _load_adrs

        adr_dir = self.workspace_dir / "docs" / "adr"
        adr_dir.mkdir(parents=True)
        (adr_dir / "0002-second.md").write_text("SECOND BODY", encoding="utf-8")
        (adr_dir / "0001-first.md").write_text("FIRST BODY", encoding="utf-8")

        adrs = _load_adrs(self.workspace_dir)
        # Sorted by filename: 0001 before 0002.
        self.assertLess(adrs.index("0001-first.md"), adrs.index("0002-second.md"))
        self.assertIn("FIRST BODY", adrs)
        self.assertIn("SECOND BODY", adrs)

    def test_load_adrs_truncates_at_limit_with_marker(self):
        from orchestrator import nodes
        from orchestrator.nodes import _load_adrs

        adr_dir = self.workspace_dir / "docs" / "adr"
        adr_dir.mkdir(parents=True)
        # Force a small limit so truncation triggers on a single large ADR.
        with patch.object(nodes, "_BINEVAL_ADR_MAX_CHARS", 200):
            big = "X" * 500
            (adr_dir / "0001-big.md").write_text(big, encoding="utf-8")
            adrs = _load_adrs(self.workspace_dir)
            self.assertIn("[ADRs truncated: exceeded limit]", adrs)
            # Output is bounded around the limit (not the full 500 chars).
            self.assertLessEqual(len(adrs), 200 + 80)

    def test_load_adrs_skips_unreadable_file(self):
        from orchestrator.nodes import _load_adrs

        adr_dir = self.workspace_dir / "docs" / "adr"
        adr_dir.mkdir(parents=True)
        good = adr_dir / "0001-good.md"
        good.write_text("GOOD BODY", encoding="utf-8")
        bad = adr_dir / "0002-bad.md"
        bad.write_text("BAD BODY", encoding="utf-8")

        with patch("pathlib.Path.read_text", side_effect=OSError("boom")):
            # When every read raises, _load_adrs returns "" (all files skipped).
            self.assertEqual(_load_adrs(self.workspace_dir), "")

    # -- _run_bineval ---------------------------------------------------------

    def _fake_llm(self, invoke_return):
        """Build a fake LLM whose invoke() captures its prompt and returns the
        configured value wrapped in an include_raw=True dict, mimicking
        with_structured_output(..., include_raw=True).invoke(prompt).
        """
        captured = {}

        def invoke(prompt):
            captured["prompt"] = prompt
            return _raw_structured_response(invoke_return)

        llm = unittest.mock.MagicMock()
        structured = unittest.mock.MagicMock()
        structured.invoke.side_effect = invoke
        llm.with_structured_output.return_value = structured
        return llm, captured

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_returns_parsed_result_on_success(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m"}
        llm, captured = self._fake_llm(_all_pass_result())
        mock_get_llm.return_value = llm

        result = _run_bineval("issue body", "plan text", "diff content", "ADR TEXT")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(all(c.passed for c in result.checks))
        # Prompt assembly: rubric, issue body, plan, adrs, diff all injected.
        prompt = captured["prompt"]
        self.assertIn("RUBRIC", prompt)
        self.assertIn("issue body", prompt)
        self.assertIn("plan text", prompt)
        self.assertIn("ADR TEXT", prompt)
        self.assertIn("diff content", prompt)
        # Security preamble present.
        self.assertIn("untrusted user input", prompt)

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_no_adrs_placeholder_in_prompt(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m"}
        llm, captured = self._fake_llm(_all_pass_result())
        mock_get_llm.return_value = llm

        _run_bineval("issue body", "plan", "diff", "")
        self.assertIn("checks 3.1 and 3.2 auto-pass", captured["prompt"])

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_empty_diff_renders_empty_marker(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m"}
        llm, captured = self._fake_llm(_all_pass_result())
        mock_get_llm.return_value = llm

        _run_bineval("issue body", "plan", "", "ADR TEXT")
        self.assertIn("=== DIFF ===\n(empty)", captured["prompt"])

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_truncates_large_diff(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        from orchestrator import nodes
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m"}
        llm, captured = self._fake_llm(_all_pass_result())
        mock_get_llm.return_value = llm

        # Diff larger than the configured limit gets truncated with a marker.
        big_diff = "D" * 50
        with patch.object(nodes, "_BINEVAL_DIFF_MAX_CHARS", 20):
            _run_bineval("issue body", "plan", big_diff, "ADR TEXT")
        prompt = captured["prompt"]
        self.assertIn("exceeded 20 char limit", prompt)
        # The full 50-char diff is not present in its entirety.
        self.assertNotIn(big_diff, prompt)

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_llm_exception_returns_none(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m"}
        llm = unittest.mock.MagicMock()
        llm.with_structured_output.return_value.invoke.side_effect = RuntimeError(
            "API down"
        )
        mock_get_llm.return_value = llm

        self.assertIsNone(_run_bineval("issue body", "plan", "diff", "ADR TEXT"))

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_empty_checks_returns_none(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        from orchestrator.nodes import BinEvalResult, _run_bineval

        mock_resolve.return_value = {"model": "m"}
        llm, _ = self._fake_llm(BinEvalResult(checks=[], summary="empty"))
        mock_get_llm.return_value = llm

        self.assertIsNone(_run_bineval("issue body", "plan", "diff", "ADR TEXT"))

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_uses_strict_and_include_raw(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        """AC: with_structured_output is called with strict=True and include_raw=True."""
        from orchestrator.nodes import BinEvalResult, _run_bineval

        mock_resolve.return_value = {"model": "m"}
        llm, _ = self._fake_llm(_all_pass_result())
        mock_get_llm.return_value = llm

        _run_bineval("issue body", "plan", "diff", "ADR TEXT")

        llm.with_structured_output.assert_called_once_with(
            BinEvalResult, strict=True, include_raw=True
        )

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_parse_failure_returns_none(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        """AC: BinEval truncation is distinguishable from soft-gate PASS —
        a persistent length-limit parse failure returns None (soft-gate PASS)
        after exactly one budget retry, and the finish_reason is logged."""
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m", "max_tokens": 8192}
        llm = unittest.mock.MagicMock()
        structured = unittest.mock.MagicMock()
        # Both the initial call and the length-limit retry return a truncated
        # response — the retry must not loop beyond a single attempt.
        structured.invoke.side_effect = [
            _raw_structured_response(
                None, finish_reason="length", parsing_error=ValueError("truncated")
            ),
            _raw_structured_response(
                None, finish_reason="length", parsing_error=ValueError("truncated")
            ),
        ]
        llm.with_structured_output.return_value = structured
        mock_get_llm.return_value = llm

        # Persistent length failure still soft-gates to PASS (returns None).
        self.assertIsNone(_run_bineval("issue body", "plan", "diff", "ADR TEXT"))
        # Exactly one retry: the initial call + a single enlarged-budget retry.
        self.assertEqual(structured.invoke.call_count, 2)

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_length_retry_succeeds(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        """AC: a length-limit truncation is recovered by the single
        enlarged-budget retry — the verdict is parsed on the second call."""
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m", "max_tokens": 8192}
        llm = unittest.mock.MagicMock()
        structured = unittest.mock.MagicMock()
        structured.invoke.side_effect = [
            _raw_structured_response(
                None, finish_reason="length", parsing_error=ValueError("truncated")
            ),
            _raw_structured_response(_all_pass_result()),
        ]
        llm.with_structured_output.return_value = structured
        mock_get_llm.return_value = llm

        result = _run_bineval("issue body", "plan", "diff", "ADR TEXT")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(all(c.passed for c in result.checks))
        # Initial call + exactly one retry.
        self.assertEqual(structured.invoke.call_count, 2)
        # The retry used an enlarged max_tokens budget (multiplier * base).
        retry_cfg = mock_get_llm.call_args_list[1].args[0]
        self.assertGreater(retry_cfg["max_tokens"], 8192)

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_non_length_failure_no_retry(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        """AC: only finish_reason=length triggers the budget retry. A parse
        failure with a different finish_reason (e.g. stop) degrades to PASS
        immediately with no retry — non-reasoning models are unaffected."""
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m", "max_tokens": 8192}
        llm = unittest.mock.MagicMock()
        structured = unittest.mock.MagicMock()
        structured.invoke.return_value = _raw_structured_response(
            None, finish_reason="stop", parsing_error=ValueError("malformed")
        )
        llm.with_structured_output.return_value = structured
        mock_get_llm.return_value = llm

        self.assertIsNone(_run_bineval("issue body", "plan", "diff", "ADR TEXT"))
        # No retry: a single invoke call.
        self.assertEqual(structured.invoke.call_count, 1)

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_length_retry_does_not_consume_semantic_budget(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        """AC (ADR-0047): the length-limit budget retry must NOT consume the
        BinEval semantic-retry budget. _run_bineval is a pure function — it
        never touches state['attempts']; only a genuine BinEval FAIL does
        (in _run_bineval_phase). A length-limit degradation returns None,
        which _run_bineval_phase treats as a degraded PASS (no increment)."""
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m", "max_tokens": 8192}
        llm = unittest.mock.MagicMock()
        structured = unittest.mock.MagicMock()
        structured.invoke.side_effect = [
            _raw_structured_response(
                None, finish_reason="length", parsing_error=ValueError("truncated")
            ),
            _raw_structured_response(
                None, finish_reason="length", parsing_error=ValueError("truncated")
            ),
        ]
        llm.with_structured_output.return_value = structured
        mock_get_llm.return_value = llm

        # _run_bineval returns None (degraded PASS) on persistent length
        # failure — it carries no state, so there is no semantic-budget
        # counter for the caller to mis-increment from this path.
        self.assertIsNone(_run_bineval("issue body", "plan", "diff", "ADR TEXT"))

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_config_resolution_failure_degrades_to_pass(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        """AC (ADR-0027 soft-gate contract): a config-resolution failure
        (e.g. malformed factory.json) must degrade to PASS (return None),
        not propagate as an exception. resolve_model_config is part of the
        soft-gate surface."""
        from orchestrator.nodes import _run_bineval

        mock_resolve.side_effect = RuntimeError("malformed factory.json")

        # Must not raise — degrades to None (soft-gate PASS).
        self.assertIsNone(_run_bineval("issue body", "plan", "diff", "ADR TEXT"))
        # The LLM was never constructed (config resolution failed first).
        mock_get_llm.assert_not_called()


class TestBinevalRetryConfig(unittest.TestCase):
    """Unit tests for _bineval_retry_config (issue #134 coverage)."""

    def test_multiplier_enlarges_budget(self):
        from orchestrator.nodes import _bineval_retry_config

        cfg = {"model": "m", "max_tokens": 8192, "temperature": 0.0}
        retry = _bineval_retry_config(cfg)
        self.assertEqual(retry["max_tokens"], 8192 * 2)
        # Other fields inherited unchanged.
        self.assertEqual(retry["model"], "m")
        self.assertEqual(retry["temperature"], 0.0)

    def test_uses_default_when_no_max_tokens(self):
        from orchestrator.nodes import _bineval_retry_config

        cfg = {"model": "m", "temperature": 0.0}
        retry = _bineval_retry_config(cfg)
        # No max_tokens in cfg -> base is the default (4096).
        self.assertEqual(retry["max_tokens"], 4096 * 2)

    def test_cap_applied(self):
        from orchestrator.nodes import _bineval_retry_config

        # A large base that would exceed the cap (16384) when doubled.
        cfg = {"model": "m", "max_tokens": 20000}
        retry = _bineval_retry_config(cfg)
        self.assertEqual(retry["max_tokens"], 16384)

    def test_original_cfg_not_mutated(self):
        from orchestrator.nodes import _bineval_retry_config

        cfg = {"model": "m", "max_tokens": 8192}
        _bineval_retry_config(cfg)
        # The original dict is unchanged (shallow copy).
        self.assertEqual(cfg["max_tokens"], 8192)


def _raise_length_limit_error():
    """Raise the production-resolved LengthFinishReasonError instance."""
    import orchestrator.nodes as nodes

    # The resolved class is what _invoke_bineval_structured catches; construct
    # it the way the OpenAI SDK does (keyword-only completion argument).
    error_cls = nodes._LENGTH_FINISH_REASON_ERRORS[0]
    return error_cls(completion=MagicMock())


@unittest.skipUnless(
    len(__import__("orchestrator.nodes", fromlist=["obj"])._LENGTH_FINISH_REASON_ERRORS)
    > 0,
    "openai SDK does not expose LengthFinishReasonError",
)
class TestBinEvalLengthErrorRetry(unittest.TestCase):
    """Unit tests for the raised-exception truncation path (issue #138).

    In production the OpenAI SDK's structured-output parse helper RAISES
    ``LengthFinishReasonError`` on finish_reason=length instead of returning a
    raw response, so ADR-0049's retry only fires if
    ``_invoke_bineval_structured`` translates the exception into the
    ``finish_reason="length"`` signal.
    """

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_invoke_bineval_length_error_returns_length_signal(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        """AC: a raised LengthFinishReasonError yields (None, 'length')."""
        from orchestrator.nodes import _invoke_bineval_structured

        mock_resolve.return_value = {"model": "m", "max_tokens": 8192}
        llm = unittest.mock.MagicMock()
        llm.with_structured_output.return_value.invoke.side_effect = (
            _raise_length_limit_error()
        )
        mock_get_llm.return_value = llm

        result, finish_reason = _invoke_bineval_structured({}, "prompt")
        self.assertIsNone(result)
        self.assertEqual(finish_reason, "length")

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_invoke_bineval_generic_exception_returns_none_signal(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        """AC: non-length API exceptions still yield (None, None) — no retry."""
        from orchestrator.nodes import _invoke_bineval_structured

        mock_resolve.return_value = {"model": "m", "max_tokens": 8192}
        llm = unittest.mock.MagicMock()
        llm.with_structured_output.return_value.invoke.side_effect = RuntimeError(
            "rate limit"
        )
        mock_get_llm.return_value = llm

        result, finish_reason = _invoke_bineval_structured({}, "prompt")
        self.assertIsNone(result)
        self.assertIsNone(finish_reason)

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_length_error_retry_fires(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        """AC: first call raises LengthFinishReasonError, the single enlarged-
        budget retry succeeds -> verdict returned, two calls total."""
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m", "max_tokens": 8192}
        llm = unittest.mock.MagicMock()
        structured = unittest.mock.MagicMock()
        structured.invoke.side_effect = [
            _raise_length_limit_error(),
            _raw_structured_response(_all_pass_result()),
        ]
        llm.with_structured_output.return_value = structured
        mock_get_llm.return_value = llm

        result = _run_bineval("issue body", "plan", "diff", "ADR TEXT")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(all(c.passed for c in result.checks))
        # Initial call + exactly one budget retry.
        self.assertEqual(structured.invoke.call_count, 2)
        # The retry used an enlarged max_tokens budget.
        retry_cfg = mock_get_llm.call_args_list[1].args[0]
        self.assertGreater(retry_cfg["max_tokens"], 8192)

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_length_error_retry_exhausted_degrades_to_pass(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        """AC: retry also raises LengthFinishReasonError -> soft-gate PASS
        (None) after exactly one retry, warning carries the retry's
        finish_reason='length'."""
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m", "max_tokens": 8192}
        llm = unittest.mock.MagicMock()
        structured = unittest.mock.MagicMock()
        structured.invoke.side_effect = [
            _raise_length_limit_error(),
            _raise_length_limit_error(),
        ]
        llm.with_structured_output.return_value = structured
        mock_get_llm.return_value = llm

        with self.assertLogs("orchestrator.nodes", level="WARNING") as logs:
            result = _run_bineval("issue body", "plan", "diff", "ADR TEXT")
        self.assertIsNone(result)
        # Exactly one retry: initial call + a single enlarged-budget retry.
        self.assertEqual(structured.invoke.call_count, 2)
        degradation = [
            line for line in logs.output if "length-limit retry still failed" in line
        ]
        self.assertTrue(degradation)
        self.assertIn("finish_reason=length", degradation[0])

    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    def test_run_bineval_non_length_exception_no_retry(
        self, _rubric, mock_resolve, mock_get_llm
    ):
        """AC: network/auth/rate-limit exceptions degrade to PASS via the
        generic path with NO retry — a single invoke call."""
        from orchestrator.nodes import _run_bineval

        mock_resolve.return_value = {"model": "m", "max_tokens": 8192}
        llm = unittest.mock.MagicMock()
        structured = unittest.mock.MagicMock()
        structured.invoke.side_effect = RuntimeError("API down")
        llm.with_structured_output.return_value = structured
        mock_get_llm.return_value = llm

        self.assertIsNone(_run_bineval("issue body", "plan", "diff", "ADR TEXT"))
        self.assertEqual(structured.invoke.call_count, 1)


@unittest.skipUnless(
    len(__import__("orchestrator.nodes", fromlist=["obj"])._LENGTH_FINISH_REASON_ERRORS)
    > 0,
    "openai SDK does not expose LengthFinishReasonError",
)
class TestBinEvalLengthErrorRetryPhase(unittest.TestCase):
    """Phase-level coverage for the raised-exception truncation path.

    Drives verify_node's BinEval phase through the REAL ``_run_bineval`` while
    the LLM raises ``LengthFinishReasonError``, asserting the soft-gate
    contract: degrade to PASS with bineval_degraded=True and NO semantic
    attempt consumed (attempts['bineval'] reset to 0, never incremented).
    """

    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()
        self.original_env = {}
        for k, v in {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "AGENT_MODE": "local",
            "AGENT_VERIFY_COMMAND": None,
        }.items():
            self.original_env[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()

    def _state(self, bineval=1):
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["attempts"] = {"verify_cmd": 0, "bineval": bineval}
        state["plan"] = '{"tasks": []}'
        state_module.save(state)
        return state

    @patch("orchestrator.nodes._load_grading_rubric", return_value="RUBRIC")
    @patch("orchestrator.nodes._load_adrs", return_value="")
    @patch("orchestrator.nodes.resolve_model_config")
    @patch("orchestrator.nodes.get_chat_model_from_config")
    @patch("orchestrator.nodes._github_api_request")
    @patch(
        "orchestrator.nodes._get_workspace_diff",
        return_value="diff --git a/f.py b/f.py\n+pass",
    )
    @patch("orchestrator.nodes.subprocess.run")
    def test_persistent_length_error_degrades_pass_without_semantic_attempt(
        self, mock_run, _diff, mock_gh, mock_get_llm, mock_resolve, _adrs, _rubric
    ):
        from orchestrator.nodes import verify_node

        mock_res = unittest.mock.MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = b"All tests passed."
        mock_run.return_value = mock_res
        mock_gh.return_value = {"body": "issue body"}
        mock_resolve.return_value = {"model": "m", "max_tokens": 8192}
        llm = unittest.mock.MagicMock()
        structured = unittest.mock.MagicMock()
        structured.invoke.side_effect = [
            _raise_length_limit_error(),
            _raise_length_limit_error(),
        ]
        llm.with_structured_output.return_value = structured
        mock_get_llm.return_value = llm

        state = self._state(bineval=1)
        new_state = verify_node(state)

        # Soft-gate degradation, not a semantic retry.
        self.assertEqual(new_state["status"], "verifying")
        self.assertTrue(new_state["bineval_degraded"])
        # ADR-0047: the budget retry must not consume the semantic counter —
        # it was reset by success semantics, NOT incremented past its input.
        self.assertEqual(new_state["attempts"]["bineval"], 0)
        self.assertIsNone(new_state["feedback"])


class TestPRNode(unittest.TestCase):
    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()

        self.original_env = {}
        vars_to_set = {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "AGENT_MODE": "local",
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v

        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(self.workspace_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(self.workspace_dir),
            check=True,
        )

        self.initial_file = self.workspace_dir / "README.md"
        self.initial_file.write_text("# Test Repo", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"], cwd=str(self.workspace_dir), check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=str(self.workspace_dir),
            check=True,
        )

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()

    @patch("orchestrator.nodes.push")
    @patch("orchestrator.nodes.commit")
    @patch("orchestrator.nodes._github_api_request")
    def test_pr_node_creates_pr(self, mock_api, mock_commit, mock_push):
        # Setup mocks
        def api_side_effect(method, path, body=None):
            if method == "GET":
                if "/pulls" in path:
                    return []
                elif "/issues/10" in path:
                    return {"title": "Fix a bug"}
            elif method == "POST" and "/pulls" in path:
                return {"html_url": "https://github.com/test-owner/test-repo/pull/1"}
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import pr_node

        new_state = pr_node(state)

        self.assertEqual(new_state["status"], "pr_open")
        self.assertEqual(new_state["phase"], "pr_open")

        # Verify POST request was called
        post_calls = [
            call
            for call in mock_api.mock_calls
            if call[1][0] == "POST" and "/pulls" in call[1][1]
        ]
        self.assertTrue(len(post_calls) > 0)

        # Verify commit and push mock calls
        mock_commit.assert_called_once()
        mock_push.assert_called_once()

    @patch("orchestrator.nodes.push")
    @patch("orchestrator.nodes.commit")
    @patch("orchestrator.nodes._github_api_request")
    def test_pr_node_enriches_body_with_plan_and_coverage(
        self, mock_api, mock_commit, mock_push
    ):
        """pr_node builds an enriched body from plan rationale + verify output.

        With a serialized DevelopmentPlan and a captured verify_output containing
        a pytest-cov term-missing table, the PR body surfaces a Summary, Key
        Changes, and Test Coverage section (issue #93, AC1). The Closes marker
        is preserved (AC4).
        """
        plan = json.dumps(
            {
                "rationale": "Wire the new signal end-to-end.",
                "tasks": [],
                "requested_files": [],
            }
        )
        coverage = (
            "collected 10 items\n"
            "Name                          Stmts   Miss   Cover   Missing\n"
            "-----------------------------------------------------------\n"
            "orchestrator/nodes.py           1234     56    95%    45-50\n"
            "TOTAL                           1234     56    95%\n"
        )

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if "/pulls" in path:
                    return []
                if "/issues/10" in path:
                    return {"title": "Fix a bug"}
            if method == "POST" and "/pulls" in path:
                return {"html_url": "https://github.com/test-owner/test-repo/pull/1"}
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state["plan"] = plan
        state["verify_output"] = coverage
        state_module.save(state)

        from orchestrator.nodes import pr_node

        pr_node(state)

        post_calls = [
            call
            for call in mock_api.mock_calls
            if call[1][0] == "POST" and "/pulls" in call[1][1]
        ]
        self.assertTrue(len(post_calls) > 0)
        body = post_calls[0].args[2]["body"]
        self.assertIn("Closes #10", body)
        self.assertIn("## Summary", body)
        self.assertIn("Wire the new signal end-to-end.", body)
        self.assertIn("## Key Changes", body)
        self.assertIn("## Test Coverage", body)
        self.assertIn("TOTAL", body)

    @patch("orchestrator.nodes.push")
    @patch("orchestrator.nodes.commit")
    @patch("orchestrator.nodes._github_api_request")
    def test_pr_node_body_degrades_when_plan_and_coverage_absent(
        self, mock_api, mock_commit, mock_push
    ):
        """A missing plan/coverage degrades to notes; the PR is still created (AC6)."""
        # TestPRNode.setUp created a single initial commit on main with no branch
        # divergence, so `git diff --stat origin/main...HEAD` is empty here too —
        # exercising the Key Changes degradation path as well.

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if "/pulls" in path:
                    return []
                if "/issues/7" in path:
                    return {"title": "T"}
            if method == "POST" and "/pulls" in path:
                return {"html_url": "https://github.com/test-owner/test-repo/pull/1"}
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 7
        state["branch"] = "feat/issue-7"
        # plan and verify_output intentionally absent (None)
        state_module.save(state)

        from orchestrator.nodes import pr_node

        pr_node(state)

        post_calls = [
            call
            for call in mock_api.mock_calls
            if call[1][0] == "POST" and "/pulls" in call[1][1]
        ]
        self.assertTrue(len(post_calls) > 0)
        body = post_calls[0].args[2]["body"]
        self.assertIn("Closes #7", body)
        self.assertIn("No plan rationale was available", body)
        self.assertIn("No coverage report was produced", body)


class TestPRBodyHelpers(unittest.TestCase):
    """Unit tests for the deterministic PR-body construction helpers (issue #93)."""

    def test_build_pr_body_full(self):
        from orchestrator.nodes import _build_pr_body

        body = _build_pr_body(
            42,
            "Refactor the merge path.",
            "orchestrator/nodes.py | 12 +-\n 1 file changed, 8 insertions(+), 4 deletions(-)",
            "Name                 Stmts  Miss  Cover\nTOTAL                10      1    90%",
        )
        self.assertIn("Closes #42", body)
        self.assertIn("## Summary", body)
        self.assertIn("Refactor the merge path.", body)
        self.assertIn("## Key Changes", body)
        self.assertIn("orchestrator/nodes.py", body)
        self.assertIn("## Test Coverage", body)
        self.assertIn("TOTAL", body)
        # Closes marker precedes the Summary (AC4 ordering preserved).
        self.assertLess(body.index("Closes #42"), body.index("## Summary"))

    def test_build_pr_body_degrades_without_rationale(self):
        from orchestrator.nodes import _build_pr_body

        body = _build_pr_body(
            9, "", "a.py | 1 +\n 1 file changed, 1 insertion(+)", "TOTAL 10 1 90%"
        )
        self.assertIn("No plan rationale was available", body)
        self.assertIn("## Key Changes", body)
        self.assertIn("a.py", body)
        self.assertIn("## Test Coverage", body)
        self.assertIn("TOTAL", body)

    def test_build_pr_body_degrades_without_diff_stat(self):
        from orchestrator.nodes import _build_pr_body

        body = _build_pr_body(9, "Rationale here.", "", "TOTAL 10 1 90%")
        self.assertIn("Rationale here.", body)
        self.assertIn("No changed files were reported", body)
        self.assertIn("## Test Coverage", body)
        self.assertIn("TOTAL", body)

    def test_build_pr_body_degrades_without_coverage(self):
        from orchestrator.nodes import _build_pr_body

        body = _build_pr_body(9, "Rationale here.", "a.py | 1 +", "")
        self.assertIn("No coverage report was produced", body)
        self.assertIn("a.py", body)

    def test_build_pr_body_degrades_all_missing(self):
        from orchestrator.nodes import _build_pr_body

        body = _build_pr_body(9, "", "", "")
        self.assertIn("Closes #9", body)
        self.assertIn("No plan rationale was available", body)
        self.assertIn("No changed files were reported", body)
        self.assertIn("No coverage report was produced", body)
        # Body never raises and ends with a single trailing newline.
        self.assertTrue(body.endswith("\n"))
        self.assertFalse(body.endswith("\n\n"))

    def test_build_pr_body_surfaces_bineval_degraded_notice(self):
        """When BinEval degraded to PASS, the PR body shows a visible notice."""
        from orchestrator.nodes import _build_pr_body

        body = _build_pr_body(9, "Rationale.", "a.py | 1 +", "", bineval_degraded=True)
        self.assertIn("BinEval Notice", body)
        self.assertIn("degraded to PASS", body)

    def test_build_pr_body_omits_notice_when_graded(self):
        """A genuine grade (PASS/FAIL) or None produces no BinEval notice."""
        from orchestrator.nodes import _build_pr_body

        for degraded in (False, None):
            body = _build_pr_body(
                9, "Rationale.", "a.py | 1 +", "", bineval_degraded=degraded
            )
            self.assertNotIn("BinEval Notice", body)

    def test_extract_coverage_tail_returns_table_block(self):
        from orchestrator.nodes import _extract_coverage_tail

        output = (
            "==== test session starts ====\n"
            "collected 5 items\n\n"
            "Name                          Stmts   Miss   Cover   Missing\n"
            "-----------------------------------------------------------\n"
            "orchestrator/nodes.py           100      5    95%    45-50\n"
            "TOTAL                           100      5    95%\n"
            "==== 5 passed ====\n"
        )
        tail = _extract_coverage_tail(output)
        self.assertIn("Name", tail)
        self.assertIn("Stmts", tail)
        self.assertIn("orchestrator/nodes.py", tail)
        self.assertTrue(tail.lstrip().startswith("Name"))
        self.assertTrue(
            tail.rstrip().endswith("TOTAL                           100      5    95%")
        )
        # The trailing pytest summary line is NOT part of the coverage block.
        self.assertNotIn("5 passed", tail)

    def test_extract_coverage_tail_returns_table_without_missing_column(self):
        from orchestrator.nodes import _extract_coverage_tail

        output = (
            "Name                 Stmts  Miss  Cover\n"
            "TOTAL                10     1     90%\n"
        )
        tail = _extract_coverage_tail(output)
        self.assertIn("TOTAL", tail)
        self.assertIn("Stmts", tail)

    def test_extract_coverage_tail_empty_when_no_report(self):
        from orchestrator.nodes import _extract_coverage_tail

        # Plain pytest output with no --cov produces no coverage table.
        self.assertEqual(_extract_coverage_tail("collected 3 items\n3 passed\n"), "")
        self.assertEqual(_extract_coverage_tail(""), "")
        self.assertEqual(_extract_coverage_tail(None), "")

    def test_extract_coverage_tail_handles_table_without_total_line(self):
        from orchestrator.nodes import _extract_coverage_tail

        # A malformed report with no TOTAL line still returns the header block
        # rather than the entire remaining output (bounded by header match).
        output = "Name  Stmts  Miss  Cover\nx.py  1  0  100%\ntrailing\n"
        tail = _extract_coverage_tail(output)
        self.assertIn("Name  Stmts  Miss  Cover", tail)
        self.assertIn("x.py  1  0  100%", tail)
        # No TOTAL present, so the block runs to end of captured output.
        self.assertIn("trailing", tail)

    def test_extract_plan_rationale_valid(self):
        from orchestrator.metrics import extract_plan_rationale

        plan = json.dumps({"rationale": "Because X.", "tasks": []})
        self.assertEqual(extract_plan_rationale(plan), "Because X.")

    def test_extract_plan_rationale_missing_field(self):
        from orchestrator.metrics import extract_plan_rationale

        self.assertEqual(extract_plan_rationale(json.dumps({"tasks": []})), "")

    def test_extract_plan_rationale_non_dict_json(self):
        """A plan that parses to a non-dict (list/scalar) degrades to ''."""
        from orchestrator.metrics import extract_plan_rationale

        self.assertEqual(extract_plan_rationale("[]"), "")
        self.assertEqual(extract_plan_rationale('"a string"'), "")
        self.assertEqual(extract_plan_rationale("123"), "")

    def test_extract_plan_rationale_non_string_rationale(self):
        """A non-string rationale value degrades to '' rather than leaking it."""
        from orchestrator.metrics import extract_plan_rationale

        self.assertEqual(extract_plan_rationale(json.dumps({"rationale": 42})), "")
        self.assertEqual(extract_plan_rationale(json.dumps({"rationale": None})), "")

    def test_extract_plan_rationale_invalid_json(self):
        from orchestrator.metrics import extract_plan_rationale

        self.assertEqual(extract_plan_rationale("not json"), "")
        self.assertEqual(extract_plan_rationale(None), "")


class TestMergeNode(unittest.TestCase):
    @staticmethod
    def _hidden_block_body(verdicts):
        """Build a review body containing the hidden llm-pr-review-verdicts block.

        verdicts maps judge keys to PASS/FAIL/NEEDS REVIEW. A key omitted from
        the dict is omitted from the block (used to test missing-line handling).
        """
        lines = ["<!-- llm-pr-review-verdicts"]
        for k in ["syntax_lint", "test_coverage", "architecture", "security"]:
            v = verdicts.get(k)
            if v is not None:
                lines.append(f"{k}: {v}")
        lines.append("-->")
        return "\n".join(lines)

    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()

        self.original_env = {}
        vars_to_set = {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "AGENT_MODE": "local",
            "AGENT_MERGE_POLL_INTERVAL": "1",
            "AGENT_MERGE_POLL_TIMEOUT": "2",
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v

        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(self.workspace_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(self.workspace_dir),
            check=True,
        )

        self.initial_file = self.workspace_dir / "README.md"
        self.initial_file.write_text("# Test Repo", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"], cwd=str(self.workspace_dir), check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=str(self.workspace_dir),
            check=True,
        )

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_node_success(self, mock_api, mock_commit_time):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": True, "state": "closed"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return []
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        self.assertEqual(new_state["status"], "done")

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_node_blocked_by_security(self, mock_api, mock_commit_time):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": self._hidden_block_body(
                                {
                                    "syntax_lint": "PASS",
                                    "test_coverage": "PASS",
                                    "architecture": "PASS",
                                    "security": "FAIL",
                                }
                            ),
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        # Actionable verdict with budget remaining -> bounded merge-fix retry.
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "merge_fix")
        self.assertEqual(new_state["attempts"].get("merge"), 1)
        self.assertEqual(new_state["attempts"].get("verify_cmd"), 0)
        self.assertEqual(new_state["attempts"].get("bineval"), 0)
        assert new_state["feedback"] is not None
        self.assertIn("merge-fix attempt 1/", new_state["feedback"])
        self.assertIn("security (FAIL)", new_state["feedback"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_node_blocked_by_architecture(self, mock_api, mock_commit_time):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": self._hidden_block_body(
                                {
                                    "syntax_lint": "PASS",
                                    "test_coverage": "PASS",
                                    "architecture": "FAIL",
                                    "security": "PASS",
                                }
                            ),
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        # Actionable verdict with budget remaining -> bounded merge-fix retry.
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "merge_fix")
        self.assertEqual(new_state["attempts"].get("merge"), 1)
        self.assertEqual(new_state["attempts"].get("verify_cmd"), 0)
        self.assertEqual(new_state["attempts"].get("bineval"), 0)
        assert new_state["feedback"] is not None
        self.assertIn("merge-fix attempt 1/", new_state["feedback"])
        self.assertIn("architecture (FAIL)", new_state["feedback"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_node_ignores_untrusted_review(self, mock_api, mock_commit_time):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    # Untrusted user tries to approve, but it is ignored
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": self._hidden_block_body(
                                {
                                    "syntax_lint": "PASS",
                                    "test_coverage": "PASS",
                                    "architecture": "PASS",
                                    "security": "PASS",
                                }
                            ),
                            "user": {"login": "malicious-user"},
                        }
                    ]
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        self.assertEqual(new_state["status"], "failed")
        assert new_state["feedback"] is not None
        self.assertIn("Polling timed out", new_state["feedback"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_node_blocked_by_syntax_lint(self, mock_api, mock_commit_time):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": self._hidden_block_body(
                                {
                                    "syntax_lint": "FAIL",
                                    "test_coverage": "PASS",
                                    "architecture": "PASS",
                                    "security": "PASS",
                                }
                            ),
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        # Actionable verdict with budget remaining -> bounded merge-fix retry.
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "merge_fix")
        self.assertEqual(new_state["attempts"].get("merge"), 1)
        self.assertEqual(new_state["attempts"].get("verify_cmd"), 0)
        self.assertEqual(new_state["attempts"].get("bineval"), 0)
        assert new_state["feedback"] is not None
        self.assertIn("merge-fix attempt 1/", new_state["feedback"])
        self.assertIn("syntax_lint (FAIL)", new_state["feedback"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_node_blocked_by_test_coverage(self, mock_api, mock_commit_time):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": self._hidden_block_body(
                                {
                                    "syntax_lint": "PASS",
                                    "test_coverage": "FAIL",
                                    "architecture": "PASS",
                                    "security": "PASS",
                                }
                            ),
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        # Actionable verdict with budget remaining -> bounded merge-fix retry.
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "merge_fix")
        self.assertEqual(new_state["attempts"].get("merge"), 1)
        self.assertEqual(new_state["attempts"].get("verify_cmd"), 0)
        self.assertEqual(new_state["attempts"].get("bineval"), 0)
        assert new_state["feedback"] is not None
        self.assertIn("merge-fix attempt 1/", new_state["feedback"])
        self.assertIn("test_coverage (FAIL)", new_state["feedback"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_node_blocked_by_needs_review(self, mock_api, mock_commit_time):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": self._hidden_block_body(
                                {
                                    "syntax_lint": "PASS",
                                    "test_coverage": "PASS",
                                    "architecture": "PASS",
                                    "security": "NEEDS REVIEW",
                                }
                            ),
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        # Actionable verdict (NEEDS REVIEW) with budget remaining -> bounded
        # merge-fix retry. A missing verdict line is treated as NEEDS REVIEW.
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "merge_fix")
        self.assertEqual(new_state["attempts"].get("merge"), 1)
        self.assertEqual(new_state["attempts"].get("verify_cmd"), 0)
        self.assertEqual(new_state["attempts"].get("bineval"), 0)
        assert new_state["feedback"] is not None
        self.assertIn("merge-fix attempt 1/", new_state["feedback"])
        self.assertIn("security (NEEDS REVIEW)", new_state["feedback"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_node_all_pass_not_merged_polls(self, mock_api, mock_commit_time):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": self._hidden_block_body(
                                {
                                    "syntax_lint": "PASS",
                                    "test_coverage": "PASS",
                                    "architecture": "PASS",
                                    "security": "PASS",
                                }
                            ),
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        # 4x PASS does not auto-merge; with the PR still open the node polls until timeout.
        self.assertEqual(new_state["status"], "failed")
        assert new_state["feedback"] is not None
        self.assertIn("Polling timed out", new_state["feedback"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_node_stale_review_ignored(self, mock_api, mock_commit_time):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    # Review authored before the reference (push) time -> stale, ignored.
                    return [
                        {
                            "submitted_at": "2026-06-27T11:00:00Z",
                            "body": self._hidden_block_body(
                                {
                                    "syntax_lint": "PASS",
                                    "test_coverage": "PASS",
                                    "architecture": "PASS",
                                    "security": "PASS",
                                }
                            ),
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        # Stale review yields no qualifying fresh block -> polls until timeout.
        self.assertEqual(new_state["status"], "failed")
        assert new_state["feedback"] is not None
        self.assertIn("Polling timed out", new_state["feedback"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_node_missing_verdict_line_blocks(self, mock_api, mock_commit_time):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    # security line omitted -> treated as NEEDS REVIEW -> blocks.
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": self._hidden_block_body(
                                {
                                    "syntax_lint": "PASS",
                                    "test_coverage": "PASS",
                                    "architecture": "PASS",
                                }
                            ),
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        # Actionable verdict (NEEDS REVIEW) with budget remaining -> bounded
        # merge-fix retry. A missing verdict line is treated as NEEDS REVIEW.
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "merge_fix")
        self.assertEqual(new_state["attempts"].get("merge"), 1)
        self.assertEqual(new_state["attempts"].get("verify_cmd"), 0)
        self.assertEqual(new_state["attempts"].get("bineval"), 0)
        assert new_state["feedback"] is not None
        self.assertIn("merge-fix attempt 1/", new_state["feedback"])
        self.assertIn("security (NEEDS REVIEW)", new_state["feedback"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_fix_findings_extracted_into_feedback(
        self, mock_api, mock_commit_time
    ):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        # Review body with actionable [SEVERITY]-tagged finding lines.
        review_body = (
            "## Security\n"
            "- `[CRITICAL]` SQL injection in `foo.py:12` via unsanitized input.\n"
            "- `[WARNING]` Hardcoded secret in `config.py`.\n"
            "<!-- llm-pr-review-verdicts\n"
            "syntax_lint: PASS\n"
            "test_coverage: PASS\n"
            "architecture: PASS\n"
            "security: FAIL\n"
            "-->"
        )

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": review_body,
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        self.assertEqual(new_state["status"], "executing")
        assert new_state["feedback"] is not None
        # Both [SEVERITY]-tagged findings are extracted into the feedback channel.
        self.assertIn("[CRITICAL]", new_state["feedback"])
        self.assertIn("[WARNING]", new_state["feedback"])
        self.assertIn("SQL injection in `foo.py:12`", new_state["feedback"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_fix_cap_exhaustion_posts_comment_and_fails(
        self, mock_api, mock_commit_time
    ):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        posted_comments = []

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": self._hidden_block_body(
                                {
                                    "syntax_lint": "PASS",
                                    "test_coverage": "PASS",
                                    "architecture": "PASS",
                                    "security": "FAIL",
                                }
                            ),
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            elif method == "POST" and path.endswith("/comments"):
                posted_comments.append(body)
                return {}
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        # Budget already exhausted: cap=1 and one merge-fix already attempted.
        state["attempts"] = {"merge": 1, "verify_cmd": 0, "bineval": 0}
        state_module.save(state)

        os.environ["AGENT_PR_FIX_MAX"] = "1"
        try:
            from orchestrator.nodes import merge_node

            new_state = merge_node(state)
        finally:
            os.environ.pop("AGENT_PR_FIX_MAX", None)

        # Cap exhausted -> escalation comment posted, then recovery.
        self.assertEqual(new_state["status"], "failed")
        self.assertEqual(len(posted_comments), 1)
        self.assertIn("Merge-fix budget exhausted", posted_comments[0]["body"])
        self.assertIn("security (FAIL)", posted_comments[0]["body"])
        assert new_state["feedback"] is not None
        self.assertIn("Merge-fix budget exhausted", new_state["feedback"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_fix_success_resets_merge_counter(self, mock_api, mock_commit_time):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": True, "state": "closed"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return []
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        # Simulate a prior merge-fix cycle whose PR then merged.
        state["attempts"] = {"merge": 2, "verify_cmd": 0, "bineval": 0}
        state_module.save(state)

        from orchestrator.nodes import merge_node

        new_state = merge_node(state)

        self.assertEqual(new_state["status"], "done")
        self.assertEqual(new_state["attempts"].get("merge"), 0)

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_fix_escalation_comment_includes_findings(
        self, mock_api, mock_commit_time
    ):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        review_body = (
            "## Security\n"
            "- `[CRITICAL]` SQL injection in `foo.py:12`.\n"
            "<!-- llm-pr-review-verdicts\n"
            "syntax_lint: PASS\n"
            "test_coverage: PASS\n"
            "architecture: PASS\n"
            "security: FAIL\n"
            "-->"
        )
        posted_comments = []

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": review_body,
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            elif method == "POST" and path.endswith("/comments"):
                posted_comments.append(body)
                return {}
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state["attempts"] = {"merge": 1, "verify_cmd": 0, "bineval": 0}
        state_module.save(state)

        os.environ["AGENT_PR_FIX_MAX"] = "1"
        try:
            from orchestrator.nodes import merge_node

            new_state = merge_node(state)
        finally:
            os.environ.pop("AGENT_PR_FIX_MAX", None)

        self.assertEqual(new_state["status"], "failed")
        self.assertEqual(len(posted_comments), 1)
        # The escalation comment includes the extracted [SEVERITY] findings.
        self.assertIn("Unresolved findings:", posted_comments[0]["body"])
        self.assertIn("[CRITICAL]", posted_comments[0]["body"])

    @patch("orchestrator.nodes.get_commit_time")
    @patch("orchestrator.nodes._github_api_request")
    def test_merge_fix_escalation_tolerates_comment_post_failure(
        self, mock_api, mock_commit_time
    ):
        mock_commit_time.return_value = "2026-06-27T12:00:00+00:00"

        def api_side_effect(method, path, body=None):
            if method == "GET":
                if path.endswith("/user"):
                    return {"login": "test-judge-user"}
                elif path.endswith("/pulls/1"):
                    return {"merged": False, "state": "open"}
                elif "/pulls" in path and "/reviews" not in path:
                    return [{"number": 1}]
                elif path.endswith("/reviews"):
                    return [
                        {
                            "submitted_at": "2026-06-27T12:05:00Z",
                            "body": self._hidden_block_body(
                                {
                                    "syntax_lint": "PASS",
                                    "test_coverage": "PASS",
                                    "architecture": "PASS",
                                    "security": "FAIL",
                                }
                            ),
                            "user": {"login": "test-judge-user"},
                        }
                    ]
            elif method == "POST" and path.endswith("/comments"):
                # Posting the escalation comment fails — must not crash the node.
                raise RuntimeError("comment post unavailable")
            raise ValueError(f"Unexpected API call: {method} {path}")

        mock_api.side_effect = api_side_effect

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state["attempts"] = {"merge": 1, "verify_cmd": 0, "bineval": 0}
        state_module.save(state)

        os.environ["AGENT_PR_FIX_MAX"] = "1"
        try:
            from orchestrator.nodes import merge_node

            new_state = merge_node(state)
        finally:
            os.environ.pop("AGENT_PR_FIX_MAX", None)

        # Comment failure is tolerated; the node still transitions to recovery.
        self.assertEqual(new_state["status"], "failed")

    def test_merge_node_requires_issue_number(self):
        from orchestrator.nodes import merge_node

        state = DEFAULT_STATE.copy()
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        with self.assertRaises(ValueError):
            merge_node(state)

    @patch("orchestrator.nodes._github_api_request")
    def test_merge_node_exception_path_sets_failed_and_returns(self, mock_api):
        # ADR-0038: an unexpected API error inside the try block is recorded as
        # failed and returned (no re-raise) so recovery_node runs.
        mock_api.side_effect = RuntimeError("github API down")

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        result = merge_node(state)
        self.assertIs(result, state)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "merging")
        self.assertIn("merge: github API down", result["error"] or "")
        assert result["feedback"] is not None
        self.assertIn("github API down", result["feedback"])

        loaded = state_module.load()
        self.assertEqual(loaded["status"], "failed")
        self.assertEqual(loaded["phase"], "merging")


class TestRouteAfterMerge(unittest.TestCase):
    """route_after_merge closes the post-PR judge-feedback loop (ADR-0036)."""

    def test_routes_to_execute_on_merge_fix_retry(self):
        from orchestrator.graph import route_after_merge

        state = cast(AgentState, {"status": "executing", "phase": "merge_fix"})
        self.assertEqual(route_after_merge(state), "execute")

    def test_routes_to_recovery_on_failed(self):
        from orchestrator.graph import route_after_merge

        # Cap exhaustion / poll timeout / closed PR all set status="failed".
        state = cast(AgentState, {"status": "failed", "phase": "merging"})
        self.assertEqual(route_after_merge(state), "recovery")

    def test_routes_to_end_on_done(self):
        from orchestrator.graph import route_after_merge

        state = cast(AgentState, {"status": "done", "phase": "merging"})
        self.assertEqual(route_after_merge(state), "end")


class TestPrNodeCommitSelection(unittest.TestCase):
    """pr_node selects `fix:` vs `feat:` commit message per the Merge-Fix Loop."""

    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()

        self.original_env = {}
        vars_to_set = {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "AGENT_MODE": "local",
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v

        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(self.workspace_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(self.workspace_dir),
            check=True,
        )
        (self.workspace_dir / "README.md").write_text("# Test Repo", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"], cwd=str(self.workspace_dir), check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=str(self.workspace_dir),
            check=True,
        )

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()

    @patch("orchestrator.nodes.push")
    @patch("orchestrator.nodes.commit")
    @patch("orchestrator.nodes.add")
    @patch("orchestrator.nodes._github_api_request")
    def test_feat_commit_on_initial_pr(
        self, mock_api, mock_add, mock_commit, mock_push
    ):
        mock_api.side_effect = lambda method, path, body=None: (
            {"number": 1}
            if method == "GET" and "/pulls" in path and "/reviews" not in path
            else {"title": "x", "body": ""}
            if method == "GET" and path.endswith("/issues/10")
            else {}
        )
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state["attempts"] = {"merge": 0}
        state_module.save(state)

        from orchestrator.nodes import pr_node

        pr_node(state)

        commit_msg = mock_commit.call_args.args[1]
        self.assertEqual(commit_msg, "feat: resolve issue #10")

    @patch("orchestrator.nodes.push")
    @patch("orchestrator.nodes.commit")
    @patch("orchestrator.nodes.add")
    @patch("orchestrator.nodes._github_api_request")
    def test_fix_commit_on_merge_fix_reentry(
        self, mock_api, mock_add, mock_commit, mock_push
    ):
        mock_api.side_effect = lambda method, path, body=None: (
            [{"number": 1}]  # PR already exists -> creation skipped
            if method == "GET" and "/pulls" in path and "/reviews" not in path
            else {}
        )
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state["attempts"] = {"merge": 2, "verify_cmd": 0, "bineval": 0}
        state_module.save(state)

        from orchestrator.nodes import pr_node

        pr_node(state)

        commit_msg = mock_commit.call_args.args[1]
        self.assertEqual(commit_msg, "fix: address PR review feedback (attempt 2)")


class TestRecoveryNode(unittest.TestCase):
    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()

        self.original_env = {}
        vars_to_set = {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "AGENT_MODE": "local",
            "AGENT_LABEL_READY": "agent-ready",
            "AGENT_LABEL_IN_PROGRESS": "agent-in-progress",
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v

        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(self.workspace_dir),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(self.workspace_dir),
            check=True,
        )

        self.initial_file = self.workspace_dir / "README.md"
        self.initial_file.write_text("# Test Repo", encoding="utf-8")
        subprocess.run(
            ["git", "add", "README.md"], cwd=str(self.workspace_dir), check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=str(self.workspace_dir),
            check=True,
        )

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()

    @patch("orchestrator.nodes._github_api_request")
    def test_recovery_node(self, mock_api):
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state_module.save(state)

        from orchestrator.nodes import recovery_node

        new_state = recovery_node(state)

        self.assertEqual(new_state["status"], "failed")
        self.assertEqual(new_state["phase"], "recovery")

        # Verify labels reset was called via GitHub API
        add_label_call = [
            call
            for call in mock_api.mock_calls
            if "labels" in str(call)
            and "POST" in str(call)
            and "agent-ready" in str(call)
        ]
        remove_label_call = [
            call
            for call in mock_api.mock_calls
            if "labels" in str(call)
            and "DELETE" in str(call)
            and "agent-in-progress" in str(call)
        ]
        self.assertTrue(len(add_label_call) > 0)
        self.assertTrue(len(remove_label_call) > 0)

    @patch("orchestrator.nodes._github_api_request")
    def test_recovery_node_security_block_quarantine(self, mock_api):
        """ADR-0044: a security-block failure quarantines the issue with the
        agent-blocked label instead of agent-ready, so the claim scan skips it."""
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 42
        state["error"] = "security_block: .env"
        state_module.save(state)

        from orchestrator.nodes import recovery_node

        new_state = recovery_node(state)

        self.assertEqual(new_state["status"], "failed")
        self.assertEqual(new_state["phase"], "recovery")

        # Verify agent-blocked was added (quarantine), NOT agent-ready
        blocked_label_call = [
            call
            for call in mock_api.mock_calls
            if "labels" in str(call)
            and "POST" in str(call)
            and "agent-blocked" in str(call)
        ]
        ready_label_call = [
            call
            for call in mock_api.mock_calls
            if "labels" in str(call)
            and "POST" in str(call)
            and "agent-ready" in str(call)
        ]
        self.assertTrue(
            len(blocked_label_call) > 0,
            "Expected agent-blocked label to be added on security block.",
        )
        self.assertTrue(
            len(ready_label_call) == 0,
            "agent-ready must NOT be added on a security block (quarantine).",
        )

        # agent-in-progress should still be removed
        remove_label_call = [
            call
            for call in mock_api.mock_calls
            if "labels" in str(call)
            and "DELETE" in str(call)
            and "agent-in-progress" in str(call)
        ]
        self.assertTrue(len(remove_label_call) > 0)


class TestTestWriterNode(unittest.TestCase):
    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()

        self.original_env = {}
        vars_to_set = {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "test-owner/test-repo",
            "AGENT_MODE": "local",
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()

    @patch("orchestrator.nodes.subprocess.run")
    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_test_writer_node_success(
        self, mock_github_api, mock_execute_worker, mock_run
    ):
        # Setup mock GitHub API response
        mock_github_api.return_value = {
            "title": "Fix a bug",
            "body": "There is a bug in main.py.",
        }
        # Setup mock subprocess output (success, no bad errors)
        mock_res = unittest.mock.MagicMock()
        mock_res.stdout = "Ran 5 tests in 0.1s\nOK"
        mock_res.stderr = ""
        mock_run.return_value = mock_res

        # Setup initial state
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state_module.save(state)

        # Run test writer node
        from orchestrator.nodes import test_writer_node

        new_state = test_writer_node(state)

        # Verify status transitions, phase
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "test_writing")

        # Verify execute_worker was called
        mock_execute_worker.assert_called_once()
        mock_run.assert_called_once()

    @patch("orchestrator.nodes.subprocess.run")
    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_test_writer_node_includes_behavior_not_structure_constraint(
        self, mock_github_api, mock_execute_worker, mock_run
    ):
        """The Test-Writer instructions must carry the Behavior-Not-Structure
        constraint (issue #58) so generated tests specify behavior and do not
        leak a specific implementation that would steer the Worker.
        """
        mock_github_api.return_value = {
            "title": "Fix a bug",
            "body": "There is a bug in main.py.",
        }
        mock_res = unittest.mock.MagicMock()
        mock_res.stdout = "Ran 5 tests in 0.1s\nOK"
        mock_res.stderr = ""
        mock_run.return_value = mock_res

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state_module.save(state)

        from orchestrator.nodes import test_writer_node

        test_writer_node(state)

        # execute_worker(instructions, plan, node_name=..., ...) — instructions
        # is the first positional argument.
        instructions = mock_execute_worker.call_args.args[0]
        self.assertIn("BEHAVIOR, NOT STRUCTURE", instructions)
        self.assertIn("public", instructions.lower())
        self.assertIn("tautological", instructions.lower())

    @patch("orchestrator.nodes.subprocess.run")
    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_test_writer_node_retry_and_success(
        self, mock_github_api, mock_execute_worker, mock_run
    ):
        mock_github_api.return_value = {
            "title": "Fix a bug",
            "body": "There is a bug in main.py.",
        }
        # First check fails with SyntaxError, second succeeds
        res_fail = unittest.mock.MagicMock()
        res_fail.stdout = ""
        res_fail.stderr = "SyntaxError: invalid syntax"

        res_success = unittest.mock.MagicMock()
        res_success.stdout = "Ran 5 tests\nOK"
        res_success.stderr = ""

        mock_run.side_effect = [res_fail, res_success]

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state_module.save(state)

        from orchestrator.nodes import test_writer_node

        new_state = test_writer_node(state)

        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "test_writing")
        self.assertEqual(mock_execute_worker.call_count, 2)

    @patch("orchestrator.nodes.subprocess.run")
    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_test_writer_node_all_attempts_fail(
        self, mock_github_api, mock_execute_worker, mock_run
    ):
        mock_github_api.return_value = {"title": "Fix a bug", "body": "There is a bug."}
        res_fail = unittest.mock.MagicMock()
        res_fail.stdout = ""
        res_fail.stderr = "ModuleNotFoundError: No module named foo"
        mock_run.return_value = res_fail

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state_module.save(state)

        from orchestrator.nodes import test_writer_node

        # ADR-0038: the pre-verification RuntimeError is caught by the node's
        # exception handler, recorded as failed, and returned (no raise) so
        # recovery_node runs.
        result = test_writer_node(state)
        self.assertIs(result, state)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "test_writing")
        self.assertIn("Test-Writer failed pre-verification", result["error"] or "")

        self.assertEqual(mock_execute_worker.call_count, 3)


class TestGraphCompilation(unittest.TestCase):
    def test_graph_compiles_successfully(self):
        from orchestrator.graph import graph

        self.assertIsNotNone(graph)


class TestGithubApiRetry(unittest.TestCase):
    """Bounded timeout + retry with backoff on transient failures."""

    def _ok_response(self, payload):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _http_error(self, code, retry_after=None):
        import email.message

        headers = email.message.Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return urllib.error.HTTPError(
            "https://api.github.com/x", code, "err", headers, None
        )

    @patch("orchestrator.nodes.time.sleep")
    @patch("orchestrator.nodes.urllib.request.urlopen")
    def test_retries_on_5xx_then_succeeds(self, mock_urlopen, mock_sleep):
        from orchestrator.nodes import _github_api_request

        mock_urlopen.side_effect = [
            self._http_error(503),
            self._ok_response({"number": 1}),
        ]
        result = _github_api_request("GET", "/repos/o/r/issues/1")
        self.assertEqual(result, {"number": 1})
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("orchestrator.nodes.time.sleep")
    @patch("orchestrator.nodes.urllib.request.urlopen")
    def test_retries_on_403_with_retry_after(self, mock_urlopen, mock_sleep):
        from orchestrator.nodes import _github_api_request, _gh_backoff_seconds

        mock_urlopen.side_effect = [
            self._http_error(403, retry_after="2"),
            self._ok_response({"ok": True}),
        ]
        self.assertEqual(_github_api_request("GET", "/x"), {"ok": True})
        # Retry-After is honored (capped at 30s).
        self.assertAlmostEqual(_gh_backoff_seconds(1, "2"), 2.0)

    def test_backoff_falls_back_on_malformed_retry_after(self):
        """A malformed Retry-After header falls back to exponential backoff."""
        from orchestrator.nodes import _gh_backoff_seconds

        # Non-numeric Retry-After → ValueError → fallback to 2^(attempt-1).
        self.assertEqual(_gh_backoff_seconds(1, "not-a-number"), 1.0)
        self.assertEqual(_gh_backoff_seconds(3, "soon"), 4.0)

    @patch("orchestrator.nodes.time.sleep")
    @patch("orchestrator.nodes.urllib.request.urlopen")
    def test_does_not_retry_on_404(self, mock_urlopen, mock_sleep):
        from orchestrator.nodes import _github_api_request

        mock_urlopen.side_effect = [self._http_error(404)]
        with self.assertRaises(RuntimeError):
            _github_api_request("GET", "/missing")
        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("orchestrator.nodes.time.sleep")
    @patch("orchestrator.nodes.urllib.request.urlopen")
    def test_passes_timeout(self, mock_urlopen, mock_sleep):
        from orchestrator.nodes import _GH_API_TIMEOUT, _github_api_request

        mock_urlopen.return_value = self._ok_response({"ok": True})
        _github_api_request("GET", "/x")
        _, kwargs = mock_urlopen.call_args
        self.assertEqual(kwargs.get("timeout"), _GH_API_TIMEOUT)

    @patch("orchestrator.nodes.time.sleep")
    @patch("orchestrator.nodes.urllib.request.urlopen")
    def test_retries_exhausted_on_persistent_5xx(self, mock_urlopen, mock_sleep):
        """After the retry budget, a persistent 5xx raises."""
        from orchestrator.nodes import _GH_API_MAX_ATTEMPTS, _github_api_request

        mock_urlopen.side_effect = [self._http_error(503)] * _GH_API_MAX_ATTEMPTS
        with self.assertRaises(RuntimeError) as ctx:
            _github_api_request("GET", "/x")
        self.assertIn("exhausted", str(ctx.exception).lower())
        self.assertEqual(mock_urlopen.call_count, _GH_API_MAX_ATTEMPTS)

    @patch("orchestrator.nodes.time.sleep")
    @patch("orchestrator.nodes.urllib.request.urlopen")
    def test_retries_exhausted_on_persistent_urlerror(self, mock_urlopen, mock_sleep):
        """Persistent connection errors exhaust the budget and raise."""
        import urllib.error as urlerr

        from orchestrator.nodes import _GH_API_MAX_ATTEMPTS, _github_api_request

        mock_urlopen.side_effect = urlerr.URLError("conn refused")
        with self.assertRaises(RuntimeError) as ctx:
            _github_api_request("GET", "/x")
        self.assertIn("exhausted", str(ctx.exception).lower())
        self.assertEqual(mock_urlopen.call_count, _GH_API_MAX_ATTEMPTS)

    @patch("orchestrator.nodes.time.sleep")
    @patch("orchestrator.nodes.urllib.request.urlopen")
    def test_generic_exception_is_not_retried(self, mock_urlopen, mock_sleep):
        """A non-HTTP/non-URLError exception is raised immediately (no retry)."""
        from orchestrator.nodes import _github_api_request

        mock_urlopen.side_effect = ValueError("unexpected")
        with self.assertRaises(RuntimeError) as ctx:
            _github_api_request("GET", "/x")
        self.assertIn("Failed to connect to GitHub API", str(ctx.exception))
        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()


class TestRecoveryOnException(unittest.TestCase):
    """ADR-0038: a node exception is recorded (status=failed, return state) so
    the status-based conditional edges route to recovery_node, which cleans the
    workspace and restores the issue's GitHub label to agent-ready.

    The graph always starts at claim_node (which on a mid-flow resume state
    collapses to idle), so this test exercises the contract directly: it injects
    an exception into verify_node, asserts the record-and-return, then runs
    recovery_node on the resulting failed state and asserts cleanup + label
    restoration. The failed→recovery edge itself is covered by the route tests.
    """

    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()
        self.original_env = {}
        for k, v in {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "owner/repo",
            "AGENT_VERIFY_COMMAND": "definitely-not-a-real-binary-xyz",
        }.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v
        # Minimal git repo so checkout/reset/clean operate; commit on main.
        subprocess.run(["git", "init", "-q"], cwd=self.workspace_dir, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t"], cwd=self.workspace_dir, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "t"], cwd=self.workspace_dir, check=True
        )
        subprocess.run(
            ["git", "checkout", "-b", "main", "-q"], cwd=self.workspace_dir, check=True
        )
        (self.workspace_dir / "committed.txt").write_text("kept")
        subprocess.run(["git", "add", "."], cwd=self.workspace_dir, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "init"], cwd=self.workspace_dir, check=True
        )
        # Create the in-flight branch and leave an untracked dirty file.
        subprocess.run(
            ["git", "checkout", "-b", "feat/issue-42", "-q"],
            cwd=self.workspace_dir,
            check=True,
        )
        (self.workspace_dir / "dirty.txt").write_text("junk")

        self.labels_added = []
        self.labels_removed = []

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()

    @patch("orchestrator.nodes._remove_label")
    @patch("orchestrator.nodes._add_label")
    def test_verify_exception_routes_to_recovery(
        self, mock_add_label, mock_remove_label
    ):
        from orchestrator.nodes import recovery_node, verify_node

        mock_add_label.side_effect = lambda repo, num, label: self.labels_added.append(
            label
        )
        mock_remove_label.side_effect = lambda repo, num, label: (
            self.labels_removed.append(label)
        )
        # Inject an exception inside verify_node's try block: a nonexistent
        # verify command makes subprocess.run raise FileNotFoundError (a genuine
        # exception, not a non-zero exit), taking the ADR-0038 recovery path.
        # (AGENT_VERIFY_COMMAND is set in setUp and restored in tearDown.)
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 42
        state["branch"] = "feat/issue-42"
        state["status"] = "verifying"
        state["phase"] = "verifying"
        state_module.save(state)

        # 1. verify_node records the failure and returns (no raise).
        failed = verify_node(state)
        self.assertIs(failed, state)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["phase"], "verifying")
        self.assertIn("verify: ", failed["error"] or "")

        # 2. Hand the failed state to recovery_node (the failed->recovery edge
        #    is wired in graph.py); assert cleanup + label restoration.
        result = recovery_node(failed)
        self.assertEqual(result["status"], "failed")
        # Workspace cleaned: the untracked dirty file is gone, the committed
        # file (on main) survives.
        self.assertFalse((self.workspace_dir / "dirty.txt").exists())
        self.assertTrue((self.workspace_dir / "committed.txt").exists())
        # The issue label was restored to agent-ready; agent-in-progress removed.
        self.assertIn("agent-ready", self.labels_added)
        self.assertIn("agent-in-progress", self.labels_removed)
        # The original root-cause error survives recovery.
        self.assertIn("verify: ", result.get("error") or "")


class TestClaimNodeExceptionPaths(unittest.TestCase):
    """ADR-0038: claim_node exception handlers record failed + return state."""

    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()
        self.original_env = {}
        for k, v in {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "owner/repo",
            "AGENT_LABEL_READY": "agent-ready",
            "AGENT_LABEL_IN_PROGRESS": "agent-in-progress",
            "AGENT_LABEL_BLOCKED": "agent-blocked",
        }.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 7
        state["status"] = "claimed"
        state["branch"] = "feat/issue-7"
        state_module.save(state)

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()

    @patch("orchestrator.nodes._get_github_repository")
    def test_claim_resolve_repo_failure_records_and_returns(self, mock_repo):
        """When repo resolution raises, claim_node records failed and returns."""
        mock_repo.side_effect = ValueError("no remote")
        result = claim_node(state_module.load())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "claim")
        self.assertIn("claim: no remote", result["error"] or "")

    @patch("orchestrator.nodes.is_git_repository", return_value=False)
    @patch("orchestrator.nodes.clone")
    @patch("orchestrator.nodes._get_github_repository", return_value="owner/repo")
    def test_claim_workspace_hygiene_failure_records_and_returns(
        self, mock_repo, mock_clone, mock_is_repo
    ):
        """When workspace cloning fails, claim_node records failed and returns."""
        mock_clone.side_effect = RuntimeError("clone failed")
        # Fresh (non-resume) state so the clone path runs.
        state = DEFAULT_STATE.copy()
        state["issue_number"] = None
        state_module.save(state)
        result = claim_node(state)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "claim")
        self.assertIn("claim: clone failed", result["error"] or "")


class TestPrNodeAndRecoveryExceptionPaths(unittest.TestCase):
    """ADR-0038: pr_node and recovery_node exception handlers record + return."""

    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()
        self.original_env = {}
        for k, v in {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "owner/repo",
        }.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v
        # Minimal git repo on main with a committed file.
        subprocess.run(["git", "init", "-q"], cwd=self.workspace_dir, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t"], cwd=self.workspace_dir, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "t"], cwd=self.workspace_dir, check=True
        )
        subprocess.run(
            ["git", "checkout", "-b", "main", "-q"], cwd=self.workspace_dir, check=True
        )
        (self.workspace_dir / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=self.workspace_dir, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "init"], cwd=self.workspace_dir, check=True
        )

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()

    @patch("orchestrator.nodes.add")
    @patch("orchestrator.nodes._get_github_repository")
    def test_pr_node_exception_records_and_returns(self, mock_repo, mock_add):
        """ADR-0038: a pr_node step failure records failed and returns state."""
        from orchestrator.nodes import pr_node

        mock_repo.return_value = "owner/repo"
        mock_add.side_effect = RuntimeError("git add failed")
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 9
        state["branch"] = "feat/issue-9"
        state_module.save(state)
        result = pr_node(state)
        self.assertIs(result, state)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["phase"], "pr_open")
        self.assertIn("pr: git add failed", result["error"] or "")

    @patch("orchestrator.nodes._remove_label")
    @patch("orchestrator.nodes._add_label")
    @patch("orchestrator.nodes._checkout_default_branch")
    def test_recovery_self_rescue_records_and_returns(
        self, mock_checkout, mock_add_label, mock_remove_label
    ):
        """ADR-0038: a failure inside recovery_node is caught; the original
        root-cause error survives and status stays failed."""
        from orchestrator.nodes import recovery_node

        # recovery_node's own cleanup blows up, but it must still persist failed.
        mock_checkout.side_effect = RuntimeError("checkout blew up")
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 9
        state["status"] = "failed"
        state["phase"] = "verifying"
        state["error"] = "verify: original boom"
        state_module.save(state)
        result = recovery_node(state)
        self.assertEqual(result["status"], "failed")
        # The original root-cause error is preserved (recovery does not mask it).
        self.assertEqual(result["error"], "verify: original boom")


class TestIsinstanceGuardCoverage(unittest.TestCase):
    """Covers the ``if not isinstance(data, dict): raise RuntimeError(...)``
    guards scattered across the nodes, plus the BinEval degradation flag.

    Each guard validates that the GitHub REST API returned a JSON object (dict)
    rather than a list/scalar. A non-dict response triggers a RuntimeError that
    is caught by the node's exception handler (ADR-0038 record-and-return for
    plan/execute/pr/test_writer, inner try/except warning-and-continue for the
    claim dependency checks and merge user check, and the BinEval soft-gate
    degradation handler).
    """

    def setUp(self):
        self.workspace_temp = tempfile.TemporaryDirectory()
        self.workspace_dir = Path(self.workspace_temp.name).resolve()
        self.logs_temp = tempfile.TemporaryDirectory()
        self.logs_dir = Path(self.logs_temp.name).resolve()
        self.original_env = {}
        for k, v in {
            "GITHUB_WORKSPACE": str(self.workspace_dir),
            "AGENT_LOG_PATH": str(self.logs_dir),
            "GITHUB_REPOSITORY": "owner/repo",
            "AGENT_LABEL_READY": "agent-ready",
            "AGENT_LABEL_IN_PROGRESS": "agent-in-progress",
            "AGENT_LABEL_BLOCKED": "agent-blocked",
            # Merge-node: an empty trusted user forces the /user API fallback
            # whose isinstance guard is under test; a zero poll timeout skips
            # the blocking polling loop so the merge test stays fast.
            "AGENT_TRUSTED_JUDGE_USER": "",
            "AGENT_MERGE_POLL_TIMEOUT": "0",
        }.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v
        # Minimal git repo on main with a committed file so workspace hygiene
        # (checkout/reset/clean) and commit-time lookups operate cleanly.
        subprocess.run(["git", "init", "-q"], cwd=self.workspace_dir, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t"], cwd=self.workspace_dir, check=True
        )
        subprocess.run(
            ["git", "config", "user.name", "t"], cwd=self.workspace_dir, check=True
        )
        subprocess.run(
            ["git", "checkout", "-b", "main", "-q"], cwd=self.workspace_dir, check=True
        )
        (self.workspace_dir / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=self.workspace_dir, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "init"], cwd=self.workspace_dir, check=True
        )

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()

    # --- plan_node (line 651) ---

    @patch("orchestrator.nodes._get_github_repository", return_value="owner/repo")
    @patch("orchestrator.nodes._github_api_request", return_value=[])
    def test_plan_node_non_dict_issue_data_records_failure(self, mock_api, mock_repo):
        from orchestrator.nodes import plan_node

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 42
        state["status"] = "claimed"
        state["branch"] = "feat/issue-42"
        state_module.save(state)

        result = plan_node(state)

        self.assertEqual(result["status"], "failed")
        self.assertIn("expected a dict", result["error"] or "")

    # --- execute_node (line 837) ---

    @patch("orchestrator.nodes._get_github_repository", return_value="owner/repo")
    @patch("orchestrator.nodes._github_api_request", return_value=[])
    def test_execute_node_non_dict_issue_data_records_failure(
        self, mock_api, mock_repo
    ):
        from orchestrator.nodes import execute_node

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 42
        state["status"] = "planning"
        state["plan"] = "do stuff"
        state["model"] = "test-model"
        state_module.save(state)

        result = execute_node(state)

        self.assertEqual(result["status"], "failed")
        self.assertIn("expected a dict", result["error"] or "")

    # --- test_writer_node (line 2139) ---

    @patch("orchestrator.nodes._get_github_repository", return_value="owner/repo")
    @patch("orchestrator.nodes._github_api_request", return_value=[])
    def test_test_writer_node_non_dict_issue_data_records_failure(
        self, mock_api, mock_repo
    ):
        from orchestrator.nodes import test_writer_node

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 42
        state["plan"] = "do stuff"
        state_module.save(state)

        result = test_writer_node(state)

        self.assertEqual(result["status"], "failed")
        self.assertIn("expected a dict", result["error"] or "")

    # --- _run_bineval_phase (line 1280 + bineval_degraded at 1288-1292) ---

    @patch("orchestrator.nodes._get_github_repository", return_value="owner/repo")
    @patch("orchestrator.nodes._github_api_request", return_value=[])
    @patch("orchestrator.nodes._get_workspace_diff", return_value="diff content")
    def test_bineval_phase_non_dict_issue_sets_degraded_flag(
        self, _diff, mock_api, mock_repo
    ):
        from orchestrator.nodes import _run_bineval_phase

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 42
        state_module.save(state)

        exit_code = _run_bineval_phase(state, 42, self.workspace_dir)

        self.assertEqual(exit_code, 0)
        self.assertTrue(state["bineval_degraded"])

    # --- pr_node (line 1597) ---

    @patch("orchestrator.nodes.push")
    @patch("orchestrator.nodes.commit")
    @patch("orchestrator.nodes._get_github_repository", return_value="owner/repo")
    @patch("orchestrator.nodes._github_api_request", side_effect=[[], []])
    def test_pr_node_non_dict_issue_data_records_failure(
        self, mock_api, mock_repo, mock_commit, mock_push
    ):
        from orchestrator.nodes import pr_node

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 42
        state["status"] = "verifying"
        state["branch"] = "feat/issue-42"
        state_module.save(state)

        result = pr_node(state)

        self.assertEqual(result["status"], "failed")
        self.assertIn("expected a dict", result["error"] or "")

    # --- claim_node: Unblocking Scan dep check (line 380) ---

    @patch("orchestrator.nodes._remove_label")
    @patch("orchestrator.nodes._add_label")
    @patch("orchestrator.nodes._get_github_repository", return_value="owner/repo")
    @patch("orchestrator.nodes._github_api_request")
    def test_claim_node_unblocking_dep_non_dict_continues(
        self, mock_api, mock_repo, mock_add_label, mock_remove_label
    ):
        from orchestrator.nodes import claim_node

        mock_api.side_effect = [
            # Unblocking scan: one blocked issue depending on #99.
            [{"number": 5, "title": "Blocked", "body": "## Blocked by - #99"}],
            # Dependency issue #99 returns a non-dict list -> line 380 guard.
            [],
            # Claim scan: no ready issues.
            [],
        ]

        # Fresh (non-resume) state so both scans run.
        state = DEFAULT_STATE.copy()
        state_module.save(state)

        result = claim_node(state)

        # No issue claimed -> idle; the non-dict RuntimeError was swallowed by
        # the inner try/except (warning + all_closed=False).
        self.assertEqual(result["status"], "idle")

    # --- claim_node: Claim Scan dep check (line 438) ---

    @patch("orchestrator.nodes._remove_label")
    @patch("orchestrator.nodes._add_label")
    @patch("orchestrator.nodes._get_github_repository", return_value="owner/repo")
    @patch("orchestrator.nodes._github_api_request")
    def test_claim_node_claim_scan_dep_non_dict_transitions_blocked(
        self, mock_api, mock_repo, mock_add_label, mock_remove_label
    ):
        from orchestrator.nodes import claim_node

        mock_api.side_effect = [
            # Unblocking scan: no blocked issues.
            [],
            # Claim scan: one ready issue depending on #88.
            [{"number": 7, "title": "Ready", "body": "## Blocked by - #88"}],
            # Dependency issue #88 returns a non-dict list -> line 438 guard.
            [],
        ]

        state = DEFAULT_STATE.copy()
        state_module.save(state)

        result = claim_node(state)

        # The ready issue had an open dependency (guard swallowed the error),
        # so it was transitioned to blocked and no issue was claimed -> idle.
        self.assertEqual(result["status"], "idle")
        mock_add_label.assert_called_with("owner/repo", 7, "agent-blocked")

    # --- claim_node: Claim Scan view_data check (line 476) ---

    @patch("orchestrator.nodes._remove_label")
    @patch("orchestrator.nodes._add_label")
    @patch("orchestrator.nodes._get_github_repository", return_value="owner/repo")
    @patch("orchestrator.nodes._github_api_request")
    def test_claim_node_claim_scan_view_data_non_dict_skips(
        self, mock_api, mock_repo, mock_add_label, mock_remove_label
    ):
        from orchestrator.nodes import claim_node

        mock_api.side_effect = [
            # Unblocking scan: no blocked issues.
            [],
            # Claim scan: one ready issue with no dependencies.
            [{"number": 7, "title": "Ready", "body": "no deps"}],
            # Double-check issue #7 returns a non-dict list -> line 476 guard.
            [],
        ]

        state = DEFAULT_STATE.copy()
        state_module.save(state)

        result = claim_node(state)

        # The view_data guard swallowed the error (skip), no issue claimed.
        self.assertEqual(result["status"], "idle")

    # --- merge_node: curr_user_data check (line 1826) ---

    @patch("orchestrator.nodes._get_github_repository", return_value="owner/repo")
    @patch("orchestrator.nodes._github_api_request", side_effect=[[{"number": 1}], []])
    def test_merge_node_non_dict_user_data_falls_back_gracefully(
        self, mock_api, mock_repo
    ):
        from orchestrator.nodes import merge_node

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 42
        state["status"] = "pr_open"
        state["branch"] = "feat/issue-42"
        state["pushed_at"] = "2024-01-01T00:00:00Z"
        state_module.save(state)

        result = merge_node(state)

        # The /user isinstance guard was swallowed by the inner except
        # (fallback to GITHUB_ACTOR). The poll loop is skipped (timeout=0),
        # so the PR times out -> failed. The key assertion is that the node
        # did not crash on the non-dict response.
        self.assertEqual(result["status"], "failed")


class TestGetGithubRepositorySelfTargetWarning(unittest.TestCase):
    """Issue #107: when GITHUB_REPOSITORY is unset and the fallback resolves to
    the orchestrator's own repository, a warning naming both repos must be logged
    exactly once (deduped) so the silent self-polling footgun surfaces."""

    def setUp(self):
        self.original_repo = os.environ.get("GITHUB_REPOSITORY")
        os.environ.pop("GITHUB_REPOSITORY", None)
        # Reset the module-level dedup flag before each test.
        import orchestrator.nodes as nodes_mod

        nodes_mod._SELF_TARGET_WARNED = False

    def tearDown(self):
        if self.original_repo is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = self.original_repo

    @patch("orchestrator.nodes._resolve_own_repo_remote", return_value="me/myself")
    @patch("orchestrator.nodes.get_remote_url")
    def test_warns_when_env_unset_and_resolved_equals_own_repo(
        self, mock_remote, mock_own
    ):
        """A warning naming both repos is logged on the self-target fallback."""
        from orchestrator.nodes import _get_github_repository

        mock_remote.return_value = "git@github.com:me/myself.git"

        with self.assertLogs("orchestrator.nodes", level="WARNING") as cm:
            repo = _get_github_repository(Path("/some/workspace"))

        self.assertEqual(repo, "me/myself")
        self.assertTrue(any("me/myself" in msg for msg in cm.output))
        self.assertTrue(any("GITHUB_REPOSITORY is not set" in msg for msg in cm.output))

    @patch("orchestrator.nodes._resolve_own_repo_remote", return_value="me/myself")
    @patch("orchestrator.nodes.get_remote_url")
    def test_no_warn_when_github_repository_env_set(self, mock_remote, mock_own):
        """When GITHUB_REPOSITORY is set explicitly, no fallback (and no warning) occurs."""
        from orchestrator.nodes import _get_github_repository

        os.environ["GITHUB_REPOSITORY"] = "explicit/target"

        with self.assertNoLogs("orchestrator.nodes", level="WARNING"):
            repo = _get_github_repository(Path("/some/workspace"))

        self.assertEqual(repo, "explicit/target")
        mock_remote.assert_not_called()

    @patch("orchestrator.nodes._resolve_own_repo_remote", return_value="me/myself")
    @patch("orchestrator.nodes.get_remote_url")
    def test_warning_deduped_across_calls(self, mock_remote, mock_own):
        """The self-target warning fires at most once per process."""
        from orchestrator.nodes import _get_github_repository

        mock_remote.return_value = "git@github.com:me/myself.git"

        with self.assertLogs("orchestrator.nodes", level="WARNING") as cm:
            _get_github_repository(Path("/some/workspace"))
            _get_github_repository(Path("/some/workspace"))
            _get_github_repository(Path("/some/workspace"))

        warning_msgs = [m for m in cm.output if "GITHUB_REPOSITORY is not set" in m]
        self.assertEqual(
            len(warning_msgs),
            1,
            "self-target warning must be deduped to a single emission",
        )

    @patch("orchestrator.nodes._resolve_own_repo_remote", return_value="me/myself")
    @patch("orchestrator.nodes.get_remote_url")
    def test_no_warn_when_resolved_repo_differs_from_own(self, mock_remote, mock_own):
        """No warning when the fallback resolves to a repo other than the orchestrator's own."""
        from orchestrator.nodes import _get_github_repository

        mock_remote.return_value = "git@github.com:other/project.git"

        with self.assertNoLogs("orchestrator.nodes", level="WARNING"):
            repo = _get_github_repository(Path("/some/workspace"))

        self.assertEqual(repo, "other/project")


class TestParseOwnerRepoFromUrl(unittest.TestCase):
    """Direct unit tests for _parse_owner_repo_from_url (issue #107).

    Exercises the URL-parsing helper that the SelfTargetWarning tests mock away,
    covering SSH, HTTPS, and the generic fallback branch.
    """

    def test_https_url(self):
        from orchestrator.nodes import _parse_owner_repo_from_url

        self.assertEqual(
            _parse_owner_repo_from_url("https://github.com/owner/repo.git"),
            "owner/repo",
        )

    def test_ssh_url(self):
        from orchestrator.nodes import _parse_owner_repo_from_url

        self.assertEqual(
            _parse_owner_repo_from_url("git@github.com:owner/repo.git"),
            "owner/repo",
        )

    def test_strips_no_git_suffix(self):
        from orchestrator.nodes import _parse_owner_repo_from_url

        self.assertEqual(
            _parse_owner_repo_from_url("https://github.com/owner/repo"),
            "owner/repo",
        )

    def test_generic_fallback(self):
        from orchestrator.nodes import _parse_owner_repo_from_url

        self.assertEqual(
            _parse_owner_repo_from_url("somehost:owner/repo.git"),
            "owner/repo",
        )


class TestResolveOwnRepoRemote(unittest.TestCase):
    """Direct unit tests for _resolve_own_repo_remote (issue #107, [Q4]).

    Exercises the helper that resolves the orchestrator's own owner/repo from
    its git remote origin. The SelfTargetWarning tests mock this away; these
    tests cover it directly so the production code path is exercised.
    """

    @patch("orchestrator.nodes.get_remote_url")
    def test_resolves_own_repo_from_remote(self, mock_remote):
        from orchestrator.nodes import _resolve_own_repo_remote

        mock_remote.return_value = "git@github.com:me/myself.git"
        result = _resolve_own_repo_remote()
        self.assertEqual(result, "me/myself")

    @patch("orchestrator.nodes.get_remote_url")
    def test_returns_none_on_failure(self, mock_remote):
        from orchestrator.nodes import _resolve_own_repo_remote

        mock_remote.side_effect = RuntimeError("no remote")
        result = _resolve_own_repo_remote()
        self.assertIsNone(result)


class TestGetGithubRepositoryErrorBranch(unittest.TestCase):
    """Direct unit tests for _get_github_repository's fallback error branch
    (issue #107, [Q4]). Exercises the `except` path that raises ValueError when
    get_remote_url fails — the SelfTargetWarning tests mock get_remote_url to
    succeed, leaving this branch uncovered."""

    def setUp(self):
        self.original_repo = os.environ.get("GITHUB_REPOSITORY")
        os.environ.pop("GITHUB_REPOSITORY", None)

    def tearDown(self):
        if self.original_repo is None:
            os.environ.pop("GITHUB_REPOSITORY", None)
        else:
            os.environ["GITHUB_REPOSITORY"] = self.original_repo

    @patch("orchestrator.nodes.get_remote_url")
    def test_raises_value_error_when_remote_resolution_fails(self, mock_remote):
        """When get_remote_url raises and GITHUB_REPOSITORY is unset, _get_github_repository raises ValueError."""
        from orchestrator.nodes import _get_github_repository

        mock_remote.side_effect = RuntimeError("no remote configured")

        with self.assertRaises(ValueError) as ctx:
            _get_github_repository(Path("/some/workspace"))

        self.assertIn("GITHUB_REPOSITORY", str(ctx.exception))
        self.assertIn("no remote configured", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
