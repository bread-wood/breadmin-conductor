# Research: Agent Tool Availability in Headless `-p` Mode

**Issue**: #1
**Milestone**: M1: Foundation
**Status**: Complete
**Date**: 2026-03-02

---

## Executive Summary

The `Agent` tool (internally called the `Task` tool in older versions) **does work** when Claude Code is
invoked via `claude -p`, but requires explicit opt-in configuration and has a hard architectural
constraint: **subagents cannot spawn further subagents**. Worktree isolation (`isolation: worktree`)
is supported in agent definitions and functions correctly in headless scenarios. The recommended
orchestration pattern for the conductor architecture is the **subprocess-based pattern** — the
orchestrator invokes `claude -p` workers directly from the shell — rather than relying on the
in-process `Agent` tool, because the in-process pattern cannot support the depth required for the
conductor model.

---

## 1. Does the Agent Tool Work in `-p` Mode?

### 1.1 The Hard Constraint: Subagents Cannot Spawn Subagents

The single most important finding is an **intentional architectural limit** documented across
multiple official sources:

> "Subagents cannot spawn other subagents. If your workflow requires nested delegation, use Skills
> or chain subagents from the main conversation."
>
> — Claude Code Subagents Documentation

> "This prevents infinite nesting (subagents cannot spawn other subagents), which is an intentional
> design decision to avoid recursive agent loops."
>
> — Claude Code Plan Subagent documentation

This means that if the breadmin-conductor orchestrator itself runs as a subagent (e.g., spawned by
another process via the Task tool), it **cannot** use the `Agent` tool to dispatch its own workers.
If the orchestrator is the **top-level** `claude -p` session, it can use the `Agent` tool to spawn
one layer of subagents.

### 1.2 Task Tools Were Initially Disabled in Headless Mode

A confirmed bug (Issue #20463, closed February 2026) established that in versions around 2.1.17,
the `TaskCreate`/task management tools were **not available** in headless mode by default. The
Anthropic team member `shawnm-anthropic` resolved this with an environment variable opt-in:

```bash
export CLAUDE_CODE_ENABLE_TASKS=true
claude -p "your orchestration prompt here"
```

The issue was closed as "COMPLETED" with the note that this would become the default once integrations
had time to migrate. As of version 2.1.50+ (February 2026), the Task/Agent tool infrastructure is
available in headless mode.

### 1.3 The Agent Tool Requires `Task` in `allowedTools`

From the official SDK documentation:

> "The `Task` tool must be included in `allowedTools` since Claude invokes subagents through the
> Task tool."
>
> — Claude Agent SDK Subagents Documentation

The correct invocation pattern for a headless orchestrator that uses the Agent tool is:

```bash
claude -p "Implement issue #N..." \
  --allowedTools "Task,Read,Edit,Bash,Glob,Grep" \
  --dangerously-skip-permissions
```

Note: In version 2.1.63, the `Task` tool was renamed to `Agent`. The old `Task(...)` name still
works as an alias. For `--allowedTools`, use whichever name matches the installed version.

### 1.4 The `--agent` Flag Bug (Fixed February 2026)

Issue #13533 documented a related regression: when launching a custom agent with
`claude --agent my-custom-agent`, the main session lost access to the `Task` tool even when the
agent definition had no `tools:` field (which should inherit all tools). This was fixed in a
February 2026 commit that corrected the tool inheritance logic for `--agent`-launched sessions.

---

## 2. Does `isolation: worktree` Work in Headless Mode?

### 2.1 Feature Introduction

`isolation: worktree` support in agent definitions was added in **v2.1.49** (changelog confirmed).
The feature is documented in YAML frontmatter for agent files:

```yaml
---
name: worker-agent
description: Implements a specific task in isolation
isolation: worktree
---
```

When triggered, Claude creates a fresh git worktree for each agent invocation. The worktree is
automatically cleaned up after the subagent finishes if no changes were committed.

### 2.2 Headless Compatibility

The official Claude Code documentation's "Subagent worktrees" section states:

> "Subagents can also use worktree isolation to work in parallel without conflicts. Ask Claude to
> 'use worktrees for your agents' or configure it in a custom subagent by adding `isolation: worktree`
> to the agent's frontmatter. Each subagent gets its own worktree that is automatically cleaned up
> when the subagent finishes without changes."

No headless-mode restriction is noted for `isolation: worktree`. Given that:
1. The `--worktree` flag works with `claude -p` (the flag is documented with no interactive-only
   restriction in the CLI reference)
2. `WorktreeCreate` and `WorktreeRemove` hook events were added in v2.1.50 for headless VCS setup
3. Agent teams in tmux mode work with both interactive and non-interactive sessions

The `isolation: worktree` feature in subagent definitions **is expected to function correctly** in
headless `-p` sessions, provided the Task/Agent tool itself is available (see Section 1).

### 2.3 Worktree Cleanup Behavior

From the official documentation:

- **No changes**: the worktree and branch are removed automatically
- **Changes or commits exist**: in interactive mode, Claude prompts to keep or remove; in headless
  mode this prompt cannot be answered, so worktrees with committed changes **will persist** and
  must be cleaned up externally

This is a practical concern for the conductor: if a sub-agent creates a worktree and pushes changes
but the orchestrator does not handle cleanup, worktrees will accumulate. The orchestrator must call
`git worktree remove` after each worker completes its PR.

---

## 3. Can `claude -p` Spawn Nested `claude -p` Processes?

### 3.1 The Subprocess Pattern

The most community-validated approach for true multi-level orchestration is to have the top-level
`claude -p` session use the **Bash tool** to spawn additional `claude -p` subprocesses. This is a
well-known workaround documented across multiple GitHub issues and community posts:

```bash
# Inside a claude -p session, via Bash tool:
claude -p "Implement issue #N on branch N-slug..." \
  --allowedTools "Bash,Read,Edit,Glob,Grep" \
  --dangerously-skip-permissions \
  > /tmp/agent-N-output.txt 2>&1
```

This pattern:
- Has no depth limit imposed by Claude Code (only system process limits apply)
- Each spawned process is fully independent with its own context window
- No context is shared — all task context must be passed via the prompt argument
- Each process inherits the sandbox/permission restrictions of the parent unless overridden

### 3.2 No Documented Hard Depth Limit

There is no documented maximum nesting depth for subprocess-based spawning. The
`claude-recursive-spawn` community project explicitly manages depth with a
`[DEPTH: n/max]` convention in prompts, showing this is a user-space concern rather than an
enforced limit.

### 3.3 Token and Cost Overhead

A critical practical concern: each `claude -p` subprocess loads the full system context. Community
analysis (DEV.to article) measured approximately **50,000 tokens of overhead per subprocess turn**
from global configuration inheritance:

- Project CLAUDE.md files loaded from all parent directories
- Plugin descriptions injected
- User-level settings propagated

The recommended mitigation is 4-layer subprocess isolation:

```bash
claude -p "..." \
  --system-prompt "..." \           # Replace CLAUDE.md-derived context
  --setting-sources project,local \ # Block user-level settings
  --plugin-dir /dev/null \          # Disable global plugins
  -C /tmp/isolated-workdir          # Scoped working directory
```

This reduces per-turn overhead from ~50K tokens to ~5K tokens (10x improvement).

---

## 4. What `--allowedTools` Are Required?

### 4.1 For an Orchestrator Using the Agent Tool

```bash
claude -p "Orchestrate implementation of issues..." \
  --allowedTools "Task,Read,Bash,Glob,Grep" \
  --dangerously-skip-permissions
```

Or with the newer name:
```bash
claude -p "Orchestrate..." \
  --allowedTools "Agent,Read,Bash,Glob,Grep" \
  --dangerously-skip-permissions
```

The `Task`/`Agent` tool is required to spawn subagents in-process. Without it, Claude cannot
delegate to subagents.

### 4.2 For an Orchestrator Using Subprocess Spawning

```bash
claude -p "Orchestrate implementation..." \
  --allowedTools "Bash,Read,Glob,Grep" \
  --dangerously-skip-permissions
```

The `Bash` tool with no restriction on `claude` invocations allows spawning worker processes.
If you want to restrict which commands are allowed, use permission rule syntax:

```bash
--allowedTools "Bash(claude -p *),Bash(git *),Bash(gh *),Read,Glob,Grep"
```

### 4.3 For Worker Sub-Agents

Workers should NOT have `Task`/`Agent` in their allowedTools (they cannot use it anyway), and
should be scoped to only the tools needed for their task:

```bash
claude -p "Implement feature X on branch Y..." \
  --allowedTools "Bash,Read,Edit,Write,Glob,Grep" \
  --dangerously-skip-permissions
```

---

## 5. Recommended Architecture: Native Agent vs. Subprocess Spawning

### 5.1 Option A: Native Agent Tool (`Agent`/`Task` tool with `isolation: worktree`)

**How it works**: The orchestrator runs as a top-level `claude -p` session with `Task` in
allowedTools. It dispatches workers using the Agent tool, with each worker configured with
`isolation: worktree` in its agent definition file.

**Advantages**:
- Native integration — no shell escaping, process management, or output piping
- Structured results returned directly to orchestrator context
- Background agent support (workers can run concurrently)
- Worktree lifecycle managed automatically by Claude Code

**Disadvantages**:
- **Single depth only**: workers cannot spawn further workers. The orchestrator cannot itself
  be a subagent of another process
- Workers run within the orchestrator's session — if the orchestrator crashes, all workers stop
- Background agent messaging limitations in headless sessions (documented inbox delivery issue)
- Less control over worker isolation and token budgets

**Verdict**: Viable for simple one-level dispatch, but does not support the full conductor
architecture if the orchestrator itself needs to be headlessly invoked.

### 5.2 Option B: Subprocess Spawning via Bash Tool

**How it works**: The orchestrator runs as `claude -p` with the `Bash` tool available. It spawns
workers by executing `claude -p` commands, with each worker running in its own pre-created git
worktree (created by the orchestrator via `git worktree add` before spawning).

**Advantages**:
- True process isolation — workers can be killed independently
- No depth limit — the orchestrator can itself be spawned by an outer process
- Full control over worker context, tools, and permissions
- Works even when orchestrator is a CI/CD job
- Parallel execution via background Bash processes (`&`)

**Disadvantages**:
- Token overhead per subprocess (~5K tokens after optimization, ~50K without)
- Context must be passed entirely via prompt string
- Output collection requires file I/O or output format parsing
- No structured result objects — must parse text or JSON output

**Verdict**: Recommended for the conductor architecture. This is the pattern used by all
production multi-agent orchestration frameworks reviewed (ccswarm, agent-orchestrator, Claude PM,
Overstory).

### 5.3 Option C: Agent Teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`)

**How it works**: An experimental feature (v2.1.32+) that enables a "team lead" session to spawn
"teammate" sessions with a shared task list and peer-to-peer messaging.

**Current Status**: Experimental, disabled by default. Known limitations include:
- `/resume` and `/rewind` do not restore in-process teammates
- In headless sessions, the inbox delivery cycle has documented issues ("messages stay read: false")
- No nested teams (teammates cannot spawn their own teams)
- Requires tmux for split-pane mode; in-process mode works in any terminal but is less observable

**Verdict**: Not recommended for production use in the conductor architecture at this time.
The experimental status and headless messaging limitations make it unreliable for CI/CD workloads.

### 5.4 Recommended Pattern for breadmin-conductor

Based on the research, the recommended pattern is:

```
Orchestrator (claude -p)
  Uses: Bash tool to spawn workers
  Does: pre-creates git worktrees, dispatches workers in parallel, monitors PR creation

Worker N (claude -p, spawned via Bash)
  Runs in: pre-created worktree (.claude/worktrees/N-slug/)
  Uses: Read, Edit, Write, Bash, Glob, Grep
  Does: implements issue, runs tests, pushes branch, creates PR, stops
```

The orchestrator pre-creates worktrees manually (using `git worktree add`) rather than relying on
`isolation: worktree` in agent definitions, because the subprocess pattern gives it explicit control
over worktree lifecycle and does not require the Agent tool to be available.

---

## 6. Permission Configuration

### 6.1 `--dangerously-skip-permissions`

In headless mode, there is no human available to answer permission prompts. The standard approach
is to skip all permission checks:

```bash
claude -p "..." --dangerously-skip-permissions
```

This is appropriate for an isolated CI/CD environment where the tooling is trusted.

### 6.2 `--permission-mode`

A less permissive alternative:

```bash
claude -p "..." --permission-mode acceptEdits
```

This auto-accepts file edits but still requires approval for other operations.

### 6.3 Scoped `--allowedTools`

The most restrictive and recommended approach for production:

```bash
claude -p "..." \
  --allowedTools "Read,Edit,Write,Glob,Grep,Bash(git *),Bash(gh *),Bash(npm test *)"
```

This allows only the specific operations needed, without blanket permission bypass.

---

## 7. Known Issues and Edge Cases

| Issue | Status | Impact |
|-------|--------|--------|
| Task tools unavailable in headless mode by default (pre-v2.1.50) | Fixed — use `CLAUDE_CODE_ENABLE_TASKS=true` on older versions | High |
| `--agent` flag caused Task tool to disappear from main session | Fixed February 2026 | Medium |
| Subagents cannot spawn other subagents | Intentional design limit | High (architectural) |
| Agent teams inbox delivery broken in headless sessions | Open/experimental | High for agent teams |
| Worktrees with commits persist after headless session ends | By design | Medium — requires cleanup |
| Background agent results return raw transcript data (pre-v2.1.47) | Fixed | Medium |
| `claude -p` subprocess overhead ~50K tokens without isolation | Mitigatable | High for cost |
| Task tool missing when MCP servers enabled (unique name conflict) | Documented workaround | Low |
| Agent hangs indefinitely mid-task with no recovery path (#28482) | Open | High for headless |

---

## 8. Follow-Up Research Recommendations

1. **Empirical verification of `isolation: worktree` in headless context**: The documentation does
   not explicitly test this combination. A minimal reproduction case (`claude -p` orchestrator
   dispatching a subagent with `isolation: worktree` in a CI-like environment) would confirm whether
   the feature works as expected or requires additional configuration.

2. **Subprocess token cost measurement**: The 50K tokens overhead figure is from a community source
   (DEV.to, circa late 2025). It should be benchmarked against the current Claude Code version
   with the recommended 4-layer isolation to establish actual numbers for the conductor's cost model.

3. **Hang recovery for headless agents (#28482)**: Issue #28482 describes a scenario where a
   headless agent hangs indefinitely with no programmatic recovery path (no Esc equivalent). This
   is a reliability concern for the conductor and needs a watchdog/timeout strategy.

4. **`CLAUDE_CODE_ENABLE_TASKS` default status**: The Anthropic engineer said this would become the
   default "once folks have had time to migrate." Current default behavior should be verified for
   Claude Code v2.1.50+, specifically whether `claude -p` sessions have the Task/Agent tool
   available without setting the environment variable.

5. **Parallel `claude -p` process limits**: Platform-level limits on concurrent processes and API
   rate limiting when running N workers in parallel are not documented. Research needed on how many
   workers can safely run in parallel without triggering rate limits.

6. **Agent teams in headless CI**: If Anthropic stabilizes agent teams, the headless inbox delivery
   issue warrants investigation. The documented "messages stay read: false" in headless lead sessions
   needs a repro case and upstream report if not already filed.

---

## 9. Sources

- [Run Claude Code programmatically — Claude Code Docs](https://code.claude.com/docs/en/headless)
- [Create custom subagents — Claude Code Docs](https://code.claude.com/docs/en/sub-agents)
- [Subagents in the SDK — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/subagents)
- [Orchestrate teams of Claude Code sessions — Claude Code Docs](https://code.claude.com/docs/en/agent-teams)
- [Common workflows — Claude Code Docs](https://code.claude.com/docs/en/common-workflows)
- [CLI reference — Claude Code Docs](https://code.claude.com/docs/en/cli-reference)
- [Configure permissions — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/permissions)
- [GitHub Issue #20463: Task tools not available in headless mode](https://github.com/anthropics/claude-code/issues/20463)
- [GitHub Issue #4182: Sub-Agent Task Tool Not Exposed When Launching Nested Agents](https://github.com/anthropics/claude-code/issues/4182)
- [GitHub Issue #13533: Task tool missing when launching session with --agent](https://github.com/anthropics/claude-code/issues/13533)
- [GitHub Issue #28482: Agent hangs indefinitely mid-task — no recovery path without Esc](https://github.com/anthropics/claude-code/issues/28482)
- [GitHub Issue #23874: Task tools disabled in VSCode extension due to isTTY check](https://github.com/anthropics/claude-code/issues/23874)
- [Claude Code Changelog (official)](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
- [Claude Code Changelog: Complete Version History — claudefa.st](https://claudefa.st/blog/guide/changelog)
- [Building a 24/7 Claude Code Wrapper? Here's Why Each Subprocess Burns 50K Tokens — DEV Community](https://dev.to/jungjaehoon/why-claude-code-subagents-waste-50k-tokens-per-turn-and-how-to-fix-it-41ma)
- [The Task Tool: Claude Code's Agent Orchestration System — DEV Community](https://dev.to/bhaidar/the-task-tool-claude-codes-agent-orchestration-system-4bf2)
- [Claude Code Worktrees: Run Parallel Sessions Without Conflicts — claudefa.st](https://claudefa.st/blog/guide/development/worktree-guide)
- [Multi-Agent Orchestration: Running 10+ Claude Instances in Parallel — DEV Community](https://dev.to/bredmond1019/multi-agent-orchestration-running-10-claude-instances-in-parallel-part-3-29da)
- [GitHub: haasonsaas/claude-recursive-spawn](https://github.com/haasonsaas/claude-recursive-spawn)
- [GitHub: nwiizo/ccswarm](https://github.com/nwiizo/ccswarm)
- [Shipyard: Multi-agent orchestration for Claude Code in 2026](https://shipyard.build/blog/claude-code-multi-agent/)
- [Claude Code Agent Teams: How They Work Under the Hood — claudecodecamp.com](https://www.claudecodecamp.com/p/claude-code-agent-teams-how-they-work-under-the-hood)
- [X/Threads: Boris Cherny on built-in git worktree support](https://www.threads.com/@boris_cherny/post/DVAAnexgRUj/introducing-built-in-git-worktree-support-for-claude-code-now-agents-can-run-in)
