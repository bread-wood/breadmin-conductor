"""Integration tests for git worktree operations.

These tests use no mocks — all ``git`` subprocess calls are real.
They are the regression safety net for worktree lifecycle bugs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from brimstone.cli import _create_worktree, _remove_worktree


class TestCreateWorktree:
    def test_happy_path_creates_worktree_directory(self, git_repo: Path) -> None:
        path = _create_worktree("1-feature", str(git_repo), "mainline")
        assert path is not None
        assert Path(path).is_dir()
        _remove_worktree(path, str(git_repo))

    def test_returns_correct_path_under_claude_worktrees(self, git_repo: Path) -> None:
        expected = str(git_repo / ".claude" / "worktrees" / "1-feature")
        path = _create_worktree("1-feature", str(git_repo), "mainline")
        assert path == expected
        _remove_worktree(path, str(git_repo))

    def test_checked_out_branch_matches_requested_name(self, git_repo: Path) -> None:
        path = _create_worktree("1-feature", str(git_repo), "mainline")
        assert path is not None
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "1-feature"
        _remove_worktree(path, str(git_repo))

    def test_stale_local_branch_cleaned_up_before_creating(self, git_repo: Path) -> None:
        """Regression: previous infinite loop caused by stale local branch.

        If a prior run fails after creating the branch but before (or during)
        worktree creation, the branch is left behind.  The next attempt must
        delete it and succeed.
        """
        subprocess.run(
            ["git", "-C", str(git_repo), "branch", "1-stale", "origin/mainline"],
            check=True,
            capture_output=True,
        )
        path = _create_worktree("1-stale", str(git_repo), "mainline")
        assert path is not None, "Must succeed even when branch already exists locally"
        _remove_worktree(path, str(git_repo))

    def test_stale_worktree_dir_cleaned_up_before_creating(self, git_repo: Path) -> None:
        """Stale directory left by a crashed ``git worktree remove`` must not block."""
        stale = git_repo / ".claude" / "worktrees" / "2-stale"
        stale.mkdir(parents=True)
        path = _create_worktree("2-stale", str(git_repo), "mainline")
        assert path is not None
        _remove_worktree(path, str(git_repo))

    def test_returns_none_for_nonexistent_default_branch(self, git_repo: Path) -> None:
        path = _create_worktree("1-feature", str(git_repo), "branch-does-not-exist")
        assert path is None

    def test_create_remove_create_cycle(self, git_repo: Path) -> None:
        """Verify no stale state is left after a clean remove."""
        wt1 = _create_worktree("3-cycle", str(git_repo), "mainline")
        assert wt1 is not None
        _remove_worktree(wt1, str(git_repo))

        wt2 = _create_worktree("3-cycle", str(git_repo), "mainline")
        assert wt2 is not None, "Second create must succeed after clean remove"
        _remove_worktree(wt2, str(git_repo))

    def test_remove_cleans_up_directory(self, git_repo: Path) -> None:
        path = _create_worktree("4-cleanup", str(git_repo), "mainline")
        assert path is not None
        _remove_worktree(path, str(git_repo))
        assert not Path(path).exists()

    def test_remove_deregisters_worktree(self, git_repo: Path) -> None:
        path = _create_worktree("5-deregister", str(git_repo), "mainline")
        assert path is not None
        _remove_worktree(path, str(git_repo))
        result = subprocess.run(
            ["git", "worktree", "list"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        )
        assert "5-deregister" not in result.stdout

    def test_two_concurrent_worktrees_coexist(self, git_repo: Path) -> None:
        wt_a = _create_worktree("6-a", str(git_repo), "mainline")
        wt_b = _create_worktree("6-b", str(git_repo), "mainline")
        assert wt_a is not None
        assert wt_b is not None
        assert Path(wt_a).is_dir()
        assert Path(wt_b).is_dir()
        _remove_worktree(wt_a, str(git_repo))
        _remove_worktree(wt_b, str(git_repo))
