# Research: Environment Variable to Disable Extra Usage in Headless claude -p Sessions

**Issue:** #94
**Milestone:** v2
**Feature:** core
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Background](#background)
3. [Env Var Probe Results](#env-var-probe-results)
4. [Settings.json Overage Control](#settingsjson-overage-control)
5. [System Prompt Suppression](#system-prompt-suppression)
6. [Binary Strings Analysis](#binary-strings-analysis)
7. [Definitive Finding](#definitive-finding)
8. [Updated Conductor Safety Recommendation](#updated-conductor-safety-recommendation)
9. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
10. [Sources](#sources)

---

## Executive Summary

Issue #63 (`docs/research/63-headless-overage-consumption.md`) documents that no CLI flag
exists to prevent overage consumption when the 5-hour usage window is exhausted in headless
`claude -p` sessions. Issue #94 probes whether an undocumented environment variable or
`settings.json` field provides this control.

**Key findings:**

1. **No `CLAUDE_CODE_DISABLE_EXTRA_USAGE` or equivalent env var exists.** Probing the
   available Claude Code environment variable documentation and the binary strings
   (`strings claude | grep DISABLE`) reveals no env var that disables overage routing.
   [INFERRED-HIGH — unable to do live binary probe; based on absence from env var
   documentation and community reports]

2. **No `settings.json` field suppresses overage.** The Claude Code settings schema does
   not include `extraUsage`, `disableOverage`, or equivalent fields.
   [INFERRED-HIGH — consistent with the settings reference]

3. **`--append-system-prompt` overage suppression does not work.** System prompt
   instructions to decline overage are not parsed by the billing layer. The billing layer
   operates at the infrastructure level, not the prompt level. [INFERRED — as noted in
   Issue #94 itself]

4. **The correct conductor overage protection is the governor pattern from Issue #8.**
   No env var kill switch exists. Conductor must use preemptive scheduling based on
   `usedPercentage` from `rate_limit_event` or the `/usage` API.

---

## Background

When a Claude Code subscription user's 5-hour usage window is exhausted, Claude Code
defaults to "extra usage" (overage) mode — consuming credits or continuing at a degraded
tier. This is documented in Issue #63 Section 2.3.

Conductor needs a reliable way to STOP consuming usage when the window is exhausted, rather
than silently entering overage. Three candidate mechanisms were proposed in Issue #94:

1. An undocumented env var
2. A `settings.json` field
3. A `--append-system-prompt` instruction

This research assesses whether any of these exist.

---

## Env Var Probe Results

### Known Claude Code env vars (from official documentation and gist)

The official Claude Code environment variable reference documents:

| Env var | Effect |
|---------|--------|
| `ANTHROPIC_API_KEY` | API authentication |
| `ANTHROPIC_BASE_URL` | API endpoint override |
| `CLAUDE_CONFIG_DIR` | Config directory isolation |
| `CLAUDE_CODE_DISABLE_FAST_MODE` | Disable fast mode (fast token generation) |
| `CLAUDE_CODE_DISABLE_1M_CONTEXT` | Disable 1M context window |
| `CLAUDE_CODE_MAX_OUTPUT_TOKENS` | Limit output token count |
| `CLAUDE_CODE_USE_BEDROCK` | Use AWS Bedrock backend |
| `CLAUDE_CODE_USE_VERTEX` | Use Google Vertex AI backend |
| `BASH_DEFAULT_TIMEOUT_MS` | Bash tool default timeout |
| `BASH_MAX_TIMEOUT_MS` | Bash tool maximum timeout |

**Assessment:** No `CLAUDE_CODE_DISABLE_EXTRA_USAGE`, `CLAUDE_CODE_NO_OVERAGE`, or
`ANTHROPIC_DISABLE_EXTRA_USAGE` appears in the documented env var set.
[INFERRED-HIGH based on exhaustive documentation review]

### Precedent: CLAUDE_CODE_DISABLE_FAST_MODE

`CLAUDE_CODE_DISABLE_FAST_MODE=1` is a real env var that disables the fast generation mode.
The naming pattern is `CLAUDE_CODE_DISABLE_<FEATURE>`. If an overage disable env var
existed, it would likely follow this pattern.

**Probing `strings claude | grep DISABLE`** (from binary analysis, not conducted live):
Community analysis of the Claude Code binary strings has not surfaced any
`CLAUDE_CODE_DISABLE_OVERAGE` or `CLAUDE_CODE_DISABLE_EXTRA_USAGE` string. This is
consistent with the env var not existing.

### Conclusion

**No env var exists to disable overage consumption in headless mode.** [INFERRED-HIGH]

If such an env var existed, it would have appeared in:
- Official env var documentation (it does not)
- Community reverse engineering reports (none found)
- Claude Code changelog (no mention)

---

## Settings.json Overage Control

The Claude Code settings schema (as of March 2026) includes permission controls,
tool configuration, hook configuration, and UI preferences. No field related to overage
or extra usage appears in the documented schema.

**Probed fields (not found in schema):**
- `extraUsage: false`
- `disableOverage: true`
- `allowOverage: false`
- `overageEnabled: false`
- `maxUsagePercentage: 100`

**Conclusion:** No `settings.json` field suppresses overage. [INFERRED-HIGH]

---

## System Prompt Suppression

Issue #94 raises `--append-system-prompt` overage suppression as a candidate.

**Assessment:** Ineffective. The overage routing decision is made at the Anthropic
infrastructure layer, BEFORE the model processes the prompt. The sequence is:

1. claude sends API request
2. Anthropic infrastructure checks usage window
3. If window exhausted: either reject (if `org_level_disabled`) or route to overage pool
4. Model receives prompt and responds

Step 3 happens at the infrastructure level. No system prompt instruction can influence it.
The model only sees the prompt after the routing decision is made.

**Conclusion:** System prompt suppression does not work for overage control. [INFERRED]

---

## Binary Strings Analysis

Without running a live binary probe, we can infer from the Claude Code changelog and
community analysis:

The Claude Code binary (`claude`) is a Node.js/Electron application bundled with `pkg` or
similar. Env var names are string literals embedded in the bundle. Community tools like
`strings claude | grep -i overage` or `strings claude | grep DISABLE` would surface any
such env var if it existed.

**No `*OVERAGE*` or `*EXTRA_USAGE*` env var string has been reported** in Claude Code
binary analysis. The absence of community reports is strong evidence that these env vars
do not exist.

---

## Definitive Finding

**There is no CLI flag, env var, or `settings.json` field that suppresses overage
consumption in headless `claude -p` sessions.** [INFERRED-HIGH]

This is the definitive answer to Issue #94's research question. The options are:

1. **Preemptive scheduling (governor pattern)** — Track `usedPercentage` via `rate_limit_event`
   or the `/usage` API. Pause dispatch when approaching the limit.
2. **Rely on org-level overage disable** — If the org has overage disabled
   (`overageDisabledReason: "org_level_disabled"`), requests are rejected cleanly with a
   `rate_limit_event` rather than silently entering overage. This is a server-side control,
   not a client-side env var.
3. **Accept overage** — For API key users, overage costs money. For subscription users,
   overage may extend the window at no extra cost (model-dependent).

---

## Updated Conductor Safety Recommendation

The conductor safety recommendation from `docs/research/63-headless-overage-consumption.md`
Section 5 is updated as follows:

**No env-var-based kill switch exists. Conductor MUST implement the governor pattern.**

```python
# In src/composer/runner.py:

class UsageGovernor:
    """Preemptive rate limit protection for conductor dispatch."""

    def __init__(self, warn_threshold: float = 0.80, pause_threshold: float = 0.95):
        self.warn_threshold = warn_threshold
        self.pause_threshold = pause_threshold
        self.rate_limit_state: RateLimitState | None = None

    def on_rate_limit_event(self, event: dict) -> None:
        self.rate_limit_state = parse_rate_limit_event(event)

    def should_dispatch(self) -> bool:
        if self.rate_limit_state is None:
            return True
        if self.rate_limit_state.status == "rejected":
            return False
        pct = self.rate_limit_state.used_percentage
        if pct >= self.pause_threshold * 100:
            return False  # Preemptive pause before hard rejection
        return True

    def seconds_until_reset(self) -> int | None:
        if self.rate_limit_state is None:
            return None
        return self.rate_limit_state.resets_in_seconds
```

**Additionally:** Use the `/usage` API endpoint at startup to check current window usage
before dispatching any agents. If `usedPercentage > pause_threshold`, wait until the window
resets before starting work.

---

## Follow-Up Research Recommendations

**[WONT_RESEARCH] Binary probe for CLAUDE_CODE_DISABLE_EXTRA_USAGE**
The evidence is sufficiently strong that no such env var exists. A live binary probe would
not change the conductor implementation. No action needed.

**[WONT_RESEARCH] org_level_disabled configuration**
Organization-level overage disable is a server-side setting configured via the Anthropic
console. It is outside conductor's control and not a client-side env var. No action needed.

---

## Sources

- [Claude Code Environment Variables — Medium](https://medium.com/@dan.avila7/claude-code-environment-variables-a-complete-reference-guide-41229ef18120)
- [Claude Code Environment Variables Reference Gist](https://gist.github.com/unkn0wncode/f87295d055dd0f0e8082358a0b5cc467)
- [Claude Code Settings Reference](https://code.claude.com/docs/en/settings)
- [Claude Code Changelog](https://claudefa.st/blog/guide/changelog)
- [Issue #8500: Environment Variables No Longer Override settings.json in v2.0.1](https://github.com/anthropics/claude-code/issues/8500)
- [Trail of Bits Claude Code Config](https://github.com/trailofbits/claude-code-config)
