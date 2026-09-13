# ADR 0060: Sandbox Egress Lockdown via nftables + CONNECT Proxy from One Policy Module

## Status

Accepted (2026-09-13, issue #162; implements FR-5 of
[cloud-deployment-requirements.md](../cloud-deployment-requirements.md), the
mechanism ADR-0056 decided at the architecture level)

## Context

ADR-0056 split the deployment into a trusted control plane and credential-free
Execution Sandboxes and decided "default-deny egress with an allowlist
(PyPI/npm/GitHub/mirrors) enforced outside the sandbox's reach" — but left the
mechanism open. FR-5 pins the requirements: hard-block the cloud-metadata
endpoint (169.254.169.254), RFC 1918, and loopback regardless of any
allowlist; enforcement independent of the sandbox's cooperation; allowlist
changes require root, not container access.

Three constraints bound the mechanism choice:

- gVisor (`runsc`, ADR-0056 / issue #161) constrains **syscalls, not
  destinations** — the network boundary needs its own enforcement layer.
- ADR-0017 keeps the runtime dependency set minimal: the policy must live
  in-repo, be generated from tested code, and add no external daemon.
- Sandboxes need **domain-level** egress (package registries are CDN-fronted,
  multi-IP; a static IP allowlist would be both brittle and over-broad), while
  the hard-block classes are **IP-level** (metadata/RFC1918/loopback).

The threat being defended against is a prompt-injectable Worker inside the
sandbox reaching cloud metadata (credential theft), internal services (SSRF
lateral movement), or arbitrary exfiltration endpoints. ADR-0028 already
established the resolve-once-pin-IP SSRF discipline at the tool layer for URL
Fetch; the network layer needs the same discipline as a backstop beneath it
(ADR-0037 layer 3).

## Considered Options

- **A — Off-the-shelf filtering proxy (squid + ICAP/ACL config)**: rejected —
  an external daemon whose policy lives in host-local config files drifts from
  the tested in-repo policy, violates the ADR-0017 minimal-dependency posture,
  and puts the allowlist outside unit-test reach.
- **B — iptables/nftables IP allowlist only**: rejected — no domain control
  (registries resolve to rotating CDN IPs), so it is either uselessly narrow or
  operationally brittle; also cannot express the CONNECT-time classification.
- **C — Documented "disable egress for local dev" knob**: rejected — a switch
  that silently voids FR-5 is exactly the failure mode the acceptance criteria
  forbid; there is deliberately no way to attach a sandbox outside the
  enforcement planes.
- **D — Two enforcement planes generated from one in-repo policy module**
  (chosen): host nftables ruleset for the IP layer, a stdlib CONNECT proxy for
  the domain layer, both rendered from `orchestrator/egress.py` so the
  allowlist, the hard-blocks, and the ruleset are one tested artifact.

## Decision

Adopt Option D. `orchestrator/egress.py` is the single policy module:

- **Allowlist**: `DEFAULT_EGRESS_DOMAINS` (PyPI, files.pythonhosted.org,
  npm, GitHub + githubusercontent, Ubuntu mirrors) ∪ the `domains` already
  governed by `config/sources.toml` (one curated list governs both the
  tool-layer URL Fetch and the network layer) ∪ the per-deployment extension
  `AGENT_SANDBOX_EGRESS_EXTRA_DOMAINS` (a target repository needing a private
  index adds its domain there — never a blanket disable; one orchestrator
  instance serves one target repository, so a deployment-scoped key IS the
  per-target-repo mechanism). Entries are loudly validated: scheme-prefixed,
  slash-containing, IP-literal, or blank entries raise `SandboxConfigError`.
- **Hard-blocked ranges** (`HARD_BLOCKED_V4_RANGES`): metadata link-local,
  RFC 1918, loopback, reserved, multicast, benchmark space — dropped at the IP
  layer before any allowlist exception.
- **Plane 1 — nftables ruleset** (`build_nftables_ruleset`, applied by root via
  `scripts/egress_setup.py rules`): selective-drop chains at priority -10 with
  `policy accept` police ONLY traffic sourced from the sandbox subnet —
  hard-blocks first, then only gateway:proxy-port is allowed, everything else
  logged (`agdc-egress-*` prefixes) with counters and dropped. Unrelated host
  and Docker traffic is untouched.
- **Plane 2 — CONNECT proxy** (`orchestrator/egress_proxy.py`, stdlib only):
  the only domain-capable route out. Every CONNECT target is classified
  against the allowlist (IP literals never allowed), resolved BY the proxy via
  `resolve_validated_destination` (every DNS answer must be a safe public
  address — the ADR-0028/ADR-0037 rebinding defense — and the connection is
  pinned to the validated IP), and denied with `HTTP/1.1 403` +
  WARNING-log(destination, client, reason) otherwise. Denials and the
  nftables drops give the observable-log requirement; the runbook maps the
  client IP to the sandbox container.
- **Sandbox wiring** (`orchestrator/sandbox.py`): every sandbox unconditionally
  attaches to the dedicated ICC-disabled Docker network
  (`AGENT_SANDBOX_EGRESS_NETWORK`, default `agdc-sandbox-egress`, subnet
  `AGENT_SANDBOX_EGRESS_SUBNET`, default `172.30.0.0/24` — the single source
  of truth from which the bridge-gateway proxy URL is derived) and receives
  `HTTP(S)_PROXY`/`http(s)_PROXY` env pointing at the host proxy. There is no
  disable knob.
- **Startup preflight** (`orchestrator/preflight.py`): verifies the egress
  network exists with the CONFIGURED subnet (drift = fail closed, because the
  ruleset polices the configured subnet) and that the proxy accepts TCP —
  before any sandbox starts. The orchestrator never applies the ruleset; it
  only verifies the result.

Enforcement is outside the sandbox's reach: the proxy runs on the host with
root-owned config, the ruleset is applied by root, and the network leaves no
other route out — a root-level process inside the sandbox cannot weaken
either plane (walk-through in the runbook).

## Consequences

### Pros

- Default-deny is structural, not cooperative: DNS-rebinding, metadata
  access, RFC-1918 pivoting, and non-allowlisted exfiltration are each blocked
  by at least one plane (DNS answers and CONNECT targets by the proxy; raw
  sockets by nftables).
- One policy module means the allowlist, the hard-blocks, and the ruleset are
  unit-tested artifacts — no host-side config drift; `--print-ruleset` /
  `--print-allowlist` make the deployed policy inspectable.
- `config/sources.toml` reuse keeps one curated domain list for tool-layer and
  network-layer egress; the per-deployment extension key covers private
  indexes without weakening the default posture.
- Selective-drop chains with `policy accept` keep the change surgical: only
  sandbox-subnet traffic is policed, so the host's own networking and Docker's
  forwarding semantics are untouched.

### Cons

- The proxy and the network/ruleset are new operational surface on the host:
  two systemd-managed processes/units (network + rules are one-shot, the
  proxy is a long-running daemon) and root-only reconfiguration (deliberate).
- Sandboxes without direct DNS: name resolution flows through Docker's
  embedded resolver and HTTP(S) through the proxy — a tool that refuses proxy
  env (rare; non-HTTP protocols) cannot egress, accepted per FR-5 (the
  Verified installs under test are pip/npm/git-over-HTTPS).
- The proxy is a single point of failure for sandbox egress: if it is down,
  installs fail (fail closed — the preflight refuses to start sandboxes
  without it).
- stdlib CONNECT proxying is deliberately less featureful than squid (no
  caching, no TLS interception) — accepted; ADR-0017 minimal dependencies and
  in-repo-testable policy outweigh those features.

## Inspiration & References

- Default-deny egress via proxy allowlist + nftables enforcement outside the
  agent's reach, including the root-only-modifiability requirement and
  `nft list ruleset` verification — adapted from INNOQ's coding-agent network
  sandbox ([innoq.com](https://www.innoq.com/en/blog/2026/03/dev-sandbox-network), T2, accessed 2026-09-13, verified by reproducing the rule structure in `build_nftables_ruleset`).
- CONNECT-proxy + domain allowlist + cloud-metadata deny list +
  DNS-rebinding defense (resolve once, connect only to the resolved IP) as
  the agent-harness pattern — CrowdStrike's `ward-proxy` network enforcement
  ([crowdstrike.com](https://www.crowdstrike.com/en-us/blog/secure-agent-harness-execution-preventing-escape), T2, accessed 2026-09-13, verified against the published enforcement table).
- Egress proxy with TLS-terminating default-deny firewall + "bypass via raw
  socket" threat note shaping the two-plane design (proxy for domains,
  nftables for raw sockets) — Hermes agent `iron-proxy` docs
  ([github.com/NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/egress/iron-proxy.md), T2, accessed 2026-09-13, verified against the documented sandbox-env injection).
- Sandbox networking design guidance: egress allowlists by hostname/IP/port,
  policy enforced independently of the agent, DNS as a path around egress
  policy ([northflank.com](https://northflank.com/blog/how-to-design-networking-for-secure-ai-agent-sandboxes), T3, accessed 2026-09-13, verified against the checklist).
- Agent execution sandbox guidance motivating destination-level (not
  syscall-level) containment ([augmentcode.com](https://www.augmentcode.com/guides/agent-execution-sandbox), T3, accessed 2026-09-13, cross-cited from ADR-0056).
