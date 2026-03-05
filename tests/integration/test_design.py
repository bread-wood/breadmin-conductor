"""Integration tests for the design worker.

Uses real git operations against a local bare repo.
Mocks: ``_gh()``, ``runner.run``, and GitHub-dependent checks.

The tests verify:
- HLD agent is dispatched and its worktree created/cleaned up
- Gate 1 (no open research) is enforced
- Gate 2 (HLD on default branch) is enforced before LLD dispatch
- LLD agents are dispatched with their own worktrees
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from brimstone.cli import _run_design_worker
from tests.integration.conftest import (
    fake_run_result,
    make_checkpoint,
    make_config,
    make_issue,
)


def _hld_issue() -> dict:
    return make_issue(10, "Design: HLD for v0.1.0", labels=["stage/design"])


def _lld_issue(n: int, module: str) -> dict:
    return make_issue(n, f"Design: LLD for {module}", labels=["stage/design"])


class TestDesignWorkerGates:
    def test_exits_if_research_still_open(self, git_repo: Path, tmp_path: Path) -> None:
        """Gate 1: research must be fully closed before design can begin."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="design")

        with (
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
            patch(
                "brimstone.cli._list_all_open_issues_by_label",
                return_value=[make_issue(1, "Blocking research")],
            ),
        ):
            with pytest.raises(SystemExit):
                _run_design_worker(
                    repo="owner/repo",
                    milestone="v0.1.0",
                    config=config,
                    checkpoint=checkpoint,
                )

    def test_exits_if_hld_agent_errors(self, git_repo: Path, tmp_path: Path) -> None:
        """If the HLD agent errors, the design worker must exit non-zero."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="design")
        hld = _hld_issue()

        with (
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
            patch("brimstone.cli._list_all_open_issues_by_label", return_value=[]),
            patch("brimstone.cli._list_open_issues_by_label", return_value=[hld]),
            patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue"),
            patch(
                "brimstone.cli._dispatch_design_agent",
                return_value=(None, "", "", fake_run_result(is_error=True)),
            ),
        ):
            with pytest.raises(SystemExit):
                _run_design_worker(
                    repo="owner/repo",
                    milestone="v0.1.0",
                    config=config,
                    checkpoint=checkpoint,
                )


class TestDesignWorkerHLDPhase:
    def test_hld_worktree_created_and_cleaned_up(self, git_repo: Path, tmp_path: Path) -> None:
        """HLD agent gets a real worktree that is removed after dispatch."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="design")
        hld = _hld_issue()

        worktrees_seen: list[str] = []

        def spy_dispatch(issue: dict, branch: str, worktree_path: str, **kwargs: object) -> tuple:
            worktrees_seen.append(worktree_path)
            assert Path(worktree_path).is_dir(), "Worktree must exist when agent is dispatched"
            return issue, branch, worktree_path, fake_run_result()

        # doc_exists calls:
        # 1. Phase 1 gate: HLD on branch? → False (run HLD phase)
        # 2. Gate 2:        HLD on branch? → True (proceed to LLD)
        # (LLD recovery checks will not be called since lld_issues is empty)
        with (
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
            patch("brimstone.cli._list_all_open_issues_by_label", return_value=[]),
            # Phase 1 design → [hld]; Phase 2 LLD → []
            patch(
                "brimstone.cli._list_open_issues_by_label",
                side_effect=[[hld], []],
            ),
            patch(
                "brimstone.cli._doc_exists_on_default_branch",
                side_effect=[False, True],
            ),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue"),
            patch("brimstone.cli._dispatch_design_agent", side_effect=spy_dispatch),
            patch("brimstone.cli._find_pr_for_branch", return_value=42),
            patch("brimstone.cli._monitor_pr", return_value=True),
        ):
            # No LLD issues → exits with SystemExit(1); that's fine for this test
            with pytest.raises(SystemExit):
                _run_design_worker(
                    repo="owner/repo",
                    milestone="v0.1.0",
                    config=config,
                    checkpoint=checkpoint,
                )

        assert len(worktrees_seen) >= 1, "HLD dispatch must have been called with a worktree"
        for wt in worktrees_seen:
            assert not Path(wt).exists(), f"Worktree {wt} not cleaned up after HLD dispatch"

    def test_hld_skipped_when_already_on_branch(self, git_repo: Path, tmp_path: Path) -> None:
        """If HLD already merged, Phase 1 is skipped."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="design")

        dispatch_called: list[str] = []

        def spy_dispatch(
            issue: dict, branch: str, worktree_path: str, skill_name: str, **kwargs: object
        ) -> tuple:
            dispatch_called.append(skill_name)
            return issue, branch, worktree_path, fake_run_result()

        # doc_exists: HLD already on branch (Phase 1 skip + Gate 2 pass)
        with (
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
            patch("brimstone.cli._list_all_open_issues_by_label", return_value=[]),
            # Gate 2 passes (HLD on branch); Phase 2 LLD → []
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch("brimstone.cli._doc_exists_on_default_branch", return_value=True),
            patch("brimstone.cli._dispatch_design_agent", side_effect=spy_dispatch),
        ):
            with pytest.raises(SystemExit):
                _run_design_worker(
                    repo="owner/repo",
                    milestone="v0.1.0",
                    config=config,
                    checkpoint=checkpoint,
                )

        hld_dispatches = [s for s in dispatch_called if "hld" in s]
        assert not hld_dispatches, "HLD agent must not be dispatched when doc already merged"


class TestDesignWorkerLLDPhase:
    def test_lld_worktrees_created_per_module(self, git_repo: Path, tmp_path: Path) -> None:
        """One worktree is created per LLD issue during Phase 2."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="design")
        llds = [_lld_issue(20, "cli"), _lld_issue(21, "runner")]

        worktrees_seen: list[str] = []

        def spy_dispatch(issue: dict, branch: str, worktree_path: str, **kwargs: object) -> tuple:
            worktrees_seen.append(worktree_path)
            assert Path(worktree_path).is_dir()
            return issue, branch, worktree_path, fake_run_result()

        # doc_exists calls:
        # 1. Phase 1 gate (HLD check) → True (skip HLD)
        # 2. Gate 2 check             → True (proceed to LLD)
        # 3. LLD recovery for "cli"   → False (not yet merged)
        # 4. LLD recovery for "runner"→ False (not yet merged)
        with (
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
            patch("brimstone.cli._list_all_open_issues_by_label", return_value=[]),
            # Phase 2 LLD listing → llds
            patch("brimstone.cli._list_open_issues_by_label", return_value=llds),
            patch(
                "brimstone.cli._doc_exists_on_default_branch",
                side_effect=[True, True, False, False],
            ),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue"),
            patch("brimstone.cli._dispatch_design_agent", side_effect=spy_dispatch),
            patch("brimstone.cli._find_pr_for_branch", return_value=50),
            patch("brimstone.cli._monitor_pr", return_value=True),
            patch("brimstone.cli._gh"),
        ):
            _run_design_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        assert len(worktrees_seen) == 2, f"Expected 2 LLD worktrees, got {len(worktrees_seen)}"
        for wt in worktrees_seen:
            assert not Path(wt).exists(), f"Worktree {wt} not cleaned up after LLD dispatch"
