"""Integration tests for ``gh`` CLI command construction.

These tests mock ``_gh()`` at the function level and assert the exact
arguments that reach it.  This catches flag-placement bugs (e.g. passing
``repo=`` as a keyword that generates ``--repo`` in the wrong position)
without requiring a real GitHub token.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from brimstone.cli import _get_default_branch_for_repo


def _mock_gh(returncode: int = 0, stdout: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    return m


class TestGetDefaultBranchForRepo:
    def test_repo_passed_as_positional_arg_not_repo_kwarg(self) -> None:
        """``gh repo view`` takes the repo as a positional arg.

        Regression: previously called ``_gh([\"repo\", \"view\", ...], repo=repo)``
        which generated ``gh --repo owner/repo repo view ...`` — an invalid command
        because ``--repo`` is not a global flag for ``gh repo view``.
        """
        captured: list[tuple[list[str], str | None]] = []

        def fake_gh(args: list[str], *, repo: str | None = None, check: bool = True) -> MagicMock:
            captured.append((list(args), repo))
            return _mock_gh(stdout="mainline\n")

        with patch("brimstone.cli._gh", side_effect=fake_gh):
            result = _get_default_branch_for_repo("owner/repo")

        assert result == "mainline"
        assert len(captured) == 1, "Expected exactly one _gh call"
        args, repo_kwarg = captured[0]
        assert repo_kwarg is None, (
            "_gh must NOT receive repo= kwarg for 'repo view' "
            "(that generates a --repo flag before the subcommand)"
        )
        assert "owner/repo" in args, "Repo must appear as a positional element in args"

    def test_returns_stdout_stripped(self) -> None:
        with patch("brimstone.cli._gh", return_value=_mock_gh(stdout="mainline\n")):
            assert _get_default_branch_for_repo("owner/repo") == "mainline"

    def test_falls_back_to_main_on_nonzero_exit(self) -> None:
        with patch("brimstone.cli._gh", return_value=_mock_gh(returncode=1, stdout="")):
            assert _get_default_branch_for_repo("owner/repo") == "main"

    def test_falls_back_to_main_on_empty_stdout(self) -> None:
        with patch("brimstone.cli._gh", return_value=_mock_gh(returncode=0, stdout="   \n")):
            assert _get_default_branch_for_repo("owner/repo") == "main"

    def test_passes_correct_json_flags(self) -> None:
        """Verify the exact JSON extraction flags are present in the command."""
        captured: list[list[str]] = []

        def fake_gh(args: list[str], *, repo: str | None = None, check: bool = True) -> MagicMock:
            captured.append(list(args))
            return _mock_gh(stdout="mainline\n")

        with patch("brimstone.cli._gh", side_effect=fake_gh):
            _get_default_branch_for_repo("owner/repo")

        args = captured[0]
        assert "--json" in args
        assert "defaultBranchRef" in args
        assert "--jq" in args
        assert ".defaultBranchRef.name" in args
