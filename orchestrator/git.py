import logging
import subprocess
from pathlib import Path

logger = logging.getLogger("orchestrator.git")


class GitError(Exception):
    """Base exception for all Git operations."""


class NotAGitRepositoryError(GitError):
    """Raised when a Git operation is attempted in a non-repository directory."""


class GitCommandError(GitError):
    """Raised when a Git command exits with a non-zero status."""

    def __init__(self, command: list[str], returncode: int, stdout: str, stderr: str):
        super().__init__(
            f"Git command {' '.join(command)} failed with exit code {returncode}.\nStderr: {stderr}"
        )
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class GitPushConflictError(GitCommandError):
    """Raised when a Git push fails due to conflicts or non-fast-forward updates."""


def _run_git(
    repo_dir: Path | str, args: list[str], check: bool = True
) -> subprocess.CompletedProcess[str]:
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
            errors="replace",
        )
    except FileNotFoundError as e:
        raise GitError("Git executable not found on the system path.") from e
    except Exception as e:
        raise GitError(f"Failed to execute git command: {e}") from e

    if check and result.returncode != 0:
        if "push" in args and any(
            x in result.stderr for x in ["rejected", "non-fast-forward", "fetch first"]
        ):
            raise GitPushConflictError(
                cmd, result.returncode, result.stdout, result.stderr
            )
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
        exists_check = _run_git(
            repo_dir, ["show-ref", "--verify", f"refs/heads/{branch}"], check=False
        )
        if exists_check.returncode == 0:
            logger.info(
                "Branch '%s' already exists locally. Falling back to checkout.", branch
            )
            _run_git(repo_dir, ["checkout", branch])
        else:
            _run_git(repo_dir, ["checkout", "-b", branch])
    else:
        _run_git(repo_dir, ["checkout", branch])


def commit(repo_dir: Path | str, message: str, author: str | None = None) -> bool:
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


def _unstage_all(repo_dir: Path | str) -> None:
    """Best-effort mixed reset so nothing remains staged (working tree untouched).

    Restores the index to ``HEAD`` after :func:`diff_cached` has captured the
    candidate diff, re-establishing the invariant the ADR-0034 hard reset relies
    on: *nothing is staged until the PR-Node*. A mixed reset unstages new files
    (returning them to untracked) and tracked modifications alike, leaving the
    working tree exactly as it was — so a later ``git reset --hard HEAD`` reverts
    only tracked-file edits and leaves Worker-created files on disk.

    Idempotent across repeated verify→execute cycles. Never raises: on failure
    (e.g. unborn HEAD, where ``git reset`` has no ref to reset to) it logs and
    returns, preserving the soft-gate contract that ``diff_cached`` never raises.
    """
    try:
        _run_git(repo_dir, ["reset", "-q"], check=False)
    except GitError as e:
        logger.debug("unstage failed in %s (non-fatal): %s", repo_dir, e)


def diff_cached(repo_dir: Path | str) -> str:
    """Return the staged diff (tracked changes + newly added files) vs HEAD.

    Stages all current changes first (tracked modifications and untracked
    files) via ``git add -A``, then returns ``git diff --cached`` output. This
    captures the full set of candidate PR changes including new files, which a
    bare ``git diff HEAD`` would miss (untracked files are not shown). Staging is
    idempotent and never commits — the PR-Node re-stages before committing.

    After capturing the diff the index is **unstaged** via :func:`_unstage_all`
    (a mixed ``git reset -q``), so the workspace returns to its pre-BinEval
    state: nothing staged, Worker-created files untracked again. This restores
    the ADR-0034 hard-reset invariant — a later ``git reset --hard HEAD`` reverts
    only tracked-file edits and leaves untracked Worker output intact. Without
    it, the staged new files would be deleted from the working tree by the hard
    reset, wiping the Worker's prior work on the final retry attempt.

    Returns the diff text, or an empty string if the directory is not a git
    repository or the diff command fails. Never raises: BinEval treats an empty
    diff as a signal to skip the soft gate (edge-case policy). The index is
    unstaged on every return path — success, diff failure, and exception — so no
    staged residue is ever left behind.
    """
    try:
        _run_git(repo_dir, ["add", "-A"], check=False)
        result = _run_git(repo_dir, ["diff", "--cached"], check=False)
    except GitError as e:
        logger.debug("diff_cached failed in %s: %s", repo_dir, e)
        _unstage_all(repo_dir)
        return ""

    diff = result.stdout if result.returncode == 0 else ""
    _unstage_all(repo_dir)
    return diff


def diff_name_only(repo_dir: Path | str, base: str = "origin/main...HEAD") -> str:
    """Return the ``--name-only`` diff of changed files between ``base`` and HEAD.

    Uses the three-dot range ``origin/main...HEAD`` by default to capture every
    file changed on the feature branch since it diverged from the default
    branch — all changes across the branch, not just the latest commit. This is
    the input for Plan Alignment (Run Observability, ADR-0029), called from the
    PR-Node right after the branch commit so HEAD carries the committed work.

    Returns the raw stdout (one path per line), or an empty string if the
    directory is not a git repository or the range ref is unavailable. Never
    raises: a failed diff degrades Plan Alignment to ``None``.
    """
    try:
        result = _run_git(repo_dir, ["diff", "--name-only", base], check=False)
        if result.returncode != 0:
            logger.debug(
                "diff_name_only(%s) failed (rc=%d): %s",
                base,
                result.returncode,
                result.stderr.strip(),
            )
            return ""
        return result.stdout
    except GitError as e:
        logger.debug("diff_name_only failed in %s: %s", repo_dir, e)
        return ""


def diff_stat(repo_dir: Path | str, base: str = "origin/main...HEAD") -> str:
    """Return the ``--stat`` diff between ``base`` and HEAD.

    Uses the three-dot range ``origin/main...HEAD`` by default so the stat
    summarizes every file changed on the feature branch since it diverged from
    the default branch (the same range :func:`diff_name_only` uses for Plan
    Alignment). The stat is re-derived at PR time from the committed branch
    state — no in-flight data is carried through :class:`AgentState` for it.

    Returns the raw ``git diff --stat`` stdout (one line per changed file plus
    a trailing ``N files changed`` summary), or an empty string if the
    directory is not a git repository or the range ref is unavailable. Never
    raises: a failed stat degrades the PR body's Key Changes section to a note.
    """
    try:
        result = _run_git(repo_dir, ["diff", "--stat", base], check=False)
        if result.returncode != 0:
            logger.debug(
                "diff_stat(%s) failed (rc=%d): %s",
                base,
                result.returncode,
                result.stderr.strip(),
            )
            return ""
        return result.stdout
    except GitError as e:
        logger.debug("diff_stat failed in %s: %s", repo_dir, e)
        return ""


def push(
    repo_dir: Path | str, branch: str, remote: str = "origin", force: bool = False
) -> None:
    """Pushes the specified branch to the remote repository."""
    cmd = ["push", remote, branch]
    if force:
        cmd.append("--force")
    _run_git(repo_dir, cmd)


def clean(
    repo_dir: Path | str, force: bool = True, remove_directories: bool = True
) -> None:
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


def get_remote_url(repo_dir: Path | str, remote: str = "origin") -> str:
    """Returns the remote URL for the specified remote name."""
    result = _run_git(repo_dir, ["remote", "get-url", remote])
    return result.stdout.strip()


def clone(repo_dir: Path | str, github_repo: str, token: str | None = None) -> None:
    """Clones the target GitHub repository into the specified directory.

    Utilizes Git's credential helper with an environment variable reference to prevent
    persisting the plaintext token in the local git config.
    """
    clean_url = f"https://github.com/{github_repo}.git"

    if token:
        token_stripped = token.strip()
        import os

        # Ensure the token is set in the environment of any subprocess
        # referencing GH_PAT (or GITHUB_TOKEN if needed)
        os.environ["GH_PAT"] = token_stripped

        # Configure credential helper referencing the GH_PAT env var
        helper_cmd = (
            '!f() { echo "username=x-access-token"; echo "password=$GH_PAT"; }; f'
        )
        cmd = [
            "clone",
            "-c",
            f"credential.helper={helper_cmd}",
            clean_url,
            str(repo_dir),
        ]
    else:
        cmd = ["clone", clean_url, str(repo_dir)]

    parent_dir = Path(repo_dir).parent
    parent_dir.mkdir(parents=True, exist_ok=True)
    _run_git(parent_dir, cmd)


def get_commit_time(repo_dir: Path | str, commit_ref: str = "HEAD") -> str:
    """Returns the committer date of the specified commit ref in ISO 8601 format."""
    result = _run_git(repo_dir, ["show", "-s", "--format=%cI", commit_ref])
    return result.stdout.strip()


def add(repo_dir: Path | str, path_spec: str = ".") -> None:
    """Stages files matching path_spec (defaults to all files in work tree)."""
    _run_git(repo_dir, ["add", path_spec])
