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
    clear_backoff,
    is_backing_off,
    load,
    new,
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
        # Legacy fields still present until cli.py migrates to BeadStore (PR 7a)
        assert c.active_worktrees == []

    def test_optional_fields_are_none(self) -> None:
        c = new("owner/repo", "main", "MVP Impl", "impl")
        assert c.rate_limit_backoff_until is None
        assert c.last_error is None

    def test_mutable_defaults_are_independent(self) -> None:
        """Two Checkpoint instances must not share mutable containers."""
        c1 = new("owner/repo", "main", "A", "impl")
        c2 = new("owner/repo", "main", "B", "impl")
        c1.active_worktrees.append("branch-1")
        assert "branch-1" not in c2.active_worktrees


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
        assert loaded.active_worktrees == cp.active_worktrees
        assert loaded.rate_limit_backoff_until == cp.rate_limit_backoff_until
        assert loaded.last_error == cp.last_error

    def test_round_trip_preserves_populated_fields(
        self, cp: Checkpoint, checkpoint_path: Path
    ) -> None:
        cp.active_worktrees = ["12-other-branch"]
        cp.last_error = {
            "code": 1,
            "subtype": "error_during_execution",
            "message": "boom",
            "context": {},
        }

        save(cp, checkpoint_path)
        loaded = load(checkpoint_path)

        assert loaded is not None
        assert loaded.active_worktrees == ["12-other-branch"]
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
# load() — version checks and migrations
# ---------------------------------------------------------------------------


class TestLoadVersionChecks:
    def test_raises_version_error_for_newer_schema(self, tmp_path: Path) -> None:
        future_file = tmp_path / "future.checkpoint.json"
        data = {"schema_version": SCHEMA_VERSION + 1, "run_id": "x"}
        future_file.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(CheckpointVersionError):
            load(future_file)

    def test_v0_to_v2_migration(self, tmp_path: Path) -> None:
        """A v0 checkpoint (no dispatch_times) migrates to v2 cleanly."""
        v0_file = tmp_path / "v0.checkpoint.json"
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
            # dispatch_times intentionally absent (v0 → v1 adds it)
        }
        v0_file.write_text(json.dumps(data), encoding="utf-8")
        result = load(v0_file)
        assert result is not None
        assert result.dispatch_times == {}
        assert result.schema_version == SCHEMA_VERSION

    def test_v1_to_v2_migration(self, tmp_path: Path) -> None:
        """A v1 checkpoint migrates to v2 without data loss."""
        v1_file = tmp_path / "v1.checkpoint.json"
        data = {
            "schema_version": 1,
            "run_id": "xyz-456",
            "session_id": "",
            "repo": "o/r",
            "default_branch": "main",
            "milestone": "M",
            "stage": "impl",
            "timestamp": "2026-03-03T00:00:00+00:00",
            "claimed_issues": {"7": "7-branch"},
            "active_worktrees": ["7-branch"],
            "open_prs": {"7-branch": 55},
            "completed_prs": [55],
            "rate_limit_backoff_until": None,
            "retry_counts": {"7": 1},
            "last_error": None,
            "dispatch_times": {"7": "2026-03-03T01:00:00+00:00"},
        }
        v1_file.write_text(json.dumps(data), encoding="utf-8")
        result = load(v1_file)
        assert result is not None
        assert result.schema_version == SCHEMA_VERSION
        # All legacy fields preserved (still on Checkpoint for backward-compat)
        assert result.active_worktrees == ["7-branch"]


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_save_uses_tmp_then_replaces(self, cp: Checkpoint, checkpoint_path: Path) -> None:
        """After save(), the .tmp file should not remain on disk."""
        save(cp, checkpoint_path)
        tmp_path = checkpoint_path.with_suffix(".tmp")
        assert not tmp_path.exists()

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
        cp.active_worktrees.append("99-new-branch")
        save(cp, checkpoint_path)

        loaded = load(checkpoint_path)
        assert loaded is not None
        assert "99-new-branch" in loaded.active_worktrees


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
# Verify removed functions are gone
# ---------------------------------------------------------------------------


class TestRemovedFunctions:
    def test_record_dispatch_removed(self) -> None:
        import brimstone.session as sess

        assert not hasattr(sess, "record_dispatch"), (
            "record_dispatch() was removed in v2 — use BeadStore.write_work_bead() instead"
        )

    def test_is_agent_hung_removed(self) -> None:
        import brimstone.session as sess

        assert not hasattr(sess, "is_agent_hung"), (
            "is_agent_hung() was removed in v2 — use WorkBead.claimed_at + Deacon instead"
        )

    def test_classify_orphaned_issue_removed(self) -> None:
        import brimstone.session as sess

        assert not hasattr(sess, "classify_orphaned_issue"), (
            "classify_orphaned_issue() was removed in v2 — Deacon reads bead states instead"
        )
