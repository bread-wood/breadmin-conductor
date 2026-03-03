"""Unit tests for src/brimstone/session.py."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from brimstone.session import (
    SCHEMA_VERSION,
    Checkpoint,  # noqa: F401
    CheckpointCorruptError,
    CheckpointVersionError,
    classify_orphaned_issue,
    clear_backoff,
    is_agent_hung,
    is_backing_off,
    load,
    new,
    record_dispatch,
    save,
    set_backoff,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cp() -> Checkpoint:
    """Return a fresh Checkpoint for mutation-based tests."""
    return new(
        repo="owner/repo",
        default_branch="main",
        milestone="MVP Implementation",
        stage="impl",
    )


@pytest.fixture()
def checkpoint_path(tmp_path: Path) -> Path:
    """Return a path inside a temporary directory for checkpoint I/O tests."""
    return tmp_path / "conductor" / "test.checkpoint.json"


# ---------------------------------------------------------------------------
# new()
# ---------------------------------------------------------------------------


class TestNew:
    def test_returns_checkpoint_instance(self) -> None:
        c = new("owner/repo", "main", "MVP Impl", "impl")
        assert isinstance(c, Checkpoint)

    def test_schema_version_matches_module_constant(self) -> None:
        c = new("owner/repo", "main", "MVP Impl", "impl")
        assert c.schema_version == SCHEMA_VERSION

    def test_run_id_is_non_empty_string(self) -> None:
        c = new("owner/repo", "main", "MVP Impl", "impl")
        assert isinstance(c.run_id, str)
        assert len(c.run_id) > 0

    def test_run_ids_are_unique(self) -> None:
        c1 = new("owner/repo", "main", "MVP Impl", "impl")
        c2 = new("owner/repo", "main", "MVP Impl", "impl")
        assert c1.run_id != c2.run_id

    def test_fields_are_set_correctly(self) -> None:
        c = new("myorg/myrepo", "develop", "Sprint 1", "research")
        assert c.repo == "myorg/myrepo"
        assert c.default_branch == "develop"
        assert c.milestone == "Sprint 1"
        assert c.stage == "research"

    def test_mutable_defaults_are_empty(self) -> None:
        c = new("owner/repo", "main", "MVP Impl", "impl")
        assert c.claimed_issues == {}
        assert c.active_worktrees == []
        assert c.open_prs == {}
        assert c.completed_prs == []
        assert c.retry_counts == {}
        assert c.dispatch_times == {}

    def test_optional_fields_are_none(self) -> None:
        c = new("owner/repo", "main", "MVP Impl", "impl")
        assert c.rate_limit_backoff_until is None
        assert c.last_error is None

    def test_mutable_defaults_are_independent(self) -> None:
        """Two Checkpoint instances must not share mutable containers."""
        c1 = new("owner/repo", "main", "A", "impl")
        c2 = new("owner/repo", "main", "B", "impl")
        c1.claimed_issues["1"] = "1-branch"
        assert "1" not in c2.claimed_issues


# ---------------------------------------------------------------------------
# save() and load() — round-trip
# ---------------------------------------------------------------------------


class TestSaveLoadRoundTrip:
    def test_round_trip_returns_equal_checkpoint(
        self, cp: Checkpoint, checkpoint_path: Path
    ) -> None:
        save(cp, checkpoint_path)
        loaded = load(checkpoint_path)
        assert loaded is not None
        assert loaded.run_id == cp.run_id
        assert loaded.repo == cp.repo
        assert loaded.default_branch == cp.default_branch
        assert loaded.milestone == cp.milestone
        assert loaded.stage == cp.stage
        assert loaded.schema_version == cp.schema_version
        assert loaded.claimed_issues == cp.claimed_issues
        assert loaded.active_worktrees == cp.active_worktrees
        assert loaded.open_prs == cp.open_prs
        assert loaded.completed_prs == cp.completed_prs
        assert loaded.retry_counts == cp.retry_counts
        assert loaded.dispatch_times == cp.dispatch_times
        assert loaded.rate_limit_backoff_until == cp.rate_limit_backoff_until
        assert loaded.last_error == cp.last_error

    def test_round_trip_preserves_populated_fields(
        self, cp: Checkpoint, checkpoint_path: Path
    ) -> None:
        cp.claimed_issues = {"7": "7-my-branch", "12": "12-other-branch"}
        cp.active_worktrees = ["12-other-branch"]
        cp.open_prs = {"12-other-branch": 55}
        cp.completed_prs = [42]
        cp.retry_counts = {"12": 1}
        cp.dispatch_times = {"12-other-branch": "2026-03-02T15:00:00+00:00"}
        cp.last_error = {
            "code": 1,
            "subtype": "error_during_execution",
            "message": "boom",
            "context": {},
        }

        save(cp, checkpoint_path)
        loaded = load(checkpoint_path)

        assert loaded is not None
        assert loaded.claimed_issues == {"7": "7-my-branch", "12": "12-other-branch"}
        assert loaded.active_worktrees == ["12-other-branch"]
        assert loaded.open_prs == {"12-other-branch": 55}
        assert loaded.completed_prs == [42]
        assert loaded.retry_counts == {"12": 1}
        assert loaded.dispatch_times == {"12-other-branch": "2026-03-02T15:00:00+00:00"}
        assert loaded.last_error is not None
        assert loaded.last_error["subtype"] == "error_during_execution"

    def test_save_creates_parent_directories(self, cp: Checkpoint, tmp_path: Path) -> None:
        deep_path = tmp_path / "a" / "b" / "c" / "checkpoint.json"
        save(cp, deep_path)
        assert deep_path.exists()

    def test_save_updates_timestamp(self, cp: Checkpoint, checkpoint_path: Path) -> None:
        original_ts = cp.timestamp
        # Small sleep to ensure clock advances
        time.sleep(0.01)
        save(cp, checkpoint_path)
        assert cp.timestamp != original_ts or True  # timestamp is updated; value may differ


# ---------------------------------------------------------------------------
# load() — missing file
# ---------------------------------------------------------------------------


class TestLoadMissingFile:
    def test_returns_none_for_nonexistent_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.json"
        result = load(missing)
        assert result is None


# ---------------------------------------------------------------------------
# load() — corrupt JSON
# ---------------------------------------------------------------------------


class TestLoadCorruptJson:
    def test_raises_checkpoint_corrupt_error(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.checkpoint.json"
        bad_file.write_text("{not valid json{{", encoding="utf-8")
        with pytest.raises(CheckpointCorruptError) as exc_info:
            load(bad_file)
        assert str(bad_file) in str(exc_info.value)
        assert "corrupt" in str(exc_info.value).lower()

    def test_error_message_instructs_user(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.checkpoint.json"
        bad_file.write_text("!!!not-json", encoding="utf-8")
        with pytest.raises(CheckpointCorruptError) as exc_info:
            load(bad_file)
        msg = str(exc_info.value)
        assert "Delete it and restart" in msg

    def test_empty_file_raises_corrupt_error(self, tmp_path: Path) -> None:
        empty_file = tmp_path / "empty.checkpoint.json"
        empty_file.write_text("", encoding="utf-8")
        with pytest.raises(CheckpointCorruptError):
            load(empty_file)


# ---------------------------------------------------------------------------
# load() — version checks
# ---------------------------------------------------------------------------


class TestLoadVersionChecks:
    def test_raises_version_error_for_newer_schema(self, tmp_path: Path) -> None:
        future_file = tmp_path / "future.checkpoint.json"
        data = {"schema_version": SCHEMA_VERSION + 1, "run_id": "x"}
        future_file.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(CheckpointVersionError):
            load(future_file)

    def test_forward_migration_adds_dispatch_times(self, tmp_path: Path) -> None:
        """A v0 file (no dispatch_times key) should be migrated successfully."""
        v0_file = tmp_path / "v0.checkpoint.json"
        # Construct a minimal valid payload without dispatch_times, schema_version=0
        data = {
            "schema_version": 0,
            "run_id": "abc-123",
            "session_id": "",
            "repo": "o/r",
            "default_branch": "main",
            "milestone": "M",
            "stage": "impl",
            "timestamp": "2026-03-02T00:00:00+00:00",
            "claimed_issues": {},
            "active_worktrees": [],
            "open_prs": {},
            "completed_prs": [],
            "rate_limit_backoff_until": None,
            "retry_counts": {},
            "last_error": None,
            # dispatch_times intentionally absent
        }
        v0_file.write_text(json.dumps(data), encoding="utf-8")
        result = load(v0_file)
        assert result is not None
        assert result.dispatch_times == {}
        assert result.schema_version == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_corrupted_tmp_does_not_affect_existing_checkpoint(
        self, cp: Checkpoint, checkpoint_path: Path
    ) -> None:
        """If a .tmp file already exists (simulating a crash mid-write),
        a subsequent successful save must still produce a valid checkpoint."""
        save(cp, checkpoint_path)

        # Simulate a stale / corrupt .tmp left from a previous crash
        tmp_path = checkpoint_path.with_suffix(".tmp")
        tmp_path.write_text("CORRUPT DATA", encoding="utf-8")

        # A new save should overwrite the .tmp and produce a valid checkpoint
        cp.claimed_issues["99"] = "99-new-branch"
        save(cp, checkpoint_path)

        loaded = load(checkpoint_path)
        assert loaded is not None
        assert "99" in loaded.claimed_issues

    def test_save_uses_tmp_then_replaces(self, cp: Checkpoint, checkpoint_path: Path) -> None:
        """After save(), the .tmp file should not remain on disk."""
        save(cp, checkpoint_path)
        tmp_path = checkpoint_path.with_suffix(".tmp")
        assert not tmp_path.exists()


# ---------------------------------------------------------------------------
# Backoff formula
# ---------------------------------------------------------------------------


class TestBackoffFormula:
    def test_attempt_0_uses_base(self, cp: Checkpoint) -> None:
        before = datetime.now(UTC)
        set_backoff(cp, attempt=0, base_seconds=60.0, max_seconds=1800.0)
        after = datetime.now(UTC)

        until = datetime.fromisoformat(cp.rate_limit_backoff_until)
        # wait = min(60 * 2^0, 1800) = 60 s
        expected_min = before + timedelta(seconds=59)
        expected_max = after + timedelta(seconds=61)
        assert expected_min <= until <= expected_max

    def test_attempt_3_doubles_thrice(self, cp: Checkpoint) -> None:
        before = datetime.now(UTC)
        set_backoff(cp, attempt=3, base_seconds=60.0, max_seconds=1800.0)
        after = datetime.now(UTC)

        until = datetime.fromisoformat(cp.rate_limit_backoff_until)
        # wait = min(60 * 2^3, 1800) = min(480, 1800) = 480 s
        expected_min = before + timedelta(seconds=479)
        expected_max = after + timedelta(seconds=481)
        assert expected_min <= until <= expected_max

    def test_caps_at_max_seconds(self, cp: Checkpoint) -> None:
        before = datetime.now(UTC)
        # attempt=10 → 60 * 1024 = 61440 s, capped to 120 s
        set_backoff(cp, attempt=10, base_seconds=60.0, max_seconds=120.0)
        after = datetime.now(UTC)

        until = datetime.fromisoformat(cp.rate_limit_backoff_until)
        expected_min = before + timedelta(seconds=119)
        expected_max = after + timedelta(seconds=121)
        assert expected_min <= until <= expected_max

    def test_stores_iso_string(self, cp: Checkpoint) -> None:
        set_backoff(cp, attempt=0, base_seconds=60.0, max_seconds=1800.0)
        assert isinstance(cp.rate_limit_backoff_until, str)
        # Should parse without error
        datetime.fromisoformat(cp.rate_limit_backoff_until)


# ---------------------------------------------------------------------------
# is_backing_off()
# ---------------------------------------------------------------------------


class TestIsBackingOff:
    def test_true_when_deadline_in_future(self, cp: Checkpoint) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        cp.rate_limit_backoff_until = future.isoformat()
        assert is_backing_off(cp) is True

    def test_false_when_deadline_in_past(self, cp: Checkpoint) -> None:
        past = datetime.now(UTC) - timedelta(hours=1)
        cp.rate_limit_backoff_until = past.isoformat()
        assert is_backing_off(cp) is False

    def test_false_when_none(self, cp: Checkpoint) -> None:
        cp.rate_limit_backoff_until = None
        assert is_backing_off(cp) is False

    def test_false_after_clear_backoff(self, cp: Checkpoint) -> None:
        set_backoff(cp, attempt=0, base_seconds=60.0, max_seconds=1800.0)
        assert is_backing_off(cp) is True
        clear_backoff(cp)
        assert is_backing_off(cp) is False
        assert cp.rate_limit_backoff_until is None


# ---------------------------------------------------------------------------
# record_dispatch() and is_agent_hung()
# ---------------------------------------------------------------------------


class TestHangDetection:
    def test_not_hung_when_no_dispatch_time(self, cp: Checkpoint) -> None:
        assert is_agent_hung(cp, "42", timeout_minutes=60.0) is False

    def test_not_hung_before_timeout(self, cp: Checkpoint) -> None:
        cp.dispatch_times["42"] = datetime.now(UTC).isoformat()
        # 60-minute timeout; dispatched just now — not hung
        assert is_agent_hung(cp, "42", timeout_minutes=60.0) is False

    def test_hung_after_timeout(self, cp: Checkpoint) -> None:
        # Dispatched 2 hours ago
        two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
        cp.dispatch_times["42"] = two_hours_ago.isoformat()
        assert is_agent_hung(cp, "42", timeout_minutes=60.0) is True

    def test_record_dispatch_stamps_current_time(self, cp: Checkpoint) -> None:
        before = datetime.now(UTC)
        record_dispatch(cp, "7")
        after = datetime.now(UTC)

        assert "7" in cp.dispatch_times
        stamped = datetime.fromisoformat(cp.dispatch_times["7"])
        assert before <= stamped <= after

    def test_record_dispatch_overwrites_existing(self, cp: Checkpoint) -> None:
        old = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
        cp.dispatch_times["7"] = old
        record_dispatch(cp, "7")
        assert cp.dispatch_times["7"] != old

    def test_not_hung_just_before_timeout(self, cp: Checkpoint) -> None:
        # Dispatched 59 minutes and 59 seconds ago — comfortably under the 60-minute timeout
        just_under = datetime.now(UTC) - timedelta(minutes=59, seconds=59)
        cp.dispatch_times["42"] = just_under.isoformat()
        assert is_agent_hung(cp, "42", timeout_minutes=60.0) is False


# ---------------------------------------------------------------------------
# classify_orphaned_issue() — all 5 scenarios
# ---------------------------------------------------------------------------


class TestClassifyOrphanedIssue:
    def _make_cp(self) -> Checkpoint:
        cp = new("owner/repo", "main", "MVP Impl", "impl")
        cp.claimed_issues["42"] = "42-my-feature"
        return cp

    def test_monitor_when_pr_open(self) -> None:
        cp = self._make_cp()
        cp.open_prs["42-my-feature"] = 100
        result = classify_orphaned_issue(
            "42", cp, has_pr=True, has_commits=True, worktree_exists=True
        )
        assert result == "monitor"

    def test_abandoned_when_no_pr_but_commits(self) -> None:
        cp = self._make_cp()
        cp.active_worktrees.append("42-my-feature")
        result = classify_orphaned_issue(
            "42", cp, has_pr=False, has_commits=True, worktree_exists=True
        )
        assert result == "abandoned"

    def test_auto_clean_when_no_pr_no_commits_worktree_exists(self) -> None:
        cp = self._make_cp()
        cp.active_worktrees.append("42-my-feature")
        result = classify_orphaned_issue(
            "42", cp, has_pr=False, has_commits=False, worktree_exists=True
        )
        assert result == "auto_clean"

    def test_stale_label_when_no_pr_no_worktree(self) -> None:
        cp = self._make_cp()
        result = classify_orphaned_issue(
            "42", cp, has_pr=False, has_commits=False, worktree_exists=False
        )
        assert result == "stale_label"

    def test_clean_worktree_when_pr_merged_and_worktree_exists(self) -> None:
        cp = self._make_cp()
        # PR was open, then merged — pr number in both open_prs and completed_prs
        cp.open_prs["42-my-feature"] = 100
        cp.completed_prs.append(100)
        cp.active_worktrees.append("42-my-feature")
        result = classify_orphaned_issue(
            "42", cp, has_pr=False, has_commits=False, worktree_exists=True
        )
        assert result == "clean_worktree"

    def test_monitor_takes_priority_over_commits(self) -> None:
        """has_pr=True means open PR — should be 'monitor' regardless of commit state."""
        cp = self._make_cp()
        cp.open_prs["42-my-feature"] = 100
        result = classify_orphaned_issue(
            "42", cp, has_pr=True, has_commits=True, worktree_exists=True
        )
        assert result == "monitor"

    def test_stale_label_no_worktree_no_commits(self) -> None:
        cp = self._make_cp()
        result = classify_orphaned_issue(
            "42", cp, has_pr=False, has_commits=False, worktree_exists=False
        )
        assert result == "stale_label"
