# Research: Multi-Model Backend Support (Gemini, GPT-4.1, Grok)

**Issue:** #36
**Milestone:** v2
**Feature:** feat:llm-alloc
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [State of Coding CLIs by Model Family](#state-of-coding-clis-by-model-family)
   - [Gemini CLI](#gemini-cli)
   - [OpenAI Codex CLI](#openai-codex-cli)
   - [xAI Grok CLI](#xai-grok-cli)
3. [Stream-JSON Output Compatibility](#stream-json-output-compatibility)
4. [Minimum Viable ModelBackend Protocol](#minimum-viable-modelbackend-protocol)
5. [Build-vs-Buy Tradeoff for Tool-Use Scaffolding](#build-vs-buy-tradeoff-for-tool-use-scaffolding)
6. [Cost/Quality Comparison for Code Generation](#costquality-comparison-for-code-generation)
7. [Recommended v2 Architecture](#recommended-v2-architecture)
8. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
9. [Sources](#sources)

---

## Executive Summary

As of March 2026, **three coding agent CLIs exist** comparable to `claude -p`: Gemini CLI
(Google, open-source), OpenAI Codex CLI (OpenAI, open-source), and an emerging official
Grok CLI from xAI (not yet publicly released). All three support headless/non-interactive
modes with structured output, but their output schemas differ from Claude Code's
`stream-json` NDJSON format — requiring a per-backend adapter layer.

**Key findings:**

1. **Gemini CLI** is the most mature alternative. It supports `--output-format stream-json`
   (NDJSON), built-in tools (Bash, file ops, web fetch), MCP integration, and a free tier.
   Its headless mode closely mirrors `claude -p --output-format stream-json`, making it the
   strongest candidate for a v2 adapter. [DOCUMENTED]

2. **OpenAI Codex CLI** supports `--json` NDJSON output and is capable of CI/headless
   deployments. Its event schema differs from Claude Code's (different event type names,
   different field structure). [DOCUMENTED]

3. **xAI Grok CLI** is not yet publicly released from xAI as of March 2026. Community
   implementations exist (superagent-ai/grok-cli) but are unofficial. An official release
   is confirmed but has no public date. **Grok should not be targeted for v2.** [DOCUMENTED]

4. **A `ModelBackend` protocol** (adapter pattern) with a minimal interface — `spawn(prompt)
   → AsyncIterator[BaseEvent]` — can unify all three CLIs. The adapter's job is to map
   backend-specific event schemas to a conductor-internal event type.

5. **Build-vs-buy**: Using existing CLIs (Gemini CLI, Codex CLI) via subprocess is the
   correct v2 approach. Building raw API tool-use scaffolding from scratch is out of scope
   for v2.

6. **Cost/quality**: Claude Sonnet 4.6 remains the best cost-quality option for conductor's
   workloads. Gemini 3.1 Pro is a viable lower-cost alternative for large-context tasks.
   GPT-5.3-Codex leads on terminal and multi-language tasks.

---

## State of Coding CLIs by Model Family

### Gemini CLI

**Status:** Actively maintained, production-ready, open-source [DOCUMENTED]

Google's [Gemini CLI](https://github.com/google-gemini/gemini-cli) is a terminal-based
coding agent using a ReAct (reason-act) loop. Key capabilities:

| Feature | Status |
|---------|--------|
| Headless / non-interactive mode | Yes (`gemini -p "..."` or pipe) |
| Structured JSON output | `--output-format json` (batch) or `--output-format stream-json` (NDJSON) |
| Built-in tools | Bash, file read/write, web fetch, Google Search |
| MCP support | Yes (local and remote MCP servers) |
| Git operations | Via Bash tool |
| Worktree isolation | Via CWD control (no native flag; inherits process CWD like `claude -p`) |
| Auth | Google account OAuth (free tier: 60 req/min, 1000 req/day) or API key |
| Context window | 1M tokens (Gemini 2.5 Pro / 3.1 Pro) |

**Headless mode trigger:** Gemini CLI enters headless mode when stdin is not a TTY or when
a query is supplied as a positional argument. No `--dangerously-skip-permissions` equivalent
is required — tool execution proceeds without confirmation prompts in headless mode.

**Known issue with JSON output:** There is an open GitHub issue (#9009) noting that despite
documentation, JSON output in some builds does not match the documented schema. The NDJSON
`stream-json` format is more reliable. [INFERRED from community reports]

**Event schema differences from `claude -p`:** Gemini CLI's NDJSON events use different
type names (e.g., `content`, `tool_call`, `tool_result` rather than Claude Code's
`assistant`, `user`, `result`). A conductor adapter must map Gemini's schema to the
conductor-internal event model.

---

### OpenAI Codex CLI

**Status:** Production-ready, open-source, CI-focused [DOCUMENTED]

OpenAI's [Codex CLI](https://github.com/openai/codex) (written in Rust) is designed for
terminal and CI/CD coding automation. Key capabilities:

| Feature | Status |
|---------|--------|
| Headless / non-interactive mode | Yes (`codex exec "..."`) |
| Structured JSON output | `--json` flag produces NDJSON |
| Built-in tools | Bash, file ops, web search, MCP tool calls |
| MCP support | Yes |
| Auth | API key (`OPENAI_API_KEY`) or subscription OAuth |
| Context window | Model-dependent (GPT-5.3-Codex: ~128K tokens) |

**NDJSON event types** (from Codex CLI `--json` output):
- `thread.started`
- `turn.started` / `turn.completed` / `turn.failed`
- `agent_message`, `reasoning`, `command_execution`, `file_change`, `web_search`

These differ substantially from Claude Code's `system/init`, `assistant`, `user`, `result`
schema. An adapter must translate Codex events into conductor's internal format.

**Important limitation:** The `codex exec` subcommand for headless use is the supported
path. There is an open issue (#4219) requesting full headless orchestration support,
suggesting the current headless mode is functional but not yet first-class. [DOCUMENTED]

---

### xAI Grok CLI

**Status:** Not yet publicly released by xAI as of March 2026 [DOCUMENTED]

xAI has confirmed an official Grok CLI is in development. The package name
`@xai-official/grok` is referenced in documentation but the npm package is not live.
Community implementations (superagent-ai/grok-cli) are unofficial weekend projects.

**xAI has released `grok-code-fast-1`** — a model optimized for agentic coding workflows
supporting parallel tool calling, multimodal inputs, and extended context. However, this
model is currently accessible only via the Grok API, not via a CLI agent.

**Recommendation:** Do not target Grok CLI for v2. File a v3 research issue once the
official CLI is available.

---

## Stream-JSON Output Compatibility

The three CLIs all produce some form of NDJSON output, but with incompatible schemas:

| Feature | Claude Code | Gemini CLI | Codex CLI |
|---------|-------------|------------|-----------|
| NDJSON flag | `--output-format stream-json` | `--output-format stream-json` | `--json` |
| Init event | `system/init` | (none documented) | `thread.started` |
| Turn event | `assistant` | `content` | `agent_message` |
| Tool call | embedded in `assistant` content | `tool_call` | `command_execution` |
| Tool result | embedded in `user` content | `tool_result` | (varies) |
| Terminal event | `result` (has `total_cost_usd`, `usage`) | `statistics` | `turn.completed` |
| Cost field | `result.total_cost_usd` | `statistics.total_tokens` | not documented |

The conductor's current stream parser (see `05-logging-observability.md` for schema details)
is tightly coupled to Claude Code's event schema. Supporting other backends requires
either: (a) per-backend adapters that normalize events to a conductor-internal schema, or
(b) a lowest-common-denominator output protocol that all adapters emit.

---

## Minimum Viable ModelBackend Protocol

A `ModelBackend` abstract protocol for conductor:

```python
from typing import AsyncIterator, Protocol
from dataclasses import dataclass

@dataclass
class ConductorEvent:
    """Conductor-internal normalized event."""
    type: str  # "init", "text", "tool_call", "tool_result", "done", "error"
    data: dict

class ModelBackend(Protocol):
    """Minimum interface any coding agent CLI backend must implement."""

    async def spawn(
        self,
        prompt: str,
        *,
        cwd: str,
        env: dict[str, str],
        allowed_tools: list[str] | None = None,
        max_budget_usd: float | None = None,
    ) -> AsyncIterator[ConductorEvent]:
        """
        Spawn the backend, stream normalized events, and close.

        Implementations wrap their CLI subprocess and translate backend-specific
        NDJSON schemas into ConductorEvent instances.
        """
        ...

    async def is_available(self) -> bool:
        """Check if the backend CLI is installed and authenticated."""
        ...

    @property
    def name(self) -> str:
        """Backend identifier: 'claude', 'gemini', 'codex'."""
        ...

    @property
    def model(self) -> str:
        """Default model ID for this backend."""
        ...
```

**Concrete adapter structure:**

```
src/composer/backends/
    __init__.py
    base.py          # ConductorEvent, ModelBackend Protocol
    claude.py        # ClaudeBackend: wraps claude -p --output-format stream-json
    gemini.py        # GeminiBackend: wraps gemini --output-format stream-json
    codex.py         # CodexBackend: wraps codex exec --json
    registry.py      # Backend registry and selection logic
```

**Runner integration:** The conductor `runner.py` currently passes `claude -p` subprocess
args directly. In v2, `runner.py` accepts a `ModelBackend` instance and delegates to
`backend.spawn(...)`. The backend wraps the subprocess and yields `ConductorEvent` objects.

---

## Build-vs-Buy Tradeoff for Tool-Use Scaffolding

**Question:** If a model has no dedicated coding CLI, should conductor build its own tool-use
scaffolding (Bash, Read, Edit, Write) over the raw API?

**Recommendation: Buy (use existing CLIs) for v2. Do not build raw tool-use scaffolding.**

Rationale:

1. **Scope creep**: Building and maintaining a tool-use scaffolding layer is a significant
   engineering investment. Each tool (Bash isolation, file permissions, git operations)
   needs sandboxing and error handling that Claude Code and Gemini CLI have already solved.

2. **Existing coverage**: Gemini CLI and Codex CLI both handle the same coding tasks
   conductor needs. There is no gap that justifies building from scratch.

3. **v3 consideration**: If conductor needs a model with no CLI (e.g., a fine-tuned coding
   model via a private API), building a minimal tool-use layer would be appropriate. That
   is a v3 scope item, not v2.

**Exception:** The credential proxy pattern (`43-anthropic-key-proxy.md`) applies to
any backend. Conductor should proxy API credentials regardless of which CLI backend is
used.

---

## Cost/Quality Comparison for Code Generation

As of March 2026 (per independent benchmarks):

| Model | CLI | Input price (per 1M tokens) | Output price (per 1M tokens) | SWE-bench Verified | Context |
|-------|-----|-----------------------------|-------------------------------|---------------------|---------|
| Claude Sonnet 4.6 | claude -p | $3.00 | $15.00 | ~77% | 200K |
| Claude Opus 4.6 | claude -p | $5.00 | $25.00 | ~81% | 200K |
| Gemini 3.1 Pro | gemini | $1.25 | $10.00 | ~65% | 1M |
| GPT-5.3-Codex | codex exec | $1.75 | $14.00 | ~70% | 128K |
| Haiku 4.5 | claude -p | $1.00 | $5.00 | ~50% | 200K |

**Cost per conductor sub-agent task** (estimate: 100K input + 20K output tokens):

| Model | Estimated cost |
|-------|---------------|
| Claude Sonnet 4.6 | ~$0.60 |
| Claude Opus 4.6 | ~$1.00 |
| Gemini 3.1 Pro | ~$0.33 |
| GPT-5.3-Codex | ~$0.45 |
| Haiku 4.5 | ~$0.20 |

**Key insight:** For Pro/Max subscription users, per-token API cost is irrelevant — the
binding constraint is the 5-hour usage window (see `08-usage-scheduling.md`). The
multi-model backend value for subscription users is **capacity sharing**: Gemini CLI
tasks draw from Google's usage pool, not Anthropic's. This is the primary v2 motivation
for multi-model support — not cost reduction, but quota relief.

For API-key users, Gemini 3.1 Pro offers ~45% cost savings vs. Sonnet 4.6 at meaningful
quality cost (~12pp SWE-bench gap).

---

## Recommended v2 Architecture

**Recommendation: Plugin/adapter pattern with lazy registration.**

1. **`ModelBackend` protocol** (defined in `backends/base.py`) — minimum interface
2. **Backend registry** — maps `backend_name` string to `ModelBackend` implementation;
   backends are registered via `pyproject.toml` entry points (similar to how conductor
   entry points are already structured per `CLAUDE.md`)
3. **Conductor config** (`conductor.toml` or env var `CONDUCTOR_BACKEND`) — selects
   active backend; defaults to `claude`
4. **Per-issue backend override** — research issues can specify
   `conductor-backend: gemini` in the issue body to use Gemini CLI for that task

**v2 scope:** Implement `ClaudeBackend` (refactor of current runner) and `GeminiBackend`.
Leave `CodexBackend` for v2.1 or v3 unless capacity pressure demands it sooner.

**Not in scope for v2:** Raw API tool-use scaffolding, cross-model result merging (see #37
for consensus protocol), Grok CLI integration.

---

## Follow-Up Research Recommendations

**[WONT_RESEARCH] Grok CLI integration for v2**
xAI's official Grok CLI is not publicly available. No action until release. File a new
issue when xAI publishes the CLI.

**[V2_RESEARCH] Gemini CLI tool allowlist and permission model**
Gemini CLI's headless tool execution model differs from `claude -p --allowedTools`. What
tools are available, how are they scoped, and does Gemini CLI support a deny list
equivalent to `--disallowedTools`? This is needed before `GeminiBackend` can enforce
module isolation.

**[V2_RESEARCH] Codex CLI cost reporting in NDJSON output**
The `codex exec --json` output does not document a cost or token-count field equivalent
to Claude Code's `result.total_cost_usd`. The conductor cost ledger (see
`05-logging-observability.md`) requires per-invocation cost data. Can cost be inferred
from the OpenAI API response headers or must it be estimated?

**[WONT_RESEARCH] Build raw tool-use scaffolding from API for v2**
Scope is too large and not needed given existing CLI coverage. Defer to v3 or later.

---

## Sources

- [Gemini CLI GitHub Repository](https://github.com/google-gemini/gemini-cli)
- [Google Blog: Introducing Gemini CLI](https://blog.google/innovation-and-ai/technology/developers-tools/introducing-gemini-cli-open-source-ai-agent/)
- [Gemini CLI Headless Mode Reference](https://geminicli.com/docs/cli/headless/)
- [Gemini CLI Issue #9009: JSON output schema mismatch](https://github.com/google-gemini/gemini-cli/issues/9009)
- [Gemini CLI Issue #9281: headless with JSON output exits on non-fatal tool errors](https://github.com/google-gemini/gemini-cli/issues/9281)
- [OpenAI Codex CLI GitHub Repository](https://github.com/openai/codex)
- [OpenAI Codex CLI Non-Interactive Mode](https://developers.openai.com/codex/noninteractive/)
- [OpenAI Codex CLI Command Reference](https://developers.openai.com/codex/cli/reference/)
- [Codex CLI Issue #4219: Add headless/non-interactive mode](https://github.com/openai/codex/issues/4219)
- [xAI: Grok Code Fast 1](https://x.ai/news/grok-code-fast-1)
- [xAI Grok CLI coming to compete with Claude and ChatGPT - EONMSK News](https://www.eonmsk.com/2026/02/25/xai-confirms-grok-cli-is-coming-to-compete-with-claude-and-chatgpt/)
- [AI API Pricing Comparison 2026: Grok vs Gemini vs GPT vs Claude — IntuitionLabs](https://intuitionlabs.ai/articles/ai-api-pricing-comparison-grok-gemini-openai-claude)
- [Codex vs Claude vs Gemini Coding Benchmark 2026 — Iterathon](https://iterathon.tech/blog/gpt-codex-vs-claude-sonnet-vs-gemini-coding-benchmark-2026)
- [Best LLM for Coding 2026: Opus 4.6 vs GPT-5.3-Codex vs Gemini 3 — SmartScope](https://smartscope.blog/en/generative-ai/chatgpt/llm-coding-benchmark-comparison-2026/)
- [Implementing an LLM Agnostic Architecture — Entrio](https://www.entrio.io/blog/implementing-llm-agnostic-architecture-generative-ai-module)
- [Moonshot AI Kosong: LLM Abstraction Layer — MarkTechPost](https://www.marktechpost.com/2025/11/10/moonshot-ai-releases-kosong-the-llm-abstraction-layer-that-powers-kimi-cli/)
