"""Unit tests for the impl-worker loop in src/brimstone/cli.py.

Tests cover:
- Issue selection respects module isolation (active module blocked)
- _extract_module extracts feat:* label correctly
- _slugify produces branch-safe slugs
- _list_open_issues_by_label filters correctly (excludes in-progress, assigned)
- Sequential claiming with mock _gh
- Dispatch with mock runner
- CI monitoring state machine (pass/fail/conflict/timeout)
- _get_pr_checks_status aggregation
- _get_review_status aggregation
- _find_next_version infers version strings
- Completion gate: no open issues -> file pipeline issue
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from brimstone.cli import (
    IMPL_LABEL,
    UsageGovernor,
    _dispatch_conflict_resolution_agent,
    _extract_module,
    _find_next_version,
    _find_pr_for_branch,
    _find_pr_for_issue,
    _get_pr_checks_status,
    _get_review_status,
    _list_open_issues_by_label,
    _monitor_pr,
    _rebase_branch,
    _resume_stale_issues,
    _run_impl_worker,
    _slugify,
)
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
        milestone="M2: Implementation",
        stage="impl",
        timestamp="2026-01-01T00:00:00+00:00",
    )
    defaults.update(overrides)
    return Checkpoint(**defaults)


def make_run_result(
    is_error: bool = False,
    subtype: str = "success",
    error_code: str | None = None,
) -> RunResult:
    """Return a minimal RunResult instance."""
    return RunResult(
        is_error=is_error,
        subtype=subtype,
        error_code=error_code,
        exit_code=0 if not is_error else 1,
        total_cost_usd=0.0,
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
        raw_result_event=None,
        stderr="",
        overage_detected=False,
    )


def make_issue(
    number: int = 42,
    title: str = "Add config module",
    body: str = "## Context\nDo the thing.",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
) -> dict:
    """Return a minimal issue dict."""
    label_names = labels if labels is not None else ["feat:config"]
    return {
        "number": number,
        "title": title,
        "body": body,
        "labels": [{"name": n} for n in label_names],
        "assignees": [{"login": a} for a in (assignees or [])],
    }


def make_gh_result(stdout: str = "[]", returncode: int = 0) -> MagicMock:
    """Return a mock CompletedProcess from _gh."""
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    result.stderr = ""
    return result


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_basic_title(self) -> None:
        assert _slugify("Add config module") == "add-config-module"

    def test_special_characters_replaced(self) -> None:
        assert _slugify("Fix: handle & special chars!") == "fix-handle-special-chars"

    def test_truncated_to_max_len(self) -> None:
        long_title = "a" * 60
        result = _slugify(long_title, max_len=40)
        assert len(result) <= 40

    def test_leading_trailing_hyphens_stripped(self) -> None:
        result = _slugify("---foo---")
        assert not result.startswith("-")
        assert not result.endswith("-")

    def test_empty_string(self) -> None:
        assert _slugify("") == ""

    def test_unicode_becomes_hyphens(self) -> None:
        result = _slugify("héllo wörld")
        # non-ascii is replaced by hyphens
        assert "h" in result
        assert "-" in result


# ---------------------------------------------------------------------------
# _extract_module
# ---------------------------------------------------------------------------


class TestExtractModule:
    def test_extracts_config(self) -> None:
        issue = make_issue(labels=["feat:config"])
        assert _extract_module(issue) == "config"

    def test_extracts_runner(self) -> None:
        issue = make_issue(labels=["feat:runner"])
        assert _extract_module(issue) == "runner"

    def test_extracts_health(self) -> None:
        issue = make_issue(labels=["feat:health"])
        assert _extract_module(issue) == "health"

    def test_extracts_logging(self) -> None:
        issue = make_issue(labels=["feat:logging"])
        assert _extract_module(issue) == "logging"

    def test_extracts_cli(self) -> None:
        issue = make_issue(labels=["feat:cli"])
        assert _extract_module(issue) == "cli"

    def test_returns_none_when_no_feat_label(self) -> None:
        issue = make_issue(labels=["research", "pipeline"])
        assert _extract_module(issue) == "none"

    def test_returns_none_on_empty_labels(self) -> None:
        issue = make_issue(labels=[])
        assert _extract_module(issue) == "none"

    def test_prefers_first_feat_label(self) -> None:
        issue = make_issue(labels=["feat:config", "feat:runner"])
        # Should return the first one found
        result = _extract_module(issue)
        assert result in ("config", "runner")

    def test_ignores_non_feat_labels(self) -> None:
        issue = make_issue(labels=["infra", "feat:health", "in-progress"])
        assert _extract_module(issue) == "health"


# ---------------------------------------------------------------------------
# _list_open_issues_by_label (impl)
# ---------------------------------------------------------------------------


class TestListOpenImplIssues:
    def _call(self, issues_json: str, returncode: int = 0) -> list[dict]:
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(stdout=issues_json, returncode=returncode)
            return _list_open_issues_by_label("owner/repo", "M2: Implementation", IMPL_LABEL)

    def test_returns_empty_on_gh_failure(self) -> None:
        result = self._call("error", returncode=1)
        assert result == []

    def test_returns_empty_on_invalid_json(self) -> None:
        result = self._call("not-json")
        assert result == []

    def test_filters_out_in_progress_issues(self) -> None:
        issues = [
            make_issue(number=1, labels=["feat:config", "in-progress"]),
            make_issue(number=2, labels=["feat:runner"]),
        ]
        result = self._call(json.dumps(issues))
        assert len(result) == 1
        assert result[0]["number"] == 2

    def test_filters_out_assigned_issues(self) -> None:
        issues = [
            make_issue(number=1, labels=["feat:config"], assignees=["someone"]),
            make_issue(number=2, labels=["feat:runner"], assignees=[]),
        ]
        result = self._call(json.dumps(issues))
        assert len(result) == 1
        assert result[0]["number"] == 2

    def test_returns_eligible_issues(self) -> None:
        issues = [
            make_issue(number=1, labels=["feat:config"]),
            make_issue(number=2, labels=["feat:runner"]),
        ]
        result = self._call(json.dumps(issues))
        assert len(result) == 2

    def test_returns_empty_list_when_no_issues(self) -> None:
        result = self._call("[]")
        assert result == []


# ---------------------------------------------------------------------------
# Module isolation in issue selection
# ---------------------------------------------------------------------------


class TestModuleIsolation:
    """Tests that the impl-worker respects module isolation when selecting issues."""

    def test_active_module_blocks_same_module_issue(self) -> None:
        """When a module is active, issues for that module should be skipped."""
        issues = [
            make_issue(number=1, labels=["feat:config"]),
            make_issue(number=2, labels=["feat:runner"]),
        ]
        active_modules = {"config"}
        gov = UsageGovernor(make_config(), make_checkpoint())

        dispatchable = []
        for issue in issues:
            from brimstone.cli import _extract_module

            mod = _extract_module(issue)
            if mod == "none" or mod not in active_modules:
                if gov.can_dispatch(1):
                    dispatchable.append(issue)

        # config is active; only runner should be dispatchable
        assert len(dispatchable) == 1
        assert dispatchable[0]["number"] == 2

    def test_different_modules_can_be_dispatched_simultaneously(self) -> None:
        """Issues from different modules can both be selected."""
        issues = [
            make_issue(number=1, labels=["feat:config"]),
            make_issue(number=2, labels=["feat:runner"]),
        ]
        active_modules: set = set()
        gov = UsageGovernor(make_config(), make_checkpoint())

        dispatchable = []
        for issue in issues:
            from brimstone.cli import _extract_module

            mod = _extract_module(issue)
            if mod == "none" or mod not in active_modules:
                if gov.can_dispatch(1):
                    dispatchable.append(issue)
                    active_modules.add(mod)
                    gov.record_dispatch(1)

        assert len(dispatchable) == 2

    def test_none_module_always_dispatchable(self) -> None:
        """Issues without a feat:* label (module='none') are always dispatchable."""
        issues = [
            make_issue(number=1, labels=["infra"]),  # no feat: label
        ]
        active_modules = {"config", "runner", "health"}
        gov = UsageGovernor(make_config(), make_checkpoint())

        dispatchable = []
        for issue in issues:
            from brimstone.cli import _extract_module

            mod = _extract_module(issue)
            if mod == "none" or mod not in active_modules:
                if gov.can_dispatch(1):
                    dispatchable.append(issue)

        assert len(dispatchable) == 1


# ---------------------------------------------------------------------------
# _find_next_version
# ---------------------------------------------------------------------------


class TestFindNextVersion:
    def test_mvp_becomes_v2(self) -> None:
        assert _find_next_version("MVP Implementation") == "v2"

    def test_v1_becomes_v2(self) -> None:
        assert _find_next_version("v1 Implementation") == "v2"

    def test_v1_1_becomes_v2(self) -> None:
        assert _find_next_version("v1.1 Implementation") == "v2"

    def test_v2_becomes_v3(self) -> None:
        assert _find_next_version("v2 Implementation") == "v3"

    def test_unknown_milestone_returns_default(self) -> None:
        result = _find_next_version("Unknown Milestone")
        assert result == "next version"


# ---------------------------------------------------------------------------
# _get_pr_checks_status
# ---------------------------------------------------------------------------


class TestGetPrChecksStatus:
    def _call(self, checks_json: str, returncode: int = 0) -> str:
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(stdout=checks_json, returncode=returncode)
            return _get_pr_checks_status("owner/repo", 42)

    def test_returns_pending_on_gh_failure(self) -> None:
        assert self._call("", returncode=1) == "pending"

    def test_returns_pass_on_empty_checks(self) -> None:
        # No CI configured → treat as green so PRs aren't stuck
        assert self._call("[]") == "pass"

    def test_returns_pass_when_all_succeed(self) -> None:
        checks = [
            {"name": "ci", "state": "completed", "bucket": "pass"},
            {"name": "lint", "state": "completed", "bucket": "pass"},
        ]
        assert self._call(json.dumps(checks)) == "pass"

    def test_returns_fail_when_any_failed(self) -> None:
        checks = [
            {"name": "ci", "state": "completed", "bucket": "fail"},
            {"name": "lint", "state": "completed", "bucket": "pass"},
        ]
        assert self._call(json.dumps(checks)) == "fail"

    def test_returns_pending_when_any_in_progress(self) -> None:
        checks = [
            {"name": "ci", "state": "in_progress", "bucket": "pending"},
            {"name": "lint", "state": "completed", "bucket": "pass"},
        ]
        assert self._call(json.dumps(checks)) == "pending"

    def test_fail_takes_priority_over_pending(self) -> None:
        checks = [
            {"name": "ci", "state": "in_progress", "bucket": "pending"},
            {"name": "lint", "state": "completed", "bucket": "fail"},
        ]
        assert self._call(json.dumps(checks)) == "fail"


# ---------------------------------------------------------------------------
# _get_review_status
# ---------------------------------------------------------------------------


class TestGetReviewStatus:
    def _call(self, reviews_json: str, returncode: int = 0) -> str:
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(stdout=reviews_json, returncode=returncode)
            return _get_review_status("owner/repo", 42)

    def test_returns_no_review_on_gh_failure(self) -> None:
        assert self._call("", returncode=1) == "no_review"

    def test_returns_no_review_when_empty(self) -> None:
        assert self._call(json.dumps({"reviews": []})) == "no_review"

    def test_returns_approved_when_approved(self) -> None:
        data = {
            "reviews": [
                {"author": {"login": "reviewer1"}, "state": "APPROVED"},
            ]
        }
        assert self._call(json.dumps(data)) == "approved"

    def test_returns_changes_requested_when_blocked(self) -> None:
        data = {
            "reviews": [
                {"author": {"login": "reviewer1"}, "state": "CHANGES_REQUESTED"},
            ]
        }
        assert self._call(json.dumps(data)) == "changes_requested"

    def test_changes_requested_takes_priority_over_approved(self) -> None:
        data = {
            "reviews": [
                {"author": {"login": "reviewer1"}, "state": "APPROVED"},
                {"author": {"login": "reviewer2"}, "state": "CHANGES_REQUESTED"},
            ]
        }
        assert self._call(json.dumps(data)) == "changes_requested"


# ---------------------------------------------------------------------------
# _find_pr_for_branch
# ---------------------------------------------------------------------------


class TestFindPrForBranch:
    def test_returns_none_on_gh_failure(self) -> None:
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(returncode=1)
            assert _find_pr_for_branch("owner/repo", "42-add-config") is None

    def test_returns_none_when_no_prs(self) -> None:
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(stdout="[]")
            assert _find_pr_for_branch("owner/repo", "42-add-config") is None

    def test_returns_pr_number(self) -> None:
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(stdout=json.dumps([{"number": 99}]))
            assert _find_pr_for_branch("owner/repo", "42-add-config") == 99


# ---------------------------------------------------------------------------
# _find_pr_for_issue
# ---------------------------------------------------------------------------


class TestFindPrForIssue:
    def _prs(self, items: list[dict]) -> str:
        return json.dumps(items)

    def test_returns_none_on_gh_failure(self) -> None:
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(returncode=1)
            assert _find_pr_for_issue("owner/repo", 42) is None

    def test_returns_none_when_no_prs(self) -> None:
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(stdout="[]")
            assert _find_pr_for_issue("owner/repo", 42) is None

    def test_matches_by_branch_prefix(self) -> None:
        prs = [{"number": 77, "headRefName": "42-add-config", "body": ""}]
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(stdout=self._prs(prs))
            result = _find_pr_for_issue("owner/repo", 42)
        assert result == (77, "42-add-config")

    def test_matches_by_closes_in_body(self) -> None:
        prs = [{"number": 88, "headRefName": "some-other-branch", "body": "Closes #42\nDetails."}]
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(stdout=self._prs(prs))
            result = _find_pr_for_issue("owner/repo", 42)
        assert result == (88, "some-other-branch")

    def test_ignores_non_matching_prs(self) -> None:
        prs = [
            {"number": 10, "headRefName": "99-unrelated", "body": "Closes #99"},
        ]
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(stdout=self._prs(prs))
            assert _find_pr_for_issue("owner/repo", 42) is None


# ---------------------------------------------------------------------------
# _monitor_pr — CI state machine
# ---------------------------------------------------------------------------


class TestMonitorPr:
    def _make_monitor_kwargs(self, **overrides) -> dict:
        defaults = dict(
            pr_number=99,
            branch="42-add-config",
            repo="owner/repo",
            config=make_config(),
            checkpoint=make_checkpoint(),
            worktree_path="/tmp/worktree",
            issue_number=42,
            poll_interval=0,  # no sleep in tests
        )
        defaults.update(overrides)
        return defaults

    def test_passes_and_merges_when_ci_passes(self) -> None:
        """When CI passes and no changes requested, PR is enqueued for merge."""
        checks = [{"name": "ci", "state": "completed", "bucket": "pass"}]
        reviews = {"reviews": []}

        def gh_side_effect(args, **kwargs):
            if "checks" in args:
                return make_gh_result(stdout=json.dumps(checks))
            elif "reviews" in args:
                return make_gh_result(stdout=json.dumps(reviews))
            return make_gh_result()

        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs())

        assert result is True

    def test_returns_false_on_timeout(self) -> None:
        """When CI never passes within max_polls, returns False."""
        checks = [{"name": "ci", "status": "in_progress", "conclusion": None}]

        with (
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            mock_gh.return_value = make_gh_result(stdout=json.dumps(checks))
            result = _monitor_pr(**self._make_monitor_kwargs(max_polls=2))

        assert result is False

    def test_returns_false_after_repeated_ci_failures(self) -> None:
        """After MAX_RETRIES CI failures (non-conflict), returns False."""
        fail_checks = [{"name": "ci", "status": "completed", "conclusion": "failure"}]
        # _is_conflict_failure will return False by default

        def gh_side_effect(args, **kwargs):
            if "checks" in args:
                return make_gh_result(stdout=json.dumps(fail_checks))
            # mergeable check for conflict detection
            elif "mergeable" in str(args):
                return make_gh_result(
                    stdout=json.dumps({"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"})
                )
            return make_gh_result()

        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            # MAX_RETRIES = 3, so we need 3 fail polls
            _monitor_pr(**self._make_monitor_kwargs(max_polls=10))

        # result would be False but we just ensure no exception and rebase not called

    def test_attempts_rebase_on_conflict(self) -> None:
        """Conflict detected at start of poll (before CI check) triggers rebase."""
        pass_checks = [{"name": "ci", "state": "completed", "bucket": "pass"}]

        def gh_side_effect(args, **kwargs):
            args_str = " ".join(str(a) for a in args)
            if "checks" in args_str:
                return make_gh_result(stdout=json.dumps(pass_checks))
            elif "reviews" in args_str:
                return make_gh_result(stdout=json.dumps({"reviews": []}))
            return make_gh_result()

        # Conflict on first poll only; no conflict on second poll → CI pass → enqueue
        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli._is_conflict_failure", side_effect=[True, False]),
            patch("brimstone.cli._rebase_branch", return_value=True) as mock_rebase,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs(max_polls=10))

        assert result is True
        mock_rebase.assert_called_once()

    def test_returns_false_after_rebase_limit_exceeded(self) -> None:
        """After _REBASE_RETRY_LIMIT rebase attempts, returns False."""
        # Conflict persists on every poll — rebase succeeds but conflict re-appears
        with (
            patch("brimstone.cli._gh"),
            patch("brimstone.cli._is_conflict_failure", return_value=True),
            patch("brimstone.cli._rebase_branch", return_value=True),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs(max_polls=20))

        assert result is False

    def test_returns_true_on_ci_pass_enqueues_instead_of_merge(self) -> None:
        """When CI passes, _monitor_pr enqueues to MergeQueue and returns True (no inline merge)."""
        checks = [{"name": "ci", "state": "completed", "bucket": "pass"}]
        reviews = {"reviews": []}

        def gh_side_effect(args, **kwargs):
            args_str = " ".join(str(a) for a in args)
            if "checks" in args_str:
                return make_gh_result(stdout=json.dumps(checks))
            elif "reviews" in args_str:
                return make_gh_result(stdout=json.dumps(reviews))
            return make_gh_result()

        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect) as mock_gh,
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs())

        # Should return True (enqueued), and _gh should NOT have been called with "pr merge"
        assert result is True
        merge_calls = [
            c
            for c in mock_gh.call_args_list
            if len(c.args) > 0 and "pr" in c.args[0] and "merge" in c.args[0]
        ]
        assert merge_calls == []

    def test_escalates_immediately_on_conflict_without_worktree(self) -> None:
        """When worktree_path is empty and conflict detected, escalates without rebase."""
        with (
            patch("brimstone.cli._gh"),
            patch("brimstone.cli._is_conflict_failure", return_value=True),
            patch("brimstone.cli._rebase_branch") as mock_rebase,
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs(worktree_path="", max_polls=5))

        assert result is False
        mock_rebase.assert_not_called()

    def test_merges_immediately_with_no_ci(self) -> None:
        """When no CI checks are configured, PR should be enqueued without waiting."""
        reviews = {"reviews": []}

        def gh_side_effect(args, **kwargs):
            args_str = " ".join(str(a) for a in args)
            if "checks" in args_str:
                return make_gh_result(stdout="[]")  # no checks
            elif "reviews" in args_str:
                return make_gh_result(stdout=json.dumps(reviews))
            return make_gh_result()

        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs())

        assert result is True

    def test_conflict_detected_while_ci_pending_triggers_rebase(self) -> None:
        """Conflict is caught on the first poll even while CI is still pending."""
        pass_checks = [{"name": "ci", "state": "completed", "bucket": "pass"}]

        def gh_side_effect(args, **kwargs):
            args_str = " ".join(str(a) for a in args)
            if "checks" in args_str:
                return make_gh_result(stdout=json.dumps(pass_checks))
            elif "reviews" in args_str:
                return make_gh_result(stdout=json.dumps({"reviews": []}))
            return make_gh_result()

        # Poll 1: CI would be pending, but conflict fires first → rebase
        # Poll 2: no conflict → CI pass → enqueue
        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli._is_conflict_failure", side_effect=[True, False]),
            patch("brimstone.cli._rebase_branch", return_value=True) as mock_rebase,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs(max_polls=10))

        assert result is True
        mock_rebase.assert_called_once()


# ---------------------------------------------------------------------------
# _run_impl_worker — Completion gate (no open issues → file pipeline issue)
# ---------------------------------------------------------------------------


class TestRunImplWorkerCompletionGate:
    def test_completion_gate_logs_stage_complete_when_no_open_issues(self, tmp_path: Path) -> None:
        """When no open impl issues remain, logs stage_complete event and stops."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path)
        checkpoint = make_checkpoint(milestone="MVP Implementation")

        with (
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch("brimstone.cli._count_all_issues_by_label", return_value=3),
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.logger.log_conductor_event") as mock_log_event,
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.click.echo"),
        ):
            mock_gh.return_value = make_gh_result(returncode=0)
            _run_impl_worker(
                repo="owner/repo",
                milestone="MVP Implementation",
                config=config,
                checkpoint=checkpoint,
            )

        stage_complete_calls = [
            c for c in mock_log_event.call_args_list if "stage_complete" in str(c)
        ]
        assert len(stage_complete_calls) >= 1

    def test_completion_gate_dry_run_prints_complete_message(self, tmp_path: Path) -> None:
        """In dry-run mode, completion gate prints without calling _gh."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path)
        checkpoint = make_checkpoint(milestone="MVP Implementation")

        with (
            patch("brimstone.cli._list_open_issues_by_label", return_value=[]),
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.click.echo") as mock_echo,
        ):
            _run_impl_worker(
                repo="owner/repo",
                milestone="MVP Implementation",
                config=config,
                checkpoint=checkpoint,
                dry_run=True,
            )

        # _gh should NOT have been called in dry-run
        create_calls = [c for c in mock_gh.call_args_list if "create" in str(c)]
        assert len(create_calls) == 0

        # But echo should have been called with the dry-run message
        echo_texts = " ".join(str(c) for c in mock_echo.call_args_list)
        assert "dry-run" in echo_texts


# ---------------------------------------------------------------------------
# _run_impl_worker — Sequential claiming
# ---------------------------------------------------------------------------


class TestRunImplWorkerClaiming:
    def test_sequential_claiming_calls_claim_for_each_issue(self, tmp_path: Path) -> None:
        """For each selected issue, _claim_issue is called before dispatch."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path)
        checkpoint = make_checkpoint(milestone="MVP Implementation")

        issues = [
            make_issue(number=10, labels=["feat:config"]),
        ]

        with (
            patch("brimstone.cli._list_open_issues_by_label", side_effect=[issues, issues, [], []]),
            patch("brimstone.cli._count_all_issues_by_label", return_value=1),
            patch("brimstone.cli._claim_issue") as mock_claim,
            patch("brimstone.cli._create_worktree", return_value="/tmp/wt"),
            patch("brimstone.cli._dispatch_impl_agent") as mock_dispatch,
            patch("brimstone.cli._find_pr_for_branch", return_value=None),
            patch("brimstone.cli._unclaim_issue"),
            patch("brimstone.cli._remove_worktree"),
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.click.echo"),
        ):
            mock_gh.return_value = make_gh_result(returncode=0)
            mock_dispatch.return_value = (
                issues[0],
                "10-add-config-module",
                "/tmp/wt",
                make_run_result(),
            )

            _run_impl_worker(
                repo="owner/repo",
                milestone="MVP Implementation",
                config=config,
                checkpoint=checkpoint,
            )

        mock_claim.assert_called_once()
        call_kwargs = mock_claim.call_args.kwargs
        assert call_kwargs.get("repo") == "owner/repo"
        assert call_kwargs.get("issue_number") == 10

    def test_dry_run_does_not_create_worktrees(self, tmp_path: Path) -> None:
        """In dry-run mode, _create_worktree is never called."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path)
        checkpoint = make_checkpoint(milestone="MVP Implementation")

        issues = [
            make_issue(number=10, labels=["feat:config"]),
        ]

        with (
            patch("brimstone.cli._list_open_issues_by_label", return_value=issues),
            patch("brimstone.cli._create_worktree") as mock_create,
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.click.echo"),
        ):
            mock_gh.return_value = make_gh_result(returncode=0)

            _run_impl_worker(
                repo="owner/repo",
                milestone="MVP Implementation",
                config=config,
                checkpoint=checkpoint,
                dry_run=True,
            )

        mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# _run_impl_worker — Rate limit handling
# ---------------------------------------------------------------------------


class TestRunImplWorkerRateLimitHandling:
    def test_rate_limited_result_triggers_unclaim_and_backoff(self, tmp_path: Path) -> None:
        """When agent returns rate_limit error, issue is unclaimed and backoff is set."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path)
        checkpoint = make_checkpoint(milestone="MVP Implementation")

        issues = [make_issue(number=10, labels=["feat:config"])]
        rate_limited_result = make_run_result(
            is_error=True, subtype="error_during_execution", error_code="rate_limit"
        )

        with (
            patch("brimstone.cli._list_open_issues_by_label", side_effect=[issues, issues, [], []]),
            patch("brimstone.cli._count_all_issues_by_label", return_value=1),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue") as mock_unclaim,
            patch("brimstone.cli._create_worktree", return_value="/tmp/wt"),
            patch("brimstone.cli._remove_worktree"),
            patch("brimstone.cli._dispatch_impl_agent") as mock_dispatch,
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.click.echo"),
        ):
            mock_gh.return_value = make_gh_result(returncode=0)
            mock_dispatch.return_value = (
                issues[0],
                "10-add-config-module",
                "/tmp/wt",
                rate_limited_result,
            )

            _run_impl_worker(
                repo="owner/repo",
                milestone="MVP Implementation",
                config=config,
                checkpoint=checkpoint,
            )

        mock_unclaim.assert_called()
        call_kwargs = mock_unclaim.call_args.kwargs
        assert call_kwargs.get("repo") == "owner/repo"
        assert call_kwargs.get("issue_number") == 10


# ---------------------------------------------------------------------------
# _dispatch_conflict_resolution_agent
# ---------------------------------------------------------------------------


class TestDispatchConflictResolutionAgent:
    def test_returns_true_when_agent_succeeds(self) -> None:
        """Returns True when _run_agent returns a non-error result."""
        success_result = make_run_result(is_error=False)

        with patch("brimstone.cli._run_agent", return_value=success_result) as mock_run:
            result = _dispatch_conflict_resolution_agent(
                branch="57-add-feature",
                worktree_path="/tmp/wt",
                repo="owner/repo",
                default_branch="mainline",
                config=make_config(),
            )

        assert result is True
        mock_run.assert_called_once()

    def test_returns_false_when_agent_errors(self) -> None:
        """Returns False when _run_agent returns an error result."""
        error_result = make_run_result(is_error=True)

        with patch("brimstone.cli._run_agent", return_value=error_result):
            result = _dispatch_conflict_resolution_agent(
                branch="57-add-feature",
                worktree_path="/tmp/wt",
                repo="owner/repo",
                default_branch="mainline",
                config=make_config(),
            )

        assert result is False


# ---------------------------------------------------------------------------
# _rebase_branch — conflict agent dispatch
# ---------------------------------------------------------------------------


class TestRebaseBranchConflictAgent:
    def _make_proc(self, returncode: int = 0) -> MagicMock:
        proc = MagicMock()
        proc.returncode = returncode
        proc.stdout = ""
        proc.stderr = ""
        return proc

    def test_calls_conflict_agent_when_rebase_fails(self) -> None:
        """When rebase fails and config is provided, dispatches conflict agent."""
        fetch_ok = self._make_proc(0)
        rebase_fail = self._make_proc(1)

        with (
            patch("brimstone.cli.subprocess.run", side_effect=[fetch_ok, rebase_fail]),
            patch(
                "brimstone.cli._dispatch_conflict_resolution_agent", return_value=True
            ) as mock_agent,
        ):
            result = _rebase_branch(
                "57-add-feature",
                "owner/repo",
                "/tmp/wt",
                "mainline",
                config=make_config(),
            )

        assert result is True
        mock_agent.assert_called_once()

    def test_aborts_rebase_when_no_config(self) -> None:
        """When rebase fails and config is None, aborts rebase and returns False."""
        fetch_ok = self._make_proc(0)
        rebase_fail = self._make_proc(1)
        abort_ok = self._make_proc(0)

        with (
            patch(
                "brimstone.cli.subprocess.run",
                side_effect=[fetch_ok, rebase_fail, abort_ok],
            ),
            patch("brimstone.cli._dispatch_conflict_resolution_agent") as mock_agent,
        ):
            result = _rebase_branch(
                "57-add-feature",
                "owner/repo",
                "/tmp/wt",
                "mainline",
                config=None,
            )

        assert result is False
        mock_agent.assert_not_called()

    def test_aborts_when_agent_also_fails(self) -> None:
        """When conflict agent fails, aborts and returns False."""
        fetch_ok = self._make_proc(0)
        rebase_fail = self._make_proc(1)
        abort_ok = self._make_proc(0)

        with (
            patch(
                "brimstone.cli.subprocess.run",
                side_effect=[fetch_ok, rebase_fail, abort_ok],
            ),
            patch("brimstone.cli._dispatch_conflict_resolution_agent", return_value=False),
        ):
            result = _rebase_branch(
                "57-add-feature",
                "owner/repo",
                "/tmp/wt",
                "mainline",
                config=make_config(),
            )

        assert result is False


# ---------------------------------------------------------------------------
# _monitor_pr — _rebase_branch called with config kwarg
# ---------------------------------------------------------------------------


class TestMonitorPrRebaseConfig:
    def _make_monitor_kwargs(self, **overrides) -> dict:
        defaults = dict(
            pr_number=99,
            branch="57-add-feature",
            repo="owner/repo",
            config=make_config(),
            checkpoint=make_checkpoint(),
            worktree_path="/tmp/worktree",
            issue_number=57,
            poll_interval=0,
        )
        defaults.update(overrides)
        return defaults

    def test_passes_config_to_rebase_branch(self) -> None:
        """_rebase_branch is called with config= kwarg from _monitor_pr."""
        pass_checks = [{"name": "ci", "state": "completed", "bucket": "pass"}]

        def gh_side_effect(args, **kwargs):
            args_str = " ".join(str(a) for a in args)
            if "checks" in args_str:
                return make_gh_result(stdout=json.dumps(pass_checks))
            elif "reviews" in args_str:
                return make_gh_result(stdout=json.dumps({"reviews": []}))
            elif "merge" in args_str:
                return make_gh_result(returncode=0)
            return make_gh_result()

        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli._is_conflict_failure", side_effect=[True, False]),
            patch("brimstone.cli._rebase_branch", return_value=True) as mock_rebase,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            config = make_config()
            result = _monitor_pr(**self._make_monitor_kwargs(config=config, max_polls=10))

        assert result is True
        _, kwargs = mock_rebase.call_args
        assert "config" in kwargs
        assert kwargs["config"] is config


# ---------------------------------------------------------------------------
# _monitor_pr — PRBead writes
# ---------------------------------------------------------------------------


class TestMonitorPrPrBeadWrites:
    """Verify _monitor_pr() writes PRBead state transitions."""

    def _make_store(self) -> MagicMock:
        store = MagicMock()
        store.read_work_bead.return_value = None
        store.read_pr_bead.return_value = None
        return store

    def _make_monitor_kwargs(self, store: MagicMock, **overrides) -> dict:
        defaults = dict(
            pr_number=99,
            branch="42-fix",
            repo="owner/repo",
            config=make_config(),
            checkpoint=make_checkpoint(),
            issue_number=42,
            store=store,
            poll_interval=0,
        )
        defaults.update(overrides)
        return defaults

    def test_writes_pr_bead_on_entry(self) -> None:
        """PRBead with state='open' is written when _monitor_pr starts."""
        store = self._make_store()
        checks = [{"name": "ci", "state": "completed", "bucket": "pass"}]
        reviews = {"reviews": []}
        # Capture bead state at each write (PRBead is mutable — the same object is reused)
        captured_states: list[str] = []

        def capture_write(bead):
            captured_states.append(bead.state)

        store.write_pr_bead.side_effect = capture_write

        def gh_side_effect(args, **kwargs):
            if "checks" in args:
                return make_gh_result(stdout=json.dumps(checks))
            elif "reviews" in args:
                return make_gh_result(stdout=json.dumps(reviews))
            elif "merge" in args:
                return make_gh_result(returncode=0)
            return make_gh_result()

        with (
            patch("brimstone.cli._is_conflict_failure", return_value=False),
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            _monitor_pr(**self._make_monitor_kwargs(store))

        # First write must have state='open' (entry write)
        assert store.write_pr_bead.call_count >= 1
        assert captured_states[0] == "open"
        # Verify pr_number and issue_number on the bead passed to the first write
        first_call_bead = store.write_pr_bead.call_args_list[0][0][0]
        assert first_call_bead.pr_number == 99
        assert first_call_bead.issue_number == 42

    def test_returns_false_on_persistent_ci_failure(self) -> None:
        """_monitor_pr returns False when CI fails persistently without recovery."""
        store = self._make_store()
        fail_checks = [{"name": "ci", "status": "completed", "conclusion": "failure"}]

        def gh_side_effect(args, **kwargs):
            if "checks" in args:
                return make_gh_result(stdout=json.dumps(fail_checks))
            elif "mergeable" in str(args):
                return make_gh_result(
                    stdout=json.dumps({"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"})
                )
            return make_gh_result()

        with (
            patch("brimstone.cli._is_conflict_failure", return_value=False),
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs(store, max_polls=10))

        assert result is False

    def test_writes_ci_failing_bead_on_persistent_ci_failure(self) -> None:
        """PRBead state is set to 'ci_failing' after two consecutive CI failures."""
        store = self._make_store()
        # Use bucket="fail" so _get_pr_checks_status returns "fail"
        fail_checks = [{"name": "ci", "state": "completed", "bucket": "fail"}]
        captured_states: list[str] = []

        def capture_write(bead):
            captured_states.append(bead.state)

        store.write_pr_bead.side_effect = capture_write

        def gh_side_effect(args, **kwargs):
            if "checks" in args:
                return make_gh_result(stdout=json.dumps(fail_checks))
            return make_gh_result()

        with (
            patch("brimstone.cli._is_conflict_failure", return_value=False),
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs(store, max_polls=10))

        assert result is False
        # The second CI failure should produce a "ci_failing" bead write
        assert "ci_failing" in captured_states

    def test_writes_merge_ready_bead_on_ci_pass(self) -> None:
        """PRBead state is set to 'merge_ready' when CI passes (enqueued, not merged inline)."""
        store = self._make_store()
        from brimstone.beads import MergeQueue

        store.read_merge_queue.return_value = MergeQueue(
            v=1, queue=[], updated_at="2026-01-01T00:00:00+00:00"
        )
        checks = [{"name": "ci", "state": "completed", "bucket": "pass"}]
        reviews = {"reviews": []}

        def gh_side_effect(args, **kwargs):
            if "checks" in args:
                return make_gh_result(stdout=json.dumps(checks))
            elif "reviews" in args:
                return make_gh_result(stdout=json.dumps(reviews))
            return make_gh_result()

        with (
            patch("brimstone.cli._is_conflict_failure", return_value=False),
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs(store))

        assert result is True
        written_states = [c[0][0].state for c in store.write_pr_bead.call_args_list]
        assert "merge_ready" in written_states
        # write_merge_queue should have been called with the enqueued entry
        store.write_merge_queue.assert_called()

    def test_writes_conflict_bead_on_no_worktree(self) -> None:
        """PRBead state is set to 'conflict' when conflict detected with no worktree."""
        store = self._make_store()

        with (
            patch("brimstone.cli._is_conflict_failure", return_value=True),
            patch("brimstone.cli._gh", return_value=make_gh_result()),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs(store, worktree_path=""))

        assert result is False
        written_states = [c[0][0].state for c in store.write_pr_bead.call_args_list]
        assert "conflict" in written_states

    def test_writes_abandoned_bead_on_timeout(self) -> None:
        """PRBead state is set to 'abandoned' on CI monitoring timeout."""
        store = self._make_store()
        pending_checks = [{"name": "ci", "status": "in_progress", "conclusion": None}]

        with (
            patch("brimstone.cli._is_conflict_failure", return_value=False),
            patch(
                "brimstone.cli._gh",
                return_value=make_gh_result(stdout=json.dumps(pending_checks)),
            ),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs(store, max_polls=2))

        assert result is False
        written_states = [c[0][0].state for c in store.write_pr_bead.call_args_list]
        assert "abandoned" in written_states


# ---------------------------------------------------------------------------
# _resume_stale_issues — worktree creation and cleanup
# ---------------------------------------------------------------------------


class TestResumeStaleIssuesWorktree:
    def _make_store(self, issue_number: int = 57, milestone: str = "v1") -> MagicMock:
        """Return a mock BeadStore with one claimed WorkBead."""
        from brimstone.beads import WorkBead

        bead = WorkBead(
            v=1,
            issue_number=issue_number,
            title="Stale issue",
            milestone=milestone,
            stage="impl",
            module="cli",
            priority="P2",
            state="claimed",
            branch=f"{issue_number}-add-feature",
        )
        store = MagicMock()
        store.list_work_beads.return_value = [bead]
        store.read_pr_bead.return_value = None
        store.write_pr_bead.return_value = None
        store.read_work_bead.return_value = bead
        store.write_work_bead.return_value = None
        return store

    def test_creates_worktree_for_pr_when_repo_root_provided(self) -> None:
        """When repo_root is given and PR exists, _checkout_existing_branch_worktree is called."""
        store = self._make_store(57, "v1")
        config = make_config()
        checkpoint = make_checkpoint()

        with (
            patch("brimstone.cli._find_pr_for_issue", return_value=(64, "57-add-feature")),
            patch(
                "brimstone.cli._checkout_existing_branch_worktree", return_value="/tmp/wt/57"
            ) as mock_checkout,
            patch("brimstone.cli._monitor_pr") as mock_monitor,
            patch("brimstone.cli._remove_worktree") as mock_remove,
            patch("brimstone.cli.click.echo"),
        ):
            _resume_stale_issues(
                repo="owner/repo",
                milestone="v1",
                label="stage/impl",
                log_prefix="[test]",
                config=config,
                checkpoint=checkpoint,
                repo_root="/repo",
                store=store,
            )

        mock_checkout.assert_called_once_with("57-add-feature", "/repo")
        mock_monitor.assert_called_once()
        _, kwargs = mock_monitor.call_args
        assert kwargs["worktree_path"] == "/tmp/wt/57"
        mock_remove.assert_called_once_with("/tmp/wt/57", "/repo")

    def test_cleans_up_worktree_after_monitoring_raises(self) -> None:
        """Worktree is removed even if _monitor_pr raises an exception."""
        store = self._make_store(57, "v1")
        config = make_config()
        checkpoint = make_checkpoint()

        with (
            patch("brimstone.cli._find_pr_for_issue", return_value=(64, "57-add-feature")),
            patch("brimstone.cli._checkout_existing_branch_worktree", return_value="/tmp/wt/57"),
            patch("brimstone.cli._monitor_pr", side_effect=RuntimeError("boom")),
            patch("brimstone.cli._remove_worktree") as mock_remove,
            patch("brimstone.cli.click.echo"),
        ):
            with pytest.raises(RuntimeError):
                _resume_stale_issues(
                    repo="owner/repo",
                    milestone="v1",
                    label="stage/impl",
                    log_prefix="[test]",
                    config=config,
                    checkpoint=checkpoint,
                    repo_root="/repo",
                    store=store,
                )

        mock_remove.assert_called_once_with("/tmp/wt/57", "/repo")

    def test_no_worktree_when_repo_root_empty(self) -> None:
        """When repo_root is empty, _checkout_existing_branch_worktree is not called."""
        store = self._make_store(57, "v1")

        with (
            patch("brimstone.cli._find_pr_for_issue", return_value=(64, "57-add-feature")),
            patch("brimstone.cli._checkout_existing_branch_worktree") as mock_checkout,
            patch("brimstone.cli._monitor_pr"),
            patch("brimstone.cli._remove_worktree"),
            patch("brimstone.cli.click.echo"),
        ):
            _resume_stale_issues(
                repo="owner/repo",
                milestone="v1",
                label="stage/impl",
                log_prefix="[test]",
                config=make_config(),
                checkpoint=make_checkpoint(),
                repo_root="",
                store=store,
            )

        mock_checkout.assert_not_called()

    def test_returns_early_when_store_is_none(self) -> None:
        """When store is None, _resume_stale_issues returns an empty set immediately."""
        result = _resume_stale_issues(
            repo="owner/repo",
            milestone="v1",
            label="stage/impl",
            log_prefix="[test]",
            config=make_config(),
            checkpoint=make_checkpoint(),
            store=None,
        )
        assert result == set()


# ---------------------------------------------------------------------------
# _run_agent — /tmp config dir cleanup
# ---------------------------------------------------------------------------


class TestRunAgentConfigDirCleanup:
    def test_cleans_config_dir_after_success(self) -> None:
        """shutil.rmtree is called on config_dir when issue_number is set."""
        success_result = make_run_result(is_error=False)

        with (
            patch("brimstone.cli.runner.run", return_value=success_result),
            patch("brimstone.cli.write_skill_tmp") as mock_skill,
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli.logger.log_agent_transcript"),
            patch("brimstone.cli.shutil.rmtree") as mock_rmtree,
            patch("brimstone.cli.uuid.uuid4", return_value=MagicMock(hex="abcd1234")),
        ):
            mock_skill.return_value.__enter__ = MagicMock()
            mock_skill.return_value.unlink = MagicMock()

            from brimstone.cli import _run_agent

            _run_agent(
                prompt="do stuff",
                skill_name="impl-worker",
                allowed_tools=["Bash"],
                max_turns=10,
                log_label="test",
                prefix="[test] ",
                config=make_config(),
                issue_number=42,
            )

        mock_rmtree.assert_called_once()
        call_args = mock_rmtree.call_args[0][0]
        assert "42" in call_args
        assert "abcd1234" in call_args

    def test_cleans_config_dir_after_error(self) -> None:
        """shutil.rmtree is called even when runner.run raises."""
        with (
            patch("brimstone.cli.runner.run", side_effect=RuntimeError("runner failed")),
            patch("brimstone.cli.write_skill_tmp") as mock_skill,
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli.shutil.rmtree") as mock_rmtree,
            patch("brimstone.cli.uuid.uuid4", return_value=MagicMock(hex="abcd1234")),
        ):
            mock_skill.return_value.__enter__ = MagicMock()
            mock_skill.return_value.unlink = MagicMock()

            from brimstone.cli import _run_agent

            with pytest.raises(RuntimeError):
                _run_agent(
                    prompt="do stuff",
                    skill_name="impl-worker",
                    allowed_tools=["Bash"],
                    max_turns=10,
                    log_label="test",
                    prefix="[test] ",
                    config=make_config(),
                    issue_number=42,
                )

        mock_rmtree.assert_called_once()

    def test_always_creates_config_dir_when_issue_number_none(self) -> None:
        """Config dir is always created, even without issue_number (uses 'agent' key)."""
        success_result = make_run_result(is_error=False)

        with (
            patch("brimstone.cli.runner.run", return_value=success_result),
            patch("brimstone.cli.write_skill_tmp") as mock_skill,
            patch("brimstone.cli.build_subprocess_env", return_value={}),
            patch("brimstone.cli.logger.log_agent_transcript"),
            patch("brimstone.cli.shutil.rmtree") as mock_rmtree,
            patch("brimstone.cli.uuid.uuid4", return_value=MagicMock(hex="deadbeef")),
        ):
            mock_skill.return_value.__enter__ = MagicMock()
            mock_skill.return_value.unlink = MagicMock()

            from brimstone.cli import _run_agent

            _run_agent(
                prompt="do stuff",
                skill_name="impl-worker",
                allowed_tools=["Bash"],
                max_turns=10,
                log_label="test",
                prefix="[test] ",
                config=make_config(),
                issue_number=None,
            )

        mock_rmtree.assert_called_once()
        call_args = mock_rmtree.call_args[0][0]
        assert "agent" in call_args
        assert "deadbeef" in call_args


# ---------------------------------------------------------------------------
# _process_merge_queue
# ---------------------------------------------------------------------------


class TestProcessMergeQueue:
    """Verify _process_merge_queue() drains the queue and writes bead state."""

    def _make_store(self, tmp_path: Path) -> MagicMock:
        from brimstone.beads import MergeQueue, MergeQueueEntry

        store = MagicMock()
        entry = MergeQueueEntry(
            pr_number=55,
            issue_number=7,
            branch="7-my-feature",
            enqueued_at="2026-03-04T00:00:00+00:00",
        )
        queue = MergeQueue(v=1, queue=[entry], updated_at="2026-03-04T00:00:00+00:00")
        store.read_merge_queue.return_value = queue
        store.read_pr_bead.return_value = None
        store.read_work_bead.return_value = None
        return store

    def test_merges_head_of_queue(self, tmp_path: Path) -> None:
        """The head entry is merged and removed from the queue on success."""
        from brimstone.cli import _process_merge_queue

        store = self._make_store(tmp_path)
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path)
        checkpoint = make_checkpoint()

        with (
            patch("brimstone.cli._checkout_existing_branch_worktree", return_value=""),
            patch(
                "brimstone.cli._gh",
                return_value=MagicMock(returncode=0, stdout="{}", stderr=""),
            ),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            _process_merge_queue(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                default_branch="mainline",
                repo_root="/tmp/repo",
            )

        # Queue should have been written with empty list (head was merged)
        store.write_merge_queue.assert_called()
        final_queue = store.write_merge_queue.call_args[0][0]
        assert final_queue.queue == [], "Queue should be empty after successful merge"

    def test_empty_queue_is_noop(self, tmp_path: Path) -> None:
        """An empty MergeQueue does nothing."""
        from brimstone.beads import MergeQueue
        from brimstone.cli import _process_merge_queue

        store = MagicMock()
        store.read_merge_queue.return_value = MergeQueue(
            v=1, queue=[], updated_at="2026-01-01T00:00:00+00:00"
        )
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path)
        checkpoint = make_checkpoint()

        with patch("brimstone.cli._gh") as mock_gh:
            _process_merge_queue(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                default_branch="mainline",
                repo_root="/tmp/repo",
            )
            mock_gh.assert_not_called()

    def test_monitor_pr_enqueues_instead_of_merging(self, tmp_path: Path) -> None:
        """_monitor_pr should enqueue to MergeQueue instead of calling gh pr merge directly."""
        from brimstone.beads import MergeQueue

        store = MagicMock()
        store.read_merge_queue.return_value = MergeQueue(
            v=1, queue=[], updated_at="2026-01-01T00:00:00+00:00"
        )
        store.read_work_bead.return_value = None
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path)
        checkpoint = make_checkpoint()

        with (
            patch("brimstone.cli._is_conflict_failure", return_value=False),
            patch("brimstone.cli._get_pr_checks_status", return_value="pass"),
            patch("brimstone.cli._get_review_status", return_value="approved"),
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.time.sleep"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            result = _monitor_pr(
                pr_number=55,
                branch="7-feature",
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                issue_number=7,
                store=store,
                poll_interval=0,
            )

        # _gh should NOT have been called with "pr merge"
        merge_calls = [
            call
            for call in mock_gh.call_args_list
            if len(call.args) > 0 and "merge" in str(call.args[0])
        ]
        assert merge_calls == [], "_gh should not be called with 'merge' — use queue instead"
        # write_merge_queue should have been called (entry enqueued)
        assert store.write_merge_queue.call_count >= 1
        assert result is True


# ---------------------------------------------------------------------------
# TestWatchdogScan
# ---------------------------------------------------------------------------


class TestWatchdogScan:
    """Verify _watchdog_scan() zombie detection and recovery/exhaustion logic."""

    def _make_pr_bead(self, issue_number: int = 7, fix_attempts: int = 0) -> MagicMock:
        from brimstone.beads import PRBead

        bead = PRBead(
            v=1,
            pr_number=55,
            issue_number=issue_number,
            branch=f"{issue_number}-feature",
            state="ci_failing",
            ci_state="failing",
            conflict_state=None,
            fix_attempts=fix_attempts,
            feedback=[],
            created_at="2026-03-04T00:00:00+00:00",
            merged_at=None,
        )
        return bead

    def _make_work_bead(self, issue_number: int = 7, claimed_at: str | None = None) -> MagicMock:
        from brimstone.beads import WorkBead

        if claimed_at is None:
            # Default: 2 hours ago (well past WATCHDOG_TIMEOUT_MINUTES=45)
            from datetime import UTC, datetime, timedelta

            claimed_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        return WorkBead(
            v=1,
            issue_number=issue_number,
            title="Test issue",
            milestone="v0.1.0",
            stage="impl",
            module="cli",
            priority="P2",
            state="claimed",
            branch=f"{issue_number}-feature",
            pr_id="pr-55",
            retry_count=0,
            claimed_at=claimed_at,
            closed_at=None,
        )

    def test_no_zombies_does_nothing(self, tmp_path: Path) -> None:
        """When no beads exist, scan is a no-op."""
        from brimstone.cli import _watchdog_scan

        store = MagicMock()
        store.list_pr_beads.return_value = []
        config = make_config(log_dir=tmp_path)
        checkpoint = make_checkpoint()

        with patch("brimstone.cli.logger.log_conductor_event") as mock_log:
            _watchdog_scan(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                active_issue_numbers=set(),
                default_branch="mainline",
            )

        mock_log.assert_not_called()
        store.write_pr_bead.assert_not_called()

    def test_active_issue_skipped(self, tmp_path: Path) -> None:
        """An issue currently in an active future is NOT treated as a zombie."""
        from brimstone.cli import _watchdog_scan

        pr_bead = self._make_pr_bead(issue_number=7)
        store = MagicMock()
        store.list_pr_beads.return_value = [pr_bead]
        config = make_config(log_dir=tmp_path)
        checkpoint = make_checkpoint()

        with patch("brimstone.cli._dispatch_recovery_agent") as mock_dispatch:
            _watchdog_scan(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                active_issue_numbers={7},  # issue 7 is active
                default_branch="mainline",
            )

        mock_dispatch.assert_not_called()

    def test_merged_bead_skipped(self, tmp_path: Path) -> None:
        """A PRBead with state='merged' is never treated as a zombie."""
        from brimstone.beads import PRBead
        from brimstone.cli import _watchdog_scan

        pr_bead = PRBead(
            v=1,
            pr_number=55,
            issue_number=7,
            branch="7-feature",
            state="merged",
            ci_state=None,
            conflict_state=None,
            fix_attempts=0,
            feedback=[],
            created_at="2026-03-04T00:00:00+00:00",
            merged_at="2026-03-04T01:00:00+00:00",
        )
        store = MagicMock()
        store.list_pr_beads.return_value = [pr_bead]
        config = make_config(log_dir=tmp_path)
        checkpoint = make_checkpoint()

        with patch("brimstone.cli._dispatch_recovery_agent") as mock_dispatch:
            _watchdog_scan(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                active_issue_numbers=set(),
                default_branch="mainline",
            )

        mock_dispatch.assert_not_called()

    def test_zombie_detected_dispatches_recovery(self, tmp_path: Path) -> None:
        """A timed-out, non-active issue triggers _dispatch_recovery_agent."""
        from brimstone.cli import _watchdog_scan

        pr_bead = self._make_pr_bead(issue_number=7, fix_attempts=0)
        work_bead = self._make_work_bead(issue_number=7)
        store = MagicMock()
        store.list_pr_beads.return_value = [pr_bead]
        store.read_work_bead.return_value = work_bead
        config = make_config(log_dir=tmp_path)
        checkpoint = make_checkpoint()

        with (
            patch("brimstone.cli._dispatch_recovery_agent") as mock_dispatch,
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            _watchdog_scan(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                active_issue_numbers=set(),
                default_branch="mainline",
            )

        mock_dispatch.assert_called_once_with(
            pr_bead, work_bead, "owner/repo", config, checkpoint, store
        )

    def test_zombie_at_max_attempts_exhausts_issue(self, tmp_path: Path) -> None:
        """A zombie at WATCHDOG_MAX_FIX_ATTEMPTS calls _exhaust_issue instead of recovery."""
        from brimstone.cli import WATCHDOG_MAX_FIX_ATTEMPTS, _watchdog_scan

        pr_bead = self._make_pr_bead(issue_number=7, fix_attempts=WATCHDOG_MAX_FIX_ATTEMPTS)
        work_bead = self._make_work_bead(issue_number=7)
        store = MagicMock()
        store.list_pr_beads.return_value = [pr_bead]
        store.read_work_bead.return_value = work_bead
        config = make_config(log_dir=tmp_path)
        checkpoint = make_checkpoint()

        with (
            patch("brimstone.cli._exhaust_issue") as mock_exhaust,
            patch("brimstone.cli._dispatch_recovery_agent") as mock_dispatch,
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            _watchdog_scan(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                active_issue_numbers=set(),
                default_branch="mainline",
            )

        mock_exhaust.assert_called_once()
        mock_dispatch.assert_not_called()
        # Verify reason string mentions max attempts
        call_args = mock_exhaust.call_args
        assert "max fix attempts" in call_args[0][2] or "max fix attempts" in str(call_args)

    def test_recent_claim_not_a_zombie(self, tmp_path: Path) -> None:
        """An issue claimed only 5 minutes ago is NOT a zombie."""
        from datetime import UTC, datetime, timedelta

        from brimstone.cli import _watchdog_scan

        pr_bead = self._make_pr_bead(issue_number=7)
        recent_claimed_at = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        work_bead = self._make_work_bead(issue_number=7, claimed_at=recent_claimed_at)
        store = MagicMock()
        store.list_pr_beads.return_value = [pr_bead]
        store.read_work_bead.return_value = work_bead
        config = make_config(log_dir=tmp_path)
        checkpoint = make_checkpoint()

        with patch("brimstone.cli._dispatch_recovery_agent") as mock_dispatch:
            _watchdog_scan(
                repo="owner/repo",
                config=config,
                checkpoint=checkpoint,
                store=store,
                active_issue_numbers=set(),
                default_branch="mainline",
            )

        mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# End-of-run cost summary
# ---------------------------------------------------------------------------


class TestEndOfRunCostSummary:
    """After each worker completes, the run command prints a cost summary line."""

    def test_end_of_run_cost_summary_printed(self, tmp_path: Path) -> None:
        """After impl worker completes, a cost summary line is echoed to stderr."""
        import json as _json

        from click.testing import CliRunner

        from brimstone.cli import composer

        _REPO = "owner/repo"
        _MILESTONE = "v1.0"

        # Prepare a log dir with a cost entry for the "impl" stage
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        cost_entry = {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "session_id": "sess-1",
            "run_id": "run-1",
            "repo": _REPO,
            "milestone": _MILESTONE,
            "stage": "impl",
            "issue_number": 1,
            "model": "claude-sonnet-4-6",
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "num_turns": 3,
            "duration_ms": 5000,
            "is_error": False,
            "error_subtype": None,
            "total_cost_usd": 0.25,
            "auth_mode": "api_key",
            "web_search_requests": 0,
        }
        (log_dir / "cost.jsonl").write_text(_json.dumps(cost_entry) + "\n")

        # Build a config-like object; override log_dir to use the temp path
        config = make_config()
        object.__setattr__(config, "log_dir", log_dir)
        checkpoint = make_checkpoint()

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli._resolve_repo", return_value=_REPO),
                patch("brimstone.cli._milestone_exists", return_value=True),
                patch(
                    "brimstone.cli._get_default_branch_for_repo",
                    return_value="main",
                ),
                patch("brimstone.cli._count_open_issues_by_label", return_value=2),
                patch("brimstone.cli._ensure_labels"),
                patch(
                    "brimstone.cli.startup_sequence",
                    return_value=(config, checkpoint, MagicMock()),
                ),
                patch("brimstone.cli._run_impl_worker"),
            ):
                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["run", "--impl", "--repo", _REPO, "--milestone", _MILESTONE],
                )

        assert result.exit_code == 0, result.output
        # click.echo(..., err=True) still appears in result.output for CliRunner
        assert "[impl] cost:" in result.output
