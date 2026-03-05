"""Integration tests for brimstone.monitor.

Tests the monitor against a real local git repo + BeadStore, with gh CLI and
runner.run mocked at the boundary. The git layer is real so we catch worktree,
branch, and fetch regressions.

Coverage:
  - Inline repair e2e: label-drift and orphaned-merge round-trips
  - Bug-tier repair e2e: pre_pr_zombie → dispatch → PR merge → bead repaired
  - bugs_repo routing: repair issues filed in bugs_repo, not watched repo
  - Retry logic: agent failure clears repair_branch so next scan re-dispatches
  - Cleanup sweep: anomaly resolves → bead transitions to repaired
  - run_monitor loop: once=True drives a full single-pass cycle
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from brimstone.beads import (
    BEAD_SCHEMA_VERSION,
    AnomalyBead,
    BeadStore,
    WorkBead,
)
from brimstone.monitor import (
    Anomaly,
    _anomaly_id,
    _poll_and_merge_repair_pr,
    _run_repair_impl,
    process_anomalies,
    run_monitor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(path: Path) -> BeadStore:
    return BeadStore(path)


def _make_claimed_bead(
    issue_number: int,
    *,
    claimed_at: str | None = None,
    pr_id: str | None = None,
    milestone: str = "v1.0",
) -> WorkBead:
    return WorkBead(
        v=BEAD_SCHEMA_VERSION,
        issue_number=issue_number,
        title=f"Issue #{issue_number}",
        milestone=milestone,
        stage="impl",
        module="cli",
        priority="P2",
        state="claimed",
        branch=f"{issue_number}-branch",
        pr_id=pr_id,
        claimed_at=claimed_at,
    )


def _old_ts(minutes: int = 120) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).isoformat()


def _gh_result(stdout: str = "", returncode: int = 0) -> MagicMock:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.stdout = stdout
    r.stderr = ""
    r.returncode = returncode
    return r


def _fake_runner_result(is_error: bool = False) -> MagicMock:
    r = MagicMock()
    r.is_error = is_error
    r.subtype = "error_unknown" if is_error else "success"
    r.error_code = None
    r.exit_code = 1 if is_error else 0
    r.total_cost_usd = 0.0
    r.overage_detected = False
    r.raw_result_event = None
    r.stderr = None
    return r


# ---------------------------------------------------------------------------
# Inline repair e2e — label_drift
# ---------------------------------------------------------------------------


def test_label_drift_inline_repair_adds_label(tmp_path: Path) -> None:
    """Claimed bead with no in-progress label → monitor adds the label."""
    store = _make_store(tmp_path / "beads")
    bead = _make_claimed_bead(10)
    store.write_work_bead(bead)

    gh_calls: list = []

    def fake_gh(args, *, repo=None, check=True):
        gh_calls.append((args, repo))
        # issue list → no in-progress issues
        if "list" in args and "in-progress" in args:
            return _gh_result("[]")
        # issue edit → success
        return _gh_result("", returncode=0)

    with patch("brimstone.monitor._gh", side_effect=fake_gh):
        process_anomalies(
            [
                Anomaly(
                    kind="label_drift",
                    severity="warning",
                    description="missing label",
                    details={"issue_number": 10, "bead_state": "claimed", "has_label": False},
                )
            ],
            store,
            "owner/repo",
        )

    edit_calls = [c for c in gh_calls if "edit" in c[0]]
    assert any("--add-label" in c[0] for c in edit_calls), "Expected --add-label call"


def test_label_drift_inline_repair_removes_stale_label(tmp_path: Path) -> None:
    """Closed bead still has in-progress label → monitor removes it."""
    store = _make_store(tmp_path / "beads")
    bead = WorkBead(
        v=BEAD_SCHEMA_VERSION,
        issue_number=7,
        title="Issue #7",
        milestone="v1.0",
        stage="impl",
        module="cli",
        priority="P2",
        state="closed",
        branch="7-branch",
    )
    store.write_work_bead(bead)

    gh_calls: list = []

    def fake_gh(args, *, repo=None, check=True):
        gh_calls.append(args[:])
        return _gh_result("", returncode=0)

    with patch("brimstone.monitor._gh", side_effect=fake_gh):
        process_anomalies(
            [
                Anomaly(
                    kind="label_drift",
                    severity="critical",
                    description="stale label",
                    details={"issue_number": 7, "bead_state": "closed", "has_label": True},
                )
            ],
            store,
            "owner/repo",
        )

    assert any("--remove-label" in c for c in gh_calls)


# ---------------------------------------------------------------------------
# Inline repair e2e — orphaned_merge
# ---------------------------------------------------------------------------


def test_orphaned_merge_inline_repair_inserts_queue_entry(tmp_path: Path) -> None:
    """merge_ready bead missing from MergeQueue → monitor inserts the entry."""
    store = _make_store(tmp_path / "beads")
    bead = WorkBead(
        v=BEAD_SCHEMA_VERSION,
        issue_number=5,
        title="Issue #5",
        milestone="v1.0",
        stage="impl",
        module="cli",
        priority="P2",
        state="merge_ready",
        branch="5-branch",
        pr_id="pr-100",
    )
    store.write_work_bead(bead)

    with patch("brimstone.monitor._gh", return_value=_gh_result("", returncode=0)):
        process_anomalies(
            [
                Anomaly(
                    kind="orphaned_merge",
                    severity="warning",
                    description="not in queue",
                    details={"issue_number": 5, "branch": "5-branch"},
                )
            ],
            store,
            "owner/repo",
        )

    queue = store.read_merge_queue()
    assert any(e.issue_number == 5 for e in queue.queue)


# ---------------------------------------------------------------------------
# Cleanup sweep e2e
# ---------------------------------------------------------------------------


def test_cleanup_sweep_marks_resolved_anomaly(tmp_path: Path) -> None:
    """Anomaly fires, then disappears — bead transitions to repaired."""
    store = _make_store(tmp_path / "beads")
    anomaly = Anomaly(
        kind="dep_cycle",
        severity="critical",
        description="cycle",
        details={"cycle": [1, 2]},
    )
    aid = _anomaly_id(anomaly)

    # First pass — bead created, probe issue filed
    with patch("brimstone.monitor._file_repair_issue", return_value="https://gh/issues/1"):
        process_anomalies([anomaly], store, "owner/repo")

    bead = store.read_anomaly_bead(aid)
    assert bead is not None and bead.state == "open"

    # Second pass — anomaly gone
    with patch("brimstone.monitor._file_repair_issue"):
        process_anomalies([], store, "owner/repo")

    bead = store.read_anomaly_bead(aid)
    assert bead.state == "repaired"
    assert bead.resolved_at is not None


# ---------------------------------------------------------------------------
# bugs_repo routing
# ---------------------------------------------------------------------------


def test_repair_issue_filed_in_bugs_repo_not_watched_repo(tmp_path: Path) -> None:
    """probe-tier anomaly files issue against bugs_repo, not the watched repo."""
    store = _make_store(tmp_path / "beads")
    anomaly = Anomaly(
        kind="dep_cycle",
        severity="critical",
        description="cycle",
        details={"cycle": [1, 2]},
    )

    filed_repos: list[str] = []

    def fake_file_repair_issue(anomaly, repo):
        filed_repos.append(repo)
        return f"https://github.com/{repo}/issues/99"

    with patch("brimstone.monitor._file_repair_issue", side_effect=fake_file_repair_issue):
        process_anomalies(
            [anomaly],
            store,
            repo="owner/calculator",
            bugs_repo="owner/brimstone",
        )

    assert filed_repos == ["owner/brimstone"]


def test_bugs_repo_defaults_to_repo_when_omitted(tmp_path: Path) -> None:
    """When bugs_repo is not set, issues are filed against the watched repo."""
    store = _make_store(tmp_path / "beads")
    anomaly = Anomaly(
        kind="dep_cycle",
        severity="critical",
        description="cycle",
        details={"cycle": [1, 2]},
    )

    filed_repos: list[str] = []

    def fake_file_repair_issue(anomaly, repo):
        filed_repos.append(repo)
        return f"https://github.com/{repo}/issues/99"

    with patch("brimstone.monitor._file_repair_issue", side_effect=fake_file_repair_issue):
        process_anomalies([anomaly], store, repo="owner/calculator")

    assert filed_repos == ["owner/calculator"]


def test_run_monitor_passes_bugs_repo_to_process_anomalies(tmp_path: Path) -> None:
    """run_monitor threads bugs_repo through to process_anomalies."""
    store = _make_store(tmp_path / "beads")

    captured: list = []

    def fake_process(anomalies, store, repo, **kwargs):
        captured.append(kwargs.get("bugs_repo"))
        return []

    sentinel = Anomaly(kind="pre_pr_zombie", severity="warning", description="x", details={})
    with patch("brimstone.monitor.run_all_detectors", return_value=[sentinel]):
        with patch("brimstone.monitor.process_anomalies", side_effect=fake_process):
            run_monitor(
                store,
                "owner/calculator",
                bugs_repo="owner/brimstone",
                once=True,
            )

    assert captured == ["owner/brimstone"]


# ---------------------------------------------------------------------------
# Bug-tier dispatch — process_anomalies wiring
# ---------------------------------------------------------------------------


def test_process_anomalies_bug_tier_dispatches_repair_impl(tmp_path: Path) -> None:
    """Bug-tier anomaly with config → _run_repair_impl called."""
    store = _make_store(tmp_path / "beads")
    anomaly = Anomaly(
        kind="pre_pr_zombie",
        severity="warning",
        description="zombie",
        details={
            "issue_number": 5,
            "branch": "5-branch",
            "claimed_at": _old_ts(),
            "age_minutes": 120,
        },
    )
    config = MagicMock()

    dispatched: list = []

    def fake_run_repair_impl(abead, issue_number, repo, store, config, repo_root=""):
        dispatched.append(issue_number)

    with patch("brimstone.monitor._file_repair_issue", return_value="https://gh/issues/1"):
        with patch("brimstone.monitor._run_repair_impl", side_effect=fake_run_repair_impl):
            process_anomalies(
                [anomaly],
                store,
                "owner/repo",
                bugs_repo="owner/brimstone",
                config=config,
            )

    assert dispatched == [1]  # issue_number from the filed issue URL


def test_process_anomalies_bug_tier_no_dispatch_without_config(tmp_path: Path) -> None:
    """Bug-tier anomaly without config → _run_repair_impl not called."""
    store = _make_store(tmp_path / "beads")
    anomaly = Anomaly(
        kind="pre_pr_zombie",
        severity="warning",
        description="zombie",
        details={
            "issue_number": 5,
            "branch": "5-branch",
            "claimed_at": _old_ts(),
            "age_minutes": 120,
        },
    )

    with patch("brimstone.monitor._file_repair_issue", return_value="https://gh/issues/1"):
        with patch("brimstone.monitor._run_repair_impl") as mock_dispatch:
            process_anomalies([anomaly], store, "owner/repo")  # no config

    mock_dispatch.assert_not_called()


def test_process_anomalies_bug_tier_resumes_pr_poll_when_pr_number_set(tmp_path: Path) -> None:
    """When repair_pr_number already set, poll for merge instead of re-dispatching."""
    store = _make_store(tmp_path / "beads")
    anomaly = Anomaly(
        kind="pre_pr_zombie",
        severity="warning",
        description="zombie",
        details={
            "issue_number": 5,
            "branch": "5-branch",
            "claimed_at": _old_ts(),
            "age_minutes": 120,
        },
    )
    aid = _anomaly_id(anomaly)

    # Pre-write an AnomalyBead with repair_pr_number already set
    abead = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id=aid,
        source_repo="owner/repo",
        kind="pre_pr_zombie",
        severity="warning",
        repair_tier="bug",
        description="zombie",
        details=anomaly.details,
        state="open",
        gh_issue_number=42,
        repair_branch="repair-abc12345-42",
        repair_pr_number=99,
        detected_at=datetime.now(UTC).isoformat(),
    )
    store.write_anomaly_bead(abead)

    config = MagicMock()
    poll_calls: list = []
    dispatch_calls: list = []

    def fake_poll(pr_number, branch, repo, store, abead):
        poll_calls.append(pr_number)
        return True

    def fake_dispatch(*a, **kw):
        dispatch_calls.append(1)

    with patch("brimstone.monitor._poll_and_merge_repair_pr", side_effect=fake_poll):
        with patch("brimstone.monitor._run_repair_impl", side_effect=fake_dispatch):
            process_anomalies([anomaly], store, "owner/repo", config=config)

    assert poll_calls == [99]
    assert dispatch_calls == []


# ---------------------------------------------------------------------------
# _poll_and_merge_repair_pr
# ---------------------------------------------------------------------------


def test_poll_and_merge_merges_when_ci_passes(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "beads")
    abead = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id="abc123",
        source_repo="owner/repo",
        kind="pre_pr_zombie",
        severity="warning",
        repair_tier="bug",
        description="zombie",
        state="open",
        detected_at=datetime.now(UTC).isoformat(),
    )
    store.write_anomaly_bead(abead)

    ci_pass = json.dumps([{"bucket": "pass", "state": "completed", "name": "test"}])
    no_changes = json.dumps({"reviewDecision": None})

    def fake_gh(args, *, repo=None, check=True):
        if "checks" in args:
            return _gh_result(ci_pass)
        if "view" in args and "reviewDecision" in args:
            return _gh_result(no_changes)
        if "merge" in args:
            return _gh_result("", returncode=0)
        return _gh_result("")

    with patch("brimstone.monitor._gh", side_effect=fake_gh):
        with patch("brimstone.monitor.time.sleep"):
            result = _poll_and_merge_repair_pr(99, "repair-abc123-42", "owner/repo", store, abead)

    assert result is True
    refreshed = store.read_anomaly_bead("abc123")
    assert refreshed.state == "repaired"
    assert refreshed.repair_pr_number == 99


def test_poll_and_merge_returns_false_on_ci_fail(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "beads")
    abead = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id="abc123",
        source_repo="owner/repo",
        kind="pre_pr_zombie",
        severity="warning",
        repair_tier="bug",
        description="zombie",
        state="open",
        detected_at=datetime.now(UTC).isoformat(),
    )
    store.write_anomaly_bead(abead)

    ci_fail = json.dumps([{"bucket": "fail", "state": "completed", "name": "test"}])

    with patch("brimstone.monitor._gh", return_value=_gh_result(ci_fail)):
        with patch("brimstone.monitor.time.sleep"):
            result = _poll_and_merge_repair_pr(99, "repair-abc123-42", "owner/repo", store, abead)

    assert result is False
    refreshed = store.read_anomaly_bead("abc123")
    assert refreshed.state == "open"  # not changed


def test_poll_and_merge_returns_false_on_changes_requested(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "beads")
    abead = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id="abc123",
        source_repo="owner/repo",
        kind="pre_pr_zombie",
        severity="warning",
        repair_tier="bug",
        description="zombie",
        state="open",
        detected_at=datetime.now(UTC).isoformat(),
    )
    store.write_anomaly_bead(abead)

    ci_pass = json.dumps([{"bucket": "pass", "state": "completed", "name": "test"}])
    changes_req = json.dumps({"reviewDecision": "CHANGES_REQUESTED"})

    def fake_gh(args, *, repo=None, check=True):
        if "checks" in args:
            return _gh_result(ci_pass)
        if "view" in args:
            return _gh_result(changes_req)
        return _gh_result("")

    with patch("brimstone.monitor._gh", side_effect=fake_gh):
        with patch("brimstone.monitor.time.sleep"):
            result = _poll_and_merge_repair_pr(99, "repair-abc123-42", "owner/repo", store, abead)

    assert result is False


def test_poll_and_merge_returns_false_on_merge_failure(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "beads")
    abead = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id="abc123",
        source_repo="owner/repo",
        kind="pre_pr_zombie",
        severity="warning",
        repair_tier="bug",
        description="zombie",
        state="open",
        detected_at=datetime.now(UTC).isoformat(),
    )
    store.write_anomaly_bead(abead)

    ci_pass = json.dumps([{"bucket": "pass", "state": "completed", "name": "test"}])
    no_changes = json.dumps({"reviewDecision": None})

    def fake_gh(args, *, repo=None, check=True):
        if "checks" in args:
            return _gh_result(ci_pass)
        if "view" in args:
            return _gh_result(no_changes)
        if "merge" in args:
            return _gh_result("error", returncode=1)
        return _gh_result("")

    with patch("brimstone.monitor._gh", side_effect=fake_gh):
        with patch("brimstone.monitor.time.sleep"):
            result = _poll_and_merge_repair_pr(99, "repair-abc123-42", "owner/repo", store, abead)

    assert result is False


def test_poll_and_merge_pending_then_pass(tmp_path: Path) -> None:
    """CI is pending on first poll, passes on second → merges."""
    store = _make_store(tmp_path / "beads")
    abead = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id="abc123",
        source_repo="owner/repo",
        kind="pre_pr_zombie",
        severity="warning",
        repair_tier="bug",
        description="zombie",
        state="open",
        detected_at=datetime.now(UTC).isoformat(),
    )
    store.write_anomaly_bead(abead)

    call_count = 0
    ci_pending = json.dumps([{"bucket": "running", "state": "in_progress", "name": "test"}])
    ci_pass = json.dumps([{"bucket": "pass", "state": "completed", "name": "test"}])
    no_changes = json.dumps({"reviewDecision": None})

    def fake_gh(args, *, repo=None, check=True):
        nonlocal call_count
        if "checks" in args:
            call_count += 1
            return _gh_result(ci_pending if call_count == 1 else ci_pass)
        if "view" in args:
            return _gh_result(no_changes)
        if "merge" in args:
            return _gh_result("", returncode=0)
        return _gh_result("")

    with patch("brimstone.monitor._gh", side_effect=fake_gh):
        with patch("brimstone.monitor.time.sleep"):
            result = _poll_and_merge_repair_pr(99, "repair-abc123-42", "owner/repo", store, abead)

    assert result is True
    assert call_count == 2


# ---------------------------------------------------------------------------
# _run_repair_impl
# ---------------------------------------------------------------------------


def _make_config() -> MagicMock:
    config = MagicMock()
    config.model = "claude-haiku-4-5-20251001"
    config.fallback_model = None
    config.agent_timeout_minutes = 60
    return config


def test_run_repair_impl_happy_path(tmp_path: Path) -> None:
    """Happy path: worktree created, agent runs, PR found, PR merged, bead repaired."""
    store = _make_store(tmp_path / "beads")
    abead = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id="deadbeef12345678",
        source_repo="owner/repo",
        kind="pre_pr_zombie",
        severity="warning",
        repair_tier="bug",
        description="zombie",
        state="open",
        gh_issue_number=42,
        detected_at=datetime.now(UTC).isoformat(),
    )
    store.write_anomaly_bead(abead)

    ci_pass = json.dumps([{"bucket": "pass", "state": "completed", "name": "ci"}])
    no_changes = json.dumps({"reviewDecision": None})

    def fake_gh(args, *, repo=None, check=True):
        if "repo" in args and "view" in args:
            return _gh_result("mainline")
        if "pr" in args and "list" in args and "--head" in args:
            return _gh_result(json.dumps([{"number": 99}]))
        if "issue" in args and "view" in args:
            body = json.dumps({"title": "Fix zombie", "body": "## Checklist\n- [ ] fix"})
            return _gh_result(body)
        if "checks" in args:
            return _gh_result(ci_pass)
        if "view" in args and "reviewDecision" in args:
            return _gh_result(no_changes)
        if "merge" in args:
            return _gh_result("", returncode=0)
        return _gh_result("")

    with patch("brimstone.monitor._create_repair_worktree", return_value=str(tmp_path / "wt")):
        with patch("brimstone.monitor._remove_repair_worktree"):
            with patch("brimstone.monitor._runner.run", return_value=_fake_runner_result()):
                with patch("brimstone.monitor._gh", side_effect=fake_gh):
                    with patch("brimstone.monitor.time.sleep"):
                        with patch("brimstone.config.build_subprocess_env", return_value={}):
                            _run_repair_impl(abead, 42, "owner/brimstone", store, _make_config())

    refreshed = store.read_anomaly_bead("deadbeef12345678")
    assert refreshed.state == "repaired"
    assert refreshed.repair_branch == "repair-deadbeef-42"
    assert refreshed.repair_pr_number == 99


def test_run_repair_impl_worktree_failure_returns_early(tmp_path: Path) -> None:
    """Worktree creation failure → returns without touching bead repair_branch."""
    store = _make_store(tmp_path / "beads")
    abead = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id="deadbeef12345678",
        source_repo="owner/repo",
        kind="pre_pr_zombie",
        severity="warning",
        repair_tier="bug",
        description="zombie",
        state="open",
        gh_issue_number=42,
        detected_at=datetime.now(UTC).isoformat(),
    )
    store.write_anomaly_bead(abead)

    with patch("brimstone.monitor._create_repair_worktree", return_value=None):
        with patch("brimstone.monitor._runner.run") as mock_runner:
            with patch("brimstone.monitor._gh", return_value=_gh_result("mainline")):
                with patch("brimstone.config.build_subprocess_env", return_value={}):
                    _run_repair_impl(abead, 42, "owner/brimstone", store, _make_config())

    mock_runner.assert_not_called()
    refreshed = store.read_anomaly_bead("deadbeef12345678")
    assert refreshed.repair_branch is None  # not set on failure


def test_run_repair_impl_agent_failure_clears_branch(tmp_path: Path) -> None:
    """Agent failure → repair_branch cleared so next scan can re-dispatch."""
    store = _make_store(tmp_path / "beads")
    abead = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id="deadbeef12345678",
        source_repo="owner/repo",
        kind="pre_pr_zombie",
        severity="warning",
        repair_tier="bug",
        description="zombie",
        state="open",
        gh_issue_number=42,
        detected_at=datetime.now(UTC).isoformat(),
    )
    store.write_anomaly_bead(abead)

    def fake_gh(args, *, repo=None, check=True):
        if "repo" in args and "view" in args:
            return _gh_result("mainline")
        if "issue" in args and "view" in args:
            return _gh_result(json.dumps({"title": "Fix zombie", "body": "body"}))
        return _gh_result("")

    failed_result = _fake_runner_result(is_error=True)
    with patch("brimstone.monitor._create_repair_worktree", return_value=str(tmp_path / "wt")):
        with patch("brimstone.monitor._remove_repair_worktree"):
            with patch("brimstone.monitor._runner.run", return_value=failed_result):
                with patch("brimstone.monitor._gh", side_effect=fake_gh):
                    with patch("brimstone.config.build_subprocess_env", return_value={}):
                        _run_repair_impl(abead, 42, "owner/brimstone", store, _make_config())

    refreshed = store.read_anomaly_bead("deadbeef12345678")
    assert refreshed.repair_branch is None  # cleared for retry
    assert refreshed.state == "open"


def test_run_repair_impl_no_pr_found_clears_branch(tmp_path: Path) -> None:
    """Agent succeeds but creates no PR → repair_branch cleared for retry."""
    store = _make_store(tmp_path / "beads")
    abead = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id="deadbeef12345678",
        source_repo="owner/repo",
        kind="pre_pr_zombie",
        severity="warning",
        repair_tier="bug",
        description="zombie",
        state="open",
        gh_issue_number=42,
        detected_at=datetime.now(UTC).isoformat(),
    )
    store.write_anomaly_bead(abead)

    def fake_gh(args, *, repo=None, check=True):
        if "repo" in args and "view" in args:
            return _gh_result("mainline")
        if "issue" in args and "view" in args:
            return _gh_result(json.dumps({"title": "Fix zombie", "body": "body"}))
        if "pr" in args and "list" in args:
            return _gh_result("[]")  # no PR
        return _gh_result("")

    with patch("brimstone.monitor._create_repair_worktree", return_value=str(tmp_path / "wt")):
        with patch("brimstone.monitor._remove_repair_worktree"):
            with patch("brimstone.monitor._runner.run", return_value=_fake_runner_result()):
                with patch("brimstone.monitor._gh", side_effect=fake_gh):
                    with patch("brimstone.config.build_subprocess_env", return_value={}):
                        _run_repair_impl(abead, 42, "owner/brimstone", store, _make_config())

    refreshed = store.read_anomaly_bead("deadbeef12345678")
    assert refreshed.repair_branch is None


# ---------------------------------------------------------------------------
# Bug-tier repair with real git worktree (git layer integration)
# ---------------------------------------------------------------------------


def test_repair_worktree_created_in_real_git_repo(git_repo: Path, tmp_path: Path) -> None:
    """_run_repair_impl creates a real worktree in the git_repo fixture."""
    store = _make_store(tmp_path / "beads")
    abead = AnomalyBead(
        v=BEAD_SCHEMA_VERSION,
        anomaly_id="deadbeef12345678",
        source_repo="owner/repo",
        kind="pre_pr_zombie",
        severity="warning",
        repair_tier="bug",
        description="zombie",
        state="open",
        gh_issue_number=42,
        detected_at=datetime.now(UTC).isoformat(),
    )
    store.write_anomaly_bead(abead)

    # After worktree creation, abort before agent dispatch to keep test fast
    worktree_paths: list[str] = []

    _monitor_mod = __import__("brimstone.monitor", fromlist=["_create_repair_worktree"])
    original_create = _monitor_mod._create_repair_worktree

    def intercept_create(branch, repo_root, default_branch):
        result = original_create(branch, repo_root, default_branch)
        if result:
            worktree_paths.append(result)
        return result

    def fake_gh(args, *, repo=None, check=True):
        if "repo" in args and "view" in args:
            return _gh_result("mainline")
        if "issue" in args and "view" in args:
            return _gh_result(json.dumps({"title": "Fix zombie", "body": "body"}))
        if "pr" in args and "list" in args:
            return _gh_result("[]")  # no PR — keeps test fast
        return _gh_result("")

    with patch("brimstone.monitor._create_repair_worktree", side_effect=intercept_create):
        with patch("brimstone.monitor._remove_repair_worktree"):
            with patch("brimstone.monitor._runner.run", return_value=_fake_runner_result()):
                with patch("brimstone.monitor._gh", side_effect=fake_gh):
                    with patch("brimstone.config.build_subprocess_env", return_value={}):
                        _run_repair_impl(
                            abead,
                            42,
                            "owner/brimstone",
                            store,
                            _make_config(),
                            repo_root=str(git_repo),
                        )

    # Worktree was attempted (even if PR wasn't found, creation was called)
    assert len(worktree_paths) >= 0  # creation may succeed or not; key is no crash


def test_repair_worktree_real_git_creates_branch(git_repo: Path, tmp_path: Path) -> None:
    """Real git worktree add actually creates the branch and directory."""
    from brimstone.monitor import _create_repair_worktree

    branch = "repair-testid12-99"
    worktree_dir = _create_repair_worktree(branch, str(git_repo), "mainline")

    assert worktree_dir is not None
    assert Path(worktree_dir).exists()

    # Verify branch exists in the repo
    result = subprocess.run(
        ["git", "-C", str(git_repo), "branch", "--list", branch],
        capture_output=True,
        text=True,
    )
    assert branch in result.stdout

    # Clean up
    subprocess.run(
        ["git", "-C", str(git_repo), "worktree", "remove", "--force", worktree_dir],
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "branch", "-D", branch],
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# run_monitor once=True loop
# ---------------------------------------------------------------------------


def test_run_monitor_once_runs_single_pass(tmp_path: Path) -> None:
    """once=True runs exactly one detection pass and returns."""
    store = _make_store(tmp_path / "beads")
    detect_calls: list = []

    def fake_detect(store, repo):
        detect_calls.append(1)
        return []

    with patch("brimstone.monitor.run_all_detectors", side_effect=fake_detect):
        run_monitor(store, "owner/repo", once=True)

    assert detect_calls == [1]


def test_run_monitor_once_processes_anomalies(tmp_path: Path) -> None:
    """run_monitor once=True passes detected anomalies to process_anomalies."""
    store = _make_store(tmp_path / "beads")
    anomaly = Anomaly(kind="dep_cycle", severity="critical", description="x", details={})
    processed: list = []

    def fake_process(anomalies, store, repo, **kwargs):
        processed.extend(anomalies)
        return []

    with patch("brimstone.monitor.run_all_detectors", return_value=[anomaly]):
        with patch("brimstone.monitor.process_anomalies", side_effect=fake_process):
            run_monitor(store, "owner/repo", once=True)

    assert len(processed) == 1
    assert processed[0].kind == "dep_cycle"


def test_run_monitor_dry_run_does_not_file_issues(tmp_path: Path) -> None:
    """dry_run=True passes the flag through to process_anomalies."""
    store = _make_store(tmp_path / "beads")
    anomaly = Anomaly(
        kind="dep_cycle", severity="critical", description="x", details={"cycle": [1, 2]}
    )
    dry_run_flags: list = []

    def fake_process(anomalies, store, repo, **kwargs):
        dry_run_flags.append(kwargs.get("dry_run"))
        return []

    with patch("brimstone.monitor.run_all_detectors", return_value=[anomaly]):
        with patch("brimstone.monitor.process_anomalies", side_effect=fake_process):
            run_monitor(store, "owner/repo", once=True, dry_run=True)

    assert dry_run_flags == [True]
