"""Integration tests for the three core flows not covered elsewhere.

1. _monitor_pr → _process_merge_queue drain (happy path)
   Confirms that monitor_pr enqueues an entry and process_merge_queue
   calls squash-merge and transitions WorkBead → closed / PRBead → merged.

2. _run_impl_worker bead lifecycle
   Confirms WorkBead is written at claim time and that _claim_issue and
   _dispatch_impl_agent are called in the right sequence with a live
   BeadStore.

3. _watchdog_scan exhaustion path
   Confirms that a zombie PRBead with fix_attempts >= max causes the issue
   to be abandoned (WorkBead.state = "abandoned") and _exhaust_issue is called.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from brimstone.beads import (
    BeadStore,
    MergeQueue,
    MergeQueueEntry,
    PRBead,
    WorkBead,
)
from brimstone.cli import (
    _monitor_pr,
    _process_merge_queue,
    _run_impl_worker,
    _watchdog_scan,
)
from tests.integration.conftest import (
    fake_run_result,
    make_checkpoint,
    make_config,
    make_issue,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gh_side_effect(args: list[str], **kwargs: object) -> MagicMock:
    """Return a sensible MagicMock _gh response based on the subcommand.

    Different _gh callers parse stdout differently:
    - ``issue view --json assignees`` → ``{"assignees": []}``
    - ``issue list --json number ...`` → ``[]``
    - everything else → empty string (returncode=0)
    """
    result = MagicMock()
    result.returncode = 0
    if "assignees" in args:
        result.stdout = '{"assignees": []}'
    elif "--json" in args and "number" in args:
        result.stdout = "[]"
    else:
        result.stdout = ""
    return result


def _impl_issue(n: int, title: str, module: str = "cli") -> dict:
    return make_issue(n, title, labels=["stage/impl", f"feat:{module}", "P2"])


def _make_pr_bead(
    pr_number: int = 55,
    issue_number: int = 7,
    fix_attempts: int = 0,
    state: str = "open",
) -> PRBead:
    return PRBead(
        v=1,
        pr_number=pr_number,
        issue_number=issue_number,
        branch=f"{issue_number}-feature",
        state=state,
        ci_state=None,
        conflict_state=None,
        fix_attempts=fix_attempts,
        feedback=[],
        created_at=datetime.now(UTC).isoformat(),
        merged_at=None,
    )


def _make_work_bead(
    issue_number: int = 7,
    state: str = "claimed",
    claimed_at: str | None = None,
) -> WorkBead:
    if claimed_at is None:
        claimed_at = datetime.now(UTC).isoformat()
    return WorkBead(
        v=1,
        issue_number=issue_number,
        title="Test issue",
        milestone="v0.1.0",
        stage="impl",
        module="cli",
        priority="P2",
        branch=f"{issue_number}-feature",
        state=state,
        claimed_at=claimed_at,
        closed_at=None,
        pr_id=None,
        retry_count=0,
    )


# ---------------------------------------------------------------------------
# 1. _monitor_pr → _process_merge_queue drain
# ---------------------------------------------------------------------------


class TestMonitorPrToMergeQueueDrain:
    """_monitor_pr enqueues; _process_merge_queue drains and updates beads."""

    def test_happy_path_bead_states_after_drain(self, git_repo: Path, tmp_path: Path) -> None:
        """Full happy path: CI passes → enqueued → squash-merged → beads closed.

        _monitor_pr writes a PRBead and enqueues a MergeQueueEntry.
        _process_merge_queue pops the entry, rebases (mocked), squash-merges,
        and writes PRBead(state="merged") + WorkBead(state="closed").
        """
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="impl")
        store = BeadStore(beads_dir=tmp_path / "beads" / "owner" / "repo")

        # Seed a WorkBead so merge queue drain can update it
        work_bead = _make_work_bead(issue_number=7)
        store.write_work_bead(work_bead)

        # --- Phase 1: _monitor_pr ---
        with (
            patch("brimstone.cli._is_conflict_failure", return_value=False),
            patch("brimstone.cli._get_pr_checks_status", return_value="pass"),
            patch("brimstone.cli._get_review_status", return_value="approved"),
            patch("brimstone.cli._gh"),
            patch("brimstone.cli.time.sleep"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            merged = _monitor_pr(
                pr_number=55,
                branch="7-feature",
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                issue_number=7,
                store=store,
                poll_interval=0,
            )

        assert merged is True, "_monitor_pr should return True on CI pass"

        # PRBead written in initial state
        pr_bead = store.read_pr_bead(55)
        assert pr_bead is not None, "PRBead must be written by _monitor_pr"

        # MergeQueue has one entry
        queue = store.read_merge_queue()
        assert len(queue.queue) == 1, "One entry must be enqueued"
        entry = queue.queue[0]
        assert entry.pr_number == 55
        assert entry.issue_number == 7

        # --- Phase 2: _process_merge_queue ---
        merge_result = MagicMock()
        merge_result.returncode = 0
        merge_result.stderr = None

        with (
            patch("brimstone.cli._checkout_existing_branch_worktree", return_value=None),
            patch("brimstone.cli._gh", return_value=merge_result),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            _process_merge_queue(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                default_branch="mainline",
                repo_root=str(git_repo),
            )

        # Queue should be empty after drain
        queue_after = store.read_merge_queue()
        assert queue_after.queue == [], "Queue must be empty after successful drain"

        # PRBead must be merged
        pr_bead_after = store.read_pr_bead(55)
        assert pr_bead_after is not None
        assert pr_bead_after.state == "merged", (
            f"PRBead.state must be 'merged', got {pr_bead_after.state!r}"
        )
        assert pr_bead_after.merged_at is not None

        # WorkBead must be closed
        work_bead_after = store.read_work_bead(7)
        assert work_bead_after is not None
        assert work_bead_after.state == "closed", (
            f"WorkBead.state must be 'closed', got {work_bead_after.state!r}"
        )

    def test_merge_failure_leaves_pr_bead_unchanged(self, git_repo: Path, tmp_path: Path) -> None:
        """If squash-merge fails, PRBead state is not updated and queue is cleared."""
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="impl")
        store = BeadStore(beads_dir=tmp_path / "beads" / "owner" / "repo")

        # Pre-seed the queue directly
        entry = MergeQueueEntry(
            pr_number=55,
            issue_number=7,
            branch="7-feature",
            enqueued_at=datetime.now(UTC).isoformat(),
        )
        mq = MergeQueue(v=1, queue=[entry], updated_at=datetime.now(UTC).isoformat())
        store.write_merge_queue(mq)

        # Seed a PRBead in reviewing state
        pr_bead = _make_pr_bead(state="reviewing")
        store.write_pr_bead(pr_bead)

        merge_result = MagicMock()
        merge_result.returncode = 1
        merge_result.stderr = "merge failed"

        with (
            patch("brimstone.cli._checkout_existing_branch_worktree", return_value=None),
            patch("brimstone.cli._gh", return_value=merge_result),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            _process_merge_queue(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                default_branch="mainline",
                repo_root=str(git_repo),
            )

        # PRBead must NOT be "merged" after a failed merge
        pr_bead_after = store.read_pr_bead(55)
        assert pr_bead_after is not None
        assert pr_bead_after.state != "merged", (
            "PRBead must not be updated to 'merged' when squash-merge fails"
        )


# ---------------------------------------------------------------------------
# 2. _run_impl_worker bead lifecycle
# ---------------------------------------------------------------------------


class TestImplWorkerBeadLifecycle:
    """WorkBead is written at claim time and transitions through the impl loop."""

    def test_work_bead_written_on_claim(self, git_repo: Path, tmp_path: Path) -> None:
        """_claim_issue must write a WorkBead(state='claimed') to the store.

        We use a real BeadStore and spy on write_work_bead to confirm it's
        called with the right state — not just that _claim_issue was called.
        """
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="impl")
        store = BeadStore(beads_dir=tmp_path / "beads" / "owner" / "repo")

        issue = _impl_issue(42, "Add config parsing")
        call_count = [0]

        def open_issues(repo: str, milestone: str, label: str) -> list:
            call_count[0] += 1
            return [issue] if call_count[0] <= 2 else []

        written_work_beads: list[WorkBead] = []
        real_write = store.write_work_bead

        def spy_write_work_bead(bead: WorkBead) -> None:
            written_work_beads.append(bead)
            real_write(bead)

        store.write_work_bead = spy_write_work_bead  # type: ignore[method-assign]

        # _dispatch_impl_agent returns (issue, branch, worktree_path, result)
        def fake_dispatch(
            issue: dict,
            branch: str,
            worktree_path: str,
            module: str,
            **kwargs: object,
        ) -> tuple:
            return (issue, branch, worktree_path, fake_run_result())

        with (
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues),
            patch(
                "brimstone.cli._filter_unblocked",
                side_effect=lambda issues, nums, store=None: issues,
            ),
            patch("brimstone.cli._sort_issues", side_effect=lambda issues: issues),
            patch("brimstone.cli._extract_module", return_value="cli"),
            patch("brimstone.cli._gh", side_effect=_gh_side_effect),
            patch("brimstone.cli._dispatch_impl_agent", side_effect=fake_dispatch),
            patch("brimstone.cli._count_all_issues_by_label", return_value=1),
        ):
            _run_impl_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
                store=store,
            )

        claimed_beads = [b for b in written_work_beads if b.state == "claimed"]
        assert len(claimed_beads) >= 1, (
            "At least one WorkBead(state='claimed') must be written during impl"
        )
        assert claimed_beads[0].issue_number == 42

    def test_dispatch_happens_after_claim(self, git_repo: Path, tmp_path: Path) -> None:
        """Dispatch must occur after claim — claim must not be skipped.

        Verifies the ordering invariant: _claim_issue is called before
        _dispatch_impl_agent for each issue.
        """
        os.chdir(git_repo)
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="impl")

        issue = _impl_issue(10, "Implement runner")
        call_count = [0]

        def open_issues(repo: str, milestone: str, label: str) -> list:
            call_count[0] += 1
            return [issue] if call_count[0] <= 2 else []

        call_log: list[str] = []

        def spy_claim(repo: str, issue_number: int, **kwargs: object) -> None:
            call_log.append(f"claim:{issue_number}")

        def spy_dispatch(
            issue: dict,
            branch: str,
            worktree_path: str,
            module: str,
            **kwargs: object,
        ) -> tuple:
            call_log.append(f"dispatch:{issue['number']}")
            return (issue, branch, worktree_path, fake_run_result())

        with (
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues),
            patch(
                "brimstone.cli._filter_unblocked",
                side_effect=lambda issues, nums, store=None: issues,
            ),
            patch("brimstone.cli._sort_issues", side_effect=lambda issues: issues),
            patch("brimstone.cli._extract_module", return_value="runner"),
            patch("brimstone.cli._claim_issue", side_effect=spy_claim),
            patch("brimstone.cli._unclaim_issue"),
            patch("brimstone.cli._dispatch_impl_agent", side_effect=spy_dispatch),
            patch("brimstone.cli._count_all_issues_by_label", return_value=1),
            patch("brimstone.cli._gh", side_effect=_gh_side_effect),
        ):
            _run_impl_worker(
                repo="owner/repo",
                milestone="v0.1.0",
                config=config,
                checkpoint=checkpoint,
            )

        assert "claim:10" in call_log, "Issue #10 must be claimed"
        assert "dispatch:10" in call_log, "Issue #10 must be dispatched"
        claim_idx = call_log.index("claim:10")
        dispatch_idx = call_log.index("dispatch:10")
        assert claim_idx < dispatch_idx, "claim must happen before dispatch"


# ---------------------------------------------------------------------------
# 3. _watchdog_scan exhaustion path
# ---------------------------------------------------------------------------


class TestWatchdogScanExhaustion:
    """_watchdog_scan detects a zombie and exhausts it when fix_attempts >= max."""

    def test_zombie_exhausted_when_max_attempts_reached(
        self, git_repo: Path, tmp_path: Path
    ) -> None:
        """WorkBead.state transitions to 'abandoned' when fix_attempts >= max.

        Setup:
        - PRBead with fix_attempts = WATCHDOG_MAX_FIX_ATTEMPTS (3)
        - WorkBead with claimed_at 2 hours ago (past WATCHDOG_TIMEOUT_MINUTES=45)
        - Issue is NOT in active_issue_numbers (no live future)

        Expect: _exhaust_issue called → WorkBead written with state='abandoned'.
        """
        os.chdir(git_repo)
        from brimstone.cli import WATCHDOG_MAX_FIX_ATTEMPTS

        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="impl")
        store = BeadStore(beads_dir=tmp_path / "beads" / "owner" / "repo")

        # PRBead at max fix_attempts, not merged
        pr_bead = _make_pr_bead(
            pr_number=55,
            issue_number=7,
            fix_attempts=WATCHDOG_MAX_FIX_ATTEMPTS,
            state="ci_failing",
        )
        store.write_pr_bead(pr_bead)

        # WorkBead claimed 2 hours ago (well past timeout)
        old_claim = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        work_bead = _make_work_bead(issue_number=7, state="claimed", claimed_at=old_claim)
        store.write_work_bead(work_bead)

        with (
            patch("brimstone.cli._gh", side_effect=_gh_side_effect),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            _watchdog_scan(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                active_issue_numbers=set(),  # no live futures
                default_branch="mainline",
            )

        # WorkBead must now be abandoned
        work_bead_after = store.read_work_bead(7)
        assert work_bead_after is not None
        assert work_bead_after.state == "abandoned", (
            f"WorkBead.state must be 'abandoned' after watchdog exhaustion, "
            f"got {work_bead_after.state!r}"
        )

    def test_active_issue_not_touched(self, git_repo: Path, tmp_path: Path) -> None:
        """Issues in active_issue_numbers must NOT be exhausted even if timed out.

        If a future is still running for an issue, the watchdog must skip it
        regardless of elapsed time — the agent is still alive.
        """
        os.chdir(git_repo)
        from brimstone.cli import WATCHDOG_MAX_FIX_ATTEMPTS

        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="impl")
        store = BeadStore(beads_dir=tmp_path / "beads" / "owner" / "repo")

        pr_bead = _make_pr_bead(
            fix_attempts=WATCHDOG_MAX_FIX_ATTEMPTS,
            state="ci_failing",
        )
        store.write_pr_bead(pr_bead)

        old_claim = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        work_bead = _make_work_bead(state="claimed", claimed_at=old_claim)
        store.write_work_bead(work_bead)

        exhaust_calls: list[int] = []

        with (
            patch("brimstone.cli._gh"),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch(
                "brimstone.cli._exhaust_issue",
                side_effect=lambda repo, n, reason, store=None: exhaust_calls.append(n),
            ),
        ):
            _watchdog_scan(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                active_issue_numbers={7},  # issue is still active
                default_branch="mainline",
            )

        assert exhaust_calls == [], (
            "Active issues must NOT be exhausted by watchdog, even if timed out"
        )

    def test_recent_claim_not_a_zombie(self, git_repo: Path, tmp_path: Path) -> None:
        """Issues claimed recently must not be treated as zombies."""
        os.chdir(git_repo)
        from brimstone.cli import WATCHDOG_MAX_FIX_ATTEMPTS

        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="impl")
        store = BeadStore(beads_dir=tmp_path / "beads" / "owner" / "repo")

        pr_bead = _make_pr_bead(fix_attempts=WATCHDOG_MAX_FIX_ATTEMPTS)
        store.write_pr_bead(pr_bead)

        # claimed 5 minutes ago — well within timeout
        recent_claim = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        work_bead = _make_work_bead(state="claimed", claimed_at=recent_claim)
        store.write_work_bead(work_bead)

        exhaust_calls: list[int] = []

        with (
            patch("brimstone.cli._gh"),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch(
                "brimstone.cli._exhaust_issue",
                side_effect=lambda repo, n, reason, store=None: exhaust_calls.append(n),
            ),
        ):
            _watchdog_scan(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                active_issue_numbers=set(),
                default_branch="mainline",
            )

        assert exhaust_calls == [], "Recently-claimed issues must not be treated as zombies"
