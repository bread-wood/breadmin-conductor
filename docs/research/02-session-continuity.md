# Research: Session Continuity and Context Management in `-p` Mode

**Issue**: #2
**Milestone**: M1: Foundation
**Status**: Complete
**Date**: 2026-03-02

---

## Executive Summary

Session continuity for headless `claude -p` orchestration is best achieved through a
**stateless chaining model with external checkpoint persistence** rather than through
long-lived session resumption or the `--resume` flag. The `--resume` flag works in `-p`
mode and restores conversation history, but it has meaningful limitations for
orchestration workloads: session IDs change on resume, `CLAUDE_CONFIG_DIR` isolation
breaks resume entirely, and the growing conversation history consumes increasing context
budget with each resumed turn. The recommended pattern for conductor is to **record
orchestration state in a durable external JSON checkpoint file**, then pass that state
explicitly in the system prompt of each new `claude -p` invocation rather than relying
on session history. Hooks (`Stop`, `SessionEnd`, `PreCompact`) can serialize state to
disk during a session; the `SessionStart` hook can reload it at the top of the next
invocation. Context window limits are managed by auto-compaction, which fires at roughly
75% utilization in `-p` mode — but auto-compaction can silently drop orchestration
context, making external state serialization critical.

---

## 1. Session Models Compared

### 1.1 Single Long `-p` Session

A single `claude -p` invocation runs until it produces a result or hits a limit. The
orchestrator could, in theory, run as one long `-p` session that manages the entire
lifecycle of many issues.

**What works**: Claude maintains full conversation history within the session. All tool
results, git operations, and intermediate reasoning are available.

**What breaks**:
- Context window exhaustion. A 200K-token context window fills quickly when running
  multiple agents sequentially in one session. Each tool call, agent output, and
  intermediate response adds tokens. A typical implementation agent run consumes
  30–80K input tokens and 8–25K output tokens (see `docs/research/08-usage-scheduling.md`).
  Running 3–5 issue cycles in one session will hit the limit before all issues are
  resolved.
- Auto-compaction silently loses orchestration state. When the context compacts, Claude
  summarizes and discards older conversation history. The summarized version may omit
  which issues are claimed, which PRs are open, and which branches exist. The orchestrator
  cannot rely on compacted history for workflow state.
- No isolation between worker runs. If a sub-agent invocation in the middle of the
  session corrupts state or causes confusion, there is no clean recovery boundary.

**Verdict**: Not recommended for multi-issue orchestration runs.

### 1.2 `--resume` Chaining

`--resume SESSION_ID` loads a previous session's transcript and continues the
conversation. This is the closest analogue to "picking up where you left off."

```bash
# First call — captures session_id from stream-json init message
SESSION_ID=$(claude -p "Orchestrate issue #5..." \
  --output-format stream-json | \
  jq -r 'select(.type=="system" and .subtype=="init") | .session_id' | head -1)

# Second call — resumes the same session
claude -p "Check PR status and merge if CI passes" \
  --resume "$SESSION_ID" \
  --output-format stream-json
```

**What is preserved on resume**: The entire message history from the previous call is
reloaded. Tool call results, Claude's reasoning, all prior messages — the context is
restored verbatim. The resumed session behaves as if Claude never stopped.

**What is lost on resume**:
- In-process state not persisted to the conversation (e.g., local variables, process
  environment).
- Hook-injected context that was not written into the transcript.
- Any session state stored in a temporary `CLAUDE_CONFIG_DIR` (see section 3 below).

**Critical limitations**:

1. **Session ID changes on resume** (Issue #12235, duplicate of #8069): When a session
   is resumed with `--resume`, a **new** `session_id` is assigned. The `session_id` in
   the `system/init` stream-json message after a resume call differs from the original.
   This breaks any hook-based tracking that keys off `session_id`. For conductor, this
   means the orchestrator must store the *original* session ID and pass the new one to
   subsequent resume calls explicitly — or avoid keying off session IDs entirely.

2. **Growing context cost**: Each resumed call replays the entire prior conversation
   history as context. A session that ran through 3 issue cycles already has 100K+
   tokens of history loaded before the new prompt is even processed. This accelerates
   context exhaustion and increases per-call token cost significantly.

3. **`CLAUDE_CONFIG_DIR` isolation breaks resume**: As documented in
   `docs/research/04-configuration.md`, conductor sets `CLAUDE_CONFIG_DIR` to a
   per-agent temp directory for strong session isolation. Sessions are stored under
   `$CLAUDE_CONFIG_DIR/projects/`. If the orchestrator uses a new `CLAUDE_CONFIG_DIR`
   for each invocation (e.g., via `tempfile.TemporaryDirectory()`), it **cannot**
   resume sessions from the prior invocation — the transcript is in a different
   directory and is not found. The `CLAUDE_CONFIG_DIR` must be preserved across calls
   for resume to work.

4. **`--continue` vs `--resume` in non-interactive mode**: `--continue` resumes the
   most recent session for the current working directory. In non-interactive `-p` mode,
   `--continue` can sometimes create a new session rather than continuing the most
   recent one. Prefer `--resume <SESSION_ID>` with an explicitly captured ID for
   reliable behavior in scripts.

**Verdict**: `--resume` chaining is viable for simple two-step workflows where the
context is small and `CLAUDE_CONFIG_DIR` is stable across calls. It is not recommended
as the primary continuity mechanism for the full conductor orchestration lifecycle
(issue claim → branch creation → agent dispatch → PR monitoring → merge) because of
context accumulation and session ID instability.

### 1.3 Stateless Multiple Calls with External Checkpoint

Each `claude -p` invocation is treated as independent. Orchestration state is stored
externally in a JSON checkpoint file. The current state is injected into the next
invocation's prompt.

```bash
# Orchestrator writes state after each step
STATE_FILE="~/.local/share/conductor/runs/run-$(date +%s).json"

# Step 1: Claim issue
claude -p "Claim issue #5 and create branch. Write final state to $STATE_FILE" \
  --output-format stream-json ...

# Step 2: Dispatch worker (state loaded from file, injected into prompt)
STATE=$(cat "$STATE_FILE")
claude -p "State: $STATE. Dispatch worker for branch 5-my-feature..." \
  --output-format stream-json ...
```

**Advantages**:
- No context accumulation between steps. Each call starts fresh.
- Full isolation: if one step fails, the checkpoint records exactly what succeeded.
- Compatible with `CLAUDE_CONFIG_DIR` isolation (no need to preserve config dir across
  calls).
- Supports resuming after crash: conductor reads the checkpoint file on startup and
  resumes from the last committed state.
- Clean boundary per orchestration step — easier to reason about and debug.

**Disadvantages**:
- State must be explicitly defined and serialized. The orchestrator cannot rely on Claude
  "remembering" context from previous calls.
- Prompt construction is more complex: context must be carefully summarized to fit within
  the available token budget.
- No conversational history continuity — Claude cannot reference prior reasoning.

**Verdict**: Recommended primary model for conductor. This is the pattern used by all
production multi-agent orchestration systems reviewed in the research for issue #1
(ccswarm, Claude PM, Overstory).

---

## 2. `--resume` Mechanics in Detail

### 2.1 Session Storage

Sessions are stored in `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR/projects/` if
`CLAUDE_CONFIG_DIR` is set). Each project directory is named after the hashed project
path. Within the project directory, each session is a `.jsonl` file — one JSON object
per line, representing events (messages, tool calls, tool results).

A global index is at `~/.claude/history.jsonl`, containing one entry per user prompt
across all sessions: session ID, project path, prompt text, timestamp.

### 2.2 Session ID Capture

The `session_id` is emitted in the first `system/init` message of a `--output-format
stream-json` session:

```bash
SESSION_ID=$(claude -p "Start orchestrating..." \
  --output-format stream-json \
  2>/dev/null | \
  jq -r 'select(.type=="system" and .subtype=="init") | .session_id' | \
  head -1)
```

For `--output-format json` (waits for full completion), the `session_id` is in the top-
level result object:

```bash
RESULT=$(claude -p "..." --output-format json)
SESSION_ID=$(echo "$RESULT" | jq -r '.session_id')
```

### 2.3 What `--resume` Restores

When `--resume SESSION_ID` is used, Claude Code reloads the full message history from
the session `.jsonl` file. This includes:

- All prior user messages (prompts)
- All assistant responses (including reasoning)
- All tool call inputs and outputs (Bash output, file reads, etc.)
- The conversation can continue exactly where it left off

What `--resume` does NOT restore:
- Background processes (any `Bash` processes that were running are long dead)
- Environment variables (the new call has a fresh process environment)
- File system state changed by Bash commands (files persist on disk, but if the prior
  session deleted or moved files, those changes are already on disk — the session only
  knows they happened because it's in the transcript)
- Any in-memory state the prior Claude session had that was not written to the transcript

### 2.4 `forkSession` Option

The Agent SDK (Python/TypeScript) supports a `fork_session=True` option when resuming.
This creates a **new** session ID branching from the resumed state, preserving the
original session history unchanged. From the CLI, the equivalent is
`--resume <SESSION_ID>` combined with `--fork-session`.

Forking is useful for: exploring different orchestration approaches from the same
checkpoint without contaminating the original session history.

---

## 3. Context Window Management in `-p` Mode

### 3.1 Auto-Compaction Behavior

Claude Code auto-compacts the context window at approximately **75% utilization**
(25% remaining context). This threshold was recently improved — earlier versions ran
until the window was nearly full (~95%), leaving insufficient room for the compaction
LLM call itself, causing failures. The current behavior proactively compacts while there
is still enough headroom to complete the compaction summary.

**Compaction mechanics**: When compaction triggers:
1. Claude analyzes the conversation history
2. Creates a summary of the key context (decisions, tool outputs, code state)
3. Replaces older messages with the summary
4. A `SessionStart` hook with matcher `compact` fires after compaction completes (see
   Section 4)

**Known compaction failures** (open as of March 2026):
- Issue #13929: auto-compact fails when conversation exceeds context limit, blocking
  manual compaction with the error `Input length and max_tokens exceed context limit`.
- Issue #19567: Claude Code hangs indefinitely during context compaction.
- Issue #18705: token limit hard-stop without warning or auto-compaction in some cases.

**Critical implication for conductor**: In `-p` mode, there is no user present to
observe or respond to compaction. If auto-compaction fails mid-session (Issue #13929 or
#19567), the session may produce an error or hang with no recovery path available to the
caller. The orchestrator must set `CONDUCTOR_TIMEOUT_SECONDS` and kill sessions that
exceed the timeout (see `docs/research/04-configuration.md`).

### 3.2 `PreCompact` Hook

The `PreCompact` hook fires before auto-compaction occurs. It cannot block compaction
(exit code 2 is ignored for this event), but it can be used for **pre-compaction state
serialization**:

```json
{
  "hooks": {
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/save-orchestration-state.sh"
          }
        ]
      }
    ]
  }
}
```

The hook receives:
```json
{
  "session_id": "abc123",
  "transcript_path": "~/.claude/projects/.../abc123.jsonl",
  "hook_event_name": "PreCompact",
  "trigger": "auto"
}
```

The script can parse the transcript at `transcript_path` to extract the current
orchestration state (claimed issues, open PRs, active branches) and write it to the
checkpoint file before compaction destroys the older context.

### 3.3 Disabling Auto-Compaction

Feature request #6689 proposes a `--no-auto-compact` flag. This has not shipped as of
March 2026. The workaround is to keep sessions short enough that they don't approach the
context limit — which the stateless chaining model naturally achieves.

---

## 4. Hook-Based State Serialization in `-p` Mode

### 4.1 Hooks That Fire in Headless Mode

All hooks fire in `-p` mode **except** `PermissionRequest`. The documentation explicitly
notes: *"`PermissionRequest` hooks do not fire in non-interactive mode (`-p`). Use
`PreToolUse` hooks for automated permission decisions."*

The full set of hooks confirmed to fire in `-p` mode:

| Hook | Fires in `-p` | Use case for conductor |
|---|---|---|
| `SessionStart` | Yes | Load checkpoint state; inject current orchestration context |
| `UserPromptSubmit` | Yes | Validate prompt structure |
| `PreToolUse` | Yes | Block dangerous tools; replace `PermissionRequest` |
| `PostToolUse` | Yes | Serialize state after key tool calls (e.g., after `gh pr create`) |
| `PostToolUseFailure` | Yes | Log failures; trigger fallback |
| `Stop` | Yes | Final state serialization before session ends |
| `SubagentStart` | Yes | Log agent dispatch |
| `SubagentStop` | Yes | Collect agent results |
| `PreCompact` | Yes | Pre-compaction state backup |
| `SessionEnd` | Yes | Final cleanup and archive |
| `WorktreeCreate` | Yes | Custom VCS worktree creation |
| `WorktreeRemove` | Yes | Custom VCS worktree cleanup |
| `PermissionRequest` | **No** | Use `PreToolUse` instead |
| `Notification` | No notification UI in `-p` | Hook fires but notification has no visible effect |

### 4.2 Stop Hook for State Serialization

The `Stop` hook fires when Claude finishes responding in a session. It can block Claude
from stopping (by returning `"decision": "block"` with a `"reason"`) or it can
serialize the current session state before exit.

**Stop hook exit code 2 behavior**: If a `Stop` hook exits with code 2, stderr is
injected back as a prompt and Claude continues working. This is the mechanism for
quality gates (e.g., "do not stop until tests pass"). The `stop_hook_active` field in
the hook input is `true` if Claude is already continuing as a result of a previous Stop
hook — check this to avoid infinite loops:

```bash
#!/bin/bash
# save-state-on-stop.sh
INPUT=$(cat)

# Prevent infinite loop
if [ "$(echo "$INPUT" | jq -r '.stop_hook_active')" = "true" ]; then
  exit 0
fi

SESSION_ID=$(echo "$INPUT" | jq -r '.session_id')
TRANSCRIPT_PATH=$(echo "$INPUT" | jq -r '.transcript_path')
LAST_MSG=$(echo "$INPUT" | jq -r '.last_assistant_message')

# Extract orchestration state from transcript and persist
python3 /path/to/extract-state.py \
  --transcript "$TRANSCRIPT_PATH" \
  --session-id "$SESSION_ID" \
  --output ~/.local/share/conductor/checkpoint.json

exit 0
```

### 4.3 SessionStart Hook for Context Injection

The `SessionStart` hook fires when a session begins (matcher `startup`) or resumes
(matcher `resume`). Stdout from this hook is injected as context for Claude.

For the stateless chaining model, a `SessionStart` hook on `startup` can inject the
current checkpoint state:

```bash
#!/bin/bash
# inject-checkpoint.sh
INPUT=$(cat)
SOURCE=$(echo "$INPUT" | jq -r '.source')

if [ "$SOURCE" = "startup" ]; then
  CHECKPOINT=~/.local/share/conductor/checkpoint.json
  if [ -f "$CHECKPOINT" ]; then
    echo "=== CONDUCTOR CHECKPOINT ==="
    cat "$CHECKPOINT"
    echo "=== END CHECKPOINT ==="
  fi
fi
exit 0
```

Alternatively, return the context via structured JSON output:

```bash
jq -n --arg ctx "$(cat ~/.local/share/conductor/checkpoint.json)" \
  '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}'
```

### 4.4 `CLAUDE_ENV_FILE` in `SessionStart`

`SessionStart` hooks have access to `CLAUDE_ENV_FILE` — a path to a file where
`export` statements can be written to persist environment variables for all subsequent
Bash commands in the session. This can be used to inject checkpoint state as env vars:

```bash
#!/bin/bash
CHECKPOINT=~/.local/share/conductor/checkpoint.json
if [ -f "$CHECKPOINT" ] && [ -n "$CLAUDE_ENV_FILE" ]; then
  CURRENT_RUN=$(jq -r '.run_id' "$CHECKPOINT")
  echo "export CONDUCTOR_RUN_ID=$CURRENT_RUN" >> "$CLAUDE_ENV_FILE"
  echo "export CONDUCTOR_CHECKPOINT_PATH=$CHECKPOINT" >> "$CLAUDE_ENV_FILE"
fi
exit 0
```

Note: `CLAUDE_ENV_FILE` is only available in `SessionStart` hooks. Other hook types do
not have access to this variable.

### 4.5 Re-inject Context After Auto-Compaction

After auto-compaction, a `SessionStart` hook with matcher `compact` fires. This is the
recommended injection point for restoring orchestration context that may have been
summarized away by compaction:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "compact",
        "hooks": [
          {
            "type": "command",
            "command": "cat ~/.local/share/conductor/checkpoint.json"
          }
        ]
      }
    ]
  }
}
```

The checkpoint file is written by the `PreCompact` hook before compaction runs, ensuring
the post-compaction injection has fresh state.

---

## 5. Checkpoint Format and Persistence

### 5.1 Recommended Checkpoint Schema

The checkpoint file captures orchestration state at each committed step. It is written
atomically (write to temp file, then rename to the final path) to avoid partial writes.

```json
{
  "schema_version": "1",
  "run_id": "run-2026-03-02T14:35:00Z",
  "repo": "myorg/myrepo",
  "default_branch": "main",
  "timestamp": "2026-03-02T14:35:22Z",
  "session_id": "abc123",
  "stage": "agents_dispatched",
  "claimed_issues": [
    {
      "number": 7,
      "branch": "7-inbound-outbound-messages",
      "claimed_at": "2026-03-02T14:32:00Z"
    },
    {
      "number": 12,
      "branch": "12-error-taxonomy",
      "claimed_at": "2026-03-02T14:32:05Z"
    }
  ],
  "active_worktrees": [
    ".claude/worktrees/7-inbound-outbound-messages",
    ".claude/worktrees/12-error-taxonomy"
  ],
  "open_prs": [],
  "completed_prs": [],
  "rate_limit_backoff_until": null,
  "last_error": null
}
```

### 5.2 Checkpoint Stages

The `stage` field tracks which step in the orchestration lifecycle the run reached:

| Stage | Description | Recovery action |
|---|---|---|
| `idle` | No work in progress | Start fresh |
| `issues_selected` | Issues chosen but not yet claimed on GitHub | Re-select and claim |
| `issues_claimed` | Issues labeled `in-progress` on GitHub | Verify labels, skip already-claimed |
| `branches_created` | Branches pushed to origin | Skip already-created branches |
| `agents_dispatched` | Workers spawned | Check PR status for each active branch |
| `prs_created` | PRs open, waiting for CI | Poll PR status |
| `prs_merging` | Merges in progress | Check merge status |
| `complete` | All issues resolved | Archive run, clean worktrees |
| `error` | Unrecoverable failure | Human review |

### 5.3 Persistence Location

Default: `~/.local/share/conductor/checkpoint.json` (configurable via
`CONDUCTOR_CHECKPOINT_PATH` env var, or derivable from `CONDUCTOR_LOG_DIR`).

For per-repo runs, use: `~/.local/share/conductor/{repo_slug}/checkpoint.json`.

The file should be:
- Written atomically (temp file + rename)
- Preserved across conductor restarts
- **Never committed to git** (add to `.gitignore`)
- Readable by hooks (world-readable permissions acceptable; it contains no secrets)

---

## 6. State Recovery After Agent Failure

### 6.1 What Survives a Crashed Agent

When a sub-agent `claude -p` subprocess crashes (non-zero exit, signal, timeout), the
following state survives:

| State | Survives? | Notes |
|---|---|---|
| Files committed to git | Yes | Git history is durable |
| Files written but not committed | Yes | Files on disk in the worktree |
| GitHub PR (if created) | Yes | PRs persist on GitHub |
| GitHub issue labels | Yes | Labels on GitHub are durable |
| Claude session transcript | Yes | JSONL file in `~/.claude/projects/` |
| Bash processes spawned by agent | No | Processes are killed when subprocess dies |
| In-flight git operations | No | May leave partial state (see below) |

### 6.2 Partial State Hazards

The most dangerous failure modes for conductor:

1. **Issue claimed, no branch created**: The GitHub issue is labeled `in-progress` but no
   branch was pushed. Recovery: detect this mismatch in the checkpoint stage and re-create
   the branch.

2. **Branch created, no PR**: Branch was pushed to origin but `gh pr create` did not
   complete. Recovery: run `gh pr list --head <branch>` to check; if no PR, re-run
   `gh pr create`.

3. **Worktree created, no cleanup**: The worktree persists on disk. Recovery: `git worktree
   list` to audit; `git worktree remove --force` for stale entries.

4. **PR merged, label not removed**: The `in-progress` label was not removed after merge.
   Recovery: on startup, conductor should check all `in-progress` issues that have merged
   PRs and remove the label.

### 6.3 Recovery Protocol

On conductor startup (before dispatching any new work):

```python
def recover_from_checkpoint(checkpoint: dict) -> None:
    """Reconcile checkpoint state with live GitHub and git state."""
    for issue in checkpoint["claimed_issues"]:
        pr = gh_pr_for_branch(issue["branch"])
        if pr and pr["state"] == "merged":
            # Work completed — remove label, clean worktree
            gh_remove_label(issue["number"], "in-progress")
            git_worktree_remove(issue["branch"])
        elif pr and pr["state"] == "open":
            # PR exists — continue monitoring
            pass
        elif branch_exists_on_origin(issue["branch"]):
            # Branch pushed but no PR — re-run gh pr create
            create_pr(issue)
        else:
            # Nothing pushed — re-dispatch agent
            re_dispatch_agent(issue)
```

This is consistent with the orchestrator startup protocol in `~/.claude/CLAUDE.md`.

### 6.4 Hook `stop_hook_active` Guard

When using a `Stop` hook for quality gates (e.g., "ensure tests pass before stopping"),
the hook must check `stop_hook_active` to prevent infinite loops:

```bash
INPUT=$(cat)
if [ "$(echo "$INPUT" | jq -r '.stop_hook_active')" = "true" ]; then
  # Already continuing because of a previous stop hook — don't block again
  exit 0
fi
```

Without this guard, a failing test will cause Claude to loop indefinitely in headless
mode.

---

## 7. The `everything-claude-code` Session State Pattern

The `affaan-m/everything-claude-code` repository demonstrates a reference implementation
of hook-based session memory persistence. Its two-hook model maps directly to conductor's
needs:

**`session-start.sh`** (SessionStart hook, `startup` matcher):
- Checks for recent session files in a `sessions/` directory (last 7 days)
- Loads learned patterns/context from previous sessions
- Injects this context into Claude's startup context via stdout

**`session-end.sh`** (SessionEnd hook):
- Creates/updates a session file under `sessions/YYYY-MM-DD-HH-MM.md`
- Records session outcomes, decisions made, and state snapshot

For conductor, this pattern translates to:
- `SessionStart` (startup): loads `checkpoint.json`, injects current orchestration state
- `SessionStart` (compact): re-injects the same checkpoint after context compaction
- `Stop` / `SessionEnd`: writes updated checkpoint state to disk

The key difference from the `everything-claude-code` pattern: conductor's checkpoint
format is structured JSON (not human-readable markdown), enabling programmatic parsing
by the Python orchestrator. The hooks write JSON; the conductor Python code reads it.

---

## 8. Interaction with `CLAUDE_CONFIG_DIR` Isolation

As documented in `docs/research/04-configuration.md`, Section 5.4:

> Setting `CLAUDE_CONFIG_DIR` to a per-agent temporary directory provides strong
> session-level isolation... The trade-off is that no settings (allowed tools, MCP
> servers) from the user's `~/.claude/` are inherited.

For session continuity, this isolation creates a critical constraint: **hooks defined in
`~/.claude/settings.json` do NOT run for sub-agents launched with isolated
`CLAUDE_CONFIG_DIR` values**.

The conductor must explicitly provide hook configuration for sub-agents via:
1. A project-level `.claude/settings.json` in the worktree (committed to the repo)
2. A `--settings <path>` flag pointing to a conductor-managed settings file

For the orchestrator session itself (which does NOT use isolated `CLAUDE_CONFIG_DIR`),
user-level hooks in `~/.claude/settings.json` fire normally.

The recommended approach:
- Orchestrator session: hooks in `~/.claude/settings.json` for state serialization
- Sub-agent sessions: hooks in the worktree's `.claude/settings.json`, scoped to the
  sub-agent's task (run tests, format code, etc.)

---

## 9. Recommended Session Model for breadmin-conductor

Based on the full research:

### 9.1 Orchestrator Session Model

**Model**: Stateless multiple calls with external checkpoint persistence.

Each orchestration step (startup, claim, dispatch, monitor, merge) is a separate
`claude -p` invocation. Between calls, state is stored in `checkpoint.json`. The
system prompt for each call includes the relevant checkpoint state.

```
Orchestrator Run N
  ├── Step 1: Startup check (claude -p, reads checkpoint, reconciles GitHub state)
  ├── Step 2: Issue selection and claiming (claude -p, writes checkpoint)
  ├── Step 3: Branch creation (claude -p, writes checkpoint)
  ├── Step 4: Worker dispatch (claude -p spawns sub-agents, writes checkpoint)
  ├── Step 5: PR monitoring loop (claude -p polls at interval, reads checkpoint)
  └── Step 6: Merge and cleanup (claude -p, writes checkpoint)
```

**Why not `--resume` chaining**: The stateless model avoids context accumulation,
works with any `CLAUDE_CONFIG_DIR` configuration, and provides clean recovery
boundaries after crashes.

### 9.2 Sub-Agent Session Model

**Model**: Single-shot, no resumption.

Each sub-agent is a single `claude -p` invocation that implements one issue and stops
after creating a PR. No resumption is needed — if the sub-agent fails, the orchestrator
re-dispatches it from the checkpoint.

```bash
claude -p "Implement issue #7 on branch 7-my-feature. [Full task context...]" \
  --allowedTools "Bash,Read,Edit,Glob,Grep,Write" \
  --dangerously-skip-permissions \
  --output-format stream-json \
  --max-turns 100 \
  --timeout 1800 \
  > ~/.local/share/conductor/logs/agent-7.jsonl 2>&1
```

Sub-agents do not use hooks for state serialization. Their state is fully visible from:
- Git history (what was committed)
- GitHub (whether a PR was created)
- The worktree (what files exist on disk)

### 9.3 Hook Configuration for Conductor

**Orchestrator-level hooks** (in `~/.claude/settings.json` or the conductor's
`.claude/settings.json`):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "~/.conductor/hooks/inject-checkpoint.sh"
          }
        ]
      },
      {
        "matcher": "compact",
        "hooks": [
          {
            "type": "command",
            "command": "~/.conductor/hooks/inject-checkpoint.sh"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.conductor/hooks/save-checkpoint.sh",
            "async": true
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.conductor/hooks/save-checkpoint.sh"
          }
        ]
      }
    ]
  }
}
```

---

## 10. Follow-Up Research Recommendations

### 10.1 Empirical Verification of `--resume` + `CLAUDE_CONFIG_DIR` Interaction

The documented behavior (persistent `CLAUDE_CONFIG_DIR` required for resume to work) is
inferred from the session storage model, not directly verified. A minimal test case is
needed:

1. Run `claude -p "..." --output-format json > /tmp/result.json` with a stable
   `CLAUDE_CONFIG_DIR=/tmp/conductor-config`
2. Extract `session_id`
3. Run `claude -p "..." --resume "$SESSION_ID"` with the same `CLAUDE_CONFIG_DIR`
4. Confirm the session history loads correctly
5. Repeat with a fresh `CLAUDE_CONFIG_DIR` to confirm failure

**Suggested issue**: `Research: Empirical test of --resume with stable CLAUDE_CONFIG_DIR`

### 10.2 Session ID Stability in Resume (Issue #12235 / #8069 Status)

The session ID change on resume (Issues #12235 and #8069) was reported in Claude Code
v2.0.50. The current status (fixed or still occurring in v2.1.x) is not confirmed. If
fixed, `--resume` chaining becomes more reliable for hook-based tracking.

**Suggested issue**: `Research: Verify session ID behavior on --resume in Claude Code v2.1.x`

### 10.3 Checkpoint Atomicity Under Concurrent Writes

The orchestrator and its hooks (which run in separate processes) may both write to the
checkpoint file. Issue #28999 documents a `.claude.json` corruption from concurrent
access in Claude Code itself — the same risk applies to the checkpoint file.

The recommended mitigation (temp file + rename) is POSIX-atomic on Linux and macOS, but
verification of the rename pattern under concurrent load is needed.

**Suggested issue**: `Research: Checkpoint file concurrency safety — temp file + rename under load`

### 10.4 `PreCompact` Hook Timing Reliability

The `PreCompact` hook must fire **before** compaction discards context. If compaction
can begin before the hook completes (e.g., if the hook times out), the state serialized
by the hook may be incomplete. The default hook timeout is 600 seconds, far longer than
typical compaction cycles, but the interaction under failure conditions needs
verification.

**Suggested issue**: `Research: PreCompact hook timing reliability under context pressure`

### 10.5 Hook Configuration Delivery to Sub-Agents

The section on `CLAUDE_CONFIG_DIR` isolation notes that user-level hooks do not fire for
sub-agents using isolated config directories. The mechanism for delivering hook
configuration to sub-agents (`--settings`, repo-level `.claude/settings.json`, or the
worktree's inherited settings) needs empirical testing to confirm which approach works
reliably with per-agent `CLAUDE_CONFIG_DIR` isolation.

This question overlaps with Issues #10 (--settings flag isolation) and #3 (error
handling). It does not warrant a new issue.

---

## 11. Sources

- [Run Claude Code programmatically — Claude Code Docs](https://code.claude.com/docs/en/headless) — `-p` mode usage, `--continue`, `--resume`, session ID capture from stream-json
- [Session Management — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/sessions) — SDK resume, forkSession, session ID in init message
- [Hooks reference — Claude Code Docs](https://code.claude.com/docs/en/hooks) — Full hook event list, input schemas, exit code behavior, `SessionStart`/`Stop`/`PreCompact`/`SessionEnd` details, `PermissionRequest` headless limitation
- [Automate workflows with hooks — Claude Code Docs](https://code.claude.com/docs/en/hooks-guide) — Hook use cases, `SessionStart` compact injection example, `PermissionRequest` headless limitation confirmed
- [Rewind file changes with checkpointing — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/file-checkpointing) — SDK file checkpointing, checkpoint UUIDs from user messages, `rewindFiles()` pattern
- [Checkpointing — Claude Code Docs](https://code.claude.com/docs/en/checkpointing) — `/rewind` interactive command, checkpoint behavior, limitations (Bash commands not tracked)
- [Compaction — Claude API Docs](https://platform.claude.com/docs/en/build-with-claude/compaction) — How auto-compaction works, ~95% trigger threshold, context preservation strategy
- [Claude Code Compaction — ClaudeLog](https://claudelog.com/faqs/what-is-claude-code-auto-compact/) — 75% threshold for proactive compaction, compaction mechanics
- [Session Management — Claude Code Docs (how-claude-code-works)](https://code.claude.com/docs/en/how-claude-code-works) — How sessions are stored, resume vs fork
- [GitHub Issue #12235: Session ID changes when resuming via --resume](https://github.com/anthropics/claude-code/issues/12235) — Session ID instability on resume (duplicate of #8069), v2.0.50
- [GitHub Issue #13929: Auto-compact fails when conversation exceeds context limit](https://github.com/anthropics/claude-code/issues/13929) — Compaction failure: manual compaction also blocked
- [GitHub Issue #19567: Claude Code hangs indefinitely during context compaction](https://github.com/anthropics/claude-code/issues/19567) — Compaction hang with no recovery
- [GitHub Issue #18705: Token limit hard-stop without warning or auto-compaction](https://github.com/anthropics/claude-code/issues/18705) — Hard stop at limit, compaction not triggered
- [GitHub Issue #6689: Add --no-auto-compact command switch](https://github.com/anthropics/claude-code/issues/6689) — Feature request (not shipped as of March 2026)
- [GitHub Issue #28999: Expose /usage subscription quota in statusLine JSON](https://github.com/anthropics/claude-code/issues/28999) — `.claude.json` concurrent write corruption documented
- [GitHub Issue #7535: Support for In-Process Hooks in Headless CLI Mode (Closed — Not Planned)](https://github.com/anthropics/claude-code/issues/7535) — Only shell command hooks in `-p` mode; no in-process TypeScript hooks
- [everything-claude-code: session-start.sh](https://github.com/affaan-m/everything-claude-code/blob/main/hooks/memory-persistence/session-start.sh) — Reference implementation: load prior session context
- [everything-claude-code: session-end.sh](https://github.com/affaan-m/everything-claude-code/blob/main/hooks/memory-persistence/session-end.sh) — Reference implementation: persist session state on end
- [Claude Code Session Hooks: Auto-Load Context Every Time — claudefa.st](https://claudefa.st/blog/tools/hooks/session-lifecycle-hooks) — SessionStart startup/compact matcher patterns
- [Teaching Claude To Remember: Part 3 — Sessions And Resumable Workflow — Medium](https://medium.com/@porter.nicholas/teaching-claude-to-remember-part-3-sessions-and-resumable-workflow-1c356d9e442f) — Multi-step headless session chaining with session IDs
- [Claude Code: Resume Sessions Without Context Loss — rigel-computer.com](https://medium.com/rigel-computer-com/you-close-claude-code-the-context-is-gone-or-is-it-3ebc5c1c379d) — What `--resume` restores; session persistence location `~/.claude/projects/`
- [Continuous Claude v3 — GitHub (parcadei)](https://github.com/parcadei/Continuous-Claude-v3) — YAML handoff pattern, PreCompact hook for state extraction, four-phase lifecycle
- [Context Recovery Hook — Coding Nexus (Medium)](https://medium.com/coding-nexus/context-recovery-hook-for-claude-code-never-lose-work-to-compaction-7ee56261ee8f) — PreCompact backup pattern; SessionStart re-injection pattern
- [Claude Code's hidden conversation history — kentgigger.com](https://kentgigger.com/posts/claude-code-conversation-history) — `~/.claude/projects/` structure, `history.jsonl` global index
- [What is the --continue Flag in Claude Code — ClaudeLog](https://claudelog.com/faqs/what-is-continue-flag-in-claude-code/) — `--continue` vs `--resume`; prefer `--resume <SESSION_ID>` in scripts
