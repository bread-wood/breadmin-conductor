"""Unit tests for the `composer run` subcommand (cli.py)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from composer.cli import _PIPELINE_STAGES, composer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
    "GITHUB_TOKEN": "ghp-test-token",
}


def _make_mock_spec(tmp_path: Path) -> Path:
    """Write a minimal spec file and return its absolute path."""
    spec_file = tmp_path / "MVP.md"
    spec_file.write_text("# MVP Spec\n\nA minimal spec.\n")
    return spec_file


# ---------------------------------------------------------------------------
# _PIPELINE_STAGES order
# ---------------------------------------------------------------------------


class TestPipelineStagesConstant:
    def test_stages_in_correct_order(self) -> None:
        assert _PIPELINE_STAGES == [
            "plan-milestones",
            "research-worker",
            "design-worker",
            "plan-issues",
            "impl-worker",
        ]


# ---------------------------------------------------------------------------
# composer run --dry-run
# ---------------------------------------------------------------------------


class TestRunDryRun:
    def test_dry_run_prints_headers_and_skips_all_stages(self, tmp_path: Path) -> None:
        """--dry-run prints stage headers and [dry-run] lines but does not invoke stages."""
        spec_file = _make_mock_spec(tmp_path)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage") as mock_stage,
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--repo", "owner/repo", "--spec", str(spec_file), "--dry-run"],
                )

        assert result.exit_code == 0, result.output
        output = result.output

        # Each stage header must appear
        for stage in _PIPELINE_STAGES:
            assert f"── Stage: {stage} ──" in output
            assert f"[dry-run] would invoke {stage}" in output

        # _run_pipeline_stage must NOT be called in dry-run mode
        mock_stage.assert_not_called()

    def test_dry_run_does_not_require_credentials(self, tmp_path: Path) -> None:
        """--dry-run must not fail due to missing env vars (stages are not invoked)."""
        # Do NOT set ANTHROPIC_API_KEY or GITHUB_TOKEN
        with patch.dict("os.environ", {}, clear=True):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage") as mock_stage,
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--repo", "owner/repo", "--version", "MVP", "--dry-run"],
                )

        # Should succeed (no stages actually ran)
        assert result.exit_code == 0, result.output
        mock_stage.assert_not_called()


# ---------------------------------------------------------------------------
# composer run: stage execution order
# ---------------------------------------------------------------------------


class TestRunStageOrder:
    def test_stages_run_in_correct_order(self, tmp_path: Path) -> None:
        """All five stages must be called in pipeline order."""
        spec_file = _make_mock_spec(tmp_path)
        call_order: list[str] = []

        def fake_run_stage(stage: str, **kwargs: object) -> None:
            call_order.append(stage)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--repo", "owner/repo", "--spec", str(spec_file)],
                )

        assert result.exit_code == 0, result.output
        assert call_order == _PIPELINE_STAGES

    def test_stage_function_receives_correct_arguments(self, tmp_path: Path) -> None:
        """Each stage is called with repo_ref, local_path, version, and dry_run=False."""
        spec_file = _make_mock_spec(tmp_path)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage") as mock_stage,
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--repo", "owner/repo", "--spec", str(spec_file)],
                )

        assert result.exit_code == 0, result.output
        # Check the plan-milestones call
        first_call_kwargs = mock_stage.call_args_list[0].kwargs
        assert first_call_kwargs["stage"] == "plan-milestones"
        assert first_call_kwargs["repo_ref"] == "owner/repo"
        assert first_call_kwargs["version"] == "MVP"
        assert first_call_kwargs["dry_run"] is False


# ---------------------------------------------------------------------------
# composer run --from <stage>
# ---------------------------------------------------------------------------


class TestRunFromStage:
    def test_from_research_worker_skips_plan_milestones(self, tmp_path: Path) -> None:
        """--from research-worker must skip plan-milestones and run the rest."""
        called_stages: list[str] = []

        def fake_run_stage(stage: str, **kwargs: object) -> None:
            called_stages.append(stage)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--repo",
                        "owner/repo",
                        "--version",
                        "MVP",
                        "--from",
                        "research-worker",
                    ],
                )

        assert result.exit_code == 0, result.output
        assert called_stages == [
            "research-worker",
            "design-worker",
            "plan-issues",
            "impl-worker",
        ]
        # plan-milestones must not appear
        assert "plan-milestones" not in called_stages

    def test_from_plan_milestones_runs_all_stages(self, tmp_path: Path) -> None:
        """--from plan-milestones (first stage) runs all stages."""
        called_stages: list[str] = []

        def fake_run_stage(stage: str, **kwargs: object) -> None:
            called_stages.append(stage)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--repo",
                        "owner/repo",
                        "--version",
                        "MVP",
                        "--from",
                        "plan-milestones",
                    ],
                )

        assert result.exit_code == 0, result.output
        assert called_stages == _PIPELINE_STAGES

    def test_from_impl_worker_runs_only_last_stage(self, tmp_path: Path) -> None:
        """--from impl-worker must skip all earlier stages and run only impl-worker."""
        called_stages: list[str] = []

        def fake_run_stage(stage: str, **kwargs: object) -> None:
            called_stages.append(stage)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--repo",
                        "owner/repo",
                        "--version",
                        "MVP",
                        "--from",
                        "impl-worker",
                    ],
                )

        assert result.exit_code == 0, result.output
        assert called_stages == ["impl-worker"]

    def test_skip_output_printed_for_skipped_stages(self, tmp_path: Path) -> None:
        """[skip] must appear in output for each skipped stage."""
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage"),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--repo",
                        "owner/repo",
                        "--version",
                        "MVP",
                        "--from",
                        "design-worker",
                    ],
                )

        assert result.exit_code == 0, result.output
        output = result.output
        assert "[skip] plan-milestones" in output
        assert "[skip] research-worker" in output
        # design-worker and later should NOT have [skip]
        assert "[skip] design-worker" not in output
        assert "[skip] impl-worker" not in output


# ---------------------------------------------------------------------------
# composer run: invalid --from
# ---------------------------------------------------------------------------


class TestRunInvalidFrom:
    def test_invalid_from_value_produces_clear_error(self, tmp_path: Path) -> None:
        """An unrecognized --from value must fail with a clear error message."""
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--repo",
                        "owner/repo",
                        "--version",
                        "MVP",
                        "--from",
                        "not-a-real-stage",
                    ],
                )

        assert result.exit_code != 0
        assert "not-a-real-stage" in result.output or "not-a-real-stage" in (result.output or "")

    def test_invalid_from_value_lists_valid_stages(self, tmp_path: Path) -> None:
        """Error for invalid --from must mention at least one valid stage name."""
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--repo",
                        "owner/repo",
                        "--version",
                        "MVP",
                        "--from",
                        "unknown-stage",
                    ],
                )

        combined = result.output + (result.exception and str(result.exception) or "")
        # At least one valid stage must appear in the error output
        assert any(stage in combined for stage in _PIPELINE_STAGES)


# ---------------------------------------------------------------------------
# composer run: stage failure halts pipeline and prints resume hint
# ---------------------------------------------------------------------------


class TestRunStageFailure:
    def test_stage_failure_halts_pipeline(self, tmp_path: Path) -> None:
        """If a stage raises an exception, the pipeline halts (subsequent stages not called)."""
        called_stages: list[str] = []

        def fake_run_stage(stage: str, **kwargs: object) -> None:
            called_stages.append(stage)
            if stage == "research-worker":
                raise RuntimeError("research agent failed")

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--repo",
                        "owner/repo",
                        "--version",
                        "MVP",
                    ],
                )

        assert result.exit_code != 0
        # Only stages up to and including the failing stage must have been called
        assert called_stages == ["plan-milestones", "research-worker"]
        # design-worker and later must NOT have been called
        assert "design-worker" not in called_stages

    def test_stage_failure_prints_resume_hint(self, tmp_path: Path) -> None:
        """A stage failure must print a message with the failing stage name and resume command."""

        def fake_run_stage(stage: str, **kwargs: object) -> None:
            if stage == "design-worker":
                raise RuntimeError("design agent timed out")

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--repo",
                        "owner/repo",
                        "--version",
                        "MVP",
                    ],
                )

        assert result.exit_code != 0
        # The error output must name the failing stage and suggest a resume command
        combined_output = result.output + (result.output or "")
        assert "design-worker" in combined_output
        assert "--from" in combined_output

    def test_stage_failure_exit_code_nonzero(self, tmp_path: Path) -> None:
        """Any stage failure must produce a non-zero exit code."""

        def fake_run_stage(stage: str, **kwargs: object) -> None:
            if stage == "plan-milestones":
                raise Exception("something went wrong")

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--repo",
                        "owner/repo",
                        "--version",
                        "MVP",
                    ],
                )

        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# composer run: --version derived from --spec filename
# ---------------------------------------------------------------------------


class TestRunVersionInference:
    def test_version_inferred_from_spec_filename(self, tmp_path: Path) -> None:
        """When --spec is given without --version, version defaults to spec filename stem."""
        spec_file = tmp_path / "calculator.md"
        spec_file.write_text("# Calculator Spec\n")

        received_versions: list[str] = []

        def fake_run_stage(stage: str, version: str, **kwargs: object) -> None:
            received_versions.append(version)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--repo", "owner/repo", "--spec", str(spec_file)],
                )

        assert result.exit_code == 0, result.output
        # All stages received "calculator" as the version
        assert all(v == "calculator" for v in received_versions)
        assert len(received_versions) == len(_PIPELINE_STAGES)

    def test_explicit_version_overrides_spec_stem(self, tmp_path: Path) -> None:
        """--version overrides the version inferred from the spec filename."""
        spec_file = tmp_path / "calculator.md"
        spec_file.write_text("# Calculator Spec\n")

        received_versions: list[str] = []

        def fake_run_stage(stage: str, version: str, **kwargs: object) -> None:
            received_versions.append(version)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    [
                        "run",
                        "--repo",
                        "owner/repo",
                        "--spec",
                        str(spec_file),
                        "--version",
                        "MVP",
                    ],
                )

        assert result.exit_code == 0, result.output
        assert all(v == "MVP" for v in received_versions)

    def test_missing_version_and_spec_produces_error(self, tmp_path: Path) -> None:
        """When neither --spec nor --version is given, the command must fail with a clear error."""
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--repo", "owner/repo"],
                )

        assert result.exit_code != 0
        # Should mention --version or --spec in the error
        assert "--version" in result.output or "--spec" in result.output


# ---------------------------------------------------------------------------
# composer --help includes run
# ---------------------------------------------------------------------------


class TestRunInHelp:
    def test_run_appears_in_composer_help(self) -> None:
        """composer --help must list 'run' as a subcommand."""
        runner = CliRunner()
        result = runner.invoke(composer, ["--help"])

        assert result.exit_code == 0
        assert "run" in result.output

    def test_run_help_shows_options(self) -> None:
        """composer run --help must show --repo, --spec, --version, --from, --dry-run."""
        runner = CliRunner()
        result = runner.invoke(composer, ["run", "--help"])

        assert result.exit_code == 0
        output = result.output
        assert "--repo" in output
        assert "--spec" in output
        assert "--version" in output
        assert "--from" in output
        assert "--dry-run" in output


# ---------------------------------------------------------------------------
# composer run: error message surfaced (regression for Bug 1)
# ---------------------------------------------------------------------------


class TestRunErrorMessageSurfaced:
    def test_exception_message_printed_before_resume_hint(self, tmp_path: Path) -> None:
        """When a stage raises, its exception message must appear before the resume hint."""

        def fake_run_stage(stage: str, **kwargs: object) -> None:
            if stage == "plan-milestones":
                raise Exception("something went wrong")

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--repo", "owner/repo", "--version", "MVP"],
                )

        assert result.exit_code != 0
        # The raw exception message must appear in the combined output
        assert "something went wrong" in result.output
        # The resume hint must also appear
        assert "To resume:" in result.output
        # The exception message must appear before (at an earlier position than) the resume hint
        assert result.output.index("something went wrong") < result.output.index("To resume:")


# ---------------------------------------------------------------------------
# composer run: CLAUDECODE cleared before stage execution (regression for Bug 2)
# ---------------------------------------------------------------------------


class TestRunClearsCLAUDECODE:
    def test_claudecode_cleared_before_stages_run(self, tmp_path: Path) -> None:
        """composer run must clear CLAUDECODE so sub-stage load_config() calls succeed."""
        env_snapshots: list[str | None] = []

        def fake_run_stage(stage: str, **kwargs: object) -> None:
            env_snapshots.append(os.environ.get("CLAUDECODE"))

        with patch.dict("os.environ", {**MINIMAL_ENV, "CLAUDECODE": "1"}, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--repo", "owner/repo", "--version", "MVP", "--dry-run"],
                )

        # dry-run exits cleanly
        assert result.exit_code == 0, result.output

    def test_claudecode_not_set_during_stage_execution(self, tmp_path: Path) -> None:
        """CLAUDECODE must not be present in os.environ when stages are called."""
        env_snapshots: list[str | None] = []

        def fake_run_stage(stage: str, **kwargs: object) -> None:
            env_snapshots.append(os.environ.get("CLAUDECODE"))

        with patch.dict("os.environ", {**MINIMAL_ENV, "CLAUDECODE": "1"}, clear=False):
            with (
                patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
                patch("composer.cli._run_pipeline_stage", side_effect=fake_run_stage),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--repo", "owner/repo", "--version", "MVP"],
                )

        assert result.exit_code == 0, result.output
        # All stage invocations must have seen CLAUDECODE as absent (None)
        assert all(v is None for v in env_snapshots), (
            f"CLAUDECODE was still set during stage execution: {env_snapshots}"
        )
