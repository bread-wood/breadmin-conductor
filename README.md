# brimstone

Headless Claude Code orchestrator for automated GitHub issue workstreams.

Runs a multi-stage pipeline (research → design → scoping → implementation → QA → release)
against a target GitHub repo by invoking `claude -p` headlessly. Each stage dispatches
sub-agents in parallel, monitors their output, and merges results back to the default branch.

## Installation

```bash
uv sync
```

Requires Python 3.11+ and the following tools on `$PATH`:

- `claude` — Claude Code CLI (`claude -p` headless mode)
- `gh` — GitHub CLI (authenticated)
- `git`

## Pipeline

```
spec → init → research → design → scoping → implementation → qa → release
```

No stage may be skipped. Each stage is triggered via `brimstone run --<stage>`.
See `CLAUDE.md` for the full orchestration protocol.

## Commands

```
brimstone run     Run one or more pipeline stages for a milestone
brimstone init    Upload spec + create milestone + seed research issues
brimstone health  Preflight checks (credentials, repo state, active worktrees)
brimstone cost    Cost ledger summary
brimstone adopt   Adopt an existing repo (not yet implemented)
```

### `brimstone run`

```bash
# Research stage
brimstone run --research --repo OWNER/REPO --milestone "v0.1.0-cold-start"

# Design stage (after research completes)
brimstone run --design --repo OWNER/REPO --milestone "v0.1.0-cold-start"

# Implementation stage
brimstone run --impl --repo OWNER/REPO --milestone "v0.1.0-cold-start"

# All stages in order
brimstone run --all --repo OWNER/REPO --milestone "v0.1.0-cold-start"
```

Common flags: `--dry-run`, `--model <model-id>`, `--max-budget <usd>`.

### `brimstone init`

```bash
brimstone init --repo OWNER/REPO \
               --spec docs/specs/v0.1.x-cold-start.md \
               --milestone "v0.1.0-cold-start"
```

Uploads the spec to `docs/specs/<spec-stem>.md` in the target repo, creates the
GitHub milestone with the given name, and seeds the first batch of `stage/research` issues.

### `--repo` resolution

| Invocation | Behaviour |
|---|---|
| *(no flag)* | Operates on the current working directory. Fails if cwd is not a git repo. |
| `--repo owner/name` | Clones the remote repo to a temp dir and operates on it. |
| `--repo path/to/local/dir` | Operates on the local directory. Fails if not a git repo. |
| `--repo name` | Scaffolds a new private GitHub repo named `name`, then operates on it. |

## Module Listing

```
src/brimstone/
├── cli.py          ← Click entry point: brimstone (subcommands: run, init, health, cost, adopt)
├── config.py       ← Config pydantic-settings model; env/flag resolution; subprocess env builder
├── runner.py       ← claude -p subprocess invocation; stream-json capture; RunResult
├── session.py      ← Session ID persistence and --resume logic
├── logger.py       ← Per-session JSONL logging and cost ledger
├── health.py       ← Preflight checks (claude, gh, git, credentials, worktrees)
└── skills/
    ├── impl-worker.md      ← Bundled system prompt for the implementation stage
    ├── research-worker.md  ← Bundled system prompt for the research stage
    ├── design-worker.md    ← Bundled system prompt for the design stage
    └── plan-milestones.md  ← Bundled system prompt for brimstone init
```

## Key Types

- `Config` (`config.py`) — validated configuration; loaded from environment + CLI flags
- `Checkpoint` (`session.py`) — persisted session state for `--resume`
- `RunResult` (`runner.py`) — result of a single `claude -p` invocation
- `HealthReport` (`health.py`) — preflight check outcome with fatal/warn status
- `UsageGovernor` (`cli.py`) — enforces concurrency limits and rate-limit backoff

## Configuration

Set environment variables or create a `.env` file in the working directory:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key |
| `BRIMSTONE_GH_TOKEN` or `GH_TOKEN` | Yes | — | GitHub token passed to sub-agents |
| `BRIMSTONE_MODEL` | No | `claude-opus-4-6` | Claude model ID |
| `BRIMSTONE_MAX_BUDGET_USD` | No | `5.00` | USD budget cap per session |
| `BRIMSTONE_MAX_CONCURRENCY` | No | `5` | Max parallel sub-agents |
| `BRIMSTONE_AGENT_TIMEOUT_MINUTES` | No | `30` | Timeout per sub-agent |
| `BRIMSTONE_DEFAULT_BRANCH` | No | `main` | Default branch name of the target repo |
| `BRIMSTONE_LOG_DIR` | No | `~/.brimstone/logs` | Session logs and cost ledger |
| `BRIMSTONE_CHECKPOINT_DIR` | No | `~/.brimstone/checkpoints` | Session checkpoints |

## Dependencies

- `click>=8.1` — CLI framework
- `pydantic>=2.0` — data validation
- `pydantic-settings>=2.0` — environment-based configuration
