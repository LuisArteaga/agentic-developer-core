"""Unit tests for the cycle-scoped sandbox lifecycle (orchestrator.sandbox_lifecycle).

Covers the issue #164 lifecycle contract: one sandbox per issue cycle
(created on first use, re-used by every later call site of the same cycle,
destroyed deterministically at cycle end), the ownership token that scopes
the startup orphan sweep, and the sweep's keep-rule for a resumable cycle.
Backends are fake runners injected through the public ``runner_factory``
seam — no Docker daemon is required.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.sandbox import (
    SandboxExecResult,
    SandboxError,
    SandboxUnavailableError,
)
from orchestrator.sandbox_lifecycle import (
    CycleSandbox,
    acquire_cycle_sandbox,
    bind_cycle,
    cycle_token_for_issue,
    release_cycle_sandbox,
    sweep_orphaned_sandboxes,
)


class _FakeRunner:
    """Minimal SandboxRunner stand-in recording the lifecycle calls it sees."""

    def __init__(self):
        self.created = []
        self.exec_calls = []
        self.destroy_count = 0

    def create(self, workspace):
        self.created.append(workspace)

    def exec(self, args, timeout):
        self.exec_calls.append((list(args), timeout))
        return SandboxExecResult(
            exit_code=0, output="ok\n", timed_out=False, duration_seconds=0.0
        )

    def destroy(self):
        self.destroy_count += 1


def _recording_factory(runners, tokens):
    """Build a runner factory that records the cycle token it was called with."""

    def _factory(cycle_token):
        tokens.append(cycle_token)
        runner = _FakeRunner()
        runners.append(runner)
        return runner

    return _factory


class _LifecycleTestBase(unittest.TestCase):
    """Base class resetting the process-local cycle state around each test."""

    def setUp(self):
        release_cycle_sandbox()
        bind_cycle(None)
        self.workspace = Path("/tmp/cycle-workspace")

    def tearDown(self):
        release_cycle_sandbox()
        bind_cycle(None)


class TestCycleTokenForIssue(unittest.TestCase):
    def test_cycle_without_an_issue_has_no_token(self):
        # An idle run owns no sandboxes; None is the sweep's "nothing is
        # resumable" signal.
        self.assertIsNone(cycle_token_for_issue(None))

    def test_token_is_stable_for_the_issue(self):
        # Stability is the whole point: a crashed cycle resumes under the same
        # token, so the sweep recognizes its containers as its own.
        self.assertEqual(cycle_token_for_issue(164), "issue-164")
        self.assertEqual(cycle_token_for_issue(164), cycle_token_for_issue(164))

    def test_different_issues_get_different_tokens(self):
        self.assertNotEqual(cycle_token_for_issue(164), cycle_token_for_issue(163))


class TestAcquireCycleSandbox(_LifecycleTestBase):
    def test_first_acquire_creates_the_sandbox_for_the_bound_cycle(self):
        runners: list[_FakeRunner] = []
        tokens: list[str | None] = []
        bind_cycle(cycle_token_for_issue(164))

        sandbox = acquire_cycle_sandbox(
            self.workspace, runner_factory=_recording_factory(runners, tokens)
        )

        self.assertEqual(tokens, ["issue-164"])
        self.assertEqual(runners[0].created, [self.workspace])
        self.assertEqual(sandbox.workspace, self.workspace)
        self.assertEqual(sandbox.cycle_token, "issue-164")

    def test_second_acquire_in_the_same_cycle_reuses_the_sandbox(self):
        runners: list[_FakeRunner] = []
        tokens: list[str | None] = []
        factory = _recording_factory(runners, tokens)
        bind_cycle(cycle_token_for_issue(164))

        first = acquire_cycle_sandbox(self.workspace, runner_factory=factory)
        second = acquire_cycle_sandbox(self.workspace, runner_factory=factory)

        # One sandbox for the cycle: the retry path (and every other call site
        # of the same cycle) re-uses it instead of re-creating it.
        self.assertIs(first, second)
        self.assertEqual(len(runners), 1)
        self.assertEqual(len(runners[0].created), 1)

    def test_acquire_for_another_workspace_recreates_the_sandbox(self):
        runners: list[_FakeRunner] = []
        tokens: list[str | None] = []
        factory = _recording_factory(runners, tokens)
        bind_cycle(cycle_token_for_issue(164))
        acquire_cycle_sandbox(self.workspace, runner_factory=factory)

        other = acquire_cycle_sandbox(Path("/tmp/other"), runner_factory=factory)

        self.assertEqual(other.workspace, Path("/tmp/other"))
        self.assertEqual(len(runners), 2)
        # The previous sandbox is torn down, never leaked into the new binding.
        self.assertEqual(runners[0].destroy_count, 1)

    def test_factory_failure_propagates_and_registers_no_handle(self):
        runners: list[_FakeRunner] = []
        tokens: list[str | None] = []

        def _failing_factory(cycle_token):
            raise SandboxUnavailableError("docker daemon unreachable")

        with self.assertRaises(SandboxUnavailableError):
            acquire_cycle_sandbox(self.workspace, runner_factory=_failing_factory)

        # Fail closed and leave no stale handle behind: the next acquire
        # builds a real sandbox instead of returning a phantom one.
        sandbox = acquire_cycle_sandbox(
            self.workspace, runner_factory=_recording_factory(runners, tokens)
        )
        self.assertEqual(len(runners), 1)
        self.assertEqual(runners[0].created, [self.workspace])
        self.assertIsInstance(sandbox, CycleSandbox)

    def test_create_failure_propagates_and_registers_no_handle(self):
        class _CreateFails(_FakeRunner):
            def create(self, workspace):
                raise SandboxError("image missing")

        def _factory(cycle_token):
            return _CreateFails()

        with self.assertRaises(SandboxError):
            acquire_cycle_sandbox(self.workspace, runner_factory=_factory)

        runners: list[_FakeRunner] = []
        tokens: list[str | None] = []
        acquire_cycle_sandbox(
            self.workspace, runner_factory=_recording_factory(runners, tokens)
        )
        self.assertEqual(len(runners), 1)


class TestReleaseCycleSandbox(_LifecycleTestBase):
    def test_release_destroys_the_live_sandbox(self):
        runners: list[_FakeRunner] = []
        tokens: list[str | None] = []
        acquire_cycle_sandbox(
            self.workspace, runner_factory=_recording_factory(runners, tokens)
        )

        release_cycle_sandbox()

        self.assertEqual(runners[0].destroy_count, 1)

    def test_release_is_idempotent(self):
        runners: list[_FakeRunner] = []
        tokens: list[str | None] = []
        acquire_cycle_sandbox(
            self.workspace, runner_factory=_recording_factory(runners, tokens)
        )

        release_cycle_sandbox()
        release_cycle_sandbox()

        # Teardown may be reached twice (Recovery after a failed node, a
        # rebinding, a repeated cycle end): the sandbox dies exactly once.
        self.assertEqual(runners[0].destroy_count, 1)

    def test_release_without_a_sandbox_is_a_noop(self):
        # A cycle that never reached an untrusted-execution call site must be
        # able to end cleanly.
        release_cycle_sandbox()


class TestBindCycle(_LifecycleTestBase):
    def test_binding_the_same_cycle_keeps_the_sandbox(self):
        runners: list[_FakeRunner] = []
        tokens: list[str | None] = []
        bind_cycle(cycle_token_for_issue(164))
        acquire_cycle_sandbox(
            self.workspace, runner_factory=_recording_factory(runners, tokens)
        )

        bind_cycle(cycle_token_for_issue(164))

        self.assertEqual(runners[0].destroy_count, 0)

    def test_binding_another_cycle_destroys_the_previous_sandbox(self):
        runners: list[_FakeRunner] = []
        tokens: list[str | None] = []
        bind_cycle(cycle_token_for_issue(163))
        acquire_cycle_sandbox(
            self.workspace, runner_factory=_recording_factory(runners, tokens)
        )

        bind_cycle(cycle_token_for_issue(164))

        # A stale handle must never leak into the next cycle.
        self.assertEqual(runners[0].destroy_count, 1)

    def test_unbinding_destroys_the_live_sandbox(self):
        runners: list[_FakeRunner] = []
        tokens: list[str | None] = []
        bind_cycle(cycle_token_for_issue(164))
        acquire_cycle_sandbox(
            self.workspace, runner_factory=_recording_factory(runners, tokens)
        )

        bind_cycle(None)

        self.assertEqual(runners[0].destroy_count, 1)


class TestCycleSandboxExec(unittest.TestCase):
    def test_exec_delegates_args_and_timeout(self):
        runner = _FakeRunner()
        sandbox = CycleSandbox(
            runner=runner, workspace=Path("/tmp/cycle-workspace"), cycle_token=None
        )

        result = sandbox.exec(["make", "verify"], timeout=300)

        self.assertEqual(runner.exec_calls, [(["make", "verify"], 300)])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "ok\n")

    def test_destroy_swallows_a_failing_backend_loudly(self):
        class _DestroyFails(_FakeRunner):
            def destroy(self):
                raise RuntimeError("docker gone")

        sandbox = CycleSandbox(
            runner=_DestroyFails(),
            workspace=Path("/tmp/cycle-workspace"),
            cycle_token=None,
        )

        # Cycle-end routing (Recovery) must never be blocked by a cleanup bug;
        # the operator still has to see it.
        with self.assertLogs("orchestrator.sandbox_lifecycle", level="ERROR") as logs:
            sandbox.destroy()
        assert "docker gone" in "\n".join(logs.output)


class TestSweepOrphanedSandboxes(unittest.TestCase):
    def _patched(self, containers, removal_results=None):
        """Patch the sweep's docker-facing helpers (listing + removal)."""
        removal_results = removal_results or {}
        removed = []

        def _remove(name, timeout=30):
            removed.append(name)
            return removal_results.get(name, True)

        return (
            patch(
                "orchestrator.sandbox_lifecycle.list_managed_sandbox_containers",
                return_value=containers,
            ),
            patch(
                "orchestrator.sandbox_lifecycle.remove_sandbox_container",
                side_effect=_remove,
            ),
            removed,
        )

    def test_resumable_cycle_containers_survive_the_sweep(self):
        listing, removal, removed = self._patched(
            [
                ("agdc-sandbox-aaaa1111", "issue-164"),
                ("agdc-sandbox-bbbb2222", "issue-163"),
            ]
        )
        with listing, removal:
            survivors = sweep_orphaned_sandboxes("issue-164")

        # The resumed cycle may still be alive in another process: its
        # containers are kept, every other managed container is an orphan.
        self.assertEqual(removed, ["agdc-sandbox-bbbb2222"])
        self.assertEqual(survivors, ["agdc-sandbox-bbbb2222"])

    def test_fresh_cycle_sweeps_every_managed_container(self):
        listing, removal, removed = self._patched(
            [
                ("agdc-sandbox-aaaa1111", "issue-164"),
                ("agdc-sandbox-bbbb2222", ""),
            ]
        )
        with listing, removal:
            survivors = sweep_orphaned_sandboxes(None)

        # Nothing is resumable, so no container is spared — including one
        # whose cycle label is missing.
        self.assertEqual(removed, ["agdc-sandbox-aaaa1111", "agdc-sandbox-bbbb2222"])
        self.assertEqual(survivors, removed)

    def test_unremovable_container_is_not_reported_as_removed(self):
        listing, removal, removed = self._patched(
            [("agdc-sandbox-aaaa1111", "issue-163")],
            removal_results={"agdc-sandbox-aaaa1111": False},
        )
        with listing, removal:
            survivors = sweep_orphaned_sandboxes(None)

        # The removal helper logged the manual-cleanup instruction; the sweep
        # reports what it actually removed.
        self.assertEqual(removed, ["agdc-sandbox-aaaa1111"])
        self.assertEqual(survivors, [])

    def test_unavailable_runtime_degrades_to_a_warning(self):
        with patch(
            "orchestrator.sandbox_lifecycle.list_managed_sandbox_containers",
            side_effect=SandboxUnavailableError("docker daemon unreachable"),
        ):
            with self.assertLogs(
                "orchestrator.sandbox_lifecycle", level="WARNING"
            ) as logs:
                survivors = sweep_orphaned_sandboxes("issue-164")

        # Cleanup must never be the reason a cycle does not begin.
        self.assertEqual(survivors, [])
        assert "docker daemon unreachable" in "\n".join(logs.output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
