# Research: Usage Monitoring and Adaptive Scheduling for Pro/Max Accounts

**Issue:** #8
**Milestone:** M1: Foundation
**Status:** Complete

## Overview

`claude -p` sessions launched by conductor draw from the same 5-hour rolling usage pool
as the interactive Claude UI. Running 3–5 parallel sub-agents against a Pro or Max 5x
account can exhaust a usage window in 15–75 minutes instead of 5 hours. This document
defines what limits apply, how to detect approaching exhaustion, and how conductor's
scheduler should behave under pressure.

---

## 1. Pro vs. Max Usage Limits

### 1.1 5-Hour Rolling Window

All plans enforce a **rolling 5-hour window**, not a fixed daily reset. Anthropic uses a
sliding window: as individual messages pass the 5-hour mark, that capacity becomes
available again. There is no single daily-reset clock — the window is continuous. Unused
quota does not roll over.

Approximate message budgets per 5-hour window by plan and model (from independent
testing; Anthropic does not publish exact numbers):

| Plan | Price | Opus 4 / 5-hr | Sonnet 4 / 5-hr | Haiku / 5-hr |
|---|---|---|---|---|
| Pro | $20/month | ~45 | ~100 | ~300 |
| Max 5x | $100/month | ~225 | ~500 | Near-unlimited |
| Max 20x | $200/month | ~900 | ~2,000 | Unlimited |

"Message" is token-weighted, not a flat count. A message in turn 50 of a long agentic
conversation includes full conversation history, consuming far more tokens than a fresh
message. Sub-agents running long-context tasks drain the budget significantly faster than
the raw message counts suggest.

**Key caveat:** Anthropic has not published the absolute token limits backing these
message counts, and the conversion of tokens to "usage percentage" is not documented.
Community measurement data (e.g., the `claude-code-limit-tracker` project) suggests
that the rate is non-linear and can vary with model, conversation length, and tool use
intensity.

### 1.2 Weekly Active-Hours Cap (Added August 28, 2025)

In addition to the 5-hour window, Anthropic introduced weekly caps targeting the top
~5% of heavy users. "Active hours" are periods when Claude models are actively processing
tokens or executing code-related reasoning (excluding idle periods like file browsing or
conversation pauses).

| Plan | Sonnet 4 active hours / week | Opus 4 active hours / week |
|---|---|---|
| Pro | 40–80 h | — (Opus not available on Pro for Claude Code) |
| Max 5x | 140–280 h | 15–35 h |
| Max 20x | 240–480 h | 24–40 h |

A conductor running 3 parallel implementation agents in Opus on Max 5x could consume the
full 35-hour Opus weekly budget in a single day of sustained multi-agent runs. This is
the more binding limit for intensive headless automation.

When the weekly cap is hit, the account is locked out until the weekly cycle resets,
with no option to purchase additional time (unlike the 5-hour window, where
consumption-based overage is available on some plans).

### 1.3 Usage Shared Across All Claude Products

All activity in Claude.ai (web, desktop, mobile), Claude Code (interactive and `-p`),
and the Claude Agent SDK (when authenticated with a subscription rather than an API key)
counts against the **same single usage pool**. A conductor session running at 2am
competes with the user's daytime interactive Claude.ai usage.

### 1.4 API Key vs. Subscription Authentication

If `ANTHROPIC_API_KEY` is set in the environment, Claude Code uses API key
authentication and **charges per token at API rates** — it does not draw from the
subscription usage pool. The `--max-budget-usd` flag (documented below) applies only to
API key sessions.

Subscription (claude.ai login) authentication routes through Anthropic's subscription
rate limiting infrastructure. There is no API-style spend meter for subscription
sessions.

---

## 2. Rate Limit Detection Signals from `claude -p`

### 2.1 HTTP 429 Error Structure

When the usage limit is reached, the Anthropic API returns HTTP 429. The error payload
observed in Claude Code logs:

```json
{
  "type": "error",
  "error": {
    "type": "rate_limit_error",
    "message": "This request would exceed your account's rate limit. Please try again later."
  },
  "request_id": "req_011CXL8s7Q7RktxAHLJeH2TD"
}
```

The terminal UI in interactive mode shows a message like:
```
You've hit your limit · resets 4pm (Asia/Kuala_Lumpur)
```

### 2.2 Exit Codes

The Claude Code CLI does not document a dedicated exit code for rate limiting. Community
observations indicate:

- **Exit code 0**: Successful completion.
- **Exit code 1**: General error, including rate limit errors and model-not-available
  errors. The error text appears on stderr as `API Error: Rate limit reached`.
- **Exit code 124**: Timeout (when a wrapper script uses `timeout`).

**Important warning on misleading errors:** The CLI surfaces multiple distinct conditions
as the generic `API Error: Rate limit reached` message:
- Actual 429 rate limit from usage exhaustion
- Model not available on the current subscription tier (e.g., using a Sonnet 1M model
  not supported on Max)
- Content safety filter triggers (GitHub issue #25778)
- Hidden internal rate limits not visible in the usage dashboard (GitHub issue #22876)

Conductor must not treat exit code 1 with this message as *only* a usage-limit signal.
Parse the full error text to distinguish these cases.

### 2.3 `--output-format stream-json` Events

The `stream-json` output format emits newline-delimited JSON. Key event types observed:

| Event type | Subtype | Description |
|---|---|---|
| `system` | `init` | Session start, includes `session_id`, available tools |
| `assistant` | — | Claude's message content blocks |
| `user` | — | Tool results from tool execution |
| `stream_event` | — | Raw API streaming events (with `--include-partial-messages`) |
| `result` | — | Final result with `session_id`, `is_error`, cost metadata |

A rate limit error during a session manifests as a `result` event with `is_error: true`
and error text in the result. The `is_error: true` flag is the reliable programmatic
signal for non-zero completion. Example pattern to detect:

```bash
claude -p "..." --output-format stream-json 2>&1 | \
  jq 'select(.type == "result") | {is_error, result}'
```

If the process terminates with `is_error: true` **and** the result text contains
`rate_limit_error` or `rate limit`, conductor should treat it as a rate-limit event
rather than an implementation failure.

**Critical gap:** The `stream-json` output does **not** currently include usage
utilization data. Claude Code internally reads the `anthropic-ratelimit-unified-*`
response headers from each API call but does not surface them in the stream-json payload.
Multiple open feature requests exist for this (#19385, #29604, #29721) as of March 2026,
none resolved.

### 2.4 `anthropic-ratelimit-unified-*` Headers (Internal to Claude Code)

These HTTP response headers are returned by the Anthropic API on every request made by
Claude Code. Claude Code reads them internally and uses them to display warnings like
"Approaching usage limit • resets at 7pm" in the interactive UI. The headers include:

| Header | Description |
|---|---|
| `anthropic-ratelimit-unified-*-utilization` | Current usage as a percentage (0–1) |
| `anthropic-ratelimit-unified-*-reset` | RFC 3339 timestamp when the window resets |
| `anthropic-ratelimit-unified-*-surpassed-threshold` | Threshold crossed (e.g., 75%, 80%) |
| `anthropic-ratelimit-unified-status` | `allowed` / `allowed_warning` / `rejected` |
| `anthropic-ratelimit-unified-overage-status` | Whether consumption-based overage is active |
| `anthropic-ratelimit-unified-representative-claim` | Rate limit type/window identifier |

These headers are available on the Anthropic API for API key users. For subscription
sessions, they are read internally by Claude Code but not passed through to the caller
via stream-json or any other mechanism available as of March 2026.

### 2.5 The `/usage` Command (Interactive Mode Only)

The `/usage` slash command in Claude Code's REPL shows current subscription utilization.
It is **not available** in `-p` (headless/print) mode. There is no equivalent CLI
one-shot command to query remaining quota. Feature request #28999 proposes adding this
to the `statusLine` JSON hook; open as of March 2026.

**Workaround available:** An undocumented OAuth endpoint (`/api/oauth/usage`) returns
aggregate usage data. An unofficial open-source project
(`https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor`) reverse-engineered this
endpoint to build a real-time quota monitor. This approach is fragile and unsupported.

---

## 3. Pre-Dispatch Usage Introspection

### 3.1 Current State: No Official Mechanism

As of March 2026, there is **no official, stable API or CLI command** to query remaining
usage headroom before dispatching a new agent. The options are:

| Method | Reliability | Notes |
|---|---|---|
| Parse `/api/oauth/usage` OAuth endpoint | Fragile | Reverse-engineered; may change without notice |
| Read `anthropic-ratelimit-unified-*-utilization` header | Not accessible from conductor | Internal to Claude Code subprocess |
| Read the `~/.claude/` JSONL session logs | Indirect | Can estimate token spend, but cannot convert to quota % without knowing absolute limits |
| Run a cheap probe request and check result headers | Moderate | Issues a real API call; consumes quota; subscription sessions may not expose headers |

### 3.2 Recommended Approach: Conservative Quota Accounting

Because direct introspection is not reliably available, conductor should implement
**conservative internal accounting**:

1. Maintain a ledger of estimated token usage per completed agent run.
2. Before dispatch, check if the estimated budget remaining exceeds the estimated cost
   of the next agent type.
3. Use the `anthropic-ratelimit-unified-status: rejected` event (if it ever becomes
   accessible) or a 429 result as the definitive signal to halt dispatch.

This approach degrades gracefully: it is conservative (will sometimes defer work that
could safely run) but never leaves issues in-progress with no PR due to mid-run
exhaustion.

---

## 4. Recommended Concurrency Limits by Tier

Based on observed token drain rates and plan capacities:

| Tier | Recommended Max Concurrency | Rationale |
|---|---|---|
| Pro ($20) | 1–2 agents | With 5 parallel agents, a Pro window exhausts in ~15 min. 1–2 prevents runaway. |
| Max 5x ($100) | 2–3 agents | With 5 agents, Max 5x exhausts in ~75 min. 2–3 gives reasonable session lifetime. |
| Max 20x ($200) | 3–5 agents | 20x budget; community reports users rarely exhaust allocation on Max 20x. |
| API key (any tier) | Per API tier limits | Governed by RPM/ITPM/OTPM; see Anthropic API rate limit tables. |

These are conservative recommendations. The safe concurrency for Opus vs. Sonnet
differs significantly:

- **Opus 4**: Higher token cost per turn; reduce concurrency by ~50% vs. Sonnet
  estimates above.
- **Sonnet 4**: Use the estimates in the table directly.
- **Haiku**: Could safely run at 2x–3x higher concurrency than Sonnet, but is unlikely
  to be used for implementation or research agents.

**Recommendation for conductor:** Default to `CONDUCTOR_MAX_CONCURRENCY = 2` as a safe
starting point that works across all subscription tiers. Expose this as a configurable
setting so users can tune it to their plan.

---

## 5. Governor Design

### 5.1 Overview

The conductor scheduler needs a **usage governor** — a component that sits between the
issue queue and the agent dispatch loop. The governor enforces concurrency limits,
detects rate limiting, and manages backoff and requeue.

```
Issue Queue
    │
    ▼
┌─────────────────────────────────────────────────┐
│  Usage Governor                                 │
│  - Check: running_agents < max_concurrency?     │
│  - Check: estimated_budget_remaining > cost?    │
│  - Gate: if rejected → defer to defer_queue     │
└───────────┬─────────────────────────────────────┘
            │ dispatch
            ▼
    Agent Subprocess (claude -p)
            │ result
            ▼
┌─────────────────────────────────────────────────┐
│  Result Handler                                 │
│  - is_error? → classify error type              │
│  - rate_limit? → backoff, requeue, alert        │
│  - success? → update ledger, dequeue next       │
└─────────────────────────────────────────────────┘
```

### 5.2 Pre-Dispatch Check

Before dispatching each agent, the governor checks:

```python
def can_dispatch(self, agent_type: AgentType) -> bool:
    """Return True if it is safe to dispatch another agent now."""
    # Concurrency gate
    if self.active_count >= self.config.max_concurrency:
        return False

    # Budget gate (conservative accounting)
    estimated_cost = COST_ESTIMATES[agent_type]
    if self.estimated_budget_remaining < estimated_cost:
        return False

    # Cooldown gate (post-429 backoff period)
    if self.in_backoff():
        return False

    return True
```

### 5.3 Adaptive Throttling

When the system observes warning signals (but not yet a hard limit), reduce concurrency:

| Signal | Governor Action |
|---|---|
| `anthropic-ratelimit-unified-status: allowed_warning` (if detectable) | Reduce max_concurrency by 1, wait for running agents to complete before new dispatches |
| Agent run completes in < 2 minutes (abnormally fast, possibly rate-limited mid-run) | Inspect result for rate limit text; if confirmed, enter backoff |
| 2+ consecutive agents return `is_error: true` with rate_limit text | Enter hard backoff; drain all active agents; do not dispatch new work |

### 5.4 Post-429 Backoff and Requeue

When a rate limit error is confirmed:

1. **Log the event** with timestamp, agent ID, issue number, and estimated usage at
   time of failure.
2. **Requeue the issue**: Remove the `in-progress` label from GitHub, reset to `open`.
   The issue must not remain stuck in-progress.
3. **Enter backoff**: Calculate the window reset time. If available from the `resets at`
   message in the error output, parse that timestamp. Otherwise, default to a 5-hour
   backoff from the time of the error.
4. **Drain running agents**: Allow currently-running agents to complete (do not kill
   them). Do not dispatch new agents during backoff.
5. **Alert the operator**: Emit a structured log event with type `rate_limit_exhausted`
   and include the estimated reset time.
6. **Resume after backoff**: When the backoff window expires, run a probe (cheap
   `claude -p "OK" --max-turns 1`) to verify the account is unblocked before resuming
   full dispatch.

**Exponential backoff formula for transient errors** (distinct from full window
exhaustion):

```
delay = min(max_delay, base_delay * (2 ** attempt)) + random(0, jitter)
```

Where:
- `base_delay = 30` seconds (for subscription rate limits, which reset in minutes, not
  seconds)
- `max_delay = 3600` seconds (1 hour)
- `jitter = base_delay * 0.25` (25% randomization to prevent thundering herd)
- Apply this formula for transient 429s (model overload, acceleration limits). For
  full window exhaustion, use the reset timestamp directly.

### 5.5 Issue Requeue Safety

The governor must guarantee that any issue that was dispatched but whose agent returned
`is_error: true` due to rate limiting is properly requeued. The safe pattern:

```python
async def handle_agent_result(self, result: AgentResult) -> None:
    if result.is_error:
        error_class = classify_error(result.error_text)
        if error_class == ErrorClass.RATE_LIMIT:
            await self.github.remove_label(result.issue_number, "in-progress")
            self.defer_queue.append(result.issue_number)
            await self.enter_backoff(reason="rate_limit", reset_hint=result.reset_time)
        elif error_class == ErrorClass.IMPLEMENTATION_FAILURE:
            # Different handler — do not remove in-progress; log for human review
            await self.alert_human(result)
        # ... other cases
```

---

## 6. `--max-budget-usd` Flag: Applicability to Pro/Max

The CLI flag is `--max-budget-usd` (not `--max-budget`). From the official CLI
reference:

> `--max-budget-usd`: Maximum dollar amount to spend on API calls before stopping
> (print mode only)

**This flag applies to API key billing only.** It measures USD spend against the
Anthropic API token-billing meter. For subscription accounts (Pro/Max) authenticated via
claude.ai login:

- There is no per-session token billing — usage is deducted from the subscription pool.
- The `--max-budget-usd` flag has no effect on subscription sessions; the `/cost` command
  documentation itself notes: "Claude Max and Pro subscribers have usage included in
  their subscription, so `/cost` data isn't relevant for billing purposes."
- The flag is relevant only when `ANTHROPIC_API_KEY` is set (API key mode).

**Conductor implication:** Do not rely on `--max-budget-usd` as a rate-limiting
mechanism for Pro/Max subscription sessions. It is appropriate to pass it for API key
sessions as a cost safety net (e.g., `--max-budget-usd 5.00` to cap runaway agents).

The `CONDUCTOR_MAX_BUDGET_USD` env var in the configuration schema (see
`docs/research/04-configuration.md`) should be documented as API-only and excluded from
subscription session invocations.

---

## 7. Usage Estimation: Cost per Agent Type

Without access to absolute quota counts, conductor must estimate usage in relative
terms. Based on observed averages (from Anthropic's own cost docs and community
benchmarks):

| Agent Type | Estimated Input Tokens | Estimated Output Tokens | Notes |
|---|---|---|---|
| Research agent | 20,000–50,000 | 5,000–15,000 | Wide context window; reads many files |
| Implementation agent | 30,000–80,000 | 8,000–25,000 | Includes full codebase context per turn |
| Simple probe / health check | 500–2,000 | 200–500 | Single-turn, minimal tools |

These are rough orders-of-magnitude estimates. Actual usage depends heavily on:
- Repository size (larger codebases = more tokens in CLAUDE.md and tool calls)
- Conversation length (token cost grows non-linearly with turn count)
- Model choice (Opus consumes the quota at a higher rate than Sonnet)
- Tool call frequency (each Bash/Read/Edit call adds tokens)

**Recommended accounting unit for conductor:** Track estimated usage in "Sonnet-equivalent
turn-equivalents" (STE) — an abstract unit that maps to relative quota consumption.
Calibrate STE values from empirical observations of actual runs once the system is
operational. Start conservatively: research agent = 5 STE, implementation agent = 10 STE,
probe = 0.1 STE.

**Agent teams multiplier:** Agent teams (multiple Claude instances spawned via the
`--agents` flag or `Task` tool) use approximately 7x more tokens than single-session
equivalents. Conductor's sub-agent model uses isolated worktrees via separate `claude -p`
subprocesses, not agent teams — so this multiplier does not apply to conductor's
architecture, but bears noting if the design ever moves to native agent teams.

---

## 8. Summary: Recommended Governor Configuration Defaults

| Setting | Pro | Max 5x | Max 20x | API key |
|---|---|---|---|---|
| `max_concurrency` | 1–2 | 2–3 | 3–5 | Per tier RPM |
| `primary_model` | Sonnet 4 | Sonnet 4 or Opus 4 | Opus 4 | Any |
| `max_budget_usd` | N/A | N/A | N/A | $5.00 per agent |
| Backoff on 429 | 5-hour window reset | 5-hour window reset | Alert + check weekly | `retry-after` header |
| Issue requeue on 429 | Yes — always | Yes — always | Yes — always | Yes — always |
| Weekly limit risk | Moderate (40–80 h) | High with Opus (35 h Opus/wk) | Low (240+ h Sonnet/wk) | None (token billing) |

---

## 9. Follow-Up Research Recommendations

### 9.1 Probe the OAuth Usage Endpoint (Unofficial)

The undocumented `/api/oauth/usage` endpoint that `Claude-Code-Usage-Monitor` uses may
provide programmatic access to remaining quota without consuming usage. Research:
- Is this endpoint stable enough to depend on?
- What does it return? Is it per-model or aggregate?
- What authentication is required (Bearer token from claude.ai session)?

If stable, conductor could poll this endpoint before each dispatch batch rather than
relying on conservative internal accounting.

**Suggested issue:** `Research: Reliability of /api/oauth/usage endpoint for pre-dispatch
quota introspection`

### 9.2 `anthropic-ratelimit-unified-*` Header Exposure Timeline

Multiple open issues (#19385, #25420, #27508, #29604) request that Claude Code expose
the `anthropic-ratelimit-unified-*` headers in the `statusLine` JSON hook payload. Track
whether this lands in a Claude Code release. If it does:
- Conductor could implement a lightweight poller that reads the hook payload after each
  completed agent run.
- This would provide near-real-time utilization feedback without relying on the OAuth
  endpoint.

**Suggested issue:** `Research: Track Claude Code statusLine rate-limit header exposure
(issues #19385/#29604)`

### 9.3 Weekly Limit vs. 5-Hour Window Interaction

The interaction between the weekly active-hours cap and the 5-hour rolling window is not
fully documented. Open questions:
- When does the weekly cap reset? Calendar week (Sunday midnight UTC)? Rolling 7-day
  window?
- Does hitting the weekly cap produce a different error message than hitting the 5-hour
  cap? Or the same `rate_limit_error`?
- Is there any indicator in the 429 response that distinguishes weekly vs. 5-hour
  exhaustion?

This distinction matters for backoff: weekly exhaustion requires a different recovery
strategy (wait days, not hours).

**Suggested issue:** `Research: Weekly vs. 5-hour rate limit signals — distinguishing
exhaustion types from 429 error payloads`

### 9.4 Empirical Token Usage Calibration

The usage estimates in section 7 are theoretical. Once conductor is running real agents,
collect empirical data:
- Parse `~/.claude/` JSONL session files after each agent run.
- Correlate raw token counts with the `anthropic-ratelimit-unified-*-utilization`
  percentage change observed between runs (if accessible).
- Build a calibrated STE mapping per agent type and model.

**Suggested issue:** `Research: Empirical token usage calibration for research and
implementation agents`

---

## 10. Cross-References

- **`docs/research/04-configuration.md`**: Defines `CONDUCTOR_MAX_BUDGET_USD` env var;
  section 6 above clarifies it is API-only and has no effect on subscription sessions.
  Section 3.2 defines `CONDUCTOR_TIMEOUT_SECONDS` — the timeout interacts with backoff
  design (an agent that times out is distinct from a rate-limited agent and should not be
  requeued as a rate-limit failure).
- **Issue #3 (Error Handling)**: The post-429 requeue logic in section 5.5 is a special
  case of the broader error handling and failure recovery design. The two documents
  should share a common error classification taxonomy.
- **Issue #5 (Logging and Observability)**: The usage ledger and `rate_limit_exhausted`
  alert event described in section 5.4 are logging/observability outputs. The JSONL log
  schema for those events should be defined in the logging research.
- **Issue #6 (Security Threat Model)**: The OAuth endpoint polling approach (section 9.1)
  involves storing or forwarding a claude.ai session token, which has security
  implications that the threat model document should address.

---

## Sources

- [Claude Max Plan: Pricing, Usage Limits, Features — IntuitionLabs](https://intuitionlabs.ai/articles/claude-max-plan-pricing-usage-limits) — Concrete 5-hour window message budgets by plan and model
- [Extra Usage for Paid Claude Plans — Claude Help Center](https://support.claude.com/en/articles/12429409-extra-usage-for-paid-claude-plans) — Overage and consumption-based billing for Pro/Max
- [Using Claude Code with Your Pro or Max Plan — Claude Help Center](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan) — Shared usage pool across Claude products, `/status` monitoring
- [Usage Limit Best Practices — Claude Help Center](https://support.claude.com/en/articles/9797557-usage-limit-best-practices) — Settings > Usage for consumption monitoring
- [Everything We Know About Claude Code Limits — Portkey.ai](https://portkey.ai/blog/claude-code-limits/) — Session model, weekly cap introduction
- [Claude Code Limits: Quotas & Rate Limits Guide — TrueFoundry](https://www.truefoundry.com/blog/claude-code-limits-explained) — Weekly active-hours definition, plan breakdown
- [Will New Weekly Rate Limits Hinder Your Coding Workflow? — APIdog](https://apidog.com/blog/weekly-rate-limits-claude-pro-max-guide/) — Weekly Sonnet/Opus hour budgets per plan; dual-limit structure
- [Claude Pro & Max Weekly Rate Limits Guide (2026) — Hypereal.tech](https://hypereal.tech/a/weekly-rate-limits-claude-pro-max-guide) — Per-model 5-hour window budgets; rolling window mechanics
- [Claude Code Limits — ClaudeLog](https://claudelog.com/claude-code-limits/) — Plan comparison table; reset cycles
- [Claude Code Usage Patterns and Limits by Plan — ClaudeLog](https://claudelog.com/faqs/claude-code-usage/) — Concurrency observations; Max 20x rarely exhausted
- [Errors — Claude API Docs](https://platform.claude.com/docs/en/api/errors) — 429 `rate_limit_error` JSON structure; request-id header
- [Rate Limits — Claude API Docs](https://platform.claude.com/docs/en/api/rate-limits) — `anthropic-ratelimit-*` response headers; `retry-after`; token bucket algorithm
- [CLI Reference — Claude Code Docs](https://code.claude.com/docs/en/cli-reference) — `--max-budget-usd` flag definition; `--output-format stream-json`; exit code semantics
- [Run Claude Code Programmatically — Claude Code Docs](https://code.claude.com/docs/en/headless) — `stream-json` event types; `is_error` result field; session_id
- [Manage Costs Effectively — Claude Code Docs](https://code.claude.com/docs/en/costs) — API cost averages ($6/dev/day); agent teams 7x multiplier; `/cost` vs `/stats`; `--max-budget-usd` API-only note
- [Agent SDK Overview — Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/overview) — SDK architecture; `system/init`, `result` message types
- [GitHub Issue #5621: StatusLine API Usage/Quota Information](https://github.com/anthropics/claude-code/issues/5621) — Session cost (`cost.total_cost_usd`) added; account quota not exposed; closed as not planned
- [GitHub Issue #29604: Expose Rate Limit Utilization in Status Line JSON](https://github.com/anthropics/claude-code/issues/29604) — `anthropic-ratelimit-unified-*` header list; proposed `rate_limit` statusLine object; open as duplicate
- [GitHub Issue #28999: Expose /usage Subscription Quota in statusLine JSON](https://github.com/anthropics/claude-code/issues/28999) — Usage endpoint exists internally; `.claude.json` corruption from concurrent access; open
- [GitHub Issue #29721: Per-Session Usage Contribution to 5h/7d Windows](https://github.com/anthropics/claude-code/issues/29721) — Absolute token limits not published; per-session contribution not available; open
- [GitHub Issue #22876: Rate Limit 429 Despite Available Quota](https://github.com/anthropics/claude-code/issues/22876) — Hidden rate limits; 429 at 72% utilization; inconsistency between API and web
- [GitHub Issue #19673: You've Hit Your Limit at 84% Usage](https://github.com/anthropics/claude-code/issues/19673) — Terminal error message format; 429 JSON structure; reset time in error text
- [GitHub Issue #27336: CLI Returns Rate Limit on Every Command](https://github.com/anthropics/claude-code/issues/27336) — `API Error: Rate limit reached` exit code 1; misleading for model-not-available errors
- [GitHub Issue #25531: API Error: Rate Limit Reached Incorrect in CLI](https://github.com/anthropics/claude-code/issues/25531) — Rate limit message as false positive for other error types
- [Sub-Agents Burn Out Tokens — DEV Community](https://dev.to/onlineeric/claude-code-sub-agents-burn-out-your-tokens-4cd8) — 5 parallel agents drain Pro in 15 min; Max 5x in ~75 min
- [Claude Code Usage Monitor — GitHub (Maciek-roboblog)](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) — Unofficial OAuth endpoint reverse engineering; real-time quota display
- [Exponential Backoff with Jitter — AWS Builders Library](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/) — Canonical backoff + jitter formula; thundering herd prevention
- [Anthropic Unveils New Rate Limits to Curb Claude Code Power Users — TechCrunch](https://techcrunch.com/2025/07/28/anthropic-unveils-new-rate-limits-to-curb-claude-code-power-users/) — Weekly cap policy context; top 5% targeting
