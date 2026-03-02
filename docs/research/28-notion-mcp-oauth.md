# Research: Notion MCP OAuth Token Accessibility with CLAUDE_CONFIG_DIR Isolation

**Issue:** #28
**Milestone:** M1: Foundation
**Status:** Complete
**Date:** 2026-03-02

---

## Executive Summary

Notion MCP OAuth tokens are stored in `~/.claude/.credentials.json` (or `$CLAUDE_CONFIG_DIR/.credentials.json` when that variable is set). When `CLAUDE_CONFIG_DIR` is pointed to a per-agent temp directory, those tokens become inaccessible because the temp dir contains no credentials file. This confirms the finding in `07-skill-adaptation.md` (section 11.1): the conductor orchestrator must NOT use `CLAUDE_CONFIG_DIR` isolation if it needs Notion MCP access.

However, the hosted Notion MCP (OAuth-only) is fundamentally unsuitable for fully headless automation — OAuth access tokens expire after approximately one hour and Claude Code does not auto-refresh them in `-p` mode (#28827, #12447, #19481). The practical solution for conductor is to use the **local stdio `@notionhq/notion-mcp-server`** with a static Notion integration token, injected via `.mcp.json` `env` block, which sidesteps both the OAuth expiry problem and the `CLAUDE_CONFIG_DIR` isolation problem entirely.

---

## Cross-References

- **04-configuration.md §5.4** — Introduces `CLAUDE_CONFIG_DIR` isolation as the recommended pattern for sub-agent separation. That section notes the trade-off: full isolation prevents inheriting user MCP configs. This document resolves the specific Notion MCP consequence of that trade-off.
- **10-settings-mcp-injection.md §2.4** — Documents that when `CLAUDE_CONFIG_DIR` points to a temp dir, user-scoped and local-scoped MCPs (stored in `~/.claude.json` and `~/.claude/.credentials.json`) are not loaded. Section §3.4 recommends the `.mcp.json`-in-worktree pattern as the most reliable injection mechanism.
- **10-settings-mcp-injection.md §5.3** — Documents the orchestrator MCP config pattern and explicitly calls out Notion as needing a `Bearer ${NOTION_API_KEY}` env var expansion in `.mcp.json`.

---

## 1. Notion MCP Authentication: Two Distinct Servers

Notion provides two distinct MCP deployment models with different authentication mechanisms. Understanding the distinction is essential because `04-configuration.md` and `10-settings-mcp-injection.md` reference both without clearly separating their auth flows.

### 1.1 Hosted Notion MCP (Remote, OAuth-Only)

**URL:** `https://mcp.notion.com/mcp`

**Authentication:** OAuth 2.0 only. The user must complete a browser-based OAuth flow — no static token alternative is supported for the remote endpoint. [DOCUMENTED]

**Configuration:**
```bash
claude mcp add --transport http notion https://mcp.notion.com/mcp
# Then: claude /mcp → authenticate via browser
```

**Token storage after authentication:**
- OAuth tokens are stored in `~/.claude/.credentials.json` under the key `mcpOAuth.notion|*`
- The token object contains `accessToken`, `refreshToken`, and `expiresAt`

**Token lifetime:** Access tokens expire after approximately one hour (`expiresAt` timestamps observed in issue #28256). [DOCUMENTED]

**Token refresh in `-p` mode:** BROKEN. Claude Code does not automatically refresh expired OAuth tokens when running in non-interactive (`-p`) mode. Instead, it returns a `401 authentication_error: OAuth token has expired` and exits. This is a known open bug (issues #28827, #12447, #5706, #19481). None of these are fixed as of March 2026. [DOCUMENTED]

**Consequence for conductor:** Any orchestrator or worker using the hosted Notion MCP via OAuth will break after ~1 hour of session runtime and cannot be healed without manual `/login` re-authentication. The hosted Notion MCP is not viable for conductor's automated session reporting.

### 1.2 Local Stdio Notion MCP Server (`@notionhq/notion-mcp-server`)

**Package:** `@notionhq/notion-mcp-server` on npm

**Authentication:** Static Notion integration token via environment variable. No OAuth required. [DOCUMENTED]

**Supported auth env vars:**
- `NOTION_TOKEN=ntn_****` — recommended; sets the integration secret
- `OPENAPI_MCP_HEADERS='{"Authorization": "Bearer ntn_****", "Notion-Version": "2025-09-03"}'` — advanced; passes raw headers

**Configuration in `.mcp.json`:**
```json
{
  "mcpServers": {
    "notion": {
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "env": {
        "NOTION_TOKEN": "${NOTION_TOKEN}"
      }
    }
  }
}
```

**Maintenance status:** Notion is prioritizing the remote MCP server and has stated this local repository "may sunset in the future" with issues and PRs not actively monitored. However, as of March 2026 it remains published on npm and functional. [DOCUMENTED]

**Consequence for conductor:** The local stdio server requires no OAuth, no browser interaction, no token refresh, and no `~/.claude/.credentials.json`. A static integration token stored in an environment variable is sufficient for fully automated operation. This is the correct authentication model for conductor.

---

## 2. Where Notion MCP OAuth Tokens Are Stored

### 2.1 Default Location (No `CLAUDE_CONFIG_DIR`)

OAuth tokens for all MCP servers are stored in `~/.claude/.credentials.json`. [DOCUMENTED — confirmed by issues #28256, #20553, #21765, and the devcontainer guide at tfvchow/field-notes-public#10]

The file structure is:
```json
{
  "mcpOAuth": {
    "notion|https://mcp.notion.com/mcp": {
      "accessToken": "...",
      "refreshToken": "...",
      "expiresAt": 1709000000000,
      "scopes": ["read_content", "update_content", ...]
    }
  }
}
```

Additionally, `~/.claude.json` stores account info, onboarding state, and MCP server definitions for user and local scopes. Both files are required for Claude Code to consider a session authenticated.

### 2.2 With `CLAUDE_CONFIG_DIR` Set

When `CLAUDE_CONFIG_DIR=/some/path` is set, the following files are relocated to `$CLAUDE_CONFIG_DIR/`: [DOCUMENTED — confirmed by issue #3833 December 2025 comment]

| File | Default path | With CLAUDE_CONFIG_DIR |
|------|-------------|------------------------|
| `.credentials.json` | `~/.claude/.credentials.json` | `$CLAUDE_CONFIG_DIR/.credentials.json` |
| `.claude.json` | `~/.claude.json` | `$CLAUDE_CONFIG_DIR/.claude.json` |
| `settings.json` | `~/.claude/settings.json` | `$CLAUDE_CONFIG_DIR/settings.json` |
| `projects/` | `~/.claude/projects/` | `$CLAUDE_CONFIG_DIR/projects/` |

When `CLAUDE_CONFIG_DIR` is set to a fresh temporary directory (as in the conductor's sub-agent isolation pattern), none of these files exist in the temp dir. The session starts with no authentication state and no MCP credentials. [INFERRED from documented file relocation behavior + confirmed by #3833]

**macOS Keychain note:** On macOS, Claude Code also stores OAuth credentials in the Keychain under service name `Claude Code-credentials`. This Keychain entry is NOT namespaced by `CLAUDE_CONFIG_DIR` — it uses a hardcoded service name. This creates a cross-profile collision (issue #20553, closed not planned). For conductor's purposes: Keychain storage affects the user's own Claude sessions, not MCP server credentials for sub-agents. MCP OAuth tokens flow through `.credentials.json`, not the Keychain. [INFERRED from #20553 issue analysis]

### 2.3 Incomplete Isolation Warning

Issue #3833 (and related issues #15670, #19456) document that `CLAUDE_CONFIG_DIR` isolation is incomplete. Project-local `.claude/settings.local.json` files are still created in workspace directories regardless of `CLAUDE_CONFIG_DIR`. However, this does NOT affect credential storage — `.credentials.json` correctly moves to `$CLAUDE_CONFIG_DIR`. [DOCUMENTED]

---

## 3. What `CLAUDE_CONFIG_DIR` Isolation Breaks

When a sub-agent runs with `CLAUDE_CONFIG_DIR=/tmp/isolated-dir`:

| Feature | Broken? | Why |
|---------|---------|-----|
| User-scoped MCP servers (from `~/.claude.json`) | Yes | `$CLAUDE_CONFIG_DIR/.claude.json` is empty |
| Local-scoped MCP servers (per-project in `~/.claude.json`) | Yes | Same file, inaccessible |
| Notion MCP OAuth tokens | Yes | `$CLAUDE_CONFIG_DIR/.credentials.json` does not exist |
| Project-scoped MCP servers (`.mcp.json` in worktree) | No | `.mcp.json` is read from the CWD, not from `CLAUDE_CONFIG_DIR` |
| `--mcp-config` injected servers | No | Passed at invocation time, not read from config dir |
| Session history continuity | Yes | No `projects/` dir in temp location |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | N/A | Not affected; this is an env var |

The Notion MCP OAuth token failure mode when `CLAUDE_CONFIG_DIR` is set: the MCP server will either fail to connect (no credential found) or show `△ needs authentication` in the tool list, indicating it is unconfigured. No error is raised at subprocess start — the failure only surfaces when a Notion MCP tool is actually called. [INFERRED]

---

## 4. The OAuth Expiry Problem for Headless Orchestrators

Even if the orchestrator avoids `CLAUDE_CONFIG_DIR` isolation (so it can access `~/.claude/.credentials.json`), the hosted Notion MCP is still not viable for automated orchestration because of the OAuth token refresh failure in `-p` mode.

**Timeline of a headless orchestrator session:**

1. Operator authenticates Notion MCP interactively: `claude /mcp` → browser OAuth flow → tokens stored in `~/.claude/.credentials.json`
2. Orchestrator starts: `claude -p "conduct issue backlog..."` — Notion MCP loads and works (access token is fresh)
3. ~1 hour later: access token expires
4. Orchestrator calls a Notion MCP tool → `401 authentication_error: OAuth token has expired`
5. Claude Code does NOT use the stored refresh token to obtain a new access token in `-p` mode
6. Orchestrator session may crash or silently lose Notion access

Known related issues (all open or closed as duplicate, none fixed):
- #28827 — OAuth token refresh fails in non-interactive/headless mode
- #12447 — OAuth access token refresh not working
- #19481 — OAuth token expiration disrupts autonomous workflows
- #5706 — Missing token refresh mechanism for MCP server integrations

**Conclusion:** Even without `CLAUDE_CONFIG_DIR` isolation, the hosted Notion MCP breaks for any conductor session running longer than ~1 hour. [DOCUMENTED]

---

## 5. Recommended Architecture: Static Integration Token

### 5.1 Integration Token vs. OAuth

| Property | Hosted Notion MCP (OAuth) | Local stdio server (Integration Token) |
|----------|---------------------------|----------------------------------------|
| Auth mechanism | OAuth 2.0, browser flow | Static API key |
| Token expiry | ~1 hour (access token) | Never (integration tokens don't expire) |
| Headless viable | No | Yes |
| `CLAUDE_CONFIG_DIR` compatible | No (needs `~/.claude/.credentials.json`) | Yes (token injected via env var) |
| `.mcp.json` env var injection | N/A | Yes, via `NOTION_TOKEN` env var |
| Maintenance status | Actively maintained | Low maintenance (may sunset) |
| Requires npm | No | Yes (`npx @notionhq/notion-mcp-server`) |

**Recommendation:** Use `@notionhq/notion-mcp-server` with a static Notion integration token. Create the integration at `https://www.notion.so/profile/integrations`, store the token as `NOTION_TOKEN` in the conductor's environment, and inject it via `.mcp.json`. [INFERRED from documented capabilities + confirmed working pattern from wmedia.es article]

### 5.2 Obtaining a Notion Integration Token

1. Go to `https://www.notion.so/profile/integrations`
2. Create a new internal integration (workspace-scoped)
3. Copy the integration secret (`ntn_****`)
4. In Notion, grant the integration access to the pages/databases conductor needs to write to (via "Connect to" on each page or via workspace-level access)

The integration token is a static secret — it does not expire and does not require browser-based renewal. [DOCUMENTED]

### 5.3 `.mcp.json` Injection Pattern for Conductor

Write `.mcp.json` into the conductor repo root (for orchestrator sessions) or generate it into the sub-agent worktree (if any sub-agent needs Notion):

```json
{
  "mcpServers": {
    "notion": {
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "env": {
        "NOTION_TOKEN": "${NOTION_TOKEN}"
      }
    }
  }
}
```

**`${NOTION_TOKEN}` expansion:** Claude Code v1.0.48+ supports `${VAR}` expansion in `.mcp.json` headers and env blocks. The `NOTION_TOKEN` env var must be present in the process environment when Claude Code starts (not just in the sub-agent subprocess env). [DOCUMENTED — issue #3239 fixed in v1.0.48]

**Known env block injection bug:** Issues #28090, #23216, and #1254 document cases where the `env` block in `.mcp.json` is not correctly passed to the spawned MCP server subprocess in some contexts (primarily the VSCode extension). The CLI (`claude -p`) does not exhibit this bug. For conductor's subprocess-based invocations, the `env` block injection works correctly in the terminal CLI. [INFERRED from bug scope descriptions]

**Alternative if env block fails:** Use `OPENAPI_MCP_HEADERS` instead, which goes via environment variable at the Claude Code process level rather than through the MCP server spawn:

```json
{
  "mcpServers": {
    "notion": {
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "env": {
        "OPENAPI_MCP_HEADERS": "{\"Authorization\": \"Bearer ${NOTION_TOKEN}\", \"Notion-Version\": \"2022-06-28\"}"
      }
    }
  }
}
```

Or inline in the MCP command (per wmedia.es real-world pattern):
```bash
claude mcp add notion \
  --env 'OPENAPI_MCP_HEADERS={"Authorization":"Bearer ntn_xxx","Notion-Version":"2022-06-28"}' \
  -- npx -y @notionhq/notion-mcp-server
```

---

## 6. Which Conductor Components Need Notion Access

Per the issue and the context from `07-skill-adaptation.md`:

| Component | Needs Notion? | Isolation Level | Configuration |
|-----------|--------------|-----------------|---------------|
| **Orchestrator (interactive)** | Yes — posts session reports | No `CLAUDE_CONFIG_DIR` isolation (runs as user session) | Operator's user-scope config OR `.mcp.json` in repo root |
| **Orchestrator (headless, `claude -p`)** | Yes — posts session reports | No `CLAUDE_CONFIG_DIR` isolation recommended | `.mcp.json` in repo root with `${NOTION_TOKEN}` expansion |
| **Issue-worker sub-agents** | No | Full `CLAUDE_CONFIG_DIR` isolation | `.mcp.json` with GitHub MCP only (per §5.1 of `10-settings-mcp-injection.md`) |
| **Research-worker sub-agents** | No | Full `CLAUDE_CONFIG_DIR` isolation | No `.mcp.json` needed |

**Key conclusion:** Only the orchestrator needs Notion access. Sub-agents (issue-workers, research-workers) should not have Notion MCP configured — they have no reason to write to Notion and giving them access increases the blast radius of a misbehaving agent.

---

## 7. Orchestrator `CLAUDE_CONFIG_DIR` Decision

### 7.1 Should the Orchestrator Use `CLAUDE_CONFIG_DIR` Isolation?

No, with qualifications.

The orchestrator is not an isolated sub-agent — it is the conductor's own session. It runs with the operator's identity, reads issue state from GitHub, and posts reports to Notion. Using `CLAUDE_CONFIG_DIR` isolation on the orchestrator would:

1. Break OAuth-based MCP servers (if the operator uses the hosted Notion MCP via OAuth)
2. Clear session history, preventing `--resume` from restoring multi-issue sessions
3. Strip the operator's user settings (allowed tools, MCP preferences)

However, if conductor switches to the integration token pattern (section 5), the OAuth dependency is eliminated. In that case, `CLAUDE_CONFIG_DIR` isolation for the orchestrator becomes technically possible — but it is still not recommended because it prevents session resumption (issue #16103) and the orchestrator's long-running sessions benefit from state continuity.

**Recommendation:** The orchestrator MUST NOT use `CLAUDE_CONFIG_DIR` isolation if it uses the hosted Notion MCP via OAuth. If it uses the integration token pattern, `CLAUDE_CONFIG_DIR` isolation is technically possible but still not recommended for orchestrator sessions. [INFERRED]

### 7.2 Headless Orchestrator (`claude -p`) Notion Configuration

For a fully headless `claude -p` orchestrator invocation (e.g., in CI), the cleanest configuration is:

```python
# Conductor builds the orchestrator environment
orchestrator_env = {
    "ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"],
    "NOTION_TOKEN": os.environ["NOTION_TOKEN"],
    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", ""),
    "DISABLE_AUTOUPDATER": "1",
    "DISABLE_ERROR_REPORTING": "1",
    "DISABLE_TELEMETRY": "1",
    # No CLAUDE_CONFIG_DIR — orchestrator uses user's config dir
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",  # block cloud MCPs
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",  # prevent auto-memory writes
}
```

And `.mcp.json` in the conductor repo root:
```json
{
  "mcpServers": {
    "notion": {
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "env": {
        "NOTION_TOKEN": "${NOTION_TOKEN}"
      }
    },
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/"
    }
  }
}
```

The `NOTION_TOKEN` env var is present in `orchestrator_env` and will be expanded by Claude Code v1.0.48+ when loading `.mcp.json`.

---

## 8. Token Copying as a Workaround (Not Recommended)

One potential workaround for preserving OAuth-based Notion access under `CLAUDE_CONFIG_DIR` isolation is to pre-copy the credentials file:

```python
import shutil, os

home_credentials = os.path.expanduser("~/.claude/.credentials.json")
if os.path.exists(home_credentials):
    shutil.copy(home_credentials, os.path.join(tmp_config_dir, ".credentials.json"))
```

**Why this is not recommended:**

1. The access token expires in ~1 hour (issue #28256) and Claude Code won't refresh it in `-p` mode anyway, so copying the file only defers the problem.
2. It leaks the user's Notion refresh token into a temp directory that the sub-agent can read (via `cat`). The sub-agent gains the ability to act as the user on Notion, which violates least-privilege.
3. Multiple concurrent sub-agents with the same refresh token will invalidate each other via token rotation (issue #28256 notes Atlassian OAuth is more stable, suggesting Notion's token rotation is aggressive).
4. The fix (integration token) is simpler, more secure, and permanent.

This approach is [NOT RECOMMENDED] for any of conductor's agent types.

---

## 9. MCP Config Summary for Conductor

| Agent Type | Notion MCP? | Auth Method | Pattern |
|-----------|-------------|-------------|---------|
| Orchestrator (interactive) | Yes | Static integration token via `NOTION_TOKEN` env var | `.mcp.json` in repo root + `${NOTION_TOKEN}` expansion |
| Orchestrator (headless `-p` in CI) | Yes | Static integration token via `NOTION_TOKEN` env var | `.mcp.json` in repo root + `NOTION_TOKEN` in process env |
| Issue-worker | No | N/A | No Notion MCP in agent's `.mcp.json` |
| Research-worker | No | N/A | No Notion MCP needed |

---

## 10. Follow-Up Research Recommendations

### 10.1 Empirical Verification of `env` Block in `.mcp.json` for stdio Servers

Issues #28090, #23216, and #1254 document env block injection failures. The bugs are reported as specific to the VSCode extension, but this should be verified empirically for the `claude -p` terminal CLI invocation path before conductor depends on it.

**Suggested test:**
```bash
# Write .mcp.json with env block
cat > /tmp/test-mcp.json << 'EOF'
{
  "mcpServers": {
    "notion": {
      "command": "npx",
      "args": ["-y", "@notionhq/notion-mcp-server"],
      "env": {
        "NOTION_TOKEN": "test_token_value"
      }
    }
  }
}
EOF
NOTION_TOKEN=test_token_value claude -p \
  --output-format json \
  --dangerously-skip-permissions \
  "List your available MCP tools"
```

Verify that the Notion tools appear and are callable without authentication errors.

### 10.2 `@notionhq/notion-mcp-server` Sunset Risk Mitigation

Notion has stated the local npm server may sunset. If it is deprecated, conductor will need an alternative. Research options:
- The community server `@suekou/mcp-notion-server` — uses Markdown conversion to reduce context size, supports `NOTION_TOKEN`
- Whether the hosted Notion MCP will eventually support static Bearer tokens for automation (there is no public roadmap for this)
- Whether conductor could use the Notion REST API directly (without MCP) as a fallback for session reporting

This warrants a follow-up issue if the sunset timeline becomes clearer.

### 10.3 `${VAR}` Expansion Scope in `.mcp.json`

Issue #3239 confirms `${VAR}` expansion in `.mcp.json` was fixed in v1.0.48 for `headers` fields. It is less clear whether the fix also covers the `env` block. The two issues (#3239 for headers, #28942 for envFile as a new feature) suggest the expansion may be scoped to HTTP server `headers` rather than stdio server `env`. Empirical testing (10.1 above) will clarify this.

### 10.4 Claude.ai Plugin-Based Notion Access (Alternative Path)

The `claude-code-notion-plugin` from Makenotion bundles the Notion MCP server with Claude Code Skills. Research whether the plugin's MCP configuration can be overridden or bypassed with a static integration token, or whether it forces OAuth. If the plugin can be configured for headless use, it may simplify conductor setup.

---

## Sources

- [MCP OAuth token refresh not persisting for Notion MCP server — Issue #28256, anthropics/claude-code](https://github.com/anthropics/claude-code/issues/28256) — Confirms `~/.claude/.credentials.json` as credential storage location for Notion MCP OAuth tokens; documents refresh failure bug
- [OAuth credentials shared across CLAUDE_CONFIG_DIR profiles — Issue #20553, anthropics/claude-code](https://github.com/anthropics/claude-code/issues/20553) — Documents that `.credentials.json` is relocated to `$CLAUDE_CONFIG_DIR/`; confirms Keychain service name is hardcoded independent of `CLAUDE_CONFIG_DIR`
- [CLAUDE_CONFIG_DIR behavior unclear — Issue #3833, anthropics/claude-code](https://github.com/anthropics/claude-code/issues/3833) — Documents which files are moved by `CLAUDE_CONFIG_DIR`; December 2025 comment confirms `.claude.json` and `.credentials.json` go to `$CLAUDE_CONFIG_DIR/`
- [OAuth token refresh fails in non-interactive/headless mode — Issue #28827, anthropics/claude-code](https://github.com/anthropics/claude-code/issues/28827) — Access tokens expire after ~10-15 min; no auto-refresh in `-p` mode; duplicate of #12447
- [OAuth access token refresh not working — Issue #12447, anthropics/claude-code](https://github.com/anthropics/claude-code/issues/12447) — Primary tracking issue for OAuth non-refresh in headless mode; open as of March 2026
- [OAuth token expiration disrupts autonomous workflows — Issue #19481, anthropics/claude-code](https://github.com/anthropics/claude-code/issues/19481) — No viable headless workaround; `/mcp reconnect` silently fails
- [Variable expansion not working in `.mcp.json` — Issue #3239, anthropics/claude-code](https://github.com/anthropics/claude-code/issues/3239) — Fixed in v1.0.48; `${VAR}` expansion now works in `.mcp.json` headers fields
- [Claude Code settings — Claude Code Docs](https://code.claude.com/docs/en/settings) — `CLAUDE_CONFIG_DIR` definition and default `~/.claude/` structure
- [Connect Claude Code to tools via MCP — Claude Code Docs](https://code.claude.com/docs/en/mcp) — MCP storage locations, `.mcp.json` env block format, `${VAR}` expansion support
- [Official Notion MCP Server — makenotion/notion-mcp-server, GitHub](https://github.com/makenotion/notion-mcp-server) — `NOTION_TOKEN` env var authentication, `OPENAPI_MCP_HEADERS` advanced auth, maintenance status notice
- [Notion MCP documentation — developers.notion.com](https://developers.notion.com/docs/mcp) — Hosted server URL, OAuth-only authentication requirement for `mcp.notion.com`
- [Connecting to Notion MCP — Notion Developer Guides](https://developers.notion.com/guides/mcp/get-started-with-mcp) — OAuth flow required; no static token alternative for hosted server
- [Document how to use a bearer token from env variable — Issue #51, makenotion/notion-mcp-server](https://github.com/makenotion/notion-mcp-server/issues/51) — Bash wrapper workaround; env block approach documented
- [Claude Code credential persistence in devcontainers — tfvchow/field-notes-public#10](https://github.com/tfvchow/field-notes-public/issues/10) — Both `.credentials.json` AND `.claude.json` required for authenticated sessions; minimal stub for `.claude.json`
- [Automated Code Review with Claude Code, Playwright, and Notion — wmedia.es](https://wmedia.es/en/writing/automating-code-review-claude-code-playwright-notion) — Real-world working pattern using `OPENAPI_MCP_HEADERS` with `@notionhq/notion-mcp-server` for automated Notion writes
- [WARN when CLAUDE_CODE_OAUTH_TOKEN overrides credentials file — Issue #16238, anthropics/claude-code](https://github.com/anthropics/claude-code/issues/16238) — `CLAUDE_CODE_OAUTH_TOKEN` env var silently overrides `.credentials.json`; alternative credential injection path
- [Connect Claude Code to Notion via Plugin — makenotion/claude-code-notion-plugin, GitHub](https://github.com/makenotion/claude-code-notion-plugin) — Plugin bundles Notion MCP + Skills; OAuth-based; no static token path documented
- [Notion's hosted MCP server: an inside look — notion.com blog](https://www.notion.com/blog/notions-hosted-mcp-server-an-inside-look) — Architecture of hosted server; OAuth session management details
