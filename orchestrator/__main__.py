import logging
import sys

from orchestrator import state as state_module
from orchestrator.graph import graph
from orchestrator.metrics import get_collector
from scripts.telemetry import end_orchestrator_loop, init_telemetry


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
