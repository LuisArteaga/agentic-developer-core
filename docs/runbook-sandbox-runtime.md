# Ops Runbook — Execution Sandbox Runtime (gVisor / runsc)

Operational runbook for FR-4 of
[cloud-deployment-requirements.md](cloud-deployment-requirements.md)
(ADR-0056): register gVisor (`runsc`) as a Docker runtime on the deployment
host and verify it before the orchestrator runs. Every Execution Sandbox
container is started with an explicit `--runtime` flag; the default `runsc`
is a hard requirement that fails closed at startup.

## 1. How the runtime is enforced

- **Runtime selection** — `AGENT_SANDBOX_RUNTIME` (default `runsc`) decides
  the `--runtime` flag passed to every `docker run` the orchestrator issues
  (`orchestrator/sandbox.py::resolve_sandbox_runtime`). Unknown values fail
  closed with a configuration error.
- **Startup preflight** — the Process Supervisor runs
  `python -m orchestrator.preflight` alongside its environment and
  reachability checks before spawning the orchestrator (issue #161). It
  verifies, in order:
  1. Docker daemon reachable (`docker info`).
  2. Host kernel ≥ 5.6 (gVisor requirement; the kernel is shared with the
     control-plane container, so this check is valid everywhere).
  3. `runsc` binary on `PATH` (skipped when the orchestrator itself runs
     inside the control-plane container — the binary lives on the Docker
     host, not in the container; the daemon-side registration check below
     is the authoritative signal in that topology).
  4. `runsc` registered as a Docker runtime
     (`docker info --format '{{json .Runtimes}}'`).
- **Fail closed** — any failed check exits non-zero and the supervisor
  refuses to start the orchestrator (its stderr tail is logged). There is
  no silent degradation to shared-kernel containers; the only alternative
  is the explicit override in §4.3.
- The preflight does NOT smoke-run a container — do that manually after
  registration (§3). A failing preflight is visible in the supervisor's
  stderr (and in the systemd journal under the service unit).

## 2. Install gVisor on the Docker host (Ubuntu LTS)

From the official install guide (see §6):

```bash
# Trust the gVisor repository signing key
sudo curl -fsSL https://gvisor.dev/gvisor.asc \
  -o /usr/share/keyrings/gvisor.asc

# Add the apt repository (release channel)
echo "deb [signed-by=/usr/share/keyrings/gvisor.asc] \
  https://storage.googleapis.com/gvisor/releases release/main" | \
  sudo tee /etc/apt/sources.list.d/gvisor.list

sudo apt-get update
sudo apt-get install -y runsc
```

`amd64` and `arm64` architectures are supported. With the `apt` package the
binary lands in the system `PATH`; if you install a release archive
manually instead, place `runsc` somewhere on `PATH` (e.g.
`/usr/local/bin/runsc`) and continue with §3.

## 3. Register with Docker and verify

- The `apt` package route handles registration and daemon restart itself
  (the quick start notes `apt`/automated installs can skip manual
  configuration).
- Manual registration (release-archive installs, or after changing
  options):

  ```bash
  sudo runsc install          # writes the 'runsc' runtime into /etc/docker/daemon.json
  sudo systemctl restart docker
  ```

  Scheduling note: restarting the Docker daemon stops running containers.
  Perform registration/restart **between cycles** (FR-10 update procedure),
  never mid-cycle.

Verify registration:

```bash
docker info --format '{{json .Runtimes}}'
# expect: {"runc":{...},"runsc":{...}}
```

Smoke test (official quick-start command):

```bash
docker run --runtime=runsc --rm hello-world
```

Optionally confirm the user-space kernel booted with
`docker run --runtime=runsc --rm -it ubuntu dmesg` — the output starts with
gVisor's boot messages. Caveat (from the official docs): `dmesg` output is
trivially forgeable by an attacker, so never use it as a security boundary —
the `docker info` runtime registration is the enforcement-relevant signal,
which is exactly what the startup preflight checks.

## 4. Troubleshooting

### 4.1 Preflight fails: runsc installed but not registered

Symptom: preflight reports the binary exists but `docker info` does not
list `runsc` — the daemon was not restarted (or registration was skipped).

```bash
sudo runsc install
sudo systemctl restart docker
```

### 4.2 Preflight fails: kernel older than 5.6

The preflight names the requirement and the detected release. Upgrade the
host kernel (Ubuntu LTS HWE kernel) or move the deployment to a host with a
supported kernel. There is no override for this check in `runsc` mode —
gVisor does not support older kernels.

### 4.3 Local development without gVisor (WSL2 / dev override)

gVisor is typically not installed on a developer workstation. The explicit
override trades isolation for a working sandbox backend:

```bash
AGENT_SANDBOX_RUNTIME=runc
```

This is loudly warned at startup and on every runner resolution (shared-
kernel containers, no gVisor syscall interception). It is a documented
dev-only trade-off (FR-4): never set it as a fleet/production default, and
only ever per environment with that justification. Unknown values of
`AGENT_SANDBOX_RUNTIME` fail closed.

### 4.4 Target-repo build fails under gVisor (rare syscall gaps)

Escalation path for a specific target repository whose toolchain trips over
a gVisor syscall gap:

1. Capture gVisor debug logs: install a debug runtime entry and point the
   failing container at it (official quick start):

   ```bash
   sudo runsc install --runtime runsc-debug -- \
     --debug --debug-log=/tmp/runsc-debug.log --strace --log-packets
   sudo systemctl restart docker
   docker run --runtime=runsc-debug ...   # reproduce the failing command
   ```

   (`docker run --runtime=runsc-debug ...`; ensure SELinux is disabled when
   running with debug enabled, per the official note.) Inspect
   `/tmp/runsc-debug.log` for the failing syscall.

2. Per-repo exception: if the gap is confirmed and blocking, set
   `AGENT_SANDBOX_RUNTIME=runc` **for that repository's deployment
   configuration only** (explicit config key, never a default) and record
   the justification. Revisit when gVisor closes the gap.

### 4.5 Preflight exits at supervisor startup

The supervisor logs the preflight's stderr tail and exits 1 — read that
message first (it names the failed check and the fix). Under systemd,
`journalctl -u <unit>` carries the same output. After fixing the host
(§2–§3), restart the supervisor; no orchestrator state is consumed by a
failed preflight (it runs before the first cycle starts).

## 5. Composability

Runtime selection composes with, and is orthogonal to, the follow-up
slices: egress lockdown (FR-5) constrains destinations outside the sandbox;
resource caps (FR-7) bound per-container resources. Neither replaces
runtime selection.

## 6. References (accessed 2026-09-11)

| Fact | Source |
| --- | --- |
| Install via apt repo / release archive; Linux ≥ 5.6 requirement | [gvisor.dev install](https://gvisor.dev/docs/user_guide/install) |
| `runsc install` registration, mandatory daemon restart, smoke test, `dmesg` forgery caveat, debug runtime entry | [gvisor.dev Docker quick start](https://gvisor.dev/docs/user_guide/quick_start/docker) |
| Debug/strace flags (`--debug`, `--debug-log`, `--strace`) | [gvisor.dev Docker quick start](https://gvisor.dev/docs/user_guide/quick_start/docker), [debugging guide](https://gvisor.dev/docs/user_guide/debugging) |
