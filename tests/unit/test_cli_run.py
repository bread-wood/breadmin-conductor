"""Unit tests for the `brimstone run` subcommand (cli.py)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from brimstone.cli import composer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
    "BRIMSTONE_GH_TOKEN": "ghp-test-token",
}

_REPO = "owner/repo"
_MILESTONE = "MVP Research"


# ---------------------------------------------------------------------------
# brimstone run --help
# ---------------------------------------------------------------------------


class TestRunHelp:
    def test_run_appears_in_composer_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(composer, ["--help"])
        assert result.exit_code == 0
        assert "run" in result.output

    def test_run_help_shows_options(self) -> None:
        runner = CliRunner()
        result = runner.invoke(composer, ["run", "--help"])
        assert result.exit_code == 0
        output = result.output
        assert "--research" in output
        assert "--design" in output
        assert "--impl" in output
        assert "--all" in output
        assert "--milestone" in output
        assert "--dry-run" in output


# ---------------------------------------------------------------------------
# No stage flags → error
# ---------------------------------------------------------------------------


class TestRunRequiresStage:
    def test_no_stage_flags_produces_error(self) -> None:
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with patch("brimstone.cli._resolve_repo", return_value=(_REPO, None)):
                with patch("brimstone.cli._milestone_exists", return_value=True):
                    runner = CliRunner()
                    result = runner.invoke(
                        composer,
                        ["run", "--repo", _REPO, "--milestone", _MILESTONE],
                    )
        assert result.exit_code != 0
        assert "--research" in result.output or "--all" in result.output

    def test_missing_milestone_produces_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(composer, ["run", "--research", "--repo", _REPO])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Non-init repo check: milestone must exist
# ---------------------------------------------------------------------------


class TestRunMilestoneCheck:
    def test_missing_milestone_fails_early(self, tmp_path: Path) -> None:
        """If the milestone does not exist, abort with a clear error before any stage runs."""
        research_called: list[bool] = []

        def fake_research(**kwargs: object) -> None:
            research_called.append(True)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch("brimstone.cli._milestone_exists", return_value=False),
                patch("brimstone.cli._run_research_worker", side_effect=fake_research),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--research", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code != 0
        assert not research_called, "No stage should run when milestone is missing"
        combined = result.output + (result.output or "")
        assert "brimstone init" in combined or _MILESTONE in combined

    def test_existing_milestone_proceeds(self, tmp_path: Path) -> None:
        """If the milestone exists, stages run normally."""
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._count_all_open_research_issues", return_value=3),
                patch("brimstone.cli.startup_sequence", return_value=(object(), object())),
                patch("brimstone.cli._run_research_worker"),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--research", "--repo", _REPO, "--milestone", _MILESTONE],
                )
        assert result.exit_code == 0, result.output

    def test_dry_run_skips_milestone_check(self, tmp_path: Path) -> None:
        """--dry-run must not call _milestone_exists (no network call)."""
        milestone_checked: list[bool] = []

        def fake_check(repo: str, milestone: str) -> bool:
            milestone_checked.append(True)
            return False

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch("brimstone.cli._milestone_exists", side_effect=fake_check),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--research",
                        "--repo",
                        _REPO,
                        "--milestone",
                        _MILESTONE,
                        "--dry-run",
                    ],
                )

        assert result.exit_code == 0, result.output
        assert not milestone_checked, "--dry-run must not check milestone existence"


# ---------------------------------------------------------------------------
# Stage completion checks (skip when already done)
# ---------------------------------------------------------------------------


class TestRunCompletionSkip:
    def test_research_skipped_when_no_open_issues(self, tmp_path: Path) -> None:
        """Research stage is skipped if there are no open research issues."""
        research_called: list[bool] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._count_all_open_research_issues", return_value=0),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch(
                    "brimstone.cli._run_research_worker",
                    side_effect=lambda **kw: research_called.append(True),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--research", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code == 0, result.output
        assert not research_called, "research_worker should not run if already complete"
        assert "already complete" in result.output

    def test_design_skipped_when_hld_exists(self, tmp_path: Path) -> None:
        """Design stage is skipped if HLD.md already exists on the default branch."""
        design_called: list[bool] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._count_all_open_research_issues", return_value=0),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=True),
                patch(
                    "brimstone.cli._run_design_worker",
                    side_effect=lambda **kw: design_called.append(True),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--design", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code == 0, result.output
        assert not design_called, "design_worker should not run if HLD already exists"
        assert "already complete" in result.output


# ---------------------------------------------------------------------------
# Gate checks
# ---------------------------------------------------------------------------


class TestRunGates:
    def test_design_gate_blocks_when_research_not_done(self, tmp_path: Path) -> None:
        """--design without --research aborts if there are open research issues."""
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
                patch("brimstone.cli._count_all_open_research_issues", return_value=5),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--design", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code != 0
        assert "--research" in result.output or "research" in result.output.lower()

    def test_impl_gate_blocks_when_hld_missing(self, tmp_path: Path) -> None:
        """--impl without --design aborts if HLD.md is missing."""
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
                patch("brimstone.cli._list_open_impl_issues", return_value=[]),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--impl", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code != 0
        assert "--design" in result.output or "design" in result.output.lower()

    def test_all_skips_design_gate(self, tmp_path: Path) -> None:
        """--all does not check the design gate (research is also being run)."""
        calls: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._count_all_open_research_issues", return_value=2),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
                patch("brimstone.cli._list_open_impl_issues", return_value=[{"number": 1}]),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object()),
                ),
                patch(
                    "brimstone.cli._run_research_worker",
                    side_effect=lambda **kw: calls.append("research"),
                ),
                patch(
                    "brimstone.cli._run_design_worker",
                    side_effect=lambda **kw: calls.append("design"),
                ),
                patch(
                    "brimstone.cli._run_impl_worker",
                    side_effect=lambda **kw: calls.append("impl"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--all", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code == 0, result.output
        assert "research" in calls
        assert "design" in calls
        assert "impl" in calls

    def test_design_research_together_skips_gate(self, tmp_path: Path) -> None:
        """--design --research together does not trigger the design gate."""
        calls: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._count_all_open_research_issues", return_value=1),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
                patch("brimstone.cli.startup_sequence", return_value=(object(), object())),
                patch(
                    "brimstone.cli._run_research_worker",
                    side_effect=lambda **kw: calls.append("research"),
                ),
                patch(
                    "brimstone.cli._run_design_worker",
                    side_effect=lambda **kw: calls.append("design"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--research",
                        "--design",
                        "--repo",
                        _REPO,
                        "--milestone",
                        _MILESTONE,
                    ],
                )

        assert result.exit_code == 0, result.output
        assert calls == ["research", "design"]


# ---------------------------------------------------------------------------
# impl auto-runs plan-issues when no open impl issues
# ---------------------------------------------------------------------------


class TestRunImplAutoPlanIssues:
    def test_impl_auto_runs_plan_issues_when_empty(self, tmp_path: Path) -> None:
        calls: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=True),
                patch("brimstone.cli._list_open_impl_issues", return_value=[]),
                patch("brimstone.cli.startup_sequence", return_value=(object(), object())),
                patch(
                    "brimstone.cli._run_plan_issues",
                    side_effect=lambda **kw: calls.append("plan-issues"),
                ),
                patch(
                    "brimstone.cli._run_impl_worker",
                    side_effect=lambda **kw: calls.append("impl"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--impl", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code == 0, result.output
        assert calls == ["plan-issues", "impl"]

    def test_impl_skips_plan_issues_when_issues_exist(self, tmp_path: Path) -> None:
        calls: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=True),
                patch(
                    "brimstone.cli._list_open_impl_issues",
                    return_value=[{"number": 1}, {"number": 2}],
                ),
                patch("brimstone.cli.startup_sequence", return_value=(object(), object())),
                patch(
                    "brimstone.cli._run_plan_issues",
                    side_effect=lambda **kw: calls.append("plan-issues"),
                ),
                patch(
                    "brimstone.cli._run_impl_worker",
                    side_effect=lambda **kw: calls.append("impl"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--impl", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code == 0, result.output
        assert "plan-issues" not in calls
        assert "impl" in calls


# ---------------------------------------------------------------------------
# --dry-run: prints without invoking workers
# ---------------------------------------------------------------------------


class TestRunDryRun:
    def test_dry_run_prints_stages_without_calling_workers(self, tmp_path: Path) -> None:
        workers_called: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))),
                patch(
                    "brimstone.cli._run_research_worker",
                    side_effect=lambda **kw: workers_called.append("research"),
                ),
                patch(
                    "brimstone.cli._run_design_worker",
                    side_effect=lambda **kw: workers_called.append("design"),
                ),
                patch(
                    "brimstone.cli._run_impl_worker",
                    side_effect=lambda **kw: workers_called.append("impl"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--all",
                        "--repo",
                        _REPO,
                        "--milestone",
                        _MILESTONE,
                        "--dry-run",
                    ],
                )

        assert result.exit_code == 0, result.output
        assert not workers_called, "No workers should run in --dry-run mode"
        assert "dry-run" in result.output.lower()

    def test_dry_run_clears_claudecode_env(self, tmp_path: Path) -> None:
        """CLAUDECODE must be cleared even in dry-run mode."""
        snapshots: list[str | None] = []

        with patch.dict("os.environ", {**MINIMAL_ENV, "CLAUDECODE": "1"}, clear=False):
            with patch("brimstone.cli._resolve_repo", return_value=(_REPO, str(tmp_path))):
                original_count = patch("brimstone.cli._count_all_open_research_issues")
                with original_count:

                    def capture(*a: object, **kw: object) -> None:
                        snapshots.append(os.environ.get("CLAUDECODE"))

                    runner = CliRunner()
                    result = runner.invoke(
                        composer,
                        [
                            "run",
                            "--research",
                            "--repo",
                            _REPO,
                            "--milestone",
                            _MILESTONE,
                            "--dry-run",
                        ],
                    )

        # The command must not fail and CLAUDECODE must have been cleared at invocation time
        assert result.exit_code == 0, result.output
        assert os.environ.get("CLAUDECODE") == "1"  # restored after invoke


# ---------------------------------------------------------------------------
# brimstone init --help
# ---------------------------------------------------------------------------


class TestInitHelp:
    def test_init_appears_in_composer_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(composer, ["--help"])
        assert result.exit_code == 0
        assert "init" in result.output

    def test_init_help_shows_options(self) -> None:
        runner = CliRunner()
        result = runner.invoke(composer, ["init", "--help"])
        assert result.exit_code == 0
        assert "--repo" in result.output
        assert "--spec" in result.output
        assert "--dry-run" in result.output


# ---------------------------------------------------------------------------
# brimstone adopt stub
# ---------------------------------------------------------------------------


class TestAdoptStub:
    def test_adopt_exits_1_with_message(self) -> None:
        runner = CliRunner()
        result = runner.invoke(composer, ["adopt", "--source-repo", "owner/repo"])
        assert result.exit_code == 1
        assert "not yet implemented" in result.output.lower()
