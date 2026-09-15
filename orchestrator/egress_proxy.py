"""Host-side egress proxy for Execution Sandboxes (FR-5, issue #162).

A stdlib-only HTTP CONNECT proxy (ADR-0017 minimal dependencies; no squid
— the policy must be in-repo, tested, and generated from the same module
that emits the nftables ruleset, see ADR-0060) that runs on the deployment
host as the ONLY domain-capable route out of the Execution Sandboxes:

1. The sandbox's HTTP(S)_PROXY env (injected by ``orchestrator.sandbox``)
   points at this proxy, so package installs etc. arrive as CONNECT
   requests.
2. Every CONNECT target is classified against the egress allowlist
   (``orchestrator.egress.classify_target``): allowlisted domain or denied.
   IP-literal targets are always denied — the policy is domain-based and
   IP destinations would bypass DNS validation.
3. Allowed domains are resolved by the proxy itself via
   ``egress.resolve_validated_destination``: every DNS answer must be a safe
   public address, and the connection is pinned to the validated IP
   (resolve-once, connect-to-the-resolved-IP — the ADR-0037 layer-3
   rebinding defense).
   A sandbox cannot weaken this: the proxy runs on the host, its config is
   root-owned, and the nftables ruleset leaves no other route out.

Denials answer ``HTTP/1.1 403`` and are logged at WARNING with the client
address, target, and reason; the deployment runbook maps the client IP to
the sandbox container (``docker network inspect``), giving the "observable
logs (destination, sandbox id)" requirement of issue #162. Allowed
tunnels are logged at DEBUG (destination + pinned IP) to keep production
logs quiet while remaining auditable.

The ``resolver`` constructor parameter is the documented seam for tests and
embedders: production passes
:func:`egress.resolve_validated_destination`; tests inject a static map. It
does not weaken production — the hard-block enforcement of resolved
addresses lives in the production resolver and is unit-tested directly.
"""

import asyncio
import argparse
import logging
import signal
import sys
from urllib.parse import urlsplit

from orchestrator.egress import (
    EgressDeniedError,
    classify_target,
    load_egress_policy,
    resolve_validated_destination,
)

logger = logging.getLogger("orchestrator.egress_proxy")

DEFAULT_PROXY_PORT = 3128
CONNECT_TIMEOUT_SECONDS = 10.0
RELAY_IDLE_TIMEOUT_SECONDS = 300.0

_CONNECT_PREFIX = "CONNECT "


class EgressProxy:
    """CONNECT-only egress proxy enforcing the sandbox allowlist."""

    def __init__(
        self,
        allowlist: frozenset[str],
        resolver=resolve_validated_destination,
    ):
        self._allowlist = allowlist
        self._resolve = resolver
        self._servers: list[asyncio.AbstractServer] = []

    # -- lifecycle -----------------------------------------------------------

    async def start(self, bind_addresses: list[tuple[str, int]]):
        """Bind every address; return the listening servers.

        Public seam for embedders and tests: binds without installing signal
        handlers and returns immediately, so the caller owns the run loop
        (``serve`` is the systemd-style wrapper that waits for SIGTERM).
        Binding port 0 yields an ephemeral port, readable from the returned
        servers.
        """
        for host, port in bind_addresses:
            server = await asyncio.start_server(
                self._handle_client, host=host, port=port
            )
            self._servers.append(server)
            logger.info("Egress proxy listening on %s:%s", host, port)
        return list(self._servers)

    async def serve(self, bind_addresses: list[tuple[str, int]]) -> None:
        """Bind every address and serve until SIGTERM/SIGINT."""
        loop = asyncio.get_running_loop()
        stop = loop.create_future()

        def _request_stop(*_: object) -> None:
            if not stop.done():
                stop.set_result(None)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except (NotImplementedError, RuntimeError):
                pass  # non-main-thread or platform without signal fds
        try:
            await self.start(bind_addresses)
            await stop
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Close all listeners (idempotent)."""
        for server in self._servers:
            server.close()
            await server.wait_closed()
        self._servers.clear()

    # -- per-connection handling ----------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        client = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)
        try:
            request_line = await asyncio.wait_for(
                reader.readline(), timeout=CONNECT_TIMEOUT_SECONDS
            )
            target = self._parse_connect(request_line)
            if target is None:
                await deny_response(writer, client, "-", "not a CONNECT request")
                return
            host, port = target
            allowed, reason = classify_target(host, self._allowlist)
            if not allowed:
                await deny_response(writer, client, f"{host}:{port}", reason)
                return
            try:
                pinned_ip, upstream_port = await self._resolve(host, port)
            except EgressDeniedError as e:
                await deny_response(writer, client, f"{host}:{port}", str(e))
                return
            logger.debug(
                "EGRESS ALLOW client=%s host=%s pinned=%s:%s",
                client,
                host,
                pinned_ip,
                upstream_port,
            )
            try:
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(pinned_ip, upstream_port),
                    timeout=CONNECT_TIMEOUT_SECONDS,
                )
            except (OSError, TimeoutError) as e:
                await deny_response(
                    writer,
                    client,
                    f"{host}:{port}",
                    f"upstream connect to pinned {pinned_ip}:{upstream_port}"
                    f" failed: {e}",
                )
                return
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await self._relay(reader, writer, upstream_reader, upstream_writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass  # client vanished mid-request — nothing to answer
        finally:
            await close_client_writer(writer)

    @staticmethod
    def _parse_connect(request_line: bytes) -> tuple[str, int] | None:
        """Extract ``(host, port)`` from a CONNECT request line, else None."""
        text = request_line.decode("latin-1").strip()
        if not text.upper().startswith(_CONNECT_PREFIX):
            return None
        target = text.split(" ")[1]
        parsed = urlsplit(f"//{target}")
        host = parsed.hostname
        raw_port = parsed.port
        if not host:
            return None
        if raw_port is None:
            return None
        return host, raw_port

    @staticmethod
    async def _relay(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        """Pump bytes both ways until either side closes or idles out."""

        async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    chunk = await asyncio.wait_for(
                        src.read(65536), timeout=RELAY_IDLE_TIMEOUT_SECONDS
                    )
                    if not chunk:
                        return
                    dst.write(chunk)
                    await dst.drain()
            finally:
                # Half-close our side so the opposite pump's read sees EOF
                # and the relay terminates (a stuck relay would keep the
                # handler task alive past Server.wait_closed()).
                dst.close()

        await asyncio.gather(
            _pump(client_reader, upstream_writer),
            _pump(upstream_reader, client_writer),
            return_exceptions=True,
        )


async def deny_response(
    writer: asyncio.StreamWriter, client: str, destination: str, reason: str
) -> None:
    """Answer a refused CONNECT with ``HTTP/1.1 403`` and log the denial.

    Public contract of the egress proxy's deny protocol (every refusal —
    non-CONNECT input, non-allowlisted domain, unsafe DNS answer, failed
    upstream — funnels here). The write is guarded: a sandbox that closes
    its socket (RST) before reading the answer must not crash the proxy,
    so the drain may raise ``ConnectionError``/``OSError`` and is swallowed
    after the WARNING log records client, destination, and reason.
    """
    logger.warning(
        "EGRESS DENY client=%s destination=%s reason=%s",
        client,
        destination,
        reason,
    )
    try:
        writer.write(
            b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
    except (ConnectionError, OSError):
        pass


async def close_client_writer(writer: asyncio.StreamWriter) -> None:
    """Close the client side of a handled request, swallowing close errors.

    Public contract of the proxy's per-connection teardown (the ``finally``
    of every handled request): by the time the handler finishes, the sandbox
    may already have RST-closed its socket, so ``close()``/``wait_closed()``
    may raise ``ConnectionError``/``OSError`` — the teardown must never let
    that escape into the accept loop, or one vanished client would take the
    proxy down with it.
    """
    try:
        writer.close()
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass


def main(argv: list[str] | None = None) -> int:
    """CLI: run the egress proxy (host deployment, see the ops runbook)."""
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.egress_proxy",
        description="Egress proxy for Execution Sandboxes (FR-5 / ADR-0060).",
    )
    parser.add_argument(
        "--bind",
        action="append",
        default=None,
        help="address to bind (repeatable); default 127.0.0.1 — cloud"
        " deployments bind the egress bridge gateway plus the control-plane"
        " gateway per the ops runbook",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PROXY_PORT,
        help=f"listen port (default {DEFAULT_PROXY_PORT})",
    )
    args = parser.parse_args(argv)
    bind_addresses = [(host, args.port) for host in (args.bind or ["127.0.0.1"])]
    allowlist = load_egress_policy()
    proxy = EgressProxy(allowlist)
    try:
        asyncio.run(proxy.serve(bind_addresses))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
