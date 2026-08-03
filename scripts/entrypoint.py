#!/usr/bin/env python3
import json
import os
import pathlib
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request


def log(msg, level="INFO"):
    print(f"[{level}] {msg}", file=sys.stderr)


def get_state_filepath():
    """Locate the state.json file, matching the path resolution of the orchestrator state module."""
    log_dir_name = os.getenv("AGENT_LOG_PATH", ".agent_logs")
    log_dir = pathlib.Path(log_dir_name)
    if not log_dir.is_absolute():
        project_root = pathlib.Path(__file__).resolve().parent.parent
        log_dir = project_root / log_dir_name
    return log_dir / "state.json"


def read_agent_status():
    """Read the status field from state.json. Defaults to 'idle' if file doesn't exist or is invalid."""
    filepath = get_state_filepath()
    if not filepath.exists():
        return "idle"
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("status", "idle")
    except Exception as e:
        log(f"Failed to read agent status from {filepath}: {e}", "WARN")
        return "idle"


def validate_environment():
    """Fail-fast check for necessary environment variables."""
    required = ["OPENROUTER_API_KEY", "GH_PAT"]
    missing = [var for var in required if not os.getenv(var)]
    if missing:
        log(f"Missing required environment variable(s): {', '.join(missing)}", "ERROR")
        sys.exit(1)
    log("Environment validation passed.")


def check_url(url, timeout=5):
    """Check reachability of a given URL."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=timeout)  # nosemgrep  # fmt: skip
        return True
    except urllib.error.HTTPError:
        # Server is reachable but returned HTTP error code (e.g. 404, 401), which is fine for reachability
        return True
    except Exception as e:
        log(f"Connection check to {url} failed: {e}", "WARN")
        return False


def verify_reachability():
    """Ensure essential external services are reachable, retrying up to 3 times."""
    skip_reachability = os.getenv("SKIP_REACHABILITY", "0").lower() in (
        "1",
        "true",
        "yes",
    )
    if skip_reachability:
        log("Reachability checks skipped.")
        return

    targets = ["https://api.github.com", "https://openrouter.ai"]

    for url in targets:
        success = False
        for attempt in range(1, 4):
            log(f"Checking reachability of {url} (attempt {attempt}/3)...")
            if check_url(url):
                success = True
                break
            if attempt < 3:
                time.sleep(2)
        if not success:
            log(
                f"Failed to reach external service {url} after 3 attempts. Exiting.",
                "ERROR",
            )
            sys.exit(1)

    log("All reachability checks passed successfully.")


def run_iteration(
    consecutive_crashes, run_once, poll_interval, initial_delay=1.0, max_delay=60.0
):
    log("Spawning orchestrator process...")
    start_time = time.time()

    try:
        # sys.executable ensures we use the exact same python environment/venv
        process = subprocess.Popen([sys.executable, "-m", "orchestrator"])
        exit_code = process.wait()
    except Exception as e:
        log(f"Failed to spawn orchestrator: {e}", "ERROR")
        exit_code = -1

    duration = time.time() - start_time
    log(
        f"Orchestrator process exited with code {exit_code} after {duration:.2f} seconds."
    )

    if exit_code == 0:
        consecutive_crashes = 0
        status = read_agent_status()
        log(f"Orchestrator completed successfully. Agent status: '{status}'.")

        if run_once:
            log("RUN_ONCE is enabled. Exiting supervisor.")
            return consecutive_crashes, True, 0

        if status == "idle":
            log(
                f"Agent is idle. Sleeping for poll interval of {poll_interval}s before next run..."
            )
            time.sleep(poll_interval)
        else:
            log(
                "Agent successfully processed a task. Rerunning immediately for next task..."
            )
            time.sleep(1.0)  # brief pause to prevent tight CPU looping
    else:
        # Orchestrator crashed
        if duration > 30.0:
            log(
                "Process ran for more than 30s before crashing. Resetting backoff delay to initial value."
            )
            consecutive_crashes = 0

        consecutive_crashes += 1
        # Calculate exponential delay: 2^(crashes-1) * initial_delay
        delay = min(initial_delay * (2 ** (consecutive_crashes - 1)), max_delay)
        # Add up to 10% random jitter
        jitter = random.uniform(0, 0.1 * delay)
        total_delay = delay + jitter

        log(
            f"Orchestrator crash detected (consecutive: {consecutive_crashes}). Sleeping for {total_delay:.2f}s before retry...",
            "ERROR",
        )

        if run_once:
            log("RUN_ONCE is enabled. Exiting supervisor with code 1.")
            return consecutive_crashes, True, 1

        time.sleep(total_delay)

    return consecutive_crashes, False, None


def main():
    validate_environment()
    verify_reachability()

    initial_delay = 1.0
    max_delay = 60.0
    consecutive_crashes = 0

    run_once = os.getenv("RUN_ONCE", "0").lower() in ("1", "true", "yes")
    poll_interval = float(os.getenv("AGENT_POLL_INTERVAL", "10.0"))

    while True:
        consecutive_crashes, should_exit, exit_code = run_iteration(
            consecutive_crashes, run_once, poll_interval, initial_delay, max_delay
        )
        if should_exit:
            sys.exit(exit_code)


if __name__ == "__main__":
    main()
