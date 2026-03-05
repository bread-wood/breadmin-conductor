"""Unit tests for the `brimstone run` subcommand (cli.py)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from brimstone.cli import brimstone, composer

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
    def test_run_appears_in_brimstone_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(brimstone, ["--help"])
        assert result.exit_code == 0
        assert "run" in result.output

    def test_run_appears_in_composer_help(self) -> None:
        """composer is an alias for brimstone — backwards compat."""
        runner = CliRunner()
        result = runner.invoke(brimstone, ["--help"])
        assert result.exit_code == 0
        assert "run" in result.output

    def test_run_help_shows_options(self) -> None:
        runner = CliRunner()
        result = runner.invoke(brimstone, ["run", "--help"])
        assert result.exit_code == 0
        output = result.output
        assert "--research" in output
        assert "--design" in output
        assert "--impl" in output
        assert "--all" in output
        assert "--milestone" in output
        assert "--dry-run" in output
        assert "--stage" in output


# ---------------------------------------------------------------------------
# No stage flags → error
# ---------------------------------------------------------------------------


class TestRunRequiresStage:
    def test_no_stage_flags_produces_error(self) -> None:
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with patch("brimstone.cli._resolve_repo", return_value=_REPO):
                with patch("brimstone.cli._milestone_exists", return_value=True):
                    runner = CliRunner()
                    result = runner.invoke(
                        brimstone,
                        ["run", "--repo", _REPO, "--milestone", _MILESTONE],
                    )
        assert result.exit_code != 0
        output = result.output
        assert "--stage" in output or "--research" in output or "--all" in output

    def test_missing_milestone_produces_error(self) -> None:
        runner = CliRunner()
        result = runner.invoke(brimstone, ["run", "--research", "--repo", _REPO])
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
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
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
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._count_open_issues_by_label", return_value=3),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
                ),
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
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
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
        """Research stage is skipped if all research beads are closed."""
        research_called: list[bool] = []
        mock_store = MagicMock()
        mock_store.list_work_beads.return_value = [
            MagicMock(state="closed"),
            MagicMock(state="closed"),
        ]

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli.make_bead_store", return_value=mock_store),
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
        """Design stage is skipped if all design beads are closed and HLD exists."""
        design_called: list[bool] = []
        mock_store = MagicMock()
        mock_store.list_work_beads.return_value = [
            MagicMock(state="closed"),
            MagicMock(state="closed"),
        ]

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli.make_bead_store", return_value=mock_store),
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
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
                patch("brimstone.cli._count_open_issues_by_label", return_value=5),
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
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
                patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
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
        mock_store = MagicMock()

        def _beads(**kw: object) -> list:
            # Return impl beads so scope is skipped; no beads for other stages so they run.
            return [MagicMock(state="open")] if kw.get("stage") == "impl" else []

        mock_store.list_work_beads.side_effect = _beads

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli.make_bead_store", return_value=mock_store),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
                patch("brimstone.cli._list_open_issues_by_label", return_value=[{"number": 1}]),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
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
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._count_open_issues_by_label", return_value=1),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
                ),
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


class TestRunScopeStage:
    def test_scope_calls_plan_issues(self, tmp_path: Path) -> None:
        """--scope must invoke _run_plan_issues."""
        calls: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=True),
                patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
                ),
                patch(
                    "brimstone.cli._run_plan_issues",
                    side_effect=lambda **kw: calls.append("plan-issues"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--scope", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code == 0, result.output
        assert calls == ["plan-issues"]

    def test_impl_fails_when_no_impl_issues(self, tmp_path: Path) -> None:
        """--impl without prior --scope must fail with a clear error."""
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=True),
                patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--impl", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code != 0
        assert "--scope" in result.output

    def test_impl_runs_when_impl_issues_exist(self, tmp_path: Path) -> None:
        calls: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=True),
                patch("brimstone.cli._count_open_issues_by_label", return_value=2),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
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
        assert calls == ["impl"]


# ---------------------------------------------------------------------------
# --dry-run: prints without invoking workers
# ---------------------------------------------------------------------------


class TestRunDryRun:
    def test_dry_run_prints_stages_without_calling_workers(self, tmp_path: Path) -> None:
        workers_called: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
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

    def test_dry_run_succeeds_when_running_inside_claude_code(self, tmp_path: Path) -> None:
        """brimstone run must succeed even when CLAUDECODE=1 is set (running inside Claude Code)."""
        with patch.dict("os.environ", {**MINIMAL_ENV, "CLAUDECODE": "1"}, clear=False):
            with patch("brimstone.cli._resolve_repo", return_value=_REPO):
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


# ---------------------------------------------------------------------------
# brimstone init --help
# ---------------------------------------------------------------------------


class TestInitHelp:
    def test_init_appears_in_brimstone_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(brimstone, ["--help"])
        assert result.exit_code == 0
        assert "init" in result.output

    def test_init_help_shows_options(self) -> None:
        runner = CliRunner()
        result = runner.invoke(brimstone, ["init", "--help"])
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
        result = runner.invoke(brimstone, ["adopt", "--source-repo", "owner/repo"])
        assert result.exit_code == 1
        assert "not yet implemented" in result.output.lower()


# ---------------------------------------------------------------------------
# --stage flag (new primary interface)
# ---------------------------------------------------------------------------


class TestStageFlag:
    def test_stage_impl_invokes_impl_worker(self, tmp_path: Path) -> None:
        """--stage impl must invoke _run_impl_worker."""
        calls: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._count_open_issues_by_label", return_value=2),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
                ),
                patch(
                    "brimstone.cli._run_impl_worker",
                    side_effect=lambda **kw: calls.append("impl"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    brimstone,
                    ["run", "--stage", "impl", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code == 0, result.output
        assert calls == ["impl"]

    def test_stage_research_invokes_research_worker(self, tmp_path: Path) -> None:
        """--stage research must invoke _run_research_worker."""
        calls: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._count_open_issues_by_label", return_value=3),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
                ),
                patch(
                    "brimstone.cli._run_research_worker",
                    side_effect=lambda **kw: calls.append("research"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    brimstone,
                    ["run", "--stage", "research", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code == 0, result.output
        assert calls == ["research"]

    def test_stage_wins_over_legacy_flags(self, tmp_path: Path) -> None:
        """When both --stage and a legacy flag are given, --stage wins."""
        calls: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._count_open_issues_by_label", return_value=3),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
                ),
                patch(
                    "brimstone.cli._run_research_worker",
                    side_effect=lambda **kw: calls.append("research"),
                ),
                patch(
                    "brimstone.cli._run_impl_worker",
                    side_effect=lambda **kw: calls.append("impl"),
                ),
            ):
                runner = CliRunner()
                # --stage research should win over --impl (legacy flag)
                result = runner.invoke(
                    brimstone,
                    [
                        "run",
                        "--stage",
                        "research",
                        "--impl",  # legacy flag — should be ignored
                        "--repo",
                        _REPO,
                        "--milestone",
                        _MILESTONE,
                    ],
                )

        assert result.exit_code == 0, result.output
        assert "research" in calls
        assert "impl" not in calls

    def test_legacy_impl_flag_still_works(self, tmp_path: Path) -> None:
        """The deprecated --impl flag must still work as before."""
        calls: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._count_open_issues_by_label", return_value=2),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
                ),
                patch(
                    "brimstone.cli._run_impl_worker",
                    side_effect=lambda **kw: calls.append("impl"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    brimstone,
                    ["run", "--impl", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code == 0, result.output
        assert calls == ["impl"]


# ---------------------------------------------------------------------------
# Positional spec arg — milestone inferred from stem
# ---------------------------------------------------------------------------


class TestPositionalSpec:
    def test_positional_spec_infers_milestone_from_stem(self, tmp_path: Path) -> None:
        """Positional spec arg should infer milestone from the filename stem."""
        spec_file = tmp_path / "v0.2.0-function-library.md"
        spec_file.write_text("# spec", encoding="utf-8")

        calls: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._ensure_labels"),
                patch("brimstone.cli._count_open_issues_by_label", return_value=3),
                patch("brimstone.cli._count_all_issues_by_label", return_value=0),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
                patch("brimstone.cli._list_open_issues_by_label", return_value=[{"number": 1}]),
                patch("brimstone.cli._upload_spec_to_repo"),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
                ),
                patch(
                    "brimstone.cli._run_plan",
                    side_effect=lambda **kw: calls.append(f"plan:{kw.get('version')}"),
                ),
                patch(
                    "brimstone.cli._run_research_worker",
                    side_effect=lambda **kw: calls.append(f"research:{kw.get('milestone')}"),
                ),
                patch(
                    "brimstone.cli._run_design_worker",
                    side_effect=lambda **kw: calls.append(f"design:{kw.get('milestone')}"),
                ),
                patch(
                    "brimstone.cli._run_plan_issues",
                    side_effect=lambda **kw: calls.append(f"scope:{kw.get('milestone')}"),
                ),
                patch(
                    "brimstone.cli._run_impl_worker",
                    side_effect=lambda **kw: calls.append(f"impl:{kw.get('milestone')}"),
                ),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    brimstone,
                    ["run", "--stage", "all", "--repo", _REPO, str(spec_file)],
                )

        assert result.exit_code == 0, result.output
        # Milestone should be inferred as "v0.2.0" from stem "v0.2.0-function-library"
        ms_calls = [c for c in calls if "v0.2.0" in c]
        assert len(ms_calls) > 0, f"Expected v0.2.0 milestone in calls, got: {calls}"

    def test_simple_version_stem(self, tmp_path: Path) -> None:
        """v0.2.0.md -> milestone v0.2.0."""
        spec_file = tmp_path / "v0.2.0.md"
        spec_file.write_text("# spec", encoding="utf-8")

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._ensure_labels"),
                patch("brimstone.cli._count_open_issues_by_label", return_value=3),
                patch("brimstone.cli._count_all_issues_by_label", return_value=0),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
                patch("brimstone.cli._list_open_issues_by_label", return_value=[{"number": 1}]),
                patch("brimstone.cli._upload_spec_to_repo"),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
                ),
                patch("brimstone.cli._run_plan"),
                patch("brimstone.cli._run_research_worker"),
                patch("brimstone.cli._run_design_worker"),
                patch("brimstone.cli._run_plan_issues"),
                patch("brimstone.cli._run_impl_worker"),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    brimstone,
                    ["run", "--stage", "all", "--repo", _REPO, str(spec_file)],
                )

        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# Campaign loop — multiple specs run in order
# ---------------------------------------------------------------------------


class TestCampaignLoop:
    def test_two_specs_run_in_order(self, tmp_path: Path) -> None:
        """With 2 spec files, both milestones run plan→research→…→impl in order."""
        spec_v1 = tmp_path / "v0.1.0.md"
        spec_v2 = tmp_path / "v0.2.0.md"
        spec_v1.write_text("# v0.1.0 spec", encoding="utf-8")
        spec_v2.write_text("# v0.2.0 spec", encoding="utf-8")

        calls: list[str] = []

        def fake_plan(**kw: object) -> None:
            calls.append(f"plan:{kw.get('version')}")

        def fake_research(**kw: object) -> None:
            calls.append(f"research:{kw.get('milestone')}")

        def fake_design(**kw: object) -> None:
            calls.append(f"design:{kw.get('milestone')}")

        def fake_scope(**kw: object) -> None:
            calls.append(f"scope:{kw.get('milestone')}")

        def fake_impl(**kw: object) -> None:
            calls.append(f"impl:{kw.get('milestone')}")

        # _count_open_issues_by_label is called in two contexts:
        # 1. Completion check before each stage (must return > 0 so the stage runs)
        # 2. Campaign gate after impl completes (must return 0 so campaign advances)
        #
        # Strategy: track which milestones have impl in the calls list.
        # For the campaign gate call (label==IMPL_LABEL, milestone already in calls),
        # return 0. Otherwise return 3 so stages are not skipped.
        def count_issues_side_effect(repo: str, milestone: str, label: str) -> int:
            if label == "stage/impl":
                # Check if impl has already been called for this milestone
                already_ran = f"impl:{milestone}" in calls
                if already_ran:
                    return 0  # campaign gate: impl is done, advance
            return 3  # completion check: issue exists, run the stage

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch("brimstone.cli._get_default_branch_for_repo", return_value="main"),
                patch("brimstone.cli._ensure_labels"),
                patch(
                    "brimstone.cli._count_open_issues_by_label",
                    side_effect=count_issues_side_effect,
                ),
                patch("brimstone.cli._count_all_issues_by_label", return_value=0),
                patch("brimstone.cli._doc_exists_on_default_branch", return_value=False),
                patch("brimstone.cli._list_open_issues_by_label", return_value=[{"number": 1}]),
                patch("brimstone.cli._upload_spec_to_repo"),
                patch("brimstone.cli.make_bead_store") as mock_make_store,
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(object(), object(), object()),
                ),
                patch("brimstone.cli._run_plan", side_effect=fake_plan),
                patch("brimstone.cli._run_research_worker", side_effect=fake_research),
                patch("brimstone.cli._run_design_worker", side_effect=fake_design),
                patch("brimstone.cli._run_plan_issues", side_effect=fake_scope),
                patch("brimstone.cli._run_impl_worker", side_effect=fake_impl),
            ):
                # Mock the campaign bead store
                mock_store = MagicMock()
                mock_store.read_campaign_bead.return_value = None
                mock_store.write_campaign_bead = MagicMock()
                mock_make_store.return_value = mock_store

                runner = CliRunner()
                result = runner.invoke(
                    brimstone,
                    [
                        "run",
                        "--stage",
                        "all",
                        "--repo",
                        _REPO,
                        str(spec_v1),
                        str(spec_v2),
                    ],
                )

        assert result.exit_code == 0, result.output

        # Both milestones should have run
        v1_calls = [c for c in calls if "v0.1.0" in c]
        v2_calls = [c for c in calls if "v0.2.0" in c]
        assert len(v1_calls) > 0, f"Expected v0.1.0 calls, got: {calls}"
        assert len(v2_calls) > 0, f"Expected v0.2.0 calls, got: {calls}"

        # v0.1.0 must come before v0.2.0 in the call order
        first_v1 = next(i for i, c in enumerate(calls) if "v0.1.0" in c)
        first_v2 = next(i for i, c in enumerate(calls) if "v0.2.0" in c)
        assert first_v1 < first_v2, "v0.1.0 should run before v0.2.0"

        # CampaignBead should have been written
        assert mock_store.write_campaign_bead.called
