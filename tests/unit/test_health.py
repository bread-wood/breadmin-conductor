"""Unit tests for src/brimstone/health.py."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brimstone.config import Config
from brimstone.health import (
    CheckResult,
    FatalHealthCheckError,
    HealthReport,
    _check_api_key,
    _check_backoff,
    _check_checkpoint_dir_writable,
    _check_default_branch,
    _check_gh_auth,
    _check_git_repo,
    _check_log_dir_writable,
    _check_open_prs,
    _check_orchestrator_lock,
    _check_orphaned_issues,
    _check_worktrees,
    acquire_orchestrator_lock,
    check_all,
    format_report,
    release_orchestrator_lock,
)
from brimstone.session import Checkpoint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path, **overrides: object) -> Config:
    """Create a minimal Config pointing all paths into tmp_path."""
    defaults: dict = {
        "anthropic_api_key": "sk-ant-test",
        "github_token": "ghp-test",
        "checkpoint_dir": tmp_path / "checkpoints",
        "log_dir": tmp_path / "logs",
    }
    defaults.update(overrides)
    return Config(**defaults)


def make_checkpoint(**overrides: object) -> Checkpoint:
    """Create a minimal Checkpoint."""
    defaults: dict = {
        "schema_version": 1,
        "run_id": "test-run-id",
        "session_id": "",
        "repo": "owner/repo",
        "default_branch": "main",
        "milestone": "MVP",
        "stage": "impl",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    defaults.update(overrides)
    return Checkpoint(**defaults)


def _subprocess_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """Build a mock subprocess.CompletedProcess."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


def _pass(name: str) -> CheckResult:
    """Shorthand: a passing CheckResult with the given name."""
    return CheckResult(name=name, status="pass", message="ok")


def _all_pass_patches() -> list:
    """Return a list of patch context managers for all 11 checks returning pass."""
    return [
        patch("brimstone.health._check_git_repo", return_value=_pass("Git repo present")),
        patch(
            "brimstone.health._check_default_branch",
            return_value=_pass("Default branch matches config"),
        ),
        patch(
            "brimstone.health._check_gh_auth",
            return_value=_pass("gh CLI authenticated"),
        ),
        patch(
            "brimstone.health._check_api_key",
            return_value=_pass("ANTHROPIC_API_KEY present"),
        ),
        patch(
            "brimstone.health._check_worktrees",
            return_value=_pass("No active worktrees"),
        ),
        patch(
            "brimstone.health._check_orphaned_issues",
            return_value=_pass("No stale in-progress issues"),
        ),
        patch(
            "brimstone.health._check_open_prs",
            return_value=_pass("Open PRs needing attention"),
        ),
        patch(
            "brimstone.health._check_backoff",
            return_value=_pass("Rate limit backoff active"),
        ),
        patch(
            "brimstone.health._check_orchestrator_lock",
            return_value=_pass("Single orchestrator guard"),
        ),
        patch(
            "brimstone.health._check_checkpoint_dir_writable",
            return_value=_pass("Checkpoint dir writable"),
        ),
        patch(
            "brimstone.health._check_log_dir_writable",
            return_value=_pass("Log dir writable"),
        ),
    ]


# ---------------------------------------------------------------------------
# CheckResult and HealthReport construction
# ---------------------------------------------------------------------------


def test_check_result_fields() -> None:
    """CheckResult stores all fields correctly."""
    r = CheckResult(
        name="Test check",
        status="pass",
        message="All good.",
        remediation=None,
    )
    assert r.name == "Test check"
    assert r.status == "pass"
    assert r.message == "All good."
    assert r.remediation is None


def test_check_result_frozen() -> None:
    """CheckResult is immutable (frozen dataclass)."""
    r = CheckResult(name="x", status="pass", message="ok")
    with pytest.raises(Exception):
        r.name = "y"  # type: ignore[misc]


def test_health_report_fields() -> None:
    """HealthReport stores checks, overall, and fatal correctly."""
    checks = [CheckResult(name="c1", status="pass", message="ok")]
    report = HealthReport(checks=checks, overall="pass", fatal=False)
    assert report.checks == checks
    assert report.overall == "pass"
    assert report.fatal is False


# ---------------------------------------------------------------------------
# check_all: ordering, short-circuit, overall computation
# ---------------------------------------------------------------------------


def test_check_all_returns_11_results(tmp_path: Path) -> None:
    """check_all always returns exactly 11 CheckResult items."""
    config = make_config(tmp_path)
    patches = _all_pass_patches()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
    ):  # noqa: E501
        report = check_all(config)
    assert len(report.checks) == 11


def test_check_all_overall_pass_when_all_pass(tmp_path: Path) -> None:
    """overall is 'pass' when all checks pass."""
    config = make_config(tmp_path)
    patches = _all_pass_patches()
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
    ):  # noqa: E501
        report = check_all(config)
    assert report.overall == "pass"
    assert report.fatal is False


def test_check_all_overall_warn_with_warn_check(tmp_path: Path) -> None:
    """overall is 'warn' when at least one check warns, none fail."""
    config = make_config(tmp_path)
    warn_result = CheckResult("No active worktrees", "warn", "2 found", "Remove them")
    patches = _all_pass_patches()
    # Override check 5 (worktrees) with a warn
    patches[4] = patch("brimstone.health._check_worktrees", return_value=warn_result)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
    ):  # noqa: E501
        report = check_all(config)
    assert report.overall == "warn"
    assert report.fatal is False


def test_check_all_short_circuits_on_fail(tmp_path: Path) -> None:
    """After a fail, remaining checks are skipped."""
    config = make_config(tmp_path)
    fail_result = CheckResult("ANTHROPIC_API_KEY present", "fail", "Missing key", "Set it")
    patches = _all_pass_patches()
    # Override check 4 (api_key) with a fail
    patches[3] = patch("brimstone.health._check_api_key", return_value=fail_result)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
    ):  # noqa: E501
        report = check_all(config)

    assert report.overall == "fail"
    assert report.fatal is True
    # Checks 5–11 (indices 4–10) should all be skipped
    skipped = [c for c in report.checks if c.status == "skip"]
    assert len(skipped) == 7


def test_check_all_fatal_true_on_fail(tmp_path: Path) -> None:
    """report.fatal is True when any check is 'fail'."""
    config = make_config(tmp_path)
    fail_result = CheckResult("Git repo present", "fail", "Not a repo", "Fix it")
    patches = _all_pass_patches()
    patches[0] = patch("brimstone.health._check_git_repo", return_value=fail_result)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
    ):  # noqa: E501
        report = check_all(config)
    assert report.fatal is True


# ---------------------------------------------------------------------------
# _check_git_repo
# ---------------------------------------------------------------------------


def test_check_git_repo_pass() -> None:
    """Returns pass when git rev-parse succeeds."""
    with patch("subprocess.run", return_value=_subprocess_result(returncode=0)):
        result = _check_git_repo()
    assert result.status == "pass"


def test_check_git_repo_fail() -> None:
    """Returns fail when git rev-parse returns non-zero."""
    with patch("subprocess.run", return_value=_subprocess_result(returncode=128)):
        result = _check_git_repo()
    assert result.status == "fail"
    assert result.remediation is not None


# ---------------------------------------------------------------------------
# _check_default_branch
# ---------------------------------------------------------------------------


def test_check_default_branch_pass_no_config(tmp_path: Path) -> None:
    """Returns pass when no default_branch configured (info only)."""
    config = make_config(tmp_path)
    with patch("subprocess.run", return_value=_subprocess_result(returncode=0, stdout="main\n")):
        result = _check_default_branch(config)
    assert result.status == "pass"


def test_check_default_branch_warn_on_gh_error(tmp_path: Path) -> None:
    """Returns warn when gh returns non-zero (not authenticated yet)."""
    config = make_config(tmp_path)
    with patch("subprocess.run", return_value=_subprocess_result(returncode=1)):
        result = _check_default_branch(config)
    assert result.status == "warn"


def test_check_default_branch_warn_on_mismatch(tmp_path: Path) -> None:
    """Returns warn when actual branch differs from configured branch."""
    config = make_config(tmp_path, default_branch="main")
    with patch(
        "subprocess.run",
        return_value=_subprocess_result(returncode=0, stdout="master\n"),
    ):
        result = _check_default_branch(config)
    assert result.status == "warn"
    assert "master" in result.message


# ---------------------------------------------------------------------------
# _check_gh_auth
# ---------------------------------------------------------------------------


def test_check_gh_auth_pass() -> None:
    """Returns pass when gh auth status exits 0."""
    with patch("subprocess.run", return_value=_subprocess_result(returncode=0)):
        result = _check_gh_auth()
    assert result.status == "pass"


def test_check_gh_auth_fail() -> None:
    """Returns fail when gh auth status exits non-zero."""
    with patch("subprocess.run", return_value=_subprocess_result(returncode=1)):
        result = _check_gh_auth()
    assert result.status == "fail"
    assert result.remediation == "Run: gh auth login"


# ---------------------------------------------------------------------------
# _check_api_key
# ---------------------------------------------------------------------------


def test_check_api_key_pass(tmp_path: Path) -> None:
    """Returns pass when anthropic_api_key is non-empty."""
    config = make_config(tmp_path)
    result = _check_api_key(config)
    assert result.status == "pass"


def test_check_api_key_fail_when_empty(tmp_path: Path) -> None:
    """Returns fail (triggering fatal) when anthropic_api_key is empty."""
    config = make_config(tmp_path, anthropic_api_key="")
    result = _check_api_key(config)
    assert result.status == "fail"
    assert result.remediation is not None


def test_check_api_key_fail_makes_report_fatal(tmp_path: Path) -> None:
    """A failing API key check makes the full report fatal and skips remaining."""
    config = make_config(tmp_path, anthropic_api_key="")
    # Don't mock _check_api_key — let real function run; mock external calls
    patches = _all_pass_patches()
    # Remove api_key patch (index 3) so the real function runs
    patches[3] = patch(
        "brimstone.health._check_api_key",
        side_effect=lambda c: CheckResult("ANTHROPIC_API_KEY present", "fail", "Missing", "Set it"),
    )
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patches[5],
        patches[6],
        patches[7],
        patches[8],
        patches[9],
        patches[10],
    ):  # noqa: E501
        report = check_all(config)

    assert report.fatal is True
    assert report.overall == "fail"
    skipped = [c for c in report.checks if c.status == "skip"]
    assert len(skipped) > 0


# ---------------------------------------------------------------------------
# _check_worktrees
# ---------------------------------------------------------------------------


def test_check_worktrees_pass_no_claude_worktrees() -> None:
    """Returns pass when no worktrees under .claude/worktrees/."""
    porcelain = "worktree /home/user/repo\nHEAD abc\nbranch refs/heads/main\n"
    with patch(
        "subprocess.run",
        return_value=_subprocess_result(returncode=0, stdout=porcelain),
    ):
        result = _check_worktrees()
    assert result.status == "pass"


def test_check_worktrees_warn_with_stale_worktrees(tmp_path: Path) -> None:
    """Returns warn when worktrees exist under .claude/worktrees/."""
    worktree_path = tmp_path / ".claude" / "worktrees" / "10-feat-config"
    worktree_path.mkdir(parents=True)
    porcelain = (
        "worktree /home/user/repo\nHEAD abc\nbranch refs/heads/main\n\n"
        f"worktree {worktree_path}\nHEAD def\nbranch refs/heads/10-feat-config\n"
    )
    with patch(
        "subprocess.run",
        return_value=_subprocess_result(returncode=0, stdout=porcelain),
    ):
        result = _check_worktrees()
    assert result.status == "warn"
    assert "1 worktree(s)" in result.message
    assert result.remediation is not None


def test_check_worktrees_warn_count_in_message(tmp_path: Path) -> None:
    """Warn message includes correct worktree count."""
    wt1 = tmp_path / ".claude" / "worktrees" / "10-feat"
    wt2 = tmp_path / ".claude" / "worktrees" / "20-fix"
    wt1.mkdir(parents=True)
    wt2.mkdir(parents=True)
    porcelain = (
        f"worktree /home/user/repo\nHEAD abc\n\n"
        f"worktree {wt1}\nHEAD def\n\n"
        f"worktree {wt2}\nHEAD ghi\n"
    )
    with patch(
        "subprocess.run",
        return_value=_subprocess_result(returncode=0, stdout=porcelain),
    ):
        result = _check_worktrees()
    assert result.status == "warn"
    assert "2 worktree(s)" in result.message


# ---------------------------------------------------------------------------
# _check_orphaned_issues
# ---------------------------------------------------------------------------


def test_check_orphaned_issues_pass_no_issues(tmp_path: Path) -> None:
    """Returns pass when no in-progress issues."""
    config = make_config(tmp_path)
    with patch("subprocess.run", return_value=_subprocess_result(returncode=0, stdout="[]")):
        result = _check_orphaned_issues(config)
    assert result.status == "pass"


def test_check_orphaned_issues_pass_all_have_prs(tmp_path: Path) -> None:
    """Returns pass when all in-progress issues have open PRs."""
    config = make_config(tmp_path)
    issues = json.dumps([{"number": 10, "title": "feat: thing"}])
    prs = json.dumps([{"number": 42, "headRefName": "10-feat-thing"}])

    call_count = 0

    def side_effect(cmd, **kwargs):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _subprocess_result(returncode=0, stdout=issues)
        return _subprocess_result(returncode=0, stdout=prs)

    with patch("subprocess.run", side_effect=side_effect):
        result = _check_orphaned_issues(config)
    assert result.status == "pass"


def test_check_orphaned_issues_warn_below_threshold(tmp_path: Path) -> None:
    """Returns warn when orphaned count is at or below max_orphaned_issues."""
    config = make_config(tmp_path, max_orphaned_issues=5)
    issues = json.dumps([{"number": 10, "title": "feat: thing"}])
    prs = json.dumps([])  # No open PRs → orphaned

    call_count = 0

    def side_effect(cmd, **kwargs):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _subprocess_result(returncode=0, stdout=issues)
        return _subprocess_result(returncode=0, stdout=prs)

    with patch("subprocess.run", side_effect=side_effect):
        result = _check_orphaned_issues(config)
    assert result.status == "warn"
    assert result.remediation is not None


def test_check_orphaned_issues_fail_above_threshold(tmp_path: Path) -> None:
    """Returns fail when orphaned count exceeds max_orphaned_issues."""
    config = make_config(tmp_path, max_orphaned_issues=2)
    issues = json.dumps(
        [
            {"number": 10, "title": "one"},
            {"number": 11, "title": "two"},
            {"number": 12, "title": "three"},
        ]
    )
    prs = json.dumps([])

    call_count = 0

    def side_effect(cmd, **kwargs):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _subprocess_result(returncode=0, stdout=issues)
        return _subprocess_result(returncode=0, stdout=prs)

    with patch("subprocess.run", side_effect=side_effect):
        result = _check_orphaned_issues(config)
    assert result.status == "fail"
    assert "3 >" in (result.message + (result.remediation or ""))


# ---------------------------------------------------------------------------
# _check_open_prs
# ---------------------------------------------------------------------------


def test_check_open_prs_pass_no_prs(tmp_path: Path) -> None:
    """Returns pass when no open PRs."""
    config = make_config(tmp_path)
    with patch("subprocess.run", return_value=_subprocess_result(returncode=0, stdout="[]")):
        result = _check_open_prs(config)
    assert result.status == "pass"
    assert "No open PRs" in result.message


def test_check_open_prs_pass_all_clean(tmp_path: Path) -> None:
    """Returns pass when open PRs have no failing CI or requested changes."""
    config = make_config(tmp_path)
    prs = json.dumps(
        [
            {
                "number": 1,
                "title": "My PR",
                "reviewDecision": "APPROVED",
                "statusCheckRollup": [{"conclusion": "SUCCESS"}],
            }
        ]
    )
    with patch("subprocess.run", return_value=_subprocess_result(returncode=0, stdout=prs)):
        result = _check_open_prs(config)
    assert result.status == "pass"


def test_check_open_prs_warn_failing_ci(tmp_path: Path) -> None:
    """Returns warn when a PR has failing CI."""
    config = make_config(tmp_path)
    prs = json.dumps(
        [
            {
                "number": 1,
                "title": "My PR",
                "reviewDecision": None,
                "statusCheckRollup": [{"conclusion": "FAILURE"}],
            }
        ]
    )
    with patch("subprocess.run", return_value=_subprocess_result(returncode=0, stdout=prs)):
        result = _check_open_prs(config)
    assert result.status == "warn"
    assert "CI failing" in result.message


def test_check_open_prs_warn_changes_requested(tmp_path: Path) -> None:
    """Returns warn when a PR has changes requested."""
    config = make_config(tmp_path)
    prs = json.dumps(
        [
            {
                "number": 2,
                "title": "Another PR",
                "reviewDecision": "CHANGES_REQUESTED",
                "statusCheckRollup": [],
            }
        ]
    )
    with patch("subprocess.run", return_value=_subprocess_result(returncode=0, stdout=prs)):
        result = _check_open_prs(config)
    assert result.status == "warn"
    assert "changes requested" in result.message


# ---------------------------------------------------------------------------
# _check_backoff
# ---------------------------------------------------------------------------


def test_check_backoff_skip_when_no_checkpoint() -> None:
    """Returns skip when checkpoint is None."""
    result = _check_backoff(None)
    assert result.status == "skip"


def test_check_backoff_pass_when_not_backing_off() -> None:
    """Returns pass when no backoff is active."""
    checkpoint = make_checkpoint(rate_limit_backoff_until=None)
    result = _check_backoff(checkpoint)
    assert result.status == "pass"


def test_check_backoff_warn_when_backing_off() -> None:
    """Returns warn when backoff deadline is in the future."""
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    checkpoint = make_checkpoint(rate_limit_backoff_until=future)
    result = _check_backoff(checkpoint)
    assert result.status == "warn"
    assert result.remediation is not None
    assert "backoff" in result.message.lower()


def test_check_backoff_pass_when_backoff_expired() -> None:
    """Returns pass when backoff deadline is in the past."""
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    checkpoint = make_checkpoint(rate_limit_backoff_until=past)
    result = _check_backoff(checkpoint)
    assert result.status == "pass"


# ---------------------------------------------------------------------------
# _check_orchestrator_lock
# ---------------------------------------------------------------------------


def test_check_orchestrator_lock_pass_no_file(tmp_path: Path) -> None:
    """Returns pass when no lock file exists."""
    config = make_config(tmp_path)
    result = _check_orchestrator_lock(config)
    assert result.status == "pass"
    assert "No orchestrator lock" in result.message


def test_check_orchestrator_lock_pass_stale_pid(tmp_path: Path) -> None:
    """Returns pass and removes lock when PID is dead (ProcessLookupError)."""
    config = make_config(tmp_path)
    lock_path = tmp_path / "checkpoints" / ".orchestrator.lock"
    lock_path.parent.mkdir(parents=True)
    lock_data = {
        "pid": 99999999,
        "started_at": "2026-01-01T00:00:00+00:00",
        "run_id": "test",
    }
    lock_path.write_text(json.dumps(lock_data))

    with patch("os.kill", side_effect=ProcessLookupError):
        result = _check_orchestrator_lock(config)

    assert result.status == "pass"
    assert "stale lock" in result.message.lower() or "no longer running" in result.message
    assert not lock_path.exists()


def test_check_orchestrator_lock_fail_live_pid(tmp_path: Path) -> None:
    """Returns fail when PID is alive (os.kill raises no exception)."""
    config = make_config(tmp_path)
    lock_path = tmp_path / "checkpoints" / ".orchestrator.lock"
    lock_path.parent.mkdir(parents=True)
    lock_data = {
        "pid": 12345,
        "started_at": "2026-01-01T00:00:00+00:00",
        "run_id": "test",
    }
    lock_path.write_text(json.dumps(lock_data))

    with patch("os.kill", return_value=None):  # No exception → alive
        result = _check_orchestrator_lock(config)

    assert result.status == "fail"
    assert "12345" in result.message
    assert result.remediation is not None


def test_check_orchestrator_lock_fail_permission_error(tmp_path: Path) -> None:
    """Returns fail when PID is alive (PermissionError means process exists)."""
    config = make_config(tmp_path)
    lock_path = tmp_path / "checkpoints" / ".orchestrator.lock"
    lock_path.parent.mkdir(parents=True)
    lock_data = {
        "pid": 12345,
        "started_at": "2026-01-01T00:00:00+00:00",
        "run_id": "test",
    }
    lock_path.write_text(json.dumps(lock_data))

    with patch("os.kill", side_effect=PermissionError):
        result = _check_orchestrator_lock(config)

    assert result.status == "fail"


# ---------------------------------------------------------------------------
# _check_checkpoint_dir_writable
# ---------------------------------------------------------------------------


def test_check_checkpoint_dir_writable_pass(tmp_path: Path) -> None:
    """Returns pass when checkpoint dir is writable."""
    config = make_config(tmp_path)
    result = _check_checkpoint_dir_writable(config)
    assert result.status == "pass"


def test_check_checkpoint_dir_writable_fail(tmp_path: Path) -> None:
    """Returns fail on OSError during write probe."""
    config = make_config(tmp_path)
    with patch("tempfile.NamedTemporaryFile", side_effect=OSError("Permission denied")):
        result = _check_checkpoint_dir_writable(config)
    assert result.status == "fail"
    assert result.remediation is not None


# ---------------------------------------------------------------------------
# _check_log_dir_writable
# ---------------------------------------------------------------------------


def test_check_log_dir_writable_pass(tmp_path: Path) -> None:
    """Returns pass when log dir is writable."""
    config = make_config(tmp_path)
    result = _check_log_dir_writable(config)
    assert result.status == "pass"


def test_check_log_dir_writable_fail(tmp_path: Path) -> None:
    """Returns fail on OSError during write probe."""
    config = make_config(tmp_path)
    with patch("tempfile.NamedTemporaryFile", side_effect=OSError("Permission denied")):
        result = _check_log_dir_writable(config)
    assert result.status == "fail"


# ---------------------------------------------------------------------------
# acquire_orchestrator_lock / release_orchestrator_lock
# ---------------------------------------------------------------------------


def test_acquire_creates_lock_file(tmp_path: Path) -> None:
    """acquire_orchestrator_lock writes a lock file with pid and run_id."""
    config = make_config(tmp_path)
    lock_path = tmp_path / "checkpoints" / ".orchestrator.lock"

    with patch("atexit.register"), patch("signal.signal"):
        acquire_orchestrator_lock(config, run_id="test-run-123")

    assert lock_path.exists()
    data = json.loads(lock_path.read_text())
    assert data["pid"] == os.getpid()
    assert data["run_id"] == "test-run-123"
    assert "started_at" in data


def test_acquire_stale_lock_removes_and_succeeds(tmp_path: Path) -> None:
    """acquire_orchestrator_lock removes stale lock (dead PID) and succeeds."""
    config = make_config(tmp_path)
    lock_path = tmp_path / "checkpoints" / ".orchestrator.lock"
    lock_path.parent.mkdir(parents=True)
    stale_data = {
        "pid": 99999999,
        "started_at": "2026-01-01T00:00:00+00:00",
        "run_id": "old",
    }
    lock_path.write_text(json.dumps(stale_data))

    with (
        patch("os.kill", side_effect=ProcessLookupError),
        patch("atexit.register"),
        patch("signal.signal"),
    ):
        acquire_orchestrator_lock(config, run_id="new-run")

    data = json.loads(lock_path.read_text())
    assert data["run_id"] == "new-run"
    assert data["pid"] == os.getpid()


def test_acquire_live_lock_raises_fatal(tmp_path: Path) -> None:
    """acquire_orchestrator_lock raises FatalHealthCheckError for live PID."""
    config = make_config(tmp_path)
    lock_path = tmp_path / "checkpoints" / ".orchestrator.lock"
    lock_path.parent.mkdir(parents=True)
    live_data = {
        "pid": 12345,
        "started_at": "2026-01-01T00:00:00+00:00",
        "run_id": "live",
    }
    lock_path.write_text(json.dumps(live_data))

    with patch("os.kill", return_value=None):  # No exception → alive
        with pytest.raises(FatalHealthCheckError) as exc_info:
            acquire_orchestrator_lock(config, run_id="new-run")

    assert "12345" in str(exc_info.value)


def test_acquire_live_lock_permission_error_raises_fatal(tmp_path: Path) -> None:
    """acquire_orchestrator_lock raises FatalHealthCheckError on PermissionError."""
    config = make_config(tmp_path)
    lock_path = tmp_path / "checkpoints" / ".orchestrator.lock"
    lock_path.parent.mkdir(parents=True)
    live_data = {
        "pid": 12345,
        "started_at": "2026-01-01T00:00:00+00:00",
        "run_id": "live",
    }
    lock_path.write_text(json.dumps(live_data))

    with patch("os.kill", side_effect=PermissionError):
        with pytest.raises(FatalHealthCheckError):
            acquire_orchestrator_lock(config, run_id="new-run")


def test_release_deletes_own_lock(tmp_path: Path) -> None:
    """release_orchestrator_lock deletes the lock when PID matches."""
    config = make_config(tmp_path)
    lock_path = tmp_path / "checkpoints" / ".orchestrator.lock"
    lock_path.parent.mkdir(parents=True)
    lock_data = {"pid": os.getpid(), "started_at": "now", "run_id": "self"}
    lock_path.write_text(json.dumps(lock_data))

    release_orchestrator_lock(config)
    assert not lock_path.exists()


def test_release_does_not_delete_other_pid_lock(tmp_path: Path) -> None:
    """release_orchestrator_lock does not delete a lock owned by another PID."""
    config = make_config(tmp_path)
    lock_path = tmp_path / "checkpoints" / ".orchestrator.lock"
    lock_path.parent.mkdir(parents=True)
    lock_data = {"pid": 99999, "started_at": "now", "run_id": "other"}
    lock_path.write_text(json.dumps(lock_data))

    release_orchestrator_lock(config)
    assert lock_path.exists()


def test_release_is_idempotent(tmp_path: Path) -> None:
    """release_orchestrator_lock is safe to call multiple times."""
    config = make_config(tmp_path)
    lock_path = tmp_path / "checkpoints" / ".orchestrator.lock"
    lock_path.parent.mkdir(parents=True)
    lock_data = {"pid": os.getpid(), "started_at": "now", "run_id": "self"}
    lock_path.write_text(json.dumps(lock_data))

    release_orchestrator_lock(config)
    release_orchestrator_lock(config)  # Second call must not raise
    assert not lock_path.exists()


def test_release_no_op_when_no_lock_file(tmp_path: Path) -> None:
    """release_orchestrator_lock is a no-op when lock file does not exist."""
    config = make_config(tmp_path)
    # Should not raise
    release_orchestrator_lock(config)


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------


def test_format_report_pass_contains_check_mark() -> None:
    """format_report uses ✓ symbol for pass checks."""
    checks = [CheckResult(name="Git repo present", status="pass", message="ok")]
    report = HealthReport(checks=checks, overall="pass", fatal=False)
    output = format_report(report)
    assert "✓" in output
    assert "Git repo present" in output


def test_format_report_warn_contains_warning_symbol() -> None:
    """format_report uses ⚠ symbol for warn checks."""
    checks = [
        CheckResult(
            name="No active worktrees",
            status="warn",
            message="2 found",
            remediation="Remove them",
        )
    ]
    report = HealthReport(checks=checks, overall="warn", fatal=False)
    output = format_report(report)
    assert "⚠" in output
    assert "No active worktrees" in output


def test_format_report_fail_contains_fail_symbol() -> None:
    """format_report uses ✗ symbol for fail checks."""
    checks = [
        CheckResult(
            name="Single orchestrator guard",
            status="fail",
            message="Live PID 123",
            remediation="Stop it",
        )
    ]
    report = HealthReport(checks=checks, overall="fail", fatal=True)
    output = format_report(report)
    assert "✗" in output
    assert "FATAL" in output


def test_format_report_skip_contains_skip_symbol() -> None:
    """format_report uses - symbol for skipped checks."""
    checks = [CheckResult(name="Log dir writable", status="skip", message="Skipped")]
    report = HealthReport(checks=checks, overall="pass", fatal=False)
    output = format_report(report)
    assert "- " in output


def test_format_report_overall_pass_line() -> None:
    """Summary line reads 'Overall: PASS' for a clean report."""
    checks = [CheckResult(name="Git repo present", status="pass", message="ok")]
    report = HealthReport(checks=checks, overall="pass", fatal=False)
    output = format_report(report)
    assert "Overall: PASS" in output


def test_format_report_overall_warn_line() -> None:
    """Summary line reads 'Overall: WARN — N warning(s), 0 error(s)'."""
    checks = [
        CheckResult(name="No active worktrees", status="warn", message="2 found"),
        CheckResult(name="Git repo present", status="pass", message="ok"),
    ]
    report = HealthReport(checks=checks, overall="warn", fatal=False)
    output = format_report(report)
    assert "Overall: WARN" in output
    assert "1 warning(s)" in output
    assert "0 error(s)" in output


def test_format_report_overall_fail_line() -> None:
    """Summary line reads 'Overall: FAIL — N warning(s), M error(s)'."""
    checks = [
        CheckResult(name="No active worktrees", status="warn", message="stale"),
        CheckResult(
            name="Single orchestrator guard",
            status="fail",
            message="live",
            remediation="stop",
        ),
    ]
    report = HealthReport(checks=checks, overall="fail", fatal=True)
    output = format_report(report)
    assert "Overall: FAIL" in output
    assert "1 warning(s)" in output
    assert "1 error(s)" in output


def test_format_report_includes_separator() -> None:
    """format_report includes horizontal rule separators."""
    checks = [CheckResult(name="Git repo present", status="pass", message="ok")]
    report = HealthReport(checks=checks, overall="pass", fatal=False)
    output = format_report(report)
    assert "─" in output


def test_format_report_remediation_prefixed_with_fix() -> None:
    """Remediation lines are prefixed with '  Fix: '."""
    checks = [
        CheckResult(
            name="gh CLI authenticated",
            status="fail",
            message="Not authenticated.",
            remediation="Run: gh auth login",
        )
    ]
    report = HealthReport(checks=checks, overall="fail", fatal=True)
    output = format_report(report)
    assert "  Fix: Run: gh auth login" in output


def test_format_report_count_matches_checks() -> None:
    """format_report warning/error counts match the actual check statuses."""
    checks = [
        CheckResult(name="c1", status="warn", message="w1"),
        CheckResult(name="c2", status="warn", message="w2"),
        CheckResult(name="c3", status="fail", message="f1"),
        CheckResult(name="c4", status="pass", message="ok"),
    ]
    report = HealthReport(checks=checks, overall="fail", fatal=True)
    output = format_report(report)
    assert "2 warning(s)" in output
    assert "1 error(s)" in output
