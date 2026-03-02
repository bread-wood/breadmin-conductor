"""Unit tests for src/composer/logger.py."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from composer.logger import (
    CONDUCTOR_EVENT_TYPES,
    LogContext,
    _append_locked,
    _estimate_cost_usd,
    log_conductor_event,
    log_cost,
    log_session_event,
    read_cost_ledger,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SESSION_ID = "550e8400-e29b-41d4-a716-446655440000"
_RUN_ID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
_REPO = "myorg/myrepo"
_STAGE = "implementation"

_CTX = LogContext(
    session_id=_SESSION_ID,
    run_id=_RUN_ID,
    repo=_REPO,
    stage=_STAGE,
    issue_number=7,
)

_RESULT_EVENT: dict = {
    "subtype": "success",
    "is_error": False,
    "total_cost_usd": 0.8341,
    "num_turns": 28,
    "duration_ms": 187432,
    "usage": {
        "input_tokens": 142500,
        "output_tokens": 18200,
        "cache_read_input_tokens": 118000,
        "cache_creation_input_tokens": 4800,
        "server_tool_use": {"web_search_requests": 0},
    },
}


def _make_result_event(**overrides: object) -> dict:
    """Return a copy of _RESULT_EVENT with optional field overrides."""
    event = dict(_RESULT_EVENT)
    event["usage"] = dict(_RESULT_EVENT["usage"])
    event["usage"]["server_tool_use"] = dict(_RESULT_EVENT["usage"]["server_tool_use"])
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# LogContext
# ---------------------------------------------------------------------------


def test_log_context_is_frozen() -> None:
    """LogContext is a frozen dataclass — mutation raises AttributeError."""
    ctx = LogContext(session_id="s", run_id="r", repo="o/r", stage="research")
    with pytest.raises((AttributeError, TypeError)):
        ctx.session_id = "other"  # type: ignore[misc]


def test_log_context_issue_number_defaults_to_none() -> None:
    """issue_number defaults to None when not supplied."""
    ctx = LogContext(session_id="s", run_id="r", repo="o/r", stage="probe")
    assert ctx.issue_number is None


# ---------------------------------------------------------------------------
# _append_locked — parent directory creation
# ---------------------------------------------------------------------------


def test_append_locked_creates_parent_dirs(tmp_path: Path) -> None:
    """_append_locked creates all missing parent directories before writing."""
    deep_path = tmp_path / "a" / "b" / "c" / "out.jsonl"
    assert not deep_path.parent.exists()

    _append_locked(deep_path, {"key": "value"})

    assert deep_path.exists()
    data = json.loads(deep_path.read_text())
    assert data == {"key": "value"}


def test_append_locked_appends_not_overwrites(tmp_path: Path) -> None:
    """_append_locked appends; each call adds a new line."""
    path = tmp_path / "out.jsonl"
    _append_locked(path, {"n": 1})
    _append_locked(path, {"n": 2})

    lines = [json.loads(ln) for ln in path.read_text().splitlines()]
    assert lines == [{"n": 1}, {"n": 2}]


# ---------------------------------------------------------------------------
# Concurrent appends — cost ledger integrity
# ---------------------------------------------------------------------------


def test_concurrent_appends_do_not_corrupt(tmp_path: Path) -> None:
    """Multiple threads writing to cost.jsonl produce one valid JSON object per line."""
    cost_path = tmp_path / "cost.jsonl"
    n_threads = 20
    n_writes_each = 10

    def worker(thread_id: int) -> None:
        for i in range(n_writes_each):
            _append_locked(cost_path, {"thread": thread_id, "i": i})

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = cost_path.read_text().splitlines()
    assert len(lines) == n_threads * n_writes_each

    for line in lines:
        parsed = json.loads(line)  # must not raise
        assert "thread" in parsed
        assert "i" in parsed


# ---------------------------------------------------------------------------
# log_cost — field mapping
# ---------------------------------------------------------------------------


def test_log_cost_writes_to_cost_jsonl(tmp_path: Path) -> None:
    """log_cost appends one line to cost.jsonl in log_dir."""
    log_cost(
        _RESULT_EVENT,
        _CTX,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    cost_path = tmp_path / "cost.jsonl"
    assert cost_path.exists()
    lines = cost_path.read_text().splitlines()
    assert len(lines) == 1


def test_log_cost_maps_all_fields_correctly(tmp_path: Path) -> None:
    """log_cost produces an entry containing all required cost ledger fields."""
    log_cost(
        _RESULT_EVENT,
        _CTX,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    entry = json.loads((tmp_path / "cost.jsonl").read_text())

    assert entry["session_id"] == _SESSION_ID
    assert entry["run_id"] == _RUN_ID
    assert entry["repo"] == _REPO
    assert entry["stage"] == _STAGE
    assert entry["issue_number"] == 7
    assert entry["model"] == "claude-sonnet-4-6"
    assert entry["input_tokens"] == 142500
    assert entry["output_tokens"] == 18200
    assert entry["cache_read_input_tokens"] == 118000
    assert entry["cache_creation_input_tokens"] == 4800
    assert entry["num_turns"] == 28
    assert entry["duration_ms"] == 187432
    assert entry["is_error"] is False
    assert entry["error_subtype"] is None
    assert entry["total_cost_usd"] == pytest.approx(0.8341)
    assert entry["auth_mode"] == "api_key"
    assert entry["web_search_requests"] == 0
    assert "timestamp" in entry


def test_log_cost_sets_error_subtype_when_is_error_true(tmp_path: Path) -> None:
    """error_subtype is extracted from result_event.subtype when is_error is True."""
    event = _make_result_event(subtype="error_max_turns", is_error=True)
    log_cost(
        event,
        _CTX,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    entry = json.loads((tmp_path / "cost.jsonl").read_text())
    assert entry["is_error"] is True
    assert entry["error_subtype"] == "error_max_turns"


def test_log_cost_error_subtype_null_when_not_error(tmp_path: Path) -> None:
    """error_subtype is null when is_error is False, regardless of subtype field."""
    log_cost(
        _RESULT_EVENT,
        _CTX,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    entry = json.loads((tmp_path / "cost.jsonl").read_text())
    assert entry["error_subtype"] is None


def test_log_cost_web_search_requests_defaults_to_zero(tmp_path: Path) -> None:
    """web_search_requests defaults to 0 when server_tool_use is absent."""
    event = _make_result_event()
    event["usage"] = {
        "input_tokens": 100,
        "output_tokens": 50,
        # no server_tool_use key
    }
    log_cost(
        event,
        _CTX,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    entry = json.loads((tmp_path / "cost.jsonl").read_text())
    assert entry["web_search_requests"] == 0


def test_log_cost_issue_number_null_for_probe(tmp_path: Path) -> None:
    """issue_number is null in the ledger entry when LogContext.issue_number is None."""
    ctx = LogContext(session_id=_SESSION_ID, run_id=_RUN_ID, repo=_REPO, stage="probe")
    log_cost(
        _RESULT_EVENT,
        ctx,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    entry = json.loads((tmp_path / "cost.jsonl").read_text())
    assert entry["issue_number"] is None


# ---------------------------------------------------------------------------
# log_cost — subscription cost estimation
# ---------------------------------------------------------------------------


def test_log_cost_subscription_estimates_cost_when_total_cost_usd_null(
    tmp_path: Path,
) -> None:
    """subscription mode computes estimated cost when total_cost_usd is None."""
    event = _make_result_event(total_cost_usd=None)
    log_cost(
        event,
        _CTX,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="subscription",
    )
    entry = json.loads((tmp_path / "cost.jsonl").read_text())
    # Estimated cost must be a positive float — not null
    assert entry["total_cost_usd"] is not None
    assert entry["total_cost_usd"] > 0


def test_log_cost_subscription_estimates_cost_when_total_cost_usd_zero(
    tmp_path: Path,
) -> None:
    """subscription mode computes estimated cost when total_cost_usd is 0."""
    event = _make_result_event(total_cost_usd=0)
    log_cost(
        event,
        _CTX,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="subscription",
    )
    entry = json.loads((tmp_path / "cost.jsonl").read_text())
    assert entry["total_cost_usd"] is not None
    assert entry["total_cost_usd"] > 0


def test_log_cost_subscription_unrecognised_model_writes_null_cost(
    tmp_path: Path,
) -> None:
    """Unrecognised model in subscription mode results in total_cost_usd=null."""
    event = _make_result_event(total_cost_usd=None)
    with pytest.warns(UserWarning, match="unrecognised model"):
        log_cost(
            event,
            _CTX,
            log_dir=tmp_path,
            model="claude-unknown-9000",
            auth_mode="subscription",
        )
    entry = json.loads((tmp_path / "cost.jsonl").read_text())
    assert entry["total_cost_usd"] is None


# ---------------------------------------------------------------------------
# _estimate_cost_usd
# ---------------------------------------------------------------------------


def test_estimate_cost_usd_sonnet_positive() -> None:
    """Sonnet pricing returns a positive float for non-zero token counts."""
    usage = {
        "input_tokens": 100_000,
        "output_tokens": 10_000,
        "cache_read_input_tokens": 50_000,
        "cache_creation_input_tokens": 5_000,
    }
    result = _estimate_cost_usd(usage, "claude-sonnet-4-6")
    assert result is not None
    assert result > 0


def test_estimate_cost_usd_opus_is_5x_sonnet() -> None:
    """Opus cost is approximately 5x the Sonnet cost for the same usage."""
    usage = {
        "input_tokens": 100_000,
        "output_tokens": 10_000,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    sonnet = _estimate_cost_usd(usage, "claude-sonnet-4-6")
    opus = _estimate_cost_usd(usage, "claude-opus-4-6")
    assert sonnet is not None
    assert opus is not None
    assert opus == pytest.approx(sonnet * 5.0, rel=1e-5)


def test_estimate_cost_usd_unknown_model_returns_none() -> None:
    """Unrecognised model name returns None."""
    usage = {"input_tokens": 1000, "output_tokens": 500}
    result = _estimate_cost_usd(usage, "claude-unknown-9000")
    assert result is None


def test_estimate_cost_usd_zero_tokens_returns_zero() -> None:
    """All-zero token counts produce a cost of 0.0."""
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    result = _estimate_cost_usd(usage, "claude-sonnet-4-6")
    assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Cost ledger round-trip
# ---------------------------------------------------------------------------


def test_cost_ledger_round_trip(tmp_path: Path) -> None:
    """write → read_cost_ledger returns the same entry that was written."""
    log_cost(
        _RESULT_EVENT,
        _CTX,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    entries = read_cost_ledger(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["session_id"] == _SESSION_ID
    assert entry["input_tokens"] == 142500
    assert entry["repo"] == _REPO


def test_cost_ledger_round_trip_multiple_entries(tmp_path: Path) -> None:
    """Multiple log_cost calls produce multiple entries in read order."""
    ctx2 = LogContext(
        session_id="aaaabbbb-0000-1111-2222-333344445555",
        run_id=_RUN_ID,
        repo=_REPO,
        stage="research",
        issue_number=3,
    )
    log_cost(
        _RESULT_EVENT,
        _CTX,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    log_cost(
        _RESULT_EVENT,
        ctx2,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    entries = read_cost_ledger(tmp_path)
    assert len(entries) == 2
    assert entries[0]["stage"] == "implementation"
    assert entries[1]["stage"] == "research"


# ---------------------------------------------------------------------------
# read_cost_ledger — filtering
# ---------------------------------------------------------------------------


def test_read_cost_ledger_filter_by_repo(tmp_path: Path) -> None:
    """read_cost_ledger returns only entries matching the given repo."""
    ctx_other = LogContext(
        session_id="bbbbcccc-0000-1111-2222-333344445555",
        run_id=_RUN_ID,
        repo="otherorg/otherrepo",
        stage="research",
    )
    log_cost(
        _RESULT_EVENT,
        _CTX,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    log_cost(
        _RESULT_EVENT,
        ctx_other,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )

    entries = read_cost_ledger(tmp_path, repo=_REPO)
    assert len(entries) == 1
    assert entries[0]["repo"] == _REPO


def test_read_cost_ledger_filter_by_stage(tmp_path: Path) -> None:
    """read_cost_ledger returns only entries matching the given stage."""
    ctx_research = LogContext(
        session_id="ccccdddd-0000-1111-2222-333344445555",
        run_id=_RUN_ID,
        repo=_REPO,
        stage="research",
    )
    log_cost(
        _RESULT_EVENT,
        _CTX,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )
    log_cost(
        _RESULT_EVENT,
        ctx_research,
        log_dir=tmp_path,
        model="claude-sonnet-4-6",
        auth_mode="api_key",
    )

    entries = read_cost_ledger(tmp_path, stage="research")
    assert len(entries) == 1
    assert entries[0]["stage"] == "research"


def test_read_cost_ledger_filter_by_repo_and_stage(tmp_path: Path) -> None:
    """Combining repo and stage filters returns only the intersection."""
    ctx_a = LogContext(
        session_id="aaaaaaaa-0000-1111-2222-333344445555",
        run_id=_RUN_ID,
        repo=_REPO,
        stage="research",
    )
    ctx_b = LogContext(
        session_id="bbbbbbbb-0000-1111-2222-333344445555",
        run_id=_RUN_ID,
        repo="other/repo",
        stage="research",
    )
    log_cost(_RESULT_EVENT, _CTX, log_dir=tmp_path, model="claude-sonnet-4-6", auth_mode="api_key")
    log_cost(_RESULT_EVENT, ctx_a, log_dir=tmp_path, model="claude-sonnet-4-6", auth_mode="api_key")
    log_cost(_RESULT_EVENT, ctx_b, log_dir=tmp_path, model="claude-sonnet-4-6", auth_mode="api_key")

    entries = read_cost_ledger(tmp_path, repo=_REPO, stage="research")
    assert len(entries) == 1
    assert entries[0]["repo"] == _REPO
    assert entries[0]["stage"] == "research"


def test_read_cost_ledger_returns_empty_when_file_absent(tmp_path: Path) -> None:
    """read_cost_ledger returns [] when cost.jsonl does not exist."""
    entries = read_cost_ledger(tmp_path)
    assert entries == []


def test_read_cost_ledger_skips_blank_lines(tmp_path: Path) -> None:
    """Blank lines in cost.jsonl are silently skipped."""
    cost_path = tmp_path / "cost.jsonl"
    cost_path.write_text('{"a": 1}\n\n{"a": 2}\n', encoding="utf-8")
    entries = read_cost_ledger(tmp_path)
    assert len(entries) == 2


def test_read_cost_ledger_skips_invalid_json_lines(tmp_path: Path) -> None:
    """Corrupt JSON lines in cost.jsonl are silently skipped."""
    cost_path = tmp_path / "cost.jsonl"
    cost_path.write_text('{"a": 1}\nNOT JSON\n{"a": 2}\n', encoding="utf-8")
    entries = read_cost_ledger(tmp_path)
    assert len(entries) == 2


# ---------------------------------------------------------------------------
# log_session_event
# ---------------------------------------------------------------------------


def test_log_session_event_writes_to_correct_path(tmp_path: Path) -> None:
    """log_session_event writes to sessions/<session-id>.jsonl."""
    log_session_event(
        _SESSION_ID,
        event_type="dispatch_start",
        phase="dispatch",
        payload={"issue_number": 7, "branch": "7-feature", "prompt_length_chars": 100},
        log_dir=tmp_path,
        run_id=_RUN_ID,
    )
    expected = tmp_path / "sessions" / f"{_SESSION_ID}.jsonl"
    assert expected.exists()


def test_log_session_event_record_fields(tmp_path: Path) -> None:
    """log_session_event record contains all required common fields."""
    payload = {"issue_number": 7, "branch": "7-feature", "prompt_length_chars": 100}
    log_session_event(
        _SESSION_ID,
        event_type="dispatch_start",
        phase="dispatch",
        payload=payload,
        log_dir=tmp_path,
        run_id=_RUN_ID,
    )
    line = (tmp_path / "sessions" / f"{_SESSION_ID}.jsonl").read_text()
    record = json.loads(line)

    assert record["session_id"] == _SESSION_ID
    assert record["run_id"] == _RUN_ID
    assert record["event_type"] == "dispatch_start"
    assert record["phase"] == "dispatch"
    assert record["payload"] == payload
    assert "timestamp" in record


def test_log_session_event_creates_sessions_dir(tmp_path: Path) -> None:
    """log_session_event creates the sessions/ subdirectory if absent."""
    assert not (tmp_path / "sessions").exists()
    log_session_event(
        _SESSION_ID,
        event_type="result",
        phase="dispatch",
        payload={"subtype": "success"},
        log_dir=tmp_path,
        run_id=_RUN_ID,
    )
    assert (tmp_path / "sessions").is_dir()


def test_log_session_event_appends_multiple_events(tmp_path: Path) -> None:
    """Multiple log_session_event calls produce multiple lines in the same file."""
    for event_type in ("dispatch_start", "stream_event", "result"):
        log_session_event(
            _SESSION_ID,
            event_type=event_type,
            phase="dispatch",
            payload={"type": event_type},
            log_dir=tmp_path,
            run_id=_RUN_ID,
        )
    path = tmp_path / "sessions" / f"{_SESSION_ID}.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event_type"] == "dispatch_start"
    assert json.loads(lines[2])["event_type"] == "result"


# ---------------------------------------------------------------------------
# log_conductor_event
# ---------------------------------------------------------------------------


def test_log_conductor_event_writes_to_correct_path(tmp_path: Path) -> None:
    """log_conductor_event writes to conductor/<run-id>.jsonl."""
    log_conductor_event(
        _RUN_ID,
        phase="init",
        event_type="stage_start",
        payload={"worker_type": "implementation", "milestone": "MVP", "issue_count": 5},
        log_dir=tmp_path,
    )
    expected = tmp_path / "conductor" / f"{_RUN_ID}.jsonl"
    assert expected.exists()


def test_log_conductor_event_record_fields(tmp_path: Path) -> None:
    """log_conductor_event record contains all required common fields."""
    payload = {"worker_type": "implementation", "milestone": "MVP Impl", "issue_count": 3}
    log_conductor_event(
        _RUN_ID,
        phase="init",
        event_type="stage_start",
        payload=payload,
        log_dir=tmp_path,
    )
    line = (tmp_path / "conductor" / f"{_RUN_ID}.jsonl").read_text()
    record = json.loads(line)

    assert record["run_id"] == _RUN_ID
    assert record["phase"] == "init"
    assert record["event_type"] == "stage_start"
    assert record["payload"] == payload
    assert "timestamp" in record


def test_log_conductor_event_creates_conductor_dir(tmp_path: Path) -> None:
    """log_conductor_event creates the conductor/ subdirectory if absent."""
    assert not (tmp_path / "conductor").exists()
    log_conductor_event(
        _RUN_ID,
        phase="merge",
        event_type="pr_merged",
        payload={"pr_number": 42, "issue_number": 7},
        log_dir=tmp_path,
    )
    assert (tmp_path / "conductor").is_dir()


def test_log_conductor_event_appends_multiple_events(tmp_path: Path) -> None:
    """Multiple log_conductor_event calls produce multiple lines in the same file."""
    events = [
        (
            "init",
            "stage_start",
            {"worker_type": "implementation", "milestone": "MVP", "issue_count": 1},
        ),  # noqa: E501
        ("claim", "issue_claimed", {"issue_number": 7, "branch": "7-feature"}),
        ("dispatch", "agent_dispatched", {"issue_number": 7, "session_id": _SESSION_ID}),
    ]
    for phase, event_type, payload in events:
        log_conductor_event(
            _RUN_ID,
            phase=phase,
            event_type=event_type,
            payload=payload,
            log_dir=tmp_path,
        )
    path = tmp_path / "conductor" / f"{_RUN_ID}.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event_type"] == "stage_start"
    assert json.loads(lines[2])["event_type"] == "agent_dispatched"


# ---------------------------------------------------------------------------
# CONDUCTOR_EVENT_TYPES constant
# ---------------------------------------------------------------------------


def test_conductor_event_types_contains_all_11() -> None:
    """CONDUCTOR_EVENT_TYPES contains exactly the 11 documented event types."""
    expected = {
        "stage_start",
        "issue_claimed",
        "agent_dispatched",
        "agent_completed",
        "pr_created",
        "ci_checked",
        "pr_merged",
        "backoff_enter",
        "backoff_exit",
        "human_escalate",
        "checkpoint_write",
        "stage_complete",
    }
    assert CONDUCTOR_EVENT_TYPES == expected
