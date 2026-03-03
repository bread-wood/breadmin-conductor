# brimstone

brimstone — headless Claude Code orchestrator for automated GitHub issue workstreams.

Runs a four-stage research → design → implementation pipeline entirely from the terminal,
invoking `claude -p` headlessly (no human at the terminal) to process GitHub issues,
write research docs, translate findings into scoped implementation issues, and merge PRs.
Each stage is driven by a dedicated CLI command that dispatches sub-agents in parallel and
monitors CI until all work is complete.

## Installation

```bash
uv sync
```

Requires Python 3.11+ and the following external tools on `$PATH`:

- `claude` — Claude Code CLI (`claude -p` headless mode)
- `gh` — GitHub CLI (authenticated)
- `git`

## Worker Pipeline

```
plan-issues → research-worker → design-worker → impl-worker
```

Each product version flows through all four stages in order.
No stage may be skipped. See `CLAUDE.md` for the full orchestration protocol.

## Entry Points

| Command | Description |
|---------|-------------|
| `research-worker` | Processes research issues for a milestone; writes docs to `docs/research/` |
| `design-worker` | Translates completed research docs into scoped implementation issues |
| `plan-issues` | Plans milestones and seeds research issues for the next version |
| `impl-worker` | Claims impl issues, dispatches sub-agents, monitors CI, and merges PRs |
| `brimstone` | Admin commands: `health` (preflight checks) and `cost` (cost ledger summary) |

## Quick Usage

```bash
# Preflight check
brimstone health --repo OWNER/REPO

# Plan milestones and seed research issues for a new version
plan-issues --repo OWNER/REPO

# Research phase
research-worker --repo OWNER/REPO --milestone "MVP Research"

# Design phase (after research declares complete)
design-worker --repo OWNER/REPO --research-milestone "MVP Research"

# Implementation phase
impl-worker --repo OWNER/REPO --milestone "MVP Implementation"

# Show cost ledger
brimstone cost
```

Each worker accepts `--dry-run` (print without executing), `--model` (override Claude model),
`--max-budget` (USD cap), `--max-turns`, and `--resume <run-id>` (resume a previous session).

## Module Listing

```
src/brimstone/
├── cli.py          ← Click entry points: research-worker, design-worker,
│                     plan-issues, impl-worker, brimstone
├── config.py       ← Config pydantic-settings model; env/flag resolution
├── runner.py       ← claude -p subprocess invocation; stream-json capture
├── session.py      ← Session ID persistence and --resume logic
├── logger.py       ← Per-session JSONL logging and cost ledger
├── health.py       ← Preflight checks (claude, gh, git, ANTHROPIC_API_KEY)
└── skills/
    ├── impl-worker.md      ← Bundled skill prompt for impl-worker
    ├── research-worker.md  ← Bundled skill prompt for research-worker
    ├── design-worker.md    ← Bundled skill prompt for design-worker
    └── plan-milestones.md  ← Bundled skill prompt for plan-issues
```

## Key Types

- `Config` (`config.py`) — validated configuration; loaded from environment + CLI flags
- `Checkpoint` (`session.py`) — persisted session state for `--resume`
- `RunResult` (`runner.py`) — result of a single `claude -p` invocation
- `HealthReport` (`health.py`) — preflight check outcome with fatal/warn status
- `UsageGovernor` (`cli.py`) — enforces concurrency limits and rate-limit backoff

## Configuration

Set environment variables or create a `.env` file:

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Required — Anthropic API key |
| `BRIMSTONE_MODEL` | Claude model to use (default: `claude-opus-4-5`) |
| `BRIMSTONE_MAX_BUDGET` | USD budget cap per session |
| `BRIMSTONE_MAX_TURNS` | Max turns per `claude -p` invocation |
| `BRIMSTONE_DATA_DIR` | Directory for checkpoints and logs (default: `~/.brimstone`) |

## Dependencies

- `click>=8.1` — CLI framework
- `pydantic>=2.0` — data validation
- `pydantic-settings>=2.0` — environment-based configuration
