"""CLI entry points for breadmin-conductor.

Entry points:
  impl-worker      → impl_worker()
  research-worker  → research_worker()
  design-worker    → design_worker()
  conductor        → conductor()
"""

from __future__ import annotations

import click


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
    # TODO: wire up runner, config, health checks
    raise NotImplementedError("Not yet implemented")


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
    # TODO: wire up runner, config, health checks
    raise NotImplementedError("Not yet implemented")


@click.command("design-worker")
@click.option("--repo", required=True, help="Target repo in OWNER/REPO format")
@click.option("--research-milestone", required=True, help="Completed research milestone to translate")
@click.option("--model", default=None, help="Override Claude model")
@click.option("--dry-run", is_flag=True, help="Print planned issues without creating them")
def design_worker(
    repo: str,
    research_milestone: str,
    model: str | None,
    dry_run: bool,
) -> None:
    """Translate research docs into scoped implementation issues."""
    # TODO: wire up runner, config, health checks
    raise NotImplementedError("Not yet implemented")


@click.group()
def conductor() -> None:
    """Conductor admin commands."""


@conductor.command("health")
@click.option("--repo", default=None, help="Repo to check (OWNER/REPO)")
def health(repo: str | None) -> None:
    """Run preflight checks."""
    # TODO: implement health checks
    raise NotImplementedError("Not yet implemented")


@conductor.command("cost")
def cost() -> None:
    """Show cost ledger summary."""
    # TODO: implement cost ledger reader
    raise NotImplementedError("Not yet implemented")
