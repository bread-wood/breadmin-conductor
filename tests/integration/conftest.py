"""Shared fixtures for brimstone integration tests.

All integration tests use:
- A real local git repo with a bare-repo "origin" (no GitHub required)
- Mocked ``gh`` CLI calls (via ``brimstone.cli._gh`` patch)
- Mocked ``runner.run`` (no LLM calls, no API cost)

The git layer is real so we catch worktree, branch, and fetch bugs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from brimstone.config import Config
from brimstone.session import Checkpoint

# ---------------------------------------------------------------------------
# Git repo fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A local git repo with a bare 'remote' using 'mainline' as default branch.

    Layout::

        tmp_path/
          origin.git/   ← bare repo (acts as the GitHub remote)
          repo/         ← working clone (what the orchestrator operates on)

    Returns the path to the working clone.
    """
    bare = tmp_path / "origin.git"
    clone = tmp_path / "repo"

    subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)

    def cfg(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(clone), "config"] + list(args),
            check=True,
            capture_output=True,
        )

    cfg("user.email", "test@brimstone.test")
    cfg("user.name", "Brimstone Test")

    # Initial commit
    (clone / "README.md").write_text("# test\n")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(clone), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )

    # Push as 'mainline' and rename local branch to match
    subprocess.run(
        ["git", "-C", str(clone), "push", "origin", "HEAD:mainline"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "branch", "-m", "mainline"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(clone), "branch", "--set-upstream-to=origin/mainline", "mainline"],
        check=True,
        capture_output=True,
    )

    return clone


# ---------------------------------------------------------------------------
# CWD restore
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def restore_cwd() -> None:  # type: ignore[return]
    """Restore working directory after each test (some tests call os.chdir)."""
    original = os.getcwd()
    yield
    os.chdir(original)


# ---------------------------------------------------------------------------
# Config / Checkpoint factories
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path) -> Config:
    """Minimal Config pointing all paths at *tmp_path*."""
    cfg = Config(
        anthropic_api_key="sk-ant-test",
        github_token="ghp-test",
        log_dir=tmp_path / "logs",
        checkpoint_dir=tmp_path / "checkpoints",
        model="claude-haiku-4-5-20251001",
    )
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "checkpoints").mkdir(parents=True, exist_ok=True)
    return cfg


def make_checkpoint(
    repo: str = "owner/repo",
    milestone: str = "v0.1.0",
    stage: str = "research",
) -> Checkpoint:
    return Checkpoint(
        schema_version=1,
        run_id="test-run-id",
        session_id="test-session-id",
        repo=repo,
        default_branch="mainline",
        milestone=milestone,
        stage=stage,
        timestamp="2026-01-01T00:00:00",
    )


def make_issue(number: int, title: str, labels: list[str] | None = None) -> dict:
    return {
        "number": number,
        "title": title,
        "body": f"Research task #{number}.",
        "labels": [{"name": lbl} for lbl in (labels or [])],
        "assignees": [],
        "milestone": {"title": "v0.1.0"},
    }


def fake_run_result(*, is_error: bool = False) -> MagicMock:
    """Fake RunResult for mocking runner.run."""
    result = MagicMock()
    result.is_error = is_error
    result.subtype = "error_unknown" if is_error else "success"
    result.error_code = "unknown_error" if is_error else None
    result.exit_code = 1 if is_error else 0
    return result
