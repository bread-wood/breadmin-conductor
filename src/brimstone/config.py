"""Configuration model for brimstone.

Resolves settings from environment variables and CLI flags.
CLI flags take precedence over env vars; env vars take precedence over defaults.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic.aliases import AliasChoices
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BrimstoneError(Exception):
    """Base exception for all brimstone errors."""


class ConfigurationError(BrimstoneError):
    """Missing or invalid configuration value."""


class OrchestratorNestingError(BrimstoneError):
    """Raised when the conductor is launched inside a Claude Code session."""


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------


class Config(BaseSettings):
    """Runtime configuration for brimstone commands."""

    model_config = SettingsConfigDict(
        env_prefix="BRIMSTONE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Required credentials (no BRIMSTONE_ prefix) ---
    anthropic_api_key: str = Field(
        validation_alias=AliasChoices(
            "BRIMSTONE_ANTHROPIC_API_KEY",
            "ANTHROPIC_API_KEY",
            "anthropic_api_key",
        ),
        description="Anthropic API key (ANTHROPIC_API_KEY or BRIMSTONE_ANTHROPIC_API_KEY)",
    )
    github_token: str = Field(
        validation_alias=AliasChoices(
            "BRIMSTONE_GH_TOKEN",
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "github_token",
        ),
        description="GitHub token (BRIMSTONE_GH_TOKEN, GH_TOKEN, or GITHUB_TOKEN)",
    )

    # --- Behaviour control ---
    max_budget_usd: float = Field(
        default=5.00,
        ge=0.01,
        description="USD budget cap per session",
    )
    max_retries: int = Field(
        default=3,
        ge=0,
        description="Maximum number of retries on transient failures",
    )
    max_concurrency: int = Field(
        default=5,
        ge=1,
        description="Maximum number of concurrent sub-agents",
    )
    backoff_base_seconds: float = Field(
        default=2.0,
        ge=0.1,
        description="Base delay in seconds for exponential backoff",
    )
    backoff_max_minutes: float = Field(
        default=32.0,
        ge=1.0,
        description="Maximum backoff delay in minutes",
    )
    agent_timeout_minutes: float = Field(
        default=30.0,
        ge=1.0,
        description="Timeout in minutes for a single sub-agent",
    )
    subscription_tier: Literal["pro", "max", "max20x"] = Field(
        default="pro",
        description="Claude subscription tier (pro, max, max20x)",
    )

    # --- Paths ---
    log_dir: Path = Field(
        default=Path("~/.brimstone/logs"),
        description="Directory for session logs and cost ledger",
    )
    checkpoint_dir: Path = Field(
        default=Path("~/.brimstone/checkpoints"),
        description="Directory for session checkpoints",
    )

    # --- Model ---
    model: str = Field(
        default="claude-sonnet-4-6",
        description="Claude model ID for impl agents (default: claude-sonnet-4-6)",
    )
    research_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Claude model ID for research agents (default: claude-haiku-4-5-20251001)",
    )
    design_model: str = Field(
        default="claude-sonnet-4-6",
        description="Claude model ID for design agents (default: claude-sonnet-4-6)",
    )
    scope_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Claude model ID for scope/plan-issues agents (default: haiku)",
    )

    # --- Git / GitHub ---
    default_branch: str = Field(
        default="main",
        description="Expected default branch name for the repository",
    )
    github_repo: str | None = Field(
        default=None,
        validation_alias=AliasChoices("GITHUB_REPO", "github_repo"),
        description='GitHub repository in "owner/repo" format',
    )
    target_repo: str | None = Field(
        default=None,
        description=(
            "Resolved target repository. "
            "For remote repos: 'owner/name' string. "
            "For local repos: absolute path string. "
            "None means operate on the current working directory."
        ),
    )

    # --- Plan-milestones spec seeding ---
    spec_path: Path | None = Field(
        default=None,
        description=(
            "Absolute path to a .md spec file to seed into the target repo before "
            "running plan-milestones. Resolved and validated by the CLI before being stored here."
        ),
    )
    version: str | None = Field(
        default=None,
        description=(
            "Version name override for plan-milestones spec seeding. "
            "When None, the version is inferred from the spec filename stem."
        ),
    )

    # --- Bead storage ---
    beads_dir: Path = Field(
        default=Path("~/.brimstone/beads"),
        description="Local bead storage root",
    )
    state_repo: str | None = Field(
        default=None,
        description="Optional 'owner/repo' for portable state (git-backed bead sync)",
    )
    state_repo_dir: Path = Field(
        default=Path("~/.brimstone/state-repos"),
        description="Clone directory for state repos",
    )

    # --- Health checks ---
    max_orphaned_issues: int = Field(
        default=5,
        ge=0,
        validation_alias=AliasChoices("BRIMSTONE_MAX_ORPHANED_ISSUES", "max_orphaned_issues"),
        description="Maximum number of orphaned in-progress issues before health check fails",
    )

    # --- Credential proxy ---
    api_key_helper: str | None = Field(
        default=None,
        description="Path to a script whose stdout is the Anthropic API key",
    )

    # --- Derived properties ---

    @property
    def sessions_dir(self) -> Path:
        """Directory for individual session logs."""
        return self.log_dir.expanduser() / "sessions"

    @property
    def cost_ledger(self) -> Path:
        """Path to the JSONL cost ledger."""
        return self.log_dir.expanduser() / "cost.jsonl"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def load_config(**cli_overrides: Any) -> Config:
    """Validate configuration and return a Config instance.

    Raises ConfigurationError if required environment variables are missing or
    if any field value fails validation.

    Args:
        **cli_overrides: Keyword arguments that override env vars and defaults.
                         Typically supplied by the CLI layer from parsed flags.

    Returns:
        A validated Config instance.
    """
    try:
        return Config(**cli_overrides)
    except Exception as exc:
        # pydantic ValidationError — reformat as ConfigurationError
        _reraise_validation_error(exc)


def _reraise_validation_error(exc: Exception) -> None:
    """Extract field-level detail from a pydantic ValidationError and re-raise
    as ConfigurationError."""
    # Import here to avoid polluting module namespace
    from pydantic import ValidationError

    if not isinstance(exc, ValidationError):
        raise ConfigurationError(str(exc)) from exc

    errors = exc.errors()
    if not errors:
        raise ConfigurationError(str(exc)) from exc

    # Use the first error for the primary message
    first = errors[0]
    field_loc = first.get("loc", ())
    field_name = field_loc[0] if field_loc else "unknown"
    msg = first.get("msg", "")
    input_val = first.get("input", "")

    # Map internal field names back to env var names
    env_var = _field_to_env_var(str(field_name))

    if "missing" in msg.lower() or "field required" in msg.lower():
        raise ConfigurationError(
            f"Missing required environment variable: {env_var}\n"
            f"  Set it in your shell:  export {env_var}=...\n"
            f"  Or add it to a .env file in the project root."
        ) from exc
    else:
        raise ConfigurationError(
            f"Invalid value for {env_var}: {input_val!r}\n  Validation error: {msg}"
        ) from exc


def _field_to_env_var(field_name: str) -> str:
    """Convert a Config field name to its corresponding environment variable name."""
    # Fields with custom aliases — maps both the Python field name and the alias
    # (pydantic-settings may put either in errors()[0]["loc"]).
    lookup = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
        "BRIMSTONE_ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
        "github_token": "BRIMSTONE_GH_TOKEN",
        "BRIMSTONE_GH_TOKEN": "BRIMSTONE_GH_TOKEN",
        "GH_TOKEN": "BRIMSTONE_GH_TOKEN",
        "GITHUB_TOKEN": "BRIMSTONE_GH_TOKEN",
    }
    if field_name in lookup:
        return lookup[field_name]
    return "BRIMSTONE_" + field_name.upper()


# ---------------------------------------------------------------------------
# Subprocess env builder
# ---------------------------------------------------------------------------


def build_subprocess_env(
    config: Config,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Construct a sanitized environment dictionary for a claude -p subprocess.

    The returned dict is fully self-contained: the subprocess receives only
    what is in this dict. No variables are inherited from the parent process
    (os.environ is never passed directly to the child).

    Args:
        config:  The validated Config instance for this conductor session.
        extra:   Additional variables to merge in after the base dict is built.
                 Values in ``extra`` override corresponding keys in the base dict.
                 Use this for per-dispatch overrides (e.g., CLAUDE_CONFIG_DIR
                 if the caller manages the temp dir lifecycle themselves).

    Returns:
        A dict[str, str] suitable for passing as the ``env`` kwarg to
        subprocess.Popen or subprocess.run.

    Side effects:
        Creates a temporary directory for CLAUDE_CONFIG_DIR unless overridden
        by ``extra``. The caller is responsible for deleting this directory after
        the subprocess exits.
    """
    if extra is None:
        extra = {}

    # Resolve the API key — either from the helper script or directly
    api_key = _resolve_api_key(config)

    # Create or use a temp dir for Claude's config isolation.
    # When the caller supplies CLAUDE_CONFIG_DIR in extra, reuse that path so
    # we don't create a temp dir that gets immediately overwritten and leaked.
    if "CLAUDE_CONFIG_DIR" not in extra:
        claude_config_dir = tempfile.mkdtemp(prefix="brimstone-claude-config-")
    else:
        claude_config_dir = extra["CLAUDE_CONFIG_DIR"]
        os.makedirs(claude_config_dir, exist_ok=True)

    claude_home = Path(os.environ.get("HOME", "~")).expanduser() / ".claude"

    # Seed the statsig cache so the SDK doesn't hang on a cold-start network
    # fetch when cached evaluations are absent.
    real_statsig = claude_home / "statsig"
    if real_statsig.is_dir():
        shutil.copytree(
            str(real_statsig), os.path.join(claude_config_dir, "statsig"), dirs_exist_ok=True
        )

    # Write a minimal settings.json so Claude Code skips the first-run
    # dangerous-mode consent dialog (which blocks on /dev/tty when absent).
    settings_path = Path(claude_config_dir) / "settings.json"
    settings_path.write_text(
        json.dumps({"skipDangerousModePermissionPrompt": True}),
        encoding="utf-8",
    )

    # Pre-seed policy-limits.json with allow_remote_control:false so that when
    # Claude fetches the same value from the API, it sees no change and skips
    # the remote-control shutdown handler.  That handler deadlocks in headless
    # mode when the remote-control server was never started (introduced in
    # Claude Code ~2.1.58 when Remote Control was expanded to all users).
    policy_limits_path = Path(claude_config_dir) / "policy-limits.json"
    policy_limits_path.write_text(
        json.dumps({"restrictions": {"allow_remote_control": {"allowed": False}}}),
        encoding="utf-8",
    )

    env: dict[str, str] = {
        # Shell essentials
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        "SHELL": os.environ.get("SHELL", "/bin/bash"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        "TERM": "dumb",
        # Git identity
        "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", ""),
        "GIT_AUTHOR_EMAIL": os.environ.get("GIT_AUTHOR_EMAIL", ""),
        "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", ""),
        "GIT_COMMITTER_EMAIL": os.environ.get("GIT_COMMITTER_EMAIL", ""),
        # Claude isolation
        "CLAUDE_CONFIG_DIR": claude_config_dir,
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_TELEMETRY": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "ENABLE_TOOL_SEARCH": "false",
        # Suppress nonessential startup network calls that can deadlock in
        # headless mode (e.g. the remote-control keepalive infrastructure).
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }

    # Include the resolved API key if available
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key

    # Include the GitHub token so worker agents can use gh CLI as yeast-bot
    if config.github_token:
        env["GH_TOKEN"] = config.github_token

    # Merge caller-supplied overrides last
    env.update(extra)

    return env


def _resolve_api_key(config: Config) -> str | None:
    """Resolve the Anthropic API key, optionally via a helper script.

    If config.api_key_helper is set, the script at that path is executed and
    its stdout is used as the API key. Otherwise, config.anthropic_api_key is
    returned directly.
    """
    if config.api_key_helper:
        result = subprocess.run(
            [config.api_key_helper],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    return config.anthropic_api_key
