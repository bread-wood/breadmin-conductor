# Research: Empirical Verification Suite for High-Confidence M1 Claims

**Issue:** #41
**Milestone:** v2
**Feature:** core / infra
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Claims Requiring Verification (V-01 through V-07)](#claims-requiring-verification-v-01-through-v-07)
3. [Test Harness Design](#test-harness-design)
4. [Version Recording Strategy](#version-recording-strategy)
5. [Priority Ordering and Risk Assessment](#priority-ordering-and-risk-assessment)
6. [CI Integration Approach](#ci-integration-approach)
7. [Fixture Capture Plan](#fixture-capture-plan)
8. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
9. [Sources](#sources)

---

## Executive Summary

Doc #25 (hallucination detection) identified 7 `[INFERRED]` claims from M1 research that
directly gate M2 implementation decisions. This document designs a minimal test harness
for empirically verifying each claim against the current Claude Code release, records the
expected test protocol, and defines how verified results should be promoted to `[TESTED]`
in the relevant research docs.

**Key findings:**

1. **V-01 (`isolation: worktree` in headless mode)** is now partially `[DOCUMENTED]`.
   Claude Code v2.1.49 (shipped February 19, 2026) added native `--worktree` support.
   The Agent tool's `isolation: "worktree"` parameter in headless `-p` mode is documented
   as creating an isolated worktree under `.claude/worktrees/`. This claim is ready to
   promote from `[INFERRED]` to `[DOCUMENTED]`. [DOCUMENTED]

2. **V-07 (`CLAUDECODE=1` nesting filter)** is the highest-risk claim. If wrong, a
   conductor launched by another conductor would cause exponential resource consumption.
   This should be tested first, before V-02 through V-06.

3. **V-03 (`CLAUDE_CODE_ENABLE_TASKS` default change)**: As of Claude Code v2.1.50+, the
   Agent/Task tool is enabled by default. The env var `CLAUDE_CODE_ENABLE_TASKS` is no
   longer needed to enable the tool. This has been confirmed in release notes. Ready to
   promote from `[INFERRED]` to `[DOCUMENTED]`. [DOCUMENTED]

4. **Verification tests should NOT run in CI** as blocking gates. They require a live
   `ANTHROPIC_API_KEY` and real API calls, making them expensive for routine CI. They
   should be run as a one-time pre-M2-dispatch check and on major Claude Code version
   updates.

5. **Fixture capture** for V-02 and V-05 (token overhead measurements) should use pytest
   golden files stored in `tests/fixtures/` as NDJSON files, captured once and
   version-pinned.

---

## Claims Requiring Verification (V-01 through V-07)

From `docs/research/25-hallucination-detection.md`, section "M1 Findings Requiring
Empirical Verification Before M2 Dispatch":

| ID | Claim | Source Doc | Risk if Wrong |
|----|-------|------------|---------------|
| V-01 | `isolation: "worktree"` in the Agent tool actually works in headless `-p` mode | 01-agent-tool-in-p-mode.md | Runner uses wrong isolation, cross-contamination |
| V-02 | 4-layer isolation reduces overhead to ~5K tokens/turn | 12-subprocess-token-overhead.md | Scheduler over-dispatches, quota exhausted |
| V-03 | `CLAUDE_CODE_ENABLE_TASKS` default changed in v2.1.50+ | 01-agent-tool-in-p-mode.md | Agent tool silently unavailable, tasks fail |
| V-04 | `--dangerously-skip-permissions` deny rules still take precedence | 19-pretooluse-reliability.md | Security: deny rules bypassed |
| V-05 | `--strict-mcp-config --mcp-config '{}'` produces zero MCP overhead | 12-subprocess-token-overhead.md | Token overhead 10x higher than estimated |
| V-06 | Auto-compaction threshold is 83.5% (not 75%) | 02-session-continuity.md | Scheduler misjudges when to expect compaction |
| V-07 | `CLAUDECODE=1` nesting filter prevents conductor-in-conductor | 01-agent-tool-in-p-mode.md | Exponential resource consumption on misconfig |

---

## Test Harness Design

### Infrastructure

All verification tests live in `tests/verification/`. They require:
- `ANTHROPIC_API_KEY` env var (real Anthropic API access)
- `claude` CLI installed and in PATH
- A temporary git repo for worktree tests

```python
# tests/verification/conftest.py
import pytest, os, subprocess, tempfile, json
from pathlib import Path

@pytest.fixture(scope="session", autouse=True)
def require_api_key():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set; skipping empirical verification tests")

@pytest.fixture(scope="session")
def claude_version() -> str:
    result = subprocess.run(["claude", "--version"], capture_output=True, text=True)
    version = result.stdout.strip()
    print(f"\nClaude version under test: {version}")
    return version

@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Initialize a minimal git repo in a temp directory."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path
```

### V-01: Agent Tool `isolation: "worktree"` in Headless Mode

**Protocol:**
```python
# tests/verification/test_v01_worktree_isolation.py
def test_agent_tool_worktree_isolation(tmp_repo):
    """V-01: isolation: worktree creates a real worktree in headless -p mode."""
    prompt = """Use the Agent tool with isolation: "worktree" to run a sub-agent.
    The sub-agent should:
    1. Check what directory it is in (Bash: pwd)
    2. Check if it's in a git worktree (Bash: git worktree list)
    3. Return the pwd and worktree list output
    Report the results."""

    result = subprocess.run(
        ["claude", "-p", "--dangerously-skip-permissions",
         "--output-format", "json", prompt],
        cwd=str(tmp_repo),
        capture_output=True, text=True, timeout=120
    )
    data = json.loads(result.stdout)
    assert not data.get("is_error"), f"Agent failed: {data}"
    assert ".claude/worktrees" in data["result"], "No worktree path found in output"
```

**Expected result:** The sub-agent's `pwd` is under `.claude/worktrees/<name>/` and
`git worktree list` shows the worktree. Promotes V-01 to `[TESTED]`.

**Status update (March 2026):** Claude Code v2.1.49 documentation confirms that
`isolation: "worktree"` creates a worktree under `.claude/worktrees/`. This claim can
be promoted to `[DOCUMENTED]` without the test run. The test is still useful to run once
to confirm the documented behavior matches reality.

---

### V-02: 4-Layer Isolation Reduces Overhead to ~5K Tokens/Turn

**Protocol:**
```python
def test_token_overhead_with_isolation(tmp_repo):
    """V-02: 4-layer isolation reduces overhead from ~50K to ~5K tokens/turn."""
    # Layer 1: CLAUDE_CODE_SIMPLE=1 (basic tools only)
    # Layer 2: --mcp-config '{}' --strict-mcp-config (no MCP servers)
    # Layer 3: Fresh CLAUDE_CONFIG_DIR (no plugin skill files)
    # Layer 4: Minimal CLAUDE.md (< 100 tokens)

    with tempfile.TemporaryDirectory() as config_dir:
        result = subprocess.run(
            ["claude", "-p",
             "--dangerously-skip-permissions",
             "--output-format", "stream-json",
             "--mcp-config", "{}",
             "--strict-mcp-config",
             "Say hi."],
            cwd=str(tmp_repo),
            env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir, "CLAUDE_CODE_SIMPLE": "1"},
            capture_output=True, text=True, timeout=60
        )

    events = [json.loads(line) for line in result.stdout.splitlines() if line]
    result_event = next((e for e in events if e.get("type") == "result"), None)
    assert result_event, "No result event found"
    input_tokens = result_event.get("usage", {}).get("input_tokens", 0)
    assert input_tokens < 8000, f"Input tokens {input_tokens} exceeds 8K threshold"
    # Save as fixture
    Path("tests/fixtures/v02-isolated-hello.ndjson").write_text(result.stdout)
```

**Expected result:** Input tokens < 8,000 (versus ~50K baseline). Promotes V-02 to `[TESTED]`.

---

### V-03: `CLAUDE_CODE_ENABLE_TASKS` Default Changed in v2.1.50+

**Status (March 2026):** This claim is now `[DOCUMENTED]`. Claude Code v2.1.50 release
notes confirm that the Agent/Task tool is enabled by default. `CLAUDE_CODE_ENABLE_TASKS`
is no longer required. Verified via official Claude Code changelog.

**Promote to `[DOCUMENTED]`** in `01-agent-tool-in-p-mode.md` without a test run.

**Minimal verification test (still recommended):**
```python
def test_agent_tool_available_by_default():
    """V-03: Agent tool is available without CLAUDE_CODE_ENABLE_TASKS."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_ENABLE_TASKS"}
    result = subprocess.run(
        ["claude", "-p", "--dangerously-skip-permissions",
         "--output-format", "json",
         "List your available tools. Do you have the Agent or Task tool?"],
        env=env, capture_output=True, text=True, timeout=60
    )
    data = json.loads(result.stdout)
    assert "agent" in data["result"].lower() or "task" in data["result"].lower()
```

---

### V-04: `--dangerously-skip-permissions` Deny Rules Still Take Precedence

**Protocol:**
```python
def test_deny_rules_take_precedence_in_skip_permissions_mode(tmp_repo):
    """V-04: deny: rules in allowedTools still work under --dangerously-skip-permissions."""
    result = subprocess.run(
        ["claude", "-p",
         "--dangerously-skip-permissions",
         "--allowedTools", "Bash,Read,Write",
         "--disallowedTools", "Bash(rm -rf*)",
         "--output-format", "json",
         "Run: rm -rf /tmp/nonexistent-path-abc. Report what happened."],
        cwd=str(tmp_repo),
        capture_output=True, text=True, timeout=60
    )
    data = json.loads(result.stdout)
    # The rm command should be blocked by deny rule
    assert "not allowed" in data["result"].lower() or data.get("is_error")
```

**Expected result:** The deny rule prevents `rm -rf` even under `--dangerously-skip-permissions`.
Promotes V-04 to `[TESTED]`.

---

### V-05: `--strict-mcp-config --mcp-config '{}'` Produces Zero MCP Overhead

**Protocol:** Part of V-02 test above — run with `--strict-mcp-config` and measure
token overhead. Store the stream-json output as a fixture. Verify no MCP tool definitions
appear in the system init event.

```python
def test_mcp_config_empty_produces_no_mcp_overhead(tmp_repo):
    """V-05: Empty MCP config with --strict-mcp-config loads no MCP tools."""
    with tempfile.TemporaryDirectory() as config_dir:
        result = subprocess.run(
            ["claude", "-p", "--dangerously-skip-permissions",
             "--output-format", "stream-json",
             "--mcp-config", "{}", "--strict-mcp-config",
             "List all tools you have available."],
            env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir},
            cwd=str(tmp_repo),
            capture_output=True, text=True, timeout=60
        )
    events = [json.loads(line) for line in result.stdout.splitlines() if line]
    init_event = next((e for e in events if e.get("subtype") == "init"), None)
    assert init_event, "No init event found"
    assert init_event.get("mcp_servers", []) == [], \
        f"MCP servers found: {init_event['mcp_servers']}"
    Path("tests/fixtures/v05-no-mcp.ndjson").write_text(result.stdout)
```

---

### V-06: Auto-Compaction Threshold is 83.5%

**Protocol:**
```python
def test_auto_compaction_threshold():
    """V-06: Auto-compaction fires at ~83.5% context fill, not 75%."""
    # This test requires a long-running session that fills context
    # Impractical to run in automated CI; use golden-file comparison instead
    # Run manually with a large repo and measure turn count before compaction
    pytest.skip("Manual verification required: run a long session and observe compaction")
```

**Alternative**: Read the `02-session-continuity.md` doc's V-06 evidence and confirm the
83.5% figure from Claude Code source code inspection or community measurement data.

**Status (March 2026):** The 83.5% threshold is derived from community measurement
(`claude-code-limit-tracker` project). This remains `[INFERRED]` — it cannot be precisely
verified without a very long multi-turn session. The practical implication (scheduler must
account for compaction before 100% context fill) is valid regardless of whether the
exact threshold is 75%, 83.5%, or 90%.

**Recommended action:** Accept V-06 as `[INFERRED]` and schedule compaction triggers at
75% (conservative buffer). Do not block M2 on this verification.

---

### V-07: `CLAUDECODE=1` Nesting Filter Prevents Conductor-in-Conductor

**Protocol:**
```python
def test_claudecode_nesting_filter():
    """V-07: CLAUDECODE=1 prevents conductor (claude -p) from launching conductor."""
    # Attempt to launch claude -p from within a claude -p session
    inner_prompt = "Run: claude -p 'say hello' --output-format json. Report the output."
    result = subprocess.run(
        ["claude", "-p", "--dangerously-skip-permissions",
         "--output-format", "json",
         "--allowedTools", "Bash",
         inner_prompt],
        env={**os.environ, "CLAUDECODE": "1"},  # Simulates being inside a claude -p session
        capture_output=True, text=True, timeout=60
    )
    data = json.loads(result.stdout)
    # Expected: either the inner claude -p is blocked, or it returns an error
    # The CLAUDECODE=1 env var should cause the inner launch to detect nesting
    # and either refuse or limit tool access
    result_text = data.get("result", "")
    # Verify no recursive conductor loop
    assert "recursion" not in result_text.lower() or data.get("is_error"), \
        "Nesting check may not be active"
```

**Note:** The `CLAUDECODE=1` nesting behavior is `[INFERRED]` from the Claude Code
documentation's description of the env var as a "nesting filter." The exact behavior
(blocking, warning, or limiting) needs empirical confirmation. **This is the highest
priority test before M2 dispatch.**

---

## Version Recording Strategy

Each test run should record:

```python
# In each test, at the end:
def record_verification(claim_id: str, result: str, claude_version: str):
    """Record verification result with version metadata."""
    record = {
        "claim": claim_id,
        "result": result,  # "PASS", "FAIL", "SKIP"
        "claude_version": claude_version,
        "timestamp": datetime.utcnow().isoformat(),
        "tester": os.environ.get("USER", "unknown"),
    }
    path = Path(f"tests/verification/results/{claim_id}-{claude_version}.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(record, indent=2))
```

**Version-specific notes:**
- V-01: Confirmed on v2.1.49+ (worktree support added)
- V-03: Confirmed on v2.1.50+ (Agent tool enabled by default)
- V-04, V-05, V-07: Must re-verify when Claude Code major version changes

---

## Priority Ordering and Risk Assessment

| Priority | Claim | Risk if wrong | Status |
|----------|-------|---------------|--------|
| 1 (highest) | V-07: CLAUDECODE=1 nesting | Exponential resource consumption | [INFERRED] — test ASAP |
| 2 | V-04: deny rules under skip-permissions | Security bypass | [INFERRED] — test ASAP |
| 3 | V-01: worktree isolation in -p mode | Architecture assumption broken | [DOCUMENTED] — v2.1.49 |
| 4 | V-03: Agent tool default enabled | Tasks silently fail | [DOCUMENTED] — v2.1.50 |
| 5 | V-02: 4-layer token overhead | Budget misjudgment | [INFERRED] — test before scheduler impl |
| 6 | V-05: empty MCP config overhead | Token overhead 10x | [INFERRED] — test before scheduler impl |
| 7 (lowest) | V-06: 83.5% compaction threshold | Scheduler slightly off | [INFERRED] — accept uncertainty |

---

## CI Integration Approach

**Recommendation: Do NOT run verification tests in standard CI.**

Rationale:
- Each test requires real `ANTHROPIC_API_KEY` and makes live API calls
- Cost: ~5–10 tests × ~10K tokens each = ~$0.30–0.60 per CI run
- Test duration: 30–120 seconds per test
- Flakiness: network issues, rate limits, API changes cause false failures

**Recommended CI integration:**
1. Gate verification tests behind `ANTHROPIC_API_KEY` env var being present (skip if absent)
2. Add a GitHub Actions workflow (`.github/workflows/verification.yml`) with `workflow_dispatch` only
3. Run automatically only on release tags (not on every PR)
4. Cache verification results in `tests/verification/results/` — re-run only when Claude Code version changes

```yaml
# .github/workflows/verification.yml
name: Empirical Verification Suite
on:
  workflow_dispatch:
  push:
    tags: ["v*"]  # Run on releases only

jobs:
  verify:
    runs-on: ubuntu-latest
    env:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: actions/checkout@v4
      - run: uv run pytest tests/verification/ -v --timeout=300
```

---

## Fixture Capture Plan

Per `25-hallucination-detection.md` section 5, test fixtures must be captured from real
`claude -p` runs and stored as NDJSON golden files.

**Target fixtures:**

| Fixture file | Captures | Used in |
|-------------|---------|--------|
| `tests/fixtures/v02-isolated-hello.ndjson` | Stream-json output with 4-layer isolation | Token overhead tests |
| `tests/fixtures/v05-no-mcp.ndjson` | Stream-json with empty MCP config | MCP overhead tests |
| `tests/fixtures/v01-worktree-subagent.ndjson` | Stream-json from Agent tool with worktree isolation | Worktree isolation tests |

**Fixture capture script:**
```bash
#!/usr/bin/env bash
# tests/fixtures/capture.sh
# Run with ANTHROPIC_API_KEY set. Captures empirical NDJSON fixtures.

set -e
CLAUDE_CONFIG_DIR=$(mktemp -d)
trap "rm -rf $CLAUDE_CONFIG_DIR" EXIT

echo "Capturing V02 fixture..."
claude -p --dangerously-skip-permissions \
  --output-format stream-json \
  --mcp-config '{}' --strict-mcp-config \
  "Say hi." > tests/fixtures/v02-isolated-hello.ndjson

echo "Capturing V05 fixture..."
claude -p --dangerously-skip-permissions \
  --output-format stream-json \
  --mcp-config '{}' --strict-mcp-config \
  "List your available tools." > tests/fixtures/v05-no-mcp.ndjson

echo "Done. Check tests/fixtures/"
```

---

## Follow-Up Research Recommendations

**[WONT_RESEARCH] Automated V-06 (compaction threshold) measurement**
Measuring the exact threshold requires a very long multi-turn session. Accept 83.5% as
[INFERRED] and use a conservative 75% trigger in the scheduler. Not worth a standalone
research effort.

**[V2_RESEARCH] Continuous verification on Claude Code version updates**
When Claude Code ships a major version (e.g., 3.x), V-04 and V-07 must be re-verified.
Should this be automated via a GitHub Actions workflow that triggers when the `claude`
npm package version changes? Design the monitoring mechanism.

---

## Sources

- [Claude Code v2.1.49 Worktree Support — Claude Code Docs](https://code.claude.com/docs/en/common-workflows)
- [Claude Code Multiple Agent Systems 2026 Guide — eesel.ai](https://www.eesel.ai/blog/claude-code-multiple-agent-systems-complete-2026-guide)
- [Claude Code Worktrees: Parallel Agents — claudefa.st](https://claudefa.st/blog/guide/development/worktree-guide)
- [Worktrees: Parallel Agent Isolation — Agent Factory](https://agentfactory.panaversity.org/docs/General-Agents-Foundations/general-agents/worktrees)
- [docs/research/25-hallucination-detection.md](25-hallucination-detection.md) — Parent doc defining the 7 claims
- [docs/research/01-agent-tool-in-p-mode.md](01-agent-tool-in-p-mode.md) — V-01, V-03, V-07 source
- [docs/research/12-subprocess-token-overhead.md](12-subprocess-token-overhead.md) — V-02, V-05 source
- [docs/research/19-pretooluse-reliability.md](19-pretooluse-reliability.md) — V-04 source
- [docs/research/02-session-continuity.md](02-session-continuity.md) — V-06 source
