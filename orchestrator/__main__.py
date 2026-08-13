import logging
import os
import sys
from pathlib import Path

from orchestrator import state as state_module
from orchestrator.graph import graph
from orchestrator.metrics import get_collector
from scripts.telemetry import end_orchestrator_loop, init_telemetry


def _load_env_file(start_path: Path | None = None) -> bool:
    """Load a .env file into os.environ using override=False semantics.

    Walks up from ``start_path`` (default: CWD) looking for a ``.env`` file,
    mirroring python-dotenv's ``find_dotenv`` behavior. For each KEY=VALUE line
    the variable is set only if not already present in the environment, so
    already-exported shell variables take precedence — the same contract as
    ``load_dotenv(override=False)``.

    Implemented in the stdlib (ADR-0017 minimal-dependency philosophy) rather
    than adding python-dotenv as a direct dependency for startup infrastructure.

    Returns True if a .env file was found and loaded, False otherwise.
    """
    search_dir = (start_path or Path.cwd()).resolve()
    env_path: Path | None = None
    for parent in (search_dir, *search_dir.parents):
        candidate = parent / ".env"
        if candidate.is_file():
            env_path = candidate
            break
    if env_path is None:
        return False

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip an optional leading `export ` directive.
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Strip matching surrounding single or double quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
    return True


def setup_logging():
    """Sets up standard logging configuration for the orchestrator CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main():
    """CLI entry point for the orchestrator, loading state and executing the compiled LangGraph workflow."""
    setup_logging()
    logger = logging.getLogger("orchestrator.main")
    logger.info("Initializing Orchestrator...")

    # Load .env (issue #107) before anything reads target-repo environment
    # variables (state_module.load reads AGENT_LOG_PATH; nodes read
    # GITHUB_REPOSITORY/GITHUB_WORKSPACE). override=False semantics so
    # already-exported shell variables take precedence; .env only fills the
    # gaps. This prevents the orchestrator from silently falling back to
    # polling its own repository when target-repo env vars are not exported.
    # Implemented in the stdlib per ADR-0017 (minimal-dependency philosophy).
    if _load_env_file():
        logger.info(".env file loaded into environment.")
    else:
        logger.debug("No .env file found; relying on exported environment variables.")

    # Load state (handles resuming from state.json if present)
    state = state_module.load()
    logger.info(
        "Current state loaded. Status: '%s', Active Issue: %s",
        state.get("status"),
        state.get("issue_number"),
    )

    is_resume = state.get("issue_number") is not None and state.get("status") not in (
        "idle",
        "done",
        "failed",
    )

    try:
        init_telemetry(
            reset_state=not is_resume,
            issue_number=state.get("issue_number"),
            branch=state.get("branch"),
        )
    except Exception as e:
        logger.debug("Non-fatal telemetry initialization error: %s", e)

    exit_code = 0
    try:
        # Run Observability (ADR-0029): reset the in-process metric accumulator
        # at the start of each cycle so a resumed process does not carry stale
        # partial metrics from a previous (crashed) run.
        get_collector().reset()
        logger.info("Invoking LangGraph execution workflow...")
        final_state = graph.invoke(state)
        logger.info(
            "Workflow execution finished successfully. Final status: '%s'",
            final_state.get("status"),
        )
        if final_state.get("status") == "failed":
            exit_code = 1
        # Run Observability (ADR-0029): write one metrics record per completed
        # issue cycle. Only a fully completed (status == "done") cycle
        # contributes — crashed or failed runs produce no record, so the trend
        # data is never polluted by partial work. The write degrades gracefully.
        if final_state.get("status") == "done":
            try:
                get_collector().write_record(
                    issue_number=final_state.get("issue_number"),
                    branch=final_state.get("branch"),
                    model=final_state.get("model", ""),
                )
            except Exception as e:
                logger.debug("Non-fatal metrics write error: %s", e)
    except Exception as e:
        logger.exception("Orchestrator execution encountered a critical error: %s", e)
        exit_code = 1
    finally:
        try:
            end_orchestrator_loop(exit_code=exit_code)
        except Exception as e:
            logger.debug("Non-fatal telemetry end loop error: %s", e)

    if exit_code != 0:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
