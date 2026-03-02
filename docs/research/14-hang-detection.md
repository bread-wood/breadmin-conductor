# Research: Headless Agent Hang Detection and Watchdog Strategy

**Issue**: #14
**Milestone**: M1: Foundation
**Status**: Complete
**Date**: 2026-03-02
**Depends on**: #1 (subprocess spawning pattern), #3 (error handling)

---

## Executive Summary

`claude -p` processes can hang indefinitely in headless mode through several distinct failure
modes. There is **no native `--timeout` CLI flag** in Claude Code — timeout must be enforced
externally by the conductor. The most reliable watchdog strategy combines three timeout tiers
(inactivity timeout, total session timeout, and post-result hang timeout) implemented as an
asyncio subprocess monitor that reads `--output-format stream-json` events and escalates from
SIGTERM to SIGKILL when a tier is breached. Cleanup after a kill requires removing the
`in-progress` label and the orphaned worktree before retrying or abandoning the issue.

---

## 1. Hang Patterns for `claude -p` Processes

Six distinct hang patterns have been confirmed across GitHub issues and community reports.
Understanding the cause is essential to choosing the right watchdog tier.

### 1.1 Permission Prompt Awaiting Input (Pattern P1)

**Trigger**: A tool call requires a permission decision but no human is available to answer.

In interactive mode, Claude Code presents a prompt and waits for keyboard input. In headless
mode with `--dangerously-skip-permissions`, all prompts are auto-accepted. However, two edge
cases remain:

- `--permission-mode acceptEdits` auto-accepts edits but still blocks on other operations
  (e.g., shell commands outside the approved list).
- When a sub-agent (spawned via the Task/Agent tool) attempts to request an edit approval from
  its parent, and no `--dangerously-skip-permissions` is active, it can stall indefinitely
  (GitHub Issue #7091: "If sub-agent asks user to approve an edit, it gets stuck indefinitely").

**Mitigation**: Always use `--dangerously-skip-permissions` with a scoped `--allowedTools`
allowlist when running in headless mode. The combination keeps permission bypass in effect
while restricting the tool surface.

### 1.2 Mid-Turn API Stream Freeze (Pattern P2)

**Trigger**: The Anthropic API SSE/streaming connection stalls mid-delivery — tokens stop
arriving without a connection error being raised.

The Claude Code process remains alive in `epoll_wait` (kernel polling state), displaying
spinner messages ("Accomplishing…", "Ruminating…") indefinitely. No error is logged. SIGTERM
has no effect on this state; only SIGKILL terminates the process (GitHub Issue #25979).

Two sub-patterns have been observed:
- The thinking block streams partially ("thought for 3s"), then the stream silently stops.
- A tool completes execution but its result is never delivered back to the conversation state
  (the JSONL log shows a `tool_use` ID without a matching `tool_result` ID).

**Root cause**: Missing read timeout on the HTTP streaming client inside Claude Code itself.
This is an upstream bug, not a user configuration error.

**Mitigation**: Inactivity watchdog — trigger on absence of stream-json events for a
configurable window (see Section 5).

### 1.3 Post-Result Stream Not Closed (Pattern P3)

**Trigger**: Claude Code successfully completes a task and emits the final
`{"type":"result","subtype":"success",...}` event, but the process never exits.

Stdout remains open, stdout EOF is never signaled, and any consumer using `for await`
iteration or `stream.on('end')` waits forever. Process accumulates in memory; requires SIGKILL.
This was reported as a 5+ minute hang after an 18-minute successful run (GitHub Issue #25629,
closed as duplicate of #21099; also GitHub Issue #3187).

**Root cause**: Pending timers or MCP server child processes inside the Node.js runtime keep
the event loop alive after the result event is emitted.

**Mitigation**: Post-result timeout — after detecting `{"type":"result",...}` in the stream,
start a short grace timer (30–60 s) and kill the process if it has not exited.

### 1.4 Agent Hang Mid-Task with No Output (Pattern P4)

**Trigger**: The agent stops producing output and making tool calls, but no error occurs and no
result event is emitted. The session appears frozen. This is the core scenario described in
GitHub Issue #28482 ("Agent hangs indefinitely mid-task — no recovery path without Esc").

Affected scenarios include:
- Multi-turn agentic loops (Task/Agent tool subagents)
- After tool calls return large results (possible context window pressure)
- During complex reasoning before tool calls

There is no programmatic equivalent of pressing Esc in headless mode. The issue is OPEN
as of 2026-03-02, with multiple duplicate reports (#19230, #17620, #25979, #28494, #28512,
#29642, #29900, #30014).

**Mitigation**: Inactivity watchdog — identical to P2 mitigation. The distinction between
"thinking deeply" and "truly hung" cannot be made with certainty; the watchdog must accept
the risk of killing a legitimate long-running reasoning step.

### 1.5 Spontaneous SIGTERM from Internal Timeout (Pattern P5)

**Trigger**: The `claude -p` process itself terminates with exit code 143 (SIGTERM) after 3–10
minutes of active work, with no user-level signal sent.

Reported specifically for Max subscription accounts (GitHub Issue #29642). The hypothesized
causes include:
- OAuth token refresh timing (Max subscription tokens may have refresh windows)
- An internal AbortController firing on transient errors
- An undocumented internal session timeout

This pattern affects WebSearch/WebFetch-heavy tasks faster than file operations. Tasks shorter
than ~2 minutes typically complete successfully.

**Mitigation**: Detect exit code 143 as distinct from user-initiated kills; treat as retriable
transient failure rather than logic error.

### 1.6 Stdin Not Closed, Process Waits for Input (Pattern P6)

**Trigger**: The spawning process does not close the child's stdin pipe. Claude Code waits for
stdin input because the pipe is still open, preventing it from producing any output.

This is a classic POSIX pipe behavior: if the parent's write end of the stdin pipe is not
closed, the child process treats stdin as potentially having more input and may block.
Documented for Java `ProcessBuilder` users (GitHub Issue #7497).

**Mitigation**: Always close the child process's stdin immediately after spawning, or use
`stdin=asyncio.subprocess.DEVNULL` in Python's asyncio subprocess API.

---

## 2. Timeout Flags: What Exists and What Does Not

### 2.1 No Native `--timeout` Flag

The official Claude Code CLI reference (as of March 2026) does **not** include a `--timeout`
flag. Process-level timeout must be enforced externally by the conductor — either via the
`timeout(1)` POSIX command wrapper or via `asyncio.wait_for()` on the subprocess.

### 2.2 Available Flags That Act as Indirect Timeout Backstops

| Flag | What it does | When it triggers | Exit behavior |
|------|-------------|-----------------|---------------|
| `--max-turns N` | Caps the number of agentic iterations | When turn N+1 would begin | Emits `{"type":"result","subtype":"error_max_turns"}`, exits with non-zero code |
| `--max-budget-usd N` | Caps total API spend | When cost would exceed N | Emits `{"type":"result","subtype":"error_max_budget_usd"}`, exits with non-zero code |

**Important**: Neither flag guards against the hang patterns in Section 1. They only enforce
logical limits on healthy sessions. A hung session (P2, P4) does not consume turns or API budget
and will not be terminated by these flags.

### 2.3 Bash Tool Timeout Variables

Two environment variables control per-Bash-call timeouts **within** an agent session (not the
session itself):

```json
{
  "env": {
    "BASH_DEFAULT_TIMEOUT_MS": "120000",
    "BASH_MAX_TIMEOUT_MS": "600000"
  }
}
```

These are set in `~/.claude/settings.json` and affect how long individual Bash tool calls run
before being killed. They have no effect on session-level hangs.

### 2.4 `timeout` Command as an External Wrapper

The POSIX `timeout` command can wrap `claude -p`:

```bash
timeout 1800 claude -p "..." --output-format stream-json ...
```

When the timeout fires, `timeout` sends SIGTERM to the process group; if the process does not
exit within the default grace period, it sends SIGKILL. Exit code is 124 when killed by timeout.

This is a simple but blunt approach — it does not distinguish between inactivity hangs and
legitimately slow operations. The conductor's asyncio watchdog (Section 6) provides more nuance.

---

## 3. Exit Code Taxonomy

| Exit Code | Meaning | Cause | Conductor Action |
|-----------|---------|-------|-----------------|
| 0 | Success | Task completed normally | Mark issue done, merge PR |
| 1 | Error | API error, auth failure, or execution error | Inspect `subtype` field; may retry |
| 124 | Killed by `timeout` command | Wall-clock total timeout | Clean up, retry or abandon |
| 130 | SIGINT (Ctrl+C) | User interrupt or conductor signal | Clean up |
| 143 | SIGTERM | User kill, conductor kill, or internal session timeout (P5) | Clean up, retry |
| 137 | SIGKILL | Forced kill (OOM or conductor escalation) | Clean up |

**Note**: When the process exits cleanly, the result message in stream-json provides additional
information via the `subtype` field:

| `subtype` value | Meaning | Exit code |
|----------------|---------|-----------|
| `"success"` | Task completed successfully | 0 |
| `"error_max_turns"` | `--max-turns` limit reached | 1 |
| `"error_max_budget_usd"` | `--max-budget-usd` limit reached | 1 |
| `"error_during_execution"` | Unhandled error during execution | 1 |

---

## 4. Healthy vs. Hung Process Signals

### 4.1 Stream-JSON Event Frequency as a Health Indicator

When running with `--output-format stream-json`, a healthy `claude -p` session emits a
continuous stream of NDJSON events. The event types (per the Agent SDK documentation) are:

- `system` — session initialization (emitted once at start, contains `session_id` and `tools`)
- `assistant` — model response messages (may contain `text` or `tool_use` blocks)
- `user` — tool results being fed back to the model
- `result` — final result event (session end)

With `--include-partial-messages` added, intermediate streaming events appear:
`message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`,
`message_delta`, `message_stop`.

**A healthy session**: emits at least one event per minute during active tool calls; emits
multiple events per second during text generation.

**A hung session (P2 or P4)**: emits no new events for several minutes despite the process
being alive. The stream is silent but not closed.

**A post-result hang (P3)**: has already emitted `{"type":"result",...}` but has not exited.

### 4.2 Process Alive vs. No Output Distinction

The conductor can distinguish:
1. **Process not started**: `returncode` is None, no events received
2. **Process running, producing events**: `returncode` is None, events arriving
3. **Process running, no events** (hung): `returncode` is None, no events for > inactivity threshold
4. **Process exited**: `returncode` is not None

Pattern 3 is the hang state. Distinguishing "thinking" from "hung" is not possible with
certainty — there is no heartbeat or ping event emitted during long reasoning pauses. The
inactivity threshold must be set conservatively enough to allow for genuine long thinking,
but short enough to recover in reasonable time.

### 4.3 Legitimate Slow Operations

Some operations are expected to be slow and should not trigger inactivity timeout:
- Web search: typically 15–45 seconds
- Bash commands with `sleep` or long network calls: bounded by `BASH_MAX_TIMEOUT_MS`
- Large file reads or writes: several seconds

During a Bash tool call, the stream-json output includes a `content_block_start` event with
`"type": "tool_use"` followed by eventual `content_block_stop` and `user` (tool_result) events.
If a `tool_use` has started but no `user` event has arrived for > N minutes, the Bash command
may be running. The inactivity timeout should be set longer than `BASH_MAX_TIMEOUT_MS` to
avoid false positives.

---

## 5. Watchdog Design: Three-Tier Timeout Model

### 5.1 Tier Overview

The conductor should implement three independent timeout tiers, each addressing a different hang
pattern:

| Tier | Timeout Value | Triggers On | Action |
|------|--------------|-------------|--------|
| T1: Inactivity | 300 s (5 min) | No new stream-json events for N seconds | SIGTERM → wait 15 s → SIGKILL |
| T2: Total session | 3600 s (60 min) | Wall-clock time since subprocess start | SIGTERM → wait 15 s → SIGKILL |
| T3: Post-result | 60 s | After `{"type":"result",...}` received | SIGKILL (process already done) |

**Rationale for values**:
- T1 (5 min): Conservative enough for long web searches (~45 s) and Bash commands, but will
  catch P2 and P4 hangs before they accumulate. The `claude_code_agent_farm` community project
  uses adaptive idle detection with a ~2-hour stale-lock threshold; the 5-minute value is tuned
  for the conductor's short-lived worker model.
- T2 (60 min): Matches `CONDUCTOR_TIMEOUT_SECONDS = 1800` from the configuration research
  (doc #4), but halved to reflect observed SIGTERM patterns (P5) which kill sessions at 3–10 min.
  Operators should tune this via `CONDUCTOR_TIMEOUT_SECONDS`.
- T3 (60 s): After the final result event, the process has no more work to do. The 60 s grace
  allows MCP server cleanup. If not exited by then, it is the P3 hang — kill immediately.

### 5.2 Timeout Tier Precedence

Tiers are evaluated independently, not sequentially. Whichever fires first wins. After any
tier fires, the other tiers are cancelled (no double-kill).

### 5.3 Inactivity Threshold Calibration

The inactivity threshold should be set to:

```
T1 = max(BASH_MAX_TIMEOUT_MS / 1000, 180) + 120
```

The default `BASH_MAX_TIMEOUT_MS` is 600 s (10 min), which would make T1 = 720 s. For
conductor's scoped workers (no long Bash commands expected), 300 s is appropriate. Operators
running workers that execute long compilation or test suites should increase T1.

---

## 6. asyncio Implementation Pattern

### 6.1 Core Watchdog Coroutine

The conductor spawns each worker as an `asyncio.subprocess.Process` with `stdout=PIPE` and
`stdin=DEVNULL` (Pattern P6 fix). A separate watchdog coroutine monitors the event stream and
enforces the three timeout tiers.

```python
import asyncio
import json
import signal
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

INACTIVITY_TIMEOUT_S = 300    # T1: 5 minutes
TOTAL_SESSION_TIMEOUT_S = 3600 # T2: 60 minutes (override with CONDUCTOR_TIMEOUT_SECONDS)
POST_RESULT_TIMEOUT_S = 60    # T3: 60 seconds after result event


async def run_worker_with_watchdog(
    cmd: list[str],
    cwd: Path,
    env: dict,
    issue_number: int,
    branch_name: str,
) -> tuple[int, str]:
    """
    Spawn a claude -p worker subprocess and enforce three-tier timeout watchdog.

    Returns (exit_code, subtype) where subtype is the stream-json result subtype
    or a synthetic value ("watchdog_inactivity", "watchdog_total", "watchdog_post_result",
    "sigterm_internal") if a watchdog tier fired or an unexpected signal was received.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.DEVNULL,   # Fix for Pattern P6
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    result_subtype: Optional[str] = None
    last_event_time = time.monotonic()
    start_time = time.monotonic()
    result_received_at: Optional[float] = None

    async def read_events():
        nonlocal last_event_time, result_subtype, result_received_at
        async for line in proc.stdout:
            last_event_time = time.monotonic()
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue
            try:
                event = json.loads(line_str)
                if event.get("type") == "result":
                    result_subtype = event.get("subtype", "unknown")
                    result_received_at = time.monotonic()
                    logger.info(
                        "worker %d result received: subtype=%s cost=%.4f turns=%d",
                        issue_number,
                        result_subtype,
                        event.get("total_cost_usd", 0),
                        event.get("num_turns", 0),
                    )
            except json.JSONDecodeError:
                pass  # Non-JSON lines (stderr mixed in, or verbose text) — ignore

    async def watchdog():
        nonlocal result_received_at
        while proc.returncode is None:
            await asyncio.sleep(10)  # Poll every 10 seconds

            elapsed = time.monotonic() - start_time
            idle = time.monotonic() - last_event_time

            # T3: post-result hang
            if result_received_at is not None:
                post_result_idle = time.monotonic() - result_received_at
                if post_result_idle > POST_RESULT_TIMEOUT_S:
                    logger.warning(
                        "worker %d: post-result hang (%.0fs since result event), killing",
                        issue_number, post_result_idle,
                    )
                    await _graceful_kill(proc, issue_number, reason="watchdog_post_result")
                    return

            # T1: inactivity timeout
            if idle > INACTIVITY_TIMEOUT_S:
                logger.warning(
                    "worker %d: inactivity timeout (%.0fs idle), killing",
                    issue_number, idle,
                )
                await _graceful_kill(proc, issue_number, reason="watchdog_inactivity")
                return

            # T2: total session timeout
            if elapsed > TOTAL_SESSION_TIMEOUT_S:
                logger.warning(
                    "worker %d: total session timeout (%.0fs elapsed), killing",
                    issue_number, elapsed,
                )
                await _graceful_kill(proc, issue_number, reason="watchdog_total")
                return

    # Run event reader and watchdog concurrently
    try:
        await asyncio.gather(
            read_events(),
            watchdog(),
        )
    except Exception as exc:
        logger.error("worker %d: unexpected error in monitor: %s", issue_number, exc)
        await _graceful_kill(proc, issue_number, reason="error")

    await proc.wait()

    exit_code = proc.returncode
    # Detect spontaneous SIGTERM (Pattern P5)
    if exit_code == 143 and result_subtype is None:
        result_subtype = "sigterm_internal"
        logger.warning(
            "worker %d: died with SIGTERM (exit 143) before result — possibly internal timeout",
            issue_number,
        )

    return exit_code, result_subtype or "unknown"


async def _graceful_kill(proc, issue_number: int, reason: str) -> None:
    """Two-phase shutdown: SIGTERM, then SIGKILL after grace period."""
    logger.info("worker %d: sending SIGTERM (%s)", issue_number, reason)
    try:
        proc.terminate()  # SIGTERM
    except ProcessLookupError:
        return  # Already exited

    try:
        await asyncio.wait_for(proc.wait(), timeout=15)
    except asyncio.TimeoutError:
        logger.warning("worker %d: SIGTERM ignored after 15s, sending SIGKILL", issue_number)
        try:
            proc.kill()  # SIGKILL
        except ProcessLookupError:
            pass  # Already exited
        await proc.wait()
```

### 6.2 Reading stderr for Diagnostics

A separate coroutine should drain stderr concurrently to avoid pipe buffer filling (which can
itself cause the process to hang):

```python
async def drain_stderr(proc, issue_number: int) -> str:
    """Drain stderr and return its contents for logging."""
    chunks = []
    async for line in proc.stderr:
        chunks.append(line.decode("utf-8", errors="replace"))
    return "".join(chunks)
```

The `read_events()` and `drain_stderr()` coroutines should both be included in the `asyncio.gather()` call.

### 6.3 Timeout Override via `CONDUCTOR_TIMEOUT_SECONDS`

```python
import os

TOTAL_SESSION_TIMEOUT_S = int(os.environ.get("CONDUCTOR_TIMEOUT_SECONDS", "3600"))
INACTIVITY_TIMEOUT_S = int(os.environ.get("CONDUCTOR_INACTIVITY_TIMEOUT_S", "300"))
POST_RESULT_TIMEOUT_S = int(os.environ.get("CONDUCTOR_POST_RESULT_TIMEOUT_S", "60"))
```

---

## 7. Graceful Shutdown and Orphaned State Cleanup

### 7.1 What Gets Left Behind When a Worker Is Killed

When the conductor kills a hung worker, three resources may be in an inconsistent state:

| Resource | State after kill | Cleanup action |
|----------|-----------------|----------------|
| `in-progress` GitHub label | Still applied to the issue | Remove label; remove assignee |
| Git worktree | Exists at `.claude/worktrees/<branch>/` | `git worktree remove --force <path>` |
| Remote branch | May or may not have commits | Leave for now; conductor decides whether to delete or retry |

### 7.2 Cleanup Procedure

The conductor must run cleanup **regardless of why the worker exited** (watchdog, SIGTERM, error,
or success). The cleanup sequence after a kill:

```python
import subprocess

async def cleanup_after_worker(
    issue_number: int,
    branch_name: str,
    worktree_path: Path,
    repo: str,
    exit_code: int,
    subtype: str,
) -> None:
    """Clean up orphaned state after a worker exits or is killed."""

    # 1. Remove in-progress label and assignee from GitHub issue
    try:
        subprocess.run(
            ["gh", "issue", "edit", str(issue_number),
             "--remove-label", "in-progress",
             "--remove-assignee", "@me",
             "--repo", repo],
            check=True, timeout=30,
        )
        logger.info("issue %d: removed in-progress label", issue_number)
    except subprocess.CalledProcessError as e:
        logger.error("issue %d: failed to remove in-progress label: %s", issue_number, e)

    # 2. Remove worktree (force-remove even if dirty)
    if worktree_path.exists():
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                check=True, timeout=30,
            )
            logger.info("removed worktree at %s", worktree_path)
        except subprocess.CalledProcessError as e:
            logger.error("failed to remove worktree %s: %s", worktree_path, e)
            # Fallback: manual removal
            import shutil
            shutil.rmtree(worktree_path, ignore_errors=True)
            subprocess.run(["git", "worktree", "prune"], timeout=10)

    # 3. Decide retry vs. abandon based on exit reason
    if subtype in ("watchdog_inactivity", "watchdog_total", "watchdog_post_result"):
        logger.warning(
            "issue %d: killed by watchdog (subtype=%s), marking for retry",
            issue_number, subtype,
        )
        # Add retry logic here: re-claim issue, recreate worktree, re-dispatch
    elif subtype == "sigterm_internal":
        logger.warning(
            "issue %d: internal SIGTERM (P5), may retry",
            issue_number,
        )
    elif subtype == "error_max_turns":
        logger.error(
            "issue %d: hit --max-turns limit — task too large or loop detected",
            issue_number,
        )
    elif subtype == "error_during_execution":
        logger.error("issue %d: error during execution — inspect session log", issue_number)
    elif subtype == "success":
        pass  # Cleanup is post-success housekeeping, not error recovery
```

### 7.3 Orphaned Worktree Detection on Startup

On conductor startup, it must check for orphaned worktrees from previous sessions (per the
global Orchestrator-Dispatch Protocol):

```bash
git worktree list
gh issue list --state open --label in-progress --repo <owner/repo>
```

A worktree is orphaned if its corresponding `claude -p` process is no longer running. The
conductor should cross-reference running PIDs against known worker worktree paths using a
PID file written at dispatch time.

---

## 8. `--permission-prompt-tool` MCP Approach

The CLI reference documents a `--permission-prompt-tool` flag that allows a custom MCP tool
to handle permission prompts in non-interactive mode:

```bash
claude -p --permission-prompt-tool mcp_auth_tool "query"
```

**Current status**: The implementation gap is documented in GitHub Issue #1175. There is no
minimal, working, documented example for implementing the required MCP server. Community
attempts have failed due to lack of documentation on the expected MCP tool schema.

**Assessment**: This flag does not solve the hang problem for breadmin-conductor at this time.
The recommended approach remains `--dangerously-skip-permissions` with a scoped `--allowedTools`
allowlist (as documented in the security threat model, doc #6).

If Anthropic provides documentation for `--permission-prompt-tool`, it could eliminate
permission-prompt hangs (Pattern P1) for tools outside the allowlist rather than requiring
`--dangerously-skip-permissions`. This warrants a follow-up research issue.

---

## 9. Cross-References

**01-agent-tool-in-p-mode.md**: Established the subprocess spawning pattern as the recommended
architecture. The watchdog in this document operates at the subprocess level — it monitors the
`claude -p` process spawned via Bash tool or directly by the conductor's Python runner. The
known issue table in that document cites #28482 as "High for headless" — this document
provides the resolution strategy for that item.

**04-configuration.md** (doc #4): Defines `CONDUCTOR_TIMEOUT_SECONDS = 1800` as the default
timeout. This document refines that into three tiers and introduces two new env vars
(`CONDUCTOR_INACTIVITY_TIMEOUT_S`, `CONDUCTOR_POST_RESULT_TIMEOUT_S`). The `build_agent_env()`
function in doc #4 should be extended to pass `BASH_DEFAULT_TIMEOUT_MS` and
`BASH_MAX_TIMEOUT_MS` to worker subprocesses.

**06-security-threat-model.md** (doc #6): The `--dangerously-skip-permissions` with scoped
`--allowedTools` pattern from that document is the prerequisite for avoiding Pattern P1
permission hangs. The cleanup procedure in Section 7.2 of this document must never expose
credentials and should use the `build_agent_env()` scrubbing pattern.

**#3 (Error handling and recovery)**: The error subtype taxonomy (Section 3) and retry logic
(Section 7.2) feed directly into the error handling and failure recovery design for that issue.

---

## 10. Follow-Up Research Recommendations

### R-HANG-A: Empirical Inactivity Threshold Calibration

**Question**: What is the longest observed silence between stream-json events in a healthy
`claude -p` session performing typical conductor tasks (file edits, tests, git operations)?
Is 5 minutes conservative enough, or are there legitimate reasoning pauses that exceed it?

**Why this matters**: Setting T1 too low will kill legitimate sessions; too high will delay
recovery. An empirical baseline from real conductor task runs would allow tuning. The current
300 s value is conservative but untested.

**Suggested approach**: Run 10+ representative conductor tasks with instrumented event logging;
plot intra-event gap distributions; identify 99th-percentile gap; set T1 = p99 + 120 s.

### R-HANG-B: `--permission-prompt-tool` MCP Schema Documentation

**Question**: What is the exact JSON schema expected by `--permission-prompt-tool`? Can a
minimal MCP server be written that auto-approves or auto-denies specific tool permission
requests without `--dangerously-skip-permissions`?

**Why this matters**: `--permission-prompt-tool` would allow granular permission handling
per-tool rather than blanket bypass, reducing the security surface (T4 in the threat model)
while eliminating Pattern P1 hangs. GitHub Issue #1175 shows this is an open gap.

### R-HANG-C: Internal Session Timeout in `claude -p` (Pattern P5 Root Cause)

**Question**: Is there an undocumented internal session timeout in `claude -p`? Is exit code
143 from Pattern P5 caused by an OAuth token refresh, an internal watchdog, or a rate limit
that manifests as SIGTERM? Can it be prevented by structuring tasks to emit regular output?

**Why this matters**: If P5 has a configurable trigger, the conductor can avoid it. If it is
a rate-limit manifestation, the retry strategy should include a backoff. GitHub Issue #29642
is OPEN with no official answer as of 2026-03-02.

### R-HANG-D: `asyncio.TimeoutError` vs. `asyncio.wait_for` on Long Bash Tool Calls

**Question**: When a `claude -p` subprocess is running a long Bash command (e.g., a 10-minute
test suite), do stream-json events stop entirely during the command, or does Claude Code emit
periodic heartbeat events? Does `BASH_MAX_TIMEOUT_MS` apply even when the command is legitimately
long?

**Why this matters**: If no events are emitted during Bash execution, the inactivity watchdog
must be set longer than the maximum expected Bash command duration. If heartbeat events exist,
the inactivity watchdog can be set shorter.

### R-HANG-E: Reliable Stream-EOF Detection After Result Event

**Question**: When does stdout EOF reliably close after the result event? Is there a version of
Claude Code where this is fixed? Can the conductor force EOF by sending SIGTERM immediately after
detecting the result event, without waiting for the T3 timeout?

**Why this matters**: The post-result hang (Pattern P3) adds up to 60 seconds of unnecessary
delay per worker. If the process exits cleanly after SIGTERM (sent after result event), the
T3 timer can be eliminated. GitHub Issue #25629 was closed as duplicate; the root issue
(#21099) should be checked for resolution status.

---

## 11. Sources

- [Run Claude Code programmatically — Claude Code Docs](https://code.claude.com/docs/en/headless)
- [CLI reference — Claude Code Docs](https://code.claude.com/docs/en/cli-reference)
- [Stream responses in real-time — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/streaming-output)
- [GitHub Issue #28482: Agent hangs indefinitely mid-task — no recovery path without Esc](https://github.com/anthropics/claude-code/issues/28482)
- [GitHub Issue #25629: Claude Code CLI hangs indefinitely after sending result event in stream-json mode](https://github.com/anthropics/claude-code/issues/25629)
- [GitHub Issue #25979: Claude Code hangs indefinitely when API streaming connection stalls](https://github.com/anthropics/claude-code/issues/25979)
- [GitHub Issue #29642: Headless single-agent sessions die with SIGTERM after 3-10 minutes](https://github.com/anthropics/claude-code/issues/29642)
- [GitHub Issue #7497: Process Hangs Indefinitely When Reading InputStream from Claude Code Headless Execution](https://github.com/anthropics/claude-code/issues/7497)
- [GitHub Issue #7091: If sub-agent asks user to approve an edit, it gets stuck indefinitely](https://github.com/anthropics/claude-code/issues/7091)
- [GitHub Issue #1175: --permission-prompt-tool needs minimal, working example and documentation](https://github.com/anthropics/claude-code/issues/1175)
- [GitHub Issue #5615: Complete Claude Code Timeout Configuration Guide (BASH_DEFAULT_TIMEOUT_MS)](https://github.com/anthropics/claude-code/issues/5615)
- [ClaudeCode.Types — ClaudeCode v0.21.0 (Elixir SDK, documents result subtypes)](https://hexdocs.pm/claude_code/ClaudeCode.Types.html)
- [ClaudeCodeSDK.Message — claude_code_sdk v0.2.2 (result subtype taxonomy)](https://hexdocs.pm/claude_code_sdk/ClaudeCodeSDK.Message.html)
- [Headless Mode and CI/CD Cheatsheet — SFEIR Institute](https://institute.sfeir.com/en/claude-code/claude-code-headless-mode-and-ci-cd/cheatsheet/)
- [Headless Mode and CI/CD FAQ — SFEIR Institute](https://institute.sfeir.com/en/claude-code/claude-code-headless-mode-and-ci-cd/faq/)
- [CI/CD and Headless Mode with Claude Code — Angelo Lima](https://angelo-lima.fr/en/claude-code-cicd-headless-en/)
- [Asyncio Subprocess in Python — Super Fast Python](https://superfastpython.com/asyncio-subprocess/)
- [Python asyncio subprocess documentation — Python 3 Docs](https://docs.python.org/3/library/asyncio-subprocess.html)
- [Running Python code in a subprocess with a time limit — Simon Willison's TILs](https://til.simonwillison.net/python/subprocess-time-limit)
- [GitHub: Dicklesworthstone/claude_code_agent_farm (adaptive idle timeout watchdog example)](https://github.com/Dicklesworthstone/claude_code_agent_farm)
- [What is --output-format in Claude Code — ClaudeLog](https://claudelog.com/faqs/what-is-output-format-in-claude-code/)
- [What is --max-turns in Claude Code — ClaudeLog](https://claudelog.com/faqs/what-is-max-turns-in-claude-code/)
- [Claude Code Down in 2026: Complete Status Guide (120 second default timeout reference)](https://www.adventureppc.com/blog/claude-code-down-in-2026-complete-status-guide-error-fixes-what-to-do-during-outages/)
