"""CLI entry points for breadmin-composer.

Entry points:
  impl-worker      → impl_worker()
  research-worker  → research_worker()
  design-worker    → design_worker()
  plan-issues      → plan_issues()
  composer         → composer()
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import date
from pathlib import Path
from typing import Any

import click

from composer import health, logger, runner, session
from composer.config import (
    Config,
    OrchestratorNestingError,
    build_subprocess_env,
    load_config,
)
from composer.health import FatalHealthCheckError
from composer.session import Checkpoint

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRIES: int = 3
MAX_PROMPT_CHARS: int = 16_000
TRIAGE_LABEL: str = "triage"
RESEARCH_LABEL: str = "research"
BACKOFF_SLEEP_SECONDS: int = 30

# ---------------------------------------------------------------------------
# Startup sequence
# ---------------------------------------------------------------------------


def startup_sequence(
    config: Config,
    checkpoint_path: Path,
    milestone: str = "",
    stage: str = "",
    resume_run_id: str | None = None,
) -> tuple[Config, Checkpoint]:
    """Shared startup sequence run by every worker before entering its main loop.

    Steps:
      1. CLAUDECODE nesting guard
      2. Health checks (abort on fatal, print warn and continue)
      3. Load or create checkpoint
      4. Validate resume_run_id if supplied
      5. Acquire orchestrator lock
      6. Log stage_start event
      7. Return (config, checkpoint)

    Args:
        config:           Validated Config instance.
        checkpoint_path:  Path to the checkpoint JSON file.
        milestone:        Active milestone name (forwarded to session.new).
        stage:            Pipeline stage (forwarded to session.new).
        resume_run_id:    If provided, validate the checkpoint run_id matches.

    Returns:
        A (Config, Checkpoint) tuple ready for the worker loop.

    Raises:
        OrchestratorNestingError: If CLAUDECODE=1 is set in the environment.
        FatalHealthCheckError:    If any health check is fatal.
        ValueError:               If resume_run_id is provided and does not
                                  match the checkpoint's run_id.
    """
    # Step 1: Nesting guard (also checked by load_config, but be explicit here)
    if os.environ.get("CLAUDECODE") == "1":
        raise OrchestratorNestingError(
            "Cannot nest orchestrator invocations.\n\n"
            "CLAUDECODE=1 is set in the current environment, which means this process is\n"
            "already running inside a Claude Code session.\n\n"
            "To run the conductor, open a plain terminal (not a Claude Code session) and\n"
            "invoke it from there."
        )

    # Step 2: Health checks
    report = health.check_all(config)
    if report.fatal:
        click.echo(health.format_report(report))
        raise FatalHealthCheckError("Fatal health check failure — see report above.")
    if report.overall == "warn":
        click.echo(health.format_report(report))

    # Step 3: Load or create checkpoint
    chk = session.load(checkpoint_path)
    if chk is None:
        chk = session.new(
            repo=config.github_repo or "",
            default_branch=config.default_branch,
            milestone=milestone,
            stage=stage,
        )

    # Step 4: Resume validation
    if resume_run_id is not None and chk.run_id != resume_run_id:
        raise ValueError(
            f"Checkpoint run_id mismatch: checkpoint has {chk.run_id!r}, "
            f"but --resume specified {resume_run_id!r}."
        )

    # Step 5: Acquire orchestrator lock
    health.acquire_orchestrator_lock(config, chk.run_id)

    # Step 6: Log stage_start event
    logger.log_conductor_event(
        run_id=chk.run_id,
        phase="init",
        event_type="stage_start",
        payload={
            "worker_type": stage,
            "milestone": milestone,
            "stage": stage,
        },
        log_dir=config.log_dir.expanduser(),
    )

    # Step 7: Return
    return config, chk


# ---------------------------------------------------------------------------
# Skill injection
# ---------------------------------------------------------------------------


def inject_skill(skill_name: str, base_prompt: str) -> str:
    """Read skills/<skill_name>.md, apply headless policy, and prepend to base_prompt.

    Args:
        skill_name:  Filename stem without extension (e.g. "research-worker").
        base_prompt: Session-specific context assembled by the caller. Runtime
                     values (repo, milestone, issue number, date) must already
                     be substituted by the caller.

    Returns:
        Full prompt string ready to pass to runner.run() as the -p argument.

    Raises:
        FileNotFoundError: If skills/<skill_name>.md does not exist.
    """
    skill_path = Path(__file__).parent / "skills" / f"{skill_name}.md"
    skill_text = skill_path.read_text(encoding="utf-8")
    skill_text = _apply_headless_policy(skill_text)
    return base_prompt + "\n\n---\n\n" + skill_text


def _apply_headless_policy(text: str) -> str:
    """Replace interactive confirmation gates with auto-resolve directives.

    Applies a best-effort set of string substitutions so that skill prompts
    work correctly in headless ``claude -p`` sessions where no user is present.

    Args:
        text: Raw skill markdown text.

    Returns:
        Text with interactive gates replaced.
    """
    replacements = [
        ("Ask the user", "Auto-resolve:"),
        ("ask the user", "Auto-resolve:"),
        ("confirm with user", "Auto-resolve:"),
        ("Wait for approval", "Proceed automatically:"),
        ("await user confirmation", "Proceed automatically:"),
    ]
    for old, new in replacements:
        text = text.replace(old, new)
    return text


# ---------------------------------------------------------------------------
# UsageGovernor
# ---------------------------------------------------------------------------


class UsageGovernor:
    """Enforces concurrency limits and manages rate-limit backoff for agent dispatch.

    Sits between the issue queue and the dispatch loop. Instantiated once per
    worker invocation. Three dispatch gates are checked in order:

      1. Backoff gate   — if session.is_backing_off() is True, return False.
      2. Concurrency    — active + n > limit, return False.
      3. Budget         — total_cost_usd >= max_budget_usd (when set), return False.

    Concurrency limits by subscription tier:
        pro     -> 2
        max     -> 3
        max20x  -> 5
    """

    def __init__(self, config: Config, checkpoint: Checkpoint) -> None:
        self.config = config
        self.checkpoint = checkpoint
        self._active_agents: int = 0
        self._total_cost_usd: float = 0.0

    def can_dispatch(self, n_agents: int = 1) -> bool:
        """Return True if it is safe to dispatch n_agents new agents right now.

        Checks three gates in order: backoff, concurrency, budget.

        Args:
            n_agents: Number of agents about to be dispatched.

        Returns:
            True if all gates pass; False if any gate blocks.
        """
        # Gate 1: backoff
        if session.is_backing_off(self.checkpoint):
            return False

        # Gate 2: concurrency
        limits: dict[str, int] = {"pro": 2, "max": 3, "max20x": 5}
        limit = self.config.max_concurrency or limits.get(self.config.subscription_tier, 3)
        if self._active_agents + n_agents > limit:
            return False

        # Gate 3: budget (only applies when max_budget_usd is set)
        if self.config.max_budget_usd and self._total_cost_usd >= self.config.max_budget_usd:
            return False

        return True

    def record_dispatch(self, n: int = 1) -> None:
        """Record that n agents have been dispatched."""
        self._active_agents += n

    def record_completion(self, n: int = 1) -> None:
        """Record that n agents have completed."""
        self._active_agents = max(0, self._active_agents - n)

    def record_429(self, attempt: int) -> None:
        """Record a rate-limit (429) response and set exponential backoff.

        Args:
            attempt: Zero-indexed retry attempt number.
        """
        session.set_backoff(
            self.checkpoint,
            attempt,
            self.config.backoff_base_seconds,
            self.config.backoff_max_minutes * 60,
        )

    def record_result(self, run_result: object) -> None:
        """Update running cost total from a RunResult.

        Args:
            run_result: A RunResult instance from runner.run(). Uses the
                        total_cost_usd attribute when present.
        """
        cost = getattr(run_result, "total_cost_usd", None)
        if cost:
            self._total_cost_usd += cost


# ---------------------------------------------------------------------------
# Issue sanitization
# ---------------------------------------------------------------------------


def _sanitize_issue_body(text: str, max_chars: int = MAX_PROMPT_CHARS) -> str:
    """Strip shell metacharacters and truncate an issue body before embedding in a prompt.

    Removes backticks and backslashes; rewrites ``$(`` as ``(`` to defuse command
    substitution syntax. Truncates at *max_chars* with a marker suffix.

    Args:
        text:      Raw issue body text from the GitHub API.
        max_chars: Maximum character count before truncation (default 16,000).

    Returns:
        Sanitized, possibly-truncated string safe to embed in a prompt.
    """
    text = re.sub(r"[`\\]", "", text)
    text = re.sub(r"\$\(", "(", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[TRUNCATED — body exceeded 16,000 characters]"
    return text


# ---------------------------------------------------------------------------
# GitHub helper functions
# ---------------------------------------------------------------------------


def _gh(
    args: list[str], *, repo: str | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a ``gh`` subcommand and return the CompletedProcess result.

    Args:
        args:  Arguments after ``gh`` (e.g. ``["issue", "list", "--json", "number"]``).
        repo:  If provided, prepend ``["--repo", repo]`` before *args*.
        check: If True, raise CalledProcessError on non-zero exit code.

    Returns:
        CompletedProcess with stdout/stderr captured as text.
    """
    cmd = ["gh"]
    if repo:
        cmd += ["--repo", repo]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _list_open_research_issues(repo: str, milestone: str) -> list[dict[str, Any]]:
    """Return open, unassigned, non-in-progress research issues for *milestone*.

    Calls ``gh issue list`` with JSON output and filters client-side:
    - label contains ``research``
    - milestone matches *milestone*
    - no assignee
    - label does NOT contain ``in-progress``

    Args:
        repo:      GitHub repository in ``owner/repo`` format.
        milestone: Milestone name to scope the query.

    Returns:
        List of issue dicts with keys: number, title, body, labels, assignees.
    """
    result = _gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            RESEARCH_LABEL,
            "--milestone",
            milestone,
            "--json",
            "number,title,body,labels,assignees,milestone",
            "--limit",
            "200",
        ],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return []

    try:
        issues: list[dict] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    # Filter out assigned or in-progress issues
    filtered = []
    for issue in issues:
        # Skip if anyone is assigned
        if issue.get("assignees"):
            continue
        # Skip if in-progress label present
        label_names = [lb.get("name", "") for lb in issue.get("labels", [])]
        if "in-progress" in label_names:
            continue
        filtered.append(issue)

    return filtered


def _parse_dependencies(body: str) -> list[int]:
    """Parse ``Depends on: #N`` references from an issue body.

    Matches patterns like ``Depends on: #42`` or ``Depends on: #42, #43``.

    Args:
        body: Raw issue body text.

    Returns:
        List of referenced issue numbers as integers.
    """
    deps: list[int] = []
    for match in re.finditer(r"[Dd]epends\s+on\s*:?\s*((?:#\d+(?:\s*,\s*)?)+)", body):
        for num_match in re.finditer(r"#(\d+)", match.group(1)):
            deps.append(int(num_match.group(1)))
    return deps


def _filter_unblocked(
    issues: list[dict[str, Any]],
    open_issue_numbers: set[int],
) -> list[dict[str, Any]]:
    """Remove issues whose dependencies are still open.

    Args:
        issues:             List of issue dicts (must include ``body`` and ``number``).
        open_issue_numbers: Set of all currently open issue numbers in the milestone.

    Returns:
        Subset of *issues* whose ``Depends on`` references are all closed/absent.
    """
    unblocked = []
    for issue in issues:
        deps = _parse_dependencies(issue.get("body") or "")
        if all(dep not in open_issue_numbers for dep in deps):
            unblocked.append(issue)
    return unblocked


def _sort_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort issues by lowest number (simple stable ordering, highest-impact-first heuristic).

    In the absence of a full downstream-impact graph, issues are sorted by
    number ascending (oldest first), which approximates the "highest impact first"
    heuristic from the spec without requiring a graph traversal.

    Args:
        issues: List of issue dicts with at least a ``number`` key.

    Returns:
        Issues sorted by number ascending.
    """
    return sorted(issues, key=lambda i: i.get("number", 0))


def _claim_issue(repo: str, issue_number: int) -> None:
    """Add ``@me`` assignee and ``in-progress`` label to a GitHub issue.

    Args:
        repo:         Repository in ``owner/repo`` format.
        issue_number: GitHub issue number.
    """
    _gh(
        ["issue", "edit", str(issue_number), "--add-assignee", "@me", "--add-label", "in-progress"],
        repo=repo,
        check=False,
    )


def _unclaim_issue(repo: str, issue_number: int) -> None:
    """Remove ``@me`` assignee and ``in-progress`` label from a GitHub issue.

    Args:
        repo:         Repository in ``owner/repo`` format.
        issue_number: GitHub issue number.
    """
    _gh(
        [
            "issue",
            "edit",
            str(issue_number),
            "--remove-assignee",
            "@me",
            "--remove-label",
            "in-progress",
        ],
        repo=repo,
        check=False,
    )


def _list_triage_issues(repo: str) -> list[dict[str, Any]]:
    """Return all open issues labelled ``triage``.

    Args:
        repo: Repository in ``owner/repo`` format.

    Returns:
        List of issue dicts.
    """
    result = _gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            TRIAGE_LABEL,
            "--json",
            "number,title,body,labels",
            "--limit",
            "200",
        ],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def _close_issue_wont_research(repo: str, issue_number: int, score: int, reason: str) -> None:
    """Close an issue as ``wont-research`` with a triage score comment.

    Args:
        repo:         Repository in ``owner/repo`` format.
        issue_number: GitHub issue number.
        score:        Triage score (0–3).
        reason:       Short explanation for rejection.
    """
    comment = f"Closing: score {score}/3 on research triage rubric. {reason}"
    _gh(
        ["issue", "close", str(issue_number), "--reason", "not planned", "--comment", comment],
        repo=repo,
        check=False,
    )
    _gh(
        [
            "issue",
            "edit",
            str(issue_number),
            "--add-label",
            "wont-research",
            "--remove-label",
            TRIAGE_LABEL,
        ],
        repo=repo,
        check=False,
    )


def _keep_triage_issue(repo: str, issue_number: int, milestone: str) -> None:
    """Keep a triage issue: remove triage label and ensure milestone is set.

    Args:
        repo:         Repository in ``owner/repo`` format.
        issue_number: GitHub issue number.
        milestone:    Active milestone to assign if the issue has none.
    """
    _gh(
        [
            "issue",
            "edit",
            str(issue_number),
            "--remove-label",
            TRIAGE_LABEL,
            "--milestone",
            milestone,
        ],
        repo=repo,
        check=False,
    )


def _find_next_research_milestone(repo: str, current_milestone: str) -> str | None:
    """Find the next research milestone beyond *current_milestone*.

    Queries ``gh milestone list`` and returns the title of the lowest-numbered
    milestone whose title contains ``Research`` (case-insensitive) and whose
    number is greater than the current milestone's number.

    Args:
        repo:               Repository in ``owner/repo`` format.
        current_milestone:  Title of the current research milestone.

    Returns:
        Title of the next research milestone, or ``None`` if none exists.
    """
    result = _gh(
        ["milestone", "list", "--json", "title,number,state", "--limit", "100"],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        milestones: list[dict] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    # Find the number of the current milestone
    current_number: int | None = None
    for ms in milestones:
        if ms.get("title") == current_milestone:
            current_number = ms.get("number")
            break

    if current_number is None:
        return None

    # Find the next open Research milestone
    research_milestones = [
        ms
        for ms in milestones
        if "research" in (ms.get("title") or "").lower()
        and ms.get("state", "").lower() == "open"
        and ms.get("number", 0) > current_number
    ]
    if not research_milestones:
        return None

    research_milestones.sort(key=lambda m: m.get("number", 0))
    return research_milestones[0].get("title")


def _find_impl_milestone(repo: str, research_milestone: str) -> str | None:
    """Find the implementation milestone that corresponds to *research_milestone*.

    Heuristic: look for the open milestone whose title contains ``Impl``
    (case-insensitive) and shares a version token with *research_milestone*.

    Args:
        repo:               Repository in ``owner/repo`` format.
        research_milestone: Title of the research milestone.

    Returns:
        Title of the matching implementation milestone, or ``None``.
    """
    result = _gh(
        ["milestone", "list", "--json", "title,number,state", "--limit", "100"],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        milestones: list[dict] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    # Derive a version token from the research milestone title:
    # e.g. "MVP Research" → "MVP", "v1.1 Research" → "v1.1"
    version_token = research_milestone.lower().replace("research", "").strip()

    for ms in milestones:
        title_lower = (ms.get("title") or "").lower()
        if "impl" in title_lower and version_token in title_lower:
            return ms.get("title")

    return None


def _migrate_issue_to_milestone(repo: str, issue_number: int, milestone: str) -> None:
    """Move an issue to a different milestone.

    Args:
        repo:         Repository in ``owner/repo`` format.
        issue_number: GitHub issue number.
        milestone:    Target milestone title.
    """
    _gh(
        ["issue", "edit", str(issue_number), "--milestone", milestone],
        repo=repo,
        check=False,
    )


def _file_pipeline_issue(
    repo: str,
    next_worker: str,
    milestone: str,
    impl_milestone: str | None = None,
) -> None:
    """Create the next pipeline stage tracking issue.

    Args:
        repo:            Repository in ``owner/repo`` format.
        next_worker:     Name of the next worker stage (e.g. ``"design-worker"``).
        milestone:       Research milestone that just completed (used in the issue title).
        impl_milestone:  Implementation milestone to assign the new issue to (optional).
    """
    title = f"Run {next_worker} for {milestone}"
    cmd = [
        "issue",
        "create",
        "--title",
        title,
        "--label",
        "pipeline",
        "--body",
        f"Pipeline stage transition: {next_worker} is ready to run for {milestone}.",
    ]
    if impl_milestone:
        cmd += ["--milestone", impl_milestone]
    _gh(cmd, repo=repo, check=False)


# ---------------------------------------------------------------------------
# Triage rubric
# ---------------------------------------------------------------------------


def _score_triage_issue(
    issue: dict[str, Any],
    repo: str,
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
) -> int:
    """Score a triage issue against the three-question rubric via a runner.run() call.

    The scoring is done by dispatching a focused Claude call that reads the issue
    body and answers three binary questions. The response is parsed to extract an
    integer score 0–3.

    Args:
        issue:      Issue dict with ``number``, ``title``, and ``body``.
        repo:       Repository in ``owner/repo`` format.
        milestone:  Active milestone name (for context).
        config:     Current Config instance.
        checkpoint: Current Checkpoint instance.
        dry_run:    If True, return a default score of 2 without calling runner.

    Returns:
        Integer score 0–3. Returns 2 on runner failure (conservative: keep).
    """
    if dry_run:
        return 2

    issue_number = issue.get("number", "?")
    issue_title = issue.get("title", "")
    issue_body = _sanitize_issue_body(issue.get("body") or "")

    prompt = (
        f"You are a research triage assistant. Score the following GitHub issue against the "
        f"research triage rubric. Answer each question with YES or NO, then give a total score.\n\n"
        f"Repository: {repo}\n"
        f"Active Milestone: {milestone}\n\n"
        f"## Issue #{issue_number}: {issue_title}\n\n"
        f"{issue_body}\n\n"
        f"## Rubric (score 1 point for each YES)\n\n"
        f"1. **Decision impact**: Would NOT knowing this change an implementation decision "
        f"in the current milestone ({milestone})?\n"
        f"2. **Novelty**: Is this genuinely new, not already covered by an existing open or "
        f"closed issue or merged doc in the repo?\n"
        f"3. **Risk**: Would NOT knowing this create a correctness or security risk?\n\n"
        f"Respond in this exact format:\n"
        f"Q1: YES or NO\n"
        f"Q2: YES or NO\n"
        f"Q3: YES or NO\n"
        f"SCORE: <integer 0-3>\n"
    )

    env = build_subprocess_env(config)
    result = runner.run(
        prompt=prompt,
        allowed_tools=["Bash"],
        env=env,
        max_turns=5,
    )

    if result.is_error:
        # Conservative: keep on runner failure
        return 2

    # Parse score from the result — look in all_events for assistant text
    raw_event = result.raw_result_event or {}
    result_text = raw_event.get("result", "") or ""

    match = re.search(r"SCORE:\s*(\d)", result_text, re.IGNORECASE)
    if match:
        return min(3, max(0, int(match.group(1))))

    # Fallback: count YES answers
    yes_count = len(re.findall(r"Q\d+:\s*YES", result_text, re.IGNORECASE))
    if yes_count > 0:
        return min(3, yes_count)

    # If parsing fails, default to keep (conservative)
    return 2


def _apply_triage_rubric(
    repo: str,
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
) -> None:
    """Fetch all open triage issues and apply the triage rubric to each.

    Issues scoring < 2 are closed with ``wont-research``.
    Issues scoring ≥ 2 have the triage label removed and stay open.

    Args:
        repo:       Repository in ``owner/repo`` format.
        milestone:  Active milestone name (for scoring context).
        config:     Current Config instance.
        checkpoint: Current Checkpoint instance.
        dry_run:    If True, print what would happen without modifying GitHub.
    """
    triage_issues = _list_triage_issues(repo)
    for issue in triage_issues:
        issue_number = issue["number"]
        score = _score_triage_issue(
            issue=issue,
            repo=repo,
            milestone=milestone,
            config=config,
            checkpoint=checkpoint,
            dry_run=dry_run,
        )
        if score < 2:
            if dry_run:
                click.echo(
                    f"  [dry-run] Would close #{issue_number} (score {score}/3) as wont-research"
                )
            else:
                _close_issue_wont_research(
                    repo=repo,
                    issue_number=issue_number,
                    score=score,
                    reason="Below threshold for standalone research.",
                )
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="triage",
                    event_type="issue_claimed",
                    payload={
                        "issue_number": issue_number,
                        "score": score,
                        "action": "closed_wont_research",
                    },
                    log_dir=config.log_dir.expanduser(),
                )
        else:
            if dry_run:
                click.echo(f"  [dry-run] Would keep #{issue_number} (score {score}/3)")
            else:
                _keep_triage_issue(repo=repo, issue_number=issue_number, milestone=milestone)
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="triage",
                    event_type="issue_claimed",
                    payload={
                        "issue_number": issue_number,
                        "score": score,
                        "action": "kept",
                    },
                    log_dir=config.log_dir.expanduser(),
                )


# ---------------------------------------------------------------------------
# Completion gate
# ---------------------------------------------------------------------------


def _classify_blocking_issues(
    open_issues: list[dict[str, Any]],
    repo: str,
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Classify remaining open research issues as blocking or non-blocking.

    An issue is blocking if answering it would change the design of a
    current-milestone implementation issue (not just improve its quality).

    Classification heuristic:
    1. If the issue body contains ``[BLOCKS_IMPL]`` tag → blocking.
    2. Otherwise, call runner.run() to ask Claude to classify.

    Args:
        open_issues: List of open research issue dicts.
        repo:        Repository in ``owner/repo`` format.
        milestone:   Active research milestone name.
        config:      Current Config instance.
        checkpoint:  Current Checkpoint instance.
        dry_run:     If True, treat all issues as non-blocking without calling runner.

    Returns:
        Tuple of (blocking_issues, non_blocking_issues).
    """
    if dry_run:
        return [], open_issues

    blocking: list[dict] = []
    non_blocking: list[dict] = []

    for issue in open_issues:
        body = issue.get("body") or ""
        if "[BLOCKS_IMPL]" in body:
            blocking.append(issue)
            continue

        # Ask Claude to classify
        issue_number = issue.get("number", "?")
        issue_title = issue.get("title", "")
        issue_body = _sanitize_issue_body(body)

        prompt = (
            f"You are a research classification assistant. Determine whether the following "
            f"open research issue is BLOCKING or NON-BLOCKING for the current implementation "
            f"milestone.\n\n"
            f"A research issue is BLOCKING if answering it would change the *design* (not "
            f"just the *quality*) of a current-milestone implementation issue. It is "
            f"NON-BLOCKING if implementation can proceed with a reasonable default and the "
            f"answer would only refine—not redesign—the implementation.\n\n"
            f"Repository: {repo}\n"
            f"Research Milestone: {milestone}\n\n"
            f"## Issue #{issue_number}: {issue_title}\n\n"
            f"{issue_body}\n\n"
            f"Respond with exactly one word on the first line: BLOCKING or NON-BLOCKING\n"
            f"Then a brief (1-2 sentence) reason.\n"
        )

        env = build_subprocess_env(config)
        result = runner.run(
            prompt=prompt,
            allowed_tools=["Bash"],
            env=env,
            max_turns=5,
        )

        if result.is_error:
            # Conservative: treat as non-blocking on runner failure
            non_blocking.append(issue)
            continue

        raw_event = result.raw_result_event or {}
        result_text = (raw_event.get("result") or "").strip().upper()

        if result_text.startswith("BLOCKING"):
            blocking.append(issue)
        else:
            non_blocking.append(issue)

    return blocking, non_blocking


# ---------------------------------------------------------------------------
# Research worker implementation
# ---------------------------------------------------------------------------


def _run_research_worker(
    repo: str,
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
) -> None:
    """Main research-worker loop.

    Processes one active research milestone to completion:
    1. Per-iteration completion gate check.
    2. Backoff check — sleep and retry if rate-limited.
    3. Fetch next unblocked, unassigned research issue.
    4. Sanitize issue body.
    5. Build prompt via inject_skill.
    6. Claim issue (add assignee + in-progress label).
    7. Run runner.run().
    8. Handle result (success → triage; rate-limited → unclaim + backoff; error → retry).

    Args:
        repo:       GitHub repository in ``owner/repo`` format.
        milestone:  Active research milestone name.
        config:     Validated Config instance.
        checkpoint: Active Checkpoint instance.
        dry_run:    If True, print invocations without executing.
    """
    gov = UsageGovernor(config, checkpoint)
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    retry_counts: dict[int, int] = {}

    today = date.today().isoformat()

    while True:
        # ------------------------------------------------------------------
        # STEP 1: Completion gate check (before each dispatch batch)
        # ------------------------------------------------------------------
        open_issues = _list_open_research_issues(repo, milestone)

        if not open_issues:
            # No open issues remain — run the completion gate
            _run_completion_gate(
                repo=repo,
                milestone=milestone,
                open_issues=[],
                config=config,
                checkpoint=checkpoint,
                dry_run=dry_run,
            )
            break

        # Check if any open issues are blocking
        blocking, non_blocking = _classify_blocking_issues(
            open_issues=open_issues,
            repo=repo,
            milestone=milestone,
            config=config,
            checkpoint=checkpoint,
            dry_run=dry_run,
        )

        if not blocking and not non_blocking:
            # No issues at all
            _run_completion_gate(
                repo=repo,
                milestone=milestone,
                open_issues=[],
                config=config,
                checkpoint=checkpoint,
                dry_run=dry_run,
            )
            break

        if not blocking:
            # All remaining issues are non-blocking — research is complete
            _run_completion_gate(
                repo=repo,
                milestone=milestone,
                open_issues=non_blocking,
                config=config,
                checkpoint=checkpoint,
                dry_run=dry_run,
            )
            break

        # ------------------------------------------------------------------
        # STEP 2: Backoff check
        # ------------------------------------------------------------------
        if not gov.can_dispatch():
            logger.log_conductor_event(
                run_id=checkpoint.run_id,
                phase="backoff",
                event_type="backoff_enter",
                payload={"reason": "rate_limit_or_concurrency"},
                log_dir=config.log_dir.expanduser(),
            )
            time.sleep(BACKOFF_SLEEP_SECONDS)
            continue

        # ------------------------------------------------------------------
        # STEP 3: Select next issue from blocking issues (unblocked subset)
        # ------------------------------------------------------------------
        open_issue_numbers = {i.get("number", 0) for i in open_issues}
        unblocked = _filter_unblocked(blocking, open_issue_numbers)

        if not unblocked:
            # Blockers exist but all are blocked by dependencies — wait or sleep
            if gov._active_agents == 0:
                # Nothing is running and nothing is unblocked — unusual; sleep and retry
                time.sleep(BACKOFF_SLEEP_SECONDS)
                continue
            else:
                time.sleep(BACKOFF_SLEEP_SECONDS)
                continue

        ranked = _sort_issues(unblocked)
        issue = ranked[0]
        issue_number: int = issue["number"]

        # ------------------------------------------------------------------
        # STEP 4: Sanitize issue body
        # ------------------------------------------------------------------
        raw_body = issue.get("body") or ""
        body = _sanitize_issue_body(raw_body)

        # ------------------------------------------------------------------
        # STEP 5: Build prompt
        # ------------------------------------------------------------------
        base_prompt = (
            f"## Session Parameters\n"
            f"- Repository: {repo}\n"
            f"- Active Milestone: {milestone}\n"
            f"- Issue: #{issue_number} — {issue.get('title', '')}\n"
            f"- Session Date: {today}\n\n"
            f"{body}"
        )
        prompt = inject_skill("research-worker", base_prompt)

        if dry_run:
            click.echo(
                f"[dry-run] Would dispatch research agent for issue "
                f"#{issue_number}: {issue.get('title', '')}"
            )
            click.echo(f"[dry-run] Prompt length: {len(prompt)} chars")
            # Simulate completion gate with no real work
            _run_completion_gate(
                repo=repo,
                milestone=milestone,
                open_issues=[],
                config=config,
                checkpoint=checkpoint,
                dry_run=dry_run,
            )
            break

        # ------------------------------------------------------------------
        # STEP 6: Claim issue
        # ------------------------------------------------------------------
        _claim_issue(repo=repo, issue_number=issue_number)
        session.record_dispatch(checkpoint, str(issue_number))
        session.save(checkpoint, checkpoint_path)

        logger.log_conductor_event(
            run_id=checkpoint.run_id,
            phase="claim",
            event_type="issue_claimed",
            payload={"issue_number": issue_number, "title": issue.get("title", "")},
            log_dir=config.log_dir.expanduser(),
        )

        gov.record_dispatch(1)

        # ------------------------------------------------------------------
        # STEP 7: Run the research agent
        # ------------------------------------------------------------------
        env = build_subprocess_env(config)
        result = runner.run(
            prompt=prompt,
            allowed_tools=[
                "Bash",
                "Read",
                "Edit",
                "Write",
                "Glob",
                "Grep",
                "WebSearch",
                "WebFetch",
                "mcp__notion__API-post-page",
            ],
            env=env,
            max_turns=100,
        )

        # ------------------------------------------------------------------
        # STEP 8: Handle result
        # ------------------------------------------------------------------
        gov.record_completion(1)
        gov.record_result(result)
        session.save(checkpoint, checkpoint_path)

        logger.log_conductor_event(
            run_id=checkpoint.run_id,
            phase="dispatch",
            event_type="agent_completed",
            payload={
                "issue_number": issue_number,
                "subtype": result.subtype,
                "is_error": result.is_error,
                "error_code": result.error_code,
            },
            log_dir=config.log_dir.expanduser(),
        )

        if result.subtype == "success" and not result.is_error:
            # Success: apply triage rubric to any follow-up issues
            _apply_triage_rubric(
                repo=repo,
                milestone=milestone,
                config=config,
                checkpoint=checkpoint,
            )

        elif result.error_code in ("rate_limit", "extra_usage_exhausted") or (
            result.subtype == "error_max_budget_usd"
        ):
            # Rate-limited or budget exhausted: unclaim and back off
            _unclaim_issue(repo=repo, issue_number=issue_number)
            attempt = retry_counts.get(issue_number, 0)
            gov.record_429(attempt)
            retry_counts[issue_number] = attempt + 1

            logger.log_conductor_event(
                run_id=checkpoint.run_id,
                phase="backoff",
                event_type="backoff_enter",
                payload={
                    "issue_number": issue_number,
                    "reason": result.error_code or result.subtype,
                    "attempt": attempt,
                },
                log_dir=config.log_dir.expanduser(),
            )
            session.save(checkpoint, checkpoint_path)

        elif result.is_error:
            # Other error: increment retry count, unclaim for retry
            current_retries = retry_counts.get(issue_number, 0) + 1
            retry_counts[issue_number] = current_retries
            _unclaim_issue(repo=repo, issue_number=issue_number)

            if current_retries >= MAX_RETRIES:
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="dispatch",
                    event_type="human_escalate",
                    payload={
                        "issue_number": issue_number,
                        "reason": result.subtype or "unknown_error",
                        "error_code": result.error_code,
                        "retry_count": current_retries,
                        "action_required": "manual investigation",
                    },
                    log_dir=config.log_dir.expanduser(),
                )
            # Issue returns to open state for retry in a future iteration

        session.save(checkpoint, checkpoint_path)


def _run_completion_gate(
    repo: str,
    milestone: str,
    open_issues: list[dict[str, Any]],
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
) -> None:
    """Declare research milestone complete and file the next pipeline issue.

    Called when zero blocking issues remain. Migrates all remaining non-blocking
    open issues to the next research milestone, logs stage_complete, and files
    a ``Run design-worker for <milestone>`` pipeline issue.

    Args:
        repo:        Repository in ``owner/repo`` format.
        milestone:   Completed research milestone name.
        open_issues: Remaining non-blocking open issues to migrate.
        config:      Current Config instance.
        checkpoint:  Current Checkpoint instance.
        dry_run:     If True, print actions without modifying GitHub.
    """
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"

    # Migrate non-blocking issues to next research milestone
    next_research_ms = _find_next_research_milestone(repo, milestone)
    for issue in open_issues:
        issue_number = issue["number"]
        if dry_run:
            click.echo(
                f"[dry-run] Would migrate #{issue_number} to "
                f"{next_research_ms or 'next research milestone'}"
            )
        else:
            if next_research_ms:
                _migrate_issue_to_milestone(
                    repo=repo, issue_number=issue_number, milestone=next_research_ms
                )

    # Log stage_complete
    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="complete",
        event_type="stage_complete",
        payload={
            "milestone": milestone,
            "non_blocking_migrated": len(open_issues),
            "next_research_milestone": next_research_ms,
        },
        log_dir=config.log_dir.expanduser(),
    )

    # Find the implementation milestone for the pipeline issue
    impl_milestone = _find_impl_milestone(repo, milestone)

    # File pipeline issue: Run design-worker for <milestone>
    if dry_run:
        click.echo(f"[dry-run] Would file: 'Run design-worker for {milestone}'")
    else:
        _file_pipeline_issue(
            repo=repo,
            next_worker="design-worker",
            milestone=milestone,
            impl_milestone=impl_milestone,
        )

    session.save(checkpoint, checkpoint_path)

    click.echo(
        f"Research milestone '{milestone}' complete. "
        f"Migrated {len(open_issues)} non-blocking issue(s). "
        f"Filed 'Run design-worker for {milestone}' pipeline issue."
    )


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


@click.command("impl-worker")
@click.option("--repo", required=True, help="Target repo in OWNER/REPO format")
@click.option("--milestone", default=None, help="Milestone name to process")
@click.option("--model", default=None, help="Override Claude model")
@click.option("--max-budget", type=float, default=None, help="USD budget cap")
@click.option("--max-turns", type=int, default=None, help="Max turns per invocation")
@click.option("--dry-run", is_flag=True, help="Print invocation without executing")
@click.option("--resume", default=None, help="Resume a previous session by ID")
def impl_worker(
    repo: str,
    milestone: str | None,
    model: str | None,
    max_budget: float | None,
    max_turns: int | None,
    dry_run: bool,
    resume: str | None,
) -> None:
    """Process implementation issues headlessly via claude -p."""
    overrides: dict = {"github_repo": repo}
    if model:
        overrides["model"] = model
    if max_budget is not None:
        overrides["max_budget_usd"] = max_budget
    if max_turns is not None:
        overrides["max_turns"] = max_turns

    config = load_config(**overrides)
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"

    _config, _checkpoint = startup_sequence(
        config=config,
        checkpoint_path=checkpoint_path,
        milestone=milestone or "",
        stage="impl",
        resume_run_id=resume,
    )
    click.echo("Not yet implemented")


@click.command("research-worker")
@click.option("--repo", required=True, help="Target repo in OWNER/REPO format")
@click.option("--milestone", required=True, help="Milestone name to process")
@click.option("--model", default=None, help="Override Claude model")
@click.option("--max-budget", type=float, default=None, help="USD budget cap")
@click.option("--max-turns", type=int, default=None, help="Max turns per invocation")
@click.option("--dry-run", is_flag=True, help="Print invocation without executing")
@click.option("--resume", default=None, help="Resume a previous session by ID")
def research_worker(
    repo: str,
    milestone: str,
    model: str | None,
    max_budget: float | None,
    max_turns: int | None,
    dry_run: bool,
    resume: str | None,
) -> None:
    """Process research issues for a milestone headlessly via claude -p."""
    overrides: dict = {"github_repo": repo}
    if model:
        overrides["model"] = model
    if max_budget is not None:
        overrides["max_budget_usd"] = max_budget
    if max_turns is not None:
        overrides["max_turns"] = max_turns

    config = load_config(**overrides)
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"

    _config, _checkpoint = startup_sequence(
        config=config,
        checkpoint_path=checkpoint_path,
        milestone=milestone,
        stage="research",
        resume_run_id=resume,
    )

    _run_research_worker(
        repo=repo,
        milestone=milestone,
        config=_config,
        checkpoint=_checkpoint,
        dry_run=dry_run,
    )


@click.command("design-worker")
@click.option("--repo", required=True, help="Target repo in OWNER/REPO format")
@click.option(
    "--research-milestone", required=True, help="Completed research milestone to translate"
)
@click.option("--model", default=None, help="Override Claude model")
@click.option("--dry-run", is_flag=True, help="Print planned issues without creating them")
def design_worker(
    repo: str,
    research_milestone: str,
    model: str | None,
    dry_run: bool,
) -> None:
    """Translate research docs into scoped implementation issues."""
    overrides: dict = {"github_repo": repo}
    if model:
        overrides["model"] = model

    config = load_config(**overrides)
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"

    _config, _checkpoint = startup_sequence(
        config=config,
        checkpoint_path=checkpoint_path,
        milestone=research_milestone,
        stage="design",
    )
    click.echo("Not yet implemented")


@click.command("plan-issues")
@click.option("--repo", required=True, help="Target repo in OWNER/REPO format")
@click.option("--model", default=None, help="Override Claude model")
@click.option("--dry-run", is_flag=True, help="Print planned milestones without creating them")
def plan_issues(
    repo: str,
    model: str | None,
    dry_run: bool,
) -> None:
    """Plan milestones and seed research issues for the next version."""
    overrides: dict = {"github_repo": repo}
    if model:
        overrides["model"] = model

    config = load_config(**overrides)
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"

    _config, _checkpoint = startup_sequence(
        config=config,
        checkpoint_path=checkpoint_path,
        milestone="",
        stage="plan-issues",
    )
    click.echo("Not yet implemented")


@click.group()
def composer() -> None:
    """Composer admin commands."""


@composer.command("health")
@click.option("--repo", default=None, help="Repo to check (OWNER/REPO)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def health_cmd(repo: str | None, as_json: bool) -> None:
    """Run preflight checks."""
    config = load_config(**({} if not repo else {"github_repo": repo}))
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    chk = session.load(checkpoint_path)
    report = health.check_all(config, chk)
    if as_json:
        import dataclasses
        import json as _json

        click.echo(_json.dumps(dataclasses.asdict(report), indent=2))
    else:
        click.echo(health.format_report(report))
    raise SystemExit(0 if not report.fatal else 1)


@composer.command("cost")
def cost() -> None:
    """Show cost ledger summary."""
    config = load_config()
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    _chk = session.load(checkpoint_path)
    _config, _checkpoint = startup_sequence(
        config=config,
        checkpoint_path=checkpoint_path,
        milestone="",
        stage="cost",
    )
    click.echo("Not yet implemented")
