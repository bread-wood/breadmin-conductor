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
import subprocess
import tempfile
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import click

from brimstone import health, logger, runner, session
from brimstone.config import (
    Config,
    OrchestratorNestingError,
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
TRIAGE_LABEL: str = "triage"
RESEARCH_LABEL: str = "stage/research"
DESIGN_LABEL: str = "stage/design"
BACKOFF_SLEEP_SECONDS: int = 30
STALL_MAX_ITERATIONS: int = 5  # 5 × BACKOFF_SLEEP_SECONDS = 2.5 min before escalation

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
        skip_checks:      Health check names to skip (for headless commands that
                          target a remote repo and don't require a local git cwd).

    Returns:
        A (Config, Checkpoint) tuple ready for the worker loop.

    Raises:
        OrchestratorNestingError: If CLAUDECODE=1 is set in the environment.
        FatalHealthCheckError:    If any health check is fatal.
        ValueError:               If resume_run_id is provided and does not
                                  match the checkpoint's run_id.
    """
    # Set GH_TOKEN so that orchestrator _gh() calls authenticate as yeast-bot.
    # pydantic-settings loads GITHUB_TOKEN from .env but does not write to
    # os.environ, so we propagate it here manually.
    if config.github_token:
        os.environ["GH_TOKEN"] = config.github_token

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


def _scaffold_new_repo(name: str) -> str:
    """Create a new local directory, init git, and push to GitHub as a private repo.

    Steps:
      1. Create ``<name>/`` directory with a minimal README.md and .gitignore
      2. ``git init``, ``git add .``, ``git commit -m "init"``
      3. ``gh repo create <name> --private --source=. --push``

    If the directory already exists and is a git repository, the scaffold is
    treated as already complete — ``_ensure_remote`` is called to guarantee a
    remote is configured, and the path is returned immediately.

    If ``gh repo create`` fails because the repository name already exists on
    GitHub, the error is treated as success and ``_ensure_remote`` is called to
    configure the remote if needed.

    Args:
        name: Repository name (no slashes). Used for directory and GitHub repo names.

    Returns:
        Absolute path to the newly-created (or already-existing) local directory.

    Raises:
        click.ClickException: If directory creation, git init, or gh repo create fails.
    """
    repo_path = os.path.abspath(name)

    if os.path.exists(repo_path):
        if _is_git_repo(repo_path):
            # Already scaffolded — reuse as-is, ensure remote is configured.
            _ensure_remote(repo_path, name)
            return repo_path
        raise click.ClickException(
            f"Directory '{repo_path}' already exists but is not a git repository. "
            "Remove it or choose a different name."
        )

    try:
        os.makedirs(repo_path)
    except OSError as exc:
        raise click.ClickException(f"Failed to create directory '{repo_path}': {exc}") from exc

    # Write a minimal README
    readme_path = os.path.join(repo_path, "README.md")
    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(f"# {name}\n\nInitialized by brimstone.\n")

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
        if "Name already exists" in create.stderr or "already exists" in create.stderr.lower():
            _ensure_remote(repo_path, name)
        else:
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
    # Case 4: Plain name with no slashes.
    # -----------------------------------------------------------------------

    # Sub-case 4a: If we're already inside a git repo whose remote name
    # matches repo_arg, use the cwd — don't scaffold a nested directory.
    cwd = os.getcwd()
    if _is_git_repo(cwd):
        cwd_remote = _infer_github_repo_from_path(cwd)
        if cwd_remote and cwd_remote.split("/")[-1] == repo_arg:
            return cwd_remote, cwd

    # Sub-case 4b: Check if the repo already exists on GitHub (gh repo view).
    # If it does, use it as a remote-only reference — no local scaffolding.
    # Only scaffold when the repo genuinely does not exist yet.
    view_result = subprocess.run(
        ["gh", "repo", "view", repo_arg, "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    if view_result.returncode == 0 and view_result.stdout.strip():
        # Repo already exists on GitHub — use it as a remote reference
        return view_result.stdout.strip(), None

    # Repo does not exist — scaffold it
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


def _list_in_progress_research_issues(repo: str, milestone: str) -> list[dict[str, Any]]:
    """Return open research issues that are currently in-progress for *milestone*.

    Used at research-worker startup to resume monitoring PRs from a previous run.
    """
    result = _gh(
        [
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            f"{RESEARCH_LABEL},in-progress",
            "--milestone",
            milestone,
            "--json",
            "number,title",
            "--limit",
            "50",
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


def _count_all_open_research_issues(repo: str, milestone: str) -> int:
    """Return the total count of open research issues for *milestone*, including in-progress.

    Used by the design-worker Gate 1 check to verify all research is done.
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


def _list_open_design_issues(repo: str, milestone: str) -> list[dict[str, Any]]:
    """Return open, unassigned, non-in-progress stage/design issues for *milestone*.

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
            DESIGN_LABEL,
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

    filtered = []
    for issue in issues:
        if issue.get("assignees"):
            continue
        label_names = [lb.get("name", "") for lb in issue.get("labels", [])]
        if "in-progress" in label_names:
            continue
        filtered.append(issue)
    return filtered


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
    result = _gh(
        ["repo", "view", "--json", "defaultBranchRef", "--jq", ".defaultBranchRef.name"],
        repo=repo,
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


def _find_next_milestone(repo: str, current_milestone: str) -> str | None:
    """Find the next open milestone beyond *current_milestone* (by milestone number).

    Args:
        repo:               Repository in ``owner/repo`` format.
        current_milestone:  Title of the current milestone.

    Returns:
        Title of the next open milestone, or ``None`` if none exists.
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

    # Find the next open milestone by number
    candidates = [
        ms
        for ms in milestones
        if ms.get("state", "").lower() == "open" and ms.get("number", 0) > current_number
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda m: m.get("number", 0))
    return candidates[0].get("title")


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
) -> None:
    """Create the next pipeline stage tracking issue.

    Args:
        repo:        Repository in ``owner/repo`` format.
        next_worker: Name of the next worker stage (e.g. ``"design-worker"``).
        milestone:   Milestone that just completed (used in the title and assignment).
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
        "--milestone",
        milestone,
    ]
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
        timeout_seconds=config.agent_timeout_minutes * 60,
        model=config.model,
        prefix="[triage] ",
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
            timeout_seconds=config.agent_timeout_minutes * 60,
            model=config.model,
            prefix=f"[blocking-check #{issue_number}] ",
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
    stall_count: int = 0
    repo_root = _get_repo_root()

    today = date.today().isoformat()

    # Pre-check: the milestone must exist before we can do anything useful.
    if not dry_run and not _milestone_exists(repo, milestone):
        click.echo(
            f"Error: Milestone '{milestone}' does not exist on GitHub. "
            "Did plan-milestones complete successfully?",
            err=True,
        )
        raise SystemExit(1)

    default_branch = _get_default_branch_for_repo(repo) if not dry_run else config.default_branch

    # Resume: merge any branches for in-progress issues that already have a pushed branch.
    # Research agents push directly (no PR) — look for a branch named <N>-*.
    if not dry_run:
        for stale in _list_in_progress_research_issues(repo, milestone):
            stale_number: int = stale["number"]
            # Check for a branch that looks like it belongs to this issue
            branch_result = _gh(
                ["api", f"repos/{repo}/git/refs/heads", "--jq",
                 f"[.[] | select(.ref | startswith(\"refs/heads/{stale_number}-\")) | .ref | ltrimstr(\"refs/heads/\")]"],
                check=False,
            )
            branches: list[str] = []
            try:
                branches = json.loads(branch_result.stdout or "[]")
            except json.JSONDecodeError:
                pass

            if branches:
                stale_branch = branches[0]
                click.echo(
                    f"[research-worker] Resuming: merging branch {stale_branch!r} for issue #{stale_number}",
                    err=True,
                )
                _merge_branch_direct(
                    repo=repo,
                    branch=stale_branch,
                    default_branch=default_branch,
                    issue_number=stale_number,
                    config=config,
                    checkpoint=checkpoint,
                )
            else:
                # No branch found — agent crashed before pushing; unclaim for re-dispatch.
                _unclaim_issue(repo, stale_number)
                click.echo(
                    f"[research-worker] Unclaimed stale #{stale_number} (no branch found)",
                    err=True,
                )

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
                # Nothing is running and nothing is unblocked — detect stall
                stall_count += 1
                if stall_count >= STALL_MAX_ITERATIONS:
                    logger.log_conductor_event(
                        run_id=checkpoint.run_id,
                        phase="dispatch",
                        event_type="human_escalate",
                        payload={
                            "reason": "deadlock_detected",
                            "stall_iterations": stall_count,
                            "stall_duration_seconds": stall_count * BACKOFF_SLEEP_SECONDS,
                            "action_required": (
                                "All blocking research issues are dependency-blocked "
                                "and no agents are running. Manual intervention required "
                                "to resolve the dependency cycle or close/skip issues."
                            ),
                        },
                        log_dir=config.log_dir.expanduser(),
                    )
                    break
                time.sleep(BACKOFF_SLEEP_SECONDS)
                continue
            else:
                stall_count = 0
                time.sleep(BACKOFF_SLEEP_SECONDS)
                continue

        stall_count = 0
        ranked = _sort_issues(unblocked)
        issue = ranked[0]
        issue_number: int = issue["number"]

        # ------------------------------------------------------------------
        # STEP 4: Sanitize issue body
        # ------------------------------------------------------------------
        raw_body = issue.get("body") or ""
        body = _sanitize_issue_body(raw_body)

        # ------------------------------------------------------------------
        # STEP 5: Build prompt (branch name and worktree path computed here)
        # ------------------------------------------------------------------
        slug = _slugify(issue.get("title", "")[:40])
        branch_name = f"{issue_number}-{slug}"
        worktree_dir = os.path.join(repo_root, ".claude", "worktrees", branch_name)

        base_prompt = (
            f"## MANDATORY: Working Directory\n"
            f"You are working in an isolated git worktree. Your FIRST action must be:\n"
            f"```\n"
            f"cd {worktree_dir}\n"
            f"```\n"
            f"ALL file writes and git operations must happen inside `{worktree_dir}`.\n"
            f"The branch `{branch_name}` is already checked out there.\n"
            f"Do NOT write to /tmp, /var/folders, ~/, or the main repo checkout.\n"
            f"Your research document goes in: `{worktree_dir}/docs/research/{milestone}/`\n"
            f"\n"
            f"## Session Parameters\n"
            f"- Repository: {repo}\n"
            f"- Active Milestone: {milestone}\n"
            f"- Branch: {branch_name}\n"
            f"- Working Directory: {worktree_dir}\n"
            f"- Issue: #{issue_number} — {issue.get('title', '')}\n"
            f"- Session Date: {today}\n\n"
            f"{body}"
        )
        skill_tmp = write_skill_tmp("research-worker")

        if dry_run:
            skill_tmp.unlink(missing_ok=True)
            click.echo(
                f"[dry-run] Would dispatch research agent for issue "
                f"#{issue_number}: {issue.get('title', '')}"
            )
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
        # STEP 6: Claim issue and create isolated worktree
        # ------------------------------------------------------------------
        _claim_issue(repo=repo, issue_number=issue_number)
        session.record_dispatch(checkpoint, str(issue_number))
        session.save(checkpoint, checkpoint_path)

        # Create branch + worktree so the agent has an isolated directory to work in.
        # _create_worktree also pushes the branch to remote.
        worktree_path = _create_worktree(branch_name, repo_root, default_branch)
        if worktree_path is None:
            click.echo(
                f"[research-worker] Failed to create worktree for #{issue_number} "
                f"(branch={branch_name!r}) — unclaiming",
                err=True,
            )
            _unclaim_issue(repo=repo, issue_number=issue_number)
            continue

        logger.log_conductor_event(
            run_id=checkpoint.run_id,
            phase="claim",
            event_type="issue_claimed",
            payload={
                "issue_number": issue_number,
                "title": issue.get("title", ""),
                "branch": branch_name,
                "worktree": worktree_path,
            },
            log_dir=config.log_dir.expanduser(),
        )

        gov.record_dispatch(1)

        # ------------------------------------------------------------------
        # STEP 7: Run the research agent
        # ------------------------------------------------------------------
        env = build_subprocess_env(config)
        env["CLAUDE_CONFIG_DIR"] = f"/tmp/brimstone-agent-{issue_number}-{uuid.uuid4().hex}"
        try:
            result = runner.run(
                prompt=base_prompt,
                allowed_tools=[
                    "Bash",
                    "Read",
                    "Edit",
                    "Write",
                    "Glob",
                    "Grep",
                    "WebSearch",
                    "WebFetch",
                ],
                append_system_prompt_file=skill_tmp,
                env=env,
                max_turns=100,
                timeout_seconds=config.agent_timeout_minutes * 60,
                model=config.model,
                prefix=f"[research-worker #{issue_number}] ",
            )
        finally:
            skill_tmp.unlink(missing_ok=True)
            # Always remove the worktree after the agent exits
            _remove_worktree(worktree_path, repo_root)

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

        if not result.is_error:
            # Success: merge the branch directly (no PR needed for research docs)
            branch_result = _gh(
                ["api", f"repos/{repo}/git/refs/heads", "--jq",
                 f"[.[] | select(.ref | startswith(\"refs/heads/{issue_number}-\")) | .ref | ltrimstr(\"refs/heads/\")]"],
                check=False,
            )
            branches: list[str] = []
            try:
                branches = json.loads(branch_result.stdout or "[]")
            except json.JSONDecodeError:
                pass

            if branches:
                _merge_branch_direct(
                    repo=repo,
                    branch=branches[0],
                    default_branch=default_branch,
                    issue_number=issue_number,
                    config=config,
                    checkpoint=checkpoint,
                )
                # Remove in-progress label and assignee. GitHub auto-closes the
                # issue via "Closes #N" in the commit message but never touches
                # labels or assignees.
                _unclaim_issue(repo=repo, issue_number=issue_number)
            else:
                click.echo(
                    f"[research-worker] Warning: no branch found for issue #{issue_number}",
                    err=True,
                )

            # Apply triage rubric to any follow-up issues filed by the agent
            _apply_triage_rubric(
                repo=repo,
                milestone=milestone,
                config=config,
                checkpoint=checkpoint,
            )

        elif result.error_code in ("rate_limit", "extra_usage_exhausted") or (
            result.subtype == "error_max_budget_usd"
        ):
            # Rate-limited or budget exhausted: unclaim and back off.
            # Delete the remote branch so the resume logic starts clean on retry.
            _unclaim_issue(repo=repo, issue_number=issue_number)
            _gh(
                ["api", f"repos/{repo}/git/refs/heads/{branch_name}", "--method", "DELETE"],
                check=False,
            )
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
            # Other error: increment retry count, unclaim for retry.
            # Delete the remote branch so the resume logic starts clean on retry.
            current_retries = retry_counts.get(issue_number, 0) + 1
            retry_counts[issue_number] = current_retries
            _unclaim_issue(repo=repo, issue_number=issue_number)
            _gh(
                ["api", f"repos/{repo}/git/refs/heads/{branch_name}", "--method", "DELETE"],
                check=False,
            )

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

    # Migrate non-blocking issues to the next milestone
    next_ms = _find_next_milestone(repo, milestone)
    for issue in open_issues:
        issue_number = issue["number"]
        if dry_run:
            click.echo(f"[dry-run] Would migrate #{issue_number} to {next_ms or 'next milestone'}")
        else:
            if next_ms:
                _migrate_issue_to_milestone(repo=repo, issue_number=issue_number, milestone=next_ms)

    # Log stage_complete
    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="complete",
        event_type="stage_complete",
        payload={
            "milestone": milestone,
            "non_blocking_migrated": len(open_issues),
            "next_milestone": next_ms,
        },
        log_dir=config.log_dir.expanduser(),
    )

    # File HLD design issue and pipeline issue
    hld_title = f"Design: HLD for {milestone}"
    if dry_run:
        click.echo(f"[dry-run] Would file: {hld_title!r}")
        click.echo(f"[dry-run] Would file: 'Run design-worker for {milestone}'")
    else:
        _file_design_issue_if_missing(
            repo=repo,
            milestone=milestone,
            title=hld_title,
            body=(
                "## Deliverable\n"
                f"`docs/design/{milestone}/HLD.md`\n\n"
                "## Instructions\n"
                f"Read all merged research docs in `docs/research/{milestone}/`. Write the HLD. "
                "For each module identified, file a `Design: LLD for <module>` issue "
                "with label `stage/design` and this milestone. Check for duplicates first.\n\n"
                "## Acceptance Criteria\n"
                f"- `docs/design/{milestone}/HLD.md` committed and PR created\n"
                "- One `Design: LLD for <module>` issue filed per module"
            ),
        )
        _file_pipeline_issue(
            repo=repo,
            next_worker="design-worker",
            milestone=milestone,
        )

    session.save(checkpoint, checkpoint_path)

    click.echo(
        f"Research milestone '{milestone}' complete. "
        f"Migrated {len(open_issues)} non-blocking issue(s). "
        f"Filed HLD design issue and 'Run design-worker for {milestone}' pipeline issue."
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
        if "stage/research" in label_names or "pipeline" in label_names:
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
        return "pass"  # no CI configured — treat as green

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


def _merge_branch_direct(
    repo: str,
    branch: str,
    default_branch: str,
    issue_number: int,
    config: Config,
    checkpoint: Checkpoint,
) -> bool:
    """Merge a branch directly into the default branch via the GitHub API (no PR).

    Used for research and design stages where review is not needed.
    Deletes the branch after a successful merge.

    Returns True on success, False on failure.
    """
    # Server-side merge via GitHub API
    result = _gh(
        [
            "api",
            f"repos/{repo}/merges",
            "--method", "POST",
            "-f", f"base={default_branch}",
            "-f", f"head={branch}",
            "-f", f"commit_message=docs: merge branch {branch} (#{issue_number}) [skip ci]",
        ],
        check=False,
    )

    if result.returncode != 0:
        # 204 = already up to date (branch already merged), treat as success
        stderr_lower = (result.stderr or "").lower()
        if "already merged" in stderr_lower or "204" in (result.stdout or ""):
            pass
        else:
            logger.log_conductor_event(
                run_id=checkpoint.run_id,
                phase="merge",
                event_type="human_escalate",
                payload={
                    "issue_number": issue_number,
                    "branch": branch,
                    "reason": "direct merge failed",
                    "stderr": result.stderr,
                },
                log_dir=config.log_dir.expanduser(),
            )
            click.echo(
                f"[research-worker] Direct merge of {branch} failed: {result.stderr}",
                err=True,
            )
            return False

    # Delete the branch
    _gh(
        ["api", f"repos/{repo}/git/refs/heads/{branch}", "--method", "DELETE"],
        check=False,
    )

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="merge",
        event_type="branch_merged_direct",
        payload={"issue_number": issue_number, "branch": branch},
        log_dir=config.log_dir.expanduser(),
    )
    return True


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
    issue_number: int,
    worktree_path: str = "",
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
        issue_number:  Original issue number (for logging).
        worktree_path: Absolute path to the worktree directory (for rebase).
                       If empty, rebase on conflict is skipped and the PR is
                       escalated to human review instead.
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
    """Dispatch a fix agent to address review comments on a PR.

    Triaging policy (applied by the fix agent):
    - Straightforward in-scope fixes → apply and push
    - Valid but out-of-scope → file a follow-up issue
    - False positives → add a skip comment on the PR

    Args:
        pr_number:    GitHub PR number.
        branch:       Branch name for the PR.
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

    # Format review comments for the fix agent
    comments_text = ""
    for c in comments[:20]:  # cap to avoid huge prompts
        path = c.get("path") or "?"
        line = c.get("line") or c.get("original_line") or "?"
        body = c.get("body") or ""
        comments_text += f"- File: `{path}`, line {line}\n  {body}\n\n"

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="review",
        event_type="review_comments_triaged",
        payload={
            "pr_number": pr_number,
            "issue_number": issue_number,
            "comment_count": len(comments),
            "action": "dispatching_fix_agent",
        },
        log_dir=config.log_dir.expanduser(),
    )

    prompt = (
        f"You are addressing review comments on PR #{pr_number} in repository `{repo}`.\n"
        f"Branch: `{branch}`\n\n"
        f"Review comments to triage:\n\n"
        f"{comments_text}\n"
        f"Steps:\n"
        f"1. Clone the repo into a temp directory:\n"
        f"   WORK=$(mktemp -d) && gh repo clone {repo} $WORK\n"
        f"2. cd $WORK && git checkout {branch} && git pull origin {branch}\n"
        f"3. For each review comment, choose one action:\n"
        f"   a. Fix it — make the change in the file, then commit and push\n"
        f"   b. Out of scope — `gh issue create --repo {repo} --title '...' --body '...'`\n"
        f"   c. False positive — `gh pr comment {pr_number} --repo {repo}"
        f" --body 'Skipping: <reason>'`\n"
        f"4. If you made changes:\n"
        f"   git add -A && git commit -m 'fix: address review on PR #{pr_number}' && git push\n"
        f"5. STOP.\n"
    )

    env = build_subprocess_env(config)
    runner.run(
        prompt=prompt,
        allowed_tools=["Bash"],
        env=env,
        max_turns=30,
        timeout_seconds=config.agent_timeout_minutes * 60,
        model=config.model,
        prefix=f"[fix-review #{pr_number}] ",
    )


def _create_worktree(
    branch: str, repo_root: str, default_branch: str = "main"
) -> str | None:
    """Create a git worktree for *branch* under ``.claude/worktrees/``.

    The branch is created from ``origin/<default_branch>`` and pushed to the remote.

    Args:
        branch:         Branch name (e.g. ``"42-add-config"``).
        repo_root:      Absolute path to the repository root.
        default_branch: Name of the default branch to base the new branch on.

    Returns:
        Absolute path to the new worktree directory, or ``None`` on failure.
    """
    worktree_dir = os.path.join(repo_root, ".claude", "worktrees", branch)

    # Create worktree with new branch based on origin/<default_branch>
    result = subprocess.run(
        ["git", "worktree", "add", worktree_dir, "-b", branch, f"origin/{default_branch}"],
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
) -> runner.RunResult:
    """Dispatch a single design agent (HLD or LLD) in its worktree.

    Builds the design-agent prompt, calls runner.run(), and returns the RunResult.
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
        RunResult from runner.run().
    """
    issue_number = issue["number"]
    today = date.today().isoformat()

    if module_name:
        prompt = (
            f"## Session Parameters\n"
            f"- Repository: {repo}\n"
            f"- Milestone: {milestone}\n"
            f"- Module: {module_name}\n"
            f"- Issue: #{issue_number}\n"
            f"- Branch: {branch}\n"
            f"- Session Date: {today}\n\n"
            f"You are the design-worker LLD agent for module `{module_name}` in `{repo}`.\n"
            f"Write the Low-Level Design document for this module following the skill "
            f"instructions in your system prompt."
        )
        prefix = f"[design-lld/{module_name}] "
    else:
        prompt = (
            f"## Session Parameters\n"
            f"- Repository: {repo}\n"
            f"- Milestone: {milestone}\n"
            f"- Issue: #{issue_number}\n"
            f"- Branch: {branch}\n"
            f"- Session Date: {today}\n\n"
            f"You are the design-worker HLD agent for `{repo}`.\n"
            f"Write the High-Level Design document for milestone `{milestone}` "
            f"following the skill instructions in your system prompt."
        )
        prefix = f"[design-hld #{issue_number}] "

    skill_tmp = write_skill_tmp(skill_name)
    env = build_subprocess_env(config)
    env["CLAUDE_CONFIG_DIR"] = f"/tmp/brimstone-agent-{issue_number}-{uuid.uuid4().hex}"

    try:
        result = runner.run(
            prompt=prompt,
            allowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
            append_system_prompt_file=skill_tmp,
            env=env,
            max_turns=200,
            timeout_seconds=config.agent_timeout_minutes * 60,
            model=config.model,
            prefix=prefix,
        )
    finally:
        skill_tmp.unlink(missing_ok=True)

    return result


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
        scope = "src/brimstone/"
    elif module == "cli":
        scope = "src/brimstone/cli.py, src/brimstone/skills/"
    else:
        scope = f"src/brimstone/{module}.py"

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
    skill_tmp = write_skill_tmp("impl-worker")

    if dry_run:
        skill_tmp.unlink(missing_ok=True)
        from brimstone.runner import RunResult as _RunResult

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
    env["CLAUDE_CONFIG_DIR"] = f"/tmp/brimstone-agent-{issue_number}-{uuid.uuid4().hex}"

    max_turns = 100
    if hasattr(config, "max_turns") and config.max_turns:
        max_turns = config.max_turns

    try:
        result = runner.run(
            prompt=base_prompt,
            allowed_tools=runner.TOOLS_IMPL_AGENT,
            append_system_prompt_file=skill_tmp,
            env=env,
            max_turns=max_turns,
            timeout_seconds=config.agent_timeout_minutes * 60,
            model=config.model,
            prefix=f"[impl-worker #{issue_number}] ",
        )
    finally:
        skill_tmp.unlink(missing_ok=True)
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
    milestone: str,
    config: Config,
    checkpoint: Checkpoint,
    dry_run: bool = False,
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
    # Gate 1: All research must be closed before design begins            #
    # ------------------------------------------------------------------ #
    if not dry_run:
        open_research = _count_all_open_research_issues(repo, milestone)
        if open_research > 0:
            click.echo(
                f"Error: {open_research} research issue(s) still open for milestone "
                f"'{milestone}'. All research must complete before design can begin.",
                err=True,
            )
            raise SystemExit(1)

        default_branch = _get_default_branch_for_repo(repo)
        repo_root = _get_repo_root()

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
        design_issues = _list_open_design_issues(repo, milestone)
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
            design_issues = _list_open_design_issues(repo, milestone)
            hld_issue = next((i for i in design_issues if i["title"] == hld_issue_title), None)

        if hld_issue is None:
            click.echo("Error: Could not find or create HLD design issue.", err=True)
            raise SystemExit(1)

        hld_number = hld_issue["number"]
        hld_branch = f"{hld_number}-{_slugify(hld_issue_title)}"

        _claim_issue(repo=repo, issue_number=hld_number)
        worktree_path = _create_worktree(hld_branch, repo_root, default_branch)
        if worktree_path is None:
            _unclaim_issue(repo=repo, issue_number=hld_number)
            click.echo(f"Error: Failed to create worktree for branch {hld_branch!r}.", err=True)
            raise SystemExit(1)

        logger.log_conductor_event(
            run_id=checkpoint.run_id,
            phase="dispatch",
            event_type="design_hld_dispatch",
            payload={"issue_number": hld_number, "branch": hld_branch},
            log_dir=config.log_dir.expanduser(),
        )

        hld_result = _dispatch_design_agent(
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

        if hld_result.is_error:
            _unclaim_issue(repo=repo, issue_number=hld_number)
            _remove_worktree(worktree_path, repo_root)
            click.echo(
                f"HLD agent failed: {hld_result.subtype} / {hld_result.error_code}",
                err=True,
            )
            raise SystemExit(1)

        pr_number = _find_pr_for_branch(repo, hld_branch)
        if pr_number is None:
            _unclaim_issue(repo=repo, issue_number=hld_number)
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
        )
        _remove_worktree(worktree_path, repo_root)
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
    all_design_issues = _list_open_design_issues(repo, milestone)
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

        # Claim issues and create worktrees sequentially, then dispatch in parallel
        dispatch_batch: list[tuple[str, dict[str, Any], str, str]] = []
        for module, issue in pending:
            issue_number = issue["number"]
            branch = f"{issue_number}-{_slugify(issue['title'])}"
            _claim_issue(repo=repo, issue_number=issue_number)
            worktree_path = _create_worktree(branch, repo_root, default_branch)
            if worktree_path is None:
                _unclaim_issue(repo=repo, issue_number=issue_number)
                click.echo(
                    f"Warning: Failed to create worktree for {branch!r} — skipping LLD "
                    f"for {module!r}",
                    err=True,
                )
                continue
            dispatch_batch.append((module, issue, branch, worktree_path))

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(dispatch_batch)) as executor:
            futures: dict = {
                executor.submit(
                    _dispatch_design_agent,
                    issue=issue,
                    branch=branch,
                    worktree_path=wt,
                    skill_name="design-worker-lld",
                    module_name=module,
                    repo=repo,
                    milestone=milestone,
                    config=config,
                    checkpoint=checkpoint,
                ): (module, issue, branch, wt)
                for module, issue, branch, wt in dispatch_batch
            }

            for future in concurrent.futures.as_completed(futures):
                module, issue, branch, wt = futures[future]
                issue_number = issue["number"]
                try:
                    result = future.result()
                except Exception as exc:
                    click.echo(f"LLD agent for {module!r} raised exception: {exc}", err=True)
                    _unclaim_issue(repo=repo, issue_number=issue_number)
                    _remove_worktree(wt, repo_root)
                    continue

                if result.is_error:
                    click.echo(f"LLD agent for {module!r} failed: {result.subtype}", err=True)
                    _unclaim_issue(repo=repo, issue_number=issue_number)
                    _remove_worktree(wt, repo_root)
                    continue

                pr_number = _find_pr_for_branch(repo, branch)
                if pr_number is None:
                    click.echo(f"LLD agent for {module!r} succeeded but no PR found.", err=True)
                    _unclaim_issue(repo=repo, issue_number=issue_number)
                    _remove_worktree(wt, repo_root)
                    continue

                merged = _monitor_pr(
                    pr_number=pr_number,
                    branch=branch,
                    repo=repo,
                    config=config,
                    checkpoint=checkpoint,
                    worktree_path=wt,
                    issue_number=issue_number,
                )
                _remove_worktree(wt, repo_root)
                if merged:
                    click.echo(f"LLD for {module!r} merged (PR #{pr_number}).")
                else:
                    click.echo(
                        f"Warning: LLD PR #{pr_number} for {module!r} did not merge. "
                        "Manual intervention required.",
                        err=True,
                    )

    # ------------------------------------------------------------------ #
    # Complete: file plan-issues pipeline issue                           #
    # ------------------------------------------------------------------ #
    _file_pipeline_issue(repo=repo, next_worker="plan-issues", milestone=milestone)
    session.save(checkpoint, checkpoint_path)

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="complete",
        event_type="design_worker_complete",
        payload={"repo": repo, "milestone": milestone},
        log_dir=config.log_dir.expanduser(),
    )
    click.echo(f"Design-worker complete for milestone '{milestone}'.")


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

    base_prompt = (
        f"## Session Parameters\n"
        f"- Repository: {repo}\n"
        f"- Milestone: {milestone}\n"
        f"- Session Date: {today}\n"
        f"- Dry Run: {dry_run}\n\n"
        f"You are the plan-issues orchestrator for the `{repo}` repository.\n"
        f"Read the HLD and LLD design docs and file fully-specified `stage/impl` issues "
        f"against milestone `{milestone}` following the skill instructions.\n"
        + (
            "\nThe `--dry-run` flag is set: print all planned issues but do NOT call "
            "`gh issue create`.\n"
            if dry_run
            else ""
        )
    )
    skill_tmp = write_skill_tmp("plan-issues")

    if dry_run:
        skill_tmp.unlink(missing_ok=True)
        click.echo(
            f"[dry-run] Would dispatch plan-issues agent for milestone "
            f"{milestone!r} in repo {repo!r}"
        )
        return

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="plan_issues_start",
        payload={"repo": repo, "milestone": milestone},
        log_dir=config.log_dir.expanduser(),
    )

    env = build_subprocess_env(config)
    try:
        result = runner.run(
            prompt=base_prompt,
            allowed_tools=["Bash", "Read", "Glob", "Grep"],
            append_system_prompt_file=skill_tmp,
            env=env,
            max_turns=200,
            timeout_seconds=config.agent_timeout_minutes * 60,
            model=config.model,
            prefix="[plan-issues] ",
        )
    finally:
        skill_tmp.unlink(missing_ok=True)

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
        click.echo(
            f"Plan-issues completed with error: {result.subtype} / {result.error_code}",
            err=True,
        )
    else:
        click.echo(f"Plan-issues complete for milestone '{milestone}'.")


# ---------------------------------------------------------------------------
# Plan-milestones helpers
# ---------------------------------------------------------------------------


def _validate_spec_path(spec: str) -> Path:
    """Resolve and validate the ``--spec`` argument.

    Accepts a relative path (resolved from cwd) or an absolute path.  Fails
    with a :exc:`click.ClickException` if the path does not exist or does not
    end in ``.md``.

    Args:
        spec: Raw value of the ``--spec`` CLI option.

    Returns:
        Resolved absolute :class:`~pathlib.Path`.

    Raises:
        click.ClickException: If the path does not exist or is not a ``.md`` file.
    """
    resolved = Path(spec).expanduser().resolve()
    if not resolved.exists():
        raise click.ClickException(f"Spec file not found: {resolved}")
    if resolved.suffix.lower() != ".md":
        raise click.ClickException(f"Spec file must be a .md file, got: {resolved.name!r}")
    return resolved


def _seed_spec(
    spec_path: Path,
    version: str,
    local_path: str,
) -> None:
    """Copy *spec_path* into the target repo at ``docs/specs/<version>.md``.

    If the destination already exists, print a warning and skip the copy.
    Otherwise, create the directory, copy the file, ``git add``, and
    ``git commit`` with a deterministic message.

    Args:
        spec_path:  Resolved absolute path to the source spec file.
        version:    Version name used to build the destination filename.
        local_path: Absolute path to the root of the target git repository.

    Side effects:
        May create ``docs/specs/`` inside *local_path*, copy the spec file,
        and create a git commit.
    """
    dest_dir = Path(local_path) / "docs" / "specs"
    dest_file = dest_dir / f"{version}.md"

    if dest_file.exists():
        click.echo(f"Warning: {dest_file} already exists in target repo. Skipping spec copy.")
        return

    dest_dir.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy2(spec_path, dest_file)

    subprocess.run(
        ["git", "-C", local_path, "add", str(dest_file)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            local_path,
            "commit",
            "-m",
            f"docs: seed spec from {spec_path}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    click.echo(f"Seeded spec into {dest_file} and committed.")


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

    Reads ``GITHUB_TOKEN`` from the environment (loaded from ``.env`` by
    pydantic-settings) and calls the GitHub Invitations API as the bot user.
    Non-fatal: prints a warning if the token is absent or the call fails.

    Args:
        repo: GitHub repository in ``owner/repo`` format.
    """
    token = (
        os.environ.get("BRIMSTONE_GH_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    )
    if not token:
        click.echo(
            f"Warning: no GitHub token found (BRIMSTONE_GH_TOKEN / GH_TOKEN / GITHUB_TOKEN); "
            f"cannot auto-accept invitation for {repo}",
            err=True,
        )
        return

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
        click.echo(f"Warning: could not list invitations for {_BRIMSTONE_BOT}", err=True)
        return

    try:
        invitations = json.loads(list_result.stdout)
    except json.JSONDecodeError:
        return

    invitation_id: int | None = None
    for inv in invitations:
        if inv.get("repository", {}).get("full_name", "") == repo:
            invitation_id = inv["id"]
            break

    if invitation_id is None:
        return  # No pending invitation — already accepted or not yet sent

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
    if http_code == "204":
        click.echo(f"{_BRIMSTONE_BOT} accepted invitation to {repo}")
    else:
        click.echo(
            f"Warning: invitation accept returned HTTP {http_code} for {repo}",
            err=True,
        )


def _add_brimstone_bot_collaborator(repo: str) -> None:
    """Add the brimstone service account as a collaborator on *repo* and auto-accept.

    Uses the GitHub Collaborators API to grant push access to
    ``yeast-bot``, then immediately accepts the invitation using the bot's
    token from ``GITHUB_TOKEN`` in the environment.  Non-fatal so init can
    proceed even if permissions are missing.

    Args:
        repo: GitHub repository in ``owner/repo`` format.
    """
    endpoint = f"repos/{repo}/collaborators/{_BRIMSTONE_BOT}"
    result = _gh(["api", endpoint, "-X", "PUT", "-f", "permission=push"], check=False)
    if result.returncode == 0:
        click.echo(f"Added {_BRIMSTONE_BOT} as a collaborator on {repo}")
        _accept_brimstone_bot_invitation(repo)
    else:
        click.echo(
            f"Warning: could not add {_BRIMSTONE_BOT} to {repo} "
            f"(HTTP status may indicate it already exists or lacks permission): "
            f"{result.stderr.strip()}",
            err=True,
        )


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


def _setup_ci(repo: str, config: "Config", dry_run: bool) -> None:
    """Push a default CI workflow and inject ANTHROPIC_API_KEY as a GitHub Actions secret.

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
        click.echo(f"[dry-run] would push {workflow_path} to {repo}")
        click.echo(f"[dry-run] would set ANTHROPIC_API_KEY secret on {repo}")
        return

    # Check if workflow already exists
    check = _gh(["api", f"repos/{repo}/contents/{workflow_path}"], check=False)
    if check.returncode == 0:
        click.echo(f"CI workflow already exists in {repo}, skipping upload")
    else:
        encoded = base64.b64encode(workflow_content.encode()).decode()
        payload: dict = {"message": "ci: add default CI workflow", "content": encoded}
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/contents/{workflow_path}", "-X", "PUT", "--input", "-"],
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
        ["gh", "secret", "set", "ANTHROPIC_API_KEY", "--repo", repo, "--body", config.anthropic_api_key],
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


def _run_initialize(
    repo_ref: str,
    local_path: str | None,
    version: str,
    spec: str | None,
    dry_run: bool = False,
) -> None:
    """Set up the target repository and optionally seed the spec file.

    This is the first pipeline stage.  It is purely a Python operation —
    no Claude agent is dispatched.

    - If ``local_path`` is None (remote-only repo), spec seeding is skipped
      with a warning.
    - If ``spec`` is provided and ``local_path`` is available, copies the spec
      to ``docs/specs/<version>.md`` and commits it (idempotent).
    - If ``spec`` is None and no spec file exists locally, prints a reminder
      but does not fail — the operator must seed the spec before plan-milestones
      can succeed.

    Args:
        repo_ref:   Resolved ``owner/name`` repository reference.
        local_path: Absolute path to the local repo clone (or None for remote-only).
        version:    Version identifier (e.g. ``"MVP"``).
        spec:       Absolute path to a spec file to seed (or None).
        dry_run:    If True, print intent without executing.
    """
    if dry_run:
        click.echo(f"[dry-run] initialize: repo={repo_ref!r}, version={version!r}, spec={spec!r}")
        return

    if spec is not None:
        if local_path is None:
            click.echo(
                "Warning: --spec provided but no local repo path is available "
                "(remote-only repos require a local checkout). Spec seeding skipped."
            )
        else:
            _seed_spec(Path(spec), version, local_path)
    elif local_path is not None:
        spec_file = Path(local_path) / "docs" / "specs" / f"{version}.md"
        if not spec_file.exists():
            click.echo(
                f"Warning: No spec found at {spec_file}. "
                "plan-milestones will fail until a spec is committed. "
                "Pass --spec <path> or commit the spec manually."
            )

    if not dry_run:
        _ensure_labels(repo_ref)

    click.echo(f"initialize: repo={repo_ref!r} version={version!r} ready.")


# ---------------------------------------------------------------------------
# Required labels — created during init so every downstream stage can rely on them
# ---------------------------------------------------------------------------

_REQUIRED_LABELS: list[tuple[str, str, str]] = [
    # (name, color, description)
    ("stage/research", "0075ca", "Research and investigation task"),
    ("stage/design",   "e4e669", "Design task (HLD, LLD)"),
    ("stage/impl",     "d93f0b", "Implementation task (code + tests)"),
    ("P0",             "b60205", "Release blocker"),
    ("P1",             "e11d48", "High priority"),
    ("P2",             "0e8a16", "Normal priority (default)"),
    ("P3",             "5319e7", "Low priority"),
    ("P4",             "cfd3d7", "Backlog"),
    ("in-progress",    "fbca04", "Currently being worked on"),
    ("triage",         "fef2c0", "Pending triage decision"),
    ("wont-research",  "eeeeee", "Closed by triage — below threshold"),
    ("pipeline",       "006b75", "Pipeline stage transition tracking"),
    ("bug",            "ee0701", "Defect filed by QA or reported post-release"),
]


def _ensure_labels(repo: str) -> None:
    """Create any missing required labels in *repo*.

    Uses ``gh label create`` with ``--force`` to create-or-update each label.
    """
    for name, color, description in _REQUIRED_LABELS:
        result = _gh(
            [
                "label", "create", name,
                "--color", color,
                "--description", description,
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


def _report_plan_milestones_output(repo: str, milestone: str) -> None:
    """Print the milestone and research issues that plan-milestones created."""
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


def _run_plan_milestones(
    repo: str,
    version: str,
    config: Config,
    checkpoint: Checkpoint,
    local_path: str | None = None,
    dry_run: bool = False,
    spec_stem: str | None = None,
) -> None:
    """Dispatch a single plan-milestones agent to create a milestone pair and seed issues.

    Builds a prompt by injecting the ``plan-milestones`` skill file and
    invoking ``runner.run()`` once. The agent reads the spec, creates
    milestones, and files seed research issues.

    Args:
        repo:       GitHub repository in ``owner/repo`` format.
        version:    Milestone name (e.g. ``"v0.1.0-cold-start"``).
        config:     Validated Config instance.
        checkpoint: Active Checkpoint instance.
        local_path: Absolute path to the local repo checkout (or None for remote-only).
        dry_run:    If True, print the prompt length without executing.
        spec_stem:  Filename stem of the spec file (e.g. ``"v0.1.x-cold-start"``).
                    When None, falls back to ``version``.
    """
    if spec_stem is None:
        spec_stem = version
    checkpoint_path = config.checkpoint_dir.expanduser() / "current.json"
    today = date.today().isoformat()

    if local_path is not None:
        spec_read_instruction = (
            f"Read the spec from the local checkout:\n  cat {local_path}/docs/specs/{spec_stem}.md"
        )
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
        f"You are the plan-milestones orchestrator for the `{repo}` repository.\n"
        f"You are running in a headless temp directory (not inside the repo checkout).\n"
        f"{spec_read_instruction}\n"
        f"Then create the milestone and file research issues"
        f" following the skill instructions in your system prompt."
        f"{dry_run_instruction}"
    )

    skill_tmp = write_skill_tmp("plan-milestones")

    if dry_run:
        click.echo(
            f"[dry-run] Dispatching plan-milestones agent for version "
            f"{version!r} in repo {repo!r} (read-only — no GitHub writes)\n"
        )

    logger.log_conductor_event(
        run_id=checkpoint.run_id,
        phase="dispatch",
        event_type="plan_milestones_start",
        payload={"repo": repo, "version": version},
        log_dir=config.log_dir.expanduser(),
    )

    env = build_subprocess_env(config)

    try:
        result = runner.run(
            prompt=base_prompt,
            allowed_tools=["Bash"],
            append_system_prompt_file=skill_tmp,
            env=env,
            max_turns=200,
            timeout_seconds=config.agent_timeout_minutes * 60,
            model=config.model,
            prefix="[plan-milestones] ",
        )
    finally:
        skill_tmp.unlink(missing_ok=True)

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
            f"Plan-milestones completed with error: {result.subtype} / {result.error_code}",
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
            click.echo(f"Plan-milestones complete for version '{version}'.")

    if dry_run:
        return

    # Post-validation: confirm the milestone was actually created.
    if not _milestone_exists(repo, version):
        click.echo(
            f"Error: plan-milestones completed but milestone '{version}' was not created.\n"
            "The agent likely exited before finishing. Re-run to try again.",
            err=True,
        )
        raise SystemExit(1)

    # Report what was created.
    _report_plan_milestones_output(repo=repo, milestone=version)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


@click.group()
def composer() -> None:
    """Composer orchestrator — run pipeline workers and admin commands."""


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
        open_count = _count_all_open_research_issues(repo, milestone)
        if open_count > 0:
            raise click.ClickException(
                f"{open_count} open research issue(s) remain for milestone '{milestone}'. "
                "Run `brimstone run --research` first, or use `--all` to run all stages."
            )

    if stage == "impl" and "design" not in stages_being_run:
        hld_path = "docs/design/HLD.md"
        if not _doc_exists_on_default_branch(repo, hld_path, default_branch):
            raise click.ClickException(
                f"Design doc '{hld_path}' does not exist on branch '{default_branch}'. "
                "Run `brimstone run --design` first, or use `--all` to run all stages."
            )


@composer.command("run")
@click.option(
    "--repo",
    default=None,
    help=(
        "Target repository. Accepts: 'owner/name' (existing remote), "
        "'name' (new private repo to scaffold), 'path/to/local/dir' (local git repo), "
        "or omit to operate on the current working directory."
    ),
)
@click.option("--research", "do_research", is_flag=True, help="Run the research stage")
@click.option("--design", "do_design", is_flag=True, help="Run the design stage")
@click.option("--impl", "do_impl", is_flag=True, help="Run the implementation stage")
@click.option("--all", "do_all", is_flag=True, help="Run all pipeline stages in order")
@click.option("--milestone", required=True, help="Milestone name to operate on")
@click.option("--model", default=None, help="Override Claude model")
@click.option("--max-budget", type=float, default=None, help="USD budget cap")
@click.option("--dry-run", is_flag=True, help="Print what each stage would do without executing")
def run(
    repo: str | None,
    do_research: bool,
    do_design: bool,
    do_impl: bool,
    do_all: bool,
    milestone: str,
    model: str | None,
    max_budget: float | None,
    dry_run: bool,
) -> None:
    """Run one or more pipeline stages for a milestone.

    Stages execute in pipeline order: research → design → impl.
    Prerequisites are checked before each stage unless the prerequisite is
    also being run in the same invocation.

    Examples:

      brimstone run --research --milestone "MVP Research"

      brimstone run --design --impl --milestone "MVP Research"

      brimstone run --all --milestone "MVP Research" --dry-run
    """
    # This is the top-level orchestrator; stage calls are direct Python
    # invocations, not nested Claude Code sessions.
    os.environ.pop("CLAUDECODE", None)

    # -----------------------------------------------------------------------
    # Determine ordered stages to run
    # -----------------------------------------------------------------------
    if do_all:
        stages: list[str] = ["research", "design", "impl"]
    else:
        stages = [
            s
            for s, flag in [
                ("research", do_research),
                ("design", do_design),
                ("impl", do_impl),
            ]
            if flag
        ]

    if not stages:
        raise click.UsageError("Specify at least one stage: --research, --design, --impl, or --all")

    # -----------------------------------------------------------------------
    # Resolve repo and build base config
    # -----------------------------------------------------------------------
    repo_ref, local_path = _resolve_repo(repo)
    if local_path is not None:
        os.chdir(local_path)
    elif repo_ref:
        # Remote owner/name repo: clone into a temp dir so workers can create
        # git worktrees. _get_repo_root() inside each worker will then return
        # the clone root rather than brimstone's own source tree.
        tmp_clone = tempfile.mkdtemp(prefix=f"brimstone-repo-")
        click.echo(f"[run] Cloning {repo_ref} into {tmp_clone}…", err=True)
        clone_result = subprocess.run(
            ["gh", "repo", "clone", repo_ref, tmp_clone],
            capture_output=True,
            text=True,
        )
        if clone_result.returncode != 0:
            raise click.ClickException(
                f"Failed to clone {repo_ref}:\n{clone_result.stderr}"
            )
        local_path = tmp_clone
        os.chdir(local_path)

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

    # Propagate GitHub token to os.environ so _gh() calls use yeast-bot's token
    # before startup_sequence() is invoked (e.g. for _milestone_exists check).
    if config.github_token:
        os.environ["GH_TOKEN"] = config.github_token

    # -----------------------------------------------------------------------
    # Non-init repo check: milestone must exist before any stage can run
    # -----------------------------------------------------------------------
    if not dry_run:
        if not _milestone_exists(repo_ref, milestone):
            raise click.ClickException(
                f"Milestone '{milestone}' not found on {repo_ref}. "
                "Run `brimstone init --repo <repo> --spec <path>` to create it."
            )

    # Resolve default branch once for gate checks
    default_branch = _get_default_branch_for_repo(repo_ref) if repo_ref and not dry_run else "main"

    # -----------------------------------------------------------------------
    # Execute stages in pipeline order
    # -----------------------------------------------------------------------
    for stage in stages:
        click.echo(f"\n── Stage: {stage} ──", err=True)

        # Completion check — skip if stage is already done
        if not dry_run:
            if stage == "research" and _count_all_open_research_issues(repo_ref, milestone) == 0:
                click.echo(f"[run] {stage}: already complete, skipping", err=True)
                continue
            if stage == "design" and _doc_exists_on_default_branch(
                repo_ref, "docs/design/HLD.md", default_branch
            ):
                click.echo(f"[run] {stage}: already complete, skipping", err=True)
                continue

        # Gate check — only when prerequisite is not also in this run
        if not dry_run:
            _check_gate_before_stage(stage, stages, repo_ref, milestone, default_branch)

        if dry_run:
            click.echo(f"[dry-run] would run {stage} for milestone={milestone!r}", err=True)
            continue

        _config, _checkpoint = startup_sequence(
            config=config,
            checkpoint_path=checkpoint_path,
            milestone=milestone,
            stage=stage,
        )

        if stage == "research":
            _run_research_worker(
                repo=repo_ref,
                milestone=milestone,
                config=_config,
                checkpoint=_checkpoint,
                dry_run=False,
            )
        elif stage == "design":
            _run_design_worker(
                repo=repo_ref,
                milestone=milestone,
                config=_config,
                checkpoint=_checkpoint,
                dry_run=False,
            )
        elif stage == "impl":
            # Auto-run plan-issues first if no open impl issues exist yet
            open_impl = _list_open_impl_issues(repo_ref, milestone)
            if not open_impl:
                click.echo(
                    f"[run] No open impl issues found for '{milestone}'; "
                    "running plan-issues first...",
                    err=True,
                )
                _run_plan_issues(
                    repo=repo_ref,
                    milestone=milestone,
                    config=_config,
                    checkpoint=_checkpoint,
                    dry_run=False,
                )
            _run_impl_worker(
                repo=repo_ref,
                milestone=milestone,
                config=_config,
                checkpoint=_checkpoint,
                dry_run=False,
            )


@composer.command("init")
@click.option(
    "--repo",
    required=True,
    help="Target repository in 'owner/name' format.",
)
@click.option(
    "--spec",
    required=True,
    type=click.Path(dir_okay=False),
    help="Path to the .md spec file to seed into the target repo.",
)
@click.option(
    "--milestone",
    required=True,
    help="Milestone name to create on GitHub (e.g. 'v0.1.0-cold-start').",
)
@click.option("--model", default=None, help="Override Claude model")
@click.option("--dry-run", is_flag=True, help="Print what would happen without executing")
def init(
    repo: str,
    spec: str,
    milestone: str,
    model: str | None,
    dry_run: bool,
) -> None:
    """Upload a spec and seed the milestone + research issues.

    Equivalent to: upload spec → run plan-milestones.

    After this command completes, use:
      brimstone run --research --milestone <milestone>
    """
    resolved_spec = _validate_spec_path(spec)
    spec_stem = resolved_spec.stem  # e.g. "v0.1.x-cold-start" — used for the file path
    # milestone is the explicit GitHub milestone name, e.g. "v0.1.0-cold-start"

    repo_ref, local_path = _resolve_repo(repo)
    overrides: dict = {"github_repo": repo_ref or None, "target_repo": repo_ref or None}
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
        }
    )
    _config, _checkpoint = startup_sequence(
        config=config,
        checkpoint_path=checkpoint_path,
        milestone=milestone,
        stage="plan-milestones",
        skip_checks=_HEADLESS_SKIP,
    )

    if dry_run:
        click.echo(f"[dry-run] would upload {resolved_spec} to {repo_ref}/docs/specs/{spec_stem}.md")
        click.echo(f"[dry-run] would add {_BRIMSTONE_BOT} as collaborator on {repo_ref}")
        _setup_ci(repo_ref, _config, dry_run=True)
    else:
        _add_brimstone_bot_collaborator(repo_ref)
        _upload_spec_to_repo(repo_ref, resolved_spec, spec_stem)
        _setup_ci(repo_ref, _config, dry_run=False)

    _run_plan_milestones(
        repo=repo_ref,
        version=milestone,
        config=_config,
        checkpoint=_checkpoint,
        local_path=local_path,
        dry_run=dry_run,
        spec_stem=spec_stem,
    )


@composer.command("adopt")
@click.option("--source-repo", required=True, help="Source repository to adopt from.")
@click.option("--target-repo", default=None, help="Target repository (defaults to source).")
def adopt(source_repo: str, target_repo: str | None) -> None:
    """Adopt an existing repository into the brimstone pipeline. (Not yet implemented.)"""
    click.echo("adopt: not yet implemented", err=True)
    raise SystemExit(1)
