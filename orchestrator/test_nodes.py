import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import state as state_module
from orchestrator.state import AgentState, DEFAULT_STATE
from orchestrator.nodes import claim_node, plan_node, execute_node, verify_node

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
                            "body": "We need to add a migration script."
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
        branch_res = subprocess.run(["git", "branch", "--show-current"], cwd=str(self.workspace_dir), capture_output=True, text=True, check=True)
        self.assertEqual(branch_res.stdout.strip(), "feat/issue-10")

    @patch("orchestrator.nodes._github_api_request")
    def test_resume_flow(self, mock_api):
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
                            "body": "Depends on ## Blocked by\n- #5"
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
        edit_calls = [call for call in mock_api.mock_calls if "11" in str(call) and "agent-blocked" in str(call)]
        self.assertTrue(len(edit_calls) > 0)

    @patch("orchestrator.nodes._github_api_request")
    def test_self_healing_unblock(self, mock_api):
        """Test that a blocked issue with all closed dependencies gets transitioned to agent-ready."""
        # Setup mock behavior
        def api_side_effect(method, path, body=None):
            if method == "GET":
                if "/issues" in path and "labels=agent-blocked" in path:
                    # Issue 12 is blocked by 5 and 6
                    return [
                        {
                            "number": 12,
                            "body": "## Blocked by\n- #5\n- #6"
                        }
                    ]
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
        unblock_calls = [call for call in mock_api.mock_calls if "12" in str(call) and "agent-ready" in str(call)]
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
                        {"number": 11, "title": "Task 2", "body": ""}
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
            "AGENT_MODE": "local"
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

    @patch("orchestrator.nodes.get_chat_model")
    @patch("orchestrator.nodes._github_api_request")
    def test_plan_node_success(self, mock_github_api, mock_get_chat_model):
        """Test successful Plan-Node execution: fetches issue, calls LLM, and serializes Pydantic plan."""
        from orchestrator.nodes import DevelopmentPlan, PlanningTask
        
        # Setup mock GitHub API response
        mock_github_api.return_value = {
            "title": "Add DB migration",
            "body": "We need a migration script for user profiles."
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
                    target_files=["schema.py"]
                ),
                PlanningTask(
                    step_number=2,
                    action="patch",
                    description="Add migration",
                    target_files=["migration.py"]
                )
            ]
        )
        mock_structured_llm.invoke.return_value = mock_plan
        
        # Setup initial state with some pre-existing read_files to verify it gets reset per ADR-0006
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["model"] = "gpt-4o"
        state["read_files"] = ["some_old_file.py"]
        state_module.save(state)
        
        # Run plan node
        new_state = plan_node(state)
        
        # Verify state transitions and read_files reset
        self.assertEqual(new_state["status"], "planning")
        self.assertEqual(new_state["phase"], "planning")
        self.assertEqual(new_state["read_files"], [])
        self.assertIsNotNone(new_state["plan"])
        self.assertEqual(new_state["model"], "gpt-4o")
        
        # Verify state file was saved
        saved_state = state_module.load()
        self.assertEqual(saved_state["status"], "planning")
        self.assertEqual(saved_state["read_files"], [])
        
        # Verify serialized plan structure
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

    @patch("orchestrator.nodes.get_chat_model")
    @patch("orchestrator.nodes._github_api_request")
    def test_plan_node_llm_failure(self, mock_github_api, mock_get_chat_model):
        """Test that Plan-Node handles LLM failures by updating status to 'failed' and propagating exception."""
        # Setup mock GitHub API response
        mock_github_api.return_value = {
            "title": "Add DB migration",
            "body": "We need a migration script for user profiles."
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
        self.assertFalse(_is_safe_path(".venv/lib/python3.12/site-packages/something.py"))
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

    @patch("orchestrator.nodes.get_chat_model")
    @patch("orchestrator.nodes._github_api_request")
    def test_plan_node_security_injection_blocking(self, mock_github_api, mock_get_chat_model):
        """Test that Plan-Node catches prompt injection attempts in the generated plan and aborts with a security block."""
        from orchestrator.nodes import DevelopmentPlan, PlanningTask
        
        # Setup mock GitHub API response
        mock_github_api.return_value = {
            "title": "Malicious Issue",
            "body": "System prompt override attempt."
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
                    target_files=[".env"]  # Unsafe file!
                )
            ]
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
        (self.workspace_dir / "src" / "main.py").write_text("print('main')", encoding="utf-8")
        (self.workspace_dir / "src" / "utils" / "helper.py").write_text("def help(): pass", encoding="utf-8")
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
            "AGENT_MODE": "local"
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
            "body": "There is a bug in main.py."
        }
        
        # Setup initial state
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state["read_files"] = ["old_file.py"]
        state_module.save(state)
        
        # Run execute node
        new_state = execute_node(state)
        
        # Verify status transitions, read_files reset
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["phase"], "executing")
        self.assertEqual(new_state["read_files"], [])
        
        # Verify execute_worker was called with correct parameters
        mock_execute_worker.assert_called_once_with(
            "Title: Fix a bug\n\nThere is a bug in main.py.",
            '{"rationale": "...", "tasks": []}',
            "google/gemini-2.5-pro"
        )
        
        # Verify state file was saved
        saved_state = state_module.load()
        self.assertEqual(saved_state["status"], "executing")
        self.assertEqual(saved_state["read_files"], [])

    @patch("orchestrator.worker.execute_worker")
    @patch("orchestrator.nodes._github_api_request")
    def test_execute_node_with_feedback(self, mock_github_api, mock_execute_worker):
        """Test that Execute-Node appends previous verification feedback to the worker prompt on retry."""
        mock_github_api.return_value = {
            "title": "Fix a bug",
            "body": "There is a bug in main.py."
        }
        
        # Setup initial state with feedback
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["plan"] = '{"rationale": "...", "tasks": []}'
        state["feedback"] = "AssertionError: 2 != 3 in test_main.py"
        state_module.save(state)
        
        # Run execute node
        new_state = execute_node(state)
        
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
            "google/gemini-2.5-pro"
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
            "AGENT_MODE": "local"
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

    @patch("orchestrator.nodes.subprocess.run")
    def test_verify_node_success(self, mock_subprocess_run):
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
            timeout=300
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
        mock_subprocess_run.side_effect = subprocess.TimeoutExpired(cmd=["make", "verify"], timeout=300, output=b"Starting tests...\n")
        
        # Setup initial state
        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state_module.save(state)
        
        # Run verify node
        new_state = verify_node(state)
        
        self.assertEqual(new_state["status"], "executing")
        self.assertEqual(new_state["attempts"]["verify"], 1)
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
        feedback_bytes = new_state["feedback"].encode("utf-8")
        self.assertTrue(len(feedback_bytes) <= 10240)
        self.assertIn("exceeded 10 KB limit", new_state["feedback"])


if __name__ == "__main__":
    unittest.main()


