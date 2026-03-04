"""Integration tests for the implementation worker loop.

Uses real git operations against a local bare repo.
Mocks: ``_gh()``, ``runner.run``, and GitHub-dependent checks.

The tests verify:
- Worktree is created with the correct default branch (not hardcoded "main")
- Module isolation is respected (one agent per module)
- Issues are claimed before dispatch and unclaimed on failure
- Completion gate fires when no open issues remain
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from brimstone.cli import _run_impl_worker
from tests.integration.conftest import (
    fake_run_result,
    make_checkpoint,
    make_config,
    make_issue,
)


def _impl_issue(n: int, title: str, module: str = "cli") -> dict:
    # _extract_module reads feat:* labels, not module/* labels
    issue = make_issue(n, title, labels=["stage/impl", f"feat:{module}", "P2"])
    return issue


class TestImplWorkerDefaultBranch:
    def test_worktree_uses_default_branch_from_gh(self, git_repo: Path, tmp_path: Path) -> None:
        """Regression: impl worker must query the repo's default branch and pass it
        to _create_worktree — NOT hardcode 'main'.

        If the repo uses 'mainline', hardcoding 'main' causes the worktree to fail
        because origin/main does not exist, causing the issue to be unclaimed and
        the loop to spin forever.
        """
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="impl")

        issue = _impl_issue(1, "Implement config parsing")
        worktrees_created: list[str] = []

        call_count = [0]

        def open_issues(repo: str, milestone: str, label: str) -> list:
            call_count[0] += 1
            return [issue] if call_count[0] <= 2 else []

        def fake_dispatch(
            issue: dict,
            branch: str,
            worktree_path: str,
            module: str,
            **kwargs: object,
        ) -> MagicMock:
            worktrees_created.append(worktree_path)
            return fake_run_result()

        with (
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues),
            patch(
                "brimstone.cli._filter_unblocked",
                side_effect=lambda issues, nums, store=None: issues,
            ),
            patch("brimstone.cli._sort_issues", side_effect=lambda issues: issues),
            patch("brimstone.cli._extract_module", return_value="cli"),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue"),
            patch("brimstone.cli._dispatch_impl_agent", side_effect=fake_dispatch),
            patch("brimstone.cli._count_all_issues_by_label", return_value=1),
            patch("brimstone.cli._gh"),  # suppress completion gate gh call
        ):
            _run_impl_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        assert len(worktrees_created) == 1, "Exactly one impl issue should have been dispatched"

        # Verify the worktree path exists/existed (may be cleaned by _dispatch_impl_agent mock)
        # The important thing: if default_branch was "main" instead of "mainline",
        # _create_worktree would have returned None and worktrees_created would be empty.

    def test_worktree_created_with_mainline_not_main(self, git_repo: Path, tmp_path: Path) -> None:
        """Direct test: _create_worktree fails with 'main', succeeds with 'mainline'."""
        from brimstone.cli import _create_worktree, _remove_worktree

        # This is the repo's actual default branch
        wt = _create_worktree("1-test", str(git_repo), "mainline")
        assert wt is not None, "Should succeed with correct branch 'mainline'"
        _remove_worktree(wt, str(git_repo))

        # 'main' does not exist in the test repo — must return None
        wt_bad = _create_worktree("1-test-bad", str(git_repo), "main")
        assert wt_bad is None, "Should fail with wrong default branch 'main'"


class TestImplWorkerLoopMechanics:
    def test_completion_gate_fires_when_no_open_issues(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="impl")

        with (
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch("brimstone.cli._count_all_issues_by_label", return_value=1),
            patch("brimstone.cli._gh"),  # suppress pipeline issue creation
        ):
            # Should exit without error (not loop forever)
            _run_impl_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

    def test_unclaimed_on_worktree_failure(self, git_repo: Path, tmp_path: Path) -> None:
        """If worktree creation fails, issue must be unclaimed immediately."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="impl")

        issue = _impl_issue(1, "Implement config parsing")
        unclaimed: list[int] = []

        call_count = [0]

        def open_issues(repo: str, milestone: str, label: str) -> list:
            call_count[0] += 1
            return [issue] if call_count[0] <= 2 else []

        def fake_unclaim(repo: str, issue_number: int, **kwargs: object) -> None:
            unclaimed.append(issue_number)

        with (
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues),
            patch(
                "brimstone.cli._filter_unblocked",
                side_effect=lambda issues, nums, store=None: issues,
            ),
            patch("brimstone.cli._sort_issues", side_effect=lambda issues: issues),
            patch("brimstone.cli._extract_module", return_value="cli"),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue", side_effect=fake_unclaim),
            # Force worktree creation to fail
            patch("brimstone.cli._create_worktree", return_value=None),
            patch("brimstone.cli._count_all_issues_by_label", return_value=1),
            patch("brimstone.cli._gh"),
        ):
            _run_impl_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        assert 1 in unclaimed, "Issue #1 must be unclaimed when worktree creation fails"

    def test_module_isolation_enforced(self, git_repo: Path, tmp_path: Path) -> None:
        """Two issues with the same feat:* module must not be dispatched simultaneously.

        issue_a and issue_b share feat:cli; issue_c is feat:runner.
        In a single dispatch batch, only issue_a and issue_c should be selected.
        issue_b must wait until a subsequent batch.
        """
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="impl")

        issue_a = _impl_issue(1, "Impl A", module="cli")
        issue_b = _impl_issue(2, "Impl B", module="cli")  # same module as issue_a
        issue_c = _impl_issue(3, "Impl C", module="runner")

        all_dispatched: list[int] = []
        call_count = [0]

        def open_issues(repo: str, milestone: str, label: str) -> list:
            call_count[0] += 1
            if call_count[0] <= 2:
                return [issue_a, issue_b, issue_c]
            return []

        def fake_dispatch(
            issue: dict,
            branch: str,
            worktree_path: str,
            module: str,
            **kwargs: object,
        ) -> MagicMock:
            all_dispatched.append(issue["number"])
            return fake_run_result()

        with (
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues),
            patch(
                "brimstone.cli._filter_unblocked",
                side_effect=lambda issues, nums, store=None: issues,
            ),
            patch("brimstone.cli._sort_issues", side_effect=lambda issues: issues),
            # Do NOT mock _extract_module — let the real one read feat:* labels
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue"),
            patch("brimstone.cli._dispatch_impl_agent", side_effect=fake_dispatch),
            patch("brimstone.cli._count_all_issues_by_label", return_value=3),
            patch("brimstone.cli._gh"),
        ):
            _run_impl_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        # With module isolation, issues #1 and #3 should be dispatched (different modules)
        # Issue #2 shares feat:cli with #1 so it must NOT be dispatched in the same batch.
        # The second open_issues call returns [], so only one batch runs.
        assert 1 in all_dispatched, "issue_a (feat:cli) must be dispatched"
        assert 3 in all_dispatched, "issue_c (feat:runner) must be dispatched"
        assert 2 not in all_dispatched, (
            "issue_b (feat:cli) must NOT be dispatched in same batch as issue_a"
        )
