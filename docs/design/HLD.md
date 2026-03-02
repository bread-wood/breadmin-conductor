# breadmin-composer — High-Level Design

**Status:** Draft
**Milestone:** M2: Implementation
**Research baseline:** M1: Foundation (all docs in `docs/research/`)

---

## 1. System Overview

breadmin-composer is a Python CLI orchestrator that automates multi-stage GitHub issue workstreams using Claude Code in headless `-p` mode. It runs as a single orchestrator process that coordinates isolated Claude Code subprocesses (agents) across six pipeline stages:

```
spec → plan-milestones → research-worker → design-worker → plan-issues → impl-worker
```

| Stage | Entry point | Produces |
|-------|-------------|---------|
| spec | *(human)* | `docs/specs/<version>.md` — scope, success criteria, constraints |
| plan-milestones | *(human + orchestrator)* | GitHub milestone pair, seed research issues |
| research-worker | `research-worker` | `docs/research/<N>-<slug>.md` per issue |
| design-worker | `design-worker` | `docs/design/HLD.md` + `docs/design/lld/<module>.md` |
| plan-issues | `plan-issues` | GitHub impl issues with acceptance criteria and dep graph |
| impl-worker | `impl-worker` | Merged PRs, working code |

No stage is skipped; each stage's output is the next stage's input. Design and issue decomposition are explicitly separated: `design-worker` produces design documents only; `plan-issues` reads those docs and creates actionable GitHub issues.

**What it is not:** breadmin-composer is not a general-purpose AI agent framework. It is purpose-built for the workstream: spec → research → design → issue decomposition → implement.

---

## 2. Component Map

```
┌─────────────────────────────────────────────────────────────┐
│  cli.py  (entry points: impl-worker, research-worker,       │
│           design-worker, composer)                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │  config  │  │  health  │  │  session │  │   logger  │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └─────┬─────┘  │
│       └─────────────┴──────────────┴──────────────┘        │
│                            │                               │
│                     ┌──────▼──────┐                        │
│                     │   runner    │                        │
│                     │  (claude -p │                        │
│                     │  subprocess)│                        │
│                     └─────────────┘                        │
│  skills/  (markdown skill files, injected into prompts)    │
└─────────────────────────────────────────────────────────────┘
```

| Module | Responsibility | Key Consumers |
|--------|----------------|---------------|
| `config` | Pydantic-settings model; env var resolution; per-invocation subprocess env dict construction | `cli`, `runner`, `health` |
| `runner` | `claude -p` subprocess invocation; stream-json parsing; `result` event extraction; error classification | `cli` (worker loops) |
| `session` | Checkpoint persistence; issue state machine; recovery protocol; backoff state | `cli` (worker loops), `health` |
| `logger` | Three JSONL log streams: cost ledger, per-session, conductor; cost accounting from `result` events | `cli`, `runner` |
| `health` | Preflight checks: credentials, git state, stale worktrees, orphaned issues, single-orchestrator guard | `cli` (startup) |
| `cli` | Entry point wiring; startup sequence; worker loops; skill injection; usage governor | — |
| `skills/` | Markdown skill definitions read and injected into agent prompts at invocation time | `cli` |

**Dependency edges (A → B means A depends on B):**

```
cli → config, health, session, logger, runner
health → config, session
runner → config
logger → config
session → config
skills/ → (no code deps; read as text by cli)
```

---

## 3. Execution Model

### 3.1 Stateless Subprocess Chaining

Each worker loop invocation of `claude -p` is **stateless**: the subprocess starts fresh with no memory of prior invocations. State is preserved across subprocess boundaries exclusively via the external checkpoint file (`~/.composer/checkpoints/<run-id>.json`).

```
Orchestrator (worker loop)
  │
  ├─ load checkpoint (session.load)
  ├─ run health checks (health.check_all)
  ├─ build prompt + env dict (config.build_env, skills.inject)
  ├─ invoke claude -p (runner.run)
  │     └─ stream-json events → parse → extract result
  ├─ update checkpoint (session.save)
  ├─ log result (logger.log_cost, logger.log_session_event)
  └─ decide next action (retry / dispatch next / merge / stop)
```

### 3.2 Agent Isolation via Git Worktrees

Sub-agents (spawned by `impl-worker`) work in isolated git worktrees under `.claude/worktrees/<branch-name>/`. The main checkout is never touched by agents.

```
main checkout (./)
  │
  └─ .claude/worktrees/
        ├─ 10-feat-config/   ← agent A's isolated copy
        ├─ 16-feat-runner/   ← agent B's isolated copy
        └─ 31-feat-session/  ← agent C's isolated copy
```

Worktrees are created by the orchestrator before dispatch and cleaned up after PR merge (or on recovery from stale state).

### 3.3 End-to-End Sequence: impl-worker Dispatch Cycle

```
impl-worker startup
  │
  ├─ health checks → abort if fatal
  ├─ load/create checkpoint
  ├─ select open impl issues (milestone, no assignee, not in-progress)
  │
  ├─ FOR EACH issue (sequential):
  │     ├─ gh issue edit → add assignee + in-progress label
  │     ├─ git worktree add .claude/worktrees/<branch>
  │     └─ git push -u origin <branch>
  │
  ├─ DISPATCH agents in parallel (Agent tool, isolation: worktree):
  │     ├─ Agent A → issue #10 → implements → pushes → creates PR → STOPS
  │     ├─ Agent B → issue #16 → implements → pushes → creates PR → STOPS
  │     └─ Agent C → issue #31 → implements → pushes → creates PR → STOPS
  │
  ├─ wait for all agents to complete
  │
  ├─ FOR EACH PR:
  │     ├─ gh pr checks → wait for CI
  │     ├─ gh pr view → check review status
  │     ├─ if conflicts → rebase + force push
  │     └─ if CI + review pass → gh pr merge --squash --delete-branch
  │
  └─ update checkpoint → log → next batch or stop
```

### 3.4 Subprocess Invocation Pattern

```
claude \
  -p "<prompt>" \
  --output-format stream-json \
  --allowedTools "<tool1>,<tool2>" \
  --max-turns <N> \
  [--append-system-prompt-file <CLAUDE.md>] \
  [--mcp-config <mcp-config.json>]
```

Environment dict passed to subprocess:
```python
{
  "ANTHROPIC_API_KEY": "<key>",
  "GITHUB_TOKEN": "<token>",
  "CLAUDECODE": "1",          # nesting guard
  "CONDUCTOR_*": "...",       # orchestrator config vars
  # No other vars from parent env
}
```

---

## 4. Error Taxonomy

The authoritative error signal is the `result` event in stream-json output, not the subprocess exit code. Exit code `1` is generic; `result.subtype` provides granular classification.

### 4.1 `result` Event Schema

```json
{
  "type": "result",
  "is_error": true,
  "error_code": "rate_limit_error",
  "subtype": "rate_limited",
  "total_cost_usd": 0.45,
  "usage": {
    "input_tokens": 10000,
    "output_tokens": 5000,
    "cache_read_input_tokens": 2000,
    "cache_creation_input_tokens": 500
  }
}
```

### 4.2 Error Classification Table

| `result.subtype` | HTTP origin | Orchestrator action |
|------------------|-------------|---------------------|
| `rate_limited` | 429 (5-hour window) | Exponential backoff; record `backoff_until`; requeue issue |
| `billing_error` / `error_during_execution` | 402 (weekly cap or billing failure) | Pause all dispatch; human escalation |
| `invalid_grant` | Auth failure | Pause; prompt credential refresh |
| `quota_exceeded` | Subscription quota | Pause; human escalation |
| `tool_error` | Tool execution failed | Classify as retryable (CI flake) or terminal (scope violation) |
| `max_turns_exceeded` | `--max-turns` hit | Increase `--max-turns` or split issue; re-dispatch |
| `permission_denied` | Tool not in `--allowedTools` | Terminal; fix allowedTools config |
| *(none, `is_error: false`)* | Success | Continue; log cost; update checkpoint |

**429 vs. 402 discrimination** (research `docs/research/64`, `87`, `95`, `100`):
- HTTP 429 → `result.subtype: rate_limited` → backoff and retry
- HTTP 402 billing cap → `result.subtype: error_during_execution` (empirically verified) → human escalation, not automatic retry

### 4.3 Retry Policy

```
tool_error (retryable):   retry up to 3x with exponential backoff (base 2s)
rate_limited:             backoff until `backoff_until`; then re-dispatch
max_turns_exceeded:       re-dispatch with higher --max-turns (up to configured max)
all other errors:         terminal → clean up → human escalation
```

After 3 failed retries on any issue: remove `in-progress` label, post escalation comment on issue, skip to next.

---

## 5. Security Architecture

Four-layer defense-in-depth. Each layer independently limits blast radius of a compromised agent or injected prompt.

### Layer 1 — Input Sanitization

All untrusted content (issue bodies, PR descriptions, code comments, research doc headings) is sanitized before inclusion in agent prompts:
- Strip shell metacharacters (`$`, `` ` ``, `\`, `|`, `;`, `&`)
- Escape triple-backtick sequences that could close a code block
- Truncate to a safe maximum length before prompt inclusion

### Layer 2 — Environment Isolation

Each subprocess receives an explicit, minimal env dict (see §3.4). The parent process env is **never** inherited (`env=` kwarg to `subprocess.run`, not `env=None`). This prevents:
- Leaking parent secrets (other API keys, SSH keys, AWS credentials)
- Subprocess reading `ANTHROPIC_API_KEY` from a source other than the credential proxy
- Recursive orchestrator invocation (`CLAUDECODE=1` guard)

### Layer 3 — Tool Permission Policy

`--allowedTools` is set per worker type at the runner call site:

| Worker | Allowed tools | Explicitly forbidden |
|--------|---------------|----------------------|
| `research-worker` agent | `gh`, `bash` (read-only), `read`, `web_search` | `git`, merge commands |
| `design-worker` | `gh` | `bash`, `read`, file creation |
| `impl-worker` agent | `gh`, `bash`, `read`, `edit`, `write` | branch deletion, `git push --force` |
| Orchestrator itself | `gh`, `bash`, `read`, Agent | direct file writes to agent scope |

**Note:** `--allowedTools` is reliable in non-`bypassPermissions` mode (research `docs/research/61`). The `--permission-prompt-tool` MCP mechanism is available as a fallback for interactive approval of edge cases (research `docs/research/31`, `60`).

### Layer 4 — OS Process Isolation

Each `claude -p` invocation runs as a separate OS process (separate PID, file descriptors, memory space). Agents cannot access the orchestrator's memory or open file handles. In high-trust deployment environments, combine with container sandboxing.

### Threat Mitigations Summary

| Threat | Mitigation |
|--------|-----------|
| T1: Prompt injection via issue body | Layer 1 (sanitization) |
| T2: CLAUDE.md injection | Verify CLAUDE.md hash before each invocation; Layer 1 |
| T3: Credential exposure | Layer 2 (explicit env dict); credential proxy for API key |
| T4: Bash scope creep | Layer 3 (allowedTools); Layer 4 (process isolation) |
| T5: Merge abuse | Orchestrator-only merge gate; CI + review required |
| T6: Lethal trifecta (private data + untrusted content + exfil) | Layers 1+2+3 combined |
| T7: Cascading failures | Bounded retry (3x max); human escalation gate |

---

## 6. Observability Model

### 6.1 Three Log Streams

| Stream | File | Audience | Retention |
|--------|------|----------|-----------|
| Cost ledger | `~/.composer/logs/cost.jsonl` | Billing reconciliation, `composer cost` CLI | Permanent (append-only) |
| Per-session log | `~/.composer/logs/sessions/<session-id>.jsonl` | Debugging, audit trail | Per-run; configurable TTL |
| Conductor log | `~/.composer/logs/conductor/<run-id>.jsonl` | Orchestrator decisions, stage transitions | Per-run; configurable TTL |

### 6.2 Cost Accounting

The `result` event is the **sole authoritative source** for token counts and cost. CLI flags like `--max-budget-usd` apply only to API billing accounts, not subscription sessions. The cost ledger records:

```json
{
  "timestamp": "2026-03-02T10:00:00Z",
  "session_id": "sess_abc123",
  "run_id": "run_xyz789",
  "repo": "owner/repo",
  "stage": "impl-worker",
  "model": "claude-opus-4-6",
  "input_tokens": 10000,
  "output_tokens": 5000,
  "cache_read_input_tokens": 2000,
  "cache_creation_input_tokens": 500,
  "total_cost_usd": 0.45
}
```

### 6.3 Conductor Log Events

Key phase transitions recorded:

```
stage_start        → worker type, run_id, milestone, issue count
issue_claimed      → issue number, branch name
agent_dispatched   → issue number, session_id
agent_completed    → issue number, result.subtype, cost_usd
pr_created         → issue number, pr_number
ci_checked         → pr_number, status (pass/fail/pending)
pr_merged          → pr_number, issue number
backoff_enter      → until (ISO timestamp), trigger subtype
backoff_exit       → resumed_at
human_escalate     → issue number, reason, action_required
checkpoint_write   → path, issue_states_summary
stage_complete     → issues_completed, total_cost_usd
```

---

## 7. Constraints

### Hard Constraints (non-negotiable)

| Constraint | Source | Consequence of violation |
|------------|--------|--------------------------|
| No nested sub-agents | research `01` | Three-level hierarchy tested to fail in headless mode |
| No `--cwd` flag | research `01` | Subprocess inherits caller CWD; manage via prompt/env |
| Single orchestrator per repo | CLAUDE.md | Race conditions on issue claiming and checkpoint writes |
| External checkpoint for state | research `02` | `--resume` unreliable across `CLAUDE_CONFIG_DIR` isolation boundaries |
| Agents never merge PRs | CLAUDE.md | Only orchestrator merges, after CI + review gate |
| `result.subtype` for error classification | research `03`, `87`, `95`, `100` | Exit codes are too coarse; wrong retry decisions |
| Explicit subprocess env dict | research `06` | Parent env leakage exposes secrets to agents |

### Rate Limit Constraints

| Subscription tier | Max concurrent agents | 5-hour window limit |
|-------------------|-----------------------|---------------------|
| Pro | 1–2 | ~45 Opus / 100 Sonnet requests |
| Max | 2–3 | ~5x Pro |
| Max 20x | 3–5 | ~20x Pro |

Pre-dispatch budget check is mandatory. On 429: exponential backoff (base 2, max 32 min), record `backoff_until` in checkpoint, requeue.

### Soft Constraints (configurable)

- Weekly active-hours cap (Pro: 40–80h Sonnet/week): monitor and pause dispatch if approaching threshold
- Per-issue retry limit: default 3, configurable via `CONDUCTOR_MAX_RETRIES`
- Hang detection timeout: default 30 min per agent, configurable via `CONDUCTOR_AGENT_TIMEOUT_MINUTES`
- Max concurrency: default per tier table above, overridable via `CONDUCTOR_MAX_CONCURRENCY`
