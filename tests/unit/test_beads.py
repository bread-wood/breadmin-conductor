"""Unit tests for src/brimstone/beads.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brimstone.beads import (
    BEAD_SCHEMA_VERSION,
    BeadCorruptError,
    BeadStore,
    FeedbackItem,
    MergeQueue,
    MergeQueueEntry,
    PRBead,
    WorkBead,
    make_bead_store,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> BeadStore:
    return BeadStore(beads_dir=tmp_path / "beads")


def _make_work_bead(issue_number: int = 42, state: str = "claimed") -> WorkBead:
    return WorkBead(
        v=BEAD_SCHEMA_VERSION,
        issue_number=issue_number,
        title="Test issue",
        milestone="v1",
        stage="impl",
        module="cli",
        priority="P2",
        state=state,
        branch=f"{issue_number}-test-slug",
        pr_id=None,
        retry_count=0,
        claimed_at="2026-03-04T10:00:00+00:00",
        closed_at=None,
    )


def _make_pr_bead(pr_number: int = 187, issue_number: int = 42, state: str = "open") -> PRBead:
    return PRBead(
        v=BEAD_SCHEMA_VERSION,
        pr_number=pr_number,
        issue_number=issue_number,
        branch="42-test-slug",
        state=state,
        ci_state=None,
        conflict_state=None,
        fix_attempts=0,
        feedback=[],
        created_at="2026-03-04T10:05:00+00:00",
        merged_at=None,
    )


# ---------------------------------------------------------------------------
# WorkBead round-trip
# ---------------------------------------------------------------------------


class TestWorkBeadRoundTrip:
    def test_write_and_read(self, store: BeadStore) -> None:
        bead = _make_work_bead()
        store.write_work_bead(bead)
        loaded = store.read_work_bead(42)
        assert loaded is not None
        assert loaded.issue_number == 42
        assert loaded.state == "claimed"
        assert loaded.branch == "42-test-slug"
        assert loaded.claimed_at == "2026-03-04T10:00:00+00:00"

    def test_read_absent_returns_none(self, store: BeadStore) -> None:
        assert store.read_work_bead(999) is None

    def test_overwrite_updates_state(self, store: BeadStore) -> None:
        bead = _make_work_bead(state="claimed")
        store.write_work_bead(bead)
        bead.state = "pr_open"
        store.write_work_bead(bead)
        loaded = store.read_work_bead(42)
        assert loaded is not None
        assert loaded.state == "pr_open"


# ---------------------------------------------------------------------------
# PRBead round-trip
# ---------------------------------------------------------------------------


class TestPRBeadRoundTrip:
    def test_write_and_read(self, store: BeadStore) -> None:
        bead = _make_pr_bead()
        store.write_pr_bead(bead)
        loaded = store.read_pr_bead(187)
        assert loaded is not None
        assert loaded.pr_number == 187
        assert loaded.issue_number == 42
        assert loaded.state == "open"

    def test_read_absent_returns_none(self, store: BeadStore) -> None:
        assert store.read_pr_bead(999) is None

    def test_feedback_round_trip(self, store: BeadStore) -> None:
        item = FeedbackItem(
            comment_id="c-1",
            author="octocat",
            is_bot=False,
            triage="fix_now",
            filed_issue=None,
            triage_reason="style nit",
        )
        bead = _make_pr_bead()
        bead.feedback = [item]
        store.write_pr_bead(bead)
        loaded = store.read_pr_bead(187)
        assert loaded is not None
        assert len(loaded.feedback) == 1
        assert loaded.feedback[0].comment_id == "c-1"
        assert loaded.feedback[0].triage == "fix_now"


# ---------------------------------------------------------------------------
# MergeQueue round-trip
# ---------------------------------------------------------------------------


class TestMergeQueueRoundTrip:
    def test_read_absent_returns_empty_queue(self, store: BeadStore) -> None:
        q = store.read_merge_queue()
        assert isinstance(q, MergeQueue)
        assert q.queue == []

    def test_write_and_read(self, store: BeadStore) -> None:
        entry = MergeQueueEntry(
            pr_number=187,
            issue_number=42,
            branch="42-test-slug",
            enqueued_at="2026-03-04T10:10:00+00:00",
        )
        q = MergeQueue(v=BEAD_SCHEMA_VERSION, queue=[entry], updated_at="2026-03-04T10:10:00+00:00")
        store.write_merge_queue(q)
        loaded = store.read_merge_queue()
        assert len(loaded.queue) == 1
        assert loaded.queue[0].pr_number == 187
        assert loaded.queue[0].branch == "42-test-slug"


# ---------------------------------------------------------------------------
# Atomic write: no partial files
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_no_tmp_file_after_write(self, store: BeadStore, tmp_path: Path) -> None:
        bead = _make_work_bead()
        store.write_work_bead(bead)
        tmp_files = list((tmp_path / "beads" / "work").glob("*.tmp"))
        assert tmp_files == [], "No .tmp files should remain after write"

    def test_target_file_exists_after_write(self, store: BeadStore, tmp_path: Path) -> None:
        bead = _make_work_bead()
        store.write_work_bead(bead)
        assert (tmp_path / "beads" / "work" / "42.json").exists()


# ---------------------------------------------------------------------------
# Corrupt file raises BeadCorruptError
# ---------------------------------------------------------------------------


class TestCorruptFile:
    def test_corrupt_work_bead_raises(self, store: BeadStore, tmp_path: Path) -> None:
        p = tmp_path / "beads" / "work" / "42.json"
        p.write_text("not json{{{", encoding="utf-8")
        with pytest.raises(BeadCorruptError):
            store.read_work_bead(42)

    def test_corrupt_pr_bead_raises(self, store: BeadStore, tmp_path: Path) -> None:
        p = tmp_path / "beads" / "prs" / "pr-187.json"
        p.write_text("not json{{{", encoding="utf-8")
        with pytest.raises(BeadCorruptError):
            store.read_pr_bead(187)


# ---------------------------------------------------------------------------
# list_work_beads — state filtering
# ---------------------------------------------------------------------------


class TestListWorkBeads:
    def test_list_all(self, store: BeadStore) -> None:
        store.write_work_bead(_make_work_bead(issue_number=1, state="claimed"))
        store.write_work_bead(_make_work_bead(issue_number=2, state="pr_open"))
        store.write_work_bead(_make_work_bead(issue_number=3, state="claimed"))
        beads = store.list_work_beads()
        assert len(beads) == 3

    def test_filter_by_state(self, store: BeadStore) -> None:
        store.write_work_bead(_make_work_bead(issue_number=1, state="claimed"))
        store.write_work_bead(_make_work_bead(issue_number=2, state="pr_open"))
        store.write_work_bead(_make_work_bead(issue_number=3, state="claimed"))
        claimed = store.list_work_beads(state="claimed")
        assert len(claimed) == 2
        assert all(b.state == "claimed" for b in claimed)

    def test_filter_no_match(self, store: BeadStore) -> None:
        store.write_work_bead(_make_work_bead(issue_number=1, state="claimed"))
        merged = store.list_work_beads(state="closed")
        assert merged == []

    def test_corrupt_file_skipped(self, store: BeadStore, tmp_path: Path) -> None:
        store.write_work_bead(_make_work_bead(issue_number=1, state="claimed"))
        # Inject a corrupt file
        (tmp_path / "beads" / "work" / "99.json").write_text("bad json", encoding="utf-8")
        beads = store.list_work_beads()
        assert len(beads) == 1  # corrupt file skipped


# ---------------------------------------------------------------------------
# list_pr_beads — state filtering
# ---------------------------------------------------------------------------


class TestListPRBeads:
    def test_filter_by_state(self, store: BeadStore) -> None:
        store.write_pr_bead(_make_pr_bead(pr_number=1, state="open"))
        store.write_pr_bead(_make_pr_bead(pr_number=2, state="merged"))
        store.write_pr_bead(_make_pr_bead(pr_number=3, state="open"))
        open_beads = store.list_pr_beads(state="open")
        assert len(open_beads) == 2


# ---------------------------------------------------------------------------
# delete_work_bead
# ---------------------------------------------------------------------------


class TestDeleteWorkBead:
    def test_delete_removes_file(self, store: BeadStore) -> None:
        bead = _make_work_bead()
        store.write_work_bead(bead)
        assert store.read_work_bead(42) is not None
        store.delete_work_bead(42)
        assert store.read_work_bead(42) is None

    def test_delete_absent_is_noop(self, store: BeadStore) -> None:
        # Should not raise
        store.delete_work_bead(999)


# ---------------------------------------------------------------------------
# flush() — no-op when state_repo_path is None
# ---------------------------------------------------------------------------


class TestFlush:
    def test_flush_noop_without_state_repo(self, tmp_path: Path) -> None:
        store = BeadStore(beads_dir=tmp_path / "beads", state_repo_path=None)
        # Should not raise and should not call subprocess
        with patch("subprocess.run") as mock_run:
            store.flush("test: flush noop")
            mock_run.assert_not_called()

    def test_flush_noop_when_repo_path_has_no_git_dir(self, tmp_path: Path) -> None:
        fake_repo = tmp_path / "fake-repo"
        fake_repo.mkdir()
        store = BeadStore(beads_dir=tmp_path / "beads", state_repo_path=fake_repo)
        with patch("subprocess.run") as mock_run:
            store.flush("test: no .git dir")
            mock_run.assert_not_called()

    def test_flush_calls_git_when_dirty(self, tmp_path: Path) -> None:
        repo = tmp_path / "state-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        store = BeadStore(beads_dir=tmp_path / "beads", state_repo_path=repo)
        with patch("subprocess.run") as mock_run:
            # Make git status return dirty output
            mock_run.return_value = MagicMock(stdout="M beads/work/42.json\n", returncode=0)
            store.flush("brimstone: claim #42")
            # First call is git status --porcelain, subsequent are add/commit/push
            assert mock_run.call_count >= 2

    def test_flush_skips_commit_when_clean(self, tmp_path: Path) -> None:
        repo = tmp_path / "state-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        store = BeadStore(beads_dir=tmp_path / "beads", state_repo_path=repo)
        with patch("subprocess.run") as mock_run:
            # git status returns empty (clean)
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            store.flush("brimstone: no-op")
            assert mock_run.call_count == 1  # only the status check


# ---------------------------------------------------------------------------
# make_bead_store — path construction
# ---------------------------------------------------------------------------


class TestMakeBreadStore:
    def test_beads_dir_from_repo_slug(self, tmp_path: Path) -> None:
        config = MagicMock()
        config.beads_dir = tmp_path / "beads"
        config.state_repo = None

        store = make_bead_store(config, "bread-wood/calculator")
        expected = tmp_path / "beads" / "bread-wood" / "calculator"
        assert store._beads_dir == expected

    def test_expanduser_called(self, tmp_path: Path) -> None:
        """Ensure ~ is expanded for the beads_dir path."""
        config = MagicMock()
        config.beads_dir = MagicMock()
        config.beads_dir.expanduser.return_value = tmp_path / "beads"
        config.state_repo = None

        make_bead_store(config, "owner/repo")
        config.beads_dir.expanduser.assert_called_once()

    def test_no_state_repo_clone_when_state_repo_is_none(self, tmp_path: Path) -> None:
        config = MagicMock()
        config.beads_dir = tmp_path / "beads"
        config.state_repo = None

        with patch("subprocess.run") as mock_run:
            store = make_bead_store(config, "owner/repo")
            mock_run.assert_not_called()
        assert store._state_repo_path is None

    def test_state_repo_cloned_when_not_present(self, tmp_path: Path) -> None:
        config = MagicMock()
        config.beads_dir = tmp_path / "beads"
        config.state_repo = "owner/state-repo"
        config.state_repo_dir = MagicMock()
        state_dir = tmp_path / "state-repos"
        state_dir.mkdir()
        config.state_repo_dir.expanduser.return_value = state_dir

        with patch("subprocess.run") as mock_run:
            store = make_bead_store(config, "owner/repo")
            # Should have called gh repo clone
            assert any("clone" in str(call) for call in mock_run.call_args_list), (
                "Expected gh repo clone to be called"
            )
        expected_state_path = state_dir / "owner-state-repo"
        assert store._state_repo_path == expected_state_path
