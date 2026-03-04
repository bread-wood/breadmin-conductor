"""Unit tests for spec-seeding helpers and the `brimstone init` command in cli.py.

Tests cover:
- _validate_spec_path: relative path resolved from cwd
- _validate_spec_path: absolute path accepted as-is
- _validate_spec_path: non-existent path raises ClickException with clear message
- _validate_spec_path: non-.md extension raises ClickException with clear message
- _validate_spec_path: GitHub path (owner/repo/file.md) fetched via gh api and written to temp file
- _validate_spec_path: GitHub path fetch failure raises ClickException with clear message
- _seed_spec: version inferred from filename stem
- _seed_spec: spec already exists in target repo → warning printed, no overwrite
- _seed_spec: spec does not exist → file is copied and committed
- brimstone init: --repo and --spec are required
- brimstone init: version inferred from spec filename stem
- brimstone init: calls _upload_spec_to_repo and _run_plan
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from brimstone.cli import _validate_spec_path, composer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
    "BRIMSTONE_GH_TOKEN": "ghp-test-token",
}


# ---------------------------------------------------------------------------
# _validate_spec_path
# ---------------------------------------------------------------------------


class TestValidateSpecPath:
    def test_relative_path_resolved_from_cwd(self, tmp_path: Path) -> None:
        """A relative path is resolved from the current working directory."""
        spec_file = tmp_path / "myspec.md"
        spec_file.write_text("# Spec")

        import os

        original_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = _validate_spec_path("myspec.md")
        finally:
            os.chdir(original_cwd)

        assert result == spec_file.resolve()
        assert result.is_absolute()

    def test_absolute_path_accepted(self, tmp_path: Path) -> None:
        """An absolute path is accepted and returned as-is (resolved)."""
        spec_file = tmp_path / "myspec.md"
        spec_file.write_text("# Spec")

        result = _validate_spec_path(str(spec_file))

        assert result == spec_file.resolve()
        assert result.is_absolute()

    def test_nonexistent_path_raises_click_exception(self, tmp_path: Path) -> None:
        """A path that does not exist raises ClickException with a clear message."""
        import click

        missing = tmp_path / "does_not_exist.md"
        with pytest.raises(click.ClickException) as exc_info:
            _validate_spec_path(str(missing))

        assert "not found" in str(exc_info.value.format_message()).lower()

    def test_non_md_extension_raises_click_exception(self, tmp_path: Path) -> None:
        """A file that does not end in .md raises ClickException with a clear message."""
        import click

        txt_file = tmp_path / "myspec.txt"
        txt_file.write_text("not markdown")

        with pytest.raises(click.ClickException) as exc_info:
            _validate_spec_path(str(txt_file))

        msg = str(exc_info.value.format_message()).lower()
        assert ".md" in msg or "md file" in msg

    def test_tilde_expanded(self, tmp_path: Path) -> None:
        """expanduser() is applied so leading ~ is expanded correctly."""
        spec_file = tmp_path / "calc.md"
        spec_file.write_text("# Spec")

        # Use a real ~ path by temporarily pointing HOME at tmp_path
        import os

        original_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(tmp_path)
            result = _validate_spec_path("~/calc.md")
        finally:
            if original_home is not None:
                os.environ["HOME"] = original_home
            else:
                del os.environ["HOME"]

        assert result.exists()
        assert result.suffix == ".md"

    def test_github_path_fetches_and_writes_temp_file(self, tmp_path: Path) -> None:
        """A GitHub path (owner/repo/file.md) is fetched via gh api and returned as a temp file."""
        spec_content = "# My GitHub Spec\n\nSome content here.\n"
        # base64-encode with newlines as GitHub API returns it
        import base64

        b64_content = base64.b64encode(spec_content.encode()).decode() + "\n"

        def fake_run(cmd: list[str], **kwargs):  # type: ignore[return]
            result = MagicMock()
            if cmd[0] == "gh":
                result.returncode = 0
                result.stdout = b64_content
                result.stderr = ""
            elif cmd[0] == "base64":
                result.returncode = 0
                result.stdout = spec_content
                result.stderr = ""
            return result

        with patch("subprocess.run", side_effect=fake_run):
            result = _validate_spec_path("bread-wood/brimstone-specs/brimstone/v0.2.0-spec.md")

        assert result.exists()
        assert result.suffix == ".md"
        assert result.read_text() == spec_content

    def test_github_path_fetch_failure_raises_click_exception(self) -> None:
        """A failing gh api call for a GitHub path raises ClickException with a clear message."""
        import click

        def fake_run(cmd: list[str], **kwargs):  # type: ignore[return]
            result = MagicMock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "HTTP 404: Not Found"
            return result

        with patch("subprocess.run", side_effect=fake_run):
            with pytest.raises(click.ClickException) as exc_info:
                _validate_spec_path("bread-wood/brimstone-specs/brimstone/nonexistent.md")

        msg = str(exc_info.value.format_message()).lower()
        assert "github" in msg or "fetch" in msg or "could not" in msg


# ---------------------------------------------------------------------------
# brimstone init Click command
# ---------------------------------------------------------------------------


class TestInitCommand:
    def test_missing_repo_fails(self) -> None:
        """brimstone init fails when REPO argument is not given."""
        runner = CliRunner()
        result = runner.invoke(composer, ["init"])
        assert result.exit_code != 0

    def test_collaborator_added_on_init(self, tmp_path: Path) -> None:
        """brimstone init must add yeast-bot as a collaborator on the repo."""
        collab_calls: list[str] = []

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_proc.stderr = ""

        runner = CliRunner()
        with (
            patch("brimstone.cli.load_config") as mock_load_config,
            patch("brimstone.cli.startup_sequence") as mock_startup,
            patch(
                "brimstone.cli._add_brimstone_bot_collaborator",
                side_effect=lambda repo: collab_calls.append(repo),
            ),
            patch("brimstone.cli.subprocess.run", return_value=mock_proc),
            patch("brimstone.cli._setup_ci"),
            patch("brimstone.cli._ensure_labels"),
            patch("brimstone.cli._add_branch_protection"),
        ):
            mock_config = MagicMock()
            mock_config.checkpoint_dir = tmp_path
            mock_load_config.return_value = mock_config
            mock_startup.return_value = (mock_config, MagicMock(), MagicMock())

            result = runner.invoke(composer, ["init", "owner/repo"])

        assert result.exit_code == 0, result.output
        assert collab_calls == ["owner/repo"], "yeast-bot must be added as collaborator"

    def test_collaborator_not_added_in_dry_run(self, tmp_path: Path) -> None:
        """--dry-run must not call _add_brimstone_bot_collaborator."""
        collab_calls: list[str] = []

        runner = CliRunner()
        with (
            patch("brimstone.cli.load_config") as mock_load_config,
            patch("brimstone.cli.startup_sequence") as mock_startup,
            patch(
                "brimstone.cli._add_brimstone_bot_collaborator",
                side_effect=lambda repo: collab_calls.append(repo),
            ),
            patch("brimstone.cli._setup_ci"),
        ):
            mock_config = MagicMock()
            mock_config.checkpoint_dir = tmp_path
            mock_load_config.return_value = mock_config
            mock_startup.return_value = (mock_config, MagicMock(), MagicMock())

            result = runner.invoke(composer, ["init", "owner/repo", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert not collab_calls, "_add_brimstone_bot_collaborator must not be called in --dry-run"


# ---------------------------------------------------------------------------
# brimstone run --plan
# ---------------------------------------------------------------------------


class TestRunPlanCommand:
    def test_missing_spec_fails(self, tmp_path: Path) -> None:
        """run --plan fails when --spec is not given."""
        runner = CliRunner()
        with patch("brimstone.cli._resolve_repo", return_value="owner/repo"):
            result = runner.invoke(
                composer, ["run", "--plan", "--repo", "owner/repo", "--milestone", "mvp"]
            )
        assert result.exit_code != 0

    def test_milestone_inferred_when_omitted(self, tmp_path: Path) -> None:
        """run --plan does not require --milestone; it is inferred from spec filename."""
        spec_file = tmp_path / "mvp.md"
        spec_file.write_text("# Spec")
        runner = CliRunner()
        with patch("brimstone.cli._resolve_repo", return_value="owner/repo"):
            result = runner.invoke(
                composer, ["run", "--plan", "--repo", "owner/repo", "--spec", str(spec_file)]
            )
        # Should not fail on missing --milestone (though it may fail for other reasons in
        # this test environment; we only check that the UsageError is NOT raised).
        assert "milestone" not in (result.output or "").lower() or result.exit_code == 0

    def test_missing_milestone_fails_for_research(self) -> None:
        """run --research fails when --milestone is not given."""
        runner = CliRunner()
        with patch("brimstone.cli._resolve_repo", return_value="owner/repo"):
            result = runner.invoke(composer, ["run", "--research", "--repo", "owner/repo"])
        assert result.exit_code != 0

    def test_version_inferred_from_spec_filename(self, tmp_path: Path) -> None:
        """Version (spec_stem) is inferred from the spec filename stem."""
        spec_file = tmp_path / "calculator.md"
        spec_file.write_text("# Spec")

        runner = CliRunner()
        with (
            patch("brimstone.cli.load_config") as mock_load_config,
            patch("brimstone.cli.startup_sequence") as mock_startup,
            patch("brimstone.cli._upload_spec_to_repo"),
            patch("brimstone.cli._run_plan") as mock_run,
            patch("brimstone.cli._resolve_repo", return_value="owner/repo"),
            patch("brimstone.cli._milestone_exists", return_value=True),
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
        ):
            mock_config = MagicMock()
            mock_config.checkpoint_dir = tmp_path
            mock_load_config.return_value = mock_config
            mock_startup.return_value = (mock_config, MagicMock(), MagicMock())
            mock_run.return_value = None

            result = runner.invoke(
                composer,
                [
                    "run",
                    "--plan",
                    "--repo",
                    "owner/repo",
                    "--milestone",
                    "calculator",
                    "--spec",
                    str(spec_file),
                ],
            )

        assert result.exit_code == 0, result.output
        run_call_kwargs = mock_run.call_args.kwargs
        assert run_call_kwargs["version"] == "calculator"

    def test_plan_milestones_called_with_correct_version(self, tmp_path: Path) -> None:
        """_run_plan is called with the milestone as version."""
        spec_file = tmp_path / "mvp.md"
        spec_file.write_text("# MVP")

        plan_calls: list[dict] = []

        def fake_plan(**kwargs: object) -> None:
            plan_calls.append(dict(kwargs))

        runner = CliRunner()
        with (
            patch("brimstone.cli.load_config") as mock_load_config,
            patch("brimstone.cli.startup_sequence") as mock_startup,
            patch("brimstone.cli._upload_spec_to_repo"),
            patch("brimstone.cli._run_plan", side_effect=fake_plan),
            patch("brimstone.cli._resolve_repo", return_value="owner/repo"),
            patch("brimstone.cli._milestone_exists", return_value=True),
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
        ):
            mock_config = MagicMock()
            mock_config.checkpoint_dir = tmp_path
            mock_load_config.return_value = mock_config
            mock_startup.return_value = (mock_config, MagicMock(), MagicMock())

            result = runner.invoke(
                composer,
                [
                    "run",
                    "--plan",
                    "--repo",
                    "owner/repo",
                    "--milestone",
                    "mvp",
                    "--spec",
                    str(spec_file),
                ],
            )

        assert result.exit_code == 0, result.output
        assert len(plan_calls) == 1
        assert plan_calls[0]["version"] == "mvp"
        assert plan_calls[0]["repo"] == "owner/repo"

    def test_dry_run_skips_upload(self, tmp_path: Path) -> None:
        """--dry-run must not call _upload_spec_to_repo."""
        spec_file = tmp_path / "mvp.md"
        spec_file.write_text("# MVP")

        upload_called: list[bool] = []

        runner = CliRunner()
        with (
            patch("brimstone.cli.load_config") as mock_load_config,
            patch("brimstone.cli.startup_sequence") as mock_startup,
            patch(
                "brimstone.cli._upload_spec_to_repo",
                side_effect=lambda *a, **kw: upload_called.append(True),
            ),
            patch("brimstone.cli._run_plan"),
            patch("brimstone.cli._resolve_repo", return_value="owner/repo"),
            patch("brimstone.cli._milestone_exists", return_value=True),
            patch("brimstone.cli._get_default_branch_for_repo", return_value="mainline"),
        ):
            mock_config = MagicMock()
            mock_config.checkpoint_dir = tmp_path
            mock_load_config.return_value = mock_config
            mock_startup.return_value = (mock_config, MagicMock(), MagicMock())

            result = runner.invoke(
                composer,
                [
                    "run",
                    "--plan",
                    "--repo",
                    "owner/repo",
                    "--milestone",
                    "mvp",
                    "--spec",
                    str(spec_file),
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        assert not upload_called, "_upload_spec_to_repo must not be called in --dry-run mode"
