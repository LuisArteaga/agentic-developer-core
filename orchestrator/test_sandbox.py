"""Unit tests for the Execution Sandbox runner (orchestrator.sandbox).

Covers the ADR-0056 slice-1 contract (issue #159): create/exec/destroy
semantics, the ADR-0043-extended sandbox env allowlist (credential names
never reach the sandbox under any configuration, FR-6), timeout handling
with partial output, the data-vs-infrastructure exit classification (no
false green), and the fail-closed backend selection. All docker
interactions are scripted fakes — no Docker daemon is required (FR-9's
no-container TDD requirement).
"""

import json
import os
import subprocess
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from orchestrator.sandbox import (
    DEFAULT_SANDBOX_CPUS,
    DEFAULT_SANDBOX_IMAGE,
    DEFAULT_SANDBOX_MEMORY,
    DEFAULT_SANDBOX_PIDS_LIMIT,
    DockerSandboxRunner,
    SandboxConfigError,
    SandboxError,
    SandboxExecResult,
    SandboxImageMissingError,
    SandboxUnavailableError,
    build_sandbox_env,
    get_sandbox_runner,
)


def _completed(returncode=0, stdout=b"", stderr=b""):
    res = unittest.mock.MagicMock()
    res.returncode = returncode
    res.stdout = stdout
    res.stderr = stderr
    return res


def _scripted_run(script):
    """Build a subprocess.run stand-in from ``(predicate, outcome)`` rules.

    The first matching rule serves its outcome (a result object or an
    exception instance); a non-matching call fails the test loudly so
    unexpected docker invocations surface instead of silently passing.
    """

    def _run(cmd, **kwargs):
        for predicate, outcome in script:
            if predicate(cmd):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"Unexpected subprocess command: {cmd}")

    return _run


def _is_docker(cmd, verb):
    return len(cmd) > 1 and cmd[0] == "docker" and cmd[1] == verb


@contextmanager
def _clean_env(extra=None):
    """Run the block with an isolated environment (save/restore guaranteed)."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(extra or {})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


class TestBuildSandboxEnv(unittest.TestCase):
    def test_default_env_is_empty_even_with_secrets_present(self):
        # FR-6: the default sandbox environment carries NO orchestrator env
        # var — parent-env secrets stay out without any filtering mechanism.
        secrets = {
            "GH_PAT": "pat-secret",
            "OPENROUTER_API_KEY": "or-secret",
            "LANGFUSE_PUBLIC_KEY": "lf-public",
            "LANGFUSE_SECRET_KEY": "lf-secret",
            "PATH": "/usr/bin",
            "HOME": "/home/user",
        }
        with _clean_env(secrets):
            self.assertEqual(build_sandbox_env(), {})

    def test_override_forwards_only_present_allowlisted_names(self):
        with _clean_env(
            {
                "AGENT_SANDBOX_ENV_ALLOWLIST": "VERIFY_TARGET, ABSENT_VAR",
                "VERIFY_TARGET": "ci",
                "GH_PAT": "pat-secret",
            }
        ):
            self.assertEqual(build_sandbox_env(), {"VERIFY_TARGET": "ci"})

    def test_override_requesting_credential_name_fails_loudly(self):
        # "Under any configuration" (FR-6): requesting a credential var by
        # name is a loud configuration error, never a silent pass-through.
        with _clean_env({"AGENT_SANDBOX_ENV_ALLOWLIST": "GH_PAT,LANGFUSE_SECRET_KEY"}):
            with self.assertRaises(SandboxConfigError) as ctx:
                build_sandbox_env()
            message = str(ctx.exception)
            assert "GH_PAT" in message
            assert "LANGFUSE_SECRET_KEY" in message

    def test_override_requesting_credential_name_fails_even_if_absent(self):
        # The guard is config validation — independent of the parent env.
        with _clean_env({"AGENT_SANDBOX_ENV_ALLOWLIST": "GH_TOKEN"}):
            with self.assertRaises(SandboxConfigError):
                build_sandbox_env()


class TestDockerSandboxRunnerCreate(unittest.TestCase):
    def setUp(self):
        self.workspace = Path("/tmp/whatever-workspace")
        self.runner = DockerSandboxRunner(
            image="verify-img", cpus="2", memory="4g", pids_limit="512"
        )

    def test_create_success_probes_daemon_and_image(self):
        calls = []

        def _run(cmd, **kwargs):
            calls.append(list(cmd))
            if _is_docker(cmd, "info"):
                return _completed(0, stdout=b"27.0.3\n")
            if _is_docker(cmd, "image"):
                return _completed(0)
            raise AssertionError(f"Unexpected subprocess command: {cmd}")

        with patch("orchestrator.sandbox.subprocess.run", side_effect=_run):
            self.runner.create(self.workspace)
        self.assertEqual(calls[0][:2], ["docker", "info"])
        self.assertEqual(calls[1][:3], ["docker", "image", "inspect"])

    def test_create_daemon_unreachable_raises(self):
        with patch(
            "orchestrator.sandbox.subprocess.run",
            return_value=_completed(1, stderr=b"Cannot connect to the Docker daemon"),
        ):
            with self.assertRaises(SandboxUnavailableError):
                self.runner.create(self.workspace)

    def test_create_missing_image_raises_loud_config_error(self):
        def _run(cmd, **kwargs):
            if _is_docker(cmd, "info"):
                return _completed(0, stdout=b"27.0.3\n")
            return _completed(1, stderr=b"No such image: verify-img")

        with patch("orchestrator.sandbox.subprocess.run", side_effect=_run):
            with self.assertRaises(SandboxImageMissingError) as ctx:
                self.runner.create(self.workspace)
            message = str(ctx.exception)
            assert "verify-img" in message
            assert "AGENT_SANDBOX_IMAGE" in message

    def test_create_missing_docker_binary_raises_unavailable(self):
        with patch(
            "orchestrator.sandbox.subprocess.run",
            side_effect=OSError("No such file or directory: 'docker'"),
        ):
            with self.assertRaises(SandboxUnavailableError):
                self.runner.create(self.workspace)

    def test_create_image_probe_subprocess_error_raises_unavailable(self):
        def _run(cmd, **kwargs):
            if _is_docker(cmd, "info"):
                return _completed(0, stdout=b"27.0.3\n")
            raise subprocess.SubprocessError("inspect exploded")

        with patch("orchestrator.sandbox.subprocess.run", side_effect=_run):
            with self.assertRaises(SandboxUnavailableError):
                self.runner.create(self.workspace)


class TestDockerSandboxRunnerExec(unittest.TestCase):
    def setUp(self):
        self.workspace = Path("/tmp/whatever-workspace")
        self.runner = DockerSandboxRunner(
            image="verify-img", cpus="2", memory="4g", pids_limit="512"
        )
        # Validate the runner through the public create() with a scripted
        # runtime — no Docker daemon is touched in tests.
        setup_script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "image"), _completed(0)),
        ]
        with patch(
            "orchestrator.sandbox.subprocess.run",
            side_effect=_scripted_run(setup_script),
        ):
            self.runner.create(self.workspace)

    def _patched(self, script):
        return patch(
            "orchestrator.sandbox.subprocess.run", side_effect=_scripted_run(script)
        )

    def test_exec_before_create_raises(self):
        fresh = DockerSandboxRunner(
            image="verify-img", cpus="2", memory="4g", pids_limit="512"
        )
        with self.assertRaises(SandboxError):
            fresh.exec(["make", "verify"], timeout=5)

    def test_exec_success_returns_data_and_default_env_is_absent(self):
        captured = {}

        def _capture(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            if _is_docker(cmd, "info"):
                return _completed(0, stdout=b"27.0.3\n")
            if _is_docker(cmd, "run"):
                return _completed(0, stdout=b"green\n")
            raise AssertionError(f"Unexpected subprocess command: {cmd}")

        with _clean_env({"GH_PAT": "pat-secret", "HOME": "/home/user"}):
            with patch("orchestrator.sandbox.subprocess.run", side_effect=_capture):
                result = self.runner.exec(["make", "verify"], timeout=300)
        argv = captured["cmd"]
        self.assertEqual(argv[0:2], ["docker", "run"])
        container_name = argv[argv.index("--name") + 1]
        assert container_name.startswith("agdc-sandbox-")
        self.assertEqual(argv[argv.index("--cpus") + 1], "2")
        self.assertEqual(argv[argv.index("--memory") + 1], "4g")
        self.assertEqual(argv[argv.index("--pids-limit") + 1], "512")
        self.assertEqual(argv[argv.index("-v") + 1], f"{self.workspace}:/workspace")
        self.assertEqual(argv[argv.index("-w") + 1], "/workspace")
        self.assertNotIn("-e", argv)  # FR-6: no env leaks by default
        self.assertEqual(argv[argv.index("--") + 1], "verify-img")
        self.assertEqual(argv[-2:], ["make", "verify"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output, "green\n")
        self.assertFalse(result.timed_out)
        assert result.duration_seconds > 0

    def test_exec_forwards_allowlisted_env_and_never_secrets(self):
        captured = {}
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "run"), _completed(0)),
        ]

        def _capture(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return _scripted_run(script)(cmd, **kwargs)

        with _clean_env(
            {
                "AGENT_SANDBOX_ENV_ALLOWLIST": "VERIFY_TARGET",
                "VERIFY_TARGET": "ci",
                "GH_PAT": "pat-secret",
                "OPENROUTER_API_KEY": "or-secret",
                "LANGFUSE_PUBLIC_KEY": "lf-public",
                "LANGFUSE_SECRET_KEY": "lf-secret",
            }
        ):
            with patch("orchestrator.sandbox.subprocess.run", side_effect=_capture):
                result = self.runner.exec(["make", "verify"], timeout=300)
        argv = captured["cmd"]
        self.assertEqual(result.exit_code, 0)
        env_flags = argv[argv.index("-e") + 1 :]
        # Only the allowlisted non-secret var is forwarded, exactly once.
        self.assertEqual(argv.count("-e"), 1)
        self.assertEqual(env_flags[0], "VERIFY_TARGET=ci")

    def test_exec_nonzero_exit_is_data_via_container_state(self):
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "run"), _completed(1, stdout=b"FAIL\n")),
            (
                lambda cmd: _is_docker(cmd, "inspect"),
                _completed(
                    0, stdout=json.dumps({"Status": "exited", "ExitCode": 1}).encode()
                ),
            ),
        ]
        with self._patched(script) as _run:
            result = self.runner.exec(["make", "verify"], timeout=300)
        self.assertEqual(result.exit_code, 1)  # data, not a runner error
        self.assertEqual(result.output, "FAIL\n")
        self.assertFalse(result.timed_out)

    def test_exec_nonzero_without_container_raises_infra(self):
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "run"), _completed(1, stdout=b"boom")),
            (lambda cmd: _is_docker(cmd, "inspect"), _completed(1)),
            (lambda cmd: _is_docker(cmd, "rm"), _completed(0)),
        ]
        with self._patched(script):
            with self.assertRaises(SandboxError) as ctx:
                self.runner.exec(["make", "verify"], timeout=300)
            assert "client rc=1" in str(ctx.exception)

    def test_exec_nonzero_with_unfinished_container_raises_infra(self):
        # False-green guard: a docker client that dies while the command is
        # still running must NEVER yield a green/red exit code.
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "run"), _completed(1)),
            (
                lambda cmd: _is_docker(cmd, "inspect"),
                _completed(
                    0, stdout=json.dumps({"Status": "running", "ExitCode": 0}).encode()
                ),
            ),
            (lambda cmd: _is_docker(cmd, "rm"), _completed(0)),
        ]
        with self._patched(script):
            with self.assertRaises(SandboxError):
                self.runner.exec(["make", "verify"], timeout=300)

    def test_exec_nonzero_with_malformed_inspect_raises_infra(self):
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "run"), _completed(1)),
            (lambda cmd: _is_docker(cmd, "inspect"), _completed(0, stdout=b"not-json")),
            (lambda cmd: _is_docker(cmd, "rm"), _completed(0)),
        ]
        with self._patched(script):
            with self.assertRaises(SandboxError):
                self.runner.exec(["make", "verify"], timeout=300)

    def test_exec_daemon_unreachable_at_exec_time_raises(self):
        # Distinct infra signal (issue #159 edge case): daemon died between
        # create and exec → SandboxUnavailableError, not a verify failure.
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(1, stderr=b"down")),
        ]
        with self._patched(script):
            with self.assertRaises(SandboxUnavailableError):
                self.runner.exec(["make", "verify"], timeout=300)

    def test_exec_timeout_kills_container_and_returns_partial_output(self):
        removed = []
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (
                lambda cmd: _is_docker(cmd, "run"),
                subprocess.TimeoutExpired(
                    cmd=["docker", "run"], timeout=300, output=b"partial out"
                ),
            ),
            (
                lambda cmd: _is_docker(cmd, "rm"),
                _completed(0, stdout=b"removed"),
            ),
        ]

        def _capture_removal(cmd, **kwargs):
            if _is_docker(cmd, "rm"):
                removed.append(list(cmd))
            return _scripted_run(script)(cmd, **kwargs)

        with patch("orchestrator.sandbox.subprocess.run", side_effect=_capture_removal):
            result = self.runner.exec(["make", "verify"], timeout=300)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.exit_code, -1)
        self.assertEqual(result.output, "partial out")
        self.assertEqual(len(removed), 1)  # container killed at timeout
        self.assertEqual(removed[0][:3], ["docker", "rm", "-f"])

    def test_exec_docker_binary_missing_raises_unavailable(self):
        script = [
            (
                lambda cmd: _is_docker(cmd, "info"),
                OSError("No such file or directory: 'docker'"),
            ),
        ]
        with self._patched(script):
            with self.assertRaises(SandboxUnavailableError):
                self.runner.exec(["make", "verify"], timeout=300)

    def test_exec_run_subprocess_error_raises_unavailable(self):
        # The daemon probe passes but the run itself dies (e.g. client killed
        # by a signal): infrastructure, never a fabricated exit code.
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (
                lambda cmd: _is_docker(cmd, "run"),
                subprocess.SubprocessError("run exploded"),
            ),
        ]
        with self._patched(script):
            with self.assertRaises(SandboxUnavailableError):
                self.runner.exec(["make", "verify"], timeout=300)

    def test_exec_run_oserror_raises_unavailable(self):
        # The docker binary disappears between probe and run.
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (
                lambda cmd: _is_docker(cmd, "run"),
                OSError("No such file or directory: 'docker'"),
            ),
        ]
        with self._patched(script):
            with self.assertRaises(SandboxUnavailableError):
                self.runner.exec(["make", "verify"], timeout=300)

    def test_exec_inspect_subprocess_error_raises_infra(self):
        # The inspect step failing (not merely returning non-zero) must also
        # refuse to fabricate an exit code.
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "run"), _completed(1, stdout=b"boom")),
            (
                lambda cmd: _is_docker(cmd, "inspect"),
                subprocess.SubprocessError("inspect exploded"),
            ),
            (lambda cmd: _is_docker(cmd, "rm"), _completed(0)),
        ]
        with self._patched(script):
            with self.assertRaises(SandboxError):
                self.runner.exec(["make", "verify"], timeout=300)

    def test_infra_message_truncates_oversized_output(self):
        long_noise = b"x" * 500
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "run"), _completed(1, stdout=long_noise)),
            (lambda cmd: _is_docker(cmd, "inspect"), _completed(1)),
            (lambda cmd: _is_docker(cmd, "rm"), _completed(0)),
        ]
        with self._patched(script):
            with self.assertRaises(SandboxError) as ctx:
                self.runner.exec(["make", "verify"], timeout=300)
        message = str(ctx.exception)
        assert "…" in message  # bounded snippet, not the full 500 bytes
        assert len(message) < len(long_noise)


class TestDockerSandboxRunnerDestroy(unittest.TestCase):
    def test_destroy_without_containers_makes_no_calls(self):
        calls = []
        runner = DockerSandboxRunner(
            image="verify-img", cpus="2", memory="4g", pids_limit="512"
        )

        def _run(cmd, **kwargs):
            calls.append(list(cmd))
            return _completed(0)

        with patch("orchestrator.sandbox.subprocess.run", side_effect=_run):
            runner.destroy()
            runner.destroy()  # idempotent
        self.assertEqual(calls, [])

    def test_destroy_removes_spawned_containers_idempotently(self):
        workspace = Path("/tmp/whatever-workspace")
        runner = DockerSandboxRunner(
            image="verify-img", cpus="2", memory="4g", pids_limit="512"
        )
        removals = []
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "image"), _completed(0)),
            (lambda cmd: _is_docker(cmd, "run"), _completed(0, stdout=b"ok")),
            (
                lambda cmd: _is_docker(cmd, "rm"),
                _completed(0, stdout=b"removed"),
            ),
        ]

        def _capture(cmd, **kwargs):
            if _is_docker(cmd, "rm"):
                removals.append(list(cmd))
            return _scripted_run(script)(cmd, **kwargs)

        with patch("orchestrator.sandbox.subprocess.run", side_effect=_capture):
            runner.create(workspace)
            runner.exec(["true"], timeout=10)
            runner.exec(["true"], timeout=10)
            runner.destroy()
            runner.destroy()  # second call is a no-op
        # One exec container per exec call; each removed exactly once —
        # the second destroy() finds nothing left to remove.
        self.assertEqual(len(removals), 2)
        for cmd in removals:
            self.assertEqual(cmd[:3], ["docker", "rm", "-f"])

    def test_destroy_swallows_removal_errors(self):
        workspace = Path("/tmp/whatever-workspace")
        runner = DockerSandboxRunner(
            image="verify-img", cpus="2", memory="4g", pids_limit="512"
        )
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "image"), _completed(0)),
            (lambda cmd: _is_docker(cmd, "run"), _completed(0, stdout=b"ok")),
        ]
        with patch(
            "orchestrator.sandbox.subprocess.run", side_effect=_scripted_run(script)
        ):
            runner.create(workspace)
            runner.exec(["true"], timeout=10)
        # The runtime disappears mid-destroy: best-effort cleanup never raises.
        with patch(
            "orchestrator.sandbox.subprocess.run",
            side_effect=OSError("docker gone"),
        ):
            runner.destroy()
            runner.destroy()  # idempotent after failure


class TestGetSandboxRunner(unittest.TestCase):
    def test_default_backend_is_docker(self):
        with _clean_env({}):
            runner = get_sandbox_runner()
        self.assertIsInstance(runner, DockerSandboxRunner)

    def test_explicit_docker_backend_is_selected(self):
        with _clean_env({"AGENT_SANDBOX_BACKEND": "Docker "}):
            runner = get_sandbox_runner()
        self.assertIsInstance(runner, DockerSandboxRunner)

    def test_host_backend_fails_closed(self):
        # Fail closed (issue #159): selecting any non-docker backend —
        # including 'host' — is a loud configuration error, never a silent
        # host-side execution path.
        with _clean_env({"AGENT_SANDBOX_BACKEND": "host"}):
            with self.assertRaises(SandboxConfigError) as ctx:
                get_sandbox_runner()
            message = str(ctx.exception)
            assert "no host-side fallback" in message

    def test_unknown_backend_fails_closed(self):
        with _clean_env({"AGENT_SANDBOX_BACKEND": "kata"}):
            with self.assertRaises(SandboxConfigError):
                get_sandbox_runner()

    def test_invalid_cpu_cap_fails_loudly(self):
        with _clean_env({"AGENT_SANDBOX_CPUS": "0"}):
            with self.assertRaises(SandboxConfigError):
                get_sandbox_runner()

    def test_non_numeric_cpu_cap_fails_loudly(self):
        with _clean_env({"AGENT_SANDBOX_CPUS": "abc"}):
            with self.assertRaises(SandboxConfigError):
                get_sandbox_runner()

    def test_blank_image_falls_back_to_default(self):
        captured = {}
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "image"), _completed(0)),
        ]

        def _capture(cmd, **kwargs):
            if _is_docker(cmd, "image"):
                captured["cmd"] = list(cmd)
            return _scripted_run(script)(cmd, **kwargs)

        with _clean_env({"AGENT_SANDBOX_IMAGE": "   "}):
            with patch("orchestrator.sandbox.subprocess.run", side_effect=_capture):
                runner = get_sandbox_runner()
                runner.create(Path("/tmp/whatever-workspace"))
        self.assertEqual(
            captured["cmd"], ["docker", "image", "inspect", DEFAULT_SANDBOX_IMAGE]
        )

    def test_invalid_pid_cap_fails_loudly(self):
        with _clean_env({"AGENT_SANDBOX_PIDS_LIMIT": "abc"}):
            with self.assertRaises(SandboxConfigError):
                get_sandbox_runner()

    def test_blank_memory_cap_fails_loudly(self):
        with _clean_env({"AGENT_SANDBOX_MEMORY": ""}):
            with self.assertRaises(SandboxConfigError):
                get_sandbox_runner()

    def test_caps_and_image_are_applied_to_exec(self):
        # Behavioral check of the configured caps/image (no private access).
        captured = {}
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "image"), _completed(0)),
            (lambda cmd: _is_docker(cmd, "run"), _completed(0)),
            (lambda cmd: _is_docker(cmd, "rm"), _completed(0)),
        ]

        def _capture(cmd, **kwargs):
            if _is_docker(cmd, "run"):
                captured["cmd"] = list(cmd)
            return _scripted_run(script)(cmd, **kwargs)

        with _clean_env(
            {
                "AGENT_SANDBOX_IMAGE": "custom-img",
                "AGENT_SANDBOX_CPUS": "3",
                "AGENT_SANDBOX_MEMORY": "2g",
                "AGENT_SANDBOX_PIDS_LIMIT": "128",
            }
        ):
            with patch("orchestrator.sandbox.subprocess.run", side_effect=_capture):
                runner = get_sandbox_runner()
                runner.create(Path("/tmp/whatever-workspace"))
                runner.exec(["make", "verify"], timeout=10)
                runner.destroy()
        argv = captured["cmd"]
        self.assertEqual(argv[argv.index("--cpus") + 1], "3")
        self.assertEqual(argv[argv.index("--memory") + 1], "2g")
        self.assertEqual(argv[argv.index("--pids-limit") + 1], "128")
        self.assertEqual(argv[argv.index("--") + 1], "custom-img")


class TestSandboxDefaults(unittest.TestCase):
    def test_documented_defaults(self):
        self.assertEqual(DEFAULT_SANDBOX_IMAGE, "python:3.12")
        self.assertEqual(DEFAULT_SANDBOX_CPUS, "2")
        self.assertEqual(DEFAULT_SANDBOX_MEMORY, "4g")
        self.assertEqual(DEFAULT_SANDBOX_PIDS_LIMIT, "512")

    def test_exec_result_defaults(self):
        result = SandboxExecResult(exit_code=0, output="")
        self.assertFalse(result.timed_out)
        self.assertEqual(result.duration_seconds, 0.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
