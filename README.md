# agentic-developer-core

An autonomous software engineer agent — the **Orchestrator** — implemented in pure Python with LangGraph + LangChain. It claims issues from a GitHub target repository, plans, writes tests first, implements, verifies, opens a pull request, and merges it — with stateful resume, telemetry, and LLM-based quality gates. No external CLI harness (Claude Code / OpenCode) is involved; the loop is a single compiled LangGraph state machine (ADR-0009).

> **Terminology**: this README mirrors the canonical glossary in [CONTEXT.md](CONTEXT.md) (Orchestrator, Worker, Localization, Stateful Resume, Blocked by, etc.). When a term is capitalized and linked, its precise meaning lives there, not here.

---

## Table of Contents

- [How it works](#how-it-works)
- [The autonomous loop](#the-autonomous-loop)
- [Worker tool suite](#worker-tool-suite)
- [Stateful resume](#stateful-resume)
- [Verification & quality gates](#verification--quality-gates)
- [Model routing](#model-routing)
- [Telemetry & metrics](#telemetry--metrics)
- [Deployment](#deployment)
- [Environment & labels](#environment--labels)
- [Get Started](#get-started)
- [Development & verification](#development--verification)
- [Architecture & guidelines](#architecture--guidelines)

---

## How it works

The Orchestrator runs a **phased LangGraph state machine** (not a free-form ReAct `while` loop) that turns one GitHub issue into one merged pull request, unattended:

1. **Claim** an `agent-ready` issue (honoring `## Blocked by` dependencies), or resume an interrupted cycle.
2. **Plan** — produce a [Localization](CONTEXT.md) (target files + intents) from the issue + a codebase snapshot, using tree-sitter structural outlines for Python/TypeScript.
3. **Write tests first** (Test-Driven Development, ADR-0010).
4. **Execute** — a ReAct [Worker](CONTEXT.md) makes the changes with a bounded tool suite, self-correcting on verification feedback.
5. **Verify** — run the target repo's deterministic gate (`make verify` by default), then a BinEval soft semantic gate; retry on failure.
6. **Open the PR** with a deterministic body (plan rationale + diff stat + coverage tail).
7. **Merge** — poll CI and PR Review Judge verdicts; on actionable feedback, run a bounded merge-fix loop back to Execute; else merge or escalate to recovery.

All failures route to **recovery**, which cleans up and re-queues the issue as `agent-ready`.

## The autonomous loop

The graph is compiled in [`orchestrator/graph.py`](orchestrator/graph.py); nodes live in [`orchestrator/nodes.py`](orchestrator/nodes.py).

```
        ┌──────────────────────────────────────────────────────────────┐
        │                                                              ▼
START → claim → plan → test_writer → execute → verify → pr → merge → END
        │                   ▲            ▲         │       │     │
        │                   │            └─────────┘       │     │
        │                   │       (Hybrid Retry:         │     │ merge-fix
        │                   │        verify → execute)     │     │ (merge → execute,
        │                   └──────────────────────────────┘     │  bounded by
        │                       (merge-fix: judge feedback)      │  AGENT_PR_FIX_MAX)
        │                                                          │
        └──────────────► recovery ←────────────────────────────────┘
                           END
```

Routing (`plan → test_writer → execute → verify → pr → merge → end`), with two retry edges:

- **Hybrid Retry** (pre-PR): `verify → execute`, incremental until `AGENT_RETRY_HARD_RESET_ATTEMPT` (default 3), then `git reset --hard` on tracked files (ADR-0034). Untracked Test-Writer files are preserved.
- **Merge-Fix Loop** (post-PR): `merge → execute` on actionable PR Review Judge verdicts, bounded by `AGENT_PR_FIX_MAX` (default 3). At exhaustion a summary PR comment is posted before failure recovery.

Node responsibilities:

| Node | Responsibility |
| --- | --- |
| `claim` | Claim an eligible `agent-ready` issue; parse `## Blocked by` (label `agent-blocked`); prepare the workspace; or resume an interrupted cycle ([Self-Healing Claim](CONTEXT.md)). |
| `plan` | LLM analysis of the issue + codebase snapshot → [Localization](CONTEXT.md) (target files + intents) and structural outlines (tree-sitter, Python/TS). Optional single Plan Detail Request re-invoke. |
| `test_writer` | Write unit/integration tests for the planned changes first (TDD, ADR-0010). |
| `execute` | ReAct [Worker](CONTEXT.md) runs the plan with custom tools, self-correcting on [Verification Feedback](CONTEXT.md). |
| `verify` | Run the target repo's deterministic gate (`make verify`) then the BinEval soft gate; on failure route back to `execute` (bounded retries). |
| `pr` | Open the PR with a deterministic body; commit `fix:` commits in the merge-fix loop ([PR Body Enrichment](CONTEXT.md)). |
| `merge` | Poll merge/CI status; parse PR Review Judge verdicts; merge, loop to `execute` (merge-fix), or escalate to recovery. |
| `recovery` | Clean up and reset the issue label to `agent-ready` for a future attempt ([Failure Recovery](CONTEXT.md)). |

Status values: `idle | claimed | planning | executing | verifying | pr_open | merging | done | failed`.

## Worker tool suite

The ReAct [Worker](CONTEXT.md) (in [`orchestrator/tools.py`](orchestrator/tools.py) and [`orchestrator/research_tools.py`](orchestrator/research_tools.py)) operates within a hardened trust boundary (ADR-0037):

- **`read_file` / `patch_file`** — block-based patching with the [Read-Before-Edit Constraint](CONTEXT.md) (line-range scoped, ADR-0033) and [Path Safety Validation](CONTEXT.md) (blocklist for credentials, VCS dirs, etc.; runtime enforcement ADR-0035).
- **`grep_search` / `list_directory` / `run_command`** — subprocess execution with a secret-stripped environment and a binary allowlist (ADR-0037).
- **`web_search` / `fetch_url`** — inline research; OpenRouter server-side search, and URL fetch with SSRF DNS pinning + domain allowlist via `config/sources.toml` (ADR-0026, ADR-0028).

## Stateful resume

Graph state is serialized atomically to `.agent_logs/state.json` on every transition ([Stateful Resume](CONTEXT.md)). A restart reloads state, checks out the branch, cleans untracked files, and resumes at the recorded phase. Worker and Test-Writer ReAct traces are persisted to JSONL sidecars under `.agent_logs/` for post-hoc debugging only — they are never reloaded into the loop.

## Verification & quality gates

- **In-cycle deterministic gate** — `make verify` (ruff lint, ruff format check, mypy, pytest). Deliberately excludes semgrep/pip-audit (ADR-0020).
- **Opt-in strict gate** — `make pre-commit-strict` (adds local semgrep mirroring CI). See [Development & verification](#development--verification).
- **BinEval soft gate** — a pre-PR semantic review (10 binary checks across Completeness / Simplicity / ADR Compliance / Robustness) graded by a lightweight LLM against the issue body, plan, git diff, and ADRs. Infra failure degrades to PASS; the post-PR Review Judges remain the hard gate (ADR-0027).
- **CI** — [`pr-checks.yml`](.github/workflows/pr-checks.yml) runs the secret scan, ruff, mypy, pytest with coverage (≥89%), semgrep, pip-audit, and then the **PR Review Judges** (`scripts/review.py`): Syntax/Lint, Test Coverage, Architecture, Security. Judges evaluate per-file chunks with [Enclosing Function Context](CONTEXT.md) (ADR-0022/0023), emit a hidden verdict block (ADR-0019), and produce `PASS` / `FAIL` / `NEEDS REVIEW` verdicts that gate the merge (ADR-0014). [`secret-scan.yml`](.github/workflows/secret-scan.yml) adds gitleaks.

## Model routing

Each orchestrator node resolves its LLM from [`config/factory.json`](config/factory.json) — a flat mapping of node name to `{model, routing, temperature, options, max_tokens, fallback_model}` (ADR-0018). Nodes include `plan`, `test_writer`, `execute`, the four judges (`syntax_lint`, `test_coverage`, `architecture`, `security`), `web_search`, and `bin_eval`. Structured-output nodes (`plan`, `bin_eval`) set `max_tokens` to prevent JSON truncation (ADR-0040).

Overrides take precedence over the factory default via environment variables and disable provider routing for the overridden node:

| Variable | Node |
| --- | --- |
| `AGENT_MODEL` | general fallback for all nodes |
| `PLAN_MODEL` | plan |
| `TEST_WRITER_MODEL` | test_writer |
| `EXECUTE_MODEL` | execute (Worker) |

The resolved Execute Model is persisted as `state["model"]` for backward compatibility with prior `state.json` files; all other nodes re-resolve from the factory on each run ([Per-Node Model Routing](CONTEXT.md)).

## Telemetry & metrics

- **Telemetry** — in-process OpenTelemetry spans (`orchestrator_loop`, `orchestrator_phase_…`). When `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` are set, spans export to Langfuse Cloud with session baggage; otherwise they fall back to the generic OTLP endpoint. Tracing never breaks execution if the exporter is unavailable (ADR-0016).
- **Run metrics** — Trajectory Length, Plan Alignment, and Token Consumption are appended to `.agent_logs/metrics.jsonl`, one record per completed issue cycle (ADR-0029). `state.json` remains strictly for Stateful Resume.

## Deployment

The `python:3.12-slim` Docker image's entrypoint is [`scripts/entrypoint.py`](scripts/entrypoint.py) — a zero-dependency Process Supervisor (ADR-0015) that:

- validates `OPENROUTER_API_KEY` and `GH_PAT`,
- verifies reachability of `api.github.com` and `openrouter.ai` (skip with `SKIP_REACHABILITY=1`),
- spawns `python -m orchestrator` and restarts it with exponential backoff + jitter,
- polls on idle using `AGENT_POLL_INTERVAL` (default `10.0`s),
- supports `RUN_ONCE=1` for container/task usage and local dev.

```bash
docker build -t agentic-developer-core .
docker run --rm \
  -e OPENROUTER_API_KEY=... -e GH_PAT=... \
  -e GITHUB_REPOSITORY=owner/repo \
  agentic-developer-core
```

## Environment & labels

Copy [`.env.example`](.env.example) to `.env` for the full environment surface. Key variables:

| Variable | Purpose |
| --- | --- |
| `OPENROUTER_API_KEY` | LLM backend access (required). |
| `GH_PAT` / `GH_TOKEN` | GitHub authentication for PR/issue operations (required). |
| `AGENT_MODE` | `ci` (GitHub Actions) or `local`. |
| `GITHUB_REPOSITORY` | The [Target Repository](CONTEXT.md) (`owner/repo`) to act on. Defaults to the orchestrator's own remote. |
| `GITHUB_WORKSPACE` | Local directory the Worker edits inside ([Workspace Isolation](CONTEXT.md)). |
| `AGENT_LOG_PATH` | Where state, traces, and metrics are written (default `.agent_logs`). |
| `AGENT_LABEL_READY` / `AGENT_LABEL_IN_PROGRESS` / `AGENT_LABEL_BLOCKED` | Issue label lifecycle (defaults: `agent-ready` / `agent-in-progress` / `agent-blocked`). |

**Issue label lifecycle**: `agent-ready` → `agent-in-progress` (claimed) / `agent-blocked` (open dependencies) → `agent-ready` (dependencies closed, or recovery after failure).

---

## Get Started

### Prerequisites

- **Python 3.12+**
- **OpenRouter API Key** (LLM backend access)
- **GitHub PAT** (with repo permission to manage PRs and issues)

### Installation & Setup

1. **Clone the repository and install dependencies** (the project uses [uv](https://docs.astral.sh/uv/)):
   ```bash
   uv venv
   source .venv/bin/activate
   uv pip install -e ".[dev]"      # editable install with dev tools; or: uv sync
   ```

2. **Configure environment variables** — copy the example configuration to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   ```

3. **Install the Pre-Commit Hook** — installs a native git hook that checks staged files with the lightweight, zero-dependency secret scanner:
   ```bash
   make setup
   ```

4. **Run the orchestrator** (local development):
   ```bash
   python -m orchestrator          # one cycle (resumes from .agent_logs/state.json if present)
   python scripts/entrypoint.py    # supervisor loop with backoff + polling (container entrypoint)
   ```

---

## Development & verification

### Running verification

```bash
make verify
```

### Optional strict pre-commit security gate

`make verify` is the fast, deterministic gate used in-cycle by the autonomous loop — it deliberately excludes slow, environment-stateful security scans (semgrep, pip-audit), which run in CI instead (ADR-0020).

To catch security-sensitive changes (new subprocess calls, URL-fetch logic, auth handling) at commit time rather than minutes later in CI, run the opt-in strict gate on demand:

```bash
make pre-commit-strict
```

This runs the fast deterministic checks (ruff + mypy) plus a local semgrep scan that mirrors the CI security scan (`semgrep scan --config=auto --error`). It is **opt-in and non-default**: it trades developer flow speed for earlier security detection. It is not wired into `make setup`'s git hook (which remains the fast ruff/mypy/secret-scan path) — invoke it explicitly when working on security-sensitive code.

---

## Architecture & guidelines

For terminology, agent roles, and design specifications, refer to the authoritative references — the README links to them rather than duplicating their content:

- [CONTEXT.md](CONTEXT.md) — canonical glossary (Orchestrator, Worker, Localization, Stateful Resume, Read-Before-Edit, Blocked by, Hybrid Retry, Merge-Fix Loop, BinEval, PR Review Judges, and more).
- [PRD.md](PRD.md) — Product Requirement Document outlining the Python LangGraph transition path.
- [docs/adr/](docs/adr/) — Architecture Decision Records. Notable entries:
  - [ADR-0009](docs/adr/0009-pure-python-langgraph-orchestrator.md) — Pure Python LangGraph orchestration (no CLI harness)
  - [ADR-0010](docs/adr/0010-test-first-orchestration-loop.md) — Test-First Orchestration Loop
  - [ADR-0014](docs/adr/0014-pr-verification-and-llm-judge-review-integration.md) — PR Verification and LLM Judge Review Integration
  - [ADR-0016](docs/adr/0016-in-process-telemetry-and-stateful-resume-tracing.md) — In-Process Telemetry and Stateful Resume Tracing
  - [ADR-0033](docs/adr/0033-line-range-scoped-read-before-edit.md) — Line-Range-Scoped Read-Before-Edit
  - [ADR-0034](docs/adr/0034-hybrid-retry-hard-rollback-on-final-attempt.md) — Hybrid Retry (hard rollback)
  - [ADR-0036](docs/adr/0036-closed-post-pr-judge-feedback-loop.md) — Closed Post-PR Judge-Feedback Loop
  - [ADR-0037](docs/adr/0037-worker-tool-trust-boundary-hardening.md) — Worker Tool Trust Boundary Hardening
