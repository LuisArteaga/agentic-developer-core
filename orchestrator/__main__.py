import logging
import os
import sys
from orchestrator import state as state_module
from orchestrator.graph import graph

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
    
    try:
        logger.info("Invoking LangGraph execution workflow...")
        final_state = graph.invoke(state)
        logger.info("Workflow execution finished successfully. Final status: '%s'", final_state.get("status"))
    except Exception as e:
        logger.exception("Orchestrator execution encountered a critical error: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
