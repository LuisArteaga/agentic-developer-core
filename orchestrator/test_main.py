import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.__main__ import main


class TestMainTelemetryWiring(unittest.TestCase):
    """Tests that __main__.main() correctly wires issue_number and branch to init_telemetry.

    Regression test for issue #34: the call site in main() must pass the loaded
    state's issue_number and branch to init_telemetry so the Langfuse session ID
    is constructed correctly on both fresh runs and stateful resumes.
    """

    def setUp(self):
        # Issue #107: main() now calls _load_env_file(). Patch it so the real
        # project .env does not leak into os.environ and pollute other tests.
        patcher = patch("orchestrator.__main__._load_env_file", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def setUp(self):
        # Issue #107: main() now calls _load_env_file(). Patch it so the real
        # project .env does not leak into os.environ and pollute other tests.
        patcher = patch("orchestrator.__main__._load_env_file", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

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


class TestMainDotenvLoading(unittest.TestCase):
    """Issue #107: main() must load .env at startup so target-repo env vars
    (GITHUB_REPOSITORY, GITHUB_WORKSPACE, ...) take effect without requiring the
    calling shell to export them manually. Uses a stdlib loader (ADR-0017)."""

    @patch("orchestrator.__main__.end_orchestrator_loop")
    @patch("orchestrator.__main__.init_telemetry")
    @patch("orchestrator.__main__.graph")
    @patch("orchestrator.__main__.state_module")
    @patch("orchestrator.__main__.setup_logging")
    @patch("orchestrator.__main__._load_env_file", return_value=True)
    def test_load_env_called_before_graph_invoke(
        self,
        mock_load_env,
        mock_setup_logging,
        mock_state_module,
        mock_graph,
        mock_init_telemetry,
        mock_end_loop,
    ):
        """main() invokes _load_env_file() exactly once, before the graph runs."""
        call_order = []

        def record_load(*args, **kwargs):
            call_order.append("load_env")
            return True

        def record_invoke(*args, **kwargs):
            call_order.append("graph.invoke")
            return state

        mock_load_env.side_effect = record_load
        mock_graph.invoke.side_effect = record_invoke
        state = {"issue_number": None, "branch": None, "status": "idle"}
        mock_state_module.load.return_value = state

        main()

        mock_load_env.assert_called_once_with()
        self.assertEqual(call_order, ["load_env", "graph.invoke"])


class TestLoadEnvFile(unittest.TestCase):
    """Direct unit tests for the stdlib _load_env_file loader (issue #107).

    Covers override=False semantics, comment/blank-line handling, quote
    stripping, the `export` directive, and directory walk-up. Replaces the
    python-dotenv dependency per ADR-0017's minimal-dependency philosophy.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmpdir.name).resolve()
        # Snapshot keys we may touch so tearDown can restore them.
        self._snap = {}
        for k in ("FOO", "BAR", "BAZ", "QUOTED", "EXPORTED"):
            self._snap[k] = os.environ.get(k)
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmpdir.cleanup()

    def test_loads_key_value_pairs(self):
        """Basic KEY=VALUE lines are loaded into os.environ."""
        (self.dir / ".env").write_text("FOO=1\nBAR=hello\n", encoding="utf-8")
        from orchestrator.__main__ import _load_env_file

        result = _load_env_file(self.dir)
        self.assertTrue(result)
        self.assertEqual(os.environ["FOO"], "1")
        self.assertEqual(os.environ["BAR"], "hello")

    def test_does_not_override_existing(self):
        """override=False: an already-set env var is not overwritten."""
        os.environ["FOO"] = "from-shell"
        (self.dir / ".env").write_text("FOO=from-file\n", encoding="utf-8")
        from orchestrator.__main__ import _load_env_file

        _load_env_file(self.dir)
        self.assertEqual(os.environ["FOO"], "from-shell")

    def test_skips_comments_and_blanks(self):
        """Comment lines and blank lines are ignored."""
        (self.dir / ".env").write_text(
            "# a comment\n\nFOO=1\n   # indented comment\nBAR=2\n",
            encoding="utf-8",
        )
        from orchestrator.__main__ import _load_env_file

        _load_env_file(self.dir)
        self.assertEqual(os.environ["FOO"], "1")
        self.assertEqual(os.environ["BAR"], "2")

    def test_strips_surrounding_quotes(self):
        """Matching surrounding single/double quotes are stripped."""
        (self.dir / ".env").write_text(
            "QUOTED=\"double value\"\nBAZ='single value'\n", encoding="utf-8"
        )
        from orchestrator.__main__ import _load_env_file

        _load_env_file(self.dir)
        self.assertEqual(os.environ["QUOTED"], "double value")
        self.assertEqual(os.environ["BAZ"], "single value")

    def test_handles_export_directive(self):
        """A leading `export ` directive is stripped."""
        (self.dir / ".env").write_text("export EXPORTED=yes\n", encoding="utf-8")
        from orchestrator.__main__ import _load_env_file

        _load_env_file(self.dir)
        self.assertEqual(os.environ["EXPORTED"], "yes")

    def test_returns_false_when_no_env_file(self):
        """Returns False when no .env file is found anywhere up the tree."""
        from orchestrator.__main__ import _load_env_file

        # Use a fresh empty subdir with no .env anywhere up to / .
        sub = self.dir / "deep" / "nested"
        sub.mkdir(parents=True)
        # This may still find a .env higher up on the host; assert against a
        # path guaranteed empty by pointing at the tmpdir itself (no .env).
        (self.dir / ".env").unlink(missing_ok=True)
        self.assertFalse(_load_env_file(self.dir))

    def test_walks_up_to_find_env(self):
        """Finds .env in a parent directory when start_path is a subdirectory."""
        (self.dir / ".env").write_text("FOO=found\n", encoding="utf-8")
        sub = self.dir / "sub" / "inner"
        sub.mkdir(parents=True)
        from orchestrator.__main__ import _load_env_file

        result = _load_env_file(sub)
        self.assertTrue(result)
        self.assertEqual(os.environ["FOO"], "found")


if __name__ == "__main__":
    unittest.main()
