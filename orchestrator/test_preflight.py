"""Unit tests for the Execution Sandbox runtime preflight (issue #161).

Covers the FR-4 fail-closed contract: every unusable-runtime condition
(daemon down, kernel too old, binary missing, runtime not registered,
unparseable daemon response) is a loud error with an actionable message;
the explicit ``runc`` dev override skips gVisor-specific checks and warns;
inside the control-plane container the host-side binary check is skipped
(the daemon-side registration is the authoritative signal). All docker
interactions are scripted fakes — no Docker daemon is required.
"""

import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from orchestrator import preflight as preflight_module
from orchestrator.preflight import (
    CONTAINER_MARKER_PATHS,
    MINIMUM_KERNEL_VERSION,
    SandboxConfigError,
    SandboxError,
    SandboxUnavailableError,
    main,
    run_preflight,
    running_in_control_plane_container,
)

_RUNTIMES_FORMAT = "{{json .Runtimes}}"
_VERSION_FORMAT = "{{.ServerVersion}}"

_RUNTIMES_WITH_RUNSC = '{"runc": {"path": "runc"}, "runsc": {"path": "/usr/bin/runsc"}}'
_RUNTIMES_WITHOUT_RUNSC = '{"runc": {"path": "runc"}}'


def _completed(returncode=0, stdout=b"", stderr=b""):
    res = unittest.mock.MagicMock()
    res.returncode = returncode
    res.stdout = stdout
    res.stderr = stderr
    return res


def _scripted_run(script):
    """First matching ``(predicate, outcome)`` rule serves; else fail loudly."""

    def _run(cmd, **kwargs):
        for predicate, outcome in script:
            if predicate(cmd):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"Unexpected subprocess command: {cmd}")

    return _run


def _is_docker_info(cmd, fmt):
    return (
        len(cmd) == 4
        and cmd[0] == "docker"
        and cmd[1] == "info"
        and cmd[2] == "--format"
        and cmd[3] == fmt
    )


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


def _runsc_happy_script():
    return [
        (
            lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
            _completed(0, stdout=b"27.0.3\n"),
        ),
        (
            lambda cmd: _is_docker_info(cmd, _RUNTIMES_FORMAT),
            _completed(0, stdout=_RUNTIMES_WITH_RUNSC.encode()),
        ),
    ]


class PreflightTestCase(unittest.TestCase):
    """Shared fakes: a healthy host (kernel 6.8, runsc installed+registered)."""

    def setUp(self):
        self._patches = [
            patch(
                "orchestrator.preflight.platform.release",
                return_value="6.8.0-1027-oracle",
            ),
            patch(
                "orchestrator.preflight.shutil.which",
                return_value="/usr/bin/runsc",
            ),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def assert_preflight_passes(self, script):
        with _clean_env({}):
            with patch(
                "orchestrator.preflight.subprocess.run",
                side_effect=_scripted_run(script),
            ):
                runtime = run_preflight()
        self.assertEqual(runtime, "runsc")

    def assert_preflight_fails(self, script, message_parts):
        with _clean_env({}):
            with patch(
                "orchestrator.preflight.subprocess.run",
                side_effect=_scripted_run(script),
            ):
                with self.assertRaises(SandboxError) as ctx:
                    run_preflight()
        message = str(ctx.exception)
        for part in message_parts:
            assert part in message
        return message


class TestRunPreflightRunscMode(PreflightTestCase):
    def test_healthy_host_passes(self):
        self.assert_preflight_passes(_runsc_happy_script())

    def test_daemon_unreachable_fails_closed(self):
        self.assert_preflight_fails(
            [
                (
                    lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                    _completed(1, stderr=b"Cannot connect to the Docker daemon"),
                )
            ],
            ["daemon unreachable"],
        )

    def test_docker_cli_missing_fails_closed(self):
        self.assert_preflight_fails(
            [
                (
                    lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                    OSError("No such file or directory: 'docker'"),
                )
            ],
            ["docker daemon probe failed"],
        )

    def test_kernel_below_5_6_fails_closed(self):
        # The kernel check precedes the runtimes probe: only the daemon
        # version probe should run before the failure.
        with patch(
            "orchestrator.preflight.platform.release",
            return_value="5.4.0-42-generic",
        ):
            self.assert_preflight_fails(
                [
                    (
                        lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                        _completed(0, stdout=b"27.0.3\n"),
                    )
                ],
                ["5.6", "5.4.0-42-generic"],
            )

    def test_kernel_minimum_constant_matches_gvisor_requirement(self):
        self.assertEqual(MINIMUM_KERNEL_VERSION, (5, 6))

    def test_kernel_release_without_version_dot_fails_closed(self):
        with patch(
            "orchestrator.preflight.platform.release",
            return_value="solos-kernel",
        ):
            self.assert_preflight_fails(
                [
                    (
                        lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                        _completed(0, stdout=b"27.0.3\n"),
                    )
                ],
                ["Cannot determine the host kernel version"],
            )

    def test_unparseable_kernel_release_fails_closed(self):
        with patch(
            "orchestrator.preflight.platform.release",
            return_value="custom.kernel",
        ):
            self.assert_preflight_fails(
                [
                    (
                        lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                        _completed(0, stdout=b"27.0.3\n"),
                    )
                ],
                ["Cannot parse the host kernel version"],
            )

    def test_runsc_binary_missing_fails_closed(self):
        with patch("orchestrator.preflight.shutil.which", return_value=None):
            self.assert_preflight_fails(
                _runsc_happy_script(),
                ["runsc binary not found", "docs/runbook-sandbox-runtime.md"],
            )

    def test_binary_installed_but_not_registered_fails_closed(self):
        # The issue's edge case: binary present, daemon not restarted —
        # the registration check catches it with the registration remedy.
        self.assert_preflight_fails(
            [
                (
                    lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                    _completed(0, stdout=b"27.0.3\n"),
                ),
                (
                    lambda cmd: _is_docker_info(cmd, _RUNTIMES_FORMAT),
                    _completed(0, stdout=_RUNTIMES_WITHOUT_RUNSC.encode()),
                ),
            ],
            ["not registered", "runsc install", "restart"],
        )

    def test_registration_failure_lists_registered_runtimes(self):
        self.assert_preflight_fails(
            [
                (
                    lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                    _completed(0, stdout=b"27.0.3\n"),
                ),
                (
                    lambda cmd: _is_docker_info(cmd, _RUNTIMES_FORMAT),
                    _completed(0, stdout=_RUNTIMES_WITHOUT_RUNSC.encode()),
                ),
            ],
            ["runc"],
        )

    def test_unparseable_runtimes_listing_fails_closed(self):
        self.assert_preflight_fails(
            [
                (
                    lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                    _completed(0, stdout=b"27.0.3\n"),
                ),
                (
                    lambda cmd: _is_docker_info(cmd, _RUNTIMES_FORMAT),
                    _completed(0, stdout=b"not-json"),
                ),
            ],
            ["unparseable runtimes listing"],
        )

    def test_runtimes_probe_failure_is_unavailable(self):
        self.assert_preflight_fails(
            [
                (
                    lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                    _completed(0, stdout=b"27.0.3\n"),
                ),
                (
                    lambda cmd: _is_docker_info(cmd, _RUNTIMES_FORMAT),
                    _completed(1, stderr=b"daemon gone"),
                ),
            ],
            ["daemon unreachable while listing runtimes"],
        )

    def test_runtimes_probe_oserror_is_unavailable(self):
        # The docker CLI can vanish (or the socket can be revoked) between
        # the two daemon probes: the runtimes probe's OSError path is its
        # own loud failure, not a crash.
        self.assert_preflight_fails(
            [
                (
                    lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                    _completed(0, stdout=b"27.0.3\n"),
                ),
                (
                    lambda cmd: _is_docker_info(cmd, _RUNTIMES_FORMAT),
                    OSError("docker CLI disappeared"),
                ),
            ],
            ["docker daemon probe failed"],
        )

    def test_non_dict_runtimes_listing_fails_closed(self):
        # A JSON array (not an object) is a daemon anomaly: fail closed.
        self.assert_preflight_fails(
            [
                (
                    lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                    _completed(0, stdout=b"27.0.3\n"),
                ),
                (
                    lambda cmd: _is_docker_info(cmd, _RUNTIMES_FORMAT),
                    _completed(0, stdout=b'["runc"]'),
                ),
            ],
            ["unexpected runtimes listing"],
        )


class TestRunPreflightContainerContext(PreflightTestCase):
    def test_binary_check_skipped_inside_container(self):
        # In the control-plane container the host's /usr/bin is not visible:
        # a missing local binary must NOT fail the preflight when the daemon
        # reports runsc as registered (the authoritative signal).
        def _which_raises(name):
            raise AssertionError("which() must not be called inside a container")

        with patch(
            "orchestrator.preflight.running_in_control_plane_container",
            return_value=True,
        ):
            with patch(
                "orchestrator.preflight.shutil.which",
                side_effect=_which_raises,
            ):
                self.assert_preflight_passes(_runsc_happy_script())


class TestRunPreflightRuncOverride(PreflightTestCase):
    def test_override_passes_without_gvisor_checks(self):
        # runc mode: daemon reachability only — no kernel check (gVisor's
        # 5.6 floor does not apply) and no runtimes probe.
        def _release_raises():
            raise AssertionError("kernel check must be skipped in runc mode")

        with _clean_env({"AGENT_SANDBOX_RUNTIME": "runc"}):
            with patch(
                "orchestrator.preflight.platform.release",
                side_effect=_release_raises,
            ):
                with patch(
                    "orchestrator.preflight.subprocess.run",
                    side_effect=_scripted_run(
                        [
                            (
                                lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                                _completed(0, stdout=b"27.0.3\n"),
                            )
                        ]
                    ),
                ):
                    with self.assertLogs(
                        "orchestrator.preflight", level="WARNING"
                    ) as logs:
                        runtime = run_preflight()
        self.assertEqual(runtime, "runc")
        warning_text = "\n".join(logs.output)
        assert "runc" in warning_text
        assert "gVisor" in warning_text


class TestRunningInControlPlaneContainer(unittest.TestCase):
    def test_marker_file_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / ".dockerenv"
            marker.write_text("")
            with patch.object(
                preflight_module,
                "CONTAINER_MARKER_PATHS",
                (str(marker),),
            ):
                self.assertTrue(running_in_control_plane_container())

    def test_no_markers(self):
        with patch.object(
            preflight_module,
            "CONTAINER_MARKER_PATHS",
            (),
        ):
            self.assertFalse(running_in_control_plane_container())

    def test_default_markers_are_container_conventions(self):
        self.assertEqual(CONTAINER_MARKER_PATHS, ("/.dockerenv", "/run/.containerenv"))


class TestPreflightMain(unittest.TestCase):
    def test_success_exits_zero(self):
        with patch("orchestrator.preflight.run_preflight", return_value="runsc"):
            self.assertEqual(main(), 0)

    def test_runtime_failure_exits_one(self):
        with patch(
            "orchestrator.preflight.run_preflight",
            side_effect=SandboxConfigError("runsc not registered"),
        ):
            with self.assertLogs("orchestrator.preflight", level="ERROR") as logs:
                self.assertEqual(main(), 1)
        assert "runsc not registered" in "\n".join(logs.output)

    def test_infrastructure_failure_exits_one(self):
        with patch(
            "orchestrator.preflight.run_preflight",
            side_effect=SandboxUnavailableError("docker daemon unreachable"),
        ):
            self.assertEqual(main(), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
