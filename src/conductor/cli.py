"""CLI entry points for breadmin-conductor.

Entry points:
  issue-worker     → issue_worker()
  research-worker  → research_worker()
  conductor        → conductor()
"""

from __future__ import annotations

import click


@click.command("issue-worker")
@click.option("--repo", required=True, help="Target repo in OWNER/REPO format")
@click.option("--model", default=None, help="Override Claude model")
@click.option("--max-budget", type=float, default=None, help="USD budget cap")
@click.option("--max-turns", type=int, default=None, help="Max turns per invocation")
@click.option("--dry-run", is_flag=True, help="Print invocation without executing")
@click.option("--resume", default=None, help="Resume a previous session by ID")
def issue_worker(
    repo: str,
    model: str | None,
    max_budget: float | None,
    max_turns: int | None,
    dry_run: bool,
    resume: str | None,
) -> None:
    """Process GitHub issues headlessly via claude -p."""
    # TODO (issue #I6): wire up runner, config, health checks
    raise NotImplementedError("Not yet implemented — see issue #I6")


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
    # TODO (issue #I6): wire up runner, config, health checks
    raise NotImplementedError("Not yet implemented — see issue #I6")


@click.group()
def conductor() -> None:
    """Conductor admin commands."""


@conductor.command("health")
@click.option("--repo", default=None, help="Repo to check (OWNER/REPO)")
def health(repo: str | None) -> None:
    """Run preflight checks."""
    # TODO (issue #I4): implement health checks
    raise NotImplementedError("Not yet implemented — see issue #I4")


@conductor.command("cost")
def cost() -> None:
    """Show cost ledger summary."""
    # TODO (issue #I5): implement cost ledger reader
    raise NotImplementedError("Not yet implemented — see issue #I5")
