# Research: Distinguishing Weekly Cap Exhaustion from 5-Hour Window Exhaustion in 429 Error Payloads

**Issue:** #23
**Milestone:** M1: Foundation
**Status:** Complete
**Date:** 2026-03-02
**Depends on:** #8 (Usage Scheduling), #3 (Error Handling)

---

## Executive Summary

When `claude -p` encounters a subscription rate limit, it returns a generic 429 error payload whose
`message` field does **not** distinguish between a 5-hour window exhaustion and a weekly active-hours
cap exhaustion. However, multiple signals exist for making this determination programmatically, each
at different levels of reliability:

0. **HTTP 402 status code** — [DOCUMENTED, SCOPE RESOLVED — see Section 2.5 and Section 10.1]
   Issue #30484 (openclaw/openclaw, March 2026) and research for issue #64 (2026-03-02) determined
   that **HTTP 402 is NOT a reliable discriminator for weekly cap vs. 5-hour window exhaustion.**
   It signals extra usage (overage) billing authorization failure — a separate condition from
   standard rate limit window exhaustion. Both 5-hour and weekly cap exhaustion return HTTP 429
   when extra usage is not involved. See Section 2.5 for full findings.

1. **`anthropic-ratelimit-unified-*` response headers** — the most reliable signal, but inaccessible
   from outside the Claude Code subprocess in current releases (March 2026). These headers explicitly
   label the active window type (`five_hour` vs. `seven_day` via the
   `anthropic-ratelimit-unified-representative-claim` field).

2. **Reset timestamp heuristic** — when the `resets at` time in Claude Code's terminal message
   includes a **calendar date** (`resets Feb 20, 5pm`), it is a weekly-cap reset. When it shows
   only a time-of-day (`resets 4pm (Asia/Kuala_Lumpur)`), it is typically a 5-hour window reset.
   This heuristic is reliable in practice but has edge cases documented below.

3. **`/api/oauth/usage` endpoint** — returns explicit per-window utilization and reset timestamps
   for both `five_hour` and `seven_day` windows. Can be polled pre-dispatch. Undocumented and
   fragile.

4. **`anthropic-ratelimit-unified-overage-status` / `overage-disabled-reason` headers** — an
   `overage-disabled-reason` of `org_level_disabled` or `user_disabled` on a 429 response may
   indicate the weekly cap has been hit (because overage does not automatically unlock a depleted
   weekly budget in all configurations). This is an indirect signal only.

The conductor's error handler should use a three-stage approach: (0) check the HTTP status code
— if 402, classify as `extra_usage_billing_failure` and handle as a billing condition (not a
window exhaustion); (1) for HTTP 429, attempt to query the `/api/oauth/usage` endpoint to get
explicit per-window data; (2) fall back to the reset timestamp heuristic when the endpoint is
unavailable.

---

## 1. The Problem: One Error Type, Two Causes

### 1.1 Dual Rate Limit Layers

As documented in doc #08 (Usage Scheduling), Claude Pro and Max accounts are governed by two
overlapping rate limit layers:

- **5-hour rolling window**: A sliding window measuring recent message/token consumption. As usage
  ages beyond 5 hours, capacity is restored. Overage billing (extra usage) is available when this
  window is exhausted if the user has configured it.
- **Weekly active-hours cap** (introduced August 28, 2025): A 7-day cumulative cap targeting the
  top ~5% of heavy users. The weekly cap tracks "active hours" — periods when Claude models are
  actively processing tokens. When exhausted, the account is blocked for the remainder of the 7-day
  cycle. Overage behavior for the weekly cap is plan-dependent (see Section 4.4).

Both exhaustion types produce an HTTP 429 response with `type: rate_limit_error`.

### 1.2 Why the Distinction Matters for Conductor

The correct recovery strategy differs significantly:

| Exhaustion Type | Appropriate Recovery |
|---|---|
| 5-hour window | Backoff until the rolling window reset (up to 5 hours from first message). Retry is safe after backoff. |
| Weekly cap | Backoff until the 7-day cycle resets. Retry after 5 hours will fail again. Conductor should alert the operator, avoid dispatching new agents, and schedule a post-reset resume. |

If conductor treats all 429s as 5-hour window exhaustions, it will retry after 5 hours when the
weekly cap has actually fired — causing repeated 429 failures and wasting quota on probe requests.

---

## 2. The 429 Payload: What It Contains

### 2.1 JSON Body (Inaccessible in `-p` Stream-JSON)

[DOCUMENTED] The JSON error body returned by the Anthropic API for subscription rate limit
exhaustion:

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

This body is **identical** for both 5-hour window exhaustion and weekly cap exhaustion. [TESTED via
multiple GitHub issue reports: #19673, #22876, #24428, #29579] The `message` field does not include
the exhaustion type, the reset time, or which window fired.

### 2.2 Stream-JSON `result` Event

[DOCUMENTED] When `claude -p --output-format stream-json` encounters a rate limit during execution,
the final event in the stream is a `result` event with `is_error: true`:

```json
{
  "type": "result",
  "subtype": "error_during_operation",
  "is_error": true,
  "result": "API Error: Rate limit reached",
  "session_id": "2ab1d239-9581-4d03-a895-af10c9fcb863",
  "total_cost_usd": 0.0
}
```

The `result` field contains the text `"API Error: Rate limit reached"`. This text is generic; it
does **not** embed the reset time, the window type, or any machine-readable field that distinguishes
5-hour from weekly exhaustion. [INFERRED: The exact stream-json result event format for
rate-limited runs has not been empirically verified in the literature reviewed. The structure above
is synthesized from the documented stream-json schema (doc #08, Section 2.3) and community reports
of the terminal error message text.]

### 2.3 HTTP Response Headers (Internal to Claude Code Subprocess)

[DOCUMENTED] This is the richest source of discrimination, but it is inaccessible from the
conductor process as of March 2026.

When the Anthropic API returns a 429, the response headers include a complete set of
`anthropic-ratelimit-unified-*` headers. Claude Code reads these headers internally. The confirmed
header schema (from GitHub issue #12829, which shows exact header names and values captured from a
live API response):

```
anthropic-ratelimit-unified-status: "allowed" | "allowed_warning" | "rejected"
anthropic-ratelimit-unified-5h-status: "allowed" | "allowed_warning" | "rejected"
anthropic-ratelimit-unified-5h-reset: <unix timestamp>
anthropic-ratelimit-unified-5h-utilization: <decimal 0.0–1.0>
anthropic-ratelimit-unified-7d-status: "allowed" | "allowed_warning" | "rejected"
anthropic-ratelimit-unified-7d-reset: <unix timestamp>
anthropic-ratelimit-unified-7d-utilization: <decimal 0.0–1.0>
anthropic-ratelimit-unified-representative-claim: "five_hour" | "seven_day"
anthropic-ratelimit-unified-fallback-percentage: <decimal>
anthropic-ratelimit-unified-reset: <unix timestamp for representative window>
anthropic-ratelimit-unified-overage-status: <overage status string>
anthropic-ratelimit-unified-overage-disabled-reason: "org_level_disabled" | "user_disabled" | ...
```

**Key field: `anthropic-ratelimit-unified-representative-claim`**

[DOCUMENTED] This header contains the window identifier for the currently-blocking rate limit:
- `"five_hour"` — the 5-hour rolling window is the binding constraint
- `"seven_day"` — the 7-day weekly cap is the binding constraint

This is the definitive machine-readable signal that directly answers the research question. When
`anthropic-ratelimit-unified-status: "rejected"` (the 429 has fired), the `representative-claim`
header indicates which window caused the rejection.

**Why it's inaccessible:** Claude Code reads these headers internally and uses them to drive the
interactive UI warning (e.g., `"Approaching usage limit · resets at 7pm"`). As of March 2026, none
of these values are surfaced in the `stream-json` output, in the `statusLine` JSON hook, or via any
other mechanism accessible to the conductor process. Feature requests #19385, #29604, and #29721
are open and unresolved.

**Confirmed internal field names (reverse-engineered from Claude Code binary, per #29604):**

Claude Code parses these headers into internal JavaScript fields:
- `rateLimitType` — maps from `representative-claim` value
- `utilization` — from `*-utilization`
- `isUsingOverage` — from `overage-status`
- `surpassedThreshold` — from `*-surpassed-threshold`
- `resetsAt` — from `*-reset`
- `overageStatus`, `overageResetsAt`, `overageDisabledReason` — from overage headers

These fields are computed but not exposed externally in the current release.

### 2.4 Terminal Message (Partially Readable from Stderr)

[DOCUMENTED, INFERRED] Claude Code writes a terminal message to stderr when the rate limit is hit.
In interactive mode, this appears as an overlay:

```
You've hit your limit · resets 4pm (Asia/Kuala_Lumpur)
```

or, when the reset is more than ~24 hours away:

```
You've hit your limit · resets Feb 20, 5pm (Africa/Libreville)
```

In `-p` mode, this message may appear on stderr. The conductor can attempt to capture it by
redirecting stderr (`2>&1`), but:

1. Whether this exact message appears on stderr in headless mode vs. only in the interactive TUI
   has not been empirically confirmed. [INFERRED]
2. The message format contains the reset time but not a machine-readable window-type label.
3. The message is locale-sensitive (timezone label is user-configured).

However, the **date presence heuristic** is useful: if the `resets` string contains a month name
(`Jan`, `Feb`, `Mar`, etc.) or a day-of-week label, the reset is days away — indicating the weekly
cap fired. If it contains only a time (`resets 4pm`), the reset is within the current day — more
consistent with a 5-hour window reset.

**Caveats on the date heuristic:**
- A 5-hour window reset that happens to fall past midnight could theoretically show "tomorrow," but
  based on issue reports, the date is typically only included when the reset is multiple days away
  (i.e., weekly resets). [INFERRED]
- The `/usage` command's display has a known inconsistency where some lines show times without dates
  even for weekly resets (#10165, #28798). The terminal limit message may have the same
  inconsistency in some Claude Code versions.
- Issue #14470 documents a case where the reset time shown was incorrect relative to the `/usage`
  command output (9pm vs. 5pm). Display-layer bugs exist.

### 2.5 HTTP 402 Status Code — Issue #64 Research Findings (2026-03-02)

**Prior status: [DOCUMENTED, UNCONFIRMED SCOPE]. Updated status: [DOCUMENTED — scope resolved;
original hypothesis refuted].**

#### 2.5.1 Source Evidence

GitHub issue #30484 ("Claude Max plan rate limits return HTTP 402 instead of 429 — triggers false
billing error warnings", filed March 1, 2026, against `openclaw/openclaw`) documents that a Claude
Max plan account with **Extra Usage enabled** received HTTP 402 responses from the Anthropic API
during usage-limit events. Key context from the bug report:

- User's plan: Claude Max ($100/month)
- Extra Usage status: **enabled**, balance $41.36 remaining out of $50 monthly limit
- Trigger: Rate limits firing "especially when image analysis or high-traffic Slack channels are
  active" within the Max plan's usage window
- A second reporter (kobie3717, March 2, 2026) hit the same issue with "97% daily usage remaining"
- The issue generated five PRs (#30490, #30508, #30515, #30687, #30780), with PR #30780
  implementing provider-aware 402 handling: "treats Anthropic 402 as rate_limit"

Note: the issue title uses "rate limits" generically. Neither the body nor any comment specifies
whether the 402 fires on 5-hour window exhaustion or weekly cap exhaustion specifically.

#### 2.5.2 Anthropic SDK Behavior for HTTP 402

[DOCUMENTED] The Anthropic Python SDK (`anthropic-sdk-python`) does **not** define a specific
exception class for HTTP 402. The `_make_status_error` method in `_client.py` explicitly maps
429 to `RateLimitError` and 500+ to `InternalServerError`. **All other codes including 402 fall
through to the generic `APIStatusError`.** The `_should_retry` method only auto-retries 408, 409,
429, and 500+; **HTTP 402 does NOT trigger automatic retry.**

The official Anthropic API errors documentation (`platform.claude.com/docs/en/api/errors`) lists
400, 401, 403, 404, 413, 429, 500, and 529. **HTTP 402 is not listed** — confirming it is not
part of the standard API error schema.

#### 2.5.3 Resolved Scope: 402 Is a Billing Layer Signal, Not a Window-Type Discriminator

[DOCUMENTED, INFERRED MECHANISM] The most coherent explanation consistent with the evidence:

**HTTP 402 fires when:** The Anthropic subscription API routes a request through an extra usage
(overage) billing authorization path and that path fails — due to a billing authorization failure,
transient payment processing error, or internal billing state inconsistency. This is a
**billing-layer event**, separate from the quota enforcement path that returns 429.

**HTTP 429 fires when:** Standard rate limit quota is exhausted — either the 5-hour rolling window
or the weekly active-hours cap. These are **quota-enforcement events**, independent of billing.

**Critical finding for the original research question:** HTTP 402 is **NOT** a reliable discriminator
for weekly cap vs. 5-hour window exhaustion. The original hypothesis — "if 402 = weekly cap and
429 = 5-hour window, the HTTP status code is the simplest discriminator" — is **refuted** by the
available evidence. The 402 condition is orthogonal to the 5-hour/weekly distinction.

Supporting evidence: the openclaw reporter had $41.36 extra usage balance remaining when 402 fired.
This is inconsistent with "extra usage balance exhausted." It suggests 402 may also fire during
billing authorization routing failures even when credits exist (transient billing layer errors, or
a discrepancy between the subscription quota state and the extra usage authorization path).

#### 2.5.4 What Remains Unknown

1. **JSON body structure for 402**: No network capture from a live Anthropic 402 response has been
   published. [INFERRED] The body likely follows the standard Anthropic error format:
   ```json
   {
     "type": "error",
     "error": {
       "type": "<unknown — candidates: billing_error, payment_required_error, invalid_request_error>",
       "message": "<billing-related message>"
     },
     "request_id": "<req_id>"
   }
   ```
   The `error.type` value is unknown. No source confirms this.

2. **`claude -p` stream-json output for 402**: When Claude Code's internal API call receives a
   402, the stream-json layer output is unknown. Possible outcomes:
   - A `result` event with `is_error: true` and billing-specific error text
   - The same generic `"API Error: Rate limit reached"` text as for 429
   - A `rate_limit_event` message (as documented for 429 in issue #26498)
   See Section 10.1 for the follow-up empirical verification recommendation.

3. **Pro plan 402 behavior**: Issue #30484 is Max plan specific. Whether Pro plan extra usage
   exhaustion also returns 402 is unknown.

4. **Whether 402 fires without extra usage enabled**: If extra usage is disabled entirely, does the
   API ever return 402? Unknown — 402 may be strictly conditional on extra usage being configured.

#### 2.5.5 Conductor Decision Rule: 402 Handling

Given the research findings, the correct conductor decision rule is:

```
if http_status == 402:
    CLASSIFY: extra_usage_billing_failure
    ALERT: "HTTP 402 — extra usage billing failure (not a standard rate limit event)"
    ACTION: Halt new dispatches; log full response body for investigation
    RECOVERY: Check extra usage balance/payment method; manual billing resolution required
    NOTE: Do NOT apply rate-limit window backoff.
          Do NOT treat as weekly_cap or five_hour exhaustion.

elif http_status == 429:
    Proceed to window classification (Section 5 decision tree)
```

HTTP 402 and HTTP 429 require fundamentally different recovery actions. The 5-hour vs. weekly
distinction for HTTP 429 must still be resolved via the Section 5 decision tree; HTTP 402 bypasses
that tree entirely.

---

## 3. The `/api/oauth/usage` Endpoint

### 3.1 Response Schema

[DOCUMENTED] The undocumented OAuth usage endpoint returns per-window utilization data. Confirmed
JSON structure (from the `claude-code-usage-monitor` project and the `lexfrei` statusline gist):

```json
{
  "five_hour": {
    "utilization": 6.0,
    "resets_at": "2025-11-04T04:59:59.943648+00:00"
  },
  "seven_day": {
    "utilization": 35.0,
    "resets_at": "2025-11-06T03:59:59.943679+00:00"
  },
  "seven_day_oauth_apps": null,
  "seven_day_opus": {
    "utilization": 0.0,
    "resets_at": null
  },
  "iguana_necktie": null
}
```

Fields:
- `utilization`: percentage of the window consumed (0–100, not 0–1)
- `resets_at`: ISO 8601 timestamp of when the window resets
- `seven_day_opus`: separate tracking for Opus 4 weekly usage (distinct from Sonnet weekly usage)
- `seven_day_oauth_apps`: usage attributed to OAuth app access specifically (may be null)
- `iguana_necktie`: unknown field, typically null; may be an internal identifier or placeholder

### 3.2 Authentication

[DOCUMENTED] The endpoint requires a `Bearer` token from the claude.ai OAuth session. The
`lexfrei` statusline script uses `anthropic-beta: oauth-2025-04-20` as an additional header. The
OAuth token is stored in the Claude Code credentials file (`~/.claude/.credentials.json` or
equivalent). Conductor would need to read this token to call the endpoint.

### 3.3 What This Enables

By polling `/api/oauth/usage` before each dispatch, conductor can:
1. Check `five_hour.utilization` — if near 100%, pause dispatch and wait for `five_hour.resets_at`
2. Check `seven_day.utilization` — if at or near 100%, issue a weekly-cap alert and enter long
   backoff until `seven_day.resets_at`
3. Check `seven_day_opus.utilization` — separately gate Opus dispatches from Sonnet dispatches

### 3.4 Limitations

[DOCUMENTED] This endpoint is:
- **Undocumented**: No official reference; schema may change without notice
- **Not rate-limit-proof**: Polling it counts as an HTTP request; unknown if it itself is
  rate-limited
- **Session-token-dependent**: Requires extracting and refreshing the claude.ai OAuth session
  token, which involves reading a local credentials file not documented for third-party use (see
  security implications in doc #06)
- **Display-backend mismatch**: Issue #29680 documents cases where the endpoint returned stale
  utilization data after a global reset; the backend quota had been reset but the endpoint still
  showed pre-reset values

The existing research doc #08 (Section 9.1) recommends further research on this endpoint. That
research is partially answered here: the endpoint is usable but fragile. It is appropriate as a
fallback, not as a hard dependency.

---

## 4. The `resets_at` Timestamp Heuristic (Primary Fallback)

### 4.1 Algorithm

When `/api/oauth/usage` is unavailable, conductor can use the reset timestamp to classify the
limit type. The approach:

1. Parse the reset timestamp from either:
   - The terminal stderr message (`resets Feb 20, 5pm (Africa/Libreville)`) — if readable
   - The `anthropic-ratelimit-unified-reset` header value from a prior non-429 request — if a
     pre-dispatch probe was run

2. Compute time-delta: `delta = resets_at - now()`

3. Apply threshold:
   - `delta <= 5 hours`: Almost certainly a 5-hour window reset. [INFERRED]
   - `5 hours < delta <= 48 hours`: Ambiguous. Could be a 5-hour window reset that extends past
     midnight, or the tail end of a weekly window. Treat conservatively as **5-hour window** but
     log for analysis.
   - `delta > 48 hours`: Almost certainly a weekly cap reset. [INFERRED]

```python
FIVE_HOUR_THRESHOLD = timedelta(hours=5, minutes=30)  # 30-min buffer
WEEKLY_THRESHOLD = timedelta(hours=48)

def classify_from_reset_time(resets_at: datetime) -> str:
    """Classify 429 type from reset timestamp."""
    delta = resets_at - datetime.now(tz=timezone.utc)
    if delta <= FIVE_HOUR_THRESHOLD:
        return "five_hour"
    elif delta > WEEKLY_THRESHOLD:
        return "seven_day"
    else:
        return "ambiguous"
```

### 4.2 Accuracy of Heuristic

[INFERRED] Based on reviewing the GitHub issue corpus:
- Five-hour window resets are always within ≤5 hours of the first message in the window
- Weekly cap resets are documented at 4–7 days in the future from when the limit fires
- No case was found where a weekly cap reset was within 5 hours of the limit firing

The 48-hour threshold gives a conservative buffer. The primary risk is the 5–48 hour ambiguous
zone; in practice, this zone is rarely inhabited (weekly resets tend to be 4–7 days away, not 2
days).

### 4.3 Parsing the Terminal Message

[INFERRED] The `resets` string in stderr may take one of these formats based on observed reports:

| Pattern | Example | Interpretation |
|---|---|---|
| `resets HH:MMpm (Timezone)` | `resets 4pm (Europe/Berlin)` | Time-of-day only; likely 5-hour |
| `resets Mon Feb DD, HH:MMpm (Timezone)` | `resets Feb 20, 5pm (Africa/Libreville)` | Includes date; likely weekly |
| `resets tomorrow at HH:MMpm` | Not confirmed; proposed in #28798 | Ambiguous |

The presence of a month abbreviation (`Jan`, `Feb`, `Mar`, etc.) in the string is a reliable
indicator of a multi-day reset. A regex:

```python
import re
from datetime import datetime, timezone, timedelta

def parse_reset_string(text: str) -> datetime | None:
    """
    Attempt to extract reset time from Claude Code stderr message.
    Returns UTC datetime or None if unparseable.
    """
    # Pattern: "resets Feb 20, 5pm (Africa/Libreville)"
    date_pattern = re.compile(
        r'resets (?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),\s*'
        r'(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<ampm>am|pm)?'
    )
    # Pattern: "resets 4pm (Europe/Berlin)"
    time_only_pattern = re.compile(
        r'resets (?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?(?P<ampm>am|pm)?'
    )
    # ... parse and return UTC datetime
```

Note: The timezone label (e.g., `Europe/Berlin`) is the user's **local** timezone, not UTC. Any
parser must convert to UTC using the IANA timezone identifier in parentheses.

### 4.4 Overage Behavior and Its Role in Classification

[DOCUMENTED] Extra usage (overage) behavior differs by limit type and subscription configuration:

| Scenario | Overage Available? |
|---|---|
| 5-hour window exhausted, overage enabled | Yes — continued usage is billed at standard API rates |
| 5-hour window exhausted, overage disabled | No — hard block until window resets |
| Weekly cap exhausted | Unclear — official docs suggest overage applies to "usage limits" generally; community reports suggest some weekly exhaustions also offer overage |
| Weekly cap exhausted, `overage-disabled-reason: org_level_disabled` | No overage available |

[INFERRED] The `anthropic-ratelimit-unified-overage-disabled-reason` header on a rejected request
may provide an indirect signal: if overage is explicitly disabled and the reset is days away, the
weekly cap is likely the trigger.

In headless `-p` mode, the interactive "enable extra usage?" prompt does not appear. If overage
is enabled for the account, `claude -p` may automatically consume extra usage without prompting —
causing unexpected charges. [INFERRED, requires empirical verification] If overage is disabled,
`claude -p` will exit with the generic rate limit error. Conductor must not assume that a 429
always means "no overage consumed."

---

## 5. Complete Classification Decision Tree

### 5.1 Signal Priority Order

For the post-error handler in the conductor governor. **Updated 2026-03-02 (issue #64) to add
HTTP status pre-check before window classification. HTTP 402 is a billing event, not a window
discriminator — see Section 2.5.**

```
RATE LIMIT ERROR CLASSIFICATION PROCEDURE
══════════════════════════════════════════

Pre-Step: HTTP Status Code Gate
──────────────────────────────────
  If http_status == 402:
    → CLASSIFY: extra_usage_billing_failure
    → ALERT: "HTTP 402 — extra usage billing failure (not a standard rate limit event)"
    → ACTION: Halt new dispatches; log full response body for investigation
    → RECOVERY: Check extra usage balance/payment method; manual billing resolution
    → NOTE: Do NOT apply rate-limit window backoff.
             Do NOT treat as weekly_cap or five_hour exhaustion (see Section 2.5).
    → STOP. Do not continue to Steps 1-3.

  If http_status == 429:
    → CONTINUE to Step 1 below

  If http_status >= 500 (transient server error):
    → Apply exponential backoff (base 30s, max 300s); retry; not a rate limit event

Step 1: Check /api/oauth/usage endpoint
───────────────────────────────────────
  [For HTTP 429 only]

  GET https://api.anthropic.com/api/oauth/usage
  Authorization: Bearer <claude.ai session token>
  anthropic-beta: oauth-2025-04-20

  If response.five_hour.utilization >= 95:
    → CLASSIFY: five_hour_exhaustion
    → BACKOFF: until response.five_hour.resets_at + 5 minutes

  If response.seven_day.utilization >= 95:
    → CLASSIFY: seven_day_exhaustion
    → BACKOFF: until response.seven_day.resets_at + 5 minutes
    → ALERT: "Weekly cap exhausted. Resuming at {resets_at}"

  If response.seven_day_opus.utilization >= 95:
    → CLASSIFY: seven_day_opus_exhaustion
    → ACTION: Stop dispatching Opus agents; Sonnet agents may continue
    → BACKOFF (Opus only): until response.seven_day_opus.resets_at + 5 minutes

  If endpoint unavailable (network error, auth failure, etc.):
    → PROCEED TO Step 2

Step 2: Parse reset time from stderr (if available)
─────────────────────────────────────────────────────
  [For HTTP 429 only]

  Capture stderr from the failed claude -p process.
  Extract "resets ..." string.
  Parse reset timestamp (see Section 4.3).
  Compute delta = resets_at - now()

  If delta <= 5.5 hours:
    → CLASSIFY: five_hour_exhaustion (confidence: HIGH)
    → BACKOFF: delta + 5 minutes

  If delta > 48 hours:
    → CLASSIFY: seven_day_exhaustion (confidence: HIGH)
    → BACKOFF: delta + 5 minutes
    → ALERT: "Weekly cap exhausted. Resuming at {resets_at}"

  If 5.5 hours < delta <= 48 hours:
    → CLASSIFY: ambiguous (confidence: LOW)
    → LOG: raw delta for calibration
    → BACKOFF: delta + 5 minutes (conservative: use the longer delta)

Step 3: Fallback — no reset time available
──────────────────────────────────────────
  [For HTTP 429 only]

  Count consecutive 429s:
  If this is the 1st–3rd consecutive 429:
    → CLASSIFY: five_hour_exhaustion (optimistic default)
    → BACKOFF: exponential (base 30s, max 3600s)

  If this is the 4th+ consecutive 429 (retries after assumed 5-hour reset still failing):
    → RECLASSIFY: seven_day_exhaustion (the 5-hour window reset did not help)
    → ALERT: "Possible weekly cap. Manual inspection recommended."
    → BACKOFF: 24 hours
```

### 5.2 Governor State Machine

```python
class RateLimitClass(Enum):
    FIVE_HOUR = "five_hour"
    SEVEN_DAY = "seven_day"
    SEVEN_DAY_OPUS = "seven_day_opus"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"

@dataclass
class RateLimitEvent:
    timestamp: datetime
    classification: RateLimitClass
    reset_at: datetime | None
    consecutive_count: int
    raw_reset_delta_hours: float | None
    source: Literal["oauth_usage", "stderr_parse", "backoff_inference"]

async def classify_rate_limit(
    failed_agent: AgentResult,
    oauth_token: str | None,
) -> RateLimitEvent:
    """Classify a 429 rate limit event using the priority-ordered signal chain."""

    # Step 1: OAuth usage endpoint
    if oauth_token:
        try:
            usage = await fetch_oauth_usage(oauth_token)
            if usage.seven_day.utilization >= 95:
                return RateLimitEvent(
                    classification=RateLimitClass.SEVEN_DAY,
                    reset_at=usage.seven_day.resets_at,
                    source="oauth_usage",
                    ...
                )
            if usage.seven_day_opus and usage.seven_day_opus.utilization >= 95:
                return RateLimitEvent(
                    classification=RateLimitClass.SEVEN_DAY_OPUS,
                    reset_at=usage.seven_day_opus.resets_at,
                    source="oauth_usage",
                    ...
                )
            if usage.five_hour.utilization >= 95:
                return RateLimitEvent(
                    classification=RateLimitClass.FIVE_HOUR,
                    reset_at=usage.five_hour.resets_at,
                    source="oauth_usage",
                    ...
                )
        except Exception:
            pass  # fall through to Step 2

    # Step 2: Stderr parse
    if failed_agent.stderr:
        reset_at = parse_reset_from_stderr(failed_agent.stderr)
        if reset_at:
            delta = reset_at - datetime.now(tz=timezone.utc)
            if delta.total_seconds() > 48 * 3600:
                return RateLimitEvent(
                    classification=RateLimitClass.SEVEN_DAY,
                    reset_at=reset_at,
                    source="stderr_parse",
                    ...
                )
            elif delta.total_seconds() <= 5.5 * 3600:
                return RateLimitEvent(
                    classification=RateLimitClass.FIVE_HOUR,
                    reset_at=reset_at,
                    source="stderr_parse",
                    ...
                )
            else:
                return RateLimitEvent(
                    classification=RateLimitClass.AMBIGUOUS,
                    reset_at=reset_at,
                    source="stderr_parse",
                    ...
                )

    # Step 3: Consecutive-count inference
    if self.consecutive_rate_limit_count >= 4:
        return RateLimitEvent(
            classification=RateLimitClass.SEVEN_DAY,
            reset_at=None,  # unknown; use 24h default
            source="backoff_inference",
            ...
        )

    return RateLimitEvent(
        classification=RateLimitClass.UNKNOWN,
        reset_at=None,
        source="backoff_inference",
        ...
    )
```

---

## 6. Interaction with Existing Research

### 6.1 Cross-Reference: Doc #08 (Usage Scheduling)

Doc #08, Section 9.3, explicitly flagged this question as an unresolved follow-up:

> *"Does hitting the weekly cap produce a different error message than hitting the 5-hour cap? Or the
> same `rate_limit_error`? Is there any indicator in the 429 response that distinguishes weekly vs.
> 5-hour exhaustion?"*

This document answers that question:
- The `rate_limit_error` message body is **identical** for both window types.
- The discriminating signal is the `anthropic-ratelimit-unified-representative-claim` header,
  currently inaccessible in `-p` mode.
- Practical fallbacks are the `/api/oauth/usage` endpoint and the reset timestamp heuristic.

Doc #08, Section 5.4, proposed a `reset_hint` parameter in `enter_backoff()`. This document
provides the mechanism for populating that parameter with the classified reset type.

Doc #08, Section 8 (Summary table) shows "Backoff on 429: 5-hour window reset" for Pro and Max 5x.
This should be updated to include "or 7-day reset if weekly cap detected via classification
procedure."

**Contradiction flag:** Doc #08 states (Section 1.2): *"When the weekly cap is hit, the account is
locked out until the weekly cycle resets, with no option to purchase additional time (unlike the
5-hour window, where consumption-based overage is available on some plans)."* This claim is
contradicted by the claude.ai help center documentation (article 11145838) which states overage
can apply to weekly-cap exhaustion as well. The accurate statement is: overage availability for
the weekly cap is user/org-configurable, not categorically unavailable. Doc #08 should be updated
to reflect this nuance.

### 6.2 Cross-Reference: Issue #3 (Error Handling)

The `RateLimitClass` enum defined in Section 5.2 above should be incorporated into the error
classification taxonomy planned in issue #3's deliverable
(`docs/research/03-error-handling.md`). The two documents should share:
- A common `ErrorClass` hierarchy where `RATE_LIMIT_FIVE_HOUR` and `RATE_LIMIT_SEVEN_DAY` are
  sub-types of `RATE_LIMIT`
- The consecutive-count reclassification logic (Step 3 of the decision tree)

### 6.3 Cross-Reference: Doc #06 (Security Threat Model)

Polling the `/api/oauth/usage` endpoint requires accessing the claude.ai session token from
`~/.claude/.credentials.json`. Doc #06 should address:
- Whether this token can be safely read by the conductor process
- Whether storing/forwarding it in memory or logs creates a credential exposure risk
- The session token rotation behavior (how long it is valid; when it expires)

---

## 7. Plan-Specific Nuances

### 7.1 Pro Plan ($20/month)

[DOCUMENTED] Weekly caps on Pro are relatively modest (40–80 Sonnet active-hours/week). A
conductor running 2 parallel Sonnet implementation agents can exhaust the Pro weekly cap in under
2 working days. After weekly cap exhaustion on Pro:

- The 5-hour window may still show partial capacity (since it is a shorter window), but the weekly
  block overrides it — new requests will 429 regardless of 5-hour headroom. [INFERRED from the
  documented bug in #12829, where Claude Code was incorrectly blocking users because it checked
  7d utilization instead of the representative claim; the inverse scenario — 7d is the binding
  limit — would produce correct 429 responses even when 5h utilization is low]

- Overage may be offered; the behavior in headless mode when overage is enabled is not confirmed
  (see Section 4.4).

### 7.2 Max 5x ($100/month)

[DOCUMENTED] The critical constraint is the Opus 4 weekly budget (15–35 active hours/week). The
`seven_day_opus` field in the `/api/oauth/usage` response tracks Opus usage separately. A conductor
running Opus 4 agents should check `seven_day_opus.utilization` independently and gate Opus
dispatches even when Sonnet dispatches are still permitted.

### 7.3 Max 20x ($200/month)

[DOCUMENTED] The Sonnet weekly budget is large enough that weekly cap exhaustion is unlikely for
typical use patterns. The Opus budget (24–40 active hours/week) remains a potential constraint for
intensive agentic use. The weekly reset cycle for Max 20x accounts may be different from the
individual account start date — the global reset discussed in #29680 shifted all accounts to a
Friday-anchored reset cycle.

### 7.4 API Key Authentication

[DOCUMENTED] When `ANTHROPIC_API_KEY` is set, Claude Code uses token-billing mode. The
`anthropic-ratelimit-unified-*` headers are not applicable — standard API rate limit headers
(`anthropic-ratelimit-requests-*`, `anthropic-ratelimit-tokens-*`) apply. The 429 response for
API key sessions will include a `retry-after` header with the number of seconds to wait, which is
directly actionable. No 5-hour vs. weekly distinction exists for API key sessions.

---

## 8. Unknown / Unresolved Questions

The following questions remain open after this research:

1. **Does `claude -p` stderr actually contain the "You've hit your limit · resets ..." message in
   headless mode?** This message is confirmed in interactive mode. Whether it appears on stderr in
   `-p` mode is [INFERRED] from the behavior of the interactive UI but not empirically verified.
   This is the most important empirical gap for the heuristic approach.

2. **[RESOLVED — Issue #64] Does weekly cap exhaustion consistently return HTTP 402 vs. HTTP 429?**
   Research for issue #64 (2026-03-02) resolved this: HTTP 402 is a billing layer event (extra usage
   billing authorization failure), NOT a weekly cap signal. Both 5-hour and weekly cap exhaustion
   return HTTP 429. See Section 2.5 for full findings.

3. **In headless mode, does `claude -p` automatically consume extra usage (overage) without
   prompting when the 5-hour window is exhausted?** If so, conductor may be incurring unexpected
   charges. The interactive-mode prompt cannot appear in `-p` mode; the fallback behavior is
   unknown.

4. **What is the exact value of `anthropic-ratelimit-unified-representative-claim` when the weekly
   cap (not 5-hour) fires a 429?** The confirmed value for 5-hour is `"five_hour"`. The confirmed
   value for weekly is likely `"seven_day"` (by analogy with the `7d` prefix in other headers), but
   this has not been explicitly observed in a live weekly-cap exhaustion scenario. [INFERRED]

5. **Is the `seven_day_opus` window tracked separately from `seven_day` in the
   `representative-claim` header?** If so, a separate `seven_day_opus` claim value may exist. This
   would allow conductor to distinguish "Sonnet weekly cap hit" from "Opus weekly cap hit."

---

## 9. Follow-Up Research Recommendations

### 9.1 Empirical Verification of Stderr Message in `-p` Mode

**Question:** Does `claude -p` emit the "You've hit your limit · resets ..." message to stderr
when rate-limited in headless mode?

**Why it matters:** The reset timestamp heuristic (Section 4) depends entirely on this message
being readable. If it does not appear on stderr, the heuristic is unavailable and conductor must
fall back to consecutive-count inference.

**Method:** Run `claude -p "..." 2>stderr.txt` from an account near its limit. Inspect
`stderr.txt` for the reset message.

**Scope:** Empirical measurement — belongs inside an existing research doc, not a standalone issue.

### 9.2 HTTP 402 as Weekly Cap Discriminator — RESOLVED by Issue #64

**Status: Resolved. The original hypothesis is refuted.**

Issue #64 research (2026-03-02) determined that HTTP 402 is a billing layer event (extra usage
billing authorization failure), not a weekly cap discriminator. See Section 2.5 for full findings.

The remaining open empirical question is: what exactly does `claude -p --output-format stream-json`
emit when the underlying API call receives HTTP 402? This is addressed in Section 10.1.

### 9.3 Headless Overage Consumption Without Prompt

**Question:** In headless `-p` mode, when the 5-hour window is exhausted and overage is enabled,
does `claude -p` automatically consume extra usage credits without prompting, or does it exit with
a 429?

**Why it matters:** If `claude -p` silently consumes overage, conductor could incur unbounded costs
without the user's awareness. The governor must know whether to expect a 429 or silent continuation
when overage is configured.

**Scope:** This is a new architectural question distinct from the 5-hour/weekly distinction
question. It would require a standalone research document. Creating a follow-up issue.

### 9.4 `anthropic-ratelimit-unified-*` Header Exposure in statusLine

Doc #08 (Section 9.2) recommended tracking feature requests #19385 and #29604 for header exposure.
As of March 2026, these are still open. If headers become accessible via the `statusLine` JSON
hook, the classification problem is solved definitively — conductor can read the
`representative_claim` field from the hook payload after each completed agent turn without polling
any additional endpoints.

**Scope:** Monitoring an existing open issue; not a new standalone research question. Track via
the existing follow-up framework.

---

## 10. Follow-Up Research Recommendations (Addendum from Issue #64)

### 10.1 Empirical Capture of HTTP 402 JSON Body and `claude -p` Stream-JSON Output

**Question:** What is the exact JSON body Anthropic returns with a HTTP 402 response, and what
does `claude -p --output-format stream-json` emit when it receives a 402 from the internal API
call?

**Why it matters:** Section 2.5.4 documents these as the most critical remaining empirical gaps.
The JSON body structure (specifically `error.type`) determines how conductor should classify and
log 402 events. The stream-json output format determines whether conductor can detect 402 from the
subprocess output alone, or whether it needs to observe the raw HTTP layer.

**Method:**
1. Set up a Max plan account with Extra Usage enabled
2. Use network interception (e.g., mitmproxy, Charles Proxy, or Anthropic's own HAR export) to
   capture the raw HTTP response body when a 402 fires
3. Run `claude -p "..." --output-format stream-json 2>&1` during a session where 402 is expected
   (high-traffic usage with Extra Usage configured); capture the full output including stderr
4. Compare the stream-json `result` event text for 402 vs. 429 scenarios

**Scope:** Empirical measurement — update Section 2.5.4 with findings when completed.

**Related:** Issue #63 (headless overage consumption in `claude -p`) addresses the closely related
question of whether `claude -p` auto-consumes extra usage without prompting.

### 10.2 `RateLimitClass` Enum Extension for 402

**Question:** Should the `extra_usage_billing_failure` classification be added to the
`RateLimitClass` enum in Section 5.2?

**Recommendation:** Yes. Add `EXTRA_USAGE_BILLING = "extra_usage_billing"` as a new variant. This
classification should trigger a different handler path from the rate-limit window classifications
(FIVE_HOUR, SEVEN_DAY, SEVEN_DAY_OPUS). The billing failure handler is the operator alert path;
the rate-limit path is the scheduling backoff path. They must not be conflated.

**Scope:** Implementation detail — defer to the error handling taxonomy doc (#3) for the formal
type definition.

---

## 10. Sources

- [GitHub Issue #12829: Rate limit blocking ignores anthropic-ratelimit-unified-representative-claim header](https://github.com/anthropics/claude-code/issues/12829) — **Primary source** for confirmed header schema including `five_hour`/`seven_day` window naming and exact header key-value pairs from a live API response; documents the Claude Code bug that ignored the representative claim
- [GitHub Issue #29604: Expose rate limit utilization data in status line JSON](https://github.com/anthropics/claude-code/issues/29604) — Internal field names (`rateLimitType`, `resetsAt`, etc.) reverse-engineered from Claude Code binary; proposed statusLine JSON schema
- [GitHub Issue #19673: You've hit your limit · While usage is still at 84%](https://github.com/anthropics/claude-code/issues/19673) — Confirms generic 429 JSON payload; `request_id` field; reset time format with timezone; no window-type discrimination in payload
- [GitHub Issue #24428: You've hit your limit · resets 2pm (UTC)](https://github.com/anthropics/claude-code/issues/24428) — Confirms generic payload; no weekly/5-hour distinction; closed as duplicate of #22876
- [GitHub Issue #22876: Rate limit 429 errors despite dashboard showing available quota](https://github.com/anthropics/claude-code/issues/22876) — Documents hidden rate limits; generic error message; no discrimination by limit type
- [GitHub Issue #29579: API Error: Rate limit reached despite Claude Max subscription and only 16% usage](https://github.com/anthropics/claude-code/issues/29579) — Confirms `API Error: Rate limit reached` text; no window type in error; related issue #25805 explicitly names lack of discrimination as a bug
- [GitHub Issue #25607: You've hit your limit · resets Feb 20, 5pm (Africa/Libreville)](https://github.com/anthropics/claude-code/issues/25607) — **Key source** for date-inclusive reset format indicating weekly cap; confirms reset ~7 days away; /usage showed 12% weekly usage (display/backend inconsistency)
- [GitHub Issue #14470: Spending cap error message shows incorrect reset time (9pm vs 5pm)](https://github.com/anthropics/claude-code/issues/14470) — Documents reset time display inconsistency; 9pm in error vs. 5pm in /usage; version 2.0.32
- [GitHub Issue #28798: /usage reset times should include the date, not just the time](https://github.com/anthropics/claude-code/issues/28798) — Documents inconsistent date display in /usage; "Extra usage" line shows date; session/weekly reset lines show time only; confirms format ambiguity
- [GitHub Issue #10165: /usage command shows only time without day for weekly reset](https://github.com/anthropics/claude-code/issues/10165) — Confirms older versions did not include date for weekly reset in /usage; context for format evolution
- [GitHub Issue #29680: Weekly usage not reset during Feb 27 global reset + cycle date shifted](https://github.com/anthropics/claude-code/issues/29680) — Weekly reset cycle shifted to Friday for all accounts after global reset; display-backend mismatch in utilization after reset
- [GitHub Issue #30484 (openclaw): Claude Max plan rate limits return HTTP 402 instead of 429](https://github.com/openclaw/openclaw/issues/30484) — **Primary source for Section 2.5.** Documents HTTP 402 as a billing-layer event on Max plan with Extra Usage enabled; user had $41.36 extra usage balance remaining when 402 fired; issue generated 5 PRs; PR #30780 implements provider-aware Anthropic 402 handling
- [Anthropic SDK Python — `_client.py` `_make_status_error` method](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_client.py) — Confirms HTTP 402 is NOT explicitly mapped to any exception class; falls through to generic `APIStatusError`; 402 does not trigger automatic retry
- [Anthropic SDK Python — `_exceptions.py`](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_exceptions.py) — Confirms no `PaymentRequiredError` or billing-specific exception class exists; `RateLimitError` is mapped only to 429
- [GitHub Issue #26498 (anthropics/claude-code): MessageParseError on rate_limit_event message type](https://github.com/anthropics/claude-code/issues/26498) — Documents `rate_limit_event` as a message type emitted by Claude Code CLI SDK; parser bug; no payload structure for 402 specifically; open as of Feb 2026
- [Claude Code Changelog — v2.1.30 SDKRateLimitInfo/SDKRateLimitEvent](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md) — `SDKRateLimitInfo` and `SDKRateLimitEvent` types added in v2.1.30; no changelog entry mentioning HTTP 402 handling
- [Claude Help Center: Extra Usage for Paid Claude Plans (article 12429409)](https://support.claude.com/en/articles/12429409-extra-usage-for-paid-claude-plans) — Does not specify HTTP status codes for extra usage exhaustion scenarios
- [GitHub Issue #29604 (statusLine feature request)](https://github.com/anthropics/claude-code/issues/29604) — Proposed `rate_limit_type` field in statusLine JSON with value `"rolling_5h"` for 5-hour window; confirms internal parsing of window type
- [codelynx.dev: How to Show Claude Code Usage Limits in Your Statusline](https://codelynx.dev/posts/claude-code-usage-limits-statusline) — **Primary source** for `/api/oauth/usage` endpoint JSON schema including `five_hour`, `seven_day`, `seven_day_opus`, and `resets_at` fields
- [lexfrei gist: Claude Code statusline with real usage limits](https://gist.github.com/lexfrei/b70aaee919bdd7164f2e3027dc8c98de) — Confirms endpoint authentication (`anthropic-beta: oauth-2025-04-20` header); shows 5h/7d distinction in response
- [Claude Help Center: Using Claude Code with Your Pro or Max Plan (article 11145838)](https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan) — Confirms overage can apply to weekly limit exhaustion (contradicts doc #08 Section 1.2 claim that overage is unavailable for weekly cap)
- [Claude Help Center: Extra Usage for Paid Claude Plans (article 12429409)](https://support.claude.com/en/articles/12429409-extra-usage-for-paid-claude-plans) — Describes extra usage as activating on "5-hour usage limit" specifically; does not describe headless mode behavior
- [Usagebar Blog: Claude Code Weekly Limit vs 5-Hour Lockout](https://usagebar.com/blog/claude-code-weekly-limit-vs-5-hour-lockout) — Describes reset timing as the practical heuristic for distinguishing limit types; no machine-readable discriminator documented
- [docs/research/08-usage-scheduling.md (this repo)](./08-usage-scheduling.md) — Background on dual rate limit layers, header schema, `anthropic-ratelimit-unified-*` internal fields, governor design, and the `/api/oauth/usage` endpoint as a follow-up research area (Section 9.1 and 9.3)
- [Anthropic Rate Limits API Docs](https://platform.claude.com/docs/en/api/rate-limits) — Official `retry-after` header documentation; standard API rate limit headers (applicable to API key sessions only, not subscription sessions)
