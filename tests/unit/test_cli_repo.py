"""Unit tests for --repo argument resolution in src/brimstone/cli.py."""

from __future__ import annotations

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
                repo_ref = _resolve_repo(None)
        assert repo_ref == "owner/repo"

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
    def test_owner_name_returns_repo_ref(self) -> None:
        """'owner/name' format returns the string without touching disk."""
        repo_ref = _resolve_repo("bread-wood/calculator-cli")
        assert repo_ref == "bread-wood/calculator-cli"

    def test_owner_name_with_dashes_and_underscores(self) -> None:
        """Complex owner/name strings are accepted as-is."""
        repo_ref = _resolve_repo("my-org/my_repo-v2")
        assert repo_ref == "my-org/my_repo-v2"


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


class TestResolveRepoBareNameMode:
    def test_plain_name_found_on_github_returns_owner_name(self) -> None:
        """Plain name (no slash) returns 'owner/name' when the repo exists on GitHub."""
        with patch(
            "brimstone.cli.subprocess.run", return_value=_gh_found("bread-wood/calculator-cli")
        ):
            repo_ref = _resolve_repo("calculator-cli")
        assert repo_ref == "bread-wood/calculator-cli"

    def test_plain_name_not_found_raises(self) -> None:
        """Plain name raises ClickException when gh repo view finds nothing."""
        with patch("brimstone.cli.subprocess.run", return_value=_gh_not_found()):
            with pytest.raises(click.ClickException) as exc_info:
                _resolve_repo("no-such-repo")
        assert "owner/name" in str(exc_info.value.format_message())


# ---------------------------------------------------------------------------
# _resolve_repo — local path rejected
# ---------------------------------------------------------------------------


class TestResolveRepoLocalPathRejected:
    def test_absolute_path_raises(self, tmp_path: Path) -> None:
        """Absolute paths are rejected with ClickException."""
        with pytest.raises(click.ClickException) as exc_info:
            _resolve_repo(str(tmp_path))
        assert "owner/name" in str(exc_info.value.format_message())

    def test_relative_dot_path_raises(self) -> None:
        """Relative paths starting with '.' are rejected with ClickException."""
        with pytest.raises(click.ClickException) as exc_info:
            _resolve_repo("./myrepo")
        assert "owner/name" in str(exc_info.value.format_message())


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
