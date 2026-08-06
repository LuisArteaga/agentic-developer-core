# agentic-developer-core

An autonomous software engineer agent using LangGraph and LangChain for stateful development loops, precise code modifications, and automated verification.

---

## Get Started

### Prerequisites
- **Python 3.12+**
- **OpenRouter API Key** (for LLM backend access)
- **GitHub PAT** (with repo permission to manage PRs and issues)


### Installation & Setup

1. **Clone the repository and set up dependencies:**
   ```bash
   # If using uv (recommended)
   uv venv
   source .venv/bin/activate
   uv pip install -r pyproject.toml
   ```

2. **Configure environment variables:**
   Copy the example configuration to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   ```

3. **Install Pre-Commit Hook:**
   Run the setup target to install a native git pre-commit hook that checks staged files using our lightweight, zero-dependency Python secret scanner:
   ```bash
   make setup
   ```

---

## Development & Verification

### Running Verification
To execute the unit tests and run the secret scanner check:
```bash
make verify
```

### Optional Strict Pre-Commit Security Gate
`make verify` is the fast, deterministic gate used in-cycle by the autonomous loop — it deliberately excludes slow, environment-stateful security scans (semgrep, pip-audit), which run in CI instead (see [ADR-0020](docs/adr/0020-deterministic-quality-gates-for-orchestrator-codebase.md)).

To catch security-sensitive changes (new subprocess calls, URL-fetch logic, auth handling) at commit time rather than minutes later in CI, run the opt-in strict gate on demand:
```bash
make pre-commit-strict
```
This runs the fast deterministic checks (ruff + mypy) plus a local semgrep scan that mirrors the CI security scan (`semgrep scan --config=auto --error`). It is **opt-in and non-default**: it trades developer flow speed for earlier security detection. It is not wired into `make setup`'s git hook (which remains the fast ruff/mypy/secret-scan path) — invoke it explicitly when working on security-sensitive code.

### Core Architecture & Guidelines
For details about the terminology, agent roles, and design specifications, refer to:
- [CONTEXT.md](CONTEXT.md) - Canonical glossary of terms, including:
  - **Stateful Resume**: Crashed or restarted loops recover state atomically from `.agent_logs/state.json`.
  - **Read-Before-Edit Constraint**: Agents must read files in the active loop cycle prior to applying patches.
  - **Blocked by**: Structured dependency checking for claimed issues.
- [PRD.md](PRD.md) - Product Requirement Document outlining the Python LangGraph transition path.

