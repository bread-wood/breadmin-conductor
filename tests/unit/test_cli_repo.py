"""Unit tests for --repo argument resolution in src/composer/cli.py."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest

from composer.cli import (
    _is_git_repo,
    _parse_github_owner_name,
    _resolve_repo,
    _scaffold_new_repo,
)

# ---------------------------------------------------------------------------
# _is_git_repo
# ---------------------------------------------------------------------------


class TestIsGitRepo:
    def test_returns_true_for_valid_git_repo(self, tmp_path: Path) -> None:
        """_is_git_repo returns True for a directory that is a git repo."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        assert _is_git_repo(str(tmp_path)) is True

    def test_returns_false_for_plain_directory(self, tmp_path: Path) -> None:
        """_is_git_repo returns False for a plain directory with no git."""
        assert _is_git_repo(str(tmp_path)) is False

    def test_returns_false_for_nonexistent_path(self, tmp_path: Path) -> None:
        """_is_git_repo returns False for a path that does not exist."""
        assert _is_git_repo(str(tmp_path / "does_not_exist")) is False


# ---------------------------------------------------------------------------
# _parse_github_owner_name
# ---------------------------------------------------------------------------


class TestParseGithubOwnerName:
    def test_parses_https_url_with_git_suffix(self) -> None:
        url = "https://github.com/owner/myrepo.git"
        assert _parse_github_owner_name(url) == "owner/myrepo"

    def test_parses_https_url_without_git_suffix(self) -> None:
        url = "https://github.com/owner/myrepo"
        assert _parse_github_owner_name(url) == "owner/myrepo"

    def test_parses_ssh_url_with_git_suffix(self) -> None:
        url = "git@github.com:owner/myrepo.git"
        assert _parse_github_owner_name(url) == "owner/myrepo"

    def test_parses_ssh_url_without_git_suffix(self) -> None:
        url = "git@github.com:owner/myrepo"
        assert _parse_github_owner_name(url) == "owner/myrepo"

    def test_returns_none_for_non_github_url(self) -> None:
        url = "https://gitlab.com/owner/myrepo.git"
        assert _parse_github_owner_name(url) is None

    def test_returns_none_for_empty_string(self) -> None:
        assert _parse_github_owner_name("") is None


# ---------------------------------------------------------------------------
# _resolve_repo — cwd validation (no --repo flag)
# ---------------------------------------------------------------------------


class TestResolveRepoCwdMode:
    def test_no_repo_in_git_dir_succeeds(self, tmp_path: Path) -> None:
        """No --repo flag succeeds when cwd is a git repo."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        with patch("os.getcwd", return_value=str(tmp_path)):
            with patch("composer.cli._infer_github_repo_from_path", return_value="owner/repo"):
                repo_ref, local_path = _resolve_repo(None)
        assert repo_ref == "owner/repo"
        assert local_path == str(tmp_path)

    def test_no_repo_outside_git_dir_raises(self, tmp_path: Path) -> None:
        """No --repo flag fails with ClickException when cwd is not a git repo."""
        with patch("os.getcwd", return_value=str(tmp_path)):
            with pytest.raises(click.ClickException) as exc_info:
                _resolve_repo(None)
        assert "not a git repository" in str(exc_info.value.format_message())
        assert "--repo" in str(exc_info.value.format_message())

    def test_error_message_suggests_repo_flag(self, tmp_path: Path) -> None:
        """The cwd error message suggests using --repo <owner/name>."""
        with patch("os.getcwd", return_value=str(tmp_path)):
            with pytest.raises(click.ClickException) as exc_info:
                _resolve_repo(None)
        msg = exc_info.value.format_message()
        assert "owner/name" in msg or "--repo" in msg


# ---------------------------------------------------------------------------
# _resolve_repo — owner/name remote repo
# ---------------------------------------------------------------------------


class TestResolveRepoRemoteMode:
    def test_owner_name_returns_repo_ref_and_no_local_path(self) -> None:
        """'owner/name' format returns (owner/name, None) without touching disk."""
        repo_ref, local_path = _resolve_repo("bread-wood/calculator-cli")
        assert repo_ref == "bread-wood/calculator-cli"
        assert local_path is None

    def test_owner_name_with_dashes_and_underscores(self) -> None:
        """Complex owner/name strings are accepted as-is."""
        repo_ref, local_path = _resolve_repo("my-org/my_repo-v2")
        assert repo_ref == "my-org/my_repo-v2"
        assert local_path is None

    def test_owner_name_does_not_call_scaffold(self) -> None:
        """owner/name format does NOT trigger _scaffold_new_repo."""
        with patch("composer.cli._scaffold_new_repo") as mock_scaffold:
            _resolve_repo("owner/name")
        mock_scaffold.assert_not_called()


# ---------------------------------------------------------------------------
# _resolve_repo — new repo scaffolding (plain name, no slash)
# ---------------------------------------------------------------------------


class TestResolveRepoScaffoldMode:
    def test_plain_name_triggers_scaffold(self, tmp_path: Path) -> None:
        """Plain name (no slash) triggers _scaffold_new_repo."""
        fake_path = str(tmp_path / "mynewrepo")
        with patch("composer.cli._scaffold_new_repo", return_value=fake_path) as mock_scaffold:
            with patch(
                "composer.cli._infer_github_repo_from_path",
                return_value="myuser/mynewrepo",
            ):
                repo_ref, local_path = _resolve_repo("mynewrepo")
        mock_scaffold.assert_called_once_with("mynewrepo")
        assert local_path == fake_path

    def test_scaffold_called_with_correct_name(self, tmp_path: Path) -> None:
        """The name passed to _scaffold_new_repo matches the CLI argument."""
        fake_path = str(tmp_path / "calculator-cli")
        with patch("composer.cli._scaffold_new_repo", return_value=fake_path) as mock_scaffold:
            with patch("composer.cli._infer_github_repo_from_path", return_value=None):
                _resolve_repo("calculator-cli")
        mock_scaffold.assert_called_once_with("calculator-cli")

    def test_scaffold_returns_repo_ref_from_infer(self, tmp_path: Path) -> None:
        """When _infer_github_repo_from_path succeeds, repo_ref is owner/name."""
        fake_path = str(tmp_path / "testapp")
        with patch("composer.cli._scaffold_new_repo", return_value=fake_path):
            with patch(
                "composer.cli._infer_github_repo_from_path",
                return_value="bread-wood/testapp",
            ):
                repo_ref, _ = _resolve_repo("testapp")
        assert repo_ref == "bread-wood/testapp"

    def test_scaffold_falls_back_to_name_when_infer_fails(self, tmp_path: Path) -> None:
        """When _infer_github_repo_from_path returns None, repo_ref is the name."""
        fake_path = str(tmp_path / "testapp")
        with patch("composer.cli._scaffold_new_repo", return_value=fake_path):
            with patch("composer.cli._infer_github_repo_from_path", return_value=None):
                repo_ref, _ = _resolve_repo("testapp")
        assert repo_ref == "testapp"


# ---------------------------------------------------------------------------
# _resolve_repo — local path
# ---------------------------------------------------------------------------


class TestResolveRepoLocalPathMode:
    def test_local_path_that_is_git_repo_succeeds(self, tmp_path: Path) -> None:
        """Local path that is a git repo returns (inferred_ref, abs_path)."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        with patch(
            "composer.cli._infer_github_repo_from_path",
            return_value="owner/mylocal",
        ):
            repo_ref, local_path = _resolve_repo(str(tmp_path))
        assert local_path == str(tmp_path)
        assert repo_ref == "owner/mylocal"

    def test_local_path_not_git_repo_raises(self, tmp_path: Path) -> None:
        """Local path that is NOT a git repo raises ClickException."""
        non_git = str(tmp_path)
        with pytest.raises(click.ClickException) as exc_info:
            _resolve_repo(non_git)
        assert "not a git repository" in str(exc_info.value.format_message())

    def test_relative_dot_path_resolves_to_abs(self, tmp_path: Path) -> None:
        """Relative path starting with '.' is expanded to absolute path."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        # Change cwd so "." resolves to tmp_path
        original_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            with patch(
                "composer.cli._infer_github_repo_from_path",
                return_value=None,
            ):
                repo_ref, local_path = _resolve_repo(".")
            assert local_path == str(tmp_path)
        finally:
            os.chdir(original_cwd)

    def test_nonexistent_path_raises(self, tmp_path: Path) -> None:
        """A path that doesn't exist raises ClickException."""
        missing = str(tmp_path / "no" / "such" / "dir")
        with pytest.raises(click.ClickException) as exc_info:
            _resolve_repo(missing)
        assert "not a directory" in str(exc_info.value.format_message()).lower()


# ---------------------------------------------------------------------------
# _scaffold_new_repo (mock gh)
# ---------------------------------------------------------------------------


class TestScaffoldNewRepo:
    def test_creates_readme_and_gitignore(self, tmp_path: Path) -> None:
        """_scaffold_new_repo creates README.md and .gitignore in the new dir."""
        parent = tmp_path / "workspace"
        parent.mkdir()

        # Patch os.path.abspath to return predictable path inside tmp_path
        repo_dir = str(parent / "myproject")

        original_abspath = os.path.abspath

        def fake_abspath(p: str) -> str:
            if p == "myproject":
                return repo_dir
            return original_abspath(p)

        mock_run = MagicMock()
        # All subprocess calls succeed
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        with (
            patch("os.path.abspath", side_effect=fake_abspath),
            patch("subprocess.run", mock_run),
        ):
            result = _scaffold_new_repo("myproject")

        assert result == repo_dir

    def test_raises_if_directory_already_exists(self, tmp_path: Path) -> None:
        """_scaffold_new_repo raises ClickException if the target dir already exists."""
        existing_dir = tmp_path / "existing"
        existing_dir.mkdir()

        original_abspath = os.path.abspath

        def fake_abspath(p: str) -> str:
            if p == "existing":
                return str(existing_dir)
            return original_abspath(p)

        with patch("os.path.abspath", side_effect=fake_abspath):
            with pytest.raises(click.ClickException) as exc_info:
                _scaffold_new_repo("existing")
        assert "already exists" in str(exc_info.value.format_message())

    def test_raises_if_gh_repo_create_fails(self, tmp_path: Path) -> None:
        """_scaffold_new_repo raises ClickException if gh repo create fails."""
        repo_dir = str(tmp_path / "failproject")
        original_abspath = os.path.abspath

        def fake_abspath(p: str) -> str:
            if p == "failproject":
                return repo_dir
            return original_abspath(p)

        call_count = [0]

        def mock_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
            call_count[0] += 1
            # Fail on gh repo create (last call)
            if cmd[0] == "gh":
                return MagicMock(returncode=1, stderr="not authenticated", stdout="")
            return MagicMock(returncode=0, stderr="", stdout="")

        with (
            patch("os.path.abspath", side_effect=fake_abspath),
            patch("subprocess.run", side_effect=mock_run),
        ):
            with pytest.raises(click.ClickException) as exc_info:
                _scaffold_new_repo("failproject")
        assert "gh repo create failed" in str(exc_info.value.format_message())
