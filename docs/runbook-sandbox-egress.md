# Ops Runbook — Execution Sandbox Egress Lockdown (nftables + proxy)

Operational runbook for FR-5 of
[cloud-deployment-requirements.md](cloud-deployment-requirements.md)
(ADR-0056; mechanism recorded in [ADR-0060](adr/0060-sandbox-egress-lockdown-nftables-connect-proxy.md)):
default-deny egress for every Execution Sandbox, enforced outside the
sandbox's reach. Two planes are generated from one policy module
(`orchestrator/egress.py`): a host nftables ruleset polices the sandbox
subnet at the IP layer, and a host-side CONNECT proxy
(`python -m orchestrator.egress_proxy`) is the only domain-capable route out.

## 1. How egress is enforced

- **Plane 1 — nftables (IP layer)** — table `inet agdc_sandbox_egress`,
  applied by root. Selective-drop chains at priority -10 (before Docker's own
  filter chains) with `policy accept`: ONLY traffic sourced from the sandbox
  subnet is policed. Hard-blocked ranges (cloud metadata 169.254.169.254,
  RFC 1918, loopback, reserved, multicast) drop first; then a sandbox may
  reach only the egress proxy port on the gateway; everything else is
  logged (`agdc-egress-hard-drop: ` / `agdc-egress-deny: ` prefixes) with
  counters and dropped. Unrelated host and Docker traffic is untouched.
- **Plane 2 — CONNECT proxy (domain layer)** — a stdlib HTTP CONNECT proxy on
  the host (port 3128). Every sandbox's `HTTP(S)_PROXY`/`http(s)_PROXY` env
  points at it (injected by `orchestrator/sandbox.py`, unconditional). It
  classifies each CONNECT target against the allowlist, resolves DNS itself,
  validates every resolved address (hard-block classes; DNS-rebinding
  defense), connects to the pinned IP, and answers `HTTP/1.1 403` +
  WARNING log otherwise. IP-literal CONNECT targets are always denied.
- **Allowlist** — `python -m orchestrator.egress --print-allowlist` prints
  the effective policy: the default registry domains (pypi.org,
  files.pythonhosted.org, registry.npmjs.org, github.com,
  githubusercontent.com, archive.ubuntu.com, security.ubuntu.com) ∪ the
  `domains` of `config/sources.toml` ∪ `AGENT_SANDBOX_EGRESS_EXTRA_DOMAINS`
  (comma-separated). Entries must be bare domain names; scheme-prefixed or
  IP-literal entries are configuration errors.
- **Fail closed** — the startup preflight
  (`python -m orchestrator.preflight`, run by the Process Supervisor)
  verifies BOTH planes before any sandbox starts: the egress network must
  exist with the CONFIGURED subnet (a drift is a loud failure — the ruleset
  polices the configured subnet), and the proxy must accept TCP. There is no
  disable knob: the orchestrator refuses to run without enforcement.

## 2. Deploy the egress lockdown (root, on the Docker host)

One-shot setup (repeatable; safe to re-run):

```bash
sudo python3 scripts/egress_setup.py all
```

`all` = `network` → `rules` → `verify`:

1. **`network`** — ensures the dedicated ICC-disabled Docker network exists
   (`agdc-sandbox-egress`, subnet `172.30.0.0/24` by default; both via
   `AGENT_SANDBOX_EGRESS_NETWORK` / `AGENT_SANDBOX_EGRESS_SUBNET`). If the
   network exists with a DIFFERENT subnet, the command fails loudly —
   recreate it (`docker network rm agdc-sandbox-egress && sudo python3
   scripts/egress_setup.py network`) and re-apply the ruleset.
2. **`rules`** — deletes the previous `inet agdc_sandbox_egress` table,
   validates the generated ruleset with `nft -c`, applies it, and verifies
   with `nft list table`. Requires root (rc 2 otherwise): allowlist changes
   require root, not container access (ADR-0056).
3. **`verify`** — probes the proxy: `169.254.169.254:80` and a
   non-allowlisted domain must both answer 403. Add `--live` to also confirm
   an allowlisted registry (pypi.org:443 → 200) end-to-end.

Inspect what will be applied before loading it:

```bash
python3 -m orchestrator.egress --print-ruleset
python3 -m orchestrator.egress --print-allowlist
```

(Both emitters read the same env keys as the orchestrator; the ruleset is
generated for the CONFIGURED subnet — do not edit by hand.)

## 3. Run the proxy (host daemon)

The proxy is a long-running host process. Recommended systemd unit
(`/etc/systemd/system/agdc-egress-proxy.service`):

```ini
[Unit]
Description=AGDC sandbox egress proxy (FR-5)
After=network-online.target docker.service

[Service]
ExecStart=/usr/bin/python3 -m orchestrator.egress_proxy \
  --bind 172.30.0.1 --port 3128
WorkingDirectory=/opt/agentic-developer-core
User=agdc
Restart=on-failure
# The proxy needs NO root (port 3128 > 1024): run it as an unprivileged
# dedicated user. Policy comes from the in-repo module (root-owned checkout),
# not from anything the user could edit — so the daemon cannot weaken its
# own policy.
[Install]
WantedBy=multi-user.target
```

- `--bind 172.30.0.1` is the egress bridge gateway (the first usable host of
  the configured subnet — Docker assigns it automatically). Repeat `--bind`
  to listen on additional addresses (e.g. the control-plane gateway for
  in-container topologies). Default bind is 127.0.0.1.
- The sandbox env is derived from `AGENT_SANDBOX_EGRESS_SUBNET`, so if you
  change the subnet you must recreate the network, regenerate the ruleset,
  AND rebind the proxy (§2 + this section), then restart the supervisor.
- `SIGTERM`/`SIGINT` shut the proxy down cleanly. `Restart=on-failure`
  keeps a crashed proxy from taking sandboxes down silently — but note:
  while the proxy is DOWN, sandbox installs fail (fail closed; the
  preflight also refuses to start new sandboxes).

## 4. Changing the allowlist

- **Per-target-repo domains** (e.g. a private package index): set
  `AGENT_SANDBOX_EGRESS_EXTRA_DOMAINS=index.internal.example:registry.internal.example`
  in the DEPLOYMENT configuration (supervisor environment), then
  **re-apply the planes** — restart the proxy service (it loads the policy
  at startup) and re-run `sudo python3 scripts/egress_setup.py rules` if the
  subnet changed. The preflight output and `--print-allowlist` show the
  effective list.
- **Never** disable the lockdown per-deployment. There is no knob by
  design; the only sanctioned extension is MORE domains, validated as bare
  names.
- Allowlist entries that would hard-block (metadata/RFC1918/loopback) are
  meaningless by construction: the proxy refuses unsafe DNS answers and IP
  targets regardless of the allowlist, and the ruleset drops those ranges
  before any allowlist exception.

## 5. Log inspection

- **Proxy denials** (WARNING, logger `orchestrator.egress_proxy`):

  ```
  EGRESS DENY client=172.30.0.2:43122 destination=169.254.169.254:80 reason=ip literals are never allowlisted
  ```

  Map the client IP to the sandbox container:

  ```bash
  docker network inspect agdc-sandbox-egress \
    --format '{{range .Containers}}{{.Name}} {{.IPv4Address}}{{"\n"}}{{end}}'
  ```

- **nftables drops** (kernel log, both prefixes carry per-rule counters):

  ```bash
  sudo nft list table inet agdc_sandbox_egress   # counters + rule layout
  sudo journalctl -k | grep agdc-egress          # individual dropped packets
  ```

  `agdc-egress-hard-drop:` = a hard-blocked range (metadata/RFC1918/loopback)
  — always investigate; `agdc-egress-deny:` = sandbox traffic that was not
  the proxy port (a raw-socket bypass attempt or misconfigured tool).

- **Allowed tunnels** are DEBUG-logged (destination + pinned IP): enable
  DEBUG on `orchestrator.egress_proxy` for an audit window, not by default.

## 6. Threat walk-through: why a root-level process inside the sandbox cannot weaken enforcement

The Worker runs as root inside its container (the sandbox image's default),
so "the sandbox is compromised" means "a root process inside the sandbox":

1. **Change the proxy env / unset it** — traffic then tries to leave the
   sandbox subnet directly; the nftables chains allow only
   gateway:proxy-port from the subnet, so direct flows are dropped (and
   logged as `agdc-egress-deny`).
2. **Talk to the proxy but ask for a forbidden target** — the CONNECT target
   is classified in the PROXY process (host side): non-allowlisted domains
   and IP literals answer 403; the sandbox cannot alter the classification
   because the policy is generated from the in-repo module, not read from
   anything the sandbox can write.
3. **Poison DNS to reach an internal service** — the proxy resolves
   allowlisted domains ITSELF and validates every answer (hard-block
   classes → 403), then connects to the pinned IP; a rebinding answer
   cannot steer the connection at metadata or RFC-1918 space.
4. **Load a kernel module / touch nftables from inside the container** —
   containers see their own (gVisor-intercepted) netns; the host ruleset is
   unreachable from inside, and the nftables/nft binaries and config are
   root-owned on the HOST — outside the sandbox's filesystem and namespace.
5. **Escape to the host** — that is the runtime boundary (gVisor, FR-4),
   not the egress boundary; FR-5 assumes containment holds and ensures the
   network layer does not depend on the sandbox's cooperation.

The remaining trusted computing base for egress is therefore: host root,
the in-repo policy module, and the deployment's own configuration — all
outside the sandbox's reach.

## 7. Troubleshooting

### 7.1 Preflight fails: egress network missing

The preflight names the network and points at
`sudo python3 scripts/egress_setup.py network`. Apply §2 step 1, then
restart the supervisor.

### 7.2 Preflight fails: subnet drift

The live network's subnet differs from `AGENT_SANDBOX_EGRESS_SUBNET`.
Recreate the network with the configured subnet and re-apply the ruleset
(§2) — the ruleset polices the CONFIGURED subnet only, so a drifted network
would be unpoliced; that is why this is fail-closed.

### 7.3 Preflight fails: proxy unreachable

`AGENT_EGRESS_PROXY_PROBE_URL` (default `http://127.0.0.1:3128`) is not
accepting TCP. Check the systemd unit (§3): `systemctl status
agdc-egress-proxy`, and that it binds an address reachable from the
supervisor's namespace (the default probe targets 127.0.0.1 — set
`AGENT_EGRESS_PROXY_PROBE_URL` to the gateway address when the supervisor
runs in the control-plane container).

### 7.4 `egress_setup.py verify` fails

A 403 is EXPECTED for the metadata and non-allowlisted probes; a FAILURE is
anything else (0 = no answer = proxy down or wrong probe URL; 200 for a
blocked target = the proxy is running WITHOUT the current policy — restart
it). With `--live`, a non-200 for pypi.org means the host itself lacks
outbound internet or the proxy cannot reach it.

### 7.5 Installs fail inside a sandbox with 403

The destination domain is not allowlisted. Confirm with
`python3 -m orchestrator.egress --print-allowlist`, check the proxy's
WARNING line for the exact destination+reason (§5), and extend
`AGENT_SANDBOX_EGRESS_EXTRA_DOMAINS` (§4) rather than debugging the sandbox.

## 8. Composability

Egress lockdown composes with the other sandbox slices and is orthogonal to
them: runtime selection (FR-4, runbook-sandbox-runtime.md) constrains
syscalls; resource caps (FR-7) bound per-container resources; credential
scoping (FR-6) keeps secrets out of the sandbox environment entirely.
Neither replaces the egress boundary.

## 9. References (accessed 2026-09-13)

| Fact | Source |
| --- | --- |
| Proxy allowlist + nftables enforcement outside the agent's reach; root-only modifiability | [INNOQ dev-sandbox-network](https://www.innoq.com/en/blog/2026/03/dev-sandbox-network) |
| CONNECT tunnel + domain allowlist + metadata deny list + resolve-once rebinding defense | [CrowdStrike ward-proxy](https://www.crowdstrike.com/en-us/blog/secure-agent-harness-execution-preventing-escape) |
| Egress firewall + raw-socket bypass threat | [Hermes iron-proxy](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/egress/iron-proxy.md) |
| Sandbox networking checklist (allowlists, DNS as bypass path) | [Northflank](https://northflank.com/blog/how-to-design-networking-for-secure-ai-agent-sandboxes) |
