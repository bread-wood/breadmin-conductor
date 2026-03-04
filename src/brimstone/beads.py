"""Bead files — atomic JSON state for brimstone orchestration.

Replaces checkpoint.json for issue/PR lifecycle tracking. Four bead types:
  - WorkBead:    issue lifecycle (claimed → pr_open → merge_ready → closed)
  - PRBead:      PR + feedback triage state
  - MergeQueue:  sequential merge ordering (replaces inline gh pr merge calls)
  - CampaignBead: multi-milestone campaign progress tracking

Beads are stored under ~/.brimstone/beads/<owner>/<repo>/:
  work/<issue_number>.json
  prs/pr-<pr_number>.json
  merge-queue.json
  campaign.json

All writes use write-to-.tmp + os.replace (atomic on POSIX).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

BEAD_SCHEMA_VERSION: int = 1

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BeadError(Exception):
    """Base exception for bead errors."""


class BeadCorruptError(BeadError):
    """Raised when a bead file cannot be parsed as JSON."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FeedbackItem:
    """A single review comment or CI feedback item triaged by an agent."""

    comment_id: str
    author: str
    is_bot: bool
    triage: str  # "pending" | "fix_now" | "filed_issue" | "skipped"
    filed_issue: int | None = None
    triage_reason: str | None = None


@dataclass
class WorkBead:
    """Issue lifecycle state — one file per issue."""

    v: int
    issue_number: int
    title: str
    milestone: str
    stage: str  # "research" | "design" | "impl"
    module: str
    priority: str  # "P0"–"P4"
    state: str  # "open" | "claimed" | "pr_open" | "merge_ready" | "closed" | "abandoned"
    branch: str
    pr_id: str | None = None  # "pr-187", links to PRBead file
    retry_count: int = 0
    blocked_by: list[int] = field(default_factory=list)  # issue numbers that must close first
    deferred: bool = False  # non-blocking for stage-gate; set at claim time from [DEFERRED] tag
    claimed_at: str | None = None  # ISO UTC
    closed_at: str | None = None


@dataclass
class PRBead:
    """PR + feedback triage state — one file per PR."""

    v: int
    pr_number: int
    issue_number: int
    branch: str
    state: str  # "open"|"ci_running"|"ci_failing"|"reviewing"|"conflict"|"merge_ready"|"merged"
    ci_state: str | None = None
    conflict_state: str | None = None
    fix_attempts: int = 0
    feedback: list[FeedbackItem] = field(default_factory=list)
    created_at: str | None = None
    merged_at: str | None = None


@dataclass
class MergeQueueEntry:
    """A single entry in the merge queue."""

    pr_number: int
    issue_number: int
    branch: str
    enqueued_at: str


@dataclass
class MergeQueue:
    """Sequential merge ordering for PRs that have passed CI + review."""

    v: int
    queue: list[MergeQueueEntry] = field(default_factory=list)
    updated_at: str = ""


@dataclass
class CampaignBead:
    """Multi-milestone campaign progress tracking — one file per repo."""

    v: int = 1
    repo: str = ""
    milestones: list[str] = field(default_factory=list)  # ordered
    current_index: int = 0
    statuses: dict[str, str] = field(default_factory=dict)
    # status values: "pending" | "planning" | "researching" | "designing"
    #                | "scoping" | "implementing" | "shipped"
    updated_at: str = ""


# ---------------------------------------------------------------------------
# BeadStore
# ---------------------------------------------------------------------------


class BeadStore:
    """Read/write interface for bead files.

    All writes are atomic: data is written to a .tmp sibling then os.replace()d
    over the target. On POSIX this guarantees no reader sees a partial write.

    Args:
        beads_dir:        Root directory for this repo's bead files.
        state_repo_path:  Optional path to a cloned ``brimstone-state`` git repo.
                          When set, :meth:`flush` commits and pushes dirty beads.
    """

    def __init__(self, beads_dir: Path, state_repo_path: Path | None = None) -> None:
        self._beads_dir = beads_dir
        self._state_repo_path = state_repo_path
        # Ensure subdirs exist
        (beads_dir / "work").mkdir(parents=True, exist_ok=True)
        (beads_dir / "prs").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _work_path(self, issue_number: int) -> Path:
        return self._beads_dir / "work" / f"{issue_number}.json"

    def _pr_path(self, pr_number: int) -> Path:
        return self._beads_dir / "prs" / f"pr-{pr_number}.json"

    def _merge_queue_path(self) -> Path:
        return self._beads_dir / "merge-queue.json"

    def _campaign_path(self) -> Path:
        return self._beads_dir / "campaign.json"

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def read_work_bead(self, issue_number: int) -> WorkBead | None:
        """Return the WorkBead for *issue_number*, or None if absent."""
        path = self._work_path(issue_number)
        if not path.exists():
            return None
        return _load_work_bead(path)

    def read_pr_bead(self, pr_number: int) -> PRBead | None:
        """Return the PRBead for *pr_number*, or None if absent."""
        path = self._pr_path(pr_number)
        if not path.exists():
            return None
        return _load_pr_bead(path)

    def read_merge_queue(self) -> MergeQueue:
        """Return the MergeQueue, or an empty queue if the file is absent."""
        path = self._merge_queue_path()
        if not path.exists():
            return MergeQueue(v=BEAD_SCHEMA_VERSION)
        return _load_merge_queue(path)

    def read_campaign_bead(self) -> CampaignBead | None:
        """Return the CampaignBead, or None if no campaign file exists."""
        path = self._campaign_path()
        if not path.exists():
            return None
        return _load_campaign_bead(path)

    def write_campaign_bead(self, bead: CampaignBead) -> None:
        """Atomically write *bead* to disk."""
        path = self._campaign_path()
        _atomic_write(path, _campaign_bead_to_dict(bead))

    # ------------------------------------------------------------------
    # Lists
    # ------------------------------------------------------------------

    def list_work_beads(self, state: str | None = None) -> list[WorkBead]:
        """Return all WorkBeads, optionally filtered by *state*."""
        work_dir = self._beads_dir / "work"
        results: list[WorkBead] = []
        for p in sorted(work_dir.glob("*.json")):
            try:
                bead = _load_work_bead(p)
            except BeadCorruptError:
                continue
            if state is None or bead.state == state:
                results.append(bead)
        return results

    def list_pr_beads(self, state: str | None = None) -> list[PRBead]:
        """Return all PRBeads, optionally filtered by *state*."""
        prs_dir = self._beads_dir / "prs"
        results: list[PRBead] = []
        for p in sorted(prs_dir.glob("pr-*.json")):
            try:
                bead = _load_pr_bead(p)
            except BeadCorruptError:
                continue
            if state is None or bead.state == state:
                results.append(bead)
        return results

    # ------------------------------------------------------------------
    # Writes (atomic)
    # ------------------------------------------------------------------

    def write_work_bead(self, bead: WorkBead) -> None:
        """Atomically write *bead* to disk."""
        path = self._work_path(bead.issue_number)
        _atomic_write(path, _work_bead_to_dict(bead))

    def write_pr_bead(self, bead: PRBead) -> None:
        """Atomically write *bead* to disk."""
        path = self._pr_path(bead.pr_number)
        _atomic_write(path, _pr_bead_to_dict(bead))

    def write_merge_queue(self, queue: MergeQueue) -> None:
        """Atomically write the merge queue to disk."""
        path = self._merge_queue_path()
        _atomic_write(path, _merge_queue_to_dict(queue))

    def delete_work_bead(self, issue_number: int) -> None:
        """Delete the WorkBead for *issue_number* if it exists."""
        path = self._work_path(issue_number)
        if path.exists():
            path.unlink()

    # ------------------------------------------------------------------
    # Flush to state repo
    # ------------------------------------------------------------------

    def flush(self, message: str) -> None:
        """Commit and push dirty bead files to the state repo.

        No-op when ``state_repo_path`` is None or the working tree is clean.

        Args:
            message: Git commit message.
        """
        if self._state_repo_path is None:
            return
        repo = self._state_repo_path
        if not (repo / ".git").exists():
            return
        # Stage all changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if not result.stdout.strip():
            return  # nothing to commit
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=repo, check=True)
        subprocess.run(["git", "push"], cwd=repo, check=True)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def make_bead_store(config: Any, repo_slug: str) -> BeadStore:
    """Create a BeadStore for the given repo slug.

    The bead directory is: ``config.beads_dir / <owner> / <repo>``.

    If ``config.state_repo`` is set, the state repo is cloned to
    ``config.state_repo_dir / <owner>-<repo>`` (if not already present).

    Args:
        config:    A Config instance with ``beads_dir``, ``state_repo``, and
                   ``state_repo_dir`` fields.
        repo_slug: GitHub repo in ``"owner/repo"`` format.
    """
    beads_dir = config.beads_dir.expanduser() / repo_slug.replace("/", os.sep)
    state_repo_path = None
    if config.state_repo:
        state_repo_path = config.state_repo_dir.expanduser() / config.state_repo.replace("/", "-")
        if not (state_repo_path / ".git").exists():
            state_repo_path.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["gh", "repo", "clone", config.state_repo, str(state_repo_path)],
                check=True,
            )
    return BeadStore(beads_dir=beads_dir, state_repo_path=state_repo_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, data: dict) -> None:
    """Write *data* as JSON to *path* via a .tmp sibling (atomic on POSIX)."""
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def _load_json(path: Path) -> dict:
    """Read and parse a JSON bead file, raising BeadCorruptError on failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise BeadCorruptError(f"Bead file corrupt: {path}") from exc


def _load_work_bead(path: Path) -> WorkBead:
    data = _load_json(path)
    return WorkBead(
        v=data.get("v", BEAD_SCHEMA_VERSION),
        issue_number=data["issue_number"],
        title=data.get("title", ""),
        milestone=data.get("milestone", ""),
        stage=data.get("stage", ""),
        module=data.get("module", ""),
        priority=data.get("priority", ""),
        state=data.get("state", "open"),
        branch=data.get("branch", ""),
        pr_id=data.get("pr_id"),
        retry_count=data.get("retry_count", 0),
        blocked_by=data.get("blocked_by", []),
        deferred=data.get("deferred", False),
        claimed_at=data.get("claimed_at"),
        closed_at=data.get("closed_at"),
    )


def _load_pr_bead(path: Path) -> PRBead:
    data = _load_json(path)
    feedback = [
        FeedbackItem(
            comment_id=f.get("comment_id", ""),
            author=f.get("author", ""),
            is_bot=f.get("is_bot", False),
            triage=f.get("triage", "pending"),
            filed_issue=f.get("filed_issue"),
            triage_reason=f.get("triage_reason"),
        )
        for f in data.get("feedback", [])
    ]
    return PRBead(
        v=data.get("v", BEAD_SCHEMA_VERSION),
        pr_number=data["pr_number"],
        issue_number=data.get("issue_number", 0),
        branch=data.get("branch", ""),
        state=data.get("state", "open"),
        ci_state=data.get("ci_state"),
        conflict_state=data.get("conflict_state"),
        fix_attempts=data.get("fix_attempts", 0),
        feedback=feedback,
        created_at=data.get("created_at"),
        merged_at=data.get("merged_at"),
    )


def _load_merge_queue(path: Path) -> MergeQueue:
    data = _load_json(path)
    entries = [
        MergeQueueEntry(
            pr_number=e["pr_number"],
            issue_number=e.get("issue_number", 0),
            branch=e.get("branch", ""),
            enqueued_at=e.get("enqueued_at", ""),
        )
        for e in data.get("queue", [])
    ]
    return MergeQueue(
        v=data.get("v", BEAD_SCHEMA_VERSION),
        queue=entries,
        updated_at=data.get("updated_at", ""),
    )


def _work_bead_to_dict(bead: WorkBead) -> dict:
    return asdict(bead)


def _pr_bead_to_dict(bead: PRBead) -> dict:
    return asdict(bead)


def _merge_queue_to_dict(queue: MergeQueue) -> dict:
    return asdict(queue)


def _load_campaign_bead(path: Path) -> CampaignBead:
    data = _load_json(path)
    return CampaignBead(
        v=data.get("v", 1),
        repo=data.get("repo", ""),
        milestones=data.get("milestones", []),
        current_index=data.get("current_index", 0),
        statuses=data.get("statuses", {}),
        updated_at=data.get("updated_at", ""),
    )


def _campaign_bead_to_dict(bead: CampaignBead) -> dict:
    return asdict(bead)
