# agentic-developer-core

An autonomous software engineer agent using LangGraph and LangChain for stateful development loops, precise code modifications, and automated verification.

---

## Get Started

### Prerequisites
- **Python 3.12+**
- **Gitleaks** (Must be installed on your local system PATH; `make setup` only configures the hook harness, not the system binary itself):
  - *macOS*: `brew install gitleaks`
  - *Linux (Ubuntu/Debian)*: `sudo apt install gitleaks`
  - *Other*: Download from the [Gitleaks Releases](https://github.com/gitleaks/gitleaks/releases) page and add to your PATH.
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

3. **Install Pre-Commit Hooks:**
   Run the setup target to install `pre-commit` and configure the local Gitleaks commit checks:
   ```bash
   make setup
   ```

---

## Development & Verification

### Running Verification
To execute the unit tests and ensure code quality:
```bash
make verify
```

### Core Architecture & Guidelines
For details about the terminology, agent roles, and design specifications, refer to:
- [CONTEXT.md](CONTEXT.md) - Canonical glossary of terms, including:
  - **Stateful Resume**: Crashed or restarted loops recover state atomically from `.agent_logs/state.json`.
  - **Read-Before-Edit Constraint**: Agents must read files in the active loop cycle prior to applying patches.
  - **AGENT_DECISION** & **AGENT_RESOLVED**: Standardized comment formats for self-documenting undocumented code choices and spec conflict resolutions.
- [PRD.md](PRD.md) - Product Requirement Document outlining the Python LangGraph transition path.
