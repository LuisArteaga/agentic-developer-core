"""Cycle-scoped Execution Sandbox lifecycle (ADR-0056 FR-7, issue #164).

One sandbox per issue cycle (ADR-0061). The cycle's sandbox is created on
first use, reused by every untrusted-execution call site within the cycle —
the Worker's ``run_command`` tool, the Test-Writer's pre-verification
discovery, and the Verify gate — and destroyed deterministically when the
cycle ends. This is what makes Hybrid Retry (ADR-0034) and the Test-Writer's
file-preservation semantics (ADR-0010) behave inside a sandbox exactly as they
did on the host: the workspace is the sandbox's persistent state (a read-write
bind mount), so an untracked test file written in attempt 1 is still there in
attempt 3, and a hard reset reverts tracked files only.

What "one sandbox per cycle" means here: one *identity* — image, resource
caps, runtime, egress network, and ownership labels, validated once instead of
re-probed per command — not one long-lived container. ``orchestrator.sandbox``
still spawns a container per exec, because that is the only way to guarantee
that a command exceeding the exec timeout is actually killed while partial
output is returned and the retry path still finds a working sandbox; the
container is removed as soon as its outcome is classified.

Ownership marker and orphan sweep. Every container a cycle spawns carries the
``SANDBOX_MANAGED_LABEL`` marker plus a ``SANDBOX_CYCLE_LABEL`` token derived
from the issue number (``cycle_token_for_issue``). A crash mid-cycle leaves the
in-flight container behind (killing the ``docker run`` client does not kill the
container), so the orchestrator sweeps at startup — the Claim-Node runs
``sweep_orphaned_sandboxes`` with the token of the cycle it is about to resume
(or ``None`` when it is starting a fresh cycle) and destroys every managed
container the resumable cycle does not own. The keep-rule is deliberately
conservative: containers of the resumable cycle survive, because a resumed
cycle may still be alive in another process (the Process Supervisor's restart
can race its own child), and destroying a live sandbox's containers is not a
recoverable mistake. Containers without the managed marker are never touched —
this deployment only cleans up what it demonstrably spawned.

Stateful Resume: a resumed cycle runs in a new process, so no live sandbox
handle exists; the next call site creates one bound to the same workspace
(the workspace volume persists on disk, the container does not need to) — the
FR-7 "recreated if absent" path, which is the normal path.
"""

import logging
from pathlib import Path
from typing import Callable

from orchestrator.sandbox import (
    SandboxError,
    SandboxExecResult,
    SandboxRunner,
    get_sandbox_runner,
    list_managed_sandbox_containers,
    remove_sandbox_container,
)

logger = logging.getLogger("orchestrator.sandbox_lifecycle")

# The cycle that currently owns sandboxes, and its live sandbox handle. Both
# are process-local: a cycle runs in one orchestrator process, so a restart
# starts from no handle and re-creates the sandbox on first use.
_bound_cycle: str | None = None
_live_sandbox: "CycleSandbox | None" = None


def cycle_token_for_issue(issue_number: int | None) -> str | None:
    """Return the ownership token of the cycle processing ``issue_number``.

    The token is the marker the startup sweep compares against, so it must be
    stable for the whole cycle (including across a crash and the resume that
    follows) and must not be shared with a different cycle. A cycle processes
    exactly one issue in one process, so the issue number is sufficient: a
    crashed cycle resumes with the same token (its containers survive the
    sweep), and a later cycle for the same issue sweeps them as orphans
    because it starts from a non-resumable state.
    """
    if issue_number is None:
        return None
    return f"issue-{issue_number}"


def bind_cycle(cycle_token: str | None) -> None:
    """Declare the cycle that owns sandboxes from this point on.

    Called by the Claim-Node: with the resumed cycle's token on the resume
    path, with the freshly claimed issue's token on a fresh cycle. Binding a
    different cycle destroys the previous cycle's sandbox first, so a stale
    handle can never leak into the next cycle (in-process defense; the sweep
    covers the cross-process case).
    """
    global _bound_cycle
    if cycle_token == _bound_cycle:
        return
    release_cycle_sandbox()
    _bound_cycle = cycle_token


def acquire_cycle_sandbox(
    workspace: Path,
    runner_factory: Callable[[str | None], SandboxRunner] | None = None,
) -> "CycleSandbox":
    """Return the bound cycle's live sandbox, creating it on first use.

    Reuse is the point: the second and later callers in a cycle (another
    attempt, another Worker command) get the sandbox that was already
    validated, cap-bound and labeled, instead of paying the daemon/image
    probes and losing the cycle identity again. The first call in a cycle (or
    after a resume, or after a binding change) creates the sandbox through
    ``runner_factory`` — the sandbox backend factory, injectable for tests and
    alternative backends — and binds it to ``workspace``.

    A runner failure (daemon unreachable, image missing, invalid configuration)
    propagates as ``SandboxError``: there is no host-side fallback (ADR-0056,
    fail closed), and no handle is registered.
    """
    global _live_sandbox
    if _live_sandbox is not None and _live_sandbox.workspace == workspace:
        return _live_sandbox
    release_cycle_sandbox()
    factory = runner_factory or get_sandbox_runner
    runner = factory(_bound_cycle)
    sandbox = CycleSandbox(runner=runner, workspace=workspace, cycle_token=_bound_cycle)
    runner.create(workspace)
    _live_sandbox = sandbox
    return sandbox


def release_cycle_sandbox() -> None:
    """Destroy the live cycle sandbox, if any; idempotent.

    The deterministic cycle-end teardown (Failure Recovery, Security-Block
    quarantine, completed merge). Safe to call when no sandbox was ever
    created, and safe to call more than once.
    """
    global _live_sandbox
    sandbox, _live_sandbox = _live_sandbox, None
    if sandbox is not None:
        sandbox.destroy()


def sweep_orphaned_sandboxes(resumable_cycle: str | None) -> list[str]:
    """Destroy managed sandbox containers the resumable cycle does not own.

    Runs at orchestrator startup (Claim-Node). ``resumable_cycle`` is the
    ownership token of the cycle being resumed, or ``None`` for a fresh cycle
    (nothing is resumable, so every managed container is an orphan). Returns
    the names of the containers it removed, for logging and tests.

    Degrades loudly instead of failing the cycle: an unreachable daemon means
    no sandbox can start anyway (the next ``create`` fails closed), and cleanup
    must never be the reason a cycle does not begin.
    """
    try:
        containers = list_managed_sandbox_containers()
    except SandboxError as e:
        logger.warning(
            "Startup sandbox sweep skipped (sandbox runtime unavailable): %s", e
        )
        return []
    removed: list[str] = []
    for name, cycle in containers:
        if resumable_cycle is not None and cycle == resumable_cycle:
            logger.info(
                "Startup sandbox sweep: keeping %s (owned by resumed cycle %s).",
                name,
                cycle,
            )
            continue
        logger.warning(
            "Startup sandbox sweep: removing orphaned sandbox container %s (cycle %s).",
            name,
            cycle or "unlabeled",
        )
        if remove_sandbox_container(name):
            removed.append(name)
    return removed


class CycleSandbox:
    """The issue cycle's Execution Sandbox: one handle, many execs.

    Thin by design — it owns the runner's lifetime for the cycle, not the exec
    mechanics. ``exec`` delegates to the backend (see ``orchestrator.sandbox``
    for the timeout, output and exit-code contracts); ``destroy`` ends the
    sandbox's life for the cycle.
    """

    def __init__(self, runner: SandboxRunner, workspace: Path, cycle_token: str | None):
        self.runner = runner
        self.workspace = workspace
        self.cycle_token = cycle_token

    def exec(self, args: list[str], timeout: float) -> SandboxExecResult:
        """Run ``args`` in the sandbox; non-zero exits are data, not failures."""
        return self.runner.exec(args, timeout=timeout)

    def destroy(self) -> None:
        """Tear the sandbox down; best-effort and never raises.

        A backend's ``destroy`` is already idempotent and non-raising by
        contract; the guard here keeps a third-party backend's bug from
        breaking cycle-end routing (Recovery must always run).
        """
        try:
            self.runner.destroy()
        except Exception as e:
            logger.error(
                "Cycle sandbox teardown failed (container cleanup is retried "
                "by the next startup sweep): %s",
                e,
            )
