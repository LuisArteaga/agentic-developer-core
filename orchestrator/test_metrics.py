import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from orchestrator.metrics import (
    METRICS_FILENAME,
    MetricsCollector,
    TokenUsageCallbackHandler,
    compute_plan_alignment,
    count_tool_invocations,
    extract_planned_files,
    get_collector,
    sum_message_tokens,
)


def _llm_result(usage_list, llm_output=None):
    """Build an LLMResult whose generations carry AIMessages with usage_metadata."""
    generations = [
        [ChatGeneration(message=AIMessage(content="x", usage_metadata=um))]
        for um in usage_list
    ]
    return LLMResult(generations=generations, llm_output=llm_output)  # type: ignore[arg-type]


class TestTrajectoryLength(unittest.TestCase):
    def test_counts_tool_messages_only(self):
        """TL equals the number of ToolMessage objects (one per tool invocation)."""
        messages = [
            HumanMessage(content="solve"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {}, "id": "1", "type": "tool_call"}
                ],
            ),
            ToolMessage(content="ok", tool_call_id="1", name="read_file"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "patch_file", "args": {}, "id": "2", "type": "tool_call"}
                ],
            ),
            ToolMessage(content="patched", tool_call_id="2", name="patch_file"),
            AIMessage(content="done"),
        ]
        self.assertEqual(count_tool_invocations(messages), 2)

    def test_counts_zero_when_no_tool_messages(self):
        self.assertEqual(
            count_tool_invocations([HumanMessage(content="x"), AIMessage(content="y")]),
            0,
        )

    def test_counts_all_seven_worker_tools(self):
        """Every Worker Tool (incl. web_search, fetch_url) produces a ToolMessage."""
        messages = [
            ToolMessage(content=str(i), tool_call_id=str(i), name=n)
            for i, n in enumerate(
                [
                    "read_file",
                    "list_directory",
                    "grep_search",
                    "patch_file",
                    "run_command",
                    "web_search",
                    "fetch_url",
                ]
            )
        ]
        self.assertEqual(count_tool_invocations(messages), 7)


class TestTokenSummation(unittest.TestCase):
    def test_sums_total_tokens_across_ai_messages(self):
        messages = [
            AIMessage(
                content="a",
                usage_metadata={
                    "total_tokens": 100,
                    "input_tokens": 80,
                    "output_tokens": 20,
                },
            ),
            ToolMessage(content="t", tool_call_id="1", name="read_file"),
            AIMessage(
                content="b",
                usage_metadata={
                    "total_tokens": 50,
                    "input_tokens": 40,
                    "output_tokens": 10,
                },
            ),
        ]
        self.assertEqual(sum_message_tokens(messages), 150)

    def test_skips_messages_without_usage_metadata(self):
        messages = [
            AIMessage(content="a"),  # usage_metadata is None
            AIMessage(
                content="b",
                usage_metadata={
                    "total_tokens": 30,
                    "input_tokens": 20,
                    "output_tokens": 10,
                },
            ),
        ]
        self.assertEqual(sum_message_tokens(messages), 30)


class TestTokenUsageCallbackHandler(unittest.TestCase):
    def test_reads_usage_metadata_without_model_name(self):
        """The handler records tokens even though response_metadata has no model_name.

        This is the core ADR-0029 motivation: the canonical UsageMetadataCallbackHandler
        keys on model_name (absent in the chat-completions path for OpenRouter) and would
        silently record zero. Our handler reads usage_metadata directly.
        """
        handler = TokenUsageCallbackHandler()
        # AIMessage built without response_metadata.model_name (chat-completions path).
        result = _llm_result(
            [{"total_tokens": 120, "input_tokens": 100, "output_tokens": 20}]
        )
        handler.on_llm_end(result)
        self.assertEqual(handler.total_tokens, 120)

    def test_accumulates_across_multiple_invokes(self):
        """A single handler sums tokens across several invoke() calls (e.g. Plan Detail Request)."""
        handler = TokenUsageCallbackHandler()
        handler.on_llm_end(
            _llm_result(
                [{"total_tokens": 120, "input_tokens": 100, "output_tokens": 20}]
            )
        )
        handler.on_llm_end(
            _llm_result([{"total_tokens": 80, "input_tokens": 60, "output_tokens": 20}])
        )
        self.assertEqual(handler.total_tokens, 200)

    def test_falls_back_to_llm_output_token_usage(self):
        """When the AIMessage carries no usage_metadata, fall back to llm_output.token_usage."""
        handler = TokenUsageCallbackHandler()
        gens = [[ChatGeneration(message=AIMessage(content="x"))]]  # no usage_metadata
        result = LLMResult(
            generations=gens,  # type: ignore[arg-type]
            llm_output={
                "token_usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                }
            },
        )
        handler.on_llm_end(result)
        self.assertEqual(handler.total_tokens, 15)

    def test_never_raises_on_empty_generations(self):
        handler = TokenUsageCallbackHandler()
        handler.on_llm_end(LLMResult(generations=[], llm_output=None))
        self.assertEqual(handler.total_tokens, 0)

    def test_never_raises_on_malformed_response(self):
        handler = TokenUsageCallbackHandler()
        # A response whose generations raise on iteration must not break the handler.
        bad = MagicMock()
        bad.generations.__iter__.side_effect = RuntimeError("boom")
        handler.on_llm_end(bad)
        self.assertEqual(handler.total_tokens, 0)


class TestPlanAlignment(unittest.TestCase):
    def test_overlap_ratio(self):
        planned = ["a.py", "b.py", "c.py"]
        modified = ["a.py", "b.py", "d.py"]
        # overlap = {a.py, b.py} = 2; modified = 3 -> 0.6667
        pa = compute_plan_alignment(planned, modified)
        assert pa is not None
        self.assertAlmostEqual(pa, 0.6667, places=4)

    def test_full_alignment(self):
        planned = ["a.py", "b.py"]
        modified = ["a.py", "b.py"]
        self.assertEqual(compute_plan_alignment(planned, modified), 1.0)

    def test_zero_alignment(self):
        planned = ["a.py"]
        modified = ["b.py"]
        self.assertEqual(compute_plan_alignment(planned, modified), 0.0)

    def test_none_when_no_files_modified(self):
        self.assertIsNone(compute_plan_alignment(["a.py"], []))
        self.assertIsNone(compute_plan_alignment([], []))

    def test_normalizes_leading_dot_slash_and_backslashes(self):
        planned = ["./src/a.py", "b.py"]
        modified = ["src\\a.py", "./b.py"]
        self.assertEqual(compute_plan_alignment(planned, modified), 1.0)

    def test_dedupes_modified_files(self):
        planned = ["a.py"]
        modified = ["a.py", "a.py"]
        self.assertEqual(compute_plan_alignment(planned, modified), 1.0)


class TestExtractPlannedFiles(unittest.TestCase):
    def _plan_json(self, tasks):
        return json.dumps({"rationale": "r", "tasks": tasks, "requested_files": []})

    def test_extracts_deduped_target_files(self):
        plan = self._plan_json(
            [
                {
                    "step_number": 1,
                    "action": "read",
                    "description": "d",
                    "target_files": ["a.py", "b.py"],
                },
                {
                    "step_number": 2,
                    "action": "patch",
                    "description": "d",
                    "target_files": ["b.py", "c.py"],
                },
            ]
        )
        self.assertEqual(extract_planned_files(plan), ["a.py", "b.py", "c.py"])

    def test_excludes_requested_files(self):
        """requested_files are outline requests, not planned modifications."""
        plan = json.dumps(
            {
                "rationale": "r",
                "tasks": [
                    {
                        "step_number": 1,
                        "action": "patch",
                        "description": "d",
                        "target_files": ["a.py"],
                    }
                ],
                "requested_files": ["outline_only.py"],
            }
        )
        self.assertEqual(extract_planned_files(plan), ["a.py"])

    def test_empty_plan_returns_empty(self):
        self.assertEqual(extract_planned_files(None), [])
        self.assertEqual(extract_planned_files(""), [])

    def test_invalid_json_returns_empty(self):
        self.assertEqual(extract_planned_files("not json"), [])

    def test_missing_tasks_key_returns_empty(self):
        self.assertEqual(extract_planned_files(json.dumps({"rationale": "r"})), [])


class TestMetricsCollector(unittest.TestCase):
    def setUp(self):
        self.collector = MetricsCollector()

    def test_reset_clears_accumulated_state(self):
        self.collector.add_trajectory_length(5)
        self.collector.add_planning_tokens(100)
        self.collector.add_execution_tokens(200)
        self.collector.set_plan_alignment(0.5, ["a.py"], ["a.py", "b.py"])
        self.collector.reset()
        self.assertEqual(self.collector.trajectory_lengths, [])
        self.assertEqual(self.collector.tokens_planning, 0)
        self.assertEqual(self.collector.tokens_execution, 0)
        self.assertIsNone(self.collector.plan_alignment)
        self.assertEqual(self.collector.files_planned, [])
        self.assertEqual(self.collector.files_modified, [])

    def test_aggregation_and_totals(self):
        self.collector.add_trajectory_length(5)
        self.collector.add_trajectory_length(3)
        self.collector.add_planning_tokens(100)
        self.collector.add_execution_tokens(250)
        self.assertEqual(self.collector.trajectory_lengths, [5, 3])
        self.assertEqual(self.collector.trajectory_length_total, 8)
        self.assertEqual(self.collector.execute_attempts, 2)
        self.assertEqual(self.collector.tokens_total, 350)

    def test_to_record_schema(self):
        self.collector.add_trajectory_length(4)
        self.collector.add_planning_tokens(100)
        self.collector.add_execution_tokens(200)
        self.collector.set_plan_alignment(1.0, ["a.py"], ["a.py"])
        record = self.collector.to_record(
            issue_number=42, branch="feat/issue-42", model="m", status="done"
        )
        expected_keys = {
            "timestamp",
            "issue_number",
            "branch",
            "model",
            "status",
            "trajectory_lengths",
            "trajectory_length_total",
            "execute_attempts",
            "plan_alignment",
            "files_planned",
            "files_modified",
            "tokens_planning",
            "tokens_execution",
            "tokens_total",
        }
        self.assertEqual(set(record.keys()), expected_keys)
        self.assertEqual(record["issue_number"], 42)
        self.assertEqual(record["trajectory_lengths"], [4])
        self.assertEqual(record["trajectory_length_total"], 4)
        self.assertEqual(record["execute_attempts"], 1)
        self.assertEqual(record["plan_alignment"], 1.0)
        self.assertEqual(record["tokens_planning"], 100)
        self.assertEqual(record["tokens_execution"], 200)
        self.assertEqual(record["tokens_total"], 300)

    def test_write_record_appends_one_jsonl_line(self):
        logs_temp = tempfile.TemporaryDirectory()
        self.addCleanup(logs_temp.cleanup)
        original = os.environ.get("AGENT_LOG_PATH")
        os.environ["AGENT_LOG_PATH"] = logs_temp.name
        self.addCleanup(
            lambda: (
                os.environ.pop("AGENT_LOG_PATH", None)
                if original is None
                else os.environ.__setitem__("AGENT_LOG_PATH", original)
            )
        )

        self.collector.add_trajectory_length(7)
        self.collector.add_planning_tokens(100)
        self.collector.add_execution_tokens(200)
        self.collector.write_record(issue_number=42, branch="b", model="m")

        path = Path(logs_temp.name) / METRICS_FILENAME
        self.assertTrue(path.exists())
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["issue_number"], 42)
        self.assertEqual(record["trajectory_length_total"], 7)
        self.assertEqual(record["tokens_total"], 300)

    def test_write_record_never_raises_on_bad_log_dir(self):
        """A write failure is swallowed so observability cannot break execution."""
        original = os.environ.get("AGENT_LOG_PATH")
        os.environ["AGENT_LOG_PATH"] = "/nonexistent/path/that/cannot/be/created"
        self.addCleanup(
            lambda: (
                os.environ.pop("AGENT_LOG_PATH", None)
                if original is None
                else os.environ.__setitem__("AGENT_LOG_PATH", original)
            )
        )
        # Must not raise.
        self.collector.write_record(issue_number=1, branch="b", model="m")

    def test_mutators_swallows_bad_input(self):
        """add_trajectory_length / token mutators never raise on non-int input."""
        self.collector.add_trajectory_length("not-an-int")  # type: ignore[arg-type]
        self.collector.add_planning_tokens("oops")  # type: ignore[arg-type]
        self.collector.add_execution_tokens(None)  # type: ignore[arg-type]
        self.assertEqual(self.collector.trajectory_lengths, [])
        self.assertEqual(self.collector.tokens_planning, 0)


class TestRunMetricsWiring(unittest.TestCase):
    """Verify execute_worker records TL (execute only) and execution tokens (always)."""

    def setUp(self):
        self.original_api_key = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "mock-key"
        self.logs_temp = tempfile.TemporaryDirectory()
        self.original_log_path = os.environ.get("AGENT_LOG_PATH")
        os.environ["AGENT_LOG_PATH"] = self.logs_temp.name
        # Isolate the process-wide collector so tests don't pollute each other.
        get_collector().reset()

    def tearDown(self):
        if self.original_api_key is not None:
            os.environ["OPENROUTER_API_KEY"] = self.original_api_key
        elif "OPENROUTER_API_KEY" in os.environ:
            del os.environ["OPENROUTER_API_KEY"]
        if self.original_log_path is not None:
            os.environ["AGENT_LOG_PATH"] = self.original_log_path
        elif "AGENT_LOG_PATH" in os.environ:
            del os.environ["AGENT_LOG_PATH"]
        self.logs_temp.cleanup()
        get_collector().reset()

    def _trajectory_messages(self):
        """human -> ai(tool_call) -> tool -> ai(final), with usage_metadata."""
        return [
            HumanMessage(content="solve"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_file", "args": {}, "id": "c1", "type": "tool_call"}
                ],
                usage_metadata={
                    "total_tokens": 50,
                    "input_tokens": 40,
                    "output_tokens": 10,
                },
            ),
            ToolMessage(content="ok", tool_call_id="c1", name="read_file"),
            AIMessage(
                content="done",
                usage_metadata={
                    "total_tokens": 30,
                    "input_tokens": 20,
                    "output_tokens": 10,
                },
            ),
        ]

    @unittest.mock.patch("orchestrator.worker.create_react_agent")
    def test_execute_worker_records_tl_and_execution_tokens(self, mock_create_agent):
        from orchestrator.worker import execute_worker

        messages = self._trajectory_messages()
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {"messages": messages}
        mock_create_agent.return_value = mock_agent

        execute_worker("x", "y", node_name="execute", issue_number=42, attempt=1)

        collector = get_collector()
        # 1 ToolMessage -> TL == 1; 50 + 30 == 80 execution tokens.
        self.assertEqual(collector.trajectory_lengths, [1])
        self.assertEqual(collector.tokens_execution, 80)

    @unittest.mock.patch("orchestrator.worker.create_react_agent")
    def test_test_writer_records_execution_tokens_but_not_tl(self, mock_create_agent):
        from orchestrator.worker import execute_worker

        messages = self._trajectory_messages()
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {"messages": messages}
        mock_create_agent.return_value = mock_agent

        execute_worker("x", "y", node_name="test_writer", issue_number=42, attempt=1)

        collector = get_collector()
        # TL is execute-only: test_writer must NOT record a trajectory length.
        self.assertEqual(collector.trajectory_lengths, [])
        # But execution tokens are still accumulated (Test-Writer feeds tokens_execution).
        self.assertEqual(collector.tokens_execution, 80)


class TestPlanNodeTokenCapture(unittest.TestCase):
    """Verify plan_node attaches the token handler and records planning tokens."""

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
        get_collector().reset()

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()
        get_collector().reset()

    @unittest.mock.patch("orchestrator.nodes.get_chat_model_from_config")
    @unittest.mock.patch("orchestrator.nodes._github_api_request")
    def test_plan_node_records_planning_tokens(
        self, mock_github_api, mock_get_chat_model
    ):
        from orchestrator.nodes import DevelopmentPlan, PlanningTask, plan_node
        from orchestrator.state import DEFAULT_STATE

        mock_github_api.return_value = {"title": "t", "body": "b"}

        # A real structured_llm whose invoke() fires the callback's on_llm_end
        # with a populated AIMessage.usage_metadata (chat-completions shape).
        mock_llm = unittest.mock.MagicMock()
        mock_structured_llm = unittest.mock.MagicMock()
        mock_get_chat_model.return_value = mock_llm
        mock_llm.with_structured_output.return_value = mock_structured_llm

        plan = DevelopmentPlan(
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

        def fake_invoke(prompt, config=None):
            # Simulate the underlying LLM call: fire the attached callback.
            for handler in (config or {}).get("callbacks", []):
                handler.on_llm_end(
                    _llm_result(
                        [
                            {
                                "total_tokens": 333,
                                "input_tokens": 300,
                                "output_tokens": 33,
                            }
                        ]
                    )
                )
            return plan

        mock_structured_llm.invoke.side_effect = fake_invoke

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 10
        state["model"] = "m"
        plan_node(state)

        self.assertEqual(get_collector().tokens_planning, 333)


class TestPrNodePlanAlignment(unittest.TestCase):
    """Verify pr_node records Plan Alignment from the committed branch diff."""

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
        }.items():
            self.original_env[k] = os.environ.get(k)
            os.environ[k] = v
        # Initialize a git repo with an origin remote so origin/main...HEAD resolves.
        import subprocess

        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "t@e.com"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        (self.workspace_dir / "README.md").write_text("init", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=str(self.workspace_dir), check=True)
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        origin_temp = tempfile.TemporaryDirectory()
        self.addCleanup(origin_temp.cleanup)
        origin_path = Path(origin_temp.name).resolve() / "origin.git"
        subprocess.run(
            ["git", "init", "--bare", str(origin_path)], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "remote", "add", "origin", str(origin_path)],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        # Create and checkout the feature branch.
        subprocess.run(
            ["git", "checkout", "-b", "feat/issue-42"],
            cwd=str(self.workspace_dir),
            check=True,
            capture_output=True,
        )
        get_collector().reset()

    def tearDown(self):
        for k, v in self.original_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.workspace_temp.cleanup()
        self.logs_temp.cleanup()
        get_collector().reset()

    @unittest.mock.patch("orchestrator.nodes._github_api_request")
    def test_pr_node_records_plan_alignment_after_commit(self, mock_github_api):
        from orchestrator.nodes import pr_node
        from orchestrator.state import DEFAULT_STATE

        # Plan targets a.py and b.py; the worker actually modifies a.py and c.py.
        plan = json.dumps(
            {
                "rationale": "r",
                "tasks": [
                    {
                        "step_number": 1,
                        "action": "patch",
                        "description": "d",
                        "target_files": ["a.py", "b.py"],
                    },
                ],
                "requested_files": [],
            }
        )
        (self.workspace_dir / "a.py").write_text("a=1", encoding="utf-8")
        (self.workspace_dir / "c.py").write_text("c=1", encoding="utf-8")

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 42
        state["branch"] = "feat/issue-42"
        state["model"] = "m"
        state["plan"] = plan

        # Per-call GitHub API responses: no existing PR, issue body, PR creation ack.
        mock_github_api.side_effect = [[], {"title": "t", "body": "b"}, {"number": 1}]

        pr_node(state)

        collector = get_collector()
        # planned = {a.py, b.py}; modified = {a.py, c.py}; overlap = {a.py} -> 0.5
        pa = collector.plan_alignment
        assert pa is not None
        self.assertAlmostEqual(pa, 0.5, places=4)
        self.assertEqual(set(collector.files_planned), {"a.py", "b.py"})
        self.assertEqual(set(collector.files_modified), {"a.py", "c.py"})

    @unittest.mock.patch("orchestrator.nodes._github_api_request")
    def test_pr_node_pa_degrades_gracefully_on_bad_git_ref(self, mock_github_api):
        """If the diff ref is unavailable, PA becomes None but pr_node still succeeds."""
        from orchestrator.nodes import pr_node
        from orchestrator.state import DEFAULT_STATE

        plan = json.dumps(
            {
                "rationale": "r",
                "tasks": [
                    {
                        "step_number": 1,
                        "action": "patch",
                        "description": "d",
                        "target_files": ["a.py"],
                    },
                ],
                "requested_files": [],
            }
        )
        (self.workspace_dir / "a.py").write_text("a=1", encoding="utf-8")

        state = DEFAULT_STATE.copy()
        state["issue_number"] = 42
        state["branch"] = "feat/issue-42"
        state["model"] = "m"
        state["plan"] = plan
        mock_github_api.side_effect = [[], {"title": "t", "body": "b"}, {"number": 1}]

        # Sabotage origin/main ref so the diff range cannot resolve.
        import subprocess

        subprocess.run(
            ["git", "update-ref", "-d", "refs/remotes/origin/main"],
            cwd=str(self.workspace_dir),
            check=False,
            capture_output=True,
        )

        pr_node(state)  # must not raise

        collector = get_collector()
        self.assertIsNone(collector.plan_alignment)


if __name__ == "__main__":
    unittest.main()
