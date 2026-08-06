import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from orchestrator.worker import execute_worker, get_worker_tools


class TestWorkerAgent(unittest.TestCase):
    def setUp(self):
        # Inject mock key to isolate tests and avoid production scaffolding
        self.original_api_key = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "mock-key"
        # Isolate AGENT_LOG_PATH so trace sidecars never pollute the repo
        self.original_log_path = os.environ.get("AGENT_LOG_PATH")
        self.logs_temp = tempfile.TemporaryDirectory()
        os.environ["AGENT_LOG_PATH"] = self.logs_temp.name

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

    def test_get_worker_tools(self):
        """Test that get_worker_tools returns the 7 expected wrapped tools."""
        wrapped_tools = get_worker_tools()
        self.assertEqual(len(wrapped_tools), 7)
        tool_names = {t.name for t in wrapped_tools}
        self.assertEqual(
            tool_names,
            {
                "read_file",
                "list_directory",
                "grep_search",
                "patch_file",
                "run_command",
                "web_search",
                "fetch_url",
            },
        )

    @unittest.mock.patch("langchain_openai.ChatOpenAI.invoke")
    def test_execute_worker_success(self, mock_invoke):
        """Test that execute_worker correctly invokes the agent and returns the final response."""
        mock_invoke.return_value = AIMessage(content="Successfully completed the task.")

        final_answer = execute_worker(
            issue_description="Verify codebase",
            plan="1. Run verify",
            node_name="execute",
        )

        self.assertEqual(final_answer, "Successfully completed the task.")

    def _build_trajectory_messages(self):
        """A representative ReAct trajectory: human → ai(tool_call) → tool → ai(final)."""
        return [
            HumanMessage(content="Solve the issue"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "main.py"},
                        "id": "call_1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content="file contents", tool_call_id="call_1", name="read_file"
            ),
            AIMessage(content="Done."),
        ]

    @unittest.mock.patch("orchestrator.worker.create_agent")
    def test_execute_worker_writes_worker_trace(self, mock_create_agent):
        """The full message trajectory is serialized to worker_trace_<issue>_<attempt>.jsonl."""
        messages = self._build_trajectory_messages()
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {"messages": messages}
        mock_create_agent.return_value = mock_agent

        execute_worker(
            issue_description="x",
            plan="y",
            issue_number=42,
            attempt=1,
        )

        trace_path = Path(os.environ["AGENT_LOG_PATH"]) / "worker_trace_42_1.jsonl"
        self.assertTrue(trace_path.exists(), "Worker trace file should be written")

        records = [json.loads(line) for line in trace_path.read_text().splitlines()]
        self.assertEqual(len(records), 4)

        # Human message
        self.assertEqual(records[0]["role"], "human")
        self.assertEqual(records[0]["content"], "Solve the issue")

        # AI message with tool_calls metadata (name + arguments)
        self.assertEqual(records[1]["role"], "ai")
        self.assertEqual(records[1]["content"], "")
        self.assertEqual(len(records[1]["tool_calls"]), 1)
        self.assertEqual(records[1]["tool_calls"][0]["name"], "read_file")
        self.assertEqual(records[1]["tool_calls"][0]["arguments"], {"path": "main.py"})

        # Tool message: content string, tool_name + tool_call_id present
        self.assertEqual(records[2]["role"], "tool")
        self.assertEqual(records[2]["content"], "file contents")
        self.assertEqual(records[2]["tool_name"], "read_file")
        self.assertEqual(records[2]["tool_call_id"], "call_1")

        # Final AI message
        self.assertEqual(records[3]["role"], "ai")
        self.assertEqual(records[3]["content"], "Done.")

    @unittest.mock.patch("orchestrator.worker.create_agent")
    def test_execute_worker_trace_uses_node_prefix_for_test_writer(
        self, mock_create_agent
    ):
        """test_writer traces use the test_writer_trace_<issue>_<attempt>.jsonl name."""
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {"messages": [AIMessage(content="ok")]}
        mock_create_agent.return_value = mock_agent

        execute_worker(
            issue_description="x",
            plan="y",
            node_name="test_writer",
            issue_number=7,
            attempt=2,
        )

        trace_path = Path(os.environ["AGENT_LOG_PATH"]) / "test_writer_trace_7_2.jsonl"
        self.assertTrue(trace_path.exists())

    @unittest.mock.patch("orchestrator.worker.create_agent")
    def test_execute_worker_skips_trace_without_issue_number(self, mock_create_agent):
        """No trace file is written when issue_number/attempt are not provided."""
        mock_agent = MagicMock()
        mock_agent.invoke.return_value = {"messages": [AIMessage(content="ok")]}
        mock_create_agent.return_value = mock_agent

        execute_worker(issue_description="x", plan="y")

        log_dir = Path(os.environ["AGENT_LOG_PATH"])
        self.assertFalse(any(log_dir.glob("*_trace_*.jsonl")))

    def test_coerce_content_handles_non_serializable(self):
        """_coerce_content safely handles None, bytes (errors=replace), and structured blocks."""
        from orchestrator.worker import _coerce_content

        self.assertEqual(_coerce_content(None), "")
        self.assertEqual(_coerce_content("plain"), "plain")
        # Invalid UTF-8 bytes must not crash; decoded with errors="replace"
        self.assertIsInstance(_coerce_content(b"\xff\xfe binary"), str)
        # Structured content blocks serialized to a JSON string
        coerced = _coerce_content([{"type": "text", "text": "hi"}])
        self.assertIsInstance(coerced, str)
        self.assertIn("hi", coerced)

    def test_serialize_message_coerces_bytes_content(self):
        """_serialize_message coerces bytes content (e.g. raw tool output) to a string."""
        from orchestrator.worker import _serialize_message

        # A tool message whose content is raw bytes (not representable by the
        # typed ToolMessage constructor, but defensively handled at runtime).
        msg = MagicMock()
        msg.type = "tool"
        msg.content = b"\xff\xfe binary"
        msg.name = "run_command"
        msg.tool_call_id = "c1"
        msg.tool_calls = None

        record = _serialize_message(msg)
        self.assertEqual(record["role"], "tool")
        self.assertIsInstance(record["content"], str)
        self.assertEqual(record["tool_name"], "run_command")
        self.assertEqual(record["tool_call_id"], "c1")

    def test_redact_arguments_handles_str_list_and_scalar(self):
        """_redact_arguments redacts str values and recurses into dicts/lists;
        non-str scalars are returned unchanged (ADR-0037 layer 4)."""
        from orchestrator.worker import _redact_arguments

        token = "ghp_" + "a" * 36
        # str -> redacted
        redacted_str = _redact_arguments(token)
        assert isinstance(redacted_str, str)
        self.assertNotIn(token, redacted_str)
        # list of str -> each redacted
        redacted_list = _redact_arguments([token, "plain"])
        assert isinstance(redacted_list, list)
        self.assertNotIn(token, redacted_list[0])
        self.assertEqual(redacted_list[1], "plain")
        # nested dict with list value
        redacted_dict = _redact_arguments({"old_string": token, "nested": [token]})
        assert isinstance(redacted_dict, dict)
        self.assertNotIn(token, redacted_dict["old_string"])
        nested = redacted_dict["nested"]
        assert isinstance(nested, list)
        self.assertNotIn(token, nested[0])
        # non-str scalar returned unchanged
        self.assertEqual(_redact_arguments(123), 123)
        self.assertIsNone(_redact_arguments(None))

    def test_serialize_message_redacts_secret_in_tool_result(self):
        """A secret leaked into a tool result is redacted in the trace record
        (ADR-0037 layer 4)."""
        from orchestrator.worker import _serialize_message

        token = "ghp_" + "a" * 36
        msg = MagicMock()
        msg.type = "tool"
        msg.content = f"the token is {token}"
        msg.name = "run_command"
        msg.tool_call_id = "c1"
        msg.tool_calls = None
        record = _serialize_message(msg)
        self.assertNotIn(token, record["content"])
        self.assertIn("[REDACTED:ghp]", record["content"])

    def test_record_run_metrics_degrades_gracefully(self):
        """_record_run_metrics never raises even if the collector import fails
        (ADR-0016 graceful degradation)."""
        from orchestrator.worker import _record_run_metrics

        # Should not raise regardless of input.
        _record_run_metrics([MagicMock()], "execute")

    def test_coerce_content_fallback_to_str(self):
        """_coerce_content falls back to str() for unhandled content types."""
        from orchestrator.worker import _coerce_content

        result = _coerce_content(object())
        self.assertIsInstance(result, str)

    def test_write_worker_trace_swallows_write_failure(self):
        """_write_worker_trace logs but does not raise when the trace file
        cannot be written (observability never impacts execution)."""
        from orchestrator.worker import _write_worker_trace

        # Point get_log_dir at an unwritable path by monkeypatching it.
        with patch(
            "orchestrator.worker.get_log_dir",
            side_effect=RuntimeError("no log dir"),
        ):
            _write_worker_trace([MagicMock()], 1, 1, "execute")  # must not raise
