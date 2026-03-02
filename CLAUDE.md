# breadmin-conductor — Orchestration Rules

This repo follows the Orchestrator-Dispatch Protocol defined in `~/.claude/CLAUDE.md`.
The rules below are repo-specific additions and overrides.

## Repo Context

`breadmin-conductor` is a Python CLI package. Source lives in `src/conductor/`.
Tests live in `tests/`. The package provides three entry points:
- `issue-worker` — headless issue processing loop
- `research-worker` — headless research processing loop
- `conductor` — admin commands (health, cost)

## Module Isolation

Agents are scoped to these modules. One agent per module at a time:

| Module | Scope |
|--------|-------|
| `config` | `src/conductor/config.py` |
| `runner` | `src/conductor/runner.py`, `src/conductor/session.py` |
| `health` | `src/conductor/health.py` |
| `logging` | `src/conductor/logger.py` |
| `cli` | `src/conductor/cli.py`, `src/conductor/skills/` |
| `infra` | `pyproject.toml`, `.github/`, `CLAUDE.md`, `README.md` |
| `docs` | `docs/` |

## Testing

```bash
uv run pytest                     # all tests
uv run pytest tests/unit/         # unit tests only
uv run pytest tests/integration/  # integration tests
```

All tests must pass before merging.

## Linting

```bash
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

Must be clean before merging.

## Dependency Management

Use `uv` for all package management. Do NOT use pip directly.

```bash
uv add <package>          # add runtime dependency
uv add --dev <package>    # add dev dependency
```

## Default Branch

`main`

## Issue Labels

| Label | Purpose |
|-------|---------|
| `research` | Research/investigation tasks |
| `infra` | Repo scaffolding, CI, pyproject |
| `feat:config` | Config dataclass + env resolution |
| `feat:runner` | Headless claude -p runner |
| `feat:health` | Preflight health checks |
| `feat:logging` | JSONL logging + cost ledger |
| `feat:cli` | CLI wiring + skill files |
| `in-progress` | Currently being worked on (managed by orchestrator) |
