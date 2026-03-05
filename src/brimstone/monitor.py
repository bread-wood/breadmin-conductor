"""Brimstone monitor — continuous bead/repo health checks.

Runs a detection loop that compares bead state against GitHub state and flags
aberrations by filing GitHub issues. Designed to run as a long-lived side
process alongside ``brimstone run``, or as a one-shot diagnostic with
``brimstone monitor --once``.

Detector inventory
------------------
check_label_drift       claimed bead <-> in-progress GitHub label mismatch
check_dep_integrity     phantom deps (dep bead missing) + dep cycles
check_state_regressions illegal bead-state transitions in event log
check_orphaned_merge    merge_ready beads absent from the MergeQueue
check_pre_pr_zombies    claimed beads older than timeout with no PRBead
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from brimstone.beads import BeadStore, detect_dep_cycles

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MONITOR_INTERVAL_SECONDS: int = 60
ZOMBIE_TIMEOUT_MINUTES: float = 90.0
MONITOR_FILED_FILENAME: str = "monitor-filed.json"

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
    kind:        Short machine-readable tag (e.g. ``"label_drift"``).
    severity:    ``"warning"`` or ``"critical"``.
    description: One-line human summary.
    details:     Dict of supporting evidence (serialised into the filed issue).
    needs_agent: True when the anomaly is ambiguous and benefits from a
                 Claude agent's analysis before filing.
    """

    kind: str
    severity: str  # "warning" | "critical"
    description: str
    details: dict = field(default_factory=dict)
    needs_agent: bool = False

    def fingerprint(self) -> str:
        """Stable string key used for dedup (kind + primary detail values)."""
        # Sort details to ensure key order doesn't affect fingerprint
        detail_str = json.dumps(self.details, sort_keys=True)
        return f"{self.kind}:{detail_str}"


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def check_label_drift(store: BeadStore, repo: str) -> list[Anomaly]:
    """Detect claimed beads without in-progress label (and vice-versa).

    A bead in state ``claimed`` should have the ``in-progress`` GitHub label.
    A bead NOT in state ``claimed`` should NOT have the label.
    """
    anomalies: list[Anomaly] = []

    # Fetch all issues with in-progress label
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

    # claimed bead but missing in-progress label
    for num in claimed_numbers - in_progress_numbers:
        anomalies.append(
            Anomaly(
                kind="label_drift",
                severity="warning",
                description=f"Issue #{num} has claimed bead but missing in-progress label",
                details={"issue_number": num, "bead_state": "claimed", "has_label": False},
            )
        )

    # in-progress label but no claimed bead (or bead in non-claimed state)
    for num in in_progress_numbers - claimed_numbers:
        bead = store.read_work_bead(num)
        bead_state = bead.state if bead else None
        if bead_state in ("closed", "abandoned", "merge_ready"):
            # These are important to flag
            severity = "critical" if bead_state in ("closed", "abandoned") else "warning"
        else:
            severity = "warning"
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
    """Detect illegal state transitions in event logs (e.g. merge_ready -> open)."""
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
            prev_state = ev.to_state  # noqa: F841 (used for future checks)

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
            continue  # has a PR, not a zombie

        if bead.claimed_at is None:
            continue  # no claim timestamp; can't determine age

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
            # Never let a detector crash the monitor loop
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
# Dedup / filing
# ---------------------------------------------------------------------------


def _load_filed(beads_dir: Path) -> dict[str, str]:
    """Load the monitor-filed.json dedup map {fingerprint: issue_url}."""
    path = beads_dir / MONITOR_FILED_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_filed(beads_dir: Path, filed: dict[str, str]) -> None:
    """Atomically write the monitor-filed.json dedup map."""
    path = beads_dir / MONITOR_FILED_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(filed, indent=2), encoding="utf-8")
    tmp.replace(path)


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
    """File a GitHub issue for the anomaly. Returns the issue URL or None on failure."""
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
    # gh issue create prints the URL on stdout
    url = result.stdout.strip()
    return url if url else None


def process_anomalies(
    anomalies: list[Anomaly],
    store: BeadStore,
    repo: str,
    dry_run: bool = False,
) -> list[str]:
    """Dedup and file GitHub issues for each new anomaly.

    Returns a list of URLs for issues filed this run.
    """
    beads_dir = store._beads_dir
    filed = _load_filed(beads_dir)
    new_urls: list[str] = []

    for anomaly in anomalies:
        fp = anomaly.fingerprint()
        if fp in filed:
            continue  # already filed

        if dry_run:
            desc = anomaly.description
            print(f"[monitor/dry-run] {anomaly.severity.upper()} {anomaly.kind}: {desc}")
            filed[fp] = "dry-run"
            continue

        url = file_anomaly_issue(anomaly, repo)
        if url:
            filed[fp] = url
            new_urls.append(url)
            print(f"[monitor] filed {anomaly.severity} issue: {url}")
        else:
            print(f"[monitor] WARN: failed to file issue for {anomaly.kind!r}")

    if not dry_run:
        _save_filed(beads_dir, filed)
    return new_urls


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_monitor(
    store: BeadStore,
    repo: str,
    *,
    once: bool = False,
    interval: int = MONITOR_INTERVAL_SECONDS,
    dry_run: bool = False,
) -> None:
    """Run the monitoring loop.

    Args:
        store:    BeadStore for the target repo.
        repo:     ``owner/repo`` string (for GitHub API calls).
        once:     If True, run one pass and return instead of looping.
        interval: Seconds between detection passes.
        dry_run:  If True, print anomalies but do not file GitHub issues.
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
            print(f"[monitor] {total} anomaly/ies found, {new} new issue(s) filed")
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
