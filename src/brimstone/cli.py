"""CLI entry points for brimstone.

Entry point:
  brimstone         → brimstone()

Subcommands:
  brimstone run     — run one or more pipeline stages for a milestone
  brimstone init    — upload spec + seed milestone and research issues
  brimstone adopt   — adopt an existing repo (stub)
  brimstone health  — preflight health checks
  brimstone cost    — cost ledger summary
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import click

from brimstone import health, logger, runner, session
from brimstone.beads import (
    BEAD_SCHEMA_VERSION,
    BeadStore,
    CampaignBead,
    MergeQueueEntry,
    PRBead,
    WorkBead,
    make_bead_store,
)
from brimstone.config import (
    Config,
    build_subprocess_env,
    load_config,
)
from brimstone.health import FatalHealthCheckError
from brimstone.session import Checkpoint

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RETRIES: int = 3
MAX_PROMPT_CHARS: int = 16_000
RESEARCH_LABEL: str = "stage/research"
DESIGN_LABEL: str = "stage/design"
IMPL_LABEL: str = "stage/impl"
BACKOFF_SLEEP_SECONDS: int = 30
STALL_MAX_ITERATIONS: int = 5  # 5 × BACKOFF_SLEEP_SECONDS = 2.5 min before escalation
WATCHDOG_INTERVAL: int = 5  # run watchdog scan every N pool iterations
WATCHDOG_TIMEOUT_MINUTES: float = 45.0
WATCHDOG_MAX_FIX_ATTEMPTS: int = 3


def _auth_mode(config: Config) -> str:
    """Return 'api_key' if an Anthropic API key is configured, else 'subscription'."""
    return "api_key" if config.anthropic_api_key else "subscription"


# ---------------------------------------------------------------------------
# Startup sequence
# ---------------------------------------------------------------------------


def startup_sequence(
    config: Config,
    checkpoint_path: Path,
    milestone: str = "",
    stage: str = "",
    resume_run_id: str | None = None,
    skip_checks: frozenset[str] = frozenset(),
) -> tuple[Config, Checkpoint, BeadStore]:
    """Shared startup sequence run by every worker before entering its main loop.

    Steps:
      1. Health checks (abort on fatal, print warn and continue)
      2. Load or create checkpoint
      3. Validate resume_run_id if supplied
      4. Acquire orchestrator lock
      5. Log stage_start event
      6. Return (config, checkpoint)

    Args:
        config:           Validated Config instance.
        checkpoint_path:  Path to the checkpoint JSON file.
        milestone:        Active milestone name (forwarded to session.new).
        stage:            Pipeline stage (forwarded to session.new).
        resume_run_id:    If provided, validate the checkpoint run_id matches.
        skip_checks:      Health check names to skip (for headless commands that
                          target a remote repo and don't require a local git cwd).

    Returns:
        A (Config, Checkpoint, BeadStore) tuple ready for the worker loop.

    Raises:
        FatalHealthCheckError: If any health check is fatal.
        ValueError:               If resume_run_id is provided and does not
                                  match the checkpoint's run_id.
    """
    # Step 1: Health checks (formerly Step 2 — nesting guard removed; brimstone
    # may safely run inside Claude Code because build_subprocess_env constructs
    # sub-agent envs from scratch and never forwards CLAUDECODE).
    report = health.check_all(config, skip_checks=skip_checks)
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

    # Step 7a: Create BeadStore
    store = make_bead_store(config, config.github_repo or "")

    # Step 7: Return
    return config, chk, store


# ---------------------------------------------------------------------------
# Skill injection
# ---------------------------------------------------------------------------


def write_skill_tmp(skill_name: str) -> Path:
    """Write skills/<skill_name>.md to a named temp file and return its path.

    The caller is responsible for deleting the file after use.

    Skill content is passed via ``--append-system-prompt`` rather than
    concatenated into the ``-p`` argument, because passing large prompts
    via ``-p`` causes Claude Code to exit silently before emitting any
    stream-json events.

    Args:
        skill_name: Filename stem without extension (e.g. "research-worker").

    Returns:
        Path to the written temp file.

    Raises:
        FileNotFoundError: If skills/<skill_name>.md does not exist.
    """
    skill_path = Path(__file__).parent / "skills" / f"{skill_name}.md"
    skill_text = _apply_headless_policy(skill_path.read_text(encoding="utf-8"))
    fd, tmp_path = tempfile.mkstemp(suffix=f"-{skill_name}.md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(skill_text)
    except Exception:
        os.unlink(tmp_path)
        raise
    return Path(tmp_path)


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


def _run_agent(
    prompt: str,
    skill_name: str,
    allowed_tools: list[str],
    max_turns: int,
    log_label: str,
    prefix: str,
    config: Config,
    issue_number: int | None = None,
    model: str | None = None,
) -> runner.RunResult:
    """Run a single headless agent and log its transcript.

    Handles skill file lifecycle, env construction, runner.run() invocation,
    and transcript logging. The caller is responsible only for building the
    prompt and interpreting the result.

    Always creates an isolated CLAUDE_CONFIG_DIR so the global ~/.claude/CLAUDE.md
    (which contains orchestrator instructions) is not loaded into sub-agents.

    Args:
        issue_number: When provided, bakes the issue number into CLAUDE_CONFIG_DIR
                      for a unique per-agent path that aids log correlation.
    """
    skill_tmp = write_skill_tmp(skill_name)
    key = issue_number if issue_number is not None else "agent"
    config_dir = f"/tmp/brimstone-agent-{key}-{uuid.uuid4().hex}"
    extra: dict[str, str] = {"CLAUDE_CONFIG_DIR": config_dir}
    env = build_subprocess_env(config, extra=extra if extra else None)
    silent_header = (
        "SILENT MODE: You are running in a fully automated headless pipeline. "
        "Minimize text output. Use tools directly. "
        "Do NOT narrate, explain, summarize, or produce any text between tool calls "
        "unless the task explicitly requires written output (e.g. a document or issue body). "
        "Every word you output costs money.\n\n"
    )
    try:
        result = runner.run(
            prompt=silent_header + prompt,
            allowed_tools=allowed_tools,
            append_system_prompt_file=skill_tmp,
            env=env,
            max_turns=max_turns,
            timeout_seconds=config.agent_timeout_minutes * 60,
            model=model or config.model,
            prefix=prefix,
            disallowed_tools=runner.TOOLS_DISALLOWED,
            max_budget_usd=config.max_budget_usd,
            fallback_model=runner.FALLBACK_MODEL,
        )
    finally:
        skill_tmp.unlink(missing_ok=True)
        if config_dir:
            shutil.rmtree(config_dir, ignore_errors=True)
    logger.log_agent_transcript(
        result.all_events,
        log_label,
        session_id=(result.raw_result_event or {}).get("session_id"),
        log_dir=config.log_dir.expanduser(),
    )
    return result


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


def _is_git_repo(path: str) -> bool:
    """Return True if *path* is inside a git repository.

    Runs ``git -C <path> rev-parse --git-dir`` and checks the exit code.

    Args:
        path: Filesystem path to test.

    Returns:
        True if the path is inside a git repo; False otherwise.
    """
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _ensure_remote(repo_path: str, name: str) -> None:
    """Ensure *repo_path* has a git remote named ``origin`` pointing at the GitHub repo.

    If a remote already exists, this is a no-op.  If no remote is configured,
    the SSH URL is fetched via ``gh repo view`` and added with ``git remote add``.

    Args:
        repo_path: Absolute path to a local git repository.
        name: GitHub repository name (no slashes) used to look up the remote URL.

    Raises:
        click.ClickException: If ``gh repo view`` or ``git remote add`` fails.
    """
    remote_check = subprocess.run(
        ["git", "-C", repo_path, "remote", "-v"],
        capture_output=True,
        text=True,
    )
    if remote_check.stdout.strip():
        # Remote already configured — nothing to do.
        return

    view = subprocess.run(
        ["gh", "repo", "view", name, "--json", "sshUrl", "--jq", ".sshUrl"],
        capture_output=True,
        text=True,
    )
    if view.returncode != 0:
        raise click.ClickException(f"gh repo view failed for '{name}':\n{view.stderr}")
    ssh_url = view.stdout.strip()

    add_remote = subprocess.run(
        ["git", "-C", repo_path, "remote", "add", "origin", ssh_url],
        capture_output=True,
        text=True,
    )
    if add_remote.returncode != 0:
        raise click.ClickException(f"git remote add failed for '{repo_path}':\n{add_remote.stderr}")


def _resolve_repo(repo_arg: str | None) -> str:
    """Resolve the ``--repo`` argument to an ``owner/name`` string.

    Resolution rules
    ----------------
    1. ``repo_arg`` is ``None`` (no ``--repo`` flag)
       → infer ``owner/name`` from cwd git remote; raise ClickException if
         cwd is not a git repo or has no GitHub remote.

    2. ``repo_arg`` matches ``owner/name``
       → use as-is.

    3. ``repo_arg`` is a bare name (no ``/``)
       → resolve via ``gh repo view <name>``; raise ClickException if not found.

    4. ``repo_arg`` looks like a local path (starts with ``.`` or ``/``, or
       contains ``os.sep``)
       → raise ClickException — local paths are not accepted.

    Args:
        repo_arg: Raw value of the ``--repo`` CLI option, or ``None``.

    Returns:
        An ``owner/name`` string for use with ``gh --repo``.

    Raises:
        click.ClickException: On validation failure or lookup error.
    """
    # -----------------------------------------------------------------------
    # Case 1: No --repo flag → infer from cwd git remote
    # -----------------------------------------------------------------------
    if repo_arg is None:
        cwd = os.getcwd()
        if not _is_git_repo(cwd):
            raise click.ClickException(
                "current directory is not a git repository.\n"
                "Run from inside a git repo, or pass --repo <owner/name>."
            )
        repo_ref = _infer_github_repo_from_path(cwd)
        if not repo_ref:
            raise click.ClickException(
                "Could not infer GitHub repo from current directory remote.\n"
                "Pass --repo <owner/name> explicitly."
            )
        return repo_ref

    # -----------------------------------------------------------------------
    # Case 2: Local path guard — reject before slug check
    # -----------------------------------------------------------------------
    if repo_arg.startswith(".") or repo_arg.startswith("/") or "\\" in repo_arg:
        raise click.ClickException(f"Use 'owner/name' format, not a local path. Got: {repo_arg!r}")

    # -----------------------------------------------------------------------
    # Case 3: Looks like "owner/name"
    # -----------------------------------------------------------------------
    _github_slug_re = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
    if _github_slug_re.match(repo_arg):
        return repo_arg

    # -----------------------------------------------------------------------
    # Case 4: Bare name (no slash) — look up on GitHub
    # -----------------------------------------------------------------------
    view_result = subprocess.run(
        ["gh", "repo", "view", repo_arg, "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    if view_result.returncode == 0 and view_result.stdout.strip():
        return view_result.stdout.strip()

    raise click.ClickException(f"Repo '{repo_arg}' not found on GitHub. Use 'owner/name' format.")


def _infer_github_repo_from_path(path: str) -> str | None:
    """Infer the GitHub ``owner/name`` from the git remote URL at *path*.

    Checks ``origin`` first, then falls back to the first remote found.
    Handles both HTTPS (``https://github.com/owner/name.git``) and SSH
    (``git@github.com:owner/name.git``) remote URL formats.

    Args:
        path: Absolute path to a git repository.

    Returns:
        ``owner/name`` string, or ``None`` if no GitHub remote is found.
    """
    result = subprocess.run(
        ["git", "-C", path, "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # Try listing all remotes
        list_result = subprocess.run(
            ["git", "-C", path, "remote"],
            capture_output=True,
            text=True,
        )
        if list_result.returncode != 0 or not list_result.stdout.strip():
            return None
        first_remote = list_result.stdout.strip().splitlines()[0]
        result = subprocess.run(
            ["git", "-C", path, "remote", "get-url", first_remote],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None

    url = result.stdout.strip()
    return _parse_github_owner_name(url)


def _parse_github_owner_name(url: str) -> str | None:
    """Extract ``owner/name`` from a GitHub remote URL.

    Supports:
    - ``https://github.com/owner/name.git``
    - ``https://github.com/owner/name``
    - ``git@github.com:owner/name.git``
    - ``git@github.com:owner/name``

    Args:
        url: Remote URL string.

    Returns:
        ``owner/name`` without ``.git`` suffix, or ``None`` if not a GitHub URL.
    """
    import re as _re

    # HTTPS: https://github.com/owner/name[.git]
    https_match = _re.match(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?$", url)
    if https_match:
        return https_match.group(1)

    # SSH: git@github.com:owner/name[.git]
    ssh_match = _re.match(r"git@github\.com:([^/]+/[^/]+?)(?:\.git)?$", url)
    if ssh_match:
        return ssh_match.group(1)

    return None


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


def _resume_open_prs(
    repo: str,
    milestone: str,
    label: str,
    log_prefix: str,
    config: Config,
    checkpoint: Checkpoint,
    default_branch: str = "main",
    repo_root: str = "",
    already_handled: set[int] | None = None,
    store: BeadStore | None = None,
) -> None:
    """Monitor any open PRs for the milestone whose issue is not already in-progress.

    Complements ``_resume_stale_issues``: that function finds issues by their
    ``in-progress`` label; this function finds open PRs directly and monitors
    any that were missed (e.g. the in-progress label was stripped, or the
    issue was closed but the PR is still open/conflicting).

    ``already_handled`` is a set of issue numbers already handled by
    ``_resume_stale_issues`` so we don't double-monitor.
    """
    if already_handled is None:
        already_handled = set()

    # Fetch all open PRs for the repo
    result = _gh(
        ["pr", "list", "--state", "open", "--json", "number,headRefName", "--limit", "100"],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return
    try:
        open_prs: list[dict] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return

    # Fetch all impl issues for the milestone (open + closed) to build a number set
    issues_result = _gh(
        [
            "issue",
            "list",
            "--state",
            "all",
            "--label",
            label,
            "--milestone",
            milestone,
            "--json",
            "number",
            "--limit",
            "200",
        ],
        repo=repo,
        check=False,
    )
    try:
        milestone_issue_numbers: set[int] = {
            i["number"] for i in json.loads(issues_result.stdout or "[]")
        }
    except (json.JSONDecodeError, KeyError):
        milestone_issue_numbers = set()

    for pr in open_prs:
        branch: str = pr.get("headRefName") or ""
        pr_number: int = int(pr["number"])
        # Branch must start with "<issue_number>-"
        dash = branch.find("-")
        if dash <= 0:
            continue
        try:
            issue_number = int(branch[:dash])
        except ValueError:
            continue
        if issue_number not in milestone_issue_numbers:
            continue
        if issue_number in already_handled:
            continue
        click.echo(
            f"{log_prefix} Resuming untracked open PR #{pr_number} for issue #{issue_number}",
            err=True,
        )
        wt_path = ""
        if repo_root:
            wt_path = _checkout_existing_branch_worktree(branch, repo_root) or ""
        try:
            _monitor_pr(
                pr_number=pr_number,
                branch=branch,
                repo=repo,
                config=config,
                checkpoint=checkpoint,
                issue_number=issue_number,
                store=store,
                worktree_path=wt_path,
                default_branch=default_branch,
            )
        finally:
            if wt_path:
                _remove_worktree(wt_path, repo_root)
        # Drain the merge queue after each PR so queued merges land before the
        # next PR's rebase (which would otherwise conflict with them).
        if store is not None and repo_root:
            _process_merge_queue(
                repo=repo,
                config=config,
                checkpoint=checkpoint,
                store=store,
                default_branch=default_branch,
                repo_root=repo_root,
            )


def _resume_stale_issues(
    repo: str,
    milestone: str,
    label: str,
    log_prefix: str,
    config: Config,
    checkpoint: Checkpoint,
    default_branch: str = "main",
    repo_root: str = "",
    store: BeadStore | None = None,
) -> set[int]:
    """Resume or unclaim in-progress issues from a crashed previous session.

    For each in-progress issue in *milestone* with *label*:
    - If an open PR exists: monitor it to completion.
    - If a merged PR exists (orchestrator crashed after merge): close the issue.
    - Otherwise: unclaim so it can be re-dispatched cleanly.

    When *repo_root* is provided, a temporary worktree is created for the
    stale branch so that conflict detection and rebase work correctly.

    Returns the set of issue numbers handled, for use by ``_resume_open_prs``
    to avoid double-monitoring.

    Called at the start of research-worker, design-worker, and impl-worker.
    """
    handled: set[int] = set()
    if store is None:
        return handled
    _LABEL_TO_STAGE = {
        "stage/research": "research",
        "stage/impl": "impl",
        "stage/design": "design",
    }
    stage_name = _LABEL_TO_STAGE.get(label, "")
    claimed_beads = [
        b
        for b in store.list_work_beads(state="claimed")
        if b.milestone == milestone and (not stage_name or b.stage == stage_name)
    ]
    stale_iter: list[int] = [b.issue_number for b in claimed_beads]
    for stale_number in stale_iter:
        found = _find_pr_for_issue(repo, stale_number)
        if found is not None:
            pr_number, stale_branch = found
            click.echo(
                f"{log_prefix} Resuming: monitoring PR #{pr_number} for issue #{stale_number}",
                err=True,
            )
            wt_path = ""
            if repo_root:
                wt_path = _checkout_existing_branch_worktree(stale_branch, repo_root) or ""
            try:
                _monitor_pr(
                    pr_number=pr_number,
                    branch=stale_branch,
                    repo=repo,
                    config=config,
                    checkpoint=checkpoint,
                    issue_number=stale_number,
                    store=store,
                    worktree_path=wt_path,
                    default_branch=default_branch,
                )
            finally:
                if wt_path:
                    _remove_worktree(wt_path, repo_root)
            handled.add(stale_number)
        elif _pr_merged_for_issue(repo, stale_number):
            _gh(["issue", "close", str(stale_number)], repo=repo, check=False)
            click.echo(
                f"{log_prefix} Closed #{stale_number} — merged PR found, issue was not auto-closed",
                err=True,
            )
            handled.add(stale_number)
        else:
            _unclaim_issue(repo=repo, issue_number=stale_number, store=store)
            click.echo(
                f"{log_prefix} Unclaimed stale #{stale_number} (no open or merged PR found)",
                err=True,
            )
            handled.add(stale_number)
    return handled


def _log_agent_cost(
    result: runner.RunResult,
    repo: str,
    stage: str,
    config: Config,
    checkpoint: Checkpoint,
    issue_number: int | None = None,
    milestone: str | None = None,
    model: str | None = None,
) -> None:
    """Log agent cost to the cost ledger."""
    logger.log_cost(
        result.raw_result_event or {},
        logger.LogContext(
            session_id=str(uuid.uuid4()),
            run_id=checkpoint.run_id,
            repo=repo,
            stage=stage,
            issue_number=issue_number,
            milestone=milestone,
        ),
        log_dir=config.log_dir.expanduser(),
        model=model or config.model,
        auth_mode=_auth_mode(config),
    )


def _list_open_issues_by_label(repo: str, milestone: str, label: str) -> list[dict[str, Any]]:
    """Return open, unassigned, non-in-progress issues for a given stage label."""
    result = _gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            label,
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
    return [
        issue
        for issue in issues
        if not issue.get("assignees")
        and "in-progress" not in {lb.get("name", "") for lb in issue.get("labels", [])}
    ]


def _list_all_open_issues_by_label(repo: str, milestone: str, label: str) -> list[dict[str, Any]]:
    """Return ALL open issues for a stage label — including in-progress ones.

    Unlike ``_list_open_issues_by_label``, this does NOT filter by assignee or
    ``in-progress`` label. Used by progress gates that need to block until every
    issue of a stage is actually closed (merged), not just until they've been
    claimed and dispatched.
    """
    result = _gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            label,
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
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return []


def _seed_work_beads(
    repo: str,
    milestone: str,
    label: str,
    stage: str,
    store: BeadStore,
) -> None:
    """Sync GitHub issues for *milestone*/*label* into the bead store.

    Queries GitHub for all open issues (including in-progress) matching the
    milestone and label, then creates a ``WorkBead(state="open")`` for any
    issue that does not already have a bead. Existing beads are never
    overwritten.

    This is the only place GitHub is queried for issue existence. All
    subsequent dispatch decisions read from the bead store.

    Also picks up followup issues created by agents mid-run (e.g. a research
    agent spinning off a new research task): the next ``_seed_work_beads``
    call will create a bead for it and make it eligible for dispatch.
    """
    result = _gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            label,
            "--milestone",
            milestone,
            "--json",
            "number,title,body,labels,milestone",
            "--limit",
            "500",
        ],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return
    try:
        issues: list[dict] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return
    for issue in issues:
        issue_number = issue.get("number")
        if issue_number is None:
            continue
        if store.read_work_bead(issue_number) is not None:
            continue  # bead already exists — never overwrite
        body = issue.get("body") or ""
        bead = WorkBead(
            v=BEAD_SCHEMA_VERSION,
            issue_number=issue_number,
            title=issue.get("title", ""),
            milestone=milestone,
            stage=stage,
            module=_extract_module(issue),
            priority=_extract_priority(issue),
            state="open",
            branch="",
            blocked_by=_parse_dependencies(body),
        )
        store.write_work_bead(bead)


def _count_open_issues_by_label(repo: str, milestone: str, label: str) -> int:
    """Return count of all open issues (including in-progress) for a stage label."""
    result = _gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            label,
            "--milestone",
            milestone,
            "--json",
            "number",
            "--limit",
            "200",
        ],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return 0
    try:
        return len(json.loads(result.stdout))
    except json.JSONDecodeError:
        return 0


def _count_all_issues_by_label(repo: str, milestone: str, label: str) -> int:
    """Return count of ALL issues (open + closed) for a stage label.

    Used by the scope completion check so that scope is not re-run once all
    impl issues have been closed by agents.
    """
    result = _gh(
        [
            "issue",
            "list",
            "--state",
            "all",
            "--label",
            label,
            "--milestone",
            milestone,
            "--json",
            "number",
            "--limit",
            "200",
        ],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return 0
    try:
        return len(json.loads(result.stdout))
    except json.JSONDecodeError:
        return 0


def _file_design_issue_if_missing(
    repo: str,
    milestone: str,
    title: str,
    body: str,
) -> None:
    """Create a ``stage/design`` issue with *title* in *repo* if it doesn't already exist.

    Fetches all open+closed issues and checks for an exact title match before
    creating, making this call idempotent on re-run.
    """
    result = _gh(
        ["issue", "list", "--state", "all", "--limit", "500", "--json", "title"],
        repo=repo,
        check=False,
    )
    if result.returncode == 0:
        try:
            existing = {i["title"] for i in json.loads(result.stdout)}
            if title in existing:
                return
        except (json.JSONDecodeError, KeyError):
            pass

    _gh(
        [
            "issue",
            "create",
            "--title",
            title,
            "--label",
            DESIGN_LABEL,
            "--milestone",
            milestone,
            "--body",
            body,
        ],
        repo=repo,
        check=False,
    )


def _doc_exists_on_default_branch(repo: str, path: str, default_branch: str) -> bool:
    """Return True if *path* exists on *default_branch* in *repo*.

    Uses the GitHub Contents API so no local checkout is required.
    """
    result = _gh(
        ["api", f"repos/{repo}/contents/{path}?ref={default_branch}"],
        check=False,
    )
    return result.returncode == 0


def _get_default_branch_for_repo(repo: str) -> str:
    """Return the default branch name for *repo* (falls back to ``"main"``)."""
    # `gh repo view` takes the repo as a positional arg, not via --repo flag.
    result = _gh(
        ["repo", "view", repo, "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "main"


def _extract_module_from_design_issue(issue: dict[str, Any]) -> str:
    """Extract the module name from a ``Design: LLD for <module>`` issue title.

    Falls back to a slugified title if the expected pattern is not found.
    """
    title = issue.get("title", "")
    match = re.search(r"[Dd]esign:\s*LLD\s+for\s+(.+)$", title)
    if match:
        return match.group(1).strip()
    return _slugify(title)


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
    store: BeadStore | None = None,
) -> list[dict[str, Any]]:
    """Remove issues whose dependencies are still open.

    Uses bead store when available (dependencies parsed once at claim time).
    Falls back to parsing the issue body when no bead exists.

    Args:
        issues:             List of issue dicts (must include ``body`` and ``number``).
        open_issue_numbers: Set of all currently open issue numbers in the milestone.
        store:              Active BeadStore, or None.

    Returns:
        Subset of *issues* whose dependencies are all closed/absent.
    """
    unblocked = []
    for issue in issues:
        issue_number = issue.get("number", 0)
        if store is not None:
            bead = store.read_work_bead(issue_number)
            if bead is not None:
                blocked = any(
                    (dep_bead := store.read_work_bead(dep)) is None or dep_bead.state != "closed"
                    for dep in bead.blocked_by
                )
                if not blocked:
                    unblocked.append(issue)
                continue
        # Fallback: parse from issue body
        deps = _parse_dependencies(issue.get("body") or "")
        if all(dep not in open_issue_numbers for dep in deps):
            unblocked.append(issue)
    return unblocked


_PRIORITY_ORDER: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}


def _issue_priority(issue: dict[str, Any]) -> int:
    """Return numeric priority for *issue* based on its P0–P4 label (lower = higher priority).

    Issues with no priority label sort after P4 (value 5).
    """
    for lb in issue.get("labels", []):
        name = lb.get("name", "") if isinstance(lb, dict) else str(lb)
        if name in _PRIORITY_ORDER:
            return _PRIORITY_ORDER[name]
    return 5


def _sort_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort issues by priority label then issue number.

    Primary key: P0 < P1 < P2 < P3 < P4 < unlabelled.
    Tiebreaker: issue number ascending (oldest first).

    Args:
        issues: List of issue dicts with at least a ``number`` key.

    Returns:
        Issues sorted by (priority, number).
    """
    return sorted(issues, key=lambda i: (_issue_priority(i), i.get("number", 0)))


def _detect_dependency_cycles(issues: list[dict[str, Any]]) -> list[list[int]]:
    """Detect cycles in the dependency graph formed by open issues.

    Only considers edges between issues that are in the provided list (i.e. open
    in the current milestone). Dependencies on closed/external issues are ignored.

    Args:
        issues: List of open issue dicts with ``number`` and ``body`` fields.

    Returns:
        List of cycles, each cycle being a list of issue numbers forming the loop.
        Empty list if no cycles exist.
    """
    open_numbers = {i["number"] for i in issues}
    graph: dict[int, list[int]] = {}
    for issue in issues:
        num = issue["number"]
        deps = [d for d in _parse_dependencies(issue.get("body") or "") if d in open_numbers]
        graph[num] = deps

    # Iterative DFS cycle detection
    cycles: list[list[int]] = []
    visited: set[int] = set()
    rec_stack: set[int] = set()
    path: list[int] = []

    def dfs(node: int) -> bool:
        visited.add(node)
        rec_stack.add(node)
        path.append(node)
        for neighbour in graph.get(node, []):
            if neighbour not in visited:
                if dfs(neighbour):
                    return True
            elif neighbour in rec_stack:
                # Found a cycle — extract the loop portion of the path
                cycle_start = path.index(neighbour)
                cycles.append(path[cycle_start:] + [neighbour])
                return True
        path.pop()
        rec_stack.discard(node)
        return False

    for node in list(graph):
        if node not in visited:
            dfs(node)

    return cycles


def _validate_dependency_refs(issues: list[dict[str, Any]], repo: str) -> None:
    """Warn about ``Depends on: #N`` references that don't exist on GitHub.

    Makes one API call per unresolved dependency (only called for deps absent
    from the current open-issue set, so this is rare in practice).

    Args:
        issues: List of open issue dicts (current milestone).
        repo:   Repository in ``owner/repo`` format.
    """
    open_numbers = {i["number"] for i in issues}
    all_referenced: set[int] = set()
    for issue in issues:
        all_referenced.update(_parse_dependencies(issue.get("body") or ""))

    unknown = all_referenced - open_numbers
    for dep in sorted(unknown):
        result = _gh(["issue", "view", str(dep), "--json", "number"], repo=repo, check=False)
        if result.returncode != 0:
            click.echo(
                f"[dep-check] Warning: issue #{dep} referenced as a dependency does not "
                f"exist on GitHub. The depending issue will be treated as unblocked.",
                err=True,
            )


def _startup_dep_checks(open_issues: list[dict[str, Any]], repo: str) -> None:
    """Run dependency validation and cycle detection at worker startup.

    Emits warnings for missing dependency references and errors out on cycles.
    Called once per worker run (not per fill cycle) since issue bodies don't change.

    Args:
        open_issues: All currently open issues for the milestone.
        repo:        Repository in ``owner/repo`` format.
    """
    _validate_dependency_refs(open_issues, repo)
    cycles = _detect_dependency_cycles(open_issues)
    if cycles:
        for cycle in cycles:
            nums = " → ".join(f"#{n}" for n in cycle)
            click.echo(f"[dep-check] Error: dependency cycle detected: {nums}", err=True)
        click.echo(
            "[dep-check] Resolve the cycle(s) above before dispatching. "
            "Break a cycle by adding [DEFERRED] to one issue's body or closing it.",
            err=True,
        )
        raise SystemExit(1)


def _claim_issue(
    repo: str,
    issue_number: int,
    issue: dict | None = None,
    branch: str = "",
    store: BeadStore | None = None,
) -> None:
    """Add the bot assignee and ``in-progress`` label to a GitHub issue.

    Args:
        repo:         Repository in ``owner/repo`` format.
        issue_number: GitHub issue number.
        issue:        Full issue dict (from GitHub API).  Required when *store* is set.
        branch:       Branch name created for this issue.  Required when *store* is set.
        store:        Active BeadStore instance.  When set, writes a WorkBead before
                      the GitHub API call.
    """
    if store is not None:
        existing = store.read_work_bead(issue_number)
        if existing is not None:
            # Bead was seeded — update in place
            existing.state = "claimed"
            existing.branch = branch
            existing.claimed_at = datetime.now(UTC).isoformat()
            store.write_work_bead(existing)
        elif issue is not None:
            # No seed bead — create from issue dict (fallback for unseeded stages)
            milestone_title = (issue.get("milestone") or {}).get("title", "")
            body = issue.get("body") or ""
            bead = WorkBead(
                v=BEAD_SCHEMA_VERSION,
                issue_number=issue_number,
                title=issue.get("title", ""),
                milestone=milestone_title,
                stage=_extract_stage(issue),
                module=_extract_module(issue),
                priority=_extract_priority(issue),
                state="claimed",
                branch=branch,
                retry_count=0,
                blocked_by=_parse_dependencies(body),
                claimed_at=datetime.now(UTC).isoformat(),
            )
            store.write_work_bead(bead)
        store.flush(f"brimstone: claim #{issue_number}")
    _gh(
        [
            "issue",
            "edit",
            str(issue_number),
            "--add-assignee",
            _BRIMSTONE_BOT,
            "--add-label",
            "in-progress",
        ],
        repo=repo,
        check=False,
    )


def _unclaim_issue(repo: str, issue_number: int, store: BeadStore | None = None) -> None:
    """Remove all assignees and the ``in-progress`` label from a GitHub issue.

    Fetches the current assignees so that legacy assignments (e.g. from a run
    before the bot account was configured) are also cleared.

    Args:
        repo:         Repository in ``owner/repo`` format.
        issue_number: GitHub issue number.
        store:        Active BeadStore instance.  When set, updates the WorkBead
                      state to ``"open"`` before the GitHub API call.
    """
    if store is not None:
        bead = store.read_work_bead(issue_number)
        if bead is not None and bead.state not in ("abandoned", "closed"):
            bead.state = "open"
            store.write_work_bead(bead)
    info = _gh(
        ["issue", "view", str(issue_number), "--json", "assignees"],
        repo=repo,
        check=False,
    )
    try:
        assignees = [a["login"] for a in json.loads(info.stdout).get("assignees", [])]
    except (json.JSONDecodeError, KeyError):
        assignees = [_BRIMSTONE_BOT]
    if not assignees:
        assignees = [_BRIMSTONE_BOT]

    args = ["issue", "edit", str(issue_number), "--remove-label", "in-progress"]
    for login in assignees:
        args += ["--remove-assignee", login]
    _gh(args, repo=repo, check=False)


def _exhaust_issue(
    repo: str, issue_number: int, reason: str, store: BeadStore | None = None
) -> None:
    """Mark an issue as permanently exhausted after all retries are spent.

    Unclaims the issue, adds the ``bug`` label, leaves a comment with the
    failure reason, and closes it.  The issue can be reopened manually to
    retry on the next brimstone run.

    Args:
        repo:         Repository in ``owner/repo`` format.
        issue_number: GitHub issue number.
        reason:       Short failure description (e.g. subtype string).
        store:        Active BeadStore instance.  When set, updates the WorkBead
                      state to ``"abandoned"`` and flushes before the GitHub API calls.
    """
    if store is not None:
        bead = store.read_work_bead(issue_number)
        if bead is not None:
            bead.state = "abandoned"
            store.write_work_bead(bead)
            store.flush(f"brimstone: #{issue_number} abandoned — {reason}")
    _gh(
        [
            "issue",
            "comment",
            str(issue_number),
            "--body",
            (
                f"brimstone: agent exhausted all {MAX_RETRIES} retries without success.\n"
                f"Failure reason: `{reason}`\n\n"
                "Manual investigation required. Reopen this issue to retry on the next run."
            ),
        ],
        repo=repo,
        check=False,
    )
    _unclaim_issue(repo=repo, issue_number=issue_number, store=store)
    _gh(["issue", "edit", str(issue_number), "--add-label", "bug"], repo=repo, check=False)
    _gh(["issue", "close", str(issue_number)], repo=repo, check=False)


def _delete_remote_branch(repo: str, branch: str) -> None:
    """Delete a remote branch ref via the GitHub API.

    Args:
        repo:   Repository in ``owner/repo`` format.
        branch: Branch name to delete (e.g. ``42-fix-auth``).
    """
    _gh(
        ["api", f"repos/{repo}/git/refs/heads/{branch}", "--method", "DELETE"],
        check=False,
    )


def _milestone_exists(repo: str, title: str) -> bool:
    """Return True if a milestone with *title* exists in *repo* (open or closed)."""
    result = _gh(
        ["api", f"repos/{repo}/milestones", "--paginate", "-q", ".[].title"],
        check=False,
    )
    if result.returncode != 0:
        return False
    titles = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return title in titles


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
    store: BeadStore | None = None,
) -> tuple[list[dict], list[dict]]:
    """Classify remaining open research issues as blocking or non-blocking.

    Classification rule (tag-based, deterministic):
    - ``[DEFERRED]`` in body → non-blocking (explicitly deferred to next version)
    - Everything else → blocking (default; research agents must explicitly defer)

    This approach is conservative by design: an issue without an explicit
    ``[DEFERRED]`` tag is assumed to block the current milestone. Research
    agents and plan-milestones are responsible for tagging deferred work.

    Args:
        open_issues: List of open research issue dicts.
        repo:        Repository in ``owner/repo`` format (unused; kept for API compat).
        milestone:   Active research milestone name (unused; kept for API compat).
        config:      Current Config instance (unused; kept for API compat).
        checkpoint:  Current Checkpoint instance (unused; kept for API compat).
        dry_run:     If True, treat all issues as non-blocking.

    Returns:
        Tuple of (blocking_issues, non_blocking_issues).
    """
    if dry_run:
        return [], open_issues

    blocking: list[dict] = []
    non_blocking: list[dict] = []

    for issue in open_issues:
        issue_number = issue.get("number", 0)
        deferred = False
        if store is not None:
            bead = store.read_work_bead(issue_number)
            if bead is not None:
                deferred = bead.deferred
        if deferred:
            non_blocking.append(issue)
        else:
            blocking.append(issue)

    click.echo(
        f"[blocking-check] {len(blocking)} blocking, {len(non_blocking)} non-blocking "
        f"of {len(open_issues)} open research issue(s)",
        err=True,
    )
    return blocking, non_blocking


# ---------------------------------------------------------------------------
# Research worker implementation
# ---------------------------------------------------------------------------


def _strip_research_prefix(title: str) -> str:
    """Strip the 'Research: ' prefix from an issue title for use in PR titles."""
    prefix = "Research: "
    if title.startswith(prefix):
        return title[len(prefix) :]
    return title


def _dispatch_research_agent(
    issue: dict,
    branch_name: str,
    worktree_path: str,
    repo: str,
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
) -> tuple:
    """Dispatch a single research agent in its isolated worktree.

    Builds the research-agent prompt, calls runner.run(), and returns the
    run result. The worktree is NOT removed here; the orchestrator removes
    it after handling the result.

    Args:
        issue:        Issue dict with number, title, body, labels.
        branch_name:  Git branch name (e.g. ``1-research-language-choice``).
        worktree_path: Absolute path to the agent's isolated worktree.
        repo:         Repository in ``owner/repo`` format.
        milestone:    Active research milestone name.
        config:       Validated Config instance.
        checkpoint:   Current Checkpoint (read-only in thread).

    Returns:
        Tuple of (issue, branch_name, worktree_path, run_result).
    """
    issue_number = issue["number"]
    raw_body = issue.get("body") or ""
    body = _sanitize_issue_body(raw_body)
    today = date.today().isoformat()

    base_prompt = (
        f"## Headless Autonomous Mode\n"
        f"You are running in a fully automated headless pipeline. No human is present.\n"
        f"- Use tools directly and silently. Do NOT produce conversational text between calls.\n"
        f"- Do NOT explain what you are about to do. Do NOT narrate your reasoning.\n"
        f"## MANDATORY: Working Directory\n"
        f"You are working in an isolated git worktree. Your FIRST action must be:\n"
        f"```\n"
        f"cd {worktree_path}\n"
        f"```\n"
        f"ALL file writes and git operations must happen inside `{worktree_path}`.\n"
        f"The branch `{branch_name}` is already checked out there.\n"
        f"Do NOT write to /tmp, /var/folders, ~/, or the main repo checkout.\n"
        f"Your research document goes in: `{worktree_path}/docs/research/{milestone}/`\n"
        f"\n"
        f"## Session Parameters\n"
        f"- Repository: {repo}\n"
        f"- Active Milestone: {milestone}\n"
        f"- Branch: {branch_name}\n"
        f"- Working Directory: {worktree_path}\n"
        f"- Issue: #{issue_number} — {issue.get('title', '')}\n"
        f"- Session Date: {today}\n\n"
        f"{body}\n\n"
        f"## MANDATORY: Required Completion Steps\n"
        f"You MUST complete ALL of the following steps autonomously. "
        f"Do NOT pause, ask, or wait for confirmation at any point — this is a "
        f"fully automated headless pipeline.\n\n"
        f"After writing the research document:\n\n"
        f"**Step A — Commit:**\n"
        f"```bash\n"
        f"cd {worktree_path}\n"
        f"git add docs/research/\n"
        f'git commit -m "docs: <one-line summary of your recommendation> [skip ci]"\n'
        f"# Example: docs: recommend recursive descent + AST for parsing [skip ci]\n"
        f"# 5-10 word summary of the key conclusion — do NOT reuse the issue title.\n"
        f"```\n\n"
        f"**Step B — Push (REQUIRED, do not skip):**\n"
        f"```bash\n"
        f"git push -u origin {branch_name}\n"
        f"```\n\n"
        f"**Step C — Create PR (REQUIRED, do not skip):**\n"
        f"```bash\n"
        f"gh pr create --repo {repo} \\\n"
        '  --title "research: '
        f'{_strip_research_prefix(issue.get("title", f"#{issue_number}"))}" \\\n'
        f'  --label "stage/research" \\\n'
        f'  --body "Closes #{issue_number}\n\n'
        f"## Summary\n<your 1-3 sentence findings>\n\n"
        f'## Follow-up issues spawned\n<list or none>"\n'
        f"```\n\n"
        f"Steps B and C are NOT optional. The pipeline stalls if no PR is created.\n"
        f"Execute them immediately — do not say 'ready to push' or ask for approval.\n\n"
        f"**Step D — Verify CI and reviews (REQUIRED):**\n"
        f"Research commits use `[skip ci]` so CI should pass immediately.\n"
        f"Check that the PR is mergeable:\n"
        f"  gh pr view <PR-number> --repo {repo}"
        f" --json mergeable,mergeStateStatus --jq '{{mergeable,mergeStateStatus}}'\n"
        f"If CHANGES_REQUESTED from a reviewer:\n"
        f"  Collect feedback: gh pr view <PR-number> --repo {repo} --json reviews\n"
        f"  Address all feedback in ONE commit, push, re-request review.\n"
        f"  Max 2 review fix attempts.\n\n"
        f"**After Step D, when CI passes + no CHANGES_REQUESTED outstanding:**\n"
        f"Output exactly one line: `Done.`\n"
        f"Do NOT summarize findings, restate conclusions, or add any other text.\n"
        f"The research document is the deliverable — not your terminal output."
    )
    result = _run_agent(
        base_prompt,
        "research-worker",
        ["Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebSearch", "WebFetch"],
        100,
        f"research-{issue_number}",
        f"[research #{issue_number}] ",
        config,
        issue_number,
        model=config.model,
    )
    return issue, branch_name, worktree_path, result


def _run_persistent_pool(
    *,
    pool_size: int,
    gov: UsageGovernor | None = None,
    repo: str,
    repo_root: str,
    milestone: str,
    model: str | None = None,
    config: Config,
    checkpoint: Checkpoint,
    stage: str,
    fill_fn: Callable[[dict], None],
    on_success: Callable[[dict, str, str], None],
    when_empty_fn: Callable[[], bool],
    on_release: Callable[..., None] | None = None,
    stall_reason: str = "dep-blocked deadlock",
    store: BeadStore | None = None,
) -> None:
    """Persistent pool loop shared by research-worker, impl-worker, and design-worker.

    Maintains a live pool of concurrent agents.  When a future completes,
    ``fill_fn`` is called immediately to refill the pool.  Exits when
    ``when_empty_fn`` returns ``True`` (no more work) or a deadlock stall
    is detected.

    Args:
        pool_size:     Maximum concurrent agents.
        gov:           UsageGovernor for dispatch gating.  If ``None``, rate-limit
                       tracking and backoff recording are skipped.
        repo:          ``owner/repo`` string.
        repo_root:     Absolute path to the local repo clone used for worktrees.
        config:        Validated Config instance.
        checkpoint:    Active Checkpoint instance.
        stage:         Log stage label (``"research"``, ``"impl"``, ``"design"``).
        fill_fn:       ``fill_fn(active) -> None`` — submits new futures into
                       ``active`` until the pool is full or no candidates remain.
                       ``active`` maps ``Future`` → ``(issue, branch, worktree, *extras)``.
        on_success:    ``on_success(issue, branch, worktree_path) -> None`` — called
                       on a clean result.  Responsible for PR monitoring, triage, and
                       worktree cleanup.
        when_empty_fn: ``() -> bool`` — called when the pool drains.  Side-effects
                       the completion gate / pipeline filing.  Returns ``True`` to
                       exit the loop, ``False`` to stall-wait.
        on_release:    Optional ``on_release(slot) -> None`` — called immediately
                       after ``active.pop(future)`` (before result inspection), e.g.
                       to release a module lock.
        stall_reason:  Human-readable reason logged on deadlock escalation.
    """
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    # In-memory fallback retry tracker used when store is None.
    _retry_counts: dict[int, int] = {}
    stall_count: int = 0
    _watchdog_tick: int = 0
    active: dict = {}

    fill_fn(active)

    while True:
        if not active:
            if when_empty_fn():
                break
            stall_count += 1
            if stall_count >= STALL_MAX_ITERATIONS:
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="stall",
                    event_type="human_escalate",
                    payload={
                        "reason": stall_reason,
                        "stall_iterations": stall_count,
                    },
                    log_dir=config.log_dir.expanduser(),
                )
                break
            time.sleep(BACKOFF_SLEEP_SECONDS)
            fill_fn(active)
            continue

        stall_count = 0

        done, _ = concurrent.futures.wait(
            list(active.keys()), return_when=concurrent.futures.FIRST_COMPLETED
        )
        for future in done:
            slot = active.pop(future)
            _issue, _branch, _worktree_path = slot[0], slot[1], slot[2]
            issue_number = _issue["number"]

            if on_release is not None:
                on_release(slot)

            try:
                _, _, _, result = future.result()
            except Exception as exc:
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="dispatch",
                    event_type="agent_exception",
                    payload={"issue_number": issue_number, "error": str(exc)},
                    log_dir=config.log_dir.expanduser(),
                )
                if gov is not None:
                    gov.record_completion(1)
                _unclaim_issue(repo=repo, issue_number=issue_number, store=store)
                _remove_worktree(_worktree_path, repo_root)
                continue

            if gov is not None:
                gov.record_completion(1)
                gov.record_result(result)
            session.save(checkpoint, checkpoint_path)

            _log_agent_cost(
                result,
                repo,
                stage,
                config,
                checkpoint,
                issue_number=issue_number,
                milestone=milestone,
                model=model,
            )

            logger.log_conductor_event(
                run_id=checkpoint.run_id,
                phase="dispatch",
                event_type="agent_completed",
                payload={
                    "issue_number": issue_number,
                    "subtype": result.subtype,
                    "is_error": result.is_error,
                    "error_code": result.error_code,
                    "exit_code": result.exit_code,
                    "stderr": result.stderr[:500] if result.stderr else "",
                },
                log_dir=config.log_dir.expanduser(),
            )

            if gov is not None and (
                result.error_code in ("rate_limit", "extra_usage_exhausted")
                or result.subtype == "error_max_budget_usd"
            ):
                _unclaim_issue(repo=repo, issue_number=issue_number, store=store)
                _delete_remote_branch(repo, _branch)
                _bead = store.read_work_bead(issue_number) if store is not None else None
                attempt = (
                    _bead.retry_count if _bead is not None else _retry_counts.get(issue_number, 0)
                )
                gov.record_429(attempt)
                if _bead is not None:
                    _bead.retry_count = attempt + 1
                    store.write_work_bead(_bead)  # type: ignore[union-attr]
                else:
                    _retry_counts[issue_number] = attempt + 1
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
                _remove_worktree(_worktree_path, repo_root)
                continue

            if result.is_error:
                _bead = store.read_work_bead(issue_number) if store is not None else None
                current_retries = (
                    _bead.retry_count if _bead is not None else _retry_counts.get(issue_number, 0)
                ) + 1
                if _bead is not None:
                    _bead.retry_count = current_retries
                    store.write_work_bead(_bead)  # type: ignore[union-attr]
                else:
                    _retry_counts[issue_number] = current_retries
                _delete_remote_branch(repo, _branch)
                if current_retries >= MAX_RETRIES:
                    reason = result.subtype or "unknown_error"
                    _exhaust_issue(repo, issue_number, reason, store)
                    click.echo(
                        f"[{stage}] #{issue_number} exhausted {MAX_RETRIES} retries "
                        f"({reason}) — closed with 'bug' label. Reopen to retry.",
                        err=True,
                    )
                    logger.log_conductor_event(
                        run_id=checkpoint.run_id,
                        phase="dispatch",
                        event_type="human_escalate",
                        payload={
                            "issue_number": issue_number,
                            "reason": reason,
                            "error_code": result.error_code,
                            "retry_count": current_retries,
                            "stderr": result.stderr[:500] if result.stderr else "",
                            "action_required": "manual investigation",
                        },
                        log_dir=config.log_dir.expanduser(),
                    )
                else:
                    _unclaim_issue(repo=repo, issue_number=issue_number, store=store)
                _remove_worktree(_worktree_path, repo_root)
                session.save(checkpoint, checkpoint_path)
                continue

            # Success — delegate PR monitoring, triage, and worktree cleanup to on_success
            on_success(_issue, _branch, _worktree_path)
            session.save(checkpoint, checkpoint_path)

        _watchdog_tick += 1
        if store is not None and _watchdog_tick % WATCHDOG_INTERVAL == 0:
            active_numbers = {active[f][0]["number"] for f in active}
            _watchdog_scan(
                repo=repo,
                config=config,
                checkpoint=checkpoint,
                store=store,
                active_issue_numbers=active_numbers,
                default_branch="",
            )

        fill_fn(active)


def _run_research_worker(
    repo: str,
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
    store: BeadStore | None = None,
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
    repo_root, _clone_dir = _ensure_worktree_repo(repo)

    # Pre-check: the milestone must exist before we can do anything useful.
    if not dry_run and not _milestone_exists(repo, milestone):
        click.echo(
            f"Error: Milestone '{milestone}' does not exist on GitHub. "
            "Did plan-milestones complete successfully?",
            err=True,
        )
        raise SystemExit(1)

    default_branch = _get_default_branch_for_repo(repo) if not dry_run else config.default_branch

    # Resume: for any in-progress issues, find their PR and monitor it.
    # If no PR exists (agent pushed a branch but crashed before creating the PR,
    # or no branch at all), unclaim so the issue can be re-dispatched cleanly.
    if not dry_run:
        _resume_stale_issues(
            repo=repo,
            milestone=milestone,
            label=RESEARCH_LABEL,
            log_prefix="[research-worker]",
            config=config,
            checkpoint=checkpoint,
            default_branch=default_branch,
            repo_root=repo_root,
            store=store,
        )
        _startup_dep_checks(_list_open_issues_by_label(repo, milestone, RESEARCH_LABEL), repo)

    research_limits: dict[str, int] = {"pro": 2, "max": 3, "max20x": 5}
    pool_size = config.max_concurrency or research_limits.get(config.subscription_tier, 3)

    if dry_run:
        if store is not None:
            _seed_work_beads(repo, milestone, RESEARCH_LABEL, "research", store)
            open_beads = store.list_work_beads(state="open", milestone=milestone, stage="research")
            eligible = [b for b in open_beads if not b.deferred]
            for bead in eligible[:1]:
                click.echo(
                    f"[dry-run] Would dispatch research agent for issue "
                    f"#{bead.issue_number}: {bead.title}"
                )
        else:
            open_issues = _list_open_issues_by_label(repo, milestone, RESEARCH_LABEL)
            for issue in open_issues[:1]:
                click.echo(
                    f"[dry-run] Would dispatch research agent for issue "
                    f"#{issue['number']}: {issue.get('title', '')}"
                )
        _run_completion_gate(
            repo=repo,
            milestone=milestone,
            open_issues=[],
            config=config,
            checkpoint=checkpoint,
            dry_run=dry_run,
        )
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=pool_size) as executor:

        def _fill(active: dict) -> None:
            if store is not None:
                # Bead-based dispatch: seed from GitHub, then dispatch from bead store
                _seed_work_beads(repo, milestone, RESEARCH_LABEL, "research", store)
                open_beads = store.list_work_beads(
                    state="open", milestone=milestone, stage="research"
                )
                if not open_beads:
                    return
                eligible = [
                    b
                    for b in open_beads
                    if b.state == "open"
                    and not b.deferred
                    and all(
                        (dep_bead := store.read_work_bead(dep)) is not None
                        and dep_bead.state == "closed"
                        for dep in b.blocked_by
                    )
                ]
                if not eligible:
                    return
                active_issue_numbers = {slot[0]["number"] for slot in active.values()}
                ranked_beads = sorted(
                    eligible,
                    key=lambda b: (_PRIORITY_ORDER.get(b.priority, 5), b.issue_number),
                )
                for bead in ranked_beads:
                    if len(active) >= pool_size:
                        break
                    if not gov.can_dispatch(1):
                        break
                    if bead.issue_number in active_issue_numbers:
                        continue
                    slug = _slugify(bead.title[:40])
                    branch_name = f"{bead.issue_number}-{slug}"
                    _claim_issue(
                        repo=repo,
                        issue_number=bead.issue_number,
                        branch=branch_name,
                        store=store,
                    )
                    session.save(checkpoint, checkpoint_path)
                    worktree_path = _create_worktree(branch_name, repo_root, default_branch)
                    if worktree_path is None:
                        click.echo(
                            f"[research-worker] Failed to create worktree for "
                            f"#{bead.issue_number} (branch={branch_name!r}) — unclaiming",
                            err=True,
                        )
                        _unclaim_issue(repo=repo, issue_number=bead.issue_number, store=store)
                        continue
                    # Fetch issue body from GitHub only at dispatch time
                    _view = _gh(
                        [
                            "issue",
                            "view",
                            str(bead.issue_number),
                            "--json",
                            "body,number,title",
                        ],
                        repo=repo,
                        check=False,
                    )
                    try:
                        issue_data = json.loads(_view.stdout)
                    except (json.JSONDecodeError, AttributeError):
                        issue_data = {
                            "number": bead.issue_number,
                            "title": bead.title,
                            "body": "",
                        }
                    logger.log_conductor_event(
                        run_id=checkpoint.run_id,
                        phase="claim",
                        event_type="issue_claimed",
                        payload={
                            "issue_number": bead.issue_number,
                            "title": bead.title,
                            "branch": branch_name,
                            "worktree": worktree_path,
                        },
                        log_dir=config.log_dir.expanduser(),
                    )
                    gov.record_dispatch(1)
                    future = executor.submit(
                        _dispatch_research_agent,
                        issue=issue_data,
                        branch_name=branch_name,
                        worktree_path=worktree_path,
                        repo=repo,
                        milestone=milestone,
                        config=config,
                        checkpoint=checkpoint,
                    )
                    active[future] = (issue_data, branch_name, worktree_path)
                    active_issue_numbers.add(bead.issue_number)
            else:
                # Fallback: no bead store — use GitHub-based dispatch
                open_issues = _list_open_issues_by_label(repo, milestone, RESEARCH_LABEL)
                if not open_issues:
                    return
                blocking, _ = _classify_blocking_issues(
                    open_issues, repo, milestone, config, checkpoint, store=None
                )
                if not blocking:
                    return
                active_issue_numbers = {slot[0]["number"] for slot in active.values()}
                ranked = _sort_issues(blocking)
                for issue in ranked:
                    if len(active) >= pool_size:
                        break
                    if not gov.can_dispatch(1):
                        break
                    issue_number = issue["number"]
                    if issue_number in active_issue_numbers:
                        continue
                    issue_title = issue.get("title", "")
                    slug = _slugify(issue_title[:40])
                    branch_name = f"{issue_number}-{slug}"
                    _claim_issue(
                        repo=repo,
                        issue_number=issue_number,
                        issue=issue,
                        branch=branch_name,
                        store=None,
                    )
                    session.save(checkpoint, checkpoint_path)
                    worktree_path = _create_worktree(branch_name, repo_root, default_branch)
                    if worktree_path is None:
                        click.echo(
                            f"[research-worker] Failed to create worktree for "
                            f"#{issue_number} (branch={branch_name!r}) — unclaiming",
                            err=True,
                        )
                        _unclaim_issue(repo=repo, issue_number=issue_number, store=None)
                        continue
                    logger.log_conductor_event(
                        run_id=checkpoint.run_id,
                        phase="claim",
                        event_type="issue_claimed",
                        payload={
                            "issue_number": issue_number,
                            "title": issue_title,
                            "branch": branch_name,
                            "worktree": worktree_path,
                        },
                        log_dir=config.log_dir.expanduser(),
                    )
                    gov.record_dispatch(1)
                    future = executor.submit(
                        _dispatch_research_agent,
                        issue=issue,
                        branch_name=branch_name,
                        worktree_path=worktree_path,
                        repo=repo,
                        milestone=milestone,
                        config=config,
                        checkpoint=checkpoint,
                    )
                    active[future] = (issue, branch_name, worktree_path)
                    active_issue_numbers.add(issue_number)

        def _on_success(issue: dict, branch: str, worktree_path: str) -> None:
            issue_number = issue["number"]
            found = _find_pr_for_issue(repo, issue_number)
            if found is not None:
                pr_number, pr_branch = found
                _monitor_pr(
                    pr_number=pr_number,
                    branch=pr_branch,
                    repo=repo,
                    config=config,
                    checkpoint=checkpoint,
                    issue_number=issue_number,
                    store=store,
                    worktree_path=worktree_path,
                    default_branch=default_branch,
                )
            else:
                click.echo(
                    f"[research-worker] Warning: no open PR found for issue #{issue_number}",
                    err=True,
                )
            _remove_worktree(worktree_path, repo_root)
            if store is not None:
                _process_merge_queue(
                    repo=repo,
                    config=config,
                    checkpoint=checkpoint,
                    store=store,
                    default_branch=default_branch,
                    repo_root=repo_root,
                )

        def _when_empty() -> bool:
            if store is not None:
                # Bead-based completion check
                _seed_work_beads(repo, milestone, RESEARCH_LABEL, "research", store)
                all_beads = store.list_work_beads(milestone=milestone, stage="research")
                open_beads = [b for b in all_beads if b.state not in ("closed", "abandoned")]
                if not open_beads:
                    _run_completion_gate(
                        repo=repo,
                        milestone=milestone,
                        open_issues=[],
                        config=config,
                        checkpoint=checkpoint,
                        dry_run=False,
                    )
                    return True
                blocking = [b for b in open_beads if not b.deferred]
                if not blocking:
                    deferred_issues = [
                        {"number": b.issue_number, "title": b.title} for b in open_beads
                    ]
                    _run_completion_gate(
                        repo=repo,
                        milestone=milestone,
                        open_issues=deferred_issues,
                        config=config,
                        checkpoint=checkpoint,
                        dry_run=False,
                    )
                    return True
                # All blocking beads are in merge_ready state — their PRs are
                # queued but not yet merged. Drain the merge queue now so the
                # research loop doesn't stall out and exit prematurely.
                if all(b.state == "merge_ready" for b in blocking):
                    _process_merge_queue(
                        repo=repo,
                        config=config,
                        checkpoint=checkpoint,
                        store=store,
                        default_branch=default_branch,
                        repo_root=repo_root,
                    )
                    # Re-check after draining
                    all_beads = store.list_work_beads(milestone=milestone, stage="research")
                    open_beads = [b for b in all_beads if b.state not in ("closed", "abandoned")]
                    if not open_beads:
                        _run_completion_gate(
                            repo=repo,
                            milestone=milestone,
                            open_issues=[],
                            config=config,
                            checkpoint=checkpoint,
                            dry_run=False,
                        )
                        return True
                    blocking = [b for b in open_beads if not b.deferred]
                    if not blocking:
                        deferred_issues = [
                            {"number": b.issue_number, "title": b.title} for b in open_beads
                        ]
                        _run_completion_gate(
                            repo=repo,
                            milestone=milestone,
                            open_issues=deferred_issues,
                            config=config,
                            checkpoint=checkpoint,
                            dry_run=False,
                        )
                        return True
                return False
            # Fallback: no bead store — use GitHub-based completion check.
            # Use _list_all_open_issues_by_label so in-progress issues (claimed,
            # PR open but not merged) are not silently excluded.
            open_issues = _list_all_open_issues_by_label(repo, milestone, RESEARCH_LABEL)
            if not open_issues:
                _run_completion_gate(
                    repo=repo,
                    milestone=milestone,
                    open_issues=[],
                    config=config,
                    checkpoint=checkpoint,
                    dry_run=False,
                )
                return True
            blocking, deferred = _classify_blocking_issues(
                open_issues, repo, milestone, config, checkpoint, store=None
            )
            if not blocking:
                _run_completion_gate(
                    repo=repo,
                    milestone=milestone,
                    open_issues=deferred,
                    config=config,
                    checkpoint=checkpoint,
                    dry_run=False,
                )
                return True
            return False

        _run_persistent_pool(
            pool_size=pool_size,
            gov=gov,
            repo=repo,
            repo_root=repo_root,
            milestone=milestone,
            model=config.model,
            config=config,
            checkpoint=checkpoint,
            stage="research",
            fill_fn=_fill,
            on_success=_on_success,
            when_empty_fn=_when_empty,
            stall_reason=(
                "All blocking research issues are dependency-blocked and no agents are "
                "running. Manual intervention required to resolve the dependency cycle "
                "or close/skip issues."
            ),
            store=store,
        )

    if _clone_dir:
        shutil.rmtree(_clone_dir, ignore_errors=True)


def _find_next_milestone(repo: str, current_milestone: str) -> str | None:
    """Return the title of the next milestone after *current_milestone*, or None.

    Lists all milestones (open + closed), sorts them alphabetically, and
    returns the one that immediately follows *current_milestone*.  Returns
    ``None`` if no later milestone exists or the listing fails.
    """
    result = _gh(
        ["api", f"repos/{repo}/milestones", "--paginate", "-q", ".[].title"],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return None
    titles = sorted(t.strip() for t in result.stdout.splitlines() if t.strip())
    try:
        idx = titles.index(current_milestone)
        return titles[idx + 1] if idx + 1 < len(titles) else None
    except ValueError:
        return None


def _migrate_issue_to_milestone(repo: str, issue_number: int, next_milestone: str) -> bool:
    """Move *issue_number* to *next_milestone*.  Returns True on success."""
    result = _gh(
        [
            "issue",
            "edit",
            str(issue_number),
            "--milestone",
            next_milestone,
        ],
        repo=repo,
        check=False,
    )
    return result.returncode == 0


def _run_completion_gate(
    repo: str,
    milestone: str,
    open_issues: list[dict[str, Any]],
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
) -> None:
    """Declare research milestone complete, migrate non-blocking issues, file the HLD issue.

    Called when zero blocking issues remain.  Non-blocking open issues are
    migrated to the next milestone when one exists.

    Args:
        repo:        Repository in ``owner/repo`` format.
        milestone:   Completed research milestone name.
        open_issues: Remaining non-blocking open issues to migrate.
        config:      Current Config instance.
        checkpoint:  Current Checkpoint instance.
        dry_run:     If True, print actions without modifying GitHub.
    """
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="complete",
        event_type="stage_complete",
        payload={"milestone": milestone},
        log_dir=config.log_dir.expanduser(),
    )

    # Migrate non-blocking open issues to the next milestone.
    if open_issues:
        next_ms = _find_next_milestone(repo, milestone)
        if next_ms:
            for issue in open_issues:
                issue_num = issue.get("number")
                if issue_num:
                    if dry_run:
                        click.echo(f"[dry-run] Would migrate #{issue_num} to milestone '{next_ms}'")
                    else:
                        ok = _migrate_issue_to_milestone(repo, issue_num, next_ms)
                        if ok:
                            click.echo(f"Migrated #{issue_num} to milestone '{next_ms}'")
                        else:
                            click.echo(
                                f"Warning: could not migrate #{issue_num} to '{next_ms}'",
                                err=True,
                            )
        else:
            click.echo(
                f"Warning: no next milestone found; {len(open_issues)} non-blocking "
                f"issue(s) left in '{milestone}'.",
                err=True,
            )

    hld_title = f"Design: HLD for {milestone}"
    if dry_run:
        click.echo(f"[dry-run] Would file: {hld_title!r}")
    else:
        _file_design_issue_if_missing(
            repo=repo,
            milestone=milestone,
            title=hld_title,
            body=(
                "## Deliverable\n"
                f"`docs/design/{milestone}/HLD.md`\n\n"
                "## Instructions\n"
                f"Read all merged research docs in `docs/research/{milestone}/`. Write the HLD.\n\n"
                "For each module identified, file a `Design: LLD for <module>` issue with "
                "label `stage/design` and this milestone. Check for duplicates first.\n\n"
                "**Each LLD issue body must include a module-specific description drawn "
                "directly from the HLD.** Use this exact template for every LLD issue body:\n\n"
                "```\n"
                "## Module description\n"
                "<1–2 sentence summary of what this module does and its role in the system, "
                "taken from the HLD.>\n\n"
                "## Key responsibilities\n"
                "<Bullet list of the module's main responsibilities as described in the HLD.>\n\n"
                "## Deliverable\n"
                f"`docs/design/{milestone}/lld/<module>.md`\n\n"
                "## Acceptance criteria\n"
                "The LLD must cover: public interface, data structures, key algorithms, "
                "error handling, and test strategy for this module.\n"
                "```\n\n"
                "## Acceptance Criteria\n"
                f"- `docs/design/{milestone}/HLD.md` committed and PR created\n"
                "- One `Design: LLD for <module>` issue filed per module, each with a "
                "module-specific description from the HLD"
            ),
        )

    session.save(checkpoint, checkpoint_path)

    click.echo(f"Research milestone '{milestone}' complete. Filed HLD design issue.")


# ---------------------------------------------------------------------------
# Impl worker helpers
# ---------------------------------------------------------------------------

# Module label prefix for extracting module name from issue labels
_FEAT_PREFIX = "feat:"

# CI poll constants
_CI_POLL_INTERVAL: int = 30  # seconds between gh pr checks polls
_CI_MAX_POLLS: int = 60  # maximum polls before timeout (30 min)
_CI_NO_CHECKS_GRACE: int = 3  # consecutive no_checks polls before treating as "no CI configured"
_REBASE_RETRY_LIMIT: int = 3  # max rebase attempts before escalating


def _slugify(title: str, max_len: int = 40) -> str:
    """Convert an issue title to a URL/branch-safe slug.

    Lowercases, replaces spaces and non-alphanumeric characters with hyphens,
    strips leading/trailing hyphens, and truncates to *max_len* characters.

    Args:
        title:   Raw issue title string.
        max_len: Maximum character count for the slug (default 40).

    Returns:
        Slug string suitable for use as a git branch name suffix.
    """
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:max_len]


def _extract_module(issue: dict[str, Any]) -> str:
    """Extract the module name from a feat:* label on an issue.

    Scans the issue's ``labels`` list for a label whose name starts with
    ``feat:``.  Returns the part after the prefix (e.g. ``"config"`` from
    ``"feat:config"``).  Returns ``"none"`` when no matching label is found.

    Args:
        issue: Issue dict with a ``labels`` key (list of label dicts with ``name``).

    Returns:
        Module name string, or ``"none"`` if no feat:* label is present.
    """
    for label in issue.get("labels", []):
        name = label.get("name", "")
        if name.startswith(_FEAT_PREFIX):
            return name[len(_FEAT_PREFIX) :]
    return "none"


def _extract_stage(issue: dict[str, Any]) -> str:
    """Return the stage string from issue labels ('research', 'impl', 'design', or '')."""
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    if "stage/research" in labels:
        return "research"
    if "stage/impl" in labels:
        return "impl"
    if "stage/design" in labels:
        return "design"
    return ""


def _extract_priority(issue: dict[str, Any]) -> str:
    """Return the priority label from issue labels (defaults to 'P2')."""
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    for p in ("P0", "P1", "P2", "P3", "P4"):
        if p in labels:
            return p
    return "P2"


def _find_pr_for_branch(repo: str, branch: str) -> int | None:
    """Find the open PR number for a given branch name.

    Args:
        repo:   Repository in ``owner/repo`` format.
        branch: Branch name to search for.

    Returns:
        PR number as integer, or ``None`` if no open PR exists.
    """
    result = _gh(
        ["pr", "list", "--head", branch, "--state", "open", "--json", "number", "--limit", "5"],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        prs = json.loads(result.stdout)
        if prs:
            return int(prs[0]["number"])
    except (json.JSONDecodeError, KeyError, ValueError):
        pass
    return None


def _find_pr_for_issue(repo: str, issue_number: int) -> tuple[int, str] | None:
    """Find an open PR that was created for *issue_number*.

    Searches open PRs by head-branch prefix (``<N>-``) and by ``Closes #N``
    in the PR body. Returns ``(pr_number, branch)`` or ``None``.

    Args:
        repo:         Repository in ``owner/repo`` format.
        issue_number: Issue number to search for.

    Returns:
        ``(pr_number, head_branch)`` tuple, or ``None`` if not found.
    """
    result = _gh(
        [
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,headRefName,body",
            "--limit",
            "100",
        ],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        prs: list[dict] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    closes_pattern = f"#{issue_number}"
    for pr in prs:
        head: str = pr.get("headRefName") or ""
        body: str = pr.get("body") or ""
        # Branch prefix match is the most reliable signal
        if head.startswith(f"{issue_number}-"):
            return int(pr["number"]), head
        # Fall back to body reference
        if f"Closes {closes_pattern}" in body or f"Closes: {closes_pattern}" in body:
            return int(pr["number"]), head
    return None


def _pr_merged_for_issue(repo: str, issue_number: int) -> bool:
    """Return True if a merged PR that closes *issue_number* exists.

    Used during crash recovery to detect the case where a PR was merged but the
    issue was not automatically closed (e.g. the orchestrator crashed between the
    merge and the close step).

    Args:
        repo:         Repository in ``owner/repo`` format.
        issue_number: Issue number to check.

    Returns:
        True if a merged PR referencing this issue is found.
    """
    result = _gh(
        [
            "pr",
            "list",
            "--state",
            "merged",
            "--json",
            "number,headRefName,body",
            "--limit",
            "200",
        ],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return False
    try:
        prs: list[dict] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False

    closes_pattern = f"#{issue_number}"
    for pr in prs:
        head: str = pr.get("headRefName") or ""
        body: str = pr.get("body") or ""
        if head.startswith(f"{issue_number}-"):
            return True
        if f"Closes {closes_pattern}" in body or f"Closes: {closes_pattern}" in body:
            return True
    return False


def _get_pr_checks_status(repo: str, pr_number: int) -> str:
    """Get the aggregate CI status for a PR.

    Calls ``gh pr checks`` and aggregates:
    - If all checks pass → ``"pass"``
    - If any check failed → ``"fail"``
    - If any check is pending/in-progress → ``"pending"``
    - On error or no checks → ``"pending"``

    Args:
        repo:      Repository in ``owner/repo`` format.
        pr_number: PR number.

    Returns:
        One of ``"pass"``, ``"fail"``, or ``"pending"``.
    """
    # gh pr checks --json uses "name,state,bucket" (not status/conclusion)
    # state: queued | in_progress | completed
    # bucket: pass | fail | pending | skipping | cancelling
    result = _gh(
        ["pr", "checks", str(pr_number), "--json", "name,state,bucket"],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        # gh pr checks exits non-zero when no checks have been reported yet.
        # Treat "no checks" as green so repos without CI don't stall forever.
        no_checks_phrases = ("no checks", "no check runs")
        stderr_lower = (result.stderr or "").lower()
        stdout_lower = (result.stdout or "").lower()
        if any(p in stderr_lower or p in stdout_lower for p in no_checks_phrases):
            return "pass"
        return "pending"

    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "pending"

    if not checks:
        # Empty list means checks haven't been queued yet (race after force-push)
        # or truly no CI configured.  Callers use a grace counter to distinguish.
        return "no_checks"

    statuses = []
    for check in checks:
        bucket = (check.get("bucket") or "").lower()
        state = (check.get("state") or "").lower()
        if bucket == "fail" or bucket == "cancelling":
            statuses.append("fail")
        elif bucket == "pass" or bucket == "skipping":
            statuses.append("pass")
        elif state == "completed":
            statuses.append("pass")
        else:
            statuses.append("pending")

    if "fail" in statuses:
        return "fail"
    if "pending" in statuses:
        return "pending"
    return "pass"


def _is_conflict_failure(repo: str, pr_number: int) -> bool:
    """Check whether a PR has a merge conflict.

    Inspects the PR's mergeable state.

    Args:
        repo:      Repository in ``owner/repo`` format.
        pr_number: PR number.

    Returns:
        True if the PR has a merge conflict.
    """
    result = _gh(
        ["pr", "view", str(pr_number), "--json", "mergeable,mergeStateStatus"],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return False
    try:
        data = json.loads(result.stdout)
        mergeable = (data.get("mergeable") or "").upper()
        state = (data.get("mergeStateStatus") or "").upper()
        return mergeable == "CONFLICTING" or state == "DIRTY"
    except (json.JSONDecodeError, AttributeError):
        return False


def _rebase_branch(
    branch: str,
    repo: str,
    worktree_path: str,
    default_branch: str = "main",
    config: Config | None = None,
) -> bool:
    """Rebase a worktree branch onto the remote default branch.

    Args:
        branch:         Branch name being rebased.
        repo:           Repository in ``owner/repo`` format (unused but kept for context).
        worktree_path:  Absolute path to the worktree directory.
        default_branch: Default branch name (e.g. ``"main"``, ``"mainline"``).
        config:         When provided, dispatches a Claude agent to resolve conflicts
                        instead of aborting immediately.

    Returns:
        True if the rebase succeeded, False if it failed or conflicted.
    """
    # Fetch latest remote state
    fetch = subprocess.run(
        ["git", "fetch", "origin"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if fetch.returncode != 0:
        return False

    # Attempt rebase
    rebase = subprocess.run(
        ["git", "rebase", f"origin/{default_branch}"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if rebase.returncode != 0:
        if config is not None:
            resolved = _dispatch_conflict_resolution_agent(
                branch, worktree_path, repo, default_branch, config
            )
            if resolved:
                return True
        # Abort on unresolvable failure
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
        )
        return False

    # Force push after successful rebase
    push = subprocess.run(
        ["git", "push", "--force-with-lease", "origin", branch],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    return push.returncode == 0


def _get_review_status(repo: str, pr_number: int) -> str:
    """Get the aggregate review status for a PR.

    Returns:
        ``"approved"`` — at least one approval and no blocking changes_requested.
        ``"changes_requested"`` — reviewer has requested changes.
        ``"no_review"`` — no reviews submitted yet (treated as approved).
    """
    result = _gh(
        ["pr", "view", str(pr_number), "--json", "reviews"],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return "no_review"

    try:
        data = json.loads(result.stdout)
        reviews = data.get("reviews", [])
    except (json.JSONDecodeError, AttributeError):
        return "no_review"

    if not reviews:
        return "no_review"

    # Use latest review per reviewer
    latest: dict[str, str] = {}
    for review in reviews:
        author = (review.get("author") or {}).get("login", "unknown")
        state = (review.get("state") or "").upper()
        latest[author] = state

    states = set(latest.values())
    if "CHANGES_REQUESTED" in states:
        return "changes_requested"
    if "APPROVED" in states:
        return "approved"
    return "no_review"


def _dispatch_conflict_resolution_agent(
    branch: str,
    worktree_path: str,
    repo: str,
    default_branch: str,
    config: Config,
) -> bool:
    """Dispatch a Claude agent to resolve rebase conflicts in the worktree.

    Called when git rebase fails due to conflicts. The agent inspects
    conflicted files, resolves them (preferring upstream for metadata),
    runs git add + git rebase --continue, then force-pushes.

    Returns True if the agent resolved conflicts and pushed, False otherwise.
    """
    prompt = (
        f"## Headless Conflict Resolution\n"
        f"You are running in a fully automated headless pipeline. No human is present.\n"
        f"Use tools directly and silently. Do NOT produce conversational text.\n\n"
        f"## Task\n"
        f"A `git rebase origin/{default_branch}` on branch `{branch}` "
        f"has left the worktree in a conflicted state.\n"
        f"Resolve the conflicts, complete the rebase, and push.\n\n"
        f"## Steps\n"
        f"1. cd {worktree_path}\n"
        f"2. Run `git status` to identify conflicted files\n"
        f"3. For each conflicted file:\n"
        f"   - Read the conflict markers carefully\n"
        f"   - For documentation/README files: accept upstream (mainline) version\n"
        f"   - For source code: merge both sets of changes correctly\n"
        f"4. `git add` each resolved file\n"
        f"5. `git rebase --continue` (set GIT_EDITOR=true to skip editor)\n"
        f"6. `git push --force-with-lease origin {branch}`\n"
        f"7. STOP. Do not create PRs or do anything else.\n"
    )
    result = _run_agent(
        prompt,
        "conflict-resolver",
        runner.TOOLS_IMPL_AGENT,
        30,
        f"conflict-resolve-{branch}",
        f"[conflict-resolve {branch}] ",
        config,
    )
    return not result.is_error


def _find_next_version(milestone: str) -> str:
    """Infer the next version string from an implementation milestone title.

    Examples:
        ``"MVP Implementation"`` → ``"v2"``
        ``"v1 Implementation"`` → ``"v2"``
        ``"v1.1 Implementation"`` → ``"v2"``

    Args:
        milestone: Title of the current implementation milestone.

    Returns:
        A version string for the next milestone (e.g. ``"v2"``).
    """
    title_lower = milestone.lower()
    match = re.search(r"(v\d+[\.\d]*|mvp|alpha|beta)", title_lower)
    if match:
        version_token = match.group(1)
        if version_token == "mvp":
            return "v2"
        ver_match = re.match(r"v(\d+)", version_token)
        if ver_match:
            next_v = int(ver_match.group(1)) + 1
            return f"v{next_v}"
    return "next version"


def _monitor_pr(
    pr_number: int,
    branch: str,
    repo: str,
    config: Config,
    checkpoint: Checkpoint,
    issue_number: int,
    store: BeadStore | None = None,
    worktree_path: str = "",
    default_branch: str = "main",
    max_polls: int = _CI_MAX_POLLS,
    poll_interval: int = _CI_POLL_INTERVAL,
) -> bool:
    """Monitor a PR's CI status and squash-merge it when CI passes.

    Polls ``gh pr checks`` up to *max_polls* times. Conflicts are detected at
    the start of each poll (before CI status is checked) so that a rebase can
    be attempted even while CI is still pending. On successful rebase, polling
    resumes so CI can re-run on the rebased branch. Rebase is retried up to
    ``_REBASE_RETRY_LIMIT`` times before escalating. On CI pass, checks reviews
    and squash-merges.

    Args:
        pr_number:      GitHub PR number.
        branch:         Branch name for the PR.
        repo:           Repository in ``owner/repo`` format.
        config:         Config instance for logging.
        checkpoint:     Checkpoint instance for logging.
        issue_number:   Original issue number (for logging).
        store:          BeadStore for writing PRBead state transitions. If
                        None, bead writes are skipped (backward-compat).
        worktree_path:  Absolute path to the worktree directory (for rebase).
                        If empty, rebase on conflict is skipped and the PR is
                        escalated to human review instead.
        default_branch: Default branch name used as the rebase target
                        (e.g. ``"main"``, ``"mainline"``).
        max_polls:      Maximum number of CI status polls before timeout.
        poll_interval:  Seconds to sleep between polls.

    Returns:
        True if the PR was successfully merged. False otherwise.
    """
    rebase_attempts = 0
    ci_fail_count = 0
    consecutive_no_checks = 0  # grace counter: empty CI after force-push vs no CI configured

    # Write initial PRBead
    pr_bead = PRBead(
        v=1,
        pr_number=pr_number,
        issue_number=issue_number,
        branch=branch,
        state="open",
        created_at=datetime.now(UTC).isoformat(),
    )
    if store is not None:
        store.write_pr_bead(pr_bead)

    for poll_idx in range(max_polls):
        time.sleep(poll_interval)

        # Detect merge conflicts at the start of every poll — this fires even
        # while CI is still pending so we don't wait 30 min before rebasing.
        if _is_conflict_failure(repo, pr_number):
            if not worktree_path:
                # No worktree available (e.g. research PRs) — can't rebase
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="ci_check",
                    event_type="human_escalate",
                    payload={
                        "pr_number": pr_number,
                        "issue_number": issue_number,
                        "reason": "conflict detected but no worktree for rebase",
                    },
                    log_dir=config.log_dir.expanduser(),
                )
                pr_bead.state = "conflict"
                if store is not None:
                    store.write_pr_bead(pr_bead)
                return False

            if rebase_attempts >= _REBASE_RETRY_LIMIT:
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="ci_check",
                    event_type="human_escalate",
                    payload={
                        "pr_number": pr_number,
                        "issue_number": issue_number,
                        "reason": "rebase limit exceeded",
                    },
                    log_dir=config.log_dir.expanduser(),
                )
                pr_bead.state = "conflict"
                if store is not None:
                    store.write_pr_bead(pr_bead)
                return False

            rebase_ok = _rebase_branch(branch, repo, worktree_path, default_branch, config=config)
            rebase_attempts += 1
            if not rebase_ok:
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="ci_check",
                    event_type="human_escalate",
                    payload={
                        "pr_number": pr_number,
                        "issue_number": issue_number,
                        "reason": "rebase failed — conflicts outside agent scope",
                    },
                    log_dir=config.log_dir.expanduser(),
                )
                pr_bead.state = "conflict"
                if store is not None:
                    store.write_pr_bead(pr_bead)
                return False
            # Rebase succeeded — continue polling so CI can re-run
            continue

        ci_status = _get_pr_checks_status(repo, pr_number)

        # Handle the "no checks yet" case: distinguish a brief post-push race
        # (CI not triggered yet) from "repo has no CI configured".
        # After _CI_NO_CHECKS_GRACE consecutive no_checks polls, treat as pass.
        if ci_status == "no_checks":
            consecutive_no_checks += 1
            if consecutive_no_checks < _CI_NO_CHECKS_GRACE:
                continue  # wait for CI to appear
            ci_status = "pass"  # grace exhausted — no CI configured
        else:
            consecutive_no_checks = 0  # reset when real checks appear

        logger.log_conductor_event(
            run_id=checkpoint.run_id,
            phase="ci_check",
            event_type="ci_checked",
            payload={
                "pr_number": pr_number,
                "issue_number": issue_number,
                "status": ci_status,
                "poll_index": poll_idx,
            },
            log_dir=config.log_dir.expanduser(),
        )

        if ci_status == "pass":
            # Check reviews
            review_status = _get_review_status(repo, pr_number)

            if review_status == "changes_requested":
                # Record that review feedback is pending — do NOT dispatch a fix agent.
                # Agents handle their own review feedback via updated skill files (PR 5).
                pr_bead.state = "reviewing"
                if store is not None:
                    store.write_pr_bead(pr_bead)
                # Continue polling — agent may push a fix commit
                continue

            # Enqueue to MergeQueue — _process_merge_queue() does the actual merge
            _now_str = datetime.now(UTC).isoformat()
            if store is not None:
                _queue = store.read_merge_queue()
                _queue.queue.append(
                    MergeQueueEntry(
                        pr_number=pr_number,
                        issue_number=issue_number,
                        branch=branch,
                        enqueued_at=_now_str,
                    )
                )
                _queue.updated_at = _now_str
                store.write_merge_queue(_queue)
                pr_bead.state = "merge_ready"
                store.write_pr_bead(pr_bead)
                _work_bead = store.read_work_bead(issue_number)
                if _work_bead is not None:
                    _work_bead.state = "merge_ready"
                    store.write_work_bead(_work_bead)
            logger.log_conductor_event(
                run_id=checkpoint.run_id,
                phase="merge",
                event_type="pr_merged",
                payload={
                    "pr_number": pr_number,
                    "issue_number": issue_number,
                    "branch": branch,
                    "queued": True,
                },
                log_dir=config.log_dir.expanduser(),
            )
            return True

        elif ci_status == "fail":
            ci_fail_count += 1
            if ci_fail_count <= 1:
                # First failure: may be transient — poll once more
                pr_bead.ci_state = "failing"
                if store is not None:
                    store.write_pr_bead(pr_bead)
            else:
                # Persistent failure — escalate; agents should fix CI via updated skills (PR 5)
                pr_bead.state = "ci_failing"
                pr_bead.ci_state = "failing"
                if store is not None:
                    store.write_pr_bead(pr_bead)
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="ci_check",
                    event_type="human_escalate",
                    payload={
                        "pr_number": pr_number,
                        "issue_number": issue_number,
                        "reason": "ci failed (persistent); agent should fix via skill",
                        "ci_fail_count": ci_fail_count,
                    },
                    log_dir=config.log_dir.expanduser(),
                )
                return False

        # ci_status == "pending": continue polling

    # Timeout
    pr_bead.state = "abandoned"
    if store is not None:
        store.write_pr_bead(pr_bead)
    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="ci_check",
        event_type="human_escalate",
        payload={
            "pr_number": pr_number,
            "issue_number": issue_number,
            "reason": "ci monitoring timeout",
        },
        log_dir=config.log_dir.expanduser(),
    )
    return False


def _process_merge_queue(
    repo: str,
    config: Config,
    checkpoint: Checkpoint,
    store: BeadStore,
    default_branch: str,
    repo_root: str,
) -> None:
    """Drain the MergeQueue — attempt to merge each enqueued PR in order.

    For each entry in the queue:
    1. Attempt rebase onto the default branch (in a temporary worktree).
    2. If rebase fails, push the entry to the tail and break (Watchdog handles
       repeated failures).
    3. If rebase succeeds, squash-merge and write merged bead state.

    Args:
        repo:           Repository in ``owner/repo`` format.
        config:         Validated Config instance.
        checkpoint:     Active Checkpoint instance (for logging).
        store:          Active BeadStore for reading/writing bead state.
        default_branch: Default branch name for rebase target.
        repo_root:      Absolute path to the local repo clone for worktrees.
    """
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    queue = store.read_merge_queue()

    # Self-heal: re-enqueue any merge_ready PRBeads that are not in the queue.
    # This recovers from sessions that updated bead state but crashed or exited
    # before writing the merge queue entry (e.g. temp repo_root was cleaned up).
    queued_pr_numbers = {e.pr_number for e in queue.queue}
    orphaned = [
        b for b in store.list_pr_beads(state="merge_ready") if b.pr_number not in queued_pr_numbers
    ]
    if orphaned:
        now_str = datetime.now(UTC).isoformat()
        for pr_bead in orphaned:
            queue.queue.append(
                MergeQueueEntry(
                    pr_number=pr_bead.pr_number,
                    issue_number=pr_bead.issue_number,
                    branch=pr_bead.branch,
                    enqueued_at=now_str,
                )
            )
        store.write_merge_queue(queue)

    if not queue.queue:
        return

    remaining: list[MergeQueueEntry] = list(queue.queue)

    while remaining:
        entry = remaining.pop(0)
        pr_number = entry.pr_number
        issue_number = entry.issue_number
        branch = entry.branch

        # Attempt rebase in a temporary worktree
        wt_path = _checkout_existing_branch_worktree(branch, repo_root) or ""
        if wt_path:
            try:
                rebase_ok = _rebase_branch(branch, repo, wt_path, default_branch, config=config)
            finally:
                _remove_worktree(wt_path, repo_root)
            if not rebase_ok:
                # Rebase failed — push to tail, stop processing
                remaining.append(entry)
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="merge",
                    event_type="human_escalate",
                    payload={
                        "pr_number": pr_number,
                        "issue_number": issue_number,
                        "reason": "rebase failed in merge queue; pushed to tail",
                    },
                    log_dir=config.log_dir.expanduser(),
                )
                break

        # Squash-merge — retry a few times to handle GitHub's eventual-consistency
        # window: after a rebase force-push (or after sibling PRs merged first),
        # the GraphQL mergeability can briefly return "not mergeable" even when
        # gh pr view shows mergeStateStatus=CLEAN.
        _merge_delays = [3, 8, 15]
        merge_result = None
        for _delay in [0] + _merge_delays:
            if _delay:
                time.sleep(_delay)
            merge_result = _gh(
                ["pr", "merge", str(pr_number), "--squash", "--delete-branch"],
                repo=repo,
                check=False,
            )
            if merge_result.returncode == 0:
                break
            if "not mergeable" not in (merge_result.stderr or "").lower():
                break  # non-retriable error

        if merge_result is not None and merge_result.returncode == 0:
            now_str = datetime.now(UTC).isoformat()
            pr_bead = store.read_pr_bead(pr_number)
            if pr_bead is not None:
                pr_bead.state = "merged"
                pr_bead.merged_at = now_str
                store.write_pr_bead(pr_bead)
            work_bead = store.read_work_bead(issue_number)
            if work_bead is not None:
                work_bead.state = "closed"
                work_bead.closed_at = now_str
                store.write_work_bead(work_bead)
            session.save(checkpoint, checkpoint_path)
            store.flush(f"brimstone: #{issue_number} merged via pr-{pr_number}")
            logger.log_conductor_event(
                run_id=checkpoint.run_id,
                phase="merge",
                event_type="pr_merged",
                payload={
                    "pr_number": pr_number,
                    "issue_number": issue_number,
                    "branch": branch,
                },
                log_dir=config.log_dir.expanduser(),
            )
        else:
            logger.log_conductor_event(
                run_id=checkpoint.run_id,
                phase="merge",
                event_type="human_escalate",
                payload={
                    "pr_number": pr_number,
                    "issue_number": issue_number,
                    "reason": "squash merge failed",
                    "stderr": (merge_result.stderr or "")[:500] if merge_result is not None else "",
                },
                log_dir=config.log_dir.expanduser(),
            )
            # Remove from queue — won't be retried automatically
            # (Watchdog or human intervention needed)

    # Write updated queue (with processed entries removed, remaining intact)
    queue.queue = remaining
    queue.updated_at = datetime.now(UTC).isoformat()
    store.write_merge_queue(queue)


def _dispatch_recovery_agent(
    pr_bead: PRBead,
    work_bead: WorkBead,
    repo: str,
    config: Config,
    checkpoint: Checkpoint,
    store: BeadStore,
) -> None:
    """Dispatch a recovery sub-agent for a zombie PR (timed-out, no active future).

    Gathers PR diff, review comments, and latest CI logs, then runs a fix
    agent.  Increments ``pr_bead.fix_attempts`` and writes the updated bead
    before returning so repeated failures are capped by
    :data:`WATCHDOG_MAX_FIX_ATTEMPTS`.

    Args:
        pr_bead:    The PRBead for the stalled PR.
        work_bead:  The WorkBead for the stalled issue.
        repo:       Repository in ``owner/repo`` format.
        config:     Validated Config instance.
        checkpoint: Active Checkpoint instance (for logging).
        store:      Active BeadStore for bead updates.
    """
    pr_number = pr_bead.pr_number
    branch = pr_bead.branch
    issue_number = pr_bead.issue_number

    # Gather context
    diff_result = _gh(
        ["pr", "diff", str(pr_number)],
        repo=repo,
        check=False,
    )
    diff_text = (diff_result.stdout or "")[:3000]

    reviews_result = _gh(
        ["pr", "view", str(pr_number), "--json", "reviews,comments"],
        repo=repo,
        check=False,
    )
    reviews_text = (reviews_result.stdout or "")[:2000]

    run_list = _gh(
        ["run", "list", "--branch", branch, "--limit", "1", "--json", "databaseId"],
        repo=repo,
        check=False,
    )
    failure_logs = "(could not retrieve CI logs)"
    try:
        runs = json.loads(run_list.stdout or "[]")
        if runs:
            run_id_val = runs[0]["databaseId"]
            log_result = _gh(
                ["run", "view", str(run_id_val), "--log-failed"],
                repo=repo,
                check=False,
            )
            if log_result.returncode == 0 and log_result.stdout:
                failure_logs = log_result.stdout[:4000]
    except (json.JSONDecodeError, KeyError, IndexError):
        pass

    issue_result = _gh(
        ["issue", "view", str(issue_number), "--json", "title,body"],
        repo=repo,
        check=False,
    )
    issue_text = (issue_result.stdout or "")[:2000]

    prompt = (
        f"You are recovering a stalled PR #{pr_number} in repository `{repo}`.\n"
        f"Branch: `{branch}`\n"
        f"Original issue #{issue_number}:\n```\n{issue_text}\n```\n\n"
        f"PR diff (truncated):\n```diff\n{diff_text}\n```\n\n"
        f"Review comments:\n```json\n{reviews_text}\n```\n\n"
        f"Latest CI failure logs:\n```\n{failure_logs}\n```\n\n"
        f"Steps:\n"
        f"1. Clone the repo: WORK=$(mktemp -d) && gh repo clone {repo} $WORK\n"
        f"2. cd $WORK && git checkout {branch} && git pull origin {branch}\n"
        f"3. Diagnose issues from the diff, reviews, and CI logs above.\n"
        f"4. Fix ALL issues in a single commit. Stay within files already on the branch.\n"
        f"5. Verify: uv run ruff check && uv run pytest\n"
        f"6. git add -A && git commit -m 'fix: watchdog recovery on PR #{pr_number}' && git push\n"
        f"7. STOP. Output exactly: Done.\n"
    )

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="watchdog",
        event_type="recovery_dispatched",
        payload={
            "pr_number": pr_number,
            "issue_number": issue_number,
            "branch": branch,
            "fix_attempts": pr_bead.fix_attempts,
        },
        log_dir=config.log_dir.expanduser(),
    )

    env = build_subprocess_env(config)
    runner.run(
        prompt=prompt,
        allowed_tools=["Bash"],
        env=env,
        max_turns=60,
        timeout_seconds=config.agent_timeout_minutes * 60,
        model=config.model,
        prefix=f"[watchdog {branch}] ",
    )

    pr_bead.fix_attempts += 1
    store.write_pr_bead(pr_bead)


def _watchdog_scan(
    repo: str,
    config: Config,
    checkpoint: Checkpoint,
    store: BeadStore,
    active_issue_numbers: set[int],
    default_branch: str,
) -> None:
    """Scan for zombie PRs and dispatch recovery agents or exhaust issues.

    A zombie is a WorkBead with ``state="claimed"`` and ``claimed_at`` older
    than :data:`WATCHDOG_TIMEOUT_MINUTES` whose ``issue_number`` is NOT in the
    set of currently-active futures.

    Three scan passes are performed:

    1. **PRBead zombies** — claimed beads that have a PR but the agent is gone.
       If ``pr_bead.state == "conflict"``, reset to open for ``_resume_stale_issues``
       to retry.  Otherwise, dispatch a recovery agent or exhaust the issue.

    2. **Pre-PR zombies** — claimed beads that never created a PR.  These are
       unclaimed so the scheduler can re-queue the issue.

    3. **Stuck merge queue** — escalate to a human if the queue head has not
       advanced within :data:`WATCHDOG_TIMEOUT_MINUTES`.

    Args:
        repo:                 Repository in ``owner/repo`` format.
        config:               Validated Config instance.
        checkpoint:           Active Checkpoint instance (for logging).
        store:                Active BeadStore for reading/writing bead state.
        active_issue_numbers: Issue numbers currently being processed by live futures.
        default_branch:       Default branch name (unused here, kept for future use).
    """
    for pr_bead in store.list_pr_beads():
        if pr_bead.state == "merged":
            continue
        if pr_bead.issue_number in active_issue_numbers:
            continue  # agent is still running — not a zombie

        work_bead = store.read_work_bead(pr_bead.issue_number)
        if not work_bead or not work_bead.claimed_at:
            continue

        claimed_dt = datetime.fromisoformat(work_bead.claimed_at)
        elapsed_min = (datetime.now(UTC) - claimed_dt).total_seconds() / 60
        if elapsed_min < WATCHDOG_TIMEOUT_MINUTES:
            continue

        # Zombie detected
        logger.log_conductor_event(
            run_id=checkpoint.run_id,
            phase="watchdog",
            event_type="zombie_detected",
            payload={
                "issue_number": pr_bead.issue_number,
                "pr_number": pr_bead.pr_number,
                "elapsed_minutes": round(elapsed_min, 1),
                "fix_attempts": pr_bead.fix_attempts,
            },
            log_dir=config.log_dir.expanduser(),
        )

        # Gap 2 — Conflict state mismatch: reset instead of dispatching a
        # recovery agent.  _resume_stale_issues will pick this up and retry
        # conflict resolution correctly.
        if pr_bead.state == "conflict":
            pr_bead.fix_attempts = 0
            pr_bead.state = "open"
            store.write_pr_bead(pr_bead)
            logger.log_conductor_event(
                run_id=checkpoint.run_id,
                phase="watchdog",
                event_type="conflict_reset",
                payload={
                    "issue_number": pr_bead.issue_number,
                    "pr_number": pr_bead.pr_number,
                },
                log_dir=config.log_dir.expanduser(),
            )
            continue

        if pr_bead.fix_attempts >= WATCHDOG_MAX_FIX_ATTEMPTS:
            reason = f"watchdog: max fix attempts ({WATCHDOG_MAX_FIX_ATTEMPTS}) exceeded"
            _exhaust_issue(repo, pr_bead.issue_number, reason, store)
        else:
            _dispatch_recovery_agent(pr_bead, work_bead, repo, config, checkpoint, store)

    # Gap 1 — Pre-PR zombies: claimed beads that never created a PR.
    # These agents died before opening a PR, so there is no PRBead to scan
    # above.  Unclaim the issue so the scheduler can re-queue it.
    for work_bead in store.list_work_beads(state="claimed"):
        if work_bead.issue_number in active_issue_numbers:
            continue
        if work_bead.pr_id is not None:
            continue  # has a PR — already covered by the PRBead loop above
        if not work_bead.claimed_at:
            continue
        claimed_dt = datetime.fromisoformat(work_bead.claimed_at)
        elapsed_min = (datetime.now(UTC) - claimed_dt).total_seconds() / 60
        if elapsed_min < WATCHDOG_TIMEOUT_MINUTES:
            continue

        logger.log_conductor_event(
            run_id=checkpoint.run_id,
            phase="watchdog",
            event_type="zombie_detected",
            payload={
                "issue_number": work_bead.issue_number,
                "pr_number": None,
                "elapsed_minutes": round(elapsed_min, 1),
                "reason": "pre_pr_zombie",
            },
            log_dir=config.log_dir.expanduser(),
        )
        _unclaim_issue(repo, work_bead.issue_number, store)

    # Gap 3 — Stuck merge queue: escalate if the head entry has not advanced
    # within WATCHDOG_TIMEOUT_MINUTES.
    queue = store.read_merge_queue()
    if queue.queue:
        try:
            oldest_dt = datetime.fromisoformat(queue.queue[0].enqueued_at)
            stuck_min = (datetime.now(UTC) - oldest_dt).total_seconds() / 60
            if stuck_min > WATCHDOG_TIMEOUT_MINUTES:
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="watchdog",
                    event_type="human_escalate",
                    payload={
                        "reason": "merge queue head stuck",
                        "pr_number": queue.queue[0].pr_number,
                        "issue_number": queue.queue[0].issue_number,
                        "stuck_minutes": round(stuck_min, 1),
                    },
                    log_dir=config.log_dir.expanduser(),
                )
        except (ValueError, AttributeError, TypeError):
            pass


def _create_worktree(branch: str, repo_root: str, default_branch: str = "main") -> str | None:
    """Create a git worktree for *branch* under ``.claude/worktrees/``.

    The branch is created from ``origin/<default_branch>``.  The agent is
    responsible for pushing the branch after making commits.

    Args:
        branch:         Branch name (e.g. ``"42-add-config"``).
        repo_root:      Absolute path to the repository root.
        default_branch: Name of the default branch to base the new branch on.

    Returns:
        Absolute path to the new worktree directory, or ``None`` on failure.
    """
    worktree_dir = os.path.join(repo_root, ".claude", "worktrees", branch)

    # Clean up any stale state from a previous attempt with the same branch name.
    # ``git worktree remove`` deletes the worktree directory and its tracking
    # entry but does NOT delete the branch — we must do that separately.
    subprocess.run(
        ["git", "worktree", "remove", "--force", worktree_dir],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    # Fetch so origin/<default_branch> is up to date before branching.
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    # Create worktree with new branch based on origin/<default_branch>
    result = subprocess.run(
        ["git", "worktree", "add", worktree_dir, "-b", branch, f"origin/{default_branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    # Push the branch to origin so agents can reference it and PRs are tracked.
    subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=worktree_dir,
        capture_output=True,
        text=True,
    )

    return worktree_dir


def _checkout_existing_branch_worktree(branch: str, repo_root: str) -> str | None:
    """Create a git worktree tracking an already-existing remote branch.

    Unlike _create_worktree, does NOT create a new branch — checks out
    the already-existing remote branch into a new worktree directory.

    Used by _resume_stale_issues so that conflict detection and rebase
    work correctly for branches pushed by a previous session.

    Args:
        branch:    Branch name (e.g. ``"57-add-calculator"``).
        repo_root: Absolute path to the repository root.

    Returns:
        Absolute path to the new worktree directory, or ``None`` on failure.
    """
    worktree_dir = os.path.join(repo_root, ".claude", "worktrees", branch)

    # Remove any stale worktree entry for this path.
    subprocess.run(
        ["git", "worktree", "remove", "--force", worktree_dir],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    # Fetch so origin/<branch> is up to date.
    subprocess.run(
        ["git", "fetch", "origin"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )

    # Create worktree checking out the existing remote branch.
    result = subprocess.run(
        ["git", "worktree", "add", worktree_dir, "-b", branch, f"origin/{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "already exists" in result.stderr:
        # Local branch exists from a previous session — check it out directly.
        result = subprocess.run(
            ["git", "worktree", "add", worktree_dir, branch],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
    return worktree_dir if result.returncode == 0 else None


def _remove_worktree(worktree_path: str, repo_root: str) -> None:
    """Force-remove a git worktree.

    Args:
        worktree_path: Absolute path to the worktree directory.
        repo_root:     Absolute path to the repository root.
    """
    subprocess.run(
        ["git", "worktree", "remove", "--force", worktree_path],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )


def _get_repo_root() -> str:
    """Return the absolute path to the current git repository root."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    return os.getcwd()


def _ensure_worktree_repo(repo: str) -> tuple[str, str]:
    """Clone *repo* to a temp directory and return ``(repo_root, tmp_parent_to_cleanup)``.

    Clones the remote repo (owner/name) to a fresh temp directory so that
    worktrees are created inside the correct repository.  The caller must
    delete ``tmp_parent_to_cleanup`` when done.

    Args:
        repo: GitHub repository in ``owner/name`` format.

    Returns:
        ``(clone_path, tmp_parent)`` where ``tmp_parent`` is the temp directory
        to remove after the caller is done.
    """
    parent = tempfile.mkdtemp(prefix="brimstone-")
    repo_name = repo.split("/")[-1]
    clone_path = os.path.join(parent, repo_name)
    result = subprocess.run(
        ["gh", "repo", "clone", repo, clone_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        shutil.rmtree(parent, ignore_errors=True)
        raise click.ClickException(
            f"Failed to clone {repo} for worktree operations: {result.stderr.strip()}"
        )
    return clone_path, parent


def _dispatch_design_agent(
    issue: dict[str, Any],
    branch: str,
    worktree_path: str,
    skill_name: str,
    module_name: str | None,
    repo: str,
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
) -> tuple[dict[str, Any], str, str, runner.RunResult]:
    """Dispatch a single design agent (HLD or LLD) in its worktree.

    Builds the design-agent prompt, calls runner.run(), and returns the outcome.
    Designed to be called from a ThreadPoolExecutor worker for LLD agents.

    Args:
        issue:        Issue dict with number, title, body.
        branch:       Branch name for the agent.
        worktree_path: Absolute path to the agent's worktree.
        skill_name:   ``"design-worker-hld"`` or ``"design-worker-lld"``.
        module_name:  Module name for LLD agents; ``None`` for HLD.
        repo:         Repository in ``owner/repo`` format.
        milestone:    Active milestone name.
        config:       Config instance.
        checkpoint:   Checkpoint instance (read-only in thread).

    Returns:
        Tuple of (issue, branch, worktree_path, run_result).
    """
    issue_number = issue["number"]
    today = date.today().isoformat()

    if module_name:
        issue_body = issue.get("body") or ""
        prompt = (
            f"## Headless Autonomous Mode\n"
            f"You are running in a fully automated headless pipeline. No human is present.\n"
            f"- Use tools directly and silently. No conversational text between calls.\n"
            f"- Do NOT explain what you are about to do. Do NOT narrate your reasoning.\n"
            f"## MANDATORY: Working Directory\n"
            f"You are working in an isolated git worktree. Your FIRST action must be:\n"
            f"```bash\n"
            f"cd {worktree_path}\n"
            f"```\n"
            f"ALL file reads, writes, and git operations must happen inside `{worktree_path}`.\n"
            f"The branch `{branch}` is already checked out there.\n"
            f"Do NOT explore the repo broadly.\n"
            f"Do NOT run `git checkout` or `git rebase`.\n"
            f"Read ONLY the files specified in the skill instructions.\n\n"
            f"## Session Parameters\n"
            f"- Repository: {repo}\n"
            f"- Milestone: {milestone}\n"
            f"- Module: {module_name}\n"
            f"- Issue: #{issue_number}\n"
            f"- Branch: {branch}\n"
            f"- Working Directory: {worktree_path}\n"
            f"- Session Date: {today}\n\n"
            f"You are the design-worker LLD agent for module `{module_name}` in `{repo}`.\n"
            f"Write the Low-Level Design document for this module following the skill "
            f"instructions in your system prompt.\n\n"
            f"## Issue description\n"
            f"{issue_body}\n\n"
            f"## MANDATORY: Required Completion Steps\n"
            f"After writing the LLD document:\n\n"
            f"**Step A — Commit:**\n"
            f"```bash\n"
            f"cd {worktree_path}\n"
            f"git add docs/design/{milestone}/lld/{module_name}.md\n"
            f'git commit -m "docs: add LLD for {module_name} (Closes #{issue_number}) [skip ci]"\n'
            f"```\n\n"
            f"**Step B — Push (REQUIRED, do not skip):**\n"
            f"```bash\n"
            f"git push -u origin {branch}\n"
            f"```\n\n"
            f"**Step C — Create PR (REQUIRED, do not skip):**\n"
            f"```bash\n"
            f"gh pr create --repo {repo} \\\n"
            f'  --title "Design: LLD for {module_name} ({milestone})" \\\n'
            f'  --label "stage/design" \\\n'
            f'  --body "Closes #{issue_number}\\n\\n## Summary\\n<1-3 sentences>"\n'
            f"```\n\n"
            f"Steps B and C are NOT optional. Execute them immediately without pausing.\n"
        )
        prefix = f"[design-lld #{issue_number}] "
    else:
        prompt = (
            f"## Headless Autonomous Mode\n"
            f"You are running in a fully automated headless pipeline. No human is present.\n"
            f"- Use tools directly and silently. No conversational text between calls.\n"
            f"- Do NOT explain what you are about to do. Do NOT narrate your reasoning.\n"
            f"## MANDATORY: Working Directory\n"
            f"You are working in an isolated git worktree. Your FIRST action must be:\n"
            f"```bash\n"
            f"cd {worktree_path}\n"
            f"```\n"
            f"ALL file reads, writes, and git operations must happen inside `{worktree_path}`.\n"
            f"The branch `{branch}` is already checked out there.\n"
            f"Do NOT run `git checkout` or `git rebase`.\n\n"
            f"## Session Parameters\n"
            f"- Repository: {repo}\n"
            f"- Milestone: {milestone}\n"
            f"- Issue: #{issue_number}\n"
            f"- Branch: {branch}\n"
            f"- Working Directory: {worktree_path}\n"
            f"- Session Date: {today}\n\n"
            f"You are the design-worker HLD agent for `{repo}`.\n"
            f"Write the High-Level Design document for milestone `{milestone}` "
            f"following the skill instructions in your system prompt."
        )
        prefix = f"[design-hld #{issue_number}] "

    label = (
        f"design-hld-{issue_number}"
        if module_name is None
        else f"design-lld-{module_name}-{issue_number}"
    )
    result = _run_agent(
        prompt,
        skill_name,
        ["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
        200,
        label,
        prefix,
        config,
        issue_number,
        model=config.model,
    )
    return issue, branch, worktree_path, result


def _dispatch_impl_agent(
    issue: dict[str, Any],
    branch: str,
    worktree_path: str,
    module: str,
    repo: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
) -> tuple[dict[str, Any], str, str, Any]:
    """Dispatch a single impl agent for *issue* in its worktree.

    Builds the impl-agent prompt, calls runner.run(), and returns the outcome.
    This function is designed to be called from a ThreadPoolExecutor worker.

    Args:
        issue:         Issue dict with number, title, body, labels.
        branch:        Branch name for the agent.
        worktree_path: Absolute path to the agent's worktree.
        module:        Module name extracted from the feat:* label.
        repo:          Repository in ``owner/repo`` format.
        config:        Config instance.
        checkpoint:    Checkpoint instance (read-only in thread).
        dry_run:       If True, return a mock success result without executing.

    Returns:
        Tuple of (issue, branch, worktree_path, run_result).
    """
    issue_number = issue["number"]
    issue_title = issue.get("title", "")
    raw_body = issue.get("body") or ""
    body = _sanitize_issue_body(raw_body)

    # Get the feat label to use for the PR (module-agnostic; target repo may have
    # different module names than brimstone — the issue body's Scope section is
    # the authoritative list of files to touch).
    feat_label = f"feat:{module},stage/impl" if module != "none" else "stage/impl"

    # Build the agent prompt
    base_prompt = (
        f"## Headless Autonomous Mode\n"
        f"You are running in a fully automated headless pipeline. No human is present.\n"
        f"- Use tools directly and silently. Do NOT produce conversational text between calls.\n"
        f"- Do NOT explain what you are about to do. Do NOT narrate your reasoning.\n"
        f"## Working directory\n"
        f"Your isolated worktree is already checked out at:\n"
        f"  {worktree_path}\n\n"
        f"Your FIRST action must be:\n"
        f"  cd {worktree_path}\n\n"
        f"ALL file writes and git operations must happen inside that directory.\n"
        f"Do NOT write to /tmp, ~/, or the main repo checkout.\n\n"
        f"## Dependencies\n"
        f"All issues listed in 'Depends on' are already merged to the default branch.\n"
        f"Your worktree was branched from the default branch tip and already contains\n"
        f"all merged dependency work. Do NOT cherry-pick, merge, pull from, or inspect\n"
        f"other branches. Implement your issue from the code that is already present.\n\n"
        f"## Task\n"
        f"You are implementing issue #{issue_number} on branch `{branch}`.\n"
        f"Repository: {repo}\n"
        f"Task: {issue_title}\n"
        f"Allowed scope: see ## Scope in the issue body below.\n\n"
        f"## Steps\n"
        f"1. cd {worktree_path}   (branch `{branch}` is already checked out)\n"
        f"2. Read the issue: gh issue view {issue_number} --repo {repo}\n"
        f"3. Implement the changes within the scope listed in the issue body\n"
        f"4. Update README if it exists at the package/module root\n"
        f"5. Run tests — all tests must pass\n"
        f"6. Run lint — must be clean\n"
        f"7. Commit with message referencing the issue\n"
        f"8. git push -u origin {branch}\n"
        f'9. Create PR: gh pr create --repo {repo} --title "{issue_title}" '
        f'--label "{feat_label}" '
        f'--body "Closes #{issue_number}\\n\\n'
        f"## Summary\\n<1-3 sentences: what was implemented and key decisions>\\n\\n"
        f'## Test plan\\n<bullet list of what was tested>"\n'
        f"10. After `gh pr create`, poll CI (max 60 attempts × 30s = 30 min):\n"
        f"    Loop: gh pr checks <PR-number> --json name,bucket --jq '[.[] | {{name,bucket}}]'\n"
        f"    Wait 30s between polls: sleep 30\n"
        f"    If any check has bucket='fail': read the logs:\n"
        f"      gh run list --repo {repo} --branch {branch}"
        f" --json databaseId --jq '.[0].databaseId'\n"
        f"      gh run view <run-id> --log-failed --repo {repo}\n"
        f"    Fix the failure, push ONE commit. Max 3 CI fix attempts.\n"
        f"    If still failing after 3 attempts: leave a PR comment explaining, then STOP.\n"
        f"      gh pr comment <PR-number> --repo {repo} --body "
        f'"brimstone: CI still failing after 3 fix attempts. Manual investigation needed."\n'
        f"11. Once CI is clean, check reviews:\n"
        f"    gh pr view <PR-number> --repo {repo} --json reviews,comments\n"
        f"    gh api repos/{repo}/pulls/<PR-number>/comments\n"
        f"    If CHANGES_REQUESTED: collect ALL feedback."
        f" Fix ALL in ONE commit. Never push per-comment.\n"
        f"      gh pr review --request <reviewer-login> --repo {repo} for re-review\n"
        f"    Never dismiss human reviews — CHANGES_REQUESTED from a human requires re-approval.\n"
        f"    Max 2 review fix attempts."
        f" If still CHANGES_REQUESTED after 2: leave a comment, STOP.\n"
        f"12. When CI passes + no CHANGES_REQUESTED outstanding:\n"
        f"    Output exactly one line: Done.\n"
        f"    Do NOT merge. The orchestrator handles merging.\n\n"
        f"## Issue body\n{body}"
    )
    if dry_run:
        return (
            issue,
            branch,
            worktree_path,
            runner.RunResult(
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
            ),
        )

    result = _run_agent(
        base_prompt,
        "impl-worker",
        runner.TOOLS_IMPL_AGENT,
        100,
        f"impl-{issue_number}",
        f"[implement #{issue_number}] ",
        config,
        issue_number,
    )
    return issue, branch, worktree_path, result


_SCAFFOLD_TITLE = "impl: project scaffold — pyproject.toml, package init, Makefile"
_SCAFFOLD_BODY = """\
## Context
Establishes the project scaffold so parallel module impl workers never conflict on shared files.
Must merge before any other impl issue begins.

## Acceptance Criteria
- [ ] `pyproject.toml` exists with correct package name, dependencies, and dev extras
- [ ] `src/<pkg>/__init__.py` exists (may be empty)
- [ ] `Makefile` exists with `test` and `lint` targets
- [ ] `make test && make lint` pass on a clean checkout
- [ ] README.md exists with package name, one-line description, and basic usage

## Scope
Files to create or modify:
- `pyproject.toml` — package metadata, dependencies, dev tooling
- `src/<pkg>/__init__.py` — package init
- `Makefile` — test and lint targets
- `README.md` — minimal project readme

## Test Requirements
- `make test` exits 0
- `make lint` exits 0

## Dependencies
None — this is the foundation issue.

## Key Design Decisions
- All other impl workers are forbidden from creating `pyproject.toml` or package __init__.py;
  they add module files only and declare deps in pyproject.toml via PR review if needed.
"""


def _ensure_impl_scaffold(
    repo: str,
    milestone: str,
    store: BeadStore,
) -> int | None:
    """Ensure a scaffold impl issue exists and all other impl beads block on it.

    Idempotent: if a scaffold issue (title contains "scaffold") already exists,
    uses its number. Otherwise creates one. Then ensures every non-scaffold impl
    bead has the scaffold issue number in its ``blocked_by`` list.

    Returns the scaffold issue number, or None on failure.
    """
    result = _gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            IMPL_LABEL,
            "--milestone",
            milestone,
            "--json",
            "number,title",
            "--limit",
            "200",
        ],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        issues: list[dict] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    scaffold = next(
        (i for i in issues if "scaffold" in i.get("title", "").lower()),
        None,
    )
    if scaffold is None:
        # Ensure `infra` label exists (may be absent in repos initialized before it
        # was added to _REQUIRED_LABELS).
        _gh(
            [
                "label",
                "create",
                "infra",
                "--color",
                "bfd4f2",
                "--description",
                "Infrastructure and tooling work",
                "--force",
            ],
            repo=repo,
            check=False,
        )
        create = _gh(
            [
                "issue",
                "create",
                "--title",
                _SCAFFOLD_TITLE,
                "--label",
                f"infra,{IMPL_LABEL},P1",
                "--milestone",
                milestone,
                "--body",
                _SCAFFOLD_BODY,
            ],
            repo=repo,
            check=False,
        )
        if create.returncode != 0:
            click.echo(
                f"[impl-worker] Warning: could not create scaffold issue: {create.stderr}",
                err=True,
            )
            return None
        url = create.stdout.strip()
        try:
            scaffold_number = int(url.split("/")[-1])
        except (ValueError, IndexError):
            return None
        click.echo(f"[impl-worker] Created scaffold issue #{scaffold_number}", err=True)
    else:
        scaffold_number = scaffold["number"]

    # Seed beads so newly created scaffold issue gets a bead
    _seed_work_beads(repo, milestone, IMPL_LABEL, "impl", store)

    # Ensure every non-scaffold impl bead blocks on the scaffold
    for bead in store.list_work_beads(milestone=milestone, stage="impl"):
        if bead.issue_number == scaffold_number:
            continue
        if scaffold_number not in bead.blocked_by:
            bead.blocked_by = [scaffold_number] + [
                n for n in bead.blocked_by if n != scaffold_number
            ]
            store.write_work_bead(bead)

    return scaffold_number


def _run_impl_worker(
    repo: str,
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
    store: BeadStore | None = None,
) -> None:
    """Main impl-worker loop.

    Claims implementation issues respecting module isolation, dispatches parallel
    agents via runner.run() using ThreadPoolExecutor, monitors CI, and squash-merges
    passing PRs.

    Args:
        repo:       GitHub repository in ``owner/repo`` format.
        milestone:  Active implementation milestone name (may be empty for all).
        config:     Validated Config instance.
        checkpoint: Active Checkpoint instance.
        dry_run:    If True, print invocations without executing.
    """
    gov = UsageGovernor(config, checkpoint)
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    active_modules: set[str] = set()  # module isolation tracker
    repo_root, _clone_dir = _ensure_worktree_repo(repo)
    default_branch = _get_default_branch_for_repo(repo) if not dry_run else config.default_branch

    impl_limits: dict[str, int] = {"pro": 2, "max": 3, "max20x": 5}
    pool_size = config.max_concurrency or impl_limits.get(config.subscription_tier, 3)

    # Resume: for any in-progress impl issues, find their PR and monitor it.
    # If no PR exists, unclaim so the issue can be re-dispatched cleanly.
    # Also scan open PRs directly in case the in-progress label was stripped.
    if not dry_run:
        handled = _resume_stale_issues(
            repo=repo,
            milestone=milestone,
            label=IMPL_LABEL,
            log_prefix="[impl-worker]",
            config=config,
            checkpoint=checkpoint,
            default_branch=default_branch,
            repo_root=repo_root,
            store=store,
        )
        _resume_open_prs(
            repo=repo,
            milestone=milestone,
            label=IMPL_LABEL,
            log_prefix="[impl-worker]",
            config=config,
            checkpoint=checkpoint,
            default_branch=default_branch,
            repo_root=repo_root,
            already_handled=handled,
            store=store,
        )
        _startup_dep_checks(_list_open_issues_by_label(repo, milestone, IMPL_LABEL), repo)
        if store is not None:
            _ensure_impl_scaffold(repo=repo, milestone=milestone, store=store)

    if dry_run:
        if store is not None:
            _seed_work_beads(repo, milestone, IMPL_LABEL, "impl", store)
            open_beads = store.list_work_beads(state="open", milestone=milestone, stage="impl")
            eligible = [
                b
                for b in open_beads
                if b.state == "open"
                and not b.deferred
                and all(
                    (dep_bead := store.read_work_bead(dep)) is not None
                    and dep_bead.state == "closed"
                    for dep in b.blocked_by
                )
            ]
            ranked_dry: list = sorted(
                eligible,
                key=lambda b: (_PRIORITY_ORDER.get(b.priority, 5), b.issue_number),
            )
            if not ranked_dry:
                click.echo("[dry-run] No open impl issues — implementation complete.")
            else:
                for bead in ranked_dry:
                    branch = f"{bead.issue_number}-{_slugify(bead.title[:40])}"
                    click.echo(
                        f"[dry-run] Would claim #{bead.issue_number}: {bead.title!r} "
                        f"(module={bead.module}, branch={branch})"
                    )
                click.echo("[dry-run] Would dispatch agents in parallel; stopping.")
        else:
            open_issues = _list_open_issues_by_label(repo, milestone, IMPL_LABEL)
            open_issue_numbers = {i.get("number", 0) for i in open_issues}
            unblocked = _filter_unblocked(open_issues, open_issue_numbers, store=store)
            ranked = _sort_issues(unblocked)
            if not ranked:
                click.echo("[dry-run] No open impl issues — implementation complete.")
            else:
                for issue in ranked:
                    issue_number = issue["number"]
                    issue_title = issue.get("title", "")
                    mod = _extract_module(issue)
                    branch = f"{issue_number}-{_slugify(issue_title)}"
                    click.echo(
                        f"[dry-run] Would claim #{issue_number}: {issue_title!r} "
                        f"(module={mod}, branch={branch})"
                    )
                click.echo("[dry-run] Would dispatch agents in parallel; stopping.")
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=pool_size) as executor:

        def _fill(active: dict) -> None:
            if store is not None:
                # Bead-based dispatch: seed from GitHub, then dispatch from bead store
                _seed_work_beads(repo, milestone, IMPL_LABEL, "impl", store)
                open_beads = store.list_work_beads(state="open", milestone=milestone, stage="impl")
                if not open_beads:
                    return
                eligible = [
                    b
                    for b in open_beads
                    if b.state == "open"
                    and not b.deferred
                    and all(
                        (dep_bead := store.read_work_bead(dep)) is not None
                        and dep_bead.state == "closed"
                        for dep in b.blocked_by
                    )
                ]
                if not eligible:
                    return
                active_issue_numbers = {slot[0]["number"] for slot in active.values()}
                ranked_beads = sorted(
                    eligible,
                    key=lambda b: (_PRIORITY_ORDER.get(b.priority, 5), b.issue_number),
                )
                for bead in ranked_beads:
                    if len(active) >= pool_size:
                        break
                    if not gov.can_dispatch(1):
                        break
                    if bead.issue_number in active_issue_numbers:
                        continue
                    mod = bead.module
                    if mod != "none" and mod in active_modules:
                        continue
                    branch = f"{bead.issue_number}-{_slugify(bead.title[:40])}"
                    _claim_issue(
                        repo=repo,
                        issue_number=bead.issue_number,
                        branch=branch,
                        store=store,
                    )
                    worktree_path = _create_worktree(branch, repo_root, default_branch)
                    if worktree_path is None:
                        _unclaim_issue(repo=repo, issue_number=bead.issue_number, store=store)
                        logger.log_conductor_event(
                            run_id=checkpoint.run_id,
                            phase="claim",
                            event_type="worktree_create_failed",
                            payload={"issue_number": bead.issue_number, "branch": branch},
                            log_dir=config.log_dir.expanduser(),
                        )
                        continue
                    # Fetch issue body from GitHub only at dispatch time
                    _view = _gh(
                        ["issue", "view", str(bead.issue_number), "--json", "body,number,title"],
                        repo=repo,
                        check=False,
                    )
                    try:
                        issue_data = json.loads(_view.stdout)
                    except (json.JSONDecodeError, AttributeError):
                        issue_data = {
                            "number": bead.issue_number,
                            "title": bead.title,
                            "body": "",
                        }
                    session.save(checkpoint, checkpoint_path)
                    logger.log_conductor_event(
                        run_id=checkpoint.run_id,
                        phase="claim",
                        event_type="issue_claimed",
                        payload={
                            "issue_number": bead.issue_number,
                            "title": bead.title,
                            "branch": branch,
                            "module": mod,
                        },
                        log_dir=config.log_dir.expanduser(),
                    )
                    gov.record_dispatch(1)
                    active_modules.add(mod)
                    future = executor.submit(
                        _dispatch_impl_agent,
                        issue=issue_data,
                        branch=branch,
                        worktree_path=worktree_path,
                        module=mod,
                        repo=repo,
                        config=config,
                        checkpoint=checkpoint,
                        dry_run=False,
                    )
                    active[future] = (issue_data, branch, worktree_path, mod)
                    active_issue_numbers.add(bead.issue_number)
            else:
                # Fallback: no bead store — use GitHub-based dispatch
                open_issues = _list_open_issues_by_label(repo, milestone, IMPL_LABEL)
                active_issue_numbers = {info[0]["number"] for info in active.values()}
                open_issue_numbers = {i.get("number", 0) for i in open_issues}
                unblocked = _filter_unblocked(open_issues, open_issue_numbers, store=store)
                ranked = _sort_issues(unblocked)
                for issue in ranked:
                    if len(active) >= pool_size:
                        break
                    if not gov.can_dispatch(1):
                        break
                    issue_number = issue["number"]
                    if issue_number in active_issue_numbers:
                        continue
                    mod = _extract_module(issue)
                    if mod != "none" and mod in active_modules:
                        continue
                    issue_title = issue.get("title", "")
                    branch = f"{issue_number}-{_slugify(issue_title)}"
                    _claim_issue(
                        repo=repo,
                        issue_number=issue_number,
                        issue=issue,
                        branch=branch,
                        store=store,
                    )
                    worktree_path = _create_worktree(branch, repo_root, default_branch)
                    if worktree_path is None:
                        _unclaim_issue(repo=repo, issue_number=issue_number, store=store)
                        logger.log_conductor_event(
                            run_id=checkpoint.run_id,
                            phase="claim",
                            event_type="worktree_create_failed",
                            payload={"issue_number": issue_number, "branch": branch},
                            log_dir=config.log_dir.expanduser(),
                        )
                        continue
                    session.save(checkpoint, checkpoint_path)
                    logger.log_conductor_event(
                        run_id=checkpoint.run_id,
                        phase="claim",
                        event_type="issue_claimed",
                        payload={
                            "issue_number": issue_number,
                            "title": issue_title,
                            "branch": branch,
                            "module": mod,
                        },
                        log_dir=config.log_dir.expanduser(),
                    )
                    gov.record_dispatch(1)
                    active_modules.add(mod)
                    future = executor.submit(
                        _dispatch_impl_agent,
                        issue=issue,
                        branch=branch,
                        worktree_path=worktree_path,
                        module=mod,
                        repo=repo,
                        config=config,
                        checkpoint=checkpoint,
                        dry_run=False,
                    )
                    active[future] = (issue, branch, worktree_path, mod)
                    active_issue_numbers.add(issue_number)

        def _on_success(issue: dict, branch: str, worktree_path: str) -> None:
            issue_number = issue["number"]
            pr_number = _find_pr_for_branch(repo, branch)
            if pr_number is None:
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="dispatch",
                    event_type="human_escalate",
                    payload={
                        "issue_number": issue_number,
                        "reason": "agent completed but no PR found",
                        "branch": branch,
                    },
                    log_dir=config.log_dir.expanduser(),
                )
                _unclaim_issue(repo=repo, issue_number=issue_number, store=store)
                _remove_worktree(worktree_path, repo_root)
                return
            session.save(checkpoint, checkpoint_path)
            logger.log_conductor_event(
                run_id=checkpoint.run_id,
                phase="dispatch",
                event_type="pr_created",
                payload={
                    "issue_number": issue_number,
                    "pr_number": pr_number,
                    "branch": branch,
                },
                log_dir=config.log_dir.expanduser(),
            )
            # Retry monitoring until the PR merges or we exhaust retries.
            # Each failed attempt re-enters _monitor_pr so that a conflict that
            # develops mid-flight (another PR merged while CI was running) is
            # caught on the next pass.
            monitor_attempts = 0
            merged = False
            while not merged and monitor_attempts < _REBASE_RETRY_LIMIT:
                merged = _monitor_pr(
                    pr_number=pr_number,
                    branch=branch,
                    repo=repo,
                    config=config,
                    checkpoint=checkpoint,
                    worktree_path=worktree_path,
                    issue_number=issue_number,
                    store=store,
                    default_branch=default_branch,
                )
                monitor_attempts += 1
                if not merged:
                    # Check if the PR is still open — if it was closed externally,
                    # stop retrying.
                    pr_info = _gh(
                        ["pr", "view", str(pr_number), "--json", "state"],
                        repo=repo,
                        check=False,
                    )
                    try:
                        pr_state = json.loads(pr_info.stdout).get("state", "")
                    except (json.JSONDecodeError, AttributeError):
                        pr_state = ""
                    if pr_state != "OPEN":
                        break
            if merged:
                _remove_worktree(worktree_path, repo_root)
            else:
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="merge",
                    event_type="human_escalate",
                    payload={
                        "issue_number": issue_number,
                        "pr_number": pr_number,
                        "reason": "CI monitoring did not result in merge",
                    },
                    log_dir=config.log_dir.expanduser(),
                )
            if store is not None:
                _process_merge_queue(
                    repo=repo,
                    config=config,
                    checkpoint=checkpoint,
                    store=store,
                    default_branch=default_branch,
                    repo_root=repo_root,
                )

        def _on_release(slot: tuple) -> None:
            active_modules.discard(slot[3])

        def _when_empty() -> bool:
            if store is not None:
                # Bead-based completion check
                _seed_work_beads(repo, milestone, IMPL_LABEL, "impl", store)
                all_beads = store.list_work_beads(milestone=milestone, stage="impl")
                # Guard: if no impl beads exist, scope may never have run
                if not all_beads:
                    total_all = _count_all_issues_by_label(repo, milestone, IMPL_LABEL)
                    if total_all == 0:
                        raise click.ClickException(
                            f"No stage/impl issues found for milestone '{milestone}' "
                            "(open or closed). The scope stage may not have run or "
                            "failed silently. Run `brimstone run --scope` first."
                        )
                open_beads = [b for b in all_beads if b.state not in ("closed", "abandoned")]
                if not open_beads:
                    logger.log_conductor_event(
                        run_id=checkpoint.run_id,
                        phase="complete",
                        event_type="stage_complete",
                        payload={"milestone": milestone},
                        log_dir=config.log_dir.expanduser(),
                    )
                    session.save(checkpoint, checkpoint_path)
                    click.echo(f"Implementation milestone '{milestone}' complete.")
                    return True
                # Only deferred beads remain — non-blocking for stage gate
                blocking = [b for b in open_beads if not b.deferred]
                if not blocking:
                    logger.log_conductor_event(
                        run_id=checkpoint.run_id,
                        phase="complete",
                        event_type="stage_complete",
                        payload={"milestone": milestone},
                        log_dir=config.log_dir.expanduser(),
                    )
                    session.save(checkpoint, checkpoint_path)
                    click.echo(f"Implementation milestone '{milestone}' complete.")
                    return True
                return False
            # Fallback: no bead store — use GitHub-based completion check
            total_open = _count_open_issues_by_label(repo, milestone, IMPL_LABEL)
            if total_open == 0:
                total_all = _count_all_issues_by_label(repo, milestone, IMPL_LABEL)
                if total_all == 0:
                    raise click.ClickException(
                        f"No stage/impl issues found for milestone '{milestone}' "
                        "(open or closed). The scope stage may not have run or "
                        "failed silently. Run `brimstone run --scope` first."
                    )
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="complete",
                    event_type="stage_complete",
                    payload={"milestone": milestone},
                    log_dir=config.log_dir.expanduser(),
                )
                session.save(checkpoint, checkpoint_path)
                click.echo(f"Implementation milestone '{milestone}' complete.")
                return True
            return False

        _run_persistent_pool(
            pool_size=pool_size,
            gov=gov,
            repo=repo,
            repo_root=repo_root,
            milestone=milestone,
            model=config.model,
            config=config,
            checkpoint=checkpoint,
            stage="impl",
            fill_fn=_fill,
            on_success=_on_success,
            when_empty_fn=_when_empty,
            on_release=_on_release,
            stall_reason="dep-blocked deadlock or all modules busy",
            store=store,
        )

    if _clone_dir:
        shutil.rmtree(_clone_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Design-worker helpers
# ---------------------------------------------------------------------------


def _run_design_worker(
    repo: str,
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
    store: BeadStore | None = None,
) -> None:
    """Run the two-phase design-worker: HLD first, then LLD agents in parallel.

    Phase 1 — HLD (sequential):
      Gate 1: all research issues for the milestone must be closed.
      Dispatches a single HLD agent to write ``docs/design/HLD.md``.
      The HLD agent also files ``Design: LLD for <module>`` issues.

    Phase 2 — LLDs (parallel):
      Gate 2: ``docs/design/HLD.md`` must exist on the default branch.
      Dispatches one LLD agent per open ``stage/design`` LLD issue in parallel.

    Both phases are idempotent: docs already merged on the default branch are
    skipped so re-running after an interruption resumes from where it left off.

    Args:
        repo:       GitHub repository in ``owner/repo`` format.
        milestone:  Active milestone name.
        config:     Validated Config instance.
        checkpoint: Active Checkpoint instance.
        dry_run:    If True, print planned actions without executing.
    """
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"

    # ------------------------------------------------------------------ #
    # Gate 1: No blocking research issues may remain                     #
    # ------------------------------------------------------------------ #
    if not dry_run:
        # Use _list_all_open_issues_by_label so in-progress research issues (those
        # with open PRs that haven't merged yet) are included in the blocking check.
        # _list_open_issues_by_label filters them out, making the gate a no-op when
        # all research is claimed but nothing has merged yet.
        open_research_issues = _list_all_open_issues_by_label(repo, milestone, RESEARCH_LABEL)
        blocking, _ = _classify_blocking_issues(
            open_research_issues, repo, milestone, config, checkpoint, store=store
        )
        if blocking:
            nums = ", ".join(f"#{i['number']}" for i in blocking)
            click.echo(
                f"Error: {len(blocking)} blocking research issue(s) still open for milestone "
                f"'{milestone}' ({nums}). All blocking research must complete before design "
                f"can begin.",
                err=True,
            )
            raise SystemExit(1)

        default_branch = _get_default_branch_for_repo(repo)
        repo_root, _clone_dir = _ensure_worktree_repo(repo)

        # Resume: for any in-progress design issues, find their PR and monitor it.
        # If no PR exists, unclaim so the issue can be re-dispatched cleanly.
        _resume_stale_issues(
            repo=repo,
            milestone=milestone,
            label=DESIGN_LABEL,
            log_prefix="[design-worker]",
            config=config,
            checkpoint=checkpoint,
            default_branch=default_branch,
            repo_root=repo_root,
            store=store,
        )
    else:
        _clone_dir = None

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="design_worker_start",
        payload={"repo": repo, "milestone": milestone},
        log_dir=config.log_dir.expanduser(),
    )

    # ------------------------------------------------------------------ #
    # Phase 1: HLD                                                        #
    # ------------------------------------------------------------------ #
    hld_doc_path = f"docs/design/{milestone}/HLD.md"
    hld_issue_title = f"Design: HLD for {milestone}"

    if dry_run:
        click.echo(
            f"[dry-run] Would dispatch HLD agent for milestone {milestone!r} in repo {repo!r}"
        )
    elif _doc_exists_on_default_branch(repo, hld_doc_path, default_branch):
        click.echo("HLD already merged — skipping Phase 1")
    else:
        # Find the HLD issue (may have been filed by completion gate or on a previous run)
        design_issues = _list_open_issues_by_label(repo, milestone, DESIGN_LABEL)
        hld_issue = next((i for i in design_issues if i["title"] == hld_issue_title), None)
        if hld_issue is None:
            # Missing — create it (idempotent) and re-fetch
            _file_design_issue_if_missing(
                repo,
                milestone,
                hld_issue_title,
                (
                    "## Deliverable\n"
                    f"`docs/design/{milestone}/HLD.md`\n\n"
                    "## Instructions\n"
                    f"Read all merged research docs in `docs/research/{milestone}/`. "
                    "Write the HLD. "
                    "For each module identified, file a `Design: LLD for <module>` "
                    "issue with label `stage/design` and this milestone. "
                    "Check for duplicates before filing."
                ),
            )
            design_issues = _list_open_issues_by_label(repo, milestone, DESIGN_LABEL)
            hld_issue = next((i for i in design_issues if i["title"] == hld_issue_title), None)

        if hld_issue is None:
            click.echo("Error: Could not find or create HLD design issue.", err=True)
            raise SystemExit(1)

        hld_number = hld_issue["number"]
        hld_branch = f"{hld_number}-{_slugify(hld_issue_title)}"

        _claim_issue(
            repo=repo,
            issue_number=hld_number,
            issue=hld_issue,
            branch=hld_branch,
            store=store,
        )
        worktree_path = _create_worktree(hld_branch, repo_root, default_branch)
        if worktree_path is None:
            _unclaim_issue(repo=repo, issue_number=hld_number, store=store)
            click.echo(f"Error: Failed to create worktree for branch {hld_branch!r}.", err=True)
            raise SystemExit(1)

        logger.log_conductor_event(
            run_id=checkpoint.run_id,
            phase="dispatch",
            event_type="design_hld_dispatch",
            payload={"issue_number": hld_number, "branch": hld_branch},
            log_dir=config.log_dir.expanduser(),
        )

        _, _, _, hld_result = _dispatch_design_agent(
            issue=hld_issue,
            branch=hld_branch,
            worktree_path=worktree_path,
            skill_name="design-worker-hld",
            module_name=None,
            repo=repo,
            milestone=milestone,
            config=config,
            checkpoint=checkpoint,
        )

        _log_agent_cost(
            hld_result,
            repo,
            "design",
            config,
            checkpoint,
            issue_number=hld_number,
            milestone=milestone,
            model=config.model,
        )

        if hld_result.is_error:
            _unclaim_issue(repo=repo, issue_number=hld_number, store=store)
            _remove_worktree(worktree_path, repo_root)
            click.echo(
                f"HLD agent failed: {hld_result.subtype} / {hld_result.error_code}",
                err=True,
            )
            raise SystemExit(1)

        pr_number = _find_pr_for_branch(repo, hld_branch)
        if pr_number is None:
            _unclaim_issue(repo=repo, issue_number=hld_number, store=store)
            _remove_worktree(worktree_path, repo_root)
            click.echo("Error: HLD agent completed but no PR found.", err=True)
            raise SystemExit(1)

        merged = _monitor_pr(
            pr_number=pr_number,
            branch=hld_branch,
            repo=repo,
            config=config,
            checkpoint=checkpoint,
            worktree_path=worktree_path,
            issue_number=hld_number,
            store=store,
            default_branch=default_branch,
        )
        _remove_worktree(worktree_path, repo_root)
        if store is not None:
            _process_merge_queue(
                repo=repo,
                config=config,
                checkpoint=checkpoint,
                store=store,
                default_branch=default_branch,
                repo_root=repo_root,
            )
        if not merged:
            click.echo(
                f"Error: HLD PR #{pr_number} did not merge. Manual intervention required.",
                err=True,
            )
            raise SystemExit(1)

        click.echo(f"HLD merged (PR #{pr_number}).")
        session.save(checkpoint, checkpoint_path)

    # ------------------------------------------------------------------ #
    # Gate 2: HLD must be on the default branch before LLD dispatch       #
    # ------------------------------------------------------------------ #
    if not dry_run and not _doc_exists_on_default_branch(repo, hld_doc_path, default_branch):
        click.echo(
            f"Error: {hld_doc_path!r} not found on branch {default_branch!r}. "
            "Phase 1 must complete before LLD agents can be dispatched.",
            err=True,
        )
        raise SystemExit(1)

    # ------------------------------------------------------------------ #
    # Phase 2: LLDs (parallel)                                           #
    # ------------------------------------------------------------------ #
    if dry_run:
        click.echo(f"[dry-run] Would dispatch LLD agents in parallel for milestone {milestone!r}")
        return

    # LLD issues were filed by the HLD agent; pick up all open ones
    all_design_issues = _list_open_issues_by_label(repo, milestone, DESIGN_LABEL)
    lld_issues = [i for i in all_design_issues if i["title"] != hld_issue_title]

    if not lld_issues:
        click.echo(
            "No open LLD design issues found. "
            "The HLD agent should have filed them. "
            "Check the HLD doc and file 'Design: LLD for <module>' issues manually.",
            err=True,
        )
        raise SystemExit(1)

    # Recovery: skip modules whose docs already exist on the default branch
    pending: list[tuple[str, dict[str, Any]]] = []
    for issue in lld_issues:
        module = _extract_module_from_design_issue(issue)
        lld_path = f"docs/design/{milestone}/lld/{module}.md"
        if _doc_exists_on_default_branch(repo, lld_path, default_branch):
            click.echo(f"LLD for {module!r} already merged — skipping")
            _gh(["issue", "close", str(issue["number"])], repo=repo, check=False)
        else:
            pending.append((module, issue))

    if not pending:
        click.echo("All LLD docs already merged — Phase 2 complete.")
    else:
        logger.log_conductor_event(
            run_id=checkpoint.run_id,
            phase="dispatch",
            event_type="design_lld_dispatch",
            payload={"milestone": milestone, "count": len(pending)},
            log_dir=config.log_dir.expanduser(),
        )

        lld_limits: dict[str, int] = {"pro": 2, "max": 3, "max20x": 5}
        lld_pool_size = config.max_concurrency or lld_limits.get(config.subscription_tier, 3)
        pending_iter = iter(pending)

        with concurrent.futures.ThreadPoolExecutor(max_workers=lld_pool_size) as executor:

            def _fill_lld(active: dict) -> None:
                """Claim and submit LLD agents until the pool is full or pending is exhausted."""
                while len(active) < lld_pool_size:
                    try:
                        module, issue = next(pending_iter)
                    except StopIteration:
                        break
                    issue_number = issue["number"]
                    branch = f"{issue_number}-{_slugify(issue['title'])}"
                    _claim_issue(
                        repo=repo,
                        issue_number=issue_number,
                        issue=issue,
                        branch=branch,
                        store=store,
                    )
                    worktree_path = _create_worktree(branch, repo_root, default_branch)
                    if worktree_path is None:
                        _unclaim_issue(repo=repo, issue_number=issue_number, store=store)
                        click.echo(
                            f"Warning: Failed to create worktree for {branch!r}"
                            f" — skipping LLD for {module!r}",
                            err=True,
                        )
                        continue
                    future = executor.submit(
                        _dispatch_design_agent,
                        issue=issue,
                        branch=branch,
                        worktree_path=worktree_path,
                        skill_name="design-worker-lld",
                        module_name=module,
                        repo=repo,
                        milestone=milestone,
                        config=config,
                        checkpoint=checkpoint,
                    )
                    active[future] = (issue, branch, worktree_path)

            def _on_lld_success(issue: dict, branch: str, worktree_path: str) -> None:
                module = _extract_module_from_design_issue(issue)
                issue_number = issue["number"]
                pr_number = _find_pr_for_branch(repo, branch)
                if pr_number is None:
                    click.echo(
                        f"LLD agent for {module!r} succeeded but no PR found.",
                        err=True,
                    )
                    _unclaim_issue(repo=repo, issue_number=issue_number, store=store)
                    _remove_worktree(worktree_path, repo_root)
                    return
                merged = _monitor_pr(
                    pr_number=pr_number,
                    branch=branch,
                    repo=repo,
                    config=config,
                    checkpoint=checkpoint,
                    worktree_path=worktree_path,
                    issue_number=issue_number,
                    store=store,
                    default_branch=default_branch,
                )
                _remove_worktree(worktree_path, repo_root)
                if merged:
                    _unclaim_issue(repo=repo, issue_number=issue_number, store=store)
                    click.echo(f"LLD for {module!r} merged (PR #{pr_number}).")
                else:
                    click.echo(
                        f"Warning: LLD PR #{pr_number} for {module!r} did not merge."
                        " Manual intervention required.",
                        err=True,
                    )
                if store is not None:
                    _process_merge_queue(
                        repo=repo,
                        config=config,
                        checkpoint=checkpoint,
                        store=store,
                        default_branch=default_branch,
                        repo_root=repo_root,
                    )

            def _when_lld_empty() -> bool:
                # pending is a pre-loaded list; once exhausted, we're done
                return True

            _run_persistent_pool(
                pool_size=lld_pool_size,
                gov=None,
                repo=repo,
                repo_root=repo_root,
                milestone=milestone,
                model=config.model,
                config=config,
                checkpoint=checkpoint,
                stage="design",
                fill_fn=_fill_lld,
                on_success=_on_lld_success,
                when_empty_fn=_when_lld_empty,
                store=store,
            )

    session.save(checkpoint, checkpoint_path)

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="complete",
        event_type="design_worker_complete",
        payload={"repo": repo, "milestone": milestone},
        log_dir=config.log_dir.expanduser(),
    )
    click.echo(f"Design-worker complete for milestone '{milestone}'.")

    if _clone_dir:
        shutil.rmtree(_clone_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Plan-issues helpers
# ---------------------------------------------------------------------------


def _run_plan_issues(
    repo: str,
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
) -> None:
    """Dispatch a single plan-issues agent to file stage/impl GitHub issues.

    Builds a prompt by injecting the ``plan-issues`` skill file and
    invoking ``runner.run()`` once. The agent is responsible for reading
    HLD and LLD design docs, filing impl issues with acceptance criteria
    and a dependency graph, and posting a Notion report.

    Args:
        repo:       GitHub repository in ``owner/repo`` format.
        milestone:  Active milestone name to file issues against.
        config:     Validated Config instance.
        checkpoint: Active Checkpoint instance.
        dry_run:    If True, print the prompt length without executing.
    """
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    today = date.today().isoformat()

    if dry_run:
        click.echo(
            f"[dry-run] Would dispatch plan-issues agent for milestone "
            f"{milestone!r} in repo {repo!r}"
        )
        return

    repo_root, clone_dir = _ensure_worktree_repo(repo)
    try:
        base_prompt = (
            f"## Headless Autonomous Mode\n"
            f"You are running in a fully automated headless pipeline. No human is present.\n"
            f"Use tools directly and silently. Do NOT produce conversational text.\n\n"
            f"## Session Parameters\n"
            f"- Repository: {repo}\n"
            f"- Milestone: {milestone}\n"
            f"- Local repo clone: {repo_root}\n"
            f"- Session Date: {today}\n\n"
            f"You are the plan-issues orchestrator for the `{repo}` repository.\n"
            f"The repository has been cloned to `{repo_root}`. "
            f"Read the HLD and LLD design docs from the local clone at "
            f"`{repo_root}/docs/design/{milestone}/` — do NOT use `gh api` to fetch them.\n"
            f"File fully-specified `stage/impl` issues against milestone `{milestone}` "
            f"following the skill instructions.\n"
        )

        logger.log_conductor_event(
            run_id=checkpoint.run_id,
            phase="dispatch",
            event_type="plan_issues_start",
            payload={"repo": repo, "milestone": milestone},
            log_dir=config.log_dir.expanduser(),
        )

        result = _run_agent(
            base_prompt,
            "scope-worker",
            ["Bash", "Read", "Glob", "Grep"],
            200,
            f"plan-issues-{milestone}",
            f"[scope {milestone}] ",
            config,
            model=config.model,
        )
    finally:
        shutil.rmtree(clone_dir, ignore_errors=True)

    _log_agent_cost(
        result,
        repo,
        "scoping",
        config,
        checkpoint,
        issue_number=None,
        milestone=milestone,
        model=config.model,
    )

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="plan_issues_complete",
        payload={
            "repo": repo,
            "milestone": milestone,
            "subtype": result.subtype,
            "is_error": result.is_error,
            "error_code": result.error_code,
        },
        log_dir=config.log_dir.expanduser(),
    )
    session.save(checkpoint, checkpoint_path)

    if result.is_error:
        if result.stderr:
            click.echo(
                f"[scope {milestone}] stderr (exit_code={result.exit_code}):\n"
                f"{result.stderr[:2000]}",
                err=True,
            )
        raise click.ClickException(
            f"Scope stage failed for milestone '{milestone}': "
            f"{result.subtype} / {result.error_code} (exit_code={result.exit_code}). "
            "No impl issues were created. Fix the failure and re-run --scope."
        )
    click.echo(f"Plan-issues complete for milestone '{milestone}'.")


# ---------------------------------------------------------------------------
# Plan-milestones helpers
# ---------------------------------------------------------------------------


def _validate_spec_path(spec: str) -> Path:
    """Resolve and validate the ``--spec`` argument.

    Accepts:
    - A local path (relative or absolute)
    - A GitHub path in ``owner/repo/path/to/spec.md`` format — fetched via
      ``gh api`` and written to a temp file that is cleaned up at exit.

    Args:
        spec: Raw value of the ``--spec`` CLI option.

    Returns:
        Resolved absolute :class:`~pathlib.Path`.

    Raises:
        click.ClickException: If the path does not exist or is not a ``.md`` file.
    """
    # Detect GitHub path: owner/repo/path — at least 3 slash-separated parts,
    # does not start with / or ~, and the local path does not exist.
    parts = spec.split("/")
    local = Path(spec).expanduser().resolve()
    is_github_path = (
        len(parts) >= 3
        and not spec.startswith("/")
        and not spec.startswith("~")
        and not local.exists()
    )
    if is_github_path:
        owner_repo = f"{parts[0]}/{parts[1]}"
        file_path = "/".join(parts[2:])
        if not file_path.lower().endswith(".md"):
            raise click.ClickException(f"Spec file must be a .md file, got: {file_path!r}")
        result = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}/contents/{file_path}", "--jq", ".content"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise click.ClickException(
                f"Could not fetch spec from GitHub ({owner_repo}/{file_path}): "
                f"{result.stderr.strip()}"
            )
        import base64
        import tempfile

        content = base64.b64decode(result.stdout.strip()).decode()
        suffix = Path(file_path).suffix
        stem = Path(file_path).stem
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, prefix=f"{stem}-", delete=False)
        tmp.write(content)
        tmp.close()
        tmp_path = Path(tmp.name)
        import atexit

        atexit.register(lambda p=tmp_path: p.unlink(missing_ok=True))
        return tmp_path

    if not local.exists():
        raise click.ClickException(f"Spec file not found: {local}")
    if local.suffix.lower() != ".md":
        raise click.ClickException(f"Spec file must be a .md file, got: {local.name!r}")
    return local


_BRIMSTONE_BOT = "yeast-bot"

_CI_WORKFLOW_TEMPLATE = """\
name: CI

on:
  push:
    branches: ["{branch}"]
  pull_request:
    branches: ["{branch}"]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - name: Run tests
        run: uv run pytest
      - name: Lint
        run: uv run ruff check
"""


def _accept_brimstone_bot_invitation(repo: str) -> None:
    """Accept the pending collaborator invitation for *repo* as ``yeast-bot``.

    Reads ``BRIMSTONE_GH_TOKEN`` from the environment and calls the GitHub
    Invitations API as the bot user.  Raises ``SystemExit`` if the token is
    absent, the invitation listing fails, or the acceptance call fails.

    Args:
        repo: GitHub repository in ``owner/repo`` format.
    """
    token = os.environ.get("BRIMSTONE_GH_TOKEN") or ""
    if not token:
        click.echo(
            "Error: BRIMSTONE_GH_TOKEN is not set; "
            f"cannot auto-accept {_BRIMSTONE_BOT} invitation for {repo}.",
            err=True,
        )
        raise SystemExit(1)

    # Find the pending invitation for this repo
    list_result = subprocess.run(
        [
            "curl",
            "-s",
            "-H",
            f"Authorization: token {token}",
            "https://api.github.com/user/repository_invitations",
        ],
        capture_output=True,
        text=True,
    )
    if list_result.returncode != 0:
        click.echo(
            f"Error: could not list invitations for {_BRIMSTONE_BOT}: {list_result.stderr.strip()}",
            err=True,
        )
        raise SystemExit(1)

    try:
        invitations = json.loads(list_result.stdout)
    except json.JSONDecodeError:
        click.echo(
            f"Error: unexpected response when listing invitations for {_BRIMSTONE_BOT}.",
            err=True,
        )
        raise SystemExit(1)

    matching_ids = [
        inv["id"] for inv in invitations if inv.get("repository", {}).get("full_name", "") == repo
    ]

    if not matching_ids:
        # No pending invitation — already accepted or GitHub hasn't created it yet.
        click.echo(f"{_BRIMSTONE_BOT} is already a collaborator on {repo} (no pending invite).")
        return

    # GitHub can create multiple pending invitations for the same user+repo
    # (one per PUT call). Accept ALL of them so the active one is not missed.
    # Older superseded invitations return 204 harmlessly.
    for invitation_id in matching_ids:
        accept_result = subprocess.run(
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "-X",
                "PATCH",
                "-H",
                f"Authorization: token {token}",
                f"https://api.github.com/user/repository_invitations/{invitation_id}",
            ],
            capture_output=True,
            text=True,
        )
        http_code = accept_result.stdout.strip()
        if http_code not in ("204", ""):
            click.echo(
                f"Warning: invitation {invitation_id} accept returned HTTP {http_code} for {repo}.",
                err=True,
            )

    click.echo(
        f"{_BRIMSTONE_BOT} accepted invitation to {repo} "
        f"({len(matching_ids)} pending invite(s) processed)"
    )


def _add_brimstone_bot_collaborator(repo: str) -> None:
    """Add the brimstone service account as a collaborator on *repo* and auto-accept.

    Uses the GitHub Collaborators API to grant push access to ``yeast-bot``,
    then immediately accepts the invitation using ``BRIMSTONE_GH_TOKEN``.
    Raises ``SystemExit`` on any failure so ``init`` surfaces the error clearly.

    This call must run as the repo owner (bread-wood), not as yeast-bot.
    ``startup_sequence`` sets ``GH_TOKEN`` to yeast-bot's token, so we
    explicitly strip it here so ``gh`` falls back to the user's keychain auth.

    Args:
        repo: GitHub repository in ``owner/repo`` format.
    """
    endpoint = f"repos/{repo}/collaborators/{_BRIMSTONE_BOT}"
    env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
    result = subprocess.run(
        ["gh", "api", endpoint, "-X", "PUT", "-f", "permission=push"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        click.echo(
            f"Error: could not add {_BRIMSTONE_BOT} to {repo}: {result.stderr.strip()}",
            err=True,
        )
        raise SystemExit(1)
    click.echo(f"Added {_BRIMSTONE_BOT} as a collaborator on {repo}")
    _accept_brimstone_bot_invitation(repo)


def _upload_spec_to_repo(repo: str, spec_path: Path, version: str) -> None:
    """Create or update ``docs/specs/<version>.md`` in *repo* via the GitHub Contents API.

    Uses ``gh api`` with a PUT request on the default branch.  If the file
    already exists its SHA is fetched first so the update is accepted by GitHub.

    Args:
        repo:      GitHub repository in ``owner/repo`` format.
        spec_path: Local path to the spec markdown file.
        version:   Version identifier used to build the remote filename stem.

    Raises:
        click.ClickException: If the API call fails.
    """
    import base64

    content = spec_path.read_bytes()
    encoded = base64.b64encode(content).decode()
    remote_path = f"docs/specs/{version}.md"
    message = f"docs: seed spec {version}"

    # Fetch existing file SHA if the file already exists (needed for updates)
    check = _gh(["api", f"repos/{repo}/contents/{remote_path}"], check=False)
    sha: str | None = None
    if check.returncode == 0:
        try:
            sha = json.loads(check.stdout).get("sha")
        except (json.JSONDecodeError, AttributeError):
            pass

    payload: dict = {"message": message, "content": encoded}
    if sha:
        payload["sha"] = sha

    result = subprocess.run(
        ["gh", "api", f"repos/{repo}/contents/{remote_path}", "-X", "PUT", "--input", "-"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"Failed to upload spec to {repo}/{remote_path}: {result.stderr.strip()}"
        )
    click.echo(f"Uploaded spec to {repo}/{remote_path}")


def _setup_ci(repo: str, config: Config, dry_run: bool) -> None:
    """Upload a default CI workflow and inject ANTHROPIC_API_KEY as a GitHub Actions secret.

    Uses the GitHub Contents API to commit the workflow directly — no local clone required.
    Non-fatal: emits warnings on failure so ``init`` can continue.

    Args:
        repo:    GitHub repository in ``owner/repo`` format.
        config:  Validated Config instance (provides default_branch and anthropic_api_key).
        dry_run: If True, print what would happen without executing.
    """
    import base64

    workflow_path = ".github/workflows/ci.yml"
    branch = config.default_branch
    workflow_content = _CI_WORKFLOW_TEMPLATE.format(branch=branch)

    if dry_run:
        click.echo(f"[dry-run] would commit {workflow_path} to {repo}")
        click.echo(f"[dry-run] would set ANTHROPIC_API_KEY secret on {repo}")
        return

    check = _gh(["api", f"repos/{repo}/contents/{workflow_path}"], check=False)
    if check.returncode == 0:
        click.echo(f"CI workflow already exists in {repo}, skipping upload")
    else:
        encoded = base64.b64encode(workflow_content.encode()).decode()
        payload: dict = {"message": "ci: add default CI workflow", "content": encoded}
        api_path = f"repos/{repo}/contents/{workflow_path}"
        result = subprocess.run(
            ["gh", "api", api_path, "-X", "PUT", "--input", "-"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            click.echo(
                f"Warning: could not push CI workflow to {repo}: {result.stderr.strip()}",
                err=True,
            )
        else:
            click.echo(f"Pushed CI workflow to {repo}/{workflow_path}")

    # Inject ANTHROPIC_API_KEY as a GitHub Actions secret
    result = subprocess.run(
        [
            "gh",
            "secret",
            "set",
            "ANTHROPIC_API_KEY",
            "--repo",
            repo,
            "--body",
            config.anthropic_api_key,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        click.echo(
            f"Warning: could not set ANTHROPIC_API_KEY secret on {repo}: {result.stderr.strip()}",
            err=True,
        )
    else:
        click.echo(f"Set ANTHROPIC_API_KEY secret on {repo}")


# ---------------------------------------------------------------------------
# Required labels — created during init so every downstream stage can rely on them
# ---------------------------------------------------------------------------

_REQUIRED_LABELS: list[tuple[str, str, str]] = [
    # (name, color, description)
    ("stage/research", "0075ca", "Research and investigation task"),
    ("stage/design", "e4e669", "Design task (HLD, LLD)"),
    ("stage/impl", "d93f0b", "Implementation task (code + tests)"),
    ("P0", "b60205", "Release blocker"),
    ("P1", "e11d48", "High priority"),
    ("P2", "0e8a16", "Normal priority (default)"),
    ("P3", "5319e7", "Low priority"),
    ("P4", "cfd3d7", "Backlog"),
    ("in-progress", "fbca04", "Currently being worked on"),
    ("wont-research", "eeeeee", "Closed by orchestrator — out of scope or duplicate"),
    ("pipeline", "006b75", "Pipeline stage transition tracking"),
    ("bug", "ee0701", "Defect filed by QA or reported post-release"),
    ("infra", "bfd4f2", "Infrastructure and tooling work"),
]


def _add_branch_protection(repo: str, branch: str) -> None:
    """Add branch protection rules to *branch* in *repo*.

    Rules applied:
    - No force-pushes
    - No deletions
    - Require linear history (squash/rebase only)
    - Require a PR before merging (0 approving reviews required — just needs a PR)
    - Admin bypass allowed so the orchestrator can merge via ``gh pr merge``

    Non-fatal: emits a warning if the API call fails (e.g. the token lacks
    ``repo`` scope for a private repo, or the branch does not exist yet).
    """
    payload = {
        "required_status_checks": None,
        "enforce_admins": False,
        "required_pull_request_reviews": {
            "dismiss_stale_reviews": False,
            "require_code_owner_reviews": False,
            "required_approving_review_count": 0,
        },
        "restrictions": None,
        "allow_force_pushes": False,
        "allow_deletions": False,
        "required_linear_history": True,
    }
    result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo}/branches/{branch}/protection",
            "-X",
            "PUT",
            "--input",
            "-",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        click.echo(
            f"Warning: could not set branch protection on {repo}/{branch}: {result.stderr.strip()}",
            err=True,
        )
    else:
        click.echo(f"Branch protection set on {repo}/{branch}")


def _ensure_labels(repo: str) -> None:
    """Create any missing required labels in *repo*.

    Uses ``gh label create`` with ``--force`` to create-or-update each label.
    """
    for name, color, description in _REQUIRED_LABELS:
        result = _gh(
            [
                "label",
                "create",
                name,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ],
            repo=repo,
            check=False,
        )
        if result.returncode != 0:
            click.echo(
                f"Warning: could not ensure label {name!r}: {result.stderr.strip()}",
                err=True,
            )
        else:
            click.echo(f"  label ok: {name}", err=True)


def _report_plan_output(repo: str, milestone: str) -> None:
    """Print the milestone and research issues that plan created."""
    # Fetch the milestone description
    ms_result = _gh(
        ["milestone", "list", "--json", "title,description", "--limit", "100"],
        repo=repo,
        check=False,
    )
    if ms_result.returncode == 0:
        try:
            milestones = json.loads(ms_result.stdout)
            for ms in milestones:
                if ms.get("title") == milestone:
                    desc = ms.get("description", "")
                    click.echo(f"\nMilestone created: {milestone}")
                    if desc:
                        click.echo(f"  {desc}")
                    break
        except json.JSONDecodeError:
            pass

    # Fetch research issues in this milestone
    issues_result = _gh(
        [
            "issue",
            "list",
            "--milestone",
            milestone,
            "--label",
            "stage/research",
            "--state",
            "open",
            "--json",
            "number,title",
            "--limit",
            "50",
        ],
        repo=repo,
        check=False,
    )
    if issues_result.returncode == 0:
        try:
            issues = json.loads(issues_result.stdout)
            if issues:
                click.echo(f"\nSeed research issues ({len(issues)}):")
                for issue in issues:
                    click.echo(f"  #{issue['number']}: {issue['title']}")
            else:
                click.echo("\nNo seed research issues were filed.")
        except json.JSONDecodeError:
            pass


def _run_plan(
    repo: str,
    version: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
    spec_stem: str | None = None,
    spec_local_path: str | None = None,
    store: BeadStore | None = None,
) -> None:
    """Dispatch a single plan agent to create a milestone pair and seed issues.

    Builds a prompt by injecting the ``plan`` skill file and
    invoking ``runner.run()`` once. The agent reads the spec, creates
    milestones, and files seed research issues.

    Args:
        repo:            GitHub repository in ``owner/repo`` format.
        version:         Milestone name (e.g. ``"v0.1.0-cold-start"``).
        config:          Validated Config instance.
        checkpoint:      Active Checkpoint instance.
        dry_run:         If True, print the prompt length without executing.
        spec_stem:       Filename stem of the spec file (e.g. ``"v0.1.x-cold-start"``).
                         When None, falls back to ``version``.
        spec_local_path: Absolute path to the original spec file on disk. When provided,
                         the agent reads it directly; otherwise reads via GitHub API.
        store:           Active BeadStore. When provided, used as source of truth to
                         detect whether research beads already exist for this milestone.
    """
    if spec_stem is None:
        spec_stem = version
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    today = date.today().isoformat()

    # Skip plan if the milestone already exists with research issues — plan already ran.
    # Plan's output is a GitHub milestone + seed issues; BeadStore is populated later
    # by the research stage dispatcher. Use milestone + issue count as the idempotency
    # signal here.
    if not dry_run:
        try:
            if _milestone_exists(repo, version):
                existing = _count_open_issues_by_label(repo, version, RESEARCH_LABEL)
                if existing > 0:
                    click.echo(
                        f"[plan] milestone {version!r} exists with {existing} open research"
                        " issue(s) — plan stage skipped (already seeded)."
                    )
                    return
        except Exception:
            pass  # If the check fails, fall through and let the agent run normally

    # Skip plan if design is already underway (HLD merged → research issues
    # seeded in a prior run; re-filing them would block design with new research).
    if not dry_run:
        hld_doc_path = f"docs/design/{version}/HLD.md"
        try:
            default_branch = _get_default_branch_for_repo(repo)
            if _doc_exists_on_default_branch(repo, hld_doc_path, default_branch):
                click.echo(
                    f"HLD already merged for {version!r} — plan stage skipped (design underway)."
                )
                return
        except Exception:
            pass  # If the check fails, fall through and let the agent run normally

    if spec_local_path is not None:
        spec_read_instruction = f"Read the spec from the local file:\n  cat {spec_local_path}"
    else:
        spec_read_instruction = (
            f"Read the spec using the GitHub API:\n"
            f"  gh api repos/{repo}/contents/docs/specs/{spec_stem}.md --jq '.content' | base64 -d"
        )

    dry_run_instruction = (
        "\n\n## DRY-RUN MODE\n"
        "Do NOT create any GitHub milestones or issues. Do NOT run any `gh` write commands.\n"
        "Instead, output the complete plan to stdout:\n"
        "1. The milestone you would create (title and description)\n"
        "2. Every research issue you would file (title, rationale, research areas, "
        "acceptance criteria, dependencies)\n"
        "3. Suggested dispatch order\n"
        "You MAY read the spec and use read-only `gh` commands to check existing state.\n"
        "Format the output clearly so a human can review and approve before a real run."
        if dry_run
        else ""
    )

    base_prompt = (
        f"## Session Parameters\n"
        f"- Repository: {repo}\n"
        f"- Version: {version}\n"
        f"- Session Date: {today}\n\n"
        f"You are the plan orchestrator for the `{repo}` repository.\n"
        f"You are running in a headless temp directory (not inside the repo checkout).\n"
        f"IMPORTANT: The milestone title MUST be exactly `{version}` — do not shorten or "
        f"normalize it (e.g. do not use `v0.1` if the version is `v0.1.0`).\n\n"
        f"CRITICAL — Label format for every research issue:\n"
        f'  --label "stage/research,P1"  (or P0 / P2 / P3 as appropriate)\n'
        f"  NEVER use bare `research` — the `stage/` prefix is required.\n"
        f"  NEVER put priority (e.g. [P1]) in the issue title.\n"
        f"  Every issue must carry both `stage/research` AND exactly one priority label.\n\n"
        f"{spec_read_instruction}\n"
        f"Then follow **all steps** in your system prompt skill to create the milestone and "
        f"file a complete research queue.\n\n"
        f"CRITICAL — Research queue requirements:\n"
        f"  - You MUST work through EVERY decomposition dimension in the skill (architecture,\n"
        f"    tooling, testing, error handling, data models, etc.) and write your analysis\n"
        f"    for each dimension before filing any issues.\n"
        f"  - The spec's 'Key Unknowns' section is a starting point only — do NOT stop there.\n"
        f"  - File a minimum of 4 research issues. If your analysis yields fewer, you missed\n"
        f"    genuine unknowns in project tooling, testing strategy, or error conventions.\n"
        f"  - Aim for 4–8 issues covering the full implementation surface."
        f"{dry_run_instruction}"
    )

    if dry_run:
        click.echo(
            f"[dry-run] Dispatching plan agent for version "
            f"{version!r} in repo {repo!r} (read-only — no GitHub writes)\n"
        )

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="plan_start",
        payload={"repo": repo, "version": version},
        log_dir=config.log_dir.expanduser(),
    )

    result = _run_agent(
        base_prompt,
        "plan-worker",
        ["Bash"],
        200,
        f"plan-{version}",
        f"[plan {version}] ",
        config,
    )

    _log_agent_cost(result, repo, "init", config, checkpoint, issue_number=None)

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="plan_complete",
        payload={
            "repo": repo,
            "version": version,
            "subtype": result.subtype,
            "is_error": result.is_error,
            "error_code": result.error_code,
            "exit_code": result.exit_code,
            "num_events": result.num_events,
            "stderr": result.stderr[:2000] if result.stderr else "",
            "dry_run": dry_run,
        },
        log_dir=config.log_dir.expanduser(),
    )
    session.save(checkpoint, checkpoint_path)

    if result.is_error:
        click.echo(
            f"Plan completed with error: {result.subtype} / {result.error_code}",
            err=True,
        )
        if result.stderr:
            click.echo(f"stderr:\n{result.stderr[:2000]}", err=True)
        raise SystemExit(1)
    elif result.subtype == "missing_result_event":
        click.echo(
            f"Warning: agent subprocess exited without producing a result event "
            f"(exit_code={result.exit_code}, num_events={result.num_events}).",
            err=True,
        )
        if result.stderr:
            click.echo(f"Subprocess stderr:\n{result.stderr[:2000]}", err=True)
        else:
            click.echo("Subprocess stderr: (empty)", err=True)
        if not dry_run:
            click.echo("Verifying milestone was created…", err=True)
    else:
        if not dry_run:
            click.echo(f"Plan complete for version '{version}'.")

    if dry_run:
        return

    # Post-validation: confirm the milestone was actually created.
    if not _milestone_exists(repo, version):
        click.echo(
            f"Error: plan completed but milestone '{version}' was not created.\n"
            "The agent likely exited before finishing. Re-run to try again.",
            err=True,
        )
        raise SystemExit(1)

    # Report what was created.
    _report_plan_output(repo=repo, milestone=version)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


@click.group()
def brimstone() -> None:
    """Brimstone orchestrator — run pipeline workers and admin commands."""


# Keep the old name as an alias for backwards compatibility
composer = brimstone


@brimstone.command("health")
@click.option(
    "--repo",
    default=None,
    help=(
        "Target repository. Accepts: 'owner/name' (existing remote), "
        "'name' (new private repo to scaffold), 'path/to/local/dir' (local git repo), "
        "or omit to operate on the current working directory."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def health_cmd(repo: str | None, as_json: bool) -> None:
    """Run preflight checks."""
    repo_ref = _resolve_repo(repo)
    overrides: dict = {}
    if repo_ref:
        overrides["github_repo"] = repo_ref
        overrides["target_repo"] = repo_ref
    config = load_config(**overrides)
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


@brimstone.command("cost")
@click.option("--run", "run_id", default=None, help="Filter by run ID")
@click.option(
    "--stage",
    default=None,
    help="Filter by stage (research/design/scope/impl)",
)
@click.option("--repo", default=None, help="Filter by repo (owner/name)")
@click.option("--milestone", default=None, help="Filter by milestone")
@click.option(
    "--breakdown",
    type=click.Choice(["run", "stage", "model", "issue"]),
    default=None,
    help="Group results by this field",
)
def cost(
    run_id: str | None,
    stage: str | None,
    repo: str | None,
    milestone: str | None,
    breakdown: str | None,
) -> None:
    """Show cost summary from the cost ledger."""
    config = load_config()
    log_dir = config.log_dir.expanduser()

    entries = logger.read_cost_ledger(log_dir, repo=repo, stage=stage)

    # Apply additional filters in Python
    if run_id is not None:
        entries = [e for e in entries if e.get("run_id") == run_id]
    if milestone is not None:
        entries = [e for e in entries if e.get("milestone") == milestone]

    if not entries:
        click.echo("No cost data found.")
        return

    def _total_cost(ents: list[dict]) -> float:
        return sum(e.get("total_cost_usd") or 0 for e in ents)

    def _total_input(ents: list[dict]) -> int:
        return sum(e.get("input_tokens") or 0 for e in ents)

    def _total_output(ents: list[dict]) -> int:
        return sum(e.get("output_tokens") or 0 for e in ents)

    click.echo("Cost summary")
    click.echo(f"  Entries : {len(entries)}")
    click.echo(f"  Total   : ${_total_cost(entries):.4f}")
    click.echo(f"  Input   : {_total_input(entries):,} tokens")
    click.echo(f"  Output  : {_total_output(entries):,} tokens")

    if breakdown:
        # Group by the breakdown field
        groups: dict[str, list[dict]] = {}
        for e in entries:
            key = str(e.get(breakdown) or "(unknown)")
            groups.setdefault(key, []).append(e)

        click.echo(f"\n  By {breakdown}:")
        for key, group_entries in sorted(groups.items()):
            n = len(group_entries)
            click.echo(f"  {key:<12}  ${_total_cost(group_entries):.4f}  {n:>4} calls")


@brimstone.command("report")
@click.option("--repo", default=None)
@click.option("--run", "run_id", default=None, help="Run ID (defaults to most recent)")
@click.option("--milestone", default=None)
@click.option(
    "--post",
    is_flag=True,
    default=False,
    help="Post report as GitHub issue comment",
)
def report(
    repo: str | None,
    run_id: str | None,
    milestone: str | None,
    post: bool,
) -> None:
    """Print a session report from BeadStore and cost ledger."""
    config = load_config()
    repo_ref = repo or config.github_repo
    if not repo_ref:
        click.echo("Error: --repo is required (or set github_repo in config)", err=True)
        raise SystemExit(1)
    _print_session_report(config, repo_ref, run_id, milestone, post)


def _print_session_report(
    config: Config,
    repo: str,
    run_id: str | None,
    milestone: str | None,
    post: bool,
) -> None:
    """Render a session report from BeadStore and cost ledger."""
    log_dir = config.log_dir.expanduser()
    store = make_bead_store(config, repo)

    # Load cost entries
    all_cost_entries = logger.read_cost_ledger(log_dir, repo=repo)
    if milestone is not None:
        all_cost_entries = [e for e in all_cost_entries if e.get("milestone") == milestone]

    # Resolve run_id from most recent cost entry if not specified
    effective_run_id = run_id
    if effective_run_id is None and all_cost_entries:
        effective_run_id = all_cost_entries[-1].get("run_id")

    if effective_run_id is not None:
        cost_entries = [e for e in all_cost_entries if e.get("run_id") == effective_run_id]
    else:
        cost_entries = all_cost_entries

    total_cost = sum(e.get("total_cost_usd") or 0 for e in cost_entries)

    # Load beads
    work_beads = store.list_work_beads()
    pr_beads = store.list_pr_beads()

    # Filter by milestone if provided
    if milestone is not None:
        work_beads = [b for b in work_beads if b.milestone == milestone]

    sep = "═" * 54
    click.echo(sep)
    click.echo("  brimstone session report")
    header_parts = [f"Repo: {repo}"]
    if milestone:
        header_parts.append(f"Milestone: {milestone}")
    click.echo(f"  {'  |  '.join(header_parts)}")
    run_label = effective_run_id if effective_run_id else "(unknown)"
    click.echo(f"  Run: {run_label}  |  Cost: ${total_cost:.2f}")
    click.echo(sep)

    # ISSUES table
    click.echo("")
    click.echo("ISSUES")
    if not work_beads:
        click.echo("  (none)")
    else:
        for wb in sorted(work_beads, key=lambda b: b.issue_number):
            pr_info = "(no PR yet)"
            if wb.pr_id:
                # pr_id is like "pr-187" — extract number
                try:
                    pr_num = int(wb.pr_id.split("-", 1)[1])
                    pr_bead = store.read_pr_bead(pr_num)
                    if pr_bead:
                        pr_info = f"PR #{pr_num}  {pr_bead.state}"
                    else:
                        pr_info = f"PR #{pr_num}"
                except (IndexError, ValueError):
                    pr_info = wb.pr_id
            click.echo(f"  #{wb.issue_number:<4}  {wb.title:<26}  {wb.state:<12}  {pr_info}")

    # PULL REQUESTS table
    click.echo("")
    click.echo("PULL REQUESTS")
    if not pr_beads:
        click.echo("  (none)")
    else:
        # Build per-PR cost map
        pr_cost_map: dict[int, float] = {}
        for e in cost_entries:
            issue_num = e.get("issue_number")
            if issue_num is None:
                continue
            # Find PR bead linked to this issue
            for pb in pr_beads:
                if pb.issue_number == issue_num:
                    prev = pr_cost_map.get(pb.pr_number, 0.0)
                    pr_cost_map[pb.pr_number] = prev + (e.get("total_cost_usd") or 0)

        for pb in sorted(pr_beads, key=lambda b: b.pr_number):
            pr_cost = pr_cost_map.get(pb.pr_number, 0.0)
            fix_info = f"{pb.fix_attempts} fix attempts"
            click.echo(
                f"  PR #{pb.pr_number:<4}  {pb.branch:<20}"
                f"  {pb.state:<14}  ${pr_cost:.2f}   {fix_info}"
            )

    # SUMMARY
    click.echo("")
    click.echo("SUMMARY")
    state_counts: dict[str, int] = {}
    for wb in work_beads:
        state_counts[wb.state] = state_counts.get(wb.state, 0) + 1
    issue_parts = [f"{cnt} {st}" for st, cnt in sorted(state_counts.items())]
    issue_summary = "  |  ".join(issue_parts) if issue_parts else "(none)"
    click.echo(f"  Issues:  {issue_summary}")

    pr_state_counts: dict[str, int] = {}
    for pb in pr_beads:
        pr_state_counts[pb.state] = pr_state_counts.get(pb.state, 0) + 1
    pr_parts = [f"{cnt} {st}" for st, cnt in sorted(pr_state_counts.items())]
    pr_summary = "  |  ".join(pr_parts) if pr_parts else "(none)"
    click.echo(f"  PRs:     {pr_summary}")
    click.echo(f"  Cost:    ${total_cost:.2f} total")
    click.echo(sep)

    if post and milestone:
        import subprocess as _subprocess

        gh_result = _subprocess.run(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--milestone",
                milestone,
                "--label",
                "roadmap",
                "--json",
                "number",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
        )
        issue_list: list[dict] = []
        try:
            import json as _json

            issue_list = _json.loads(gh_result.stdout or "[]")
        except Exception:
            issue_list = []

        if not issue_list:
            click.echo(
                f"Warning: no roadmap tracking issue found for milestone "
                f"{milestone!r}; skipping post.",
                err=True,
            )
        else:
            tracking_issue_number = issue_list[0]["number"]
            # Build markdown body
            run_md = effective_run_id or "(unknown)"
            md_lines = [
                "## brimstone session report",
                (
                    f"**Repo:** {repo}  |  **Milestone:** {milestone}"
                    f"  |  **Run:** {run_md}  |  **Cost:** ${total_cost:.2f}"
                ),
                "",
                "### Issues",
                "| # | Title | State | PR |",
                "|---|-------|-------|----|",
            ]
            for wb in sorted(work_beads, key=lambda b: b.issue_number):
                pr_col = ""
                if wb.pr_id:
                    try:
                        pr_num = int(wb.pr_id.split("-", 1)[1])
                        pr_bead = store.read_pr_bead(pr_num)
                        pr_col = f"PR #{pr_num} {pr_bead.state if pr_bead else ''}"
                    except (IndexError, ValueError):
                        pr_col = wb.pr_id
                md_lines.append(f"| #{wb.issue_number} | {wb.title} | {wb.state} | {pr_col} |")

            md_lines += ["", "### Summary", f"- Cost: ${total_cost:.2f}", ""]
            body = "\n".join(md_lines)

            _subprocess.run(
                [
                    "gh",
                    "issue",
                    "comment",
                    str(tracking_issue_number),
                    "--repo",
                    repo,
                    "--body",
                    body,
                ],
                capture_output=True,
                text=True,
            )


# ---------------------------------------------------------------------------
# run / init / adopt — unified pipeline interface
# ---------------------------------------------------------------------------


def _check_gate_before_stage(
    stage: str,
    stages_being_run: list[str],
    repo: str,
    milestone: str,
    default_branch: str,
) -> None:
    """Abort with a clear error if a prerequisite stage has not been completed.

    Gates are only applied when the prerequisite stage is NOT also being run in
    the same invocation (e.g. ``--all`` skips both gates because research and
    design are both included).

    Args:
        stage:             Stage about to be executed.
        stages_being_run:  All stages requested in this invocation.
        repo:              GitHub repository in ``owner/repo`` format.
        milestone:         Active milestone name.
        default_branch:    Default branch of the remote repo.

    Raises:
        click.ClickException: If the prerequisite is not satisfied.
    """
    if stage == "design" and "research" not in stages_being_run:
        open_count = _count_open_issues_by_label(repo, milestone, RESEARCH_LABEL)
        if open_count > 0:
            raise click.ClickException(
                f"{open_count} open research issue(s) remain for milestone '{milestone}'. "
                "Run `brimstone run --research` first, or use `--all` to run all stages."
            )

    if stage == "scope" and "design" not in stages_being_run:
        hld_path = f"docs/design/{milestone}/HLD.md"
        if not _doc_exists_on_default_branch(repo, hld_path, default_branch):
            raise click.ClickException(
                f"Design doc '{hld_path}' does not exist on branch '{default_branch}'. "
                "Run `brimstone run --design` first."
            )

    if stage == "impl" and "scope" not in stages_being_run:
        if _count_open_issues_by_label(repo, milestone, IMPL_LABEL) == 0:
            raise click.ClickException(
                f"No open impl issues found for milestone '{milestone}'. "
                "Run `brimstone run --scope` first to generate them from the design docs."
            )


@brimstone.command("run")
@click.argument("specs", nargs=-1, type=click.Path(dir_okay=False), metavar="[SPEC]...")
@click.option(
    "--repo",
    default=None,
    help=(
        "Target repository. Accepts: 'owner/name' (remote repo), "
        "or omit to infer from the current working directory's git remote."
    ),
)
@click.option(
    "--stage",
    "stage_flag",
    default=None,
    type=click.Choice(["plan", "research", "design", "scope", "impl", "all"]),
    help=(
        "Pipeline stage to run. One of: plan, research, design, scope, impl, all. "
        "When positional SPEC args are given and --stage is omitted, defaults to 'all'."
    ),
)
@click.option(
    "--plan",
    "do_plan",
    is_flag=True,
    help="[Deprecated] Run the plan-milestones stage. Use --stage plan instead.",
)
@click.option(
    "--research",
    "do_research",
    is_flag=True,
    help="[Deprecated] Run the research stage. Use --stage research instead.",
)
@click.option(
    "--design",
    "do_design",
    is_flag=True,
    help="[Deprecated] Run the design stage. Use --stage design instead.",
)
@click.option(
    "--scope",
    "do_scope",
    is_flag=True,
    help="[Deprecated] Run the scoping stage. Use --stage scope instead.",
)
@click.option(
    "--impl",
    "do_impl",
    is_flag=True,
    help="[Deprecated] Run the implementation stage. Use --stage impl instead.",
)
@click.option(
    "--all",
    "do_all",
    is_flag=True,
    help="[Deprecated] Run all pipeline stages in order. Use --stage all instead.",
)
@click.option(
    "--milestone",
    multiple=True,
    help=(
        "Milestone name to operate on. Repeat to run multiple milestones in sequence. "
        "Required when no positional SPEC args are given. "
        "Optional when SPEC args are provided (milestones inferred from spec filenames)."
    ),
)
@click.option(
    "--spec",
    "spec_opt",
    multiple=True,
    type=click.Path(dir_okay=False),
    help=(
        "Path to a spec .md file (option form). Equivalent to positional SPEC args. "
        "Repeat to plan multiple milestones in one invocation."
    ),
)
@click.option("--model", default=None, help="Override Claude model")
@click.option("--max-budget", type=float, default=None, help="USD budget cap")
@click.option("--dry-run", is_flag=True, help="Print what each stage would do without executing")
def run(
    specs: tuple[str, ...],
    repo: str | None,
    stage_flag: str | None,
    do_plan: bool,
    do_research: bool,
    do_design: bool,
    do_scope: bool,
    do_impl: bool,
    do_all: bool,
    milestone: tuple[str, ...],
    spec_opt: tuple[str, ...],
    model: str | None,
    max_budget: float | None,
    dry_run: bool,
) -> None:
    """Run one or more pipeline stages for a milestone.

    Stages execute in pipeline order: plan → research → design → scope → impl.
    Prerequisites are checked before each stage unless the prerequisite is
    also being run in the same invocation.

    SPEC is an optional positional argument: path to a spec .md file. When
    provided, the milestone is inferred from the filename stem (e.g.
    v0.2.0-feature.md → v0.2.0). Multiple SPECs run a campaign: each
    milestone completes fully before the next begins.

    Examples:

      brimstone run --stage plan --repo owner/repo /path/to/v0.1.0.md

      brimstone run /path/to/v0.2.0.md /path/to/v0.3.0.md --repo owner/repo

      brimstone run --stage research --milestone "v0.1.0"

      brimstone run --stage impl --repo owner/repo --milestone v0.2.0

      brimstone run --stage all --milestone "v0.1.0" --dry-run

      brimstone run --all --spec v0.3.0.md --spec v0.4.0.md --repo owner/repo
    """
    # -----------------------------------------------------------------------
    # Merge positional specs and --spec option into a single tuple
    # -----------------------------------------------------------------------
    spec: tuple[str, ...] = specs + spec_opt

    # -----------------------------------------------------------------------
    # Resolve --stage (primary) vs legacy flags (deprecated aliases)
    # --stage wins if both are provided.
    # -----------------------------------------------------------------------
    if stage_flag is not None:
        # --stage is the primary interface
        effective_stage = stage_flag
        # Map to legacy booleans for the stages-list logic below
        do_all = effective_stage == "all"
        do_plan = effective_stage == "plan"
        do_research = effective_stage == "research"
        do_design = effective_stage == "design"
        do_scope = effective_stage == "scope"
        do_impl = effective_stage == "impl"
    else:
        # No --stage given — check if legacy flags were used
        if not any([do_plan, do_research, do_design, do_scope, do_impl, do_all]):
            # No flags at all: if spec provided, default to "all"
            if spec:
                do_all = True
            # else: fall through to the "no stages" error below

    # -----------------------------------------------------------------------
    # Determine ordered stages to run
    # -----------------------------------------------------------------------
    if do_all:
        stages: list[str] = ["research", "design", "scope", "impl"]
        if spec:
            stages = ["plan"] + stages
    else:
        stages = [
            s
            for s, flag in [
                ("plan", do_plan),
                ("research", do_research),
                ("design", do_design),
                ("scope", do_scope),
                ("impl", do_impl),
            ]
            if flag
        ]

    if not stages:
        raise click.UsageError(
            "Specify a stage: --stage <plan|research|design|scope|impl|all>, "
            "or pass a SPEC file to run the full pipeline."
        )

    # --spec is required when --plan is in the stage list
    if "plan" in stages and not spec:
        raise click.UsageError(
            "--spec (or a positional SPEC arg) is required when --plan is specified"
        )

    # Resolve spec paths eagerly so errors surface before any network calls
    resolved_specs = [_validate_spec_path(s) for s in spec]

    # Infer milestone from spec stem: take everything up to and including the
    # first component that starts with 'v' and looks like a version.
    def _infer_milestone_from_spec(path: Path) -> str:
        stem = path.stem
        parts = stem.split("-")
        version_parts: list[str] = []
        for part in parts:
            version_parts.append(part)
            if re.match(r"^v\d+(\.\d+)*$", part):
                break
        return "-".join(version_parts) if version_parts else stem

    # Determine the effective milestone list for non-plan stages.
    # When --all (or spec with no flags) is used with specs and no explicit
    # --milestone, infer from spec stems.
    non_plan_stages = [s for s in stages if s != "plan"]
    if non_plan_stages:
        if milestone:
            effective_milestones: tuple[str, ...] = milestone
        elif resolved_specs and ("plan" in stages or do_all or stage_flag in (None, "all")):
            effective_milestones = tuple(_infer_milestone_from_spec(p) for p in resolved_specs)
        elif resolved_specs:
            # Single-stage run with spec args — infer milestone from stem
            effective_milestones = tuple(_infer_milestone_from_spec(p) for p in resolved_specs)
        else:
            raise click.UsageError(
                f"--milestone is required for: {', '.join(f'--{s}' for s in non_plan_stages)}"
            )
    else:
        # Plan-only run: infer milestone from spec stems if no explicit --milestone
        if milestone:
            effective_milestones = milestone
        elif resolved_specs:
            effective_milestones = tuple(_infer_milestone_from_spec(p) for p in resolved_specs)
        else:
            effective_milestones = milestone  # empty; will be caught downstream

    # -----------------------------------------------------------------------
    # Resolve repo and build base config
    # -----------------------------------------------------------------------
    repo_ref = _resolve_repo(repo)

    overrides: dict = {
        "github_repo": repo_ref or None,
        "target_repo": repo_ref or None,
    }
    if model:
        overrides["model"] = model
    if max_budget is not None:
        overrides["max_budget_usd"] = max_budget
    config = load_config(**overrides)
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"

    # -----------------------------------------------------------------------
    # Milestone existence check for all non-plan stages
    # Skip check when plan is also running — it will create the milestone first.
    # -----------------------------------------------------------------------
    if not dry_run and non_plan_stages and "plan" not in stages:
        for ms in effective_milestones:
            if not _milestone_exists(repo_ref, ms):
                raise click.ClickException(
                    f"Milestone '{ms}' not found on {repo_ref}. "
                    "Run `brimstone run --plan --repo <repo> --spec <path>` to create it."
                )

    # Resolve default branch once for gate checks
    default_branch = _get_default_branch_for_repo(repo_ref) if repo_ref and not dry_run else "main"

    # Ensure all required labels exist on the repo before any stage runs.
    # This is idempotent — safe to call on repos that went through `brimstone init`
    # and on repos adopted or pointed at directly without init.
    if not dry_run and repo_ref:
        _ensure_labels(repo_ref)

    # -----------------------------------------------------------------------
    # Campaign bead — track multi-milestone progress
    # -----------------------------------------------------------------------
    is_campaign = len(effective_milestones) > 1
    campaign_store: BeadStore | None = None

    if is_campaign and not dry_run and repo_ref:
        campaign_store = make_bead_store(config, repo_ref)
        existing = campaign_store.read_campaign_bead()
        if existing is None:
            now_str = datetime.now(UTC).isoformat()
            campaign_bead = CampaignBead(
                v=1,
                repo=repo_ref,
                milestones=list(effective_milestones),
                current_index=0,
                statuses={ms: "pending" for ms in effective_milestones},
                updated_at=now_str,
            )
            campaign_store.write_campaign_bead(campaign_bead)
        else:
            campaign_bead = existing

    # -----------------------------------------------------------------------
    # Execute milestone-first: complete each milestone fully before starting
    # the next. This ensures implementation decisions from vN inform vN+1
    # research and planning.
    #
    # Order: plan v0.3.0 → research → design → scope → impl v0.3.0
    #        plan v0.4.0 → research → design → scope → impl v0.4.0
    # -----------------------------------------------------------------------
    _plan_skip = frozenset({"yeast-bot is repo collaborator"})

    _STAGE_STATUS_MAP = {
        "plan": "planning",
        "research": "researching",
        "design": "designing",
        "scope": "scoping",
        "impl": "implementing",
    }

    for i, ms in enumerate(effective_milestones):
        for stage in stages:
            click.echo(f"\n── Stage: {stage} ({ms}) ──", err=True)

            # Update campaign bead status
            if is_campaign and not dry_run and campaign_store is not None:
                status_name = _STAGE_STATUS_MAP.get(stage, stage)
                campaign_bead.statuses[ms] = status_name
                campaign_bead.current_index = i
                campaign_bead.updated_at = datetime.now(UTC).isoformat()
                campaign_store.write_campaign_bead(campaign_bead)

            if stage == "plan":
                resolved_spec = resolved_specs[i]
                stem = resolved_spec.stem
                if dry_run:
                    click.echo(
                        f"[dry-run] would upload {resolved_spec} to "
                        f"{repo_ref}/docs/specs/{stem}.md"
                        f" and run plan-milestones for milestone={ms!r}",
                        err=True,
                    )
                    continue
                _config, _checkpoint, _store = startup_sequence(
                    config=config,
                    checkpoint_path=checkpoint_path,
                    milestone=ms,
                    stage=stage,
                    skip_checks=_plan_skip,
                )
                _upload_spec_to_repo(repo_ref, resolved_spec, stem)
                _run_plan(
                    repo=repo_ref,
                    version=ms,
                    config=_config,
                    checkpoint=_checkpoint,
                    dry_run=False,
                    spec_stem=stem,
                    spec_local_path=str(resolved_spec),
                    store=_store,
                )
            else:
                # Completion check — skip if stage is already done.
                # Beads are the source of truth; fall back to GitHub issue counts
                # only when no beads exist for the stage yet.
                if not dry_run:
                    _skip_store = make_bead_store(config, repo_ref) if repo_ref else None
                    if stage == "research":
                        _r_beads = (
                            _skip_store.list_work_beads(milestone=ms, stage="research")
                            if _skip_store
                            else []
                        )
                        # merge_ready = PR exists but hasn't merged yet — not done
                        _r_done = _r_beads and not any(
                            b.state in ("open", "claimed", "merge_ready") for b in _r_beads
                        )
                        if _r_done:
                            click.echo(
                                f"[run] {stage} ({ms}): already complete, skipping", err=True
                            )
                            continue
                    if stage == "design":
                        _d_beads = (
                            _skip_store.list_work_beads(milestone=ms, stage="design")
                            if _skip_store
                            else []
                        )
                        _d_done = (
                            _d_beads
                            and not any(
                                b.state in ("open", "claimed", "merge_ready") for b in _d_beads
                            )
                            and _doc_exists_on_default_branch(
                                repo_ref, f"docs/design/{ms}/HLD.md", default_branch
                            )
                        )
                        if _d_done:
                            click.echo(
                                f"[run] {stage} ({ms}): already complete, skipping", err=True
                            )
                            continue
                    if stage == "scope":
                        _i_beads = (
                            _skip_store.list_work_beads(milestone=ms, stage="impl")
                            if _skip_store
                            else []
                        )
                        if _i_beads:
                            click.echo(
                                f"[run] {stage} ({ms}): impl issues already exist, skipping",
                                err=True,
                            )
                            continue

                # Gate check — only when prerequisite is not also in this run
                if not dry_run:
                    _check_gate_before_stage(stage, stages, repo_ref, ms, default_branch)

                if dry_run:
                    click.echo(f"[dry-run] would run {stage} for milestone={ms!r}", err=True)
                    continue

                _config, _checkpoint, _store = startup_sequence(
                    config=config,
                    checkpoint_path=checkpoint_path,
                    milestone=ms,
                    stage=stage,
                    skip_checks=frozenset(),
                )
                if stage == "research":
                    _run_research_worker(
                        repo=repo_ref,
                        milestone=ms,
                        config=_config,
                        checkpoint=_checkpoint,
                        dry_run=False,
                        store=_store,
                    )
                elif stage == "design":
                    _run_design_worker(
                        repo=repo_ref,
                        milestone=ms,
                        config=_config,
                        checkpoint=_checkpoint,
                        dry_run=False,
                        store=_store,
                    )
                elif stage == "scope":
                    _run_plan_issues(
                        repo=repo_ref,
                        milestone=ms,
                        config=_config,
                        checkpoint=_checkpoint,
                        dry_run=False,
                    )
                elif stage == "impl":
                    _run_impl_worker(
                        repo=repo_ref,
                        milestone=ms,
                        config=_config,
                        checkpoint=_checkpoint,
                        dry_run=False,
                        store=_store,
                    )
                    # Campaign gate: after impl, wait until 0 open impl issues
                    # before allowing the next milestone to start.
                    if is_campaign and repo_ref:
                        click.echo(
                            f"[campaign] Waiting for all impl issues to close for {ms}…",
                            err=True,
                        )
                        while True:
                            open_count = _count_open_issues_by_label(repo_ref, ms, IMPL_LABEL)
                            if open_count == 0:
                                break
                            click.echo(
                                f"[campaign] {open_count} impl issue(s) still open for {ms}. "
                                f"Sleeping {BACKOFF_SLEEP_SECONDS}s…",
                                err=True,
                            )
                            time.sleep(BACKOFF_SLEEP_SECONDS)
                        click.echo(f"[campaign] {ms} fully shipped.", err=True)

                # Print per-stage cost summary
                try:
                    _entries = logger.read_cost_ledger(
                        _config.log_dir.expanduser(),
                        repo=repo_ref or "",
                        stage=stage,
                    )
                    if _entries:
                        _stage_cost = sum(e.get("total_cost_usd") or 0 for e in _entries)
                        click.echo(
                            f"[{stage}] cost: ${_stage_cost:.4f} ({len(_entries)} calls)",
                            err=True,
                        )
                except AttributeError:
                    pass

        # Mark milestone as shipped in campaign bead
        if is_campaign and not dry_run and campaign_store is not None:
            campaign_bead.statuses[ms] = "shipped"
            campaign_bead.current_index = i + 1
            campaign_bead.updated_at = datetime.now(UTC).isoformat()
            campaign_store.write_campaign_bead(campaign_bead)


@brimstone.command("init")
@click.argument("repo")
@click.option("--model", default=None, help="Override Claude model")
@click.option("--dry-run", is_flag=True, help="Print what would happen without executing")
def init(
    repo: str,
    model: str | None,
    dry_run: bool,
) -> None:
    """Create and set up REPO for the brimstone pipeline.

    REPO must be in 'owner/name' format. The repository is created on GitHub
    if it does not already exist, then cloned locally. yeast-bot is added as
    a collaborator, the CI workflow is installed, issue labels are created, and
    branch protection is set (non-fatal if it fails).

    Run this once per new repository, then seed research issues with:
      brimstone run --plan --repo <repo> --spec <path/to/spec.md>
    """
    if "/" not in repo:
        raise click.UsageError("REPO must be in 'owner/name' format")
    repo_ref = repo

    overrides: dict = {"github_repo": repo_ref, "target_repo": repo_ref}
    if model:
        overrides["model"] = model
    config = load_config(**overrides)
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"

    _HEADLESS_SKIP = frozenset(
        {
            "Git repo present",
            "Default branch matches config",
            "No stale in-progress issues",
            "Open PRs needing attention",
            "No active worktrees",
            "yeast-bot is repo collaborator",
        }
    )
    _config, _checkpoint, _store = startup_sequence(
        config=config,
        checkpoint_path=checkpoint_path,
        milestone="",
        stage="init",
        skip_checks=_HEADLESS_SKIP,
    )

    if dry_run:
        click.echo(f"[dry-run] would create repo {repo_ref} on GitHub (if missing)")
        click.echo(f"[dry-run] would upload CI workflow to {repo_ref} via GitHub API")
        click.echo(f"[dry-run] would add {_BRIMSTONE_BOT} as collaborator on {repo_ref}")
        click.echo(f"[dry-run] would create issue labels on {repo_ref}")
        click.echo(f"[dry-run] would set branch protection on {repo_ref}")
        return

    # ── 1. Create repo on GitHub (idempotent — skip if already exists) ──────
    click.echo(f"Creating repo {repo_ref} on GitHub (if missing)…")
    create_result = subprocess.run(
        ["gh", "repo", "create", repo_ref, "--private", "--confirm"],
        capture_output=True,
        text=True,
    )
    if create_result.returncode != 0:
        # Already exists → not an error; any other failure is non-fatal warning
        stderr = create_result.stderr.strip()
        if "already exists" in stderr or "Name already exists" in stderr:
            click.echo(f"Repo {repo_ref} already exists, skipping creation.")
        else:
            click.echo(f"Warning: could not create {repo_ref}: {stderr}", err=True)

    # ── 2. Add yeast-bot as collaborator ────────────────────────────────────
    _add_brimstone_bot_collaborator(repo_ref)

    # ── 3. Install CI workflow via GitHub Contents API (no local clone needed) ──
    # This creates the first commit on `main`, establishing the branch.
    _setup_ci(repo_ref, _config, dry_run=False)

    # ── 3a. Rename default branch to mainline (idempotent) ──────────────────
    # Must run AFTER _setup_ci so that the `main` branch exists (first commit).
    default_branch = _config.default_branch  # "mainline"
    rename_result = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{repo_ref}/branches/main/rename",
            "--method",
            "POST",
            "--field",
            f"new_name={default_branch}",
        ],
        capture_output=True,
        text=True,
    )
    if rename_result.returncode == 0:
        click.echo(f"Renamed default branch to {default_branch}")
    else:
        stderr = rename_result.stderr.strip()
        if "Branch not found" in stderr or "already exists" in stderr:
            pass  # already on mainline or rename not needed
        else:
            click.echo(f"Warning: could not rename default branch: {stderr}", err=True)

    # ── 4. Create issue labels ───────────────────────────────────────────────
    _ensure_labels(repo_ref)

    # ── 5. Branch protection (non-fatal) ────────────────────────────────────
    _add_branch_protection(repo_ref, _config.default_branch)

    click.echo(
        f"\nRepo {repo_ref} is ready. Next step:\n"
        f"  brimstone run --plan --repo {repo_ref} --spec <path/to/spec.md>"
    )


@brimstone.command("adopt")
@click.option("--source-repo", required=True, help="Source repository to adopt from.")
@click.option("--target-repo", default=None, help="Target repository (defaults to source).")
def adopt(source_repo: str, target_repo: str | None) -> None:
    """Adopt an existing repository into the brimstone pipeline. (Not yet implemented.)"""
    click.echo("adopt: not yet implemented", err=True)
    raise SystemExit(1)


@brimstone.command("status")
@click.option(
    "--repo",
    default=None,
    help="Repository in owner/repo format. Inferred from git remote if omitted.",
)
def status_cmd(repo: str | None) -> None:
    """Show campaign status for a repository."""
    repo_ref = _resolve_repo(repo)
    if not repo_ref:
        raise click.UsageError(
            "Could not determine repository. Pass --repo <owner/repo> or run from a git repo."
        )

    config = load_config(github_repo=repo_ref, target_repo=repo_ref)
    store = make_bead_store(config, repo_ref)
    campaign = store.read_campaign_bead()

    # Derive a short project name from the repo slug
    project_name = repo_ref.split("/")[-1] if "/" in repo_ref else repo_ref
    click.echo(f"{project_name} ({repo_ref})")

    if campaign is None:
        # No campaign bead — fall back to querying GitHub for milestone summaries
        result = _gh(
            ["api", f"repos/{repo_ref}/milestones", "--paginate", "-q", ".[].title"],
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            click.echo("  (no milestones found)")
            return
        milestone_titles = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        last_idx = len(milestone_titles) - 1
        for idx, ms in enumerate(milestone_titles):
            open_count = _count_open_issues_by_label(repo_ref, ms, IMPL_LABEL)
            connector = "└──" if idx == last_idx else "├──"
            if open_count == 0:
                click.echo(f"{connector} {ms}  [SHIPPED]")
            else:
                click.echo(f"{connector} {ms}  [PENDING  {open_count} impl issues open]")
        return

    milestones = campaign.milestones
    last_idx = len(milestones) - 1
    for idx, ms in enumerate(milestones):
        connector = "└──" if idx == last_idx else "├──"
        raw_status = campaign.statuses.get(ms, "pending")
        status_upper = raw_status.upper()

        if raw_status == "shipped":
            click.echo(f"{connector} {ms}  [{status_upper}]")
        elif raw_status == "implementing":
            open_count = _count_open_issues_by_label(repo_ref, ms, IMPL_LABEL)
            total_count = _count_all_issues_by_label(repo_ref, ms, IMPL_LABEL)
            click.echo(
                f"{connector} {ms}  [{status_upper}  {open_count}/{total_count} issues open]"
            )
        else:
            click.echo(f"{connector} {ms}  [{status_upper}]")
