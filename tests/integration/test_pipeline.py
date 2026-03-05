"""Integration tests for the full pipeline CLI.

These tests invoke ``brimstone run`` via Click's CliRunner and mock the
entire worker functions.  The goal is to test the CLI orchestration layer:

- Stage ordering and gate enforcement
- ``_resolve_repo`` correctly chdir-ing into the target repo
- Config loading and milestone existence check
- ``--all``, ``--research``, ``--design``, ``--impl`` flag combinations

Worker internals (git operations, gh calls) are covered by the per-stage
integration tests; they are mocked here to keep the pipeline test focused.

NOTE on stage gates:
- ``--all`` skips gate checks (all stages run regardless)
- ``--design`` alone triggers the "research open?" gate
- ``--impl`` alone triggers the "HLD on branch?" gate
The completion-skip checks (already done) run for every --all invocation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from brimstone.cli import composer
from tests.integration.conftest import make_checkpoint

MINIMAL_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test",
    "BRIMSTONE_GH_TOKEN": "ghp-test",
}

_REPO = "owner/repo"


def _fake_startup(
    config: object,
    checkpoint_path: object,
    milestone: str,
    stage: str,
    skip_checks: object = None,
    resume_run_id: object = None,
) -> tuple:
    """Minimal startup_sequence replacement: skips health checks."""
    from pathlib import Path as _Path

    from brimstone.beads import BeadStore

    cfg = MagicMock()
    cfg.github_token = "ghp-test"
    cfg.log_dir = MagicMock()
    cfg.log_dir.expanduser.return_value = _Path("/tmp/brimstone-test-logs")
    chk = make_checkpoint(milestone=milestone, stage=stage)
    store = BeadStore(beads_dir=_Path("/tmp/brimstone-test-beads"))
    return cfg, chk, store


_COMMON_PATCHES = {
    "resolve_repo": "brimstone.cli._resolve_repo",
    "milestone_exists": "brimstone.cli._milestone_exists",
    "open_research": "brimstone.cli._count_open_issues_by_label",
    "doc_exists": "brimstone.cli._doc_exists_on_default_branch",
    "open_impl": "brimstone.cli._list_open_issues_by_label",
    "plan_issues": "brimstone.cli._run_plan_issues",
    "default_branch": "brimstone.cli._get_default_branch_for_repo",
    "startup": "brimstone.cli.startup_sequence",
    "research_worker": "brimstone.cli._run_research_worker",
    "design_worker": "brimstone.cli._run_design_worker",
    "impl_worker": "brimstone.cli._run_impl_worker",
}


class TestPipelineSingleStage:
    def test_research_flag_calls_only_research_worker(self, git_repo: Path, tmp_path: Path) -> None:
        cli_runner = CliRunner()
        called: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch(_COMMON_PATCHES["resolve_repo"], return_value=_REPO),
                patch(_COMMON_PATCHES["milestone_exists"], return_value=True),
                patch(_COMMON_PATCHES["open_research"], return_value=3),
                patch(_COMMON_PATCHES["default_branch"], return_value="mainline"),
                patch(_COMMON_PATCHES["startup"], side_effect=_fake_startup),
                patch(
                    _COMMON_PATCHES["research_worker"],
                    side_effect=lambda **kw: called.append("research"),
                ),
                patch(
                    _COMMON_PATCHES["design_worker"],
                    side_effect=lambda **kw: called.append("design"),
                ),
                patch(
                    _COMMON_PATCHES["impl_worker"],
                    side_effect=lambda **kw: called.append("impl"),
                ),
            ):
                result = cli_runner.invoke(
                    composer,
                    ["run", "--research", "--repo", "owner/repo", "--milestone", "v0.1.0"],
                )

        assert result.exit_code == 0, result.output
        assert called == ["research"]

    def test_design_flag_calls_only_design_worker(self, git_repo: Path, tmp_path: Path) -> None:
        cli_runner = CliRunner()
        called: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch(_COMMON_PATCHES["resolve_repo"], return_value=_REPO),
                patch(_COMMON_PATCHES["milestone_exists"], return_value=True),
                # Gate: research complete
                patch(_COMMON_PATCHES["open_research"], return_value=0),
                patch(_COMMON_PATCHES["doc_exists"], return_value=False),
                patch(_COMMON_PATCHES["default_branch"], return_value="mainline"),
                patch(_COMMON_PATCHES["startup"], side_effect=_fake_startup),
                patch(
                    _COMMON_PATCHES["research_worker"],
                    side_effect=lambda **kw: called.append("research"),
                ),
                patch(
                    _COMMON_PATCHES["design_worker"],
                    side_effect=lambda **kw: called.append("design"),
                ),
                patch(
                    _COMMON_PATCHES["impl_worker"],
                    side_effect=lambda **kw: called.append("impl"),
                ),
            ):
                result = cli_runner.invoke(
                    composer,
                    ["run", "--design", "--repo", "owner/repo", "--milestone", "v0.1.0"],
                )

        assert result.exit_code == 0, result.output
        assert called == ["design"]


class TestPipelineGateEnforcement:
    def test_design_gate_blocks_when_research_open(self, git_repo: Path, tmp_path: Path) -> None:
        """``--design`` alone is blocked when research issues are still open."""
        cli_runner = CliRunner()

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch(_COMMON_PATCHES["resolve_repo"], return_value=_REPO),
                patch(_COMMON_PATCHES["milestone_exists"], return_value=True),
                patch(_COMMON_PATCHES["open_research"], return_value=2),
                patch(_COMMON_PATCHES["default_branch"], return_value="mainline"),
            ):
                result = cli_runner.invoke(
                    composer,
                    ["run", "--design", "--repo", "owner/repo", "--milestone", "v0.1.0"],
                )

        assert result.exit_code != 0

    def test_impl_gate_blocks_when_hld_missing(self, git_repo: Path, tmp_path: Path) -> None:
        """``--impl`` alone is blocked when HLD doc is not on the default branch."""
        cli_runner = CliRunner()

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch(_COMMON_PATCHES["resolve_repo"], return_value=_REPO),
                patch(_COMMON_PATCHES["milestone_exists"], return_value=True),
                patch(_COMMON_PATCHES["doc_exists"], return_value=False),
                patch(_COMMON_PATCHES["default_branch"], return_value="mainline"),
            ):
                result = cli_runner.invoke(
                    composer,
                    ["run", "--impl", "--repo", "owner/repo", "--milestone", "v0.1.0"],
                )

        assert result.exit_code != 0

    def test_all_flag_bypasses_gates(self, git_repo: Path, tmp_path: Path) -> None:
        """``--all`` runs all stages; no gate checks fire even if prerequisites incomplete."""
        cli_runner = CliRunner()
        called: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch(_COMMON_PATCHES["resolve_repo"], return_value=_REPO),
                patch(_COMMON_PATCHES["milestone_exists"], return_value=True),
                # Research still open, HLD not on branch — but --all should bypass gates
                patch(_COMMON_PATCHES["open_research"], return_value=2),
                patch(_COMMON_PATCHES["doc_exists"], return_value=False),
                patch(_COMMON_PATCHES["open_impl"], return_value=[{"number": 1}]),
                patch(_COMMON_PATCHES["plan_issues"]),
                patch(_COMMON_PATCHES["default_branch"], return_value="mainline"),
                patch(_COMMON_PATCHES["startup"], side_effect=_fake_startup),
                patch(
                    _COMMON_PATCHES["research_worker"],
                    side_effect=lambda **kw: called.append("research"),
                ),
                patch(
                    _COMMON_PATCHES["design_worker"],
                    side_effect=lambda **kw: called.append("design"),
                ),
                patch(
                    _COMMON_PATCHES["impl_worker"],
                    side_effect=lambda **kw: called.append("impl"),
                ),
            ):
                result = cli_runner.invoke(
                    composer,
                    ["run", "--all", "--repo", "owner/repo", "--milestone", "v0.1.0"],
                )

        assert result.exit_code == 0, result.output
        assert "research" in called
        assert "design" in called
        assert "impl" in called


class TestPipelineAllStages:
    def test_stages_run_in_research_design_impl_order(self, git_repo: Path, tmp_path: Path) -> None:
        cli_runner = CliRunner()
        call_order: list[str] = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch(_COMMON_PATCHES["resolve_repo"], return_value=_REPO),
                patch(_COMMON_PATCHES["milestone_exists"], return_value=True),
                patch(_COMMON_PATCHES["open_research"], return_value=3),
                patch(_COMMON_PATCHES["doc_exists"], return_value=False),
                patch(_COMMON_PATCHES["open_impl"], return_value=[{"number": 1}]),
                patch(_COMMON_PATCHES["plan_issues"]),
                patch(_COMMON_PATCHES["default_branch"], return_value="mainline"),
                patch(_COMMON_PATCHES["startup"], side_effect=_fake_startup),
                patch(
                    _COMMON_PATCHES["research_worker"],
                    side_effect=lambda **kw: call_order.append("research"),
                ),
                patch(
                    _COMMON_PATCHES["design_worker"],
                    side_effect=lambda **kw: call_order.append("design"),
                ),
                patch(
                    _COMMON_PATCHES["impl_worker"],
                    side_effect=lambda **kw: call_order.append("impl"),
                ),
            ):
                result = cli_runner.invoke(
                    composer,
                    ["run", "--all", "--repo", "owner/repo", "--milestone", "v0.1.0"],
                )

        assert result.exit_code == 0, result.output
        assert call_order == ["research", "design", "impl"], (
            f"Expected research→design→impl, got: {call_order}"
        )

    def test_research_skipped_when_already_complete(self, git_repo: Path, tmp_path: Path) -> None:
        """Research stage is skipped when all research beads are closed."""
        cli_runner = CliRunner()
        called: list[str] = []
        mock_store = MagicMock()

        def _beads_for_stage(**kw: object) -> list:
            stage = kw.get("stage", "")
            if stage == "research":
                return [MagicMock(state="closed"), MagicMock(state="closed")]
            if stage == "impl":
                return [MagicMock(state="open")]  # so scope is skipped
            return []

        mock_store.list_work_beads.side_effect = _beads_for_stage

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch(_COMMON_PATCHES["resolve_repo"], return_value=_REPO),
                patch(_COMMON_PATCHES["milestone_exists"], return_value=True),
                patch(_COMMON_PATCHES["doc_exists"], return_value=False),
                patch(_COMMON_PATCHES["open_impl"], return_value=[{"number": 1}]),
                patch(_COMMON_PATCHES["plan_issues"]),
                patch(_COMMON_PATCHES["default_branch"], return_value="mainline"),
                patch(_COMMON_PATCHES["startup"], side_effect=_fake_startup),
                patch("brimstone.cli.make_bead_store", return_value=mock_store),
                patch(
                    _COMMON_PATCHES["research_worker"],
                    side_effect=lambda **kw: called.append("research"),
                ),
                patch(
                    _COMMON_PATCHES["design_worker"],
                    side_effect=lambda **kw: called.append("design"),
                ),
                patch(
                    _COMMON_PATCHES["impl_worker"],
                    side_effect=lambda **kw: called.append("impl"),
                ),
            ):
                result = cli_runner.invoke(
                    composer,
                    ["run", "--all", "--repo", "owner/repo", "--milestone", "v0.1.0"],
                )

        assert result.exit_code == 0, result.output
        assert "research" not in called, "Research must be skipped when already complete"
        assert "design" in called

    def test_design_skipped_when_hld_already_merged(self, git_repo: Path, tmp_path: Path) -> None:
        """Design stage is skipped when all design beads are closed and HLD exists."""
        cli_runner = CliRunner()
        called: list[str] = []
        mock_store = MagicMock()

        def _beads_for_stage(**kw: object) -> list:
            stage = kw.get("stage", "")
            if stage in ("research", "design"):
                return [MagicMock(state="closed"), MagicMock(state="closed")]
            return []

        mock_store.list_work_beads.side_effect = _beads_for_stage

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch(_COMMON_PATCHES["resolve_repo"], return_value=_REPO),
                patch(_COMMON_PATCHES["milestone_exists"], return_value=True),
                # HLD already merged
                patch(_COMMON_PATCHES["doc_exists"], return_value=True),
                patch(_COMMON_PATCHES["open_impl"], return_value=[{"number": 1}]),
                patch(_COMMON_PATCHES["plan_issues"]),
                patch(_COMMON_PATCHES["default_branch"], return_value="mainline"),
                patch(_COMMON_PATCHES["startup"], side_effect=_fake_startup),
                patch("brimstone.cli.make_bead_store", return_value=mock_store),
                patch(
                    _COMMON_PATCHES["research_worker"],
                    side_effect=lambda **kw: called.append("research"),
                ),
                patch(
                    _COMMON_PATCHES["design_worker"],
                    side_effect=lambda **kw: called.append("design"),
                ),
                patch(
                    _COMMON_PATCHES["impl_worker"],
                    side_effect=lambda **kw: called.append("impl"),
                ),
            ):
                result = cli_runner.invoke(
                    composer,
                    ["run", "--all", "--repo", "owner/repo", "--milestone", "v0.1.0"],
                )

        assert result.exit_code == 0, result.output
        assert "design" not in called, "Design must be skipped when HLD already merged"
        assert "impl" in called

    def test_missing_milestone_exits_nonzero(self, git_repo: Path, tmp_path: Path) -> None:
        cli_runner = CliRunner()

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch(_COMMON_PATCHES["resolve_repo"], return_value=_REPO),
                patch(_COMMON_PATCHES["milestone_exists"], return_value=False),
                patch(_COMMON_PATCHES["default_branch"], return_value="mainline"),
            ):
                result = cli_runner.invoke(
                    composer,
                    ["run", "--research", "--repo", "owner/repo", "--milestone", "v0.1.0"],
                )

        assert result.exit_code != 0


class TestPipelineRepoResolution:
    def test_resolve_repo_passes_repo_to_worker(self, git_repo: Path, tmp_path: Path) -> None:
        """_resolve_repo result is passed as repo= to the worker, not used for os.chdir."""
        cli_runner = CliRunner()
        repo_at_dispatch: list[str] = []

        def capture_repo(**kw: object) -> None:
            repo_at_dispatch.append(str(kw.get("repo", "")))

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch(_COMMON_PATCHES["resolve_repo"], return_value=_REPO),
                patch(_COMMON_PATCHES["milestone_exists"], return_value=True),
                patch(_COMMON_PATCHES["open_research"], return_value=3),
                patch(_COMMON_PATCHES["default_branch"], return_value="mainline"),
                patch(_COMMON_PATCHES["startup"], side_effect=_fake_startup),
                patch(_COMMON_PATCHES["research_worker"], side_effect=capture_repo),
            ):
                cli_runner.invoke(
                    composer,
                    ["run", "--research", "--repo", "owner/repo", "--milestone", "v0.1.0"],
                )

        assert repo_at_dispatch, "Research worker must have been invoked"
        assert repo_at_dispatch[0] == _REPO
