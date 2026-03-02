# Research: API Key Rotation and Credential Refresh in Long-Running Sub-Agent Sessions

**Issue:** #11
**Milestone:** M1: Foundation
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Overview](#overview)
2. [apiKeyHelper: Mechanics and Configuration](#apikeyhelper-mechanics-and-configuration)
   - [What apiKeyHelper Is](#what-apikeyhelper-is)
   - [Configuration Format](#configuration-format)
   - [Refresh Trigger Logic](#refresh-trigger-logic)
   - [Known Bug: TTL Caching Does Not Work](#known-bug-ttl-caching-does-not-work)
   - [What Credentials apiKeyHelper Applies To](#what-credentials-apikeyhelper-applies-to)
3. [Credential Lifecycle Concerns for Long Conductor Sessions](#credential-lifecycle-concerns-for-long-conductor-sessions)
   - [Long-Lived ANTHROPIC_API_KEY (Anthropic Direct)](#long-lived-anthropic_api_key-anthropic-direct)
   - [Short-Lived Tokens: OAuth and SSO Environments](#short-lived-tokens-oauth-and-sso-environments)
   - [Short-Lived Cloud Provider Credentials](#short-lived-cloud-provider-credentials)
   - [Concurrent Session Credential Conflicts](#concurrent-session-credential-conflicts)
4. [Recommended Credential Passing Strategy](#recommended-credential-passing-strategy)
   - [Option A: Environment Dict (Primary Recommendation)](#option-a-environment-dict-primary-recommendation)
   - [Option B: apiKeyHelper via --settings (Secondary)](#option-b-apikeyhelper-via---settings-secondary)
   - [Option C: Temp File (Not Recommended)](#option-c-temp-file-not-recommended)
   - [Comparison Table](#comparison-table)
5. [Credential Isolation Between Concurrent Subprocesses](#credential-isolation-between-concurrent-subprocesses)
6. [Detection and Response When a Credential Becomes Invalid Mid-Session](#detection-and-response-when-a-credential-becomes-invalid-mid-session)
   - [How 401/403 Errors Surface in stream-json Output](#how-401403-errors-surface-in-stream-json-output)
   - [Failure Modes by Credential Type](#failure-modes-by-credential-type)
   - [Conductor Response Protocol](#conductor-response-protocol)
7. [Conductor-Level Key Refresh Loop](#conductor-level-key-refresh-loop)
8. [Security Implications of Credential Injection Approaches](#security-implications-of-credential-injection-approaches)
9. [Cross-References](#cross-references)
10. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
11. [Sources](#sources)

---

## Overview

This document answers the core question from issue #11: **what is the credential lifecycle concern when the conductor runs for multi-hour sessions dispatching 20+ sub-agents, and what is the correct strategy for credential passing and rotation?**

The short summary:

- **Standard `ANTHROPIC_API_KEY` (Anthropic direct API)**: Long-lived, does not expire automatically. Key rotation is an operational concern, not a TTL concern. The primary risk is key revocation after a suspected leak, not automatic expiry.
- **OAuth tokens (`CLAUDE_CODE_OAUTH_TOKEN`)**: Short-lived (~10–15 minutes). OAuth token refresh is **broken in headless `-p` mode** (Issue #28827, confirmed March 2026). Using OAuth auth for conductor sub-agents is not viable.
- **Cloud provider short-lived credentials (Bedrock STS, Vertex SA tokens)**: Expire on organization-defined schedules (typically 1–8 hours). Claude Code does **not** automatically refresh these mid-session (Issue #2280). The `apiKeyHelper` mechanism is the recommended solution for this case.
- **`apiKeyHelper`**: A shell script configured in `settings.json` that Claude Code calls to fetch a fresh credential. The default refresh interval is 5 minutes or on HTTP 401. The configured TTL (`CLAUDE_CODE_API_KEY_HELPER_TTL_MS`) has a **known non-functional caching bug** (Issue #11639). For conductor's sub-agents, `apiKeyHelper` must be injected per-agent via `--settings`, not via the global `~/.claude/settings.json`.

The **recommended strategy for conductor** is to pass credentials via an explicit `env` dict to each subprocess at dispatch time, using long-lived `ANTHROPIC_API_KEY` values, and to implement a conductor-level refresh loop that re-reads the key from the host environment before each sub-agent dispatch rather than relying on `apiKeyHelper` in-process.

---

## apiKeyHelper: Mechanics and Configuration

### What apiKeyHelper Is

`apiKeyHelper` is a setting in Claude Code's `settings.json` that points to an executable shell script. When set, Claude Code executes this script in `/bin/sh` and uses its stdout as the credential for all Anthropic API requests. The returned value is sent as both:

- `X-Api-Key` header (for direct Anthropic API key authentication)
- `Authorization: Bearer <value>` header (for OAuth-style tokens)

This mechanism exists to support environments where credentials:
- Come from a secrets vault (AWS Secrets Manager, 1Password, HashiCorp Vault)
- Rotate automatically on a schedule
- Are issued per-session by an IAM system
- Are short-lived and require a helper process to generate/refresh them

### Configuration Format

`apiKeyHelper` is configured in `settings.json`:

```json
{
  "apiKeyHelper": "/path/to/credential-helper.sh"
}
```

The value is a path to an executable script. The script must:
1. Print the credential value to stdout
2. Exit with code 0 on success

Example helper script for fetching from a secrets manager:

```bash
#!/bin/sh
# /usr/local/bin/fetch-anthropic-key.sh
aws secretsmanager get-secret-value \
  --secret-id prod/anthropic-api-key \
  --query SecretString \
  --output text
```

Example for 1Password:

```bash
#!/bin/sh
op read --no-newline "op://MyVault/Anthropic/credential"
```

Example for a simple static key (useful in development/CI):

```bash
#!/bin/sh
echo "$ANTHROPIC_API_KEY"
```

The script executes with the same environment variables available to Claude Code hooks. This means environment variables set in the subprocess `env` dict (including `ANTHROPIC_API_KEY`) are accessible within the helper script itself.

### Refresh Trigger Logic

By default, `apiKeyHelper` is invoked:
1. **At session start** — before the first API request
2. **After 5 minutes** (300,000 ms) — a default TTL for credential caching
3. **On HTTP 401 response** — triggered automatically on authentication failure

The TTL can be customized via the `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` environment variable:

```bash
# Refresh every 30 minutes
export CLAUDE_CODE_API_KEY_HELPER_TTL_MS=1800000

# Refresh every 5 minutes (default)
export CLAUDE_CODE_API_KEY_HELPER_TTL_MS=300000
```

### Known Bug: TTL Caching Does Not Work

**Issue #11639** (closed November 2025 as duplicate of #7660) confirms that `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` does not work as documented. The helper script is called on **nearly every API request** (approximately every 5 minutes) regardless of the configured TTL value. Evidence from the bug report shows distinct PIDs on every invocation, indicating no in-process caching of the returned credential.

**Practical impact for conductor:**
- If the `apiKeyHelper` script calls an external API (vault, AWS Secrets Manager, 1Password), it will be called very frequently — potentially hundreds of times per sub-agent session.
- If the external secret store has rate limits, this can trigger throttling.
- If the helper script is expensive (auth network call), the sub-agent's performance degrades significantly.

**Workaround:** Implement caching **within** the helper script itself, using a local cache file with its own TTL check:

```bash
#!/bin/sh
# Cached credential helper — works around TTL bug in Claude Code
CACHE_FILE="$HOME/.claude/.api-key-cache"
CACHE_TTL=3600  # 1 hour in seconds

if [ -f "$CACHE_FILE" ]; then
    CACHE_AGE=$(( $(date +%s) - $(stat -f %m "$CACHE_FILE" 2>/dev/null || stat -c %Y "$CACHE_FILE") ))
    if [ "$CACHE_AGE" -lt "$CACHE_TTL" ]; then
        cat "$CACHE_FILE"
        exit 0
    fi
fi

# Fetch fresh credential
NEW_KEY=$(aws secretsmanager get-secret-value \
    --secret-id prod/anthropic-api-key \
    --query SecretString \
    --output text)

# Cache it
printf '%s' "$NEW_KEY" > "$CACHE_FILE"
chmod 600 "$CACHE_FILE"
printf '%s' "$NEW_KEY"
```

**Security note:** The cache file on disk is a credential at rest. Restrict its permissions to `0600` and place it in a directory only readable by the conductor's OS user. For concurrent sub-agents, use per-agent cache files rather than a shared one to avoid race conditions.

### What Credentials apiKeyHelper Applies To

`apiKeyHelper` applies to the **Anthropic direct API** (`api.anthropic.com`). It sets both the `X-Api-Key` and `Authorization: Bearer` headers. Based on the bug report pattern and documentation:

- It supports standard `ANTHROPIC_API_KEY` values (direct API keys from the Anthropic Console)
- It supports `ANTHROPIC_AUTH_TOKEN` OAuth token values (for users authenticated via Claude.ai accounts)
- For **Amazon Bedrock** (`CLAUDE_CODE_USE_BEDROCK=1`), a separate `awsAuthHelper` mechanism was proposed (Issue #2280) but as of March 2026 has not shipped. The existing `apiKeyHelper` does not apply to Bedrock authentication.
- For **Google Vertex AI**, the credential mechanism uses ADC (Application Default Credentials) and is not managed by `apiKeyHelper`.

**Conflict with static env var:** Issue #11587 documents a bug where setting both `CLAUDE_CODE_OAUTH_TOKEN` (in env) and `apiKeyHelper` (in settings) causes an authentication conflict error. Only one authentication mechanism should be active at a time.

---

## Credential Lifecycle Concerns for Long Conductor Sessions

### Long-Lived ANTHROPIC_API_KEY (Anthropic Direct)

For teams using direct Anthropic API keys (from console.anthropic.com):

- **API keys do not expire automatically.** An `ANTHROPIC_API_KEY` starting with `sk-ant-` remains valid indefinitely until manually revoked.
- **Rotation concern:** Key rotation is an operational process (rotating because of a suspected leak, organizational policy, or key compromise), not a session-duration concern.
- **For a 4-hour conductor run with 20 sub-agents:** No credential lifecycle concern as long as the key is not revoked mid-run.
- **If a key is revoked:** All subsequent API calls fail with `401 authentication_error`. This is immediate and affects all in-flight sub-agents simultaneously.

**Conclusion for conductor:** With direct API keys, the credential lifecycle concern is:
1. Key revocation during a run (emergency response scenario, not normal operation)
2. Key leakage via subprocess environment (security concern, see `06-security-threat-model.md` T3)

Not: key expiry, TTL, or automatic rotation.

### Short-Lived Tokens: OAuth and SSO Environments

For teams using **OAuth tokens** (`CLAUDE_CODE_OAUTH_TOKEN`) via Claude.ai account authentication:

- OAuth access tokens expire after approximately **10–15 minutes**.
- Claude Code's interactive mode automatically refreshes OAuth tokens using a stored refresh token.
- **In headless `-p` mode, OAuth token refresh is broken** (Issue #28827, closed March 1, 2026 as duplicate of #12447). The CLI does not attempt to refresh the OAuth token during a non-interactive session.
- Sub-agents running for 30+ minutes will fail mid-session with `401 OAuth token has expired`.

**Conclusion for conductor:** OAuth-based authentication (`CLAUDE_CODE_OAUTH_TOKEN`) is **not suitable for sub-agent subprocess spawning** in long-running conductor sessions. The conductor must use static `ANTHROPIC_API_KEY` values from the Anthropic Console or from a cloud provider API key.

**If the operator's organization uses Claude for Enterprise with SSO:** The Console generates API keys that are long-lived (not OAuth tokens). These are safe to use in sub-agent processes. Operators should generate a dedicated API key from the Console for conductor's use rather than relying on their personal OAuth session.

### Short-Lived Cloud Provider Credentials

For teams using **Amazon Bedrock** (`CLAUDE_CODE_USE_BEDROCK=1`) or **Google Vertex AI** (`CLOUD_ML_REGION`, `ANTHROPIC_VERTEX_PROJECT_ID`):

- AWS STS tokens (from SSO, SAML, or assumed roles) typically have a 1–8 hour TTL.
- GCP service account access tokens have a 1-hour default TTL.
- Claude Code does **not** automatically detect credential expiry and re-request from the provider (Issue #2280, confirmed for Bedrock).
- When credentials expire mid-session, Claude Code continues failing with `403 security token expired` or equivalent, with no auto-recovery.

**For Bedrock sessions, the recommended approach is `apiKeyHelper`** (Issue #3038 confirms this is the intended mechanism, even though Bedrock-specific `awsAuthHelper` support is not yet implemented):

```json
{
  "apiKeyHelper": "/usr/local/bin/refresh-aws-creds.sh"
}
```

Where the script re-runs `aws sts assume-role` or `aws sso login` to generate a fresh token and outputs it.

**Important caveat:** As noted in section 2.3, the TTL bug means the helper is called very frequently. For AWS SSO, this could trigger MFA prompts in interactive environments — use a fully non-interactive credential refresh script (e.g., using `aws configure export-credentials` with a pre-authenticated profile).

### Concurrent Session Credential Conflicts

Issue #28207 documents that when multiple Claude Code sessions run concurrently (particularly in tmux or parallel subprocess environments), they can interfere with each other's credential state. Specific patterns:

- Sessions share OAuth credential storage in `$CLAUDE_CONFIG_DIR/auth.json` (or `~/.claude/auth.json` if `CLAUDE_CONFIG_DIR` is not isolated)
- If one session triggers an OAuth refresh and writes the new token, other sessions may be reading the old token simultaneously
- With `CLAUDE_CONFIG_DIR` isolation (each subprocess gets its own temp dir), this conflict is eliminated: each agent has an isolated credential store

**The `CLAUDE_CONFIG_DIR` isolation pattern documented in `04-configuration.md` section 5.4 also resolves concurrent credential conflicts.** When each sub-agent uses a distinct `CLAUDE_CONFIG_DIR`, there is no shared credential file for concurrent writes to corrupt.

---

## Recommended Credential Passing Strategy

### Option A: Environment Dict (Primary Recommendation)

Pass `ANTHROPIC_API_KEY` as an explicit entry in the subprocess `env` dict at dispatch time:

```python
import os

def build_agent_env(base_env: dict) -> dict:
    """
    Build a clean env dict for a sub-agent subprocess.
    Always reads ANTHROPIC_API_KEY fresh from the conductor's own environment
    at dispatch time, not from a stale value cached at conductor startup.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in conductor environment")

    return {
        "ANTHROPIC_API_KEY": api_key,
        "PATH": base_env.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": base_env.get("HOME", ""),
        "LANG": base_env.get("LANG", "en_US.UTF-8"),
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        # NEVER include: CLAUDECODE, ANTHROPIC_AUTH_TOKEN, GITHUB_TOKEN, AWS_*
    }
```

**Why read fresh at dispatch time:** If the conductor itself is long-running (multi-hour orchestration loop), `os.environ["ANTHROPIC_API_KEY"]` reads the current value each time `build_agent_env` is called. If an operator rotates the key in the host environment (via `export ANTHROPIC_API_KEY=new-key` in another terminal, or via a secrets manager that updates the process environment), each new sub-agent dispatch picks up the latest value without the conductor needing to restart.

**Caveat:** This only works if the conductor's own environment is updated. On most Unix systems, environment variable updates in one process do not propagate to already-running parent processes. For live rotation, the conductor needs to be integrated with a secret-watcher pattern (see section 7).

### Option B: apiKeyHelper via --settings (Secondary)

For organizations with rotating credentials (cloud providers, vault-backed keys), inject `apiKeyHelper` into the sub-agent via the per-agent `--settings` file:

```python
import json, tempfile, os

def build_agent_settings_with_helper(
    allowed_tools: list[str],
    denied_tools: list[str],
    api_key_helper_path: str | None = None,
) -> str:
    """Write a temporary settings JSON with optional apiKeyHelper."""
    settings: dict = {
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
    if api_key_helper_path:
        settings["apiKeyHelper"] = api_key_helper_path

    f = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="conductor-agent-settings-",
        delete=False,
    )
    json.dump(settings, f)
    f.close()
    return f.name
```

The `apiKeyHelper` script path can be configured globally (all agents share the same helper) or per-agent-type (different helper scripts for issue-workers vs. research-workers).

**When to use this approach:** When `ANTHROPIC_API_KEY` is not a static long-lived key but is fetched from a secrets manager per-run, and when the helper script supports fast non-interactive execution.

**When not to use this approach:** When the helper script involves interactive auth (1Password desktop prompt, MFA challenge) — these will block in headless `-p` mode. When the secrets manager has strict rate limits that would be exceeded by the TTL bug calling the helper every 5 minutes.

Note: Because `--settings` merges with project settings (see `10-settings-mcp-injection.md` section 1.3), combining it with `CLAUDE_CONFIG_DIR` isolation (empty temp dir) ensures the `apiKeyHelper` in `--settings` is the only credential mechanism in effect.

### Option C: Temp File (Not Recommended)

Writing the API key to a temp file and passing the path to the subprocess is a recognized antipattern:

- The file is a credential at rest on disk
- Cleanup on subprocess crash is unreliable
- World-readable `/tmp` on some systems exposes the file
- Provides no benefit over env dict injection

Do not use this approach.

### Comparison Table

| Approach | TTL Awareness | Concurrent Agent Safety | Secrets Vault Integration | Risk |
|----------|--------------|------------------------|--------------------------|------|
| Env dict (read at dispatch) | Manual (conductor re-reads) | Safe with `CLAUDE_CONFIG_DIR` isolation | Manual (conductor reads from vault at dispatch) | Low |
| `apiKeyHelper` via `--settings` | Automatic (5-min or 401) | Safe (per-agent settings file) | Native (script calls vault directly) | Medium (TTL bug, rate limits) |
| `apiKeyHelper` in global `~/.claude/settings.json` | Automatic | **Unsafe** (shared across all agents) | Native | High (shared state) |
| Temp credential file | None | Unsafe (race conditions) | Manual | High |
| Inherit full parent env | None | **Unsafe** (see T3 in 06-security-threat-model.md) | N/A | Critical |

---

## Credential Isolation Between Concurrent Subprocesses

When 20 sub-agents run simultaneously, credential isolation matters for two reasons:

1. **Security:** No sub-agent should be able to read another sub-agent's credentials or the conductor's own credentials beyond what it was explicitly given.
2. **Stability:** No credential operation by one agent (OAuth refresh, `apiKeyHelper` call) should affect another agent's in-flight requests.

**The recommended isolation model combines three patterns from `04-configuration.md` sections 5.4 and 6.1:**

```python
import tempfile, os, subprocess

def spawn_sub_agent(prompt: str, worktree_path: str, settings_path: str) -> subprocess.Popen:
    """
    Spawn a Claude Code sub-agent with full credential isolation.
    Each agent gets:
    - Its own CLAUDE_CONFIG_DIR (no shared credential store)
    - An explicit, scrubbed env dict (no inherited parent secrets)
    - Its own --settings file (no shared apiKeyHelper invocations)
    """
    with tempfile.TemporaryDirectory() as tmp_config:
        agent_env = build_agent_env(os.environ)
        agent_env["CLAUDE_CONFIG_DIR"] = tmp_config

        return subprocess.Popen(
            [
                "claude", "-p",
                "--settings", settings_path,
                "--setting-sources", "",  # prevent merging user settings
                "--dangerously-skip-permissions",
                "--output-format", "stream-json",
                "--max-turns", "100",
                prompt,
            ],
            cwd=worktree_path,
            env=agent_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
```

**What this achieves:**

| Isolation Concern | Mechanism | Effect |
|------------------|-----------|--------|
| Shared OAuth token file | Per-agent `CLAUDE_CONFIG_DIR` temp dir | Each agent has its own `auth.json`; no concurrent writes to shared file |
| Parent env secrets leaked | Scrubbed env dict via `build_agent_env()` | Sub-agents never see `GITHUB_TOKEN`, `AWS_*`, or other parent secrets |
| User `settings.json` inheritance | `--setting-sources ""` + `CLAUDE_CONFIG_DIR` isolation | No user-level `apiKeyHelper` bleeds in; only the explicit `--settings` is active |
| Cross-agent `apiKeyHelper` collisions | Per-agent `--settings` file with isolated `apiKeyHelper` | Each agent's helper runs independently; no shared cache file contention |

**`ANTHROPIC_API_KEY` in the scrubbed env dict:** Each agent receives the same `ANTHROPIC_API_KEY` value. This is acceptable because the key is the same for all agents — it is not a per-agent secret. The key is scoped to the Anthropic account, not to an individual agent session. Isolation is achieved by preventing *other* secrets from reaching agents, not by giving each agent a different API key.

**For environments with per-agent API key scoping:** If the organization's policy requires each agent to use a distinct API key (e.g., to track per-agent spend at the key level), the conductor can maintain a pool of API keys and assign one per agent invocation. This requires key pool management at the conductor level, not via `apiKeyHelper`.

---

## Detection and Response When a Credential Becomes Invalid Mid-Session

### How 401/403 Errors Surface in stream-json Output

When a sub-agent's credential becomes invalid during a running `--output-format stream-json` session, the error surfaces as a structured JSON event in the stdout stream:

```json
{
  "type": "assistant",
  "subtype": "error",
  "error": {
    "type": "authentication_error",
    "message": "Invalid API key provided."
  }
}
```

Or for OAuth token expiry:

```json
{
  "type": "assistant",
  "subtype": "error",
  "error": {
    "type": "authentication_error",
    "message": "OAuth token has expired. Please obtain a new token or refresh your existing token."
  }
}
```

**Critical caveat (parser-breaking output):** Issue #27182 and related bugs document that in some Claude Code versions and error paths, authentication errors are emitted as **non-JSON text** to stdout rather than structured JSON events. This produces a stream-json parse failure of the form:

```
Unexpected token 'A', "API Error: 4..." is not valid JSON
```

The conductor's stream-json parser must handle this gracefully — wrapping each line in a try/except for JSON parse errors and treating non-JSON lines as potential error signals.

### Failure Modes by Credential Type

| Credential Type | Expiry Behavior | Error Code | Claude Code Recovery |
|----------------|-----------------|------------|---------------------|
| `ANTHROPIC_API_KEY` (static) | Does not expire; only revoked | `401 authentication_error` | None automatic; sub-agent halts |
| OAuth `CLAUDE_CODE_OAUTH_TOKEN` | Expires ~10–15 min | `401 authentication_error` | **None in headless mode** (Issue #28827) |
| AWS STS token (Bedrock) | Expires per org policy (1–8h) | `403 security token expired` | None automatic (Issue #2280) |
| GCP SA token (Vertex) | Expires 1h | `401 UNAUTHENTICATED` | None automatic |
| `apiKeyHelper`-managed key | Refreshed on 401 or TTL | N/A (managed) | Automatic — new key fetched and request retried |

**Key insight:** `apiKeyHelper` is the **only** mechanism by which Claude Code internally recovers from credential expiry without process restart. For all other credential types, the sub-agent halts on authentication failure with no self-recovery.

### Conductor Response Protocol

When the conductor's stream-json reader detects an authentication error from a sub-agent:

```python
import json, subprocess

AUTH_ERROR_INDICATORS = [
    "authentication_error",
    "Invalid API key",
    "OAuth token has expired",
    "security token expired",
    "UNAUTHENTICATED",
]

def is_auth_error(event: dict) -> bool:
    """Return True if the stream-json event represents an auth failure."""
    if event.get("type") == "assistant" and event.get("subtype") == "error":
        error_msg = event.get("error", {}).get("message", "")
        return any(indicator in error_msg for indicator in AUTH_ERROR_INDICATORS)
    return False

def handle_auth_error(issue_number: int, branch: str, worktree_path: str) -> None:
    """
    Protocol when a sub-agent hits an auth error mid-session.

    Steps:
    1. Log the failure with session ID, issue number, and error message
    2. Terminate the sub-agent subprocess (it cannot recover itself)
    3. Check if the credential is still valid by making a direct test call
    4. If key is valid: transient error, re-dispatch the agent
    5. If key is invalid: escalate to operator and halt conductor
    """
    pass  # implementation detail
```

**Triage logic:**

1. **Was the key just rotated?** Check if `ANTHROPIC_API_KEY` in the conductor's current environment differs from what was passed to the failed sub-agent. If yes: the conductor needs to be restarted or the key needs to be re-injected.

2. **Is the error transient?** Make a minimal test API call from the conductor itself (not via `claude -p`) to verify the key is still valid. If the conductor's direct call succeeds, the sub-agent failure was transient (e.g., API gateway hiccup, not a credential issue). Re-dispatch.

3. **Is the key genuinely invalid?** If the conductor's own test call also fails with 401: the key has been revoked. Do not re-dispatch — re-dispatching in a loop with an invalid key burns rate limit retries and generates noise. Log the error, remove the `in-progress` label from all affected issues, and alert the operator.

4. **Was this an OAuth expiry?** If `CLAUDE_CODE_OAUTH_TOKEN` was used (not recommended for conductor): no in-process recovery is possible. The conductor must be restarted with a fresh OAuth token. This is why using OAuth tokens in conductor subprocesses is not recommended.

**Cross-reference:** The error handling and retry logic described here integrates with the issue #3 (Error Handling) research. Credential errors are non-retryable in the same way as 400-range client errors — the fix is to address the credential before retrying, not to retry blindly.

---

## Conductor-Level Key Refresh Loop

Rather than relying on `apiKeyHelper` inside sub-agents to manage credential freshness, the conductor can implement a **credential watcher** at the orchestrator level. This is the simpler and more auditable design:

```python
import os, time, threading

class CredentialWatcher:
    """
    Watches for credential changes in the host environment and/or a secrets
    manager, updating the conductor's internal state.

    This supports two refresh patterns:
    1. Live env var update: operator exports a new key in the host shell;
       conductor picks it up on next dispatch.
    2. Periodic poll: conductor polls a secrets manager on a schedule and
       updates an internal credential cache.
    """

    def __init__(self, secrets_source: callable | None = None, poll_interval: int = 3600):
        self._lock = threading.Lock()
        self._current_key: str | None = None
        self._secrets_source = secrets_source  # optional vault fetch function
        self._poll_interval = poll_interval

    def get_key(self) -> str:
        """Return the current credential. Always call this at sub-agent dispatch time."""
        with self._lock:
            if self._secrets_source:
                return self._current_key or os.environ["ANTHROPIC_API_KEY"]
            # No secrets source: read directly from env at call time
            return os.environ["ANTHROPIC_API_KEY"]

    def _refresh_loop(self) -> None:
        """Background thread: poll the secrets source on a schedule."""
        while True:
            time.sleep(self._poll_interval)
            try:
                new_key = self._secrets_source()
                with self._lock:
                    self._current_key = new_key
            except Exception as exc:
                # Log but do not crash — continue using existing key
                pass

    def start(self) -> None:
        if self._secrets_source:
            t = threading.Thread(target=self._refresh_loop, daemon=True)
            t.start()
```

Usage in the conductor's dispatch loop:

```python
# At conductor startup
watcher = CredentialWatcher(
    secrets_source=lambda: fetch_from_vault("prod/anthropic-api-key"),
    poll_interval=3600,  # refresh from vault every hour
)
watcher.start()

# At each sub-agent dispatch
def dispatch_agent(issue: dict, worktree_path: str) -> subprocess.Popen:
    env = build_agent_env({"ANTHROPIC_API_KEY": watcher.get_key()})
    # ... rest of dispatch
```

**Advantages over `apiKeyHelper`:**

- The conductor controls exactly when credentials are refreshed (predictable, auditable)
- No TTL bug to work around — the conductor manages TTL itself
- The helper script is a Python function, not a shell script that runs inside each sub-agent
- Concurrent sub-agents all use the latest key from a single authoritative source

**Disadvantages:**

- Does not handle key rotation that happens mid-way through an already-running sub-agent session (the sub-agent was spawned with the old key in its env dict)
- Requires manual implementation vs. the built-in `apiKeyHelper` mechanism

For conductor's use case (each sub-agent runs for 30–90 minutes, each is dispatched by the conductor), the stateless "read at dispatch time" pattern is sufficient for most organizations. Only organizations with sub-hour key rotation policies need the active background watcher.

---

## Security Implications of Credential Injection Approaches

The credential injection design directly impacts the threat surface documented in `06-security-threat-model.md`:

**T3 (Credential Exposure via Process Environment):** The env dict approach means `ANTHROPIC_API_KEY` is present in the sub-agent's process environment. An agent that runs `env` or `printenv` can read it. The mitigations are:

1. Block `Bash(env)` and `Bash(printenv)` in the `--allowedTools` denylist (see T3 mitigations in `06-security-threat-model.md`)
2. Use the credential proxy pattern (see R-SEC-A in `06-security-threat-model.md`) to avoid putting the real key in the subprocess env at all. A local proxy intercepts Anthropic API calls, injects the key, and proxies them — the sub-agent never holds the actual key.

**T3 and the `apiKeyHelper` interaction:** If `apiKeyHelper` is configured in the sub-agent's `--settings`, the helper script runs with the sub-agent's process environment. If the conductor's API key is already in the env dict, the `apiKeyHelper` script can simply echo it back — but this provides no additional security (the key is in env either way). The `apiKeyHelper` only adds value when it fetches a credential from an external source that the sub-agent cannot otherwise access.

**R-SEC-D (Minimal blast radius of `ANTHROPIC_API_KEY`):** The research question from `06-security-threat-model.md` asked whether Anthropic supports short-lived per-session tokens to reduce blast radius. As of March 2026:

- Anthropic does not offer short-lived API key tokens. API keys from the Console are long-lived until revoked.
- There is no documented mechanism to exchange a long-lived key for a short-lived session token.
- The closest analog is using `CLAUDE_CODE_OAUTH_TOKEN` from a Claude.ai account session, but as documented above, OAuth tokens are broken in headless mode.

**Conclusion:** Blast radius reduction for `ANTHROPIC_API_KEY` must be achieved through:
1. Dedicated conductor key (not the operator's personal key) — limits blast to conductor's access scope
2. Credential proxy pattern — prevents agents from holding the key in their env at all
3. Prompt blockers — `Bash(env)`, `Bash(printenv)` in denylist prevents opportunistic exfiltration

---

## Cross-References

- **`04-configuration.md` § 6.1–6.3:** The `build_agent_env()` function pattern, scrubbed env dict approach, `CLAUDECODE=1` nesting problem, and `apiKeyHelper` introduction. This document extends those findings with detailed apiKeyHelper mechanics and the rotation strategy.
- **`04-configuration.md` § 5.4:** `CLAUDE_CONFIG_DIR` isolation per sub-agent — directly addresses concurrent credential conflicts.
- **`06-security-threat-model.md` T3:** Credential exposure via process environment — the threat this document's recommendations are designed to mitigate.
- **`06-security-threat-model.md` R-SEC-A:** Credential proxy pattern for `gh` CLI — a future research item for eliminating `GITHUB_TOKEN` from sub-agent environments (distinct from the `ANTHROPIC_API_KEY` concern here, but related design pattern).
- **`06-security-threat-model.md` R-SEC-D:** Short-lived API key blast radius reduction — this document's section 8 addresses the current state of that question.
- **`10-settings-mcp-injection.md` § 1.4–1.6:** The `--settings` flag as the injection mechanism for per-agent `apiKeyHelper` configuration. Confirms that `apiKeyHelper` set via `--settings` wins over user settings (scalar override behavior).
- **`02-session-continuity.md` § 8:** `CLAUDE_CONFIG_DIR` isolation breaks `--resume` — relevant because the isolation model used for credential safety also prevents session resumption, reinforcing the stateless dispatch model.
- **`03-error-handling.md`** (pending): Credential errors are non-retryable. The Conductor Response Protocol in section 6.3 of this document provides the specific detection and classification logic that will feed into issue #3's error taxonomy.

---

## Follow-Up Research Recommendations

### R-CRED-A: Empirical Verification of apiKeyHelper in Headless `-p` Mode

**Question:** Does `apiKeyHelper` fire correctly in a `claude -p` subprocess? Specifically:

- Does the helper script have access to the subprocess's env dict (including `ANTHROPIC_API_KEY` if it was passed)?
- Does the 401-triggered refresh work in headless mode, or does the same TTL bug (Issue #11639) affect the 401 path too?
- When the helper is injected via `--settings` (not global `~/.claude/settings.json`), does it take precedence over any `ANTHROPIC_API_KEY` in the env dict, or do both coexist?

**Why it matters:** The TTL bug means the helper is called very frequently. If the 401-triggered path is also broken, `apiKeyHelper` provides no mid-session recovery for cloud provider credentials.

**Suggested test:**
```bash
# Create a helper that logs each invocation with a timestamp
cat > /tmp/test-helper.sh << 'EOF'
#!/bin/sh
echo "$(date +%s): helper called" >> /tmp/helper-calls.log
echo "$ANTHROPIC_API_KEY"
EOF
chmod +x /tmp/test-helper.sh

# Run with apiKeyHelper injected via --settings
claude -p \
  --settings '{"apiKeyHelper": "/tmp/test-helper.sh"}' \
  --output-format stream-json \
  "Count to 100 slowly and make one API call per number." \
  > /tmp/test-output.jsonl

# Count invocations
wc -l /tmp/helper-calls.log
```

### R-CRED-B: Per-Agent API Key Scoping for Per-Key Spend Tracking

**Question:** Can conductor maintain a pool of API keys (one per concurrent sub-agent) to enable per-agent spend tracking at the Anthropic Console level? Specifically:

- Does assigning different API keys to concurrent sub-agents work correctly with the Anthropic Console's usage analytics?
- Is there a documented limit on the number of API keys per Anthropic account?
- What is the key provisioning latency (can keys be created programmatically via the Anthropic API, or only via the web Console)?

**Why it matters:** For organizations that need per-agent cost attribution, per-key assignment is the current mechanism. If key creation is automated via API, the conductor could provision ephemeral keys per agent invocation and revoke them after completion.

### R-CRED-C: Credential Proxy Pattern for ANTHROPIC_API_KEY

**Question:** Is it feasible to run a local Unix-socket or loopback HTTP proxy that intercepts `claude -p`'s outbound requests to `api.anthropic.com`, injects the `X-Api-Key` header, and forwards the request — so sub-agents never hold the key in their process environment?

This is the equivalent of R-SEC-A in `06-security-threat-model.md` (which covers the `gh` CLI), applied to the Anthropic API itself.

**Implementation approach:**
- Set `ANTHROPIC_BASE_URL=http://localhost:9000/proxy` in the sub-agent env (omit `ANTHROPIC_API_KEY`)
- Run a local proxy (e.g., using `mitmproxy` or a small Python `http.server`) that adds the `X-Api-Key` header and forwards to `api.anthropic.com`
- Sub-agent never has the real key; key exfiltration via `Bash(env)` yields nothing useful

**Why it matters:** This eliminates T3 (Credential Exposure) entirely for the Anthropic key, at the cost of proxy implementation and maintenance overhead. Given CVE-2026-21852 (API key exfiltration via `ANTHROPIC_BASE_URL`), the same mechanism can be an attack vector — the proxy's `ANTHROPIC_BASE_URL` must be validated before passing to sub-agents.

### R-CRED-D: Bedrock awsAuthHelper Status and Implementation

**Question:** Has the proposed `awsAuthHelper` mechanism from Issue #2280 (Bedrock short-lived IAM credential refresh) shipped in any Claude Code version as of mid-2026? If so, what is the configuration and behavior?

**Why it matters:** For organizations that deploy conductor on AWS infrastructure using IAM roles and STS tokens for Bedrock, the lack of automatic credential refresh is a blocker for long-running sessions. If `awsAuthHelper` has shipped, it replaces the manual `apiKeyHelper` workaround documented in section 3.5.

---

## Sources

- [Authentication — Claude Code Docs](https://code.claude.com/docs/en/authentication) — apiKeyHelper description, CLAUDE_CODE_API_KEY_HELPER_TTL_MS, credential storage on macOS Keychain
- [Settings — Claude Code Docs](https://code.claude.com/docs/en/settings) — apiKeyHelper configuration format, `env` field in settings.json, CLAUDE_CODE_API_KEY_HELPER_TTL_MS, available settings schema
- [How to Dynamically Change Anthropic API Key in Claude Code — AI Engineer Guide](https://aiengineerguide.com/blog/dynamically-change-api-key-in-claude-code/) — apiKeyHelper practical configuration, refresh behavior, credential rotation use cases
- [GitHub Issue #11639: CLAUDE_CODE_API_KEY_HELPER_TTL_MS cache not working — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/11639) — Confirmed TTL bug: helper called every ~5 min regardless of configured TTL; PowerShell cache workaround
- [GitHub Issue #7660: apiKeyHelper TTL caching original report — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/7660) — Parent issue for the TTL caching bug
- [GitHub Issue #11587: Auth conflict with CLAUDE_CODE_OAUTH_TOKEN and apiKeyHelper — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/11587) — Do not use both OAuth token and apiKeyHelper simultaneously
- [GitHub Issue #28827: OAuth token refresh fails in non-interactive/headless mode — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/28827) — OAuth tokens expire after ~10–15 min; refresh broken in headless mode; closed as dup of #12447
- [GitHub Issue #12447: OAuth token expiration disrupts autonomous workflows — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/12447) — Root issue for OAuth expiry in headless automation
- [GitHub Issue #28207: API Error 401 on new sessions and idle session resumption — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/28207) — Concurrent session credential conflicts; closed as dup of #22924
- [GitHub Issue #2280: Claude Code does not handle expiry of short-term IAM credentials — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/2280) — Bedrock STS token expiry; no auto-refresh; closed as dup of #3038
- [GitHub Issue #3038: apiKeyHelper support for Bedrock — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/3038) — apiKeyHelper as the intended mechanism for Bedrock credential refresh
- [GitHub Issue #7100: Document Headless/Remote Authentication for CI/CD — anthropics/claude-code](https://github.com/anthropics/claude-code/issues/7100) — ANTHROPIC_API_KEY for headless auth; documentation gaps for remote environments; closed not planned
- [Managing API Key Environment Variables in Claude Code — Claude Help Center](https://support.claude.com/en/articles/12304248-managing-api-key-environment-variables-in-claude-code) — ANTHROPIC_API_KEY vs CLAUDE_CODE_OAUTH_TOKEN usage patterns
- [API Key Best Practices — Claude Help Center](https://support.claude.com/en/articles/9767949-api-key-best-practices-keeping-your-keys-safe-and-secure) — Anthropic API keys do not expire automatically; revocation is manual
- [Securely Deploying AI Agents — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/secure-deployment) — Credential proxy pattern; principle of least privilege for agent credentials
- [Caught in the Hook: RCE and API Token Exfiltration via CVE-2026-21852 — Check Point Research](https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/) — ANTHROPIC_BASE_URL as exfiltration vector; patched February 25, 2026; deferred-request fix
- [Claude Code deployment patterns and best practices with Amazon Bedrock — AWS Blog](https://aws.amazon.com/blogs/machine-learning/claude-code-deployment-patterns-and-best-practices-with-amazon-bedrock/) — Bedrock auth patterns, awsAuthRefresh and awsCredentialExport mechanisms
- [Bug: subprocess inherits CLAUDECODE=1 env var — anthropics/claude-agent-sdk-python #573](https://github.com/anthropics/claude-agent-sdk-python/issues/573) — CLAUDECODE must be filtered from subprocess env; credential isolation prerequisite
- [Claude Code Settings Reference — 10-settings-mcp-injection.md](10-settings-mcp-injection.md) — apiKeyHelper field in --settings schema (section 1.4), per-agent settings generation pattern (section 1.6), CLAUDE_CONFIG_DIR isolation (section 1.5)
- [Configuration and Environment Management — 04-configuration.md](04-configuration.md) — build_agent_env() pattern (section 6.1), CLAUDECODE nesting problem (section 6.2), apiKeyHelper introduction (section 6.3), CLAUDE_CONFIG_DIR isolation (section 5.4)
- [Security Threat Model — 06-security-threat-model.md](06-security-threat-model.md) — T3 (credential exposure), R-SEC-A (credential proxy), R-SEC-D (short-lived key blast radius)
- [Errors — Anthropic API Docs](https://platform.claude.com/docs/en/api/errors) — 401 authentication_error not retryable; 403 handling for cloud providers
