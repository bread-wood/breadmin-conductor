# Research: Credential Proxy Pattern for gh CLI in Conductor Sub-Agents

**Issue:** #17
**Milestone:** M1: Foundation
**Status:** Research Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Overview](#overview)
2. [Cross-References](#cross-references)
3. [gh CLI Credential Resolution Order](#gh-cli-credential-resolution-order)
4. [git Credential Helper Mechanism](#git-credential-helper-mechanism)
5. [Minimal Token Scope for Conductor Sub-Agents](#minimal-token-scope-for-conductor-sub-agents)
6. [Fine-Grained PAT vs. GitHub App Token](#fine-grained-pat-vs-github-app-token)
7. [Feasibility of a Credential Proxy](#feasibility-of-a-credential-proxy)
   - [Option A: Local HTTP/HTTPS Proxy (HTTPS_PROXY)](#option-a-local-httphttps-proxy-https_proxy)
   - [Option B: Unix-Socket Proxy (gh-native)](#option-b-unix-socket-proxy-gh-native)
   - [Option C: git Credential Helper Script](#option-c-git-credential-helper-script)
   - [Option D: Fine-Grained Token Scoping (No Proxy)](#option-d-fine-grained-token-scoping-no-proxy)
   - [Option E: GitHub App Installation Token per Dispatch](#option-e-github-app-installation-token-per-dispatch)
8. [How Claude Code on the Web Solves This](#how-claude-code-on-the-web-solves-this)
9. [Recommended Approach](#recommended-approach)
10. [Implementation Sketch](#implementation-sketch)
11. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
12. [Sources](#sources)

---

## Overview

This document answers the research question raised in issue #6 (Security Threat Model), section R-SEC-A: is it feasible to run a local proxy that intercepts `gh` CLI API calls and injects `GITHUB_TOKEN` so that the agent subprocess never holds the token directly? It also covers the simpler alternatives — token scoping and the git credential helper pattern — and recommends the approach that provides the best security-to-complexity tradeoff for conductor's current stage.

The central problem is T3 from the threat model: when conductor spawns `claude -p` as a subprocess, if `GITHUB_TOKEN` is present in the agent's environment, any successful prompt injection can exfiltrate it with `env` or `gh auth token`. The mitigations explored here break that connection.

---

## Cross-References

- **06-security-threat-model.md** — T3 is the threat this document addresses directly. The recommended mitigation in T3 is "use credential proxy pattern" and "block `gh auth token`." This document evaluates whether that proxy is feasible and what alternatives exist.
- **04-configuration.md** — Section 6.1 defines `build_agent_env()`, which strips the subprocess environment. The credential solution here integrates with that function — either the token is never added to the agent env dict, or it is replaced by a scoped short-lived token.
- **10-settings-mcp-injection.md** — Section 7.2 notes that `.mcp.json` files written to the worktree could contain MCP tokens readable by the agent. The credential isolation strategy here should be consistent with that concern: always prefer env-var expansion over hardcoded tokens in any file accessible in the worktree.

---

## gh CLI Credential Resolution Order

[DOCUMENTED] The `gh` CLI resolves authentication in this strict precedence order:

1. **`GH_TOKEN` environment variable** — takes highest precedence; overrides stored credentials.
2. **`GITHUB_TOKEN` environment variable** — second priority; also overrides stored credentials.
3. **`GH_ENTERPRISE_TOKEN`** / **`GITHUB_ENTERPRISE_TOKEN`** — for GitHub Enterprise Server hosts only.
4. **Stored credentials** — tokens previously saved by `gh auth login` and kept in the system keychain or `~/.config/gh/hosts.yml`.

If either `GH_TOKEN` or `GITHUB_TOKEN` is present in the process environment, gh uses that token for all API calls and does not consult the credential store. If neither is present, gh reads the stored token from `~/.config/gh/hosts.yml` (or system keychain, depending on platform).

**Key implication for conductor:** If the agent subprocess env dict contains `GH_TOKEN` or `GITHUB_TOKEN`, the agent can read the token with `Bash(env)`, `Bash(printenv)`, or `Bash(gh auth token)`. If neither variable is present but `~/.config/gh/hosts.yml` exists (from the operator's personal `gh auth login`), `gh` will still work because it reads from the credential store. In this case the token is not in the environment, but `gh auth token` still prints it — a separate exfiltration path.

**`GH_HOST` and base-URL redirect:** `GH_HOST` allows overriding which GitHub host gh targets, but it does not accept `http://` prefixes and cannot redirect API calls to a local HTTP-only endpoint. Attempts to use `GH_HOST=localhost:8080` cause malformed URLs. The `GH_ENTERPRISE_TOKEN` + direct `gh api 'http://localhost:...'` workaround exists but only affects explicit `gh api` calls, not the git credential path used by `git push`.

---

## git Credential Helper Mechanism

[DOCUMENTED] Git uses a layered credential system. When `git push` or `git fetch` needs to authenticate to `https://github.com`, it consults configured credential helpers in order:

1. The helper string is read from `git config credential.https://github.com.helper` (host-specific) or `git config credential.helper` (global).
2. Git invokes the helper with the argument `get` and writes credential context (protocol, host) to the helper's stdin in key=value format.
3. The helper responds on stdout with `username=<user>` and `password=<token>` lines.
4. Git uses the first helper that supplies both values.

**Shell snippet helpers (`!` prefix):** If the helper string begins with `!`, Git executes the rest as a shell snippet. This enables arbitrarily simple helpers without an installed binary:

```bash
git config credential.https://github.com.helper \
  '!f() { test "$1" = "get" && echo "password=TOKEN_HERE"; echo "username=x-token"; }; f'
```

**Key insight for credential proxying:** The credential helper is a shell command, not an environment variable. It runs in a child process of git (not of the agent) and can read from files, a Unix socket, or a local HTTP endpoint — places the agent cannot trivially access because they require knowing the socket path or file location, which the conductor controls.

**`GIT_CONFIG_GLOBAL` for per-process override:** Git reads the global config from `~/.gitconfig` by default. Setting the environment variable `GIT_CONFIG_GLOBAL=/path/to/agent-specific-gitconfig` makes that subprocess use a completely different global gitconfig — one that the conductor writes with a credential helper pointing to a token-supplying script, without touching the operator's `~/.gitconfig`. This is the cleanest per-process isolation mechanism.

[DOCUMENTED] The `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_N` / `GIT_CONFIG_VALUE_N` environment variables can also inject git config entries at runtime without touching any file:

```python
agent_env["GIT_CONFIG_COUNT"] = "1"
agent_env["GIT_CONFIG_KEY_0"] = "credential.https://github.com.helper"
agent_env["GIT_CONFIG_VALUE_0"] = "!/path/to/conductor-token-helper.sh"
```

This completely replaces the credential helper for that process without writing any config file.

---

## Minimal Token Scope for Conductor Sub-Agents

[DOCUMENTED] The operations a conductor sub-agent must perform are:

| Operation | Required Permission |
|-----------|-------------------|
| `git clone` / `git fetch` / `git pull` | Contents: read |
| `git push` (new branch) | Contents: write |
| `gh issue view <N>` | Issues: read |
| `gh issue list` | Issues: read |
| `gh pr create` | Pull requests: write |
| `gh pr view` | Pull requests: read |
| `gh pr checks` | Actions: read |
| `gh issue create` (follow-up issues) | Issues: write |

Operations the sub-agent must **not** be able to perform:

| Operation | Permission to Withhold |
|-----------|----------------------|
| `gh pr merge` | Pull requests: write is not sufficient; merge additionally requires branch protection bypass. Fine-grained tokens do not have a separate "merge" permission — they use pull requests: write. However, if merge is blocked at the branch protection level (require review, require CI), a token with pull requests: write cannot merge unilaterally. |
| `gh repo delete` | Administration: write — omit entirely |
| Push to `main` / protected branch | Requires branch protection bypass — do not grant |
| Read other repos | Scope token to **single repository** only |
| Read org settings, secrets | Leave all org-level permissions unset |

**Minimum fine-grained PAT for a sub-agent:**

| Permission | Level | Justification |
|-----------|-------|--------------|
| Contents | Read and write | git push to feature branch |
| Metadata | Read | Automatically required; not separately grantable |
| Pull requests | Read and write | gh pr create, gh pr view, gh pr checks |
| Issues | Read and write | gh issue view, gh issue list, gh issue create |
| Actions | Read | gh pr checks (workflow run status) |

**Repository scope:** A single named repository. Not the operator's full account.

[INFERRED] With fine-grained PAT scoped to one repo and only the above permissions, a compromised agent cannot: merge PRs (blocked by branch protection), delete the repo, read other repos, push to main (blocked by branch protection), or exfiltrate the org's secrets or settings.

---

## Fine-Grained PAT vs. GitHub App Token

### Fine-Grained Personal Access Token (PAT)

**Pros:**
- Easy to create (GitHub UI or API)
- Scoped to a single repository
- Permissions map 1:1 to the operations listed above
- No additional infrastructure (no GitHub App registration)
- Supported by `gh` CLI natively via `GH_TOKEN`

**Cons:**
- Lives indefinitely (configurable expiry of 1–90 days, but not hours)
- Tied to a specific GitHub user account; if that account is suspended, tokens stop working
- Cannot be programmatically refreshed without user interaction (no programmatic issuance from conductor)
- The token value is static — once stolen, it is valid until expiry or manual revocation
- `gh auth token` will print it if the agent has shell access

### GitHub App Installation Token

[DOCUMENTED] GitHub Apps generate installation access tokens that expire after **1 hour** (not 8 hours — the 8-hour figure applies to user access tokens, not installation tokens). The generation flow is:

1. Conductor holds the GitHub App **private key** (a PEM file) and the **App ID**.
2. Conductor generates a short-lived JWT (10-minute TTL) signed with the private key.
3. Conductor calls `POST /app/installations/{installation_id}/access_tokens` with the JWT.
4. GitHub returns an installation access token (1-hour TTL) scoped to specific repositories and permissions.
5. Conductor passes this token to the sub-agent via `GH_TOKEN`.

**Pros:**
- Short-lived (1-hour): reduces blast radius if exfiltrated
- Programmatically issuable by conductor without user interaction
- Scoped to exact repositories and permissions at issuance time
- Not tied to a human user account

**Cons:**
- Requires registering a GitHub App (one-time setup; not trivial for personal use)
- Conductor must hold the App private key (which is itself a high-value secret)
- Still passes the token via `GH_TOKEN`, so the agent can still read it via `env` unless the env is scrubbed
- gh CLI and git will still print it if the agent runs `gh auth token` or `cat ~/.config/gh/hosts.yml`
- Additional dependency (PyGithub or `jwt` library for token generation)
- Installation ID must be discovered and stored per-repo

**Verdict on GitHub App token vs. fine-grained PAT:** The GitHub App approach gives better token lifetime isolation (1 hour vs. weeks/months) and programmatic issuance — significant advantages. However, neither approach solves the core problem that the agent can read the token from its environment. The token is still in `GH_TOKEN`. The GitHub App approach is strictly better *if* the conductor is already using it for other purposes, but adding it solely for credential isolation is high overhead for uncertain gain unless the git credential helper approach (Option C below) is also used.

---

## Feasibility of a Credential Proxy

### Option A: Local HTTP/HTTPS Proxy (HTTPS_PROXY)

**Concept:** Conductor runs a local HTTPS proxy on `127.0.0.1:<port>`. The agent's subprocess env contains `HTTPS_PROXY=https://127.0.0.1:<port>`. The proxy intercepts API calls from `gh`, strips or ignores the Authorization header, and injects the real `GITHUB_TOKEN` before forwarding to `api.github.com`.

**gh CLI proxy support:** [DOCUMENTED] `gh` CLI (Go-based) natively respects `HTTPS_PROXY`, `HTTP_PROXY`, and `NO_PROXY` environment variables. The CLI passes all API calls through the Go `http.DefaultTransport`, which honors these env vars. A correctly configured proxy would intercept all outbound `gh` API calls.

**TLS interception problem:** `api.github.com` and `github.com` are served over TLS. To intercept the traffic, the proxy must perform TLS termination ("MITM"), which requires the proxy to present a self-signed certificate trusted by the gh CLI's certificate store. This is the Go standard library's certificate pool, which by default only trusts system CAs. Injecting a custom CA into a subprocess's trust store is platform-specific and fragile.

**Feasibility assessment:** [INFERRED] A TLS-intercepting proxy is architecturally sound but operationally expensive. On macOS (conductor's primary platform), the system CA store can be extended programmatically, but doing so affects all processes on the machine, not just the agent subprocess. Per-process CA injection for Go programs requires setting `SSL_CERT_FILE` or `GOSSL_*` environment variables — not well-standardized. The complexity is high; the risk of breaking legitimate HTTPS connections is real.

**Verdict: Not recommended for initial implementation.** This is the gold standard but has too much implementation complexity and fragility for the current stage.

### Option B: Unix-Socket Proxy (gh-native)

**Concept:** gh CLI has supported Unix socket transport since PR #3779 (June 2021). A local Unix socket could accept gh API calls and proxy them with injected auth.

**Problem:** The Unix socket support in `gh` is for *connecting to a GitHub host* via a socket (used for GitHub Enterprise in containerized environments), not for intercepting and rewriting authentication headers. The socket replaces the TCP dial but does not eliminate TLS — the protocol is still HTTPS over the socket. This does not help with credential injection without full TLS interception.

**Verdict: Not applicable.** The Unix socket feature does not solve the credential injection problem.

### Option C: git Credential Helper Script

[DOCUMENTED — partially verified] This is the most practical near-term approach for git operations (push/pull/fetch). It does not cover `gh` REST/GraphQL API calls, only the git protocol authentication.

**Mechanism:**

1. Conductor writes a credential helper script to a per-agent temp path (e.g., `/tmp/conductor-cred-<uuid>.sh`):
   ```bash
   #!/bin/bash
   if [ "$1" = "get" ]; then
     echo "password=$(cat /tmp/conductor-token-<uuid>)"
     echo "username=x-token"
   fi
   ```
2. Conductor writes the GitHub token to a separate temp file (`/tmp/conductor-token-<uuid>`) with mode `0600`.
3. Conductor sets in the agent's subprocess env:
   ```python
   agent_env["GIT_CONFIG_COUNT"] = "1"
   agent_env["GIT_CONFIG_KEY_0"] = "credential.https://github.com.helper"
   agent_env["GIT_CONFIG_VALUE_0"] = f"!/tmp/conductor-cred-{uuid}.sh"
   ```
4. `GH_TOKEN` and `GITHUB_TOKEN` are **not** set in the agent env.
5. When the agent runs `git push`, git invokes the credential helper, which reads the token from the temp file and prints it.
6. The agent cannot easily read `/tmp/conductor-token-<uuid>` via `cat` because: (a) the file path is not known unless the agent reads the git config or the env; (b) the agent may not have `Read(/tmp/*)` in its allowed tools; (c) even if it can read it, this is a weaker attack vector than `Bash(env)` or `Bash(gh auth token)`.

**Coverage gap:** This approach only covers git operations. `gh` CLI API calls (issue view, pr create, pr checks) will fail because `gh` has no analog of the git credential helper for its REST/GraphQL calls. `gh` requires `GH_TOKEN` or stored credentials. To cover `gh` calls, one of the following is needed:
- `GH_TOKEN` is still set (defeats the purpose for gh calls)
- Use a GitHub App installation token with a 1-hour TTL so the blast radius of exfiltration is bounded
- The agent uses the GitHub MCP (HTTP server) instead of the `gh` CLI for GitHub API operations

**The `~/.config/gh/hosts.yml` path:** If the agent's `CLAUDE_CONFIG_DIR` is isolated (as recommended in `10-settings-mcp-injection.md`), the agent's `HOME` may still point to the operator's home directory. `gh` reads `~/.config/gh/hosts.yml` from `HOME`, not from `CLAUDE_CONFIG_DIR`. If the operator is logged into `gh` on their machine, the token in `hosts.yml` is available to the agent even without `GH_TOKEN` in the env. Mitigation: set `GH_CONFIG_DIR` to a temp directory in the agent env:
  ```python
  agent_env["GH_CONFIG_DIR"] = "/tmp/conductor-gh-<uuid>"
  ```
  With `GH_CONFIG_DIR` pointing to an empty temp directory, `gh` finds no stored credentials and falls back to `GH_TOKEN` or fails. Combined with `GH_TOKEN` not being set, `gh` API calls fail (which is the desired behavior for an agent that should use scoped tokens only).

**Verdict: Recommended for git operations as part of a layered approach.** Does not solve `gh` API calls alone, but is an important component.

### Option D: Fine-Grained Token Scoping (No Proxy)

**Concept:** Pass `GH_TOKEN` to the agent, but use a fine-grained PAT scoped to one repository with only the exact permissions needed. Accept that the agent can read the token via `env`, but accept that what the token can do is constrained.

**Security gain:** Even if the token is exfiltrated:
- Attacker can only access the one repo the token is scoped to
- Attacker can push branches and create PRs but cannot merge (branch protection enforces this at the server)
- Attacker cannot delete the repo, access other repos, or modify org settings

**Security gap:** The token is still in the environment and can be exfiltrated and used externally. It does not protect against prompt injection followed by external API calls using the token after the session ends.

**Verdict: Necessary but not sufficient.** Fine-grained scoping is the baseline regardless of which other approach is used. It is not a substitute for keeping the token out of the env, but it significantly reduces blast radius.

### Option E: GitHub App Installation Token per Dispatch

**Concept:** Conductor generates a fresh 1-hour GitHub App installation token at dispatch time and passes it as `GH_TOKEN`. Agent can read it via `env`, but the token expires in 1 hour — before the operator would typically notice and rotate.

**Python implementation (with PyGithub):**
```python
from github import Auth, Github

def generate_scoped_token(app_id: int, private_key: str, installation_id: int, repo_name: str) -> str:
    """Generate a 1-hour installation token scoped to one repo with minimal permissions."""
    auth = Auth.AppAuth(app_id, private_key)
    gi = Github(auth=auth).get_github_for_installation(installation_id)
    token = gi.get_installation(installation_id).create_token(
        permissions={
            "contents": "write",
            "pull_requests": "write",
            "issues": "write",
            "actions": "read",
        },
        repositories=[repo_name],
    )
    return token.token
```

**Verdict: Recommended for teams or CI deployments** where the overhead of GitHub App registration is acceptable. Provides the best token-lifetime isolation. For personal/solo use, fine-grained PAT with short expiry (7 days) is a reasonable alternative until the GitHub App infrastructure is in place.

---

## How Claude Code on the Web Solves This

[DOCUMENTED] The official Claude Code on the web documentation explicitly describes its credential architecture:

> "Credential protection: Sensitive credentials (such as git credentials or signing keys) are never inside the sandbox with Claude Code. Authentication is handled through a secure proxy using scoped credentials."

The key quote from the GitHub proxy section:

> "The git client authenticates using a custom-built scoped credential. This proxy: Manages GitHub authentication securely — the git client uses a scoped credential inside the sandbox, which the proxy verifies and translates to your actual GitHub authentication token; Restricts git push operations to the current working branch for safety."

**What this means architecturally:** In the Anthropic cloud implementation:
1. The agent inside the sandbox has a **scoped credential** — a token that is not the real GitHub token, but a proxy-issued credential.
2. Git push/pull inside the sandbox uses this scoped credential.
3. A proxy service outside the sandbox verifies the scoped credential and translates it to the real GitHub token before forwarding to GitHub.
4. The agent never has the real token.
5. The proxy additionally enforces safety rules (restricts push to the current working branch).

This is the "gold standard" described in `06-security-threat-model.md` section R-SEC-A. The implementation requires a proxy service with its own credential issuance system — essentially a mini OAuth server that issues per-session, per-repo, branch-scoped tokens.

**Feasibility for conductor:** This level of proxy infrastructure is feasible but requires significant implementation effort:
- A Python HTTP server (e.g., using `http.server` or `aiohttp`) acting as a GitHub API proxy
- A custom credential issuance endpoint for the proxy
- TLS for the connection from the agent to the proxy (or use Unix sockets to avoid TLS)
- A scoped credential format (could be a signed JWT or a random UUID with a lookup table)
- Token injection into outgoing GitHub API calls

[INFERRED] For conductor's near-term use case (single operator machine, personal GitHub account), this full proxy is over-engineered. The simpler layered approach described in the Recommended Approach section below achieves most of the security benefit at a fraction of the complexity.

---

## Recommended Approach

Given the complexity-security tradeoff, the recommended credential isolation strategy for conductor sub-agents is a **layered defense** that combines four mechanisms:

### Layer 1: Never pass raw GITHUB_TOKEN in agent env

```python
ALLOWED_ENV_VARS = {
    "PATH", "HOME", "USER", "SHELL", "TERM", "LANG", "LC_ALL",
    "CONDUCTOR_MODEL", "CONDUCTOR_MAX_BUDGET", "CONDUCTOR_MAX_TURNS",
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
    # NOT: GH_TOKEN, GITHUB_TOKEN, ANTHROPIC_API_KEY (handled separately)
}
clean_env = {k: v for k, v in os.environ.items() if k in ALLOWED_ENV_VARS}
```

### Layer 2: Inject scoped token via git credential helper

Use `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_N` / `GIT_CONFIG_VALUE_N` to inject a shell-snippet credential helper that reads from a temp file. The temp file is written with mode `0600` by the conductor:

```python
import os, tempfile, uuid

def inject_git_credential_helper(agent_env: dict, github_token: str) -> str:
    """
    Write a token file and configure the git credential helper env vars.
    Returns the token file path for cleanup.
    """
    run_id = uuid.uuid4().hex[:12]
    # Write token to a temp file with restricted permissions
    fd, token_path = tempfile.mkstemp(prefix=f"conductor-token-{run_id}-", suffix=".txt")
    os.write(fd, github_token.encode())
    os.close(fd)
    os.chmod(token_path, 0o600)

    # Write credential helper script
    fd2, helper_path = tempfile.mkstemp(prefix=f"conductor-cred-{run_id}-", suffix=".sh")
    helper_script = f'#!/bin/bash\nif [ "$1" = "get" ]; then echo "password=$(cat {token_path})"; echo "username=x-token"; fi\n'
    os.write(fd2, helper_script.encode())
    os.close(fd2)
    os.chmod(helper_path, 0o700)

    # Inject into agent env via GIT_CONFIG_* env vars (no file write to ~/.gitconfig)
    agent_env["GIT_CONFIG_COUNT"] = "1"
    agent_env["GIT_CONFIG_KEY_0"] = "credential.https://github.com.helper"
    agent_env["GIT_CONFIG_VALUE_0"] = f"!{helper_path}"

    return token_path, helper_path
```

After the agent exits, clean up both temp files.

### Layer 3: Isolate gh config directory

Set `GH_CONFIG_DIR` to an empty temp directory so `gh` does not find stored credentials from the operator's `gh auth login`:

```python
gh_config_dir = tempfile.mkdtemp(prefix="conductor-gh-config-")
agent_env["GH_CONFIG_DIR"] = gh_config_dir
```

With no `GH_TOKEN` in env and no stored credentials in `GH_CONFIG_DIR`, `gh` API calls fail. This is intentional — the agent should not have `gh` API access unless a scoped token is explicitly provided.

**For `gh` API calls (pr create, issue view, etc.):** Provide a fine-grained PAT or GitHub App token scoped to the single repo, and set it as `GH_TOKEN` in the agent env. Accept that this token is visible via `env`, but its scope is limited. The `GIT_CONFIG_*` approach handles git operations; `GH_TOKEN` handles the `gh` CLI API operations.

### Layer 4: Denylist token-printing commands

In the `--disallowedTools` / `permissions.deny` configuration (already documented in `06-security-threat-model.md`):

```json
"Bash(gh auth token)",
"Bash(gh auth status)",
"Bash(cat ~/.config/gh/*)",
"Read(~/.config/gh/**)"
```

These are defense-in-depth additions that block the most direct exfiltration paths. They do not prevent `env` from showing `GH_TOKEN` if it is set, which is why Layers 1–3 are the primary defenses.

### Summary Table

| Mechanism | What it protects | Complexity | Recommended? |
|-----------|-----------------|------------|-------------|
| Strip `GH_TOKEN` / `GITHUB_TOKEN` from env | Prevents `env` / `printenv` exfiltration of raw token | Low | YES — always |
| `GIT_CONFIG_COUNT` credential helper injection | git push/pull without token in env | Low | YES — for git ops |
| `GH_CONFIG_DIR` isolation | Prevents gh reading operator's stored token | Low | YES — always |
| Fine-grained PAT for `gh` API calls | Limits blast radius if token is exfiltrated | Low | YES — as baseline |
| GitHub App installation token | Short-lived token for `gh` API calls | Medium | YES — for team/CI deployments |
| Full TLS-intercepting HTTP proxy | Agent never has any token | Very High | NOT YET — deferred |
| `Bash(gh auth token)` in denylist | Defense-in-depth against direct token printing | Low | YES — always |

---

## Implementation Sketch

Complete pattern for dispatching a sub-agent with credential isolation:

```python
import os, uuid, tempfile, subprocess

def dispatch_agent_with_credential_isolation(
    prompt: str,
    worktree_path: str,
    github_token: str,
    base_env: dict,
) -> subprocess.CompletedProcess:
    """
    Spawn a sub-agent with GitHub credentials isolated from the process env.

    git operations use a credential helper script (token not in env).
    gh CLI operations use a scoped GH_TOKEN (token in env, but scope limited).
    gh config directory is isolated (operator stored credentials not accessible).
    """
    run_id = uuid.uuid4().hex[:12]
    tmp_paths = []

    try:
        agent_env = _build_clean_env(base_env)

        # Layer 2: git credential helper via GIT_CONFIG_* env vars
        token_path, helper_path = _inject_git_credential_helper(
            agent_env, github_token, run_id
        )
        tmp_paths.extend([token_path, helper_path])

        # Layer 3: isolated gh config directory (no stored credentials)
        gh_config_dir = tempfile.mkdtemp(prefix=f"conductor-gh-config-{run_id}-")
        tmp_paths.append(gh_config_dir)
        agent_env["GH_CONFIG_DIR"] = gh_config_dir

        # Provide scoped token for gh API calls
        # This token is visible in the env, but is scoped to one repo
        agent_env["GH_TOKEN"] = github_token  # Use fine-grained PAT or App token

        # CLAUDE_CODE_DISABLE_AUTO_MEMORY and other standard isolation
        agent_env.update({
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_ERROR_REPORTING": "1",
        })

        return subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions", prompt],
            cwd=worktree_path,
            env=agent_env,
            capture_output=True,
            text=True,
        )
    finally:
        for path in tmp_paths:
            if os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                import shutil
                shutil.rmtree(path, ignore_errors=True)


def _build_clean_env(base_env: dict) -> dict:
    ALLOWED = {
        "PATH", "HOME", "USER", "SHELL", "TERM", "LANG", "LC_ALL",
        "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
    }
    return {k: v for k, v in base_env.items() if k in ALLOWED}


def _inject_git_credential_helper(agent_env: dict, token: str, run_id: str):
    fd, token_path = tempfile.mkstemp(prefix=f"cond-tok-{run_id}-")
    os.write(fd, token.encode())
    os.close(fd)
    os.chmod(token_path, 0o600)

    fd2, helper_path = tempfile.mkstemp(prefix=f"cond-cred-{run_id}-", suffix=".sh")
    script = (
        '#!/bin/bash\n'
        f'if [ "$1" = "get" ]; then echo "password=$(cat {token_path})"; '
        'echo "username=x-token"; fi\n'
    )
    os.write(fd2, script.encode())
    os.close(fd2)
    os.chmod(helper_path, 0o700)

    agent_env["GIT_CONFIG_COUNT"] = "1"
    agent_env["GIT_CONFIG_KEY_0"] = "credential.https://github.com.helper"
    agent_env["GIT_CONFIG_VALUE_0"] = f"!{helper_path}"

    return token_path, helper_path
```

**Note on `GH_TOKEN` still being in env:** The `GH_TOKEN` env var is still set for `gh` API calls. The git credential helper provides isolation for `git push` specifically (which is the highest-frequency operation), but `GH_TOKEN` remains in the env for gh's REST/GraphQL API. The denylist (`Bash(env)`, `Bash(printenv)`, `Bash(gh auth token)`) is still needed as defense-in-depth. A future upgrade path would replace `GH_TOKEN` in the env with a GitHub App installation token, minimizing the token lifetime from weeks/months to 1 hour.

---

## Follow-Up Research Recommendations

### R-CRED-A: Empirical test of `GIT_CONFIG_COUNT` in Claude Code subprocess

**Question:** Do the `GIT_CONFIG_COUNT` / `GIT_CONFIG_KEY_N` / `GIT_CONFIG_VALUE_N` environment variables correctly override the credential helper when the agent subprocess runs `git push`? Is there any interaction with Claude Code's own git configuration or with the worktree's `.git/config`?

**Why it matters:** The entire git credential isolation depends on this env var mechanism working correctly in a Claude Code subprocess. It needs to be verified empirically before being relied on for security.

**Suggested test:**
```bash
GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=credential.https://github.com.helper \
  GIT_CONFIG_VALUE_0='!echo password=TEST; echo username=x' \
  git credential fill <<< $'protocol=https\nhost=github.com'
# Expected output: password=TEST, username=x
```

### R-CRED-B: `GH_CONFIG_DIR` environment variable support

**Question:** Does the `gh` CLI reliably respect `GH_CONFIG_DIR` as a full replacement for `~/.config/gh/`? When `GH_CONFIG_DIR` points to an empty directory, does `gh` fall back to `GH_TOKEN` or error immediately?

**Why it matters:** If `gh` silently falls back to the operator's `~/.config/gh/hosts.yml` when `GH_CONFIG_DIR` is unset, the credential isolation has a gap.

**Suggested test:**
```bash
GH_CONFIG_DIR=/tmp/empty-gh-config GH_TOKEN="" gh auth status
# Expected: Error: not logged in
```

### R-CRED-C: Full credential proxy implementation feasibility

**Question:** Is a Python-based credential proxy (analogous to Claude Code on the web's GitHub proxy) feasible for a local conductor deployment? Specifically: can a Python `aiohttp`-based HTTPS proxy with a custom self-signed CA certificate intercept `gh` API calls without breaking TLS verification? Can the CA be injected per-subprocess via `SSL_CERT_FILE` or Go's `GOSSL_*` env vars?

**Why it matters:** The full proxy (Option A) is the gold-standard defense that eliminates `GH_TOKEN` from the agent env entirely. If the implementation complexity is manageable (a 200-300 line Python server), it becomes the right long-term solution.

### R-CRED-D: GitHub App infrastructure for conductor

**Question:** What is the minimal GitHub App configuration for conductor's use case? Can a single GitHub App installation cover multiple repos operated by the same user? What is the conductor-side flow for discovering `installation_id` per-repo?

**Why it matters:** GitHub App installation tokens (1-hour TTL) are the best credential type for `GH_TOKEN` in the near term. The setup overhead needs to be quantified and documented before recommending it as the default.

### R-CRED-E: Git credential helper security in `--dangerously-skip-permissions` mode

**Question:** If `--dangerously-skip-permissions` is active and `Read` is in the allowlist, can the agent read the credential helper script file to discover the token file path, then read the token file? Is the `Read(/tmp/*)` path blocked by default, or does the operator need to explicitly deny it?

**Why it matters:** The credential helper approach assumes the agent cannot trivially traverse from the helper env var to the token value. This assumption needs validation against Claude Code's actual `Read` tool behavior.

---

## Sources

- [GitHub CLI: Environment variables (GH_TOKEN, GITHUB_TOKEN, GH_HOST precedence)](https://cli.github.com/manual/gh_help_environment)
- [gh wants GH_TOKEN env variable even when logged in — cli/cli Discussion #8347](https://github.com/cli/cli/discussions/8347)
- [gh auth setup-git — GitHub CLI manual](https://cli.github.com/manual/gh_auth_setup-git)
- [GitHub - jongio/gh-setup-git-credential-helper: GitHub CLI Extension to add gh as a gitcredentials helper](https://github.com/jongio/gh-setup-git-credential-helper)
- [Git credential system documentation — gitcredentials(7)](https://git-scm.com/docs/gitcredentials)
- [Git credential fill/get protocol — git-credential(1)](https://git-scm.com/docs/git-credential)
- [Git credential helper storage overview](https://git-scm.com/doc/credential-helpers)
- [Git Credential Storage — Pro Git Book Chapter](https://git-scm.com/book/en/v2/Git-Tools-Credential-Storage)
- [Git environment variables (GIT_CONFIG_GLOBAL, GIT_CONFIG_COUNT)](https://git-scm.com/book/en/v2/Git-Internals-Environment-Variables)
- [git-config documentation (GIT_CONFIG_COUNT/KEY/VALUE mechanism)](https://git-scm.com/docs/git-config)
- [Permissions required for fine-grained personal access tokens — GitHub Docs](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)
- [Introducing fine-grained personal access tokens — GitHub Blog](https://github.blog/security/application-security/introducing-fine-grained-personal-access-tokens-for-github/)
- [Fine-grained PAT permissions discussion — community #133558](https://github.com/orgs/community/discussions/133558)
- [GitHub Actions: Control permissions for GITHUB_TOKEN — GitHub Changelog](https://github.blog/changelog/2021-04-20-github-actions-control-permissions-for-github_token/)
- [Use GITHUB_TOKEN for authentication in workflows — GitHub Docs](https://docs.github.com/actions/reference/authentication-in-a-workflow)
- [Differences between GitHub Apps and OAuth apps — GitHub Docs](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/differences-between-github-apps-and-oauth-apps)
- [Generating an installation access token for a GitHub App — GitHub Docs](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app)
- [Secure CI/CD with GitHub Apps Short Lived Tokens — Medium](https://medium.com/@devopswithyoge/secure-ci-cd-with-github-apps-short-lived-tokens-227d6e05c5fa)
- [Still Using PATs in 2025? Time to move to Github Apps — Bruno Terra](https://bmterra.eu/articles/010625-using-github-apps/)
- [PyGithub Authentication — AppInstallationAuth](https://pygithub.readthedocs.io/en/stable/examples/Authentication.html)
- [Document proxy support — cli/cli Issue #2037](https://github.com/cli/cli/issues/2037)
- [Allow use of a unix-socket forward proxy — cli/cli Issue #3268](https://github.com/cli/cli/issues/3268)
- [Better support for pointing gh at localhost — cli/cli Issue #8640](https://github.com/cli/cli/issues/8640)
- [How can Github CLI use a proxy to access Github? — cli/cli Discussion #7602](https://github.com/cli/cli/discussions/7602)
- [Claude Code on the web: Network access and security (GitHub proxy, scoped credentials)](https://code.claude.com/docs/en/claude-code-on-the-web)
- [Securely deploying AI agents — Claude Platform Docs](https://platform.claude.com/docs/en/agent-sdk/secure-deployment)
- [Bug: Subprocess inherits CLAUDECODE=1 env var — anthropics/claude-agent-sdk-python #573](https://github.com/anthropics/claude-agent-sdk-python/issues/573)
- [Security Bug Report: Claude Code Exposes Sensitive Environment Variables — claude-code Issue #11271](https://github.com/anthropics/claude-code/issues/11271)
- [persist-credentials in separate file breaks GitHub authentication for Git worktrees — actions/checkout #2318](https://github.com/actions/checkout/issues/2318)
