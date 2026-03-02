# Research: Configuration and Environment Management

**Issue:** #4
**Milestone:** M1: Foundation
**Status:** Complete

## Overview

This document covers how `breadmin-conductor` should resolve configuration from environment variables, CLI flags, and config files, and how it should handle multi-repo deployments. It answers the core questions from issue #4 using the official Claude Code documentation and the pydantic-settings library.

---

## 1. CWD and CLAUDE.md Resolution in `claude -p`

### 1.1 Working Directory Semantics

`claude -p` does **not** have a `--cwd` flag. As of March 2026, `--cwd` is an open feature request ([anthropics/claude-code #26287](https://github.com/anthropics/claude-code/issues/26287)) marked "Low — Nice to have." It has not shipped.

When `claude -p` is launched as a subprocess, it **inherits the caller's CWD**. The process's working directory at the time of `subprocess.run()` / `subprocess.Popen()` becomes the session's working directory. This is standard POSIX behaviour: child processes inherit `cwd` from their parent unless the spawner explicitly sets it.

The conductor can control a sub-agent's CWD in two ways:

| Method | How | Notes |
|---|---|---|
| `cwd` kwarg in `subprocess.Popen` | `Popen([...], cwd="/path/to/repo")` | Cleanest. Does not change the conductor's own shell CWD. |
| `cd && claude -p` via shell | `Popen(["bash", "-c", "cd /repo && claude -p '...'"])` | Works but less portable; changes shell CWD as a side-effect inside the shell process only. |

**Recommendation:** Use the `cwd=` parameter to `subprocess.Popen`. Set it to the absolute path of the target repository's worktree checkout. This is the correct, side-effect-free approach.

Example:
```python
import subprocess

result = subprocess.run(
    ["claude", "-p", "--dangerously-skip-permissions", "--output-format", "json", prompt],
    cwd="/path/to/repo/.claude/worktrees/7-my-feature",
    env=agent_env,
    capture_output=True,
    text=True,
)
```

### 1.2 CLAUDE.md Resolution Order

Claude Code discovers CLAUDE.md files by **walking up the directory tree from the CWD**. For a session launched with CWD `/repos/myrepo`, the following files are loaded in order from broadest to most specific (most specific wins):

| Scope | Location | Notes |
|---|---|---|
| Managed policy | `/Library/Application Support/ClaudeCode/CLAUDE.md` (macOS) | Cannot be excluded |
| User | `~/.claude/CLAUDE.md` | Always loaded |
| Project | `{cwd}/CLAUDE.md` or `{cwd}/.claude/CLAUDE.md` | Shared via git |
| Local project | `{cwd}/CLAUDE.local.md` | Gitignored, machine-local |
| Ancestor directories | `{cwd}/../CLAUDE.md`, `{cwd}/../../CLAUDE.md`, etc. | Loaded if present |

Files in subdirectories *below* the CWD are loaded **on demand** when Claude reads files in those directories, not at startup.

**CLAUDE.md Discovery Control:**

The `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1` env var enables CLAUDE.md loading from directories provided with `--add-dir`. Without this var, `--add-dir` grants file access but does not load CLAUDE.md from those directories.

To **exclude** unwanted CLAUDE.md files (e.g., in a monorepo where sibling-team CLAUDE.md files would be picked up), configure `claudeMdExcludes` in `.claude/settings.local.json`:

```json
{
  "claudeMdExcludes": [
    "**/other-team/.claude/**",
    "/path/to/root/CLAUDE.md"
  ]
}
```

**Key implication for conductor:** When spawning a sub-agent in a worktree at `.claude/worktrees/7-my-feature/`, that agent's CWD is inside the conductor repo. It will walk up and discover **the conductor's own CLAUDE.md**. If the worktree is for a different repo (e.g., `breadwinner`), the conductor's CLAUDE.md may not apply. Use `claudeMdExcludes` in the sub-agent's `.claude/settings.json` if needed, or use a custom `--append-system-prompt-file` to provide repo-specific context.

---

## 2. `--append-system-prompt-file` Semantics

### 2.1 System Prompt Flags Reference

| Flag | Behaviour | Modes | Use case |
|---|---|---|---|
| `--system-prompt` | **Replaces** entire default prompt | Interactive + Print | Complete control; loses all Claude Code defaults |
| `--system-prompt-file` | **Replaces** with file contents | Print only | Version-controlled prompt templates |
| `--append-system-prompt` | **Appends** to default prompt | Interactive + Print | Add instructions, preserve defaults |
| `--append-system-prompt-file` | **Appends** file contents to default | Print only | Add file-based instructions, preserve defaults |

`--system-prompt` and `--system-prompt-file` are **mutually exclusive**. The append flags can be combined with either replacement flag.

### 2.2 Interaction with CLAUDE.md

**CLAUDE.md and `--append-system-prompt-file` are distinct and additive:**

- `--append-system-prompt-file` appends text to the **system prompt** (before the first user turn).
- CLAUDE.md is loaded as a separate context block — historically as a user-turn message in Claude Code's default configuration, but effectively treated as part of the agent's instructions.

When using the Agent SDK programmatically (Python/TypeScript), CLAUDE.md is **only loaded if you explicitly set `setting_sources=["project"]`** (or `"user"`). The `claude_code` preset alone does NOT auto-load CLAUDE.md. The CLI (`claude -p`) is different: it behaves like interactive mode and loads CLAUDE.md from the CWD hierarchy automatically.

**Important distinction:**
- CLI `claude -p`: auto-discovers and loads CLAUDE.md based on CWD.
- Python Agent SDK: CLAUDE.md only loads if `setting_sources` is configured.

For conductor's subprocess-based approach using `claude -p`, CLAUDE.md loads automatically from the worktree's CWD. The `--append-system-prompt-file` flag can be used *in addition* to inject conductor-level orchestration instructions that supplement the project's CLAUDE.md.

### 2.3 Recommended Use Pattern

```bash
claude -p \
  --dangerously-skip-permissions \
  --output-format stream-json \
  --append-system-prompt-file ~/.conductor/agent-instructions.md \
  "Implement issue #7 on branch 7-my-feature..."
```

Where `agent-instructions.md` contains the sub-agent instructions template (from CLAUDE.md global rules), while the project-specific CLAUDE.md in the worktree provides repo context.

---

## 3. `CONDUCTOR_*` Environment Variable Schema

The conductor should use a `CONDUCTOR_` prefix for all its own env vars. The table below defines the complete schema, separated into required, optional, and passthrough categories.

### 3.1 Required Variables

| Variable | Type | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | `str` | — | Anthropic API key for all `claude -p` calls. **Required.** |
| `CONDUCTOR_REPO` | `str` | — | Default GitHub repo (`owner/repo`) for issue operations. Required unless passed as CLI flag. |

### 3.2 Optional Variables — Behaviour Control

| Variable | Type | Default | Description |
|---|---|---|---|
| `CONDUCTOR_MODEL` | `str` | `"claude-opus-4-6"` | Model passed as `--model` to `claude -p`. |
| `CONDUCTOR_MAX_TURNS` | `int` | `50` | Max agentic turns per sub-agent session (`--max-turns`). |
| `CONDUCTOR_MAX_BUDGET_USD` | `float` | `5.00` | Max spend per sub-agent call (`--max-budget-usd`). |
| `CONDUCTOR_TIMEOUT_SECONDS` | `int` | `1800` | Wall-clock timeout for each `claude -p` subprocess (seconds). |
| `CONDUCTOR_ALLOWED_TOOLS` | `str` | `"Bash,Read,Edit,Glob,Grep,Write"` | Comma-separated tools to pass as `--allowedTools`. |
| `CONDUCTOR_PERMISSION_MODE` | `str` | `"dangerously-skip"` | Maps to `--dangerously-skip-permissions` (`dangerously-skip`) or `--permission-mode plan` (`plan`). |
| `CONDUCTOR_OUTPUT_FORMAT` | `str` | `"stream-json"` | Output format for `claude -p` calls (`text`, `json`, `stream-json`). |
| `CONDUCTOR_APPEND_SYSTEM_PROMPT_FILE` | `Path \| None` | `None` | Path to a `.md` file whose contents are appended to every sub-agent's system prompt. |
| `CONDUCTOR_WORKTREE_BASE` | `Path` | `{repo}/.claude/worktrees` | Directory under which sub-agent git worktrees are created. |

### 3.3 Optional Variables — Paths and Storage

| Variable | Type | Default | Description |
|---|---|---|---|
| `CONDUCTOR_LOG_DIR` | `Path` | `~/.local/share/conductor/logs` | Directory for per-session JSONL logs. |
| `CONDUCTOR_COST_LEDGER` | `Path` | `~/.local/share/conductor/cost.jsonl` | Path to the append-only cost ledger. |
| `CONDUCTOR_CONFIG_FILE` | `Path \| None` | auto-discovered | Explicit path to `.conductor.toml` or `pyproject.toml`. Overrides auto-discovery. |

### 3.4 Optional Variables — Claude Code Pass-Through

These map directly to Claude Code env vars and are passed to sub-agent processes:

| Variable | Maps to Claude Code Var | Default | Description |
|---|---|---|---|
| `CONDUCTOR_CC_DISABLE_TELEMETRY` | `CLAUDE_CODE_ENABLE_TELEMETRY=0` | unset | Disable telemetry in sub-agents. |
| `CONDUCTOR_CC_DISABLE_NONESSENTIAL` | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` | unset | Disable autoupdater, bug command, error reporting in sub-agents. |
| `CONDUCTOR_CC_DISABLE_AUTO_MEMORY` | `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | unset | Disable auto memory in sub-agents. |
| `CONDUCTOR_CC_MAX_OUTPUT_TOKENS` | `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | `"32000"` | Token limit for sub-agent output. |
| `CONDUCTOR_CC_SUBAGENT_MODEL` | `CLAUDE_CODE_SUBAGENT_MODEL` | unset | Model for sub-agents spawned within the sub-agent session. |

### 3.5 Important Claude Code Env Vars (Not Prefixed)

These are read **by Claude Code itself** and should be included in the subprocess environment as-is:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Authentication. Must be present in subprocess env. |
| `ANTHROPIC_MODEL` | Overrides model if `--model` flag is not used. |
| `ANTHROPIC_BASE_URL` | Custom endpoint (e.g., for proxying). |
| `CLAUDE_CONFIG_DIR` | Override where Claude Code stores its config and data. Useful for isolation. |
| `DISABLE_AUTOUPDATER=1` | Prevents auto-update check during headless runs. |
| `DISABLE_ERROR_REPORTING=1` | Disables Sentry error reporting. |
| `DISABLE_TELEMETRY=1` | Opt out of Statsig telemetry. |

---

## 4. Config Resolution Order

Following the convention of most Python CLIs and pydantic-settings:

```
CLI flags
  > Environment variables (CONDUCTOR_*)
    > Config file ([tool.conductor] in pyproject.toml or .conductor.toml)
      > Defaults (hardcoded in ConductorSettings)
```

### 4.1 Config File Discovery

The config file is discovered in this order:

1. **`CONDUCTOR_CONFIG_FILE`** env var — explicit override, highest priority.
2. **`pyproject.toml` in the current working directory** — standard Python project config.
3. **Walk up the directory tree** looking for `pyproject.toml` (up to N levels, configurable via `pyproject_toml_depth`).
4. **`~/.config/conductor/config.toml`** — user-global config, as a fallback.
5. **Defaults** — no file required for basic operation.

### 4.2 pyproject.toml Table Format

Config stored in `[tool.conductor]`:

```toml
[tool.conductor]
repo = "myorg/myrepo"
model = "claude-opus-4-6"
max_turns = 50
max_budget_usd = 5.00
timeout_seconds = 1800
log_dir = "~/.local/share/conductor/logs"
```

### 4.3 pydantic-settings Implementation Pattern

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict, PyprojectTomlConfigSettingsSource

class ConductorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONDUCTOR_",
        env_nested_delimiter="__",
        pyproject_toml_table_header=("tool", "conductor"),
        pyproject_toml_depth=3,  # walk up to 3 parent dirs
    )

    # Required
    anthropic_api_key: str = Field(..., validation_alias="ANTHROPIC_API_KEY")
    repo: str | None = None

    # Optional — behaviour
    model: str = "claude-opus-4-6"
    max_turns: int = 50
    max_budget_usd: float = 5.00
    timeout_seconds: int = 1800
    allowed_tools: str = "Bash,Read,Edit,Glob,Grep,Write"
    permission_mode: str = "dangerously-skip"
    output_format: str = "stream-json"
    append_system_prompt_file: str | None = None

    # Optional — paths
    log_dir: str = "~/.local/share/conductor/logs"
    cost_ledger: str = "~/.local/share/conductor/cost.jsonl"

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (
            kwargs["init_settings"],          # programmatic
            kwargs["env_settings"],            # CONDUCTOR_* env vars
            PyprojectTomlConfigSettingsSource(settings_cls),  # pyproject.toml
            kwargs["dotenv_settings"],         # .env file (optional)
            kwargs["secrets_settings"],        # /run/secrets/ (optional)
        )
```

Note: `ANTHROPIC_API_KEY` uses `validation_alias` to bypass the prefix, since it is a well-known env var name not prefixed with `CONDUCTOR_`.

---

## 5. Multi-Repo Isolation Requirements

### 5.1 Isolation Model

A single conductor process managing multiple repos in parallel requires isolation at three levels:

| Level | Risk without isolation | Isolation mechanism |
|---|---|---|
| Filesystem | Sub-agents overwrite each other's working files | Git worktrees: each agent gets its own `.claude/worktrees/{branch}` |
| CWD | CLAUDE.md from wrong repo loaded | Pass `cwd=` to subprocess — each points to its own worktree |
| Config | Shared config state leaking between repos | Per-invocation env dict; no shared mutable state |

### 5.2 Git Worktree Strategy

Each sub-agent runs in an isolated git worktree. The conductor creates the worktree before dispatching the sub-agent:

```bash
git worktree add .claude/worktrees/7-my-feature 7-my-feature
```

The sub-agent's CWD is set to that worktree path. CLAUDE.md discovery from that path walks up through the worktree — sharing the same `.git/` metadata (and thus the same `~/.claude/projects/<repo>/memory/`) — but each agent operates on its own branch.

**Critical note:** Auto-memory in `~/.claude/projects/<repo>/memory/` is **shared across all worktrees** of the same git repository. Sub-agents in different worktrees of the same repo will read and write the same `MEMORY.md`. This is a known behaviour as of 2025. For conductor's use case (short-lived sub-agents), `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` should be set to prevent cross-contamination.

### 5.3 Multi-Repo Support

A single conductor invocation can manage multiple repos simultaneously by dispatching sub-agents with different `cwd=` values. The key requirements:

1. Each subprocess receives its own `env` dict derived from a per-repo config merge.
2. `CONDUCTOR_REPO` is set per-subprocess to the correct `owner/repo` value.
3. Worktree paths must be unique across repos (use absolute paths, never relative).
4. If repos share a parent (monorepo scenario), configure `claudeMdExcludes` in each sub-agent's settings to prevent ancestor CLAUDE.md files from being loaded across repo boundaries.

### 5.4 `CLAUDE_CONFIG_DIR` for Isolation

Setting `CLAUDE_CONFIG_DIR` to a per-agent temporary directory provides strong session-level isolation:

```python
import tempfile

with tempfile.TemporaryDirectory() as tmp_config:
    agent_env = {
        **base_env,
        "CLAUDE_CONFIG_DIR": tmp_config,
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }
    subprocess.run(["claude", "-p", ...], cwd=worktree_path, env=agent_env)
```

This prevents sub-agents from reading or writing shared Claude Code settings or session history. The trade-off is that no settings (allowed tools, MCP servers) from the user's `~/.claude/` are inherited. The conductor must pass all relevant settings explicitly via CLI flags.

---

## 6. Secrets Management

### 6.1 `ANTHROPIC_API_KEY` Handling

The `ANTHROPIC_API_KEY` must be present in every sub-agent subprocess environment. The safest approach is:

1. **Conductor reads the key once at startup** from its own env (`os.environ["ANTHROPIC_API_KEY"]`).
2. **Conductor never logs or prints the key** — structured logging must redact it.
3. **Sub-agents receive it via the subprocess `env` dict**, copied from the conductor's own env.
4. **Never hardcode the key** in config files, CLI invocations, or log output.

```python
import os

def build_agent_env(base_env: dict) -> dict:
    """Build a clean env dict for a sub-agent subprocess."""
    env = {
        # Always include the API key
        "ANTHROPIC_API_KEY": base_env["ANTHROPIC_API_KEY"],
        # Pass through PATH and locale
        "PATH": base_env.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": base_env.get("HOME", ""),
        "LANG": base_env.get("LANG", "en_US.UTF-8"),
        # Disable non-essential traffic in sub-agents
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        # CRITICAL: filter out CLAUDECODE=1 to allow nesting
        # See: https://github.com/anthropics/claude-agent-sdk-python/issues/573
    }
    # Do NOT include CLAUDECODE or any session-state vars from the parent
    return env
```

### 6.2 The `CLAUDECODE=1` Nesting Problem

Claude Code sets `CLAUDECODE=1` in its own environment. If the conductor is itself running inside a Claude Code session (e.g., during development/testing), this variable is inherited by sub-agent subprocesses. The Claude Code CLI detects `CLAUDECODE=1` and refuses to start, producing:

```
Error: Claude Code cannot be launched inside another Claude Code session.
```

**Fix:** Explicitly exclude `CLAUDECODE` from the subprocess env dict. Never include it in `build_agent_env()`. This is documented as bug [anthropics/claude-agent-sdk-python #573](https://github.com/anthropics/claude-agent-sdk-python/issues/573) and fixed in PR #594 of the Python SDK, but conductor must handle it independently since it spawns `claude` via subprocess rather than the SDK.

### 6.3 `apiKeyHelper` for Rotating Credentials

For environments where API keys rotate (e.g., CI/CD, cloud deployments), Claude Code supports an `apiKeyHelper` setting:

```json
{
  "apiKeyHelper": "/usr/local/bin/fetch-api-key.sh"
}
```

The helper is called every `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` milliseconds to refresh the key. For conductor, this should be configured in the sub-agent's settings file (which can be passed via `--settings ./sub-agent-settings.json`).

### 6.4 `.env` File Support

Conductor should support loading `ANTHROPIC_API_KEY` from a `.env` file in the project root (via `python-dotenv` or pydantic-settings' `env_file` support), but `.env` must **never** be committed to git. The `.gitignore` for the conductor project must exclude:

```
.env
.env.*
!.env.example
```

---

## 7. Key Claude Code Env Vars Summary

The following table consolidates all Claude Code env vars relevant to conductor's subprocess management:

| Variable | Required? | Set by | Purpose |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Conductor env | Authentication |
| `ANTHROPIC_MODEL` | No | Conductor config | Model override |
| `ANTHROPIC_BASE_URL` | No | Conductor config | Custom endpoint |
| `CLAUDE_CONFIG_DIR` | No | Conductor per-agent | Isolation of config/state |
| `CLAUDECODE` | **Must NOT be set** | Filter from parent env | Prevents nesting rejection |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | Recommended | Conductor per-agent | Prevent cross-agent memory pollution |
| `DISABLE_AUTOUPDATER` | Recommended | Conductor per-agent | Avoid update checks in CI |
| `DISABLE_ERROR_REPORTING` | Recommended | Conductor per-agent | Avoid Sentry traffic |
| `DISABLE_TELEMETRY` | Recommended | Conductor per-agent | Opt out of Statsig |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | No | Conductor config | Cap token usage per turn |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | Optional | Conductor per-agent | Batch disable non-essential calls |

---

## 8. Related Research Documents

As of this writing, no other research documents exist in `docs/research/` — this is the first delivered document. The following cross-references apply once those documents are complete:

- **#1 Agent Tool in `-p` Mode**: The isolation model in section 5 assumes sub-agents are spawned via `subprocess.Popen`. If issue #1 finds that `Agent(isolation: "worktree")` is available in `-p` mode, the CWD/env handling in sections 1 and 6 would apply to the outer orchestrator invocation only.
- **#2 Session Continuity**: The `CLAUDE_CONFIG_DIR` isolation pattern in section 5.4 interacts with session history. A session with an isolated `CLAUDE_CONFIG_DIR` cannot be resumed by a later `--resume` call unless the temp dir is preserved.
- **#3 Error Handling**: The `CONDUCTOR_TIMEOUT_SECONDS` env var in section 3.2 feeds directly into the error handling and retry logic.
- **#5 Logging and Observability**: The `CONDUCTOR_LOG_DIR` and `CONDUCTOR_COST_LEDGER` paths in section 3.3 are inputs to the logging design.
- **#6 Security Threat Model**: The `CLAUDECODE=1` nesting issue (section 6.2) and the subprocess env stripping pattern (section 6.1) are inputs to the security threat model.

---

## 9. Follow-Up Research Recommendations

The following open questions warrant dedicated research issues:

### 9.1 `--settings` Flag Isolation (New Question)

Claude Code's `--settings <path>` flag loads a settings JSON file, which can configure `allowedTools`, `permissions`, `env`, and `apiKeyHelper`. This could be a cleaner alternative to passing many individual env vars and CLI flags to each sub-agent:

- Can a per-sub-agent `settings.json` be generated at dispatch time and passed via `--settings`?
- Does `--settings` compose with or override `~/.claude/settings.json`?
- What is the full schema of the settings JSON that `--settings` accepts?

### 9.2 `CLAUDE_CONFIG_DIR` Isolation Tradeoffs

Full isolation via `CLAUDE_CONFIG_DIR` prevents inheriting the user's MCP server configs, allowed tools, and other preferences. The tradeoffs need clarification:

- Does `--settings` override or supplement the settings in `CLAUDE_CONFIG_DIR`?
- Can conductor inject MCP server configs (e.g., GitHub MCP) via `--mcp-config` without requiring the user's global config?

### 9.3 Config File Format: `.conductor.toml` vs. `pyproject.toml [tool.conductor]`

Both are valid. The research question is which to **default to** and whether to support both:

- `pyproject.toml [tool.conductor]`: Standard Python convention; useful when conductor is installed in the target repo.
- `.conductor.toml`: Standalone; useful when conductor manages multiple repos from a central location.
- Which approach better supports the "conductor as a global CLI" vs. "conductor as a per-repo tool" deployment model?

### 9.4 Secrets Rotation in Long-Running Sessions

If `claude -p` runs for 30+ minutes, the `ANTHROPIC_API_KEY` passed at process start may expire (for organisations using short-lived tokens). The `apiKeyHelper` mechanism exists for this, but:

- How does `apiKeyHelper` interact with subprocess spawning? Does the helper run inside the sub-agent or in the conductor?
- Is there a programmatic way to signal a key refresh to a running `claude -p` process?

---

## Sources

- [Claude Code: Run programmatically (headless docs)](https://code.claude.com/docs/en/headless) — Official headless/SDK usage documentation
- [Claude Code CLI Reference](https://code.claude.com/docs/en/cli-reference) — Complete flag reference including `--append-system-prompt-file`, `--settings`, `--worktree`
- [Claude Code: How Claude remembers your project (memory/CLAUDE.md)](https://code.claude.com/docs/en/memory) — CLAUDE.md resolution hierarchy, load order, directory walking behaviour
- [Claude Code Settings Reference](https://code.claude.com/docs/en/settings) — Complete env var catalogue, settings JSON schema, `CLAUDE_CONFIG_DIR`, `apiKeyHelper`
- [Agent SDK: Modifying system prompts](https://platform.claude.com/docs/en/agent-sdk/modifying-system-prompts) — How `--append-system-prompt-file` interacts with CLAUDE.md; `setting_sources` requirement
- [GitHub Issue: `--cwd` flag feature request (anthropics/claude-code #26287)](https://github.com/anthropics/claude-code/issues/26287) — Confirms `--cwd` is not yet available; workarounds documented
- [GitHub Issue: CLAUDECODE=1 nesting bug (anthropics/claude-agent-sdk-python #573)](https://github.com/anthropics/claude-agent-sdk-python/issues/573) — CLAUDECODE env var must be filtered from subprocess env
- [GitHub Issue: Expose project root to subagents (anthropics/claude-code #26429)](https://github.com/anthropics/claude-code/issues/26429) — CLAUDE_PROJECT_DIR empty in subagent environments
- [GitHub Issue: CWD memory leak (anthropics/claude-code #8856)](https://github.com/anthropics/claude-code/issues/8856) — CWD tracking via `/tmp/claude-*-cwd` temp files
- [Pydantic Settings — Settings Management](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — `env_prefix`, `PyprojectTomlConfigSettingsSource`, `pyproject_toml_table_header`, `pyproject_toml_depth`
- [Claude Code Worktrees Guide](https://claudefa.st/blog/guide/development/worktree-guide) — Multi-agent parallel isolation using git worktrees
- [GitGuardian: Python secrets management best practices](https://blog.gitguardian.com/how-to-handle-secrets-in-python/) — API key handling, `.env` gitignore, secret rotation
- [Claude Code: SFEIR Institute — Headless Mode and CI/CD](https://institute.sfeir.com/en/claude-code/claude-code-headless-mode-and-ci-cd/tutorial/) — Headless mode patterns and env var handling
