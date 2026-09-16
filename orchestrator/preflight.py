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

Credential scoping (FR-6, issue #163) is verified FIRST in both modes: the
orchestrator's GitHub token (GH_PAT, falling back to GH_TOKEN/GITHUB_TOKEN)
is validated behaviorally against the Target Repository. GitHub exposes no
API to enumerate a fine-grained PAT's granted permissions, so the preflight
issues the exact read requests the orchestrator's nodes depend on (repo
metadata, branch/head-SHA resolution, judge check-run polling, issues,
pulls) and any refusal becomes a fail-closed gap report naming the
permission to grant (docs/runbook-pat-scoping.md). Write permissions have
no non-destructive probe and are validated by the first cycle;
``python -m orchestrator.preflight --scopes-only`` runs only this
credential check for PAT-migration validation on any machine.
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
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import Message
from pathlib import Path
from urllib.parse import urlsplit

from orchestrator.git import get_remote_url
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

# PAT scope validation (FR-6, issue #163). GitHub exposes no API to
# enumerate a fine-grained PAT's granted permissions (community discussion
# #156115), so validation is behavioral: each probe is a read request the
# orchestrator's nodes actually issue, and a refusal is a gap. Transport
# failures raise SandboxUnavailableError — the supervisor's crash backoff
# retries the whole start, so probes deliberately do not retry internally.
_PAT_PROBE_TIMEOUT = 10
_GITHUB_API_BASE = "https://api.github.com"


class TokenScopeError(SandboxError):
    """The GitHub PAT lacks required Target-Repository permissions (FR-6)."""


@dataclass(frozen=True)
class ScopeProbe:
    """One behavioral permission probe against the Target Repository.

    ``permission`` names the fine-grained PAT permission the probe
    validates, ``reason`` the orchestrator function depending on it, and
    ``path`` the GitHub REST path template (``{repo}``/``{branch}``/
    ``{sha}`` placeholders). ``feeds``/``json_field`` optionally extract a
    value from a successful probe's JSON body to fill later probes'
    placeholders (default branch → head SHA).
    """

    permission: str
    reason: str
    path: str
    feeds: str | None = None
    json_field: str | None = None


REQUIRED_PAT_SCOPE_PROBES: tuple[ScopeProbe, ...] = (
    ScopeProbe(
        permission="Metadata (read)",
        reason="resolves the Target Repository and its default branch "
        "(issue polling and PR operations)",
        path="/repos/{repo}",
        feeds="branch",
        json_field="default_branch",
    ),
    ScopeProbe(
        permission="Contents (read)",
        reason="workspace clone/fetch and head-SHA resolution (Contents RW "
        "per FR-6; the write half is validated by the first cycle)",
        path="/repos/{repo}/commits/{branch}",
        feeds="sha",
        json_field="sha",
    ),
    ScopeProbe(
        permission="Checks (read)",
        reason="Merge-Node judge check-run polling — missing access breaks "
        "merge gating silently (ADR-0014)",
        path="/repos/{repo}/commits/{sha}/check-runs?per_page=1",
    ),
    ScopeProbe(
        permission="Issues (read)",
        reason="issue polling, claiming, and label updates (Issues RW per FR-6)",
        path="/repos/{repo}/issues?per_page=1&state=all",
    ),
    ScopeProbe(
        permission="Pull requests (read)",
        reason="PR creation and judge-review polling (Pull requests RW per FR-6)",
        path="/repos/{repo}/pulls?per_page=1&state=all",
    ),
)


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


def resolve_scope_target_repo(workspace: Path | None = None) -> str | None:
    """Resolve the Target Repository slug for scope probing, or ``None``.

    Uses ``GITHUB_REPOSITORY`` when set; falls back to the orchestrator's
    own ``origin`` remote (the documented self-target default). Returns
    ``None`` when neither is available — the scope check then skips with a
    warning instead of failing (the supervised start requires a token via
    environment checks; standalone runs keep working without GitHub setup).
    """
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if repo:
        return repo
    try:
        url = get_remote_url(workspace or Path(__file__).resolve().parent.parent)
    except Exception:
        return None
    # Parse owner/repo from SSH (git@github.com:owner/repo.git) or HTTPS
    # (https://github.com/owner/repo.git) remote URLs (mirrors nodes.py).
    if "github.com/" in url:
        part = url.split("github.com/", 1)[1]
    elif "github.com:" in url:
        part = url.split("github.com:", 1)[1]
    else:
        return None
    return part.removesuffix(".git")


def describe_token_type(token: str, headers: Message | None) -> str:
    """Classify the token for the preflight log (informational, not gating).

    Fine-grained PATs carry a ``github_pat_`` prefix; classic tokens
    (``ghp_``/``gho_``) answer every authenticated request with an
    ``x-oauth-scopes`` header that fine-grained tokens never send —
    together these make the migration state observable at startup
    (GitHub community discussions #156115 / #25259).
    """
    if token.startswith("github_pat_"):
        return "fine-grained PAT"
    if token.startswith(("ghp_", "gho_")):
        scopes = (headers.get("x-oauth-scopes") or "").strip() if headers else ""
        if scopes:
            return f"classic token (scopes: {scopes})"
        return "classic token (no x-oauth-scopes header)"
    if token.startswith("ghs_"):
        return "GitHub App installation token"
    return "unrecognized token format"


def _probe_endpoint(path: str, token: str) -> tuple[int, Message | None, str]:
    """GET one GitHub REST endpoint; return ``(status, headers, body)``.

    HTTP error statuses are resolved into status/headers/body (a 403
    refusal carries ``X-Accepted-GitHub-Permissions`` naming the required
    permission — the basis of the actionable gap report). Transport-level
    failures (DNS, connection refused, timeout) raise
    ``SandboxUnavailableError``: the supervisor's crash backoff retries the
    whole start, so the probe deliberately does not retry internally.
    """
    request = urllib.request.Request(
        f"{_GITHUB_API_BASE}{path}",
        headers={
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"Bearer {token.strip()}",
            "User-Agent": "agentic-developer-core",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            request, timeout=_PAT_PROBE_TIMEOUT
        ) as response:  # nosemgrep
            return (
                response.status,
                response.headers,
                response.read().decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        return e.code, e.headers, body
    except OSError as e:
        raise SandboxUnavailableError(
            f"GitHub API unreachable during PAT scope validation: {e} — "
            "the supervisor retries the start with backoff"
        ) from e


def _json_field(body: str, field: str, path: str) -> str:
    """Extract a non-empty string field from a probe's JSON body.

    A 200 response with an unexpected shape means the endpoint answered
    with something the probe contract does not model — fail closed with an
    actionable message rather than probing downstream with bogus values.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        value = None
    else:
        value = payload.get(field) if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        raise TokenScopeError(
            f"Unexpected response from GET {path}: no usable {field!r} in "
            "the response body — the Target Repository's identity cannot "
            "be verified (fail closed)"
        )
    return value


def _gap_report(repo: str, gaps: list[str]) -> str:
    """Assemble the fail-closed gap report for the FR-6 scope validation."""
    lines = [
        f"PAT scope validation FAILED for {repo} ({len(gaps)} of "
        f"{len(REQUIRED_PAT_SCOPE_PROBES)} required permissions missing or "
        "unreachable). Refusing to start: missing permissions break polling "
        "and merge gating silently mid-cycle (fail closed, ADR-0056 FR-6).",
        *gaps,
        "Fix: regenerate the fine-grained PAT with the listed permissions "
        "granted on the Target Repository only — step-by-step guide: "
        "docs/runbook-pat-scoping.md",
    ]
    if any("check-runs" in gap for gap in gaps):
        lines.append(
            "Note: a failing Checks probe can also be the known GitHub "
            "platform gap — the REST API docs list 'Checks (read)' for "
            "fine-grained PATs, but the permission is not grantable for "
            "some fine-grained tokens (community discussion #129512, "
            "gh cli #8842). Supported postures: keep the classic token "
            "(rollback, see runbook) or use a GitHub App."
        )
    return "\n".join(lines)


def check_pat_scopes() -> None:
    """Validate the orchestrator GitHub token against the FR-6 matrix.

    Behavioral validation (see ``REQUIRED_PAT_SCOPE_PROBES``): GitHub offers
    no scope-enumeration API for fine-grained PATs, so the preflight probes
    the exact read requests the orchestrator's nodes depend on. Any refusal
    raises ``TokenScopeError`` naming the permission to grant — fail closed,
    because a scope gap breaks issue polling or merge gating silently
    mid-cycle. Write permissions (Contents/Issues/Pull requests/Workflows
    RW) have no non-destructive API probe; the first cycle validates them
    operationally (docs/runbook-pat-scoping.md, section "Validation").

    Skips with a warning when no GitHub token is configured or the target
    repository cannot be resolved: the supervised path guarantees both
    (environment validation runs first), while standalone/dev runs without
    GitHub setup keep working.
    """
    token = os.getenv("GH_PAT") or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token:
        logger.warning(
            "PAT scope validation skipped: no GitHub token configured "
            "(GH_PAT/GH_TOKEN/GITHUB_TOKEN) — the supervised start checks "
            "required variables before invoking this preflight"
        )
        return
    repo = resolve_scope_target_repo()
    if not repo:
        logger.warning(
            "PAT scope validation skipped: GITHUB_REPOSITORY is unset and "
            "the orchestrator's own git remote origin could not be resolved"
        )
        return

    values: dict[str, str] = {"repo": repo}
    gaps: list[str] = []
    token_described = False
    for probe in REQUIRED_PAT_SCOPE_PROBES:
        try:
            path = probe.path.format(**values)
        except KeyError:
            # A prerequisite probe failed and its gap is already recorded;
            # downstream probes cannot be built without it.
            continue
        status, headers, body = _probe_endpoint(path, token)
        if status == 401:
            raise TokenScopeError(
                "GitHub rejected the configured token (HTTP 401 on GET "
                f"{path}): invalid, expired, or revoked — regenerate it per "
                "docs/runbook-pat-scoping.md before starting"
            )
        if status != 200:
            detail = f"HTTP {status}"
            accepted = (
                (headers.get("X-Accepted-GitHub-Permissions") or "").strip()
                if headers
                else ""
            )
            if accepted:
                detail += f" (required permission per GitHub: {accepted})"
            gaps.append(
                f"- {probe.permission}: GET {path} -> {detail} [{probe.reason}]"
            )
            continue
        logger.info("PAT scope probe OK (%s): GET %s", probe.permission, path)
        if not token_described:
            logger.info(
                "Token under validation: %s.", describe_token_type(token, headers)
            )
            token_described = True
        if probe.feeds and probe.json_field:
            values[probe.feeds] = _json_field(body, probe.json_field, path)
    if gaps:
        raise TokenScopeError(_gap_report(repo, gaps))
    logger.info(
        "PAT scope validation passed for %s: all %d required read probes "
        "succeeded. Write permissions (Contents/Issues/Pull requests/"
        "Workflows RW) have no non-destructive API probe — the first cycle "
        "validates them (docs/runbook-pat-scoping.md).",
        repo,
        len(REQUIRED_PAT_SCOPE_PROBES),
    )


def run_preflight() -> str:
    """Verify the PAT scopes and the sandbox runtime; return the name.

    Credential scoping (FR-6) is checked first via ``check_pat_scopes``
    (skips with a warning when no token/repository is configured). All
    remaining checks verify the configured sandbox runtime (FR-4) and its
    egress enforcement (FR-5), raising ``SandboxError`` subclasses with
    actionable messages when unusable (fail closed). The Process Supervisor
    turns a non-zero ``python -m orchestrator.preflight`` exit into a
    refused start.
    """
    check_pat_scopes()
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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (``python -m orchestrator.preflight``): exit 0/1.

    ``--scopes-only`` runs only the FR-6 PAT scope validation — the
    per-machine validation step of the PAT migration runbook — and skips
    every sandbox runtime/egress check.
    """
    args = sys.argv[1:] if argv is None else argv
    scopes_only = "--scopes-only" in args
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    runtime = os.getenv("AGENT_SANDBOX_RUNTIME", DEFAULT_SANDBOX_RUNTIME)
    try:
        if scopes_only:
            check_pat_scopes()
        else:
            run_preflight()
    except SandboxError as e:
        logger.error(
            "Execution Sandbox preflight FAILED (runtime=%s%s): %s",
            runtime,
            ", scopes-only" if scopes_only else "",
            e,
        )
        return 1
    return 0


def _snippet(raw: bytes | None, limit: int = 300) -> str:
    text = (raw or b"").decode("utf-8", errors="replace").strip()
    return text[:limit] if text else "<no output>"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
