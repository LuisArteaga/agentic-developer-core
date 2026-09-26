"""Execution Sandbox runner (ADR-0056 slice 1, issue #159).

Implements the swappable sandbox interface (FR-1) and routes the Verify
phase's deterministic gate through it (FR-2,
docs/cloud-deployment-requirements.md): create (from image, workspace mount,
resource caps), exec (command, timeout, bounded-output capture), destroy
(idempotent cleanup). The sandbox receives the workspace and nothing else —
the orchestrator's environment never crosses the plane boundary, so secrets
cannot leak into untrusted execution (extends ADR-0043's allowlist semantics
to sandboxes; FR-6's regression invariant is enforced by
``FORBIDDEN_SANDBOX_ENV_NAMES`` + ``test_sandbox.py``).

Backend mechanics (Docker via the ``docker`` CLI — no Python Docker SDK
dependency, same subprocess pattern as ``orchestrator.git``, ADR-0007):

- ``create`` validates the runtime loudly: daemon reachable
  (``docker info`` → ``SandboxUnavailableError``) and configured image
  present (``docker image inspect`` → ``SandboxImageMissingError``).
- ``exec`` spawns a fresh, uniquely named container per command
  (``docker run --name <id>``). Rationale: killing the ``docker run`` client
  process does NOT kill the container, so a per-exec container plus
  ``docker rm -f`` is the only way to guarantee that a command exceeding the
  exec timeout is actually killed while partial output is still returned;
  and after a timeout the retry path needs a working sandbox, which a
  killed persistent container could not provide. Workspace state survives
  across execs because it lives in the read-write bind mount, not in
  container-internal paths.
- Every ``exec`` failure that means "the command did not run to completion"
  raises ``SandboxError`` (infrastructure), never a green/red exit code.
  The docker client's exit code alone is ambiguous — daemon errors also
  surface as client exit 1, and 125/126/127 are docker-reserved — so a
  non-zero ``docker run`` exit is classified by inspecting the container's
  recorded ``State`` (Status == "exited" → the command ran; its ExitCode is
  data). This makes a false-green verify structurally impossible. A completed
  exec container is removed as soon as its command has been classified
  (``remove_sandbox_container``) — success or data exit alike — so at most one
  in-flight container exists per runner at any time. Rationale (FR-7, issue
  #164): a stopped container keeps its writable layer, so a Worker command
  that installs packages would otherwise accumulate megabytes per exec until
  the cycle ends, and a crash would hand every one of them to the startup
  sweep. ``destroy`` therefore only has leftovers to remove (an in-flight
  container, or one whose removal failed).
- ``destroy`` removes any container this runner instance still tracks and is
  idempotent; it is best-effort — each removal is retried once and a
  persistent failure is logged loudly with the container name (manual cleanup
  is ``docker rm -f <name>``) — and never raises.

Fail closed (issue #159 constraint, ADR-0056): there is NO host-side
fallback. ``get_sandbox_runner`` only returns the Docker backend; an
unsupported ``AGENT_SANDBOX_BACKEND`` value raises ``SandboxConfigError``
(the dev-only host backend of FR-9 is a separate follow-up slice). Runner
infrastructure failures are caught by ``verify_node`` and routed into
Recovery per ADR-0038 without consuming the make-verify retry budget.

Runtime hardening (FR-4, issue #161): every sandbox container starts with
an explicit ``--runtime`` flag (default ``runsc`` — the gVisor user-space
kernel). The configured runtime is resolved by ``resolve_sandbox_runtime``;
``AGENT_SANDBOX_RUNTIME=runc`` is the explicit, loudly-warned dev-only
override that trades gVisor isolation for shared-kernel containers. Startup
preflight lives in ``orchestrator.preflight`` (verifies daemon, kernel,
binary and daemon-side runtime registration before the first cycle and is
wired into the Process Supervisor's validation phase — fail closed).

Egress lockdown (FR-5, issue #162 / ADR-0060): every sandbox container is
attached to the dedicated, ICC-disabled egress network (``--network``) and
receives HTTP(S)_PROXY env pointing at the host-side egress proxy — the
only domain-capable route out (orchestrator.egress_proxy). There is no
disable knob: a sandbox without egress enforcement is a misconfiguration,
not a mode (fail closed). The proxy and the host nftables ruleset are
generated from the same policy (``orchestrator.egress``) and applied at
deployment time (``scripts/egress_setup.py``); the preflight verifies
network + proxy reachability at startup in both runtime modes.
"""

import ipaddress
import json
import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

logger = logging.getLogger("orchestrator.sandbox")

# Fixed in-container location of the read-write workspace bind mount (FR-2).
SANDBOX_WORKSPACE_MOUNT = "/workspace"

DEFAULT_SANDBOX_IMAGE = "python:3.12"
DEFAULT_SANDBOX_CPUS = "2"
DEFAULT_SANDBOX_MEMORY = "4g"
DEFAULT_SANDBOX_PIDS_LIMIT = "512"
# Bounded tmpfs for the container's scratch space (FR-7, issue #164): the
# image's own /tmp lives in the container's writable layer, so without a cap
# a runaway build can fill the host disk. Docker's default tmpfs ceiling is
# 50% of host RAM (docs.docker.com/engine/storage/tmpfs), which is not a cap
# in any useful sense on a 16 GB deployment host — hence an explicit size.
DEFAULT_SANDBOX_TMPFS_SIZE = "512m"

# Cycle-ownership labels (FR-7, issue #164). Every sandbox container carries
# the managed marker; the cycle label names the issue cycle that owns it, so
# the startup sweep can tell this deployment's orphans from containers it
# never spawned. Label keys are pinned by orchestrator/test_sandbox.py.
SANDBOX_MANAGED_LABEL = "agdc.sandbox.managed"
SANDBOX_CYCLE_LABEL = "agdc.sandbox.cycle"

# Removal attempts for one container (the first try plus one retry) before the
# failure is escalated to an operator-facing ERROR (FR-7, issue #164).
_REMOVE_ATTEMPTS = 2

# ``docker rm -f`` reports an already-absent container on a non-zero exit
# ("Error response from daemon: No such container: <name>"). That is the one
# non-zero exit whose postcondition already holds, so it counts as gone;
# treating it as a failure would raise a manual-cleanup ERROR for a container
# that does not exist (the startup sweep's listing-to-removal race) and keep
# the name in the runner's leftovers list forever.
_ALREADY_GONE_MARKER = "no such container"

# Container runtime for sandbox containers (FR-4, issue #161). The default
# ``runsc`` (gVisor user-space kernel) is a hard requirement: when it is not
# available the preflight refuses to start the orchestrator instead of
# silently degrading to shared-kernel containers. ``runc`` is the explicit
# dev-only override (loud warning, documented trade-off) — never a default.
DEFAULT_SANDBOX_RUNTIME = "runsc"
SANDBOX_RUNTIME_OVERRIDE = "runc"

# Egress lockdown defaults (FR-5, issue #162 / ADR-0060). Sandboxes attach to
# a dedicated Docker network with a fixed private subnet (no IPv6) and reach
# the outside world only through the host-side egress proxy listening on the
# network's gateway IP. The gateway is derived from the subnet (Docker
# assigns the first usable host as gateway), so one knob governs both the
# preflight's drift check and the injected proxy env.
DEFAULT_SANDBOX_EGRESS_NETWORK = "agdc-sandbox-egress"
DEFAULT_SANDBOX_EGRESS_SUBNET = "172.30.0.0/24"
DEFAULT_EGRESS_PROXY_PORT = 3128
# The subnet-derived default proxy URL (first usable host of the default
# subnet + proxy port); used as the constructor fallback so the runner's
# defaults stay environment-independent.
DEFAULT_SANDBOX_EGRESS_PROXY_URL = "http://172.30.0.1:3128"
DEFAULT_PROXY_PROBE_URL = "http://127.0.0.1:3128"
_PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

# Credential env var names that must NEVER be requested into a sandbox
# environment (FR-6). This is not an env-construction mechanism (ADR-0043's
# denylist prohibition concerns pass-everything-then-strip construction); it
# is loud config validation on the opt-in allowlist override: requesting one
# of these names raises instead of silently filtering, so the invariant "the
# sandbox environment contains none of these under any configuration" holds
# by construction. The set is the documented orchestrator secret inventory
# (.env.example + consumers: GH_PAT/GH_TOKEN/GITHUB_TOKEN tokens, OpenRouter,
# Langfuse, SmithDB, and the OTLP headers string, whose value may embed an
# Authorization header) — orchestrator/test_sandbox.py pins this set to the
# documented inventory so a newly introduced secret cannot bypass it.
# JUDGE_GH_TOKEN is deliberately absent: it is a Target-Repository CI secret
# consumed by the judges' own workflow runs, never an orchestrator env var.
FORBIDDEN_SANDBOX_ENV_NAMES = frozenset(
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


class SandboxError(Exception):
    """Base class for Execution Sandbox runner failures (infrastructure)."""


class SandboxConfigError(SandboxError):
    """Invalid sandbox configuration (backend value, caps, env override)."""


class SandboxUnavailableError(SandboxError):
    """The sandbox runtime is unreachable (e.g. docker daemon down)."""


class SandboxImageMissingError(SandboxError):
    """The configured sandbox image is not present on the host."""


# Resource caps that can stop a command inside the sandbox (FR-7, issue
# #164) and the actionable explanation each one owes the Worker. A cap hit is
# a *failed attempt*, never runner infrastructure: the command did not run to
# completion, so the deterministic gate stays red, but the raw output alone
# ("Killed") tells the Worker nothing about how to proceed.
CAP_HIT_MESSAGES = {
    "memory": (
        "the sandbox hit its memory cap (AGENT_SANDBOX_MEMORY) and the kernel "
        "killed the command — reduce the command's memory footprint or raise "
        "the cap for this deployment"
    ),
    "pids": (
        "the sandbox hit its process cap (AGENT_SANDBOX_PIDS_LIMIT) and could "
        "not fork — a runaway process tree (fork bomb) or an unbounded test "
        "parallelism, not a missing dependency"
    ),
    "disk": (
        "the sandbox ran out of disk space (bounded tmpfs scratch space, "
        "AGENT_SANDBOX_TMPFS_SIZE) — reduce the artifact written, or raise the "
        "cap for this deployment"
    ),
}


def classify_cap_hit(exit_code: int, state: dict | None, output: str) -> str | None:
    """Name the resource cap that stopped a command, if any (FR-7, #164).

    Returns ``"memory"``, ``"pids"``, ``"disk"`` or ``None``. The signals are
    deliberately conservative — a false positive would misdirect the Worker,
    while a missed cap hit merely degrades to raw output:

    - ``memory`` — Docker recorded ``OOMKilled`` on the container. The
      authoritative signal: exit code 137 is ``SIGKILL`` and is *not*
      sufficient on its own, since an external ``docker kill`` produces the
      same code with ``OOMKilled: false``.
    - ``pids`` — the command exited non-zero and reported a fork failure in
      the kernel's own EAGAIN wording ("Resource temporarily unavailable",
      usually after the shell's "can't fork:"/"unable to fork"). The exit code
      is deliberately *not* pinned: the kernel rejects the fork rather than
      killing the container, so the shell reports an ordinary failure with its
      own code (busybox ``sh`` exits 2, bash 1), and a live probe on a
      pids-limited container produced exactly
      ``sh: can't fork: Resource temporarily unavailable`` with exit ``2``.
    - ``disk`` — the output reports ``ENOSPC`` ("No space left on device").

    Every classification requires a non-zero exit: a command that exited 0 ran
    to its own completion, so a marker in its output is something it handled or
    warned about, not the cap that stopped it (the caller only consults this on
    the failed-exit path anyway).
    """
    if exit_code == 0:
        return None
    if state is not None and state.get("OOMKilled") is True:
        return "memory"
    lowered = output.lower()
    if "no space left on device" in lowered:
        return "disk"
    if any(
        marker in lowered
        for marker in (
            "resource temporarily unavailable",
            "unable to fork",
            "cannot fork",
            "failed to fork",
        )
    ):
        return "pids"
    return None


def cap_hit_message(cap_hit: str) -> str:
    """Actionable explanation of a cap hit for the Worker (FR-7, #164)."""
    return CAP_HIT_MESSAGES.get(
        cap_hit, "the sandbox's resource caps stopped the command"
    )


@dataclass(frozen=True)
class SandboxExecResult:
    """Outcome of one exec inside the sandbox.

    ``exit_code`` is the command's own exit status — a non-zero value is
    data (a failed verification), not a runner failure. ``output`` is the
    combined stdout+stderr stream, decoded with errors replaced. ``timed_out``
    marks that the command was killed at the exec timeout with partial
    output preserved (``exit_code`` is then the ``-1`` sentinel used by the
    Verify phase).

    ``cap_hit`` names the resource cap that stopped the command (``"memory"``,
    ``"pids"`` or ``"disk"``, see :func:`classify_cap_hit`) or ``None`` for a
    command that ran to its own exit. A cap hit is data like any other failed
    exit — the command did not complete, so the attempt fails — but the caller
    owes the Worker an actionable explanation instead of raw output (FR-7).
    """

    exit_code: int
    output: str
    timed_out: bool = False
    duration_seconds: float = 0.0
    cap_hit: str | None = None


class SandboxRunner(Protocol):
    """Swappable Execution Sandbox interface (ADR-0056, FR-1).

    ``create`` validates the runtime and binds the workspace; ``exec`` runs a
    command inside the sandbox and returns its outcome; ``destroy`` cleans up
    idempotently and is safe to call more than once or never after create.
    """

    def create(self, workspace: Path) -> None: ...

    def exec(self, args: list[str], timeout: float) -> SandboxExecResult: ...

    def destroy(self) -> None: ...


def build_sandbox_env() -> dict[str, str]:
    """Build the environment passed into the sandbox (ADR-0043 semantics).

    Default is EMPTY: the sandbox receives no orchestrator environment
    variable at all (the container image's own PATH/HOME defaults apply —
    host values would be wrong inside the container anyway). Deployments may
    add specific non-secret vars via ``AGENT_SANDBOX_ENV_ALLOWLIST``
    (comma-separated names, mirroring ``AGENT_SUBPROCESS_ENV_ALLOWLIST``);
    only names actually present in ``os.environ`` are forwarded. Requesting
    a credential name fails loudly with ``SandboxConfigError``.
    """
    override = os.getenv("AGENT_SANDBOX_ENV_ALLOWLIST", "")
    requested = [name.strip() for name in override.split(",") if name.strip()]
    forbidden = sorted(set(requested) & FORBIDDEN_SANDBOX_ENV_NAMES)
    if forbidden:
        raise SandboxConfigError(
            "AGENT_SANDBOX_ENV_ALLOWLIST may not request credential vars: "
            + ", ".join(forbidden)
        )
    return {name: os.environ[name] for name in requested if name in os.environ}


def _resolve_sandbox_caps() -> tuple[str, str, str, str]:
    """Resolve resource caps from the environment with loud validation."""
    cpus = os.getenv("AGENT_SANDBOX_CPUS", DEFAULT_SANDBOX_CPUS).strip()
    memory = os.getenv("AGENT_SANDBOX_MEMORY", DEFAULT_SANDBOX_MEMORY).strip()
    pids_limit = os.getenv(
        "AGENT_SANDBOX_PIDS_LIMIT", DEFAULT_SANDBOX_PIDS_LIMIT
    ).strip()
    tmpfs_size = os.getenv(
        "AGENT_SANDBOX_TMPFS_SIZE", DEFAULT_SANDBOX_TMPFS_SIZE
    ).strip()
    try:
        cpus_value = float(cpus)
    except ValueError:
        cpus_value = 0.0
    if cpus_value <= 0:
        raise SandboxConfigError(
            f"AGENT_SANDBOX_CPUS must be a positive number, got {cpus!r}"
        )
    if not pids_limit.isdigit() or int(pids_limit) <= 0:
        raise SandboxConfigError(
            f"AGENT_SANDBOX_PIDS_LIMIT must be a positive integer, got {pids_limit!r}"
        )
    if not memory:
        raise SandboxConfigError("AGENT_SANDBOX_MEMORY must not be blank")
    # A malformed tmpfs size would silently become a docker CLI usage error at
    # the first exec, i.e. a sandbox that never starts — fail closed here. A
    # zero magnitude is rejected too: it is syntactically fine but makes the
    # scratch space unusable.
    tmpfs_match = re.fullmatch(r"([0-9]+)([kmg]?)", tmpfs_size.lower())
    if tmpfs_match is None or int(tmpfs_match.group(1)) <= 0:
        raise SandboxConfigError(
            "AGENT_SANDBOX_TMPFS_SIZE must be a positive size like '512m' or "
            f"'1g', got {tmpfs_size!r}"
        )
    return cpus, memory, pids_limit, tmpfs_size


def resolve_sandbox_runtime() -> str:
    """Resolve the container runtime for sandbox containers (FR-4, #161).

    Default is ``runsc`` (gVisor): FR-4 requires every sandbox to start with
    ``--runtime=runsc``, and an unavailable runtime must fail closed (the
    startup preflight in ``orchestrator.preflight`` refuses to start the
    orchestrator rather than silently degrading to shared-kernel isolation).

    ``AGENT_SANDBOX_RUNTIME=runc`` is the explicit, loudly-warned dev-only
    override for environments without gVisor (e.g. WSL2 development) — a
    documented trade-off, never a default. Any other value (including a
    blank one) is a loud ``SandboxConfigError``: ambiguity around the
    isolation runtime is never resolved silently.
    """
    raw = os.getenv("AGENT_SANDBOX_RUNTIME", DEFAULT_SANDBOX_RUNTIME)
    value = raw.strip().lower() if raw else ""
    if value == DEFAULT_SANDBOX_RUNTIME:
        return DEFAULT_SANDBOX_RUNTIME
    if value == SANDBOX_RUNTIME_OVERRIDE:
        logger.warning(
            "SANDBOX RUNTIME OVERRIDE ACTIVE: AGENT_SANDBOX_RUNTIME=runc runs "
            "sandboxes as shared-kernel containers WITHOUT gVisor isolation "
            "(dev-only documented trade-off, ADR-0056 FR-4); never set this "
            "in production"
        )
        return SANDBOX_RUNTIME_OVERRIDE
    raise SandboxConfigError(
        f"Unsupported AGENT_SANDBOX_RUNTIME {raw!r}: expected "
        f"{DEFAULT_SANDBOX_RUNTIME!r} (default, gVisor isolation) or "
        f"{SANDBOX_RUNTIME_OVERRIDE!r} (explicit dev-only override)"
    )


@dataclass(frozen=True)
class SandboxEgressConfig:
    """Resolved egress-lockdown configuration (FR-5, ADR-0060).

    ``network`` is the dedicated Docker network every sandbox attaches to;
    ``subnet`` is its fixed IPv4 subnet (the nftables ruleset matches sandbox
    traffic by source subnet); ``proxy_url`` is the host-side egress proxy
    URL injected into the sandbox environment.
    """

    network: str
    subnet: ipaddress.IPv4Network
    proxy_url: str

    @property
    def gateway(self) -> ipaddress.IPv4Address:
        """The bridge gateway IP (Docker assigns the first usable host)."""
        gateway = next(self.subnet.hosts(), None)
        if gateway is None:
            raise SandboxConfigError(
                f"Egress subnet {self.subnet} has no usable host address to "
                "derive the egress proxy gateway from"
            )
        return gateway


def resolve_sandbox_egress() -> SandboxEgressConfig:
    """Resolve the egress-lockdown configuration (FR-5, ADR-0060).

    The proxy URL defaults to ``http://<first-usable-host-of-subnet>:<port>``
    — Docker assigns the first usable host of an explicitly configured
    subnet as the bridge gateway, so ``AGENT_SANDBOX_EGRESS_SUBNET`` is the
    single source of truth for both the preflight's drift check and the
    injected env. ``AGENT_SANDBOX_EGRESS_PROXY_URL`` overrides the URL for
    exotic gateway setups. There is deliberately no disable knob: egress
    lockdown is unconditional (a sandbox attached to a default-allow bridge
    would silently undo FR-5; the documented setup path is the runbook's).

    Every misconfiguration raises ``SandboxConfigError`` loudly: a blank
    network name, an unparseable subnet, a subnet that is not private or
    not IPv4 (the egress network is created without IPv6), or an override
    URL without an http scheme.
    """
    network = os.getenv(
        "AGENT_SANDBOX_EGRESS_NETWORK", DEFAULT_SANDBOX_EGRESS_NETWORK
    ).strip()
    if not network:
        raise SandboxConfigError(
            "AGENT_SANDBOX_EGRESS_NETWORK must not be blank: sandboxes "
            "attach to the dedicated egress network (FR-5); there is no "
            "egress-disabled mode"
        )
    subnet_raw = os.getenv(
        "AGENT_SANDBOX_EGRESS_SUBNET", DEFAULT_SANDBOX_EGRESS_SUBNET
    ).strip()
    try:
        subnet = ipaddress.ip_network(subnet_raw, strict=False)
    except ValueError:
        raise SandboxConfigError(
            f"AGENT_SANDBOX_EGRESS_SUBNET {subnet_raw!r} is not a valid "
            "IP network; the egress ruleset is generated for a fixed "
            "private subnet"
        ) from None
    if subnet.version != 4:
        raise SandboxConfigError(
            f"AGENT_SANDBOX_EGRESS_SUBNET {subnet_raw!r} must be IPv4: the "
            "egress network is created without IPv6 and the generated "
            "nftables ruleset covers the IPv4 sandbox subnet only"
        )
    if not subnet.is_private:
        raise SandboxConfigError(
            f"AGENT_SANDBOX_EGRESS_SUBNET {subnet_raw!r} must be a private "
            "range (RFC 1918 / documentation space) — a public bridge "
            "subnet would defeat the egress boundary"
        )
    default_proxy_url = f"http://{next(subnet.hosts())}:{DEFAULT_EGRESS_PROXY_PORT}"
    proxy_url = os.getenv("AGENT_SANDBOX_EGRESS_PROXY_URL", default_proxy_url).strip()
    if not proxy_url:
        raise SandboxConfigError(
            "AGENT_SANDBOX_EGRESS_PROXY_URL must not be blank; unset it to "
            "derive the proxy URL from the egress subnet gateway"
        )
    parsed = urlsplit(proxy_url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise SandboxConfigError(
            f"AGENT_SANDBOX_EGRESS_PROXY_URL {proxy_url!r} must be an "
            "http://host:port URL pointing at the host-side egress proxy"
        )
    return SandboxEgressConfig(network=network, subnet=subnet, proxy_url=proxy_url)


def get_sandbox_runner(cycle_token: str | None = None) -> SandboxRunner:
    """Return the configured sandbox backend (default: docker).

    ``cycle_token`` is the ownership token of the issue cycle requesting the
    sandbox (FR-7, issue #164). It is stamped onto every container this runner
    spawns as the ``SANDBOX_CYCLE_LABEL`` label, which is what lets the
    startup sweep distinguish this deployment's orphans from containers it
    never spawned. Callers normally do not pass it: the cycle-scoped lifecycle
    (``orchestrator.sandbox_lifecycle``) owns the token and the runner's
    lifetime, so a bare ``get_sandbox_runner()`` is only used by tests and
    tooling that needs an unlabeled runner.

    Fail closed: any ``AGENT_SANDBOX_BACKEND`` value other than ``docker``
    raises ``SandboxConfigError`` — there is deliberately NO host-side
    fallback path (ADR-0056; FR-9's dev-only host backend is a follow-up).
    """
    backend = os.getenv("AGENT_SANDBOX_BACKEND", "docker").strip().lower()
    if backend != "docker":
        raise SandboxConfigError(
            f"Unsupported AGENT_SANDBOX_BACKEND {backend!r}: only 'docker' is "
            "implemented and there is no host-side fallback (fail closed per "
            "ADR-0056); the dev-only host backend is a follow-up slice"
        )
    image = os.getenv("AGENT_SANDBOX_IMAGE", DEFAULT_SANDBOX_IMAGE).strip()
    if not image:
        image = DEFAULT_SANDBOX_IMAGE
    cpus, memory, pids_limit, tmpfs_size = _resolve_sandbox_caps()
    egress = resolve_sandbox_egress()
    return DockerSandboxRunner(
        image=image,
        cpus=cpus,
        memory=memory,
        pids_limit=pids_limit,
        runtime=resolve_sandbox_runtime(),
        egress_network=egress.network,
        egress_proxy_url=egress.proxy_url,
        tmpfs_size=tmpfs_size,
        cycle_token=cycle_token,
    )


class DockerSandboxRunner:
    """Docker-CLI-backed Execution Sandbox (default backend).

    The runner validates loudly at ``create`` and spawns one container per
    ``exec`` (see module docstring for why spawn-per-exec is required for
    timeout and retry semantics).
    """

    _DAEMON_PROBE_TIMEOUT = 15
    _IMAGE_PROBE_TIMEOUT = 30
    _INSPECT_TIMEOUT = 30
    _REMOVE_TIMEOUT = 30

    def __init__(
        self,
        image: str,
        cpus: str,
        memory: str,
        pids_limit: str,
        runtime: str = DEFAULT_SANDBOX_RUNTIME,
        egress_network: str = DEFAULT_SANDBOX_EGRESS_NETWORK,
        egress_proxy_url: str = DEFAULT_SANDBOX_EGRESS_PROXY_URL,
        tmpfs_size: str = DEFAULT_SANDBOX_TMPFS_SIZE,
        cycle_token: str | None = None,
    ):
        self._image = image
        self._cpus = cpus
        self._memory = memory
        self._pids_limit = pids_limit
        self._runtime = runtime
        self._egress_network = egress_network
        self._egress_proxy_url = egress_proxy_url
        self._tmpfs_size = tmpfs_size
        self._cycle_token = cycle_token
        self._workspace: Path | None = None
        self._containers: list[str] = []

    # -- lifecycle -----------------------------------------------------------

    def create(self, workspace: Path) -> None:
        """Validate the Docker runtime and bind the workspace mount.

        Raises ``SandboxUnavailableError`` when the daemon is unreachable and
        ``SandboxImageMissingError`` when the configured image is absent —
        the loud, at-spawn configuration error required by issue #159.
        """
        self._probe_daemon()
        try:
            inspect = subprocess.run(
                ["docker", "image", "inspect", self._image],
                capture_output=True,
                timeout=self._IMAGE_PROBE_TIMEOUT,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise SandboxUnavailableError(f"docker image inspect failed: {e}") from e
        if inspect.returncode != 0:
            raise SandboxImageMissingError(
                f"Sandbox image {self._image!r} not found on the host: "
                f"{stderr_snippet(inspect.stderr)} "
                "(set AGENT_SANDBOX_IMAGE to an image that provides the "
                "target repository's verify toolchain)"
            )
        self._workspace = workspace

    def exec(self, args: list[str], timeout: float) -> SandboxExecResult:
        """Run ``args`` inside the sandbox; non-zero exits are data.

        Every spawn carries the resource caps (CPU/memory/PID/bounded tmpfs)
        and, when the runner was created with a cycle token, the ownership
        labels (FR-7). The container is removed as soon as the outcome has
        been classified, so at most one container per runner exists at a time.

        Raises ``SandboxUnavailableError`` when the daemon is unreachable at
        exec time and ``SandboxError`` when the command could not run to
        completion (never a fabricated exit code).
        """
        if self._workspace is None:
            raise SandboxError("sandbox exec called before create()")
        self._probe_daemon()
        name = f"agdc-sandbox-{uuid.uuid4().hex[:12]}"
        argv = [
            "docker",
            "run",
            "--name",
            name,
            "--cpus",
            self._cpus,
            "--memory",
            self._memory,
            "--pids-limit",
            self._pids_limit,
            # Bounded scratch space (FR-7): /tmp is otherwise the container's
            # writable layer with no ceiling but the host disk.
            "--tmpfs",
            f"/tmp:rw,size={self._tmpfs_size},mode=1777",
            "--runtime",
            self._runtime,
            # Egress lockdown (FR-5, ADR-0060): attach to the dedicated
            # ICC-disabled egress network and route HTTP(S) through the
            # host-side proxy — the only domain-capable path out. Unconditional:
            # there is no egress-disabled mode.
            "--network",
            self._egress_network,
            # Cycle ownership (FR-7, ADR-0061): the managed marker scopes the
            # startup sweep to containers this deployment spawned; the cycle
            # label names the cycle that owns the container, so a sweep keeps
            # the sandboxes of a cycle it is about to resume.
            "--label",
            f"{SANDBOX_MANAGED_LABEL}=1",
        ]
        if self._cycle_token:
            argv += ["--label", f"{SANDBOX_CYCLE_LABEL}={self._cycle_token}"]
        argv += [
            "-v",
            f"{self._workspace}:{SANDBOX_WORKSPACE_MOUNT}",
            "-w",
            SANDBOX_WORKSPACE_MOUNT,
        ]
        for env_name in _PROXY_ENV_NAMES:
            argv += ["-e", f"{env_name}={self._egress_proxy_url}"]
        for key, value in build_sandbox_env().items():
            argv += ["-e", f"{key}={value}"]
        argv += ["--", self._image, *args]
        self._containers.append(name)
        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as e:
            # Kill the docker client AND the container: the in-container
            # verify process keeps running otherwise (the client being dead
            # does not stop it). Partial output is still returned as
            # verification feedback (issue #159 edge case).
            self._remove_container(name)
            return SandboxExecResult(
                exit_code=-1,
                output=(e.stdout or b"").decode("utf-8", errors="replace"),
                timed_out=True,
                duration_seconds=time.monotonic() - started,
            )
        except OSError as e:
            raise SandboxUnavailableError(f"docker CLI unavailable: {e}") from e
        except subprocess.SubprocessError as e:
            raise SandboxUnavailableError(f"docker run failed: {e}") from e
        output = (proc.stdout or b"").decode("utf-8", errors="replace")
        if proc.returncode == 0:
            self._remove_container(name)
            return SandboxExecResult(
                exit_code=0,
                output=output,
                timed_out=False,
                duration_seconds=time.monotonic() - started,
            )
        # Non-zero docker client exit: classify via the container's recorded
        # State — only a container that actually ran to completion may
        # contribute an exit code (which is data, not a runner failure).
        state = self._inspect_container_state(name)
        if state is not None and state.get("Status") == "exited":
            exit_code = int(state.get("ExitCode", proc.returncode))
            result = SandboxExecResult(
                exit_code=exit_code,
                output=output,
                timed_out=False,
                duration_seconds=time.monotonic() - started,
                cap_hit=classify_cap_hit(exit_code, state, output),
            )
            self._remove_container(name)
            return result
        self._remove_container(name)
        raise SandboxError(
            f"docker run failed without completing the command "
            f"(client rc={proc.returncode}): {stderr_snippet(proc.stdout)}"
        )

    def destroy(self) -> None:
        """Remove containers this runner instance still tracks; idempotent.

        A completed exec already removed its own container, so in the happy
        path there is nothing left to do — what remains is an in-flight
        container (timeout/infrastructure path) or one whose removal failed.
        A name is dropped from the tracking list only once its removal is
        confirmed, so a repeated ``destroy`` retries exactly the leftovers.
        """
        for name in list(self._containers):
            self._remove_container(name)

    # -- docker helpers ------------------------------------------------------

    def _probe_daemon(self) -> None:
        try:
            probe = subprocess.run(
                [
                    "docker",
                    "info",
                    "--format",
                    "{{.ServerVersion}}",
                ],
                capture_output=True,
                timeout=self._DAEMON_PROBE_TIMEOUT,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise SandboxUnavailableError(f"docker daemon probe failed: {e}") from e
        if probe.returncode != 0:
            raise SandboxUnavailableError(
                "docker daemon unreachable: " + stderr_snippet(probe.stderr)
            )

    def _inspect_container_state(self, name: str) -> dict | None:
        try:
            inspect = subprocess.run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{json .State}}",
                    name,
                ],
                capture_output=True,
                timeout=self._INSPECT_TIMEOUT,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if inspect.returncode != 0:
            return None
        try:
            state = json.loads(inspect.stdout.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return None
        return state if isinstance(state, dict) else None

    def _remove_container(self, name: str) -> bool:
        """Remove one exec container and untrack it once it is gone.

        Returns whether the container is gone. A successful removal drops the
        name from the tracking list, so ``destroy`` only ever retries genuine
        leftovers (a container whose removal failed earlier); a failed removal
        keeps it tracked for that retry.
        """
        if not remove_sandbox_container(name, timeout=self._REMOVE_TIMEOUT):
            return False
        if name in self._containers:
            self._containers.remove(name)
        return True


def remove_sandbox_container(name: str, timeout: int = 30) -> bool:
    """Force-remove one sandbox container by name; never raises (FR-7, #164).

    Best-effort by contract: cleanup must never block recovery routing, so a
    failure is retried once and then logged loudly with the container name and
    the manual-cleanup command (a sandbox that outlives its cycle is a
    deployment problem an operator has to see). Returns whether the container
    is gone: ``True`` on a successful removal and on an already-absent
    container, because ``docker rm -f``'s postcondition — no such container —
    already holds there, and the startup sweep's listing-to-removal race is not
    a failure an operator can act on. Every other non-zero exit (daemon
    unreachable, permission denied) is a real failure and returns ``False``.
    """
    detail = "no output"
    for attempt in range(1, _REMOVE_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=timeout,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            detail = str(e)
        else:
            if proc.returncode == 0:
                return True
            detail = stderr_snippet(proc.stderr) or stderr_snippet(proc.stdout)
            if _ALREADY_GONE_MARKER in detail.lower():
                return True
        if attempt < _REMOVE_ATTEMPTS:
            logger.warning(
                "Sandbox container removal attempt %d/%d failed for %s: %s",
                attempt,
                _REMOVE_ATTEMPTS,
                name,
                detail,
            )
    logger.error(
        "Sandbox container %s could not be removed after %d attempts (%s); "
        "manual cleanup required: docker rm -f %s",
        name,
        _REMOVE_ATTEMPTS,
        detail,
        name,
    )
    return False


def list_managed_sandbox_containers(timeout: int = 30) -> list[tuple[str, str]]:
    """List this deployment's sandbox containers as ``(name, cycle)`` pairs.

    Selects on the ``SANDBOX_MANAGED_LABEL`` marker only, so containers this
    deployment never spawned are never candidates for the sweep; the second
    element is the container's ``SANDBOX_CYCLE_LABEL`` value (empty string when
    unlabeled), which the caller compares against the resumable cycle. Raises
    ``SandboxUnavailableError`` when the daemon or the CLI is unavailable — the
    sweep's caller decides how to degrade.
    """
    try:
        listing = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label={SANDBOX_MANAGED_LABEL}=1",
                "--format",
                f'{{{{.Names}}}} {{{{.Label "{SANDBOX_CYCLE_LABEL}"}}}}',
            ],
            capture_output=True,
            timeout=timeout,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise SandboxUnavailableError(f"docker ps failed: {e}") from e
    if listing.returncode != 0:
        raise SandboxUnavailableError(
            "docker ps failed: " + stderr_snippet(listing.stderr)
        )
    containers: list[tuple[str, str]] = []
    for line in listing.stdout.decode("utf-8", errors="replace").splitlines():
        fields = line.split()
        if not fields:
            continue
        name = fields[0]
        cycle = fields[1] if len(fields) > 1 else ""
        containers.append((name, cycle))
    return containers


def stderr_snippet(raw: bytes | None, limit: int = 300) -> str:
    """Decode a captured stream for log/error messages, bounded and never empty."""
    text = (raw or b"").decode("utf-8", errors="replace").strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "(no output)"
