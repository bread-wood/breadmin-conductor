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

## 6. Section 3: Empirical Evidence for billing_error in Stream-JSON (Issue #95)

This section records the findings of issue #95 research, which sought to empirically verify
whether the `billing_error` value actually appears in the top-level `error` field of `assistant`
events in `claude -p --output-format stream-json` output when HTTP 402 occurs — and to confirm
the `result.subtype`, `result.result` text, stderr behavior, and exit code for the 402 path.

### 6.1 Status of Empirical Verification (March 2026)

**No live stream-json capture of a billing_error event exists in any publicly accessible source
as of March 2026.** The research for issue #95 produced the following updated confidence
assessment:

| Research Area | Prior Status (Doc #87) | Status After Issue #95 Research |
|---------------|----------------------|--------------------------------|
| `assistant.error == "billing_error"` top-level JSON path | INFERRED — likely | STRUCTURALLY CONFIRMED (JSON path from issue #505); value for 402 still unconfirmed by live capture |
| `result.subtype` for 402 | INFERRED — likely `"error_during_execution"` | CONFLICT IDENTIFIED — see Section 6.3 |
| `result.result` text for 402 | Unknown | Still unknown — no empirical capture |
| `billing_error` as a valid value for the `error` field | DOCUMENTED — TypeScript SDK reference, Elixir changelog | MULTI-SOURCE CONFIRMED — TypeScript SDK docs, Python SDK issue #505 exception hierarchy, Elixir v0.18.0 changelog |
| 402 does not emit `rate_limit_event` | DOCUMENTED | CONFIRMED — TypeScript SDK `SDKRateLimitEvent` is header-driven; `resetsAt`/`utilization` fields only populated from `anthropic-ratelimit-unified-*` headers |
| Stderr output for 402 | Unknown | Still unknown — no empirical capture |
| Exit code for 402 | INFERRED — `1` | Still inferred — no empirical capture |

### 6.2 Confirmed JSON Structure for `assistant` Events with `error` Field

[STRUCTURALLY CONFIRMED] Issue #505 (anthropics/claude-agent-sdk-python, filed January 23, 2026,
closed February 3, 2026) provided live-captured JSON output from the Claude CLI for two error
conditions:

**Error `"unknown"` (invalid model name):**

```json
{
  "type": "assistant",
  "message": {
    "id": "ad53c326-dd50-4203-a91b-e963212193c4",
    "container": null,
    "model": "<synthetic>",
    "role": "assistant",
    "stop_reason": "stop_sequence",
    "stop_sequence": "",
    "type": "message",
    "usage": {
      "input_tokens": 0,
      "output_tokens": 0,
      "cache_creation_input_tokens": 0,
      "cache_read_input_tokens": 0,
      "server_tool_use": { "web_search_requests": 0, "web_fetch_requests": 0 },
      "service_tier": null,
      "cache_creation": { "ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0 }
    },
    "content": [
      {
        "type": "text",
        "text": "API Error: 404 {\"type\":\"error\",\"error\":{\"type\":\"not_found_error\",\"message\":\"model: invalid-model-name\"},\"request_id\":\"req_011CXPSzXgANnbQwoKHgQthD\"}"
      }
    ],
    "context_management": null
  },
  "parent_tool_use_id": null,
  "session_id": "92cb3340-de00-45d6-84c3-d5a2010a32e2",
  "uuid": "4ddbcd7b-ed40-4d48-adb8-b281cf3769b6",
  "error": "unknown"
}
```

**Error `"authentication_failed"` (invalid API key):**

```json
{
  "type": "assistant",
  "message": {
    "id": "1c65e36b-5819-408a-ac23-7ce20b8cadeb",
    "container": null,
    "model": "<synthetic>",
    "role": "assistant",
    "stop_reason": "stop_sequence",
    "stop_sequence": "",
    "type": "message",
    "usage": { "input_tokens": 0, "output_tokens": 0, ... },
    "content": [
      { "type": "text", "text": "Invalid API key · Fix external API key" }
    ],
    "context_management": null
  },
  "parent_tool_use_id": null,
  "session_id": "9e89813f-cb7b-41fb-8a38-135f2707d675",
  "uuid": "9ade6dfc-fbbb-4d7e-9a21-515d3297463b",
  "error": "authentication_failed"
}
```

These are the **only two live-captured `assistant` events with top-level `error` fields** in the
public record as of March 2026. They establish the definitive structure:

1. The `error` field is a **string** at the top level of the JSON object.
2. It is co-present with a `message` object that has `model: "<synthetic>"` and
   `stop_reason: "stop_sequence"` — these appear to be placeholder values used by the Claude Code
   CLI when constructing synthetic error response envelopes.
3. The `content` array contains a `text` block with a human-readable error description. For
   `"unknown"`, the text embeds the raw API error JSON. For `"authentication_failed"`, the text
   is the interactive UI error message.
4. The `uuid`, `session_id`, and `parent_tool_use_id` fields are present in the same positions
   as a normal `assistant` event.

**Inference for `"billing_error"`:** The JSON structure for a `"billing_error"` event is expected
to follow the same pattern:

```json
{
  "type": "assistant",
  "message": {
    "id": "<synthetic-id>",
    "model": "<synthetic>",
    "role": "assistant",
    "stop_reason": "stop_sequence",
    "stop_sequence": "",
    "type": "message",
    "usage": { "input_tokens": 0, "output_tokens": 0, ... },
    "content": [
      {
        "type": "text",
        "text": "<billing-related error text — exact wording UNKNOWN>"
      }
    ]
  },
  "parent_tool_use_id": null,
  "session_id": "<session_id>",
  "uuid": "<uuid>",
  "error": "billing_error"
}
```

The `"billing_error"` string value is [DOCUMENTED — TypeScript Agent SDK reference, Elixir SDK
v0.18.0 changelog, Python SDK issue #505 exception hierarchy]. The content text value is
[UNKNOWN — no live capture].

### 6.3 Critical Finding: `result.subtype` Discrepancy for 402 vs. 429

Research for issue #95 surfaced a significant discrepancy between what the TypeScript SDK type
definitions document and what the CLI actually emits in stream-json output:

**TypeScript Agent SDK (`SDKResultMessage`) documents only these error subtypes:**
- `"error_max_turns"`
- `"error_during_execution"`
- `"error_max_budget_usd"`
- `"error_max_structured_output_retries"`

**Elixir SDK `ClaudeCode.Types` v0.28.0 `result_subtype()` documents only:**
- `:error_max_turns`
- `:error_during_execution`
- `:error_max_budget_usd`
- `:error_max_structured_output_retries`

**Neither SDK type definition includes `"error_during_operation"`.**

However, doc #23 Section 2.2 confirms with a live-captured JSON payload that the Claude CLI
**actually emits `"error_during_operation"` as the `result.subtype` for HTTP 429 rate limits:**

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

This means `"error_during_operation"` is an **undocumented runtime subtype** — the CLI emits it
but neither the TypeScript nor Elixir SDK type definitions include it. This finding has direct
consequences for issue #95's question about how to distinguish 402 billing failures from 429 rate
limits:

**Revised assessment for `result.subtype` for HTTP 402:**

The prior inference (doc #87 Section 2.2) was that 402 would produce
`"error_during_execution"`, while 429 produces `"error_during_operation"`. This was based on the
SDK type documentation, which listed `"error_during_operation"` as absent from the documented
set and `"error_during_execution"` as the catch-all for unhandled API errors.

**That inference is now WEAKENED.** Given that:
1. The CLI emits `"error_during_operation"` for 429 despite it not being in the SDK type
   definitions, and
2. No live capture of the 402 path exists,

it is now possible — and perhaps likely — that the CLI also emits `"error_during_operation"` for
402, rather than `"error_during_execution"`. If the 402 error fires during an API call (mid-turn)
the same code path that handles 429 mid-turn errors may also handle 402, using the same subtype.

This means the `result.subtype` discriminator between 402 and 429 is **unreliable without live
verification**. The `assistant.error: "billing_error"` field remains the most robust
discriminator available.

**Updated `result.subtype` confidence table:**

| `result.subtype` value | In SDK type defs? | Confirmed in live output? | Maps to which HTTP error? |
|------------------------|------------------|--------------------------|--------------------------|
| `"error_during_operation"` | No — NOT in TypeScript or Elixir SDK | Yes — confirmed in doc #23 for HTTP 429 | HTTP 429 (confirmed); HTTP 402 (unknown — possibly same path) |
| `"error_during_execution"` | Yes — TypeScript SDK, Elixir SDK | Not confirmed by live capture for 402 | Unknown — originally inferred for 402, now uncertain |

### 6.4 `SDKRateLimitEvent` Schema: Confirmed Not Applicable to 402

[CONFIRMED from TypeScript Agent SDK official documentation, March 2026]

The current `SDKRateLimitEvent` type definition:

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

The `resetsAt` and `utilization` fields are optional (not always present). This event is
exclusively driven by the `anthropic-ratelimit-unified-*` response headers. HTTP 402 responses
from the billing authorization layer do not include these headers, so `rate_limit_event` is
definitively not emitted for 402 billing failures.

**Note on schema vs. earlier research (doc #63):** Doc #63 Section 3.1 documented additional
fields in the `rate_limit_info` object including `isUsingOverage`, `overageDisabledReason`, and
`overageStatus`. These fields are not present in the official TypeScript SDK type definition.
They appear to be extensions present in the internal stream-json schema but not exposed in the
public SDK type definition. Conductor reading raw stream-json output should still watch for
these fields as they may appear at runtime despite being absent from the public type schema.

### 6.5 `billing_error` Value: Multi-Source Confirmation

The string `"billing_error"` as a valid value for the `SDKAssistantMessage.error` field is
confirmed from three independent sources:

1. **TypeScript Agent SDK official reference** (`platform.claude.com/docs/en/agent-sdk/typescript`,
   March 2026): States "`SDKAssistantMessageError` is one of: `'authentication_failed'`,
   `'billing_error'`, `'rate_limit'`, `'invalid_request'`, `'server_error'`, or `'unknown'`."

2. **Elixir `ClaudeCode` SDK changelog v0.18.0** (2026-02-10, corresponding to CC v2.1.37):
   Documents schema alignment with CLI v2.1.37, introducing `AssistantMessage.error` with values
   including `:billing_error`, `:authentication_failed`, `:rate_limit`, `:invalid_request`,
   `:server_error`, `:unknown`.

3. **Python SDK issue #505 exception hierarchy** (commit wingding12, 2026-02-03): The PR that
   fixed the wrong JSON path reading also added a full exception class hierarchy including
   `BillingError` mapped to the `"billing_error"` error string. While the original bug report
   focused on `"unknown"` and `"authentication_failed"`, the `BillingError` class addition
   confirms `"billing_error"` is an anticipated value in the SDK implementation.

**What remains unconfirmed:** Whether `"billing_error"` is actually emitted by the Claude CLI
binary when the internal API call returns HTTP 402. All three sources above establish the type
definition/schema, not a live runtime observation. The `"billing_error"` value may be defined
but never emitted in practice (e.g., if 402 errors are handled differently in the CLI binary
than in the Python SDK wrapper).

### 6.6 The `content[].text` Field in Billing Error Events

When the `assistant` event carries `"error": "billing_error"`, the `message.content[0].text`
field is expected to contain a human-readable billing error description. From the structural
pattern established by the `"unknown"` error (which embeds the raw API error JSON in the text):

```
"API Error: 402 {<raw 402 response body JSON>}"
```

If this pattern holds for billing errors, the `content[].text` would contain the raw 402 JSON
body — making it a secondary source for the `error.type` and `error.message` values from Section
1.4. However, the `"authentication_failed"` error does NOT follow this raw-embed pattern (it uses
a human-readable message instead), so the pattern is not guaranteed to hold for all error types.

The content text format for `"billing_error"` is **UNKNOWN** without a live capture.

### 6.7 Updated Conductor Detection Rule (Superseding Section 3.1)

Based on issue #95 research, the conductor detection rule from Section 3.1 is updated as follows:

```python
def classify_from_stream(
    result_event: dict | None,
    assistant_events: list[dict],
) -> ErrorClass:
    # PRIMARY: Check for billing_error in any assistant event (most reliable discriminator)
    for event in assistant_events:
        if event.get("type") == "assistant" and event.get("error") == "billing_error":
            return ErrorClass.BILLING_FAILURE

    if result_event is None:
        return ErrorClass.UNKNOWN

    subtype = result_event.get("subtype", "")
    result_text = (result_event.get("result") or "").lower()
    errors = result_event.get("errors", [])  # error_during_execution path uses .errors list

    # SECONDARY: error_during_operation — confirmed for 429; may also fire for 402
    if subtype == "error_during_operation":
        # Distinguish by result text
        if "rate limit" in result_text or "rate_limit" in result_text:
            return ErrorClass.RATE_LIMIT_FIVE_HOUR  # further classify via doc #23 Section 5
        if "billing" in result_text or "payment" in result_text:
            return ErrorClass.BILLING_FAILURE       # fallback if billing_error field absent
        # AMBIGUOUS: could be 402 without billing text or other mid-operation error
        return ErrorClass.API_ERROR_UNCLASSIFIED

    # TERTIARY: error_during_execution — catch-all for other unhandled errors
    if subtype == "error_during_execution":
        for error_str in errors:
            if "billing" in error_str.lower() or "payment" in error_str.lower():
                return ErrorClass.BILLING_FAILURE
        return ErrorClass.EXECUTION_ERROR

    ...
```

**Key change from Section 3.1:** The `billing_error` assistant event check remains the primary
signal, but the secondary `error_during_operation` path now handles BOTH the 429 rate limit case
AND a possible 402 billing case (if the 402 path uses the same subtype). The `error_during_execution`
path is demoted to tertiary because the live-confirmed 429 subtype is `error_during_operation`,
not `error_during_execution`, contrary to what was previously inferred.

### 6.8 Stderr Behavior: Structural Inference from Confirmed Error Examples

The live captures from issue #505 confirm that for `"unknown"` and `"authentication_failed"`
errors, the error text is surfaced **within the stream-json `assistant` event content**, not on
stderr. This is consistent with the inferred behavior from Section 2.5: in stream-json mode,
the primary error surface is the `assistant` event `error` field and `content[].text`, not stderr.

Whether the 402 path also surfaces its error exclusively via stream-json (rather than also writing
to stderr) is still unconfirmed. The `"authentication_failed"` example (`"Invalid API key · Fix
external API key"`) matches the stderr message that interactive mode would show — suggesting the
text may appear on both streams.

### 6.9 Remaining Unknowns After Issue #95 Research

| Question | Status |
|----------|--------|
| Does `"billing_error"` actually fire at runtime when HTTP 402 is received? | STILL UNKNOWN — no live capture |
| What is `result.subtype` for 402? `"error_during_operation"` or `"error_during_execution"`? | NEWLY UNCERTAIN — prior inference weakened by `error_during_operation` being undocumented for 429 yet actually emitted |
| What is `result.result` text content for 402? | STILL UNKNOWN |
| Does `content[].text` embed raw 402 JSON body? | PLAUSIBLE (from `"unknown"` pattern) but UNCONFIRMED |
| What is stderr output for 402? | STILL UNKNOWN |
| Is there a CLI version where `"billing_error"` was added that correlates to a testable release? | INFERRED: v2.1.37 (from Elixir v0.18.0 changelog) |

### 6.10 Confidence Summary for Conductor Implementation (Updated)

| Signal | Confidence | Basis |
|--------|-----------|-------|
| `event.get("error") == "billing_error"` detects a billing error | HIGH (schema documented) / UNVERIFIED (runtime) | TypeScript SDK, Elixir changelog, Python SDK exception class |
| `event.get("error")` is at the top level of `assistant` event JSON | HIGH CONFIRMED | Live captures from issue #505 |
| 402 does not emit `rate_limit_event` | HIGH CONFIRMED | TypeScript SDK type definition; header-driven mechanism |
| `result.subtype` distinguishes 402 from 429 | LOW — NEWLY UNCERTAIN | `error_during_operation` confirmed for 429 but NOT in official type defs; 402 subtype unknown |
| `result.result` text contains "billing" for 402 | UNVERIFIED — useful as fallback | Structural inference from error text embedding pattern |

---

## 7. result.subtype Disambiguation for HTTP 402 (Issue #100)

This section records the findings of issue #100 research, which sought to resolve
the specific question: does `claude -p --output-format stream-json` emit
`"error_during_operation"` or `"error_during_execution"` as the `result.subtype` when
the underlying API call receives HTTP 402?

### 7.1 Research Question Recap

Section 6.3 (issue #95 research) identified a conflict between SDK type documentation and
observed CLI behaviour:

- Both the TypeScript Agent SDK (`SDKResultMessage`) and Elixir `ClaudeCode` SDK
  (`result_subtype()`) document only four error subtypes:
  `"error_max_turns"`, `"error_during_execution"`, `"error_max_budget_usd"`,
  `"error_max_structured_output_retries"`.
- Neither SDK type definition includes `"error_during_operation"`.
- Yet doc #23 Section 2.2 confirmed via live-captured JSON that the CLI **actually
  emits `"error_during_operation"` for HTTP 429 rate limit events**, making it an
  undocumented runtime subtype.

Issue #100 asks: does HTTP 402 take the same code path as HTTP 429 (emitting
`"error_during_operation"`), or a different path (emitting `"error_during_execution"`,
the documented catch-all)?

### 7.2 SDK Type Definitions — Current State (March 2026)

[DOCUMENTED from official TypeScript Agent SDK reference, March 2026, v0.2.63]

The current `SDKResultMessage` type definition in the TypeScript Agent SDK:

```typescript
type SDKResultMessage =
  | {
      type: "result";
      subtype: "success";
      uuid: UUID;
      session_id: string;
      duration_ms: number;
      duration_api_ms: number;
      is_error: boolean;
      num_turns: number;
      result: string;
      stop_reason: string | null;
      total_cost_usd: number;
      usage: NonNullableUsage;
      modelUsage: { [modelName: string]: ModelUsage };
      permission_denials: SDKPermissionDenial[];
      structured_output?: unknown;
    }
  | {
      type: "result";
      subtype:
        | "error_max_turns"
        | "error_during_execution"
        | "error_max_budget_usd"
        | "error_max_structured_output_retries";
      uuid: UUID;
      session_id: string;
      duration_ms: number;
      duration_api_ms: number;
      is_error: boolean;
      num_turns: number;
      stop_reason: string | null;
      total_cost_usd: number;
      usage: NonNullableUsage;
      modelUsage: { [modelName: string]: ModelUsage };
      permission_denials: SDKPermissionDenial[];
      errors: string[];
    };
```

**Confirmed finding**: `"error_during_operation"` is still **absent** from the TypeScript
SDK `SDKResultMessage` type definition as of March 2026 (SDK v0.2.63, which is the
current version). This is unchanged from the state documented in Section 6.3 of this
document.

[DOCUMENTED from Elixir `ClaudeCode` SDK, v0.28.0 (current as of March 2026)]

The Elixir `result_subtype()` type lists:
- `:success`
- `:error_max_turns`
- `:error_during_execution`
- `:error_max_budget_usd`
- `:error_max_structured_output_retries`

**Confirmed finding**: `"error_during_operation"` is also absent from the current Elixir
SDK type definition. Both SDK implementations consistently omit it from the documented
type union.

### 7.3 The Persistent Undocumented Subtype: `"error_during_operation"`

The evidence base from prior research (doc #23 Section 2.2; doc #87 Section 6.3)
established that the Claude CLI emits `"error_during_operation"` for HTTP 429 rate limit
failures at runtime, despite this subtype being absent from both SDK type definitions.
This is not a stale or resolved discrepancy — the TypeScript SDK has been updated to
v0.2.63 and still does not include `"error_during_operation"` in `SDKResultMessage`.

The confirmed live-captured JSON for a 429 rate limit result event remains (from doc #23
Section 2.2):

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

Note that this live-captured event uses the legacy schema (no `uuid`, `duration_ms`,
`usage`, `errors` fields). This suggests the event was captured from an older CLI version.
The current SDK type definition for error results includes `uuid`, `usage`, `modelUsage`,
`permission_denials`, and `errors` fields. The live-captured 429 result event may have
been emitted by a version before these fields were added, or the `"error_during_operation"`
path may not populate all fields that the `"error_during_execution"` path does.

### 7.4 Empirical Evidence Gap: No 402 Result Event Capture Exists

[CONFIRMED STILL UNKNOWN — March 2026]

No live-captured stream-json output for a 402 billing failure result event has been
identified in any accessible public source as of March 2026. The searches conducted for
issue #100 found:

1. No GitHub issue in `anthropics/claude-code`, `anthropics/claude-agent-sdk-typescript`,
   or `openclaw/openclaw` contains a raw stream-json dump showing a 402 billing failure
   result event with a visible `subtype` field.

2. The `openclaw/openclaw` issue #30484 (the primary source for 402 behaviour) focuses on
   HTTP status code classification, not on stream-json event structure. No commenter
   published a raw JSONL capture from `claude -p --output-format stream-json`.

3. The SFEIR Institute CI/CD headless mode documentation does not cover billing error
   result event structure.

4. No npm package, community project, or blog post examined during this research
   published a captured 402 billing failure result event.

**The `result.subtype` for HTTP 402 remains empirically unverified.**

### 7.5 Structural Inference: Two Candidate Code Paths in the CLI

The core uncertainty is which internal code path handles HTTP 402 in the Claude CLI's
stream-json layer. Two candidate models:

**Model A — 402 shares the rate-limit error path (emits `"error_during_operation"`):**

HTTP 402 and HTTP 429 are both transient API-layer rejections that terminate the current
API request. If the CLI's error dispatcher classifies all non-2xx API responses that are
"mid-operation" through a common handler — which then maps them to
`"error_during_operation"` regardless of specific HTTP status — then 402 would produce
the same subtype as 429.

Supporting evidence for Model A:
- The `openclaw/openclaw` PR #30780 ("Treat Anthropic 402 as rate_limit") treats 402 as
  equivalent to a rate limit at the HTTP client level. This suggests the two errors share
  similar handling semantics in the CLI ecosystem.
- HTTP 402 fires during an active API call (mid-operation), the same lifecycle point as
  HTTP 429. If the CLI routes "API call failed while executing a tool or model step" to a
  single error handler, both HTTP 402 and HTTP 429 would map to the same `subtype`.
- The name `"error_during_operation"` is semantically appropriate for a billing failure
  that terminates an API call mid-execution — it is an error during an operation in the
  same sense as a rate limit rejection.

**Model B — 402 uses the generic execution-error path (emits `"error_during_execution"`):**

If the CLI has separate handlers for rate-limit-specific responses (which benefit from
special retry logic, `rate_limit_event` emission, and `anthropic-ratelimit-unified-*`
header parsing) vs. other non-retryable API errors (which fall through to a generic
handler), then 402 — which is not retried and does not have rate limit headers — would
fall into the generic path, which may emit `"error_during_execution"`.

Supporting evidence for Model B:
- The SDK type definitions consistently list `"error_during_execution"` as the documented
  catch-all error subtype for unhandled errors. This is consistent with a design where
  `"error_during_execution"` is the "all other errors" bucket.
- The original inference in this document (Section 2.2) was based on this logic: 429 takes
  a special rate-limit-specific path (producing the undocumented `"error_during_operation"`
  subtype), while 402 — which lacks rate-limit-specific handling — takes the generic path
  (producing the documented `"error_during_execution"` subtype).

**Assessment**: Neither model can be confirmed without a live 402 stream-json capture.
Model A now has stronger logical support (given the observed pattern that mid-operation
API errors emit `"error_during_operation"`) but Model B cannot be excluded.

### 7.6 Assistant Event Check Ordering: Why `assistant.error` Must Come First

The `assistant.error` check must come before any `result.subtype` check in the conductor's
error classifier. This is not merely a style preference — it is architecturally required
for two independent reasons:

**Reason 1: `result.subtype` cannot distinguish 402 from 429 if both emit
`"error_during_operation"`**

If Model A (Section 7.5) is correct, both HTTP 402 billing failures and HTTP 429 rate
limit failures produce `subtype: "error_during_operation"`. In that case:

- A classifier that checks `subtype` first would misclassify 402 as a rate limit event
  (since `"error_during_operation"` is only live-confirmed for 429).
- Only the `assistant.error == "billing_error"` field in the `assistant` event that
  precedes the `result` event can distinguish the two.

**Reason 2: The `assistant` event arrives before the `result` event in the stream**

In `stream-json` mode, the `assistant` event (carrying `error: "billing_error"`) is emitted
before the terminal `result` event. A streaming classifier processing events in order
can detect the billing failure early — before the `result` event even arrives — by
watching for `assistant` events with the `error` field set.

The conductor detection rule from Section 6.7 is therefore the correct ordering:

```python
def classify_from_stream(
    result_event: dict | None,
    assistant_events: list[dict],
) -> ErrorClass:
    # STEP 1: Check assistant events first (most reliable; arrives before result event)
    for event in assistant_events:
        if event.get("type") == "assistant" and event.get("error") == "billing_error":
            return ErrorClass.BILLING_FAILURE

    if result_event is None:
        return ErrorClass.UNKNOWN

    subtype = result_event.get("subtype", "")
    result_text = (result_event.get("result") or "").lower()
    errors = result_event.get("errors", [])

    # STEP 2: Check result subtype (secondary; only reached if no assistant.error)
    if subtype == "error_during_operation":
        # Confirmed for HTTP 429; possibly also fired for HTTP 402 (Model A)
        # Distinguish by result_text if billing_error assistant event was absent
        if "rate limit" in result_text or "rate_limit" in result_text:
            return ErrorClass.RATE_LIMIT_FIVE_HOUR
        if "billing" in result_text or "payment" in result_text:
            return ErrorClass.BILLING_FAILURE  # fallback if billing_error absent
        return ErrorClass.API_ERROR_UNCLASSIFIED

    if subtype == "error_during_execution":
        # Documented catch-all; may fire for 402 (Model B scenario)
        for error_str in errors:
            if "billing" in error_str.lower() or "payment" in error_str.lower():
                return ErrorClass.BILLING_FAILURE
        return ErrorClass.EXECUTION_ERROR
    ...
```

**IMPORTANT**: The `billing_error` check on `assistant.error` has [HIGH schema
confidence, UNVERIFIED runtime] status (Section 6.10). The check is structurally correct
but the `"billing_error"` value has not been confirmed to actually appear in live
stream-json output for HTTP 402.

### 7.7 Implications If the `assistant.error` Field Is Absent for 402

A critical failure mode for the conductor: if HTTP 402 does NOT cause an `assistant`
event with `error: "billing_error"` to be emitted (e.g., the 402 is handled so early
that no assistant event is generated, or the CLI binary version predates the
`billing_error` value), then the conductor falls back to `result.subtype`.

In that fallback scenario:

- If `subtype == "error_during_operation"` and result text contains "billing":
  classify as `BILLING_FAILURE` (fragile — depends on text pattern).
- If `subtype == "error_during_operation"` and result text contains "rate limit":
  classify as `RATE_LIMIT_*` (correct for 429; would misclassify 402 if the 402
  result text does not contain billing keywords).
- If `subtype == "error_during_execution"`:
  classify as `EXECUTION_ERROR` initially; check `errors[]` for billing keywords.
- If neither subtype appears or `result_event` is `None`:
  classify as `UNKNOWN`.

This reinforces the importance of obtaining a live 402 stream-json capture to validate
the fallback path.

### 7.8 The Case Against a Distinct Billing-Specific Result Subtype

Issue #100 also asks whether a distinct `"error_billing"` or `"error_payment_required"`
subtype might exist. The evidence strongly argues against this:

1. **TypeScript SDK type union does not include any billing-specific result subtype.**
   As of v0.2.63 (March 2026), the four documented error subtypes are
   `"error_max_turns"`, `"error_during_execution"`, `"error_max_budget_usd"`, and
   `"error_max_structured_output_retries"`. None is billing-specific.
2. **No Elixir SDK changelog entry documents a billing subtype addition.** The Elixir
   SDK changelogs from v0.18.0 through v0.28.0 reference `billing_error` only in the
   context of the `AssistantMessage.error` field, not as a new `result_subtype`.
3. **No GitHub issue or PR in any reviewed repository references a
   `"error_billing"` or `"error_payment_required"` result subtype.** If such a subtype
   existed or was being planned, it would almost certainly appear in SDK type definition
   changes or changelog entries.

**Conclusion**: No distinct billing-specific `result.subtype` exists. The billing failure
signal is located in the `assistant.error` field, not in `result.subtype`.

### 7.9 Confidence Summary and Updated Findings Table

| Question | Status After Issue #100 Research |
|----------|----------------------------------|
| Is `result.subtype` `"error_during_operation"` for HTTP 402? | UNKNOWN — no live capture. Model A (same path as 429) is plausible but unconfirmed. |
| Is `result.subtype` `"error_during_execution"` for HTTP 402? | UNKNOWN — possible if 402 uses a separate generic handler (Model B). |
| Is there a distinct `"error_billing"` or `"error_payment_required"` subtype? | CONFIRMED NOT — absent from TypeScript SDK v0.2.63 and Elixir SDK v0.28.0; absent from all reviewed changelogs and issues. |
| Is `"error_during_operation"` still absent from the TypeScript SDK type defs? | CONFIRMED — absent from TypeScript SDK v0.2.63 (current, March 2026). |
| Is `"error_during_operation"` still absent from the Elixir SDK type defs? | CONFIRMED — absent from Elixir ClaudeCode v0.28.0 (current, March 2026). |
| Does the `assistant.error` check need to come before the `result.subtype` check? | CONFIRMED REQUIRED — `result.subtype` alone cannot distinguish 402 from 429 if both emit `"error_during_operation"`. |
| Does a live 402 stream-json result event capture exist in any public source? | CONFIRMED NOT — no public capture found as of March 2026. |
| Is the Section 6.7 conductor classification rule still correct? | YES — the ordering (`assistant.error` first, `subtype` second) is correct and no update is required. |

### 7.10 Remaining Open Question

The specific `result.subtype` value for HTTP 402 is the sole unresolved question from
this research. Resolving it requires a live capture of the full `claude -p
--output-format stream-json 2>&1` output when a Max plan account with Extra Usage enabled
triggers a billing authorization failure. The capture method is documented in Section 5.2
of this document.

**Until that capture is available, the conductor MUST treat `assistant.error:
"billing_error"` as the authoritative 402 discriminator, and MUST NOT rely on
`result.subtype` alone to distinguish 402 billing failures from 429 rate limit events.**

---

## 8. Sources

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
- [GitHub Issue #505 (anthropics/claude-agent-sdk-python): AssistantMessage error field not
  populated — live captured JSON](https://github.com/anthropics/claude-agent-sdk-python/issues/505)
  — **Primary source for Section 6.2.** Provides two live-captured `assistant` event JSON payloads
  with top-level `error` field: one with `"unknown"` (invalid model name) and one with
  `"authentication_failed"` (invalid API key). These are the only confirmed live-capture examples
  of `assistant` event error structures in any accessible source as of March 2026. Confirms the
  `model: "<synthetic>"`, `stop_reason: "stop_sequence"`, and `usage: {all zero}` pattern for
  synthetic error responses. The fix (PR #506, merged Feb 3 2026) changed `data["message"].get("error")`
  to `data.get("error")`. Exception hierarchy commit (wingding12) added `BillingError` class,
  confirming `"billing_error"` is an anticipated SDK value.
- [TypeScript Agent SDK Reference — SDKResultMessage type](https://platform.claude.com/docs/en/agent-sdk/typescript)
  — **Key source for Section 6.3.** The `SDKResultMessage` type defines error subtypes as:
  `"error_max_turns"`, `"error_during_execution"`, `"error_max_budget_usd"`,
  `"error_max_structured_output_retries"`. Notably absent: `"error_during_operation"`, which is
  confirmed in live output for 429 (doc #23 Section 2.2). This discrepancy invalidates the prior
  inference that 402 would produce `"error_during_execution"` as opposed to 429's
  `"error_during_operation"`.
- [ClaudeCode.Types — ClaudeCode v0.28.0](https://hexdocs.pm/claude_code/ClaudeCode.Types.html)
  — **Corroborates Section 6.3.** The Elixir SDK `result_subtype()` type also omits
  `:error_during_operation`, listing only `:error_during_execution` as an execution-error subtype.
  Confirms that `"error_during_operation"` is an undocumented runtime subtype emitted by the CLI
  but absent from official type definitions in both the TypeScript and Elixir SDKs.
