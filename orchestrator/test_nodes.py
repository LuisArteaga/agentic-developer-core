import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from orchestrator import state as state_module
from orchestrator.nodes import claim_node, execute_node, plan_node, verify_node
from orchestrator.state import DEFAULT_STATE, AgentState


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
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
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
        mock_structured_llm.invoke.return_value = mock_plan

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
        """Test that Plan-Node handles LLM failures by updating status to 'failed' and propagating exception."""
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

        # Verify exception is propagated
        with self.assertRaises(RuntimeError) as ctx:
            plan_node(state)
        self.assertEqual(str(ctx.exception), "OpenRouter API error")

        # Verify state is updated and saved as 'failed' at 'planning' phase
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
        self.assertFalse(_is_safe_path("ssh_keys/id_rsa"))
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
        mock_structured_llm.invoke.return_value = mock_malicious_plan

        # Setup initial state
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state_module.save(state)

        # Verify ValueError is raised with a security block message
        with self.assertRaises(ValueError) as ctx:
            plan_node(state)
        self.assertIn("Security Block", str(ctx.exception))
        self.assertIn("'.env'", str(ctx.exception))

        # Verify state is updated and saved as 'failed' at 'planning' phase
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
        mock_structured_llm.invoke.side_effect = [first_plan, second_plan]

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
        mock_structured_llm.invoke.return_value = mock_plan

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
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
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
        """Test that Execute-Node transitions status to failed and propagates exception on worker error."""
        # Setup GitHub API to raise an exception
        mock_github_api.side_effect = RuntimeError("API rate limit")

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state_module.save(state)

        # Verify exception is propagated
        with self.assertRaises(RuntimeError) as ctx:
            execute_node(state)
        self.assertEqual(str(ctx.exception), "API rate limit")

        # Verify state is updated to failed at executing phase
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
        # attempts["verify"]=0 -> first execute attempt (1) -> below threshold (3)
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
        # attempts["verify"]=2 -> execute attempt 3 -> meets default threshold
        state["attempts"] = {"verify": 2}
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
        # attempts["verify"]=1 -> execute attempt 2 -> meets configured threshold
        state["attempts"] = {"verify": 1}
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
        state["attempts"] = {"verify": 2}  # attempt 3, but threshold 99 -> no reset
        state_module.save(state)

        execute_node(state)

        mock_reset_hard.assert_not_called()
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
        }
        for k, v in vars_to_set.items():
            self.original_env[k] = os.environ.get(k)
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
        state["attempts"] = {"verify": 2}
        state["feedback"] = "some previous error"
        state_module.save(state)

        # Run verify node
        new_state = verify_node(state)

        # Verify state changes: status 'verifying', attempts reset to 0, feedback cleared
        self.assertEqual(new_state["status"], "verifying")
        self.assertEqual(new_state["phase"], "verifying")
        self.assertEqual(new_state["attempts"]["verify"], 0)
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
        state["attempts"] = {"verify": 1}
        state_module.save(state)

        # Run verify node
        new_state = verify_node(state)

        # Verify state: status becomes 'executing' for retry, attempts incremented to 2, feedback saved
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "verifying")
        self.assertEqual(new_state["attempts"]["verify"], 2)
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
        state["attempts"] = {"verify": 2}
        state_module.save(state)

        # Run verify node (this is attempt 3)
        new_state = verify_node(state)

        # Verify state: status becomes 'failed', attempts incremented to 3
        self.assertEqual(new_state["status"], "failed")
        self.assertEqual(new_state["phase"], "verifying")
        self.assertEqual(new_state["attempts"]["verify"], 3)
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
        self.assertEqual(new_state["attempts"]["verify"], 1)
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
        }.items():
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

    def _state(self, attempts_verify=0):
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["attempts"] = {"verify": attempts_verify}
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
            state = self._state(attempts_verify=2)
            new_state = verify_node(state)
        self.assertEqual(new_state["status"], "verifying")
        self.assertEqual(new_state["attempts"]["verify"], 0)
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
            state = self._state(attempts_verify=0)
            new_state = verify_node(state)
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["attempts"]["verify"], 1)
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
            state = self._state(attempts_verify=2)
            new_state = verify_node(state)
        self.assertEqual(new_state["status"], "failed")
        self.assertEqual(new_state["attempts"]["verify"], 3)
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
            state = self._state(attempts_verify=1)
            new_state = verify_node(state)
        self.assertEqual(new_state["status"], "verifying")
        self.assertEqual(new_state["attempts"]["verify"], 0)
        self.assertIsNone(new_state["feedback"])

    @patch("orchestrator.nodes._github_api_request")
    @patch("orchestrator.nodes._get_workspace_diff", return_value="")
    @patch("orchestrator.nodes.subprocess.run")
    def test_bineval_skipped_on_empty_diff(self, mock_run, _diff, mock_gh):
        from orchestrator.nodes import verify_node

        mock_run.return_value = self._mock_make_verify_pass()
        state = self._state(attempts_verify=2)
        new_state = verify_node(state)
        # Empty diff -> skip BinEval -> PASS path -> reset + PR transition.
        self.assertEqual(new_state["status"], "verifying")
        self.assertEqual(new_state["attempts"]["verify"], 0)
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
                state = self._state(attempts_verify=0)
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
        configured value, mimicking with_structured_output(...).invoke(prompt).
        """
        captured = {}

        def invoke(prompt):
            captured["prompt"] = prompt
            return invoke_return

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
        self.assertEqual(new_state["attempts"].get("verify"), 0)
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
        self.assertEqual(new_state["attempts"].get("verify"), 0)
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
        self.assertEqual(new_state["attempts"].get("verify"), 0)
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
        self.assertEqual(new_state["attempts"].get("verify"), 0)
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
        self.assertEqual(new_state["attempts"].get("verify"), 0)
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
        self.assertEqual(new_state["attempts"].get("verify"), 0)
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
        state["attempts"] = {"merge": 1, "verify": 0}
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
        state["attempts"] = {"merge": 2, "verify": 0}
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
        state["attempts"] = {"merge": 1, "verify": 0}
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
        state["attempts"] = {"merge": 1, "verify": 0}
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
    def test_merge_node_exception_path_sets_failed_and_reraises(self, mock_api):
        # An unexpected API error inside the try block takes the exception path.
        mock_api.side_effect = RuntimeError("github API down")

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["branch"] = "feat/issue-10"
        state_module.save(state)

        from orchestrator.nodes import merge_node

        with self.assertRaises(RuntimeError):
            merge_node(state)

        loaded = state_module.load()
        self.assertEqual(loaded["status"], "failed")
        self.assertEqual(loaded["phase"], "merging")
        assert loaded["feedback"] is not None
        self.assertIn("github API down", loaded["feedback"])


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
        state["attempts"] = {"merge": 2, "verify": 0}
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

        with self.assertRaises(RuntimeError):
            test_writer_node(state)

        self.assertEqual(mock_execute_worker.call_count, 3)


class TestGraphCompilation(unittest.TestCase):
    def test_graph_compiles_successfully(self):
        from orchestrator.graph import graph

        self.assertIsNotNone(graph)


if __name__ == "__main__":
    unittest.main()
