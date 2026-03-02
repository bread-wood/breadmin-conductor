# Research: CI/Server Deployment Without a Personal Claude Session

**Issue:** #38
**Milestone:** v2
**Feature:** feat:ci-deploy
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [API Key Auth for `claude -p` in CI](#api-key-auth-for-claude--p-in-ci)
3. [GitHub Actions Integration Pattern](#github-actions-integration-pattern)
4. [Headless OAuth Feasibility](#headless-oauth-feasibility)
5. [Cost Model: API Billing vs. Max Subscription](#cost-model-api-billing-vs-max-subscription)
6. [Recommended Trigger Mechanism](#recommended-trigger-mechanism)
7. [Self-Hosted Runner Requirements](#self-hosted-runner-requirements)
8. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
9. [Sources](#sources)

---

## Executive Summary

Conductor can run in GitHub Actions with `ANTHROPIC_API_KEY` as the auth mechanism.
API key auth is **fully supported** by `claude -p` and is the **only viable path** for
CI/server deployments — headless OAuth is not supported and is explicitly prohibited
by Anthropic's Terms of Service for third-party tools.

**Key findings:**

1. **`ANTHROPIC_API_KEY` in `claude -p` works in CI.** Setting the env var causes
   Claude Code to use the API directly, bypassing OAuth. All conductor-critical features
   (Agent tool, MCP, worktrees) are available. [DOCUMENTED]

2. **Headless OAuth is not viable.** OAuth tokens expire in ~10–15 minutes and do not
   auto-refresh in non-interactive mode (Issue #28827). Furthermore, Anthropic's ToS
   (February 2026) explicitly prohibits third-party tools using OAuth tokens — API keys
   are the supported path for programmatic use. [DOCUMENTED]

3. **Anthropic's official GitHub Actions action** (`anthropics/claude-code-action`) is
   available and uses `ANTHROPIC_API_KEY` stored as a GitHub secret. For conductor's
   use case, this action is a review/assistance tool, not a full conductor runner — but
   it demonstrates the correct pattern. [DOCUMENTED]

4. **Cost for server workloads**: API billing at Sonnet 4.6 rates (~$0.60/issue) costs
   approximately $12/month for 20 issues. Max subscription at $100/month covers ~150
   issues. The break-even is at ~100+ issues/month. Most personal conductor users are
   below break-even; team deployments with shared API budgets favor API billing.

5. **Recommended trigger**: `workflow_dispatch` (manual trigger) plus a `schedule` cron
   for nightly runs. Avoid event-driven triggers (push, PR) for conductor's pull-based
   issue queue polling model.

6. **Self-hosted runner requirements**: 4+ CPU cores, 16+ GB RAM, 50+ GB disk for
   worktree operations. GitHub-hosted runners (2-core, 7 GB RAM) are insufficient
   for concurrent conductor agents.

---

## API Key Auth for `claude -p` in CI

### How It Works

When `ANTHROPIC_API_KEY` is present in the process environment, Claude Code uses it
for all API calls and bypasses the OAuth credential store entirely. This is the primary
and officially supported auth path for programmatic/CI use.

```bash
# In GitHub Actions workflow
env:
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}

# In conductor's subprocess dispatch
result = subprocess.run(
    ["claude", "-p", "--dangerously-skip-permissions", ...],
    env={
        "ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"],
        # ... other required env vars
    },
    ...
)
```

### Feature Availability with API Key Auth

All conductor-critical features are available with `ANTHROPIC_API_KEY`:

| Feature | Available with API Key |
|---------|----------------------|
| `claude -p` headless mode | Yes |
| `--output-format stream-json` | Yes |
| Agent/Task tool (sub-agents) | Yes |
| `--allowedTools` / `--disallowedTools` | Yes |
| MCP server integration | Yes |
| `--append-system-prompt-file` | Yes |
| `--max-turns` | Yes |
| `--max-budget-usd` | Yes (hard spend cap) |
| Worktree isolation (via CWD) | Yes |

**`--max-budget-usd`** is particularly valuable for CI: it provides a hard per-invocation
spend cap that prevents runaway costs. This feature is NOT available with subscription auth.

### Known Issue: Non-Interactive Auth Failure (Resolved)

GitHub issue anthropics/claude-code#551 documented a case where non-interactive mode
failed with "Invalid API key — Please run /login" even with `ANTHROPIC_API_KEY` set.
This was caused by a conflict with stored OAuth credentials in `~/.claude/.credentials.json`.

**Resolution:** Set `CLAUDE_CONFIG_DIR=/tmp/claude-ci-$JOB_ID` to give each CI job a
fresh, empty config directory, preventing any conflict with stored credentials.

```bash
env:
  CLAUDE_CONFIG_DIR: /tmp/claude-ci-${{ github.run_id }}
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

This also provides job-level isolation when multiple conductor jobs run concurrently on
the same runner (critical for self-hosted runners).

---

## GitHub Actions Integration Pattern

### Anthropic's Official Action

Anthropic provides `anthropics/claude-code-action@v1` for GitHub Actions integration.
This action supports:
- `ANTHROPIC_API_KEY` as a repository secret
- Amazon Bedrock and Google Vertex AI auth (for enterprise deployments)
- Review/assistance on PRs and issues (triggered by comments or events)

However, this action is designed for **review and assistance** (responding to `@claude`
comments), not for running conductor's full orchestration loop. Conductor needs a different
approach: a workflow that invokes `python -m conductor research-worker ...` as a step.

### Conductor Workflow Pattern

```yaml
# .github/workflows/conductor-research.yml
name: Conductor Research Worker

on:
  workflow_dispatch:
    inputs:
      milestone:
        description: "Milestone to process (e.g., v2)"
        required: true
        default: "v2"
  schedule:
    - cron: "0 2 * * *"  # Nightly at 2am UTC

jobs:
  research-worker:
    runs-on: self-hosted  # Requires self-hosted runner (see requirements below)
    timeout-minutes: 360  # 6 hours max

    env:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      CLAUDE_CONFIG_DIR: /tmp/claude-ci-${{ github.run_id }}

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # Full history for git worktree operations

      - name: Install uv and dependencies
        run: |
          pip install uv
          uv sync

      - name: Run research worker
        run: |
          uv run conductor research-worker \
            --repo bread-wood/breadmin-composer \
            --milestone "${{ github.event.inputs.milestone || 'v2' }}"
```

### Concurrency Control

GitHub Actions provides `concurrency` groups to prevent multiple runs of the same
workflow from overlapping:

```yaml
concurrency:
  group: conductor-research-${{ github.ref }}
  cancel-in-progress: false  # Let existing runs finish
```

This maps cleanly to conductor's "one orchestrator session per repo at a time" rule.

### Secrets Management

Required secrets for conductor in GitHub Actions:

| Secret | Purpose |
|--------|---------|
| `ANTHROPIC_API_KEY` | Claude Code API authentication |
| `GITHUB_TOKEN` | Automatically provided by GHA; scoped to repo |
| `NOTION_API_KEY` | (Optional) For Notion session reports |

For multi-repo conductors (see issue #39), per-repo `GITHUB_TOKEN` scoping requires
fine-grained PATs stored as additional secrets. The automatically-provided
`GITHUB_TOKEN` only has access to the current repo.

---

## Headless OAuth Feasibility

**Conclusion: Not viable. Do not implement.**

Three blockers:

1. **Token expiry**: OAuth access tokens expire in ~10–15 minutes. In headless `-p` mode,
   Claude Code does not trigger a refresh flow (Issue #28827, confirmed March 2026).
   Sessions longer than 15 minutes will fail mid-run.

2. **Anthropic ToS (February 2026)**: Anthropic explicitly prohibits third-party tools
   from using OAuth tokens belonging to a personal Claude subscription. Only Claude.ai
   and Claude Code itself are permitted OAuth consumers. Conductor, as a third-party
   orchestration layer, must use API keys. [DOCUMENTED]

3. **No device flow**: There is no documented device authorization flow or service-account
   OAuth path that would allow conductor to authenticate a headless server process via
   the user's subscription. The only non-interactive option is `ANTHROPIC_API_KEY`.

**Historical context:** Before February 2026, some community tools used OAuth tokens
extracted from `~/.claude/.credentials.json` to run headless sessions on personal
subscriptions. Anthropic's enforcement action shut this down. Conductor must not use
this approach regardless of technical feasibility.

---

## Cost Model: API Billing vs. Max Subscription

### Workload Assumptions

| Parameter | Value |
|-----------|-------|
| Issues per month | 20 |
| Tokens per issue (research) | 100K input + 20K output |
| Model | Claude Sonnet 4.6 |

### API Billing Cost

- Input: 20 issues × 100K tokens × $3.00/MTok = $6.00/month
- Output: 20 issues × 20K tokens × $15.00/MTok = $6.00/month
- **Total: ~$12/month** for 20 research issues

### Max Subscription Cost

- Max 5x: $100/month
- Approximate capacity: ~500 Sonnet 4.6 turns per 5-hour window
- For conductor: 20 issues × 5 turns avg = 100 turns/month, well within quota
- **Cost: $100/month** (fixed, regardless of actual usage)

### Break-Even Analysis

| Monthly issues | API cost (Sonnet) | Max 5x | More economical |
|----------------|-------------------|--------|-----------------|
| 20 | ~$12 | $100 | API |
| 50 | ~$30 | $100 | API |
| 100 | ~$60 | $100 | API (just below break-even) |
| 167+ | ~$100 | $100 | Equal |
| 250+ | ~$150 | $100 | Max subscription |

**Recommendation:**
- Personal deployments (< 100 issues/month): API billing is more economical
- Team deployments with shared API budget (100+ issues/month): Max subscription
- CI/server deployments always benefit from `--max-budget-usd` (only available with API billing)

### Additional Benefit of API Billing: Hard Spend Caps

`--max-budget-usd` provides a per-invocation hard cap not available with subscription auth.
For CI deployments, this is critical — a runaway agent cannot consume more than the cap.

Subscription-based usage has soft limits (5-hour windows) but no per-invocation hard cap.

---

## Recommended Trigger Mechanism

### Options Evaluated

| Trigger | Mechanism | Suitable for Conductor? |
|---------|-----------|------------------------|
| `workflow_dispatch` | Manual trigger via GitHub UI or API | Yes — good for on-demand runs |
| `schedule` (cron) | Runs at fixed intervals | Yes — nightly poll of issue queue |
| `push` | Triggers on git push | No — conductor is pull-based |
| `issues` event | Triggers on new/labeled issues | Possible but fragile — race conditions |
| `pull_request` event | Triggers on PR events | No — conductor creates PRs, doesn't react to them |

### Recommended: `workflow_dispatch` + `schedule`

```yaml
on:
  workflow_dispatch:
    inputs:
      milestone:
        description: "Target milestone"
        required: true
  schedule:
    - cron: "0 2 * * 1-5"  # Weekday nights at 2am UTC
```

**Why not issue events?** The `issues` event fires when an issue is labeled `in-progress`
(by the orchestrator itself), creating potential infinite loops. The pull-based polling
model in conductor's research-worker loop handles freshness without event-driven triggers.

**`workflow_dispatch` for manual runs**: When a developer wants to trigger research
immediately, they use `gh workflow run conductor-research.yml -f milestone=v2`. This
integrates cleanly with conductor's existing CLI design.

---

## Self-Hosted Runner Requirements

GitHub-hosted runners (2-core, 7 GB RAM, 14 GB SSD) are **insufficient** for conductor.

**Minimum requirements for self-hosted runner:**

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU cores | 4 | 8 |
| RAM | 16 GB | 32 GB |
| Disk | 50 GB | 100 GB |
| OS | Ubuntu 22.04 LTS | Ubuntu 22.04 LTS |
| Git | 2.35+ | Latest |
| Node.js | 18+ | 20+ (for `claude` CLI) |
| Python | 3.11+ | 3.12 |

**Why the high requirements:**
- Each `claude -p` sub-agent spawns a Node.js process (~256 MB RSS)
- 5 concurrent agents = ~1.5 GB RAM overhead from Claude Code processes alone
- Python conductor process adds ~200 MB
- Git worktrees for each agent: ~50–200 MB disk per worktree depending on repo size
- For breadmin-composer: 5 worktrees × ~100 MB = 500 MB disk per conductor run

**Cloud provider options:**

| Provider | Instance type | vCPU | RAM | Monthly cost |
|---------|---------------|------|-----|-------------|
| AWS | c6i.2xlarge | 8 | 16 GB | ~$250 |
| AWS | t3.2xlarge | 8 | 32 GB | ~$270 |
| GCP | n2-standard-8 | 8 | 32 GB | ~$240 |
| Hetzner | CX42 | 8 | 16 GB | ~$40 |

For personal deployments, a Hetzner CX42 instance provides sufficient resources at a
fraction of major cloud costs.

---

## Follow-Up Research Recommendations

**[WONT_RESEARCH] Detailed cost benchmarking of API vs. subscription for specific conductor workloads**
This requires actual conductor runs with token counting. The break-even analysis above is
sufficient for design decisions. Calibrate empirically once conductor is running in CI.

**[V2_RESEARCH] GitHub Actions `issues` event trigger for conductor research worker**
Is there a safe, race-condition-free way to trigger the research worker when a new
`stage/research` issue is labeled? Requires careful analysis of GHA concurrency controls
and conductor's self-assignment logic to avoid infinite trigger loops.

**[WONT_RESEARCH] Kubernetes deployment for conductor**
Out of scope for v2. A single VM self-hosted runner is sufficient. Kubernetes adds
complexity without commensurate benefit at conductor's current scale.

---

## Sources

- [Claude Code GitHub Actions — Official Docs](https://code.claude.com/docs/en/github-actions)
- [anthropics/claude-code-action GitHub Repository](https://github.com/anthropics/claude-code-action)
- [Run Claude Code Programmatically — Claude Code Docs](https://code.claude.com/docs/en/headless)
- [Claude Code GitLab CI/CD — Claude Code Docs](https://code.claude.com/docs/en/gitlab-ci-cd)
- [OAuth token refresh fails in non-interactive mode — anthropics/claude-code Issue #28827](https://github.com/anthropics/claude-code/issues/28827)
- [Anthropic clarifies ban on third-party Claude OAuth usage — The Register, Feb 2026](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/)
- [Non-Interactive Mode Fails to Authenticate Using API Key — anthropics/claude-code Issue #551](https://github.com/anthropics/claude-code/issues/551)
- [Claude Code API Pricing 2026 — Anthropic Platform](https://platform.claude.com/docs/en/about-claude/pricing)
- [Claude Plans Pricing — claude.com](https://claude.com/pricing)
- [Anthropic API Pricing 2026 Guide — nops.io](https://www.nops.io/blog/anthropic-api-pricing/)
- [About Self-Hosted Runners — GitHub Docs](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/about-self-hosted-runners)
- [Managing API Key Environment Variables in Claude Code — Claude Help Center](https://support.claude.com/en/articles/12304248-managing-api-key-environment-variables-in-claude-code)
- [Claude Code Action with OAuth — GitHub Marketplace](https://github.com/marketplace/actions/claude-code-action-with-oauth)

**Cross-references:**
- `11-api-key-rotation.md` — credential lifecycle concerns for long-running sessions
- `17-credential-proxy.md` — gh CLI credential isolation for sub-agents
- `43-anthropic-key-proxy.md` — ANTHROPIC_API_KEY proxy pattern for sub-agent security
- `08-usage-scheduling.md` — usage window limits for Pro/Max subscription accounts
- `04-configuration.md` — `CLAUDE_CONFIG_DIR` and environment isolation per sub-agent
