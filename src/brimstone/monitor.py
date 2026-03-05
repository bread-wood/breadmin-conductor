"""Brimstone monitor — continuous bead/repo health checks.

Runs a detection loop that compares bead state against GitHub state, bead-ifies
every anomaly in the source repo's bead store, and responds in one of three tiers:

  inline  — fix is trivial and deterministic; monitor applies it directly with no
             issue filed (e.g. add/remove a GitHub label, insert a MergeQueue entry).

  bug     — fix is known but requires agent execution; a ``stage/impl`` repair issue
             is filed in the repo's ``repairs`` milestone so the normal impl pipeline
             can pick it up.

  probe   — the cause is unclear; a ``stage/research`` issue is filed in ``repairs``
             so an agent can investigate before a fix is attempted.

AnomalyBeads live in the source repo's bead store (``anomalies/<id>.json``).
When brimstone watches multiple repos (``--watch``), each repo's anomaly beads stay
in that repo's bead store and repair issues are filed in that repo's ``repairs``
milestone — never cross-contaminated.

Detector inventory
------------------
check_label_drift         claimed bead <-> in-progress GitHub label mismatch
check_dep_integrity       phantom deps (dep bead missing) + dep cycles
check_state_regressions   illegal bead-state transitions in event log
check_orphaned_merge      merge_ready beads absent from the MergeQueue
check_pre_pr_zombies      claimed beads older than timeout with no PRBead

Repair tiers per anomaly kind
------------------------------
label_drift           inline   (add or remove the label)
orphaned_merge        inline   (insert MergeQueue entry)
pre_pr_zombie         bug      (create missing PR, or reset bead if branch gone)
dep_cycle             probe    (which blocked_by edge is stale?)
phantom_dep           probe    (was the dep intentionally removed?)
state_regression      probe    (what caused the illegal transition?)
detector_error        probe    (why did the detector itself fail?)
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from brimstone.beads import (
    BEAD_SCHEMA_VERSION,
    AnomalyBead,
    BeadStore,
    MergeQueueEntry,
    detect_dep_cycles,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MONITOR_INTERVAL_SECONDS: int = 60
ZOMBIE_TIMEOUT_MINUTES: float = 90.0
MONITOR_FILED_FILENAME: str = "monitor-filed.json"
REPAIRS_MILESTONE: str = "repairs"
INLINE_REPAIR_MAX_ATTEMPTS: int = 3  # escalate to bug after this many inline failures

# Illegal state transitions: (from_state, to_state) pairs that must never appear
_BAD_TRANSITIONS: frozenset[tuple[str | None, str]] = frozenset(
    [
        ("merge_ready", "open"),
        ("merge_ready", "claimed"),
        ("closed", "open"),
        ("closed", "claimed"),
        ("abandoned", "open"),
        ("abandoned", "claimed"),
    ]
)

# ---------------------------------------------------------------------------
# Anomaly dataclass
# ---------------------------------------------------------------------------


@dataclass
class Anomaly:
    """A single detected aberration in bead or repo state.

    Attributes
    ----------
    kind:         Short machine-readable tag (e.g. ``"label_drift"``).
    severity:     ``"warning"`` or ``"critical"``.
    description:  One-line human summary.
    details:      Dict of supporting evidence (serialised into the filed issue).
    needs_agent:  Kept for backward compatibility; superseded by ``repair_tier``.
    is_blocking:  True when this anomaly can prevent the active milestone from
                  making forward progress — set by ``classify_blocking()``.
    repair_tier:  How the monitor responds:
                  ``"inline"``  — fix applied directly, no issue filed
                  ``"bug"``     — ``stage/impl`` issue in ``repairs`` milestone
                  ``"probe"``   — ``stage/research`` issue in ``repairs`` milestone
    """

    kind: str
    severity: str  # "warning" | "critical"
    description: str
    details: dict = field(default_factory=dict)
    needs_agent: bool = False
    is_blocking: bool = False
    repair_tier: str = "probe"  # "inline" | "bug" | "probe"

    def fingerprint(self) -> str:
        """Stable string key used for dedup (kind + primary detail values)."""
        detail_str = json.dumps(self.details, sort_keys=True)
        return f"{self.kind}:{detail_str}"


# ---------------------------------------------------------------------------
# Anomaly ID
# ---------------------------------------------------------------------------


def _anomaly_id(anomaly: Anomaly) -> str:
    """Return a stable 16-char hex ID: first 16 chars of SHA-256(fingerprint)."""
    return hashlib.sha256(anomaly.fingerprint().encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Active-milestone helper
# ---------------------------------------------------------------------------


def _get_active_milestone(store: BeadStore) -> str | None:
    """Return the name of the currently-active campaign milestone, or None."""
    campaign = store.read_campaign_bead()
    if campaign is None:
        return None
    for ms in campaign.milestones[campaign.current_index :]:
        if campaign.statuses.get(ms) != "shipped":
            return ms
    return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_blocking(
    anomaly: Anomaly,
    store: BeadStore,
    active_milestone: str | None,
) -> bool:
    """Return True when this anomaly can prevent the active milestone from progressing."""
    kind = anomaly.kind

    if kind in ("dep_cycle", "phantom_dep", "state_regression", "orphaned_merge"):
        return True

    if kind == "detector_error":
        return False

    if kind == "pre_pr_zombie":
        if active_milestone is None:
            return False
        issue_number = anomaly.details.get("issue_number")
        if issue_number is None:
            return False
        bead = store.read_work_bead(issue_number)
        return bead is not None and bead.milestone == active_milestone

    if kind == "label_drift":
        # Only blocking when a terminal bead still carries the active label —
        # a zombie label could fool the orchestrator into skipping a re-dispatch.
        bead_state = anomaly.details.get("bead_state")
        has_label = anomaly.details.get("has_label", False)
        return has_label and bead_state in ("closed", "abandoned")

    return False


def classify_repair_tier(anomaly: Anomaly) -> str:
    """Return the repair tier for *anomaly* (pure function of kind + details)."""
    kind = anomaly.kind
    # Inline: trivially safe, reversible operations the monitor can do itself
    if kind in ("label_drift", "orphaned_merge"):
        return "inline"
    # Bug: fix is known, an impl agent can execute it without research
    if kind == "pre_pr_zombie":
        return "bug"
    # Everything else: cause is unclear, needs investigation first
    return "probe"


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def check_label_drift(store: BeadStore, repo: str) -> list[Anomaly]:
    """Detect claimed beads without in-progress label (and vice-versa)."""
    anomalies: list[Anomaly] = []

    result = _gh(
        [
            "issue",
            "list",
            "--label",
            "in-progress",
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number",
        ],
        repo=repo,
        check=False,
    )
    in_progress_numbers: set[int] = set()
    if result.returncode == 0 and result.stdout.strip():
        try:
            in_progress_numbers = {i["number"] for i in json.loads(result.stdout)}
        except (json.JSONDecodeError, KeyError):
            pass

    claimed_beads = store.list_work_beads(state="claimed")
    claimed_numbers = {b.issue_number for b in claimed_beads}

    for num in claimed_numbers - in_progress_numbers:
        anomalies.append(
            Anomaly(
                kind="label_drift",
                severity="warning",
                description=f"Issue #{num} has claimed bead but missing in-progress label",
                details={"issue_number": num, "bead_state": "claimed", "has_label": False},
            )
        )

    for num in in_progress_numbers - claimed_numbers:
        bead = store.read_work_bead(num)
        bead_state = bead.state if bead else None
        severity = "critical" if bead_state in ("closed", "abandoned") else "warning"
        anomalies.append(
            Anomaly(
                kind="label_drift",
                severity=severity,
                description=(
                    f"Issue #{num} has in-progress label but bead state is "
                    f"{bead_state!r} (not claimed)"
                ),
                details={"issue_number": num, "bead_state": bead_state, "has_label": True},
            )
        )

    return anomalies


def check_dep_integrity(store: BeadStore) -> list[Anomaly]:
    """Detect phantom deps (referenced issue has no bead) and dep cycles."""
    anomalies: list[Anomaly] = []
    all_beads = store.list_work_beads()
    known = {b.issue_number for b in all_beads}

    for bead in all_beads:
        if bead.state in ("closed", "abandoned"):
            continue
        for dep in bead.blocked_by:
            if dep not in known:
                anomalies.append(
                    Anomaly(
                        kind="phantom_dep",
                        severity="critical",
                        description=(
                            f"Issue #{bead.issue_number} blocked_by #{dep} but #{dep} has no bead"
                        ),
                        details={"issue_number": bead.issue_number, "phantom_dep": dep},
                    )
                )

    cycles = detect_dep_cycles(all_beads)
    for cycle in cycles:
        anomalies.append(
            Anomaly(
                kind="dep_cycle",
                severity="critical",
                description=f"Dependency cycle detected: {' -> '.join(str(n) for n in cycle)}",
                details={"cycle": cycle},
            )
        )

    return anomalies


def check_state_regressions(store: BeadStore) -> list[Anomaly]:
    """Detect illegal state transitions in event logs."""
    anomalies: list[Anomaly] = []
    all_beads = store.list_work_beads()

    for bead in all_beads:
        events = store.read_events("work", str(bead.issue_number))
        prev_state: str | None = None
        for ev in events:
            transition = (ev.from_state, ev.to_state)
            if transition in _BAD_TRANSITIONS:
                anomalies.append(
                    Anomaly(
                        kind="state_regression",
                        severity="critical",
                        description=(
                            f"Issue #{bead.issue_number} illegal transition "
                            f"{ev.from_state!r} -> {ev.to_state!r} at {ev.ts}"
                        ),
                        details={
                            "issue_number": bead.issue_number,
                            "from_state": ev.from_state,
                            "to_state": ev.to_state,
                            "ts": ev.ts,
                        },
                    )
                )
            prev_state = ev.to_state  # noqa: F841

    return anomalies


def check_orphaned_merge(store: BeadStore) -> list[Anomaly]:
    """Detect merge_ready beads absent from the MergeQueue."""
    anomalies: list[Anomaly] = []
    merge_ready_beads = store.list_work_beads(state="merge_ready")
    if not merge_ready_beads:
        return anomalies

    queue = store.read_merge_queue()
    queued_issues = {e.issue_number for e in queue.queue}

    for bead in merge_ready_beads:
        if bead.issue_number not in queued_issues:
            anomalies.append(
                Anomaly(
                    kind="orphaned_merge",
                    severity="warning",
                    description=(
                        f"Issue #{bead.issue_number} is merge_ready but absent from MergeQueue"
                    ),
                    details={"issue_number": bead.issue_number, "branch": bead.branch},
                )
            )

    return anomalies


def check_pre_pr_zombies(
    store: BeadStore, timeout_minutes: float = ZOMBIE_TIMEOUT_MINUTES
) -> list[Anomaly]:
    """Detect claimed beads older than timeout with no associated PRBead."""
    anomalies: list[Anomaly] = []
    claimed_beads = store.list_work_beads(state="claimed")
    now = datetime.now(UTC)

    for bead in claimed_beads:
        if bead.pr_id is not None:
            continue
        if bead.claimed_at is None:
            continue
        try:
            claimed_dt = datetime.fromisoformat(bead.claimed_at)
        except ValueError:
            continue
        age_minutes = (now - claimed_dt).total_seconds() / 60
        if age_minutes >= timeout_minutes:
            anomalies.append(
                Anomaly(
                    kind="pre_pr_zombie",
                    severity="warning",
                    description=(
                        f"Issue #{bead.issue_number} claimed {age_minutes:.0f}m ago "
                        f"with no PR (branch: {bead.branch!r})"
                    ),
                    details={
                        "issue_number": bead.issue_number,
                        "branch": bead.branch,
                        "claimed_at": bead.claimed_at,
                        "age_minutes": round(age_minutes, 1),
                    },
                )
            )

    return anomalies


# ---------------------------------------------------------------------------
# All detectors
# ---------------------------------------------------------------------------

_DETECTORS = [
    ("label_drift", lambda store, repo: check_label_drift(store, repo)),
    ("dep_integrity", lambda store, repo: check_dep_integrity(store)),
    ("state_regressions", lambda store, repo: check_state_regressions(store)),
    ("orphaned_merge", lambda store, repo: check_orphaned_merge(store)),
    ("pre_pr_zombies", lambda store, repo: check_pre_pr_zombies(store)),
]


def run_all_detectors(store: BeadStore, repo: str) -> list[Anomaly]:
    """Run every detector and return the combined list of anomalies."""
    anomalies: list[Anomaly] = []
    for name, detector in _DETECTORS:
        try:
            found = detector(store, repo)
            anomalies.extend(found)
        except Exception as exc:  # noqa: BLE001
            anomalies.append(
                Anomaly(
                    kind="detector_error",
                    severity="warning",
                    description=f"Detector {name!r} raised an exception: {exc}",
                    details={"detector": name, "error": str(exc)},
                )
            )
    return anomalies


# ---------------------------------------------------------------------------
# Inline repair actions
# ---------------------------------------------------------------------------


def _inline_repair_label_drift(anomaly: Anomaly, repo: str) -> bool:
    """Add or remove the in-progress label to match bead state."""
    issue_number = anomaly.details.get("issue_number")
    has_label = anomaly.details.get("has_label", False)
    if issue_number is None:
        return False
    flag = "--remove-label" if has_label else "--add-label"
    result = _gh(
        ["issue", "edit", str(issue_number), flag, "in-progress"],
        repo=repo,
        check=False,
    )
    return result.returncode == 0


def _inline_repair_orphaned_merge(anomaly: Anomaly, store: BeadStore) -> bool:
    """Insert the missing MergeQueue entry for a merge_ready bead."""
    issue_number = anomaly.details.get("issue_number")
    if issue_number is None:
        return False
    bead = store.read_work_bead(issue_number)
    if bead is None or bead.pr_id is None:
        return False
    try:
        pr_number = int(bead.pr_id.replace("pr-", ""))
    except ValueError:
        return False
    queue = store.read_merge_queue()
    if any(e.issue_number == issue_number for e in queue.queue):
        return True  # already present; anomaly will clear on next scan
    entry = MergeQueueEntry(
        pr_number=pr_number,
        issue_number=issue_number,
        branch=bead.branch,
        enqueued_at=datetime.now(UTC).isoformat(),
        priority=0,
    )
    queue.queue.append(entry)
    queue.updated_at = datetime.now(UTC).isoformat()
    store.write_merge_queue(queue)
    return True


def _apply_inline_repair(anomaly: Anomaly, store: BeadStore, repo: str) -> bool:
    """Dispatch to the correct inline-repair handler. Returns True on success."""
    if anomaly.kind == "label_drift":
        return _inline_repair_label_drift(anomaly, repo)
    if anomaly.kind == "orphaned_merge":
        return _inline_repair_orphaned_merge(anomaly, store)
    return False


# ---------------------------------------------------------------------------
# Repair issue filing (bug + probe tiers)
# ---------------------------------------------------------------------------

_REPAIR_CHECKLISTS: dict[str, str] = {
    "pre_pr_zombie": (
        "- [ ] Check whether the agent branch (`{branch}`) exists on the remote\n"
        "- [ ] If branch has commits: `gh pr create --head {branch}` to recover the PR\n"
        "- [ ] If branch is empty or missing: reset bead state to `open` for re-dispatch\n"
        "  (`jq '.state = \"open\" | .pr_id = null | .claimed_at = null' "
        "~/.brimstone/beads/{repo}/work/{issue_number}.json > tmp && mv tmp ...`)\n"
        "- [ ] Verify: `brimstone monitor --once --dry-run --repo {repo}`"
    ),
    "dep_cycle": (
        "## Investigation\n\n"
        "- [ ] List all issues in the cycle (see details above)\n"
        "- [ ] For each `blocked_by` edge in the cycle, check whether the dependency\n"
        "  is still valid or was superseded\n"
        "- [ ] Remove the stale `blocked_by` entry from the WorkBead JSON\n"
        "- [ ] Verify: `brimstone monitor --once --dry-run --repo {repo}`"
    ),
    "phantom_dep": (
        "## Investigation\n\n"
        "- [ ] Check if issue #{phantom_dep} was intentionally closed/deleted\n"
        "- [ ] If stale reference: remove `{phantom_dep}` from issue "
        "#{issue_number}'s `blocked_by`\n"
        "- [ ] If genuinely missing: re-file the dependency issue and create its "
        "WorkBead\n"
        "- [ ] Verify: `brimstone monitor --once --dry-run --repo {repo}`"
    ),
    "state_regression": (
        "## Investigation\n\n"
        "- [ ] Read the event log: "
        "`cat ~/.brimstone/beads/{repo}/events/work-{issue_number}.jsonl`\n"
        "- [ ] Identify what caused the illegal `{from_state}` → `{to_state}` transition\n"
        "- [ ] If bead is corrupted: restore last valid state from event log\n"
        "- [ ] Document the root cause in a comment on this issue\n"
        "- [ ] Verify: `brimstone monitor --once --dry-run --repo {repo}`"
    ),
    "detector_error": (
        "## Investigation\n\n"
        "- [ ] Identify which detector raised: `{detector}`\n"
        "- [ ] Find the exception in monitor logs: `{error}`\n"
        "- [ ] Fix the detector or add a guard around the failing code path\n"
        "- [ ] Verify: `brimstone monitor --once --dry-run --repo {repo}`"
    ),
}


def _repair_checklist(anomaly: Anomaly, repo: str) -> str:
    template = _REPAIR_CHECKLISTS.get(
        anomaly.kind,
        "- [ ] Investigate the anomaly details above and resolve manually.",
    )
    # Fill in detail placeholders where present
    ctx = {"repo": repo, **anomaly.details}
    try:
        return template.format(**ctx)
    except KeyError:
        return template


def _get_repairs_milestone_number(repo: str) -> int | None:
    """Return the GH milestone number for the ``repairs`` milestone, or None."""
    result = _gh(
        [
            "api",
            f"repos/{repo}/milestones",
            "--jq",
            f'[.[] | select(.title=="{REPAIRS_MILESTONE}")] | first | .number',
        ],
        repo=None,
        check=False,
    )
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw or raw == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _build_repair_issue_body(anomaly: Anomaly, repo: str) -> str:
    details_block = json.dumps(anomaly.details, indent=2)
    checklist = _repair_checklist(anomaly, repo)
    blocking_str = "**yes — build may stall**" if anomaly.is_blocking else "no"
    tier_label = {"bug": "impl (fix known)", "probe": "research (investigate first)"}.get(
        anomaly.repair_tier, anomaly.repair_tier
    )
    return (
        f"## Monitor Anomaly: `{anomaly.kind}`\n\n"
        f"**Severity:** {anomaly.severity}  \n"
        f"**Is blocking:** {blocking_str}  \n"
        f"**Repair tier:** {tier_label}  \n"
        f"**Anomaly ID:** `{_anomaly_id(anomaly)}`\n\n"
        f"**Description:** {anomaly.description}\n\n"
        f"## Details\n"
        f"```json\n{details_block}\n```\n\n"
        f"## Checklist\n\n"
        f"{checklist}\n\n"
        f"*Filed automatically by `brimstone monitor`.*"
    )


def _file_repair_issue(anomaly: Anomaly, repo: str) -> str | None:
    """File a bug or probe repair issue in the repo's ``repairs`` milestone.

    Returns the issue URL on success, None on failure.
    """
    ms_number = _get_repairs_milestone_number(repo)
    if ms_number is None:
        print(
            f"[monitor] WARN: '{REPAIRS_MILESTONE}' milestone missing in {repo}; "
            f"run 'brimstone monitor --init --repo={repo}' to create it"
        )
        return None

    priority = "P0" if anomaly.is_blocking else "P2"
    stage = "stage/impl" if anomaly.repair_tier == "bug" else "stage/research"
    labels = f"bug,{priority},{stage}"
    title = f"[monitor/{anomaly.repair_tier}] {anomaly.kind}: {anomaly.description[:80]}"
    body = _build_repair_issue_body(anomaly, repo)

    result = _gh(
        [
            "issue",
            "create",
            "--title",
            title,
            "--label",
            labels,
            "--milestone",
            str(ms_number),
            "--body",
            body,
        ],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url if url else None


# ---------------------------------------------------------------------------
# Legacy issue filing (kept for backward compatibility)
# ---------------------------------------------------------------------------


def _build_issue_body(anomaly: Anomaly) -> str:
    details_block = json.dumps(anomaly.details, indent=2)
    return (
        f"## Monitor Anomaly: `{anomaly.kind}`\n\n"
        f"**Severity:** {anomaly.severity}\n\n"
        f"**Description:** {anomaly.description}\n\n"
        f"## Details\n"
        f"```json\n{details_block}\n```\n\n"
        f"*Filed automatically by `brimstone monitor`.*"
    )


def file_anomaly_issue(anomaly: Anomaly, repo: str) -> str | None:
    """File a GitHub issue for the anomaly (legacy path, no AnomalyBead).

    Prefer ``_file_repair_issue`` for new callers.
    Returns the issue URL or None on failure.
    """
    label = "bug,P1" if anomaly.severity == "critical" else "bug,P2"
    title = f"[monitor] {anomaly.kind}: {anomaly.description[:80]}"
    body = _build_issue_body(anomaly)
    result = _gh(
        ["issue", "create", "--title", title, "--label", label, "--body", body],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url if url else None


# ---------------------------------------------------------------------------
# Dedup / processing
# ---------------------------------------------------------------------------


def _load_filed(beads_dir: Path) -> dict[str, str]:
    """Load the legacy monitor-filed.json dedup map {fingerprint: issue_url}."""
    path = beads_dir / MONITOR_FILED_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_filed(beads_dir: Path, filed: dict[str, str]) -> None:
    """Atomically write the monitor-filed.json dedup map (legacy)."""
    path = beads_dir / MONITOR_FILED_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(filed, indent=2), encoding="utf-8")
    tmp.replace(path)


def process_anomalies(
    anomalies: list[Anomaly],
    store: BeadStore,
    repo: str,
    dry_run: bool = False,
    # bugs_repo kept for backward compat but ignored — issues go to repo's repairs milestone
    bugs_repo: str | None = None,  # noqa: ARG001
) -> list[str]:
    """Classify, bead-ify, and respond to each detected anomaly.

    For each anomaly:
    - Sets ``is_blocking`` and ``repair_tier`` on the Anomaly object.
    - Creates an AnomalyBead in the source repo's bead store (dedup by anomaly_id).
    - ``inline``: applies the fix directly, no issue filed.
    - ``bug``: files a ``stage/impl`` issue in repo's ``repairs`` milestone.
    - ``probe``: files a ``stage/research`` issue in repo's ``repairs`` milestone.

    Also runs a cleanup sweep: AnomalyBeads in ``open`` state whose anomaly no
    longer appears are transitioned to ``repaired``.

    Returns a list of URLs for repair issues filed this run.
    """
    active_milestone = _get_active_milestone(store)

    for anomaly in anomalies:
        anomaly.is_blocking = classify_blocking(anomaly, store, active_milestone)
        anomaly.repair_tier = classify_repair_tier(anomaly)

    current_ids = {_anomaly_id(a) for a in anomalies}

    # Cleanup sweep: mark resolved AnomalyBeads
    for abead in store.list_anomaly_beads(state="open"):
        if abead.anomaly_id not in current_ids:
            abead.state = "repaired"
            abead.resolved_at = datetime.now(UTC).isoformat()
            store.write_anomaly_bead(abead)
            print(f"[monitor] anomaly {abead.anomaly_id} ({abead.kind}) resolved")

    # Legacy fallback: fingerprints already filed before AnomalyBeads existed
    legacy_filed = _load_filed(store._beads_dir)
    new_urls: list[str] = []

    for anomaly in anomalies:
        aid = _anomaly_id(anomaly)
        fp = anomaly.fingerprint()

        existing = store.read_anomaly_bead(aid)

        # Skip terminal anomalies
        if existing and existing.state in ("repaired", "wont_fix"):
            continue

        # Skip anomalies covered by legacy dedup
        if existing is None and fp in legacy_filed:
            continue

        if dry_run:
            status = "BLOCKING" if anomaly.is_blocking else "non-blocking"
            print(
                f"[monitor/dry-run] {status} {anomaly.repair_tier.upper()} "
                f"{anomaly.kind}: {anomaly.description}"
            )
            continue

        # Create AnomalyBead if new
        if existing is None:
            existing = AnomalyBead(
                v=BEAD_SCHEMA_VERSION,
                anomaly_id=aid,
                source_repo=repo,
                kind=anomaly.kind,
                severity=anomaly.severity,
                is_blocking=anomaly.is_blocking,
                repair_tier=anomaly.repair_tier,
                description=anomaly.description,
                details=anomaly.details,
                state="open",
                auto_repair_attempts=0,
                detected_at=datetime.now(UTC).isoformat(),
            )
            store.write_anomaly_bead(existing)
            print(f"[monitor] new anomaly {aid} ({anomaly.kind}, {anomaly.repair_tier})")

        # --- Inline tier ---
        if anomaly.repair_tier == "inline":
            success = _apply_inline_repair(anomaly, store, repo)
            existing.auto_repair_attempts += 1
            store.write_anomaly_bead(existing)
            if success:
                print(f"[monitor] inline repair applied for {aid} ({anomaly.kind})")
            else:
                attempts = existing.auto_repair_attempts
                print(
                    f"[monitor] inline repair attempt {attempts} failed for {aid} ({anomaly.kind})"
                )
                if attempts >= INLINE_REPAIR_MAX_ATTEMPTS:
                    # Escalate to bug
                    print(
                        f"[monitor] escalating {aid} to bug after "
                        f"{INLINE_REPAIR_MAX_ATTEMPTS} failed inline attempts"
                    )
                    anomaly.repair_tier = "bug"
                    existing.repair_tier = "bug"
                    url = _file_repair_issue(anomaly, repo)
                    if url:
                        existing.gh_issue_url = url
                        try:
                            existing.gh_issue_number = int(url.rstrip("/").split("/")[-1])
                        except ValueError:
                            pass
                        store.write_anomaly_bead(existing)
                        new_urls.append(url)
                        print(f"[monitor] escalated bug issue filed: {url}")

        # --- Bug / Probe tiers ---
        else:
            if existing.gh_issue_number is None:
                url = _file_repair_issue(anomaly, repo)
                if url:
                    existing.gh_issue_url = url
                    try:
                        existing.gh_issue_number = int(url.rstrip("/").split("/")[-1])
                    except ValueError:
                        pass
                    store.write_anomaly_bead(existing)
                    new_urls.append(url)
                    print(
                        f"[monitor] {anomaly.repair_tier} issue filed: {url} "
                        f"({'blocking' if anomaly.is_blocking else 'non-blocking'})"
                    )
                else:
                    print(
                        f"[monitor] WARN: failed to file {anomaly.repair_tier} "
                        f"issue for {anomaly.kind!r}"
                    )

    return new_urls


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_monitor(
    store: BeadStore,
    repo: str,
    *,
    bugs_repo: str | None = None,  # kept for backward compat; ignored
    once: bool = False,
    interval: int = MONITOR_INTERVAL_SECONDS,
    dry_run: bool = False,
) -> None:
    """Run the monitoring loop.

    Args:
        store:     BeadStore for the target repo.
        repo:      ``owner/repo`` string (for GitHub API calls).
        bugs_repo: Deprecated; ignored. Repair issues are now filed in each
                   repo's own ``repairs`` milestone, not a central bugs repo.
        once:      If True, run one pass and return instead of looping.
        interval:  Seconds between detection passes.
        dry_run:   If True, print anomalies but do not write beads or file issues.
    """
    print(f"[monitor] starting for {repo} (interval={interval}s, once={once})")

    while True:
        ts = datetime.now(UTC).isoformat()
        print(f"[monitor] scan at {ts}")

        anomalies = run_all_detectors(store, repo)

        if anomalies:
            new_urls = process_anomalies(anomalies, store, repo, dry_run=dry_run)
            total = len(anomalies)
            new = len(new_urls)
            blocking = sum(1 for a in anomalies if a.is_blocking)
            print(
                f"[monitor] {total} anomaly/ies found "
                f"({blocking} blocking), {new} new issue(s) filed"
            )
        else:
            print("[monitor] clean — no anomalies")

        if once:
            break

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _gh(
    args: list[str], *, repo: str | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    """Thin wrapper around ``gh`` CLI."""
    cmd = ["gh"]
    if repo:
        cmd += ["--repo", repo]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True, check=check)
