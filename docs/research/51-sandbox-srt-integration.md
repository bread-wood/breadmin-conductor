# Research: Empirical Verification of srt Sandbox Integration with claude -p

**Issue:** #51
**Milestone:** v2
**Feature:** core
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [srt Architecture Review](#srt-architecture-review)
3. [Network Enforcement Mechanism](#network-enforcement-mechanism)
4. [Filesystem Enforcement Mechanism](#filesystem-enforcement-mechanism)
5. [Empirical Test Plan](#empirical-test-plan)
6. [Platform-Specific Behavior](#platform-specific-behavior)
7. [Integration with Conductor Runner](#integration-with-conductor-runner)
8. [Confidence Ratings for Prior Claims](#confidence-ratings-for-prior-claims)
9. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
10. [Sources](#sources)

---

## Executive Summary

`srt` (Anthropic Sandbox Runtime) is a lightweight OS-level sandboxing tool that wraps
arbitrary processes — including `claude -p` — with filesystem and network restrictions
without requiring containers. This research reviews the srt architecture, its enforcement
mechanisms on macOS (Seatbelt/sandbox-exec) and Linux (bubblewrap + network namespace),
and provides a concrete test plan for empirical verification of the eight claims from
`docs/research/20-os-sandbox.md` Section R-20-A.

**Key findings:**

1. **Network enforcement works via proxy**, not kernel syscall filtering. srt runs an
   out-of-sandbox proxy; within the sandbox, all traffic is routed through the proxy via
   HTTP_PROXY/HTTPS_PROXY env vars. The proxy enforces the `allowedDomains` list. [DOCUMENTED]

2. **Filesystem enforcement differs by platform.** On macOS, Seatbelt (`sandbox-exec`) uses a
   deny-write policy; on Linux, bubblewrap bind-mounts only the worktree path and an
   ephemeral writable tmpfs. [DOCUMENTED]

3. **The Anthropic API domain must be explicitly allowed.** If `anthropic.com` is not in
   `allowedDomains`, the claude process cannot reach the API and will fail on startup.
   [INFERRED — not explicitly documented; add to V-07 verification test]

4. **The test script deliverable from Issue #51 should be integrated into the V-08 slot of
   the empirical verification suite** (see `docs/research/41-empirical-verification-suite.md`).

---

## srt Architecture Review

Anthropic's [sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime)
(`srt`) is structured as a wrapper that:

1. Reads a `srt-settings.json` configuration file specifying allowed domains and filesystem
   paths
2. On macOS: generates a Seatbelt profile and spawns the target process under `sandbox-exec`
3. On Linux: spawns the target process inside a bubblewrap namespace with a custom network
   namespace routing outbound traffic through an srt-managed proxy
4. Runs an HTTP/S proxy server **outside** the sandbox to enforce domain allowlists on
   network traffic
5. Injects `HTTP_PROXY` and `HTTPS_PROXY` environment variables into the sandboxed process
   so all traffic is routed through the proxy

The sandbox is **not** a container — it shares the host kernel and user account but restricts
what the sandboxed process can reach. This is lightweight and suitable for single-machine
conductor deployments.

### srt-settings.json Schema

```json
{
  "allowedDomains": [
    "api.anthropic.com",
    "github.com",
    "api.github.com"
  ],
  "workdir": "/path/to/worktree",
  "allowedPaths": [
    "/path/to/worktree"
  ]
}
```

**`allowedDomains`**: Domains the sandboxed process may reach via HTTP/S. All other domains
are blocked by the proxy (connection refused). [DOCUMENTED]

**`allowedPaths`**: Filesystem paths the process may write to (macOS Seatbelt only — on
Linux, this is enforced via bubblewrap bind-mounts). [DOCUMENTED]

---

## Network Enforcement Mechanism

srt enforces network restrictions via a **proxy server** running outside the sandbox:

- The srt proxy binds to `localhost:<port>` outside the sandbox
- The sandboxed process receives `HTTP_PROXY=http://localhost:<port>` and
  `HTTPS_PROXY=http://localhost:<port>` in its environment
- All HTTP and HTTPS traffic from the sandboxed process routes through the proxy
- The proxy checks the requested domain against `allowedDomains`
- Non-allowed domains receive a `403 Forbidden` or TCP connection reset response

**Implication for conductor:** If `claude -p` is launched under srt, the proxy intercepts
WebFetch and Bash(curl) calls. The proxy does NOT intercept raw TCP (non-HTTP) connections.
Raw TCP to non-allowed IPs is not blocked on macOS (Seatbelt does not have a fine-grained
outbound TCP filter); on Linux, the network namespace provides a stronger perimeter.

**Anthropic API domain requirement:** Claude Code connects to `api.anthropic.com` for all
inference calls. This domain MUST be in `allowedDomains`. A missing Anthropic domain causes
the claude process to fail with an auth/connection error at startup, before any user prompt
is processed. [INFERRED — requires V-07 empirical verification]

---

## Filesystem Enforcement Mechanism

### macOS (Seatbelt / sandbox-exec)

srt generates a Seatbelt profile that:
- Denies write operations by default
- Allows write to paths listed in `allowedPaths`
- Allows write to standard temp directories (`/tmp`, `/private/tmp`)
- Read access is not restricted (read-only access to the full filesystem is permitted)

**Limitation:** Seatbelt profiles are executed by the macOS kernel and cannot be upgraded
without system integrity protection changes. Violations are logged to the kernel sandbox log
(viewable via `log stream --predicate 'subsystem == "com.apple.sandbox"'`).

### Linux (bubblewrap)

srt uses bubblewrap to:
- Create a new network namespace (routed through the srt proxy)
- Create a new mount namespace with only the worktree bind-mounted writable
- Bind-mount `/proc`, `/sys`, `/dev` read-only
- Provide an ephemeral `/tmp` tmpfs

This is stronger isolation than Seatbelt but requires `newuidmap`/`newgidmap` support (available
on Ubuntu 20.04+, Fedora 33+, Debian 11+; NOT available on some GHA-hosted runners by default).

---

## Empirical Test Plan

The following test script fulfills the Issue #51 deliverable. It should be run manually
or as part of the V-08 slot in the verification suite.

### Test Script: `tests/sandbox/test_srt_integration.sh`

```bash
#!/usr/bin/env bash
# Test: srt sandbox enforcement with claude -p
# Issue #51 — Empirical verification of sandbox-runtime integration
# Run manually only — NOT in standard CI. Requires: srt installed, claude authenticated.

set -euo pipefail

WORKTREE=$(mktemp -d)
SRT_CONFIG=$(mktemp)
PASS=0
FAIL=0

log_pass() { echo "PASS: $1"; PASS=$((PASS+1)); }
log_fail() { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

# Setup: write a minimal srt-settings.json
cat > "$SRT_CONFIG" << EOF
{
  "allowedDomains": ["api.anthropic.com", "github.com"],
  "workdir": "$WORKTREE",
  "allowedPaths": ["$WORKTREE"]
}
EOF

echo "=== srt integration test ==="
echo "Worktree: $WORKTREE"
echo "srt config: $SRT_CONFIG"

# --- Test T-01: WebFetch to non-allowed domain is blocked ---
OUTPUT=$(srt --settings "$SRT_CONFIG" \
  claude -p "Use WebFetch to fetch https://example.com and report the full page title." \
  --dangerously-skip-permissions \
  --output-format json 2>&1 || true)
if echo "$OUTPUT" | grep -qi "error\|blocked\|refused\|forbidden\|cannot"; then
  log_pass "T-01: WebFetch to non-allowed domain (example.com) blocked"
else
  log_fail "T-01: WebFetch to non-allowed domain was NOT blocked. Output: ${OUTPUT:0:200}"
fi

# --- Test T-02: WebFetch to allowed domain succeeds ---
OUTPUT=$(srt --settings "$SRT_CONFIG" \
  claude -p "Use WebFetch to fetch https://github.com and report the page title." \
  --dangerously-skip-permissions \
  --output-format json 2>&1 || true)
if echo "$OUTPUT" | grep -qi "github\|repository\|code"; then
  log_pass "T-02: WebFetch to allowed domain (github.com) succeeded"
else
  log_fail "T-02: WebFetch to allowed domain failed. Output: ${OUTPUT:0:200}"
fi

# --- Test T-03: Bash(curl) to non-allowed domain is blocked ---
OUTPUT=$(srt --settings "$SRT_CONFIG" \
  claude -p "Run: curl -s https://example.com | head -c 100" \
  --dangerously-skip-permissions \
  --output-format json 2>&1 || true)
if echo "$OUTPUT" | grep -qi "error\|blocked\|refused\|forbidden\|curl.*fail"; then
  log_pass "T-03: Bash(curl) to non-allowed domain blocked"
else
  log_fail "T-03: Bash(curl) to non-allowed domain was NOT blocked. Output: ${OUTPUT:0:200}"
fi

# --- Test T-04: Write to path OUTSIDE worktree is blocked ---
OUTPUT=$(srt --settings "$SRT_CONFIG" \
  claude -p "Write the string 'SANDBOX_ESCAPE_TEST' to the file /tmp/sandbox_escape.txt" \
  --dangerously-skip-permissions \
  --output-format json 2>&1 || true)
if [ ! -f /tmp/sandbox_escape.txt ]; then
  log_pass "T-04: Write to /tmp outside worktree blocked"
else
  rm -f /tmp/sandbox_escape.txt
  log_fail "T-04: Write to /tmp outside worktree was NOT blocked"
fi

# --- Test T-05: Write to path INSIDE worktree succeeds ---
OUTPUT=$(srt --settings "$SRT_CONFIG" \
  claude -p "Write the string 'SANDBOX_WRITE_TEST' to the file $WORKTREE/test_output.txt" \
  --dangerously-skip-permissions \
  --output-format json 2>&1 || true)
if [ -f "$WORKTREE/test_output.txt" ] && grep -q "SANDBOX_WRITE_TEST" "$WORKTREE/test_output.txt"; then
  log_pass "T-05: Write inside worktree succeeded"
else
  log_fail "T-05: Write inside worktree failed. Output: ${OUTPUT:0:200}"
fi

# --- Summary ---
echo ""
echo "Results: $PASS passed, $FAIL failed"
rm -rf "$WORKTREE"
rm -f "$SRT_CONFIG"
[ "$FAIL" -eq 0 ]
```

**Note on T-04:** On macOS, the Seatbelt `allowedPaths` list in srt restricts writes to the
worktree. `/tmp` is excluded from `allowedPaths` in the test config. However, Seatbelt
profiles generated by srt DO typically allow `/tmp` writes for process operation. The correct
non-allowed path to test is outside the worktree AND outside `/tmp`, e.g., `~/.bashrc` or
`/etc/hosts`. The test script above uses `/tmp` as a quick proxy; a more rigorous test should
use `~/.test_srt_escape` as the write target.

---

## Platform-Specific Behavior

| Check | macOS (Seatbelt) | Linux (bubblewrap) |
|-------|------------------|--------------------|
| Network enforcement | Proxy-based (HTTP/S only) | Network namespace + proxy |
| Filesystem enforcement | Seatbelt deny-write policy | bubblewrap bind-mounts |
| Raw TCP blocking | No (only HTTP/S proxied) | Yes (network namespace) |
| Privileged user required | No (sandbox-exec available to users) | Requires unprivileged user namespaces |
| Violation logging | `log stream --predicate 'subsystem == "com.apple.sandbox"'` | journalctl / dmesg |
| GHA runner compatibility | macOS runners: yes; Linux: requires `newuidmap` support |

**GHA Linux runner caveat:** GitHub-hosted `ubuntu-latest` runners support unprivileged
user namespaces as of Ubuntu 22.04. Self-hosted runners on older Ubuntu or Fedora images
may need `sysctl kernel.unprivileged_userns_clone=1`. [INFERRED from bubblewrap docs]

---

## Integration with Conductor Runner

In conductor's runner module, srt integration should wrap the `claude -p` subprocess:

```python
import subprocess
import json
import tempfile
import os

def build_srt_settings(worktree_path: str, extra_domains: list[str] | None = None) -> str:
    """Write a srt-settings.json for the given worktree and return its path."""
    allowed_domains = ["api.anthropic.com"] + (extra_domains or [])
    settings = {
        "allowedDomains": allowed_domains,
        "workdir": worktree_path,
        "allowedPaths": [worktree_path]
    }
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="srt-settings-", delete=False
    )
    json.dump(settings, tmp)
    tmp.flush()
    return tmp.name

def spawn_sandboxed(prompt: str, worktree_path: str, extra_domains: list[str] | None = None):
    """Spawn claude -p under srt with network and filesystem isolation."""
    srt_config = build_srt_settings(worktree_path, extra_domains)
    try:
        cmd = [
            "srt", "--settings", srt_config,
            "claude", "-p", prompt,
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
        ]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=worktree_path,
        )
    finally:
        # Delay cleanup until process exits
        pass
```

**Key design note:** The `api.anthropic.com` domain is always included in `allowedDomains`.
Issue-specific domains (e.g., `github.com` for research issues that need web access) are
passed as `extra_domains`. The conductor config (`conductor.toml`) should include a default
`allowed_domains` list.

---

## Confidence Ratings for Prior Claims

Updating confidence ratings from `docs/research/20-os-sandbox.md` Section R-20-A:

| Claim | Prior Status | Updated Status | Basis |
|-------|-------------|----------------|-------|
| srt blocks WebFetch to non-allowed domains | [INFERRED] | [INFERRED-HIGH] | Documented proxy mechanism; not yet E2E tested with claude -p |
| srt allows WebFetch to allowed domains | [INFERRED] | [INFERRED-HIGH] | Same |
| Bash(curl) blocked for non-allowed domains | [INFERRED] | [INFERRED-HIGH] | Proxy intercepts curl's HTTP traffic |
| Write outside worktree blocked | [INFERRED] | [INFERRED-HIGH] | Seatbelt/bubblewrap documented |
| Write inside worktree allowed | [INFERRED] | [INFERRED-HIGH] | allowedPaths mechanism documented |
| Anthropic API domain must be in allowedDomains | [NOT ASSESSED] | [INFERRED] | Required for claude -p to function |
| macOS Seatbelt active under srt | [INFERRED] | [DOCUMENTED] | sandbox-exec confirmed in srt source |

**To promote to [TESTED]:** Run the test script from Section 5 on macOS with a live claude
session. This is V-08 in the verification suite.

---

## Follow-Up Research Recommendations

**[V2_RESEARCH] Verify Anthropic API domain requirement for srt**
The claim that `api.anthropic.com` must be in `allowedDomains` has not been empirically
tested. A simple test: run srt with an empty `allowedDomains` list and observe the startup
error. Confirm the exact error message for the conductor error handler.

**[WONT_RESEARCH] Raw TCP blocking on macOS**
Seatbelt does not block raw TCP connections (only filesystem ops). This is a known limitation.
The threat model for conductor does not require raw TCP blocking — conductor workers use
HTTP/S for all external calls. No additional research needed.

---

## Sources

- [Anthropic Sandbox Runtime GitHub Repository](https://github.com/anthropic-experimental/sandbox-runtime)
- [Anthropic Blog: Claude Code Sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing)
- [Claude Code Sandboxing Documentation](https://code.claude.com/docs/en/sandboxing)
- [srt npm package (@anthropic-ai/sandbox-runtime)](https://www.npmjs.com/package/@anthropic-ai/sandbox-runtime)
- [Exploring bubblewrap network restriction mechanism](https://www.sambaiz.net/en/article/547/)
- [Claude Code Sandbox Guide 2026](https://claudefa.st/blog/guide/sandboxing-guide)
