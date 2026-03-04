"""Unit tests for the `brimstone cost` and `brimstone report` commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from brimstone.cli import composer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-test-key",
    "BRIMSTONE_GH_TOKEN": "ghp-test-token",
}


def _write_cost_entries(log_dir: Path, entries: list[dict]) -> None:
    """Write JSONL cost entries to {log_dir}/cost.jsonl."""
    log_dir.mkdir(parents=True, exist_ok=True)
    cost_path = log_dir / "cost.jsonl"
    with cost_path.open("w") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _make_cost_entry(
    *,
    run_id: str = "run-abc",
    repo: str = "owner/repo",
    stage: str = "impl",
    milestone: str = "v1.0",
    total_cost_usd: float = 0.10,
    input_tokens: int = 1000,
    output_tokens: int = 500,
) -> dict:
    return {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "session_id": "sess-1",
        "run_id": run_id,
        "repo": repo,
        "milestone": milestone,
        "stage": stage,
        "issue_number": 1,
        "model": "claude-sonnet-4-6",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "num_turns": 3,
        "duration_ms": 5000,
        "is_error": False,
        "error_subtype": None,
        "total_cost_usd": total_cost_usd,
        "auth_mode": "api_key",
        "web_search_requests": 0,
    }


# ---------------------------------------------------------------------------
# brimstone cost
# ---------------------------------------------------------------------------


class TestCostCommand:
    def test_cost_no_data(self, tmp_path: Path) -> None:
        """cost command with empty ledger prints 'No cost data found.'"""
        # log_dir has no cost.jsonl at all
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with patch("brimstone.cli.load_config") as mock_config:
                cfg = MagicMock()
                cfg.log_dir = tmp_path
                mock_config.return_value = cfg

                runner = CliRunner()
                result = runner.invoke(composer, ["cost"])

        assert result.exit_code == 0, result.output
        assert "No cost data found." in result.output

    def test_cost_totals(self, tmp_path: Path) -> None:
        """cost command with 3 entries sums correctly."""
        log_dir = tmp_path / "logs"
        entries = [
            _make_cost_entry(total_cost_usd=0.10, input_tokens=1000, output_tokens=200),
            _make_cost_entry(total_cost_usd=0.20, input_tokens=2000, output_tokens=400),
            _make_cost_entry(total_cost_usd=0.30, input_tokens=3000, output_tokens=600),
        ]
        _write_cost_entries(log_dir, entries)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with patch("brimstone.cli.load_config") as mock_config:
                cfg = MagicMock()
                cfg.log_dir = log_dir
                mock_config.return_value = cfg

                runner = CliRunner()
                result = runner.invoke(composer, ["cost"])

        assert result.exit_code == 0, result.output
        assert "Entries : 3" in result.output
        assert "$0.6000" in result.output
        assert "6,000" in result.output  # total input tokens
        assert "1,200" in result.output  # total output tokens

    def test_cost_breakdown_by_stage(self, tmp_path: Path) -> None:
        """--breakdown stage groups entries correctly."""
        log_dir = tmp_path / "logs"
        entries = [
            _make_cost_entry(stage="research", total_cost_usd=0.10),
            _make_cost_entry(stage="research", total_cost_usd=0.05),
            _make_cost_entry(stage="impl", total_cost_usd=0.30),
        ]
        _write_cost_entries(log_dir, entries)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with patch("brimstone.cli.load_config") as mock_config:
                cfg = MagicMock()
                cfg.log_dir = log_dir
                mock_config.return_value = cfg

                runner = CliRunner()
                result = runner.invoke(composer, ["cost", "--breakdown", "stage"])

        assert result.exit_code == 0, result.output
        assert "By stage" in result.output
        assert "research" in result.output
        assert "impl" in result.output
        # research total is 0.15, impl is 0.30
        assert "0.1500" in result.output
        assert "0.3000" in result.output

    def test_cost_filter_by_run_id(self, tmp_path: Path) -> None:
        """--run filters to only that run_id."""
        log_dir = tmp_path / "logs"
        entries = [
            _make_cost_entry(run_id="run-x", total_cost_usd=0.10),
            _make_cost_entry(run_id="run-y", total_cost_usd=0.99),
        ]
        _write_cost_entries(log_dir, entries)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with patch("brimstone.cli.load_config") as mock_config:
                cfg = MagicMock()
                cfg.log_dir = log_dir
                mock_config.return_value = cfg

                runner = CliRunner()
                result = runner.invoke(composer, ["cost", "--run", "run-x"])

        assert result.exit_code == 0, result.output
        assert "Entries : 1" in result.output
        assert "$0.1000" in result.output
        assert "0.9900" not in result.output

    def test_cost_filter_by_milestone(self, tmp_path: Path) -> None:
        """--milestone filters to only entries for that milestone."""
        log_dir = tmp_path / "logs"
        entries = [
            _make_cost_entry(milestone="v1.0", total_cost_usd=0.10),
            _make_cost_entry(milestone="v2.0", total_cost_usd=0.99),
        ]
        _write_cost_entries(log_dir, entries)

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with patch("brimstone.cli.load_config") as mock_config:
                cfg = MagicMock()
                cfg.log_dir = log_dir
                mock_config.return_value = cfg

                runner = CliRunner()
                result = runner.invoke(composer, ["cost", "--milestone", "v1.0"])

        assert result.exit_code == 0, result.output
        assert "Entries : 1" in result.output
        assert "$0.1000" in result.output


# ---------------------------------------------------------------------------
# brimstone report
# ---------------------------------------------------------------------------


class TestReportCommand:
    def test_report_no_beads(self, tmp_path: Path) -> None:
        """report with empty BeadStore prints header + empty tables."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        mock_store = MagicMock()
        mock_store.list_work_beads.return_value = []
        mock_store.list_pr_beads.return_value = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli.load_config") as mock_cfg,
                patch("brimstone.cli.make_bead_store", return_value=mock_store),
            ):
                cfg = MagicMock()
                cfg.log_dir = log_dir
                cfg.github_repo = "owner/repo"
                mock_cfg.return_value = cfg

                runner = CliRunner()
                result = runner.invoke(composer, ["report", "--repo", "owner/repo"])

        assert result.exit_code == 0, result.output
        assert "brimstone session report" in result.output
        assert "ISSUES" in result.output
        assert "PULL REQUESTS" in result.output
        assert "SUMMARY" in result.output
        assert "(none)" in result.output

    def test_report_no_repo_shows_error(self, tmp_path: Path) -> None:
        """report without --repo and no config github_repo shows error."""
        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with patch("brimstone.cli.load_config") as mock_cfg:
                cfg = MagicMock()
                cfg.log_dir = tmp_path
                cfg.github_repo = None
                mock_cfg.return_value = cfg

                runner = CliRunner()
                result = runner.invoke(composer, ["report"])

        assert result.exit_code != 0

    def test_report_with_cost_entries(self, tmp_path: Path) -> None:
        """report shows cost total from ledger."""
        log_dir = tmp_path / "logs"
        entries = [
            _make_cost_entry(
                run_id="run-test",
                repo="owner/repo",
                total_cost_usd=0.42,
            )
        ]
        _write_cost_entries(log_dir, entries)

        mock_store = MagicMock()
        mock_store.list_work_beads.return_value = []
        mock_store.list_pr_beads.return_value = []

        with patch.dict("os.environ", MINIMAL_ENV, clear=False):
            with (
                patch("brimstone.cli.load_config") as mock_cfg,
                patch("brimstone.cli.make_bead_store", return_value=mock_store),
            ):
                cfg = MagicMock()
                cfg.log_dir = log_dir
                cfg.github_repo = "owner/repo"
                mock_cfg.return_value = cfg

                runner = CliRunner()
                result = runner.invoke(
                    composer,
                    ["report", "--repo", "owner/repo", "--run", "run-test"],
                )

        assert result.exit_code == 0, result.output
        assert "$0.42" in result.output
