# brimstone

brimstone — autonomous GitHub issue workstream orchestrator

## How It Works

brimstone runs a five-stage pipeline (plan → research → design → scope → impl) against a
target GitHub repo by invoking Claude Code sub-agents in isolated git worktrees. Each stage
reads the previous stage's output from GitHub issues and docs, then produces the next stage's
input.
Bead files (`~/.brimstone/beads/`) provide durable state so that the orchestrator survives
restarts and rate-limit backoffs. A Watchdog loop detects zombie agents and dispatches recovery
sub-agents automatically. A MergeQueue ensures sequential squash merges to keep the commit
history linear. Sub-agents own their own CI and review feedback — the orchestrator only merges
after CI passes.

## Prerequisites

- `claude` — Claude Code CLI (with `-p` headless mode)
- `gh` — GitHub CLI (authenticated)
- `git`
- Python 3.11+
- yeast-bot GitHub account added as a repo collaborator (for agent PR creation); configure
  via `GH_TOKEN`

## Installation

```bash
uv sync
```

## Quick Start

```bash
# Run all stages from a spec file (milestone inferred from filename)
brimstone run specs/v0.2.0-function-library.md --repo OWNER/REPO

# Or run stages individually in order:
brimstone run --stage research --repo OWNER/REPO --milestone v0.2.0
brimstone run --stage design   --repo OWNER/REPO --milestone v0.2.0
brimstone run --stage scope    --repo OWNER/REPO --milestone v0.2.0
brimstone run --stage impl     --repo OWNER/REPO --milestone v0.2.0
```

## `--repo` Resolution

| Invocation | Behaviour |
|---|---|
| `--repo OWNER/REPO` | Uses the specified GitHub repo |
| `BRIMSTONE_REPO=OWNER/REPO` env var | Fallback when `--repo` is not passed |
| *(no flag, no env var)* | Auto-detects from `git remote get-url origin` in CWD |

## Module Listing

```
src/brimstone/
├── cli.py          ← Entry point: brimstone run, health, cost, init; Watchdog; MergeQueue
├── config.py       ← Config pydantic-settings model; env/flag resolution; subprocess env
├── runner.py       ← claude -p subprocess invocation; stream-json parsing; RunResult
├── session.py      ← Checkpoint (schema v3): run metadata + backoff state
├── beads.py        ← WorkBead, PRBead, MergeQueue, BeadStore; atomic bead I/O
├── logger.py       ← Four JSONL log streams; cost ledger; prompt cache accounting
├── health.py       ← Preflight checks: credentials, gh CLI, git, yeast-bot collaborator
└── skills/
    ├── impl-worker.md      ← Prompt for implementation stage agents
    ├── research-worker.md  ← Prompt for research stage agents
    ├── design-worker.md    ← Prompt for design stage agents
    └── scope-worker.md     ← Prompt for scope stage agents
```

## Key Types

| Type | Module | Purpose |
|------|--------|---------|
| `Config` | `config.py` | Validated configuration; loaded from environment + CLI flags |
| `RunResult` | `runner.py` | Result of a single `claude -p` invocation |
| `Checkpoint` | `session.py` | Run metadata and backoff state (schema v3) |
| `BeadStore` | `beads.py` | Atomic read/write for WorkBead, PRBead, MergeQueue files |
| `WorkBead` | `beads.py` | Issue lifecycle state (open → claimed → pr_open → closed) |
| `PRBead` | `beads.py` | PR lifecycle state (open → ci_running → merge_ready → merged) |
| `MergeQueue` | `beads.py` | Ordered list of PRs ready to squash merge |

## Configuration

Set environment variables or create a `.env` file in the working directory:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key |
| `GH_TOKEN` | Yes | — | GitHub token for yeast-bot; used by sub-agents to create PRs and push branches |
| `BRIMSTONE_MODEL` | No | `claude-opus-4-6` | Claude model ID |
| `BRIMSTONE_MAX_BUDGET_USD` | No | `5.00` | USD budget cap per session (API key mode only) |
| `BRIMSTONE_MAX_CONCURRENCY` | No | `5` | Max parallel sub-agents |
| `BRIMSTONE_AGENT_TIMEOUT_MINUTES` | No | `30` | Timeout per sub-agent |
| `BRIMSTONE_LOG_DIR` | No | `~/.brimstone/logs` | Session logs and cost ledger |
| `BRIMSTONE_CHECKPOINT_DIR` | No | `~/.brimstone/checkpoints` | Run checkpoints |
| `BRIMSTONE_BEADS_DIR` | No | `~/.brimstone/beads` | Bead files root directory |
| `BRIMSTONE_STATE_REPO` | No | — | Optional git repo URL for pushing bead state |
| `BRIMSTONE_STATE_REPO_DIR` | No | — | Local clone path for `BRIMSTONE_STATE_REPO` |
| `BRIMSTONE_REPO` | No | — | Default target repo (`OWNER/REPO`); overridable with `--repo` |

## State Files

```
~/.brimstone/
  checkpoints/
    impl.checkpoint.json        ← Run metadata + backoff state (schema v3)
    research.checkpoint.json
  logs/
    cost.jsonl                  ← Permanent cost ledger (append-only)
    sessions/
      <session-id>.jsonl        ← Per-agent execution log
    conductor/
      <run-id>.jsonl            ← Orchestrator decisions and stage transitions
  beads/
    <owner>/
      <repo>/
        work/
          <N>.json              ← WorkBead per issue
        prs/
          pr-<N>.json           ← PRBead per PR
        merge-queue.json        ← MergeQueue
```

## Commands

```
brimstone run     Run one pipeline stage for a milestone
brimstone health  Preflight checks (credentials, repo state, yeast-bot, worktrees)
brimstone cost    Cost ledger summary
brimstone init    Scaffold a repo (add yeast-bot, install CI workflow, create labels)
```

## Dependencies

- `click>=8.1` — CLI framework
- `pydantic>=2.0` — data validation
- `pydantic-settings>=2.0` — environment-based configuration
