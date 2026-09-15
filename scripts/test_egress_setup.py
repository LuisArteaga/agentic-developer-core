"""Tests for scripts/egress_setup.py (FR-5, issue #162 / ADR-0060).

The deployment CLI is tested through its public seams: docker/nft
invocations are scripted fakes (same pattern as orchestrator/test_sandbox),
the proxy probes run against real local loopback servers, and ``main``
routing is asserted per subcommand. No Docker daemon, no nftables, no root.
"""

import ipaddress
import socket
import threading
import unittest
from unittest.mock import MagicMock, patch

from scripts import egress_setup as setup_mod
from orchestrator.sandbox import SandboxEgressConfig


def _egress_config():
    return SandboxEgressConfig(
        network="agdc-sandbox-egress",
        subnet=ipaddress.IPv4Network("172.30.0.0/24"),
        proxy_url="http://172.30.0.1:3128",
    )


def _scripted_run(script):
    def _run(cmd, **kwargs):
        for predicate, outcome in script:
            if predicate(cmd):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"Unexpected subprocess command: {cmd}")

    return _run


def _completed(returncode=0, stdout=b"", stderr=b""):
    res = MagicMock()
    res.returncode = returncode
    res.stdout = stdout
    res.stderr = stderr
    return res


def _is_docker_network_inspect(cmd):
    return cmd[:2] == ["docker", "network"] and cmd[2] == "inspect"


def _is_docker_network_create(cmd):
    return cmd[:3] == ["docker", "network", "create"]


def _is_nft_verb(cmd, verb):
    return len(cmd) > 1 and cmd[0] == "nft" and cmd[1] == verb


class _StatusServer:
    """One-shot loopback TCP server answering a fixed HTTP status line."""

    def __init__(self, status_line: bytes):
        self._status_line = status_line
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(self._status_line)
        except OSError:
            pass

    def close(self):
        self._sock.close()


def _closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestEnsureNetwork(unittest.TestCase):
    def test_missing_network_is_created_with_fixed_subnet_and_no_icc(self):
        captured = []

        def _capture(cmd, **kwargs):
            captured.append(list(cmd))
            if _is_docker_network_inspect(cmd):
                return _completed(1, stderr=b"Error: No such network")
            if _is_docker_network_create(cmd):
                return _completed(0, stdout=b"abc123\n")
            raise AssertionError(f"Unexpected subprocess command: {cmd}")

        with patch("scripts.egress_setup.subprocess.run", side_effect=_capture):
            rc = setup_mod._ensure_network(_egress_config())
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 2)
        create = captured[1]
        assert "--subnet" in create
        assert "172.30.0.0/24" in create
        assert "--ipv6=false" in create
        assert "com.docker.network.bridge.enable_icc=false" in create
        assert "agdc-sandbox-egress" in create

    def test_existing_network_with_matching_subnet_is_idempotent(self):
        captured = []

        def _capture(cmd, **kwargs):
            captured.append(list(cmd))
            if _is_docker_network_inspect(cmd):
                return _completed(
                    0,
                    stdout=b'[{"Name": "x", "IPAM": {"Config": '
                    b'[{"Subnet": "172.30.0.0/24", "Gateway": "172.30.0.1"}]}}]',
                )
            raise AssertionError(f"Unexpected subprocess command: {cmd}")

        with patch("scripts.egress_setup.subprocess.run", side_effect=_capture):
            rc = setup_mod._ensure_network(_egress_config())
        self.assertEqual(rc, 0)
        self.assertEqual(len(captured), 1)

    def test_existing_network_with_drifted_subnet_fails_loud(self):
        def _capture(cmd, **kwargs):
            if _is_docker_network_inspect(cmd):
                return _completed(
                    0,
                    stdout=b'[{"Name": "x", "IPAM": {"Config": '
                    b'[{"Subnet": "172.31.0.0/24"}]}}]',
                )
            raise AssertionError(f"Unexpected subprocess command: {cmd}")

        with patch("scripts.egress_setup.subprocess.run", side_effect=_capture):
            with self.assertLogs(setup_mod.logger, level="ERROR") as logs:
                rc = setup_mod._ensure_network(_egress_config())
        self.assertEqual(rc, 1)
        assert "172.31.0.0/24" in "\n".join(logs.output)
        assert "172.30.0.0/24" in "\n".join(logs.output)

    def test_unparseable_ipam_treated_as_drift(self):
        def _capture(cmd, **kwargs):
            if _is_docker_network_inspect(cmd):
                return _completed(0, stdout=b"not-json")
            raise AssertionError(f"Unexpected subprocess command: {cmd}")

        with patch("scripts.egress_setup.subprocess.run", side_effect=_capture):
            rc = setup_mod._ensure_network(_egress_config())
        self.assertEqual(rc, 1)

    def test_create_failure_reports_daemon_error(self):
        def _capture(cmd, **kwargs):
            if _is_docker_network_inspect(cmd):
                return _completed(1, stderr=b"no such network")
            if _is_docker_network_create(cmd):
                return _completed(1, stderr=b"Pool overlaps")
            raise AssertionError(f"Unexpected subprocess command: {cmd}")

        with patch("scripts.egress_setup.subprocess.run", side_effect=_capture):
            with self.assertLogs(setup_mod.logger, level="ERROR") as logs:
                rc = setup_mod._ensure_network(_egress_config())
        self.assertEqual(rc, 1)
        assert "Pool overlaps" in "\n".join(logs.output)


class TestApplyRules(unittest.TestCase):
    def test_non_root_refuses_without_calling_nft(self):
        calls = []

        def _capture(cmd, **kwargs):
            calls.append(list(cmd))
            return _completed(0)

        with patch("scripts.egress_setup.os.geteuid", return_value=1000):
            with patch("scripts.egress_setup.subprocess.run", side_effect=_capture):
                rc = setup_mod._apply_rules(_egress_config())
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_first_application_deletes_then_checks_then_applies(self):
        captured = []

        def _capture(cmd, **kwargs):
            captured.append(list(cmd))
            if _is_nft_verb(cmd, "delete"):
                return _completed(1, stderr=b"No such table")
            if _is_nft_verb(cmd, "list"):
                return _completed(0, stdout=b"table inet agdc_sandbox_egress {")
            return _completed(0)

        with patch("scripts.egress_setup.os.geteuid", return_value=0):
            with patch("scripts.egress_setup.subprocess.run", side_effect=_capture):
                rc = setup_mod._apply_rules(_egress_config())
        self.assertEqual(rc, 0)
        verbs = [cmd[1] for cmd in captured if cmd[0] == "nft"]
        self.assertEqual(verbs, ["delete", "-c", "-f", "list"])

    def test_failed_nft_check_aborts_before_apply(self):
        captured = []

        def _capture(cmd, **kwargs):
            captured.append(list(cmd))
            if _is_nft_verb(cmd, "delete"):
                return _completed(1, stderr=b"No such table")
            if _is_nft_verb(cmd, "-c"):
                return _completed(1, stderr=b"syntax error")
            raise AssertionError(f"Unexpected subprocess command: {cmd}")

        with patch("scripts.egress_setup.os.geteuid", return_value=0):
            with patch("scripts.egress_setup.subprocess.run", side_effect=_capture):
                with self.assertLogs(setup_mod.logger, level="ERROR") as logs:
                    rc = setup_mod._apply_rules(_egress_config())
        self.assertEqual(rc, 1)
        assert "syntax error" in "\n".join(logs.output)
        verbs = [cmd[1] for cmd in captured if cmd[0] == "nft"]
        self.assertEqual(verbs, ["delete", "-c"])

    def test_failed_apply_is_loud(self):
        def _capture(cmd, **kwargs):
            if _is_nft_verb(cmd, "delete"):
                return _completed(1, stderr=b"No such table")
            if _is_nft_verb(cmd, "-f"):
                return _completed(1, stderr=b"Operation not permitted")
            return _completed(0)

        with patch("scripts.egress_setup.os.geteuid", return_value=0):
            with patch("scripts.egress_setup.subprocess.run", side_effect=_capture):
                with self.assertLogs(setup_mod.logger, level="ERROR"):
                    rc = setup_mod._apply_rules(_egress_config())
        self.assertEqual(rc, 1)

    def test_missing_table_after_apply_is_loud(self):
        def _capture(cmd, **kwargs):
            if _is_nft_verb(cmd, "list"):
                return _completed(1, stderr=b"No such table")
            return _completed(0)

        with patch("scripts.egress_setup.os.geteuid", return_value=0):
            with patch("scripts.egress_setup.subprocess.run", side_effect=_capture):
                with self.assertLogs(setup_mod.logger, level="ERROR"):
                    rc = setup_mod._apply_rules(_egress_config())
        self.assertEqual(rc, 1)


class TestConnectProbe(unittest.TestCase):
    def test_probe_reads_the_answered_status(self):
        for status in (b"HTTP/1.1 403 Forbidden\r\n\r\n", b"HTTP/1.1 200 OK\r\n\r\n"):
            server = _StatusServer(status)
            try:
                code = setup_mod._connect_probe(
                    "169.254.169.254", 80, f"http://127.0.0.1:{server.port}"
                )
                self.assertEqual(code, int(status.split(b" ")[1]))
            finally:
                server.close()

    def test_unreachable_proxy_returns_zero(self):
        dead = _closed_port()
        with self.assertLogs(setup_mod.logger, level="ERROR"):
            code = setup_mod._connect_probe(
                "169.254.169.254", 80, f"http://127.0.0.1:{dead}"
            )
        self.assertEqual(code, 0)

    def test_schemeless_probe_url_defaults_to_http(self):
        server = _StatusServer(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        try:
            code = setup_mod._connect_probe(
                "169.254.169.254", 80, f"127.0.0.1:{server.port}"
            )
            self.assertEqual(code, 403)
        finally:
            server.close()

    def test_server_closing_before_answer_yields_no_status(self):
        # A proxy that accepts and closes without answering (crashed proxy,
        # RST race) must degrade to "no status" (0), not crash or hang.
        server = _StatusServer(b"")
        try:
            code = setup_mod._connect_probe(
                "169.254.169.254", 80, f"http://127.0.0.1:{server.port}"
            )
            self.assertEqual(code, 0)
        finally:
            server.close()


class TestVerify(unittest.TestCase):
    def test_all_refusals_pass(self):
        with patch.object(
            setup_mod, "_connect_probe", side_effect=[403, 403]
        ) as fake_probe:
            rc = setup_mod._verify("http://127.0.0.1:3128", live=False)
        self.assertEqual(rc, 0)
        self.assertEqual(fake_probe.call_count, 2)

    def test_live_adds_allowlisted_registry_check(self):
        with patch.object(
            setup_mod, "_connect_probe", side_effect=[403, 403, 200]
        ) as fake_probe:
            rc = setup_mod._verify("http://127.0.0.1:3128", live=True)
        self.assertEqual(rc, 0)
        self.assertEqual(fake_probe.call_count, 3)

    def test_unexpected_answer_fails(self):
        with patch.object(setup_mod, "_connect_probe", side_effect=[200, 403]):
            with self.assertLogs(setup_mod.logger, level="ERROR") as logs:
                rc = setup_mod._verify("http://127.0.0.1:3128", live=False)
        self.assertEqual(rc, 1)
        assert "metadata IP must be refused" in "\n".join(logs.output)

    def test_probe_error_zero_counts_as_failure(self):
        with patch.object(setup_mod, "_connect_probe", side_effect=[0, 403]):
            rc = setup_mod._verify("http://127.0.0.1:3128", live=False)
        self.assertEqual(rc, 1)


class TestMainRouting(unittest.TestCase):
    def test_network_command_routes_to_ensure_network(self):
        with patch.object(
            setup_mod, "resolve_sandbox_egress", return_value=_egress_config()
        ):
            with patch.object(
                setup_mod, "_ensure_network", return_value=0
            ) as fake_ensure:
                rc = setup_mod.main(["network"])
        self.assertEqual(rc, 0)
        fake_ensure.assert_called_once()

    def test_verify_command_forwards_live_flag(self):
        with patch.object(
            setup_mod, "resolve_sandbox_egress", return_value=_egress_config()
        ):
            with patch.object(setup_mod, "_verify", return_value=0) as fake_verify:
                rc = setup_mod.main(["verify", "--live"])
        self.assertEqual(rc, 0)
        fake_verify.assert_called_once()
        assert fake_verify.call_args.kwargs["live"] is True

    def test_rules_command_routes_to_apply_rules(self):
        with patch.object(
            setup_mod, "resolve_sandbox_egress", return_value=_egress_config()
        ):
            with patch.object(setup_mod, "_apply_rules", return_value=0) as fake_rules:
                rc = setup_mod.main(["rules"])
        self.assertEqual(rc, 0)
        fake_rules.assert_called_once()

    def test_all_runs_sequence_and_stops_on_first_failure(self):
        with patch.object(
            setup_mod, "resolve_sandbox_egress", return_value=_egress_config()
        ):
            with patch.object(setup_mod, "_ensure_network", return_value=0):
                with patch.object(setup_mod, "_apply_rules", return_value=1):
                    with patch.object(setup_mod, "_verify", return_value=0):
                        rc = setup_mod.main(["all"])
        self.assertEqual(rc, 1)

    def test_all_stops_before_rules_when_network_fails(self):
        with patch.object(
            setup_mod, "resolve_sandbox_egress", return_value=_egress_config()
        ):
            with patch.object(setup_mod, "_ensure_network", return_value=1):
                with patch.object(
                    setup_mod, "_apply_rules", return_value=0
                ) as fake_rules:
                    rc = setup_mod.main(["all"])
        self.assertEqual(rc, 1)
        fake_rules.assert_not_called()

    def test_all_happy_path_runs_verify_last(self):
        with patch.object(
            setup_mod, "resolve_sandbox_egress", return_value=_egress_config()
        ):
            with patch.object(setup_mod, "_ensure_network", return_value=0):
                with patch.object(setup_mod, "_apply_rules", return_value=0):
                    with patch.object(
                        setup_mod, "_verify", return_value=0
                    ) as fake_verify:
                        rc = setup_mod.main(["all"])
        self.assertEqual(rc, 0)
        fake_verify.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
