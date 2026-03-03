# brimstone — Orchestration Rules

This repo follows the Orchestrator-Dispatch Protocol defined in `~/.claude/CLAUDE.md`.
The rules below are repo-specific additions and overrides.

## Repo Context

`brimstone` is a Python CLI package. Source lives in `src/brimstone/`.
Tests live in `tests/`. The package provides one entry point (`brimstone`) with these
subcommands:
- `brimstone run` — run one or more pipeline stages for a milestone
- `brimstone init` — upload spec + seed milestone and research issues
- `brimstone adopt` — adopt an existing repo (stub, not yet implemented)
- `brimstone health` — preflight health checks
- `brimstone cost` — cost ledger summary

## Worker Pipeline

Each product version follows this eight-stage pipeline:

```
spec            ← human: write a product spec (docs/specs/<version>.md) defining
     |            scope, success criteria, and key constraints for this version
     ↓
init            ← human + orchestrator: read the spec, create milestone, file seed
     |            research issues
     ↓
research        ← dispatches research agents, fires completion gate when done,
     |            migrates non-blocking issues to the next version's research
     ↓
design          ← reads merged research docs, produces HLD (docs/design/HLD.md)
     |            and per-module LLD docs (docs/design/lld/<module>.md)
     ↓
scoping         ← reads HLD + LLD docs, produces fully-specified implementation issues
     |            with acceptance criteria, file scope, test requirements, dep graph
     ↓
implementation  ← claims implementation issues, dispatches sub-agents, monitors CI,
     |            merges PRs
     ↓
qa              ← runs full test suite + exercises product against spec acceptance
     |            criteria; files bug issues; loops with implementation until clean
     ↓
release         ← creates git tag on mainline, publishes GitHub release, files
                  init issue for next version
```

**No stage is skipped.**
- `research` must complete before `design` begins
- `design` must produce and merge all design docs before `scoping` begins
- `scoping` must file all implementation issues before `implementation` begins
- `implementation` must close all implementation issues before `qa` begins
- `qa` must pass clean before `release` runs

### Stage responsibilities

| Stage | Produces | Does NOT produce |
|-------|----------|-----------------|
| `spec` | `docs/specs/<version>.md` | milestones, issues |
| `init` | GitHub milestone, seed research issues | design docs, impl issues |
| `research` | `docs/research/<N>-<slug>.md` per issue | design docs, impl issues |
| `design` | `docs/design/HLD.md`, `docs/design/lld/<module>.md` | impl issues, code |
| `scoping` | GitHub implementation issues with acceptance criteria and dep graph | design docs, code |
| `implementation` | merged PRs, working code | anything in prior stages |
| `qa` | bug issues (if any); clean bill of health | code, design docs |
| `release` | git tag, GitHub release, init issue for next version | code, design docs |

## Milestone Model

One milestone per version. All issues for a version — across all stages — belong to the
same milestone. Stage labels (`stage/research`, `stage/design`, `stage/impl`) filter by
stage within the milestone.

The system always plans **at most one version ahead** — when `implementation` begins for
version N, run `init` to create the milestone for version N+1 and seed its research issues.

**Never plan more than two versions out.** Research findings change scope; over-planning
is waste.

## Branch Strategy

Trunk-based: all implementation PRs target `mainline` directly. The milestone is a GitHub
milestone label, not a git branch. Release = git tag on mainline when `release` stage
completes (e.g. `v0.1.0`).

## Priority Labels

Every issue must carry exactly one priority label:

| Label | Meaning |
|-------|---------|
| `P0` | Release blocker — drop everything, must fix before any other progress |
| `P1` | High — fix before the current stage completes |
| `P2` | Normal — standard priority (default for new issues) |
| `P3` | Low — fix if time allows, can slip to next version |
| `P4` | Backlog — acknowledged but not scheduled |

**Who sets priority:**
- `init` seeds research issues at `P2` unless the spec flags a topic as critical (`P1`)
- `scoping` assigns priority to implementation issues based on the dependency graph —
  blockers get `P1`, leaf nodes get `P2`
- `qa` files bugs at `P0` (crash / data loss) or `P1` (wrong output / spec violation)
- Humans may override any priority at any time

**How workers respect priority:**
- Dispatch queue sorted by priority ascending (P0 first) before claiming the next issue
- Within the same priority, oldest issue (lowest number) first

## Module Isolation

Agents are scoped to these modules. One agent per module at a time:

| Module | Scope |
|--------|-------|
| `config` | `src/brimstone/config.py` |
| `runner` | `src/brimstone/runner.py`, `src/brimstone/session.py` |
| `health` | `src/brimstone/health.py` |
| `logging` | `src/brimstone/logger.py` |
| `cli` | `src/brimstone/cli.py`, `src/brimstone/skills/` (all skill files) |
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

`mainline`

## Pipeline Stage Tracking

Each pipeline stage transition must be tracked as a GitHub issue so the current project
phase is always visible from the issue tracker.

### Issue format

Title: `<stage>: <version>`

Examples:
- `research: v0.1.0`
- `design: v0.1.0`
- `implementation: v0.1.0`
- `qa: v0.1.0`
- `release: v0.1.0`
- `init: v0.2.0`

### Lifecycle

1. **Filed** — when the previous stage completes, the worker files the next stage's issue
2. **`in-progress`** — added when the stage begins (same as any other issue)
3. **Closed** — when the stage completes; the closing comment links to the session report

### Who files what

| Stage completes | Files next issue |
|----------------|-----------------|
| `init` | `research: <version>` |
| `research` | `design: <version>` |
| `design` | `scoping: <version>` |
| `scoping` | `implementation: <version>` |
| `implementation` | `qa: <version>` |
| `qa` | `release: <version>` (if clean) or loops back to `implementation: <version>` (if bugs filed) |
| `release` | `init: <next version>` |

```bash
gh issue create \
  --repo <owner>/<repo> \
  --title "<stage>: <version>" \
  --label "pipeline" \
  --milestone "<version>"
```

## Research Issue Triage

Every research issue — including follow-ups created by agents — **must pass the triage rubric
before being dispatched**. Apply it immediately after an agent creates follow-up issues (Step 3
of the research loop), not at dispatch time.

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

Research for a milestone is **done enough to begin design** when all remaining open
research issues are non-blocking.

### Blocking vs. non-blocking test

A research issue is **blocking** if answering it would change the *design* (not just the
*quality*) of a current-milestone implementation issue. Examples:
- Blocking: "Does `--permission-prompt-tool` work in the current CLI?" — if no, the entire
  security architecture in the runner must change.
- Non-blocking: "What is the exact backoff curve for 429 retries?" — implementation proceeds
  with a reasonable default; the answer refines but doesn't redesign.

### Gate procedure

Before each research dispatch batch, the orchestrator checks:

```
For each open research issue in the active milestone:
  Is there a specific current-milestone implementation issue that cannot be
  designed without this answer?
    YES → blocking, must dispatch before design starts
    NO  → non-blocking, migrate to the next version's research milestone
```

If **zero blocking issues remain**, declare research complete:
1. Migrate all remaining non-blocking open research issues to the next version milestone
2. Post the session report
3. STOP — do not create more research issues just to fill the queue

## Follow-Up Milestone Assignment

When a research agent (or the orchestrator) creates a new issue, it must choose the correct
milestone — **do not default to the parent's milestone**.

### Decision tree

```
Is this follow-up needed to design a current-version implementation issue?
  YES → current milestone
  NO  → next version milestone (default for anything non-blocking)
```

### Practical guidance for agents

When writing the "Follow-Up Research Recommendations" section of a doc, tag each item:

- `[BLOCKS_IMPL]` — must be researched before the current implementation milestone
- `[V2_RESEARCH]` — useful but doesn't block current version; file under the next milestone
- `[WONT_RESEARCH]` — not worth a standalone doc; note inline

Only create GitHub issues for `[BLOCKS_IMPL]` and `[V2_RESEARCH]` items.

To find the correct milestone, inspect what exists and pick by purpose — do not hardcode names:
```bash
gh milestone list --repo <owner>/<repo>
```

## Issue Labels

### Stage
| Label | Purpose |
|-------|---------|
| `stage/research` | Research and investigation tasks |
| `stage/design` | Design tasks (HLD, LLD) |
| `stage/impl` | Implementation tasks (code + tests) |

### Priority
| Label | Purpose |
|-------|---------|
| `P0` | Release blocker |
| `P1` | High priority |
| `P2` | Normal (default) |
| `P3` | Low priority |
| `P4` | Backlog |

### Status
| Label | Purpose |
|-------|---------|
| `in-progress` | Currently being worked on (managed by orchestrator) |
| `triage` | Follow-up issues pending triage decision (applied by research agents) |
| `wont-research` | Closed by triage gate — below threshold for standalone research |
| `pipeline` | Pipeline stage transition tracking issues |
| `bug` | Defect filed by qa or reported post-release |

### Feature / domain
| Label | Purpose |
|-------|---------|
| `feat:config` | Config dataclass + env resolution |
| `feat:runner` | Headless claude -p runner |
| `feat:health` | Preflight health checks |
| `feat:logging` | JSONL logging + cost ledger |
| `feat:cli` | CLI wiring + skill files |
| `infra` | Repo scaffolding, CI, pyproject |
| `core` | Core pipeline plumbing |
