# LLD: Session Module

**Module:** `session`
**File:** `src/composer/session.py`
**Issue:** #111
**Status:** Draft
**Date:** 2026-03-02

---

## 1. Module Overview

The `session` module provides durable orchestration state for the conductor. Its single responsibility is to serialize and recover the conductor's in-flight work across restarts, crashes, and rate-limit backoffs. It does not dispatch agents, manage GitHub labels, or interpret CI results — those concerns live in `runner.py` and `cli.py`. The session module purely owns the checkpoint lifecycle: create, load, save, and recover.

**File path:** `src/composer/session.py`

**Exports:**

| Symbol | Kind | Consumers |
|--------|------|-----------|
| `Checkpoint` | dataclass | `runner`, `cli` |
| `IssueState` | dataclass | `runner`, `cli` |
| `new` | function | `cli` (worker startup) |
| `load` | function | `cli` (worker startup), `runner` |
| `save` | function | `runner`, `cli` |
| `recover` | function | `cli` (startup reconciliation) |
| `set_backoff` | function | `runner` (on 429) |
| `is_backing_off` | function | `runner` (pre-dispatch guard) |
| `clear_backoff` | function | `runner` (post-backoff dispatch) |
| `record_dispatch` | function | `runner` (on agent launch) |
| `is_agent_hung` | function | `runner` (monitor loop) |

---

## 2. Checkpoint Schema

### 2.1 Field-by-Field Documentation

The checkpoint is a single JSON file written atomically to disk. It captures the complete orchestrator state at the most recent committed step.

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | `int` | Bumped on any breaking field change. Current value: `1`. The loader rejects files with a higher version and warns on a lower version. |
| `run_id` | `str` | UUID (v4) generated once at orchestrator startup via `uuid.uuid4()`. Stable for the entire conductor run — survives restarts by being reloaded from the checkpoint. Also used as the filename base for the conductor JSONL log. |
| `session_id` | `str` | The `session_id` from the most recent `system/init` stream-json event of the orchestrator's own `claude -p` invocation. Changes on each orchestrator invocation. Sub-agent session IDs are not stored here — they live in the per-session JSONL logs. |
| `repo` | `str` | GitHub repo in `"owner/repo"` format, e.g. `"myorg/breadmin-composer"`. Set once at startup, never mutated. |
| `default_branch` | `str` | Default branch name, e.g. `"main"`. Resolved at startup via `gh repo view`. |
| `milestone` | `str` | Active milestone name, e.g. `"MVP Implementation"`. Set at startup from the CLI argument. |
| `stage` | `str` | Pipeline stage for the active milestone. One of `"research"`, `"design"`, `"impl"`. |
| `timestamp` | `str` | ISO 8601 UTC timestamp, updated on every `save()` call. Used to detect stale checkpoints at startup. |
| `claimed_issues` | `dict[str, str]` | Maps issue number (as string key) to the branch name created for that issue. Example: `{"7": "7-inbound-outbound-messages"}`. An issue appearing here means it has been assigned and labelled `in-progress` on GitHub. |
| `active_worktrees` | `list[str]` | Branch names (not full paths) of worktrees that are currently checked out under `.claude/worktrees/`. A branch appearing here means a `git worktree add` was successfully executed. |
| `open_prs` | `dict[str, int]` | Maps branch name to PR number for PRs that are open and waiting for CI or review. Example: `{"7-inbound-outbound-messages": 42}`. |
| `completed_prs` | `list[int]` | PR numbers that have been squash-merged in this run. Accumulated monotonically; never removed. |
| `rate_limit_backoff_until` | `str \| null` | ISO 8601 UTC timestamp until which all dispatch is suspended, or `null` when not in backoff. Set by `set_backoff()`, cleared by `clear_backoff()`. |
| `retry_counts` | `dict[str, int]` | Maps issue number (string key) to the number of dispatch attempts that have failed for that issue. A fresh dispatch increments this by 1. Reset to 0 on successful PR creation. |
| `last_error` | `dict \| null` | Structured record of the most recent error that caused a state change. Schema: `{"code": int \| null, "subtype": str, "message": str, "context": dict}`. `null` when the last operation succeeded. |
| `dispatch_times` | `dict[str, str]` | Maps branch name to ISO 8601 UTC timestamp when the agent for that branch was most recently dispatched. Used by the hang watchdog in `is_agent_hung()`. |

### 2.2 Full Example: Three-Issue Dispatch

One issue completed (PR merged), one in-flight (PR open, CI pending), one pending dispatch (claimed but agent not yet started):

```json
{
  "schema_version": 1,
  "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "session_id": "f7e6d5c4-b3a2-9180-fedc-ba9876543210",
  "repo": "myorg/breadmin-composer",
  "default_branch": "main",
  "milestone": "MVP Implementation",
  "stage": "impl",
  "timestamp": "2026-03-02T15:12:44Z",
  "claimed_issues": {
    "7": "7-inbound-outbound-messages",
    "12": "12-error-taxonomy",
    "16": "16-llm-call-dataclass"
  },
  "active_worktrees": [
    "12-error-taxonomy"
  ],
  "open_prs": {
    "12-error-taxonomy": 55
  },
  "completed_prs": [42],
  "rate_limit_backoff_until": null,
  "retry_counts": {
    "7": 0,
    "12": 1,
    "16": 0
  },
  "last_error": {
    "code": 1,
    "subtype": "error_during_execution",
    "message": "Agent for issue #12 exited with error on first attempt",
    "context": {
      "issue_number": 12,
      "branch": "12-error-taxonomy",
      "exit_code": 1
    }
  },
  "dispatch_times": {
    "12-error-taxonomy": "2026-03-02T15:00:00Z"
  }
}
```

State interpretation for this example:

- Issue **#7** (`7-inbound-outbound-messages`): PR #42 is merged (`completed_prs`). No worktree. Done.
- Issue **#12** (`12-error-taxonomy`): First agent attempt failed (`retry_counts["12"] == 1`, `last_error` records the event). A second agent was dispatched at 15:00:00Z (`dispatch_times`). PR #55 is open and under CI (`open_prs`). The worktree is active (`active_worktrees`).
- Issue **#16** (`16-llm-call-dataclass`): Claimed (`claimed_issues`), but no worktree and no PR. Pending agent dispatch — orchestrator will dispatch next.

---

## 3. Issue State Machine

### 3.1 States

| State | Description |
|-------|-------------|
| `unclaimed` | Issue exists in the milestone but the orchestrator has not yet acted on it. |
| `claimed` | Issue has been assigned `@me` and labelled `in-progress` on GitHub. Branch has been created and pushed to `origin`. |
| `dispatched` | Agent subprocess has been spawned for this issue. `dispatch_times` entry exists. |
| `pr_open` | Agent completed successfully. PR is open on GitHub. CI and/or review in progress. |
| `ci_pass` | All CI checks on the PR have passed. PR is ready to merge. |
| `merged` | PR has been squash-merged. Issue label removed. Worktree deleted. |
| `failed` | Agent exited with a non-success result, or watchdog killed the agent. Retry is scheduled subject to `CONDUCTOR_MAX_RETRIES`. |
| `abandoned` | `retry_count >= CONDUCTOR_MAX_RETRIES`. Human escalation required. Branch is preserved. Label `abandoned` applied. No further automated action. |

### 3.2 Valid Transitions

| From | To | Triggering Event |
|------|----|-----------------|
| `unclaimed` | `claimed` | Orchestrator calls `gh issue edit --add-label in-progress` and creates branch |
| `claimed` | `dispatched` | `record_dispatch()` called; agent subprocess spawned |
| `dispatched` | `pr_open` | Agent creates PR; conductor calls `gh pr list --head <branch>` and finds open PR |
| `dispatched` | `failed` | Agent exits with non-zero code, non-success subtype, or watchdog fires (`is_agent_hung()` returns `True`) |
| `pr_open` | `ci_pass` | `gh pr checks <PR>` returns all checks passed |
| `pr_open` | `failed` | CI fails definitively (not flaky); human review requests blocking changes |
| `ci_pass` | `merged` | `gh pr merge --squash --delete-branch` succeeds |
| `failed` | `claimed` | `retry_count < CONDUCTOR_MAX_RETRIES`; orchestrator re-dispatches from the `claimed` state (branch already exists) |
| `failed` | `abandoned` | `retry_count >= CONDUCTOR_MAX_RETRIES` |

### 3.3 ASCII State Diagram

```
                    ┌───────────────────────────────────────────────────┐
                    │ retry_count < CONDUCTOR_MAX_RETRIES               │
                    │ (re-dispatch from claimed state)                  │
                    │                                                   │
   ┌───────────┐   │  ┌─────────┐   agent    ┌────────────┐           │
   │ unclaimed │──────▶│ claimed │──spawned──▶│ dispatched │──────┐    │
   └───────────┘      └─────────┘            └────────────┘      │    │
   (issue exists                                  │               │    │
   in milestone,                            agent creates         │    │
   not yet acted                              PR on GitHub        │    │
   on)                                           │            watchdog │
                                                 │            fires or │
                                                 ▼           non-zero  │
                                           ┌──────────┐      exit code │
                                           │ pr_open  │           │    │
                                           └──────────┘           │    │
                                                 │                │    │
                                          all CI checks           │    │
                                           pass                   ▼    │
                                                 │           ┌────────┐│
                                                 ▼           │ failed │┘
                                           ┌──────────┐      └────────┘
                                           │ ci_pass  │           │
                                           └──────────┘     retry_count
                                                 │          >= MAX
                                         gh pr merge              │
                                         --squash                 ▼
                                                 │          ┌──────────┐
                                                 ▼          │ abandoned│
                                           ┌──────────┐     │(terminal)│
                                           │  merged  │     └──────────┘
                                           │(terminal)│
                                           └──────────┘
```

The issue state is not stored as an explicit field in the checkpoint. It is derived at runtime by inspecting `claimed_issues`, `active_worktrees`, `open_prs`, `completed_prs`, `retry_counts`, and querying GitHub live during the recovery phase. This avoids state-machine synchronization bugs between the checkpoint and GitHub.

---

## 4. Read/Write API

### 4.1 `new(repo, milestone, stage) -> Checkpoint`

Creates a fresh checkpoint with a new `run_id`. Called once at orchestrator startup when no existing checkpoint is found (or when the user explicitly clears the checkpoint).

```python
def new(repo: str, milestone: str, stage: str) -> Checkpoint:
```

**Behaviour:**
1. Generates `run_id = str(uuid.uuid4())`.
2. Constructs `Checkpoint` with all list/dict fields empty and `rate_limit_backoff_until = None`, `last_error = None`.
3. Sets `timestamp = datetime.now(timezone.utc).isoformat()`.
4. Does **not** write to disk — caller must call `save()`.

### 4.2 `load(path: Path) -> Checkpoint | None`

Reads and deserialises the checkpoint file.

```python
def load(path: Path) -> Checkpoint | None:
```

**Behaviour:**
1. If `path` does not exist, returns `None`. This is the normal first-run case — not an error.
2. Opens and parses the file as JSON.
3. If `schema_version` in the file is greater than the module's `SCHEMA_VERSION` constant, raises `CheckpointVersionError` with a message instructing the user to upgrade `breadmin-composer`.
4. If `schema_version` is less than `SCHEMA_VERSION`, logs a warning and attempts forward-migration (see Section 4.5).
5. Constructs and returns the `Checkpoint` dataclass from the parsed dict.
6. **On corrupt JSON** (i.e., any `json.JSONDecodeError`): logs the error to stderr with the exact parse exception message, then raises `CheckpointCorruptError`. The caller must catch this, prompt the human to delete the checkpoint file and restart, and exit. Do not silently overwrite a corrupt checkpoint — it may represent unrecovered in-flight work.

### 4.3 `save(checkpoint: Checkpoint, path: Path) -> None`

Atomically writes the checkpoint to disk.

```python
def save(checkpoint: Checkpoint, path: Path) -> None:
```

**Behaviour:**
1. Updates `checkpoint.timestamp = datetime.now(timezone.utc).isoformat()`.
2. Serialises the checkpoint to a JSON string (indent=2 for human readability).
3. Writes to a temporary file at `path.with_suffix(".tmp")` in the same directory as `path`.
4. Calls `os.replace(tmp_path, path)` to atomically rename the temp file over the target. On POSIX (Linux, macOS), `os.replace` is atomic within the same filesystem — no reader will ever see a partial file.
5. Calls `log_conductor_event` with event type `checkpoint_write` (see `logger.py` Section 5.2).

**Error handling:** `OSError` on disk-full or permission errors propagates to the caller. The temp file is left on disk in this case — the original checkpoint is unmodified.

### 4.4 `Checkpoint` Dataclass

```python
from dataclasses import dataclass, field

@dataclass
class Checkpoint:
    schema_version: int
    run_id: str
    session_id: str
    repo: str
    default_branch: str
    milestone: str
    stage: str
    timestamp: str
    claimed_issues: dict[str, str] = field(default_factory=dict)
    active_worktrees: list[str] = field(default_factory=list)
    open_prs: dict[str, int] = field(default_factory=dict)
    completed_prs: list[int] = field(default_factory=list)
    rate_limit_backoff_until: str | None = None
    retry_counts: dict[str, int] = field(default_factory=dict)
    last_error: dict | None = None
    dispatch_times: dict[str, str] = field(default_factory=dict)
```

### 4.5 Schema Migration

When `schema_version` in the file is lower than the current `SCHEMA_VERSION`:

1. Load the raw dict.
2. Apply migration functions in sequence. Each migration function takes a raw dict and returns a mutated dict. Example: `_migrate_v0_to_v1(data: dict) -> dict` adds the `dispatch_times` field with a default of `{}` if absent.
3. Set `schema_version` to the current value.
4. Construct and return the `Checkpoint`.
5. The migrated checkpoint is **not** automatically written back to disk. The caller's next `save()` call will persist it.

### 4.6 Corrupt Checkpoint: Human Escalation Path

When `load()` raises `CheckpointCorruptError`, the caller (worker CLI command) must:

```
ERROR: Checkpoint at <path> is corrupt and cannot be parsed.
       This may indicate a partial write or disk error.

       To inspect the file manually:
           cat <path>

       If you are sure the file is unrecoverable, delete it and restart:
           rm <path>

       WARNING: Deleting the checkpoint without reviewing it may leave
       GitHub issues labelled in-progress with no corresponding agent.
       Run the following to audit before deleting:
           gh issue list --state open --label in-progress --repo <repo>
```

The CLI then exits with code 2. It does not attempt to delete or overwrite the corrupt file.

---

## 5. Recovery Protocol

Recovery runs at orchestrator startup, before any new issues are claimed or agents dispatched. It reconciles the checkpoint's claimed state against live GitHub and git state.

### 5.1 Decision Tree Pseudocode

```
procedure recover(checkpoint, repo, data_dir):

    for each (issue_number, branch) in checkpoint.claimed_issues:

        pr = gh_pr_for_branch(branch, repo)
        branch_on_remote = remote_branch_exists(branch, repo)
        worktree_path = Path(".claude/worktrees") / branch
        worktree_exists = worktree_path.exists()
        has_commits = worktree_exists and worktree_has_commits_beyond_base(worktree_path, checkpoint.default_branch)

        # Case 1: PR exists and is merged
        if pr and pr.state == "merged":
            if pr.number not in checkpoint.completed_prs:
                checkpoint.completed_prs.append(pr.number)
            if branch in checkpoint.open_prs:
                del checkpoint.open_prs[branch]
            if branch in checkpoint.active_worktrees:
                checkpoint.active_worktrees.remove(branch)
            git_worktree_remove(worktree_path)       # no-op if already removed
            gh_remove_label(issue_number, "in-progress", repo)
            log_conductor_event("recovery", "orphan_auto_resolved", {
                "condition": "pr_merged",
                "issue_number": issue_number,
                "branch": branch,
                "pr_number": pr.number
            })
            continue

        # Case 2: PR exists and is open
        if pr and pr.state == "open":
            if branch not in checkpoint.open_prs:
                checkpoint.open_prs[branch] = pr.number
            # Continue monitoring; no cleanup needed
            log_conductor_event("recovery", "orphan_auto_resolved", {
                "condition": "pr_open_continue",
                "issue_number": issue_number,
                "branch": branch,
                "pr_number": pr.number
            })
            continue

        # Case 3: No PR, but branch has commits in worktree
        if not pr and has_commits:
            # Ambiguous state: agent may have been killed before running gh pr create.
            # Preserve all work; mark abandoned; human must review.
            gh_add_label(issue_number, "abandoned", repo)
            gh_remove_label(issue_number, "in-progress", repo)
            del checkpoint.claimed_issues[issue_number]
            if branch in checkpoint.active_worktrees:
                checkpoint.active_worktrees.remove(branch)
            log_conductor_event("recovery", "human_escalate", {
                "condition": "no_pr_with_commits",
                "issue_number": issue_number,
                "branch": branch,
                "action_required": "Review worktree at .claude/worktrees/{branch}; create PR manually or delete branch."
            })
            continue

        # Case 4: No PR, worktree exists but no commits beyond base
        if not pr and worktree_exists and not has_commits:
            # Agent started but produced no work. Safe to clean up.
            git_worktree_remove(worktree_path)
            if branch in checkpoint.active_worktrees:
                checkpoint.active_worktrees.remove(branch)
            # Issue remains in claimed_issues; orchestrator will re-dispatch
            log_conductor_event("recovery", "orphan_auto_resolved", {
                "condition": "no_pr_no_commits_worktree_exists",
                "issue_number": issue_number,
                "branch": branch,
                "action_taken": "worktree removed; issue remains claimed"
            })
            continue

        # Case 5: No PR, no worktree at all (stale label only)
        if not pr and not worktree_exists:
            if branch_on_remote:
                # Branch pushed but no PR and no local worktree.
                # Most likely the worktree was already cleaned up but gh pr create never ran.
                # Issue remains in claimed_issues; orchestrator will re-dispatch.
                log_conductor_event("recovery", "orphan_auto_resolved", {
                    "condition": "no_pr_no_worktree_branch_on_remote",
                    "issue_number": issue_number,
                    "branch": branch,
                    "action_taken": "will re-dispatch from existing branch"
                })
            else:
                # Nothing on remote and no worktree: stale in-progress label.
                gh_remove_label(issue_number, "in-progress", repo)
                gh_remove_assignee(issue_number, "@me", repo)
                del checkpoint.claimed_issues[issue_number]
                log_conductor_event("recovery", "orphan_auto_resolved", {
                    "condition": "stale_label",
                    "issue_number": issue_number,
                    "branch": branch,
                    "action_taken": "removed in-progress label; removed from checkpoint"
                })
            continue

    # Check for active_worktrees not in claimed_issues (orphaned from a previous run)
    for branch in list(checkpoint.active_worktrees):
        if not any(b == branch for b in checkpoint.claimed_issues.values()):
            worktree_path = Path(".claude/worktrees") / branch
            pr = gh_pr_for_branch(branch, repo)
            if pr and pr.state == "merged":
                git_worktree_remove(worktree_path)
                checkpoint.active_worktrees.remove(branch)
            elif is_stale_worktree(worktree_path):
                git_worktree_remove(worktree_path)
                checkpoint.active_worktrees.remove(branch)
                log_conductor_event("recovery", "orphan_auto_resolved", {
                    "condition": "stale_unclaimed_worktree",
                    "branch": branch
                })

    save(checkpoint, checkpoint_path)
```

### 5.2 Staleness Threshold

A worktree is considered stale when it has received no new commits for longer than `CONDUCTOR_STALE_WORKTREE_DAYS` (default: 3 days).

```python
def is_stale_worktree(worktree_path: Path) -> bool:
    """Return True if the worktree's latest commit is older than the staleness threshold."""
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "log", "-1", "--format=%ct"],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return True  # Can't read git log — treat as stale
    try:
        last_commit_ts = int(result.stdout.strip())
    except ValueError:
        return True
    threshold_days = int(os.environ.get("CONDUCTOR_STALE_WORKTREE_DAYS", "3"))
    age_seconds = time.time() - last_commit_ts
    return age_seconds > threshold_days * 86400
```

### 5.3 Human Escalation Trigger

When `retry_counts[issue_number] >= CONDUCTOR_MAX_RETRIES` (default: 3), the orchestrator must not attempt another dispatch. Instead:

1. Call `gh issue edit <N> --add-label abandoned --remove-label in-progress --repo <repo>`.
2. Log a `human_escalate` event to the conductor log with `reason` and `action_required`.
3. Remove the issue from `checkpoint.claimed_issues`.
4. Call `save()`.
5. Continue with the remaining issues in the run.

The `CONDUCTOR_MAX_RETRIES` threshold is read from the environment:

```python
MAX_RETRIES = int(os.environ.get("CONDUCTOR_MAX_RETRIES", "3"))
```

---

## 6. Backoff State

Rate-limit backoff suspends all agent dispatch until the backoff window expires. The checkpoint persists the backoff deadline so that a restarted orchestrator correctly waits rather than immediately hammering the API.

### 6.1 Setting Backoff

Called by `runner.py` when a 429 response (or `billing_error` subtype from the stream-json result) is received from the runner:

```python
def set_backoff(checkpoint: Checkpoint, duration_seconds: int, attempt: int) -> None:
    """
    Set the backoff deadline using exponential backoff.

    Formula: min(CONDUCTOR_BACKOFF_BASE_SECONDS * 2^attempt, CONDUCTOR_BACKOFF_MAX_MINUTES * 60)
    The 'duration_seconds' parameter is the base; 'attempt' is the zero-indexed retry attempt.
    """
    base = int(os.environ.get("CONDUCTOR_BACKOFF_BASE_SECONDS", "60"))
    max_s = int(os.environ.get("CONDUCTOR_BACKOFF_MAX_MINUTES", "30")) * 60
    wait = min(base * (2 ** attempt), max_s)
    until = datetime.now(timezone.utc) + timedelta(seconds=wait)
    checkpoint.rate_limit_backoff_until = until.isoformat()
```

The exponential backoff formula: `wait = min(base * 2^attempt, max_seconds)`

| Attempt | Base=60s | Effective Wait |
|---------|----------|----------------|
| 0 | 60 s | 60 s |
| 1 | 60 s | 120 s |
| 2 | 60 s | 240 s |
| 3 | 60 s | 480 s |
| 4 | 60 s | 900 s (capped at CONDUCTOR_BACKOFF_MAX_MINUTES=30min → 1800 s cap) |

### 6.2 Checking Backoff

Called at the top of each monitor loop iteration before dispatching any new agents:

```python
def is_backing_off(checkpoint: Checkpoint) -> bool:
    """Return True if the backoff deadline is in the future."""
    if checkpoint.rate_limit_backoff_until is None:
        return False
    until = datetime.fromisoformat(checkpoint.rate_limit_backoff_until)
    return datetime.now(timezone.utc) < until
```

When `is_backing_off()` returns `True`, the orchestrator must skip dispatch for all pending issues and sleep until the deadline before checking again.

### 6.3 Clearing Backoff

Called after a successful agent dispatch following a backoff period:

```python
def clear_backoff(checkpoint: Checkpoint) -> None:
    """Clear the backoff deadline after a successful dispatch post-backoff."""
    checkpoint.rate_limit_backoff_until = None
```

`clear_backoff()` is called only after the dispatched agent's `system/init` event is received — confirming the API accepted the new session. If the very first event after a backoff is another 429, `set_backoff()` is called again with `attempt + 1`.

---

## 7. Hang Detection

The conductor's monitor loop must detect agents that have silently stopped producing output (Pattern P2 and P4 from `docs/research/14-hang-detection.md`) and agents that have produced their final `result` event but not exited (Pattern P3).

### 7.1 Recording Dispatch Time

When an agent is dispatched, the conductor calls `record_dispatch()` to stamp the branch in the checkpoint:

```python
def record_dispatch(checkpoint: Checkpoint, branch: str) -> None:
    """Record the time an agent was dispatched for a branch."""
    checkpoint.dispatch_times[branch] = datetime.now(timezone.utc).isoformat()
```

This is called immediately after the agent subprocess is spawned, before the `system/init` event is received.

### 7.2 Hang Detection Check

On each monitor loop iteration (typically every 30–60 seconds), the orchestrator calls:

```python
def is_agent_hung(checkpoint: Checkpoint, branch: str) -> bool:
    """
    Return True if the agent for 'branch' has been running longer than
    CONDUCTOR_AGENT_TIMEOUT_MINUTES without completing.
    """
    dispatch_time_str = checkpoint.dispatch_times.get(branch)
    if dispatch_time_str is None:
        return False  # Not yet dispatched; not hung
    dispatch_time = datetime.fromisoformat(dispatch_time_str)
    timeout_minutes = int(os.environ.get("CONDUCTOR_AGENT_TIMEOUT_MINUTES", "60"))
    elapsed = datetime.now(timezone.utc) - dispatch_time
    return elapsed.total_seconds() > timeout_minutes * 60
```

`CONDUCTOR_AGENT_TIMEOUT_MINUTES` defaults to 60. This maps to the T2 (total session timeout) tier defined in `docs/research/14-hang-detection.md` Section 5.1.

Note: The asyncio watchdog in `runner.py` enforces T1 (inactivity, 300 s) and T3 (post-result, 60 s) at the subprocess level without going through the checkpoint. `is_agent_hung()` is the checkpoint-level guard for the monitor loop's coarser-grained view — it catches cases where the watchdog itself fails or the runner process dies without updating state.

### 7.3 On Timeout

When `is_agent_hung()` returns `True` for a branch:

1. Log an `agent_hang` event to the conductor log:
   ```python
   log_conductor_event(run_id, "monitor", "agent_hang", {
       "branch": branch,
       "issue_number": issue_number,
       "dispatch_time": checkpoint.dispatch_times[branch],
       "elapsed_minutes": elapsed_minutes,
   }, data_dir=data_dir)
   ```
2. Treat the issue as `failed`: increment `retry_counts[issue_number]` by 1.
3. Remove `branch` from `checkpoint.active_worktrees` and `checkpoint.dispatch_times`.
4. Remove the worktree with `git worktree remove --force`.
5. Remove the `in-progress` label from the GitHub issue.
6. If `retry_counts[issue_number] < CONDUCTOR_MAX_RETRIES`, re-dispatch the agent from the `claimed` state (branch already exists on origin).
7. If `retry_counts[issue_number] >= CONDUCTOR_MAX_RETRIES`, escalate to `abandoned` (Section 5.3).
8. Call `save()`.

---

## 8. Interface Summary

### 8.1 All Public Symbols

| Symbol | Kind | Signature |
|--------|------|-----------|
| `SCHEMA_VERSION` | `int` | Module constant; current value `1` |
| `CheckpointVersionError` | exception | Raised when checkpoint `schema_version > SCHEMA_VERSION` |
| `CheckpointCorruptError` | exception | Raised when checkpoint JSON is unparseable |
| `Checkpoint` | dataclass | See Section 4.4 |
| `new` | function | `(repo: str, milestone: str, stage: str) -> Checkpoint` |
| `load` | function | `(path: Path) -> Checkpoint \| None` |
| `save` | function | `(checkpoint: Checkpoint, path: Path) -> None` |
| `recover` | function | `(checkpoint: Checkpoint, repo: str, data_dir: Path) -> None` |
| `set_backoff` | function | `(checkpoint: Checkpoint, duration_seconds: int, attempt: int) -> None` |
| `is_backing_off` | function | `(checkpoint: Checkpoint) -> bool` |
| `clear_backoff` | function | `(checkpoint: Checkpoint) -> None` |
| `record_dispatch` | function | `(checkpoint: Checkpoint, branch: str) -> None` |
| `is_agent_hung` | function | `(checkpoint: Checkpoint, branch: str) -> bool` |

### 8.2 Consumer Call Map

| Consumer | Function | When |
|----------|----------|------|
| `cli.py` (worker startup) | `load` | At the very start of `impl_worker`, `research_worker`, `design_worker` — before any GitHub API calls |
| `cli.py` (worker startup) | `new` | When `load` returns `None` (first run or cleared checkpoint) |
| `cli.py` (worker startup) | `recover` | Immediately after `load` or `new`, before claiming any issues |
| `cli.py` (worker issue-claim loop) | `save` | After each issue is labelled `in-progress` and branch is pushed |
| `runner.py` (dispatch) | `record_dispatch` | Immediately after spawning each agent subprocess |
| `runner.py` (dispatch) | `save` | After `record_dispatch`, to persist the updated `dispatch_times` |
| `runner.py` (stream monitor) | `set_backoff` | When a 429 or `billing_error` result is received |
| `runner.py` (stream monitor) | `clear_backoff` | After a successful agent `system/init` following a backoff period |
| `runner.py` (stream monitor) | `save` | After `set_backoff` or `clear_backoff` |
| `runner.py` (monitor loop) | `is_backing_off` | At the top of each dispatch iteration |
| `runner.py` (monitor loop) | `is_agent_hung` | For each active branch on each monitor loop tick |
| `runner.py` (PR created) | `save` | After updating `checkpoint.open_prs` with the new PR number |
| `runner.py` (merge) | `save` | After moving PR number from `open_prs` to `completed_prs` |
| `health.py` | `load` | Reads `checkpoint.timestamp` and `completed_prs` to report run health |

### 8.3 Checkpoint File Location

The checkpoint file path is not managed by `session.py` — it is resolved by the caller from `Config`:

```python
checkpoint_path = config.data_dir / "conductor" / f"{run_id}.checkpoint.json"
```

`Config.data_dir` defaults to `~/.local/share/composer` and is overridden by `CONDUCTOR_DATA_DIR`. On the first call to `new()`, the `run_id` is freshly generated. On subsequent runs, `run_id` is loaded from the existing checkpoint, so the checkpoint path is stable across restarts within the same logical run.

### 8.4 What This Module Does NOT Do

- Does not call `gh`, `git`, or any subprocess directly. Recovery helpers (`git_worktree_remove`, `gh_remove_label`, etc.) are passed in by the caller or implemented in `runner.py` as thin wrappers. This keeps `session.py` testable without shell access.
- Does not manage the conductor JSONL log. It calls `log_conductor_event` (from `logger.py`) for checkpoint-related events, but does not own the log file.
- Does not implement the asyncio watchdog (T1, T3 tiers). That lives in `runner.py`. `session.py` owns only the checkpoint-level hang detection (`is_agent_hung`) for the monitor loop's T2 tier.
- Does not store sub-agent session IDs. Those are written to the per-session JSONL log by `logger.py`.

---

## 9. Cross-References

- **`docs/research/02-session-continuity.md`**: Section 1.3 (Stateless Multiple Calls with External Checkpoint) is the primary design authority for the checkpoint pattern. Section 5.1 defines the original checkpoint schema; this LLD refines and expands it with `dispatch_times`, `retry_counts`, `last_error`, and `default_branch` fields. Section 6.3 (Recovery Protocol) is the direct ancestor of the decision tree in Section 5.1 above.
- **`docs/research/14-hang-detection.md`**: Section 5 (Three-Tier Timeout Model) is the basis for Section 7. The T2 (total session timeout) tier maps to `is_agent_hung()`. T1 and T3 are the `runner.py` asyncio watchdog and are outside this module's scope.
- **`docs/research/07-skill-adaptation.md`**: Section 2.1 (Auto-resolve policy for orphaned work) defines the decision table that the `recover()` function implements. The five conditions in that table map exactly to the five cases in Section 5.1 above.
- **`src/composer/logger.py`** / **`docs/design/lld/logger.md`**: The `checkpoint_write` event type (Section 5.2 of the logger LLD) is written by `session.save()`. The `human_escalate` event type is written by `session.recover()` and the hang escalation path.
- **`src/composer/config.py`**: `Config.data_dir` determines the checkpoint file path. `CONDUCTOR_MAX_RETRIES`, `CONDUCTOR_STALE_WORKTREE_DAYS`, `CONDUCTOR_AGENT_TIMEOUT_MINUTES`, `CONDUCTOR_BACKOFF_BASE_SECONDS`, and `CONDUCTOR_BACKOFF_MAX_MINUTES` are environment variables read directly by `session.py` at call time (not stored in `Config`, to avoid requiring a `Config` instance in every function).
- **`src/composer/runner.py`**: Primary caller for `set_backoff`, `clear_backoff`, `record_dispatch`, `is_backing_off`, `is_agent_hung`, and `save`. The runner owns the asyncio subprocess lifecycle and the per-agent stream monitor; it calls into `session.py` to persist state transitions.
