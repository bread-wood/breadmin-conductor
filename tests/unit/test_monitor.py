"""Unit tests for brimstone.monitor."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from brimstone.beads import (
    BEAD_SCHEMA_VERSION,
    BeadStore,
    MergeQueue,
    MergeQueueEntry,
    WorkBead,
)
from brimstone.monitor import (
    Anomaly,
    check_dep_integrity,
    check_label_drift,
    check_orphaned_merge,
    check_pre_pr_zombies,
    check_state_regressions,
    process_anomalies,
    run_all_detectors,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_work_bead(
    issue_number: int,
    state: str = "open",
    stage: str = "impl",
    milestone: str = "v1.0",
    branch: str = "",
    pr_id: str | None = None,
    claimed_at: str | None = None,
    blocked_by: list[int] | None = None,
) -> WorkBead:
    return WorkBead(
        v=BEAD_SCHEMA_VERSION,
        issue_number=issue_number,
        title=f"Issue #{issue_number}",
        milestone=milestone,
        stage=stage,
        module="cli",
        priority="P2",
        state=state,
        branch=branch or f"{issue_number}-branch",
        pr_id=pr_id,
        claimed_at=claimed_at,
        blocked_by=blocked_by or [],
    )


def _make_store(tmp_path: Path) -> BeadStore:
    return BeadStore(tmp_path)


# ---------------------------------------------------------------------------
# Anomaly.fingerprint
# ---------------------------------------------------------------------------


def test_anomaly_fingerprint_stable():
    a = Anomaly(
        kind="label_drift",
        severity="warning",
        description="test",
        details={"issue_number": 5, "has_label": False},
    )
    assert a.fingerprint() == a.fingerprint()


def test_anomaly_fingerprint_differs_by_kind():
    a = Anomaly(kind="label_drift", severity="warning", description="x", details={"n": 1})
    b = Anomaly(kind="dep_cycle", severity="warning", description="x", details={"n": 1})
    assert a.fingerprint() != b.fingerprint()


def test_anomaly_fingerprint_differs_by_details():
    a = Anomaly(kind="label_drift", severity="warning", description="x", details={"n": 1})
    b = Anomaly(kind="label_drift", severity="warning", description="x", details={"n": 2})
    assert a.fingerprint() != b.fingerprint()


# ---------------------------------------------------------------------------
# check_label_drift
# ---------------------------------------------------------------------------


def _make_gh_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.stdout = stdout
    r.returncode = returncode
    return r


def test_check_label_drift_clean(tmp_path):
    """No beads, no in-progress labels → no anomalies."""
    store = _make_store(tmp_path)
    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result("[]")
        anomalies = check_label_drift(store, "owner/repo")
    assert anomalies == []


def test_check_label_drift_claimed_missing_label(tmp_path):
    """Claimed bead but no in-progress label → label_drift anomaly."""
    store = _make_store(tmp_path)
    bead = _make_work_bead(10, state="claimed")
    store.write_work_bead(bead)

    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result("[]")
        anomalies = check_label_drift(store, "owner/repo")

    assert len(anomalies) == 1
    assert anomalies[0].kind == "label_drift"
    assert anomalies[0].details["issue_number"] == 10
    assert anomalies[0].details["has_label"] is False


def test_check_label_drift_label_no_bead(tmp_path):
    """in-progress label on issue with no bead → label_drift anomaly."""
    store = _make_store(tmp_path)

    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result(json.dumps([{"number": 42}]))
        anomalies = check_label_drift(store, "owner/repo")

    assert len(anomalies) == 1
    assert anomalies[0].kind == "label_drift"
    assert anomalies[0].details["issue_number"] == 42
    assert anomalies[0].details["bead_state"] is None


def test_check_label_drift_closed_bead_with_label_is_critical(tmp_path):
    """Closed bead with in-progress label → critical severity."""
    store = _make_store(tmp_path)
    bead = _make_work_bead(7, state="closed")
    store.write_work_bead(bead)

    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result(json.dumps([{"number": 7}]))
        anomalies = check_label_drift(store, "owner/repo")

    assert len(anomalies) == 1
    assert anomalies[0].severity == "critical"


def test_check_label_drift_clean_claimed_with_label(tmp_path):
    """Claimed bead AND in-progress label → no anomaly."""
    store = _make_store(tmp_path)
    bead = _make_work_bead(5, state="claimed")
    store.write_work_bead(bead)

    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result(json.dumps([{"number": 5}]))
        anomalies = check_label_drift(store, "owner/repo")

    assert anomalies == []


# ---------------------------------------------------------------------------
# check_dep_integrity
# ---------------------------------------------------------------------------


def test_check_dep_integrity_clean(tmp_path):
    """No deps → no anomalies."""
    store = _make_store(tmp_path)
    store.write_work_bead(_make_work_bead(1))
    assert check_dep_integrity(store) == []


def test_check_dep_integrity_phantom_dep(tmp_path):
    """Bead blocked_by an issue number with no bead → phantom_dep anomaly."""
    store = _make_store(tmp_path)
    bead = _make_work_bead(2, blocked_by=[999])
    store.write_work_bead(bead)

    anomalies = check_dep_integrity(store)
    assert len(anomalies) == 1
    assert anomalies[0].kind == "phantom_dep"
    assert anomalies[0].details["phantom_dep"] == 999


def test_check_dep_integrity_cycle(tmp_path):
    """Two beads blocking each other → dep_cycle anomaly."""
    store = _make_store(tmp_path)
    store.write_work_bead(_make_work_bead(10, blocked_by=[11]))
    store.write_work_bead(_make_work_bead(11, blocked_by=[10]))

    anomalies = check_dep_integrity(store)
    cycle_anomalies = [a for a in anomalies if a.kind == "dep_cycle"]
    assert len(cycle_anomalies) >= 1


def test_check_dep_integrity_closed_phantom_skipped(tmp_path):
    """Closed bead blocked_by phantom → not flagged (closed beads skip check)."""
    store = _make_store(tmp_path)
    bead = _make_work_bead(3, state="closed", blocked_by=[999])
    store.write_work_bead(bead)

    anomalies = [a for a in check_dep_integrity(store) if a.kind == "phantom_dep"]
    assert anomalies == []


# ---------------------------------------------------------------------------
# check_state_regressions
# ---------------------------------------------------------------------------


def test_check_state_regressions_clean(tmp_path):
    """Normal transitions → no anomalies."""
    store = _make_store(tmp_path)
    bead = _make_work_bead(1)
    store.write_work_bead(bead)
    store.append_event("work", "1", None, "open")
    store.append_event("work", "1", "open", "claimed")
    store.append_event("work", "1", "claimed", "merge_ready")
    store.append_event("work", "1", "merge_ready", "closed")

    assert check_state_regressions(store) == []


def test_check_state_regressions_bad_transition(tmp_path):
    """merge_ready → open transition → state_regression anomaly."""
    store = _make_store(tmp_path)
    bead = _make_work_bead(1)
    store.write_work_bead(bead)
    store.append_event("work", "1", None, "open")
    store.append_event("work", "1", "open", "claimed")
    store.append_event("work", "1", "claimed", "merge_ready")
    store.append_event("work", "1", "merge_ready", "open")  # illegal

    anomalies = check_state_regressions(store)
    assert len(anomalies) == 1
    assert anomalies[0].kind == "state_regression"
    assert anomalies[0].details["from_state"] == "merge_ready"
    assert anomalies[0].details["to_state"] == "open"


def test_check_state_regressions_closed_to_open(tmp_path):
    """closed → open → state_regression anomaly."""
    store = _make_store(tmp_path)
    bead = _make_work_bead(2)
    store.write_work_bead(bead)
    store.append_event("work", "2", "open", "closed")
    store.append_event("work", "2", "closed", "open")  # illegal

    anomalies = check_state_regressions(store)
    assert any(a.kind == "state_regression" for a in anomalies)


# ---------------------------------------------------------------------------
# check_orphaned_merge
# ---------------------------------------------------------------------------


def test_check_orphaned_merge_clean(tmp_path):
    """merge_ready bead in MergeQueue → no anomaly."""
    store = _make_store(tmp_path)
    bead = _make_work_bead(5, state="merge_ready")
    store.write_work_bead(bead)

    queue = MergeQueue(v=BEAD_SCHEMA_VERSION)
    queue.queue.append(
        MergeQueueEntry(
            pr_number=100, issue_number=5, branch="5-b", enqueued_at="2024-01-01T00:00:00+00:00"
        )
    )
    store.write_merge_queue(queue)

    assert check_orphaned_merge(store) == []


def test_check_orphaned_merge_missing_from_queue(tmp_path):
    """merge_ready bead absent from MergeQueue → orphaned_merge anomaly."""
    store = _make_store(tmp_path)
    bead = _make_work_bead(5, state="merge_ready")
    store.write_work_bead(bead)
    # Don't add to queue

    anomalies = check_orphaned_merge(store)
    assert len(anomalies) == 1
    assert anomalies[0].kind == "orphaned_merge"
    assert anomalies[0].details["issue_number"] == 5


def test_check_orphaned_merge_no_merge_ready(tmp_path):
    """No merge_ready beads → no anomalies."""
    store = _make_store(tmp_path)
    store.write_work_bead(_make_work_bead(1, state="open"))
    assert check_orphaned_merge(store) == []


# ---------------------------------------------------------------------------
# check_pre_pr_zombies
# ---------------------------------------------------------------------------


def test_check_pre_pr_zombies_no_claimed(tmp_path):
    """No claimed beads → no anomalies."""
    store = _make_store(tmp_path)
    assert check_pre_pr_zombies(store) == []


def test_check_pre_pr_zombies_fresh_claimed(tmp_path):
    """Claimed bead claimed recently → no anomaly."""
    store = _make_store(tmp_path)
    from datetime import timedelta

    recent = (
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        - timedelta(minutes=10)
    ).isoformat()
    bead = _make_work_bead(3, state="claimed", claimed_at=recent)
    store.write_work_bead(bead)

    anomalies = check_pre_pr_zombies(store, timeout_minutes=60.0)
    assert anomalies == []


def test_check_pre_pr_zombies_old_no_pr(tmp_path):
    """Claimed bead older than timeout with no pr_id → pre_pr_zombie anomaly."""
    store = _make_store(tmp_path)
    from datetime import timedelta

    old = (
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        - timedelta(minutes=120)
    ).isoformat()
    bead = _make_work_bead(4, state="claimed", claimed_at=old, pr_id=None)
    store.write_work_bead(bead)

    anomalies = check_pre_pr_zombies(store, timeout_minutes=60.0)
    assert len(anomalies) == 1
    assert anomalies[0].kind == "pre_pr_zombie"
    assert anomalies[0].details["issue_number"] == 4


def test_check_pre_pr_zombies_has_pr_skipped(tmp_path):
    """Claimed bead with pr_id → not a zombie."""
    store = _make_store(tmp_path)
    from datetime import timedelta

    old = (
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        - timedelta(minutes=120)
    ).isoformat()
    bead = _make_work_bead(5, state="claimed", claimed_at=old, pr_id="pr-200")
    store.write_work_bead(bead)

    anomalies = check_pre_pr_zombies(store, timeout_minutes=60.0)
    assert anomalies == []


# ---------------------------------------------------------------------------
# process_anomalies (dedup + filing)
# ---------------------------------------------------------------------------


def test_process_anomalies_dry_run(tmp_path, capsys):
    """Dry-run mode prints anomalies without calling gh."""
    store = _make_store(tmp_path)
    anomaly = Anomaly(kind="test_kind", severity="warning", description="test anomaly")

    with patch("brimstone.monitor.file_anomaly_issue") as mock_file:
        urls = process_anomalies([anomaly], store, "owner/repo", dry_run=True)

    mock_file.assert_not_called()
    assert urls == []
    captured = capsys.readouterr()
    assert "test_kind" in captured.out


def test_process_anomalies_dedup(tmp_path):
    """Same anomaly filed twice: second call is skipped."""
    store = _make_store(tmp_path)
    anomaly = Anomaly(kind="dup_kind", severity="warning", description="dup", details={"x": 1})
    url = "https://github.com/issues/99"

    with patch("brimstone.monitor.file_anomaly_issue", return_value=url) as mock_file:
        process_anomalies([anomaly], store, "owner/repo")
        process_anomalies([anomaly], store, "owner/repo")

    # Should only have been called once
    assert mock_file.call_count == 1


def test_process_anomalies_files_critical(tmp_path):
    """Critical anomaly → file_anomaly_issue called and URL returned."""
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="dep_cycle", severity="critical", description="cycle!", details={"cycle": [1, 2]}
    )
    url = "https://github.com/issues/50"

    with patch("brimstone.monitor.file_anomaly_issue", return_value=url) as mock_file:
        urls = process_anomalies([anomaly], store, "owner/repo")

    mock_file.assert_called_once()
    assert urls == ["https://github.com/issues/50"]


# ---------------------------------------------------------------------------
# run_all_detectors
# ---------------------------------------------------------------------------


def test_run_all_detectors_returns_list(tmp_path):
    """run_all_detectors always returns a list (no crash even on empty store)."""
    store = _make_store(tmp_path)
    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result("[]")
        anomalies = run_all_detectors(store, "owner/repo")
    assert isinstance(anomalies, list)


def test_run_all_detectors_detector_error_caught(tmp_path):
    """A detector that raises → detector_error anomaly, not a crash."""
    store = _make_store(tmp_path)

    def _bad_detector(store, repo):
        raise RuntimeError("boom")

    with patch("brimstone.monitor._DETECTORS", [("bad", _bad_detector)]):
        with patch("brimstone.monitor._gh") as mock_gh:
            mock_gh.return_value = _make_gh_result("[]")
            anomalies = run_all_detectors(store, "owner/repo")

    assert len(anomalies) == 1
    assert anomalies[0].kind == "detector_error"
    assert "boom" in anomalies[0].description
