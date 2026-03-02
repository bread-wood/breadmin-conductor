# Research: bug-worker — Error-Driven Diagnosis and Issue Filing Agent

**Issue:** #79
**Milestone:** v2
**Feature:** core
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Input Interface Design](#input-interface-design)
3. [Investigation Playbook](#investigation-playbook)
4. [Multi-Package Log Aggregation Strategy](#multi-package-log-aggregation-strategy)
5. [Issue Quality Standard](#issue-quality-standard)
6. [Scope Limits and Stop Conditions](#scope-limits-and-stop-conditions)
7. [Integration with impl-worker](#integration-with-impl-worker)
8. [Invocation Patterns](#invocation-patterns)
9. [Skill File Design](#skill-file-design)
10. [Prior Art: Autonomous Debugging Agents](#prior-art-autonomous-debugging-agents)
11. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
12. [Sources](#sources)

---

## Executive Summary

The `bug-worker` is the inverse of `impl-worker`: it takes an observed error as input,
autonomously investigates the codebase and logs to identify the root cause, and files a
well-structured GitHub issue. Unlike `impl-worker` (which implements known issues),
`bug-worker` discovers and documents unknown failures.

**Key design decisions:**

1. **Input surface**: Accept three forms — raw error string, log file path + line range,
   or natural language description. Structured JSON input is also accepted for
   programmatic invocation from monitoring hooks. [DOCUMENTED]

2. **Investigation playbook**: A 7-step ordered sequence: error parsing → stack trace
   tracing → log correlation → git blame → test coverage check → cross-package impact →
   root cause hypothesis. Each step has explicit stop conditions. [INFERRED]

3. **Log aggregation**: Use structlog's JSONL format with a standard `log/` directory
   under each package root. Timestamp-based correlation across packages. Grep for the
   error's unique tokens across all known log directories. [INFERRED]

4. **Issue quality**: Filed issues must include: verbatim error + stack trace (or excerpt),
   root cause hypothesis with confidence tag (`[CONFIDENT]`/`[LIKELY]`/`[SPECULATIVE]`),
   affected files with line numbers, suggested fix direction, and a label of `bug+diagnosed`
   or `bug+needs-repro`. [INFERRED]

5. **Stop conditions**: Stop and ask when: the root cause involves business logic the
   agent cannot verify, when 3+ competing hypotheses exist with equal evidence, or when
   the error is in infrastructure/environment. File a `bug+needs-investigation` issue
   with partial findings rather than asking interactively. [INFERRED]

6. **Integration**: Bug-worker filed issues should be impl-worker compatible: acceptance
   criteria, affected files list, and suggested fix direction make them immediately
   actionable for impl-worker dispatch. [INFERRED]

---

## Input Interface Design

### Accepted Input Forms

**Form 1: Raw error string**
```bash
bug-worker --error "ValueError: quantity must be positive, got -5.0"
```

**Form 2: Log file + line range**
```bash
bug-worker --log breadwinner.log --lines 1450-1480
```

**Form 3: Natural language description**
```bash
bug-worker --description "execute-trades crashes on notional orders when market is closed"
```

**Form 4: Stdin pipe**
```bash
cat error.log | bug-worker
# or
tail -n 100 breadwinner.log | bug-worker
```

**Form 5: JSON (for programmatic invocation)**
```bash
echo '{"error": "...", "context": "..."}' | bug-worker --format json
```

### Input Processing Rules

| Input form | Parser behavior |
|------------|----------------|
| Python exception | Extract type, message, traceback frames (file, line, function name) |
| Java/Node.js stack trace | Extract language-specific frame format |
| JSONL log line | Extract `level`, `message`, `timestamp`, `module` fields |
| Natural language | Pass as-is to the agent; agent decides investigation path |
| Multiple inputs | Combine all forms; use error string to anchor log search |

**Edge case: ambiguous input (no stack trace, no file references)**

When the error is a pure natural language description with no technical artifacts, the
agent enters "hypothesis mode" first: searches the codebase for code paths matching the
described behavior before inspecting logs.

---

## Investigation Playbook

The investigation follows these 7 steps in order. Each step may short-circuit to
root cause hypothesis if sufficient evidence is found.

### Step 1: Error Parsing and Normalization

**Actions:**
- Extract: exception type, error message, stack trace frames (file paths, line numbers,
  function names)
- If Python: parse `Traceback (most recent call last):` block
- If JSONL log line: extract fields
- Normalize file paths to absolute repo paths

**Output:** A structured error record with extracted metadata.

**Example:**
```
error_type: ValueError
message: "quantity must be positive, got -5.0"
frames:
  - file: src/breadwinner/orders.py, line: 147, function: validate_order
  - file: src/breadwinner/executor.py, line: 89, function: execute_trade
  - file: src/breadwinner/scheduler.py, line: 234, function: run_cycle
```

### Step 2: Stack Trace Tracing

**Actions:**
- Read each file in the stack trace at the referenced line numbers (+/- 10 lines context)
- Identify: what condition triggers the error, what input value violates it
- Find the caller that provides the violating value
- Trace backwards: where does the value originate?

**Output:** The "blame chain" — which function passes the bad value and where it comes from.

**Tools:** `Read`, `Glob`, `Grep`

### Step 3: Log Correlation

**Actions:**
- Search log files for the error string (or unique substrings) in the time window
  around the error occurrence
- Find related log entries in the 60 seconds before the error (what was the system doing?)
- Find entries from other packages in the same time window (multi-package correlation)

**Output:** A timeline of events leading to the error.

**Search strategy:**
```bash
# Find the error in logs
grep -r "quantity must be positive" logs/

# Find entries 60 seconds before the error timestamp
awk '/2026-03-02T14:22/{found=1} found && /2026-03-02T14:21/{print}' breadwinner.log
```

### Step 4: Git Blame and Recent Changes

**Actions:**
- `git blame` the file and line where the error originates
- Find the most recent commit that touched that line
- Check git log for any changes in the last 7 days to the involved files

**Output:** Which commit introduced the failing behavior and when.

**Tools:** `Bash(git blame)`, `Bash(git log --since=7d -- <file>)`

### Step 5: Test Coverage Check

**Actions:**
- Search `tests/` for tests that cover the failing function
- Check if the failing scenario (e.g., negative quantity) is tested
- If no test exists: note "no regression test for this case" in the issue

**Output:** Coverage gap identification.

**Tools:** `Grep(pattern="def test.*validate_order", path="tests/")`

### Step 6: Cross-Package Impact Assessment

**Actions:**
- Check if the failing function is called by code in other packages
- If yes: does the same bad input reach those call sites?
- Are there similar patterns elsewhere in the codebase that could have the same bug?

**Output:** Blast radius assessment — is this isolated or systemic?

**Tools:** `Grep(pattern="validate_order", path="src/")`

### Step 7: Root Cause Hypothesis Formulation

**Actions:**
- Synthesize findings from steps 1–6 into a hypothesis
- Assign confidence: `[CONFIDENT]` (stack trace + blame point to specific commit),
  `[LIKELY]` (circumstantial evidence, no direct blame), `[SPECULATIVE]` (multiple
  plausible causes, insufficient evidence)
- If `[SPECULATIVE]`: list all competing hypotheses

**Output:** Root cause hypothesis ready for issue filing.

---

## Multi-Package Log Aggregation Strategy

### Log Directory Convention

For breadmin-platform's multi-package layout, establish a convention:

```
breadmin-platform/
├── breadwinner/
│   └── logs/
│       ├── breadwinner.jsonl   ← structured JSONL (structlog)
│       └── error.log           ← plain text fallback
├── moot/
│   └── logs/
│       ├── moot.jsonl
│       └── error.log
└── core/
    └── logs/
        ├── core.jsonl
        └── error.log
```

**Log discovery:** Bug-worker searches for all `*.jsonl` and `*.log` files under known
package roots (or under a configurable `LOG_ROOT` env var).

### Timestamp Alignment

JSONL logs use ISO 8601 timestamps (UTC). Cross-package correlation:

```python
# Collect all log entries in a 2-minute window around the error timestamp
ERROR_TIMESTAMP = "2026-03-02T14:22:15Z"
TIME_WINDOW_SECONDS = 120

all_events = []
for log_file in discover_log_files():
    for line in log_file.read_text().splitlines():
        entry = json.loads(line)
        if abs(parse_timestamp(entry["timestamp"]) - parse_timestamp(ERROR_TIMESTAMP)) < TIME_WINDOW_SECONDS:
            all_events.append(entry)

# Sort by timestamp
all_events.sort(key=lambda e: e["timestamp"])
```

### Structured vs. Unstructured Logs

| Format | Parser | Advantage |
|--------|--------|----------|
| JSONL (structlog) | `json.loads()` + field extraction | Queryable, timestamp-aligned |
| Plain text (Python logging) | Regex + log record patterns | Human-readable but less queryable |
| Nginx/system logs | Custom regex per format | Infrastructure errors |

**Recommendation:** Require JSONL for breadmin packages. Plain text is accepted as fallback
but yields lower-quality correlations.

---

## Issue Quality Standard

A well-structured bug-worker issue must include all of the following:

### Required Sections

```markdown
## Bug Report
**Filed by:** bug-worker (automated)
**Date:** 2026-03-02
**Confidence:** [CONFIDENT | LIKELY | SPECULATIVE]

## Error
<verbatim error message and stack trace or JSONL excerpt>

## Root Cause Hypothesis
<concise statement of what caused the error>

## Evidence
### Stack Trace Analysis
<what the stack trace reveals>

### Log Timeline
<key log entries in the 60 seconds before the error, from all packages>

### Git Blame
<commit that introduced the failing behavior, if identified>

## Affected Files
- `src/breadwinner/orders.py:147` (validate_order function)
- `src/breadwinner/executor.py:89` (execute_trade caller)

## Suggested Fix Direction
<not a full implementation; what the fix should accomplish>

## Reproduction Steps
<minimal steps to reproduce, if known>

## Related Issues and PRs
<any linked issues or PRs from `gh issue list --search "quantity"`, if found>

## Test Gap
<does the failing scenario have a test? If not, note it>
```

### Labels

| Label | When to apply |
|-------|--------------|
| `bug+diagnosed` | Root cause identified with `[CONFIDENT]` or `[LIKELY]` confidence |
| `bug+needs-repro` | Hypothesis is `[SPECULATIVE]`; human verification needed |
| `bug+needs-investigation` | Agent could not determine root cause; partial findings filed |
| `module/<name>` | The affected module, per CLAUDE.md module table |

---

## Scope Limits and Stop Conditions

Bug-worker must recognize when it cannot reliably diagnose further and file a partial
findings issue rather than continuing indefinitely.

### Stop Conditions

| Condition | Action |
|-----------|--------|
| 3+ competing hypotheses with equal evidence | File `bug+needs-investigation` with all hypotheses listed |
| Root cause requires understanding business logic | File `bug+needs-investigation`; note "business logic verification required" |
| Error is in infrastructure (OS, network, cloud) | File `bug+needs-investigation`; label `infra` not `module/<name>` |
| Stack trace points to a third-party library | Check if the library version is pinned; file against dependency if so |
| No logs available, no stack trace, pure description | File `bug+needs-repro` with reproduction steps request |
| Investigation exceeds 20 turns | File partial findings; stop |

### When NOT to Ask Interactively

Bug-worker runs headlessly. It must never block waiting for human input. When uncertain,
file the partial-findings issue and STOP. The issue itself communicates the uncertainty
to the human operator.

---

## Integration with impl-worker

### Issue Compatibility

Bug-worker filed issues must be impl-worker compatible:

```markdown
## Acceptance Criteria
- [ ] Fix validate_order to reject negative quantities at the input layer
- [ ] Add test: test_validate_order_rejects_negative_quantity
- [ ] Ensure the fix does not change behavior for positive quantities

## Affected Files (Allowed Scope)
- `src/breadwinner/orders.py`
- `tests/unit/test_orders.py`
```

This mirrors the format that `plan-issues` produces for impl issues. An impl-worker can
dispatch immediately on a `bug+diagnosed` issue without additional planning.

### Label Flow

```
bug-worker files →  bug+diagnosed  →  impl-worker picks up (impl issue)
bug-worker files →  bug+needs-repro →  human reviews →  re-labels bug+diagnosed
bug-worker files →  bug+needs-investigation →  research-worker could investigate
```

### Stage Label

Bug-worker filed issues receive `stage/impl` (not `stage/research`) when confidence is
`[CONFIDENT]` or `[LIKELY]` — the fix is ready to implement. They receive `stage/research`
when `[SPECULATIVE]` — more investigation is needed.

---

## Invocation Patterns

### CLI

```bash
# From error string
composer bug-worker --error "ValueError: quantity must be positive, got -5.0"

# From log file
composer bug-worker --log breadwinner/logs/breadwinner.jsonl --lines 1450-1480

# From natural language
composer bug-worker --description "execute-trades crashes on notional orders"

# Stdin pipe
tail -n 100 breadwinner/logs/error.log | composer bug-worker

# File a result to a specific repo
composer bug-worker --error "..." --repo bread-wood/breadwinner
```

### Programmatic (from monitoring hook)

```python
import asyncio
from composer.workers.bug_worker import run_bug_worker

# Triggered by a structured log entry with level=ERROR
async def on_error_log(entry: dict):
    if entry.get("level") == "ERROR":
        await run_bug_worker(
            error=entry["message"],
            context=json.dumps(entry),
            repo="bread-wood/breadwinner",
        )
```

### Skill Invocation from Another Agent

Any conductor sub-agent can trigger bug-worker by creating a GitHub issue with label
`bug+needs-investigation`. The conductor's monitoring loop detects these issues and
dispatches a bug-worker instance.

---

## Skill File Design

The bug-worker skill file follows the same pattern as research-worker and impl-worker.

```markdown
# bug-worker skill

You are a bug-worker agent. Your task is to diagnose the reported error and file a
well-structured GitHub issue with findings.

## Input
You will receive one or more of:
- Raw error string / stack trace
- Log file path and line range
- Natural language description of the failing behavior

## Investigation Playbook
Execute these steps in order:
1. Parse error and extract: type, message, stack trace frames (file, line, function)
2. Read each file in the stack trace at the referenced lines (+/- 10 lines)
3. Search logs for the error string in the time window; find related log entries
4. git blame the failing line; check recent commits to the affected files
5. Search tests/ for coverage of the failing function
6. Check if the failing function is called by other packages
7. Formulate root cause hypothesis with confidence: [CONFIDENT], [LIKELY], [SPECULATIVE]

## Stop Conditions
Stop and file partial findings when:
- 3+ competing hypotheses with equal evidence
- Root cause requires business logic knowledge you lack
- Error is in infrastructure (not application code)
- Investigation exceeds 20 turns

## Issue Filing
File a GitHub issue using:
gh issue create --title "Bug: <concise description>" \
  --label "bug+diagnosed,module/<name>,stage/impl" \
  --body "<formatted issue body>"

## DO NOT
- Ask interactively (run headlessly to completion)
- Implement the fix (file the issue, then STOP)
- Create PRs (bug-worker only files issues)
```

---

## Prior Art: Autonomous Debugging Agents

### AgentDebug (2025)

Research by PSU/Duke (ICML 2025 poster) proposes automated failure attribution in LLM
multi-agent systems. AgentDebug isolates root-cause failures and provides corrective
feedback, enabling agents to recover iteratively. Key insight: attributing failures
to specific agents and steps (not just outputs) dramatically improves diagnosis accuracy.

**Relevance for conductor:** Bug-worker can adopt the AgentDebug framework's "decisive
error step" identification: not just "what failed" but "which function call triggered the
cascade."

### FVDebug (NVIDIA, 2025)

FVDebug automates root-cause analysis by combining multiple data sources into a Causal
Graph Synthesis pipeline. It demonstrates that combining code trace + log data + git
history into a unified causal graph dramatically improves root cause accuracy compared
to analyzing each source independently.

**Relevance for conductor:** The investigation playbook above implements a simplified
version of FVDebug's approach without a formal causal graph. A v3 enhancement could
build a structured causal graph from the investigation steps.

### AgentErrorTaxonomy (2025)

A lifecycle-oriented bug taxonomy categorizes framework-level bugs in LLM agent systems.
The primary failure source is "execution-semantics mechanisms in the self-action stage"
(i.e., tool calls that silently succeed but produce wrong output).

**Relevance for conductor:** Bug-worker should check for this class of failure: did a
tool call succeed but with unexpected side effects? This requires comparing expected vs.
actual output of intermediate steps.

---

## Follow-Up Research Recommendations

**[V2_RESEARCH] Structured log search via JSONL field queries for bug-worker**
Bug-worker currently searches logs via grep (string matching). For JSONL logs, field-level
queries (e.g., `level=ERROR AND module=orders`) would be more precise and faster. Research
whether `jq` or a Python JSONL query library is the right tool, and how to expose this
as a tool the bug-worker agent can use.

**[WONT_RESEARCH] Formal causal graph construction for root cause analysis**
FVDebug's causal graph approach is research-grade and would significantly increase
implementation complexity. Bug-worker's 7-step playbook achieves 80% of the accuracy
with 20% of the complexity. Defer formal causal graphs to v3.

---

## Sources

- [Which Agent Causes Task Failures? — ICML 2025 (PSU/Duke)](https://icml.cc/virtual/2025/poster/45823)
- [arXiv 2505.00212: Automated Failure Attribution of LLM Multi-Agent Systems](https://arxiv.org/abs/2505.00212)
- [FVDebug: LLM-Driven Debugging — NVIDIA Research 2025](https://research.nvidia.com/publication/2025-09_fvdebug-llm-driven-debugging-assistant-automated-root-cause-analysis-formal)
- [An Empirical Study of Bugs in Modern LLM Agent Frameworks — arXiv 2602.21806](https://arxiv.org/html/2602.21806v3)
- [Where LLM Agents Fail and How They Can Learn — arXiv 2509.25370](https://arxiv.org/abs/2509.25370)
- [When Agents Fail: Bugs in LLM Agents with Automated Labeling — arXiv 2601.15232](https://arxiv.org/html/2601.15232)

**Cross-references:**
- `07-skill-adaptation.md` — skill file design patterns for conductor workers
- `05-logging-observability.md` — JSONL log format and conductor session logging
- `25-hallucination-detection.md` — confidence tagging taxonomy ([CONFIDENT], [LIKELY], [SPECULATIVE]) adapted from [TESTED]/[DOCUMENTED]/[INFERRED]
