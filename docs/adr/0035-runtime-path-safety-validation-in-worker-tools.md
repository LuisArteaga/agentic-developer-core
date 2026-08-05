# ADR 0035: Runtime Path-Safety Validation in Worker Tools

## Status
Accepted

## Context
ADR-0012 introduced **pre-flight path-safety validation**: the Plan-Node validates every *planned* target file against the shared `is_safe_path` blocklist (credentials, private keys, certificate extensions, VCS/dependency/agent-state directories) and fails fast with a `Security Block` before execution begins.

This pre-flight layer has a bypass. The `patch_file` tool (Execute-Node) only checks `read_files` membership — not whether the path is sensitive. An agent that calls `read_file` on a sensitive file (e.g. `.env`) gains `read_files` membership and can then `patch_file` it with no path-safety check, because pre-flight validation only covers *planned* paths, not *executed* paths. A successful indirect-prompt-injection that survives planning (or any runtime drift) can thus reach credentials. Additionally, merely reading a sensitive file exfiltrates its contents into the agent's context, where it can leak via a subsequent PR commit, `run_command`, or `web_search`.

Runtime-enforcement research argues pre-flight alone is insufficient: it is bypassable by symlink/TOCTOU drift and by any path the planner never saw. Tool-call time is the last point where a deterministic, code-enforced policy can run without modifying the model's behaviour.

## Decision
Add a **runtime path-safety check** inside the `read_file` and `patch_file` worker tools that re-validates against the *same* `is_safe_path` blocklist used by the Plan-Node (imported from the shared `orchestrator.path_safety` module — no second blocklist).

1. **Both `patch_file` and `read_file` are gated.** `patch_file` closes the write bypass (the issue's required change). `read_file` is gated too, because reading a secret is itself the exfiltration event and because a blocked path must never be registered in `read_files` — otherwise the read-then-patch bypass persists. This matches the observability-driven-sandboxing model where every tool invocation is a capability request evaluated at runtime.

2. **The check runs on the normalized relative path (`rel_str`), after `_normalize_path`, not on the raw input.** This is a deliberate deviation from the issue's literal proposal (`if not _is_safe_path(path)`). `is_safe_path` rejects *all* absolute paths and `..`, but the worker tools legitimately accept absolute paths *within the project root* (every existing test passes absolute paths). Running the check on `rel_str` reuses the forbidden-dir/name/extension blocklist while letting `_normalize_path` remain the single authority for root-containment and `..` resolution — no false positives on legitimate absolute paths, no duplicated containment logic.

3. **The check precedes the Read-Before-Edit gate in `patch_file`** (and precedes file access / `read_files` registration in `read_file`). A blocked path is a hard stop that performs no filesystem access and records no `read_files` membership, so read-state can never override the block.

`list_directory`, `grep_search`, and `run_command` are intentionally **not** gated here. `grep_search` already prunes the sensitive directories; `list_directory` reveals only names, not contents; and `run_command` is the Target Repository's own verification surface (its `make verify`), out of scope for a path blocklist. Gating them is left to a future hardening if a concrete exfiltration vector is demonstrated.

## Consequences

### Pros
- **Defense in depth**: sensitive paths are blocked at both the planning trust boundary and at tool-call time; either layer alone is bypassable, together they are not.
- **No secret exfiltration via read**: a blocked read returns an error and registers nothing, so secrets never enter the agent's context.
- **Zero expected false positives**: the blocklist is unchanged; legitimate source paths (including absolute paths within the workspace) still pass.
- **No new dependency or blocklist**: reuses `is_safe_path` verbatim; the two layers cannot drift apart.

### Cons
- **Slightly stricter than pre-flight**: the agent can no longer read a sensitive file even for legitimate debugging. In an autonomous development loop this is the desired posture, but it is a behaviour change worth knowing.
- **Lexical, not symlink-resolving**: `is_safe_path` inspects path *parts*, not the resolved target. A benignly-named symlink that resolves to `.env` would pass. Mitigating this would require resolving the target and re-checking its parts — deliberately deferred as a separate hardening to keep ADR-0012's lexical semantics consistent across both layers and to avoid changing the Plan-Node's established contract.

## Rejected Alternatives

### 1. Pre-flight only (status quo)
Rejected because `read_files` membership is granted by a runtime `read_file` call that the Plan-Node never sees, so the pre-flight block on *planned* paths does not constrain *executed* paths. This is the exact bypass the issue describes.

### 2. Raw-path check inside the tools (the issue's literal proposal)
Rejected because `is_safe_path` rejects all absolute paths, while the tools legitimately accept absolute paths within the workspace (and every existing test passes them). Checking `rel_str` after normalization preserves the blocklist's intent without those false positives.

### 3. Gate only `patch_file`, leave `read_file` open
Rejected because reading a secret is the exfiltration event, and a registered-but-blocked read would still leave the read-then-patch bypass structurally open. Gating reads is what makes the bypass *unreachable*, not merely *detected*.

### 4. Resolve symlinks inside `is_safe_path`
Deferred. It would strengthen the TOCTOU case but would change the Plan-Node's lexical contract and risk false positives on legitimate symlinked source trees. Tracked as future work, not part of this enhancement.

## Inspiration & References
- **ADR-0012** — the pre-flight layer this runtime layer completes; same blocklist, same fail-fast intent.
- **CaMeL — *Defeating Prompt Injections by Design* (Debenedetti et al., arXiv:2503.18813, Google DeepMind/ETH Zurich)**: enforces security policies in a custom interpreter at *tool-call time* by checking the capabilities of every tool argument, rather than trusting the planner. Direct support for runtime enforcement over pre-flight alone. https://arxiv.org/abs/2503.18813
- **Arize — *How Observability-Driven Sandboxing Secures AI Agents***: "a runtime enforcement layer that intercepts agent tool calls and decides whether they are allowed to execute ... the sandbox resides between inference and side effects." Treats every tool invocation as a capability request evaluated at runtime — the model for gating *both* read and write tools. https://arize.com/blog/how-observability-driven-sandboxing-secures-ai-agents
- **GitHub Issue #60** — documents the read-then-patch bypass and the pre-flight-vs-runtime trade-off table.
