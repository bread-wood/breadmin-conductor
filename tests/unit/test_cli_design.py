"""Unit tests for design_worker and plan_issues loops in src/composer/cli.py.

Tests cover:
- _run_design_worker: dry-run prints prompt info without calling runner.run
- _run_design_worker: dispatches runner.run with correct tools and logs events
- _run_design_worker: handles runner error result gracefully
- _run_plan_issues: dry-run prints prompt info without calling runner.run
- _run_plan_issues: dispatches runner.run with correct tools and logs events
- _run_plan_issues: handles runner error result gracefully
- design_worker Click command: requires --repo and --research-milestone
- plan_issues Click command: requires --repo and --impl-milestone
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from composer.cli import _run_design_worker, _run_plan_issues, design_worker, plan_issues
from composer.config import Config
from composer.runner import RunResult
from composer.session import Checkpoint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
    "GITHUB_TOKEN": "ghp-test-token",
}


def make_config(tmp_path: Path, **overrides) -> Config:
    """Return a minimal Config instance with tmp_path-based dirs."""
    with patch.dict("os.environ", MINIMAL_ENV, clear=False):
        config = Config(
            anthropic_api_key=MINIMAL_ENV["ANTHROPIC_API_KEY"],
            github_token=MINIMAL_ENV["GITHUB_TOKEN"],
        )
    object.__setattr__(config, "checkpoint_dir", tmp_path / "checkpoints")
    object.__setattr__(config, "log_dir", tmp_path / "logs")
    for k, v in overrides.items():
        object.__setattr__(config, k, v)
    return config


def make_checkpoint(**overrides) -> Checkpoint:
    """Return a minimal Checkpoint instance."""
    defaults = dict(
        schema_version=1,
        run_id="test-run-id",
        session_id="",
        repo="owner/repo",
        default_branch="main",
        milestone="v1",
        stage="design",
        timestamp="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Checkpoint(**defaults)


def make_run_result(
    *,
    is_error: bool = False,
    subtype: str | None = "success",
    error_code: str | None = None,
) -> RunResult:
    """Return a RunResult with the given classification."""
    return RunResult(
        is_error=is_error,
        subtype=subtype,
        error_code=error_code,
        exit_code=1 if is_error else 0,
        total_cost_usd=0.05 if not is_error else None,
        input_tokens=500,
        output_tokens=200,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        raw_result_event={"result": "", "subtype": subtype},
        stderr="",
        overage_detected=False,
    )


# ---------------------------------------------------------------------------
# _run_design_worker
# ---------------------------------------------------------------------------


class TestRunDesignWorker:
    def test_dry_run_prints_info_without_running(self, tmp_path: Path, capsys) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="design")

        with (
            patch("composer.cli.runner.run") as mock_run,
            patch("composer.cli.logger.log_conductor_event"),
            patch("composer.cli.session.save"),
        ):
            _run_design_worker(
                repo="owner/repo",
                research_milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=True,
            )
            mock_run.assert_not_called()

        captured = capsys.readouterr()
        assert "[dry-run]" in captured.out
        assert "design-worker" in captured.out
        assert "v1" in captured.out

    def test_dispatches_runner_with_correct_tools(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="design")
        run_result = make_run_result()

        with (
            patch("composer.cli.runner.run", return_value=run_result) as mock_run,
            patch("composer.cli.logger.log_conductor_event"),
            patch("composer.cli.session.save"),
            patch("composer.cli.build_subprocess_env", return_value={}),
        ):
            _run_design_worker(
                repo="owner/repo",
                research_milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        allowed_tools = call_kwargs.kwargs.get("allowed_tools") or call_kwargs.args[1]
        assert "Bash" in allowed_tools
        assert "Read" in allowed_tools
        assert "Write" in allowed_tools
        assert "Glob" in allowed_tools
        assert "Grep" in allowed_tools
        assert "mcp__notion__API-post-page" in allowed_tools
        # design-worker must NOT dispatch sub-agents, so no agent tool
        assert "mcp__claude_code__execute" not in allowed_tools

    def test_prompt_includes_milestone_and_repo(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="design")
        run_result = make_run_result()
        captured_prompt: list[str] = []

        def capture_run(prompt: str, **kwargs) -> RunResult:
            captured_prompt.append(prompt)
            return run_result

        with (
            patch("composer.cli.runner.run", side_effect=capture_run),
            patch("composer.cli.logger.log_conductor_event"),
            patch("composer.cli.session.save"),
            patch("composer.cli.build_subprocess_env", return_value={}),
        ):
            _run_design_worker(
                repo="owner/repo",
                research_milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        assert len(captured_prompt) == 1
        prompt = captured_prompt[0]
        assert "owner/repo" in prompt
        assert "v1" in prompt
        # Skill file content injected
        assert "design-worker" in prompt.lower() or "research" in prompt.lower()

    def test_error_result_prints_to_stderr(self, tmp_path: Path, capsys) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="design")
        run_result = make_run_result(is_error=True, subtype="error_timeout", error_code="timeout")

        with (
            patch("composer.cli.runner.run", return_value=run_result),
            patch("composer.cli.logger.log_conductor_event"),
            patch("composer.cli.session.save"),
            patch("composer.cli.build_subprocess_env", return_value={}),
        ):
            _run_design_worker(
                repo="owner/repo",
                research_milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        captured = capsys.readouterr()
        assert "error" in captured.err.lower()

    def test_logs_start_and_complete_events(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="design")
        run_result = make_run_result()
        logged_events: list[str] = []

        def record_event(**kwargs) -> None:
            logged_events.append(kwargs.get("event_type", ""))

        with (
            patch("composer.cli.runner.run", return_value=run_result),
            patch("composer.cli.logger.log_conductor_event", side_effect=record_event),
            patch("composer.cli.session.save"),
            patch("composer.cli.build_subprocess_env", return_value={}),
        ):
            _run_design_worker(
                repo="owner/repo",
                research_milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        assert "design_worker_start" in logged_events
        assert "design_worker_complete" in logged_events


# ---------------------------------------------------------------------------
# _run_plan_issues
# ---------------------------------------------------------------------------


class TestRunPlanIssues:
    def test_dry_run_prints_info_without_running(self, tmp_path: Path, capsys) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="plan-issues")

        with (
            patch("composer.cli.runner.run") as mock_run,
            patch("composer.cli.logger.log_conductor_event"),
            patch("composer.cli.session.save"),
        ):
            _run_plan_issues(
                repo="owner/repo",
                impl_milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=True,
            )
            mock_run.assert_not_called()

        captured = capsys.readouterr()
        assert "[dry-run]" in captured.out
        assert "plan-issues" in captured.out
        assert "v1" in captured.out

    def test_dispatches_runner_with_correct_tools(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="plan-issues")
        run_result = make_run_result()

        with (
            patch("composer.cli.runner.run", return_value=run_result) as mock_run,
            patch("composer.cli.logger.log_conductor_event"),
            patch("composer.cli.session.save"),
            patch("composer.cli.build_subprocess_env", return_value={}),
        ):
            _run_plan_issues(
                repo="owner/repo",
                impl_milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args
        allowed_tools = call_kwargs.kwargs.get("allowed_tools") or call_kwargs.args[1]
        assert "Bash" in allowed_tools
        assert "Read" in allowed_tools
        assert "Glob" in allowed_tools
        assert "Grep" in allowed_tools
        assert "mcp__notion__API-post-page" in allowed_tools
        # plan-issues only reads files and calls gh; must NOT use Write/Edit
        assert "Write" not in allowed_tools
        assert "Edit" not in allowed_tools

    def test_prompt_includes_impl_milestone_and_repo(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="plan-issues")
        run_result = make_run_result()
        captured_prompt: list[str] = []

        def capture_run(prompt: str, **kwargs) -> RunResult:
            captured_prompt.append(prompt)
            return run_result

        with (
            patch("composer.cli.runner.run", side_effect=capture_run),
            patch("composer.cli.logger.log_conductor_event"),
            patch("composer.cli.session.save"),
            patch("composer.cli.build_subprocess_env", return_value={}),
        ):
            _run_plan_issues(
                repo="owner/repo",
                impl_milestone="v1-impl",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        assert len(captured_prompt) == 1
        prompt = captured_prompt[0]
        assert "owner/repo" in prompt
        assert "v1-impl" in prompt
        # Skill file content injected
        assert "plan-issues" in prompt.lower() or "impl" in prompt.lower()

    def test_dry_run_flag_noted_in_prompt(self, tmp_path: Path) -> None:
        """When dry_run=True the prompt mentions the dry-run constraint."""
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="plan-issues")
        captured_prompt: list[str] = []

        # We need to capture the base_prompt before inject_skill is called.
        # Patch inject_skill to capture what was passed.
        original_inject = __import__("composer.cli", fromlist=["inject_skill"]).inject_skill

        def capture_inject(skill_name: str, base_prompt: str) -> str:
            captured_prompt.append(base_prompt)
            return original_inject(skill_name, base_prompt)

        with (
            patch("composer.cli.runner.run") as mock_run,
            patch("composer.cli.logger.log_conductor_event"),
            patch("composer.cli.session.save"),
            patch("composer.cli.inject_skill", side_effect=capture_inject),
        ):
            _run_plan_issues(
                repo="owner/repo",
                impl_milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=True,
            )
            mock_run.assert_not_called()

        # With dry_run=True we return early, but the base_prompt is built first
        # and the dry-run note is embedded.
        assert len(captured_prompt) == 1
        assert "dry-run" in captured_prompt[0].lower() or "dry_run" in captured_prompt[0].lower()

    def test_error_result_prints_to_stderr(self, tmp_path: Path, capsys) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="plan-issues")
        run_result = make_run_result(
            is_error=True, subtype="error_max_turns", error_code="max_turns_exceeded"
        )

        with (
            patch("composer.cli.runner.run", return_value=run_result),
            patch("composer.cli.logger.log_conductor_event"),
            patch("composer.cli.session.save"),
            patch("composer.cli.build_subprocess_env", return_value={}),
        ):
            _run_plan_issues(
                repo="owner/repo",
                impl_milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        captured = capsys.readouterr()
        assert "error" in captured.err.lower()

    def test_logs_start_and_complete_events(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="plan-issues")
        run_result = make_run_result()
        logged_events: list[str] = []

        def record_event(**kwargs) -> None:
            logged_events.append(kwargs.get("event_type", ""))

        with (
            patch("composer.cli.runner.run", return_value=run_result),
            patch("composer.cli.logger.log_conductor_event", side_effect=record_event),
            patch("composer.cli.session.save"),
            patch("composer.cli.build_subprocess_env", return_value={}),
        ):
            _run_plan_issues(
                repo="owner/repo",
                impl_milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        assert "plan_issues_start" in logged_events
        assert "plan_issues_complete" in logged_events


# ---------------------------------------------------------------------------
# Click command: design_worker
# ---------------------------------------------------------------------------


class TestDesignWorkerCommand:
    def test_no_repo_outside_git_dir_fails(self, tmp_path: Path) -> None:
        """design-worker without --repo fails when cwd is not a git repo."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(design_worker, ["--research-milestone", "v1"])
        assert result.exit_code != 0
        assert "not a git repository" in result.output or "Error" in result.output

    def test_missing_research_milestone_fails(self) -> None:
        runner = CliRunner()
        result = runner.invoke(design_worker, ["--repo", "owner/repo"])
        assert result.exit_code != 0
        assert "Missing option" in result.output or "research-milestone" in result.output

    def test_dry_run_exits_cleanly(self, tmp_path: Path) -> None:
        """design-worker --dry-run should print and exit 0."""
        runner = CliRunner()
        with (
            patch("composer.cli.load_config") as mock_load_config,
            patch("composer.cli.startup_sequence") as mock_startup,
            patch("composer.cli._run_design_worker") as mock_run,
        ):
            mock_config = MagicMock()
            mock_config.checkpoint_dir = tmp_path
            mock_load_config.return_value = mock_config
            mock_startup.return_value = (mock_config, make_checkpoint())
            mock_run.return_value = None

            result = runner.invoke(
                design_worker,
                ["--repo", "owner/repo", "--research-milestone", "v1", "--dry-run"],
            )

        assert result.exit_code == 0
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["dry_run"] is True
        assert call_kwargs["repo"] == "owner/repo"
        assert call_kwargs["research_milestone"] == "v1"


# ---------------------------------------------------------------------------
# Click command: plan_issues
# ---------------------------------------------------------------------------


class TestPlanIssuesCommand:
    def test_no_repo_outside_git_dir_fails(self, tmp_path: Path) -> None:
        """plan-issues without --repo fails when cwd is not a git repo."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(plan_issues, ["--impl-milestone", "v1"])
        assert result.exit_code != 0
        assert "not a git repository" in result.output or "Error" in result.output

    def test_missing_impl_milestone_fails(self) -> None:
        runner = CliRunner()
        result = runner.invoke(plan_issues, ["--repo", "owner/repo"])
        assert result.exit_code != 0
        assert "Missing option" in result.output or "impl-milestone" in result.output

    def test_dry_run_exits_cleanly(self, tmp_path: Path) -> None:
        """plan-issues --dry-run should print and exit 0."""
        runner = CliRunner()
        with (
            patch("composer.cli.load_config") as mock_load_config,
            patch("composer.cli.startup_sequence") as mock_startup,
            patch("composer.cli._run_plan_issues") as mock_run,
        ):
            mock_config = MagicMock()
            mock_config.checkpoint_dir = tmp_path
            mock_load_config.return_value = mock_config
            mock_startup.return_value = (mock_config, make_checkpoint(stage="plan-issues"))
            mock_run.return_value = None

            result = runner.invoke(
                plan_issues,
                [
                    "--repo",
                    "owner/repo",
                    "--impl-milestone",
                    "v1",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args.kwargs
        assert call_kwargs["dry_run"] is True
        assert call_kwargs["repo"] == "owner/repo"
        assert call_kwargs["impl_milestone"] == "v1"
