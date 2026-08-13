import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.telemetry import (
    HAS_OTEL,
    _build_langfuse_auth_header,
    _is_langfuse_configured,
    _LANGFUSE_DEFAULT_OTLP_ENDPOINT,
    end_orchestrator_loop,
    end_orchestrator_phase,
    get_agent_logs_dir,
    init_telemetry,
    record_security_block,
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
        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-1234567890"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-1234567890"

        headers = _build_langfuse_auth_header()

        expected_credentials = base64.b64encode(
            b"pk-lf-1234567890:sk-lf-1234567890"
        ).decode()
        self.assertEqual(headers["Authorization"], f"Basic {expected_credentials}")
        self.assertEqual(headers["x-langfuse-ingestion-version"], "4")

    def test_langfuse_endpoint_precedence(self):
        """init_telemetry passes the correct endpoint and headers to OTLPSpanExporter.

        Exercises init_telemetry and inspects the OTLPSpanExporter constructor
        args — does NOT mirror the endpoint-selection expression.
        Verifies: Langfuse default when no explicit endpoint, explicit endpoint
        when set, and generic path without Langfuse auth when keys are absent.
        """
        if not HAS_OTEL:
            self.skipTest("OpenTelemetry is not installed in the current environment.")

        # Clean env of non-Langfuse auth vars that could pollute assertions
        os.environ.pop("SMITHDB_API_KEY", None)
        os.environ.pop("OTEL_EXPORTER_OTLP_HEADERS", None)

        # Case 1: Langfuse keys set, no explicit endpoint → Langfuse default
        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)

        with (
            patch("scripts.telemetry.OTLPSpanExporter") as mock_cls,
            patch("scripts.telemetry.trace.get_tracer_provider") as mock_get,
            patch("scripts.telemetry.trace.set_tracer_provider"),
        ):
            mock_get.return_value = MagicMock()
            init_telemetry(reset_state=True)
            mock_cls.assert_called_once()
            _, kwargs = mock_cls.call_args
            self.assertEqual(kwargs["endpoint"], _LANGFUSE_DEFAULT_OTLP_ENDPOINT)
            self.assertIn("Authorization", kwargs["headers"])

        # Case 2: Langfuse keys set, explicit endpoint → explicit wins
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = "https://custom.collector/v1/traces"

        with (
            patch("scripts.telemetry.OTLPSpanExporter") as mock_cls,
            patch("scripts.telemetry.trace.get_tracer_provider") as mock_get,
            patch("scripts.telemetry.trace.set_tracer_provider"),
        ):
            mock_get.return_value = MagicMock()
            init_telemetry(reset_state=True)
            mock_cls.assert_called_once()
            _, kwargs = mock_cls.call_args
            self.assertEqual(kwargs["endpoint"], "https://custom.collector/v1/traces")
            self.assertIn("Authorization", kwargs["headers"])

        # Case 3: No Langfuse keys → generic OTLP path, no Authorization header
        os.environ.pop("LANGFUSE_PUBLIC_KEY")
        os.environ.pop("LANGFUSE_SECRET_KEY")
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = (
            "https://generic.collector/v1/traces"
        )

        with (
            patch("scripts.telemetry.OTLPSpanExporter") as mock_cls,
            patch("scripts.telemetry.trace.get_tracer_provider") as mock_get,
            patch("scripts.telemetry.trace.set_tracer_provider"),
        ):
            mock_get.return_value = MagicMock()
            init_telemetry(reset_state=True)
            mock_cls.assert_called_once()
            _, kwargs = mock_cls.call_args
            self.assertEqual(kwargs["endpoint"], "https://generic.collector/v1/traces")
            self.assertNotIn("Authorization", kwargs.get("headers", {}))

    def test_langfuse_session_id_in_spans(self):
        """langfuse.session.id appears as a span attribute when Langfuse is configured.

        The BaggageSpanProcessor must copy the session ID from OTel baggage to
        span attributes, enabling trace grouping in the Langfuse UI.

        Only OTLPSpanExporter is mocked — the real TracerProvider (with the
        LocalJSONLFileSpanProcessor and BaggageSpanProcessor) is still installed
        so spans are written to local JSONL, where the session-id is asserted.
        Mocking the exporter prevents a real flush to Langfuse Cloud, which 401s
        on the test credentials and pollutes the test output.
        """
        if not HAS_OTEL:
            self.skipTest("OpenTelemetry is not installed in the current environment.")

        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)

        with patch("scripts.telemetry.OTLPSpanExporter") as mock_exporter_cls:
            mock_exporter_cls.return_value = MagicMock()
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

    def test_langfuse_session_id_attached_on_already_set_provider(self):
        """BaggageSpanProcessor attaches on the already-set-provider path (issue #75).

        Regression guard for the fix to ``init_telemetry``'s early-return branch.
        When a first ``init_telemetry(in_memory_exporter=...)`` call registers a
        fresh provider WITHOUT Langfuse configured (so no BaggageSpanProcessor),
        a subsequent ``init_telemetry()`` call with Langfuse keys set must still
        attach the BaggageSpanProcessor to the existing provider — the
        already-set-provider path — so ``langfuse.session.id`` propagates to
        spans regardless of call order. Before the fix, the early-return path
        skipped the BaggageSpanProcessor and the session id never landed on
        spans; this test failed in that state.

        Spans are asserted via the in-memory exporter attached in the first
        call (it remains on the shared provider), not via JSONL — so the
        assertion is independent of file I/O.
        """
        if not HAS_OTEL:
            self.skipTest("OpenTelemetry is not installed in the current environment.")

        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        # Step 1: register a fresh provider with an in-memory exporter but
        # WITHOUT Langfuse configured, so no BaggageSpanProcessor is attached
        # (the in_memory_exporter branch of the fresh path skips it).
        os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
        os.environ.pop("LANGFUSE_SECRET_KEY", None)
        os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
        exporter = InMemorySpanExporter()
        init_telemetry(in_memory_exporter=exporter, reset_state=True)

        # Step 2: now configure Langfuse and re-init. This takes the
        # already-set-provider early-return path. The fix attaches the
        # BaggageSpanProcessor here; the pre-fix code did not.
        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
        with patch("scripts.telemetry.OTLPSpanExporter"):
            init_telemetry(reset_state=True, issue_number=42, branch="feat/issue-42")

        start_orchestrator_loop(issue_number=42, branch="feat/issue-42")
        start_orchestrator_phase("plan")
        end_orchestrator_phase(exit_code=0)
        end_orchestrator_loop(exit_code=0)

        spans = exporter.get_finished_spans()
        self.assertTrue(spans, "Expected spans captured by the in-memory exporter")

        session_ids = {
            span.attributes.get("langfuse.session.id")
            for span in spans
            if span.attributes
        }
        self.assertIn(
            "42_feat/issue-42",
            session_ids,
            "BaggageSpanProcessor must attach on the already-set-provider path "
            "so langfuse.session.id propagates regardless of init_telemetry "
            "call order (issue #75)",
        )

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

    def test_otlp_uses_simple_span_processor(self):
        """OTLP export attaches SimpleSpanProcessor, not BatchSpanProcessor.

        Regression guard for issue #62 (ADR-0016 amendment): SimpleSpanProcessor
        exports each ended span synchronously, so every span reaches the OTLP
        collector before end_orchestrator_loop returns. No force_flush() is
        called in the loop, so a batched processor could lose spans on quick
        process exit; the synchronous processor eliminates that risk. If the
        OTLP exporter were ever re-wrapped in BatchSpanProcessor, this test
        fails because SimpleSpanProcessor would not be called at all.
        """
        if not HAS_OTEL:
            self.skipTest("OpenTelemetry is not installed in the current environment.")

        os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
        os.environ.pop("LANGFUSE_SECRET_KEY", None)
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = (
            "https://generic.collector/v1/traces"
        )

        with (
            patch("scripts.telemetry.OTLPSpanExporter") as mock_exporter_cls,
            patch("scripts.telemetry.SimpleSpanProcessor") as mock_simple,
            patch("scripts.telemetry.trace.get_tracer_provider") as mock_get,
            patch("scripts.telemetry.trace.set_tracer_provider"),
        ):
            mock_exporter_cls.return_value = MagicMock()
            mock_get.return_value = MagicMock()
            init_telemetry(reset_state=True)

            # The OTLP exporter is constructed and wrapped in SimpleSpanProcessor.
            mock_exporter_cls.assert_called_once()
            mock_simple.assert_called_once()

    def test_verify_bineval_nests_under_verify_phase(self):
        """BinEval phase span must be a child of the verify phase span (ADR-0016)."""
        if not HAS_OTEL:
            self.skipTest("OpenTelemetry is not installed in the current environment.")

        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        init_telemetry(in_memory_exporter=exporter, reset_state=True)

        start_orchestrator_loop(issue_number=101)
        start_orchestrator_phase("verify")
        # BinEval runs as a nested sub-phase of verify (parent="verify").
        start_orchestrator_phase("bineval", parent="verify")
        end_orchestrator_phase(exit_code=0, phase_name="bineval")
        end_orchestrator_phase(exit_code=0, phase_name="verify")
        end_orchestrator_loop(exit_code=0)

        spans = exporter.get_finished_spans()
        # 1 loop + 1 verify + 1 bineval
        self.assertEqual(len(spans), 3)
        spans_by_name = {span.name: span for span in spans}
        self.assertIn("orchestrator_loop", spans_by_name)
        self.assertIn("orchestrator_phase_verify", spans_by_name)
        self.assertIn("orchestrator_phase_bineval", spans_by_name)

        verify_span = spans_by_name["orchestrator_phase_verify"]
        bineval_span = spans_by_name["orchestrator_phase_bineval"]

        # The bineval span's parent must be the verify span, not the loop span.
        assert bineval_span.parent is not None
        assert verify_span.context is not None
        self.assertEqual(
            bineval_span.parent.span_id,
            verify_span.context.span_id,
            "bineval span must nest under the verify phase span",
        )
        # And the verify span's parent is the loop span (not bineval).
        loop_span = spans_by_name["orchestrator_loop"]
        assert verify_span.parent is not None
        assert loop_span.context is not None
        self.assertEqual(verify_span.parent.span_id, loop_span.context.span_id)

    def test_phase_model_name_secret_redacted_in_telemetry(self):
        """Secret-shaped model_name values are redacted before span export (ADR-0037 layer 4).

        Covers the redact_secrets() call on llm.model_name in
        _export_recorded_spans — a defense-in-depth scrub so a secret that
        somehow reaches the model_name telemetry field is not exported.
        """
        if not HAS_OTEL:
            self.skipTest("OpenTelemetry is not installed in the current environment.")

        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        init_telemetry(in_memory_exporter=exporter, reset_state=True)

        secret_model = "sk-or-v1-" + "x" * 30
        start_orchestrator_loop(issue_number=999)
        start_orchestrator_phase("execute")
        end_orchestrator_phase(exit_code=0, model_name=secret_model)
        end_orchestrator_loop(exit_code=0)

        spans = exporter.get_finished_spans()
        spans_by_name = {span.name: span for span in spans}
        execute_span = spans_by_name["orchestrator_phase_execute"]
        attrs = execute_span.attributes or {}

        # The raw secret must not appear; it must be replaced by the redaction marker.
        model_attr = str(attrs.get("llm.model_name", ""))
        self.assertNotIn(secret_model, model_attr)
        self.assertIn("[REDACTED:sk-or-v1]", model_attr)

    def test_record_security_block_persists_to_state(self):
        """record_security_block appends a record to _state['security_blocks'] (ADR-0044)."""
        init_telemetry(reset_state=True)
        start_orchestrator_loop(issue_number=77)

        record_security_block(layer="plan", blocked_path=".env", issue_number=77)
        record_security_block(
            layer="runtime", blocked_path=".git/config", issue_number=77
        )

        state_file = self.test_dir_path / "telemetry_state.json"
        with open(state_file, "r") as f:
            state_data = json.load(f)

        blocks = state_data.get("security_blocks", [])
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["layer"], "plan")
        self.assertEqual(blocks[0]["blocked_path"], ".env")
        self.assertEqual(blocks[0]["issue_number"], 77)
        self.assertEqual(blocks[1]["layer"], "runtime")
        self.assertEqual(blocks[1]["blocked_path"], ".git/config")

    def test_record_security_block_resets_on_new_loop(self):
        """security_blocks is wiped on start_orchestrator_loop (fresh cycle)."""
        init_telemetry(reset_state=True)
        start_orchestrator_loop(issue_number=1)
        record_security_block(layer="plan", blocked_path=".env", issue_number=1)

        # Start a new loop — should wipe prior security blocks
        start_orchestrator_loop(issue_number=2)
        state_file = self.test_dir_path / "telemetry_state.json"
        with open(state_file, "r") as f:
            state_data = json.load(f)
        self.assertEqual(state_data.get("security_blocks", []), [])

    def test_security_block_span_enrichment(self):
        """_export_recorded_spans attaches security.block event + langfuse.trace.tags
        + ERROR status on the matching phase span (ADR-0044).

        Verifies the three Langfuse-searchability signals:
        - tag:security-block (via langfuse.trace.tags attribute)
        - security.block span event
        - ERROR span status
        """
        if not HAS_OTEL:
            self.skipTest("OpenTelemetry is not installed in the current environment.")

        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        init_telemetry(in_memory_exporter=exporter, reset_state=True)

        start_orchestrator_loop(issue_number=100)
        start_orchestrator_phase("plan")
        # Record a security block during the plan phase
        record_security_block(layer="plan", blocked_path=".env", issue_number=100)
        end_orchestrator_phase(exit_code=1)
        end_orchestrator_loop(exit_code=42)

        spans = exporter.get_finished_spans()
        spans_by_name = {span.name: span for span in spans}

        # The plan phase span should carry the security.block event + tag + ERROR
        plan_span = spans_by_name["orchestrator_phase_plan"]
        plan_attrs = plan_span.attributes or {}
        self.assertEqual(plan_attrs.get("langfuse.trace.tags"), ("security-block",))

        # Span status should be ERROR
        from opentelemetry.trace import StatusCode

        self.assertEqual(plan_span.status.status_code, StatusCode.ERROR)

        # The loop span should also carry the tag (trace-level filterability)
        loop_span = spans_by_name["orchestrator_loop"]
        loop_attrs = loop_span.attributes or {}
        self.assertEqual(loop_attrs.get("langfuse.trace.tags"), ("security-block",))


if __name__ == "__main__":
    unittest.main()
