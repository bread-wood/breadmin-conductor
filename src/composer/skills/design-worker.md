Start the design worker for this repository in autonomous mode.

The design worker translates completed research into fully-specified implementation issues.
It runs **after** research is declared complete (research-worker Step 4 verdict) and **before**
impl-worker begins. It produces no code — only well-specified GitHub issues.

## Setup

1. Detect the default branch:
   ```bash
   DEFAULT_BRANCH=$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name')
   git checkout $DEFAULT_BRANCH && git pull origin $DEFAULT_BRANCH
   ```

2. Run startup checks per the Orchestrator-Dispatch Protocol in ~/.claude/CLAUDE.md.

3. Read the project-level CLAUDE.md to understand module scope and labels.

## Inputs

The design worker requires:
- `--research-milestone` — the research milestone whose docs to translate
- `--repo` — the target repository

Identify the corresponding implementation milestone:
```bash
gh milestone list --repo <owner>/<repo>
```
Look for a milestone whose title contains "Impl" or "Implementation" for the same version.
If it doesn't exist, create it:
```bash
gh api repos/<owner>/<repo>/milestones \
  -f title="<Version> Implementation" \
  -f description="Implementation phase for <version>"
```

## Execution

### Step 1 — Read All Research Docs

```bash
gh issue list --state closed --label research --milestone "<research-milestone>" \
  --json number,title --limit 200
```

For each closed research issue, find and read its doc in `docs/research/`:
```bash
ls docs/research/
```

Read every doc that corresponds to the research milestone. Build a mental model of:
- What is now known (key findings, constraints, chosen approaches)
- What was explicitly deferred or marked `[V2_RESEARCH]`
- Any blocking contradictions or unresolved `[INFERRED]` claims that affect design

### Step 2 — Audit Existing Impl Issues

Check what implementation issues already exist for the impl milestone to avoid duplication:
```bash
gh issue list --state open --milestone "<impl-milestone>" --limit 200 \
  --json number,title,labels,body
```

### Step 3 — Design Implementation Issues

For each logical unit of work needed to implement the findings:

1. **Define scope** — which source files does it touch? (reference CLAUDE.md module isolation)
2. **Write acceptance criteria** — concrete, testable conditions for "done"
3. **Write test requirements** — what unit/integration tests must pass
4. **Identify dependencies** — which other impl issues must complete first?
5. **Assign label** — use the appropriate `feat:*` label from CLAUDE.md

File the issue:
```bash
gh issue create \
  --repo <owner>/<repo> \
  --title "<imperative verb phrase>" \
  --label "<feat:*>,<infra if applicable>" \
  --milestone "<impl-milestone>" \
  --body "$(cat <<'EOF'
## Context
<1-2 sentences: what research finding this implements and why it's needed>

## Acceptance Criteria
- [ ] <concrete, testable criterion 1>
- [ ] <concrete, testable criterion 2>
- [ ] ...

## Scope
Files to create or modify:
- `<path>` — <what changes>

## Test Requirements
- <what must be unit-tested>
- <what must be integration-tested (if any)>

## Dependencies
<Depends on: #N, or "None">

## Key Design Decisions
<Bullet list of non-obvious decisions made by research, so the impl agent doesn't re-litigate them>
EOF
)"
```

### Step 4 — Set Dependency Order

After all issues are filed, review their dependencies and verify the order is correct.
If any dependency is missing or circular, fix it before finishing.

Print the planned execution order:
```
#N1 (no deps) → #N2 (depends on N1) → #N3 (depends on N1, N2) → ...
```

### Step 5 — Report

Print a summary:
- Implementation milestone name
- Issues created (number, title, label, deps)
- Execution order
- Any research gaps that prevented full spec (flag as follow-up issues in the research milestone)
- **Verdict**: "Ready for impl-worker — N issues filed, dependency graph complete."

File the next pipeline stage issue:
```bash
gh issue create --title "Run impl-worker for <impl milestone>" --label "pipeline" --milestone "<impl milestone>"
```

Post a Notion report under "CC Autonomous Coding Sessions"
(parent page ID: `317bb275-6a02-803d-a59f-dc56c3527942`) with:
- **Title**: `Design Session — {YYYY-MM-DD} — {repo name}`
- **Body**: research milestone processed, implementation issues filed, execution order, gaps

## Constraints

- **No code** — design-worker creates issues only, never touches source files
- **No dispatching** — design-worker does not launch sub-agents
- **Spec first** — every issue must have acceptance criteria and scope before being filed
- **One issue per logical unit** — split by module boundary (see CLAUDE.md module isolation)
- **No gold-plating** — spec only what research explicitly determined; don't invent scope
- **Post session report to Notion** when done
