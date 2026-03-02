# Research: --settings Flag Isolation and MCP Config Injection for Sub-Agents

**Issue:** #10
**Milestone:** M1: Foundation
**Status:** Complete
**Date:** 2026-03-02

---

## Executive Summary

Claude Code provides two distinct mechanisms for per-invocation configuration injection: the `--settings` flag (which loads a JSON file or inline JSON string and merges it into the session's settings at second-highest precedence) and `--mcp-config` (which loads MCP server definitions from JSON files or inline strings). The `--strict-mcp-config` flag, combined with `--mcp-config`, is the canonical pattern for restricting a sub-agent to only a specified set of MCP servers. However, several known bugs affect `--mcp-config` behavior, and the interaction between `--strict-mcp-config` and the `disabledMcpServers` list in `~/.claude.json` is broken (closed as not planned). For the conductor's purposes, the recommended approach is to generate a temporary per-agent `.mcp.json` file in the sub-agent's worktree and combine it with `CLAUDE_CONFIG_DIR` isolation, which provides clean MCP injection without relying on the buggy CLI flags.

---

## Cross-References

- **01-agent-tool-in-p-mode.md** — The subprocess spawning pattern described in section 3.2 of that document is the architectural context for MCP injection. Each `claude -p` worker is a fully independent process that can receive its own `--mcp-config` and `--settings` flags.
- **04-configuration.md** — Section 5.4 of that document introduces `CLAUDE_CONFIG_DIR` isolation and notes the key trade-off: full isolation prevents inheriting user MCP configs. That trade-off is the core problem this document resolves.
- **06-security-threat-model.md** — Section R-SEC-D of that document asks whether `ANTHROPIC_API_KEY` can be scoped to a session token. The `apiKeyHelper` field in the `--settings` schema is the mechanism for credential injection (see section 3.1 of this document).

---

## 1. The `--settings` Flag

### 1.1 Flag Syntax

From the official CLI reference:

```bash
# Load from a JSON file
claude --settings ./agent-settings.json -p "your prompt"

# Load from an inline JSON string
claude --settings '{"permissions":{"allow":["Bash(npm run *)"]}}' -p "your prompt"
```

The flag accepts either a **file path** or an **inline JSON string**. Both forms are equivalent; the distinction is only shell quoting and convenience. The CLI reference describes it as:

> "Path to a settings JSON file or a JSON string to load additional settings from."

### 1.2 Precedence Position

Settings loaded via `--settings` occupy the **second-highest precedence** position in the full composition hierarchy:

```
1. Managed settings (/Library/Application Support/ClaudeCode/managed-settings.json)
   -- cannot be overridden by anything below --
2. --settings flag (CLI invocation) ← this document
3. .claude/settings.local.json (local project, gitignored)
4. .claude/settings.json (shared project, in git)
5. ~/.claude/settings.json (user settings)
```

This means `--settings` overrides everything from project and user settings, but cannot override managed (enterprise/IT-deployed) settings.

### 1.3 Composition: Merge vs. Override

Array-valued keys **merge across scopes** rather than replace. Scalar keys are overridden.

**Array merge example:**

If `~/.claude/settings.json` contains:
```json
{"permissions": {"allow": ["Read"]}}
```

And `--settings` contains:
```json
{"permissions": {"allow": ["Bash(git *)"]}}
```

The effective `allow` list is `["Read", "Bash(git *)"]` — both entries are present.

**Scalar override example:**

If `~/.claude/settings.json` contains `{"model": "claude-sonnet-4-6"}` and `--settings` contains `{"model": "claude-haiku-4-6"}`, the effective model is `claude-haiku-4-6`.

**Critical implication for security policies:** If the operator wants `--settings` to _replace_ an allow list rather than extend it, they cannot rely on `--settings` alone. They must either:
1. Use managed settings (which are merged first but cannot be overridden by user settings)
2. Use `CLAUDE_CONFIG_DIR` pointing to a fresh temp directory (no user settings to merge with)
3. Accept additive behavior and ensure the base allow lists are empty

### 1.4 What `--settings` Can Configure

The full settings JSON schema accepted by `--settings` is a subset of the same schema accepted by `~/.claude/settings.json`. Key fields relevant to the conductor:

| Field | Type | Use Case |
|-------|------|----------|
| `permissions.allow` | `string[]` | Allowlist tool rules for this invocation |
| `permissions.deny` | `string[]` | Denylist tool rules for this invocation |
| `permissions.defaultMode` | `string` | `"acceptEdits"`, `"dontAsk"`, `"bypassPermissions"`, `"plan"` |
| `env` | `{[key: string]: string}` | Inject environment variables into the session |
| `apiKeyHelper` | `string` | Path to script that generates the API key dynamically |
| `hooks` | `object` | `PreToolUse`, `PostToolUse`, etc. for this session |
| `model` | `string` | Override the model for this session |
| `cleanupPeriodDays` | `int` | Session transcript retention |
| `enabledMcpjsonServers` | `string[]` | Allowlist of `.mcp.json` server names to load |
| `disabledMcpjsonServers` | `string[]` | Denylist of `.mcp.json` server names to block |

**What `--settings` does NOT configure directly:**

MCP server definitions themselves (the `mcpServers` object with command/args/env) are **not** stored in `settings.json`. They live in `~/.claude.json` (user/local scope) or `.mcp.json` (project scope). The `--settings` flag can control _which_ already-configured MCP servers are enabled or disabled via `enabledMcpjsonServers` / `disabledMcpjsonServers`, but it cannot inject new MCP server definitions. For that, use `--mcp-config`.

### 1.5 `--settings` and `CLAUDE_CONFIG_DIR` Interaction

When `CLAUDE_CONFIG_DIR` is set to a temp directory, Claude Code's user settings (`$CLAUDE_CONFIG_DIR/settings.json`) will not exist. The settings file hierarchy becomes:

```
1. Managed settings (system directory, unchanged)
2. --settings flag (still loads from the explicit path passed)
3. .claude/settings.local.json (in worktree, if present)
4. .claude/settings.json (in worktree, if present)
5. $CLAUDE_CONFIG_DIR/settings.json (empty temp dir → no user settings)
```

This is the **recommended isolation pattern**: use `CLAUDE_CONFIG_DIR` to eliminate user settings inheritance, then inject exactly the settings the sub-agent needs via `--settings`. No accidental merging from the operator's personal configuration.

### 1.6 Recommended Pattern: Per-Agent `--settings`

Generate a temporary settings file at dispatch time:

```python
import json, tempfile, os, subprocess

def build_agent_settings(allowed_tools: list[str], denied_tools: list[str]) -> str:
    """Write a temporary settings JSON and return the file path."""
    settings = {
        "permissions": {
            "allow": allowed_tools,
            "deny": denied_tools,
            "defaultMode": "dontAsk",
        },
        "env": {
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_ERROR_REPORTING": "1",
        },
    }
    # Use a named temp file so the path can be passed to claude
    f = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="conductor-agent-settings-",
        delete=False,
    )
    json.dump(settings, f)
    f.close()
    return f.name  # caller is responsible for os.unlink() after subprocess exits
```

Then pass it to the subprocess:

```python
settings_path = build_agent_settings(
    allowed_tools=["Read", "Edit(/src/**)", "Bash(git *)"],
    denied_tools=["Bash(curl *)", "Bash(env)", "WebFetch"],
)
try:
    subprocess.run(
        ["claude", "-p", "--settings", settings_path,
         "--dangerously-skip-permissions", prompt],
        cwd=worktree_path,
        env=agent_env,
    )
finally:
    os.unlink(settings_path)
```

**Security note:** The temporary settings file contains no secrets (it only describes permissions). It is safe to write to `/tmp`. The file must be cleaned up after the subprocess exits to avoid leaving permission policies on disk.

---

## 2. MCP Server Configuration Resolution Order in `-p` Mode

### 2.1 Where MCP Servers Are Stored

MCP server definitions live in three locations, each with a different scope:

| Storage location | Scope | How added |
|-----------------|-------|-----------|
| `~/.claude.json` under the current project path | **Local** — private, current project only | `claude mcp add` (default scope) |
| `.mcp.json` in project root | **Project** — shared, version-controlled | `claude mcp add --scope project` |
| `~/.claude.json` globally | **User** — private, all projects | `claude mcp add --scope user` |
| System `managed-mcp.json` | **Managed** — IT-deployed, exclusive | Sysadmin deployment |

**Important distinction:** MCP server _definitions_ (command, args, env) go in `~/.claude.json` or `.mcp.json`. The `settings.json` file only controls which already-defined servers are _enabled_ or _disabled_, via `enabledMcpjsonServers` / `disabledMcpjsonServers`.

### 2.2 Scope Hierarchy and Precedence

When servers with the same name exist at multiple scopes, **local** wins over project, which wins over user:

```
Local scope (~/.claude.json, project path) → highest
Project scope (.mcp.json) → middle
User scope (~/.claude.json, global) → lowest
```

### 2.3 Full Resolution Order at Session Start

When `claude -p` starts, MCP servers are loaded in this order:

1. **`managed-mcp.json`** (system) — if present, takes exclusive control
2. **`--mcp-config` files/strings** — loaded if the flag is provided
3. **Local scope** (`~/.claude.json`, project path) — merged in
4. **Project scope** (`.mcp.json`) — merged in
5. **User scope** (`~/.claude.json`, global) — merged in

After loading, `allowedMcpServers`/`deniedMcpServers` from managed settings filter the final list.

### 2.4 Impact of `CLAUDE_CONFIG_DIR` on MCP Loading

When `CLAUDE_CONFIG_DIR` is set to a temp directory:
- User-scoped MCP servers (stored in `~/.claude.json`) are **not loaded** — the temp dir has no `claude.json`
- Local-scoped MCP servers (stored in `~/.claude.json` under project path) are also **not loaded** — same file
- Project-scoped servers (`.mcp.json` in the worktree) **are** loaded if present

This is why `04-configuration.md` identified a trade-off: `CLAUDE_CONFIG_DIR` isolation prevents inheriting the user's carefully configured MCP servers. The solution documented in sections 3 and 4 below closes this gap.

### 2.5 `ENABLE_CLAUDEAI_MCP_SERVERS` Environment Variable

Claude.ai cloud-based MCP servers (from `claude.ai/settings/connectors`) are also loaded by default if the user is logged in with a Claude.ai account. These can be disabled per-invocation:

```bash
ENABLE_CLAUDEAI_MCP_SERVERS=false claude -p "..." --dangerously-skip-permissions
```

The conductor should always set `ENABLE_CLAUDEAI_MCP_SERVERS=false` in sub-agent process environments to prevent the user's personal cloud integrations from being injected into autonomous workers.

---

## 3. Per-Invocation MCP Injection Pattern

### 3.1 The `--mcp-config` Flag

The `--mcp-config` flag loads one or more MCP server definition files:

```bash
# Load from a file
claude -p --mcp-config ./agent-mcp.json "your prompt"

# Load inline JSON string
claude -p --mcp-config '{"mcpServers":{"github":{"type":"http","url":"https://api.githubcopilot.com/mcp/"}}}' "your prompt"

# Multiple configs (merged)
claude -p --mcp-config ./base-mcp.json --mcp-config ./extra-mcp.json "prompt"
```

The JSON format accepted by `--mcp-config`:

```json
{
  "mcpServers": {
    "server-name": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@some/mcp-package"],
      "env": {
        "API_KEY": "value"
      }
    },
    "remote-server": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer token"
      }
    }
  }
}
```

### 3.2 `--strict-mcp-config`: Restriction to Only Injected Servers

The `--strict-mcp-config` flag causes Claude Code to **ignore all other MCP configurations** and use only what was passed via `--mcp-config`:

```bash
claude -p \
  --strict-mcp-config \
  --mcp-config '{"mcpServers":{"github":{"type":"http","url":"https://api.githubcopilot.com/mcp/"}}}' \
  "Implement issue #7..."
```

This is the canonical pattern for MCP injection in sub-agents: the agent gets exactly the MCPs it needs and nothing more.

**Known bug: `--strict-mcp-config` does not override `disabledMcpServers`** (Issue #14490, closed as not planned, February 2026). If the operator's `~/.claude.json` has a server in the `disabledMcpServers` list, that server will remain disabled even when injected via `--mcp-config --strict-mcp-config`. The workaround is to use `CLAUDE_CONFIG_DIR` pointing to a temp directory (no `~/.claude.json` to inherit disabled lists from).

### 3.3 `--mcp-config` Argument Parsing Bug

A bug in v1.0.73 (Issue #5593, closed as not planned, January 2026) caused `--mcp-config` argument parsing to fail: arguments after `--mcp-config` were incorrectly treated as additional config file paths, and schema validation failed even for valid MCP configurations.

**Workaround:** Use the `--` separator before the prompt string:

```bash
claude --mcp-config ./mcp.json -- "your prompt here"
```

Or use inline JSON (which avoids the argument ordering issue):

```bash
claude --mcp-config '{"mcpServers":{...}}' -p "prompt"
```

As of March 2026, this bug is closed as not planned. Test the specific version before relying on `--mcp-config` in production.

### 3.4 The Reliable Pattern: `.mcp.json` in the Worktree

Given the known fragility of `--mcp-config`, the most reliable pattern for per-agent MCP injection is to write a `.mcp.json` file into the sub-agent's worktree before spawning it:

```python
import json, os, subprocess

def inject_mcp_config(worktree_path: str, mcp_servers: dict) -> None:
    """Write .mcp.json into the worktree for the sub-agent to pick up."""
    mcp_config = {"mcpServers": mcp_servers}
    mcp_path = os.path.join(worktree_path, ".mcp.json")
    with open(mcp_path, "w") as f:
        json.dump(mcp_config, f, indent=2)

# Example: give issue-worker the GitHub MCP only
inject_mcp_config(
    worktree_path="/path/to/.claude/worktrees/7-my-feature",
    mcp_servers={
        "github": {
            "type": "http",
            "url": "https://api.githubcopilot.com/mcp/"
        }
    }
)
```

Combined with `CLAUDE_CONFIG_DIR` pointing to a temp dir, this is the cleanest isolation:
- `CLAUDE_CONFIG_DIR` prevents the user's `~/.claude.json` from being loaded (no user/local MCPs)
- `.mcp.json` in the worktree provides exactly the desired project-scoped MCPs
- The conductor cleans up `.mcp.json` after the sub-agent completes its PR

**Security note:** `.mcp.json` is a project-scope config that would normally be checked into git. Since the conductor creates it in a short-lived worktree and cleans it up, this is safe. Ensure `.mcp.json` is in the project's `.gitignore` to prevent accidental commits by sub-agents.

### 3.5 Per-Agent MCP Injection via Subagent Frontmatter `mcpServers` Field

When the conductor uses the in-process `Agent` tool (not subprocess spawning), sub-agents defined as markdown files or via `--agents` can specify their own MCP servers:

```yaml
# .claude/agents/issue-worker.md
---
name: issue-worker
description: Implements a GitHub issue on a specific branch
tools: Read, Edit, Bash, Glob, Grep, Write
mcpServers:
  - github  # reference to an already-configured server by name
---
```

Or with an inline server definition:

```yaml
mcpServers:
  - github:
      type: http
      url: https://api.githubcopilot.com/mcp/
```

The `mcpServers` field accepts either a server name string (referencing a server already configured in `~/.claude.json` or `.mcp.json`) or an inline `{name: config}` object with the full server definition.

**Limitation:** This approach only applies to in-process subagents (invoked via the `Agent` tool). For subprocess-based workers (the recommended conductor pattern per `01-agent-tool-in-p-mode.md`), the `.mcp.json` injection pattern in section 3.4 is used instead.

---

## 4. How to Disable All MCPs for a Sub-Agent

### 4.1 The Empty `--mcp-config` Pattern

The documented workaround to disable all MCPs for a session:

```bash
claude -p \
  --strict-mcp-config \
  --mcp-config '{}' \
  "Research task that only needs WebFetch..."
```

Passing an empty `mcpServers` object with `--strict-mcp-config` tells Claude Code to ignore all configured MCPs and start with none. This is the official workaround (referenced in feature request #20873 before it was closed as not planned).

**Caveat:** Due to the `--strict-mcp-config` + `disabledMcpServers` bug (section 3.2), this may not fully suppress servers listed in the user's disabled list. Use `CLAUDE_CONFIG_DIR` to bypass this entirely.

### 4.2 The `CLAUDE_CONFIG_DIR` Pattern

The most reliable approach for a fully MCP-free sub-agent:

```python
import tempfile, os, subprocess

with tempfile.TemporaryDirectory() as tmp_config:
    agent_env = {
        **base_env,
        "CLAUDE_CONFIG_DIR": tmp_config,
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }
    # No --mcp-config needed — empty CLAUDE_CONFIG_DIR means no user MCPs
    # No .mcp.json in worktree means no project MCPs
    subprocess.run(
        ["claude", "-p", "--dangerously-skip-permissions", prompt],
        cwd=worktree_path,
        env=agent_env,
    )
```

With this configuration:
- No user-scoped MCPs (no `~/.claude.json` in the temp dir)
- No local-scoped MCPs (same)
- No project-scoped MCPs (no `.mcp.json` in the worktree)
- No Claude.ai MCPs (`ENABLE_CLAUDEAI_MCP_SERVERS=false`)

This is the recommended pattern for **research-worker agents** that only need WebFetch and built-in tools.

### 4.3 `ENABLE_TOOL_SEARCH=false`

When many MCP servers are configured, Claude Code uses MCP Tool Search to defer loading tool definitions. For sub-agents where no MCPs are desired, also set:

```bash
ENABLE_TOOL_SEARCH=false
```

This prevents the `MCPSearch` tool from appearing in the agent's tool context.

---

## 5. Recommended MCP Configs by Agent Type

### 5.1 Issue-Worker Agents

Issue-workers implement code changes in a git worktree and create PRs. They need the GitHub MCP for `gh`-equivalent operations.

```json
{
  "mcpServers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/"
    }
  }
}
```

Write this as `.mcp.json` in the worktree before spawning. Combined with `CLAUDE_CONFIG_DIR` isolation:

```python
agent_env = {
    **clean_base_env,
    "CLAUDE_CONFIG_DIR": tmp_config_dir,
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}
```

**Tools:** `Read, Edit(/src/**), Edit(/tests/**), Bash(git *), Bash(gh *), Bash(uv run pytest *), Bash(uv run ruff *), Write, Glob, Grep`

### 5.2 Research-Worker Agents

Research-workers write to `docs/research/` and use WebFetch for web research. They do NOT need GitHub MCP or any database access.

```json
{}
```

No `.mcp.json` needed — an empty worktree with `CLAUDE_CONFIG_DIR` isolation provides a clean MCP-free environment. WebFetch is a built-in tool, not an MCP tool.

```python
agent_env = {
    **clean_base_env,
    "CLAUDE_CONFIG_DIR": tmp_config_dir,
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "ENABLE_TOOL_SEARCH": "false",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}
```

**Tools:** `Read, Edit(/docs/research/**), Bash(git status), Bash(git add docs/research/*), Bash(git commit *), Bash(git push -u origin *), Bash(gh issue view *), Bash(gh issue list *), Bash(gh issue create *), Bash(gh pr create *), Bash(gh pr checks *), WebFetch, Glob, Grep`

Note: WebFetch domain restriction (`WebFetch(domain:github.com)` etc.) should still be applied via `--settings` or `permissions.allow` to implement the research-worker policy from `06-security-threat-model.md`.

### 5.3 Orchestrator Sessions

The orchestrator is an interactive or long-running `claude -p` session that dispatches workers. It needs the GitHub MCP to manage issues and PRs. It also needs the Notion MCP if session reports are written there.

Since the orchestrator is the parent session (not a sub-agent), it runs with the operator's own `~/.claude.json` configuration. No special injection is needed — the operator configures the GitHub and Notion MCPs in their user or project scope beforehand.

If the orchestrator itself is headlessly invoked (e.g., in CI), inject via `.mcp.json` in the repo root:

```json
{
  "mcpServers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/"
    },
    "notion": {
      "type": "http",
      "url": "https://mcp.notion.com/mcp",
      "headers": {
        "Authorization": "Bearer ${NOTION_API_KEY}"
      }
    }
  }
}
```

**Note on environment variable expansion in `.mcp.json`:** Claude Code supports `${VAR}` expansion in `.mcp.json` fields. This allows the Notion API key to be passed as an environment variable rather than hardcoded.

### 5.4 MCP Config Summary

| Agent Type | MCPs Needed | Pattern |
|-----------|-------------|---------|
| Issue-worker | GitHub MCP | `.mcp.json` in worktree + `CLAUDE_CONFIG_DIR` isolation |
| Research-worker | None | `CLAUDE_CONFIG_DIR` isolation only (no `.mcp.json`) |
| Orchestrator (interactive) | GitHub + Notion | Operator's user/project scope (no injection needed) |
| Orchestrator (CI/headless) | GitHub + Notion | `.mcp.json` in repo root, env var expansion for tokens |

---

## 6. `--settings` and `~/.claude/settings.json` Interaction

### 6.1 Merging Rules Summary

| Key Type | Behavior |
|----------|----------|
| `permissions.allow` (array) | Merged — both `--settings` and user settings entries are active |
| `permissions.deny` (array) | Merged — deny rules from all scopes accumulate |
| `model` (scalar) | `--settings` value wins |
| `env` (object) | Per-key: `--settings` value wins for each key. Pre-existing shell variables are NOT overwritten by `env` entries. |
| `apiKeyHelper` (scalar) | `--settings` value wins |
| `hooks` (object) | Array merge: `PreToolUse` hooks from all scopes are combined |

### 6.2 Interaction with `--setting-sources` Flag

The `--setting-sources` flag controls which settings scopes are loaded:

```bash
# Load only user settings (skip project and local)
claude --setting-sources user "prompt"

# Load project and local settings only (skip user)
claude --setting-sources project,local "prompt"

# Load nothing from disk (useful with --settings providing everything)
claude --setting-sources "" "prompt"
```

When `--setting-sources ""` is combined with `--settings ./my-settings.json`, the session uses **only** the provided settings file (plus managed settings, which cannot be excluded). This is the cleanest way to prevent any user or project settings from being merged in.

**Recommended pattern for conductor sub-agents:**

```bash
claude -p \
  --setting-sources "" \
  --settings ./generated-agent-settings.json \
  --dangerously-skip-permissions \
  "$PROMPT"
```

This gives the conductor complete control over the sub-agent's settings without any inheritance from the operator's personal configuration.

### 6.3 `CLAUDE_CONFIG_DIR` vs. `--setting-sources ""`

Both achieve settings isolation, but through different mechanisms:

| Mechanism | What it isolates | What remains |
|-----------|-----------------|--------------|
| `CLAUDE_CONFIG_DIR=/tmp/xyz` | User settings, user MCP servers, session history | Managed settings, `--settings` flag, project settings |
| `--setting-sources ""` | User and project settings (disk-based) | Managed settings, `--settings` flag, MCP from `~/.claude.json` |
| Both combined | Everything except managed + `--settings` | Managed settings, `--settings` flag only |

The combination of `CLAUDE_CONFIG_DIR` + `--setting-sources ""` + `--settings` provides maximum isolation.

---

## 7. Per-Invocation Settings Generation: Security and Practicality

### 7.1 Is Per-Agent Settings Generation Practical?

Yes. The pattern is:

1. Conductor generates a temporary `settings.json` specific to the agent type
2. Conductor generates a `.mcp.json` in the worktree for the MCPs the agent needs
3. Conductor spawns the sub-agent with `--settings <path>` and `CLAUDE_CONFIG_DIR` isolation
4. Sub-agent runs; its session writes no state to the real `~/.claude/` directory
5. Conductor cleans up the temp settings file and `.mcp.json` after the sub-agent exits

This pattern is used in production by multi-agent orchestration frameworks including ccswarm and the patterns documented in the DEV.to community articles.

### 7.2 Security Implications

**Positive:**
- Sub-agent cannot inherit operator's personal API keys, session tokens, or webhook configs from settings
- MCP server credentials are explicitly injected (not inherited) — principle of least privilege
- Permission policies are explicit and auditable per agent type
- No accidental escalation from operator's permissive personal settings

**Negative / risks:**
- Generated settings files on disk are readable by the agent (Bash tool could `cat` them). The settings file should contain no secrets — API keys should be in the subprocess `env` dict, not in `--settings`.
- If the worktree's `.mcp.json` contains MCP tokens (e.g., Notion `Authorization` header), those are accessible via `cat .mcp.json`. Prefer environment variable expansion (`${NOTION_API_KEY}`) over hardcoded tokens in `.mcp.json`.
- Temp files in `/tmp` are world-readable on some systems. Use `tempfile.mkstemp()` with mode `0o600` and clean up promptly.

Cross-reference: `06-security-threat-model.md` T3 covers credential exposure via process environment. The per-settings pattern moves the attack surface from environment variables to files, which is arguably worse (files persist). The mitigation is restrictive file permissions and cleanup.

---

## 8. Follow-Up Research Recommendations

### 8.1 Empirical Testing of `--setting-sources ""` in `-p` Mode

The `--setting-sources ""` flag is documented in the CLI reference, but its behavior when combined with `--dangerously-skip-permissions` and `--settings` in headless mode has not been empirically verified. A minimal test case should confirm:
- Does `--setting-sources ""` suppress user settings inheritance?
- Does it prevent MCP servers from `~/.claude.json` loading (or does MCP loading happen through a different path)?
- Is there a version requirement (was this flag added after a certain Claude Code version)?

**Suggested test:**
```bash
claude --setting-sources "" --settings '{"model":"claude-haiku-4-6"}' \
  -p --output-format json "What is your current model?"
```

### 8.2 `--mcp-config` Argument Parsing Status

Issue #5593 was closed as "not planned" in January 2026. The `--mcp-config` flag may have had its argument parsing behavior changed without being formally fixed. The current behavior should be verified on the latest Claude Code version (v2.1.50+) before relying on inline JSON form in production.

**Suggested test:**
```bash
claude --mcp-config '{"mcpServers":{}}' -p "What MCP servers do you have available?"
```

### 8.3 Subagent `mcpServers` Inline Definition Syntax

The official documentation states that the `mcpServers` field in subagent frontmatter accepts "a server name string or a `{name: config}` object." The exact YAML syntax for the inline object form is not demonstrated in the official docs with a complete example. This should be empirically verified, especially for HTTP servers with auth headers.

**Suggested test:** Create a test agent at `.claude/agents/test-mcp.md` with:
```yaml
---
name: test-mcp
description: Test MCP injection
mcpServers:
  - test-server:
      type: http
      url: https://example.com/mcp
---
```

Verify that the agent receives the injected server and the main session does not.

### 8.4 Plugin-Based MCP Distribution for Team Deployments

The MCP docs note that plugins can bundle MCP servers. If the conductor project distributes a plugin that includes the GitHub and Notion MCPs, operators would only need to install the plugin rather than configuring servers manually. This could simplify the orchestrator setup story for new users. Research whether plugin-bundled MCPs can be scoped to only the conductor's own sessions or whether they inject globally.

---

## 9. Sources

- [CLI reference — Claude Code Docs](https://code.claude.com/docs/en/cli-reference) — Complete `--settings`, `--mcp-config`, `--strict-mcp-config`, `--setting-sources`, `--tools`, `--agents` flag documentation
- [Settings — Claude Code Docs](https://code.claude.com/docs/en/settings) — Settings file hierarchy, composition rules, array merge behavior, MCP enable/disable fields, `CLAUDE_CONFIG_DIR` interaction
- [Connect Claude Code to tools via MCP — Claude Code Docs](https://code.claude.com/docs/en/mcp) — MCP storage locations, scope hierarchy, `--mcp-config` format, `managed-mcp.json`, `.mcp.json`, environment variable expansion
- [Create custom subagents — Claude Code Docs](https://code.claude.com/docs/en/sub-agents) — `mcpServers` frontmatter field syntax, inline vs. named server references, `--agents` JSON flag format
- [GitHub Issue #5593: Bug: --mcp-config flag broken in v1.0.73](https://github.com/anthropics/claude-code/issues/5593) — Argument parsing bug and schema validation failure in `--mcp-config`; closed not planned
- [GitHub Issue #14490: --strict-mcp-config does not override disabledMcpServers](https://github.com/anthropics/claude-code/issues/14490) — `--strict-mcp-config` interaction bug with `~/.claude.json` disabled list; closed not planned
- [GitHub Issue #20873: Feature request: --no-mcp, --no-plugins, --no-agents CLI flags](https://github.com/anthropics/claude-code/issues/20873) — Current workaround for disabling all MCPs (`--strict-mcp-config --mcp-config '{}'`); feature closed as not planned
- [GitHub Issue #6915: Allow MCP tools to be available only to subagent](https://github.com/anthropics/claude-code/issues/6915) — MCP context pollution problem, current status (open, 263+ upvotes), and workarounds
- [GitHub Issue #4476: Implement Agent-Scoped MCP Configuration with Strict Isolation](https://github.com/anthropics/claude-code/issues/4476) — Feature request for per-agent MCP config isolation
- [GitHub Issue #7289: Disabling MCP tool context in default, but enabling for subagents](https://github.com/anthropics/claude-code/issues/7289) — Related feature request for subagent-only MCP enablement
- [Blocking MCP Tools in Claude Code — Ujjwal Khadka, Medium](https://medium.com/@khadkaujjwal47/blocking-all-mcp-tools-in-claude-code-6dd35e08df0a) — PreToolUse hook approach to blocking all MCP tools
- [claude-code-settings-schema.json — GitHub Gist (xdannyrobertsx)](https://gist.github.com/xdannyrobertsx/0a395c59b1ef09508e52522289bd5bf6) — Community-sourced complete settings.json schema
- [Configuring MCP Tools in Claude Code — Scott Spence](https://scottspence.com/posts/configuring-mcp-tools-in-claude-code) — Practical MCP configuration patterns
- [Claude Code Settings Reference — claudefa.st](https://claudefa.st/blog/guide/settings-reference) — Community settings reference with composition examples
- [SFEIR Institute: Headless Mode and CI/CD Cheatsheet](https://institute.sfeir.com/en/claude-code/claude-code-headless-mode-and-ci-cd/cheatsheet/) — Headless mode flag patterns including settings flags
