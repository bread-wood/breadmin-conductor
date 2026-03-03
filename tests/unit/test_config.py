"""Unit tests for src/brimstone/config.py."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from brimstone.config import (
    Config,
    ConfigurationError,
    OrchestratorNestingError,
    build_subprocess_env,
    load_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
    "BRIMSTONE_GH_TOKEN": "ghp-test-token",
}


def make_env(**overrides: str) -> dict[str, str]:
    """Return a minimal valid environment, optionally with overrides."""
    env = dict(MINIMAL_ENV)
    env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# Config loading — happy path
# ---------------------------------------------------------------------------


def test_config_loads_with_all_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config instantiates when all required env vars are present."""
    for key, val in make_env().items():
        monkeypatch.setenv(key, val)

    config = Config()
    assert config.anthropic_api_key == "sk-ant-test-key"
    assert config.github_token == "ghp-test-token"


def test_load_config_returns_config_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_config() returns a Config object when env is valid."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    for key, val in make_env().items():
        monkeypatch.setenv(key, val)

    config = load_config()
    assert isinstance(config, Config)


def test_config_defaults_are_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default values are set when optional vars are not in the environment."""
    for key, val in make_env().items():
        monkeypatch.setenv(key, val)

    config = Config()
    assert config.max_budget_usd == 5.00
    assert config.max_retries == 3
    assert config.max_concurrency == 5
    assert config.backoff_base_seconds == 2.0
    assert config.backoff_max_minutes == 32.0
    assert config.agent_timeout_minutes == 30.0
    assert config.subscription_tier == "pro"
    assert config.api_key_helper is None


def test_config_paths_default_to_home_brimstone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default log_dir and checkpoint_dir point to ~/.brimstone/*."""
    for key, val in make_env().items():
        monkeypatch.setenv(key, val)

    config = Config()
    assert config.log_dir == Path("~/.brimstone/logs")
    assert config.checkpoint_dir == Path("~/.brimstone/checkpoints")


# ---------------------------------------------------------------------------
# Config loading — BRIMSTONE_* overrides
# ---------------------------------------------------------------------------


def test_conductor_max_concurrency_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """BRIMSTONE_MAX_CONCURRENCY overrides the default."""
    for key, val in make_env(BRIMSTONE_MAX_CONCURRENCY="5").items():
        monkeypatch.setenv(key, val)

    config = Config()
    assert config.max_concurrency == 5


def test_conductor_max_concurrency_custom_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom BRIMSTONE_MAX_CONCURRENCY value is reflected in the config."""
    for key, val in make_env(BRIMSTONE_MAX_CONCURRENCY="8").items():
        monkeypatch.setenv(key, val)

    config = Config()
    assert config.max_concurrency == 8


def test_conductor_subscription_tier_max(monkeypatch: pytest.MonkeyPatch) -> None:
    """BRIMSTONE_SUBSCRIPTION_TIER=max is accepted."""
    for key, val in make_env(BRIMSTONE_SUBSCRIPTION_TIER="max").items():
        monkeypatch.setenv(key, val)

    config = Config()
    assert config.subscription_tier == "max"


def test_conductor_subscription_tier_max20x(monkeypatch: pytest.MonkeyPatch) -> None:
    """BRIMSTONE_SUBSCRIPTION_TIER=max20x is accepted."""
    for key, val in make_env(BRIMSTONE_SUBSCRIPTION_TIER="max20x").items():
        monkeypatch.setenv(key, val)

    config = Config()
    assert config.subscription_tier == "max20x"


# ---------------------------------------------------------------------------
# Config loading — validation errors
# ---------------------------------------------------------------------------


def test_missing_anthropic_api_key_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_config() raises ConfigurationError when ANTHROPIC_API_KEY is absent."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("BRIMSTONE_GH_TOKEN", "ghp-test-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert "ANTHROPIC_API_KEY" in str(exc_info.value)


def test_missing_github_token_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_config() raises ConfigurationError when BRIMSTONE_GH_TOKEN is absent."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("BRIMSTONE_GH_TOKEN", raising=False)

    with pytest.raises(ConfigurationError) as exc_info:
        load_config()

    assert "BRIMSTONE_GH_TOKEN" in str(exc_info.value)


def test_invalid_subscription_tier_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognised BRIMSTONE_SUBSCRIPTION_TIER raises ConfigurationError."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    for key, val in make_env(BRIMSTONE_SUBSCRIPTION_TIER="enterprise").items():
        monkeypatch.setenv(key, val)

    with pytest.raises(ConfigurationError):
        load_config()


def test_invalid_max_budget_type_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric BRIMSTONE_MAX_BUDGET_USD raises ConfigurationError."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    for key, val in make_env(BRIMSTONE_MAX_BUDGET_USD="not-a-number").items():
        monkeypatch.setenv(key, val)

    with pytest.raises(ConfigurationError):
        load_config()


# ---------------------------------------------------------------------------
# Nesting guard
# ---------------------------------------------------------------------------


def test_claudecode_env_raises_nested_orchestrator_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_config() raises OrchestratorNestingError when CLAUDECODE=1."""
    for key, val in make_env().items():
        monkeypatch.setenv(key, val)
    monkeypatch.setenv("CLAUDECODE", "1")

    with pytest.raises(OrchestratorNestingError):
        load_config()


def test_claudecode_env_not_1_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDECODE=0 does not trigger the nesting guard."""
    monkeypatch.setenv("CLAUDECODE", "0")
    for key, val in make_env().items():
        monkeypatch.setenv(key, val)

    config = load_config()
    assert isinstance(config, Config)


def test_claudecode_absent_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Absent CLAUDECODE env var does not trigger the nesting guard."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    for key, val in make_env().items():
        monkeypatch.setenv(key, val)

    config = load_config()
    assert isinstance(config, Config)


# ---------------------------------------------------------------------------
# build_subprocess_env — allowlist enforcement
# ---------------------------------------------------------------------------


def _make_config(**overrides: object) -> Config:
    """Construct a Config instance directly, bypassing the nesting guard."""
    return Config(
        anthropic_api_key="sk-ant-test-key",
        github_token="ghp-test-token",
        **overrides,
    )


def test_build_subprocess_env_does_not_include_parent_env_secrets() -> None:
    """build_subprocess_env never exposes arbitrary parent env vars."""
    config = _make_config()

    # Pollute the parent env with secrets that must not leak
    with patch.dict(
        os.environ,
        {
            "AWS_SECRET_ACCESS_KEY": "top-secret",
            "DATABASE_URL": "postgres://user:pass@host/db",
            "GOOGLE_API_KEY": "google-key",
            "BRIMSTONE_SOME_INTERNAL": "internal-value",
            "SSH_AUTH_SOCK": "/tmp/ssh-agent.sock",
        },
    ):
        env = build_subprocess_env(config)

    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "DATABASE_URL" not in env
    assert "GOOGLE_API_KEY" not in env
    assert "BRIMSTONE_SOME_INTERNAL" not in env
    assert "SSH_AUTH_SOCK" not in env


def test_build_subprocess_env_excludes_claudecode() -> None:
    """CLAUDECODE must never appear in the subprocess env dict."""
    config = _make_config()

    with patch.dict(os.environ, {"CLAUDECODE": "1"}):
        env = build_subprocess_env(config)

    assert "CLAUDECODE" not in env


def test_build_subprocess_env_includes_gh_token() -> None:
    """GH_TOKEN is set so worker agents can use gh CLI as yeast-bot."""
    config = _make_config()
    env = build_subprocess_env(config)
    assert "GITHUB_TOKEN" not in env
    assert env.get("GH_TOKEN") == config.github_token


def test_build_subprocess_env_includes_required_keys() -> None:
    """The returned dict contains all mandatory isolation and identity keys."""
    config = _make_config()
    env = build_subprocess_env(config)

    required_keys = {
        "PATH",
        "HOME",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "DISABLE_AUTOUPDATER",
        "DISABLE_ERROR_REPORTING",
        "DISABLE_TELEMETRY",
        "ENABLE_CLAUDEAI_MCP_SERVERS",
        "ENABLE_TOOL_SEARCH",
        "TERM",
    }
    for key in required_keys:
        assert key in env, f"Expected key {key!r} in subprocess env"


def test_build_subprocess_env_sets_term_to_dumb() -> None:
    """TERM is hardcoded to 'dumb' for headless operation."""
    config = _make_config()
    env = build_subprocess_env(config)
    assert env["TERM"] == "dumb"


def test_build_subprocess_env_creates_fresh_claude_config_dir() -> None:
    """Each call creates a distinct CLAUDE_CONFIG_DIR temp directory."""
    config = _make_config()
    env1 = build_subprocess_env(config)
    env2 = build_subprocess_env(config)
    assert env1["CLAUDE_CONFIG_DIR"] != env2["CLAUDE_CONFIG_DIR"]


def test_build_subprocess_env_claude_config_dir_exists() -> None:
    """The CLAUDE_CONFIG_DIR created by build_subprocess_env actually exists."""
    config = _make_config()
    env = build_subprocess_env(config)
    assert Path(env["CLAUDE_CONFIG_DIR"]).is_dir()


def test_build_subprocess_env_extra_overrides_base() -> None:
    """Values in the extra dict override the corresponding base dict values."""
    config = _make_config()
    custom_dir = "/tmp/custom-config-dir"
    env = build_subprocess_env(config, extra={"CLAUDE_CONFIG_DIR": custom_dir})
    assert env["CLAUDE_CONFIG_DIR"] == custom_dir


def test_build_subprocess_env_extra_can_inject_github_token() -> None:
    """Callers can inject GITHUB_TOKEN via extra without it appearing by default."""
    config = _make_config()
    env = build_subprocess_env(config, extra={"GH_TOKEN": "scoped-token"})
    assert env["GH_TOKEN"] == "scoped-token"


def test_build_subprocess_env_includes_anthropic_api_key() -> None:
    """ANTHROPIC_API_KEY from config is included when no helper is set."""
    config = _make_config()
    env = build_subprocess_env(config)
    assert env.get("ANTHROPIC_API_KEY") == "sk-ant-test-key"


# ---------------------------------------------------------------------------
# Derived properties
# ---------------------------------------------------------------------------


def test_sessions_dir_derived_from_log_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """sessions_dir is log_dir.expanduser() / 'sessions'."""
    for key, val in make_env(BRIMSTONE_LOG_DIR="/tmp/test-logs").items():
        monkeypatch.setenv(key, val)

    config = Config()
    assert config.sessions_dir == Path("/tmp/test-logs/sessions")


def test_cost_ledger_derived_from_log_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """cost_ledger is log_dir.expanduser() / 'cost.jsonl'."""
    for key, val in make_env(BRIMSTONE_LOG_DIR="/tmp/test-logs").items():
        monkeypatch.setenv(key, val)

    config = Config()
    assert config.cost_ledger == Path("/tmp/test-logs/cost.jsonl")
