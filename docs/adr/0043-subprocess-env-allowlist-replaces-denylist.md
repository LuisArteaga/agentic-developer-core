# ADR 0043: Subprocess env allowlist replaces denylist

## Status
Accepted (amends ADR-0037 layer 1's environment strategy; the command allowlist, `is_safe_path`, DNS pinning, and redaction layers of ADR-0037 are unchanged).

Amended 2026-09 (issue #160, ADR-0056 slice 2): with Worker command execution moved inside the Execution Sandbox, the allowlist mechanism itself migrated from the tool surface to the runner — `orchestrator.tools._build_subprocess_env` and `AGENT_SUBPROCESS_ENV_ALLOWLIST` are removed; `orchestrator.sandbox.build_sandbox_env` is authoritative (empty by default, opt-in `AGENT_SANDBOX_ENV_ALLOWLIST`). The allowlist-over-denylist decision and its fail-closed invariant are unchanged, and now hold for every untrusted execution path.

## Context
ADR-0037 layer 1 hardens `run_command` by passing a **denylist** environment to the child: it strips named secrets (`GH_PAT`, `OPENROUTER_API_KEY`, …) and any var matching `.*(KEY|TOKEN|SECRET|PASSWORD)$`. The command allowlist and `shell=False` are correct and stay; the env sanitization is the weak link.

The suffix regex is narrow. These secret-bearing vars leak into the child process and are readable via `python -c "import os; print(os.environ[...])"`:

| Var | Leaks? | Reason |
|---|---|---|
| `DATABASE_URL` | yes | no matching suffix |
| `*_CREDENTIAL` | yes | `CREDENTIAL` ≠ `PASSWORD` |
| `*_CONN` / `CONN_STRING` | yes | no matching suffix |
| connection strings with embedded creds | yes | name-based, value-agnostic |

A Worker that has read `.env.example` learns the var names and can exfiltrate any non-matching secret. A denylist can only block secrets whose names it already knows — every new secret-naming convention is a new leak until the regex is extended. The fail-open posture is structural: the default is "pass everything, then remove known-bad."

## Decision
Replace the denylist (`_sanitize_subprocess_env` + `_SECRET_ENV_NAMES` + `_SECRET_ENV_NAME_RE`) with a **minimal allowlist** (`_build_subprocess_env` + `_DEFAULT_SUBPROCESS_ENV_ALLOWLIST`). Only explicitly permitted vars reach the child; every other `os.environ` entry — including any secret regardless of naming — is absent by default.

The default allowlist covers only non-secret operational essentials (`PATH`, `HOME`, `LANG`, locale, `TZ`, Python runtime vars, `VIRTUAL_ENV`, and the non-secret `AGENT_LABEL_*` / `AGENT_POLL_INTERVAL` config). A `AGENT_SUBPROCESS_ENV_ALLOWLIST` env var lets deployments add project-specific non-secret vars without code changes, mirroring the existing `AGENT_RUN_COMMAND_ALLOWLIST` override pattern.

`_SECRET_ENV_NAMES` / `_SECRET_ENV_NAME_RE` and the now-unused `import re` are removed — the allowlist supersedes them, and keeping dead denylist code would invite confusion about which path is authoritative.

## Considered Options
- **Keep the denylist, extend the regex** — rejected: whack-a-mole. Every new secret-naming convention (`*_CONN`, `*_CREDENTIAL`, `DATABASE_URL`, `DSN`, …) is a new leak until someone remembers to extend the regex. The structural problem is fail-open, not an incomplete pattern.
- **Allowlist + retain denylist as defense-in-depth** — rejected: the denylist is dead code once the allowlist is authoritative (nothing outside the allowlist reaches the child). Retaining it adds maintenance surface and ambiguity ("which is authoritative?") with zero security benefit.
- **Static hardcoded env dict (no `os.environ` at all)** — rejected: `PATH`, `HOME`, locale, and `VIRTUAL_ENV` vary per deployment and must be read from the parent to remain functional. A hardcoded dict would break binary resolution and locale handling. The Sourcery guidance recommends this for fully static deployments; this orchestrator runs in varied local/CI/Docker contexts.

## Consequences
- **Pro**: fails closed — a new secret var is absent from the child by default regardless of its name. Closes the `DATABASE_URL` / `*_CREDENTIAL` / `*_CONN` leak class entirely.
- **Pro**: removes the `/proc/self/environ` exfiltration surface for non-matching secrets (the hermes-agent CVE class): secrets that never enter the child env cannot be recovered from the child's `/proc`.
- **Con**: maintenance surface — adding a legitimate non-secret var requires updating the allowlist or setting `AGENT_SUBPROCESS_ENV_ALLOWLIST`. Mitigated by the override, which is documented in the README and mirrors the established `AGENT_RUN_COMMAND_ALLOWLIST` pattern.
- **Con**: a var that the child legitimately needs but that is not in the default allowlist will be silently absent, potentially breaking a command. Mitigated by the override and by the allowlist being limited to the well-understood operational essentials the subprocess (`git`, `python`, `pytest`, `make`, `ruff`, …) actually requires.

## Inspiration & References
- **Sourcery — Python Subprocess with Tainted Environment Arguments** (T1: authoritative primary, security rule DB): "Set minimal env explicitly: `subprocess.run(cmd, env={'PATH': '/usr/bin'}, shell=False)`. Don't use `env=os.environ`. Only include variables command requires." Directly prescribes the allowlist-over-denylist pattern adopted here. https://www.sourcery.ai/vulnerabilities/python-lang-security-audit-dangerous-subprocess-use-tainted-env-args (accessed 2026-07)
- **HashiCorp envconsul** (T2: authoritative secondary, renowned OSS): provides an explicit `allowlist` / `denylist` / `pristine` config for the child process environment — `pristine` clears all inherited env and only passes allowlisted/custom vars. Industry validation that allowlist is the preferred control for subprocess env isolation. https://github.com/hashicorp/envconsul (accessed 2026-07)
- **coleam00/Archon issue #1135 — "zero env leaks from managed repos into provider subprocesses"** (T3: community, but directly on-point): a peer agentic-coding tool replacing `buildSubprocessEnv()` returning `{ ...process.env }` with a strict allowlist (`--allow-env-keys`), enforcing that target-repo env never leaks into provider subprocesses. Same threat model (prompt-injected agent exfiltrating secrets) and same fix. https://github.com/coleam00/Archon/issues/1135 (accessed 2026-07)
- **NousResearch/hermes-agent issue #4427 — denylist bypassed via `/proc/environ`** (T3: community): demonstrates that even a working `_sanitize_subprocess_env` denylist is bypassable when the child reads the parent's `/proc/<ppid>/environ`. Reinforces that secrets must never enter the child env at all (allowlist), not merely be filtered at the boundary. https://github.com/NousResearch/hermes-agent/issues/4427 (accessed 2026-07)
- **Semgrep — Prevent Command Injection for Python** (T1: authoritative primary): documents `shell=False` + arg-list as the command-injection mitigation and the env as a separate secret-leak surface. https://docs.semgrep.dev/cheat-sheets/python-command-injection (accessed 2026-07)
- Amends [ADR-0037](./0037-worker-tool-trust-boundary-hardening.md) layer 1 (env strategy only).
