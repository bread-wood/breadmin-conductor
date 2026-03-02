# Research: Error Handling and Failure Recovery in Headless Mode

**Issue**: #3
**Milestone**: M1: Foundation
**Status**: Complete
**Date**: 2026-03-02
**Depends on**: #2 (session continuity), #8 (usage scheduling), #14 (hang detection), #23 (429 error cap distinction)

---

## Executive Summary

Conductor must handle failures at two levels: the `claude -p` subprocess (exit codes, stream-json
error events, signals) and the orchestration workflow (orphaned GitHub labels, stale worktrees,
unmerged branches, partial PR creation). The core finding is that exit code alone is insufficient
for recovery decisions — the `result.subtype` field in stream-json output is the authoritative
signal for classifying subprocess failures. Rate limit errors (429) are a special case requiring
requeue rather than abandon, with classification of 5-hour vs. weekly exhaustion type determining
the backoff duration. Orphaned state cleanup must be idempotent and run on every conductor startup.
CI failure handling requires a bounded retry policy with human escalation as the terminal action.

---

## 1. Exit Code Taxonomy

### 1.1 Exit Code Overview

`claude -p` exits with a small set of codes. The taxonomy below synthesizes the official CLI
reference, SDK type documentation (Elixir SDK `ClaudeCode.Types`, v0.21.0), community reports,
and the SFEIR Institute CI/CD headless mode analysis:

| Exit Code | Meaning | Source | Conductor Recovery Action |
|-----------|---------|--------|--------------------------|
| `0` | Success — task completed normally | Official | Mark PR ready; proceed to monitor/merge |
| `1` | General error — API error, authentication failure, execution error, rate limit, or logic error | Official | Inspect `result.subtype` and stderr; classify; see Section 2 |
| `2` | Authentication error — missing or invalid API key | SFEIR/community | Fatal: halt dispatch; alert operator; do not retry |
| `124` | Killed by `timeout(1)` command (wall-clock timeout) | POSIX/community | Clean up orphaned state; retry once with longer timeout |
| `130` | SIGINT — user Ctrl+C or conductor sent SIGINT | POSIX | Clean up; do not retry (operator interrupted) |
| `137` | SIGKILL — forced kill (OOM, conductor escalation) | POSIX | Clean up orphaned state; retry if cause is transient |
| `143` | SIGTERM — conductor watchdog kill or spontaneous internal timeout (Pattern P5 from #14) | POSIX/community | Clean up; retry if subtype absent; see Section 3.3 |

**Critical limitation**: Exit code `1` is overloaded. It covers rate limits, implementation
failures, max-turns exhaustion, max-budget exhaustion, tool errors, and auth token expiry. The
conductor must parse the `result.subtype` field from stream-json output and/or stderr text to
distinguish these cases before choosing a recovery action.

Exit code `2` for authentication errors is documented by SFEIR Institute as a distinct code but
**not confirmed** in the official CLI reference as of March 2026 — the official reference only
documents `0` (success) and `1` (error) for `-p` mode. Treat exit code `2` conservatively as
authentication failure when observed, but do not rely on it as a guaranteed discriminator.

### 1.2 Internal Result Subtypes (Exit Code 1)

When `--output-format stream-json` is used, the final `result` event carries a `subtype` field
that identifies the cause of a non-zero exit. These subtypes are documented in the Elixir
`ClaudeCode.Types` SDK, confirmed in community reports, and alluded to in the official stream-json
output documentation:

| `result.subtype` | Exit Code | Meaning | Recovery |
|-----------------|-----------|---------|----------|
| `"success"` | `0` | Task completed normally | Proceed |
| `"error_max_turns"` | `1` | `--max-turns` limit hit — too many agentic iterations | Increase `--max-turns` or split the issue; do not retry as-is |
| `"error_max_budget_usd"` | `1` | `--max-budget-usd` cap exceeded | Increase budget cap or split the task |
| `"error_during_execution"` | `1` | Unhandled execution error during a tool call or model step | Inspect stderr; may retry once if transient |
| `"error_during_operation"` | `1` | Rate limit or API error mid-operation | Parse stderr/result text for rate limit signals; see Section 5 |
| `"error_max_structured_output_retries"` | `1` | Structured output validation failed after max retries | Not applicable to conductor's unstructured output mode |
| `"sigterm_internal"` | `143` | Spontaneous SIGTERM before any result was emitted (synthesized by conductor watchdog) | Treat as transient; retry once with a backoff |

**Note**: The `"error_during_operation"` subtype is the form that appears when rate limiting fires
mid-session (as documented in #23 and #08). The `result.result` text field in this case contains
`"API Error: Rate limit reached"`. The conductor must check both the subtype and the text to
correctly route this case to the rate-limit handler rather than the generic error handler.

### 1.3 Exit Code Mapping Diagram

```
claude -p exits
│
├── exit 0
│   └── subtype: "success" → SUCCESS PATH
│
├── exit 1
│   ├── subtype: "error_max_turns" → ABANDON (task too large; file new issue)
│   ├── subtype: "error_max_budget_usd" → ABANDON or reconfigure
│   ├── subtype: "error_during_operation"
│   │   ├── result text contains "rate_limit" → RATE LIMIT PATH (Section 5)
│   │   └── result text contains other error → TRANSIENT ERROR PATH
│   └── subtype: "error_during_execution" → TRANSIENT or PERMANENT (inspect stderr)
│
├── exit 2 → AUTHENTICATION FAILURE (halt; alert)
│
├── exit 124 → TIMEOUT (watchdog: clean + retry once)
│
├── exit 130 → USER INTERRUPT (clean; no retry)
│
├── exit 137 → FORCED KILL (clean; retry if transient)
│
└── exit 143
    ├── result emitted before kill → treat as subtype from result
    └── no result emitted → "sigterm_internal" → TRANSIENT (retry once)
```

---

## 2. Structured Error Output: stream-json Event Schema

### 2.1 Event Stream Overview

`claude -p --output-format stream-json` produces newline-delimited JSON (NDJSON). Every line is a
complete JSON object. The conductor must consume and parse this stream in real time (not after the
process exits) to implement the watchdog (doc #14) and to capture the result subtype even when the
process is killed before it exits naturally.

The event types (confirmed in official SDK documentation and community reports):

| `type` | `subtype` | When emitted | Key fields |
|--------|-----------|--------------|------------|
| `system` | `init` | Session start | `session_id`, `tools` (available tools list) |
| `assistant` | — | Model response | `message.content` (array of text/tool_use blocks) |
| `user` | — | Tool result injected back to model | `message.content` (tool_result blocks) |
| `stream_event` | various | Per-token streaming events (requires `--include-partial-messages`) | `event.delta` |
| `result` | `success` / `error_*` | Session end | `is_error`, `subtype`, `result` (text), `session_id`, `total_cost_usd`, `num_turns`, `usage` |

### 2.2 The `result` Event Schema

The `result` event is the single authoritative event for determining completion status. Its full
schema (synthesized from the official SDK streaming output docs, Elixir SDK types, and community
reports):

```json
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "result": "Task completed. PR #42 created at https://...",
  "session_id": "2ab1d239-9581-4d03-a895-af10c9fcb863",
  "total_cost_usd": 0.0847,
  "num_turns": 23,
  "duration_ms": 184302,
  "usage": {
    "input_tokens": 42180,
    "output_tokens": 8234,
    "cache_read_input_tokens": 31200,
    "cache_creation_input_tokens": 0
  }
}
```

For error cases, `is_error` is `true` and `subtype` is one of the `error_*` values:

```json
{
  "type": "result",
  "subtype": "error_during_operation",
  "is_error": true,
  "result": "API Error: Rate limit reached",
  "session_id": "...",
  "total_cost_usd": 0.0,
  "num_turns": 5,
  "usage": { "input_tokens": 12400, "output_tokens": 340, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0 }
}
```

**Important**: `total_cost_usd` and `usage` are reliable even when `is_error: true`. The cost
ledger (doc #05) should record all invocations, including errored ones, since they consume quota.

### 2.3 Parsing the Result Event

The conductor's result parser should handle three failure modes:

1. **Process exits, result event received**: The `result` event contains all needed classification
   information. Parse `subtype` and `result` text.

2. **Process killed before result event**: No `result` event arrives. The conductor must synthesize
   a subtype from the exit code and elapsed time (e.g., `"sigterm_internal"` for exit 143 without
   a result event).

3. **Known bug — result event missing**: GitHub Issue #8126 documents a case where the result event
   is not emitted even on success. In this case, the conductor should:
   - Check whether a PR exists for the branch (`gh pr list --head <branch>`)
   - If yes: treat as success
   - If no and exit was 0: treat as partial completion (see Section 4)

```python
from dataclasses import dataclass
from typing import Literal

ResultSubtype = Literal[
    "success",
    "error_max_turns",
    "error_max_budget_usd",
    "error_during_execution",
    "error_during_operation",
    "sigterm_internal",
    "watchdog_inactivity",
    "watchdog_total",
    "watchdog_post_result",
    "missing_result_event",
    "unknown",
]

@dataclass
class WorkerResult:
    exit_code: int
    subtype: ResultSubtype
    result_text: str | None
    is_error: bool
    total_cost_usd: float
    num_turns: int
    session_id: str | None
    stderr_text: str | None
    pr_url: str | None  # populated by post-exit reconciliation
```

### 2.4 Error Classification from Result Text

When `subtype == "error_during_operation"` or `subtype == "error_during_execution"`, the
`result` text provides additional classification context. Key patterns to match:

| Pattern in `result` text or stderr | Classification | Handler |
|------------------------------------|---------------|---------|
| `"rate_limit_error"` or `"Rate limit reached"` | Rate limit (5-hour or weekly) | Rate limit handler (Section 5) |
| `"Invalid API key"` or `"authentication"` | Auth failure | Halt dispatch; alert operator |
| `"content filtering"` or `"safety"` | Content refusal | File new issue for human review; do not retry |
| `"model is overloaded"` or `"overloaded_error"` | Transient model overload | Exponential backoff; retry up to 3 times |
| `"context_length_exceeded"` | Context window exceeded | Do not retry; split task or reduce scope |
| `"tool execution failed"` | Tool error (Bash, file I/O) | Inspect worktree; retry once if environment issue |
| No recognizable pattern | Unknown error | Log for human review; abandon issue |

---

## 3. Retry vs. Abandon Policy

### 3.1 Decision Framework

The retry-vs-abandon decision follows a two-axis classification: **error permanence** (transient vs.
permanent) and **progress made** (none vs. partial vs. complete). The combination determines the
recovery action.

```
Error Permanence × Progress Made Matrix
═══════════════════════════════════════════════════════════════
               │ No Progress     │ Partial Progress │ Complete
───────────────┼─────────────────┼──────────────────┼─────────
Transient      │ Retry w/ backoff│ Retry from clean │ Verify PR;
               │                 │ state            │ treat as success
───────────────┼─────────────────┼──────────────────┼─────────
Permanent      │ Abandon issue   │ Abandon; preserve│ N/A (not
               │ (unclaim)       │ partial work     │ an error)
═══════════════════════════════════════════════════════════════
```

### 3.2 Transient Errors (Retry Eligible)

Retry these errors with exponential backoff:

| Error | Max Retries | Initial Backoff | Notes |
|-------|-------------|-----------------|-------|
| Model overload (`overloaded_error`) | 3 | 30 s | Backoff: `min(3600, 30 × 2^attempt) + jitter(25%)` |
| Internal SIGTERM (exit 143, no result) | 1 | 60 s | Pattern P5 from #14 — often rate-related or OAuth refresh |
| Watchdog inactivity kill (hang) | 1 | 120 s | Retry once with increased `CONDUCTOR_INACTIVITY_TIMEOUT_S` |
| Watchdog total-time kill | 1 | 0 (immediate) | Retry once; may need to split the issue |
| Subprocess SIGKILL (exit 137) | 1 | 30 s | OOM kill; check available memory |
| Tool execution error (isolated environment) | 2 | 15 s | Environment may have been corrupted; fresh worktree |
| Network timeout during API call | 3 | 30 s | Transient connectivity |

### 3.3 Permanent Errors (Abandon)

Do NOT retry these errors. Unclaim the issue and file a human-review note:

| Error | Action |
|-------|--------|
| `error_max_turns` | Abandon issue; file a follow-up issue to split the task |
| `error_max_budget_usd` | Abandon issue; review budget configuration |
| Content safety refusal | Abandon issue; file a note for human review |
| Authentication failure (exit 2) | Halt all dispatch; alert operator |
| `context_length_exceeded` | Abandon issue; task scope too large for one agent |
| Unknown error (3rd consecutive failure) | Abandon issue; escalate to human |

### 3.4 Retry Safety Conditions

Before retrying, the conductor must verify:

1. **Clean state**: Remove the orphaned worktree and re-create it from the branch tip. Do not retry
   in a potentially dirty worktree — the previous run may have left partially-applied changes.
2. **No PR created**: If a PR was created by the previous run, the issue does not need to be
   re-dispatched; the orchestrator should inspect the existing PR's CI status instead.
3. **In-progress label still set**: If the label was removed during cleanup, re-apply it before
   re-dispatching.
4. **Retry count not exceeded**: Track retry count in the checkpoint. Do not exceed `MAX_RETRIES`
   (recommended default: 2) for any single issue.

### 3.5 Exponential Backoff Formula

For transient retries (excluding rate limit exhaustion, which uses the reset timestamp):

```python
import random
import math

def backoff_seconds(
    attempt: int,
    base_delay: float = 30.0,
    max_delay: float = 3600.0,
    jitter_factor: float = 0.25,
) -> float:
    """
    Compute backoff delay for attempt N (0-indexed).
    Uses full jitter (AWS Builders Library recommendation) to prevent
    thundering herd when multiple workers hit errors simultaneously.
    """
    exponential = min(max_delay, base_delay * (2 ** attempt))
    # Full jitter: random value in [0, exponential]
    return random.uniform(0, exponential)
```

This matches the AWS Builders Library recommendation for preventing thundering herd in distributed
systems with concurrent workers.

---

## 4. Orphaned State Inventory and Cleanup

### 4.1 What Gets Orphaned

When an agent fails — whether by error exit, signal, or watchdog kill — four categories of state
can be left orphaned. The conductor must detect and clean up all four on startup and after each
worker failure:

#### 4.1.1 Git Worktrees

**What it is**: A git worktree created at `.claude/worktrees/<branch>/` for the agent to work in.

**Orphan condition**: The `claude -p` worker process is dead but the worktree directory still
exists on disk.

**Detection**:
```bash
git worktree list
```
Cross-reference against PID files written at dispatch time (recommended: write
`<worktree_path>/.conductor-pid` with the worker subprocess PID). If the PID is not running and
the worktree exists, it is orphaned.

**Cleanup**:
```bash
git worktree remove --force .claude/worktrees/<branch>
```
If `git worktree remove` fails (common when the directory was partially created or the branch
reference is broken):
```bash
rm -rf .claude/worktrees/<branch>
git worktree prune
```

**Important**: A worktree with committed changes will NOT be removed by default — `--force` is
required. After force-removal, `git worktree prune` updates the git index.

#### 4.1.2 Feature Branches on `origin`

**What it is**: A branch pushed to `origin` (e.g., `origin/7-my-feature`) during agent execution.

**Orphan condition**: Branch exists on origin but has no open PR and the issue is no longer
in-progress (or the agent failed before creating a PR).

**Detection**:
```bash
gh pr list --head <branch> --state open --repo <owner/repo>
```
If this returns empty and the branch exists on origin, and the corresponding issue has no open PR,
the branch may be orphaned.

**Cleanup policy**: Do NOT automatically delete remote branches without a PR. The branch may
contain useful partial work. Instead:
1. Check if any commits were made beyond the base branch.
2. If commits exist: create a draft PR to preserve the work and note the failure in the PR body.
3. If no commits exist (branch is identical to the default branch): delete the remote branch.

```bash
# Check for commits beyond mainline
git fetch origin
COMMITS=$(git rev-list --count origin/mainline..origin/<branch>)
if [ "$COMMITS" -eq 0 ]; then
  git push origin --delete <branch>
fi
```

#### 4.1.3 GitHub Issue `in-progress` Labels

**What it is**: The `in-progress` label applied to a GitHub issue when the conductor claims it.

**Orphan condition**: Issue has `in-progress` label, has no open PR, and no worker subprocess is
actively running for it.

**Detection**:
```bash
gh issue list --state open --label in-progress --repo <owner/repo> --json number,title
```
Cross-reference each result against:
1. Open PRs: `gh pr list --state open --repo <owner/repo> --json headRefName`
2. Active worker PIDs in the checkpoint file

**Cleanup**:
```bash
gh issue edit <N> \
  --remove-label in-progress \
  --remove-assignee @me \
  --repo <owner/repo>
```
This must be idempotent — removing a label that is already removed should not error. Use
`--remove-label` rather than `--label` (which would replace all labels).

#### 4.1.4 Open Draft PRs from Failed Agents

**What it is**: A PR in `DRAFT` state created by an agent that subsequently failed (e.g., CI
never ran because the agent crashed after creating the PR but before pushing a complete
implementation).

**Orphan condition**: PR is in draft state, its issue is labeled `in-progress`, and no worker is
actively running for it.

**Detection**:
```bash
gh pr list --state open --draft --repo <owner/repo> --json number,headRefName,url
```

**Cleanup policy**: Do not automatically close draft PRs. Convert them to a comment on the
original issue explaining the failure:
```bash
gh pr comment <pr-number> --body "Agent failed before completing this PR. Conductor will re-dispatch."
```
Then convert back to draft if not already, re-dispatch, and push new commits to the same branch.
If re-dispatch also fails, close the draft PR and unclaim the issue.

### 4.2 Startup Reconciliation Protocol

On every conductor startup, before dispatching any new work:

```python
async def reconcile_on_startup(
    checkpoint: dict,
    repo: str,
    worktree_base: Path,
) -> None:
    """
    Reconcile checkpoint state with live GitHub and git state.
    Run before any new work is dispatched.
    """

    # 1. Audit worktrees
    existing_worktrees = git_worktree_list()  # parse `git worktree list --porcelain`
    for wt in existing_worktrees:
        if wt.path.is_relative_to(worktree_base):
            branch = wt.branch
            pr = await gh_pr_for_branch(branch, repo)
            pid_file = wt.path / ".conductor-pid"

            if pid_file.exists():
                pid = int(pid_file.read_text().strip())
                if not process_is_alive(pid):
                    # Worker died; the worktree is orphaned
                    if pr and pr["state"] == "open":
                        # PR exists — CI will run; monitor it
                        logger.info("worktree %s has open PR %s; no cleanup needed", wt.path, pr["url"])
                    elif pr and pr["state"] == "merged":
                        # Work completed — clean up worktree and label
                        await gh_remove_label(pr["issue_number"], "in-progress", repo)
                        git_worktree_remove(wt.path)
                    else:
                        # No PR — worker failed before creating PR
                        logger.warning("orphaned worktree %s with no PR; cleaning up", wt.path)
                        git_worktree_remove(wt.path)
                        # Issue will be retried or abandoned based on retry count

    # 2. Audit in-progress issues
    in_progress = await gh_list_issues(repo, label="in-progress")
    open_pr_branches = {pr["headRefName"] for pr in await gh_list_prs(repo, state="open")}

    for issue in in_progress:
        branch = branch_for_issue(issue["number"], checkpoint)
        if branch and branch not in open_pr_branches:
            retry_count = checkpoint.get("retry_counts", {}).get(str(issue["number"]), 0)
            if retry_count >= MAX_RETRIES:
                # Permanent failure — unclaim
                await gh_remove_label(issue["number"], "in-progress", repo)
                logger.error(
                    "issue %d: max retries (%d) exceeded; unclaiming",
                    issue["number"], MAX_RETRIES
                )
            else:
                # Re-dispatch
                logger.info("issue %d: retrying (attempt %d)", issue["number"], retry_count + 1)
                await re_dispatch_agent(issue, branch, checkpoint)
```

### 4.3 Cleanup Idempotency Requirements

All cleanup operations must be safe to run multiple times. Specifically:

- `git worktree remove --force` on a non-existent path must not fail (check existence first)
- `gh issue edit --remove-label` on an issue without the label must not fail
- `git push origin --delete <branch>` on a non-existent branch must not fail (use `--quiet`)
- `git worktree prune` is always safe to run and should be called after any worktree modification

---

## 5. Rate Limit Handling

### 5.1 Detection Signals

Rate limit events from `claude -p` produce the following observable signals (from #08 and #23):

1. **Exit code**: `1` (generic — not specific to rate limits)
2. **Result subtype**: `"error_during_operation"` (when rate limit fires mid-session)
3. **Result text**: `"API Error: Rate limit reached"`
4. **Stderr**: May contain `"You've hit your limit · resets <timestamp>"` (confirmed in interactive
   mode; unconfirmed in headless `-p` mode — see Section 8.1)
5. **HTTP 429** or possibly **HTTP 402** (for weekly cap on Max plans — see #23 and #64)

The conductor must check signals 2 and 3 together (both the subtype and the result text) because
subtype `"error_during_operation"` can also arise from non-rate-limit API errors.

### 5.2 Rate Limit Classification

As documented in #23, the 429 payload does not distinguish between a 5-hour window exhaustion and
a weekly cap exhaustion. The conductor should use the three-stage classification procedure from
that document:

1. **Try `/api/oauth/usage` endpoint** (OAuth session token required): check `five_hour.utilization`
   and `seven_day.utilization`. If either is ≥ 95%, classify accordingly.
2. **Parse stderr reset timestamp**: if the reset time is > 48 hours away, classify as weekly cap.
3. **Fallback by consecutive count**: if 4+ consecutive 429s occur across retry attempts, reclassify
   as weekly cap.

The `ErrorClass` hierarchy for conductor:

```python
class ErrorClass(Enum):
    SUCCESS = "success"
    # Rate limit sub-types
    RATE_LIMIT_FIVE_HOUR = "rate_limit_five_hour"
    RATE_LIMIT_SEVEN_DAY = "rate_limit_seven_day"
    RATE_LIMIT_SEVEN_DAY_OPUS = "rate_limit_seven_day_opus"
    RATE_LIMIT_AMBIGUOUS = "rate_limit_ambiguous"
    # Transient errors
    MODEL_OVERLOAD = "model_overload"
    NETWORK_TIMEOUT = "network_timeout"
    TOOL_EXECUTION_ERROR = "tool_execution_error"
    INTERNAL_SIGTERM = "sigterm_internal"
    WATCHDOG_KILL = "watchdog_kill"
    # Permanent errors
    AUTH_FAILURE = "auth_failure"
    MAX_TURNS = "max_turns"
    MAX_BUDGET = "max_budget"
    CONTENT_REFUSAL = "content_refusal"
    CONTEXT_EXCEEDED = "context_exceeded"
    IMPLEMENTATION_FAILURE = "implementation_failure"
    # Unknown
    UNKNOWN = "unknown"
```

### 5.3 Drain-and-Requeue Pattern

When a rate limit event is confirmed, the conductor must:

1. **Requeue the issue**: Remove `in-progress` label; reset to `open`. The issue must not remain
   stuck in-progress during the backoff window.

2. **Drain active agents**: Allow currently-running workers to complete their current work. Do not
   kill them — they may finish successfully before the rate limit affects them. Do not dispatch
   new workers during backoff.

3. **Calculate backoff window**:
   - For 5-hour window: backoff until `five_hour.resets_at + 5 minutes` (from OAuth endpoint) or
     `parse_reset_from_stderr(stderr) + 5 minutes` (from stderr heuristic)
   - For weekly cap: backoff until `seven_day.resets_at + 5 minutes`; alert operator
   - For ambiguous/fallback: exponential backoff with formula:
     `min(3600, 30 * 2^consecutive_count) + jitter(25%)`

4. **Store backoff state in checkpoint**:
   ```json
   {
     "rate_limit_backoff_until": "2026-03-02T19:35:00Z",
     "rate_limit_class": "five_hour",
     "consecutive_rate_limit_count": 1
   }
   ```

5. **Alert operator** (via structlog event, Telegram notification if configured):
   - For weekly cap: emit `rate_limit_weekly_cap_exhausted` event with reset time
   - For 5-hour window: emit `rate_limit_five_hour_exhausted` event

6. **Resume after backoff**: After the backoff window expires, run a probe invocation to verify
   the account is unblocked before resuming full dispatch:
   ```bash
   claude -p "OK" --max-turns 1 --output-format json
   ```
   If the probe succeeds (exit 0, subtype `"success"`), resume dispatch. If the probe also 429s,
   double the backoff window and retry the probe.

### 5.4 Pre-Dispatch Gate

Before dispatching each new agent, the usage governor (from #08) must check:

```python
async def can_dispatch(self, agent_type: AgentType) -> bool:
    if self.active_count >= self.config.max_concurrency:
        return False
    if self.is_in_backoff():
        return False
    estimated_cost = COST_ESTIMATES[agent_type]
    if self.estimated_budget_remaining < estimated_cost:
        return False
    return True
```

If `can_dispatch` returns `False` due to backoff, the issue stays in the `deferred_queue` and is
not unclaimed — it remains in-progress on GitHub but no worker runs until the backoff clears.

---

## 6. CI Failure Handling

### 6.1 Post-PR CI Lifecycle

After an agent successfully creates a PR, the conductor polls CI status:

```bash
gh pr checks <PR-number> --repo <owner/repo>
```

The typical CI check states reported by `gh`:
- `queued` — not yet started
- `in_progress` — currently running
- `success` — passed
- `failure` — failed
- `cancelled` — cancelled (manual or timeout)
- `skipped` — skipped by CI condition

### 6.2 CI Retry Policy

| CI Check State | Conductor Action |
|----------------|-----------------|
| `success` | Proceed to merge decision |
| `failure` (first time) | Wait `CI_RETRY_BACKOFF_S` (default: 120 s); trigger re-run via `gh pr checks <N> --repo <R> --watch` and if failed again, dispatch a fix-agent |
| `failure` (fix-agent dispatched) | Wait for fix-agent result; if fix-agent also fails, escalate to human |
| `failure` (human escalation) | Remove `in-progress` label; post PR comment; leave PR open for human review |
| `cancelled` | Trigger re-run once; if cancelled again, treat as failure |
| `in_progress` | Wait; re-poll after `CI_POLL_INTERVAL_S` (default: 60 s) |

**Retry limit**: Maximum 1 fix-agent dispatch per PR. If the fix-agent fails to resolve CI, the
conductor escalates to human review.

### 6.3 Fix-Agent Dispatch for CI Failures

When CI fails after PR creation, the conductor can dispatch a narrow fix-agent:

```bash
claude -p "CI failed on PR #<N> for branch <branch>. Read the CI failure log:
$(gh pr checks <N> --repo <repo> | grep failure)
Fix the failure within the allowed scope. Push fixes to the same branch. Do not create a new PR." \
  --allowedTools "Bash,Read,Edit,Glob,Grep,Write" \
  --dangerously-skip-permissions \
  --output-format stream-json \
  --max-turns 30 \
  > ~/.local/share/conductor/logs/fix-agent-<N>.jsonl 2>&1
```

The fix-agent works on the same branch, pushes new commits, and does not create a new PR.

**Scope restriction**: The fix-agent must respect the same module boundary as the original agent.
CI failures from lint or type errors are in scope. CI failures from infrastructure issues or
external service unavailability are not in scope (treat as transient; trigger a CI re-run instead).

### 6.4 CI Failure Classification

Before dispatching a fix-agent, the conductor should attempt to classify the CI failure:

| CI Log Pattern | Classification | Action |
|----------------|---------------|--------|
| Lint error (`ruff check`, `mypy`) | Code quality failure | Fix-agent: fix lint/type errors |
| Test failure (`pytest`, `make test`) | Logic or regression failure | Fix-agent: fix failing tests |
| Dependency installation failure | Environment failure | Trigger CI re-run; do not dispatch fix-agent |
| Timeout (CI job exceeded limit) | Infrastructure failure | Trigger CI re-run once |
| Authentication error in CI | CI configuration failure | Escalate to human |
| Network unreachable in CI | Infrastructure failure | Trigger CI re-run once |

### 6.5 Abandon Criteria

Abandon CI remediation and escalate to human review when:
- Fix-agent has been dispatched once and CI still fails
- CI has been re-triggered 3+ times and fails with infrastructure errors
- The fix-agent produces a result with `error_max_turns` or `error_during_execution`

The escalation action:
```bash
gh pr comment <N> \
  --body "Conductor was unable to automatically fix CI failures. Human review required. Failure log: $(gh pr checks <N> | grep failure)" \
  --repo <owner/repo>
```
Then remove `in-progress` label and leave the PR open.

---

## 7. Partial Completion Detection

### 7.1 Partial Completion Scenarios

Partial completion occurs when an agent completes some work but not the required final step (PR
creation). These scenarios require different reconciliation paths:

| State | Git commits pushed | PR created | Reconciliation |
|-------|-------------------|------------|----------------|
| S1: Agent failed before any work | No | No | Clean worktree; re-dispatch |
| S2: Code changes made, not committed | No (unstaged) | No | Discard worktree; re-dispatch from clean state |
| S3: Changes committed, not pushed | Yes (local) | No | Worktree is gone; no remote evidence; re-dispatch |
| S4: Branch pushed, no PR | Yes (remote) | No | Create PR for existing branch; do not re-dispatch |
| S5: PR created, CI running | Yes (remote) | Yes (open) | Monitor CI; do not re-dispatch |
| S6: PR merged, label not removed | Yes (merged) | Yes (merged) | Remove in-progress label; clean worktree |

### 7.2 Reconciliation Logic

For each claimed issue on startup, the conductor runs this check:

```python
async def reconcile_issue(issue_number: int, branch: str, repo: str) -> None:
    """Determine and execute the correct recovery action for a claimed issue."""

    pr = await gh_pr_for_branch(branch, repo)

    if pr and pr["state"] == "merged":
        # S6: Completed
        await gh_remove_label(issue_number, "in-progress", repo)
        git_worktree_remove_if_exists(branch)
        return

    if pr and pr["state"] == "open":
        # S5: Normal — monitor CI
        await enqueue_for_ci_monitoring(pr["number"])
        return

    branch_exists = await git_remote_branch_exists(branch, repo)

    if branch_exists:
        commits_ahead = await git_commits_ahead(branch, "mainline", repo)
        if commits_ahead > 0:
            # S4: Branch has work — create PR
            await gh_pr_create(
                branch=branch,
                title=f"Partial work for issue #{issue_number} (conductor recovery)",
                body=f"Closes #{issue_number}\n\n"
                     "NOTE: This PR was created by conductor's recovery handler. "
                     "The original agent failed before creating the PR.",
                repo=repo,
            )
        else:
            # Branch exists but is empty — re-dispatch
            await re_dispatch_agent(issue_number, branch)
    else:
        # S1/S2/S3: Nothing on remote — re-dispatch from scratch
        await re_dispatch_agent(issue_number, branch)
```

---

## 8. Follow-Up Research Recommendations

### 8.1 Empirical Verification of Stderr Message in `-p` Mode [BLOCKS_IMPL]

**Question**: Does `claude -p` emit the `"You've hit your limit · resets <timestamp>"` message to
stderr in headless mode, or is this message only shown in the interactive TUI?

**Why it blocks implementation**: The reset timestamp heuristic in doc #23 (Section 4) and the
rate limit classification in Section 5 of this document both depend on being able to parse this
message from stderr. If it does not appear in headless mode, conductor must fall back to
consecutive-count inference, which is less accurate and slower to classify weekly caps.

**Method**: Run `claude -p "..." 2>stderr.txt` from an account near its limit; inspect `stderr.txt`
for the reset message. Requires an account near the 5-hour or weekly limit to trigger.

**Suggested issue**: Create if not already covered — distinct from #23 (which analyzed the message
format) and not yet covered by #41 (empirical verification suite).

### 8.2 Empirical Verification of `result.subtype` for Rate Limit Events [BLOCKS_IMPL]

**Question**: Is the `result.subtype` value `"error_during_operation"` when a rate limit fires
mid-session? Or does a different subtype appear? The structure in Section 2.2 is synthesized from
documentation and community reports but has not been verified from a live rate-limit event in
stream-json output.

**Why it blocks implementation**: The error classification logic in Section 2.4 routes on
`subtype == "error_during_operation"` combined with rate-limit text patterns. If the subtype is
different (e.g., if the process exits before emitting a result event), the classifier will miss
rate limit events.

**Scope**: This is a narrow empirical measurement. Add to #41 (empirical verification suite) rather
than creating a standalone issue.

### 8.3 Headless Overage Consumption Without Prompt [V2_RESEARCH]

**Question**: In headless `-p` mode, when the 5-hour window is exhausted and overage is enabled on
the account, does `claude -p` automatically consume extra usage credits without prompting the user,
or does it always exit with a rate limit error?

**Why this matters**: If `claude -p` silently consumes overage, conductor could incur unbounded
charges without the operator's awareness. The governor's assumption (Section 5.4) is that a 429
always stops the agent — if overage is consumed silently, this assumption is wrong.

**Note**: This question is also flagged as #63 (`Research: Headless overage consumption in claude -p
when 5-hour window is exhausted`). Defer to that issue.

**Tag**: [V2_RESEARCH] — the interaction with overage is not on the critical path for M1 since most
users will have overage disabled or will use API keys.

### 8.4 HTTP 402 as Weekly Cap Discriminator [V2_RESEARCH]

**Question**: Does weekly cap exhaustion on Max plans consistently return HTTP 402 instead of 429?
(Single data point from #30484 in the openclaw project.)

**Why this matters**: If HTTP 402 is reliable, it is the simplest discriminator for weekly cap —
simpler than the timestamp heuristic or OAuth endpoint polling.

**Note**: This is covered by issue #64 (`Research: HTTP 402 vs 429 as discriminator for weekly cap
vs 5-hour window exhaustion`). Defer to that issue.

**Tag**: [V2_RESEARCH] — the current fallback strategy (timestamp heuristic + OAuth endpoint) is
sufficient for M1.

### 8.5 Fix-Agent Scope Enforcement [V2_RESEARCH]

**Question**: When a fix-agent is dispatched to address CI failures, what mechanism prevents it from
making changes outside the module boundary of the original issue? The current design relies on
prompt instruction, which is not enforced.

**Why this matters**: An unscoped fix-agent could accidentally modify files in other packages,
violating the module isolation invariant.

**Suggested approach**: Use `--allowedTools "Bash(ruff *),Bash(pytest *),Read,Edit"` with a
restrictive allowlist that limits file edits to the allowed scope via PreToolUse hooks.

**Tag**: [V2_RESEARCH] — module boundary enforcement is not yet the focus of M1.

### 8.6 SIGTERM Pattern P5 Root Cause and Conductor Mitigation [V2_RESEARCH]

**Question**: What causes the spontaneous SIGTERM (exit 143) at 3–10 minutes in Max subscription
headless sessions (#29642)? Is it an OAuth token refresh issue, an internal watchdog, or a
rate-limit manifestation? Can conductor prevent or work around it?

**Note**: This is covered by issue #32 (`Research: Root cause of spontaneous SIGTERM in headless
claude -p sessions (exit 143)`). Defer to that issue.

**Tag**: [V2_RESEARCH] — the M1 mitigation (single retry with backoff) is sufficient.

---

## 9. Sources

- [Run Claude Code programmatically — Claude Code Docs](https://code.claude.com/docs/en/headless) — `-p` mode usage, `--output-format stream-json`, session ID capture, stream-json event types
- [CLI reference — Claude Code Docs](https://code.claude.com/docs/en/cli-reference) — All CLI flags; `--max-turns`, `--max-budget-usd`, `--output-format`, exit semantics; `claude auth status` exits 0/1
- [Stream responses in real-time — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/streaming-output) — Agent SDK streaming; event types; `result` message schema
- [ClaudeCode.Types — ClaudeCode v0.21.0 (Elixir SDK)](https://hexdocs.pm/claude_code/ClaudeCode.Types.html) — `result_subtype` enum: `success`, `error_max_turns`, `error_max_budget_usd`, `error_during_execution`, `error_max_structured_output_retries`
- [ClaudeCodeSDK.Message — claude_code_sdk v0.2.2](https://hexdocs.pm/claude_code_sdk/ClaudeCodeSDK.Message.html) — Result subtype taxonomy confirmed
- [Headless Mode and CI/CD — Cheatsheet — SFEIR Institute](https://institute.sfeir.com/en/claude-code/claude-code-headless-mode-and-ci-cd/cheatsheet/) — Exit code conventions; `0 = success`, `1 = error`, `2 = auth error`; 3 most common CI errors
- [Headless Mode and CI/CD — FAQ — SFEIR Institute](https://institute.sfeir.com/en/claude-code/claude-code-headless-mode-and-ci-cd/faq/) — Common error conditions in CI; rate limit vs auth failure text patterns
- [Headless Mode and CI/CD — Common Mistakes — SFEIR Institute](https://institute.sfeir.com/en/claude-code/claude-code-headless-mode-and-ci-cd/errors/) — Authentication errors, exit code 1/2 patterns
- [GitHub Issue #8126: Missing final result event in stream-json sometimes](https://github.com/anthropics/claude-code/issues/8126) — Known bug: result event not emitted on success in some cases
- [GitHub Issue #25629: Claude Code CLI hangs after result event in stream-json mode](https://github.com/anthropics/claude-code/issues/25629) — P3 hang: post-result stream not closed; T3 watchdog tier justification
- [GitHub Issue #28482: Agent hangs indefinitely mid-task — no recovery path](https://github.com/anthropics/claude-code/issues/28482) — P4 hang: mid-task stall; watchdog rationale
- [GitHub Issue #29642: Headless sessions die with SIGTERM after 3–10 minutes](https://github.com/anthropics/claude-code/issues/29642) — P5 pattern: spontaneous SIGTERM at 3–10 min; Max subscription accounts
- [GitHub Issue #5666: Invalid API Key — Fix external API key when running headless mode with -p](https://github.com/anthropics/claude-code/issues/5666) — Documented authentication failure in headless mode; exit code behavior
- [GitHub Issue #1920: Missing Final Result Event in Streaming JSON Output](https://github.com/anthropics/claude-code/issues/1920) — Missing result event bug; SDK issue
- [GitHub Issue #14648 (opencode): Worktree bootstrap failures leak orphaned directories](https://github.com/anomalyco/opencode/issues/14648) — Worktree bootstrap failure pattern; partial creation leaves orphaned directories
- [docs/research/02-session-continuity.md (this repo)](./02-session-continuity.md) — Checkpoint stage taxonomy; partial state hazards; recovery protocol; `stop_hook_active` guard
- [docs/research/08-usage-scheduling.md (this repo)](./08-usage-scheduling.md) — Rate limit detection signals; HTTP 429 structure; exit code 1 for rate limits; drain-and-requeue backoff; `ErrorClass` integration point; governor design
- [docs/research/14-hang-detection.md (this repo)](./14-hang-detection.md) — Exit code taxonomy (P1–P6 hang patterns); exit codes 124, 130, 137, 143; three-tier watchdog; orphaned state cleanup; `sigterm_internal` synthetic subtype; cleanup procedure
- [docs/research/23-429-error-cap-distinction.md (this repo)](./23-429-error-cap-distinction.md) — Rate limit type classification; `representative-claim` header; OAuth endpoint schema; reset timestamp heuristic; `RateLimitClass` enum
- [Recommendations for handling transient faults — Microsoft Azure Well-Architected Framework](https://learn.microsoft.com/en-us/azure/well-architected/design-guides/handle-transient-faults) — Transient vs permanent failure classification framework; retry policy design
- [Retry logic in Workflows — Temporal.io](https://temporal.io/blog/failure-handling-in-practice) — Backoff + jitter caps; circuit breaker patterns; max retry count
- [Retry with backoff pattern — AWS Prescriptive Guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/retry-backoff.html) — Exponential backoff with jitter; full jitter recommendation
- [Exponential Backoff and Jitter — AWS Builders Library](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/) — Full jitter formula (canonical reference); thundering herd prevention
- [Queue-Based Exponential Backoff — DEV Community](https://dev.to/andreparis/queue-based-exponential-backoff-a-resilient-retry-pattern-for-distributed-systems-37f3) — Delete-Calculate-Requeue pattern; drain-and-requeue semantics
- [Git Worktrees: The Complete Guide for 2026 — DevToolbox](https://devtoolbox.dedyn.io/blog/git-worktrees-complete-guide) — `git worktree remove --force`; `git worktree prune` pattern; orphaned worktree recovery
- [Worktrees: Parallel Agent Isolation — Agent Factory](https://agentfactory.panaversity.org/docs/General-Agents-Foundations/general-agents/worktrees) — Worktree bootstrap failure patterns; cleanup procedures in CI
- [IssueOps: Automate CI/CD with GitHub Issues and Actions — GitHub Blog](https://github.blog/engineering/issueops-automate-ci-cd-and-more-with-github-issues-and-actions/) — Issue label lifecycle management; `in-progress` label pattern; automated requeue
- [Auto-rerun GitHub workflow on failure — GitHub Community](https://github.com/orgs/community/discussions/67654) — CI re-run trigger patterns; `gh run rerun` API
- [Tackling the "Partial Completion" Problem in LLM AI Agents — Medium](https://medium.com/@georgekar91/tackling-the-partial-completion-problem-in-llm-agents-9a7ec8949c84) — Partial completion taxonomy; detection patterns for LLM agents
- [Claude Code Troubleshooting — Claude Code Docs](https://code.claude.com/docs/en/troubleshooting) — Official troubleshooting guidance; authentication errors; common exit codes
