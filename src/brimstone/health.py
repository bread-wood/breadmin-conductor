"""Preflight health checks for brimstone commands.

Runs 11 ordered checks at worker startup. Distinguishes fatal checks (abort)
from warnings (proceed with caution). Manages the single-orchestrator lock file.
Powers `brimstone health`.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from brimstone.config import Config
from brimstone.session import Checkpoint, is_backing_off

# ---------------------------------------------------------------------------
# Module-level lock config ref (used by SIGTERM handler)
# ---------------------------------------------------------------------------

_lock_config: Config | None = None

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FatalHealthCheckError(RuntimeError):
    """Raised when check_all() returns a report with fatal=True."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single preflight check."""

    name: str
    status: Literal["pass", "warn", "fail", "skip"]
    message: str
    remediation: str | None = None


@dataclass(frozen=True)
class HealthReport:
    """Aggregated result of check_all()."""

    checks: list[CheckResult]
    overall: Literal["pass", "warn", "fail"]
    fatal: bool


# ---------------------------------------------------------------------------
# check_all
# ---------------------------------------------------------------------------


def check_all(
    config: Config,
    checkpoint: Checkpoint | None = None,
    skip_checks: frozenset[str] = frozenset(),
) -> HealthReport:
    """Run all 11 preflight checks in order.

    Short-circuits on any "fail" result — remaining checks get status "skip".
    Computes overall as worst status across all non-skip results.
    Sets fatal=True if any check is "fail".

    Args:
        config:       Resolved Config instance.
        checkpoint:   Current Checkpoint object, or None if no checkpoint yet.
        skip_checks:  Set of check names to skip entirely (e.g. checks that are
                      irrelevant for headless commands targeting a remote repo).

    Returns:
        A HealthReport with all check results.
    """
    named_checks: list[tuple[str, object]] = [
        ("Git repo present", _check_git_repo),
        ("Default branch matches config", lambda: _check_default_branch(config)),
        ("gh CLI authenticated", _check_gh_auth),
        ("ANTHROPIC_API_KEY present", lambda: _check_api_key(config)),
        ("No active worktrees", _check_worktrees),
        ("No stale in-progress issues", lambda: _check_orphaned_issues(config)),
        ("Open PRs needing attention", lambda: _check_open_prs(config)),
        ("Rate limit backoff active", lambda: _check_backoff(checkpoint)),
        ("Single orchestrator guard", lambda: _check_orchestrator_lock(config)),
        ("Checkpoint dir writable", lambda: _check_checkpoint_dir_writable(config)),
        ("Log dir writable", lambda: _check_log_dir_writable(config)),
    ]

    results: list[CheckResult] = []
    failed = False

    for name, fn in named_checks:
        if name in skip_checks:
            results.append(
                CheckResult(
                    name=name,
                    status="skip",
                    message="Not applicable for this command.",
                    remediation=None,
                )
            )
        elif failed:
            results.append(
                CheckResult(
                    name=name,
                    status="skip",
                    message="Skipped due to earlier fatal failure",
                    remediation=None,
                )
            )
        else:
            result = fn()  # type: ignore[operator]
            results.append(result)
            if result.status == "fail":
                failed = True

    # Compute overall from non-skip results
    statuses = {r.status for r in results if r.status != "skip"}
    if "fail" in statuses:
        overall: Literal["pass", "warn", "fail"] = "fail"
    elif "warn" in statuses:
        overall = "warn"
    else:
        overall = "pass"

    fatal = overall == "fail"
    return HealthReport(checks=results, overall=overall, fatal=fatal)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_git_repo() -> CheckResult:
    """Check 1: Git repo present."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return CheckResult(
            name="Git repo present",
            status="pass",
            message="Working directory is inside a git repo.",
        )
    return CheckResult(
        name="Git repo present",
        status="fail",
        message="Working directory is not inside a git repo.",
        remediation="Change to a git repository before running brimstone.",
    )


def _check_default_branch(config: Config) -> CheckResult:
    """Check 2: Default branch matches config."""
    result = subprocess.run(
        [
            "gh",
            "repo",
            "view",
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return CheckResult(
            name="Default branch matches config",
            status="warn",
            message=(
                "Could not verify default branch: gh returned an error (run 'gh auth login' first)."
            ),
            remediation="Run: gh auth login",
        )

    actual_branch = result.stdout.strip()

    configured_branch = getattr(config, "default_branch", None)
    if not configured_branch:
        return CheckResult(
            name="Default branch matches config",
            status="pass",
            message=f"Repo default branch is '{actual_branch}' (no config override set).",
        )

    if actual_branch == configured_branch:
        return CheckResult(
            name="Default branch matches config",
            status="pass",
            message=f"Default branch matches config: '{actual_branch}'.",
        )

    return CheckResult(
        name="Default branch matches config",
        status="warn",
        message=f"Mismatch: config={configured_branch}, repo={actual_branch}",
        remediation=(
            f"Set BRIMSTONE_DEFAULT_BRANCH={actual_branch} or update config to match "
            f"the repo's default branch ({actual_branch})."
        ),
    )


def _check_gh_auth() -> CheckResult:
    """Check 3: gh CLI authenticated."""
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return CheckResult(
            name="gh CLI authenticated",
            status="pass",
            message="gh CLI is authenticated.",
        )
    return CheckResult(
        name="gh CLI authenticated",
        status="fail",
        message="gh CLI is not authenticated.",
        remediation="Run: gh auth login",
    )


def _check_api_key(config: Config) -> CheckResult:
    """Check 4: ANTHROPIC_API_KEY present."""
    key = getattr(config, "anthropic_api_key", None) or ""
    if key:
        return CheckResult(
            name="ANTHROPIC_API_KEY present",
            status="pass",
            message="ANTHROPIC_API_KEY is set.",
        )
    return CheckResult(
        name="ANTHROPIC_API_KEY present",
        status="fail",
        message="ANTHROPIC_API_KEY is not set or is empty.",
        remediation="Set the ANTHROPIC_API_KEY environment variable before running brimstone.",
    )


def _check_worktrees() -> CheckResult:
    """Check 5: No active worktrees under .claude/worktrees/."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return CheckResult(
            name="No active worktrees",
            status="warn",
            message="Could not list git worktrees.",
            remediation=None,
        )

    # Parse worktree paths from porcelain output
    worktree_paths: list[Path] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            worktree_paths.append(Path(line[len("worktree ") :]))

    # Filter to paths under .claude/worktrees/
    stale = [p for p in worktree_paths if ".claude/worktrees/" in str(p)]

    if not stale:
        return CheckResult(
            name="No active worktrees",
            status="pass",
            message="No worktrees found under .claude/worktrees/.",
        )

    now = datetime.now(UTC)
    count = len(stale)
    msg_lines = [f"{count} worktree(s) found under .claude/worktrees/:"]
    remediation_paths: list[str] = []
    for path in stale:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            age = now - mtime
            age_days = age.days
            age_str = f"{age_days} day(s) old" if age_days >= 1 else "less than 1 day old"
            msg_lines.append(f"  {path} ({age_str})")
        except OSError:
            msg_lines.append(f"  {path}")
        remediation_paths.append(str(path))

    remediation = "Stale worktrees found. Remove with:\n" + "\n".join(
        f"  git worktree remove --force {p}" for p in remediation_paths
    )

    return CheckResult(
        name="No active worktrees",
        status="warn",
        message="\n".join(msg_lines),
        remediation=remediation,
    )


def _check_orphaned_issues(config: Config) -> CheckResult:
    """Check 6: No stale in-progress issues."""
    max_orphaned: int = getattr(config, "max_orphaned_issues", 5)

    # Get in-progress issues
    issues_result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--label",
            "in-progress",
            "--state",
            "open",
            "--json",
            "number,title",
            "--limit",
            "100",
        ],
        capture_output=True,
        text=True,
    )
    if issues_result.returncode != 0:
        return CheckResult(
            name="No stale in-progress issues",
            status="warn",
            message="Could not list in-progress issues via gh.",
            remediation=None,
        )

    try:
        issues: list[dict] = json.loads(issues_result.stdout)
    except json.JSONDecodeError:
        return CheckResult(
            name="No stale in-progress issues",
            status="warn",
            message="Could not parse gh issue list output.",
            remediation=None,
        )

    if not issues:
        return CheckResult(
            name="No stale in-progress issues",
            status="pass",
            message="No in-progress issues found.",
        )

    # Get open PRs to cross-reference by branch naming convention
    pr_result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,headRefName",
            "--limit",
            "100",
        ],
        capture_output=True,
        text=True,
    )
    open_prs: list[dict] = []
    if pr_result.returncode == 0:
        try:
            open_prs = json.loads(pr_result.stdout)
        except json.JSONDecodeError:
            pass

    # Build set of issue numbers that have a corresponding open PR
    issues_with_prs: set[int] = set()
    for pr in open_prs:
        head_ref: str = pr.get("headRefName", "")
        parts = head_ref.split("-", 1)
        if parts[0].isdigit():
            issues_with_prs.add(int(parts[0]))

    # Orphaned = in-progress label, no open PR
    orphaned = [issue for issue in issues if issue["number"] not in issues_with_prs]
    count = len(orphaned)

    if count == 0:
        return CheckResult(
            name="No stale in-progress issues",
            status="pass",
            message=f"{len(issues)} in-progress issue(s) all have open PRs.",
        )

    if count > max_orphaned:
        return CheckResult(
            name="No stale in-progress issues",
            status="fail",
            message=(
                f"Too many orphaned in-progress issues: {count} with no open PR "
                f"(threshold: {max_orphaned})."
            ),
            remediation=(
                f"Too many orphaned in-progress issues ({count} > {max_orphaned}). "
                "Clear stale labels before starting a new run."
            ),
        )

    issue_list = ", ".join(f"#{i['number']} '{i['title']}'" for i in orphaned)
    return CheckResult(
        name="No stale in-progress issues",
        status="warn",
        message=f"{count} in-progress issue(s) with no open PR: {issue_list}.",
        remediation=(
            f"In-progress issues with no open PR: {issue_list}. "
            "Inspect and remove the 'in-progress' label if stale."
        ),
    )


def _check_open_prs(config: Config) -> CheckResult:
    """Check 7: Open PRs needing attention."""
    github_repo: str | None = getattr(config, "github_repo", None)

    cmd = [
        "gh",
        "pr",
        "list",
        "--state",
        "open",
        "--json",
        "number,title,statusCheckRollup,reviewDecision",
        "--limit",
        "50",
    ]
    if github_repo:
        cmd.extend(["--repo", github_repo])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return CheckResult(
            name="Open PRs needing attention",
            status="warn",
            message="Could not list open PRs via gh.",
            remediation=None,
        )

    try:
        prs: list[dict] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return CheckResult(
            name="Open PRs needing attention",
            status="warn",
            message="Could not parse gh pr list output.",
            remediation=None,
        )

    if not prs:
        return CheckResult(
            name="Open PRs needing attention",
            status="pass",
            message="No open PRs.",
        )

    # Identify PRs needing attention
    needing_attention: list[str] = []
    for pr in prs:
        reasons: list[str] = []

        if pr.get("reviewDecision") == "CHANGES_REQUESTED":
            reasons.append("changes requested")

        status_checks = pr.get("statusCheckRollup") or []
        for check in status_checks:
            conclusion = check.get("conclusion", "")
            if conclusion in ("FAILURE", "CANCELLED"):
                reasons.append("CI failing")
                break

        if reasons:
            reason_str = " / ".join(reasons)
            needing_attention.append(f"#{pr['number']} '{pr['title']}' [{reason_str}]")

    if not needing_attention:
        return CheckResult(
            name="Open PRs needing attention",
            status="pass",
            message=f"{len(prs)} open PR(s), all passing.",
        )

    pr_list = ", ".join(needing_attention)
    return CheckResult(
        name="Open PRs needing attention",
        status="warn",
        message=f"{len(needing_attention)} PR(s) needing attention: {pr_list}.",
        remediation=(f"PRs needing attention: {pr_list}. Review before starting new work."),
    )


def _check_backoff(checkpoint: Checkpoint | None) -> CheckResult:
    """Check 8: Rate limit backoff active."""
    if checkpoint is None:
        return CheckResult(
            name="Rate limit backoff active",
            status="skip",
            message="No checkpoint — backoff check skipped.",
        )

    if not is_backing_off(checkpoint):
        return CheckResult(
            name="Rate limit backoff active",
            status="pass",
            message="No rate limit backoff active.",
        )

    backoff_until_str = checkpoint.rate_limit_backoff_until
    assert backoff_until_str is not None  # guaranteed by is_backing_off
    backoff_until = datetime.fromisoformat(backoff_until_str)
    now = datetime.now(UTC)
    remaining = backoff_until - now
    remaining_minutes = max(1, int(remaining.total_seconds() / 60) + 1)

    return CheckResult(
        name="Rate limit backoff active",
        status="warn",
        message=(
            f"Rate limit backoff active. "
            f"Resumes at {backoff_until.isoformat()} (in {remaining_minutes} minute(s))."
        ),
        remediation=(
            f"Rate limit backoff active until {backoff_until.isoformat()}. "
            "Worker will pause until then."
        ),
    )


def _check_orchestrator_lock(config: Config) -> CheckResult:
    """Check 9: Single orchestrator guard."""
    lock_path = Path(config.checkpoint_dir).expanduser() / ".orchestrator.lock"

    if not lock_path.exists():
        return CheckResult(
            name="Single orchestrator guard",
            status="pass",
            message="No orchestrator lock file found.",
        )

    try:
        lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(lock_data["pid"])
        started_at = lock_data.get("started_at", "unknown")
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return CheckResult(
            name="Single orchestrator guard",
            status="warn",
            message="Orchestrator lock file exists but could not be parsed.",
            remediation=f"Inspect or remove: {lock_path}",
        )

    if pid == os.getpid():
        return CheckResult(
            name="Single orchestrator guard",
            status="pass",
            message="Orchestrator lock is held by the current process.",
        )

    try:
        os.kill(pid, 0)
        # No exception — PID is alive
        return CheckResult(
            name="Single orchestrator guard",
            status="fail",
            message=f"Another orchestrator is running (PID {pid}, started {started_at}).",
            remediation=(
                f"Another orchestrator is running (PID {pid}, started {started_at}). "
                "Stop it before starting a new run."
            ),
        )
    except ProcessLookupError:
        # PID is dead — remove stale lock
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass
        return CheckResult(
            name="Single orchestrator guard",
            status="pass",
            message=f"Removed stale lock (PID {pid} is no longer running).",
        )
    except PermissionError:
        # PID exists but we cannot signal it — still alive
        return CheckResult(
            name="Single orchestrator guard",
            status="fail",
            message=f"Another orchestrator is running (PID {pid}, started {started_at}).",
            remediation=(
                f"Another orchestrator is running (PID {pid}, started {started_at}). "
                "Stop it before starting a new run."
            ),
        )


def _check_checkpoint_dir_writable(config: Config) -> CheckResult:
    """Check 10: Checkpoint dir writable."""
    checkpoint_dir = Path(config.checkpoint_dir).expanduser()

    try:
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            name="Checkpoint dir writable",
            status="fail",
            message=f"Cannot create checkpoint directory {checkpoint_dir}: {exc}.",
            remediation=(
                f"Cannot write to checkpoint directory {checkpoint_dir}: {exc}. "
                "Check permissions and disk space."
            ),
        )

    try:
        with tempfile.NamedTemporaryFile(dir=checkpoint_dir, prefix=".health-probe-", delete=True):
            pass
        return CheckResult(
            name="Checkpoint dir writable",
            status="pass",
            message=f"Checkpoint directory {checkpoint_dir} is writable.",
        )
    except OSError as exc:
        return CheckResult(
            name="Checkpoint dir writable",
            status="fail",
            message=f"Cannot write to checkpoint directory {checkpoint_dir}: {exc}.",
            remediation=(
                f"Cannot write to checkpoint directory {checkpoint_dir}: {exc}. "
                "Check permissions and disk space."
            ),
        )


def _check_log_dir_writable(config: Config) -> CheckResult:
    """Check 11: Log dir writable."""
    log_dir = Path(config.log_dir).expanduser() / "conductor"

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            name="Log dir writable",
            status="fail",
            message=f"Cannot create log directory {log_dir}: {exc}.",
            remediation=(
                f"Cannot write to log directory {log_dir}: {exc}. Check permissions and disk space."
            ),
        )

    try:
        with tempfile.NamedTemporaryFile(dir=log_dir, prefix=".health-probe-", delete=True):
            pass
        return CheckResult(
            name="Log dir writable",
            status="pass",
            message=f"Log directory {log_dir} is writable.",
        )
    except OSError as exc:
        return CheckResult(
            name="Log dir writable",
            status="fail",
            message=f"Cannot write to log directory {log_dir}: {exc}.",
            remediation=(
                f"Cannot write to log directory {log_dir}: {exc}. Check permissions and disk space."
            ),
        )


# ---------------------------------------------------------------------------
# Orchestrator lock management
# ---------------------------------------------------------------------------


def acquire_orchestrator_lock(config: Config, run_id: str) -> None:
    """Acquire the single-orchestrator lock file.

    Writes {pid, started_at, run_id} to the lock file atomically using
    os.replace(). Registers atexit and SIGTERM handlers for cleanup.

    Args:
        config: Resolved Config instance.
        run_id: Conductor run ID (UUID string).

    Raises:
        FatalHealthCheckError: If a live process already holds the lock.
    """
    global _lock_config
    lock_path = Path(config.checkpoint_dir).expanduser() / ".orchestrator.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Check for existing lock
    if lock_path.exists():
        try:
            lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
            pid = int(lock_data["pid"])
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            # Unreadable lock — remove and continue
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            if pid == os.getpid():
                # Current process already holds the lock — allow re-acquisition
                # (happens when run calls startup_sequence once per stage)
                pass
            else:
                try:
                    os.kill(pid, 0)
                    # PID alive — cannot acquire
                    raise FatalHealthCheckError(
                        f"Another orchestrator is running (PID {pid}). Stop it first."
                    )
                except ProcessLookupError:
                    # PID dead — remove stale lock and continue
                    try:
                        lock_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                except PermissionError:
                    # PID alive (no permission to signal)
                    raise FatalHealthCheckError(
                        f"Another orchestrator is running (PID {pid}). Stop it first."
                    )

    # Write lock atomically via tmp + os.replace
    lock_data = {
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
    }
    tmp_path = lock_path.parent / f".orchestrator.lock.tmp.{os.getpid()}"
    tmp_path.write_text(json.dumps(lock_data), encoding="utf-8")
    os.replace(tmp_path, lock_path)

    # Store config ref for SIGTERM handler
    _lock_config = config

    # Register cleanup
    atexit.register(release_orchestrator_lock, config)
    signal.signal(signal.SIGTERM, _sigterm_handler)


def release_orchestrator_lock(config: Config) -> None:
    """Release the single-orchestrator lock file if owned by this process.

    Only deletes the lock when the PID in the file matches os.getpid().
    Safe to call multiple times.

    Args:
        config: Resolved Config instance.
    """
    lock_path = Path(config.checkpoint_dir).expanduser() / ".orchestrator.lock"

    if not lock_path.exists():
        return

    try:
        lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(lock_data.get("pid", -1))
    except (OSError, json.JSONDecodeError, ValueError):
        return

    if pid != os.getpid():
        # Another process owns the lock — do not delete
        return

    lock_path.unlink(missing_ok=True)


def _sigterm_handler(signum: int, frame: object) -> None:
    """SIGTERM handler: release lock then re-raise with default disposition."""
    if _lock_config is not None:
        release_orchestrator_lock(_lock_config)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    sys.exit(128 + signal.SIGTERM)


# ---------------------------------------------------------------------------
# format_report
# ---------------------------------------------------------------------------

_STATUS_SYMBOLS: dict[str, str] = {
    "pass": "✓",
    "warn": "⚠",
    "fail": "✗",
    "skip": "-",
}

_SEPARATOR = "─" * 45


def format_report(report: HealthReport) -> str:
    """Return a formatted human-readable health report string.

    Args:
        report: The HealthReport to format.

    Returns:
        A multi-line formatted string suitable for printing to stdout.
    """
    lines: list[str] = [
        "brimstone health check",
        _SEPARATOR,
    ]

    warn_count = 0
    error_count = 0

    for check in report.checks:
        symbol = _STATUS_SYMBOLS[check.status]

        if check.status == "fail":
            lines.append(f"{symbol} FATAL: {check.name}")
            error_count += 1
        else:
            lines.append(f"{symbol} {check.name}")
            if check.status == "warn":
                warn_count += 1

        # Additional message lines (skip the first — already in the header)
        msg_lines = check.message.splitlines()
        for msg_line in msg_lines[1:]:
            lines.append(f"  {msg_line}")

        # Remediation
        if check.remediation is not None:
            for rem_line in check.remediation.splitlines():
                lines.append(f"  Fix: {rem_line}")

    lines.append(_SEPARATOR)

    overall_label = report.overall.upper()
    if report.overall == "pass":
        lines.append(f"Overall: {overall_label}")
    elif report.overall == "warn":
        lines.append(f"Overall: {overall_label} — {warn_count} warning(s), 0 error(s)")
    else:
        lines.append(f"Overall: {overall_label} — {warn_count} warning(s), {error_count} error(s)")

    return "\n".join(lines)
