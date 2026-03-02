# Research: Logging and Observability for Headless Sessions

**Issue:** #5
**Milestone:** M1: Foundation
**Status:** Complete
**Date:** 2026-03-02

---

## Executive Summary

Conductor needs three distinct logging concerns that each solve a different problem:

1. **Cost ledger** (`cost.jsonl`) — a per-invocation JSONL file capturing token counts and
   estimated USD cost, extracted from the `result` event in `--output-format stream-json`
   output. This enables conservative budget accounting for Pro/Max subscription sessions
   (see `08-usage-scheduling.md`) and feeds the usage governor's pre-dispatch check.

2. **Per-session logs** (`sessions/<session-id>.jsonl`) — a structured JSONL log of each
   `claude -p` subprocess, including phase markers, tool call traces, and error events.
   Source material is the raw `--output-format stream-json` event stream piped to disk, then
   selectively parsed for phase annotations added by conductor.

3. **Conductor orchestrator log** — structured operational logs from conductor's own Python
   process (issue claim, dispatch, PR creation, merge decisions), emitted via
   **structlog** to a rotating daily JSONL file.

The `result` event in `stream-json` format is the single authoritative source for token
counts and cost. It carries `total_cost_usd`, `usage.input_tokens`, `usage.output_tokens`,
`usage.cache_read_input_tokens`, and `usage.cache_creation_input_tokens`. These fields are
reliable even when the subprocess exits with `is_error: true`.

For terminal progress, the recommended approach is **Rich** (`rich.progress.Progress` with
`SpinnerColumn` + `TimeElapsedColumn`) running in a background thread with asyncio
`create_subprocess_exec` driving a line-by-line reader of the subprocess stdout.

---

## 1. Complete `stream-json` Event Schema

`claude -p --output-format stream-json` emits newline-delimited JSON (NDJSON) — one JSON
object per line. The stream begins with a `system/init` event, proceeds through
`assistant` and `user` message events, and ends with a `result` event. When
`--include-partial-messages` is also passed, `stream_event` messages are interleaved.

### 1.1 SystemInitMessage

Emitted once at session start. Contains environment and capability metadata.

```json
{
  "type": "system",
  "subtype": "init",
  "uuid": "msg-01JNK5X4VQFVNG9GS5K9XH2J5A",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "cwd": "/path/to/worktree/7-my-feature",
  "model": "claude-sonnet-4-6",
  "tools": ["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
  "mcp_servers": [],
  "permissionMode": "bypassPermissions",
  "apiKeySource": "ANTHROPIC_API_KEY",
  "slash_commands": [],
  "agents": [],
  "claude_code_version": "2.1.50"
}
```

**Key fields for conductor:**
- `session_id` — correlates all subsequent events; capture this for the cost ledger
- `model` — required for cost ledger (Opus vs. Sonnet price differs 5×)
- `tools` — audit surface; verify expected tools are available
- `mcp_servers` — verify no unexpected servers loaded (defense-in-depth)

### 1.2 AssistantMessage

Emitted each time Claude produces a response (including partial responses when
`--include-partial-messages` is active). Contains content blocks and per-turn token usage.

```json
{
  "type": "assistant",
  "uuid": "msg-01JNK5X4VQFVNG9GS5K9XH2J5A",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "parent_tool_use_id": null,
  "message": {
    "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
    "type": "message",
    "role": "assistant",
    "model": "claude-sonnet-4-6",
    "content": [
      {
        "type": "text",
        "text": "I'll implement this feature now."
      },
      {
        "type": "tool_use",
        "id": "toolu_01A09q90qw90lq917835lq9",
        "name": "Bash",
        "input": { "command": "git status" }
      }
    ],
    "stop_reason": "tool_use",
    "stop_sequence": null,
    "usage": {
      "input_tokens": 12783,
      "output_tokens": 47,
      "cache_creation_input_tokens": 367,
      "cache_read_input_tokens": 12416
    }
  }
}
```

**Content block types:**
- `{"type": "text", "text": "..."}` — text response
- `{"type": "tool_use", "id": "...", "name": "...", "input": {...}}` — tool invocation

**`parent_tool_use_id`**: non-null when this assistant message is generated inside a
sub-agent launched by the `Agent`/`Task` tool. For conductor's subprocess-based model,
this will always be `null`.

**Usage note**: Per-turn `usage` in `AssistantMessage` may be duplicated across multiple
events sharing the same `message.id` when Claude uses parallel tool calls. The
`ResultMessage.usage` is the authoritative aggregate — deduplicate per-turn counts by
`message.id` when building per-step breakdowns.

### 1.3 UserMessage

Emitted each time a tool result is returned to Claude. Carries the tool execution output.

```json
{
  "type": "user",
  "uuid": "msg-01JNK5X4VQFVNG9GS5K9XHZZZ",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "parent_tool_use_id": "toolu_01A09q90qw90lq917835lq9",
  "message": {
    "role": "user",
    "content": [
      {
        "type": "tool_result",
        "tool_use_id": "toolu_01A09q90qw90lq917835lq9",
        "content": "On branch 7-my-feature\nnothing to commit\n"
      }
    ]
  },
  "tool_use_result": {
    "filenames": [],
    "durationMs": 234,
    "numFiles": 0,
    "truncated": false
  }
}
```

**`parent_tool_use_id`**: non-null here when the tool result is from a tool invoked inside
an inner sub-agent. Used to reconstruct tool call trees in per-session logs.

**`tool_use_result`**: metadata about the execution — duration, whether output was
truncated, and file paths returned (useful for `Read`/`Edit` calls).

### 1.4 ResultMessage (Success)

The final event in every `--output-format stream-json` session. This is the primary source
for cost and usage data. **Always present even when `is_error: true`.**

```json
{
  "type": "result",
  "subtype": "success",
  "uuid": "msg-01JNK5X4VQFVNG9GS5K9XHEND",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "is_error": false,
  "duration_ms": 187432,
  "duration_api_ms": 164821,
  "num_turns": 28,
  "result": "PR created: https://github.com/myorg/myrepo/pull/42",
  "total_cost_usd": 0.8341,
  "usage": {
    "input_tokens": 142500,
    "output_tokens": 18200,
    "cache_creation_input_tokens": 4800,
    "cache_read_input_tokens": 118000,
    "server_tool_use": {
      "web_search_requests": 0,
      "web_fetch_requests": 0
    }
  },
  "modelUsage": {
    "claude-sonnet-4-6": {
      "inputTokens": 142500,
      "outputTokens": 18200,
      "cacheReadInputTokens": 118000,
      "cacheCreationInputTokens": 4800,
      "webSearchRequests": 0,
      "costUSD": 0.8341,
      "contextWindow": 200000,
      "maxOutputTokens": 64000
    }
  },
  "permission_denials": [],
  "structured_output": null
}
```

**Key cost/usage fields:**
- `total_cost_usd` — aggregate USD cost for the entire session (API key mode only; `null`
  or `0` for Pro/Max subscription sessions)
- `usage.input_tokens` — regular input tokens billed at full rate
- `usage.output_tokens` — output tokens generated
- `usage.cache_creation_input_tokens` — tokens written to prompt cache (125% price on
  first write)
- `usage.cache_read_input_tokens` — tokens read from cache (10% of input price)
- `usage.server_tool_use.web_search_requests` — web search calls (if any)
- `modelUsage` — per-model breakdown; in multi-model sessions (e.g., sub-agents using
  different models), contains multiple keys
- `num_turns` — number of assistant turns; correlates with session complexity
- `duration_ms` — total wall-clock time including tool execution
- `duration_api_ms` — time spent waiting for API responses (excludes tool run time)

### 1.5 ResultMessage (Error Subtypes)

Error results share most fields with success results but set `is_error: true` and include
an `errors` array.

```json
{
  "type": "result",
  "subtype": "error_during_execution",
  "uuid": "msg-01JNK5X4VQFVNG9GS5K9XHERR",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "is_error": true,
  "errors": ["API Error: Rate limit reached"],
  "duration_ms": 8234,
  "duration_api_ms": 7100,
  "num_turns": 1,
  "total_cost_usd": 0.0031
}
```

**Error subtypes:**

| `subtype` | Description | `is_error` |
|---|---|---|
| `success` | Normal completion | `false` |
| `error_max_turns` | Hit `--max-turns` limit without completing | `true` |
| `error_max_budget_usd` | Hit `--max-budget-usd` limit (API key mode only) | `true` |
| `error_during_execution` | Unhandled error, rate limit, or crash | `true` |

**Important**: `total_cost_usd` is present and non-zero even in error results. Always
extract it regardless of `is_error` status — tokens were consumed up to the point of
failure.

### 1.6 StreamEventMessage (partial streaming only)

Only emitted when `--include-partial-messages` is passed alongside `--output-format
stream-json`. Carries raw Claude API streaming events for real-time text display.

```json
{
  "type": "stream_event",
  "uuid": "msg-01JNK5X4VQFVNG9GS5K9XHSTM",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "parent_tool_use_id": null,
  "event": {
    "type": "content_block_delta",
    "index": 0,
    "delta": {
      "type": "text_delta",
      "text": "I'll now implement"
    }
  }
}
```

**Inner `event.type` values:**

| `event.type` | Description |
|---|---|
| `message_start` | Beginning of a new assistant message |
| `content_block_start` | New content block begins (text or tool_use) |
| `content_block_delta` | Incremental delta (text chunk or tool input JSON fragment) |
| `content_block_stop` | Content block complete |
| `message_delta` | Message-level update (stop_reason, final usage) |
| `message_stop` | Message complete |

**Conductor use**: conductor sub-agents do NOT need `--include-partial-messages`. The
raw text streaming adds log noise without providing useful data for cost accounting or
state recovery. Use it only in the orchestrator's own terminal progress display logic.

### 1.7 rate_limit_event

A non-standard event type that appears in some Claude Code versions between turns when the
account approaches its usage limit.

```json
{
  "type": "rate_limit_event",
  "rate_limit_info": {
    "status": "allowed_warning",
    "overageDisabledReason": null,
    "isUsingOverage": false
  }
}
```

Conductor must handle this event without crashing (treat as known-unknown: log and skip).
The reliable rate limit signal remains `ResultMessage.is_error: true` with
`errors[0].includes("rate_limit")`.

### 1.8 CompactBoundaryMessage

Emitted when the session's conversation history was compacted. Relevant for per-session
log analysis — marks a context boundary.

```json
{
  "type": "compact_boundary"
}
```

---

## 2. Cost and Usage Extraction

### 2.1 Which Events Carry Cost Data

| Event | Cost data? | When to use |
|---|---|---|
| `system/init` | None | Extract `session_id`, `model` |
| `assistant` | Per-turn `usage` | Optional per-step breakdown; deduplicate by `message.id` |
| `user` | None | Tool call tracing only |
| `result` | `total_cost_usd`, `usage`, `modelUsage` | **Always use this for cost ledger** |

The `ResultMessage` is authoritative. Never sum `assistant.message.usage` across turns
as the cost ledger source — the `result` event already aggregates correctly and avoids
the duplicate-counting bug (GitHub Issue #6805, where parallel tool calls share a
`message.id` but emit multiple `assistant` events with identical usage).

### 2.2 Cost Extraction: Python Example

```python
import json
import asyncio


async def extract_result_from_stream(stream_reader) -> dict | None:
    """Parse a stream-json subprocess stdout, return the result event."""
    result_event = None
    session_id = None
    model = None

    async for line in stream_reader:
        line = line.decode().strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip malformed lines

        event_type = event.get("type")

        if event_type == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id")
            model = event.get("model")

        elif event_type == "result":
            result_event = event
            # Backfill session_id and model if not captured from init
            if not result_event.get("session_id") and session_id:
                result_event["session_id"] = session_id
            if model:
                result_event["_model_from_init"] = model

    return result_event


def build_cost_entry(
    result: dict,
    *,
    repo: str,
    issue_number: int,
    agent_type: str,
    session_id: str,
    model: str,
) -> dict:
    """Build a cost.jsonl entry from a ResultMessage."""
    from datetime import datetime, timezone

    usage = result.get("usage", {})
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": result.get("session_id", session_id),
        "repo": repo,
        "issue_number": issue_number,
        "agent_type": agent_type,
        "model": model,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "num_turns": result.get("num_turns", 0),
        "duration_ms": result.get("duration_ms", 0),
        "is_error": result.get("is_error", False),
        "error_subtype": result.get("subtype") if result.get("is_error") else None,
        "estimated_cost_usd": result.get("total_cost_usd"),
        "web_search_requests": (
            usage.get("server_tool_use", {}).get("web_search_requests", 0)
        ),
    }
```

**Note on `estimated_cost_usd`**: For API key sessions (`ANTHROPIC_API_KEY` set),
`total_cost_usd` is populated. For Pro/Max subscription sessions, `total_cost_usd` is
`null` or `0`. In subscription mode, the field is labeled `estimated_cost_usd` and
**must be computed** from token counts using the known API pricing rates (see
`12-subprocess-token-overhead.md` Section 7.1 for the formula). The subscription
session does not bill per token, but the estimate is needed for the usage governor's
relative budget tracking.

### 2.3 Subscription Mode: Computing the Estimate

For Pro/Max subscription sessions (no `ANTHROPIC_API_KEY`):

```python
# Prices as of March 2026 for Sonnet 4.6 (used as base for STE calculations)
SONNET_PRICES = {
    "input_per_mtok": 3.00,
    "output_per_mtok": 15.00,
    "cache_write_per_mtok": 3.75,   # 125% of input price
    "cache_read_per_mtok": 0.30,    # 10% of input price
}
OPUS_MULTIPLIER = 5.0  # Opus 4.6 is ~5x more expensive than Sonnet 4.6


def estimate_cost_usd(usage: dict, model: str) -> float:
    """Compute estimated USD cost from token counts and model."""
    prices = SONNET_PRICES
    multiplier = OPUS_MULTIPLIER if "opus" in model.lower() else 1.0

    cost = (
        usage.get("input_tokens", 0) / 1_000_000 * prices["input_per_mtok"]
        + usage.get("output_tokens", 0) / 1_000_000 * prices["output_per_mtok"]
        + usage.get("cache_creation_input_tokens", 0)
          / 1_000_000 * prices["cache_write_per_mtok"]
        + usage.get("cache_read_input_tokens", 0)
          / 1_000_000 * prices["cache_read_per_mtok"]
    ) * multiplier
    return round(cost, 6)
```

---

## 3. `cost.jsonl` Format

The cost ledger is an append-only JSONL file: one JSON object per line, one entry per
`claude -p` invocation. Written atomically (full entry appended as one line). Never
updated in place — only appended.

**Location**: `~/.local/share/conductor/{repo_slug}/cost.jsonl`
(configurable via `CONDUCTOR_LOG_DIR`, see `04-configuration.md`)

### 3.1 Entry Schema

```json
{
  "timestamp": "2026-03-02T14:35:22.418Z",
  "session_id": "550e8400-e29b-41d4-a716-446655440000",
  "repo": "myorg/myrepo",
  "issue_number": 7,
  "branch": "7-inbound-outbound-messages",
  "agent_type": "implementation",
  "model": "claude-sonnet-4-6",
  "input_tokens": 142500,
  "output_tokens": 18200,
  "cache_creation_input_tokens": 4800,
  "cache_read_input_tokens": 118000,
  "num_turns": 28,
  "duration_ms": 187432,
  "is_error": false,
  "error_subtype": null,
  "estimated_cost_usd": 0.8341,
  "web_search_requests": 0,
  "auth_mode": "subscription"
}
```

**Field descriptions:**

| Field | Type | Notes |
|---|---|---|
| `timestamp` | ISO 8601 UTC | When the subprocess completed (not started) |
| `session_id` | UUID string | From `system/init` or `result` event |
| `repo` | string | `owner/repo` format |
| `issue_number` | int or null | GitHub issue number; null for health-check probes |
| `branch` | string or null | Git branch name; null for probes |
| `agent_type` | string | One of: `research`, `implementation`, `probe`, `orchestrator` |
| `model` | string | Model ID from `system/init` event |
| `input_tokens` | int | From `result.usage.input_tokens` |
| `output_tokens` | int | From `result.usage.output_tokens` |
| `cache_creation_input_tokens` | int | From `result.usage.cache_creation_input_tokens` |
| `cache_read_input_tokens` | int | From `result.usage.cache_read_input_tokens` |
| `num_turns` | int | From `result.num_turns` |
| `duration_ms` | int | From `result.duration_ms` |
| `is_error` | bool | From `result.is_error` |
| `error_subtype` | string or null | From `result.subtype` when `is_error: true` |
| `estimated_cost_usd` | float or null | `result.total_cost_usd` (API key) or computed (subscription) |
| `web_search_requests` | int | From `result.usage.server_tool_use.web_search_requests` |
| `auth_mode` | string | `api_key` or `subscription` |

### 3.2 Querying the Ledger

```bash
# Total cost today
jq -s '[.[] | select(.timestamp | startswith("2026-03-02")) | .estimated_cost_usd // 0] | add' \
  ~/.local/share/conductor/myorg-myrepo/cost.jsonl

# Tokens consumed by implementation agents this week
jq -s '[.[] | select(.agent_type == "implementation") | .input_tokens + .output_tokens] | add' \
  ~/.local/share/conductor/myorg-myrepo/cost.jsonl

# Error rate
jq -s '[(. | length) as $total | ([.[] | select(.is_error)] | length) / $total * 100]' \
  ~/.local/share/conductor/myorg-myrepo/cost.jsonl
```

---

## 4. Per-Session Log Format

Each `claude -p` subprocess produces a raw JSONL session log. This is the unprocessed
stream from `--output-format stream-json`, with conductor-side phase annotations
prepended.

**Location**: `~/.local/share/conductor/{repo_slug}/sessions/{session_id}.jsonl`

### 4.1 Conductor Phase Annotations

Before spawning the subprocess, conductor writes a phase-start annotation. After the
subprocess exits, it writes a phase-end annotation. These annotations use a synthetic
event type `conductor_event` so they can be filtered separately from Claude events:

```json
{"type": "conductor_event", "subtype": "phase_start", "timestamp": "2026-03-02T14:32:00.000Z", "phase": "dispatch", "issue_number": 7, "branch": "7-inbound-outbound-messages", "agent_type": "implementation", "session_id": null}
```

```json
{"type": "conductor_event", "subtype": "phase_end", "timestamp": "2026-03-02T14:35:22.418Z", "phase": "dispatch", "issue_number": 7, "session_id": "550e8400-e29b-41d4-a716-446655440000", "exit_code": 0, "cost_entry_written": true}
```

### 4.2 Session Log File Structure

A complete session log file (in order of appearance):

```
# Line 1: conductor phase-start annotation
{"type":"conductor_event","subtype":"phase_start",...}

# Lines 2+: raw stream-json events from claude -p subprocess
{"type":"system","subtype":"init","session_id":"...","model":"...","tools":[...],...}
{"type":"assistant","uuid":"...","message":{"content":[{"type":"text","text":"..."},{"type":"tool_use","id":"...","name":"Bash","input":{"command":"git status"}}],"usage":{...}},...}
{"type":"user","uuid":"...","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"...","content":"On branch 7-my-feature\n..."}]},...}
... (more assistant/user pairs)
{"type":"result","subtype":"success","is_error":false,"num_turns":28,"total_cost_usd":0.8341,"usage":{...},...}

# Final line: conductor phase-end annotation
{"type":"conductor_event","subtype":"phase_end",...}
```

### 4.3 Rationale for Raw Event Logging

Storing raw `stream-json` events instead of a summarized format provides:

- **Full tool call audit trail**: exact tool names, inputs, outputs, durations — critical
  for debugging `is_error: true` cases
- **Replay capability**: the session JSONL can be fed back into analysis tools (e.g.,
  `ccusage`, `claude-code-log`) without transformation
- **Compatible with `--resume` forensics**: the session ID from the log can be used with
  `--resume` for investigation (provided the `CLAUDE_CONFIG_DIR` used during the session
  is preserved — see `02-session-continuity.md` Section 2.4)
- **Phase-start/end annotations separate from Claude events**: parseable without
  conflicting with the Claude-defined event types

---

## 5. Terminal Progress Display

### 5.1 Requirements

The conductor must display, for a human watching the terminal:

1. Which agents are currently running and on which issue/branch
2. How long each agent has been running (elapsed time)
3. Whether the agent is actively executing a tool or generating text
4. A summary line when an agent completes (success/error + token count)

### 5.2 Recommended Approach: Rich + asyncio

**Library: `rich`** (`pip install rich` / `uv add rich`).

Rich's `Progress` class with `SpinnerColumn`, `TextColumn`, and `TimeElapsedColumn` is
the best fit for conductor's needs:

- Supports multiple concurrent tasks (one row per running agent)
- Works with asyncio via `threading.Thread` running the `Progress` context in the
  background
- Handles stdout interleaving correctly when subprocess output is redirected to file
- `Live` display updates cleanly on standard terminals without flicker

**Pattern**: The subprocess stdout is **not** printed directly to the terminal — it is
captured line-by-line into the session log file. The Rich progress display is driven
by parsing key events from the stream (tool call starts, tool call ends, result event)
and updating task descriptions accordingly.

```python
import asyncio
import json
import threading
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    BarColumn,
    TaskID,
)
from rich.console import Console

console = Console(stderr=True)  # Progress to stderr, not stdout


class AgentProgressDisplay:
    """Thread-safe progress display for concurrent claude -p agents."""

    def __init__(self) -> None:
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]{task.description}"),
            TextColumn("[dim]{task.fields[status]}"),
            TimeElapsedColumn(),
            console=console,
            transient=False,
            refresh_per_second=4,
        )
        self._task_ids: dict[str, TaskID] = {}
        self._lock = threading.Lock()

    def __enter__(self):
        self._progress.__enter__()
        return self

    def __exit__(self, *args):
        self._progress.__exit__(*args)

    def add_agent(self, agent_id: str, description: str) -> None:
        with self._lock:
            task_id = self._progress.add_task(
                description, total=None, status="starting"
            )
            self._task_ids[agent_id] = task_id

    def update_status(self, agent_id: str, status: str) -> None:
        with self._lock:
            if task_id := self._task_ids.get(agent_id):
                self._progress.update(task_id, status=status)

    def complete_agent(self, agent_id: str, result_summary: str) -> None:
        with self._lock:
            if task_id := self._task_ids.get(agent_id):
                self._progress.update(
                    task_id,
                    description=f"[green]{self._progress.tasks[task_id].description}",
                    status=result_summary,
                )
                self._progress.stop_task(task_id)
```

**Consuming stream-json events to drive progress updates:**

```python
async def run_agent_with_progress(
    agent_id: str,
    cmd: list[str],
    session_log_path: str,
    display: AgentProgressDisplay,
) -> dict | None:
    """Run claude -p subprocess, update progress display, write session log."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout stream
    )

    result_event = None

    with open(session_log_path, "ab") as log_file:
        async for raw_line in process.stdout:
            log_file.write(raw_line)
            log_file.flush()

            try:
                event = json.loads(raw_line.decode().strip())
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            event_type = event.get("type")

            if event_type == "assistant":
                # Check if a tool call is starting
                content = event.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "tool")
                        tool_input = block.get("input", {})
                        # Show a brief summary of the tool call
                        summary = _tool_summary(tool_name, tool_input)
                        display.update_status(agent_id, f"[{summary}]")

            elif event_type == "user":
                # Tool completed — show turn count
                duration_ms = event.get("tool_use_result", {}).get("durationMs", 0)
                display.update_status(agent_id, f"tool done ({duration_ms}ms)")

            elif event_type == "result":
                result_event = event

    await process.wait()
    return result_event


def _tool_summary(tool_name: str, tool_input: dict) -> str:
    """Produce a brief human-readable summary of a tool call."""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")[:60]
        return f"Bash: {cmd}"
    elif tool_name in ("Read", "Edit", "Write"):
        path = tool_input.get("file_path", tool_input.get("path", ""))
        return f"{tool_name}: {path[-40:]}"
    return tool_name
```

### 5.3 When NOT to Use Rich Live

If conductor is running in a non-TTY environment (CI/CD, redirected to file), Rich
automatically disables its live display and falls back to plain logging. Always check:

```python
import sys
IS_TTY = sys.stderr.isatty()
```

In non-TTY mode, emit structured log events instead of progress updates.

---

## 6. Structured Logging: Recommendation

### 6.1 Recommendation: structlog with JSONL Output

Use **structlog** (not stdlib `logging`, not custom JSONL) for conductor's own
operational logs.

**Why structlog over stdlib:**
- Native key-value context binding (`log.bind(issue=7, branch="7-my-feature")`) —
  context propagates through all subsequent log calls in a request
- Configurable processor chain: dev mode → pretty human-readable; prod mode → JSONL
- Zero-overhead when logging is disabled (no string formatting until a level passes)
- First-class async support — no `logging.Handler` threading concerns
- stdlib integration available: can wrap existing libraries that use `logging`

**Why not custom JSONL:** stdlib's `logging.Handler` with a JSON formatter works, but
requires manual key extraction for every log call. structlog's bound context approach
(setting `issue_number`, `session_id`, `agent_type` once per worker coroutine) is safer
— no risk of forgetting to include a field.

### 6.2 Conductor Logging Configuration

```python
import structlog
import logging
import sys


def configure_logging(log_dir: str, *, debug: bool = False) -> None:
    """Configure structlog for conductor operations."""
    import os
    from datetime import date

    log_file = os.path.join(log_dir, f"conductor-{date.today()}.jsonl")
    os.makedirs(log_dir, exist_ok=True)

    # Shared processors for all environments
    shared_processors = [
        structlog.contextvars.merge_contextvars,  # thread-local bound context
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    if sys.stderr.isatty() and debug:
        # Development: pretty output to stderr
        structlog.configure(
            processors=shared_processors + [
                structlog.dev.ConsoleRenderer()
            ],
            logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        )
    else:
        # Production: JSONL to file
        with open(log_file, "a") as fh:
            structlog.configure(
                processors=shared_processors + [
                    structlog.processors.JSONRenderer()
                ],
                logger_factory=structlog.WriteLoggerFactory(file=fh),
            )
```

### 6.3 Log Event Types for Conductor Operations

The conductor operational log should emit structured events for the following lifecycle
events (not to be confused with `stream-json` event types):

```python
log = structlog.get_logger()

# Issue lifecycle
log.info("issue.claimed", issue=7, branch="7-my-feature", repo="myorg/myrepo")
log.info("agent.dispatched", issue=7, agent_type="implementation", model="claude-sonnet-4-6")
log.info("agent.completed", issue=7, session_id="...", num_turns=28, cost_usd=0.83, is_error=False)
log.info("pr.created", issue=7, pr_number=42, branch="7-my-feature")
log.info("pr.merged", issue=7, pr_number=42, strategy="squash")

# Error conditions
log.error("agent.rate_limited", issue=7, error_text="rate_limit_error", backoff_until="2026-03-02T19:35:00Z")
log.error("agent.failed", issue=7, error_subtype="error_during_execution", errors=["..."])
log.warning("ci.failing", issue=7, pr_number=42, checks=["test-suite"])

# Usage governor events
log.info("governor.backoff_entered", reason="rate_limit", reset_hint="2026-03-02T19:35:00Z")
log.info("governor.dispatch_deferred", reason="budget_low", estimated_remaining_usd=0.25)
log.info("rate_limit_exhausted", issue=7, agent_type="implementation", estimated_reset_time="...")
```

The `rate_limit_exhausted` event type is specifically cross-referenced from
`08-usage-scheduling.md` Section 5.4 as the signal the usage governor emits when a 429
is confirmed.

### 6.4 stdlib Logging Bridge

Third-party libraries (e.g., `httpx`, `asyncio`) use stdlib `logging`. Bridge these into
structlog:

```python
import structlog
import logging

# Route stdlib logging through structlog
structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
)
logging.basicConfig(
    level=logging.WARNING,
    handlers=[structlog.stdlib.ProcessorFormatter.wrap_for_formatter(
        # stdlib log records processed through the same processor chain
    )],
)
```

---

## 7. Notion Report Integration

The issue-worker and research-worker skills post Notion reports at the end of each run.
Conductor surfaces these in two ways:

### 7.1 PR Comment Linkback

When the Notion report URL is emitted in the agent's `result` text, conductor should
parse it and post it as a PR comment:

```bash
# Pattern to extract from result text:
NOTION_URL=$(echo "$RESULT_TEXT" | grep -oP 'https://www\.notion\.so/[^\s]+')
if [ -n "$NOTION_URL" ]; then
  gh pr comment "$PR_NUMBER" --body "Notion session report: $NOTION_URL"
fi
```

### 7.2 Conductor Terminal Summary

After all agents for a batch complete, conductor prints a terminal summary:

```
Batch complete: 2 issues resolved, 1 failed
  #7  7-inbound-outbound-messages  PR#42  ✓  28 turns  $0.83  https://notion.so/...
  #12 12-error-taxonomy             PR#43  ✓  19 turns  $0.61
  #16 16-llm-call-dataclass         ERROR  error_max_turns  12 turns  $0.43
Total estimated cost: $1.87
```

This summary is generated from the cost ledger entries written during the batch, not
from re-querying GitHub or Notion.

### 7.3 What Conductor Does NOT Do

Conductor does not create its own Notion pages. It surfaces links from agent output and
adds them to PR comments. The Notion-writing logic lives in the skill files used by the
sub-agents, not in conductor itself.

---

## 8. Log Rotation Policy

### 8.1 Files and Rotation Schedule

| File | Rotation | Retention | Compression |
|---|---|---|---|
| `conductor-{date}.jsonl` (orchestrator log) | Daily | 30 days | gzip after 7 days |
| `cost.jsonl` (cost ledger) | Never rotate; append-only | Permanent | None |
| `sessions/{session-id}.jsonl` (per-session) | On creation (one per session) | 14 days | gzip after 7 days |

**`cost.jsonl` is never rotated** because it is the historical billing record. Its size
grows at approximately 1 KB per agent invocation. At 20 invocations/day, this is ~20 KB/day
— negligible. A full year of active use would produce ~7 MB.

**Per-session logs** are the largest files (~100 KB–1 MB per session depending on turn
count). Retention at 14 days balances debugging access against disk usage.

### 8.2 Python Rotation via `TimedRotatingFileHandler`

For the conductor operational log, Python's `logging.handlers.TimedRotatingFileHandler`
integrates cleanly with structlog's stdlib bridge:

```python
from logging.handlers import TimedRotatingFileHandler

handler = TimedRotatingFileHandler(
    filename=os.path.join(log_dir, "conductor.jsonl"),
    when="midnight",
    interval=1,
    backupCount=30,      # keep 30 days
    encoding="utf-8",
    utc=True,
)
```

For per-session log cleanup, conductor should run a cleanup coroutine on startup:

```python
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta


def cleanup_old_session_logs(sessions_dir: str, max_age_days: int = 14) -> int:
    """Remove session log files older than max_age_days. Return count removed."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    removed = 0
    for path in Path(sessions_dir).glob("*.jsonl"):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            path.unlink()
            removed += 1
    return removed
```

---

## 9. Cross-References

- **`docs/research/02-session-continuity.md`**: The `session_id` captured from the
  `system/init` event is the same ID used for `--resume`. Per-session logs preserve
  the session ID for forensic `--resume` investigation. Section 8 of that doc documents
  the `CLAUDE_CONFIG_DIR` constraint that must be preserved for resume to work.

- **`docs/research/08-usage-scheduling.md`**: The `rate_limit_exhausted` event emitted
  by the usage governor (Section 5.4) should be structured as a conductor operational
  log entry. The `estimated_cost_usd` field in `cost.jsonl` provides the data for the
  conservative budget accounting in the governor's `can_dispatch()` check (Section 5.2).
  The `--max-budget-usd` flag is API-only and does not replace the internal ledger for
  Pro/Max sessions (Section 6).

- **`docs/research/12-subprocess-token-overhead.md`**: The `result` event's
  `usage.cache_read_input_tokens` and `usage.cache_creation_input_tokens` fields
  (Section F3 of that doc's follow-up recommendations) are now documented in Section 1.4
  here. The cost estimation formula in Section 2.3 above incorporates the cache discount
  factors defined in Section 7.1 of that doc. Research agent: ~80–160K effective billed
  tokens; implementation agent: ~80–160K effective billed tokens — these calibrate the
  `estimated_cost_usd` field in `cost.jsonl`.

---

## 10. Follow-Up Research Recommendations

### 10.1 Verify `total_cost_usd` Presence on Pro/Max Sessions

The field `total_cost_usd` in the `ResultMessage` is documented as populated for API
key sessions. Multiple community sources (GitHub Issue #5621, closed as "not planned")
suggest it is `0` or `null` for subscription sessions. This needs empirical verification:

1. Run `claude -p "echo OK" --output-format json` authenticated via subscription (no
   `ANTHROPIC_API_KEY`)
2. Check whether `total_cost_usd` is `0`, `null`, or absent
3. If non-zero: document whether it reflects a shadow cost calculation or is always wrong

**Suggested issue**: Not warranted — this is a narrow empirical verification that can be
done in the integration test suite without a separate research issue. Add a test case
when the runner is implemented.

### 10.2 Parallel Tool Call Deduplication Bug (Issue #6805) Status

GitHub Issue #6805 documented that `--output-format stream-json` duplicates token usage
statistics when Claude uses parallel tool calls in a single turn. The fix was reportedly
applied in Claude Code v2.0.x. Verify whether the current version (v2.1.50+) still
exhibits this behavior:

1. Trigger a parallel tool call (two `Bash` commands in one turn)
2. Sum `assistant.message.usage.input_tokens` across all events sharing the same
   `message.id`
3. Compare against `result.usage.input_tokens`
4. If they differ: the deduplication bug is still present; always use the `result` event

**Suggested issue**: `Research: Verify parallel tool call usage deduplication in stream-json v2.1.50+`
— however, check whether this overlaps with issue #12's F3 recommendation. Given the
overlap, this should be filed only if #12's follow-up work does not cover it.

### 10.3 `rate_limit_event` Schema Verification

The `rate_limit_event` documented in Section 1.7 was extracted from community observations
(gitbutlerapp/gitbutler Issue #12552). The exact schema of `rate_limit_info` (all subfields,
whether it correlates with `anthropic-ratelimit-unified-*` header values) is unverified.

**Suggested issue**: `Research: Verify rate_limit_event schema in stream-json output` — but
check first whether issue #23 (distinguishing weekly vs. 5-hour exhaustion) already covers
this. If it does, add this as a note to #23 rather than creating a new issue.

### 10.4 Per-Turn Usage Logging for Long Agents

The current design logs only the final `ResultMessage` to `cost.jsonl`. For very long
agents (30+ turns, 150K+ tokens), a mid-session cost snapshot would help detect runaway
sessions before they exhaust the budget.

Options to explore:
- Hook the `PostToolUse` event to emit intermediate cost estimates after every N turns
- Parse the `assistant.message.usage` per turn (with deduplication) and emit a
  `cost_checkpoint` event to the session log at configurable turn intervals

**Suggested issue**: `feat: mid-session cost checkpointing for long-running agents` — only
if the implementation phase reveals that cost overruns are a practical problem.

---

## 11. Sources

- [Run Claude Code programmatically — Claude Code Docs](https://code.claude.com/docs/en/headless) — `--output-format stream-json` usage; `session_id` extraction pattern; `--include-partial-messages`; `--output-format json` vs `stream-json`
- [Stream responses in real-time — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/streaming-output) — Complete streaming message flow diagram; `StreamEvent` structure; `include_partial_messages` option; tool call streaming events; `CompactBoundaryMessage`
- [Track cost and usage — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/cost-tracking) — `ResultMessage.total_cost_usd`; `usage` dict fields; `cache_creation_input_tokens`; `cache_read_input_tokens`; TypeScript per-step `modelUsage`; deduplication by `message.id`; cost on failed conversations
- [CLAUDE_AGENT_SDK_SPEC.md (SamSaffron gist)](https://gist.github.com/SamSaffron/603648958a8c18ceae34939a8951d417) — Authoritative field-level schema for all five message types: `SystemInitMessage`, `AssistantMessage`, `UserMessage`, `ResultMessage` (success and error), `StreamEventMessage`; error subtypes; `modelUsage` per-model fields
- [CLAUDE_AGENT_SDK_SPEC.md (POWERFULMOVES gist)](https://gist.github.com/POWERFULMOVES/58bcadab9483bf5e633e865f131e6c25) — Corroborating schema; `error_max_turns`, `error_max_budget_usd`, `error_during_execution` subtypes; `permission_denials` field; `server_tool_use.web_search_requests`
- [GitHub Issue #1920: Missing Final Result Event in Streaming JSON Output](https://github.com/anthropics/claude-code/issues/1920) — Confirms result event format with `cost_usd`, `num_turns`, `subtype: "success"`; CLI v1.0.18 intermittent missing result event
- [GitHub Issue #6805: Token Usage Statistics Duplicated in stream-json Mode](https://github.com/anthropics/claude-code/issues/6805) — Parallel tool call duplication bug; always use `ResultMessage.usage` for cost ledger, not per-turn sums
- [GitHub Issue #5904: Local /cost doubles usage when parsing session JSONL](https://github.com/anthropics/claude-code/issues/5904) — Session JSONL `usage` object schema; `cache_creation_input_tokens` nested types; deduplication requirement
- [GitHub Issue #12552 (gitbutlerapp): unknown variant `rate_limit_event`](https://github.com/gitbutlerapp/gitbutler/issues/12552) — `rate_limit_event` type observed in stream-json; `rate_limit_info` sub-object
- [Manage costs effectively — Claude Code Docs](https://code.claude.com/docs/en/costs) — `total_cost_usd` field; API-only billing; prompt caching; agent cost averages
- [CLI reference — Claude Code Docs](https://code.claude.com/docs/en/cli-reference) — `--output-format stream-json` flag; `--include-partial-messages` flag; `--max-budget-usd` API-only note
- [ClaudeCode.Types — Elixir SDK hexdocs](https://hexdocs.pm/claude_code/ClaudeCode.Types.html) — `model_usage.cost_usd` field; `usage` fields; `stop_reason` enum
- [Leveling Up Your Python Logs with Structlog — Dash0](https://www.dash0.com/guides/python-logging-with-structlog) — structlog processor chain; JSONL production configuration; dev vs. prod pattern
- [Logging Best Practices — structlog docs](https://www.structlog.org/en/stable/logging-best-practices.html) — `cache_logger_on_first_use`; `BytesLoggerFactory` for performance; contextvars integration
- [Python Logging: Top 6 Libraries — Better Stack](https://betterstack.com/community/community/guides/logging/best-python-logging-libraries/) — Comparison of structlog vs. stdlib for production JSONL output
- [Progress Display — Rich 14.1.0 docs](https://rich.readthedocs.io/en/stable/progress.html) — `SpinnerColumn`, `TimeElapsedColumn`, `Progress` class; multiple concurrent task support
- [Live Display — Rich 14.1.0 docs](https://rich.readthedocs.io/en/latest/live.html) — `Live` class for custom displays; asyncio threading pattern
- [Asyncio Subprocess — Python 3 docs](https://docs.python.org/3/library/asyncio-subprocess.html) — `create_subprocess_exec`; `PIPE` for stdout; `StreamReader` async line iteration
- [docs/research/02-session-continuity.md](02-session-continuity.md) — Session ID capture pattern from `system/init` event; `--resume` forensics; `CLAUDE_CONFIG_DIR` constraint
- [docs/research/08-usage-scheduling.md](08-usage-scheduling.md) — `rate_limit_exhausted` event; conservative budget accounting; `--max-budget-usd` API-only; cost estimates by agent type
- [docs/research/12-subprocess-token-overhead.md](12-subprocess-token-overhead.md) — Token cost formula with cache discounts; 80–160K billed tokens per agent; `result` event F3 follow-up recommendation
