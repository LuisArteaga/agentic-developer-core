import copy
import datetime
import json
import logging
import os
from pathlib import Path
from typing import Dict, Literal, Optional, TypedDict, Union

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
    issue_number: Optional[int]
    status: StatusType
    phase: str
    attempts: Dict[str, int]
    branch: Optional[str]
    model: str
    plan: Optional[str]
    read_files: list[str]
    updated_at: str

DEFAULT_STATE: AgentState = {
    "issue_number": None,
    "status": "idle",
    "phase": "",
    "attempts": {},
    "branch": None,
    "model": "",
    "plan": None,
    "read_files": [],
    "updated_at": "",
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

def get_state_filepath() -> Path:
    """Resolve and return the path to the state.json file, honoring the AGENT_LOG_PATH env var."""
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
    return log_dir / "state.json"

def save(state: AgentState, filepath: Optional[Union[str, Path]] = None) -> None:
    """Save the agent state atomically to the specified filepath or default state.json path.

    Creates the parent directory if it does not exist. Updates the 'updated_at' timestamp.
    """
    target_path = Path(filepath) if filepath else get_state_filepath()

    # Ensure parent directory exists
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Update updated_at field with current ISO-8601 UTC timestamp
    state["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    # Write to a temporary file in the same directory to guarantee atomic replace
    temp_path = target_path.with_suffix(".tmp")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

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

def load(filepath: Optional[Union[str, Path]] = None) -> AgentState:
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
        required_keys = {"issue_number", "status", "phase", "attempts", "branch", "model", "plan", "read_files", "updated_at"}
        missing_keys = required_keys - data.keys()
        if missing_keys:
            raise ValueError(f"State is missing required keys: {missing_keys}")

        # Validate status value
        if data["status"] not in VALID_STATUSES:
            raise ValueError(f"Invalid status value: {data['status']}")

        # Validate attempts type
        if not isinstance(data["attempts"], dict):
            raise ValueError("Field 'attempts' must be a dictionary")

        # Validate read_files type
        if not isinstance(data["read_files"], list):
            raise ValueError("Field 'read_files' must be a list")

        # Return successfully parsed state
        return data

    except Exception as e:
        logger.warning(
            "Failed to load agent state from %s due to: %s. Falling back to default state.",
            target_path,
            e,
        )
        return copy.deepcopy(DEFAULT_STATE)
