"""Unit tests for plan_milestones --spec flag in src/composer/cli.py.

Tests cover:
- _validate_spec_path: relative path resolved from cwd
- _validate_spec_path: absolute path accepted as-is
- _validate_spec_path: non-existent path raises ClickException with clear message
- _validate_spec_path: non-.md extension raises ClickException with clear message
- _seed_spec: version inferred from filename stem
- _seed_spec: spec already exists in target repo → warning printed, no overwrite
- _seed_spec: spec does not exist → file is copied and committed
- plan_milestones command: --version required when --spec not given
- plan_milestones command: --version inferred from spec filename stem when --spec given
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from composer.cli import _seed_spec, _validate_spec_path, plan_milestones

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
    "GITHUB_TOKEN": "ghp-test-token",
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


# ---------------------------------------------------------------------------
# _seed_spec
# ---------------------------------------------------------------------------


class TestSeedSpec:
    def _init_git_repo(self, path: Path) -> None:
        """Initialise a bare-minimum git repo at *path*."""
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "test@test.com"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "Test"],
            check=True,
            capture_output=True,
        )
        # Initial commit so there is a HEAD
        readme = path / "README.md"
        readme.write_text("hello")
        subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(path), "commit", "-m", "init"],
            check=True,
            capture_output=True,
        )

    def test_version_inferred_from_filename_stem(self, tmp_path: Path) -> None:
        """_seed_spec uses the spec filename stem as the destination filename."""
        repo = tmp_path / "target_repo"
        repo.mkdir()
        self._init_git_repo(repo)

        spec = tmp_path / "calculator.md"
        spec.write_text("# Calculator Spec")

        _seed_spec(spec, "calculator", str(repo))

        dest = repo / "docs" / "specs" / "calculator.md"
        assert dest.exists()
        assert dest.read_text() == "# Calculator Spec"

    def test_spec_already_exists_prints_warning_no_overwrite(self, tmp_path: Path, capsys) -> None:
        """When docs/specs/<version>.md exists, print warning and skip copy."""
        repo = tmp_path / "target_repo"
        repo.mkdir()
        self._init_git_repo(repo)

        # Pre-create the destination
        dest_dir = repo / "docs" / "specs"
        dest_dir.mkdir(parents=True)
        dest = dest_dir / "calculator.md"
        original_content = "# Original Spec"
        dest.write_text(original_content)

        spec = tmp_path / "calculator.md"
        spec.write_text("# New Spec")

        with patch("click.echo") as mock_echo:
            _seed_spec(spec, "calculator", str(repo))

        # File must not be overwritten
        assert dest.read_text() == original_content
        # Warning was printed
        warning_calls = [str(c) for c in mock_echo.call_args_list]
        assert any("already exists" in c or "Warning" in c for c in warning_calls)

    def test_spec_does_not_exist_copies_and_commits(self, tmp_path: Path) -> None:
        """When the destination does not exist, the file is copied and committed."""
        repo = tmp_path / "target_repo"
        repo.mkdir()
        self._init_git_repo(repo)

        spec = tmp_path / "myversion.md"
        spec.write_text("# My Version Spec")

        _seed_spec(spec, "myversion", str(repo))

        dest = repo / "docs" / "specs" / "myversion.md"
        assert dest.exists()
        assert dest.read_text() == "# My Version Spec"

        # Verify the file was committed (it should appear in git log)
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "--oneline"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "seed spec" in log.stdout

    def test_docs_specs_dir_created_if_missing(self, tmp_path: Path) -> None:
        """_seed_spec creates docs/specs/ when it does not exist."""
        repo = tmp_path / "target_repo"
        repo.mkdir()
        self._init_git_repo(repo)

        spec = tmp_path / "v2.md"
        spec.write_text("# v2 Spec")

        assert not (repo / "docs" / "specs").exists()
        _seed_spec(spec, "v2", str(repo))
        assert (repo / "docs" / "specs" / "v2.md").exists()

    def test_git_add_and_commit_called_with_correct_args(self, tmp_path: Path) -> None:
        """_seed_spec calls git add and git commit with expected arguments."""
        repo = tmp_path / "target_repo"
        repo.mkdir()
        self._init_git_repo(repo)

        spec = tmp_path / "myspec.md"
        spec.write_text("# Spec")

        with patch("composer.cli.subprocess.run", wraps=subprocess.run) as mock_run:
            _seed_spec(spec, "myspec", str(repo))

        calls = [c for c in mock_run.call_args_list]
        # Each call's first positional arg is the command list, e.g. ["git", "-C", path, "add", ...]
        cmd_lists = [c.args[0] for c in calls]
        assert any("add" in cmd for cmd in cmd_lists)
        assert any("commit" in cmd for cmd in cmd_lists)


# ---------------------------------------------------------------------------
# plan_milestones Click command
# ---------------------------------------------------------------------------


class TestPlanMilestonesCommand:
    def test_missing_version_and_spec_fails(self) -> None:
        """plan-milestones fails with a clear error when neither --version nor --spec is given."""
        runner = CliRunner()
        result = runner.invoke(plan_milestones, ["--repo", "owner/repo"])
        assert result.exit_code != 0
        output = result.output
        assert "--version" in output or "version" in output.lower() or "Error" in output

    def test_version_inferred_from_spec_filename(self, tmp_path: Path) -> None:
        """When --spec is given and --version is omitted, version is inferred from stem."""
        spec_file = tmp_path / "calculator.md"
        spec_file.write_text("# Spec")

        runner = CliRunner()
        with (
            patch("composer.cli.load_config") as mock_load_config,
            patch("composer.cli.startup_sequence") as mock_startup,
            patch("composer.cli._seed_spec") as mock_seed,
            patch("composer.cli._run_plan_milestones") as mock_run,
            patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
        ):
            mock_config = MagicMock()
            mock_config.checkpoint_dir = tmp_path
            mock_load_config.return_value = mock_config
            mock_startup.return_value = (mock_config, MagicMock())
            mock_run.return_value = None
            mock_seed.return_value = None

            result = runner.invoke(
                plan_milestones,
                ["--repo", "owner/repo", "--spec", str(spec_file)],
            )

        assert result.exit_code == 0, result.output
        # The inferred version should be "calculator"
        run_call_kwargs = mock_run.call_args.kwargs
        assert run_call_kwargs["version"] == "calculator"

    def test_explicit_version_overrides_spec_stem(self, tmp_path: Path) -> None:
        """When both --spec and --version are given, the explicit --version is used."""
        spec_file = tmp_path / "calculator.md"
        spec_file.write_text("# Spec")

        runner = CliRunner()
        with (
            patch("composer.cli.load_config") as mock_load_config,
            patch("composer.cli.startup_sequence") as mock_startup,
            patch("composer.cli._seed_spec") as mock_seed,
            patch("composer.cli._run_plan_milestones") as mock_run,
            patch("composer.cli._resolve_repo", return_value=("owner/repo", str(tmp_path))),
        ):
            mock_config = MagicMock()
            mock_config.checkpoint_dir = tmp_path
            mock_load_config.return_value = mock_config
            mock_startup.return_value = (mock_config, MagicMock())
            mock_run.return_value = None
            mock_seed.return_value = None

            result = runner.invoke(
                plan_milestones,
                [
                    "--repo",
                    "owner/repo",
                    "--spec",
                    str(spec_file),
                    "--version",
                    "MVP",
                ],
            )

        assert result.exit_code == 0, result.output
        run_call_kwargs = mock_run.call_args.kwargs
        assert run_call_kwargs["version"] == "MVP"

    def test_spec_with_remote_only_repo_fails(self, tmp_path: Path) -> None:
        """--spec requires a local repo path; remote-only ref raises ClickException."""
        spec_file = tmp_path / "calc.md"
        spec_file.write_text("# Spec")

        runner = CliRunner()
        with (
            patch("composer.cli.load_config") as mock_load_config,
            patch("composer.cli.startup_sequence") as mock_startup,
            patch("composer.cli._run_plan_milestones") as mock_run,
            # _resolve_repo returns (remote_ref, None) for a GitHub slug
            patch(
                "composer.cli._resolve_repo",
                return_value=("owner/repo", None),
            ),
        ):
            mock_config = MagicMock()
            mock_config.checkpoint_dir = tmp_path
            mock_load_config.return_value = mock_config
            mock_startup.return_value = (mock_config, MagicMock())
            mock_run.return_value = None

            result = runner.invoke(
                plan_milestones,
                ["--repo", "owner/repo", "--spec", str(spec_file), "--version", "calc"],
            )

        assert result.exit_code != 0
        assert "local" in result.output.lower() or "Error" in result.output
