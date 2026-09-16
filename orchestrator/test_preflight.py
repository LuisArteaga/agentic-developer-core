"""Unit tests for the Execution Sandbox runtime preflight (issue #161).

Covers the FR-4 fail-closed contract: every unusable-runtime condition
(daemon down, kernel too old, binary missing, runtime not registered,
unparseable daemon response) is a loud error with an actionable message;
the explicit ``runc`` dev override skips gVisor-specific checks and warns;
inside the control-plane container the host-side binary check is skipped
(the daemon-side registration is the authoritative signal). All docker
interactions are scripted fakes — no Docker daemon is required.
"""

import io
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
import unittest
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from orchestrator import preflight as preflight_module
from orchestrator.preflight import (
    CONTAINER_MARKER_PATHS,
    MINIMUM_KERNEL_VERSION,
    REQUIRED_PAT_SCOPE_PROBES,
    SandboxConfigError,
    SandboxError,
    SandboxUnavailableError,
    TokenScopeError,
    check_pat_scopes,
    describe_token_type,
    main,
    resolve_scope_target_repo,
    run_preflight,
    running_in_control_plane_container,
)

_RUNTIMES_FORMAT = "{{json .Runtimes}}"
_VERSION_FORMAT = "{{.ServerVersion}}"
_IPAM_FORMAT = "{{json .IPAM.Config}}"

_RUNTIMES_WITH_RUNSC = '{"runc": {"path": "runc"}, "runsc": {"path": "/usr/bin/runsc"}}'
_RUNTIMES_WITHOUT_RUNSC = '{"runc": {"path": "runc"}}'

# The egress network inspection every run_preflight() performs (FR-5): a
# healthy daemon answers with the default egress subnet. Appended to every
# scripted daemon run below so the runtime checks' failure scenarios keep
# their original ordering semantics.
_EGRESS_IPAM_OK = b'[{"Subnet": "172.30.0.0/24", "Gateway": "172.30.0.1"}]'


def _completed(returncode=0, stdout=b"", stderr=b""):
    res = unittest.mock.MagicMock()
    res.returncode = returncode
    res.stdout = stdout
    res.stderr = stderr
    return res


# The egress network inspection every run_preflight() performs (FR-5): a
# healthy daemon answers with the default egress subnet. Appended to every
# scripted daemon run below so the runtime checks' failure scenarios keep
# their original ordering semantics.
_EGRESS_INSPECT_RULE = (
    lambda cmd: (
        len(cmd) > 3
        and cmd[0] == "docker"
        and cmd[1] == "network"
        and cmd[2] == "inspect"
    ),
    _completed(0, stdout=_EGRESS_IPAM_OK),
)


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


class _FakeApiResponse:
    """Context-manager stand-in for a urllib HTTPResponse (200 path)."""

    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = body
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(status, headers=None, body=b'{"message": "refused"}'):
    """Build a real HTTPError (resolvable .headers/.read) for refusal routes."""
    hdrs = Message()
    for key, value in (headers or {}).items():
        hdrs[key] = value
    return urllib.error.HTTPError(
        "https://api.github.com/repos/owner/repo",
        status,
        "error",
        hdrs,
        io.BytesIO(body),
    )


_REPO_BODY = b'{"default_branch": "main"}'
_COMMIT_BODY = b'{"sha": "abc123def"}'


def _healthy_routes():
    """(url_fragment, outcome) rules answering every FR-6 probe with 200.

    Ordering matters: the first matching fragment serves, so the more
    specific paths (check-runs, commits, issues, pulls) must precede the
    bare repo path.
    """
    return [
        (
            "check-runs",
            _FakeApiResponse(body=b'{"check_runs": []}'),
        ),
        ("/repos/owner/repo/commits/main", _FakeApiResponse(body=_COMMIT_BODY)),
        ("/repos/owner/repo/issues", _FakeApiResponse(body=b"[]")),
        ("/repos/owner/repo/pulls", _FakeApiResponse(body=b"[]")),
        ("/repos/owner/repo", _FakeApiResponse(body=_REPO_BODY)),
    ]


def _urlopen_script(routes):
    """Build a urlopen stand-in from (url_fragment, outcome) rules.

    Records every requested URL; the first matching rule serves its outcome
    (a ``_FakeApiResponse``, an ``HTTPError``, or an exception instance);
    an unmatched URL fails the test loudly so unexpected API calls surface.
    """
    calls = []

    def _open(request, timeout=None):
        url = request.full_url
        calls.append(url)
        for fragment, outcome in routes:
            if fragment in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"Unexpected GitHub API call: {url}")

    return _open, calls


class _BrokenBytesIO(io.BytesIO):
    """Byte stream whose read fails mid-transfer (truncated error body)."""

    def read(self, size=-1):
        raise OSError("connection reset while reading the error body")


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
    """Shared fakes: a healthy host (kernel 6.8, runsc installed+registered,
    egress network present, egress proxy reachable)."""

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
            # FR-5: the egress proxy TCP probe succeeds by default.
            patch(
                "orchestrator.preflight.socket.create_connection",
                return_value=unittest.mock.MagicMock(),
            ),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _script_with_egress(script):
        return [*script, _EGRESS_INSPECT_RULE]

    def assert_preflight_passes(self, script):
        with _clean_env({}):
            with patch(
                "orchestrator.preflight.subprocess.run",
                side_effect=_scripted_run(self._script_with_egress(script)),
            ):
                runtime = run_preflight()
        self.assertEqual(runtime, "runsc")

    def assert_preflight_fails(self, script, message_parts):
        with _clean_env({}):
            with patch(
                "orchestrator.preflight.subprocess.run",
                side_effect=_scripted_run(self._script_with_egress(script)),
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
        # runc mode: daemon reachability, egress enforcement (FR-5 runs in
        # both modes), and the loud shared-kernel warning — no kernel check
        # (gVisor's 5.6 floor does not apply) and no runtimes probe.
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
                        self._script_with_egress(
                            [
                                (
                                    lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                                    _completed(0, stdout=b"27.0.3\n"),
                                )
                            ]
                        )
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


class TestEgressPreflight(PreflightTestCase):
    """FR-5 fail-closed checks (issue #162): egress network + proxy probe."""

    def test_missing_egress_network_fails_closed(self):
        with _clean_env({}):
            with patch(
                "orchestrator.preflight.subprocess.run",
                side_effect=_scripted_run(
                    [
                        (
                            lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                            _completed(0, stdout=b"27.0.3\n"),
                        ),
                        (
                            lambda cmd: (
                                len(cmd) > 3
                                and cmd[0] == "docker"
                                and cmd[1] == "network"
                                and cmd[2] == "inspect"
                            ),
                            _completed(1, stderr=b"No such network"),
                        ),
                    ]
                ),
            ):
                with self.assertRaises(SandboxUnavailableError) as ctx:
                    run_preflight()
        message = str(ctx.exception)
        assert "agdc-sandbox-egress" in message
        assert "egress_setup.py network" in message

    def test_subnet_drift_fails_closed(self):
        # The ruleset polices the CONFIGURED subnet only: a live network on a
        # different subnet would silently void the lockdown — loud error.
        with _clean_env({}):
            with patch(
                "orchestrator.preflight.subprocess.run",
                side_effect=_scripted_run(
                    [
                        (
                            lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                            _completed(0, stdout=b"27.0.3\n"),
                        ),
                        (
                            lambda cmd: (
                                len(cmd) > 3
                                and cmd[0] == "docker"
                                and cmd[1] == "network"
                                and cmd[2] == "inspect"
                            ),
                            _completed(
                                0,
                                stdout=b'[{"Subnet": "10.99.0.0/24"}]',
                            ),
                        ),
                    ]
                ),
            ):
                with self.assertRaises(SandboxConfigError) as ctx:
                    run_preflight()
        message = str(ctx.exception)
        assert "drifted" in message
        assert "172.30.0.0/24" in message

    def test_unparseable_ipam_listing_fails_closed(self):
        with _clean_env({}):
            with patch(
                "orchestrator.preflight.subprocess.run",
                side_effect=_scripted_run(
                    [
                        (
                            lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                            _completed(0, stdout=b"27.0.3\n"),
                        ),
                        (
                            lambda cmd: (
                                len(cmd) > 3
                                and cmd[0] == "docker"
                                and cmd[1] == "network"
                                and cmd[2] == "inspect"
                            ),
                            _completed(0, stdout=b"not-json"),
                        ),
                    ]
                ),
            ):
                with self.assertRaises(SandboxConfigError) as ctx:
                    run_preflight()
        assert "unparseable IPAM" in str(ctx.exception)

    def test_unreachable_egress_proxy_fails_closed(self):
        with _clean_env({}):
            with patch(
                "orchestrator.preflight.socket.create_connection",
                side_effect=OSError("connection refused"),
            ):
                with patch(
                    "orchestrator.preflight.subprocess.run",
                    side_effect=_scripted_run(
                        [
                            (
                                lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                                _completed(0, stdout=b"27.0.3\n"),
                            ),
                            _EGRESS_INSPECT_RULE,
                        ]
                    ),
                ):
                    with self.assertRaises(SandboxUnavailableError) as ctx:
                        run_preflight()
        message = str(ctx.exception)
        assert "Egress proxy unreachable" in message
        assert "127.0.0.1:3128" in message

    def test_invalid_probe_url_fails_closed(self):
        with _clean_env({"AGENT_EGRESS_PROXY_PROBE_URL": "ftp://x"}):
            with patch(
                "orchestrator.preflight.subprocess.run",
                side_effect=_scripted_run(
                    [
                        (
                            lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                            _completed(0, stdout=b"27.0.3\n"),
                        ),
                        _EGRESS_INSPECT_RULE,
                    ]
                ),
            ):
                with self.assertRaises(SandboxConfigError) as ctx:
                    run_preflight()
        assert "AGENT_EGRESS_PROXY_PROBE_URL" in str(ctx.exception)

    def test_inspect_subprocess_error_is_unavailable(self):
        # A docker binary present but broken (OSError/timeout) is
        # infrastructure unavailability, not a config error — fail closed.
        with _clean_env({}):
            with patch(
                "orchestrator.preflight.subprocess.run",
                side_effect=_scripted_run(
                    [
                        (
                            lambda cmd: _is_docker_info(cmd, _VERSION_FORMAT),
                            _completed(0, stdout=b"27.0.3\n"),
                        ),
                        (
                            lambda cmd: (
                                len(cmd) > 3
                                and cmd[0] == "docker"
                                and cmd[1] == "network"
                                and cmd[2] == "inspect"
                            ),
                            OSError("docker binary vanished"),
                        ),
                    ]
                ),
            ):
                with self.assertRaises(SandboxUnavailableError) as ctx:
                    run_preflight()
        assert "docker network inspect failed" in str(ctx.exception)


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


class TestPreflightMainScopesOnly(unittest.TestCase):
    """``--scopes-only`` routes main() to the FR-6 credential check only."""

    def test_scopes_only_runs_scope_check_and_skips_runtime_checks(self):
        with (
            patch("orchestrator.preflight.check_pat_scopes") as mock_scopes,
            patch("orchestrator.preflight.subprocess.run") as mock_run,
        ):
            self.assertEqual(main(["--scopes-only"]), 0)
        mock_scopes.assert_called_once()
        mock_run.assert_not_called()

    def test_scopes_only_failure_exits_one(self):
        with patch(
            "orchestrator.preflight.check_pat_scopes",
            side_effect=TokenScopeError("missing Checks (read)"),
        ):
            with self.assertLogs("orchestrator.preflight", level="ERROR") as logs:
                self.assertEqual(main(["--scopes-only"]), 1)
        assert "missing Checks (read)" in "\n".join(logs.output)
        assert "scopes-only" in "\n".join(logs.output)


class TestDescribeTokenType(unittest.TestCase):
    """Contract tests for the informational token classification (FR-6).

    The classification is log-only (never gating): it makes the migration
    state observable — fine-grained vs classic, plus the classic token's
    observed ``x-oauth-scopes``.
    """

    def test_prefixes_map_to_token_classes(self):
        self.assertEqual(
            describe_token_type("github_pat_XYZ", None), "fine-grained PAT"
        )
        self.assertEqual(
            describe_token_type("ghs_XYZ", None), "GitHub App installation token"
        )
        assert "unrecognized" in describe_token_type("weird_XYZ", None)

    def test_classic_token_reports_observed_scopes(self):
        headers = Message()
        headers["x-oauth-scopes"] = "repo, workflow"
        description = describe_token_type("ghp_XYZ", headers)
        assert "classic token" in description
        assert "repo, workflow" in description

    def test_classic_token_without_scopes_header(self):
        assert "classic token" in describe_token_type("gho_XYZ", None)


class TestResolveScopeTargetRepo(unittest.TestCase):
    """Contract tests for the scope check's target-repository resolution."""

    def test_env_takes_precedence(self):
        with _clean_env({"GITHUB_REPOSITORY": "env-owner/env-repo"}):
            self.assertEqual(
                resolve_scope_target_repo(Path("/nonexistent")),
                "env-owner/env-repo",
            )

    def test_falls_back_to_https_git_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/own/repo.git"],
                cwd=repo_dir,
                check=True,
            )
            with _clean_env({}):
                self.assertEqual(resolve_scope_target_repo(repo_dir), "own/repo")

    def test_falls_back_to_ssh_git_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
            subprocess.run(
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    "git@github.com:ssh-own/ssh-repo.git",
                ],
                cwd=repo_dir,
                check=True,
            )
            with _clean_env({}):
                self.assertEqual(
                    resolve_scope_target_repo(repo_dir), "ssh-own/ssh-repo"
                )

    def test_unresolvable_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _clean_env({}):
                self.assertIsNone(resolve_scope_target_repo(Path(tmp)))

    def test_non_github_remote_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://gitlab.com/own/repo.git"],
                cwd=repo_dir,
                check=True,
            )
            with _clean_env({}):
                self.assertIsNone(resolve_scope_target_repo(repo_dir))


class TestPatScopePreflight(PreflightTestCase):
    """FR-6 credential scoping inside the startup preflight (issue #163).

    Drives the public entry points (``run_preflight`` / ``check_pat_scopes``)
    with a scripted ``urllib.request.urlopen`` transport; docker probes use
    the shared happy-script harness so runtime checks pass and only the
    credential behavior varies.
    """

    def setUp(self):
        super().setUp()
        self._urlopen, self._calls = _urlopen_script(_healthy_routes())
        urlopen_patch = patch(
            "orchestrator.preflight.urllib.request.urlopen", self._urlopen
        )
        urlopen_patch.start()
        self.addCleanup(urlopen_patch.stop)
        run_patch = patch(
            "orchestrator.preflight.subprocess.run",
            side_effect=_scripted_run(self._script_with_egress(_runsc_happy_script())),
        )
        run_patch.start()
        self.addCleanup(run_patch.stop)

    @staticmethod
    def _token_env(token="github_pat_TESTTOKEN"):
        return {"GH_PAT": token, "GITHUB_REPOSITORY": "owner/repo"}

    def _healthy_routes_with(self, fragment, outcome):
        # Replace the rule IN PLACE: first-match routing means appending at
        # the end would be shadowed by the bare-repo catch-all rule.
        routes = _healthy_routes()
        for i, (frag, _) in enumerate(routes):
            if frag == fragment:
                routes[i] = (fragment, outcome)
                break
        return routes

    def _reroute(self, routes):
        self._urlopen, self._calls = _urlopen_script(routes)
        return patch("orchestrator.preflight.urllib.request.urlopen", self._urlopen)

    def test_healthy_fine_grained_token_passes_all_probes(self):
        with _clean_env(self._token_env()):
            runtime = run_preflight()
        self.assertEqual(runtime, "runsc")
        probed = [url for url in self._calls if "api.github.com" in url]
        self.assertEqual(len(probed), len(REQUIRED_PAT_SCOPE_PROBES))
        # The probe sequence follows the required matrix: repository
        # metadata first, check-run polling third (after branch and head-SHA
        # resolution), then the issue and PR listings.
        assert "/repos/owner/repo/commits/main/check-runs" not in probed[0]
        assert probed[0].endswith("/repos/owner/repo")
        assert "/commits/main" in probed[1]
        assert "check-runs" in probed[2]
        assert "/issues" in probed[3]
        assert "/pulls" in probed[4]

    def test_missing_checks_permission_fails_closed_with_known_gap_note(self):
        with _clean_env(self._token_env()):
            with self._reroute(
                self._healthy_routes_with(
                    "check-runs",
                    _http_error(
                        403, headers={"X-Accepted-GitHub-Permissions": "checks=read"}
                    ),
                )
            ):
                with self.assertRaises(TokenScopeError) as ctx:
                    run_preflight()
        message = str(ctx.exception)
        assert "Checks (read)" in message
        assert "checks=read" in message
        assert "#129512" in message
        assert "docs/runbook-pat-scoping.md" in message

    def test_missing_issues_permission_reports_gap(self):
        with _clean_env(self._token_env()):
            with self._reroute(
                self._healthy_routes_with("/repos/owner/repo/issues", _http_error(404))
            ):
                with self.assertRaises(TokenScopeError) as ctx:
                    run_preflight()
        message = str(ctx.exception)
        assert "Issues (read)" in message
        assert "HTTP 404" in message
        # All five probes run — gaps are collected, not fail-fast.
        probed = [url for url in self._calls if "api.github.com" in url]
        self.assertEqual(len(probed), len(REQUIRED_PAT_SCOPE_PROBES))

    def test_invalid_token_fails_immediately(self):
        with _clean_env(self._token_env()):
            with self._reroute(
                self._healthy_routes_with("/repos/owner/repo", _http_error(401))
            ):
                with self.assertRaises(TokenScopeError) as ctx:
                    run_preflight()
        message = str(ctx.exception)
        assert "invalid, expired, or revoked" in message
        assert self._calls == ["https://api.github.com/repos/owner/repo"]

    def test_prerequisite_gap_skips_downstream_probes(self):
        # A repo-metadata refusal leaves branch/SHA-dependent probes
        # unbuildable — their gap is already recorded, so no bogus
        # follow-up is issued; repo-independent probes still run.
        with _clean_env(self._token_env()):
            with self._reroute(
                self._healthy_routes_with("/repos/owner/repo", _http_error(403))
            ):
                with self.assertRaises(TokenScopeError) as ctx:
                    run_preflight()
        assert "Metadata (read)" in str(ctx.exception)
        probed = [url for url in self._calls if "api.github.com" in url]
        self.assertEqual(len(probed), 3)
        assert not any("/commits/" in url for url in probed)
        assert any("/issues" in url for url in probed)
        assert any("/pulls" in url for url in probed)

    def test_no_token_skips_validation(self):
        with _clean_env({}):
            runtime = run_preflight()
        self.assertEqual(runtime, "runsc")
        self.assertEqual(self._calls, [])

    def test_unresolvable_repo_skips_validation(self):
        remote_patch = patch(
            "orchestrator.preflight.get_remote_url",
            side_effect=Exception("not a git repository"),
        )
        env = {"GH_PAT": "github_pat_TESTTOKEN"}
        with _clean_env(env):
            with remote_patch:
                runtime = run_preflight()
        self.assertEqual(runtime, "runsc")
        self.assertEqual(self._calls, [])

    def test_classic_token_scopes_are_logged(self):
        routes = self._healthy_routes_with(
            "/repos/owner/repo",
            _FakeApiResponse(
                body=_REPO_BODY, headers={"x-oauth-scopes": "repo, workflow"}
            ),
        )
        with _clean_env(self._token_env(token="ghp_CLASSIC")):
            with self._reroute(routes):
                with self.assertLogs("orchestrator.preflight", level="INFO") as logs:
                    run_preflight()
        joined = "\n".join(logs.output)
        # Data-level assertions: the observed scopes and the token class
        # appear in the preflight log (not exact sentence wording).
        assert "repo, workflow" in joined
        assert "classic token" in joined

    def test_github_api_unreachable_raises_unavailable(self):
        with _clean_env(self._token_env()):
            with self._reroute(
                self._healthy_routes_with(
                    "/repos/owner/repo", urllib.error.URLError("connection refused")
                )
            ):
                with self.assertRaises(SandboxUnavailableError) as ctx:
                    run_preflight()
        assert "GitHub API unreachable" in str(ctx.exception)

    def test_unexpected_repo_body_fails_closed(self):
        with _clean_env(self._token_env()):
            with self._reroute(
                self._healthy_routes_with(
                    "/repos/owner/repo", _FakeApiResponse(body=b'{"nope": true}')
                )
            ):
                with self.assertRaises(TokenScopeError) as ctx:
                    run_preflight()
        assert "default_branch" in str(ctx.exception)

    def test_check_pat_scopes_direct_entry_point(self):
        # The runbook's --scopes-only path drives check_pat_scopes() directly.
        with _clean_env(self._token_env()):
            check_pat_scopes()
        probed = [url for url in self._calls if "api.github.com" in url]
        self.assertEqual(len(probed), len(REQUIRED_PAT_SCOPE_PROBES))

    def test_error_body_read_failure_reports_status_only(self):
        # A truncated 403 error body (mid-read transport failure) must not
        # crash the probe — the gap is reported from the status alone.
        broken = urllib.error.HTTPError(
            "https://api.github.com/repos/owner/repo",
            403,
            "forbidden",
            Message(),
            _BrokenBytesIO(),
        )
        with _clean_env(self._token_env()):
            with self._reroute(self._healthy_routes_with("/repos/owner/repo", broken)):
                with self.assertRaises(TokenScopeError) as ctx:
                    run_preflight()
        assert "Metadata (read)" in str(ctx.exception)
        assert "HTTP 403" in str(ctx.exception)

    def test_non_json_repo_body_fails_closed(self):
        # A 200 whose body is not JSON (e.g. an intercepting proxy's error
        # page) is a loud failure, never a bogus downstream probe.
        with _clean_env(self._token_env()):
            with self._reroute(
                self._healthy_routes_with(
                    "/repos/owner/repo",
                    _FakeApiResponse(body=b"<html>proxy error page</html>"),
                )
            ):
                with self.assertRaises(TokenScopeError) as ctx:
                    run_preflight()
        assert "default_branch" in str(ctx.exception)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
