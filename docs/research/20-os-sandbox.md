# Research: OS-Level Sandbox for Research-Worker Agents with Domain-Allowlisted Web Access

**Issue:** #20
**Milestone:** M1: Foundation
**Status:** Research Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Background and Mandatory Context](#background-and-mandatory-context)
3. [macOS Sandbox Options](#macos-sandbox-options)
   - [sandbox-exec / Seatbelt (Deprecated but Active)](#sandbox-exec--seatbelt-deprecated-but-active)
   - [sandbox-runtime (Anthropic's Tool)](#sandbox-runtime-anthropics-tool)
   - [pfctl / pf Firewall](#pfctl--pf-firewall)
   - [macOS Network Extensions (App Sandbox)](#macos-network-extensions-app-sandbox)
4. [Linux Sandbox Options](#linux-sandbox-options)
   - [bubblewrap (bwrap)](#bubblewrap-bwrap)
   - [Landlock](#landlock)
   - [seccomp-bpf](#seccomp-bpf)
   - [Firejail](#firejail)
5. [Docker Per-Agent: Isolation Quality vs. Overhead](#docker-per-agent-isolation-quality-vs-overhead)
6. [Domain Allowlist: How Network Filtering Actually Works](#domain-allowlist-how-network-filtering-actually-works)
   - [The Proxy Architecture](#the-proxy-architecture)
   - [TLS and Domain Fronting Limitations](#tls-and-domain-fronting-limitations)
   - [DNS Exfiltration Bypass](#dns-exfiltration-bypass)
7. [Recommended Approach for macOS (Primary Platform)](#recommended-approach-for-macos-primary-platform)
8. [Recommended Approach for Linux (CI)](#recommended-approach-for-linux-ci)
9. [Domain Allowlist Configuration for Research Workers](#domain-allowlist-configuration-for-research-workers)
10. [Filesystem Write Restriction to Worktree Path](#filesystem-write-restriction-to-worktree-path)
11. [Impact on `claude -p` Process Functionality](#impact-on-claude--p-process-functionality)
12. [Per-Invocation Sandbox Configuration Mechanism](#per-invocation-sandbox-configuration-mechanism)
13. [Security Limitations and Residual Risks](#security-limitations-and-residual-risks)
14. [Cross-References to Prior Research](#cross-references-to-prior-research)
15. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
16. [Sources](#sources)

---

## Executive Summary

The critical context for this research is that `--allowedTools` allowlisting is **silently ignored** under `bypassPermissions` mode (confirmed bug #12232, closed not planned). This finding from doc 19 means OS-level sandboxing is not optional defense-in-depth — it is the **primary enforcement layer** for network and filesystem restrictions on conductor sub-agents.

The recommended approach for breadmin-conductor is:

- **macOS (development):** Use Anthropic's `sandbox-runtime` (`srt`) as a wrapper around `claude -p`, with a per-invocation `~/.srt-settings.json` written to a temporary path before each agent spawn. This uses `sandbox-exec` (Seatbelt) under the hood. Despite `sandbox-exec` being marked deprecated by Apple, it remains the only viable unprivileged OS-level sandbox on macOS, and Anthropic's own tooling depends on it.
- **Linux (CI):** Use `bubblewrap` either directly (via `sandbox-runtime`) or through Claude Code's built-in sandbox support. `bubblewrap` provides stronger, more reliable isolation than macOS Seatbelt.
- **Docker per-agent:** Viable but carries 550–600ms warm-start overhead per agent on macOS (due to the LinuxKit VM), making it impractical for research workers that spin up frequently. Docker is better suited as a persistent execution environment for an entire conductor session, not per-agent instantiation.

For research-worker domain allowlisting, network traffic is not inspected at the content level — filtering operates at the hostname level via a localhost proxy. The domain `github.com` grants access to all of github.com, which is a known risk. An attacker-controlled GitHub page can receive exfiltrated data as long as it's on an allowed domain. This is an accepted residual risk given the functional requirement for web access.

---

## Background and Mandatory Context

**Cross-reference: `06-security-threat-model.md`, T6 (Lethal Trifecta), R-SEC-E.**

Research-worker agents occupy the most dangerous position in the conductor architecture: they simultaneously hold credentials (process env), process untrusted content (web pages, GitHub issue bodies), and require external network access to function. The security threat model (doc 06) identified this as the "lethal trifecta" requiring OS-level sandboxing to break at least one leg.

**Cross-reference: `19-pretooluse-reliability.md`, Issue 4 (`--allowedTools` bypass).**

The `19-pretooluse-reliability.md` research confirmed that `--allowedTools` allowlisting is broken under `bypassPermissions` mode. Specifically:

> "The `--allowedTools` allowlist approach is broken when combined with `--permission-mode bypassPermissions`. Reported behavior: `Bash(curl ifconfig.me)` executed despite a `--allowedTools Read` allowlist."

This means the assumed Layer 3 software defense (allowedTools allowlist restricting the agent to only specific tools) provides no enforcement. The OS-level sandbox is therefore **mandatory** to enforce the constraints that `--allowedTools` cannot.

---

## macOS Sandbox Options

### sandbox-exec / Seatbelt (Deprecated but Active)

**Feasibility: VIABLE with caveats** [DOCUMENTED]

`sandbox-exec` is a command-line wrapper around Apple's Seatbelt kernel framework. It applies a sandbox profile written in a Scheme-derived policy language before executing a command. The sandbox profile specifies which system calls, file paths, and network operations are permitted.

**How it works for network restriction:**

The Seatbelt profile cannot filter network access by domain name — it operates at the socket/syscall level, which has no concept of hostnames or TLS SNI. A profile that allows network at all must allow it to any IP address. The workaround used by `sandbox-runtime` is:

1. The Seatbelt profile **blocks all outbound network** except connections to a specific localhost port (e.g., `127.0.0.1:3128`).
2. An HTTP proxy runs **outside the sandbox** on that localhost port.
3. All sandbox processes are configured to route HTTP and HTTPS traffic through this proxy via `HTTP_PROXY` and `HTTPS_PROXY` environment variables.
4. The proxy enforces the domain allowlist, refusing connections to non-allowed hosts.

This is a **hostname-based filter at the proxy layer**, not a kernel-level domain filter. The Seatbelt profile simply enforces "talk only to localhost proxy; nothing else."

**Deprecation status:** [DOCUMENTED]

Apple marked `sandbox-exec` as deprecated in its man page (since macOS 10.12). However:
- It remains functional and is not removed from macOS.
- Anthropic's `sandbox-runtime` depends on it as of early 2026.
- Internal Apple tools use it extensively, making removal unlikely in the near term.
- macOS 26 (Tahoe) is introducing a new `Containerization` framework that may eventually replace it.

**Key limitation:** The sandbox profile language is not publicly documented. Anthropic (and other tools like OpenAI Codex) uses profiles they have reverse-engineered or obtained internally. Writing correct profiles from scratch requires expertise. `sandbox-runtime` abstracts this by generating profiles programmatically.

**Child process inheritance:** Once applied, the sandbox is inherited by all child processes spawned by the sandboxed process. The sandbox cannot be removed from within the sandbox.

**Startup overhead:** Sub-millisecond. `sandbox-exec` is a thin syscall wrapper with no daemon.

### sandbox-runtime (Anthropic's Tool)

**Feasibility: RECOMMENDED for macOS** [DOCUMENTED]

`sandbox-runtime` (npm package `@anthropic-ai/sandbox-runtime`, CLI command `srt`) is Anthropic's open-source tool that wraps `sandbox-exec` on macOS and `bubblewrap` on Linux. It provides:

- A JSON configuration schema for filesystem and network rules
- A localhost HTTP proxy and SOCKS5 proxy for domain-level network filtering
- A CLI that writes the Seatbelt profile (macOS) or `bwrap` arguments (Linux) based on the config
- Per-invocation configuration via `--settings <path>`

**Configuration format (`~/.srt-settings.json`):**

```json
{
  "network": {
    "allowedDomains": [
      "github.com",
      "*.github.com",
      "api.github.com",
      "anthropic.com",
      "*.anthropic.com",
      "pypi.org",
      "*.pypi.org",
      "arxiv.org",
      "owasp.org",
      "genai.owasp.org",
      "docs.python.org",
      "packaging.python.org"
    ],
    "deniedDomains": [],
    "allowLocalBinding": false,
    "httpProxyPort": "auto",
    "socksProxyPort": "auto"
  },
  "filesystem": {
    "denyRead": ["~/.ssh", "~/.aws", "~/.kube", "~/.config/gcloud"],
    "allowWrite": ["./"],
    "denyWrite": [".env", ".claude/"]
  }
}
```

**Usage to wrap `claude -p`:**

```bash
srt --settings /tmp/srt-agent-N.json \
  claude -p "$PROMPT" \
  --dangerously-skip-permissions \
  --settings /tmp/claude-agent-N-settings.json
```

Where `/tmp/srt-agent-N.json` contains the per-agent sandbox policy and `/tmp/claude-agent-N-settings.json` contains the Claude Code settings (deny rules, hooks).

**Installation:**

```bash
npm install -g @anthropic-ai/sandbox-runtime
```

macOS: no additional dependencies (uses built-in `sandbox-exec`).
Linux: requires `bubblewrap`, `socat` (`apt install bubblewrap socat`).

### pfctl / pf Firewall

**Feasibility: NOT RECOMMENDED for per-agent use** [INFERRED]

macOS's `pf` packet filter (ported from OpenBSD) can enforce per-process or per-IP network restrictions. However:
- It requires root privileges to modify rules.
- It operates at the IP level, not the hostname level — domain filtering requires DNS resolution and dynamic rule management.
- Modifying `pf` rules atomically for each agent spawn/teardown is complex and fragile.
- `pf` rules are system-global, creating a race condition if multiple agents run concurrently.

`pfctl` is appropriate for system-level firewall policies but is not practical for per-agent, per-invocation sandboxing at the conductor layer.

### macOS Network Extensions (App Sandbox)

**Feasibility: NOT VIABLE for conductor's use case** [DOCUMENTED]

Apple's App Sandbox and Network Extension framework provide enterprise-grade network filtering, but they are restricted to signed, notarized applications distributed through the App Store or with explicit entitlements. They cannot be applied to arbitrary `claude -p` subprocess invocations by a third-party orchestrator. This approach is not viable for conductor.

---

## Linux Sandbox Options

### bubblewrap (bwrap)

**Feasibility: RECOMMENDED for Linux** [DOCUMENTED/TESTED]

`bubblewrap` (used by Flatpak) creates lightweight containers using Linux user namespaces without requiring root. It uses `clone(2)` with `CLONE_NEWNET`, `CLONE_NEWPID`, `CLONE_NEWNS`, and optionally `CLONE_NEWUSER`.

**Network restriction mechanism:**

`bubblewrap` uses `--unshare-net` to create a new network namespace with only a loopback interface. All external network is blocked. The `sandbox-runtime` implementation restores controlled access via:

1. `socat` forwards specific localhost ports in the sandbox to the host proxy.
2. `HTTP_PROXY`/`HTTPS_PROXY` environment variables point to those forwarded ports.
3. The host proxy enforces the domain allowlist.

**Performance benchmark:** [DOCUMENTED]

Community benchmarks show 100 bubblewrap invocations complete in ~0.374 seconds total (~3.7ms each), compared to ~11ms per Docker invocation. This is negligible overhead for per-agent use.

**Key advantages over macOS Seatbelt:**
- Not deprecated; actively maintained.
- Documented, open specification.
- Stronger filesystem isolation (bind mounts can overlay `/dev/null` over specific paths).
- Supported in CI environments (GitHub Actions, etc.) — though `CLONE_NEWUSER` requires specific kernel config in some container-based CI.

**GitHub Actions note:** [INFERRED]

Standard GitHub Actions runners (Ubuntu 22.04+) support bubblewrap with user namespaces. Some older configurations may require `sysctl kernel.unprivileged_userns_clone=1`. This should be verified empirically.

**Nested sandbox issue:** If conductor itself runs inside Docker, bubblewrap requires `--privileged` or user namespaces enabled in the outer container. `sandbox-runtime` has an `enableWeakerNestedSandbox` option that works inside Docker without privileges, but Anthropic explicitly documents that this "considerably weakens security." In CI where the outer runner is a VM (not a container), standard bubblewrap works without privilege escalation.

### Landlock

**Feasibility: SUPPLEMENTARY, not standalone** [DOCUMENTED]

Landlock is a Linux Security Module available from kernel 5.13+ (filesystem) and 6.4+ (network). It allows a process to restrict **itself** without requiring root or helper processes.

- Filesystem restrictions: available in kernel 5.13+
- Network restrictions (TCP connect/bind): added in kernel 6.4+

**Landlock network limitations for domain filtering:**

Landlock's network module blocks TCP connections by port, not by domain. `LANDLOCK_ACCESS_NET_CONNECT_TCP` blocks all outbound TCP connection attempts. It cannot express "allow connections to github.com but not attacker.com" — it does not parse DNS or SNI. Domain-level filtering still requires the proxy architecture.

**Use case for conductor:** Landlock can be combined with the proxy approach to add a redundant kernel-enforced layer that blocks direct TCP connections (preventing proxy bypass), while the proxy itself enforces the domain allowlist. This is defense-in-depth, not a standalone solution.

### seccomp-bpf

**Feasibility: SUPPLEMENTARY** [DOCUMENTED]

`seccomp-bpf` filters syscalls with BPF programs. It cannot filter by domain name (it does not understand network layer 3/4+). It can block socket creation syscalls entirely (`socket(AF_INET, ...)`) or restrict to specific socket families (`AF_UNIX` only for local IPC).

Used by Claude Cowork's Linux VM as an additional layer alongside bubblewrap. Not suitable as a standalone network filter, but adds defense-in-depth by blocking socket syscalls that don't go through the proxy.

### Firejail

**Feasibility: VIABLE but LESS PREFERRED than bubblewrap** [DOCUMENTED]

Firejail is an SUID sandbox that uses Linux namespaces and seccomp-bpf. It is easier to configure than raw bubblewrap but adds SUID complexity (a security tradeoff). It does not natively support domain-level network filtering — it uses `--net-none` for full network isolation or `--net=bridge` for network namespace with a virtual NIC.

Domain filtering with Firejail requires the same external proxy approach as bubblewrap. For conductor's use case, `bubblewrap` (either via `sandbox-runtime` or directly) is preferred because:
- No SUID requirement
- More granular control
- Already used by `sandbox-runtime`
- Well-tested with Claude Code specifically

---

## Docker Per-Agent: Isolation Quality vs. Overhead

**Isolation quality: EXCELLENT** [DOCUMENTED]

Docker containers provide the strongest isolation available without full VMs: separate network namespace, PID namespace, filesystem (OverlayFS), cgroups, and capability drops. Docker's `--network none` flag or a custom network with only a proxy container accessible gives full network isolation. Domain filtering via a sidecar proxy (e.g., `mitmproxy` or Squid in a `research-allowlist` network) is straightforward.

**Startup overhead on macOS: PROHIBITIVE for per-agent use** [DOCUMENTED]

On macOS, Docker runs inside a Linux VM (LinuxKit via HyperKit or Apple Virtualization Framework). Benchmark data:
- Warm container start (image already pulled): **550–600ms** on macOS Docker Desktop
- Cold start (image pull required): **2–5 seconds** depending on image size
- The variance is primarily from the VM/hypervisor layer, not image size

For comparison, `sandbox-runtime` (sandbox-exec on macOS) adds sub-millisecond overhead.

A conductor research worker that spawns every few minutes would accumulate 550ms overhead per agent — not catastrophic, but significant compared to the near-zero overhead of `sandbox-runtime`. More critically, Docker requires the Docker daemon to be running, which is a runtime dependency not present in all environments.

**Startup overhead on Linux (CI): ACCEPTABLE** [DOCUMENTED]

On Linux (no VM layer), warm Docker container starts are **300–600ms** (dominated by OverlayFS mount, namespace setup, and cgroup configuration, not image size). For research agents with multi-second execution times, this overhead may be acceptable.

**Recommendation:**

Use Docker as a **persistent session container** (one container per conductor run, not per agent spawn) rather than per-agent instantiation. Inside the container, use bubblewrap or sandbox-runtime for per-agent isolation. This amortizes Docker's startup cost across the full session.

The Claude Cowork architecture demonstrates this pattern: one persistent Linux VM running multiple Claude Code sessions simultaneously, each isolated by bubblewrap and seccomp, without per-session VM boot overhead.

---

## Domain Allowlist: How Network Filtering Actually Works

### The Proxy Architecture

`sandbox-runtime` and Claude Code's built-in sandbox do **not** perform kernel-level domain filtering. The mechanism is:

```
Sandboxed process
  │
  │  HTTP_PROXY=http://127.0.0.1:3128
  │  HTTPS_PROXY=http://127.0.0.1:3128
  ▼
localhost:3128 (HTTP proxy, outside sandbox)
  │
  │  proxy checks requested hostname against allowedDomains
  │
  ├── allowed? → forward to real destination
  └── denied?  → return 403 Forbidden
```

For macOS (Seatbelt):
- The sandbox profile blocks all `connect()` syscalls except to `127.0.0.1` on the proxy port.
- `HTTP_PROXY`/`HTTPS_PROXY` are injected into the sandboxed process's environment.
- Processes that respect these proxy environment variables route traffic through the allowlist filter.
- Processes that **don't** respect proxy env vars (e.g., apps that use raw sockets or hardcoded DNS) are blocked at the Seatbelt layer because their non-proxy connections fail with `EPERM`.

For Linux (bubblewrap):
- The `--unshare-net` flag removes all network interfaces from the process's network namespace (except loopback).
- `socat` creates a socket forwarding bridge between the sandbox's loopback and the host's proxy.
- The same `HTTP_PROXY` environment variables route traffic through the bridge to the host proxy.

**Important:** The domain allowlist filters at the **hostname** level, not the URL or path level. Allowing `github.com` permits access to any path on github.com, including attacker-controlled repositories and Gists that could receive exfiltrated data. The documentation explicitly warns: "Users should be aware of potential risks that come from allowing broad domains like `github.com` that may allow for data exfiltration."

### TLS and Domain Fronting Limitations

**The proxy does NOT inspect TLS/HTTPS traffic content.** [DOCUMENTED]

The network filtering system operates by filtering on the **CONNECT hostname** (for HTTPS) and **Host header** (for HTTP). It does not perform TLS termination or content inspection. This means:

- It cannot distinguish `github.com/legit-user/repo` from `github.com/attacker/exfil`.
- Domain fronting (using a CDN that serves multiple hostnames) can potentially bypass the filter, as documented in the Claude Code sandboxing security limitations.

The `allowedDomains` array supports wildcards: `*.github.com` allows all subdomains of github.com. Wildcards match the leftmost label only (`*.github.com` does not match `a.b.github.com`), consistent with standard glob behavior in the srt implementation.

### DNS Exfiltration Bypass

**A known vulnerability exists when `allowLocalBinding: true` is set.** [DOCUMENTED — sandbox-runtime issue #88]

When `allowLocalBinding` is enabled (required if the sandboxed process needs to bind a local server), an attacker can exfiltrate data through DNS resolution:
1. The attacker creates a domain `evil.com` with NS records delegating `*.secret.evil.com` to their DNS server.
2. The sandboxed process attempts to resolve `your-ssh-key.a.secret.evil.com`.
3. The recursive DNS resolver forwards the query to the attacker's nameserver.
4. The queried hostname itself contains the exfiltrated data.

For conductor's research-worker agents, `allowLocalBinding` should be `false` (default), which mitigates this issue. Research workers do not need to bind local ports.

---

## Recommended Approach for macOS (Primary Platform)

### Mechanism: sandbox-runtime wrapping claude -p

The recommended architecture for conductor on macOS is to use `sandbox-runtime` as a process wrapper:

```python
import json
import os
import subprocess
import tempfile

RESEARCH_WORKER_SANDBOX_CONFIG = {
    "network": {
        "allowedDomains": [
            "github.com",
            "*.github.com",
            "api.github.com",
            "anthropic.com",
            "*.anthropic.com",
            "code.claude.com",
            "platform.claude.com",
            "pypi.org",
            "files.pythonhosted.org",
            "arxiv.org",
            "owasp.org",
            "genai.owasp.org",
            "docs.python.org",
            "packaging.python.org",
            "npmjs.org",
            "registry.npmjs.org"
        ],
        "deniedDomains": [],
        "allowLocalBinding": False,
        "httpProxyPort": "auto",
        "socksProxyPort": "auto"
    },
    "filesystem": {
        "denyRead": [
            "~/.ssh",
            "~/.aws",
            "~/.kube",
            "~/.config/gcloud",
            "~/.npmrc",
            "~/.pypirc"
        ],
        "allowWrite": ["./"],   # worktree path only
        "denyWrite": [
            ".env",
            ".claude/",
            ".github/"
        ]
    }
}

def spawn_research_worker(worktree_path: str, prompt: str, agent_id: str) -> subprocess.Popen:
    # Write per-agent sandbox config to temp file
    srt_config_path = f"/tmp/srt-research-{agent_id}.json"
    with open(srt_config_path, "w") as f:
        json.dump(RESEARCH_WORKER_SANDBOX_CONFIG, f)

    # Write per-agent Claude settings (deny rules, hooks)
    claude_settings = {
        "permissions": {
            "deny": [
                "Bash(env)", "Bash(printenv)", "Bash(curl *)", "Bash(wget *)",
                "Bash(nc *)", "Bash(python -c *)", "Bash(bash -c *)",
                "Bash(eval *)", "Bash(exec *)", "Bash(rm -rf *)",
                "Bash(git push --force *)", "Bash(git push origin main *)",
                "Bash(gh pr merge *)", "Bash(gh issue edit *)",
                "Read(.env)", "Read(~/.aws/**)", "Read(~/.ssh/**)",
                "Edit(.github/**)", "Edit(.claude/**)",
            ]
        }
    }
    claude_settings_path = f"/tmp/claude-settings-{agent_id}.json"
    with open(claude_settings_path, "w") as f:
        json.dump(claude_settings, f)

    # Scrubbed environment (no secrets)
    clean_env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "USER": os.environ["USER"],
        "GIT_AUTHOR_NAME": "conductor-agent",
        "GIT_AUTHOR_EMAIL": "agent@conductor.local",
        "CLAUDE_CONFIG_DIR": f"/tmp/claude-config-{agent_id}",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }

    cmd = [
        "srt", "--settings", srt_config_path,
        "claude", "-p", prompt,
        "--dangerously-skip-permissions",
        "--settings", claude_settings_path,
    ]

    return subprocess.Popen(
        cmd,
        cwd=worktree_path,
        env=clean_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
```

### Why not built-in Claude Code sandbox?

Claude Code has a built-in `/sandbox` command that uses the same `sandbox-runtime` internally. The difference is that the built-in sandbox is configured via `~/.claude/settings.json` and is interactive — it prompts the user when a new domain is first requested.

For conductor's **headless, non-interactive** use case, the built-in sandbox prompting behavior is a blocker: it would halt the agent waiting for user input. Pre-populating `allowedDomains` in `settings.json` disables the prompting for known domains, but this is a global config that cannot be per-agent without `CLAUDE_CONFIG_DIR` isolation (which doc 10 covers). Using `srt` as a process wrapper is simpler, more explicit, and does not depend on Claude Code's internal sandbox activation state.

**Alternatively,** using `CLAUDE_CONFIG_DIR` isolation (write a per-agent `settings.json` with `sandbox.enabled: true` and `sandbox.allowedDomains: [...]` to a temp directory, then set `CLAUDE_CONFIG_DIR=/tmp/claude-config-<id>` in the subprocess environment) achieves the same result through Claude Code's built-in mechanism. This approach requires understanding the complete settings schema and how it interacts with the sandbox mode, which carries more unknowns than wrapping with `srt` directly.

---

## Recommended Approach for Linux (CI)

### Mechanism: sandbox-runtime (bubblewrap + socat + proxy)

The same `srt --settings ... claude -p ...` pattern works identically on Linux, with bubblewrap replacing sandbox-exec. The `srt` tool abstracts this difference.

**CI prerequisites:**

```yaml
# GitHub Actions example
- name: Install sandbox dependencies
  run: sudo apt-get install -y bubblewrap socat

- name: Install sandbox-runtime
  run: npm install -g @anthropic-ai/sandbox-runtime
```

**Nested container note:** If the CI runner itself runs inside Docker (some GitHub-hosted runners), `bubblewrap` may need `--allow-new-privileges` or user namespace support. `sandbox-runtime`'s `enableWeakerNestedSandbox: true` option handles this case but weakens isolation. Where possible, use VM-based CI runners (GitHub's standard Ubuntu runners are VM-based, not container-based).

### Alternative for Linux: Direct bubblewrap invocation

For environments where `srt` is not available or not desired, a direct `bwrap` wrapper is viable:

```bash
#!/bin/bash
# spawn-sandboxed-agent.sh
WORKTREE_PATH="$1"
AGENT_ID="$2"

# Start host-side proxy (outside sandbox)
# (In production, use a proper proxy; this is illustrative)
PROXY_PORT=3128

bwrap \
  --unshare-net \
  --bind "$WORKTREE_PATH" "$WORKTREE_PATH" \
  --ro-bind /usr /usr \
  --ro-bind /lib /lib \
  --ro-bind /bin /bin \
  --ro-bind /etc/resolv.conf /etc/resolv.conf \
  --dev /dev \
  --proc /proc \
  --tmpfs /tmp \
  --bind /home/user/.claude /home/user/.claude \
  --bind /run/user/"$(id -u)"/sandbox-$AGENT_ID.sock /run/sandbox.sock \
  --setenv HTTP_PROXY "http://127.0.0.1:$PROXY_PORT" \
  --setenv HTTPS_PROXY "http://127.0.0.1:$PROXY_PORT" \
  --die-with-parent \
  -- \
  claude -p "$PROMPT" --dangerously-skip-permissions
```

This approach requires separately managing the host-side proxy process. `sandbox-runtime` handles this complexity automatically.

---

## Domain Allowlist Configuration for Research Workers

The research-worker agent's legitimate access domains, grouped by function:

| Domain | Purpose | Risk Level |
|--------|---------|-----------|
| `github.com`, `*.github.com` | Issue reading, PR creation, code search | MEDIUM — broad domain; attacker can receive data via repos/Gists |
| `api.github.com` | GitHub REST/GraphQL API | MEDIUM — same risk as github.com |
| `anthropic.com`, `*.anthropic.com` | Anthropic docs, API | LOW — operator-controlled service |
| `code.claude.com`, `platform.claude.com` | Claude Code docs | LOW |
| `pypi.org`, `files.pythonhosted.org` | Python package docs | LOW |
| `arxiv.org` | Research papers | LOW — read-only source |
| `owasp.org`, `genai.owasp.org` | Security references | LOW |
| `docs.python.org` | Language reference | LOW |

**Domains NOT to allow by default:**
- `*.s3.amazonaws.com` — common exfiltration target
- `pastebin.com`, `gist.github.com` as write targets — note: `github.com` includes Gists, unavoidable without path-level filtering
- `ngrok.io`, `*.ngrok.io`, `*.tunnel.dev` — tunneling services
- `requestcatcher.com`, `webhook.site`, `canarytokens.org` — exfiltration testing services

**Configuration in srt settings:**

```json
{
  "network": {
    "allowedDomains": [
      "github.com",
      "*.github.com",
      "anthropic.com",
      "*.anthropic.com",
      "pypi.org",
      "files.pythonhosted.org",
      "arxiv.org",
      "owasp.org",
      "genai.owasp.org",
      "docs.python.org"
    ],
    "deniedDomains": [
      "ngrok.io",
      "*.ngrok.io",
      "requestcatcher.com",
      "webhook.site",
      "canarytokens.org"
    ]
  }
}
```

Note: `deniedDomains` takes precedence over `allowedDomains`. Explicit deny entries for known exfiltration services add a layer even if the main allowlist is later broadened.

**Per-invocation customization:** Research workers spawned for different tasks may need different domain sets. For example, a worker researching npm security needs `registry.npmjs.org`; one researching AWS patterns needs `docs.aws.amazon.com`. The `srt --settings <path>` mechanism allows the conductor to write a task-specific config file before each agent spawn, enabling per-agent domain scoping.

---

## Filesystem Write Restriction to Worktree Path

The sandbox-runtime filesystem configuration enforces:

```json
{
  "filesystem": {
    "allowWrite": ["./"]
  }
}
```

When `srt` is invoked with `cwd` set to the worktree path, `"./"` resolves to the worktree root. All write attempts outside this path fail with `EPERM` (macOS) or a namespace write error (Linux).

**Critical paths that must be blocked for write:**

| Path | Risk |
|------|------|
| `~/.bashrc`, `~/.zshrc`, `~/.profile` | Persistence via shell config |
| `~/.ssh/authorized_keys`, `~/.ssh/config` | SSH backdoor |
| `~/.aws/credentials` | Cloud credential write |
| `~/.claude/settings.json` | Override Claude Code security policy |
| `~/.gitconfig` | Git credential helper injection |
| `/etc/cron.d/`, `/etc/cron.daily/` | Cron persistence |

By setting `allowWrite: ["./"]` with no other entries, all of the above are blocked by default (deny-by-default write policy). The `denyRead` list provides an additional explicit block for reading sensitive files even if the write restriction doesn't apply to reads.

**Note on `denyRead`:** The sandbox-runtime default allows reading the entire filesystem. Explicit `denyRead` entries are required to block credential reads:

```json
{
  "filesystem": {
    "denyRead": [
      "~/.ssh",
      "~/.aws",
      "~/.kube",
      "~/.config/gcloud",
      "~/.netrc",
      "~/.npmrc",
      "~/.pypirc"
    ],
    "allowWrite": ["./"],
    "denyWrite": [".env", ".claude/", ".github/"]
  }
}
```

---

## Impact on `claude -p` Process Functionality

**Does sandboxing break claude -p functionality?** [PARTIALLY — known limitations documented]

The following table summarizes what works and what breaks under the research-worker sandbox config:

| Functionality | Status under Sandbox |
|--------------|---------------------|
| Anthropic API calls (LLM inference) | **Works** — `anthropic.com` is in allowedDomains |
| WebFetch to allowed domains | **Works** — routed through proxy |
| WebFetch to non-allowed domains | **Blocked** — 403 from proxy |
| GitHub API via `gh` CLI | **Works** — `api.github.com` is in allowedDomains (requires `GITHUB_TOKEN` in env or `gh` credential store accessible) |
| Git push/pull to github.com | **Works** — `github.com` is in allowedDomains |
| File reads within worktree | **Works** |
| File writes within worktree | **Works** |
| File writes outside worktree | **Blocked** |
| Read of `~/.ssh` | **Blocked** |
| Docker socket access | **Blocked** (not in allowedUnixSockets) |
| `npm install` (requiring network) | **Works** if npmjs.org added to allowedDomains |
| `pip install` (requiring network) | **Works** if pypi.org added to allowedDomains |
| `watchman` (used by jest) | **Broken** — documented incompatibility with sandbox; use `jest --no-watchman` |
| MCP servers that need external network | **Blocked** unless MCP server's domain is in allowedDomains |

**The escape hatch mechanism:** Claude Code's built-in sandbox has an intentional escape hatch: when a command fails due to sandbox restrictions, Claude may retry it with `dangerouslyDisableSandbox: true`, which bypasses the sandbox for that command (but still requires user permission, blocking in headless mode). For conductor's use case, this escape hatch should be **disabled** by setting `"allowUnsandboxedCommands": false` in the Claude Code settings. When using `srt` as an external wrapper, this escape hatch is irrelevant because `srt` applies restrictions at the OS level, not via Claude Code's internal sandbox layer.

**Key risk:** If `claude -p` itself spawns processes that don't respect `HTTP_PROXY` environment variables (e.g., Go binaries that use custom TLS stacks, some `git` operations with certificate checking), those processes may fail because they attempt direct connections that the Seatbelt/bwrap policy blocks. This is observable as unexpected connection failures in the agent's Bash commands. The typical fix is to add the required domain to `allowedDomains` or configure the tool's proxy settings explicitly.

---

## Per-Invocation Sandbox Configuration Mechanism

Conductor needs to configure each research-worker agent with a **different** sandbox policy (different allowed domains for different research tasks, different write paths for different worktrees).

**The recommended mechanism:**

1. Before spawning each agent, conductor writes two temp files:
   - `/tmp/srt-<agent-id>.json` — the `srt` sandbox policy
   - `/tmp/claude-settings-<agent-id>.json` — the Claude Code settings (deny rules, hooks)

2. Conductor invokes:
   ```bash
   srt --settings /tmp/srt-<agent-id>.json \
     claude -p "$PROMPT" \
     --dangerously-skip-permissions \
     --settings /tmp/claude-settings-<agent-id>.json
   ```

3. After the agent completes, conductor deletes the temp files.

**The `--settings` flag on `srt`** performs a deep merge with `~/.srt-settings.json` (the global default). To avoid inheriting unexpected global config, conductor should write complete policy files (not partial overrides) and ensure no inadvertently permissive entries exist in `~/.srt-settings.json`.

**Alternative via `CLAUDE_CONFIG_DIR`:** Rather than `srt --settings`, conductor can write a per-agent Claude Code settings file to an isolated config directory and set `CLAUDE_CONFIG_DIR=/tmp/claude-config-<agent-id>` in the subprocess environment. This activates Claude Code's **built-in** sandbox (which calls `srt` internally) based on the `sandbox.enabled: true` and `sandbox.network.allowedDomains: [...]` settings in that directory's `settings.json`. This approach ties sandbox activation to Claude Code's internal orchestration, with the tradeoff that it inherits Claude Code's sandbox escape hatch behavior unless explicitly disabled.

---

## Security Limitations and Residual Risks

**Cross-reference: `06-security-threat-model.md`, T6; `19-pretooluse-reliability.md`, Issue 4.**

After implementing OS-level sandboxing, the following residual risks remain:

| Risk | Residual Severity | Mitigation Available |
|------|-----------------|---------------------|
| Data exfiltration via allowed domain (e.g., github.com Gist write) | MEDIUM | Accepted risk; credential scrubbing reduces blast radius (doc 06 T3) |
| Domain fronting via CDN bypasses hostname filtering | LOW | No easy mitigation; add explicit `deniedDomains` for known fronting CDNs |
| DNS exfiltration via subdomain delegation | LOW (requires `allowLocalBinding: true`) | Keep `allowLocalBinding: false` |
| sandbox-exec deprecation and removal in future macOS | LONG-TERM | Monitor macOS 26 Containerization framework; plan migration |
| Nested sandbox weakness (Docker-inside-Docker in CI) | MEDIUM if triggered | Use VM-based CI runners; avoid nested container CI |
| Process injection via allowed Unix sockets | MEDIUM | Keep `allowUnixSockets: []` and `allowAllUnixSockets: false` |
| Weak network sandbox mode on Linux (enableWeakerNestedSandbox) | HIGH if enabled | Never enable unless in explicitly privileged context |
| Agent writes malicious code inside worktree (not blocked by sandbox) | MEDIUM | CI code scanning (truffleHog, gitleaks) before merge; see doc 06 T5 |

**The sandbox does not protect against:** An agent that writes malicious code into the worktree (which is in the allowed write path) and has that code later executed by CI or a human. The sandbox restricts the agent's **runtime** behavior, not the content of files it produces. This is covered by doc 06's Layer 5 (PostToolUse audit log, CI secret scanning, mandatory CI gate before merge).

---

## Cross-References to Prior Research

- **`06-security-threat-model.md`:** The defense architecture in that document listed OS-level sandbox as Layer 4 (optional defense-in-depth). Given the findings from doc 19 that `--allowedTools` is broken under `bypassPermissions`, Layer 4 must be **reclassified as mandatory**. The `sandbox-runtime` configuration described in this document is the concrete implementation of Layer 4 for both macOS and Linux.

- **`19-pretooluse-reliability.md`:** The critical finding that `--allowedTools` is silently ignored under `bypassPermissions` (issue #12232) makes OS sandboxing the only reliable network enforcement layer. This document provides the implementation path. The revised defense architecture in doc 19 already reclassifies Layer 4 as "MANDATORY — not optional."

- **`04-configuration.md`:** The `CLAUDE_CONFIG_DIR` isolation approach for per-agent settings files is the mechanism by which per-agent sandbox config can be injected into Claude Code's built-in sandbox path. The two approaches (external `srt` wrapper vs. `CLAUDE_CONFIG_DIR` settings injection) are complementary.

- **`10-settings-mcp-injection.md`:** MCP servers may require network access that the research-worker domain allowlist would block. Any MCP server domains need to be added to the `allowedDomains` list for that agent. Issue 10 covers the mechanism for injecting per-agent MCP config.

---

## Follow-Up Research Recommendations

### R-20-A: Empirical Smoke Test of sandbox-runtime with claude -p

**Question:** Does `srt --settings <config> claude -p "$PROMPT" --dangerously-skip-permissions` correctly:
1. Block `WebFetch` to a domain not in `allowedDomains` (e.g., `example.com`)?
2. Allow `WebFetch` to `github.com` when it is in `allowedDomains`?
3. Block `Bash(curl https://non-allowed.com)`?
4. Block writes to `~/.bashrc` from inside the agent?
5. Allow writes to the worktree path?

**Why this matters:** The entire security model depends on this integration working correctly. Existing documentation confirms the mechanism [DOCUMENTED], but empirical verification would upgrade critical claims to [TESTED]. Suggested as part of the M1 empirical verification suite (issue #41).

**Confidence in current recommendation:** [DOCUMENTED] — based on Anthropic's engineering blog, the official sandboxing docs, and the sambaiz.net bubblewrap analysis. Not [TESTED] in the conductor context.

### R-20-B: CLAUDE_CONFIG_DIR-Based Sandbox Activation vs. External srt Wrapper

**Question:** Is the `CLAUDE_CONFIG_DIR` approach (activating Claude Code's built-in sandbox via per-agent settings) equivalent in security to the external `srt` wrapper approach? Specifically:
- Does `sandbox.enabled: true` in a temp `CLAUDE_CONFIG_DIR` reliably activate Seatbelt on macOS?
- Does the escape hatch (`allowUnsandboxedCommands`) fire in headless `-p` mode (which would block if it prompts)?
- Which approach is more maintainable for conductor?

**Why this matters:** Two viable implementation paths exist. Choosing the wrong one could introduce subtle security gaps or operational friction.

### R-20-C: GitHub Actions Runner Namespace Support

**Question:** Do GitHub Actions standard Ubuntu runners (ubuntu-22.04, ubuntu-24.04) support `CLONE_NEWUSER` unprivileged user namespaces, which bubblewrap requires?

**Why this matters:** If GitHub Actions runners don't support unprivileged user namespaces, `sandbox-runtime` on Linux CI requires `enableWeakerNestedSandbox: true`, which significantly weakens the isolation. Knowing this determines whether conductor's CI security is meaningfully weaker than its development environment security.

### R-20-D: macOS 26 Containerization Framework as sandbox-exec Replacement

**Question:** Apple's macOS 26 (Tahoe) introduces a `Containerization` framework with sub-second container startup. Does this provide a viable path to replace `sandbox-exec` before Apple removes it? What is the API surface for programmatic container creation by a Python subprocess orchestrator?

**Why this matters:** `sandbox-exec` deprecation is a long-term risk. Planning a migration path ahead of any removal ensures continuity.

### R-20-E: Domain-Level vs. Path-Level Filtering via mitmproxy

**Question:** Can a local `mitmproxy` instance perform path-level filtering (blocking `github.com/gist/*` as a write endpoint, while allowing `github.com/user/repo`), and what is the configuration overhead? Does TLS interception with a self-signed CA work reliably with Python's `httpx`/`requests` and `curl` inside the sandbox?

**Why this matters:** Path-level filtering would close the "exfiltrate to allowed domain" residual risk. The feasibility and overhead determine whether it's worth pursuing.

---

## Sources

- [Sandboxing — Claude Code Docs](https://code.claude.com/docs/en/sandboxing) — Complete sandbox documentation: Seatbelt/bubblewrap mechanism, filesystem/network configuration schema, allowedDomains, escape hatch behavior, security limitations including domain fronting warning
- [Making Claude Code More Secure and Autonomous — Anthropic Engineering](https://www.anthropic.com/engineering/claude-code-sandboxing) — Architecture overview: proxy mechanism, 84% permission prompt reduction, filesystem and network dual-layer isolation
- [sandbox-runtime — GitHub (anthropic-experimental)](https://github.com/anthropic-experimental/sandbox-runtime) — Configuration schema, network allowedDomains syntax, wildcard support, per-invocation `--settings` flag, platform-specific behavior (macOS: sandbox-exec profiles; Linux: bwrap + socat)
- [sandbox-runtime — npm package (@anthropic-ai/sandbox-runtime)](https://www.npmjs.com/package/@anthropic-ai/sandbox-runtime) — Library API, SandboxManager TypeScript interface
- [Sandboxing Claude Code on macOS: What I Actually Found — Infralovers](https://www.infralovers.com/blog/2026-02-15-sandboxing-claude-code-macos/) — macOS-specific analysis: Seatbelt deprecation concern, Docker conflicts, domain proxy mechanism
- [A deep dive on agent sandboxes — Pierce Freeman](https://pierce.dev/notes/a-deep-dive-on-agent-sandboxes) — Comparative analysis: macOS Seatbelt "binary network" limitation, Linux Landlock/seccomp advantages, Seatbelt cannot do domain-level filtering natively
- [Trying out bubblewrap used in Claude Code's Sandbox Runtime — sambaiz.net](https://www.sambaiz.net/en/article/547/) — Bubblewrap mechanism: `--unshare-net`, CLONE_NEWNET, socat socket forwarding, proxy-based domain filtering, benchmark: 100 invocations in 0.374s
- [Sandbox Coding Agents Securely With Bubblewrap — 2k-or-nothing.com](https://2k-or-nothing.com/posts/Sandbox-Coding-Agents-Securely-With-Bubblewrap) — Practical bubblewrap configuration for coding agents: bind mount patterns, secret file overlays with /dev/null, running claude -p with --dangerously-skip-permissions under bwrap
- [Inside Claude Cowork — PVIEITO](https://pvieito.com/2026/01/inside-claude-cowork) — VM-based sandboxing reference architecture: Ubuntu 22.04 VM, bubblewrap per session, seccomp, ports 3128/1080 proxy, allowlist: api.anthropic.com + package registries, direct DNS blocked
- [GitHub Issue #88: DNS Exfiltration via subdomain delegation when allowLocalBinding: true — anthropic-experimental/sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime/issues/88) — DNS exfiltration vulnerability analysis and mitigation (keep allowLocalBinding: false)
- [Domain fronting security warning — GitHub Issue #20255, anthropics/claude-code](https://github.com/anthropics/claude-code/issues/20255) — Domain fronting bypass of sandbox network filtering; documented as known limitation
- [Hardening with Firejail, Landlock, and bubblewrap — Tamas Sallai, Medium](https://medium.com/@tamas.sallai/hardening-with-firejail-landlock-and-bubblewrap-5d0a63155f95) — Comparison of Linux sandboxing options; Landlock network added in kernel 6.4
- [Sandboxing Network Tools with Landlock — domcyrus.dev](https://domcyrus.github.io/systems-programming/security/linux/2025/12/06/landlock-sandboxing-network-tools.html) — Landlock TCP network restriction: LANDLOCK_ACCESS_NET_CONNECT_TCP, kernel 6.4+ requirement, limitation: port-level not domain-level
- [Linux Landlock and seccomp — openai/codex analysis](https://zread.ai/openai/codex/14-linux-landlock-and-seccomp) — Codex sandbox implementation using Landlock + seccomp-bpf; codex-linux-sandbox helper binary pattern
- [Decomposing Docker Container Startup Performance — arXiv:2602.15214](https://arxiv.org/abs/2602.15214) — Docker warm-start latency: 554–568ms on Premium SSD; macOS shows 21.7% more variance from LinuxKit VM overhead
- [Claude Code Sandbox Guide — claudefa.st](https://claudefa.st/blog/guide/sandboxing-guide) — Settings schema for sandbox section, allowedDomains array configuration, auto-allow mode for headless operation
- [OS-Level Sandboxing for Kiro CLI — GitHub Issue #5658, kirodotdev/Kiro](https://github.com/kirodotdev/Kiro/issues/5658) — Alternative perspective on agent sandbox requirements; confirms sandbox-runtime applicability to claude -p patterns
- [HN: macOS sandbox-exec / seatbelt deprecated status (2025)](https://news.ycombinator.com/item?id=44283454) — Community analysis: Apple internal tools use it heavily; macOS 26 Containerization as potential future replacement
- [sandbox-exec: macOS's Little-Known Command-Line Sandboxing Tool — igorstechnoclub.com](https://igorstechnoclub.com/sandbox-exec/) — Seatbelt profile language overview; child process inheritance behavior
- [GitHub - firejail: Linux namespaces and seccomp-bpf sandbox](https://github.com/netblue30/firejail) — Firejail: SUID sandbox, --net-none for full network isolation, DNS via fdns proxy
- [Bubblewrap — ArchWiki](https://wiki.archlinux.org/title/Bubblewrap) — Technical reference for bwrap flags, unprivileged namespace requirements
- [19-pretooluse-reliability.md — breadmin-conductor](docs/research/19-pretooluse-reliability.md) — Critical context: `--allowedTools` allowlist is broken under bypassPermissions (issue #12232), making OS sandbox the mandatory enforcement layer
- [06-security-threat-model.md — breadmin-conductor](docs/research/06-security-threat-model.md) — Security threat model that spawned R-SEC-E; T6 lethal trifecta requiring OS-level sandboxing to break the exfiltration leg
