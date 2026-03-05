"""Unit tests for the two-phase design-worker and its Click commands.

Tests cover:
- _run_design_worker: Gate 1 aborts when open research issues remain
- _run_design_worker: dry-run prints info without executing
- _run_design_worker: Phase 1 skipped when HLD already merged (recovery)
- _run_design_worker: Phase 1 dispatches HLD agent and merges PR
- _run_design_worker: Phase 1 aborts on HLD agent error
- _run_design_worker: Gate 2 aborts when HLD not on default branch after Phase 1 skip
- _run_design_worker: Phase 2 dispatches LLD agents in parallel
- _run_design_worker: Phase 2 skips already-merged LLD docs (recovery)
- _run_design_worker: Phase 2 aborts when no LLD issues found
- _run_plan_issues: dry-run prints info without calling runner.run
- _run_plan_issues: dispatches runner.run with correct tools and logs events
- _run_plan_issues: handles runner error result gracefully
- design_worker Click command: requires --repo and --milestone
- plan_issues Click command: requires --repo and --milestone
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import click
import pytest

from brimstone.cli import _run_design_worker, _run_plan_issues
from brimstone.config import Config
from brimstone.runner import RunResult
from brimstone.session import Checkpoint

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_ensure_worktree_repo(tmp_path):
    """Patch _ensure_worktree_repo to return a tmp dir without cloning."""
    with patch(
        "brimstone.cli._ensure_worktree_repo",
        return_value=(str(tmp_path), None),
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
    "BRIMSTONE_GH_TOKEN": "ghp-test-token",
}


def make_config(tmp_path: Path, **overrides) -> Config:
    """Return a minimal Config instance with tmp_path-based dirs."""
    with patch.dict("os.environ", MINIMAL_ENV, clear=False):
        config = Config(
            anthropic_api_key=MINIMAL_ENV["ANTHROPIC_API_KEY"],
            github_token=MINIMAL_ENV["BRIMSTONE_GH_TOKEN"],
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


def make_design_issue(number: int, title: str) -> dict:
    return {
        "number": number,
        "title": title,
        "body": "",
        "labels": [{"name": "stage/design"}],
        "assignees": [],
    }


# ---------------------------------------------------------------------------
# Shared patch context for _run_design_worker
# ---------------------------------------------------------------------------

_DESIGN_PATCHES = {
    "count_research": "brimstone.cli._count_open_issues_by_label",
    "list_research": "brimstone.cli._list_all_open_issues_by_label",
    "classify_blocking": "brimstone.cli._classify_blocking_issues",
    "default_branch": "brimstone.cli._get_default_branch_for_repo",
    "repo_root": "brimstone.cli._get_repo_root",
    "doc_exists": "brimstone.cli._doc_exists_on_default_branch",
    "list_design": "brimstone.cli._list_open_issues_by_label",
    "file_design": "brimstone.cli._file_design_issue_if_missing",
    "claim": "brimstone.cli._claim_issue",
    "unclaim": "brimstone.cli._unclaim_issue",
    "create_wt": "brimstone.cli._create_worktree",
    "remove_wt": "brimstone.cli._remove_worktree",
    "dispatch_agent": "brimstone.cli._dispatch_design_agent",
    "find_pr": "brimstone.cli._find_pr_for_branch",
    "monitor_pr": "brimstone.cli._monitor_pr",
    "log_event": "brimstone.cli.logger.log_conductor_event",
    "save_session": "brimstone.cli.session.save",
    "slugify": "brimstone.cli._slugify",
}


# ---------------------------------------------------------------------------
# _run_design_worker: Gate 1 — open research issues
# ---------------------------------------------------------------------------


class TestDesignWorkerGate1:
    def test_aborts_when_blocking_research_open(self, tmp_path: Path, capsys) -> None:
        """Gate fires when there are open research issues without [DEFERRED] tag."""
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()

        def _issue(n: int, body: str) -> dict:
            return {
                "number": n,
                "title": f"Research: {n}",
                "body": body,
                "labels": [],
                "assignees": [],
            }

        blocking_issues = [
            _issue(10, "no tag"),
            _issue(11, "[BLOCKS_IMPL]"),
            _issue(12, "also no tag"),
        ]

        with (
            patch(_DESIGN_PATCHES["list_research"], return_value=blocking_issues),
            patch(_DESIGN_PATCHES["log_event"]),
            patch(_DESIGN_PATCHES["save_session"]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_design_worker(
                    repo="owner/repo",
                    milestone="v1",
                    config=config,
                    checkpoint=checkpoint,
                    dry_run=False,
                )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "research" in captured.err.lower()
        assert "3" in captured.err

    def test_proceeds_when_only_deferred_research_remain(self, tmp_path: Path) -> None:
        """Gate passes when only [DEFERRED] issues remain open."""
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()

        v2_only = [
            {
                "number": 99,
                "title": "Research: deferred",
                "body": "[DEFERRED]",
                "labels": [],
                "assignees": [],
            },
        ]

        with (
            patch(_DESIGN_PATCHES["list_research"], return_value=v2_only),
            patch(_DESIGN_PATCHES["default_branch"], return_value="main"),
            patch(_DESIGN_PATCHES["repo_root"], return_value="/repo"),
            patch(_DESIGN_PATCHES["doc_exists"], return_value=True),  # HLD exists → skip Phase 1
            patch(_DESIGN_PATCHES["list_design"], return_value=[]),
            patch(_DESIGN_PATCHES["log_event"]),
            patch(_DESIGN_PATCHES["save_session"]),
        ):
            # No LLD issues → SystemExit(1), but Gate 1 passed
            with pytest.raises(SystemExit) as exc_info:
                _run_design_worker(
                    repo="owner/repo",
                    milestone="v1",
                    config=config,
                    checkpoint=checkpoint,
                    dry_run=False,
                )
        # Exits because no LLD issues, not because Gate 1
        assert exc_info.value.code == 1

    def test_proceeds_when_no_open_research(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()

        with (
            patch(_DESIGN_PATCHES["classify_blocking"], return_value=([], [])),
            patch(_DESIGN_PATCHES["default_branch"], return_value="main"),
            patch(_DESIGN_PATCHES["repo_root"], return_value="/repo"),
            patch(_DESIGN_PATCHES["doc_exists"], return_value=True),  # HLD exists → skip Phase 1
            patch(_DESIGN_PATCHES["list_design"], return_value=[]),
            patch(_DESIGN_PATCHES["log_event"]),
            patch(_DESIGN_PATCHES["save_session"]),
        ):
            # No LLD issues → SystemExit(1), but Gate 1 passed
            with pytest.raises(SystemExit) as exc_info:
                _run_design_worker(
                    repo="owner/repo",
                    milestone="v1",
                    config=config,
                    checkpoint=checkpoint,
                    dry_run=False,
                )
        # Exits because no LLD issues, not because Gate 1
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _run_design_worker: dry-run
# ---------------------------------------------------------------------------


class TestDesignWorkerDryRun:
    def test_dry_run_prints_info_without_executing(self, tmp_path: Path, capsys) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()

        with (
            patch(_DESIGN_PATCHES["dispatch_agent"]) as mock_dispatch,
            patch(_DESIGN_PATCHES["log_event"]),
            patch(_DESIGN_PATCHES["save_session"]),
        ):
            _run_design_worker(
                repo="owner/repo",
                milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=True,
            )
            mock_dispatch.assert_not_called()

        captured = capsys.readouterr()
        assert "[dry-run]" in captured.out
        assert "HLD" in captured.out
        assert "LLD" in captured.out
        assert "v1" in captured.out


# ---------------------------------------------------------------------------
# _run_design_worker: Phase 1 HLD
# ---------------------------------------------------------------------------


class TestDesignWorkerPhase1:
    def test_phase1_skipped_when_hld_exists(self, tmp_path: Path, capsys) -> None:
        """When HLD doc already exists on default branch, Phase 1 is skipped."""
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()

        def doc_exists(repo, path, branch):
            return True  # HLD exists, LLDs all exist too

        lld_issue = make_design_issue(20, "Design: LLD for parser")

        with (
            patch(_DESIGN_PATCHES["classify_blocking"], return_value=([], [])),
            patch(_DESIGN_PATCHES["default_branch"], return_value="main"),
            patch(_DESIGN_PATCHES["repo_root"], return_value="/repo"),
            patch(_DESIGN_PATCHES["doc_exists"], side_effect=doc_exists),
            patch(_DESIGN_PATCHES["list_design"], return_value=[lld_issue]),
            patch(_DESIGN_PATCHES["dispatch_agent"]) as mock_dispatch,
            patch(_DESIGN_PATCHES["claim"]),
            patch(_DESIGN_PATCHES["create_wt"], return_value="/wt/20"),
            patch(_DESIGN_PATCHES["find_pr"], return_value=5),
            patch(_DESIGN_PATCHES["monitor_pr"], return_value=True),
            patch(_DESIGN_PATCHES["remove_wt"]),
            patch(_DESIGN_PATCHES["log_event"]),
            patch(_DESIGN_PATCHES["save_session"]),
            patch(_DESIGN_PATCHES["slugify"], return_value="design-lld-parser"),
        ):
            mock_dispatch.return_value = (None, "", "", make_run_result())
            _run_design_worker(
                repo="owner/repo",
                milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        captured = capsys.readouterr()
        assert "skipping Phase 1" in captured.out

    def test_phase1_dispatches_hld_agent(self, tmp_path: Path) -> None:
        """Phase 1 dispatches the HLD agent when HLD doc is missing."""
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()
        hld_issue = make_design_issue(10, "Design: HLD for v1")
        lld_issue = make_design_issue(20, "Design: LLD for parser")

        hld_calls = [0]

        def doc_exists(repo, path, branch):
            if "HLD" in path:
                # First call: HLD missing (Phase 1 trigger)
                # Subsequent calls: HLD merged (Gate 2 + LLD skip check)
                hld_calls[0] += 1
                return hld_calls[0] > 1
            return True  # LLD docs present → Phase 2 skips all

        def list_design(repo, milestone):
            if milestone == "v1":
                return [lld_issue]
            return []

        with (
            patch(_DESIGN_PATCHES["classify_blocking"], return_value=([], [])),
            patch(_DESIGN_PATCHES["default_branch"], return_value="main"),
            patch(_DESIGN_PATCHES["repo_root"], return_value="/repo"),
            patch(_DESIGN_PATCHES["doc_exists"], side_effect=doc_exists),
            patch(_DESIGN_PATCHES["list_design"], side_effect=list_design),
            patch(_DESIGN_PATCHES["file_design"]),
            patch(_DESIGN_PATCHES["claim"]),
            patch(_DESIGN_PATCHES["create_wt"], return_value="/wt/10"),
            patch(_DESIGN_PATCHES["find_pr"], return_value=1),
            patch(_DESIGN_PATCHES["monitor_pr"], return_value=True),
            patch(_DESIGN_PATCHES["remove_wt"]),
            patch(
                _DESIGN_PATCHES["dispatch_agent"],
                return_value=(None, "", "", make_run_result()),
            ) as mock_da,
            patch(_DESIGN_PATCHES["log_event"]),
            patch(_DESIGN_PATCHES["save_session"]),
            patch(_DESIGN_PATCHES["slugify"], return_value="design-hld-v1"),
            # Gate 1 calls _list_open_issues_by_label for research before _classify_blocking_issues
            patch(
                "brimstone.cli._list_open_issues_by_label",
                side_effect=[
                    [],  # Gate 1: research check (classify_blocking patched → ([], []))
                    [hld_issue],  # Phase 1: finding HLD issue
                    [lld_issue],  # Phase 2: finding LLD issues
                ],
            ),
        ):
            _run_design_worker(
                repo="owner/repo",
                milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        # HLD agent was dispatched with the right skill
        hld_call = mock_da.call_args_list[0]
        assert hld_call.kwargs["skill_name"] == "design-worker-hld"
        assert hld_call.kwargs["module_name"] is None

    def test_phase1_aborts_on_hld_agent_error(self, tmp_path: Path, capsys) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()
        hld_issue = make_design_issue(10, "Design: HLD for v1")

        with (
            patch(_DESIGN_PATCHES["classify_blocking"], return_value=([], [])),
            patch(_DESIGN_PATCHES["default_branch"], return_value="main"),
            patch(_DESIGN_PATCHES["repo_root"], return_value="/repo"),
            patch(_DESIGN_PATCHES["doc_exists"], return_value=False),
            patch("brimstone.cli._list_open_issues_by_label", return_value=[hld_issue]),
            patch(_DESIGN_PATCHES["claim"]),
            patch(_DESIGN_PATCHES["unclaim"]),
            patch(_DESIGN_PATCHES["create_wt"], return_value="/wt/10"),
            patch(_DESIGN_PATCHES["remove_wt"]),
            patch(
                _DESIGN_PATCHES["dispatch_agent"],
                return_value=(
                    None,
                    "",
                    "",
                    make_run_result(is_error=True, subtype="error_timeout"),
                ),  # noqa: E501
            ),
            patch(_DESIGN_PATCHES["log_event"]),
            patch(_DESIGN_PATCHES["save_session"]),
            patch(_DESIGN_PATCHES["slugify"], return_value="design-hld-v1"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_design_worker(
                    repo="owner/repo",
                    milestone="v1",
                    config=config,
                    checkpoint=checkpoint,
                    dry_run=False,
                )
        assert exc_info.value.code == 1
        assert "HLD agent failed" in capsys.readouterr().err

    def test_gate2_aborts_when_hld_not_merged(self, tmp_path: Path, capsys) -> None:
        """Gate 2: if HLD skipped (e.g. issue was missing) but doc not on branch, abort."""
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()

        # Simulate: HLD was skipped (no issue found) but doc still not on branch
        with (
            patch(_DESIGN_PATCHES["classify_blocking"], return_value=([], [])),
            patch(_DESIGN_PATCHES["default_branch"], return_value="main"),
            patch(_DESIGN_PATCHES["repo_root"], return_value="/repo"),
            patch(_DESIGN_PATCHES["doc_exists"], return_value=False),
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch(_DESIGN_PATCHES["file_design"]),
            patch(_DESIGN_PATCHES["log_event"]),
            patch(_DESIGN_PATCHES["save_session"]),
            patch(_DESIGN_PATCHES["slugify"], return_value="slug"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_design_worker(
                    repo="owner/repo",
                    milestone="v1",
                    config=config,
                    checkpoint=checkpoint,
                    dry_run=False,
                )
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _run_design_worker: Phase 2 LLDs
# ---------------------------------------------------------------------------


class TestDesignWorkerPhase2:
    def _make_full_patches(self, hld_merged=True, lld_doc_exists=False, lld_issues=None):
        """Build a consistent set of patches for Phase 2 tests."""
        if lld_issues is None:
            lld_issues = [make_design_issue(20, "Design: LLD for parser")]

        def doc_exists(repo, path, branch):
            if "HLD" in path:
                return hld_merged
            return lld_doc_exists

        return {
            "count_research": (0,),
            "default_branch": ("main",),
            "repo_root": ("/repo",),
            "doc_exists_fn": doc_exists,
            "lld_issues": lld_issues,
        }

    def test_phase2_dispatches_lld_agents(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()

        lld_issues = [
            make_design_issue(20, "Design: LLD for parser"),
            make_design_issue(21, "Design: LLD for formatter"),
        ]

        def doc_exists(repo, path, branch):
            return "HLD" in path  # HLD present, no LLD docs yet

        with (
            patch(_DESIGN_PATCHES["classify_blocking"], return_value=([], [])),
            patch(_DESIGN_PATCHES["default_branch"], return_value="main"),
            patch(_DESIGN_PATCHES["repo_root"], return_value="/repo"),
            patch(_DESIGN_PATCHES["doc_exists"], side_effect=doc_exists),
            patch(
                "brimstone.cli._list_open_issues_by_label",
                return_value=lld_issues,
            ),
            patch(_DESIGN_PATCHES["claim"]),
            patch(
                _DESIGN_PATCHES["create_wt"],
                side_effect=["/wt/20", "/wt/21"],
            ),
            patch(
                _DESIGN_PATCHES["dispatch_agent"],
                return_value=(None, "", "", make_run_result()),
            ) as mock_da,
            patch(_DESIGN_PATCHES["find_pr"], return_value=5),
            patch(_DESIGN_PATCHES["monitor_pr"], return_value=True),
            patch(_DESIGN_PATCHES["remove_wt"]),
            patch(_DESIGN_PATCHES["log_event"]),
            patch(_DESIGN_PATCHES["save_session"]),
            patch(_DESIGN_PATCHES["slugify"], return_value="some-slug"),
        ):
            _run_design_worker(
                repo="owner/repo",
                milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        # Both LLD agents dispatched
        assert mock_da.call_count == 2
        skill_names = {c.kwargs["skill_name"] for c in mock_da.call_args_list}
        assert skill_names == {"design-worker-lld"}
        module_names = {c.kwargs["module_name"] for c in mock_da.call_args_list}
        assert "parser" in module_names
        assert "formatter" in module_names

    def test_phase2_skips_already_merged_ldds(self, tmp_path: Path, capsys) -> None:
        """Already-merged LLD docs are skipped and their issues closed."""
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()

        lld_issues = [
            make_design_issue(20, "Design: LLD for parser"),
            make_design_issue(21, "Design: LLD for formatter"),
        ]

        def doc_exists(repo, path, branch):
            # HLD and parser both exist; formatter does not
            return "HLD" in path or "parser" in path

        with (
            patch(_DESIGN_PATCHES["classify_blocking"], return_value=([], [])),
            patch(_DESIGN_PATCHES["default_branch"], return_value="main"),
            patch(_DESIGN_PATCHES["repo_root"], return_value="/repo"),
            patch(_DESIGN_PATCHES["doc_exists"], side_effect=doc_exists),
            patch("brimstone.cli._list_open_issues_by_label", return_value=lld_issues),
            patch(_DESIGN_PATCHES["claim"]),
            patch(_DESIGN_PATCHES["create_wt"], return_value="/wt/21"),
            patch(
                _DESIGN_PATCHES["dispatch_agent"],
                return_value=(None, "", "", make_run_result()),
            ) as mock_da,
            patch(_DESIGN_PATCHES["find_pr"], return_value=5),
            patch(_DESIGN_PATCHES["monitor_pr"], return_value=True),
            patch(_DESIGN_PATCHES["remove_wt"]),
            patch(_DESIGN_PATCHES["unclaim"]),
            patch(_DESIGN_PATCHES["log_event"]),
            patch(_DESIGN_PATCHES["save_session"]),
            patch(_DESIGN_PATCHES["slugify"], return_value="slug"),
            patch("brimstone.cli._gh"),  # swallow the issue-close call
        ):
            _run_design_worker(
                repo="owner/repo",
                milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        # Only formatter was dispatched
        assert mock_da.call_count == 1
        assert mock_da.call_args.kwargs["module_name"] == "formatter"
        captured = capsys.readouterr()
        assert "parser" in captured.out and "skipping" in captured.out

    def test_phase2_aborts_when_no_lld_issues(self, tmp_path: Path, capsys) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()

        with (
            patch(_DESIGN_PATCHES["classify_blocking"], return_value=([], [])),
            patch(_DESIGN_PATCHES["default_branch"], return_value="main"),
            patch(_DESIGN_PATCHES["repo_root"], return_value="/repo"),
            patch(_DESIGN_PATCHES["doc_exists"], return_value=True),  # HLD present
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch(_DESIGN_PATCHES["log_event"]),
            patch(_DESIGN_PATCHES["save_session"]),
        ):
            with pytest.raises(SystemExit) as exc_info:
                _run_design_worker(
                    repo="owner/repo",
                    milestone="v1",
                    config=config,
                    checkpoint=checkpoint,
                    dry_run=False,
                )
        assert exc_info.value.code == 1
        assert "lld" in capsys.readouterr().err.lower()

    def test_logs_start_and_complete_events(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint()
        lld_issue = make_design_issue(20, "Design: LLD for parser")
        logged_events: list[str] = []

        def record_event(**kwargs) -> None:
            logged_events.append(kwargs.get("event_type", ""))

        def doc_exists(repo, path, branch):
            return "HLD" in path

        with (
            patch(_DESIGN_PATCHES["classify_blocking"], return_value=([], [])),
            patch(_DESIGN_PATCHES["default_branch"], return_value="main"),
            patch(_DESIGN_PATCHES["repo_root"], return_value="/repo"),
            patch(_DESIGN_PATCHES["doc_exists"], side_effect=doc_exists),
            patch("brimstone.cli._list_open_issues_by_label", return_value=[lld_issue]),
            patch(_DESIGN_PATCHES["claim"]),
            patch(_DESIGN_PATCHES["create_wt"], return_value="/wt/20"),
            patch(
                _DESIGN_PATCHES["dispatch_agent"],
                return_value=(None, "", "", make_run_result()),
            ),
            patch(_DESIGN_PATCHES["find_pr"], return_value=5),
            patch(_DESIGN_PATCHES["monitor_pr"], return_value=True),
            patch(_DESIGN_PATCHES["remove_wt"]),
            patch(_DESIGN_PATCHES["log_event"], side_effect=record_event),
            patch(_DESIGN_PATCHES["save_session"]),
            patch(_DESIGN_PATCHES["slugify"], return_value="slug"),
        ):
            _run_design_worker(
                repo="owner/repo",
                milestone="v1",
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
            patch("brimstone.cli.runner.run") as mock_run,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            _run_plan_issues(
                repo="owner/repo",
                milestone="v1",
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
            patch("brimstone.cli.runner.run", return_value=run_result) as mock_run,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli._ensure_worktree_repo", return_value=("/fake/repo", "/fake/tmp")),
            patch("brimstone.cli.shutil.rmtree"),
        ):
            _run_plan_issues(
                repo="owner/repo",
                milestone="v1",
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
        assert "mcp__notion__API-post-page" not in allowed_tools
        # plan-issues only reads files and calls gh; must NOT use Write/Edit
        assert "Write" not in allowed_tools
        assert "Edit" not in allowed_tools

    def test_prompt_includes_milestone_and_repo(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="plan-issues")
        run_result = make_run_result()
        captured_prompt: list[str] = []

        def capture_run(prompt: str, **kwargs) -> RunResult:
            captured_prompt.append(prompt)
            return run_result

        with (
            patch("brimstone.cli.runner.run", side_effect=capture_run),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli._ensure_worktree_repo", return_value=("/fake/repo", "/fake/tmp")),
            patch("brimstone.cli.shutil.rmtree"),
        ):
            _run_plan_issues(
                repo="owner/repo",
                milestone="v1-impl",
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

    def test_dry_run_skips_runner_and_prints_milestone(self, tmp_path: Path, capsys) -> None:
        """When dry_run=True runner.run is not called and output mentions the milestone."""
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="plan-issues")

        with (
            patch("brimstone.cli.runner.run") as mock_run,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            _run_plan_issues(
                repo="owner/repo",
                milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=True,
            )
            mock_run.assert_not_called()

        out = capsys.readouterr().out
        assert "v1" in out
        assert "owner/repo" in out

    def test_error_result_raises_click_exception(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="plan-issues")
        run_result = make_run_result(
            is_error=True, subtype="error_max_turns", error_code="max_turns_exceeded"
        )

        with (
            patch("brimstone.cli.runner.run", return_value=run_result),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli._ensure_worktree_repo", return_value=("/fake/repo", "/fake/tmp")),
            patch("brimstone.cli.shutil.rmtree"),
            pytest.raises(click.ClickException) as exc_info,
        ):
            _run_plan_issues(
                repo="owner/repo",
                milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        assert "error_max_turns" in exc_info.value.format_message()

    def test_logs_start_and_complete_events(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        checkpoint = make_checkpoint(stage="plan-issues")
        run_result = make_run_result()
        logged_events: list[str] = []

        def record_event(**kwargs) -> None:
            logged_events.append(kwargs.get("event_type", ""))

        with (
            patch("brimstone.cli.runner.run", return_value=run_result),
            patch("brimstone.cli.logger.log_conductor_event", side_effect=record_event),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli._ensure_worktree_repo", return_value=("/fake/repo", "/fake/tmp")),
            patch("brimstone.cli.shutil.rmtree"),
        ):
            _run_plan_issues(
                repo="owner/repo",
                milestone="v1",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        assert "plan_issues_start" in logged_events
        assert "plan_issues_complete" in logged_events


# ---------------------------------------------------------------------------
# Click command: design_worker
# ---------------------------------------------------------------------------
