# breadmin-conductor

Headless Claude Code orchestrator CLI. Packages the `issue-worker` and `research-worker`
loops as standalone CLI commands that invoke `claude -p` without a human at the terminal.

## What it does

- `issue-worker` — claims GitHub issues, dispatches sub-agents via `claude -p`, monitors
  CI, and squash-merges passing PRs
- `research-worker` — processes research issues for a milestone, writes `docs/research/`
  documents, and creates follow-up issues
- `conductor health` — preflight check (claude, gh, git, ANTHROPIC_API_KEY)
- `conductor cost` — cost ledger summary from all sessions

## Package structure

```
src/conductor/
├── cli.py        ← click group, issue-worker + research-worker commands
├── config.py     ← Config pydantic model, env/flag resolution
├── runner.py     ← claude -p subprocess invocation, stream-json capture
├── session.py    ← session ID persistence and --resume logic
├── logger.py     ← per-session JSONL logging + cost ledger
├── health.py     ← preflight checks
└── skills/
    ├── issue-worker.md     ← bundled skill prompt
    └── research-worker.md  ← bundled skill prompt
```

## Usage

```bash
# Preflight check
conductor health

# Run issue worker (processes all open non-research issues in the repo)
issue-worker --repo OWNER/REPO

# Run research worker for a specific milestone
research-worker --repo OWNER/REPO --milestone "M1: Foundation"

# Dry run (prints claude invocation without executing)
issue-worker --repo OWNER/REPO --dry-run

# Resume a previous session
issue-worker --repo OWNER/REPO --resume SESSION_ID

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
