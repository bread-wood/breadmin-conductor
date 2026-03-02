"""Per-session JSONL logging and cost ledger.

Three independent append-only JSONL write streams:

  {log_dir}/cost.jsonl               -- permanent cost ledger; file-locked for concurrent writes
  {log_dir}/sessions/<session>.jsonl -- per-session execution log (single-writer, no lock)
  {log_dir}/conductor/<run>.jsonl    -- conductor orchestration log (single-writer, no lock)

Public API
----------
LogContext          -- frozen dataclass carrying per-invocation identity fields
log_cost            -- append one entry to cost.jsonl
log_session_event   -- append one structured event to sessions/<session-id>.jsonl
log_conductor_event -- append one structured event to conductor/<run-id>.jsonl
read_cost_ledger    -- read and optionally filter cost.jsonl; used by ``composer cost``

Conductor event types (11 total)
---------------------------------
  stage_start        -- worker begins processing a milestone
  issue_claimed      -- GitHub issue assigned and labelled in-progress
  agent_dispatched   -- subprocess launched; session_id known from system/init event
  agent_completed    -- subprocess exited; cost entry written
  pr_created         -- gh pr create succeeded
  ci_checked         -- gh pr checks polled
  pr_merged          -- gh pr merge --squash succeeded
  backoff_enter      -- conductor enters rate-limit or budget backoff wait
  backoff_exit       -- conductor resumes after backoff
  human_escalate     -- conductor cannot proceed without human intervention
  checkpoint_write   -- conductor checkpoint file persisted
  stage_complete     -- all milestone issues resolved; pipeline stage exits

Platform note: ``fcntl.flock`` is used for the cost ledger. This module targets POSIX
(macOS, Linux) only. Windows is not supported.
"""

from __future__ import annotations

import fcntl
import json
import os
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: All valid conductor event_type values. Callers should pass one of these strings
#: to log_conductor_event to keep the log parseable.
CONDUCTOR_EVENT_TYPES: frozenset[str] = frozenset(
    {
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
)

# Pricing constants for subscription-mode cost estimation (March 2026, claude-sonnet-4-6)
_SONNET_PRICES: dict[str, float] = {
    "input_per_mtok": 3.00,
    "output_per_mtok": 15.00,
    "cache_write_per_mtok": 3.75,  # 125% of input price
    "cache_read_per_mtok": 0.30,  # 10% of input price
}

#: claude-opus-4-6 is approximately 5x the cost of claude-sonnet-4-6.
_OPUS_MULTIPLIER: float = 5.0


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogContext:
    """Per-invocation identity fields threaded through logger calls.

    Constructed by runner.py after the system/init event is received from
    the stream-json subprocess output.

    Attributes:
        session_id:   UUID from stream-json system/init event; matches filename
                      under sessions/.
        run_id:       UUID stored in the conductor checkpoint at run start;
                      matches filename under conductor/.
        repo:         Repository in ``owner/repo`` format.
        stage:        One of ``research``, ``implementation``, ``probe``,
                      ``orchestrator``.
        issue_number: GitHub issue number; ``None`` for probes and orchestrator
                      sessions.
    """

    session_id: str
    run_id: str
    repo: str
    stage: str
    issue_number: int | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _estimate_cost_usd(usage: dict, model: str) -> float | None:
    """Estimate total cost in USD from token counts.

    Uses Sonnet 4.6 pricing as the baseline. Applies a 5x multiplier for
    Opus models. Returns ``None`` when the model family is unrecognised.

    Args:
        usage: Dict with token count keys (input_tokens, output_tokens,
               cache_read_input_tokens, cache_creation_input_tokens).
        model: The Claude model ID string (e.g. ``claude-sonnet-4-6``).

    Returns:
        Estimated cost in USD, rounded to 6 decimal places, or ``None`` if
        the model multiplier cannot be determined.
    """
    model_lower = model.lower()
    if "opus" in model_lower:
        mult = _OPUS_MULTIPLIER
    elif "sonnet" in model_lower or "haiku" in model_lower:
        mult = 1.0
    else:
        return None

    p = _SONNET_PRICES
    return round(
        (
            usage.get("input_tokens", 0) / 1_000_000 * p["input_per_mtok"]
            + usage.get("output_tokens", 0) / 1_000_000 * p["output_per_mtok"]
            + usage.get("cache_creation_input_tokens", 0) / 1_000_000 * p["cache_write_per_mtok"]
            + usage.get("cache_read_input_tokens", 0) / 1_000_000 * p["cache_read_per_mtok"]
        )
        * mult,
        6,
    )


def _append_locked(path: Path, record: dict) -> None:
    """Append a JSON-serialised record to *path* under an exclusive flock.

    Creates parent directories as needed. Opens the file in append mode,
    acquires an exclusive advisory lock via ``fcntl.flock``, writes
    ``json.dumps(record) + "\\n"``, flushes, then releases the lock.

    Args:
        path:   Absolute or relative path to the target JSONL file.
        record: A JSON-serialisable dict; serialised with ``json.dumps``.

    Raises:
        OSError: On disk or permission errors.
    """
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.write(json.dumps(record) + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _append_unlocked(path: Path, record: dict) -> None:
    """Append a JSON-serialised record to *path* without locking.

    Single-writer logs (session and conductor) do not require locking.
    Creates parent directories as needed.

    Args:
        path:   Absolute or relative path to the target JSONL file.
        record: A JSON-serialisable dict; serialised with ``json.dumps``.

    Raises:
        OSError: On disk or permission errors.
    """
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------


def log_cost(
    result_event: dict,
    context: LogContext,
    *,
    log_dir: Path,
    model: str,
    auth_mode: str,
) -> None:
    """Append one entry to the cost ledger at ``{log_dir}/cost.jsonl``.

    Extracts billing fields from *result_event*, computes estimated cost when
    running under a subscription account, and appends a locked JSON line to the
    shared cost ledger.

    Args:
        result_event: The ``result`` stream-json event dict emitted by
                      ``claude -p --output-format stream-json``.
        context:      Per-invocation identity (session_id, run_id, repo, stage,
                      issue_number).
        log_dir:      Resolved log root directory (e.g. ``Config.log_dir``).
        model:        Claude model ID string (e.g. ``claude-sonnet-4-6``).
        auth_mode:    ``"api_key"`` or ``"subscription"``.

    Raises:
        OSError: On disk or permission errors.
    """
    usage: dict = result_event.get("usage", {})
    server_tool_use: dict = usage.get("server_tool_use", {})

    is_error: bool = bool(result_event.get("is_error", False))
    error_subtype: str | None = result_event.get("subtype") if is_error else None

    total_cost_usd: float | None = result_event.get("total_cost_usd")

    # For subscription mode, or when the API didn't return a cost, estimate it.
    if auth_mode == "subscription" or not total_cost_usd:
        estimated = _estimate_cost_usd(usage, model)
        if estimated is not None:
            total_cost_usd = estimated
        elif auth_mode == "subscription":
            # Unrecognised model — record null and warn
            total_cost_usd = None
            warnings.warn(
                f"log_cost: unrecognised model {model!r}; total_cost_usd will be null",
                stacklevel=2,
            )

    entry: dict = {
        "timestamp": _now_iso(),
        "session_id": context.session_id,
        "run_id": context.run_id,
        "repo": context.repo,
        "stage": context.stage,
        "issue_number": context.issue_number,
        "model": model,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
        "num_turns": result_event.get("num_turns", 0),
        "duration_ms": result_event.get("duration_ms", 0),
        "is_error": is_error,
        "error_subtype": error_subtype,
        "total_cost_usd": total_cost_usd,
        "auth_mode": auth_mode,
        "web_search_requests": server_tool_use.get("web_search_requests", 0),
    }

    _append_locked(log_dir / "cost.jsonl", entry)


def log_session_event(
    session_id: str,
    event_type: str,
    phase: str,
    payload: dict,
    *,
    log_dir: Path,
    run_id: str,
) -> None:
    """Append one structured event to ``{log_dir}/sessions/{session_id}.jsonl``.

    Per-session logs are single-writer; no file locking is applied.

    Args:
        session_id: UUID identifying the claude -p session; used as the filename.
        event_type: Discriminator string (e.g. ``dispatch_start``,
                    ``stream_event``, ``result``, ``error``, ``retry``).
        phase:      Conductor phase at the time of the event (e.g. ``dispatch``,
                    ``ci_check``, ``merge``).
        payload:    Event-type-specific dict; must be JSON-serialisable.
        log_dir:    Resolved log root directory.
        run_id:     UUID of the conductor run that owns this session.

    Raises:
        OSError: On disk or permission errors.
    """
    record: dict = {
        "timestamp": _now_iso(),
        "session_id": session_id,
        "run_id": run_id,
        "event_type": event_type,
        "phase": phase,
        "payload": payload,
    }
    path = log_dir / "sessions" / f"{session_id}.jsonl"
    _append_unlocked(path, record)


def log_conductor_event(
    run_id: str,
    phase: str,
    event_type: str,
    payload: dict,
    *,
    log_dir: Path,
) -> None:
    """Append one structured event to ``{log_dir}/conductor/{run_id}.jsonl``.

    The conductor log is single-writer (only the orchestrator process writes to
    its own run file); no file locking is applied.

    Valid event types (see ``CONDUCTOR_EVENT_TYPES``):
      stage_start, issue_claimed, agent_dispatched, agent_completed,
      pr_created, ci_checked, pr_merged, backoff_enter, backoff_exit,
      human_escalate, checkpoint_write, stage_complete.

    Args:
        run_id:     UUID of the conductor run; used as the filename.
        phase:      Current pipeline phase (e.g. ``init``, ``claim``,
                    ``dispatch``, ``ci_check``, ``merge``, ``backoff``).
        event_type: Discriminator string; one of ``CONDUCTOR_EVENT_TYPES``.
        payload:    Event-type-specific dict; must be JSON-serialisable.
        log_dir:    Resolved log root directory.

    Raises:
        OSError: On disk or permission errors.
    """
    record: dict = {
        "timestamp": _now_iso(),
        "run_id": run_id,
        "phase": phase,
        "event_type": event_type,
        "payload": payload,
    }
    path = log_dir / "conductor" / f"{run_id}.jsonl"
    _append_unlocked(path, record)


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------


def read_cost_ledger(
    log_dir: Path,
    repo: str | None = None,
    stage: str | None = None,
) -> list[dict]:
    """Read and optionally filter the cost ledger at ``{log_dir}/cost.jsonl``.

    Skips blank lines and lines that fail ``json.loads`` silently. Returns
    entries in file order (oldest-first).

    Args:
        log_dir: Resolved log root directory.
        repo:    If provided, return only entries where ``entry["repo"] == repo``.
        stage:   If provided, return only entries where ``entry["stage"] == stage``.

    Returns:
        List of parsed dicts, filtered by *repo* and *stage* when given.

    Raises:
        OSError: On permission errors (file-not-found is swallowed; returns ``[]``).
    """
    cost_path = log_dir / "cost.jsonl"
    try:
        text = cost_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []

    entries: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if repo is not None and entry.get("repo") != repo:
            continue
        if stage is not None and entry.get("stage") != stage:
            continue
        entries.append(entry)

    return entries
