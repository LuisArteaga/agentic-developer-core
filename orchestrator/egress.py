"""Sandbox egress policy: allowlist, classification, nftables generation.

FR-5 of docs/cloud-deployment-requirements.md (issue #162), implementing the
mechanism ADR-0056 decided and ADR-0060 records in detail: default-deny
egress for Execution Sandboxes enforced OUTSIDE the sandbox's reach, as two
planes generated from this one policy module —

1. The host nftables ruleset (``build_nftables_ruleset``): sandbox-subnet
   traffic may only reach the egress proxy; everything else is logged and
   dropped, with hard-blocked ranges (cloud metadata, RFC 1918, loopback,
   reserved, multicast) dropped even before that. Applied at deployment
   time by root (``scripts/egress_setup.py``), never by the orchestrator —
   allowlist changes therefore require host root, not container access.
2. The egress proxy (``orchestrator.egress_proxy``): a stdlib CONNECT proxy
   running on the host as the only domain-capable route out. It classifies
   every CONNECT target against the allowlist (``classify_target``) and
   resolves DNS itself, validating every resolved address against the
   hard-blocked ranges before connecting to the pinned IP
   (``resolve_validated_destination``) — the DNS-rebinding defense from
   ADR-0037 layer 3 / ADR-0028, applied to sandbox egress.

The allowlist merges three sources: the default package-registry domains
required by Verification installs, the domains already governed by
``config/sources.toml`` for URL Fetch/Web Search (so one curated list
governs both the tool layer and the network layer), and the per-deployment
extension key ``AGENT_SANDBOX_EGRESS_EXTRA_DOMAINS`` (a target repository
needing a private index adds its domain there — never a blanket disable).

SSRF posture note (ADR-0028): the tool-layer validation in
``orchestrator.research_tools`` is unchanged and remains the first line of
defense for URL Fetch; this module is the independent network-layer backstop
beneath it. The address-classification semantics (``_ip_is_unsafe``) and the
resolve-once-pin-IP discipline are ports of ``research_tools``
``_resolve_validated_ip`` rather than a new SSRF implementation (ADR-0009),
kept local because the contexts differ: egress raises with a reason instead
of returning ``None``, and the proxy owns the socket it pins.
"""

import argparse
import ipaddress
import logging
import os
import socket
import sys

from orchestrator.sandbox import (
    DEFAULT_EGRESS_PROXY_PORT,
    SandboxEgressConfig,
    SandboxConfigError,
    resolve_sandbox_egress,
)
from orchestrator.sources_config import load_sources_config

logger = logging.getLogger("orchestrator.egress")

# Per-deployment extension key (FR-5 edge case, issue #162): comma-separated
# extra domains for a target repository that needs a private index. A single
# orchestrator instance serves a single target repository, so a
# deployment-scoped key IS the per-target-repo mechanism.
EGRESS_EXTRA_DOMAINS_ENV = "AGENT_SANDBOX_EGRESS_EXTRA_DOMAINS"

# Default package-registry domains required by Verification installs
# (pip/npm/git-over-HTTPS against releases) plus the Ubuntu mirrors of the
# documented deployment OS (FR-8). Subdomain matching applies, so
# ``githubusercontent.com`` covers raw/objects/codeload endpoints.
DEFAULT_EGRESS_DOMAINS = frozenset(
    {
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "github.com",
        "githubusercontent.com",
        "archive.ubuntu.com",
        "security.ubuntu.com",
    }
)

# Hard-blocked destination ranges for the nftables ruleset (issue #162 edge
# case): cloud metadata link-local, RFC 1918, loopback, plus the reserved /
# multicast / benchmark space. The egress proxy enforces the same classes at
# connect time via ``_ip_is_unsafe`` (ADR-0028 semantics); the ruleset makes
# them explicit at the IP layer so even a proxy-bypassing flow cannot reach
# them.
HARD_BLOCKED_V4_RANGES: tuple[str, ...] = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)

NFTABLES_TABLE_NAME = "agdc_sandbox_egress"
_LOG_HARD_DROP_PREFIX = "agdc-egress-hard-drop: "
_LOG_DENY_PREFIX = "agdc-egress-deny: "


class EgressDeniedError(Exception):
    """A CONNECT target (or its DNS answer) violates the egress policy."""


def _ip_is_unsafe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for private/loopback/link-local/reserved/multicast/unspecified IPs.

    Same address-classification semantics as
    ``orchestrator.research_tools._ip_is_unsafe`` (ADR-0028 / ADR-0037);
    kept local per the module docstring.
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_allowlist_entry(entry: str) -> str:
    """Normalize one allowlist entry to a bare lowercase domain.

    Loud validation (issue #162 acceptance criterion 2): an allowlist is a
    list of domain NAMES. IP literals, CIDR ranges, and scheme-prefixed
    entries are configuration errors — even though the proxy and ruleset
    would refuse such destinations at runtime, accepting them silently in
    config would make the allowlist's meaning ambiguous. Raises
    ``SandboxConfigError`` on anything that is not a bare domain.
    """
    value = entry.strip().lower()
    if not value:
        raise SandboxConfigError(
            f"Blank egress allowlist entry in {EGRESS_EXTRA_DOMAINS_ENV} or "
            "config/sources.toml"
        )
    if "://" in value:
        raise SandboxConfigError(
            f"Egress allowlist entry {entry!r} carries a scheme; bare "
            "domain names only (the proxy matches CONNECT targets, not URLs)"
        )
    if "/" in value:
        raise SandboxConfigError(
            f"Egress allowlist entry {entry!r} looks like an IP range; the "
            "allowlist takes domain names only — a range cannot be "
            "hard-block-correct and is rejected by validation"
        )
    try:
        parsed_ip = ipaddress.ip_address(value)
    except ValueError:
        return value
    raise SandboxConfigError(
        f"Egress allowlist entry {entry!r} is the IP literal {parsed_ip!r}; "
        "the allowlist takes domain names only — IP destinations are never "
        "allowlisted (hard-blocked ranges stay blocked regardless of the "
        "allowlist)"
    )


def load_egress_policy() -> frozenset[str]:
    """Build the effective egress domain allowlist.

    Merges (1) ``DEFAULT_EGRESS_DOMAINS``, (2) the ``domains`` of
    ``config/sources.toml`` (missing file → its loader falls back to an
    empty, non-strict config with a warning — the egress allowlist then
    still contains the defaults), and (3) the comma-separated
    ``AGENT_SANDBOX_EGRESS_EXTRA_DOMAINS`` extension. Every entry passes
    ``_validate_allowlist_entry``; duplicates collapse via the set union.
    """
    sources = load_sources_config()
    raw_entries: list[str] = list(DEFAULT_EGRESS_DOMAINS)
    raw_entries.extend(sources.domains)
    extra = os.getenv(EGRESS_EXTRA_DOMAINS_ENV, "")
    raw_entries.extend(part.strip() for part in extra.split(",") if part.strip())
    return frozenset(_validate_allowlist_entry(entry) for entry in raw_entries)


def classify_target(host: str, allowlist: frozenset[str]) -> tuple[bool, str]:
    """Decide whether a CONNECT target host is reachable.

    Returns ``(allowed, reason)``. Matching semantics follow
    ``research_tools.is_domain_allowed`` (ADR-0028): exact match or
    subdomain match against the allowlist. IP literals are never
    allowlisted — the policy is domain-based, and a raw IP destination
    would bypass the DNS validation the proxy performs for domains.
    """
    normalized = host.strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(normalized)
    except ValueError:
        pass
    else:
        return False, "ip literals are never allowlisted"
    for domain in allowlist:
        if normalized == domain or normalized.endswith("." + domain):
            return True, f"matches allowlisted domain {domain}"
    return False, "not in the egress allowlist"


def resolve_validated_destination(host: str, port: int) -> tuple[str, int]:
    """Resolve ``host`` and return ONE validated, pinned ``(ip, port)``.

    Production DNS step of the egress proxy: the proxy connects to the
    returned IP directly (no second resolution at connect time, closing the
    rebinding TOCTOU the way ADR-0037 layer 3 does for URL Fetch). Every
    address the resolver answers with must be a safe public address
    (``_ip_is_unsafe``); any private/loopback/link-local/reserved/multicast/
    unspecified answer — or an unparseable one, or a failed lookup — raises
    ``EgressDeniedError``, so a DNS rebinding attack against an allowlisted
    domain can never steer the proxy at the metadata endpoint or an
    internal service.

    The port is passed through unchanged in production; returning it keeps
    the resolver the single seam that decides WHERE the proxy connects,
    which is what tests inject an ephemeral loopback upstream through.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise EgressDeniedError(f"DNS resolution failed for {host!r}: {e}") from e
    if not infos:
        raise EgressDeniedError(f"DNS returned no addresses for {host!r}")
    pinned: str | None = None
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            raise EgressDeniedError(
                f"DNS returned an unparseable address {addr!r} for {host!r}"
            ) from None
        if _ip_is_unsafe(ip):
            raise EgressDeniedError(
                f"{host!r} resolves into a blocked range ({addr}) — denied "
                "regardless of the domain allowlist (DNS rebinding / SSRF "
                "backstop)"
            )
        if pinned is None:
            pinned = addr
    assert pinned is not None, "validated addresses present but none pinned"
    return pinned, port


def build_nftables_ruleset(egress: SandboxEgressConfig) -> str:
    """Render the host nftables ruleset for the sandbox egress boundary.

    The ruleset is loaded at deployment time by root (``scripts/egress_setup.py``
    deletes any previous ``inet agdc_sandbox_egress`` table first and runs
    ``nft -c -f`` before applying). Design notes (ADR-0060):

    - Two selective-drop chains at priority ``-10`` (before Docker's own
      filter chains) with ``policy accept``: ONLY traffic sourced from the
      sandbox subnet is policed, so unrelated host/bridge traffic is never
      perturbed and Docker's own forwarding semantics stay intact.
    - Hard-blocked ranges drop FIRST, before any exception (criterion:
      metadata/RFC1918/loopback blocked regardless of the allowlist).
    - Sandboxes may reach only the egress proxy (TCP to the gateway's proxy
      port) on the FORWARD and INPUT paths; direct DNS is unnecessary
      (Docker's embedded resolver serves containers from the host stack)
      and therefore denied with the rest.
    - Every drop is logged (``log prefix`` + counter) so blocked attempts
      are observable with destination and sandbox source IP.
    """
    gateway = egress.gateway
    port = DEFAULT_EGRESS_PROXY_PORT
    hard_blocks = ",\n".join(f"        {cidr}" for cidr in HARD_BLOCKED_V4_RANGES)
    subnet = egress.subnet.with_prefixlen
    return "\n".join(
        [
            "#!/usr/sbin/nft -f",
            "# Generated by `python -m orchestrator.egress --print-ruleset`"
            " (orchestrator.egress, FR-5 / ADR-0060). DO NOT EDIT BY HAND —",
            "# regenerate from the policy and apply via"
            " scripts/egress_setup.py (root).",
            "",
            f"table inet {NFTABLES_TABLE_NAME} {{",
            "    set hard_block_v4 {",
            "        type ipv4_addr",
            "        flags interval",
            "        elements = {",
            hard_blocks,
            "        }",
            "    }",
            "",
            "    chain forward {",
            "        type filter hook forward priority -10; policy accept;",
            f"        ip saddr {subnet} ip daddr @hard_block_v4"
            f' counter log prefix "{_LOG_HARD_DROP_PREFIX}" drop',
            f"        ip saddr {subnet} ip daddr != {gateway}"
            f' counter log prefix "{_LOG_DENY_PREFIX}" drop',
            f"        ip saddr {subnet} ip daddr {gateway}"
            f" tcp dport != {port}"
            f' counter log prefix "{_LOG_DENY_PREFIX}" drop',
            f"        ip saddr {subnet} ip daddr {gateway}"
            f" udp dport != {port}"
            f' counter log prefix "{_LOG_DENY_PREFIX}" drop',
            "    }",
            "",
            "    chain input {",
            "        type filter hook input priority -10; policy accept;",
            f"        ip saddr {subnet} ip daddr != {gateway}"
            f' counter log prefix "{_LOG_DENY_PREFIX}" drop',
            f"        ip saddr {subnet} ip daddr {gateway}"
            f" tcp dport != {port}"
            f' counter log prefix "{_LOG_DENY_PREFIX}" drop',
            f"        ip saddr {subnet} ip daddr {gateway}"
            f" udp dport != {port}"
            f' counter log prefix "{_LOG_DENY_PREFIX}" drop',
            "    }",
            "}",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: emit the nftables ruleset or the effective allowlist to stdout."""
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.egress",
        description="Egress policy emitter (FR-5 / ADR-0060): the generated"
        " ruleset is applied at deployment by scripts/egress_setup.py.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--print-ruleset",
        action="store_true",
        help="print the nftables ruleset for the configured egress subnet",
    )
    group.add_argument(
        "--print-allowlist",
        action="store_true",
        help="print the effective egress domain allowlist, one per line",
    )
    args = parser.parse_args(argv)
    if args.print_allowlist:
        for domain in sorted(load_egress_policy()):
            print(domain)
        return 0
    egress = resolve_sandbox_egress()
    print(build_nftables_ruleset(egress), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
