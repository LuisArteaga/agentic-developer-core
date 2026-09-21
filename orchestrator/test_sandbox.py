"""Unit tests for the Execution Sandbox runner (orchestrator.sandbox).

Covers the ADR-0056 slice-1 contract (issue #159): create/exec/destroy
semantics, the ADR-0043-extended sandbox env allowlist (credential names
never reach the sandbox under any configuration, FR-6), timeout handling
with partial output, the data-vs-infrastructure exit classification (no
false green), and the fail-closed backend selection. All docker
interactions are scripted fakes — no Docker daemon is required (FR-9's
no-container TDD requirement).
"""

import ipaddress
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
    DEFAULT_SANDBOX_RUNTIME,
    DEFAULT_SANDBOX_TMPFS_SIZE,
    SANDBOX_CYCLE_LABEL,
    SANDBOX_MANAGED_LABEL,
    SANDBOX_RUNTIME_OVERRIDE,
    DockerSandboxRunner,
    FORBIDDEN_SANDBOX_ENV_NAMES,
    SandboxConfigError,
    SandboxEgressConfig,
    SandboxError,
    SandboxExecResult,
    SandboxImageMissingError,
    SandboxUnavailableError,
    build_sandbox_env,
    cap_hit_message,
    classify_cap_hit,
    get_sandbox_runner,
    list_managed_sandbox_containers,
    remove_sandbox_container,
    resolve_sandbox_egress,
    resolve_sandbox_runtime,
)


def _completed(returncode=0, stdout=b"", stderr=b""):
    res = unittest.mock.MagicMock()
    res.returncode = returncode
    res.stdout = stdout
    res.stderr = stderr
    return res


def _scripted_run(script, removals=None):
    """Build a subprocess.run stand-in from ``(predicate, outcome)`` rules.

    The first matching rule serves its outcome (a result object or an
    exception instance); a non-matching call fails the test loudly so
    unexpected docker invocations surface instead of silently passing.

    ``docker rm -f`` is scripted as a success by default and recorded in
    ``removals`` when a list is passed: every exec classification path
    (success, data exit, timeout, infrastructure failure) removes its
    container, so repeating that rule in every test would drown the intent
    it is meant to preserve. A test that needs a failing removal scripts it
    explicitly — explicit rules win, the default is the last resort.
    """

    def _run(cmd, **kwargs):
        for predicate, outcome in script:
            if predicate(cmd):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        if _is_docker(cmd, "rm"):
            if removals is not None:
                removals.append(list(cmd))
            return _completed(0)
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


class TestForbiddenSandboxEnvMatrix(unittest.TestCase):
    """FR-6 regression matrix (issue #163): NO configuration path places an
    orchestrator secret inside the sandbox environment.

    Parametrized over the full documented secret inventory × the env-
    construction configurations (default / allowlist override with the
    secret present / override with the secret absent), plus an inventory
    pin so a newly introduced orchestrator secret cannot silently bypass
    the forbidden-name guard.
    """

    # The documented orchestrator secret inventory (.env.example + its
    # consumers: orchestrator/nodes.py GitHub API auth, orchestrator/git.py
    # credential helper, scripts/telemetry.py Langfuse/SmithDB/OTLP headers).
    # A new secret in .env.example MUST be added to FORBIDDEN_SANDBOX_ENV_NAMES
    # and here — the pin below fails loudly otherwise. JUDGE_GH_TOKEN is
    # deliberately absent: it is a Target-Repository CI secret consumed by the
    # judges' own workflow runs, never an orchestrator environment variable
    # (issue #163 cross-cutting note).
    DOCUMENTED_SECRET_INVENTORY = frozenset(
        {
            "GH_PAT",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "OPENROUTER_API_KEY",
            "LANGFUSE_PUBLIC_KEY",
            "LANGFUSE_SECRET_KEY",
            "SMITHDB_API_KEY",
            "OTEL_EXPORTER_OTLP_HEADERS",
        }
    )

    def test_documented_inventory_is_fully_rejected(self):
        # Anti-drift pin: the forbidden-name guard must equal the documented
        # secret inventory exactly — no secret left unprotected, no
        # non-secret name blocked without documentation.
        self.assertEqual(FORBIDDEN_SANDBOX_ENV_NAMES, self.DOCUMENTED_SECRET_INVENTORY)

    def test_secrets_never_reach_sandbox_env_under_any_configuration(self):
        for name in sorted(FORBIDDEN_SANDBOX_ENV_NAMES):
            with self.subTest(secret=name):
                # (a) default configuration: nothing is forwarded, ever.
                with _clean_env({name: "secret-value", "PATH": "/usr/bin"}):
                    self.assertEqual(build_sandbox_env(), {})
                # (b) override requesting the secret while it is present:
                # loud rejection, never a silent pass-through.
                with _clean_env(
                    {name: "secret-value", "AGENT_SANDBOX_ENV_ALLOWLIST": name}
                ):
                    with self.assertRaises(SandboxConfigError) as ctx:
                        build_sandbox_env()
                    assert name in str(ctx.exception)
                # (c) override requesting the secret while it is absent:
                # config validation is independent of the parent environment.
                with _clean_env({"AGENT_SANDBOX_ENV_ALLOWLIST": name}):
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
        removals = []

        def _capture(cmd, **kwargs):
            if _is_docker(cmd, "run"):
                captured["cmd"] = list(cmd)
                return _completed(0, stdout=b"green\n")
            if _is_docker(cmd, "info"):
                return _completed(0, stdout=b"27.0.3\n")
            if _is_docker(cmd, "rm"):
                removals.append(list(cmd))
                return _completed(0)
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
        # FR-7: bounded scratch space — /tmp is a size-capped tmpfs, not the
        # container's unbounded writable layer.
        self.assertEqual(
            argv[argv.index("--tmpfs") + 1],
            f"/tmp:rw,size={DEFAULT_SANDBOX_TMPFS_SIZE},mode=1777",
        )
        self.assertEqual(argv[argv.index("--runtime") + 1], DEFAULT_SANDBOX_RUNTIME)
        # FR-7: the ownership marker scopes the startup sweep to containers
        # this deployment spawned. This runner was created without a cycle
        # token, so no cycle label is attached.
        labels = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--label"]
        self.assertEqual(labels, [f"{SANDBOX_MANAGED_LABEL}=1"])
        # FR-5: every sandbox attaches to the dedicated egress network and
        # routes HTTP(S) through the host-side proxy — unconditional.
        self.assertEqual(argv[argv.index("--network") + 1], "agdc-sandbox-egress")
        self.assertEqual(argv[argv.index("-v") + 1], f"{self.workspace}:/workspace")
        self.assertEqual(argv[argv.index("-w") + 1], "/workspace")
        # The only -e entries by default are the egress proxy vars (both
        # upper- and lower-case so pip/npm/curl/apt honor them); no host env
        # value (FR-6) and no secret name leaks into the sandbox.
        env_values = [argv[i + 1] for i, flag in enumerate(argv) if flag == "-e"]
        self.assertEqual(len(env_values), 4)
        self.assertEqual(
            sorted(env_values),
            sorted(
                f"{name}=http://172.30.0.1:3128"
                for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
            ),
        )
        for secret in ("pat-secret", "or-secret", "/home/user", "/usr/bin"):
            assert secret not in " ".join(argv)
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
            if _is_docker(cmd, "run"):
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
        env_flags = [argv[i + 1] for i, flag in enumerate(argv) if flag == "-e"]
        # The 4 egress proxy vars plus exactly one allowlisted non-secret
        # var, forwarded once; no secret ever reaches the sandbox.
        self.assertEqual(len(env_flags), 5)
        self.assertIn("VERIFY_TARGET=ci", env_flags)
        joined = " ".join(env_flags)
        for secret in ("pat-secret", "or-secret", "lf-public", "lf-secret"):
            assert secret not in joined

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

    def test_failed_exec_removal_keeps_the_container_tracked_for_destroy(self):
        """A container whose removal fails during exec stays tracked, so
        destroy() retries exactly that leftover (FR-7)."""
        workspace = Path("/tmp/whatever-workspace")
        runner = DockerSandboxRunner(
            image="verify-img", cpus="2", memory="4g", pids_limit="512"
        )
        setup_script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "image"), _completed(0)),
            (lambda cmd: _is_docker(cmd, "run"), _completed(0, stdout=b"ok")),
            (lambda cmd: _is_docker(cmd, "rm"), _completed(1, stderr=b"device busy")),
        ]
        with patch(
            "orchestrator.sandbox.subprocess.run",
            side_effect=_scripted_run(setup_script),
        ):
            runner.create(workspace)
            with self.assertLogs("orchestrator.sandbox", level="ERROR") as logs:
                runner.exec(["true"], timeout=10)
        # Loud, actionable: the container name and the manual command.
        error_text = "\n".join(logs.output)
        assert "could not be removed" in error_text
        assert "docker rm -f agdc-sandbox-" in error_text

        # The runtime recovers: destroy() retries the leftover and succeeds.
        removals: list[list[str]] = []
        with patch(
            "orchestrator.sandbox.subprocess.run",
            side_effect=_scripted_run([], removals=removals),
        ):
            runner.destroy()
        self.assertEqual(len(removals), 1)

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

    def test_invalid_tmpfs_cap_fails_loudly(self):
        for value in ("0", "512mb", "1T", "-1m"):
            with self.subTest(value=value):
                with _clean_env({"AGENT_SANDBOX_TMPFS_SIZE": value}):
                    with self.assertRaises(SandboxConfigError):
                        get_sandbox_runner()

    def test_blank_tmpfs_cap_fails_loudly(self):
        with _clean_env({"AGENT_SANDBOX_TMPFS_SIZE": "   "}):
            with self.assertRaises(SandboxConfigError):
                get_sandbox_runner()

    def test_tmpfs_cap_and_cycle_label_reach_exec_argv(self):
        # FR-7: the cycle token threads through the factory into the
        # container's ownership labels, and the scratch-space cap into the
        # tmpfs mount — the two things the startup sweep and the cap-hit
        # classifier depend on.
        captured = {}
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "image"), _completed(0)),
            (lambda cmd: _is_docker(cmd, "run"), _completed(0)),
        ]

        def _capture(cmd, **kwargs):
            if _is_docker(cmd, "run"):
                captured["cmd"] = list(cmd)
            return _scripted_run(script)(cmd, **kwargs)

        with _clean_env({"AGENT_SANDBOX_TMPFS_SIZE": "1g"}):
            with patch("orchestrator.sandbox.subprocess.run", side_effect=_capture):
                runner = get_sandbox_runner(cycle_token="issue-164")
                runner.create(Path("/tmp/whatever-workspace"))
                runner.exec(["true"], timeout=10)
                runner.destroy()
        argv = captured["cmd"]
        self.assertEqual(argv[argv.index("--tmpfs") + 1], "/tmp:rw,size=1g,mode=1777")
        labels = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--label"]
        self.assertEqual(
            labels,
            [
                f"{SANDBOX_MANAGED_LABEL}=1",
                f"{SANDBOX_CYCLE_LABEL}=issue-164",
            ],
        )


class TestResolveSandboxRuntime(unittest.TestCase):
    def test_default_runtime_is_runsc(self):
        # FR-4: gVisor is the default and a hard requirement — the
        # preflight fails closed when it is unavailable.
        with _clean_env({}):
            self.assertEqual(resolve_sandbox_runtime(), "runsc")

    def test_explicit_runsc_is_selected(self):
        with _clean_env({"AGENT_SANDBOX_RUNTIME": " RunSC "}):
            self.assertEqual(resolve_sandbox_runtime(), DEFAULT_SANDBOX_RUNTIME)

    def test_runc_override_is_explicit_and_warned(self):
        # The dev-only override is honored but loud: a warning names the
        # override value and the shared-kernel trade-off.
        with _clean_env({"AGENT_SANDBOX_RUNTIME": "runc"}):
            with self.assertLogs("orchestrator.sandbox", level="WARNING") as logs:
                resolved = resolve_sandbox_runtime()
        self.assertEqual(resolved, SANDBOX_RUNTIME_OVERRIDE)
        warning_text = "\n".join(logs.output)
        assert SANDBOX_RUNTIME_OVERRIDE in warning_text
        assert "gVisor" in warning_text

    def test_unknown_runtime_fails_closed(self):
        with _clean_env({"AGENT_SANDBOX_RUNTIME": "kata"}):
            with self.assertRaises(SandboxConfigError):
                resolve_sandbox_runtime()

    def test_blank_runtime_fails_closed(self):
        # A blank explicit value signals a broken deploy script: loud
        # configuration error, never a silent fallback to the default.
        with _clean_env({"AGENT_SANDBOX_RUNTIME": "   "}):
            with self.assertRaises(SandboxConfigError):
                resolve_sandbox_runtime()

    def test_default_runtime_reaches_exec_argv(self):
        # "Sandboxes start with --runtime=runsc" (FR-4 acceptance), verified
        # behaviorally through the public runner path.
        argv = self._exec_with_runtime_env({})
        assert argv[argv.index("--runtime") + 1] == DEFAULT_SANDBOX_RUNTIME

    def test_runc_override_reaches_exec_argv(self):
        argv = self._exec_with_runtime_env({"AGENT_SANDBOX_RUNTIME": "runc"})
        assert argv[argv.index("--runtime") + 1] == SANDBOX_RUNTIME_OVERRIDE

    def _exec_with_runtime_env(self, extra_env):
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

        with _clean_env(extra_env):
            with patch("orchestrator.sandbox.subprocess.run", side_effect=_capture):
                runner = get_sandbox_runner()
                runner.create(Path("/tmp/whatever-workspace"))
                runner.exec(["make", "verify"], timeout=10)
                runner.destroy()
        return captured["cmd"]


class TestResolveSandboxEgress(unittest.TestCase):
    """FR-5 config resolution (issue #162 / ADR-0060): loud validation."""

    def test_defaults_derive_proxy_url_from_subnet_gateway(self):
        with _clean_env({}):
            egress = resolve_sandbox_egress()
        self.assertEqual(egress.network, "agdc-sandbox-egress")
        self.assertEqual(str(egress.subnet), "172.30.0.0/24")
        self.assertEqual(egress.gateway, ipaddress.ip_address("172.30.0.1"))
        self.assertEqual(egress.proxy_url, "http://172.30.0.1:3128")

    def test_explicit_network_and_subnet_are_honored(self):
        with _clean_env(
            {
                "AGENT_SANDBOX_EGRESS_NETWORK": " my-egress-net ",
                "AGENT_SANDBOX_EGRESS_SUBNET": "192.168.77.0/24",
            }
        ):
            egress = resolve_sandbox_egress()
        self.assertEqual(egress.network, "my-egress-net")
        self.assertEqual(egress.gateway, ipaddress.ip_address("192.168.77.1"))
        self.assertEqual(egress.proxy_url, "http://192.168.77.1:3128")

    def test_explicit_proxy_url_overrides_derivation(self):
        with _clean_env(
            {"AGENT_SANDBOX_EGRESS_PROXY_URL": "http://192.168.77.254:9999"}
        ):
            egress = resolve_sandbox_egress()
        self.assertEqual(egress.proxy_url, "http://192.168.77.254:9999")

    def test_blank_network_fails_closed(self):
        with _clean_env({"AGENT_SANDBOX_EGRESS_NETWORK": "  "}):
            with self.assertRaises(SandboxConfigError) as ctx:
                resolve_sandbox_egress()
        assert "no egress-disabled mode" in str(ctx.exception)

    def test_unparseable_subnet_fails_closed(self):
        with _clean_env({"AGENT_SANDBOX_EGRESS_SUBNET": "not-a-subnet"}):
            with self.assertRaises(SandboxConfigError):
                resolve_sandbox_egress()

    def test_public_subnet_fails_closed(self):
        # A public bridge subnet would defeat the egress boundary entirely.
        with _clean_env({"AGENT_SANDBOX_EGRESS_SUBNET": "1.2.3.0/24"}):
            with self.assertRaises(SandboxConfigError) as ctx:
                resolve_sandbox_egress()
        assert "private" in str(ctx.exception)

    def test_ipv6_subnet_fails_closed(self):
        # The egress network is created without IPv6; the ruleset polices
        # the IPv4 subnet only — an IPv6 subnet cannot be enforced.
        with _clean_env({"AGENT_SANDBOX_EGRESS_SUBNET": "fd00::/64"}):
            with self.assertRaises(SandboxConfigError) as ctx:
                resolve_sandbox_egress()
        assert "IPv4" in str(ctx.exception)

    def test_blank_proxy_url_fails_closed(self):
        with _clean_env({"AGENT_SANDBOX_EGRESS_PROXY_URL": "   "}):
            with self.assertRaises(SandboxConfigError):
                resolve_sandbox_egress()

    def test_non_http_proxy_url_fails_closed(self):
        with _clean_env({"AGENT_SANDBOX_EGRESS_PROXY_URL": "socks5://x:1080"}):
            with self.assertRaises(SandboxConfigError) as ctx:
                resolve_sandbox_egress()
        assert "http://host:port" in str(ctx.exception)

    def test_gateway_without_usable_host_fails_closed(self):
        # Defensive edge (e.g. a /31-style network whose hosts() is empty):
        # the gateway derivation must fail LOUDLY, not return None.
        class _NoHostsNetwork(ipaddress.IPv4Network):
            def hosts(self):
                return iter(())

        config = SandboxEgressConfig(
            network="agdc-sandbox-egress",
            subnet=_NoHostsNetwork("172.30.0.0/24"),
            proxy_url="http://172.30.0.1:3128",
        )
        with self.assertRaises(SandboxConfigError) as ctx:
            config.gateway
        assert "no usable host address" in str(ctx.exception)

    def test_resolved_egress_reaches_exec_argv(self):
        # Behavioral: a custom subnet's derived gateway lands in the
        # sandbox env (no proxy-url override needed).
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

        with _clean_env({"AGENT_SANDBOX_EGRESS_SUBNET": "192.168.77.0/24"}):
            with patch("orchestrator.sandbox.subprocess.run", side_effect=_capture):
                runner = get_sandbox_runner()
                runner.create(Path("/tmp/whatever-workspace"))
                runner.exec(["true"], timeout=10)
                runner.destroy()
        argv = captured["cmd"]
        self.assertEqual(argv[argv.index("--network") + 1], "agdc-sandbox-egress")
        env_flags = [argv[i + 1] for i, flag in enumerate(argv) if flag == "-e"]
        assert "HTTPS_PROXY=http://192.168.77.1:3128" in env_flags


class TestSandboxCapHitClassification(unittest.TestCase):
    """FR-7 resource caps (issue #164): a cap hit is data, never infra."""

    def test_oom_kill_is_a_memory_cap_hit(self):
        # The authoritative memory signal is Docker's OOMKilled flag; exit
        # code 137 alone is not enough (an external docker kill looks the
        # same without an OOM kill).
        self.assertEqual(
            classify_cap_hit(137, {"Status": "exited", "OOMKilled": True}, ""),
            "memory",
        )

    def test_killed_without_oom_is_not_a_cap_hit(self):
        # An externally killed container (137, OOMKilled false) must not be
        # reported as a memory cap: a false positive misdirects the Worker.
        self.assertIsNone(
            classify_cap_hit(137, {"Status": "exited", "OOMKilled": False}, "Killed")
        )

    def test_no_space_left_on_device_is_a_disk_cap_hit(self):
        self.assertEqual(
            classify_cap_hit(
                1,
                {"Status": "exited", "OOMKilled": False},
                "OSError: No space left on device",
            ),
            "disk",
        )

    def test_fork_failure_is_a_pids_cap_hit(self):
        for marker in (
            "bash: fork: Resource temporarily unavailable",
            "unable to fork",
            "cannot fork",
            "failed to fork",
        ):
            with self.subTest(marker=marker):
                self.assertEqual(
                    classify_cap_hit(
                        1, {"Status": "exited", "OOMKilled": False}, marker
                    ),
                    "pids",
                )

    def test_fork_failure_is_a_pids_cap_hit_for_any_non_zero_exit_code(self):
        # Live evidence (pids-limited alpine container, 2026-09-21): the shell
        # reports the kernel's fork rejection with its OWN exit code —
        # "sh: can't fork: Resource temporarily unavailable", exit 2. Pinning
        # the exit code to 1/137 silently dropped a real cap hit.
        self.assertEqual(
            classify_cap_hit(
                2,
                {"Status": "exited", "OOMKilled": False},
                "sh: can't fork: Resource temporarily unavailable",
            ),
            "pids",
        )
        self.assertEqual(classify_cap_hit(143, None, "unable to fork"), "pids")

    def test_fork_marker_on_a_successful_exit_is_not_a_cap_hit(self):
        # Conservative guard: a command that reported a fork error but still
        # exited 0 ran to its own completion — not a cap hit.
        self.assertIsNone(classify_cap_hit(0, {"Status": "exited"}, "unable to fork"))
        self.assertIsNone(
            classify_cap_hit(
                0, {"Status": "exited"}, "warning: No space left on device, retried"
            )
        )

    def test_ordinary_failure_is_not_a_cap_hit(self):
        self.assertIsNone(
            classify_cap_hit(1, {"Status": "exited", "OOMKilled": False}, "FAIL\n")
        )

    def test_missing_container_state_is_not_a_cap_hit(self):
        self.assertIsNone(classify_cap_hit(1, None, "boom"))

    def test_every_cap_message_names_its_cap_and_a_remedy(self):
        expected = {
            "memory": "AGENT_SANDBOX_MEMORY",
            "pids": "AGENT_SANDBOX_PIDS_LIMIT",
            "disk": "AGENT_SANDBOX_TMPFS_SIZE",
        }
        for cap_hit, env_name in expected.items():
            with self.subTest(cap_hit=cap_hit):
                message = cap_hit_message(cap_hit)
                # The Worker needs the knob name (to ask for a bigger cap)
                # and a hint about what to change in the command itself.
                assert env_name in message
                assert "cap" in message

    def test_oom_kill_reaches_the_exec_result(self):
        # Behavioral check through the runner: the cap hit is attached to the
        # returned result (data), so call sites can turn it into actionable
        # feedback instead of an opaque exit code.
        workspace = Path("/tmp/whatever-workspace")
        runner = DockerSandboxRunner(
            image="verify-img", cpus="2", memory="4g", pids_limit="512"
        )
        script = [
            (lambda cmd: _is_docker(cmd, "info"), _completed(0, stdout=b"27.0.3\n")),
            (lambda cmd: _is_docker(cmd, "image"), _completed(0)),
            (lambda cmd: _is_docker(cmd, "run"), _completed(137, stdout=b"Killed\n")),
            (
                lambda cmd: _is_docker(cmd, "inspect"),
                _completed(
                    0,
                    stdout=json.dumps(
                        {"Status": "exited", "ExitCode": 137, "OOMKilled": True}
                    ).encode(),
                ),
            ),
        ]
        with patch(
            "orchestrator.sandbox.subprocess.run", side_effect=_scripted_run(script)
        ):
            runner.create(workspace)
            result = runner.exec(["make", "verify"], timeout=10)
        self.assertEqual(result.exit_code, 137)
        self.assertEqual(result.cap_hit, "memory")
        self.assertFalse(result.timed_out)


class TestSandboxContainerCleanup(unittest.TestCase):
    """FR-7 lifecycle mechanics (issue #164): no container outlives its exec."""

    def test_every_exec_outcome_removes_its_container(self):
        # Success, data exit, timeout and infrastructure failure all remove
        # the container before returning/raising: a crashed cycle leaves at
        # most one in-flight container for the startup sweep.
        outcomes = {
            "success": (
                [
                    (lambda cmd: _is_docker(cmd, "run"), _completed(0, stdout=b"ok")),
                ],
                "return",
            ),
            "data_exit": (
                [
                    (
                        lambda cmd: _is_docker(cmd, "run"),
                        _completed(1, stdout=b"FAIL\n"),
                    ),
                    (
                        lambda cmd: _is_docker(cmd, "inspect"),
                        _completed(0, stdout=b'{"Status": "exited", "ExitCode": 1}'),
                    ),
                ],
                "return",
            ),
            "timeout": (
                [
                    (
                        lambda cmd: _is_docker(cmd, "run"),
                        subprocess.TimeoutExpired(cmd="docker run", timeout=1),
                    ),
                ],
                "return",
            ),
            "infrastructure": (
                [
                    (lambda cmd: _is_docker(cmd, "run"), _completed(1, stdout=b"boom")),
                    (lambda cmd: _is_docker(cmd, "inspect"), _completed(1)),
                ],
                "raise",
            ),
        }
        for name, (exec_rules, expectation) in outcomes.items():
            with self.subTest(outcome=name):
                removals: list[list[str]] = []
                script = [
                    (
                        lambda cmd: _is_docker(cmd, "info"),
                        _completed(0, stdout=b"27.0.3\n"),
                    ),
                    (lambda cmd: _is_docker(cmd, "image"), _completed(0)),
                    *exec_rules,
                ]
                runner = DockerSandboxRunner(
                    image="verify-img", cpus="2", memory="4g", pids_limit="512"
                )
                with patch(
                    "orchestrator.sandbox.subprocess.run",
                    side_effect=_scripted_run(script, removals=removals),
                ):
                    runner.create(Path("/tmp/whatever-workspace"))
                    if expectation == "raise":
                        with self.assertRaises(SandboxError):
                            runner.exec(["make", "verify"], timeout=1)
                    else:
                        runner.exec(["make", "verify"], timeout=1)
                    # destroy() has nothing left to do: the exec already
                    # removed its container.
                    runner.destroy()
                self.assertEqual(len(removals), 1)
                self.assertEqual(removals[0][:3], ["docker", "rm", "-f"])

    def test_removal_retries_once_then_reports_failure(self):
        attempts = []

        def _run(cmd, **kwargs):
            attempts.append(list(cmd))
            return _completed(1, stderr=b"permission denied")

        with patch("orchestrator.sandbox.subprocess.run", side_effect=_run):
            with self.assertLogs("orchestrator.sandbox", level="ERROR") as logs:
                removed = remove_sandbox_container("agdc-sandbox-deadbeef")
        self.assertFalse(removed)
        self.assertEqual(len(attempts), 2)
        error_text = "\n".join(logs.output)
        # The operator needs the container name and the manual command.
        assert "agdc-sandbox-deadbeef" in error_text
        assert "docker rm -f agdc-sandbox-deadbeef" in error_text
        assert "permission denied" in error_text

    def test_removal_succeeds_on_retry(self):
        attempts = []

        def _run(cmd, **kwargs):
            attempts.append(list(cmd))
            if len(attempts) == 1:
                return _completed(1, stderr=b"temporary failure")
            return _completed(0)

        with patch("orchestrator.sandbox.subprocess.run", side_effect=_run):
            removed = remove_sandbox_container("agdc-sandbox-retry")
        self.assertTrue(removed)
        self.assertEqual(len(attempts), 2)

    def test_removal_survives_an_unavailable_cli(self):
        with patch(
            "orchestrator.sandbox.subprocess.run",
            side_effect=OSError("docker gone"),
        ):
            with self.assertLogs("orchestrator.sandbox", level="ERROR"):
                removed = remove_sandbox_container("agdc-sandbox-gone")
        self.assertFalse(removed)

    def test_removal_of_an_absent_container_is_reported_as_failed(self):
        with patch(
            "orchestrator.sandbox.subprocess.run",
            side_effect=lambda cmd, **kwargs: _completed(
                1, stderr=b"Error: No such container: agdc-sandbox-old"
            ),
        ):
            # A non-zero docker exit is a failure: the sweep must not claim a
            # removal it cannot prove, but it must not raise either.
            self.assertFalse(remove_sandbox_container("agdc-sandbox-old"))

    def test_managed_container_listing_parses_names_and_cycles(self):
        listing = (
            b"agdc-sandbox-aaaa1111 issue-164\n"
            b"agdc-sandbox-bbbb2222 issue-163\n"
            b"agdc-sandbox-cccc3333 \n"
            b"\n"
        )
        captured = {}

        def _run(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return _completed(0, stdout=listing)

        with patch("orchestrator.sandbox.subprocess.run", side_effect=_run):
            containers = list_managed_sandbox_containers()
        self.assertEqual(
            containers,
            [
                ("agdc-sandbox-aaaa1111", "issue-164"),
                ("agdc-sandbox-bbbb2222", "issue-163"),
                ("agdc-sandbox-cccc3333", ""),
            ],
        )
        # The filter is the ownership marker: unmanaged containers are never
        # candidates for the sweep.
        listing_cmd = " ".join(captured["cmd"])
        assert f"label={SANDBOX_MANAGED_LABEL}=1" in listing_cmd
        assert f'{{{{.Label "{SANDBOX_CYCLE_LABEL}"}}}}' in listing_cmd

    def test_managed_container_listing_fails_loudly_on_daemon_failure(self):
        with patch(
            "orchestrator.sandbox.subprocess.run",
            side_effect=lambda cmd, **kwargs: _completed(1, stderr=b"cannot connect"),
        ):
            with self.assertRaises(SandboxUnavailableError) as ctx:
                list_managed_sandbox_containers()
        assert "cannot connect" in str(ctx.exception)

    def test_managed_container_listing_fails_loudly_on_missing_cli(self):
        with patch(
            "orchestrator.sandbox.subprocess.run",
            side_effect=OSError("docker gone"),
        ):
            with self.assertRaises(SandboxUnavailableError):
                list_managed_sandbox_containers()


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
