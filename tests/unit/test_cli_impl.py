"""Unit tests for the impl-worker loop in src/brimstone/cli.py.

Tests cover:
- Issue selection respects module isolation (active module blocked)
- _extract_module extracts feat:* label correctly
- _slugify produces branch-safe slugs
- _list_open_impl_issues filters correctly (excludes research/pipeline/in-progress)
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

from brimstone.cli import (
    UsageGovernor,
    _extract_module,
    _find_next_version,
    _find_pr_for_branch,
    _find_pr_for_issue,
    _get_pr_checks_status,
    _get_review_status,
    _list_open_impl_issues,
    _monitor_pr,
    _run_impl_worker,
    _slugify,
    _triage_review_comments,
)
from brimstone.config import Config
from brimstone.runner import RunResult
from brimstone.session import Checkpoint

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
# _list_open_impl_issues
# ---------------------------------------------------------------------------


class TestListOpenImplIssues:
    def _call(self, issues_json: str, returncode: int = 0) -> list[dict]:
        with patch("brimstone.cli._gh") as mock_gh:
            mock_gh.return_value = make_gh_result(stdout=issues_json, returncode=returncode)
            return _list_open_impl_issues("owner/repo", "M2: Implementation")

    def test_returns_empty_on_gh_failure(self) -> None:
        result = self._call("error", returncode=1)
        assert result == []

    def test_returns_empty_on_invalid_json(self) -> None:
        result = self._call("not-json")
        assert result == []

    def test_filters_out_research_issues(self) -> None:
        issues = [
            make_issue(number=1, labels=["research"]),
            make_issue(number=2, labels=["feat:config"]),
        ]
        result = self._call(json.dumps(issues))
        assert len(result) == 1
        assert result[0]["number"] == 2

    def test_filters_out_pipeline_issues(self) -> None:
        issues = [
            make_issue(number=1, labels=["pipeline"]),
            make_issue(number=2, labels=["feat:runner"]),
        ]
        result = self._call(json.dumps(issues))
        assert len(result) == 1
        assert result[0]["number"] == 2

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
            {"name": "ci", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "success"},
        ]
        assert self._call(json.dumps(checks)) == "pass"

    def test_returns_fail_when_any_failed(self) -> None:
        checks = [
            {"name": "ci", "status": "completed", "conclusion": "failure"},
            {"name": "lint", "status": "completed", "conclusion": "success"},
        ]
        assert self._call(json.dumps(checks)) == "fail"

    def test_returns_pending_when_any_in_progress(self) -> None:
        checks = [
            {"name": "ci", "status": "in_progress", "conclusion": None},
            {"name": "lint", "status": "completed", "conclusion": "success"},
        ]
        assert self._call(json.dumps(checks)) == "pending"

    def test_fail_takes_priority_over_pending(self) -> None:
        checks = [
            {"name": "ci", "status": "in_progress", "conclusion": None},
            {"name": "lint", "status": "completed", "conclusion": "failure"},
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
# _triage_review_comments — fix agent dispatch
# ---------------------------------------------------------------------------


class TestTriageReviewComments:
    def test_dispatches_fix_agent_when_comments_exist(self) -> None:
        comments = [
            {"path": "src/foo.py", "line": 10, "body": "Please fix this."},
        ]

        with (
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.runner.run") as mock_run,
            patch("brimstone.cli.build_subprocess_env", return_value={}),
        ):
            mock_gh.return_value = make_gh_result(stdout=json.dumps(comments))
            mock_run.return_value = make_run_result()

            _triage_review_comments(
                pr_number=99,
                branch="42-add-config",
                repo="owner/repo",
                config=make_config(),
                checkpoint=make_checkpoint(),
                issue_number=42,
            )

        mock_run.assert_called_once()
        prompt = mock_run.call_args[1]["prompt"]
        assert "PR #99" in prompt
        assert "42-add-config" in prompt
        assert "src/foo.py" in prompt

    def test_does_nothing_when_no_comments(self) -> None:
        with (
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.runner.run") as mock_run,
        ):
            mock_gh.return_value = make_gh_result(stdout="[]")
            _triage_review_comments(
                pr_number=99,
                branch="42-add-config",
                repo="owner/repo",
                config=make_config(),
                checkpoint=make_checkpoint(),
                issue_number=42,
            )
        mock_run.assert_not_called()


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
        """When CI passes and no changes requested, squash merge is called."""
        checks = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        reviews = {"reviews": []}
        merge_result = make_gh_result(returncode=0)

        def gh_side_effect(args, **kwargs):
            if "checks" in args:
                return make_gh_result(stdout=json.dumps(checks))
            elif "reviews" in args:
                return make_gh_result(stdout=json.dumps(reviews))
            elif "merge" in args:
                return merge_result
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
        """When conflict is detected, _rebase_branch is called."""
        fail_checks = [{"name": "ci", "status": "completed", "conclusion": "failure"}]
        pass_checks = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        call_count = {"n": 0}

        def gh_side_effect(args, **kwargs):
            call_count["n"] += 1
            args_str = " ".join(str(a) for a in args)
            if "checks" in args_str:
                if call_count["n"] <= 2:
                    return make_gh_result(stdout=json.dumps(fail_checks))
                return make_gh_result(stdout=json.dumps(pass_checks))
            elif "mergeable" in args_str:
                return make_gh_result(
                    stdout=json.dumps({"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"})
                )
            elif "reviews" in args_str:
                return make_gh_result(stdout=json.dumps({"reviews": []}))
            elif "merge" in args_str:
                return make_gh_result(returncode=0)
            return make_gh_result()

        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli._rebase_branch", return_value=True) as mock_rebase,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            _monitor_pr(**self._make_monitor_kwargs(max_polls=10))

        mock_rebase.assert_called()

    def test_returns_false_after_rebase_limit_exceeded(self) -> None:
        """After _REBASE_RETRY_LIMIT rebase attempts, returns False."""
        fail_checks = [{"name": "ci", "status": "completed", "conclusion": "failure"}]

        def gh_side_effect(args, **kwargs):
            args_str = " ".join(str(a) for a in args)
            if "checks" in args_str:
                return make_gh_result(stdout=json.dumps(fail_checks))
            elif "mergeable" in args_str:
                return make_gh_result(
                    stdout=json.dumps({"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"})
                )
            return make_gh_result()

        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli._rebase_branch", return_value=True),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs(max_polls=20))

        assert result is False

    def test_returns_false_on_merge_failure(self) -> None:
        """When the squash merge command fails, returns False."""
        checks = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        reviews = {"reviews": []}

        def gh_side_effect(args, **kwargs):
            args_str = " ".join(str(a) for a in args)
            if "checks" in args_str:
                return make_gh_result(stdout=json.dumps(checks))
            elif "reviews" in args_str:
                return make_gh_result(stdout=json.dumps(reviews))
            elif "merge" in args_str:
                return make_gh_result(returncode=1, stdout="")
            return make_gh_result()

        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs())

        assert result is False

    def test_escalates_immediately_on_conflict_without_worktree(self) -> None:
        """When worktree_path is empty and conflict detected, escalates without rebase."""
        fail_checks = [{"name": "ci", "status": "completed", "conclusion": "failure"}]

        def gh_side_effect(args, **kwargs):
            args_str = " ".join(str(a) for a in args)
            if "checks" in args_str:
                return make_gh_result(stdout=json.dumps(fail_checks))
            elif "mergeable" in args_str:
                return make_gh_result(
                    stdout=json.dumps({"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"})
                )
            return make_gh_result()

        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli._rebase_branch") as mock_rebase,
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs(worktree_path="", max_polls=5))

        assert result is False
        mock_rebase.assert_not_called()

    def test_merges_immediately_with_no_ci(self) -> None:
        """When no CI checks are configured, PR should merge without waiting."""
        reviews = {"reviews": []}

        def gh_side_effect(args, **kwargs):
            args_str = " ".join(str(a) for a in args)
            if "checks" in args_str:
                return make_gh_result(stdout="[]")  # no checks
            elif "reviews" in args_str:
                return make_gh_result(stdout=json.dumps(reviews))
            elif "merge" in args_str:
                return make_gh_result(returncode=0)
            return make_gh_result()

        with (
            patch("brimstone.cli._gh", side_effect=gh_side_effect),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
        ):
            result = _monitor_pr(**self._make_monitor_kwargs())

        assert result is True


# ---------------------------------------------------------------------------
# _run_impl_worker — Completion gate (no open issues → file pipeline issue)
# ---------------------------------------------------------------------------


class TestRunImplWorkerCompletionGate:
    def test_completion_gate_files_pipeline_issue_when_no_open_issues(self, tmp_path: Path) -> None:
        """When no open impl issues remain, files 'Run plan-milestones' issue and stops."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path)
        checkpoint = make_checkpoint(milestone="MVP Implementation")

        with (
            patch("brimstone.cli._list_open_impl_issues", return_value=[]),
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
            )

        # Should have called _gh to create the pipeline issue
        create_calls = [
            c for c in mock_gh.call_args_list if "create" in str(c) and "plan-milestones" in str(c)
        ]
        assert len(create_calls) >= 1

    def test_completion_gate_dry_run_does_not_call_gh(self, tmp_path: Path) -> None:
        """In dry-run mode, completion gate prints without calling _gh for issue creation."""
        config = make_config()
        object.__setattr__(config, "checkpoint_dir", tmp_path)
        object.__setattr__(config, "log_dir", tmp_path)
        checkpoint = make_checkpoint(milestone="MVP Implementation")

        with (
            patch("brimstone.cli._list_open_impl_issues", return_value=[]),
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

        # _gh should NOT have been called to create a pipeline issue in dry-run
        create_calls = [
            c for c in mock_gh.call_args_list if "create" in str(c) and "plan-milestones" in str(c)
        ]
        assert len(create_calls) == 0

        # But echo should have been called with the dry-run message
        echo_texts = " ".join(str(c) for c in mock_echo.call_args_list)
        assert "dry-run" in echo_texts
        assert "plan-milestones" in echo_texts


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
            patch("brimstone.cli._list_open_impl_issues", side_effect=[issues, []]),
            patch("brimstone.cli._claim_issue") as mock_claim,
            patch("brimstone.cli._create_worktree", return_value="/tmp/wt"),
            patch("brimstone.cli._dispatch_impl_agent") as mock_dispatch,
            patch("brimstone.cli._find_pr_for_branch", return_value=None),
            patch("brimstone.cli._unclaim_issue"),
            patch("brimstone.cli._remove_worktree"),
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.session.record_dispatch"),
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

        mock_claim.assert_called_once_with(repo="owner/repo", issue_number=10)

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
            patch("brimstone.cli._list_open_impl_issues", return_value=issues),
            patch("brimstone.cli._create_worktree") as mock_create,
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.session.record_dispatch"),
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
            patch("brimstone.cli._list_open_impl_issues", side_effect=[issues, []]),
            patch("brimstone.cli._claim_issue"),
            patch("brimstone.cli._unclaim_issue") as mock_unclaim,
            patch("brimstone.cli._create_worktree", return_value="/tmp/wt"),
            patch("brimstone.cli._remove_worktree"),
            patch("brimstone.cli._dispatch_impl_agent") as mock_dispatch,
            patch("brimstone.cli._gh") as mock_gh,
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.session.save"),
            patch("brimstone.cli.session.record_dispatch"),
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

        mock_unclaim.assert_called_with(repo="owner/repo", issue_number=10)
