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
    _check_bot_collaborator,
    _check_bot_token,
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
    """Return a list of patch context managers for all 13 checks returning pass."""
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
            "brimstone.health._check_bot_token",
            return_value=_pass("BRIMSTONE_GH_TOKEN present"),
        ),
        patch(
            "brimstone.health._check_bot_collaborator",
            return_value=_pass("yeast-bot is repo collaborator"),
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


def test_check_all_returns_13_results(tmp_path: Path) -> None:
    """check_all always returns exactly 13 CheckResult items."""
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
        patches[11],
        patches[12],
    ):
        report = check_all(config)
    assert len(report.checks) == 13


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
        patches[11],
        patches[12],
    ):
        report = check_all(config)
    assert report.overall == "pass"
    assert report.fatal is False


def test_check_all_overall_warn_with_warn_check(tmp_path: Path) -> None:
    """overall is 'warn' when at least one check warns, none fail."""
    config = make_config(tmp_path)
    warn_result = CheckResult("No active worktrees", "warn", "2 found", "Remove them")
    patches = _all_pass_patches()
    # Override check 7 (worktrees, now at index 6) with a warn
    patches[6] = patch("brimstone.health._check_worktrees", return_value=warn_result)
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
        patches[11],
        patches[12],
    ):
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
        patches[11],
        patches[12],
    ):
        report = check_all(config)

    assert report.overall == "fail"
    assert report.fatal is True
    # Checks 5–13 (indices 4–12) should all be skipped
    skipped = [c for c in report.checks if c.status == "skip"]
    assert len(skipped) == 9


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
        patches[11],
        patches[12],
    ):
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
    """Returns warn when BRIMSTONE_DEFAULT_BRANCH is set and differs from repo branch."""
    config = make_config(tmp_path, default_branch="main")
    with (
        patch(
            "subprocess.run",
            return_value=_subprocess_result(returncode=0, stdout="master\n"),
        ),
        patch.dict("os.environ", {"BRIMSTONE_DEFAULT_BRANCH": "main"}),
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
        patches[11],
    ):  # noqa: E501
        report = check_all(config)

    assert report.fatal is True
    assert report.overall == "fail"
    skipped = [c for c in report.checks if c.status == "skip"]
    assert len(skipped) > 0


# ---------------------------------------------------------------------------
# _check_bot_token
# ---------------------------------------------------------------------------


def test_check_bot_token_pass() -> None:
    """Returns pass when BRIMSTONE_GH_TOKEN is set."""
    with patch.dict("os.environ", {"BRIMSTONE_GH_TOKEN": "ghp_fake_token"}):
        result = _check_bot_token()
    assert result.status == "pass"


def test_check_bot_token_fail_when_unset() -> None:
    """Returns fail when BRIMSTONE_GH_TOKEN is absent."""
    env = {k: v for k, v in os.environ.items() if k != "BRIMSTONE_GH_TOKEN"}
    with patch.dict("os.environ", env, clear=True):
        result = _check_bot_token()
    assert result.status == "fail"
    assert result.remediation is not None


# ---------------------------------------------------------------------------
# _check_bot_collaborator
# ---------------------------------------------------------------------------


def test_check_bot_collaborator_skip_when_no_repo(tmp_path: Path) -> None:
    """Returns skip when config.github_repo is not set."""
    config = make_config(tmp_path)  # github_repo defaults to None
    result = _check_bot_collaborator(config)
    assert result.status == "skip"


def test_check_bot_collaborator_pass_when_active(tmp_path: Path) -> None:
    """Returns pass when gh API confirms yeast-bot is a collaborator."""
    config = make_config(tmp_path, github_repo="owner/repo")
    check_ok = _subprocess_result(returncode=0)
    with patch("subprocess.run", return_value=check_ok):
        result = _check_bot_collaborator(config)
    assert result.status == "pass"
    assert "active collaborator" in result.message


def test_check_bot_collaborator_auto_adds_and_accepts(tmp_path: Path) -> None:
    """When not a collaborator, adds yeast-bot and accepts the invitation."""
    config = make_config(tmp_path, github_repo="owner/repo")
    # First call: check returns 404 (not a collaborator)
    check_fail = _subprocess_result(returncode=1, stderr="Not Found")
    # Second call: PUT add returns success
    add_ok = _subprocess_result(returncode=0)
    # Third call: curl list invitations
    inv_list = _subprocess_result(
        returncode=0,
        stdout=json.dumps([{"id": 123, "repository": {"full_name": "owner/repo"}}]),
    )
    # Fourth call: curl PATCH accept
    accept_ok = _subprocess_result(returncode=0, stdout="204")

    with (
        patch("subprocess.run", side_effect=[check_fail, add_ok, inv_list, accept_ok]),
        patch.dict("os.environ", {"BRIMSTONE_GH_TOKEN": "ghp_fake_token"}),
    ):
        result = _check_bot_collaborator(config)

    assert result.status == "pass"
    assert "Added yeast-bot" in result.message


def test_check_bot_collaborator_fail_when_add_fails(tmp_path: Path) -> None:
    """Returns fail if the auto-add gh API call fails."""
    config = make_config(tmp_path, github_repo="owner/repo")
    check_fail = _subprocess_result(returncode=1, stderr="Not Found")
    add_fail = _subprocess_result(returncode=1, stderr="Forbidden")

    with patch("subprocess.run", side_effect=[check_fail, add_fail]):
        result = _check_bot_collaborator(config)

    assert result.status == "fail"
    assert result.remediation is not None


def test_check_bot_collaborator_fail_when_token_missing_after_add(tmp_path: Path) -> None:
    """Returns fail if add succeeds but BRIMSTONE_GH_TOKEN is absent."""
    config = make_config(tmp_path, github_repo="owner/repo")
    check_fail = _subprocess_result(returncode=1)
    add_ok = _subprocess_result(returncode=0)
    env = {k: v for k, v in os.environ.items() if k != "BRIMSTONE_GH_TOKEN"}

    with (
        patch("subprocess.run", side_effect=[check_fail, add_ok]),
        patch.dict("os.environ", env, clear=True),
    ):
        result = _check_bot_collaborator(config)

    assert result.status == "fail"
    assert result.remediation is not None


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


def test_check_worktrees_auto_removes_stale(tmp_path: Path) -> None:
    """Stale worktrees are removed automatically and the check returns pass."""
    worktree_path = tmp_path / ".claude" / "worktrees" / "10-feat-config"
    worktree_path.mkdir(parents=True)
    porcelain = (
        "worktree /home/user/repo\nHEAD abc\nbranch refs/heads/main\n\n"
        f"worktree {worktree_path}\nHEAD def\nbranch refs/heads/10-feat-config\n"
    )
    list_ok = _subprocess_result(returncode=0, stdout=porcelain)
    remove_ok = _subprocess_result(returncode=0, stdout="")
    with patch("subprocess.run", side_effect=[list_ok, remove_ok]):
        result = _check_worktrees()
    assert result.status == "pass"
    assert "Removed 1" in result.message


def test_check_worktrees_warn_when_removal_fails(tmp_path: Path) -> None:
    """Warn with remediation instructions when auto-removal fails."""
    wt1 = tmp_path / ".claude" / "worktrees" / "10-feat"
    wt2 = tmp_path / ".claude" / "worktrees" / "20-fix"
    wt1.mkdir(parents=True)
    wt2.mkdir(parents=True)
    porcelain = (
        f"worktree /home/user/repo\nHEAD abc\n\n"
        f"worktree {wt1}\nHEAD def\n\n"
        f"worktree {wt2}\nHEAD ghi\n"
    )
    list_ok = _subprocess_result(returncode=0, stdout=porcelain)
    remove_fail = _subprocess_result(returncode=1, stdout="", stderr="locked")
    with patch("subprocess.run", side_effect=[list_ok, remove_fail, remove_fail]):
        result = _check_worktrees()
    assert result.status == "warn"
    assert "failed to remove 2" in result.message
    assert result.remediation is not None


# ---------------------------------------------------------------------------
# _check_orphaned_issues
# ---------------------------------------------------------------------------


def test_check_orphaned_issues_pass_no_github_repo(tmp_path: Path) -> None:
    """Returns pass (skipped) when no github_repo is configured."""
    config = make_config(tmp_path)  # no github_repo
    result = _check_orphaned_issues(config)
    assert result.status == "pass"
    assert "skipped" in result.message


def test_check_orphaned_issues_pass_no_claimed_beads(tmp_path: Path) -> None:
    """Returns pass when bead store has no claimed beads."""
    from brimstone.beads import make_bead_store

    config = make_config(tmp_path, github_repo="owner/repo", beads_dir=tmp_path / "beads")
    store = make_bead_store(config, "owner/repo")
    # Write an open (not claimed) bead to confirm it isn't counted
    from brimstone.beads import WorkBead

    store.write_work_bead(
        WorkBead(
            v=1,
            issue_number=1,
            title="open issue",
            milestone="v1",
            stage="impl",
            module="config",
            priority="P2",
            state="open",
            branch="1-open-issue",
        )
    )
    result = _check_orphaned_issues(config)
    assert result.status == "pass"


def test_check_orphaned_issues_pass_all_have_pr_beads(tmp_path: Path) -> None:
    """Returns pass when all claimed beads have active PR beads."""
    from brimstone.beads import PRBead, WorkBead, make_bead_store

    config = make_config(tmp_path, github_repo="owner/repo", beads_dir=tmp_path / "beads")
    store = make_bead_store(config, "owner/repo")
    store.write_work_bead(
        WorkBead(
            v=1,
            issue_number=10,
            title="feat: thing",
            milestone="v1",
            stage="impl",
            module="config",
            priority="P2",
            state="claimed",
            branch="10-feat-thing",
        )
    )
    store.write_pr_bead(
        PRBead(v=1, pr_number=42, issue_number=10, branch="10-feat-thing", state="open")
    )
    result = _check_orphaned_issues(config)
    assert result.status == "pass"


def test_check_orphaned_issues_warn_no_pr_bead(tmp_path: Path) -> None:
    """Returns warn when a claimed bead has no active PR bead."""
    from brimstone.beads import WorkBead, make_bead_store

    config = make_config(tmp_path, github_repo="owner/repo", beads_dir=tmp_path / "beads")
    store = make_bead_store(config, "owner/repo")
    store.write_work_bead(
        WorkBead(
            v=1,
            issue_number=10,
            title="feat: thing",
            milestone="v1",
            stage="impl",
            module="config",
            priority="P2",
            state="claimed",
            branch="10-feat-thing",
        )
    )
    # No PR bead written → orphaned
    result = _check_orphaned_issues(config)
    assert result.status == "warn"
    assert "#10" in result.message
    assert result.remediation is not None


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
