# brimstone — Agent Context

`brimstone` is a Python CLI package. Source lives in `src/brimstone/`.
Tests live in `tests/`. Default branch: `mainline`.

## Module Isolation

One agent per module at a time. Each agent may only modify files within its assigned module:

| Module | Scope |
|--------|-------|
| `beads` | `src/brimstone/beads.py`, `~/.brimstone/beads/` |
| `config` | `src/brimstone/config.py` |
| `runner` | `src/brimstone/runner.py`, `src/brimstone/session.py` |
| `health` | `src/brimstone/health.py` |
| `logging` | `src/brimstone/logger.py` |
| `cli` | `src/brimstone/cli.py`, `src/brimstone/skills/` |
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

Use `uv` for all package management. Do not use pip directly.

```bash
uv add <package>        # runtime dependency
uv add --dev <package>  # dev dependency
```

## Issue Labels

### Stage
| Label | Purpose |
|-------|---------|
| `stage/research` | Research and investigation |
| `stage/design` | Design (HLD, LLD) |
| `stage/impl` | Implementation (code + tests) |

### Priority
| Label | Purpose |
|-------|---------|
| `P0` | Release blocker |
| `P1` | High |
| `P2` | Normal (default) |
| `P3` | Low |
| `P4` | Backlog |

### Status / domain
| Label | Purpose |
|-------|---------|
| `in-progress` | Claimed by an agent |
| `triage` | Pending triage decision |
| `wont-research` | Below triage threshold |
| `pipeline` | Stage transition tracking |
| `feat:config` | Config dataclass + env resolution |
| `feat:runner` | Headless runner |
| `feat:health` | Preflight health checks |
| `feat:logging` | JSONL logging + cost ledger |
| `feat:cli` | CLI wiring + skill files |
| `infra` | Repo scaffolding, CI, pyproject |
| `core` | Core pipeline plumbing |
