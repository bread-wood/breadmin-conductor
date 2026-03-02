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

## Research Issue Triage

Every research issue — including follow-ups created by agents — **must pass the triage rubric
before being dispatched**. Apply it immediately after an agent creates follow-up issues (Step 3
of the research-worker loop), not at dispatch time.

### Rubric

Score 1 point for each "yes":

1. **Decision impact** — Would not knowing this change an implementation decision in the current milestone?
2. **Novelty** — Is this genuinely new, not already covered by an existing open or closed issue or doc?
3. **Risk** — Would not knowing this create a correctness or security risk?

**Score ≥ 2 → keep.** Assign to the active milestone if missing.

**Score < 2 → close with `wont-research` label:**
```bash
gh issue close <N> --reason "not planned" \
  --comment "Closing: score X/3 on research triage rubric. <brief reason>"
gh issue edit <N> --add-label "wont-research"
```

### Label Workflow

- Research agents tag their own follow-up issues with `triage` at creation time.
- The orchestrator reads all `triage`-labelled issues after each merge and scores them.
- Issues that pass become candidates for the next dispatch batch.
- Issues that fail are closed immediately — do not leave them open.

### Anti-patterns to reject (score 0 automatically)

- Empirical measurements that belong *inside* an existing research doc
- Implementation tasks or data collection scripts
- Topics already answered in a merged research doc
- Narrow edge cases that don't alter the core design

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
| `triage` | Follow-up issues pending triage decision (applied by research agents) |
| `wont-research` | Closed by triage gate — below threshold for standalone research |
