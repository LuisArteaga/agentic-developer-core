# Cloud Deployment Requirements — Split-Plane Execution Sandboxing

Technical requirements for running the Orchestrator Repository in the cloud
(a single cloud VPS) with hardened isolation of untrusted code execution. This document
is the traceability anchor for the issue breakdown; decisions are recorded in
[ADR-0056](./adr/0056-split-plane-execution-sandbox-on-cloud-vps.md).

## 1. Goal & protection goals

Primary driver: **isolation/security** — an LLM Worker must not be able to do
damage ("stupid things"). The loop should additionally run 24/7 unattended.

A compromised/prompt-injected Worker must be unable to reach:

1. **Host integrity** — the server OS/filesystem outside the workspace.
2. **Secrets** — `GH_PAT`, `OPENROUTER_API_KEY`, telemetry keys.
3. **Unscoped GitHub access** — PAT rights exceed the single Target Repository's needs.
4. **Unrestricted web** — outbound fetch stays allowlisted (`config/sources.toml`, ADR-0028).

## 2. Decided architecture (summary of ADR-0056)

- **Split-plane**: trusted control plane (Orchestrator, holds all secrets) vs.
  ephemeral credential-free Execution Sandbox(es) where Worker command execution
  and Verification run. Commits/pushes remain control-plane operations (PR-Node).
- **Hosting**: single cloud VPS (KVM guest, dedicated cores/RAM). The
  provider offers no nested KVM (verified — see references), therefore Firecracker/
  self-hosted E2B/Daytona are excluded by constraint, not preference.
- **Sandbox runtime**: Docker + gVisor (`runsc`) from day one — user-space kernel,
  no `/dev/kvm` required, runs inside a KVM guest.
- **Swappable interface**: the sandbox is accessed through a runner abstraction
  (spawn → exec command → collect bounded output → destroy) so the runtime can be
  exchanged later without moving the boundary.

## 3. Functional requirements

### FR-1 Execution Sandbox runner

- New runner module exposing: create (from image, workspace mount, resource caps),
  exec (command + timeout + bounded output capture), destroy; idempotent cleanup.
- Output truncation reuses the Verification Feedback bounds (150 lines / 10 KB).
- Exit code, stdout/stderr, duration returned to the caller; non-zero exits are
  data, not runner failures.
- Runner failures (daemon unreachable, image missing) fail loudly and route into
  existing error handling (Graph error routing to recovery, ADR-0038).

### FR-2 Verify phase containment (first containment slice)

- `AGENT_VERIFY_COMMAND` executes inside the Execution Sandbox, not on the host;
  `AGENT_VERIFY_TIMEOUT` maps to the exec timeout.
- Workspace bind-mounted read-write; no orchestrator environment variables leak
  beyond a minimal allowlist (extends ADR-0043 semantics to sandboxes).

### FR-3 Worker command containment

- Worker Tool `run_command` routes through the same runner; binary allowlist
  (ADR-0037) continues to apply *inside* the sandbox.
- No dual code path: after migration, host-side execution of untrusted commands
  is removed (fail closed if the daemon is unavailable).

### FR-4 Runtime hardening

- gVisor registered as Docker runtime; all sandboxes start with `--runtime=runsc`.
- Documented fallback posture if `runsc` is unavailable at startup: refuse to run
  (fail closed) rather than silently degrading to shared-kernel containers.

Implemented by `orchestrator/preflight.py` (startup preflight, wired into the
Process Supervisor's validation phase alongside reachability checks) and the
`AGENT_SANDBOX_RUNTIME` selection in `orchestrator/sandbox.py` (issue #161);
operational procedures: [runbook-sandbox-runtime.md](runbook-sandbox-runtime.md).

### FR-5 Egress lockdown

- Default-deny network egress from sandboxes; explicit allowlist only:
  PyPI/npm/GitHub/OS package mirrors as required by Verification installs, plus
  the URL Fetch/Web Search paths already governed by `config/sources.toml`.
- Hard-block link-local (169.254.169.254 metadata), RFC 1918, loopback destinations.
- Enforcement independent of the sandbox's cooperation (nftables/proxy layer on
  the host, INNOQ pattern); allowlist changes require root, not container access.

Implemented by the two enforcement planes generated from one policy module:
the host nftables ruleset (`orchestrator/egress.py::build_nftables_ruleset`,
applied by root via `scripts/egress_setup.py`) and the stdlib CONNECT egress
proxy (`orchestrator/egress_proxy.py`) with the allowlist merged from
`DEFAULT_EGRESS_DOMAINS` ∪ `config/sources.toml` ∪
`AGENT_SANDBOX_EGRESS_EXTRA_DOMAINS` (ADR-0060, issue #162); every sandbox
unconditionally attaches to the dedicated egress network and receives proxy
env (`orchestrator/sandbox.py`), the startup preflight verifies both planes
fail-closed (`orchestrator/preflight.py`); operational procedures:
[runbook-sandbox-egress.md](runbook-sandbox-egress.md).

### FR-6 Credential scoping

- Migration guidance to a GitHub fine-grained PAT scoped to the single Target
  Repository: Contents RW, Pull requests RW, Issues RW, Metadata R, Checks R;
  Workflows RW only because pushes may touch `.github/workflows/*`.
- Regression test asserting the sandbox environment contains none of
  `GH_PAT`/`OPENROUTER_API_KEY`/Langfuse keys under any configuration.

Implemented by two layers (issue #163): the sandbox env invariant —
`orchestrator/sandbox.py::FORBIDDEN_SANDBOX_ENV_NAMES` holds the full
documented orchestrator secret inventory, `build_sandbox_env` rejects any
allowlist override requesting one, and
`orchestrator/test_sandbox.py::TestForbiddenSandboxEnvMatrix` regression-tests
every secret name against every configuration — plus the startup PAT scope
validation — `orchestrator/preflight.py::check_pat_scopes` probes the loop's
own read endpoints behaviorally (GitHub exposes no scope-enumeration API for
fine-grained PATs) and refuses to start with an actionable gap report;
`python -m orchestrator.preflight --scopes-only` validates a staged token on
any machine. Operational procedures, the Workflows-scope gotcha, and the
fine-grained Checks-permission known gap:
[runbook-pat-scoping.md](runbook-pat-scoping.md).

### FR-7 Resource caps & lifecycle mapping

- Per-sandbox CPU/memory/PID/disk caps (fork-bomb/runaway-loop containment).
- Lifecycle mirrors Hybrid Retry semantics: one sandbox persists across Execute
  attempts within a cycle (workspace state incl. untracked Test-Writer files is
  preserved); destroyed on Failure Recovery and at cycle end; recreated on
  Stateful Resume if absent (fresh clone of the resumed branch).

### FR-8 Deployment stack & operations

- Ubuntu LTS on the VPS; minimum 4 dedicated cores / 8 GB RAM,
  recommended 8 cores / 16 GB for parallel verify workloads.
- Control plane runs via existing Docker image + Process Supervisor (ADR-0015)
  under systemd; restart/backoff behavior preserved.
- `.agent_logs/` (state.json, traces, metrics) included in host backup routine;
  log rotation added.
- SSH hardening, automatic security updates, firewall default-deny inbound —
  documented runbook (no inbound services required; everything is outbound polling).

### FR-9 Local development without container backend

- Unit-level/TDD development requires no Docker: the runner interface is
  consumed through fakes in tests.
- An explicitly opted-in **dev-only host backend** (`AGENT_SANDBOX_BACKEND=host`)
  executes untrusted commands directly on the host, preserving today's
  pre-migration behavior (binary allowlist, env stripping) behind the same
  runner interface — for end-to-end cycle debugging on WSL2 without Docker.
- The host backend **refuses to start** in production contexts (orchestrator
  container image, `AGENT_MODE=ci`, server deployment markers) with an
  actionable error; when active it logs a loud, once-per-run warning.
- Default backend remains Docker; the host path is never selected implicitly.
  Rationale: a *silent* host fallback would be a permanent escape hatch
  undermining the split-plane boundary — an *explicit, prod-refusing* one keeps
  dev/prod parity honest.

### FR-10 Repository provisioning & maintenance

The instance maintains two repository classes with distinct lifecycles:

- **Target Repository checkout(s)** (`GITHUB_WORKSPACE`): cloned once per target
  repository (single-branch, default branch), then **synced at Claim time before
  every cycle** (`fetch` + hard reset to origin default) so work never starts
  from a stale base. [Workspace Hygiene](../CONTEXT.md) applies between cycles;
  `git gc` plus an LRU eviction cap bound disk growth across many target repos.
  Private-repo clone auth uses the scoped fine-grained PAT (FR-6).
- **Orchestrator Repository** (control-plane deployment): reaches the server via
  a documented update procedure — pull, image rebuild, Process Supervisor
  restart — applied **only between cycles**, never mid-cycle; startup logs the
  running version stamp for diagnosability.
- **Self-target case**: when `GITHUB_REPOSITORY` equals the Orchestrator
  Repository itself (the loop improving its own code), the deploy checkout and
  the workspace remain strictly separate paths; a running cycle is never
  invalidated by a concurrent self-update.

## 4. Non-goals

- MicroVM/Firecracker-class isolation (blocked by provider constraint; revisit on
  bare-metal or via managed APIs later — the runner interface anticipates this).
- Multi-tenant hardening (single-tenant threat model).
- Managed sandbox services (E2B Cloud etc.) — cost/latency per tool call; Daytona
  self-hosting no longer exists (closed-source since June 2026).

## 5. References (accessed 2026-08-25)

| Fact | Source | Tier |
| --- | --- | --- |
| Firecracker/E2B self-host requires `/dev/kvm`; Nomad+Consul+Terraform ops weight | [e2b-dev/E2B](https://github.com/e2b-dev/e2b), [temps.sh analysis](https://temps.sh/blog/best-e2b-alternatives-ai-sandboxes-2026) | T1/T3 |
| Daytona no longer self-hostable | [awesome-sandbox §Daytona](https://github.com/restyler/awesome-sandbox) (Jun 2026) | T3 |
| gVisor install/runtime registration, Linux ≥5.6, no hardware virt needed | [gvisor.dev install](https://gvisor.dev/docs/user_guide/install), [Docker quick start](https://gvisor.dev/docs/user_guide/quick_start/docker) | T1 |
| Default-deny egress + proxy/nftables allowlist pattern; block metadata IP/RFC1918 | [INNOQ blog](https://www.innoq.com/en/blog/2026/03/dev-sandbox-network), [Augment guide](https://www.augmentcode.com/guides/agent-execution-sandbox) | T2/T3 |
| Fine-grained PAT scopes; workflow-scope push requirement | [GitHub docs](https://docs.github.com/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens), [community discussion #26254](https://github.com/orgs/community/discussions/26254) | T1/T2 |

Provider-specific references (hosting provider's forum thread on nested
virtualization and its SKU/pricing page) were removed during public-release
preparation (2026-09); the underlying facts — no nested virtualization on the
selected VPS (verified April 2026) and the stated core/RAM tiers — are stated
in §2 and FR-8.
