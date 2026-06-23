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

### Core Architecture & Guidelines
For details about the terminology, agent roles, and design specifications, refer to:
- [CONTEXT.md](CONTEXT.md) - Canonical glossary of terms, including:
  - **Stateful Resume**: Crashed or restarted loops recover state atomically from `.agent_logs/state.json`.
  - **Read-Before-Edit Constraint**: Agents must read files in the active loop cycle prior to applying patches.
  - **Blocked by**: Structured dependency checking for claimed issues.
- [PRD.md](PRD.md) - Product Requirement Document outlining the Python LangGraph transition path.

