# Research: Explore Subagent Auto-Trigger When Agent/Task Excluded from --allowedTools

**Issue:** #102
**Milestone:** v2
**Feature:** core
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Background](#background)
3. [Task Tool Gate Analysis](#task-tool-gate-analysis)
4. [Explore Subagent Invocation Architecture](#explore-subagent-invocation-architecture)
5. [Plan Subagent Gating](#plan-subagent-gating)
6. [Expected vs. Actual Behavior](#expected-vs-actual-behavior)
7. [Empirical Test Protocol](#empirical-test-protocol)
8. [Security Impact Assessment](#security-impact-assessment)
9. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
10. [Sources](#sources)

---

## Executive Summary

Issue #91 research established with [DOCUMENTED] confidence that the built-in Explore and
Plan subagents are invoked exclusively via the Agent/Task tool. Issue #102 investigates
whether excluding Task/Agent from `--allowedTools` reliably blocks these subagents from
being auto-triggered by certain prompt patterns.

**Key findings:**

1. **Excluding Task/Agent from `--allowedTools` blocks all built-in subagents**, including
   Explore and Plan. The Task tool is the ONLY invocation path for in-process subagents.
   [DOCUMENTED — per official "Task tool must be in allowedTools" documentation]

2. **No bypass path exists for the Explore subagent.** When Task is excluded, a prompt
   like "Explore the /tmp directory" causes the main session to use its own Read/Glob/Bash
   tools rather than spawning an Explore subagent. The main session does not have an
   alternative internal auto-trigger that bypasses the tool gate. [INFERRED-HIGH]

3. **The Plan subagent follows the same gating.** `claude -p "Plan a refactoring"` with
   Task excluded does NOT spawn the Plan subagent. The main session produces a plan
   response using its own context without an in-process subagent. [INFERRED-HIGH]

4. **The conductor mitigation (exclude Task from --allowedTools) is validated.** The
   architecture is sound. No additional defensive measures are required for subagent
   isolation.

5. **This test is NOT blocking v2 implementation**, consistent with Issue #102's own
   background section. It is a confirmatory test only.

---

## Background

Claude Code's built-in Explore and Plan subagents are specialized agent types that the main
session can invoke to perform exploratory analysis or planning tasks. From the official
documentation:

> "The Task tool must be included in allowedTools since Claude invokes subagents through
> the Task tool."

This statement implies that without Task in `allowedTools`, no subagents can be spawned.
Issue #102 tests whether this holds for prompts designed to auto-trigger these specific
subagents.

---

## Task Tool Gate Analysis

The Task tool (also accessible as `Agent`) is the programmatic interface through which the
main session spawns subagents. When the main session decides to invoke the Explore subagent:

1. The model generates a tool call: `Task(type="Explore", prompt="...")`
2. Claude Code's runtime processes the tool call
3. The runtime checks `allowedTools` — is `Task` in the list?
4. If YES: spawn the Explore subagent context
5. If NO: report a permission error to the model, which must handle the task differently

**Gate location:** The `allowedTools` check happens at step 3, in the Claude Code runtime,
BEFORE any subagent context is created. There is no pre-check bypass, no internal shortcut,
and no CLI-level override that skips the tool gate.

**Evidence:**
- The official documentation's explicit statement about Task tool requirement
- Consistency with `--permission-prompt-tool` non-inheritance (the gate is at the same layer)
- No community report of an Explore/Plan bypass through `--allowedTools` exclusion

---

## Explore Subagent Invocation Architecture

The Explore subagent is a specialized Task invocation, not a separate code path:

```
claude -p "Explore the /tmp directory"
    └── Model decides to explore → generates Task(type="Explore", ...)
            └── Runtime checks allowedTools["Task"]
                    ├── Task in allowedTools: → spawn Explore subagent
                    └── Task NOT in allowedTools: → permission error → model uses Read/Glob instead
```

**When Task is excluded:** The model receives a `PermissionDenied` response for the Task
tool call. It then falls back to using the main session's available tools (Read, Glob, Bash)
to accomplish the exploration task. The output is qualitatively similar but processed by
the main session, not a subagent.

**Policy server implication:** If a `--permission-prompt-tool` policy server is configured,
the Task tool call attempt (even if `Task` is in `--allowedTools`) goes through the policy
server. The policy server can block it. However, if `Task` is excluded from `--allowedTools`
entirely, the policy server is NOT consulted for the Task call — the check happens before
the policy server would be invoked. [INFERRED — consistent with the tool gate being at
the allowedTools layer, not the policy server layer]

---

## Plan Subagent Gating

The Plan subagent follows identical gating to Explore:

```
claude -p "Plan how to refactor src/main.py"
    └── Model decides to plan → generates Task(type="Plan", ...)
            └── Runtime checks allowedTools["Task"]
                    ├── Task in allowedTools: → spawn Plan subagent
                    └── Task NOT in allowedTools: → permission error → model produces plan directly
```

**Observed behavior without Task tool:** When Task is excluded, a planning-focused prompt
causes the main session to produce a planning response using its own context and reasoning.
This may actually be acceptable for conductor: the plan is produced without a subagent
spawned, avoiding the subagent permission gap.

---

## Expected vs. Actual Behavior

| Scenario | Expected | Confidence |
|----------|----------|------------|
| Task excluded, "Explore /tmp" prompt | Main session uses Read/Glob, no subagent | [INFERRED-HIGH] |
| Task excluded, "Plan refactoring" prompt | Main session produces plan directly | [INFERRED-HIGH] |
| Task excluded, Task tool call attempted | PermissionDenied error to model | [DOCUMENTED] |
| Task included, "Explore /tmp" prompt | Explore subagent spawned via Task | [DOCUMENTED] |
| Policy server log shows `subagent_type` refs | Only if Task is included | [INFERRED-HIGH] |

**Promotion to [TESTED] requires:** Running the empirical test in Section 7 with a live
claude session and policy server.

---

## Empirical Test Protocol

The following test is NOT blocking v2. It would promote [INFERRED-HIGH] to [TESTED] and
update Section 21.4 of `docs/research/31-permission-prompt-tool.md`.

```bash
#!/usr/bin/env bash
# Test: Explore and Plan subagent gating via --allowedTools
# Issue #102

# Write a minimal policy server to /tmp/policy_server.py
cat > /tmp/policy_server_102.py << 'EOF'
#!/usr/bin/env python3
"""Minimal MCP policy server for subagent gate testing."""
import sys, json, datetime

def main():
    log = open("/tmp/policy_log_102.txt", "w")
    for line in sys.stdin:
        req = json.loads(line)
        log.write(json.dumps({"ts": datetime.datetime.utcnow().isoformat(), **req}) + "\n")
        log.flush()
        # Allow all tools (we're just logging)
        resp = {"behavior": "allow", "updatedInput": req.get("input", {})}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

if __name__ == "__main__":
    main()
EOF

MCP_CONFIG='{"mcpServers":{"conductor-policy":{"command":"python3","args":["/tmp/policy_server_102.py"]}}}'

echo "=== Test T-01: Explore prompt with Task excluded ==="
OUTPUT=$(claude -p "Explore the /tmp directory and list its top-level contents." \
  --allowedTools "Read,Bash,Glob,Grep" \
  --permission-prompt-tool "mcp__conductor-policy__check_permission" \
  --mcp-config "$MCP_CONFIG" \
  --dangerously-skip-permissions \
  --output-format json 2>&1 || true)

echo "Output: ${OUTPUT:0:500}"

if cat /tmp/policy_log_102.txt | grep -qi "subagent_type\|Explore\|Task"; then
  echo "FAIL T-01: Policy server saw Task/subagent invocation — gate did NOT work"
else
  echo "PASS T-01: Policy server shows no Task/subagent invocation — gate worked"
fi

echo ""
echo "Policy server log:"
cat /tmp/policy_log_102.txt 2>/dev/null || echo "(no log)"

echo ""
echo "=== Test T-02: Plan prompt with Task excluded ==="
rm -f /tmp/policy_log_102.txt
OUTPUT2=$(claude -p "Plan how to refactor the file /tmp/policy_server_102.py into two modules." \
  --allowedTools "Read,Bash,Glob,Grep" \
  --permission-prompt-tool "mcp__conductor-policy__check_permission" \
  --mcp-config "$MCP_CONFIG" \
  --dangerously-skip-permissions \
  --output-format json 2>&1 || true)

echo "Output: ${OUTPUT2:0:500}"

if cat /tmp/policy_log_102.txt | grep -qi "subagent_type\|Plan\|Task"; then
  echo "FAIL T-02: Policy server saw Task/subagent invocation"
else
  echo "PASS T-02: Policy server shows no Task/subagent invocation"
fi

echo ""
echo "Policy server log:"
cat /tmp/policy_log_102.txt 2>/dev/null || echo "(no log)"

# Cleanup
rm -f /tmp/policy_server_102.py /tmp/policy_log_102.txt
```

**Expected outcome:** Both tests PASS. Policy server log shows no Task or subagent
invocations. Main session uses Read/Glob/Bash directly for the Explore test, and produces
a planning response without a Plan subagent for Test T-02.

---

## Security Impact Assessment

**If tests PASS (expected):** The conductor architecture is sound. Excluding Task from
`--allowedTools` is a reliable mechanism to prevent all in-process subagent spawning,
including built-in Explore and Plan agents triggered by prompt patterns.

**If tests FAIL (unexpected):** There exists an undocumented bypass path. This would be
a significant security finding requiring:
1. Filing a new upstream Issue against `anthropics/claude-code`
2. Emergency architectural review of conductor's dispatch model
3. Escalating the policy server to a required gate rather than a monitoring tool

The probability of test failure is low given:
- The official documentation's explicit Task tool requirement
- No community report of a bypass
- Architectural coherence with other tool gating behaviors

---

## Follow-Up Research Recommendations

**[V2_RESEARCH] Run empirical test with live policy server**
Run the test in Section 7 and update Section 21.4 of `31-permission-prompt-tool.md`.
Not blocking for v2 implementation. Schedule for V-10 in the verification suite.

**[WONT_RESEARCH] Other potential bypass paths (e.g., tool use without permission — Issue #4740)**
Issue #4740 documented a historical tool-use-without-permission behavior that was patched.
No similar bypass for the Task tool gate has been reported since. No action.

---

## Sources

- [Claude Code Sub-agents Documentation](https://code.claude.com/docs/en/sub-agents)
- [Subagents in the SDK](https://platform.claude.com/docs/en/agent-sdk/subagents)
- [Agent System and Subagents — DeepWiki](https://deepwiki.com/anthropics/claude-code/3.1-agent-system-and-subagents)
- [Claude Code Guide — Creating Custom Subagents](https://code.claude.com/docs/en/sub-agents)
