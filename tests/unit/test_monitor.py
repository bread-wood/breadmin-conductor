"""Unit tests for brimstone.monitor."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from brimstone.beads import (
    BEAD_SCHEMA_VERSION,
    AnomalyBead,
    BeadStore,
    MergeQueue,
    MergeQueueEntry,
    WorkBead,
)
from brimstone.monitor import (
    INLINE_REPAIR_MAX_ATTEMPTS,
    Anomaly,
    _anomaly_id,
    _apply_inline_repair,
    _get_active_milestone,
    _inline_repair_label_drift,
    _inline_repair_orphaned_merge,
    check_dep_integrity,
    check_label_drift,
    check_orphaned_merge,
    check_pre_pr_zombies,
    check_state_regressions,
    classify_blocking,
    classify_repair_tier,
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


def _make_gh_result(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.stdout = stdout
    r.returncode = returncode
    return r


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
# _anomaly_id
# ---------------------------------------------------------------------------


def test_anomaly_id_stable():
    a = Anomaly(kind="label_drift", severity="warning", description="x", details={"n": 1})
    assert _anomaly_id(a) == _anomaly_id(a)
    assert len(_anomaly_id(a)) == 16


def test_anomaly_id_differs_by_kind():
    a = Anomaly(kind="label_drift", severity="warning", description="x", details={"n": 1})
    b = Anomaly(kind="dep_cycle", severity="warning", description="x", details={"n": 1})
    assert _anomaly_id(a) != _anomaly_id(b)


# ---------------------------------------------------------------------------
# classify_repair_tier
# ---------------------------------------------------------------------------


def test_classify_repair_tier_inline():
    a = Anomaly(kind="label_drift", severity="w", description="")
    assert classify_repair_tier(a) == "inline"
    b = Anomaly(kind="orphaned_merge", severity="w", description="")
    assert classify_repair_tier(b) == "inline"


def test_classify_repair_tier_bug():
    a = Anomaly(kind="pre_pr_zombie", severity="w", description="")
    assert classify_repair_tier(a) == "bug"


def test_classify_repair_tier_probe():
    for kind in ("dep_cycle", "phantom_dep", "state_regression", "detector_error"):
        assert classify_repair_tier(Anomaly(kind=kind, severity="c", description="")) == "probe"


# ---------------------------------------------------------------------------
# classify_blocking
# ---------------------------------------------------------------------------


def test_classify_blocking_always_blocking(tmp_path):
    store = _make_store(tmp_path)
    for kind in ("dep_cycle", "phantom_dep", "state_regression", "orphaned_merge"):
        a = Anomaly(kind=kind, severity="critical", description="x")
        assert classify_blocking(a, store, "v1.0") is True


def test_classify_blocking_detector_error_never(tmp_path):
    store = _make_store(tmp_path)
    a = Anomaly(kind="detector_error", severity="warning", description="x")
    assert classify_blocking(a, store, "v1.0") is False


def test_classify_blocking_pre_pr_zombie_in_active_milestone(tmp_path):
    store = _make_store(tmp_path)
    bead = _make_work_bead(7, state="claimed", milestone="v1.0")
    store.write_work_bead(bead)
    a = Anomaly(
        kind="pre_pr_zombie",
        severity="warning",
        description="x",
        details={"issue_number": 7},
    )
    assert classify_blocking(a, store, "v1.0") is True


def test_classify_blocking_pre_pr_zombie_different_milestone(tmp_path):
    store = _make_store(tmp_path)
    bead = _make_work_bead(7, state="claimed", milestone="v2.0")
    store.write_work_bead(bead)
    a = Anomaly(
        kind="pre_pr_zombie",
        severity="warning",
        description="x",
        details={"issue_number": 7},
    )
    assert classify_blocking(a, store, "v1.0") is False


def test_classify_blocking_pre_pr_zombie_no_active_milestone(tmp_path):
    store = _make_store(tmp_path)
    a = Anomaly(
        kind="pre_pr_zombie", severity="warning", description="x", details={"issue_number": 7}
    )
    assert classify_blocking(a, store, None) is False


def test_classify_blocking_label_drift_zombie_label_on_closed(tmp_path):
    store = _make_store(tmp_path)
    a = Anomaly(
        kind="label_drift",
        severity="critical",
        description="x",
        details={"issue_number": 5, "bead_state": "closed", "has_label": True},
    )
    assert classify_blocking(a, store, "v1.0") is True


def test_classify_blocking_label_drift_missing_label_not_blocking(tmp_path):
    store = _make_store(tmp_path)
    a = Anomaly(
        kind="label_drift",
        severity="warning",
        description="x",
        details={"issue_number": 5, "bead_state": "claimed", "has_label": False},
    )
    assert classify_blocking(a, store, "v1.0") is False


# ---------------------------------------------------------------------------
# _get_active_milestone
# ---------------------------------------------------------------------------


def test_get_active_milestone_no_campaign(tmp_path):
    store = _make_store(tmp_path)
    assert _get_active_milestone(store) is None


def test_get_active_milestone_finds_first_non_shipped(tmp_path):
    from brimstone.beads import CampaignBead

    store = _make_store(tmp_path)
    campaign = CampaignBead(
        v=1,
        repo="owner/repo",
        milestones=["v1.0", "v2.0", "v3.0"],
        current_index=0,
        statuses={"v1.0": "shipped", "v2.0": "implementing", "v3.0": "pending"},
    )
    store.write_campaign_bead(campaign)
    assert _get_active_milestone(store) == "v2.0"


# ---------------------------------------------------------------------------
# check_label_drift
# ---------------------------------------------------------------------------


def test_check_label_drift_clean(tmp_path):
    store = _make_store(tmp_path)
    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result("[]")
        anomalies = check_label_drift(store, "owner/repo")
    assert anomalies == []


def test_check_label_drift_claimed_missing_label(tmp_path):
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
    store = _make_store(tmp_path)
    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result(json.dumps([{"number": 42}]))
        anomalies = check_label_drift(store, "owner/repo")

    assert len(anomalies) == 1
    assert anomalies[0].kind == "label_drift"
    assert anomalies[0].details["issue_number"] == 42
    assert anomalies[0].details["bead_state"] is None


def test_check_label_drift_closed_bead_with_label_is_critical(tmp_path):
    store = _make_store(tmp_path)
    bead = _make_work_bead(7, state="closed")
    store.write_work_bead(bead)

    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result(json.dumps([{"number": 7}]))
        anomalies = check_label_drift(store, "owner/repo")

    assert len(anomalies) == 1
    assert anomalies[0].severity == "critical"


def test_check_label_drift_clean_claimed_with_label(tmp_path):
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
    store = _make_store(tmp_path)
    store.write_work_bead(_make_work_bead(1))
    assert check_dep_integrity(store) == []


def test_check_dep_integrity_phantom_dep(tmp_path):
    store = _make_store(tmp_path)
    bead = _make_work_bead(2, blocked_by=[999])
    store.write_work_bead(bead)

    anomalies = check_dep_integrity(store)
    assert len(anomalies) == 1
    assert anomalies[0].kind == "phantom_dep"
    assert anomalies[0].details["phantom_dep"] == 999


def test_check_dep_integrity_cycle(tmp_path):
    store = _make_store(tmp_path)
    store.write_work_bead(_make_work_bead(10, blocked_by=[11]))
    store.write_work_bead(_make_work_bead(11, blocked_by=[10]))

    anomalies = check_dep_integrity(store)
    cycle_anomalies = [a for a in anomalies if a.kind == "dep_cycle"]
    assert len(cycle_anomalies) >= 1


def test_check_dep_integrity_closed_phantom_skipped(tmp_path):
    store = _make_store(tmp_path)
    bead = _make_work_bead(3, state="closed", blocked_by=[999])
    store.write_work_bead(bead)

    anomalies = [a for a in check_dep_integrity(store) if a.kind == "phantom_dep"]
    assert anomalies == []


# ---------------------------------------------------------------------------
# check_state_regressions
# ---------------------------------------------------------------------------


def test_check_state_regressions_clean(tmp_path):
    store = _make_store(tmp_path)
    bead = _make_work_bead(1)
    store.write_work_bead(bead)
    store.append_event("work", "1", None, "open")
    store.append_event("work", "1", "open", "claimed")
    store.append_event("work", "1", "claimed", "merge_ready")
    store.append_event("work", "1", "merge_ready", "closed")

    assert check_state_regressions(store) == []


def test_check_state_regressions_bad_transition(tmp_path):
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
    store = _make_store(tmp_path)
    bead = _make_work_bead(5, state="merge_ready")
    store.write_work_bead(bead)

    anomalies = check_orphaned_merge(store)
    assert len(anomalies) == 1
    assert anomalies[0].kind == "orphaned_merge"
    assert anomalies[0].details["issue_number"] == 5


def test_check_orphaned_merge_no_merge_ready(tmp_path):
    store = _make_store(tmp_path)
    store.write_work_bead(_make_work_bead(1, state="open"))
    assert check_orphaned_merge(store) == []


# ---------------------------------------------------------------------------
# check_pre_pr_zombies
# ---------------------------------------------------------------------------


def test_check_pre_pr_zombies_no_claimed(tmp_path):
    store = _make_store(tmp_path)
    assert check_pre_pr_zombies(store) == []


def test_check_pre_pr_zombies_fresh_claimed(tmp_path):
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
# Inline repair: _inline_repair_label_drift
# ---------------------------------------------------------------------------


def test_inline_repair_label_drift_add_label(tmp_path):
    """Missing label → gh issue edit --add-label called."""
    anomaly = Anomaly(
        kind="label_drift",
        severity="warning",
        description="x",
        details={"issue_number": 5, "bead_state": "claimed", "has_label": False},
    )
    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result("", returncode=0)
        result = _inline_repair_label_drift(anomaly, "owner/repo")

    assert result is True
    call_args = mock_gh.call_args[0][0]
    assert "--add-label" in call_args


def test_inline_repair_label_drift_remove_label(tmp_path):
    """Stale label on closed bead → gh issue edit --remove-label called."""
    anomaly = Anomaly(
        kind="label_drift",
        severity="critical",
        description="x",
        details={"issue_number": 7, "bead_state": "closed", "has_label": True},
    )
    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result("", returncode=0)
        result = _inline_repair_label_drift(anomaly, "owner/repo")

    assert result is True
    call_args = mock_gh.call_args[0][0]
    assert "--remove-label" in call_args


def test_inline_repair_label_drift_gh_failure(tmp_path):
    anomaly = Anomaly(
        kind="label_drift",
        severity="warning",
        description="x",
        details={"issue_number": 5, "bead_state": "claimed", "has_label": False},
    )
    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result("", returncode=1)
        result = _inline_repair_label_drift(anomaly, "owner/repo")

    assert result is False


# ---------------------------------------------------------------------------
# Inline repair: _inline_repair_orphaned_merge
# ---------------------------------------------------------------------------


def test_inline_repair_orphaned_merge_inserts_entry(tmp_path):
    store = _make_store(tmp_path)
    bead = _make_work_bead(5, state="merge_ready", pr_id="pr-100")
    store.write_work_bead(bead)

    anomaly = Anomaly(
        kind="orphaned_merge",
        severity="warning",
        description="x",
        details={"issue_number": 5, "branch": "5-branch"},
    )
    result = _inline_repair_orphaned_merge(anomaly, store)

    assert result is True
    queue = store.read_merge_queue()
    assert any(e.issue_number == 5 for e in queue.queue)


def test_inline_repair_orphaned_merge_no_bead(tmp_path):
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="orphaned_merge",
        severity="warning",
        description="x",
        details={"issue_number": 99, "branch": "99-branch"},
    )
    result = _inline_repair_orphaned_merge(anomaly, store)
    assert result is False


def test_inline_repair_orphaned_merge_already_queued(tmp_path):
    store = _make_store(tmp_path)
    bead = _make_work_bead(5, state="merge_ready", pr_id="pr-100")
    store.write_work_bead(bead)
    queue = MergeQueue(v=BEAD_SCHEMA_VERSION)
    queue.queue.append(
        MergeQueueEntry(
            pr_number=100,
            issue_number=5,
            branch="5-branch",
            enqueued_at="2024-01-01T00:00:00+00:00",
        )
    )
    store.write_merge_queue(queue)

    anomaly = Anomaly(
        kind="orphaned_merge",
        severity="warning",
        description="x",
        details={"issue_number": 5, "branch": "5-branch"},
    )
    result = _inline_repair_orphaned_merge(anomaly, store)
    assert result is True  # idempotent — entry already present


# ---------------------------------------------------------------------------
# _apply_inline_repair dispatch
# ---------------------------------------------------------------------------


def test_apply_inline_repair_routes_label_drift(tmp_path):
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="label_drift",
        severity="warning",
        description="x",
        details={"issue_number": 5, "has_label": False},
    )
    with patch("brimstone.monitor._inline_repair_label_drift", return_value=True) as mock_fn:
        result = _apply_inline_repair(anomaly, store, "owner/repo")
    mock_fn.assert_called_once()
    assert result is True


def test_apply_inline_repair_routes_orphaned_merge(tmp_path):
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="orphaned_merge",
        severity="warning",
        description="x",
        details={"issue_number": 5},
    )
    with patch("brimstone.monitor._inline_repair_orphaned_merge", return_value=True) as mock_fn:
        result = _apply_inline_repair(anomaly, store, "owner/repo")
    mock_fn.assert_called_once()
    assert result is True


def test_apply_inline_repair_unknown_kind_returns_false(tmp_path):
    store = _make_store(tmp_path)
    anomaly = Anomaly(kind="dep_cycle", severity="critical", description="x")
    result = _apply_inline_repair(anomaly, store, "owner/repo")
    assert result is False


# ---------------------------------------------------------------------------
# process_anomalies — new bead-based behaviour
# ---------------------------------------------------------------------------


def test_process_anomalies_dry_run(tmp_path, capsys):
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="dep_cycle",
        severity="critical",
        description="test anomaly",
        details={"cycle": [1, 2]},
    )
    with patch("brimstone.monitor._file_repair_issue") as mock_file:
        urls = process_anomalies([anomaly], store, "owner/repo", dry_run=True)

    mock_file.assert_not_called()
    assert urls == []
    captured = capsys.readouterr()
    assert "dep_cycle" in captured.out


def test_process_anomalies_creates_anomaly_bead(tmp_path):
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="dep_cycle",
        severity="critical",
        description="cycle",
        details={"cycle": [1, 2]},
    )
    with patch("brimstone.monitor._file_repair_issue", return_value="https://github.com/issues/1"):
        process_anomalies([anomaly], store, "owner/repo")

    aid = _anomaly_id(anomaly)
    bead = store.read_anomaly_bead(aid)
    assert bead is not None
    assert bead.kind == "dep_cycle"
    assert bead.repair_tier == "probe"
    assert bead.source_repo == "owner/repo"


def test_process_anomalies_dedup_by_anomaly_bead(tmp_path):
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="dep_cycle",
        severity="critical",
        description="cycle",
        details={"cycle": [1, 2]},
    )
    patch_target = "brimstone.monitor._file_repair_issue"
    with patch(patch_target, return_value="https://github.com/issues/1") as mock_file:
        process_anomalies([anomaly], store, "owner/repo")
        process_anomalies([anomaly], store, "owner/repo")

    # Issue should only be filed once
    assert mock_file.call_count == 1


def test_process_anomalies_inline_applies_repair(tmp_path):
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="label_drift",
        severity="warning",
        description="missing label",
        details={"issue_number": 5, "bead_state": "claimed", "has_label": False},
    )
    with patch("brimstone.monitor._apply_inline_repair", return_value=True) as mock_repair:
        with patch("brimstone.monitor._file_repair_issue") as mock_file:
            process_anomalies([anomaly], store, "owner/repo")

    mock_repair.assert_called_once()
    mock_file.assert_not_called()  # inline: no issue filed on success


def test_process_anomalies_inline_escalates_after_max_failures(tmp_path):
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="label_drift",
        severity="warning",
        description="missing label",
        details={"issue_number": 5, "bead_state": "claimed", "has_label": False},
    )
    with patch("brimstone.monitor._apply_inline_repair", return_value=False):
        issue_url = "https://github.com/issues/99"
        with patch("brimstone.monitor._file_repair_issue", return_value=issue_url) as mock_file:
            for _ in range(INLINE_REPAIR_MAX_ATTEMPTS):
                process_anomalies([anomaly], store, "owner/repo")

    # Should have escalated and filed exactly once
    mock_file.assert_called_once()


def test_process_anomalies_cleanup_sweep_resolves_gone_anomaly(tmp_path):
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="dep_cycle",
        severity="critical",
        description="cycle",
        details={"cycle": [1, 2]},
    )
    aid = _anomaly_id(anomaly)

    # First pass: bead created
    with patch("brimstone.monitor._file_repair_issue", return_value="https://github.com/issues/1"):
        process_anomalies([anomaly], store, "owner/repo")

    # Second pass: anomaly gone (empty list)
    with patch("brimstone.monitor._file_repair_issue"):
        process_anomalies([], store, "owner/repo")

    bead = store.read_anomaly_bead(aid)
    assert bead is not None
    assert bead.state == "repaired"
    assert bead.resolved_at is not None


def test_process_anomalies_skips_terminal_beads(tmp_path):
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="dep_cycle",
        severity="critical",
        description="cycle",
        details={"cycle": [1, 2]},
    )
    aid = _anomaly_id(anomaly)

    # Pre-write a repaired bead
    existing = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id=aid,
        source_repo="owner/repo",
        kind="dep_cycle",
        severity="critical",
        state="repaired",
        detected_at="2024-01-01T00:00:00+00:00",
    )
    store.write_anomaly_bead(existing)

    with patch("brimstone.monitor._file_repair_issue") as mock_file:
        process_anomalies([anomaly], store, "owner/repo")

    mock_file.assert_not_called()


def test_process_anomalies_probe_files_issue(tmp_path):
    store = _make_store(tmp_path)
    anomaly = Anomaly(
        kind="phantom_dep",
        severity="critical",
        description="phantom",
        details={"issue_number": 10, "phantom_dep": 999},
    )
    url = "https://github.com/owner/repo/issues/42"
    with patch("brimstone.monitor._file_repair_issue", return_value=url) as mock_file:
        urls = process_anomalies([anomaly], store, "owner/repo")

    assert urls == [url]
    mock_file.assert_called_once()
    aid = _anomaly_id(anomaly)
    bead = store.read_anomaly_bead(aid)
    assert bead.gh_issue_number == 42


# ---------------------------------------------------------------------------
# run_all_detectors
# ---------------------------------------------------------------------------


def test_run_all_detectors_returns_list(tmp_path):
    store = _make_store(tmp_path)
    with patch("brimstone.monitor._gh") as mock_gh:
        mock_gh.return_value = _make_gh_result("[]")
        anomalies = run_all_detectors(store, "owner/repo")
    assert isinstance(anomalies, list)


def test_run_all_detectors_detector_error_caught(tmp_path):
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
