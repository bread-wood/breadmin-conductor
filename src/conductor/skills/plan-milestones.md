Plan the next set of milestones for this repository.

Run this at two points:
1. **Project start** — plan MVP and the version after it
2. **When implementation begins** — plan the next research phase so it runs in parallel

## When to Run

- Before the first research-worker session (plan MVP research + MVP impl milestones)
- When impl-worker begins a version (plan the research phase for version N+1)
- Never plan more than one version ahead — research findings change scope

## Setup

```bash
DEFAULT_BRANCH=$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name')
git checkout $DEFAULT_BRANCH && git pull origin $DEFAULT_BRANCH
```

Read the project CLAUDE.md to understand the domain and constraints.

## Execution

### Step 1 — Assess Current State

```bash
# What milestones already exist?
gh milestone list --repo <owner>/<repo>

# What has been built so far?
gh issue list --state closed --limit 200 --json number,title,milestone,labels

# What is in progress or planned?
gh issue list --state open --limit 200 --json number,title,milestone,labels
```

Read the last 3–5 session Notion reports if available to understand what was completed
and what gaps or follow-ups were identified.

### Step 2 — Define the Next Version Scope

Based on what exists, determine the next version to plan. Use the following principles:

**Version naming**: choose a meaningful identifier (MVP, v1.1, v2, etc.) — not M3/M4.
The milestone titles must contain either "Research" or "Implementation" (or "Impl") so
the pipeline workers can identify their type.

**Scope heuristic**:
- MVP / first version: the smallest useful thing — what must work for a single human operator?
- Each subsequent version: one coherent capability increment — not a kitchen sink
- A version's research phase answers only what's needed for *that version's* implementation

**Scope document** — write a brief version scope in your thinking before creating anything:
```
Version: <name>
Goal: <one sentence>
Included: <3-5 bullet points of what this version delivers>
Excluded (next version): <what's explicitly out of scope>
Seed research questions: <3-7 open questions that must be answered before implementation>
```

### Step 3 — Create Milestones

```bash
# Research milestone
gh api repos/<owner>/<repo>/milestones \
  -f title="<Version> Research" \
  -f description="<one-line goal for the research phase>"

# Implementation milestone
gh api repos/<owner>/<repo>/milestones \
  -f title="<Version> Implementation" \
  -f description="<one-line goal for the implementation phase>"
```

### Step 4 — File Seed Research Issues

File 3–7 seed research issues for the new research milestone. These are the highest-priority
blocking questions — not an exhaustive list. The research-worker will discover more.

For each seed issue:
```bash
gh issue create \
  --repo <owner>/<repo> \
  --title "Research: <question>" \
  --label "research" \
  --milestone "<Version> Research" \
  --body "$(cat <<'EOF'
## Background
<Why this question matters for implementation>

## Research Areas
- <specific sub-question 1>
- <specific sub-question 2>

## Deliverable
A research doc at docs/research/<NN>-<slug>.md answering the above.

## Dependencies
<Depends on: #N, or "None">
EOF
)"
```

Apply the `[BLOCKS_IMPL]` standard: only file issues that would block the implementation
phase if unanswered. Nice-to-have questions are noted in a comment, not as issues.

### Step 5 — Report

Print:
- Milestones created (names, purposes)
- Seed research issues filed (numbers, titles)
- Explicit scope boundaries (what's in, what's out, what's next)
- Suggested order of operations: which research issues to dispatch first

Post a Notion report under "CC Autonomous Coding Sessions"
(parent page ID: `317bb275-6a02-803d-a59f-dc56c3527942`) with:
- **Title**: `Milestone Plan — {YYYY-MM-DD} — {repo name} — {version name}`
- **Body**: version scope, milestones created, seed issues, next steps

## Constraints

- **Maximum 2 versions planned at once** — never plan 3 versions ahead
- **Seed issues only** — do not attempt to enumerate all research questions; research-worker discovers more
- **Scope boundaries are explicit** — every version plan must say what is out of scope and why
- **No implementation issues** — plan-milestones creates only research milestones and seed research issues; design-worker creates impl issues after research completes
