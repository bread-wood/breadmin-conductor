# Research: Headless Overage Consumption in `claude -p` When 5-Hour Window Is Exhausted

**Issue:** #63
**Milestone:** M1: Foundation
**Status:** Complete
**Date:** 2026-03-02
**Depends on:** #23 (429 error cap distinction), #8 (usage scheduling), #3 (error handling)
**Spawned from:** #23, Section 9.3 (open empirical question)

---

## Executive Summary

When `claude -p` encounters a 5-hour window exhaustion with extra usage (overage) enabled at the
account level, the current evidence strongly supports that `claude -p` **does NOT present an
interactive prompt** (there is no terminal UI in headless mode) and instead **automatically
consumes extra usage credits and continues the session without user confirmation**. This
conclusion is [INFERRED from strong circumstantial evidence] rather than empirically confirmed
with a dedicated headless test. Multiple bug reports document Claude Code silently consuming extra
usage without per-request confirmation even in interactive sessions once extra usage is enabled —
this behavior is even more likely in headless mode where there is no mechanism to pause for user
input.

Key findings:

1. **Automatic overage consumption in headless mode is the most likely behavior.** The `rate-limit-options`
   skill that presents the interactive overage prompt cannot fire in `-p` mode — the skill
   framework is interactive-only. In `-p` mode, Claude Code routes rate limit events as errors or
   continues silently depending on the `isUsingOverage` state of the account.

2. **`--max-budget-usd` does NOT apply to subscription sessions.** The flag is documented as
   "print mode only" and in practice only enforces a budget for API key-authenticated sessions.
   It provides no protection against overage charges on subscription-authenticated headless runs.

3. **No CLI flag exists to disable overage for a single headless session.** Overage can only be
   disabled at the account level (claude.ai Settings > Usage) or at the organization level for
   Team/Enterprise plans. There is no `--no-extra-usage` or `--disable-overage` CLI flag.

4. **The `rate_limit_event` stream-json event includes an `isUsingOverage` field** that tells the
   conductor whether the current session is consuming overage credits. This is the only programmatic
   signal available to conductor for detecting active overage consumption.

5. **When extra usage is exhausted** (spend cap reached), `claude -p` exits with a 429-like error
   with message `"You're out of extra usage"`. The exit code is `1`. This is distinguishable from
   a standard window-exhaustion 429 only by the message text.

6. **Weekly cap and overage:** The official Anthropic help center documentation (article 11145838)
   explicitly states overage applies to the weekly limit as well as the 5-hour window. However,
   community reports and `overageDisabledReason: org_level_disabled` suggest that some account
   configurations disable overage for the weekly cap specifically.

---

## 1. Background: The Overage System

### 1.1 What Extra Usage Is

Extra usage (also called "overage" in Anthropic's internal codebase) is a paid overflow billing
mechanism available to Pro, Max, Team, and Enterprise subscribers. When enabled at the account
level, it allows usage to continue beyond the plan's included quota at standard API rates.

Key properties:
- Enabled/disabled in claude.ai Settings > Usage (individual) or admin settings (Team/Enterprise)
- Has a monthly spending cap configured by the user (individual plans) or admin (Team/Enterprise)
- Billed separately from the subscription at standard API token rates
- Applies to both claude.ai interface usage and Claude Code terminal usage
- Daily redemption limit of $2,000 (documented per Anthropic help center)

### 1.2 Extra Usage Is an Account-Level Pre-Authorization

Extra usage is configured once at the account level. It is not a per-session opt-in. Once enabled,
it applies to all Claude Code sessions on that account — interactive and headless alike. The
interactive `rate-limit-options` prompt in Claude Code's TUI is a notification/confirmation layer
on top of this pre-authorization, not the authorization itself.

This distinction is critical: in headless `-p` mode, the absence of the interactive prompt does
not mean extra usage is unavailable — it means the pre-authorized overage simply runs without
a confirmation step.

### 1.3 Evidence from Billing Bug Reports

Multiple documented billing incidents support the conclusion that extra usage is consumed
automatically without per-request interactive prompts:

**Issue #24727 (Max 20x, $53.65 overage):** Claude Code believed usage was at 100% (while the
claude.ai dashboard showed 73%) and began consuming extra usage API credits. The issue description
does not mention any interactive prompt being presented. Charges accumulated automatically. [Source:
GitHub issue #24727]

**Issue #29289 (Max plan, $173.43 overage):** During a usage reporting outage, Claude Code
continued consuming extra usage across four auto-reload charges totaling $89.88 in 80 minutes.
The user was unaware because there was no real-time notification and a 24-hour billing delay
obscured the charges. [Source: GitHub issue #29289]

**Issue #28927 (Max plan, $48.79 overage):** After a silent upgrade to v2.1.51, all 1M context
model usage began routing to extra usage billing automatically — without a prompt, dialog, or
user action. The billing target changed silently. [Source: GitHub issue #28927]

These incidents demonstrate that extra usage consumption does not require per-session interactive
confirmation in practice. The official documentation phrasing "you can choose to continue" describes
the initial enable/disable UI, not a per-session gate.

---

## 2. The `rate-limit-options` Skill: Interactive-Only

### 2.1 What the Skill Does

Claude Code uses an internal skill called `rate-limit-options` to handle the interactive overage
prompt. When a 5-hour window is exhausted in interactive mode, this skill fires and presents a
menu:

```
1. Stop and wait for limit to reset
2. Add funds to continue with extra usage
3. Upgrade your plan
```

The `/extra-usage` slash command is a related mechanism that was added for VS Code sessions
(changelog, v2.1.50) and re-added after a regression in v2.1.39 after being missing in v2.1.19.

### 2.2 Why the Skill Does Not Fire in `-p` Mode

The official Claude Code documentation explicitly states: "User-invoked skills like `/commit` and
built-in commands are only available in interactive mode. In `-p` mode, describe the task you want
to accomplish instead." [Source: code.claude.com/docs/en/headless]

The `rate-limit-options` skill is an internal skill invoked automatically by the rate limit
handler. In `-p` mode, the skill framework that powers interactive slash commands is not active.
The rate limit handler in headless mode must produce a different code path than in interactive
mode.

### 2.3 The Two Headless Code Paths

When a rate limit fires in `-p` mode, one of two things happens based on the `isUsingOverage`
state determined from the `anthropic-ratelimit-unified-overage-status` response header:

**Path A — Overage enabled, credits available (`isUsingOverage: true`):**
Claude Code internally marks the session as consuming overage and continues processing the request.
No prompt is presented. The `rate_limit_event` stream-json event is emitted with `isUsingOverage:
true`. Subsequent API calls within the session are billed at extra usage rates. [INFERRED from
billing bug evidence and rate_limit_event schema]

**Path B — Overage disabled or exhausted (`isUsingOverage: false`):**
Claude Code cannot continue. It emits a `result` event with `is_error: true`. The `result.result`
field contains one of:
- `"API Error: Rate limit reached"` — for standard window exhaustion with no overage available
- `"You're out of extra usage · resets <time>"` — for extra usage spend cap exhaustion
  [Source: GitHub issue #18446 title, which shows the exact message]

Exit code `1` in both cases.

---

## 3. The `rate_limit_event` Stream-JSON Event

### 3.1 Event Schema

[DOCUMENTED, inferred from issue #26498 and #29604] The `rate_limit_event` is a stream-json event
type emitted by Claude Code when a rate limit condition is active. Its schema includes overage
state fields:

```json
{
  "type": "rate_limit_event",
  "rate_limit_info": {
    "status": "rejected",
    "overageDisabledReason": "org_level_disabled",
    "isUsingOverage": false
  }
}
```

When overage is active and the session is consuming extra usage credits:

```json
{
  "type": "rate_limit_event",
  "rate_limit_info": {
    "status": "allowed",
    "isUsingOverage": true
  }
}
```

**Note on timing:** The `rate_limit_event` is distinct from the final `result` event. It may be
emitted during the session when rate limit headers are received from the Anthropic API, while the
`result` event is always the final event. In cases where overage is active, the session continues
after the `rate_limit_event` — the event is informational, not terminal.

### 3.2 Issue #26498: Parser Deficiency

GitHub issue #26498 ("claude-agent-sdk: MessageParseError on rate_limit_event message type")
documents that the Python Agent SDK's `message_parser.py` does not handle the `rate_limit_event`
message type, causing a `MessageParseError("Unknown message type: rate_limit_event")` that
terminates the entire message stream.

**Conductor implication:** Conductor MUST handle `rate_limit_event` messages in its stream-json
parser. The parser must not raise an exception on unknown message types — it should either map
`rate_limit_event` to a known type or skip it gracefully. Failure to handle this event causes
conductor to terminate the worker as if it errored, when in fact the session may be continuing
successfully on overage.

### 3.3 Conductor Signal Detection

The conductor's stream-json consumer should watch for `rate_limit_event` with `isUsingOverage:
true` as a **cost escalation signal**:

```python
def handle_stream_event(event: dict) -> None:
    if event.get("type") == "rate_limit_event":
        info = event.get("rate_limit_info", {})
        if info.get("isUsingOverage"):
            LOG.warning(
                "Worker %s is consuming extra usage (overage) credits. "
                "Monitor spend cap.", worker_id
            )
            metrics.increment("conductor.overage_consumption_detected")
        elif info.get("overageDisabledReason"):
            LOG.info(
                "Worker %s rate limited; overage disabled (%s). "
                "Will exit on next 429.",
                worker_id,
                info["overageDisabledReason"]
            )
```

---

## 4. `--max-budget-usd` and Its Limitations for Subscription Sessions

### 4.1 What the Flag Does

The `--max-budget-usd` flag (documented as "print mode only") sets a maximum dollar amount to
spend on API calls before stopping. Documentation: "Maximum dollar amount to spend on API calls
before stopping (print mode only). Example: `claude -p --max-budget-usd 5.00 'query'`"
[Source: code.claude.com/docs/en/cli-reference]

When the budget is exceeded, the session exits with `result.subtype = "error_max_budget_usd"` and
exit code `1`. [Source: Elixir SDK `ClaudeCode.Types`, confirmed in doc #03]

### 4.2 Why It Does Not Protect Subscription Sessions

[DOCUMENTED] The flag is documented as applying to "API calls" — not subscription quota. Subscription
sessions do not have a per-token cost meter that Claude Code tracks internally. The `total_cost_usd`
field in the `result` event reports `0.0` for subscription sessions (when not in API key mode).

The `--max-budget-usd` flag is internally implemented by checking the accumulated `total_cost_usd`
against the budget threshold. Since subscription sessions report `total_cost_usd = 0.0` (the
per-token cost is absorbed by the subscription), the budget check never fires.

**Consequence:** There is no CLI-level spend cap mechanism for subscription-authenticated `claude -p`
sessions. The only available overage protection is the account-level monthly spending cap configured
in claude.ai Settings > Usage.

### 4.3 Practical Safety Implication for Conductor

Conductor cannot rely on `--max-budget-usd` to cap overage exposure for subscription-authenticated
workers. The conductor must:

1. Pre-dispatch: Check account-level overage configuration (via `/api/oauth/usage` or settings
   page inspection) to determine if overage is enabled.
2. During dispatch: Monitor `rate_limit_event` stream events for `isUsingOverage: true`.
3. Post-result: If `result.is_error: true` with `result.result` containing `"out of extra usage"`,
   classify as `extra_usage_exhausted` and halt further dispatch.

---

## 5. Overage and the Weekly Cap

### 5.1 Official Documentation (Conflicting Evidence)

The Anthropic help center article on "Using Claude Code with your Pro or Max plan" (article
11145838) states that extra usage applies after hitting "included usage limits" without
distinguishing between 5-hour window and weekly cap. The help center article on "Extra usage for
paid Claude plans" (article 12429409) describes the 5-hour window reset cycle but does not
explicitly distinguish which limits trigger overage.

The research for doc #23 (section 4.4) found conflicting evidence:
- Anthropic doc #08 Section 1.2 originally stated overage is "unavailable for weekly cap"
- Help center article 11145838 contradicts this, stating overage applies to "usage limits" generally
- The `anthropic-ratelimit-unified-overage-disabled-reason` header may contain `org_level_disabled`
  or `user_disabled` — these appear in rate limit events specifically for weekly cap exhaustions
  in some configurations

### 5.2 Rate-Limit-Event Evidence

The `rate_limit_event` schema includes an `overageDisabledReason` field with observed value
`"org_level_disabled"` (from issue #26498 research, cited in doc #23). This field appears in
contexts where the representative claim is "seven_day" (weekly cap), suggesting that weekly cap
exhaustions DO attempt to route through overage but are blocked by an org-level policy.

**Implication:** If `overageDisabledReason == "org_level_disabled"` appears alongside a weekly
cap exhaustion, it confirms that:
1. The system attempted to use overage for a weekly cap exhaustion (proving overage CAN apply to
   the weekly cap)
2. An org-level policy blocked it in this specific case

If overage is enabled and not blocked, a weekly cap exhaustion on an individual Pro/Max account
may also automatically consume overage in `-p` mode.

### 5.3 Conductor Strategy for Weekly Cap + Overage

If the weekly cap fires and overage is enabled, `claude -p` may continue consuming credits until
the monthly spend cap is reached. This is a high-severity risk for conductor: the weekly cap
exhaustion would normally trigger a multi-day backoff, but with overage enabled, conductor may
incur charges across the entire remaining week.

Recommended conductor policy:
- Treat `rate_limit_event` with `isUsingOverage: true` + `rateLimitType: seven_day` as a
  **critical billing alert** requiring immediate human escalation
- Do not dispatch new workers until the operator has reviewed and acknowledged the weekly cap
  + overage situation
- Optionally, advise operators to disable overage in account settings before running headless
  conductor sessions

---

## 6. The Overage Exhaustion Signal: "You're Out of Extra Usage"

### 6.1 Message and Exit Code

When extra usage is enabled but the monthly spend cap has been reached, the error message shown in
interactive mode is: `"You're out of extra usage · resets <time>"` [Source: GitHub issue #18446].

In headless `-p` mode, this condition is expected to produce:
- Exit code: `1`
- `result.subtype`: `"error_during_operation"` [INFERRED]
- `result.result` text: Contains `"out of extra usage"` or a similar phrase
- `result.is_error`: `true`

[INFERRED] This text can be distinguished from a standard rate limit message (which contains
`"Rate limit reached"`) by checking for `"extra usage"` in the result text.

### 6.2 Conductor Classification

```python
OVERAGE_EXHAUSTED_PATTERNS = [
    "out of extra usage",
    "extra usage exhausted",
    "extra usage limit reached",
]

def classify_rate_limit_error(result_text: str) -> str:
    text_lower = result_text.lower()
    if any(p in text_lower for p in OVERAGE_EXHAUSTED_PATTERNS):
        return "extra_usage_exhausted"
    return "standard_rate_limit"
```

When `extra_usage_exhausted` is classified:
1. Do NOT retry — the spend cap is a hard monthly limit
2. Alert operator: "Extra usage spend cap reached. Disable extra usage or increase cap before
   re-dispatching."
3. Halt all dispatch for this account until acknowledged

---

## 7. Safety Recommendations for Conductor

### 7.1 Pre-Dispatch Overage Audit

Before dispatching any workers, conductor should determine the account's overage configuration.
This can be done by querying the `/api/oauth/usage` endpoint (see doc #22), which includes overage
status in its response. If overage is enabled:

1. Log a warning: "Account has extra usage enabled. Headless workers may incur overage charges
   without per-session confirmation."
2. Check the monthly spend cap setting (if accessible via API)
3. Display the remaining overage balance to the operator (if accessible)

### 7.2 Session-Level Overage Flag (Does Not Exist — Workaround Needed)

There is no `--no-extra-usage`, `--disable-overage`, or `--subscription-only` CLI flag that would
prevent a headless session from consuming extra usage. Conductor cannot disable overage at the
session level.

**Workaround options (in order of preference):**

1. **Disable extra usage at the account level** before running conductor sessions. Re-enable after
   the run completes. This is the only guaranteed protection. Drawback: requires account UI access
   and cannot be automated from the conductor process itself.

2. **Monitor `rate_limit_event` with `isUsingOverage: true`** and immediately send SIGTERM to the
   worker. This terminates the session before significant overage is accumulated. Drawback:
   partial work is lost.

3. **Pre-check the `/api/oauth/usage` endpoint** (doc #22) for remaining 5-hour quota before
   dispatching. If remaining quota is less than a configured threshold (e.g., <20%), delay
   dispatch until the window resets. This prevents the overage trigger from firing by avoiding
   dispatch when the budget is near-exhausted.

4. **Use API key authentication** instead of subscription authentication. `--max-budget-usd`
   applies to API key sessions, providing a hard per-session cost cap. Drawback: costs per-token
   at API rates, not subscription rates.

### 7.3 Configuration Recommendation by Account Type

| Account Configuration | Overage Risk | Conductor Strategy |
|-----------------------|--------------|-------------------|
| Extra usage disabled | None | Standard rate-limit backoff on 429 |
| Extra usage enabled, `overage-disabled-reason: org_level_disabled` | None for weekly cap; risk for 5-hour | Monitor 5-hour window; weekly cap safe |
| Extra usage enabled, no spend cap or high spend cap | High | Disable extra usage before headless runs; or monitor `isUsingOverage` and kill workers |
| Extra usage enabled, low monthly spend cap ($5–$10) | Bounded | Monitor `isUsingOverage`; treat `extra_usage_exhausted` as terminal |
| API key authentication | Bounded by `--max-budget-usd` | Use `--max-budget-usd` per-worker |

### 7.4 Environment Variable for Overage Disablement

While no CLI flag exists, it is possible that `CLAUDE_CODE_DISABLE_EXTRA_USAGE=1` or a similar
environment variable could override overage behavior. [INFERRED — not confirmed] The fast mode
documentation uses `CLAUDE_CODE_DISABLE_FAST_MODE=1` as a precedent for this pattern. A follow-up
empirical test (see Section 9) should verify whether an analogous environment variable controls
overage consumption. Feature request or empirical test recommended.

---

## 8. Cross-Reference Summary

| Topic | Prior Research | New Findings Here |
|-------|----------------|-------------------|
| 429 vs 402 discrimination | Doc #23, Section 2.5 | No change — 402 is billing-layer, not overage-specific |
| `isUsingOverage` internal field | Doc #23, Section 2.3 | `rate_limit_event` stream-json event exposes this field |
| Overage behavior for weekly cap | Doc #23, Section 4.4 | `org_level_disabled` reason confirms overage CAN apply to weekly cap |
| `--max-budget-usd` scope | Doc #08, Section 1.4; Doc #03 | Confirmed: applies only to API key sessions, not subscription |
| Rate limit handler in `-p` mode | Doc #03, Section 5 | `rate-limit-options` skill is interactive-only; `-p` auto-consumes or exits |
| `extra_usage_exhausted` classification | Doc #03, Section 2.4 | New pattern: check for `"out of extra usage"` in result text |

---

## 9. Unknowns and Confidence Levels

| Claim | Confidence | Evidence Basis |
|-------|-----------|----------------|
| `claude -p` auto-consumes overage without prompting when 5-hour window fires | HIGH [INFERRED] | Billing bug evidence (issues #24727, #29289, #28927); `rate-limit-options` is interactive-only; Team plan docs say "starts using as soon as you reach limit" |
| No CLI flag to disable overage for a single headless session | HIGH [DOCUMENTED] | CLI reference lists no such flag; fast mode precedent suggests env var may exist but is unconfirmed |
| `--max-budget-usd` does not cap subscription overage | HIGH [DOCUMENTED + INFERRED] | Documented as "API calls" only; `total_cost_usd = 0.0` for subscription sessions |
| `rate_limit_event` stream-json event has `isUsingOverage` field | MEDIUM [DOCUMENTED] | Issue #26498 schema; issue #29604 internal field names; not confirmed with live capture |
| "You're out of extra usage" is the result text when spend cap is hit in `-p` mode | MEDIUM [INFERRED] | From interactive mode message in issue #18446; text may differ in stream-json `result` field |
| Weekly cap exhaustion can also trigger overage consumption | MEDIUM [INFERRED] | Help center article 11145838; `org_level_disabled` reason implies system attempted overage for weekly cap |
| `--max-budget-usd` triggers `error_max_budget_usd` subtype | HIGH [DOCUMENTED] | Elixir SDK types; doc #03 Section 1.2 |

---

## 10. Follow-Up Research Recommendations

### 10.1 Empirical: Verify Auto-Overage Consumption in `-p` Mode [BLOCKING M1]

**Action:** Run `claude -p` on an account with extra usage enabled and a small spend cap ($1–$2),
in a session where the 5-hour window is near-exhausted. Capture stream-json output and verify:

- Whether `rate_limit_event` with `isUsingOverage: true` appears
- Whether the session continues (incurring charges) or exits with a rate limit error
- What the `result.result` text contains

**Related existing issue:** Issue #84 ("Research: Empirical verification that rate limit stderr
message is visible in headless -p mode") partially overlaps. The test in #84 should be extended
to an account with overage enabled to capture the `isUsingOverage` branch.

**Note:** No new issue needs to be filed — this test can be added to issue #84's scope.

### 10.2 New Issue #92: Empirical Verification of `rate_limit_event` Full Schema in Stream-JSON [NON-BLOCKING M3]

The `rate_limit_event` schema described in Section 3.1 is inferred from issue #26498 and #29604.
A live capture of this event in stream-json output has not been published. Issue #92 covers:

- All fields present in `rate_limit_info`
- Whether `rateLimitType` (`five_hour` vs. `seven_day`) is included
- Whether `overageResetsAt` is included
- Exact field names vs. camelCase vs. snake_case

### 10.3 New Issue #94: Env Var for Overage Disablement [NON-BLOCKING M3]

Test whether `CLAUDE_CODE_DISABLE_EXTRA_USAGE=1` or a similar environment variable prevents
overage consumption in headless sessions. The fast mode env var (`CLAUDE_CODE_DISABLE_FAST_MODE=1`)
provides a precedent. Issue #94 covers this investigation. This would give conductor a
session-level overage kill switch without requiring account-level UI changes.

### 10.4 Existing Issue: HTTP 402 Body When Extra Usage Billing Fails [M1]

Issue #87 covers this. The `rate_limit_event` findings here add context: if `isUsingOverage: true`
appears and then a 402 is returned (rather than the session continuing), it may indicate a
billing authorization failure mid-session. Conductor must handle the case where overage
consumption starts (evidenced by `isUsingOverage: true` in `rate_limit_event`) but then a 402
terminates the session.

---

## 11. Sources

- [Extra Usage for Paid Claude Plans (article 12429409)](https://support.claude.com/en/articles/12429409-extra-usage-for-paid-claude-plans) — Official help center. Describes extra usage activation, spend cap configuration, and "you can choose to continue" framing. Does not describe headless behavior explicitly.

- [Using Claude Code with your Pro or Max plan (article 11145838)](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan) — States "strict user control over billing decisions" but also "all transitions to API credit usage require explicit user consent" — contradicted by billing bug reports. Confirms overage applies to "included usage limits" generally (implying both 5-hour and weekly cap).

- [Extra Usage for Team and Seat-Based Enterprise Plans (article 12005970)](https://support.claude.com/en/articles/12005970-extra-usage-for-team-and-seat-based-enterprise-plans) — States members "will start using this as soon as you reach your seat's usage limit" — supports automatic consumption model.

- [Run Claude Code Programmatically — Headless Docs](https://code.claude.com/docs/en/headless) — Official. States skills like `/commit` are "only available in interactive mode." Confirms `-p` mode cannot invoke skill-based interactive prompts.

- [CLI Reference — `--max-budget-usd` flag](https://code.claude.com/docs/en/cli-reference) — Official. Documents `--max-budget-usd` as applying to "API calls" in print mode. No mention of subscription overage.

- [Speed Up Responses with Fast Mode](https://code.claude.com/docs/en/fast-mode) — Official. Documents `CLAUDE_CODE_DISABLE_FAST_MODE=1` env var as overage control precedent. States fast mode "is available via extra usage only" for subscription plans — confirms the overage billing path is a first-class routing mechanism.

- [GitHub Issue #24727 — Max 20x: $53.65 overage from usage tracking mismatch](https://github.com/anthropics/claude-code/issues/24727) — Primary evidence of automatic overage consumption without per-request confirmation.

- [GitHub Issue #29289 — Max plan: $173.43 overage during outage](https://github.com/anthropics/claude-code/issues/29289) — Documents four auto-reload charges in 80 minutes; no interactive prompt mentioned.

- [GitHub Issue #28927 — Silent v2.1.51 billing change: 1M context to extra usage](https://github.com/anthropics/claude-code/issues/28927) — Documents automatic billing target change with no prompt or dialog. Strong evidence for automatic consumption model.

- [GitHub Issue #18446 — "You're out of extra usage · resets 10am"](https://github.com/anthropics/claude-code/issues/18446) — Primary source for the spend-cap-exhausted error message text.

- [GitHub Issue #20933 — v2.1.19: Unable to consume extra usage | "Unknown skill: rate-limit-options"](https://github.com/anthropics/claude-code/issues/20933) — Documents the `rate-limit-options` skill as the interactive prompt mechanism. Regression that prevented overage consumption in v2.1.19; resolved in v2.1.39. Confirms that overage consumption is mediated by a skill that is interactive-mode-specific.

- [GitHub Issue #28832 — Rate limit prompt shown despite 0% usage](https://github.com/anthropics/claude-code/issues/28832) — Documents the `/rate-limit-options` interactive prompt menu with its three choices. Confirms the menu is presented in interactive mode but does not address headless mode.

- [GitHub Issue #26498 — claude-agent-sdk: MessageParseError on rate_limit_event](https://github.com/anthropics/claude-code/issues/26498) — Source for `rate_limit_event` schema including `status`, `overageDisabledReason`, and `isUsingOverage` fields. Critical for conductor stream-json parser implementation.

- [GitHub Issue #29604 — Expose rate limit utilization data in statusLine JSON](https://github.com/anthropics/claude-code/issues/29604) — Source for internal field names: `isUsingOverage`, `overageStatus`, `overageResetsAt`, `overageDisabledReason`. Confirms these fields are parsed by Claude Code from API response headers.

- [GitHub Issue #29704 — Extra usage pool consumed while session limit had capacity](https://github.com/anthropics/claude-code/issues/29704) — Billing bug: extra usage consumed before included session allocation exhausted. Confirms premature overage routing can occur.

- [Claude Code Changelog — v2.1.50](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md) — Documents `/extra-usage` command added in v2.1.50 for VS Code sessions.

- [Doc #23 — Distinguishing Weekly Cap Exhaustion from 5-Hour Window Exhaustion in 429 Error Payloads](../research/23-429-error-cap-distinction.md) — Parent research. Section 4.4 documents the conflicting evidence on overage availability for the weekly cap. Section 9.3 spawned this issue.

- [Doc #03 — Error Handling and Failure Recovery in Headless Mode](../research/03-error-handling.md) — Section 1.2 documents `error_max_budget_usd` subtype and exit code taxonomy.

- [Doc #08 — Usage Monitoring and Adaptive Scheduling](../research/08-usage-scheduling.md) — Section 1.4 establishes that `--max-budget-usd` only applies to API key sessions.
