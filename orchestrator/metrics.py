"""Run Observability: in-process metric collection for TL, PA, and token consumption.

This module provides the measurement layer for the autonomous developer loop.
Metrics are accumulated in a module-level, in-memory :class:`MetricsCollector`
singleton across a single issue cycle and written once to
``.agent_logs/metrics.jsonl`` when the cycle completes successfully.

Design posture (ADR-0029):

* **Fire-and-forget, no crash persistence.** The accumulator lives in memory for
  the duration of one process (one issue cycle). If the process crashes, the
  accumulator is lost and no record is written — crashed runs contribute no
  metrics, by design. This is a deliberate contrast with ADR-0016's
  crash-resilient disk-based telemetry: metrics are directional trends, not
  safety-critical state, and partial records would pollute the trend.
* **``state.json`` is not used for metric accumulation.** No metric fields are
  added to :class:`AgentState`; collection is entirely side-channel.
* **Graceful degradation.** Every public entry point catches its own errors
  and logs at debug level, mirroring ``_safe_telemetry``. Metric collection can
  never break execution (ADR-0016 telemetry posture).
"""

import datetime
import json
import logging
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, ToolMessage

from orchestrator.state import get_log_dir

logger = logging.getLogger("orchestrator.metrics")

# One record per completed issue cycle is appended to this file.
METRICS_FILENAME = "metrics.jsonl"


class TokenUsageCallbackHandler(BaseCallbackHandler):
    """Accumulate ``total_tokens`` from ``on_llm_end`` across one or more invokes.

    Reads ``AIMessage.usage_metadata`` directly from each generation's message,
    without gating on ``response_metadata["model_name"]``. The canonical
    :class:`langchain_core.callbacks.UsageMetadataCallbackHandler` keys its
    records by ``model_name`` and silently drops entries when that field is
    absent. For the OpenRouter + chat-completions stack used here
    (``ChatOpenAI`` with ``use_responses_api=False``), the message's
    ``response_metadata`` is **not** populated with ``model_name`` — that value
    only lives in ``ChatResult.llm_output`` — so the canonical handler would
    silently record zero. This handler avoids that trap by reading
    ``usage_metadata`` (which *is* populated on every AIMessage in the
    chat-completions path) and falling back to ``llm_output["token_usage"]``.

    Attach a single instance to every ``invoke()`` call whose tokens should be
    summed (e.g. both the initial and Plan-Detail-Request re-invokes share one
    instance so usage accumulates across them). Read ``total_tokens`` after.
    """

    def __init__(self) -> None:
        self.total_tokens: int = 0

    def on_llm_end(self, response, **kwargs: Any) -> None:
        """Sum total tokens from every AIMessage generation in the result."""
        try:
            total = 0
            generations = getattr(response, "generations", []) or []
            for generation_row in generations:
                for generation in generation_row:
                    message = getattr(generation, "message", None)
                    if isinstance(message, AIMessage):
                        usage = message.usage_metadata
                        if isinstance(usage, dict):
                            total += int(usage.get("total_tokens", 0) or 0)
            # Fallback to the raw token_usage in llm_output when the message
            # carried no usage_metadata (e.g. some OpenAI-compatible providers).
            # Type-checked so a malformed/missing llm_output never leaks garbage.
            if total == 0:
                llm_output = getattr(response, "llm_output", None)
                if isinstance(llm_output, dict):
                    token_usage = llm_output.get("token_usage")
                    if isinstance(token_usage, dict):
                        total_tokens = token_usage.get("total_tokens")
                        if isinstance(total_tokens, int):
                            total += total_tokens
                        else:
                            prompt = token_usage.get("prompt_tokens", 0)
                            completion = token_usage.get("completion_tokens", 0)
                            if isinstance(prompt, int) and isinstance(completion, int):
                                total += prompt + completion
            self.total_tokens += total
        except Exception as e:  # noqa: BLE001 - never break execution
            # nosemgrep: python-logger-credential-disclosure - logs an exception, not a secret; "Token" is a class name
            logger.debug("TokenUsageCallbackHandler error: %s", e)


def count_tool_invocations(messages: list) -> int:
    """Count Worker Tool invocations as the number of ``ToolMessage`` objects.

    Each tool call produces exactly one ``ToolMessage`` in the ReAct trajectory,
    so this equals the count of actual tool invocations (across all seven Worker
    Tools: ``read_file``, ``list_directory``, ``grep_search``, ``patch_file``,
    ``run_command``, ``web_search``, ``fetch_url``), including calls that
    returned an error result.
    """
    return sum(1 for m in messages if isinstance(m, ToolMessage))


def sum_message_tokens(messages: list) -> int:
    """Sum ``total_tokens`` across every ``AIMessage.usage_metadata`` in a ReAct result.

    Used for the ReAct agents (Execute-Node and Test-Writer-Node), where the
    full message list is available and every ``AIMessage`` carries its own
    ``usage_metadata`` from the provider response.
    """
    total = 0
    for m in messages:
        if isinstance(m, AIMessage):
            usage = getattr(m, "usage_metadata", None)
            if isinstance(usage, dict):
                total += int(usage.get("total_tokens", 0) or 0)
    return total


def _normalize_path(path: str) -> str:
    """Normalize a project-relative path for set comparison.

    Strips a leading ``./`` and converts backslashes to forward slashes. Does
    not lowercase — paths are case-sensitive on the target OS (Linux).
    """
    p = (path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def compute_plan_alignment(planned: list[str], modified: list[str]) -> float | None:
    """Compute Plan Alignment as ``|planned ∩ modified| / |modified|``.

    Returns ``None`` when no files were modified (the ratio is undefined and a
    directional trend over nulls is more honest than a fabricated 0.0 or 1.0).
    Inputs are path-normalized before the intersection.
    """
    planned_set = {_normalize_path(p) for p in planned if p}
    modified_set = {_normalize_path(m) for m in modified if m}
    if not modified_set:
        return None
    overlap = planned_set & modified_set
    return round(len(overlap) / len(modified_set), 4)


def extract_planned_files(plan_json: str | None) -> list[str]:
    """Extract the deduplicated set of planned files from a serialized DevelopmentPlan.

    ``DevelopmentPlan`` is stored in state as ``model_dump_json()``; its
    ``tasks[].target_files`` are the files the planner pointed the Worker at.
    ``requested_files`` (Plan-Detail-Request outline targets) are intentionally
    excluded — they are outline requests, not planned modifications.
    """
    if not plan_json:
        return []
    try:
        data = json.loads(plan_json)
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug("extract_planned_files failed to parse plan JSON: %s", e)
        return []
    if not isinstance(data, dict):
        return []
    files: list[str] = []
    seen: set[str] = set()
    for task in data.get("tasks", []) or []:
        if not isinstance(task, dict):
            continue
        for file_path in task.get("target_files", []) or []:
            if file_path and file_path not in seen:
                seen.add(file_path)
                files.append(file_path)
    return files


def extract_plan_rationale(plan_json: str | None) -> str:
    """Extract the ``rationale`` field from a serialized ``DevelopmentPlan``.

    ``DevelopmentPlan`` is stored in state as ``model_dump_json()``; its
    ``rationale`` is the high-level architectural reasoning produced by the
    Plan-Node. Used to populate the Summary section of the PR body without an
    additional LLM call (deterministic enrichment). Mirrors
    :func:`extract_planned_files` for error handling: a missing/invalid plan
    yields an empty string so the PR body degrades gracefully rather than
    raising — the PR must always be created.
    """
    if not plan_json:
        return ""
    try:
        data = json.loads(plan_json)
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug("extract_plan_rationale failed to parse plan JSON: %s", e)
        return ""
    if not isinstance(data, dict):
        return ""
    rationale = data.get("rationale")
    return rationale if isinstance(rationale, str) else ""


class MetricsCollector:
    """In-memory accumulator for one issue cycle's Run Observability metrics.

    A single module-level instance (``_collector``) is reused across nodes
    within one process. It is reset before each cycle and written once when the
    cycle completes. All mutating methods swallow their own errors so a metric
    failure never propagates into the orchestrator.
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """Clear all accumulated metrics (start of a fresh cycle)."""
        self.trajectory_lengths: list[int] = []
        self.tokens_planning: int = 0
        self.tokens_execution: int = 0
        self.plan_alignment: float | None = None
        self.files_planned: list[str] = []
        self.files_modified: list[str] = []

    def add_trajectory_length(self, length: int) -> None:
        """Record one Execute attempt's Trajectory Length (tool-call count)."""
        try:
            self.trajectory_lengths.append(int(length))
        except Exception as e:  # noqa: BLE001
            logger.debug("add_trajectory_length error: %s", e)

    def add_planning_tokens(self, tokens: int) -> None:
        """Accumulate Plan-Node tokens (structured-output LLM calls)."""
        try:
            self.tokens_planning += int(tokens)
        except Exception as e:  # noqa: BLE001
            # nosemgrep: python-logger-credential-disclosure - logs an exception, not a secret; "tokens" is a method name
            logger.debug("add_planning_tokens error: %s", e)

    def add_execution_tokens(self, tokens: int) -> None:
        """Accumulate execution tokens (Execute + Test-Writer ReAct agents)."""
        try:
            self.tokens_execution += int(tokens)
        except Exception as e:  # noqa: BLE001
            # nosemgrep: python-logger-credential-disclosure - logs an exception, not a secret; "tokens" is a method name
            logger.debug("add_execution_tokens error: %s", e)

    def set_plan_alignment(
        self,
        alignment: float | None,
        planned: list[str],
        modified: list[str],
    ) -> None:
        """Record Plan Alignment and the file sets it was computed from."""
        try:
            self.plan_alignment = alignment
            self.files_planned = sorted(planned)
            self.files_modified = sorted(modified)
        except Exception as e:  # noqa: BLE001
            logger.debug("set_plan_alignment error: %s", e)

    @property
    def trajectory_length_total(self) -> int:
        return sum(self.trajectory_lengths)

    @property
    def execute_attempts(self) -> int:
        return len(self.trajectory_lengths)

    @property
    def tokens_total(self) -> int:
        return self.tokens_planning + self.tokens_execution

    def to_record(
        self,
        issue_number: int | None,
        branch: str | None,
        model: str,
        status: str = "done",
    ) -> dict[str, Any]:
        """Build the JSONL record dict for this cycle."""
        return {
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "issue_number": issue_number,
            "branch": branch,
            "model": model,
            "status": status,
            "trajectory_lengths": list(self.trajectory_lengths),
            "trajectory_length_total": self.trajectory_length_total,
            "execute_attempts": self.execute_attempts,
            "plan_alignment": self.plan_alignment,
            "files_planned": list(self.files_planned),
            "files_modified": list(self.files_modified),
            "tokens_planning": self.tokens_planning,
            "tokens_execution": self.tokens_execution,
            "tokens_total": self.tokens_total,
        }

    def write_record(
        self,
        issue_number: int | None,
        branch: str | None,
        model: str,
        status: str = "done",
    ) -> None:
        """Append one metrics record to ``.agent_logs/metrics.jsonl``.

        Never raises: a write failure is logged at debug level so observability
        never impacts execution (ADR-0016 / ADR-0029).
        """
        try:
            record = self.to_record(issue_number, branch, model, status)
            path = get_log_dir() / METRICS_FILENAME
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            logger.info("Metrics record written to %s", path)
        except Exception as e:  # noqa: BLE001
            logger.debug("write_record error: %s", e)


# Module-level singleton: one accumulator per process (one issue cycle).
_collector = MetricsCollector()


def get_collector() -> MetricsCollector:
    """Return the process-wide :class:`MetricsCollector` singleton."""
    return _collector
