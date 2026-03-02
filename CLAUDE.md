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

## Research Completion Gate

Research for a milestone is **done enough to begin implementation** when all remaining open
research issues are non-blocking — i.e., none of them would require a significant rewrite of
an implementation issue in the current milestone if answered later.

### Blocking vs. non-blocking test

A research issue is **blocking** if answering it would change the *design* (not just the
*quality*) of a current-milestone implementation issue. Examples:
- Blocking: "Does `--permission-prompt-tool` work in the current CLI?" — if no, the entire
  security architecture in the runner must change.
- Non-blocking: "What is the exact backoff curve for 429 retries?" — implementation proceeds
  with a reasonable default; the answer refines but doesn't redesign.

### Gate procedure

Before each research-worker dispatch batch, the orchestrator checks:

```
For each open research issue in the active milestone:
  Is there a specific current-milestone implementation issue that cannot be
  designed without this answer?
    YES → blocking, must dispatch before M_impl starts
    NO  → non-blocking, migrate to the next research milestone
```

If **zero blocking issues remain**, declare research complete for this milestone:
1. Migrate all remaining non-blocking open research issues to the next research milestone
2. Post the session report (Step 5)
3. STOP — do not create more research issues just to fill the queue

### Milestone labels

| Milestone | Purpose |
|-----------|---------|
| `M1: Foundation` | Research that must complete before M2 implementation begins |
| `M2: Implementation` | Core v1 CLI implementation issues |
| `M3: v2 Research` | Research for future versions — non-blocking for v1 |
| `M4: v2 Implementation` | Future implementation (not yet scheduled) |

## Follow-Up Milestone Assignment

When a research agent (or the orchestrator) creates a new issue, it must choose the correct
milestone — **do not default to the parent's milestone**.

### Decision tree

```
Is this follow-up needed to design a v1 implementation issue?
  YES → same milestone as the current research milestone (M1 if in M1 research)
  NO  →
    Is it needed for a v2 feature already scoped?
      YES → M3: v2 Research
      NO  → M3: v2 Research (default for anything non-blocking)
```

### Practical guidance for agents

When writing the "Follow-Up Research Recommendations" section of a doc, tag each item:

- `[BLOCKS_IMPL]` — must be researched before the current implementation milestone
- `[V2_RESEARCH]` — useful but doesn't block v1; file under the next research milestone
- `[WONT_RESEARCH]` — not worth a standalone doc; note inline

Only create GitHub issues for `[BLOCKS_IMPL]` and `[V2_RESEARCH]` items.

To find the correct milestone, inspect what exists and pick by purpose — do not hardcode names:
```bash
gh milestone list --repo <owner>/<repo>
```
`[BLOCKS_IMPL]` → current research milestone.
`[V2_RESEARCH]` → the lowest-numbered research milestone beyond the current one.

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
