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


if __name__ == "__main__":
    unittest.main()
