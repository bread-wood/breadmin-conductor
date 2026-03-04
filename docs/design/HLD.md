# brimstone — High-Level Design

**Status:** Current
**Version:** v0.1.1

---

## 1. System Overview

brimstone is a Python CLI orchestrator that automates multi-stage GitHub issue workstreams using Claude Code in headless `-p` mode. It runs as a single orchestrator process (`brimstone run`) that coordinates isolated Claude Code sub-agents across five pipeline stages:

```
plan → research → design → scope → impl
```

| Stage | Entry point | Produces |
|-------|-------------|---------|
| `plan` | `brimstone run --stage plan` | GitHub milestone + `stage/research` issues seeded from spec |
| `research` | `brimstone run --stage research` | `docs/research/<N>-<slug>.md` per issue |
| `design` | `brimstone run --stage design` | `docs/design/HLD.md` + `docs/design/lld/<module>.md` |
| `scope` | `brimstone run --stage scope` | `stage/impl` issues decomposed from HLD/LLD docs |
| `impl` | `brimstone run --stage impl` | Merged PRs, working code |

Each stage's output is the next stage's input. Design and issue decomposition are explicitly separated: the design stage produces design documents; scope decomposes those into actionable GitHub issues.

**What it is not:** brimstone is not a general-purpose AI agent framework. It is purpose-built for the workstream: plan → research → design → scope → implement.

---

## 2. Component Map

```
┌─────────────────────────────────────────────────────────────┐
│  cli.py  (entry point: brimstone run / health / cost)       │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────┐  │
│  │  config  │  │  health  │  │  session │  │   logger  │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └─────┬─────┘  │
│       └─────────────┴──────────────┴──────────────┘        │
│                            │                               │
│  ┌────────────┐     ┌──────▼──────┐                        │
│  │  beads.py  │     │   runner    │                        │
│  │ BeadStore  │     │  (claude -p │                        │
│  │ WorkBead   │     │  subprocess)│                        │
│  │ PRBead     │     └─────────────┘                        │
│  │ MergeQueue │                                            │
│  └────────────┘                                            │
│  skills/  (markdown skill files, injected into prompts)    │
└─────────────────────────────────────────────────────────────┘
```

| Module | Responsibility |
|--------|----------------|
| `config` | Pydantic-settings model; env var resolution; subprocess env dict construction |
| `runner` | `claude -p` subprocess invocation; stream-json parsing; `RunResult` extraction |
| `session` | Checkpoint persistence (schema v3); run metadata; backoff state |
| `beads` | BeadStore, WorkBead, PRBead, MergeQueue; atomic bead reads/writes; `flush()` to optional state repo |
| `logger` | Four JSONL log streams: conductor events, agent transcripts, cost ledger, health log |
| `health` | Preflight checks: credentials, git state, stale worktrees, orphaned issues, yeast-bot collaborator |
| `cli` | Entry point wiring; startup sequence; worker loops; MergeQueue drain; Watchdog |
| `skills/` | Markdown skill definitions read and injected into agent prompts at dispatch time |

**Dependency edges (A → B means A depends on B):**

```
cli → config, health, session, beads, logger, runner
health → config, session
runner → config
logger → config
session → config
beads → config
skills/ → (no code deps; read as text by cli)
```

---

## 3. Execution Model

### 3.1 State Model: Two Layers

brimstone uses two complementary state stores:

**`checkpoint.json`** (schema v3) — run metadata and backoff state. Slim, fast, written on every state transition. Fields: `run_id`, `timestamp`, `schema_version`, `backoff_until`, `backoff_reason`.

**Bead files** (`~/.brimstone/beads/<owner>/<repo>/`) — issue and PR lifecycle. Written atomically via `.tmp` + `os.replace`. Three types:
- `work/<N>.json` — WorkBead: one per issue, tracks `open → claimed → pr_open → merge_ready → closed/abandoned`
- `prs/pr-<N>.json` — PRBead: one per PR, tracks CI and review state
- `merge-queue.json` — MergeQueue: ordered list of PRs waiting for squash merge

The checkpoint is lightweight and always in `~/.brimstone/`. Beads are the authoritative issue lifecycle store. Beads survive restarts, enable cross-session recovery, and can be pushed to an optional `state_repo` for cross-machine durability.

### 3.2 Agent Isolation via Git Worktrees

Sub-agents work in isolated git worktrees managed by Claude Code via `Agent(isolation:"worktree")`. The main checkout is never touched by agents.

```
main checkout (./)
  │
  └─ .claude/worktrees/
        ├─ 10-feat-config/   ← agent A's isolated copy
        ├─ 16-feat-runner/   ← agent B's isolated copy
        └─ 31-feat-session/  ← agent C's isolated copy
```

Worktrees are created automatically by the `isolation:"worktree"` parameter and cleaned up after the agent completes or the Watchdog recovers them.

### 3.3 End-to-End Sequence: impl Dispatch Cycle

```
brimstone run --stage impl
  │
  ├─ startup_sequence() → (Config, Checkpoint, BeadStore)
  ├─ health checks → abort if fatal
  ├─ _resume_stale_issues(store) → re-dispatch any claimed-but-stale work
  │
  ├─ select open impl issues (milestone, stage/impl label, no assignee)
  │
  ├─ FOR EACH issue (sequential claim):
  │     ├─ gh issue edit → add assignee + in-progress label
  │     ├─ create branch: git checkout -b <N>-<slug> origin/<default>
  │     ├─ write WorkBead(state="claimed")
  │     └─ store.flush()
  │
  ├─ DISPATCH agents in parallel via Agent(isolation:"worktree"):
  │     ├─ Agent A → issue #10 → implements → pushes → creates PR → outputs "Done." → STOPS
  │     ├─ Agent B → issue #16 → implements → pushes → creates PR → outputs "Done." → STOPS
  │     └─ Agent C → issue #31 → implements → pushes → creates PR → outputs "Done." → STOPS
  │
  │   Agents handle CI and review feedback autonomously:
  │     - CI max 3 attempts; review max 2 attempts
  │     - On each attempt: read feedback → triage → fix now / file issue / skip
  │     - After max attempts: comment on PR and output "Done."
  │
  ├─ _monitor_pr() for each PR:
  │     ├─ write PRBead states as CI progresses
  │     └─ on CI pass → enqueue MergeQueue
  │
  ├─ every WATCHDOG_INTERVAL=5 pool iterations:
  │     └─ _watchdog_scan() → detect zombie PRs → dispatch recovery or exhaust
  │
  ├─ _process_merge_queue() after each batch:
  │     ├─ pop entry → rebase onto default branch
  │     ├─ gh pr merge --squash --delete-branch
  │     ├─ write WorkBead(state="closed") + PRBead(state="merged")
  │     └─ store.flush()
  │
  └─ repeat until no open issues remain
```

### 3.4 yeast-bot: Worker Agent Account

Worker agents create PRs and push branches using the `yeast-bot` GitHub account. This is a dedicated bot account configured via `BRIMSTONE_GH_TOKEN`. The health check verifies yeast-bot is a collaborator on the target repo and auto-adds it if missing using the GitHub collaborator API (`repos/{owner}/{repo}/collaborators/{username}`).

---

## 4. Error Classification

The authoritative error signal is the `result` event in stream-json output. Exit codes are too coarse for recovery decisions.

### 4.1 `result` Event Schema

```json
{
  "type": "result",
  "is_error": true,
  "error_code": "rate_limit_error",
  "subtype": "error_during_execution",
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
| `billing_error` / `error_during_execution` | 402 (billing failure) | Pause dispatch; human escalation |
| `invalid_grant` | Auth failure | Pause; credential refresh |
| `extra_usage_exhausted` | Overage cap | Pause; human escalation |
| `auth_failure` | Token invalid | Abort; fix credentials |
| `max_turns_exceeded` | `--max-turns` hit | Re-dispatch with higher limit (up to max) |
| *(none, `is_error: false`)* | Success | Log cost; continue |

**429 vs. 402 discrimination:** HTTP 429 → `rate_limited` → backoff and retry. HTTP 402 billing cap → `error_during_execution` → human escalation, not automatic retry.

### 4.3 Retry Policy

```
rate_limited:         backoff until checkpoint.backoff_until; then re-dispatch
max_turns_exceeded:   re-dispatch with higher --max-turns (up to config max)
all other errors:     terminal → clean up → human escalation (Watchdog)
```

After `WATCHDOG_MAX_FIX_ATTEMPTS=3` failed recoveries on any issue: mark WorkBead abandoned, post comment on issue.

---

## 5. MergeQueue

The MergeQueue prevents rebase conflicts from concurrent PRs landing at the same time. Only one PR is merged at a time, in FIFO order.

```
_monitor_pr() → CI pass → enqueue MergeQueue entry

_process_merge_queue() (called after each pool batch):
  while queue not empty:
    entry = queue.entries[0]
    git fetch origin
    git rebase origin/<default>   ← may conflict → write PRBead(conflict) → skip
    gh pr merge <N> --squash --delete-branch
    write WorkBead(closed) + PRBead(merged)
    remove entry from queue
    store.flush()
```

This is inspired by Steve Yegge's Gas Town Refinery pattern (see Prior Art).

---

## 6. Watchdog

The Watchdog runs every `WATCHDOG_INTERVAL=5` pool iterations. It detects zombie PRs — PRs that are stuck in `ci_failing` or `conflict` state for longer than `WATCHDOG_TIMEOUT_MINUTES=45`.

```
_watchdog_scan():
  for bead in store.list_pr_beads(state=["ci_failing", "conflict"]):
    if age_minutes(bead) > WATCHDOG_TIMEOUT_MINUTES:
      if bead.fix_attempts < WATCHDOG_MAX_FIX_ATTEMPTS (3):
        _dispatch_recovery_agent(bead)  ← increment fix_attempts; dispatch agent
      else:
        _exhaust_issue(bead)            ← mark abandoned; post comment; flush
```

This is inspired by Steve Yegge's Gas Town Deacon pattern (renamed Watchdog in brimstone — see Prior Art).

---

## 7. BeadStore Flush

`store.flush()` is called at every state-changing operation to ensure durability:

1. Git add all changed bead files in `~/.brimstone/beads/<owner>/<repo>/`
2. Git commit with a structured message
3. If `config.state_repo` is set: `git push origin main` to the state repo

The atomic write pattern uses `.tmp` + `os.replace` so readers never see a partial bead file.

Flush points:
- Claim: WorkBead(claimed) written + flush
- PR open: PRBead(open) written + flush
- CI pass: PRBead(merge_ready) written + enqueue + flush
- Merge: WorkBead(closed) + PRBead(merged) + dequeue + flush
- Exhaust: WorkBead(abandoned) + PRBead(abandoned) + flush
- Watchdog recovery dispatch: PRBead(fix_attempts++) + flush

---

## 8. Security Architecture

### Layer 1 — Input Sanitization

Untrusted content (issue bodies, PR descriptions, code comments) is sanitized before inclusion in agent prompts: strip shell metacharacters, escape triple-backtick sequences, truncate to safe length.

### Layer 2 — Environment Isolation

Each subprocess receives an explicit, minimal env dict. The parent process env is never inherited (`env=` kwarg to `subprocess.run`, not `env=None`). This prevents secret leakage and recursive orchestrator invocation.

### Layer 3 — Tool Permission Policy

`--allowedTools` is set per worker type. Workers cannot branch-delete or force-push.

### Layer 4 — OS Process Isolation

Each `claude -p` invocation runs as a separate OS process. Agents cannot access the orchestrator's memory or open file handles.

---

## 9. Observability Model

### Four Log Streams

| Stream | File | Audience |
|--------|------|----------|
| Cost ledger | `~/.brimstone/logs/cost.jsonl` | Billing reconciliation; `brimstone cost` CLI |
| Agent transcripts | `~/.brimstone/logs/transcripts/<session-id>.jsonl` | Debugging; audit trail |
| Conductor log | `~/.brimstone/logs/conductor/<run-id>.jsonl` | Orchestrator decisions; stage transitions |
| Health log | `~/.brimstone/logs/health.jsonl` | Preflight check outcomes |

### Cost Accounting

The `result` event is the sole authoritative source for token counts and cost. The cost ledger records `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, and `total_cost_usd` per agent invocation.

Cache pricing tiers (prompt cache, as of March 2026):
- Cache creation: 125% of input price (5-minute ephemeral)
- Cache read: 10% of input price (1-hour window)

Subscription sessions report `total_cost_usd: 0.0` or null; brimstone estimates cost from token counts using the model's published pricing.

---

## 10. Constraints

### Hard Constraints

| Constraint | Consequence of violation |
|------------|--------------------------|
| Single orchestrator per repo | Race conditions on issue claiming and bead writes |
| Agents output "Done." to signal completion | Orchestrator cannot detect when agent is finished |
| External bead files for state | State lost on orchestrator restart if beads not flushed |
| Agents never merge PRs | Only orchestrator merges, after CI + review gate |
| `result.subtype` for error classification | Exit codes are too coarse; wrong retry decisions |

### Rate Limit Constraints

Pre-dispatch budget check is mandatory. On 429: exponential backoff (base 2, max 32 min), record `backoff_until` in checkpoint, requeue.

`rate_limit_event` in stream-json provides early warning at ~80-90% window usage (`status: "allowed"`, `usedPercentage > 80`) before hard rejection (`status: "rejected"`).

---

## 11. Prior Art / Acknowledgements

### Gas Town (Steve Yegge)

brimstone drew heavily from Steve Yegge's **Gas Town** project ([github.com/steveyegge/gastown](https://github.com/steveyegge/gastown); SE Daily interview, February 2026). Key borrowings:

| Gas Town concept | brimstone equivalent | Notes |
|---|---|---|
| Mayor | Orchestrator (`cli.py` / `brimstone run`) | Developed independently |
| Polecats | Sub-agents via `Agent(isolation:"worktree")` | Developed independently |
| Beads | WorkBead, PRBead, MergeQueue | Name + persistence insight borrowed; structure diverged |
| The Refinery | `_process_merge_queue()` + MergeQueue | Concept borrowed; renamed |
| The Deacon | `_watchdog_scan()` | Name borrowed → renamed to Watchdog in brimstone |
| Git worktrees | `isolation:"worktree"` | Both arrived independently |

**What brimstone borrowed from Gas Town:**
- The "beads" name and the core insight: persist agent work state as structured files that survive crashes and enable reliable handoffs between sessions.
- The Refinery pattern: serialize merges through a queue to prevent rebase conflicts.
- The Deacon/watchdog concept: scan for zombie agents and dispatch recovery or exhaust.

**What brimstone did differently:** Gas Town's beads are generic work items stored inside git worktrees. brimstone's beads are three typed structures (WorkBead, PRBead, MergeQueue) stored outside the repo in `~/.brimstone/beads/<owner>/<repo>/`, with atomic POSIX rename and an optional separate `state_repo`. Tuned to the GitHub PR lifecycle, not generic task tracking.

The orchestrator/agent split and git worktree isolation were developed independently by brimstone before encountering Gas Town. Gas Town arrived at the same worktree pattern independently.
