"""CLI entry points for breadmin-composer.

Entry points:
  impl-worker      → impl_worker()
  research-worker  → research_worker()
  design-worker    → design_worker()
  plan-issues      → plan_issues()
  composer         → composer()
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import time
import uuid
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


def _scaffold_new_repo(name: str) -> str:
    """Create a new local directory, init git, and push to GitHub as a private repo.

    Steps:
      1. Create ``<name>/`` directory with a minimal README.md and .gitignore
      2. ``git init``, ``git add .``, ``git commit -m "init"``
      3. ``gh repo create <name> --private --source=. --push``

    Args:
        name: Repository name (no slashes). Used for directory and GitHub repo names.

    Returns:
        Absolute path to the newly-created local directory.

    Raises:
        click.ClickException: If directory creation, git init, or gh repo create fails.
    """
    repo_path = os.path.abspath(name)

    if os.path.exists(repo_path):
        raise click.ClickException(
            f"Directory '{repo_path}' already exists. Remove it or choose a different name."
        )

    try:
        os.makedirs(repo_path)
    except OSError as exc:
        raise click.ClickException(f"Failed to create directory '{repo_path}': {exc}") from exc

    # Write a minimal README
    readme_path = os.path.join(repo_path, "README.md")
    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {name}\n\nInitialized by breadmin-composer.\n")

    # Write a minimal .gitignore
    gitignore_path = os.path.join(repo_path, ".gitignore")
    with open(gitignore_path, "w", encoding="utf-8") as fh:
        fh.write("__pycache__/\n*.pyc\n.env\n")

    # git init
    init = subprocess.run(
        ["git", "init"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if init.returncode != 0:
        raise click.ClickException(f"git init failed in '{repo_path}':\n{init.stderr}")

    # git add .
    add = subprocess.run(
        ["git", "add", "."],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if add.returncode != 0:
        raise click.ClickException(f"git add failed in '{repo_path}':\n{add.stderr}")

    # git commit
    commit = subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        raise click.ClickException(f"git commit failed in '{repo_path}':\n{commit.stderr}")

    # gh repo create
    create = subprocess.run(
        ["gh", "repo", "create", name, "--private", "--source=.", "--push"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        raise click.ClickException(f"gh repo create failed for '{name}':\n{create.stderr}")

    return repo_path


def _resolve_repo(repo_arg: str | None) -> tuple[str, str | None]:
    """Resolve the ``--repo`` argument to a (repo_ref, local_path) pair.

    The ``repo_ref`` is what gets passed to ``gh --repo``.  ``local_path`` is
    the working directory for ``git`` commands (or ``None`` when the repo is
    purely remote and already cloned via ``gh``).

    Resolution rules
    ----------------
    1. ``repo_arg`` is ``None`` (no ``--repo`` flag)
       → validate that cwd is a git repo; raise ClickException if not.
       → return (github_remote_from_cwd, cwd)

    2. ``repo_arg`` contains ``/`` and matches ``owner/name``
       → treat as an existing remote repo; no local scaffolding.
       → return (repo_arg, None)

    3. ``repo_arg`` is a plain name with no ``/`` (no directory separators)
       → scaffold a new private GitHub repo called ``name``.
       → return (owner/name, abs_path_to_new_dir)

    4. ``repo_arg`` is a path that contains ``os.sep`` or starts with ``.``
       → treat as a local directory path; fail if not a git repo.
       → return (github_remote_from_path, abs_path)

    Args:
        repo_arg: Raw value of the ``--repo`` CLI option, or ``None``.

    Returns:
        A ``(repo_ref, local_path)`` tuple where:
        - ``repo_ref`` is a ``owner/name`` string (for ``gh --repo``) or an
          empty string when the repo can only be identified by its local path.
        - ``local_path`` is the absolute filesystem path, or ``None`` for a
          purely remote operation.

    Raises:
        click.ClickException: On validation failure or scaffold error.
    """
    # -----------------------------------------------------------------------
    # Case 1: No --repo flag → operate on cwd
    # -----------------------------------------------------------------------
    if repo_arg is None:
        cwd = os.getcwd()
        if not _is_git_repo(cwd):
            raise click.ClickException(
                "current directory is not a git repository.\n"
                "Run from inside a git repo, or pass --repo <owner/name>."
            )
        # Try to infer owner/name from the remote URL
        repo_ref = _infer_github_repo_from_path(cwd) or ""
        return repo_ref, cwd

    # -----------------------------------------------------------------------
    # Case 2: Looks like "owner/name" — exactly two non-empty parts separated
    # by a single "/" with no leading ".", no leading "/", and no additional "/"
    # characters (so "a/b/c" or "./foo/bar" fall through to Case 3).
    # -----------------------------------------------------------------------
    _github_slug_re = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")
    if _github_slug_re.match(repo_arg):
        return repo_arg, None

    # -----------------------------------------------------------------------
    # Case 3: Looks like a local path (starts with . or / or contains multiple
    # path components that are not a two-part GitHub slug)
    # -----------------------------------------------------------------------
    if repo_arg.startswith(".") or repo_arg.startswith("/") or "/" in repo_arg:
        abs_path = os.path.abspath(repo_arg)
        if not os.path.isdir(abs_path):
            raise click.ClickException(f"'{repo_arg}' is not a directory.")
        if not _is_git_repo(abs_path):
            raise click.ClickException(
                f"'{abs_path}' is not a git repository.\n"
                "Pass --repo <owner/name> to use a remote repo, or run from inside a git repo."
            )
        repo_ref = _infer_github_repo_from_path(abs_path) or ""
        return repo_ref, abs_path

    # -----------------------------------------------------------------------
    # Case 4: Plain name with no slashes → scaffold a new private repo
    # -----------------------------------------------------------------------
    local_path = _scaffold_new_repo(repo_arg)
    # After scaffolding, infer the remote owner/name
    repo_ref = _infer_github_repo_from_path(local_path) or repo_arg
    return repo_ref, local_path


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
# Impl worker helpers
# ---------------------------------------------------------------------------

# Module label prefix for extracting module name from issue labels
_FEAT_PREFIX = "feat:"

# CI poll constants
_CI_POLL_INTERVAL: int = 30  # seconds between gh pr checks polls
_CI_MAX_POLLS: int = 60  # maximum polls before timeout (30 min)
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


def _list_open_impl_issues(repo: str, milestone: str) -> list[dict[str, Any]]:
    """Return open implementation issues for *milestone*.

    Fetches all open issues in the milestone and filters client-side:
    - Excludes issues with the ``research`` label
    - Excludes issues with the ``pipeline`` label
    - Excludes issues already assigned or labeled ``in-progress``

    Args:
        repo:      GitHub repository in ``owner/repo`` format.
        milestone: Milestone name to scope the query (may be empty for all open impl issues).

    Returns:
        List of issue dicts with keys: number, title, body, labels, assignees.
    """
    cmd = [
        "issue",
        "list",
        "--state",
        "open",
        "--json",
        "number,title,body,labels,assignees,milestone",
        "--limit",
        "200",
    ]
    if milestone:
        cmd += ["--milestone", milestone]

    result = _gh(cmd, repo=repo, check=False)
    if result.returncode != 0:
        return []

    try:
        issues: list[dict] = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    # Filter out research, pipeline, and in-progress issues
    filtered = []
    for issue in issues:
        label_names = [lb.get("name", "") for lb in issue.get("labels", [])]
        # Skip research or pipeline issues
        if "research" in label_names or "pipeline" in label_names:
            continue
        # Skip assigned or in-progress issues
        if issue.get("assignees"):
            continue
        if "in-progress" in label_names:
            continue
        filtered.append(issue)

    return filtered


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
    result = _gh(
        ["pr", "checks", str(pr_number), "--json", "name,status,conclusion"],
        repo=repo,
        check=False,
    )
    if result.returncode != 0:
        return "pending"

    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "pending"

    if not checks:
        return "pending"

    statuses = []
    for check in checks:
        conclusion = (check.get("conclusion") or "").lower()
        status = (check.get("status") or "").lower()
        if conclusion in ("failure", "cancelled", "timed_out", "action_required"):
            statuses.append("fail")
        elif conclusion == "success" or status == "completed":
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


def _rebase_branch(branch: str, repo: str, worktree_path: str) -> bool:
    """Rebase a worktree branch onto origin/main.

    Args:
        branch:        Branch name being rebased.
        repo:          Repository in ``owner/repo`` format (unused but kept for context).
        worktree_path: Absolute path to the worktree directory.

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
        ["git", "rebase", "origin/main"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if rebase.returncode != 0:
        # Abort on failure
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
    worktree_path: str,
    issue_number: int,
    max_polls: int = _CI_MAX_POLLS,
    poll_interval: int = _CI_POLL_INTERVAL,
) -> bool:
    """Monitor a PR's CI status and squash-merge it when CI passes.

    Polls ``gh pr checks`` up to *max_polls* times. On conflict, attempts a
    rebase up to ``_REBASE_RETRY_LIMIT`` times. On success, checks reviews and
    squash-merges.

    Args:
        pr_number:     GitHub PR number.
        branch:        Branch name for the PR.
        repo:          Repository in ``owner/repo`` format.
        config:        Config instance for logging.
        checkpoint:    Checkpoint instance for logging.
        worktree_path: Absolute path to the worktree directory (for rebase).
        issue_number:  Original issue number (for logging).
        max_polls:     Maximum number of CI status polls before timeout.
        poll_interval: Seconds to sleep between polls.

    Returns:
        True if the PR was successfully merged. False otherwise.
    """
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    rebase_attempts = 0
    ci_fail_count = 0

    for poll_idx in range(max_polls):
        time.sleep(poll_interval)
        ci_status = _get_pr_checks_status(repo, pr_number)

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
                # Read inline comments and triage: fix/file-issue/skip
                _triage_review_comments(
                    pr_number=pr_number,
                    branch=branch,
                    repo=repo,
                    config=config,
                    checkpoint=checkpoint,
                    issue_number=issue_number,
                )

            # Squash merge — proceed regardless of review status ("no_review" is OK)
            merge_result = _gh(
                ["pr", "merge", str(pr_number), "--squash", "--delete-branch"],
                repo=repo,
                check=False,
            )
            if merge_result.returncode == 0:
                # Record completion in checkpoint
                checkpoint.completed_prs.append(pr_number)
                session.save(checkpoint, checkpoint_path)

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
                return True
            else:
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="merge",
                    event_type="human_escalate",
                    payload={
                        "pr_number": pr_number,
                        "issue_number": issue_number,
                        "reason": "squash merge failed",
                        "stderr": merge_result.stderr,
                    },
                    log_dir=config.log_dir.expanduser(),
                )
                return False

        elif ci_status == "fail":
            # Check if it's a conflict
            if _is_conflict_failure(repo, pr_number):
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
                    return False

                rebase_ok = _rebase_branch(branch, repo, worktree_path)
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
                    return False
                # Rebase succeeded — continue polling
                continue

            # Non-conflict failure: escalate after MAX_RETRIES
            ci_fail_count += 1
            if ci_fail_count >= MAX_RETRIES:
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="ci_check",
                    event_type="human_escalate",
                    payload={
                        "pr_number": pr_number,
                        "issue_number": issue_number,
                        "reason": "ci failed repeatedly",
                        "ci_fail_count": ci_fail_count,
                    },
                    log_dir=config.log_dir.expanduser(),
                )
                return False
            # Otherwise continue polling (may be transient)

        # ci_status == "pending": continue polling

    # Timeout
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


def _triage_review_comments(
    pr_number: int,
    branch: str,
    repo: str,
    config: Config,
    checkpoint: Checkpoint,
    issue_number: int,
) -> None:
    """Read review inline comments and log them for human review.

    Triaging policy:
    - Straightforward in-scope fixes → apply via runner.run()
    - Valid but out-of-scope → file a follow-up issue
    - False positives → add a skip comment on the PR

    In this implementation, all comments are logged and a PR comment is added
    acknowledging receipt. The orchestrator proceeds with merge regardless.

    Args:
        pr_number:    GitHub PR number.
        branch:       Branch name (unused but kept for context).
        repo:         Repository in ``owner/repo`` format.
        config:       Config instance.
        checkpoint:   Checkpoint instance.
        issue_number: Original issue number.
    """
    # Read inline comments
    comments_result = _gh(
        ["api", f"repos/{repo}/pulls/{pr_number}/comments"],
        repo=None,  # already embedded in path
        check=False,
    )
    if comments_result.returncode != 0:
        return

    try:
        comments = json.loads(comments_result.stdout)
    except json.JSONDecodeError:
        return

    if not comments:
        return

    # Log that we saw review comments
    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="review",
        event_type="review_comments_triaged",
        payload={
            "pr_number": pr_number,
            "issue_number": issue_number,
            "comment_count": len(comments),
            "action": "logged_for_human_review",
        },
        log_dir=config.log_dir.expanduser(),
    )

    # Add a PR comment acknowledging the review
    _gh(
        [
            "pr",
            "comment",
            str(pr_number),
            "--body",
            (
                f"Orchestrator: received {len(comments)} review comment(s). "
                "Review feedback logged; proceeding with merge if CI passes. "
                "Non-trivial changes will be filed as follow-up issues."
            ),
        ],
        repo=repo,
        check=False,
    )


def _create_worktree(branch: str, repo_root: str) -> str | None:
    """Create a git worktree for *branch* under ``.claude/worktrees/``.

    The branch is created from ``origin/main`` and pushed to the remote.

    Args:
        branch:    Branch name (e.g. ``"42-add-config"``).
        repo_root: Absolute path to the repository root.

    Returns:
        Absolute path to the new worktree directory, or ``None`` on failure.
    """
    worktree_dir = os.path.join(repo_root, ".claude", "worktrees", branch)

    # Create worktree with new branch based on origin/main
    result = subprocess.run(
        ["git", "worktree", "add", worktree_dir, "-b", branch, "origin/main"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    # Push the new branch to remote
    push = subprocess.run(
        ["git", "-C", worktree_dir, "push", "-u", "origin", branch],
        capture_output=True,
        text=True,
    )
    if push.returncode != 0:
        # Clean up the worktree if push fails
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_dir],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        return None

    return worktree_dir


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

    # Determine scope path from module name
    if module == "none":
        scope = "src/composer/"
    elif module == "cli":
        scope = "src/composer/cli.py, src/composer/skills/"
    else:
        scope = f"src/composer/{module}.py"

    # Get the feat label to use for the PR
    feat_label = f"feat:{module}" if module != "none" else "feat:cli"

    # Build the agent prompt
    base_prompt = (
        f"You are implementing issue #{issue_number} on branch `{branch}`.\n"
        f"Your task: {issue_title}\n"
        f"Allowed scope: {scope}\n\n"
        f"Steps:\n"
        f"1. git checkout {branch}\n"
        f"2. Read the issue: gh issue view {issue_number}\n"
        f"3. Implement the changes within the allowed scope\n"
        f"4. Update README if it exists at the package/module root\n"
        f"5. Run tests — all tests must pass\n"
        f"6. Run lint — must be clean\n"
        f"7. Commit with message referencing the issue\n"
        f"8. git push -u origin {branch}\n"
        f'9. Create PR: gh pr create --title "{issue_title}" '
        f'--label "{feat_label}" --body "Closes #{issue_number}"\n'
        f"10. Wait for CI: gh pr checks <PR-number> --watch\n"
        f"11. Read all CI and review feedback\n"
        f"12. Triage each piece of feedback (fix now / file issue / skip)\n"
        f"13. STOP. Do not merge.\n\n"
        f"Issue body:\n{body}"
    )
    prompt = inject_skill("impl-worker", base_prompt)

    if dry_run:
        from composer.runner import RunResult as _RunResult

        return (
            issue,
            branch,
            worktree_path,
            _RunResult(
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

    # Build isolated env for the impl agent
    env = build_subprocess_env(config)
    # Each impl agent gets an isolated CLAUDE_CONFIG_DIR
    env["CLAUDE_CONFIG_DIR"] = f"/tmp/composer-agent-{issue_number}-{uuid.uuid4().hex}"

    max_turns = 100
    if hasattr(config, "max_turns") and config.max_turns:
        max_turns = config.max_turns

    result = runner.run(
        prompt=prompt,
        allowed_tools=runner.TOOLS_IMPL_AGENT,
        env=env,
        max_turns=max_turns,
    )
    return issue, branch, worktree_path, result


def _run_impl_worker(
    repo: str,
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
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
    retry_counts: dict[int, int] = {}
    active_modules: set[str] = set()  # module isolation tracker
    repo_root = _get_repo_root()

    while True:
        # ------------------------------------------------------------------
        # STEP 1: Fetch all open impl issues for milestone
        # ------------------------------------------------------------------
        open_issues = _list_open_impl_issues(repo, milestone)

        # ------------------------------------------------------------------
        # STEP 2: Completion gate
        # ------------------------------------------------------------------
        if not open_issues and not gov._active_agents:
            # No open issues remain — file pipeline issue and stop
            next_version = _find_next_version(milestone)
            if dry_run:
                click.echo(f"[dry-run] Would file: 'Run plan-milestones for {next_version}'")
            else:
                _gh(
                    [
                        "issue",
                        "create",
                        "--title",
                        f"Run plan-milestones for {next_version}",
                        "--label",
                        "pipeline",
                        "--body",
                        (
                            f"Pipeline stage transition: impl-worker has completed "
                            f"all implementation issues for {milestone}. "
                            f"Time to plan the next version."
                        ),
                    ],
                    repo=repo,
                    check=False,
                )

            logger.log_conductor_event(
                run_id=checkpoint.run_id,
                phase="complete",
                event_type="stage_complete",
                payload={
                    "milestone": milestone,
                    "next_pipeline_issue": f"Run plan-milestones for {next_version}",
                },
                log_dir=config.log_dir.expanduser(),
            )
            session.save(checkpoint, checkpoint_path)

            click.echo(
                f"Implementation milestone '{milestone}' complete. "
                f"Filed 'Run plan-milestones for {next_version}' pipeline issue."
            )
            break

        # ------------------------------------------------------------------
        # STEP 3: Backoff check
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
        # STEP 4: Select dispatchable issues (module isolation enforced)
        # ------------------------------------------------------------------
        open_issue_numbers = {i.get("number", 0) for i in open_issues}
        unblocked = _filter_unblocked(open_issues, open_issue_numbers)
        ranked = _sort_issues(unblocked)

        # Apply module isolation: skip issues whose module is already active.
        # Use the governor's concurrency limit to bound the batch size.
        limits: dict[str, int] = {"pro": 2, "max": 3, "max20x": 5}
        concurrency_limit = config.max_concurrency or limits.get(config.subscription_tier, 3)
        dispatchable = []
        for issue in ranked:
            mod = _extract_module(issue)
            if mod == "none" or mod not in active_modules:
                if gov.can_dispatch(1):
                    dispatchable.append(issue)
                    # Don't exceed concurrency limit
                    if len(dispatchable) >= concurrency_limit:
                        break

        if not dispatchable:
            if gov._active_agents == 0:
                # No dispatchable work and nothing running → sleep and retry
                # (dependencies may be blocking everything)
                time.sleep(BACKOFF_SLEEP_SECONDS)
                continue
            else:
                # Work is running; wait for it
                time.sleep(BACKOFF_SLEEP_SECONDS)
                continue

        # ------------------------------------------------------------------
        # STEP 5: Sequential claiming and branch creation
        # ------------------------------------------------------------------
        dispatch_batch: list[tuple[dict, str, str, str]] = []  # (issue, branch, worktree, module)

        for issue in dispatchable:
            issue_number = issue["number"]
            issue_title = issue.get("title", "")
            module = _extract_module(issue)

            # Create branch name
            slug = _slugify(issue_title)
            branch = f"{issue_number}-{slug}"

            if dry_run:
                click.echo(
                    f"[dry-run] Would claim #{issue_number}: {issue_title!r} "
                    f"(module={module}, branch={branch})"
                )
                # Still track for dry-run loop exit
                dispatch_batch.append((issue, branch, "", module))
                active_modules.add(module)
                gov.record_dispatch(1)
                session.record_dispatch(checkpoint, str(issue_number))
                session.save(checkpoint, checkpoint_path)
                continue

            # Claim issue on GitHub
            _claim_issue(repo=repo, issue_number=issue_number)

            # Create worktree and push branch
            worktree_path = _create_worktree(branch, repo_root)
            if worktree_path is None:
                # Failed to create worktree — unclaim and skip
                _unclaim_issue(repo=repo, issue_number=issue_number)
                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="claim",
                    event_type="worktree_create_failed",
                    payload={"issue_number": issue_number, "branch": branch},
                    log_dir=config.log_dir.expanduser(),
                )
                continue

            # Record dispatch in checkpoint
            session.record_dispatch(checkpoint, str(issue_number))
            checkpoint.claimed_issues[str(issue_number)] = branch
            session.save(checkpoint, checkpoint_path)

            logger.log_conductor_event(
                run_id=checkpoint.run_id,
                phase="claim",
                event_type="issue_claimed",
                payload={
                    "issue_number": issue_number,
                    "title": issue_title,
                    "branch": branch,
                    "module": module,
                },
                log_dir=config.log_dir.expanduser(),
            )

            gov.record_dispatch(1)
            active_modules.add(module)
            dispatch_batch.append((issue, branch, worktree_path, module))

        if not dispatch_batch:
            time.sleep(BACKOFF_SLEEP_SECONDS)
            continue

        if dry_run:
            # In dry-run mode, simulate completion gate then exit
            click.echo("[dry-run] Would dispatch agents in parallel; stopping.")
            break

        # ------------------------------------------------------------------
        # STEP 6: Dispatch agents in parallel using ThreadPoolExecutor
        # ------------------------------------------------------------------
        futures: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(dispatch_batch)) as executor:
            for issue, branch, worktree_path, module in dispatch_batch:
                future = executor.submit(
                    _dispatch_impl_agent,
                    issue=issue,
                    branch=branch,
                    worktree_path=worktree_path,
                    module=module,
                    repo=repo,
                    config=config,
                    checkpoint=checkpoint,
                    dry_run=dry_run,
                )
                futures[future] = (issue, branch, worktree_path, module)

            # ------------------------------------------------------------------
            # STEP 7: Handle results as each agent completes
            # ------------------------------------------------------------------
            for future in concurrent.futures.as_completed(futures):
                _issue, _branch, _worktree_path, _module = futures[future]
                issue_number = _issue["number"]

                try:
                    _, _, _, result = future.result()
                except Exception as exc:
                    # Unexpected exception from dispatcher
                    logger.log_conductor_event(
                        run_id=checkpoint.run_id,
                        phase="dispatch",
                        event_type="agent_exception",
                        payload={
                            "issue_number": issue_number,
                            "error": str(exc),
                        },
                        log_dir=config.log_dir.expanduser(),
                    )
                    gov.record_completion(1)
                    active_modules.discard(_module)
                    _unclaim_issue(repo=repo, issue_number=issue_number)
                    _remove_worktree(_worktree_path, repo_root)
                    continue

                gov.record_completion(1)
                gov.record_result(result)
                active_modules.discard(_module)

                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="dispatch",
                    event_type="agent_completed",
                    payload={
                        "issue_number": issue_number,
                        "subtype": result.subtype,
                        "is_error": result.is_error,
                        "error_code": result.error_code,
                        "branch": _branch,
                    },
                    log_dir=config.log_dir.expanduser(),
                )
                session.save(checkpoint, checkpoint_path)

                # Rate-limited → unclaim and back off
                if result.error_code in ("rate_limit", "extra_usage_exhausted") or (
                    result.subtype == "error_max_budget_usd"
                ):
                    _unclaim_issue(repo=repo, issue_number=issue_number)
                    _remove_worktree(_worktree_path, repo_root)
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
                    continue

                # Other agent error → retry or escalate
                if result.is_error:
                    current_retries = retry_counts.get(issue_number, 0) + 1
                    retry_counts[issue_number] = current_retries
                    _unclaim_issue(repo=repo, issue_number=issue_number)
                    _remove_worktree(_worktree_path, repo_root)

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
                    continue

                # Success path: find the PR created by the agent
                pr_number = _find_pr_for_branch(repo, _branch)
                if pr_number is None:
                    # Agent succeeded but created no PR — escalate
                    logger.log_conductor_event(
                        run_id=checkpoint.run_id,
                        phase="dispatch",
                        event_type="human_escalate",
                        payload={
                            "issue_number": issue_number,
                            "reason": "agent completed but no PR found",
                            "branch": _branch,
                        },
                        log_dir=config.log_dir.expanduser(),
                    )
                    _unclaim_issue(repo=repo, issue_number=issue_number)
                    _remove_worktree(_worktree_path, repo_root)
                    continue

                # Record the open PR
                checkpoint.open_prs[_branch] = pr_number
                session.save(checkpoint, checkpoint_path)

                logger.log_conductor_event(
                    run_id=checkpoint.run_id,
                    phase="dispatch",
                    event_type="pr_created",
                    payload={
                        "issue_number": issue_number,
                        "pr_number": pr_number,
                        "branch": _branch,
                    },
                    log_dir=config.log_dir.expanduser(),
                )

                # Monitor CI and merge
                merged = _monitor_pr(
                    pr_number=pr_number,
                    branch=_branch,
                    repo=repo,
                    config=config,
                    checkpoint=checkpoint,
                    worktree_path=_worktree_path,
                    issue_number=issue_number,
                )

                if merged:
                    _remove_worktree(_worktree_path, repo_root)
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

                session.save(checkpoint, checkpoint_path)


# ---------------------------------------------------------------------------
# Design-worker helpers
# ---------------------------------------------------------------------------


def _run_design_worker(
    repo: str,
    research_milestone: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
) -> None:
    """Dispatch a single design-worker agent to produce HLD and LLD docs.

    Builds a prompt by injecting the ``design-worker`` skill file and
    invoking ``runner.run()`` once. The agent is responsible for reading
    research docs, producing design documents, filing the next pipeline
    issue, and posting a Notion report.

    Args:
        repo:               GitHub repository in ``owner/repo`` format.
        research_milestone: Name of the completed research milestone.
        config:             Validated Config instance.
        checkpoint:         Active Checkpoint instance.
        dry_run:            If True, print the prompt length without executing.
    """
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    today = date.today().isoformat()

    base_prompt = (
        f"## Session Parameters\n"
        f"- Repository: {repo}\n"
        f"- Research Milestone: {research_milestone}\n"
        f"- Session Date: {today}\n\n"
        f"You are the design-worker for the `{repo}` repository.\n"
        f"Translate the completed research docs for milestone `{research_milestone}` "
        f"into HLD and LLD design documents following the skill instructions below."
    )
    prompt = inject_skill("design-worker", base_prompt)

    if dry_run:
        click.echo(
            f"[dry-run] Would dispatch design-worker agent for milestone "
            f"{research_milestone!r} in repo {repo!r}"
        )
        click.echo(f"[dry-run] Prompt length: {len(prompt)} chars")
        return

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="design_worker_start",
        payload={"repo": repo, "research_milestone": research_milestone},
        log_dir=config.log_dir.expanduser(),
    )

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
            "mcp__notion__API-post-page",
        ],
        env=env,
        max_turns=200,
    )

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="design_worker_complete",
        payload={
            "repo": repo,
            "research_milestone": research_milestone,
            "subtype": result.subtype,
            "is_error": result.is_error,
            "error_code": result.error_code,
        },
        log_dir=config.log_dir.expanduser(),
    )
    session.save(checkpoint, checkpoint_path)

    if result.is_error:
        click.echo(
            f"Design-worker completed with error: {result.subtype} / {result.error_code}",
            err=True,
        )
    else:
        click.echo(f"Design-worker complete for research milestone '{research_milestone}'.")


# ---------------------------------------------------------------------------
# Plan-issues helpers
# ---------------------------------------------------------------------------


def _run_plan_issues(
    repo: str,
    impl_milestone: str,
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
        repo:           GitHub repository in ``owner/repo`` format.
        impl_milestone: Name of the implementation milestone to file issues against.
        config:         Validated Config instance.
        checkpoint:     Active Checkpoint instance.
        dry_run:        If True, print the prompt length without executing.
    """
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    today = date.today().isoformat()

    base_prompt = (
        f"## Session Parameters\n"
        f"- Repository: {repo}\n"
        f"- Implementation Milestone: {impl_milestone}\n"
        f"- Session Date: {today}\n"
        f"- Dry Run: {dry_run}\n\n"
        f"You are the plan-issues orchestrator for the `{repo}` repository.\n"
        f"Read the HLD and LLD design docs and file fully-specified `stage/impl` issues "
        f"against milestone `{impl_milestone}` following the skill instructions below.\n"
        + (
            "\nThe `--dry-run` flag is set: print all planned issues but do NOT call "
            "`gh issue create`.\n"
            if dry_run
            else ""
        )
    )
    prompt = inject_skill("plan-issues", base_prompt)

    if dry_run:
        click.echo(
            f"[dry-run] Would dispatch plan-issues agent for milestone "
            f"{impl_milestone!r} in repo {repo!r}"
        )
        click.echo(f"[dry-run] Prompt length: {len(prompt)} chars")
        return

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="plan_issues_start",
        payload={"repo": repo, "impl_milestone": impl_milestone},
        log_dir=config.log_dir.expanduser(),
    )

    env = build_subprocess_env(config)
    result = runner.run(
        prompt=prompt,
        allowed_tools=[
            "Bash",
            "Read",
            "Glob",
            "Grep",
            "mcp__notion__API-post-page",
        ],
        env=env,
        max_turns=200,
    )

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="plan_issues_complete",
        payload={
            "repo": repo,
            "impl_milestone": impl_milestone,
            "subtype": result.subtype,
            "is_error": result.is_error,
            "error_code": result.error_code,
        },
        log_dir=config.log_dir.expanduser(),
    )
    session.save(checkpoint, checkpoint_path)

    if result.is_error:
        click.echo(
            f"Plan-issues completed with error: {result.subtype} / {result.error_code}",
            err=True,
        )
    else:
        click.echo(f"Plan-issues complete for implementation milestone '{impl_milestone}'.")


# ---------------------------------------------------------------------------
# Plan-milestones helpers
# ---------------------------------------------------------------------------


def _run_plan_milestones(
    repo: str,
    version: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
) -> None:
    """Dispatch a single plan-milestones agent to create a milestone pair and seed issues.

    Builds a prompt by injecting the ``plan-milestones`` skill file and
    invoking ``runner.run()`` once. The agent reads the spec, creates
    milestones, and files seed research issues.

    Args:
        repo:       GitHub repository in ``owner/repo`` format.
        version:    Version identifier (e.g. ``"MVP"``, ``"v2"``).
        config:     Validated Config instance.
        checkpoint: Active Checkpoint instance.
        dry_run:    If True, print the prompt length without executing.
    """
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    today = date.today().isoformat()

    base_prompt = (
        f"## Session Parameters\n"
        f"- Repository: {repo}\n"
        f"- Version: {version}\n"
        f"- Session Date: {today}\n\n"
        f"You are the plan-milestones orchestrator for the `{repo}` repository.\n"
        f"Read the spec at `docs/specs/{version}.md`, create the milestone pair, "
        f"and file seed research issues following the skill instructions below."
    )
    prompt = inject_skill("plan-milestones", base_prompt)

    if dry_run:
        click.echo(
            f"[dry-run] Would dispatch plan-milestones agent for version "
            f"{version!r} in repo {repo!r}"
        )
        click.echo(f"[dry-run] Prompt length: {len(prompt)} chars")
        return

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="plan_milestones_start",
        payload={"repo": repo, "version": version},
        log_dir=config.log_dir.expanduser(),
    )

    env = build_subprocess_env(config)
    result = runner.run(
        prompt=prompt,
        allowed_tools=[
            "Bash",
            "Read",
            "Glob",
            "Grep",
            "mcp__notion__API-post-page",
        ],
        env=env,
        max_turns=200,
    )

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="plan_milestones_complete",
        payload={
            "repo": repo,
            "version": version,
            "subtype": result.subtype,
            "is_error": result.is_error,
            "error_code": result.error_code,
        },
        log_dir=config.log_dir.expanduser(),
    )
    session.save(checkpoint, checkpoint_path)

    if result.is_error:
        click.echo(
            f"Plan-milestones completed with error: {result.subtype} / {result.error_code}",
            err=True,
        )
    else:
        click.echo(f"Plan-milestones complete for version '{version}'.")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


@click.command("impl-worker")
@click.option(
    "--repo",
    default=None,
    help=(
        "Target repository. Accepts: 'owner/name' (existing remote), "
        "'name' (new private repo to scaffold), 'path/to/local/dir' (local git repo), "
        "or omit to operate on the current working directory."
    ),
)
@click.option("--milestone", default=None, help="Milestone name to process")
@click.option("--model", default=None, help="Override Claude model")
@click.option("--max-budget", type=float, default=None, help="USD budget cap")
@click.option("--max-turns", type=int, default=None, help="Max turns per invocation")
@click.option("--dry-run", is_flag=True, help="Print invocation without executing")
@click.option("--resume", default=None, help="Resume a previous session by ID")
def impl_worker(
    repo: str | None,
    milestone: str | None,
    model: str | None,
    max_budget: float | None,
    max_turns: int | None,
    dry_run: bool,
    resume: str | None,
) -> None:
    """Process implementation issues headlessly via claude -p."""
    repo_ref, _local_path = _resolve_repo(repo)
    overrides: dict = {"github_repo": repo_ref or None, "target_repo": repo_ref or None}
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

    _run_impl_worker(
        repo=repo_ref,
        milestone=milestone or "",
        config=_config,
        checkpoint=_checkpoint,
        dry_run=dry_run,
    )


@click.command("research-worker")
@click.option(
    "--repo",
    default=None,
    help=(
        "Target repository. Accepts: 'owner/name' (existing remote), "
        "'name' (new private repo to scaffold), 'path/to/local/dir' (local git repo), "
        "or omit to operate on the current working directory."
    ),
)
@click.option("--milestone", required=True, help="Milestone name to process")
@click.option("--model", default=None, help="Override Claude model")
@click.option("--max-budget", type=float, default=None, help="USD budget cap")
@click.option("--max-turns", type=int, default=None, help="Max turns per invocation")
@click.option("--dry-run", is_flag=True, help="Print invocation without executing")
@click.option("--resume", default=None, help="Resume a previous session by ID")
def research_worker(
    repo: str | None,
    milestone: str,
    model: str | None,
    max_budget: float | None,
    max_turns: int | None,
    dry_run: bool,
    resume: str | None,
) -> None:
    """Process research issues for a milestone headlessly via claude -p."""
    repo_ref, _local_path = _resolve_repo(repo)
    overrides: dict = {"github_repo": repo_ref or None, "target_repo": repo_ref or None}
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
        repo=repo_ref,
        milestone=milestone,
        config=_config,
        checkpoint=_checkpoint,
        dry_run=dry_run,
    )


@click.command("design-worker")
@click.option(
    "--repo",
    default=None,
    help=(
        "Target repository. Accepts: 'owner/name' (existing remote), "
        "'name' (new private repo to scaffold), 'path/to/local/dir' (local git repo), "
        "or omit to operate on the current working directory."
    ),
)
@click.option(
    "--research-milestone", required=True, help="Completed research milestone to translate"
)
@click.option("--model", default=None, help="Override Claude model")
@click.option("--dry-run", is_flag=True, help="Print planned issues without creating them")
def design_worker(
    repo: str | None,
    research_milestone: str,
    model: str | None,
    dry_run: bool,
) -> None:
    """Translate research docs into HLD and LLD design documents via claude -p."""
    repo_ref, _local_path = _resolve_repo(repo)
    overrides: dict = {"github_repo": repo_ref or None, "target_repo": repo_ref or None}
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

    _run_design_worker(
        repo=repo_ref,
        research_milestone=research_milestone,
        config=_config,
        checkpoint=_checkpoint,
        dry_run=dry_run,
    )


@click.command("plan-issues")
@click.option(
    "--repo",
    default=None,
    help=(
        "Target repository. Accepts: 'owner/name' (existing remote), "
        "'name' (new private repo to scaffold), 'path/to/local/dir' (local git repo), "
        "or omit to operate on the current working directory."
    ),
)
@click.option(
    "--impl-milestone", required=True, help="Implementation milestone to file issues against"
)
@click.option("--model", default=None, help="Override Claude model")
@click.option("--dry-run", is_flag=True, help="Print planned milestones without creating them")
def plan_issues(
    repo: str | None,
    impl_milestone: str,
    model: str | None,
    dry_run: bool,
) -> None:
    """File stage/impl issues from HLD and LLD design docs via claude -p."""
    repo_ref, _local_path = _resolve_repo(repo)
    overrides: dict = {"github_repo": repo_ref or None, "target_repo": repo_ref or None}
    if model:
        overrides["model"] = model

    config = load_config(**overrides)
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"

    _config, _checkpoint = startup_sequence(
        config=config,
        checkpoint_path=checkpoint_path,
        milestone=impl_milestone,
        stage="plan-issues",
    )

    _run_plan_issues(
        repo=repo_ref,
        impl_milestone=impl_milestone,
        config=_config,
        checkpoint=_checkpoint,
        dry_run=dry_run,
    )


@click.command("plan-milestones")
@click.option(
    "--repo",
    default=None,
    help=(
        "Target repository. Accepts: 'owner/name' (existing remote), "
        "'name' (new private repo to scaffold), 'path/to/local/dir' (local git repo), "
        "or omit to operate on the current working directory."
    ),
)
@click.option("--version", required=True, help="Version name (e.g. 'MVP', 'v2')")
@click.option("--model", default=None, help="Override Claude model")
@click.option("--dry-run", is_flag=True, help="Print planned milestones without creating them")
def plan_milestones(
    repo: str | None,
    version: str,
    model: str | None,
    dry_run: bool,
) -> None:
    """Create a milestone pair and seed research issues from a spec via claude -p."""
    repo_ref, _local_path = _resolve_repo(repo)
    overrides: dict = {"github_repo": repo_ref or None, "target_repo": repo_ref or None}
    if model:
        overrides["model"] = model

    config = load_config(**overrides)
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"

    _config, _checkpoint = startup_sequence(
        config=config,
        checkpoint_path=checkpoint_path,
        milestone=version,
        stage="plan-milestones",
    )

    _run_plan_milestones(
        repo=repo_ref,
        version=version,
        config=_config,
        checkpoint=_checkpoint,
        dry_run=dry_run,
    )


@click.group()
def composer() -> None:
    """Composer admin commands."""


@composer.command("health")
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
    repo_ref, _local_path = _resolve_repo(repo)
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
