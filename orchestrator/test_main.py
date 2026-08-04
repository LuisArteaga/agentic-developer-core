import unittest
from unittest.mock import patch

from orchestrator.__main__ import main


class TestMainTelemetryWiring(unittest.TestCase):
    """Tests that __main__.main() correctly wires issue_number and branch to init_telemetry.

    Regression test for issue #34: the call site in main() must pass the loaded
    state's issue_number and branch to init_telemetry so the Langfuse session ID
    is constructed correctly on both fresh runs and stateful resumes.
    """

    @patch("orchestrator.__main__.end_orchestrator_loop")
    @patch("orchestrator.__main__.init_telemetry")
    @patch("orchestrator.__main__.graph")
    @patch("orchestrator.__main__.state_module")
    @patch("orchestrator.__main__.setup_logging")
    def test_resume_passes_issue_and_branch_to_telemetry(
        self,
        mock_setup_logging,
        mock_state_module,
        mock_graph,
        mock_init_telemetry,
        mock_end_loop,
    ):
        """On resume, main() passes loaded issue_number and branch to init_telemetry."""
        resume_state = {
            "issue_number": 42,
            "branch": "feat/issue-42",
            "status": "claimed",
        }
        mock_state_module.load.return_value = resume_state
        mock_graph.invoke.return_value = resume_state

        main()

        mock_init_telemetry.assert_called_once_with(
            reset_state=False,
            issue_number=42,
            branch="feat/issue-42",
        )

    @patch("orchestrator.__main__.end_orchestrator_loop")
    @patch("orchestrator.__main__.init_telemetry")
    @patch("orchestrator.__main__.graph")
    @patch("orchestrator.__main__.state_module")
    @patch("orchestrator.__main__.setup_logging")
    def test_fresh_run_passes_none_to_telemetry(
        self,
        mock_setup_logging,
        mock_state_module,
        mock_graph,
        mock_init_telemetry,
        mock_end_loop,
    ):
        """On a fresh run (no issue_number), main() passes None to init_telemetry."""
        fresh_state = {
            "issue_number": None,
            "branch": None,
            "status": "idle",
        }
        mock_state_module.load.return_value = fresh_state
        mock_graph.invoke.return_value = fresh_state

        main()

        mock_init_telemetry.assert_called_once_with(
            reset_state=True,
            issue_number=None,
            branch=None,
        )


class TestMainMetricsOrchestration(unittest.TestCase):
    """Tests that __main__.main() resets the metrics collector before the graph
    runs and writes one record only when the cycle completes (status == "done").

    Regression coverage for the Run Observability wiring in __main__.py
    (ADR-0029): the cycle-level reset + conditional write path that the
    MetricsCollector unit tests do not exercise.
    """

    def test_reset_called_before_graph_invoke(self):
        """get_collector().reset() is called before graph.invoke()."""
        from unittest.mock import MagicMock

        collector = MagicMock()
        done_state = {
            "issue_number": 7,
            "branch": "feat/x",
            "status": "done",
            "model": "m",
        }

        # Record whether reset() has been called at the moment invoke runs.
        invoke_seen_reset = {"called": False}

        def invoke_side_effect(state):
            invoke_seen_reset["called"] = collector.reset.called
            return done_state

        with (
            patch("orchestrator.__main__.end_orchestrator_loop"),
            patch("orchestrator.__main__.init_telemetry"),
            patch("orchestrator.__main__.get_collector", return_value=collector),
            patch("orchestrator.__main__.graph") as mock_graph,
            patch("orchestrator.__main__.state_module") as mock_state_module,
            patch("orchestrator.__main__.setup_logging"),
        ):
            mock_state_module.load.return_value = done_state
            mock_graph.invoke.side_effect = invoke_side_effect
            main()

        self.assertTrue(
            invoke_seen_reset["called"],
            "collector.reset() must be called before graph.invoke() runs.",
        )

    @patch("orchestrator.__main__.end_orchestrator_loop")
    @patch("orchestrator.__main__.init_telemetry")
    @patch("orchestrator.__main__.get_collector")
    @patch("orchestrator.__main__.graph")
    @patch("orchestrator.__main__.state_module")
    @patch("orchestrator.__main__.setup_logging")
    def test_write_record_on_done(
        self,
        mock_setup_logging,
        mock_state_module,
        mock_graph,
        mock_get_collector,
        mock_init_telemetry,
        mock_end_loop,
    ):
        """A completed cycle (status == 'done') writes exactly one metrics record."""
        collector = mock_get_collector.return_value
        done_state = {
            "issue_number": 42,
            "branch": "feat/issue-42",
            "status": "done",
            "model": "deepseek/test",
        }
        mock_state_module.load.return_value = done_state
        mock_graph.invoke.return_value = done_state

        main()

        collector.reset.assert_called_once_with()
        collector.write_record.assert_called_once_with(
            issue_number=42, branch="feat/issue-42", model="deepseek/test"
        )

    @patch("orchestrator.__main__.end_orchestrator_loop")
    @patch("orchestrator.__main__.init_telemetry")
    @patch("orchestrator.__main__.get_collector")
    @patch("orchestrator.__main__.graph")
    @patch("orchestrator.__main__.state_module")
    @patch("orchestrator.__main__.setup_logging")
    def test_no_write_record_on_failed(
        self,
        mock_setup_logging,
        mock_state_module,
        mock_graph,
        mock_get_collector,
        mock_init_telemetry,
        mock_end_loop,
    ):
        """A failed cycle writes no record, but still resets the collector."""
        collector = mock_get_collector.return_value
        failed_state = {
            "issue_number": 42,
            "branch": "feat/issue-42",
            "status": "failed",
            "model": "m",
        }
        mock_state_module.load.return_value = failed_state
        mock_graph.invoke.return_value = failed_state

        # main() calls sys.exit(1) on a failed cycle.
        with self.assertRaises(SystemExit) as ctx:
            main()
        self.assertEqual(ctx.exception.code, 1)

        collector.reset.assert_called_once_with()
        collector.write_record.assert_not_called()

    @patch("orchestrator.__main__.end_orchestrator_loop")
    @patch("orchestrator.__main__.init_telemetry")
    @patch("orchestrator.__main__.get_collector")
    @patch("orchestrator.__main__.graph")
    @patch("orchestrator.__main__.state_module")
    @patch("orchestrator.__main__.setup_logging")
    def test_no_write_record_on_non_done_status(
        self,
        mock_setup_logging,
        mock_state_module,
        mock_graph,
        mock_get_collector,
        mock_init_telemetry,
        mock_end_loop,
    ):
        """No record is written for any non-'done' terminal status (e.g. 'idle')."""
        collector = mock_get_collector.return_value
        idle_state = {
            "issue_number": None,
            "branch": None,
            "status": "idle",
            "model": "",
        }
        mock_state_module.load.return_value = idle_state
        mock_graph.invoke.return_value = idle_state

        main()

        collector.reset.assert_called_once_with()
        collector.write_record.assert_not_called()

    @patch("orchestrator.__main__.end_orchestrator_loop")
    @patch("orchestrator.__main__.init_telemetry")
    @patch("orchestrator.__main__.get_collector")
    @patch("orchestrator.__main__.graph")
    @patch("orchestrator.__main__.state_module")
    @patch("orchestrator.__main__.setup_logging")
    def test_write_record_failure_is_swallowed(
        self,
        mock_setup_logging,
        mock_state_module,
        mock_graph,
        mock_get_collector,
        mock_init_telemetry,
        mock_end_loop,
    ):
        """A write_record exception must not propagate (graceful degradation)."""
        collector = mock_get_collector.return_value
        collector.write_record.side_effect = OSError("disk full")
        done_state = {
            "issue_number": 42,
            "branch": "feat/issue-42",
            "status": "done",
            "model": "m",
        }
        mock_state_module.load.return_value = done_state
        mock_graph.invoke.return_value = done_state

        # Must not raise.
        main()

        collector.write_record.assert_called_once()
        # The telemetry end loop still runs and the process exits cleanly (no sys.exit).
        mock_end_loop.assert_called_once()

    @patch("orchestrator.__main__.end_orchestrator_loop")
    @patch("orchestrator.__main__.init_telemetry")
    @patch("orchestrator.__main__.get_collector")
    @patch("orchestrator.__main__.graph")
    @patch("orchestrator.__main__.state_module")
    @patch("orchestrator.__main__.setup_logging")
    def test_graph_exception_skips_write_record(
        self,
        mock_setup_logging,
        mock_state_module,
        mock_graph,
        mock_get_collector,
        mock_init_telemetry,
        mock_end_loop,
    ):
        """If graph.invoke raises, no metrics record is written (crashed run)."""
        collector = mock_get_collector.return_value
        mock_state_module.load.return_value = {
            "issue_number": 42,
            "branch": "feat/issue-42",
            "status": "executing",
            "model": "m",
        }
        mock_graph.invoke.side_effect = RuntimeError("boom")

        # graph.invoke raising is caught by main(); exit_code becomes 1 and
        # main() calls sys.exit(1). No metrics record is written (crashed run).
        with self.assertRaises(SystemExit) as ctx:
            main()
        self.assertEqual(ctx.exception.code, 1)

        collector.reset.assert_called_once_with()
        collector.write_record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
