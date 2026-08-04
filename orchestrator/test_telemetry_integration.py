import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.telemetry import (
    HAS_OTEL,
    _build_langfuse_auth_header,
    _is_langfuse_configured,
    _LANGFUSE_DEFAULT_OTLP_ENDPOINT,
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

    def test_local_jsonl_traces_without_otlp_endpoint(self):
        """Local JSONL traces must be written even when no OTLP endpoint is set.

        Regression test for issue #33: the TracerProvider and
        LocalJSONLFileSpanProcessor must remain active when OTLP is absent,
        degrading to local-only tracing rather than full no-op.
        """
        if not HAS_OTEL:
            self.skipTest("OpenTelemetry is not installed in the current environment.")

        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)

        init_telemetry(reset_state=True)

        start_orchestrator_loop(issue_number=789)
        start_orchestrator_phase("plan")
        end_orchestrator_phase(exit_code=0)
        end_orchestrator_loop(exit_code=0)

        jsonl_files = list(self.test_dir_path.glob("otel_traces_*.jsonl"))
        self.assertTrue(jsonl_files, "Expected at least one otel_traces_*.jsonl file")

        span_names = set()
        for f in jsonl_files:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    span = json.loads(line)
                    span_names.add(span.get("name"))

        self.assertIn("orchestrator_loop", span_names)
        self.assertIn("orchestrator_phase_plan", span_names)

    def test_langfuse_configured_detection(self):
        """_is_langfuse_configured returns True only when BOTH keys are set."""
        # Both keys set → True
        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
        self.assertTrue(_is_langfuse_configured())

        # Only public key → False
        os.environ.pop("LANGFUSE_SECRET_KEY")
        self.assertFalse(_is_langfuse_configured())

        # Only secret key → False
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
        os.environ.pop("LANGFUSE_PUBLIC_KEY")
        self.assertFalse(_is_langfuse_configured())

        # Neither → False
        os.environ.pop("LANGFUSE_SECRET_KEY")
        self.assertFalse(_is_langfuse_configured())

    def test_langfuse_auth_header_format(self):
        """_build_langfuse_auth_header produces correct Basic auth + ingestion version."""
        import base64

        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-1234567890"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-1234567890"

        headers = _build_langfuse_auth_header()

        expected_credentials = base64.b64encode(
            b"pk-lf-1234567890:sk-lf-1234567890"
        ).decode()
        self.assertEqual(headers["Authorization"], f"Basic {expected_credentials}")
        self.assertEqual(headers["x-langfuse-ingestion-version"], "4")

    def test_langfuse_endpoint_precedence(self):
        """Explicit OTEL_EXPORTER_OTLP_ENDPOINT takes precedence over Langfuse default.

        Verifies the endpoint selection logic: when Langfuse keys are set and no
        explicit endpoint is configured, the Langfuse default is used. When an
        explicit endpoint is set, it overrides the Langfuse default.
        """
        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"

        # No explicit endpoint → Langfuse default
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        endpoint = (
            os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
            or _LANGFUSE_DEFAULT_OTLP_ENDPOINT
        )
        self.assertEqual(endpoint, _LANGFUSE_DEFAULT_OTLP_ENDPOINT)

        # Explicit endpoint → takes precedence
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://custom.collector/v1/traces"
        endpoint = (
            os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
            or _LANGFUSE_DEFAULT_OTLP_ENDPOINT
        )
        self.assertEqual(endpoint, "https://custom.collector/v1/traces")

    def test_langfuse_session_id_in_spans(self):
        """langfuse.session.id appears as a span attribute when Langfuse is configured.

        The BaggageSpanProcessor must copy the session ID from OTel baggage to
        span attributes, enabling trace grouping in the Langfuse UI.
        """
        if not HAS_OTEL:
            self.skipTest("OpenTelemetry is not installed in the current environment.")

        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)

        init_telemetry(reset_state=True, issue_number=42, branch="feat/issue-42")

        start_orchestrator_loop(issue_number=42, branch="feat/issue-42")
        start_orchestrator_phase("plan")
        end_orchestrator_phase(exit_code=0)
        end_orchestrator_loop(exit_code=0)

        jsonl_files = list(self.test_dir_path.glob("otel_traces_*.jsonl"))
        self.assertTrue(jsonl_files, "Expected at least one otel_traces_*.jsonl file")

        session_ids = set()
        for f in jsonl_files:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    span = json.loads(line)
                    attrs = span.get("attributes", {})
                    if "langfuse.session.id" in attrs:
                        session_ids.add(attrs["langfuse.session.id"])

        self.assertIn("42_feat/issue-42", session_ids)

    def test_no_langfuse_keys_unchanged_behavior(self):
        """Without Langfuse keys, behavior is unchanged (generic OTLP or local-only).

        Regression test: no langfuse.session.id attribute should appear on spans
        when Langfuse keys are absent.
        """
        if not HAS_OTEL:
            self.skipTest("OpenTelemetry is not installed in the current environment.")

        os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
        os.environ.pop("LANGFUSE_SECRET_KEY", None)
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)

        init_telemetry(reset_state=True)

        start_orchestrator_loop(issue_number=99)
        start_orchestrator_phase("plan")
        end_orchestrator_phase(exit_code=0)
        end_orchestrator_loop(exit_code=0)

        jsonl_files = list(self.test_dir_path.glob("otel_traces_*.jsonl"))
        self.assertTrue(jsonl_files, "Expected at least one otel_traces_*.jsonl file")

        for f in jsonl_files:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    span = json.loads(line)
                    attrs = span.get("attributes", {})
                    self.assertNotIn(
                        "langfuse.session.id",
                        attrs,
                        "langfuse.session.id must not appear without Langfuse keys",
                    )


if __name__ == "__main__":
    unittest.main()
