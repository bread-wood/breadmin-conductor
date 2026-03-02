# Research: PreToolUse Hook Reliability under bypassPermissions Mode

**Issue:** #19
**Milestone:** M1: Foundation
**Status:** Research Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Context and Motivation](#context-and-motivation)
3. [Do PreToolUse Hooks Fire in -p Mode?](#do-pretooluse-hooks-fire-in--p-mode)
4. [Do PreToolUse Hooks Fire under bypassPermissions?](#do-pretooluse-hooks-fire-under-bypasermissions)
5. [How to Block a Tool Call from a PreToolUse Hook](#how-to-block-a-tool-call-from-a-pretooluse-hook)
6. [Known Reliability Issues and Edge Cases](#known-reliability-issues-and-edge-cases)
7. [Impact on the Security Model from doc 06](#impact-on-the-security-model-from-doc-06)
8. [Fallback Defenses if PreToolUse is Unreliable](#fallback-defenses-if-pretooluse-is-unreliable)
9. [Permission Evaluation Order](#permission-evaluation-order)
10. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
11. [Sources](#sources)

---

## Executive Summary

PreToolUse hooks **do fire in headless `-p` mode**, and they **do fire under `bypassPermissions` / `--dangerously-skip-permissions`**. The official Anthropic documentation explicitly states: *"bypassPermissions: Auto-approves all tool uses without prompts. Hooks still execute and can block operations if needed."* This is the strongest available evidence — a documented guarantee from Anthropic. However, there are **multiple confirmed open bug reports** describing conditions where PreToolUse hook blocks are silently ignored (the tool executes anyway), and one known first-launch reliability bug specific to `--dangerously-skip-permissions` mode. The net assessment is:

- **Do hooks fire?** [DOCUMENTED] Yes, in both `-p` mode and bypassPermissions mode.
- **Can they block?** [DOCUMENTED] Yes, via exit code 2 or `permissionDecision: "deny"` JSON output.
- **Are they reliable?** [INFERRED from bug reports] Not unconditionally. There are unresolved open bugs where the block is ignored for certain tool types (Write, Edit) and a first-launch initialization bug specific to `--dangerously-skip-permissions`. Additionally, `--allowedTools` allowlisting is broken under `bypassPermissions` (documented in issue #12232, closed not planned), which means the assumed defense-in-depth (allowedTools + hooks) may have only one working layer.

The security model in doc 06 that depends on PreToolUse hooks for runtime command validation **is partially reliable but not guaranteed**. It should be treated as one defense layer within a defense-in-depth strategy, not as a sole enforcement mechanism.

---

## Context and Motivation

`06-security-threat-model.md` (cross-reference: issue #6) describes a "Layer 3" defense that uses a PreToolUse hook to validate every Bash command against a compiled regex denylist before execution. The document also noted `--dangerously-skip-permissions` as a necessary flag for headless operation, and flagged under R-SEC-C that the interaction between bypass mode and hooks was an open question.

`02-session-continuity.md` (cross-reference: issue #2) confirmed that `PermissionRequest` hooks do not fire in `-p` mode, and explicitly recommends using `PreToolUse` as the replacement:

> "All hooks fire in -p mode **except** `PermissionRequest`. The documentation explicitly notes: 'PermissionRequest hooks do not fire in non-interactive mode (-p). Use PreToolUse hooks for automated permission decisions.'"

This research resolves whether `PreToolUse` itself is reliable in the `-p` + bypassPermissions combination that conductor uses.

---

## Do PreToolUse Hooks Fire in -p Mode?

**Finding: YES** [DOCUMENTED]

The official hooks reference documentation (`code.claude.com/docs/en/hooks`) lists `PreToolUse` in the full hook event table with no qualification, exception, or headless-mode caveat. The lifecycle diagram shows `PreToolUse` firing inside the agentic loop for every tool call.

The hooks reference specifies the common input fields received by every hook:

```json
{
  "session_id": "abc123",
  "transcript_path": "~/.claude/projects/.../transcript.jsonl",
  "cwd": "/home/user/my-project",
  "permission_mode": "default",
  "hook_event_name": "PreToolUse",
  "tool_name": "Bash",
  "tool_input": { "command": "npm test" }
}
```

Note that `permission_mode` is present in the input. When running under `--dangerously-skip-permissions`, this field will contain `"bypassPermissions"`, which allows hook scripts to detect the active permission mode and adjust their behavior accordingly. This is a confirmed documented field (official hooks reference, `permission_mode` in common input fields table), not an inferred one.

`02-session-continuity.md` confirms the known exception: `PermissionRequest` does not fire in `-p` mode. `PreToolUse` is not listed as an exception.

---

## Do PreToolUse Hooks Fire under bypassPermissions?

**Finding: YES, with documented guarantee** [DOCUMENTED]

The official Agent SDK permissions documentation (`platform.claude.com/docs/en/agent-sdk/permissions`) includes an explicit statement in the `bypassPermissions` mode description:

> "bypassPermissions: Auto-approves all tool uses without prompts. **Hooks still execute and can block operations if needed.**"

This is the strongest available evidence — a documented behavioral guarantee from Anthropic's own documentation. The word "still" confirms that hook execution is preserved when bypass mode is active, not bypassed by it.

The permission evaluation order documented in that same page clarifies why:

```
1. Hooks run first (can allow, deny, or continue)
2. Permission rules (allow/deny rules in settings.json)
3. Permission mode (bypassPermissions auto-approves at this step)
4. canUseTool callback (interactive fallback)
```

**bypassPermissions operates at step 3.** Hooks operate at step 1, before the bypass takes effect. This architectural ordering means hooks fire unconditionally, and `bypassPermissions` only affects the outcome for tool calls that hooks did not deny at step 1.

---

## How to Block a Tool Call from a PreToolUse Hook

There are two supported methods. The recommended current method is the JSON `permissionDecision` format; the exit code method also works but is simpler and has fewer control options.

### Method 1: Exit Code 2 (Simple)

```bash
#!/bin/bash
# .claude/hooks/validate_bash.sh
command=$(jq -r '.tool_input.command' < /dev/stdin)

if echo "$command" | grep -qE '\benv\b|\bprintenv\b|\bcurl\b|\bwget\b|\beval\b'; then
  echo "BLOCKED: command matches security denylist pattern" >&2
  exit 2  # Blocking error: tool call is prevented
fi

exit 0  # Success: tool call proceeds
```

**Exit code semantics:**
- Exit `0` — success, tool call proceeds. Claude Code parses stdout for JSON output.
- Exit `2` — blocking error. Stderr text is fed back to Claude as an error message. For PreToolUse, this blocks the tool call. Claude Code ignores any JSON in stdout when exit code is 2.
- Any other non-zero exit code — non-blocking error. Stderr shown in verbose mode, execution continues.

**Critical distinction:** Only exit code `2` blocks. Exit code `1` (the common "generic error" exit code used in many shell scripts) is a **non-blocking** error. An erroneous hook that uses `exit 1` instead of `exit 2` will silently allow the tool to proceed. This is a common mistake.

### Method 2: JSON permissionDecision (Recommended)

```bash
#!/bin/bash
# .claude/hooks/validate_bash.sh
command=$(jq -r '.tool_input.command' < /dev/stdin)

if echo "$command" | grep -qE '\benv\b|\bprintenv\b|\bcurl\b'; then
  jq -n '{
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "deny",
      permissionDecisionReason: "Command matches security denylist: network/env access blocked"
    }
  }'
  exit 0  # Must be exit 0 when using JSON output
else
  exit 0  # Allow
fi
```

Valid values for `permissionDecision`:
- `"allow"` — bypasses the permission system, auto-approves the tool call
- `"deny"` — prevents the tool call; reason is shown to Claude
- `"ask"` — escalates to user for interactive confirmation (not viable in `-p` mode)

**Important:** You must choose one method per hook invocation. Claude Code only processes JSON on exit 0. If you exit 2, all JSON output is ignored. The deprecated top-level `decision: "block"` / `decision: "approve"` format no longer applies to PreToolUse; use `hookSpecificOutput.permissionDecision`.

When multiple hooks match a single tool call and return conflicting decisions, **`deny` takes precedence over `ask`, which takes precedence over `allow`**.

### Python Implementation (from doc 06)

```python
# .claude/hooks/pre_tool_use_validator.py
import json, sys, re

DANGEROUS_PATTERNS = [
    r'\benv\b',
    r'\bprintenv\b',
    r'\bcurl\b',
    r'\bwget\b',
    r'\bnc\b',
    r'\beval\b',
    r'\bexec\b',
    r'rm\s+-rf',
    r'git push.*--force',
    r'git push.*origin.*main',
    r'cat\s+~/',
    r'cat\s+\.env',
]

payload = json.load(sys.stdin)
if payload.get("tool_name") == "Bash":
    cmd = payload.get("tool_input", {}).get("command", "")
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd):
            # Use JSON method to get structured denial
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": f"Command blocked by denylist pattern: {pattern}"
                }
            }))
            sys.exit(0)  # Exit 0 required when printing JSON

sys.exit(0)
```

---

## Known Reliability Issues and Edge Cases

### Issue 1: First-Launch Initialization Bug with --dangerously-skip-permissions

**Severity: HIGH** [DOCUMENTED — GitHub Issue #10385, closed not planned, January 2026]

When Claude Code is launched for the **first time** using `--dangerously-skip-permissions` mode (on a machine that has never run Claude Code in any other mode), the hooks system may fail to register properly. Hooks are configured correctly (parseable via `/hooks` command) but hook events are never triggered or executed.

**Condition:** Affects first launch only in bypass mode. Once the session has been initialized through debug mode or standard mode, subsequent uses of `--dangerously-skip-permissions` work correctly.

**Status:** Closed as not planned. No fix was implemented.

**Mitigation for conductor:** Before using `--dangerously-skip-permissions` for the first time on any new machine, run at least one interactive Claude Code session (any prompt) to ensure the hooks infrastructure is initialized. In a CI/CD context where the machine is ephemeral, this is a real concern. Add a pre-flight step: `claude -p "echo hooks-init" --dangerously-skip-permissions --max-turns 1` on first machine setup, then verify a test hook fires before relying on hooks for security.

### Issue 2: PreToolUse exit code 1 (non-blocking) vs exit code 2 (blocking) confusion

**Severity: HIGH** [DOCUMENTED — Official hooks reference]

The exit code behavior is not intuitive:
- Exit code 2 = blocking (tool is prevented)
- Exit code 1 = non-blocking (tool proceeds, error shown in verbose mode)
- Other non-zero = non-blocking

Hook scripts that use the common shell convention of `exit 1` for error will silently allow tool calls. This is a common authoring mistake. The 06-security-threat-model.md example uses `sys.exit(2)` correctly for the Python script and `exit 2` for bash, but any hook written without this awareness will have a latent security hole.

**Mitigation:** Add unit tests that call the hook script directly with sample JSON input and verify the exit code. Never use `set -e` in hook scripts without understanding that `set -e` can cause the script to exit with code 1 on any failing sub-command.

### Issue 3: PreToolUse blocks ignored for Write/Edit tool types

**Severity: CRITICAL** [DOCUMENTED — GitHub Issue #13744, closed as duplicate December 2025; Issue #21988, closed as duplicate January 2026]

Multiple confirmed bug reports describe a scenario where a PreToolUse hook returns exit code 2 or `permissionDecision: "deny"` for Write or Edit tool calls, but the file operation **still completes**. The hook executes, outputs error messages to stderr, and returns the blocking signal — but Claude Code logs "PreToolUse hook error" and proceeds anyway.

Issue #13744 specifically documented:
- Bash tool: exit code 2 → operation blocked (working correctly)
- Write tool: exit code 2 → file created anyway (broken)
- Edit tool: exit code 2 → edit applied anyway (broken)

Issue #21988 confirmed the same pattern: hook error logged, operation proceeds. Both issues were closed as duplicates of older issues (#3514, #4669), but **the underlying bug remains unresolved as of January 2026**.

**Impact on conductor:** The security model in doc 06 relies on PreToolUse hooks to block dangerous Bash commands. Bash blocking appears to work correctly. However, hooks cannot be relied on to block Edit or Write tool operations. The PreToolUse hook is not a reliable control point for file writes.

**Mitigation for conductor:** Since issue-worker agents use Bash tool for git operations and test execution, and are scoped to a worktree via `--allowedTools`, the Bash blocking path (which appears reliable) is the most critical path. However, for file write operations, use `--allowedTools "Edit(/src/**),Edit(/tests/**)"` path restriction as the primary defense (OS-level path restriction via allowedTools), not hook-based blocking.

### Issue 4: --allowedTools allowlist ignored under bypassPermissions

**Severity: CRITICAL** [DOCUMENTED — GitHub Issue #12232, closed not planned, January 2026]

The `--allowedTools` allowlist approach (whitelist-first: only allow specified tools, deny everything else) is broken when combined with `--permission-mode bypassPermissions`. Reported behavior:

```bash
# Test: allowedTools with bypassPermissions
claude --verbose -p "get my public ip from curl ifconfig.me" \
  --allowedTools Read \
  --permission-mode bypassPermissions
# Result: 1.2.3.4 (Bash tool executed despite Read-only allowlist)
```

The `--disallowedTools` denylist **does** work under bypassPermissions. The whitelist approach does not.

**This is critical.** The security model in doc 06 assumes that `--allowedTools` allowlist provides defense in depth alongside PreToolUse hooks. If allowlisting is broken under bypassPermissions, then:
1. The allowlist defense layer is not functioning
2. PreToolUse hooks become the **sole** software enforcement layer within the Claude Code process (not counting OS-level sandbox)

**Status:** Closed as not planned in January 2026. Workaround: use `--disallowedTools` denylist (which does work) rather than `--allowedTools` allowlist for blocking. But a denylist is inherently incomplete — it requires enumerating all dangerous tools.

**Mitigation:** Switch the security model to lead with `--disallowedTools` for the most dangerous tools (network access, env dump, force push) and treat `--allowedTools` as advisory only. Layer PreToolUse hooks as the primary in-process control, and OS-level sandbox (Docker, sandbox-runtime) as the enforcement layer.

### Issue 5: Race condition with parallel PreToolUse hooks

**Severity: MEDIUM** [DOCUMENTED — GitHub Issue #24327, closed completed, resolution: race condition]

When multiple PreToolUse hooks are configured to run in parallel for the same event, a race condition can cause intermittent failures where one hook's exit code 2 block is processed but Claude does not act on the error feedback (goes idle instead of retrying). The root cause was identified as a parallel hook execution race condition, not a fundamental issue with exit code 2 processing.

**Mitigation:** Use a single hook dispatcher script that calls multiple checks sequentially rather than configuring multiple hooks for the same event. The doc 06 Python example already follows this pattern.

### Issue 6: PreToolUse and PostToolUse hooks not firing at all (older bug)

**Severity: HIGH** [DOCUMENTED — GitHub Issue #6305, open, macOS, version 1.0.38+]

A reported bug where PreToolUse and PostToolUse hooks completely fail to fire despite correct configuration, affecting multiple users on macOS. Other hook types (Stop, SubagentStop, UserPromptSubmit) work correctly. The root cause is described as a broken trigger mechanism.

This bug has no confirmed status (open, marked stale). It may have been fixed in later versions or may still affect some configurations. The specific versions affected (1.0.38, 1.0.89) are much older than the current v2.1.x releases, and the bug may have been resolved without a formal close.

**Mitigation:** Add a smoke test to conductor's startup sequence: emit a known test tool call and verify the hook fires and produces expected output. If the hook doesn't fire, abort and alert.

---

## Impact on the Security Model from doc 06

**Cross-reference: `06-security-threat-model.md`**

### Current Model Assumptions

The defense architecture in doc 06 (Layer 3) assumes:

1. `--allowedTools` allowlist restricts tools to only what the agent needs
2. PreToolUse hooks validate every Bash command against a denylist before execution
3. `--disallowedTools` denylist blocks known dangerous commands
4. `--dangerously-skip-permissions` is used alongside these controls

### What This Research Changes

| Assumption | Actual Status |
|-----------|--------------|
| `--allowedTools` allowlist provides defense layer | **BROKEN** under bypassPermissions (issue #12232, closed not planned) |
| PreToolUse hooks fire in `-p` mode | **CONFIRMED** [DOCUMENTED] |
| PreToolUse hooks fire under bypassPermissions | **CONFIRMED** [DOCUMENTED] |
| PreToolUse hooks can reliably block Bash tool calls | **PARTIALLY CONFIRMED** — Bash blocking appears to work; Write/Edit blocking is unreliable (issues #13744, #21988) |
| PreToolUse hooks can reliably block Edit/Write tool calls | **NOT RELIABLE** [DOCUMENTED — unresolved bugs] |
| Exit code 2 is the correct way to block | **CONFIRMED** [DOCUMENTED] |
| First-launch reliability is guaranteed | **NOT GUARANTEED** — first-launch initialization bug under bypass mode (issue #10385, closed not planned) |

### Revised Risk Assessment

The security model's overall defense posture is:
- **Strong for Bash command blocking:** PreToolUse hooks + `--disallowedTools` + PostToolUse audit log provide layered defense for Bash operations. The highest-risk vectors (T1 prompt injection attempting to run `env | curl ...`) go through Bash and are covered.
- **Weak for file write operations:** PreToolUse hooks cannot reliably block Write/Edit tool calls. Path restriction via `--allowedTools "Edit(/src/**)"` should be the primary defense for file scope, but this is also broken under bypassPermissions. This means an injected write to an out-of-scope path may not be blocked by software controls.
- **OS-level sandbox becomes essential:** Given the unreliability of software-level controls, the OS-level sandbox (Docker, sandbox-runtime) described in Layer 4 of doc 06 is not optional — it is the enforcement fallback that makes the overall architecture viable.

### Revised Defense Architecture

```
Layer 1: Input Sanitization (unchanged from doc 06)
  ├── Strip HTML comments from issue bodies
  ├── Audit CLAUDE.md hash before spawning
  └── Truncate and XML-delimit untrusted content

Layer 2: Environment Isolation (unchanged from doc 06)
  ├── Scrub env to ALLOWED_ENV_VARS only
  └── Verify working directory and branch

Layer 3: Tool Permission Controls (revised)
  ├── --disallowedTools denylist (works under bypassPermissions — primary SW control)
  │   Focus: network tools, env dump, force push, dangerous bash patterns
  ├── PreToolUse hook for Bash validation (confirmed reliable for Bash blocking)
  │   Focus: runtime regex check of Bash commands not caught by --disallowedTools
  ├── --allowedTools (unreliable under bypassPermissions — use as advisory only)
  │   Do NOT rely on for security enforcement; prefer --disallowedTools
  └── PostToolUse JSONL audit log (for post-hoc detection, not prevention)

Layer 4: OS-Level Sandbox (MANDATORY — not optional)
  ├── sandbox-runtime or Docker with --network none
  ├── Filesystem write restricted to worktree directory
  └── Network access via domain-allowlisted proxy only
  Note: Layer 4 is the enforcement fallback for all Layer 3 failures
```

---

## Fallback Defenses if PreToolUse is Unreliable

If PreToolUse hooks fail to fire or fail to block in specific conditions, the following fallback defenses remain in effect:

### 1. --disallowedTools Denylist (Working under bypassPermissions)

Unlike `--allowedTools`, the `--disallowedTools` denylist is confirmed to work under bypassPermissions. For the most dangerous tool patterns, use explicit deny rules:

```bash
claude -p "$PROMPT" \
  --disallowedTools "Bash(env),Bash(printenv),Bash(curl *),Bash(wget *),Bash(nc *),Bash(python -c *),Bash(bash -c *),Bash(eval *),Bash(exec *),Bash(rm -rf *),Bash(git push --force *),Bash(git push origin main *),Bash(gh pr merge *),WebFetch" \
  --dangerously-skip-permissions
```

This denylist approach is less complete than an allowlist (future tools or command variants may not be covered), but it works.

### 2. OS-Level Network Sandbox

Even if a hook fails to block a `curl` command, an OS-level network sandbox (Docker `--network none`, or `sandbox-runtime` with a domain allowlist) prevents the exfiltration from completing. This is the strongest reliable fallback.

### 3. PostToolUse Audit Logging

PostToolUse hooks fire after tool execution and cannot prevent damage, but they provide evidence for incident response. Audit logs help detect attacks that succeeded past the PreToolUse layer. Combined with secret scanning and CI verification, they create a detection path.

### 4. Worktree Filesystem Isolation

Agents run in `.claude/worktrees/<id>/` with no access to sibling worktrees or the host filesystem (when properly sandboxed). An injected write that escapes the hook control layer is still constrained to the worktree path by OS-level filesystem permissions.

### 5. ANTHROPIC_API_KEY Environment Scrubbing

As documented in doc 06 (T3), scrubbing the environment before spawning the subprocess means that even if an exfiltration command runs (bypassing all hook-based controls), the credentials available to exfiltrate are limited to what was in the cleaned environment.

### Recommended Production Configuration

Given the issues found, the recommended production configuration for conductor sub-agents is:

```python
# Minimum required environment (no secrets)
clean_env = {
    "PATH": os.environ["PATH"],
    "HOME": os.environ["HOME"],
    "USER": os.environ["USER"],
    "GIT_AUTHOR_NAME": "...",
    "GIT_AUTHOR_EMAIL": "...",
    "CLAUDE_CONFIG_DIR": tmp_config_dir,
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}

# Generate settings with hooks and denylist
settings = {
    "permissions": {
        "deny": [
            "Bash(env)", "Bash(printenv)", "Bash(curl *)", "Bash(wget *)",
            "Bash(nc *)", "Bash(python -c *)", "Bash(bash -c *)",
            "Bash(eval *)", "Bash(exec *)", "Bash(rm -rf *)",
            "Bash(git push --force *)", "Bash(git push origin main *)",
            "Bash(gh pr merge *)", "Bash(gh issue edit *)",
            "Read(.env)", "Read(~/.aws/**)", "Read(~/.ssh/**)",
            "Edit(.github/**)", "Edit(.claude/**)", "WebFetch",
        ]
    },
    "hooks": {
        "PreToolUse": [{
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "/conductor/hooks/validate_bash.py"}]
        }],
        "PostToolUse": [{
            "hooks": [{"type": "command", "command": "/conductor/hooks/audit_log.sh", "async": True}]
        }]
    }
}

subprocess.run(
    ["claude", "-p", "--settings", settings_path, "--dangerously-skip-permissions", prompt],
    cwd=worktree_path,
    env=clean_env,
)
```

This configuration:
1. Uses `--disallowedTools`-equivalent deny rules in settings (confirmed working)
2. Adds PreToolUse Bash validation as a defense-in-depth layer for Bash (confirmed reliable)
3. Adds PostToolUse audit logging (async, non-blocking)
4. Scrubs the environment (no credentials)
5. Still requires OS-level sandbox as the enforcement fallback

---

## Permission Evaluation Order

The documented permission evaluation order from `platform.claude.com/docs/en/agent-sdk/permissions`:

```
Step 1: Hooks
  Run PreToolUse hooks. Can allow, deny, or continue.
  [BEFORE bypassPermissions is consulted]

Step 2: Permission Rules
  Check settings.json allow/deny rules.
  Deny rules take precedence over allow rules.

Step 3: Permission Mode
  bypassPermissions auto-approves here.
  dontAsk also auto-approves at this step.
  [bypassPermissions only operates here, after hooks]

Step 4: canUseTool callback
  Interactive approval prompt (not available in -p mode).
```

This ordering confirms: **bypassPermissions cannot skip hooks because hooks run before the permission mode is consulted.** A `deny` from a PreToolUse hook prevents the tool call from even reaching Step 3 where bypass mode would approve it.

---

## Follow-Up Research Recommendations

### R-19-A: Empirical Smoke Test for Hook Firing

A minimal empirical test to verify that PreToolUse hooks fire in a `-p` + `--dangerously-skip-permissions` invocation is needed. The test:

1. Write a PreToolUse hook that logs to a file and exits 0
2. Run `claude -p "run ls" --dangerously-skip-permissions` with the hook configured
3. Confirm the log file was written
4. Then verify the blocking behavior: write a hook that exits 2 for any Bash command starting with `ls`
5. Confirm `ls` does not execute (no output) and error is returned

This would upgrade the "Do PreToolUse hooks fire under bypassPermissions?" finding from [DOCUMENTED] to [TESTED].

**Suggested issue:** Already planned as part of the empirical verification work stream.

### R-19-B: Write/Edit Hook Block Bug Status in Current Claude Code Version

Issue #13744 and #21988 were closed as duplicates of #3514 and #4669 in December 2025 / January 2026. The parent issues should be checked for resolution status. If the Write/Edit blocking bug is fixed in v2.1.50+, the security model can be restored to include file write blocking via hooks.

**Question:** What is the current status of GitHub issues #3514 and #4669? Are they fixed in v2.1.x?

### R-19-C: --allowedTools Bypass Behavior Root Cause

Issue #12232 (allowedTools ignored under bypassPermissions) was closed as not planned in January 2026. The root cause was not publicly explained. Understanding whether this is:
- An intentional design decision (bypassPermissions intentionally overrides allowedTools)
- A bug that Anthropic doesn't plan to fix
- Or a misuse of the flags

...would clarify whether an alternative invocation pattern can restore allowlist enforcement.

**Suggested issue:** Research whether `--setting-sources ""` + `--settings` with a permissions block provides a working allowlist under bypassPermissions.

### R-19-D: dontAsk Mode as Alternative to bypassPermissions

Issue #12232's user asked whether `dontAsk` mode (which auto-denies unapproved tools rather than bypassing) preserves `--allowedTools` enforcement. If `dontAsk` mode:
- Works in headless `-p` mode without hanging
- Correctly enforces `--allowedTools` allowlist
- Does not silently skip pre-approved tool calls

...then `dontAsk` could replace `bypassPermissions` for conductor, providing a stronger permission enforcement baseline. The trade-off is that `dontAsk` auto-denies any tool not explicitly approved, which may cause the agent to fail on legitimate tool calls if the allowlist is incomplete.

**Suggested issue:** Research dontAsk mode behavior in headless -p mode.

---

## Sources

- [Hooks reference — Claude Code Docs](https://code.claude.com/docs/en/hooks) — Full hook event documentation, exit code semantics, JSON permissionDecision format, PreToolUse input schema, and exit code 2 behavior per event table
- [Configure permissions — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/permissions) — bypassPermissions mode description with explicit statement "Hooks still execute and can block operations if needed"; permission evaluation order (hooks → rules → mode → canUseTool)
- [Intercept and control agent behavior with hooks — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/hooks) — SDK hook documentation, callback return formats, deny priority over ask over allow, hook execution order
- [Configure permissions — Claude Code Docs](https://code.claude.com/docs/en/permissions) — Permission mode descriptions and hook integration
- [GitHub Issue #10385: Hooks Initialization Failure on First Startup with --dangerously-skip-permissions](https://github.com/anthropics/claude-code/issues/10385) — First-launch hook initialization bug under bypass mode; closed not planned January 2026
- [GitHub Issue #12232: Is --allowedTools with --permission-mode bypassPermissions behavior expected?](https://github.com/anthropics/claude-code/issues/12232) — Confirmed bug: allowedTools allowlist ignored under bypassPermissions; disallowedTools denylist works; closed not planned January 2026
- [GitHub Issue #13744: PreToolUse hooks with exit code 2 don't block Write/Edit operations](https://github.com/anthropics/claude-code/issues/13744) — Confirmed bug: PreToolUse exit 2 blocks Bash but not Write/Edit; closed as duplicate December 2025
- [GitHub Issue #21988: PreToolUse hooks exit code ignored - operations proceed after hook failure](https://github.com/anthropics/claude-code/issues/21988) — Confirmed: hook error logged, tool proceeds; closed as duplicate January 2026; underlying bug unresolved
- [GitHub Issue #24327: PreToolUse hook exit code 2 causes Claude to stop instead of acting on error feedback](https://github.com/anthropics/claude-code/issues/24327) — Race condition with parallel hooks; closed completed
- [GitHub Issue #6305: Post/PreToolUse Hooks Not Executing in Claude Code](https://github.com/anthropics/claude-code/issues/6305) — Hooks not firing at all on macOS in older versions; open/stale
- [GitHub Issue #4719: Feature Request: Expose Active Permission Mode to PreToolUse Hook](https://github.com/anthropics/claude-code/issues/4719) — permission_mode field in hook input
- [GitHub Issue #6227: Feature Request: Expose Active Permission Mode to Hooks and Statusline](https://github.com/anthropics/claude-code/issues/6227) — Confirmation that permission_mode is exposed in hook input
- [Block Tool Commands Before Execution with PreToolUse Hooks — egghead.io](https://egghead.io/block-tool-commands-before-execution-with-pre-tool-use-hooks~erv55) — Practical PreToolUse hook implementation guide
- [Claude Code --dangerously-skip-permissions: Safe Usage Guide — ksred.com](https://www.ksred.com/claude-code-dangerously-skip-permissions-when-to-use-it-and-when-you-absolutely-shouldnt/) — Community analysis of bypass mode behavior
- [Dangerous Skip Permissions — ClaudeLog](https://claudelog.com/mechanics/dangerous-skip-permissions/) — Documentation of bypass mode: "When you enable bypass mode, all subagents inherit full autonomous access"
- [GitHub — kornysietsma/claude-code-permissions-hook](https://github.com/kornysietsma/claude-code-permissions-hook) — Reference implementation of PreToolUse hook for granular permission controls
- [GitHub — trailofbits/claude-code-config](https://github.com/trailofbits/claude-code-config) — Trail of Bits opinionated defaults including PreToolUse hooks
- [Secure Your Claude Skills with Custom PreToolUse Hooks — egghead.io](https://egghead.io/secure-your-claude-skills-with-custom-pre-tool-use-hooks~dhqko) — PreToolUse hook security patterns (referenced in 06-security-threat-model.md)
- [Bash Command Validator Example — anthropics/claude-code](https://github.com/anthropics/claude-code/blob/main/examples/hooks/bash_command_validator_example.py) — Official reference implementation for Bash command validation via PreToolUse hook
- [06-security-threat-model.md — breadmin-conductor](docs/research/06-security-threat-model.md) — Security model that depends on PreToolUse hooks; see AgentShield Patterns and R-SEC-C
- [02-session-continuity.md — breadmin-conductor](docs/research/02-session-continuity.md) — Confirmed PermissionRequest does not fire in -p mode; recommended PreToolUse as replacement
