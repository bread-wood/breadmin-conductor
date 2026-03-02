"""Configuration model for breadmin-conductor.

Resolves settings from environment variables and CLI flags.
CLI flags take precedence over env vars; env vars take precedence over defaults.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    """Runtime configuration for conductor commands."""

    model_config = SettingsConfigDict(env_prefix="CONDUCTOR_", env_file=".env")

    # Claude invocation
    model: str = Field(default="claude-sonnet-4-6", description="Claude model ID")
    max_budget: float = Field(default=5.00, description="USD budget cap per session")
    max_turns: int = Field(default=200, description="Max turns per claude -p invocation")

    # Paths
    data_dir: Path = Field(
        default=Path("~/.local/share/conductor").expanduser(),
        description="Root data directory for sessions and cost ledger",
    )

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def cost_ledger_path(self) -> Path:
        return self.data_dir / "cost.jsonl"
