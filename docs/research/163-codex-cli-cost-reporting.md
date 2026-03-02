# Research: Codex CLI Cost Reporting in NDJSON Output

**Issue:** #163
**Milestone:** v2
**Feature:** feat:llm-alloc
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [NDJSON Terminal Event Schema](#ndjson-terminal-event-schema)
3. [Token Usage Fields in turn.completed](#token-usage-fields-in-turncompleted)
4. [Cost Field Availability](#cost-field-availability)
5. [Comparison with Claude Code Terminal Event](#comparison-with-claude-code-terminal-event)
6. [Cost Extraction Strategy for CodexBackend](#cost-extraction-strategy-for-codexbackend)
7. [Implementation: Cost Ledger Extractor](#implementation-cost-ledger-extractor)
8. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
9. [Sources](#sources)

---

## Executive Summary

This research documents the OpenAI Codex CLI's NDJSON output schema for cost and token
reporting, specifically from `codex exec --json` sessions. The goal is to determine how
conductor's cost ledger can extract per-invocation cost data from `CodexBackend` sessions,
equivalent to Claude Code's `result.total_cost_usd` field.

**Key findings:**

1. **`turn.completed` events include a `usage` object** with `input_tokens`,
   `cached_input_tokens`, and `output_tokens` fields. [DOCUMENTED]

2. **No direct cost field exists** (no `total_cost_usd` equivalent). Cost must be
   calculated from token counts using the GPT-5.3-Codex pricing table. [DOCUMENTED]

3. **The confirmed `turn.completed` schema:**
   ```json
   {"type": "turn.completed", "usage": {"input_tokens": 24763, "cached_input_tokens": 24448, "output_tokens": 122}}
   ```
   [DOCUMENTED — from official Codex non-interactive mode documentation]

4. **Cumulative token counting:** Each event with `payload.type == "token_count"` reports
   cumulative totals; the CLI subtracts previous totals to recover per-turn incremental
   usage. The `turn.completed` `usage` object contains the final cumulative totals.
   [DOCUMENTED]

5. **Cost estimation** from token counts is straightforward using GPT-5.3-Codex pricing:
   ~$1.75/1M input tokens, ~$14.00/1M output tokens (as of March 2026 pricing).

---

## NDJSON Terminal Event Schema

When `codex exec --json` is run, stdout becomes a NDJSON (newline-delimited JSON) stream.
The event sequence is:

```
{"type": "thread.started", ...}
{"type": "turn.started", ...}
{"type": "agent_message", ...}          # zero or more
{"type": "reasoning", ...}             # zero or more (if reasoning model)
{"type": "command_execution", ...}     # zero or more (tool calls)
{"type": "file_change", ...}           # zero or more
{"type": "web_search", ...}            # zero or more
{"type": "turn.completed", "usage": {...}}  # ← terminal event with token usage
```

The **terminal event** is `turn.completed`. It is the Codex CLI equivalent of Claude Code's
`result` event.

---

## Token Usage Fields in turn.completed

### Confirmed schema (from OpenAI Codex non-interactive mode documentation)

```json
{
  "type": "turn.completed",
  "usage": {
    "input_tokens": 24763,
    "cached_input_tokens": 24448,
    "output_tokens": 122
  }
}
```

### Field descriptions

| Field | Type | Description |
|-------|------|-------------|
| `input_tokens` | integer | Total input tokens consumed (includes system prompt, context, tool results) |
| `cached_input_tokens` | integer | Subset of `input_tokens` that were served from cache (prompt caching) |
| `output_tokens` | integer | Total output tokens generated |

**Derived fields (calculated by conductor):**

| Derived | Formula |
|---------|---------|
| `non_cached_input_tokens` | `input_tokens - cached_input_tokens` |
| `billable_input_tokens` | Depends on whether cached tokens are billed at reduced rate |
| `estimated_cost_usd` | See Section 5 |

### turn.failed terminal event

When a Codex session fails, the terminal event is `turn.failed`:

```json
{
  "type": "turn.failed",
  "error": "string describing the failure",
  "usage": {
    "input_tokens": 5000,
    "cached_input_tokens": 0,
    "output_tokens": 100
  }
}
```

`usage` is present on `turn.failed` as well, enabling cost accounting even for failed runs.
[INFERRED — consistent with Codex CLI's token count tracking architecture; not
explicitly confirmed for failure case]

---

## Cost Field Availability

**There is no `total_cost_usd` field in Codex CLI NDJSON output.** This is a known gap
documented in GitHub Issue #5085 ("Cost Tracking & Usage Analytics") and Issue #1047
("Display the current session's cumulative token usage").

The community has also filed Issue #6113 ("Token usage spikes too quickly in Codex CLI
sessions") which confirms that token usage is tracked but cost is not surfaced in the CLI.

**As of March 2026:** No cost field has been added to the `turn.completed` event. Cost
must be estimated by conductor from token counts.

---

## Comparison with Claude Code Terminal Event

| Field | Claude Code `result` event | Codex CLI `turn.completed` | Available? |
|-------|---------------------------|---------------------------|-----------|
| Event type | `result` | `turn.completed` | Different names |
| Cost (USD) | `result.total_cost_usd` | *(not available)* | Claude Code only |
| Input tokens | `result.usage.input_tokens` | `usage.input_tokens` | Both |
| Cached tokens | `result.usage.cache_read_input_tokens` | `usage.cached_input_tokens` | Both (different names) |
| Output tokens | `result.usage.output_tokens` | `usage.output_tokens` | Both |
| Error status | `result.subtype == "error"` | `turn.failed` (separate event type) | Both (different patterns) |
| Session cost | Exact (from billing API) | Estimated (from token counts) | Claude Code is exact |

**Key difference:** Claude Code's `result.total_cost_usd` is provided by Anthropic's
billing infrastructure, making it exact. Codex CLI requires conductor to estimate cost
from token counts, which is an approximation.

---

## Cost Extraction Strategy for CodexBackend

### Pricing reference (GPT-5.3-Codex, March 2026)

From the OpenAI Codex Pricing documentation:

| Token type | Price (per 1M tokens) |
|-----------|----------------------|
| Input (non-cached) | $1.75 |
| Input (cached) | $0.44 (75% discount) |
| Output | $14.00 |

**Cost formula:**
```
cost_usd = (
    (input_tokens - cached_input_tokens) * 1.75 / 1_000_000 +
    cached_input_tokens * 0.44 / 1_000_000 +
    output_tokens * 14.00 / 1_000_000
)
```

### Fallback strategy if pricing table changes

Conductor should maintain a `MODEL_PRICING` table in config:

```toml
# conductor.toml
[backends.codex.pricing]
model = "gpt-5.3-codex"
input_per_1m = 1.75
cached_input_per_1m = 0.44
output_per_1m = 14.00
```

If pricing changes, update the config table without code changes.

---

## Implementation: Cost Ledger Extractor

```python
from dataclasses import dataclass

@dataclass
class CodexUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    model: str
    estimated_cost_usd: float

# Pricing table (updated from conductor.toml)
CODEX_PRICING = {
    "gpt-5.3-codex": {
        "input_per_1m": 1.75,
        "cached_input_per_1m": 0.44,
        "output_per_1m": 14.00,
    }
}

def extract_codex_usage(terminal_event: dict, model: str = "gpt-5.3-codex") -> CodexUsage:
    """Extract usage and estimate cost from a Codex turn.completed event."""
    assert terminal_event.get("type") in ("turn.completed", "turn.failed")

    usage = terminal_event.get("usage", {})
    input_tokens = usage.get("input_tokens", 0)
    cached_input_tokens = usage.get("cached_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)

    pricing = CODEX_PRICING.get(model, CODEX_PRICING["gpt-5.3-codex"])
    non_cached = input_tokens - cached_input_tokens

    cost = (
        non_cached * pricing["input_per_1m"] / 1_000_000 +
        cached_input_tokens * pricing["cached_input_per_1m"] / 1_000_000 +
        output_tokens * pricing["output_per_1m"] / 1_000_000
    )

    return CodexUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        model=model,
        estimated_cost_usd=round(cost, 6),
    )

def parse_codex_ndjson_stream(stream: Iterable[str]) -> tuple[list[ConductorEvent], CodexUsage | None]:
    """Parse a Codex --json NDJSON stream into ConductorEvents."""
    events = []
    usage = None

    for line in stream:
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)

        event_type = event.get("type", "")

        if event_type == "thread.started":
            events.append(ConductorEvent(type="init", data=event))
        elif event_type == "agent_message":
            events.append(ConductorEvent(type="text", data=event))
        elif event_type == "command_execution":
            events.append(ConductorEvent(type="tool_call", data=event))
        elif event_type in ("turn.completed", "turn.failed"):
            is_error = event_type == "turn.failed"
            usage = extract_codex_usage(event)
            events.append(ConductorEvent(
                type="error" if is_error else "done",
                data={"usage": usage, "error": event.get("error") if is_error else None}
            ))
        else:
            events.append(ConductorEvent(type="unknown", data=event))

    return events, usage
```

---

## Follow-Up Research Recommendations

**[WONT_RESEARCH] Official cost field in Codex CLI NDJSON**
GitHub Issue #5085 requests a cost field. If Anthropic adds it, conductor's CodexBackend
cost ledger extractor can be simplified. Monitor the issue; update `163-codex-cli-cost-
reporting.md` when resolved. No action until then — estimation strategy is sufficient.

**[V2_RESEARCH] Verify cached_input_tokens pricing for GPT-5.3-Codex**
The 75% cache discount ($0.44/1M) is from March 2026 OpenAI pricing. Verify this is
the correct discount rate for the Codex-specific model variant. Update pricing table
in `conductor.toml` if different.

---

## Sources

- [OpenAI Codex Non-Interactive Mode Documentation](https://developers.openai.com/codex/noninteractive/)
- [OpenAI Codex CLI Command Reference](https://developers.openai.com/codex/cli/reference/)
- [Codex Pricing](https://developers.openai.com/codex/pricing/)
- [Issue #5085: Cost Tracking & Usage Analytics](https://github.com/openai/codex/issues/5085)
- [Issue #1047: Display cumulative token usage](https://github.com/openai/codex/issues/1047)
- [Issue #6113: Token usage spikes too quickly](https://github.com/openai/codex/issues/6113)
- [OpenAI Codex CLI GitHub Repository](https://github.com/openai/codex)
- [Unrolling the Codex Agent Loop — OpenAI](https://openai.com/index/unrolling-the-codex-agent-loop/)
- [ccusage: Codex CLI Overview](https://ccusage.com/guide/codex/)
