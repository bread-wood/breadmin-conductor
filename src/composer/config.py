"""Configuration model for breadmin-composer.

Resolves settings from environment variables and CLI flags.
CLI flags take precedence over env vars; env vars take precedence over defaults.
"""

from __future__ import annotations

import os
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


class ComposerError(Exception):
    """Base exception for all composer errors."""


class ConfigurationError(ComposerError):
    """Missing or invalid configuration value."""


class OrchestratorNestingError(ComposerError):
    """Raised when the conductor is launched inside a Claude Code session."""


# ---------------------------------------------------------------------------
# Settings model
# ---------------------------------------------------------------------------


class Config(BaseSettings):
    """Runtime configuration for composer commands."""

    model_config = SettingsConfigDict(
        env_prefix="CONDUCTOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- Required credentials (no CONDUCTOR_ prefix) ---
    anthropic_api_key: str = Field(
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "anthropic_api_key"),
        description="Anthropic API key",
    )
    github_token: str = Field(
        validation_alias=AliasChoices("GITHUB_TOKEN", "github_token"),
        description="GitHub personal access token",
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
        default=Path("~/.composer/logs"),
        description="Directory for session logs and cost ledger",
    )
    checkpoint_dir: Path = Field(
        default=Path("~/.composer/checkpoints"),
        description="Directory for session checkpoints",
    )

    # --- Model ---
    model: str = Field(
        default="claude-opus-4-6",
        description="Claude model ID passed to claude -p via --model",
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

    # --- Health checks ---
    max_orphaned_issues: int = Field(
        default=5,
        ge=0,
        validation_alias=AliasChoices("CONDUCTOR_MAX_ORPHANED_ISSUES", "max_orphaned_issues"),
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

    Raises OrchestratorNestingError if CLAUDECODE=1 is set in the environment.
    Raises ConfigurationError if required environment variables are missing or
    if any field value fails validation.

    Args:
        **cli_overrides: Keyword arguments that override env vars and defaults.
                         Typically supplied by the CLI layer from parsed flags.

    Returns:
        A validated Config instance.
    """
    if os.environ.get("CLAUDECODE") == "1":
        raise OrchestratorNestingError(
            "Cannot nest orchestrator invocations.\n\n"
            "CLAUDECODE=1 is set in the current environment, which means this process is\n"
            "already running inside a Claude Code session.\n\n"
            "To run the conductor, open a plain terminal (not a Claude Code session) and\n"
            "invoke it from there. If you need to test the conductor from within Claude\n"
            "Code, use a sub-shell that unsets CLAUDECODE:\n\n"
            "    (unset CLAUDECODE && composer impl-worker)"
        )

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
    # Fields with custom aliases (no CONDUCTOR_ prefix)
    no_prefix = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "github_token": "GITHUB_TOKEN",
    }
    if field_name in no_prefix:
        return no_prefix[field_name]
    return "CONDUCTOR_" + field_name.upper()


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

    # Create a fresh temp dir for Claude's config isolation
    claude_config_dir = tempfile.mkdtemp(prefix="composer-claude-config-")

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
    }

    # Include the resolved API key if available
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key

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
