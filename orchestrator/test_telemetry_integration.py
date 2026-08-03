import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.telemetry import (
    HAS_OTEL,
    end_orchestrator_loop,
    end_orchestrator_phase,
    get_agent_logs_dir,
    init_telemetry,
    start_orchestrator_loop,
    start_orchestrator_phase,
)


class TestTelemetryIntegration(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for each test
        self.test_dir = tempfile.TemporaryDirectory()
        self.test_dir_path = Path(self.test_dir.name).resolve()

        # Save original environment
        self.original_env = dict(os.environ)

        # Point AGENT_LOG_PATH to our temporary directory
        os.environ["AGENT_LOG_PATH"] = str(self.test_dir_path)

    def tearDown(self):
        # Restore environment
        os.environ.clear()
        os.environ.update(self.original_env)

        # Clean up temporary directory
        self.test_dir.cleanup()

    def test_get_agent_logs_dir_respects_env_var(self):
        """Verify that get_agent_logs_dir correctly resolves to AGENT_LOG_PATH."""
        resolved_path = get_agent_logs_dir()
        self.assertEqual(resolved_path, str(self.test_dir_path))

    def test_telemetry_loop_and_phase_lifecycle(self):
        """Verify the full lifecycle of start/end loop and phase calls, asserting correct state persistence."""
        # Initialize telemetry
        init_telemetry(reset_state=True)

        # Loop state file should not exist yet since loop hasn't started
        state_file = self.test_dir_path / "telemetry_state.json"
        self.assertFalse(state_file.exists())

        # Start orchestrator loop
        start_orchestrator_loop(issue_number=123)
        self.assertTrue(state_file.exists())

        # Load and verify loop start time was recorded
        with open(state_file, "r") as f:
            state_data = json.load(f)
        self.assertIsNotNone(state_data.get("loop_start_time"))
        self.assertEqual(state_data.get("loop_issue_number"), 123)
        self.assertEqual(state_data.get("phases"), {})

        # Test all phases including the new 'test_writing' phase
        phases = ["plan", "test_writing", "execute", "verify"]
        for phase in phases:
            # Start phase
            start_orchestrator_phase(phase)
            with open(state_file, "r") as f:
                state_data = json.load(f)
            self.assertIn(phase, state_data["phases"])
            self.assertIsNotNone(state_data["phases"][phase]["start_time"])
            self.assertIsNone(state_data["phases"][phase]["end_time"])

            # End phase
            end_orchestrator_phase(exit_code=0)
            with open(state_file, "r") as f:
                state_data = json.load(f)
            self.assertIsNotNone(state_data["phases"][phase]["end_time"])
            self.assertEqual(state_data["phases"][phase]["exit_code"], 0)

        # End loop
        end_orchestrator_loop(exit_code=0)

        # The loop end removes the temporary telemetry state file
        self.assertFalse(state_file.exists())

    def test_telemetry_with_in_memory_exporter(self):
        """If OpenTelemetry is installed, check that the retrospective spans are generated correctly."""
        if not HAS_OTEL:
            self.skipTest("OpenTelemetry is not installed in the current environment.")

        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()

        # Initialize telemetry with our custom in-memory exporter
        init_telemetry(in_memory_exporter=exporter, reset_state=True)

        # Run loop and phases
        start_orchestrator_loop(issue_number=456)
        start_orchestrator_phase("plan")
        end_orchestrator_phase(exit_code=0)

        start_orchestrator_phase("test_writing")
        end_orchestrator_phase(exit_code=1)

        end_orchestrator_loop(exit_code=1)

        # Retrieve finished spans
        spans = exporter.get_finished_spans()

        # We expect 3 spans: 1 parent (orchestrator_loop) and 2 children (orchestrator_phase_plan, orchestrator_phase_test_writing)
        self.assertEqual(len(spans), 3)

        # Sort spans by name
        spans_by_name = {span.name: span for span in spans}

        self.assertIn("orchestrator_loop", spans_by_name)
        self.assertIn("orchestrator_phase_plan", spans_by_name)
        self.assertIn("orchestrator_phase_test_writing", spans_by_name)

        # Verify attributes on loop span
        loop_span = spans_by_name["orchestrator_loop"]
        loop_attrs = loop_span.attributes or {}
        self.assertEqual(loop_attrs.get("issue.number"), 456)
        self.assertEqual(loop_attrs.get("command.exit_code"), 1)

        # Verify attributes on phase spans
        plan_span = spans_by_name["orchestrator_phase_plan"]
        plan_attrs = plan_span.attributes or {}
        self.assertEqual(plan_attrs.get("phase"), "plan")
        self.assertEqual(plan_attrs.get("command.exit_code"), 0)

        test_writing_span = spans_by_name["orchestrator_phase_test_writing"]
        test_attrs = test_writing_span.attributes or {}
        self.assertEqual(test_attrs.get("phase"), "test_writing")
        self.assertEqual(test_attrs.get("command.exit_code"), 1)


if __name__ == "__main__":
    unittest.main()
