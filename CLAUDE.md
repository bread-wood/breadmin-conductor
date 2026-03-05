# brimstone — Agent Context

`brimstone` is a Python CLI package. Source lives in `src/brimstone/`.
Tests live in `tests/`. Default branch: `mainline`.

## Bead Architecture — Source of Truth

**Beads are the canonical source of truth for all task accounting.** GitHub issues and PRs are inputs/outputs; beads are the internal ledger.

### Bead types

| File | Type | Purpose |
|------|------|---------|
| `~/.brimstone/beads/<owner>/<repo>/work/<N>.json` | `WorkBead` | Issue lifecycle: `open → claimed → merge_ready → closed` (or `abandoned`) |
| `~/.brimstone/beads/<owner>/<repo>/prs/pr-<N>.json` | `PRBead` | PR + feedback triage state |
| `~/.brimstone/beads/<owner>/<repo>/merge-queue.json` | `MergeQueue` | Sequential merge ordering |
| `~/.brimstone/beads/<owner>/<repo>/campaign.json` | `CampaignBead` | Multi-milestone campaign progress |

### Invariants

1. **Beads lead, GitHub follows.** When checking what work exists, query beads first. GitHub issue state is eventually consistent; bead state is immediately consistent.
2. **Every tracked issue has a bead.** Before dispatching any agent, a `WorkBead` must exist with `state="open"`. Create the bead before (or atomically with) the GitHub issue.
3. **Dedup against beads, not GitHub issues.** When checking if a work item already exists (e.g. LLD for a module), check `store.list_work_beads()` — not `gh issue list`. GitHub may have stale, closed, or cross-milestone matches.
4. **Stage gates use beads.** Skip a stage only when all beads for that stage/milestone have `state` in `{"closed", "abandoned"}`. Do not rely on GitHub issue counts alone.
5. **Abandoned beads close PRs.** When a bead transitions to `abandoned`, any open PR for that branch must be closed. Do not leave open PRs for abandoned beads.
6. **`in-progress` label mirrors `claimed` bead state.** A GitHub issue carries `in-progress` if and only if its bead is `claimed`. On startup, reconcile: remove stale `in-progress` labels from issues whose bead is not `claimed`.

### When to create beads

- **Research/impl issues**: seeded in bulk by `_seed_work_beads` at stage startup.
- **Design LLD issues**: created by `_run_design_worker` Phase 2 self-heal — parse `### Module:` headings from the merged HLD, call `_file_design_issue_if_missing` for each missing module, then `_seed_work_beads` to create the bead. Always check existing beads first, not GitHub issues.
- **Never** create a bead for an issue that has `state="closed"` or `state="abandoned"` — those are terminal.

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
