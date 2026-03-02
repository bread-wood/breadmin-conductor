# Low-Level Design: `config` Module

**Module:** `src/composer/config.py`
**Issue:** #109
**Milestone:** M2: Implementation
**Date:** 2026-03-02
**Status:** Draft

---

## Table of Contents

1. [Module Overview](#1-module-overview)
2. [Pydantic-settings Model](#2-pydantic-settings-model)
3. [Resolution Order](#3-resolution-order)
4. [Subprocess Env Dict Construction](#4-subprocess-env-dict-construction)
5. [Credential Proxy Pattern](#5-credential-proxy-pattern)
6. [Error Cases](#6-error-cases)
7. [Interface Summary](#7-interface-summary)

---

## 1. Module Overview

**Purpose:** Resolve, validate, and expose all runtime configuration for breadmin-composer.

**File path:** `src/composer/config.py`

**What it exports:**

| Symbol | Kind | Purpose |
|--------|------|---------|
| `Config` | class | Pydantic-settings model; the single source of truth for all runtime settings |
| `build_subprocess_env` | function | Construct a sanitized environment dict for spawning `claude -p` subprocesses |
| `load_config` | function | Convenience factory that validates settings and aborts on missing required vars |

No other symbols are part of the public interface. Implementation helpers (e.g., path expanders, validators) are module-private.

---

## 2. Pydantic-settings Model

### 2.1 Class Definition

```
class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONDUCTOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,          # CONDUCTOR_MODEL and conductor_model both work
        populate_by_name=True,         # allows both alias and field name
    )

    # --- Required ---
    anthropic_api_key : str        [env: ANTHROPIC_API_KEY,  required, no prefix]
    github_token      : str        [env: GITHUB_TOKEN,       required, no prefix]

    # --- Behaviour Control ---
    max_budget_usd              : float  [env: CONDUCTOR_MAX_BUDGET_USD,             default: 5.00,  min: 0.01]
    max_retries                 : int    [env: CONDUCTOR_MAX_RETRIES,                default: 3,     min: 0]
    max_concurrency             : int    [env: CONDUCTOR_MAX_CONCURRENCY,            default: 5,     min: 1]
    backoff_base_seconds        : float  [env: CONDUCTOR_BACKOFF_BASE_SECONDS,       default: 2.0,   min: 0.1]
    backoff_max_minutes         : float  [env: CONDUCTOR_BACKOFF_MAX_MINUTES,        default: 32.0,  min: 1.0]
    agent_timeout_minutes       : float  [env: CONDUCTOR_AGENT_TIMEOUT_MINUTES,      default: 30.0,  min: 1.0]
    subscription_tier           : str    [env: CONDUCTOR_SUBSCRIPTION_TIER,          default: "pro", choices: pro|max|max20x]

    # --- Paths ---
    log_dir           : Path  [env: CONDUCTOR_LOG_DIR,          default: ~/.composer/logs]
    checkpoint_dir    : Path  [env: CONDUCTOR_CHECKPOINT_DIR,   default: ~/.composer/checkpoints]

    # --- Derived (properties, not fields) ---
    @property sessions_dir   -> Path   (log_dir / "sessions")
    @property cost_ledger    -> Path   (log_dir / "cost.jsonl")
```

### 2.2 Field Reference Table

| Field | Type | Env Var | Default | Required | Validation Rule |
|-------|------|---------|---------|----------|-----------------|
| `anthropic_api_key` | `str` | `ANTHROPIC_API_KEY` | — | YES | Non-empty; uses `validation_alias` to bypass `CONDUCTOR_` prefix |
| `github_token` | `str` | `GITHUB_TOKEN` | — | YES | Non-empty; uses `validation_alias` to bypass `CONDUCTOR_` prefix |
| `max_budget_usd` | `float` | `CONDUCTOR_MAX_BUDGET_USD` | `5.00` | No | `> 0.0`; Pydantic `Field(ge=0.01)` |
| `max_retries` | `int` | `CONDUCTOR_MAX_RETRIES` | `3` | No | `>= 0`; `Field(ge=0)` |
| `max_concurrency` | `int` | `CONDUCTOR_MAX_CONCURRENCY` | `5` | No | `>= 1`; `Field(ge=1)` |
| `backoff_base_seconds` | `float` | `CONDUCTOR_BACKOFF_BASE_SECONDS` | `2.0` | No | `>= 0.1`; `Field(ge=0.1)` |
| `backoff_max_minutes` | `float` | `CONDUCTOR_BACKOFF_MAX_MINUTES` | `32.0` | No | `>= 1.0`; `Field(ge=1.0)` |
| `agent_timeout_minutes` | `float` | `CONDUCTOR_AGENT_TIMEOUT_MINUTES` | `30.0` | No | `>= 1.0`; `Field(ge=1.0)` |
| `subscription_tier` | `str` | `CONDUCTOR_SUBSCRIPTION_TIER` | `"pro"` | No | `Literal["pro", "max", "max20x"]` |
| `log_dir` | `Path` | `CONDUCTOR_LOG_DIR` | `~/.composer/logs` | No | Path is created if missing (by caller; config does not mkdir) |
| `checkpoint_dir` | `Path` | `CONDUCTOR_CHECKPOINT_DIR` | `~/.composer/checkpoints` | No | Same as above |

### 2.3 Required Fields: Bypass of `CONDUCTOR_` Prefix

`ANTHROPIC_API_KEY` and `GITHUB_TOKEN` are industry-standard env var names that must NOT receive the `CONDUCTOR_` prefix. Pydantic-settings handles this with `validation_alias`:

```python
from pydantic import Field
from pydantic.aliases import AliasChoices

class Config(BaseSettings):
    anthropic_api_key: str = Field(
        validation_alias=AliasChoices("ANTHROPIC_API_KEY", "anthropic_api_key"),
    )
    github_token: str = Field(
        validation_alias=AliasChoices("GITHUB_TOKEN", "github_token"),
    )
```

`AliasChoices` allows either the canonical env var name or the lowercase Python attribute name, but not the `CONDUCTOR_ANTHROPIC_API_KEY` form.

### 2.4 CLI Flag Override Behavior

CLI flags shadow env vars. The `load_config` factory accepts keyword arguments that override any env var or default:

```python
# Pseudocode: how CLI flags flow into Config
def load_config(**cli_overrides) -> Config:
    return Config(**cli_overrides)
```

Pydantic-settings applies overrides in this order (highest to lowest):
1. `__init__` kwargs (CLI-sourced overrides passed by the CLI layer)
2. Environment variables
3. `.env` file values
4. Field defaults

The CLI layer (in `cli.py`) is responsible for parsing flags and passing them as kwargs. The `Config` class itself is unaware of argparse or Click; it only sees the resolved values.

**Example:** If `CONDUCTOR_MAX_BUDGET_USD=10.00` is set in the environment but `--max-budget 3.00` is passed on the CLI, `cli.py` calls `load_config(max_budget_usd=3.00)`, which results in `config.max_budget_usd == 3.00`.

### 2.5 `subscription_tier` Semantics

The `subscription_tier` field informs the runner about which Claude Code subscription the operator has. This affects concurrency ceilings and backoff policy:

| Tier | Meaning | Recommended `max_concurrency` |
|------|---------|-------------------------------|
| `pro` | Pro plan | 3–5 |
| `max` | Max plan (5× usage cap) | 10 |
| `max20x` | Max plan (20× usage cap) | 20 |

The config module does not enforce the concurrency limit based on tier; it only stores the tier value. The runner reads `config.subscription_tier` and `config.max_concurrency` separately and applies its own logic.

---

## 3. Resolution Order

### 3.1 Full Priority Diagram

```
Priority (high to low)
┌────────────────────────────────────────────────────────────┐
│  1. CLI flags                                              │
│     Passed as __init__ kwargs by cli.py to load_config()  │
│     Example: --max-budget 3.00  →  max_budget_usd=3.0     │
├────────────────────────────────────────────────────────────┤
│  2. Environment variables                                  │
│     CONDUCTOR_MAX_BUDGET_USD, ANTHROPIC_API_KEY, etc.      │
│     Pydantic-settings reads os.environ at construction     │
├────────────────────────────────────────────────────────────┤
│  3. .env file                                              │
│     Loaded from the process's CWD by pydantic-settings     │
│     via python-dotenv; does NOT override live env vars     │
│     (dotenv convention: env var wins over .env file)       │
├────────────────────────────────────────────────────────────┤
│  4. Field defaults                                         │
│     Hardcoded in the Config class definition               │
└────────────────────────────────────────────────────────────┘
```

The `.env` file is loaded only if present. Its absence is not an error.

### 3.2 `CLAUDE_CONFIG_DIR` for Subprocess Isolation

Each `claude -p` subprocess must receive its own `CLAUDE_CONFIG_DIR` pointing to an ephemeral temp directory. This prevents:
- Cross-contamination of Claude Code session state between concurrent sub-agents
- Sub-agents reading or writing to the operator's personal `~/.claude/` directory
- `apiKeyHelper` fallback to the operator's stored credentials if the key is absent from the env dict

The `Config` model does NOT store `CLAUDE_CONFIG_DIR`. Instead, `build_subprocess_env` generates a fresh temp directory per call and includes it in the returned dict. The temp directory is the caller's responsibility to clean up after the subprocess exits.

```python
# Pseudocode: where CLAUDE_CONFIG_DIR comes from
import tempfile

def build_subprocess_env(config: Config, extra: dict = {}) -> dict:
    tmp_config_dir = tempfile.mkdtemp(prefix="composer-claude-config-")
    # ... (see Section 4 for full dict)
    env["CLAUDE_CONFIG_DIR"] = tmp_config_dir
    return env
```

### 3.3 CLAUDE.md Location Algorithm

When `claude -p` starts in a given `cwd`, it discovers CLAUDE.md files by walking the directory tree. The effective load order (from most general to most specific, with the more specific winning on conflict) is:

| Priority | Location | Notes |
|----------|----------|-------|
| 1 (lowest) | Managed policy: `/Library/Application Support/ClaudeCode/CLAUDE.md` (macOS) | Cannot be excluded; loaded before everything else |
| 2 | User: `~/.claude/CLAUDE.md` | Always loaded; operator's global instructions |
| 3 | Ancestor directories: `{cwd}/../../CLAUDE.md`, `{cwd}/../CLAUDE.md` | Loaded if present; depends on directory tree above the worktree |
| 4 | Project root: `{cwd}/CLAUDE.md` or `{cwd}/.claude/CLAUDE.md` | The target repo's shared instructions |
| 5 (highest) | Local project: `{cwd}/CLAUDE.local.md` | Gitignored; machine-local overrides |

**Key implication for sub-agents in worktrees:** A sub-agent launched with `cwd=.claude/worktrees/7-my-feature/` will walk up through the worktree into the conductor repo root and load the conductor's own `CLAUDE.md`. This is usually correct behavior (the conductor's CLAUDE.md contains the protocol rules). If the sub-agent is working on a different repository checked out inside the worktree, use `claudeMdExcludes` in `.claude/settings.json` to suppress unwanted ancestor CLAUDE.md files.

**CLAUDE.md not found:** If no CLAUDE.md is found at the project scope, Claude Code proceeds without project-level instructions. This is a warning condition, not a fatal error. The config module does not check for CLAUDE.md existence; that is the runner's concern (see Section 6).

---

## 4. Subprocess Env Dict Construction

### 4.1 Function Signature

```python
def build_subprocess_env(
    config: Config,
    extra: dict[str, str] = {},
) -> dict[str, str]:
    """
    Construct a sanitized environment dictionary for a claude -p subprocess.

    The returned dict is fully self-contained: the subprocess receives only
    what is in this dict. No variables are inherited from the parent process
    (os.environ is never passed directly to the child).

    Args:
        config:  The validated Config instance for this conductor session.
        extra:   Additional variables to merge in after the base dict is built.
                 Values in `extra` override corresponding keys in the base dict.
                 Use this for per-dispatch overrides (e.g., CLAUDE_CONFIG_DIR
                 if the caller manages the temp dir lifecycle themselves).

    Returns:
        A dict[str, str] suitable for passing as the `env` kwarg to
        subprocess.Popen or subprocess.run.

    Side effects:
        Creates a temporary directory for CLAUDE_CONFIG_DIR unless overridden
        by `extra`. The caller is responsible for deleting this directory after
        the subprocess exits.
    """
```

### 4.2 Variables INCLUDED in the Returned Dict

The following keys are always present unless overridden by `extra`:

| Key | Value Source | Notes |
|-----|-------------|-------|
| `PATH` | Copied from `os.environ["PATH"]` | Required for `claude`, `git`, `gh`, `uv` to be found |
| `HOME` | Copied from `os.environ["HOME"]` | Required by git, gh, and various shell tools |
| `USER` | Copied from `os.environ.get("USER", "")` | Some tools need it; harmless |
| `SHELL` | Copied from `os.environ.get("SHELL", "/bin/bash")` | Needed for subprocess shell invocations |
| `LANG` | Copied from `os.environ.get("LANG", "en_US.UTF-8")` | Prevents locale errors in CLI tools |
| `LC_ALL` | Copied from `os.environ.get("LC_ALL", "")` | Same as LANG |
| `TERM` | `"dumb"` (hardcoded) | Prevents interactive terminal features in headless subprocesses |
| `GIT_AUTHOR_NAME` | Copied from `os.environ.get("GIT_AUTHOR_NAME", "")` | Required for git commits |
| `GIT_AUTHOR_EMAIL` | Copied from `os.environ.get("GIT_AUTHOR_EMAIL", "")` | Required for git commits |
| `GIT_COMMITTER_NAME` | Copied from `os.environ.get("GIT_COMMITTER_NAME", "")` | Required for git commits |
| `GIT_COMMITTER_EMAIL` | Copied from `os.environ.get("GIT_COMMITTER_EMAIL", "")` | Required for git commits |
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:{proxy_port}` | Points to credential proxy; see Section 5 |
| `CLAUDE_CONFIG_DIR` | `tempfile.mkdtemp(prefix="composer-claude-config-")` | Fresh per-call temp dir; prevents state leakage |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | `"1"` | Prevents cross-agent memory pollution in shared repo worktrees |
| `DISABLE_AUTOUPDATER` | `"1"` | Prevents update checks during headless runs |
| `DISABLE_ERROR_REPORTING` | `"1"` | Disables Sentry error reporting |
| `DISABLE_TELEMETRY` | `"1"` | Opts out of Statsig telemetry |
| `ENABLE_CLAUDEAI_MCP_SERVERS` | `"false"` | Prevents the operator's personal Claude.ai cloud MCPs from loading in sub-agents |
| `ENABLE_TOOL_SEARCH` | `"false"` | Prevents the MCPSearch tool from appearing when no MCPs are configured |
| `CLAUDECODE` | NOT SET — explicitly excluded | See Section 4.4 |

**Note on `ANTHROPIC_API_KEY`:** This variable is NOT included in the subprocess env dict. Authentication is handled entirely through the credential proxy (see Section 5). If the proxy is disabled (e.g., in test mode), `ANTHROPIC_API_KEY` must be added via `extra`.

**Note on `GITHUB_TOKEN` / `GH_TOKEN`:** These are NOT included in the base dict. The runner is responsible for credential injection via the git credential helper pattern (see Section 5 and `docs/research/17-credential-proxy.md`). If a scoped token must be passed for `gh` API calls, it is added via `extra`.

### 4.3 Variables EXCLUDED from the Returned Dict

The returned dict is constructed from an explicit allowlist. Everything not on the list is excluded. The following variables deserve explicit mention as intentionally excluded:

| Variable | Reason for Exclusion |
|----------|---------------------|
| `ANTHROPIC_API_KEY` | Kept in credential proxy only; sub-agent authenticates via `ANTHROPIC_BASE_URL` proxy |
| `ANTHROPIC_AUTH_TOKEN` | Same as above |
| `GITHUB_TOKEN` | Injected via git credential helper or scoped token in `extra` |
| `GH_TOKEN` | Same as above |
| `CLAUDECODE` | See Section 4.4: must never be set in the subprocess env |
| `AWS_*` | Unrelated; would be a credential leak |
| `GOOGLE_*` | Unrelated; would be a credential leak |
| `DATABASE_URL` / `DB_*` | Unrelated; would be a credential leak |
| `CONDUCTOR_*` | Conductor-internal configuration; sub-agents must not read the orchestrator's settings |
| `CLAUDE_CODE_SUBAGENT_MODEL` | Prevents sub-agents from spawning their own deeper sub-agents with a different model |
| `npm_*` / `NODE_*` | Node.js environment noise from the operator's shell; excluded to reduce attack surface |
| `SSH_AUTH_SOCK` / `SSH_AGENT_PID` | Prevent sub-agents from using the operator's SSH agent |

### 4.4 `CLAUDECODE=1` Nesting Guard

Claude Code sets `CLAUDECODE=1` in its own process environment when running. If the conductor itself is being run inside a Claude Code session (common during development and testing), this variable exists in the conductor's `os.environ`. If it were passed to a sub-agent subprocess, the `claude` binary would detect it and refuse to start with:

```
Error: Claude Code cannot be launched inside another Claude Code session.
```

**How the guard works:**

1. `build_subprocess_env` never includes `CLAUDECODE` in the returned dict (the allowlist approach ensures this automatically — `CLAUDECODE` is simply not on the list).
2. At startup, `load_config` (or the CLI entry point) checks for `CLAUDECODE` in `os.environ` and raises `OrchestratorNestingError` if found:

```python
def load_config(**cli_overrides) -> Config:
    if os.environ.get("CLAUDECODE") == "1":
        raise OrchestratorNestingError(
            "CLAUDECODE=1 detected in environment. "
            "Cannot nest orchestrator invocations. "
            "Run the conductor in a plain terminal, not inside a Claude Code session."
        )
    return Config(**cli_overrides)
```

This check applies to the conductor's own startup, not to the sub-agents. Sub-agents are launched with a clean env that excludes `CLAUDECODE`, so they start successfully.

**Summary table:**

| Scenario | `CLAUDECODE` in conductor env | `CLAUDECODE` in subprocess env | Outcome |
|----------|-------------------------------|-------------------------------|---------|
| Conductor run in plain terminal | Not set | Not included (allowlist) | Normal |
| Conductor run inside Claude Code session | `"1"` | Not included (allowlist) | `load_config` raises `OrchestratorNestingError` at startup |
| Sub-agent subprocess | Not applicable | Not included (allowlist) | Sub-agent starts normally |

### 4.5 Example: Returned Dict Shape

```python
# Representative output of build_subprocess_env(config)
{
    "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "HOME": "/Users/operator",
    "USER": "operator",
    "SHELL": "/bin/zsh",
    "LANG": "en_US.UTF-8",
    "LC_ALL": "en_US.UTF-8",
    "TERM": "dumb",
    "GIT_AUTHOR_NAME": "Operator Name",
    "GIT_AUTHOR_EMAIL": "operator@example.com",
    "GIT_COMMITTER_NAME": "Operator Name",
    "GIT_COMMITTER_EMAIL": "operator@example.com",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:51234",   # ephemeral proxy port
    "CLAUDE_CONFIG_DIR": "/tmp/composer-claude-config-a1b2c3d4",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_TELEMETRY": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "ENABLE_TOOL_SEARCH": "false",
    # NOT present: ANTHROPIC_API_KEY, GITHUB_TOKEN, CLAUDECODE, CONDUCTOR_*, AWS_*, etc.
}
```

---

## 5. Credential Proxy Pattern

### 5.1 Overview

The credential proxy is the mechanism that keeps `ANTHROPIC_API_KEY` out of the sub-agent subprocess environment. Anthropic explicitly recommends this pattern in the secure deployment documentation:

> "Rather than giving an agent direct access to an API key, you could run a proxy outside the agent's environment that injects the key into requests."

The proxy is an HTTP server running on `127.0.0.1` at an ephemeral port. It is started once per conductor session (not per sub-agent) and shared by all concurrent sub-agents. Each sub-agent's `ANTHROPIC_BASE_URL` points to the proxy.

### 5.2 How `apiKeyHelper` Works

Claude Code's `apiKeyHelper` mechanism provides an alternative credential delivery path:

1. A shell script path is configured in the session's `settings.json` under the `apiKeyHelper` key.
2. When Claude Code needs to authenticate, it invokes the script as a subprocess.
3. The script's stdout is used as the credential value (same as if it were `ANTHROPIC_API_KEY` in the env).
4. Claude Code calls the helper at session start and on 401 responses.

```json
{
  "apiKeyHelper": "/usr/local/bin/conductor-key-helper.sh"
}
```

Example helper script:
```bash
#!/bin/bash
# Fetch the key from the system keychain (macOS)
security find-generic-password -a conductor -s anthropic-api-key -w
```

**Security limitation of `apiKeyHelper`:** The key still enters the Claude Code process's memory after the helper runs. `apiKeyHelper` solves key rotation (the helper is called periodically to refresh the value) but does not keep the key entirely outside the subprocess. The full proxy (`ANTHROPIC_BASE_URL`) is the only mechanism that achieves true credential isolation.

### 5.3 ANTHROPIC_BASE_URL Proxy Mechanism

```
┌─────────────────────────────────────────────┐
│  Conductor process (holds real API key)      │
│                                              │
│  AnthropicCredentialProxy                    │
│    listening on http://127.0.0.1:PORT        │
│    _key = os.environ["ANTHROPIC_API_KEY"]    │
└──────────────────┬──────────────────────────┘
                   │ Strips incoming X-Api-Key
                   │ Injects real key
                   │ Forwards via TLS
                   ▼
            api.anthropic.com

┌─────────────────────────────────────────────┐
│  Sub-agent subprocess                        │
│  env: ANTHROPIC_BASE_URL=http://127.0.0.1:PORT │
│  env: ANTHROPIC_API_KEY — NOT SET            │
│  (if Bash(env) runs: key not visible)        │
└─────────────────────────────────────────────┘
```

**Protocol:** The sub-agent sends plaintext HTTP to the loopback address. TLS is not required for loopback traffic — inter-process communication on `127.0.0.1` does not leave the host. The proxy then makes a TLS connection outbound to `api.anthropic.com`.

**Port selection:** The proxy binds to an ephemeral port (pass `port=0` to the OS). The actual port is discovered after binding and passed to sub-agents via `ANTHROPIC_BASE_URL`.

**Streaming:** The proxy must handle SSE (Server-Sent Events) streaming, because `claude -p --output-format stream-json` uses it. The proxy must stream response chunks back incrementally rather than buffering the entire response. See `docs/research/43-anthropic-key-proxy.md` for the `aiohttp`-based async implementation sketch.

**Lifecycle:** The proxy starts before the first sub-agent is dispatched and stops when the conductor session ends. Key rotation is handled by calling `proxy.rotate_key(new_key)` without restarting sub-agents.

### 5.4 When to Use Direct Env Var vs. Proxy

| Scenario | Recommended approach |
|----------|---------------------|
| Single sub-agent, development/test only | Direct `ANTHROPIC_API_KEY` in `extra` dict is acceptable |
| Multiple concurrent sub-agents in production | Credential proxy (`ANTHROPIC_BASE_URL`) — required |
| CI/CD deployment | Credential proxy — required |
| Key rotation needed (tokens expire) | Credential proxy with `proxy.rotate_key()` |
| Offline / local LLM testing | Direct env var or `ANTHROPIC_BASE_URL` pointing to local LLM |

### 5.5 GitHub Token Isolation

`GITHUB_TOKEN` is handled separately from the Anthropic key. Because `gh` CLI has no analog of `ANTHROPIC_BASE_URL`, the isolation strategy for GitHub credentials is layered:

**Layer 1 — Git operations (push/fetch):** Inject a shell-snippet git credential helper via `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_0` / `GIT_CONFIG_VALUE_0` env vars. The helper script reads the token from a temp file. `GH_TOKEN` and `GITHUB_TOKEN` are NOT set in the agent env. The agent's git operations authenticate without the token appearing in the process env.

**Layer 2 — gh CLI API operations (pr create, issue view, etc.):** A scoped fine-grained PAT or GitHub App installation token (1-hour TTL) is provided via `GH_TOKEN` in `extra`. This token is visible via `Bash(env)`, but its scope is limited to one repo and the blast radius if exfiltrated is bounded.

**Layer 3 — gh config dir isolation:** `GH_CONFIG_DIR` is set to an empty temp dir so the operator's stored credentials (from `gh auth login`) are not accessible to the sub-agent.

The runner (not the config module) is responsible for constructing and cleaning up the credential helper temp files. The config module's `build_subprocess_env` sets `GH_CONFIG_DIR` in the base dict.

Reference: `docs/research/17-credential-proxy.md` sections 3–7 for implementation details.

---

## 6. Error Cases

### 6.1 Missing Required Env Var

**Trigger:** `ANTHROPIC_API_KEY` or `GITHUB_TOKEN` is absent from the environment, `.env` file, and CLI args.

**Behavior:** Pydantic-settings raises `pydantic.ValidationError` during `Config()` construction. The `load_config` wrapper catches this and re-raises a human-readable `ConfigurationError`:

```
ConfigurationError: Missing required environment variable: ANTHROPIC_API_KEY
  Set it in your shell:  export ANTHROPIC_API_KEY=sk-ant-...
  Or add it to a .env file in the project root.
```

The error message includes the exact variable name that is missing, a one-line resolution hint, and no stack trace (the stack trace is logged at DEBUG level).

### 6.2 Invalid Type or Value

**Trigger:** An env var exists but has an invalid value (e.g., `CONDUCTOR_MAX_BUDGET_USD=abc`, `CONDUCTOR_SUBSCRIPTION_TIER=enterprise`).

**Behavior:** Pydantic raises `ValidationError` with field-level detail. The `load_config` wrapper reformats it:

```
ConfigurationError: Invalid value for CONDUCTOR_MAX_BUDGET_USD: 'abc'
  Expected: float (e.g., 5.00)
  Got: 'abc'
  Validation error: Input should be a valid number, unable to parse string as a number
```

For enum fields like `subscription_tier`:
```
ConfigurationError: Invalid value for CONDUCTOR_SUBSCRIPTION_TIER: 'enterprise'
  Expected one of: pro, max, max20x
  Got: 'enterprise'
```

### 6.3 CLAUDE.md Not Found

**Trigger:** When the runner is about to spawn a sub-agent, it checks whether the target worktree contains a project-level CLAUDE.md.

**Behavior:** This is a warning, not an error. The sub-agent proceeds without project-level CLAUDE.md context. The conductor logs:

```
WARNING: No CLAUDE.md found in worktree {path}. Sub-agent will run without project instructions.
         Expected: {path}/CLAUDE.md or {path}/.claude/CLAUDE.md
```

This check is performed by the runner, not the config module. The config module has no knowledge of worktree paths.

### 6.4 `CLAUDECODE=1` Detected at Startup

**Trigger:** `load_config()` finds `CLAUDECODE=1` in `os.environ`.

**Behavior:** Raises `OrchestratorNestingError` before any other work is done:

```
OrchestratorNestingError: Cannot nest orchestrator invocations.

CLAUDECODE=1 is set in the current environment, which means this process is
already running inside a Claude Code session.

To run the conductor, open a plain terminal (not a Claude Code session) and
invoke it from there. If you need to test the conductor from within Claude
Code, use a sub-shell that unsets CLAUDECODE:

    (unset CLAUDECODE && composer impl-worker)
```

The error exits with code 1. No partial initialization occurs.

### 6.5 `.env` File Not Found

**Behavior:** Silently ignored. The `.env` file is optional. Pydantic-settings does not raise an error when the file is absent — it simply skips that source and falls back to environment variables and defaults.

### 6.6 Error Hierarchy

```
ComposerError                     (base for all composer exceptions)
├── ConfigurationError            (Section 6.1 and 6.2: missing or invalid config)
└── OrchestratorNestingError      (Section 6.4: CLAUDECODE=1 detected)
```

These exception classes are defined in `config.py` so that `cli.py` and `runner.py` can import and catch them without circular dependencies.

---

## 7. Interface Summary

### 7.1 Public API

```python
# ── Exceptions ──────────────────────────────────────────────────
class ComposerError(Exception): ...
class ConfigurationError(ComposerError): ...
class OrchestratorNestingError(ComposerError): ...

# ── Settings model ──────────────────────────────────────────────
class Config(BaseSettings):
    # Fields (see Section 2)
    anthropic_api_key: str
    github_token: str
    max_budget_usd: float
    max_retries: int
    max_concurrency: int
    backoff_base_seconds: float
    backoff_max_minutes: float
    agent_timeout_minutes: float
    subscription_tier: str          # "pro" | "max" | "max20x"
    log_dir: Path
    checkpoint_dir: Path

    # Derived properties
    @property sessions_dir: Path    # log_dir / "sessions"
    @property cost_ledger: Path     # log_dir / "cost.jsonl"

# ── Factory ─────────────────────────────────────────────────────
def load_config(**cli_overrides: Any) -> Config:
    """
    Validate configuration and return a Config instance.
    Raises OrchestratorNestingError if CLAUDECODE=1 is set.
    Raises ConfigurationError if required vars are missing or invalid.
    """

# ── Subprocess env builder ───────────────────────────────────────
def build_subprocess_env(
    config: Config,
    extra: dict[str, str] = {},
) -> dict[str, str]:
    """
    Return a sanitized environment dict for claude -p subprocesses.
    Creates a fresh CLAUDE_CONFIG_DIR temp dir (caller must delete it).
    ANTHROPIC_API_KEY is never included; auth goes through the proxy.
    """
```

### 7.2 Consumer Map

| Consumer | What it gets from `config` |
|----------|---------------------------|
| `cli.py` | Calls `load_config(**cli_overrides)`. Uses `config` to display health info and pass settings to workers. Catches `ConfigurationError` and `OrchestratorNestingError` to show friendly error messages and exit(1). |
| `runner.py` | Receives a `Config` instance. Uses `config.max_budget_usd`, `config.max_retries`, `config.max_concurrency`, `config.backoff_base_seconds`, `config.backoff_max_minutes`, `config.agent_timeout_minutes`, and `config.subscription_tier` to control dispatch behavior. Calls `build_subprocess_env(config, extra={...})` for each sub-agent launch. |
| `health.py` | Receives a `Config` instance. Checks `config.anthropic_api_key` (non-empty) and `config.github_token` (non-empty). Verifies connectivity to the credential proxy using `config.anthropic_api_key` to start a proxy and test it. |
| `logger.py` | Receives `config.log_dir` and derived `config.sessions_dir` and `config.cost_ledger`. Does not access credential fields. |
| `session.py` | Receives `config.checkpoint_dir` for checkpoint read/write paths. Does not access credential fields. |

### 7.3 Import Pattern

Consumers import only the symbols they need:

```python
# Minimal import for most consumers
from composer.config import Config, load_config

# Full import for the runner (needs subprocess env builder)
from composer.config import Config, load_config, build_subprocess_env

# Exception handling at the CLI layer
from composer.config import ConfigurationError, OrchestratorNestingError
```

The `Config` class should not be instantiated directly by consumers; they should always call `load_config()` to get the nesting guard and validation error formatting.

---

## Cross-References

- `docs/research/04-configuration.md` — Resolution order, pydantic-settings implementation pattern, `CLAUDE_CONFIG_DIR` isolation rationale, `CLAUDECODE=1` nesting issue
- `docs/research/06-security-threat-model.md` — T3 credential exposure, `build_agent_env()` allowlist approach, `CLAUDECODE=1` filter requirement
- `docs/research/10-settings-mcp-injection.md` — `CLAUDE_CONFIG_DIR` + `--settings` combined isolation; `ENABLE_CLAUDEAI_MCP_SERVERS` and `ENABLE_TOOL_SEARCH` env vars
- `docs/research/17-credential-proxy.md` — GitHub token isolation via git credential helper and `GH_CONFIG_DIR`
- `docs/research/43-anthropic-key-proxy.md` — `ANTHROPIC_BASE_URL` proxy mechanism, `apiKeyHelper` semantics, CVE-2026-21852 interaction, streaming proxy implementation
