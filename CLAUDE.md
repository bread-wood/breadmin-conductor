# breadmin-composer — Orchestration Rules

This repo follows the Orchestrator-Dispatch Protocol defined in `~/.claude/CLAUDE.md`.
The rules below are repo-specific additions and overrides.

## Repo Context

`breadmin-composer` is a Python CLI package. Source lives in `src/composer/`.
Tests live in `tests/`. The package provides five entry points:
- `research-worker` — headless research processing loop
- `design-worker` — reads research docs, produces HLD and LLD design documents
- `plan-issues` — reads design docs (HLD/LLD), breaks them into scoped impl issues
- `impl-worker` — headless implementation issue processing loop
- `composer` — admin commands (health, cost)

## Worker Pipeline

Each product version follows this six-stage pipeline:

```
spec              ← human: write a product spec (docs/specs/<version>.md) defining
     |              scope, success criteria, and key constraints for this version
     ↓
plan-milestones   ← human + orchestrator: read the spec, create milestone pair
     |              (Research + Implementation), file seed research issues
     ↓
research-worker   ← dispatches research agents, fires completion gate when done,
     |              migrates non-blocking issues to the next research milestone
     ↓
design-worker     ← reads merged research docs, produces HLD (docs/design/HLD.md)
     |              and per-module LLD docs (docs/design/lld/<module>.md)
     ↓
plan-issues       ← reads HLD + LLD docs, produces fully-specified impl issues
     |              with acceptance criteria, file scope, test requirements, dep graph
     ↓
impl-worker       ← claims impl issues, dispatches sub-agents, monitors CI, merges PRs
```

**No stage is skipped.**
- `research-worker` must complete before `design-worker` begins
- `design-worker` must produce and merge all design docs before `plan-issues` begins
- `plan-issues` must file all impl issues before `impl-worker` begins

### Stage responsibilities

| Stage | Produces | Does NOT produce |
|-------|----------|-----------------|
| `spec` | `docs/specs/<version>.md` | milestones, issues |
| `plan-milestones` | GitHub milestone pair, seed research issues | design docs, impl issues |
| `research-worker` | `docs/research/<N>-<slug>.md` per issue | design docs, impl issues |
| `design-worker` | `docs/design/HLD.md`, `docs/design/lld/<module>.md` | impl issues, code |
| `plan-issues` | GitHub impl issues with acceptance criteria and dep graph | design docs, code |
| `impl-worker` | merged PRs, working code | anything in prior stages |

## Milestone Model

Milestones come in ordered pairs — Research and Implementation — one pair per version.
The system always plans **at most one version ahead** of the current active pair.

**Naming convention**: `<Version> Research` and `<Version> Implementation` (or `Impl`).
Use meaningful version identifiers (MVP, v1.1, v2 …) rather than opaque numbers.
Workers identify milestone type by looking for the word "Research" or "Impl" in the title.

**Always-one-ahead rule**: when the implementation phase of version N begins,
run `plan-milestones` to create the research phase for version N+1.

**Never plan more than two versions out.** Research findings change scope; over-planning
is waste.

## Module Isolation

Agents are scoped to these modules. One agent per module at a time:

| Module | Scope |
|--------|-------|
| `config` | `src/composer/config.py` |
| `runner` | `src/composer/runner.py`, `src/composer/session.py` |
| `health` | `src/composer/health.py` |
| `logging` | `src/composer/logger.py` |
| `cli` | `src/composer/cli.py`, `src/composer/skills/` (all skill files) |
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

## Pipeline Stage Tracking

Each pipeline stage transition must be tracked as a GitHub issue so the current project
phase is always visible from the issue tracker.

### Issue format

Title: `Run <worker> for <milestone or version>`

Examples:
- `Run research-worker for MVP Research`
- `Run design-worker for MVP Research`
- `Run impl-worker for MVP Implementation`
- `Run plan-milestones for v2`

### Lifecycle

1. **Filed** — when the previous stage completes, the worker files the next stage's issue
2. **`in-progress`** — added when the stage begins (same as any other issue)
3. **Closed** — when the stage completes; the closing comment links to the Notion session report

### Who files what

| Stage completes | Files next issue |
|----------------|-----------------|
| `plan-milestones` | `Run research-worker for <milestone>` |
| `research-worker` | `Run design-worker for <milestone>` |
| `design-worker` | `Run plan-issues for <milestone>` |
| `plan-issues` | `Run impl-worker for <milestone>` |
| `impl-worker` | `Run plan-milestones for <next version>` |

```bash
gh issue create \
  --repo <owner>/<repo> \
  --title "Run <worker> for <milestone>" \
  --label "pipeline" \
  --milestone "<relevant milestone>"
```

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
| `build` | Design and build planning tasks (HLD, LLD, impl issue decomposition) |
| `infra` | Repo scaffolding, CI, pyproject |
| `feat:config` | Config dataclass + env resolution |
| `feat:runner` | Headless claude -p runner |
| `feat:health` | Preflight health checks |
| `feat:logging` | JSONL logging + cost ledger |
| `feat:cli` | CLI wiring + skill files |
| `in-progress` | Currently being worked on (managed by orchestrator) |
| `triage` | Follow-up issues pending triage decision (applied by research agents) |
| `wont-research` | Closed by triage gate — below threshold for standalone research |
| `pipeline` | Pipeline stage transition tracking issues |
