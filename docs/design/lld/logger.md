# LLD: Logger Module

**Module:** `logger`
**File:** `src/composer/logger.py`
**Issue:** #112
**Status:** Draft
**Date:** 2026-03-02

---

## 1. Module Overview

The `logger` module provides three independent JSONL write streams for conductor and its sub-agents: a permanent cost ledger, per-session execution logs, and a conductor orchestrator log. It exports a small write API and one dataclass. All consumers call these functions directly; nothing in this module reads or queries logs — that responsibility belongs to `cli.py` (`composer cost`).

**File path:** `src/composer/logger.py`

**Exports:**

| Symbol | Kind | Consumers |
|--------|------|-----------|
| `LogContext` | dataclass | `runner`, `cli` |
| `log_cost` | function | `runner` |
| `log_session_event` | function | `runner` |
| `log_conductor_event` | function | `runner`, `cli` |
| `read_cost_ledger` | function | `cli` (`composer cost`) |

---

## 2. Log Directory Layout

```
~/.composer/
  logs/
    cost.jsonl                        ← cost ledger (permanent, append-only)
    sessions/
      <session-id>.jsonl              ← per-session execution log (one per claude -p run)
    conductor/
      <run-id>.jsonl                  ← orchestrator decisions and stage transitions
```

### 2.1 Directory Creation

All directories are created on first write with `os.makedirs(path, exist_ok=True)`. No initialization step is required at startup. The caller must not assume directories already exist.

### 2.2 Configuration Override

The log root defaults to `~/.composer/logs`. The environment variable `CONDUCTOR_LOG_DIR` overrides the entire root path. All three subdirectory paths derive from the resolved root:

```
log_dir / "cost.jsonl"          → cost ledger
log_dir / "sessions"            → per-session log directory
log_dir / "conductor"           → conductor log directory
```

`logger.py` does not read environment variables or import `Config` directly. Callers pass a resolved `Path` object (`log_dir: Path`) into every write function. This keeps the module testable without environment setup.

### 2.3 File Naming

- **`cost.jsonl`**: single file, no rotation.
- **`sessions/<session-id>.jsonl`**: the `session_id` is the UUID captured from the `system/init` event of the `--output-format stream-json` stream. If the `system/init` event is not observed before the `result` event, fall back to `result.session_id`.
- **`conductor/<run-id>.jsonl`**: the `run_id` is the UUID stored in the conductor checkpoint at the start of the run. It is passed into `log_conductor_event` by the caller.

### 2.4 Log Rotation and Retention

| File | Rotation | Retention |
|------|----------|-----------|
| `cost.jsonl` | Never — append-only permanent ledger | Permanent |
| `sessions/<session-id>.jsonl` | One file per session; no rotation | 14 days (cleanup on startup) |
| `conductor/<run-id>.jsonl` | One file per run; no rotation | 30 days (cleanup on startup) |

Cleanup is the responsibility of the caller (runner startup), not this module.

---

## 3. Cost Ledger Schema

`cost.jsonl` is an append-only JSONL file. Each line is one complete JSON object representing a single `claude -p` invocation. Lines are never modified after writing.

### 3.1 Field Specification

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `timestamp` | string (ISO 8601 UTC) | `datetime.now(timezone.utc).isoformat()` | Time the entry was written (subprocess completion, not start) |
| `session_id` | string (UUID) | `result.session_id` or `system/init.session_id` | Correlates with per-session log file name |
| `run_id` | string (UUID) | `LogContext.run_id` | Conductor run that dispatched this agent |
| `repo` | string | `LogContext.repo` | `owner/repo` format |
| `stage` | string | `LogContext.stage` | One of: `research`, `implementation`, `probe`, `orchestrator` |
| `issue_number` | int or null | `LogContext.issue_number` | GitHub issue number; `null` for health probes and orchestrator sessions |
| `model` | string | `system/init.model` | Claude model ID, e.g. `claude-sonnet-4-6` |
| `input_tokens` | int | `result.usage.input_tokens` | Regular input tokens billed at full rate |
| `output_tokens` | int | `result.usage.output_tokens` | Output tokens generated |
| `cache_read_input_tokens` | int | `result.usage.cache_read_input_tokens` | Tokens read from prompt cache (10% of input price) |
| `cache_creation_input_tokens` | int | `result.usage.cache_creation_input_tokens` | Tokens written to prompt cache (125% of input price) |
| `num_turns` | int | `result.num_turns` | Number of assistant turns in the session |
| `duration_ms` | int | `result.duration_ms` | Total wall-clock duration including tool execution |
| `is_error` | bool | `result.is_error` | `true` if the session terminated with an error |
| `error_subtype` | string or null | `result.subtype` when `is_error` is `true`, else `null` | One of: `error_max_turns`, `error_max_budget_usd`, `error_during_execution` |
| `total_cost_usd` | float or null | `result.total_cost_usd` (API key) or computed (subscription) | `null` if computation was not possible |
| `auth_mode` | string | Presence of `ANTHROPIC_API_KEY` in environment | `api_key` or `subscription` |
| `web_search_requests` | int | `result.usage.server_tool_use.web_search_requests` | Defaults to `0` if field absent |

### 3.2 Full Example Entry

Successful session (API key auth):

```json
{"timestamp": "2026-03-02T14:35:22.418Z", "session_id": "550e8400-e29b-41d4-a716-446655440000", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "repo": "myorg/myrepo", "stage": "implementation", "issue_number": 7, "model": "claude-sonnet-4-6", "input_tokens": 142500, "output_tokens": 18200, "cache_read_input_tokens": 118000, "cache_creation_input_tokens": 4800, "num_turns": 28, "duration_ms": 187432, "is_error": false, "error_subtype": null, "total_cost_usd": 0.8341, "auth_mode": "api_key", "web_search_requests": 0}
```

Error session (subscription auth, estimated cost):

```json
{"timestamp": "2026-03-02T16:12:05.003Z", "session_id": "661f9511-f30c-52e5-b827-557766551111", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "repo": "myorg/myrepo", "stage": "implementation", "issue_number": 16, "model": "claude-sonnet-4-6", "input_tokens": 89300, "output_tokens": 12100, "cache_read_input_tokens": 72000, "cache_creation_input_tokens": 2400, "num_turns": 12, "duration_ms": 93210, "is_error": true, "error_subtype": "error_max_turns", "total_cost_usd": 0.4312, "auth_mode": "subscription", "web_search_requests": 0}
```

Probe session (no issue number):

```json
{"timestamp": "2026-03-02T17:00:00.000Z", "session_id": "772a0622-041d-63f6-c938-668877662222", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "repo": "myorg/myrepo", "stage": "probe", "issue_number": null, "model": "claude-sonnet-4-6", "input_tokens": 1200, "output_tokens": 480, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "num_turns": 2, "duration_ms": 4820, "is_error": false, "error_subtype": null, "total_cost_usd": 0.0112, "auth_mode": "api_key", "web_search_requests": 0}
```

### 3.3 Append-Only Invariant

Writers must never open `cost.jsonl` with mode `"w"` (truncate). The only permitted open mode is `"a"` (append). The file lock (see Section 6.3) ensures atomic line append even when multiple agents write concurrently.

### 3.4 Subscription Mode: Cost Estimation

When `auth_mode` is `subscription`, `result.total_cost_usd` is `null` or `0`. In this case `total_cost_usd` must be computed from token counts using the following formula (prices as of March 2026 for `claude-sonnet-4-6`):

```python
SONNET_PRICES = {
    "input_per_mtok": 3.00,
    "output_per_mtok": 15.00,
    "cache_write_per_mtok": 3.75,   # 125% of input
    "cache_read_per_mtok": 0.30,    # 10% of input
}
OPUS_MULTIPLIER = 5.0  # claude-opus-4-6 is ~5x Sonnet 4.6


def _estimate_cost_usd(usage: dict, model: str) -> float:
    p = SONNET_PRICES
    mult = OPUS_MULTIPLIER if "opus" in model.lower() else 1.0
    return round((
        usage.get("input_tokens", 0) / 1_000_000 * p["input_per_mtok"]
        + usage.get("output_tokens", 0) / 1_000_000 * p["output_per_mtok"]
        + usage.get("cache_creation_input_tokens", 0) / 1_000_000 * p["cache_write_per_mtok"]
        + usage.get("cache_read_input_tokens", 0) / 1_000_000 * p["cache_read_per_mtok"]
    ) * mult, 6)
```

If the model string is unrecognized and a multiplier cannot be determined, write `total_cost_usd: null` and log a warning to the conductor log.

### 3.5 `composer cost` Aggregation

`composer cost` calls `read_cost_ledger(log_dir)`, then groups entries by `repo`, then by `stage`, summing `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, and `total_cost_usd`. The full aggregation logic is specified in Section 7.

---

## 4. Per-Session Log Schema

Each `claude -p` subprocess has a dedicated JSONL file at `sessions/<session-id>.jsonl`. The file contains conductor phase annotations and a subset of parsed stream-json events. Raw stream-json events are **not** written verbatim — only selected, summarized events are written. This keeps files small and avoids storing tool output content (which may be large and sensitive).

### 4.1 Common Fields

Every line in a session log shares these fields:

| Field | Type | Notes |
|-------|------|-------|
| `timestamp` | string (ISO 8601 UTC) | Time the event was processed by conductor |
| `session_id` | string (UUID) | Identifies the session; matches the filename |
| `run_id` | string (UUID) | Conductor run that owns this session |
| `event_type` | string | Discriminator; see Section 4.2 |
| `phase` | string | Conductor phase at time of event; e.g. `dispatch`, `ci_check`, `merge` |
| `payload` | object | Event-type-specific fields |

### 4.2 Event Types and Payloads

#### `dispatch_start`

Written by conductor immediately before spawning the subprocess.

```
payload: {
  issue_number: int | null,
  branch: string | null,
  prompt_length_chars: int        ← character count of the prompt passed to claude -p
}
```

#### `stream_event`

Written for each stream-json event received from the subprocess. The full event body is **not** stored — only the type and a brief summary.

```
payload: {
  source_type: string,            ← value of event["type"] from stream-json
  source_subtype: string | null,  ← value of event.get("subtype") if present
  summary: string                 ← human-readable one-liner; see summary rules below
}
```

**Summary rules by source event type:**

| `source_type` | `summary` content |
|---------------|-------------------|
| `system` (init) | `"init model=<model> tools=<tool_count>"` |
| `assistant` | `"turn text"` if text-only; `"turn tool=<ToolName> input_preview=<first_60_chars>"` if tool call |
| `user` | `"tool_result tool_use_id=<id> duration_ms=<N>"` |
| `result` | `"subtype=<subtype> is_error=<bool> turns=<N>"` |
| `rate_limit_event` | `"status=<status>"` |
| `compact_boundary` | `"compacted"` |
| other | `"unknown type=<source_type>"` |

#### `result`

Written when the subprocess emits its `result` stream-json event (always the last event). This is the authoritative completion record for the session log.

```
payload: {
  subtype: string,                ← "success" | "error_max_turns" | "error_max_budget_usd" | "error_during_execution"
  is_error: bool,
  total_cost_usd: float | null,
  input_tokens: int,
  output_tokens: int,
  cache_read_input_tokens: int,
  cache_creation_input_tokens: int,
  num_turns: int,
  duration_ms: int
}
```

#### `error`

Written when the subprocess exits with a non-zero exit code, or when parsing the stream fails unrecoverably.

```
payload: {
  subtype: string,                ← "process_exit" | "parse_failure" | "timeout"
  exit_code: int | null,          ← OS exit code; null for timeout/parse_failure
  message: string                 ← human-readable error description
}
```

#### `retry`

Written before each retry attempt (conductor backs off and re-spawns).

```
payload: {
  attempt: int,                   ← 1-indexed; attempt=1 means the first retry (second total try)
  reason: string,                 ← "rate_limit" | "process_error" | "parse_failure"
  backoff_seconds: int
}
```

### 4.3 Full Example Session Log

A complete per-session log for a successful single-issue dispatch (one JSON object per line):

```json
{"timestamp": "2026-03-02T14:32:00.000Z", "session_id": "550e8400-e29b-41d4-a716-446655440000", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "event_type": "dispatch_start", "phase": "dispatch", "payload": {"issue_number": 7, "branch": "7-inbound-outbound-messages", "prompt_length_chars": 4820}}
{"timestamp": "2026-03-02T14:32:01.112Z", "session_id": "550e8400-e29b-41d4-a716-446655440000", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "event_type": "stream_event", "phase": "dispatch", "payload": {"source_type": "system", "source_subtype": "init", "summary": "init model=claude-sonnet-4-6 tools=6"}}
{"timestamp": "2026-03-02T14:32:02.340Z", "session_id": "550e8400-e29b-41d4-a716-446655440000", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "event_type": "stream_event", "phase": "dispatch", "payload": {"source_type": "assistant", "source_subtype": null, "summary": "turn tool=Bash input_preview=\"git checkout 7-inbound-outbound-messages\""}}
{"timestamp": "2026-03-02T14:32:03.001Z", "session_id": "550e8400-e29b-41d4-a716-446655440000", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "event_type": "stream_event", "phase": "dispatch", "payload": {"source_type": "user", "source_subtype": null, "summary": "tool_result tool_use_id=toolu_01A09q90 duration_ms=234"}}
{"timestamp": "2026-03-02T14:35:22.000Z", "session_id": "550e8400-e29b-41d4-a716-446655440000", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "event_type": "stream_event", "phase": "dispatch", "payload": {"source_type": "result", "source_subtype": "success", "summary": "subtype=success is_error=false turns=28"}}
{"timestamp": "2026-03-02T14:35:22.418Z", "session_id": "550e8400-e29b-41d4-a716-446655440000", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "event_type": "result", "phase": "dispatch", "payload": {"subtype": "success", "is_error": false, "total_cost_usd": 0.8341, "input_tokens": 142500, "output_tokens": 18200, "cache_read_input_tokens": 118000, "cache_creation_input_tokens": 4800, "num_turns": 28, "duration_ms": 187432}}
```

Session with a retry before success:

```json
{"timestamp": "2026-03-02T15:00:00.000Z", "session_id": "883b1733-152e-74g7-d049-779988773333", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "event_type": "dispatch_start", "phase": "dispatch", "payload": {"issue_number": 9, "branch": "9-auth-middleware", "prompt_length_chars": 3200}}
{"timestamp": "2026-03-02T15:00:01.000Z", "session_id": "883b1733-152e-74g7-d049-779988773333", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "event_type": "stream_event", "phase": "dispatch", "payload": {"source_type": "result", "source_subtype": "error_during_execution", "summary": "subtype=error_during_execution is_error=true turns=1"}}
{"timestamp": "2026-03-02T15:00:01.500Z", "session_id": "883b1733-152e-74g7-d049-779988773333", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "event_type": "error", "phase": "dispatch", "payload": {"subtype": "process_exit", "exit_code": 1, "message": "Subprocess exited with code 1 after error_during_execution result"}}
{"timestamp": "2026-03-02T15:02:01.500Z", "session_id": "883b1733-152e-74g7-d049-779988773333", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "event_type": "retry", "phase": "dispatch", "payload": {"attempt": 1, "reason": "rate_limit", "backoff_seconds": 120}}
```

### 4.4 File Lifecycle

1. File is created (opened with `"a"`) on the first `log_session_event` call for a given `session_id`.
2. The directory `sessions/` is created with `os.makedirs(exist_ok=True)` if it does not exist.
3. The file is not explicitly closed between writes — each call opens, appends, and closes via a context manager.
4. Per-session logs are single-writer (only one conductor coroutine drives each subprocess), so no file locking is needed.

---

## 5. Conductor Log Schema

Conductor's own operational decisions are written to `conductor/<run-id>.jsonl`. This stream covers the orchestrator lifecycle: stage start/stop, issue claim/dispatch, PR management, rate-limit backoff, and checkpoint writes.

### 5.1 Common Fields

| Field | Type | Notes |
|-------|------|-------|
| `timestamp` | string (ISO 8601 UTC) | Time the event occurred in conductor |
| `run_id` | string (UUID) | Identifies the conductor run; matches the filename |
| `phase` | string | Current pipeline phase; e.g. `init`, `claim`, `dispatch`, `ci_check`, `merge`, `backoff` |
| `event_type` | string | Discriminator; see Section 5.2 |
| `payload` | object | Event-type-specific fields |

### 5.2 Event Types and Payloads

#### `stage_start`

Written once when a worker begins processing a milestone.

```
payload: {
  worker_type: string,            ← "research" | "implementation" | "design"
  milestone: string,              ← milestone name, e.g. "MVP Research"
  issue_count: int                ← number of open issues in the milestone at start
}
```

#### `issue_claimed`

Written after the GitHub issue is assigned and labelled `in-progress`.

```
payload: {
  issue_number: int,
  branch: string                  ← branch name created for this issue
}
```

#### `agent_dispatched`

Written after the subprocess is launched and the `system/init` event is received (so `session_id` is known).

```
payload: {
  issue_number: int,
  session_id: string              ← UUID from stream-json system/init event
}
```

#### `agent_completed`

Written after the subprocess exits and the cost entry is written.

```
payload: {
  issue_number: int,
  subtype: string,                ← result.subtype from stream-json
  is_error: bool,
  cost_usd: float | null          ← total_cost_usd from cost ledger entry
}
```

#### `pr_created`

Written after `gh pr create` succeeds.

```
payload: {
  issue_number: int,
  pr_number: int
}
```

#### `ci_checked`

Written after each `gh pr checks` poll.

```
payload: {
  pr_number: int,
  status: string                  ← "pass" | "fail" | "pending"
}
```

#### `pr_merged`

Written after `gh pr merge --squash` succeeds.

```
payload: {
  pr_number: int,
  issue_number: int
}
```

#### `backoff_enter`

Written when conductor enters a rate-limit or budget backoff wait.

```
payload: {
  until: string,                  ← ISO 8601 UTC timestamp when backoff ends
  trigger_subtype: string         ← result.subtype that triggered backoff, or "budget_low"
}
```

#### `backoff_exit`

Written when conductor resumes after a backoff wait.

```
payload: {
  resumed_at: string              ← ISO 8601 UTC timestamp
}
```

#### `human_escalate`

Written when conductor cannot proceed without human intervention (e.g. persistent CI failure, merge conflict outside agent scope).

```
payload: {
  issue_number: int | null,
  reason: string,                 ← human-readable description of what blocked progress
  action_required: string         ← specific action the human must take
}
```

#### `checkpoint_write`

Written each time the conductor checkpoint file is persisted.

```
payload: {
  path: string,                   ← absolute path to the checkpoint file
  claimed_count: int,             ← number of issues claimed so far in this run
  completed_count: int            ← number of issues fully merged so far in this run
}
```

#### `stage_complete`

Written once when all issues in the milestone are resolved and the pipeline stage exits.

```
payload: {
  issues_completed: int,
  total_cost_usd: float | null    ← sum of cost_usd across all completed agents in the run
}
```

### 5.3 Full Example Conductor Log

A single-issue run (one JSON object per line):

```json
{"timestamp": "2026-03-02T14:30:00.000Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "init", "event_type": "stage_start", "payload": {"worker_type": "implementation", "milestone": "MVP Implementation", "issue_count": 3}}
{"timestamp": "2026-03-02T14:30:01.500Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "claim", "event_type": "issue_claimed", "payload": {"issue_number": 7, "branch": "7-inbound-outbound-messages"}}
{"timestamp": "2026-03-02T14:32:01.200Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "dispatch", "event_type": "agent_dispatched", "payload": {"issue_number": 7, "session_id": "550e8400-e29b-41d4-a716-446655440000"}}
{"timestamp": "2026-03-02T14:35:22.500Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "dispatch", "event_type": "agent_completed", "payload": {"issue_number": 7, "subtype": "success", "is_error": false, "cost_usd": 0.8341}}
{"timestamp": "2026-03-02T14:35:35.100Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "dispatch", "event_type": "pr_created", "payload": {"issue_number": 7, "pr_number": 42}}
{"timestamp": "2026-03-02T14:35:36.000Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "dispatch", "event_type": "checkpoint_write", "payload": {"path": "/home/user/.composer/logs/conductor/a1b2c3d4-e5f6-7890-abcd-ef1234567890.jsonl", "claimed_count": 1, "completed_count": 0}}
{"timestamp": "2026-03-02T14:40:12.000Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "ci_check", "event_type": "ci_checked", "payload": {"pr_number": 42, "status": "pending"}}
{"timestamp": "2026-03-02T14:47:30.000Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "ci_check", "event_type": "ci_checked", "payload": {"pr_number": 42, "status": "pass"}}
{"timestamp": "2026-03-02T14:47:45.000Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "merge", "event_type": "pr_merged", "payload": {"pr_number": 42, "issue_number": 7}}
{"timestamp": "2026-03-02T14:47:46.000Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "merge", "event_type": "checkpoint_write", "payload": {"path": "/home/user/.composer/logs/conductor/a1b2c3d4-e5f6-7890-abcd-ef1234567890.jsonl", "claimed_count": 1, "completed_count": 1}}
{"timestamp": "2026-03-02T14:47:47.000Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "complete", "event_type": "stage_complete", "payload": {"issues_completed": 1, "total_cost_usd": 0.8341}}
```

Backoff example (rate limit):

```json
{"timestamp": "2026-03-02T15:10:00.000Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "backoff", "event_type": "backoff_enter", "payload": {"until": "2026-03-02T15:42:00.000Z", "trigger_subtype": "error_during_execution"}}
{"timestamp": "2026-03-02T15:42:00.500Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "backoff", "event_type": "backoff_exit", "payload": {"resumed_at": "2026-03-02T15:42:00.500Z"}}
```

Human escalation example:

```json
{"timestamp": "2026-03-02T16:00:00.000Z", "run_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "phase": "ci_check", "event_type": "human_escalate", "payload": {"issue_number": 9, "reason": "CI failed 3 consecutive times on tests/integration/test_auth.py", "action_required": "Inspect test_auth.py failures and resolve or mark flaky before re-queuing"}}
```

### 5.4 File Lifecycle

1. File is created on the first `log_conductor_event` call for a given `run_id`.
2. The directory `conductor/` is created with `os.makedirs(exist_ok=True)` if it does not exist.
3. The conductor log is single-writer (only the orchestrator process writes to its own run file), so no file locking is needed.

---

## 6. Write API

### 6.1 `LogContext` Dataclass

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class LogContext:
    session_id: str        # UUID from stream-json system/init event
    run_id: str            # UUID from conductor checkpoint
    repo: str              # "owner/repo"
    stage: str             # "research" | "implementation" | "probe" | "orchestrator"
    issue_number: int | None = None  # None for probes and orchestrator sessions
```

`LogContext` is constructed by `runner.py` after the `system/init` event is received and passed into `log_cost` and `log_session_event`. `log_conductor_event` does not use `LogContext` — it takes `run_id`, `phase`, and `event_type` directly.

### 6.2 `log_cost(result_event, context, *, log_dir, model, auth_mode)`

Appends one entry to `cost.jsonl`.

```python
def log_cost(
    result_event: dict,
    context: LogContext,
    *,
    log_dir: Path,
    model: str,
    auth_mode: str,         # "api_key" | "subscription"
) -> None:
```

**Behaviour:**

1. Extracts `usage`, `total_cost_usd`, `num_turns`, `duration_ms`, `is_error`, `subtype` from `result_event`.
2. If `auth_mode == "subscription"` and `total_cost_usd` is `None` or `0`, computes estimated cost via `_estimate_cost_usd(usage, model)`.
3. Constructs the cost ledger entry dict (all fields from Section 3.1).
4. Acquires an exclusive `fcntl.flock` on `cost.jsonl` (creating it if absent).
5. Appends `json.dumps(entry) + "\n"` to the file.
6. Releases the lock.

**Raises:** `OSError` on disk errors (caller must catch and log to conductor log). Never raises on missing or zero `total_cost_usd` — uses the estimation fallback instead.

### 6.3 Thread Safety for `cost.jsonl`

Multiple sub-agents may complete concurrently and call `log_cost` simultaneously from different asyncio tasks (which may run in different threads if `asyncio.to_thread` is used). The cost ledger uses `fcntl.flock` (POSIX exclusive advisory lock) to serialize writes:

```python
import fcntl
import json
import os
from pathlib import Path


def _append_locked(path: Path, line: str) -> None:
    """Append a newline-terminated JSON line to path under an exclusive flock."""
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(line + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
```

`fcntl.flock` is not available on Windows. This is acceptable: the module targets POSIX (macOS, Linux) only. If Windows support is needed in the future, replace `fcntl.flock` with `msvcrt.locking` inside a platform guard.

**Per-session and conductor logs are single-writer** — only one coroutine writes to a given session file, and only the orchestrator process writes to its own conductor file. No locking is needed for these.

### 6.4 `log_session_event(session_id, event_type, phase, payload, *, log_dir, run_id)`

Appends one structured event to `sessions/<session-id>.jsonl`.

```python
def log_session_event(
    session_id: str,
    event_type: str,
    phase: str,
    payload: dict,
    *,
    log_dir: Path,
    run_id: str,
) -> None:
```

**Behaviour:**

1. Constructs the line dict: `{timestamp, session_id, run_id, event_type, phase, payload}`.
2. Resolves path: `log_dir / "sessions" / f"{session_id}.jsonl"`.
3. Creates parent directory if absent.
4. Opens file in `"a"` mode, writes `json.dumps(line) + "\n"`, closes.

No locking. Caller must ensure single-writer invariant (one asyncio task per session).

### 6.5 `log_conductor_event(run_id, phase, event_type, payload, *, log_dir)`

Appends one structured event to `conductor/<run-id>.jsonl`.

```python
def log_conductor_event(
    run_id: str,
    phase: str,
    event_type: str,
    payload: dict,
    *,
    log_dir: Path,
) -> None:
```

**Behaviour:**

1. Constructs the line dict: `{timestamp, run_id, phase, event_type, payload}`.
2. Resolves path: `log_dir / "conductor" / f"{run_id}.jsonl"`.
3. Creates parent directory if absent.
4. Opens file in `"a"` mode, writes `json.dumps(line) + "\n"`, closes.

No locking. The orchestrator is single-writer for its own run file.

### 6.6 `read_cost_ledger(log_dir)`

Reads all entries from `cost.jsonl` and returns them as a list of dicts. Used by `composer cost`.

```python
def read_cost_ledger(log_dir: Path) -> list[dict]:
```

**Behaviour:**

1. Opens `log_dir / "cost.jsonl"` in read mode.
2. Iterates lines; skips blank lines and lines that fail `json.loads`.
3. Returns list of parsed dicts in file order (oldest-first).
4. Returns `[]` if the file does not exist.

**Raises:** `OSError` only on permission errors (file-not-found is swallowed and returns `[]`).

---

## 7. `composer cost` Aggregation

`composer cost` calls `read_cost_ledger(log_dir)`, then aggregates and prints a summary table.

### 7.1 Aggregation Logic

```
Group entries by repo.
  For each repo, group by stage.
    Sum: input_tokens, output_tokens, cache_read_input_tokens,
         cache_creation_input_tokens, total_cost_usd.
    Count: sessions (entries), errors (is_error == true).
  Compute repo total: sum of stage totals.
Compute grand total across all repos.
```

Entries with `total_cost_usd: null` contribute `0` to the cost sum. The table notes when estimates are unavailable.

`total_cost_usd` for subscription sessions is the computed estimate from `_estimate_cost_usd` (see Section 3.4). The column header reflects this: `Est. Cost (USD)`.

### 7.2 Output Format

```
composer cost

Repo: myorg/myrepo
+------------------+----------+-----------------+---------------+-----------------+
| Stage            | Sessions |   Input Tokens  | Output Tokens | Est. Cost (USD) |
+------------------+----------+-----------------+---------------+-----------------+
| research         |        4 |         512,000 |        68,400 |           $2.41 |
| implementation   |        8 |       1,138,000 |       145,600 |           $6.67 |
| probe            |        2 |          24,000 |         3,200 |           $0.12 |
+------------------+----------+-----------------+---------------+-----------------+
| TOTAL            |       14 |       1,674,000 |       217,200 |           $9.20 |
+------------------+----------+-----------------+---------------+-----------------+

Grand total across 1 repo: $9.20
```

Multiple repos are printed as separate tables, each with its own repo header. A single grand total line follows all repo tables.

### 7.3 CLI Command Signature

The CLI command in `cli.py`:

```python
@composer.command("cost")
@click.option("--repo", default=None, help="Filter to a specific repo (owner/repo)")
@click.option("--stage", default=None, help="Filter to a specific stage")
def cost(repo: str | None, stage: str | None) -> None:
    """Show cost ledger summary."""
```

Filters are applied before aggregation if provided.

---

## 8. Interface Summary

### 8.1 All Public Symbols

| Symbol | Kind | Signature |
|--------|------|-----------|
| `LogContext` | dataclass | `(session_id, run_id, repo, stage, issue_number=None)` |
| `log_cost` | function | `(result_event, context, *, log_dir, model, auth_mode) -> None` |
| `log_session_event` | function | `(session_id, event_type, phase, payload, *, log_dir, run_id) -> None` |
| `log_conductor_event` | function | `(run_id, phase, event_type, payload, *, log_dir) -> None` |
| `read_cost_ledger` | function | `(log_dir) -> list[dict]` |

### 8.2 Consumer Call Map

| Consumer | Function called | When |
|----------|-----------------|------|
| `runner.py` | `log_cost` | After subprocess exits and `result` event is parsed |
| `runner.py` | `log_session_event` | For each parsed stream-json event and on `dispatch_start` |
| `runner.py` | `log_conductor_event` | On `agent_dispatched`, `agent_completed`, `pr_created`, `ci_checked`, `pr_merged`, `backoff_enter`, `backoff_exit`, `checkpoint_write`, `human_escalate` |
| `cli.py` (`impl_worker`, `research_worker`) | `log_conductor_event` | On `stage_start`, `issue_claimed`, `stage_complete` |
| `cli.py` (`composer cost`) | `read_cost_ledger` | On `composer cost` invocation |

### 8.3 What This Module Does NOT Do

- Does not configure `structlog` or stdlib `logging` — those are caller responsibilities.
- Does not read session logs or conductor logs — read paths are not implemented here.
- Does not compress or rotate logs — cleanup is a runner startup responsibility.
- Does not emit to stdout or stderr — all output is written to files.
- Does not validate schemas on read — `read_cost_ledger` returns raw parsed dicts.
- Does not resolve `CONDUCTOR_LOG_DIR` — callers resolve the env var and pass a `Path`.

---

## 9. Cross-References

- **`docs/research/05-logging-observability.md`**: Authoritative source for stream-json event schemas, cost field extraction, structlog configuration, and the `fcntl.flock` write pattern.
- **`src/composer/config.py`**: Resolves `CONDUCTOR_LOG_DIR` env var and constructs `Config.log_dir`; callers pass `Config.log_dir` into this module's functions.
- **`src/composer/runner.py`**: Primary caller of `log_cost` and `log_session_event`; owns the subprocess lifecycle and stream parsing.
- **`src/composer/cli.py`**: Calls `log_conductor_event` for stage-level events; calls `read_cost_ledger` for `composer cost`.
- **`docs/research/08-usage-scheduling.md` §5.2**: `total_cost_usd` in cost ledger entries feeds the usage governor's `can_dispatch()` budget check.
- **`docs/research/02-session-continuity.md` §8**: The `session_id` in per-session log filenames is the same ID used for `--resume` forensics.
