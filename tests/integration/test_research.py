"""Integration tests for the research worker loop.

Uses real git operations against a local bare repo.
Mocks: ``_gh()``, ``runner.run``, and any post-agent GitHub actions.

The tests verify loop mechanics:
- Worktree is created (real git) and cleaned up after the agent exits
- Issues are claimed before dispatch and the claim is preserved on success
- Stale in-progress issues are unclaimed before the main loop starts
- Completion gate fires when all open issues are non-blocking
- Runner errors cause the issue to be unclaimed (not left orphaned)
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from brimstone.cli import _run_research_worker
from tests.integration.conftest import (
    fake_run_result,
    make_checkpoint,
    make_config,
    make_issue,
)

# ---------------------------------------------------------------------------
# Shared patches applied to every test in this module
# ---------------------------------------------------------------------------

_BASE_PATCHES = {
    "milestone_exists": "brimstone.cli._milestone_exists",
    "default_branch": "brimstone.cli._get_default_branch_for_repo",
    "claim": "brimstone.cli._claim_issue",
    "unclaim": "brimstone.cli._unclaim_issue",
    "find_pr": "brimstone.cli._find_pr_for_issue",
    "merged_pr": "brimstone.cli._pr_merged_for_issue",
    "file_design": "brimstone.cli._file_design_issue_if_missing",
    "runner": "brimstone.cli.runner.run",
    "build_env": "brimstone.cli.build_subprocess_env",
    "skill_tmp": "brimstone.cli.write_skill_tmp",
    "classify": "brimstone.cli._classify_blocking_issues",
    "dep_checks": "brimstone.cli._startup_dep_checks",
}


def _skill_mock(tmp_path: Path) -> MagicMock:
    """Returns a mock for write_skill_tmp that creates a real temp file."""
    skill_file = tmp_path / "skill.md"
    skill_file.write_text("")
    m = MagicMock(return_value=skill_file)
    return m


class TestResearchWorkerHappyPath:
    def test_worktree_created_and_removed(self, git_repo: Path, tmp_path: Path) -> None:
        """A worktree is created for the dispatched issue and cleaned up after the agent."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()
        issue = make_issue(1, "Research language choice")

        call_count = [0]

        def open_issues(repo: str, milestone: str, label: str) -> list:
            call_count[0] += 1
            return [issue] if call_count[0] <= 2 else []

        with (
            patch(_BASE_PATCHES["milestone_exists"], return_value=True),
            patch(_BASE_PATCHES["default_branch"], return_value="mainline"),
            patch(_BASE_PATCHES["claim"]),
            patch(_BASE_PATCHES["unclaim"]),
            patch(_BASE_PATCHES["find_pr"], return_value=None),
            patch(_BASE_PATCHES["file_design"]),
            patch(_BASE_PATCHES["runner"], return_value=fake_run_result()),
            patch(_BASE_PATCHES["build_env"], return_value={}),
            patch(_BASE_PATCHES["skill_tmp"], side_effect=_skill_mock(tmp_path)),
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues),
            patch(_BASE_PATCHES["classify"], return_value=([issue], [])),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        # Worktrees directory should have no leftover entries
        worktrees_dir = git_repo / ".claude" / "worktrees"
        if worktrees_dir.exists():
            assert list(worktrees_dir.iterdir()) == [], "Stale worktrees found after loop"

    def test_git_worktree_list_clean_after_run(self, git_repo: Path, tmp_path: Path) -> None:
        """git worktree list must show only the main worktree after the loop."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()
        issue = make_issue(1, "Research language choice")

        call_count = [0]

        def open_issues(repo: str, milestone: str, label: str) -> list:
            call_count[0] += 1
            return [issue] if call_count[0] <= 2 else []

        with (
            patch(_BASE_PATCHES["milestone_exists"], return_value=True),
            patch(_BASE_PATCHES["default_branch"], return_value="mainline"),
            patch(_BASE_PATCHES["claim"]),
            patch(_BASE_PATCHES["unclaim"]),
            patch(_BASE_PATCHES["find_pr"], return_value=None),
            patch(_BASE_PATCHES["file_design"]),
            patch(_BASE_PATCHES["runner"], return_value=fake_run_result()),
            patch(_BASE_PATCHES["build_env"], return_value={}),
            patch(_BASE_PATCHES["skill_tmp"], side_effect=_skill_mock(tmp_path)),
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues),
            patch(_BASE_PATCHES["classify"], return_value=([issue], [])),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        wt_list = subprocess.run(
            ["git", "worktree", "list"],
            cwd=git_repo,
            capture_output=True,
            text=True,
        ).stdout
        assert "1-research-language-choice" not in wt_list

    def test_issue_claimed_before_dispatch(self, git_repo: Path, tmp_path: Path) -> None:
        """_claim_issue must be called before runner.run."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()
        issue = make_issue(1, "Research language choice")

        call_order: list[str] = []

        def fake_claim(repo: str, issue_number: int, **kwargs: object) -> None:
            call_order.append("claim")

        def fake_run(**kwargs: object) -> MagicMock:
            call_order.append("run")
            return fake_run_result()

        call_count = [0]

        def open_issues(repo: str, milestone: str, label: str) -> list:
            call_count[0] += 1
            return [issue] if call_count[0] <= 2 else []

        with (
            patch(_BASE_PATCHES["milestone_exists"], return_value=True),
            patch(_BASE_PATCHES["default_branch"], return_value="mainline"),
            patch(_BASE_PATCHES["claim"], side_effect=fake_claim),
            patch(_BASE_PATCHES["unclaim"]),
            patch(_BASE_PATCHES["find_pr"], return_value=None),
            patch(_BASE_PATCHES["file_design"]),
            patch(_BASE_PATCHES["runner"], side_effect=fake_run),
            patch(_BASE_PATCHES["build_env"], return_value={}),
            patch(_BASE_PATCHES["skill_tmp"], side_effect=_skill_mock(tmp_path)),
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues),
            patch(_BASE_PATCHES["classify"], return_value=([issue], [])),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        assert call_order.index("claim") < call_order.index("run")

    def test_completion_gate_fires_when_no_open_issues(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """When open issues list is empty, completion gate must be called."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()

        gate_called: list[bool] = []

        def fake_gate(**kwargs: object) -> None:
            gate_called.append(True)

        with (
            patch(_BASE_PATCHES["milestone_exists"], return_value=True),
            patch(_BASE_PATCHES["default_branch"], return_value="mainline"),
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch("brimstone.cli._run_completion_gate", side_effect=fake_gate),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        assert gate_called, "Completion gate must fire when no open issues remain"

    def test_completion_gate_fires_when_all_non_blocking(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """When all open issues are non-blocking, completion gate fires immediately."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()
        issue = make_issue(1, "Non-blocking research")

        gate_called: list[bool] = []
        dispatched: list[bool] = []

        def fake_gate(**kwargs: object) -> None:
            gate_called.append(True)

        def fake_run(**kwargs: object) -> MagicMock:
            dispatched.append(True)
            return fake_run_result()

        with (
            patch(_BASE_PATCHES["milestone_exists"], return_value=True),
            patch(_BASE_PATCHES["default_branch"], return_value="mainline"),
            patch("brimstone.cli._list_open_issues_by_label", return_value=[issue]),
            # All issues non-blocking
            patch(_BASE_PATCHES["classify"], return_value=([], [issue])),
            patch("brimstone.cli._run_completion_gate", side_effect=fake_gate),
            patch(_BASE_PATCHES["runner"], side_effect=fake_run),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        assert gate_called, "Completion gate must fire when all issues are non-blocking"
        assert not dispatched, "No agent should be dispatched for non-blocking issues"


class TestResearchWorkerErrorHandling:
    def test_worktree_cleaned_up_on_runner_error(self, git_repo: Path, tmp_path: Path) -> None:
        """Worktree must be removed even when the agent errors out."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()
        issue = make_issue(1, "Research language choice")

        call_count = [0]

        def open_issues(repo: str, milestone: str, label: str) -> list:
            call_count[0] += 1
            # After the error path, unclaim means issue is open again but
            # we return [] on second call to let the loop exit via gate.
            return [issue] if call_count[0] <= 2 else []

        with (
            patch(_BASE_PATCHES["milestone_exists"], return_value=True),
            patch(_BASE_PATCHES["default_branch"], return_value="mainline"),
            patch(_BASE_PATCHES["claim"]),
            patch(_BASE_PATCHES["unclaim"]),
            patch(_BASE_PATCHES["find_pr"], return_value=None),
            patch(_BASE_PATCHES["file_design"]),
            patch(_BASE_PATCHES["runner"], return_value=fake_run_result(is_error=True)),
            patch(_BASE_PATCHES["build_env"], return_value={}),
            patch(_BASE_PATCHES["skill_tmp"], side_effect=_skill_mock(tmp_path)),
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues),
            patch(_BASE_PATCHES["classify"], return_value=([issue], [])),
            patch("brimstone.cli._gh"),  # suppress remote branch delete call
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        worktrees_dir = git_repo / ".claude" / "worktrees"
        if worktrees_dir.exists():
            assert list(worktrees_dir.iterdir()) == [], "Worktree must be cleaned up on error"

    def test_issue_unclaimed_on_runner_error(self, git_repo: Path, tmp_path: Path) -> None:
        """On runner error the issue must be unclaimed so it can be retried."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()
        issue = make_issue(1, "Research language choice")

        unclaimed: list[int] = []

        def fake_unclaim(repo: str, issue_number: int, **kwargs: object) -> None:
            unclaimed.append(issue_number)

        call_count = [0]

        def open_issues(repo: str, milestone: str, label: str) -> list:
            call_count[0] += 1
            return [issue] if call_count[0] <= 2 else []

        with (
            patch(_BASE_PATCHES["milestone_exists"], return_value=True),
            patch(_BASE_PATCHES["default_branch"], return_value="mainline"),
            patch(_BASE_PATCHES["claim"]),
            patch(_BASE_PATCHES["unclaim"], side_effect=fake_unclaim),
            patch(_BASE_PATCHES["find_pr"], return_value=None),
            patch(_BASE_PATCHES["file_design"]),
            patch(_BASE_PATCHES["runner"], return_value=fake_run_result(is_error=True)),
            patch(_BASE_PATCHES["build_env"], return_value={}),
            patch(_BASE_PATCHES["skill_tmp"], side_effect=_skill_mock(tmp_path)),
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues),
            patch(_BASE_PATCHES["classify"], return_value=([issue], [])),
            patch("brimstone.cli._gh"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        assert 1 in unclaimed, "Issue #1 must be unclaimed after runner error"

    def test_stale_in_progress_issue_without_pr_unclaimed_on_startup(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """Stale in-progress issue (no open PR) must be unclaimed at startup."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()

        from brimstone.beads import WorkBead

        stale_bead = WorkBead(
            v=1,
            issue_number=99,
            title="Stale from crashed session",
            milestone="v0.1.0",
            stage="research",
            module="cli",
            priority="P2",
            state="claimed",
            branch="99-stale-branch",
        )
        store = MagicMock()
        store.list_work_beads.return_value = [stale_bead]
        store.read_pr_bead.return_value = None
        store.write_pr_bead.return_value = None
        store.read_work_bead.return_value = stale_bead
        store.write_work_bead.return_value = None
        store.flush.return_value = None

        unclaimed: list[int] = []

        def fake_unclaim(repo: str, issue_number: int, **kwargs: object) -> None:
            unclaimed.append(issue_number)

        with (
            patch(_BASE_PATCHES["milestone_exists"], return_value=True),
            patch(_BASE_PATCHES["default_branch"], return_value="mainline"),
            patch(_BASE_PATCHES["unclaim"], side_effect=fake_unclaim),
            # _find_pr_for_issue returns None → no open PR
            patch(_BASE_PATCHES["find_pr"], return_value=None),
            # _pr_merged_for_issue returns False → no merged PR → unclaim
            patch(_BASE_PATCHES["merged_pr"], return_value=False),
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch("brimstone.cli._run_completion_gate"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
                store=store,
            )

        assert 99 in unclaimed, "Stale in-progress issue with no PR must be unclaimed"


class TestResearchWorkerParallelDispatch:
    def test_research_parallel_dispatch(self, git_repo: Path, tmp_path: Path) -> None:
        """Three unblocked issues with pool_size≥3 must all have runner.run called
        before any _unclaim_issue fires — confirming concurrent dispatch.

        A threading.Barrier(3) is used so all three worker threads must reach
        their runner.run call before any of them returns, which guarantees the
        sequence ["run", "run", "run", ...unclaims...] in call_order.
        """
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()
        issue_a = make_issue(1, "Research A")
        issue_b = make_issue(2, "Research B")
        issue_c = make_issue(3, "Research C")

        call_order: list[str] = []
        order_lock = threading.Lock()
        # All three runner.run calls must start before any of them returns
        start_barrier = threading.Barrier(3, timeout=10.0)

        def controlled_run(**kwargs: object) -> MagicMock:
            with order_lock:
                call_order.append("run")
            start_barrier.wait()
            # Return an error so the completion handler calls _unclaim_issue
            return fake_run_result(is_error=True)

        def fake_unclaim(repo: str, issue_number: int, **kwargs: object) -> None:
            with order_lock:
                call_order.append("unclaim")

        call_count = [0]

        def open_issues(repo: str, milestone: str, label: str) -> list:
            call_count[0] += 1
            return [issue_a, issue_b, issue_c] if call_count[0] <= 2 else []

        with (
            patch(_BASE_PATCHES["milestone_exists"], return_value=True),
            patch(_BASE_PATCHES["default_branch"], return_value="mainline"),
            patch(_BASE_PATCHES["claim"]),
            patch(_BASE_PATCHES["unclaim"], side_effect=fake_unclaim),
            patch(_BASE_PATCHES["find_pr"], return_value=None),
            patch(_BASE_PATCHES["file_design"]),
            patch(_BASE_PATCHES["runner"], side_effect=controlled_run),
            patch(_BASE_PATCHES["build_env"], return_value={}),
            patch(_BASE_PATCHES["skill_tmp"], side_effect=_skill_mock(tmp_path)),
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues),
            patch(_BASE_PATCHES["classify"], return_value=([issue_a, issue_b, issue_c], [])),
            patch("brimstone.cli._gh"),  # suppress branch-delete and pipeline gh calls
            patch("brimstone.cli._run_completion_gate"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        run_calls = [x for x in call_order if x == "run"]
        assert len(run_calls) == 3, f"Expected 3 runner.run calls, got {len(run_calls)}"

        # All three "run" entries must appear before the first "unclaim"
        first_unclaim_idx = next(
            (i for i, x in enumerate(call_order) if x == "unclaim"), len(call_order)
        )
        run_indices = [i for i, x in enumerate(call_order) if x == "run"]
        assert all(i < first_unclaim_idx for i in run_indices), (
            f"Some runner.run calls happened after an unclaim: call_order={call_order}"
        )
