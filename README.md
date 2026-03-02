# breadmin-conductor

Headless Claude Code orchestrator CLI. Runs the full research → design → implementation
pipeline as standalone CLI commands that invoke `claude -p` without a human at the terminal.

## Worker Pipeline

```
plan-milestones → research-worker → design-worker → impl-worker
```

Each product version flows through all four stages. See `CLAUDE.md` for the full protocol.

## Commands

- `research-worker` — processes research issues for a milestone, writes `docs/research/` docs
- `design-worker` — translates completed research into scoped implementation issues
- `impl-worker` — claims impl issues, dispatches sub-agents, monitors CI, merges PRs
- `conductor health` — preflight check (claude, gh, git, ANTHROPIC_API_KEY)
- `conductor cost` — cost ledger summary from all sessions

## Package structure

```
src/conductor/
├── cli.py          ← click commands: impl-worker, research-worker, design-worker, conductor
├── config.py       ← Config pydantic model, env/flag resolution
├── runner.py       ← claude -p subprocess invocation, stream-json capture
├── session.py      ← session ID persistence and --resume logic
├── logger.py       ← per-session JSONL logging + cost ledger
├── health.py       ← preflight checks
└── skills/
    ├── impl-worker.md      ← bundled skill prompt
    ├── research-worker.md  ← bundled skill prompt
    ├── design-worker.md    ← bundled skill prompt
    └── plan-milestones.md  ← bundled skill prompt
```

## Usage

```bash
# Preflight check
conductor health

# Plan milestones for a new version
plan-milestones --repo OWNER/REPO  # interactive, run in a Claude Code session

# Research phase
research-worker --repo OWNER/REPO --milestone "MVP Research"

# Design phase (after research declares complete)
design-worker --repo OWNER/REPO --research-milestone "MVP Research"

# Implementation phase
impl-worker --repo OWNER/REPO --milestone "MVP Implementation"

# Show cost ledger
conductor cost
```

## Configuration

Copy `.env.example` to `.env` and fill in values, or set environment variables directly.

Key variables: `ANTHROPIC_API_KEY`, `CONDUCTOR_MODEL`, `CONDUCTOR_MAX_BUDGET`,
`CONDUCTOR_MAX_TURNS`, `CONDUCTOR_DATA_DIR`.

## Dependencies

- Python 3.11+
- `click` — CLI framework
- `pydantic` — config validation
- `claude` — Claude Code CLI (`claude -p` headless mode)
- `gh` — GitHub CLI
- `git`
