# ADR 0061: Cycle-Scoped Sandbox Lifecycle, Ownership Labels, and Resource Caps

## Status

Accepted (2026-09-21, issue #164; implements FR-7 of
[cloud-deployment-requirements.md](../cloud-deployment-requirements.md), on top
of the split-plane decision of ADR-0056 and the sandbox runner interface of
issue #159 / PR #184)

## Context

Issue #159 put every untrusted command behind `orchestrator.sandbox`: a
spawn-per-exec container with a workspace bind mount and no orchestrator
credentials, with a non-zero exit classified through `docker inspect` so a
false green is structurally impossible. FR-7 then asks for the two things that
runner deliberately left open:

- **Resource caps** on every spawn (CPU, memory, PID, disk) so a fork bomb or a
  runaway loop inside a sandbox cannot take the deployment host — which also
  runs the control plane — down with it.
- **Lifecycle mapping** that preserves the retry semantics the loop already
  relies on: Hybrid Retry (ADR-0034) and the Test-Writer's file-preservation
  semantics (ADR-0010) both assume that *the workspace*, not the execution
  environment, carries state across attempts.

Three properties of the existing system bound the solution space:

- The runner spawns a **container per exec and removes it immediately** after
  classifying the outcome. That is what makes the exec timeout a real kill (the
  container dies with the client, ADR-0056's timeout-kill guarantee) — a
  property no long-lived execution container can offer for free.
- The workspace is a **read-write bind mount**, so it outlives any container;
  a hard reset reverts tracked files only, and untracked Test-Writer artifacts
  survive (ADR-0034/ADR-0010). Sandbox lifetime therefore must not be the thing
  that carries attempt-to-attempt state.
- A crash is normal for this loop (ADR-0038 routes graph errors to Recovery;
  the Process Supervisor restarts the orchestrator, ADR-0015). Killing the
  `docker run` client does **not** kill the container, so every crash mid-exec
  leaves a container behind. Nothing in the pre-#164 system ever looked for
  those, and nothing matched them to a cycle.

What "one sandbox per cycle" has to mean is therefore **one identity** — image,
caps, runtime, egress network, ownership — validated once instead of per
command, not one long-lived container.

## Considered Options

- **A — Long-lived per-cycle container (create at claim, `exec` per command,
  destroy at cycle end)**: rejected. It removes the timeout-kill guarantee:
  killing a wedged `docker exec` client mid-command leaves the command running
  inside the still-alive container, which is exactly the failure mode the
  current runner was built to avoid, and it requires a durable container
  registry to survive a restart (registry drift becomes a new failure class).
  It also makes cap derivation a per-container decision instead of a
  per-spawn invariant.
- **B — Status quo (per-call containers, no cycle identity, no cleanup, no
  caps)**: rejected — FR-7's orphan and cap requirements are unmet. Unbounded
  memory/PIDs on the control-plane host is not a defensible default, and
  leaked containers accumulate silently across crashes.
- **C — Blanket `docker container prune` sweep at startup**: rejected. Prune
  does accept a `label` filter, but it removes *all stopped* containers
  matching the filter set in one shot; it has no per-container decision, no
  keep-set, and its only time dimension is **creation** time (`until=<ts>`),
  not cycle ownership. A sweep that cannot express "everything except the
  cycle being resumed" is precisely the wrong primitive here: a resumed cycle's
  containers may still be alive in another process, and destroying a live
  sandbox's containers is not a recoverable mistake.
- **D — Cycle identity as labels + a lifecycle manager, caps on every spawn,
  explicit label-selected sweep (chosen)**: the runner keeps spawn-per-exec;
  a module-level lifecycle manager (`orchestrator.sandbox_lifecycle`) owns
  which workspace is live for the current cycle; every container carries an
  ownership marker plus a cycle token; the Claim-Node sweeps exactly the
  managed containers the resumable cycle does not own.
- **E — `--storage-opt size` as the disk cap**: rejected as the disk cap.
  `--storage-opt` configures the *storage driver*: in the overlay2 driver it is
  guarded to overlay-on-xfs with `pquota`, and the option constrains the
  container's own read-write layer — it has no meaning for a host path
  bind-mounted into the container, which is how the workspace is provided.
  The disk cap is therefore a bounded tmpfs for the container's scratch space.

## Decision

Adopt Option D. The division of responsibility is deliberate: caps and
ownership labels live in the runner (they are properties of a *spawn*), the
cycle binding lives in the lifecycle module (it is a property of a *cycle*).

- **One identity per cycle, still one container per exec.**
  `orchestrator.sandbox_lifecycle` binds a cycle token
  (`cycle_token_for_issue` → `issue-<n>`, `None` when no issue is known) and
  hands out a single `CycleSandbox` per cycle via `acquire_cycle_sandbox`,
  re-created on first use in a fresh process. Every untrusted-execution call
  site — the Worker's `run_command`, the Test-Writer's pre-verification
  discovery, and the Verify gate — goes through it instead of constructing a
  runner per call, so image, caps, runtime, egress network and labels are
  resolved once per cycle. `orchestrator.sandbox` still creates and removes a
  container per command (see Context); nothing about the timeout-kill
  guarantee changes.
- **Resource caps on every spawn** (`_resolve_sandbox_caps`, validated loudly):
  `--cpus` (`AGENT_SANDBOX_CPUS`, default `2`), `--memory`
  (`AGENT_SANDBOX_MEMORY`, default `4g`), `--pids-limit`
  (`AGENT_SANDBOX_PIDS_LIMIT`, default `512`), and a bounded scratch space
  `--tmpfs /tmp:rw,size=<AGENT_SANDBOX_TMPFS_SIZE>,mode=1777` (default `512m`).
  The tmpfs matters because the image's own `/tmp` otherwise lives in the
  container's writable layer with no ceiling but the host disk; Docker's
  unset-tmpfs default (50 % of host RAM) is not a cap in any useful sense on a
  16 GB host. The size is validated against `[0-9]+[kmg]?` with a non-zero
  magnitude, so a typo fails at resolution instead of silently disabling the
  cap. Validation is per-deployment configuration, never per call site.
- **Ownership as container labels.** Every sandbox container carries
  `agdc.sandbox.managed=1` (`SANDBOX_MANAGED_LABEL`) and, when the runner knows
  its cycle, `agdc.sandbox.cycle=issue-<n>` (`SANDBOX_CYCLE_LABEL`). The
  marker's job is to make the sweep demonstrably *this deployment's* cleanup:
  containers without the marker are never touched.
- **Startup orphan sweep with a conservative keep rule.** The Claim-Node runs
  `sweep_orphaned_sandboxes(resumable_cycle)` — the resumed cycle's token on the
  resume path, `None` on a fresh cycle — **before** workspace hygiene, so
  leaked containers are gone before the cycle's first command and the current
  cycle's own containers are never candidates. The keep rule and the rationale
  are mirrored in the module docstring: a container whose cycle label equals the
  resumable cycle is spared, because a resumed cycle may still be alive in
  another process (the supervisor's restart can race its own child) and
  destroying a live sandbox is not recoverable; a fresh cycle therefore sweeps
  *all* managed leftovers, including those of a previous, non-resumable cycle
  for the same issue. If the daemon is unreachable the sweep degrades loudly
  (WARNING naming the count) instead of aborting the cycle — a cleanup pass must
  not become a new startup failure mode.
- **Destroy is deterministic and non-blocking.** The lifecycle is released on
  the three cycle-end paths: a clean merge, Failure Recovery, and the
  Security-Block quarantine path (`recovery_node`). `release_cycle_sandbox` is
  idempotent, and a failed removal is retried once (`_REMOVE_ATTEMPTS = 2`) and
  then logged as an operator-facing ERROR naming the container id — never
  raised into the recovery path, and the name is dropped from the tracking list
  only once removal is confirmed, so a later `destroy()` retries exactly the
  leftovers.
- **A cap hit is a failed attempt, not runner infrastructure.**
  `classify_cap_hit` returns `memory` / `pids` / `disk` / `None` from
  conservative signals, and `cap_hit_message` supplies the actionable
  explanation the caller folds into the feedback. The distinction is
  behavioral, not cosmetic: ADR-0038 routes runner *infrastructure* failures to
  Recovery (bypassing the retry budget) while a failed *attempt* consumes it —
  and a cap hit is something the Worker can act on (write smaller artifacts,
  reduce parallelism, stop recursing). The authoritative memory signal is the
  container's post-mortem `State.OOMKilled`; exit code `137` alone is *not*
  sufficient because an external `docker kill` produces the same code with
  `OOMKilled: false`. The `pids` and `disk` classes are read from the kernel's
  own wording in the output, and every class requires a non-zero exit — a
  command that exited 0 ran to its own completion, so a marker in its output is
  something it handled rather than the cap that stopped it.
- **All three cap classes were verified against a live daemon** (alpine
  container through the real runner, 2026-09-21), and the probe changed the
  classification: a pids-limited container reports
  `sh: can't fork: Resource temporarily unavailable` and exits with the
  **shell's** code — `2`, not the `1`/`137` the first implementation pinned —
  so a real fork-bomb cap hit would have been silently reported as raw output
  (ordinary failure). The same probe confirmed `OOMKilled: true` with exit
  `137` for the memory class and `dd: error writing '/tmp/big': No space left
  on device` (exit `1`, tmpfs bounded at 8 MB) for the disk class, plus the
  ownership labels and cycle token on live containers.

## Consequences

### Pros

- FR-7's caps are enforced per spawn by construction (single argv builder),
  and the values are one deployment-level knob set instead of per-call-site
  parameters that can drift.
- The orphan sweep is scoped by a marker this deployment itself wrote, so it
  cannot touch containers it did not spawn — the failure mode that makes blunt
  pruning dangerous.
- Retry semantics are unchanged and explicitly *not* re-implemented: because
  the workspace bind mount, not the container, carries state, attempt 1's
  untracked Test-Writer files are still present in attempt 3 and a hard reset
  still reverts tracked files only (ADR-0034/ADR-0010). The lifecycle change is
  therefore invisible to Hybrid Retry — which is what the parity test pins.
- Cleanup is bounded in failure cost: sweep degrades loudly, removal retries
  once and then logs with the container id, so neither can block a cycle or
  Recovery.
- One sandbox identity per cycle also means the container inventory is
  diagnosable: `docker ps -a --filter label=agdc.sandbox.cycle=issue-<n>` names
  exactly the containers of one cycle.

### Cons

- Cycle ownership is process-local state (`_bound_cycle` / `_live_sandbox`).
  That is deliberate — a cycle runs in one process, and a restart begins with
  no handle and re-creates the sandbox — but it means the "keep" decision at
  sweep time relies on the *resume* signal being truthful. A bogus resume
  would spare containers that are in fact orphans; they are cleaned up by the
  next fresh cycle.
- `--memory` bounds memory **and swap** jointly: with `--memory-swap` unset,
  Docker allows the container up to twice the configured value in
  memory+swap. The cap is knowingly a 2× headroom rather than an exact total,
  because a hard `--memory-swap` equal to `--memory` converts legitimate swap
  use into an OOM kill on a host we do not control. The cap's purpose is
  containing runaway loops, not precise accounting; revisit if a deployment
  enables a large swap file.
- The tmpfs counts against the container's memory cgroup, so a sandbox that
  fills its scratch space can also trip the memory cap and be classified as a
  memory hit (the `disk` classification leans on `ENOSPC` in the output). Both
  outcomes are actionable for the Worker; the mix is accepted rather than
  disambiguated with extra instrumentation.
- PIDs and disk cap hits are recognized from output markers
  ("Resource temporarily unavailable", "No space left on device") because the
  kernel reports them as ordinary command failures rather than container
  deaths. The exit code is deliberately not part of the pids rule — shells
  report the fork rejection with their own status (live probe: busybox `sh`
  exits `2`) — and a marker miss degrades to raw output, deliberately chosen
  over a false positive that would misdirect the Worker.
- Caps are per sandbox, not per cycle: nothing aggregates usage across the
  cycle's many short-lived containers, so a cycle can still consume a
  cumulative multiple of the per-container cap. Cycle-level accounting is
  follow-up material, not part of FR-7's per-sandbox requirement.

## Inspiration & References

- Container resource-constraint semantics used by the cap set — `--cpus`,
  `-m/--memory` (with the unset-`--memory-swap` doubling documented
  explicitly), `--pids-limit`, and the memory-cgroup interaction of tmpfs —
  Docker Docs, *Resource constraints*
  ([docs.docker.com](https://docs.docker.com/engine/containers/resource_constraints/), T1, accessed 2026-09-21, verified by quoting the flag table and the "can use 600m in total of memory and swap" worked example that motivates the 2× Cons).
- Bounded tmpfs scratch space and its default ceiling — `size` / `mode` mount
  options, "if unset, the default maximum size of a tmpfs volume is 50% of the
  host's total RAM", plus the note that tmpfs usage counts against the
  container memory limit — Docker Docs, *tmpfs mounts*
  ([docs.docker.com](https://docs.docker.com/engine/storage/tmpfs/), T1, accessed 2026-09-21, verified against the option table and limitations section behind `DEFAULT_SANDBOX_TMPFS_SIZE`).
- Exit-status and post-mortem classification: `SIGKILL(9)` is reported as
  status 137 and its documented causes (manual kill, `docker kill`, daemon
  restart) do **not** include OOM, while the Engine API defines
  `State.OOMKilled` as the "process … killed because it ran out of memory"
  signal — `docker/cli` reference
  ([github.com/docker/cli](https://github.com/docker/cli/blob/master/docs/reference/commandline/container_ls.md), T1, accessed 2026-09-21, verified by comparing the exit-status list against the swagger `State.OOMKilled` definition used in `classify_cap_hit`); corroborated at T3 by the Docker community forum's 137/SIGKILL explanation ([forums.docker.com](https://forums.docker.com/t/docker-exiting-with-code-137-but-oomkilled-is-false/137273), T3, accessed 2026-09-21).
- Label-based container selection (`--filter label=key[=value]`) as the sweep
  primitive, and the creation-time meaning of prune's `until` filter that made
  option C unfit for ownership-based cleanup — `docker/cli` reference for
  `container ls` and `container prune`
  ([github.com/docker/cli](https://github.com/docker/cli/blob/master/docs/reference/commandline/container_prune.md), T1, accessed 2026-09-21, verified against both filter tables, including prune's AND-combining `label` × `until` semantics).
- `--storage-opt` is a storage-driver option, not a bind-mount quota: the
  overlay2 driver guards `overlay2.size` behind an xfs-with-`pquota` backing
  filesystem and returns "`--storage-opt size is only supported for ReadWrite
  Layers`" otherwise — moby source
  ([github.com/moby/moby](https://github.com/moby/moby/blob/master/daemon/graphdriver/overlay2/overlay.go), T1, accessed 2026-09-21, verified by reading the guard branches that reject non-xfs backing filesystems; cited as source code because the upstream docs section no longer renders).
- One stateful, singleton sandbox workload with a stable identity, persistent
  storage across restarts, and controller-managed lifecycle including scheduled
  deletion — kubernetes-sigs Agent Sandbox
  ([github.com/kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox), T1, accessed 2026-09-21, verified against the project's feature list; the same project delegates isolation to gVisor/Kata, matching ADR-0056's runtime choice).
- A single per-session runtime container reused for every action, with resource
  control named as the motive — OpenHands runtime architecture
  ([docs.openhands.dev](https://docs.openhands.dev/openhands/usage/architecture/runtime), T1, accessed 2026-09-21, verified against the "launches a Docker container … executes them in the sandboxed environment" description).
- Explicit sandbox lifecycle states where destruction is the terminal state and
  the default timeout action is `kill` (resume restores saved state instead of
  keeping a container alive) — E2B sandbox persistence docs
  ([docs.e2b.dev](https://docs.e2b.dev/sandbox/persistence), T1, accessed 2026-09-21, verified against the Running/Paused/Killed state table and the `onTimeout` default).
- Ownership-based garbage collection with the explicit warning that blunt
  external pruning "can break … behavior and remove containers that should
  exist" — Kubernetes garbage-collection concepts
  ([kubernetes.io](https://kubernetes.io/docs/concepts/architecture/garbage-collection/), T1, accessed 2026-09-21, verified against the owner-reference cleanup description that shaped Option C's rejection).