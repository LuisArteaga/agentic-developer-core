#!/usr/bin/env python3
import collections
import json
import os
import pathlib
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request


# ADR-0044: distinct exit code for security-block failures. Mirrored from
# orchestrator/constants.py (the supervisor is a zero-dependency process that
# does not import the orchestrator package, ADR-0015). The orchestrator exits
# with this code when a path-safety block is detected, so the supervisor can
# branch it into the circuit-breaker path instead of exponential backoff.
SECURITY_BLOCK_EXIT_CODE = 42


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


class SecurityBlockTracker:
    """Rolling-window circuit breaker for security-block exits (ADR-0044).

    Tracks security-block exit codes (42) over a configurable rolling time
    window using a ``collections.deque`` of timestamps — the standard sliding-
    window pattern (deque ``append`` + ``popleft`` eviction, O(1) amortized,
    thread-safe in CPython). If the count of security blocks within the window
    reaches the threshold, the circuit breaker trips and the supervisor halts,
    preventing an active prompt-injection attack from churning across multiple
    quarantined issues.

    In-memory only: a supervisor restart resets the window. This is acceptable
    because a restart is itself a reset event, and persisting the counter would
    require filesystem state (violating the supervisor's zero-dependency,
    stateless design).
    """

    def __init__(self, threshold=None, window=None):
        self.threshold = threshold or int(
            os.getenv("AGENT_SECURITY_BLOCK_THRESHOLD", "5")
        )
        self.window = window or float(os.getenv("AGENT_SECURITY_BLOCK_WINDOW", "3600"))
        self._timestamps: collections.deque[float] = collections.deque()

    def _evict(self, now):
        """Remove timestamps older than the rolling window."""
        cutoff = now - self.window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def record_block(self, now=None):
        """Record a security-block exit at the given (or current) time."""
        now = now if now is not None else time.time()
        self._timestamps.append(now)
        self._evict(now)

    def count(self, now=None):
        """Return the number of security blocks within the current window."""
        now = now if now is not None else time.time()
        self._evict(now)
        return len(self._timestamps)

    def is_tripped(self, now=None):
        """Return True if the security-block count has reached the threshold."""
        return self.count(now) >= self.threshold


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


def preflight_sandbox_runtime():
    """Verify sandbox runtime and credential scopes before the first cycle.

    Runs ``python -m orchestrator.preflight`` as a subprocess — NOT an import:
    the supervisor stays a zero-dependency process that never imports the
    orchestrator package (ADR-0015); it merely spawns it, exactly like
    ``run_iteration`` does. A non-zero exit (runtime missing, kernel too
    old, runsc not registered with the daemon, or the GitHub PAT failing
    the FR-6 scope probes) refuses to start the orchestrator: fail closed,
    never a silent degradation. The 180s subprocess bound covers the five
    bounded GitHub API probes (10s each worst case) plus the daemon probes.
    ``AGENT_SANDBOX_RUNTIME=runc`` is honored as the explicit,
    loudly-warned dev-only override.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "orchestrator.preflight"],
            capture_output=True,
            timeout=180,
        )
    except subprocess.SubprocessError as e:
        log(f"Sandbox runtime preflight could not run: {e}", "ERROR")
        sys.exit(1)
    if proc.returncode != 0:
        detail = (
            (proc.stderr or proc.stdout or b"")
            .decode("utf-8", errors="replace")
            .strip()
        )
        log(f"Sandbox runtime preflight failed; refusing to start:\n{detail}", "ERROR")
        sys.exit(1)
    log("Startup preflight passed (sandbox runtime FR-4 + PAT scopes FR-6).")


def run_iteration(
    consecutive_crashes,
    run_once,
    poll_interval,
    initial_delay=1.0,
    max_delay=60.0,
    security_tracker=None,
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
    elif exit_code == SECURITY_BLOCK_EXIT_CODE:
        # ADR-0044: a security block is a deliberate, non-retriable termination
        # (the issue is quarantined as agent-blocked by recovery). It is NOT a
        # crash — consecutive_crashes is reset and no exponential backoff is
        # applied. The circuit breaker tracks the rate of security blocks
        # across iterations; if the threshold is exceeded within the rolling
        # window, the supervisor halts to prevent an active prompt-injection
        # attack from churning.
        consecutive_crashes = 0
        if security_tracker is None:
            security_tracker = SecurityBlockTracker()
        security_tracker.record_block()
        count = security_tracker.count()
        if security_tracker.is_tripped():
            log(
                f"CIRCUIT_BREAKER: security blocks exceeded threshold "
                f"({count} in {security_tracker.window}s window, threshold "
                f"{security_tracker.threshold}). Halting supervisor.",
                "ERROR",
            )
            return consecutive_crashes, True, 2

        log(
            f"Security block exit (code {exit_code}). Issue quarantined. "
            f"Security blocks in window: {count}/{security_tracker.threshold}."
        )

        if run_once:
            log("RUN_ONCE is enabled. Exiting supervisor with code 1.")
            return consecutive_crashes, True, 1

        status = read_agent_status()
        if status == "idle":
            time.sleep(poll_interval)
        else:
            time.sleep(1.0)
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
    preflight_sandbox_runtime()

    initial_delay = 1.0
    max_delay = 60.0
    consecutive_crashes = 0
    security_tracker = SecurityBlockTracker()

    run_once = os.getenv("RUN_ONCE", "0").lower() in ("1", "true", "yes")
    poll_interval = float(os.getenv("AGENT_POLL_INTERVAL", "10.0"))

    while True:
        consecutive_crashes, should_exit, exit_code = run_iteration(
            consecutive_crashes,
            run_once,
            poll_interval,
            initial_delay,
            max_delay,
            security_tracker=security_tracker,
        )
        if should_exit:
            sys.exit(exit_code)


if __name__ == "__main__":
    main()
