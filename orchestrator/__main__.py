import logging
import sys
from orchestrator import state as state_module
from orchestrator.graph import graph
from scripts.telemetry import init_telemetry, end_orchestrator_loop

def setup_logging():
    """Sets up standard logging configuration for the orchestrator CLI."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )

def main():
    """CLI entry point for the orchestrator, loading state and executing the compiled LangGraph workflow."""
    setup_logging()
    logger = logging.getLogger("orchestrator.main")
    logger.info("Initializing Orchestrator...")
    
    # Load state (handles resuming from state.json if present)
    state = state_module.load()
    logger.info("Current state loaded. Status: '%s', Active Issue: %s", state.get("status"), state.get("issue_number"))
    
    is_resume = (
        state.get("issue_number") is not None
        and state.get("status") not in ("idle", "done", "failed")
    )
    
    try:
        init_telemetry(reset_state=not is_resume)
    except Exception as e:
        logger.debug("Non-fatal telemetry initialization error: %s", e)
        
    exit_code = 0
    try:
        logger.info("Invoking LangGraph execution workflow...")
        final_state = graph.invoke(state)
        logger.info("Workflow execution finished successfully. Final status: '%s'", final_state.get("status"))
        if final_state.get("status") == "failed":
            exit_code = 1
    except Exception as e:
        logger.exception("Orchestrator execution encountered a critical error: %s", e)
        exit_code = 1
        sys.exit(1)
    finally:
        try:
            end_orchestrator_loop(exit_code=exit_code)
        except Exception as e:
            logger.debug("Non-fatal telemetry end loop error: %s", e)
            
    if exit_code != 0:
        sys.exit(exit_code)

if __name__ == "__main__":
    main()

