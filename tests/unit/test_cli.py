"""Unit tests for src/brimstone/cli.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from brimstone import session
from brimstone.cli import (
    UsageGovernor,
    _apply_headless_policy,
    inject_skill,
    startup_sequence,
)
from brimstone.config import Config, OrchestratorNestingError
from brimstone.health import FatalHealthCheckError
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
    env = dict(MINIMAL_ENV)
    env.update(overrides)
    with patch.dict("os.environ", env, clear=False):
        return Config(
            anthropic_api_key=env["ANTHROPIC_API_KEY"],
            github_token=env["BRIMSTONE_GH_TOKEN"],
        )


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


# ---------------------------------------------------------------------------
# inject_skill
# ---------------------------------------------------------------------------


class TestInjectSkill:
    def test_reads_correct_file_and_prepends_skill_text(self) -> None:
        """inject_skill reads the named skill file and prepends it after base_prompt."""
        fake_skill_content = "# Skill\nDo the thing."
        base = "## Session Parameters\n- repo: owner/repo"

        with patch.object(Path, "read_text", return_value=fake_skill_content):
            result = inject_skill("research-worker", base)

        assert result.startswith(base)
        assert "\n\n---\n\n" in result
        assert fake_skill_content in result

    def test_raises_file_not_found_for_missing_skill(self) -> None:
        """inject_skill raises FileNotFoundError for unknown skill names."""
        with pytest.raises(FileNotFoundError):
            inject_skill("nonexistent-skill-xyz", "base prompt")

    def test_applies_headless_policy(self) -> None:
        """inject_skill applies _apply_headless_policy to the skill text."""
        fake_skill = "Ask the user before proceeding."
        base = "base"

        with patch.object(Path, "read_text", return_value=fake_skill):
            result = inject_skill("research-worker", base)

        assert "Ask the user" not in result
        assert "Auto-resolve:" in result

    def test_real_research_worker_skill_file_exists(self) -> None:
        """The real research-worker.md skill file exists and is readable."""
        result = inject_skill("research-worker", "base")
        assert "---" in result
        assert len(result) > 100

    def test_real_impl_worker_skill_file_exists(self) -> None:
        """The real impl-worker.md skill file exists and is readable."""
        result = inject_skill("impl-worker", "base")
        assert "---" in result
        assert len(result) > 100


# ---------------------------------------------------------------------------
# _apply_headless_policy
# ---------------------------------------------------------------------------


class TestApplyHeadlessPolicy:
    def test_replaces_ask_the_user_capitalized(self) -> None:
        text = "Ask the user before cleaning up."
        assert "Auto-resolve:" in _apply_headless_policy(text)
        assert "Ask the user" not in _apply_headless_policy(text)

    def test_replaces_ask_the_user_lowercase(self) -> None:
        text = "You should ask the user for confirmation."
        result = _apply_headless_policy(text)
        assert "Auto-resolve:" in result
        assert "ask the user" not in result

    def test_replaces_confirm_with_user(self) -> None:
        text = "confirm with user before proceeding."
        result = _apply_headless_policy(text)
        assert "Auto-resolve:" in result
        assert "confirm with user" not in result

    def test_replaces_wait_for_approval(self) -> None:
        text = "Wait for approval before continuing."
        result = _apply_headless_policy(text)
        assert "Proceed automatically:" in result
        assert "Wait for approval" not in result

    def test_replaces_await_user_confirmation(self) -> None:
        text = "await user confirmation before the next step."
        result = _apply_headless_policy(text)
        assert "Proceed automatically:" in result
        assert "await user confirmation" not in result

    def test_no_change_on_unmatched_text(self) -> None:
        text = "Proceed automatically without any gates."
        assert _apply_headless_policy(text) == text

    def test_multiple_replacements_in_one_pass(self) -> None:
        text = "Ask the user. Then await user confirmation."
        result = _apply_headless_policy(text)
        assert "Ask the user" not in result
        assert "await user confirmation" not in result
        assert result.count("Auto-resolve:") >= 1
        assert result.count("Proceed automatically:") >= 1


# ---------------------------------------------------------------------------
# UsageGovernor.can_dispatch
# ---------------------------------------------------------------------------


class TestUsageGovernorCanDispatch:
    def _make_governor(self, **config_overrides) -> UsageGovernor:
        config = make_config()
        # Override attributes directly (Config is a pydantic model)
        for k, v in config_overrides.items():
            object.__setattr__(config, k, v)
        checkpoint = make_checkpoint()
        return UsageGovernor(config=config, checkpoint=checkpoint)

    def test_returns_true_when_no_gates_apply(self) -> None:
        gov = self._make_governor()
        assert gov.can_dispatch() is True

    def test_returns_false_when_backing_off(self) -> None:
        gov = self._make_governor()
        # Inject a future backoff timestamp into the checkpoint
        from datetime import UTC, datetime, timedelta

        future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        gov.checkpoint.rate_limit_backoff_until = future
        assert gov.can_dispatch() is False

    def test_returns_false_at_concurrency_limit(self) -> None:
        gov = self._make_governor(subscription_tier="pro", max_concurrency=0)
        # max_concurrency=0 means limits dict fallback: pro->2
        # But since max_concurrency is 0 (falsy), limits.get("pro", 3) = 2
        # active_agents=0, n_agents=1 → 0+1 > 2? No. Let's set active high.
        gov._active_agents = 10
        assert gov.can_dispatch(1) is False

    def test_returns_false_when_exactly_at_concurrency_limit(self) -> None:
        gov = self._make_governor(subscription_tier="pro", max_concurrency=0)
        # limits["pro"] = 2
        gov._active_agents = 2
        assert gov.can_dispatch(1) is False

    def test_returns_false_when_budget_exhausted(self) -> None:
        gov = self._make_governor(max_budget_usd=5.0, max_concurrency=0)
        gov._total_cost_usd = 5.0
        assert gov.can_dispatch() is False

    def test_returns_true_when_budget_not_yet_exhausted(self) -> None:
        gov = self._make_governor(max_budget_usd=10.0, max_concurrency=0)
        gov._total_cost_usd = 4.99
        assert gov.can_dispatch() is True

    def test_concurrency_tier_pro(self) -> None:
        """pro tier uses limit 2."""
        gov = self._make_governor(subscription_tier="pro", max_concurrency=0)
        gov._active_agents = 1
        assert gov.can_dispatch(1) is True  # 1+1 == 2, not > 2
        gov._active_agents = 2
        assert gov.can_dispatch(1) is False  # 2+1 > 2

    def test_concurrency_tier_max(self) -> None:
        """max tier uses limit 3."""
        gov = self._make_governor(subscription_tier="max", max_concurrency=0)
        gov._active_agents = 2
        assert gov.can_dispatch(1) is True
        gov._active_agents = 3
        assert gov.can_dispatch(1) is False

    def test_concurrency_tier_max20x(self) -> None:
        """max20x tier uses limit 5."""
        gov = self._make_governor(subscription_tier="max20x", max_concurrency=0)
        gov._active_agents = 4
        assert gov.can_dispatch(1) is True
        gov._active_agents = 5
        assert gov.can_dispatch(1) is False

    def test_explicit_max_concurrency_overrides_tier(self) -> None:
        """Explicit max_concurrency in config overrides the tier default."""
        gov = self._make_governor(max_concurrency=10)
        gov._active_agents = 9
        assert gov.can_dispatch(1) is True
        gov._active_agents = 10
        assert gov.can_dispatch(1) is False


# ---------------------------------------------------------------------------
# UsageGovernor.record_429
# ---------------------------------------------------------------------------


class TestUsageGovernorRecord429:
    def test_record_429_sets_backoff_on_checkpoint(self) -> None:
        config = make_config()
        checkpoint = make_checkpoint()
        gov = UsageGovernor(config=config, checkpoint=checkpoint)

        assert checkpoint.rate_limit_backoff_until is None
        gov.record_429(attempt=0)
        assert checkpoint.rate_limit_backoff_until is not None

    def test_record_429_checkpoint_is_in_backoff_after_call(self) -> None:
        from brimstone.session import is_backing_off

        config = make_config()
        checkpoint = make_checkpoint()
        gov = UsageGovernor(config=config, checkpoint=checkpoint)

        gov.record_429(attempt=0)
        assert is_backing_off(checkpoint) is True

    def test_record_429_higher_attempt_sets_longer_backoff(self) -> None:
        from datetime import UTC, datetime

        config = make_config()
        checkpoint0 = make_checkpoint()
        checkpoint1 = make_checkpoint()

        gov0 = UsageGovernor(config=config, checkpoint=checkpoint0)
        gov1 = UsageGovernor(config=config, checkpoint=checkpoint1)

        gov0.record_429(attempt=0)
        gov1.record_429(attempt=3)

        until0 = datetime.fromisoformat(checkpoint0.rate_limit_backoff_until)  # type: ignore[arg-type]
        until1 = datetime.fromisoformat(checkpoint1.rate_limit_backoff_until)  # type: ignore[arg-type]
        now = datetime.now(UTC)

        # Higher attempt should yield a longer wait time
        assert (until1 - now) > (until0 - now)


# ---------------------------------------------------------------------------
# startup_sequence
# ---------------------------------------------------------------------------


class TestStartupSequence:
    def test_aborts_on_fatal_health_check(self, tmp_path: Path) -> None:
        """startup_sequence raises FatalHealthCheckError when health check is fatal."""
        from brimstone.health import CheckResult, HealthReport

        fatal_report = HealthReport(
            checks=[
                CheckResult(
                    name="gh CLI authenticated",
                    status="fail",
                    message="gh not authenticated",
                    remediation="Run: gh auth login",
                )
            ],
            overall="fail",
            fatal=True,
        )

        config = make_config()
        checkpoint_path = tmp_path / "current.json"

        with (
            patch.dict("os.environ", {"CLAUDECODE": ""}, clear=False),
            patch("brimstone.cli.health.check_all", return_value=fatal_report),
            patch("brimstone.cli.health.format_report", return_value="FATAL"),
            patch("brimstone.cli.click.echo"),
        ):
            with pytest.raises(FatalHealthCheckError):
                startup_sequence(
                    config=config,
                    checkpoint_path=checkpoint_path,
                    milestone="MVP",
                    stage="research",
                )

    def test_proceeds_with_warning_health_check(self, tmp_path: Path) -> None:
        """startup_sequence continues (and prints) when health check has only warnings."""
        from brimstone.health import CheckResult, HealthReport

        warn_report = HealthReport(
            checks=[
                CheckResult(
                    name="No active worktrees",
                    status="warn",
                    message="1 worktree found",
                )
            ],
            overall="warn",
            fatal=False,
        )

        config = make_config()
        checkpoint_path = tmp_path / "current.json"

        with (
            patch.dict("os.environ", {"CLAUDECODE": ""}, clear=False),
            patch("brimstone.cli.health.check_all", return_value=warn_report),
            patch("brimstone.cli.health.format_report", return_value="WARN"),
            patch("brimstone.cli.health.acquire_orchestrator_lock"),
            patch("brimstone.cli.logger.log_conductor_event"),
            patch("brimstone.cli.click.echo") as mock_echo,
        ):
            _cfg, _chk = startup_sequence(
                config=config,
                checkpoint_path=checkpoint_path,
                milestone="MVP",
                stage="research",
            )

        # Should have echoed the warning report
        mock_echo.assert_called()

    def test_raises_orchestrator_nesting_error_when_claudecode_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """startup_sequence raises OrchestratorNestingError when CLAUDECODE=1."""
        monkeypatch.setenv("CLAUDECODE", "1")
        config = make_config()
        checkpoint_path = tmp_path / "current.json"

        with pytest.raises(OrchestratorNestingError):
            startup_sequence(
                config=config,
                checkpoint_path=checkpoint_path,
                milestone="MVP",
                stage="research",
            )

    def test_raises_value_error_on_run_id_mismatch(self, tmp_path: Path) -> None:
        """startup_sequence raises ValueError when resume_run_id doesn't match checkpoint."""
        from brimstone.health import CheckResult, HealthReport

        pass_report = HealthReport(
            checks=[CheckResult(name="Git repo present", status="pass", message="ok")],
            overall="pass",
            fatal=False,
        )

        # Write a checkpoint with a specific run_id
        existing_checkpoint = make_checkpoint(run_id="actual-run-id")
        checkpoint_path = tmp_path / "current.json"
        session.save(existing_checkpoint, checkpoint_path)

        config = make_config()

        with (
            patch.dict("os.environ", {"CLAUDECODE": ""}, clear=False),
            patch("brimstone.cli.health.check_all", return_value=pass_report),
            patch("brimstone.cli.health.acquire_orchestrator_lock"),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            with pytest.raises(ValueError, match="run_id mismatch"):
                startup_sequence(
                    config=config,
                    checkpoint_path=checkpoint_path,
                    milestone="MVP",
                    stage="research",
                    resume_run_id="wrong-run-id",
                )

    def test_creates_new_checkpoint_when_none_exists(self, tmp_path: Path) -> None:
        """startup_sequence creates a new checkpoint when file does not exist."""
        from brimstone.health import CheckResult, HealthReport

        pass_report = HealthReport(
            checks=[CheckResult(name="Git repo present", status="pass", message="ok")],
            overall="pass",
            fatal=False,
        )

        config = make_config()
        checkpoint_path = tmp_path / "current.json"
        assert not checkpoint_path.exists()

        with (
            patch.dict("os.environ", {"CLAUDECODE": ""}, clear=False),
            patch("brimstone.cli.health.check_all", return_value=pass_report),
            patch("brimstone.cli.health.acquire_orchestrator_lock"),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            _cfg, chk = startup_sequence(
                config=config,
                checkpoint_path=checkpoint_path,
                milestone="MVP Research",
                stage="research",
            )

        assert chk.run_id != ""
        assert chk.milestone == "MVP Research"
        assert chk.stage == "research"

    def test_loads_existing_checkpoint(self, tmp_path: Path) -> None:
        """startup_sequence loads an existing checkpoint from disk."""
        from brimstone.health import CheckResult, HealthReport

        pass_report = HealthReport(
            checks=[CheckResult(name="Git repo present", status="pass", message="ok")],
            overall="pass",
            fatal=False,
        )

        existing_chk = make_checkpoint(run_id="existing-run-id", milestone="MVP Research")
        checkpoint_path = tmp_path / "current.json"
        session.save(existing_chk, checkpoint_path)

        config = make_config()

        with (
            patch.dict("os.environ", {"CLAUDECODE": ""}, clear=False),
            patch("brimstone.cli.health.check_all", return_value=pass_report),
            patch("brimstone.cli.health.acquire_orchestrator_lock"),
            patch("brimstone.cli.logger.log_conductor_event"),
        ):
            _cfg, chk = startup_sequence(
                config=config,
                checkpoint_path=checkpoint_path,
                milestone="MVP Research",
                stage="research",
                resume_run_id="existing-run-id",
            )

        assert chk.run_id == "existing-run-id"


# ---------------------------------------------------------------------------
# brimstone --help lists all subcommands
# ---------------------------------------------------------------------------


class TestComposerHelp:
    def test_composer_help_lists_all_subcommands(self) -> None:
        """composer --help must list all subcommands with descriptions."""
        from click.testing import CliRunner

        from brimstone.cli import composer

        runner = CliRunner()
        result = runner.invoke(composer, ["--help"])

        assert result.exit_code == 0
        output = result.output

        # New unified interface subcommands
        assert "run" in output
        assert "init" in output
        assert "adopt" in output
        assert "health" in output
        assert "cost" in output
