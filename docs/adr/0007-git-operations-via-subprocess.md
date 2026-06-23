# ADR 0007: Git Operations via Python Subprocess

## Status
Accepted

## Context
The Python-based orchestrator needs to perform git workspace hygiene and lifecycle commands (such as cloning repositories, checking out feature branches, committing changes, pushing commits, and running `git clean` or `git reset`). We need to decide how to interface with Git from our Python code.

## Decision
We will execute git operations using Python's native `subprocess` module to call the system `git` binary directly (e.g., using `subprocess.run(["git", "checkout", branch_name], shell=False)`). 

To ensure safety and security, we will:
1. Avoid `shell=True` to prevent command injection risks.
2. Wrap execution in a helper function that logs the commands and validates return codes.

## Rejected Alternatives

### 1. GitPython Library
We rejected using GitPython or similar native Python Git wrappers.
- **Why**: GitPython introduces heavy external dependencies that must be compiled and installed in our Alpine-based Docker container. Configuring GitHub authentication via Personal Access Tokens (PAT) inside GitPython's abstraction layer is complex and prone to edge-case errors. Calling the standard `git` CLI natively is simpler, lighter, and matches developer expectations.

## Consequences

### Pros
- **Zero Dependencies**: Relies entirely on Python standard libraries and the container's pre-installed `git` binary.
- **Readability**: Logs show the exact command-line syntax (e.g., `git push origin feat/branch`), making issues easy to debug.
- **Simple Authentication**: Subprocesses inherit environment variables (like `GH_PAT`), allowing standard Git credential helpers or URL injection to work natively.

### Cons
- **Output Parsing**: Any information we need from git (like branch lists or status) must be parsed from stdout string outputs rather than accessed via structured object properties.
