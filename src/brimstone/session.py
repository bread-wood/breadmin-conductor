"""Checkpoint persistence and state recovery for the orchestrator.

Single source of truth for in-flight orchestrator state across subprocess
boundaries. Owns the checkpoint schema, atomic read/write, and backoff state.

Issue/PR lifecycle tracking is migrating to bead files (src/brimstone/beads.py).
The fields claimed_issues, open_prs, completed_prs, retry_counts, and
dispatch_times are retained here for backward-compat while cli.py transitions;
they will be removed once cli.py no longer writes to them (PR 7a).

The functions record_dispatch(), is_agent_hung(), and classify_orphaned_issue()
have been removed — their logic moves to BeadStore + Deacon (beads.py / cli.py).

No subprocess calls are made here — callers pass in any GitHub/git state
they have already resolved, keeping this module fully testable without
shell access.
"""

from __future__ import annotations

import dataclasses
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Module constant
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 2

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CheckpointError(Exception):
    """Base exception for checkpoint errors."""


class CheckpointVersionError(CheckpointError):
    """Raised when the checkpoint schema_version is newer than SCHEMA_VERSION."""


class CheckpointCorruptError(CheckpointError):
    """Raised when the checkpoint JSON is unparseable."""


# ---------------------------------------------------------------------------
# Checkpoint dataclass
# ---------------------------------------------------------------------------


@dataclass
class Checkpoint:
    """Complete orchestrator state persisted to disk between restarts.

    Note: claimed_issues, open_prs, completed_prs, retry_counts, and
    dispatch_times are legacy fields retained while cli.py transitions to bead
    files. They will be dropped in a future schema version once cli.py no longer
    writes to them.

    All mutable collection fields use ``field(default_factory=...)`` so that
    each instance gets its own independent container — never a shared default.
    """

    schema_version: int
    run_id: str
    session_id: str
    repo: str
    default_branch: str
    milestone: str
    stage: str
    timestamp: str
    # Legacy fields — will be removed once cli.py reads from BeadStore instead
    claimed_issues: dict[str, str] = field(default_factory=dict)
    active_worktrees: list[str] = field(default_factory=list)
    open_prs: dict[str, int] = field(default_factory=dict)
    completed_prs: list[int] = field(default_factory=list)
    rate_limit_backoff_until: str | None = None
    retry_counts: dict[str, int] = field(default_factory=dict)
    last_error: dict | None = None
    dispatch_times: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Read / Write API
# ---------------------------------------------------------------------------


def new(
    repo: str,
    default_branch: str,
    milestone: str,
    stage: str,
) -> Checkpoint:
    """Create a fresh checkpoint.

    Generates a new UUID ``run_id``. All collection fields start empty.
    Does *not* write to disk — the caller must call :func:`save`.

    Args:
        repo:           GitHub repository in ``"owner/repo"`` format.
        default_branch: Default branch name (e.g. ``"main"``).
        milestone:      Active milestone name (e.g. ``"MVP Implementation"``).
        stage:          Pipeline stage — one of ``"research"``, ``"design"``,
                        ``"plan-issues"``, ``"impl"``.

    Returns:
        A :class:`Checkpoint` with all mutable collections empty.
    """
    return Checkpoint(
        schema_version=SCHEMA_VERSION,
        run_id=str(uuid.uuid4()),
        session_id="",
        repo=repo,
        default_branch=default_branch,
        milestone=milestone,
        stage=stage,
        timestamp=datetime.now(UTC).isoformat(),
    )


def load(path: Path) -> Checkpoint | None:
    """Load a checkpoint from disk.

    Args:
        path: Filesystem path to the checkpoint JSON file.

    Returns:
        A :class:`Checkpoint` on success, or ``None`` if the file does not
        exist (normal first-run case).

    Raises:
        CheckpointVersionError: When ``schema_version`` in the file is greater
            than :data:`SCHEMA_VERSION`.  The caller should instruct the user to
            upgrade ``brimstone``.
        CheckpointCorruptError: When the file cannot be parsed as JSON.  The
            caller must prompt the user to inspect the file before deleting it;
            do *not* silently overwrite — it may represent unrecovered
            in-flight work.
    """
    if not path.exists():
        return None

    try:
        raw_text = path.read_text(encoding="utf-8")
        data: dict = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        import sys

        print(
            f"[session] ERROR: checkpoint at {path} failed JSON parse: {exc}",
            file=sys.stderr,
        )
        raise CheckpointCorruptError(
            f"Checkpoint at {path} is corrupt. Delete it and restart."
        ) from exc

    file_version: int = data.get("schema_version", 0)

    if file_version > SCHEMA_VERSION:
        raise CheckpointVersionError(
            f"Checkpoint at {path} has schema_version={file_version}, "
            f"but this installation only understands up to version {SCHEMA_VERSION}. "
            "Upgrade brimstone to continue."
        )

    if file_version < SCHEMA_VERSION:
        data = _migrate(data, from_version=file_version)

    return _dict_to_checkpoint(data)


def save(checkpoint: Checkpoint, path: Path) -> None:
    """Atomically write the checkpoint to disk.

    Updates ``checkpoint.timestamp`` to the current UTC time, serialises to
    JSON (indent=2), writes to a ``.tmp`` sibling file, then calls
    :func:`os.replace` to atomically rename it over *path*.  On POSIX,
    ``os.replace`` is atomic within the same filesystem — no reader will ever
    observe a partial write.

    Parent directories are created as needed.

    Args:
        checkpoint: The :class:`Checkpoint` to persist (mutated: timestamp
                    is updated in-place).
        path:       Target filesystem path for the checkpoint JSON file.

    Raises:
        OSError: On disk-full or permission errors.  The ``.tmp`` file is left
                 on disk in this case; the original checkpoint is unmodified.
    """
    checkpoint.timestamp = datetime.now(UTC).isoformat()

    os.makedirs(path.parent, exist_ok=True)

    tmp_path = path.with_suffix(".tmp")
    payload = json.dumps(dataclasses.asdict(checkpoint), indent=2)
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, path)


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def _migrate(data: dict, *, from_version: int) -> dict:
    """Apply forward migrations from ``from_version`` to :data:`SCHEMA_VERSION`.

    Each migration mutates *data* in-place and returns it.  The returned dict
    will have ``schema_version`` set to :data:`SCHEMA_VERSION`.
    """
    version = from_version

    if version < 1:
        # v0 → v1: add dispatch_times if absent
        data.setdefault("dispatch_times", {})
        version = 1

    if version < 2:
        # v1 → v2: schema_version bump; legacy fields are still present on
        # Checkpoint for now (they will be dropped in a later schema version
        # once cli.py reads from BeadStore exclusively).
        version = 2

    data["schema_version"] = SCHEMA_VERSION
    return data


def _dict_to_checkpoint(data: dict) -> Checkpoint:
    """Construct a :class:`Checkpoint` from a raw (possibly migrated) dict."""
    return Checkpoint(
        schema_version=data.get("schema_version", SCHEMA_VERSION),
        run_id=data.get("run_id", ""),
        session_id=data.get("session_id", ""),
        repo=data.get("repo", ""),
        default_branch=data.get("default_branch", ""),
        milestone=data.get("milestone", ""),
        stage=data.get("stage", ""),
        timestamp=data.get("timestamp", ""),
        claimed_issues=data.get("claimed_issues", {}),
        active_worktrees=data.get("active_worktrees", []),
        open_prs=data.get("open_prs", {}),
        completed_prs=data.get("completed_prs", []),
        rate_limit_backoff_until=data.get("rate_limit_backoff_until"),
        retry_counts=data.get("retry_counts", {}),
        last_error=data.get("last_error"),
        dispatch_times=data.get("dispatch_times", {}),
    )


# ---------------------------------------------------------------------------
# Backoff state
# ---------------------------------------------------------------------------


def set_backoff(
    checkpoint: Checkpoint,
    attempt: int,
    base_seconds: float,
    max_seconds: float,
) -> None:
    """Set the rate-limit backoff deadline using exponential backoff.

    Formula: ``wait = min(base_seconds * 2 ** attempt, max_seconds)``

    The deadline is stored as an ISO 8601 UTC string in
    ``checkpoint.rate_limit_backoff_until``.

    Args:
        checkpoint:   The checkpoint to mutate.
        attempt:      Zero-indexed retry attempt number.
        base_seconds: Base wait duration in seconds.
        max_seconds:  Upper cap on the computed wait duration.
    """
    wait = min(base_seconds * (2**attempt), max_seconds)
    until = datetime.now(UTC) + timedelta(seconds=wait)
    checkpoint.rate_limit_backoff_until = until.isoformat()


def is_backing_off(checkpoint: Checkpoint) -> bool:
    """Return ``True`` if the backoff deadline is in the future.

    Returns ``False`` when ``rate_limit_backoff_until`` is ``None``.
    """
    if checkpoint.rate_limit_backoff_until is None:
        return False
    until = datetime.fromisoformat(checkpoint.rate_limit_backoff_until)
    return datetime.now(UTC) < until


def clear_backoff(checkpoint: Checkpoint) -> None:
    """Clear the backoff deadline after a successful dispatch post-backoff."""
    checkpoint.rate_limit_backoff_until = None
