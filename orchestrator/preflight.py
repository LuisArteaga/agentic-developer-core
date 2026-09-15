"""Execution Sandbox runtime preflight (ADR-0056 FR-4, issue #161).

Fails closed at startup: when the configured sandbox runtime is not usable,
the orchestrator refuses to run instead of silently degrading to
shared-kernel containers. The Process Supervisor (ADR-0015) invokes this
module as ``python -m orchestrator.preflight`` in its validation phase —
as a subprocess, not an import, preserving the supervisor's
zero-dependency, no-orchestrator-import design (it already spawns
``python -m orchestrator`` the same way).

Checks, in order (runsc mode — the default):

1. Docker daemon reachable (``docker info``) — the control plane talks to
   the daemon through the mounted Docker socket; without it no sandbox can
   ever start.
2. Host kernel ≥ 5.6 — gVisor's minimum kernel (gvisor.dev install guide,
   T1). Valid inside the control-plane container too: containers share the
   host kernel, so ``platform.release()`` reports the deployment host's
   kernel.
3. ``runsc`` binary on PATH (skipped inside the control-plane container —
   the binary lives on the Docker *host*, not in the orchestrator image;
   there the daemon-side runtime registration below is the authoritative
   signal that the binary exists).
4. Runtime registered with the daemon (``docker info`` → ``Runtimes``) —
   catches the "binary installed but daemon not restarted" case, since
   ``runsc install`` only writes the entry and a daemon restart applies it.

``AGENT_SANDBOX_RUNTIME=runc`` (the explicit dev-only override) skips the
gVisor-specific checks and verifies only daemon reachability, after logging
a loud multi-line warning about the shared-kernel trade-off.

Egress enforcement (FR-5, issue #162 / ADR-0060) is verified in BOTH
runtime modes: the dedicated egress network must exist with the configured
subnet (the nftables ruleset is generated for that subnet — a drift would
leave sandboxes unpoliced), and the host-side egress proxy must accept TCP
connections. A missing network or unreachable proxy refuses the start —
fail closed, matching the runtime posture.
"""

import ipaddress
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from orchestrator.sandbox import (
    DEFAULT_PROXY_PROBE_URL,
    DEFAULT_SANDBOX_RUNTIME,
    SANDBOX_RUNTIME_OVERRIDE,
    SandboxConfigError,
    SandboxError,
    SandboxUnavailableError,
    resolve_sandbox_egress,
    resolve_sandbox_runtime,
)

logger = logging.getLogger("orchestrator.preflight")

# gVisor requires Linux 5.6 or newer (gvisor.dev install guide, T1).
MINIMUM_KERNEL_VERSION = (5, 6)

# Files that mark the current process as running inside a container. The
# preflight runs inside the control-plane container in cloud deployments,
# where ``which runsc`` inspects the wrong filesystem (the binary lives on
# the Docker host); the daemon-side runtime registration is checked instead.
CONTAINER_MARKER_PATHS = ("/.dockerenv", "/run/.containerenv")

_DAEMON_PROBE_TIMEOUT = 15
_RUNTIMES_PROBE_TIMEOUT = 15

# docker info format strings — the daemon is the single source of truth for
# both reachability and registered runtimes.
_DAEMON_VERSION_FORMAT = "{{.ServerVersion}}"
_RUNTIMES_FORMAT = "{{json .Runtimes}}"


def running_in_control_plane_container() -> bool:
    """True when this process runs inside a (control-plane) container.

    Uses the well-known container marker files rather than cgroup parsing:
    the markers are cheap, dependency-free and stable across Docker and
    Podman. In that context the host filesystem is not visible, so the
    ``runsc`` binary check is skipped (see module docstring).
    """
    return any(Path(marker).is_file() for marker in CONTAINER_MARKER_PATHS)


def _probe_daemon_version() -> None:
    """Raise unless the Docker CLI reaches the daemon (``docker info``)."""
    try:
        probe = subprocess.run(
            ["docker", "info", "--format", _DAEMON_VERSION_FORMAT],
            capture_output=True,
            timeout=_DAEMON_PROBE_TIMEOUT,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise SandboxUnavailableError(f"docker daemon probe failed: {e}") from e
    if probe.returncode != 0:
        raise SandboxUnavailableError(
            "docker daemon unreachable (start Docker / check the mounted "
            "socket): " + _snippet(probe.stderr)
        )


def _probe_registered_runtimes() -> dict:
    """Return the daemon's registered runtimes (``docker info`` JSON map)."""
    try:
        probe = subprocess.run(
            ["docker", "info", "--format", _RUNTIMES_FORMAT],
            capture_output=True,
            timeout=_RUNTIMES_PROBE_TIMEOUT,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise SandboxUnavailableError(f"docker daemon probe failed: {e}") from e
    if probe.returncode != 0:
        raise SandboxUnavailableError(
            "docker daemon unreachable while listing runtimes: "
            + _snippet(probe.stderr)
        )
    try:
        runtimes = json.loads(probe.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise SandboxConfigError(
            "docker info returned an unparseable runtimes listing; the "
            "sandbox runtime cannot be verified — " + _snippet(probe.stdout)
        ) from None
    if not isinstance(runtimes, dict):
        raise SandboxConfigError(
            "docker info returned an unexpected runtimes listing; the "
            "sandbox runtime cannot be verified"
        )
    return runtimes


def _check_kernel_version() -> None:
    """Raise unless the host kernel meets gVisor's minimum (≥ 5.6)."""
    release = platform.release()
    parts = release.split(".")
    if len(parts) < 2:
        raise SandboxConfigError(
            f"Cannot determine the host kernel version from {release!r}; "
            "gVisor requires Linux >= 5.6 (see the ops runbook)"
        )
    try:
        version = (int(parts[0]), int(parts[1]))
    except ValueError:
        raise SandboxConfigError(
            f"Cannot parse the host kernel version from {release!r}; "
            "gVisor requires Linux >= 5.6 (see the ops runbook)"
        ) from None
    if version < MINIMUM_KERNEL_VERSION:
        raise SandboxConfigError(
            f"gVisor requires a Linux kernel >= {MINIMUM_KERNEL_VERSION[0]}."
            f"{MINIMUM_KERNEL_VERSION[1]}, but the host runs "
            f"{release}; upgrade the deployment host's kernel (see the ops "
            "runbook)"
        )


def _check_runsc_binary() -> None:
    """Raise unless the ``runsc`` binary is on PATH (host context only)."""
    if shutil.which("runsc") is None:
        raise SandboxConfigError(
            "runsc binary not found on PATH: install gVisor per the ops "
            "runbook (docs/runbook-sandbox-runtime.md, apt repo section) — "
            "the orchestrator refuses to run without the configured sandbox "
            "runtime (fail closed, ADR-0056 FR-4)"
        )


def _check_runtime_registered() -> None:
    """Raise unless the daemon has ``runsc`` among its registered runtimes."""
    runtimes = _probe_registered_runtimes()
    if DEFAULT_SANDBOX_RUNTIME not in runtimes:
        binary_hint = (
            "the runsc binary is installed but the daemon has not picked it "
            "up — finish registration with `runsc install` and restart "
            "Docker (`sudo systemctl restart docker`)"
            if shutil.which("runsc") is not None
            else "install gVisor per the ops runbook, then restart Docker"
        )
        registered = ", ".join(sorted(runtimes)) or "<none>"
        raise SandboxConfigError(
            f"runsc is not registered with the Docker daemon (registered: "
            f"{registered}). {binary_hint}. See "
            "docs/runbook-sandbox-runtime.md — the orchestrator refuses to "
            "run without the configured sandbox runtime (fail closed, "
            "ADR-0056 FR-4)"
        )


def _check_egress_network(subnet: ipaddress.IPv4Network, network: str) -> None:
    """Raise unless the egress network exists with the configured subnet.

    The nftables ruleset polices sandbox traffic BY SOURCE SUBNET — if the
    live network drifted to a different subnet, the applied rules no longer
    match sandbox traffic and the lockdown is silently void. That mismatch
    is a loud configuration error, never a warning.
    """
    fmt = "{{json .IPAM.Config}}"
    try:
        probe = subprocess.run(
            ["docker", "network", "inspect", "--format", fmt, network],
            capture_output=True,
            timeout=_DAEMON_PROBE_TIMEOUT,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise SandboxUnavailableError(f"docker network inspect failed: {e}") from e
    if probe.returncode != 0:
        raise SandboxUnavailableError(
            f"Egress network {network!r} not found: run "
            "`python3 scripts/egress_setup.py network` (root) per "
            "docs/runbook-sandbox-egress.md — the orchestrator refuses to "
            "run without egress enforcement (fail closed, ADR-0056 FR-5): "
            + _snippet(probe.stderr)
        )
    try:
        ipam = json.loads(probe.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        raise SandboxConfigError(
            f"docker network inspect returned an unparseable IPAM listing "
            f"for {network!r}; the egress subnet cannot be verified — "
            + _snippet(probe.stdout)
        ) from None
    live_subnets = {entry.get("Subnet") for entry in ipam if isinstance(entry, dict)}
    if str(subnet) not in live_subnets:
        raise SandboxConfigError(
            f"Egress network {network!r} drifted: configured subnet "
            f"{subnet} is not among the live subnets {sorted(filter(None, live_subnets))}. "
            "Recreate the network via scripts/egress_setup.py and regenerate "
            "the nftables ruleset (the lockdown polices the CONFIGURED "
            "subnet only — see docs/runbook-sandbox-egress.md)"
        )


def _check_egress_proxy() -> None:
    """Raise unless the host-side egress proxy accepts TCP connections."""
    raw = os.getenv("AGENT_EGRESS_PROXY_PROBE_URL", DEFAULT_PROXY_PROBE_URL).strip()
    parsed = urlsplit(raw if "//" in raw else f"//{raw}")
    host = parsed.hostname
    port = parsed.port or 3128
    if not host or parsed.scheme not in ("", "http"):
        raise SandboxConfigError(
            f"AGENT_EGRESS_PROXY_PROBE_URL {raw!r} must be an http URL (no "
            "scheme defaults to http), e.g. http://127.0.0.1:3128"
        )
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError as e:
        raise SandboxUnavailableError(
            f"Egress proxy unreachable at {host}:{port} ({e}): start it per "
            "docs/runbook-sandbox-egress.md (host systemd unit, "
            "`python -m orchestrator.egress_proxy`) — the orchestrator "
            "refuses to run without the sandbox egress proxy (fail closed, "
            "ADR-0056 FR-5)"
        ) from e


def run_preflight() -> str:
    """Verify the configured sandbox runtime; return the runtime name.

    Raises ``SandboxError`` subclasses with actionable messages when the
    runtime is not usable (fail closed). The Process Supervisor turns a
    non-zero ``python -m orchestrator.preflight`` exit into a refused start.
    """
    runtime = resolve_sandbox_runtime()
    _probe_daemon_version()
    egress = resolve_sandbox_egress()
    _check_egress_network(egress.subnet, egress.network)
    _check_egress_proxy()
    logger.info(
        "Egress enforcement verified: network %s (%s) + proxy reachable.",
        egress.network,
        egress.subnet,
    )
    if runtime == SANDBOX_RUNTIME_OVERRIDE:
        logger.warning(
            "*** SANDBOX PREFLIGHT: shared-kernel runtime runc ACTIVE — "
            "gVisor (runsc) isolation is DISABLED by explicit "
            "AGENT_SANDBOX_RUNTIME=runc (dev-only documented trade-off, "
            "ADR-0056 FR-4) ***"
        )
        return runtime
    _check_kernel_version()
    if running_in_control_plane_container():
        logger.info(
            "Control-plane container detected: skipping the host-side runsc "
            "binary check; daemon-side runtime registration is the "
            "authoritative signal"
        )
    else:
        _check_runsc_binary()
    _check_runtime_registered()
    logger.info("Sandbox runtime %s is available and registered.", runtime)
    return runtime


def main() -> int:
    """CLI entry point (``python -m orchestrator.preflight``): exit 0/1."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    runtime = os.getenv("AGENT_SANDBOX_RUNTIME", DEFAULT_SANDBOX_RUNTIME)
    try:
        run_preflight()
    except SandboxError as e:
        logger.error(
            "Execution Sandbox preflight FAILED (runtime=%s): %s",
            runtime,
            e,
        )
        return 1
    return 0


def _snippet(raw: bytes | None, limit: int = 300) -> str:
    text = (raw or b"").decode("utf-8", errors="replace").strip()
    return text[:limit] if text else "<no output>"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
