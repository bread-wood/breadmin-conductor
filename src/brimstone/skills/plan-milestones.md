Plan the next milestone and file a complete research queue for it.

## When to Run

- Before the first research-worker session (plan MVP)
- When impl-worker begins a version (plan the research phase for version N+1)
- Never plan more than one version ahead — research findings change scope

## Target Repository

The `--repo` argument controls which repository this worker operates on:

| Invocation | Behaviour |
|---|---|
| *(no flag)* | Operate on the current working directory. Fails if cwd is not a git repo. |
| `--repo owner/name` | Operate on an existing remote GitHub repo. All `gh` commands use `--repo owner/name`. |
| `--repo name` | Scaffold a new private GitHub repo named `name`, then operate on it. |
| `--repo path/to/local/dir` | Operate on the local directory. Fails if it is not a git repo. |

All `gh` commands in this skill must be scoped with `--repo <owner>/<name>` when the target is a remote repo.
All `git` commands must be run with `-C <local_path>` (or inside the cloned directory) when operating on a local path.

## Setup

```bash
DEFAULT_BRANCH=$(gh repo view --repo <owner>/<repo> --json defaultBranchRef --jq '.defaultBranchRef.name')
git -C <local_path> checkout $DEFAULT_BRANCH && git -C <local_path> pull origin $DEFAULT_BRANCH
```

Read the project CLAUDE.md to understand the domain and constraints.

## Execution

### Step 0 — Read the Spec

Before doing anything else, locate and read the spec file for the target version.

**For remote repos** (`owner/name` format — running headless outside the repo checkout):
```bash
gh api repos/<owner>/<repo>/contents/docs/specs/<version>.md --jq '.content' | base64 -d
```

**For local repos** (path format — running inside or alongside the repo checkout):
```bash
cat <local_path>/docs/specs/<version>.md
```

**If the spec file does not exist** (the API returns a 404 or the local file is missing), halt immediately with this error:

```
Error: No spec found at docs/specs/<version>.md.

Before running plan-milestones, a human must write the spec using the template:
  docs/specs/TEMPLATE.md

Write the spec, commit it, and then re-run plan-milestones.
```

The spec defines scope, constraints, and success criteria. It is the authority on **what** to build.
Your job in the steps below is to figure out **how** — decomposing every aspect that requires
research before implementation can begin.

### Step 1 — Assess Current State

```bash
# What milestones already exist? (save for dup check in Step 3)
EXISTING_MILESTONES=$(gh api repos/<owner>/<repo>/milestones --paginate -q '.[].title')

# What issues already exist? (save titles for dup check in Steps 4–5)
EXISTING_ISSUES=$(gh issue list --repo <owner>/<repo> --state all --limit 500 \
  --json title --jq '.[].title')

# What has been built so far?
gh issue list --repo <owner>/<repo> --state closed --limit 200 --json number,title,milestone,labels

# What is in progress or planned?
gh issue list --repo <owner>/<repo> --state open --limit 200 --json number,title,milestone,labels
```

### Step 2 — Define the Version Scope

Write a brief scope summary in your thinking before creating anything.
Mirror the spec exactly — do not add or remove scope:

```
Version: <name>
Goal: <from spec Overview>
Included: <from spec Scope section>
Excluded (next version): <from spec Non-Goals section>
Constraints: <from spec Constraints section>
```

**Version naming**: use a meaningful identifier (MVP, v1.1, v2, etc.) — not M3/M4.
The milestone title is the version name only (e.g. `v1`, `MVP`). Do NOT append "Research"
or "Implementation" — all stages for a version share one milestone; workers select work
by `stage/*` label, not by milestone name.

### Step 3 — Create Milestone

Create a single milestone for the version. Skip if it already exists.

```bash
if echo "$EXISTING_MILESTONES" | grep -qxF "<Version>"; then
  echo "Milestone '<Version>' already exists — skipping creation"
else
  gh api repos/<owner>/<repo>/milestones \
    -f title="<Version>" \
    -f description="<one-line goal for this version>"
fi
```

### Step 4 — Decompose Into a Full Research Queue

**Goal**: produce a complete set of research issues so that when research-worker finishes,
design-worker has everything it needs to write the HLD and LLDs without any unknowns.

Specs are intentionally high-level. Do not limit yourself to the spec's "Key Unknowns"
section — treat it as one input among many. Your job is to reason about every aspect of the
implementation and ask: *what do we need to know before we can design this?*

#### Decomposition dimensions

Work through every dimension below. For each one, ask whether there is a genuine unknown
that would affect a design decision. If yes, file a research issue.

**Architecture & approach**
- What architectural patterns are appropriate? Are there established conventions in this
  domain or ecosystem we should follow?
- Are there multiple viable approaches? What are the trade-offs?
- What are the system boundaries and integration points?

**APIs, libraries, and tooling**
- Which libraries or tools are candidates? What are the trade-offs (maturity, license,
  maintenance, performance)?
- What are the actual APIs we'll be calling? Are there undocumented behaviours or
  known edge cases?
- Are there version or compatibility constraints?

**Data models and state**
- What data needs to be stored, passed, or transformed?
- What are the schema options and their trade-offs?
- What are the consistency, ordering, or concurrency requirements?

**External integrations and protocols**
- What external services, APIs, or protocols does this touch?
- What are their auth, rate-limiting, error, and retry behaviours?
- Are there SDK wrappers or must we use raw HTTP?

**Error handling, edge cases, and failure modes**
- What are the failure modes for each component?
- What does partial failure look like? What needs to be recoverable vs restartable?
- What inputs or states can cause hard-to-debug failures?

**Security and trust**
- What are the trust boundaries? What is user-controlled vs system-controlled?
- Are there injection, credential, or privilege-escalation risks?
- What needs to be validated at each boundary?

**Testing strategy**
- What can be unit-tested vs what needs integration or end-to-end tests?
- What are the hard-to-test parts? What mocking or test-double strategies work here?
- Are there existing test patterns in the repo to follow?

**Performance and resource constraints**
- Are there latency, throughput, or memory requirements that affect design choices?
- What are the expected load characteristics?

**Developer experience and operations**
- How will this be configured, deployed, or operated?
- What observability (logging, metrics, tracing) is needed?
- Are there CLI UX conventions or user-facing error message standards to follow?

#### Filing research issues

For each genuine unknown identified above, apply the `[BLOCKS_IMPL]` filter before filing:

> **File an issue only if** not knowing the answer would cause a design-level rework of
> an implementation task. Skip questions answerable in seconds or that only affect
> fine-grained implementation details.

For each issue that passes the filter, check for a duplicate before filing:

```bash
TITLE="Research: <concise question>"
if echo "$EXISTING_ISSUES" | grep -qxF "$TITLE"; then
  echo "Issue '$TITLE' already exists — skipping"
else
  gh issue create \
    --repo <owner>/<repo> \
    --title "$TITLE" \
    --label "research,stage/research" \
    --milestone "<Version>" \
    --body "$(cat <<'EOF'
## Why This Matters
<How the answer changes a design decision. Be specific — name the implementation component affected.>

## Research Areas
- <specific sub-question 1>
- <specific sub-question 2>
- <specific sub-question 3>

## Acceptance Criteria
The research doc must answer the above questions and include a concrete recommendation
(not just a list of options) with rationale.

## Deliverable
A research doc at docs/research/<Version>/<NN>-<slug>.md

## Dependencies
<Depends on: #N, or "None">
EOF
)"
fi
```

Group related sub-questions into a single issue rather than filing one issue per sub-question.
Aim for 4–10 well-scoped issues that together give design-worker complete coverage.

### Step 5 — Report

Print:
- Milestone created (name, goal)
- All research issues filed (numbers, titles, one-line rationale)
- Scope boundaries: what's in, what's out, what's deferred
- Suggested dispatch order: which issues to send to research-worker first (dependency order)

File the next pipeline stage issue (skip if it already exists):
```bash
PIPELINE_TITLE="Run research-worker for <version>"
if echo "$EXISTING_ISSUES" | grep -qxF "$PIPELINE_TITLE"; then
  echo "Pipeline issue '$PIPELINE_TITLE' already exists — skipping"
else
  gh issue create \
    --repo <owner>/<repo> \
    --title "$PIPELINE_TITLE" \
    --label "pipeline" \
    --milestone "<version>"
fi
```

## Constraints

- **Spec is required** — plan-milestones must not run without a spec file at `docs/specs/<version>.md`
- **Spec defines scope** — do not add features or research questions outside the spec's scope
- **Decompose fully** — the spec's Key Unknowns section is a starting point, not a ceiling;
  infer and file every research question needed for design-worker to have complete coverage
- **File an issue per topic, not per sub-question** — group related questions; aim for 4–10 issues
- **`[BLOCKS_IMPL]` filter** — skip questions that don't affect a design decision
- **Maximum 2 versions planned at once** — never plan 3 versions ahead
- **No implementation issues** — plan-milestones creates only the milestone and research issues;
  design-worker creates impl issues after research completes
- **Scope boundaries are explicit** — every plan must state what is out of scope and why
