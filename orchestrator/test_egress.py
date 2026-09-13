"""Tests for the sandbox egress lockdown (FR-5, issue #162 / ADR-0060).

Covers the issue's acceptance criteria at the enforceable layers:

- Allowlist config validation rejects IP literals, ranges, and schemes even
  when someone adds them via the extension key or ``config/sources.toml``
  (criterion: metadata/RFC1918/loopback blocked regardless of the allowlist).
- ``classify_target`` denies non-allowlisted and IP-literal targets;
  ``resolve_validated_destination`` denies private/loopback/link-local answers
  (the DNS-rebinding backstop, ADR-0028/ADR-0037 semantics).
- The generated nftables ruleset default-denies the sandbox subnet with
  hard-blocked ranges first and only the proxy port as the exception.
- The egress proxy answers CONNECT on the wire: allowed domain tunnels
  bytes to the pinned IP, denied targets answer 403 and are logged with
  the destination ("observable logs" edge case).
- Both CLIs (``orchestrator.egress`` and ``orchestrator.egress_proxy``)
  route to the documented behavior.

All network interactions are local loopback sockets driven by the proxy's
public ``start()``/resolver seams — no external network, no Docker daemon.
"""

import asyncio
import ipaddress
import os
import signal
import socket
import unittest
from contextlib import contextmanager
from io import StringIO
from unittest.mock import AsyncMock, patch

from orchestrator import egress as egress_mod
from orchestrator.egress import (
    DEFAULT_EGRESS_DOMAINS,
    EGRESS_EXTRA_DOMAINS_ENV,
    EgressDeniedError,
    NFTABLES_TABLE_NAME,
    build_nftables_ruleset,
    classify_target,
    load_egress_policy,
    main as egress_main,
    resolve_validated_destination,
    _validate_allowlist_entry,
)
from orchestrator.egress_proxy import EgressProxy, main as proxy_main
from orchestrator.sandbox import (
    SandboxConfigError,
    SandboxEgressConfig,
)
from orchestrator.sources_config import SourcesConfig


def _ga_info(addr):
    """A ``socket.getaddrinfo``-shaped single record for ``addr``."""
    return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))


@contextmanager
def _clean_env(extra=None):
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(extra or {})
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


class TestValidateAllowlistEntry(unittest.TestCase):
    def test_bare_domain_normalizes_to_lowercase(self):
        self.assertEqual(_validate_allowlist_entry("PyPI.ORG"), "pypi.org")

    def test_ip_literal_metadata_rejected(self):
        with self.assertRaises(SandboxConfigError) as ctx:
            _validate_allowlist_entry("169.254.169.254")
        assert "domain names" in str(ctx.exception)

    def test_ip_literal_rfc1918_rejected(self):
        with self.assertRaises(SandboxConfigError):
            _validate_allowlist_entry("10.0.0.1")

    def test_loopback_rejected(self):
        with self.assertRaises(SandboxConfigError):
            _validate_allowlist_entry("127.0.0.1")

    def test_cidr_range_rejected(self):
        with self.assertRaises(SandboxConfigError) as ctx:
            _validate_allowlist_entry("10.0.0.0/8")
        assert "IP range" in str(ctx.exception)

    def test_scheme_prefixed_rejected(self):
        with self.assertRaises(SandboxConfigError) as ctx:
            _validate_allowlist_entry("https://pypi.org")
        assert "scheme" in str(ctx.exception)

    def test_blank_entry_rejected(self):
        with self.assertRaises(SandboxConfigError):
            _validate_allowlist_entry("   ")


class TestLoadEgressPolicy(unittest.TestCase):
    def _policy(self, sources_domains=(), extra=""):
        with _clean_env({EGRESS_EXTRA_DOMAINS_ENV: extra} if extra else {}):
            with patch(
                "orchestrator.egress.load_sources_config",
                return_value=SourcesConfig(strict=False, domains=list(sources_domains)),
            ):
                return load_egress_policy()

    def test_default_registries_are_allowlisted(self):
        policy = self._policy()
        for domain in (
            "pypi.org",
            "files.pythonhosted.org",
            "registry.npmjs.org",
            "github.com",
            "githubusercontent.com",
            "archive.ubuntu.com",
            "security.ubuntu.com",
        ):
            assert domain in policy
        self.assertEqual(DEFAULT_EGRESS_DOMAINS, frozenset(DEFAULT_EGRESS_DOMAINS))

    def test_sources_toml_domains_merge_in(self):
        policy = self._policy(sources_domains=["arxiv.org", "dl.acm.org"])
        assert "arxiv.org" in policy
        assert "dl.acm.org" in policy

    def test_extra_domains_env_merges_in(self):
        policy = self._policy(extra=" private-index.example.com ,corporate.dev ")
        assert "private-index.example.com" in policy
        assert "corporate.dev" in policy

    def test_duplicates_collapse(self):
        policy = self._policy(sources_domains=["pypi.org"], extra="pypi.org")
        self.assertEqual(len(policy), len(set(policy)))

    def test_extra_domain_ip_literal_is_rejected(self):
        # Criterion: blocked destinations are rejected by VALIDATION even
        # when someone adds them to the extension config.
        with self.assertRaises(SandboxConfigError) as ctx:
            self._policy(extra="169.254.169.254")
        assert "IP literal" in str(ctx.exception) or "domain names" in str(
            ctx.exception
        )

    def test_extra_domain_cidr_is_rejected(self):
        with self.assertRaises(SandboxConfigError):
            self._policy(extra="10.0.0.0/8")

    def test_sources_domain_ip_literal_is_rejected(self):
        with self.assertRaises(SandboxConfigError):
            self._policy(sources_domains=["127.0.0.1"])


class TestClassifyTarget(unittest.TestCase):
    ALLOWED = frozenset({"pypi.org", "githubusercontent.com"})

    def test_exact_match_allowed(self):
        allowed, reason = classify_target("pypi.org", self.ALLOWED)
        self.assertTrue(allowed)

    def test_subdomain_match_allowed(self):
        allowed, _ = classify_target("raw.githubusercontent.com", self.ALLOWED)
        self.assertTrue(allowed)

    def test_lookalike_domain_denied(self):
        allowed, reason = classify_target("evilpypi.org", self.ALLOWED)
        self.assertFalse(allowed)
        assert "not in the egress allowlist" in reason

    def test_parent_domain_denied(self):
        # Allowlisting a subdomain must not open its parent.
        allowed, _ = classify_target("com", self.ALLOWED)
        self.assertFalse(allowed)

    def test_ip_literal_denied_regardless_of_content(self):
        for literal in ("169.254.169.254", "10.0.0.5", "127.0.0.1", "::1"):
            allowed, reason = classify_target(literal, self.ALLOWED)
            self.assertFalse(allowed)
            assert "ip literals" in reason

    def test_trailing_dot_and_case_normalized(self):
        allowed, _ = classify_target("PYPI.org.", self.ALLOWED)
        self.assertTrue(allowed)


class TestIpIsUnsafe(unittest.TestCase):
    def test_hard_blocked_classes(self):
        blocked = ["169.254.169.254", "10.1.2.3", "127.0.0.1", "192.168.1.1", "0.0.0.0"]
        for addr in blocked:
            assert egress_mod._ip_is_unsafe(ipaddress.ip_address(addr))

    def test_public_is_safe(self):
        assert not egress_mod._ip_is_unsafe(ipaddress.ip_address("151.101.0.223"))
        assert not egress_mod._ip_is_unsafe(ipaddress.ip_address("140.82.121.4"))

    def test_ipv6_private_and_loopback_unsafe(self):
        assert egress_mod._ip_is_unsafe(ipaddress.ip_address("fc00::1"))
        assert egress_mod._ip_is_unsafe(ipaddress.ip_address("::1"))


class TestResolveValidatedDestination(unittest.TestCase):
    def test_public_answers_pin_the_first_address(self):
        answers = [
            _ga_info("151.101.0.223"),
            _ga_info("151.101.64.223"),
        ]
        with patch("orchestrator.egress.socket.getaddrinfo", return_value=answers):
            pinned_ip, pinned_port = resolve_validated_destination("pypi.org", 443)
        self.assertEqual(pinned_ip, "151.101.0.223")
        self.assertEqual(pinned_port, 443)

    def test_private_answer_denies_the_whole_lookup(self):
        answers = [_ga_info("151.101.0.223"), _ga_info("10.0.0.5")]
        with patch("orchestrator.egress.socket.getaddrinfo", return_value=answers):
            with self.assertRaises(EgressDeniedError) as ctx:
                resolve_validated_destination("rebound.example.com", 443)
        assert "10.0.0.5" in str(ctx.exception)

    def test_metadata_answer_denied(self):
        with patch(
            "orchestrator.egress.socket.getaddrinfo",
            return_value=[_ga_info("169.254.169.254")],
        ):
            with self.assertRaises(EgressDeniedError) as ctx:
                resolve_validated_destination("rebound.example.com", 443)
        assert "blocked range" in str(ctx.exception)

    def test_loopback_answer_denied(self):
        with patch(
            "orchestrator.egress.socket.getaddrinfo",
            return_value=[_ga_info("127.0.0.1")],
        ):
            with self.assertRaises(EgressDeniedError):
                resolve_validated_destination("localhost.example.com", 443)

    def test_unparseable_answer_fails_closed(self):
        with patch(
            "orchestrator.egress.socket.getaddrinfo",
            return_value=[_ga_info("not-an-ip")],
        ):
            with self.assertRaises(EgressDeniedError):
                resolve_validated_destination("weird.example.com", 443)

    def test_failed_lookup_denied(self):
        with patch(
            "orchestrator.egress.socket.getaddrinfo",
            side_effect=socket.gaierror("NXDOMAIN"),
        ):
            with self.assertRaises(EgressDeniedError) as ctx:
                resolve_validated_destination("nope.invalid", 443)
        assert "DNS resolution failed" in str(ctx.exception)

    def test_empty_answers_denied(self):
        with patch("orchestrator.egress.socket.getaddrinfo", return_value=[]):
            with self.assertRaises(EgressDeniedError):
                resolve_validated_destination("empty.example.com", 443)


def _egress_config(subnet="172.30.0.0/24", network="agdc-sandbox-egress"):
    return SandboxEgressConfig(
        network=network,
        subnet=ipaddress.IPv4Network(subnet),
        proxy_url="http://172.30.0.1:3128",
    )


class TestBuildNftablesRuleset(unittest.TestCase):
    def test_structure_default_deny_for_subnet_only(self):
        ruleset = build_nftables_ruleset(_egress_config())
        # Selective-drop base chains before Docker's own filter chains.
        assert "chain forward {" in ruleset
        assert "type filter hook forward priority -10; policy accept;" in ruleset
        assert "chain input {" in ruleset
        assert "type filter hook input priority -10; policy accept;" in ruleset
        # Only sandbox-subnet traffic is policed.
        assert "ip saddr 172.30.0.0/24" in ruleset

    def test_hard_blocked_ranges_drop_first(self):
        ruleset = build_nftables_ruleset(_egress_config())
        hard_index = ruleset.index("agdc-egress-hard-drop")
        set_index = ruleset.index("@hard_block_v4")
        deny_index = ruleset.index("agdc-egress-deny")
        assert set_index < hard_index < deny_index
        for cidr in ("169.254.0.0/16", "10.0.0.0/8", "127.0.0.0/8", "192.168.0.0/16"):
            assert cidr in ruleset

    def test_only_the_proxy_port_is_reachable(self):
        ruleset = build_nftables_ruleset(_egress_config())
        assert "ip daddr != 172.30.0.1" in ruleset
        assert "tcp dport != 3128" in ruleset
        assert "udp dport != 3128" in ruleset

    def test_every_drop_is_logged(self):
        ruleset = build_nftables_ruleset(_egress_config())
        assert ruleset.count("log prefix") == 7

    def test_custom_subnet_and_gateway(self):
        config = SandboxEgressConfig(
            network="other-net",
            subnet=ipaddress.IPv4Network("192.168.77.0/24"),
            proxy_url="http://192.168.77.1:3128",
        )
        ruleset = build_nftables_ruleset(config)
        assert "ip saddr 192.168.77.0/24" in ruleset
        assert "ip daddr != 192.168.77.1" in ruleset

    def test_table_name_constant_matches_ruleset(self):
        ruleset = build_nftables_ruleset(_egress_config())
        assert f"table inet {NFTABLES_TABLE_NAME}" in ruleset


class TestEgressMain(unittest.TestCase):
    def test_print_allowlist_sorted(self):
        buffer = StringIO()
        with patch.object(egress_mod, "load_egress_policy") as fake_load:
            fake_load.return_value = frozenset({"b.example.com", "a.example.com"})
            with patch("sys.stdout", buffer):
                rc = egress_main(["--print-allowlist"])
        self.assertEqual(rc, 0)
        self.assertEqual(buffer.getvalue(), "a.example.com\nb.example.com\n")

    def test_print_ruleset_emits_generated_rules(self):
        buffer = StringIO()
        with patch.object(
            egress_mod, "resolve_sandbox_egress", return_value=_egress_config()
        ):
            with patch("sys.stdout", buffer):
                rc = egress_main(["--print-ruleset"])
        self.assertEqual(rc, 0)
        assert f"table inet {NFTABLES_TABLE_NAME}" in buffer.getvalue()


def _static_resolver(mapping):
    """Test resolver seam: static domain→(ip, port) map (the proxy
    constructor's documented injection point)."""

    async def _resolve(host, port):
        entry = mapping.get(host)
        if entry is None:
            raise EgressDeniedError(f"no DNS mapping for {host!r}") from None
        ip, pinned_port = entry
        return ip, pinned_port

    return _resolve


async def _echo_handler(reader, writer):
    """Echo server: bounces every byte back through the tunnel."""
    try:
        while True:
            chunk = await reader.read(1024)
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()
    finally:
        writer.close()


class TestEgressProxyWire(unittest.TestCase):
    """Wire-level CONNECT behavior on loopback sockets (no external net)."""

    ALLOWED = frozenset({"pypi.org", "files.pythonhosted.org"})

    def _run(self, scenario, timeout=10):
        async def runner():
            await asyncio.wait_for(scenario(), timeout)

        return asyncio.run(runner())

    async def _start_proxy(self, resolver):
        proxy = EgressProxy(self.ALLOWED, resolver=resolver)
        servers = await proxy.start([("127.0.0.1", 0)])
        port = servers[0].sockets[0].getsockname()[1]
        return proxy, servers, port

    async def _cleanup(self, servers, upstream):
        for server in servers:
            server.close()
            await server.wait_closed()
        upstream.close()
        await upstream.wait_closed()

    async def _connect_and_read_status(self, proxy_port, request_line):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", proxy_port), 5
        )
        writer.write(request_line)
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), 5)
        # Drain the rest of the response headers so the tunnel bytes that
        # follow start on a clean buffer.
        while True:
            line = await asyncio.wait_for(reader.readline(), 5)
            if line in (b"\r\n", b"\n", b""):
                break
        return reader, writer, status

    def test_allowed_domain_tunnels_to_pinned_ip(self):
        async def scenario():
            upstream = await asyncio.start_server(_echo_handler, "127.0.0.1", 0)
            up_port = upstream.sockets[0].getsockname()[1]
            proxy, servers, port = await self._start_proxy(
                _static_resolver({"pypi.org": ("127.0.0.1", up_port)})
            )
            try:
                reader, writer, status = await self._connect_and_read_status(
                    port,
                    # A bare CONNECT line (no extra headers) — anything the
                    # client sends after the status line is tunneled verbatim,
                    # so extra request headers would be echoed back first.
                    b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n",
                )
                assert status.startswith(b"HTTP/1.1 200"), status
                writer.write(b"ping")
                await writer.drain()
                # The proxy forwards any buffered remainder of the client's
                # request (e.g. the blank line) into the tunnel, so skip
                # whatever the echo bounces back before the payload.
                echoed = b""
                while b"ping" not in echoed:
                    echoed += await asyncio.wait_for(reader.read(64), 5)
                writer.close()
                await writer.wait_closed()
            finally:
                await self._cleanup(servers, upstream)

        self._run(scenario)

    def test_denied_domain_answers_403_and_logs_destination(self):
        async def scenario():
            upstream = await asyncio.start_server(_echo_handler, "127.0.0.1", 0)
            proxy, servers, port = await self._start_proxy(
                _static_resolver({"pypi.org": ("127.0.0.1", 1)})
            )
            try:
                with self.assertLogs(
                    "orchestrator.egress_proxy", level="WARNING"
                ) as logs:
                    reader, writer, status = await self._connect_and_read_status(
                        port,
                        b"CONNECT blocked.invalid:443 HTTP/1.1\r\n\r\n",
                    )
                assert status.startswith(b"HTTP/1.1 403"), status
                deny_lines = "\n".join(logs.output)
                assert "EGRESS DENY" in deny_lines
                assert "blocked.invalid" in deny_lines
                writer.close()
            finally:
                await self._cleanup(servers, upstream)

        self._run(scenario)

    def test_ip_literal_metadata_target_denied_and_logged(self):
        async def scenario():
            upstream = await asyncio.start_server(_echo_handler, "127.0.0.1", 0)
            proxy, servers, port = await self._start_proxy(
                _static_resolver({"pypi.org": ("127.0.0.1", 1)})
            )
            try:
                with self.assertLogs(
                    "orchestrator.egress_proxy", level="WARNING"
                ) as logs:
                    reader, writer, status = await self._connect_and_read_status(
                        port, b"CONNECT 169.254.169.254:80 HTTP/1.1\r\n\r\n"
                    )
                assert status.startswith(b"HTTP/1.1 403"), status
                deny_lines = "\n".join(logs.output)
                assert "169.254.169.254" in deny_lines
                assert "ip literals" in deny_lines
                writer.close()
            finally:
                await self._cleanup(servers, upstream)

        self._run(scenario)

    def test_rebinding_dns_answer_denied_and_logged(self):
        # The domain is allowlisted but its DNS answer is private: the
        # resolver (production seam) denies, the proxy answers 403.
        async def scenario():
            upstream = await asyncio.start_server(_echo_handler, "127.0.0.1", 0)

            async def rebinding(host, port):
                raise EgressDeniedError(
                    "rebound.example.com resolves into a blocked range (10.0.0.5)"
                )

            proxy, servers, port = await self._start_proxy(rebinding)
            try:
                with self.assertLogs(
                    "orchestrator.egress_proxy", level="WARNING"
                ) as logs:
                    reader, writer, status = await self._connect_and_read_status(
                        port, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n"
                    )
                assert status.startswith(b"HTTP/1.1 403"), status
                assert "blocked range" in "\n".join(logs.output)
                writer.close()
            finally:
                await self._cleanup(servers, upstream)

        self._run(scenario)

    def test_upstream_connect_failure_answers_403(self):
        async def scenario():
            closed = socket.socket()
            closed.bind(("127.0.0.1", 0))
            dead_port = closed.getsockname()[1]
            closed.close()

            async def pass_through(host, port):
                return "127.0.0.1", port

            proxy, servers, port = await self._start_proxy(pass_through)
            try:
                with self.assertLogs(
                    "orchestrator.egress_proxy", level="WARNING"
                ) as logs:
                    reader, writer, status = await self._connect_and_read_status(
                        port,
                        f"CONNECT pypi.org:{dead_port} HTTP/1.1\r\n\r\n".encode(),
                    )
                assert status.startswith(b"HTTP/1.1 403"), status
                assert "upstream connect" in "\n".join(logs.output)
                writer.close()
            finally:
                for server in servers:
                    server.close()
                    await server.wait_closed()

        self._run(scenario)

    def test_non_connect_request_denied(self):
        async def scenario():
            upstream = await asyncio.start_server(_echo_handler, "127.0.0.1", 0)
            proxy, servers, port = await self._start_proxy(
                _static_resolver({"pypi.org": ("127.0.0.1", 1)})
            )
            try:
                reader, writer, status = await self._connect_and_read_status(
                    port, b"GET / HTTP/1.1\r\n\r\n"
                )
                assert status.startswith(b"HTTP/1.1 403"), status
                writer.close()
            finally:
                await self._cleanup(servers, upstream)

        self._run(scenario)

    def test_connect_without_port_denied(self):
        async def scenario():
            upstream = await asyncio.start_server(_echo_handler, "127.0.0.1", 0)
            proxy, servers, port = await self._start_proxy(
                _static_resolver({"pypi.org": ("127.0.0.1", 1)})
            )
            try:
                reader, writer, status = await self._connect_and_read_status(
                    port, b"CONNECT pypi.org HTTP/1.1\r\n\r\n"
                )
                assert status.startswith(b"HTTP/1.1 403"), status
                writer.close()
            finally:
                await self._cleanup(servers, upstream)

        self._run(scenario)

    def test_serve_binds_and_stops_on_sigterm(self):
        async def scenario():
            proxy = EgressProxy(self.ALLOWED, resolver=_static_resolver({}))
            task = asyncio.create_task(proxy.serve([("127.0.0.1", 0)]))
            await asyncio.sleep(0.05)
            assert not task.done()
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.wait_for(task, 5)
            assert task.done()

        self._run(scenario)

    def test_main_runs_proxy_with_default_bind(self):
        with patch(
            "orchestrator.egress_proxy.load_egress_policy",
            return_value=self.ALLOWED,
        ):
            with patch.object(
                EgressProxy, "serve", new_callable=AsyncMock, return_value=None
            ) as fake_serve:
                rc = proxy_main(["--bind", "127.0.0.1", "--port", "39313"])
        self.assertEqual(rc, 0)
        self.assertEqual(fake_serve.call_args.args[0], [("127.0.0.1", 39313)])
