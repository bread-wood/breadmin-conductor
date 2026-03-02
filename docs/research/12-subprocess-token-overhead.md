# Research: Subprocess Token Overhead and Cost Optimization for Headless Workers

**Issue:** #12
**Milestone:** M1: Foundation
**Status:** Complete
**Date:** 2026-03-02

---

## Executive Summary

Each `claude -p` subprocess invocation loads a substantial amount of context before doing
any productive work. Under default conditions this overhead reaches approximately **50,000
tokens per turn** — caused by layered configuration inheritance: global CLAUDE.md files,
user settings, plugin and skill descriptions, and the full MCP tool catalog. A 4-layer
isolation strategy reduces per-turn overhead to approximately **5,000 tokens** (10x
improvement). For API-key-authenticated sessions this directly translates to a 10x cost
reduction per agent turn. For Pro/Max subscription sessions it translates to a 10x
reduction in usage-quota draw, which is the binding constraint.

---

## 1. Baseline Token Overhead per `claude -p` Invocation

### 1.1 The 50K Token Problem

Under default conditions, a `claude -p` subprocess inherits the full global configuration
of the user's environment:

| Layer | Token Estimate | Source |
|---|---|---|
| Core Claude Code system prompt (base) | ~5,000–8,000 | Piebald-AI/claude-code-system-prompts; conditionally assembled 110+ prompt strings |
| CLAUDE.md files loaded from CWD upward | ~4,000–15,000 | Each 100-line CLAUDE.md ≈ 4,000 tokens; multiple ancestors compound |
| User-level `~/.claude/settings.json` | ~500–2,000 | Plugin and server lists re-read each turn |
| Plugin skill descriptions | ~5,000–20,000 | Each plugin skill file adds hundreds to thousands of tokens |
| MCP tool catalogs (active servers) | ~5,000–20,000 | Chrome DevTools alone ≈ 20K; each active server contributes |
| Built-in tool definitions (18 tools) | ~8,000–12,000 | Task/Agent tool: 1,331 tks; Teammate tool: 1,642 tks |

**Total observed overhead**: ~50,000 tokens per turn, measured by community analysis
("Building a 24/7 Claude Code Wrapper"). The overhead recurs on every turn because the
CLI re-reads global settings on each invocation — not just the first turn of a session.

### 1.2 Why This Matters for Multi-Turn Agents

Token costs in a multi-turn session are not linear:

- Turn 1: system prompt + tools + empty history = ~50K baseline tokens
- Turn N: system prompt + tools + N-1 previous turns = 50K + cumulative history tokens
- At 10 turns with ~3K tokens of content per turn: ~80K total tokens
- At 20 turns: ~110K total tokens — approaching the auto-compaction threshold

Without isolation, the overhead tokens alone (50K) consume a significant fraction of the
200K context window before a single line of code is read or written. For conductor's
sub-agents that may run 15–50 turns to implement an issue, this is the primary cost driver.

### 1.3 Prompt Caching Does Not Eliminate the Problem

Claude Code automatically applies prompt caching (cache reads cost 10% of base input
token price; cache writes cost 125% on first use). The system prompt prefix is cached
across requests within a session. However:

- Prompt caching only helps within a **single session** — it does not apply across
  separate `claude -p` invocations.
- Each new subprocess starts from scratch and must populate its own cache.
- The 50K overhead is incurred fresh on every subprocess startup.
- Within a multi-turn session, caching reduces the re-read cost of the system prompt
  prefix (roughly 18K tokens of system prompt + tool definitions), saving ~90% on
  repeat reads within that session.

**Net effect**: Prompt caching helps reduce cost within a running agent session (the
system prompt is paid fully once, then at 10% on subsequent turns). It does not reduce
the per-invocation startup cost when the orchestrator spawns a new subprocess.

---

## 2. Four-Layer Isolation Strategy

The 4-layer isolation approach, documented in the DEV.to article and cross-referenced in
`01-agent-tool-in-p-mode.md` (Section 3.3), addresses each configuration injection path
independently:

### Layer 1: Scoped Working Directory

Set the subprocess CWD to an isolated worktree rather than the home directory or the
conductor's own repo root. This prevents upward CLAUDE.md traversal from discovering
the user's `~/CLAUDE.md`, the conductor's own CLAUDE.md, and any ancestor configs.

```bash
# Set via subprocess.Popen cwd= parameter (see 04-configuration.md Section 1.1)
cwd="/path/to/worktree/branch-name"
```

**Tokens saved**: 4,000–15,000 tokens per turn (depending on how many CLAUDE.md files
exist in the ancestor path).

### Layer 2: Git Boundary (`.git/HEAD` Trick)

Claude Code stops its upward CLAUDE.md walk at the first `.git` directory it encounters.
For pre-created git worktrees (from `git worktree add`), this boundary is already
present. For isolated scratch workspaces that are not git repos, create a stub `.git/`
directory to act as a stop:

```bash
mkdir -p /tmp/isolated-workdir/.git
echo "ref: refs/heads/main" > /tmp/isolated-workdir/.git/HEAD
```

**Purpose**: Prevents the subprocess from walking all the way to the filesystem root
and loading unexpected ancestor CLAUDE.md files.

### Layer 3: Empty Plugin Directory

Pass an empty directory to `--plugin-dir` to prevent global plugin skills from loading:

```bash
mkdir -p /tmp/empty-plugins
claude -p "..." --plugin-dir /tmp/empty-plugins
```

Or on macOS, use `/dev/null` (it is a valid non-directory path that results in no plugins
loading on some Claude Code versions — verify with your installed version):

```bash
claude -p "..." --plugin-dir /dev/null
```

**Tokens saved**: 5,000–20,000 tokens per turn (depends on how many skill files are
registered globally).

### Layer 4: Restrict Setting Sources

The `--setting-sources` flag controls which settings layers are loaded:

```bash
claude -p "..." --setting-sources "project,local"
# OR — load nothing:
claude -p "..." --setting-sources ""
```

Using `project,local` excludes the user-level `~/.claude/settings.json`, which contains
the list of enabled plugins and MCP servers. This prevents plugin re-injection through
the settings path even if `--plugin-dir` is not empty.

**Tokens saved**: 500–2,000 tokens per turn from blocking user settings; additionally
prevents MCP server re-initialization from user-level `mcpServers` config.

### Combined Isolation Template

```bash
claude -p "<worker prompt>" \
  --system-prompt "<minimal worker instructions>" \
  --setting-sources "project,local" \
  --plugin-dir /tmp/empty-plugins \
  --dangerously-skip-permissions \
  --output-format stream-json \
  --max-turns 50
```

This subprocess must be launched from (or with `cwd=`) the pre-created git worktree
directory. The worktree's own `.git/` directory acts as the traversal boundary.

**Result**: Per-turn overhead drops from ~50,000 tokens to ~5,000 tokens (10x
improvement), per the DEV.to analysis. This matches the figure cited in
`01-agent-tool-in-p-mode.md` Section 3.3.

---

## 3. MCP Overhead and How to Eliminate It

### 3.1 MCP Tool Catalog Size

Each active MCP server contributes its full tool definitions to every context:

| MCP Server Type | Approximate Token Cost |
|---|---|
| Chrome DevTools MCP | ~20,000 tokens |
| Database tools MCP | ~10,000 tokens |
| GitHub MCP (full) | ~5,000–8,000 tokens |
| Filesystem MCP | ~5,000 tokens |
| Small single-tool MCP | ~500–2,000 tokens |

With 3–4 active MCP servers, the tool catalog alone can consume **30,000–40,000 tokens**
before any conversation begins — half the context budget consumed before a word is
written.

### 3.2 MCP Tool Search (Auto-Deferral)

Claude Code now enables MCP Tool Search by default. When MCP tool descriptions exceed
10% of the context window, tools are automatically deferred and loaded on-demand via
the `MCPSearch` tool rather than injected upfront. This can reduce MCP overhead from
~77K tokens to ~8.7K tokens (95% reduction) for sessions with large tool catalogs.

The threshold is configurable:

```bash
# In environment or settings.json:
ENABLE_TOOL_SEARCH=auto:5  # Defer when tools exceed 5% of context
```

**Important caveat for conductor sub-agents**: Tool Search still loads some metadata
upfront. The cleanest approach for sub-agents that genuinely do not need MCP is to
disable MCP entirely.

### 3.3 Eliminating MCP Overhead Entirely

To run a subprocess with zero MCP overhead:

```bash
# Method A: strict-mcp-config with empty config (no MCP servers at all)
claude -p "..." \
  --strict-mcp-config \
  --mcp-config '{}'

# Method B: combination with setting-sources (blocks user-level MCP config too)
claude -p "..." \
  --strict-mcp-config \
  --mcp-config '{}' \
  --setting-sources "project,local"
```

`--strict-mcp-config` tells Claude Code to ignore all MCP server configurations from
user settings and project settings, using only what is specified in `--mcp-config`.
Passing an empty JSON object results in zero MCP servers being initialized.

**Tokens saved**: Up to 40,000+ tokens per turn for heavily-configured environments.

Note: A `--no-mcp` flag has been requested ([GitHub Issue #20873](https://github.com/anthropics/claude-code/issues/20873))
as a simpler alternative. As of March 2026 it has not shipped. Use `--strict-mcp-config
--mcp-config '{}'` in the interim.

---

## 4. `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`

### 4.1 What It Does

Auto-memory is the MEMORY.md feature introduced in late 2025, where Claude Code
automatically accumulates notes across sessions in
`~/.claude/projects/<repo>/memory/MEMORY.md`. When a subprocess launches with a CWD
inside a git repo, Claude Code reads the existing MEMORY.md at startup and injects it
into the system prompt.

Setting `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` prevents:
- Reading the existing MEMORY.md at startup (eliminates inject-at-start tokens)
- Writing new memories at session end (prevents cross-agent contamination)

### 4.2 Token Impact

A populated MEMORY.md file can range from a few hundred tokens (sparse notes) to
several thousand tokens (months of accumulated memory). For conductor sub-agents:

- The memory is **shared across all worktrees of the same git repository** — a sub-agent
  working in `.claude/worktrees/7-my-feature/` reads the same MEMORY.md as the
  orchestrator's main checkout.
- Without disabling auto-memory, multiple parallel sub-agents can write conflicting
  memory entries (see `04-configuration.md` Section 5.2 for the cross-contamination
  risk).

**Recommended**: Always set `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` for sub-agent processes.
This both eliminates the inject-at-start overhead and prevents cross-agent memory
pollution.

```bash
# In the subprocess environment dict:
{
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    ...
}
```

**Estimated token savings**: 500–5,000 tokens per invocation depending on MEMORY.md
size; prevents unbounded growth as agents accumulate notes over time.

---

## 5. Estimated Token Cost per Agent Type

These estimates are for the **optimized invocation** (4-layer isolation applied). They
represent total input tokens across all turns of a session, including cumulative history
growth.

### 5.1 Research Agent

A research agent reads documentation, searches the web, inspects code files, and writes
a markdown report.

| Phase | Input Tokens | Output Tokens | Notes |
|---|---|---|---|
| System startup (isolated) | ~5,000 | — | Baseline with isolation |
| Issue reading (gh issue view) | ~1,000–2,000 | ~500 | Brief tool call |
| Web search turns (×5–8) | ~3,000–5,000 each | ~2,000–4,000 each | Fetch + summarize |
| Doc file reading (×3–5) | ~2,000–8,000 each | ~1,000–2,000 each | Read existing research docs |
| Final write | ~5,000–10,000 | ~3,000–8,000 | Full report generation |
| **Total per session** | **~50,000–100,000** | **~20,000–40,000** | **15–25 turns** |

**Rough estimate**: 70,000–140,000 total tokens per research agent session (input +
output combined, unoptimized rate). With prompt caching within the session, effective
billed tokens are lower — approximately **40,000–80,000** tokens for a research agent
once prompt cache hits kick in after turn 1.

### 5.2 Implementation Agent

An implementation agent reads code, edits files, runs tests, pushes a branch, and
creates a PR.

| Phase | Input Tokens | Output Tokens | Notes |
|---|---|---|---|
| System startup (isolated) | ~5,000 | — | Baseline with isolation |
| Issue + codebase exploration (×5–10) | ~5,000–15,000 each | ~1,000–3,000 each | Reading multiple source files |
| Implementation turns (×10–20) | ~10,000–30,000 cumulative | ~3,000–8,000 each | History grows with code changes |
| Test runs (×3–5) | ~3,000–5,000 each | ~1,000–2,000 each | Bash output |
| PR creation | ~5,000–10,000 | ~1,000–2,000 | Final commit + push |
| **Total per session** | **~100,000–200,000** | **~40,000–80,000** | **20–50 turns** |

**Rough estimate**: 140,000–280,000 total tokens per implementation agent session.
With prompt caching: approximately **80,000–160,000** effective billed tokens.

### 5.3 Health Check / Probe

A minimal probe that verifies configuration is valid:

| Action | Tokens |
|---|---|
| System startup (isolated) | ~5,000 |
| Single turn (return status) | ~500–1,000 |
| **Total** | **~5,500–6,000** |

### 5.4 Comparison: Unoptimized vs. Optimized

| Agent Type | Unoptimized (total tokens) | Optimized (total tokens) | Savings |
|---|---|---|---|
| Research agent (20 turns) | ~1,000,000+ | ~80,000–160,000 | ~87% |
| Implementation agent (30 turns) | ~1,500,000+ | ~80,000–160,000 | ~90%+ |
| Probe (1 turn) | ~50,000 | ~5,500 | ~89% |

The unoptimized figures assume 50K overhead per turn × N turns. History growth makes
the unoptimized case exponentially worse as turn count grows.

---

## 6. Context Compaction Impact on Multi-Turn Sessions

### 6.1 Auto-Compaction Threshold

Claude Code triggers auto-compaction at approximately 83.5% of the context window
(~167,000 tokens of a 200K window). The auto-compact buffer was reduced from 45K to
33K tokens in early 2026, meaning:

- Compaction fires at ~167K tokens
- After compaction, the active context is reduced by summarizing older turns
- The 33K buffer provides headroom for the compaction request itself

### 6.2 How Auto-Compaction Affects Per-Turn Cost

Without compaction, each turn in a long session pays for the full cumulative history:

```
Turn 1:   5K tokens
Turn 10: ~50K tokens (history accumulates)
Turn 20: ~100K tokens
Turn 30: ~150K tokens → nears compaction threshold
Turn 31: compaction fires → context reduced to ~50K summary + active state
Turn 40: ~80K tokens (continues from compacted base)
```

Post-compaction, the agent effectively "restarts" from a condensed summary, making
subsequent turns cheaper. The compaction itself costs roughly 18K system-prompt tokens
(cache hit) + the full context at compaction time as input, plus a summary as output.
Because the system prompt prefix is cached, compaction cost is dominated by processing
the conversation history.

**Important**: Compaction causes information loss. Specific variable names, exact error
messages, and nuanced decisions from early turns are compressed into a summary. For
implementation agents that need to remember exact test output or specific line numbers
from turn 5, auto-compaction can cause rework.

### 6.3 `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`

This environment variable controls when auto-compaction fires:

```bash
# Fire compaction earlier (more frequent but cheaper post-compaction turns):
CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=70

# Default behavior (~83.5%):
# (do not set this variable)
```

For conductor sub-agents, the default is usually fine. Earlier compaction reduces peak
per-turn cost but increases the number of compaction events.

### 6.4 Manual Compaction for Long Sessions

For implementation agents that are expected to run 40+ turns, instructing the agent to
compact manually partway through can reduce cost:

```bash
# In the agent's system prompt:
"When your context usage exceeds 70%, use /compact to summarize your progress before
continuing. Focus the summary on: current branch state, files modified, tests passing,
and next steps."
```

This gives the agent control over what is preserved, avoiding information loss from
automatic compaction.

---

## 7. Cost Estimation Formula for Pre-Dispatch Budget Checks

### 7.1 API Key Mode (USD)

For sessions using `ANTHROPIC_API_KEY` with direct API billing:

```
Estimated cost per agent = (
    (avg_input_tokens × input_price_per_token) +
    (avg_output_tokens × output_price_per_token)
) × cache_discount_factor
```

Current Anthropic API prices (as of March 2026) for Claude Sonnet 4.6:
- Input tokens: $3.00 per million tokens
- Output tokens: $15.00 per million tokens
- Cache read: $0.30 per million tokens (10% of input price)
- Cache write: $3.75 per million tokens (125% of input price, first use only)

Using the optimized estimates from Section 5:

| Agent Type | Est. Input | Est. Output | Raw Cost | With Cache |
|---|---|---|---|---|
| Research agent | 100K | 30K | $0.75 | ~$0.45 |
| Implementation agent | 150K | 60K | $1.35 | ~$0.80 |
| Probe | 6K | 1K | $0.03 | ~$0.02 |

These are per-session estimates. Actual costs depend heavily on codebase size and
agent complexity.

### 7.2 Pre-Dispatch Budget Check Pattern

```python
COST_ESTIMATES = {
    "research":        {"input_k": 100, "output_k": 30,  "usd": 0.45},
    "implementation":  {"input_k": 150, "output_k": 60,  "usd": 0.80},
    "probe":           {"input_k": 6,   "output_k": 1,   "usd": 0.02},
}

def estimate_cost(agent_type: str, model: str = "sonnet") -> float:
    """Return estimated USD cost for one agent session."""
    estimate = COST_ESTIMATES[agent_type]
    if model == "opus":
        # Opus 4.6 is roughly 5x more expensive than Sonnet 4.6
        return estimate["usd"] * 5.0
    return estimate["usd"]

def can_dispatch(agent_type: str, budget_remaining: float) -> bool:
    """Return True if estimated cost fits within remaining budget."""
    return budget_remaining >= estimate_cost(agent_type) * 1.5  # 50% safety margin
```

The 1.5x safety margin accounts for:
- Larger-than-average codebases
- More complex issues requiring more turns
- Context compaction overhead
- Token count variability between issues

### 7.3 Subscription Mode (Pro/Max) — Relative Units

For Pro/Max subscription sessions, there is no USD meter. Use relative
"Sonnet-equivalent turn-equivalents" (STE) as defined in `08-usage-scheduling.md`
Section 7:

| Agent Type | STE | Notes |
|---|---|---|
| Research agent | 5 STE | Calibrate from real runs |
| Implementation agent | 10 STE | Calibrate from real runs |
| Probe | 0.1 STE | Negligible |
| Opus multiplier | 3–5× | Opus draws quota ~3–5× faster than Sonnet |

---

## 8. Recommended Invocation Flags for Minimum Overhead

### 8.1 Implementation Worker (Standard)

```bash
claude -p "<worker prompt>" \
  --system-prompt "<minimal worker instructions>" \
  --setting-sources "project,local" \
  --strict-mcp-config \
  --mcp-config '{}' \
  --plugin-dir /tmp/empty-plugins \
  --tools "Bash,Read,Edit,Write,Glob,Grep" \
  --dangerously-skip-permissions \
  --output-format stream-json \
  --max-turns 50 \
  --no-session-persistence
```

Environment variables:
```bash
CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
DISABLE_AUTOUPDATER=1
DISABLE_ERROR_REPORTING=1
DISABLE_TELEMETRY=1
```

### 8.2 Research Worker (Needs Web Access)

```bash
claude -p "<research prompt>" \
  --system-prompt "<minimal research instructions>" \
  --setting-sources "project,local" \
  --strict-mcp-config \
  --mcp-config '{}' \
  --plugin-dir /tmp/empty-plugins \
  --tools "Bash,Read,Write,Glob,Grep,WebSearch,WebFetch" \
  --dangerously-skip-permissions \
  --output-format stream-json \
  --max-turns 30 \
  --no-session-persistence
```

Note: `WebSearch` and `WebFetch` are Claude Code built-in tools, not MCP servers —
they do not require an active MCP configuration.

### 8.3 What `--system-prompt` Removes and What to Restore

Using `--system-prompt` instead of `--append-system-prompt` **replaces** the entire
Claude Code default prompt. This eliminates:
- All built-in tool usage guidance
- Security and permission guidance
- Task execution principles

For worker agents that have already been given explicit instructions, this is acceptable.
The minimal system prompt for a conductor sub-agent needs only:

```
You are a worker agent in the breadmin-conductor system. You have been given a specific
task to implement. Follow the instructions in the user message exactly. Use the available
tools to complete the work. When the task is done, stop — do not continue with unrelated
work.
```

This replaces ~8,000–12,000 tokens of default Claude Code instructions with ~100 tokens
of task-specific instructions — saving ~8,000–12,000 tokens on every turn.

### 8.4 `--disable-slash-commands` for Sub-agents

```bash
claude -p "..." --disable-slash-commands
```

This flag disables all skill and command loading for the session. Since conductor
sub-agents do not use slash commands, this eliminates skill file injection overhead.
Combined with `--plugin-dir /tmp/empty-plugins`, it provides defense-in-depth against
skill re-injection.

### 8.5 `--no-session-persistence` for Ephemeral Workers

```bash
claude -p "..." --no-session-persistence
```

Sub-agents that will not be resumed after completion should not write their session
history to disk. This flag prevents session files from being written to
`~/.claude/projects/`, eliminating file I/O overhead and preventing disk accumulation
from many parallel agent runs.

---

## 9. Stateful vs. Stateless Workers: `--resume` Overhead

### 9.1 The Persistent Stream-JSON Pattern

An alternative to spawning separate `claude -p` processes for each interaction is to
maintain a long-running `claude` process in "persistent stream-json" mode:

```bash
# Start once, keep process alive, pipe turns via stdin:
claude --print \
  --input-format stream-json \
  --output-format stream-json \
  --session-id "$(uuidgen)"
```

In this mode:
- The system prompt is sent **once** at startup, then cached
- Subsequent turns pay only the cache-read price for the system prompt prefix
- No per-invocation startup overhead
- Turn-by-turn history accumulation is still present

**Savings**: Within a session, the 18K-token system prompt is paid at cache-read rate
(10% of base) on turns 2+, saving ~$0.05 per 1M tokens of system prompt re-reads.

### 9.2 When to Use `--resume` for Workers

The `--resume` flag restores a previous session's context (full conversation history).
For conductor sub-agents:

- **Do not use `--resume`** for workers that failed and need to retry — retrying with
  a full failure history wastes tokens and may confuse the agent.
- **Do use `--resume`** only for long-running tasks that hit `--max-turns` mid-work and
  need to continue (supervisor resumes the same session rather than starting over).
- Note: sessions with isolated `CLAUDE_CONFIG_DIR` cannot be resumed unless the config
  directory is preserved (see `04-configuration.md` Section 5.4).

---

## 10. Summary: Token Overhead by Layer

| Isolation Layer | What It Blocks | Est. Tokens Saved per Turn |
|---|---|---|
| Scoped CWD (worktree) | Ancestor CLAUDE.md files | 4,000–15,000 |
| `--system-prompt` (replace) | Full default Claude Code instructions | 8,000–12,000 |
| `--setting-sources "project,local"` | User `~/.claude/settings.json` (plugin/MCP lists) | 500–2,000 |
| `--strict-mcp-config --mcp-config '{}'` | All MCP tool catalogs | 5,000–40,000+ |
| `--plugin-dir /tmp/empty-plugins` | Global plugin skill files | 5,000–20,000 |
| `--disable-slash-commands` | Skill file injection | 1,000–5,000 |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | MEMORY.md injection at startup | 500–5,000 |
| `--no-session-persistence` | Session write I/O (not tokens, but disk) | N/A (operational) |
| **Total potential savings** | | **~25,000–99,000+ tokens/turn** |

---

## Follow-Up Research Recommendations

### F1: Empirical Baseline Measurement

The 50K → 5K reduction figure comes from a community DEV.to article from late 2025 and
has not been independently verified against Claude Code v2.1.50+. A conductor test run
should:

1. Spawn a `claude -p` subprocess with no isolation flags in the conductor repo CWD.
2. Pass `--output-format json` and examine the `result` event for `usage.input_tokens`.
3. Repeat with each isolation layer applied incrementally.
4. Record actual per-layer savings against the current Claude Code version.

This measurement should be collected as part of conductor's initial integration test
suite and updated with each significant Claude Code version bump.

**Suggested issue**: `Research: Empirical per-layer token overhead measurement for
current Claude Code version` (ensure it is not already covered by issue #12 follow-up
work).

### F2: `--system-prompt` vs. `--append-system-prompt` for Sub-agent Identity

Using `--system-prompt` to replace the entire default prompt provides maximum savings
but removes all built-in Claude Code guidance. The tradeoff:

- Are sub-agents that lack the default prompt still reliable at tool use, file editing,
  and code generation?
- Does the default prompt provide meaningful guidance for implementation quality, or
  is it mostly administrative overhead?
- Is there a minimal `--append-system-prompt` payload that preserves important defaults
  at low token cost?

**Suggested issue**: Evaluate whether replacement (`--system-prompt`) or augmentation
(`--append-system-prompt`) is the right approach for conductor sub-agents in practice.

### F3: Cost Ledger Integration with `stream-json` Result Events

The `result` event in `--output-format stream-json` includes session cost metadata:

```json
{
  "type": "result",
  "session_id": "...",
  "is_error": false,
  "result": "...",
  "usage": {
    "input_tokens": 45000,
    "output_tokens": 8000,
    "cache_read_input_tokens": 12000,
    "cache_creation_input_tokens": 3000
  },
  "total_cost_usd": 0.12
}
```

Conductor should parse this event after each agent run to populate the cost ledger
defined in `04-configuration.md` Section 3.3 (`CONDUCTOR_COST_LEDGER`). Cross-reference
with issue #5 (Logging and Observability) to ensure the ledger schema accommodates
these fields.

**Suggested issue**: Design the JSONL cost ledger schema to capture per-agent token
usage from stream-json result events.

### F4: Per-Agent Model Selection Strategy

Section 5.4 shows Opus costs 5× more than Sonnet per turn. Conductor could use a
tiered model strategy:

- Research agents: Sonnet 4.6 (sufficient reasoning for document synthesis)
- Implementation agents: Sonnet 4.6 (most implementation work does not require Opus)
- Complex architectural decisions: Opus 4.6 (only when specifically needed)

The `CLAUDE_CODE_SUBAGENT_MODEL` env var and `--model` flag allow per-invocation model
selection. Research needed to determine which agent types genuinely benefit from Opus
vs. Sonnet in the conductor workflow.

---

## Sources

- [Building a 24/7 Claude Code Wrapper? Here's Why Each Subprocess Burns 50K Tokens — DEV Community](https://dev.to/jungjaehoon/why-claude-code-subagents-waste-50k-tokens-per-turn-and-how-to-fix-it-41ma) — Primary source for the 4-layer isolation strategy, 50K → 5K reduction measurement, and per-layer savings breakdown
- [CLI Reference — Claude Code Docs](https://code.claude.com/docs/en/cli-reference) — Authoritative flag reference: `--system-prompt`, `--setting-sources`, `--plugin-dir`, `--strict-mcp-config`, `--mcp-config`, `--tools`, `--no-session-persistence`, `--disable-slash-commands`, `--max-turns`, `--max-budget-usd`
- [Manage Costs Effectively — Claude Code Docs](https://code.claude.com/docs/en/costs) — $6/dev/day average; prompt caching; auto-compaction behavior; agent teams 7x multiplier; `CLAUDE_CODE_MAX_OUTPUT_TOKENS`; MCP overhead reduction strategies
- [How Claude Remembers Your Project — Claude Code Docs](https://code.claude.com/docs/en/memory) — CLAUDE.md loading order; MEMORY.md auto-memory; `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`
- [Connect Claude Code to Tools via MCP — Claude Code Docs](https://code.claude.com/docs/en/mcp) — `ENABLE_TOOL_SEARCH`; MCP Tool Search deferral mechanism; auto-threshold at 10%
- [GitHub: Piebald-AI/claude-code-system-prompts](https://github.com/Piebald-AI/claude-code-system-prompts) — Catalog of all Claude Code system prompt components and per-component token counts
- [Claude Code Subagent Cost Explosion: 887K Tokens/Min Crisis — AICosts.ai](https://www.aicosts.ai/blog/claude-code-subagent-cost-explosion-887k-tokens-minute-crisis) — Per-agent initialization overhead (20K–77K tokens); multiplicative vs. linear cost scaling; 73% cost reduction via three-tier hierarchy
- [Claude Code Context Buffer: The 33K-45K Token Problem — claudefa.st](https://claudefa.st/blog/guide/mechanics/context-buffer-management) — 33K buffer size post-2026 update; compaction threshold at 167K; `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`
- [Claude Code MCP Tool Search: Save 95% Context — claudefa.st](https://claudefa.st/blog/tools/mcp-extensions/mcp-tool-search) — 77K → 8.7K token reduction with Tool Search; threshold configuration
- [GitHub Issue #20873: CLI flags to disable MCP, plugins, and agents](https://github.com/anthropics/claude-code/issues/20873) — Feature request for `--no-mcp` flag; confirms `--strict-mcp-config --mcp-config '{}'` as workaround
- [GitHub Issue #23544: Need ability to disable auto-memory (MEMORY.md)](https://github.com/anthropics/claude-code/issues/23544) — Background on `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`
- [Prompt Caching — Claude API Docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) — Cache write (125%), cache read (10%) pricing; TTL behavior; within-session vs. cross-invocation scope
- [Pricing — Claude API Docs](https://platform.claude.com/docs/en/about-claude/pricing) — API token prices for Sonnet and Opus models
- [docs/research/01-agent-tool-in-p-mode.md](01-agent-tool-in-p-mode.md) — Section 3.3: original 50K → 5K isolation claim and 4-layer approach; Section 5.2 recommended subprocess pattern
- [docs/research/04-configuration.md](04-configuration.md) — Section 1.2: CLAUDE.md resolution order; Section 5.2: MEMORY.md shared-across-worktrees behavior; Section 6.1: subprocess env construction pattern; Section 3.4: `CONDUCTOR_CC_DISABLE_AUTO_MEMORY` variable
- [docs/research/08-usage-scheduling.md](08-usage-scheduling.md) — Section 7: STE unit definition; Section 2: token cost estimates for research/implementation agents; Section 5: governor pre-dispatch check pattern
