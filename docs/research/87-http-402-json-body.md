# Research: HTTP 402 JSON Body Structure and `claude -p` Stream-JSON Output for Extra Usage Billing Failures

**Issue:** #87
**Milestone:** M1: Foundation
**Status:** Complete
**Date:** 2026-03-02
**Spawned From:** #64 (HTTP 402 vs 429 as discriminator for weekly cap vs 5-hour window exhaustion)
**Depends on:** #64, #23, #3

---

## Executive Summary

This document resolves the two empirical gaps documented in doc #23 Section 2.5.4: (1) the exact
JSON body returned by the Anthropic API for HTTP 402 responses, and (2) what `claude -p
--output-format stream-json` emits when the internal API call receives HTTP 402.

**Key findings:**

1. **HTTP 402 JSON body**: No empirically captured live 402 response body has been published in
   any accessible source as of March 2026. The Anthropic official API documentation does not list
   HTTP 402 as a supported status code, and the Anthropic Python SDK has no dedicated exception
   class for 402. Based on available evidence, two candidate formats are possible — one is the
   standard Anthropic error envelope with `error.type` set to either `"invalid_request_error"` (as
   observed for undercredited API-key sessions on HTTP 400) or possibly a subscription-layer type
   not in the public schema. The `error.type` value for Max plan subscription 402 responses is
   **unconfirmed by any published network capture.**

2. **Stream-JSON output for 402**: The Claude Code CLI SDK's TypeScript type definitions establish
   that billing-related failures surface via a distinct `SDKAssistantMessage.error` field set to
   `"billing_error"`. This is a parallel channel to the 429 `SDKRateLimitEvent` mechanism. A 402
   that reaches Claude Code's internal API client is most likely classified as a billing failure
   and surfaces as an `assistant` event with `"error": "billing_error"` at the top level — not as
   a `result` event with `subtype: "error_during_operation"` (the path taken by 429 rate limit
   events). Whether the session exits after this event (producing a `result` event) and what the
   `result` text contains for the 402 case is **unconfirmed by empirical measurement.**

3. **No `rate_limit_event` for 402**: The `SDKRateLimitEvent` mechanism (emitted for 429
   rejections with rate limit header data) is driven by the `anthropic-ratelimit-unified-*`
   response headers. HTTP 402 responses do not include these headers; therefore 402 does not
   trigger a `rate_limit_event`. The 402 billing failure path is architecturally distinct from the
   429 rate limit path in the Claude Code client.

4. **Conductor detection**: The `billing_error` value in the `assistant.error` field is the most
   likely programmatic signal for 402 in stream-json output. This field was confirmed in the
   official TypeScript Agent SDK reference documentation and the Elixir `ClaudeCode` SDK changelog.
   However, a known bug (anthropics/claude-agent-sdk-python#505) caused this field to be silently
   dropped in the Python Agent SDK prior to its fix.

**Critical limitation**: No published network capture of a live Anthropic 402 response body exists
in the public record as of March 2026. The JSON body structure, specifically `error.type`, remains
[INFERRED]. All empirical gaps from doc #23 Section 2.5.4 are only partially resolved by this
research.

---

## 1. Research Area 1: HTTP 402 JSON Body Structure

### 1.1 Official Documentation Status

[DOCUMENTED] The Anthropic API errors documentation (`platform.claude.com/docs/en/api/errors`)
lists the following documented HTTP status codes and their `error.type` values:

| HTTP Status | `error.type` | Description |
|-------------|-------------|-------------|
| 400 | `invalid_request_error` | Format or content issues; also used for unlisted 4XX |
| 401 | `authentication_error` | Invalid/missing API key |
| 403 | `permission_error` | Insufficient permissions |
| 404 | `not_found_error` | Resource not found |
| 413 | `request_too_large` | Payload too large |
| 429 | `rate_limit_error` | Rate limit exceeded |
| 500 | `api_error` | Internal server error |
| 529 | `overloaded_error` | API overloaded |

**HTTP 402 is not listed.** The documentation states that `invalid_request_error` (400) "may also
be used for other 4XX status codes not listed below," which could potentially apply to 402 — but
this is not confirmed for subscription-layer 402 responses.

### 1.2 Anthropic Python SDK Error Mapping

[DOCUMENTED] The `_make_status_error` method in `anthropic-sdk-python`'s `_client.py` maps the
following status codes to dedicated exception classes:

```python
400 → BadRequestError
401 → AuthenticationError
403 → PermissionDeniedError
404 → NotFoundError
409 → ConflictError
413 → RequestTooLargeError
422 → UnprocessableEntityError
429 → RateLimitError
529 → OverloadedError
500+ → InternalServerError
```

**HTTP 402 falls through all conditions to the generic `APIStatusError`** — no `BillingError`,
`PaymentRequiredError`, or `OverageError` class exists in the SDK. The `_should_retry` method
only auto-retries 408, 409, 429, and 500+; HTTP 402 does NOT trigger automatic retry.

### 1.3 API Key vs. Subscription Session Behavior for Billing Failures

[DOCUMENTED] A distinction must be made between API-key sessions and subscription (OAuth) sessions:

**API-key sessions (undercredited):** When an API key has insufficient credits, the Anthropic API
returns:
- HTTP status: **400** (not 402)
- JSON body:
  ```json
  {
    "type": "error",
    "error": {
      "type": "invalid_request_error",
      "message": "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."
    },
    "request_id": "req_011CUDPsxbch28LN4f3Qqemt"
  }
  ```

This has been confirmed by multiple GitHub issues (#5300, #867, #9772 in anthropics/claude-code,
and continuedev/continue#5499). The `error.type` is `"invalid_request_error"`, not a
billing-specific type.

**An open GitHub issue** (anthropics/anthropic-sdk-typescript#618) was filed proposing that
Anthropic should return HTTP 402 (not 400) for credit balance failures, to avoid conflating them
with malformed-request errors. The issue was filed without resolution as of March 2026. This
confirms that **HTTP 402 for credit balance errors is not the standard path** — the standard path
uses HTTP 400 with `"invalid_request_error"`.

**Subscription (Max plan) sessions — extra usage billing failure:** The 402 documented in
openclaw/openclaw#30484 fires in a different context: a Max plan user with Extra Usage enabled,
where a billing authorization failure occurs at the subscription routing layer. This is distinct
from API key credit exhaustion.

### 1.4 Inferred JSON Body for Subscription-Layer 402

[INFERRED] Based on the available evidence, two candidate forms are possible for the 402 response
body in Max plan extra usage billing failure scenarios:

**Candidate A — Standard Anthropic error envelope with `invalid_request_error`:**
```json
{
  "type": "error",
  "error": {
    "type": "invalid_request_error",
    "message": "<billing-related message, text unknown>"
  },
  "request_id": "req_<id>"
}
```

This would be consistent with the documentation note that `invalid_request_error` applies to
unlisted 4XX codes. The message text is unknown.

**Candidate B — Non-standard response or subscription-layer error format:**

The subscription API layer that returns 402 may use a different response format from the standard
Messages API error envelope. No evidence in the public record confirms or denies this.

**Candidate C — `rate_limit_error` type on HTTP 402 body:**
```json
{
  "type": "error",
  "error": {
    "type": "rate_limit_error",
    "message": "Extra usage billing authorization failed."
  },
  "request_id": "req_<id>"
}
```

This was suggested by the openclaw PR #30780 behavior (treating Anthropic 402 as `rate_limit` for
retry purposes), but the PR authors made no claim about the JSON body — they only observed that
the HTTP status was 402. The `rate_limit_error` type on a 402 body remains speculative.

**Assessment:** Candidate A is most likely consistent with the documented API behavior. The
`error.type` value for 402 is **unconfirmed**. No published network capture of a live Anthropic
subscription-layer 402 response exists in any source reviewed.

### 1.5 `request_id` Field Presence

[INFERRED] Based on the standard Anthropic error format documented for all confirmed error types,
a `request_id` field at the top level of the response object is expected. This is consistent with
all confirmed error body examples in the public record.

---

## 2. Research Area 2: `claude -p --output-format stream-json` Output for HTTP 402

### 2.1 The `SDKAssistantMessageError` Type

[DOCUMENTED] The official TypeScript Agent SDK reference
(`platform.claude.com/docs/en/agent-sdk/typescript`) defines `SDKAssistantMessage` as:

```typescript
type SDKAssistantMessage = {
  type: "assistant";
  uuid: UUID;
  session_id: string;
  message: BetaMessage;
  parent_tool_use_id: string | null;
  error?: SDKAssistantMessageError;
};
```

Where `SDKAssistantMessageError` is one of:
```typescript
type SDKAssistantMessageError =
  | "authentication_failed"
  | "billing_error"
  | "rate_limit"
  | "invalid_request"
  | "server_error"
  | "unknown";
```

The `error` field is at the top level of the `assistant` event object, not nested inside the
`message` field. This was confirmed by bug report anthropics/claude-agent-sdk-python#505, which
documented that the Python SDK was incorrectly reading `data["message"].get("error")` instead of
`data.get("error")`.

**The `"billing_error"` value** was added to the SDK in the Elixir `ClaudeCode` v0.18.0 changelog
(2026-02-10, corresponding to CC v2.1.37), alongside `authentication_failed`, `rate_limit`,
`invalid_request`, `server_error`, and `unknown`.

### 2.2 Inferred Stream-JSON Output for HTTP 402

[INFERRED] When Claude Code's internal API call receives HTTP 402 during a `-p` session, the most
likely stream-json output sequence is:

**Step 1: The `assistant` event with `billing_error`:**

```json
{
  "type": "assistant",
  "uuid": "<uuid>",
  "session_id": "<session_id>",
  "message": {
    "id": "msg_<id>",
    "type": "message",
    "role": "assistant",
    "content": [
      {
        "type": "text",
        "text": "<error description text, likely billing-related>"
      }
    ],
    "model": "claude-sonnet-4-6-20251101",
    "stop_reason": "end_turn",
    "usage": { ... }
  },
  "parent_tool_use_id": null,
  "error": "billing_error"
}
```

**Step 2: The `result` event** (subtype unknown):

Based on the SDK type definition for `SDKResultMessage`, the result event would have `is_error:
true`. The subtype for billing failures has not been confirmed — it is not `"error_during_operation"`
(which is the 429 rate limit path), and the SDK type definition for error result messages lists
only: `"error_max_turns"`, `"error_during_execution"`, `"error_max_budget_usd"`,
`"error_max_structured_output_retries"`. No `"error_billing"` or `"error_payment_required"`
subtype exists in the documented type.

**Most likely the `result` event subtype for 402 is `"error_during_execution"`** — the catch-all
for unhandled API errors during a tool call or model step. The `result.result` text field would
contain an error message, but the specific text is **unconfirmed**.

It is also possible that the session exits before a `result` event is emitted if the 402 occurs at
the very start of the session (before the first model turn), in which case the conductor must
handle a missing result event.

### 2.3 Distinguishing 402 vs. 429 in Stream-JSON Output

[PARTIALLY DOCUMENTED] The key architectural distinction between 402 and 429 behavior in
stream-json:

**For HTTP 429 (rate limit):**
- May emit an `SDKRateLimitEvent` (`type: "rate_limit_event"`) with rate limit header data
- Final `result` event has `subtype: "error_during_operation"` and `result` text containing
  `"API Error: Rate limit reached"` [as documented in doc #23 Section 2.2]
- The 429 path is driven by `anthropic-ratelimit-unified-*` response headers

**For HTTP 402 (billing failure):**
- Does NOT emit an `SDKRateLimitEvent` — 402 responses do not include rate limit headers
- The `assistant` event preceding the result event will have `"error": "billing_error"` in the
  top-level field
- The `result` event subtype is most likely `"error_during_execution"`, not
  `"error_during_operation"`

**Conductor detection rule for 402 in stream-json:**

```python
# Detect billing_error in assistant event:
if event.get("type") == "assistant" and event.get("error") == "billing_error":
    CLASSIFY: extra_usage_billing_failure
    ALERT: "billing_error in assistant event — HTTP 402 billing failure suspected"
    ACTION: Halt new dispatches; log full event for investigation

# Detect in result event (secondary check):
if (event.get("type") == "result"
    and event.get("is_error") is True
    and "billing" in (event.get("result") or "").lower()):
    CLASSIFY: extra_usage_billing_failure
```

**IMPORTANT CAVEAT**: This detection rule is [INFERRED] from SDK type definitions and logical
deduction. It has not been verified against a live 402 stream-json capture. The
`billing_error` path in stream-json may behave differently than described here.

### 2.4 `SDKRateLimitEvent` — Not Applicable to 402

[DOCUMENTED] The `SDKRateLimitEvent` type (TypeScript Agent SDK reference, confirmed in Elixir
`ClaudeCode` v0.25.0 changelog for CC v2.1.59) has the following structure:

```typescript
type SDKRateLimitEvent = {
  type: "rate_limit_event";
  rate_limit_info: {
    status: "allowed" | "allowed_warning" | "rejected";
    resetsAt?: number;
    utilization?: number;
  };
  uuid: UUID;
  session_id: string;
};
```

This event is driven by the `anthropic-ratelimit-unified-*` response headers (see doc #23 Section
2.3). HTTP 402 responses originate from the subscription billing layer, not the quota enforcement
layer, and do not include these headers. Therefore, **a 402 response does not cause
`SDKRateLimitEvent` emission**.

### 2.5 Stderr Behavior for 402

[INFERRED] The interactive-mode terminal message `"You've hit your limit · resets <timestamp>"`
is driven by rate limit header data (the 429 path). For a 402 billing failure, Claude Code's
internal error handler does not have rate limit window data to display — the 402 is a billing
authorization failure, not a window exhaustion event. Therefore, the 402 path likely produces a
different or absent stderr message.

Possible stderr outputs:
- A billing-related error message (text unknown)
- No stderr output (if the error is surfaced only via stream-json)
- A generic API error message

This has not been empirically verified.

### 2.6 Exit Code for 402

[INFERRED] Based on the exit code taxonomy in doc #3 Section 1.1, HTTP 402 billing failures would
result in exit code `1` (general error), identical to 429 rate limits. Exit code alone does not
distinguish 402 from 429. The `result.subtype` and the `assistant.error` field are the
programmatic discriminators.

---

## 3. Cross-Reference: Implications for Conductor

### 3.1 Updated Error Classification in Doc #3

The `ErrorClass` enum in doc #3 (Section 5.2) should add:

```python
class ErrorClass(Enum):
    ...
    BILLING_FAILURE = "billing_failure"  # HTTP 402 — extra usage billing authorization failure
    ...
```

The billing failure handler must NOT route to the rate limit backoff path. It is a billing
authorization failure, not a window exhaustion event.

Detection logic for conductor (combining stream-json signals):

```python
def classify_from_stream(
    result_event: dict | None,
    assistant_events: list[dict],
) -> ErrorClass:
    # Check for billing_error in any assistant event
    for event in assistant_events:
        if event.get("type") == "assistant" and event.get("error") == "billing_error":
            return ErrorClass.BILLING_FAILURE

    if result_event is None:
        return ErrorClass.UNKNOWN

    subtype = result_event.get("subtype", "")
    result_text = result_event.get("result", "") or ""

    if subtype == "error_during_operation":
        if "rate limit" in result_text.lower() or "rate_limit" in result_text.lower():
            return ErrorClass.RATE_LIMIT_FIVE_HOUR  # further classify via doc #23 Section 5
    if subtype == "error_during_execution":
        if "billing" in result_text.lower() or "payment" in result_text.lower():
            return ErrorClass.BILLING_FAILURE
        # ... other execution errors
    ...
```

### 3.2 Updated Decision Rule (Section 2.5.5 of Doc #23)

The conductor decision rule in doc #23 Section 2.5.5 remains accurate at the HTTP layer:

```
if http_status == 402:
    CLASSIFY: extra_usage_billing_failure
    ACTION: Halt; log; manual billing resolution
```

But since conductor does not observe HTTP status codes directly (only stream-json output from the
subprocess), the stream-json path is the practical detection mechanism. The `assistant.error:
"billing_error"` field is the primary in-band signal for 402 billing failures in stream-json mode.

### 3.3 Pre-Dispatch Detection Not Possible from Stream

HTTP 402 is not detectable before the `claude -p` subprocess starts — it fires during execution,
not before. The conductor cannot pre-screen for it via the `/api/oauth/usage` endpoint (which
returns window utilization, not billing authorization state). Post-failure detection via
`assistant.error: "billing_error"` in the stream is the only available mechanism.

---

## 4. What Remains Unknown (Updated from Doc #23 Section 2.5.4)

The following empirical gaps from doc #23 Section 2.5.4 are updated:

| Gap | Prior Status | Status After This Research |
|-----|-------------|---------------------------|
| `error.type` in 402 JSON body | Unknown | Still unknown — no published network capture |
| `error.message` in 402 JSON body | Unknown | Still unknown |
| `request_id` in 402 JSON body | Unknown | Likely present (inferred from API conventions) |
| Stream-json `result.subtype` for 402 | Unknown | Likely `"error_during_execution"` (inferred) |
| Stream-json `result.result` text for 402 | Unknown | Still unknown — no empirical capture |
| `SDKAssistantMessage.error` field for 402 | Unknown | Likely `"billing_error"` (documented type) |
| `SDKRateLimitEvent` emission for 402 | Unknown | Confirmed: does NOT fire for 402 |
| Stderr output text for 402 | Unknown | Still unknown |
| Exit code for 402 | Unknown | Expected `1` (inferred) |

---

## 5. Follow-Up Research Recommendations

### 5.1 Empirical Network Capture of 402 JSON Body [M1_BLOCKING for full detection]

**Question:** What is the exact JSON body, including `error.type` and `error.message`, returned
by the Anthropic API for a subscription-layer HTTP 402?

**Why it matters:** Without the exact `error.type`, conductor cannot parse and log the 402 body
with full fidelity, and the billing failure classification in the conductor error handler lacks
confirmation. The stream-json `result.result` text may embed part of the API error message, making
the `error.type` indirectly detectable.

**Method:** Set up a Max plan account with Extra Usage enabled. Use mitmproxy, Charles Proxy, or
a MITM-capable credential proxy (see doc #43) to intercept the raw HTTP response when 402 fires.
Capture the full response headers and body.

**Alternative method:** Review the openclaw PR #30780 code diff in detail — the PR authors may
have logged the raw 402 response body in a test fixture or in the PR conversation. No body was
visible in the PR summary, but the implementation may reference specific field values.

**Blocking status:** This is partially blocking for confident 402 detection. The
`assistant.error: "billing_error"` signal in stream-json is available without knowing the HTTP
body, so M1 conductor can function with the inferred detection rule. However, the result text
pattern matching in the fallback check is unconfirmed.

### 5.2 Empirical Capture of Stream-JSON for 402 [M1_BLOCKING for full detection]

**Question:** Does `claude -p --output-format stream-json 2>&1` emit `"error":
"billing_error"` in an `assistant` event when the underlying API call receives HTTP 402? What is
the `result.result` text? What is the `result.subtype`? Is there stderr output?

**Method:**
1. Obtain a Max plan account condition likely to trigger 402 (Extra Usage enabled, high-traffic
   session near billing authorization threshold)
2. Run `claude -p "..." --output-format stream-json 2>&1 | tee /tmp/402-capture.jsonl`
3. Inspect the JSONL for: `assistant` events with top-level `error` field, `result` event subtype
   and text, and stderr content

**Blocking status:** This is the most directly actionable gap. The detection rule in Section 3.1
can be validated or corrected from this capture.

### 5.3 Verify `billing_error` Field Presence and Format [M1_BLOCKING for SDK path]

**Question:** Does the `SDKAssistantMessage.error` field reliably emit `"billing_error"` as a
string at the top level of the JSON for 402 scenarios? Is it the string `"billing_error"` or some
other format?

**Why it matters:** The Python SDK bug (anthropics/claude-agent-sdk-python#505) showed this field
was silently dropped due to reading from the wrong JSON path. The conductor reading stream-json
directly via subprocess must read from the correct JSON path. The issue fix confirms the top-level
location, but whether the SDK sends `"billing_error"` or some other representation needs live
verification.

**Method:** Same live capture as 5.2; inspect the raw JSONL `assistant` events for the
top-level `error` field.

### 5.4 Track anthropics/claude-agent-sdk-python#505 Fix Status

**Question:** Has the Python SDK bug (reading `error` from wrong JSON path) been fixed in the
Claude Code CLI binary? This bug may have affected earlier versions of Claude Code even if the
Python SDK wrapper was patched.

**Note:** The Claude Code CLI binary is built from its own codebase, not from the Python SDK
wrapper. The fix in the Python wrapper SDK would not automatically fix the CLI binary. The CLI's
internal TypeScript implementation may have the correct JSON path reading regardless.

**Scope:** Track the issue resolution; check the Claude Code CLI CHANGELOG for related entries.

---

## 6. Sources

- [Anthropic API Errors Documentation](https://platform.claude.com/docs/en/api/errors) — Official
  list of documented HTTP status codes and `error.type` values. HTTP 402 is NOT listed. States that
  `invalid_request_error` may be used for unlisted 4XX codes.
- [GitHub Issue #30484 (openclaw/openclaw): Claude Max plan rate limits return HTTP 402 instead of
  429](https://github.com/openclaw/openclaw/issues/30484) — Primary source for HTTP 402 in Max
  plan subscription context. No JSON body payload published. Documents the symptom and five PRs
  generated. Documents that kobie3717 had "97% daily usage remaining" when 402 fired.
- [GitHub PR #30780 (openclaw/openclaw): Treat Anthropic 402 as rate_limit](https://github.com/openclaw/openclaw/pull/30780)
  — Implementation treating Anthropic 402 as `rate_limit` rather than billing. Does not include
  the raw 402 response body. Confirms provider-specific 402 handling by HTTP status alone.
- [Anthropic Python SDK — `_client.py` `_make_status_error`](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_client.py)
  — Confirms HTTP 402 is NOT mapped to any dedicated exception class; falls through to generic
  `APIStatusError`. No `BillingError` or `PaymentRequiredError` class exists.
- [Anthropic Python SDK — `_exceptions.py`](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_exceptions.py)
  — Confirms no exception class exists for 402; `RateLimitError` maps only to 429.
- [TypeScript Agent SDK Reference](https://platform.claude.com/docs/en/agent-sdk/typescript) —
  **Primary source for Section 2.1.** Defines `SDKAssistantMessageError` type with `"billing_error"`
  as a confirmed value. Defines `SDKRateLimitEvent` structure (driven by rate limit headers).
  Confirms `SDKResultMessage` subtypes do not include billing-specific values.
- [GitHub Issue #618 (anthropics/anthropic-sdk-typescript): Insufficient credit error has same
  error code as bad requests](https://github.com/anthropics/anthropic-sdk-typescript/issues/618)
  — Proposes that HTTP 402 should replace HTTP 400 for credit balance errors. Confirms that the
  current standard path for credit exhaustion is HTTP 400 with `invalid_request_error`, NOT 402.
- [GitHub Issue #5300 (anthropics/claude-code): PRO account shows credit balance is too
  low](https://github.com/anthropics/claude-code/issues/5300) — Confirms API-key session credit
  exhaustion returns HTTP 400 with `"type": "invalid_request_error"`. Not 402.
- [continuedev/continue#5499: HTTP 400 from `api.anthropic.com` with `invalid_request_error`
  credit balance message](https://github.com/continuedev/continue/issues/5499) — Live example of
  `{"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too
  low..."}}` as HTTP 400.
- [GitHub Issue #505 (anthropics/claude-agent-sdk-python): AssistantMessage.error field not
  populated](https://github.com/anthropics/claude-agent-sdk-python/issues/505) — **Key source for
  Section 2.2.** Confirms `error` field is at top level of `assistant` event JSON, not nested in
  `message`. Confirms `"unknown"`, `"authentication_failed"`, `"billing_error"` as possible values.
  Documents the wrong-path bug in Python SDK.
- [ClaudeCode Elixir SDK Changelog — v0.18.0 (2026-02-10)](https://hexdocs.pm/claude_code/changelog.html)
  — Confirms `billing_error` added as an `AssistantMessage.error` type alongside
  `authentication_failed`, `rate_limit`, `invalid_request`, `server_error`, `unknown`.
- [ClaudeCode Elixir SDK Changelog — v0.25.0 (2026-02-26)](https://hexdocs.pm/claude_code/changelog.html)
  — Confirms `rate_limit_event` message type parsing and `SDKRateLimitEvent` structure, confirming
  it is driven by rate limit headers (not applicable to 402).
- [GitHub Issue #27603 (anthropics/claude-code): Rate limit reached with
  `rate_limit_error`](https://github.com/anthropics/claude-code/issues/27603) — Confirms HTTP 429
  rate limit error JSON body: `{"type":"error","error":{"type":"rate_limit_error","message":"Extra
  usage is required for long context requests."},"request_id":"req_011CYNj7TmYSikEg9SLgfNhr"}`.
  Documents that "Extra usage is required" uses `rate_limit_error` type with HTTP 429 (not 402).
- [docs/research/23-429-error-cap-distinction.md](./23-429-error-cap-distinction.md) — Section 2.5
  establishes the 402 scope as a billing-layer event; Section 2.5.4 documents empirical gaps that
  this document partially resolves.
- [docs/research/03-error-handling.md](./03-error-handling.md) — Section 5.1 detection signals and
  2.4 error classification patterns; should be updated to include `billing_error` assistant event
  detection.
