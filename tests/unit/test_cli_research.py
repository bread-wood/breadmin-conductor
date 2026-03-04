"""Unit tests for the research-worker loop in src/brimstone/cli.py.

Tests cover:
- Issue selection filters (label, milestone, no assignee, not in-progress)
- Completion gate logic (zero blocking → stop)
- Rate-limit requeue (record_429 called, issue unclaimed)
- Resume: stale in-progress issues with no PR are unclaimed for re-dispatch
- All subprocess and GitHub API calls are mocked
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brimstone.cli import (
    RESEARCH_LABEL,
    UsageGovernor,
    _classify_blocking_issues,
    _filter_unblocked,
    _list_open_issues_by_label,
    _parse_dependencies,
    _run_completion_gate,
    _run_research_worker,
    _sanitize_issue_body,
    _sort_issues,
)
from brimstone.config import Config
from brimstone.runner import RunResult
from brimstone.session import Checkpoint

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_milestone_exists():
    """Patch _milestone_exists to return True so tests don't hit GitHub."""
    with patch("brimstone.cli._milestone_exists", return_value=True):
        yield


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


def make_config(**overrides) -> Config:
    """Return a minimal Config instance."""
    with patch.dict("os.environ", MINIMAL_ENV, clear=False):
        config = Config(
            anthropic_api_key=MINIMAL_ENV["ANTHROPIC_API_KEY"],
            github_token=MINIMAL_ENV["BRIMSTONE_GH_TOKEN"],
        )
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
        milestone="MVP Research",
        stage="research",
        timestamp="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Checkpoint(**defaults)


def make_run_result(
    *,
    is_error: bool = False,
    subtype: str | None = "success",
    error_code: str | None = None,
    result_text: str = "",
) -> RunResult:
    """Return a RunResult with the given classification."""
    return RunResult(
        is_error=is_error,
        subtype=subtype,
        error_code=error_code,
        exit_code=1 if is_error else 0,
        total_cost_usd=0.01 if not is_error else None,
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        raw_result_event={"result": result_text, "subtype": subtype} if result_text else None,
        stderr="",
        overage_detected=False,
    )


def make_issue(number: int, title: str = "Test issue", body: str = "", **extra) -> dict:
    """Return a minimal GitHub issue dict."""
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": "research"}],
        "assignees": [],
        "milestone": {"title": "MVP Research"},
        **extra,
    }


def make_gh_result(stdout: str = "", returncode: int = 0) -> MagicMock:
    """Return a mock subprocess.CompletedProcess."""
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = ""
    return result


# ---------------------------------------------------------------------------
# _sanitize_issue_body
# ---------------------------------------------------------------------------


class TestSanitizeIssueBody:
    def test_removes_backticks(self) -> None:
        assert "`" not in _sanitize_issue_body("Use `code` here")

    def test_removes_backslashes(self) -> None:
        assert "\\" not in _sanitize_issue_body("path\\to\\file")

    def test_replaces_dollar_paren(self) -> None:
        result = _sanitize_issue_body("Run $(echo hello)")
        assert "$(" not in result
        assert "(echo hello)" in result

    def test_truncates_at_max_chars(self) -> None:
        long_text = "a" * 20_000
        result = _sanitize_issue_body(long_text, max_chars=16_000)
        assert len(result) > 16_000  # includes truncation marker
        assert "TRUNCATED" in result
        assert result.startswith("a" * 16_000)

    def test_no_truncation_below_limit(self) -> None:
        text = "short text"
        result = _sanitize_issue_body(text)
        assert result == text
        assert "TRUNCATED" not in result

    def test_empty_string(self) -> None:
        assert _sanitize_issue_body("") == ""


# ---------------------------------------------------------------------------
# _parse_dependencies
# ---------------------------------------------------------------------------


class TestParseDependencies:
    def test_parses_single_dependency(self) -> None:
        body = "Depends on: #42"
        assert _parse_dependencies(body) == [42]

    def test_parses_multiple_dependencies(self) -> None:
        body = "Depends on: #10, #20, #30"
        assert _parse_dependencies(body) == [10, 20, 30]

    def test_case_insensitive(self) -> None:
        body = "depends on: #5"
        assert _parse_dependencies(body) == [5]

    def test_no_dependencies(self) -> None:
        body = "This issue has no dependencies."
        assert _parse_dependencies(body) == []

    def test_ignores_non_dependency_issue_refs(self) -> None:
        body = "See issue #99 for context. Depends on: #10"
        result = _parse_dependencies(body)
        assert 10 in result
        assert 99 not in result


# ---------------------------------------------------------------------------
# _filter_unblocked
# ---------------------------------------------------------------------------


class TestFilterUnblocked:
    def test_returns_all_when_no_dependencies(self) -> None:
        issues = [make_issue(1), make_issue(2)]
        result = _filter_unblocked(issues, open_issue_numbers={1, 2})
        assert len(result) == 2

    def test_excludes_issue_with_open_dependency(self) -> None:
        issues = [
            make_issue(1, body="Depends on: #2"),
            make_issue(2),
        ]
        result = _filter_unblocked(issues, open_issue_numbers={1, 2})
        # Issue 1 depends on 2 which is still open
        assert len(result) == 1
        assert result[0]["number"] == 2

    def test_includes_issue_with_closed_dependency(self) -> None:
        issues = [make_issue(1, body="Depends on: #5")]
        # 5 is not in open_issue_numbers → dependency is closed
        result = _filter_unblocked(issues, open_issue_numbers={1})
        assert len(result) == 1

    def test_empty_input(self) -> None:
        assert _filter_unblocked([], open_issue_numbers=set()) == []


# ---------------------------------------------------------------------------
# _sort_issues
# ---------------------------------------------------------------------------


class TestSortIssues:
    def test_sorted_by_number_ascending(self) -> None:
        issues = [make_issue(30), make_issue(5), make_issue(10)]
        result = _sort_issues(issues)
        assert [i["number"] for i in result] == [5, 10, 30]

    def test_stable_for_equal_numbers(self) -> None:
        issues = [make_issue(1, title="A"), make_issue(1, title="B")]
        result = _sort_issues(issues)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _list_open_issues_by_label (research)
# ---------------------------------------------------------------------------


class TestListOpenResearchIssues:
    def test_filters_assigned_issues(self) -> None:
        """Issues with assignees should be excluded."""
        issues = [
            {
                "number": 1,
                "title": "Open issue",
                "body": "",
                "labels": [{"name": "research"}],
                "assignees": [],
                "milestone": {"title": "MVP Research"},
            },
            {
                "number": 2,
                "title": "Assigned issue",
                "body": "",
                "labels": [{"name": "research"}],
                "assignees": [{"login": "someone"}],
                "milestone": {"title": "MVP Research"},
            },
        ]
        gh_output = json.dumps(issues)

        with patch("brimstone.cli.subprocess.run", return_value=make_gh_result(stdout=gh_output)):
            result = _list_open_issues_by_label("owner/repo", "MVP Research", RESEARCH_LABEL)

        assert len(result) == 1
        assert result[0]["number"] == 1

    def test_filters_in_progress_issues(self) -> None:
        """Issues with in-progress label should be excluded."""
        issues = [
            {
                "number": 3,
                "title": "In-progress issue",
                "body": "",
                "labels": [{"name": "research"}, {"name": "in-progress"}],
                "assignees": [],
                "milestone": {"title": "MVP Research"},
            },
            {
                "number": 4,
                "title": "Available issue",
                "body": "",
                "labels": [{"name": "research"}],
                "assignees": [],
                "milestone": {"title": "MVP Research"},
            },
        ]
        gh_output = json.dumps(issues)

        with patch("brimstone.cli.subprocess.run", return_value=make_gh_result(stdout=gh_output)):
            result = _list_open_issues_by_label("owner/repo", "MVP Research", RESEARCH_LABEL)

        assert len(result) == 1
        assert result[0]["number"] == 4

    def test_returns_empty_on_gh_failure(self) -> None:
        with patch("brimstone.cli.subprocess.run", return_value=make_gh_result(returncode=1)):
            result = _list_open_issues_by_label("owner/repo", "MVP Research", RESEARCH_LABEL)
        assert result == []

    def test_returns_empty_on_invalid_json(self) -> None:
        with patch("brimstone.cli.subprocess.run", return_value=make_gh_result(stdout="not json")):
            result = _list_open_issues_by_label("owner/repo", "MVP Research", RESEARCH_LABEL)
        assert result == []


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _classify_blocking_issues
# ---------------------------------------------------------------------------


class TestClassifyBlockingIssues:
    def test_no_tag_is_blocking_by_default(self) -> None:
        """Issues without [DEFERRED] are blocking by default."""
        config = make_config()
        checkpoint = make_checkpoint()
        issues = [make_issue(1, body="Is X secure?")]

        blocking, non_blocking = _classify_blocking_issues(
            issues, "owner/repo", "MVP Research", config, checkpoint
        )

        assert len(blocking) == 1
        assert len(non_blocking) == 0
        assert blocking[0]["number"] == 1

    def test_blocks_impl_tag_is_blocking(self) -> None:
        """Issues with [BLOCKS_IMPL] are blocking (conservative default still applies)."""
        config = make_config()
        checkpoint = make_checkpoint()
        issues = [make_issue(2, body="[BLOCKS_IMPL] — critical design question")]

        blocking, non_blocking = _classify_blocking_issues(
            issues, "owner/repo", "MVP Research", config, checkpoint
        )

        assert len(blocking) == 1
        assert len(non_blocking) == 0

    def test_deferred_tag_is_non_blocking(self) -> None:
        """Issues tagged [DEFERRED] are explicitly non-blocking."""
        config = make_config()
        checkpoint = make_checkpoint()
        issues = [make_issue(3, body="[DEFERRED] — nice to know but not urgent")]

        blocking, non_blocking = _classify_blocking_issues(
            issues, "owner/repo", "MVP Research", config, checkpoint
        )

        assert len(blocking) == 0
        assert len(non_blocking) == 1

    def test_mixed_tags(self) -> None:
        """Mix of tagged and untagged issues is classified correctly."""
        config = make_config()
        checkpoint = make_checkpoint()
        issues = [
            make_issue(4, body="[DEFERRED] deferred"),
            make_issue(5, body="[BLOCKS_IMPL] critical"),
            make_issue(6, body="no tag — blocking by default"),
        ]

        blocking, non_blocking = _classify_blocking_issues(
            issues, "owner/repo", "MVP Research", config, checkpoint
        )

        assert len(blocking) == 2
        assert len(non_blocking) == 1
        assert non_blocking[0]["number"] == 4

    def test_dry_run_returns_all_non_blocking(self) -> None:
        """In dry_run mode all issues are treated as non-blocking."""
        config = make_config()
        checkpoint = make_checkpoint()
        issues = [make_issue(5, body="[BLOCKS_IMPL]"), make_issue(6, body="normal")]

        blocking, non_blocking = _classify_blocking_issues(
            issues, "owner/repo", "MVP Research", config, checkpoint, dry_run=True
        )

        assert len(blocking) == 0
        assert len(non_blocking) == 2


# ---------------------------------------------------------------------------
# _run_completion_gate
# ---------------------------------------------------------------------------


class TestRunCompletionGate:
    def test_logs_stage_complete(self, tmp_path: Path) -> None:
        """stage_complete event is logged."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        with (
            patch("brimstone.cli._file_design_issue_if_missing"),
            patch("brimstone.cli.logger.log_conductor_event") as mock_log,
            patch("brimstone.cli.session.save"),
        ):
            _run_completion_gate(
                repo="owner/repo",
                milestone="v1",
                open_issues=[],
                config=config,
                checkpoint=checkpoint,
            )

        logged_types = [c.kwargs.get("event_type") or c.args[2] for c in mock_log.call_args_list]
        assert "stage_complete" in logged_types

    def test_files_hld_design_issue(self, tmp_path: Path) -> None:
        """HLD design issue is created on completion."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        with (
            patch("brimstone.cli._file_design_issue_if_missing") as mock_hld,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            _run_completion_gate(
                repo="owner/repo",
                milestone="v1",
                open_issues=[],
                config=config,
                checkpoint=checkpoint,
            )

        mock_hld.assert_called_once()
        assert mock_hld.call_args.kwargs.get("milestone") == "v1"

    def test_non_blocking_issues_left_in_place(self, tmp_path: Path) -> None:
        """Non-blocking open issues are not migrated anywhere."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()
        non_blocking = [make_issue(5), make_issue(6)]

        with (
            patch("brimstone.cli._file_design_issue_if_missing"),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.subprocess.run") as mock_subprocess,
        ):
            _run_completion_gate(
                repo="owner/repo",
                milestone="v1",
                open_issues=non_blocking,
                config=config,
                checkpoint=checkpoint,
            )

        # No gh issue edit calls to move issues to another milestone
        gh_calls = [
            c
            for c in mock_subprocess.call_args_list
            if c.args and "issue" in c.args[0] and "edit" in c.args[0]
        ]
        assert not gh_calls

    def test_dry_run_does_not_file_hld(self, tmp_path: Path) -> None:
        """In dry_run mode, no real GitHub calls are made."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        with (
            patch("brimstone.cli._file_design_issue_if_missing") as mock_hld,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.click.echo"),
        ):
            _run_completion_gate(
                repo="owner/repo",
                milestone="v1",
                open_issues=[make_issue(99)],
                config=config,
                checkpoint=checkpoint,
                dry_run=True,
            )

        mock_hld.assert_not_called()


# ---------------------------------------------------------------------------
# _run_research_worker — completion gate (zero blocking → stop)
# ---------------------------------------------------------------------------


class TestRunResearchWorkerCompletionGate:
    def test_stops_when_no_open_issues(self, tmp_path: Path) -> None:
        """Worker exits immediately when there are no open research issues."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        with (
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch("brimstone.cli._run_completion_gate") as mock_gate,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
            )

        mock_gate.assert_called_once()

    def test_stops_when_zero_blocking_issues(self, tmp_path: Path) -> None:
        """Worker exits when all remaining open issues are non-blocking."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        open_issues = [make_issue(10, body="Non-blocking research")]

        with (
            patch("brimstone.cli._list_open_issues_by_label", return_value=open_issues),
            patch("brimstone.cli._classify_blocking_issues", return_value=([], open_issues)),
            patch("brimstone.cli._run_completion_gate") as mock_gate,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
            )

        mock_gate.assert_called_once_with(
            repo="owner/repo",
            milestone="MVP Research",
            open_issues=open_issues,
            config=config,
            checkpoint=checkpoint,
            dry_run=False,
        )


# ---------------------------------------------------------------------------
# _run_research_worker — issue selection filters
# ---------------------------------------------------------------------------


class TestRunResearchWorkerIssueSelection:
    def test_dispatches_unblocked_issue(self, tmp_path: Path) -> None:
        """An available blocking issue should be dispatched."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        blocking_issue = make_issue(1, body="[BLOCKS_IMPL]")
        success_result = make_run_result(subtype="success")

        calls = {"count": 0}

        def open_issues_side_effect(repo, milestone, label):
            # First call: return open issue; second call (after dispatch): return empty
            calls["count"] += 1
            if calls["count"] <= 2:
                return [blocking_issue]
            return []

        def classify_side_effect(open_issues, repo, milestone, config, checkpoint, dry_run=False):
            if open_issues:
                return ([blocking_issue], [])
            return ([], [])

        with (
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues_side_effect),
            patch("brimstone.cli._classify_blocking_issues", side_effect=classify_side_effect),
            patch("brimstone.cli._filter_unblocked", return_value=[blocking_issue]),
            patch("brimstone.cli._sort_issues", return_value=[blocking_issue]),
            patch("brimstone.cli._claim_issue") as mock_claim,
            patch("brimstone.cli.runner.run", return_value=success_result),
            patch("brimstone.cli._run_completion_gate"),
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
            )

        mock_claim.assert_called_once_with(repo="owner/repo", issue_number=1)

    def test_monitors_pr_after_success(self, tmp_path: Path) -> None:
        """After a successful run, PR monitoring should be called."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        blocking_issue = make_issue(1, body="[BLOCKS_IMPL]")
        success_result = make_run_result(subtype="success")

        calls = {"count": 0}

        def open_issues_side_effect(repo, milestone, label):
            calls["count"] += 1
            if calls["count"] <= 2:
                return [blocking_issue]
            return []

        def classify_side_effect(open_issues, repo, milestone, config, checkpoint, dry_run=False):
            if open_issues:
                return ([blocking_issue], [])
            return ([], [])

        with (
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues_side_effect),
            patch("brimstone.cli._classify_blocking_issues", side_effect=classify_side_effect),
            patch("brimstone.cli._filter_unblocked", return_value=[blocking_issue]),
            patch("brimstone.cli._sort_issues", return_value=[blocking_issue]),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._create_worktree", return_value="/tmp/fake-worktree"),
            patch("brimstone.cli._remove_worktree"),
            patch("brimstone.cli.runner.run", return_value=success_result),
            patch("brimstone.cli._find_pr_for_issue", return_value=(99, "1-test-issue")),
            patch("brimstone.cli._monitor_pr", return_value=True) as mock_monitor,
            patch("brimstone.cli._run_completion_gate"),
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
            )

        mock_monitor.assert_called_once()


# ---------------------------------------------------------------------------
# _run_research_worker — rate-limit requeue
# ---------------------------------------------------------------------------


class TestRunResearchWorkerRateLimit:
    def test_unclaims_issue_on_rate_limit(self, tmp_path: Path) -> None:
        """On rate-limited result, issue should be unclaimed and backoff set."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        blocking_issue = make_issue(1, body="[BLOCKS_IMPL]")
        rate_limited_result = make_run_result(
            is_error=True,
            subtype="error_during_execution",
            error_code="rate_limit",
        )

        calls = {"count": 0}

        def open_issues_side_effect(repo, milestone, label):
            calls["count"] += 1
            if calls["count"] <= 2:
                return [blocking_issue]
            return []

        classify_calls = {"count": 0}

        def classify_side_effect(open_issues, repo, milestone, config, checkpoint, dry_run=False):
            classify_calls["count"] += 1
            if classify_calls["count"] <= 2:
                return ([blocking_issue], [])
            return ([], [])

        with (
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues_side_effect),
            patch("brimstone.cli._classify_blocking_issues", side_effect=classify_side_effect),
            patch("brimstone.cli._filter_unblocked", return_value=[blocking_issue]),
            patch("brimstone.cli._sort_issues", return_value=[blocking_issue]),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue") as mock_unclaim,
            patch("brimstone.cli.runner.run", return_value=rate_limited_result),
            patch("brimstone.cli._run_completion_gate"),
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.time.sleep"),  # skip actual sleep
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
            )

        # Issue should have been unclaimed after the rate-limited result
        mock_unclaim.assert_called_with(repo="owner/repo", issue_number=1)

    def test_record_429_called_on_rate_limit(self, tmp_path: Path) -> None:
        """UsageGovernor.record_429 must be called when rate-limited."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        blocking_issue = make_issue(1, body="[BLOCKS_IMPL]")
        rate_limited_result = make_run_result(
            is_error=True,
            subtype="error_during_execution",
            error_code="rate_limit",
        )

        calls = {"count": 0}

        def open_issues_side_effect(repo, milestone, label):
            calls["count"] += 1
            # Return issue twice so we can observe one rate-limit cycle, then stop
            if calls["count"] <= 2:
                return [blocking_issue]
            return []

        classify_calls = {"count": 0}

        def classify_side_effect(open_issues, repo, milestone, config, checkpoint, dry_run=False):
            classify_calls["count"] += 1
            if classify_calls["count"] <= 2:
                return ([blocking_issue], [])
            return ([], [])

        with (
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues_side_effect),
            patch("brimstone.cli._classify_blocking_issues", side_effect=classify_side_effect),
            patch("brimstone.cli._filter_unblocked", return_value=[blocking_issue]),
            patch("brimstone.cli._sort_issues", return_value=[blocking_issue]),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue"),
            patch("brimstone.cli._create_worktree", return_value="/tmp/fake-worktree"),
            patch("brimstone.cli._remove_worktree"),
            patch("brimstone.cli.runner.run", return_value=rate_limited_result),
            patch("brimstone.cli._run_completion_gate"),
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.time.sleep"),
            patch.object(UsageGovernor, "record_429") as mock_429,
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
            )

        mock_429.assert_called()

    def test_record_429_also_called_on_budget_exhausted(self, tmp_path: Path) -> None:
        """record_429 is called on error_max_budget_usd too."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        blocking_issue = make_issue(1, body="[BLOCKS_IMPL]")
        budget_result = make_run_result(
            is_error=True,
            subtype="error_max_budget_usd",
            error_code=None,
        )

        calls = {"count": 0}

        def open_issues_side_effect(repo, milestone, label):
            calls["count"] += 1
            if calls["count"] <= 2:
                return [blocking_issue]
            return []

        classify_calls = {"count": 0}

        def classify_side_effect(open_issues, repo, milestone, config, checkpoint, dry_run=False):
            classify_calls["count"] += 1
            if classify_calls["count"] <= 2:
                return ([blocking_issue], [])
            return ([], [])

        with (
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues_side_effect),
            patch("brimstone.cli._classify_blocking_issues", side_effect=classify_side_effect),
            patch("brimstone.cli._filter_unblocked", return_value=[blocking_issue]),
            patch("brimstone.cli._sort_issues", return_value=[blocking_issue]),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue"),
            patch("brimstone.cli._create_worktree", return_value="/tmp/fake-worktree"),
            patch("brimstone.cli._remove_worktree"),
            patch("brimstone.cli.runner.run", return_value=budget_result),
            patch("brimstone.cli._run_completion_gate"),
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.time.sleep"),
        ):
            with patch.object(UsageGovernor, "record_429") as mock_429:
                _run_research_worker(
                    repo="owner/repo",
                    milestone="MVP Research",
                    config=config,
                    checkpoint=checkpoint,
                )

        mock_429.assert_called()


# ---------------------------------------------------------------------------
# _run_research_worker — error retry and escalation
# ---------------------------------------------------------------------------


class TestRunResearchWorkerErrorHandling:
    def test_escalates_after_max_retries(self, tmp_path: Path) -> None:
        """After MAX_RETRIES failures, a human_escalate event is logged."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        blocking_issue = make_issue(1, body="[BLOCKS_IMPL]")
        error_result = make_run_result(is_error=True, subtype="error_during_execution")

        dispatch_calls = {"count": 0}

        def open_issues_side_effect(repo, milestone, label):
            # Return the issue enough times to exhaust retries, then stop
            dispatch_calls["count"] += 1
            if dispatch_calls["count"] <= 4:
                return [blocking_issue]
            return []

        classify_calls = {"count": 0}

        def classify_side_effect(open_issues, repo, milestone, config, checkpoint, dry_run=False):
            classify_calls["count"] += 1
            if classify_calls["count"] <= 4:
                return ([blocking_issue], [])
            return ([], [])

        logged_events: list[str] = []

        def capture_event(**kwargs):
            logged_events.append(kwargs.get("event_type", ""))

        with (
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues_side_effect),
            patch("brimstone.cli._classify_blocking_issues", side_effect=classify_side_effect),
            patch("brimstone.cli._filter_unblocked", return_value=[blocking_issue]),
            patch("brimstone.cli._sort_issues", return_value=[blocking_issue]),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue"),
            patch("brimstone.cli._create_worktree", return_value="/tmp/fake-worktree"),
            patch("brimstone.cli._remove_worktree"),
            patch("brimstone.cli.runner.run", return_value=error_result),
            patch("brimstone.cli._run_completion_gate"),
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli.logger.log_conductor_event", side_effect=capture_event),
            patch("brimstone.cli.session.save"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
            )

        assert "human_escalate" in logged_events

    def test_unclaims_issue_on_error(self, tmp_path: Path) -> None:
        """On any error, the issue should be unclaimed for retry."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        blocking_issue = make_issue(1, body="[BLOCKS_IMPL]")
        error_result = make_run_result(is_error=True, subtype="error_during_execution")

        calls = {"count": 0}

        def open_issues_side_effect(repo, milestone, label):
            calls["count"] += 1
            if calls["count"] <= 2:
                return [blocking_issue]
            return []

        classify_calls = {"count": 0}

        def classify_side_effect(open_issues, repo, milestone, config, checkpoint, dry_run=False):
            classify_calls["count"] += 1
            if classify_calls["count"] <= 2:
                return ([blocking_issue], [])
            return ([], [])

        with (
            patch("brimstone.cli._list_open_issues_by_label", side_effect=open_issues_side_effect),
            patch("brimstone.cli._classify_blocking_issues", side_effect=classify_side_effect),
            patch("brimstone.cli._filter_unblocked", return_value=[blocking_issue]),
            patch("brimstone.cli._sort_issues", return_value=[blocking_issue]),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue") as mock_unclaim,
            patch("brimstone.cli.runner.run", return_value=error_result),
            patch("brimstone.cli._run_completion_gate"),
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
            )

        mock_unclaim.assert_called()


# ---------------------------------------------------------------------------
# _run_research_worker — dry_run mode
# ---------------------------------------------------------------------------


class TestRunResearchWorkerDryRun:
    def test_dry_run_does_not_call_runner(self, tmp_path: Path) -> None:
        """In dry_run mode, runner.run should never be called."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        blocking_issue = make_issue(1, body="[BLOCKS_IMPL]")

        with (
            patch("brimstone.cli._list_open_issues_by_label", return_value=[blocking_issue]),
            patch("brimstone.cli._classify_blocking_issues", return_value=([blocking_issue], [])),
            patch("brimstone.cli._filter_unblocked", return_value=[blocking_issue]),
            patch("brimstone.cli._sort_issues", return_value=[blocking_issue]),
            patch("brimstone.cli.runner.run") as mock_run,
            patch("brimstone.cli._run_completion_gate"),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.click.echo"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
                dry_run=True,
            )

        mock_run.assert_not_called()

    def test_dry_run_calls_completion_gate(self, tmp_path: Path) -> None:
        """In dry_run mode, completion gate should still be called."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        blocking_issue = make_issue(1, body="[BLOCKS_IMPL]")

        with (
            patch("brimstone.cli._list_open_issues_by_label", return_value=[blocking_issue]),
            patch("brimstone.cli._classify_blocking_issues", return_value=([blocking_issue], [])),
            patch("brimstone.cli._filter_unblocked", return_value=[blocking_issue]),
            patch("brimstone.cli._sort_issues", return_value=[blocking_issue]),
            patch("brimstone.cli.runner.run"),
            patch("brimstone.cli._run_completion_gate") as mock_gate,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.click.echo"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
                dry_run=True,
            )

        mock_gate.assert_called_once()


# ---------------------------------------------------------------------------
# Resume: unclaim stale in-progress issues with no PR
# ---------------------------------------------------------------------------


class TestResumeUnclaim:
    """Verify the resume block correctly handles stale in-progress issues."""

    def test_stale_issue_with_pr_is_monitored(self, tmp_path: Path) -> None:
        """In-progress issue that already has an open PR is resumed via _monitor_pr."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        stale = make_issue(42, title="Stale with PR")

        monitor_calls: list[int] = []
        unclaim_calls: list[int] = []

        with (
            patch("brimstone.cli._list_in_progress_issues", return_value=[stale]),
            patch("brimstone.cli._find_pr_for_issue", return_value=(99, "42-stale-branch")),
            patch(
                "brimstone.cli._monitor_pr",
                side_effect=lambda pr_number, **kw: monitor_calls.append(pr_number),
            ),
            patch(
                "brimstone.cli._unclaim_issue",
                side_effect=lambda repo, num: unclaim_calls.append(num),
            ),
            # After resume, return no open issues so the main loop exits
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch("brimstone.cli._run_completion_gate"),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.click.echo"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        assert 99 in monitor_calls, "_monitor_pr should be called for issue with PR"
        assert 42 not in unclaim_calls, "_unclaim_issue must NOT be called for issue with PR"

    def test_stale_issue_without_pr_is_unclaimed(self, tmp_path: Path) -> None:
        """In-progress issue with no PR is unclaimed so the loop can re-dispatch it."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        stale = make_issue(77, title="Stale no PR")

        unclaim_calls: list[int] = []
        monitor_calls: list[int] = []

        with (
            patch("brimstone.cli._list_in_progress_issues", return_value=[stale]),
            patch("brimstone.cli._find_pr_for_issue", return_value=None),
            patch(
                "brimstone.cli._unclaim_issue",
                side_effect=lambda repo, num: unclaim_calls.append(num),
            ),
            patch(
                "brimstone.cli._monitor_pr",
                side_effect=lambda **kw: monitor_calls.append(kw.get("pr_number")),
            ),
            # After unclaim, no open issues remain so the main loop exits
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch("brimstone.cli._run_completion_gate"),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.click.echo"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        assert 77 in unclaim_calls, "_unclaim_issue must be called for stale issue with no PR"
        assert not monitor_calls, "_monitor_pr must NOT be called when there is no PR"

    def test_multiple_stale_issues_handled_independently(self, tmp_path: Path) -> None:
        """Each stale issue is handled independently: PR → monitor, no PR → unclaim."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path / "logs")
        checkpoint = make_checkpoint()

        stale_with_pr = make_issue(10, title="Has PR")
        stale_no_pr = make_issue(20, title="No PR")

        def fake_find_pr(repo: str, issue_number: int):
            if issue_number == 10:
                return (55, "10-branch")
            return None

        monitor_calls: list[int] = []
        unclaim_calls: list[int] = []

        with (
            patch(
                "brimstone.cli._list_in_progress_issues",
                return_value=[stale_with_pr, stale_no_pr],
            ),
            patch("brimstone.cli._find_pr_for_issue", side_effect=fake_find_pr),
            patch(
                "brimstone.cli._monitor_pr",
                side_effect=lambda pr_number, **kw: monitor_calls.append(pr_number),
            ),
            patch(
                "brimstone.cli._unclaim_issue",
                side_effect=lambda repo, num: unclaim_calls.append(num),
            ),
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch("brimstone.cli._run_completion_gate"),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.click.echo"),
        ):
            _run_research_worker(
                repo="owner/repo",
                milestone="MVP Research",
                config=config,
                checkpoint=checkpoint,
                dry_run=False,
            )

        assert 55 in monitor_calls, "Issue 10 (has PR) should be monitored"
        assert 20 in unclaim_calls, "Issue 20 (no PR) should be unclaimed"
        assert 10 not in unclaim_calls, "Issue 10 (has PR) must not be unclaimed"
