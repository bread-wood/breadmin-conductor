"""Headless claude -p runner.

Invokes `claude -p` as a subprocess with stream-json output, drains stdout
and stderr concurrently via select(), parses stream-json events line by line,
and returns a structured RunResult to the caller.

No orchestration decisions are made here — all classification is surfaced in
RunResult fields for the worker loop to act on.
"""

from __future__ import annotations

import json
import select
import shlex
import signal
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Tool set constants
# ---------------------------------------------------------------------------

TOOLS_RESEARCH: list[str] = ["gh", "bash", "read", "web_search"]
TOOLS_DESIGN: list[str] = ["gh"]
TOOLS_IMPL_AGENT: list[str] = ["gh", "bash", "read", "edit", "write", "Glob", "Grep"]

# ---------------------------------------------------------------------------
# RunResult dataclass
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """Parsed result of one ``claude -p`` invocation.

    Every field is populated from the parsed stream-json output. When the
    subprocess exits before emitting a ``result`` event, fields are synthesised
    from the exit code and stderr.
    """

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run(
    prompt: str,
    allowed_tools: list[str],
    env: dict[str, str],
    *,
    dry_run: bool = False,
    max_turns: int = 100,
    append_system_prompt_file: Path | None = None,
    mcp_config: Path | None = None,
    verbose: bool = True,
    timeout_seconds: float | None = None,
) -> RunResult:
    """Invoke ``claude -p`` as a subprocess and return a structured result.

    Parameters
    ----------
    prompt:
        The user-turn prompt passed to claude via ``-p``.
    allowed_tools:
        List of tool names permitted for this invocation. Passed as
        ``--allowedTools "<comma-separated>"``. Must not be empty.
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
        If provided, pass ``--append-system-prompt <path>``. Use for stable
        worker-type instructions that benefit from being cache-friendly.
        Mutually exclusive with inline system prompt injection in the prompt
        string.
    mcp_config:
        If provided, pass ``--mcp-config <path>``. If None, runner passes
        ``--strict-mcp-config --mcp-config '{}'`` to suppress all MCP servers.
    verbose:
        If True (default), print tool invocations and brief output to stderr
        as they occur so the operator can see progress in real time.
    timeout_seconds:
        Wall-clock deadline for the entire subprocess. If the process is still
        running after this many seconds it receives SIGTERM; if it does not exit
        within 5 seconds it is SIGKILLed. The returned RunResult will have
        ``subtype="timeout"`` and ``is_error=True``. None means no limit.

    Returns
    -------
    RunResult
        Fully populated result. is_error=False only when subtype="success".

    Raises
    ------
    ValueError
        If ``allowed_tools`` is empty.
    """
    if not allowed_tools:
        raise ValueError(
            "allowed_tools must not be empty; running with no tools is almost certainly a bug."
        )

    cmd = _assemble_command(
        prompt=prompt,
        allowed_tools=allowed_tools,
        max_turns=max_turns,
        append_system_prompt_file=append_system_prompt_file,
        mcp_config=mcp_config,
    )

    if dry_run:
        print("[dry-run] " + " ".join(shlex.quote(a) for a in cmd))
        return RunResult(
            is_error=False,
            subtype="success",
            error_code=None,
            exit_code=0,
            total_cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            raw_result_event=None,
            stderr="",
            overage_detected=False,
        )

    if verbose:
        print("  (first response may take a few minutes…)", file=sys.stderr, flush=True)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=False,  # binary mode; we decode manually
    )

    result_event, all_events, stderr_text, overage_detected, timed_out = _parse_stream(
        proc, verbose=verbose, timeout_seconds=timeout_seconds
    )
    proc.wait()
    exit_code = proc.returncode

    if timed_out:
        return _synthesise_result(124, all_events, stderr_text, overage_detected)

    if result_event is not None:
        return _build_result_from_event(
            result_event=result_event,
            all_events=all_events,
            exit_code=exit_code,
            stderr_text=stderr_text,
            overage_detected=overage_detected,
        )

    # No result event — synthesise from exit code
    return _synthesise_result(
        exit_code=exit_code,
        all_events=all_events,
        stderr_text=stderr_text,
        overage_detected=overage_detected,
    )


# ---------------------------------------------------------------------------
# Command assembly
# ---------------------------------------------------------------------------


def _assemble_command(
    prompt: str,
    allowed_tools: list[str],
    max_turns: int,
    append_system_prompt_file: Path | None,
    mcp_config: Path | None,
) -> list[str]:
    """Assemble the ``claude`` command list."""
    cmd: list[str] = ["claude", "-p", prompt]

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

    # System prompt override: use append-system-prompt if file provided
    if append_system_prompt_file is not None:
        cmd += ["--append-system-prompt", str(append_system_prompt_file)]

    # MCP suppression or explicit config
    if mcp_config is None:
        cmd += ["--strict-mcp-config", "--mcp-config", "{}"]
    else:
        cmd += ["--mcp-config", str(mcp_config)]

    return cmd


# ---------------------------------------------------------------------------
# Stream-JSON parsing
# ---------------------------------------------------------------------------


def _print_progress(event: dict) -> None:
    """Print a one-line terminal summary of a stream-json event to stderr.

    Shows tool invocations (assistant → tool_use blocks) and brief tool
    output (user → tool_result blocks) so the operator can see what Claude
    is doing without waiting for the full session to complete.
    """
    event_type = event.get("type")

    if event_type == "assistant":
        message = event.get("message")
        if not isinstance(message, dict):
            return
        for block in message.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name", "?")
            inp = block.get("input") or {}
            if name == "Bash":
                detail = (inp.get("command") or "").split("\n")[0][:120]
            elif name in ("Read", "Write", "Edit", "NotebookEdit"):
                detail = inp.get("file_path", "")
            elif name == "Glob":
                detail = inp.get("pattern", "")
            elif name == "Grep":
                detail = inp.get("pattern", "")
            else:
                detail = str(inp)[:80]
            print(f"  → {name}: {detail}", file=sys.stderr, flush=True)

    elif event_type == "user":
        message = event.get("message")
        if not isinstance(message, dict):
            return
        for block in message.get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            raw = block.get("content") or ""
            if isinstance(raw, list):
                text = next(
                    (
                        b.get("text", "")
                        for b in raw
                        if isinstance(b, dict) and b.get("type") == "text"
                    ),
                    "",
                )
            else:
                text = str(raw)
            lines = text.strip().splitlines()
            n = len(lines)
            if n == 0:
                print("    (empty)", file=sys.stderr, flush=True)
            elif n == 1:
                print(f"    {lines[0][:120]}", file=sys.stderr, flush=True)
            else:
                print(f"    {lines[0][:100]}  (+{n - 1} lines)", file=sys.stderr, flush=True)


def _parse_stream(
    proc: subprocess.Popen,
    verbose: bool = True,
    timeout_seconds: float | None = None,
) -> tuple[dict | None, list[dict], str, bool, bool]:
    """Read stream-json events from proc.stdout until EOF or timeout.

    Uses ``select()`` to multiplex stdout and stderr concurrently, preventing
    pipe deadlock when the subprocess writes large volumes to either fd.

    Returns
    -------
    result_event : dict | None
        The parsed ``result`` event, or None if not received.
    all_events : list[dict]
        All events received (for session logging / error classification).
    stderr_text : str
        Full stderr captured.
    overage_detected : bool
        True if any rate_limit_event with isUsingOverage=True was observed.
    timed_out : bool
        True if the subprocess was killed due to exceeding ``timeout_seconds``.
    """
    result_event: dict | None = None
    all_events: list[dict] = []
    overage_detected: bool = False
    read_buffer: str = ""
    timed_out: bool = False

    stderr_chunks: list[bytes] = []

    stdout_done = False
    stderr_done = False

    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None

    while not (stdout_done and stderr_done):
        readable_fds = []
        if not stdout_done:
            readable_fds.append(proc.stdout)
        if not stderr_done:
            readable_fds.append(proc.stderr)

        if not readable_fds:
            break

        try:
            read_ready, _, _ = select.select(readable_fds, [], [], 1.0)
        except (ValueError, OSError):
            # fd was closed unexpectedly
            break

        for readable in read_ready:
            if readable is proc.stdout:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    stdout_done = True
                else:
                    read_buffer += chunk.decode("utf-8", errors="replace")

                    # Split on newlines; last segment may be partial
                    lines = read_buffer.split("\n")
                    read_buffer = lines[-1]  # keep incomplete tail in buffer

                    for line in lines[:-1]:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            warnings.warn(
                                f"stream-json decode failed: {line[:200]!r}",
                                stacklevel=2,
                            )
                            continue

                        all_events.append(event)
                        if verbose:
                            _print_progress(event)
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

        # If select() returned nothing and fds are still open, loop back
        # (timeout=1.0 prevents busy-spinning)

        # Deadline check — after each select cycle
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            try:
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            break

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
    return result_event, all_events, stderr_text, overage_detected, timed_out


# ---------------------------------------------------------------------------
# RunResult construction
# ---------------------------------------------------------------------------


def _build_result_from_event(
    result_event: dict,
    all_events: list[dict],
    exit_code: int,
    stderr_text: str,
    overage_detected: bool,
) -> RunResult:
    """Build a RunResult from a parsed ``result`` stream-json event."""
    usage = result_event.get("usage") or {}

    is_error: bool = bool(result_event.get("is_error", False))
    subtype: str | None = result_event.get("subtype")

    # Derive error_code only when the session ended in error
    error_code: str | None = None
    if is_error:
        error_code = _classify_error_code(result_event, all_events, stderr_text)

    return RunResult(
        is_error=is_error,
        subtype=subtype,
        error_code=error_code,
        exit_code=exit_code,
        total_cost_usd=result_event.get("total_cost_usd"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cache_read_input_tokens=usage.get("cache_read_input_tokens"),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
        raw_result_event=result_event,
        stderr=stderr_text,
        overage_detected=overage_detected,
    )


def _synthesise_result(
    exit_code: int,
    all_events: list[dict],
    stderr_text: str,
    overage_detected: bool,
) -> RunResult:
    """Synthesise a RunResult when no ``result`` event was received."""
    if exit_code == 0:
        # Exit 0 with no result event — known bug in some Claude Code versions.
        subtype = "missing_result_event"
    elif exit_code == 143:
        # SIGTERM
        subtype = "sigterm_internal"
    elif exit_code == 130:
        # SIGINT — operator interrupt
        subtype = "user_interrupt"
    elif exit_code == 137:
        # SIGKILL — OOM or forced kill
        subtype = "sigkill"
    elif exit_code == 124:
        # timeout(1) wall-clock kill
        subtype = "timeout"
    else:
        subtype = "unknown"

    return RunResult(
        is_error=True,
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


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _classify_error_code(
    result_event: dict | None,
    all_events: list[dict],
    stderr: str,
) -> str | None:
    """Derive secondary error_code from result text, assistant events, and stderr.

    Returns None for non-error runs or when no specific code can be determined.

    Detection priority (per LLD §6.4):
    1. Check all assistant events for event["error"] == "billing_error" (HTTP 402 path)
    2. Check result["result"] text for known patterns
    3. Fall back to stderr text
    """
    # 1. Billing error — check assistant events for error field (HTTP 402 path)
    for evt in all_events:
        if evt.get("type") == "assistant" and evt.get("error") == "billing_error":
            return "billing_error"

    if result_event is None:
        return _classify_error_code_from_stderr(stderr)

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


def _classify_error_code_from_stderr(stderr: str) -> str | None:
    """Derive error_code from stderr text when no result event is available."""
    stderr_lower = stderr.lower()
    if "invalid api key" in stderr_lower or "authentication" in stderr_lower:
        return "auth_failure"
    return None


# ---------------------------------------------------------------------------
# Internal logging shim
# ---------------------------------------------------------------------------


def _log_parse_warning(msg: str) -> None:
    """Emit a parse warning. Prints to stderr until logger.py is wired in."""
    print(f"[runner] WARNING: {msg}", file=sys.stderr)
