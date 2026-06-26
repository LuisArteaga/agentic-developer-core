import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from orchestrator import state as state_module
from orchestrator.state import AgentState
from orchestrator.git import is_git_repository, checkout, clean, reset_hard

logger = logging.getLogger("orchestrator.nodes")

def _run_gh(args: list[str]) -> str:
    """Run a GitHub CLI command and return its stdout, raising RuntimeError on failure."""
    cmd = ["gh"] + args
    logger.debug("Running gh command: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, shell=False)
    if result.returncode != 0:
        raise RuntimeError(f"GitHub CLI command {' '.join(cmd)} failed (code {result.returncode}): {result.stderr.strip()}")
    return result.stdout

def _get_github_repository(workspace_path: Path) -> str:
    """Resolve the target owner/repo string, falling back to git remote origin if GITHUB_REPOSITORY is empty."""
    repo = os.getenv("GITHUB_REPOSITORY", "").strip()
    if repo:
        return repo

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            shell=False,
            check=True
        )
        url = result.stdout.strip()
        # Parse owner/repo from SSH (git@github.com:owner/repo.git) or HTTPS (https://github.com/owner/repo.git)
        if "github.com/" in url:
            part = url.split("github.com/", 1)[1]
        elif "github.com:" in url:
            part = url.split("github.com:", 1)[1]
        else:
            # Fallback for other formats
            part = url.split(":")[-1]

        if part.endswith(".git"):
            part = part[:-4]
        return part
    except Exception as e:
        raise ValueError(
            f"GITHUB_REPOSITORY environment variable is not set and could not be resolved from git remote origin: {e}"
        )

def _parse_dependencies(body: Optional[str]) -> list[int]:
    """Parse the '## Blocked by' section of an issue body and return a list of blocked-by issue numbers."""
    if not body:
        return []
    
    # Find "## Blocked by" case-insensitively
    match = re.search(r"(?i)##\s*Blocked\s+by\b", body)
    if not match:
        return []
    
    # Extract everything after the header
    start_idx = match.end()
    remaining = body[start_idx:]
    
    # Read until the next markdown header (line starting with #)
    next_header = re.search(r"\n\s*#", remaining)
    section_text = remaining[:next_header.start()] if next_header else remaining
    
    # Extract all issue numbers like #123
    issue_numbers = re.findall(r"#(\d+)", section_text)
    return [int(num) for num in issue_numbers]

def _checkout_default_branch(workspace_path: Path) -> None:
    """Attempt to checkout the default branch (main or master)."""
    try:
        checkout(workspace_path, "main")
    except Exception:
        try:
            checkout(workspace_path, "master")
        except Exception as e:
            logger.error("Failed to checkout default branch (main/master): %s", e)
            raise

def claim_node(state: AgentState) -> AgentState:
    """Programmatically polls GitHub issues, performs workspace hygiene, claims an issue, and updates the state.
    
    Implements the two-step Self-Healing Claim process and handles the resume path.
    """
    logger.info("Starting Claim phase...")
    
    # 1. Load configuration and resolve paths
    label_ready = os.getenv("AGENT_LABEL_READY", "agent-ready").strip()
    label_in_progress = os.getenv("AGENT_LABEL_IN_PROGRESS", "agent-in-progress").strip()
    label_blocked = os.getenv("AGENT_LABEL_BLOCKED", "agent-blocked").strip()
    
    workspace_env = os.getenv("GITHUB_WORKSPACE", ".")
    workspace_path = Path(workspace_env).resolve()
    
    # Try to resolve the GITHUB_REPOSITORY (either from env or git remote)
    try:
        # If the workspace doesn't exist yet, we check the env first.
        # If env is empty, we must resolve it in the current orchestrator directory first to clone.
        if not workspace_path.exists() or not is_git_repository(workspace_path):
            github_repo = _get_github_repository(Path(__file__).resolve().parent.parent)
        else:
            github_repo = _get_github_repository(workspace_path)
    except Exception as e:
        logger.error("Failed to resolve target repository: %s", e)
        state["status"] = "failed"
        state_module.save(state)
        return state

    # 2. Check for Resume
    # Resume is active if state has an issue_number and branch, and status is not idle/done/failed.
    resume = False
    if (
        state.get("issue_number") is not None 
        and state.get("branch") is not None 
        and state.get("status") not in ("idle", "done", "failed")
    ):
        resume = True

    # 3. Workspace Hygiene & Cloning
    try:
        # If the workspace directory doesn't exist or is not a git repo, clone it.
        if not workspace_path.exists() or not is_git_repository(workspace_path):
            logger.info("Workspace '%s' does not exist or is not a git repository. Cloning %s...", workspace_path, github_repo)
            workspace_path.mkdir(parents=True, exist_ok=True)
            # Use gh repo clone which handles auth natively
            _run_gh(["repo", "clone", github_repo, str(workspace_path)])
            logger.info("Repository cloned successfully into %s.", workspace_path)

        if resume:
            # On resume, skip clean and reset, and checkout the active branch
            branch_name = state["branch"]
            logger.info("Resuming. Skipping workspace cleanup. Checking out branch '%s'...", branch_name)
            checkout(workspace_path, branch_name)
        else:
            # On fresh claim, checkout default branch, hard reset, and clean
            logger.info("Fresh run. Performing workspace cleanup...")
            _checkout_default_branch(workspace_path)
            reset_hard(workspace_path, "HEAD")
            clean(workspace_path)
            logger.info("Workspace hygiene complete.")
    except Exception as e:
        logger.error("Workspace hygiene/cloning failed: %s", e)
        state["status"] = "failed"
        state_module.save(state)
        return state

    # 4. Step 1: Unblocking Scan (0 LLM Tokens)
    if not resume:
        logger.info("Step 1: Running Unblocking Scan for '%s' issues...", label_blocked)
        try:
            blocked_issues_json = _run_gh([
                "issue", "list",
                "-R", github_repo,
                "--label", label_blocked,
                "--state", "open",
                "--json", "number,body",
                "--limit", "1000"
            ])
            blocked_issues = json.loads(blocked_issues_json)
        except Exception as e:
            logger.error("Failed to poll blocked issues: %s", e)
            blocked_issues = []

        for issue in blocked_issues:
            issue_num = issue["number"]
            body = issue.get("body") or ""
            dependencies = _parse_dependencies(body)
            if not dependencies:
                continue
            
            # Check if all dependencies are closed
            all_closed = True
            for dep in dependencies:
                try:
                    dep_state_json = _run_gh([
                        "issue", "view",
                        str(dep),
                        "-R", github_repo,
                        "--json", "state"
                    ])
                    dep_state = json.loads(dep_state_json)
                    if dep_state.get("state") != "CLOSED":
                        all_closed = False
                        break
                except Exception as e:
                    logger.warning("Failed to check status of dependency issue #%d: %s", dep, e)
                    all_closed = False
                    break
            
            if all_closed:
                logger.info("Unblocking issue #%d since all dependencies are closed.", issue_num)
                try:
                    _run_gh([
                        "issue", "edit",
                        str(issue_num),
                        "-R", github_repo,
                        "--add-label", label_ready,
                        "--remove-label", label_blocked
                    ])
                except Exception as e:
                    logger.error("Failed to update labels for unblocked issue #%d: %s", issue_num, e)

    # 5. Step 2: Claim Scan (only if not resuming)
    claimed_issue = None
    if not resume:
        logger.info("Step 2: Running Claim Scan for '%s' issues...", label_ready)
        try:
            ready_issues_json = _run_gh([
                "issue", "list",
                "-R", github_repo,
                "--label", label_ready,
                "--state", "open",
                "--json", "number,title,body",
                "--limit", "1000"
            ])
            ready_issues = json.loads(ready_issues_json)
        except Exception as e:
            logger.error("Failed to poll ready issues: %s", e)
            ready_issues = []

        for issue in ready_issues:
            issue_num = issue["number"]
            title = issue["title"]
            body = issue.get("body") or ""
            dependencies = _parse_dependencies(body)
            
            # Check if there are any open dependencies
            has_open_dep = False
            for dep in dependencies:
                try:
                    dep_state_json = _run_gh([
                        "issue", "view",
                        str(dep),
                        "-R", github_repo,
                        "--json", "state"
                    ])
                    dep_state = json.loads(dep_state_json)
                    if dep_state.get("state") != "CLOSED":
                        has_open_dep = True
                        break
                except Exception as e:
                    logger.warning("Failed to check dependency issue #%d for ready issue #%d: %s", dep, issue_num, e)
                    has_open_dep = True
                    break
            
            if has_open_dep:
                logger.info("Issue #%d has open dependencies. Transitioning to '%s'...", issue_num, label_blocked)
                try:
                    _run_gh([
                        "issue", "edit",
                        str(issue_num),
                        "-R", github_repo,
                        "--add-label", label_blocked,
                        "--remove-label", label_ready
                    ])
                except Exception as e:
                    logger.error("Failed to transition issue #%d to blocked: %s", issue_num, e)
                continue
            
            # Double check if the issue is still ready (concurrency check)
            try:
                view_json = _run_gh([
                    "issue", "view",
                    str(issue_num),
                    "-R", github_repo,
                    "--json", "labels"
                ])
                view_data = json.loads(view_json)
                labels = [l["name"] for l in view_data.get("labels", [])]
                if label_ready not in labels:
                    logger.warning("Issue #%d no longer has '%s' label. Skipping.", issue_num, label_ready)
                    continue
            except Exception as e:
                logger.error("Failed to verify labels for issue #%d: %s. Skipping.", issue_num, e)
                continue
            
            # Attempt to claim the issue atomically
            try:
                _run_gh([
                    "issue", "edit",
                    str(issue_num),
                    "-R", github_repo,
                    "--add-label", label_in_progress,
                    "--remove-label", label_ready
                ])
                logger.info("Successfully claimed issue #%d: '%s'.", issue_num, title)
                claimed_issue = issue
                break
            except Exception as e:
                logger.error("Failed to claim issue #%d: %s. Skipping.", issue_num, e)
                continue

        # 6. Post-Claim Branch Creation & State Update
        if claimed_issue:
            issue_num = claimed_issue["number"]
            branch_name = f"feat/issue-{issue_num}"
            
            state["issue_number"] = issue_num
            state["status"] = "claimed"
            state["phase"] = "claimed"
            state["branch"] = branch_name
            state["plan"] = None
            state["read_files"] = []
            
            try:
                # Create and checkout the local feature branch
                checkout(workspace_path, branch_name, create=True)
                logger.info("Created and checked out feature branch '%s' in workspace.", branch_name)
            except Exception as e:
                logger.error("Failed to create feature branch '%s': %s", branch_name, e)
                # Do not abort node execution if git branch checkout fails; we still have the claim
        else:
            logger.info("No eligible issues found to claim. Orchestrator transitioning to idle.")
            state["status"] = "idle"
            state["phase"] = ""
            
    # Save the updated state
    state_module.save(state)
    return state
