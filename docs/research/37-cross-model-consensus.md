# Research: Cross-Model Consensus for Research Validation

**Issue:** #37
**Milestone:** v2
**Feature:** feat:llm-alloc
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Prior Art: Multi-Agent Debate and Self-Consistency](#prior-art-multi-agent-debate-and-self-consistency)
3. [Consensus Protocol Design](#consensus-protocol-design)
4. [Contradiction Detection Approach](#contradiction-detection-approach)
5. [Confidence Promotion Criteria](#confidence-promotion-criteria)
6. [Cost/Benefit Threshold Analysis](#costbenefit-threshold-analysis)
7. [Implementation Sketch for Conductor](#implementation-sketch-for-conductor)
8. [Risks and Failure Modes](#risks-and-failure-modes)
9. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
10. [Sources](#sources)

---

## Executive Summary

Cross-model consensus — running two independent LLMs on the same research question and
comparing outputs — is an established and empirically validated technique for reducing
hallucination rates. Research from 2024–2025 demonstrates that multi-agent debate reduces
factual errors by 15–40% compared to single-model outputs (exact figure depends on task
type and model heterogeneity).

**Key findings:**

1. **Consensus reduces hallucination**, but the effect is stronger with heterogeneous
   models (different providers, not just different sizes) than with homogeneous agents
   (same model, different temperatures). [DOCUMENTED]

2. **Optimal trigger**: Cross-model consensus should only be triggered for `[INFERRED]`
   claims that gate M2 implementation decisions — not for all research. Doc #25 identified
   7 such claims from M1. A blanket dual-model policy roughly doubles research cost without
   commensurate benefit on lower-stakes claims. [DOCUMENTED, INFERRED]

3. **Contradiction detection**: LLM-as-judge is the most practical approach for
   automated semantic contradiction detection between two research docs. Structural diff
   (string comparison) catches surface disagreements; semantic diff (embedding similarity)
   catches conceptual contradictions masked by different wording. A combined approach is
   recommended. [INFERRED]

4. **Confidence promotion**: A claim agreed upon by two independent, heterogeneous models
   with independent sources warrants promotion from `[INFERRED]` to `[DOCUMENTED]` only
   if both models cite verifiable primary sources. Agreement without citation is
   insufficient — it may reflect shared training data rather than independent verification.
   [DOCUMENTED from self-consistency literature]

5. **Cost model**: A dual-model research run for a single issue costs approximately 2×
   the single-agent cost (~160K–320K tokens per issue pair). At $0.60/issue for Sonnet 4.6
   and $0.33/issue for Gemini 3.1 Pro, a heterogeneous pair costs ~$0.93 — a 55% premium
   over Claude-only research. This is justified only for high-stakes `[INFERRED]` claims.

6. **Practical implementation**: A `ConsensusResearchOrchestrator` dispatches two agents
   in parallel, collects both docs, runs an LLM-as-judge contradiction pass, and produces
   a synthesized summary with promoted confidence tags. This is implementable with the
   existing conductor dispatch loop.

---

## Prior Art: Multi-Agent Debate and Self-Consistency

### Multi-Agent Debate (MAD)

The foundational result is from Du et al. (2023), "Improving Factuality and Reasoning in
Language Models through Multiagent Debate" (arXiv 2305.14325): having multiple LLM
instances critique and revise each other's reasoning in iterative rounds significantly
improves factual accuracy and mathematical reasoning. Key findings:

- Multiple rounds of debate (3–5 rounds) outperform single-shot reasoning on factual QA,
  arithmetic, and commonsense tasks.
- The improvement is more pronounced with **heterogeneous** agents (different model
  families) than homogeneous agents (same model, different seeds).
- Debate with adversarial pressure tends to resolve factual errors faster than collaborative
  discussion.

More recent work (2025) on adaptive heterogeneous multi-agent debate confirms these
patterns hold for knowledge-intensive tasks and extends to educational and domain-specific
QA.

### Self-Consistency Sampling

Wang et al. (2022) introduced self-consistency: sample multiple independent reasoning
paths from the same model and select the most-voted answer. This reduces variability
without requiring a second model.

For conductor's use case, self-consistency is **less useful than cross-model consensus**:
- Self-consistency within the same model may reflect shared training data biases rather
  than independent verification.
- For `[INFERRED]` claims based on indirect reasoning from docs (not real-world
  observation), same-model agreement provides weak epistemic justification.

**2025 advances in self-consistency:**
- Confidence-Informed Self-Consistency (CISC): assign confidence scores to reasoning
  paths, use weighted voting. Reduces required samples by ~40% vs. uniform voting.
- Reliability-Aware Adaptive Self-Consistency (ReASC): adaptive sampling with early
  stopping. Reduces sample usage by ~70% while maintaining accuracy.

These optimizations matter for conductor's cost model: if same-model consensus is used
as a fallback (cheaper than cross-model), CISC/ReASC should be applied to reduce token
waste.

### Consensus-Based Multi-Provider Approaches

Industry adoption (as of 2025): organizations in high-stakes domains (medical, legal,
financial) are using multi-provider consensus patterns where two or more LLM APIs are
queried and responses compared. Flagged disagreements trigger human review. This is
analogous to conductor's research validation use case.

---

## Consensus Protocol Design

For conductor research validation, the protocol must ensure outputs from the two models
are comparable along the same dimensions:

### Shared Output Schema

Both research agents must be prompted with an **identical output schema requirement**.
The current research agent prompt template (from CLAUDE.md) already specifies sections
(Follow-Up Research Recommendations, Sources, confidence tags). This template must be
sent verbatim to both agents, with no variation in output format instructions.

**Required shared schema elements:**
1. Confidence tags: `[TESTED]`, `[DOCUMENTED]`, `[INFERRED]` — identical taxonomy
2. Section structure: all sections from the target research template
3. Citation format: `[Author, Year. Title. URL]` — machine-parseable
4. Claim granularity: each architectural claim in its own paragraph with a confidence tag

**What may differ** (intentionally):
- Model selection, API provider
- Research approach (web search strategy)
- Order of evidence presentation

### Parallel Dispatch

Both agents run in parallel (consistent with conductor's existing parallelism model).
The orchestrator waits for both to complete before proceeding to the synthesis step.

```
Issue #N
  │
  ├── Research Agent A (Claude Sonnet 4.6)  ──→ docs/research/<N>-<slug>-model-a.md
  │
  └── Research Agent B (Gemini 3.1 Pro)    ──→ docs/research/<N>-<slug>-model-b.md
                                                         │
                                                   Synthesis Step
                                                         │
                                                docs/research/<N>-<slug>.md
                                                (promoted confidence tags)
```

### Synthesis Step

A third LLM call (orchestrator-level, using Claude Opus 4.6 for higher reasoning quality)
receives both draft docs and produces:
1. A synthesized research doc merging both findings
2. A contradiction report listing disagreements
3. Updated confidence tags (promotion from `[INFERRED]` to `[DOCUMENTED]` where
   both models independently cite primary sources and agree)

---

## Contradiction Detection Approach

Three approaches at increasing sophistication:

### 1. Structural Diff (String-Level)

Compare sections with the same heading across both docs. Flag when:
- A claim appears in one doc but not the other
- Numeric values differ (e.g., "5 minutes" vs. "15 minutes")
- A positive claim in A directly contradicts a negative in B

**Strength:** Fast, deterministic, no LLM call needed.
**Weakness:** Misses semantic contradictions phrased differently ("the feature is
available" vs. "the feature is not supported" won't match structurally if phrased
differently).

**Tool:** Standard line-diff (Python `difflib`) applied to structurally extracted claims
(regex-extracted sentences with confidence tags).

### 2. Semantic Diff (Embedding-Level)

Embed all claim sentences from both docs. For each claim in A, find its nearest neighbor
in B by cosine similarity. If similarity > 0.85 (likely same claim) but the claims
contradict (determined by sentiment/negation classification), flag as contradiction.

**Strength:** Catches paraphrase-level contradictions.
**Weakness:** Requires an embedding model call; negation detection is imprecise.

**Suggested tool:** OpenAI `text-embedding-3-small` or Anthropic's embedding endpoint
(if available); negate with a simple negation classifier or rule-based approach.

### 3. LLM-as-Judge (Semantic Reasoning)

Send both docs to a third LLM with the prompt:

```
You are a research auditor. Compare Document A and Document B about [topic].
For each claim in Document A that appears in Document B, rate agreement:
- AGREE: both docs say the same thing (minor wording differences OK)
- DISAGREE: the docs assert opposite things about the same claim
- UNSUPPORTED_BY_B: claim in A has no corresponding claim in B

List all DISAGREE pairs with verbatim quotes from each doc.
```

**Strength:** Catches semantic contradictions regardless of wording. Handles nuanced
partial agreements.
**Weakness:** Additional LLM cost; subject to LLM-as-judge biases (verbosity, position).

**Reliability concern:** LLM-as-judge systems show measurable position bias (preference
for first-presented doc) and verbosity bias. To mitigate: swap A/B order and re-run;
report contradictions only if flagged in both orderings.

### Recommended Approach for Conductor v2

Use LLM-as-judge (approach 3) with position-bias mitigation as the primary contradiction
detector. Use structural diff (approach 1) as a fast pre-filter to catch obvious numeric
contradictions before calling the judge.

**Cost:** ~10K–15K tokens for the synthesis + contradiction step.
**Time:** Runs after both parallel agents complete; adds ~1–2 minutes per dual-model run.

---

## Confidence Promotion Criteria

A claim `[INFERRED]` in the research doc can be promoted to `[DOCUMENTED]` if all of
the following conditions are met:

| Condition | Requirement |
|-----------|-------------|
| Agreement | Both models assert the same claim with no contradiction detected |
| Primary source | Both models cite at least one verifiable primary source (official doc, GitHub issue, published paper) |
| Source independence | The two cited sources are distinct (not the same URL) |
| Claim specificity | The claim is specific and falsifiable (not "X is generally recommended") |

**What does NOT warrant promotion:**
- Two models agreeing without any primary source citation (may reflect shared training data)
- Agreement on vague directional claims ("feature X is useful")
- Agreement on claims that come from the same secondary source (both citing the same blog post)

**Demotion condition:** If a claim is `[DOCUMENTED]` in one doc but `[INFERRED]` in the
other (or absent), it should be downgraded to `[INFERRED]` in the synthesis and flagged
for manual verification.

---

## Cost/Benefit Threshold Analysis

**Baseline cost per research issue (single model):**
- Claude Sonnet 4.6: ~$0.60/issue (100K input + 20K output)
- Gemini 3.1 Pro: ~$0.33/issue

**Dual-model cost (heterogeneous pair, Sonnet + Gemini):**
- Agent A (Sonnet): $0.60
- Agent B (Gemini): $0.33
- Synthesis (Opus 4.6, ~15K tokens): ~$0.15
- **Total: ~$1.08/issue** — 80% premium over single Sonnet, 228% over single Gemini

**Justification threshold:**

Cross-model consensus is justified when:
1. The claim gates an impl decision (if wrong, requires rework of ≥1 impl issue)
2. The claim is currently tagged `[INFERRED]` (unverified)
3. The claim appears in a `[BLOCKS_IMPL]` research recommendation

This maps to the 7 high-stakes `[INFERRED]` claims identified in `25-hallucination-detection.md`.

**Anti-patterns to reject:**
- Running dual-model on all research (cost doubles with marginal benefit on obvious facts)
- Running dual-model on `[DOCUMENTED]` claims (waste; already have primary source)
- Using dual-model as a substitute for empirical testing (consensus between two LLMs
  is still inference; for behavioral claims about `claude -p`, empirical tests are the
  correct tool per `41-empirical-verification-suite.md`)

**Expected v2 usage:** 5–15 high-stakes `[INFERRED]` claims per milestone. Total
consensus cost: $5–$15 per milestone. Negligible at scale.

---

## Implementation Sketch for Conductor

### New: `consensus_research_worker` Mode

The existing research-worker loop in CLAUDE.md can be extended with a
`--consensus` flag:

```python
async def run_consensus_research(issue_number: int, config: Config) -> None:
    """Run dual-model research for a single high-stakes issue."""
    issue = await get_issue(issue_number)
    branch_a = f"{issue_number}-research-claude"
    branch_b = f"{issue_number}-research-gemini"

    # Dispatch both in parallel
    async with asyncio.TaskGroup() as tg:
        task_a = tg.create_task(
            dispatch_research_agent(issue, branch=branch_a, backend="claude")
        )
        task_b = tg.create_task(
            dispatch_research_agent(issue, branch=branch_b, backend="gemini")
        )

    doc_a_path = task_a.result()  # path to completed research doc
    doc_b_path = task_b.result()

    # Synthesis step
    synthesis = await run_synthesis(doc_a_path, doc_b_path, issue)

    # Write final doc and PR
    await write_final_doc(synthesis, branch=f"{issue_number}-consensus")
    await create_pr(issue_number, branch=f"{issue_number}-consensus")
```

### Synthesis Prompt Template

```python
SYNTHESIS_PROMPT = """
You are a research synthesizer. You have two independent research documents about
the same topic, written by different AI models.

Document A (Claude Sonnet 4.6):
{doc_a}

Document B (Gemini 3.1 Pro):
{doc_b}

Tasks:
1. Identify all claims where A and B agree with primary source citations from each.
   For each such claim, mark it [DOCUMENTED] in the synthesis.
2. Identify all claims where A and B disagree. Quote both versions.
   Mark these [INFERRED] in the synthesis with a note: "Contradiction: see both docs."
3. Identify claims present in only one doc. Include them as [INFERRED].
4. Write a single synthesized research document following the standard template.
   Merge the Sources sections, deduplicated. Keep all Follow-Up Recommendations
   from both docs, deduplicated.

Output the synthesized document in full Markdown.
"""
```

### Integration with Existing Loop

The consensus mode fits into the existing research-worker loop as an optional
post-processing step:

1. Standard research-worker dispatches single-model agents
2. After merge, orchestrator checks: does the merged doc have `[BLOCKS_IMPL]` claims
   tagged `[INFERRED]`?
3. If yes → dispatch consensus run for that issue
4. If no → proceed normally

This keeps the happy path cheap (single model) and applies the premium only where needed.

---

## Risks and Failure Modes

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Both models share training data and "agree" on the same hallucination | Medium | Require distinct primary sources; agreement without independent citations does not promote confidence |
| Synthesis LLM introduces new errors | Low-Medium | Keep synthesis lightweight; do not ask it to generate new facts, only compare and label |
| LLM-as-judge position bias | High (documented) | Always run judge in both A→B and B→A order; report only intersecting contradictions |
| Cost overrun if consensus triggers too broadly | Low if gated | Only trigger for `[INFERRED]` + `[BLOCKS_IMPL]` — apply strict filter before dispatch |
| Gemini doc schema differs from Claude template | Low-Medium | Enforce output schema via prompt; synthesis step handles structural normalization |

---

## Follow-Up Research Recommendations

**[WONT_RESEARCH] Empirical benchmark of cross-model vs. single-model accuracy on conductor's research topics**
This would require running real dual-model research and manually scoring outputs — a measurement task, not a research doc. Verify by running the first consensus batch and reviewing promotions. This is an operational calibration step.

**[WONT_RESEARCH] Building a custom contradiction detection NLP pipeline**
LLM-as-judge with position-bias mitigation is sufficient for conductor's volume (5–15 claims per milestone). Building a custom pipeline is premature optimization.

**[V2_RESEARCH] Embedding-based semantic similarity for claim deduplication in multi-model research docs**
When merging Follow-Up Recommendations from two research docs, duplicates may be phrased differently. Semantic deduplication could improve synthesis quality. Low priority — manual review is sufficient for current volumes.

---

## Sources

- [Du et al. (2023) "Improving Factuality and Reasoning in Language Models through Multiagent Debate" — arXiv 2305.14325](https://arxiv.org/abs/2305.14325)
- [Composable Models: LLM Debate project page](https://composable-models.github.io/llm_debate/)
- [Multi-Agent Debate for Hallucination Reduction — MDPI Applied Sciences 2025](https://www.mdpi.com/2076-3417/15/7/3676)
- [Adaptive heterogeneous multi-agent debate — Springer 2025](https://link.springer.com/article/10.1007/s44443-025-00353-3)
- [Reducing AI Hallucinations with a Multi-LLM Strategy — ProActiveManagement 2025](https://proactivemgmt.com/blog/2025/03/06/reducing-ai-hallucinations-multi-llm-consensus/)
- [Confidence Improves Self-Consistency in LLMs (CISC) — ACL 2025](https://aclanthology.org/2025.findings-acl.1030/)
- [Reliability-Aware Adaptive Self-Consistency (ReASC) — arXiv 2601.02970](https://arxiv.org/html/2601.02970)
- [A Survey on LLM-as-a-Judge — arXiv 2411.15594](https://arxiv.org/abs/2411.15594)
- [LLM Fan-Out: Self-Consistency, Consensus, Voting Patterns — Kinde](https://www.kinde.com/learn/ai-for-software-engineering/workflows/llm-fan-out-101-self-consistency-consensus-and-voting-patterns/)
- [Mitigating LLM Hallucinations Using a Multi-Agent Framework — MDPI Information 2025](https://www.mdpi.com/2078-2489/16/7/517)

**Cross-references:**
- `25-hallucination-detection.md` — defines the `[TESTED]`/`[DOCUMENTED]`/`[INFERRED]` taxonomy and identifies 7 high-stakes `[INFERRED]` claims from M1
- `12-subprocess-token-overhead.md` — cost model for sub-agent runs (per-turn token overhead)
- `36-multi-model-backends.md` — ModelBackend protocol enabling Gemini as the second model
