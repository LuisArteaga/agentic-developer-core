# Ops Runbook — Execution Sandbox Lifecycle & Resource Caps

Operational runbook for FR-7 of
[cloud-deployment-requirements.md](cloud-deployment-requirements.md)
(ADR-0061, issue #164): one Execution Sandbox *identity* per issue cycle, a
resource-capped spawn per command, ownership marked on every container, and a
startup sweep that removes what a crash left behind. Sibling runbooks: runtime
selection ([runbook-sandbox-runtime.md](runbook-sandbox-runtime.md), FR-4),
egress lockdown ([runbook-sandbox-egress.md](runbook-sandbox-egress.md), FR-5).

## 1. What the lifecycle guarantees

- **One sandbox per cycle** is one *identity* — image, caps, runtime, egress
  network, ownership labels — resolved once and reused by every
  untrusted-execution call site in the cycle (Worker `run_command`, Test-Writer
  discovery, Verify gate). It is **not** one long-lived container:
  `orchestrator.sandbox` still creates a container per command and removes it
  as soon as the outcome is classified, because that is what makes the exec
  timeout a real kill.
- **The workspace carries the state, not the sandbox.** The workspace is a
  read-write bind mount (`/workspace`), so it survives every container. This is
  what keeps Hybrid Retry (ADR-0034) and the Test-Writer's file-preservation
  semantics (ADR-0010) intact inside a sandbox: attempt 1's untracked test
  files are still present in attempt 3, and a hard reset reverts tracked files
  only.
- **Deterministic teardown.** The cycle's sandbox is released on the three
  cycle-end paths: completed merge, Failure Recovery, and Security-Block
  quarantine. Released means destroyed — never kept warm for the next cycle.
- **Stateful Resume re-creates, it does not restore.** A resumed cycle runs in
  a new process with no live handle; the next command creates a sandbox bound
  to the same workspace. The workspace persists on disk, the container does
  not need to.

## 2. Resource caps (applied to every spawn)

| Knob | Default | Flag | Purpose |
| --- | --- | --- | --- |
| `AGENT_SANDBOX_CPUS` | `2` | `--cpus` | Bound a runaway loop's CPU share |
| `AGENT_SANDBOX_MEMORY` | `4g` | `--memory` | Bound memory (and swap, see below) |
| `AGENT_SANDBOX_PIDS_LIMIT` | `512` | `--pids-limit` | Contain a fork bomb / unbounded test parallelism |
| `AGENT_SANDBOX_TMPFS_SIZE` | `512m` | `--tmpfs /tmp:...,size=<v>` | Bound the container's scratch space |

Tuning notes:

- Values are validated when the runner is resolved: CPU must be a positive
  number, PIDs a positive integer, tmpfs a positive size literal
  (`512m`, `2g`, `1024k`). A blank or malformed value **fails closed** with
  `SandboxConfigError` before any container starts — a typo never silently
  disables a cap.
- The tmpfs is what actually bounds scratch space. Without it, the image's own
  `/tmp` lives in the container's writable layer, ceilinged only by the host
  disk — and `/tmp` is exactly where a runaway build writes. Docker's unset
  tmpfs default (50 % of host RAM) is not a cap on a 16 GB host, hence the
  explicit size.
- **tmpfs usage counts against the container's memory cgroup.** A sandbox that
  fills `/tmp` can therefore also trip `AGENT_SANDBOX_MEMORY`; see §6.1.
- `--memory` bounds memory **and swap jointly**: with `--memory-swap` unset,
  Docker permits up to twice the configured value in memory+swap. The cap is
  deliberately a 2× headroom rather than an exact total — forcing
  `--memory-swap` equal to `--memory` would turn legitimate swap use into an
  OOM kill. Size `AGENT_SANDBOX_MEMORY` knowing that.
- Caps are per sandbox container, not aggregated per cycle; a cycle running
  hundreds of short commands can still consume a cumulative multiple.

## 3. Cycle ownership labels

Every sandbox container carries two labels (`orchestrator/sandbox.py`):

| Label | Value | Meaning |
| --- | --- | --- |
| `agdc.sandbox.managed` | `1` | Spawned by this deployment — the sweep's only selection criterion |
| `agdc.sandbox.cycle` | `issue-<n>` | The cycle that owns the container |

Inspect the sandbox inventory of one cycle:

```bash
docker ps -a --filter label=agdc.sandbox.managed=1 \
  --format '{{.Names}}\t{{.Label "agdc.sandbox.cycle"}}\t{{.Status}}'
```

Containers **without** the managed marker are never touched by the sweep —
cleanup is scoped to what this deployment demonstrably spawned.

## 4. Startup orphan sweep

The Claim-Node runs the sweep before workspace hygiene, once per cycle start
(`orchestrator.sandbox_lifecycle.sweep_orphaned_sandboxes`):

- **Fresh cycle** (`resumable_cycle=None`, the normal path): every managed
  leftover is an orphan and is removed — including containers of an earlier,
  abandoned cycle for the same issue.
- **Resumed cycle**: containers whose cycle label equals the resumed cycle's
  token are **kept**. A resumed cycle may still be alive in another process
  (the supervisor's restart can race its own child) and destroying a live
  sandbox is not recoverable; the conservative rule costs one cycle's worth of
  leftovers at worst.
- **Degrades loudly:** an unreachable daemon logs
  `Startup sandbox sweep skipped (sandbox runtime unavailable): …` and the
  cycle proceeds. Cleanup must never be the reason a cycle does not begin
  (no sandbox can start without the daemon anyway; the next `create` fails
  closed).

Expected log lines: `Startup sandbox sweep: removing orphaned sandbox container
<name> (cycle <token>)` and, on a resume, `… keeping <name> (owned by resumed
cycle <token>)`.

## 5. Teardown, failures, and what is never allowed to block

- `release_cycle_sandbox()` is idempotent and called on merge, on Recovery, and
  on Security-Block quarantine. Calling it twice, or without a sandbox ever
  having been created, is a no-op.
- Removal is retried once (`docker rm -f`, 2 attempts total). A persistent
  failure is **not** raised into the recovery path: it logs an ERROR naming the
  container and the manual command —
  `Sandbox container <name> could not be removed after 2 attempts (<detail>); manual cleanup required: docker rm -f <name>`
  — and the name stays tracked, so a later `destroy()` retries exactly the
  leftovers.
- A wedged exec that hits the timeout kills both the `docker run` client and
  its container, and returns the partial output as verification feedback; it
  does not leave a running container behind.
- `.agent_logs/` (state, traces, metrics) remains control-plane-side; nothing
  about the sandbox lifecycle writes into the target workspace.

## 6. Troubleshooting

### 6.1 Verify/test run fails with an actionable cap message

The failure text names the cap ("hit its memory cap (AGENT_SANDBOX_MEMORY)",
"hit its process cap (AGENT_SANDBOX_PIDS_LIMIT)", "ran out of disk space
(bounded tmpfs scratch space, AGENT_SANDBOX_TMPFS_SIZE)"). That is a
**failed attempt**, not infrastructure: the loop will retry and the Worker is
expected to reduce its footprint.

Confirm the classification on a container that still exists:

```bash
docker inspect <name> --format 'exit={{.State.ExitCode}} oom={{.State.OOMKilled}} status={{.State.Status}}'
```

`oom=true` is the authoritative memory signal. Exit `137` alone is **not**:
`SIGKILL` (128+9) is also what an external `docker kill` or a daemon restart
produces, which is why the classification reads `OOMKilled` instead of the exit
code. `No space left on device` in the output is the disk signal; fork-failure
markers ("Resource temporarily unavailable", "unable to fork") are the
process-limit signal. None of the classes is inferred from the exit code alone:
a fork-limited command exits with its **shell's** status (busybox `sh` exits
`2`), and all three classifications require a non-zero exit — a command that
exited `0` ran to completion even if its output mentions a cap.

Remedies, in order of preference: fix the command (smaller artifacts, bounded
test parallelism, no recursive native builds) → then raise the relevant cap for
the deployment. A recurring memory hit on a workload that also fills `/tmp` may
be the tmpfs-vs-memory cgroup interaction (§2) rather than a genuine
memory-hungry command; check `AGENT_SANDBOX_TMPFS_SIZE` before raising memory.

### 6.2 A cap hit is being reported as a runner failure

Runner failures bypass the retry budget (ADR-0038 routes them to Recovery) —
a misclassification there wastes a whole cycle. The runner raises
`SandboxError` only when `docker run` did not complete the command; an exit
code recorded in the container's `State` is always data. If you see Recovery
triggered on a command that produced output, check the log for the
`docker run failed without completing the command (client rc=…)` message and
inspect the daemon's health — that is an infrastructure path, not a cap path.

### 6.3 Containers leaked after a crash

```bash
docker ps -a --filter label=agdc.sandbox.managed=1 \
  --format '{{.Names}}\t{{.Label "agdc.sandbox.cycle"}}\t{{.Status}}'
```

They are removed automatically by the next cycle's startup sweep (§4). To clean
up immediately, remove exactly the ones you have identified — never blanket
`docker container prune`, which drops all stopped containers matching its
filters in one shot with no per-container decision and no keep-set, and can
remove containers that are supposed to exist:

```bash
docker rm -f <name>          # one container, explicit
```

### 6.4 ERROR: container could not be removed

The removal retry pair failed (daemon unreachable or the container is stuck).
The message names the container and the manual command. Diagnose:

```bash
docker inspect <name> --format '{{.State.Status}} {{.State.Pid}}'   # stuck process?
sudo systemctl status docker                                        # daemon drift?
docker rm -f <name>                                                 # re-run manually
```

A sandbox that outlives its cycle is a deployment problem, but it is not a
correctness problem: the next fresh cycle's sweep removes it.

### 6.5 The sweep kept containers that look orphaned

They carry the cycle label of the cycle being resumed — the keep rule in §4. If
you are certain no live orchestrator process owns that cycle, remove them
manually (§6.3); the following fresh cycle would remove them anyway.

## 7. Composability

Runtime selection (FR-4) decides *which kernel* runs the container, egress
lockdown (FR-5) decides *where it may connect*, and this slice decides *how
many resources it may consume and how long it exists*. All three act on the
same spawn path (`orchestrator.sandbox`), and none of them has a disable knob.
The dev-only `AGENT_SANDBOX_BACKEND=host` path (FR-9) is unaffected: it is a
separate backend, and the caps are Docker-runtime flags.

## 8. References (accessed 2026-09-21)

| Fact | Source |
| --- | --- |
| `--cpus` / `--memory` / `--pids-limit` / `--tmpfs` flag semantics; unset `--memory-swap` doubles memory+swap | [docker/cli run reference](https://github.com/docker/cli/blob/master/docs/reference/commandline/run.md), [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/) |
| tmpfs `size`/`mode` options; unset default ceiling 50 % of host RAM; tmpfs counts against the memory cgroup | [Docker tmpfs mounts](https://docs.docker.com/engine/storage/tmpfs/) |
| Exit status 137 = `SIGKILL(9)` (not an OOM signal on its own); `State.OOMKilled` as the memory signal | [docker/cli container ls reference](https://github.com/docker/cli/blob/master/docs/reference/commandline/container_ls.md), [Docker Engine API swagger](https://github.com/moby/moby/blob/master/api/swagger.yaml) |
| `--filter label=…` selection; prune's `until` is creation time and prune has no keep-set | [docker/cli container ls](https://github.com/docker/cli/blob/master/docs/reference/commandline/container_ls.md), [docker/cli container prune](https://github.com/docker/cli/blob/master/docs/reference/commandline/container_prune.md) |
| Ownership-scoped cleanup over blanket pruning | [Kubernetes garbage collection](https://kubernetes.io/docs/concepts/architecture/garbage-collection/) |