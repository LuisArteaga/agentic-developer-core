# Split-Plane Execution Sandbox on a Cloud VPS

When moving the Orchestrator to always-on cloud hosting (netcup RS), the primary
driver was security: an untrusted, prompt-injectable Worker must not reach host,
secrets, or unscoped credentials. We decided on a **split-plane architecture** —
a trusted control plane (the Orchestrator holding all secrets) and ephemeral,
credential-free **Execution Sandboxes** (Worker command execution + Verification)
behind a swappable runner interface — hosted on a single netcup Root Server with
Docker + gVisor (`runsc`) as the initial sandbox runtime and default-deny egress
with an allowlist (PyPI/npm/GitHub/mirrors) enforced outside the sandbox's reach.
MicroVM isolation (self-hosted E2B/Firecracker) was excluded by constraint:
netcup provides no nested virtualization ("not even on root servers", official
forum, Apr 2026), and Daytona self-hosting no longer exists (closed-source since
June 2026); both facts make Firecracker-class sandboxes physically impossible on
this provider rather than merely unfavored.

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

## Consequences

- `run_command` and Verify lose their host-side path entirely (fail closed when
  the sandbox backend is unavailable).
- The Docker socket exists only in the control plane; sandbox environments carry
  only a minimal allowlist (ADR-0043 extended) and are regression-tested to be
  secret-free.
- Egress filtering must be enforced by host-level nftables/proxy, since gVisor
  constrains syscalls but not destinations.
- Stateful Resume gains a sandbox-recreation path (fresh clone of the resumed
  branch when the per-cycle sandbox no longer exists).

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
