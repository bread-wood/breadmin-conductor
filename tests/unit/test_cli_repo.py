"""Unit tests for --repo argument resolution in src/brimstone/cli.py."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest

from brimstone.cli import (
    _ensure_remote,
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
            with patch("brimstone.cli._infer_github_repo_from_path", return_value="owner/repo"):
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
        with patch("brimstone.cli._scaffold_new_repo") as mock_scaffold:
            _resolve_repo("owner/name")
        mock_scaffold.assert_not_called()


# ---------------------------------------------------------------------------
# _resolve_repo — new repo scaffolding (plain name, no slash)
# ---------------------------------------------------------------------------


def _gh_not_found() -> MagicMock:
    """Fake subprocess result for 'gh repo view' when the repo does not exist."""
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    return m


def _gh_found(name_with_owner: str) -> MagicMock:
    """Fake subprocess result for 'gh repo view' when the repo exists."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = name_with_owner + "\n"
    return m


class TestResolveRepoScaffoldMode:
    def test_plain_name_triggers_scaffold_when_repo_not_on_github(self, tmp_path: Path) -> None:
        """Plain name (no slash) triggers _scaffold_new_repo when repo doesn't exist on GitHub."""
        fake_path = str(tmp_path / "mynewrepo")
        with patch("brimstone.cli.subprocess.run", return_value=_gh_not_found()):
            with patch("brimstone.cli._scaffold_new_repo", return_value=fake_path) as mock_scaffold:
                with patch(
                    "brimstone.cli._infer_github_repo_from_path",
                    return_value="myuser/mynewrepo",
                ):
                    repo_ref, local_path = _resolve_repo("mynewrepo")
        mock_scaffold.assert_called_once_with("mynewrepo")
        assert local_path == fake_path

    def test_plain_name_uses_existing_remote_repo(self) -> None:
        """Plain name returns (owner/name, None) when the repo already exists on GitHub."""
        with patch(
            "brimstone.cli.subprocess.run", return_value=_gh_found("bread-wood/calculator-cli")
        ):
            repo_ref, local_path = _resolve_repo("calculator-cli")
        assert repo_ref == "bread-wood/calculator-cli"
        assert local_path is None

    def test_plain_name_matches_cwd_repo_name_uses_cwd(self, tmp_path: Path) -> None:
        """Plain name matching cwd git repo's remote name uses cwd — no scaffold or gh call."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        # cwd remote is "bread-wood/calculator-cli"; repo_arg is "calculator-cli" (matches suffix).
        # We patch _infer_github_repo_from_path so the remote lookup returns a known value.
        # subprocess.run is NOT mocked — _is_git_repo must run for real against tmp_path.
        with (
            patch("os.getcwd", return_value=str(tmp_path)),
            patch(
                "brimstone.cli._infer_github_repo_from_path",
                return_value="bread-wood/calculator-cli",
            ),
            patch("brimstone.cli._scaffold_new_repo") as mock_scaffold,
        ):
            repo_ref, local_path = _resolve_repo("calculator-cli")
        mock_scaffold.assert_not_called()
        assert repo_ref == "bread-wood/calculator-cli"
        assert local_path == str(tmp_path)

    def test_scaffold_called_with_correct_name(self, tmp_path: Path) -> None:
        """The name passed to _scaffold_new_repo matches the CLI argument."""
        fake_path = str(tmp_path / "calculator-cli")
        with patch("brimstone.cli.subprocess.run", return_value=_gh_not_found()):
            with patch("brimstone.cli._scaffold_new_repo", return_value=fake_path) as mock_scaffold:
                with patch("brimstone.cli._infer_github_repo_from_path", return_value=None):
                    _resolve_repo("calculator-cli")
        mock_scaffold.assert_called_once_with("calculator-cli")

    def test_scaffold_returns_repo_ref_from_infer(self, tmp_path: Path) -> None:
        """When _infer_github_repo_from_path succeeds, repo_ref is owner/name."""
        fake_path = str(tmp_path / "testapp")
        with patch("brimstone.cli.subprocess.run", return_value=_gh_not_found()):
            with patch("brimstone.cli._scaffold_new_repo", return_value=fake_path):
                with patch(
                    "brimstone.cli._infer_github_repo_from_path",
                    return_value="bread-wood/testapp",
                ):
                    repo_ref, _ = _resolve_repo("testapp")
        assert repo_ref == "bread-wood/testapp"

    def test_scaffold_falls_back_to_name_when_infer_fails(self, tmp_path: Path) -> None:
        """When _infer_github_repo_from_path returns None, repo_ref is the name."""
        fake_path = str(tmp_path / "testapp")
        with patch("brimstone.cli.subprocess.run", return_value=_gh_not_found()):
            with patch("brimstone.cli._scaffold_new_repo", return_value=fake_path):
                with patch("brimstone.cli._infer_github_repo_from_path", return_value=None):
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
            "brimstone.cli._infer_github_repo_from_path",
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
                "brimstone.cli._infer_github_repo_from_path",
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

    def test_raises_if_directory_exists_and_is_not_git_repo(self, tmp_path: Path) -> None:
        """_scaffold_new_repo raises ClickException when the dir exists but is not a git repo."""
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
        assert "not a git repository" in str(exc_info.value.format_message())

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

    def test_directory_exists_as_git_repo_returns_path(self, tmp_path: Path) -> None:
        """If the dir already exists as a git repo, _scaffold_new_repo returns the path."""
        existing_dir = tmp_path / "myrepo"
        existing_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(existing_dir), check=True, capture_output=True)

        original_abspath = os.path.abspath

        def fake_abspath(p: str) -> str:
            if p == "myrepo":
                return str(existing_dir)
            return original_abspath(p)

        with (
            patch("os.path.abspath", side_effect=fake_abspath),
            patch("brimstone.cli._ensure_remote") as mock_ensure,
        ):
            result = _scaffold_new_repo("myrepo")

        assert result == str(existing_dir)
        mock_ensure.assert_called_once_with(str(existing_dir), "myrepo")

    def test_directory_exists_as_git_repo_no_remote_calls_ensure_remote(
        self, tmp_path: Path
    ) -> None:
        """If the dir exists as a git repo with no remote, _ensure_remote is called."""
        existing_dir = tmp_path / "myrepo"
        existing_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(existing_dir), check=True, capture_output=True)

        original_abspath = os.path.abspath

        def fake_abspath(p: str) -> str:
            if p == "myrepo":
                return str(existing_dir)
            return original_abspath(p)

        with (
            patch("os.path.abspath", side_effect=fake_abspath),
            patch("brimstone.cli._ensure_remote") as mock_ensure,
        ):
            _scaffold_new_repo("myrepo")

        mock_ensure.assert_called_once_with(str(existing_dir), "myrepo")

    def test_gh_repo_create_name_already_exists_no_exception(self, tmp_path: Path) -> None:
        """If gh repo create returns 'Name already exists', no exception is raised."""
        repo_dir = str(tmp_path / "alreadyrepo")
        original_abspath = os.path.abspath

        def fake_abspath(p: str) -> str:
            if p == "alreadyrepo":
                return repo_dir
            return original_abspath(p)

        def mock_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[0] == "gh" and "repo" in cmd and "create" in cmd:
                return MagicMock(
                    returncode=1,
                    stderr="GraphQL: Name already exists on this account (createRepository)",
                    stdout="",
                )
            return MagicMock(returncode=0, stderr="", stdout="")

        with (
            patch("os.path.abspath", side_effect=fake_abspath),
            patch("subprocess.run", side_effect=mock_run),
            patch("brimstone.cli._ensure_remote") as mock_ensure,
        ):
            result = _scaffold_new_repo("alreadyrepo")

        assert result == repo_dir
        mock_ensure.assert_called_once_with(repo_dir, "alreadyrepo")


# ---------------------------------------------------------------------------
# _ensure_remote
# ---------------------------------------------------------------------------


class TestEnsureRemote:
    def test_no_op_when_remote_already_configured(self, tmp_path: Path) -> None:
        """_ensure_remote does nothing when a remote already exists."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:owner/repo.git"],
            cwd=str(tmp_path),
            check=True,
            capture_output=True,
        )

        # Only git remote -v should be called; no gh or git remote add calls.
        original_run = subprocess.run
        calls: list[list[str]] = []

        def tracking_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(list(cmd))
            return original_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=tracking_run):
            _ensure_remote(str(tmp_path), "repo")

        # gh and git remote add should NOT have been called.
        assert not any(c[0] == "gh" for c in calls)
        assert not any("add" in c for c in calls)

    def test_adds_remote_when_none_configured(self, tmp_path: Path) -> None:
        """_ensure_remote adds the origin remote when none is configured."""
        subprocess.run(["git", "init"], cwd=str(tmp_path), check=True, capture_output=True)

        def mock_run(cmd: list, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[0] == "git" and "remote" in cmd and "-v" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            if cmd[0] == "gh":
                return MagicMock(
                    returncode=0, stdout="git@github.com:owner/myrepo.git\n", stderr=""
                )
            if cmd[0] == "git" and "add" in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=mock_run) as mock_sp:
            _ensure_remote(str(tmp_path), "myrepo")

        # Verify that git remote add was called with the SSH URL.
        add_calls = [
            call
            for call in mock_sp.call_args_list
            if call.args[0][0] == "git" and "add" in call.args[0]
        ]
        assert len(add_calls) == 1
        assert "git@github.com:owner/myrepo.git" in add_calls[0].args[0]
