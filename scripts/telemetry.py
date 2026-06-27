import os
import sys
import json
import datetime
import threading
from pathlib import Path

# Try importing opentelemetry, fallback to dummy classes if not installed
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider, SpanProcessor
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, BatchSpanProcessor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
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
    if os.path.exists("/workspace/.agent_logs") and os.access("/workspace/.agent_logs", os.W_OK):
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
        os.chmod(tmp_logs, 0o700)
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
                status_code = span.status.status_code.value if hasattr(span.status.status_code, "value") else int(span.status.status_code)
                status_description = span.status.description or ""
                
                span_dict = {
                    "trace_id": f"{span.context.trace_id:032x}",
                    "span_id": f"{span.context.span_id:016x}",
                    "parent_span_id": f"{span.parent.span_id:016x}" if span.parent else "",
                    "name": span.name,
                    "kind": span.kind.value if hasattr(span.kind, "value") else int(span.kind),
                    "start_time_unix_nano": span.start_time,
                    "end_time_unix_nano": span.end_time,
                    "attributes": dict(span.attributes or {}),
                    "status_code": status_code,
                    "status_message": status_description,
                    "resource_attributes": dict(span.resource.attributes or {}),
                    "scope_name": span.instrumentation_scope.name if span.instrumentation_scope else "unknown",
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
                sys.stderr.write(f"[WARN] LocalJSONLFileSpanProcessor failed to write span: {e}\n")

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True


# Module-level stack for active orchestrator spans (loop + phases)
_span_stack = []


# Module-level state for in-process tracking
def _get_state_file_path() -> str:
    """Returns a secure, user-private path for the telemetry state file."""
    logs_dir = get_agent_logs_dir()
    return os.path.join(logs_dir, "telemetry_state.json")

_state = {
    "loop_start_time": None,
    "loop_end_time": None,
    "loop_exit_code": None,
    "loop_issue_number": None,
    "phases": {}
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


def init_telemetry(in_memory_exporter=None, reset_state=True):
    """
    Initializes OpenTelemetry and OpenInference tracer provider if installed.
    Runs silently as a no-op otherwise.
    """
    global _state
    if reset_state:
        # Reset internal state
        _state = {
            "loop_start_time": None,
            "loop_end_time": None,
            "loop_exit_code": None,
            "loop_issue_number": None,
            "phases": {}
        }
        state_file = _get_state_file_path()
        if os.path.exists(state_file):
            try:
                os.remove(state_file)
            except Exception:
                pass

    if not HAS_OTEL:
        sys.stderr.write("[INFO] OpenTelemetry is not installed. Running in no-op tracing mode.\n")
        return

    # If a real TracerProvider is already set (e.g., in repeated test setUps),
    # attach the new in-memory exporter to it instead of trying to replace it.
    current_provider = trace.get_tracer_provider()
    if isinstance(current_provider, TracerProvider):
        if in_memory_exporter is not None:
            current_provider.add_span_processor(SimpleSpanProcessor(in_memory_exporter))
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "my-agent-service")
    project_name = os.getenv("SMITHDB_PROJECT_NAME", "default")
    
    resource = Resource.create({
        "service.name": service_name,
        "openinference.project.name": project_name,
    })
    
    provider = TracerProvider(resource=resource)
    
    # Register our crash-resistant local logging SpanProcessor
    provider.add_span_processor(LocalJSONLFileSpanProcessor())
    
    if in_memory_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(in_memory_exporter))
    else:
        # Read environment config for exporter
        configure_otlp_endpoint()
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if not endpoint:
            # If no OTLP endpoint is configured, run in no-op tracing mode
            return
        api_key = os.getenv("SMITHDB_API_KEY", "")
        
        headers = {}
        if api_key:
            headers["x-api-key"] = api_key
            
        extra_headers_str = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
        if extra_headers_str:
            for item in extra_headers_str.split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    headers[k.strip()] = v.strip()
                    
        try:
            exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers)
            provider.add_span_processor(BatchSpanProcessor(exporter))
        except Exception as e:
            sys.stderr.write(f"[WARN] Failed to initialize OTLP exporter: {e}\n")
            
    trace.set_tracer_provider(provider)


def start_orchestrator_loop(issue_number=None):
    """Start the parent orchestrator_loop span.

    <!-- AGENT_DECISION: openinference.span.kind set to CHAIN because the loop is a
    higher-level container span, not an LLM call. A future spec could require
    a different kind without changing the public API. -->
    """
    global _state
    # Wipe old state on new loop start
    _state = {
        "loop_start_time": None,
        "loop_end_time": None,
        "loop_exit_code": None,
        "loop_issue_number": None,
        "phases": {}
    }
    state_file = _get_state_file_path()
    if os.path.exists(state_file):
        try:
            os.remove(state_file)
        except Exception:
            pass

    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    _state["loop_start_time"] = now
    _state["loop_issue_number"] = issue_number
    _save_state()
    return None


def end_orchestrator_loop(exit_code=0):
    """End the active orchestrator_loop span, auto-ending any open phase spans."""
    _load_state()
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
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


def start_orchestrator_phase(phase_name):
    """Start a nested span for one of the orchestrator phases.

    phase_name must be one of "plan", "test_writing", "execute", "verify".
    """
    if phase_name not in ("plan", "test_writing", "execute", "verify"):
        return None
    _load_state()
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    _state["phases"][phase_name] = {
        "start_time": now,
        "end_time": None,
        "exit_code": 0
    }
    _save_state()
    return None


def end_orchestrator_phase(exit_code=0, prompt_tokens=None, completion_tokens=None, model_name=None):
    """End the active orchestrator phase span.

    Captures command exit status and OpenInference token/model attributes.
    """
    _load_state()
    active_phase = None
    for name, data in _state["phases"].items():
        if data.get("end_time") is None:
            active_phase = name
            
    if active_phase is None:
        return
        
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    _state["phases"][active_phase]["end_time"] = now
    _state["phases"][active_phase]["exit_code"] = exit_code
    if prompt_tokens is not None:
        _state["phases"][active_phase]["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        _state["phases"][active_phase]["completion_tokens"] = completion_tokens
    if model_name is not None:
        _state["phases"][active_phase]["model_name"] = model_name
        
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
    
    loop_span = tracer.start_span(
        "orchestrator_loop",
        start_time=loop_start_nano,
        attributes={
            OPENINFERENCE_SPAN_KIND: "CHAIN"
        }
    )
    if _state.get("loop_issue_number") is not None:
        loop_span.set_attribute("issue.number", _state["loop_issue_number"])
        
    from opentelemetry.trace import set_span_in_context
    loop_context = set_span_in_context(loop_span)
    
    for phase_name, phase_data in _state.get("phases", {}).items():
        p_start = phase_data.get("start_time")
        p_end = phase_data.get("end_time") or datetime.datetime.now(datetime.timezone.utc).timestamp()
        if not p_start:
            continue
            
        p_start_nano = int(p_start * 1e9)
        p_end_nano = int(p_end * 1e9)
        p_exit = phase_data.get("exit_code", 0)
        
        phase_span = tracer.start_span(
            f"orchestrator_phase_{phase_name}",
            start_time=p_start_nano,
            context=loop_context,
            attributes={
                OPENINFERENCE_SPAN_KIND: "CHAIN",
                "phase": phase_name,
                "command.exit_code": p_exit
            }
        )
        
        if "prompt_tokens" in phase_data and phase_data["prompt_tokens"] is not None:
            phase_span.set_attribute("llm.usage.prompt_tokens", phase_data["prompt_tokens"])
        if "completion_tokens" in phase_data and phase_data["completion_tokens"] is not None:
            phase_span.set_attribute("llm.usage.completion_tokens", phase_data["completion_tokens"])
        if "model_name" in phase_data and phase_data["model_name"] is not None:
            phase_span.set_attribute("llm.model_name", phase_data["model_name"])
            
        status_code = trace.StatusCode.OK if p_exit == 0 else trace.StatusCode.ERROR
        phase_span.set_status(trace.Status(status_code, f"exit code {p_exit}" if p_exit != 0 else None))
        phase_span.end(end_time=p_end_nano)
        
    exit_code = _state.get("loop_exit_code", 0)
    loop_span.set_attribute("command.exit_code", exit_code)
    status_code = trace.StatusCode.OK if exit_code == 0 else trace.StatusCode.ERROR
    loop_span.set_status(trace.Status(status_code, f"exit code {exit_code}" if exit_code != 0 else None))
    loop_span.end(end_time=loop_end_nano)
