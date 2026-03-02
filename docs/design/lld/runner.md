# LLD: Runner Module

**Module:** `runner`
**Files:** `src/composer/runner.py`, `src/composer/session.py`
**Issue:** #110
**Status:** Draft
**Date:** 2026-03-02

---

## 1. Module Overview

The `runner` module is the core execution engine of breadmin-composer. It assembles a `claude -p`
invocation, spawns the subprocess, reads and parses the stream-json output line by line, and
returns a structured `RunResult` to the caller. It does not make orchestration decisions — all
classification logic terminates in fields on `RunResult`; the caller (worker loop in `cli.py`)
decides the recovery action.

### 1.1 File Responsibilities

| File | Responsibility |
|------|----------------|
| `src/composer/runner.py` | `RunResult` dataclass; `run()` function; stream-json parsing; error classification |
| `src/composer/session.py` | `SessionCheckpoint` dataclass; read/write helpers for the per-issue checkpoint stored on disk |

### 1.2 Exports

**`runner.py`** exports:

| Symbol | Kind | Description |
|--------|------|-------------|
| `RunResult` | dataclass | Parsed result of one `claude -p` invocation |
| `run` | function | Invoke `claude -p`, parse output, return `RunResult` |

**`session.py`** exports:

| Symbol | Kind | Description |
|--------|------|-------------|
| `SessionCheckpoint` | dataclass | Persistent state for one in-flight issue (branch, session_id, retry count, last result) |
| `load_checkpoint` | function | Read a checkpoint JSON file from disk |
| `save_checkpoint` | function | Write a checkpoint JSON file to disk atomically |

---

## 2. `RunResult` Dataclass

`RunResult` is the return type of `run()`. Every field is populated from the parsed stream-json
output. When the subprocess exits before emitting a `result` event, fields are synthesized from
the exit code and stderr.

```python
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class RunResult:
    # --- terminal classification ---
    is_error: bool
    """True if the session ended with an error (mirrors result.is_error)."""

    subtype: str | None
    """
    result.subtype from the final stream-json result event.
    One of: "success", "error_max_turns", "error_max_budget_usd",
    "error_during_execution", "error_during_operation",
    "sigterm_internal", "missing_result_event", "unknown".
    None only when the result event was not received and no subtype can be inferred.
    """

    error_code: str | None
    """
    Secondary classification derived from result text or assistant.error field.
    Populated when subtype alone is ambiguous. Examples:
      "rate_limit"        — result text contains "Rate limit reached" (429 path)
      "billing_error"     — assistant event had error="billing_error" (402 path)
      "extra_usage_exhausted" — result text contains "out of extra usage"
      "auth_failure"      — result text contains "Invalid API key"
    None for successful runs.
    """

    # --- subprocess state ---
    exit_code: int
    """OS exit code of the claude subprocess. 0 = success, 1 = error, 130/137/143 = signals."""

    # --- cost and usage (from result.usage) ---
    total_cost_usd: float | None
    """
    result.total_cost_usd from the result event.
    Non-None for API-key sessions. For subscription sessions the API reports 0.0 or null;
    the logger module estimates cost from token counts. runner.py stores the raw value here.
    """

    input_tokens: int | None
    """result.usage.input_tokens. None if result event was not received."""

    output_tokens: int | None
    """result.usage.output_tokens. None if result event was not received."""

    cache_read_input_tokens: int | None
    """result.usage.cache_read_input_tokens. None if result event was not received."""

    cache_creation_input_tokens: int | None
    """result.usage.cache_creation_input_tokens. None if result event was not received."""

    # --- raw event and diagnostics ---
    raw_result_event: dict | None
    """
    The full parsed result event dict, as received from stream-json.
    None if no result event was received (subprocess crash, signal).
    Stored for diagnostic and logging purposes; logger.py reads this field.
    """

    stderr: str
    """
    Full text captured from the subprocess stderr file descriptor.
    Always populated (empty string if nothing was written to stderr).
    Used for secondary error classification when subtype is ambiguous.
    """

    # --- overage signal ---
    overage_detected: bool = field(default=False)
    """
    True if a rate_limit_event with isUsingOverage=true was observed during the session.
    Signals to the caller that the session consumed extra usage credits.
    """
```

### 2.1 Field Population Rules

| Field | Source | Fallback when no result event |
|-------|--------|-------------------------------|
| `is_error` | `result["is_error"]` | `True` (no result = error) |
| `subtype` | `result["subtype"]` | Synthesized: `"sigterm_internal"` for exit 143, `"missing_result_event"` for exit 0, `"unknown"` otherwise |
| `error_code` | Derived from `result["result"]` text or `assistant["error"]` field | Derived from stderr text |
| `exit_code` | `subprocess.returncode` | Same |
| `total_cost_usd` | `result.get("total_cost_usd")` | `None` |
| `input_tokens` | `result["usage"]["input_tokens"]` | `None` |
| `output_tokens` | `result["usage"]["output_tokens"]` | `None` |
| `cache_read_input_tokens` | `result["usage"]["cache_read_input_tokens"]` | `None` |
| `cache_creation_input_tokens` | `result["usage"]["cache_creation_input_tokens"]` | `None` |
| `raw_result_event` | The full `result` event dict | `None` |
| `stderr` | All bytes read from stderr fd | `""` |
| `overage_detected` | Any `rate_limit_event` with `rate_limit_info.isUsingOverage == True` | `False` |

---

## 3. `run()` API

### 3.1 Function Signature

```python
from pathlib import Path


def run(
    prompt: str,
    allowed_tools: list[str],
    env: dict[str, str],
    *,
    dry_run: bool = False,
    max_turns: int = 100,
    append_system_prompt_file: Path | None = None,
    mcp_config: Path | None = None,
) -> RunResult:
    """
    Invoke `claude -p` as a subprocess and return a structured result.

    Parameters
    ----------
    prompt:
        The user-turn prompt passed to claude via `-p`.
    allowed_tools:
        List of tool names permitted for this invocation. Passed as
        --allowedTools "<comma-separated>". Must not be empty.
    env:
        Complete environment dict for the subprocess. Caller constructs
        this via config.py; runner.py does not add or modify env vars.
    dry_run:
        If True, print the assembled command to stdout and return a mock
        RunResult with is_error=False, subtype="success". No subprocess
        is spawned.
    max_turns:
        Maximum number of agentic turns before the session exits with
        subtype "error_max_turns". Passed as --max-turns.
    append_system_prompt_file:
        If provided, pass --append-system-prompt <path>. Use for stable
        worker-type instructions that benefit from being cache-friendly
        (see Section 7). Mutually exclusive with inline system prompt
        injection in the prompt string.
    mcp_config:
        If provided, pass --mcp-config <path>. If None, runner passes
        --strict-mcp-config with an empty inline JSON object to suppress
        all MCP servers (see Section 7).

    Returns
    -------
    RunResult
        Fully populated result. is_error=False only when subtype="success".
    """
```

### 3.2 Argument Semantics

| Parameter | Semantics |
|-----------|-----------|
| `prompt` | Passed verbatim as the `-p` argument. Must not contain shell metacharacters (the subprocess is exec'd, not shell-interpolated). |
| `allowed_tools` | Defines exactly which tools the agent can call. An empty list is rejected with `ValueError` — running with no tools is almost certainly a bug. |
| `env` | The complete environment. `runner.py` does not inherit `os.environ` — callers pass explicit dicts. This prevents accidental token or config leakage. |
| `dry_run` | Useful for `composer health` and testing. Returns a mock success result without touching the network. |
| `max_turns` | Hard cap on agent iterations. Default 100 is high; callers set tighter values per worker type (research: 30, implementation: 50). |
| `append_system_prompt_file` | When present, the file content is appended to Claude's default system prompt (additive). Use this for stable worker instructions; the unchanged prefix is cache-eligible. |
| `mcp_config` | When None, runner injects `--strict-mcp-config --mcp-config '{}'` to suppress all MCP servers and eliminate their token overhead. When provided, the file path is passed directly. |

### 3.3 CLI Argument Assembly

`run()` assembles the `claude` command as a Python list (not a shell string). Arguments are added
in the order shown. Each parameter maps to flags as follows:

```python
cmd = ["claude", "-p", prompt]

# Output format — always stream-json
cmd += ["--output-format", "stream-json"]

# Tool restriction
cmd += ["--allowedTools", ",".join(allowed_tools)]

# Turn limit
cmd += ["--max-turns", str(max_turns)]

# Skip interactive permission prompts
cmd += ["--dangerously-skip-permissions"]

# Slash command overhead elimination
cmd += ["--disable-slash-commands"]

# No session history written to disk (workers are ephemeral)
cmd += ["--no-session-persistence"]

# System prompt override: use append-system-prompt if file provided,
# otherwise skip (the default Claude Code system prompt is used or
# overridden by caller-supplied env vars)
if append_system_prompt_file is not None:
    cmd += ["--append-system-prompt", str(append_system_prompt_file)]

# MCP suppression or explicit config
if mcp_config is None:
    cmd += ["--strict-mcp-config", "--mcp-config", "{}"]
else:
    cmd += ["--mcp-config", str(mcp_config)]
```

The subprocess is launched via `subprocess.Popen` with:
- `stdout=subprocess.PIPE` — stream-json is read from stdout
- `stderr=subprocess.PIPE` — stderr is captured to `RunResult.stderr`
- `env=env` — explicit environment, no `os.environ` inheritance
- `text=False` — binary mode; the parser decodes bytes to str with `"utf-8"` and `errors="replace"`

### 3.4 Dry-Run Behavior

When `dry_run=True`:

1. Assemble the command list exactly as described in 3.3.
2. Print to stdout: `"[dry-run] " + " ".join(shlex.quote(a) for a in cmd)`.
3. Return a mock `RunResult` with:
   - `is_error=False`
   - `subtype="success"`
   - `error_code=None`
   - `exit_code=0`
   - `total_cost_usd=0.0`
   - All token fields set to `0`
   - `raw_result_event=None`
   - `stderr=""`
   - `overage_detected=False`

No subprocess is spawned. No network calls are made.

---

## 4. `--allowedTools` Construction

The `allowed_tools` list is assembled by the worker loop in `cli.py` before calling `run()`.
`runner.py` accepts whatever list it receives; it does not know which worker type is calling.

### 4.1 Tool Sets Per Worker Type

| Worker type | Tools | Rationale |
|-------------|-------|-----------|
| research-worker agent | `Bash`, `Read`, `Write`, `Glob`, `Grep`, `WebSearch`, `WebFetch` | Needs web access for literature search; file write for doc output; no gh CLI needed (research docs are plain markdown files, pushed via Bash) |
| design-worker | `Bash`, `Read`, `Write`, `Glob`, `Grep` | Reads merged research docs, writes impl issue bodies via `gh` CLI invoked through Bash; no web access needed |
| impl-worker agent | `Bash`, `Read`, `Edit`, `Write`, `Glob`, `Grep` | Full file editing; Bash for git, tests, lint, gh CLI |
| health probe | `Bash` | Single turn; only needs to echo a string or read a file |

Notes:
- `gh` CLI access is provided via the `Bash` tool, not a dedicated tool — agents run `gh issue view`, `gh pr create`, etc. as shell commands.
- `WebSearch` and `WebFetch` are built-in Claude Code tools, not MCP servers. They are safe to include without enabling any MCP configuration.
- The `Agent` tool (`Task` in versions before 2.1.63) is intentionally excluded from all worker tool sets. Workers do not spawn their own sub-agents; the orchestrator manages parallelism.

### 4.2 Passing the Tool List to `claude`

The tool list is passed as a single comma-separated string:

```
--allowedTools "Bash,Read,Edit,Write,Glob,Grep"
```

This is equivalent to the `--tools` flag alias used in some documentation. `runner.py` uses
`--allowedTools` (the canonical flag name in the official CLI reference).

### 4.3 `Agent` vs. `Task` in `allowedTools` (Version Compatibility)

In Claude Code v2.1.63+, the subagent tool was renamed from `Task` to `Agent`. If an orchestrator
session needs to dispatch subagents via the `Agent` tool (not the subprocess pattern), include
`Agent` in the tool list for v2.1.63+ and `Task` for older versions.

Worker agents in breadmin-composer **do not** use the `Agent`/`Task` tool — they are the leaf
nodes. This note applies only if a future orchestrator mode uses in-process `Agent` tool dispatch
rather than subprocess spawn.

---

## 5. Stream-JSON Parsing

### 5.1 Overview

`claude -p --output-format stream-json` writes newline-delimited JSON (NDJSON) to stdout. Each
complete line is one JSON object. Lines arrive as the model generates output — they are not
buffered until process exit. The parser must handle:

1. Partial lines (a TCP read may deliver half a JSON object)
2. Unknown event types (must not crash the parser)
3. Missing result event (subprocess killed before emitting it)

### 5.2 Parsing Loop (Pseudocode)

```python
import json
import subprocess
import select
from collections import deque


def _parse_stream(proc: subprocess.Popen) -> tuple[dict | None, list[dict], str, bool]:
    """
    Read stream-json events from proc.stdout until EOF.

    Returns
    -------
    result_event : dict | None
        The parsed "result" event, or None if not received.
    all_events : list[dict]
        All events received (for session logging).
    stderr_text : str
        Full stderr captured.
    overage_detected : bool
        True if any rate_limit_event with isUsingOverage=True was observed.
    """
    result_event: dict | None = None
    all_events: list[dict] = []
    overage_detected: bool = False
    read_buffer: str = ""

    stdout_fd = proc.stdout.fileno()
    stderr_fd = proc.stderr.fileno()
    stderr_chunks: list[bytes] = []

    stdout_done = False
    stderr_done = False

    while not (stdout_done and stderr_done):
        # Use select() to multiplex stdout and stderr without blocking
        read_ready, _, _ = select.select(
            [proc.stdout, proc.stderr] if not stdout_done else [proc.stderr],
            [], [],
            timeout=1.0,
        )

        for readable in read_ready:
            if readable is proc.stdout:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    stdout_done = True
                else:
                    read_buffer += chunk.decode("utf-8", errors="replace")

                    # Split on newlines; last segment may be partial
                    lines = read_buffer.split("\n")
                    read_buffer = lines[-1]   # keep incomplete tail in buffer

                    for line in lines[:-1]:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            # Corrupted line — log and skip; do not crash
                            _log_parse_warning(f"json decode failed: {line[:200]!r}")
                            continue

                        all_events.append(event)
                        event_type = event.get("type")

                        if event_type == "result":
                            result_event = event

                        elif event_type == "rate_limit_event":
                            info = event.get("rate_limit_info", {})
                            if info.get("isUsingOverage"):
                                overage_detected = True

                        # All other event types: append to all_events, continue

            elif readable is proc.stderr:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    stderr_done = True
                else:
                    stderr_chunks.append(chunk)

    # Flush any remaining partial line from buffer (rare: incomplete last line)
    if read_buffer.strip():
        try:
            event = json.loads(read_buffer.strip())
            all_events.append(event)
            if event.get("type") == "result":
                result_event = event
        except json.JSONDecodeError:
            pass  # Incomplete fragment — discard

    stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return result_event, all_events, stderr_text, overage_detected
```

### 5.3 Buffer Handling for Partial Lines

TCP reads and pipe reads may deliver JSON objects split across multiple `read()` calls. The parser
maintains a string accumulation buffer (`read_buffer`). After each read, it splits on `"\n"` and
processes all complete lines (all segments except the last). The last segment — which may or may
not be a complete JSON object — is retained in the buffer until the next read. At EOF, the buffer
is flushed and parsed as a final attempt.

This approach is correct for NDJSON where:
- Each JSON object is terminated by exactly one `"\n"`
- JSON objects do not themselves contain unescaped literal newlines

These invariants hold for `claude -p --output-format stream-json`.

### 5.4 Event Types Handled

| `type` value | Action |
|---|---|
| `system` (subtype `init`) | Extract `session_id`, `model`, and available tools list. Populate `LogContext`. |
| `assistant` | Check for top-level `"error"` field (see Section 6 — billing_error detection). Log via `log_session_event`. |
| `user` | Log via `log_session_event` (tool result echo). |
| `tool_use` | Rarely appears as a standalone event; usually nested inside `assistant.message.content`. Ignore at top level. |
| `tool_result` | Same as above. |
| `result` | **Terminal event.** Store as `result_event`. Parsing continues until EOF to drain any trailing stderr. |
| `rate_limit_event` | Check `rate_limit_info.isUsingOverage`. Set `overage_detected=True` if present. Log warning. Do not treat as terminal. |
| `compact_boundary` | Indicates auto-compaction occurred. Log. No action needed. |
| Anything else | Log as `"unknown type=<type>"` via `log_session_event`. Do not raise. |

### 5.5 Identifying the Terminal `result` Event

The `result` event has `"type": "result"`. It is always the last event emitted before the
subprocess exits. The parser captures it by checking `event.get("type") == "result"` and storing
the full event dict.

Because the subprocess normally exits immediately after emitting the `result` event, the parser
will observe EOF on stdout shortly after receiving the result event. The parser does not stop
reading at the result event — it reads until EOF to ensure no events are dropped and stderr is
fully drained.

### 5.6 Synthesizing `RunResult` When No `result` Event Appears

When the subprocess exits without emitting a `result` event (crash, signal, or the known bug in
GitHub Issue #8126):

```python
exit_code = proc.returncode

if exit_code == 0:
    # Exit 0 with no result event — known bug in some Claude Code versions.
    # Treat conservatively as missing_result_event.
    subtype = "missing_result_event"
    is_error = True   # caller will check whether PR was created as a reconciliation step

elif exit_code == 143:
    # SIGTERM — typically the conductor watchdog or an internal timeout.
    subtype = "sigterm_internal"
    is_error = True

elif exit_code == 130:
    # SIGINT — operator interrupt.
    subtype = "user_interrupt"
    is_error = True

elif exit_code == 137:
    # SIGKILL — OOM or forced kill.
    subtype = "sigkill"
    is_error = True

elif exit_code == 124:
    # timeout(1) wall-clock kill.
    subtype = "timeout"
    is_error = True

else:
    subtype = "unknown"
    is_error = True

return RunResult(
    is_error=is_error,
    subtype=subtype,
    error_code=_classify_error_code_from_stderr(stderr_text),
    exit_code=exit_code,
    total_cost_usd=None,
    input_tokens=None,
    output_tokens=None,
    cache_read_input_tokens=None,
    cache_creation_input_tokens=None,
    raw_result_event=None,
    stderr=stderr_text,
    overage_detected=overage_detected,
)
```

---

## 6. Error Classification

### 6.1 Two Classification Layers

Error classification in `runner.py` operates at two independent layers:

1. **`subtype`**: Directly from `result["subtype"]`. This is the primary authoritative signal
   documented by the Elixir SDK and Claude Code official docs.

2. **`error_code`**: A secondary refinement derived from `result["result"]` text, `assistant["error"]`
   field, and stderr. This disambiguates cases where `subtype` alone is underspecified (e.g.,
   `"error_during_operation"` covers both 429 rate limit and "out of extra usage").

`runner.py` populates both fields on `RunResult` and returns them to the caller. The caller (worker
loop in `cli.py`) consults both to decide the recovery action. `runner.py` does not enqueue
retries, back off, or modify GitHub state — those are orchestrator responsibilities.

### 6.2 `result.subtype` Taxonomy

| `result.subtype` | Exit Code | `is_error` | Meaning | Caller Action |
|---|---|---|---|---|
| `"success"` | `0` | `false` | Task completed normally | Proceed: monitor PR, run CI |
| `"error_max_turns"` | `1` | `true` | `--max-turns` limit exhausted | Abandon: task too large; file follow-up issue to split |
| `"error_max_budget_usd"` | `1` | `true` | `--max-budget-usd` cap hit (API key sessions only) | Abandon or reconfigure budget |
| `"error_during_operation"` | `1` | `true` | Rate limit (429) or other mid-session API error | Inspect `error_code`; see Section 6.3 |
| `"error_during_execution"` | `1` | `true` | Unhandled error during a tool call or model step; also used for billing failures (402) | Inspect `error_code`; may retry once |
| `"sigterm_internal"` | `143` | `true` | SIGTERM before any result emitted (watchdog kill or spontaneous internal timeout) | Retry once with backoff |
| `"missing_result_event"` | `0` | `true` | Exit 0 but no result event — known bug; PR may already exist | Check for existing PR; reconcile |
| `"user_interrupt"` | `130` | `true` | Operator SIGINT | Clean up; do not retry |
| `"sigkill"` | `137` | `true` | SIGKILL (OOM or forced kill) | Clean up; retry once if cause is transient |
| `"timeout"` | `124` | `true` | Wall-clock timeout via `timeout(1)` | Retry once with longer timeout |
| `"unknown"` | any | `true` | No result event and unrecognized exit code | Log for human review; abandon |

### 6.3 `error_code` Refinements for Ambiguous Subtypes

When `subtype` is `"error_during_operation"` or `"error_during_execution"`, `error_code` provides
the specific cause. The classification function:

```python
def _classify_error_code(
    result_event: dict | None,
    assistant_events: list[dict],
    stderr: str,
) -> str | None:
    """
    Derive secondary error_code from result text, assistant events, and stderr.
    Returns None for non-error runs.
    """
    # Billing error — check assistant events for error field (HTTP 402 path)
    for evt in assistant_events:
        if evt.get("type") == "assistant" and evt.get("error") == "billing_error":
            return "billing_error"

    if result_event is None:
        # No result event — try stderr
        stderr_lower = stderr.lower()
        if "invalid api key" in stderr_lower or "authentication" in stderr_lower:
            return "auth_failure"
        return None

    result_text = (result_event.get("result") or "").lower()

    # 429 rate limit path
    if "rate limit" in result_text or "rate_limit_error" in result_text:
        # Sub-classify: overage exhaustion vs. standard window exhaustion
        if "out of extra usage" in result_text or "extra usage exhausted" in result_text:
            return "extra_usage_exhausted"
        return "rate_limit"

    # Auth failure
    if "invalid api key" in result_text or "authentication" in result_text:
        return "auth_failure"

    # Content filtering
    if "content filtering" in result_text or "safety" in result_text:
        return "content_refused"

    # Model overload (transient)
    if "overloaded_error" in result_text or "model is overloaded" in result_text:
        return "model_overloaded"

    # Context window exceeded
    if "context_length_exceeded" in result_text:
        return "context_length_exceeded"

    # Billing in result text (secondary check for 402 path)
    if "billing" in result_text:
        return "billing_error"

    return None
```

### 6.4 HTTP 402 vs. HTTP 429 Discrimination

The two billing-related error paths are architecturally distinct in stream-json output:

**HTTP 429 (rate limit — subscription window exhaustion):**

- Does emit `rate_limit_event` events with `rate_limit_info` during the session
- Final `result` event: `subtype = "error_during_operation"`, `result` text contains `"API Error: Rate limit reached"`
- `error_code` resolved to `"rate_limit"` (or `"extra_usage_exhausted"` for spend-cap hits)

**HTTP 402 (billing authorization failure — extra usage billing layer):**

- Does NOT emit `rate_limit_event` — 402 responses do not include `anthropic-ratelimit-unified-*` headers
- An `assistant` event with top-level field `"error": "billing_error"` appears before the result event
- Final `result` event: `subtype` is most likely `"error_during_execution"` (the catch-all for unhandled API errors); confirmed `subtype` for 402 is not documented as of March 2026
- `error_code` resolved to `"billing_error"` from the `assistant.error` field check

```
Detection priority:
  1. Check all assistant events for event["error"] == "billing_error"
     → If found: error_code = "billing_error"  (regardless of result.subtype)
  2. Check result["result"] text for "rate limit" patterns
     → If found: error_code = "rate_limit" or "extra_usage_exhausted"
  3. Everything else: error_code = None or specific pattern match
```

The caller maps `error_code` to recovery action, not `runner.py`:

| `error_code` | Caller action |
|---|---|
| `"rate_limit"` | Backoff until reset timestamp (from stderr or `/api/oauth/usage` endpoint); re-enqueue |
| `"extra_usage_exhausted"` | Halt dispatch; alert operator; do not retry |
| `"billing_error"` | Halt dispatch; alert operator (402 = billing auth failure; not transient) |
| `"auth_failure"` | Halt all dispatch; alert operator; do not retry |
| `"content_refused"` | Abandon issue; file human-review note |
| `"model_overloaded"` | Exponential backoff; retry up to 3 times |
| `"context_length_exceeded"` | Abandon issue; task scope too large |
| `None` (on error) | Inspect stderr; if unrecognized, abandon after max retries |

### 6.5 `rate_limit_event` Stream Event Handling

The `rate_limit_event` may be emitted by Claude Code when a rate-limit condition is active. Per
GitHub Issue #26498, the Python Agent SDK did not handle this event type and crashed on it. The
runner parser must accept this event type gracefully:

```python
elif event_type == "rate_limit_event":
    info = event.get("rate_limit_info", {})
    if info.get("isUsingOverage"):
        overage_detected = True
        _log_warning("overage consumption detected in session")
    # Continue parsing — this event is informational; the session may still succeed
```

The `rate_limit_event` is **not terminal**. When `isUsingOverage=True`, the session continues
consuming extra usage credits. When `isUsingOverage=False` with a `rejected` status, the session
will produce a `result` event with `is_error=True` shortly after.

---

## 7. Token Overhead Minimization

`runner.py` enforces the 4-layer isolation strategy from research doc #12 through its default
argument assembly. The goal is to reduce per-turn overhead from ~50,000 tokens (unoptimized) to
~5,000 tokens.

### 7.1 Flags That Reduce Token Overhead

| Flag | Tokens Saved per Turn | Notes |
|------|----------------------|-------|
| `--disable-slash-commands` | ~1,000–5,000 | Eliminates skill file injection. Always applied by `run()`. |
| `--no-session-persistence` | N/A (disk I/O) | Prevents session history files from accumulating. Always applied. |
| `--strict-mcp-config --mcp-config '{}'` (default) | ~5,000–40,000+ | Suppresses all MCP tool catalogs. Applied when `mcp_config=None`. |
| `--allowedTools` scoping | ~2,000–8,000 | Restricts tool definitions injected into system prompt. Caller provides appropriate list. |
| CWD set to worktree | ~4,000–15,000 | Prevents ancestor CLAUDE.md traversal. Caller sets `cwd` in env or subprocess args. |

**Note on `--system-prompt` vs. `--append-system-prompt`**: `run()` does not use `--system-prompt`
(replacement mode) by default, because replacing the entire default prompt removes built-in tool
guidance that agents rely on for correct behavior. Instead, `--append-system-prompt-file` is
offered as an optional additive mechanism.

### 7.2 Cache-Aware Prompt Structure

To maximize prompt cache hits within a multi-turn session, the `append_system_prompt_file` content
should follow this structure:

```
[Stable prefix — cache-eligible]
  - Worker type identity ("You are a research agent…")
  - Invariant constraints ("Always commit with Closes #N…")
  - Tool usage guidance

[Variable suffix — do not cache]
  - Issue-specific instructions (passed inline in the prompt parameter)
  - Branch name, issue number, paths to modify
```

The `append_system_prompt_file` path should point to a file that changes infrequently (e.g.,
once per worker type, not per issue). The per-issue specifics belong in the `prompt` parameter.
Claude Code caches the prefix of the system prompt across turns within a session; stable content
at the top of the prompt maximizes cache hit rate.

### 7.3 When to Use `append_system_prompt_file` vs. Inline

| Scenario | Recommended approach |
|----------|---------------------|
| Worker type instructions (10–50 lines, same for all issues of this worker type) | `append_system_prompt_file` pointing to a static file |
| Issue-specific instructions (branch name, file scope, issue number) | Inline in `prompt` parameter |
| Single-turn probe (< 5 turns expected) | Inline in `prompt`; overhead saving from file path is negligible |
| Long-running impl agent (20–50 turns) | `append_system_prompt_file` — amortizes cache write cost across many turns |

### 7.4 Environment Variables for Overhead Reduction

The `env` dict passed by the caller should include:

```python
{
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",     # Prevents MEMORY.md injection (~500–5,000 tokens)
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # Suppresses telemetry pings
    "DISABLE_AUTOUPDATER": "1",                  # Prevents version check traffic
    "DISABLE_ERROR_REPORTING": "1",              # Suppresses Sentry/error reporting
    "DISABLE_TELEMETRY": "1",                    # Full telemetry suppression
    # ... plus ANTHROPIC_API_KEY or OAuth tokens as appropriate
}
```

These are set by `config.py` when constructing the subprocess environment dict. `runner.py` passes
the dict through unchanged.

---

## 8. Stdout/Stderr Routing

### 8.1 Routing Summary

| Data | Destination | Notes |
|------|-------------|-------|
| `claude` stdout (stream-json events) | Parse buffer → `RunResult` and `log_session_event` calls | Never echoed to terminal |
| `claude` stderr | Captured to `RunResult.stderr`; read in full after EOF | Used for secondary error classification; not echoed to terminal |
| `runner.py` log calls | `log_session_event` / `log_conductor_event` → JSONL files | All structured logging goes to files (see `lld/logger.md`) |
| Dry-run output | `sys.stdout` | Only exception: dry-run prints the assembled command |
| Parse warnings | Conductor log via `log_conductor_event` | Corrupted lines trigger a warning log, not a crash |

### 8.2 No Live Output to Terminal

`runner.py` is a headless module. It does not print stream-json events, model responses, or tool
call output to the terminal. The operator never sees the agent's work in real time via this module.
Observability is provided exclusively through the JSONL log files defined in `lld/logger.md`.

This is intentional: the worker loop may run multiple agents in parallel, and interleaved terminal
output would be unreadable. The `composer health` command (which uses dry-run) is the only `runner`
code path that writes to stdout.

### 8.3 Stderr Capture Detail

Stderr is read concurrently with stdout using `select()` to prevent the stderr pipe buffer from
filling (which would deadlock the subprocess if it writes more than ~65 KB to stderr). After the
subprocess exits, all stderr bytes are joined and decoded as UTF-8 with replacement for invalid
byte sequences. The decoded string is stored verbatim in `RunResult.stderr`.

---

## 9. Interface Summary

### 9.1 `runner.py` Exports

| Symbol | Kind | Signature | Notes |
|--------|------|-----------|-------|
| `RunResult` | dataclass | See Section 2 | Return type of `run()` |
| `run` | function | `run(prompt, allowed_tools, env, *, dry_run, max_turns, append_system_prompt_file, mcp_config) -> RunResult` | Core invocation API |

### 9.2 `session.py` Exports

`session.py` holds the checkpoint data structures used by the worker loop to persist state across
process restarts. `runner.py` does not call `session.py` directly — the worker loop in `cli.py`
reads the checkpoint before calling `run()` and writes it after `run()` returns.

| Symbol | Kind | Description |
|--------|------|-------------|
| `SessionCheckpoint` | dataclass | Persistent state for one in-flight issue |
| `load_checkpoint` | function | `load_checkpoint(path: Path) -> SessionCheckpoint \| None` — returns `None` if file does not exist |
| `save_checkpoint` | function | `save_checkpoint(checkpoint: SessionCheckpoint, path: Path) -> None` — atomic write via `tmp` + rename |

**`SessionCheckpoint` fields:**

```python
@dataclass
class SessionCheckpoint:
    issue_number: int
    branch: str
    session_id: str | None        # populated after system/init event received
    retry_count: int              # incremented by caller on each re-dispatch
    last_subtype: str | None      # result.subtype from the last completed run
    last_exit_code: int | None    # exit code from the last run
    pr_url: str | None            # populated after PR is created
    created_at: str               # ISO 8601 UTC timestamp of first dispatch
    updated_at: str               # ISO 8601 UTC timestamp of last update
```

### 9.3 Consumer Call Map

| Consumer | Calls | When |
|----------|-------|------|
| `cli.py` worker loop | `run()` | Once per agent dispatch; passed `prompt`, `allowed_tools`, `env`, and config-derived kwargs |
| `cli.py` worker loop | `save_checkpoint()` | After `run()` returns; writes `RunResult.subtype` and `exit_code` to checkpoint |
| `cli.py` worker loop | `load_checkpoint()` | On startup and before each re-dispatch; reads retry count and last subtype |
| `health.py` | `run(dry_run=True)` | During `composer health` to verify CLI arg assembly without spawning a subprocess |
| `logger.py` | `RunResult.raw_result_event` | After `run()` returns; `log_cost()` reads this field to extract usage metrics |

### 9.4 What `runner.py` Does NOT Do

- Does not make orchestration decisions (retry, backoff, abandon) — that is the caller's responsibility
- Does not write to GitHub (no `gh` CLI calls)
- Does not read or write checkpoints
- Does not modify the `env` dict it receives
- Does not inherit `os.environ` implicitly — the caller provides the complete environment
- Does not print to the terminal (except in dry-run mode)
- Does not validate that `allowed_tools` names are real Claude Code tool names

---

## 10. Cross-References

| Document | Relevant sections |
|----------|-------------------|
| `docs/research/03-error-handling.md` | §1 Exit code taxonomy; §1.2 result.subtype values; §2 stream-json event schema; §2.2 result event full schema |
| `docs/research/12-subprocess-token-overhead.md` | §2 Four-layer isolation strategy; §8 Recommended invocation flags; §8.4 `--disable-slash-commands`; §4 `CLAUDE_CODE_DISABLE_AUTO_MEMORY` |
| `docs/research/23-429-error-cap-distinction.md` | §2.1 429 JSON body; 402 vs 429 architectural distinction |
| `docs/research/63-headless-overage-consumption.md` | §3 `rate_limit_event` schema; §3.2 parser deficiency bug (Issue #26498); §6 overage exhaustion signal |
| `docs/research/87-http-402-json-body.md` | §2.1 `SDKAssistantMessageError` type; §2.2 inferred stream-json for 402; §2.3 distinguishing 402 vs 429 |
| `docs/research/01-agent-tool-in-p-mode.md` | §1.3 Task/Agent tool in allowedTools; §1 Note on v2.1.63 rename |
| `docs/design/lld/logger.md` | §6.1 `LogContext`; §6.2 `log_cost`; §6.4 `log_session_event`; cost ledger schema |
