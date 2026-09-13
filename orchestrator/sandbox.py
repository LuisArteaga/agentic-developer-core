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
  data). This makes a false-green verify structurally impossible. Completed
  exec containers are removed by ``destroy()`` (the caller's lifecycle hook),
  keeping the exec path free of extra docker round-trips on success.
- ``destroy`` removes any containers this runner instance spawned and is
  idempotent; it is best-effort and never raises.

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
# by construction.
FORBIDDEN_SANDBOX_ENV_NAMES = frozenset(
    {
        "GH_PAT",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "OPENROUTER_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
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


@dataclass(frozen=True)
class SandboxExecResult:
    """Outcome of one exec inside the sandbox.

    ``exit_code`` is the command's own exit status — a non-zero value is
    data (a failed verification), not a runner failure. ``output`` is the
    combined stdout+stderr stream, decoded with errors replaced. ``timed_out``
    marks that the command was killed at the exec timeout with partial
    output preserved (``exit_code`` is then the ``-1`` sentinel used by the
    Verify phase).
    """

    exit_code: int
    output: str
    timed_out: bool = False
    duration_seconds: float = 0.0


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


def _resolve_sandbox_caps() -> tuple[str, str, str]:
    """Resolve resource caps from the environment with loud validation."""
    cpus = os.getenv("AGENT_SANDBOX_CPUS", DEFAULT_SANDBOX_CPUS).strip()
    memory = os.getenv("AGENT_SANDBOX_MEMORY", DEFAULT_SANDBOX_MEMORY).strip()
    pids_limit = os.getenv(
        "AGENT_SANDBOX_PIDS_LIMIT", DEFAULT_SANDBOX_PIDS_LIMIT
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
    return cpus, memory, pids_limit


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


def get_sandbox_runner() -> SandboxRunner:
    """Return the configured sandbox backend (default: docker).

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
    cpus, memory, pids_limit = _resolve_sandbox_caps()
    egress = resolve_sandbox_egress()
    return DockerSandboxRunner(
        image=image,
        cpus=cpus,
        memory=memory,
        pids_limit=pids_limit,
        runtime=resolve_sandbox_runtime(),
        egress_network=egress.network,
        egress_proxy_url=egress.proxy_url,
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
    ):
        self._image = image
        self._cpus = cpus
        self._memory = memory
        self._pids_limit = pids_limit
        self._runtime = runtime
        self._egress_network = egress_network
        self._egress_proxy_url = egress_proxy_url
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
                f"{_stderr_snippet(inspect.stderr)} "
                "(set AGENT_SANDBOX_IMAGE to an image that provides the "
                "target repository's verify toolchain)"
            )
        self._workspace = workspace

    def exec(self, args: list[str], timeout: float) -> SandboxExecResult:
        """Run ``args`` inside the sandbox; non-zero exits are data.

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
            "--runtime",
            self._runtime,
            # Egress lockdown (FR-5, ADR-0060): attach to the dedicated
            # ICC-disabled egress network and route HTTP(S) through the
            # host-side proxy — the only domain-capable path out. Unconditional:
            # there is no egress-disabled mode.
            "--network",
            self._egress_network,
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
            return SandboxExecResult(
                exit_code=int(state.get("ExitCode", proc.returncode)),
                output=output,
                timed_out=False,
                duration_seconds=time.monotonic() - started,
            )
        self._remove_container(name)
        raise SandboxError(
            f"docker run failed without completing the command "
            f"(client rc={proc.returncode}): {_stderr_snippet(proc.stdout)}"
        )

    def destroy(self) -> None:
        """Remove containers spawned by this runner instance; idempotent."""
        for name in list(self._containers):
            self._remove_container(name)
        self._containers.clear()

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
                "docker daemon unreachable: " + _stderr_snippet(probe.stderr)
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

    def _remove_container(self, name: str) -> None:
        try:
            subprocess.run(
                ["docker", "rm", "-f", name],
                capture_output=True,
                timeout=self._REMOVE_TIMEOUT,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("Best-effort sandbox removal of %s failed: %s", name, e)


def _stderr_snippet(raw: bytes | None, limit: int = 300) -> str:
    text = (raw or b"").decode("utf-8", errors="replace").strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "(no output)"
