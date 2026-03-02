Plan the next set of milestones for this repository.

Run this at two points:
1. **Project start** — plan MVP and the version after it
2. **When implementation begins** — plan the next research phase so it runs in parallel

## When to Run

- Before the first research-worker session (plan MVP research + MVP impl milestones)
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

### Spec Seeding (`--spec`)

When `--spec <path>` is passed to `composer plan-milestones`, the CLI resolves and copies the
spec file into the target repo **before** this skill runs. By the time this skill executes,
`docs/specs/<version>.md` is already committed in the target repo — proceed directly to Step 0.

**How it works:**
- `--spec` accepts a relative path (resolved from cwd) or an absolute path to a `.md` file.
- The version name is inferred from the spec filename stem (e.g. `calculator.md` → `calculator`)
  unless `--version` is explicitly provided.
- If `docs/specs/<version>.md` already exists in the target repo, the copy is skipped and a
  warning is printed; the skill proceeds with the existing file.
- The spec file is copied to `docs/specs/<version>.md`, then `git add`ed and committed with
  the message `docs: seed spec from <source path>`.

**Example invocations:**
```bash
# Relative path (resolved from cwd)
composer plan-milestones --repo calculator-cli --spec docs/specs/calculator.md

# Absolute path
composer plan-milestones --repo calculator-cli --spec /Users/me/specs/calculator.md

# Override version name
composer plan-milestones --repo calculator-cli --spec docs/specs/calculator.md --version MVP
```

## Execution

### Step 0 — Read the Spec

Before doing anything else, locate and read the spec file for the target version:

```bash
cat docs/specs/<version>.md
```

**If the spec file does not exist**, halt immediately with this error:

```
Error: No spec found at docs/specs/<version>.md.

Before running plan-milestones, a human must write the spec using the template:
  docs/specs/TEMPLATE.md

Write the spec, commit it, and then re-run plan-milestones.
```

Do not invent scope, goals, or research questions. Everything in Steps 1–4 must be grounded
in the spec. The spec is the single source of truth.

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

Use the spec's **Scope**, **Constraints**, and **Success Criteria** sections to anchor the
version scope. Do not override or extend what the spec defines.

**Version naming**: choose a meaningful identifier (MVP, v1.1, v2, etc.) — not M3/M4.
The milestone titles must contain either "Research" or "Implementation" (or "Impl") so
the pipeline workers can identify their type.

**Scope document** — write a brief version scope in your thinking before creating anything.
This must mirror the spec — not re-derive it:

```
Version: <name>
Goal: <from spec Overview>
Included: <from spec Scope section>
Excluded (next version): <from spec Non-Goals section>
Seed research questions: <from spec Key Unknowns section — not invented>
Constraints: <from spec Constraints section>
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

File seed research issues for the new research milestone. Source the research questions
**directly from the spec's Key Unknowns section** — do not invent questions.

Apply the `[BLOCKS_IMPL]` filter: only file an issue if not knowing the answer would block
implementation. A Key Unknown in the spec that is answerable in 10 seconds or that does not
change the design is not worth a standalone issue — note it inline instead.

For each qualifying Key Unknown:

```bash
gh issue create \
  --repo <owner>/<repo> \
  --title "Research: <question>" \
  --label "research" \
  --milestone "<Version> Research" \
  --body "$(cat <<'EOF'
## Background
<Why this question matters for implementation>

## Spec Reference
Derived from the Key Unknowns section of docs/specs/<version>.md:
> <exact quote of the Key Unknown from the spec>

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

### Step 5 — Report

Print:
- Milestones created (names, purposes)
- Seed research issues filed (numbers, titles)
- Explicit scope boundaries (what's in, what's out, what's next) — sourced from the spec
- Suggested order of operations: which research issues to dispatch first

File the next pipeline stage issue:
```bash
gh issue create --title "Run research-worker for <research milestone>" --label "pipeline" --milestone "<research milestone>"
```

Post a Notion report under "CC Autonomous Coding Sessions"
(parent page ID: `317bb275-6a02-803d-a59f-dc56c3527942`) with:
- **Title**: `Milestone Plan — {YYYY-MM-DD} — {repo name} — {version name}`
- **Body**: version scope (from spec), milestones created, seed issues, next steps

## Constraints

- **Spec is required** — plan-milestones must not run without a spec file at `docs/specs/<version>.md`
- **Spec is authoritative** — scope, constraints, and research questions come from the spec; do not override or extend them
- **Maximum 2 versions planned at once** — never plan 3 versions ahead
- **Seed issues only** — do not attempt to enumerate all research questions; research-worker discovers more
- **Scope boundaries are explicit** — every version plan must say what is out of scope and why (sourced from spec Non-Goals)
- **No implementation issues** — plan-milestones creates only research milestones and seed research issues; design-worker creates impl issues after research completes
