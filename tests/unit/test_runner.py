"""Unit tests for src/composer/runner.py."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from composer.runner import (
    TOOLS_DESIGN,
    TOOLS_IMPL_AGENT,
    TOOLS_RESEARCH,
    RunResult,
    _assemble_command,
    _classify_error_code,
    _synthesise_result,
    run,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ENV: dict[str, str] = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/root",
    "ANTHROPIC_API_KEY": "sk-ant-test",
}


def _make_event(**fields: Any) -> dict:
    """Build a stream-json event dict."""
    return dict(fields)


def _make_result_event(
    is_error: bool = False,
    subtype: str = "success",
    result_text: str = "",
    total_cost_usd: float = 0.01,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read_input_tokens: int = 10,
    cache_creation_input_tokens: int = 5,
) -> dict:
    """Build a well-formed ``result`` stream-json event."""
    return {
        "type": "result",
        "is_error": is_error,
        "subtype": subtype,
        "result": result_text,
        "total_cost_usd": total_cost_usd,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
        },
    }


def _ndjson(*events: dict) -> bytes:
    """Encode a sequence of dicts as newline-delimited JSON bytes."""
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def _make_mock_proc(
    stdout_bytes: bytes,
    stderr_bytes: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Construct a mock subprocess.Popen compatible with _parse_stream()."""
    proc = MagicMock(spec=subprocess.Popen)
    proc.returncode = returncode

    # stdout: binary stream that yields chunks then EOF
    stdout_buf = io.BytesIO(stdout_bytes)
    proc.stdout = MagicMock()
    proc.stdout.read = stdout_buf.read
    proc.stdout.fileno.return_value = 100  # fake fd

    # stderr: binary stream
    stderr_buf = io.BytesIO(stderr_bytes)
    proc.stderr = MagicMock()
    proc.stderr.read = stderr_buf.read
    proc.stderr.fileno.return_value = 101  # fake fd

    return proc


def _make_select_side_effect(proc: MagicMock) -> Any:
    """
    Return a side_effect callable for select.select().

    On each call it returns a ready list that includes both stdout and stderr
    until the respective BytesIO is exhausted, then marks them done.
    """

    # We track via the BytesIO position in the mocks — simplest is to just
    # return both fds ready every call; read() will return b"" at EOF.
    def _select(rlist, wlist, xlist, timeout=None):  # noqa: ANN001
        ready = [fd for fd in rlist]
        return ready, [], []

    return _select


# ---------------------------------------------------------------------------
# Tool set constants
# ---------------------------------------------------------------------------


def test_tools_research_contains_expected_tools() -> None:
    assert set(TOOLS_RESEARCH) == {"gh", "bash", "read", "web_search"}


def test_tools_design_contains_gh_only() -> None:
    assert TOOLS_DESIGN == ["gh"]


def test_tools_impl_agent_contains_expected_tools() -> None:
    assert set(TOOLS_IMPL_AGENT) == {"gh", "bash", "read", "edit", "write", "Glob", "Grep"}


# ---------------------------------------------------------------------------
# _assemble_command
# ---------------------------------------------------------------------------


def test_assemble_command_baseline() -> None:
    """Baseline command contains required flags."""
    cmd = _assemble_command(
        prompt="do the thing",
        allowed_tools=["bash", "read"],
        max_turns=50,
        append_system_prompt_file=None,
        mcp_config=None,
    )
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "do the thing" in cmd
    assert "--output-format" in cmd
    assert "stream-json" in cmd
    assert "--allowedTools" in cmd
    assert "bash,read" in cmd
    assert "--max-turns" in cmd
    assert "50" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--disable-slash-commands" in cmd
    assert "--no-session-persistence" in cmd


def test_assemble_command_mcp_suppression_default() -> None:
    """When mcp_config is None, --strict-mcp-config and --mcp-config '{}' are injected."""
    cmd = _assemble_command(
        prompt="x",
        allowed_tools=["bash"],
        max_turns=10,
        append_system_prompt_file=None,
        mcp_config=None,
    )
    assert "--strict-mcp-config" in cmd
    assert "--mcp-config" in cmd
    idx = cmd.index("--mcp-config")
    assert cmd[idx + 1] == "{}"


def test_assemble_command_mcp_config_path() -> None:
    """When mcp_config is provided, --strict-mcp-config is not injected."""
    mcp_path = Path("/tmp/mcp.json")
    cmd = _assemble_command(
        prompt="x",
        allowed_tools=["bash"],
        max_turns=10,
        append_system_prompt_file=None,
        mcp_config=mcp_path,
    )
    assert "--strict-mcp-config" not in cmd
    assert "--mcp-config" in cmd
    idx = cmd.index("--mcp-config")
    assert cmd[idx + 1] == str(mcp_path)


def test_assemble_command_append_system_prompt_file() -> None:
    """append_system_prompt_file injects --append-system-prompt <path>."""
    sp_file = Path("/tmp/system-prompt.txt")
    cmd = _assemble_command(
        prompt="x",
        allowed_tools=["bash"],
        max_turns=10,
        append_system_prompt_file=sp_file,
        mcp_config=None,
    )
    assert "--append-system-prompt" in cmd
    idx = cmd.index("--append-system-prompt")
    assert cmd[idx + 1] == str(sp_file)


def test_assemble_command_no_append_system_prompt_when_none() -> None:
    """When append_system_prompt_file is None, the flag is absent."""
    cmd = _assemble_command(
        prompt="x",
        allowed_tools=["bash"],
        max_turns=10,
        append_system_prompt_file=None,
        mcp_config=None,
    )
    assert "--append-system-prompt" not in cmd


# ---------------------------------------------------------------------------
# run() — dry-run
# ---------------------------------------------------------------------------


def test_dry_run_returns_success_run_result(capsys: pytest.CaptureFixture) -> None:
    """dry_run=True returns a mock success RunResult without spawning a process."""
    result = run(
        prompt="hello",
        allowed_tools=["bash"],
        env=MINIMAL_ENV,
        dry_run=True,
    )

    assert isinstance(result, RunResult)
    assert result.is_error is False
    assert result.subtype == "success"
    assert result.error_code is None
    assert result.exit_code == 0
    assert result.total_cost_usd == 0.0
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.cache_read_input_tokens == 0
    assert result.cache_creation_input_tokens == 0
    assert result.raw_result_event is None
    assert result.stderr == ""
    assert result.overage_detected is False


def test_dry_run_prints_command(capsys: pytest.CaptureFixture) -> None:
    """dry_run=True prints the assembled command to stdout."""
    run(
        prompt="hello world",
        allowed_tools=["bash", "read"],
        env=MINIMAL_ENV,
        dry_run=True,
    )
    captured = capsys.readouterr()
    assert captured.out.startswith("[dry-run] ")
    assert "claude" in captured.out
    assert "-p" in captured.out
    assert "hello world" in captured.out


def test_dry_run_does_not_spawn_subprocess() -> None:
    """dry_run=True never calls subprocess.Popen."""
    with patch("subprocess.Popen") as mock_popen:
        run(
            prompt="test",
            allowed_tools=["bash"],
            env=MINIMAL_ENV,
            dry_run=True,
        )
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# run() — empty allowed_tools raises ValueError
# ---------------------------------------------------------------------------


def test_run_raises_value_error_when_allowed_tools_is_empty() -> None:
    """run() with an empty allowed_tools list raises ValueError."""
    with pytest.raises(ValueError, match="allowed_tools must not be empty"):
        run(
            prompt="x",
            allowed_tools=[],
            env=MINIMAL_ENV,
        )


# ---------------------------------------------------------------------------
# run() — stream-json parsing with mock result event
# ---------------------------------------------------------------------------


def test_run_success_parses_result_event_fields() -> None:
    """run() correctly extracts all fields from a success result event."""
    result_evt = _make_result_event(
        is_error=False,
        subtype="success",
        total_cost_usd=0.05,
        input_tokens=200,
        output_tokens=80,
        cache_read_input_tokens=20,
        cache_creation_input_tokens=10,
    )
    stdout_bytes = _ndjson(result_evt)

    mock_proc = _make_mock_proc(stdout_bytes=stdout_bytes, returncode=0)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(
            prompt="test",
            allowed_tools=["bash"],
            env=MINIMAL_ENV,
        )

    assert result.is_error is False
    assert result.subtype == "success"
    assert result.error_code is None
    assert result.exit_code == 0
    assert result.total_cost_usd == 0.05
    assert result.input_tokens == 200
    assert result.output_tokens == 80
    assert result.cache_read_input_tokens == 20
    assert result.cache_creation_input_tokens == 10
    assert result.raw_result_event == result_evt
    assert result.stderr == ""
    assert result.overage_detected is False


def test_run_captures_stderr() -> None:
    """run() captures stderr into RunResult.stderr."""
    result_evt = _make_result_event()
    stdout_bytes = _ndjson(result_evt)
    stderr_bytes = b"some warning from claude\n"

    mock_proc = _make_mock_proc(
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        returncode=0,
    )

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(
            prompt="test",
            allowed_tools=["bash"],
            env=MINIMAL_ENV,
        )

    assert result.stderr == "some warning from claude\n"


# ---------------------------------------------------------------------------
# run() — crash case (no result event)
# ---------------------------------------------------------------------------


def test_crash_no_result_event_exit_1_synthesised() -> None:
    """When no result event is emitted and exit code is 1, synthesise 'unknown' error."""
    stdout_bytes = b""  # no events at all
    mock_proc = _make_mock_proc(stdout_bytes=stdout_bytes, returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(
            prompt="test",
            allowed_tools=["bash"],
            env=MINIMAL_ENV,
        )

    assert result.is_error is True
    assert result.subtype == "unknown"
    assert result.raw_result_event is None
    assert result.exit_code == 1
    assert result.total_cost_usd is None
    assert result.input_tokens is None


def test_crash_exit_0_no_result_event_synthesised() -> None:
    """Exit 0 with no result event → 'missing_result_event' synthesised."""
    stdout_bytes = b""
    mock_proc = _make_mock_proc(stdout_bytes=stdout_bytes, returncode=0)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(
            prompt="test",
            allowed_tools=["bash"],
            env=MINIMAL_ENV,
        )

    assert result.is_error is True
    assert result.subtype == "missing_result_event"


def test_crash_sigterm_143_synthesised() -> None:
    """Exit code 143 (SIGTERM) → 'sigterm_internal'."""
    mock_proc = _make_mock_proc(stdout_bytes=b"", returncode=143)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(
            prompt="test",
            allowed_tools=["bash"],
            env=MINIMAL_ENV,
        )

    assert result.is_error is True
    assert result.subtype == "sigterm_internal"


def test_crash_sigint_130_synthesised() -> None:
    """Exit code 130 (SIGINT) → 'user_interrupt'."""
    mock_proc = _make_mock_proc(stdout_bytes=b"", returncode=130)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(
            prompt="test",
            allowed_tools=["bash"],
            env=MINIMAL_ENV,
        )

    assert result.is_error is True
    assert result.subtype == "user_interrupt"


def test_crash_sigkill_137_synthesised() -> None:
    """Exit code 137 (SIGKILL) → 'sigkill'."""
    mock_proc = _make_mock_proc(stdout_bytes=b"", returncode=137)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(
            prompt="test",
            allowed_tools=["bash"],
            env=MINIMAL_ENV,
        )

    assert result.is_error is True
    assert result.subtype == "sigkill"


def test_crash_timeout_124_synthesised() -> None:
    """Exit code 124 (timeout(1)) → 'timeout'."""
    mock_proc = _make_mock_proc(stdout_bytes=b"", returncode=124)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(
            prompt="test",
            allowed_tools=["bash"],
            env=MINIMAL_ENV,
        )

    assert result.is_error is True
    assert result.subtype == "timeout"


# ---------------------------------------------------------------------------
# Error subtype classification
# ---------------------------------------------------------------------------


def test_subtype_error_max_turns_is_error() -> None:
    """result.subtype='error_max_turns' → is_error=True in RunResult."""
    result_evt = _make_result_event(
        is_error=True,
        subtype="error_max_turns",
        result_text="Max turns exceeded",
    )
    mock_proc = _make_mock_proc(stdout_bytes=_ndjson(result_evt), returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.is_error is True
    assert result.subtype == "error_max_turns"


def test_subtype_error_max_budget_usd_is_error() -> None:
    """result.subtype='error_max_budget_usd' → is_error=True."""
    result_evt = _make_result_event(
        is_error=True,
        subtype="error_max_budget_usd",
    )
    mock_proc = _make_mock_proc(stdout_bytes=_ndjson(result_evt), returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.is_error is True
    assert result.subtype == "error_max_budget_usd"


def test_subtype_error_during_operation_rate_limit() -> None:
    """error_during_operation with 'Rate limit' in result text → error_code='rate_limit'."""
    result_evt = _make_result_event(
        is_error=True,
        subtype="error_during_operation",
        result_text="API Error: Rate limit reached, please try again later.",
    )
    mock_proc = _make_mock_proc(stdout_bytes=_ndjson(result_evt), returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.is_error is True
    assert result.subtype == "error_during_operation"
    assert result.error_code == "rate_limit"


def test_subtype_error_during_operation_extra_usage_exhausted() -> None:
    """error_during_operation with rate_limit + 'out of extra usage' → 'extra_usage_exhausted'."""
    result_evt = _make_result_event(
        is_error=True,
        subtype="error_during_operation",
        result_text="API Error: Rate limit reached. You have run out of extra usage.",
    )
    mock_proc = _make_mock_proc(stdout_bytes=_ndjson(result_evt), returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.is_error is True
    assert result.error_code == "extra_usage_exhausted"


def test_subtype_error_during_execution_billing_error_from_assistant_event() -> None:
    """402 path: assistant event with error='billing_error' → error_code='billing_error'."""
    assistant_evt = {
        "type": "assistant",
        "error": "billing_error",
        "message": "Billing authorization failed",
    }
    result_evt = _make_result_event(
        is_error=True,
        subtype="error_during_execution",
        result_text="",
    )
    stdout_bytes = _ndjson(assistant_evt, result_evt)
    mock_proc = _make_mock_proc(stdout_bytes=stdout_bytes, returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.is_error is True
    assert result.subtype == "error_during_execution"
    assert result.error_code == "billing_error"


def test_billing_error_takes_priority_over_result_text() -> None:
    """billing_error from assistant event takes priority over result text patterns."""
    assistant_evt = {
        "type": "assistant",
        "error": "billing_error",
    }
    result_evt = _make_result_event(
        is_error=True,
        subtype="error_during_execution",
        result_text="Rate limit reached",  # would normally → rate_limit
    )
    stdout_bytes = _ndjson(assistant_evt, result_evt)
    mock_proc = _make_mock_proc(stdout_bytes=stdout_bytes, returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.error_code == "billing_error"


def test_auth_failure_from_result_text() -> None:
    """'Invalid API key' in result text → error_code='auth_failure'."""
    result_evt = _make_result_event(
        is_error=True,
        subtype="error_during_execution",
        result_text="Invalid API key provided.",
    )
    mock_proc = _make_mock_proc(stdout_bytes=_ndjson(result_evt), returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.error_code == "auth_failure"


def test_model_overloaded_from_result_text() -> None:
    """'overloaded_error' in result text → error_code='model_overloaded'."""
    result_evt = _make_result_event(
        is_error=True,
        subtype="error_during_execution",
        result_text="overloaded_error: the model is currently unavailable.",
    )
    mock_proc = _make_mock_proc(stdout_bytes=_ndjson(result_evt), returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.error_code == "model_overloaded"


def test_context_length_exceeded_from_result_text() -> None:
    """'context_length_exceeded' in result text → error_code='context_length_exceeded'."""
    result_evt = _make_result_event(
        is_error=True,
        subtype="error_during_execution",
        result_text="context_length_exceeded: prompt too long.",
    )
    mock_proc = _make_mock_proc(stdout_bytes=_ndjson(result_evt), returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.error_code == "context_length_exceeded"


def test_content_refused_from_result_text() -> None:
    """'content filtering' in result text → error_code='content_refused'."""
    result_evt = _make_result_event(
        is_error=True,
        subtype="error_during_execution",
        result_text="Blocked by content filtering policy.",
    )
    mock_proc = _make_mock_proc(stdout_bytes=_ndjson(result_evt), returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.error_code == "content_refused"


def test_no_error_code_for_success() -> None:
    """Successful runs have error_code=None."""
    result_evt = _make_result_event(is_error=False, subtype="success")
    mock_proc = _make_mock_proc(stdout_bytes=_ndjson(result_evt), returncode=0)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.error_code is None


# ---------------------------------------------------------------------------
# Partial-line buffer test
# ---------------------------------------------------------------------------


def test_partial_line_buffer_reassembly() -> None:
    """A JSON line split across two reads is correctly reassembled and parsed."""
    result_evt = _make_result_event(is_error=False, subtype="success", total_cost_usd=0.02)
    full_line = json.dumps(result_evt).encode() + b"\n"

    # Split the line into two chunks at an arbitrary mid-point
    split_at = len(full_line) // 2
    chunk1 = full_line[:split_at]
    chunk2 = full_line[split_at:]

    # Set up a mock stdout that returns chunks in sequence then EOF
    read_calls = iter([chunk1, chunk2, b""])

    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.returncode = 0
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.read = lambda n=4096: next(read_calls)
    mock_proc.stdout.fileno.return_value = 100

    stderr_buf = io.BytesIO(b"")
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.read = stderr_buf.read
    mock_proc.stderr.fileno.return_value = 101

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.is_error is False
    assert result.subtype == "success"
    assert result.total_cost_usd == 0.02


# ---------------------------------------------------------------------------
# overage_detected flag
# ---------------------------------------------------------------------------


def test_overage_detected_set_when_rate_limit_event_has_is_using_overage_true() -> None:
    """overage_detected=True when a rate_limit_event with isUsingOverage=True is seen."""
    rate_limit_evt = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "isUsingOverage": True,
            "requestsRemaining": 0,
        },
    }
    result_evt = _make_result_event(is_error=False, subtype="success")
    stdout_bytes = _ndjson(rate_limit_evt, result_evt)
    mock_proc = _make_mock_proc(stdout_bytes=stdout_bytes, returncode=0)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.overage_detected is True


def test_overage_detected_false_when_is_using_overage_false() -> None:
    """overage_detected=False when isUsingOverage=False in the rate_limit_event."""
    rate_limit_evt = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "isUsingOverage": False,
        },
    }
    result_evt = _make_result_event(is_error=False, subtype="success")
    stdout_bytes = _ndjson(rate_limit_evt, result_evt)
    mock_proc = _make_mock_proc(stdout_bytes=stdout_bytes, returncode=0)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.overage_detected is False


def test_overage_detected_false_when_no_rate_limit_event() -> None:
    """overage_detected=False when no rate_limit_event appears in the stream."""
    result_evt = _make_result_event(is_error=False, subtype="success")
    mock_proc = _make_mock_proc(stdout_bytes=_ndjson(result_evt), returncode=0)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.overage_detected is False


def test_overage_detected_true_carried_to_crash_synthesised_result() -> None:
    """overage_detected is True even when the subprocess crashes (no result event)."""
    rate_limit_evt = {
        "type": "rate_limit_event",
        "rate_limit_info": {"isUsingOverage": True},
    }
    stdout_bytes = _ndjson(rate_limit_evt)  # no result event
    mock_proc = _make_mock_proc(stdout_bytes=stdout_bytes, returncode=1)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.is_error is True
    assert result.overage_detected is True


# ---------------------------------------------------------------------------
# _classify_error_code — unit tests for the classification function directly
# ---------------------------------------------------------------------------


def test_classify_error_code_billing_error_from_assistant_events() -> None:
    """Billing error detected from assistant events before result text."""
    events = [{"type": "assistant", "error": "billing_error"}]
    result = _classify_error_code(
        result_event=_make_result_event(is_error=True, result_text="anything"),
        all_events=events,
        stderr="",
    )
    assert result == "billing_error"


def test_classify_error_code_rate_limit_from_result_text() -> None:
    result = _classify_error_code(
        result_event=_make_result_event(is_error=True, result_text="Rate limit reached"),
        all_events=[],
        stderr="",
    )
    assert result == "rate_limit"


def test_classify_error_code_extra_usage_exhausted() -> None:
    result = _classify_error_code(
        result_event=_make_result_event(
            is_error=True,
            result_text="Rate limit reached. You have run out of extra usage.",
        ),
        all_events=[],
        stderr="",
    )
    assert result == "extra_usage_exhausted"


def test_classify_error_code_none_for_no_match() -> None:
    result = _classify_error_code(
        result_event=_make_result_event(is_error=True, result_text="some random error message"),
        all_events=[],
        stderr="",
    )
    assert result is None


def test_classify_error_code_auth_failure_from_stderr() -> None:
    """When result_event is None, auth failure is extracted from stderr."""
    result = _classify_error_code(
        result_event=None,
        all_events=[],
        stderr="Error: Invalid API key provided",
    )
    assert result == "auth_failure"


# ---------------------------------------------------------------------------
# _synthesise_result — direct unit tests
# ---------------------------------------------------------------------------


def test_synthesise_result_exit_0() -> None:
    r = _synthesise_result(exit_code=0, all_events=[], stderr_text="", overage_detected=False)
    assert r.subtype == "missing_result_event"
    assert r.is_error is True


def test_synthesise_result_exit_143() -> None:
    r = _synthesise_result(exit_code=143, all_events=[], stderr_text="", overage_detected=False)
    assert r.subtype == "sigterm_internal"


def test_synthesise_result_exit_130() -> None:
    r = _synthesise_result(exit_code=130, all_events=[], stderr_text="", overage_detected=False)
    assert r.subtype == "user_interrupt"


def test_synthesise_result_exit_137() -> None:
    r = _synthesise_result(exit_code=137, all_events=[], stderr_text="", overage_detected=False)
    assert r.subtype == "sigkill"


def test_synthesise_result_exit_124() -> None:
    r = _synthesise_result(exit_code=124, all_events=[], stderr_text="", overage_detected=False)
    assert r.subtype == "timeout"


def test_synthesise_result_exit_other() -> None:
    r = _synthesise_result(exit_code=2, all_events=[], stderr_text="", overage_detected=False)
    assert r.subtype == "unknown"


def test_synthesise_result_token_fields_are_none() -> None:
    r = _synthesise_result(exit_code=1, all_events=[], stderr_text="", overage_detected=False)
    assert r.total_cost_usd is None
    assert r.input_tokens is None
    assert r.output_tokens is None
    assert r.cache_read_input_tokens is None
    assert r.cache_creation_input_tokens is None
    assert r.raw_result_event is None


# ---------------------------------------------------------------------------
# Multiple non-result events before result event
# ---------------------------------------------------------------------------


def test_run_ignores_non_result_events_gracefully() -> None:
    """run() handles assistant, user, system events without crashing."""
    events = [
        {"type": "system", "subtype": "init", "session_id": "abc123"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "working..."}]}},
        {"type": "user", "message": {"content": []}},
        _make_result_event(is_error=False, subtype="success", total_cost_usd=0.003),
    ]
    mock_proc = _make_mock_proc(stdout_bytes=_ndjson(*events), returncode=0)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.is_error is False
    assert result.total_cost_usd == 0.003


def test_run_handles_corrupted_json_line_gracefully() -> None:
    """A corrupted JSON line in the stream is skipped; valid events still parsed."""
    result_evt = _make_result_event(is_error=False, subtype="success")
    bad_line = b"not-valid-json\n"
    stdout_bytes = bad_line + json.dumps(result_evt).encode() + b"\n"
    mock_proc = _make_mock_proc(stdout_bytes=stdout_bytes, returncode=0)

    with (
        patch("subprocess.Popen", return_value=mock_proc),
        patch("select.select", side_effect=_make_select_side_effect(mock_proc)),
    ):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = run(prompt="test", allowed_tools=["bash"], env=MINIMAL_ENV)

    assert result.is_error is False
    assert result.subtype == "success"
