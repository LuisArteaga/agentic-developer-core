import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import git
from orchestrator.git import (
    GitError,
    GitPushConflictError,
    NotAGitRepositoryError,
    checkout,
    clean,
    commit,
    current_branch,
    is_git_repository,
    push,
    reset_hard,
)


class TestGitSubprocessHelper(unittest.TestCase):
    def setUp(self):
        # Create an isolated temporary directory for the local repository
        self.local_temp = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.local_temp.name).resolve()

        # Initialize a clean git repository
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=str(self.repo_path),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=str(self.repo_path),
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=str(self.repo_path),
            check=True,
        )

        # Create an initial commit so HEAD exists and we have a base
        self.test_file = self.repo_path / "initial.txt"
        self.test_file.write_text("initial content", encoding="utf-8")
        subprocess.run(
            ["git", "add", "initial.txt"], cwd=str(self.repo_path), check=True
        )
        subprocess.run(
            ["git", "commit", "-m", "initial commit"],
            cwd=str(self.repo_path),
            check=True,
        )

    def tearDown(self):
        self.local_temp.cleanup()

    def test_is_git_repository(self):
        """Test is_git_repository returns True for a valid repo and False for others."""
        self.assertTrue(is_git_repository(self.repo_path))

        # Create a plain directory outside the repository
        with tempfile.TemporaryDirectory() as plain_dir:
            self.assertFalse(is_git_repository(plain_dir))

    def test_current_branch(self):
        """Test current_branch returns the correct active branch name."""
        self.assertEqual(current_branch(self.repo_path), "main")

        # Create and checkout a new branch
        subprocess.run(
            ["git", "checkout", "-b", "feature-test"],
            cwd=str(self.repo_path),
            check=True,
            capture_output=True,
        )
        self.assertEqual(current_branch(self.repo_path), "feature-test")

        # Test detached HEAD state
        # Checkout the initial commit directly
        rev_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self.repo_path),
            check=True,
            capture_output=True,
            text=True,
        )
        commit_hash = rev_result.stdout.strip()

        subprocess.run(
            ["git", "checkout", commit_hash],
            cwd=str(self.repo_path),
            check=True,
            capture_output=True,
        )
        # Should return the commit hash or "HEAD"
        self.assertEqual(current_branch(self.repo_path), "HEAD")

    def test_checkout_existing_and_new(self):
        """Test checkout function for new and existing branches."""
        # 1. Create a new branch
        checkout(self.repo_path, "feature-1", create=True)
        self.assertEqual(current_branch(self.repo_path), "feature-1")

        # 2. Checkout existing branch
        checkout(self.repo_path, "main", create=False)
        self.assertEqual(current_branch(self.repo_path), "main")

        # 3. Graceful handling: checkout with create=True on an already existing branch
        # This should not raise an error, but switch to the existing branch
        checkout(self.repo_path, "feature-1", create=True)
        self.assertEqual(current_branch(self.repo_path), "feature-1")

    def test_commit_with_and_without_staged_changes(self):
        """Test commit function returns True on success and False when nothing to commit."""
        # 1. Commit with no staged changes
        # Should return False and not raise any error
        result = commit(self.repo_path, "empty commit")
        self.assertFalse(result)

        # 2. Commit with staged changes
        new_file = self.repo_path / "new.txt"
        new_file.write_text("new content", encoding="utf-8")
        subprocess.run(["git", "add", "new.txt"], cwd=str(self.repo_path), check=True)

        result = commit(
            self.repo_path,
            "add new file",
            author="Another Author <another@example.com>",
        )
        self.assertTrue(result)

        # Verify the commit author using git log
        log_res = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"],
            cwd=str(self.repo_path),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(log_res.stdout.strip(), "Another Author <another@example.com>")

    def test_clean_and_reset_hard(self):
        """Test workspace hygiene commands: clean and reset_hard."""
        # Modify an existing tracked file
        self.test_file.write_text("modified content", encoding="utf-8")

        # Create an untracked file and an untracked directory
        untracked_file = self.repo_path / "untracked.txt"
        untracked_file.write_text("untracked", encoding="utf-8")

        untracked_dir = self.repo_path / "untracked_dir"
        untracked_dir.mkdir()
        (untracked_dir / "file.txt").write_text("nested untracked", encoding="utf-8")

        # 1. Test clean()
        # Clean untracked files and directories
        clean(self.repo_path, force=True, remove_directories=True)

        # Untracked files/folders must be deleted
        self.assertFalse(untracked_file.exists())
        self.assertFalse(untracked_dir.exists())
        # Tracked file modification should still be there
        self.assertEqual(self.test_file.read_text(encoding="utf-8"), "modified content")

        # 2. Test reset_hard()
        # Revert tracked file modification
        reset_hard(self.repo_path, "HEAD")
        self.assertEqual(self.test_file.read_text(encoding="utf-8"), "initial content")

        # 3. Test clean() in a non-repository directory raises NotAGitRepositoryError
        with tempfile.TemporaryDirectory() as plain_dir:
            with self.assertRaises(NotAGitRepositoryError):
                clean(plain_dir)

    def test_push_success_and_conflict(self):
        """Test push operations: success path and conflict path via a local mock remote."""
        # Create a second temp directory as a bare remote repository
        with tempfile.TemporaryDirectory() as remote_temp:
            remote_path = Path(remote_temp).resolve()
            subprocess.run(
                ["git", "init", "--bare", "-b", "main"],
                cwd=str(remote_path),
                check=True,
                capture_output=True,
            )

            # Add the remote to our local repository
            subprocess.run(
                ["git", "remote", "add", "origin", str(remote_path)],
                cwd=str(self.repo_path),
                check=True,
            )

            # 1. Push main branch (success path)
            push(self.repo_path, "main")

            # 2. Simulate concurrent push / conflict
            # Clone the bare remote to another temp directory (simulating a collaborator)
            with tempfile.TemporaryDirectory() as clone_temp:
                clone_path = Path(clone_temp).resolve()
                subprocess.run(
                    ["git", "clone", str(remote_path), str(clone_path)],
                    check=True,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "config", "user.name", "Collaborator"],
                    cwd=str(clone_path),
                    check=True,
                )
                subprocess.run(
                    ["git", "config", "user.email", "collab@example.com"],
                    cwd=str(clone_path),
                    check=True,
                )

                # Make a change in the clone and push it
                collab_file = clone_path / "initial.txt"
                collab_file.write_text("collaborator change", encoding="utf-8")
                subprocess.run(
                    ["git", "add", "initial.txt"], cwd=str(clone_path), check=True
                )
                subprocess.run(
                    ["git", "commit", "-m", "collab commit"],
                    cwd=str(clone_path),
                    check=True,
                )
                subprocess.run(
                    ["git", "push", "origin", "main"], cwd=str(clone_path), check=True
                )

                # In our local repository, make a conflicting change without fetching first
                self.test_file.write_text("our conflicting change", encoding="utf-8")
                subprocess.run(
                    ["git", "add", "initial.txt"], cwd=str(self.repo_path), check=True
                )
                subprocess.run(
                    ["git", "commit", "-m", "conflicting commit"],
                    cwd=str(self.repo_path),
                    check=True,
                )

                # Pushing now should fail and raise GitPushConflictError
                with self.assertRaises(GitPushConflictError) as ctx:
                    push(self.repo_path, "main")

                # Verify exception metadata
                self.assertIn("push", ctx.exception.command)
                self.assertNotEqual(ctx.exception.returncode, 0)
                self.assertTrue(
                    any(
                        x in ctx.exception.stderr
                        for x in ["rejected", "non-fast-forward", "fetch first"]
                    )
                )

    def test_run_git_executable_not_found(self):
        """Test _run_git raises GitError if git command execution fails (e.g. invalid executable)."""
        # Mock _run_git's subprocess call to throw FileNotFoundError
        # We can test this by calling a non-existent git path, but since _run_git hardcodes 'git',
        # we can mock subprocess.run to raise FileNotFoundError.
        from unittest import mock

        with mock.patch(
            "subprocess.run", side_effect=FileNotFoundError("git not found")
        ):
            with self.assertRaises(GitError) as ctx:
                git._run_git(self.repo_path, ["status"])
            self.assertIn("Git executable not found", str(ctx.exception))

    def test_get_remote_url(self):
        """Test get_remote_url returns the correct remote URL."""
        with tempfile.TemporaryDirectory() as remote_temp:
            remote_path = Path(remote_temp).resolve()
            # Add a mock remote
            subprocess.run(
                ["git", "remote", "add", "upstream", f"file://{remote_path}"],
                cwd=str(self.repo_path),
                check=True,
            )
            url = git.get_remote_url(self.repo_path, "upstream")
            self.assertEqual(url, f"file://{remote_path}")

    def test_clone_secure_token(self):
        """Test that clone works and stores the credential helper reference, NOT the plaintext token, on disk."""
        with tempfile.TemporaryDirectory() as remote_temp:
            remote_path = Path(remote_temp).resolve()
            # Initialize bare remote repo
            subprocess.run(
                ["git", "init", "--bare", "-b", "main"],
                cwd=str(remote_path),
                check=True,
                capture_output=True,
            )

            with tempfile.TemporaryDirectory() as clone_temp:
                clone_path = Path(clone_temp).resolve() / "target_clone"

                # Mock repository string: e.g. owner/repo (we can use the local file path as a fake owner/repo by mocking the URL inside clone or just letting git resolve it relative to path)
                # Actually, git clone supports local directory paths as URLs, but clone() in git.py appends f"https://github.com/{github_repo}.git".
                # If we want to test clone() without hitting github.com, we can mock clean_url in clone() or mock the subprocess call,
                # or just mock the remote URL.
                # Let's patch clean_url inside git.clone to point to our local bare repo!
                from unittest import mock

                original_run = subprocess.run
                with mock.patch("subprocess.run") as mock_run:
                    # Let's temporarily change clean_url inside clone to file://{remote_path}
                    # We can do this by mocking clean_url, but since clean_url is local to clone(),
                    # we can mock the clone URL by patching clean_url to point to the local file path.
                    # Wait, how about we just mock the URL in the git clone args?
                    # Let's patch subprocess.run to intercept the URL and replace it!
                    def side_effect(*args, **kwargs):
                        cmd = list(args[0])
                        for i, arg in enumerate(cmd):
                            if "github.com/" in arg:
                                cmd[i] = str(remote_path)
                        args = (cmd,) + args[1:]
                        return original_run(*args, **kwargs)

                    mock_run.side_effect = side_effect

                    git.clone(
                        clone_path,
                        "fake-owner/fake-repo",
                        token="my-super-secret-pat-token",
                    )

                # Verify repository cloned successfully (contains .git)
                self.assertTrue(git.is_git_repository(clone_path))

                # Read .git/config and verify the token is NOT present in plaintext
                config_path = clone_path / ".git" / "config"
                config_content = config_path.read_text(encoding="utf-8")

                self.assertNotIn("my-super-secret-pat-token", config_content)
                self.assertIn("credential", config_content)
                self.assertIn("x-access-token", config_content)
                self.assertIn("$GH_PAT", config_content)

                # Check that GH_PAT was injected into os.environ
                self.assertEqual(os.environ.get("GH_PAT"), "my-super-secret-pat-token")

    def test_get_commit_time(self):
        """Test get_commit_time returns a valid ISO 8601 formatted datetime string."""
        commit_time = git.get_commit_time(self.repo_path, "HEAD")
        self.assertIsNotNone(commit_time)
        # Should be in ISO 8601 format (e.g. contains T and offset or Z)
        self.assertIn("T", commit_time)
        import datetime

        dt = datetime.datetime.fromisoformat(commit_time)
        self.assertIsNotNone(dt)
