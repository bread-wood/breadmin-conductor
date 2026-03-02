# Research: GitHub Actions issues Event Trigger for Conductor Research Worker

**Issue:** #164
**Milestone:** v2
**Feature:** feat:ci-deploy
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [GitHub Actions issues Event Capabilities](#github-actions-issues-event-capabilities)
3. [Race Condition Analysis](#race-condition-analysis)
4. [Safe Trigger Configuration](#safe-trigger-configuration)
5. [workflow_dispatch via API as Decoupled Trigger](#workflow_dispatch-via-api-as-decoupled-trigger)
6. [Concurrency Group Configuration](#concurrency-group-configuration)
7. [Alternative: GitHub App Webhook](#alternative-github-app-webhook)
8. [Recommended Trigger Architecture](#recommended-trigger-architecture)
9. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
10. [Sources](#sources)

---

## Executive Summary

Issue #38 (`docs/research/38-ci-server-deployment.md`) recommends `workflow_dispatch` +
schedule cron as the trigger for conductor in CI. Issue #164 investigates whether the
GitHub Actions `issues` event can provide a more immediate trigger (firing when a
`stage/research` issue is labeled) without creating infinite loops or race conditions.

**Key findings:**

1. **The `issues` event with `types: [labeled]` CAN trigger a workflow** when a label is
   added to an issue. It supports filtering, but only at the workflow level (not per-label
   in the event trigger syntax). [DOCUMENTED]

2. **A race condition infinite loop IS a risk.** Conductor's issue-claiming sequence adds
   the `in-progress` label to claimed issues. If the workflow triggers on `labeled`, adding
   `in-progress` would re-trigger the workflow, creating a loop. [DOCUMENTED]

3. **The loop can be broken using GITHUB_TOKEN exclusion.** Workflows triggered by
   `GITHUB_TOKEN` do NOT trigger further workflow runs from their own events. If conductor
   uses `GITHUB_TOKEN` to add labels, the `labeled` event from that label addition will not
   re-trigger the workflow. [DOCUMENTED]

4. **The safest and cleanest trigger architecture** combines:
   - `issues: types: [labeled]` → filter on `stage/research` label addition
   - `GITHUB_TOKEN` for all conductor label operations → prevents re-trigger
   - `concurrency:` group with `cancel-in-progress: false` → queues runs rather than
     cancelling active runs

5. **`workflow_dispatch` via the GitHub API** is a viable alternative for conductor to
   self-trigger after filing a new issue, decoupling the trigger entirely from label events.

---

## GitHub Actions issues Event Capabilities

### Trigger syntax

```yaml
on:
  issues:
    types:
      - labeled
```

This fires when any label is added to any issue. The event payload includes:
- `github.event.label.name` — the label that was just added
- `github.event.issue.number` — the issue number
- `github.event.issue.labels` — all labels currently on the issue

### Label filtering (in-workflow, not in trigger)

The `issues` event trigger does NOT support per-label filtering in the `on:` block. All
`labeled` events fire the workflow; filtering must happen in a job's `if:` condition:

```yaml
jobs:
  research-worker:
    if: github.event.label.name == 'stage/research'
    runs-on: ubuntu-latest
    steps:
      - run: echo "New stage/research issue labeled"
```

**Limitation:** This means the workflow STARTS for every label addition, even if the
`if:` condition causes it to immediately exit. For high-activity repos with many labels,
this creates unnecessary workflow runs.

### Latency comparison with alternatives

| Trigger | Typical latency | Notes |
|---------|----------------|-------|
| `issues: labeled` | ~30-60 seconds | Near-real-time |
| `workflow_dispatch` (manual) | Instant | Requires human action |
| `workflow_dispatch` via API | ~30-60 seconds | Conductor calls API |
| `schedule: cron` | Up to 60 minutes | For background polling |

---

## Race Condition Analysis

### Conductor's label sequence (from `38-ci-server-deployment.md`)

When conductor claims an issue, it:
1. Adds `in-progress` label to the issue
2. Adds `@me` as assignee

If the workflow is triggered by `issues: labeled`, adding `in-progress` triggers another
workflow run. This run sees a `stage/research` issue being labeled (not with `stage/research`,
but with `in-progress`), so the `if: github.event.label.name == 'stage/research'` condition
would be FALSE — the workflow would exit immediately.

**However:** If multiple labels are added in rapid succession, GHA may trigger on each
label addition. The `in-progress` addition is unlikely to re-trigger if the label filter
is strict.

**Real loop risk:** Conductor adding `stage/research` label to a follow-up issue would
trigger the workflow. This IS the intended behavior — a new `stage/research` issue should
trigger the research worker. The loop risk is specifically:

> Conductor runs → sees issue #X → claims it (adds `in-progress`) → does `in-progress` trigger re-run? → NO (if: stage/research filter prevents it)

### GITHUB_TOKEN loop prevention

When a workflow uses `GITHUB_TOKEN` (the default) to perform GitHub API operations (label
addition, comment creation, etc.), those operations do NOT trigger further workflow runs
from the `issues`, `pull_request`, etc. events. [DOCUMENTED]

This is the primary loop-prevention mechanism. Conductor's CI workflow MUST use
`GITHUB_TOKEN` (not a personal access token) for all label operations.

**Exception:** `workflow_dispatch` events triggered by `GITHUB_TOKEN` DO run. This means
conductor can use `GITHUB_TOKEN` to trigger `workflow_dispatch` on itself, creating a
controlled self-trigger without loops.

### Residual risk: labeled event from external actor

If a human (or another tool) manually adds a `stage/research` label to an issue while
a conductor run is active, a second conductor run would start. The concurrency group
(Section 6) handles this by queuing or cancelling the duplicate run.

---

## Safe Trigger Configuration

### Recommended workflow trigger block

```yaml
on:
  issues:
    types:
      - labeled
  workflow_dispatch:
    inputs:
      reason:
        description: "Reason for manual dispatch"
        required: false
        default: "manual"

concurrency:
  group: research-worker
  cancel-in-progress: false  # Queue, don't cancel

jobs:
  research-worker:
    if: |
      github.event_name == 'workflow_dispatch' ||
      (github.event_name == 'issues' &&
       github.event.label.name == 'stage/research' &&
       !contains(github.event.issue.labels.*.name, 'in-progress'))
    runs-on: ubuntu-latest
    permissions:
      issues: write
      contents: read
    steps:
      - name: Run conductor research worker
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}  # Use GITHUB_TOKEN, not PAT
        run: |
          uv run conductor research-worker
```

### Key elements

1. **`!contains(github.event.issue.labels.*.name, 'in-progress')`**: Prevents triggering
   on issues that are already claimed. If `in-progress` is already present when the
   `stage/research` label is added, the workflow skips. This handles the case where
   conductor adds both labels in sequence.

2. **`GITHUB_TOKEN` for label operations**: All conductor label operations (adding
   `in-progress`, removing `in-progress`) use `GITHUB_TOKEN`, preventing re-trigger loops.

3. **`cancel-in-progress: false`**: A new trigger event queues behind the active run
   rather than cancelling it. This prevents lost work when multiple issues are labeled
   quickly.

4. **`workflow_dispatch` support**: Allows manual or API-triggered dispatch as a fallback.

---

## workflow_dispatch via API as Decoupled Trigger

Conductor can trigger itself via the GitHub API after filing a new issue:

```python
import httpx

async def trigger_research_worker(repo: str, github_token: str, reason: str) -> None:
    """Trigger the research-worker workflow via workflow_dispatch."""
    owner, name = repo.split("/")
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.github.com/repos/{owner}/{name}/actions/workflows/research-worker.yml/dispatches",
            headers={"Authorization": f"token {github_token}"},
            json={
                "ref": "main",
                "inputs": {"reason": reason}
            }
        )
        resp.raise_for_status()
```

**Advantage:** Conductor controls exactly when the trigger fires (after issue creation +
label assignment), rather than reacting to label events. This avoids the race condition
entirely.

**Disadvantage:** Requires conductor to be "self-aware" of its CI deployment. In a
personal-machine deployment (not CI), this call would need to be gated.

---

## Concurrency Group Configuration

### cancel-in-progress: false vs. cancel-in-progress: true

| Setting | Behavior | When to use |
|---------|----------|-------------|
| `cancel-in-progress: false` | New runs queue behind active run | Research worker (long-running) |
| `cancel-in-progress: true` | New run cancels active run | Deployment workflows |

For conductor's research worker, `cancel-in-progress: false` is correct. A running
research worker may have active agent sessions. Cancelling mid-run leaves `in-progress`
labels orphaned.

### Known limitation: only one pending run

GHA concurrency queues allow only ONE pending run at a time. If three issues are labeled
while a run is active, only the last trigger event will queue; the previous two are
discarded. For conductor, this means some `stage/research` label events may not trigger
immediate runs.

**Mitigation:** The conductor research worker should process ALL open `stage/research`
issues (not just the one that triggered it) in a single run. The trigger is just a
wakeup signal, not an issue-specific selector.

---

## Alternative: GitHub App Webhook

A GitHub App can receive issue webhooks via an HTTP server rather than GHA. The App
would:
1. Receive `issues` webhook event (label added)
2. Validate the event is `stage/research` label addition
3. Send a `workflow_dispatch` event to trigger the conductor workflow (or run conductor
   directly if co-hosted)

**Viability for conductor:** Low for personal/low-ops deployments. A GitHub App requires
hosting an HTTP endpoint that stays up 24/7. This adds operational complexity that
contradicts conductor's design goal of minimal infrastructure.

**Conclusion:** GitHub App webhook is not recommended for v2. Use the `issues: labeled`
trigger + `workflow_dispatch` hybrid instead.

---

## Recommended Trigger Architecture

**Primary recommendation: Hybrid trigger**

```
issues: labeled (stage/research) → immediate wakeup for new issues
workflow_dispatch → manual and API-triggered fallback
schedule: cron (every 4 hours) → catch any missed label events
```

This triple-trigger approach ensures:
- New `stage/research` issues start a run within ~60 seconds
- Manual override is always available
- Background cron catches any label events missed during active runs (due to the one-pending-run limitation)

**workflow_dispatch via API from conductor itself** is the cleanest mechanism for the
conductor-files-its-own-follow-up-issue case. After creating a new follow-up issue and
adding `stage/research`, conductor calls the GitHub API to dispatch itself.

---

## Follow-Up Research Recommendations

**[WONT_RESEARCH] GitHub App webhook for conductor**
Too much operational complexity for personal deployments. Not worth researching further
for v2.

**[V2_RESEARCH] Test one-pending-run limitation impact on conductor**
The GHA concurrency queue discards all but the latest pending trigger. If conductor
files multiple follow-up issues in one run, only the last `stage/research` label event
queues. Verify whether this causes issues in practice, or whether the background cron
is sufficient to catch missed events.

---

## Sources

- [GitHub Docs: Workflow syntax — on.issues](https://docs.github.com/en/actions/using-workflows/workflow-syntax-for-github-actions)
- [GitHub Docs: Control concurrency of workflows and jobs](https://docs.github.com/actions/writing-workflows/choosing-what-your-workflow-does/control-the-concurrency-of-workflows-and-jobs)
- [GitHub Community Discussion #26970: Workflow infinite loop](https://github.com/orgs/community/discussions/26970)
- [GitHub Community Discussion #5435: Concurrency Cancel Pending](https://github.com/orgs/community/discussions/5435)
- [GitHub Community Discussion #69337: Race condition with pull_request workflows](https://github.com/orgs/community/discussions/69337)
- [futurestud.io: GitHub Actions — Limit Concurrency and Cancel In-Progress Jobs](https://futurestud.io/tutorials/github-actions-limit-concurrency-and-cancel-in-progress-jobs)
- [Codefresh: GitHub Actions Triggers — 5 Ways to Trigger a Workflow](https://codefresh.io/learn/github-actions/github-actions-triggers-5-ways-to-trigger-a-workflow-with-code/)
- [oneuptime: How to Control Concurrency in GitHub Actions](https://oneuptime.com/blog/post/2026-01-25-github-actions-concurrency-control/view)
