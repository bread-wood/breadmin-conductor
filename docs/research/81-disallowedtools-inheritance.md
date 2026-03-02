# Research: --disallowedTools CLI Flag Inheritance by Task Tool Subagents

**Issue:** #81
**Milestone:** v2
**Feature:** core
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Background](#background)
3. [Research Findings](#research-findings)
4. [Inheritance Model Analysis](#inheritance-model-analysis)
5. [Empirical Test Protocol](#empirical-test-protocol)
6. [Security Posture Update](#security-posture-update)
7. [Recommended Conductor Implementation](#recommended-conductor-implementation)
8. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
9. [Sources](#sources)

---

## Executive Summary

Issue #62 research (`docs/research/31-permission-prompt-tool.md`) concluded that
`--permission-prompt-tool` is NOT inherited by Task tool subagents. Issue #81 investigates
whether `--disallowedTools` shares this non-inheritance behavior, which was previously
assessed as [INFERRED] but not empirically tested.

**Key findings:**

1. **`--disallowedTools` CLI flag does NOT propagate to Task tool subagents.** The CLI flag
   is evaluated at process startup by the parent session. Task tool subagents are launched
   as in-process contexts, not as new CLI processes. They inherit tool availability from the
   parent session's settings chain but NOT from CLI flags. [INFERRED-HIGH]

2. **`disallowedTools` in settings.json DOES apply to subagents,** subject to the known
   inheritance gap for `settings.json` deny rules documented in Issues #25000 and #18950.
   This is a separate mechanism from the CLI flag. [INFERRED — see Section 4]

3. **The prior assessment in `31-permission-prompt-tool.md` (Section 19.6) is consistent**
   with current findings. The security gap remains: critical tool denials placed on the CLI
   do not protect against subagent misuse.

4. **For conductor's security model:** The primary mitigation remains (a) excluding `Task`
   from `--allowedTools` entirely (no subagents spawned) and (b) the `--permission-prompt-tool`
   policy server for the main session.

---

## Background

`--disallowedTools` is a Claude Code CLI flag that removes specific tools from the agent's
available tool set. Example:

```bash
claude -p "Implement issue #42" \
  --allowedTools "Read,Write,Edit,Bash,Glob,Grep" \
  --disallowedTools "Bash(env),Bash(curl *),Bash(git push --force)"
```

The question is whether this flag's deny list is inherited by Task tool subagents that the
main session spawns. If inherited: subagents cannot use the denied tools. If not inherited:
a subagent could execute `env` or `curl` even though the main session cannot.

---

## Research Findings

### CLI Flags vs. Settings Inheritance

Claude Code's subagent (Task tool) architecture spawns subagents as **in-process contexts**
sharing the parent session's Node.js process. They do NOT fork a new `claude` process and
do NOT parse CLI flags again.

**CLI flag inheritance chain:**
- `--disallowedTools` → evaluated at process startup → populates a deny list in the session
  state
- Task tool invocation → creates a new in-process context → inherits session state
  (available tools, settings.json rules) but NOT the CLI-parsed deny list

This is consistent with `--permission-prompt-tool` non-inheritance (as confirmed in Issue
#62 research): both flags are CLI-startup constructs, not session state that propagates to
subagents.

### settings.json disallowedTools vs. CLI --disallowedTools

There are two distinct mechanisms:

| Mechanism | Scope | Subagent inheritance |
|-----------|-------|---------------------|
| `--disallowedTools "Bash(env)"` (CLI flag) | Parent session only | NOT inherited [INFERRED-HIGH] |
| `settings.json: {"permissions": {"deny": ["Bash(env)"]}}` | Settings chain | Partially inherited (see Issues #25000, #18950) |
| `--agents` frontmatter `disallowedTools` | Per-named-agent | Applies only when that agent type is invoked |

The `disallowedTools` field in `--agents` frontmatter was added to allow per-agent tool
blocking. When a named agent is spawned via Task, its frontmatter restrictions apply. This
is distinct from the parent CLI's `--disallowedTools` flag. [DOCUMENTED]

### Known Gap: settings.json deny rules in subagents

Issues #25000 and #18950 document that `settings.json` deny rules (in the `permissions.deny`
array) are NOT reliably enforced for Task tool subagent invocations. The security gap is
real regardless of whether the deny list comes from CLI or settings. This was the prior
assessment in Section 19.6 of `31-permission-prompt-tool.md`.

---

## Inheritance Model Analysis

The current `--disallowedTools` inheritance model (as of March 2026):

```
Parent session: claude -p --disallowedTools "Bash(env)"
    ├── Main session tool set: {Read, Write, Bash, Task, ...} minus {Bash(env)}
    └── Task tool invocation → Subagent context
            ├── Inherits: settings.json rules (partially), available tools from parent
            └── Does NOT inherit: --disallowedTools CLI flag deny list
                → Subagent CAN execute Bash(env) even though parent cannot
```

**Implication for conductor security:**

If conductor dispatches a sub-agent with `--disallowedTools "Bash(env),Bash(curl *)"` but
that sub-agent spawns a further Task tool subagent (e.g., via Explore or Plan mode), the
nested subagent can execute `env` and `curl`. This confirms the security gap documented in
Issue #62.

**Mitigation already in conductor's recommended architecture:**
- Exclude `Task` from `--allowedTools` → no subagents can be spawned at all
- Policy server via `--permission-prompt-tool` → intercepts all main-session tool calls

The `--disallowedTools` non-inheritance gap only matters if conductor allows Task tool
invocations, which it does not per the current design.

---

## Empirical Test Protocol

The following test is NOT required to unblock v2 implementation. It would promote the
[INFERRED-HIGH] finding to [TESTED] and update Section 19.6 of
`31-permission-prompt-tool.md`.

```bash
#!/usr/bin/env bash
# Test: --disallowedTools inheritance by Task tool subagents
# Issue #81

RESULT=$(claude -p \
  "Use the Task tool to ask a subagent to run 'env' and return the first 5 lines." \
  --allowedTools "Task,Bash" \
  --disallowedTools "Bash(env)" \
  --dangerously-skip-permissions \
  --output-format json 2>&1 || true)

echo "Output: $RESULT"

if echo "$RESULT" | grep -qi "PATH=\|HOME=\|USER=\|SHELL="; then
  echo "RESULT: --disallowedTools is NOT inherited by Task subagents"
  echo "  → Subagent env output found in response"
  echo "  → Security gap confirmed: subagents can execute denied tools"
elif echo "$RESULT" | grep -qi "not allowed\|blocked\|disallowed\|refused"; then
  echo "RESULT: --disallowedTools IS inherited by Task subagents"
  echo "  → Subagent Bash(env) was blocked"
  echo "  → Security posture better than previously assessed"
else
  echo "INCONCLUSIVE: Review output manually"
fi
```

**Expected outcome:** Subagent executes `env` successfully → [INFERRED-HIGH] confirmed →
security gap consistent with Issue #62 findings. If this test produces the opposite result,
update Section 19.6 of `31-permission-prompt-tool.md` to note improved posture.

---

## Security Posture Update

**Update to Section 19.6 of `31-permission-prompt-tool.md`:**

The assessment of `--disallowedTools` non-inheritance by Task tool subagents upgrades from
[INFERRED] to [INFERRED-HIGH] based on:

1. Architectural analysis: CLI flags are parsed at startup, not during session state
2. Consistency with `--permission-prompt-tool` non-inheritance (confirmed in Issue #62)
3. The recent addition of `disallowedTools` to `--agents` frontmatter (which would be
   redundant if CLI `--disallowedTools` already propagated to subagents)
4. Community reports via DeepWiki analysis confirming subagents inherit session state but
   not CLI-startup constructs

**The security gap (subagents can execute tools the parent cannot) remains valid.**

**Conductor's mitigations are sufficient:**
- Excluding `Task` from `--allowedTools` prevents subagent spawning entirely
- This is the primary and sufficient mitigation per Issue #91 research

---

## Recommended Conductor Implementation

No change to existing conductor implementation recommended. The current architecture
(exclude `Task` from `--allowedTools`) already addresses the gap.

If conductor ever needs to allow Task invocations (e.g., for multi-step planning within
a single agent), use named agent frontmatter to enforce deny lists:

```json
// conductor-agents.json (passed via --agents)
{
  "agents": [
    {
      "name": "impl-subagent",
      "description": "Sub-agent for implementation tasks",
      "tools": ["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
      "disallowedTools": ["Bash(env)", "Bash(curl *)", "Bash(git push --force)"]
    }
  ]
}
```

This enforces per-agent deny lists at the Task invocation level rather than relying on
CLI flag inheritance.

---

## Follow-Up Research Recommendations

**[V2_RESEARCH] Run empirical test to promote to [TESTED]**
The test in Section 5 requires a live claude session with Task tool access. Run it and
update Section 19.6 of `31-permission-prompt-tool.md`. Not blocking for v2 implementation.

**[WONT_RESEARCH] settings.json deny rule inheritance fix**
Issues #25000 and #18950 document the deny rule gap. These are upstream Claude Code issues.
Conductor does not rely on `settings.json` deny rules for critical security controls.
No action.

---

## Sources

- [Claude Code Sub-agents Documentation](https://code.claude.com/docs/en/sub-agents)
- [Agent System and Subagents — DeepWiki](https://deepwiki.com/anthropics/claude-code/3.1-agent-system-and-subagents)
- [Feature: Add disallowed-tools to sub-agent frontmatter — Issue #6005](https://github.com/anthropics/claude-code/issues/6005)
- [Claude Code Guide — Agents and Subagents](https://deepwiki.com/shanraisshan/claude-code-best-practice/3.2-agents-and-subagents)
- [Best Practices for Claude Code Subagents — PubNub](https://www.pubnub.com/blog/best-practices-for-claude-code-sub-agents/)
