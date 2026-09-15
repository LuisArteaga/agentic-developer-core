"""Deployment CLI for the sandbox egress lockdown (FR-5, issue #162).

Applies the enforcement planes generated from ``orchestrator.egress`` on
the deployment host (root), per docs/runbook-sandbox-egress.md and
ADR-0060:

- ``network``  ensure the dedicated ICC-disabled Docker network exists with
  the configured subnet (creates it when missing; verifies the live subnet
  when present — a drift is a loud failure, never a silent regenerate).
- ``rules``    load the generated nftables ruleset (deletes the previous
  ``inet agdc_sandbox_egress`` table first, checks with ``nft -c``, then
  applies). Requires root — allowlist changes require root, not container
  access (ADR-0056).
- ``verify``   exercise the proxy: a hard-blocked IP target and a
  non-allowlisted (never-resolvable) domain must BOTH be refused; with
  ``--live`` an allowlisted registry must be reachable through the proxy.
- ``all``      network, then rules, then verify.

The orchestrator itself never runs this script (it only reads the result
via the preflight); the sandbox fleet starts AFTER enforcement is in place.
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.egress import NFTABLES_TABLE_NAME, build_nftables_ruleset
from orchestrator.sandbox import (
    DEFAULT_EGRESS_PROXY_PORT,
    DEFAULT_PROXY_PROBE_URL,
    resolve_sandbox_egress,
)

logger = logging.getLogger("egress_setup")

_PROBE_TIMEOUT = 10
_NFT_CHECK_TIMEOUT = 60
_DOCKER_TIMEOUT = 30


def _ensure_network(egress) -> int:
    """Create the egress network when missing; verify the subnet when not."""
    name = egress.network
    subnet = str(egress.subnet)
    inspect = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
        timeout=_DOCKER_TIMEOUT,
        shell=False,
    )
    if inspect.returncode == 0:
        try:
            ipam = json.loads(inspect.stdout.decode("utf-8", errors="replace"))
            live = {
                entry.get("Subnet")
                for entry in ipam[0].get("IPAM", {}).get("Config", [])
            }
        except (json.JSONDecodeError, IndexError, AttributeError):
            live = set()
        if subnet not in live:
            logger.error(
                "Network %s exists with subnets %s but the configured egress "
                "subnet is %s. Recreate it (docker network rm %s && python3 "
                "scripts/egress_setup.py network) and regenerate the "
                "nftables ruleset — the ruleset polices the configured "
                "subnet only.",
                name,
                sorted(filter(None, live)) or "<unknown>",
                subnet,
                name,
            )
            return 1
        logger.info("Egress network %s already exists with subnet %s.", name, subnet)
        return 0
    create = subprocess.run(
        [
            "docker",
            "network",
            "create",
            "--subnet",
            subnet,
            "--ipv6=false",
            "--opt",
            "com.docker.network.bridge.enable_icc=false",
            name,
        ],
        capture_output=True,
        timeout=_DOCKER_TIMEOUT,
        shell=False,
    )
    if create.returncode != 0:
        logger.error(
            "Creating egress network %s failed: %s",
            name,
            create.stderr.decode("utf-8", errors="replace").strip(),
        )
        return 1
    logger.info(
        "Created egress network %s with subnet %s (ICC disabled).", name, subnet
    )
    return 0


def _apply_rules(egress) -> int:
    """Delete the previous table, check, then apply the generated ruleset."""
    if os.geteuid() != 0:
        logger.error(
            "The egress ruleset must be applied as root (allowlist changes "
            "require root, ADR-0056 FR-5); re-run with sudo."
        )
        return 2
    ruleset = build_nftables_ruleset(egress)
    delete = subprocess.run(
        ["nft", "delete", "table", "inet", NFTABLES_TABLE_NAME],
        capture_output=True,
        timeout=_NFT_CHECK_TIMEOUT,
        shell=False,
    )
    if delete.returncode != 0:
        # First application: the table does not exist yet — expected.
        logger.info(
            "No previous %s table (delete ignored): %s",
            NFTABLES_TABLE_NAME,
            delete.stderr.decode("utf-8", errors="replace").strip() or "absent",
        )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".nft", prefix="agdc-egress-", delete=False
    ) as handle:
        handle.write(ruleset)
        rules_path = handle.name
    try:
        check = subprocess.run(
            ["nft", "-c", "-f", rules_path],
            capture_output=True,
            timeout=_NFT_CHECK_TIMEOUT,
            shell=False,
        )
        if check.returncode != 0:
            logger.error(
                "nft check failed for the generated ruleset: %s",
                check.stderr.decode("utf-8", errors="replace").strip(),
            )
            return 1
        apply_proc = subprocess.run(
            ["nft", "-f", rules_path],
            capture_output=True,
            timeout=_NFT_CHECK_TIMEOUT,
            shell=False,
        )
        if apply_proc.returncode != 0:
            logger.error(
                "Applying the egress ruleset failed: %s",
                apply_proc.stderr.decode("utf-8", errors="replace").strip(),
            )
            return 1
    finally:
        Path(rules_path).unlink(missing_ok=True)
    listed = subprocess.run(
        ["nft", "list", "table", "inet", NFTABLES_TABLE_NAME],
        capture_output=True,
        timeout=_NFT_CHECK_TIMEOUT,
        shell=False,
    )
    if listed.returncode != 0:
        logger.error(
            "Post-apply verification failed: the %s table is not listed. %s",
            NFTABLES_TABLE_NAME,
            listed.stderr.decode("utf-8", errors="replace").strip(),
        )
        return 1
    logger.info(
        "Egress ruleset applied (table inet %s, default-deny for %s).",
        NFTABLES_TABLE_NAME,
        egress.subnet,
    )
    return 0


def _connect_probe(target_host: str, target_port: int, probe_url: str) -> int:
    """Send one CONNECT through the proxy; return the HTTP status (0 = error)."""
    parsed_probe = urlsplit(probe_url if "//" in probe_url else f"//{probe_url}")
    proxy_host = parsed_probe.hostname or "127.0.0.1"
    proxy_port = parsed_probe.port or DEFAULT_EGRESS_PROXY_PORT
    try:
        with socket.create_connection(
            (proxy_host, proxy_port), timeout=_PROBE_TIMEOUT
        ) as sock:
            request = (
                f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
                f"Host: {target_host}:{target_port}\r\n\r\n"
            )
            sock.sendall(request.encode("latin-1"))
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk
            head = response.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
            parts = head.split(" ")
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    except OSError as e:
        logger.error("Probe to the proxy at %s failed: %s", probe_url, e)
    return 0


def _verify(probe_url: str, live: bool) -> int:
    """Exercise the proxy: hard-block and non-allowlisted targets refuse."""
    failures: list[str] = []
    checks = [
        (
            "metadata IP must be refused",
            _connect_probe("169.254.169.254", 80, probe_url),
            403,
        ),
        (
            "non-allowlisted domain must be refused",
            _connect_probe("blocked-by-policy.invalid", 443, probe_url),
            403,
        ),
    ]
    if live:
        checks.append(
            (
                "pypi.org must be reachable through the proxy",
                _connect_probe("pypi.org", 443, probe_url),
                200,
            )
        )
    for label, actual, expected in checks:
        if actual == expected:
            logger.info("OK: %s (proxy answered %s).", label, actual)
        else:
            failures.append(f"{label}: expected {expected}, got {actual}")
            logger.error("FAIL: %s — expected %s, got %s.", label, expected, actual)
    if failures:
        logger.error(
            "%d egress verify check(s) failed; see "
            "docs/runbook-sandbox-egress.md (is the proxy running with the "
            "current policy?)",
            len(failures),
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="python3 scripts/egress_setup.py",
        description="Deploy the sandbox egress lockdown (FR-5, ADR-0060).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("network", help="ensure the egress network exists")
    sub.add_parser("rules", help="apply the nftables ruleset (root)")
    verify_parser = sub.add_parser("verify", help="exercise the proxy probes")
    verify_parser.add_argument(
        "--live",
        action="store_true",
        help="also probe an allowlisted registry (needs outbound internet)",
    )
    sub.add_parser("all", help="network, then rules, then verify")
    args = parser.parse_args(argv)

    egress = resolve_sandbox_egress()
    probe_url = os.getenv(
        "AGENT_EGRESS_PROXY_PROBE_URL", DEFAULT_PROXY_PROBE_URL
    ).strip()
    if args.command == "network":
        return _ensure_network(egress)
    if args.command == "rules":
        return _apply_rules(egress)
    if args.command == "verify":
        return _verify(probe_url, live=args.live)
    # all
    rc = _ensure_network(egress)
    if rc != 0:
        return rc
    rc = _apply_rules(egress)
    if rc != 0:
        return rc
    return _verify(probe_url, live=False)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
