"""Bead files — atomic JSON state for brimstone orchestration.

Replaces checkpoint.json for issue/PR lifecycle tracking. Six bead types:
  - WorkBead:       issue lifecycle (claimed → pr_open → merge_ready → closed)
  - PRBead:         PR + feedback triage state
  - MergeQueue:     sequential merge ordering (replaces inline gh pr merge calls)
  - CampaignBead:   multi-milestone campaign progress tracking
  - MilestoneBead:  per-milestone lifecycle state (one file per milestone)
  - AnomalyBead:    monitor-detected aberration lifecycle (open → repaired | wont_fix)

Beads are stored under ~/.brimstone/beads/<owner>/<repo>/:
  work/<issue_number>.json
  prs/pr-<pr_number>.json
  milestones/<milestone-name>.json
  anomalies/<anomaly_id>.json        ← pinned to the repo where anomaly was detected
  merge-queue.json
  campaign.json
  events/work-<issue_number>.jsonl   ← append-only state-transition log
  events/pr-<pr_number>.jsonl

All writes use write-to-.tmp + os.replace (atomic on POSIX).
Event log appends use line-at-a-time writes (single POSIX write ≤ 4 KiB).
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
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
    restart_count: int = 0  # nuclear-restart count; exhausted after 2 restarts
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
    priority: int = 0  # higher = merge sooner; default 0
    attempts: int = 0  # rebase attempt count; incremented on retriable rebase failures
    merge_attempts: int = 0  # squash-merge attempt count; incremented on conflict-race retries


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
    #
    # Cross-repo milestone dependencies.
    # Maps milestone name → list of "owner/repo:milestone" blockers that must
    # be "shipped" before this milestone can begin.
    # Example: {"v0.2.1-multi-model": ["bread-wood/brimstone:v0.2.0-hardened-core"]}
    # Example: {"v0.1.0-knowledge-wire": ["bread-wood/moot:v0.4.0-council-spine"]}
    milestone_blocked_by: dict[str, list[str]] = field(default_factory=dict)
    updated_at: str = ""


@dataclass
class MilestoneBead:
    """Per-milestone lifecycle state — one file per milestone.

    Pinned to one repo.  Cross-repo dependencies are expressed as
    ``"owner/repo:milestone"`` strings in ``blocked_by``.

    Status values (same vocabulary as CampaignBead.statuses):
        "pending" | "planning" | "researching" | "designing"
        | "scoping" | "implementing" | "shipped"
    """

    v: int
    repo: str
    name: str  # milestone title, e.g. "v0.2.0"
    status: str
    blocked_by: list[str] = field(default_factory=list)  # ["owner/repo:milestone"]
    created_at: str | None = None
    updated_at: str | None = None
    shipped_at: str | None = None


@dataclass
class AnomalyBead:
    """Monitor-detected aberration — one file per anomaly, pinned to source repo.

    Lifecycle: open → repaired | wont_fix
    Auto-repair anomalies attempt inline fixes; deferred anomalies file a GH issue
    in the source repo's ``repairs`` milestone and wait for human resolution.
    """

    v: int = 1
    anomaly_id: str = ""  # first 16 hex chars of SHA-256(fingerprint)
    source_repo: str = ""  # "owner/repo" where anomaly was detected
    kind: str = ""  # e.g. "label_drift", "dep_cycle"
    severity: str = ""  # "warning" | "critical"
    is_blocking: bool = False  # blocks the active milestone's critical path
    repair_tier: str = "probe"  # "inline" | "bug" | "probe"
    description: str = ""
    details: dict = field(default_factory=dict)
    state: str = "open"  # "open" | "repaired" | "wont_fix"
    auto_repair_attempts: int = 0
    gh_issue_number: int | None = None
    gh_issue_url: str | None = None
    detected_at: str = ""
    resolved_at: str | None = None


@dataclass
class BeadEvent:
    """A single state-transition event appended to an events JSONL file."""

    ts: str  # ISO UTC timestamp
    bead_type: str  # "work" | "pr"
    bead_id: str  # str(issue_number) or str(pr_number)
    from_state: str | None
    to_state: str
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Module-level dep-graph helpers
# ---------------------------------------------------------------------------


def detect_dep_cycles(beads: list[WorkBead]) -> list[list[int]]:
    """DFS cycle detection over the ``blocked_by`` dependency graph.

    Returns a list of cycle paths (each path is a list of issue numbers that
    form the cycle).  Returns ``[]`` when the graph is acyclic.

    Only active beads (state not in ``{"closed", "abandoned"}``) participate.
    """
    active = {b.issue_number for b in beads if b.state not in ("closed", "abandoned")}
    graph: dict[int, list[int]] = {
        b.issue_number: [d for d in b.blocked_by if d in active]
        for b in beads
        if b.state not in ("closed", "abandoned")
    }
    cycles: list[list[int]] = []
    visited: set[int] = set()
    rec_stack: set[int] = set()
    path: list[int] = []

    def _dfs(node: int) -> bool:
        visited.add(node)
        rec_stack.add(node)
        path.append(node)
        for neighbour in graph.get(node, []):
            if neighbour not in visited:
                if _dfs(neighbour):
                    return True
            elif neighbour in rec_stack:
                cycle_start = path.index(neighbour)
                cycles.append(path[cycle_start:] + [neighbour])
                return True
        path.pop()
        rec_stack.discard(node)
        return False

    for node in list(graph):
        if node not in visited:
            _dfs(node)
    return cycles


# ---------------------------------------------------------------------------
# BeadStore
# ---------------------------------------------------------------------------


class BeadStore:
    """Read/write interface for bead files.

    All writes are atomic: data is written to a .tmp sibling then os.replace()d
    over the target. On POSIX this guarantees no reader sees a partial write.

    State-transition events are appended to per-bead JSONL files under
    ``events/``.  Each line is a JSON object; writes are single-call appends
    (atomic for payloads under the OS page size).

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
        (beads_dir / "milestones").mkdir(parents=True, exist_ok=True)
        (beads_dir / "anomalies").mkdir(parents=True, exist_ok=True)
        (beads_dir / "events").mkdir(parents=True, exist_ok=True)

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

    def _milestone_path(self, name: str) -> Path:
        safe_name = name.replace("/", "-")
        return self._beads_dir / "milestones" / f"{safe_name}.json"

    def _anomaly_path(self, anomaly_id: str) -> Path:
        return self._beads_dir / "anomalies" / f"{anomaly_id}.json"

    def _events_path(self, bead_type: str, bead_id: str) -> Path:
        return self._beads_dir / "events" / f"{bead_type}-{bead_id}.jsonl"

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------

    def append_event(
        self,
        bead_type: str,
        bead_id: str,
        from_state: str | None,
        to_state: str,
        meta: dict | None = None,
    ) -> None:
        """Append a state-transition event to the bead's JSONL event log.

        Each call appends exactly one line.  Safe for concurrent readers
        (readers see complete lines; a partial line is never flushed without
        the preceding newline because we write the whole line in one call).
        """
        event = {
            "ts": datetime.now(UTC).isoformat(),
            "bead_type": bead_type,
            "bead_id": bead_id,
            "from": from_state,
            "to": to_state,
            "meta": meta or {},
        }
        path = self._events_path(bead_type, bead_id)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def read_events(self, bead_type: str, bead_id: str) -> list[BeadEvent]:
        """Return all events for the given bead, oldest first."""
        path = self._events_path(bead_type, bead_id)
        if not path.exists():
            return []
        events: list[BeadEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                events.append(
                    BeadEvent(
                        ts=d.get("ts", ""),
                        bead_type=d.get("bead_type", bead_type),
                        bead_id=d.get("bead_id", bead_id),
                        from_state=d.get("from"),
                        to_state=d.get("to", ""),
                        meta=d.get("meta", {}),
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue
        return events

    # ------------------------------------------------------------------
    # Dep-graph helpers
    # ------------------------------------------------------------------

    def check_deps_satisfied(self, bead: WorkBead) -> tuple[bool, list[int]]:
        """Check whether all ``blocked_by`` deps have reached a terminal state.

        Returns ``(satisfied, blocking)`` where *blocking* is the list of
        issue numbers that still have a non-terminal bead (or no bead at all).
        When *satisfied* is True, *blocking* is empty.
        """
        blocking: list[int] = []
        for dep_num in bead.blocked_by:
            dep = self.read_work_bead(dep_num)
            if dep is None or dep.state not in ("closed", "abandoned"):
                blocking.append(dep_num)
        return (len(blocking) == 0, blocking)

    def detect_dep_cycles(self, milestone: str | None = None) -> list[list[int]]:
        """Run DFS cycle detection over beads for *milestone* (or all beads).

        Delegates to the module-level :func:`detect_dep_cycles` function.
        """
        beads = self.list_work_beads(milestone=milestone)
        return detect_dep_cycles(beads)

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

    def read_milestone_bead(self, name: str) -> MilestoneBead | None:
        """Return the MilestoneBead for *name*, or None if absent."""
        path = self._milestone_path(name)
        if not path.exists():
            return None
        return _load_milestone_bead(path)

    def write_milestone_bead(self, bead: MilestoneBead) -> None:
        """Atomically write *bead* to disk."""
        path = self._milestone_path(bead.name)
        _atomic_write(path, _milestone_bead_to_dict(bead))

    def read_anomaly_bead(self, anomaly_id: str) -> AnomalyBead | None:
        """Return the AnomalyBead for *anomaly_id*, or None if absent."""
        path = self._anomaly_path(anomaly_id)
        if not path.exists():
            return None
        return _load_anomaly_bead(path)

    def write_anomaly_bead(self, bead: AnomalyBead) -> None:
        """Atomically write *bead* to disk."""
        path = self._anomaly_path(bead.anomaly_id)
        _atomic_write(path, _anomaly_bead_to_dict(bead))

    def list_anomaly_beads(self, state: str | None = None) -> list[AnomalyBead]:
        """Return all AnomalyBeads, optionally filtered by *state*."""
        anomalies_dir = self._beads_dir / "anomalies"
        results: list[AnomalyBead] = []
        for p in sorted(anomalies_dir.glob("*.json")):
            try:
                bead = _load_anomaly_bead(p)
            except BeadCorruptError:
                continue
            if state is None or bead.state == state:
                results.append(bead)
        return results

    # ------------------------------------------------------------------
    # Lists
    # ------------------------------------------------------------------

    def list_work_beads(
        self,
        state: str | None = None,
        milestone: str | None = None,
        stage: str | None = None,
    ) -> list[WorkBead]:
        """Return all WorkBeads, optionally filtered by *state*, *milestone*, and/or *stage*."""
        work_dir = self._beads_dir / "work"
        results: list[WorkBead] = []
        for p in sorted(work_dir.glob("*.json")):
            try:
                bead = _load_work_bead(p)
            except BeadCorruptError:
                continue
            if state is not None and bead.state != state:
                continue
            if milestone is not None and bead.milestone != milestone:
                continue
            if stage is not None and bead.stage != stage:
                continue
            results.append(bead)
        return results

    def scope_needs_rerun(self, milestone: str) -> bool:
        """Return True if a design LLD was closed after scope last ran.

        Compares the latest design bead ``closed`` event timestamp against
        the earliest impl bead creation event timestamp.  If any design bead
        closed *after* the first impl bead was seeded, scope ran before all
        LLDs were merged and must re-run to file the missing impl issues.

        Returns False (don't rerun) when:
        - No impl beads exist yet (scope hasn't run; handled by the caller).
        - All design close events predate the earliest impl bead creation.
        - Event log files are absent (old bead pre-dating event log).

        Returns True (rerun needed) when:
        - The latest design close event is newer than the earliest impl
          bead creation event (a new LLD merged after scope ran).
        - Impl beads exist but none have a creation event (conservative:
          we can't prove scope was complete, so rerun).
        """
        design_beads = self.list_work_beads(milestone=milestone, stage="design")
        impl_beads = self.list_work_beads(milestone=milestone, stage="impl")
        if not impl_beads:
            return False  # scope hasn't run; caller decides whether to run it

        latest_design_close: str | None = None
        for bead in design_beads:
            for ev in self.read_events("work", str(bead.issue_number)):
                if ev.to_state == "closed":
                    if latest_design_close is None or ev.ts > latest_design_close:
                        latest_design_close = ev.ts

        if latest_design_close is None:
            return False  # no design bead has ever closed; nothing to compare

        earliest_impl_created: str | None = None
        for bead in impl_beads:
            for ev in self.read_events("work", str(bead.issue_number)):
                if ev.from_state is None and ev.to_state == "open":
                    if earliest_impl_created is None or ev.ts < earliest_impl_created:
                        earliest_impl_created = ev.ts
                    break

        if earliest_impl_created is None:
            # Impl beads exist but have no creation events (pre-event-log era).
            # Be conservative: re-run scope to ensure all LLDs are covered.
            return True

        return latest_design_close > earliest_impl_created

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

    def list_milestone_beads(self, status: str | None = None) -> list[MilestoneBead]:
        """Return all MilestoneBeads, optionally filtered by *status*."""
        ms_dir = self._beads_dir / "milestones"
        results: list[MilestoneBead] = []
        for p in sorted(ms_dir.glob("*.json")):
            try:
                bead = _load_milestone_bead(p)
            except BeadCorruptError:
                continue
            if status is None or bead.status == status:
                results.append(bead)
        return results

    # ------------------------------------------------------------------
    # Writes (atomic) + event emission
    # ------------------------------------------------------------------

    def write_work_bead(self, bead: WorkBead) -> None:
        """Atomically write *bead* to disk, appending a state-transition event."""
        path = self._work_path(bead.issue_number)
        old_state: str | None = None
        if path.exists():
            try:
                old = _load_work_bead(path)
                old_state = old.state
            except BeadCorruptError:
                pass
        _atomic_write(path, _work_bead_to_dict(bead))
        if bead.state != old_state:
            self.append_event(
                bead_type="work",
                bead_id=str(bead.issue_number),
                from_state=old_state,
                to_state=bead.state,
            )

    def write_pr_bead(self, bead: PRBead) -> None:
        """Atomically write *bead* to disk, appending a state-transition event."""
        path = self._pr_path(bead.pr_number)
        old_state: str | None = None
        if path.exists():
            try:
                old = _load_pr_bead(path)
                old_state = old.state
            except BeadCorruptError:
                pass
        _atomic_write(path, _pr_bead_to_dict(bead))
        if bead.state != old_state:
            self.append_event(
                bead_type="pr",
                bead_id=str(bead.pr_number),
                from_state=old_state,
                to_state=bead.state,
            )

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
            priority=e.get("priority", 0),
            attempts=e.get("attempts", 0),
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
        milestone_blocked_by=data.get("milestone_blocked_by", {}),
        updated_at=data.get("updated_at", ""),
    )


def _campaign_bead_to_dict(bead: CampaignBead) -> dict:
    return asdict(bead)


def _load_milestone_bead(path: Path) -> MilestoneBead:
    data = _load_json(path)
    return MilestoneBead(
        v=data.get("v", BEAD_SCHEMA_VERSION),
        repo=data.get("repo", ""),
        name=data.get("name", ""),
        status=data.get("status", "pending"),
        blocked_by=data.get("blocked_by", []),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        shipped_at=data.get("shipped_at"),
    )


def _milestone_bead_to_dict(bead: MilestoneBead) -> dict:
    return asdict(bead)


def _load_anomaly_bead(path: Path) -> AnomalyBead:
    data = _load_json(path)
    return AnomalyBead(
        v=data.get("v", 1),
        anomaly_id=data.get("anomaly_id", ""),
        source_repo=data.get("source_repo", ""),
        kind=data.get("kind", ""),
        severity=data.get("severity", ""),
        is_blocking=data.get("is_blocking", False),
        repair_tier=data.get("repair_tier", "deferred"),
        description=data.get("description", ""),
        details=data.get("details", {}),
        state=data.get("state", "open"),
        auto_repair_attempts=data.get("auto_repair_attempts", 0),
        gh_issue_number=data.get("gh_issue_number"),
        gh_issue_url=data.get("gh_issue_url"),
        detected_at=data.get("detected_at", ""),
        resolved_at=data.get("resolved_at"),
    )


def _anomaly_bead_to_dict(bead: AnomalyBead) -> dict:
    return asdict(bead)
