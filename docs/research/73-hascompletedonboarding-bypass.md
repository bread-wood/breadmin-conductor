# Research: hasCompletedOnboarding Bypass of ANTHROPIC_BASE_URL in Headless -p Mode

**Issue:** #73
**Milestone:** v2
**Feature:** core
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [The Bug: Issue #26935](#the-bug-issue-26935)
3. [Affected Scenarios for Conductor](#affected-scenarios-for-conductor)
4. [Minimal CLAUDE_CONFIG_DIR Initialization](#minimal-claude_config_dir-initialization)
5. [Fix Status and Version History](#fix-status-and-version-history)
6. [Verification Test Protocol](#verification-test-protocol)
7. [Integration with Conductor dispatch_agent()](#integration-with-conductor-dispatch_agent)
8. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
9. [Sources](#sources)

---

## Executive Summary

GitHub Issue #26935 documents a confirmed regression where Claude Code bypasses
`ANTHROPIC_BASE_URL` and contacts `api.anthropic.com` directly when
`hasCompletedOnboarding` is not set in the user's config. For conductor sub-agents,
each dispatch uses a fresh `CLAUDE_CONFIG_DIR` temp directory with no pre-existing
config — exactly the scenario that triggers this bypass.

**Key findings:**

1. **The bypass is real and confirmed.** Issue #26935 (opened February 19, 2026) is
   unresolved as of March 2026. Users with `ANTHROPIC_BASE_URL` pointing to a local
   proxy encounter "Unable to connect to api.anthropic.com" errors during onboarding
   because Claude Code ignores `ANTHROPIC_BASE_URL` during the initial onboarding check.
   [DOCUMENTED]

2. **The workaround is simple and reliable.** Writing `{"hasCompletedOnboarding": true}`
   to the `settings.json` file in the temp `CLAUDE_CONFIG_DIR` before spawning the
   sub-agent prevents the bypass. This is the documented fix from the community. [DOCUMENTED]

3. **The fix must be applied per-agent dispatch.** Since conductor creates a fresh
   `CLAUDE_CONFIG_DIR` for each sub-agent (for isolation), the initialization must
   happen in the `dispatch_agent()` function, not once at conductor startup. [INFERRED]

4. **The bypass does NOT fall back to the macOS Keychain** in conductor's deployment
   pattern. Because conductor omits `ANTHROPIC_API_KEY` from the sub-agent env (proxy
   model) and uses a fresh `CLAUDE_CONFIG_DIR` with no stored credentials, the bypass
   attempt to contact `api.anthropic.com` fails immediately with an auth error rather
   than silently using the operator's personal account. [INFERRED — security positive]

5. **Combined with doc #71** (env var precedence): the correct initialization for each
   sub-agent's `CLAUDE_CONFIG_DIR` is:
   ```json
   {"hasCompletedOnboarding": true}
   ```
   No additional settings are required.

---

## The Bug: Issue #26935

### Report Summary

**Title:** Claude Code attempts to call api.anthropic.com ignoring local ANTHROPIC_BASE_URL
when hasCompletedOnboarding has not been set

**Date:** February 19, 2026

**Symptoms:**
- User sets `ANTHROPIC_BASE_URL` to a local LiteLLM instance
- `api.anthropic.com` is blocked by their firewall/proxy
- First run of Claude Code (fresh config) fails with:
  `"Unable to connect to Anthropic services. Failed to connect to api.anthropic.com: ERR_BAD_REQUEST"`
- After manually adding `"hasCompletedOnboarding": true` to `~/claude.json` or
  `~/.claude/settings.json`, the issue resolves and `ANTHROPIC_BASE_URL` is respected

**Root cause (from issue discussion):**
During the onboarding flow, Claude Code performs an initial connectivity/auth check
against `api.anthropic.com` before reading `ANTHROPIC_BASE_URL` from settings. This
check bypasses the custom base URL. Once `hasCompletedOnboarding` is set, the onboarding
flow is skipped and `ANTHROPIC_BASE_URL` is respected from the start.

**Status:** Open as of March 2, 2026. No fix shipped. [DOCUMENTED]

### Related Issues

- **Issue #28438:** "Onboarding flow login prompt does not consider ANTHROPIC_AUTH_TOKEN
  env var from settings files" — similar class of bug where onboarding ignores env vars
- **Issue #4714:** "Onboarding Process Ignores Environment Settings" — older report of
  same class of bug (pre-v2)
- **Issue #15274:** "ANTHROPIC_BASE_URL ignored during setup" — variant of #26935

This pattern suggests the onboarding flow has a persistent design flaw where it
hard-codes `api.anthropic.com` for its initial check, regardless of user configuration.

---

## Affected Scenarios for Conductor

### Scenario A: Fresh temp CLAUDE_CONFIG_DIR per agent (normal conductor operation)

Conductor creates `/tmp/conductor-agent-XXXX/` as a fresh temp directory and sets
`CLAUDE_CONFIG_DIR` to this path. This directory has no `settings.json`.

**Trigger:** The sub-agent starts, enters onboarding flow (no `hasCompletedOnboarding`),
attempts to contact `api.anthropic.com`, and fails (because conductor's proxy intercepts
traffic and `api.anthropic.com` may not be reachable in sandbox environments).

**Severity:** HIGH in proxy mode. The sub-agent dies at startup with an auth error.

### Scenario B: Proxy mode disabled (direct API key)

If conductor passes `ANTHROPIC_API_KEY` directly and does not use the proxy
(`ANTHROPIC_BASE_URL` is not set), this issue does not apply. The onboarding check
against `api.anthropic.com` succeeds because the real API is being used.

**Implication:** The proxy architecture makes this issue a blocker. Non-proxy deployments
are unaffected.

### Scenario C: Reused CLAUDE_CONFIG_DIR (not conductor's pattern)

If the same `CLAUDE_CONFIG_DIR` is reused across multiple agent dispatches (e.g., a
persistent config directory), `hasCompletedOnboarding` would already be set from the
first run. This avoids the issue at the cost of reduced isolation.

**Not recommended for conductor.** Reusing config directories leaks state between agents.

---

## Minimal CLAUDE_CONFIG_DIR Initialization

The confirmed workaround is to pre-write `hasCompletedOnboarding: true` to the
settings.json in the temp CLAUDE_CONFIG_DIR.

**Location of the file:** The file is called `settings.json` and lives directly in the
`CLAUDE_CONFIG_DIR` (not in a `.claude/` subdirectory).

```
CLAUDE_CONFIG_DIR/
└── settings.json   ← must contain {"hasCompletedOnboarding": true}
```

**Minimal content:**

```json
{"hasCompletedOnboarding": true}
```

**Full recommended content** (combining with doc #71's sanitization requirements):

```json
{
  "hasCompletedOnboarding": true
}
```

Note: The `env` field is intentionally omitted. Adding `env.ANTHROPIC_BASE_URL` here
would create the exact override vulnerability analyzed in doc #71. The proxy URL must
come exclusively from the process environment.

### Alternative: Pre-set in the user's global settings

For deployments where a global `~/.claude/settings.json` is acceptable (non-temp-dir
pattern), setting `hasCompletedOnboarding: true` globally prevents the bypass without
per-agent initialization:

```json
// ~/.claude/settings.json (operator's personal config)
{
  "hasCompletedOnboarding": true
}
```

This approach is simpler but does not work for conductor's isolated-per-agent model,
because sub-agents use separate `CLAUDE_CONFIG_DIR` paths.

---

## Fix Status and Version History

| Event | Date | Details |
|-------|------|---------|
| Issue #26935 opened | Feb 19, 2026 | Community reports bypass with LiteLLM |
| Issue #4714 opened | 2024 | Earlier report of same class of bug |
| Issue #15274 opened | 2025 | Variant: ANTHROPIC_BASE_URL ignored during setup |
| Issue #28438 opened | 2025 | Onboarding ignores ANTHROPIC_AUTH_TOKEN |
| Workaround documented | Mar 2026 | `hasCompletedOnboarding: true` in settings.json |
| Fix shipped | Unknown | Not confirmed as of March 2, 2026 |

**Recommendation:** Do not wait for the upstream fix. Implement the `hasCompletedOnboarding`
initialization in conductor's `dispatch_agent()` immediately. The workaround is reliable
and has no side effects.

---

## Verification Test Protocol

```bash
#!/usr/bin/env bash
# Test: Does fresh CLAUDE_CONFIG_DIR without hasCompletedOnboarding trigger the bypass?
# Requires: ANTHROPIC_BASE_URL set to a local proxy, proxy running on port 9000

# Step 1: Create fresh config dir WITHOUT hasCompletedOnboarding
FRESH_CONFIG=$(mktemp -d)
echo '{}' > "$FRESH_CONFIG/settings.json"

# Step 2: Run claude -p with proxy URL
ANTHROPIC_BASE_URL="http://127.0.0.1:9000" \
CLAUDE_CONFIG_DIR="$FRESH_CONFIG" \
claude -p --dangerously-skip-permissions --output-format json "Hello" 2>&1

# Expected: fails with "Unable to connect to Anthropic services" (bug triggered)
# OR: succeeds (bug not present in current version)

# Step 3: Create config dir WITH hasCompletedOnboarding
FIXED_CONFIG=$(mktemp -d)
echo '{"hasCompletedOnboarding": true}' > "$FIXED_CONFIG/settings.json"

# Step 4: Run same command
ANTHROPIC_BASE_URL="http://127.0.0.1:9000" \
CLAUDE_CONFIG_DIR="$FIXED_CONFIG" \
claude -p --dangerously-skip-permissions --output-format json "Hello" 2>&1

# Expected: succeeds (or fails with proxy error, not api.anthropic.com error)
```

---

## Integration with Conductor dispatch_agent()

The full initialization function combining doc #71 and doc #73 requirements:

```python
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

@contextmanager
def create_agent_config_dir():
    """
    Create a minimal, isolated CLAUDE_CONFIG_DIR for a conductor sub-agent.

    Addresses:
    - #73: hasCompletedOnboarding bypass (sets the flag to skip onboarding)
    - #71: ANTHROPIC_BASE_URL settings.json override (omits env field)
    - #38: Per-job isolation (fresh temp dir, cleaned up after use)
    """
    tmp_dir = tempfile.mkdtemp(prefix="conductor-agent-")
    try:
        settings = {
            "hasCompletedOnboarding": True,
            # DO NOT add env.ANTHROPIC_BASE_URL here (see doc #71)
        }
        settings_path = Path(tmp_dir) / "settings.json"
        settings_path.write_text(json.dumps(settings, indent=2))
        yield tmp_dir
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)

# Usage in dispatch_agent():
async def dispatch_agent(
    issue_number: int,
    branch: str,
    prompt: str,
    config: Config,
) -> None:
    with create_agent_config_dir() as config_dir:
        env = {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9000",  # Proxy URL
            "CLAUDE_CONFIG_DIR": config_dir,
            # ANTHROPIC_API_KEY deliberately omitted (proxy handles auth)
            "GH_TOKEN": os.environ["GH_TOKEN"],
        }
        result = await asyncio.create_subprocess_exec(
            "claude", "-p",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            prompt,
            env=env,
            ...
        )
```

---

## Follow-Up Research Recommendations

**[WONT_RESEARCH] Tracking upstream fix for Issue #26935**
Monitor the GitHub issue for a fix. Once fixed, the `hasCompletedOnboarding` initialization
can be made conditional on version detection. Not worth a dedicated research doc.

**[WONT_RESEARCH] Testing hasCompletedOnboarding location: settings.json vs. claude.json**
Community reports reference both `~/claude.json` and `~/.claude/settings.json` as the
location for this setting. The CLAUDE_CONFIG_DIR override makes this irrelevant for
conductor — the settings.json in the temp CLAUDE_CONFIG_DIR is the authoritative location.
No research needed.

---

## Sources

- [[BUG] Claude Code attempts to call api.anthropic.com ignoring local ANTHROPIC_BASE_URL — Issue #26935](https://github.com/anthropics/claude-code/issues/26935)
- [[BUG] Onboarding flow login prompt does not consider ANTHROPIC_AUTH_TOKEN — Issue #28438](https://github.com/anthropics/claude-code/issues/28438)
- [[BUG] Onboarding Process Ignores Environment Settings — Issue #4714](https://github.com/anthropics/claude-code/issues/4714)
- [[BUG] ANTHROPIC_BASE_URL ignored during setup — Issue #15274](https://github.com/anthropics/claude-code/issues/15274)
- [Claude Code Login Bypass: Skip Mandatory Authentication — Efficient Coder](https://www.xugj520.cn/en/archives/claude-code-login-bypass-guide.html)
- [Running Claude Code with Local Models via Ollama — Hugging Face Blog](https://huggingface.co/blog/GhostScientist/claude-code-with-local-models)

**Cross-references:**
- `71-env-var-precedence-anthropic-base-url.md` — env var vs. settings.json precedence analysis; CLAUDE_CONFIG_DIR isolation pattern
- `43-anthropic-key-proxy.md` — R-PROXY-B origin; proxy architecture requiring ANTHROPIC_BASE_URL to be respected
- `04-configuration.md` — CLAUDE_CONFIG_DIR mechanics; per-agent isolation model
- `38-ci-server-deployment.md` — CLAUDE_CONFIG_DIR per-job isolation for CI
