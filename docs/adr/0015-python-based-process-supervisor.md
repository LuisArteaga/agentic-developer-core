# ADR 0015: Python-Based Process Supervisor

* **Status**: Accepted
* **Date**: 2026-06-27
* **Deciders**: Luis Arteaga & Antigravity

## Context and Problem Statement
The containerized autonomous developer orchestrator requires a resilient entrypoint to manage its execution. The entrypoint must perform environment variable validation, check external service reachability (GitHub APIs and OpenRouter), spawn the orchestrator Python process, and manage recovery/exponential backoff in case of a process crash.

The PRD and initial issue requirements specified creating a Bash script (`scripts/entrypoint.sh`) to perform these tasks. However, Bash process supervisors have significant drawbacks:
1. **Testing Complexity**: Verifying backoff delays, reachability retries, and crash loops in Bash is fragile, requiring complex subprocess spawning, path manipulation, and linter checks.
2. **Readability & Platform Independence**: Writing exponential backoff, random jitter calculation, and graceful exit handling in Bash is verbose, error-prone, and platform-specific compared to Python.

## Decision Drivers
* **Robustness & Error Resilience**: Fast and reliable backoff recovery from startup crashes vs runtime crashes.
* **Testability**: The supervisor must be fully covered by unit tests without introducing risk of test hangs, resource leaks, or infinite loops.
* **Maintainability**: Clear separation of concern between process control (looping/termination) and logic.

## Considered Options
* **Option 1: Bash Process Supervisor (`scripts/entrypoint.sh`)**
  Implement the launcher as a Bash script. It fulfills the traditional CLI entrypoint pattern but limits automated unit test coverage.
* **Option 2: Pure Python Process Supervisor (`scripts/entrypoint.py`) with Monolithic Loop**
  Implement the supervisor in Python. Better readability and testability, but a monolithic `while True` main loop makes testing multi-iteration backoff prone to infinite CPU spinning if mocks are misconfigured or fail.
* **Option 3: Python Process Supervisor with Loop-Iteration Extraction (`run_iteration`)**
  Implement the supervisor in Python, but extract the core execution step into a helper function `run_iteration()` that returns a control tuple `(consecutive_crashes, should_exit, exit_code)`. The `main()` loop simply executes this function and acts on the exit signal.

## Decision
We chose **Option 3**.

Implementing the supervisor in Python ensures cross-platform consistency and zero-dependency imports. By refactoring the loop body into `run_iteration(...)`, we completely decoupled process execution from process control.

This allows us to write unit tests (`scripts/test_entrypoint.py`) that test individual loop iterations linearly and deterministically:
* No `while` loops are executed in the unit tests, eliminating any risk of test hangs or high CPU/RAM usage.
* Process termination (`sys.exit`) is handled at the top level in `main()`, making `run_iteration` a side-effect-free, easily testable logic function.
* Random jitter is patched in tests using `@patch("random.uniform")` to verify exact sleep durations.

## Consequences
* **Pros**:
  * **100% Test Coverage**: Every branch of the supervisor (startup crash, runtime crash reset, exponential backoff scaling, idle sleep) is covered by fast, safe, and deterministic unit tests.
  * **Zero CPU Spinning in Tests**: Eliminating `while True` from the tested code paths prevents WSL/container crashes due to out-of-memory or high CPU usage.
  * **Zero Dependencies**: Imports are limited to Python's standard library (`os`, `sys`, `time`, `subprocess`, `urllib.request`, `random`, `json`, `pathlib`).
* **Cons**:
  * **Indirection**: Slightly more code lines due to splitting the execution step from the infinite loop wrapper.

## Inspiration & References
* **Separation of Concerns (Clean Code)**: Isolating loop mechanics from execution steps is a standard pattern in service daemons. C.f. [celery/worker/loops.py](https://github.com/celery/celery/blob/master/celery/worker/loops.py) where the actual loop is abstracted away from task processing logic.
* **Deterministic Randomness in Tests**: Patching random generators to ensure consistent delays is a standard testing pattern, documented in [Python Mock Documentation](https://docs.python.org/3/library/unittest.mock.html#unittest.mock.Mock.side_effect).
