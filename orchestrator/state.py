import copy
import datetime
import json
import logging
import os
from pathlib import Path
from typing import Literal, TypedDict, cast

# Set up logging for state persistence warnings
logger = logging.getLogger("orchestrator.state")

StatusType = Literal[
    "idle",
    "claimed",
    "planning",
    "executing",
    "verifying",
    "pr_open",
    "merging",
    "done",
    "failed",
]


class AgentState(TypedDict):
    issue_number: int | None
    status: StatusType
    phase: str
    attempts: dict[str, int]
    branch: str | None
    model: str
    plan: str | None
    read_files: dict[str, list[list[int]]]
    updated_at: str
    feedback: str | None
    pushed_at: str | None
    verify_output: str | None
    # Transient root-cause message set by a node's exception handler so
    # recovery_node and a resumed run can surface the prior failure reason
    # without parsing a stack trace (ADR-0038). Overwritten each claim; not a
    # durable schema field beyond the optional defaults below.
    error: str | None
    # Set by the BinEval soft gate when it degraded to PASS on an
    # infrastructure failure (ADR-0037 #7): a correlated outage could otherwise
    # hide bad code behind a silent PASS. Surfaced in the PR body so a human
    # reviewer sees the PR was not semantically graded.
    bineval_degraded: bool | None


DEFAULT_STATE: AgentState = {
    "issue_number": None,
    "status": "idle",
    "phase": "",
    "attempts": {},
    "branch": None,
    "model": "",
    "plan": None,
    "read_files": {},
    "updated_at": "",
    "feedback": None,
    "pushed_at": None,
    "verify_output": None,
    "error": None,
    "bineval_degraded": None,
}

VALID_STATUSES = {
    "idle",
    "claimed",
    "planning",
    "executing",
    "verifying",
    "pr_open",
    "merging",
    "done",
    "failed",
}


def get_log_dir() -> Path:
    """Resolve and return the agent log directory, honoring the AGENT_LOG_PATH env var.

    Relative paths are resolved against the project root. The directory is created
    if missing. This is the single source of truth for the AGENT_LOG_PATH location
    shared by state persistence, telemetry, and worker trace sidecars (ADR-0016).
    """
    log_dir_name = os.getenv("AGENT_LOG_PATH", ".agent_logs")
    log_dir = Path(log_dir_name)

    # If the path is relative, resolve it relative to the project root
    if not log_dir.is_absolute():
        project_root = Path(__file__).resolve().parent.parent
        log_dir = project_root / log_dir_name

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return log_dir


def get_state_filepath() -> Path:
    """Resolve and return the path to the state.json file, honoring the AGENT_LOG_PATH env var."""
    return get_log_dir() / "state.json"


def save(state: AgentState, filepath: str | Path | None = None) -> None:
    """Save the agent state atomically to the specified filepath or default state.json path.

    Creates the parent directory if it does not exist. Updates the 'updated_at' timestamp.
    """
    target_path = Path(filepath) if filepath else get_state_filepath()

    # Ensure parent directory exists
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Update updated_at field with current ISO-8601 UTC timestamp
    state["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat()

    # Write to a temporary file in the same directory to guarantee atomic replace
    temp_path = target_path.with_suffix(".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
            # Power-loss durability (ADR-0037 #6): flush the Python buffer and
            # fsync the file descriptor so the bytes reach stable storage before
            # the atomic replace. Without this, a crash between write and
            # os.replace could leave a torn state.json that breaks Stateful Resume.
            f.flush()
            os.fsync(f.fileno())

        # Atomically replace target file with the temp file
        os.replace(temp_path, target_path)
    except Exception as e:
        # Clean up temp file if it was created
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise e


def load(filepath: str | Path | None = None) -> AgentState:
    """Load the agent state from the specified filepath or default state.json path.

    If the file does not exist, or the JSON content is corrupt or invalid, logs a warning
    and falls back to a clean copy of the default state.
    """
    target_path = Path(filepath) if filepath else get_state_filepath()

    if not target_path.exists():
        return copy.deepcopy(DEFAULT_STATE)

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                raise ValueError("State file is empty")
            data = json.loads(content)

        if not isinstance(data, dict):
            raise ValueError("State JSON root is not a dictionary")

        # Validate that all required keys exist
        required_keys = {
            "issue_number",
            "status",
            "phase",
            "attempts",
            "branch",
            "model",
            "plan",
            "read_files",
            "updated_at",
        }
        missing_keys = required_keys - data.keys()
        if missing_keys:
            raise ValueError(f"State is missing required keys: {missing_keys}")

        # Ensure optional/new keys have defaults for backward compatibility
        if "feedback" not in data:
            data["feedback"] = None
        if "pushed_at" not in data:
            data["pushed_at"] = None
        if "verify_output" not in data:
            data["verify_output"] = None
        if "error" not in data:
            data["error"] = None
        if "bineval_degraded" not in data:
            data["bineval_degraded"] = None

        # Validate status value
        if data["status"] not in VALID_STATUSES:
            raise ValueError(f"Invalid status value: {data['status']}")

        # Validate attempts type
        if not isinstance(data["attempts"], dict):
            raise ValueError("Field 'attempts' must be a dictionary")

        # Validate read_files type and migrate legacy path-list format to the
        # range-scoped dict format (path -> list of [start, end] inclusive ranges).
        # Legacy list entries authorized the whole file; since read_files is reset
        # on phase start and on stateful resume (ADR-0006 / ADR-0033), an in-flight
        # legacy state is stale anyway — migrate to an empty dict (forces fresh reads).
        if isinstance(data["read_files"], list):
            data["read_files"] = {}
        if not isinstance(data["read_files"], dict):
            raise ValueError("Field 'read_files' must be a dictionary")

        # Return successfully parsed state
        return cast(AgentState, data)

    except Exception as e:
        logger.warning(
            "Failed to load agent state from %s due to: %s. Falling back to default state.",
            target_path,
            e,
        )
        return copy.deepcopy(DEFAULT_STATE)
