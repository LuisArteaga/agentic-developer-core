import base64
import datetime
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

from scripts.redaction import redact_secrets

# Try importing opentelemetry, fallback to dummy classes if not installed
try:
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor

    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False

# Literal keys from OpenInference Semantic Conventions
OPENINFERENCE_SPAN_KIND = "openinference.span.kind"
INPUT_VALUE = "input.value"
OUTPUT_VALUE = "output.value"
LLM_MODEL_NAME = "llm.model_name"
TOOL_NAME = "tool.name"
TOOL_PARAMETERS = "tool.parameters"

_DOCKER_SENTINEL = "/.dockerenv"
_DEFAULT_OTLP_ENDPOINT = "http://host.docker.internal:4318/v1/traces"

# Langfuse Cloud OTLP endpoint — full traces path (OTLPSpanExporter uses endpoint as-is,
# no path appending). See Langfuse OTLP docs:
# https://langfuse.com/integrations/native/opentelemetry
_LANGFUSE_DEFAULT_OTLP_ENDPOINT = "https://cloud.langfuse.com/api/public/otel/v1/traces"

# Module-level session ID for Langfuse trace grouping ({issue_number}_{branch}).
# Set during init_telemetry (resume) or start_orchestrator_loop (fresh claim).
_langfuse_session_id: str | None = None


def _is_docker() -> bool:
    """Return True when running inside a Docker container."""
    return os.path.exists(_DOCKER_SENTINEL)


def configure_otlp_endpoint() -> str:
    """Return the OTLP endpoint to use, defaulting to host.docker.internal when in Docker.

    If OTEL_EXPORTER_OTLP_ENDPOINT is already set in the environment, that value is
    returned unchanged. Otherwise, if /.dockerenv is present, the default
    http://host.docker.internal:4318/v1/traces endpoint is set and returned.
    Returns an empty string when neither condition applies.

    # AGENT_DECISION: default only when inside Docker (/.dockerenv) and env var is absent.
    # If a non-Docker deployment is needed, the caller should set
    # OTEL_EXPORTER_OTLP_ENDPOINT explicitly; no change to this function required.
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    if not endpoint and _is_docker():
        endpoint = _DEFAULT_OTLP_ENDPOINT
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = endpoint
    return endpoint


def _is_langfuse_configured() -> bool:
    """Return True only when both Langfuse API keys are set (non-empty).

    A single key without the other must NOT activate Langfuse export;
    the code falls back to generic OTLP instead.
    """
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY")) and bool(
        os.getenv("LANGFUSE_SECRET_KEY")
    )


def _attach_langfuse_processors(provider) -> None:
    """Attach the Langfuse session-level SpanProcessor(s) to *provider*.

    The BaggageSpanProcessor copies ``langfuse.session.id`` from OTel baggage
    to span attributes, enabling trace grouping in the Langfuse UI. It is
    attached whenever Langfuse is configured, on **both** the fresh-provider
    path and the already-set-provider path of ``init_telemetry``, so the
    processor set is independent of which ``init_telemetry`` call runs first
    (issue #75: the early-return branch previously skipped it, making
    ``test_langfuse_session_id_in_spans`` order-dependent).

    Re-attachment on an already-configured provider is idempotent in effect —
    the processor sets the same attribute to the same value — which is the
    accepted cost of not inspecting a provider's registered processors
    (the OTel SDK exposes no public way to enumerate them).
    """
    if _is_langfuse_configured():
        provider.add_span_processor(BaggageSpanProcessor())


def _build_langfuse_auth_header() -> dict[str, str]:
    """Build the Authorization header for Langfuse Cloud OTLP export.

    Langfuse uses Basic Auth with base64(public_key:secret_key).
    Also includes x-langfuse-ingestion-version: 4 for real-time ingestion
    in Langfuse v4 (without it, data is delayed up to 10 minutes).

    Reference: https://langfuse.com/integrations/native/opentelemetry
    """
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return {
        "Authorization": f"Basic {credentials}",
        "x-langfuse-ingestion-version": "4",
    }


# Dummy definitions for graceful failover when OTel is not installed
class DummySpan:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

    def set_attribute(self, key, value):
        pass

    def record_exception(self, exception):
        pass

    def set_status(self, status):
        pass


class DummyTracer:
    def start_as_current_span(self, name, *args, **kwargs):
        return DummySpan()


class DummyStatusCode:
    OK = 0
    ERROR = 1


class DummyStatus:
    def __init__(self, code, message=""):
        self.code = code
        self.message = message


class DummyTraceModule:
    StatusCode = DummyStatusCode

    def Status(self, code, message=""):
        return DummyStatus(code, message)


if not HAS_OTEL:
    # Export a dummy trace object that matches opentelemetry API used in review.py
    trace = DummyTraceModule()


def get_tracer():
    """Returns the central tracer instance, or a dummy tracer if OTel is missing."""
    if HAS_OTEL:
        return trace.get_tracer("agentic-developer-core-review")
    else:
        return DummyTracer()


def get_agent_logs_dir() -> str:
    """Resolve and return the path to the agent logs directory, ensuring it exists and is writable."""
    # First check if AGENT_LOG_PATH is configured in the environment
    agent_log_path = os.getenv("AGENT_LOG_PATH")
    if agent_log_path:
        try:
            os.makedirs(agent_log_path, exist_ok=True)
            if os.access(agent_log_path, os.W_OK):
                return str(Path(agent_log_path).resolve())
        except Exception:
            pass

    # Fallback to checking /workspace/.agent_logs (inside container)
    if os.path.exists("/workspace/.agent_logs") and os.access(
        "/workspace/.agent_logs", os.W_OK
    ):
        return "/workspace/.agent_logs"
    # Otherwise check local .agent_logs relative to current working dir or project root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    local_logs = os.path.join(project_root, ".agent_logs")
    if os.path.exists(local_logs) and os.access(local_logs, os.W_OK):
        return local_logs
    # Fallback to temp logs if nothing else works
    tmp_logs = "/tmp/agent_logs"
    try:
        os.makedirs(tmp_logs, mode=0o700, exist_ok=True)
        os.chmod(tmp_logs, 0o700)  # nosemgrep: insecure-file-permissions
    except Exception:
        pass
    return tmp_logs


if HAS_OTEL:

    class LocalJSONLFileSpanProcessor(SpanProcessor):
        """A custom OpenTelemetry SpanProcessor that serializes finished spans to local JSONL files."""

        def __init__(self):
            self._lock = threading.Lock()

        def on_start(self, span, parent_context=None):
            pass

        def on_end(self, span):
            try:
                status_code = (
                    span.status.status_code.value
                    if hasattr(span.status.status_code, "value")
                    else int(span.status.status_code)
                )
                status_description = span.status.description or ""

                span_dict = {
                    "trace_id": f"{span.context.trace_id:032x}",
                    "span_id": f"{span.context.span_id:016x}",
                    "parent_span_id": f"{span.parent.span_id:016x}"
                    if span.parent
                    else "",
                    "name": span.name,
                    "kind": span.kind.value
                    if hasattr(span.kind, "value")
                    else int(span.kind),
                    "start_time_unix_nano": span.start_time,
                    "end_time_unix_nano": span.end_time,
                    "attributes": dict(span.attributes or {}),
                    "status_code": status_code,
                    "status_message": status_description,
                    "resource_attributes": dict(span.resource.attributes or {}),
                    "scope_name": span.instrumentation_scope.name
                    if span.instrumentation_scope
                    else "unknown",
                }

                # Write to dated file
                today_str = datetime.date.today().isoformat()
                logs_dir = get_agent_logs_dir()
                log_file_path = os.path.join(logs_dir, f"otel_traces_{today_str}.jsonl")

                with self._lock:
                    with open(log_file_path, "a", encoding="utf-8") as f:
                        f.write(json.dumps(span_dict) + "\n")
                        f.flush()
            except Exception as e:
                sys.stderr.write(
                    f"[WARN] LocalJSONLFileSpanProcessor failed to write span: {e}\n"
                )

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    class BaggageSpanProcessor(SpanProcessor):
        """Copies langfuse.session.id from OTel Baggage to span attributes.

        Langfuse v4 recommends OTel Baggage + a BaggageSpanProcessor so that
        trace-level attributes (like session.id) appear on every span, enabling
        reliable filtering and session grouping in the Langfuse UI.

        Reference: https://langfuse.com/integrations/native/opentelemetry#propagating-attributes

        # AGENT_DECISION: implemented as a custom processor (~8 lines) instead of
        # installing the official opentelemetry-processor-baggage package, to
        # honor the Radical Simplicity constraint (ADR-0009: no new heavy deps).
        # Only the langfuse.session.id key is copied — opt-in, no sensitive data leakage.
        """

        def on_start(self, span, parent_context=None):
            from opentelemetry.baggage import get_all

            # OTel Python 1.37: baggage.get_value re-exports context.get_value,
            # which looks up the key directly in the context — NOT through the
            # _BAGGAGE_KEY dict. Use get_all to properly read baggage entries.
            all_baggage = get_all(context=parent_context)
            value = all_baggage.get("langfuse.session.id")
            if value is not None:
                # Defense-in-depth (ADR-0037 layer 4): scrub any secret shape
                # from string span values before they reach the OTLP exporter.
                span.set_attribute("langfuse.session.id", redact_secrets(str(value)))

        def on_end(self, span):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True


# Module-level stack for active orchestrator spans (loop + phases)
_span_stack: list[Any] = []


# Module-level state for in-process tracking
def _get_state_file_path() -> str:
    """Returns a secure, user-private path for the telemetry state file."""
    logs_dir = get_agent_logs_dir()
    return os.path.join(logs_dir, "telemetry_state.json")


_state: dict[str, Any] = {
    "loop_start_time": None,
    "loop_end_time": None,
    "loop_exit_code": None,
    "loop_issue_number": None,
    "phases": {},
    "security_blocks": [],
}


def _load_state():
    global _state
    state_file = _get_state_file_path()
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                loaded = json.load(f)
                for k, v in loaded.items():
                    if k == "phases":
                        _state["phases"].update(v)
                    else:
                        _state[k] = v
        except Exception:
            pass


def _save_state():
    try:
        state_file = _get_state_file_path()
        with open(state_file, "w") as f:
            json.dump(_state, f)
    except Exception:
        pass


def init_telemetry(
    in_memory_exporter=None, reset_state=True, issue_number=None, branch=None
):
    """
    Initializes OpenTelemetry and OpenInference tracer provider if installed.
    Runs silently as a no-op otherwise.

    issue_number and branch are used to construct the Langfuse session ID
    ({issue_number}_{branch}) for trace grouping. On a fresh run these are
    None (the issue hasn't been claimed yet); start_orchestrator_loop sets
    them later. On a stateful resume, they come from the loaded state.
    """
    global _state, _langfuse_session_id
    if reset_state:
        # Reset internal state
        _langfuse_session_id = None
        _state = {
            "loop_start_time": None,
            "loop_end_time": None,
            "loop_exit_code": None,
            "loop_issue_number": None,
            "phases": {},
            "security_blocks": [],
        }
        state_file = _get_state_file_path()
        if os.path.exists(state_file):
            try:
                os.remove(state_file)
            except Exception:
                pass

    if issue_number is not None and branch is not None:
        _langfuse_session_id = f"{issue_number}_{branch}"

    if not HAS_OTEL:
        sys.stderr.write(
            "[INFO] OpenTelemetry is not installed. Running in no-op tracing mode.\n"
        )
        return

    # If a real TracerProvider is already set (e.g., in repeated test setUps),
    # attach the new in-memory exporter to it instead of trying to replace it.
    # The global provider can only be set once per process (OTel's Once() guard),
    # so re-initialization augments the existing provider rather than replacing it.
    current_provider = trace.get_tracer_provider()
    if isinstance(current_provider, TracerProvider):
        # Attach session-level processors identically to the fresh-provider
        # path so outcomes are independent of call order (issue #75).
        _attach_langfuse_processors(current_provider)
        if in_memory_exporter is not None:
            current_provider.add_span_processor(SimpleSpanProcessor(in_memory_exporter))
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "my-agent-service")
    project_name = os.getenv("SMITHDB_PROJECT_NAME", "default")

    resource = Resource.create(
        {
            "service.name": service_name,
            "openinference.project.name": project_name,
        }
    )

    provider = TracerProvider(resource=resource)

    # Register our crash-resistant local logging SpanProcessor
    provider.add_span_processor(LocalJSONLFileSpanProcessor())

    # Attach Langfuse session-level processors identically to the already-set
    # provider path so outcomes are independent of call order (issue #75).
    _attach_langfuse_processors(provider)

    if in_memory_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(in_memory_exporter))
    else:
        if _is_langfuse_configured():
            # Langfuse Cloud export: explicit endpoint takes precedence over the
            # Langfuse default; auth headers always use the Langfuse Basic credentials.
            # AGENT_DECISION: do NOT call configure_otlp_endpoint() here — it would
            # set the Docker default into the env var, masking the Langfuse default.
            endpoint = (
                os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
                or _LANGFUSE_DEFAULT_OTLP_ENDPOINT
            )
            headers = _build_langfuse_auth_header()
        else:
            # Generic OTLP export (Docker default or explicit endpoint)
            configure_otlp_endpoint()
            endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
            headers = {}
            api_key = os.getenv("SMITHDB_API_KEY", "")
            if api_key:
                headers["x-api-key"] = api_key

            extra_headers_str = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
            if extra_headers_str:
                for item in extra_headers_str.split(","):
                    if "=" in item:
                        k, v = item.split("=", 1)
                        headers[k.strip()] = v.strip()

        if endpoint:
            try:
                exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
                # ADR-0016 amendment (2026-08): SimpleSpanProcessor exports each
                # ended span synchronously to the OTLP endpoint, instead of
                # BatchSpanProcessor's deferred batch flush. No force_flush() is
                # called anywhere in the loop, so a batched processor could lose
                # spans when the process exits shortly after _export_recorded_spans;
                # the synchronous processor guarantees every span reaches the
                # collector before end_orchestrator_loop returns. Note: because
                # spans are still created/ended retrospectively in
                # _export_recorded_spans, this swap gives immediate end-of-run
                # export (and exit safety), NOT live in-run visibility — see the
                # ADR-0016 amendment for why in-run streaming is a separate decision.
                provider.add_span_processor(SimpleSpanProcessor(exporter))
            except Exception as e:
                sys.stderr.write(f"[WARN] Failed to initialize OTLP exporter: {e}\n")

    trace.set_tracer_provider(provider)


def start_orchestrator_loop(issue_number=None, branch=None):
    """Start the parent orchestrator_loop span.

    issue_number and branch are used to set the Langfuse session ID
    ({issue_number}_{branch}) for trace grouping. On a fresh claim these
    are available immediately; init_telemetry may have received None on
    a fresh run, so this is the authoritative setter for new cycles.

    <!-- AGENT_DECISION: openinference.span.kind set to CHAIN because the loop is a
    higher-level container span, not an LLM call. A future spec could require
    a different kind without changing the public API. -->
    """
    global _state, _langfuse_session_id
    if issue_number is not None and branch is not None:
        _langfuse_session_id = f"{issue_number}_{branch}"
    # Wipe old state on new loop start
    _state = {
        "loop_start_time": None,
        "loop_end_time": None,
        "loop_exit_code": None,
        "loop_issue_number": None,
        "phases": {},
        "security_blocks": [],
    }
    state_file = _get_state_file_path()
    if os.path.exists(state_file):
        try:
            os.remove(state_file)
        except Exception:
            pass

    now = datetime.datetime.now(datetime.UTC).timestamp()
    _state["loop_start_time"] = now
    _state["loop_issue_number"] = issue_number
    _save_state()


def end_orchestrator_loop(exit_code=0):
    """End the active orchestrator_loop span, auto-ending any open phase spans."""
    _load_state()
    now = datetime.datetime.now(datetime.UTC).timestamp()
    _state["loop_end_time"] = now
    _state["loop_exit_code"] = exit_code
    _save_state()

    _export_recorded_spans()

    state_file = _get_state_file_path()
    if os.path.exists(state_file):
        try:
            os.remove(state_file)
        except Exception:
            pass


def start_orchestrator_phase(phase_name, parent=None):
    """Start a nested span for one of the orchestrator phases.

    phase_name must be one of "plan", "test_writing", "execute", "verify",
    "bineval". The optional `parent` names another phase whose span this one
    nests under (e.g. "bineval" nests under "verify" per ADR-0016). When parent
    is set, the export step links this span as a child of that parent phase span
    instead of the loop span.
    """
    if phase_name not in ("plan", "test_writing", "execute", "verify", "bineval"):
        return
    _load_state()
    now = datetime.datetime.now(datetime.UTC).timestamp()
    entry = {"start_time": now, "end_time": None, "exit_code": 0}
    if parent:
        entry["parent"] = parent
    _state["phases"][phase_name] = entry
    _save_state()
    return


def end_orchestrator_phase(
    exit_code=0,
    prompt_tokens=None,
    completion_tokens=None,
    model_name=None,
    phase_name=None,
):
    """End the active orchestrator phase span.

    Captures command exit status and OpenInference token/model attributes. When
    `phase_name` is given, ends that specific phase (required for nested phases
    where multiple phases are simultaneously open); otherwise ends the single
    currently-active phase (the one with end_time None) — preserving the
    pre-existing behavior for the non-nested callers.
    """
    _load_state()
    active_phase = phase_name
    if active_phase is None:
        for name, data in _state["phases"].items():
            if data.get("end_time") is None:
                active_phase = name

    if active_phase is None or active_phase not in _state["phases"]:
        return

    now = datetime.datetime.now(datetime.UTC).timestamp()
    _state["phases"][active_phase]["end_time"] = now
    _state["phases"][active_phase]["exit_code"] = exit_code
    if prompt_tokens is not None:
        _state["phases"][active_phase]["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        _state["phases"][active_phase]["completion_tokens"] = completion_tokens
    if model_name is not None:
        _state["phases"][active_phase]["model_name"] = model_name

    _save_state()


def record_security_block(
    layer: str, blocked_path: str, issue_number: int | None = None
):
    """Record a security-block event for retrospective span enrichment (ADR-0044).

    Appends a record to the telemetry ``_state`` so ``_export_recorded_spans`` can
    attach a ``security.block`` span event, set the span status to ERROR, and set
    the ``langfuse.trace.tags`` attribute (``["security-block"]``) on the matching
    phase span — making the breach searchable in Langfuse via ``tag:security-block``.

    This is the adapted form of the issue's proposed ``trace.get_current_span()``
    helper, which would be a **no-op** in this project: spans are created
    retrospectively in ``_export_recorded_spans`` (ADR-0016), not live via
    ``start_as_current_span``. There is no active recording span at the time the
    tools or nodes run, so the event must be buffered and attached during export
    (Framework-First Research, ADR-0042).

    ``layer`` is ``"plan"`` (Plan-Node pre-flight) or ``"runtime"`` (Worker Tool
    path-safety refusal). Safe to call when telemetry is not initialized — the
    record is simply buffered and ignored if no spans are exported.
    """
    _load_state()
    blocks = _state.setdefault("security_blocks", [])
    blocks.append(
        {
            "layer": layer,
            "blocked_path": blocked_path,
            "issue_number": issue_number or 0,
            "timestamp": datetime.datetime.now(datetime.UTC).timestamp(),
        }
    )
    _save_state()


def _export_recorded_spans():
    if not HAS_OTEL:
        return

    loop_start = _state.get("loop_start_time")
    loop_end = _state.get("loop_end_time")
    if not loop_start or not loop_end:
        return

    tracer = get_tracer()
    loop_start_nano = int(loop_start * 1e9)
    loop_end_nano = int(loop_end * 1e9)

    # Attach langfuse.session.id as OTel Baggage so the BaggageSpanProcessor
    # copies it to span attributes on every span (loop + phases).
    # Context is immutable — set_span_in_context preserves baggage entries.
    from opentelemetry import baggage as otel_baggage
    from opentelemetry.context import Context
    from opentelemetry.trace import set_span_in_context

    ctx = None
    if _langfuse_session_id:
        ctx = otel_baggage.set_baggage("langfuse.session.id", _langfuse_session_id)

    loop_span = tracer.start_span(
        "orchestrator_loop",
        start_time=loop_start_nano,
        context=ctx,
        attributes={OPENINFERENCE_SPAN_KIND: "CHAIN"},
    )
    if _state.get("loop_issue_number") is not None:
        loop_span.set_attribute("issue.number", _state["loop_issue_number"])

    loop_context = (
        set_span_in_context(loop_span, context=ctx)
        if ctx
        else set_span_in_context(loop_span)
    )

    # Maps phase_name -> OTel context carrying that phase's span, so a later
    # nested phase can link to its parent. Filled in insertion order.
    phase_contexts: dict[str, Context] = {}
    # Maps phase_name -> the Span object itself, so security-block events can
    # be attached to the correct phase span before it is ended (ADR-0044).
    phase_spans: dict[str, Any] = {}

    for phase_name, phase_data in _state.get("phases", {}).items():
        p_start = phase_data.get("start_time")
        p_end = (
            phase_data.get("end_time")
            or datetime.datetime.now(datetime.UTC).timestamp()
        )
        if not p_start:
            continue

        p_start_nano = int(p_start * 1e9)
        p_end_nano = int(p_end * 1e9)
        p_exit = phase_data.get("exit_code", 0)

        # Resolve parent context: a nested phase (e.g. "bineval" under
        # "verify", per ADR-0016) links to its parent phase span instead of
        # the loop span. Phases are stored in insertion order, so a parent
        # is always exported before its children.
        parent_name = phase_data.get("parent")
        parent_ctx = (
            phase_contexts[parent_name]
            if parent_name and parent_name in phase_contexts
            else loop_context
        )

        phase_span = tracer.start_span(
            f"orchestrator_phase_{phase_name}",
            start_time=p_start_nano,
            context=parent_ctx,
            attributes={
                OPENINFERENCE_SPAN_KIND: "CHAIN",
                "phase": phase_name,
                "command.exit_code": p_exit,
            },
        )

        if "prompt_tokens" in phase_data and phase_data["prompt_tokens"] is not None:
            phase_span.set_attribute(
                "llm.usage.prompt_tokens", phase_data["prompt_tokens"]
            )
        if (
            "completion_tokens" in phase_data
            and phase_data["completion_tokens"] is not None
        ):
            phase_span.set_attribute(
                "llm.usage.completion_tokens", phase_data["completion_tokens"]
            )
        if "model_name" in phase_data and phase_data["model_name"] is not None:
            # Defense-in-depth (ADR-0037 layer 4): scrub secret shapes from
            # string span attributes before export.
            phase_span.set_attribute(
                "llm.model_name", redact_secrets(str(phase_data["model_name"]))
            )

        status_code = trace.StatusCode.OK if p_exit == 0 else trace.StatusCode.ERROR
        phase_span.set_status(
            trace.Status(status_code, f"exit code {p_exit}" if p_exit != 0 else None)
        )

        # Deferred end: security-block events (ADR-0044) must be attached
        # before the span is ended, so ending is moved past the enrichment
        # step below.
        phase_contexts[phase_name] = set_span_in_context(phase_span)
        phase_spans[phase_name] = (phase_span, p_start, p_end, p_end_nano)

    # --- Security-block span enrichment (ADR-0044) ---
    # Attach a ``security.block`` span event + ERROR status + the
    # ``langfuse.trace.tags`` attribute to the phase span that was active when
    # the block occurred (matched by timestamp interval), falling back to the
    # loop span. The ``langfuse.trace.tags`` attribute is the filterable signal
    # in Langfuse (tag:security-block) — per the Langfuse OTel attribute mapping
    # (T1: langfuse.com/integrations/native/opentelemetry), ``langfuse.trace.tags``
    # maps to the trace-level tags field; ``langfuse.tags`` (as proposed in the
    # issue) would land in the unfilterable metadata.attributes catch-all.
    security_blocks = _state.get("security_blocks", [])
    has_security_block = bool(security_blocks)

    for block in security_blocks:
        b_ts = block.get("timestamp")
        b_layer = block.get("layer", "unknown")
        b_path = block.get("blocked_path", "")
        b_issue = block.get("issue_number", 0)

        # Find the phase span whose [start, end] interval contains the block.
        target_span = loop_span
        for p_name, (p_span, p_start, p_end, _) in phase_spans.items():
            if p_start and p_start <= b_ts <= p_end:
                target_span = p_span
                break

        target_span.add_event(
            "security.block",
            attributes={
                "security.layer": b_layer,
                "security.blocked_path": b_path,
                "security.issue_number": b_issue,
                "security.severity": "high",
            },
        )
        # A security block is always an ERROR, regardless of the phase's own
        # exit code (a runtime refusal during an otherwise-successful execute
        # is still a security breach).
        target_span.set_status(
            trace.Status(trace.StatusCode.ERROR, f"security block: {b_path}")
        )
        target_span.set_attribute("langfuse.trace.tags", ["security-block"])

    # Tag the loop (root) span so the whole trace is filterable in Langfuse
    # via tag:security-block, regardless of which phase span the event landed on.
    if has_security_block:
        loop_span.set_attribute("langfuse.trace.tags", ["security-block"])

    # End all phase spans now that enrichment is complete.
    for p_name, (p_span, _, _, p_end_nano) in phase_spans.items():
        p_span.end(end_time=p_end_nano)

    exit_code = _state.get("loop_exit_code", 0)
    loop_span.set_attribute("command.exit_code", exit_code)
    status_code = trace.StatusCode.OK if exit_code == 0 else trace.StatusCode.ERROR
    loop_span.set_status(
        trace.Status(status_code, f"exit code {exit_code}" if exit_code != 0 else None)
    )
    loop_span.end(end_time=loop_end_nano)
