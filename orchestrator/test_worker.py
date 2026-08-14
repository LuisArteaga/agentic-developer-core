import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from orchestrator.worker import (
    LoopDetectionMiddleware,
    ToolLoopDetector,
    _canonicalize_args,
    execute_worker,
    get_worker_tools,
)


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
        mock_agent.stream.return_value = iter([{"messages": messages}])
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
        mock_agent.stream.return_value = iter([{"messages": [AIMessage(content="ok")]}])
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
        mock_agent.stream.return_value = iter([{"messages": [AIMessage(content="ok")]}])
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

        # Patch the metrics collector to raise so the graceful-degradation
        # try/except in _record_run_metrics is actually exercised — a plain
        # call with no failure does not cover the except branch.
        with patch(
            "orchestrator.metrics.get_collector",
            side_effect=RuntimeError("no collector"),
        ):
            # Must not raise despite the collector failure.
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

    # ------------------------------------------------------------------
    # Graceful GraphRecursionError handling (ADR-0045, issue #117)
    # ------------------------------------------------------------------

    @staticmethod
    def _stream_then_recursion_error(partial_state):
        """Return a side_effect for agent.stream that yields one partial state
        chunk then raises GraphRecursionError (mirrors real exhaustion)."""

        def _gen(*_args, **_kwargs):
            yield partial_state
            raise GraphRecursionError("Recursion limit reached")

        return _gen

    @unittest.mock.patch("orchestrator.worker.create_agent")
    def test_graceful_exhaustion_returns_message_not_raise(self, mock_create_agent):
        """GraphRecursionError is caught and a deterministic budget-exhausted
        message is returned instead of propagating (ADR-0045 layer 3)."""
        messages = self._build_trajectory_messages()
        mock_agent = MagicMock()
        mock_agent.stream.side_effect = self._stream_then_recursion_error(
            {"messages": messages}
        )
        mock_create_agent.return_value = mock_agent

        result = execute_worker(
            issue_description="x",
            plan="y",
            node_name="execute",
            issue_number=5,
            attempt=2,
        )

        self.assertIsInstance(result, str)
        self.assertIn("[RECURSION_LIMIT_EXHAUSTED]", result)
        self.assertIn("node=execute", result)
        self.assertIn("attempt=2", result)

    @unittest.mock.patch("orchestrator.worker.create_agent")
    def test_graceful_exhaustion_writes_partial_trace(self, mock_create_agent):
        """The Worker Trace sidecar is still written on graceful exhaustion,
        containing the partial trajectory (ADR-0016 / ADR-0045)."""
        messages = self._build_trajectory_messages()
        mock_agent = MagicMock()
        mock_agent.stream.side_effect = self._stream_then_recursion_error(
            {"messages": messages}
        )
        mock_create_agent.return_value = mock_agent

        execute_worker(
            issue_description="x",
            plan="y",
            node_name="execute",
            issue_number=9,
            attempt=1,
        )

        trace_path = Path(os.environ["AGENT_LOG_PATH"]) / "worker_trace_9_1.jsonl"
        self.assertTrue(trace_path.exists())
        records = [json.loads(line) for line in trace_path.read_text().splitlines()]
        # All 4 partial messages are persisted.
        self.assertEqual(len(records), 4)
        self.assertEqual(records[2]["role"], "tool")
        self.assertEqual(records[2]["tool_name"], "read_file")

    @unittest.mock.patch("orchestrator.worker.create_agent")
    def test_graceful_exhaustion_records_partial_metrics(self, mock_create_agent):
        """Run Observability metrics still run on the partial trajectory of a
        gracefully-exhausted run (ADR-0029 / ADR-0045). TL is recorded for the
        execute node from the partial ToolMessage count."""
        from orchestrator.metrics import MetricsCollector

        messages = self._build_trajectory_messages()  # 1 ToolMessage -> TL=1
        mock_agent = MagicMock()
        mock_agent.stream.side_effect = self._stream_then_recursion_error(
            {"messages": messages}
        )
        mock_create_agent.return_value = mock_agent

        collector = MetricsCollector()
        with patch("orchestrator.metrics.get_collector", return_value=collector):
            execute_worker(
                issue_description="x",
                plan="y",
                node_name="execute",
                issue_number=3,
                attempt=1,
            )

        # TL recorded from the partial trajectory (1 tool invocation).
        self.assertEqual(collector.trajectory_lengths, [1])

    @unittest.mock.patch("orchestrator.worker.create_agent")
    def test_graceful_exhaustion_zero_tool_calls_does_not_raise(
        self, mock_create_agent
    ):
        """Exhaustion on the very first step (no productive tool calls — possible
        LangGraph v1 infinite-loop regression) still degrades gracefully rather
        than masking the failure as a crash (ADR-0045 edge case)."""
        # Only the input HumanMessage made it through before the cap was hit.
        partial = {"messages": [HumanMessage(content="go")]}
        mock_agent = MagicMock()
        mock_agent.stream.side_effect = self._stream_then_recursion_error(partial)
        mock_create_agent.return_value = mock_agent

        result = execute_worker(issue_description="x", plan="y", node_name="execute")

        self.assertIsInstance(result, str)
        self.assertIn("[RECURSION_LIMIT_EXHAUSTED]", result)
        self.assertIn("0 tool", result)

    @unittest.mock.patch("orchestrator.worker.create_agent")
    def test_configurable_recursion_limit_honored(self, mock_create_agent):
        """The per-node recursion_limit resolved via resolve_model_config() is
        passed into the LangGraph run config (no hardcoded magic constant)."""
        mock_agent = MagicMock()
        mock_agent.stream.return_value = iter(
            [{"messages": [AIMessage(content="done")]}]
        )
        mock_create_agent.return_value = mock_agent

        with patch.dict(os.environ, {"EXECUTE_RECURSION_LIMIT": "7"}):
            execute_worker(issue_description="x", plan="y", node_name="execute")

        config = mock_agent.stream.call_args.kwargs["config"]
        self.assertEqual(config["recursion_limit"], 7)

    @unittest.mock.patch("orchestrator.worker.create_agent")
    def test_success_path_uses_default_recursion_limit(self, mock_create_agent):
        """Without overrides, the default recursion_limit (50) is applied."""
        mock_agent = MagicMock()
        mock_agent.stream.return_value = iter(
            [{"messages": [AIMessage(content="done")]}]
        )
        mock_create_agent.return_value = mock_agent

        # Clear any stray env override so the default is used.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXECUTE_RECURSION_LIMIT", None)
            os.environ.pop("AGENT_RECURSION_LIMIT", None)
            execute_worker(issue_description="x", plan="y", node_name="execute")

        config = mock_agent.stream.call_args.kwargs["config"]
        self.assertEqual(config["recursion_limit"], 50)

    # ------------------------------------------------------------------
    # _summarize_last_action branch coverage (ADR-0045)
    # ------------------------------------------------------------------

    def test_summarize_last_action_empty_messages(self):
        """Empty message list returns 'none (no steps taken)'."""
        from orchestrator.worker import _summarize_last_action

        self.assertEqual(_summarize_last_action([]), "none (no steps taken)")

    def test_summarize_last_action_tool_calls(self):
        """Last message with tool_calls returns the tool-call summary."""
        from orchestrator.worker import _summarize_last_action

        msg = AIMessage(
            content="",
            tool_calls=[
                {"name": "read_file", "args": {}, "id": "c1", "type": "tool_call"},
                {"name": "patch_file", "args": {}, "id": "c2", "type": "tool_call"},
            ],
        )
        result = _summarize_last_action([HumanMessage(content="go"), msg])
        self.assertIn("read_file", result)
        self.assertIn("patch_file", result)

    def test_summarize_last_action_content_snippet(self):
        """Last message with content returns a truncated snippet."""
        from orchestrator.worker import _summarize_last_action

        msg = AIMessage(content="I have finished editing the file.")
        result = _summarize_last_action([msg])
        self.assertEqual(result, "I have finished editing the file.")

    def test_summarize_last_action_long_content_truncated(self):
        """Content longer than 200 chars is truncated with '...'."""
        from orchestrator.worker import _summarize_last_action

        long_text = "x" * 300
        msg = AIMessage(content=long_text)
        result = _summarize_last_action([msg])
        self.assertTrue(result.endswith("..."))
        self.assertEqual(len(result), 203)  # 200 + "..."

    def test_summarize_last_action_no_content_no_tool_calls(self):
        """Last message with empty content and no tool_calls returns
        'no further action'."""
        from orchestrator.worker import _summarize_last_action

        msg = AIMessage(content="")
        self.assertEqual(_summarize_last_action([msg]), "no further action")

    def test_summarize_last_action_exception_returns_unknown(self):
        """Any exception inside the helper returns 'unknown' (must not raise,
        per ADR-0045 graceful-exhaustion path safety)."""
        from orchestrator.worker import _summarize_last_action

        # A mock that raises on getattr(last, "tool_calls", None) — simulates
        # a broken message object. The except clause catches it.
        broken = MagicMock()
        # Make .tool_calls property access raise via a side-effecting spec.
        type(broken).tool_calls = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        broken.content = ""
        self.assertEqual(_summarize_last_action([broken]), "unknown")


class TestToolLoopDetector(unittest.TestCase):
    """Unit tests for the ToolLoopDetector (issue #121 / ADR-0046)."""

    def test_no_signatures_is_ok(self):
        d = ToolLoopDetector(warn_threshold=3, hard_limit=5, window_size=10)
        self.assertEqual(d.status(), ("ok", None, 0))

    def test_warn_at_threshold(self):
        """3 identical repeats emit a single warn."""
        d = ToolLoopDetector(warn_threshold=3, hard_limit=5, window_size=10)
        d.record("run_command", {"command": "pip install x"})
        d.record("run_command", {"command": "pip install x"})
        self.assertEqual(d.status()[0], "ok")  # 2 < 3
        d.record("run_command", {"command": "pip install x"})
        state, sig, cnt = d.status()
        self.assertEqual(state, "warn")
        self.assertEqual(cnt, 3)
        assert sig is not None
        self.assertEqual(sig[0], "run_command")

    def test_warn_fires_once_per_episode(self):
        """After the first warn, subsequent repeats below hard_limit do not re-warn."""
        d = ToolLoopDetector(warn_threshold=3, hard_limit=5, window_size=10)
        for _ in range(3):
            d.record("run_command", {"command": "pip install x"})
        self.assertEqual(d.status()[0], "warn")  # first warn
        d.record("run_command", {"command": "pip install x"})
        self.assertEqual(d.status()[0], "ok")  # already warned (count 4)

    def test_terminate_at_hard_limit(self):
        """5 identical repeats force terminate, even after a prior warn."""
        d = ToolLoopDetector(warn_threshold=3, hard_limit=5, window_size=10)
        for _ in range(3):
            d.record("run_command", {"command": "pip install x"})
        d.status()  # triggers warn
        for _ in range(2):
            d.record("run_command", {"command": "pip install x"})
        state, sig, cnt = d.status()
        self.assertEqual(state, "terminate")
        self.assertEqual(cnt, 5)

    def test_varied_args_not_flagged(self):
        """Identical name but different arguments are distinct keys (pagination)."""
        d = ToolLoopDetector(warn_threshold=3, hard_limit=5, window_size=10)
        for i in range(5):
            d.record("read_file", {"path": f"file_{i}", "start_line": i})
        state, _, cnt = d.status()
        self.assertEqual(state, "ok")
        self.assertEqual(cnt, 1)  # each signature appears once

    def test_different_offset_is_distinct(self):
        """Same path, different offset (paginated read) is not a loop."""
        d = ToolLoopDetector(warn_threshold=3, hard_limit=5, window_size=10)
        d.record("read_file", {"path": "a", "start_line": 1})
        d.record("read_file", {"path": "a", "start_line": 11})
        d.record("read_file", {"path": "a", "start_line": 21})
        state, _, cnt = d.status()
        self.assertEqual(state, "ok")
        self.assertEqual(cnt, 1)

    def test_transient_retry_not_warned(self):
        """A single identical retry (count 2) is not warned (warn_threshold >= 2)."""
        d = ToolLoopDetector(warn_threshold=3, hard_limit=5, window_size=10)
        d.record("run_command", {"command": "flake8 ."})
        d.record("run_command", {"command": "flake8 ."})  # transient retry succeeds
        self.assertEqual(d.status()[0], "ok")

    def test_oscillation_caught_by_window(self):
        """A->B->A->B... oscillation accumulates each signature within the window."""
        d = ToolLoopDetector(warn_threshold=3, hard_limit=5, window_size=10)
        # 5 As and 5 Bs interleaved -> each reaches 5 within the window of 10
        for _ in range(5):
            d.record("run_command", {"command": "a"})
            d.record("run_command", {"command": "b"})
        state, _, cnt = d.status()
        self.assertEqual(state, "terminate")
        self.assertEqual(cnt, 5)

    def test_window_slides_and_resets(self):
        """A signature that leaves the window is pruned from _warned, so it can
        be re-warned if it recurs in a fresh episode."""
        d = ToolLoopDetector(warn_threshold=2, hard_limit=10, window_size=3)
        # Episode 1: A repeats -> warn A.
        d.record("run_command", {"command": "a"})
        d.record("run_command", {"command": "a"})
        self.assertEqual(d.status()[0], "warn")
        # Fill the window with B so A evicts out of it.
        d.record("run_command", {"command": "b"})
        d.record("run_command", {"command": "b"})
        d.record("run_command", {"command": "b"})
        # Episode 2: A recurs. It was pruned from _warned, so it warns again.
        d.record("run_command", {"command": "a"})
        d.record("run_command", {"command": "a"})
        state, sig, _ = d.status()
        self.assertEqual(state, "warn")
        assert sig is not None
        self.assertIn('"command": "a"', sig[1])

    def test_disabled_when_hard_limit_zero(self):
        """hard_limit <= 0 disables detection (always ok)."""
        d = ToolLoopDetector(warn_threshold=3, hard_limit=0, window_size=10)
        for _ in range(20):
            d.record("run_command", {"command": "pip install x"})
        self.assertEqual(d.status(), ("ok", None, 0))

    def test_canonicalize_args_sorted_keys(self):
        """Dict args are canonicalized with sorted keys (order-independent)."""
        a = _canonicalize_args({"b": 1, "a": 2})
        b = _canonicalize_args({"a": 2, "b": 1})
        self.assertEqual(a, b)
        self.assertIn('"a"', a)

    def test_canonicalize_args_handles_non_serializable(self):
        """Non-JSON-serializable values never raise (default=str fallback)."""

        class Weird:
            pass

        result = _canonicalize_args({"obj": Weird()})
        self.assertIsInstance(result, str)


class TestLoopDetectionMiddlewareIntegration(unittest.TestCase):
    """Integration test: a real create_agent with a synthetic repeat-model
    force-terminates via the middleware before the recursion budget is spent
    (issue #121 / ADR-0046)."""

    def test_repeating_model_terminates_early_with_partial_trajectory(self):
        from langchain.agents import create_agent
        from langchain_core.tools import tool

        # A fake model that ALWAYS requests the same tool call — the issue #13
        # failure pattern (repeated `pip install` until budget exhaustion).
        class RepeatModel:
            def bind_tools(self, tools, **kwargs):
                return self

            def with_structured_output(self, *a, **k):
                return self

            def invoke(self, messages, config=None, **kwargs):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "run_command",
                            "args": {"command": "pip install -e .[dev]"},
                            "id": "c1",
                            "type": "tool_call",
                        }
                    ],
                )

        @tool
        def run_command(command: str) -> str:
            """Run a shell command."""
            return "usage: pip ... (no install performed)"

        detector = ToolLoopDetector(warn_threshold=3, hard_limit=5, window_size=10)
        mw = LoopDetectionMiddleware(detector, node_name="execute", attempt=1)
        agent = create_agent(
            RepeatModel(),
            [run_command],
            system_prompt="you are a worker",
            middleware=[mw],
        )  # type: ignore[call-overload]

        last_state = None
        try:
            for chunk in agent.stream(
                {"messages": [("user", "do the task")]},
                config={"recursion_limit": 50},
                stream_mode="values",
            ):
                last_state = chunk
        except GraphRecursionError:
            self.fail("Loop detection should have terminated before budget exhaustion")

        messages = (last_state or {}).get("messages", [])

        # The middleware force-terminated; the budget crash guard did NOT fire.
        self.assertTrue(mw.terminated)
        # Far fewer than the full budget: exactly hard_limit tool calls.
        tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
        self.assertEqual(len(tool_msgs), 5)
        # A steering message was injected at the warn threshold.
        self.assertTrue(
            any(
                isinstance(m, SystemMessage) and "[LOOP DETECTION]" in m.content
                for m in messages
            )
        )
        # A usable partial trajectory was captured.
        self.assertGreater(len(messages), 0)


class TestExecuteWorkerLoopDetection(unittest.TestCase):
    """execute_worker returns a deterministic [LOOP_DETECTED] message and still
    writes the trace + metrics when the middleware force-terminates (ADR-0046)."""

    def setUp(self):
        self.original_api_key = os.environ.get("OPENROUTER_API_KEY")
        os.environ["OPENROUTER_API_KEY"] = "mock-key"
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

    @unittest.mock.patch("orchestrator.worker.create_agent")
    def test_loop_termination_returns_deterministic_message(self, mock_create_agent):
        """When the middleware force-terminates, execute_worker returns
        [LOOP_DETECTED] (counted as one failed attempt) and still writes the
        worker trace on the partial trajectory (ADR-0046)."""
        messages = [
            HumanMessage(content="Solve the issue"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_command",
                        "args": {"command": "pip install x"},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content="usage: pip ...", tool_call_id="c1", name="run_command"
            ),
        ]

        captured = {}

        def fake_create_agent(llm, tools, system_prompt=None, middleware=None):
            # execute_worker passes the LoopDetectionMiddleware it constructed;
            # simulate the middleware having force-terminated during the run so
            # execute_worker's post-stream check reads terminated=True.
            captured["middleware"] = middleware
            if middleware:
                mw = middleware[0]
                mw.terminated = True
                mw.termination_signature = (
                    "run_command",
                    '{"command": "pip install x"}',
                )
                mw.termination_count = 5
            mock_agent = MagicMock()
            mock_agent.stream.return_value = iter([{"messages": messages}])
            return mock_agent

        mock_create_agent.side_effect = fake_create_agent

        result = execute_worker(
            issue_description="x",
            plan="y",
            node_name="execute",
            issue_number=13,
            attempt=1,
        )

        # The middleware was attached (default hard_limit > 0).
        self.assertIsNotNone(captured.get("middleware"))
        self.assertEqual(len(captured["middleware"]), 1)
        self.assertIn("[LOOP_DETECTED]", result)
        self.assertIn("pip install x", result)
        self.assertIn("failed attempt", result)

        # The worker trace sidecar still runs on the partial trajectory.
        trace_path = Path(os.environ["AGENT_LOG_PATH"]) / "worker_trace_13_1.jsonl"
        self.assertTrue(trace_path.exists())
