import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("orchestrator.git")

class GitError(Exception):
    """Base exception for all Git operations."""
    pass

class NotAGitRepositoryError(GitError):
    """Raised when a Git operation is attempted in a non-repository directory."""
    pass

class GitCommandError(GitError):
    """Raised when a Git command exits with a non-zero status."""
    def __init__(self, command: list[str], returncode: int, stdout: str, stderr: str):
        super().__init__(f"Git command {' '.join(command)} failed with exit code {returncode}.\nStderr: {stderr}")
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

class GitPushConflictError(GitCommandError):
    """Raised when a Git push fails due to conflicts or non-fast-forward updates."""
    pass

def _run_git(repo_dir: Path | str, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Runs a Git command in the specified directory, capturing stdout and stderr.
    
    Args:
        repo_dir: The directory to run the command in.
        args: The command arguments (excluding the 'git' binary itself).
        check: If True, raises GitCommandError on non-zero exit codes.
    """
    cmd = ["git"] + args
    logger.debug("Running git command: %s in %s", " ".join(cmd), repo_dir)
    
    try:
        result = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            shell=False,
            errors="replace"
        )
    except FileNotFoundError as e:
        raise GitError("Git executable not found on the system path.") from e
    except Exception as e:
        raise GitError(f"Failed to execute git command: {e}") from e
        
    if check and result.returncode != 0:
        if "push" in args and any(
            x in result.stderr for x in ["rejected", "non-fast-forward", "fetch first"]
        ):
            raise GitPushConflictError(cmd, result.returncode, result.stdout, result.stderr)
        raise GitCommandError(cmd, result.returncode, result.stdout, result.stderr)
        
    return result

def is_git_repository(repo_dir: Path | str) -> bool:
    """Verifies if the specified directory is inside a Git repository work tree."""
    try:
        result = _run_git(repo_dir, ["rev-parse", "--is-inside-work-tree"], check=False)
        return result.returncode == 0
    except GitError:
        return False

def checkout(repo_dir: Path | str, branch: str, create: bool = False) -> None:
    """Checks out the specified branch. If create is True, attempts to create it first.
    
    If the branch already exists locally and create is True, handles it gracefully by
    falling back to a standard checkout of the existing branch.
    """
    if create:
        # Check if branch already exists locally
        exists_check = _run_git(repo_dir, ["show-ref", "--verify", f"refs/heads/{branch}"], check=False)
        if exists_check.returncode == 0:
            logger.info("Branch '%s' already exists locally. Falling back to checkout.", branch)
            _run_git(repo_dir, ["checkout", branch])
        else:
            _run_git(repo_dir, ["checkout", "-b", branch])
    else:
        _run_git(repo_dir, ["checkout", branch])

def commit(repo_dir: Path | str, message: str, author: Optional[str] = None) -> bool:
    """Commits staged changes. If there are no staged changes, returns False without raising an error.
    
    Returns:
        True if a commit was created, False otherwise.
    """
    # Check if there are staged changes using `git diff --cached --quiet`
    # --quiet exits with 1 if there are changes, and 0 if none.
    diff_check = _run_git(repo_dir, ["diff", "--cached", "--quiet"], check=False)
    if diff_check.returncode == 0:
        logger.info("No staged changes to commit in %s.", repo_dir)
        return False
        
    cmd = ["commit", "-m", message]
    if author:
        cmd += ["--author", author]
        
    _run_git(repo_dir, cmd)
    return True

def push(repo_dir: Path | str, branch: str, remote: str = "origin", force: bool = False) -> None:
    """Pushes the specified branch to the remote repository."""
    cmd = ["push", remote, branch]
    if force:
        cmd.append("--force")
    _run_git(repo_dir, cmd)

def clean(repo_dir: Path | str, force: bool = True, remove_directories: bool = True) -> None:
    """Cleans untracked files from the working directory.
    
    Raises NotAGitRepositoryError if the directory is not a Git repository.
    """
    if not is_git_repository(repo_dir):
        raise NotAGitRepositoryError(f"Directory '{repo_dir}' is not a Git repository.")
        
    cmd = ["clean"]
    if force:
        cmd.append("-f")
    if remove_directories:
        cmd.append("-d")
        
    _run_git(repo_dir, cmd)

def reset_hard(repo_dir: Path | str, commit_or_ref: str = "HEAD") -> None:
    """Performs a hard reset to the specified commit or ref."""
    _run_git(repo_dir, ["reset", "--hard", commit_or_ref])

def current_branch(repo_dir: Path | str) -> str:
    """Returns the name of the current active branch.
    
    If the repository is in a detached HEAD state or has no commits, returns 'HEAD' or the default branch name.
    """
    try:
        result = _run_git(repo_dir, ["branch", "--show-current"])
        branch_name = result.stdout.strip()
        if branch_name:
            return branch_name
    except GitError:
        pass

    try:
        rev_result = _run_git(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"])
        return rev_result.stdout.strip()
    except GitError:
        try:
            sym_result = _run_git(repo_dir, ["symbolic-ref", "--short", "HEAD"])
            return sym_result.stdout.strip()
        except GitError:
            return "HEAD"
