# ADR 0056: Split-Plane Execution Sandbox on a Cloud VPS

## Status

Accepted (decided in the 2026-08-25 grilling session; detailed technical
requirements FR-1…FR-10 live in
[cloud-deployment-requirements.md](../cloud-deployment-requirements.md),
implementation slices in issues #159–#169)

## Context

When moving the Orchestrator to always-on cloud hosting (a single netcup RS),
the primary driver was security: an untrusted, prompt-injectable Worker must
not reach host resources, secrets, or unscoped credentials. On a developer
workstation the blast radius of a compromised Worker was the user's own
machine; on a shared cloud VPS it becomes the whole deployment.

Two provider-level constraints bound the solution space before any option
comparison:

- netcup provides **no nested virtualization** ("not even on root servers",
  official forum thread 22070, April 2026) — Firecracker-class microVM
  isolation is physically impossible there, not merely unfavored.
- Daytona self-hosting no longer exists (closed-source since June 2026),
  removing the turnkey self-hosted sandbox option.

## Considered Options

- **A — Whole-container hardening** (gVisor around one container): rejected —
  secrets share the environment with untrusted execution, failing protection
  goal #2 regardless of runtime strength.
- **B — Split-plane with container-class sandbox** (chosen): meets all four
  protection goals at VPS cost; boundary placement keeps a later upgrade to
  Kata/Firecracker possible without re-architecture.
- **C — MicroVM-per-execution** (E2B infra or raw Firecracker): rejected for
  now — requires bare-metal/nested-KVM hosts (~4× cost) plus Nomad/Consul-grade
  operations for isolation strength the single-tenant threat model doesn't demand.

## Decision

Adopt the **split-plane architecture** (Option B): a trusted control plane
(the Orchestrator holding all secrets) and ephemeral, credential-free
**Execution Sandboxes** (Worker command execution + Verification) behind a
swappable runner interface — hosted on a single netcup Root Server with
Docker + gVisor (`runsc`) as the initial sandbox runtime and default-deny
egress with an allowlist (PyPI/npm/GitHub/mirrors) enforced outside the
sandbox's reach. Commits/pushes remain control-plane operations (PR-Node).

The runner interface (spawn → exec → collect bounded output → destroy) keeps
the boundary stable so the runtime can later be exchanged for Kata/
Firecracker-class isolation without re-architecture. Detailed functional
requirements FR-1…FR-10 (runner contract, Verify/Worker containment, gVisor
preflight, egress lockdown, credential scoping incl. the fine-grained PAT
Workflows-scope caveat, resource caps mapped to Hybrid Retry & Stateful
Resume, dev-only host backend, server runbook, repository lifecycle) are in
[cloud-deployment-requirements.md](../cloud-deployment-requirements.md); the
glossary term **Execution Sandbox** is defined in
[CONTEXT.md](../../CONTEXT.md).

## Consequences

### Pros

- Meets all four protection goals (host integrity, secrets, unscoped GitHub
  access, unrestricted web) at VPS cost, without bare-metal or nested-KVM
  hosting.
- Secrets never share an environment with untrusted execution: the Docker
  socket exists only in the control plane, sandbox environments carry only a
  minimal allowlist (ADR-0043 extended), and they are regression-tested to be
  secret-free.
- Boundary placement preserves the upgrade path to Kata/Firecracker-class
  isolation without moving the plane boundary.

### Cons

- `run_command` and Verify lose their host-side path entirely (fail closed when
  the sandbox backend is unavailable).
- Egress filtering adds host-level operational surface: gVisor constrains
  syscalls but not destinations, so nftables/proxy enforcement must be
  maintained as root-level configuration.
- Stateful Resume gains a sandbox-recreation path (fresh clone of the resumed
  branch when the per-cycle sandbox no longer exists) — added lifecycle
  complexity.
- Control plane and sandboxes remain co-tenants of one physical server;
  gVisor-mediated isolation is weaker than hypervisor separation, accepted
  for the single-tenant threat model.

## Inspiration & References

- gVisor user-space kernel — chosen because it needs no `/dev/kvm` and runs inside
  KVM guests; install/runtime-registration per [gvisor.dev](https://gvisor.dev/docs/user_guide/install) (T1, accessed 2026-08-25, verified against official docs).
- Default-deny egress via proxy allowlist + nftables enforcement, including
  metadata-IP/RFC1918 blocks — adapted from INNOQ's coding-agent network sandbox
  ([innoq.com](https://www.innoq.com/en/blog/2026/03/dev-sandbox-network), T2, accessed 2026-08-25, verified by reproducing rule structure) and industry agent-sandbox guidance ([augmentcode.com](https://www.augmentcode.com/guides/agent-execution-sandbox), T3).
- Split-plane precedent: OpenHands/SWE-agent execute agent actions in a separate
  runtime container while the application holds credentials; E2B separates its
  control plane from Firecracker sandboxes (T2/T3, accessed 2026-08-25).
- Fine-grained PAT scoping incl. the workflow-scope push requirement —
  [GitHub docs](https://docs.github.com/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens) (T1) and [community discussion #26254](https://github.com/orgs/community/discussions/26254) (T2), accessed 2026-08-25.
