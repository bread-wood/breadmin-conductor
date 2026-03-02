# LLD: Health Module

**Module:** `health`
**File:** `src/composer/health.py`
**Issue:** #113
**Status:** Draft
**Date:** 2026-03-02

---

## 1. Module Overview

The `health` module runs preflight checks before any worker or CLI command executes. It verifies that the environment is correctly configured, that no orphaned sessions are blocking the queue, and that the single-orchestrator invariant holds. Workers call `check_all()` on startup and abort if any fatal check fails. The `composer health` CLI command surfaces the same report to the operator.

**File path:** `src/composer/health.py`

**Exports:**

| Symbol | Kind | Consumers |
|--------|------|-----------|
| `CheckResult` | dataclass | `health`, `cli` |
| `HealthReport` | dataclass | `health`, `cli`, `runner` |
| `FatalHealthCheckError` | exception | `runner`, `cli` |
| `check_all` | function | `runner`, `cli` |
| `acquire_orchestrator_lock` | function | `runner` |
| `release_orchestrator_lock` | function | `runner` |

---

## 2. Dataclasses

### 2.1 `CheckResult`

Represents the outcome of a single preflight check.

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class CheckResult:
    name: str                                    # Human-readable check name
    status: Literal["pass", "warn", "fail", "skip"]  # Outcome
    message: str                                 # One-line summary of what was found
    remediation: str | None = None               # What the operator must do to fix a warn/fail
```

**Field notes:**

- `name`: Short label used in the health table, e.g. `"Git repo present"`.
- `status`: `"pass"` — check succeeded; `"warn"` — check found a problem but worker may proceed; `"fail"` — check found a blocking problem; `"skip"` — check was not run because an earlier fatal check failed.
- `message`: Always populated. For pass results, summarises what was confirmed (e.g. `"Authenticated as breadbot"`). For warn/fail, describes what was found (e.g. `"2 worktrees found under .claude/worktrees/"`).
- `remediation`: `None` on pass/skip. For warn/fail, a concrete action string, e.g. `"Run: git worktree remove --force .claude/worktrees/10-feat-config"`.

### 2.2 `HealthReport`

Aggregated result of `check_all()`.

```python
@dataclass(frozen=True)
class HealthReport:
    checks: list[CheckResult]                    # All checks in run order; skipped checks included
    overall: Literal["pass", "warn", "fail"]     # Worst status across all non-skip checks
    fatal: bool                                  # True if any check has status "fail"
```

**Derivation rules:**

- `overall` is derived from `checks` at construction time: if any check is `"fail"` → `"fail"`; else if any check is `"warn"` → `"warn"`; else `"pass"`. Skipped checks do not contribute.
- `fatal` is `True` if and only if `overall == "fail"`. It is a convenience field for workers that need a single boolean gate.

---

## 3. `check_all()` API

```python
def check_all(
    config: Config,
    checkpoint: Checkpoint | None = None,
) -> HealthReport:
    ...
```

**Parameters:**

- `config`: Resolved `Config` instance. Used to derive `data_dir` (which contains the checkpoint directory and lock file), `log_dir` (derived as `config.data_dir / "conductor"`), and `default_branch`.
- `checkpoint`: The current conductor `Checkpoint` object, or `None` if no checkpoint exists yet (first run). If `None`, the rate-limit backoff check (check 8) is skipped.

**Behaviour:**

1. Runs all 11 checks in the order defined in Section 4, collecting `CheckResult` objects.
2. After any check that returns `status="fail"`, marks all remaining checks as `CheckResult(name=..., status="skip", message="Skipped due to earlier fatal failure", remediation=None)` and stops running further checks.
3. Constructs and returns a `HealthReport` from the collected results.

**Short-circuit rule:** Only fatal checks trigger short-circuit. Warn results do not skip subsequent checks — all checks after a warn are still run.

**Side effects:** None. `check_all()` is read-only. It does not acquire the orchestrator lock, write to disk, or modify GitHub state. Lock acquisition is a separate step handled by `acquire_orchestrator_lock()` (Section 5).

---

## 4. Preflight Check Sequence

Checks run in this exact order. Each subsection specifies: detection method, pass/warn/fail classification, and remediation message.

### Check 1: Git repo present

| Field | Value |
|-------|-------|
| **Name** | `Git repo present` |
| **Detection** | `subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True)` in the current working directory |
| **Pass** | Exit code 0 — working directory is inside a git repo |
| **Fail** | Exit code non-zero — not a git repo |
| **Remediation** | `"Change to a git repository before running composer."` |

This is check 1 because all subsequent checks that use `git` commands depend on it. A fail here triggers short-circuit.

### Check 2: Default branch matches config

| Field | Value |
|-------|-------|
| **Name** | `Default branch matches config` |
| **Detection** | `subprocess.run(["gh", "repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"], capture_output=True)` |
| **Pass** | Output matches `config.default_branch` (stripped of whitespace) |
| **Warn** | Output does not match `config.default_branch` |
| **Remediation** | `f"Set CONDUCTOR_DEFAULT_BRANCH={actual_branch} or update config to match the repo's default branch ({actual_branch})."` |

If `gh` is not yet authenticated (check 3 not yet run), this check may fail with a `gh` auth error. In that case, return `warn` with message `"Could not verify default branch: gh returned an error (run 'gh auth login' first)."` rather than `fail`. This avoids a misleading cascade.

### Check 3: gh CLI authenticated

| Field | Value |
|-------|-------|
| **Name** | `gh CLI authenticated` |
| **Detection** | `subprocess.run(["gh", "auth", "status"], capture_output=True)` |
| **Pass** | Exit code 0 |
| **Fail** | Exit code non-zero |
| **Remediation** | `"Run: gh auth login"` |

A fail here triggers short-circuit. Checks 6, 7, and 8 all require `gh`.

### Check 4: ANTHROPIC_API_KEY present

| Field | Value |
|-------|-------|
| **Name** | `ANTHROPIC_API_KEY present` |
| **Detection** | `os.environ.get("ANTHROPIC_API_KEY", "")` — check that the variable exists and is non-empty |
| **Pass** | Variable is set and non-empty |
| **Fail** | Variable is absent or empty string |
| **Remediation** | `"Set the ANTHROPIC_API_KEY environment variable before running composer."` |

This check does not validate the key format or make an API call. Presence is sufficient for the preflight gate. A fail here triggers short-circuit.

### Check 5: No active worktrees

| Field | Value |
|-------|-------|
| **Name** | `No active worktrees` |
| **Detection** | `subprocess.run(["git", "worktree", "list", "--porcelain"], capture_output=True)`; parse output to collect all worktree paths; filter to paths under `<repo-root>/.claude/worktrees/` |
| **Pass** | Zero worktrees found under `.claude/worktrees/` |
| **Warn** | One or more worktrees found under `.claude/worktrees/` |
| **Remediation** | List each path with its age: `"Stale worktrees found. Remove with: git worktree remove --force <path>"`. List each worktree on its own line in the remediation string. |

**Message format for warn:**
```
2 worktrees found under .claude/worktrees/:
  .claude/worktrees/10-feat-config (3 days old)
  .claude/worktrees/16-llm-call (1 day old)
```

Age is computed as `now - path.stat().st_mtime`. If `st_mtime` is unavailable, omit the age annotation.

This check does not remove worktrees. Removal is the operator's responsibility (or the cleanup procedure in runner.py).

### Check 6: No stale in-progress issues

| Field | Value |
|-------|-------|
| **Name** | `No stale in-progress issues` |
| **Detection** | `gh issue list --label in-progress --state open --json number,title --limit 100` (JSON output); for each issue, check whether it has an open PR via `gh pr list --state open --json number,headRefName` — match by branch name convention `<issue-number>-*` |
| **Pass** | Zero in-progress issues with no associated open PR |
| **Warn** | One or more in-progress issues with no open PR, and count does not exceed `CONDUCTOR_MAX_ORPHANED` (default: 5) |
| **Fail** | Count of in-progress issues with no open PR exceeds `CONDUCTOR_MAX_ORPHANED` |
| **Remediation (warn)** | `"In-progress issues with no open PR: #<N> '<title>'. Inspect and remove the 'in-progress' label if stale."` |
| **Remediation (fail)** | `f"Too many orphaned in-progress issues ({count} > {max_orphaned}). Clear stale labels before starting a new run."` |

**`CONDUCTOR_MAX_ORPHANED`** is read from the environment at startup (default `5`). It is not stored in `Config` — it is read directly via `int(os.environ.get("CONDUCTOR_MAX_ORPHANED", "5"))` inside the check function.

The PR matching heuristic: an in-progress issue with number `N` has a corresponding PR if any open PR's `headRefName` starts with `str(N) + "-"`. This matches the branch naming convention `<N>-<short-slug>`.

### Check 7: Open PRs needing attention

| Field | Value |
|-------|-------|
| **Name** | `Open PRs needing attention` |
| **Detection** | `gh pr list --state open --json number,title,statusCheckRollup,reviewDecision --limit 50`; inspect `statusCheckRollup` for any check with `conclusion == "FAILURE"` or `conclusion == "CANCELLED"`; inspect `reviewDecision == "CHANGES_REQUESTED"` |
| **Pass** | No open PRs, or all open PRs have passing CI and no requested changes |
| **Warn** | One or more open PRs have failing CI or requested changes |
| **Remediation** | `"PRs needing attention: #<N> '<title>' [CI failing / changes requested]. Review before starting new work."` |

This check never fails — it is advisory. Worker proceeds regardless. The warn informs the operator that PRs are stalled and may need manual intervention before new work is dispatched.

### Check 8: Rate limit backoff active

| Field | Value |
|-------|-------|
| **Name** | `Rate limit backoff active` |
| **Detection** | If `checkpoint` is `None`, skip this check (`status="skip"`). Otherwise read `checkpoint.rate_limit_backoff_until`; compare to `datetime.now(timezone.utc)` |
| **Pass** | `checkpoint.rate_limit_backoff_until` is `None` or is in the past |
| **Warn** | `checkpoint.rate_limit_backoff_until` is in the future |
| **Remediation** | `f"Rate limit backoff active until {backoff_until.isoformat()}. Worker will pause until then."` |

The message for warn:
```
Rate limit backoff active. Resumes at 2026-03-02T19:45:00+00:00 (in 47 minutes).
```

Include the human-readable duration (rounded to nearest minute) alongside the ISO timestamp. This is a warn, not a fail — the worker respects the backoff internally and will wait rather than abort.

### Check 9: Single orchestrator guard

| Field | Value |
|-------|-------|
| **Name** | `Single orchestrator guard` |
| **Detection** | Check for lock file at `config.data_dir / ".orchestrator.lock"`. If absent → pass. If present → read PID from lock file → `os.kill(pid, 0)` to test liveness |
| **Pass** | Lock file absent, or lock file present but PID is dead (stale lock cleaned up automatically) |
| **Fail** | Lock file present and PID is alive |
| **Remediation** | `f"Another orchestrator is running (PID {pid}, started {started_at}). Stop it before starting a new run."` |

**Stale lock cleanup:** If the lock file exists but `os.kill(pid, 0)` raises `ProcessLookupError`, the PID is dead. Remove the lock file silently and return `pass` with message `"Removed stale lock (PID {pid} is no longer running)."`.

**Pass-through exception:** If `os.kill(pid, 0)` raises `PermissionError`, the PID is alive (we do not have permission to signal it, which implies it exists). Treat as fail.

Lock file details are specified in Section 5. This check does not acquire the lock — it only inspects it. A fail here triggers short-circuit.

### Check 10: Checkpoint dir writable

| Field | Value |
|-------|-------|
| **Name** | `Checkpoint dir writable` |
| **Detection** | `config.data_dir` directory: attempt `tempfile.NamedTemporaryFile(dir=config.data_dir, prefix=".health-probe-", delete=True)` |
| **Pass** | Temp file created and deleted without error |
| **Fail** | `OSError` raised (permission denied, directory does not exist and cannot be created, disk full) |
| **Remediation** | `f"Cannot write to checkpoint directory {config.data_dir}: {error}. Check permissions and disk space."` |

Directory creation: before the write probe, attempt `config.data_dir.mkdir(parents=True, exist_ok=True)`. If `mkdir` itself raises `OSError`, that is the fail condition. A fail here triggers short-circuit.

### Check 11: Log dir writable

| Field | Value |
|-------|-------|
| **Name** | `Log dir writable` |
| **Detection** | Same as check 10, but using `config.data_dir / "conductor"` as the probe directory |
| **Pass** | Temp file created and deleted without error |
| **Fail** | `OSError` raised |
| **Remediation** | `f"Cannot write to log directory {config.data_dir / 'conductor'}: {error}. Check permissions and disk space."` |

---

## 5. Single Orchestrator Guard

### 5.1 Lock File Format

Path: `config.data_dir / ".orchestrator.lock"`

Content: a single JSON object on one line:

```json
{"pid": 12345, "started_at": "2026-03-02T14:30:00+00:00", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"}
```

| Field | Type | Notes |
|-------|------|-------|
| `pid` | int | OS process ID of the orchestrator |
| `started_at` | string (ISO 8601 UTC) | Time the lock was acquired |
| `run_id` | string (UUID) | Conductor run ID from the checkpoint |

### 5.2 `acquire_orchestrator_lock(config, run_id)`

```python
def acquire_orchestrator_lock(config: Config, run_id: str) -> None:
    ...
```

Called by `runner.py` after `check_all()` passes, before beginning the pipeline stage.

**Behaviour:**

1. Check if `config.data_dir / ".orchestrator.lock"` exists.
2. If it exists, read and parse the JSON. Test the PID with `os.kill(pid, 0)`:
   - `ProcessLookupError` → PID dead → remove stale lock and continue.
   - `PermissionError` → PID alive → raise `FatalHealthCheckError("Another orchestrator is running (PID {pid}). Stop it first.")`.
3. Write the lock file with the current PID, `datetime.now(timezone.utc).isoformat()`, and `run_id`.
4. Register cleanup handlers:
   - `atexit.register(release_orchestrator_lock, config)` — handles normal exit.
   - `signal.signal(signal.SIGTERM, _sigterm_handler)` — calls `release_orchestrator_lock(config)` then re-raises with default signal disposition.

**Atomicity:** Use `os.replace()` (atomic rename) rather than direct open-and-write to avoid a race between two concurrent orchestrators both passing the existence check simultaneously. Write to a temp file in the same directory, then `os.replace(tmp, lock_path)`.

### 5.3 `release_orchestrator_lock(config)`

```python
def release_orchestrator_lock(config: Config) -> None:
    ...
```

**Behaviour:**

1. Path: `config.data_dir / ".orchestrator.lock"`.
2. Read the lock file. If `pid` in the lock file does not match `os.getpid()`, do nothing (another process owns the lock — do not delete it).
3. If PID matches, delete the file with `path.unlink(missing_ok=True)`.

**Idempotent:** safe to call multiple times (e.g. both `atexit` handler and `SIGTERM` handler fire). `missing_ok=True` prevents errors if the file was already removed.

### 5.4 SIGTERM Handler

```python
import signal
import sys


def _sigterm_handler(signum: int, frame) -> None:
    release_orchestrator_lock(_config)   # _config held as module-level state after acquire
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    sys.exit(128 + signal.SIGTERM)
```

The module stores the `Config` reference at module level (`_config: Config | None = None`) when `acquire_orchestrator_lock` is called, so the SIGTERM handler can reach it without a closure parameter.

---

## 6. Non-Fatal vs. Fatal Classification

| Check | Fail Severity | Fatal? |
|-------|--------------|--------|
| 1: Git repo present | fail | Yes — subsequent git commands will fail |
| 2: Default branch matches config | warn | No |
| 3: gh CLI authenticated | fail | Yes — subsequent gh commands will fail |
| 4: ANTHROPIC_API_KEY present | fail | Yes — claude -p will fail immediately |
| 5: No active worktrees | warn | No |
| 6: No stale in-progress issues (below threshold) | warn | No |
| 6: No stale in-progress issues (above threshold) | fail | Yes — queue state is too corrupted to proceed safely |
| 7: Open PRs needing attention | warn | No |
| 8: Rate limit backoff active | warn | No |
| 9: Single orchestrator guard | fail | Yes — two orchestrators would corrupt shared state |
| 10: Checkpoint dir writable | fail | Yes — checkpoint writes will fail at runtime |
| 11: Log dir writable | fail | Yes — log writes will fail at runtime |

**Fatal checks trigger short-circuit:** after any fatal fail, remaining checks are marked `"skip"` and `check_all()` returns immediately with `report.fatal = True`.

**Warn checks never trigger short-circuit:** all checks after a warn are still run. The operator receives the full picture even when warnings are present.

**Threshold for check 6:** the transition from warn to fail is controlled by `CONDUCTOR_MAX_ORPHANED`. Default is `5`. When the count of in-progress issues with no open PR exceeds this value, the check returns `fail`. The fail is fatal because dispatching new work on top of many orphaned issues risks further label pollution and lost state.

---

## 7. `composer health` Output Format

### 7.1 Human-Readable Table (default)

```
breadmin-composer health check
─────────────────────────────────────────────
✓ Git repo present
⚠ Default branch matches config
  Mismatch: config=main, repo=master
  Fix: Set CONDUCTOR_DEFAULT_BRANCH=master or update config.
✓ gh CLI authenticated
✓ ANTHROPIC_API_KEY present
⚠ No active worktrees
  2 worktrees found under .claude/worktrees/:
    .claude/worktrees/10-feat-config (3 days old)
    .claude/worktrees/16-llm-call (1 day old)
  Fix: git worktree remove --force <path>
✓ No stale in-progress issues
✓ Open PRs needing attention
✓ Rate limit backoff active
✓ Single orchestrator guard
✓ Checkpoint dir writable
✓ Log dir writable
─────────────────────────────────────────────
Overall: WARN — 2 warning(s), 0 error(s)
```

**Symbols:**

| Symbol | Status |
|--------|--------|
| `✓` | pass |
| `⚠` | warn |
| `✗` | fail |
| `-` | skip |

**Indentation:** checks that have a non-None `message` body (beyond the one-liner) indent it by 2 spaces. The `remediation` string (if present) is prefixed with `  Fix: `.

**Line separator:** a horizontal rule of `─` characters, 45 characters wide.

**Summary line format:**

- `Overall: PASS` — all checks passed
- `Overall: WARN — N warning(s), 0 error(s)` — at least one warn, no fails
- `Overall: FAIL — N warning(s), M error(s)` — at least one fail

### 7.2 JSON Output (`--json` flag)

```
composer health --json
```

Outputs the `HealthReport` serialised to JSON. The schema:

```json
{
  "overall": "warn",
  "fatal": false,
  "checks": [
    {
      "name": "Git repo present",
      "status": "pass",
      "message": "Working directory is inside a git repo.",
      "remediation": null
    },
    {
      "name": "No active worktrees",
      "status": "warn",
      "message": "2 worktrees found under .claude/worktrees/: .claude/worktrees/10-feat-config (3 days old), .claude/worktrees/16-llm-call (1 day old)",
      "remediation": "Remove stale worktrees: git worktree remove --force <path>"
    }
  ]
}
```

Use `json.dumps(dataclasses.asdict(report), indent=2)`. The `HealthReport.checks` list preserves run order.

### 7.3 Exit Codes

| Exit Code | Condition |
|-----------|-----------|
| 0 | `report.overall` is `"pass"` or `"warn"` |
| 1 | `report.overall` is `"fail"` (i.e. `report.fatal` is `True`) |

Workers use the same exit code convention: a fatal health report causes `sys.exit(1)`.

---

## 8. Integration with Worker Startup

Every worker entry point (`impl-worker`, `research-worker`, `design-worker`) calls `check_all()` as its first action, before any GitHub API calls or subprocess spawning.

### 8.1 Startup Sequence

```python
from composer.health import check_all, acquire_orchestrator_lock, FatalHealthCheckError
from composer.config import Config
from composer.session import Checkpoint


def run_worker(config: Config, checkpoint: Checkpoint | None) -> None:
    # Step 1: Run health checks
    report = check_all(config, checkpoint)

    # Step 2: Handle report
    if report.fatal:
        _print_health_report(report)
        raise FatalHealthCheckError("Health check failed. Fix the errors above and retry.")

    if report.overall == "warn":
        _print_health_report(report)
        log_conductor_event(...)   # Log warnings to conductor log; continue

    # Step 3: Acquire orchestrator lock (after checks pass)
    acquire_orchestrator_lock(config, run_id=checkpoint.run_id if checkpoint else str(uuid4()))

    # Step 4: Begin pipeline stage ...
```

### 8.2 `FatalHealthCheckError`

```python
class FatalHealthCheckError(RuntimeError):
    """Raised when check_all() returns a report with fatal=True."""
    pass
```

Workers catch `FatalHealthCheckError` at the top-level entry point and exit with code 1. They do not log a traceback — the health report printed before the raise is sufficient for operator diagnosis.

### 8.3 Warn Behaviour

When `report.overall == "warn"` and `report.fatal == False`:

1. Print the full health report table to stdout (same format as `composer health`).
2. Log each warn check to the conductor log with `event_type="health_warn"` and the check's `name`, `message`, and `remediation` in the payload.
3. Continue execution — do not raise, do not pause.

### 8.4 Pass Behaviour

When `report.overall == "pass"`:

1. Do not print anything to stdout (silent pass).
2. Log a single `event_type="health_pass"` entry to the conductor log with `payload={"check_count": len(report.checks)}`.
3. Continue execution.

---

## 9. Interface Summary

### 9.1 All Public Symbols

| Symbol | Kind | Signature |
|--------|------|-----------|
| `CheckResult` | dataclass | `(name, status, message, remediation=None)` |
| `HealthReport` | dataclass | `(checks, overall, fatal)` |
| `FatalHealthCheckError` | exception | `RuntimeError` subclass |
| `check_all` | function | `(config: Config, checkpoint: Checkpoint | None = None) -> HealthReport` |
| `acquire_orchestrator_lock` | function | `(config: Config, run_id: str) -> None` |
| `release_orchestrator_lock` | function | `(config: Config) -> None` |

### 9.2 Consumer Call Map

| Consumer | Symbol used | When |
|----------|-------------|------|
| `runner.py` (all workers) | `check_all`, `acquire_orchestrator_lock` | First action on worker startup, before any pipeline work |
| `runner.py` (all workers) | `release_orchestrator_lock` | Via `atexit` and SIGTERM handler registered by `acquire_orchestrator_lock` |
| `cli.py` (`composer health`) | `check_all` | On `composer health` invocation; outputs result and sets exit code |
| `runner.py` | `FatalHealthCheckError` | Caught at top-level entry point; triggers `sys.exit(1)` |

### 9.3 What This Module Does NOT Do

- Does not modify GitHub state (no label changes, no issue edits).
- Does not remove worktrees or clean up orphaned branches.
- Does not acquire the orchestrator lock — that is `acquire_orchestrator_lock()`, called separately after `check_all()` passes.
- Does not configure logging — it calls `log_conductor_event` but does not set up the logger.
- Does not make API calls to Anthropic or Claude Code — only `git`, `gh`, and OS-level checks.
- Does not read the checkpoint file — `checkpoint` is passed in by the caller.

---

## 10. Cross-References

- **`docs/research/14-hang-detection.md` §7.3**: Defines the orphaned worktree detection pattern (`git worktree list` + cross-referencing running PIDs) that check 5 implements. The lock file PID check in check 9 extends this pattern to the orchestrator process itself.
- **`docs/research/08-usage-scheduling.md` §5.4**: Defines the rate-limit backoff state stored in the checkpoint (`rate_limit_backoff_until`), which check 8 reads. The governor's post-429 requeue logic is what sets this field.
- **`docs/research/19-pretooluse-reliability.md` §6**: The revised security architecture requires a smoke test before relying on hooks. An optional check 12 (hook smoke test) may be added in a future issue; it is out of scope for the current milestone.
- **`src/composer/config.py`**: `Config.data_dir` is the root from which the lock file path (`data_dir / ".orchestrator.lock"`), checkpoint directory (`data_dir`), and log directory (`data_dir / "conductor"`) are derived.
- **`src/composer/runner.py`**: Primary caller of `check_all()` and `acquire_orchestrator_lock()`. Owns the worker lifecycle and is responsible for catching `FatalHealthCheckError`.
- **`src/composer/cli.py`**: Calls `check_all()` for the `composer health` command; formats and prints the `HealthReport`; sets process exit code.
- **`docs/design/lld/logger.md` §5**: The `health_warn` and `health_pass` conductor log events (Section 8.3–8.4) are written using `log_conductor_event`. Their `payload` schemas should be added to the conductor log event type table in that document.
